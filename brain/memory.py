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
from .jsonio import dumps as _dumps

EVENT_LOG = config.MEMORY_DIR / "EVENT-LOG.jsonl"
SIGNALS_LOG = config.MEMORY_DIR / "SIGNALS-LOG.jsonl"
STRATEGY = config.MEMORY_DIR / "STRATEGY.md"
BACKLOG = config.MEMORY_DIR / "PIPELINE-BACKLOG.md"
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


def append_pipeline_backlog(items, now_et, stats=None):
    """Append the week's *engineering* requests — deliberately not into STRATEGY.md.

    STRATEGY.md is read back into every deep run, so anything written there becomes a rule the
    analyst tries to trade by. "Add an options-flow source" is not a rule the analyst can apply;
    it is work for whoever maintains the harness. Mixing the two would waste packet budget every
    cycle on instructions no model can act on.
    """
    if not items:
        return False
    try:
        BACKLOG.parent.mkdir(parents=True, exist_ok=True)
        head = ""
        if not BACKLOG.exists():
            head = ("# Pipeline backlog\n\n"
                    "Engineering changes the weekly review asked for, newest at the bottom. Read "
                    "by humans, **not** loaded into any run's packet — unlike STRATEGY.md. Each "
                    "item cites the measurement that motivated it.\n")
        block = [f"\n## Week of {now_et.strftime('%Y-%m-%d')}\n"]
        if stats:
            block.append(f"\n_Week {stats.get('week_return_pct', 0):+.2f}% · "
                         f"{stats.get('n_closed', 0)} closed_\n\n")
        for it in items:
            if isinstance(it, dict):
                effort = it.get("effort")
                block.append(f"- [ ] **{it.get('change')}**"
                             + (f" _({effort})_" if effort else "")
                             + (f"\n      - why: {it.get('why')}\n" if it.get("why") else "\n"))
            else:
                block.append(f"- [ ] {it}\n")
        with open(BACKLOG, "a", encoding="utf-8") as f:
            f.write(head + "".join(block))
        print(f"[memory] appended {len(items)} pipeline item(s) -> {BACKLOG.name}")
        return True
    except Exception as e:  # pragma: no cover - fs
        print(f"[warn] append_pipeline_backlog: {e}", file=sys.stderr)
        return False


# ── logging events ────────────────────────────────────────────────────────────

def _append_to(path, record):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(_dumps(record) + "\n")


def _append(record):
    _append_to(EVENT_LOG, record)


def log_events(market, calendar, spy_price=None, with_calendar=True):
    """Record this cycle's patterns + calendar events with a market snapshot.

    ref_ticker/ref_price anchor the forward-return calc: a ticker's own price for chart
    patterns, SPY for macro/calendar events. Returns the number of events logged.

    `with_calendar=False` logs the chart patterns and skips the macro rows. The crypto cycle
    passes it: CPI and FOMC are one event, and the equity cycle already files them against SPY.
    Filing them a second time against a crypto benchmark would not add a second data point, it
    would add a duplicate — and `analogs_for` counts rows, so every macro analog's sample size
    would quietly double while its statistical weight stayed the same.
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
    cal_events = calendar.get("imminent", []) + calendar.get("trump_soon", []) if with_calendar else []
    for e in cal_events:
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


# ── the signal ledger (every idea, executed or not) ─────────────────────────────

def log_signals(analysis, outcomes, mode, prices=None):
    """Record every signal the model published and what the harness did with it.

    A signal used to leave a trace only if it became a fill. A rejection printed one line to a
    log nobody reads and then ceased to exist, and a signal the model raised without proposing
    an action left nothing at all. That makes two of Lind's questions structurally unanswerable
    — "what did you recommend?" and "what did the gates cost me?" — because the answer was
    never written down.

    Each record carries ref_price and an empty `forward` block, so backfill_returns() prices
    these exactly like chart patterns. After a few weeks that turns the rejections into a real
    counterfactual: the weekly review can compare what the gates blocked against what it would
    have made, instead of assuming every block was correct.

    `outcomes` maps TICKER -> {"action", "ok", "msg"} as returned by the gate.
    """
    signals = (analysis or {}).get("signals") or []
    actions = (analysis or {}).get("portfolio_actions") or []
    if not (signals or actions):
        return 0
    prices = prices or {}
    ts = datetime.now(config.UTC).isoformat()
    seen, n = set(), 0
    for sig in signals:
        ticker = (sig.get("ticker") or "").upper()
        seen.add(ticker)
        res = (outcomes or {}).get(ticker) or {}
        if not res:
            # The model published the idea but proposed no action on it — a recommendation
            # rather than a trade. Worth keeping: these are the ones it talked itself out of.
            outcome, gate_reason = "advisory", "no portfolio action proposed"
        else:
            outcome = "executed" if res.get("ok") else "rejected"
            gate_reason = res.get("msg") or ""
        ref = sig.get("entry") or prices.get(ticker)
        _append_to(SIGNALS_LOG, {
            "id": uuid.uuid4().hex[:12], "ts": ts, "mode": mode, "ticker": ticker,
            "outcome": outcome, "proposed_action": (res.get("action") or "").upper() or None,
            "gate_reason": gate_reason,
            "direction": sig.get("direction"), "trade_type": sig.get("trade_type"),
            "holding_period": sig.get("holding_period"), "confidence": sig.get("confidence"),
            "confidence_rationale": sig.get("confidence_rationale"),
            "entry": sig.get("entry"), "stop": sig.get("stop"),
            "target1": sig.get("target1"), "target2": sig.get("target2"),
            "why": sig.get("why"), "indicators_used": sig.get("indicators_used") or [],
            "news_read": sig.get("news_read"), "historical_analog": sig.get("historical_analog"),
            "ref_ticker": ticker, "ref_price": ref,
            "forward": {h: None for h in _HORIZONS},
        })
        n += 1
    for act in actions:
        ticker = (act.get("ticker") or "").upper()
        if ticker in seen:
            continue        # already logged above, with its full signal attached
        res = (outcomes or {}).get(ticker) or {}
        # A managed action with no signal of its own — a risk trim, an ADD, a HOLD. It still
        # went through the gates, so it still belongs in the ledger.
        _append_to(SIGNALS_LOG, {
            "id": uuid.uuid4().hex[:12], "ts": ts, "mode": mode, "ticker": ticker,
            "outcome": "executed" if res.get("ok") else "rejected",
            "proposed_action": (act.get("action") or "").upper() or None,
            "gate_reason": res.get("msg") or "", "direction": None,
            "trade_type": act.get("trade_type"), "holding_period": None,
            "confidence": act.get("confidence"), "confidence_rationale": None,
            "entry": act.get("entry"), "stop": act.get("stop"),
            "target1": None, "target2": None,
            "why": act.get("reason") or act.get("why"),
            "indicators_used": act.get("indicators_used") or [],
            "news_read": None, "historical_analog": None,
            "ref_ticker": ticker, "ref_price": act.get("entry") or prices.get(ticker),
            "forward": {h: None for h in _HORIZONS},
        })
        n += 1
    return n


def log_suppressed(suppressed, mode, prices=None):
    """Record ideas the de-duplicator held back, so the recap can count the repetition.

    These never reach Lind's inbox, but they are still things the analyst said. Written with
    outcome `duplicate` and the same `forward` block as everything else: if the brain keeps
    suppressing a call that then runs 8%, that is a cooldown that is too long, and the only way
    to find out is to have priced the ones nobody saw.
    """
    if not suppressed:
        return 0
    prices = prices or {}
    ts = datetime.now(config.UTC).isoformat()
    n = 0
    for sig in suppressed:
        ticker = (sig.get("ticker") or "").upper()
        _append_to(SIGNALS_LOG, {
            "id": uuid.uuid4().hex[:12], "ts": ts, "mode": mode, "ticker": ticker,
            "outcome": "duplicate", "proposed_action": None,
            "gate_reason": sig.get("suppressed_reason") or "suppressed as a repeat",
            "direction": sig.get("direction"), "trade_type": sig.get("trade_type"),
            "holding_period": sig.get("holding_period"), "confidence": sig.get("confidence"),
            "confidence_rationale": sig.get("confidence_rationale"),
            "entry": sig.get("entry"), "stop": sig.get("stop"),
            "target1": sig.get("target1"), "target2": sig.get("target2"),
            "why": sig.get("why"), "indicators_used": sig.get("indicators_used") or [],
            "news_read": sig.get("news_read"), "historical_analog": sig.get("historical_analog"),
            "ref_ticker": ticker, "ref_price": sig.get("entry") or prices.get(ticker),
            "forward": {h: None for h in _HORIZONS},
        })
        n += 1
    return n


def signals_between(start_iso, end_iso=None):
    """Every logged signal in a window — the weekly recap's answer to 'what did you recommend?'."""
    end_iso = end_iso or datetime.now(config.UTC).isoformat()
    return [r for r in _read_jsonl(SIGNALS_LOG)
            if r.get("ts") and start_iso <= r["ts"] <= end_iso]


def recent_signals(hours):
    """Signals published in the last `hours`, newest last — the de-duplicator's memory.

    Kept separate from signals_between() because the caller here has no calendar in hand, only
    "how far back does a repeat still count as a repeat", and that is a per-trade-type number.
    """
    # Strictly after the cutoff, not on it. Windows' clock ticks at ~15ms, so a signal logged and
    # then read back inside one tick stamps the same microseconds as the cutoff — with `>=` a
    # zero-hour window remembers the row it was just asked to forget.
    cutoff = (datetime.now(config.UTC) - timedelta(hours=float(hours))).isoformat()
    return [r for r in _read_jsonl(SIGNALS_LOG) if (r.get("ts") or "") > cutoff]


# ── forward-return backfill ─────────────────────────────────────────────────────

def _read_jsonl(path):
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _write_jsonl(path, records):
    tmp = path.with_suffix(".jsonl.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        for r in records:
            f.write(_dumps(r) + "\n")
    tmp.replace(path)


def _read_all():
    return _read_jsonl(EVENT_LOG)


def _write_all(records):
    _write_jsonl(EVENT_LOG, records)


def _backfill_file(path, price_lookup, now):
    records = _read_jsonl(path)
    if not records:
        return 0
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
            if r.setdefault("forward", {}).get(h) is None and elapsed_h >= hours:
                r["forward"][h] = round((cur - ref_p) / ref_p * 100, 2)
                filled += 1
    if filled:
        _write_jsonl(path, records)
    return filled


def backfill_returns(price_lookup):
    """Fill any elapsed-but-empty forward-return buckets using current prices.

    price_lookup: {ticker -> current price}. Approximate (uses the latest price for every
    due bucket) but accumulates a usable analog dataset over weeks. Returns count filled.

    Runs over the signal ledger as well as the event log, which is what makes a rejected
    signal answerable later: the price kept moving after the gate said no, and this records
    where it went.
    """
    now = datetime.now(config.UTC)
    return (_backfill_file(EVENT_LOG, price_lookup, now)
            + _backfill_file(SIGNALS_LOG, price_lookup, now))


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
        # Push only when a remote actually exists. The VPS clone deliberately has none — it is
        # the live copy, synced by file transfer, not by git — so an unconditional push logged a
        # "No configured push destination" warning on every single cycle for no reason.
        try:
            if _git("remote", check=False).stdout.strip():
                _git("pull", "--rebase", check=False)
                push = _git("push", check=False)
                if push.returncode != 0:
                    print(f"[warn] commit_memory push: {push.stderr.strip()[:200]}",
                          file=sys.stderr)
        except Exception as e:
            print(f"[warn] commit_memory push: {e}", file=sys.stderr)
        return True
    except subprocess.CalledProcessError as e:
        print(f"[warn] commit_memory: {e.stderr.strip()[:200]}", file=sys.stderr)
        return False
