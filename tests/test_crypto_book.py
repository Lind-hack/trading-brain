"""The crypto book: a second paper account with its own gates, its own ledger, its own clock.

Two things are being protected here. The first is the crypto side working at all — wider stops,
its own position caps, its own sector map, a venue that never shuts. The second matters more:
**the equity book must not have moved an inch.** Every gate the stock track ran under this morning
still has to hold exactly, because the RuleSet refactor touched the same code path. Half the tests
below exist purely to prove nothing was loosened on the way past.

Run:  python -m pytest tests/ -q
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from brain import config, market_hours as mh
from brain.portfolio import Portfolio


@pytest.fixture
def stock_pf(tmp_path):
    return Portfolio(path=tmp_path / "PORTFOLIO.json", rules=config.STOCK_RULES)


@pytest.fixture
def crypto_pf(tmp_path):
    return Portfolio(path=tmp_path / "PORTFOLIO_CRYPTO.json", rules=config.CRYPTO_RULES)


def _buy(pf, ticker, entry, weight=None, stop=None):
    action = {"action": "BUY", "ticker": ticker, "entry": entry, "stop": stop, "reason": "test"}
    if weight is not None:
        action["target_weight_pct"] = weight
    return pf.apply_action(action, prices={ticker: entry})


# ── the equity book is unchanged ────────────────────────────────────────────────

def test_stock_rules_still_carry_the_shipped_gate_numbers():
    r = config.STOCK_RULES
    assert (r.max_positions, r.max_position_pct, r.max_new_trades_per_week) == (8, 15.0, 6)
    assert (r.hard_stop_pct, r.trail_base_pct) == (-7.0, 10.0)
    assert r.always_open is False


def test_default_portfolio_is_still_the_stock_book(tmp_path):
    """No `rules=` must mean the equity rules. Every existing caller relies on this."""
    pf = Portfolio(path=tmp_path / "P.json")
    assert pf.rules is config.STOCK_RULES
    assert pf.state["starting_cash"] == config.STARTING_CASH


def test_stock_book_still_stops_out_at_minus_seven(stock_pf):
    _buy(stock_pf, "NVDA", 100.0)
    fired = stock_pf.mark_to_market({"NVDA": 92.0})    # -8%
    assert any("NVDA" in str(f) for f in fired), fired
    assert "NVDA" not in stock_pf.state["positions"]


def test_stock_book_survives_a_move_that_would_stop_out_crypto(stock_pf):
    """-6% must NOT close an equity position. Guards against the crypto stop leaking across."""
    _buy(stock_pf, "AAPL", 100.0)
    stock_pf.mark_to_market({"AAPL": 94.0})
    assert "AAPL" in stock_pf.state["positions"]


# ── the crypto book stands on its own ───────────────────────────────────────────

def test_crypto_rules_are_re_derived_not_inherited():
    r = config.CRYPTO_RULES
    assert r.always_open is True
    assert r.hard_stop_pct == -15.0                    # not the equity -7
    assert r.max_position_pct == 25.0                  # not the equity 15
    assert r.ledger != config.STOCK_RULES.ledger       # a separate file, so separate money


def test_crypto_universe_uses_the_working_sui_symbol():
    """SUI-USD resolves to nothing on Yahoo; the CoinMarketCap-id form is the live one."""
    assert "SUI20947-USD" in config.CRYPTO_TICKERS
    assert "SUI-USD" not in config.CRYPTO_TICKERS
    assert set(config.CRYPTO_TICKERS) == set(config.CRYPTO_SECTORS)


def test_crypto_position_rides_out_a_drop_that_would_stop_out_a_stock(crypto_pf):
    """The whole reason for a separate book: -8% on BTC is a Tuesday, not an exit."""
    _buy(crypto_pf, "BTC-USD", 100.0)
    crypto_pf.mark_to_market({"BTC-USD": 92.0})
    assert "BTC-USD" in crypto_pf.state["positions"]


def test_crypto_position_still_stops_out_eventually(crypto_pf):
    _buy(crypto_pf, "BTC-USD", 100.0)
    crypto_pf.mark_to_market({"BTC-USD": 84.0})           # -16%, past the -15 gate
    assert "BTC-USD" not in crypto_pf.state["positions"]


def test_crypto_default_stop_is_placed_at_the_crypto_level(crypto_pf):
    _buy(crypto_pf, "ETH-USD", 100.0)
    assert crypto_pf.state["positions"]["ETH-USD"]["hard_stop"] == pytest.approx(85.0)


def test_crypto_weight_cap_is_twenty_five_not_fifteen(crypto_pf):
    _buy(crypto_pf, "BTC-USD", 100.0, weight=40)       # asks for more than the cap
    held = crypto_pf.state["positions"]["BTC-USD"]
    weight_pct = held["shares"] * held["entry"] * 100.0 / config.CRYPTO_RULES.starting_cash
    assert weight_pct == pytest.approx(25.0, abs=0.1)


def test_crypto_position_cap_is_the_size_of_the_universe(crypto_pf):
    for t in config.CRYPTO_TICKERS:
        ok, msg = _buy(crypto_pf, t, 100.0)
        assert ok, msg
    ok, msg = _buy(crypto_pf, "DOGE-USD", 100.0)
    assert not ok and "cap" in msg


# ── the two books cannot reach each other ───────────────────────────────────────

def test_a_stock_sector_lockout_does_not_touch_the_crypto_book(stock_pf, crypto_pf):
    """Two losing semis must not stop the crypto book buying BTC. Separate ledgers, separate fails."""
    for t in ("NVDA", "AMD"):          # both map to "Semis" in STOCK_SECTORS
        _buy(stock_pf, t, 100.0)
        stock_pf._close(t, 90.0, "test loss")
    assert stock_pf.state["sector_fails"]["Semis"] >= config.STOCK_RULES.sector_fail_limit
    assert crypto_pf.state["sector_fails"] == {}
    ok, msg = _buy(crypto_pf, "BTC-USD", 100.0)
    assert ok, msg


def test_each_token_is_its_own_sector(crypto_pf):
    """All three labelled 'Crypto' would mean two losers anywhere shut the entire book."""
    assert config.CRYPTO_RULES.sector_of("BTC-USD") == "BTC"
    assert config.CRYPTO_RULES.sector_of("ETH-USD") == "ETH"
    _buy(crypto_pf, "BTC-USD", 100.0)
    crypto_pf._close("BTC-USD", 80.0, "test loss")
    _buy(crypto_pf, "BTC-USD", 100.0)
    crypto_pf._close("BTC-USD", 80.0, "test loss")
    ok, msg = _buy(crypto_pf, "ETH-USD", 100.0)
    assert ok, f"a BTC lockout must not block ETH: {msg}"


def test_rules_lookup_fails_loudly_on_a_typo():
    """A silent fall back to the stock rules would run crypto on a -7% stop."""
    assert config.rules_for("crypto") is config.CRYPTO_RULES
    with pytest.raises(ValueError):
        config.rules_for("Crypto")


# ── the crypto clock ────────────────────────────────────────────────────────────

def _utc(y, m, d, hh, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc)


def test_crypto_cycle_runs_on_a_saturday_night():
    allowed, why = mh.crypto_gate("cycle", _utc(2026, 8, 1, 3, 15))    # Sat 03:15 UTC
    assert allowed, why


def test_crypto_cycle_runs_on_christmas():
    assert mh.crypto_gate("cycle", _utc(2026, 12, 25, 15, 0))[0]


def test_the_equity_gate_is_still_shut_at_those_same_moments():
    """The crypto track must not have loosened the stock track on its way in."""
    assert not mh.gate("cycle", _utc(2026, 8, 1, 3, 15))[0]            # Saturday
    assert not mh.gate("cycle", _utc(2026, 12, 25, 15, 0))[0]          # Christmas


def test_crypto_entries_are_allowed_at_any_hour():
    assert mh.crypto_entries_allowed(_utc(2026, 8, 2, 4, 0))           # Sunday 04:00 UTC
    assert not mh.entries_allowed(_utc(2026, 8, 2, 4, 0))


def test_crypto_daily_anchor_fires_just_after_the_utc_roll():
    assert mh.crypto_gate("daily", _utc(2026, 7, 28, 0, 20))[0]
    assert not mh.crypto_gate("daily", _utc(2026, 7, 28, 9, 0))[0]


def test_crypto_weekly_recap_waits_for_the_monday_utc_roll():
    """Not Friday's close — that would drop Saturday and Sunday from the recap."""
    assert not mh.crypto_gate("weekly", _utc(2026, 7, 31, 20, 30))[0]  # Fri, after the NYSE close
    assert not mh.crypto_gate("weekly", _utc(2026, 8, 2, 0, 30))[0]    # Sun
    assert mh.crypto_gate("weekly", _utc(2026, 8, 3, 0, 30))[0]        # Mon 00:30 UTC
    assert not mh.crypto_gate("weekly", _utc(2026, 8, 3, 9, 0))[0]     # Mon, window gone


def test_crypto_week_is_always_seven_days_long():
    assert mh.crypto_days_left_this_week(_utc(2026, 8, 3, 1, 0)) == 7  # Monday
    assert mh.crypto_days_left_this_week(_utc(2026, 8, 9, 1, 0)) == 1  # Sunday


# ── venue dispatch: the screener bar follows the symbol ─────────────────────────

def test_is_crypto_recognises_the_venue_from_the_symbol():
    assert config.is_crypto("BTC-USD") and config.is_crypto("SUI20947-USD")
    for equity in ("NVDA", "^VIX", "ES=F", "DX-Y.NYB", "SPY"):
        assert not config.is_crypto(equity), equity


def test_thresholds_are_picked_by_ticker_not_by_a_passed_flag():
    assert config.thresholds_for("BTC-USD") is config.CRYPTO_SCREEN
    assert config.thresholds_for("NVDA") is config.STOCK_SCREEN
    assert config.CRYPTO_SCREEN.rsi_hot > config.STOCK_SCREEN.rsi_hot
    assert config.CRYPTO_SCREEN.gap_pct > config.STOCK_SCREEN.gap_pct


def test_an_rsi_that_flags_a_stock_is_a_quiet_tuesday_for_a_token():
    """72 is the whole reason the bands were widened: hot on equities, ordinary on a token."""
    from brain import screen
    def snap(ticker):
        return {"ticker": ticker, "indicators": {"rsi14_d": 72, "vol_vs_avg": 1.0}, "patterns": []}
    assert screen._score_ticker(snap("NVDA"))[0] == 1
    assert screen._score_ticker(snap("BTC-USD"))[0] == 0


def test_label_makes_the_sui_symbol_readable():
    assert config.label_for("SUI20947-USD") == "SUI"
    assert config.label_for("NVDA") == "NVDA"      # unmapped symbols pass through


# ── news: the crypto track's sources are its own ────────────────────────────────

def test_crypto_first_hand_desks_outrank_the_farms():
    from brain import news_quality as nq
    assert nq.source_tier("CoinDesk") == 1 and nq.source_tier("The Block") == 1
    assert nq.source_tier("Bitcoinist") == 3 and nq.source_tier("U.Today") == 3
    assert nq.source_tier("Reuters") == 1       # equity tiers untouched


def test_crypto_clickbait_is_filtered_but_real_reporting_is_not():
    from brain import news_quality as nq
    assert nq.farm_reason("Bitcoin Price Prediction: BTC to $200K?")
    assert nq.farm_reason("3 Coins Under $1 Including Sui") == "listicle"
    assert nq.farm_reason("SUI could hit $5 this cycle") == "price-target-bait"
    assert nq.farm_reason("Bitcoin ETFs post third straight weekly inflows") is None
    # A token unlock is a real supply event, not a listicle — the lookahead exists for this.
    assert nq.farm_reason("SUI faces pressure as 13.72M tokens unlock this week") is None


def test_a_token_gets_no_fundamentals_block_at_all(monkeypatch):
    """Absent, not zeroed. A zeroed block reads as a company with no revenue, which is a lie."""
    import pandas as pd
    from brain import collect

    monkeypatch.setattr(collect, "fetch_intraday", lambda *a, **k: pd.DataFrame())
    monkeypatch.setattr(collect, "daily_bars", lambda *a, **k: pd.DataFrame())
    monkeypatch.setattr(collect, "indicator_snapshot", lambda *a, **k: {"price": 1.0})
    monkeypatch.setattr(collect, "detect_patterns", lambda *a, **k: [])
    monkeypatch.setattr(collect, "finnhub_fundamentals",
                        lambda *a, **k: pytest.fail("no fundamentals exist for a token"))
    called = {}
    monkeypatch.setattr(collect, "finnhub_news",
                        lambda *a, **k: pytest.fail("Finnhub company news does not cover tokens"))
    monkeypatch.setattr(collect, "google_news",
                        lambda q, **k: called.setdefault("q", q) or [])

    snap = collect.collect_ticker("BTC-USD", with_news=True, with_fundamentals=True)
    assert "fundamentals" not in snap
    assert "Bitcoin" in called["q"]
