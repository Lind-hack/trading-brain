"""Is the tape worth being invested in right now?

Every other gate in this book asks about one trade. This one asks about all of them at once, and
it exists because of the goal Lind actually stated: beat the S&P 500. A long book does not lose to
an index by picking bad names — over a year it loses by staying fully invested through the stretch
where participation narrows, the index keeps printing green on five mega-caps, and everything the
book actually owns rolls over. Nothing in this system could see that. `config.MARKET_CONTEXT` was
SPY, QQQ, the VIX, futures, the dollar and the ten-year: six ways of asking what the *index* did,
and no way of asking how many stocks came with it.

So this module measures participation, from two sources that cost almost nothing:

**Internal breadth is free.** `collect.py` already puts `price`, `ma20`, `ma50` and `range_pos` on
every focus ticker, every cycle. The share of the book's own universe above its averages, and the
count sitting at 20-day highs against 20-day lows, is arithmetic over data already in hand — no
extra request, and it measures the tape this book actually trades rather than a broad index it
does not.

**Cross-asset ratios cost four symbols.** RSP against SPY reads concentration (equal weight
lagging cap weight means the average stock is not participating), IWM against SPY reads size
appetite, HYG against LQD reads credit. Each is measured as distance above its own 20-day mean, so
one current snapshot is enough and no history has to be stored.

Deliberately deterministic and model-free, in the same spirit as `brain/exits.py`: arithmetic over
data the cycle already collected, so it costs nothing, cannot hallucinate, and runs even on the
off-hours passes where no model runs at all.

**Missing is missing.** A component that cannot be computed is absent from the score rather than
counted as neutral — the same rule the fundamentals block runs under. A verdict built on two
readings says so, and the harness treats a degraded read as NEUTRAL rather than as permission.
"""
from __future__ import annotations

import os

from . import config

# Verdicts, worst to best. CONTRACTION is the only one the harness acts on: EXPANSION does not
# raise any limit, because "the tape is good" has never been a reason to take a worse trade.
CONTRACTION, NEUTRAL, EXPANSION = "CONTRACTION", "NEUTRAL", "EXPANSION"

# How many components must be computable before the verdict means anything. Below this the read is
# degraded and reports NEUTRAL — a brake that engages on one missing data feed would be worse than
# no brake, because it would fire on exactly the days the collector was struggling.
MIN_COMPONENTS = int(os.environ.get("BRAIN_REGIME_MIN_COMPONENTS", "3"))


def _ind(market, ticker):
    return ((market or {}).get(ticker) or {}).get("indicators") or {}


def _above_own_mean(market, ticker):
    """How far a symbol sits above its own 20-day mean, in percent.

    Comparing two of these is a relative-strength read that needs no history: both sides are
    normalised by their own average, so RSP at +1% and SPY at +3% says the average stock is
    lagging the index by two points without either series being stored.
    """
    ind = _ind(market, ticker)
    price, ma20 = ind.get("price"), ind.get("ma20")
    if not price or not ma20:
        return None
    return (price - ma20) / ma20 * 100


def _spread(market, a, b):
    """`a` minus `b`, each measured against its own mean. None when either side is missing."""
    x, y = _above_own_mean(market, a), _above_own_mean(market, b)
    return None if x is None or y is None else round(x - y, 2)


def breadth(market, universe=None):
    """Participation across the book's own universe. Context symbols are excluded by construction.

    The universe is what this book can actually trade, not an index — a book of thirty names is
    better served by knowing how its own thirty are doing than by the S&P's advance-decline line,
    and it is the only breadth available without paying for constituent data.
    """
    names = [t for t in (universe or config.FOCUS_TICKERS or [])
             if t not in config.MARKET_CONTEXT]
    above20 = above50 = highs = lows = counted = 0
    for t in names:
        ind = _ind(market, t)
        price = ind.get("price")
        if not price:
            continue
        counted += 1
        if ind.get("ma20"):
            above20 += 1 if price > ind["ma20"] else 0
        if ind.get("ma50"):
            above50 += 1 if price > ind["ma50"] else 0
        rp = ind.get("range_pos")
        if rp is not None:
            highs += 1 if rp >= 0.95 else 0
            lows += 1 if rp <= 0.05 else 0
    if not counted:
        return {"counted": 0}
    return {
        "counted": counted,
        "pct_above_ma20": round(above20 / counted, 3),
        "pct_above_ma50": round(above50 / counted, 3),
        "new_highs": highs,
        "new_lows": lows,
    }


def assess(market, universe=None):
    """The regime read: components, a signed score, and a verdict.

    Each component votes −1, 0 or +1. The thresholds are deliberately wide — this decides whether
    the book takes new risk at all, so it must fire on a real change in participation and not on
    an ordinary Tuesday.
    """
    b = breadth(market, universe)
    votes, reasons = [], []

    def vote(v, why):
        votes.append(v)
        if v:
            reasons.append(why)

    if b.get("counted"):
        p20, p50 = b.get("pct_above_ma20"), b.get("pct_above_ma50")
        if p50 is not None:
            vote(-1 if p50 < 0.40 else (1 if p50 > 0.60 else 0),
                 f"{p50:.0%} of the universe above its 50-day mean")
        if p20 is not None:
            vote(-1 if p20 < 0.35 else (1 if p20 > 0.65 else 0),
                 f"{p20:.0%} above the 20-day mean")
        hi, lo = b.get("new_highs", 0), b.get("new_lows", 0)
        if hi or lo:
            # Doubling, not a bare majority: a 6-5 split is noise on a thirty-name universe.
            vote(-1 if lo >= max(2, hi * 2) else (1 if hi >= max(2, lo * 2) else 0),
                 f"{hi} at 20-day highs vs {lo} at lows")

    # Equal weight against cap weight. The single most useful line in a narrowing tape: the index
    # can rise on five names while the average stock falls, and this is what says so.
    conc = _spread(market, "RSP", "SPY")
    if conc is not None:
        vote(-1 if conc < -1.0 else (1 if conc > 1.0 else 0),
             f"equal-weight vs cap-weight {conc:+.2f}pp")
    size = _spread(market, "IWM", "SPY")
    if size is not None:
        vote(-1 if size < -1.0 else (1 if size > 1.0 else 0),
             f"small caps vs the index {size:+.2f}pp")
    credit = _spread(market, "HYG", "LQD")
    if credit is not None:
        vote(-1 if credit < -0.75 else (1 if credit > 0.75 else 0),
             f"high yield vs investment grade {credit:+.2f}pp")
    vix = _above_own_mean(market, "^VIX")
    if vix is not None:
        # Only the risk-off side votes. A calm VIX is not evidence that breadth is healthy, and
        # letting it vote +1 would have complacency arguing for more exposure.
        vote(-1 if vix > 15 else 0, f"VIX {vix:+.1f}% above its 20-day mean")

    score = sum(votes)
    degraded = len(votes) < MIN_COMPONENTS
    if degraded:
        verdict = NEUTRAL
    elif score <= -2:
        verdict = CONTRACTION
    elif score >= 2:
        verdict = EXPANSION
    else:
        verdict = NEUTRAL
    return {
        "verdict": verdict,
        "score": score,
        "components": len(votes),
        "degraded": degraded,
        "breadth": b,
        "concentration_pp": conc,
        "size_pp": size,
        "credit_pp": credit,
        "vix_vs_mean_pct": None if vix is None else round(vix, 1),
        # Only the readings that actually voted, so the analyst cites a number rather than a label.
        "reasons": reasons[:5],
    }


def is_hostile(regime):
    """Does this read mean the harness should take less new risk?

    A degraded read is never hostile. The brake must engage on measured contraction, never on a
    collector that returned half a snapshot.
    """
    r = regime or {}
    return r.get("verdict") == CONTRACTION and not r.get("degraded")
