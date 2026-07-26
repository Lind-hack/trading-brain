"""What the brain recommended has to outlive the run that recommended it.

Two related gaps, both of which made Lind's weekly question unanswerable. A signal the gates
rejected became one printed line and then ceased to exist, so "what did you recommend but not
take" had no source of truth. And the reasoning attached to a signal — the confidence rationale,
the expected holding period — was dropped at the moment of the fill, so by the time the journal
graded the exit the fields it was told to grade were gone.

Run:  python -m pytest tests/ -q
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from brain import config, memory
from brain.portfolio import ENTRY_META_FIELDS, Portfolio


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    p = tmp_path / "SIGNALS-LOG.jsonl"
    monkeypatch.setattr(memory, "SIGNALS_LOG", p)
    monkeypatch.setattr(memory, "EVENT_LOG", tmp_path / "EVENT-LOG.jsonl")
    return p


@pytest.fixture
def pf(tmp_path):
    return Portfolio(path=tmp_path / "PORTFOLIO.json", mirror=False)


def _signal(ticker="NVDA", **kw):
    base = {
        "ticker": ticker, "direction": "LONG", "trade_type": "SHORT_TERM",
        "holding_period": "5-10 trading days", "confidence": 68,
        "confidence_rationale": "68 — 20-day breakout on 2.1x volume; capped by earnings in 4d",
        "entry": 100.0, "stop": 93.0, "target1": 112.0, "target2": 120.0,
        "why": "breakout with volume confirmation", "indicators_used": ["RSI", "MA20", "volume"],
        "news_read": "Haiku flagged an upgrade at high materiality",
        "historical_analog": "3 prior breakouts averaged +4.1% over 5d (n=3)",
    }
    base.update(kw)
    return base


# ── the ledger ──────────────────────────────────────────────────────────────────

def test_executed_signal_is_logged(ledger):
    n = memory.log_signals({"signals": [_signal()], "portfolio_actions": [{"ticker": "NVDA", "action": "BUY"}]},
                           {"NVDA": {"action": "BUY", "ok": True, "msg": "opened NVDA 15 sh"}},
                           "cycle")
    assert n == 1
    rec = memory._read_jsonl(ledger)[0]
    assert rec["outcome"] == "executed"
    assert rec["proposed_action"] == "BUY"
    assert rec["confidence_rationale"].startswith("68 —")


def test_rejected_signal_survives_with_its_gate_reason(ledger):
    """The whole point: a blocked trade is data, not a discarded print statement."""
    memory.log_signals(
        {"signals": [_signal("COIN")], "portfolio_actions": [{"ticker": "COIN", "action": "BUY"}]},
        {"COIN": {"action": "BUY", "ok": False, "msg": "weekly new-trade cap reached (3/week)"}},
        "mid")
    rec = memory._read_jsonl(ledger)[0]
    assert rec["outcome"] == "rejected"
    assert "weekly new-trade cap" in rec["gate_reason"]
    assert rec["confidence"] == 68        # the reasoning is kept, not just the verdict


def test_a_signal_with_no_action_is_advisory(ledger):
    """An idea the model raised without proposing a trade is still a recommendation."""
    memory.log_signals({"signals": [_signal("PLTR")], "portfolio_actions": []}, {}, "close")
    rec = memory._read_jsonl(ledger)[0]
    assert rec["outcome"] == "advisory"
    assert rec["proposed_action"] is None


def test_a_managed_action_with_no_signal_is_still_logged(ledger):
    """Risk trims carry no signal, but they went through the gates and belong in the ledger."""
    memory.log_signals(
        {"signals": [], "portfolio_actions": [{"ticker": "CRM", "action": "SELL",
                                               "reason": "thesis broke"}]},
        {"CRM": {"action": "SELL", "ok": True, "msg": "closed CRM -4.1%"}}, "cycle")
    rec = memory._read_jsonl(ledger)[0]
    assert rec["ticker"] == "CRM" and rec["proposed_action"] == "SELL"
    assert rec["why"] == "thesis broke"


def test_one_record_per_ticker_not_two(ledger):
    """A signal and its own action are one idea, and must not be double-counted in the recap."""
    memory.log_signals(
        {"signals": [_signal("NVDA")], "portfolio_actions": [{"ticker": "NVDA", "action": "BUY"}]},
        {"NVDA": {"action": "BUY", "ok": True, "msg": "ok"}}, "cycle")
    assert len(memory._read_jsonl(ledger)) == 1


def test_nothing_to_log_writes_no_file(ledger):
    assert memory.log_signals({"signals": [], "portfolio_actions": []}, {}, "cycle") == 0
    assert not ledger.exists()


def test_rejections_get_forward_returns(ledger):
    """The counterfactual: what the blocked trade went on to do.

    Without this the weekly review has to assume every gate rejection was correct. With it,
    a quarter of blocked-but-profitable setups is a measurable argument for changing a rule.
    """
    memory.log_signals(
        {"signals": [_signal("COIN", entry=200.0)],
         "portfolio_actions": [{"ticker": "COIN", "action": "BUY"}]},
        {"COIN": {"action": "BUY", "ok": False, "msg": "at position cap (6)"}}, "cycle")
    # Backdate the record so the 1h and 4h buckets are due.
    recs = memory._read_jsonl(ledger)
    recs[0]["ts"] = (datetime.now(config.UTC) - timedelta(hours=5)).isoformat()
    memory._write_jsonl(ledger, recs)

    filled = memory.backfill_returns({"COIN": 220.0})
    assert filled == 2                       # 1h and 4h are due; 1d and 5d are not
    fwd = memory._read_jsonl(ledger)[0]["forward"]
    assert fwd["1h"] == pytest.approx(10.0)
    assert fwd["5d"] is None


def test_signals_between_windows_the_week(ledger):
    memory.log_signals({"signals": [_signal("AAPL")], "portfolio_actions": []}, {}, "cycle")
    week_ago = (datetime.now(config.UTC) - timedelta(days=7)).isoformat()
    assert len(memory.signals_between(week_ago)) == 1
    tomorrow = (datetime.now(config.UTC) + timedelta(days=1)).isoformat()
    assert memory.signals_between(tomorrow) == []


# ── entry metadata survives to the exit ─────────────────────────────────────────

def test_every_entry_field_reaches_the_exit_record(pf):
    """The grader is told to check the confidence rationale and the horizon. They must be there."""
    action = {"action": "BUY", "ticker": "NVDA", "target_weight_pct": 10, "reason": "breakout",
              **{k: v for k, v in _signal().items() if k in ENTRY_META_FIELDS}}
    ok, _ = pf.apply_action(action, prices={"NVDA": 100.0})
    assert ok
    pf.apply_action({"action": "SELL", "ticker": "NVDA", "entry": 108.0}, prices={"NVDA": 108.0})

    e = pf.exits_this_run[0]
    for k in ENTRY_META_FIELDS:
        assert k in e, f"exit record lost {k} somewhere between the fill and the close"
    assert e["confidence_rationale"].startswith("68 —")
    assert e["holding_period"] == "5-10 trading days"
    assert e["trade_type"] == "SHORT_TERM"
    assert e["thesis"] == "breakout"


def test_exit_kind_is_structured_not_prose(pf):
    """The recap counts the exit mix; counting it by grepping a sentence would break on rewording."""
    pf.apply_action({"action": "BUY", "ticker": "CRM", "target_weight_pct": 10},
                    prices={"CRM": 100.0})
    pf.mark_to_market({"CRM": 100 * (1 + (config.HARD_STOP_PCT - 1) / 100)})
    assert pf.exits_this_run[0]["exit_kind"] == "hard_stop"

    pf.apply_action({"action": "BUY", "ticker": "ORCL", "target_weight_pct": 10},
                    prices={"ORCL": 50.0})
    pf.apply_action({"action": "SELL", "ticker": "ORCL", "entry": 55.0}, prices={"ORCL": 55.0})
    assert pf.exits_this_run[1]["exit_kind"] == "model_sell"


def test_open_positions_report_their_horizon(pf):
    """A SCALP still open on Friday is a finding — but only if summary() says it is a SCALP."""
    pf.apply_action({"action": "BUY", "ticker": "TSLA", "target_weight_pct": 10,
                     "trade_type": "SCALP", "holding_period": "same session", "confidence": 55},
                    prices={"TSLA": 300.0})
    p = pf.summary({"TSLA": 305.0})["open_positions"][0]
    assert p["trade_type"] == "SCALP"
    assert p["holding_period"] == "same session"
    assert p["held_days"] is not None and p["held_days"] < 1
