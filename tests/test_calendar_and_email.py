"""Regression tests for the three original bugs — and the calendar parsing behind one of them.

Two of the three were silent failures: the ForexFactory feed returned nothing useful, and every
email went to the wrong address. Neither raised, so neither showed up in a log. These tests are
the alarm that was missing.

Run:  python -m pytest tests/test_calendar_and_email.py -q
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from brain import collect, config, notify


def _iso(hours_from_now):
    return (datetime.now(config.UTC) + timedelta(hours=hours_from_now)).strftime(
        "%Y-%m-%dT%H:%M:%S%z")


def _ev(title, impact="High", country="USD", hours=5, **kw):
    row = {"title": title, "country": country, "impact": impact, "date": _iso(hours)}
    row.update(kw)
    return row


def _feed(monkeypatch, *pages):
    """Serve `pages` in order, one per URL the collector asks for."""
    served = []

    class _R:
        def __init__(self, rows):
            self._rows = rows

        def raise_for_status(self):
            pass

        def json(self):
            return self._rows

    def get(url, **kw):
        served.append(url)
        return _R(pages[min(len(served) - 1, len(pages) - 1)])

    monkeypatch.setattr(collect.requests, "get", get)
    return served


# ── the feed's own date format ─────────────────────────────────────────────────

def test_both_shapes_of_forexfactory_timestamp_parse():
    assert collect._parse_ff_dt("2026-07-29T08:30:00-04:00").hour == 8
    naive = collect._parse_ff_dt("2026-07-29T08:30:00")
    assert naive.tzinfo is not None, "a naive feed timestamp must be pinned to UTC, not left bare"


@pytest.mark.parametrize("raw", ["", None, "next tuesday", "2026-07-29 08:30:00"])
def test_an_unparseable_timestamp_is_none_not_a_crash(raw):
    assert collect._parse_ff_dt(raw) is None


# ── what gets kept ─────────────────────────────────────────────────────────────

def test_high_impact_events_are_kept_and_low_impact_dropped(monkeypatch):
    _feed(monkeypatch, [_ev("Core CPI m/m"), _ev("Loan Officer Survey", impact="Low")])
    cal = collect.forexfactory_calendar()
    assert [e["title"] for e in cal["events"]] == ["Core CPI m/m"]


def test_a_watched_keyword_survives_a_medium_impact_tag(monkeypatch):
    """FOMC tagged Medium is still FOMC. The keyword list exists because the tags are unreliable."""
    _feed(monkeypatch, [_ev("FOMC Meeting Minutes", impact="Medium")])
    assert collect.forexfactory_calendar()["events"]


def test_a_speech_lands_in_both_lists(monkeypatch):
    """Callers scan `events` for the tape and `speeches` to pre-fetch news — it must be in both."""
    _feed(monkeypatch, [_ev("Fed Chair Powell Speaks", impact="Low")])
    cal = collect.forexfactory_calendar()
    assert len(cal["speeches"]) == 1 and len(cal["events"]) == 1


def test_a_holiday_row_is_kept(monkeypatch):
    _feed(monkeypatch, [_ev("Bank Holiday", impact="Holiday")])
    assert collect.forexfactory_calendar()["events"]


# ── imminence ──────────────────────────────────────────────────────────────────

def test_imminent_is_the_screener_horizon_not_the_whole_week(monkeypatch):
    far = config.SCREEN_EVENT_HORIZON_H + 24
    _feed(monkeypatch, [_ev("Core CPI m/m", hours=1), _ev("NFP", hours=far)])
    cal = collect.forexfactory_calendar()
    assert [e["title"] for e in cal["imminent"]] == ["Core CPI m/m"]


def test_an_event_that_just_printed_is_still_imminent(monkeypatch):
    """The 30 minutes after a CPI release are the most tradeable of the week."""
    _feed(monkeypatch, [_ev("Core CPI m/m", hours=-0.25, actual="0.3%", forecast="0.2%")])
    cal = collect.forexfactory_calendar()
    assert cal["imminent"] and cal["imminent"][0]["actual"] == "0.3%"


def test_an_event_with_no_usable_time_is_never_imminent(monkeypatch):
    """`hours_away` of None used to be compared against a number. That raised, mid-cycle."""
    _feed(monkeypatch, [{"title": "Core CPI m/m", "country": "USD", "impact": "High",
                         "date": "who knows"}])
    cal = collect.forexfactory_calendar()
    assert cal["events"][0]["hours_away"] is None
    assert cal["imminent"] == []


def test_a_foreign_print_that_matched_a_keyword_does_not_force_a_deep_run(monkeypatch):
    """Both of these are real rows that escalated real cycles.

    "Unemployment Rate" and "CPI" are on the watch list because US titles vary. They also match
    Spain's regional print and the BOJ's, neither of which reprices anything this book trades.
    """
    _feed(monkeypatch, [_ev("Spanish Unemployment Rate", impact="Low", country="EUR", hours=1),
                        _ev("BOJ Core CPI y/y", impact="Low", country="JPY", hours=1)])
    cal = collect.forexfactory_calendar()
    assert len(cal["events"]) == 2, "the analyst should still see them in the packet"
    assert cal["imminent"] == []


def test_a_us_print_still_forces_one(monkeypatch):
    _feed(monkeypatch, [_ev("Core CPI m/m", country="USD", hours=1),
                        _ev("CPI y/y", country="AUD", hours=1)])
    assert [e["title"] for e in collect.forexfactory_calendar()["imminent"]] == ["Core CPI m/m"]


def test_the_escalating_countries_are_configurable(monkeypatch):
    monkeypatch.setattr(config, "CALENDAR_ESCALATE_COUNTRIES", {"USD", "EUR"})
    _feed(monkeypatch, [_ev("Main Refinancing Rate", country="EUR", hours=1)])
    assert len(collect.forexfactory_calendar()["imminent"]) == 1


def test_trump_soon_is_trump_within_a_day(monkeypatch):
    _feed(monkeypatch, [_ev("Trump Speaks", impact="Low", hours=6),
                        _ev("Trump Speaks", impact="Low", hours=100),
                        _ev("Fed Chair Powell Speaks", impact="Low", hours=6)])
    cal = collect.forexfactory_calendar()
    assert len(cal["speeches"]) == 3
    assert len(cal["trump_soon"]) == 1


# ── the two feeds ──────────────────────────────────────────────────────────────

def test_the_retired_nextweek_feed_is_not_requested(monkeypatch):
    """faireconomy 404s that URL now, so `FF_NEXTWEEK_URL` is None and must stay unrequested.

    The thisweek feed's weeks start on Sunday, so the Sunday digest already sees the week ahead.
    If someone ever restores the URL, the test below is the one that has to keep working.
    """
    served = _feed(monkeypatch, [_ev("Core CPI m/m")])
    collect.forexfactory_calendar(include_next_week=True)
    assert served == [config.FF_FEED_URL]
    assert None not in served


def test_a_restored_nextweek_feed_would_be_fetched(monkeypatch):
    monkeypatch.setattr(config, "FF_NEXTWEEK_URL", "https://example.test/nextweek.json")
    served = _feed(monkeypatch, [_ev("Core CPI m/m")], [_ev("NFP")])
    cal = collect.forexfactory_calendar(include_next_week=True)
    assert len(served) == 2
    assert {e["title"] for e in cal["events"]} == {"Core CPI m/m", "NFP"}


def test_an_event_on_both_feeds_is_reported_once(monkeypatch):
    row = _ev("Core CPI m/m")
    _feed(monkeypatch, [row], [row])
    cal = collect.forexfactory_calendar(include_next_week=True)
    assert len(cal["events"]) == 1


def test_a_dead_feed_returns_an_empty_calendar_rather_than_exploding(monkeypatch):
    def boom(url, **kw):
        raise OSError("connection reset")
    monkeypatch.setattr(collect.requests, "get", boom)
    cal = collect.forexfactory_calendar()
    assert cal == {"events": [], "speeches": [], "imminent": [], "trump_soon": []}


def test_a_feed_that_returns_null_is_treated_as_empty(monkeypatch):
    class _R:
        def raise_for_status(self):
            pass

        def json(self):
            return None
    monkeypatch.setattr(collect.requests, "get", lambda url, **kw: _R())
    assert collect.forexfactory_calendar()["events"] == []


# ── the recipient ──────────────────────────────────────────────────────────────

def test_the_default_recipient_is_linds_address():
    assert config.RECIPIENT == "lindsylqa@gmail.com"


def test_the_envelope_and_the_header_agree(monkeypatch):
    """The original bug: mail went out addressed to one place and delivered to another."""
    sent = {}

    class _SMTP:
        def __init__(self, host, port):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def login(self, user, pw):
            pass

        def sendmail(self, frm, to, body):
            sent["to"], sent["body"] = to, body

    monkeypatch.setattr(config, "GMAIL_USER", "bot@example.com")
    monkeypatch.setattr(config, "GMAIL_PASS", "app-password")
    monkeypatch.setattr(notify.smtplib, "SMTP_SSL", _SMTP)

    assert notify.send_email("subject", "text", "<p>html</p>") is True
    assert sent["to"] == config.RECIPIENT
    assert f"To: {config.RECIPIENT}" in sent["body"]


def test_no_credentials_means_no_send_and_no_crash(monkeypatch):
    monkeypatch.setattr(config, "GMAIL_USER", "")
    monkeypatch.setattr(notify.smtplib, "SMTP_SSL",
                        lambda *a, **k: pytest.fail("tried to connect with no credentials"))
    assert notify.send_email("s", "t", "<p>h</p>") is False
