"""Regression tests for the wiring between the phases.

The per-phase suites all test their own module in isolation: the gate decides correctly, the
scorer scores correctly, the recap renders correctly. This file tests the seams — that `main()`
actually asks the gate before doing anything, that a gated-out run still leaves a heartbeat, that
the news pass hands its scored headlines forward instead of letting a second scrape overwrite
them, and that the Finnhub cache is flushed however the run ends.

Every one of these is a bug that the unit tests would have passed straight through.

Run:  python -m pytest tests/test_pipeline_wiring.py -q
"""
import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import market_brain
from brain import collect, config, market_hours, news_intel, supabase


@pytest.fixture(autouse=True)
def _isolate_memory(tmp_path, monkeypatch):
    """`main()` writes a run-log marker. Point it somewhere disposable.

    Without this the suite reaches into the live brain-memory/ — which on the VPS is the real
    journal, and a marker written there by a test would suppress a real anchor run.
    """
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)


@pytest.fixture
def wired(monkeypatch):
    """Neuter every side effect `main()` can reach, and record what it tried to do."""
    calls = {"cycle": [], "research": [], "weekly": [], "digest": [],
             "heartbeat": [], "cache_save": 0}
    monkeypatch.setattr(market_brain, "do_cycle",
                        lambda a, mode="cycle", force=False: calls["cycle"].append((mode, force)) or 0)
    monkeypatch.setattr(market_brain, "do_research", lambda a: calls["research"].append(a.research) or 0)
    monkeypatch.setattr(market_brain, "do_weekly_review", lambda a: calls["weekly"].append(True) or 0)
    monkeypatch.setattr(market_brain, "do_digest", lambda a: calls["digest"].append(True) or 0)
    monkeypatch.setattr(supabase, "push_heartbeat",
                        lambda mode, now, why: calls["heartbeat"].append((mode, why)) or True)
    monkeypatch.setattr(collect, "fundamentals_cache_save",
                        lambda: calls.__setitem__("cache_save", calls["cache_save"] + 1))
    return calls


def _run(monkeypatch, argv, gate=None):
    monkeypatch.setattr(sys, "argv", ["market_brain.py"] + argv)
    if gate is not None:
        monkeypatch.setattr(market_hours, "gate", lambda mode, now=None: gate(mode))
    return market_brain.main()


OPEN = lambda mode: (True, "session open")
SHUT = lambda mode: (False, "weekend — market closed")


# ── phase 1: the gate is actually wired in ─────────────────────────────────────

def test_a_closed_market_stops_the_run_before_any_work(monkeypatch, wired):
    assert _run(monkeypatch, ["--cycle"], SHUT) == 0
    assert wired["cycle"] == [], "the cycle ran on a day the gate refused"


def test_a_gated_out_run_still_leaves_a_heartbeat(monkeypatch, wired):
    """Otherwise 'the market was closed' looks exactly like 'the VPS is down'."""
    _run(monkeypatch, ["--cycle"], SHUT)
    assert wired["heartbeat"] == [("cycle", "weekend — market closed")]


def test_a_dry_run_does_not_write_a_heartbeat(monkeypatch, wired):
    _run(monkeypatch, ["--cycle", "--dry-run"], SHUT)
    assert wired["heartbeat"] == []


def test_the_gate_can_be_overridden_for_a_manual_backfill(monkeypatch, wired):
    _run(monkeypatch, ["--cycle", "--ignore-market-hours"], SHUT)
    assert wired["cycle"] == [("cycle", False)]


@pytest.mark.parametrize("argv,mode", [
    (["--cycle"], "cycle"),
    (["--anchor", "pre"], "pre"),
    (["--anchor", "mid"], "mid"),
    (["--anchor", "close"], "close"),
    (["--research", "NVDA"], "research"),
    (["--weekly-review"], "weekly"),
    (["--digest"], "digest"),
])
def test_every_mode_asks_the_gate_under_its_own_name(monkeypatch, wired, argv, mode):
    """An anchor must reach the gate as 'close', not as 'anchor' — the windows differ per mode."""
    seen = []
    _run(monkeypatch, argv, lambda m: (seen.append(m), (False, "no"))[1])
    assert seen == [mode]


def test_an_anchor_forces_the_deep_run_and_a_plain_cycle_does_not(monkeypatch, wired):
    _run(monkeypatch, ["--anchor", "close"], OPEN)
    _run(monkeypatch, ["--cycle"], OPEN)
    assert wired["cycle"] == [("close", True), ("cycle", False)]


def test_research_reaches_the_ticker_it_was_given(monkeypatch, wired):
    _run(monkeypatch, ["--research", "NVDA"], OPEN)
    assert wired["research"] == ["NVDA"]


def test_the_real_gate_lets_a_wednesday_midday_cycle_through(monkeypatch, wired):
    """Not a mock: the shipped gate, called by the shipped main()."""
    real = market_hours.gate
    monkeypatch.setattr(market_hours, "gate",
                        lambda mode, now=None: real(mode, datetime(2026, 7, 29, 11, 3, tzinfo=config.ET)))
    monkeypatch.setattr(sys, "argv", ["market_brain.py", "--cycle"])
    market_brain.main()
    assert wired["cycle"] == [("cycle", False)]


def test_the_real_gate_refuses_the_same_cycle_on_a_saturday(monkeypatch, wired):
    real = market_hours.gate
    monkeypatch.setattr(market_hours, "gate",
                        lambda mode, now=None: real(mode, datetime(2026, 7, 25, 11, 3, tzinfo=config.ET)))
    monkeypatch.setattr(sys, "argv", ["market_brain.py", "--cycle", "--dry-run"])
    market_brain.main()
    assert wired["cycle"] == []


# ── the finally block ──────────────────────────────────────────────────────────

def test_the_fundamentals_cache_is_flushed_on_a_normal_run(monkeypatch, wired):
    _run(monkeypatch, ["--cycle"], OPEN)
    assert wired["cache_save"] == 1


def test_the_fundamentals_cache_is_flushed_even_when_the_run_blows_up(monkeypatch, wired):
    """A crashed cycle has still spent the API calls. Losing the cache means paying twice."""
    def boom(a, mode="cycle", force=False):
        raise RuntimeError("collector died")
    monkeypatch.setattr(market_brain, "do_cycle", boom)
    with pytest.raises(RuntimeError):
        _run(monkeypatch, ["--cycle"], OPEN)
    assert wired["cache_save"] == 1


def test_a_gated_out_run_does_not_pretend_to_have_a_cache_to_flush(monkeypatch, wired):
    _run(monkeypatch, ["--cycle"], SHUT)
    assert wired["cache_save"] == 0


# ── phase 3 → phase 6: the scored headlines survive the handoff ────────────────

def test_the_news_pass_hands_forward_both_its_scores_and_its_headlines(monkeypatch):
    """`headlines` is a superset of what Haiku cited — everything that passed the quality gate."""
    scraped = {"AAPL": [{"title": "8-K: supplier contract terminated", "source": "SEC",
                         "crowding": "under-covered", "outlets": 1, "source_tier": "filing"}]}
    monkeypatch.setattr(news_intel, "load_store", lambda: {}, raising=False)
    monkeypatch.setattr(news_intel.news_quality, "load_store", lambda: {})
    monkeypatch.setattr(news_intel.news_quality, "save_store", lambda s: None)
    monkeypatch.setattr(news_intel, "gather_headlines",
                        lambda t, store=None, stats=None: (stats.update({"kept": 1, "dropped": 4}),
                                                           scraped)[1])
    monkeypatch.setattr(news_intel, "gather_macro_headlines",
                        lambda store=None, venue="stock": [])
    monkeypatch.setattr(news_intel, "analyze",
                        lambda h, m, c, use_claude=True: {"tickers": {}, "degraded": False})

    intel = news_intel.run(["AAPL"], use_claude=False)
    # `cited` rides along on the same stats dict — the reuse pass stamps what the read actually
    # quoted, and an analysis returning no tickers quoted nothing.
    assert intel["news_quality"] == {"kept": 1, "dropped": 4, "cited": 0}
    assert intel["headlines"]["AAPL"][0]["crowding"] == "under-covered"


def test_the_scored_headlines_reach_the_snapshot_the_analyst_reads():
    """The seam the duplicate-scrape fix created: quality metadata must survive onto the packet."""
    market = {"AAPL": {"ticker": "AAPL", "indicators": {"price": 190.0}}}
    intel = {"headlines": {"AAPL": [{"title": "8-K", "crowding": "under-covered",
                                     "source_tier": "filing"}]}}
    market_brain._attach_news(market, intel)
    news = market["AAPL"]["news"]
    assert news and news[0]["source_tier"] == "filing"
    assert market["AAPL"]["indicators"]["price"] == 190.0, "attaching news clobbered the snapshot"


def test_only_the_freshest_headlines_are_attached():
    market = {"AAPL": {"ticker": "AAPL"}}
    intel = {"headlines": {"AAPL": [{"title": f"h{i}"} for i in range(12)]}}
    market_brain._attach_news(market, intel, limit=4)
    assert [h["title"] for h in market["AAPL"]["news"]] == ["h0", "h1", "h2", "h3"]


# ── the pace block reaches the analyst ──────────────────────────────────────────

def _packet(new_trades, now):
    from brain import deep
    return deep.build_packet("cycle", {}, {"triggers": [], "why": []}, {},
                             {"new_trades_this_week": new_trades})["pace"]


def test_the_packet_carries_the_week_against_the_pace_target(monkeypatch):
    """Opus cannot pace itself against a number it never sees."""
    monkeypatch.setattr(market_hours, "sessions_left_this_week", lambda d: 3)
    pace = _packet(2, None)
    assert pace["new_trades_this_week"] == 2
    assert pace["weekly_target"] == config.WEEKLY_TRADE_TARGET
    assert pace["weekly_cap"] == config.MAX_NEW_TRADES_PER_WEEK
    assert pace["sessions_left_this_week"] == 3


def test_the_cap_sits_above_the_target_so_it_never_blocks_the_pace():
    """A cap at or below the target would refuse the very trade the target asks for."""
    assert config.MAX_NEW_TRADES_PER_WEEK > config.WEEKLY_TRADE_TARGET


def test_behind_pace_only_fires_when_the_week_is_actually_running_out(monkeypatch):
    target = config.WEEKLY_TRADE_TARGET
    monkeypatch.setattr(market_hours, "sessions_left_this_week", lambda d: target)
    assert not _packet(0, None)["behind_pace"], "a full week left is not behind"
    monkeypatch.setattr(market_hours, "sessions_left_this_week", lambda d: 1)
    assert _packet(target - 2, None)["behind_pace"], "two short with one session left is behind"
    assert not _packet(target, None)["behind_pace"], "target met is never behind"


def test_a_missing_portfolio_summary_does_not_break_the_packet(monkeypatch):
    """The pace block is context, not a dependency — it must never be the thing that kills a run."""
    from brain import deep
    monkeypatch.setattr(market_hours, "sessions_left_this_week", lambda d: 2)
    assert deep.build_packet("cycle", {}, {}, {}, None)["pace"]["new_trades_this_week"] == 0


# ── the news watchlist: who Haiku is allowed to read about ────────────────────────

class _FakePortfolio:
    def __init__(self, held):
        self._held = list(held)

    def held_tickers(self):
        return list(self._held)


def _watchlist(held=(), triggered=(), monkeypatch=None, focus=None, limit=20, reserve=4):
    """Run the real selector against a synthetic screen result."""
    focus = list(focus if focus is not None else config.FOCUS_TICKERS)
    if monkeypatch is not None:
        monkeypatch.setattr(config, "FOCUS_TICKERS", focus)
        monkeypatch.setattr(config, "NEWS_TICKERS_PER_CYCLE", limit)
        monkeypatch.setattr(config, "NEWS_DISCOVERY_RESERVE", reserve)
    market = {t: {} for t in focus}
    screen_result = {"triggers": [{"ticker": t, "score": 9 - i} for i, t in enumerate(triggered)]}
    return market_brain._news_watchlist(market, screen_result, _FakePortfolio(held))


def test_open_positions_are_never_dropped_from_the_news_read(monkeypatch):
    """News that threatens money already at risk outranks news about a maybe."""
    focus = [f"F{i}" for i in range(40)]
    held = focus[:8]
    out = _watchlist(held=held, triggered=focus[8:34], monkeypatch=monkeypatch, focus=focus)
    assert set(held) <= set(out)


def test_a_busy_tape_cannot_starve_news_first_discovery(monkeypatch):
    """The bug this fixes: 8 held + 18 triggered filled every place, so a name whose chart was
    quiet but whose filing was fresh could not be found — on exactly the days that matter most."""
    focus = [f"F{i}" for i in range(40)]
    held, triggered = focus[:8], focus[8:26]
    out = _watchlist(held=held, triggered=triggered, monkeypatch=monkeypatch, focus=focus)
    quiet = [t for t in out if t not in held and t not in triggered]
    assert len(quiet) == 4, f"discovery reserve was starved: {out}"
    assert len(out) == 20


def test_the_reserve_goes_back_to_the_screener_when_discovery_has_nothing_to_offer(monkeypatch):
    """A floor, not a quota — an idle reserve would waste the cycle's most informative places."""
    focus = [f"F{i}" for i in range(20)]
    out = _watchlist(held=focus[:2], triggered=focus[2:], monkeypatch=monkeypatch, focus=focus)
    assert len(out) == 20, "every place should be used when the focus list is exhausted"
    assert set(out) == set(focus)


def test_the_watchlist_never_exceeds_its_budget(monkeypatch):
    """Each name costs three HTTP calls and a slice of the Haiku prompt. The cap is the budget."""
    focus = [f"F{i}" for i in range(60)]
    out = _watchlist(held=focus[:8], triggered=focus[8:40], monkeypatch=monkeypatch, focus=focus)
    assert len(out) == 20
    assert len(set(out)) == 20, "a name read twice is a call paid for twice"


def test_market_context_tickers_are_never_read_for_news(monkeypatch):
    """SPY/VIX set the regime; they are not trade candidates and must not eat a place."""
    focus = list(config.MARKET_CONTEXT) + [f"F{i}" for i in range(10)]
    out = _watchlist(triggered=config.MARKET_CONTEXT, monkeypatch=monkeypatch, focus=focus)
    assert not (set(out) & set(config.MARKET_CONTEXT))


def test_a_full_book_still_leaves_room_to_look_outward(monkeypatch):
    """The real numbers, not synthetic ones: at MAX_POSITIONS held, the cycle must still be able
    to read about names it does not own. Otherwise the brain can only ever manage what it has."""
    assert config.NEWS_TICKERS_PER_CYCLE - config.MAX_POSITIONS >= config.NEWS_DISCOVERY_RESERVE
