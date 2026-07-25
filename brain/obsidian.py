"""Obsidian second-brain writer.

Every run (not just ones that email) writes to the Lind Brain vault so the analysis,
data, and pattern recognition all land in the second brain — Lind's explicit requirement.
Active only when LIND_BRAIN points at the vault root (PC: D:\\Obsidian\\Lind Brain;
VPS: its Syncthing copy, which auto-syncs back to the PC). One dated note per ET day,
append-only, plus a rolling trade log.
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

from . import config


def _folder():
    if not config.LIND_BRAIN:
        return None
    f = Path(config.LIND_BRAIN) / config.BRAIN_VAULT_SUBFOLDER
    f.mkdir(parents=True, exist_ok=True)
    return f


def write_run(analysis, screen_result, portfolio_summary, mode, now_et, escalated):
    """Append a run block to today's Trading Brain note. Returns the note path or None."""
    folder = _folder()
    if folder is None:
        return None
    day = now_et.strftime("%Y-%m-%d")
    note = folder / f"{day} - Trading Brain.md"
    try:
        out = []
        if not note.exists():
            out.append(
                f"---\ntags: [stocks, trading-brain, paper-trading, ai-analysis]\ncreated: {day}\n---\n\n"
                f"# Trading Brain — {day}\n\n"
                "AI market analysis (Claude Code subscription) over deterministic screening, "
                "chart-pattern detection, news + ForexFactory calendar, and a simulated paper "
                "portfolio. Paper-trading education, not financial advice. "
                "See [[1000 - Stocks & Markets/Trading Brain/index|Trading Brain index]].\n"
            )
        stamp = now_et.strftime("%I:%M %p ET")
        out.append(f"\n## {stamp} — {mode.upper()} run")
        out.append(f" ({'deep analysis' if escalated else 'screen only — quiet'})\n\n")
        if screen_result.get("why"):
            out.append("- **Screener:** " + "; ".join(screen_result["why"]) + "\n")
        if screen_result.get("calendar_flags"):
            out.append("- **Calendar:** " + "; ".join(screen_result["calendar_flags"]) + "\n")

        if analysis:
            if analysis.get("market_outlook"):
                out.append(f"- **Outlook:** {analysis['market_outlook']}\n")
            for s in analysis.get("signals", []) or []:
                ttype = (s.get("trade_type") or "").upper()
                out.append(
                    f"\n### {s.get('ticker')} — {s.get('direction')} · {ttype} "
                    f"(conf {s.get('confidence')})\n"
                )
                out.append(f"- **Type:** {ttype} · hold {s.get('holding_period','')}\n")
                out.append(f"- **Why:** {s.get('why','')}\n")
                out.append(f"- **Plan:** entry {s.get('entry')} · stop {s.get('stop')} "
                           f"· T1 {s.get('target1')} · T2 {s.get('target2')}\n")
                out.append(f"- **Analysis:** {s.get('analysis_done','')}\n")
                out.append(f"- **Indicators:** {s.get('indicators','')}\n")
                out.append(f"- **Charts:** {s.get('chart_read','')}\n")
                for n in (s.get("news") or [])[:4]:
                    link = n.get("link", "")
                    md = f"[{n.get('title','')}]({link})" if link else n.get("title", "")
                    out.append(f"- **News:** {md} — {n.get('source','')}\n")
                if s.get("historical_analog"):
                    out.append(f"- **Analog:** {s.get('historical_analog')}\n")
                out.append(f"- **Sources:** {', '.join(s.get('data_sources', []))}\n")

        if portfolio_summary:
            out.append(f"\n- **Paper portfolio:** ${portfolio_summary.get('equity',0):,.0f} "
                       f"({portfolio_summary.get('total_return_pct',0):+.1f}%) · "
                       f"{portfolio_summary.get('n_open',0)} open · "
                       f"cash ${portfolio_summary.get('cash',0):,.0f}\n")

        with open(note, "a", encoding="utf-8") as f:
            f.write("".join(out))
        print(f"[obsidian] wrote run -> {note}")
        return str(note)
    except Exception as e:  # pragma: no cover - fs
        print(f"[warn] obsidian write_run failed: {e}", file=sys.stderr)
        return None


def log_trades(exits, applied_actions, now_et):
    """Append mechanical exits + applied portfolio actions to a rolling trade log."""
    folder = _folder()
    if folder is None or not (exits or applied_actions):
        return None
    note = folder / "Paper Trade Log.md"
    try:
        out = []
        if not note.exists():
            out.append("---\ntags: [trading-brain, paper-trading, trade-log]\n---\n\n"
                       "# Paper Trade Log\n\nEvery simulated fill and exit, newest at the bottom. "
                       "Paper trading only — not financial advice.\n")
        stamp = now_et.strftime("%Y-%m-%d %I:%M %p ET")
        for e in exits or []:
            out.append(f"- {stamp} — **EXIT {e['ticker']}** {e['pnl_pct']:+.1f}% "
                       f"(${e['pnl_usd']:+,.0f}) — {e['reason']}\n")
        for a in applied_actions or []:
            out.append(f"- {stamp} — **{a}**\n")
        with open(note, "a", encoding="utf-8") as f:
            f.write("".join(out))
        return str(note)
    except Exception as e:  # pragma: no cover - fs
        print(f"[warn] obsidian log_trades failed: {e}", file=sys.stderr)
        return None


def write_weekly_review(text, now_et):
    folder = _folder()
    if folder is None:
        return None
    note = folder / f"{now_et.strftime('%Y-%m-%d')} - Weekly Review.md"
    try:
        note.write_text(
            f"---\ntags: [trading-brain, weekly-review, paper-trading]\ncreated: {now_et.strftime('%Y-%m-%d')}\n---\n\n"
            f"# Trading Brain — Weekly Review ({now_et.strftime('%Y-%m-%d')})\n\n{text}\n",
            encoding="utf-8")
        print(f"[obsidian] wrote weekly review -> {note}")
        return str(note)
    except Exception as e:  # pragma: no cover - fs
        print(f"[warn] obsidian weekly review failed: {e}", file=sys.stderr)
        return None
