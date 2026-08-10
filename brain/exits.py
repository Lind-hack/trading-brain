"""The exit monitor — the three ways a trade dies that price alone cannot see.

`portfolio.mark_to_market` already answers "has this gone far enough against me?" three ways: the
hard stop, the trailing stop, and the scalp clock. All three read the position's own price and
nothing else. Lind asked for the rest of it:

    "monitor the trades that we are in and give me a sell signal when the trade has peaked or
     there is no more money to be made ... ticker news, global news — if Trump starts bombing
     Iran again and before that we were in a scalp trade for long — and the chart trend shifting."

So this module scores every open position against four families of evidence:

  * **news**    — tier 1's read on that ticker, pointed the wrong way for the position we hold.
  * **macro**   — the tape turning under the whole book at once: Haiku's macro sentiment, and the
                  VIX stretched above its own 20-day mean, which is the same statement in price.
  * **chart**   — the trend the entry was taken on, no longer there: lost the 20-day mean, MACD
                  rolled over, RSI through the midline, back at the bottom of the range.
  * **peaked**  — the move is simply over: half a real gain handed back from the high-water mark,
                  the first target printed, the trailing stop about to do this anyway.

**Nothing here closes anything.** It returns alerts; `notify` renders them and Lind decides. That
was his answer when asked — *"email alert only, you decide"* — and it is the reason the thresholds
below can sit where they are actually useful. An unattended auto-exit on a headline would need to
be far more conservative, and would then never fire on the case he described.

Two design notes worth keeping:

* **Weights are small and additive, and no single trigger can reach EXIT alone.** A headline is
  never the whole case for closing a position — the tape has to agree with it. The bands are
  `config.EXIT_ALERT_SCORE` / `EXIT_TRIM_SCORE` / `EXIT_WATCH_SCORE`.
* **The alert repeats only when it gets worse.** A broken chart stays broken, and a monitor that
  re-sends the same card every 30 minutes trains its reader to ignore it. The store below
  remembers what was last said about each position and stays quiet until the verdict escalates or
  the cooldown lapses.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta

from . import config

STORE_PATH = config.MEMORY_DIR / "EXIT-ALERTS.json"

# How long the same verdict on the same position stays quiet. An escalation (WATCH → TRIM → EXIT)
# always goes out regardless — the point of the cooldown is to stop repetition, not to delay news.
COOLDOWN_H = float(os.environ.get("BRAIN_EXIT_COOLDOWN_H", "6"))

_RANK = {"WATCH": 1, "TRIM": 2, "EXIT": 3}


def _num(x):
    """Float, or None for anything that is not a usable number — including NaN.

    NaN matters here for the same reason it does in portfolio.usable_price: it survives a null
    check and then loses every comparison it is given, so a NaN RSI would silently mean "no
    trigger" rather than "no data", and the two read identically in the output.
    """
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


def _sign(direction):
    """+1 for a long, -1 for a short. An unstated direction is a long — this book's default, and
    every position it has ever opened."""
    return -1 if str(direction or "").strip().upper() == "SHORT" else 1


# ── the four evidence families ──────────────────────────────────────────────────

def _news_triggers(ticker, pos, intel, side):
    """Tier 1's read on this name, weighed against the side we are on."""
    info = ((intel or {}).get("tickers") or {}).get(ticker)
    if not isinstance(info, dict):
        return []
    sent = _num(info.get("sentiment")) or 0.0
    mat = str(info.get("materiality") or "low").lower()
    # `against` is the sentiment pointed the wrong way for this position, as a positive magnitude.
    against = -sent * side
    if against <= 0 or not info.get("is_fresh", True):
        return []
    catalyst = info.get("catalyst") or "news"
    summary = (info.get("summary") or "").strip()
    detail = f"{catalyst} news against the position (sentiment {sent:+.0f})"
    if summary:
        detail += f" — {summary[:120]}"
    if mat == "high":
        # High materiality is the model saying this changes the story, not that it dislikes the
        # tone. That is worth the same as a broken chart on its own.
        return [{"kind": "news", "weight": 3, "detail": detail}]
    if against >= config.EXIT_NEWS_SENTIMENT:
        return [{"kind": "news", "weight": 2, "detail": detail}]
    if mat == "medium" and against >= config.EXIT_NEWS_SENTIMENT / 2:
        return [{"kind": "news", "weight": 1, "detail": detail}]
    return []


def macro_triggers(intel, market, side=1):
    """The book-wide state of the world. Computed once per cycle, applied to every position.

    Split out and given a public name because it is genuinely not per-position: every long on the
    book gets the identical macro reason, and recomputing it per ticker would only invite it to
    drift between two cards in the same email.
    """
    out = []
    macro = _num((intel or {}).get("macro_sentiment")) or 0.0
    against = -macro * side
    if against >= config.EXIT_MACRO_SENTIMENT:
        read = ((intel or {}).get("macro_read") or "").strip()
        detail = f"macro sentiment {macro:+.0f} against the book"
        if read:
            detail += f" — {read[:160]}"
        out.append({"kind": "macro", "weight": 2, "detail": detail})

    # The VIX said in price. There is no daily change in the indicator snapshot, so the stretch is
    # measured against its own 20-day mean — which is the more honest number anyway: a jump off a
    # calm base is the risk event, and a VIX that has been at 30 for a fortnight is the regime.
    vix = ((market or {}).get("^VIX") or {}).get("indicators") or {}
    px, ma20 = _num(vix.get("price")), _num(vix.get("ma20"))
    if side > 0 and px is not None:
        stretch = ((px / ma20 - 1) * 100) if ma20 else None
        hot = px >= config.EXIT_VIX_LEVEL
        jumped = stretch is not None and stretch >= config.EXIT_VIX_JUMP_PCT
        if hot or jumped:
            bits = [f"VIX {px:.1f}"]
            if stretch is not None:
                bits.append(f"{stretch:+.0f}% vs its 20-day mean")
            out.append({"kind": "macro", "weight": 2 if (hot and jumped) else 1,
                        "detail": ", ".join(bits)})
    return out


def calendar_triggers(calendar):
    """A scheduled print inside the next few hours. Never more than a note.

    Weight 1 on purpose and it cannot climb: an event that has not happened yet is a reason to know
    what you hold going into it, not evidence that the trade is wrong.
    """
    imminent = [e for e in ((calendar or {}).get("imminent") or []) if isinstance(e, dict)]
    trump = [s for s in ((calendar or {}).get("trump_soon") or []) if isinstance(s, dict)]
    for e in imminent[:1]:
        hrs = _num(e.get("hours_away"))
        when = f"in {hrs:.1f}h" if hrs is not None and hrs >= 0 else "just printed"
        return [{"kind": "calendar", "weight": 1,
                 "detail": f"{e.get('title') or 'high-impact event'} {when}"}]
    for s in trump[:1]:
        return [{"kind": "calendar", "weight": 1,
                 "detail": f"{s.get('title') or 'speech'} inside the day"}]
    return []


def _chart_triggers(snap, side):
    """The trend the entry was taken on, checked for still being there."""
    ind = (snap or {}).get("indicators") or {}
    px, ma20 = _num(ind.get("price")), _num(ind.get("ma20"))
    out = []
    if px is not None and ma20:
        # Signed so a short reads the mirror image rather than a second set of branches.
        if (ma20 - px) * side > 0:
            out.append({"kind": "chart", "weight": 1,
                        "detail": f"price {px:.2f} on the wrong side of the 20-day mean {ma20:.2f}"})
    macd = str(ind.get("macd_d") or "")
    if macd:
        rolled = macd.startswith("bearish") if side > 0 else macd.startswith("bullish")
        if rolled:
            out.append({"kind": "chart", "weight": 1, "detail": f"MACD {macd}"})
    rsi = _num(ind.get("rsi14_d"))
    if rsi is not None and (50 - rsi) * side > 5:
        out.append({"kind": "chart", "weight": 1, "detail": f"daily RSI {rsi:.0f} through the midline"})
    rpos = _num(ind.get("range_pos"))
    if rpos is not None:
        # Bottom quarter of the 20-day range for a long. The entry gate refuses a *buy* at the top
        # of the range for the mirror-image reason: no room left in the direction of the trade.
        low_end = rpos <= 0.25 if side > 0 else rpos >= 0.75
        if low_end:
            out.append({"kind": "chart", "weight": 1,
                        "detail": f"back at the {'bottom' if side > 0 else 'top'} of its 20-day "
                                  f"range (range_pos {rpos:.2f})"})
    return out


def _peak_triggers(pos, px, rules, side, scalp_hours_left=None):
    """Is the move over? Everything here is measured against the position's own high-water mark."""
    entry = _num(pos.get("entry"))
    if not entry or px is None:
        return []
    gain = (px - entry) / entry * 100 * side
    hw = _num(pos.get("high_water")) or entry
    peak = (hw - entry) / entry * 100 * side
    out = []

    if peak >= rules.exit_min_peak_pct and gain <= peak * (1 - config.EXIT_GIVEBACK_FRAC):
        # The one trigger that is worth an exit on its own arithmetic. A winner that has handed
        # back half of a real move is not a winner that is resting.
        out.append({"kind": "peaked", "weight": 3,
                    "detail": f"peaked at {peak:+.1f}%, now {gain:+.1f}% — over half the move "
                              f"handed back"})

    t1 = _num(pos.get("target1")) or _num(pos.get("target"))
    if t1 and (px - t1) * side >= 0:
        out.append({"kind": "peaked", "weight": 2,
                    "detail": f"first target {config.format_price(t1)} printed "
                              f"({config.format_price(px)} now)"})

    stop = _num(pos.get("stop_level"))
    if stop and stop > 0:
        room = (px - stop) / px * 100 * side
        if 0 <= room <= rules.exit_near_stop_pct:
            out.append({"kind": "peaked", "weight": 1,
                        "detail": f"{room:.1f}% from the trailing stop at "
                                  f"{config.format_price(stop)} — it will fire on its own"})

    hrs = _num(scalp_hours_left)
    if hrs is not None and hrs <= 1:
        out.append({"kind": "peaked", "weight": 1,
                    "detail": f"scalp clock: {hrs:.1f}h before the time stop force-closes it"})

    ext = _num((pos.get("_indicators") or {}).get("ext_atr"))
    rsi = _num((pos.get("_indicators") or {}).get("rsi14_d"))
    if gain > 0 and ext is not None and rsi is not None:
        stretched = (ext * side >= 2) and ((rsi - 50) * side >= 20)
        if stretched:
            out.append({"kind": "peaked", "weight": 1,
                        "detail": f"stretched {ext:+.1f} ATR from the mean at RSI {rsi:.0f} — "
                                  f"the easy part of this move is behind it"})
    return out


# ── scoring ─────────────────────────────────────────────────────────────────────

def verdict_for(score):
    """Score to verdict, or None when there is nothing worth an email."""
    if score >= config.EXIT_ALERT_SCORE:
        return "EXIT"
    if score >= config.EXIT_TRIM_SCORE:
        return "TRIM"
    return "WATCH" if score >= config.EXIT_WATCH_SCORE else None


def review(portfolio, prices=None, market=None, intel=None, calendar=None, now=None,
           store=None):
    """Score every open position. Returns the alerts worth sending, worst first.

    Pure with respect to the ledger: it reads positions and writes nothing to them. `store` is the
    repeat-suppression memory — pass one to keep it in the caller's hands (the tests do), or leave
    it None to load and save the on-disk one.
    """
    now = now or datetime.now(config.UTC)
    prices = prices or {}
    rules = portfolio.rules
    own_store = store is None
    store = load_store() if own_store else store

    macro_long = macro_triggers(intel, market, side=1)
    macro_short = macro_triggers(intel, market, side=-1)
    cal = calendar_triggers(calendar)

    alerts = []
    for ticker, pos in (portfolio.state.get("positions") or {}).items():
        px = _num(prices.get(ticker)) or _num(pos.get("last")) or _num(pos.get("entry"))
        if px is None:
            continue
        side = _sign(pos.get("direction"))
        snap = (market or {}).get(ticker) or {}
        # Handed to _peak_triggers on a copy of the position rather than as another argument: the
        # stretch test needs the same indicators the chart tests read, and threading them through
        # two more parameters buys nothing.
        pos_view = dict(pos, _indicators=(snap.get("indicators") or {}))

        triggers = (_news_triggers(ticker, pos, intel, side)
                    + (macro_long if side > 0 else macro_short)
                    + _chart_triggers(snap, side)
                    + _peak_triggers(pos_view, px, rules, side,
                                     scalp_hours_left=portfolio.scalp_hours_left(pos, now)))
        # The calendar only ever qualifies something already flagged. On its own it would put a
        # WATCH card on every open position twice a week, which is noise wearing a schedule.
        if triggers:
            triggers += cal

        score = sum(int(t.get("weight") or 0) for t in triggers)
        verdict = verdict_for(score)
        if not verdict:
            continue

        entry = _num(pos.get("entry")) or px
        hw = _num(pos.get("high_water")) or entry
        alerts.append({
            "ticker": ticker, "verdict": verdict, "score": score,
            "direction": "SHORT" if side < 0 else "LONG",
            "trade_type": pos.get("trade_type"),
            "entry": entry, "last": px,
            "pnl_pct": round((px - entry) / entry * 100 * side, 2),
            "peak_pct": round((hw - entry) / entry * 100 * side, 2),
            "stop_level": _num(pos.get("stop_level")),
            "target1": _num(pos.get("target1")) or _num(pos.get("target")),
            "confidence": pos.get("confidence"),
            "thesis": pos.get("thesis"),
            "triggers": triggers,
            "reasons": [t["detail"] for t in triggers],
            "kinds": sorted({t["kind"] for t in triggers}),
            "book": rules.name,
        })

    alerts.sort(key=lambda a: (-_RANK[a["verdict"]], -a["score"], a["pnl_pct"]))
    fresh = [a for a in alerts if _should_send(a, store, now)]
    for a in fresh:
        _record(a, store, now)
    _forget_closed(store, portfolio.state.get("positions") or {})
    if own_store:
        save_store(store)
    return fresh


def summarize(alerts):
    """One line for the run log."""
    if not alerts:
        return "no exit alerts"
    by = {}
    for a in alerts:
        by[a["verdict"]] = by.get(a["verdict"], 0) + 1
    parts = [f"{by[v]} {v}" for v in ("EXIT", "TRIM", "WATCH") if v in by]
    names = ", ".join(f"{a['ticker']} {a['verdict']}" for a in alerts[:4])
    return f"{'/'.join(parts)} — {names}"


# ── repeat suppression ──────────────────────────────────────────────────────────

def _should_send(alert, store, now):
    """Has this already been said? Escalation always goes out; a repeat waits for the cooldown."""
    prev = (store.get("alerts") or {}).get(alert["ticker"])
    if not isinstance(prev, dict):
        return True
    if _RANK[alert["verdict"]] > _RANK.get(prev.get("verdict"), 0):
        return True
    try:
        last = datetime.fromisoformat(str(prev.get("ts")).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=config.UTC)
    return now - last >= timedelta(hours=COOLDOWN_H)


def _record(alert, store, now):
    store.setdefault("alerts", {})[alert["ticker"]] = {
        "verdict": alert["verdict"], "score": alert["score"], "ts": now.isoformat(),
    }


def _forget_closed(store, positions):
    """Drop the memory of a name that is no longer held.

    Without this, a ticker bought again next month inherits the suppression from the last time it
    was on the book — the new position would be silently held back by an alert about an old one.
    """
    for t in list((store.get("alerts") or {}).keys()):
        if t not in positions:
            store["alerts"].pop(t, None)


def load_store():
    try:
        with open(STORE_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and isinstance(data.get("alerts"), dict):
            return data
    except FileNotFoundError:
        pass
    except Exception as e:  # pragma: no cover - corrupt file
        print(f"[warn] exit-alert store unreadable ({e}) — starting a fresh one", file=sys.stderr)
    return {"version": 1, "alerts": {}}


def save_store(store):
    try:
        STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = STORE_PATH.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(store, fh, indent=1, sort_keys=True)
        os.replace(tmp, STORE_PATH)
    except Exception as e:  # pragma: no cover - disk
        print(f"[warn] could not save exit-alert store: {e}", file=sys.stderr)
