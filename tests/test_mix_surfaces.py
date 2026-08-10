"""The week's shape has to be visible, not just computed.

`mix_gap` reaches the analyst through the packet. These tests cover the other direction — what Lind
reads. A week that opened five trades is on pace by the count and can be five scalps, so every
surface that prints the count now prints the shape beside it, zeros included.

Run:  python -m pytest tests/test_mix_surfaces.py -q
"""
import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from brain import config, journal, notify
from brain.portfolio import Portfolio

NOW = datetime(2026, 7, 31, 16, 15, tzinfo=config.ET)


@pytest.fixture
def pf(tmp_path):
    return Portfolio(path=tmp_path / "PORTFOLIO.json")


def _buy(pf, ticker, trade_type):
    pf.apply_action({"action": "BUY", "ticker": ticker, "entry": 100.0,
                     "trade_type": trade_type, "target_weight_pct": 8.0,
                     "confidence": 70, "reason": "test"}, prices={ticker: 100.0})


# ── the recap's own numbers ─────────────────────────────────────────────────────

def test_the_recap_counts_the_week_by_horizon(pf):
    _buy(pf, "NVDA", "SCALP")
    _buy(pf, "AMD", "SHORT_TERM")
    st = journal.build_recap(pf, "2026-01-01T00:00:00", prices={"NVDA": 100.0,
                                                               "AMD": 100.0})["stats"]
    assert st["mix_opened"] == {"SCALP": 1, "SHORT_TERM": 1}
    assert st["mix_target"] == {"SCALP": 2, "SHORT_TERM": 2, "LONG_TERM": 1}
    assert st["mix_gap"]["LONG_TERM"] == 1


def test_the_recap_is_graded_against_its_own_books_shape(pf, tmp_path):
    """The crypto recap wants the crypto mix. Reading the equity one would grade a book of five
    against a target it was never given."""
    cpf = Portfolio(path=tmp_path / "PORTFOLIO_CRYPTO.json", rules=config.CRYPTO_RULES)
    st = journal.build_recap(cpf, "2026-01-01T00:00:00")["stats"]
    assert st["mix_target"] == dict(config.CRYPTO_RULES.weekly_mix)
    assert st["weekly_trade_target"] == 5


def test_an_untagged_open_is_not_credited_to_a_horizon(pf):
    _buy(pf, "NVDA", None)
    st = journal.build_recap(pf, "2026-01-01T00:00:00", prices={"NVDA": 100.0})["stats"]
    assert st["mix_opened"] == {"UNSPECIFIED": 1}
    assert st["mix_gap"] == {"SCALP": 2, "SHORT_TERM": 2, "LONG_TERM": 1}


# ── how it prints ───────────────────────────────────────────────────────────────

def test_the_shape_prints_its_zeros():
    """The horizon that never happened is the one worth showing, and it has no row of its own."""
    assert notify._mix_parts({"SCALP": 2}) == "2 scalp · 0 swing · 0 long"


def test_an_untagged_trade_is_named_rather_than_folded_in():
    assert "1 untagged" in notify._mix_parts({"SCALP": 1, "UNSPECIFIED": 1})


def test_the_long_slot_ambers_until_it_is_filled():
    open_slot = notify._mix_html({"mix_opened": {"SCALP": 2, "SHORT_TERM": 2},
                                  "mix_target": dict(config.WEEKLY_MIX)})
    filled = notify._mix_html({"mix_opened": {"SCALP": 2, "LONG_TERM": 1},
                               "mix_target": dict(config.WEEKLY_MIX)})
    assert "#f59e0b" in open_slot and "#22c55e" not in open_slot
    assert "#22c55e" in filled


def _weekly(stats, crypto=None):
    recap = {"week_label": "week of 2026-07-27", "stats": stats, "closed": [], "open": [],
             "gradings": [], "analytics": {}, "changes": [], "narrative": "flat week"}
    if crypto:
        recap["crypto"] = crypto
    return notify.build_weekly_email(recap, NOW)


def test_the_friday_recap_shows_the_shape_in_both_renderings():
    _, text, html = _weekly({"n_opened": 4, "weekly_trade_target": 5,
                             "mix_opened": {"SCALP": 3, "SHORT_TERM": 1},
                             "mix_target": dict(config.WEEKLY_MIX)})
    assert "3 scalp · 1 swing · 0 long" in text
    assert "wants 2 scalp · 2 swing · 1 long" in text
    assert "Trade mix" in " ".join(html.split())


def test_the_crypto_card_carries_its_own_shape():
    crypto = {"stats": {"n_opened": 2, "weekly_trade_target": 5, "week_return_pct": 1.0,
                        "equity_start": 10000, "equity_end": 10100, "realized_usd": 100,
                        "n_closed": 0, "n_open": 2,
                        "mix_opened": {"SCALP": 2},
                        "mix_target": dict(config.CRYPTO_WEEKLY_MIX)},
              "closed": []}
    _, text, html = _weekly({"n_opened": 0, "weekly_trade_target": 5}, crypto=crypto)
    assert "2 scalp · 0 swing · 0 long" in text
    assert " ".join(html.split()).count("Trade mix") == 2


def test_a_recap_written_before_the_mix_existed_still_renders():
    """Older `brain-memory` recaps carry no mix keys at all. The Friday email must not crash on
    one — the whole point of the weekly run is that it goes out."""
    _, text, html = _weekly({"n_opened": 3, "weekly_trade_target": 5})
    assert "0 scalp · 0 swing · 0 long" in text
    assert html


def test_the_cycle_card_shows_the_shape_once_the_week_has_one():
    psum = {"equity": 10000, "cash": 5000, "n_open": 1, "new_trades_this_week": 2,
            "new_trades_by_type": {"SCALP": 2}, "open_positions": []}
    assert "(2 scalp · 0 swing · 0 long)" in notify._portfolio_card(psum)


def test_an_empty_week_does_not_clutter_the_cycle_card():
    psum = {"equity": 10000, "cash": 5000, "n_open": 0, "new_trades_this_week": 0,
            "new_trades_by_type": {}, "open_positions": []}
    assert "scalp" not in notify._portfolio_card(psum)
