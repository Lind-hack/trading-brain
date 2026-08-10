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

from . import broker, config, entry
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


# Sub-cent tokens cannot take a two-decimal rounding — see config.round_price for what breaks.
# Re-exported under the name this module's callers already use.
round_px = config.round_price

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
        # Entries the broker never filled, backed out of the ledger. Deliberately NOT in
        # closed_trades: a limit that never traded is not a trade, and counting it would put a
        # 0.0% row into every win-rate, exit-mix and P&L number the recap computes.
        "unfilled": [],           # [{ticker, shares, price, order_id, status, ts, reason}]
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

    def new_trades_this_week_by_type(self):
        """This week's opens broken down by horizon: {"SCALP": 2, "SHORT_TERM": 1, ...}.

        Derived from the ledger rather than kept as a second counter beside `week_trades`. Both a
        scalp closed the same afternoon and an unfilled limit backed out of the book would have to
        remember to touch a parallel tally, and the one that forgot would quietly under-report the
        horizon it belonged to for the rest of the week. Positions and closed trades both carry
        `opened` and `trade_type`, so the answer is already on disk.

        A position whose `trade_type` names no known horizon is counted under `"UNSPECIFIED"` — it
        happened, and hiding it would make the counts disagree with `new_trades_this_week`.
        """
        wk = self._iso_week()
        counts = {}
        opened_this_week = [p for p in self.state["positions"].values()]
        opened_this_week += [t for t in self.state["closed_trades"]]
        for rec in opened_this_week:
            try:
                if self._iso_week(datetime.fromisoformat(rec["opened"])) != wk:
                    continue
            except (KeyError, TypeError, ValueError):
                continue     # no readable open timestamp — it cannot be placed in a week at all
            horizon = config.normalize_trade_type(rec.get("trade_type")) or "UNSPECIFIED"
            counts[horizon] = counts.get(horizon, 0) + 1
        return counts

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

            # One call now decides the trailing distance for all three horizons — the scalp's flat
            # band, the swing ladder that tightens at +15%/+20%, and the long-term `None`. The
            # ladder used to be inlined here, which meant the long horizon could not opt out of it
            # without this block growing a third branch that the entry path (which writes
            # `trail_pct` at open) would then have to repeat.
            trail = self.rules.trail_pct_for(pos.get("trade_type"), peak_gain)
            pos["trail_pct"] = trail
            # A long-term position has no trailing stop at all, so there is no trail level to be
            # through and the hard stop is the only price exit it has. `-inf` rather than 0: a
            # trail level of zero is a real level that a sub-$1 token can actually print.
            trail_level = pos["high_water"] * (1 - trail / 100) if trail is not None else float("-inf")
            pos["stop_level"] = round_px(max(trail_level, pos.get("hard_stop", 0)))

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
        # The benchmark is stamped onto the same curve point as the equity it will be compared
        # against, rather than fetched later for "roughly that week". Two prices read from one
        # `prices` dict on one cycle span exactly the same window by construction, which is the
        # whole reason the comparison can be trusted. Absent (no quote this cycle, a book with no
        # benchmark, every point written before this shipped) the key is simply missing, and
        # build_recap reads it defensively — an old ledger on disk predates the field entirely.
        point = {"ts": datetime.now(config.UTC).isoformat(), "equity": self.equity(prices)}
        bench_px = prices.get(getattr(self.rules, "benchmark", None) or "")
        if usable_price(bench_px):
            point["bench"] = bench_px
        self.state["equity_curve"].append(point)
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
                # The three numbers the email quotes go to the broker as well, so the account
                # holds the plan rather than an approximation of it: a limit at `entry`, with
                # `stop` and `target` attached as bracket legs.
                res = broker.submit(ticker, kw["shares"], side="buy", entry=kw.get("entry"),
                                    stop=kw.get("stop"), target=kw.get("target"))
            else:
                res = broker.close(ticker)
        except Exception as e:                      # belt and braces — broker already catches
            res = {"mirrored": False, "reason": str(e)[:200]}
        ev = {"kind": kind, "ticker": ticker, "ts": datetime.now(config.UTC).isoformat()}
        ev.update(res or {})
        self.broker_events.append(ev)
        return res

    # ── broker reconciliation ────────────────────────────────────────────────
    #
    # A market order is a fill; a limit order is a request. Once entries go out as limits at the
    # quoted price, the ledger can no longer assume the trade happened just because it decided the
    # trade should happen. This pass asks Alpaca what actually became of each pending entry, and
    # it is the piece that makes the bracket safe to switch on: without it, a limit that never
    # traded leaves the simulator holding a position that exists in no account anywhere.
    #
    # Ordering matters — run it *before* mark_to_market, so a phantom position is gone before the
    # stops get a chance to price it and the equity curve gets a chance to record it.

    _TERMINAL = ("canceled", "cancelled", "expired", "rejected", "done_for_day", "suspended")

    def reconcile_broker(self):
        """Settle every pending broker entry. Returns one record per order that resolved.

        Three outcomes, and only the third changes the ledger:

          filled       — record the *real* fill price and the slippage against the quoted entry.
                         The simulator keeps booking the quoted price, on purpose: it is grading
                         the analyst's plan, and a plan graded at a price the analyst did not
                         choose grades the venue instead. The delta is recorded, not applied.
          working      — left alone. A day limit is allowed to sit unfilled until the bell.
          never filled — backed out. Cash returned, the week's trade count decremented, the
                         attempt logged to state["unfilled"].

        A broker that cannot be reached returns nothing and everything stays pending, which is
        the safe direction: an unreadable order is not evidence that it failed.
        """
        if not self.mirror:
            return []
        out = []
        for ticker in list(self.state["positions"].keys()):
            pos = self.state["positions"].get(ticker)
            if not pos:
                continue
            # The opening order first, then any ADD that is still pending. An ADD carries its own
            # broker record inside pos["adds"], so it needs backing out at its own size rather
            # than unwinding the whole position underneath it.
            for slot, holder in [("open", pos)] + [
                    ("add", a) for a in (pos.get("adds") or [])]:
                b = holder.get("broker") or {}
                if not (b.get("mirrored") and b.get("pending") and b.get("order_id")):
                    continue
                o = broker.order(b["order_id"])
                if not o:
                    continue
                status = str(o.get("status") or "").lower()
                filled = float(o.get("filled_qty") or 0)
                if status == "filled" or filled > 0:
                    b["pending"] = False
                    b["filled_qty"] = filled
                    b["fill_price"] = o.get("filled_avg_price")
                    quoted = holder.get("price") if slot == "add" else pos.get("entry")
                    if o.get("filled_avg_price") and quoted:
                        b["slippage_pct"] = round(
                            (o["filled_avg_price"] - quoted) / quoted * 100, 3)
                    if status != "filled":
                        b["partial"] = True
                    out.append({"ticker": ticker, "slot": slot, "outcome": "filled",
                                "fill_price": o.get("filled_avg_price"),
                                "slippage_pct": b.get("slippage_pct"), "status": status})
                elif status in self._TERMINAL:
                    b["pending"] = False
                    rec = self._unwind_unfilled(ticker, holder, slot, status)
                    out.append(rec)
                    if slot == "open":
                        break       # the position is gone; its adds went with it
        return out

    def _unwind_unfilled(self, ticker, holder, slot, status):
        """Back an entry the broker never filled out of the ledger."""
        now = datetime.now(config.UTC).isoformat()
        b = holder.get("broker") or {}
        pos = self.state["positions"][ticker]
        if slot == "add":
            shares, price = holder["shares"], holder["price"]
            remaining = pos["shares"] - shares
            if remaining > 1e-9:
                # Un-average the entry: back out exactly the cost this add put in.
                pos["entry"] = round_px(
                    (pos["entry"] * pos["shares"] - price * shares) / remaining, dp=4)
                pos["shares"] = remaining
                pos["adds"] = [a for a in pos.get("adds", []) if a is not holder]
            else:
                slot = "open"       # the add was the whole position; fall through and drop it
        if slot == "open":
            shares, price = pos["shares"], pos["entry"]
            self.state["positions"].pop(ticker, None)
            # The week's pace counter counted a trade that never happened. Credited back to the
            # week the order was *placed* — a Friday limit reconciled on Monday belongs to Friday.
            wk = self._iso_week()
            try:
                wk = self._iso_week(datetime.fromisoformat(pos["opened"]))
            except (KeyError, TypeError, ValueError):
                pass
            if self.state["week_trades"].get(wk):
                self.state["week_trades"][wk] -= 1
        self.state["cash"] += shares * price
        reason = (f"entry limit at {config.format_price(price)} never filled "
                  f"(order {status})")
        rec = {"ticker": ticker, "slot": slot, "outcome": "never_filled",
               "shares": shares, "price": price, "order_id": b.get("order_id"),
               "status": status, "ts": now, "reason": reason}
        self.state.setdefault("unfilled", []).append(rec)
        self.state["unfilled"] = self.state["unfilled"][-100:]
        return rec

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
    def validate_action(self, action, prices, market=None):
        """Check a proposed action against every hard rule.

        action: {"action": "BUY"|"SELL"|"ADD"|"HOLD", "ticker": str,
                 "entry": float?, "stop": float?, "target_weight_pct": float?}
        market: the cycle's collected snapshots, keyed by ticker. Optional, and only the entry
                gate uses it — without it every other rule below still applies exactly as before.
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
            # Conviction first, before any bookkeeping rule. An idea the analyst is 48% sure of
            # should be refused for being a weak idea, not for arriving on a week whose trade cap
            # happened to be spent — and a rejection message that says the wrong thing teaches the
            # wrong lesson to the Friday review that reads it.
            # An action that states no number is "unrated" and passes — see RuleSet.confidence_band
            # for why an absent claim is not a weak one.
            band = self.rules.confidence_band(action.get("confidence"))
            if band == "watch":
                return False, (f"confidence {action['confidence']} — watch only, "
                               f"{self.rules.min_confidence} required to execute")
            if band == "below_bar":
                return False, (f"confidence {action['confidence']} — below the "
                               f"{self.rules.watch_confidence} floor, not a trade")
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
            # The price being paid, checked against the chart it was read off. Last of the hard
            # gates on purpose: a name that is already capped, locked out or unaffordable should
            # say so rather than report an entry problem it never got far enough to have.
            snap = (market or {}).get(ticker) or {}
            good, why, level = entry.check(action, snap.get("indicators"), self.rules,
                                           last_price=prices.get(ticker))
            if not good:
                at = f"; wait for {config.format_price(level)}" if level else ""
                return False, f"{why}{at}"
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

    def apply_action(self, action, prices, market=None):
        """Validate then apply. Returns (applied: bool, message: str)."""
        ok, msg = self.validate_action(action, prices, market=market)
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
        hard_stop = action.get("stop") or round_px(price * (1 + stop_pct / 100))
        # A scalp's proposed stop is clamped to the scalp band. The model routinely attaches the
        # swing-width stop it was thinking in to an idea it then labels SCALP, and an un-clamped
        # −12% stop on a position meant to live four hours is the mislabel this whole block
        # exists to stop. Wider than the band is refused; tighter than it is the model's call.
        if self.rules.is_scalp(trade_type):
            hard_stop = max(hard_stop, round_px(price * (1 + stop_pct / 100)))
        # A long-term stop is clamped the other way — a proposal *tighter* than the band is
        # widened to it. That inverts the scalp rule ("tighter is the model's call") on purpose,
        # and only here. `mark_to_market` cuts a long-term position at the band percentage, not at
        # this number, so a −7% stop attached to a two-year thesis does not shorten the ledger's
        # exit; it only reaches Alpaca as a bracket leg and sells there instead. The two would
        # then disagree, the broker would win, and the horizon band would be silently undone by
        # the one field the model still controls. Same band on both sides, or it is not a band.
        #
        # Long side only. Both clamps here read a stop as a price *below* entry, which is the
        # short position's mirror image and would put its stop through the fill the moment it
        # opened. The scalp clamp above has always carried that assumption; this one states it
        # rather than inheriting it silently.
        elif (self.rules.is_long_term(trade_type)
              and str(action.get("direction") or "LONG").upper() != "SHORT"):
            hard_stop = min(hard_stop, round_px(price * (1 + stop_pct / 100)))
        now = datetime.now(config.UTC).isoformat()
        # Reasoning snapshot taken at entry — the journal grades this against the exit.
        entry_meta = {k: action.get(k) for k in ENTRY_META_FIELDS}
        entry_meta["thesis"] = action.get("reason") or action.get("why")
        # `hard_stop`, not the raw proposal: a scalp's stop was clamped above, and the broker has
        # to hold the stop this book will actually honour rather than the one it was offered.
        mirror = self._mirror("buy", ticker, shares=shares, entry=price, stop=hard_stop,
                              target=action.get("target1") or action.get("target"))
        if kind == "ADD" and ticker in self.state["positions"]:
            pos = self.state["positions"][ticker]
            tot = pos["shares"] + shares
            pos["entry"] = round_px((pos["entry"] * pos["shares"] + price * shares) / tot, dp=4)
            pos["shares"] = tot
            pos["adds"] = (pos.get("adds") or []) + [
                {"ts": now, "shares": shares, "price": price, "broker": mirror}]
        else:
            self.state["positions"][ticker] = {
                "shares": shares, "entry": price, "opened": now,
                "sector": self.rules.sector_of(ticker), "high_water": price,
                "hard_stop": hard_stop,
                # `None` for a long-term entry — see RuleSet.trail_pct_for. The value is refreshed
                # every cycle by mark_to_market anyway; what it must not do at open is claim a
                # trailing stop the horizon does not run.
                "trail_pct": self.rules.trail_pct_for(trade_type),
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
        elif mirror and mirror.get("protected"):
            # Say the three numbers back. The point of the bracket is that the account now holds
            # the same plan the email quotes, and that is only checkable if the email says so.
            tp = mirror.get("take_profit")
            msg += (f" [Alpaca {mirror.get('order_class')}: limit "
                    f"{config.format_price(mirror.get('limit_price'))}, stop "
                    f"{config.format_price(mirror.get('stop_price'))}"
                    + (f", target {config.format_price(tp)}" if tp else "")
                    + f" · order {str(mirror.get('order_id'))[:8]} pending fill]")
            if mirror.get("note"):
                msg += f" ({mirror['note']})"
        elif mirror:
            msg += (f" [Alpaca paper market order {str(mirror.get('order_id'))[:8]} — "
                    f"no stop attached: {mirror.get('unprotected_because') or 'unknown'}]")
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
            "new_trades_by_type": self.new_trades_this_week_by_type(),
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
