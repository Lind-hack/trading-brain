"""Is this a good price to pay for a thesis that is already good?

Everything upstream of this module answers a different question. The screener asks whether a name
is worth looking at; Haiku asks what the news says; Opus asks whether there is a trade. None of
them asks what the entry costs, and on 2026-07-27 three defensible theses were all bought above the
prior 20-day high — a place with no room left above it and the whole range below as downside.

So this is a price gate, not a thesis gate. It never argues with the analysis and it never silently
drops a signal — a refused entry still goes out in the email carrying `level`, the highest price at
which the same trade would have passed. See the ENTRY_* block in config.py for the ledger rows.

Two independent tests, either of which refuses:

  range_pos   where the entry sits in the last 20 daily closes, 0 at the low and 1 at the high
  ext_atr     how far above the 20-day mean it sits, in ATRs

They overlap but do not coincide, and both were needed: CRM was refused on range alone at
ext_atr +1.00, while a name that has drifted up inside a wide range fails the ATR test first. A
third test catches the different mistake of quoting a price above the last print.
"""
from . import config


def _f(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and abs(f) != float("inf") else None


def check(action, indicators, rules, last_price=None):
    """Grade a proposed entry price. Returns (ok, reason, level).

    action:     the proposed action dict — `entry`, `direction`, `action`.
    indicators: that ticker's `indicators` block from the packet (hi20/lo20/ma20/atr14/price).
    rules:      the book's RuleSet, which carries the three bars.
    last_price: the tape, if the caller has a fresher one than `indicators["price"]`.

    `ok` is True whenever the entry passes *or* cannot be judged. Missing indicators are the normal
    state for a thinly-covered name and a gate that fails closed on absent data would quietly stop
    the book trading, which is a worse failure than the one it is guarding against. `level` is the
    best price that would have passed, or None when there is nothing to say.
    """
    kind = (action.get("action") or "").upper()
    if kind not in ("BUY", "ADD"):
        return True, "", None

    entry = _f(action.get("entry"))
    if entry is None or entry <= 0:
        return True, "no entry price to grade", None

    ind = indicators or {}
    short = str(action.get("direction") or "LONG").strip().upper() == "SHORT"
    hi20, lo20 = _f(ind.get("hi20")), _f(ind.get("lo20"))
    ma20, atr14 = _f(ind.get("ma20")), _f(ind.get("atr14"))
    last = _f(last_price) if last_price is not None else _f(ind.get("price"))

    max_pos = rules.entry_max_range_pos
    max_ext = rules.entry_max_ext_atr

    # Every bar restated as the price that would just pass it, so the answer to "then what should
    # I have paid?" is one number rather than three thresholds the reader has to combine. Worked
    # out up front, because the level quoted on a rejection has to satisfy the *other* tests too —
    # naming a range-derived price that is still 3 ATRs extended would just move the problem.
    caps = []
    if hi20 is not None and lo20 is not None and hi20 > lo20:
        span = hi20 - lo20
        caps.append(lo20 + ((1 - max_pos) if short else max_pos) * span)
    if ma20 is not None and atr14:
        caps.append(ma20 + (-max_ext if short else max_ext) * atr14)
    # A short wants the highest price that still passes; a long, the lowest. Shaved a tenth of a
    # percent to the passing side before rounding: the raw cap sits exactly *on* the bar, and
    # rounding it to a tradeable number lands on the wrong side of it about half the time — so the
    # price the rejection tells Lind to wait for would itself be rejected.
    level = None
    if caps:
        raw = max(caps) if short else min(caps)
        level = config.round_price(raw * (1.001 if short else 0.999))

    # 1. Where in the range. A SHORT is the mirror image: selling the low of the range is the same
    #    mistake as buying the high, and it fails for the same reason — no room left to be right in.
    if hi20 is not None and lo20 is not None and hi20 > lo20:
        pos = (entry - lo20) / (hi20 - lo20)
        if (pos < 1 - max_pos) if short else (pos > max_pos):
            return False, (f"entry {config.format_price(entry)} sits at {pos * 100:.0f}% of the "
                           f"20-day range — too near the {'low' if short else 'high'} "
                           f"({config.format_price(lo20)}–{config.format_price(hi20)})"), level

    # 2. How far from the mean, in ATRs — the same question where the range is an awkward shape.
    if ma20 is not None and atr14:
        ext = (entry - ma20) / atr14
        if (ext < -max_ext) if short else (ext > max_ext):
            return False, (f"entry {config.format_price(entry)} is {abs(ext):.1f} ATR "
                           f"{'below' if short else 'above'} the 20-day mean "
                           f"({config.format_price(ma20)}) — extended"), level

    # 3. Paying up. Not a chart problem: an entry above the last print is a quote that was never
    #    available, and the fill is booked at it regardless — portfolio.py fills at `entry`.
    if last and last > 0:
        drift = (entry - last) / last * 100
        chase = -drift if short else drift
        if chase > rules.entry_max_chase_pct:
            return False, (f"entry {config.format_price(entry)} is {chase:.2f}% "
                           f"{'below' if short else 'above'} the last print "
                           f"({config.format_price(last)}) — chasing"), config.round_price(last)

    return True, "entry priced inside the range", level
