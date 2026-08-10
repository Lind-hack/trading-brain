"""The delta gate: what stops Opus escalating on 100% of cycles.

The bug these cover is not a wrong threshold, it is a wrong question. Screener triggers are
standing conditions — 27 of ~30 equity names flagged on every cycle in the week of 2026-07-27 —
so any bar on the *level* is cleared by the tape's resting state. These assert the gate fires on
the *change* instead, and that perishable evidence is not made to wait behind a cooldown.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from brain import config, escalate


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    """Never touch the real brain-memory marker from a test."""
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    return tmp_path


def _screen(scores=None, calendar_flags=(), held=()):
    return {
        "triggers": [{"ticker": t, "score": s, "reasons": ["x"], "held": t in held}
                     for t, s in (scores or {}).items()],
        "calendar_flags": list(calendar_flags),
        "escalate": True,
        "why": [],
    }


def _now():
    from datetime import datetime
    return datetime(2026, 8, 3, 10, 30, tzinfo=config.ET)


def test_the_first_run_of_a_book_always_escalates():
    ok, why, _ = escalate.decide("stock", _screen({"AAPL": 8}), now=_now())
    assert ok is True
    assert "no deep run on record" in why[0]


def test_an_unchanged_tape_does_not_buy_a_second_run():
    """The exact failure: the same 28 names, the same scores, thirty minutes later."""
    now = _now()
    screen = _screen({"AAPL": 8, "MSFT": 7, "NVDA": 9})
    _, _, meta = escalate.decide("stock", screen, now=now)
    escalate.commit("stock", meta, now=now)

    ok, why, _ = escalate.decide("stock", screen, now=now + timedelta(minutes=30))
    assert ok is False
    assert "nothing new" in why[0]


def test_a_newly_flagged_name_escalates_once_the_cooldown_has_passed():
    now = _now()
    _, _, meta = escalate.decide("stock", _screen({"AAPL": 8}), now=now)
    escalate.commit("stock", meta, now=now)

    later = _screen({"AAPL": 8, "TSLA": 7})
    early = escalate.decide("stock", later, now=now + timedelta(minutes=30))
    assert early[0] is False, "chart evidence waits behind the cooldown"

    ok, why, _ = escalate.decide("stock", later, now=now + timedelta(minutes=90))
    assert ok is True
    assert "TSLA newly flagged" in why[0]


def test_a_one_point_drift_is_not_new_evidence():
    """One extra detector firing is noise; two points is the setup actually developing."""
    now = _now()
    _, _, meta = escalate.decide("stock", _screen({"AAPL": 7}), now=now)
    escalate.commit("stock", meta, now=now)

    drift = escalate.decide("stock", _screen({"AAPL": 8}), now=now + timedelta(minutes=90))
    assert drift[0] is False
    real = escalate.decide("stock", _screen({"AAPL": 9}), now=now + timedelta(minutes=90))
    assert real[0] is True


def test_a_new_calendar_event_jumps_the_cooldown():
    """CPI landing five minutes after a deep run is exactly what the deep run is for."""
    now = _now()
    _, _, meta = escalate.decide("stock", _screen({"AAPL": 8}), now=now)
    escalate.commit("stock", meta, now=now)

    with_cpi = _screen({"AAPL": 8}, calendar_flags=["Core CPI m/m (High) in 0.5h"])
    ok, why, _ = escalate.decide("stock", with_cpi, now=now + timedelta(minutes=5))
    assert ok is True
    assert "new event evidence" in why[0]


def test_the_same_event_counting_down_is_not_new_each_cycle():
    """The flag text carries a live countdown. Comparing raw strings made every event new forever."""
    now = _now()
    first = _screen({"AAPL": 8}, calendar_flags=["Core CPI m/m (High) in 2.5h"])
    _, _, meta = escalate.decide("stock", first, now=now)
    escalate.commit("stock", meta, now=now)

    later = _screen({"AAPL": 8}, calendar_flags=["Core CPI m/m (High) in 1.5h"])
    assert escalate.decide("stock", later, now=now + timedelta(minutes=30))[0] is False


def test_a_fresh_news_reason_jumps_the_cooldown():
    now = _now()
    _, _, meta = escalate.decide("stock", _screen({"AAPL": 8}), now=now)
    escalate.commit("stock", meta, now=now)

    ok, _, _ = escalate.decide(
        "stock", _screen({"AAPL": 8}), now=now + timedelta(minutes=10),
        news_reasons=["NVDA: high-materiality regulatory news (sentiment -80, under-covered)"])
    assert ok is True


def test_the_same_headline_re_read_is_not_a_second_reason():
    now = _now()
    reason = ["NVDA: high-materiality regulatory news (sentiment -80, under-covered)"]
    _, _, meta = escalate.decide("stock", _screen({"AAPL": 8}), now=now, news_reasons=reason)
    escalate.commit("stock", meta, now=now)

    drifted = ["NVDA: high-materiality regulatory news (sentiment -76, under-covered)"]
    assert escalate.decide("stock", _screen({"AAPL": 8}), now=now + timedelta(minutes=30),
                           news_reasons=drifted)[0] is False


def test_a_quiet_tape_is_still_re_read_at_the_floor():
    now = _now()
    screen = _screen({"AAPL": 8})
    _, _, meta = escalate.decide("stock", screen, now=now)
    escalate.commit("stock", meta, now=now)

    ok, why, _ = escalate.decide("stock", screen,
                                 now=now + timedelta(minutes=config.DEEP_MAX_GAP_MIN + 1))
    assert ok is True
    assert "since the last deep read" in why[0]


def test_an_anchor_run_is_never_gated():
    now = _now()
    _, _, meta = escalate.decide("stock", _screen({"AAPL": 8}), now=now)
    escalate.commit("stock", meta, now=now)
    ok, why, _ = escalate.decide("stock", _screen({"AAPL": 8}), now=now + timedelta(minutes=1),
                                 force=True)
    assert ok is True
    assert why == ["scheduled anchor run"]


def test_the_two_books_do_not_share_a_cooldown():
    now = _now()
    _, _, meta = escalate.decide("stock", _screen({"AAPL": 8}), now=now)
    escalate.commit("stock", meta, now=now)
    assert escalate.decide("crypto", _screen({"BTC-USD": 8}), now=now)[0] is True


def test_a_failed_run_is_not_recorded_as_a_read():
    """commit() is called after the deep run, so a crash leaves the next cycle free to retry."""
    now = _now()
    screen = _screen({"AAPL": 8})
    escalate.decide("stock", screen, now=now)          # decided, then the run died
    assert escalate.decide("stock", screen, now=now + timedelta(minutes=30))[0] is True


def test_triggers_below_the_bar_are_not_evidence():
    ev = escalate.evidence(_screen({"AAPL": 3, "MSFT": 8}), bar=6)
    assert "chart:MSFT" in ev
    assert "chart:AAPL" not in ev
