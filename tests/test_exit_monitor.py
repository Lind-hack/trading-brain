"""A trade also dies of things the stop cannot see.

Lind's ask: *"monitor the trades that we are in ... give me a sell signal when the trade has peaked
or there is no more money to be made"*, on ticker news, on global news — *"if Trump starts bombing
Iran again and before that we were in a scalp trade for long"* — and on the chart turning.

His answer on what to do about it was `email alert only, you decide`, so the load-bearing assertion
in this file is the negative one: the monitor never closes anything.

Run:  python -m pytest tests/test_exit_monitor.py -q
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import market_brain
from brain import config, exits, notify
from brain.portfolio import Portfolio

NOW = datetime(2026, 7, 30, 15, 0, tzinfo=config.UTC)


@pytest.fixture
def store():
    return {"version": 1, "alerts": {}}


@pytest.fixture
def pf(tmp_path):
    return Portfolio(path=tmp_path / "PORTFOLIO.json")


def _buy(pf, ticker="NVDA", price=100.0, trade_type="SHORT_TERM", **extra):
    action = {"action": "BUY", "ticker": ticker, "entry": price, "trade_type": trade_type,
              "target_weight_pct": 8.0, "confidence": 70, "reason": "test"}
    action.update(extra)
    ok, msg = pf.apply_action(action, prices={ticker: price})
    assert ok, msg
    return pf.state["positions"][ticker]


def _market(ticker="NVDA", **ind):
    base = {"price": 100.0, "ma20": 95.0, "macd_d": "bullish, hist +0.40", "rsi14_d": 58.0,
            "range_pos": 0.55, "ext_atr": 0.6}
    base.update(ind)
    return {ticker: {"ticker": ticker, "indicators": base}}


def _intel(sentiment=0, materiality="low", macro=0, **kw):
    out = {"macro_sentiment": macro, "macro_read": kw.pop("macro_read", ""),
           "tickers": {"NVDA": {"sentiment": sentiment, "materiality": materiality,
                                "catalyst": kw.pop("catalyst", "regulatory"),
                                "summary": kw.pop("summary", "a summary"),
                                "is_fresh": kw.pop("is_fresh", True)}}}
    out.update(kw)
    return out


def _review(pf, prices=None, **kw):
    kw.setdefault("store", {"version": 1, "alerts": {}})
    kw.setdefault("now", NOW)
    return exits.review(pf, prices or {"NVDA": 100.0}, **kw)


# ── the one thing that must never happen ────────────────────────────────────────

def test_the_monitor_never_closes_a_position(pf):
    """Lind's call was "email alert only, you decide". A monitor that could exit would make the
    mechanical stops and the advisory alerts indistinguishable in the ledger."""
    _buy(pf)
    alerts = _review(pf, {"NVDA": 60.0},
                     market=_market(price=60.0, ma20=95.0, macd_d="bearish, hist -1.10",
                                    rsi14_d=31.0, range_pos=0.04),
                     intel=_intel(sentiment=-90, materiality="high"))
    assert alerts and alerts[0]["verdict"] == "EXIT"
    assert "NVDA" in pf.state["positions"]
    assert pf.state["closed_trades"] == []
    assert pf.exits_this_run == []


def test_a_quiet_book_produces_nothing(pf):
    _buy(pf)
    assert _review(pf, market=_market(), intel=_intel()) == []


# ── the story turned ────────────────────────────────────────────────────────────

def test_high_materiality_news_against_a_long_is_flagged(pf):
    _buy(pf)
    alerts = _review(pf, market=_market(), intel=_intel(sentiment=-30, materiality="high"))
    assert alerts[0]["verdict"] == "TRIM"
    assert "news" in alerts[0]["kinds"]


def test_news_in_the_position_s_favour_is_not_an_exit(pf):
    _buy(pf)
    assert _review(pf, market=_market(), intel=_intel(sentiment=95, materiality="high")) == []


def test_stale_news_does_not_count(pf):
    """`is_fresh: false` is tier 1 saying it already told us. Re-alerting on it would make the
    monitor repeat itself through a different door than the cooldown guards."""
    _buy(pf)
    assert _review(pf, market=_market(),
                   intel=_intel(sentiment=-90, materiality="high", is_fresh=False)) == []


def test_a_headline_alone_never_reaches_exit(pf):
    """Deliberate: the tape has to agree. One story is a reason to look, not to close."""
    _buy(pf)
    alerts = _review(pf, market=_market(), intel=_intel(sentiment=-100, materiality="high"))
    assert alerts[0]["verdict"] != "EXIT"


# ── the world turned ────────────────────────────────────────────────────────────

def test_a_macro_shock_reaches_every_long_on_the_book(pf):
    """Lind's own example: long a scalp, and then the world changes under it. Nothing in the
    ticker's own news moves — the whole tape does."""
    _buy(pf, "NVDA", trade_type="SCALP")
    _buy(pf, "AMD", trade_type="SCALP")
    alerts = _review(pf, {"NVDA": 100.0, "AMD": 100.0},
                     market={**_market(), **{"AMD": {"indicators": _market()["NVDA"]["indicators"]}},
                             "^VIX": {"indicators": {"price": 34.0, "ma20": 17.0}}},
                     intel=_intel(macro=-85, macro_read="risk-off on the strike headlines"))
    assert {a["ticker"] for a in alerts} == {"NVDA", "AMD"}
    assert all("macro" in a["kinds"] for a in alerts)
    assert any("risk-off" in r for r in alerts[0]["reasons"])


def test_the_vix_is_read_against_its_own_mean_not_a_fixed_level(pf):
    """A VIX that has sat at 30 for a fortnight is the regime, not the event."""
    calm = exits.macro_triggers({}, {"^VIX": {"indicators": {"price": 15.0, "ma20": 14.6}}})
    spike = exits.macro_triggers({}, {"^VIX": {"indicators": {"price": 21.0, "ma20": 14.6}}})
    assert calm == []
    assert spike and spike[0]["kind"] == "macro"


def test_the_calendar_alone_never_raises_an_alert(pf):
    """An event that has not happened yet is a thing to know you hold into, not evidence."""
    _buy(pf)
    cal = {"imminent": [{"title": "CPI y/y", "hours_away": 1.5, "country": "USD"}]}
    assert _review(pf, market=_market(), intel=_intel(), calendar=cal) == []


def test_the_calendar_qualifies_an_alert_that_already_exists(pf):
    _buy(pf)
    cal = {"imminent": [{"title": "CPI y/y", "hours_away": 1.5, "country": "USD"}]}
    alerts = _review(pf, market=_market(), intel=_intel(sentiment=-30, materiality="high"),
                     calendar=cal)
    assert "calendar" in alerts[0]["kinds"]
    assert any("CPI" in r for r in alerts[0]["reasons"])


# ── the chart turned ────────────────────────────────────────────────────────────

def test_a_fully_broken_trend_is_a_trim_not_an_exit(pf):
    """Every chart reading against the position at once still tops out below the EXIT band. That is
    deliberate: indicators disagreeing with a position is the ordinary weather of holding one, and
    an EXIT card that fires on price alone would say nothing the trailing stop is not already
    saying more cheaply."""
    _buy(pf)
    alerts = _review(pf, {"NVDA": 88.0},
                     market=_market(price=88.0, ma20=96.0, macd_d="bearish, hist -0.80",
                                    rsi14_d=41.0, range_pos=0.12),
                     intel=_intel())
    assert alerts[0]["verdict"] == "TRIM"
    assert set(alerts[0]["kinds"]) == {"chart"}


def test_the_chart_and_the_story_agreeing_reach_exit(pf):
    """This is the shape of a real EXIT: two independent families pointing the same way. Neither
    gets there alone, which is the whole reason the weights add rather than trigger."""
    _buy(pf)
    alerts = _review(pf, {"NVDA": 88.0},
                     market=_market(price=88.0, ma20=96.0, macd_d="bearish, hist -0.80",
                                    rsi14_d=41.0, range_pos=0.12),
                     intel=_intel(sentiment=-70, materiality="high"))
    assert alerts[0]["verdict"] == "EXIT"
    assert set(alerts[0]["kinds"]) == {"chart", "news"}


def test_a_short_reads_the_same_indicators_the_other_way(pf):
    _buy(pf, trade_type="SCALP", direction="SHORT")
    up = _market(price=112.0, ma20=104.0, macd_d="bullish, hist +0.90", rsi14_d=59.0,
                 range_pos=0.88)
    alerts = _review(pf, {"NVDA": 112.0}, market=up, intel=_intel())
    assert alerts and alerts[0]["direction"] == "SHORT"
    assert "chart" in alerts[0]["kinds"]
    # And the same tape is silent for the long that actually wants it.
    assert alerts[0]["pnl_pct"] < 0


# ── the move is over ────────────────────────────────────────────────────────────

def test_half_a_real_gain_handed_back_is_flagged(pf):
    pos = _buy(pf)
    pos["high_water"] = 118.0
    alerts = _review(pf, {"NVDA": 106.0}, market=_market(price=106.0), intel=_intel())
    assert alerts[0]["peak_pct"] == 18.0
    assert any("handed back" in r for r in alerts[0]["reasons"])


def test_a_small_pullback_from_a_small_peak_is_not_a_giveback(pf):
    """+3% to +1% is noise. Without the floor the monitor would call every position a fading
    winner the first time it ticked down."""
    pos = _buy(pf)
    pos["high_water"] = 103.0
    alerts = _review(pf, {"NVDA": 101.0}, market=_market(price=101.0), intel=_intel())
    assert alerts == []


def test_the_crypto_book_uses_its_own_giveback_floor(tmp_path):
    """+8% is an ordinary hour on a token. The equity floor would fire on every crypto position
    that ever worked."""
    cpf = Portfolio(path=tmp_path / "PORTFOLIO_CRYPTO.json", rules=config.CRYPTO_RULES)
    cpf.apply_action({"action": "BUY", "ticker": "SOL-USD", "entry": 100.0,
                      "trade_type": "SHORT_TERM", "target_weight_pct": 10.0,
                      "confidence": 70, "reason": "t"}, prices={"SOL-USD": 100.0})
    cpf.state["positions"]["SOL-USD"]["high_water"] = 110.0
    quiet = exits.review(cpf, {"SOL-USD": 104.0}, market={}, now=NOW,
                         store={"version": 1, "alerts": {}})
    cpf.state["positions"]["SOL-USD"]["high_water"] = 120.0
    loud = exits.review(cpf, {"SOL-USD": 108.0}, market={}, now=NOW,
                        store={"version": 1, "alerts": {}})
    assert quiet == []
    assert any("handed back" in r for r in loud[0]["reasons"])


def test_the_first_target_printing_is_worth_saying(pf):
    _buy(pf, target1=112.0)
    alerts = _review(pf, {"NVDA": 113.0}, market=_market(price=113.0),
                     intel=_intel(sentiment=-25, materiality="medium"))
    assert any("first target" in r for r in alerts[0]["reasons"])


def test_the_scalp_clock_is_surfaced_before_it_fires(pf):
    """The time stop closes the position anyway. Saying so half an hour early is the difference
    between choosing the exit and being handed one."""
    pos = _buy(pf, trade_type="SCALP")
    pos["opened"] = (NOW - timedelta(hours=7.5)).isoformat()
    alerts = _review(pf, market=_market(price=97.0, ma20=99.0), intel=_intel())
    assert any("scalp clock" in r for r in alerts[0]["reasons"])


# ── it must not repeat itself ───────────────────────────────────────────────────

def test_the_same_verdict_stays_quiet_until_the_cooldown_lapses(pf, store):
    _buy(pf)
    args = dict(market=_market(price=88.0, ma20=96.0, macd_d="bearish, hist -0.80",
                               rsi14_d=41.0, range_pos=0.12), intel=_intel())
    first = exits.review(pf, {"NVDA": 88.0}, now=NOW, store=store, **args)
    again = exits.review(pf, {"NVDA": 88.0}, now=NOW + timedelta(minutes=30), store=store, **args)
    later = exits.review(pf, {"NVDA": 88.0},
                         now=NOW + timedelta(hours=exits.COOLDOWN_H + 0.1), store=store, **args)
    assert first and not again and later


def test_an_escalation_goes_out_immediately(pf, store):
    """A WATCH becoming an EXIT is new information. Holding it for the cooldown would delay the
    one alert the cooldown exists to make readable."""
    _buy(pf)
    watch = exits.review(pf, {"NVDA": 100.0}, market=_market(price=100.0, ma20=101.0,
                                                             rsi14_d=44.0),
                         intel=_intel(), now=NOW, store=store)
    assert watch[0]["verdict"] == "WATCH"
    worse = exits.review(pf, {"NVDA": 88.0},
                         market=_market(price=88.0, ma20=96.0, macd_d="bearish, hist -0.80",
                                        rsi14_d=41.0, range_pos=0.12),
                         intel=_intel(sentiment=-70, materiality="high"),
                         now=NOW + timedelta(minutes=30), store=store)
    assert worse[0]["verdict"] == "EXIT"


def test_closing_a_name_forgets_it(pf, store):
    """Otherwise the same ticker bought again next month inherits the suppression from the last
    time it was on the book, and its first real alert is silently swallowed."""
    _buy(pf)
    exits.review(pf, {"NVDA": 88.0},
                 market=_market(price=88.0, ma20=96.0, macd_d="bearish, hist -0.80",
                                rsi14_d=41.0, range_pos=0.12),
                 intel=_intel(), now=NOW, store=store)
    assert "NVDA" in store["alerts"]
    pf.apply_action({"action": "SELL", "ticker": "NVDA"}, prices={"NVDA": 88.0})
    exits.review(pf, {}, market={}, intel=None, now=NOW, store=store)
    assert store["alerts"] == {}


# ── what Lind reads ─────────────────────────────────────────────────────────────

_ALERT = {"ticker": "NVDA", "verdict": "EXIT", "score": 5, "direction": "LONG",
          "trade_type": "SCALP", "entry": 100.0, "last": 88.0, "pnl_pct": -12.0,
          "peak_pct": 4.0, "stop_level": 86.0, "target1": 112.0, "kinds": ["chart", "news"],
          "reasons": ["MACD bearish, hist -0.80", "regulatory news against the position"]}


def test_the_card_says_nothing_was_closed():
    html = notify._exit_alerts_card([_ALERT])
    assert "CONSIDER CLOSING" in html and "NVDA" in html
    assert "Nothing here was closed" in html


def test_the_alert_reaches_both_renderings_of_the_email():
    subject, text, html = notify.build_email({"market_outlook": "", "signals": []}, {}, "cycle",
                                             datetime(2026, 7, 30, 11, 0, tzinfo=config.ET),
                                             exit_alerts=[_ALERT])
    assert "EXIT NVDA" in subject and subject.startswith("🚨")
    assert "EXIT MONITOR" in text and "MACD bearish" in text
    assert "Exit monitor" in " ".join(html.split())


def test_an_exit_warning_outranks_a_new_idea_in_the_subject():
    sig = {"ticker": "AMD", "direction": "LONG", "confidence": 70, "trade_type": "SCALP",
           "why": "x", "entry": 1.0}
    subject, _, _ = notify.build_email({"market_outlook": "", "signals": [sig]}, {}, "cycle",
                                       datetime(2026, 7, 30, 11, 0, tzinfo=config.ET),
                                       exit_alerts=[_ALERT])
    assert subject.startswith("🚨") and "AMD" in subject


def test_an_email_with_no_alerts_is_unchanged():
    subject, text, html = notify.build_email({"market_outlook": "quiet", "signals": []}, {},
                                             "cycle",
                                             datetime(2026, 7, 30, 11, 0, tzinfo=config.ET))
    assert "🚨" not in subject and "EXIT MONITOR" not in text
    assert "Exit monitor" not in html


def test_a_quiet_cycle_with_an_alert_still_sends(capsys):
    """The cycle that most needs this is the one with nothing else in it — no signals, no stop
    fired, and on the crypto off-hours pass no analysis at all. Before this branch existed that
    run printed "quiet" and told Lind nothing."""
    args = SimpleNamespace(dry_run=True)
    market_brain._emit(None, {}, {}, "cycle", datetime(2026, 7, 30, 11, 0, tzinfo=config.ET),
                       False, [], [], args, exit_alerts=[_ALERT])
    out = capsys.readouterr().out
    assert "would email" in out and "EXIT NVDA" in out
    assert "Exit monitor flagged an open position" in out


def test_a_genuinely_quiet_cycle_is_still_silent(capsys):
    args = SimpleNamespace(dry_run=True)
    market_brain._emit(None, {}, {}, "cycle", datetime(2026, 7, 30, 11, 0, tzinfo=config.ET),
                       False, [], [], args)
    out = capsys.readouterr().out
    assert "would email" not in out and "quiet" in out


def test_the_monitor_can_never_take_a_cycle_down(capsys):
    """It is an advisory bolted onto a run that has already done the expensive work. A bug in it
    must cost the alert, never the cycle."""
    class Exploding:
        @property
        def state(self):
            raise RuntimeError("boom")

    assert market_brain.watch_open_trades(Exploding(), {}, {}, None, None, persist=False) == []
    assert "exit monitor" in capsys.readouterr().err


def test_the_run_log_line_names_the_worst_first():
    line = exits.summarize([_ALERT, dict(_ALERT, ticker="AMD", verdict="WATCH")])
    assert line.startswith("1 EXIT/1 WATCH") and "NVDA EXIT" in line
    assert exits.summarize([]) == "no exit alerts"
