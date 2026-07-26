"""Tests for the per-cycle API budget (Phase 6): daily-bar cache, Finnhub throttle, news reuse.

The cache here is the one piece of the pipeline where being *slightly* stale is not a performance
detail but a wrong answer: `indicators.price` marks the paper portfolio and fires the -7% hard
stop. So most of these tests are about the cache refusing to serve a stale price, not about it
serving a fast one.

Run:  python -m pytest tests/test_bars_cache.py -q
"""
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import market_brain
from brain import collect, config


# A Wednesday inside regular trading hours, and the same day after the close.
OPEN_ET = datetime(2026, 7, 22, 11, 0, tzinfo=config.ET)
CLOSED_ET = datetime(2026, 7, 22, 18, 0, tzinfo=config.ET)


def _daily(last_date, n=60, close=100.0):
    """A daily frame ending on `last_date`, shaped like yfinance's."""
    idx = pd.date_range(end=pd.Timestamp(last_date, tz=config.ET), periods=n, freq="D")
    return pd.DataFrame({"Open": close, "High": close + 1, "Low": close - 1, "Close": close,
                         "Volume": 1_000_000.0, "Dividends": 0.0, "Stock Splits": 0.0},
                        index=idx)


def _intraday(day, closes=(101.0, 103.0, 102.5)):
    idx = [pd.Timestamp(datetime.combine(day, datetime.min.time()).replace(hour=9 + i),
                        tz=config.ET) for i in range(len(closes))]
    return pd.DataFrame({"Open": list(closes), "High": [c + 0.5 for c in closes],
                         "Low": [c - 0.5 for c in closes], "Close": list(closes),
                         "Volume": [10_000.0] * len(closes)}, index=idx)


def _stamp_cache(ticker, when):
    """Backdate the cache file so it looks written during session `when`.

    The on-disk session stamp is the file's mtime — a frame fetched pre-market legitimately has no
    bar for today, so the frame itself can't say which session it belongs to. Tests run on a real
    clock against a fixed fake session, so they have to say it out loud.
    """
    ts = when.timestamp()
    os.utime(collect._bars_path(ticker), (ts, ts))


class _Fetches:
    """Stands in for fetch_daily and counts how often the wire was actually touched."""

    def __init__(self, frame):
        self.frame, self.n = frame, 0

    def __call__(self, ticker, period="1y"):
        self.n += 1
        return self.frame


@pytest.fixture
def bars(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "BARS_CACHE_DIR", tmp_path / "bars")
    monkeypatch.setattr(config, "BARS_CACHE", True)
    collect._bars_mem.clear()
    yield
    collect._bars_mem.clear()


# ── the cache ──────────────────────────────────────────────────────────────────

def test_a_cold_cache_fetches_once_and_a_warm_one_not_at_all(bars, monkeypatch):
    fetch = _Fetches(_daily(OPEN_ET.date()))
    monkeypatch.setattr(collect, "fetch_daily", fetch)
    intra = _intraday(OPEN_ET.date())

    first = collect.daily_bars("AAPL", intraday=intra, now_et=OPEN_ET)
    assert fetch.n == 1 and not first.empty

    collect._bars_mem.clear()                      # a fresh process, same session
    _stamp_cache("AAPL", OPEN_ET)
    second = collect.daily_bars("AAPL", intraday=intra, now_et=OPEN_ET)
    assert fetch.n == 1, "the second cycle re-downloaded a frame it had on disk"
    assert len(second) == len(first)


def test_the_cached_frame_still_reports_a_live_price(bars, monkeypatch):
    """A frozen price is not a slow cache — it is a wrong -7% stop decision."""
    monkeypatch.setattr(collect, "fetch_daily", _Fetches(_daily(OPEN_ET.date(), close=100.0)))
    collect.daily_bars("AAPL", intraday=_intraday(OPEN_ET.date()), now_et=OPEN_ET)

    moved = _intraday(OPEN_ET.date(), closes=(101.0, 104.0, 92.4))
    df = collect.daily_bars("AAPL", intraday=moved, now_et=OPEN_ET)
    assert df["Close"].iloc[-1] == 92.4            # the crash, not yesterday's 100
    assert df["High"].iloc[-1] == 104.5
    assert df["Low"].iloc[-1] == 91.9
    assert df["Volume"].iloc[-1] == 30_000.0


def test_todays_bar_is_replaced_not_appended_twice(bars, monkeypatch):
    monkeypatch.setattr(collect, "fetch_daily", _Fetches(_daily(OPEN_ET.date())))
    n_before = len(collect.daily_bars("AAPL", intraday=_intraday(OPEN_ET.date()), now_et=OPEN_ET))
    df = collect.daily_bars("AAPL", intraday=_intraday(OPEN_ET.date()), now_et=OPEN_ET)
    assert len(df) == n_before
    assert sum(1 for ts in df.index if collect._et_date(ts) == OPEN_ET.date()) == 1


def test_a_frame_fetched_before_the_open_gains_todays_bar_rather_than_losing_it(bars, monkeypatch):
    """The pre-market anchor caches a frame ending yesterday. The 10am cycle must add today."""
    yesterday = OPEN_ET.date() - timedelta(days=1)
    monkeypatch.setattr(collect, "fetch_daily", _Fetches(_daily(yesterday)))
    pre = collect.daily_bars("AAPL", intraday=pd.DataFrame(), now_et=OPEN_ET.replace(hour=8))
    assert collect._et_date(pre.index[-1]) == yesterday

    df = collect.daily_bars("AAPL", intraday=_intraday(OPEN_ET.date()), now_et=OPEN_ET)
    assert collect._et_date(df.index[-1]) == OPEN_ET.date()
    assert len(df) == len(pre) + 1


def test_an_open_session_with_no_intraday_refetches_instead_of_serving_a_stale_close(bars,
                                                                                     monkeypatch):
    fetch = _Fetches(_daily(OPEN_ET.date()))
    monkeypatch.setattr(collect, "fetch_daily", fetch)
    collect.daily_bars("AAPL", intraday=_intraday(OPEN_ET.date()), now_et=OPEN_ET)
    collect.daily_bars("AAPL", intraday=pd.DataFrame(), now_et=OPEN_ET)
    assert fetch.n == 2


def test_with_the_market_shut_the_cache_is_served_without_intraday(bars, monkeypatch):
    """Nothing can move after the close, so a cache hit is the whole point of the cache."""
    fetch = _Fetches(_daily(CLOSED_ET.date()))
    monkeypatch.setattr(collect, "fetch_daily", fetch)
    collect.daily_bars("^VIX", intraday=pd.DataFrame(), now_et=CLOSED_ET)
    collect.daily_bars("^VIX", intraday=pd.DataFrame(), now_et=CLOSED_ET)
    assert fetch.n == 1


def test_yesterdays_cache_is_not_used_today(bars, monkeypatch):
    fetch = _Fetches(_daily(OPEN_ET.date()))
    monkeypatch.setattr(collect, "fetch_daily", fetch)
    collect.daily_bars("AAPL", intraday=_intraday(OPEN_ET.date()), now_et=OPEN_ET)
    collect._bars_mem.clear()
    _stamp_cache("AAPL", OPEN_ET)                  # a real cache from yesterday's session
    tomorrow = OPEN_ET + timedelta(days=1)
    collect.daily_bars("AAPL", intraday=_intraday(tomorrow.date()), now_et=tomorrow)
    assert fetch.n == 2


def test_a_corrupt_cache_file_is_a_miss_not_a_crash(bars, monkeypatch):
    config.BARS_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    collect._bars_path("AAPL").write_text("this is not a dataframe", encoding="utf-8")
    fetch = _Fetches(_daily(OPEN_ET.date()))
    monkeypatch.setattr(collect, "fetch_daily", fetch)
    df = collect.daily_bars("AAPL", intraday=_intraday(OPEN_ET.date()), now_et=OPEN_ET)
    assert fetch.n == 1 and not df.empty


def test_the_cache_can_be_switched_off_entirely(bars, monkeypatch):
    monkeypatch.setattr(config, "BARS_CACHE", False)
    fetch = _Fetches(_daily(OPEN_ET.date()))
    monkeypatch.setattr(collect, "fetch_daily", fetch)
    for _ in range(3):
        collect.daily_bars("AAPL", intraday=_intraday(OPEN_ET.date()), now_et=OPEN_ET)
    assert fetch.n == 3


def test_a_ticker_with_an_awkward_symbol_still_gets_a_cache_file(bars, monkeypatch):
    monkeypatch.setattr(collect, "fetch_daily", _Fetches(_daily(CLOSED_ET.date())))
    collect.daily_bars("DX-Y.NYB", intraday=pd.DataFrame(), now_et=CLOSED_ET)
    assert collect._bars_path("DX-Y.NYB").exists()


def test_an_empty_fetch_writes_nothing_and_keeps_returning_empty(bars, monkeypatch):
    monkeypatch.setattr(collect, "fetch_daily", _Fetches(pd.DataFrame()))
    df = collect.daily_bars("NOPE", intraday=pd.DataFrame(), now_et=OPEN_ET)
    assert df.empty
    assert not collect._bars_path("NOPE").exists()


def test_both_price_fetchers_pass_an_explicit_timeout(monkeypatch):
    """A hung yfinance socket used to be able to eat a whole 30-minute slot."""
    seen = []

    class _T:
        def __init__(self, ticker):
            pass

        def history(self, **kw):
            seen.append(kw.get("timeout"))
            return _daily(OPEN_ET.date())

    monkeypatch.setattr(collect.yf, "Ticker", _T)
    collect.fetch_daily("AAPL")
    collect.fetch_intraday("AAPL")
    assert seen == [config.YF_TIMEOUT, config.YF_TIMEOUT]


# ── the Finnhub throttle ───────────────────────────────────────────────────────

def test_the_throttle_lets_the_allowance_through_then_waits(monkeypatch):
    monkeypatch.setattr(config, "FINNHUB_MAX_PER_MIN", 3)
    monkeypatch.setattr(collect, "_finnhub_calls", [])
    clock, slept = {"t": 1000.0}, []
    monkeypatch.setattr(collect.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(collect.time, "sleep", lambda s: (slept.append(s),
                                                          clock.__setitem__("t", clock["t"] + s)))
    for _ in range(3):
        collect._finnhub_throttle()
    assert slept == []
    collect._finnhub_throttle()          # the 4th must wait for the window to roll
    assert slept and sum(slept) >= 55


def test_calls_older_than_a_minute_stop_counting(monkeypatch):
    monkeypatch.setattr(config, "FINNHUB_MAX_PER_MIN", 2)
    monkeypatch.setattr(collect, "_finnhub_calls", [])
    clock = {"t": 500.0}
    monkeypatch.setattr(collect.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(collect.time, "sleep", lambda s: pytest.fail("should not have waited"))
    collect._finnhub_throttle()
    collect._finnhub_throttle()
    clock["t"] += 61
    collect._finnhub_throttle()
    assert len(collect._finnhub_calls) == 1


def test_every_finnhub_caller_goes_through_the_throttle(monkeypatch):
    """A limiter one code path skips is not a limiter."""
    hits = []
    monkeypatch.setattr(collect, "_finnhub_throttle", lambda: hits.append(1))
    monkeypatch.setattr(config, "FINNHUB_API_KEY", "test-key")

    class _R:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return []

    monkeypatch.setattr(collect.requests, "get", lambda *a, **k: _R())
    collect.finnhub_news("AAPL")
    monkeypatch.setattr(collect, "_fund_cache", {})
    collect._finnhub_json("metric", "AAPL", "/stock/metric", {"symbol": "AAPL"})
    assert len(hits) == 2


def test_no_key_means_no_throttle_wait(monkeypatch):
    monkeypatch.setattr(config, "FINNHUB_API_KEY", "")
    monkeypatch.setattr(collect, "_finnhub_throttle",
                        lambda: pytest.fail("throttled a call that was never going to be made"))
    assert collect.finnhub_news("AAPL") == []


# ── news reuse ─────────────────────────────────────────────────────────────────

def test_the_snapshot_gets_haikus_scored_headlines_not_a_second_scrape():
    market = {"AAPL": {"ticker": "AAPL", "indicators": {}}, "SPY": {"ticker": "SPY"}}
    intel = {"headlines": {"AAPL": [{"title": "8-K filed", "source": "SEC",
                                     "crowding": "under-covered", "outlets": 1}]}}
    market_brain._attach_news(market, intel)
    assert market["AAPL"]["news"][0]["crowding"] == "under-covered"
    assert "news" not in market["SPY"]


def test_headlines_for_a_ticker_we_never_snapshotted_are_dropped_quietly():
    market = {"AAPL": {"ticker": "AAPL"}}
    market_brain._attach_news(market, {"headlines": {"ZZZZ": [{"title": "x"}]}})
    assert set(market) == {"AAPL"}


def test_a_degraded_news_pass_leaves_the_snapshots_alone():
    market = {"AAPL": {"ticker": "AAPL"}}
    market_brain._attach_news(market, {"degraded": True})
    market_brain._attach_news(market, None)
    assert "news" not in market["AAPL"]
