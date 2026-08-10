"""The dashboard can only separate what the feed distinguishes.

Lind's complaint about signal-deck — that a trade the brain took looked identical to one it merely
recommended — was never a rendering problem. The rows carried no verdict, so both arrived in the
same list saying the same thing. These tests pin the field that fixes it (`outcome`, plus the
`gate_reason` that says which rule blocked a rejection) all the way from the gate that decides it
to the JSON that leaves for Supabase, and cover the thesis board's own channel.

Run:  python -m pytest tests/test_dashboard_feed.py -q
"""
import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import market_brain
from brain import config, supabase

NOW = datetime(2026, 7, 27, 10, 30, tzinfo=config.ET)


def _signal(ticker="NVDA", **kw):
    base = {"ticker": ticker, "direction": "LONG", "trade_type": "SHORT_TERM",
            "holding_period": "5-10 trading days", "confidence": 68,
            "entry": 100.0, "stop": 93.0, "target1": 112.0, "target2": 120.0,
            "why": "breakout with volume confirmation"}
    base.update(kw)
    return base


# ── the verdict, stamped at the gate ────────────────────────────────────────────

def test_an_approved_signal_is_marked_executed():
    a = {"signals": [_signal()]}
    market_brain._stamp_outcomes(a, {"NVDA": {"action": "BUY", "ok": True, "msg": "opened NVDA 15 sh"}})
    s = a["signals"][0]
    assert s["outcome"] == "executed"
    assert s["proposed_action"] == "BUY"


def test_a_blocked_signal_carries_the_rule_that_blocked_it():
    """The reason is the whole value of the card — "not taken" without a why teaches nothing."""
    a = {"signals": [_signal()]}
    market_brain._stamp_outcomes(a, {"NVDA": {"action": "BUY", "ok": False,
                                              "msg": "sector lockout: 2 consecutive losses in tech"}})
    s = a["signals"][0]
    assert s["outcome"] == "rejected"
    assert "sector lockout" in s["gate_reason"]


def test_a_signal_with_no_proposed_action_is_advisory():
    a = {"signals": [_signal()]}
    market_brain._stamp_outcomes(a, {})
    s = a["signals"][0]
    assert s["outcome"] == "advisory"
    assert s["proposed_action"] is None
    assert s["gate_reason"]


def test_the_ticker_is_matched_case_insensitively():
    a = {"signals": [_signal(ticker="nvda")]}
    market_brain._stamp_outcomes(a, {"NVDA": {"action": "BUY", "ok": True, "msg": "opened"}})
    assert a["signals"][0]["outcome"] == "executed"


def test_stamping_survives_an_empty_analysis():
    assert market_brain._stamp_outcomes(None, {"NVDA": {"ok": True}}) is None
    assert market_brain._stamp_outcomes({}, None) == {}


# ── what actually reaches Supabase ──────────────────────────────────────────────

class _Resp:
    def __init__(self, status=201, text=""):
        self.status_code, self.text = status, text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}: {self.text}")


class _Recorder(list):
    """The recorded (table, payload) pairs, plus the knob that fails one table's first insert."""
    reject_once = None


@pytest.fixture
def posted(monkeypatch):
    """Record every insert as (table, payload). Nothing leaves the process."""
    calls = _Recorder()
    reject_once = {"on": None}

    def fake_post(url, headers=None, timeout=None, json=None):
        table = url.rsplit("/", 1)[-1]
        calls.append((table, json))
        if reject_once["on"] == table:
            reject_once["on"] = None
            # Names a column from both new-column sets, so one knob covers either table's
            # migration-pending path. PostgREST really does name the offending column.
            return _Resp(400, "PGRST204 could not find the 'outcome' / 'model' column")
        return _Resp()

    monkeypatch.setattr(supabase.requests, "post", fake_post)
    monkeypatch.setattr(config, "SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setattr(config, "SUPABASE_ANON", "anon-key")
    monkeypatch.setattr(config, "SIGNAL_INGEST_KEY", "ingest-key")
    calls.reject_once = reject_once
    return calls


def _rows(calls, table):
    return [payload for t, payload in calls if t == table]


def test_the_signal_row_carries_the_verdict(posted):
    sig = _signal(outcome="rejected", gate_reason="max 6 positions already open",
                  proposed_action="BUY", news_edge="the 8-K nobody covered")
    supabase.push({"signals": [sig], "market_outlook": "chop"}, {"why": []}, None, "cycle", NOW, True)
    row = _rows(posted, "sd_brain_signals")[0][0]
    assert row["outcome"] == "rejected"
    assert row["gate_reason"] == "max 6 positions already open"
    assert row["proposed_action"] == "BUY"
    assert row["news_edge"] == "the 8-K nobody covered"


def test_a_long_term_signal_carries_its_thesis_anchor(posted):
    """thesis.annotate_signals() attaches the board entry as a dict; the card wants the theme."""
    sig = _signal(trade_type="LONG_TERM", thesis_id="th-ram-2026",
                  thesis={"id": "th-ram-2026", "theme": "DRAM shortage repricing memory makers",
                          "conviction": 72})
    supabase.push({"signals": [sig]}, {"why": []}, None, "cycle", NOW, True)
    row = _rows(posted, "sd_brain_signals")[0][0]
    assert row["thesis_id"] == "th-ram-2026"
    assert row["thesis_theme"] == "DRAM shortage repricing memory makers"


def test_a_string_thesis_does_not_become_a_theme(posted):
    """sd_trades stores `thesis` as prose. Reading .theme off it would be a crash or a lie."""
    supabase.push({"signals": [_signal(thesis="just a sentence of prose")]},
                  {"why": []}, None, "cycle", NOW, True)
    assert _rows(posted, "sd_brain_signals")[0][0]["thesis_theme"] is None


def test_an_unmigrated_table_still_gets_the_old_columns(posted):
    """Shipping the code before the SQL must degrade, not silently drop every signal."""
    posted.reject_once["on"] = "sd_brain_signals"
    supabase.push({"signals": [_signal(outcome="executed", gate_reason="opened")]},
                  {"why": []}, None, "cycle", NOW, True)
    first, retry = _rows(posted, "sd_brain_signals")
    assert "outcome" in first[0]
    assert not set(supabase._NEW_SIGNAL_COLS) & set(retry[0])
    assert retry[0]["ticker"] == "NVDA"


# ── which tier actually ran ─────────────────────────────────────────────────────
# The deck read `escalated` as "Opus is running", so a news-only cycle, a gated-out run and an
# escalation that fell back to the rules all advertised a deep run that never happened. These
# four columns are what let the strip say which model produced a heartbeat instead of guessing.

def test_a_deep_run_names_the_model_that_produced_it(posted):
    supabase.push({"signals": [_signal()], "model": config.CLAUDE_DEEP_MODEL},
                  {"why": []}, None, "cycle", NOW, True,
                  intel={"model": config.CLAUDE_LITE_MODEL, "degraded": False})
    row = _rows(posted, "sd_brain_scans")[0]
    assert row["model"] == config.CLAUDE_DEEP_MODEL
    assert row["news_model"] == config.CLAUDE_LITE_MODEL
    assert row["news_degraded"] is False
    assert row["degraded_kind"] is None


def test_a_news_only_cycle_is_not_reported_as_a_deep_run(posted):
    """The exact confusion Lind reported: Haiku scans, the page says Opus woke up."""
    supabase.push(None, {"why": ["quiet"]}, None, "cycle", NOW, False,
                  intel={"model": config.CLAUDE_LITE_MODEL, "degraded": False})
    row = _rows(posted, "sd_brain_scans")[0]
    assert row["model"] is None
    assert row["news_model"] == config.CLAUDE_LITE_MODEL


def test_an_escalation_that_fell_back_claims_no_model(posted):
    """`escalated` was true and no deep run happened — the row has to say the second part."""
    supabase.push({"signals": [], "degraded": True, "degraded_kind": "usage"},
                  {"why": []}, None, "cycle", NOW, True,
                  intel={"model": config.CLAUDE_LITE_MODEL, "degraded": False})
    row = _rows(posted, "sd_brain_scans")[0]
    assert row["escalated"] is True and row["model"] is None
    assert row["degraded_kind"] == "usage"


def test_a_failed_news_pass_names_the_fallback(posted):
    supabase.push(None, {"why": []}, None, "cycle", NOW, False,
                  intel={"model": "keyword-fallback", "degraded": True, "degraded_kind": "auth"})
    row = _rows(posted, "sd_brain_scans")[0]
    assert row["news_model"] == "keyword-fallback"
    assert row["news_degraded"] is True
    assert row["degraded_kind"] == "auth"


def test_a_run_with_no_news_pass_says_none_not_null(posted):
    """The off-hours crypto sweep calls no model at all. 'none' distinguishes that from a gap."""
    supabase.push(None, {"why": ["stops only"]}, None, "crypto", NOW, False)
    row = _rows(posted, "sd_brain_scans")[0]
    assert row["news_model"] == "none" and row["model"] is None


def test_the_gate_heartbeat_wakes_nothing(posted):
    assert supabase.push_heartbeat("cycle", NOW, "market closed (weekend)") is True
    row = _rows(posted, "sd_brain_scans")[0]
    assert row["model"] is None and row["news_model"] == "none"
    assert row["news_degraded"] is False


def test_an_unmigrated_scans_table_still_gets_the_heartbeat(posted):
    """Shipping the code before the SQL must not cost the heartbeat — that row is how the deck
    tells "the VPS is down" from "the schema is behind"."""
    posted.reject_once["on"] = "sd_brain_scans"
    supabase.push({"signals": [], "model": config.CLAUDE_DEEP_MODEL},
                  {"why": []}, None, "cycle", NOW, True)
    first, retry = _rows(posted, "sd_brain_scans")
    assert "model" in first
    assert not set(supabase._NEW_SCAN_COLS) & set(retry)
    assert retry["mode"] == "cycle" and retry["escalated"] is True


# ── the thesis board ────────────────────────────────────────────────────────────

def test_the_board_is_published_whole(posted):
    board = [{"theme": "DRAM shortage", "conviction": 72, "tickers": ["MU"]},
             {"theme": "Grid buildout", "conviction": 58, "tickers": ["ETN", "PWR"]}]
    assert supabase.push_theses(board, "digest", NOW, regime="late-cycle, capex-led") is True
    row = _rows(posted, "sd_theses")[0]
    assert row["mode"] == "digest"
    assert row["n_active"] == 2
    assert row["regime"] == "late-cycle, capex-led"
    assert row["board"] == board


def test_an_empty_board_still_posts_a_row(posted):
    """The dashboard reads the newest row only. Skipping the write would leave a stale board up."""
    assert supabase.push_theses([], "weekly", NOW) is True
    row = _rows(posted, "sd_theses")[0]
    assert row["n_active"] == 0 and row["board"] == []


def test_the_board_is_not_pushed_without_credentials(monkeypatch):
    monkeypatch.setattr(config, "SIGNAL_INGEST_KEY", "")
    assert supabase.push_theses([{"theme": "x"}], "digest", NOW) is False
