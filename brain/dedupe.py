"""Signal de-duplication — stop re-sending the same idea every thirty minutes.

Lind's complaint was specific: "i kept getting signals in my email to take the same trade over
and over again." That is not a model failure, it is a structural one. The screener fires on a
*condition* — a 20-day breakout, RSI above 72, a gap — and a condition persists. Thirty minutes
later the breakout is still a breakout, so the cycle escalates again, Opus looks at the same
chart and quite correctly reaches the same conclusion, and the harness emails it again. Every
individual step is right and the aggregate is spam.

Nothing upstream can fix this, because no upstream stage remembers anything. `SIGNALS-LOG.jsonl`
does, so the fix lives here: before a signal reaches the email or the portfolio gates, check
whether the brain already said this recently, and if it did, say nothing.

Three things re-open a suppressed idea, because each means the thesis genuinely changed rather
than merely persisted:

  * the cooldown expired — per trade type, since "still the same idea" means hours for a SCALP
    and weeks for a LONG_TERM thesis;
  * the entry moved more than `SIGNAL_ENTRY_DRIFT_PCT` — a breakout re-tested from a different
    price is a different trade with a different risk/reward;
  * conviction moved more than `SIGNAL_CONFIDENCE_JUMP` in either direction — the analyst learned
    something, and a 58 that becomes an 80 is news even when the ticker and direction are not.

A direction flip is never suppressed at all: LONG yesterday and SHORT today is the single most
important thing the brain can tell you.

There is a fourth, added when the pace target went in: an idea that is *actually being executed*
this cycle. Suppression is about not repeating a recommendation; it was never meant to stop a
trade. If the earlier call was blocked by the gates and never became a position, the repeat is
the first time Lind hears the trade was taken — see apply().

Suppressed signals are not discarded. They are written to the ledger with outcome `duplicate`,
so the Friday recap can measure how often the analyst repeats itself — which is itself a
signal about the screener's thresholds.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from . import config, memory

# Never let an unknown trade type inherit the longest cooldown by accident — a mislabelled
# signal should err toward being shown, not silently swallowed for five days.
_DEFAULT_TYPE = "SHORT_TERM"


def _norm_type(trade_type):
    t = (trade_type or "").strip().upper().replace("-", "_").replace(" ", "_")
    return t if t in config.SIGNAL_COOLDOWN_H else _DEFAULT_TYPE


def _norm_dir(direction):
    return (direction or "").strip().upper() or None


def _cooldown_h(trade_type):
    return config.SIGNAL_COOLDOWN_H[_norm_type(trade_type)]


def _parse_ts(ts):
    try:
        return datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None       # NaN is not a price


def _drift_pct(new_entry, old_entry):
    a, b = _num(new_entry), _num(old_entry)
    if a is None or b is None or b == 0:
        return None
    return abs(a - b) / abs(b) * 100.0


def _history_key(rec):
    return ((rec.get("ticker") or "").upper(), _norm_dir(rec.get("direction")))


def _prior_for(signal, history):
    """The most recent live publication of this exact ticker+direction, or None.

    Records already marked `duplicate` are skipped: they were suppressed, so Lind never saw
    them, so they cannot be the thing that makes a new one redundant. Without this the very
    first suppression would keep renewing its own cooldown and the idea would never resurface.
    """
    key = ((signal.get("ticker") or "").upper(), _norm_dir(signal.get("direction")))
    best, best_ts = None, None
    for rec in history or []:
        if rec.get("outcome") == "duplicate" or _history_key(rec) != key:
            continue
        ts = _parse_ts(rec.get("ts"))
        if ts and (best_ts is None or ts > best_ts):
            best, best_ts = rec, ts
    return best


def classify(signal, history, now=None):
    """Decide whether `signal` is worth publishing. Returns (status, reason).

    status is "new" (publish) or "repeat" (suppress). The reason is human-readable and ends up
    in the ledger, so a suppression is always explainable after the fact.
    """
    now = now or datetime.now(config.UTC)
    ticker = (signal.get("ticker") or "").upper()
    if not ticker:
        return "new", "no ticker to match against"

    prior = _prior_for(signal, history)
    if not prior:
        return "new", "not published before"

    prior_ts = _parse_ts(prior.get("ts"))
    if not prior_ts:
        return "new", "prior record has no usable timestamp"
    if prior_ts.tzinfo is None:
        prior_ts = prior_ts.replace(tzinfo=config.UTC)

    cooldown = _cooldown_h(signal.get("trade_type") or prior.get("trade_type"))
    age_h = (now - prior_ts).total_seconds() / 3600.0
    if age_h >= cooldown:
        return "new", f"last published {age_h:.1f}h ago, past the {cooldown:g}h cooldown"

    drift = _drift_pct(signal.get("entry"), prior.get("entry"))
    if drift is not None and drift > config.SIGNAL_ENTRY_DRIFT_PCT:
        return "new", (f"entry moved {drift:.1f}% from the last call "
                       f"({prior.get('entry')} to {signal.get('entry')})")

    c_new, c_old = _num(signal.get("confidence")), _num(prior.get("confidence"))
    if c_new is not None and c_old is not None:
        delta = c_new - c_old
        if abs(delta) >= config.SIGNAL_CONFIDENCE_JUMP:
            return "new", f"confidence moved {delta:+.0f} ({c_old:.0f} to {c_new:.0f})"

    held = " and it is already an open position" if prior.get("outcome") == "executed" else ""
    return "repeat", (f"same {signal.get('direction') or '?'} {ticker} call as {age_h:.1f}h ago"
                      f"{held}; nothing material changed inside the {cooldown:g}h cooldown")


def _lookback_h():
    """Far enough back to cover the longest cooldown, and no further."""
    return max(config.SIGNAL_COOLDOWN_H.values())


def _buy_action_for(analysis, ticker):
    """The BUY/ADD this signal is asking the harness to execute, if there is one."""
    for a in (analysis or {}).get("portfolio_actions") or []:
        if ((a.get("ticker") or "").upper() == ticker
                and (a.get("action") or "").upper() in ("BUY", "ADD")):
            return a
    return None


def apply(analysis, history=None, now=None, can_execute=None):
    """Strip repeats out of `analysis` in place. Returns the list of suppressed records.

    Both the signal and any BUY/ADD action attached to the same ticker are removed: publishing
    the trade while hiding the reasoning would be worse than either. Exits are never touched —
    a SELL or a TRIM is a risk decision and always gets through, however often it repeats.

    One exception, and it is the difference between "stop repeating yourself" and "stop trading".
    A repeat whose earlier emission never became a position is not old news — nothing was ever
    bought. Suppressing it a second and third time is how a good idea that the gates happened to
    block at 10:00 (cap full, pre-market, no cash) never gets taken at all, and the week ends
    under pace for a reason nobody can see. So when `can_execute` says the attached BUY/ADD would
    actually fill right now, the signal is published and the action survives.

    `can_execute` is the harness's own validator, not a guess — a rescue that the gates then
    reject would put exactly the repeat email Lind complained about back in his inbox. Callers
    that pass nothing keep the old strict behaviour.
    """
    signals = (analysis or {}).get("signals") or []
    if not signals:
        return []
    if history is None:
        history = memory.recent_signals(_lookback_h())
    now = now or datetime.now(config.UTC)

    kept, suppressed = [], []
    for sig in signals:
        status, reason = classify(sig, history, now=now)
        if status == "repeat" and can_execute is not None:
            action = _buy_action_for(analysis, (sig.get("ticker") or "").upper())
            if action is not None and can_execute(action):
                status = "new"
                reason = f"repeat, but it is being taken this time — {reason}"
                sig["repeat_but_executing"] = True
                print(f"[dedupe] {sig.get('ticker')} repeats an earlier call but was never "
                      f"filled; publishing because the gates accept it now")
        if status == "new":
            kept.append(sig)
            # A published signal joins the history immediately, so two cycles inside one run
            # (or two signals on one ticker in one response) cannot both count as first.
            history = list(history) + [{
                "ts": now.isoformat(), "ticker": (sig.get("ticker") or "").upper(),
                "direction": sig.get("direction"), "trade_type": sig.get("trade_type"),
                "entry": sig.get("entry"), "confidence": sig.get("confidence"),
                "outcome": "pending",
            }]
        else:
            suppressed.append({**sig, "suppressed_reason": reason})
            print(f"[dedupe] suppressed {sig.get('ticker')} — {reason}")

    if not suppressed:
        return []

    quiet = {(s.get("ticker") or "").upper() for s in suppressed}
    analysis["signals"] = kept
    analysis["portfolio_actions"] = [
        a for a in (analysis.get("portfolio_actions") or [])
        if (a.get("ticker") or "").upper() not in quiet
        or (a.get("action") or "").upper() not in ("BUY", "ADD")
    ]
    # Leave a trace in the analysis itself so the email footer and the dashboard can say
    # "3 ideas held back as repeats" rather than silently showing a shorter list.
    analysis["suppressed_repeats"] = [
        {"ticker": s.get("ticker"), "direction": s.get("direction"),
         "trade_type": s.get("trade_type"), "reason": s.get("suppressed_reason")}
        for s in suppressed
    ]
    return suppressed


def summarize(suppressed):
    """One line for the run log / email footer."""
    if not suppressed:
        return ""
    names = ", ".join(sorted({(s.get("ticker") or "?") for s in suppressed}))
    return f"{len(suppressed)} repeat signal(s) held back: {names}"


def repeats_between(start_iso, end_iso=None):
    """How often the analyst repeated itself in a window — for the Friday recap."""
    return [r for r in memory.signals_between(start_iso, end_iso)
            if r.get("outcome") == "duplicate"]


def stale_cutoff(hours=None):
    """Exposed for tests and for the recap's 'ideas older than this stopped counting' line."""
    return datetime.now(config.UTC) - timedelta(hours=hours or _lookback_h())
