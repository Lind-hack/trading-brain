"""Deterministic 30-minute screener — the free gate before any Claude deep run.

Scores the collected market snapshot against fixed thresholds. If nothing crosses a
threshold the cycle exits QUIET (logs the tick, spends no subscription quota). If
anything fires — or the caller forces it (an anchor run, an imminent calendar event) —
the screener returns a ranked trigger list and the orchestrator escalates to `deep.py`.
"""
from __future__ import annotations

from . import config


def _score_ticker(snap):
    """Return (score, [reasons]) for one ticker snapshot from collect.collect_ticker."""
    score, reasons = 0, []
    ind = snap.get("indicators", {}) or {}
    patterns = snap.get("patterns", []) or []
    # The bar is the venue's, taken from the symbol. RSI 70 is a Tuesday on a trending token, so
    # scoring crypto against the equity bands would flag all three every cycle.
    th = config.thresholds_for(snap.get("ticker"))

    for p in patterns:
        w = p.get("strength", 1)
        score += w
        reasons.append(f"{p['name']} ({p['detail']})")

    rsi = ind.get("rsi14_d")
    if rsi is not None:
        if rsi >= th.rsi_hot:
            score += 1
            reasons.append(f"RSI hot {rsi}")
        elif rsi <= th.rsi_cold:
            score += 1
            reasons.append(f"RSI cold {rsi}")

    vol = ind.get("vol_vs_avg")
    if vol is not None and vol >= th.vol_mult:
        score += 1
        reasons.append(f"volume {vol}x average")

    return score, reasons


def _when(hours_away):
    """Render a calendar event's timing in words.

    `hours_away` is deliberately allowed to go slightly negative upstream (collect.py keeps a
    just-released print and an in-progress speech in the window, because that is exactly when the
    tape reprices). Formatting it as "in -1.5h" made those reads look like a bug in the escalation
    reason, so say plainly which side of now the event sits on.
    """
    if hours_away is None:
        return "time unknown"
    if hours_away < 0:
        return f"{abs(hours_away)}h ago, still repricing"
    if hours_away < 0.5:
        return "now"
    return f"in {hours_away}h"


def screen(market, calendar=None, held_tickers=None, force=False, threshold=3,
           escalate_score=None):
    """Evaluate a full market snapshot.

    market        : dict ticker -> snapshot (from collect.collect_market)
    calendar      : forexfactory_calendar() output, or None
    held_tickers  : tickers currently in the paper portfolio (lower their trigger bar)
    force         : anchor run — always escalate (still returns the ranked context)
    threshold     : per-ticker score needed to flag when not forced
    escalate_score: if set, a flagged ticker only *escalates* the cycle at this score or above.
                    Flagging and escalating are separate questions once the universe is wide
                    enough that something is always flagged; below this bar the triggers still
                    reach the packet, they just do not buy a deep run on their own. A held name
                    clears one step lower. None keeps the original "any trigger escalates".

                    This is the *level* gate. brain/escalate.py then applies the delta gate on top
                    of it, because a standing trigger clears any level bar on every cycle forever.

    Returns dict: {escalate: bool, why: [...], triggers: [{ticker, score, reasons}], calendar_flags: [...]}.
    """
    held = set(held_tickers or [])
    calendar = calendar or {}
    triggers, why, calendar_flags = [], [], []

    # Calendar pressure — imminent high-impact events / Trump speeches force a deep run.
    for e in calendar.get("imminent", []):
        calendar_flags.append(f"{e['title']} ({e['impact']}) {_when(e.get('hours_away'))}")
    for s in calendar.get("trump_soon", []):
        calendar_flags.append(f"SPEECH: {s['title']} {_when(s.get('hours_away'))}")
    calendar_pressure = bool(calendar_flags)

    for ticker, snap in market.items():
        if ticker in config.MARKET_CONTEXT:
            continue  # context, not a trade candidate on its own
        score, reasons = _score_ticker(snap)
        bar = threshold - 1 if ticker in held else threshold  # held names trip a step sooner
        if score >= bar and reasons:
            triggers.append({"ticker": ticker, "score": score, "reasons": reasons,
                             "held": ticker in held})

    triggers.sort(key=lambda t: t["score"], reverse=True)

    if escalate_score is None:
        worth_a_deep_run = list(triggers)
    else:
        # A held name gets a step of relief, not a free pass. "Held ⇒ escalate at any score" read
        # as prudence and behaved as a bypass: once the book carries a position, every cycle has a
        # held name flagged at *something*, so the bar stopped existing. One step down still buys
        # the look sooner for a position under stress, which is the case it was written for.
        worth_a_deep_run = [t for t in triggers
                            if t["score"] >= (escalate_score - 1 if t["held"] else escalate_score)]

    escalate = force or calendar_pressure or bool(worth_a_deep_run)
    if force:
        why.append("scheduled anchor run")
    if calendar_pressure:
        why.append("calendar pressure: " + "; ".join(calendar_flags))
    if triggers:
        top = triggers[0]
        why.append(f"{len(triggers)} ticker(s) flagged, top {top['ticker']} score {top['score']}")
        if escalate_score is not None and not worth_a_deep_run:
            why.append(f"none reached the escalation bar of {escalate_score}")

    return {
        "escalate": escalate,
        "why": why,
        "triggers": triggers,
        "calendar_flags": calendar_flags,
    }
