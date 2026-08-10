"""A story we already told Lind is not news on the next cycle.

His words: *"haiku is using the same news for the next runs."* Saturation cannot catch that — a
scoop from a wire twenty minutes ago scores in the nineties on every other axis, and scores exactly
the same twenty minutes later when it is the second time he has read it. So the store remembers
what the news pass actually *cited*, and a cited story is held back until another outlet picks it
up — the one honest sign there is something new to say about it.

Run:  python -m pytest tests/test_news_reuse.py -q
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from brain import config, news_intel, news_quality as nq

NOW = datetime(2026, 7, 29, 18, 0, tzinfo=config.UTC)
LATER = NOW + timedelta(minutes=30)


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(nq, "SEEN_PATH", tmp_path / "SEEN-HEADLINES.json")
    return {"version": 1, "stories": {}}


def _h(title, source="Reuters", published=None):
    return {"title": title, "source": source, "link": "https://x/1",
            "published": (published or NOW).isoformat()}


HEAD = "Micron raises HBM guidance on AI server demand"


def _cycle(store, items, now, cite=None):
    """One cycle: score, rank, then mark what the pass cited. Returns (kept, dropped)."""
    scored = nq.observe(items, "MU", store, now=now)
    kept, dropped = nq.rank(scored, limit=5)
    if cite:
        nq.mark_used(cite, store=store, now=now)
    return kept, dropped


# ── the memory itself ───────────────────────────────────────────────────────────

def test_a_cited_story_is_a_repeat_on_the_next_cycle(store):
    kept, _ = _cycle(store, [_h(HEAD)], NOW, cite=[_h(HEAD)])
    assert [k["repeat"] for k in kept] == [False]
    again = nq.observe([_h(HEAD)], "MU", store, now=LATER)[0]
    assert again["repeat"] is True
    assert again["times_used"] == 1


def test_a_story_that_was_shown_but_not_cited_comes_back(store):
    """Fed to the model and skipped is not "told to Lind". Suppressing it would bury it unread."""
    _cycle(store, [_h(HEAD)], NOW, cite=None)
    again = nq.observe([_h(HEAD)], "MU", store, now=LATER)[0]
    assert again["repeat"] is False
    assert again["times_used"] == 0


def test_a_repeat_is_held_back_from_the_next_pass(store):
    _cycle(store, [_h(HEAD)], NOW, cite=[_h(HEAD)])
    scored = nq.observe([_h(HEAD), _h("Micron opens second Boise fab", "Bloomberg")],
                        "MU", store, now=LATER)
    kept, dropped = nq.rank(scored, limit=5)
    assert [k["title"] for k in kept] == ["Micron opens second Boise fab"]
    assert [d["title"] for d in dropped] == [HEAD]


def test_a_story_other_desks_pick_up_is_allowed_back(store):
    """A second outlet carrying it is the difference between a developing story and an echo."""
    _cycle(store, [_h(HEAD)], NOW, cite=[_h(HEAD)])
    scored = nq.observe([_h("Micron guidance raised on HBM demand", "Bloomberg")],
                        "MU", store, now=LATER)
    assert scored[0]["repeat"] is False
    assert scored[0]["new_outlets_since_use"] == 1
    kept, _ = nq.rank(scored, limit=5)
    assert len(kept) == 1


def test_citing_it_again_re_arms_the_suppression(store):
    """The developed story got through and was cited a second time. That resets the bar to the
    outlet count it now has, so the third cycle does not wave it through on the same growth."""
    _cycle(store, [_h(HEAD)], NOW, cite=[_h(HEAD)])
    dev = _h("Micron guidance raised on HBM demand", "Bloomberg")
    _cycle(store, [dev], LATER, cite=[dev])
    third = nq.observe([dev], "MU", store, now=LATER + timedelta(minutes=30))[0]
    assert third["repeat"] is True
    assert third["times_used"] == 2


def test_repeated_citation_scores_it_down_further(store):
    _cycle(store, [_h(HEAD)], NOW, cite=[_h(HEAD)])
    once = nq.observe([_h(HEAD)], "MU", store, now=LATER)[0]
    nq.mark_used([_h(HEAD)], store=store, now=LATER)
    twice = nq.observe([_h(HEAD)], "MU", store, now=LATER + timedelta(minutes=30))[0]
    assert twice["novelty"] < once["novelty"]


# ── the guards ──────────────────────────────────────────────────────────────────

def test_a_thin_name_still_gets_its_headlines_labelled(store):
    """The valve that never lets a ticker be filtered to zero still holds — but what comes back
    has to say it is a repeat, or the model reports our own last cycle to us as news."""
    _cycle(store, [_h(HEAD)], NOW, cite=[_h(HEAD)])
    scored = nq.observe([_h(HEAD)], "MU", store, now=LATER)
    kept, _ = nq.rank(scored, limit=5)
    assert len(kept) == 1
    assert kept[0]["low_quality"] is True and kept[0]["repeat"] is True
    assert news_intel._for_prompt(kept)[0]["already_reported"] is True


def test_a_fresh_headline_carries_no_repeat_marker(store):
    kept, _ = _cycle(store, [_h(HEAD)], NOW)
    assert "already_reported" not in news_intel._for_prompt(kept)[0]


def test_marking_a_headline_we_never_scraped_records_nothing(store):
    """`_sanitize` already refuses a model-invented headline. Creating a story record for one here
    would put the model's own output into the store as evidence."""
    assert nq.mark_used([_h("Micron acquires the moon")], store=store, now=NOW) == 0
    assert store["stories"] == {}


def test_one_story_cited_under_two_tickers_counts_once(store):
    nq.observe([_h(HEAD)], "MU", store, now=NOW)
    assert nq.mark_used([_h(HEAD), _h(HEAD)], store=store, now=NOW) == 1
    assert list(store["stories"].values())[0]["used"] == 1


def test_the_drop_is_reported_as_already_reported(store):
    _cycle(store, [_h(HEAD)], NOW, cite=[_h(HEAD)])
    scored = nq.observe([_h(HEAD), _h("Micron opens second Boise fab", "Bloomberg")],
                        "MU", store, now=LATER)
    kept, dropped = nq.rank(scored, limit=5)
    stats = nq.summarize(kept, dropped)
    assert stats["repeats"] == 1
    assert stats["drop_reasons"]["already-reported"] == 1


# ── the wiring ──────────────────────────────────────────────────────────────────

def test_the_pass_marks_what_it_cited(monkeypatch, tmp_path):
    """End to end through `run`: the store on disk has to carry the citation, because the next
    cycle is a different process and the file is the only thing that survives."""
    monkeypatch.setattr(nq, "SEEN_PATH", tmp_path / "SEEN-HEADLINES.json")
    monkeypatch.setattr(news_intel.collect, "sec_filings", lambda *a, **k: [])
    monkeypatch.setattr(news_intel.collect, "finnhub_news", lambda *a, **k: [_h(HEAD)])
    monkeypatch.setattr(news_intel.collect, "google_news", lambda *a, **k: [])

    intel = news_intel.run(["MU"], use_claude=False)
    assert intel["news_quality"]["cited"] >= 1

    reloaded = nq.load_store()
    rec = list(reloaded["stories"].values())[0]
    assert rec["used"] == 1 and rec["outlets_at_use"] == len(rec["sources"])


def test_the_cited_list_covers_tickers_and_macro():
    intel = {"tickers": {"MU": {"headlines_used": [{"title": "a"}]},
                         "NVDA": {"headlines_used": []}},
             "top_stories": [{"title": "b"}, "not a dict"]}
    assert [h["title"] for h in news_intel.cited_headlines(intel)] == ["a", "b"]
