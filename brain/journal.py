"""Trade journal in the Obsidian vault — one note per trade, graded on close.

The point of this module is that the brain can read its own history back. `obsidian.py`
writes the *run* log (what was analysed at 10:03 today); this writes the *trade* record: one
note per position, opened with the full reasoning that justified it, then reopened on exit and
appended with what actually happened and whether the original reasoning held.

Filenames are derived deterministically from ticker + the position's `opened` timestamp, so
the close pass finds the same note the open pass created without needing to store a path.

Everything here is best-effort: no vault configured, or any filesystem error, logs a warning
and returns None. The JSON ledger stays the source of truth.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from pathlib import Path

from . import config

_SAFE = re.compile(r"[^A-Za-z0-9 _.\-]")

# Expected holding windows per trade type, in days — used to grade whether the trade behaved
# like the type it was labelled as. A "SCALP" still open after a week was mislabelled.
_EXPECTED_DAYS = {"SCALP": (0, 1), "SHORT_TERM": (1, 21), "LONG_TERM": (60, 3650)}


def _folder(sub="Trades"):
    if not config.LIND_BRAIN:
        return None
    f = Path(config.LIND_BRAIN) / config.BRAIN_VAULT_SUBFOLDER / sub
    f.mkdir(parents=True, exist_ok=True)
    return f


def _slug(text):
    return _SAFE.sub("", str(text or "")).strip()


def _parse_iso(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None


def note_path(ticker, opened_iso, folder=None):
    """Deterministic note path for a trade, from ticker + open timestamp."""
    folder = folder or _folder()
    if folder is None:
        return None
    dt = _parse_iso(opened_iso)
    stamp = dt.astimezone(config.ET).strftime("%Y-%m-%d %H%M") if dt else "undated"
    return folder / f"{stamp} ET - {_slug(ticker)}.md"


def _fmt_news(items, limit=5):
    out = []
    for n in (items or [])[:limit]:
        title = (n.get("title") or "").strip()
        link = n.get("link") or ""
        src = n.get("source") or ""
        md = f"[{title}]({link})" if link else title
        out.append(f"- {md}{f' — {src}' if src else ''}\n")
    return out


# ── Open ────────────────────────────────────────────────────────────────────────

def open_trade(ticker, position, signal=None, action=None, now_et=None, intel_for_ticker=None):
    """Write the entry note: every reason this trade was taken, before the outcome is known.

    `signal` is the deep model's signal dict for this ticker (may be None if the action came
    from a portfolio-management proposal with no matching signal).
    """
    folder = _folder()
    if folder is None:
        return None
    now_et = now_et or datetime.now(config.ET)
    sig = signal or {}
    act = action or {}
    note = note_path(ticker, position.get("opened"), folder)
    try:
        ttype = (sig.get("trade_type") or position.get("trade_type") or "UNCLASSIFIED").upper()
        conf = sig.get("confidence", position.get("confidence"))
        mirror = position.get("broker") or {}
        lines = [
            "---\n",
            "tags: [trading-brain, paper-trading, trade-journal, "
            f"{ttype.lower().replace('_', '-')}]\n",
            f"ticker: {ticker}\n",
            f"trade_type: {ttype}\n",
            f"direction: {sig.get('direction', 'LONG')}\n",
            f"confidence: {conf if conf is not None else ''}\n",
            f"opened: {position.get('opened')}\n",
            f"entry: {position.get('entry')}\n",
            f"shares: {position.get('shares')}\n",
            "status: OPEN\n",
            "outcome: \n",
            "---\n\n",
            f"# {ticker} — {sig.get('direction', 'LONG')} · {ttype}\n\n",
            f"Opened {now_et.strftime('%Y-%m-%d %I:%M %p ET')} at "
            f"**{position.get('entry')}** · {position.get('shares')} sh · "
            f"sector {position.get('sector')}\n\n",
        ]

        lines.append("## Why this trade\n\n")
        lines.append((sig.get("why") or act.get("reason") or "No thesis recorded.") + "\n\n")

        lines.append("## Confidence and what drives it\n\n")
        lines.append(f"- **Confidence:** {conf if conf is not None else 'n/a'}/100\n")
        if sig.get("confidence_rationale"):
            lines.append(f"- **Rationale:** {sig['confidence_rationale']}\n")
        lines.append(f"- **Expected hold:** {sig.get('holding_period', 'unspecified')}\n\n")

        lines.append("## Trade plan\n\n")
        lines.append(f"- Entry {position.get('entry')} · hard stop {position.get('hard_stop')} "
                     f"· trailing {position.get('trail_pct')}%\n")
        lines.append(f"- Targets: T1 {sig.get('target1')} · T2 {sig.get('target2')}\n")
        if act.get("target_weight_pct"):
            lines.append(f"- Sized to {act['target_weight_pct']}% of equity "
                         f"(capped at {config.MAX_POSITION_PCT}%)\n")
        lines.append("\n")

        lines.append("## Indicators used\n\n")
        used = sig.get("indicators_used") or position.get("indicators_used") or []
        if used:
            lines.append("- " + ", ".join(str(u) for u in used) + "\n")
        if sig.get("indicators"):
            lines.append(f"- **Values:** {sig['indicators']}\n")
        if sig.get("chart_read"):
            lines.append(f"- **Chart read:** {sig['chart_read']}\n")
        if not (used or sig.get("indicators")):
            lines.append("- none recorded\n")
        lines.append("\n")

        lines.append("## News read (Haiku 4.5 tier 1)\n\n")
        if sig.get("news_read"):
            lines.append(sig["news_read"] + "\n\n")
        ni = intel_for_ticker or {}
        if ni:
            lines.append(f"- **Sentiment** {ni.get('sentiment')} · **materiality** "
                         f"{ni.get('materiality')} · **catalyst** {ni.get('catalyst')}\n")
            if ni.get("summary"):
                lines.append(f"- {ni['summary']}\n")
        headlines = sig.get("news") or (ni.get("headlines_used") if ni else []) \
            or position.get("news_at_entry") or []
        lines += _fmt_news(headlines)
        if not (sig.get("news_read") or ni or headlines):
            lines.append("- no news read for this name at entry\n")
        lines.append("\n")

        if sig.get("historical_analog"):
            lines.append(f"## Historical analog\n\n{sig['historical_analog']}\n\n")
        if sig.get("analysis_done"):
            lines.append(f"## Analysis performed\n\n{sig['analysis_done']}\n\n")

        lines.append("## Execution\n\n")
        if mirror.get("mirrored"):
            lines.append(f"- Mirrored to Alpaca **paper** account — order "
                         f"`{mirror.get('order_id')}` ({mirror.get('status')})\n")
            if mirror.get("note"):
                lines.append(f"- {mirror['note']}\n")
        elif mirror:
            lines.append(f"- Not mirrored to the paper broker: {mirror.get('reason')}\n")
        else:
            lines.append("- Simulator only (no paper broker configured)\n")
        lines.append(f"\n{config.DISCLAIMER}\n")

        note.write_text("".join(lines), encoding="utf-8")
        print(f"[journal] opened {ticker} -> {note.name}")
        _append_index(ticker, position, ttype, conf, note)
        return str(note)
    except Exception as e:  # pragma: no cover - fs
        print(f"[warn] journal open_trade {ticker} failed: {e}", file=sys.stderr)
        return None


# ── Close ───────────────────────────────────────────────────────────────────────

def _grade(exit_rec):
    """Deterministic self-assessment: did the trade behave like the label said it would?

    Kept rules-based on purpose — the weekly review model reads these gradings, so they must
    not themselves be model opinion, or the review would just be grading its own guesses.
    """
    notes = []
    pnl = exit_rec.get("pnl_pct") or 0
    conf = exit_rec.get("confidence")
    ttype = (exit_rec.get("trade_type") or "").upper()
    opened, closed = _parse_iso(exit_rec.get("opened")), _parse_iso(exit_rec.get("closed"))
    held_days = None
    if opened and closed:
        held_days = round((closed - opened).total_seconds() / 86400, 2)
        lo, hi = _EXPECTED_DAYS.get(ttype, (None, None))
        if lo is not None:
            if held_days > hi:
                notes.append(f"Held {held_days}d — longer than a {ttype} should run "
                             f"(expected ≤{hi}d). Either the label or the exit was wrong.")
            elif held_days < lo:
                notes.append(f"Held {held_days}d — cut far sooner than a {ttype} thesis "
                             f"implies (expected ≥{lo}d).")
            else:
                notes.append(f"Held {held_days}d — consistent with the {ttype} label.")
    if isinstance(conf, (int, float)):
        if conf >= 70 and pnl < 0:
            notes.append(f"High-conviction loss: {conf}/100 confidence, {pnl:+.1f}% result. "
                         "Check whether the confidence rationale was actually supported.")
        elif conf <= 45 and pnl > 0:
            notes.append(f"Low-conviction win: {conf}/100 confidence, {pnl:+.1f}%. "
                         "Possibly under-confident on this setup type.")
    reason = (exit_rec.get("reason") or "").lower()
    if "hard stop" in reason:
        notes.append("Exited on the mechanical -7% cut, not on the thesis breaking — the entry "
                     "was likely too early or the stop too tight for this name's volatility.")
    elif "trailing stop" in reason:
        notes.append("Trailing stop took the profit; thesis never invalidated.")
    verdict = "WIN" if pnl > 0 else ("FLAT" if abs(pnl) < 0.05 else "LOSS")
    return verdict, held_days, notes


def close_trade(exit_rec, now_et=None):
    """Append the outcome + grading to the trade's existing note."""
    folder = _folder()
    if folder is None:
        return None
    now_et = now_et or datetime.now(config.ET)
    ticker = exit_rec.get("ticker")
    note = note_path(ticker, exit_rec.get("opened"), folder)
    verdict, held_days, notes = _grade(exit_rec)
    try:
        pnl_pct = exit_rec.get("pnl_pct") or 0
        pnl_usd = exit_rec.get("pnl_usd") or 0
        mirror = exit_rec.get("broker") or {}
        block = [
            f"\n---\n\n## Outcome — {verdict} ({pnl_pct:+.2f}%, ${pnl_usd:+,.2f})\n\n",
            f"Closed {now_et.strftime('%Y-%m-%d %I:%M %p ET')} at "
            f"**{exit_rec.get('exit')}** — {exit_rec.get('reason')}\n\n",
            f"- Entry {exit_rec.get('entry')} → exit {exit_rec.get('exit')}"
            f"{f' · held {held_days}d' if held_days is not None else ''}\n",
            f"- Confidence at entry: {exit_rec.get('confidence', 'n/a')}\n",
        ]
        if mirror.get("mirrored"):
            block.append(f"- Paper broker position flattened (order `{mirror.get('order_id')}`)\n")
        elif mirror:
            block.append(f"- Paper broker close not mirrored: {mirror.get('reason')}\n")
        block.append("\n### What this trade teaches\n\n")
        block += [f"- {n}\n" for n in notes] or ["- nothing anomalous to flag\n"]

        if note.exists():
            text = note.read_text(encoding="utf-8")
            text = text.replace("status: OPEN", "status: CLOSED", 1)
            text = text.replace("outcome: \n", f"outcome: {verdict}\npnl_pct: {pnl_pct}\n", 1)
            note.write_text(text + "".join(block), encoding="utf-8")
        else:
            # Entry note missing (trade predates the journal) — write a stub so the
            # outcome is still recorded rather than silently dropped.
            note.write_text(
                f"---\ntags: [trading-brain, paper-trading, trade-journal]\nticker: {ticker}\n"
                f"status: CLOSED\noutcome: {verdict}\n---\n\n"
                f"# {ticker} — closed trade (entry note not found)\n\n"
                "This trade was opened before the journal existed, or its entry note was "
                "deleted. Outcome recorded from the portfolio ledger.\n"
                + "".join(block), encoding="utf-8")
        print(f"[journal] closed {ticker} {verdict} {pnl_pct:+.2f}% -> {note.name}")
        _mark_index_closed(ticker, exit_rec, verdict)
        return str(note)
    except Exception as e:  # pragma: no cover - fs
        print(f"[warn] journal close_trade {ticker} failed: {e}", file=sys.stderr)
        return None


# ── Rolling index ───────────────────────────────────────────────────────────────

_INDEX_HEADER = (
    "---\ntags: [trading-brain, paper-trading, trade-journal, index]\n---\n\n"
    "# Trade Journal Index\n\n"
    "Every paper trade the brain has taken, newest at the bottom. Each row links to the "
    "trade's own note with the full reasoning and its grading on exit.\n\n"
    "| Opened | Ticker | Type | Conf | Entry | Exit | Result | Note |\n"
    "|---|---|---|---|---|---|---|---|\n"
)


def _index_path():
    folder = _folder()
    return (folder / "Trade Journal Index.md") if folder else None


def _append_index(ticker, position, ttype, conf, note):
    p = _index_path()
    if p is None:
        return
    try:
        if not p.exists():
            p.write_text(_INDEX_HEADER, encoding="utf-8")
        dt = _parse_iso(position.get("opened"))
        stamp = dt.astimezone(config.ET).strftime("%Y-%m-%d %H:%M") if dt else "?"
        row = (f"| {stamp} | {ticker} | {ttype} | {conf if conf is not None else '—'} "
               f"| {position.get('entry')} | — | OPEN | [[{note.stem}]] |\n")
        with open(p, "a", encoding="utf-8") as f:
            f.write(row)
    except Exception as e:  # pragma: no cover - fs
        print(f"[warn] journal index append failed: {e}", file=sys.stderr)


def _mark_index_closed(ticker, exit_rec, verdict):
    """Fill the exit/result cells on the row this trade opened with."""
    p = _index_path()
    if p is None or not p.exists():
        return
    try:
        dt = _parse_iso(exit_rec.get("opened"))
        stamp = dt.astimezone(config.ET).strftime("%Y-%m-%d %H:%M") if dt else None
        lines = p.read_text(encoding="utf-8").splitlines(keepends=True)
        for i, line in enumerate(lines):
            if not line.startswith("|") or "| OPEN |" not in line:
                continue
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if cells[1] != ticker or (stamp and cells[0] != stamp):
                continue
            cells[5] = str(exit_rec.get("exit"))
            cells[6] = f"{verdict} {exit_rec.get('pnl_pct', 0):+.1f}%"
            lines[i] = "| " + " | ".join(cells) + " |\n"
            break
        p.write_text("".join(lines), encoding="utf-8")
    except Exception as e:  # pragma: no cover - fs
        print(f"[warn] journal index close failed: {e}", file=sys.stderr)


# ── Weekly recap note ───────────────────────────────────────────────────────────

def write_weekly_recap(recap, now_et=None):
    """Write the week's recap note: performance, every trade, and the accuracy changes."""
    folder = _folder(sub="")
    if folder is None:
        return None
    now_et = now_et or datetime.now(config.ET)
    day = now_et.strftime("%Y-%m-%d")
    note = Path(folder) / f"{day} - Weekly Recap.md"
    try:
        st = recap.get("stats", {})
        lines = [
            f"---\ntags: [trading-brain, paper-trading, weekly-recap]\ncreated: {day}\n",
            f"week_return_pct: {st.get('week_return_pct', 0)}\n",
            f"win_rate: {st.get('win_rate') if st.get('win_rate') is not None else ''}\n",
            "---\n\n",
            f"# Trading Week Recap — {recap.get('week_label', day)}\n\n",
            "## How the week went\n\n",
            (recap.get("narrative") or "No narrative generated.") + "\n\n",
            "## Numbers\n\n",
            f"- Equity: ${st.get('equity_start', 0):,.2f} → ${st.get('equity_end', 0):,.2f} "
            f"(**{st.get('week_return_pct', 0):+.2f}%**)\n",
            f"- Realised this week: ${st.get('realized_usd', 0):+,.2f} across "
            f"{st.get('n_closed', 0)} closed trade(s)\n",
            f"- Win rate: {st.get('win_rate')}%" if st.get("win_rate") is not None
            else "- Win rate: n/a (no closes)",
            "\n",
            f"- Opened this week: {st.get('n_opened', 0)} · still open: {st.get('n_open', 0)}\n",
            f"- Best: {st.get('best', '—')} · Worst: {st.get('worst', '—')}\n\n",
        ]

        lines.append("## Every trade this week\n\n")
        closed = recap.get("closed") or []
        if closed:
            lines.append("| Ticker | Type | Conf | Entry | Exit | P&L | Why it closed |\n")
            lines.append("|---|---|---|---|---|---|---|\n")
            for c in closed:
                lines.append(
                    f"| {c.get('ticker')} | {c.get('trade_type') or '—'} "
                    f"| {c.get('confidence') if c.get('confidence') is not None else '—'} "
                    f"| {c.get('entry')} | {c.get('exit')} "
                    f"| {c.get('pnl_pct', 0):+.1f}% (${c.get('pnl_usd', 0):+,.0f}) "
                    f"| {(c.get('reason') or '')[:70]} |\n")
        else:
            lines.append("No trades closed this week.\n")
        lines.append("\n")

        still = recap.get("open") or []
        if still:
            lines.append("### Still open\n\n")
            for o in still:
                lines.append(f"- **{o.get('ticker')}** {o.get('trade_type') or ''} — entry "
                             f"{o.get('entry')} · now {o.get('last')} "
                             f"({o.get('pnl_pct', 0):+.1f}%)\n")
            lines.append("\n")

        lines.append("## What to change to be more accurate\n\n")
        changes = recap.get("changes") or []
        lines += [f"{i}. {c}\n" for i, c in enumerate(changes, 1)] or ["- none proposed\n"]
        lines.append("\n")

        if recap.get("mistakes"):
            lines.append("## Mistakes\n\n")
            lines += [f"- {m}\n" for m in recap["mistakes"]]
            lines.append("\n")
        if recap.get("successes"):
            lines.append("## What worked\n\n")
            lines += [f"- {s}\n" for s in recap["successes"]]
            lines.append("\n")
        if recap.get("gradings"):
            lines.append("## Per-trade gradings (rules-based, not model opinion)\n\n")
            for g in recap["gradings"]:
                lines.append(f"- **{g.get('ticker')}** {g.get('verdict')}: "
                             + "; ".join(g.get("notes") or []) + "\n")
            lines.append("\n")

        lines.append("See [[Trade Journal Index]] for every trade's own note.\n\n")
        lines.append(config.DISCLAIMER + "\n")
        note.write_text("".join(lines), encoding="utf-8")
        print(f"[journal] wrote weekly recap -> {note}")
        return str(note)
    except Exception as e:  # pragma: no cover - fs
        print(f"[warn] journal weekly recap failed: {e}", file=sys.stderr)
        return None


# ── Recap assembly (deterministic; the model only writes the narrative) ─────────

def build_recap(portfolio, week_start_iso, prices=None, narrative=None, changes=None,
                mistakes=None, successes=None, week_label=None):
    """Compute the week's numbers from the ledger. Pure arithmetic — no model involved."""
    prices = prices or {}
    closed = portfolio.trades_between(week_start_iso)
    opened = portfolio.opened_between(week_start_iso)
    summary = portfolio.summary(prices)
    realized = round(sum(c.get("pnl_usd", 0) for c in closed), 2)
    wins = [c for c in closed if c.get("pnl_usd", 0) > 0]
    curve = [e for e in portfolio.state.get("equity_curve", [])
             if e.get("ts", "") >= week_start_iso]
    equity_start = curve[0]["equity"] if curve else summary["equity"]
    equity_end = summary["equity"]
    best = max(closed, key=lambda c: c.get("pnl_pct", 0), default=None)
    worst = min(closed, key=lambda c: c.get("pnl_pct", 0), default=None)
    gradings = []
    for c in closed:
        verdict, _, notes = _grade(c)
        gradings.append({"ticker": c.get("ticker"), "verdict": verdict, "notes": notes})
    return {
        "week_label": week_label or f"week of {week_start_iso[:10]}",
        "narrative": narrative,
        "changes": changes or [],
        "mistakes": mistakes or [],
        "successes": successes or [],
        "closed": closed,
        "open": summary["open_positions"],
        "gradings": gradings,
        "stats": {
            "equity_start": equity_start, "equity_end": equity_end,
            "week_return_pct": round((equity_end - equity_start) / equity_start * 100, 2)
            if equity_start else 0.0,
            "realized_usd": realized,
            "n_closed": len(closed), "n_opened": len(opened), "n_open": summary["n_open"],
            "win_rate": round(len(wins) / len(closed) * 100, 1) if closed else None,
            "best": f"{best['ticker']} {best['pnl_pct']:+.1f}%" if best else "—",
            "worst": f"{worst['ticker']} {worst['pnl_pct']:+.1f}%" if worst else "—",
            "total_return_pct": summary["total_return_pct"],
        },
    }


def recap_packet(recap):
    """Compact JSON the weekly-review model reasons over to write the narrative + changes."""
    return json.dumps({
        "stats": recap["stats"],
        "closed_trades": [{k: c.get(k) for k in
                           ("ticker", "trade_type", "confidence", "entry", "exit", "pnl_pct",
                            "reason", "thesis", "indicators_used", "opened", "closed")}
                          for c in recap["closed"]],
        "still_open": recap["open"],
        "rule_based_gradings": recap["gradings"],
    }, indent=2, default=str)
