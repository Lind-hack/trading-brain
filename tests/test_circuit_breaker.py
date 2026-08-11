"""The brake that watches the equity curve rather than any one position.

Every risk rule written before this one asks about a single trade: is this stop wide enough, is
this entry chased, has this sector lost twice. Sector lockout catches two losers in one sector.
Nothing caught six losers spread across six — a book can bleed its way down without any individual
rule objecting, which is the ordinary way an account that was beating the index stops beating it.

Three properties, in order of how expensive they are to get wrong:

1. It halts buying and never selling. A brake that could block an exit is not risk control, it is
   a trap.
2. It has hysteresis. Halting at −8% and re-arming at −8% flaps across the boundary, refusing a
   buy one cycle and allowing it the next on a book oscillating by a tenth of a percent.
3. The high-water mark survives. It lives in state rather than being derived from `equity_curve`,
   which is trimmed to 500 points — a peak set three weeks ago would otherwise fall off the end
   and hand the breaker a fresh high exactly when the drawdown was deepest.

Run:  python -m pytest tests/test_circuit_breaker.py -q
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from brain import config
from brain.portfolio import Portfolio


@pytest.fixture
def pf(tmp_path):
    return Portfolio(path=tmp_path / "PORTFOLIO.json", rules=config.STOCK_RULES)


@pytest.fixture
def crypto_pf(tmp_path):
    return Portfolio(path=tmp_path / "PORTFOLIO_CRYPTO.json", rules=config.CRYPTO_RULES)


def _drive_equity(pf, equity):
    """Put the book at a chosen equity by moving cash, then mark it."""
    pf.state["cash"] = equity
    pf.mark_to_market({})
    return pf


def _buy(ticker="AAA", conf=85):
    return {"action": "BUY", "ticker": ticker, "entry": 100.0, "confidence": conf,
            "trade_type": "SHORT_TERM", "reason": "test"}


# ── the high-water mark ─────────────────────────────────────────────────────────

def test_the_high_water_mark_only_rises(pf):
    _drive_equity(pf, 12000)
    assert pf.state["equity_high_water"] == pytest.approx(12000)
    _drive_equity(pf, 9000)
    assert pf.state["equity_high_water"] == pytest.approx(12000), "a peak is not un-set by a fall"


def test_drawdown_is_measured_from_the_peak_not_the_start(pf):
    _drive_equity(pf, 20000)
    _drive_equity(pf, 18000)
    assert pf.drawdown_pct({}) == pytest.approx(-10.0)


def test_an_unmarked_book_has_no_drawdown_to_report(pf):
    """The peak must be observed, not assumed. Falling back to `starting_cash` would invent one,
    and any caller that had not yet marked the book — a hand-run action, a test fixture — would
    measure a drawdown against a number nothing ever reached."""
    assert pf.drawdown_pct({}) is None
    pf.mark_to_market({})
    assert pf.drawdown_pct({}) == pytest.approx(0.0)


# ── tripping and re-arming ──────────────────────────────────────────────────────

def test_a_shallow_drawdown_does_not_trip(pf):
    _drive_equity(pf, 10000)
    _drive_equity(pf, 9400)          # −6%, inside the −8% halt
    ok, msg = pf.validate_action(_buy(), prices={"AAA": 100.0})
    assert ok, msg


def test_breaching_the_threshold_halts_new_buys(pf):
    _drive_equity(pf, 10000)
    _drive_equity(pf, 9000)          # −10%
    ok, msg = pf.validate_action(_buy(), prices={"AAA": 100.0})
    assert not ok
    assert "circuit breaker" in msg and "high-water" in msg


def test_it_stays_tripped_through_a_partial_recovery(pf):
    """The hysteresis. −6% is above the halt level but below the re-arm level, and a brake that
    cleared here would flap every other cycle on an oscillating book."""
    _drive_equity(pf, 10000)
    _drive_equity(pf, 9000)          # trip at −10%
    _drive_equity(pf, 9400)          # recover to −6%
    ok, msg = pf.validate_action(_buy(), prices={"AAA": 100.0})
    assert not ok, "still engaged between the halt and re-arm levels"


def test_a_real_recovery_clears_it(pf):
    _drive_equity(pf, 10000)
    _drive_equity(pf, 9000)
    _drive_equity(pf, 9700)          # −3%, past the −4% re-arm
    ok, msg = pf.validate_action(_buy(), prices={"AAA": 100.0})
    assert ok, msg
    assert pf.state["breaker_tripped"] is False


def test_the_tripped_state_survives_a_reload(pf, tmp_path):
    """It is a fact about the past. Recomputing it from today's number alone would lose the
    hysteresis on every restart."""
    _drive_equity(pf, 10000)
    _drive_equity(pf, 9000)
    pf.validate_action(_buy(), prices={"AAA": 100.0})
    pf.save()
    reloaded = Portfolio(path=tmp_path / "PORTFOLIO.json", rules=config.STOCK_RULES)
    assert reloaded.state["breaker_tripped"] is True


# ── what it must never do ───────────────────────────────────────────────────────

def test_it_never_blocks_a_sell(pf):
    pf.apply_action(_buy(conf=90), prices={"AAA": 100.0})
    _drive_equity(pf, pf.state["cash"])
    pf.state["equity_high_water"] = 100000        # force a deep drawdown
    ok, msg = pf.validate_action({"action": "SELL", "ticker": "AAA"}, prices={"AAA": 50.0})
    assert ok, f"a breaker that blocks exits is a trap, not risk control: {msg}"


def test_the_crypto_book_uses_its_own_numbers(crypto_pf):
    """−8% is a bad fortnight on equities and an ordinary Tuesday on a book running a −15% hard
    stop. A breaker set at the equity level would spend the year tripped."""
    _drive_equity(crypto_pf, 10000)
    _drive_equity(crypto_pf, 8900)               # −11%: past the equity halt, inside crypto's
    ok, msg = crypto_pf.validate_action(
        {"action": "BUY", "ticker": "BTC-USD", "entry": 100.0, "confidence": 85,
         "trade_type": "SHORT_TERM", "reason": "test"}, prices={"BTC-USD": 100.0})
    assert ok, msg
    _drive_equity(crypto_pf, 7900)               # −21%: past crypto's −18%
    ok, _ = crypto_pf.validate_action(
        {"action": "BUY", "ticker": "ETH-USD", "entry": 100.0, "confidence": 85,
         "trade_type": "SHORT_TERM", "reason": "test"}, prices={"ETH-USD": 100.0})
    assert not ok
