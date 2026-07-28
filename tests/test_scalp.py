"""Scalps: the horizon the harness enforces rather than merely labels.

`trade_type: "SCALP"` existed long before this file, but it was decoration — a scalp inherited the
swing book's stop, the swing book's trail and the swing book's unlimited hold, so a position the
model called a scalp could and did sit on the book for a week. These tests pin the three mechanics
that make the label mean something: a time stop that closes it, a tighter cut while it runs, and a
size cap it cannot talk its way past.

The other half of the file guards the direction nobody thinks to check: a SHORT_TERM or LONG_TERM
position must behave exactly as it did before any of this existed. A time stop that leaks onto a
swing trade would be far more expensive than one that never fired at all.

Run:  python -m pytest tests/test_scalp.py -q
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from brain import config, journal
from brain.portfolio import Portfolio, held_hours


@pytest.fixture
def stock_pf(tmp_path):
    return Portfolio(path=tmp_path / "PORTFOLIO.json", rules=config.STOCK_RULES)


@pytest.fixture
def crypto_pf(tmp_path):
    return Portfolio(path=tmp_path / "PORTFOLIO_CRYPTO.json", rules=config.CRYPTO_RULES)


def _open(pf, ticker, entry, trade_type, weight=None, stop=None):
    action = {"action": "BUY", "ticker": ticker, "entry": entry, "stop": stop,
              "trade_type": trade_type, "reason": "test"}
    if weight is not None:
        action["target_weight_pct"] = weight
    ok, msg = pf.apply_action(action, prices={ticker: entry})
    assert ok, msg
    return pf.state["positions"][ticker]


def _later(pf, ticker, hours):
    """A `now` that far past this position's entry."""
    opened = pf.state["positions"][ticker]["opened"]
    return datetime.fromisoformat(opened) + timedelta(hours=hours)


# ── the time stop ───────────────────────────────────────────────────────────────

def test_crypto_scalp_is_force_closed_at_24h(crypto_pf):
    _open(crypto_pf, "BTC-USD", 100.0, "SCALP")
    # 23h in and flat: still running.
    assert crypto_pf.mark_to_market({"BTC-USD": 100.5}, now=_later(crypto_pf, "BTC-USD", 23)) == []
    assert "BTC-USD" in crypto_pf.state["positions"]
    # 24h in: out, whatever the price is doing.
    exits = crypto_pf.mark_to_market({"BTC-USD": 100.5}, now=_later(crypto_pf, "BTC-USD", 24))
    assert len(exits) == 1
    assert exits[0]["exit_kind"] == "time_stop"
    assert "BTC-USD" not in crypto_pf.state["positions"]


def test_the_time_stop_closes_a_winner_too(crypto_pf):
    """The point of the horizon is that it is not conditional on the result."""
    _open(crypto_pf, "ETH-USD", 100.0, "SCALP")
    exits = crypto_pf.mark_to_market({"ETH-USD": 104.0}, now=_later(crypto_pf, "ETH-USD", 25))
    assert exits[0]["exit_kind"] == "time_stop"
    assert exits[0]["pnl_pct"] > 0


def test_equity_scalp_runs_on_the_session_length_window(stock_pf):
    _open(stock_pf, "NVDA", 100.0, "SCALP")
    assert stock_pf.mark_to_market({"NVDA": 100.2}, now=_later(stock_pf, "NVDA", 7)) == []
    exits = stock_pf.mark_to_market({"NVDA": 100.2}, now=_later(stock_pf, "NVDA", 8.5))
    assert exits and exits[0]["exit_kind"] == "time_stop"


def test_a_swing_trade_is_never_time_stopped(crypto_pf):
    """The regression that would cost the most: the clock leaking onto the other horizons."""
    _open(crypto_pf, "SOL-USD", 100.0, "SHORT_TERM")
    _open(crypto_pf, "BTC-USD", 100.0, "LONG_TERM")
    out = crypto_pf.mark_to_market({"SOL-USD": 100.0, "BTC-USD": 100.0},
                                   now=_later(crypto_pf, "SOL-USD", 24 * 30))
    assert out == []
    assert set(crypto_pf.state["positions"]) == {"SOL-USD", "BTC-USD"}


def test_an_untagged_position_is_not_time_stopped(crypto_pf):
    """No `trade_type` at all must mean the old behaviour, not the tightest new one."""
    ok, msg = crypto_pf.apply_action(
        {"action": "BUY", "ticker": "BTC-USD", "entry": 100.0, "reason": "test"},
        prices={"BTC-USD": 100.0})
    assert ok, msg
    assert crypto_pf.mark_to_market({"BTC-USD": 100.0},
                                    now=_later(crypto_pf, "BTC-USD", 500)) == []


def test_scalp_label_is_matched_loosely(crypto_pf):
    """It arrives from model JSON. `" scalp "` must not quietly become a swing trade."""
    _open(crypto_pf, "BTC-USD", 100.0, " scalp ")
    exits = crypto_pf.mark_to_market({"BTC-USD": 100.0}, now=_later(crypto_pf, "BTC-USD", 25))
    assert exits and exits[0]["exit_kind"] == "time_stop"


def test_a_price_stop_beats_the_clock_in_the_exit_reason(crypto_pf):
    """Both true at once: report the one that explains more."""
    _open(crypto_pf, "BTC-USD", 100.0, "SCALP")
    exits = crypto_pf.mark_to_market({"BTC-USD": 90.0}, now=_later(crypto_pf, "BTC-USD", 30))
    assert exits[0]["exit_kind"] == "hard_stop"


def test_a_broken_opened_timestamp_disables_the_clock_not_the_position(crypto_pf):
    _open(crypto_pf, "BTC-USD", 100.0, "SCALP")
    crypto_pf.state["positions"]["BTC-USD"]["opened"] = "not-a-date"
    assert held_hours(crypto_pf.state["positions"]["BTC-USD"]) is None
    assert crypto_pf.mark_to_market({"BTC-USD": 100.0}) == []
    assert "BTC-USD" in crypto_pf.state["positions"]


def test_the_deadline_written_at_entry_survives_a_rule_change(crypto_pf):
    """Re-tuning the window must not retroactively time out a position already on the book."""
    pos = _open(crypto_pf, "BTC-USD", 100.0, "SCALP")
    assert pos["scalp_max_hold_h"] == 24
    pos["scalp_max_hold_h"] = 72
    assert crypto_pf.mark_to_market({"BTC-USD": 100.0},
                                    now=_later(crypto_pf, "BTC-USD", 30)) == []


# ── the tighter risk a scalp runs under ─────────────────────────────────────────

def test_crypto_scalp_cuts_at_six_not_fifteen(crypto_pf):
    _open(crypto_pf, "BTC-USD", 100.0, "SCALP")
    exits = crypto_pf.mark_to_market({"BTC-USD": 93.0})     # -7%: noise for a swing, out for a scalp
    assert exits and exits[0]["exit_kind"] == "hard_stop"


def test_crypto_swing_rides_out_the_same_move(crypto_pf):
    _open(crypto_pf, "BTC-USD", 100.0, "SHORT_TERM")
    assert crypto_pf.mark_to_market({"BTC-USD": 93.0}) == []


def test_scalp_default_stop_sits_in_the_scalp_band(crypto_pf, stock_pf):
    assert _open(crypto_pf, "BTC-USD", 100.0, "SCALP")["hard_stop"] == 94.0
    assert _open(stock_pf, "NVDA", 100.0, "SCALP")["hard_stop"] == 97.0


def test_a_swing_width_stop_on_a_scalp_is_clamped_back(crypto_pf):
    """The model attaches the stop it was thinking in and then labels the idea SCALP."""
    pos = _open(crypto_pf, "BTC-USD", 100.0, "SCALP", stop=85.0)
    assert pos["hard_stop"] == 94.0


def test_a_tighter_stop_than_the_band_is_the_models_call(crypto_pf):
    assert _open(crypto_pf, "BTC-USD", 100.0, "SCALP", stop=98.0)["hard_stop"] == 98.0


def test_scalp_trails_one_tight_distance_and_skips_the_ladder(crypto_pf):
    """A +22% swing tightens to 10%. A scalp stays at 7% — it never earns the ladder."""
    _open(crypto_pf, "BTC-USD", 100.0, "SCALP")
    crypto_pf.mark_to_market({"BTC-USD": 122.0})
    assert crypto_pf.state["positions"]["BTC-USD"]["trail_pct"] == config.CRYPTO_RULES.scalp_trail_pct
    _open(crypto_pf, "ETH-USD", 100.0, "SHORT_TERM")
    crypto_pf.mark_to_market({"ETH-USD": 122.0})
    assert crypto_pf.state["positions"]["ETH-USD"]["trail_pct"] == config.CRYPTO_RULES.trail_tight_20


# ── size ────────────────────────────────────────────────────────────────────────

def test_scalp_is_sized_smaller_by_default(crypto_pf):
    pos = _open(crypto_pf, "BTC-USD", 100.0, "SCALP")
    weight = pos["shares"] * pos["entry"] * 100.0 / config.CRYPTO_RULES.starting_cash
    assert weight == pytest.approx(config.CRYPTO_RULES.scalp_position_pct, abs=0.1)


def test_a_scalp_cannot_ask_for_a_swing_sized_position(crypto_pf):
    pos = _open(crypto_pf, "BTC-USD", 100.0, "SCALP", weight=25)
    weight = pos["shares"] * pos["entry"] * 100.0 / config.CRYPTO_RULES.starting_cash
    assert weight == pytest.approx(config.CRYPTO_RULES.scalp_position_pct, abs=0.1)


def test_swing_sizing_is_untouched(crypto_pf, stock_pf):
    for pf, rules, t in ((crypto_pf, config.CRYPTO_RULES, "BTC-USD"),
                         (stock_pf, config.STOCK_RULES, "NVDA")):
        pos = _open(pf, t, 100.0, "SHORT_TERM")
        weight = pos["shares"] * pos["entry"] * 100.0 / rules.starting_cash
        assert weight == pytest.approx(rules.default_position_pct, abs=0.1)


# ── what the readers see ────────────────────────────────────────────────────────

def test_summary_carries_the_remaining_clock(crypto_pf):
    _open(crypto_pf, "BTC-USD", 100.0, "SCALP")
    _open(crypto_pf, "ETH-USD", 100.0, "LONG_TERM")
    rows = {p["ticker"]: p for p in crypto_pf.summary()["open_positions"]}
    assert 23 < rows["BTC-USD"]["scalp_hours_left"] <= 24
    assert rows["ETH-USD"]["scalp_hours_left"] is None


def test_journal_does_not_call_a_time_stopped_scalp_a_mislabel():
    """A 24h window closes at ~1.02 days, and the old grader flagged >1d as a bad label."""
    opened = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)
    rec = {"ticker": "BTC-USD", "trade_type": "SCALP", "exit_kind": "time_stop",
           "pnl_pct": 1.4, "confidence": 60,
           "opened": opened.isoformat(), "closed": (opened + timedelta(hours=24.5)).isoformat()}
    _verdict, _held, notes = journal._grade(rec)
    notes = " ".join(notes)
    assert "longer than a SCALP" not in notes
    assert "time stop" in notes


# ── the dated scalp-focus tilt ──────────────────────────────────────────────────
#
# Lind asked for intraday trades on a specific day (2026-07-28). The tilt therefore has to be a
# window, not a setting: a permanent scalp bias would turn a book meant to hold multi-week
# positions into a day-trading account, one reasonable-looking run at a time.

from brain import deep                                        # noqa: E402


def _focus(monkeypatch, value):
    monkeypatch.setattr(config, "SCALP_FOCUS_UNTIL", value, raising=False)


def test_focus_is_off_when_unset(monkeypatch):
    _focus(monkeypatch, "")
    assert config.scalp_focus_active() is False


def test_focus_is_on_up_to_and_including_the_last_day(monkeypatch):
    _focus(monkeypatch, "2026-07-28")
    on_the_day = datetime(2026, 7, 28, 15, 30, tzinfo=config.ET)
    assert config.scalp_focus_active(on_the_day) is True


def test_focus_expires_by_itself(monkeypatch):
    _focus(monkeypatch, "2026-07-28")
    next_morning = datetime(2026, 7, 29, 9, 31, tzinfo=config.ET)
    assert config.scalp_focus_active(next_morning) is False


def test_a_broken_date_turns_the_tilt_off_rather_than_crashing(monkeypatch):
    _focus(monkeypatch, "tomorrow")
    assert config.scalp_focus_active() is False


def test_an_active_window_reaches_the_prompt(monkeypatch):
    _focus(monkeypatch, "2026-07-28")
    monkeypatch.setattr(config, "scalp_focus_active", lambda *_a: True, raising=False)
    text = deep.build_prompt("cycle", {})
    assert "SCALP FOCUS IS ON" in text
    # The tilt changes what is looked at first, never the evidence bar.
    assert "does NOT change what qualifies" in text.replace("It ", "")


def test_no_window_means_no_note(monkeypatch):
    monkeypatch.setattr(config, "scalp_focus_active", lambda *_a: False, raising=False)
    assert "SCALP FOCUS IS ON" not in deep.build_prompt("cycle", {})


def test_the_tilt_reaches_the_crypto_book_too(monkeypatch):
    monkeypatch.setattr(config, "scalp_focus_active", lambda *_a: True, raising=False)
    text = deep.build_prompt("crypto", {})
    assert "SCALP FOCUS IS ON" in text and "THIS RUN IS THE CRYPTO BOOK" in text
