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
import math
from datetime import datetime
from pathlib import Path

from . import broker, config
from .jsonio import dumps as _dumps


def usable_price(px):
    """True only for a real, positive, finite quote.

    yfinance hands back NaN for a halted name, a bad session, or a symbol it briefly cannot
    resolve. NaN is poisonous here: `NaN is None` is False so it slips past a null check,
    every comparison against it is False so a stop silently never fires, and it propagates
    through equity into PORTFOLIO.json as a bare `NaN` token that is not valid JSON. Treat a
    non-finite price as no price at all and leave the position marked at its last good quote.
    """
    try:
        return px is not None and math.isfinite(float(px)) and float(px) > 0
    except (TypeError, ValueError):
        return False

# The sector map now lives with the rule set that owns it (config.STOCK_SECTORS), because a
# second book needs a second map and a module-level dict cannot serve both. Re-exported here so
# existing importers keep working.
SECTORS = config.STOCK_SECTORS


def sector_of(ticker):
    """Stock-book sector lookup. Inside the class use `self.rules.sector_of` — the crypto book
    maps each token to its own sector and must not be answered from the equity table."""
    return config.STOCK_RULES.sector_of(ticker)


# The reasoning the model attached to an entry, carried whole from proposal to exit.
#
# This list used to be five fields, and everything downstream paid for it. The rulebook tells
# the model that `confidence_rationale` is graded on exit and that `holding_period` is checked
# against the actual hold — but neither survived past the fill, so the grader was grading fields
# that no longer existed and the weekly recap could not explain a single number it printed.
# A field dropped at entry is unrecoverable at exit: the packet that produced it is long gone.
# So the rule now is to persist the whole signal and let the readers choose, rather than to
# guess at write time which fields a future reader will want.
ENTRY_META_FIELDS = (
    "trade_type", "holding_period", "direction", "confidence", "confidence_rationale",
    "indicators_used", "news_at_entry", "news_read", "news_edge", "chart_read",
    "historical_analog", "analysis_done", "data_sources", "target1", "target2",
)


def held_hours(pos, now=None):
    """Wall-clock hours this position has been open, or None if it cannot be worked out.

    None rather than 0 on a bad timestamp, deliberately: 0 reads as "just opened" and would make
    the time stop hold a position forever, which is the exact failure it exists to prevent. None
    is checked explicitly at every call site so an unparseable `opened` skips the time stop and
    leaves the price stops to do their job.
    """
    opened = pos.get("opened")
    if not opened:
        return None
    try:
        ts = datetime.fromisoformat(str(opened).replace("Z", "+00:00"))
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=config.UTC)
    now = now or datetime.now(config.UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=config.UTC)
    return (now - ts).total_seconds() / 3600.0


def _default_state(rules=None):
    rules = rules or config.STOCK_RULES
    return {
        "cash": rules.starting_cash,
        "starting_cash": rules.starting_cash,
        "positions": {},          # ticker -> position dict
        "closed_trades": [],      # list of closed-trade records
        "sector_fails": {},       # sector -> consecutive-loss count
        "week_trades": {},        # ISO "YYYY-Www" -> count of NEW buys opened
        "equity_curve": [],       # [{ts, equity}]
        "updated": None,
    }


class Portfolio:
    def __init__(self, path=None, mirror=True, rules=None):
        # Which book this is. Everything gate-shaped reads from here rather than from the config
        # module, so the crypto book cannot silently inherit an equity stop.
        self.rules = rules or config.STOCK_RULES
        self.path = Path(path or (config.MEMORY_DIR / self.rules.ledger))
        self.state = self._load()
        # Mirror gate-approved decisions into the Alpaca PAPER account. Off for --dry-run and
        # for tests, so the gate suite never needs network access.
        self.mirror = bool(mirror) and broker.enabled()
        self.broker_events = []   # per-run mirror results, for the email + journal
        # Every position closed during this run, whatever closed it. mark_to_market() returns
        # its own stops directly, but a model-proposed SELL goes through apply_action(), whose
        # (ok, msg) contract has nowhere to put an exit record — so those closes used to reach
        # neither the live trade tape nor the journal's exit grading. Collecting them here means
        # the caller sees every exit through one list instead of two code paths.
        self.exits_this_run = []

    # ── persistence ──────────────────────────────────────────────────────────
    def _load(self):
        if self.path.exists():
            try:
                st = json.loads(self.path.read_text(encoding="utf-8"))
                base = _default_state(self.rules)
                base.update(st)
                return base
            except Exception:
                pass
        return _default_state(self.rules)

    def save(self):
        self.state["updated"] = datetime.now(config.UTC).isoformat()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(_dumps(self.state, indent=2), encoding="utf-8")

    # ── valuation ────────────────────────────────────────────────────────────
    def equity(self, prices=None):
        prices = prices or {}
        total = self.state["cash"]
        for t, pos in self.state["positions"].items():
            px = prices.get(t)
            if not usable_price(px):
                px = pos.get("last") if usable_price(pos.get("last")) else pos["entry"]
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
    def mark_to_market(self, prices, now=None):
        """Update last prices + high-water marks, then auto-exit on hard/trailing/time stops.

        Returns a list of {ticker, reason, pnl_pct, pnl_usd} for exits taken this pass.

        `now` is injectable so the time stop can be tested without waiting a day for it.
        """
        now = now or datetime.now(config.UTC)
        exits = []
        for t in list(self.state["positions"].keys()):
            pos = self.state["positions"][t]
            px = prices.get(t)
            if not usable_price(px):
                continue  # no fresh quote (or a NaN one) — never mark or stop on it
            pos["last"] = px
            pos["high_water"] = max(pos.get("high_water", pos["entry"]), px)
            gain = (px - pos["entry"]) / pos["entry"] * 100
            peak_gain = (pos["high_water"] - pos["entry"]) / pos["entry"] * 100
            scalp = self.rules.is_scalp(pos.get("trade_type"))
            stop_pct = self.rules.stop_pct_for(pos.get("trade_type"))

            # Tighten the trailing stop as the trade works — but a scalp does not step through
            # the +15%/+20% ladder at all. That ladder exists to let a multi-week winner breathe;
            # on a position measured in hours it would hand back most of a move that took
            # minutes to make. A scalp trails one tight distance from its peak, start to finish.
            if scalp:
                trail = self.rules.scalp_trail_pct
            else:
                trail = self.rules.trail_base_pct
                if peak_gain >= 20:
                    trail = self.rules.trail_tight_20
                elif peak_gain >= 15:
                    trail = self.rules.trail_tight_15
            pos["trail_pct"] = trail
            trail_level = pos["high_water"] * (1 - trail / 100)
            pos["stop_level"] = round(max(trail_level, pos.get("hard_stop", 0)), 2)

            # The deadline written at entry wins over the current rules, so re-tuning the window
            # never retroactively times out (or reprieves) a position already on the book.
            max_hold = pos.get("scalp_max_hold_h") or self.rules.scalp_max_hold_h
            held_h = held_hours(pos, now)
            if scalp and held_h is not None:
                # Surfaced on the position so the email, the dashboard and a human reading the
                # ledger can all see the clock, not just the exit that eventually fires from it.
                pos["scalp_hours_left"] = round(max(0.0, max_hold - held_h), 2)

            reason = kind = None
            if gain <= stop_pct:
                reason = f"hard stop hit ({gain:.1f}% ≤ {stop_pct}%)"
                kind = "hard_stop"
            elif px <= trail_level:
                reason = f"trailing stop hit ({trail:.0f}% from peak {pos['high_water']:.2f})"
                kind = "trailing_stop"
            elif scalp and held_h is not None and held_h >= max_hold:
                # Checked last on purpose. If a scalp is both out of time and through its stop,
                # the price stop is the more useful thing to read on the exit card and in the
                # journal's grading — "it went against me" explains more than "time was up".
                reason = (f"scalp time stop ({held_h:.1f}h held ≥ "
                          f"{max_hold:g}h max, closed {gain:+.1f}%)")
                kind = "time_stop"
            if reason:
                exits.append(self._close(t, px, reason, kind=kind))
        self.state["equity_curve"].append(
            {"ts": datetime.now(config.UTC).isoformat(), "equity": self.equity(prices)})
        self.state["equity_curve"] = self.state["equity_curve"][-500:]
        return exits

    def scalp_hours_left(self, pos, now=None):
        """Hours before this scalp is force-closed, or None if it is not on the scalp clock."""
        if not self.rules.is_scalp(pos.get("trade_type")):
            return None
        held_h = held_hours(pos, now)
        if held_h is None:
            return None
        max_hold = pos.get("scalp_max_hold_h") or self.rules.scalp_max_hold_h
        return round(max(0.0, max_hold - held_h), 2)

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

    def _close(self, ticker, price, reason, kind="model_sell"):
        """Close a position and hand back one record that carries the whole trade.

        `kind` is the machine-readable counterpart to `reason`: "hard_stop", "trailing_stop" or
        "model_sell". The weekly recap counts the exit mix, and counting it by substring-matching
        a human sentence would silently miscount the first time someone rewords a message.
        """
        pos = self.state["positions"].pop(ticker)
        proceeds = pos["shares"] * price
        pnl_usd = proceeds - pos["shares"] * pos["entry"]
        pnl_pct = (price - pos["entry"]) / pos["entry"] * 100
        self.state["cash"] += proceeds
        sector = pos.get("sector", self.rules.sector_of(ticker))
        # sector-fail bookkeeping: a loser increments, a winner resets
        if pnl_usd < 0:
            self.state["sector_fails"][sector] = self.state["sector_fails"].get(sector, 0) + 1
        else:
            self.state["sector_fails"][sector] = 0
        mirror = self._mirror("close", ticker)
        # The entry's whole reasoning snapshot, carried onto the exit so the journal grades what
        # was actually claimed rather than reconstructing it.
        carried = {k: pos.get(k) for k in ENTRY_META_FIELDS}
        carried["thesis"] = pos.get("thesis")
        rec = {
            "ticker": ticker, "sector": sector, "shares": pos["shares"],
            "entry": pos["entry"], "exit": price, "reason": reason, "exit_kind": kind,
            "pnl_usd": round(pnl_usd, 2), "pnl_pct": round(pnl_pct, 2),
            "opened": pos.get("opened"), "closed": datetime.now(config.UTC).isoformat(),
            "broker": mirror, **carried,
        }
        self.state["closed_trades"].append(rec)
        out = {"ticker": ticker, "sector": sector, "reason": reason, "exit_kind": kind,
               "entry": pos["entry"], "exit": price,
               "pnl_pct": round(pnl_pct, 2), "pnl_usd": round(pnl_usd, 2),
               "opened": pos.get("opened"), "closed": rec["closed"],
               "broker": mirror, **carried}
        self.exits_this_run.append(out)
        return out

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
            if kind == "BUY" and len(self.state["positions"]) >= self.rules.max_positions:
                return False, f"at position cap ({self.rules.max_positions})"
            if self.new_trades_this_week() >= self.rules.max_new_trades_per_week and kind == "BUY":
                return False, (f"weekly new-trade cap reached "
                               f"({self.rules.max_new_trades_per_week}/week)")
            sector = self.rules.sector_of(ticker)
            if self.state["sector_fails"].get(sector, 0) >= self.rules.sector_fail_limit:
                return False, (f"sector '{sector}' locked out after "
                               f"{self.rules.sector_fail_limit} consecutive losers")
            price = action.get("entry") or prices.get(ticker)
            if not price:
                return False, f"no price for {ticker}"
            tt = action.get("trade_type")
            target_pct = min(action.get("target_weight_pct", self.rules.default_pct_for(tt)),
                         self.rules.weight_cap_for(tt))
            target_usd = eq * target_pct / 100
            if kind == "ADD":
                cur = self.state["positions"].get(ticker)
                cur_val = cur["shares"] * price if cur else 0
                target_usd = max(0, target_usd - cur_val)
            if target_usd > self.state["cash"]:
                target_usd = self.state["cash"]
            # Crypto is bought in fractions, so "cannot afford one whole unit" is not a real
            # constraint there — a $1,000 slot is 0.0154 BTC, and applying the equity floor made
            # the crypto book structurally unable to open BTC (~$65k/coin) or ETH at any legal
            # weight. Equities keep the whole-share floor; apply_action's `sized to zero shares`
            # check still catches a slot too small to round to a position.
            unit_floor = 0.0 if config.is_crypto(ticker) else price
            if target_usd <= unit_floor:
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
        trade_type = action.get("trade_type")
        target_pct = min(action.get("target_weight_pct", self.rules.default_pct_for(trade_type)),
                         self.rules.weight_cap_for(trade_type))
        target_usd = min(eq * target_pct / 100, self.state["cash"])
        shares = round(target_usd / price, 4)
        if shares <= 0:
            return False, "sized to zero shares"
        stop_pct = self.rules.stop_pct_for(trade_type)
        hard_stop = action.get("stop") or round(price * (1 + stop_pct / 100), 2)
        # A scalp's proposed stop is clamped to the scalp band. The model routinely attaches the
        # swing-width stop it was thinking in to an idea it then labels SCALP, and an un-clamped
        # −12% stop on a position meant to live four hours is the mislabel this whole block
        # exists to stop. Wider than the band is refused; tighter than it is the model's call.
        if self.rules.is_scalp(trade_type):
            hard_stop = max(hard_stop, round(price * (1 + stop_pct / 100), 2))
        now = datetime.now(config.UTC).isoformat()
        # Reasoning snapshot taken at entry — the journal grades this against the exit.
        entry_meta = {k: action.get(k) for k in ENTRY_META_FIELDS}
        entry_meta["thesis"] = action.get("reason") or action.get("why")
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
                "sector": self.rules.sector_of(ticker), "high_water": price,
                "hard_stop": hard_stop,
                "trail_pct": (self.rules.scalp_trail_pct if self.rules.is_scalp(trade_type)
                              else self.rules.trail_base_pct),
                # Written at entry so the deadline is a fact in the ledger rather than something
                # recomputed from whatever the rules happen to say on the day it fires.
                "scalp_max_hold_h": (self.rules.scalp_max_hold_h
                                     if self.rules.is_scalp(trade_type) else None),
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
            px = prices.get(t)
            if not usable_price(px):
                px = pos.get("last") if usable_price(pos.get("last")) else pos["entry"]
            opened = pos.get("opened")
            held_days = None
            if opened:
                try:
                    held_days = round(
                        (datetime.now(config.UTC)
                         - datetime.fromisoformat(str(opened).replace("Z", "+00:00"))
                         ).total_seconds() / 86400, 2)
                except ValueError:
                    held_days = None
            positions.append({
                "ticker": t, "shares": pos["shares"], "entry": pos["entry"],
                "last": px, "pnl_pct": round((px - pos["entry"]) / pos["entry"] * 100, 2),
                "value": round(pos["shares"] * px, 2), "stop_level": pos.get("stop_level"),
                "sector": pos.get("sector"),
                # Carried so a reader can tell a scalp from an investment without opening the
                # ledger. The weekly recap needs the horizon to flag a SCALP still open on
                # Friday, and the dashboard row was showing a blank type for every open name.
                "trade_type": pos.get("trade_type"), "confidence": pos.get("confidence"),
                "holding_period": pos.get("holding_period"),
                "opened": opened, "held_days": held_days,
                # Recomputed here rather than read off the position: mark_to_market only writes
                # it when a fresh quote arrived, and a scalp whose feed went quiet is exactly the
                # one whose remaining clock a reader most needs to see.
                "scalp_hours_left": self.scalp_hours_left(pos),
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
            # Which book this is, and its own caps. The email and the dashboard used to read the
            # module-level equity constants, so a crypto summary rendered "3/8 positions" against
            # limits that book has never been subject to.
            "book": self.rules.name,
            "max_positions": self.rules.max_positions,
            "max_new_trades_per_week": self.rules.max_new_trades_per_week,
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
