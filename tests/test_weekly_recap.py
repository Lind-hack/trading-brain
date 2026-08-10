"""Tests for the Friday-close weekly recap (Phase 5).

The recap used to be the model's own guess about how it had behaved. Now it is arithmetic over
the ledger, and the model only gets to write the prose. These tests protect the arithmetic:

1. **Calibration is a measurement, not a mood.** A bucket that wins less often than its midpoint
   implies must be called overconfident, and a thin sample must be labelled as thin rather than
   quoted as if it were evidence.
2. **A SHORT that falls is a win.** Forward returns on untaken signals are direction-adjusted, so
   the gate verdict cannot be inverted by a profitable short.
3. **Engineering asks never reach STRATEGY.md.** Anything written there is reloaded into every
   deep run, so a pipeline request there would burn packet budget every cycle on an instruction
   no model can act on.

Run:  python -m pytest tests/test_weekly_recap.py -q
"""
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from brain import config, journal, memory, notify


def _closed(ticker, pnl_pct, confidence=None, indicators=None, trade_type=None,
            exit_kind=None, sector=None):
    return {"ticker": ticker, "pnl_pct": pnl_pct, "pnl_usd": pnl_pct * 10,
            "confidence": confidence, "indicators_used": indicators or [],
            "trade_type": trade_type, "exit_kind": exit_kind, "sector": sector,
            "entry": 100, "exit": 100 + pnl_pct, "reason": "test"}


def _signal(ticker, outcome, direction="LONG", confidence=70, fwd_1d=None, fwd_5d=None,
            gate_reason=""):
    return {"ticker": ticker, "outcome": outcome, "direction": direction,
            "trade_type": "SHORT_TERM", "confidence": confidence, "gate_reason": gate_reason,
            "why": "because", "forward": {"1d": fwd_1d, "5d": fwd_5d}}


# ── calibration ────────────────────────────────────────────────────────────────

def test_a_bucket_that_wins_less_than_it_promised_is_called_overconfident():
    """A 75 that wins half the time is the single most actionable number in the recap."""
    closed = [_closed("A", 5, 75), _closed("B", 5, 72), _closed("C", -5, 78),
              _closed("D", -5, 71), _closed("E", -5, 70), _closed("F", -5, 79)]
    out = {b["bucket"]: b for b in journal.confidence_calibration(closed)}
    b = out["70-79"]
    assert b["n"] == 6
    assert b["win_rate"] == pytest.approx(33.3, abs=0.1)
    assert b["implied_win_rate"] == 74.5
    assert b["gap_pts"] < -10
    assert b["read"] == "overconfident"


def test_a_bucket_that_beats_its_midpoint_is_underconfident():
    closed = [_closed(t, 4, 55) for t in "ABCD"]
    b = journal.confidence_calibration(closed)[0]
    assert b["win_rate"] == 100.0 and b["gap_pts"] >= 10
    assert b["read"] == "underconfident"


def test_three_trades_are_labelled_thin_not_quoted_as_evidence():
    """100% off three trades is noise wearing a percentage sign."""
    closed = [_closed(t, 4, 75) for t in "ABC"]
    b = journal.confidence_calibration(closed)[0]
    assert b["win_rate"] == 100.0
    assert b["read"] == "too few trades to judge"
    an = journal.week_analytics(closed)
    assert an["sample_note"] and "descriptive, not evidence" in an["sample_note"]


def test_a_full_sample_drops_the_thinness_note():
    closed = [_closed(t, 4, 65) for t in "ABCD"]
    assert journal.week_analytics(closed)["sample_note"] is None


def test_trades_with_no_confidence_are_skipped_not_bucketed_at_zero():
    closed = [_closed("A", 5, None), _closed("B", 5, 65)]
    buckets = journal.confidence_calibration(closed)
    assert [b["bucket"] for b in buckets] == ["60-69"]
    assert buckets[0]["n"] == 1


# ── the grouped cuts ───────────────────────────────────────────────────────────

def test_one_trade_counts_once_per_indicator_it_claimed():
    closed = [_closed("A", 6, indicators=["RSI", "MACD"]),
              _closed("B", -6, indicators=["RSI"])]
    rows = {r["indicator"]: r for r in journal.indicator_hit_rate(closed)}
    assert rows["RSI"]["n"] == 2 and rows["RSI"]["win_rate"] == 50.0
    assert rows["MACD"]["n"] == 1 and rows["MACD"]["win_rate"] == 100.0
    assert sum(r["n"] for r in rows.values()) != len(closed)   # deliberately not a partition


def test_indicators_rank_by_sample_size_so_the_thin_ones_sink():
    closed = [_closed("A", 3, indicators=["VWAP"]), _closed("B", 3, indicators=["VWAP"]),
              _closed("C", 3, indicators=["ATR"])]
    assert [r["indicator"] for r in journal.indicator_hit_rate(closed)] == ["VWAP", "ATR"]


def test_an_exit_with_no_recorded_kind_is_the_model_selling_not_a_missing_row():
    closed = [_closed("A", -7, exit_kind="hard_stop"), _closed("B", 3)]
    kinds = {r["name"]: r["n"] for r in journal.week_analytics(closed)["by_exit_kind"]}
    assert kinds == {"hard_stop": 1, "model_sell": 1}


def test_sectors_that_were_never_recorded_do_not_become_a_none_bucket():
    closed = [_closed("A", 3, sector="Tech"), _closed("B", 3)]
    rows = journal.week_analytics(closed)["by_sector"]
    assert [r["name"] for r in rows] == ["Tech"]


# ── signals: recommended vs executed ───────────────────────────────────────────

def test_the_recap_counts_what_was_recommended_not_only_what_filled():
    sigs = [_signal("A", "executed"), _signal("B", "rejected", gate_reason="max positions (6)"),
            _signal("C", "rejected", gate_reason="max positions (6)"),
            _signal("D", "advisory")]
    rev = journal.signal_review(sigs)
    assert (rev["n_signals"], rev["n_executed"], rev["n_rejected"], rev["n_advisory"]) == (4, 1, 2, 1)
    assert rev["rejection_reasons"] == [{"reason": "max positions", "n": 2}]
    assert len(rev["recommended"]) == 4


def test_a_profitable_short_is_not_scored_as_a_loss():
    """Raw forward return on a SHORT is backwards: the price falling is the thesis working."""
    sigs = ([_signal(t, "rejected", direction="SHORT", fwd_1d=-3.0) for t in "ABCD"]
            + [_signal("E", "executed", fwd_1d=1.0)])
    rev = journal.signal_review(sigs)
    assert rev["not_taken_forward_1d"] == 3.0          # not -3.0: the shorts were right
    assert "cost performance" in rev["gate_verdict"]


def test_the_gates_get_credit_when_the_skipped_signals_lost():
    sigs = ([_signal(t, "rejected", fwd_1d=-2.0) for t in "ABCD"]
            + [_signal("E", "executed", fwd_1d=4.0)])
    rev = journal.signal_review(sigs)
    assert rev["executed_forward_1d"] == 4.0 and rev["not_taken_forward_1d"] == -2.0
    assert "the gates helped" in rev["gate_verdict"]


def test_three_untaken_signals_are_too_few_to_convict_the_gates():
    sigs = [_signal(t, "rejected", fwd_1d=9.0) for t in "ABC"]
    assert "too few to judge" in journal.signal_review(sigs)["gate_verdict"]


def test_a_week_with_no_forward_returns_yet_says_so_instead_of_guessing():
    sigs = [_signal(t, "rejected") for t in "ABCD"]
    assert "not backfilled yet" in journal.signal_review(sigs)["gate_verdict"]


def test_no_signals_at_all_is_a_valid_week():
    rev = journal.signal_review([])
    assert rev["n_signals"] == 0 and rev["recommended"] == []
    assert journal.week_analytics([])["signals"]["n_signals"] == 0


# ── the packet handed to the review model ──────────────────────────────────────

def test_the_review_packet_carries_the_measurements_and_stays_valid_json():
    recap = {"stats": {"week_return_pct": 1.0}, "closed": [_closed("A", 4, 75)],
             "open": [], "gradings": [],
             "analytics": journal.week_analytics([_closed("A", 4, 75)]),
             "week_ahead": {"events": [{"when": "Tue", "title": "CPI", "country": "USD"}]}}
    packet = json.loads(journal.recap_packet(recap))
    assert packet["measured_performance"]["by_confidence"][0]["bucket"] == "70-79"
    assert packet["week_ahead"]["events"][0]["title"] == "CPI"


def test_a_nan_pnl_never_reaches_the_review_model():
    """json.dumps writes a bare NaN token; the model then reads "NaN" as a number."""
    recap = {"stats": {"week_return_pct": float("nan")}, "closed": [], "open": [],
             "gradings": [], "analytics": {}, "week_ahead": {}}
    json.loads(journal.recap_packet(recap))    # would raise on a bare NaN


# ── the two audiences ──────────────────────────────────────────────────────────

def test_pipeline_asks_go_to_the_backlog_and_never_into_strategy(tmp_path, monkeypatch):
    """STRATEGY.md is reloaded into every deep run. An engineering ask there is dead weight
    the analyst re-reads every 30 minutes and can never act on."""
    monkeypatch.setattr(memory, "BACKLOG", tmp_path / "PIPELINE-BACKLOG.md")
    monkeypatch.setattr(memory, "STRATEGY", tmp_path / "STRATEGY.md")
    ok = memory.append_pipeline_backlog(
        [{"change": "add options flow", "why": "by_indicator shows volume alone at 40%",
          "effort": "medium"}],
        datetime(2026, 7, 24), stats={"week_return_pct": -1.2, "n_closed": 5})
    assert ok
    text = (tmp_path / "PIPELINE-BACKLOG.md").read_text(encoding="utf-8")
    assert "- [ ] **add options flow** _(medium)_" in text
    assert "by_indicator shows volume alone at 40%" in text
    assert "Week of 2026-07-24" in text
    assert not (tmp_path / "STRATEGY.md").exists()


def test_an_empty_backlog_writes_no_file_at_all(tmp_path, monkeypatch):
    """"The pipeline was adequate this week" is a real finding, not an empty section."""
    monkeypatch.setattr(memory, "BACKLOG", tmp_path / "PIPELINE-BACKLOG.md")
    assert memory.append_pipeline_backlog([], datetime(2026, 7, 24)) is False
    assert not (tmp_path / "PIPELINE-BACKLOG.md").exists()


def test_a_second_week_appends_below_the_first_without_repeating_the_header(tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "BACKLOG", tmp_path / "PIPELINE-BACKLOG.md")
    memory.append_pipeline_backlog(["first"], datetime(2026, 7, 17))
    memory.append_pipeline_backlog(["second"], datetime(2026, 7, 24))
    text = (tmp_path / "PIPELINE-BACKLOG.md").read_text(encoding="utf-8")
    assert text.count("# Pipeline backlog") == 1
    assert text.index("first") < text.index("second")


# ── the email ──────────────────────────────────────────────────────────────────

def _full_recap():
    closed = [_closed("NVDA", 6.0, 78, ["RSI", "volume-vs-avg"], "SHORT_TERM", "target", "Tech"),
              _closed("MU", -7.0, 82, ["RSI"], "SCALP", "hard_stop", "Tech"),
              _closed("XOM", 2.0, 61, ["MA20"], "LONG_TERM", "model_sell", "Energy"),
              _closed("KO", -1.0, 55, ["MACD"], "SHORT_TERM", "trailing_stop", "Staples")]
    sigs = [_signal("NVDA", "executed", fwd_1d=1.1),
            _signal("PLTR", "rejected", gate_reason="sector lockout: Tech", fwd_1d=-2.4),
            _signal("SOFI", "rejected", gate_reason="sector lockout: Tech", fwd_1d=-1.0),
            _signal("F", "rejected", gate_reason="weekly trade cap (3)", fwd_1d=-0.5),
            _signal("T", "advisory", fwd_1d=0.2)]
    return {
        "week_label": "week of 2026-07-20", "narrative": "Choppy.",
        "changes": ["require 1.5x volume on any breakout BUY"],
        "mistakes": ["MU: sized a SCALP like a swing"], "successes": ["NVDA breakout worked"],
        "closed": closed, "open": [{"ticker": "AAPL", "entry": 200, "last": 204, "pnl_pct": 2.0}],
        "gradings": [{"ticker": "MU", "verdict": "LOSS", "notes": ["high-conviction loss"]}],
        "analytics": journal.week_analytics(closed, sigs),
        "signal_review": "Four ideas, one filled; the three blocked all fell.",
        "pipeline_changes": [{"change": "cache daily bars", "why": "6 redundant fetches/cycle",
                              "effort": "small"}],
        "week_ahead": {"events": [{"when": "Tue 08:30", "title": "CPI m/m", "country": "USD",
                                   "impact": "high"}],
                       "speeches": [{"when": "Wed 14:00", "title": "Powell speaks"}]},
        "stats": {"equity_start": 10000, "equity_end": 10050, "week_return_pct": 0.5,
                  "realized_usd": 50, "n_closed": 4, "n_opened": 2, "n_open": 1,
                  "win_rate": 50.0, "best": "NVDA +6.0%", "worst": "MU -7.0%",
                  "total_return_pct": 0.5},
    }


def test_the_email_carries_every_new_section_in_both_bodies():
    subject, text, html = notify.build_weekly_email(_full_recap(), datetime(2026, 7, 24))
    assert "+0.50%" in subject
    for body in (text, html):
        assert "cache daily bars" in body                  # pipeline backlog
        assert "CPI m/m" in body                           # week ahead
        assert "Powell speaks" in body
        assert "PLTR" in body                              # a signal that never filled
        assert "sector lockout: Tech" in body              # why it did not
        assert "overconfident" in body or "too few" in body  # the calibration read
        assert "hard_stop" in body                         # exit-kind cut
    assert "the gates helped this week" in text


def test_a_dead_quiet_week_renders_without_blowing_up():
    """No trades, no signals, no proposals — a legitimate result, not an error path."""
    recap = {"week_label": "week of 2026-07-20", "narrative": None, "changes": [],
             "mistakes": [], "successes": [], "closed": [], "open": [], "gradings": [],
             "analytics": journal.week_analytics([], []), "pipeline_changes": [],
             "week_ahead": {},
             "stats": {"equity_start": 10000, "equity_end": 10000, "week_return_pct": 0.0,
                       "realized_usd": 0, "n_closed": 0, "n_opened": 0, "n_open": 0,
                       "win_rate": None, "best": "—", "worst": "—", "total_return_pct": 0.0}}
    subject, text, html = notify.build_weekly_email(recap, datetime(2026, 7, 24))
    assert "No signals logged this week." in html
    assert "No pipeline changes proposed" in html
    assert "No high-impact events" in html
    assert "WEEK AHEAD" not in text          # nothing to say, so the section is absent


def test_the_obsidian_recap_note_mirrors_the_email(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LIND_BRAIN", str(tmp_path), raising=False)
    path = journal.write_weekly_recap(_full_recap(), datetime(2026, 7, 24))
    assert path, "recap note was not written"
    md = Path(path).read_text(encoding="utf-8")
    assert "- [ ] **cache daily bars**" in md
    assert "## Week ahead" in md
    assert "CPI m/m" in md
    assert "PLTR" in md
    assert "[[Trade Journal Index]]" in md


# ── pace lands in the recap ──────────────────────────────────────────────────────

def test_the_recap_email_reports_the_week_against_the_pace_target():
    """A green week on one trade and a green week on five are different weeks. The recap has to
    say which one it was, or the Friday review has nothing to diagnose."""
    _, text, html = notify.build_weekly_email(_full_recap(), datetime(2026, 7, 24))
    assert f"2 / {config.WEEKLY_TRADE_TARGET} target" in text
    assert f"/ {config.WEEKLY_TRADE_TARGET} target" in html


def test_a_recap_built_from_the_ledger_carries_the_pace_numbers(tmp_path):
    from brain.portfolio import Portfolio
    pf = Portfolio(path=tmp_path / "PORTFOLIO.json")
    recap = journal.build_recap(pf, "2026-07-20T00:00:00", prices={})
    assert recap["stats"]["weekly_trade_target"] == config.WEEKLY_TRADE_TARGET
    assert recap["stats"]["pace_gap"] == -config.WEEKLY_TRADE_TARGET   # nothing opened


# ── one week, two books ─────────────────────────────────────────────────────────

def test_the_crypto_book_is_graded_against_its_own_pace(tmp_path):
    """It used to read the module constant, which would have marked a crypto book that hit its
    own target as three trades behind the equity one."""
    from brain.portfolio import Portfolio
    pf = Portfolio(path=tmp_path / "PORTFOLIO_CRYPTO.json", rules=config.CRYPTO_RULES)
    recap = journal.build_recap(pf, "2026-07-20T00:00:00", prices={})
    assert recap["book"] == config.CRYPTO_RULES.name
    assert recap["stats"]["weekly_trade_target"] == config.CRYPTO_RULES.weekly_trade_target


def test_the_review_packet_carries_both_ledgers():
    """Both books keep the same hours now, so the Friday review reads one week, not two."""
    recap = _full_recap()
    recap["crypto"] = {**_full_recap(), "book": "crypto"}
    packet = json.loads(journal.recap_packet(recap))
    assert packet["crypto_book"]["book"] == "crypto"
    assert packet["crypto_book"]["stats"]["n_closed"] == 4
    assert "week_ahead" in packet and "week_ahead" not in packet["crypto_book"]


def test_a_missing_crypto_ledger_leaves_the_packet_valid():
    packet = json.loads(journal.recap_packet(_full_recap()))
    assert "crypto_book" not in packet


def test_the_recap_email_shows_the_crypto_week_beside_the_equity_one():
    recap = _full_recap()
    recap["crypto"] = {**_full_recap(), "book": "crypto",
                       "closed": [_closed("SOL-USD", 9.0, 71, ["RSI"], "SHORT_TERM",
                                          "target", "L1")]}
    _, text, html = notify.build_weekly_email(recap, datetime(2026, 7, 24))
    assert "SOL-USD" in html and "SOL-USD" in text
    assert "Crypto book" in html
    assert "CRYPTO BOOK" in text


def test_the_recap_without_a_crypto_book_renders_unchanged():
    _, text, html = notify.build_weekly_email(_full_recap(), datetime(2026, 7, 24))
    assert "CRYPTO BOOK" not in text
    assert "Crypto book" not in html
