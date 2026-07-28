"""The crypto cycle as the CLI actually reaches it.

test_crypto_book.py covers the pieces — the universe, the thresholds, the separate ledger. This
file covers the seam: that `--crypto-cycle` exists, that it runs the crypto book rather than the
equity one, and above all that it does **not** pass through the equity session gate. That last one
is the whole feature. A crypto track that stops at 16:00 ET and all weekend is not a 24/7 track,
and the scalp time stop would have nothing running at 03:00 UTC to fire it.

Run:  python -m pytest tests/test_crypto_cycle.py -q
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import market_brain
from brain import collect, config, market_hours, memory, news_intel, notify, obsidian, supabase
from brain import screen as screener
from brain.portfolio import Portfolio


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MEMORY_DIR", tmp_path)


class _Args:
    """The argparse namespace do_crypto_cycle reads, with the side effects off."""
    dry_run = True
    no_claude = True
    no_apply = False
    ignore_market_hours = False
    crypto_cycle = True
    cycle = anchor = research = weekly_review = digest = False


def _snap(price, ticker):
    return {"ticker": ticker, "indicators": {"price": price}, "patterns": []}


@pytest.fixture
def quiet_market(monkeypatch):
    """Every collector and every model call stubbed. Nothing escalates, nothing goes out."""
    market = {t: _snap(100.0, t) for t in config.CRYPTO_TICKERS}
    monkeypatch.setattr(collect, "collect_crypto_market", lambda with_news=True: market)
    monkeypatch.setattr(collect, "forexfactory_calendar", lambda: {"imminent": [], "trump_soon": []})
    monkeypatch.setattr(screener, "screen",
                        lambda m, calendar=None, held_tickers=None, force=False,
                        escalate_score=None:
                        {"escalate": False, "why": [], "triggers": [], "calendar_flags": []})
    monkeypatch.setattr(news_intel, "run",
                        lambda t, calendar=None, use_claude=True, venue="stock":
                        {"tickers": {}, "degraded": False, "venue": venue, "watchlist": list(t)})
    monkeypatch.setattr(news_intel, "escalation_reasons", lambda intel: [])
    monkeypatch.setattr(market_brain, "record_trades",
                        lambda *a, **k: None)
    monkeypatch.setattr(obsidian, "write_run", lambda *a, **k: None)
    monkeypatch.setattr(supabase, "push", lambda *a, **k: None)
    monkeypatch.setattr(notify, "send_email", lambda *a, **k: None)
    return market


# ── the CLI seam ────────────────────────────────────────────────────────────────

def _main(monkeypatch, argv):
    monkeypatch.setattr(sys, "argv", ["market_brain.py"] + argv)
    return market_brain.main()


def test_the_flag_reaches_the_crypto_cycle(monkeypatch):
    seen = []
    monkeypatch.setattr(market_brain, "do_crypto_cycle", lambda a, force=False: seen.append(a) or 0)
    assert _main(monkeypatch, ["--crypto-cycle", "--dry-run"]) == 0
    assert len(seen) == 1 and seen[0].crypto_cycle is True


def test_the_crypto_cycle_never_asks_the_equity_gate(monkeypatch):
    """The regression that would silently delete most of this venue's trading hours."""
    asked = []
    monkeypatch.setattr(market_hours, "gate",
                        lambda mode, now=None: (asked.append(mode), (False, "weekend"))[1])
    monkeypatch.setattr(market_brain, "do_crypto_cycle", lambda a, force=False: 0)
    assert _main(monkeypatch, ["--crypto-cycle"]) == 0
    assert asked == [], "the 24/7 book was put through the equity session gate"


def test_a_shut_equity_market_does_not_stop_the_crypto_book(monkeypatch):
    ran = []
    monkeypatch.setattr(market_hours, "gate", lambda mode, now=None: (False, "weekend — closed"))
    monkeypatch.setattr(market_brain, "do_crypto_cycle", lambda a, force=False: ran.append(True) or 0)
    monkeypatch.setattr(market_brain, "do_cycle", lambda a, mode="cycle", force=False: 0)
    _main(monkeypatch, ["--crypto-cycle"])
    assert ran == [True]


def test_the_equity_cycle_still_asks_the_gate(monkeypatch):
    """Routing crypto around the gate must not have unhooked it for everyone else."""
    asked = []
    monkeypatch.setattr(market_hours, "gate",
                        lambda mode, now=None: (asked.append(mode), (False, "weekend"))[1])
    monkeypatch.setattr(supabase, "push_heartbeat", lambda *a, **k: True)
    _main(monkeypatch, ["--cycle", "--dry-run"])
    assert asked == ["cycle"]


def test_crypto_reaches_the_gate_layer_under_its_own_mode_name(monkeypatch):
    args = _Args()
    assert market_brain._mode_of(args) == "crypto"


def test_the_two_cycles_are_mutually_exclusive(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["market_brain.py", "--cycle", "--crypto-cycle"])
    with pytest.raises(SystemExit):
        market_brain.main()


# ── what the cycle does once it is running ──────────────────────────────────────

def test_a_quiet_cycle_runs_the_stops_without_calling_the_model(monkeypatch, quiet_market):
    called = []
    monkeypatch.setattr(market_brain, "run_analysis",
                        lambda *a, **k: called.append(True) or {"signals": []})
    assert market_brain.do_crypto_cycle(_Args()) == 0
    assert called == [], "tier 2 ran on a cycle where nothing escalated"


def test_the_time_stop_fires_on_a_quiet_cycle(monkeypatch, quiet_market, tmp_path):
    """The reason the crypto cron runs overnight: nothing else closes an expired scalp."""
    pf = Portfolio(path=tmp_path / config.CRYPTO_RULES.ledger, mirror=False,
                   rules=config.CRYPTO_RULES)
    ok, msg = pf.apply_action({"action": "BUY", "ticker": "BTC-USD", "entry": 100.0,
                               "trade_type": "SCALP", "reason": "t"}, prices={"BTC-USD": 100.0})
    assert ok, msg
    opened = datetime.fromisoformat(pf.state["positions"]["BTC-USD"]["opened"])
    pf.state["positions"]["BTC-USD"]["opened"] = (opened - timedelta(hours=30)).isoformat()
    pf.save()

    args = _Args()
    args.dry_run = False              # so the exit is written back to the ledger
    monkeypatch.setattr(memory, "backfill_returns", lambda prices: 0)
    monkeypatch.setattr(memory, "log_events", lambda m, c, with_calendar=True: 0)
    assert market_brain.do_crypto_cycle(args) == 0

    after = Portfolio(path=tmp_path / config.CRYPTO_RULES.ledger, mirror=False,
                      rules=config.CRYPTO_RULES)
    assert "BTC-USD" not in after.state["positions"]
    assert after.state["closed_trades"][-1]["exit_kind"] == "time_stop"


def test_the_crypto_cycle_leaves_the_equity_book_alone(monkeypatch, quiet_market, tmp_path):
    stock = Portfolio(path=tmp_path / config.STOCK_RULES.ledger, mirror=False,
                      rules=config.STOCK_RULES)
    ok, _ = stock.apply_action({"action": "BUY", "ticker": "NVDA", "entry": 100.0,
                                "trade_type": "SCALP", "reason": "t"}, prices={"NVDA": 100.0})
    assert ok
    opened = datetime.fromisoformat(stock.state["positions"]["NVDA"]["opened"])
    stock.state["positions"]["NVDA"]["opened"] = (opened - timedelta(hours=40)).isoformat()
    stock.save()

    args = _Args()
    args.dry_run = False
    monkeypatch.setattr(memory, "backfill_returns", lambda prices: 0)
    monkeypatch.setattr(memory, "log_events", lambda m, c, with_calendar=True: 0)
    market_brain.do_crypto_cycle(args)

    # NVDA is 40h into an 8h scalp, but it is not this book's position and this run has no quote
    # for it. The crypto cycle must not reach into the equity ledger at all.
    after = Portfolio(path=tmp_path / config.STOCK_RULES.ledger, mirror=False,
                      rules=config.STOCK_RULES)
    assert "NVDA" in after.state["positions"]


def test_the_news_pass_is_rationed_and_is_told_the_venue(monkeypatch, quiet_market):
    """This asserted the whole universe was read, which was true and free at four tokens. At eighteen
    it would be five times the tier-1 bill on a job that runs every hour of every day, so the pass
    is now bounded — and the bound is the thing worth testing."""
    seen = {}
    monkeypatch.setattr(news_intel, "run",
                        lambda t, calendar=None, use_claude=True, venue="stock":
                        seen.update(tickers=list(t), venue=venue) or
                        {"tickers": {}, "degraded": False})
    market_brain.do_crypto_cycle(_Args())
    assert seen["venue"] == "crypto"
    assert len(seen["tickers"]) == config.CRYPTO_NEWS_TICKERS_PER_CYCLE
    assert set(seen["tickers"]) <= set(config.CRYPTO_TICKERS)


# ── how the tier-1 budget is spent ──────────────────────────────────────────────

class _Book:
    """Just enough Portfolio for the watchlist. It reads held_tickers() and nothing else."""
    def __init__(self, *held):
        self._held = list(held)

    def held_tickers(self):
        return self._held


def _at(hour):
    return datetime(2026, 7, 28, hour, 18, tzinfo=timezone.utc)


def test_a_held_token_is_read_even_when_the_budget_is_full():
    """The one asymmetry in the rationing. Missing news on a token we might buy costs an entry we
    would likely get next cycle; missing it on a token we are holding means the -15% stop is the
    first thing that tells us the thesis broke. So the budget is a floor for held names, not a cap
    — a book fuller than the budget reads the whole book."""
    held = config.CRYPTO_TICKERS[:config.CRYPTO_NEWS_TICKERS_PER_CYCLE + 2]
    out = market_brain._crypto_news_watchlist({"triggers": []}, _Book(*held), now=_at(9))
    assert set(held) <= set(out)


def test_screener_hits_outrank_the_rotation():
    trig = {"triggers": [{"ticker": "BONK-USD", "score": 90},
                         {"ticker": "RENDER-USD", "score": 80}]}
    out = market_brain._crypto_news_watchlist(trig, _Book("BTC-USD"), now=_at(9))
    assert out[0] == "BTC-USD"                       # held leads
    assert out[1:3] == ["BONK-USD", "RENDER-USD"]    # then triggers, score-sorted
    assert len(out) == config.CRYPTO_NEWS_TICKERS_PER_CYCLE


def test_the_quiet_tail_rotates_so_no_token_is_never_read():
    """A fixed slice would mean the bottom of the table is read literally never — the tokens most
    likely to be sitting on unpriced news are the ones nothing else is pointing at."""
    reads = set()
    for hour in range(24):
        reads |= set(market_brain._crypto_news_watchlist({"triggers": []}, _Book(), now=_at(hour)))
    assert reads == set(config.CRYPTO_TICKERS)


def test_the_rotation_does_not_repeat_a_token_within_a_cycle():
    trig = {"triggers": [{"ticker": "SOL-USD", "score": 90}]}
    out = market_brain._crypto_news_watchlist(trig, _Book("SOL-USD", "BTC-USD"), now=_at(3))
    assert len(out) == len(set(out))


def test_an_equity_ticker_never_reaches_the_crypto_news_pass():
    """Belt and braces on the venue split — a stray NVDA here would send Haiku off to read equity
    headlines on a crypto cycle and file them against the crypto book."""
    trig = {"triggers": [{"ticker": "NVDA", "score": 99}]}
    out = market_brain._crypto_news_watchlist(trig, _Book("AMD"), now=_at(9))
    assert "NVDA" not in out and "AMD" not in out


def test_the_macro_context_is_not_sent_to_the_news_pass(monkeypatch, quiet_market):
    """DXY and the VIX ride along as context. There is no headline sweep to run on either."""
    market = dict(quiet_market)
    market["DX-Y.NYB"] = _snap(98.0, "DX-Y.NYB")
    monkeypatch.setattr(collect, "collect_crypto_market", lambda with_news=True: market)
    seen = {}
    monkeypatch.setattr(news_intel, "run",
                        lambda t, calendar=None, use_claude=True, venue="stock":
                        seen.update(tickers=list(t)) or {"tickers": {}, "degraded": False})
    market_brain.do_crypto_cycle(_Args())
    assert "DX-Y.NYB" not in seen["tickers"]
    assert "^VIX" not in seen["tickers"]


def test_the_calendar_is_read_into_the_packet_but_not_into_memory(monkeypatch, quiet_market):
    """CPI is one event. The equity cycle already files it; filing it twice doubles the analogs."""
    seen = {}
    monkeypatch.setattr(memory, "backfill_returns", lambda prices: 0)
    monkeypatch.setattr(memory, "log_events",
                        lambda m, c, with_calendar=True: seen.update(with_calendar=with_calendar) or 0)
    args = _Args()
    args.dry_run = False
    market_brain.do_crypto_cycle(args)
    assert seen["with_calendar"] is False


def test_an_escalated_cycle_analyses_only_crypto_tickers(monkeypatch, quiet_market):
    monkeypatch.setattr(screener, "screen",
                        lambda m, calendar=None, held_tickers=None, force=False,
                        escalate_score=None:
                        {"escalate": True, "why": ["BTC breakout"],
                         "triggers": [{"ticker": "BTC-USD", "score": 90, "reasons": []}],
                         "calendar_flags": []})
    seen = {}
    monkeypatch.setattr(market_brain, "run_analysis",
                        lambda mode, market, sr, cal, pf, args, focus=None, **k:
                        seen.update(mode=mode, focus=list(focus or []), kw=k) or {"signals": []})
    market_brain.do_crypto_cycle(_Args())
    assert seen["mode"] == "crypto"
    assert set(seen["focus"]) == set(config.CRYPTO_TICKERS)
    assert seen["kw"]["with_fundamentals"] is False


# ── the escalation floor ────────────────────────────────────────────────────────
# "Any flagged ticker escalates" worked while the universe was four tokens. At eighteen it stopped
# gating anything: a Bollinger squeeze fires across most of the book whenever crypto vol
# compresses, so a score of 3 is the resting state. These tests pin the separation between being
# on the ranked list and being worth an Opus run.

def _scored(**by_ticker):
    """A market of bare snapshots carrying one pattern each, weighted to hit a wanted score."""
    market = {}
    for ticker, score in by_ticker.items():
        market[ticker] = {"ticker": ticker, "indicators": {},
                          "patterns": [{"name": "test pattern", "detail": "d", "strength": score}]}
    return market


def test_a_drifting_book_no_longer_buys_a_deep_run():
    market = _scored(**{"BTC-USD": 3, "SOL-USD": 3, "DOGE-USD": 4})
    r = screener.screen(market, escalate_score=5)
    assert len(r["triggers"]) == 3, "they still reach the packet"
    assert r["escalate"] is False
    assert any("escalation bar" in w for w in r["why"])


def test_one_real_move_still_does():
    r = screener.screen(_scored(**{"BTC-USD": 3, "SOL-USD": 6}), escalate_score=5)
    assert r["escalate"] is True


def test_a_held_name_escalates_at_any_score():
    """A position under stress is the one case where paying for a look is always worth it."""
    r = screener.screen(_scored(**{"BONK-USD": 2}), held_tickers=["BONK-USD"], escalate_score=5)
    assert r["escalate"] is True


def test_the_floor_does_not_override_a_forced_or_calendar_run():
    quiet = _scored(**{"BTC-USD": 3})
    assert screener.screen(quiet, force=True, escalate_score=5)["escalate"] is True
    cal = {"imminent": [{"title": "Core CPI m/m", "impact": "High", "hours_away": 1.0}]}
    assert screener.screen(quiet, calendar=cal, escalate_score=5)["escalate"] is True


def test_the_equity_screener_is_untouched():
    """No `escalate_score` means the original behaviour, exactly. The stock book did not change."""
    r = screener.screen(_scored(**{"AAPL": 3}))
    assert r["escalate"] is True


def test_the_crypto_cycle_passes_its_floor_to_the_screener(monkeypatch, quiet_market):
    seen = {}

    def _screen(m, calendar=None, held_tickers=None, force=False, escalate_score=None):
        seen["escalate_score"] = escalate_score
        return {"escalate": False, "why": [], "triggers": [], "calendar_flags": []}

    monkeypatch.setattr(screener, "screen", _screen)
    market_brain.do_crypto_cycle(_Args())
    assert seen["escalate_score"] == config.CRYPTO_ESCALATE_SCORE
