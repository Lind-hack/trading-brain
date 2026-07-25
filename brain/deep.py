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

from . import config


def build_packet(mode, market, screen_result, calendar, portfolio_summary,
                 analogs=None, fundamentals=None, focus=None, news_intel=None,
                 strategy_notes=None):
    """Assemble the compact JSON packet the deep prompt reasons over."""
    focus = focus or [t["ticker"] for t in screen_result.get("triggers", [])][:8]
    slim_market = {}
    for t in set(focus) | set(config.MARKET_CONTEXT):
        if t in market:
            slim_market[t] = market[t]
    intel = news_intel or {}
    return {
        "as_of": datetime.now(config.ET).strftime("%Y-%m-%d %H:%M ET"),
        "mode": mode,
        # Tier 1 (Haiku 4.5) read of the news. The only headlines you may cite are these.
        "news_intel": {
            "model": intel.get("model"),
            "degraded": bool(intel.get("degraded")),
            "macro_read": intel.get("macro_read"),
            "macro_sentiment": intel.get("macro_sentiment"),
            "tickers": {t: v for t, v in (intel.get("tickers") or {}).items()
                        if t in focus or (v.get("materiality") == "high")},
            "top_stories": intel.get("top_stories") or [],
        },
        # Lessons the weekly review has written back into brain-memory/STRATEGY.md.
        "strategy_lessons": strategy_notes or "",
        "screen": {
            "why": screen_result.get("why", []),
            "calendar_flags": screen_result.get("calendar_flags", []),
            "triggers": screen_result.get("triggers", [])[:12],
        },
        "market_context": {t: market.get(t, {}).get("indicators", {}) for t in config.MARKET_CONTEXT},
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
            "cycle": "cycle", "weekly": "weekly_review", "research": "research"}.get(mode, "cycle")
    p = config.REPO_ROOT / "prompts" / f"{name}.md"
    if p.exists():
        return p.read_text(encoding="utf-8")
    return _load_prompt.DEFAULT  # type: ignore


_load_prompt.DEFAULT = (
    "You are the Market Brain analyst. Analyze the DATA PACKET and return ONLY the required "
    "JSON object. Follow the schema in CLAUDE.md exactly. Paper trading only."
)


def build_prompt(mode, packet):
    template = _load_prompt(mode)
    return (
        f"{template}\n\n"
        "You are tier 2 of a two-model pipeline. Tier 1 (Haiku 4.5) already read the news; its\n"
        "findings are in packet.news_intel. Only cite headlines that appear there — anything else\n"
        "is fabrication. If news_intel.degraded is true, no AI read the news this cycle: say so\n"
        "and lower confidence accordingly.\n\n"
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
        '      "historical_analog": "cite a matching past event + its outcome, or null",\n'
        '      "data_sources": ["yfinance", "Finnhub", "ForexFactory", "Google News RSS"]\n'
        "    }\n"
        "  ],\n"
        '  "portfolio_actions": [\n'
        '    {"action": "BUY"|"SELL"|"ADD"|"HOLD", "ticker": "NVDA", "entry": number,\n'
        '     "stop": number, "target": number, "target_weight_pct": number, "reason": "..."}\n'
        "  ],\n"
        '  "notes": "anything the trader should watch next"\n'
        "}\n\n"
        "DATA PACKET:\n```json\n"
        + json.dumps(packet, indent=2, default=str)
        + "\n```\n"
    )


_REVIEW_SCHEMA = """{
  "week_summary": "one line: how the week went",
  "narrative": "3-6 sentences, honest: what the week's decisions actually got right and wrong",
  "successes": ["specific things that worked, each tied to a real trade or setup type"],
  "mistakes": ["specific errors, each naming the trade and the actual mistake"],
  "changes": ["concrete, checkable changes to make the NEXT week's calls more accurate"]
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
        "Respond with ONE JSON object and nothing else — no prose before or after, no code fence.\n"
        f"Schema:\n{_REVIEW_SCHEMA}\n\n"
        "WEEK PACKET:\n```json\n" + recap_json + "\n```\n"
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


def run_claude(prompt, model=None, attempts=2, timeout=None):
    """Invoke the subscription `claude` CLI headlessly. Returns parsed dict or raises."""
    model = model or config.CLAUDE_DEEP_MODEL
    timeout = timeout or config.CLAUDE_TIMEOUT
    # The prompt goes on STDIN, not argv: the data packet pushes it far past the OS
    # command-line limit (Windows ~32K → WinError 206; Linux ARG_MAX is higher but still
    # finite). `claude -p` with no positional query reads the prompt from stdin.
    cmd = [config.CLAUDE_BIN, "-p",
           "--model", model, "--output-format", "json"]
    last_err = None
    for i in range(attempts):
        try:
            proc = subprocess.run(
                cmd, cwd=str(config.REPO_ROOT), input=prompt, capture_output=True, text=True,
                timeout=timeout, encoding="utf-8", errors="replace")
        except subprocess.TimeoutExpired as e:
            last_err = f"timeout after {timeout}s"
            print(f"[warn] claude run {i+1}: {last_err}", file=sys.stderr)
            continue
        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        if proc.returncode != 0:
            last_err = f"exit {proc.returncode}: {err[:300]}"
            print(f"[warn] claude run {i+1}: {last_err}", file=sys.stderr)
            if "usage limit" in err.lower() or "rate" in err.lower():
                break  # no point retrying a quota wall
            continue
        try:
            return _extract_json(out)
        except Exception as e:
            last_err = f"parse: {e}; head={out[:200]!r}"
            print(f"[warn] claude run {i+1}: {last_err}", file=sys.stderr)
    raise RuntimeError(f"claude deep run failed: {last_err}")


def fallback_analysis(screen_result, market, news_intel=None):
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
            "ticker": ticker, "direction": direction, "trade_type": "SHORT_TERM",
            "holding_period": "unclassified (rules-only)", "confidence": 35,
            "confidence_rationale": "Capped at 35: the deterministic screener flagged the setup "
                                    "but no deep model confirmed it, so this is a watch item.",
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
    return {
        "market_outlook": "AI deep analysis was unavailable this cycle (subscription limit or "
                          "transient error). The items below are the deterministic screener's raw "
                          "flags only — treat as watchlist, not conviction.",
        "signals": signals, "portfolio_actions": [], "notes": "rules-only fallback",
        "degraded": True,
    }
