"""The long-horizon thesis board.

Everything else in this repo is rebuilt from scratch every thirty minutes. A 1-3 year structural
bet — Lind's RAM example: demand shifts, supply cannot answer inside a year, the listed makers
re-rate — cannot survive that, so it is the one thing the pipeline carries between runs.

These tests pin the two properties that make it trustworthy: a thesis is never silently deleted
(a wrong call is the most valuable record on the board), and a *cycle* can add evidence but can
never move conviction — that judgement happens twice a week and nowhere else.

Run:  python -m pytest tests/test_thesis.py -q
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from brain import config, thesis

NOW = datetime(2026, 7, 27, 8, 0, tzinfo=config.ET)


@pytest.fixture
def board_file(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    monkeypatch.setattr(thesis, "BOARD", tmp_path / "THESES.json")
    return tmp_path / "THESES.json"


def ram(**kw):
    """The user's own example, used as the canonical entry."""
    base = {
        "theme": "DRAM undersupply from AI server build-out",
        "driver": "Hyperscaler HBM demand is absorbing wafer capacity faster than fabs are added",
        "thesis": "Contract DRAM pricing stays elevated through the cycle, and the three makers "
                  "earn through it rather than competing it away",
        "tickers": ["MU", "WDC"],
        "conviction": 72,
        "invalidation": ["Contract DRAM pricing falls two quarters running",
                         "A fourth entrant announces greenfield capacity"],
        "milestones": ["Micron guides gross margin above 45%"],
    }
    base.update(kw)
    return base


# ── persistence ────────────────────────────────────────────────────────────────

def test_a_missing_board_reads_as_empty(board_file):
    assert thesis.load()["theses"] == []


def test_a_corrupt_board_does_not_take_down_the_run(board_file):
    """A cycle must survive a bad file — losing the board is bad, losing the day is worse."""
    board_file.write_text("{not json", encoding="utf-8")
    assert thesis.load()["theses"] == []


def test_the_board_survives_the_process(board_file):
    thesis.merge([ram()], now=NOW)
    assert thesis.active(thesis.load())[0]["theme"].startswith("DRAM")


# ── merge ──────────────────────────────────────────────────────────────────────

def test_a_new_thesis_is_added_with_its_falsifiers(board_file):
    board, added, updated, closed = thesis.merge([ram()], now=NOW, save_board=False)
    assert (added, updated, closed) == (1, 0, 0)
    t = board["theses"][0]
    assert t["tickers"] == ["MU", "WDC"] and t["primary_ticker"] == "MU"
    assert len(t["invalidation"]) == 2
    assert t["horizon"] == "1-3 years"


def test_a_restated_thesis_updates_rather_than_forking(board_file):
    """The model re-words its own thesis every week. That must not become two entries."""
    board, *_ = thesis.merge([ram()], now=NOW, save_board=False)
    board, added, updated, _ = thesis.merge(
        [ram(conviction=80, thesis="Pricing holds even as capacity lands")],
        board=board, now=NOW, save_board=False)
    assert (added, updated) == (0, 1)
    assert len(board["theses"]) == 1
    assert board["theses"][0]["conviction"] == 80


def test_first_seen_is_never_rewritten_by_an_update(board_file):
    """Age is the only evidence of whether a thesis has been patient or merely stubborn."""
    board, *_ = thesis.merge([ram()], now=NOW - timedelta(days=200), save_board=False)
    first = board["theses"][0]["first_seen"]
    board, *_ = thesis.merge([ram(conviction=60)], board=board, now=NOW, save_board=False)
    assert board["theses"][0]["first_seen"] == first


def test_a_retired_thesis_is_re_statused_not_deleted(board_file):
    """The record of a call that turned out wrong is the point of keeping a board."""
    board, *_ = thesis.merge([ram()], now=NOW, save_board=False)
    board, _, _, closed = thesis.merge(
        [ram(status="invalidated", close_reason="pricing rolled over two quarters running")],
        board=board, now=NOW, save_board=False)
    assert closed == 1
    t = board["theses"][0]
    assert t["status"] == "invalidated" and t["closed_reason"].startswith("pricing rolled")
    assert len(board["theses"]) == 1
    assert thesis.active(board) == []


def test_a_thesis_without_a_theme_is_dropped(board_file):
    board, added, *_ = thesis.merge([{"conviction": 90}, "not a dict"], now=NOW, save_board=False)
    assert added == 0 and board["theses"] == []


def test_conviction_is_clamped_to_the_scale(board_file):
    board, *_ = thesis.merge([ram(conviction=430)], now=NOW, save_board=False)
    assert board["theses"][0]["conviction"] == 100


def test_an_overfull_board_demotes_rather_than_deletes(board_file):
    """A board of thirty convictions is a board of none."""
    entries = [ram(theme=f"structural theme {i}", conviction=90 - i)
               for i in range(config.THESIS_MAX_ACTIVE + 4)]
    board, *_ = thesis.merge(entries, now=NOW, save_board=False)
    assert len(board["theses"]) == config.THESIS_MAX_ACTIVE + 4
    live = [t for t in board["theses"] if t["status"] == "active"]
    assert len(live) == config.THESIS_MAX_ACTIVE
    assert all(t["status"] == "watch" for t in board["theses"] if t not in live)


def test_a_low_conviction_thesis_falls_to_watch(board_file):
    board, *_ = thesis.merge([ram(conviction=config.THESIS_MIN_CONVICTION - 10)],
                             now=NOW, save_board=False)
    assert board["theses"][0]["status"] == "watch"


def test_a_thesis_with_no_recent_evidence_is_flagged_for_review(board_file):
    """An unfalsified thesis nobody revisits is just a bias with a ticker attached."""
    old = NOW - timedelta(days=config.THESIS_STALE_DAYS + 10)
    board, *_ = thesis.merge([ram()], now=old, save_board=False)
    thesis.flag_stale(board, NOW)
    assert board["theses"][0]["needs_review"] is True
    thesis.merge([ram(evidence=[{"note": "Micron guides margin above 45%"}])],
                 board=board, now=NOW, save_board=False)
    assert board["theses"][0]["needs_review"] is False


# ── evidence (what a cycle is allowed to do) ───────────────────────────────────

def test_a_cycle_can_attach_evidence_to_a_named_ticker(board_file):
    board, *_ = thesis.merge([ram()], now=NOW, save_board=False)
    n = thesis.attach_evidence("MU", [{"note": "Samsung delays a fab", "source": "Reuters"}],
                               board=board, now=NOW, save_board=False)
    assert n == 1
    assert board["theses"][0]["evidence"][-1]["source"] == "Reuters"


def test_a_cycle_cannot_move_conviction(board_file):
    """Gathering evidence is factual and continuous. Re-scoring is a judgement, twice a week."""
    board, *_ = thesis.merge([ram(conviction=72)], now=NOW, save_board=False)
    thesis.attach_evidence("MU", [{"note": "pricing up again", "stance": "supports"}],
                           board=board, now=NOW, save_board=False)
    assert board["theses"][0]["conviction"] == 72


def test_the_same_headline_twice_is_not_new_evidence(board_file):
    board, *_ = thesis.merge([ram()], now=NOW, save_board=False)
    item = [{"note": "Samsung delays a fab"}]
    thesis.attach_evidence("MU", item, board=board, now=NOW, save_board=False)
    assert thesis.attach_evidence("MU", item, board=board, now=NOW, save_board=False) == 0


def test_evidence_for_an_unnamed_ticker_goes_nowhere(board_file):
    board, *_ = thesis.merge([ram()], now=NOW, save_board=False)
    assert thesis.attach_evidence("KO", [{"note": "unrelated"}],
                                  board=board, now=NOW, save_board=False) == 0


def test_the_evidence_log_is_capped(board_file):
    board, *_ = thesis.merge([ram()], now=NOW, save_board=False)
    for i in range(60):
        thesis.attach_evidence("MU", [{"note": f"datapoint {i}"}],
                               board=board, now=NOW, save_board=False)
    assert len(board["theses"][0]["evidence"]) == 40


# ── reading ────────────────────────────────────────────────────────────────────

def test_for_ticker_finds_every_live_thesis_naming_it(board_file):
    board, *_ = thesis.merge([ram(), ram(theme="Grid buildout", tickers=["MU", "ETN"])],
                             now=NOW, save_board=False)
    assert len(thesis.for_ticker("MU", board)) == 2
    assert thesis.for_ticker("mu", board), "ticker matching must be case-insensitive"


def test_the_packet_is_trimmed_and_ordered_by_conviction(board_file):
    board, *_ = thesis.merge([ram(conviction=60),
                              ram(theme="Grid buildout", conviction=88)],
                             now=NOW, save_board=False)
    packet = thesis.for_packet(board)
    assert [t["conviction"] for t in packet] == [88, 60]
    assert "evidence" not in packet[0] and "recent_evidence" in packet[0]
    assert packet[0]["age_days"] is not None


# ── signal binding ─────────────────────────────────────────────────────────────

def test_a_long_term_signal_is_bound_to_its_thesis(board_file):
    board, *_ = thesis.merge([ram()], now=NOW, save_board=False)
    analysis = {"signals": [{"ticker": "MU", "trade_type": "LONG_TERM"}]}
    thesis.annotate_signals(analysis, board)
    sig = analysis["signals"][0]
    assert sig["thesis"]["theme"].startswith("DRAM")
    assert sig["thesis_id"] == board["theses"][0]["id"]
    assert "unanchored_long_term" not in analysis


def test_a_long_term_signal_with_no_thesis_is_surfaced(board_file):
    """A long-term call with nothing structural behind it is a mislabelled swing trade."""
    board, *_ = thesis.merge([ram()], now=NOW, save_board=False)
    analysis = {"signals": [{"ticker": "KO", "trade_type": "LONG_TERM"}]}
    thesis.annotate_signals(analysis, board)
    assert analysis["unanchored_long_term"] == ["KO"]
    assert analysis["signals"][0]["thesis"] is None


def test_short_horizon_signals_are_left_alone(board_file):
    board, *_ = thesis.merge([ram()], now=NOW, save_board=False)
    analysis = {"signals": [{"ticker": "MU", "trade_type": "SCALP"}]}
    thesis.annotate_signals(analysis, board)
    assert "thesis" not in analysis["signals"][0]


def test_summary_line_says_something_useful_when_empty(board_file):
    assert "No long-term theses" in thesis.summary_line(thesis.load())
