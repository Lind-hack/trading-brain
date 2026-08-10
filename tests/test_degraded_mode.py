"""What the cycle emits when no model read the tape.

On 2026-08-10 Lind found the brain stale and reported that "the claude usage was always at 0%".
It had in fact stopped on 2026-08-04. Six days of outage produced no alarm anywhere, and one
reason is visible in the output itself: a rules-only cycle emits screener flags at confidence 35,
which lands them under the floor next to genuinely unconvincing ideas, in the same anonymous grey
one-liner. A day where nobody looked read exactly like a quiet day with thin setups.

These tests pin the distinction:

1. A degraded signal is labelled as one, on the signal itself — those rows outlive the email into
   the ledger, the journal and the dashboard, each of which reads one signal at a time.
2. It claims no horizon. The screener flags a chart condition and does not reason about how long
   a trade should live; stamping SHORT_TERM on it is a claim nothing made.
3. It is never executable, and the email says why in both the HTML and the plain-text part.

Run:  python -m pytest tests/test_degraded_mode.py -q
"""
import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from brain import config, deep, notify


@pytest.fixture
def screen_result():
    return {"triggers": [
        {"ticker": "AAPL", "score": 7, "reasons": ["20-day breakout", "2.1x volume"]},
        {"ticker": "NVDA", "score": 6, "reasons": ["RSI 78"]},
    ]}


@pytest.fixture
def market():
    return {
        "AAPL": {"indicators": {"price": 210.0, "rsi": 61, "ma20": 205.0},
                 "patterns": [{"bias": "bullish"}]},
        "NVDA": {"indicators": {"price": 900.0, "rsi": 78},
                 "patterns": [{"bias": "bearish"}]},
    }


def test_every_fallback_signal_is_flagged_degraded(screen_result, market):
    """The envelope already carried `degraded`; the signals did not, and they are what persists."""
    out = deep.fallback_analysis(screen_result, market, reason="auth")
    assert out["degraded"] is True
    assert out["signals"], "the screener flagged names; they should still be reported"
    for s in out["signals"]:
        assert s["degraded"] is True
        assert s["degraded_reason"] == "auth"


def test_fallback_claims_no_horizon(screen_result, market):
    """`UNSPECIFIED`, not `SHORT_TERM` — and it must normalise to no horizon at all, so it fills
    no slot in the weekly mix and the review sees a signal that didn't say what it was."""
    out = deep.fallback_analysis(screen_result, market, reason="quota")
    for s in out["signals"]:
        assert s["trade_type"] == "UNSPECIFIED"
        assert config.normalize_trade_type(s["trade_type"]) is None


def test_fallback_stays_under_the_execute_bar(screen_result, market):
    """A rules-only flag must never reach the gates as a tradeable idea."""
    rules = config.STOCK_RULES
    for s in deep.fallback_analysis(screen_result, market)["signals"]:
        assert rules.confidence_band(s["confidence"]) == "below_bar"


def test_the_reason_reaches_the_rationale(screen_result, market):
    out = deep.fallback_analysis(screen_result, market, reason="auth wall")
    assert "auth wall" in out["signals"][0]["confidence_rationale"]


# ── the email ───────────────────────────────────────────────────────────────────

def _email(analysis):
    return notify.build_email(analysis, {"equity": 10000, "open_positions": [], "n_open": 0},
                              "cycle", datetime.now(config.ET))


def test_outage_is_visible_in_the_email(screen_result, market):
    """Amber block in the HTML, and a legible line in the plain-text part — which is what a phone
    notification previews."""
    analysis = deep.fallback_analysis(screen_result, market, reason="auth")
    subject, text, html = _email(analysis)
    assert "Models unavailable" in html
    assert "not analysed and not executable" in html
    assert "MODELS UNAVAILABLE" in text
    assert "AAPL" in html and "AAPL" in text


def test_a_degraded_signal_is_not_filed_as_an_ordinary_weak_idea(screen_result, market):
    """The failure this whole file is about: an outage hiding inside the below-floor one-liner."""
    analysis = deep.fallback_analysis(screen_result, market, reason="auth")
    html = _email(analysis)[2]
    assert "confidence floor, not shown" not in html, \
        "degraded flags must not be swept into the ordinary below-bar line"


def test_a_normal_weak_signal_still_uses_the_below_bar_line():
    """The regression guard in the other direction — the ordinary path must be untouched."""
    analysis = {"market_outlook": "quiet", "signals": [
        {"ticker": "TSLA", "direction": "LONG", "trade_type": "SHORT_TERM", "confidence": 40,
         "why": "thin", "entry": 300}], "portfolio_actions": []}
    html = _email(analysis)[2]
    assert "confidence floor, not shown" in html
    assert "Models unavailable" not in html
