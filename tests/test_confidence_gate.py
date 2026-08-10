"""Confidence is a gate now, and a gate that isn't tested is a decoration with extra steps.

Lind's complaint was that the best number he ever saw was 54. Two things caused it — both model
tiers were dead in cron so most signals were `deep.fallback_analysis`'s hardcoded 35, and nothing
downstream cared what the number said. The first is fixed upstream. These tests pin the second:
65 executes, 55–64 is a WATCH that takes no position but still gets read, under 55 is not a trade,
and an action that never stated a number at all is none of those three.

Run:  python -m pytest tests/test_confidence_gate.py -q
"""
import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from brain import config, notify
from brain.portfolio import Portfolio

NOW = datetime(2026, 8, 3, 10, 30, tzinfo=config.ET)


@pytest.fixture
def pf(tmp_path):
    return Portfolio(path=tmp_path / "PORTFOLIO.json")


def _buy(pf, ticker="NVDA", entry=100.0, **kw):
    action = {"action": "BUY", "ticker": ticker, "entry": entry,
              "target_weight_pct": 10.0, "reason": "test"}
    action.update(kw)
    return pf.apply_action(action, prices={ticker: entry})


def _signal(ticker="NVDA", **kw):
    base = {"ticker": ticker, "direction": "LONG", "trade_type": "SHORT_TERM",
            "holding_period": "5-10 trading days", "confidence": 72,
            "entry": 100.0, "stop": 93.0, "target1": 112.0, "target2": 120.0,
            "why": "breakout with volume confirmation", "confidence_rationale": "two legs agree"}
    base.update(kw)
    return base


# ── the band itself ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("rules", [config.STOCK_RULES, config.CRYPTO_RULES])
@pytest.mark.parametrize("conf,band", [
    (90, "execute"), (65, "execute"), (64.9, "watch"), (55, "watch"),
    (54.9, "below_bar"), (0, "below_bar"),
])
def test_the_boundaries_land_where_lind_put_them(rules, conf, band):
    """65 and 55 are inclusive lower edges, and both books run the same two numbers — a 58 on BTC
    is exactly as unconvinced as a 58 on NVDA."""
    assert rules.confidence_band(conf) == band


@pytest.mark.parametrize("value", [None, "", "high", float("nan"), {}])
def test_an_unstated_number_is_unrated_not_weak(value):
    """A mechanical exit and a hand-run action carry no conviction claim at all. Repricing that
    silence as a weak claim would make the confidence gate refuse trades for a reason that was
    never about confidence."""
    assert config.STOCK_RULES.confidence_band(value) == "unrated"


def test_a_numeric_string_is_still_a_number():
    """The model returns JSON; a quoted "72" is a formatting slip, not an unrated signal."""
    assert config.STOCK_RULES.confidence_band("72") == "execute"


# ── what the harness does with it ───────────────────────────────────────────────

def test_a_conviction_buy_is_filled(pf):
    ok, msg = _buy(pf, confidence=71)
    assert ok, msg
    assert "NVDA" in pf.state["positions"]


def test_a_watch_takes_no_position(pf):
    ok, msg = _buy(pf, confidence=58)
    assert not ok
    assert "watch only" in msg and str(config.STOCK_RULES.min_confidence) in msg
    assert pf.state["positions"] == {}


def test_below_the_floor_is_not_a_trade(pf):
    ok, msg = _buy(pf, confidence=42)
    assert not ok
    assert f"below the {config.STOCK_RULES.watch_confidence} floor" in msg


def test_the_conviction_check_runs_before_the_bookkeeping_rules(pf):
    """A 48 refused for "the weekly cap is spent" teaches the Friday review the wrong lesson. It
    was a weak idea, and it would have been a weak idea on Monday."""
    for i in range(config.STOCK_RULES.max_new_trades_per_week):
        assert _buy(pf, f"N{i}", entry=50.0, confidence=70)[0]
    ok, msg = _buy(pf, "ZZZ", entry=50.0, confidence=48)
    assert not ok
    assert "floor" in msg and "cap" not in msg


def test_an_add_clears_the_same_bar(pf):
    assert _buy(pf, confidence=70)[0]
    ok, msg = pf.apply_action({"action": "ADD", "ticker": "NVDA", "entry": 100.0,
                               "target_weight_pct": 14.0, "confidence": 58},
                              prices={"NVDA": 100.0})
    assert not ok and "watch only" in msg


def test_an_exit_is_never_gated_on_conviction(pf):
    """Getting out is risk management. A 30 on "this thesis is broken" is the honest number and
    must not be the reason the position stays on the book."""
    assert _buy(pf, confidence=70)[0]
    ok, msg = pf.apply_action({"action": "SELL", "ticker": "NVDA", "confidence": 30,
                               "reason": "thesis broken"}, prices={"NVDA": 104.0})
    assert ok, msg
    assert "NVDA" not in pf.state["positions"]


def test_an_unrated_action_still_fills(pf):
    ok, msg = _buy(pf)
    assert ok, msg


# ── what the email does with it ─────────────────────────────────────────────────

def _email(*signals, mode="cycle"):
    """(subject, text, html) — build_email returns the plaintext fallback in the middle slot."""
    return notify.build_email({"signals": list(signals), "market_outlook": "chop"},
                              None, mode, NOW)


def test_a_watch_card_says_it_was_not_taken():
    _subject, text, html = _email(_signal(confidence=58))
    assert "WATCH ONLY" in html
    assert f"{config.STOCK_RULES.min_confidence} confidence required to execute" in html
    assert "[WATCH" in text


def test_a_filled_trade_carries_no_watch_banner():
    _subject, text, html = _email(_signal(confidence=78))
    assert "WATCH ONLY" not in html
    assert "[WATCH" not in text


def test_the_subject_does_not_read_like_a_fill_when_nothing_cleared_the_bar():
    subject, _text, _html = _email(_signal(confidence=57))
    assert "watch —" in subject


def test_below_the_floor_is_counted_not_carded():
    """Dropping them silently would make the email look like the analyst saw less than it did;
    carding them would bury the one idea that mattered under four that didn't."""
    _subject, _text, html = _email(_signal(confidence=79),
                                   _signal("AMD", confidence=41),
                                   _signal("INTC", confidence=38))
    # The card wraps the sentence across source lines, so collapse whitespace before matching.
    flat = " ".join(html.split())
    assert f"2 ideas below the {config.STOCK_RULES.watch_confidence} confidence floor" in flat
    assert "AMD" in html and "INTC" in html
    # Named in the footnote line, not given a plan of their own.
    assert "INTC LONG" not in html


def test_the_crypto_book_reads_its_own_rules():
    subject, _text, html = _email(_signal("BTC-USD", confidence=58), mode="crypto")
    assert "Crypto" in subject
    assert f"{config.CRYPTO_RULES.min_confidence} confidence required" in html
