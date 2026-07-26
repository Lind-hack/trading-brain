"""The week-ahead digest email.

The old one was a single `<pre>` block — Lind's words: "the email needs to be more organized and
more readable with a better design not just text all over the place". The rewrite has structure,
so these tests pin the structure: the thesis board is visible with its falsifiers, the calendar
is grouped by day in human time rather than ISO, and the plain-text alternative carries the same
facts for a client that refuses HTML.

Run:  python -m pytest tests/test_digest_email.py -q
"""
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from brain import config, notify

NOW = datetime(2026, 7, 27, 8, 0, tzinfo=config.ET)

PSUM = {"equity": 10450.0, "cash": 4000.0, "total_return_pct": 4.5, "n_open": 2,
        "win_rate": 60.0, "new_trades_this_week": 1,
        "open_positions": [
            {"ticker": "MU", "trade_type": "LONG_TERM", "pnl_pct": 8.4, "value": 3200.0},
            {"ticker": "NVDA", "trade_type": "SHORT_TERM", "pnl_pct": -2.1, "value": 2100.0}]}

THESIS = {"id": "abc123", "theme": "DRAM undersupply from AI server build-out",
          "driver": "HBM demand is absorbing wafer capacity faster than fabs are added",
          "thesis": "Contract pricing stays elevated and the makers earn through the cycle",
          "tickers": ["MU", "WDC"], "horizon": "1-3 years", "conviction": 74,
          "age_days": 61, "needs_review": False,
          "invalidation": ["Contract DRAM pricing falls two quarters running"],
          "milestones": ["Micron guides gross margin above 45%"]}


def digest(**kw):
    base = {
        "week_label": "Jul 27, 2026",
        "session_line": "Generated Mon Jul 27, 08:00 ET · pre-market",
        "portfolio": PSUM,
        "theses": [THESIS],
        "regime": "Liquidity is loosening while the AI capex cycle is still absorbing supply.",
        "thesis_delta_line": "1 new, 2 re-scored",
        "events": [{"when": "2026-07-29T14:00:00-04:00", "title": "FOMC Statement",
                    "country": "USD", "impact": "high"}],
        "speeches": [{"when": "2026-07-29T14:30:00-04:00", "title": "FOMC Press Conference"}],
        "watchlist": ["MU", "WDC", "NVDA"],
        "last_week": {"n_signals": 7, "n_executed": 2, "n_rejected": 3,
                      "n_advisory": 2, "n_duplicate": 5},
        "session_plan": ["Signals run only while the market is open — 09:30–16:00 ET."],
    }
    base.update(kw)
    return base


def build(**kw):
    return notify.build_digest_email(digest(**kw), NOW)


# ── the shell ──────────────────────────────────────────────────────────────────

def test_the_subject_names_the_week():
    subject, _, _ = build()
    assert subject == "Market Brain: week ahead — Jul 27, 2026"


def test_the_disclaimer_is_on_both_parts():
    _, text, html = build()
    assert config.DISCLAIMER in text and config.DISCLAIMER in html


def test_it_is_no_longer_a_pre_block():
    """The specific thing Lind objected to."""
    _, _, html = build()
    assert "<pre" not in html
    assert html.count("<table") > 3


def test_the_top_line_shows_equity_positions_and_the_board_size():
    _, _, html = build()
    assert "$10,450" in html
    assert f"2/{config.MAX_POSITIONS}" in html
    assert f"1/{config.MAX_NEW_TRADES_PER_WEEK} new trades used" in html


# ── the thesis board ───────────────────────────────────────────────────────────

def test_the_board_shows_its_falsifiers():
    """A thesis you cannot see the falsifier for is one you cannot argue with."""
    _, text, html = build()
    assert "DRAM undersupply" in html and "Wrong if" in html
    assert "Contract DRAM pricing falls two quarters running" in html
    assert "wrong if:" in text


def test_the_board_shows_conviction_age_and_tickers():
    _, _, html = build()
    assert "74/100" in html and "61d on the board" in html
    assert "MU" in html and "WDC" in html


def test_a_stale_thesis_is_marked_rather_than_hidden():
    stale = {**THESIS, "needs_review": True}
    _, _, html = build(theses=[stale])
    assert "evidence has gone quiet" in html


def test_an_empty_board_says_so_plainly():
    """Holding no structural view is a legitimate answer, not a rendering gap."""
    _, _, html = build(theses=[], regime=None)
    assert "would rather hold none" in html


def test_the_regime_read_is_carried_into_both_parts():
    _, text, html = build()
    assert "Liquidity is loosening" in html
    assert "STRUCTURAL READ" in text


# ── the calendar ───────────────────────────────────────────────────────────────

def test_events_are_grouped_by_day_in_human_time():
    _, text, html = build()
    assert "Wed Jul 29" in html and "14:00" in html
    assert "2026-07-29T14:00:00-04:00" not in html, "raw ISO is what made the old one unreadable"
    assert "Wed Jul 29" in text


def test_an_event_listed_as_both_an_event_and_a_speech_appears_once():
    dupe = {"when": "2026-07-29T14:00:00-04:00", "title": "FOMC Statement"}
    _, _, html = build(speeches=[dupe])
    assert html.count("FOMC Statement") == 1


def test_an_empty_calendar_is_a_sentence_not_a_blank():
    _, _, html = build(events=[], speeches=[])
    assert "No high-impact events scheduled" in html


def test_an_unparseable_timestamp_does_not_crash_the_email():
    _, _, html = build(events=[{"when": "sometime", "title": "Mystery print", "country": "USD"}])
    assert "Mystery print" in html


# ── the rest ───────────────────────────────────────────────────────────────────

def test_last_week_counts_the_repeats_it_held_back():
    """Suppressed signals are invisible by design, so the digest is where they get accounted for."""
    _, text, html = build()
    assert "5 held back as repeats" in html
    assert "5 held back as repeats" in text


def test_the_schedule_is_spelled_out():
    """The point of the schedule change was that Lind could predict when the brain speaks."""
    _, text, html = build()
    assert "09:30–16:00 ET" in html and "09:30–16:00 ET" in text


def test_open_positions_are_listed_with_their_horizon():
    _, _, html = build()
    assert "LONG_TERM" in html and "+8.4%" in html and "-2.1%" in html


def test_a_flat_book_says_so():
    _, _, html = build(portfolio={**PSUM, "open_positions": [], "n_open": 0})
    assert "Flat into the week" in html


def test_the_whole_email_survives_a_minimal_digest():
    """A degraded run still has to produce a sendable email."""
    subject, text, html = notify.build_digest_email({"portfolio": {"equity": 10000.0}}, NOW)
    assert subject and text and html.startswith("<!DOCTYPE html>")
