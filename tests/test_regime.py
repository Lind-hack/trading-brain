"""Participation, and what the book does when it narrows.

A long book does not lose to the index by picking bad names. Over a year it loses by staying fully
invested through the stretch where participation narrows — the index still rises on five mega-caps
while the average holding rolls over — and gives back the alpha without one bad decision at the
trade level. Nothing in this system could see that: `MARKET_CONTEXT` was six ways of asking what
the *index* did and no way of asking how many stocks came with it.

These tests pin the measurement and the brake:

1. Breadth is computed over the book's own universe, never the context symbols.
2. Missing data is missing, not neutral — the same rule `fundamentals` runs under.
3. A thin read is degraded and never hostile: a brake that engages when the collector returns half
   a snapshot would fire on exactly the days it should stay quiet.
4. Contraction refuses low-conviction entries and halves the ones it allows. It never blocks a
   sell.

Run:  python -m pytest tests/test_regime.py -q
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from brain import config, regime
from brain.portfolio import Portfolio

UNIVERSE = ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG", "HHH", "III", "JJJ"]


def _names(pct_above, range_pos):
    """A universe where `pct_above` of names are over both means."""
    out, n_up = {}, int(len(UNIVERSE) * pct_above)
    for i, t in enumerate(UNIVERSE):
        up = i < n_up
        out[t] = {"indicators": {"price": 110.0 if up else 90.0, "ma20": 100.0, "ma50": 100.0,
                                 "range_pos": range_pos}}
    return out


def _ratios(conc=0.0, size=0.0, credit=0.0, vix=0.0):
    """Context symbols placed at chosen distances above their own 20-day means."""
    def at(pct):
        return {"indicators": {"price": 100.0 * (1 + pct / 100), "ma20": 100.0}}
    return {"SPY": at(0.0), "RSP": at(conc), "IWM": at(size),
            "LQD": at(0.0), "HYG": at(credit), "^VIX": at(vix)}


def _tape(pct_above=0.5, range_pos=0.5, **kw):
    m = _names(pct_above, range_pos)
    m.update(_ratios(**kw))
    return m


# ── measurement ─────────────────────────────────────────────────────────────────

def test_breadth_counts_the_books_universe_not_the_context():
    """SPY and the VIX set the regime; counting them as constituents would let two index symbols
    outvote the names actually held."""
    b = regime.breadth(_tape(pct_above=0.5), universe=UNIVERSE + ["SPY", "^VIX"])
    assert b["counted"] == len(UNIVERSE)


def test_breadth_reads_participation():
    b = regime.breadth(_tape(pct_above=0.8), universe=UNIVERSE)
    assert b["pct_above_ma20"] == pytest.approx(0.8)
    assert b["pct_above_ma50"] == pytest.approx(0.8)


def test_new_highs_and_lows_come_off_range_position():
    assert regime.breadth(_tape(range_pos=0.98), universe=UNIVERSE)["new_highs"] == len(UNIVERSE)
    assert regime.breadth(_tape(range_pos=0.01), universe=UNIVERSE)["new_lows"] == len(UNIVERSE)


def test_a_name_with_no_price_is_not_counted():
    m = _tape()
    m["AAA"] = {"indicators": {}}
    assert regime.breadth(m, universe=UNIVERSE)["counted"] == len(UNIVERSE) - 1


# ── the verdict ─────────────────────────────────────────────────────────────────

def test_a_broad_tape_reads_expansion():
    r = regime.assess(_tape(pct_above=0.9, range_pos=0.97, conc=2.0, size=2.0, credit=1.5),
                      universe=UNIVERSE)
    assert r["verdict"] == regime.EXPANSION
    assert not regime.is_hostile(r)


def test_a_narrowing_tape_reads_contraction():
    """The case the whole module exists for: the index is fine, the average stock is not."""
    r = regime.assess(_tape(pct_above=0.1, range_pos=0.02, conc=-2.5, size=-2.0, credit=-1.5),
                      universe=UNIVERSE)
    assert r["verdict"] == regime.CONTRACTION
    assert regime.is_hostile(r)
    assert r["reasons"], "a verdict the analyst cannot cite a number for is not usable"


def test_a_mixed_tape_reads_neutral():
    r = regime.assess(_tape(pct_above=0.5, range_pos=0.5), universe=UNIVERSE)
    assert r["verdict"] == regime.NEUTRAL
    assert not regime.is_hostile(r)


def test_a_calm_vix_never_argues_for_more_exposure():
    """Only the risk-off side of the VIX votes. Complacency is not evidence of breadth."""
    quiet = regime.assess(_tape(pct_above=0.5, range_pos=0.5, vix=-30), universe=UNIVERSE)
    flat = regime.assess(_tape(pct_above=0.5, range_pos=0.5, vix=0), universe=UNIVERSE)
    assert quiet["score"] == flat["score"]


def test_a_thin_read_is_degraded_and_never_hostile():
    """A collector that returned almost nothing must not trip the brake — that would fire it on
    exactly the days the data was worst, which is when it is least trustworthy."""
    r = regime.assess({"SPY": {"indicators": {"price": 100, "ma20": 100}}}, universe=UNIVERSE)
    assert r["degraded"] is True
    assert r["verdict"] == regime.NEUTRAL
    assert not regime.is_hostile(r)


def test_missing_ratios_are_absent_not_zero():
    r = regime.assess(_names(0.5, 0.5), universe=UNIVERSE)
    assert r["concentration_pp"] is None and r["credit_pp"] is None


# ── enforcement ─────────────────────────────────────────────────────────────────

@pytest.fixture
def pf(tmp_path):
    p = Portfolio(path=tmp_path / "PORTFOLIO.json", rules=config.STOCK_RULES)
    p.rules = config.STOCK_RULES
    return p


def _buy(conf=80, weight=None):
    a = {"action": "BUY", "ticker": "AAA", "entry": 90.0, "confidence": conf,
         "trade_type": "SHORT_TERM", "reason": "test"}
    if weight is not None:
        a["target_weight_pct"] = weight
    return a


HOSTILE = dict(pct_above=0.1, range_pos=0.02, conc=-2.5, size=-2.0, credit=-1.5)


def test_contraction_refuses_a_thin_conviction_entry(pf, monkeypatch):
    monkeypatch.setattr(config.STOCK_RULES, "tickers", UNIVERSE, raising=False)
    ok, msg = pf.validate_action(_buy(conf=68), prices={"AAA": 90.0}, market=_tape(**HOSTILE))
    assert not ok and "regime is contracting" in msg


def test_contraction_still_allows_real_conviction(pf, monkeypatch):
    monkeypatch.setattr(config.STOCK_RULES, "tickers", UNIVERSE, raising=False)
    ok, msg = pf.validate_action(_buy(conf=82), prices={"AAA": 90.0}, market=_tape(**HOSTILE))
    assert ok, msg


def test_contraction_halves_the_position(pf, monkeypatch):
    """Being wrong about the regime should cost a smaller size, not a missed year."""
    monkeypatch.setattr(config.STOCK_RULES, "tickers", UNIVERSE, raising=False)
    pf.apply_action(_buy(conf=82, weight=12), prices={"AAA": 90.0}, market=_tape(**HOSTILE))
    held = pf.state["positions"]["AAA"]
    weight_pct = held["shares"] * held["entry"] / pf.state["starting_cash"] * 100
    assert weight_pct == pytest.approx(6.0, abs=0.5), "12% under contraction should size to ~6%"


def test_a_healthy_tape_leaves_sizing_alone(pf, monkeypatch):
    monkeypatch.setattr(config.STOCK_RULES, "tickers", UNIVERSE, raising=False)
    good = _tape(pct_above=0.9, range_pos=0.97, conc=2.0, size=2.0, credit=1.5)
    pf.apply_action(_buy(conf=82, weight=12), prices={"AAA": 90.0}, market=good)
    held = pf.state["positions"]["AAA"]
    weight_pct = held["shares"] * held["entry"] / pf.state["starting_cash"] * 100
    assert weight_pct == pytest.approx(12.0, abs=0.5)


def test_the_brake_never_blocks_an_exit(pf, monkeypatch):
    """Refusing to sell into a contracting tape would be the opposite of risk control."""
    monkeypatch.setattr(config.STOCK_RULES, "tickers", UNIVERSE, raising=False)
    pf.apply_action(_buy(conf=90), prices={"AAA": 90.0}, market=None)
    ok, msg = pf.validate_action({"action": "SELL", "ticker": "AAA"},
                                 prices={"AAA": 80.0}, market=_tape(**HOSTILE))
    assert ok, msg


def test_no_market_snapshot_means_no_brake(pf):
    """Off-hours passes and hand-run actions arrive without a snapshot. Absent evidence is not
    evidence of contraction."""
    ok, msg = pf.validate_action(_buy(conf=68), prices={"AAA": 90.0}, market=None)
    assert ok, msg
