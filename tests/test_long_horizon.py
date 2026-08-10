"""The long horizon: the second lock that kept LONG_TERM at zero, and the benchmark it is for.

`tests/test_scalp.py` pins the horizon the harness enforces downward — a tighter stop, a flat
trail, a clock. This file pins the one it enforces *outward*, and it exists because the rulebook's
own diagnosis of the empty long slot was only half the story.

CLAUDE.md blamed attention: a 30-minute screener escalates on 30-minute setups, so the week's
trade budget was spent before anything with a multi-month argument was examined. True, and
WEEKLY_MIX addresses it. But a LONG_TERM position that *did* get proposed and filled inherited the
swing book's −7% cut and a trail that tightens to 5% once the trade is +20% — so the better the
idea worked, the tighter the noose got. No holding of the 1–3 year kind Lind asked for survives
that. Zero LONG_TERM trades in 805 signals had two causes, and only one of them was upstream.

The second half of the file covers the number that says whether any of this worked: alpha against
the benchmark. "Beat the S&P 500" was the stated goal of the whole system and nothing computed it
— `week_return_pct` and `total_return_pct` are both absolute, and a +4% week against a +6% SPY
reads as a good one in every surface the book has.

Run:  python -m pytest tests/test_long_horizon.py -q
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from brain import config, journal, notify
from brain.portfolio import Portfolio


@pytest.fixture
def stock_pf(tmp_path):
    return Portfolio(path=tmp_path / "PORTFOLIO.json", rules=config.STOCK_RULES)


@pytest.fixture
def crypto_pf(tmp_path):
    return Portfolio(path=tmp_path / "PORTFOLIO_CRYPTO.json", rules=config.CRYPTO_RULES)


def _open(pf, ticker, entry, trade_type, stop=None, direction=None):
    action = {"action": "BUY", "ticker": ticker, "entry": entry, "stop": stop,
              "trade_type": trade_type, "reason": "test"}
    if direction:
        action["direction"] = direction
    ok, msg = pf.apply_action(action, prices={ticker: entry})
    assert ok, msg
    return pf.state["positions"][ticker]


# ── the stop band ───────────────────────────────────────────────────────────────

def test_long_term_survives_the_drawdown_that_stops_a_swing(stock_pf):
    """−10% closes a SHORT_TERM position and must not close a LONG_TERM one.

    This is the whole fix in one assertion. Both positions are down the same amount on the same
    tape; only the declared horizon differs.
    """
    _open(stock_pf, "AAPL", 100.0, "SHORT_TERM")
    _open(stock_pf, "MSFT", 100.0, "LONG_TERM")
    exits = stock_pf.mark_to_market({"AAPL": 90.0, "MSFT": 90.0})
    stopped = {e["ticker"] for e in exits}
    assert stopped == {"AAPL"}, f"expected only the swing trade cut, got {stopped}"
    assert "MSFT" in stock_pf.state["positions"]


def test_long_term_is_still_cut_at_its_own_band(stock_pf):
    """−20% is a floor, not an absence of one. A thesis that broke this far is still closed."""
    _open(stock_pf, "MSFT", 100.0, "LONG_TERM")
    assert not stock_pf.mark_to_market({"MSFT": 81.0}), "−19% is inside the band"
    exits = stock_pf.mark_to_market({"MSFT": 79.0})
    assert [e["ticker"] for e in exits] == ["MSFT"]
    assert exits[0]["reason"].startswith("hard stop")


def test_crypto_long_term_gets_the_venue_s_own_band(crypto_pf):
    """−20% is a disaster floor on a stock and an ordinary six weeks on a token."""
    _open(crypto_pf, "BTC-USD", 100.0, "LONG_TERM")
    assert not crypto_pf.mark_to_market({"BTC-USD": 70.0}), "−30% is inside the crypto band"
    assert crypto_pf.mark_to_market({"BTC-USD": 64.0}), "−36% is through it"


# ── the trailing stop that must not exist here ──────────────────────────────────

def test_long_term_hands_back_a_run_without_being_sold(stock_pf):
    """Peak +40%, give back most of it, still open — and still open *because* of the horizon.

    The swing ladder would have tightened to 5% at +20% and sold this near 133. Giving back a run
    is the price of holding something for a year; a trailing stop is the mechanism that makes
    that impossible.
    """
    _open(stock_pf, "MSFT", 100.0, "LONG_TERM")
    stock_pf.mark_to_market({"MSFT": 140.0})
    assert stock_pf.state["positions"]["MSFT"]["trail_pct"] is None
    assert not stock_pf.mark_to_market({"MSFT": 105.0}), "no trail should fire on a long-term hold"
    assert "MSFT" in stock_pf.state["positions"]


def test_swing_ladder_is_untouched(stock_pf):
    """The regression guard. A SHORT_TERM position must behave exactly as it did before."""
    _open(stock_pf, "AAPL", 100.0, "SHORT_TERM")
    stock_pf.mark_to_market({"AAPL": 125.0})           # +25% peak → trail tightens to 5%
    assert stock_pf.state["positions"]["AAPL"]["trail_pct"] == config.TRAIL_TIGHT_20
    exits = stock_pf.mark_to_market({"AAPL": 118.0})   # 5.6% off the high
    assert [e["ticker"] for e in exits] == ["AAPL"]
    assert "trailing stop" in exits[0]["reason"]


def test_untagged_position_keeps_swing_rules(stock_pf):
    """`UNSPECIFIED` must not fall through to the widest band available.

    An untagged position is the one case where a mistake here is silent and expensive: reading a
    missing horizon as long-term would hand a −20% stop to every signal that forgot the field.
    """
    _open(stock_pf, "AAPL", 100.0, None)
    assert stock_pf.mark_to_market({"AAPL": 92.0}), "an untagged position keeps the −7% cut"


@pytest.mark.parametrize("written", ["LONG_TERM", "long term", "Long-Term", " long "])
def test_horizon_is_matched_leniently(stock_pf, written):
    """The value arrives from model JSON. A spelling that fails to match gets swing stops
    silently, which is the exact failure the band was added to end."""
    _open(stock_pf, "MSFT", 100.0, written)
    assert not stock_pf.mark_to_market({"MSFT": 90.0}), f"{written!r} should read as long-term"


# ── the stop the model proposes ─────────────────────────────────────────────────

def test_a_tight_proposed_stop_is_widened_to_the_band(stock_pf):
    """The ledger cuts at the band, so a −7% stop attached to a two-year thesis would only ever
    reach Alpaca — where it would sell the position the ledger intended to hold. Same band on
    both sides, or it is not a band."""
    pos = _open(stock_pf, "MSFT", 100.0, "LONG_TERM", stop=93.0)
    assert pos["hard_stop"] == pytest.approx(80.0), "a tighter long-term stop is widened"


def test_a_short_s_stop_is_left_alone(stock_pf):
    """Both clamps read a stop as a price below entry. On a short that is through the fill."""
    pos = _open(stock_pf, "MSFT", 100.0, "LONG_TERM", stop=107.0, direction="SHORT")
    assert pos["hard_stop"] == pytest.approx(107.0), "a short's stop must not be pushed below entry"


def test_scalp_clamp_still_runs_the_other_way(stock_pf):
    """The scalp rule is the mirror image and must not have been inverted by the new branch:
    wider than the band is refused, tighter is the model's call."""
    assert _open(stock_pf, "AAPL", 100.0, "SCALP", stop=88.0)["hard_stop"] == pytest.approx(97.0)
    assert _open(stock_pf, "NVDA", 100.0, "SCALP", stop=99.0)["hard_stop"] == pytest.approx(99.0)


# ── alpha ───────────────────────────────────────────────────────────────────────

def test_benchmark_price_is_stamped_on_the_equity_curve(stock_pf):
    """Both legs come off one `prices` dict on one cycle, so they span the same window by
    construction rather than by a later best guess about which days to compare."""
    _open(stock_pf, "AAPL", 100.0, "SHORT_TERM")
    stock_pf.mark_to_market({"AAPL": 101.0, "SPY": 500.0})
    assert stock_pf.state["equity_curve"][-1]["bench"] == 500.0


def test_no_benchmark_quote_leaves_the_point_unstamped(stock_pf):
    """A cycle that could not price SPY records no benchmark rather than a stale or zero one."""
    _open(stock_pf, "AAPL", 100.0, "SHORT_TERM")
    stock_pf.mark_to_market({"AAPL": 101.0})
    assert "bench" not in stock_pf.state["equity_curve"][-1]


def _curve(*pairs):
    return [{"ts": f"2026-08-{3 + i:02d}T20:00:00+00:00", "equity": eq, "bench": b}
            for i, (eq, b) in enumerate(pairs)]


def test_alpha_is_measured_across_the_paired_points():
    """Book +10%, index +5% → beat by 5 points."""
    leg = journal._benchmark_leg(_curve((10000, 500.0), (11000, 525.0)), "SPY")
    assert leg["available"] and leg["beat"]
    assert leg["benchmark_return_pct"] == pytest.approx(5.0)
    assert leg["book_return_pct"] == pytest.approx(10.0)
    assert leg["alpha_pct"] == pytest.approx(5.0)


def test_a_green_week_that_lost_to_the_index_is_not_a_win():
    """The failure the whole field exists to catch: +4% reads as a good week until SPY did +6%."""
    leg = journal._benchmark_leg(_curve((10000, 500.0), (10400, 530.0)), "SPY")
    assert leg["available"] and not leg["beat"]
    assert leg["alpha_pct"] == pytest.approx(-2.0)


def test_one_stamped_point_is_not_a_flat_week():
    """A book that could not measure its benchmark has not tied with it."""
    leg = journal._benchmark_leg(_curve((10000, 500.0)), "SPY")
    assert leg["available"] is False
    assert "fewer than two" in leg["reason"]


def test_points_written_before_the_field_existed_are_skipped():
    """Every ledger on disk predates this. An old curve point is missing `bench`, not zeroed."""
    old = [{"ts": "2026-08-03T20:00:00+00:00", "equity": 10000}]
    leg = journal._benchmark_leg(old + _curve((10100, 500.0), (10300, 505.0)), "SPY")
    assert leg["available"] and leg["window_points"] == 2
    assert leg["book_return_pct"] == pytest.approx(1.98, abs=0.01), "measured from the stamped pair"


def test_a_book_with_no_benchmark_says_so():
    assert journal._benchmark_leg(_curve((10000, 500.0), (11000, 525.0)), None)["available"] is False


def test_recap_carries_the_benchmark_and_both_surfaces_render_it(stock_pf):
    """The stat reaches `stats.benchmark`, and neither the note nor the email invents a number
    for a week that could not be measured."""
    _open(stock_pf, "AAPL", 100.0, "SHORT_TERM")
    stock_pf.mark_to_market({"AAPL": 100.0, "SPY": 500.0})
    stock_pf.mark_to_market({"AAPL": 130.0, "SPY": 505.0})
    recap = journal.build_recap(stock_pf, "2026-01-01T00:00:00+00:00", prices={"AAPL": 130.0})
    leg = recap["stats"]["benchmark"]
    assert leg["available"] and leg["ticker"] == "SPY"
    assert leg["benchmark_return_pct"] == pytest.approx(1.0)
    assert "vs SPY" in journal.benchmark_line(recap["stats"])
    assert "pts" in notify._bench_html(recap["stats"])
    unmeasured = journal.benchmark_line({"benchmark": {"available": False, "reason": "no data"}})
    assert "not measurable" in unmeasured
    assert "not measurable" in notify._bench_html({"benchmark": {"available": False}})
