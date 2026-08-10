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

import re
import sys
from datetime import datetime
from pathlib import Path

from . import config, jsonio

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
        if exit_rec.get("exit_kind") == "time_stop":
            # The harness closed this one *because* the horizon ran out, so grading the hold
            # against the horizon is circular — and a 24h crypto scalp lands at ~1.02 days, which
            # would otherwise be flagged as "held longer than a SCALP should run" by the very
            # mechanism that enforced the window. What is worth grading is the result.
            notes.append(f"Held {held_days}d — closed by the scalp time stop, so the horizon was "
                         f"honoured. Grade the setup, not the hold: "
                         f"{pnl:+.1f}% at the moment the clock ran out.")
        elif lo is not None:
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

def _pct(v, suffix="%"):
    return "—" if v is None else f"{v}{suffix}"


def _recap_signal_section(recap):
    """Recommended vs executed vs rejected — including how the untaken ones did."""
    sig = ((recap.get("analytics") or {}).get("signals")) or {}
    if not sig.get("n_signals"):
        return ["## Signals sent\n\nNo signals were logged this week.\n\n"]
    lines = ["## Signals sent and recommended\n\n",
             f"{sig['n_signals']} signal(s): **{sig['n_executed']} executed**, "
             f"{sig['n_rejected']} rejected by the gates, {sig['n_advisory']} advisory "
             "(flagged but never proposed as a trade).\n\n"]
    if recap.get("signal_review"):
        lines.append(recap["signal_review"] + "\n\n")
    lines.append("| Ticker | Dir | Type | Conf | Outcome | 1d | 5d | Gate said |\n")
    lines.append("|---|---|---|---|---|---|---|---|\n")
    for r in sig.get("recommended", []):
        lines.append(
            f"| {r.get('ticker')} | {r.get('direction') or '—'} | {r.get('trade_type') or '—'} "
            f"| {r.get('confidence') if r.get('confidence') is not None else '—'} "
            f"| {r.get('outcome')} | {_pct(r.get('forward_1d'))} | {_pct(r.get('forward_5d'))} "
            f"| {(r.get('gate_reason') or '')[:60]} |\n")
    lines.append("\n")
    if sig.get("rejection_reasons"):
        lines.append("**Why the gates said no:** "
                     + " · ".join(f"{r['reason']} ×{r['n']}" for r in sig["rejection_reasons"])
                     + "\n\n")
    lines.append(f"_{sig.get('gate_verdict')}_\n\n")
    return lines


def _recap_measured_section(an):
    """The five cuts, rendered. Arithmetic over the ledger — no model opinion in here."""
    if not an:
        return []
    lines = ["## Measured performance (arithmetic, not opinion)\n\n"]
    if an.get("sample_note"):
        lines.append(f"> {an['sample_note']}\n\n")

    conf = an.get("by_confidence") or []
    if conf:
        lines.append("### Was the confidence number honest?\n\n")
        lines.append("| Bucket | Trades | Won | Implied | Gap | Read |\n|---|---|---|---|---|---|\n")
        for c in conf:
            lines.append(f"| {c['bucket']} | {c['n']} | {_pct(c['win_rate'])} "
                         f"| {c['implied_win_rate']}% | {c['gap_pts']:+.1f} | {c['read']} |\n")
        lines.append("\n")

    for title, key, label in (
            ("Which indicators carried signal", "by_indicator", "Indicator"),
            ("By trade type", "by_trade_type", "Type"),
            ("How trades ended", "by_exit_kind", "Exit"),
            ("By sector", "by_sector", "Sector")):
        rows = an.get(key) or []
        if not rows:
            continue
        name_key = "indicator" if key == "by_indicator" else "name"
        lines.append(f"### {title}\n\n")
        lines.append(f"| {label} | Trades | Win rate | Avg P&L |\n|---|---|---|---|\n")
        for r in rows:
            lines.append(f"| {r[name_key]} | {r['n']} | {_pct(r['win_rate'])} "
                         f"| {_pct(r['avg_pnl_pct'])} |\n")
        lines.append("\n")
    return lines


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
            benchmark_line(st),
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

        cr = recap.get("crypto")
        if cr:
            cst = cr.get("stats", {})
            lines.append("## Crypto book\n\n")
            lines.append(f"- Equity: ${cst.get('equity_start', 0):,.2f} → "
                         f"${cst.get('equity_end', 0):,.2f} "
                         f"(**{cst.get('week_return_pct', 0):+.2f}%**)\n")
            # BTC, not SPY — see CRYPTO_RULES.benchmark for why the crypto book is graded
            # against the majors rather than against an index it does not compete with.
            lines.append(benchmark_line(cst))
            lines.append(f"- Realised ${cst.get('realized_usd', 0):+,.2f} across "
                         f"{cst.get('n_closed', 0)} closed trade(s)\n")
            lines.append(f"- Opened this week: {cst.get('n_opened', 0)} / "
                         f"{cst.get('weekly_trade_target', 0)} target · still open "
                         f"{cst.get('n_open', 0)}\n")
            for c in cr.get("closed") or []:
                lines.append(f"  - **{c.get('ticker')}** {c.get('trade_type') or '—'} "
                             f"{c.get('entry')} → {c.get('exit')} "
                             f"{c.get('pnl_pct', 0):+.1f}% — {(c.get('reason') or '')[:70]}\n")
            lines.append("\n")

        lines += _recap_signal_section(recap)
        lines += _recap_measured_section(recap.get("analytics") or {})

        lines.append("## What to change to be more accurate\n\n")
        changes = recap.get("changes") or []
        lines += [f"{i}. {c}\n" for i, c in enumerate(changes, 1)] or ["- none proposed\n"]
        lines.append("\n")

        pipe = recap.get("pipeline_changes") or []
        if pipe:
            lines.append("## Pipeline changes to implement\n\n")
            for p in pipe:
                effort = f" _({p.get('effort')})_" if p.get("effort") else ""
                lines.append(f"- [ ] **{p.get('change')}**{effort}\n")
                if p.get("why"):
                    lines.append(f"    - why: {p['why']}\n")
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

        ahead = recap.get("week_ahead") or {}
        if ahead.get("events") or ahead.get("speeches"):
            lines.append("## Week ahead\n\n")
            for e in ahead.get("events", []):
                lines.append(f"- {e.get('when')} — {e.get('title')} ({e.get('country')}) "
                             f"[{e.get('impact')}]\n")
            for s in ahead.get("speeches", []):
                lines.append(f"- 🎙 {s.get('when')} — {s.get('title')}\n")
            lines.append("\n")

        lines.append("See [[Trade Journal Index]] for every trade's own note.\n\n")
        lines.append(config.DISCLAIMER + "\n")
        note.write_text("".join(lines), encoding="utf-8")
        print(f"[journal] wrote weekly recap -> {note}")
        return str(note)
    except Exception as e:  # pragma: no cover - fs
        print(f"[warn] journal weekly recap failed: {e}", file=sys.stderr)
        return None


# ── Week analytics (the five cuts the ledger already pays for) ──────────────────
#
# Every field these read was already being captured and then thrown away at recap time. None of
# this costs an API call or a model token: it is arithmetic over closed_trades and the signal
# ledger. The point is that "what should we change to be more accurate" stops being the model's
# guess about its own behaviour and starts being a measurement of it.

_CONF_BUCKETS = ((80, 100, "80-100"), (70, 79, "70-79"), (60, 69, "60-69"),
                 (50, 59, "50-59"), (0, 49, "0-49"))
_MIN_SAMPLE = 4          # below this, a rate is noise wearing a percentage sign


def _bucket_stats(rows):
    """(n, win_rate, avg_pnl) over a group of closed trades."""
    n = len(rows)
    if not n:
        return {"n": 0, "win_rate": None, "avg_pnl_pct": None}
    wins = sum(1 for r in rows if (r.get("pnl_pct") or 0) > 0)
    return {"n": n,
            "win_rate": round(wins / n * 100, 1),
            "avg_pnl_pct": round(sum(r.get("pnl_pct") or 0 for r in rows) / n, 2)}


def _group(closed, key):
    """Group closed trades by a single-valued key, skipping the ones that never recorded it."""
    buckets = {}
    for c in closed:
        k = key(c)
        if k:
            buckets.setdefault(k, []).append(c)
    return sorted(({"name": k, **_bucket_stats(v)} for k, v in buckets.items()),
                  key=lambda r: (-r["n"], r["name"]))


def indicator_hit_rate(closed):
    """Win rate per indicator the analyst *claimed* to have used.

    One trade contributes to several indicators, so these do not sum to the trade count. That is
    the intended read: "breakouts you called on RSI lost, the ones you called on volume won".
    """
    per = {}
    for c in closed:
        for ind in (c.get("indicators_used") or []):
            name = str(ind).strip()
            if name:
                per.setdefault(name, []).append(c)
    return sorted(({"indicator": k, **_bucket_stats(v)} for k, v in per.items()),
                  key=lambda r: (-r["n"], r["indicator"]))


def confidence_calibration(closed):
    """Does a 75 actually win three times in four?

    `confidence` is not literally a probability, but it is the number the analyst attaches to
    every trade, so treating the bucket midpoint as the implied hit rate is the only honest way
    to check whether it means anything. A persistent gap is the most actionable thing in the
    whole recap: it is a number the next week's prompt can be told to correct by.
    """
    out = []
    for lo, hi, label in _CONF_BUCKETS:
        rows = [c for c in closed
                if isinstance(c.get("confidence"), (int, float)) and lo <= c["confidence"] <= hi]
        st = _bucket_stats(rows)
        if not st["n"]:
            continue
        implied = (lo + hi) / 2
        st.update({"bucket": label, "implied_win_rate": implied,
                   "gap_pts": round(st["win_rate"] - implied, 1)})
        st["read"] = ("too few trades to judge" if st["n"] < _MIN_SAMPLE
                      else "overconfident" if st["gap_pts"] <= -10
                      else "underconfident" if st["gap_pts"] >= 10
                      else "well calibrated")
        out.append(st)
    return out


def _direction_adjusted(fwd, direction):
    """A SHORT that falls 3% was right. Raw forward return would score that as a loss."""
    if fwd is None:
        return None
    return -fwd if (direction or "").upper() == "SHORT" else fwd


def signal_review(signals):
    """What was recommended, what the gates let through, and whether they were right.

    Since the ledger backfills forward returns onto rejections too, this is the one place that
    can answer the question the old recap could not: the trades we did NOT take — how did they
    do? A gate that keeps blocking winners is a gate to argue with.
    """
    signals = signals or []
    by_outcome = {"executed": [], "rejected": [], "advisory": []}
    for s in signals:
        by_outcome.setdefault(s.get("outcome") or "advisory", []).append(s)

    reasons = {}
    for s in by_outcome.get("rejected", []):
        key = (s.get("gate_reason") or "unstated").split(" (")[0][:80]
        reasons[key] = reasons.get(key, 0) + 1

    def _fwd(rows, horizon):
        vals = [_direction_adjusted((r.get("forward") or {}).get(horizon), r.get("direction"))
                for r in rows]
        vals = [v for v in vals if v is not None]
        return round(sum(vals) / len(vals), 2) if vals else None

    not_taken = by_outcome.get("rejected", []) + by_outcome.get("advisory", [])
    return {
        "n_signals": len(signals),
        "n_executed": len(by_outcome.get("executed", [])),
        "n_rejected": len(by_outcome.get("rejected", [])),
        "n_advisory": len(by_outcome.get("advisory", [])),
        "rejection_reasons": sorted(({"reason": k, "n": v} for k, v in reasons.items()),
                                    key=lambda r: -r["n"]),
        "recommended": [
            {"ticker": s.get("ticker"), "direction": s.get("direction"),
             "trade_type": s.get("trade_type"), "confidence": s.get("confidence"),
             "outcome": s.get("outcome"), "gate_reason": s.get("gate_reason"),
             "why": (s.get("why") or "")[:200],
             "forward_1d": (s.get("forward") or {}).get("1d"),
             "forward_5d": (s.get("forward") or {}).get("5d")}
            for s in signals],
        "executed_forward_1d": _fwd(by_outcome.get("executed", []), "1d"),
        "not_taken_forward_1d": _fwd(not_taken, "1d"),
        "not_taken_forward_5d": _fwd(not_taken, "5d"),
        "gate_verdict": _gate_verdict(by_outcome.get("executed", []), not_taken, _fwd),
    }


def _gate_verdict(executed, not_taken, fwd):
    """One sentence on whether skipping was the right call — or 'not enough' when it is not knowable."""
    if len(not_taken) < _MIN_SAMPLE:
        return f"only {len(not_taken)} signal(s) went untaken — too few to judge the gates"
    took, skipped = fwd(executed, "1d"), fwd(not_taken, "1d")
    if skipped is None:
        return "forward returns not backfilled yet for the untaken signals"
    if took is None:
        return f"nothing executed to compare against; untaken signals averaged {skipped:+.2f}% at 1d"
    if skipped > took:
        return (f"the signals we skipped averaged {skipped:+.2f}% at 1d vs {took:+.2f}% for the "
                "ones taken — the gates cost performance this week")
    return (f"the signals we skipped averaged {skipped:+.2f}% at 1d vs {took:+.2f}% for the ones "
            "taken — the gates helped this week")


def week_analytics(closed, signals=None):
    """The five cuts, plus an honest note when the sample is too thin to mean anything."""
    return {
        "n_closed": len(closed),
        "sample_note": (f"{len(closed)} closed trade(s) — below {_MIN_SAMPLE}, so every rate "
                        "below is descriptive, not evidence")
        if len(closed) < _MIN_SAMPLE else None,
        "by_indicator": indicator_hit_rate(closed),
        "by_confidence": confidence_calibration(closed),
        "by_trade_type": _group(closed, lambda c: (c.get("trade_type") or "").upper() or None),
        "by_exit_kind": _group(closed, lambda c: c.get("exit_kind") or "model_sell"),
        "by_sector": _group(closed, lambda c: c.get("sector")),
        "signals": signal_review(signals),
    }


# ── Recap assembly (deterministic; the model only writes the narrative) ─────────

def benchmark_line(stats):
    """One markdown line stating whether the book beat its index — or why that is unknown.

    Shared by the Obsidian recap note and the weekly email so the two can never quote different
    arithmetic at the same week. Always returns a line: "not measurable" is itself the finding
    when the ledger has not been carrying benchmark prices long enough.
    """
    b = (stats or {}).get("benchmark") or {}
    if not b.get("available"):
        return f"- vs benchmark: not measurable — {b.get('reason', 'no benchmark data')}\n"
    verdict = "**beat**" if b.get("beat") else "**trailed**"
    return (f"- vs {b['ticker']}: book {b['book_return_pct']:+.2f}% vs index "
            f"{b['benchmark_return_pct']:+.2f}% → {verdict} by "
            f"**{abs(b['alpha_pct']):.2f} pts** (alpha {b['alpha_pct']:+.2f})\n")


def _benchmark_leg(curve, ticker):
    """What the index did over the same window, and what the book did against it.

    Lind's goal for this whole system is stated in one line — "the main goal is to beat the
    s&p500 cause that is what most traders invest in" — and until now nothing computed it. The
    ledger reported `week_return_pct` and `total_return_pct`, both absolute, both unable to
    distinguish a good week from a rising tide. A +4% week against a +6% SPY is a losing week and
    read as a winning one.

    The comparison is drawn from the equity curve itself, where `mark_to_market` stamps the
    benchmark's price onto the same point as the equity it is measured against. That matters more
    than it sounds: the alternative is fetching "SPY over roughly this week" separately, which
    quietly compares two different windows and produces an alpha number nobody can reconstruct.

    Both legs are measured across the *paired* points — first and last cycle that carried a
    benchmark quote — rather than the full week. A week that only started stamping the benchmark
    on Wednesday gets a Wednesday-to-Friday alpha and says so in `window_points`, instead of
    silently comparing five days of equity against two of index.

    Returns `available: False` with a `reason` when it cannot be computed, never a zero. A book
    that could not measure its benchmark has not tied with it.
    """
    if not ticker:
        return {"available": False, "reason": "book has no benchmark configured"}
    pts = [e for e in (curve or []) if e.get("bench") and e.get("equity")]
    if len(pts) < 2:
        return {"available": False, "ticker": ticker, "window_points": len(pts),
                "reason": (f"fewer than two cycles this window carried a {ticker} quote — "
                           "the comparison needs a start and an end, and points written before "
                           "benchmark tracking shipped do not have one")}
    b_start, b_end = pts[0]["bench"], pts[-1]["bench"]
    e_start, e_end = pts[0]["equity"], pts[-1]["equity"]
    if not b_start or not e_start:
        return {"available": False, "ticker": ticker, "reason": "zero price or equity at window start"}
    bench_pct = round((b_end - b_start) / b_start * 100, 2)
    book_pct = round((e_end - e_start) / e_start * 100, 2)
    return {
        "available": True,
        "ticker": ticker,
        "benchmark_return_pct": bench_pct,
        # The book's return over the benchmark's exact window, which is not always the week's
        # headline `week_return_pct` — quote this one whenever quoting alpha, or the subtraction
        # does not hold.
        "book_return_pct": book_pct,
        "alpha_pct": round(book_pct - bench_pct, 2),
        "beat": book_pct > bench_pct,
        "window_points": len(pts),
        "window": [pts[0].get("ts"), pts[-1].get("ts")],
    }


def build_recap(portfolio, week_start_iso, prices=None, narrative=None, changes=None,
                mistakes=None, successes=None, week_label=None, signals=None,
                week_ahead=None, pipeline_changes=None):
    """Compute the week's numbers from the ledger. Pure arithmetic — no model involved.

    Book-agnostic: every rule it reads comes off `portfolio.rules`, so the same function produces
    the equity recap and the crypto one. It used to read `config.WEEKLY_TRADE_TARGET` directly,
    which would have graded the crypto book's three-trade pace against the equity book's five.
    """
    prices = prices or {}
    rules = getattr(portfolio, "rules", None) or config.STOCK_RULES
    closed = portfolio.trades_between(week_start_iso)
    opened = portfolio.opened_between(week_start_iso)
    summary = portfolio.summary(prices)
    realized = round(sum(c.get("pnl_usd", 0) for c in closed), 2)
    wins = [c for c in closed if c.get("pnl_usd", 0) > 0]
    curve = [e for e in portfolio.state.get("equity_curve", [])
             if e.get("ts", "") >= week_start_iso]
    equity_start = curve[0]["equity"] if curve else summary["equity"]
    equity_end = summary["equity"]
    bench = _benchmark_leg(curve, getattr(rules, "benchmark", None))
    best = max(closed, key=lambda c: c.get("pnl_pct", 0), default=None)
    worst = min(closed, key=lambda c: c.get("pnl_pct", 0), default=None)
    gradings = []
    for c in closed:
        verdict, _, notes = _grade(c)
        gradings.append({"ticker": c.get("ticker"), "verdict": verdict, "notes": notes})
    # The week's shape, not only its size. A book that opened five trades is on pace by the count
    # and can still have taken five scalps — which is exactly the history here: zero LONG_TERM
    # trades in 805 signals. Counted off `opened` rather than the live summary so the recap of a
    # past week reports that week.
    mix_opened = {}
    for o in opened:
        h = config.normalize_trade_type(o.get("trade_type")) or "UNSPECIFIED"
        mix_opened[h] = mix_opened.get(h, 0) + 1
    return {
        "week_label": week_label or f"week of {week_start_iso[:10]}",
        "book": rules.name,
        "narrative": narrative,
        "changes": changes or [],
        "pipeline_changes": pipeline_changes or [],
        "mistakes": mistakes or [],
        "successes": successes or [],
        "closed": closed,
        "open": summary["open_positions"],
        "gradings": gradings,
        "analytics": week_analytics(closed, signals),
        "week_ahead": week_ahead or {},
        "stats": {
            "equity_start": equity_start, "equity_end": equity_end,
            "week_return_pct": round((equity_end - equity_start) / equity_start * 100, 2)
            if equity_start else 0.0,
            "realized_usd": realized,
            "n_closed": len(closed), "n_opened": len(opened), "n_open": summary["n_open"],
            # Activity against the pace target, so the review judges how much the brain traded
            # as well as how well. A week under target is not automatically a failure — a dead
            # tape is a real answer — but it is a question the review has to answer explicitly.
            "weekly_trade_target": rules.weekly_trade_target,
            "pace_gap": len(opened) - rules.weekly_trade_target,
            "mix_opened": mix_opened,
            "mix_target": dict(rules.weekly_mix),
            "mix_gap": rules.mix_gap(mix_opened),
            "win_rate": round(len(wins) / len(closed) * 100, 1) if closed else None,
            "best": f"{best['ticker']} {best['pnl_pct']:+.1f}%" if best else "—",
            "worst": f"{worst['ticker']} {worst['pnl_pct']:+.1f}%" if worst else "—",
            "total_return_pct": summary["total_return_pct"],
            # The only number on this block that says whether the week was actually any good.
            # See _benchmark_leg — absent rather than zero when it could not be measured.
            "benchmark": bench,
        },
    }


# What each closed trade carries into the review. The old whitelist stopped at 11 keys and so
# the reviewer was asked "why were these calls wrong?" without being shown the confidence
# rationale that justified them, the news read that moved them, or how they actually exited.
_RECAP_TRADE_FIELDS = (
    "ticker", "sector", "direction", "trade_type", "holding_period", "confidence",
    "confidence_rationale", "entry", "exit", "stop", "target1", "target2",
    "pnl_pct", "pnl_usd", "reason", "exit_kind", "thesis", "why",
    "indicators_used", "chart_read", "news_read", "news_edge", "historical_analog",
    "analysis_done", "data_sources", "opened", "closed",
)


def _packet_book(recap):
    """One book's numbers, in the shape the reviewer reads them."""
    return {
        "book": recap.get("book"),
        "stats": recap["stats"],
        "closed_trades": [{k: c.get(k) for k in _RECAP_TRADE_FIELDS if c.get(k) is not None}
                          for c in recap["closed"]],
        "still_open": recap["open"],
        "rule_based_gradings": recap["gradings"],
        # Measured, not remembered: the reviewer sees its own hit rates rather than
        # reconstructing them from the trade list and guessing.
        "measured_performance": recap.get("analytics") or {},
    }


def recap_packet(recap):
    """Compact JSON the weekly-review model reasons over to write the narrative + changes.

    The equity book stays at the top level — the keys the review prompt names have not moved —
    and the crypto book rides alongside under `crypto_book` when the recap carries one. Both are
    in the same packet on purpose: they are one week of one operator's decisions, and a lesson
    learned on a crypto scalp usually applies to an equity one.
    """
    body = dict(_packet_book(recap))
    body["week_ahead"] = recap.get("week_ahead") or {}
    if recap.get("crypto"):
        body["crypto_book"] = _packet_book(recap["crypto"])
    return jsonio.dumps(body, indent=2, default=str)
