"""A free Finnhub key is 60 calls a minute, and the brain wants 14 names an anchor.

The wider company pull Lind asked for — earnings, margins, growth, analyst revisions, insider
buying — is only affordable because nothing here is fetched twice. These tests pin the three
things that make that true and stay true: the disk cache honours per-endpoint TTLs, a premium
endpoint's 401 is remembered so we stop asking, and the parsed output is JSON-clean.

They also pin the two judgement calls inside the parsing. An option exercise is not an insider
buying his own stock, and an analyst spread means nothing without the month-over-month change.

Run:  python -m pytest tests/ -q
"""
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from brain import collect, config, jsonio

TODAY = datetime.now(config.UTC).date()


def _ago(days):
    return (TODAY - timedelta(days=days)).isoformat()


class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


@pytest.fixture
def fund_cache(tmp_path, monkeypatch):
    """A clean on-disk cache per test — the module-level one would leak between them."""
    monkeypatch.setattr(collect, "_FUND_CACHE_PATH", tmp_path / "FUNDAMENTALS-CACHE.json")
    monkeypatch.setattr(collect, "_fund_cache", {})
    monkeypatch.setattr(config, "FINNHUB_API_KEY", "test-key")
    return tmp_path / "FUNDAMENTALS-CACHE.json"


class _Counter:
    """Stands in for requests.get and records what was actually asked of the network."""

    def __init__(self, router):
        self.router = router
        self.calls = []

    def __call__(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, dict(params or {})))
        for frag, resp in self.router.items():
            if frag in url:
                return resp(params) if callable(resp) else resp
        return _Resp({}, 404)

    @property
    def n(self):
        return len(self.calls)


# ── the cache ───────────────────────────────────────────────────────────────────

def test_a_second_read_inside_the_ttl_never_touches_the_network(fund_cache, monkeypatch):
    get = _Counter({"/stock/metric": _Resp({"metric": {"peTTM": 30}})})
    monkeypatch.setattr(collect.requests, "get", get)

    first = collect._finnhub_json("metric", "AAPL", "/stock/metric", {"symbol": "AAPL"})
    second = collect._finnhub_json("metric", "AAPL", "/stock/metric", {"symbol": "AAPL"})

    assert first == second == {"metric": {"peTTM": 30}}
    assert get.n == 1


def test_an_expired_entry_is_refetched(fund_cache, monkeypatch):
    get = _Counter({"/stock/metric": _Resp({"metric": {"peTTM": 30}})})
    monkeypatch.setattr(collect.requests, "get", get)
    collect._finnhub_json("metric", "AAPL", "/stock/metric", {"symbol": "AAPL"})

    # age the entry past its own TTL rather than sleeping through 24 hours
    collect._fund_cache["metric:AAPL"]["at"] -= collect._FUND_TTL["metric"] + 1
    collect._finnhub_json("metric", "AAPL", "/stock/metric", {"symbol": "AAPL"})

    assert get.n == 2


def test_each_endpoint_keeps_its_own_ttl(fund_cache, monkeypatch):
    """Earnings go stale in hours; a balance sheet does not. One shared TTL would be wrong twice."""
    assert collect._FUND_TTL["earnings"] < collect._FUND_TTL["metric"]
    assert collect._FUND_TTL["insider"] < collect._FUND_TTL["recommendation"]


def test_a_premium_endpoint_is_asked_once_and_then_left_alone(fund_cache, monkeypatch):
    """403 means the free key will never have this. Retrying it every cycle spends the budget
    on a guaranteed refusal."""
    get = _Counter({"/stock/insider-transactions": _Resp({}, 403)})
    monkeypatch.setattr(collect.requests, "get", get)

    assert collect._finnhub_json("insider", "AAPL", "/stock/insider-transactions", {}) is None
    assert collect._finnhub_json("insider", "AAPL", "/stock/insider-transactions", {}) is None
    assert get.n == 1


def test_a_network_failure_is_not_cached(fund_cache, monkeypatch):
    """A timeout is transient — caching it would blind the brain until the TTL expired."""
    def boom(*a, **kw):
        raise OSError("connection reset")

    monkeypatch.setattr(collect.requests, "get", boom)
    assert collect._finnhub_json("metric", "AAPL", "/stock/metric", {}) is None
    assert "metric:AAPL" not in collect._fund_cache


def test_no_key_means_no_request(fund_cache, monkeypatch):
    monkeypatch.setattr(config, "FINNHUB_API_KEY", "")
    get = _Counter({"/stock/metric": _Resp({"metric": {}})})
    monkeypatch.setattr(collect.requests, "get", get)
    assert collect._finnhub_json("metric", "AAPL", "/stock/metric", {}) is None
    assert get.n == 0


def test_the_cache_survives_a_save_and_reload(fund_cache, monkeypatch):
    get = _Counter({"/stock/metric": _Resp({"metric": {"peTTM": 30}})})
    monkeypatch.setattr(collect.requests, "get", get)
    collect._finnhub_json("metric", "AAPL", "/stock/metric", {})
    collect.fundamentals_cache_save()

    monkeypatch.setattr(collect, "_fund_cache", None)          # cold process
    assert collect._finnhub_json("metric", "AAPL", "/stock/metric", {}) == {"metric": {"peTTM": 30}}
    assert get.n == 1


def test_saving_an_untouched_cache_writes_nothing(fund_cache, monkeypatch):
    monkeypatch.setattr(collect, "_fund_cache", None)
    collect.fundamentals_cache_save()
    assert not fund_cache.exists()


# ── insider transactions ────────────────────────────────────────────────────────

def test_only_open_market_trades_count_as_insider_conviction():
    """A (grant) and M (option exercise) are payroll. Counting them shows buying every vest."""
    rows = [
        {"transactionCode": "P", "change": 5000, "name": "CEO", "transactionDate": _ago(10)},
        {"transactionCode": "A", "change": 90000, "name": "CFO", "transactionDate": _ago(10)},
        {"transactionCode": "M", "change": 40000, "name": "CTO", "transactionDate": _ago(10)},
    ]
    out = collect._summarize_insiders(rows)
    assert out["shares_bought"] == 5000
    assert out["shares_sold"] == 0
    assert out["buyers"] == 1
    assert out["read"] == "net buying"


def test_net_selling_is_reported_as_selling():
    rows = [
        {"transactionCode": "P", "change": 1000, "name": "A", "transactionDate": _ago(5)},
        {"transactionCode": "S", "change": -8000, "name": "B", "transactionDate": _ago(5)},
    ]
    out = collect._summarize_insiders(rows)
    assert out["shares_sold"] == 8000            # sign of `change` must not flip the tally
    assert out["net_shares"] == -7000
    assert out["read"] == "net selling"


def test_trades_outside_the_window_are_dropped():
    rows = [{"transactionCode": "P", "change": 5000, "name": "CEO", "transactionDate": _ago(200)}]
    assert collect._summarize_insiders(rows, days=90) is None


def test_no_open_market_activity_reports_nothing_rather_than_zero():
    """None means "no signal". A zeroed dict reads as "insiders were flat", which is a claim."""
    assert collect._summarize_insiders([]) is None
    assert collect._summarize_insiders(
        [{"transactionCode": "A", "change": 1000, "transactionDate": _ago(1)}]) is None


# ── analyst recommendations ─────────────────────────────────────────────────────

def test_the_analyst_revision_is_what_gets_reported():
    rows = [
        {"period": "2026-06-01", "strongBuy": 10, "buy": 5, "hold": 2, "sell": 1, "strongSell": 0},
        {"period": "2026-07-01", "strongBuy": 14, "buy": 6, "hold": 1, "sell": 0, "strongSell": 0},
    ]
    out = collect._summarize_recommendations(rows)
    assert out["period"] == "2026-07-01"          # newest wins regardless of input order
    assert out["score"] == 34                     # 14*2 + 6
    assert out["score_change_1m"] == 34 - 24
    assert out["prior_period"] == "2026-06-01"


def test_a_single_period_has_no_change_to_report():
    out = collect._summarize_recommendations(
        [{"period": "2026-07-01", "strongBuy": 1, "buy": 1, "hold": 0, "sell": 0, "strongSell": 0}])
    assert "score_change_1m" not in out


def test_no_coverage_is_none():
    assert collect._summarize_recommendations([]) is None
    assert collect._summarize_recommendations(None) is None


# ── the earnings calendar ───────────────────────────────────────────────────────

def test_one_bulk_call_serves_every_ticker(fund_cache, monkeypatch):
    soon, later = _ago(-3), _ago(-11)
    payload = {"earningsCalendar": [
        {"symbol": "AAPL", "date": later, "hour": "amc", "epsEstimate": 2.1},
        {"symbol": "AAPL", "date": soon, "hour": "amc", "epsEstimate": 2.0},
        {"symbol": "NVDA", "date": soon, "hour": "bmo", "epsEstimate": 1.1},
        {"symbol": "ZZZZ", "date": soon, "hour": "bmo", "epsEstimate": 9.9},
    ]}
    get = _Counter({"/calendar/earnings": _Resp(payload)})
    monkeypatch.setattr(collect.requests, "get", get)
    monkeypatch.setattr(config, "FOCUS_TICKERS", ["AAPL", "NVDA"])

    out = collect.earnings_calendar_map(days=14)
    collect.earnings_calendar_map(days=14)

    assert get.n == 1                                  # cached, not re-pulled per name
    assert set(out) == {"AAPL", "NVDA"}                # off-watchlist names dropped
    assert out["AAPL"]["date"] == soon                 # the nearest print, not the first row


def test_the_flat_calendar_is_sorted_nearest_first(fund_cache, monkeypatch):
    monkeypatch.setattr(collect, "earnings_calendar_map",
                        lambda days=14: {"AAPL": {"date": "2026-08-10"},
                                         "NVDA": {"date": "2026-08-03"}})
    rows = collect.finnhub_earnings_calendar()
    assert [r["ticker"] for r in rows] == ["NVDA", "AAPL"]


# ── the whole fundamentals pull ─────────────────────────────────────────────────

@pytest.fixture
def wired(fund_cache, monkeypatch):
    """A full Finnhub surface: earnings, metrics, calendar, analysts, insiders."""
    router = {
        "/stock/earnings": _Resp([
            {"period": "2026-05-28", "actual": 2.4, "estimate": 2.1, "surprisePercent": 14.3},
            {"period": "2026-02-26", "actual": 1.9, "estimate": 1.9, "surprisePercent": 0.0},
            {"period": "2025-11-20", "actual": 1.7, "estimate": 1.5, "surprisePercent": 13.3},
            {"period": "2025-08-27", "actual": 1.3, "estimate": 1.2, "surprisePercent": 8.3},
        ]),
        "/stock/metric": _Resp({"metric": {
            "peTTM": 54.2, "psTTM": 28.1, "pbQuarterly": 46.0,
            "revenuePerShareTTM": 5.42, "grossMarginTTM": 75.1,
            "operatingMarginTTM": 62.4, "netProfitMarginTTM": 55.8,
            "roeTTM": 119.2, "roaTTM": 65.3,
            "totalDebt/totalEquityQuarterly": 0.12,
            "currentRatioQuarterly": 4.4, "quickRatioQuarterly": 3.9,
            "revenueGrowthTTMYoy": 114.2, "epsGrowthTTMYoy": 168.0,
            "revenueGrowth5Y": 69.4, "beta": 1.74,
            "dividendYieldIndicatedAnnual": 0.02,
            "52WeekHigh": 195.0, "52WeekLow": 86.6,
            "52WeekPriceReturnDaily": 44.1,
            "10DayAverageTradingVolume": 210.4,
        }}),
        "/calendar/earnings": _Resp({"earningsCalendar": [
            {"symbol": "NVDA", "date": _ago(-6), "hour": "amc", "epsEstimate": 2.6}]}),
        "/stock/recommendation": _Resp([
            {"period": "2026-07-01", "strongBuy": 30, "buy": 12, "hold": 3, "sell": 1,
             "strongSell": 0},
            {"period": "2026-06-01", "strongBuy": 26, "buy": 14, "hold": 4, "sell": 1,
             "strongSell": 0},
        ]),
        "/stock/insider-transactions": _Resp({"data": [
            {"transactionCode": "S", "change": -12000, "name": "EVP",
             "transactionDate": _ago(20)},
            {"transactionCode": "A", "change": 300000, "name": "CEO",
             "transactionDate": _ago(20)},
        ]}),
    }
    get = _Counter(router)
    monkeypatch.setattr(collect.requests, "get", get)
    monkeypatch.setattr(config, "FOCUS_TICKERS", ["NVDA"])
    return get


def test_the_long_term_thesis_now_has_something_to_stand_on(wired):
    """The old pull kept five fields; a LONG_TERM call needs margins, growth and leverage."""
    out = collect.finnhub_fundamentals("NVDA")
    for field in ("gross_margin", "operating_margin", "profit_margin", "roe", "roa",
                  "debt_to_equity", "current_ratio", "revenue_growth_yoy", "eps_growth_yoy",
                  "revenue_growth_5y", "beta", "52w_high", "52w_low"):
        assert out.get(field) is not None, f"{field} missing from fundamentals"
    assert out["debt_to_equity"] == 0.12          # the slash-named Finnhub key is mapped
    assert out["eps_beats_last_4"] == "3/4"
    assert out["last_eps_surprise_pct"] == 14.3


def test_the_days_to_earnings_countdown_is_computed(wired):
    out = collect.finnhub_fundamentals("NVDA")
    assert out["next_earnings_date"] == _ago(-6)
    assert out["days_to_earnings"] == 6
    assert out["next_eps_estimate"] == 2.6


def test_analysts_and_insiders_ride_along(wired):
    out = collect.finnhub_fundamentals("NVDA")
    assert out["analysts"]["score_change_1m"] == 72 - 66
    assert out["insiders"]["read"] == "net selling"
    assert out["insiders"]["shares_sold"] == 12000     # the grant is excluded


def test_the_old_field_name_still_resolves(wired):
    """`revenue_ttm` is read by the existing prompt and journal; renaming it silently would
    blank the field rather than error."""
    out = collect.finnhub_fundamentals("NVDA")
    assert out["revenue_ttm"] == out["revenue_per_share_ttm"] == 5.42


def test_a_nan_from_the_api_never_reaches_the_packet(fund_cache, monkeypatch):
    """json.dumps emits a bare NaN token, and the model then reads "NaN" as a number. This
    already happened once, on MU."""
    router = {"/stock/metric": _Resp({"metric": {"peTTM": float("nan"), "roeTTM": 40.0,
                                                 "beta": "n/a"}}),
              "/calendar/earnings": _Resp({"earningsCalendar": []})}
    monkeypatch.setattr(collect.requests, "get", _Counter(router))
    monkeypatch.setattr(config, "FOCUS_TICKERS", ["MU"])

    out = collect.finnhub_fundamentals("MU", with_analysts=False, with_insiders=False)

    assert "pe_ttm" not in out          # unknowable, so absent — not NaN, not 0
    assert "beta" not in out
    assert out["roe"] == 40.0
    assert "NaN" not in jsonio.dumps(out)
    json.loads(jsonio.dumps(out))       # would raise if a bare NaN slipped through


def test_a_dead_key_degrades_to_an_empty_dict_not_a_crash(fund_cache, monkeypatch):
    monkeypatch.setattr(config, "FINNHUB_API_KEY", "")
    assert collect.finnhub_fundamentals("NVDA") == {"revenue_ttm": None}
