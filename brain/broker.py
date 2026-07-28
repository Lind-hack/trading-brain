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


# ── Write ───────────────────────────────────────────────────────────────────────

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


def submit(symbol, shares, side="buy"):
    """Mirror a gate-approved decision into the paper account.

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
    is_fractional = abs(shares - round(shares)) > 1e-6
    try:
        o = _submit(symbol, shares, side)
        return {"mirrored": True, "order_id": o.get("id"), "qty": shares,
                "status": o.get("status"), "fractional": is_fractional}
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
                        "note": "fractional rejected (outside market hours); "
                                "queued whole shares instead"}
            except BrokerError as e2:
                first = f"{first} | whole-share retry: {e2}"
        print(f"[warn] broker submit {side} {symbol}: {first}", file=sys.stderr)
        return {"mirrored": False, "reason": first[:300]}


def close(symbol):
    """Flatten a symbol in the paper account (mirrors a simulator exit)."""
    if not enabled():
        return {"mirrored": False, "reason": "no paper keys configured"}
    try:
        # The slash in a crypto pair is a path separator until it is encoded.
        o = _request("DELETE", f"/v2/positions/{quote(_alpaca_symbol(symbol), safe='')}")
        return {"mirrored": True, "order_id": (o or {}).get("id"), "status": (o or {}).get("status")}
    except BrokerError as e:
        msg = str(e)
        if "position does not exist" in msg.lower() or "404" in msg:
            return {"mirrored": False, "reason": "no paper position to close"}
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
