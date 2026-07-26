"""The repeat-signal guard.

Lind's complaint was "i kept getting signals in my email to take the same trade over and over
again". The screener fires on a condition and a condition persists, so the same idea is reached
honestly every thirty minutes. These tests pin the three things that must still get through — a
real change of price, a real change of conviction, and a direction flip — because a de-duplicator
that suppresses those is worse than the spam it replaced.

Run:  python -m pytest tests/test_dedupe.py -q
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from brain import config, dedupe, memory

NOW = datetime(2026, 7, 29, 15, 0, tzinfo=config.UTC)


def sig(ticker="AAPL", direction="LONG", trade_type="SHORT_TERM", entry=200.0, confidence=70):
    return {"ticker": ticker, "direction": direction, "trade_type": trade_type,
            "entry": entry, "confidence": confidence, "why": "breakout"}


def rec(hours_ago=1.0, outcome="executed", **kw):
    base = sig()
    base.update(kw)
    base["ts"] = (NOW - timedelta(hours=hours_ago)).isoformat()
    base["outcome"] = outcome
    return base


# ── classify ───────────────────────────────────────────────────────────────────

def test_a_first_sighting_is_always_new():
    assert dedupe.classify(sig(), [], now=NOW)[0] == "new"


def test_the_same_call_an_hour_later_is_a_repeat():
    status, reason = dedupe.classify(sig(), [rec(hours_ago=1)], now=NOW)
    assert status == "repeat"
    assert "nothing material changed" in reason


def test_the_cooldown_expiring_re_opens_the_idea():
    """A swing setup still valid a day later is worth saying again."""
    status, reason = dedupe.classify(sig(), [rec(hours_ago=25)], now=NOW)
    assert status == "new"
    assert "cooldown" in reason


def test_cooldowns_differ_by_trade_type():
    """Four hours kills a scalp's relevance and does nothing to a multi-quarter thesis."""
    old = rec(hours_ago=4, trade_type="SCALP")
    assert dedupe.classify(sig(trade_type="SCALP"), [old], now=NOW)[0] == "new"
    old = rec(hours_ago=4, trade_type="LONG_TERM")
    assert dedupe.classify(sig(trade_type="LONG_TERM"), [old], now=NOW)[0] == "repeat"


def test_a_materially_different_entry_is_a_different_trade():
    prior = rec(hours_ago=1, entry=200.0)
    moved = sig(entry=200.0 * (1 + (config.SIGNAL_ENTRY_DRIFT_PCT + 1) / 100))
    status, reason = dedupe.classify(moved, [prior], now=NOW)
    assert status == "new"
    assert "entry moved" in reason


def test_a_trivial_entry_wobble_is_still_the_same_trade():
    prior = rec(hours_ago=1, entry=200.0)
    assert dedupe.classify(sig(entry=200.2), [prior], now=NOW)[0] == "repeat"


def test_a_big_confidence_move_is_news_in_both_directions():
    prior = rec(hours_ago=1, confidence=60)
    up = sig(confidence=60 + config.SIGNAL_CONFIDENCE_JUMP)
    down = sig(confidence=60 - config.SIGNAL_CONFIDENCE_JUMP)
    assert dedupe.classify(up, [prior], now=NOW)[0] == "new"
    assert dedupe.classify(down, [prior], now=NOW)[0] == "new"


def test_a_small_confidence_drift_is_not():
    prior = rec(hours_ago=1, confidence=60)
    assert dedupe.classify(sig(confidence=64), [prior], now=NOW)[0] == "repeat"


def test_a_direction_flip_is_never_suppressed():
    """LONG yesterday, SHORT today is the most important thing the brain can say."""
    prior = rec(hours_ago=0.5, direction="LONG")
    assert dedupe.classify(sig(direction="SHORT"), [prior], now=NOW)[0] == "new"


def test_a_different_ticker_is_never_suppressed():
    assert dedupe.classify(sig(ticker="MSFT"), [rec(hours_ago=0.5)], now=NOW)[0] == "new"


def test_a_prior_suppression_cannot_renew_its_own_cooldown():
    """Otherwise the first repeat freezes the idea forever, one suppression at a time."""
    history = [rec(hours_ago=30, outcome="executed"), rec(hours_ago=1, outcome="duplicate")]
    assert dedupe.classify(sig(), history, now=NOW)[0] == "new"


def test_a_rejected_signal_still_counts_as_published():
    """Lind saw it in the email. The gates rejecting it does not make it unsaid."""
    assert dedupe.classify(sig(), [rec(hours_ago=1, outcome="rejected")], now=NOW)[0] == "repeat"


def test_an_unparseable_prior_timestamp_fails_open():
    bad = rec(hours_ago=1)
    bad["ts"] = "not a date"
    assert dedupe.classify(sig(), [bad], now=NOW)[0] == "new"


def test_an_unknown_trade_type_gets_the_middle_cooldown_not_the_longest():
    """A mislabelled signal should err toward being shown, not swallowed for five days."""
    prior = rec(hours_ago=30, trade_type="MOMENTUM")
    assert dedupe.classify(sig(trade_type="MOMENTUM"), [prior], now=NOW)[0] == "new"


# ── apply ──────────────────────────────────────────────────────────────────────

def test_apply_strips_the_repeat_and_keeps_the_rest():
    analysis = {"signals": [sig(ticker="AAPL"), sig(ticker="NVDA")]}
    suppressed = dedupe.apply(analysis, history=[rec(hours_ago=1, ticker="AAPL")], now=NOW)
    assert [s["ticker"] for s in analysis["signals"]] == ["NVDA"]
    assert [s["ticker"] for s in suppressed] == ["AAPL"]
    assert analysis["suppressed_repeats"][0]["ticker"] == "AAPL"


def test_apply_also_drops_the_matching_entry_action():
    """Executing a trade whose reasoning was withheld is worse than either alone."""
    analysis = {"signals": [sig(ticker="AAPL")],
                "portfolio_actions": [{"action": "BUY", "ticker": "AAPL"},
                                      {"action": "BUY", "ticker": "NVDA"}]}
    dedupe.apply(analysis, history=[rec(hours_ago=1, ticker="AAPL")], now=NOW)
    assert [a["ticker"] for a in analysis["portfolio_actions"]] == ["NVDA"]


def test_apply_never_touches_an_exit():
    """A SELL is a risk decision. It gets through however often it repeats."""
    analysis = {"signals": [sig(ticker="AAPL")],
                "portfolio_actions": [{"action": "SELL", "ticker": "AAPL"},
                                      {"action": "TRIM", "ticker": "AAPL"}]}
    dedupe.apply(analysis, history=[rec(hours_ago=1, ticker="AAPL")], now=NOW)
    assert len(analysis["portfolio_actions"]) == 2


def test_two_identical_signals_in_one_response_publish_once():
    analysis = {"signals": [sig(), sig()]}
    dedupe.apply(analysis, history=[], now=NOW)
    assert len(analysis["signals"]) == 1


def test_a_clean_run_leaves_the_analysis_untouched():
    analysis = {"signals": [sig()], "portfolio_actions": [{"action": "BUY", "ticker": "AAPL"}]}
    assert dedupe.apply(analysis, history=[], now=NOW) == []
    assert "suppressed_repeats" not in analysis
    assert len(analysis["portfolio_actions"]) == 1


def test_an_empty_analysis_is_not_an_error():
    assert dedupe.apply({}, history=[], now=NOW) == []
    assert dedupe.apply({"signals": []}, history=[], now=NOW) == []


def test_summarize_names_the_tickers_once_each():
    out = dedupe.summarize([sig(ticker="AAPL"), sig(ticker="AAPL"), sig(ticker="NVDA")])
    assert "3 repeat signal(s)" in out and "AAPL, NVDA" in out
    assert dedupe.summarize([]) == ""


# ── the ledger side ────────────────────────────────────────────────────────────

@pytest.fixture
def ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    monkeypatch.setattr(memory, "SIGNALS_LOG", tmp_path / "SIGNALS-LOG.jsonl")
    return tmp_path


def test_suppressed_signals_are_logged_not_discarded(ledger):
    """The recap needs to know how often the analyst repeats itself — that is a screener signal."""
    suppressed = [{**sig(), "suppressed_reason": "same LONG AAPL call as 1.0h ago"}]
    memory.log_suppressed(suppressed, "cycle", prices={"AAPL": 201.0})
    rows = memory.signals_between("2000-01-01T00:00:00+00:00")
    assert len(rows) == 1
    assert rows[0]["outcome"] == "duplicate"
    assert rows[0]["ticker"] == "AAPL"
    assert "same LONG AAPL call" in rows[0]["gate_reason"]


def test_repeats_between_finds_only_the_suppressed_ones(ledger):
    memory.log_suppressed([{**sig(), "suppressed_reason": "repeat"}], "cycle")
    memory.log_signals({"signals": [sig(ticker="NVDA")]}, {}, "cycle")
    assert [r["ticker"] for r in dedupe.repeats_between("2000-01-01T00:00:00+00:00")] == ["AAPL"]


def test_recent_signals_respects_the_window(ledger):
    memory.log_suppressed([{**sig(), "suppressed_reason": "repeat"}], "cycle")
    assert len(memory.recent_signals(1)) == 1
    assert memory.recent_signals(0) == []


def test_the_lookback_covers_the_longest_cooldown():
    """A LONG_TERM repeat five days old must still be visible to classify()."""
    assert dedupe._lookback_h() >= max(config.SIGNAL_COOLDOWN_H.values())
    assert dedupe.stale_cutoff() < datetime.now(config.UTC)
