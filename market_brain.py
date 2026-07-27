#!/usr/bin/env python3
"""Market Brain orchestrator — Signal Deck v2.

Two-model pipeline, every 30 minutes:
  tier 1  Haiku 4.5 scrapes + reads the news (cheap, runs every cycle)
  tier 2  Opus 5 makes the trade decision, but only when the screener or Haiku's read says
          something material actually happened

Modes:
  --cycle                 collect + news read + screen; deep-analyze only if something fires
  --crypto-cycle          the same, for the 24/7 crypto book (separate ledger, own gate)
  --anchor {pre,mid,close} scheduled deep run (always escalates)
  --research TICKER       on-demand deep dive into one ticker (news + fundamentals)
  --weekly-review         weekly recap: performance, every trade, every signal, accuracy changes
  --digest                on-demand week-ahead calendar (no longer scheduled — see below)
  --dry-run               do everything except email / dashboard / broker / git push
  --no-claude             skip both model tiers (deterministic fallback only)
  --ignore-market-hours   bypass the session gate (manual backfills only)

Every *equity* mode passes through brain/market_hours.gate() before it does anything. The market is
the schedule: no weekends, no holidays, nothing outside the session, and the close and weekly runs
wait for the real close (13:00 ET on a half-day). Cron only proposes a time. The Sunday digest
is gone — its week-ahead calendar is now a section of the Friday recap.

`--crypto-cycle` is the exception, and deliberately so: its venue has no bell, so it routes around
the equity gate entirely and uses market_hours.crypto_gate() from inside do_crypto_cycle. Putting
it through the session gate would stop it at 16:00 ET and all weekend — which is most of the tape
it exists to trade, and would leave a 24-hour scalp clock with nothing running to expire it.

Paper trading only. No real-money orders are ever placed: gate-approved trades are mirrored
to an Alpaca PAPER account (brain/broker.py, paper host hardcoded) and the JSON ledger stays
the accounting source of truth. Runs on Lind's Claude Code subscription (no Anthropic API
key). See CLAUDE.md for the analyst rulebook and boundaries.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta

from brain import (config, collect, dedupe, screen as screener, deep, journal, market_hours, memory,
                   runlog, news_intel, notify, obsidian, supabase, thesis)
from brain.portfolio import ENTRY_META_FIELDS, Portfolio, usable_price


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

    The third group is the only way a *news-first* opportunity is ever found: a name whose chart
    is saying nothing, so the screener never flags it, but which just filed an 8-K. Straight
    priority order starves that group exactly when it matters most — a full book plus a busy tape
    fills every place with held and triggered names, so the busier the day, the blinder the brain
    is to news alone. That is backwards, so a few places are reserved for it.

    The reserve is a floor, not a quota: if the focus list has nothing left to offer, or if held
    names alone fill the cycle, the places go back to the screener's triggers.
    """
    limit = config.NEWS_TICKERS_PER_CYCLE

    def _dedup(seq, seen):
        out = []
        for t in seq:
            if t and t not in seen and t not in config.MARKET_CONTEXT:
                seen.add(t)
                out.append(t)
        return out

    seen = set()
    held = _dedup(portfolio.held_tickers(), seen)
    # Triggers arrive score-sorted, so a truncation here drops the weakest setups, not random ones.
    triggered = _dedup((t.get("ticker") for t in screen_result.get("triggers", [])), seen)
    quiet = _dedup((t for t in config.FOCUS_TICKERS if t in market), seen)

    out = held[:limit]
    room = limit - len(out)
    reserved = min(config.NEWS_DISCOVERY_RESERVE, len(quiet), room)
    out += triggered[:room - reserved]
    out += quiet[:limit - len(out)]
    # Whatever the reserve did not need goes back to the triggers rather than going unused.
    out += [t for t in triggered if t not in out][:limit - len(out)]
    return out[:limit]


def _crypto_news_watchlist(screen_result, portfolio):
    """Which tokens Haiku reads on a crypto cycle.

    Far simpler than the equity version, and deliberately so: that one rations a ~50-name sweep
    down to a handful and reserves places so a news-first idea is not crowded out by the screener.
    Here the universe is four tokens and the cycle budget is larger than that, so there is nothing
    to ration — every token gets read every cycle. Held names lead only so the ordering matches the
    equity path for anyone reading both logs.
    """
    seen, out = set(), []
    for t in list(portfolio.held_tickers()) + \
            [x.get("ticker") for x in screen_result.get("triggers", [])] + \
            list(config.CRYPTO_TICKERS):
        # `is_crypto` rather than a MARKET_CONTEXT exclusion list: DXY and the VIX ride along in
        # the crypto packet as context and there is no headline sweep to run on either of them.
        if t and t not in seen and config.is_crypto(t):
            seen.add(t)
            out.append(t)
    return out


def _attach_news(market, intel, limit=4):
    """Hang tier 1's scored headlines on the snapshots so `focus[*].news` still has content.

    The deep packet reads news off the market snapshot. Those headlines now come from the pass
    that already scraped and scored them rather than from a second, unscored scrape — so what
    reaches the analyst carries `outlets`, `crowding` and `source_tier`, which the old anchor
    fetch never had.
    """
    for ticker, items in ((intel or {}).get("headlines") or {}).items():
        snap = market.get(ticker)
        if isinstance(snap, dict) and items:
            snap["news"] = items[:limit]
    return market


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


def _feed_theses(intel, persist=True):
    """File this cycle's material news against any long-horizon thesis that names the ticker.

    Gathering evidence is continuous; scoring it is not. A cycle may only append — conviction
    moves in the Monday and Friday reviews, so a bad afternoon cannot talk the brain out of a
    three-year argument. See brain/thesis.py.
    """
    board = thesis.load()
    if not thesis.active(board):
        return 0
    headlines = (intel or {}).get("headlines") or {}
    filed = 0
    for ticker, info in ((intel or {}).get("tickers") or {}).items():
        if (info.get("materiality") or "").lower() not in ("high", "medium"):
            continue
        items = [{"note": h.get("title"), "source": h.get("source"), "link": h.get("link"),
                  "stance": "supports" if (info.get("sentiment") or 0) >= 0 else "undermines"}
                 for h in (headlines.get(ticker) or [])[:2] if h.get("title")]
        if not items:
            continue
        filed += thesis.attach_evidence(ticker, items, board=board, save_board=False)
    if filed and persist:
        thesis.save(board)
        print(f"[thesis] filed {filed} piece(s) of evidence against the board")
    return filed


def _hold_new_entries(analysis):
    """Strip BUY/ADD proposals when the session is shut. Returns how many were held.

    Lind's rule: signals from the open to the close, then nothing new until the next open. An
    equity order sent to Alpaca outside the session does not fail — it sits `accepted` and
    queues for the next open, which means a stale idea would fill hours later at a price nobody
    analysed. Exits are deliberately left alone: risk management has no closing bell.

    The signals themselves survive, so the analysis still reaches the journal and the dashboard
    as a *recommendation*. Only the execution is withheld.
    """
    actions = (analysis or {}).get("portfolio_actions") or []
    if not actions:
        return 0
    keep = [a for a in actions if (a.get("action") or "").upper() not in ("BUY", "ADD")]
    held = len(actions) - len(keep)
    if held:
        analysis["portfolio_actions"] = keep
        analysis["entries_held"] = held
        for sig in (analysis.get("signals") or []):
            sig.setdefault("execution", "recommended-only: session shut, no new entries")
    return held


def record_trades(portfolio, exits, opened, analysis, intel, now_et, prices, persist):
    """Journal + push every fill and exit the moment it happens.

    This is what makes trades appear live on signal-deck and land in the Obsidian journal in
    the same pass that took them, rather than at the next snapshot.

    `exits` must be portfolio.exits_this_run, not the list mark_exits() returned: that one holds
    only the mechanical stops, so a model-proposed SELL reached neither the trade tape nor the
    exit grading. Every close of any kind registers in exits_this_run.
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
                               news_intel=intel, strategy_notes=memory.read_strategy(),
                               theses=thesis.for_packet())
    if args.no_claude:
        return thesis.annotate_signals(
            deep.fallback_analysis(screen_result, market, news_intel=intel))
    try:
        prompt = deep.build_prompt(
            mode if mode in ("pre", "mid", "close", "crypto") else "cycle", packet)
        # Bind each LONG_TERM call to the board entry it claims to rest on — an unanchored one
        # is a momentum trade wearing an investment label, and the email says so.
        return thesis.annotate_signals(deep.run_claude(prompt, model=config.CLAUDE_DEEP_MODEL))
    except Exception as e:
        print(f"[warn] deep run failed, using fallback: {e}", file=sys.stderr)
        return thesis.annotate_signals(
            deep.fallback_analysis(screen_result, market, news_intel=intel))


def mark_exits(portfolio, prices, persist=True):
    """Mechanical stops, run once per cycle before anything else looks at the book.

    Deliberately separate from apply_actions(): mark_to_market appends an equity-curve point,
    so running it twice in one cycle would double-count the curve.
    """
    exits = portfolio.mark_to_market(prices)
    if persist:
        portfolio.save()
    return exits


def apply_actions(analysis, portfolio, prices, persist=True, mode="cycle"):
    """Run the model's proposed actions through the gates.

    Returns (applied, opened) — `opened` is the tickers that actually got a fill, so the caller
    can journal them with the reasoning that justified each one.

    Every proposal is written to the signal ledger with its gate verdict, whether it filled or
    not. Rejections used to be a printed line and nothing else, so "what did you recommend but
    not take, and why" had no answer anywhere in the system.
    """
    applied, opened, outcomes = [], [], {}
    before = set(portfolio.state["positions"])
    for action in (analysis or {}).get("portfolio_actions", []) or []:
        ticker = (action.get("ticker") or "").upper()
        # Carry the signal's reasoning onto the action so the position stores why it was
        # opened; the journal grades that reasoning against the outcome on exit. Copied
        # wholesale — see portfolio.ENTRY_META_FIELDS for why picking a subset here was a bug.
        sig = _signal_for(analysis, ticker)
        if sig:
            for k in ENTRY_META_FIELDS:
                if action.get(k) is None and sig.get(k) is not None:
                    action[k] = sig[k]
            # The signal calls it `news`; the position stores it as `news_at_entry`.
            if action.get("news_at_entry") is None:
                action["news_at_entry"] = sig.get("news")
            if action.get("why") is None:
                action["why"] = sig.get("why")
        ok, msg = portfolio.apply_action(action, prices)
        outcomes[ticker] = {"action": action.get("action"), "ok": ok, "msg": msg}
        tag = "" if ok else "REJECTED: "
        line = f"{tag}{action.get('action','')} {ticker} — {msg}"
        applied.append(line)
        print(f"[portfolio] {line}")
    opened = [t for t in portfolio.state["positions"] if t not in before]
    _stamp_outcomes(analysis, outcomes)
    if persist:
        portfolio.save()
        n = memory.log_signals(analysis, outcomes, mode, prices=prices)
        if n:
            print(f"[memory] logged {n} signal(s) to the ledger")
    return applied, opened


def _stamp_outcomes(analysis, outcomes):
    """Write the gate's verdict back onto each signal.

    memory.log_signals() works this out for the ledger, but the dashboard reads the signal dict
    itself, and without the verdict on it every idea rendered identically — a trade the brain
    actually took looked exactly like one the gates threw out. That is the confusion Lind
    reported on signal-deck, and it was a missing field rather than a missing view.
    """
    for sig in ((analysis or {}).get("signals") or []):
        res = (outcomes or {}).get((sig.get("ticker") or "").upper()) or {}
        if not res:
            sig["outcome"], sig["gate_reason"] = "advisory", "no portfolio action proposed"
        else:
            sig["outcome"] = "executed" if res.get("ok") else "rejected"
            sig["gate_reason"] = res.get("msg") or ""
        sig["proposed_action"] = (res.get("action") or "").upper() or None
    return analysis


def do_cycle(args, mode="cycle", force=False):
    now_et = datetime.now(config.ET)
    persist = not args.dry_run
    # Mirror to the paper broker only on a real run; --dry-run stays entirely local.
    portfolio = Portfolio(mirror=persist)
    # No news in the market sweep. It used to scrape every one of the ~50 snapshot tickers on
    # anchor cycles, and then news_intel scraped the watchlist again seconds later from the same
    # two sources — twice the calls for a strictly worse result, since only the second pass runs
    # the quality gate. The watchlist's scored headlines are attached below instead.
    market = collect.collect_market(with_news=False)
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
    _attach_news(market, intel)
    news_reasons = news_intel.escalation_reasons(intel)
    if news_reasons:
        screen_result["why"] = list(screen_result.get("why", [])) + news_reasons
    _feed_theses(intel, persist=persist)

    escalated = bool(screen_result["escalate"] or news_reasons)
    print(f"[screen] escalate={escalated} :: {'; '.join(screen_result['why']) or 'quiet'}")

    analysis = None
    applied, opened = [], []
    exits = mark_exits(portfolio, prices, persist=persist)
    if escalated:
        # ── tier 2: Opus decides the trade, reasoning over Haiku's read ──
        analysis = run_analysis(mode, market, screen_result, calendar, portfolio, args,
                                intel=intel)
        # A persistent chart condition produces a persistent conclusion. Drop the ideas already
        # sent before they reach either the inbox or the gates — see brain/dedupe.py.
        # An idea only stays suppressed while it stays hypothetical: if the gates would fill it
        # now, the repeat is the first time Lind is told the trade was actually taken.
        can_execute = None
        if market_hours.entries_allowed(now_et):
            can_execute = lambda a: portfolio.validate_action(a, prices)[0]  # noqa: E731
        suppressed = dedupe.apply(analysis, can_execute=can_execute)
        if suppressed:
            print(f"[dedupe] {dedupe.summarize(suppressed)}")
            if persist:
                memory.log_suppressed(suppressed, mode, prices=prices)
        # New entries are a session-hours privilege. Outside the session the run still manages
        # risk, still reads the news, still journals — it just stops proposing ways in.
        if not market_hours.entries_allowed(now_et):
            held = _hold_new_entries(analysis)
            if held:
                print(f"[gate] {held} new-entry proposal(s) held: "
                      f"{market_hours.entries_reason(now_et)}")
        applied, opened = apply_actions(analysis, portfolio, prices, persist=persist, mode=mode)

    record_trades(portfolio, portfolio.exits_this_run, opened, analysis, intel, now_et,
                  prices, persist)
    psum = portfolio.summary(prices)
    _emit(analysis, screen_result, psum, mode, now_et, escalated, exits, applied, args,
          intel=intel)
    return 0


def do_crypto_cycle(args, force=False):
    """The crypto book's cycle — the same pipeline, a different venue, a different account.

    Written as its own function rather than a `venue=` flag through do_cycle() because almost
    every line differs in a way a flag would have to branch on anyway: a different portfolio, a
    different collector, a different watchlist, no fundamentals, no broker mirror, no session
    gate. A flag would read as "mostly the same" and the one thing that must never happen here is
    a crypto run silently picking up an equity default.

    Three deliberate omissions, each with a reason:
      * No broker mirror. Alpaca paper does not list SUI at all and spells the others `BTC/USD`,
        so a mirror attempt would half-succeed — two of four tokens filled — and the ledger would
        stop matching the paper account. The JSON ledger is the whole truth for this book.
      * No calendar rows into the events memory. CPI is one event; the equity cycle already files
        it. See memory.log_events(with_calendar=...).
      * No fundamentals. There are none.
    """
    now_et = datetime.now(config.ET)
    persist = not args.dry_run
    portfolio = Portfolio(mirror=False, rules=config.CRYPTO_RULES)
    market = collect.collect_crypto_market(with_news=False)
    prices = _price_lookup(market)
    # The macro calendar still matters — CPI and the FOMC move the dollar, and the dollar moves
    # this book — so it is read into the packet. It is only the *memory* write that is skipped.
    calendar = collect.forexfactory_calendar()

    if persist:
        memory.backfill_returns(prices)
        logged = memory.log_events(market, calendar, with_calendar=False)
        print(f"[memory] logged {logged} crypto event(s)")

    screen_result = screener.screen(market, calendar=calendar,
                                    held_tickers=portfolio.held_tickers(), force=force)

    watchlist = _crypto_news_watchlist(screen_result, portfolio)
    intel = news_intel.run(watchlist, calendar=calendar, use_claude=not args.no_claude,
                           venue="crypto")
    _attach_news(market, intel)
    news_reasons = news_intel.escalation_reasons(intel)
    if news_reasons:
        screen_result["why"] = list(screen_result.get("why", [])) + news_reasons

    escalated = bool(screen_result["escalate"] or news_reasons)
    allowed, why = market_hours.crypto_gate("cycle", now_et)
    print(f"[crypto] {market_hours.crypto_describe(now_et)} — {why}")
    print(f"[screen] escalate={escalated} :: {'; '.join(screen_result['why']) or 'quiet'}")

    analysis = None
    applied, opened = [], []
    # Mechanical exits first, every cycle, escalation or not. This is where the scalp time stop
    # fires, and it is the reason the crypto cron has to run through the night: a 24-hour clock
    # started at 03:00 UTC expires at 03:00 UTC, and nothing else in this system will close it.
    exits = mark_exits(portfolio, prices, persist=persist)
    if escalated and allowed:
        analysis = run_analysis("crypto", market, screen_result, calendar, portfolio, args,
                                focus=[t for t in config.CRYPTO_TICKERS if t in market],
                                with_fundamentals=False, intel=intel)
        # Entries are always allowed on this venue, so the suppression check has a live executor
        # on every cycle — unlike the equity path, where an out-of-session repeat stays hypothetical.
        suppressed = dedupe.apply(
            analysis, can_execute=lambda a: portfolio.validate_action(a, prices)[0])
        if suppressed:
            print(f"[dedupe] {dedupe.summarize(suppressed)}")
            if persist:
                memory.log_suppressed(suppressed, "crypto", prices=prices)
        applied, opened = apply_actions(analysis, portfolio, prices, persist=persist,
                                        mode="crypto")

    record_trades(portfolio, portfolio.exits_this_run, opened, analysis, intel, now_et,
                  prices, persist)
    psum = portfolio.summary(prices)
    _emit(analysis, screen_result, psum, "crypto", now_et, escalated, exits, applied, args,
          intel=intel)
    return 0


def do_research(args):
    ticker = args.research.upper()
    now_et = datetime.now(config.ET)
    persist = not args.dry_run
    portfolio = Portfolio(mirror=persist and not args.no_apply)
    # News comes from the tier-1 pass below, which scrapes the same sources through the quality
    # gate — asking for it here too would scrape this one name twice.
    snap = collect.collect_ticker(ticker, with_news=False, with_fundamentals=True)
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
    _attach_news(market, intel)
    analysis = run_analysis("research", market, screen_result, calendar, portfolio, args,
                            focus=[ticker], intel=intel)
    exits, applied, opened = [], [], []
    if not args.no_apply:
        exits = mark_exits(portfolio, prices, persist=persist)
        applied, opened = apply_actions(analysis, portfolio, prices, persist=persist,
                                        mode="research")
    record_trades(portfolio, portfolio.exits_this_run, opened, analysis, intel, now_et,
                  prices, persist)
    psum = portfolio.summary(prices)
    _emit(analysis, screen_result, psum, "research", now_et, True, exits, applied, args,
          intel=intel)
    return 0


def run_thesis_review(args, calendar=None, portfolio_summary=None, tickers=None):
    """Re-score the long-horizon board. Runs twice a week — Monday pre-open, Friday post-close.

    Everything else in the pipeline answers "what should I do in the next few days". This answers
    "where is the world going over the next 1-3 years, and which listed companies are on the
    receiving end of it" — Lind's RAM example, generalised.

    Cheap enough to run twice weekly and never on a cycle: one Haiku news pass over the board's
    own names plus the focus list, the fundamentals already cached, and one deep run.
    """
    board = thesis.load()
    watch = []
    for t in thesis.active(board):
        watch += (t.get("tickers") or [])
    # Names already on the board first — the review's first duty is to re-examine what it holds,
    # not to go shopping. The focus list fills the rest so new themes can still surface.
    for t in config.FOCUS_TICKERS:
        if t not in watch:
            watch.append(t)
    seen, watchlist = set(), []
    for t in (tickers or watch):
        if t not in seen and t not in config.MARKET_CONTEXT:
            seen.add(t)
            watchlist.append(t)
    watchlist = watchlist[:config.NEWS_TICKERS_PER_CYCLE]

    intel = news_intel.run(watchlist, calendar=calendar, use_claude=not args.no_claude)
    fundamentals = {}
    for t in watchlist[:8]:
        try:
            fundamentals[t] = collect.finnhub_fundamentals(t)
        except Exception as e:
            print(f"[warn] thesis fundamentals {t}: {e}", file=sys.stderr)

    context = {}
    for t in config.MARKET_CONTEXT:
        try:
            context[t] = collect.collect_ticker(t).get("indicators", {})
        except Exception:
            pass

    packet = {
        "as_of": datetime.now(config.ET).strftime("%Y-%m-%d %H:%M ET"),
        "board": board.get("theses", []),
        "market_context": context,
        "fundamentals": fundamentals,
        "news_intel": {
            "model": intel.get("model"), "degraded": bool(intel.get("degraded")),
            "macro_read": intel.get("macro_read"),
            "tickers": intel.get("tickers") or {},
            "top_stories": intel.get("top_stories") or [],
        },
        "calendar": {"high_impact_week": [e for e in (calendar or {}).get("events", [])
                                          if (e.get("impact") or "").lower() == "high"][:12]},
        "portfolio": portfolio_summary or {},
        "rules": {
            "horizon_years": list(config.THESIS_HORIZON_YEARS),
            "max_active": config.THESIS_MAX_ACTIVE,
            "min_conviction_to_stay_active": config.THESIS_MIN_CONVICTION,
            "stale_after_days": config.THESIS_STALE_DAYS,
        },
    }

    if args.no_claude:
        print("[thesis] --no-claude: board carried forward unchanged")
        thesis.flag_stale(board)
        thesis.save(board)
        return board, {"added": 0, "updated": 0, "closed": 0, "regime": None}

    try:
        out = deep.run_claude(deep.build_thesis_prompt(deep.dumps(packet, indent=2)),
                              model=config.CLAUDE_DEEP_MODEL)
    except Exception as e:
        print(f"[warn] thesis review deep run failed: {e}", file=sys.stderr)
        thesis.flag_stale(board)
        thesis.save(board)
        return board, {"added": 0, "updated": 0, "closed": 0, "regime": None}

    board, added, updated, closed = thesis.merge(out.get("theses"), board=board,
                                                 save_board=not args.dry_run)
    print(f"[thesis] board: +{added} new, {updated} updated, {closed} closed")
    return board, {"added": added, "updated": updated, "closed": closed,
                   "regime": out.get("market_regime")}


def _week_ahead(limit_events=12, limit_speeches=6):
    """Next week's high-impact calendar, folded into the Friday recap.

    This used to be its own Sunday `--digest` run. The market-hours gate stops the pipeline at
    the weekly close, so the week-ahead now rides along with the recap that already fires there —
    one email at the moment Lind actually reads it, instead of two on days the market is shut.
    """
    try:
        cal = collect.forexfactory_calendar(include_next_week=True)
    except Exception as e:
        print(f"[warn] week-ahead calendar: {e}", file=sys.stderr)
        return {}

    def _ahead(e):
        return (e.get("hours_away") or 0) > 0

    return {
        "events": [{k: e.get(k) for k in ("when", "title", "country", "impact")}
                   for e in cal.get("events", [])
                   if (e.get("impact") or "").lower() == "high" and _ahead(e)][:limit_events],
        "speeches": [{k: s.get(k) for k in ("when", "title")}
                     for s in cal.get("speeches", []) if _ahead(s)][:limit_speeches],
    }


def _clean_pipeline_changes(items):
    """Normalise the review's engineering asks to {change, why, effort} dicts.

    The model may hand back bare strings. Coercing here keeps the renderers from having to
    branch on the shape three separate times.
    """
    out = []
    for it in (items or [])[:6]:
        if isinstance(it, dict):
            change = str(it.get("change") or it.get("title") or "").strip()
            if not change:
                continue
            out.append({"change": change,
                        "why": str(it.get("why") or "").strip() or None,
                        "effort": (str(it.get("effort") or "").strip().lower() or None)})
        elif str(it).strip():
            out.append({"change": str(it).strip(), "why": None, "effort": None})
    return out


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
    # Everything recommended this week, filled or not. "What signals did you send" has a
    # different answer from "what trades did you take", and the recap owes both.
    signals = memory.signals_between(week_start)
    recap = journal.build_recap(portfolio, week_start, prices=prices, signals=signals,
                                week_ahead=_week_ahead(),
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
            recap["signal_review"] = review.get("signal_review")
            recap["pipeline_changes"] = _clean_pipeline_changes(review.get("pipeline_changes"))
        except Exception as e:
            print(f"[warn] weekly review deep run failed: {e}", file=sys.stderr)
    # The board's second (and last) scheduled re-score of the week. Friday is when the evidence
    # the cycles hung on each thesis is freshest, and it is the only other moment the pipeline is
    # allowed to move conviction.
    try:
        board, tstats = run_thesis_review(args, portfolio_summary=portfolio.summary(prices))
        recap["theses"] = thesis.for_packet(board)
        recap["thesis_regime"] = tstats.get("regime")
    except Exception as e:
        print(f"[warn] weekly thesis review: {e}", file=sys.stderr)

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
        # Engineering asks go to their own file: STRATEGY.md is reloaded into every deep run,
        # and "add an options-flow source" is not a rule the analyst can trade by.
        memory.append_pipeline_backlog(recap.get("pipeline_changes"), now_et,
                                       stats=recap["stats"])
        notify.send_email(subject, text, html)
        supabase.push({"market_outlook": recap["narrative"], "signals": []},
                      {"why": ["weekly recap"]}, portfolio.summary(prices), "weekly",
                      now_et, False)
        if recap.get("theses") is not None:
            supabase.push_theses(recap["theses"], "weekly", now_et,
                                 regime=recap.get("thesis_regime"))
        memory.commit_memory(f"weekly recap {now_et.strftime('%Y-%m-%d')}")
    else:
        print(f"[dry-run] would email: {subject}\n{text[:2500]}")
    return 0


def _last_week_stats():
    """Counts of what was published in the previous seven days, by outcome.

    The digest opens the week, so the only honest way to say "here is where we are" is to say
    what the week just gone actually produced — including the repeats the de-duplicator held
    back, which are now invisible to Lind by design and would otherwise never be accounted for.
    """
    try:
        signals = memory.signals_between((datetime.now(config.UTC) - timedelta(days=7)).isoformat())
    except Exception as e:
        print(f"[warn] last-week stats: {e}", file=sys.stderr)
        return {}
    counts = {"n_signals": 0, "n_executed": 0, "n_rejected": 0,
              "n_advisory": 0, "n_duplicate": 0}
    for s in signals:
        outcome = (s.get("outcome") or "advisory").lower()
        if outcome == "duplicate":
            counts["n_duplicate"] += 1
            continue          # never published, so never counted as a signal Lind saw
        counts["n_signals"] += 1
        key = f"n_{outcome}"
        # Anything the log calls something else (held, error) is still a signal Lind was shown,
        # so it lands in advisory rather than vanishing from the totals.
        counts[key if key in counts else "n_advisory"] += 1
    return counts


def _digest_session_plan(now_et):
    """The week's operating schedule, in Lind's words rather than cron's.

    Spelled out in the email because the whole point of the schedule change was that he stopped
    being able to predict when the brain would talk to him.
    """
    # The week's *first* session, not today's — on a holiday Monday there is no session today,
    # and the schedule Lind needs is the one that governs the week he is about to trade.
    first = market_hours.first_trading_day_of_week(now_et.date()) or now_et.date()
    bounds = market_hours.session_bounds(first)
    o, c = ((bounds[0].strftime("%H:%M"), bounds[1].strftime("%H:%M"))
            if bounds else ("09:30", "16:00"))
    return [
        f"Signals run only while the market is open — {o}–{c} ET, Monday to Friday.",
        "A cycle every 30 minutes inside that window; deep analysis only when something "
        "actually triggers, so a quiet tape means a quiet inbox.",
        "Nothing new after the close. Positions still get their stops checked, but no fresh "
        "entry is proposed until the next open.",
        "Repeat ideas are suppressed — the same setup will not be emailed twice while the "
        "condition simply persists.",
        "Friday after the close: the weekly recap, with every trade graded.",
    ]


def do_digest(args):
    """The one email that lands before the week starts.

    It is the week's opening statement: where the paper book stands, what the brain believes
    structurally over 1-3 years, what on the calendar can reprice the tape, and when it will and
    will not speak. The thesis board is re-scored here and on Friday, and nowhere else.
    """
    now_et = datetime.now(config.ET)
    persist = not args.dry_run
    portfolio = Portfolio(mirror=False)   # read-only run
    calendar = collect.forexfactory_calendar(include_next_week=True)
    psum = portfolio.summary()

    board, tstats = run_thesis_review(args, calendar=calendar, portfolio_summary=psum)
    theses = thesis.for_packet(board)
    delta = []
    if tstats.get("added"):
        delta.append(f"{tstats['added']} new")
    if tstats.get("updated"):
        delta.append(f"{tstats['updated']} re-scored")
    if tstats.get("closed"):
        delta.append(f"{tstats['closed']} retired")

    ahead = _week_ahead(limit_events=15, limit_speeches=8)
    watchlist = []
    for t in theses:
        for tk in (t.get("tickers") or []):
            if tk not in watchlist:
                watchlist.append(tk)
    for tk in portfolio.held_tickers():
        if tk not in watchlist:
            watchlist.append(tk)

    digest = {
        "week_label": now_et.strftime("%b %d, %Y"),
        "session_line": f"Generated {now_et.strftime('%a %b %d, %H:%M ET')} · "
                        f"{market_hours.entries_reason(now_et)}",
        "portfolio": psum,
        "theses": theses,
        "regime": tstats.get("regime"),
        "thesis_delta_line": ", ".join(delta) if delta else "board carried forward",
        "events": ahead.get("events") or [],
        "speeches": ahead.get("speeches") or [],
        "watchlist": watchlist[:18],
        "last_week": _last_week_stats(),
        "session_plan": _digest_session_plan(now_et),
    }

    subject, text, html = notify.build_digest_email(digest, now_et)
    if persist:
        obsidian.write_weekly_review(text, now_et)  # reuse dated-note writer
        notify.send_email(subject, text, html)
        supabase.push({"market_outlook": tstats.get("regime") or thesis.summary_line(board),
                       "signals": []},
                      {"why": ["week-ahead digest"]}, psum, "digest", now_et, False)
        supabase.push_theses(theses, "digest", now_et, regime=tstats.get("regime"))
        memory.commit_memory(f"week-ahead digest {now_et.strftime('%Y-%m-%d')}")
    else:
        print(f"[dry-run] would email: {subject}\n{text}")
    return 0


def _mode_of(args):
    """The gate's name for what this invocation is — the anchor's own name, not 'anchor'."""
    if args.crypto_cycle:
        return "crypto"
    if args.anchor:
        return args.anchor
    if args.research:
        return "research"
    if args.weekly_review:
        return "weekly"
    if args.digest:
        return "digest"
    return "cycle"


def main():
    ap = argparse.ArgumentParser(description="Market Brain — paper-trading analysis engine")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--cycle", action="store_true", help="collect+screen, deep-analyze only if flagged")
    g.add_argument("--crypto-cycle", action="store_true", dest="crypto_cycle",
                   help="same, for the 24/7 crypto book — separate ledger, no session gate")
    g.add_argument("--anchor", choices=["pre", "mid", "close"], help="scheduled deep run")
    g.add_argument("--research", metavar="TICKER", help="on-demand deep dive into one ticker")
    g.add_argument("--weekly-review", action="store_true", dest="weekly_review")
    g.add_argument("--digest", action="store_true", help="weekend week-ahead digest")
    ap.add_argument("--dry-run", action="store_true", help="no email / dashboard / git push")
    ap.add_argument("--no-claude", action="store_true", help="skip the CLI deep run (fallback only)")
    ap.add_argument("--no-apply", action="store_true", help="research: don't touch the paper portfolio")
    ap.add_argument("--ignore-market-hours", action="store_true",
                    help="run even when the market-hours gate says no (manual backfills only)")
    args = ap.parse_args()

    # ── the market-hours gate ───────────────────────────────────────────────────
    # One chokepoint for every mode. Cron proposes a time; this decides whether the market is
    # actually open for it. Nothing below runs on a weekend, a holiday, or outside the session,
    # and the close/weekly runs wait for the *real* close rather than a fixed UTC hour.
    #
    # The crypto cycle is routed around it, not exempted from gating: it runs
    # market_hours.crypto_gate() itself, which knows that 03:00 on a Sunday is a normal trading
    # hour there. The equity gate would refuse every one of those runs.
    mode = _mode_of(args)
    if not args.ignore_market_hours and not args.crypto_cycle:
        allowed, why = market_hours.gate(mode)
        if not allowed:
            now_et = datetime.now(config.ET)
            print(f"[gate] skipping {mode} run — {why}")
            if not args.dry_run:
                supabase.push_heartbeat(mode, now_et, why)
            return 0
        # The second half of the gate. Debian cron has no CRON_TZ, so the crontab offers each
        # anchor at both of its possible UTC times and lets the gate discard the wrong one — but
        # in one DST regime both candidates fall inside the same window. Without this, the close
        # anchor would run twice: two Opus runs, two emails, one session.
        if runlog.already_ran(mode):
            print(f"[gate] skipping {mode} run — already ran today")
            return 0
        print(f"[gate] {mode} run allowed — {why}")

    try:
        if args.cycle:
            return do_cycle(args, mode="cycle", force=False)
        if args.crypto_cycle:
            return do_crypto_cycle(args)
        if args.anchor:
            rc = do_cycle(args, mode=args.anchor, force=True)
            if rc == 0:
                runlog.mark_ran(mode)      # only after it worked, so a crash is retried
            return rc
        if args.research:
            return do_research(args)
        if args.weekly_review:
            rc = do_weekly_review(args)
            if rc == 0:
                runlog.mark_ran(mode)
            return rc
        if args.digest:
            rc = do_digest(args)
            if rc == 0:
                runlog.mark_ran(mode)
            return rc
        ap.error("no mode selected")
    finally:
        # Flush the Finnhub disk cache once, at the end, however the run ended. Fundamentals
        # move on the order of hours; re-fetching them every 30 minutes burns the free tier's
        # 60 calls/min for data that has not changed.
        collect.fundamentals_cache_save()


if __name__ == "__main__":
    sys.exit(main())
