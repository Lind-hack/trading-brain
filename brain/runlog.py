"""Which once-a-day runs have already happened today.

This exists because of one fact about the box the brain runs on: Debian's cron (3.0pl1) has no
`CRON_TZ`. Only cronie does. So the crontab cannot say "16:15 Eastern" — it can only say a UTC
hour, and the UTC hour of the Eastern close moves by one in November and back in March.

The way out is to let cron offer *both* candidate ticks and let `market_hours.gate()` throw away
the wrong one. That works for the cycle, which is supposed to run repeatedly. It does not work for
the anchors and the weekly recap: in one of the two DST regimes both candidate ticks land inside
the same window, and the run would happen twice — two Opus deep runs, two emails, two sets of
paper trades proposed off the same session.

So: the gate answers "is this a legal time for a `close` run?", and this module answers "have we
already done today's?". A run is recorded only once it has finished successfully, which means a
crashed run is retried by the next candidate tick rather than being silently skipped.

The file is a marker, not memory — it is regenerable, it changes daily, and it is gitignored so it
never shows up as noise in the brain-memory commit history.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime

from . import config
from .jsonio import dumps

# The cycle is deliberately absent: it is meant to run every 30 minutes.
# `digest` belongs here for the same reason as the anchors — its cron offers two candidate ticks
# for one Monday morning, and Lind should get one week-ahead email, not two.
ONCE_PER_DAY = ("pre", "mid", "close", "weekly", "digest")


def _path():
    return config.MEMORY_DIR / ".run-log.json"


def _load():
    """The marker file, or {} — a missing or corrupt file means "nothing has run today"."""
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as e:
        print(f"[warn] runlog unreadable ({e}); treating today as fresh", file=sys.stderr)
        return {}
    return data if isinstance(data, dict) else {}


def already_ran(mode: str, now: datetime | None = None) -> bool:
    """Has `mode` already completed today (Eastern), so this tick is the duplicate one?"""
    if mode not in ONCE_PER_DAY:
        return False
    now = now or datetime.now(config.ET)
    return _load().get(mode) == now.date().isoformat()


def mark_ran(mode: str, now: datetime | None = None) -> None:
    """Record `mode` as done for today. Called after the run succeeds, never before."""
    if mode not in ONCE_PER_DAY:
        return
    now = now or datetime.now(config.ET)
    data = _load()
    data[mode] = now.date().isoformat()
    try:
        config.MEMORY_DIR.mkdir(parents=True, exist_ok=True)
        _path().write_text(dumps(data, indent=2), encoding="utf-8")
    except Exception as e:  # pragma: no cover - disk
        # A marker we could not write means at worst a duplicate run tomorrow, so this must never
        # be the thing that takes down a cycle that otherwise worked.
        print(f"[warn] runlog: {e}", file=sys.stderr)
