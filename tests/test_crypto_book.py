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


def test_crypto_universe_uses_the_working_yahoo_symbols():
    """The symbols Yahoo disambiguates with a CoinMarketCap id, which is where this silently breaks.

    `SUI-USD` resolves to nothing at all; the others resolve to something, which is worse — a run
    that "works" on an unrelated listing. Anyone tidying these back to the plain form drops the
    token from every cycle, and the only symptom is a name that stops appearing in emails.
    """
    for plain, live in (("SUI-USD", "SUI20947-USD"), ("UNI-USD", "UNI7083-USD"),
                        ("PEPE-USD", "PEPE24478-USD"), ("TRUMP-USD", "TRUMP35336-USD"),
                        ("JUP-USD", "JUP29210-USD")):
        assert live in config.CRYPTO_TICKERS
        assert plain not in config.CRYPTO_TICKERS


def test_the_jupiter_impostor_is_the_one_the_name_check_cannot_catch():
    """`JUP-USD` resolves, and its `shortName` is "Jupiter USD" — the same string the real one
    carries. The Ondo and Polygon near-misses were caught by reading the name; this pair cannot be.
    It printed $0.00026 against BingX's $0.183 on 2026-07-28, so price is the only tell, and the
    plain form is the one a tidy-up would reach for."""
    assert "JUP29210-USD" in config.CRYPTO_TICKERS
    assert config.CRYPTO_LABELS["JUP29210-USD"] == "JUP"


def test_the_four_crypto_maps_cannot_drift_apart():
    """They are derived from one table now. This is what that table is for."""
    assert set(config.CRYPTO_TICKERS) == set(config.CRYPTO_SECTORS)
    assert set(config.CRYPTO_TICKERS) == set(config.CRYPTO_LABELS)
    assert set(config.CRYPTO_TICKERS) == set(config.CRYPTO_NEWS_QUERIES)
    assert len(config.CRYPTO_TICKERS) == len(set(config.CRYPTO_TICKERS))


def test_every_requested_sector_is_actually_populated():
    """Lind asked for meme / DeFi / liquid staking / DePIN by name. A sector with no token in it is
    a label, not exposure — and the failure is invisible, since nothing errors, the book just never
    trades that branch."""
    sectors = set(config.CRYPTO_SECTORS.values())
    assert {"Meme", "DeFi", "LST", "DePIN"} <= sectors
    # Solana-native coverage specifically: SOL itself plus the branches that live on it.
    for t in ("SOL-USD", "BONK-USD", "WIF-USD", "TRUMP35336-USD", "RENDER-USD",
              "JUP29210-USD", "JTO-USD", "PYTH-USD"):
        assert t in config.CRYPTO_TICKERS


def test_a_token_alpaca_cannot_fill_is_still_in_the_universe():
    """JUP, JTO and PYTH were cut once for being unlisted on Alpaca. Wrong test: Lind trades this
    book on BingX and Alpaca is only the paper mirror, so an unfillable name still has to be
    screened, analysed and emailed. `broker.submit` is the *only* place a listing may be checked."""
    for t in ("JUP29210-USD", "JTO-USD", "PYTH-USD"):
        assert t in config.CRYPTO_TICKERS
        assert t in config.CRYPTO_NEWS_QUERIES


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


def test_a_coin_dearer_than_the_whole_slot_is_still_buyable(crypto_pf):
    """The bug the first live crypto cycle found, 2026-07-27 23:18 UTC.

    BTC costs ~$65k a coin against a $10k book, so *every* legal weight buys less than one unit.
    The equity whole-share floor rejected that as "insufficient cash for BTC-USD ($10000)" and the
    crypto book could never have opened BTC at all — nor ETH, which was the trade it actually
    refused. Crypto is bought fractionally; the floor does not belong on this venue.
    """
    ok, msg = _buy(crypto_pf, "BTC-USD", 65_000.0, weight=10)
    assert ok, msg
    held = crypto_pf.state["positions"]["BTC-USD"]
    assert held["shares"] == pytest.approx(1000.0 / 65_000.0, rel=1e-3)


def test_the_rejected_eth_trade_now_goes_through(crypto_pf):
    """The exact refused signal: ETH-USD LONG, entry 1888.42, SHORT_TERM at a 10% weight."""
    ok, msg = _buy(crypto_pf, "ETH-USD", 1888.42, weight=10)
    assert ok, msg
    assert crypto_pf.state["positions"]["ETH-USD"]["shares"] == pytest.approx(0.5295, abs=1e-3)


def test_an_empty_crypto_book_still_refuses_to_buy(crypto_pf):
    """Fractional sizing must not turn a broke book into an infinitely divisible one."""
    crypto_pf.state["cash"] = 0.0
    ok, msg = _buy(crypto_pf, "BTC-USD", 65_000.0, weight=10)
    assert not ok and "insufficient cash" in msg


def test_a_stock_too_dear_for_the_slot_is_still_rejected(stock_pf):
    """The equity book keeps the whole-share floor — this change must not have reached it."""
    ok, msg = _buy(stock_pf, "BRK-A", 700_000.0, weight=10)
    assert not ok and "insufficient cash" in msg


def test_crypto_position_cap_is_eight_not_the_size_of_the_universe(crypto_pf):
    """The cap used to be `len(CRYPTO_TICKERS)`, which was honest at four tokens and became a lie at
    eighteen — the $10k book cannot fund eighteen positions at any legal weight. Small weights here so
    cash is not what stops it: the ninth buy must be refused by the position cap itself."""
    for t in config.CRYPTO_TICKERS[:8]:
        ok, msg = _buy(crypto_pf, t, 100.0, weight=5)
        assert ok, msg
    ok, msg = _buy(crypto_pf, config.CRYPTO_TICKERS[8], 100.0, weight=5)
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


def test_the_sector_brake_is_a_brake_now_rather_than_a_shutdown(crypto_pf):
    """Every token used to be its own sector, because at four tokens a shared "Crypto" label meant
    two losers anywhere locked the entire book. That was a kill switch wearing a brake's name.

    Real sectors only work above a certain size, and eighteen tokens across six sectors is above it:
    two losing meme trades stop meme trades, and DeFi carries on. The BTC/ETH pair sharing "Major"
    is deliberate too — after two losing majors, "buy the other major" is the same trade."""
    assert config.CRYPTO_RULES.sector_of("BONK-USD") == "Meme"
    assert config.CRYPTO_RULES.sector_of("AAVE-USD") == "DeFi"
    for t in ("BONK-USD", "WIF-USD"):
        _buy(crypto_pf, t, 100.0, weight=5)
        crypto_pf._close(t, 80.0, "test loss")
    ok, msg = _buy(crypto_pf, "DOGE-USD", 100.0, weight=5)
    assert not ok and "sector" in msg.lower(), f"two meme losers must stop a third: {msg}"
    ok, msg = _buy(crypto_pf, "AAVE-USD", 100.0, weight=5)
    assert ok, f"a meme lockout must not block DeFi: {msg}"


# ── sub-cent tokens ─────────────────────────────────────────────────────────────
# The meme sector brought prices four to six decimal places below anything this repo had held
# before, and every one of these is a two-decimal rounding that was correct until it wasn't.

def test_a_sub_cent_token_gets_a_real_hard_stop(crypto_pf):
    """The expensive one. `round(price * 0.85, 2)` on BONK at $0.00000296 is **0.0**, and a stop of
    zero is not a tight stop — `gain <= stop_pct` can never fire against it, so the position rides
    all the way to nothing while the ledger shows a stop is in place."""
    _buy(crypto_pf, "BONK-USD", 0.00000296, weight=5)
    pos = crypto_pf.state["positions"]["BONK-USD"]
    assert pos["hard_stop"] > 0
    assert pos["hard_stop"] == pytest.approx(0.00000296 * 0.85, rel=1e-3)


def test_a_sub_cent_token_still_stops_out(crypto_pf):
    """End to end, because the stop existing and the stop firing are two different bugs."""
    _buy(crypto_pf, "BONK-USD", 0.00000296, weight=5)
    crypto_pf.mark_to_market({"BONK-USD": 0.00000240})        # -19%, past the -15 gate
    assert "BONK-USD" not in crypto_pf.state["positions"]


def test_adding_to_a_sub_cent_position_does_not_zero_the_entry(crypto_pf):
    """The averaged entry rounded to 4 dp, so an ADD on PEPE set entry to 0.0 — after which every
    P&L line on the position divides by zero."""
    _buy(crypto_pf, "PEPE24478-USD", 0.0000028, weight=5)
    ok, msg = crypto_pf.apply_action(
        {"action": "ADD", "ticker": "PEPE24478-USD", "entry": 0.0000030,
         "target_weight_pct": 15, "reason": "test"},     # a real increase, not the same weight
        prices={"PEPE24478-USD": 0.0000030})
    assert ok, msg
    pos = crypto_pf.state["positions"]["PEPE24478-USD"]
    assert pos["entry"] > 0
    crypto_pf.mark_to_market({"PEPE24478-USD": 0.0000031})    # would raise on a zero entry


def test_a_meme_coin_card_shows_actual_numbers():
    """`:,.2f` rendered every price on a BONK card as "0.00" — a trade card with no trade on it,
    which reads as a broken engine rather than a formatting choice."""
    from brain import notify
    card = notify.build_card(
        {"ticker": "BONK-USD", "direction": "LONG", "trade_type": "SCALP", "confidence": 60,
         "entry": 0.00000296, "stop": 0.00000252, "target1": 0.0000034, "target2": 0.0000041},
        datetime(2026, 7, 28, 9, 30, tzinfo=timezone.utc))
    assert "0.00000296" in card
    assert "0.00000252" in card
    assert ">0.00<" not in card


def test_an_ordinary_price_is_still_two_decimals():
    from brain import notify
    card = notify.build_card(
        {"ticker": "BTC-USD", "direction": "LONG", "entry": 65_000.4567, "stop": 55_250.39},
        datetime(2026, 7, 28, 9, 30, tzinfo=timezone.utc))
    assert "65,000.46" in card
    assert "55,250.39" in card


def test_equity_prices_round_exactly_as_they_always_did():
    """This must not have reached the stock book. Anything at or above $1 keeps two decimals."""
    from brain.portfolio import round_px
    assert round_px(142.10999) == 142.11
    assert round_px(1.0) == 1.0
    assert round_px(65_000.4567) == 65_000.46
    assert round_px(None) is None


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
