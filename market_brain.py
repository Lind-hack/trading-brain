#!/usr/bin/env python3
"""Market Brain orchestrator — Signal Deck v2.

Two-model pipeline, every 30 minutes:
  tier 1  Haiku 4.5 scrapes + reads the news (cheap, runs every cycle)
  tier 2  Opus 5 makes the trade decision, but only when the screener or Haiku's read says
          something material actually happened

Modes:
  --cycle                 collect + news read + screen; deep-analyze only if something fires
  --anchor {pre,mid,close} scheduled deep run (always escalates)
  --research TICKER       on-demand deep dive into one ticker (news + fundamentals)
  --weekly-review         weekly recap: performance, every trade, accuracy changes
  --digest                Sunday: week-ahead calendar + portfolio recap
  --dry-run               do everything except email / dashboard / broker / git push
  --no-claude             skip both model tiers (deterministic fallback only)

Paper trading only. No real-money orders are ever placed: gate-approved trades are mirrored
to an Alpaca PAPER account (brain/broker.py, paper host hardcoded) and the JSON ledger stays
the accounting source of truth. Runs on Lind's Claude Code subscription (no Anthropic API
key). See CLAUDE.md for the analyst rulebook and boundaries.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta

from brain import (config, collect, screen as screener, deep, journal, memory, news_intel,
                   notify, obsidian, supabase)
from brain.portfolio import Portfolio, usable_price


def _price_lookup(market):
    out = {}
    for t, snap in market.items():
        px = snap.get("indicators", {}).get("price")
        if usable_price(px):   # a NaN quote is not a quote — see portfolio.usable_price
            out[t] = px
    return out


def _news_watchlist(market, screen_result, portfolio):
    """Which names Haiku reads this cycle: held first, then screener hits, then the focus list.

    Held names come first on purpose — news that threatens an open position matters more than
    news about a name we might buy.
    """
    ordered = list(portfolio.held_tickers())
    ordered += [t["ticker"] for t in screen_result.get("triggers", [])]
    ordered += [t for t in config.FOCUS_TICKERS if t in market]
    seen, out = set(), []
    for t in ordered:
        if t not in seen and t not in config.MARKET_CONTEXT:
            seen.add(t)
            out.append(t)
    return out[:config.NEWS_TICKERS_PER_CYCLE]


def _signal_for(analysis, ticker):
    for s in (analysis or {}).get("signals", []) or []:
        if (s.get("ticker") or "").upper() == ticker:
            return s
    return None


def _action_for(analysis, ticker):
    for a in (analysis or {}).get("portfolio_actions", []) or []:
        if (a.get("ticker") or "").upper() == ticker:
            return a
    return None


def record_trades(portfolio, exits, opened, analysis, intel, now_et, prices, persist):
    """Journal + push every fill and exit the moment it happens.

    This is what makes trades appear live on signal-deck and land in the Obsidian journal in
    the same pass that took them, rather than at the next snapshot.
    """
    if not persist or not (exits or opened):
        return
    equity = portfolio.equity(prices)
    intel_tickers = (intel or {}).get("tickers") or {}
    for t in opened:
        pos = portfolio.state["positions"].get(t)
        if not pos:
            continue
        sig = _signal_for(analysis, t)
        act = _action_for(analysis, t)
        journal.open_trade(t, pos, signal=sig, action=act, now_et=now_et,
                           intel_for_ticker=intel_tickers.get(t))
        supabase.push_trade_open(t, pos, signal=sig, action=act, equity=equity)
    for e in exits or []:
        journal.close_trade(e, now_et)
        supabase.push_trade_close(e, equity=equity)


def _emit(analysis, screen_result, psum, mode, now_et, escalated, exits, applied, args,
          intel=None):
    """Fan out to email + Obsidian + dashboard + git memory (respecting --dry-run)."""
    has_output = bool((analysis or {}).get("signals")) or exits or applied
    # Obsidian: every run writes (Lind wants all data in the second brain)
    if not args.dry_run:
        obsidian.write_run(analysis, screen_result, psum, mode, now_et, escalated)
        if exits or applied:
            obsidian.log_trades(exits, applied, now_et)
        supabase.push(analysis, screen_result, psum, mode, now_et, escalated)

    # Email: only when there's something to say (or a forced anchor with signals)
    if analysis and (analysis.get("signals") or applied or mode in ("weekly", "digest")):
        subject, text, html = notify.build_email(analysis, psum, mode, now_et, intel=intel,
                                                 applied=applied, exits=exits)
        if args.dry_run:
            print(f"[dry-run] would email: {subject}")
            print(text[:1500])
        else:
            notify.send_email(subject, text, html)
    elif exits and not args.dry_run:
        # A mechanical stop fired with no new analysis — that still needs telling.
        subject, text, html = notify.build_email(
            {"market_outlook": "Position exited by a mechanical stop. No new analysis this run.",
             "signals": []}, psum, mode, now_et, intel=intel, applied=applied, exits=exits)
        notify.send_email(subject, text, html)
    elif not has_output:
        print(f"[{mode}] quiet — nothing to email (silence means no setup).")

    if not args.dry_run:
        memory.commit_memory(f"brain {mode} run {now_et.strftime('%Y-%m-%d %H:%M ET')}")


def run_analysis(mode, market, screen_result, calendar, portfolio, args, focus=None,
                 with_fundamentals=True, intel=None):
    """Build the packet, run the deep model (or fallback), and return the analysis dict."""
    psum = portfolio.summary(_price_lookup(market))
    focus = focus or [t["ticker"] for t in screen_result.get("triggers", [])][:8]
    # Names Haiku flagged as materially newsworthy deserve tier-2 eyes even if the
    # deterministic screener never touched them — that is the point of the news layer.
    for t, info in ((intel or {}).get("tickers") or {}).items():
        if info.get("materiality") == "high" and t not in focus:
            focus.append(t)
    if not focus and mode in ("pre", "mid", "close"):
        # anchor with no screener hits: analyze the most active context names anyway
        focus = config.FOCUS_TICKERS[:6]
    focus = focus[:10]

    analogs = memory.analogs_for(market, calendar)
    fundamentals = {}
    if with_fundamentals:
        for t in focus[:6]:
            fundamentals[t] = collect.finnhub_fundamentals(t)

    packet = deep.build_packet(mode, market, screen_result, calendar, psum,
                               analogs=analogs, fundamentals=fundamentals, focus=focus,
                               news_intel=intel, strategy_notes=memory.read_strategy())
    if args.no_claude:
        return deep.fallback_analysis(screen_result, market, news_intel=intel)
    try:
        prompt = deep.build_prompt(mode if mode in ("pre", "mid", "close") else "cycle", packet)
        return deep.run_claude(prompt, model=config.CLAUDE_DEEP_MODEL)
    except Exception as e:
        print(f"[warn] deep run failed, using fallback: {e}", file=sys.stderr)
        return deep.fallback_analysis(screen_result, market, news_intel=intel)


def mark_exits(portfolio, prices, persist=True):
    """Mechanical stops, run once per cycle before anything else looks at the book.

    Deliberately separate from apply_actions(): mark_to_market appends an equity-curve point,
    so running it twice in one cycle would double-count the curve.
    """
    exits = portfolio.mark_to_market(prices)
    if persist:
        portfolio.save()
    return exits


def apply_actions(analysis, portfolio, prices, persist=True):
    """Run the model's proposed actions through the gates.

    Returns (applied, opened) — `opened` is the tickers that actually got a fill, so the caller
    can journal them with the reasoning that justified each one.
    """
    applied, opened = [], []
    before = set(portfolio.state["positions"])
    for action in (analysis or {}).get("portfolio_actions", []) or []:
        ticker = (action.get("ticker") or "").upper()
        # Carry the signal's reasoning onto the action so the position stores why it was
        # opened; the journal grades that reasoning against the outcome on exit.
        sig = _signal_for(analysis, ticker)
        if sig:
            action.setdefault("trade_type", sig.get("trade_type"))
            action.setdefault("confidence", sig.get("confidence"))
            action.setdefault("indicators_used", sig.get("indicators_used"))
            action.setdefault("news_at_entry", sig.get("news"))
            action.setdefault("why", sig.get("why"))
        ok, msg = portfolio.apply_action(action, prices)
        tag = "" if ok else "REJECTED: "
        line = f"{tag}{action.get('action','')} {ticker} — {msg}"
        applied.append(line)
        print(f"[portfolio] {line}")
    opened = [t for t in portfolio.state["positions"] if t not in before]
    if persist:
        portfolio.save()
    return applied, opened


def do_cycle(args, mode="cycle", force=False):
    now_et = datetime.now(config.ET)
    persist = not args.dry_run
    # Mirror to the paper broker only on a real run; --dry-run stays entirely local.
    portfolio = Portfolio(mirror=persist)
    with_news = force or mode in ("pre", "mid", "close")
    market = collect.collect_market(with_news=with_news)
    prices = _price_lookup(market)
    calendar = collect.forexfactory_calendar()

    # memory: backfill forward returns, then log this cycle's events (skip disk writes on dry-run)
    if persist:
        memory.backfill_returns(prices)
        logged = memory.log_events(market, calendar, spy_price=prices.get("SPY"))
        print(f"[memory] logged {logged} event(s)")

    screen_result = screener.screen(market, calendar=calendar,
                                    held_tickers=portfolio.held_tickers(), force=force)

    # ── tier 1: Haiku reads the news every cycle, whether or not the screener fired ──
    watchlist = _news_watchlist(market, screen_result, portfolio)
    intel = news_intel.run(watchlist, calendar=calendar, use_claude=not args.no_claude)
    news_reasons = news_intel.escalation_reasons(intel)
    if news_reasons:
        screen_result["why"] = list(screen_result.get("why", [])) + news_reasons

    escalated = bool(screen_result["escalate"] or news_reasons)
    print(f"[screen] escalate={escalated} :: {'; '.join(screen_result['why']) or 'quiet'}")

    analysis = None
    applied, opened = [], []
    exits = mark_exits(portfolio, prices, persist=persist)
    if escalated:
        # ── tier 2: Opus decides the trade, reasoning over Haiku's read ──
        analysis = run_analysis(mode, market, screen_result, calendar, portfolio, args,
                                intel=intel)
        applied, opened = apply_actions(analysis, portfolio, prices, persist=persist)

    record_trades(portfolio, exits, opened, analysis, intel, now_et, prices, persist)
    psum = portfolio.summary(prices)
    _emit(analysis, screen_result, psum, mode, now_et, escalated, exits, applied, args,
          intel=intel)
    return 0


def do_research(args):
    ticker = args.research.upper()
    now_et = datetime.now(config.ET)
    persist = not args.dry_run
    portfolio = Portfolio(mirror=persist and not args.no_apply)
    snap = collect.collect_ticker(ticker, with_news=True, with_fundamentals=True)
    market = {ticker: snap}
    for c in config.MARKET_CONTEXT:
        try:
            market[c] = collect.collect_ticker(c)
        except Exception:
            pass
    calendar = collect.forexfactory_calendar()
    prices = _price_lookup(market)
    screen_result = {"escalate": True, "why": [f"on-demand research: {ticker}"],
                     "triggers": [{"ticker": ticker, "score": 99,
                                   "reasons": [p["name"] for p in snap.get("patterns", [])]}],
                     "calendar_flags": []}
    intel = news_intel.run([ticker], calendar=calendar, use_claude=not args.no_claude)
    analysis = run_analysis("research", market, screen_result, calendar, portfolio, args,
                            focus=[ticker], intel=intel)
    exits, applied, opened = [], [], []
    if not args.no_apply:
        exits = mark_exits(portfolio, prices, persist=persist)
        applied, opened = apply_actions(analysis, portfolio, prices, persist=persist)
    record_trades(portfolio, exits, opened, analysis, intel, now_et, prices, persist)
    psum = portfolio.summary(prices)
    _emit(analysis, screen_result, psum, "research", now_et, True, exits, applied, args,
          intel=intel)
    return 0


def do_weekly_review(args):
    """The weekly recap: how the trading week went + what to change to be more accurate.

    Deterministic arithmetic (journal.build_recap) does the scoreboard and the rule-based
    per-trade gradings; the model only writes the narrative and the proposed changes. Those
    changes are appended to brain-memory/STRATEGY.md, which every later deep run reads — that
    feedback loop is the point of the whole exercise.
    """
    now_et = datetime.now(config.ET)
    persist = not args.dry_run
    portfolio = Portfolio(mirror=False)   # a review never trades and never mutates the ledger

    # Fresh prices so the week's equity figure is honest. Read-only: summary(prices) values the
    # book without touching it, and no stop is evaluated here — that is the cycle's job.
    prices = {}
    for t in portfolio.held_tickers():
        try:
            px = collect.collect_ticker(t)["indicators"]["price"]
            if usable_price(px):
                prices[t] = px
        except Exception as e:
            print(f"[warn] weekly review price for {t}: {e}", file=sys.stderr)

    week_start = (datetime.now(config.UTC) - timedelta(days=7)).isoformat()
    recap = journal.build_recap(portfolio, week_start, prices=prices,
                                week_label=f"{(now_et - timedelta(days=7)).strftime('%b %d')}"
                                           f" – {now_et.strftime('%b %d, %Y')}")

    if not args.no_claude:
        try:
            review = deep.run_claude(deep.build_review_prompt(journal.recap_packet(recap)),
                                     model=config.CLAUDE_DEEP_MODEL)
            recap["narrative"] = review.get("narrative") or review.get("week_summary")
            recap["changes"] = [str(c) for c in (review.get("changes") or [])][:8]
            recap["mistakes"] = [str(m) for m in (review.get("mistakes") or [])][:8]
            recap["successes"] = [str(s) for s in (review.get("successes") or [])][:8]
        except Exception as e:
            print(f"[warn] weekly review deep run failed: {e}", file=sys.stderr)
    if not recap.get("narrative"):
        st = recap["stats"]
        recap["narrative"] = (
            f"AI review unavailable — numbers only. The week moved equity from "
            f"${st['equity_start']:,.2f} to ${st['equity_end']:,.2f} "
            f"({st['week_return_pct']:+.2f}%) across {st['n_closed']} closed trade(s).")

    subject, text, html = notify.build_weekly_email(recap, now_et)
    if persist:
        # The recap note supersedes obsidian.write_weekly_review() — it carries every trade,
        # the gradings, and the changes, not just the narrative.
        journal.write_weekly_recap(recap, now_et)
        memory.append_strategy(recap.get("changes"), now_et, stats=recap["stats"])
        notify.send_email(subject, text, html)
        supabase.push({"market_outlook": recap["narrative"], "signals": []},
                      {"why": ["weekly recap"]}, portfolio.summary(prices), "weekly",
                      now_et, False)
        memory.commit_memory(f"weekly recap {now_et.strftime('%Y-%m-%d')}")
    else:
        print(f"[dry-run] would email: {subject}\n{text[:2500]}")
    return 0


def do_digest(args):
    now_et = datetime.now(config.ET)
    portfolio = Portfolio(mirror=False)   # read-only run
    calendar = collect.forexfactory_calendar(include_next_week=True)
    psum = portfolio.summary()
    # Only what's still ahead — the feed's week includes days that already closed.
    def _ahead(e):
        return (e.get("hours_away") or 0) > 0

    high = [e for e in calendar.get("events", [])
            if e.get("impact", "").lower() == "high" and _ahead(e)][:15]
    speeches = [s for s in calendar.get("speeches", []) if _ahead(s)][:8]
    lines = [f"# Week-ahead digest — {now_et.strftime('%Y-%m-%d')}", "",
             f"Paper portfolio: ${psum['equity']:,.0f} ({psum['total_return_pct']:+.1f}%), "
             f"{psum['n_open']} open, win rate {psum['win_rate']}%.", "",
             "## High-impact events this/next week"]
    for e in high:
        lines.append(f"- {e['when']} — {e['title']} ({e['country']}) [{e['impact']}]")
    if not high:
        lines.append("- none remaining in the feed's current week")
    if speeches:
        lines.append("\n## Speeches to watch")
        for s in speeches:
            lines.append(f"- {s['when']} — {s['title']}")
    lines.append(f"\n{config.DISCLAIMER}")
    text = "\n".join(lines)
    if not args.dry_run:
        obsidian.write_weekly_review(text, now_et)  # reuse dated-note writer
        html = f"<pre style='font-family:monospace;color:#e5e7eb;background:#0a0a0a;padding:20px;white-space:pre-wrap;'>{text}</pre>"
        notify.send_email(f"Market Brain: week-ahead digest — {now_et.strftime('%b %d')}", text, html)
        memory.commit_memory(f"weekend digest {now_et.strftime('%Y-%m-%d')}")
    else:
        print(f"[dry-run] digest:\n{text}")
    return 0


def main():
    ap = argparse.ArgumentParser(description="Market Brain — paper-trading analysis engine")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--cycle", action="store_true", help="collect+screen, deep-analyze only if flagged")
    g.add_argument("--anchor", choices=["pre", "mid", "close"], help="scheduled deep run")
    g.add_argument("--research", metavar="TICKER", help="on-demand deep dive into one ticker")
    g.add_argument("--weekly-review", action="store_true", dest="weekly_review")
    g.add_argument("--digest", action="store_true", help="weekend week-ahead digest")
    ap.add_argument("--dry-run", action="store_true", help="no email / dashboard / git push")
    ap.add_argument("--no-claude", action="store_true", help="skip the CLI deep run (fallback only)")
    ap.add_argument("--no-apply", action="store_true", help="research: don't touch the paper portfolio")
    args = ap.parse_args()

    if args.cycle:
        return do_cycle(args, mode="cycle", force=False)
    if args.anchor:
        return do_cycle(args, mode=args.anchor, force=True)
    if args.research:
        return do_research(args)
    if args.weekly_review:
        return do_weekly_review(args)
    if args.digest:
        return do_digest(args)
    ap.error("no mode selected")


if __name__ == "__main__":
    sys.exit(main())
