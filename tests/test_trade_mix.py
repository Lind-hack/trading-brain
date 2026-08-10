"""The week has a shape now, not just a count.

Across 805 logged signals this book produced zero LONG_TERM trades. Most of that was the deep model
being dead in cron — `deep.fallback_analysis` hardcodes SHORT_TERM — but the rest would have
survived the fix: a 30-minute screener escalates on 30-minute setups and spends the weekly budget on
them before anything with a multi-month argument is looked at. These tests pin the counter that
makes the shortfall visible, the target it is measured against, and the fact that it stays advisory
— the harness must never fill a horizon slot on its own.

Run:  python -m pytest tests/test_trade_mix.py -q
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from brain import config, deep
from brain.portfolio import Portfolio


@pytest.fixture
def pf(tmp_path):
    return Portfolio(path=tmp_path / "PORTFOLIO.json")


def _buy(pf, ticker, trade_type, entry=100.0):
    return pf.apply_action({"action": "BUY", "ticker": ticker, "entry": entry,
                            "trade_type": trade_type, "target_weight_pct": 8.0,
                            "confidence": 70, "reason": "test"},
                           prices={ticker: entry})


# ── reading a horizon off model JSON ────────────────────────────────────────────

@pytest.mark.parametrize("raw,horizon", [
    ("SCALP", "SCALP"), (" scalp ", "SCALP"),
    ("SHORT_TERM", "SHORT_TERM"), ("short term", "SHORT_TERM"), ("swing", "SHORT_TERM"),
    ("LONG_TERM", "LONG_TERM"), ("long-term", "LONG_TERM"), ("Long Term", "LONG_TERM"),
])
def test_a_horizon_survives_the_models_formatting(raw, horizon):
    assert config.normalize_trade_type(raw) == horizon


@pytest.mark.parametrize("raw", [None, "", "  ", "position trade", 7, {}])
def test_an_unrecognised_horizon_is_not_quietly_assigned_one(raw):
    """Defaulting an untagged position into a horizon would credit a mix slot it never claimed."""
    assert config.normalize_trade_type(raw) is None


# ── the week's shape, counted off the ledger ────────────────────────────────────

def test_the_mix_counts_what_the_week_opened(pf):
    _buy(pf, "NVDA", "SCALP")
    _buy(pf, "AMD", "SCALP")
    _buy(pf, "MU", "LONG_TERM")
    assert pf.new_trades_this_week_by_type() == {"SCALP": 2, "LONG_TERM": 1}


def test_a_position_closed_the_same_week_still_counts(pf):
    """A scalp opened Tuesday and stopped out Tuesday afternoon spent a slot. Counting only open
    positions would report the week as emptier than it was and invite a sixth trade."""
    _buy(pf, "NVDA", "SCALP")
    pf.apply_action({"action": "SELL", "ticker": "NVDA", "reason": "done"}, prices={"NVDA": 103.0})
    assert pf.new_trades_this_week_by_type() == {"SCALP": 1}
    assert pf.state["positions"] == {}


def test_last_weeks_trades_do_not_count(pf):
    _buy(pf, "NVDA", "SCALP")
    old = datetime.now(config.UTC) - timedelta(days=9)
    pf.state["positions"]["NVDA"]["opened"] = old.isoformat()
    assert pf.new_trades_this_week_by_type() == {}


def test_an_untagged_position_is_counted_as_unspecified(pf):
    """It happened. Dropping it would make the horizon counts disagree with the trade count."""
    _buy(pf, "NVDA", None)
    assert pf.new_trades_this_week_by_type() == {"UNSPECIFIED": 1}


def test_a_broken_open_timestamp_does_not_crash_the_count(pf):
    _buy(pf, "NVDA", "SCALP")
    pf.state["positions"]["NVDA"]["opened"] = "not a date"
    assert pf.new_trades_this_week_by_type() == {}


def test_the_summary_carries_the_shape(pf):
    _buy(pf, "MU", "LONG_TERM")
    assert pf.summary({"MU": 100.0})["new_trades_by_type"] == {"LONG_TERM": 1}


# ── the gap the analyst is shown ────────────────────────────────────────────────

def test_an_empty_week_wants_the_whole_mix():
    assert config.STOCK_RULES.mix_gap({}) == {"SCALP": 2, "SHORT_TERM": 2, "LONG_TERM": 1}


def test_a_filled_horizon_drops_out_of_the_gap():
    """"You have taken enough scalps" is not something the analyst needs told — the weekly cap and
    the evidence bar handle over-trading. Only the shortfall is worth the packet space."""
    gap = config.STOCK_RULES.mix_gap({"SCALP": 2, "SHORT_TERM": 3})
    assert gap == {"LONG_TERM": 1}


def test_the_long_slot_is_what_a_scalp_heavy_week_still_owes():
    assert config.STOCK_RULES.mix_gap({"SCALP": 4, "UNSPECIFIED": 1}) == {"SHORT_TERM": 2,
                                                                          "LONG_TERM": 1}


def test_both_books_ask_for_a_long_horizon_trade():
    for rules in (config.STOCK_RULES, config.CRYPTO_RULES):
        assert rules.weekly_mix["LONG_TERM"] >= 1
        assert sum(rules.weekly_mix.values()) == rules.weekly_trade_target


def test_the_crypto_book_targets_five_a_week():
    """Lind asked for "5 trades on the stock market and 5 trades on the crypto side every week"."""
    assert config.CRYPTO_RULES.weekly_trade_target == 5
    assert config.STOCK_RULES.weekly_trade_target == 5


# ── what reaches the analyst ────────────────────────────────────────────────────

def _pace(summary):
    return deep.build_packet("cycle", {}, {}, {}, summary)["pace"]


def test_the_packet_shows_the_gap_and_flags_the_long_slot():
    pace = _pace({"book": "stock", "new_trades_this_week": 3,
                  "new_trades_by_type": {"SCALP": 2, "SHORT_TERM": 1}})
    assert pace["mix_gap"] == {"SHORT_TERM": 1, "LONG_TERM": 1}
    assert pace["long_term_open"] is True
    assert pace["mix_target"] == {"SCALP": 2, "SHORT_TERM": 2, "LONG_TERM": 1}


def test_a_week_with_its_long_slot_filled_stops_asking():
    pace = _pace({"book": "stock", "new_trades_this_week": 1,
                  "new_trades_by_type": {"LONG_TERM": 1}})
    assert pace["long_term_open"] is False
    assert "LONG_TERM" not in pace["mix_gap"]


def test_the_crypto_summary_is_paced_against_the_crypto_rules():
    """Showing the crypto analyst an equity target is the one number here it must not read wrong."""
    pace = _pace({"book": "crypto", "new_trades_this_week": 2, "new_trades_by_type": {}})
    assert pace["weekly_target"] == config.CRYPTO_RULES.weekly_trade_target
    assert pace["weekly_cap"] == config.CRYPTO_RULES.max_new_trades_per_week


def test_a_summary_with_no_book_is_read_as_the_equity_one():
    """--digest and --research build their packets without a portfolio; the pace block still has to
    render rather than raising on a missing key."""
    assert _pace(None)["mix_target"] == dict(config.STOCK_RULES.weekly_mix)
