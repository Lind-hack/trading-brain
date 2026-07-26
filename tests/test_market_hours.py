"""The market is the schedule.

Two things are being pinned down here. The first is Lind's rule: no run on a Saturday, a Sunday
or a holiday, and no run outside the session. The second is the bug that rule exposed — the close
anchor and the weekly review were pinned to fixed UTC times, so from November through March they
fired at 15:15 and 15:45 ET, before the 16:00 close, and the "weekly" recap covered a week that
had not ended. Every gate decision here is expressed relative to the real session, so the same
assertions hold in January and in July.

Run:  python -m pytest tests/ -q
"""
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from brain import config, market_hours as mh


def et(y, m, d, hh=12, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=config.ET)


# ── the holiday table ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("d,name", [
    (date(2026, 1, 1), "New Year's Day"),
    (date(2026, 1, 19), "Martin Luther King Jr. Day"),
    (date(2026, 2, 16), "Washington's Birthday"),
    (date(2026, 4, 3), "Good Friday"),
    (date(2026, 5, 25), "Memorial Day"),
    (date(2026, 6, 19), "Juneteenth"),
    (date(2026, 7, 3), "Independence Day"),      # Jul 4 is a Saturday, so it moves to Friday
    (date(2026, 9, 7), "Labor Day"),
    (date(2026, 11, 26), "Thanksgiving"),
    (date(2026, 12, 25), "Christmas"),
])
def test_2026_holidays(d, name):
    assert mh.holidays(2026).get(d) == name
    assert not mh.is_trading_day(d)


def test_new_year_on_a_saturday_is_not_observed():
    """The one holiday that vanishes rather than moving: Jan 1 2028 is a Saturday."""
    assert date(2028, 1, 1).weekday() == 5
    assert date(2027, 12, 31) not in mh.holidays(2027)
    assert mh.is_trading_day(date(2027, 12, 31))


def test_a_sunday_holiday_moves_to_monday():
    """Jun 19 2027 is a Saturday; Jul 4 2027 is a Sunday, observed Monday the 5th."""
    assert mh.holidays(2027).get(date(2027, 7, 5)) == "Independence Day"


def test_good_friday_tracks_easter():
    assert mh._easter(2026) == date(2026, 4, 5)
    assert date(2026, 4, 3) in mh.holidays(2026)
    assert date(2027, 3, 26) in mh.holidays(2027)


# ── half-days ───────────────────────────────────────────────────────────────────

def test_half_days_close_at_one():
    day_after_thanksgiving = date(2026, 11, 27)
    assert day_after_thanksgiving in mh.early_closes(2026)
    _, close = mh.session_bounds(day_after_thanksgiving)
    assert close.hour == 13 and close.minute == 0


def test_christmas_eve_is_a_half_day_only_on_a_weekday():
    assert date(2026, 12, 24) in mh.early_closes(2026)     # Christmas falls Friday
    assert date(2027, 12, 24) not in mh.early_closes(2027)  # that year it is the holiday itself


def test_july_3_is_not_a_half_day_when_it_is_the_holiday():
    """2026: Jul 4 is a Saturday, so Jul 3 is the full holiday, not a 1:00 close."""
    assert date(2026, 7, 3) not in mh.early_closes(2026)


# ── the gate, by mode ───────────────────────────────────────────────────────────

def test_no_cycle_on_the_weekend():
    for d in (date(2026, 7, 25), date(2026, 7, 26)):   # Saturday, Sunday
        ok, why = mh.gate("cycle", et(d.year, d.month, d.day, 11, 3))
        assert not ok and "weekend" in why


def test_no_cycle_on_a_holiday():
    ok, why = mh.gate("cycle", et(2026, 12, 25, 11, 3))
    assert not ok and "Christmas" in why


def test_cycle_only_inside_the_session():
    wed = (2026, 7, 29)
    assert not mh.gate("cycle", et(*wed, 8, 3))[0]      # pre-market
    assert mh.gate("cycle", et(*wed, 9, 33))[0]         # first tick after the open
    assert mh.gate("cycle", et(*wed, 15, 33))[0]
    assert not mh.gate("cycle", et(*wed, 16, 3))[0]     # first tick after the close
    assert not mh.gate("cycle", et(*wed, 17, 33))[0]


def test_cycle_stops_at_one_on_a_half_day():
    friday = (2026, 11, 27)
    assert mh.gate("cycle", et(*friday, 12, 33))[0]
    ok, why = mh.gate("cycle", et(*friday, 13, 33))
    assert not ok and "early close" in why


def test_pre_anchor_runs_before_the_open_and_not_after():
    wed = (2026, 7, 29)
    assert mh.gate("pre", et(*wed, 8, 35))[0]
    assert not mh.gate("pre", et(*wed, 5, 0))[0]        # far too early to be a real pre run
    assert not mh.gate("pre", et(*wed, 10, 0))[0]       # session already open


def test_close_anchor_waits_for_the_actual_close():
    """The DST bug, stated as a test: 15:15 ET is never 'after the close'."""
    wed = (2026, 7, 29)
    assert not mh.gate("close", et(*wed, 15, 15))[0]
    assert mh.gate("close", et(*wed, 16, 15))[0]


def test_close_anchor_moves_with_a_half_day():
    friday = (2026, 11, 27)
    assert mh.gate("close", et(*friday, 13, 15))[0]     # 13:00 is the real close that day
    assert not mh.gate("close", et(*friday, 12, 15))[0]


def test_close_anchor_expires():
    """Hours after the close it is not a close run any more, it is a mistake."""
    assert not mh.gate("close", et(2026, 7, 29, 23, 0))[0]


def test_weekly_only_on_the_last_trading_day_after_the_close():
    assert not mh.gate("weekly", et(2026, 7, 30, 16, 45))[0]   # Thursday
    assert not mh.gate("weekly", et(2026, 7, 31, 15, 45))[0]   # Friday, still trading
    assert mh.gate("weekly", et(2026, 7, 31, 16, 45))[0]       # Friday, after the close


def test_weekly_moves_to_thursday_in_good_friday_week():
    """Apr 3 2026 is Good Friday, so the week closes on Thursday and the recap must follow."""
    assert mh.last_trading_day_of_week(date(2026, 4, 2)) == date(2026, 4, 2)
    assert mh.gate("weekly", et(2026, 4, 2, 16, 45))[0]
    assert not mh.gate("weekly", et(2026, 4, 3, 16, 45))[0]    # holiday: nothing runs


def test_research_is_never_gated():
    """On-demand runs are Lind's call — a closed market is not a reason to refuse him."""
    ok, _ = mh.gate("research", et(2026, 12, 25, 3, 0))
    assert ok


# ── DST, stated directly ────────────────────────────────────────────────────────

def test_the_session_is_the_same_local_time_in_both_halves_of_the_year():
    """The whole point of gating in code: 16:00 ET is 20:00 UTC in July and 21:00 UTC in January.

    A fixed-UTC crontab cannot express that. These bounds can.
    """
    _, july = mh.session_bounds(date(2026, 7, 29))
    _, january = mh.session_bounds(date(2026, 1, 28))
    assert july.hour == january.hour == 16
    assert july.utcoffset() != january.utcoffset()


def test_every_weekday_of_a_normal_week_is_a_trading_day():
    monday = date(2026, 7, 27)
    assert all(mh.is_trading_day(monday + timedelta(days=i)) for i in range(5))
    assert not mh.is_trading_day(monday + timedelta(days=5))
    assert not mh.is_trading_day(monday + timedelta(days=6))


# ── the week's first trading day ────────────────────────────────────────────────

def test_the_week_starts_on_monday_normally():
    for d in (date(2026, 7, 27), date(2026, 7, 29), date(2026, 7, 31)):
        assert mh.first_trading_day_of_week(d) == date(2026, 7, 27)


@pytest.mark.parametrize("holiday_monday,tuesday", [
    (date(2026, 1, 19), date(2026, 1, 20)),      # MLK Day
    (date(2026, 2, 16), date(2026, 2, 17)),      # Presidents' Day
    (date(2026, 5, 25), date(2026, 5, 26)),      # Memorial Day
    (date(2026, 9, 7), date(2026, 9, 8)),        # Labor Day
])
def test_a_holiday_monday_postpones_the_week_rather_than_cancelling_it(holiday_monday, tuesday):
    assert mh.first_trading_day_of_week(holiday_monday) == tuesday


# ── the week-ahead digest ───────────────────────────────────────────────────────

def test_the_digest_runs_before_the_first_open_of_the_week():
    mon = (2026, 7, 27)
    assert mh.gate("digest", et(*mon, 8, 0))[0]
    assert not mh.gate("digest", et(*mon, 3, 0))[0]        # too early to be this week's brief
    ok, why = mh.gate("digest", et(*mon, 10, 0))
    assert not ok and "already open" in why                 # missed its window


def test_the_digest_does_not_repeat_on_the_rest_of_the_week():
    for day in (28, 29, 30, 31):
        ok, why = mh.gate("digest", et(2026, 7, day, 8, 0))
        assert not ok and "first trading day" in why


def test_the_digest_waits_for_tuesday_when_monday_is_a_holiday():
    """Lind gets the week-ahead before the week starts — which is not always a Monday."""
    assert not mh.gate("digest", et(2026, 9, 7, 8, 0))[0]   # Labor Day
    assert mh.gate("digest", et(2026, 9, 8, 8, 0))[0]


def test_the_digest_and_the_pre_anchor_never_email_on_the_same_morning():
    """Two pre-open emails on one morning is the noise the schedule change was meant to remove."""
    mon = (2026, 7, 27)
    assert mh.gate("digest", et(*mon, 8, 35))[0]
    ok, why = mh.gate("pre", et(*mon, 8, 35))
    assert not ok and "digest" in why
    assert mh.gate("pre", et(2026, 7, 28, 8, 35))[0]        # Tuesday: the anchor is back


# ── the entry gate ──────────────────────────────────────────────────────────────

def test_new_entries_are_allowed_only_while_the_session_is_open():
    wed = (2026, 7, 29)
    assert mh.entries_allowed(et(*wed, 10, 0))
    assert not mh.entries_allowed(et(*wed, 8, 0))           # pre-market
    assert not mh.entries_allowed(et(*wed, 17, 0))          # after the close


def test_no_new_entries_at_all_on_a_closed_day():
    assert not mh.entries_allowed(et(2026, 7, 25, 12, 0))   # Saturday
    assert not mh.entries_allowed(et(2026, 12, 25, 12, 0))  # Christmas


def test_the_entry_gate_moves_with_a_half_day():
    friday = (2026, 11, 27)
    assert mh.entries_allowed(et(*friday, 12, 0))
    assert not mh.entries_allowed(et(*friday, 14, 0))       # 13:00 is the real close


def test_the_entry_reason_says_when_the_brain_will_trade_again():
    wed = (2026, 7, 29)
    assert "session open" in mh.entries_reason(et(*wed, 10, 0))
    assert "09:30" in mh.entries_reason(et(*wed, 8, 0))
    assert "no new entries until tomorrow" in mh.entries_reason(et(*wed, 17, 0))
    assert "market closed" in mh.entries_reason(et(2026, 12, 25, 12, 0))
