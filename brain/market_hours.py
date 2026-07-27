"""When the US equity market is actually open — the brain's single time authority.

The pipeline used to run on cron alone: fixed UTC times, and seven days a week for the overnight
job. That produced two separate problems. The obvious one is that cycles burned API quota on
Saturdays and on Christmas. The subtle one is worse — the close anchor and the Friday review were
pinned to 20:15 and 20:45 UTC, which is 16:15 and 16:45 ET in summer but 15:15 and 15:45 ET once
the clocks go back. So for four months of every year the two "after the close" runs fired *before*
the close, and the weekly recap summarised a week that had not finished.

Both problems disappear once the code, not the crontab, decides whether a run may proceed. Cron
becomes a proposal ("it is roughly this time of day"); gate() is the authority, and it reasons
about the real session, so an early close moves the close anchor with it and a DST change moves
nothing at all.

No third-party calendar is required. The NYSE holiday rules are stable and expressible, so they
live here in full. exchange_calendars is consulted only when it happens to be installed, and only
to *add* a closure our rules cannot predict (a presidential funeral, Hurricane Sandy) — it can
never open a day we consider shut.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta

from . import config

MON, TUE, WED, THU, FRI, SAT, SUN = range(7)

OPEN_TIME = time(9, 30)
CLOSE_TIME = time(16, 0)
EARLY_CLOSE_TIME = time(13, 0)     # half-days: 1:00 PM ET, not 4:00

# How far ahead of the open a pre-market anchor may run, and how long after the close the
# close anchor stays valid. Wide enough that a late cron tick or a slow previous run still
# lands inside the window; narrow enough that a badly-set crontab is caught rather than excused.
PRE_WINDOW_H = 3.5
POST_CLOSE_WINDOW_H = 4.0
# The week-ahead digest gets a wider runway than the daily pre-market anchor: it is the one email
# meant to be read *before* the week starts, so landing it a few hours ahead of the first open is
# the point, not a miss.
DIGEST_WINDOW_H = 5.0

# Closures no rule can derive. Add entries as the exchange announces them.
AD_HOC_CLOSURES: dict[date, str] = {
    date(2025, 1, 9): "national day of mourning (Jimmy Carter)",
}


def _easter(year: int) -> date:
    """Anonymous Gregorian computus — Good Friday is the only moveable NYSE holiday."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    lam = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * lam) // 451
    month = (h + lam - 7 * m + 114) // 31
    day = ((h + lam - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    nxt = date(year + (month == 12), (month % 12) + 1, 1)
    last = nxt - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _observed(d: date) -> date | None:
    """NYSE weekend rule: a Saturday holiday moves to the Friday before, a Sunday one to Monday.

    New Year's Day is the exception. When it lands on a Saturday the exchange simply stays open
    on December 31 rather than closing it, so the holiday vanishes instead of moving — hence the
    None return, which the caller drops.
    """
    if d.weekday() == SAT:
        return None if (d.month, d.day) == (1, 1) else d - timedelta(days=1)
    if d.weekday() == SUN:
        return d + timedelta(days=1)
    return d


def holidays(year: int) -> dict[date, str]:
    """Every day the NYSE is closed in `year`, mapped to why."""
    out: dict[date, str] = {}

    def add(d: date | None, name: str):
        if d is not None:
            out[d] = name

    add(_observed(date(year, 1, 1)), "New Year's Day")
    add(_nth_weekday(year, 1, MON, 3), "Martin Luther King Jr. Day")
    add(_nth_weekday(year, 2, MON, 3), "Washington's Birthday")
    add(_easter(year) - timedelta(days=2), "Good Friday")
    add(_last_weekday(year, 5, MON), "Memorial Day")
    add(_observed(date(year, 6, 19)), "Juneteenth")
    add(_observed(date(year, 7, 4)), "Independence Day")
    add(_nth_weekday(year, 9, MON, 1), "Labor Day")
    add(_nth_weekday(year, 11, THU, 4), "Thanksgiving")
    add(_observed(date(year, 12, 25)), "Christmas")
    for d, why in AD_HOC_CLOSURES.items():
        if d.year == year:
            out[d] = why
    return out


def early_closes(year: int) -> dict[date, str]:
    """The 1:00 PM ET half-days.

    Each is conditional: the exchange only shortens the day before a holiday when that day is
    itself a normal weekday. When Independence Day falls on a Saturday, for instance, July 3 is
    the holiday rather than a half-day, so the rule must not fire.
    """
    out: dict[date, str] = {}
    jul3, jul4 = date(year, 7, 3), date(year, 7, 4)
    if jul3.weekday() < SAT and jul4.weekday() < SAT:
        out[jul3] = "day before Independence Day"
    out[_nth_weekday(year, 11, THU, 4) + timedelta(days=1)] = "day after Thanksgiving"
    dec24, dec25 = date(year, 12, 24), date(year, 12, 25)
    if dec24.weekday() < SAT and dec25.weekday() < SAT:
        out[dec24] = "Christmas Eve"
    shut = holidays(year)
    return {d: why for d, why in out.items() if d not in shut}


def _extra_closure(d: date) -> str | None:
    """Ask exchange_calendars about closures the rules above cannot know.

    Optional dependency, consulted in one direction only — it may mark a day closed that we
    think is open, never the reverse. The rule table stays authoritative and the VPS runs
    correctly whether or not the package is installed.
    """
    try:
        import exchange_calendars
        import pandas as pd
    except Exception:
        return None
    try:
        if not exchange_calendars.get_calendar("XNYS").is_session(pd.Timestamp(d)):
            return "exchange holiday (exchange_calendars)"
    except Exception:
        return None
    return None


def closed_reason(d: date) -> str | None:
    """Why the market is shut on `d`, or None when it is a normal trading day."""
    if d.weekday() >= SAT:
        return "weekend"
    return holidays(d.year).get(d) or _extra_closure(d)


def is_trading_day(d: date) -> bool:
    return closed_reason(d) is None


def session_bounds(d: date) -> tuple[datetime, datetime] | None:
    """(open, close) as aware ET datetimes, or None when the market is shut that day."""
    if not is_trading_day(d):
        return None
    close_t = EARLY_CLOSE_TIME if d in early_closes(d.year) else CLOSE_TIME
    return (datetime.combine(d, OPEN_TIME, tzinfo=config.ET),
            datetime.combine(d, close_t, tzinfo=config.ET))


def is_open(now: datetime | None = None) -> bool:
    now = now or datetime.now(config.ET)
    b = session_bounds(now.date())
    return bool(b and b[0] <= now <= b[1])


def last_trading_day_of_week(d: date) -> date | None:
    """The day the trading week actually ends.

    Friday, normally — but Good Friday closes the exchange, so in that week the week ends on
    Thursday and the weekly recap has to move with it.
    """
    friday = d + timedelta(days=(FRI - d.weekday()) % 7)
    for back in range(5):
        cand = friday - timedelta(days=back)
        if is_trading_day(cand):
            return cand
    return None


def first_trading_day_of_week(d: date) -> date | None:
    """The day the trading week actually begins — the mirror of last_trading_day_of_week().

    Monday normally, but MLK Day, Presidents' Day, Memorial Day and Labor Day all fall on a
    Monday, and when one does the week starts on the Tuesday. The week-ahead digest keys off
    this rather than off `weekday() == MON`, so a holiday Monday postpones it instead of
    cancelling it.
    """
    monday = d - timedelta(days=d.weekday())
    for fwd in range(5):
        cand = monday + timedelta(days=fwd)
        if is_trading_day(cand):
            return cand
    return None


def sessions_left_this_week(d: date) -> int:
    """Trading days remaining in this week, counting today if the exchange is open today.

    The pace context the analyst is measured against: "2 trades so far, 3 sessions left" is a
    different situation from "2 trades so far, 1 session left", and the second one is the only
    one that should make it lean into a marginal setup. Counts calendar reality, so a Thursday
    holiday shortens the runway instead of quietly promising a session that never comes.

    A weekend rolls forward to the coming week (last_trading_day_of_week does), reading as five
    sessions rather than zero. Nothing calls this outside a session anyway — the gate has already
    refused the run — but "no time left" would be the wrong answer to give on a Sunday.
    """
    last = last_trading_day_of_week(d)
    if last is None or d > last:
        return 0
    return sum(1 for i in range((last - d).days + 1) if is_trading_day(d + timedelta(days=i)))


def entries_allowed(now: datetime | None = None) -> bool:
    """May the brain open *new* positions right now?

    Lind's rule, in one predicate: signals from the open to the close, then nothing new until the
    next open. Exits are excluded on purpose — this gates entries only, and risk management has
    no closing bell.

    It also happens to be the correct broker behaviour. An equity order sent to Alpaca outside
    the session does not fail; it sits `accepted` and queues for the next open, so a thesis
    formed at 18:00 would fill fifteen hours later at a price nobody looked at.
    """
    return is_open(now)


def entries_reason(now: datetime | None = None) -> str:
    """Why entries are or are not allowed — the line that ends up in the email and the log."""
    now = now or datetime.now(config.ET)
    if entries_allowed(now):
        return "session open — new entries allowed"
    shut = closed_reason(now.date())
    if shut:
        return f"market closed ({shut}) — recommendations only, nothing executes"
    open_dt, close_dt = session_bounds(now.date())
    if now < open_dt:
        return (f"pre-market — recommendations only until the {open_dt.strftime('%H:%M')} ET "
                f"open")
    return (f"after the {close_dt.strftime('%H:%M')} ET close — recommendations only, no new "
            f"entries until tomorrow's open")


def gate(mode: str, now: datetime | None = None) -> tuple[bool, str]:
    """May a run of `mode` proceed right now? Returns (allowed, human-readable reason).

    Every mode's window is defined relative to the real session rather than to a wall-clock
    time, which is what makes this immune to both DST and half-days:

      cycle, mid    inside regular trading hours
      pre           shortly before the open, and never after it — except on the week's first
                    trading day, where the week-ahead digest is the morning's email instead
      close         at or after the *real* close (13:00 ET on half-days), same day
      digest        before the open on the week's first trading day only
      weekly        at or after the close of the week's last trading day
      research      always — Lind asked for it, so the exchange's hours are not the constraint
    """
    now = now or datetime.now(config.ET)
    if mode == "research":
        return True, "on-demand run"

    today = now.date()
    if mode == "digest":
        # The one email that is *supposed* to arrive while the market is shut. It runs before the
        # week's first open, so it is gated on the coming session rather than on a live one.
        first = first_trading_day_of_week(today)
        if first is None:
            return False, "no trading day this week"
        if today != first:
            return False, (f"the week-ahead digest runs on the week's first trading day — that is "
                           f"{first.strftime('%a %b %d')}")
        open_dt, _ = session_bounds(today)
        if now >= open_dt:
            return False, "the session is already open — the week-ahead digest missed its window"
        mins = (open_dt - now).total_seconds() / 60
        if mins > DIGEST_WINDOW_H * 60:
            return False, f"too early — {int(mins)} min before the week's first open"
        return True, f"the week starts in {int(mins)} min"

    shut = closed_reason(today)
    if shut:
        return False, f"market closed ({shut})"

    open_dt, close_dt = session_bounds(today)
    early = early_closes(today.year).get(today)
    tag = f" — early close {close_dt.strftime('%H:%M')} ET, {early}" if early else ""

    if mode in ("cycle", "mid"):
        if now < open_dt:
            return False, f"pre-market — the session opens at {open_dt.strftime('%H:%M')} ET"
        if now > close_dt:
            return False, f"after the close{tag}"
        return True, f"regular trading hours{tag}"

    if mode == "pre":
        if today == first_trading_day_of_week(today):
            # Two pre-open emails on the same morning is exactly the noise Lind objected to. On
            # the week's first day the digest is the morning brief, and the analysis resumes with
            # the cycles once the bell rings.
            return False, "the week-ahead digest covers this morning"
        if now > open_dt:
            return False, "the session is already open — the pre-market anchor missed its window"
        mins = (open_dt - now).total_seconds() / 60
        if mins > PRE_WINDOW_H * 60:
            return False, f"too early — {int(mins)} min before the open"
        return True, f"pre-market, {int(mins)} min to the open"

    if mode == "close":
        if now < close_dt:
            return False, f"the session runs until {close_dt.strftime('%H:%M')} ET{tag}"
        if (now - close_dt).total_seconds() > POST_CLOSE_WINDOW_H * 3600:
            return False, f"too long after the {close_dt.strftime('%H:%M')} ET close"
        return True, f"after the close{tag}"

    if mode == "weekly":
        last = last_trading_day_of_week(today)
        if today != last:
            return False, (f"not the last trading day of the week — that is "
                           f"{last.strftime('%a %b %d') if last else 'unknown'}")
        if now < close_dt:
            return False, f"the week is not over — the session runs to {close_dt.strftime('%H:%M')} ET{tag}"
        return True, f"the trading week has closed{tag}"

    return True, "ungated mode"


def describe(now: datetime | None = None) -> str:
    """One line about the session, for logs and the top of the weekly recap."""
    now = now or datetime.now(config.ET)
    shut = closed_reason(now.date())
    if shut:
        return f"{now:%a %b %d} — market closed ({shut})"
    open_dt, close_dt = session_bounds(now.date())
    state = "open" if open_dt <= now <= close_dt else "closed"
    early = early_closes(now.date().year).get(now.date())
    half = f", half-day ({early})" if early else ""
    return (f"{now:%a %b %d} — session {open_dt.strftime('%H:%M')}–"
            f"{close_dt.strftime('%H:%M')} ET, currently {state}{half}")
