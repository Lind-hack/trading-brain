"""Every close must be visible to the caller, whatever closed it.

The live trade tape (signal-deck) and the journal's exit grading are both driven by the list of
exits a run produced. Mechanical stops always made it there, because mark_to_market() returns
them. A model-proposed SELL goes through apply_action(), whose (ok, msg) contract has nowhere to
put an exit record — so those closes silently reached neither. That is exactly what happened to
the CRM exit on 2026-07-25: the ledger closed it, the email mentioned it, and sd_trades stayed
empty.

Run:  python -m pytest tests/ -q
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from brain import config
from brain.portfolio import Portfolio


@pytest.fixture
def pf(tmp_path):
    return Portfolio(path=tmp_path / "PORTFOLIO.json", mirror=False)


def _open(pf, ticker="CRM", entry=100.0):
    ok, _ = pf.apply_action(
        {"action": "BUY", "ticker": ticker, "entry": entry, "target_weight_pct": 10,
         "reason": "test thesis", "trade_type": "SHORT_TERM", "confidence": 70,
         "indicators_used": ["RSI", "MA20"]},
        prices={ticker: entry})
    assert ok
    return pf.state["positions"][ticker]


def test_model_sell_is_recorded(pf):
    _open(pf)
    ok, msg = pf.apply_action({"action": "SELL", "ticker": "CRM", "entry": 96.0,
                               "reason": "thesis broke"}, prices={"CRM": 96.0})
    assert ok
    assert len(pf.exits_this_run) == 1
    e = pf.exits_this_run[0]
    assert e["ticker"] == "CRM"
    assert e["reason"] == "thesis broke"
    assert e["pnl_pct"] == pytest.approx(-4.0, abs=0.01)


def test_mechanical_stop_is_recorded(pf):
    _open(pf)
    # Below the -7% hard stop, so mark_to_market cuts it.
    exits = pf.mark_to_market({"CRM": 100 * (1 + (config.HARD_STOP_PCT - 1) / 100)})
    assert len(exits) == 1
    assert len(pf.exits_this_run) == 1
    assert pf.exits_this_run[0] is exits[0]


def test_both_paths_land_in_one_list(pf):
    _open(pf, "CRM", 100.0)
    _open(pf, "ORCL", 50.0)
    pf.mark_to_market({"CRM": 100 * (1 + (config.HARD_STOP_PCT - 1) / 100), "ORCL": 50.0})
    pf.apply_action({"action": "SELL", "ticker": "ORCL", "entry": 52.0}, prices={"ORCL": 52.0})
    assert [e["ticker"] for e in pf.exits_this_run] == ["CRM", "ORCL"]


def test_exit_record_carries_what_the_tape_shows(pf):
    """push_trade_close() reads these keys straight off the record."""
    _open(pf)
    pf.apply_action({"action": "SELL", "ticker": "CRM", "entry": 110.0}, prices={"CRM": 110.0})
    e = pf.exits_this_run[0]
    for k in ("ticker", "sector", "entry", "exit", "pnl_usd", "pnl_pct", "reason",
              "thesis", "trade_type", "confidence", "indicators_used", "opened", "closed"):
        assert k in e, f"exit record is missing {k}, which the dashboard row expects"
    assert e["entry"] == 100.0
    assert e["exit"] == 110.0
    assert e["trade_type"] == "SHORT_TERM"


def test_a_run_with_no_exits_stays_empty(pf):
    _open(pf)
    pf.mark_to_market({"CRM": 101.0})
    pf.apply_action({"action": "HOLD", "ticker": "CRM"}, prices={"CRM": 101.0})
    assert pf.exits_this_run == []
