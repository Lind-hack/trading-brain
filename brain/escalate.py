"""Is this cycle worth an Opus deep run?

The screener answers "is anything happening", which turned out to be the wrong question. Over the
week of 2026-07-27 the equity screener flagged **27–28 of ~30 names on every single cycle**, top
score 8–9, and the crypto screener flagged 8–9 of 18 with a top score of 6 against an escalation
bar of 5. Both books escalated 100% of cycles — 63/63 equity, 118/118 crypto — and 85 of those deep
runs returned no signal at all. At a measured $0.027–$0.068 and 9–12k cache-creation tokens per
invocation, that is the single largest line in the bill, spent mostly on re-reading a tape that had
not changed since the last read.

The reason is structural, not a bad threshold. A 20-day breakout is still a 20-day breakout thirty
minutes later; RSI 78 is still 78. Screener triggers are **standing conditions**, so "something
flagged" is a constant and cannot ration anything. What is actually new in a cycle is:

  * a name that was not flagged before, or whose score stepped up materially;
  * a calendar event that has entered the window;
  * a headline Haiku just read that it had not read before.

So this module escalates on the **delta**, not the level. It keeps the last deep run's evidence per
book and compares. Two tiers, because not all evidence deserves to jump the queue:

  * **Events override the cooldown** — a new calendar flag or a new news reason is exactly the thing
    the deep run exists to catch, and making it wait 60 minutes would defeat the purpose.
  * **Chart evidence respects it** — a newly flagged name is worth a look, but not four looks an
    hour, and the setup will still be there.

Plus a floor: `max_gap_min` since the last deep run escalates on its own, so a genuinely quiet tape
still gets periodically re-read rather than going dark for a session.

The state file is a marker, not memory — regenerable, changes constantly, gitignored.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime

from . import config
from .jsonio import dumps

_DIGITS = re.compile(r"[-+]?\d+(?:\.\d+)?")


def _path():
    return config.MEMORY_DIR / ".deep-runs.json"


def _load():
    """The whole marker file, or {} — missing or corrupt means "nothing has ever run"."""
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as e:
        print(f"[warn] escalation state unreadable ({e}); treating this cycle as fresh",
              file=sys.stderr)
        return {}
    return data if isinstance(data, dict) else {}


def _save(data):
    try:
        config.MEMORY_DIR.mkdir(parents=True, exist_ok=True)
        _path().write_text(dumps(data, indent=2), encoding="utf-8")
    except Exception as e:  # pragma: no cover - disk
        # Losing the marker costs one extra deep run, so it must never take down a working cycle.
        print(f"[warn] escalation state: {e}", file=sys.stderr)


def _norm(text):
    """Collapse a reason string to what stays constant while the same story does.

    Both calendar flags and news reasons carry live numbers — "in 2.5h", "sentiment -70" — that
    change every cycle for one unchanged event. Comparing the raw strings would make every event
    look new forever, which is the bug this module exists to fix.
    """
    head = str(text or "").split(" (")[0].strip()
    return _DIGITS.sub("#", head).lower()


def evidence(screen_result, news_reasons=(), bar=0):
    """What this cycle knows, as {key: strength}. Keys are stable across cycles by construction."""
    ev = {}
    for t in (screen_result or {}).get("triggers", []) or []:
        score = t.get("score") or 0
        if score >= bar:
            ev[f"chart:{t.get('ticker')}"] = score
    for f in (screen_result or {}).get("calendar_flags", []) or []:
        ev[f"cal:{_norm(f)}"] = 1
    for r in news_reasons or []:
        ev[f"news:{_norm(r)}"] = 1
    return ev


def _new_keys(ev, prev, jump):
    """Split this cycle's evidence into (events, chart) that the last deep run had not seen."""
    events, chart = [], []
    for key, strength in ev.items():
        was = prev.get(key)
        if key.startswith("chart:"):
            if was is None:
                chart.append(f"{key[6:]} newly flagged at {strength}")
            elif strength >= was + jump:
                chart.append(f"{key[6:]} strengthened {was} to {strength}")
        elif was is None:
            events.append(key.split(":", 1)[1][:110])
    return events, chart


def decide(book, screen_result, news_reasons=(), now=None, force=False, bar=None,
           cooldown_min=None, max_gap_min=None):
    """Should this cycle spend a deep run? Returns (escalate, reasons, meta).

    `book` names the ledger ("stock" / "crypto") so the two never share a cooldown. `meta` carries
    the evidence dict, which the caller passes back to `commit()` once the run has actually
    happened — recording it before then would make a crashed run look like a completed read.
    """
    now = now or datetime.now(config.ET)
    bar = config.ESCALATE_SCORE if bar is None else bar
    cooldown_min = config.DEEP_COOLDOWN_MIN if cooldown_min is None else cooldown_min
    max_gap_min = config.DEEP_MAX_GAP_MIN if max_gap_min is None else max_gap_min

    state = _load().get(book) or {}
    prev = state.get("evidence") or {}
    ev = evidence(screen_result, news_reasons, bar=bar)
    meta = {"evidence": ev, "book": book}

    since = None
    try:
        last = datetime.fromisoformat(state["last_run"])
        since = (now - last).total_seconds() / 60.0
    except (KeyError, TypeError, ValueError):
        since = None
    meta["minutes_since_deep_run"] = None if since is None else round(since, 1)

    if force:
        return True, ["scheduled anchor run"], meta
    if since is None:
        return True, ["no deep run on record for this book"], meta

    events, chart = _new_keys(ev, prev, config.ESCALATE_SCORE_JUMP)
    reasons = []
    if events:
        # Events jump the cooldown: a print that just landed or a headline just read is perishable.
        reasons.append("new event evidence: " + "; ".join(events[:3]))
    if since >= max_gap_min:
        reasons.append(f"{round(since)}m since the last deep read (floor {max_gap_min}m)")
    if chart and since >= cooldown_min:
        reasons.append("new chart evidence: " + "; ".join(chart[:3]))
    if reasons:
        return True, reasons, meta

    held = "nothing new" if not chart else f"{len(chart)} new chart flag(s), inside cooldown"
    return False, [f"held: {held}; last deep read {round(since)}m ago "
                   f"(cooldown {cooldown_min}m, floor {max_gap_min}m)"], meta


def commit(book, meta, now=None):
    """Record that a deep run just happened, with the evidence it was given.

    Called *after* the run, so a crash or a quota wall leaves the previous signature standing and
    the next cycle retries rather than treating the failed read as done.
    """
    now = now or datetime.now(config.ET)
    data = _load()
    data[book] = {"last_run": now.isoformat(), "evidence": (meta or {}).get("evidence") or {}}
    _save(data)
