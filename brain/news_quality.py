"""Headline quality, saturation and novelty scoring — the filter in front of Haiku.

Lind's ask: *"news that not a lot of people have heard of and are not that saturated."* The old
`gather_headlines()` did the opposite of that by accident. It de-duped on the normalised title
within a single call and threw the duplicates away, which destroyed the one signal that measures
saturation — **how many outlets carried the same story**. Twelve outlets running the same wire
copy collapsed into one headline that looked exactly like a scoop.

So duplicates are now counted instead of discarded, and three things get scored:

  1. **Source tier** — a regulatory filing or a wire beats a syndicated aggregator, which beats
     a content farm ("3 Stocks To Buy Before They Soar").
  2. **Saturation** — how many distinct outlets have carried this story, across every cycle, not
     just this one. That is what "everybody has heard it" actually means.
  3. **Age** — measured from the article's own publish time when it has one, so a three-day-old
     story is stale on the very first cycle we see it, not on the third.

The store lives in `brain-memory/SEEN-HEADLINES.json` (git-as-memory, same as the rest), so
"first seen" survives the process and a story recycled at 09:33 and again at 14:03 is recognised
as recycled.

One deliberate safety valve: filtering can never take a ticker to zero headlines. A thin-news
name whose only coverage is mediocre keeps its best couple of items flagged `low_quality`, because
"no news" and "only bad news outlets" are different facts and the model must not confuse them.
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime

from . import config

SEEN_PATH = config.MEMORY_DIR / "SEEN-HEADLINES.json"
STORE_TTL_DAYS = 14
STORE_MAX_STORIES = 4000


# ── Source tiering ──────────────────────────────────────────────────────────────
# Tier 0: the primary document itself. Tier 1: wires and outlets that break news.
# Tier 2: mainstream reporting (also the default for anything unrecognised — an unknown
# outlet is treated as ordinary, not as junk, so a small trade publication is not punished).
# Tier 3: aggregators, syndicators and content farms that rewrite tier 0-2 hours later.

_TIER0 = ("sec edgar", "sec.gov", "edgar", "company filing")
_TIER1 = (
    "reuters", "bloomberg", "dow jones", "wall street journal", "wsj", "financial times",
    "business wire", "businesswire", "pr newswire", "prnewswire", "globe newswire",
    "globenewswire", "the information", "nikkei", "barron", "associated press", "ap news",
    "digitimes", "axios", "semafor", "politico", "the economist",
)
_TIER2 = (
    "cnbc", "yahoo finance", "marketwatch", "investor's business daily", "forbes", "fortune",
    "the verge", "techcrunch", "ars technica", "tom's hardware", "engadget", "the register",
    "bbc", "the guardian", "new york times", "washington post", "business insider", "cnn",
    "stat news", "endpoints news", "electrek", "protocol", "sherwood", "quartz", "time",
)
_TIER3 = (
    "motley fool", "fool.com", "zacks", "simply wall st", "investorplace", "24/7 wall st",
    "247wallst", "benzinga", "tipranks", "insider monkey", "gurufocus", "marketbeat",
    "investing.com", "finbold", "stocktwits", "thestreet", "the street", "invezz", "barchart",
    "seeking alpha", "kiplinger", "gobankingrates", "aol", "msn", "nasdaq.com", "watcher.guru",
    "coinspeaker", "aiinvest", "ai invest", "quiverquant", "stockstory", "bnn breaking",
    "analyst ratings", "sportskeeda", "financhill", "moneywise", "nypost", "dailymail",
)

TIER_NAMES = {0: "primary", 1: "wire/first-hand", 2: "mainstream", 3: "aggregator"}
_TIER_PENALTY = {0: 0, 1: 5, 2: 16, 3: 38}


def source_tier(source):
    """0 = the filing itself, 3 = a content farm. Unknown outlets default to 2."""
    s = (source or "").strip().lower()
    if not s:
        return 2
    for tier, names in ((0, _TIER0), (1, _TIER1), (3, _TIER3), (2, _TIER2)):
        if any(n in s for n in names):
            return tier
    return 2


# ── Title heuristics ────────────────────────────────────────────────────────────
# These catch the format rather than the outlet: a listicle is a listicle whoever ran it.

_FARM_TITLE = (
    (r"\b\d+\s+(?:top\s+|best\s+|cheap\s+|great\s+|no-brainer\s+)?(?:stocks?|reasons|things|ways|charts)\b",
     "listicle"),
    (r"\bstocks?\s+to\s+(?:buy|watch|consider|own|avoid)\b", "listicle"),
    (r"\bshould\s+you\s+(?:buy|sell|own|invest)\b", "advice-bait"),
    (r"\bbetter\s+buy\b", "advice-bait"),
    (r"\bis\s+it\s+too\s+late\s+to\b", "advice-bait"),
    (r"\bmillionaire\b", "hype"),
    (r"\bprediction\b", "hype"),
    (r"\bwhy\s+.{0,40}\b(?:stock|shares)\b.{0,20}\b(?:jumped|soared|plunged|sank|fell|rose|dropped|popped|slipped)\b.{0,20}\btoday\b",
     "price-recap"),
    (r"\bhere'?s\s+(?:why|what|how)\b.*\b(?:could|might|may|will)\b", "speculation-bait"),
    (r"\bmy\s+top\b", "opinion-bait"),
    (r"\bmotley\s+fool\b", "farm"),
    (r"\b(?:everything|what)\s+you\s+need\s+to\s+know\b", "explainer-filler"),
)
_FARM_RE = tuple((re.compile(p, re.I), tag) for p, tag in _FARM_TITLE)
_FARM_PENALTY = 32


def farm_reason(title):
    """The blocklist tag for a title written to farm clicks rather than report news."""
    t = title or ""
    for rx, tag in _FARM_RE:
        if rx.search(t):
            return tag
    return None


# ── Story identity ──────────────────────────────────────────────────────────────

_STOP = {
    "the", "a", "an", "of", "to", "in", "on", "for", "and", "or", "as", "at", "by", "is", "are",
    "was", "were", "with", "from", "after", "amid", "its", "it", "that", "this", "says", "said",
    "new", "stock", "stocks", "shares", "share", "inc", "corp", "corporation", "company", "com",
    "update", "report", "reports", "reported", "million", "billion", "may", "will", "has", "have",
}


MERGE_MIN_COMMON = 3      # below this, an overlap ratio is noise
MERGE_OVERLAP = 0.6       # |A∩B| / min(|A|,|B|) — tolerates a long rewrite of a short wire item


def _stem(word):
    """Crude suffix stripping: "raises"/"raised" and "beats"/"beat" are the same event."""
    if len(word) > 4 and word.endswith("ing"):
        return word[:-3]
    if len(word) > 4 and word.endswith("ed"):
        return word[:-2]
    if len(word) > 3 and word.endswith("es"):
        return word[:-2]
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def tokens(title):
    """The content words of a headline, stemmed and de-duplicated, longest first."""
    raw = [_stem(t) for t in re.findall(r"[a-z0-9]+", (title or "").lower())
           if len(t) > 2 and t not in _STOP]
    raw = [t for t in raw if t not in _STOP]
    return sorted(dict.fromkeys(raw), key=lambda w: (-len(w), w))[:8]


def signature(title):
    """A wording-tolerant fingerprint, so the same story from six outlets collapses to one key.

    Exact-match on the sorted content stems handles the common case (a desk retitling the same
    wire item). Genuine rewrites — "Nvidia Beats Q2 Estimates On Data Center Demand" against
    "Nvidia earnings beat as data center revenue surges" — share most of their content words but
    not all, so `observe()` additionally merges near-neighbours by overlap.
    """
    toks = tokens(title)
    return " ".join(sorted(toks[:6])) if toks else ""


def _index_stories(stories):
    """token -> [signature]. Rebuilt per call; the store is small and this keeps it stateless."""
    idx = {}
    for sig, rec in stories.items():
        for tok in rec.get("tokens") or sig.split():
            idx.setdefault(tok, []).append(sig)
    return idx


def _find_similar(toks, stories, index):
    """The existing story this headline is a rewrite of, or None."""
    if len(toks) < MERGE_MIN_COMMON:
        return None
    tset = set(toks)
    counts = {}
    for tok in tset:
        for sig in index.get(tok, ()):
            counts[sig] = counts.get(sig, 0) + 1
    best, best_score = None, 0.0
    for sig, common in counts.items():
        if common < MERGE_MIN_COMMON:
            continue
        other = set((stories[sig].get("tokens") or sig.split()))
        score = common / max(1, min(len(tset), len(other)))
        if score >= MERGE_OVERLAP and score > best_score:
            best, best_score = sig, score
    return best


def _parse_published(raw):
    """Google gives RFC-822, Finnhub gives ISO, SEC gives a date. Accept all three, or None."""
    if not raw:
        return None
    raw = str(raw).strip()
    try:
        dt = parsedate_to_datetime(raw)
        if dt is not None:
            return dt if dt.tzinfo else dt.replace(tzinfo=config.UTC)
    except (TypeError, ValueError, IndexError):
        pass
    for fmt in (None, "%Y-%m-%d"):
        try:
            dt = datetime.fromisoformat(raw) if fmt is None else datetime.strptime(raw, fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=config.UTC)
        except ValueError:
            continue
    return None


# ── The seen-store ──────────────────────────────────────────────────────────────

def load_store():
    try:
        with open(SEEN_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and isinstance(data.get("stories"), dict):
            return data
    except FileNotFoundError:
        pass
    except Exception as e:  # pragma: no cover - corrupt file
        print(f"[warn] seen-headline store unreadable, starting fresh: {e}", file=sys.stderr)
    return {"version": 1, "stories": {}}


def save_store(store, now=None):
    """Prune, then write atomically — a half-written store would poison every later cycle."""
    prune(store, now=now)
    try:
        SEEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = SEEN_PATH.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(store, fh, indent=1, sort_keys=True)
        os.replace(tmp, SEEN_PATH)
    except Exception as e:  # pragma: no cover - disk
        print(f"[warn] could not save seen-headline store: {e}", file=sys.stderr)


def prune(store, days=STORE_TTL_DAYS, now=None):
    now = now or datetime.now(config.UTC)
    cutoff = (now - timedelta(days=days)).isoformat()
    stories = store.get("stories") or {}
    for sig in [s for s, v in stories.items() if (v.get("last_seen") or "") < cutoff]:
        stories.pop(sig, None)
    if len(stories) > STORE_MAX_STORIES:
        keep = sorted(stories.items(), key=lambda kv: kv[1].get("last_seen") or "",
                      reverse=True)[:STORE_MAX_STORIES]
        stories = dict(keep)
    store["stories"] = stories
    return store


# ── Scoring ─────────────────────────────────────────────────────────────────────

def _age_penalty(age_min):
    if age_min is None:
        return 8          # undated: mildly suspect, most real reporting carries a timestamp
    if age_min <= 90:
        return 0
    if age_min <= 360:
        return 12
    if age_min <= 1440:
        return 26
    return 42


def crowding_of(saturation):
    if saturation >= 60:
        return "saturated"
    if saturation >= 25:
        return "mixed"
    return "under-covered"


def observe(items, ticker=None, store=None, now=None):
    """Register each headline in the store and annotate it with quality metadata.

    Returns the same list of dicts, each gaining: tier, tier_name, outlets, saturation,
    first_seen, age_min, novelty, crowding, farm_flag. Mutates `store` — the caller saves it
    once per cycle. Not thread-safe by design: scraping is parallel, scoring is not.
    """
    store = load_store() if store is None else store
    now = now or datetime.now(config.UTC)
    stories = store.setdefault("stories", {})
    index = _index_stories(stories)
    out = []
    for n in items or []:
        title = (n.get("title") or "").strip()
        if not title:
            continue
        toks = tokens(title)
        sig = signature(title) or title.lower()[:80]
        if sig not in stories:
            sig = _find_similar(toks, stories, index) or sig
        src = (n.get("source") or "").strip() or "unknown"
        fresh_key = sig not in stories
        rec = stories.setdefault(sig, {"first_seen": None, "last_seen": None, "sources": {},
                                       "title": title, "tickers": [], "tokens": toks})
        rec.setdefault("tokens", toks)
        if fresh_key:
            for tok in toks:
                index.setdefault(tok, []).append(sig)
        published = _parse_published(n.get("published"))
        # The story's own age wins over ours: a wire piece filed yesterday is a day old the
        # first time we see it, and pretending otherwise would make every restart look fresh.
        candidates = [c for c in (rec.get("first_seen"),
                                  published.isoformat() if published else None,
                                  now.isoformat()) if c]
        rec["first_seen"] = min(candidates)
        rec["last_seen"] = now.isoformat()
        rec["sources"].setdefault(src.lower(), now.isoformat())
        if ticker and ticker not in rec["tickers"]:
            rec["tickers"].append(ticker)

        first_dt = _parse_published(rec["first_seen"]) or now
        age_min = max(0.0, (now - first_dt).total_seconds() / 60.0)
        outlets = len(rec["sources"])
        saturation = min(100, int(round((outlets - 1) * 26)))
        tier = source_tier(src)
        farm = farm_reason(title)
        novelty = 100
        novelty -= _TIER_PENALTY[tier]
        novelty -= saturation * 0.45
        novelty -= _age_penalty(age_min)
        if farm:
            novelty -= _FARM_PENALTY
        item = dict(n)
        item.update({
            "tier": tier,
            "tier_name": TIER_NAMES[tier],
            "outlets": outlets,
            "saturation": saturation,
            "crowding": crowding_of(saturation),
            "first_seen": rec["first_seen"],
            "age_min": int(age_min),
            "novelty": max(0, min(100, int(round(novelty)))),
            "farm_flag": farm,
        })
        out.append(item)
    return out


def rank(items, limit=None, min_novelty=None, keep_min=2):
    """Best-first, farm/stale items removed — but never down to nothing.

    Returns (kept, dropped). If the filter would empty a ticker, the top `keep_min` come back
    tagged `low_quality`, because a name with only recycled coverage is a real finding and
    silently reporting it as "no news" would be a lie the model then reasons over.
    """
    min_novelty = config.NEWS_MIN_NOVELTY if min_novelty is None else min_novelty
    ranked = sorted(items or [], key=lambda n: (-n.get("novelty", 0), n.get("age_min") or 0))
    kept, dropped = [], []
    for n in ranked:
        good = not n.get("farm_flag") and n.get("novelty", 0) >= min_novelty
        (kept if good else dropped).append(n)
    if not kept and ranked:
        kept = [dict(n, low_quality=True) for n in ranked[:keep_min]]
        dropped = ranked[len(kept):]
    if limit:
        overflow = kept[limit:]
        kept = kept[:limit]
        dropped = dropped + overflow
    return kept, dropped


def summarize(kept, dropped):
    """One-line cycle stats for the log — how much of the feed was noise this cycle."""
    total = len(kept) + len(dropped)
    reasons = {}
    for n in dropped:
        key = n.get("farm_flag") or ("stale/saturated" if n.get("novelty", 0) < 40 else "overflow")
        reasons[key] = reasons.get(key, 0) + 1
    fresh = sum(1 for n in kept if n.get("crowding") == "under-covered")
    return {"seen": total, "kept": len(kept), "dropped": len(dropped),
            "under_covered": fresh, "drop_reasons": reasons}
