"""Tests for the paper-broker mirror (brain/broker.py) and the trade journal (brain/journal.py).

Two things are being protected here:

1. **The paper boundary.** broker.py must be structurally incapable of sending an order to the
   live Alpaca host. These tests point the config at the live host and assert nothing is sent.
2. **The accounting stays independent of the broker.** A broker failure must never corrupt the
   simulated ledger — the JSON portfolio is the source of truth, the mirror is best-effort.

Run:  python -m pytest tests/ -q
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from brain import broker, config, journal
from brain.portfolio import Portfolio


class _Resp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload if payload is not None else {"id": "ord-1", "status": "accepted"}
        self.text = "{}"

    def json(self):
        return self._payload


@pytest.fixture
def paper_keys(monkeypatch):
    """Fake credentials so enabled() is True without touching the real .env."""
    monkeypatch.setattr(config, "ALPACA_KEY_ID", "PKTEST", raising=False)
    monkeypatch.setattr(config, "ALPACA_SECRET", "sectest", raising=False)
    monkeypatch.setattr(broker, "_health_cache", None, raising=False)


# ── the paper boundary ──────────────────────────────────────────────────────────

def test_guard_rejects_live_host(monkeypatch, paper_keys):
    monkeypatch.setattr(config, "ALPACA_PAPER_BASE", "https://api.alpaca.markets", raising=False)
    with pytest.raises(broker.BrokerError) as e:
        broker._guard()
    assert "not the Alpaca paper host" in str(e.value)


def test_submit_to_live_host_sends_nothing(monkeypatch, paper_keys):
    """The guard runs before the HTTP call — a live host must not produce a request at all."""
    monkeypatch.setattr(config, "ALPACA_PAPER_BASE", "https://api.alpaca.markets", raising=False)

    def _boom(*a, **kw):
        raise AssertionError("an HTTP request was made to a non-paper host")

    monkeypatch.setattr(broker.requests, "request", _boom)
    res = broker.submit("NVDA", 10, side="buy")
    assert res["mirrored"] is False
    assert "not the Alpaca paper host" in res["reason"]


def test_close_to_live_host_sends_nothing(monkeypatch, paper_keys):
    monkeypatch.setattr(config, "ALPACA_PAPER_BASE", "https://api.alpaca.markets", raising=False)
    monkeypatch.setattr(broker.requests, "request",
                        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("request sent")))
    res = broker.close("NVDA")
    assert res["mirrored"] is False


def test_submit_targets_the_paper_host(monkeypatch, paper_keys):
    seen = {}

    def _fake(method, url, **kw):
        seen["method"], seen["url"], seen["json"] = method, url, kw.get("json")
        return _Resp()

    monkeypatch.setattr(broker.requests, "request", _fake)
    res = broker.submit("NVDA", 3, side="buy")
    assert res["mirrored"] is True
    assert seen["url"].startswith("https://paper-api.alpaca.markets/v2/orders")
    assert "//api.alpaca.markets" not in seen["url"]
    assert seen["json"]["symbol"] == "NVDA" and seen["json"]["side"] == "buy"


def test_fractional_rejection_retries_whole_shares(monkeypatch, paper_keys):
    """Alpaca only fills fractional inside RTH; the anchors run outside it."""
    calls = []

    def _fake(method, url, **kw):
        calls.append(kw.get("json", {}).get("qty"))
        if len(calls) == 1:
            return _Resp(status=422, payload={})
        return _Resp()

    monkeypatch.setattr(broker.requests, "request", _fake)
    res = broker.submit("AAPL", 4.37, side="buy")
    assert res["mirrored"] is True
    assert res["qty"] == 4
    assert calls == ["4.37", "4"]


# ── crypto reaches the paper account too ────────────────────────────────────────

@pytest.fixture
def crypto_listed(monkeypatch):
    """Pretend the asset lookup already ran. SUI is absent on Alpaca — that is not a test fixture
    convenience, it is what /v2/assets returns."""
    monkeypatch.setattr(broker, "_crypto_assets",
                        {"BTC/USD", "ETH/USD", "SOL/USD"}, raising=False)


def test_yahoo_crypto_names_become_alpaca_pairs():
    assert broker._alpaca_symbol("BTC-USD") == "BTC/USD"
    assert broker._alpaca_symbol("ETH-USD") == "ETH/USD"
    # The CoinMarketCap id Yahoo wedges in when the plain ticker is taken must not survive.
    assert broker._alpaca_symbol("SUI20947-USD") == "SUI/USD"


def test_equity_symbols_pass_through_untouched():
    for t in ("NVDA", "AAPL", "BRK-B"):
        assert broker._alpaca_symbol(t) == t


def test_crypto_order_uses_the_slash_pair_and_gtc(monkeypatch, paper_keys, crypto_listed):
    seen = {}

    def _fake(method, url, **kw):
        seen.update(kw.get("json") or {})
        return _Resp()

    monkeypatch.setattr(broker.requests, "request", _fake)
    res = broker.submit("BTC-USD", 0.0154, side="buy")
    assert res["mirrored"] is True
    assert seen["symbol"] == "BTC/USD"
    # `day` is rejected outright on a venue with no close.
    assert seen["time_in_force"] == "gtc"
    assert seen["qty"] == "0.0154"


def test_equity_order_still_uses_day(monkeypatch, paper_keys):
    seen = {}
    monkeypatch.setattr(broker.requests, "request",
                        lambda m, u, **kw: (seen.update(kw.get("json") or {}), _Resp())[1])
    broker.submit("NVDA", 3, side="buy")
    assert seen["time_in_force"] == "day"


def test_a_rejected_crypto_order_is_not_retried_whole(monkeypatch, paper_keys, crypto_listed):
    """0.53 ETH rounded down is no position at all. The RTH retry belongs to equities only."""
    calls = []

    def _fake(method, url, **kw):
        calls.append((kw.get("json") or {}).get("qty"))
        return _Resp(status=422, payload={})

    monkeypatch.setattr(broker.requests, "request", _fake)
    res = broker.submit("ETH-USD", 0.5295, side="buy")
    assert res["mirrored"] is False
    assert calls == ["0.5295"]


def test_an_unlisted_token_never_leaves_the_simulator(monkeypatch, paper_keys, crypto_listed):
    """Alpaca does not list SUI. Firing a doomed order every cycle reads like a broken mirror."""
    monkeypatch.setattr(broker.requests, "request",
                        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("order sent")))
    res = broker.submit("SUI20947-USD", 120.0, side="buy")
    assert res["mirrored"] is False
    assert "not tradable on Alpaca" in res["reason"]


def test_closing_a_crypto_position_encodes_the_slash(monkeypatch, paper_keys):
    seen = {}
    monkeypatch.setattr(broker.requests, "request",
                        lambda m, u, **kw: (seen.update(url=u), _Resp())[1])
    broker.close("BTC-USD")
    # An un-encoded slash would address /v2/positions/BTC/USD, which is a different route.
    assert seen["url"].endswith("/v2/positions/BTC%2FUSD")


def test_crypto_orders_still_cannot_reach_the_live_host(monkeypatch, paper_keys, crypto_listed):
    monkeypatch.setattr(config, "ALPACA_PAPER_BASE", "https://api.alpaca.markets", raising=False)
    monkeypatch.setattr(broker.requests, "request",
                        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("request sent")))
    res = broker.submit("BTC-USD", 0.01, side="buy")
    assert res["mirrored"] is False
    assert "not the Alpaca paper host" in res["reason"]


def test_no_keys_is_a_clean_noop(monkeypatch):
    monkeypatch.setattr(config, "ALPACA_KEY_ID", "", raising=False)
    monkeypatch.setattr(config, "ALPACA_SECRET", "", raising=False)
    monkeypatch.setattr(broker.requests, "request",
                        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("request sent")))
    assert broker.enabled() is False
    assert broker.submit("NVDA", 5)["mirrored"] is False
    assert broker.close("NVDA")["mirrored"] is False


# ── the ledger survives the broker ──────────────────────────────────────────────

def _pf(tmp_path, mirror=False):
    return Portfolio(path=tmp_path / "PORTFOLIO.json", mirror=mirror)


def _buy(pf, ticker, entry, **extra):
    action = {"action": "BUY", "ticker": ticker, "entry": entry,
              "target_weight_pct": 10.0, "reason": "test"}
    action.update(extra)
    return pf.apply_action(action, prices={ticker: entry})


def test_mirror_off_records_no_broker_events(tmp_path):
    pf = _pf(tmp_path, mirror=False)
    assert _buy(pf, "NVDA", 100)[0]
    assert pf.broker_events == []
    assert pf.state["positions"]["NVDA"].get("broker") is None


def test_mirror_on_records_the_fill(tmp_path, monkeypatch):
    monkeypatch.setattr(broker, "enabled", lambda: True)
    monkeypatch.setattr(broker, "submit",
                        lambda s, q, side="buy": {"mirrored": True, "order_id": "o1",
                                                  "qty": q, "status": "accepted"})
    monkeypatch.setattr(broker, "close", lambda s: {"mirrored": True, "order_id": "o2"})
    pf = _pf(tmp_path, mirror=True)
    assert _buy(pf, "NVDA", 100)[0]
    assert pf.state["positions"]["NVDA"]["broker"]["mirrored"] is True

    exits = pf.mark_to_market({"NVDA": 90})          # -10% -> hard stop
    assert exits[0]["broker"]["order_id"] == "o2"
    assert [e["kind"] for e in pf.broker_events] == ["buy", "close"]


def test_broker_failure_does_not_break_the_ledger(tmp_path, monkeypatch):
    """The simulator is the source of truth — a dead broker must not lose the position."""
    monkeypatch.setattr(broker, "enabled", lambda: True)

    def _explode(*a, **kw):
        raise RuntimeError("alpaca down")

    monkeypatch.setattr(broker, "submit", _explode)
    pf = _pf(tmp_path, mirror=True)
    ok, msg = _buy(pf, "AMD", 100)
    assert ok, msg
    assert "AMD" in pf.state["positions"]
    assert pf.broker_events[0]["mirrored"] is False
    assert "alpaca down" in pf.broker_events[0]["reason"]


def test_entry_reasoning_survives_to_the_exit_record(tmp_path):
    """The journal grades the exit against the entry thesis, so the exit must carry it."""
    pf = _pf(tmp_path)
    ok, _ = _buy(pf, "TSLA", 100, trade_type="SCALP", confidence=82,
                 indicators_used=["RSI", "VWAP"], reason="vwap reclaim on volume")
    assert ok
    exits = pf.mark_to_market({"TSLA": 92})
    e = exits[0]
    assert e["trade_type"] == "SCALP"
    assert e["confidence"] == 82
    assert e["thesis"] == "vwap reclaim on volume"
    rec = pf.state["closed_trades"][0]
    assert rec["indicators_used"] == ["RSI", "VWAP"]


# ── journal grading (deterministic, no model) ───────────────────────────────────

def _exit_rec(**kw):
    opened = datetime.now(config.UTC) - timedelta(days=kw.pop("held_days", 1))
    rec = {"ticker": "NVDA", "entry": 100, "exit": 110, "pnl_pct": 10.0, "pnl_usd": 100.0,
           "reason": "thesis played out", "trade_type": "SHORT_TERM", "confidence": 60,
           "opened": opened.isoformat(), "closed": datetime.now(config.UTC).isoformat()}
    rec.update(kw)
    return rec


def test_grade_flags_scalp_held_for_weeks():
    verdict, held, notes = journal._grade(_exit_rec(trade_type="SCALP", held_days=9))
    assert verdict == "WIN"
    assert held == pytest.approx(9, abs=0.05)
    assert any("longer than a SCALP" in n for n in notes)


def test_grade_flags_high_conviction_loss():
    _, _, notes = journal._grade(_exit_rec(confidence=85, pnl_pct=-6.2, exit=93.8,
                                           reason="hard stop hit (-7.2% <= -7%)"))
    assert any("High-conviction loss" in n for n in notes)
    assert any("mechanical" in n for n in notes)


def test_grade_flags_low_conviction_win():
    _, _, notes = journal._grade(_exit_rec(confidence=40, pnl_pct=12.0))
    assert any("Low-conviction win" in n for n in notes)


def test_grade_verdicts():
    assert journal._grade(_exit_rec(pnl_pct=5.0))[0] == "WIN"
    assert journal._grade(_exit_rec(pnl_pct=-5.0))[0] == "LOSS"
    assert journal._grade(_exit_rec(pnl_pct=0.0))[0] == "FLAT"


# ── journal notes round-trip ────────────────────────────────────────────────────

@pytest.fixture
def vault(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LIND_BRAIN", str(tmp_path / "vault"), raising=False)
    return tmp_path / "vault"


def test_open_then_close_updates_the_same_note(vault, tmp_path):
    pf = _pf(tmp_path)
    assert _buy(pf, "NVDA", 100, trade_type="SHORT_TERM", confidence=72)[0]
    pos = pf.state["positions"]["NVDA"]
    signal = {"trade_type": "SHORT_TERM", "confidence": 72, "why": "20-day breakout",
              "confidence_rationale": "72 because volume confirmed; capped by earnings in 4d",
              "indicators_used": ["RSI", "MACD", "volume-vs-avg"],
              "news_read": "Haiku: upgrade, high materiality", "direction": "LONG"}
    path = journal.open_trade("NVDA", pos, signal=signal)
    assert path is not None
    text = Path(path).read_text(encoding="utf-8")
    assert "status: OPEN" in text
    assert "72 because volume confirmed" in text
    assert "RSI, MACD, volume-vs-avg" in text
    assert "Haiku: upgrade, high materiality" in text

    exits = pf.mark_to_market({"NVDA": 90})
    closed = journal.close_trade(exits[0])
    assert closed == path                      # same note, not a second one
    text = Path(path).read_text(encoding="utf-8")
    assert "status: CLOSED" in text
    assert "status: OPEN" not in text
    assert "outcome: LOSS" in text
    assert "What this trade teaches" in text


def test_close_without_entry_note_writes_a_stub(vault):
    rec = _exit_rec(pnl_pct=-3.0, ticker="AMD")
    path = journal.close_trade(rec)
    assert path is not None
    text = Path(path).read_text(encoding="utf-8")
    assert "entry note not found" in text
    assert "status: CLOSED" in text


def test_no_vault_configured_is_a_noop(monkeypatch):
    monkeypatch.setattr(config, "LIND_BRAIN", "", raising=False)
    assert journal.open_trade("NVDA", {"entry": 1, "shares": 1, "opened": None}) is None
    assert journal.close_trade(_exit_rec()) is None


# ── weekly recap arithmetic ─────────────────────────────────────────────────────

def test_build_recap_counts_only_this_week(tmp_path):
    pf = _pf(tmp_path)
    assert _buy(pf, "NVDA", 100, trade_type="SHORT_TERM", confidence=80)[0]
    assert _buy(pf, "AAPL", 50)[0]
    pf.mark_to_market({"NVDA": 90, "AAPL": 50})     # NVDA hard-stops out, AAPL holds

    # An old trade that must NOT count toward this week.
    stale = _exit_rec(ticker="OLD", pnl_usd=999.0, pnl_pct=40.0)
    stale["closed"] = (datetime.now(config.UTC) - timedelta(days=30)).isoformat()
    pf.state["closed_trades"].append(stale)

    week_start = (datetime.now(config.UTC) - timedelta(days=7)).isoformat()
    recap = journal.build_recap(pf, week_start, prices={"AAPL": 55})
    st = recap["stats"]
    assert st["n_closed"] == 1
    assert st["win_rate"] == 0.0
    assert st["realized_usd"] < 0
    assert [c["ticker"] for c in recap["closed"]] == ["NVDA"]
    assert recap["gradings"][0]["verdict"] == "LOSS"
    assert [p["ticker"] for p in recap["open"]] == ["AAPL"]


def test_recap_packet_is_json(tmp_path):
    import json as _json
    pf = _pf(tmp_path)
    week_start = (datetime.now(config.UTC) - timedelta(days=7)).isoformat()
    packet = _json.loads(journal.recap_packet(journal.build_recap(pf, week_start)))
    assert set(packet) == {"stats", "closed_trades", "still_open", "rule_based_gradings",
                           "measured_performance", "week_ahead"}


def test_a_crypto_buy_mirrors_end_to_end(monkeypatch, paper_keys, crypto_listed, tmp_path):
    """The whole path: gate approves, ledger opens, Alpaca gets a slash pair at a gtc TIF.

    The crypto book ran with mirror=False until 2026-07-28 because the symbol spelling and the
    unlisted-SUI problem made a half-working mirror worse than none. Both are handled now, so
    this asserts the path is actually joined up rather than merely capable.
    """
    sent = []
    monkeypatch.setattr(broker.requests, "request",
                        lambda m, u, **kw: (sent.append(kw.get("json") or {}), _Resp())[1])
    pf = Portfolio(path=tmp_path / "PORTFOLIO_CRYPTO.json", mirror=True,
                   rules=config.CRYPTO_RULES)
    ok, msg = pf.apply_action(
        {"action": "BUY", "ticker": "BTC-USD", "entry": 65_000.0, "target_weight_pct": 10},
        prices={"BTC-USD": 65_000.0})
    assert ok, msg
    assert sent and sent[0]["symbol"] == "BTC/USD" and sent[0]["time_in_force"] == "gtc"
    assert pf.broker_events[0]["mirrored"] is True
    # The ledger is still the accounting truth, in our spelling, not Alpaca's.
    assert "BTC-USD" in pf.state["positions"]


def test_an_unlisted_token_still_opens_in_the_ledger(monkeypatch, paper_keys, crypto_listed,
                                                     tmp_path):
    """SUI is not on Alpaca. The sim must still take the trade — the mirror is best-effort."""
    monkeypatch.setattr(broker.requests, "request",
                        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("order sent")))
    pf = Portfolio(path=tmp_path / "PORTFOLIO_CRYPTO.json", mirror=True,
                   rules=config.CRYPTO_RULES)
    ok, _msg = pf.apply_action(
        {"action": "BUY", "ticker": "SUI20947-USD", "entry": 3.10, "target_weight_pct": 10},
        prices={"SUI20947-USD": 3.10})
    assert ok
    assert "SUI20947-USD" in pf.state["positions"]
    assert pf.broker_events[0]["mirrored"] is False
    assert "not tradable" in pf.broker_events[0]["reason"]
