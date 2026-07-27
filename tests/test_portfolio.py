"""Unit tests for the paper-portfolio discipline gates (brain/portfolio.py).

These are the rules that make the simulated account safe: position cap, per-name weight cap,
weekly new-trade cap, sector lockout, the -7% hard cut, and the tightening trailing stop.
Run:  python -m pytest tests/ -q
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from brain import config
from brain.portfolio import Portfolio, sector_of


@pytest.fixture
def pf(tmp_path):
    """A fresh $10k paper account backed by a throwaway JSON file."""
    return Portfolio(path=tmp_path / "PORTFOLIO.json")


def _buy(pf, ticker, entry, weight=10.0, stop=None):
    return pf.apply_action(
        {"action": "BUY", "ticker": ticker, "entry": entry,
         "stop": stop, "target_weight_pct": weight, "reason": "test"},
        prices={ticker: entry},
    )


# ── position cap ────────────────────────────────────────────────────────────────

def test_one_position_past_the_cap_is_rejected(pf):
    # Preload a full book directly (bypasses the weekly cap so we isolate the position cap).
    for i in range(config.MAX_POSITIONS):
        t = f"T{i}"
        pf.state["positions"][t] = {
            "shares": 1, "entry": 100, "opened": None, "sector": "Other",
            "high_water": 100, "hard_stop": 93, "trail_pct": 10, "last": 100,
        }
    ok, msg = pf.validate_action(
        {"action": "BUY", "ticker": "NVDA", "entry": 100, "target_weight_pct": 10},
        prices={"NVDA": 100})
    assert not ok
    assert "position cap" in msg


# ── per-name weight cap ───────────────────────────────────────────────────────────

def test_position_weight_capped_at_the_configured_limit(pf):
    # Ask for 50% — the gate must cap the fill at MAX_POSITION_PCT of equity.
    ok, msg = _buy(pf, "AAPL", entry=100, weight=50.0)
    assert ok, msg
    pos = pf.state["positions"]["AAPL"]
    value = pos["shares"] * pos["entry"]
    assert value == pytest.approx(config.STARTING_CASH * config.MAX_POSITION_PCT / 100, rel=1e-6)


# ── weekly new-trade cap ──────────────────────────────────────────────────────────

def test_weekly_new_trade_cap(pf):
    for i in range(config.MAX_NEW_TRADES_PER_WEEK):
        ok, msg = _buy(pf, f"N{i}", entry=50, weight=8.0)
        assert ok, msg
    ok, msg = pf.validate_action(
        {"action": "BUY", "ticker": "ZZZ", "entry": 50, "target_weight_pct": 8.0},
        prices={"ZZZ": 50})
    assert not ok
    assert "weekly new-trade cap" in msg


# ── buy-on-held must use ADD ───────────────────────────────────────────────────────

def test_buy_on_held_rejected(pf):
    assert _buy(pf, "AAPL", entry=100)[0]
    ok, msg = pf.validate_action(
        {"action": "BUY", "ticker": "AAPL", "entry": 100, "target_weight_pct": 10},
        prices={"AAPL": 100})
    assert not ok
    assert "already held" in msg


# ── sector lockout ────────────────────────────────────────────────────────────────

def test_sector_lockout_after_two_losers(pf):
    sector = sector_of("NVDA")  # "Semis"
    pf.state["sector_fails"][sector] = config.SECTOR_FAIL_LIMIT
    ok, msg = pf.validate_action(
        {"action": "BUY", "ticker": "NVDA", "entry": 100, "target_weight_pct": 10},
        prices={"NVDA": 100})
    assert not ok
    assert "locked out" in msg


def test_losing_close_increments_sector_fail(pf):
    assert _buy(pf, "AMD", entry=100)[0]
    exits = pf.mark_to_market({"AMD": 90})  # -10% -> hard-stop exit, a loser
    assert exits and exits[0]["ticker"] == "AMD"
    assert pf.state["sector_fails"].get(sector_of("AMD"), 0) == 1


# ── -7% hard stop (mechanical) ────────────────────────────────────────────────────

def test_hard_stop_auto_cut(pf):
    assert _buy(pf, "TSLA", entry=100)[0]
    exits = pf.mark_to_market({"TSLA": 92})  # -8% <= -7%
    assert len(exits) == 1
    assert exits[0]["ticker"] == "TSLA"
    assert "hard stop" in exits[0]["reason"]
    assert "TSLA" not in pf.state["positions"]
    assert len(pf.state["closed_trades"]) == 1


def test_hard_stop_not_triggered_above_threshold(pf):
    assert _buy(pf, "MSFT", entry=100)[0]
    exits = pf.mark_to_market({"MSFT": 95})  # -5% -> still holding
    assert exits == []
    assert "MSFT" in pf.state["positions"]


# ── trailing stop tightening ──────────────────────────────────────────────────────

def test_trailing_stop_tightens_at_15_and_20(pf):
    assert _buy(pf, "NVDA", entry=100)[0]

    pf.mark_to_market({"NVDA": 118})  # +18% peak -> trail tightens to 7%
    assert pf.state["positions"]["NVDA"]["trail_pct"] == config.TRAIL_TIGHT_15

    pf.mark_to_market({"NVDA": 125})  # +25% peak -> trail tightens to 5%
    assert pf.state["positions"]["NVDA"]["trail_pct"] == config.TRAIL_TIGHT_20

    # 5% off the 125 peak = 118.75; a print below that must trigger the trailing exit.
    exits = pf.mark_to_market({"NVDA": 118})
    assert len(exits) == 1
    assert "trailing stop" in exits[0]["reason"]
    assert "NVDA" not in pf.state["positions"]


def test_trailing_stop_holds_within_band(pf):
    assert _buy(pf, "AVGO", entry=100)[0]
    pf.mark_to_market({"AVGO": 110})           # +10% peak, base 10% trail -> stop at 99
    exits = pf.mark_to_market({"AVGO": 104})   # still above 99 and above -7% entry stop
    assert exits == []
    assert "AVGO" in pf.state["positions"]


# ── SELL guards ───────────────────────────────────────────────────────────────────

def test_sell_unheld_rejected(pf):
    ok, msg = pf.validate_action({"action": "SELL", "ticker": "GOOGL"}, prices={"GOOGL": 100})
    assert not ok
    assert "not held" in msg


# ── the default weight when the analyst omits one ────────────────────────────────

def test_an_unsized_buy_falls_back_to_the_default_weight(pf):
    """The old fallback was a hardcoded 15%. At 8 positions that is a book the account
    cannot fund, so an unsized proposal has to fall back to the configured default."""
    ok, msg = pf.apply_action(
        {"action": "BUY", "ticker": "AAPL", "entry": 100, "stop": 93, "reason": "no weight given"},
        prices={"AAPL": 100})
    assert ok, msg
    pos = pf.state["positions"]["AAPL"]
    assert pos["shares"] * pos["entry"] == pytest.approx(
        config.STARTING_CASH * config.DEFAULT_POSITION_PCT / 100, rel=1e-6)


def test_a_full_book_at_the_default_weight_still_fits_the_account():
    """8 positions at the default must not need more cash than the account has."""
    assert config.MAX_POSITIONS * config.DEFAULT_POSITION_PCT <= 100
    assert config.DEFAULT_POSITION_PCT <= config.MAX_POSITION_PCT
