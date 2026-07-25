"""Simulated paper portfolio — the discipline layer.

A virtual $10k account with the PDF's Part-2 hard rules enforced *in code*. Claude only
proposes actions; every proposal passes through `validate_action`, and mechanical exits
(-7% hard cut, trailing stops) run automatically each cycle regardless of what Claude says.

NO REAL-MONEY ORDERS ARE EVER PLACED. This JSON ledger (brain-memory/PORTFOLIO.json) remains
the accounting source of truth for the gates, the equity curve, and the journal. Once Alpaca
PAPER keys are configured, every gate-approved entry and every mechanical exit is *mirrored*
into that paper account through brain/broker.py, which can only reach
paper-api.alpaca.markets — a different host from Alpaca's live API. A broker failure is
recorded and ignored; it never changes the ledger or breaks a cycle.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from . import broker, config

# Minimal sector map for the focus list; anything unmapped is "Other".
SECTORS = {
    "AAPL": "Tech", "MSFT": "Tech", "NVDA": "Semis", "AMD": "Semis", "AVGO": "Semis",
    "TSM": "Semis", "MU": "Semis", "SMCI": "Semis", "ORCL": "Tech", "ADBE": "Tech",
    "CRM": "Tech", "PANW": "Tech", "SNOW": "Tech", "PLTR": "Tech",
    "TSLA": "Auto", "AMZN": "Consumer", "META": "Tech", "GOOGL": "Tech", "NFLX": "Media",
    "DIS": "Media", "UBER": "Tech", "COIN": "Crypto", "HOOD": "Fintech", "MSTR": "Crypto",
    "MARA": "Crypto", "SOFI": "Fintech", "JPM": "Banks", "GS": "Banks", "BAC": "Banks",
    "XOM": "Energy", "CVX": "Energy", "CEG": "Energy", "LLY": "Health", "UNH": "Health",
    "GE": "Industrial", "BA": "Industrial", "CAT": "Industrial",
}


def sector_of(ticker):
    return SECTORS.get(ticker, "Other")


def _default_state():
    return {
        "cash": config.STARTING_CASH,
        "starting_cash": config.STARTING_CASH,
        "positions": {},          # ticker -> position dict
        "closed_trades": [],      # list of closed-trade records
        "sector_fails": {},       # sector -> consecutive-loss count
        "week_trades": {},        # ISO "YYYY-Www" -> count of NEW buys opened
        "equity_curve": [],       # [{ts, equity}]
        "updated": None,
    }


class Portfolio:
    def __init__(self, path=None, mirror=True):
        self.path = Path(path or (config.MEMORY_DIR / "PORTFOLIO.json"))
        self.state = self._load()
        # Mirror gate-approved decisions into the Alpaca PAPER account. Off for --dry-run and
        # for tests, so the gate suite never needs network access.
        self.mirror = bool(mirror) and broker.enabled()
        self.broker_events = []   # per-run mirror results, for the email + journal

    # ── persistence ──────────────────────────────────────────────────────────
    def _load(self):
        if self.path.exists():
            try:
                st = json.loads(self.path.read_text(encoding="utf-8"))
                base = _default_state()
                base.update(st)
                return base
            except Exception:
                pass
        return _default_state()

    def save(self):
        self.state["updated"] = datetime.now(config.UTC).isoformat()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.state, indent=2), encoding="utf-8")

    # ── valuation ────────────────────────────────────────────────────────────
    def equity(self, prices=None):
        prices = prices or {}
        total = self.state["cash"]
        for t, pos in self.state["positions"].items():
            px = prices.get(t, pos.get("last", pos["entry"]))
            total += pos["shares"] * px
        return round(total, 2)

    def held_tickers(self):
        return list(self.state["positions"].keys())

    def _iso_week(self, dt=None):
        dt = dt or datetime.now(config.UTC)
        y, w, _ = dt.isocalendar()
        return f"{y}-W{w:02d}"

    def new_trades_this_week(self):
        return self.state["week_trades"].get(self._iso_week(), 0)

    # ── mechanical exits (run every cycle, independent of Claude) ─────────────
    def mark_to_market(self, prices):
        """Update last prices + high-water marks, then auto-exit on hard/trailing stops.

        Returns a list of {ticker, reason, pnl_pct, pnl_usd} for exits taken this pass.
        """
        exits = []
        for t in list(self.state["positions"].keys()):
            pos = self.state["positions"][t]
            px = prices.get(t)
            if px is None:
                continue
            pos["last"] = px
            pos["high_water"] = max(pos.get("high_water", pos["entry"]), px)
            gain = (px - pos["entry"]) / pos["entry"] * 100
            peak_gain = (pos["high_water"] - pos["entry"]) / pos["entry"] * 100

            # tighten trailing stop as the trade works
            trail = config.TRAIL_BASE_PCT
            if peak_gain >= 20:
                trail = config.TRAIL_TIGHT_20
            elif peak_gain >= 15:
                trail = config.TRAIL_TIGHT_15
            pos["trail_pct"] = trail
            trail_level = pos["high_water"] * (1 - trail / 100)
            pos["stop_level"] = round(max(trail_level, pos.get("hard_stop", 0)), 2)

            reason = None
            if gain <= config.HARD_STOP_PCT:
                reason = f"hard stop hit ({gain:.1f}% ≤ {config.HARD_STOP_PCT}%)"
            elif px <= trail_level:
                reason = f"trailing stop hit ({trail:.0f}% from peak {pos['high_water']:.2f})"
            if reason:
                exits.append(self._close(t, px, reason))
        self.state["equity_curve"].append(
            {"ts": datetime.now(config.UTC).isoformat(), "equity": self.equity(prices)})
        self.state["equity_curve"] = self.state["equity_curve"][-500:]
        return exits

    def _mirror(self, kind, ticker, **kw):
        """Send one decision to the paper broker. Records the result; never raises."""
        if not self.mirror:
            return None
        try:
            if kind == "buy":
                res = broker.submit(ticker, kw["shares"], side="buy")
            else:
                res = broker.close(ticker)
        except Exception as e:                      # belt and braces — broker already catches
            res = {"mirrored": False, "reason": str(e)[:200]}
        ev = {"kind": kind, "ticker": ticker, "ts": datetime.now(config.UTC).isoformat()}
        ev.update(res or {})
        self.broker_events.append(ev)
        return res

    def _close(self, ticker, price, reason):
        pos = self.state["positions"].pop(ticker)
        proceeds = pos["shares"] * price
        pnl_usd = proceeds - pos["shares"] * pos["entry"]
        pnl_pct = (price - pos["entry"]) / pos["entry"] * 100
        self.state["cash"] += proceeds
        sector = pos.get("sector", sector_of(ticker))
        # sector-fail bookkeeping: a loser increments, a winner resets
        if pnl_usd < 0:
            self.state["sector_fails"][sector] = self.state["sector_fails"].get(sector, 0) + 1
        else:
            self.state["sector_fails"][sector] = 0
        mirror = self._mirror("close", ticker)
        rec = {
            "ticker": ticker, "sector": sector, "shares": pos["shares"],
            "entry": pos["entry"], "exit": price, "reason": reason,
            "pnl_usd": round(pnl_usd, 2), "pnl_pct": round(pnl_pct, 2),
            "opened": pos.get("opened"), "closed": datetime.now(config.UTC).isoformat(),
            # Carried from the entry so the journal can grade the original reasoning
            # against the outcome instead of guessing why the trade was taken.
            "trade_type": pos.get("trade_type"), "thesis": pos.get("thesis"),
            "confidence": pos.get("confidence"), "indicators_used": pos.get("indicators_used"),
            "news_at_entry": pos.get("news_at_entry"),
            "broker": mirror,
        }
        self.state["closed_trades"].append(rec)
        return {"ticker": ticker, "reason": reason, "exit": price,
                "pnl_pct": round(pnl_pct, 2), "pnl_usd": round(pnl_usd, 2),
                "trade_type": pos.get("trade_type"), "thesis": pos.get("thesis"),
                "confidence": pos.get("confidence"), "opened": pos.get("opened"),
                "broker": mirror}

    # ── proposed-action gates ────────────────────────────────────────────────
    def validate_action(self, action, prices):
        """Check a proposed action against every hard rule.

        action: {"action": "BUY"|"SELL"|"ADD"|"HOLD", "ticker": str,
                 "entry": float?, "stop": float?, "target_weight_pct": float?}
        Returns (ok: bool, message: str).
        """
        kind = (action.get("action") or "").upper()
        ticker = action.get("ticker", "")
        if kind in ("HOLD", "WATCH", ""):
            return True, "no-op"
        if kind == "SELL":
            if ticker not in self.state["positions"]:
                return False, f"cannot SELL {ticker}: not held"
            return True, "sell allowed"
        if kind in ("BUY", "ADD"):
            eq = self.equity(prices)
            if kind == "BUY" and ticker in self.state["positions"]:
                return False, f"{ticker} already held (use ADD)"
            if kind == "BUY" and len(self.state["positions"]) >= config.MAX_POSITIONS:
                return False, f"at position cap ({config.MAX_POSITIONS})"
            if self.new_trades_this_week() >= config.MAX_NEW_TRADES_PER_WEEK and kind == "BUY":
                return False, (f"weekly new-trade cap reached "
                               f"({config.MAX_NEW_TRADES_PER_WEEK}/week)")
            sector = sector_of(ticker)
            if self.state["sector_fails"].get(sector, 0) >= config.SECTOR_FAIL_LIMIT:
                return False, (f"sector '{sector}' locked out after "
                               f"{config.SECTOR_FAIL_LIMIT} consecutive losers")
            price = action.get("entry") or prices.get(ticker)
            if not price:
                return False, f"no price for {ticker}"
            target_pct = min(action.get("target_weight_pct", 15.0), config.MAX_POSITION_PCT)
            target_usd = eq * target_pct / 100
            if kind == "ADD":
                cur = self.state["positions"].get(ticker)
                cur_val = cur["shares"] * price if cur else 0
                target_usd = max(0, target_usd - cur_val)
            if target_usd > self.state["cash"]:
                target_usd = self.state["cash"]
            if target_usd < price:  # can't afford even one share
                return False, f"insufficient cash for {ticker} (${self.state['cash']:.0f})"
            return True, f"buy ~${target_usd:.0f} ({target_pct:.0f}% target)"
        return False, f"unknown action '{kind}'"

    def apply_action(self, action, prices):
        """Validate then apply. Returns (applied: bool, message: str)."""
        ok, msg = self.validate_action(action, prices)
        if not ok:
            return False, msg
        kind = (action.get("action") or "").upper()
        ticker = action.get("ticker", "")
        if kind in ("HOLD", "WATCH", ""):
            return True, "held"
        if kind == "SELL":
            px = action.get("entry") or prices.get(ticker) or self.state["positions"][ticker].get("last")
            res = self._close(ticker, px, action.get("reason", "Claude proposed exit"))
            return True, f"closed {ticker} {res['pnl_pct']:+.1f}%"
        # BUY / ADD
        price = action.get("entry") or prices.get(ticker)
        eq = self.equity(prices)
        target_pct = min(action.get("target_weight_pct", 15.0), config.MAX_POSITION_PCT)
        target_usd = min(eq * target_pct / 100, self.state["cash"])
        shares = round(target_usd / price, 4)
        if shares <= 0:
            return False, "sized to zero shares"
        hard_stop = action.get("stop") or round(price * (1 + config.HARD_STOP_PCT / 100), 2)
        now = datetime.now(config.UTC).isoformat()
        # Reasoning snapshot taken at entry — the journal grades this against the exit.
        entry_meta = {
            "trade_type": action.get("trade_type"),
            "thesis": action.get("reason") or action.get("why"),
            "confidence": action.get("confidence"),
            "indicators_used": action.get("indicators_used"),
            "news_at_entry": action.get("news_at_entry"),
        }
        mirror = self._mirror("buy", ticker, shares=shares)
        if kind == "ADD" and ticker in self.state["positions"]:
            pos = self.state["positions"][ticker]
            tot = pos["shares"] + shares
            pos["entry"] = round((pos["entry"] * pos["shares"] + price * shares) / tot, 4)
            pos["shares"] = tot
            pos["adds"] = (pos.get("adds") or []) + [
                {"ts": now, "shares": shares, "price": price, "broker": mirror}]
        else:
            self.state["positions"][ticker] = {
                "shares": shares, "entry": price, "opened": now,
                "sector": sector_of(ticker), "high_water": price,
                "hard_stop": hard_stop, "trail_pct": config.TRAIL_BASE_PCT,
                "last": price, "target": action.get("target"),
                "broker": mirror, **entry_meta,
            }
            wk = self._iso_week()
            self.state["week_trades"][wk] = self.state["week_trades"].get(wk, 0) + 1
        self.state["cash"] -= shares * price
        msg = f"opened {ticker} {shares} sh @ {price:.2f}"
        if mirror and not mirror.get("mirrored"):
            msg += f" [paper broker not mirrored: {mirror.get('reason','')[:80]}]"
        elif mirror:
            msg += f" [Alpaca paper order {str(mirror.get('order_id'))[:8]}]"
        return True, msg

    # ── reporting ─────────────────────────────────────────────────────────────
    def summary(self, prices=None):
        prices = prices or {}
        eq = self.equity(prices)
        positions = []
        for t, pos in self.state["positions"].items():
            px = prices.get(t, pos.get("last", pos["entry"]))
            positions.append({
                "ticker": t, "shares": pos["shares"], "entry": pos["entry"],
                "last": px, "pnl_pct": round((px - pos["entry"]) / pos["entry"] * 100, 2),
                "value": round(pos["shares"] * px, 2), "stop_level": pos.get("stop_level"),
                "sector": pos.get("sector"),
            })
        closed = self.state["closed_trades"]
        wins = [c for c in closed if c["pnl_usd"] > 0]
        return {
            "equity": eq, "cash": round(self.state["cash"], 2),
            "starting_cash": self.state["starting_cash"],
            "total_return_pct": round((eq - self.state["starting_cash"]) / self.state["starting_cash"] * 100, 2),
            "open_positions": positions,
            "n_open": len(positions), "n_closed": len(closed),
            "win_rate": round(len(wins) / len(closed) * 100, 1) if closed else None,
            "new_trades_this_week": self.new_trades_this_week(),
            "sector_fails": {k: v for k, v in self.state["sector_fails"].items() if v},
            "broker": broker.health(),
            "broker_events": self.broker_events,
        }

    def realized_pnl(self):
        return round(sum(c["pnl_usd"] for c in self.state["closed_trades"]), 2)

    def trades_between(self, start_iso, end_iso=None):
        """Closed trades in a window — the weekly recap's raw material."""
        end_iso = end_iso or datetime.now(config.UTC).isoformat()
        return [c for c in self.state["closed_trades"]
                if c.get("closed") and start_iso <= c["closed"] <= end_iso]

    def opened_between(self, start_iso, end_iso=None):
        """Positions opened in a window, including ones still open."""
        end_iso = end_iso or datetime.now(config.UTC).isoformat()
        out = []
        for t, pos in self.state["positions"].items():
            if pos.get("opened") and start_iso <= pos["opened"] <= end_iso:
                out.append({"ticker": t, **pos, "still_open": True})
        for c in self.state["closed_trades"]:
            if c.get("opened") and start_iso <= c["opened"] <= end_iso:
                out.append({**c, "still_open": False})
        return out
