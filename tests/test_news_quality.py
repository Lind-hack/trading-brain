"""Twelve outlets running the same wire copy is not twelve stories, and it is not a scoop.

Lind asked for news "not a lot of people have heard of and are not that saturated". The old
gatherer measured the opposite by accident: it de-duped titles inside one call and dropped the
copies, so the wider a story had spread the *cleaner* it looked by the time Haiku read it. These
tests pin the inversion — duplicates are counted, age runs from the publisher's own timestamp,
and a filing outranks the article rewriting it — plus the one guard that matters more than the
filter itself: a thin-news ticker must never be filtered into looking like a quiet one.

Run:  python -m pytest tests/ -q
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from brain import collect, config, news_intel, news_quality as nq

NOW = datetime(2026, 7, 29, 18, 0, tzinfo=config.UTC)


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(nq, "SEEN_PATH", tmp_path / "SEEN-HEADLINES.json")
    return {"version": 1, "stories": {}}


def _h(title, source="Reuters", published=None, **kw):
    return dict({"title": title, "source": source, "link": "https://x/1",
                 "published": (published or NOW).isoformat()}, **kw)


# ── story identity ──────────────────────────────────────────────────────────────

def test_a_retitled_wire_item_is_one_story(store):
    """Same words, different order — the commonest form of "everyone ran the same copy"."""
    nq.observe([_h("Micron raises HBM guidance", "Reuters")], "MU", store, now=NOW)
    again = nq.observe([_h("Micron guidance raised on HBM", "CNBC")], "MU", store, now=NOW)[0]
    assert again["outlets"] == 2
    assert len(store["stories"]) == 1


def test_a_genuine_rewrite_is_still_the_same_story(store):
    """Different desks, different sentences, one event. Overlap catches what an exact key cannot."""
    nq.observe([_h("Nvidia Beats Q2 Estimates On Data Center Demand", "Reuters")],
               "NVDA", store, now=NOW)
    rewrite = nq.observe([_h("Nvidia earnings beat as data center revenue surges", "CNBC")],
                         "NVDA", store, now=NOW)[0]
    assert rewrite["outlets"] == 2
    assert len(store["stories"]) == 1


def test_different_stories_stay_different(store):
    nq.observe([_h("Nvidia beats Q2 estimates on data center demand", "Reuters")],
               "NVDA", store, now=NOW)
    other = nq.observe([_h("Nvidia CFO resigns abruptly after audit dispute", "Bloomberg")],
                       "NVDA", store, now=NOW)[0]
    assert other["outlets"] == 1
    assert len(store["stories"]) == 2


# ── source tiering ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("source,tier", [
    ("SEC EDGAR", 0), ("Reuters", 1), ("Business Wire", 1), ("CNBC", 2),
    ("The Motley Fool", 3), ("Zacks Investment Research", 3), ("Benzinga", 3),
])
def test_source_tiers(source, tier):
    assert nq.source_tier(source) == tier


def test_an_unknown_outlet_is_treated_as_ordinary_not_as_junk():
    """A small trade publication is often where the real scoop is. Unknown must not mean bad."""
    assert nq.source_tier("Semiconductor Weekly Bulletin") == 2


# ── title heuristics ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("title", [
    "3 Stocks To Buy Before They Soar",
    "Should You Buy Nvidia Stock Right Now?",
    "Better Buy: AMD vs. Nvidia",
    "Why Palantir Stock Jumped Today",
    "This Stock Could Make You A Millionaire",
])
def test_clickbait_is_flagged(title):
    assert nq.farm_reason(title)


@pytest.mark.parametrize("title", [
    "Nvidia files 8-K — item 2.02 results of operations",
    "Micron raises fiscal Q4 guidance on HBM pricing",
    "FTC opens antitrust probe into Broadcom's VMware licensing",
])
def test_real_reporting_is_not_flagged(title):
    assert nq.farm_reason(title) is None


# ── saturation ──────────────────────────────────────────────────────────────────

def test_each_new_outlet_raises_saturation(store):
    """The count is the whole point: one outlet is a scoop, six is yesterday's consensus."""
    first = nq.observe([_h("Micron raises HBM guidance", "Reuters")], "MU", store, now=NOW)[0]
    assert first["outlets"] == 1 and first["crowding"] == "under-covered"

    for src in ("CNBC", "Yahoo Finance", "Benzinga", "Zacks"):
        nq.observe([_h("Micron guidance raised on HBM", src)], "MU", store, now=NOW)
    later = nq.observe([_h("Micron raises HBM guidance", "MarketWatch")], "MU", store, now=NOW)[0]
    assert later["outlets"] == 6
    assert later["crowding"] == "saturated"
    assert later["novelty"] < first["novelty"]


def test_saturation_is_shared_across_tickers(store):
    """AMD's coverage of an Nvidia story still means one more outlet has run it."""
    nq.observe([_h("Nvidia cuts H20 China shipments", "Reuters")], "NVDA", store, now=NOW)
    seen = nq.observe([_h("Nvidia cuts H20 shipments to China", "Bloomberg")], "AMD", store, now=NOW)[0]
    assert seen["outlets"] == 2
    assert set(store["stories"][nq.signature("Nvidia cuts H20 China shipments")]["tickers"]) == {"NVDA", "AMD"}


# ── age ─────────────────────────────────────────────────────────────────────────

def test_a_three_day_old_article_is_stale_the_first_time_we_see_it(store):
    """Age comes from the publisher's timestamp, so a cold start does not make old news fresh."""
    old = nq.observe([_h("Apple supplier cuts orders", published=NOW - timedelta(days=3))],
                     "AAPL", store, now=NOW)[0]
    fresh = nq.observe([_h("Apple names new COO", published=NOW - timedelta(minutes=10))],
                       "AAPL", store, now=NOW)[0]
    assert old["age_min"] > 4000
    assert old["novelty"] < fresh["novelty"]


def test_first_seen_survives_the_process(store, tmp_path, monkeypatch):
    """A story recycled at 14:03 must be recognised as the one we first read at 09:33."""
    nq.observe([_h("Tesla opens Berlin line", published=NOW)], "TSLA", store, now=NOW)
    nq.save_store(store, now=NOW)

    reloaded = nq.load_store()
    later = nq.observe([_h("Tesla opens Berlin line", "CNBC", published=NOW)], "TSLA",
                       reloaded, now=NOW + timedelta(hours=5))[0]
    assert later["age_min"] >= 300
    assert later["first_seen"].startswith("2026-07-29T18:00")


def test_prune_forgets_old_stories(store):
    nq.observe([_h("ancient news")], "SPY", store, now=NOW - timedelta(days=30))
    nq.prune(store, now=NOW)
    assert store["stories"] == {}


# ── ranking ─────────────────────────────────────────────────────────────────────

def test_the_filing_outranks_the_rewrite(store):
    items = nq.observe([
        _h("Why Nvidia Stock Jumped Today", "The Motley Fool"),
        _h("NVIDIA Corp filed 8-K — item 2.02 results of operations", "SEC EDGAR",
           published=NOW - timedelta(minutes=20)),
        _h("Nvidia posts record data center revenue", "Reuters",
           published=NOW - timedelta(minutes=15)),
    ], "NVDA", store, now=NOW)
    kept, dropped = nq.rank(items, limit=5)
    assert kept[0]["source"] == "SEC EDGAR"
    assert [n["source"] for n in dropped] == ["The Motley Fool"]


def test_filtering_never_reports_a_thin_ticker_as_silent(store):
    """"No coverage" and "only bad coverage" are different facts, and only one of them is true."""
    items = nq.observe([
        _h("5 Reasons To Buy SOFI Stock Now", "InvestorPlace", published=NOW - timedelta(days=4)),
        _h("Should You Buy SOFI Before Earnings?", "Zacks", published=NOW - timedelta(days=6)),
    ], "SOFI", store, now=NOW)
    kept, _ = nq.rank(items, limit=5)
    assert len(kept) == 2
    assert all(n["low_quality"] for n in kept)


def test_rank_respects_the_limit(store):
    items = nq.observe([_h(f"Broadcom signs deal number {i}", "Reuters") for i in range(9)],
                       "AVGO", store, now=NOW)
    kept, dropped = nq.rank(items, limit=3)
    assert len(kept) == 3 and len(dropped) == 6


# ── SEC EDGAR shaping ───────────────────────────────────────────────────────────

class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _edgar(monkeypatch, forms, items, dates=None):
    n = len(forms)
    payload = {"name": "NVIDIA CORP", "filings": {"recent": {
        "form": forms, "items": items,
        "filingDate": dates or ["2026-07-29"] * n,
        "accessionNumber": [f"0001045810-26-00010{i}" for i in range(n)],
        "primaryDocument": [f"doc{i}.htm" for i in range(n)],
        "primaryDocDescription": ["" for _ in range(n)],
    }}}
    monkeypatch.setattr(collect, "sec_ticker_map", lambda *a, **k: {"NVDA": "0001045810"})
    monkeypatch.setattr(collect.requests, "get", lambda *a, **k: _Resp(payload))


def test_8k_item_codes_become_readable(monkeypatch):
    _edgar(monkeypatch, ["8-K"], ["2.02,9.01"])
    f = collect.sec_filings("NVDA", days=5)[0]
    assert "results of operations" in f["title"]
    assert f["source"] == "SEC EDGAR"
    assert f["link"].startswith("https://www.sec.gov/Archives/edgar/data/1045810/000104581026000100/")


def test_an_exhibits_only_8k_is_not_news(monkeypatch):
    """Item 9.01 alone is bookkeeping. Escalating a deep run on it wastes the budget."""
    _edgar(monkeypatch, ["8-K"], ["9.01"])
    assert collect.sec_filings("NVDA", days=5) == []


def test_filings_outside_the_window_stop_the_scan(monkeypatch):
    _edgar(monkeypatch, ["8-K", "8-K"], ["5.02", "2.02"], dates=["2026-07-29", "2026-01-04"])
    monkeypatch.setattr(collect, "datetime", collect.datetime)
    out = collect.sec_filings("NVDA", days=3)
    assert len(out) <= 1


# ── the gatherer, end to end ────────────────────────────────────────────────────

def test_gather_headlines_scores_and_trims(tmp_path, monkeypatch):
    monkeypatch.setattr(nq, "SEEN_PATH", tmp_path / "SEEN-HEADLINES.json")
    monkeypatch.setattr(collect, "sec_filings", lambda *a, **k: [])
    monkeypatch.setattr(collect, "finnhub_news", lambda t, **k: [
        _h(f"{t} wins a large supply contract", "Reuters"),
        _h(f"3 Reasons To Buy {t} Stock Today", "The Motley Fool"),
    ])
    monkeypatch.setattr(collect, "google_news", lambda q, **k: [
        _h("Nvidia wins large supply contract", "CNBC"),
    ])

    stats = {}
    out = news_intel.gather_headlines(["NVDA"], per_ticker=3, stats=stats)
    titles = [n["title"] for n in out["NVDA"]]
    assert "3 Reasons To Buy NVDA Stock Today" not in titles
    assert stats["dropped"] == 1 and stats["kept"] == 2
    assert (tmp_path / "SEEN-HEADLINES.json").exists()


# ── escalation ──────────────────────────────────────────────────────────────────

def test_loud_but_saturated_news_does_not_buy_a_deep_run():
    intel = {"tickers": {"TSLA": {"materiality": "medium", "sentiment": 80, "is_fresh": True,
                                  "crowding": "saturated", "catalyst": "product"}},
             "macro_sentiment": 0}
    assert news_intel.escalation_reasons(intel) == []


def test_the_same_news_under_covered_does_escalate():
    intel = {"tickers": {"TSLA": {"materiality": "medium", "sentiment": 80, "is_fresh": True,
                                  "crowding": "under-covered", "catalyst": "product"}},
             "macro_sentiment": 0}
    assert len(news_intel.escalation_reasons(intel)) == 1


def test_high_materiality_escalates_even_when_everyone_has_it():
    """An FOMC decision is saturated by definition and still moves the tape."""
    intel = {"tickers": {"SPY": {"materiality": "high", "sentiment": 10, "is_fresh": True,
                                 "crowding": "saturated", "catalyst": "macro", "summary": "cut"}},
             "macro_sentiment": 0}
    assert len(news_intel.escalation_reasons(intel)) == 1


# ── the gate, re-narrowed after it was measured ─────────────────────────────────

def test_merely_notable_news_no_longer_buys_a_look():
    """Medium materiality used to escalate, on the theory that a deep run concluding nothing was
    cheaper than never seeing the setup. Measured on 2026-07-28: 30 of 30 cycles escalated, and 16
    of them had no high-materiality reason at all. A gate that never closes is not rationing the
    most expensive call in the pipeline, it is just spending it — so `medium` is out."""
    intel = {"tickers": {"AMD": {"materiality": "medium", "sentiment": 20, "is_fresh": True,
                                 "crowding": "mixed", "catalyst": "guidance"}},
             "macro_sentiment": 0}
    assert news_intel.escalation_reasons(intel) == []


def test_medium_materiality_that_everyone_has_run_is_still_ignored():
    """Belt and braces: saturated medium news was already blocked by the crowding guard, and it
    stays blocked now that materiality alone would reject it. Two independent reasons, on purpose —
    if the materiality gate is ever widened again the saturation hole must not reopen with it."""
    intel = {"tickers": {"AMD": {"materiality": "medium", "sentiment": 10, "is_fresh": True,
                                 "crowding": "saturated", "catalyst": "guidance"}},
             "macro_sentiment": 0}
    assert news_intel.escalation_reasons(intel) == []


def test_loud_tone_alone_needs_to_be_genuinely_extreme():
    """The sentiment-only path is the second front door. At 45 it stood wide open; at 70 a
    tone-driven escalation is an outlier rather than a routine one."""
    base = {"materiality": "low", "is_fresh": True, "crowding": "under-covered",
            "catalyst": "guidance"}
    warm = {"tickers": {"AMD": dict(base, sentiment=55)}, "macro_sentiment": 0}
    extreme = {"tickers": {"AMD": dict(base, sentiment=80)}, "macro_sentiment": 0}
    assert news_intel.escalation_reasons(warm) == []
    assert len(news_intel.escalation_reasons(extreme)) == 1


def test_high_materiality_escalates_even_when_saturated():
    """The exception that has to survive: an FOMC decision or an earnings miss moves the tape
    whether or not the brain read it first."""
    intel = {"tickers": {"AMD": {"materiality": "high", "sentiment": 5, "is_fresh": True,
                                 "crowding": "saturated", "catalyst": "earnings"}},
             "macro_sentiment": 0}
    assert len(news_intel.escalation_reasons(intel)) == 1


def test_stale_news_never_escalates_at_any_materiality():
    intel = {"tickers": {"AMD": {"materiality": "high", "sentiment": 90, "is_fresh": False,
                                 "crowding": "under-covered", "catalyst": "earnings"}},
             "macro_sentiment": 0}
    assert news_intel.escalation_reasons(intel) == []
