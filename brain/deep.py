"""Deep analysis via the Claude Code subscription (no Anthropic API key).

Lind is region-ineligible for an API key, so the brain shells out to the `claude` CLI
authenticated with his subscription OAuth (`claude setup-token`). The screener decides
*whether* to spend a deep run; this module builds the data packet + prompt, invokes
`claude -p --output-format json`, and parses a strict JSON block back out.

The CLI runs with the repo as cwd so trading-brain/CLAUDE.md loads as the agent rulebook.
On usage-limit or transient failure it retries with backoff, then degrades to a
deterministic fallback signal set (built from the screener triggers) so a cycle never
dies just because the subscription is momentarily unavailable.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from . import config, market_hours, regime
from .jsonio import dumps, json_safe   # noqa: F401 — re-exported for the packet builders


def _pace(now, portfolio_summary):
    """The week's trade count and shape against the target, plus how much week is left."""
    psum = portfolio_summary or {}
    taken = int(psum.get("new_trades_this_week", 0) or 0)
    left = market_hours.sessions_left_this_week(now.date())
    # Whichever book produced this summary. Falling back to the stock rules would show the crypto
    # analyst an equity target, which is the one number here it must not read wrong.
    rules = config.rules_for(psum.get("book") or "stock")
    by_type = psum.get("new_trades_by_type") or {}
    gap = rules.mix_gap(by_type)
    return {
        "new_trades_this_week": taken,
        "weekly_target": rules.weekly_trade_target,
        "weekly_cap": rules.max_new_trades_per_week,
        "sessions_left_this_week": left,
        # Behind only when the week can no longer fit the remaining trades at one a session.
        # Zero trades with five sessions left is on pace, not behind — flagging that would put a
        # "you are behind" nudge in front of the analyst every Monday morning, which is noise.
        "behind_pace": left < rules.weekly_trade_target - taken,
        # The week's shape. In 805 logged signals not one was LONG_TERM: an intraday screener
        # escalates on intraday setups and spends the budget before a months-long idea is ever
        # considered. `mix_gap` is what puts the unfilled horizon in front of the analyst — it
        # lowers the bar for what gets *looked at*, never the evidence a trade has to carry.
        "by_type": by_type,
        "mix_target": dict(rules.weekly_mix),
        "mix_gap": gap,
        "long_term_open": bool(gap.get("LONG_TERM")),
    }


def build_packet(mode, market, screen_result, calendar, portfolio_summary,
                 analogs=None, fundamentals=None, focus=None, news_intel=None,
                 strategy_notes=None, theses=None):
    """Assemble the compact JSON packet the deep prompt reasons over."""
    focus = focus or [t["ticker"] for t in screen_result.get("triggers", [])][:8]
    slim_market = {}
    for t in set(focus) | set(config.MARKET_CONTEXT):
        if t in market:
            slim_market[t] = market[t]
    intel = news_intel or {}
    now = datetime.now(config.ET)
    return {
        "as_of": now.strftime("%Y-%m-%d %H:%M ET"),
        "mode": mode,
        # Where the week stands against the pace target. Visible, never enforced: the harness has
        # no rule that fires a trade to hit a number. It exists so a quiet week reads as a decision
        # the analyst made rather than a question nobody asked.
        "pace": _pace(now, portfolio_summary),
        # Tier 1 (Haiku 4.5) read of the news. The only headlines you may cite are these.
        "news_intel": {
            "model": intel.get("model"),
            "degraded": bool(intel.get("degraded")),
            # A cached read is a real model read, just not one taken this minute: no new story
            # survived the quality gate since it, so re-reading would have bought the same answer
            # at the price of a session-limit slot. Distinct from `degraded`, which means nothing
            # read the news at all — conflating the two would either throw away a good news leg
            # or claim one that does not exist.
            "cached": bool(intel.get("cached")),
            "cache_age_min": intel.get("cache_age_min"),
            "macro_read": intel.get("macro_read"),
            "macro_sentiment": intel.get("macro_sentiment"),
            "tickers": {t: v for t, v in (intel.get("tickers") or {}).items()
                        if t in focus or (v.get("materiality") == "high")},
            "top_stories": intel.get("top_stories") or [],
            # How much of this cycle's feed was recycled — context for how much the news is worth.
            "quality": intel.get("news_quality") or {},
        },
        # Lessons the weekly review has written back into brain-memory/STRATEGY.md.
        "strategy_lessons": strategy_notes or "",
        # The standing long-horizon board (brain/thesis.py). Read-only here: a cycle may *use* a
        # thesis to justify a LONG_TERM entry, but only the Monday and Friday reviews may score
        # one. That separation is what stops a 30-minute chart from rewriting a 3-year argument.
        "theses": theses or [],
        "screen": {
            "why": screen_result.get("why", []),
            "calendar_flags": screen_result.get("calendar_flags", []),
            "triggers": screen_result.get("triggers", [])[:12],
        },
        "market_context": {t: market.get(t, {}).get("indicators", {}) for t in config.MARKET_CONTEXT},
        # How many names came with the index, not just what the index did. `market_context` above
        # can be green on five mega-caps while the average holding rolls over, and that gap is
        # where a long book gives a year back. Deterministic, so it is present on every cycle —
        # including the off-hours passes where no model runs at all. Enforced, not advisory: see
        # portfolio.validate_action.
        "regime": regime.assess(
            market, universe=config.rules_for("crypto" if mode == "crypto" else "stock").tickers),
        "focus": {t: slim_market.get(t, {}) for t in focus},
        "calendar": {
            "imminent": calendar.get("imminent", []),
            "speeches": calendar.get("speeches", [])[:6],
            "high_impact_week": [e for e in calendar.get("events", []) if e.get("impact", "").lower() == "high"][:12],
        },
        "fundamentals": fundamentals or {},
        "news_analogs": analogs or [],
        "portfolio": portfolio_summary,
    }


def _load_prompt(mode):
    name = {"pre": "anchor_pre", "mid": "anchor_mid", "close": "anchor_close",
            "cycle": "cycle", "weekly": "weekly_review", "research": "research",
            "crypto": "crypto_cycle"}.get(mode, "cycle")
    p = config.REPO_ROOT / "prompts" / f"{name}.md"
    if p.exists():
        return p.read_text(encoding="utf-8")
    return _load_prompt.DEFAULT  # type: ignore


_load_prompt.DEFAULT = (
    "You are the Market Brain analyst. Analyze the DATA PACKET and return ONLY the required "
    "JSON object. Follow the schema in CLAUDE.md exactly. Paper trading only."
)


_CRYPTO_VENUE_NOTE = (
    "THIS RUN IS THE CRYPTO BOOK. It is a separate paper account from the equity one, with its\n"
    "own cash, its own positions and its own rules. Never propose an equity ticker here, and never\n"
    "reason about the crypto book's exposure using the stock book's positions.\n"
    "  - The venue never closes. There is no open, no close, no half-day and no weekend, so\n"
    "    'wait for the bell' and 'no time to work before the close' are not available reasons.\n"
    "    The day rolls at 00:00 UTC and so does every daily candle you are shown.\n"
    "  - `fundamentals` is absent for every token and always will be. A token has no earnings\n"
    "    date, no margin and no Form 4. Its absence is not a missing data point to note or work\n"
    "    around — the concept does not apply. LONG_TERM here rests on the tape, on flows and on\n"
    "    the news, and `thesis_id` may be null.\n"
    "  - The stops are wider because the asset is. This book cuts at -15% and trails 20%, so an\n"
    "    equity-width -7% stop on BTC is not caution, it is an exit on ordinary noise.\n"
    "  - SCALP is enforced here, not merely labelled. Tag a signal SCALP and the harness will\n"
    "    close it automatically within 24 hours of the fill, at whatever the tape says — winner,\n"
    "    loser or flat — and it runs on a tighter -6% cut, a 7% trail and a smaller position than\n"
    "    a swing. Use it when the idea genuinely resolves inside a day. Do NOT tag a multi-day\n"
    "    thesis SCALP: it will be closed a day in regardless of how right it was going to be.\n"
    "    Equally, do not label a genuine intraday momentum trade SHORT_TERM to dodge the clock.\n"
    "    `holding_period` must match: hours for a SCALP, days-weeks for SHORT_TERM.\n\n"
)


_SCALP_FOCUS_NOTE = (
    "SCALP FOCUS IS ON FOR THIS SESSION. Lind has asked specifically for intraday trades today.\n"
    "Look at the intraday setups FIRST — VWAP reclaims and rejections, opening-range breaks, gap\n"
    "fills, 5-15m momentum against the day's structure — and only then at the swing board.\n"
    "  - This changes what you look at first. It does NOT change what qualifies. A SCALP still\n"
    "    needs a real level, real volume confirmation and an honest confidence_rationale, and an\n"
    "    empty signals array is still the right answer on a dead tape. Do not manufacture an\n"
    "    intraday trade to satisfy this note; say in `notes` that nothing set up.\n"
    "  - Do NOT relabel a multi-day thesis as SCALP to fit the request. The tag is mechanical:\n"
    "    the harness force-closes a SCALP at its deadline (24h crypto, 8h equity) at whatever the\n"
    "    tape shows, on a tighter stop and a smaller position. A swing wearing a SCALP tag gets\n"
    "    killed a day in no matter how right it was. If the best idea available is a swing,\n"
    "    propose the swing and label it SHORT_TERM.\n"
    "  - `holding_period` must read in hours for anything tagged SCALP.\n\n"
)


def build_prompt(mode, packet):
    template = _load_prompt(mode)
    return (
        f"{template}\n\n"
        + (_CRYPTO_VENUE_NOTE if mode == "crypto" else "")
        + (_SCALP_FOCUS_NOTE if config.scalp_focus_active() else "")
        + "You are tier 2 of a two-model pipeline. Tier 1 (Haiku 4.5) already read the news; its\n"
        "findings are in packet.news_intel. Only cite headlines that appear there — anything else\n"
        "is fabrication. If news_intel.degraded is true, no AI read the news this cycle: say so\n"
        "and lower confidence accordingly.\n"
        "If news_intel.cached is true the read is real but `cache_age_min` minutes old, kept\n"
        "because no new story survived the quality gate since. Treat it as a valid news leg —\n"
        "it is not degraded — but do not describe it as breaking, and if the age is long against\n"
        "your holding period, say so in confidence_rationale.\n"
        "Each name in news_intel carries `crowding` (under-covered / mixed / saturated), a\n"
        "`source_tier`, and an `edge`. A saturated story is already in the price — it is context,\n"
        "not an entry. An under-covered story from a primary source is the one case where the news\n"
        "itself justifies raising confidence, and you must say so in confidence_rationale.\n"
        "`thin_coverage: true` means nothing good was available for that name: treat it as no news,\n"
        "not as quiet confirmation.\n\n"
        "packet.fundamentals now carries the company facts a LONG_TERM thesis has to rest on:\n"
        "margins (gross/operating/profit), roe/roa, debt_to_equity, current_ratio, growth\n"
        "(revenue_growth_yoy, eps_growth_yoy, revenue_growth_5y), valuation (pe_ttm, ps_ttm, pb),\n"
        "beta, the 52-week range, `eps_beats_last_4`, `days_to_earnings`, `analysts` (the spread\n"
        "plus `score_change_1m` — the revision matters more than the level) and `insiders` (open-\n"
        "market Form 4 buying/selling only; grants and option exercises are excluded). A field that\n"
        "is absent could not be computed — do not guess it, and do not read a missing value as zero.\n"
        "`days_to_earnings` under ~5 caps confidence on any swing: the print outranks the chart.\n\n"
        "packet.theses is the standing long-horizon board: structural 1-3 year bets on a demand or\n"
        "pricing shift (the archetype being a memory-price squeeze lifting the DRAM makers). Rules:\n"
        "  - Every LONG_TERM signal MUST set `thesis_id` to a board entry. If nothing on the board\n"
        "    fits, the idea is not a LONG_TERM trade — label it SHORT_TERM or drop it.\n"
        "  - You may NOT add, re-score or retire a thesis here. That happens twice a week, in the\n"
        "    Monday and Friday reviews. If a cycle's news materially confirms or breaks one, say so\n"
        "    in `thesis_notes` and it will be carried into the next review.\n"
        "  - `needs_review: true` on an entry means its evidence has gone quiet. Treat its\n"
        "    conviction as suspect and do not open new size against it.\n\n"
        "packet.pace is how the week is tracking: `new_trades_this_week` against `weekly_target`\n"
        "(the pace Lind asked for, roughly one entry per session) with `sessions_left_this_week`\n"
        "and the hard `weekly_cap`. Read it as an activity check, not a quota. This is a fast tape\n"
        "and the failure it guards against is real: passing on a decent setup at 09:45 because a\n"
        "better one might appear at 14:30, every day, until the week ends flat. If you are behind\n"
        "pace, lower the bar to *look* — take the marginal breakout, the second-tier name, the\n"
        "smaller size — do NOT lower the bar for evidence. A trade with no thesis is worse than no\n"
        "trade, and `behind_pace: true` is never a reason to fabricate one. When you end a run with\n"
        "no signals while behind pace, say in `notes` what specifically was missing, so the Friday\n"
        "review can tell a genuinely dead tape from an analyst who was too slow to commit.\n\n"
        "packet.pace also carries the week's SHAPE, and this is the part that has been failing.\n"
        "`by_type` is what the week has actually opened per horizon, `mix_target` is what it wants\n"
        "(2 scalps, 2 swings, 1 long-horizon), and `mix_gap` is the shortfall. Across 805 logged\n"
        "signals this book produced ZERO LONG_TERM trades — not because none existed but because a\n"
        "30-minute screener escalates on 30-minute setups and the budget was always spent on them\n"
        "first. Lind asked for the mix explicitly.\n"
        "  - When `mix_gap` names a horizon, spend part of THIS run looking for that horizon\n"
        "    specifically. With LONG_TERM open, read packet.theses and packet.fundamentals before\n"
        "    the intraday triggers: the question is which board thesis the tape is currently\n"
        "    offering a sane entry into, not what just broke out.\n"
        "  - The mix never lowers the evidence bar. A LONG_TERM signal still needs its `thesis_id`,\n"
        "    still needs fundamentals behind it, and still needs to clear the confidence gate. An\n"
        "    unfilled slot at the end of the week is a fine outcome; a months-long position opened\n"
        "    to fill a slot is the worst trade on the book.\n"
        "  - Do not relabel to fill a slot. Tagging a two-day momentum idea LONG_TERM to close a\n"
        "    gap corrupts the horizon that the harness enforces and the journal grades.\n\n"
        "Respond with ONE JSON object and nothing else — no prose before or after, no code fence.\n"
        "Schema:\n"
        "{\n"
        '  "market_outlook": "2-4 sentence read of the tape right now",\n'
        '  "signals": [\n'
        "    {\n"
        '      "ticker": "NVDA",\n'
        '      "direction": "LONG" | "SHORT",\n'
        '      "trade_type": "SCALP" | "SHORT_TERM" | "LONG_TERM",\n'
        '      "holding_period": "e.g. minutes-hours | 3-10 days | 3-12 months",\n'
        '      "confidence": 0-100,\n'
        '      "confidence_rationale": "WHY that number: which indicators and which Haiku news '
        'findings raise it, and what specifically holds it back",\n'
        '      "entry": number, "stop": number, "target1": number, "target2": number,\n'
        '      "why": "why this is a good trade, plain language",\n'
        '      "analysis_done": "which analysis stages contributed",\n'
        '      "indicators": "the actual indicator values that mattered",\n'
        '      "indicators_used": ["RSI", "MACD", "MA20", "VWAP", "ATR", "volume-vs-avg"],\n'
        '      "chart_read": "the named chart pattern(s) with numbers",\n'
        '      "news_read": "what the Haiku 4.5 news pass concluded for this name, and how it '
        'changed your view",\n'
        '      "news": [{"title": "...", "source": "...", "link": "...", "takeaway": "..."}],\n'
        '      "news_edge": "what this trade knows that the crowd has not priced (cite the '
        'under-covered story or filing), or null if the news here is saturated",\n'
        '      "historical_analog": "cite a matching past event + its outcome, or null",\n'
        '      "thesis_id": "REQUIRED for LONG_TERM: the packet.theses id this rests on, else null",\n'
        '      "data_sources": ["yfinance", "Finnhub", "ForexFactory", "Google News RSS"]\n'
        "    }\n"
        "  ],\n"
        '  "thesis_notes": [\n'
        '    {"thesis_id": "...", "observation": "what today confirmed or broke", '
        '"stance": "supports"|"undermines"}\n'
        "  ],\n"
        '  "portfolio_actions": [\n'
        '    {"action": "BUY"|"SELL"|"ADD"|"HOLD", "ticker": "NVDA", "entry": number,\n'
        '     "stop": number, "target": number, "target_weight_pct": number, "reason": "..."}\n'
        "  ],\n"
        '  "notes": "anything the trader should watch next"\n'
        "}\n\n"
        "DATA PACKET:\n```json\n"
        + dumps(packet, indent=2)
        + "\n```\n"
    )


_REVIEW_SCHEMA = """{
  "week_summary": "one line: how the week went",
  "narrative": "3-6 sentences, honest: what the week's decisions actually got right and wrong",
  "successes": ["specific things that worked, each tied to a real trade or setup type"],
  "mistakes": ["specific errors, each naming the trade and the actual mistake"],
  "changes": ["analyst-side rules for your future self, checkable at the next entry"],
  "signal_review": "2-4 sentences on what you recommended vs what the gates let through, and \
whether the untaken signals would have worked",
  "pipeline_changes": [
    {"change": "a concrete engineering change to the pipeline itself",
     "why": "the measurement in measured_performance that motivates it",
     "effort": "small" | "medium" | "large"}
  ]
}"""


def build_review_prompt(recap_json):
    """Weekly accuracy review — a different contract from the trade schema.

    Deliberately not routed through build_prompt(): this run must not emit signals or
    portfolio actions, and its output feeds STRATEGY.md, which every later deep run reads.
    """
    template = _load_prompt("weekly")
    return (
        f"{template}\n\n"
        "You are reviewing your OWN past trade decisions. The packet contains the week's closed\n"
        "trades with the reasoning recorded at entry, plus rule-based gradings computed by the\n"
        "harness (not by a model). Judge the decisions against what actually happened.\n\n"
        "`changes` is the important field: each entry gets appended to brain-memory/STRATEGY.md\n"
        "and read by every future deep run, so write instructions your future self can act on\n"
        '("require volume >1.5x average before taking a breakout on a semi name"), not platitudes\n'
        '("be more disciplined"). If the sample is too small to conclude anything, say that in\n'
        "`narrative` and return few or no changes — inventing lessons from 1 trade is worse than\n"
        "admitting the sample is thin.\n\n"
        "`measured_performance` is arithmetic over the ledger, not opinion — win rate per\n"
        "indicator you claimed to use, calibration of each confidence bucket against its implied\n"
        "hit rate, results by trade type, by exit kind and by sector, and `signals`: everything\n"
        "recommended this week with the gate's verdict and the forward return on the ones that\n"
        "were NOT taken. Ground `mistakes` and `changes` in those numbers, quoting them. Where\n"
        "`sample_note` or a bucket's `read` says the sample is too thin, say so instead of\n"
        "drawing a conclusion from it.\n\n"
        "`stats.n_opened` against `stats.weekly_trade_target` (and `pace_gap`) is the activity\n"
        "check: Lind asked for roughly one entry per session. Coming in under target is not a\n"
        "failure by itself, but the review must say which it was — a tape that offered nothing\n"
        "(cite the regime and what was missing) or setups you passed on. Cross-check\n"
        "`measured_performance.signals` for advisory and rejected calls that then ran: those are\n"
        "evidence of hesitation and belong in `mistakes`. Over target with a weak win rate is the\n"
        "mirror failure. Never write 'trade more' as a change on its own.\n\n"
        "`changes` and `pipeline_changes` are different things and must not be mixed:\n"
        "  - `changes` are analyst-side rules for how YOU judge a setup. They never override the\n"
        "    harness gates (position limits, the -7% cut, trailing stops, sector lockout).\n"
        "  - `pipeline_changes` are engineering requests for the humans maintaining the harness:\n"
        "    data you needed and did not have, a gate that measurably blocked winners, a source\n"
        "    that was consistently stale or useless, a field you could not fill honestly. Each\n"
        "    must cite the measurement that motivates it. Return an empty list rather than\n"
        "    inventing work — 'the pipeline was adequate this week' is a valid finding.\n\n"
        "Respond with ONE JSON object and nothing else — no prose before or after, no code fence.\n"
        f"Schema:\n{_REVIEW_SCHEMA}\n\n"
        "WEEK PACKET:\n```json\n" + recap_json + "\n```\n"
    )


_THESIS_SCHEMA = """{
  "market_regime": "2-4 sentences: where the money is actually flowing over quarters, not days",
  "theses": [
    {
      "id": "existing board id, or omit for a new thesis",
      "theme": "short name, e.g. 'DRAM undersupply' or 'grid capex for AI datacentres'",
      "driver": "the structural demand or pricing shift itself, with the numbers that show it",
      "thesis": "the argument in plain language: who wants more of what, why supply cannot \
answer quickly, and how that reaches revenue and margin",
      "what_the_market_misses": "why this is not already in the price, or null if it is",
      "tickers": ["the cleanest listed expressions, most direct first"],
      "primary_ticker": "the single best expression",
      "horizon": "1-3 years",
      "conviction": 0-100,
      "status": "active" | "watch" | "invalidated" | "realized",
      "close_reason": "required when status is invalidated or realized",
      "invalidation": ["specific, checkable conditions that would prove this wrong"],
      "milestones": ["what should be observable in the next 1-2 quarters if it is right"],
      "evidence": [{"note": "...", "source": "...", "link": "...", \
"stance": "supports"|"undermines"}]
    }
  ],
  "retired": ["ids removed from consideration, with the reason in close_reason above"]
}"""


def build_thesis_prompt(packet_json):
    """The long-horizon review — the only run permitted to score the thesis board.

    Kept apart from both the trade schema and the weekly accuracy review because it answers a
    different question on a different clock: not "was that trade right" but "is this still where
    the world is going".
    """
    return (
        "You are the Market Brain's long-horizon analyst. This run is NOT about today's tape.\n"
        "You are maintaining a standing board of 1-3 year structural investment theses, and you\n"
        "are the only run allowed to change it.\n\n"
        "What counts as a thesis here: a durable shift in what people or companies demand, big\n"
        "enough that the supply side cannot answer it within a year, expressible through listed\n"
        "companies whose revenue and margin should visibly benefit. The archetype Lind gave: when\n"
        "memory demand ran ahead of fab capacity and DRAM contract prices doubled, the memory\n"
        "makers were the trade — not because of a chart, but because the product was wanted and\n"
        "could not be made fast enough.\n\n"
        "What does NOT count: a stock that has gone up; a good company at any price; a story with\n"
        "no supply constraint; anything whose whole case is momentum or sentiment. If the honest\n"
        "answer is that nothing structural is visible right now, return few or no theses. An empty\n"
        "board beats a fabricated one — you will be graded against these in a year.\n\n"
        "For each thesis you MUST supply `invalidation`: specific, checkable conditions that would\n"
        "prove it wrong (a capacity number, a price level, a demand datapoint). A thesis with no\n"
        "falsifier is a bias, and it will be rejected.\n\n"
        "Re-score what is already on the board before proposing anything new. Conviction should\n"
        "move on evidence in the packet, not on price action. Retire an entry honestly: mark it\n"
        "`invalidated` when the argument broke and `realized` when it has played out, and say why\n"
        "in `close_reason`. Entries flagged `needs_review` have had no fresh evidence in weeks —\n"
        "either find some in this packet or cut the conviction.\n\n"
        "Only cite headlines and data present in the packet. Fabricated evidence is the worst\n"
        "possible failure here, because a fake datapoint on a 3-year thesis survives for 3 years.\n\n"
        "Respond with ONE JSON object and nothing else — no prose before or after, no code fence.\n"
        f"Schema:\n{_THESIS_SCHEMA}\n\n"
        "PACKET:\n```json\n" + packet_json + "\n```\n"
    )


_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def _extract_json(text):
    """Pull the JSON object out of the CLI's text response."""
    text = text.strip()
    # claude -p --output-format json wraps the result; try that envelope first
    try:
        env = json.loads(text)
        if isinstance(env, dict) and "result" in env and isinstance(env["result"], str):
            text = env["result"].strip()
        elif isinstance(env, dict) and "signals" in env:
            return env
    except json.JSONDecodeError:
        pass
    # strip a ```json fence if present
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    m = _JSON_RE.search(text)
    if not m:
        raise ValueError("no JSON object found in Claude response")
    return json.loads(m.group(0))


class ClaudeRunError(RuntimeError):
    """A `claude -p` invocation failed.

    `kind` is what the caller needs to act on: "quota" and "auth" are walls that no amount of
    retrying gets through, "timeout" and "parse" are transient, "other" is unclassified. `reason`
    is the CLI's own words, not a reconstruction.
    """

    def __init__(self, reason, kind="other", model=None):
        super().__init__(reason)
        self.reason = reason
        self.kind = kind
        self.model = model


# The CLI reports auth and quota failures through its stdout envelope, not stderr: it exits 1 with
# stderr *empty* and `{"type":"result","is_error":true,"result":"Not logged in · Please run /login"}`
# on stdout. Reading only stderr is how the 2026-07-28 → 07-31 outage presented as `exit 1: ` with
# nothing after the colon, burned both retries every cycle, and silently degraded ~700 signals to
# the fallback's flat SHORT_TERM / confidence 35.
_WALL_PATTERNS = (
    ("usage limit", "quota"),
    ("rate limit", "quota"),
    ("quota", "quota"),
    ("credit balance", "quota"),
    ("out of credits", "quota"),
    ("not logged in", "auth"),
    ("/login", "auth"),
    ("invalid api key", "auth"),
    ("authentication_error", "auth"),
    ("unauthorized", "auth"),
    ("oauth token", "auth"),
)


def _envelope_message(out):
    """The CLI's own error text, dug out of the JSON envelope. None when there isn't one."""
    if not out:
        return None
    try:
        env = json.loads(out)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(env, dict):
        return None
    for key in ("result", "error", "message"):
        val = env.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
        if isinstance(val, dict):
            inner = val.get("message")
            if isinstance(inner, str) and inner.strip():
                return inner.strip()
    return None


def _classify(text):
    """Map a failure message onto a `ClaudeRunError.kind`."""
    low = (text or "").lower()
    for needle, kind in _WALL_PATTERNS:
        if needle in low:
            return kind
    return "other"


def _envelope_is_error(out):
    """True when the CLI exited 0 but flagged the result itself as an error."""
    try:
        env = json.loads(out)
    except (json.JSONDecodeError, ValueError):
        return False
    return isinstance(env, dict) and bool(env.get("is_error"))


def run_claude(prompt, model=None, attempts=2, timeout=None):
    """Invoke the subscription `claude` CLI headlessly. Returns parsed dict or raises."""
    model = model or config.CLAUDE_DEEP_MODEL
    timeout = timeout or config.CLAUDE_TIMEOUT
    # The prompt goes on STDIN, not argv: the data packet pushes it far past the OS
    # command-line limit (Windows ~32K → WinError 206; Linux ARG_MAX is higher but still
    # finite). `claude -p` with no positional query reads the prompt from stdin.
    cmd = [config.CLAUDE_BIN, "-p",
           "--model", model, "--output-format", "json"]
    last_err, last_kind = None, "other"
    for i in range(attempts):
        try:
            proc = subprocess.run(
                cmd, cwd=str(config.REPO_ROOT), input=prompt, capture_output=True, text=True,
                timeout=timeout, encoding="utf-8", errors="replace")
        except subprocess.TimeoutExpired:
            last_err, last_kind = f"timeout after {timeout}s", "timeout"
            print(f"[warn] claude run {i+1} ({model}): {last_err}", file=sys.stderr)
            continue
        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        if proc.returncode != 0 or _envelope_is_error(out):
            # Prefer the envelope: on a quota or auth wall it is the only place the reason exists.
            why = _envelope_message(out) or err or "no error message on stderr or stdout"
            last_kind = _classify(why) if _classify(why) != "other" else _classify(err)
            last_err = f"exit {proc.returncode} [{last_kind}]: {why[:300]}"
            print(f"[warn] claude run {i+1} ({model}): {last_err}", file=sys.stderr)
            if last_kind in ("quota", "auth"):
                break  # a wall, not a hiccup — the second attempt fails identically
            continue
        try:
            return _extract_json(out)
        except Exception as e:
            last_err, last_kind = f"parse: {e}; head={out[:200]!r}", "parse"
            print(f"[warn] claude run {i+1} ({model}): {last_err}", file=sys.stderr)
    raise ClaudeRunError(f"claude run failed ({model}): {last_err}", kind=last_kind, model=model)


def fallback_analysis(screen_result, market, news_intel=None, reason=None):
    """Deterministic degradation when the deep model is unavailable.

    Turns the top screener triggers into low-confidence WATCH-grade signals so the
    cycle still emails something actionable, clearly flagged as rules-only (no AI read).
    Haiku's news pass is independent of the deep run, so if tier 1 succeeded its findings
    still get carried through here rather than thrown away.
    """
    intel_tickers = (news_intel or {}).get("tickers") or {}
    signals = []
    for t in screen_result.get("triggers", [])[:5]:
        ticker = t["ticker"]
        ind = market.get(ticker, {}).get("indicators", {})
        price = ind.get("price")
        bull = sum(1 for p in market.get(ticker, {}).get("patterns", []) if p.get("bias") == "bullish")
        bear = sum(1 for p in market.get(ticker, {}).get("patterns", []) if p.get("bias") == "bearish")
        direction = "LONG" if bull >= bear else "SHORT"
        ni = intel_tickers.get(ticker) or {}
        sent = int(ni.get("sentiment") or 0)
        news_read = (f"Haiku read: {ni.get('summary')} (sentiment {sent:+d}, "
                     f"{ni.get('materiality')} materiality)" if ni.get("summary") else
                     "No news read for this name this cycle.")
        signals.append({
            # `UNSPECIFIED`, not `SHORT_TERM`. The screener flags a chart condition; it does not
            # reason about horizon, and stamping one on anyway was how 805 signals came to carry a
            # SHORT_TERM label nothing had actually chosen. `config.normalize_trade_type` returns
            # None for this, so `portfolio` counts it under UNSPECIFIED, it fills no slot in the
            # weekly mix, and the review sees a signal that did not say what it was — which is the
            # truth about it.
            "ticker": ticker, "direction": direction, "trade_type": "UNSPECIFIED",
            "holding_period": "unclassified (rules-only)", "confidence": 35,
            "confidence_rationale": (
                "Capped at 35: the deterministic screener flagged the setup but no deep model "
                "confirmed it, so this is a watch item"
                + (f" ({reason})" if reason else "") + "."),
            # Carried per signal, not only on the envelope. These rows outlive the email — into
            # the ledger, the journal and the dashboard — and each of those reads one signal at a
            # time, where an analysis-level flag is not visible. A 35 that reads as an ordinary
            # low-conviction call is a different thing from a 35 that means nobody looked.
            "degraded": True,
            "degraded_reason": reason,
            "entry": price, "stop": None, "target1": None, "target2": None,
            "why": "Screener flagged this name; AI deep-analysis was unavailable this cycle.",
            "analysis_done": "deterministic chart-pattern screener only"
                             + ("; Haiku 4.5 news pass" if ni else ""),
            "indicators": ", ".join(f"{k}={v}" for k, v in list(ind.items())[:5]),
            "indicators_used": list(ind.keys())[:6],
            "chart_read": "; ".join(t["reasons"][:3]),
            "news_read": news_read,
            "news": ni.get("headlines_used") or [], "historical_analog": None,
            "data_sources": ["yfinance"] + (["Google News RSS", "Finnhub"] if ni else []),
        })
    why = f" Reason: {reason}." if reason else ""
    return {
        "market_outlook": "AI deep analysis was unavailable this cycle." + why
                          + " The items below are the deterministic screener's raw flags only — "
                            "treat as watchlist, not conviction.",
        "signals": signals, "portfolio_actions": [], "notes": "rules-only fallback",
        "degraded": True, "degraded_reason": reason,
    }
