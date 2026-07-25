"""Git-as-memory: the brain's long-term store in brain-memory/.

Two jobs:
  1. EVENT-LOG.jsonl — every pattern / calendar / news / speech event, stamped with a
     market snapshot. Forward returns (1h/4h/1d/5d) are backfilled on later cycles, which
     slowly turns the log into a *historical-analog database*: "the last N times this same
     event fired, price did X." That is the news/pattern-recognition memory Lind asked for.
  2. commit_memory() — commits + rebases + pushes brain-memory/ to the private repo so the
     memory survives across VPS runs (the PDF's git-as-memory model). Never force-pushes.
"""
from __future__ import annotations

import json
import subprocess
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from . import config

EVENT_LOG = config.MEMORY_DIR / "EVENT-LOG.jsonl"
STRATEGY = config.MEMORY_DIR / "STRATEGY.md"
_HORIZONS = {"1h": 1, "4h": 4, "1d": 24, "5d": 120}  # hours


# ── strategy lessons (written by the weekly review, read by every deep run) ──────

def read_strategy(max_chars=4000):
    """The accumulated lessons the weekly review has written back.

    Fed into every deep-run packet so the brain actually acts on its own post-mortems
    instead of re-learning the same mistake each week. Tail-truncated: newest lessons are
    appended at the bottom, so the tail is the part worth carrying.
    """
    try:
        if STRATEGY.exists():
            return STRATEGY.read_text(encoding="utf-8")[-max_chars:]
    except Exception as e:  # pragma: no cover - fs
        print(f"[warn] read_strategy: {e}", file=sys.stderr)
    return ""


def append_strategy(changes, now_et, stats=None):
    """Append this week's proposed rule changes so future runs read them."""
    if not changes:
        return False
    try:
        STRATEGY.parent.mkdir(parents=True, exist_ok=True)
        head = ""
        if not STRATEGY.exists():
            head = ("# Strategy lessons\n\n"
                    "Written by the weekly review, read by every deep run. Newest at the "
                    "bottom. These are accuracy corrections derived from closed paper trades — "
                    "not new rules for the harness gates, which live in config.py.\n")
        block = [f"\n## Week of {now_et.strftime('%Y-%m-%d')}\n"]
        if stats:
            block.append(f"\n_Week {stats.get('week_return_pct', 0):+.2f}% · "
                         f"{stats.get('n_closed', 0)} closed · "
                         f"win rate {stats.get('win_rate')}_\n\n")
        block += [f"- {c}\n" for c in changes]
        with open(STRATEGY, "a", encoding="utf-8") as f:
            f.write(head + "".join(block))
        print(f"[memory] appended {len(changes)} strategy lesson(s) -> {STRATEGY.name}")
        return True
    except Exception as e:  # pragma: no cover - fs
        print(f"[warn] append_strategy: {e}", file=sys.stderr)
        return False


# ── logging events ────────────────────────────────────────────────────────────

def _append(record):
    EVENT_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(EVENT_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def log_events(market, calendar, spy_price=None):
    """Record this cycle's patterns + calendar events with a market snapshot.

    ref_ticker/ref_price anchor the forward-return calc: a ticker's own price for chart
    patterns, SPY for macro/calendar events. Returns the number of events logged.
    """
    now = datetime.now(config.UTC)
    ts = now.isoformat()
    n = 0
    for ticker, snap in market.items():
        if ticker in config.MARKET_CONTEXT:
            continue
        price = snap.get("indicators", {}).get("price")
        for p in snap.get("patterns", []):
            _append({
                "id": uuid.uuid4().hex[:12], "ts": ts, "type": "pattern",
                "ticker": ticker, "label": p["name"], "detail": p["detail"],
                "bias": p.get("bias"), "ref_ticker": ticker, "ref_price": price,
                "forward": {h: None for h in _HORIZONS},
            })
            n += 1
    for e in calendar.get("imminent", []) + calendar.get("trump_soon", []):
        _append({
            "id": uuid.uuid4().hex[:12], "ts": ts,
            "type": "speech" if "speak" in e["title"].lower() else "calendar",
            "ticker": None, "label": e["title"], "detail": e.get("impact", ""),
            "bias": None, "actual": e.get("actual"), "forecast": e.get("forecast"),
            "previous": e.get("previous"), "ref_ticker": "SPY", "ref_price": spy_price,
            "forward": {h: None for h in _HORIZONS},
        })
        n += 1
    return n


# ── forward-return backfill ─────────────────────────────────────────────────────

def _read_all():
    if not EVENT_LOG.exists():
        return []
    out = []
    for line in EVENT_LOG.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _write_all(records):
    tmp = EVENT_LOG.with_suffix(".jsonl.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")
    tmp.replace(EVENT_LOG)


def backfill_returns(price_lookup):
    """Fill any elapsed-but-empty forward-return buckets using current prices.

    price_lookup: {ticker -> current price}. Approximate (uses the latest price for every
    due bucket) but accumulates a usable analog dataset over weeks. Returns count filled.
    """
    records = _read_all()
    if not records:
        return 0
    now = datetime.now(config.UTC)
    filled = 0
    for r in records:
        ref_t, ref_p = r.get("ref_ticker"), r.get("ref_price")
        cur = price_lookup.get(ref_t)
        if not ref_p or not cur:
            continue
        try:
            ev_time = datetime.fromisoformat(r["ts"])
        except (ValueError, KeyError):
            continue
        elapsed_h = (now - ev_time).total_seconds() / 3600
        for h, hours in _HORIZONS.items():
            if r["forward"].get(h) is None and elapsed_h >= hours:
                r["forward"][h] = round((cur - ref_p) / ref_p * 100, 2)
                filled += 1
    if filled:
        _write_all(records)
    return filled


# ── historical analogs ──────────────────────────────────────────────────────────

def find_analogs(label, event_type=None, bias=None, limit=8):
    """Return past occurrences of an event (same label) with their forward returns,
    plus an aggregate. Feeds the deep-run packet so Claude can cite precedent.
    """
    records = _read_all()
    matches = []
    for r in reversed(records):  # newest first
        if r.get("label") != label:
            continue
        if event_type and r.get("type") != event_type:
            continue
        if bias and r.get("bias") != bias:
            continue
        fwd = {h: v for h, v in (r.get("forward") or {}).items() if v is not None}
        if fwd:
            matches.append({"ts": r["ts"], "ticker": r.get("ticker"),
                            "detail": r.get("detail"), "forward": fwd})
        if len(matches) >= limit:
            break
    agg = {}
    for h in _HORIZONS:
        vals = [m["forward"][h] for m in matches if h in m["forward"]]
        if vals:
            agg[h] = {"avg": round(sum(vals) / len(vals), 2), "n": len(vals)}
    return {"label": label, "occurrences": matches, "aggregate": agg}


def analogs_for(market, calendar):
    """Build the analog list for everything firing this cycle (dedup by label)."""
    seen, out = set(), []
    for ticker, snap in market.items():
        for p in snap.get("patterns", []):
            key = ("pattern", p["name"], p.get("bias"))
            if key in seen:
                continue
            seen.add(key)
            a = find_analogs(p["name"], event_type="pattern", bias=p.get("bias"))
            if a["aggregate"]:
                out.append(a)
    for e in calendar.get("imminent", []) + calendar.get("trump_soon", []):
        if e["title"] in seen:
            continue
        seen.add(e["title"])
        a = find_analogs(e["title"])
        if a["aggregate"]:
            out.append(a)
    return out


# ── git-as-memory ───────────────────────────────────────────────────────────────

def _git(*args, check=True):
    return subprocess.run(["git", "-C", str(config.REPO_ROOT), *args],
                          capture_output=True, text=True, check=check)


def commit_memory(message):
    """Commit brain-memory/ and push (rebase, never force). Safe no-op off-repo / off-network."""
    try:
        _git("rev-parse", "--git-dir")
    except Exception:
        print("[warn] commit_memory: not a git repo, skipping", file=sys.stderr)
        return False
    try:
        _git("add", "brain-memory")
        status = _git("status", "--porcelain", "brain-memory").stdout.strip()
        if not status:
            return False  # nothing changed
        _git("commit", "-m", message)
        # pull --rebase then push; tolerate no-remote / offline gracefully
        try:
            _git("pull", "--rebase", check=False)
            push = _git("push", check=False)
            if push.returncode != 0:
                print(f"[warn] commit_memory push: {push.stderr.strip()[:200]}", file=sys.stderr)
        except Exception as e:
            print(f"[warn] commit_memory push: {e}", file=sys.stderr)
        return True
    except subprocess.CalledProcessError as e:
        print(f"[warn] commit_memory: {e.stderr.strip()[:200]}", file=sys.stderr)
        return False
