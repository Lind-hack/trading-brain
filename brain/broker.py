"""Alpaca PAPER account adapter — where the brain's own trades actually get placed.

THE BOUNDARY, RESTATED: this module can only ever reach `paper-api.alpaca.markets`. Alpaca
serves paper and live trading from two different hosts, and the host here is a module-level
constant built from config.ALPACA_PAPER_BASE, re-checked on every single request by
`_guard()`. There is no env var, no argument, and no model output that can point it at
`api.alpaca.markets`. If the constant is ever edited to a non-paper host, every call raises
instead of sending. Paper keys are also not interchangeable with live keys, so a
misconfiguration fails closed rather than trading real money.

The internal simulator (portfolio.py) stays the accounting source of truth: it owns the
discipline gates, the equity curve, and the journal. This module mirrors gate-approved
decisions into the Alpaca paper account so the fills, slippage, and market-hours behaviour
are real rather than assumed. When keys are absent everything here no-ops and the brain runs
exactly as it did before.
"""
from __future__ import annotations

import math
import re
import sys
from urllib.parse import quote

import requests

from . import config

_TIMEOUT = 20
_PAPER_HOST = "paper-api.alpaca.markets"


class BrokerError(RuntimeError):
    pass


def _guard():
    """Refuse to talk to anything that is not the Alpaca paper host."""
    base = config.ALPACA_PAPER_BASE or ""
    if _PAPER_HOST not in base:
        raise BrokerError(
            f"refusing to send orders: {base!r} is not the Alpaca paper host ({_PAPER_HOST}). "
            "This brain never places real-money orders.")
    return base.rstrip("/")


def enabled():
    """True when paper keys are configured. Everything degrades to sim-only when False."""
    return bool(config.ALPACA_KEY_ID and config.ALPACA_SECRET)


def _alpaca_symbol(ticker):
    """Alpaca's name for one of our symbols. Equities pass through untouched.

    The two venues do not spell crypto the same way. Yahoo — which is where every price in this
    repo comes from — names spot pairs `BTC-USD`, and disambiguates a token whose plain ticker was
    already taken by wedging its CoinMarketCap id in: `SUI20947-USD`. Alpaca wants `BTC/USD` and
    `SUI/USD`. Sending the Yahoo spelling is a 422, which is why crypto mirroring never worked.
    """
    t = str(ticker or "").upper()
    if not config.is_crypto(t):
        return t
    base = config.CRYPTO_LABELS.get(ticker) or config.CRYPTO_LABELS.get(t)
    if not base:
        base = re.sub(r"\d+$", "", t[:-4])          # drop "-USD", then the disambiguating digits
    return f"{base.upper()}/USD"


_crypto_assets = None


def tradable_crypto():
    """The set of crypto pairs this paper account can actually trade, e.g. {"BTC/USD", ...}.

    Alpaca lists ~36 USD pairs and our universe is not a subset of it — SUI is not there at all.
    Without this check every cycle that proposes SUI fires a doomed order and logs a broker
    warning, which reads like a fault in the mirroring rather than an asset that does not exist.
    Cached per process; an empty set means the lookup failed, and then we let the order try.
    """
    global _crypto_assets
    if _crypto_assets is not None:
        return _crypto_assets
    try:
        rows = _request("GET", "/v2/assets?asset_class=crypto&status=active") or []
        _crypto_assets = {r.get("symbol") for r in rows if r.get("tradable")}
    except Exception as e:
        print(f"[warn] could not list Alpaca crypto assets: {e}", file=sys.stderr)
        _crypto_assets = set()
    return _crypto_assets


def _headers():
    return {
        "APCA-API-KEY-ID": config.ALPACA_KEY_ID,
        "APCA-API-SECRET-KEY": config.ALPACA_SECRET,
        "Content-Type": "application/json",
    }


def _request(method, path, **kw):
    base = _guard()
    if not enabled():
        raise BrokerError("Alpaca paper keys are not configured")
    r = requests.request(method, f"{base}{path}", headers=_headers(), timeout=_TIMEOUT, **kw)
    if r.status_code >= 400:
        raise BrokerError(f"{method} {path} -> {r.status_code}: {r.text[:300]}")
    if r.text.strip():
        return r.json()
    return {}


# ── Read ────────────────────────────────────────────────────────────────────────

def account():
    """Paper account snapshot: equity, cash, buying power, blocked flags."""
    a = _request("GET", "/v2/account")
    return {
        "account_number": a.get("account_number"),
        "status": a.get("status"),
        "equity": float(a.get("equity") or 0),
        "cash": float(a.get("cash") or 0),
        "buying_power": float(a.get("buying_power") or 0),
        "trading_blocked": bool(a.get("trading_blocked")),
        "shorting_enabled": bool(a.get("shorting_enabled")),
    }


def positions():
    """Open paper positions keyed by symbol."""
    out = {}
    for p in _request("GET", "/v2/positions") or []:
        out[p.get("symbol")] = {
            "shares": float(p.get("qty") or 0),
            "entry": float(p.get("avg_entry_price") or 0),
            "last": float(p.get("current_price") or 0),
            "market_value": float(p.get("market_value") or 0),
            "pnl_usd": float(p.get("unrealized_pl") or 0),
            "pnl_pct": round(float(p.get("unrealized_plpc") or 0) * 100, 2),
        }
    return out


def clock():
    """Market clock — tells us whether a fractional market order can fill right now."""
    try:
        c = _request("GET", "/v2/clock")
        return {"is_open": bool(c.get("is_open")), "next_open": c.get("next_open"),
                "next_close": c.get("next_close")}
    except Exception:
        return {"is_open": None, "next_open": None, "next_close": None}


def order(order_id, nested=True):
    """One order by id, with its bracket legs. Returns None if it cannot be read.

    `filled_avg_price` is the only place the *real* fill price exists — the simulator books the
    price the analyst asked for, which is why reconciliation needs this at all.
    """
    if not enabled() or not order_id:
        return None
    try:
        o = _request("GET", f"/v2/orders/{quote(str(order_id), safe='')}"
                            f"{'?nested=true' if nested else ''}")
    except BrokerError as e:
        print(f"[warn] broker order {order_id}: {e}", file=sys.stderr)
        return None
    return {
        "id": o.get("id"),
        "symbol": o.get("symbol"),
        "status": o.get("status"),
        "qty": float(o.get("qty") or 0),
        "filled_qty": float(o.get("filled_qty") or 0),
        "filled_avg_price": float(o.get("filled_avg_price") or 0) or None,
        "limit_price": float(o.get("limit_price") or 0) or None,
        "order_class": o.get("order_class"),
        "legs": [{"id": l.get("id"), "type": l.get("type"), "status": l.get("status"),
                  "limit_price": l.get("limit_price"), "stop_price": l.get("stop_price")}
                 for l in (o.get("legs") or [])],
    }


def cancel_open(symbol):
    """Cancel every working order on a symbol. Returns how many were cancelled.

    Flattening a position does **not** retire the bracket legs still resting against it. Leave
    them and a filled take-profit or stop on a position that no longer exists opens a short in
    the other direction — the one way this module could hold something nobody decided to hold.
    So every exit cancels first and closes second.
    """
    if not enabled():
        return 0
    want = _alpaca_symbol(symbol)
    n = 0
    try:
        for o in _request("GET", "/v2/orders?status=open&limit=500&nested=false") or []:
            if not isinstance(o, dict) or o.get("symbol") != want:
                continue
            try:
                _request("DELETE", f"/v2/orders/{quote(str(o.get('id')), safe='')}")
                n += 1
            except BrokerError as e:
                print(f"[warn] broker cancel {o.get('id')}: {e}", file=sys.stderr)
    except BrokerError as e:
        print(f"[warn] broker list open orders {want}: {e}", file=sys.stderr)
    return n


# ── Write ───────────────────────────────────────────────────────────────────────

def _tick(px):
    """A price Alpaca will accept. Sub-dollar equities take four decimals, everything else two."""
    px = float(px)
    return round(px, 2) if px >= 1 else round(px, 4)


def _protection(side, entry, stop, target):
    """The advanced-order block for a protected entry, or None if the levels do not support one.

    These are Alpaca's constraints rather than ours. A buy bracket needs `take_profit.limit_price`
    strictly above `stop_loss.stop_price`, and both have to sit the right side of the entry or the
    order is rejected outright — a "stop" above the price you are paying is not a stop.

    A stop with no usable target still gets sent, as an `oto`. Half of a bracket is the half that
    matters: the take-profit is an optimisation, the stop is the reason any of this exists.
    """
    if side != "buy":
        return None
    e = float(entry or 0)
    s = float(stop or 0)
    t = float(target or 0)
    if e <= 0 or s <= 0 or s >= e:
        return None
    legs = {"stop_loss": {"stop_price": str(_tick(s))}}
    if t > e:
        legs["take_profit"] = {"limit_price": str(_tick(t))}
        legs["order_class"] = "bracket"
    else:
        legs["order_class"] = "oto"
    return legs


def _submit(symbol, qty, side, extra=None):
    # A venue that never closes has no concept of a day order: Alpaca accepts only `gtc` or `ioc`
    # on crypto and rejects `day` outright. Equities keep `day` — a stale queued order is worse
    # there than one that expires at the bell.
    tif = "gtc" if config.is_crypto(symbol) else "day"
    body = {"symbol": _alpaca_symbol(symbol), "qty": str(qty), "side": side,
            "type": "market", "time_in_force": tif}
    if extra:
        body.update(extra)
    return _request("POST", "/v2/orders", json=body)


def submit(symbol, shares, side="buy", entry=None, stop=None, target=None):
    """Mirror a gate-approved decision into the paper account.

    `entry`, `stop` and `target` are the levels the email quotes. Given them, an equity buy goes
    out as a **limit order at `entry` with the stop and target attached as bracket legs**, so the
    account holds the same three numbers Lind was told about. Without them — or on crypto, where
    Alpaca supports no advanced order class at all — it degrades to the plain market order this
    used to always send, and the position stays protected only by the simulator's polled stop.

    Two consequences worth stating, both reported back in the returned dict:

    - **Whole shares.** Alpaca refuses a fractional bracket. A protected order is worth more than
      the fraction, so the quantity floors; under one share there is nothing to floor to and the
      order goes out fractional and naked.
    - **It can go unfilled.** A limit at a pullback is exactly the order that does not fill on a
      day the price never comes back. That is the honest outcome, and `portfolio.reconcile_broker`
      is what notices it — the simulator must not keep a position the broker never opened.

    Alpaca only fills fractional quantities during regular market hours. The brain's anchors
    deliberately run pre-market and after the close too, so a fractional order outside RTH is
    expected to be rejected: we retry with whole shares, which queues for the next open.
    Returns a status dict — never raises, because a broker hiccup must not break a cycle
    whose accounting already lives in the simulator.
    """
    if not enabled():
        return {"mirrored": False, "reason": "no paper keys configured"}
    symbol = symbol.upper()
    shares = round(float(shares), 4)
    if shares <= 0:
        return {"mirrored": False, "reason": "zero shares"}
    crypto = config.is_crypto(symbol)
    if crypto:
        pair = _alpaca_symbol(symbol)
        listed = tradable_crypto()
        if listed and pair not in listed:
            return {"mirrored": False,
                    "reason": f"{pair} is not tradable on Alpaca — simulator only"}

    # The protected path. Crypto never takes it: Alpaca rejects bracket/oto/oco on every pair,
    # so a token's stop stays where it has always been, in the simulator's per-cycle poll.
    legs = None if crypto else _protection(side, entry, stop, target)
    whole = int(math.floor(shares))
    if crypto:
        naked = "crypto — Alpaca supports no bracket, OTO or OCO order class on any pair"
    elif not legs:
        naked = "no usable stop below the entry on the action — nothing to attach"
    elif whole < 1:
        naked = f"{shares} sh is under one share and a bracket cannot be fractional"
    else:
        naked = None
    if legs and whole >= 1:
        klass = legs.pop("order_class")
        body = {"type": "limit", "limit_price": str(_tick(entry)), "time_in_force": "day",
                "order_class": klass, **legs}
        try:
            o = _submit(symbol, whole, side, extra=body)
            out = {"mirrored": True, "order_id": o.get("id"), "qty": whole,
                   "status": o.get("status"), "fractional": False, "protected": True,
                   "order_class": klass, "limit_price": _tick(entry),
                   "stop_price": _tick(stop),
                   "take_profit": _tick(target) if klass == "bracket" else None,
                   # Read by reconcile_broker: a limit order is a request, not a fill, and until
                   # this flips the simulator's position is provisional.
                   "pending": True}
            if whole != shares:
                out["note"] = (f"bracket orders are whole-share only — sent {whole} of "
                               f"{shares} sh")
            return out
        except BrokerError as e:
            # A rejected bracket must not cost the mirror entirely: fall through to the plain
            # market order, unprotected but real, and say which one was sent.
            print(f"[warn] broker bracket {side} {symbol}: {e}", file=sys.stderr)
            naked = f"Alpaca rejected the bracket ({str(e)[:120]}) — sent unprotected instead"

    is_fractional = abs(shares - round(shares)) > 1e-6
    try:
        o = _submit(symbol, shares, side)
        return {"mirrored": True, "order_id": o.get("id"), "qty": shares,
                "status": o.get("status"), "fractional": is_fractional,
                "protected": False, "unprotected_because": naked}
    except BrokerError as e:
        first = str(e)
        whole = int(math.floor(shares)) if side == "buy" else int(math.floor(shares))
        # The whole-share retry exists because Alpaca only fills equity fractions during RTH. On a
        # 24/7 venue every fraction is fillable, so a crypto rejection means something else is
        # wrong — and rounding 0.53 ETH down would silently resize the trade to nothing anyway.
        if is_fractional and whole >= 1 and not crypto:
            try:
                o = _submit(symbol, whole, side)
                return {"mirrored": True, "order_id": o.get("id"), "qty": whole,
                        "status": o.get("status"), "fractional": False,
                        "protected": False, "unprotected_because": naked,
                        "note": "fractional rejected (outside market hours); "
                                "queued whole shares instead"}
            except BrokerError as e2:
                first = f"{first} | whole-share retry: {e2}"
        print(f"[warn] broker submit {side} {symbol}: {first}", file=sys.stderr)
        return {"mirrored": False, "reason": first[:300]}


def close(symbol):
    """Flatten a symbol in the paper account (mirrors a simulator exit).

    Cancels first. A bracket's take-profit and stop legs go on resting after the position they
    protect is gone, and one of them filling later would open a short nobody asked for.
    """
    if not enabled():
        return {"mirrored": False, "reason": "no paper keys configured"}
    cancelled = cancel_open(symbol)
    try:
        # The slash in a crypto pair is a path separator until it is encoded.
        o = _request("DELETE", f"/v2/positions/{quote(_alpaca_symbol(symbol), safe='')}")
        return {"mirrored": True, "order_id": (o or {}).get("id"),
                "status": (o or {}).get("status"), "cancelled_orders": cancelled}
    except BrokerError as e:
        msg = str(e)
        if "position does not exist" in msg.lower() or "404" in msg:
            # Not always a no-op: an entry limit that never filled has no position but does have
            # a working order, and cancelling it is the whole exit.
            return {"mirrored": False, "reason": "no paper position to close",
                    "cancelled_orders": cancelled}
        print(f"[warn] broker close {symbol}: {msg}", file=sys.stderr)
        return {"mirrored": False, "reason": msg[:300]}


_health_cache = None


def health():
    """One-line status for emails/logs. Never raises.

    Cached per process: portfolio.summary() calls this and summary() is called several times a
    run (email, dashboard push, recap), which would otherwise be several round trips for a
    status line that cannot change mid-cycle.
    """
    global _health_cache
    if not enabled():
        return {"enabled": False, "detail": "Alpaca paper keys not set — simulator only"}
    if _health_cache is not None:
        return _health_cache
    try:
        a = account()
        c = clock()
        _health_cache = {"enabled": True, "account": a["account_number"], "status": a["status"],
                         "equity": a["equity"], "cash": a["cash"],
                         "market_open": c["is_open"], "trading_blocked": a["trading_blocked"],
                         "detail": f"Alpaca PAPER {a['account_number']} · ${a['equity']:,.0f}"}
        return _health_cache
    except Exception as e:
        return {"enabled": True, "error": str(e)[:200], "detail": f"Alpaca paper unreachable: {e}"}
