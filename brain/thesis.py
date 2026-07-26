"""The long-horizon thesis board — structural bets measured in quarters, not sessions.

Everything else in this repo runs on a thirty-minute clock. That clock is the right one for a
breakout and completely wrong for the thing Lind actually described:

    "investing in companies that you think will have potential in 1-3 years or more ... for
    example when RAM was more in demand and RAM prices started to surge up 2x-3x then investing
    in RAM companies was a good long term investment since their product and the company overall
    is so wanted"

That is not a chart pattern. It is a claim about *demand for a product* outrunning the supply of
it, held long enough for the income statement to catch up. A cycle that rebuilds its worldview
from scratch every thirty minutes structurally cannot hold such a claim: by construction it has
no yesterday. So the board is persistent state, and it is the only part of the brain that is.

How it works:

  * A thesis is a written argument with a named driver, the tickers that express it, an
    explicit list of what would prove it *wrong*, and milestones to check it against.
  * Evidence accrues to it over time — each new datum stamped and tagged as supporting or
    undermining. Conviction is re-scored from that record, not from how the chart looks today.
  * It is reviewed on exactly two occasions: the Monday week-ahead digest and the Friday recap.
    Never on a cycle. A thesis that changes with the intraday tape was never a thesis.
  * Every LONG_TERM signal must cite a board entry by id. That is what stops the analyst from
    relabelling a momentum pop as an investment case, which is the single failure mode this
    whole file exists to prevent.

The invalidation list is the part that matters most. An unfalsifiable thesis is indistinguishable
from a bias, and a thesis nobody revisits becomes one automatically — hence `THESIS_STALE_DAYS`,
after which an entry with no fresh evidence is flagged for review rather than quietly carried.
"""
from __future__ import annotations

import json
import re
import sys
import uuid
from datetime import date, datetime, timedelta

from . import config
from .jsonio import dumps

BOARD = config.MEMORY_DIR / "THESES.json"

STATUSES = ("active", "watch", "invalidated", "realized")
_STANCES = ("supports", "undermines", "neutral")


# ── persistence ─────────────────────────────────────────────────────────────────

def _empty():
    return {"version": 1, "updated": None, "theses": []}


def load():
    """The board, or an empty one. A corrupt file is never allowed to take down a run."""
    try:
        data = json.loads(BOARD.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _empty()
    except Exception as e:
        print(f"[warn] thesis board unreadable ({e}); starting from empty", file=sys.stderr)
        return _empty()
    if not isinstance(data, dict) or not isinstance(data.get("theses"), list):
        return _empty()
    return data


def save(board):
    try:
        BOARD.parent.mkdir(parents=True, exist_ok=True)
        board["updated"] = datetime.now(config.UTC).isoformat()
        BOARD.write_text(dumps(board, indent=2), encoding="utf-8")
        return True
    except Exception as e:  # pragma: no cover - disk
        print(f"[warn] thesis board save: {e}", file=sys.stderr)
        return False


# ── shaping ─────────────────────────────────────────────────────────────────────

def _slug(theme):
    return re.sub(r"[^a-z0-9]+", "-", (theme or "").lower()).strip("-")[:60]


def _clamp(v, lo=0, hi=100, default=None):
    try:
        return max(lo, min(hi, int(round(float(v)))))
    except (TypeError, ValueError):
        return default


def _tickers(v):
    if isinstance(v, str):
        v = [v]
    out = []
    for t in (v or []):
        t = str(t).strip().upper()
        if t and t not in out:
            out.append(t)
    return out[:8]


def _strlist(v, limit=6, maxlen=300):
    if isinstance(v, str):
        v = [v]
    return [str(x).strip()[:maxlen] for x in (v or []) if str(x).strip()][:limit]


def _today():
    return datetime.now(config.ET).date().isoformat()


def normalize(raw, now_iso=None):
    """Coerce one model-authored thesis into the board's shape. Returns None if unusable."""
    theme = str(raw.get("theme") or raw.get("title") or "").strip()
    if not theme:
        return None
    now_iso = now_iso or _today()
    tickers = _tickers(raw.get("tickers") or raw.get("ticker"))
    status = str(raw.get("status") or "active").strip().lower()
    return {
        "id": str(raw.get("id") or uuid.uuid4().hex[:10]),
        "slug": _slug(theme),
        "theme": theme[:120],
        "driver": str(raw.get("driver") or "").strip()[:400],
        "thesis": str(raw.get("thesis") or raw.get("why") or "").strip()[:1200],
        "what_the_market_misses": str(raw.get("what_the_market_misses") or "").strip()[:600]
                                  or None,
        "tickers": tickers,
        "primary_ticker": (str(raw.get("primary_ticker") or "").strip().upper()
                           or (tickers[0] if tickers else None)),
        "horizon": str(raw.get("horizon")
                       or f"{config.THESIS_HORIZON_YEARS[0]}-{config.THESIS_HORIZON_YEARS[1]} years")[:40],
        "conviction": _clamp(raw.get("conviction"), default=config.THESIS_MIN_CONVICTION),
        "status": status if status in STATUSES else "active",
        "first_seen": str(raw.get("first_seen") or now_iso)[:10],
        "last_reviewed": now_iso,
        "last_evidence": str(raw.get("last_evidence") or now_iso)[:10],
        "invalidation": _strlist(raw.get("invalidation") or raw.get("what_would_kill_it")),
        "milestones": _strlist(raw.get("milestones") or raw.get("what_to_watch")),
        "evidence": [],
        "needs_review": False,
    }


def _evidence(raw, now_iso=None):
    note = str(raw.get("note") or raw.get("headline") or raw.get("title") or "").strip()
    if not note:
        return None
    stance = str(raw.get("stance") or "supports").strip().lower()
    return {
        "date": str(raw.get("date") or now_iso or _today())[:10],
        "note": note[:400],
        "source": str(raw.get("source") or "").strip()[:80] or None,
        "link": str(raw.get("link") or "").strip()[:400] or None,
        "stance": stance if stance in _STANCES else "supports",
    }


# ── reading ─────────────────────────────────────────────────────────────────────

def _by_conviction(t):
    return -(t.get("conviction") or 0)


def active(board=None, include_watch=True):
    board = board or load()
    live = {"active", "watch"} if include_watch else {"active"}
    return sorted([t for t in board["theses"] if t.get("status") in live], key=_by_conviction)


def for_ticker(ticker, board=None):
    """Every live thesis that names this ticker — how a LONG_TERM signal finds its anchor."""
    ticker = (ticker or "").upper()
    return [t for t in active(board) if ticker in (t.get("tickers") or [])]


def by_id(tid, board=None):
    for t in (board or load())["theses"]:
        if t.get("id") == tid:
            return t
    return None


def for_packet(board=None, limit=None, evidence_per=3):
    """The compact board the deep run reasons over.

    Trimmed hard on purpose: the full evidence log of a year-old thesis would crowd out the
    actual market data. The newest few entries carry the trajectory, which is what matters.
    """
    limit = limit or config.THESIS_MAX_ACTIVE
    out = []
    for t in active(board)[:limit]:
        out.append({
            "id": t.get("id"), "theme": t.get("theme"), "driver": t.get("driver"),
            "thesis": t.get("thesis"), "tickers": t.get("tickers"),
            "horizon": t.get("horizon"), "conviction": t.get("conviction"),
            "status": t.get("status"), "first_seen": t.get("first_seen"),
            "age_days": _age_days(t.get("first_seen")),
            "invalidation": t.get("invalidation"), "milestones": t.get("milestones"),
            "needs_review": t.get("needs_review", False),
            "recent_evidence": (t.get("evidence") or [])[-evidence_per:],
        })
    return out


def _age_days(iso):
    try:
        return (date.today() - date.fromisoformat(str(iso)[:10])).days
    except (TypeError, ValueError):
        return None


def summary_line(board=None):
    live = active(board)
    if not live:
        return "No long-term theses on the board yet."
    top = live[0]
    return (f"{len(live)} long-term thesis/theses on the board; highest conviction: "
            f"{top['theme']} ({top['conviction']}/100, "
            f"{', '.join(top.get('tickers') or []) or 'no ticker yet'}).")


# ── writing ─────────────────────────────────────────────────────────────────────

def flag_stale(board, now=None):
    """Mark entries whose evidence has gone quiet. Returns how many were flagged."""
    now = now or datetime.now(config.ET)
    cutoff = (now.date() - timedelta(days=config.THESIS_STALE_DAYS)).isoformat()
    n = 0
    for t in board["theses"]:
        if t.get("status") not in ("active", "watch"):
            continue
        stale = (t.get("last_evidence") or t.get("first_seen") or "") < cutoff
        if stale != bool(t.get("needs_review")):
            t["needs_review"] = stale
        if stale:
            n += 1
    return n


def _demote_overflow(board):
    """Keep at most THESIS_MAX_ACTIVE entries `active`; the rest fall back to `watch`.

    A board of thirty convictions is a board of none. Nothing is deleted — a demoted thesis
    keeps its evidence log and can be promoted again when the evidence turns.
    """
    live = sorted([t for t in board["theses"] if t.get("status") == "active"], key=_by_conviction)
    for t in live[config.THESIS_MAX_ACTIVE:]:
        t["status"] = "watch"
    for t in board["theses"]:
        if t.get("status") == "active" and (t.get("conviction") or 0) < config.THESIS_MIN_CONVICTION:
            t["status"] = "watch"


def _find(board, entry):
    """Match an incoming entry to one already on the board: by id, then by theme slug.

    Slug matching is what lets the model re-state a thesis in its own words each week without
    forking it into a second entry — the evidence log stays continuous.
    """
    tid = str(entry.get("id") or "")
    if tid:
        hit = by_id(tid, board)
        if hit:
            return hit
    slug = _slug(entry.get("theme") or entry.get("title") or "")
    if not slug:
        return None
    for t in board["theses"]:
        if t.get("slug") == slug:
            return t
    return None


def merge(entries, board=None, now=None, save_board=True):
    """Fold a review's thesis output into the board. Returns (board, added, updated, closed).

    Deliberately conservative about deletion: a thesis is never removed, only re-statused. The
    record of a call that turned out wrong is the most valuable thing here — it is the only way
    the Friday review can ever learn that a whole *class* of thesis does not work.
    """
    board = board or load()
    now = now or datetime.now(config.ET)
    now_iso = now.date().isoformat()
    added = updated = closed = 0

    for raw in (entries or []):
        if not isinstance(raw, dict):
            continue
        existing = _find(board, raw)
        if existing is None:
            fresh = normalize(raw, now_iso)
            if not fresh:
                continue
            for ev in (raw.get("evidence") or []):
                e = _evidence(ev if isinstance(ev, dict) else {"note": ev}, now_iso)
                if e:
                    fresh["evidence"].append(e)
                    fresh["last_evidence"] = max(fresh["last_evidence"], e["date"])
            board["theses"].append(fresh)
            added += 1
            continue

        # An update. Overwrite the argument, keep the history.
        for field, coerce in (("driver", lambda v: str(v).strip()[:400]),
                              ("thesis", lambda v: str(v).strip()[:1200]),
                              ("horizon", lambda v: str(v).strip()[:40]),
                              ("what_the_market_misses", lambda v: str(v).strip()[:600])):
            if raw.get(field):
                existing[field] = coerce(raw[field])
        if raw.get("tickers") or raw.get("ticker"):
            existing["tickers"] = _tickers(raw.get("tickers") or raw.get("ticker"))
            if existing["tickers"]:
                existing.setdefault("primary_ticker", existing["tickers"][0])
        conv = _clamp(raw.get("conviction"))
        if conv is not None:
            existing["conviction"] = conv
        for field, key in (("invalidation", "what_would_kill_it"), ("milestones", "what_to_watch")):
            vals = _strlist(raw.get(field) or raw.get(key))
            if vals:
                existing[field] = vals
        status = str(raw.get("status") or "").strip().lower()
        if status in STATUSES and status != existing.get("status"):
            existing["status"] = status
            if status in ("invalidated", "realized"):
                existing["closed_on"] = now_iso
                existing["closed_reason"] = str(raw.get("close_reason")
                                                or raw.get("reason") or "").strip()[:400] or None
                closed += 1
        for ev in (raw.get("evidence") or []):
            e = _evidence(ev if isinstance(ev, dict) else {"note": ev}, now_iso)
            if e and e not in (existing.get("evidence") or []):
                existing.setdefault("evidence", []).append(e)
                existing["last_evidence"] = max(existing.get("last_evidence") or "", e["date"])
        # An evidence log that grows without bound eventually dwarfs the thesis it supports.
        existing["evidence"] = (existing.get("evidence") or [])[-40:]
        existing["last_reviewed"] = now_iso
        updated += 1

    _demote_overflow(board)
    flag_stale(board, now)
    if save_board:
        save(board)
    return board, added, updated, closed


def attach_evidence(ticker, items, board=None, now=None, save_board=True):
    """Hang a cycle's news onto whatever theses name this ticker.

    Called from the live pipeline, which is otherwise forbidden to touch conviction. The
    distinction is deliberate: gathering evidence is a cheap, factual act that should happen
    continuously; *re-scoring* on it is a judgement, and judgements happen twice a week.
    """
    board = board or load()
    now = now or datetime.now(config.ET)
    now_iso = now.date().isoformat()
    hits = for_ticker(ticker, board)
    if not hits:
        return 0
    n = 0
    for t in hits:
        for raw in (items or [])[:3]:
            e = _evidence({**raw, "date": now_iso, "stance": raw.get("stance") or "neutral"},
                          now_iso)
            if not e:
                continue
            if any(x.get("note") == e["note"] for x in (t.get("evidence") or [])):
                continue     # the same headline resurfacing is not new evidence
            t.setdefault("evidence", []).append(e)
            t["evidence"] = t["evidence"][-40:]
            t["last_evidence"] = now_iso
            t["needs_review"] = False
            n += 1
    if n and save_board:
        save(board)
    return n


def annotate_signals(analysis, board=None):
    """Bind every LONG_TERM signal to its board entry, and say so when it has none.

    A long-term call with no thesis behind it is the exact mislabelling the rulebook warns
    about, so it is surfaced rather than silently accepted.
    """
    board = board or load()
    unanchored = []
    for sig in ((analysis or {}).get("signals") or []):
        if (sig.get("trade_type") or "").upper() != "LONG_TERM":
            continue
        t = by_id(sig.get("thesis_id"), board) if sig.get("thesis_id") else None
        if t is None:
            hits = for_ticker(sig.get("ticker"), board)
            t = hits[0] if hits else None
        if t is None:
            sig["thesis"] = None
            unanchored.append(sig.get("ticker"))
            continue
        sig["thesis_id"] = t.get("id")
        sig["thesis"] = {
            "id": t.get("id"), "theme": t.get("theme"), "driver": t.get("driver"),
            "conviction": t.get("conviction"), "horizon": t.get("horizon"),
            "age_days": _age_days(t.get("first_seen")),
            "invalidation": (t.get("invalidation") or [])[:3],
        }
    if unanchored:
        analysis["unanchored_long_term"] = unanchored
    return analysis
