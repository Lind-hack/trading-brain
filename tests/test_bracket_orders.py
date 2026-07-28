"""Bracket entries and broker reconciliation.

Before this, an emailed signal and the paper account agreed on nothing. The email quoted an entry,
a stop and two targets; Alpaca received a bare market order carrying none of them. The stop lived
only in the simulator, enforced by a poll that runs every 30 minutes on equities and hourly on
crypto — so between polls, and across every overnight gap, the position was unprotected.

Two things are pinned here:

1. **The account holds the plan.** An equity buy leaves as a limit at the quoted entry with the
   stop and target attached as bracket legs. Where Alpaca will not take one — crypto, sub-share
   sizes, a stop the wrong side of the entry — it degrades to the old market order and *says which*
   rather than quietly looking the same.
2. **A limit is a request, not a fill.** The ledger may no longer assume the trade happened. An
   entry the broker never filled is backed out: cash returned, pace counter credited back, logged
   to `unfilled` and deliberately kept out of `closed_trades`, where it would corrupt every
   win-rate and exit-mix number the recap computes.

Run:  python -m pytest tests/test_bracket_orders.py -q
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from brain import broker, config
from brain.portfolio import Portfolio


class _Resp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload if payload is not None else {"id": "ord-1", "status": "accepted"}
        self.text = json.dumps(self._payload)

    def json(self):
        return self._payload


@pytest.fixture
def paper_keys(monkeypatch):
    monkeypatch.setattr(config, "ALPACA_KEY_ID", "PKTEST", raising=False)
    monkeypatch.setattr(config, "ALPACA_SECRET", "sectest", raising=False)
    monkeypatch.setattr(broker, "_health_cache", None, raising=False)


@pytest.fixture
def crypto_listed(monkeypatch):
    monkeypatch.setattr(broker, "_crypto_assets", {"BTC/USD", "ETH/USD", "SOL/USD"}, raising=False)


def _capture(monkeypatch, payload=None, fail_post=None):
    """Record every request broker.py makes. Returns the list of (method, url, body)."""
    calls = []

    def _fake(method, url, **kw):
        body = kw.get("json")
        calls.append((method, url, body))
        if fail_post and method == "POST" and body and body.get("order_class"):
            return _Resp(422, {"message": fail_post})
        if method == "GET" and "/v2/orders?" in url:
            return _Resp(200, payload if payload is not None else [])
        return _Resp(200, {"id": "ord-1", "status": "accepted"})

    monkeypatch.setattr(broker.requests, "request", _fake)
    return calls


def _order_body(calls):
    for method, url, body in calls:
        if method == "POST" and url.endswith("/v2/orders"):
            return body
    return None


# ── what actually leaves for Alpaca ─────────────────────────────────────────────

def test_an_equity_buy_carries_the_entry_stop_and_target(monkeypatch, paper_keys):
    """The whole point: the three numbers in the email are the three numbers in the account."""
    calls = _capture(monkeypatch)
    res = broker.submit("NVDA", 10, side="buy", entry=180.25, stop=170.0, target=200.0)
    b = _order_body(calls)
    assert b["type"] == "limit" and b["limit_price"] == "180.25"
    assert b["order_class"] == "bracket"
    assert b["stop_loss"] == {"stop_price": "170.0"}
    assert b["take_profit"] == {"limit_price": "200.0"}
    assert b["time_in_force"] == "day"          # bracket takes day or gtc; never `ioc`
    assert res["protected"] is True and res["pending"] is True


def test_a_stop_with_no_target_still_goes_out_as_an_oto():
    """Half a bracket is the half that matters. The target is an optimisation; the stop is the
    reason any of this exists, and a missing target must not cost us one."""
    legs = broker._protection("buy", entry=100.0, stop=90.0, target=None)
    assert legs["order_class"] == "oto"
    assert legs["stop_loss"] == {"stop_price": "90.0"}
    assert "take_profit" not in legs


def test_a_target_below_the_entry_is_dropped_rather_than_sent():
    """Alpaca rejects a buy bracket whose take-profit is under the stop, and the whole order goes
    with it. Better an OTO that protects than a bracket that 422s."""
    legs = broker._protection("buy", entry=100.0, stop=90.0, target=95.0)
    assert legs["order_class"] == "oto"


def test_a_stop_above_the_entry_is_not_a_stop():
    assert broker._protection("buy", entry=100.0, stop=110.0, target=120.0) is None
    assert broker._protection("buy", entry=100.0, stop=0, target=120.0) is None


def test_a_naked_order_says_why_it_is_naked(monkeypatch, paper_keys):
    calls = _capture(monkeypatch)
    res = broker.submit("NVDA", 10, side="buy", entry=100.0, stop=110.0)
    assert _order_body(calls)["type"] == "market"
    assert res["protected"] is False
    assert "no usable stop" in res["unprotected_because"]


# ── crypto cannot take one, and must not pretend otherwise ──────────────────────

def test_crypto_never_gets_a_bracket(monkeypatch, paper_keys, crypto_listed):
    """Alpaca supports no advanced order class on any crypto pair. Sending one is a 422, and
    quietly sending a market order without saying so is how the stop went missing in the first
    place — the token's stop stays in the simulator's poll and the mirror admits it."""
    calls = _capture(monkeypatch)
    res = broker.submit("BTC-USD", 0.5, side="buy", entry=60000.0, stop=51000.0, target=70000.0)
    b = _order_body(calls)
    assert b["type"] == "market" and "order_class" not in b
    assert b["time_in_force"] == "gtc"
    assert res["protected"] is False
    assert "crypto" in res["unprotected_because"]


# ── the fractional-share collision ──────────────────────────────────────────────

def test_a_fractional_quantity_floors_so_the_bracket_survives(monkeypatch, paper_keys):
    """Alpaca refuses fractional bracket orders outright. A protected 10 shares beats an
    unprotected 10.62, and the shortfall is reported rather than swallowed."""
    calls = _capture(monkeypatch)
    res = broker.submit("NVDA", 10.62, side="buy", entry=180.0, stop=170.0, target=200.0)
    assert _order_body(calls)["qty"] == "10"
    assert res["qty"] == 10 and res["protected"] is True
    assert "whole-share" in res["note"]


def test_under_one_share_there_is_nothing_to_floor_to(monkeypatch, paper_keys):
    calls = _capture(monkeypatch)
    res = broker.submit("NVDA", 0.4, side="buy", entry=180.0, stop=170.0, target=200.0)
    assert _order_body(calls)["type"] == "market"
    assert res["protected"] is False
    assert "under one share" in res["unprotected_because"]


def test_a_rejected_bracket_still_gets_the_trade_on(monkeypatch, paper_keys):
    """A bracket Alpaca will not take must not cost the mirror entirely — fall back to the plain
    order, unprotected, and name the rejection."""
    calls = _capture(monkeypatch, fail_post="stop price must be below base price")
    res = broker.submit("NVDA", 10, side="buy", entry=180.0, stop=170.0, target=200.0)
    assert res["mirrored"] is True and res["protected"] is False
    assert "rejected the bracket" in res["unprotected_because"]
    assert [b["type"] for _, _, b in calls if b][-1] == "market"


# ── exits have to retire the legs ───────────────────────────────────────────────

def test_closing_cancels_the_resting_legs_first(monkeypatch, paper_keys):
    """A take-profit still resting against a position that no longer exists opens a short in the
    other direction. Cancel, then flatten — and in that order."""
    calls = _capture(monkeypatch, payload=[{"id": "leg-1", "symbol": "NVDA"},
                                           {"id": "leg-2", "symbol": "AAPL"}])
    res = broker.close("NVDA")
    verbs = [(m, u.rsplit("/", 1)[-1]) for m, u, _ in calls]
    assert ("DELETE", "leg-1") in verbs
    assert ("DELETE", "leg-2") not in verbs, "cancelled another symbol's order"
    assert verbs[-1] == ("DELETE", "NVDA"), "flattened before cancelling"
    assert res["cancelled_orders"] == 1


# ── reconciliation: the ledger stops assuming ───────────────────────────────────

def _pf(tmp_path, mirror=True):
    return Portfolio(path=tmp_path / "PORTFOLIO.json", mirror=mirror)


def _open(pf, ticker="NVDA", price=100.0, **broker_extra):
    """Put a position on the book with a pending broker order attached."""
    mirror = {"mirrored": True, "order_id": "o1", "pending": True, "protected": True}
    mirror.update(broker_extra)
    ok, _ = pf.apply_action({"action": "BUY", "ticker": ticker, "entry": price,
                             "target_weight_pct": 10.0, "reason": "t"}, prices={ticker: price})
    assert ok
    pf.state["positions"][ticker]["broker"] = mirror
    return pf.state["positions"][ticker]


@pytest.fixture
def no_broker(monkeypatch):
    """`mirror=True` without a network: submit/close no-op, `order` is set per test."""
    monkeypatch.setattr(broker, "enabled", lambda: True)
    monkeypatch.setattr(broker, "submit", lambda s, q, side="buy", **kw: None)
    monkeypatch.setattr(broker, "close", lambda s: {"mirrored": True})


def test_a_filled_order_records_the_real_price_without_rewriting_the_ledger(
        tmp_path, monkeypatch, no_broker):
    """The simulator keeps booking the quoted entry on purpose — it is grading the analyst's plan,
    and a plan graded at a price the analyst did not pick grades the venue instead. The gap is
    recorded as slippage, not applied."""
    pf = _pf(tmp_path)
    _open(pf, "NVDA", 100.0)
    monkeypatch.setattr(broker, "order", lambda oid, nested=True: {
        "status": "filled", "filled_qty": 10, "filled_avg_price": 100.40})
    out = pf.reconcile_broker()
    assert out[0]["outcome"] == "filled"
    assert pf.state["positions"]["NVDA"]["entry"] == 100.0
    assert pf.state["positions"]["NVDA"]["broker"]["fill_price"] == 100.40
    assert pf.state["positions"]["NVDA"]["broker"]["slippage_pct"] == 0.4
    assert pf.state["positions"]["NVDA"]["broker"]["pending"] is False


def test_a_working_order_is_left_alone(tmp_path, monkeypatch, no_broker):
    """A day limit at a pullback is allowed to sit there until the bell without anyone panicking."""
    pf = _pf(tmp_path)
    _open(pf, "NVDA", 100.0)
    monkeypatch.setattr(broker, "order", lambda oid, nested=True: {
        "status": "new", "filled_qty": 0, "filled_avg_price": None})
    assert pf.reconcile_broker() == []
    assert "NVDA" in pf.state["positions"]
    assert pf.state["positions"]["NVDA"]["broker"]["pending"] is True


def test_an_expired_entry_is_backed_out_of_the_ledger(tmp_path, monkeypatch, no_broker):
    """The failure this whole pass exists for: a limit that never traded, leaving the simulator
    holding a position that exists in no account anywhere."""
    pf = _pf(tmp_path)
    cash_before = pf.state["cash"]
    _open(pf, "NVDA", 100.0)
    assert pf.state["cash"] < cash_before
    monkeypatch.setattr(broker, "order", lambda oid, nested=True: {
        "status": "expired", "filled_qty": 0, "filled_avg_price": None})

    out = pf.reconcile_broker()
    assert out[0]["outcome"] == "never_filled"
    assert "NVDA" not in pf.state["positions"]
    assert pf.state["cash"] == pytest.approx(cash_before)
    assert pf.state["unfilled"][-1]["ticker"] == "NVDA"
    assert "never filled" in pf.state["unfilled"][-1]["reason"]


def test_an_unfilled_entry_is_not_a_closed_trade(tmp_path, monkeypatch, no_broker):
    """Filing it as a 0.0% close would put a phantom row into every win-rate, exit-mix and average
    the weekly recap computes. It is not a losing trade; it is not a trade."""
    pf = _pf(tmp_path)
    _open(pf, "NVDA", 100.0)
    monkeypatch.setattr(broker, "order", lambda oid, nested=True: {
        "status": "canceled", "filled_qty": 0, "filled_avg_price": None})
    pf.reconcile_broker()
    assert pf.state["closed_trades"] == []
    assert pf.state["sector_fails"] == {}, "a trade that never happened is not a sector loss"


def test_the_pace_counter_is_credited_back(tmp_path, monkeypatch, no_broker):
    """`week_trades` drives the pace target the prompts read. An order that never filled must not
    spend the week's budget."""
    pf = _pf(tmp_path)
    _open(pf, "NVDA", 100.0)
    assert pf.new_trades_this_week() == 1
    monkeypatch.setattr(broker, "order", lambda oid, nested=True: {
        "status": "expired", "filled_qty": 0, "filled_avg_price": None})
    pf.reconcile_broker()
    assert pf.new_trades_this_week() == 0


def test_an_unreadable_order_leaves_everything_pending(tmp_path, monkeypatch, no_broker):
    """A broker we cannot reach is not evidence the order failed. Fail in the direction that keeps
    the position — the next cycle asks again."""
    pf = _pf(tmp_path)
    _open(pf, "NVDA", 100.0)
    monkeypatch.setattr(broker, "order", lambda oid, nested=True: None)
    assert pf.reconcile_broker() == []
    assert "NVDA" in pf.state["positions"]


def test_a_partial_fill_keeps_the_position(tmp_path, monkeypatch, no_broker):
    pf = _pf(tmp_path)
    _open(pf, "NVDA", 100.0)
    monkeypatch.setattr(broker, "order", lambda oid, nested=True: {
        "status": "partially_filled", "filled_qty": 4, "filled_avg_price": 99.9})
    out = pf.reconcile_broker()
    assert out[0]["outcome"] == "filled"
    assert pf.state["positions"]["NVDA"]["broker"]["partial"] is True
    assert "NVDA" in pf.state["positions"]


def test_an_unfilled_add_backs_out_only_its_own_shares(tmp_path, monkeypatch, no_broker):
    """An ADD carries its own order. Unwinding the whole position underneath it would delete a
    holding the broker really is carrying."""
    pf = _pf(tmp_path)
    _open(pf, "NVDA", 100.0)
    pf.state["positions"]["NVDA"]["broker"]["pending"] = False       # the open filled
    held = pf.state["positions"]["NVDA"]["shares"]
    # 15%, not 5% — an ADD sizes to the *gap* between the target weight and what is already held,
    # so a target under the current weight is correctly refused as nothing left to buy.
    ok, _ = pf.apply_action({"action": "ADD", "ticker": "NVDA", "entry": 110.0,
                             "target_weight_pct": 15.0, "reason": "t"}, prices={"NVDA": 110.0})
    assert ok
    pos = pf.state["positions"]["NVDA"]
    pos["adds"][-1]["broker"] = {"mirrored": True, "order_id": "o2", "pending": True}
    assert pos["shares"] > held and pos["entry"] > 100.0

    monkeypatch.setattr(broker, "order", lambda oid, nested=True: {
        "status": "expired", "filled_qty": 0, "filled_avg_price": None})
    out = pf.reconcile_broker()
    assert out[0]["slot"] == "add"
    pos = pf.state["positions"]["NVDA"]
    assert pos["shares"] == pytest.approx(held)
    assert pos["entry"] == pytest.approx(100.0), "the add's cost was not un-averaged"


def test_reconciliation_is_a_no_op_without_a_broker(tmp_path):
    """Every book that runs simulator-only — no keys, --dry-run, the tokens Alpaca cannot fill —
    has to behave exactly as it did before any of this existed."""
    pf = _pf(tmp_path, mirror=False)
    ok, _ = pf.apply_action({"action": "BUY", "ticker": "NVDA", "entry": 100.0,
                             "target_weight_pct": 10.0, "reason": "t"}, prices={"NVDA": 100.0})
    assert ok
    assert pf.reconcile_broker() == []
    assert "NVDA" in pf.state["positions"]


def test_an_old_ledger_without_the_unfilled_key_still_loads(tmp_path):
    """PORTFOLIO.json predates this field by every commit so far."""
    p = tmp_path / "PORTFOLIO.json"
    p.write_text(json.dumps({"cash": 9000, "positions": {}, "closed_trades": []}), encoding="utf-8")
    assert Portfolio(path=p, mirror=False).state["unfilled"] == []


# ── what the email says ─────────────────────────────────────────────────────────

def test_the_applied_line_quotes_the_levels_the_broker_holds(tmp_path, monkeypatch):
    """Lind's question was whether the account holds what the email says. The answer is only
    checkable if the email says which."""
    monkeypatch.setattr(broker, "enabled", lambda: True)
    monkeypatch.setattr(broker, "submit", lambda s, q, side="buy", **kw: {
        "mirrored": True, "order_id": "abcdef1234", "protected": True, "order_class": "bracket",
        "limit_price": 180.25, "stop_price": 170.0, "take_profit": 200.0, "pending": True})
    monkeypatch.setattr(broker, "close", lambda s: {"mirrored": True})
    pf = _pf(tmp_path)
    ok, msg = pf.apply_action({"action": "BUY", "ticker": "NVDA", "entry": 180.25, "stop": 170.0,
                               "target1": 200.0, "target_weight_pct": 10.0, "reason": "t"},
                              prices={"NVDA": 180.25})
    assert ok
    assert "bracket" in msg and "limit 180.25" in msg and "stop 170.00" in msg
    assert "target 200.00" in msg and "pending fill" in msg


def test_an_unprotected_order_is_labelled_as_one(tmp_path, monkeypatch):
    monkeypatch.setattr(broker, "enabled", lambda: True)
    monkeypatch.setattr(broker, "submit", lambda s, q, side="buy", **kw: {
        "mirrored": True, "order_id": "abcdef1234", "protected": False,
        "unprotected_because": "crypto — Alpaca supports no bracket"})
    monkeypatch.setattr(broker, "close", lambda s: {"mirrored": True})
    pf = _pf(tmp_path)
    ok, msg = pf.apply_action({"action": "BUY", "ticker": "NVDA", "entry": 100.0,
                               "target_weight_pct": 10.0, "reason": "t"}, prices={"NVDA": 100.0})
    assert ok and "no stop attached" in msg and "crypto" in msg
