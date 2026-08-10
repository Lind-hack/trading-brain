"""Spending the tier-1 read on new evidence rather than on the clock.

The 2026-08-10 health check found 297 failed Claude runs and 147 cycles that had fallen back to
rules-only, every one carrying "You've hit your session limit". That was not the outage — it was
happening while the brain was perfectly healthy. Both tiers spend one Claude Code subscription
(there is no API key in this project and cannot be one), and tier 1 was spending it on a fixed
clock: a Haiku pass every 30 minutes on equities and every hour on crypto, most of them re-reading
the same stories. The logs give the ratio: "423 scraped, 65 kept, 358 filtered (250 already
reported)".

The cost of that is not the bill. A cycle without its Haiku pass has no news leg, and the
rulebook caps a two-leg signal at 70-79 — so an exhausted session limit was quietly making an 80
unreachable. These tests pin the rationing:

1. Same stories → reuse the previous read instead of buying an identical one.
2. A genuinely new story → spend the call.
3. A cached read is labelled as cached, never as degraded, and carries its age.
4. A failed read is never cached — replaying a keyword fallback for two hours would look like a
   news leg while carrying nothing a model ever saw.
5. A quiet tape still gets re-read: the cache expires even when nothing changed.

Run:  python -m pytest tests/test_news_ration.py -q
"""
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from brain import config, news_intel


@pytest.fixture(autouse=True)
def isolated_memory(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)
    return tmp_path


def _heads(*titles):
    return {"AAPL": [{"title": t} for t in titles]}


def _intel(tag="live"):
    return {"model": "claude-haiku-4-5", "macro_read": tag, "tickers": {"AAPL": {"summary": tag}}}


# ── the fingerprint ─────────────────────────────────────────────────────────────

def test_same_stories_produce_the_same_key():
    a = news_intel._fingerprint(_heads("Apple beats on iPhone revenue"), [])
    b = news_intel._fingerprint(_heads("Apple beats on iPhone revenue"), [])
    assert a == b


def test_a_retitled_wire_item_is_not_new_evidence():
    """`news_quality.signature` is wording-tolerant on purpose. A second desk retitling one wire
    item must not read as a new story and buy another model call — that is the exact waste being
    rationed."""
    a = news_intel._fingerprint(_heads("Nvidia beats Q2 estimates on data center demand"), [])
    b = news_intel._fingerprint(_heads("Nvidia Beats Q2 Estimates On Data Center Demand"), [])
    assert a == b


def test_a_genuinely_new_story_changes_the_key():
    a = news_intel._fingerprint(_heads("Apple beats on iPhone revenue"), [])
    b = news_intel._fingerprint(_heads("Apple beats on iPhone revenue",
                                       "Fed holds rates steady in surprise decision"), [])
    assert a != b


def test_macro_headlines_count_toward_the_key():
    a = news_intel._fingerprint(_heads("Apple beats"), [])
    b = news_intel._fingerprint(_heads("Apple beats"), [{"title": "Fed holds rates steady"}])
    assert a != b


# ── reuse ───────────────────────────────────────────────────────────────────────

def test_an_unchanged_tape_reuses_the_previous_read():
    news_intel._store_intel("stock", "abc123", _intel())
    got = news_intel._cached_intel("stock", "abc123")
    assert got is not None
    assert got["cached"] is True
    assert got["macro_read"] == "live"
    assert got["cache_age_min"] >= 0


def test_a_changed_tape_spends_the_call():
    news_intel._store_intel("stock", "abc123", _intel())
    assert news_intel._cached_intel("stock", "different") is None


def test_the_two_books_never_share_a_read():
    """An equity macro sweep and a crypto one ask different questions; serving one book's read to
    the other would be worse than no read at all."""
    news_intel._store_intel("stock", "same", _intel("equity read"))
    assert news_intel._cached_intel("crypto", "same") is None


def test_a_quiet_tape_is_still_re_read_eventually():
    """Otherwise the book trades all afternoon off one morning opinion."""
    stale = datetime.now(config.UTC) - timedelta(minutes=config.NEWS_CACHE_MAX_AGE_MIN + 5)
    news_intel._store_intel("stock", "abc123", _intel(), now=stale)
    assert news_intel._cached_intel("stock", "abc123") is None


def test_a_read_inside_the_window_survives():
    fresh = datetime.now(config.UTC) - timedelta(minutes=config.NEWS_CACHE_MAX_AGE_MIN - 5)
    news_intel._store_intel("stock", "abc123", _intel(), now=fresh)
    assert news_intel._cached_intel("stock", "abc123") is not None


# ── the thing that must never happen ────────────────────────────────────────────

def test_a_failed_read_is_never_stored():
    """Replaying a keyword fallback for two hours would present a news leg that no model ever
    produced — worse than having none, because the analyst would price it as evidence."""
    news_intel._store_intel("stock", "abc123", {"degraded": True, "macro_read": "fallback"})
    assert news_intel._cached_intel("stock", "abc123") is None


def test_a_cached_read_is_not_a_degraded_one():
    news_intel._store_intel("stock", "abc123", _intel())
    got = news_intel._cached_intel("stock", "abc123")
    assert got.get("degraded") in (None, False)
    assert got["cached"] is True


def test_a_corrupt_cache_is_a_miss_not_a_crash():
    (config.MEMORY_DIR / ".news-intel.json").write_text("{not json", encoding="utf-8")
    assert news_intel._cached_intel("stock", "abc123") is None


def test_headlines_are_not_stored_in_the_cache():
    """`headlines` is the current scrape and is re-attached fresh every cycle; persisting it would
    bloat the marker and risk serving yesterday's list as today's."""
    news_intel._store_intel("stock", "abc123", dict(_intel(), headlines={"AAPL": [1, 2, 3]}))
    raw = json.loads((config.MEMORY_DIR / ".news-intel.json").read_text(encoding="utf-8"))
    assert "headlines" not in raw["stock"]["intel"]
