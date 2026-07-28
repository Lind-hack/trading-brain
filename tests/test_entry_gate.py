"""The entry gate: a good thesis at a bad price is a bad trade.

The week of 2026-07-27 is the whole reason this exists. Three theses that read fine on the news and
fine on the chart, all three bought above the prior 20-day high. Lind's read was "the news and
technical analysis were good, it was just that the position entry wasn't good."

The fourth fill that week was entered mid-range and is the only one closed — stopped out at -4.1%,
the worst of the four so far. So these four trades are not evidence that a range-top entry loses
more; four trades are not a sample. They are the specification for *where the gate fires*: it must
refuse the three bought above the high and must still fill the one bought at 41% of its range, or
it has not learned a rule, it has just stopped trading.

The first four tests below replay those four real fills through the gate. Everything after them is
the edge cases the specification implies.

Run:  python -m pytest tests/test_entry_gate.py -q
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from brain import config, entry
from brain.portfolio import Portfolio


def _ind(price, hi20, lo20, ma20, atr14):
    return {"price": price, "hi20": hi20, "lo20": lo20, "ma20": ma20, "atr14": atr14}


def _buy(px, direction="LONG", kind="BUY"):
    return {"action": kind, "ticker": "T", "entry": px, "direction": direction}


# The four fills, measured from brain-memory/PORTFOLIO.json on the VPS and yfinance daily bars
# truncated to the fill date. Kept as data so the numbers stay checkable.
JPM = _ind(357.72, hi20=356.20, lo20=327.33, ma20=340.10, atr14=7.38)
BAC = _ind(62.53, hi20=62.13, lo20=56.98, ma20=59.60, atr14=1.44)
CRM_LOSS = _ind(176.03, hi20=173.79, lo20=156.66, ma20=168.20, atr14=5.80)
CRM_MID = _ind(163.61, hi20=173.79, lo20=156.66, ma20=164.80, atr14=5.18)


# ── the four real trades ────────────────────────────────────────────────────────

def test_jpm_at_357_50_is_refused():
    """Bought at 104.5% of the 20-day range and 2.4 ATR above the mean."""
    ok, why, level = entry.check(_buy(357.50), JPM, config.STOCK_RULES)
    assert not ok
    assert "20-day range" in why
    assert level and level < 357.50


def test_bac_at_62_50_is_refused():
    ok, why, level = entry.check(_buy(62.50), BAC, config.STOCK_RULES)
    assert not ok and level < 62.50


def test_crm_at_174_is_refused_on_range_even_though_it_is_only_one_atr_out():
    """The trade that proves one test is not enough. CRM was +1.00 ATR — well inside the extension
    bar — and still bought at the top of its range. Drop the range test and this one fills again."""
    ok, why, level = entry.check(_buy(174.00), CRM_LOSS, config.STOCK_RULES)
    assert not ok
    assert "20-day range" in why


def test_the_mid_range_entry_still_fills():
    """CRM at 163.66 three days earlier: 41% of the range, 0.2 ATR *below* the mean. This one was
    stopped out at -4.1% and the gate still has to pass it — a price gate that refuses a mid-range
    entry because the trade went on to lose has not learned a rule, it has curve-fit one loss."""
    ok, why, _ = entry.check(_buy(163.66), CRM_MID, config.STOCK_RULES)
    assert ok, why


def test_the_only_crypto_entry_so_far_still_fills():
    """ETH at 1877 on 2026-07-28 — 63% of the range, +0.2 ATR. The crypto bars are looser than the
    equity ones, but this trade clears the *equity* bars too, so they are not fitted to it."""
    eth = _ind(1878.21, hi20=1953.41, lo20=1744.48, ma20=1866.0, atr14=48.0)
    assert entry.check(_buy(1877.0), eth, config.CRYPTO_RULES)[0]
    assert entry.check(_buy(1877.0), eth, config.STOCK_RULES)[0]


# ── what the rejection hands back ───────────────────────────────────────────────

def test_the_named_level_would_itself_pass():
    """A rejection that names an unbuyable price is just a second rejection. Whatever `level` says,
    feeding it straight back in has to clear every bar."""
    for ind in (JPM, BAC, CRM_LOSS):
        _, _, level = entry.check(_buy(999_999.0), ind, config.STOCK_RULES)
        ok, why, _ = entry.check(_buy(level), ind, config.STOCK_RULES)
        assert ok, f"{level} was offered as the passing price but {why}"


def test_the_level_satisfies_the_tighter_of_the_two_bars():
    """Computed across both tests rather than off whichever one happened to fire — a range-derived
    price that is still three ATRs extended has only moved the problem."""
    _, _, level = entry.check(_buy(357.50), JPM, config.STOCK_RULES)
    assert level <= JPM["lo20"] + 0.85 * (JPM["hi20"] - JPM["lo20"]) + 1e-9
    assert level <= JPM["ma20"] + 2.0 * JPM["atr14"] + 1e-9


# ── the crypto book runs the same rule at its own numbers ───────────────────────

def test_the_crypto_bars_are_looser_than_the_equity_ones():
    """A token holds the top of its range for weeks where an equity would have mean-reverted."""
    assert config.CRYPTO_RULES.entry_max_range_pos > config.STOCK_RULES.entry_max_range_pos
    assert config.CRYPTO_RULES.entry_max_ext_atr > config.STOCK_RULES.entry_max_ext_atr


def test_an_entry_between_the_two_bars_fills_on_crypto_and_not_on_equities():
    """88% of the range: past the equity bar of 0.85, inside the crypto bar of 0.90."""
    ind = _ind(88.0, hi20=100.0, lo20=0.0, ma20=80.0, atr14=20.0)
    assert not entry.check(_buy(88.0), ind, config.STOCK_RULES)[0]
    assert entry.check(_buy(88.0), ind, config.CRYPTO_RULES)[0]


# ── shorts are the mirror, not an exemption ─────────────────────────────────────

def test_selling_the_low_of_the_range_is_refused_the_same_way():
    ind = _ind(3.0, hi20=100.0, lo20=0.0, ma20=50.0, atr14=10.0)
    ok, why, level = entry.check(_buy(3.0, direction="SHORT"), ind, config.STOCK_RULES)
    assert not ok
    assert "too near the low" in why
    assert level > 3.0, "a short should be told to wait for a *higher* price"


def test_the_same_price_that_refuses_a_long_is_fine_for_a_short():
    ind = _ind(97.0, hi20=100.0, lo20=0.0, ma20=50.0, atr14=10.0)
    assert not entry.check(_buy(97.0, direction="LONG"), ind, config.STOCK_RULES)[0]
    assert entry.check(_buy(97.0, direction="SHORT"), ind, config.STOCK_RULES)[0]


# ── chasing the tape ────────────────────────────────────────────────────────────

def test_an_entry_quoted_above_the_last_print_is_refused():
    """A different mistake from an extended chart: this price was never on offer, and the fill is
    booked at it regardless — portfolio.py fills at `entry`, not at the market."""
    ind = _ind(50.0, hi20=100.0, lo20=0.0, ma20=50.0, atr14=10.0)
    ok, why, level = entry.check(_buy(51.0), ind, config.STOCK_RULES)
    assert not ok
    assert "chasing" in why
    assert level == pytest.approx(50.0)


def test_the_real_fills_are_nowhere_near_the_chase_bar():
    """Between 1.16% below and 0.03% above the tape. If this bar ever fires on ordinary behaviour
    it is mispriced, so the margin is worth pinning."""
    ind = _ind(100.0, hi20=200.0, lo20=0.0, ma20=100.0, atr14=20.0)
    for px in (98.84, 99.94, 99.95, 100.03):
        assert entry.check(_buy(px), ind, config.STOCK_RULES)[0], px


def test_a_fresher_tape_beats_the_snapshot():
    ind = _ind(50.0, hi20=100.0, lo20=0.0, ma20=50.0, atr14=10.0)
    assert entry.check(_buy(51.0), ind, config.STOCK_RULES, last_price=51.2)[0]


# ── what the gate must not do ───────────────────────────────────────────────────

def test_a_sell_is_never_price_gated():
    """Exits are risk management. Refusing one because the price is extended would trap the book in
    exactly the position it most needs to leave."""
    ok, _, _ = entry.check({"action": "SELL", "ticker": "T", "entry": 999.0}, JPM,
                           config.STOCK_RULES)
    assert ok


def test_missing_indicators_pass_rather_than_fail_closed():
    """Absent data is the normal state for a thinly-covered name. A gate that refuses on it stops
    the book trading and reports it as an entry problem, which is the more expensive failure."""
    assert entry.check(_buy(100.0), {}, config.STOCK_RULES)[0]
    assert entry.check(_buy(100.0), None, config.STOCK_RULES)[0]
    assert entry.check(_buy(100.0), {"hi20": None, "lo20": None}, config.STOCK_RULES)[0]


def test_a_flat_range_does_not_divide_by_zero():
    ind = _ind(10.0, hi20=10.0, lo20=10.0, ma20=10.0, atr14=0.0)
    assert entry.check(_buy(10.0), ind, config.STOCK_RULES)[0]


def test_an_action_with_no_entry_price_is_left_alone():
    """The harness fills those at the market, so there is no quoted price to grade."""
    ok, why, _ = entry.check({"action": "BUY", "ticker": "T"}, JPM, config.STOCK_RULES)
    assert ok and "no entry price" in why


def test_a_sub_cent_token_is_graded_on_the_ratio_not_the_rounded_price():
    """BONK-scale numbers. `range_pos` is a ratio, so it survives prices that a two-decimal round
    would flatten to zero — and the level handed back has to survive it too."""
    ind = _ind(0.0000032, hi20=0.0000033, lo20=0.0000020, ma20=0.0000025, atr14=0.0000003)
    ok, why, level = entry.check(_buy(0.0000032), ind, config.CRYPTO_RULES)
    assert not ok
    assert level and level > 0, "a stop-level of 0.0 is the bug config.round_price exists to stop"


# ── the gate inside the portfolio ───────────────────────────────────────────────

@pytest.fixture
def pf(tmp_path):
    return Portfolio(path=tmp_path / "P.json", rules=config.STOCK_RULES)


def _market(ind):
    return {"JPM": {"ticker": "JPM", "indicators": ind}}


def test_the_portfolio_refuses_the_jpm_fill_when_it_is_given_the_chart(pf):
    ok, msg = pf.apply_action({"action": "BUY", "ticker": "JPM", "entry": 357.50,
                               "direction": "LONG", "reason": "test"},
                              prices={"JPM": 357.72}, market=_market(JPM))
    assert not ok
    assert "wait for" in msg, msg
    assert "JPM" not in pf.state["positions"]


def test_the_same_fill_goes_through_at_the_price_the_gate_named(pf):
    _, msg = pf.validate_action({"action": "BUY", "ticker": "JPM", "entry": 357.50,
                                 "direction": "LONG"},
                                prices={"JPM": 357.72}, market=_market(JPM))
    level = float(msg.rsplit("wait for ", 1)[1].replace(",", ""))
    ok, msg = pf.apply_action({"action": "BUY", "ticker": "JPM", "entry": level,
                               "direction": "LONG", "reason": "test"},
                              prices={"JPM": level}, market=_market(JPM))
    assert ok, msg


def test_without_a_market_snapshot_every_other_gate_still_applies(pf):
    """The gate is additive. A caller that has no snapshot must not lose the position cap with it."""
    ok, _ = pf.apply_action({"action": "BUY", "ticker": "JPM", "entry": 357.50, "reason": "t"},
                            prices={"JPM": 357.50})
    assert ok
    pf.state["cash"] = 0.0
    ok, msg = pf.apply_action({"action": "BUY", "ticker": "BAC", "entry": 62.50, "reason": "t"},
                              prices={"BAC": 62.50})
    assert not ok and "insufficient cash" in msg


def test_the_cheaper_rejections_are_reported_before_the_entry_one(pf):
    """A locked-out sector should say so. Reporting "bad entry" on a trade that could never have
    filled anyway sends Lind chasing a pullback that buys him nothing."""
    pf.state["sector_fails"]["Banks"] = config.STOCK_RULES.sector_fail_limit
    ok, msg = pf.apply_action({"action": "BUY", "ticker": "JPM", "entry": 357.50, "reason": "t"},
                              prices={"JPM": 357.72}, market=_market(JPM))
    assert not ok and "locked out" in msg


def test_an_add_at_the_top_is_refused_too(pf):
    """Adding at the high is the same mistake with more size behind it."""
    ok, _ = pf.apply_action({"action": "BUY", "ticker": "JPM", "entry": 340.0, "reason": "t"},
                            prices={"JPM": 340.0}, market=_market(JPM))
    assert ok
    ok, msg = pf.apply_action({"action": "ADD", "ticker": "JPM", "entry": 357.50,
                               "target_weight_pct": 15, "reason": "t"},
                              prices={"JPM": 357.72}, market=_market(JPM))
    assert not ok and "20-day range" in msg


def test_an_exit_is_never_blocked_by_the_gate(pf):
    pf.apply_action({"action": "BUY", "ticker": "JPM", "entry": 340.0, "reason": "t"},
                    prices={"JPM": 340.0}, market=_market(JPM))
    ok, msg = pf.apply_action({"action": "SELL", "ticker": "JPM", "entry": 357.50,
                               "reason": "take the win"},
                              prices={"JPM": 357.72}, market=_market(JPM))
    assert ok, msg
