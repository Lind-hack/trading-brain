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

from . import collect, config, deep

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

def gather_headlines(tickers, per_ticker=None, max_workers=6):
    """Scrape fresh headlines per ticker. Returns {ticker: [{title, source, published, link}]}."""
    per_ticker = per_ticker or config.NEWS_HEADLINES_PER_TICKER
    out = {}

    def _job(t):
        items = collect.finnhub_news(t, days=3, limit=per_ticker)
        if len(items) < per_ticker:
            items += collect.google_news(f"{t} stock", limit=per_ticker - len(items))
        # de-dupe on normalised title
        seen, uniq = set(), []
        for n in items:
            key = (n.get("title") or "").strip().lower()[:90]
            if key and key not in seen:
                seen.add(key)
                uniq.append(n)
        return t, uniq[:per_ticker]

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        for t, items in ex.map(_job, tickers):
            if items:
                out[t] = items
    return out


def gather_macro_headlines(limit=8):
    """Market-wide headlines: what is moving the whole tape right now."""
    queries = ["stock market today", "Federal Reserve interest rates", "S&P 500 outlook"]
    items, seen = [], set()
    for q in queries:
        for n in collect.google_news(q, limit=limit):
            key = (n.get("title") or "").strip().lower()[:90]
            if key and key not in seen:
                seen.add(key)
                items.append(n)
    return items[:limit]


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
      "headlines_used": [{"title": "...", "source": "...", "link": "..."}]
    }
  },
  "top_stories": [{"title": "...", "source": "...", "link": "...", "why_it_matters": "..."}]
}"""

_INSTRUCTIONS = """You are the Market Brain's news analyst (tier 1 of 2). You read headlines and
classify them. You do NOT propose trades, prices, entries, or stops — a separate model does that.

Rules:
- Judge MATERIALITY honestly. "high" means this headline can move the stock today; recycled
  summaries, listicles, and "3 stocks to watch" filler are "low". Most names are "low".
- SENTIMENT is about the news content only, not the chart.
- is_fresh = false if the headlines look like restatements of old news.
- Use ONLY the headlines given to you. Never invent a headline, a source, or a link. If a
  ticker's headlines say nothing meaningful, give it materiality "low" and catalyst "none".
- Every headline you cite in headlines_used must appear verbatim in the input.
- Include only tickers you were given. Skip a ticker entirely rather than guessing about it.

Respond with ONE JSON object and nothing else — no prose, no code fence."""


def _build_prompt(headlines, macro_headlines, calendar):
    payload = {
        "macro_headlines": macro_headlines,
        "ticker_headlines": headlines,
        "calendar_imminent": (calendar or {}).get("imminent", [])[:8],
        "calendar_speeches": (calendar or {}).get("speeches", [])[:6],
    }
    return (
        f"{_INSTRUCTIONS}\n\nSchema:\n{_SCHEMA}\n\nHEADLINES:\n```json\n"
        + json.dumps(payload, indent=2, default=str)
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
        print(f"[warn] news intel failed, using keyword fallback: {e}", file=sys.stderr)
        return _fallback(headlines, macro_headlines)


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
        clean[tk] = {
            "sentiment": sentiment,
            "materiality": mat if mat in ("high", "medium", "low") else "low",
            "catalyst": str(info.get("catalyst") or "none").lower(),
            "summary": str(info.get("summary") or "").strip(),
            "is_fresh": bool(info.get("is_fresh", True)),
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


def _fallback(headlines, macro_headlines):
    """Keyword sentiment when the subscription is unavailable — clearly flagged as degraded."""
    tickers = {}
    for t, items in headlines.items():
        if not items:
            continue
        scores = [_score_title(n.get("title", "")) for n in items]
        avg = round(sum(scores) / len(scores))
        strongest = max(items, key=lambda n: abs(_score_title(n.get("title", ""))))
        tickers[t] = {
            "sentiment": avg,
            "materiality": "medium" if abs(avg) >= 20 else "low",
            "catalyst": _catalyst_of(strongest.get("title", "")),
            "summary": f"keyword scan only (no AI read): {strongest.get('title','')[:120]}",
            "is_fresh": True,
            "headlines_used": items[:3],
        }
    macro_scores = [_score_title(n.get("title", "")) for n in macro_headlines] or [0]
    return {
        "macro_read": "AI news read unavailable this cycle — keyword sentiment only.",
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
    """Which news findings justify spending an Opus deep run this cycle."""
    reasons = []
    for t, info in (intel.get("tickers") or {}).items():
        mat = info.get("materiality")
        sent = info.get("sentiment") or 0
        if mat in config.NEWS_ESCALATE_MATERIALITY and info.get("is_fresh"):
            reasons.append(f"{t}: {mat}-materiality {info.get('catalyst')} news "
                           f"(sentiment {sent:+d}) — {info.get('summary','')[:90]}")
        elif abs(sent) >= config.NEWS_ESCALATE_ABS_SENTIMENT and info.get("is_fresh"):
            reasons.append(f"{t}: strong news sentiment {sent:+d} ({info.get('catalyst')})")
    if abs(intel.get("macro_sentiment") or 0) >= 70:
        reasons.append(f"macro news sentiment {intel['macro_sentiment']:+d}")
    return reasons


def news_for_ticker(intel, ticker):
    """The headlines Haiku actually used for one name — for the email/journal citation."""
    info = (intel.get("tickers") or {}).get(ticker) or {}
    return info.get("headlines_used") or []


def run(tickers, calendar=None, use_claude=True):
    """Convenience: scrape + analyze in one call. Returns the intel dict."""
    tickers = list(dict.fromkeys(tickers))[:config.NEWS_TICKERS_PER_CYCLE]
    headlines = gather_headlines(tickers)
    macro = gather_macro_headlines()
    return analyze(headlines, macro, calendar, use_claude=use_claude)
