"""News intelligence layer — Haiku 4.5 reads the tape's headlines every cycle.

Tier 1 of the two-model pipeline. Every 30 minutes this module scrapes fresh headlines
(Google News RSS + Finnhub company news, both free) and hands them to Haiku 4.5, which is
cheap enough to run on every cycle. Haiku does *not* propose trades — it classifies:

  - per ticker: sentiment, materiality, catalyst type, a one-line read
  - macro: what the day's cross-asset story is
  - which headlines actually matter vs. which are noise/recycled

Tier 2 (deep.py, Opus 5) then makes the trade decision, reasoning over Haiku's read plus
the deterministic indicators. Keeping the split means a quiet news day costs almost nothing
and a real catalyst gets escalated to the expensive model within 30 minutes.

Everything degrades: no subscription, a parse failure, or a timeout yields a deterministic
keyword-scored fallback so the cycle keeps its news signal instead of losing it.
"""
from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor

from . import collect, config, deep, jsonio, news_quality

# Deterministic fallback vocabulary — used only when Haiku is unreachable.
_BULL_WORDS = (
    "beat", "beats", "record", "surge", "soar", "upgrade", "raises guidance", "raised guidance",
    "wins", "approval", "approved", "buyback", "outperform", "strong demand", "partnership",
)
_BEAR_WORDS = (
    "miss", "misses", "cut", "cuts guidance", "downgrade", "probe", "investigation", "lawsuit",
    "recall", "layoff", "layoffs", "warns", "warning", "halts", "delay", "delays", "sues",
)
_CATALYST_WORDS = {
    "earnings": ("earnings", "q1", "q2", "q3", "q4", "results", "eps", "revenue"),
    "guidance": ("guidance", "outlook", "forecast"),
    "product": ("launch", "unveil", "announces", "release", "chip", "model"),
    "regulatory": ("sec ", "ftc", "doj", "antitrust", "probe", "investigation", "lawsuit", "tariff"),
    "analyst": ("upgrade", "downgrade", "price target", "initiated", "overweight", "underweight"),
    "macro": ("fed", "powell", "cpi", "inflation", "rates", "jobs", "trump", "tariffs"),
}


# ── Headline gathering (free sources, parallel) ──────────────────────────────────

def _merge_stats(total, part):
    for k in ("seen", "kept", "dropped", "under_covered", "repeats"):
        total[k] = total.get(k, 0) + part.get(k, 0)
    for reason, n in (part.get("drop_reasons") or {}).items():
        total.setdefault("drop_reasons", {})
        total["drop_reasons"][reason] = total["drop_reasons"].get(reason, 0) + n
    return total


def gather_headlines(tickers, per_ticker=None, max_workers=6, store=None, with_sec=None,
                     stats=None):
    """Scrape wide, then keep only the least-saturated few per ticker.

    Scraping stays parallel; scoring is deliberately sequential because the seen-headline store
    is shared mutable state and a story's outlet count is only correct if every ticker's
    headlines are folded into the same store in a defined order.
    """
    per_ticker = per_ticker or config.NEWS_HEADLINES_PER_TICKER
    candidates = max(per_ticker, config.NEWS_CANDIDATES_PER_TICKER)
    with_sec = config.NEWS_SEC_FILINGS if with_sec is None else with_sec

    def _job(t):
        items = []
        if config.is_crypto(t):
            # All three equity sources are dead ends for a token: there is no EDGAR filer behind
            # BTC, Finnhub's company-news endpoint keys on an equity symbol and returns nothing,
            # and "BTC-USD stock" as a search string returns coverage of the ETFs rather than of
            # the asset. So the crypto track runs on one well-aimed Google News query instead of
            # three broad ones, and asks it for the full candidate count since nothing else feeds
            # the quality scorer.
            query = config.CRYPTO_NEWS_QUERIES.get(t) or f"{config.label_for(t)} crypto"
            return t, collect.google_news(query, limit=candidates)
        if with_sec:
            items += collect.sec_filings(t, days=4, limit=3)
        items += collect.finnhub_news(t, days=3, limit=candidates)
        items += collect.google_news(f"{t} stock", limit=max(4, candidates // 2))
        return t, items

    raw = {}
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        for t, items in ex.map(_job, tickers):
            raw[t] = items

    own_store = store is None
    store = news_quality.load_store() if own_store else store
    totals = {"seen": 0, "kept": 0, "dropped": 0, "under_covered": 0, "repeats": 0,
              "drop_reasons": {}}
    out = {}
    for t, items in raw.items():
        scored = news_quality.observe(items, ticker=t, store=store)
        kept, dropped = news_quality.rank(scored, limit=per_ticker)
        _merge_stats(totals, news_quality.summarize(kept, dropped))
        if kept:
            out[t] = kept
    if own_store:
        news_quality.save_store(store)
    if totals["seen"]:
        print(f"[news] {totals['seen']} headline(s) scraped, {totals['kept']} kept "
              f"({totals['under_covered']} under-covered), {totals['dropped']} filtered "
              f"({totals['repeats']} already reported) {totals['drop_reasons']}")
    if stats is not None:
        stats.update(totals)
    return out


_MACRO_QUERIES = {
    "stock": ["stock market today", "Federal Reserve interest rates", "S&P 500 outlook"],
    # The crypto tape has its own macro, and it is not the S&P's. What moves all three tokens at
    # once is ETF flow, regulation, and a large liquidation cascade — none of which appear in an
    # equity macro sweep. Rates stay on the list because the dollar and real yields still set the
    # risk backdrop this book trades inside.
    "crypto": ["crypto market today", "bitcoin ETF flows", "crypto regulation SEC",
               "crypto liquidations"],
}


def gather_macro_headlines(limit=8, store=None, venue="stock"):
    """Market-wide headlines: what is moving the whole tape right now.

    `venue` picks which tape. The store key is separated with it, so a crypto sweep's outlet counts
    never inflate the crowding score of an equity headline covering the same story.
    """
    queries = _MACRO_QUERIES.get(venue, _MACRO_QUERIES["stock"])
    items = []
    for q in queries:
        items += collect.google_news(q, limit=limit)
    own_store = store is None
    store = news_quality.load_store() if own_store else store
    scored = news_quality.observe(items, ticker="_MACRO" if venue == "stock" else f"_MACRO_{venue.upper()}",
                                  store=store)
    kept, _ = news_quality.rank(scored, limit=limit)
    if own_store:
        news_quality.save_store(store)
    return kept


# ── Haiku pass ──────────────────────────────────────────────────────────────────

_SCHEMA = """{
  "macro_read": "2-3 sentences: what today's news flow means for US equities right now",
  "macro_sentiment": -100..100,
  "tickers": {
    "TICKER": {
      "sentiment": -100..100,
      "materiality": "high" | "medium" | "low",
      "catalyst": "earnings" | "guidance" | "product" | "regulatory" | "analyst" | "macro" | "none",
      "summary": "one line: the single most important thing the news says about this name",
      "is_fresh": true|false,
      "crowding": "under-covered" | "mixed" | "saturated",
      "edge": "what a reader who only follows mainstream coverage would NOT already know, or null",
      "headlines_used": [{"title": "...", "source": "...", "link": "..."}]
    }
  },
  "top_stories": [{"title": "...", "source": "...", "link": "...", "why_it_matters": "..."}]
}"""

_INSTRUCTIONS = """You are the Market Brain's news analyst (tier 1 of 2). You read headlines and
classify them. You do NOT propose trades, prices, entries, or stops — a separate model does that.

Every headline arrives pre-scored by the harness. Use those fields, do not re-guess them:
- `source_tier`: "primary" is the SEC filing itself, "wire/first-hand" broke the story,
  "mainstream" reported it, "aggregator" rewrote someone else's work hours later.
- `outlets`: how many distinct outlets have run this same story. `crowding` summarises it —
  "saturated" means the market has already read this everywhere.
- `age_min`: minutes since the story first appeared (ours or the publisher's timestamp,
  whichever is older). A story hours old is already in the price.
- `summary`: the article's own lede when the feed gave us one.
- `low_quality: true` means nothing better existed for that ticker — treat it as thin coverage.
- `already_reported: true` means we cited this exact story in an earlier cycle and no new outlet
  has carried it since. It is not new information however big it sounds: materiality "low",
  is_fresh false, and do not put it in top_stories. It only reached you because nothing else
  existed for that name — say so in summary rather than reporting it again.

Rules:
- Judge MATERIALITY honestly. "high" means this headline can move the stock today AND is new
  information. A saturated story everyone has already traded is at most "medium" no matter how
  dramatic it sounds. Recycled summaries and filler are "low". Most names are "low".
- An SEC filing outranks any article rewriting it. If both are present, cite the filing.
- Prefer under-covered, high-tier stories in headlines_used and top_stories. An obscure item from
  a primary source is worth more to us than the same fact from ten aggregators.
- `edge` is where the value is: state what this specific coverage reveals that the crowd has not
  priced. If the whole story is already everywhere, set edge to null and say so in summary.
- SENTIMENT is about the news content only, not the chart.
- is_fresh = false if the headlines look like restatements of old news.
- Use ONLY the headlines given to you. Never invent a headline, a source, or a link. If a
  ticker's headlines say nothing meaningful, give it materiality "low" and catalyst "none".
- Every headline you cite in headlines_used must appear verbatim in the input.
- Include only tickers you were given. Skip a ticker entirely rather than guessing about it.

Respond with ONE JSON object and nothing else — no prose, no code fence."""

_PROMPT_FIELDS = ("title", "source", "published", "link", "summary", "form")


def _for_prompt(items):
    """Trim a scored headline to what the model needs — the score, not the bookkeeping."""
    out = []
    for n in items or []:
        rec = {k: n[k] for k in _PROMPT_FIELDS if n.get(k)}
        if rec.get("summary"):
            rec["summary"] = rec["summary"][:280]
        rec["source_tier"] = n.get("tier_name")
        rec["outlets"] = n.get("outlets")
        rec["crowding"] = n.get("crowding")
        rec["age_min"] = n.get("age_min")
        if n.get("low_quality"):
            rec["low_quality"] = True
        # Only reaches the model through the keep-min valve — a repeat is normally filtered out
        # before this. When it does arrive it has to arrive labelled, or the model reports our own
        # last cycle back to us as news.
        if n.get("repeat"):
            rec["already_reported"] = True
        out.append(rec)
    return out


def _build_prompt(headlines, macro_headlines, calendar):
    payload = {
        "macro_headlines": _for_prompt(macro_headlines),
        "ticker_headlines": {t: _for_prompt(v) for t, v in (headlines or {}).items()},
        "calendar_imminent": (calendar or {}).get("imminent", [])[:8],
        "calendar_speeches": (calendar or {}).get("speeches", [])[:6],
    }
    return (
        f"{_INSTRUCTIONS}\n\nSchema:\n{_SCHEMA}\n\nHEADLINES:\n```json\n"
        + jsonio.dumps(payload, indent=2)
        + "\n```\n"
    )


def analyze(headlines, macro_headlines=None, calendar=None, use_claude=True):
    """Run the Haiku news pass. Always returns a dict (falls back deterministically)."""
    macro_headlines = macro_headlines if macro_headlines is not None else []
    if not headlines and not macro_headlines:
        return {"macro_read": "No headlines retrieved this cycle.", "macro_sentiment": 0,
                "tickers": {}, "top_stories": [], "model": "none", "degraded": True}
    if not use_claude:
        return _fallback(headlines, macro_headlines)
    try:
        prompt = _build_prompt(headlines, macro_headlines, calendar)
        result = deep.run_claude(prompt, model=config.CLAUDE_LITE_MODEL, attempts=2,
                                 timeout=config.CLAUDE_LITE_TIMEOUT)
        result = _sanitize(result, headlines)
        result["model"] = config.CLAUDE_LITE_MODEL
        result["degraded"] = False
        n_high = sum(1 for v in result["tickers"].values() if v.get("materiality") == "high")
        print(f"[news] Haiku read {len(headlines)} name(s), {n_high} material")
        return result
    except Exception as e:
        reason = getattr(e, "reason", None) or str(e)
        kind = getattr(e, "kind", "other")
        print(f"[warn] news intel failed ({kind}), using keyword fallback: {reason}",
              file=sys.stderr)
        return _fallback(headlines, macro_headlines, reason=reason, kind=kind)


def _sanitize(result, headlines):
    """Never let a hallucinated ticker or headline through.

    Anti-fabrication is the one rule CLAUDE.md calls the worst possible failure, so it is
    enforced in code rather than trusted: tickers we did not ask about are dropped, and cited
    headlines must match a title we actually scraped.
    """
    if not isinstance(result, dict):
        raise ValueError("news intel result was not a JSON object")
    known_titles = {}
    for t, items in headlines.items():
        for n in items:
            key = (n.get("title") or "").strip().lower()[:60]
            if key:
                known_titles[key] = n
    clean = {}
    for ticker, info in (result.get("tickers") or {}).items():
        tk = str(ticker).upper()
        if tk not in headlines or not isinstance(info, dict):
            continue
        cited = []
        for h in (info.get("headlines_used") or [])[:5]:
            key = (h.get("title") or "").strip().lower()[:60]
            if key in known_titles:
                cited.append(known_titles[key])   # use OUR record, not the model's echo
        try:
            sentiment = max(-100, min(100, int(info.get("sentiment") or 0)))
        except (TypeError, ValueError):
            sentiment = 0
        mat = str(info.get("materiality") or "low").lower()
        # Crowding is measured, not opined: take it from the best headline we actually scraped
        # for this name rather than from the model's impression of how widely covered it is.
        pool = cited or headlines.get(tk) or []
        best = max(pool, key=lambda n: n.get("novelty", 0), default={})
        edge = str(info.get("edge") or "").strip()
        clean[tk] = {
            "sentiment": sentiment,
            "materiality": mat if mat in ("high", "medium", "low") else "low",
            "catalyst": str(info.get("catalyst") or "none").lower(),
            "summary": str(info.get("summary") or "").strip(),
            "is_fresh": bool(info.get("is_fresh", True)),
            "crowding": best.get("crowding") or "mixed",
            "novelty": best.get("novelty"),
            "source_tier": best.get("tier_name"),
            "thin_coverage": bool(best.get("low_quality")),
            "edge": edge or None,
            "headlines_used": cited,
        }
    result["tickers"] = clean
    try:
        result["macro_sentiment"] = max(-100, min(100, int(result.get("macro_sentiment") or 0)))
    except (TypeError, ValueError):
        result["macro_sentiment"] = 0
    result["macro_read"] = str(result.get("macro_read") or "").strip()
    result["top_stories"] = [s for s in (result.get("top_stories") or [])[:6] if isinstance(s, dict)]
    return result


# ── Deterministic fallback ──────────────────────────────────────────────────────

def _score_title(title):
    low = title.lower()
    score = sum(6 for w in _BULL_WORDS if w in low) - sum(6 for w in _BEAR_WORDS if w in low)
    return max(-100, min(100, score))


def _catalyst_of(title):
    low = title.lower()
    for kind, words in _CATALYST_WORDS.items():
        if any(w in low for w in words):
            return kind
    return "none"


def _fallback(headlines, macro_headlines, reason=None, kind=None):
    """Keyword sentiment when the subscription is unavailable — clearly flagged as degraded."""
    tickers = {}
    for t, items in headlines.items():
        if not items:
            continue
        scores = [_score_title(n.get("title", "")) for n in items]
        avg = round(sum(scores) / len(scores))
        strongest = max(items, key=lambda n: abs(_score_title(n.get("title", ""))))
        best = max(items, key=lambda n: n.get("novelty", 0))
        tickers[t] = {
            "sentiment": avg,
            "materiality": "medium" if abs(avg) >= 20 else "low",
            "catalyst": _catalyst_of(strongest.get("title", "")),
            "summary": f"keyword scan only (no AI read): {strongest.get('title','')[:120]}",
            "is_fresh": True,
            "crowding": best.get("crowding") or "mixed",
            "novelty": best.get("novelty"),
            "source_tier": best.get("tier_name"),
            "thin_coverage": bool(best.get("low_quality")),
            "edge": None,
            "headlines_used": items[:3],
        }
    macro_scores = [_score_title(n.get("title", "")) for n in macro_headlines] or [0]
    return {
        "macro_read": "AI news read unavailable this cycle — keyword sentiment only."
                      + (f" Reason: {reason}." if reason else ""),
        "degraded_reason": reason, "degraded_kind": kind,
        "macro_sentiment": round(sum(macro_scores) / len(macro_scores)),
        "tickers": tickers,
        "top_stories": [{"title": n.get("title"), "source": n.get("source"),
                         "link": n.get("link"), "why_it_matters": "keyword-flagged"}
                        for n in macro_headlines[:4]],
        "model": "keyword-fallback",
        "degraded": True,
    }


# ── Escalation ──────────────────────────────────────────────────────────────────

def escalation_reasons(intel):
    """Which news findings justify spending an Opus deep run this cycle.

    `high` materiality escalates on its own — a saturated FOMC decision moves the tape whether or
    not it is a scoop. Everything below that must not already be saturated: loud tone, or merely
    notable news, on a story ten outlets have run is the definition of news that is in the price,
    and paying for a deep run on it is how the budget gets spent on nothing.

    That distinction is what lets `medium` sit in NEWS_ESCALATE_MATERIALITY at all. Widening the
    threshold to hit a ~1-trade-a-day pace only buys real looks if the extra volume is
    under-covered; without this guard it would just buy deep runs on yesterday's headlines.
    """
    reasons = []
    for t, info in (intel.get("tickers") or {}).items():
        mat = info.get("materiality")
        sent = info.get("sentiment") or 0
        crowd = info.get("crowding") or "mixed"
        tag = f", {crowd}" + (f" via {info['source_tier']}" if info.get("source_tier") else "")
        mat_ok = mat in config.NEWS_ESCALATE_MATERIALITY and (mat == "high" or crowd != "saturated")
        if mat_ok and info.get("is_fresh"):
            reasons.append(f"{t}: {mat}-materiality {info.get('catalyst')} news "
                           f"(sentiment {sent:+d}{tag}) — {info.get('summary','')[:90]}")
        elif (abs(sent) >= config.NEWS_ESCALATE_ABS_SENTIMENT and info.get("is_fresh")
              and crowd != "saturated"):
            reasons.append(f"{t}: strong news sentiment {sent:+d} ({info.get('catalyst')}{tag})")
    if abs(intel.get("macro_sentiment") or 0) >= 70:
        reasons.append(f"macro news sentiment {intel['macro_sentiment']:+d}")
    return reasons


def cited_headlines(intel):
    """Every headline this pass actually reported on — per ticker plus the macro picks.

    The input to `news_quality.mark_used`. Deliberately not the headlines we *fed* the model: a
    story it was shown and skipped was never told to Lind, and suppressing it next cycle would bury
    it before he ever read it.
    """
    out = []
    for info in (intel.get("tickers") or {}).values():
        out += [h for h in (info.get("headlines_used") or []) if isinstance(h, dict)]
    out += [s for s in (intel.get("top_stories") or []) if isinstance(s, dict)]
    return out


def news_for_ticker(intel, ticker):
    """The headlines Haiku actually used for one name — for the email/journal citation."""
    info = (intel.get("tickers") or {}).get(ticker) or {}
    return info.get("headlines_used") or []


def run(tickers, calendar=None, use_claude=True, venue="stock"):
    """Convenience: scrape + analyze in one call. Returns the intel dict.

    `venue` only picks the macro sweep's queries. The per-ticker scrape needs no such flag: it
    already dispatches on the symbol, so a mixed list would be handled correctly either way.
    """
    tickers = list(dict.fromkeys(tickers))[:config.NEWS_TICKERS_PER_CYCLE]
    # One store for the whole cycle: a macro story that also ran under a ticker must count as
    # two outlets on the same story, not as two unrelated first sightings.
    store = news_quality.load_store()
    stats = {}
    headlines = gather_headlines(tickers, store=store, stats=stats)
    macro = gather_macro_headlines(store=store, venue=venue)
    intel = analyze(headlines, macro, calendar, use_claude=use_claude)
    # Save after the read, not before it: what the pass cited is the half of the store that stops
    # the next cycle repeating this one, and a store written before `analyze` cannot carry it.
    n_marked = news_quality.mark_used(cited_headlines(intel), store=store)
    news_quality.save_store(store)
    stats["cited"] = n_marked
    intel["news_quality"] = stats
    # The scraped, scored, ranked headlines — kept so the caller can attach them to the market
    # snapshots instead of scraping the same names a second time. Superset of headlines_used:
    # these are everything that survived the quality gate, not only what Haiku chose to cite.
    intel["headlines"] = headlines
    return intel
