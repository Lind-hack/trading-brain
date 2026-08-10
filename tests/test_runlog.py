"""The once-a-day guard, and the DST arithmetic it exists to survive.

Debian's cron has no CRON_TZ, so the crontab offers each anchor at both of its possible UTC
times and lets the gate discard the wrong one. In one DST regime both candidates are legal, and
without a guard the close anchor would fire twice — two Opus deep runs, two emails, one session.

The second half of this file walks the actual crontab times through the actual gate, in both
halves of the year, and asserts that exactly one run survives. That is the check that would have
caught the CRON_TZ assumption before it shipped.

Run:  python -m pytest tests/test_runlog.py -q
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import market_brain
from brain import config, market_hours as mh, runlog, supabase


@pytest.fixture
def memdir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    return tmp_path


WED = datetime(2026, 7, 29, 16, 15, tzinfo=config.ET)


# ── the marker ─────────────────────────────────────────────────────────────────

def test_a_fresh_day_has_nothing_recorded(memdir):
    assert runlog.already_ran("close", WED) is False


def test_a_recorded_run_blocks_the_second_tick(memdir):
    runlog.mark_ran("close", WED)
    assert runlog.already_ran("close", WED) is True


def test_yesterdays_run_does_not_block_today(memdir):
    runlog.mark_ran("close", WED)
    assert runlog.already_ran("close", WED + timedelta(days=1)) is False


def test_each_mode_is_tracked_separately(memdir):
    runlog.mark_ran("pre", WED)
    assert runlog.already_ran("pre", WED) is True
    assert runlog.already_ran("close", WED) is False
    assert runlog.already_ran("weekly", WED) is False


def test_the_cycle_is_never_guarded(memdir):
    """It is supposed to run every thirty minutes. Guarding it would stop the pipeline dead."""
    runlog.mark_ran("cycle", WED)
    assert runlog.already_ran("cycle", WED) is False
    assert not (memdir / ".run-log.json").exists()


def test_the_marker_survives_the_process(memdir):
    runlog.mark_ran("mid", WED)
    assert (memdir / ".run-log.json").exists()
    assert runlog.already_ran("mid", WED) is True


def test_a_corrupt_marker_is_treated_as_a_fresh_day(memdir):
    """Failing open costs at most one duplicate run. Failing closed costs the whole day."""
    (memdir / ".run-log.json").write_text("{not json", encoding="utf-8")
    assert runlog.already_ran("close", WED) is False


def test_an_unwritable_marker_does_not_take_down_the_run(memdir, monkeypatch):
    """A disk problem here costs one duplicate anchor. It must not cost the run itself."""
    def boom(*a, **kw):
        raise OSError("read-only file system")
    monkeypatch.setattr(runlog, "dumps", boom)
    runlog.mark_ran("close", WED)      # must not raise
    assert runlog.already_ran("close", WED) is False


# ── the wiring ─────────────────────────────────────────────────────────────────

@pytest.fixture
def wired(monkeypatch, memdir):
    calls = []
    monkeypatch.setattr(market_brain, "do_cycle",
                        lambda a, mode="cycle", force=False: calls.append(mode) or 0)
    monkeypatch.setattr(market_brain, "do_weekly_review", lambda a: calls.append("weekly") or 0)
    monkeypatch.setattr(supabase, "push_heartbeat", lambda *a: True)
    monkeypatch.setattr(market_brain.collect, "fundamentals_cache_save", lambda: None)
    return calls


def _invoke(monkeypatch, argv):
    monkeypatch.setattr(sys, "argv", ["market_brain.py"] + argv)
    return market_brain.main()


def test_the_second_close_anchor_of_the_day_does_no_work(monkeypatch, wired):
    monkeypatch.setattr(mh, "gate", lambda mode, now=None: (True, "after the close"))
    _invoke(monkeypatch, ["--anchor", "close"])
    _invoke(monkeypatch, ["--anchor", "close"])
    assert wired == ["close"]


def test_the_second_weekly_recap_of_the_day_does_no_work(monkeypatch, wired):
    monkeypatch.setattr(mh, "gate", lambda mode, now=None: (True, "the trading week has closed"))
    _invoke(monkeypatch, ["--weekly-review"])
    _invoke(monkeypatch, ["--weekly-review"])
    assert wired == ["weekly"]


def test_a_failed_anchor_is_retried_by_the_next_candidate_tick(monkeypatch, wired):
    """Recording before the work would turn one transient failure into a lost trading day."""
    monkeypatch.setattr(mh, "gate", lambda mode, now=None: (True, "after the close"))
    monkeypatch.setattr(market_brain, "do_cycle", lambda a, mode="cycle", force=False: 1)
    assert _invoke(monkeypatch, ["--anchor", "close"]) == 1
    monkeypatch.setattr(market_brain, "do_cycle",
                        lambda a, mode="cycle", force=False: wired.append(mode) or 0)
    _invoke(monkeypatch, ["--anchor", "close"])
    assert wired == ["close"]


def test_the_cycle_is_not_blocked_by_its_own_earlier_run(monkeypatch, wired):
    monkeypatch.setattr(mh, "gate", lambda mode, now=None: (True, "regular trading hours"))
    for _ in range(3):
        _invoke(monkeypatch, ["--cycle"])
    assert wired == ["cycle", "cycle", "cycle"]


def test_ignore_market_hours_also_ignores_the_run_log(monkeypatch, wired):
    """A manual backfill is Lind asking directly. It should not be refused by a marker file."""
    monkeypatch.setattr(mh, "gate", lambda mode, now=None: (True, "ok"))
    _invoke(monkeypatch, ["--anchor", "close"])
    _invoke(monkeypatch, ["--anchor", "close", "--ignore-market-hours"])
    assert wired == ["close", "close"]


# ── the crontab, walked through the real gate ──────────────────────────────────

def _utc_to_et(month, day, hh, mm):
    from datetime import timezone
    return datetime(2026, month, day, hh, mm, tzinfo=timezone.utc).astimezone(config.ET)


# (mode, the UTC times the crontab offers)
OFFERED = {
    "pre": [(12, 35), (13, 35)],
    "mid": [(16, 33)],
    "close": [(20, 15), (21, 15)],
}


@pytest.mark.parametrize("mode", ["pre", "mid", "close"])
@pytest.mark.parametrize("month,day,season", [(7, 29, "EDT"), (1, 28, "EST")])
def test_exactly_one_offered_tick_survives_in_each_season(memdir, mode, month, day, season):
    ran = []
    for hh, mm in OFFERED[mode]:
        now = _utc_to_et(month, day, hh, mm)
        if mh.gate(mode, now)[0] and not runlog.already_ran(mode, now):
            runlog.mark_ran(mode, now)
            ran.append(now.strftime("%H:%M"))
    assert len(ran) == 1, f"{mode} in {season} ran {len(ran)}x at {ran}"


def test_the_weekly_recap_runs_once_on_friday_and_never_earlier(memdir):
    ran = []
    for day in (27, 28, 29, 30, 31):                    # Mon–Fri, Jul 2026
        for hh, mm in [(20, 45), (21, 45)]:
            now = _utc_to_et(7, day, hh, mm)
            if mh.gate("weekly", now)[0] and not runlog.already_ran("weekly", now):
                runlog.mark_ran("weekly", now)
                ran.append(now.strftime("%a %H:%M"))
    assert len(ran) == 1 and ran[0].startswith("Fri")


# The digest's offered ticks, in UTC: the Sunday evening pair it now lands on, plus the weekday
# retries — an evening one for a holiday-shifted week, and the two pre-open ones.
DIGEST_SUNDAY = [(21, 5), (22, 5)]
DIGEST_WEEKDAY = [(12, 5), (13, 5), (21, 5)]


def _walk_digest(month, days, ran=None):
    """Every offered digest tick over `days`, gated and de-duplicated exactly as cron would."""
    ran = ran if ran is not None else []
    for day in days:
        for hh, mm in DIGEST_SUNDAY + DIGEST_WEEKDAY:
            now = _utc_to_et(month, day, hh, mm)
            if now.weekday() == 6 and (hh, mm) not in DIGEST_SUNDAY:
                continue
            if now.weekday() != 6 and (hh, mm) not in DIGEST_WEEKDAY:
                continue
            if mh.gate("digest", now)[0] and not runlog.already_ran("digest", now):
                runlog.mark_ran("digest", now)
                ran.append(now.strftime("%a %H:%M"))
    return ran


def test_the_digest_runs_once_on_the_eve_of_the_week(memdir):
    """Five offered ticks across six days — the run-log is what makes it one email."""
    ran = _walk_digest(7, (26, 27, 28, 29, 30, 31))     # Sun–Fri, Jul 2026
    assert len(ran) == 1 and ran[0].startswith("Sun"), ran


def test_a_dead_sunday_still_gets_the_week_ahead_out(memdir):
    """The Monday morning tick is the retry. It only fires when Sunday recorded nothing."""
    ran = _walk_digest(7, (27, 28, 29, 30, 31))         # Mon–Fri only: Sunday never ran
    assert len(ran) == 1 and ran[0].startswith("Mon"), ran


def test_sunday_and_monday_cannot_both_send(memdir):
    """The one failure a per-day stamp would have allowed: the digest's ticks span two dates."""
    runlog.mark_ran("digest", _utc_to_et(7, 26, 21, 5))  # Sunday evening, recorded
    assert runlog.already_ran("digest", _utc_to_et(7, 27, 12, 5)) is True
    assert _walk_digest(7, (27, 28, 29, 30, 31)) == []


def test_last_weeks_digest_does_not_block_this_one(memdir):
    """Stamping by week is only safe if the stamp actually rolls over."""
    runlog.mark_ran("digest", _utc_to_et(7, 26, 21, 5))
    assert runlog.already_ran("digest", _utc_to_et(8, 2, 21, 5)) is False


def test_the_digest_survives_a_holiday_monday(memdir):
    """Labor Day 2026 is Monday Sep 7, so the week starts Tuesday — and its eve is the Monday."""
    ran = _walk_digest(9, (6, 7, 8, 9, 10, 11))         # Sun–Fri, Sep 2026
    assert len(ran) == 1 and ran[0].startswith("Mon"), ran


def test_the_digest_tick_is_inside_the_window_in_winter_too(memdir):
    """21:05 UTC is 16:05 ET in January — still Sunday, which is all the eve check asks."""
    assert mh.gate("digest", _utc_to_et(1, 25, 21, 5))[0]   # Sunday, Jan 2026
    assert mh.gate("digest", _utc_to_et(1, 26, 12, 5))[0]   # Monday retry, 07:05 ET


def test_the_digest_and_the_pre_anchor_do_not_both_fire_on_monday(memdir):
    """One pre-open email on the week's first morning, not two."""
    fired = []
    for mode, offered in (("digest", [(12, 5), (13, 5)]), ("pre", [(12, 35), (13, 35)])):
        for hh, mm in offered:
            now = _utc_to_et(7, 27, hh, mm)             # Monday
            if mh.gate(mode, now)[0] and not runlog.already_ran(mode, now):
                runlog.mark_ran(mode, now)
                fired.append(mode)
    assert fired == ["digest"]


def test_the_cycle_window_covers_the_whole_session_in_both_seasons(memdir):
    """12–21 UTC must contain every half-hour of the session, summer and winter."""
    for month, day in ((7, 29), (1, 28)):
        offered = [_utc_to_et(month, day, hh, mm) for hh in range(12, 22) for mm in (3, 33)]
        allowed = [t for t in offered if mh.gate("cycle", t)[0]]
        assert allowed, f"no cycle tick survived on {month}/{day}"
        first, last = allowed[0], allowed[-1]
        open_dt, close_dt = mh.session_bounds(first.date())
        assert (first - open_dt).total_seconds() <= 30 * 60, "the open is not covered"
        assert (close_dt - last).total_seconds() <= 30 * 60, "the close is not covered"
