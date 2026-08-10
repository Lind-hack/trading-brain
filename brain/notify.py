"""Email delivery — the detailed trade breakdown Lind asked for.

Every trade card states, in depth: the trade TYPE (scalp / short-term / long-term with the
holding period), why it's a good trade, what analysis was done, the indicator values, the
chart read, the news scraped (with links), any historical analog, and which data
sources / APIs fed it. Rendered from the deep-run JSON produced by deep.py.

Gmail SMTP, same transport as the stock engine. Paper-trading disclaimer on every send.
"""
from __future__ import annotations

import smtplib
import sys
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from . import config

_TYPE_META = {
    "SCALP": ("⚡ SCALP", "#f59e0b", "minutes–hours", "Quick in-and-out on momentum. Small size, tight stop — and the harness force-closes it when the clock runs out, win or lose."),
    "SHORT_TERM": ("📈 SHORT-TERM", "#3b82f6", "days–weeks", "A swing trade riding a multi-day move. Manage with the trailing stop."),
    "LONG_TERM": ("🏦 LONG-TERM INVESTMENT", "#a855f7", "months+", "A position you benefit from holding over a long period — thesis-driven, wider stop."),
}


def _row(k, v):
    return (f'<tr><td style="padding:5px 0;color:#9ca3af;font-size:13px;vertical-align:top;">{k}</td>'
            f'<td style="padding:5px 0;color:#e5e7eb;font-size:13px;text-align:right;padding-left:16px;">{v}</td></tr>')


def _section(title, body, color="#60a5fa"):
    return (f'<p style="margin:18px 0 6px;font-size:11px;font-weight:700;letter-spacing:0.08em;'
            f'text-transform:uppercase;color:{color};">{title}</p>{body}')


def _news_html(news):
    if not news:
        return '<p style="margin:0;font-size:13px;color:#6b7280;">No fresh headlines matched this name.</p>'
    items = []
    for n in news[:5]:
        title = n.get("title", "").strip()
        src = n.get("source", "")
        link = n.get("link", "")
        take = n.get("takeaway", "")
        head = f'<a href="{link}" style="color:#93c5fd;text-decoration:none;">{title}</a>' if link else title
        meta = f' <span style="color:#6b7280;">— {src}</span>' if src else ""
        tk = f'<br><span style="color:#9ca3af;">↳ {take}</span>' if take else ""
        items.append(f'<li style="margin:6px 0;font-size:13px;color:#d1d5db;">{head}{meta}{tk}</li>')
    return f'<ul style="margin:0;padding-left:18px;">{"".join(items)}</ul>'


def _chips(items, color="#60a5fa"):
    if not items:
        return '<p style="margin:0;font-size:13px;color:#6b7280;">none recorded</p>'
    return "".join(
        f'<span style="display:inline-block;background:{color}1f;color:{color};font-size:11px;'
        f'font-weight:700;padding:3px 9px;border-radius:999px;margin:0 5px 5px 0;'
        f'font-family:monospace;">{i}</span>' for i in items[:10])


def _conf_bar(conf):
    """Visual confidence bar — colour-coded so a 40 doesn't read like a 90 at a glance."""
    if not isinstance(conf, (int, float)):
        return ""
    pct = max(0, min(100, int(conf)))
    color = "#22c55e" if pct >= 70 else ("#f59e0b" if pct >= 50 else "#ef4444")
    return (f'<div style="background:#1f2937;border-radius:999px;height:7px;margin:6px 0 10px;">'
            f'<div style="width:{pct}%;background:{color};height:7px;border-radius:999px;"></div></div>')


def _intel_html(sig, intel_ticker=None):
    """What Haiku 4.5 concluded for this name — the tier-1 read the trade was built on."""
    ni = intel_ticker or {}
    parts = []
    if sig.get("news_read"):
        parts.append(f'<p style="margin:0 0 8px;font-size:13px;color:#d1d5db;">{sig["news_read"]}</p>')
    if ni:
        sent = int(ni.get("sentiment") or 0)
        scolor = "#22c55e" if sent > 0 else ("#ef4444" if sent < 0 else "#9ca3af")
        mat = (ni.get("materiality") or "low").upper()
        mcolor = {"HIGH": "#f59e0b", "MEDIUM": "#60a5fa"}.get(mat, "#6b7280")
        stale = ("" if ni.get("is_fresh", True) else
                 ' · <span style="color:#f59e0b;">recycled news</span>')
        parts.append(
            f'<p style="margin:0 0 8px;font-size:12px;color:#9ca3af;">'
            f'sentiment <span style="color:{scolor};font-weight:700;">{sent:+d}</span> · '
            f'materiality <span style="color:{mcolor};font-weight:700;">{mat}</span> · '
            f'catalyst <span style="color:#e5e7eb;">{ni.get("catalyst","none")}</span>'
            f'{stale}</p>')
        if ni.get("summary"):
            parts.append(f'<p style="margin:0;font-size:13px;color:#d1d5db;">↳ {ni["summary"]}</p>')
    if not parts:
        parts.append('<p style="margin:0;font-size:13px;color:#6b7280;">'
                     'No news read for this name this cycle.</p>')
    return "".join(parts)


def build_card(sig, now_et, intel_ticker=None, watch_bar=None):
    """One signal, rendered.

    `watch_bar` is the execute threshold when this card is a *watch* rather than a trade — the
    idea cleared the floor worth reading but not the bar `portfolio.validate_action` requires, so
    the harness will refuse it. Passing the number rather than a boolean means the card can say
    what it missed by, which is the part that teaches. `None` renders an ordinary trade card.
    """
    ttype = (sig.get("trade_type") or "SHORT_TERM").upper()
    label, tcolor, default_hold, blurb = _TYPE_META.get(ttype, _TYPE_META["SHORT_TERM"])
    direction = (sig.get("direction") or "LONG").upper()
    dcolor = "#22c55e" if direction == "LONG" else "#ef4444"
    demoji = "🟢" if direction == "LONG" else "🔴"
    ticker = sig.get("ticker", "?")
    conf = sig.get("confidence")
    hold = sig.get("holding_period") or default_hold

    def fmt(x):
        # Not `:,.2f`: BONK trades at $0.00000296, and two decimals renders every number on its
        # card as "0.00". See config.format_price.
        if not isinstance(x, (int, float)) or isinstance(x, bool):
            return "—"
        return config.format_price(x)

    plan_rows = "".join([
        _row("Entry", fmt(sig.get("entry"))),
        _row("Stop", fmt(sig.get("stop"))),
        _row("Target 1", fmt(sig.get("target1"))),
        _row("Target 2", fmt(sig.get("target2"))),
    ])

    data_sources = ", ".join(sig.get("data_sources", []) or ["yfinance"])
    analog = sig.get("historical_analog")
    analog_html = (f'<p style="margin:0;font-size:13px;color:#d1d5db;">{analog}</p>'
                   if analog else
                   '<p style="margin:0;font-size:13px;color:#6b7280;">No matching precedent in the event log yet.</p>')

    # A watch is bordered amber and says so above the ticker, because the one thing that must not
    # happen is Lind reading a card the book already declined to trade as a trade it took.
    border = "#f59e0b" if watch_bar else dcolor
    watch_tag = ("" if not watch_bar else
                 f'<div style="display:inline-block;background:#f59e0b;color:#0a0a0a;font-weight:800;'
                 f'font-size:12px;padding:5px 12px;border-radius:999px;letter-spacing:0.04em;'
                 f'margin-right:6px;">WATCH ONLY</div>')
    watch_note = ("" if not watch_bar else
                  f'<p style="margin:0 0 14px;font-size:12px;color:#f59e0b;">Not taken — '
                  f'{watch_bar} confidence required to execute. Watch it; if the evidence '
                  f'improves it comes back as a trade.</p>')

    return f"""
  <tr><td style="background:#111;border:1px solid {border}55;border-radius:14px;padding:24px 26px;">
    {watch_tag}<div style="display:inline-block;background:{tcolor};color:#0a0a0a;font-weight:800;font-size:12px;
         padding:5px 12px;border-radius:999px;letter-spacing:0.04em;">{label} · {hold}</div>
    <p style="margin:10px 0 2px;font-size:12px;color:#9ca3af;">{blurb}</p>
    <h1 style="margin:12px 0 2px;font-size:30px;font-weight:800;color:{dcolor};">{demoji} {ticker} {direction}</h1>
    <p style="margin:0 0 14px;font-size:12px;color:#6b7280;">
      {now_et.strftime('%a %b %d, %I:%M %p ET')} · confidence {conf if conf is not None else '—'}/100 · data delayed ~15 min</p>
    {watch_note}

    {_section("Why this is a good trade", f'<p style="margin:0;font-size:13px;color:#d1d5db;">{sig.get("why","—")}</p>', dcolor)}
    {_section("How confident, and why", _conf_bar(conf) + f'<p style="margin:0;font-size:13px;color:#d1d5db;">{sig.get("confidence_rationale","No rationale given.")}</p>')}
    {_section("Trade plan", f'<table width="100%" cellpadding="0" cellspacing="0">{plan_rows}</table>')}
    {_section("What analysis was done", f'<p style="margin:0;font-size:13px;color:#d1d5db;">{sig.get("analysis_done","—")}</p>')}
    {_section("Indicators used", _chips(sig.get("indicators_used")) + f'<p style="margin:8px 0 0;font-size:13px;color:#e5e7eb;font-family:monospace;">{sig.get("indicators","—")}</p>')}
    {_section("What the charts showed", f'<p style="margin:0;font-size:13px;color:#d1d5db;">{sig.get("chart_read","—")}</p>')}
    {_section("News read by Haiku 4.5", _intel_html(sig, intel_ticker), "#f59e0b")}
    {_section("Headlines scraped", _news_html(sig.get("news")))}
    {_section("Historical pattern (news/chart analog)", analog_html)}
    {_section("Data sources / APIs used", f'<p style="margin:0;font-size:12px;color:#9ca3af;font-family:monospace;">{data_sources}</p>')}

    <p style="margin:20px 0 0;padding-top:14px;border-top:1px solid #ffffff14;font-size:11px;color:#4b5563;">
      {config.DISCLAIMER}
    </p>
  </td></tr>
  <tr><td style="height:16px;"></td></tr>"""


def _clock(p):
    """The scalp's remaining runway, or nothing at all for the other horizons.

    Only a scalp carries `scalp_hours_left`; every other position gets None from the ledger, so
    this stays out of the way on a card that has no clock to show.
    """
    left = p.get("scalp_hours_left")
    if left is None:
        return ""
    color = "#ef4444" if left <= 2 else "#f59e0b"
    text = f"{left:.0f}h left" if left >= 1 else f"{left * 60:.0f}m left"
    return (f' <span style="color:{color};font-size:11px;font-weight:700;">⚡ {text}</span>')


def _portfolio_card(psum):
    if not psum:
        return ""
    rows = "".join(
        f'<tr><td style="padding:4px 0;font-size:13px;color:#e5e7eb;">{p["ticker"]}{_clock(p)}</td>'
        f'<td style="padding:4px 0;font-size:13px;text-align:right;color:{"#22c55e" if p["pnl_pct"]>=0 else "#ef4444"};">'
        f'{p["pnl_pct"]:+.1f}%</td>'
        f'<td style="padding:4px 0;font-size:13px;text-align:right;color:#9ca3af;">${p["value"]:,.0f}</td></tr>'
        for p in psum.get("open_positions", []))
    if not rows:
        rows = '<tr><td colspan="3" style="padding:4px 0;font-size:13px;color:#6b7280;">No open positions.</td></tr>'
    ret = psum.get("total_return_pct", 0)
    rc = "#22c55e" if ret >= 0 else "#ef4444"
    # The book's own caps, falling back to the equity constants only for a summary written before
    # there was a second book. Showing 8 positions to a crypto book capped at 4 is just wrong.
    max_pos = psum.get("max_positions") or config.MAX_POSITIONS
    max_new = psum.get("max_new_trades_per_week") or config.MAX_NEW_TRADES_PER_WEEK
    title = ("Crypto paper portfolio (simulated)" if psum.get("book") == "crypto"
             else "Paper portfolio (simulated)")
    # The week's shape beside its count, once there is a week to describe. A book sitting at 4/6
    # reads as on pace and can be four scalps — the horizon that is missing only shows here.
    by_type = psum.get("new_trades_by_type") or {}
    mix = f" ({_mix_parts(by_type)})" if by_type else ""
    return f"""
  <tr><td style="background:#0f172a;border:1px solid #1e293b;border-radius:14px;padding:22px 26px;">
    <p style="margin:0 0 4px;font-size:11px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;color:#60a5fa;">{title}</p>
    <p style="margin:0;font-size:26px;font-weight:800;color:#fff;">${psum.get("equity",0):,.0f}
      <span style="font-size:15px;color:{rc};">({ret:+.1f}%)</span></p>
    <p style="margin:2px 0 12px;font-size:12px;color:#6b7280;">
      cash ${psum.get("cash",0):,.0f} · {psum.get("n_open",0)}/{max_pos} positions ·
      {psum.get("new_trades_this_week",0)}/{max_new} new trades this week{mix} ·
      win rate {psum.get("win_rate") if psum.get("win_rate") is not None else "—"}%</p>
    <table width="100%" cellpadding="0" cellspacing="0">{rows}</table>
  </td></tr>
  <tr><td style="height:16px;"></td></tr>"""


def _macro_card(intel):
    """Tier-1 macro read: what Haiku says the whole tape's news flow means right now."""
    if not intel or not intel.get("macro_read"):
        return ""
    sent = intel.get("macro_sentiment") or 0
    scolor = "#22c55e" if sent > 0 else ("#ef4444" if sent < 0 else "#9ca3af")
    model = intel.get("model") or "unknown"
    warn = ('<p style="margin:8px 0 0;font-size:12px;color:#f59e0b;">No AI news read this cycle — '
            'keyword scoring only. Treat the news layer as unconfirmed.</p>'
            if intel.get("degraded") else "")
    story_items = []
    for s in (intel.get("top_stories") or [])[:4]:
        title = (s.get("title") or "").strip()
        link = s.get("link") or ""
        head = f'<a href="{link}" style="color:#93c5fd;text-decoration:none;">{title}</a>' if link else title
        why = s.get("why_it_matters") or ""
        tail = f' <span style="color:#6b7280;">— {why}</span>' if why else ""
        story_items.append(f'<li style="margin:5px 0;font-size:12px;color:#d1d5db;">{head}{tail}</li>')
    stories_html = (f'<ul style="margin:8px 0 0;padding-left:18px;">{"".join(story_items)}</ul>'
                    if story_items else "")
    return f"""
  <tr><td style="background:#141005;border:1px solid #f59e0b44;border-radius:14px;padding:20px 24px;">
    <p style="margin:0 0 6px;font-size:11px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;color:#f59e0b;">
      News read — tier 1 · {model}</p>
    <p style="margin:0;font-size:13px;color:#e5e7eb;line-height:1.5;">{intel.get("macro_read")}</p>
    <p style="margin:8px 0 0;font-size:12px;color:#9ca3af;">macro news sentiment
      <span style="color:{scolor};font-weight:700;">{sent:+d}</span></p>
    {stories_html}{warn}
  </td></tr>
  <tr><td style="height:16px;"></td></tr>"""


def _applied_card(applied, exits, broker_info=None):
    """What the harness actually executed this run — the audit trail, not the proposal."""
    if not (applied or exits):
        return ""
    rows = []
    for e in exits or []:
        c = "#22c55e" if (e.get("pnl_pct") or 0) >= 0 else "#ef4444"
        rows.append(f'<li style="margin:6px 0;font-size:13px;color:#d1d5db;">'
                    f'<b style="color:{c};">CLOSED {e.get("ticker")}</b> '
                    f'<span style="color:{c};">{e.get("pnl_pct",0):+.1f}% '
                    f'(${e.get("pnl_usd",0):+,.0f})</span> — {e.get("reason","")}</li>')
    for a in applied or []:
        rows.append(f'<li style="margin:6px 0;font-size:13px;color:#d1d5db;">{a}</li>')
    bline = ""
    if broker_info:
        if broker_info.get("enabled") and not broker_info.get("error"):
            bline = (f'<p style="margin:10px 0 0;font-size:12px;color:#6b7280;">'
                     f'Mirrored to {broker_info.get("detail")} · '
                     f'market {"open" if broker_info.get("market_open") else "closed"}</p>')
        else:
            bline = (f'<p style="margin:10px 0 0;font-size:12px;color:#6b7280;">'
                     f'{broker_info.get("detail")}</p>')
    return f"""
  <tr><td style="background:#0b1220;border:1px solid #22c55e44;border-radius:14px;padding:20px 24px;">
    <p style="margin:0 0 6px;font-size:11px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;color:#22c55e;">
      Trades taken this run (paper)</p>
    <ul style="margin:0;padding-left:18px;">{"".join(rows)}</ul>{bline}
  </td></tr>
  <tr><td style="height:16px;"></td></tr>"""


_EXIT_META = {
    "EXIT": ("🚨 CONSIDER CLOSING", "#ef4444"),
    "TRIM": ("✂️ CONSIDER TRIMMING", "#f59e0b"),
    "WATCH": ("👁 WATCH THIS POSITION", "#60a5fa"),
}
_EXIT_KIND = {"news": "news", "macro": "macro", "chart": "chart", "peaked": "the move",
              "calendar": "calendar"}


def _exit_alerts_card(alerts):
    """The open book's exit warnings, at the top of the email where a warning belongs.

    Deliberately worded as a recommendation throughout — "consider closing", never "closed". The
    only thing that ever shuts a position without Lind is a mechanical stop, and an email that
    reads like an execution report would make it impossible to tell the two apart at a glance.
    """
    if not alerts:
        return ""
    blocks = []
    for a in alerts:
        label, color = _EXIT_META.get(a.get("verdict"), _EXIT_META["WATCH"])
        pnl = a.get("pnl_pct") or 0
        pnl_color = "#22c55e" if pnl >= 0 else "#ef4444"
        kinds = " · ".join(_EXIT_KIND.get(k, k) for k in (a.get("kinds") or []))
        reasons = "".join(
            f'<li style="margin:5px 0;font-size:13px;color:#d1d5db;">{r}</li>'
            for r in (a.get("reasons") or [])[:6])
        levels = []
        if a.get("stop_level"):
            levels.append(f'stop {config.format_price(a["stop_level"])}')
        if a.get("target1"):
            levels.append(f'T1 {config.format_price(a["target1"])}')
        levels.append(f'peak {a.get("peak_pct", 0):+.1f}%')
        blocks.append(f"""
    <div style="margin:0 0 14px;padding:0 0 12px;border-bottom:1px solid #ffffff10;">
      <p style="margin:0 0 4px;font-size:14px;">
        <b style="color:{color};">{label} {a.get("ticker")}</b>
        <span style="color:#6b7280;font-size:12px;"> {(a.get("trade_type") or "").upper()}
          {a.get("direction","LONG")} · {kinds}</span></p>
      <p style="margin:0 0 6px;font-size:13px;color:#9ca3af;">
        entry {config.format_price(a.get("entry"))} → {config.format_price(a.get("last"))}
        <span style="color:{pnl_color};font-weight:700;">{pnl:+.1f}%</span>
        <span style="color:#6b7280;"> · {" · ".join(levels)}</span></p>
      <ul style="margin:0;padding-left:18px;">{reasons}</ul>
    </div>""")
    worst = max((_EXIT_META.get(a.get("verdict"), _EXIT_META["WATCH"])[1] for a in alerts),
                key=lambda c: {"#ef4444": 3, "#f59e0b": 2, "#60a5fa": 1}.get(c, 0))
    return f"""
  <tr><td style="background:#160b0b;border:1px solid {worst}55;border-radius:14px;padding:20px 24px;">
    <p style="margin:0 0 12px;font-size:11px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;color:{worst};">
      Exit monitor — open positions</p>
    {"".join(blocks)}
    <p style="margin:10px 0 0;font-size:11px;color:#6b7280;">
      Nothing here was closed. These are alerts on positions still open — only the mechanical
      stops (hard, trailing, scalp clock) exit without you.</p>
  </td></tr>
  <tr><td style="height:16px;"></td></tr>"""


def _exit_alerts_text(alerts):
    """Same, for the plaintext body."""
    if not alerts:
        return []
    lines = ["EXIT MONITOR — OPEN POSITIONS (nothing closed; alerts only):"]
    for a in alerts:
        lines.append(f"  [{a.get('verdict')}] {a.get('ticker')} "
                     f"{(a.get('trade_type') or '').upper()} {a.get('direction','LONG')} — "
                     f"{config.format_price(a.get('entry'))} → {config.format_price(a.get('last'))} "
                     f"({a.get('pnl_pct',0):+.1f}%, peak {a.get('peak_pct',0):+.1f}%)")
        for r in (a.get("reasons") or [])[:6]:
            lines.append(f"     - {r}")
    return lines + [""]


def _below_bar_card(dropped, rules):
    """One line for the ideas that didn't clear the floor.

    They get named and counted rather than carded. Dropping them without a word would make the
    email look like the analyst saw less than it did; giving each a full card would bury the two
    that matter under four that don't.
    """
    if not dropped:
        return ""
    names = ", ".join(
        f'{s.get("ticker","?")} {(s.get("direction") or "").lower()} ({s.get("confidence","—")})'
        for s in dropped[:8])
    more = f" +{len(dropped) - 8} more" if len(dropped) > 8 else ""
    return f"""
  <tr><td style="background:#0b0b0b;border:1px solid #ffffff14;border-radius:14px;padding:14px 20px;">
    <p style="margin:0;font-size:12px;color:#6b7280;">
      {len(dropped)} idea{"" if len(dropped) == 1 else "s"} below the {rules.watch_confidence}
      confidence floor, not shown: <span style="color:#9ca3af;">{names}{more}</span></p>
  </td></tr>
  <tr><td style="height:16px;"></td></tr>"""


def build_email(analysis, portfolio_summary, mode, now_et, intel=None, applied=None, exits=None,
                exit_alerts=None):
    signals = analysis.get("signals", []) or []
    outlook = analysis.get("market_outlook", "")
    degraded = analysis.get("degraded")
    intel_tickers = (intel or {}).get("tickers") or {}

    scalps = [s for s in signals if (s.get("trade_type") or "").upper() == "SCALP"]
    shorts = [s for s in signals if (s.get("trade_type") or "").upper() == "SHORT_TERM"]
    longs = [s for s in signals if (s.get("trade_type") or "").upper() == "LONG_TERM"]
    ordered = scalps + shorts + longs + [s for s in signals if s not in scalps + shorts + longs]

    # ── the conviction split ────────────────────────────────────────────────────
    # `portfolio.validate_action` refuses every BUY under the execute bar, so an email that renders
    # all three bands identically describes trades the harness already declined. Watches are still
    # worth reading — they are the setups that need one more piece of evidence — but they must not
    # look like fills. Below the floor is noise; it gets a count, not a card, so nothing vanishes
    # silently while nothing dilutes what's above it either.
    rules = config.rules_for("crypto" if mode == "crypto" else "stock")
    # "unrated" — no number stated — rides with the trades. The gates let it through for the same
    # reason (an absent claim is not a weak one), and an email that hid a signal for a field it
    # never filled would be the harness quietly editing the analyst.
    banded = [(s, rules.confidence_band(s.get("confidence"))) for s in ordered]
    trades = [s for s, b in banded if b in ("execute", "unrated")]
    watches = [s for s, b in banded if b == "watch"]
    dropped = [s for s, b in banded if b == "below_bar"]
    shown = trades + watches

    # The crypto book gets its own subject prefix. Both books email the same inbox and the two
    # arrive interleaved at all hours; without it there is nothing in the subject that says which
    # account a BTC line belongs to.
    book = "Market Brain 🪙 Crypto" if mode == "crypto" else "Market Brain"
    # An exit warning outranks a new idea in the subject line. A cycle can easily produce both, and
    # the one that is time-sensitive is the position already on the book.
    alerts = list(exit_alerts or [])
    urgent = [a for a in alerts if a.get("verdict") in ("EXIT", "TRIM")]
    if shown:
        tags = ", ".join(f"{s.get('ticker')} {s.get('direction','')}" for s in shown[:5])
        prefix = "⚠️ " if degraded else ""
        if urgent:
            prefix = "🚨 " + prefix
        # A subject that reads like a trade list when nothing cleared the bar is the same lie the
        # cards used to tell, one line earlier.
        kind = "" if trades else "watch — "
        subject = f"{prefix}{book}: {kind}{tags} — {now_et.strftime('%I:%M %p ET')}"
    elif alerts:
        tags = ", ".join(f"{a['verdict']} {a['ticker']}" for a in alerts[:4])
        subject = (f"{'🚨 ' if urgent else ''}{book}: exit monitor — {tags} — "
                   f"{now_et.strftime('%I:%M %p ET')}")
    else:
        subject = f"{book}: {mode} recap — {now_et.strftime('%I:%M %p ET')}"

    header = f"""
  <tr><td style="padding:0 0 18px;">
    <p style="margin:0;font-size:11px;color:#6b7280;text-transform:uppercase;letter-spacing:0.1em;">Market Brain · {mode.upper()} · Paper trading only</p>
    <p style="margin:8px 0 0;font-size:14px;color:#d1d5db;line-height:1.5;">{outlook}</p>
  </td></tr>"""

    cards = "".join(
        build_card(s, now_et, intel_tickers.get(s.get("ticker")),
                   watch_bar=None if s in trades else rules.min_confidence)
        for s in shown)
    cards += _below_bar_card(dropped, rules)
    pcard = _portfolio_card(portfolio_summary)
    mcard = _macro_card(intel)
    acard = _applied_card(applied, exits, (portfolio_summary or {}).get("broker"))
    xcard = _exit_alerts_card(alerts)

    html = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#0a0a0a;font-family:-apple-system,'Segoe UI',Helvetica,Arial,sans-serif;color:#e5e7eb;">
<table width="100%" cellpadding="0" cellspacing="0"><tr><td align="center" style="padding:32px 16px;">
<table width="560" cellpadding="0" cellspacing="0" style="max-width:560px;width:100%;">
{header}{xcard}{mcard}{acard}{cards}{pcard}
<tr><td style="padding:8px 4px;font-size:11px;color:#4b5563;text-align:center;">{config.DISCLAIMER}</td></tr>
</table></td></tr></table>
</body></html>"""

    # plain-text fallback
    lines = [f"MARKET BRAIN — {mode.upper()} ({now_et.strftime('%a %b %d %I:%M %p ET')})", "", outlook, ""]
    lines += _exit_alerts_text(alerts)
    if intel and intel.get("macro_read"):
        lines += [f"NEWS READ ({intel.get('model')}): {intel['macro_read']}",
                  f"  macro news sentiment {intel.get('macro_sentiment', 0):+d}", ""]
    if exits or applied:
        lines.append("TRADES TAKEN THIS RUN (paper):")
        for e in exits or []:
            lines.append(f"  CLOSED {e.get('ticker')} {e.get('pnl_pct',0):+.1f}% "
                         f"(${e.get('pnl_usd',0):+,.0f}) — {e.get('reason','')}")
        for a in applied or []:
            lines.append(f"  {a}")
        lines.append("")
    for s in shown:
        tag = "" if s in trades else f"[WATCH — {rules.min_confidence} to execute] "
        lines.append(f"{tag}[{(s.get('trade_type') or '').upper()}] {s.get('ticker')} {s.get('direction')} "
                     f"(conf {s.get('confidence')}) — hold {s.get('holding_period')}")
        lines.append(f"  Why: {s.get('why','')}")
        lines.append(f"  Confidence: {s.get('confidence')} — {s.get('confidence_rationale','')}")
        # format_price rather than the raw value: a sub-cent token's entry repr's as "2.95e-06",
        # which is correct and unreadable. The HTML card already goes through the same formatter.
        _pl = lambda v: config.format_price(v) if isinstance(v, (int, float)) else (v or "—")
        lines.append(f"  Plan: entry {_pl(s.get('entry'))} stop {_pl(s.get('stop'))} "
                     f"T1 {_pl(s.get('target1'))} T2 {_pl(s.get('target2'))}")
        lines.append(f"  Indicators used: {', '.join(str(i) for i in (s.get('indicators_used') or []))}")
        lines.append(f"  Indicators: {s.get('indicators','')}")
        lines.append(f"  Charts: {s.get('chart_read','')}")
        if s.get("news_read"):
            lines.append(f"  News read: {s['news_read']}")
        for n in (s.get("news") or [])[:3]:
            lines.append(f"  News: {n.get('title','')} ({n.get('source','')}) {n.get('link','')}")
        if s.get("historical_analog"):
            lines.append(f"  Analog: {s.get('historical_analog')}")
        lines.append(f"  Sources: {', '.join(s.get('data_sources', []))}")
        lines.append("")
    if dropped:
        lines.append(f"{len(dropped)} below the {rules.watch_confidence} confidence floor, not shown: "
                     + ", ".join(f"{s.get('ticker')} ({s.get('confidence')})" for s in dropped))
        lines.append("")
    if portfolio_summary:
        lines.append(f"PAPER PORTFOLIO ({portfolio_summary.get('book','stock')}): "
                     f"${portfolio_summary.get('equity',0):,.0f} "
                     f"({portfolio_summary.get('total_return_pct',0):+.1f}%)")
        for p in portfolio_summary.get("open_positions", []):
            left = p.get("scalp_hours_left")
            clock = f" — scalp clock {left:.1f}h left" if left is not None else ""
            lines.append(f"  {p['ticker']} {p['pnl_pct']:+.1f}% ${p['value']:,.0f}{clock}")
    lines.append(config.DISCLAIMER)
    return subject, "\n".join(lines), html


def _pct(v):
    return "—" if v is None else f"{v}%"


def _mini_table(headers, rows, aligns=None):
    """A compact dark table for the recap's measured sections."""
    aligns = aligns or (["left"] + ["right"] * (len(headers) - 1))
    head = "".join(
        f'<td style="font-size:10px;color:#4b5563;text-transform:uppercase;'
        f'letter-spacing:0.06em;text-align:{a};padding-bottom:4px;">{h}</td>'
        for h, a in zip(headers, aligns))
    body = ""
    for r in rows:
        body += "<tr>" + "".join(
            f'<td style="padding:5px 6px 5px 0;font-size:12px;color:{c[1]};text-align:{a};">'
            f'{c[0]}</td>' for c, a in zip(r, aligns)) + "</tr>"
    return (f'<table width="100%" cellpadding="0" cellspacing="0"><tr>{head}</tr>{body}</table>')


def _signals_html(sig, narrative=None):
    """What was recommended, not only what filled."""
    if not sig.get("n_signals"):
        return '<p style="margin:0;font-size:13px;color:#6b7280;">No signals logged this week.</p>'
    head = (f'<p style="margin:0 0 10px;font-size:13px;color:#d1d5db;">'
            f'<b style="color:#fff;">{sig["n_signals"]}</b> signal(s) — '
            f'<span style="color:#22c55e;">{sig["n_executed"]} executed</span>, '
            f'{sig["n_rejected"]} rejected by the gates, {sig["n_advisory"]} advisory.</p>')
    if narrative:
        head += (f'<p style="margin:0 0 12px;font-size:13px;color:#d1d5db;line-height:1.6;">'
                 f'{narrative}</p>')
    colors = {"executed": "#22c55e", "rejected": "#f59e0b", "advisory": "#9ca3af"}
    rows = []
    for r in sig.get("recommended", [])[:20]:
        oc = r.get("outcome") or "advisory"
        f1 = r.get("forward_1d")
        rows.append([
            (r.get("ticker") or "—", "#e5e7eb"),
            (f'{r.get("direction") or "—"} {r.get("trade_type") or ""}'.strip(), "#9ca3af"),
            (r.get("confidence") if r.get("confidence") is not None else "—", "#9ca3af"),
            (oc, colors.get(oc, "#9ca3af")),
            (f"{f1:+.2f}%" if f1 is not None else "—",
             "#22c55e" if (f1 or 0) > 0 else "#ef4444" if f1 is not None else "#6b7280"),
        ])
    table = _mini_table(["Ticker", "Dir / type", "Conf", "Gate", "1d after"], rows,
                        ["left", "left", "right", "right", "right"])
    why = ""
    if sig.get("rejection_reasons"):
        why = ('<p style="margin:10px 0 0;font-size:11px;color:#6b7280;">Rejections: '
               + " · ".join(f'{r["reason"]} ×{r["n"]}' for r in sig["rejection_reasons"])
               + "</p>")
    verdict = (f'<p style="margin:10px 0 0;font-size:12px;color:#93c5fd;">'
               f'{sig.get("gate_verdict", "")}</p>' if sig.get("gate_verdict") else "")
    return head + table + why + verdict


def _measured_html(an):
    """The five cuts the ledger already paid for."""
    if not an:
        return '<p style="margin:0;font-size:13px;color:#6b7280;">Nothing measured this week.</p>'
    out = []
    if an.get("sample_note"):
        out.append(f'<p style="margin:0 0 12px;font-size:12px;color:#f59e0b;">'
                   f'{an["sample_note"]}</p>')
    conf = an.get("by_confidence") or []
    if conf:
        rows = [[(c["bucket"], "#e5e7eb"), (c["n"], "#9ca3af"), (_pct(c["win_rate"]), "#e5e7eb"),
                 (f'{c["implied_win_rate"]}%', "#6b7280"),
                 (f'{c["gap_pts"]:+.1f}',
                  "#22c55e" if c["gap_pts"] >= 0 else "#ef4444"),
                 (c["read"], "#9ca3af")] for c in conf]
        out.append('<p style="margin:0 0 4px;font-size:12px;color:#9ca3af;">'
                   'Confidence vs. what it implied</p>')
        out.append(_mini_table(["Bucket", "N", "Won", "Implied", "Gap", "Read"], rows,
                               ["left", "right", "right", "right", "right", "right"]))
    for title, key, name_key in (
            ("Indicators (win rate where you claimed to use it)", "by_indicator", "indicator"),
            ("Trade type", "by_trade_type", "name"),
            ("How trades ended", "by_exit_kind", "name"),
            ("Sector", "by_sector", "name")):
        rows = an.get(key) or []
        if not rows:
            continue
        body = [[(r[name_key], "#e5e7eb"), (r["n"], "#9ca3af"), (_pct(r["win_rate"]), "#e5e7eb"),
                 (_pct(r["avg_pnl_pct"]),
                  "#22c55e" if (r["avg_pnl_pct"] or 0) >= 0 else "#ef4444")]
                for r in rows[:10]]
        out.append(f'<p style="margin:16px 0 4px;font-size:12px;color:#9ca3af;">{title}</p>')
        out.append(_mini_table(["", "N", "Win rate", "Avg P&L"], body))
    return "".join(out)


def _pipeline_html(items):
    if not items:
        return ('<p style="margin:0;font-size:13px;color:#6b7280;">'
                'No pipeline changes proposed — the harness was adequate this week.</p>')
    li = ""
    for p in items:
        effort = (f' <span style="color:#6b7280;font-size:11px;">({p["effort"]})</span>'
                  if p.get("effort") else "")
        why = (f'<br><span style="color:#6b7280;font-size:11px;">{p["why"]}</span>'
               if p.get("why") else "")
        li += (f'<li style="margin:8px 0;font-size:13px;color:#d1d5db;">'
               f'<b style="color:#fff;">{p.get("change")}</b>{effort}{why}</li>')
    return f'<ul style="margin:0;padding-left:18px;">{li}</ul>'


def _week_ahead_html(ahead):
    events, speeches = (ahead or {}).get("events") or [], (ahead or {}).get("speeches") or []
    if not (events or speeches):
        return ('<p style="margin:0;font-size:13px;color:#6b7280;">'
                'No high-impact events in the feed for next week.</p>')
    li = "".join(
        f'<li style="margin:5px 0;font-size:13px;color:#d1d5db;">'
        f'<span style="color:#6b7280;">{e.get("when")}</span> — {e.get("title")} '
        f'<span style="color:#6b7280;">({e.get("country")})</span></li>' for e in events)
    li += "".join(
        f'<li style="margin:5px 0;font-size:13px;color:#d1d5db;">'
        f'<span style="color:#6b7280;">{s.get("when")}</span> — 🎙 {s.get("title")}</li>'
        for s in speeches)
    return f'<ul style="margin:0;padding-left:18px;">{li}</ul>'


_MIX_LABEL = {"SCALP": "scalp", "SHORT_TERM": "swing", "LONG_TERM": "long"}


def _mix_parts(mix):
    """"2 scalp · 1 swing · 0 long" — the week's shape spelled out, zeros included.

    The zeros are the whole point. A week that opened five trades reads as on-pace from the count
    alone and can be five scalps, which is what "you havent given me long term trades" was
    describing. An untagged position is appended rather than folded into a horizon it never claimed.
    """
    mix = mix or {}
    parts = [f"{int(mix.get(h, 0) or 0)} {_MIX_LABEL[h]}" for h in config.TRADE_HORIZONS]
    if mix.get("UNSPECIFIED"):
        parts.append(f"{int(mix['UNSPECIFIED'])} untagged")
    return " · ".join(parts)


def _mix_html(st):
    """The shape against the target, ambered while the long slot is still open."""
    mix = st.get("mix_opened") or {}
    target = st.get("mix_target") or config.WEEKLY_MIX
    short = int(target.get("LONG_TERM", 0) or 0) - int(mix.get("LONG_TERM", 0) or 0)
    color = "#f59e0b" if short > 0 else "#22c55e"
    return (f'<span style="color:{color};font-weight:700;">{_mix_parts(mix)}</span> '
            f'<span style="color:#6b7280;">/ wants {_mix_parts(target)}</span>')


def _crypto_recap_html(crypto):
    """The crypto book's week, as a card inside the equity recap.

    Deliberately the short form — the numbers, the closed trades, the pace. The narrative, the
    gradings and the proposed changes are written across both books at once, so repeating those
    per book would say the same thing twice.
    """
    if not crypto:
        return ('<p style="margin:0;font-size:13px;color:#6b7280;">'
                'No crypto book figures this week.</p>')
    st = crypto.get("stats", {})
    ret = st.get("week_return_pct", 0) or 0
    rc = "#22c55e" if ret >= 0 else "#ef4444"
    wr = st.get("win_rate")
    target = st.get("weekly_trade_target") or 0
    opened_n = st.get("n_opened", 0)
    pace_c = "#22c55e" if opened_n >= target else ("#f59e0b" if opened_n >= target - 2 else "#ef4444")
    pace = (f'<span style="color:{pace_c};font-weight:700;">{opened_n}</span> '
            f'<span style="color:#6b7280;">/ {target} target</span>')
    rows = "".join(
        f'<li style="margin:5px 0;font-size:13px;color:#d1d5db;">'
        f'<b>{c.get("ticker")}</b> <span style="color:#6b7280;">{c.get("trade_type") or "—"}</span> '
        f'{c.get("entry")} → {c.get("exit")} '
        f'<span style="color:{"#22c55e" if (c.get("pnl_pct") or 0) >= 0 else "#ef4444"};">'
        f'{c.get("pnl_pct",0):+.1f}%</span> '
        f'<span style="color:#6b7280;">{(c.get("reason") or "")[:60]}</span></li>'
        for c in crypto.get("closed") or [])
    trades = (f'<ul style="margin:0;padding-left:18px;">{rows}</ul>' if rows else
              '<p style="margin:0;font-size:13px;color:#6b7280;">No crypto trades closed this week.</p>')
    return (
        f'<p style="margin:0;font-size:26px;font-weight:800;color:{rc};">{ret:+.2f}%</p>'
        f'<p style="margin:2px 0 12px;font-size:13px;color:#9ca3af;">'
        f'${st.get("equity_start",0):,.0f} → <b style="color:#fff;">${st.get("equity_end",0):,.0f}</b>'
        f' · realised ${st.get("realized_usd",0):+,.0f}</p>'
        f'<table width="100%" cellpadding="0" cellspacing="0">'
        f'{_row("Alpha vs benchmark", _bench_html(st))}'
        f'{_row("Closed trades", st.get("n_closed", 0))}'
        f'{_row("Opened this week", pace)}'
        f'{_row("Trade mix", _mix_html(st))}'
        f'{_row("Still open", st.get("n_open", 0))}'
        f'{_row("Win rate", f"{wr}%" if wr is not None else "—")}'
        f'</table>'
        f'<div style="height:12px;"></div>{trades}')


def _bench_html(stats):
    """The alpha cell: what the book did against the index it is trying to beat.

    This is the one number in the recap that answers the question the whole system was built to
    answer, so it renders green/red on *alpha* rather than on the book's own return — a +3% week
    against a +5% SPY is red here and should be. Amber when the comparison could not be made,
    because "unknown" must not be mistaken for "flat".
    """
    b = (stats or {}).get("benchmark") or {}
    if not b.get("available"):
        return (f'<span style="color:#f59e0b;">not measurable</span> '
                f'<span style="color:#6b7280;">— {b.get("reason", "no benchmark data")}</span>')
    a = b.get("alpha_pct", 0)
    c = "#22c55e" if b.get("beat") else "#ef4444"
    return (f'<span style="color:{c};font-weight:700;">{a:+.2f} pts</span> '
            f'<span style="color:#6b7280;">(book {b["book_return_pct"]:+.2f}% vs '
            f'{b["ticker"]} {b["benchmark_return_pct"]:+.2f}%)</span>')


def build_weekly_email(recap, now_et):
    """The Friday-close recap: how the week went, every trade and signal, and what to change."""
    st = recap.get("stats", {})
    ret = st.get("week_return_pct", 0) or 0
    rc = "#22c55e" if ret >= 0 else "#ef4444"
    wr = st.get("win_rate")
    label = recap.get("week_label", now_et.strftime("%Y-%m-%d"))

    trade_rows = []
    for c in recap.get("closed", []) or []:
        pc = "#22c55e" if (c.get("pnl_pct") or 0) >= 0 else "#ef4444"
        trade_rows.append(
            f'<tr>'
            f'<td style="padding:7px 0;font-size:13px;color:#e5e7eb;font-weight:700;">{c.get("ticker")}</td>'
            f'<td style="padding:7px 6px;font-size:11px;color:#9ca3af;">{(c.get("trade_type") or "—")}</td>'
            f'<td style="padding:7px 6px;font-size:12px;color:#9ca3af;text-align:right;">'
            f'{c.get("confidence") if c.get("confidence") is not None else "—"}</td>'
            f'<td style="padding:7px 6px;font-size:12px;color:#9ca3af;text-align:right;">'
            f'{c.get("entry")} → {c.get("exit")}</td>'
            f'<td style="padding:7px 0;font-size:13px;color:{pc};text-align:right;font-weight:700;">'
            f'{c.get("pnl_pct",0):+.1f}%</td></tr>'
            f'<tr><td colspan="5" style="padding:0 0 8px;font-size:11px;color:#6b7280;">'
            f'{c.get("reason","")}</td></tr>')
    trades_html = (
        '<table width="100%" cellpadding="0" cellspacing="0">'
        '<tr><td style="font-size:10px;color:#4b5563;text-transform:uppercase;letter-spacing:0.06em;">Ticker</td>'
        '<td style="font-size:10px;color:#4b5563;">Type</td>'
        '<td style="font-size:10px;color:#4b5563;text-align:right;">Conf</td>'
        '<td style="font-size:10px;color:#4b5563;text-align:right;">Entry → Exit</td>'
        '<td style="font-size:10px;color:#4b5563;text-align:right;">P&amp;L</td></tr>'
        + "".join(trade_rows) + '</table>'
        if trade_rows else
        '<p style="margin:0;font-size:13px;color:#6b7280;">No trades closed this week.</p>')

    def _list(items, empty):
        if not items:
            return f'<p style="margin:0;font-size:13px;color:#6b7280;">{empty}</p>'
        return ('<ol style="margin:0;padding-left:20px;">' + "".join(
            f'<li style="margin:7px 0;font-size:13px;color:#d1d5db;">{i}</li>' for i in items)
            + '</ol>')

    open_html = "".join(
        f'<li style="margin:5px 0;font-size:13px;color:#d1d5db;">'
        f'<b>{o.get("ticker")}</b> entry {o.get("entry")} · now {o.get("last")} '
        f'<span style="color:{"#22c55e" if (o.get("pnl_pct") or 0) >= 0 else "#ef4444"};">'
        f'{o.get("pnl_pct",0):+.1f}%</span></li>'
        for o in recap.get("open", []) or [])
    grade_html = "".join(
        f'<li style="margin:6px 0;font-size:13px;color:#d1d5db;"><b>{g.get("ticker")}</b> '
        f'{g.get("verdict")} — {"; ".join(g.get("notes") or []) or "nothing anomalous"}</li>'
        for g in recap.get("gradings", []) or [])

    # Activity against the pace target, next to the P&L. A green week on one trade and a green
    # week on five are different weeks, and only this line says which one it was.
    target = st.get("weekly_trade_target") or config.WEEKLY_TRADE_TARGET
    opened_n = st.get("n_opened", 0)
    pace_c = "#22c55e" if opened_n >= target else ("#f59e0b" if opened_n >= target - 2 else "#ef4444")
    pace_html = (f'<span style="color:{pace_c};font-weight:700;">{opened_n}</span> '
                 f'<span style="color:#6b7280;">/ {target} target</span>')

    an = recap.get("analytics") or {}
    sig_html = _signals_html(an.get("signals") or {}, recap.get("signal_review"))
    meas_html = _measured_html(an)
    pipe_html = _pipeline_html(recap.get("pipeline_changes"))
    ahead_html = _week_ahead_html(recap.get("week_ahead"))
    # An unreadable crypto ledger drops the card rather than printing an empty one — the equity
    # recap is the email, and a section with nothing in it reads like a rendering fault.
    crypto_card = (
        '<tr><td style="background:#0c1512;border:1px solid #34d39944;border-radius:14px;'
        f'padding:22px 26px;">{_section("Crypto book — the same week, the other ledger", _crypto_recap_html(recap["crypto"]), "#34d399")}'
        '</td></tr><tr><td style="height:16px;"></td></tr>'
    ) if recap.get("crypto") else ""

    html = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#0a0a0a;font-family:-apple-system,'Segoe UI',Helvetica,Arial,sans-serif;color:#e5e7eb;">
<table width="100%" cellpadding="0" cellspacing="0"><tr><td align="center" style="padding:32px 16px;">
<table width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;">
  <tr><td style="padding:0 0 18px;">
    <p style="margin:0;font-size:11px;color:#6b7280;text-transform:uppercase;letter-spacing:0.1em;">Market Brain · Weekly recap · Paper trading only</p>
    <h1 style="margin:8px 0 0;font-size:26px;font-weight:800;color:#fff;">Trading week — {label}</h1>
  </td></tr>

  <tr><td style="background:#0f172a;border:1px solid #1e293b;border-radius:14px;padding:22px 26px;">
    <p style="margin:0;font-size:34px;font-weight:800;color:{rc};">{ret:+.2f}%</p>
    <p style="margin:2px 0 14px;font-size:13px;color:#9ca3af;">
      ${st.get("equity_start",0):,.0f} → <b style="color:#fff;">${st.get("equity_end",0):,.0f}</b>
      · realised ${st.get("realized_usd",0):+,.0f}</p>
    <table width="100%" cellpadding="0" cellspacing="0">
      {_row("Alpha vs benchmark", _bench_html(st))}
      {_row("Closed trades", st.get("n_closed", 0))}
      {_row("Opened this week", pace_html)}
      {_row("Trade mix", _mix_html(st))}
      {_row("Still open", st.get("n_open", 0))}
      {_row("Win rate", f"{wr}%" if wr is not None else "—")}
      {_row("Best / worst", f'{st.get("best","—")} / {st.get("worst","—")}')}
      {_row("All-time return", f'{st.get("total_return_pct",0):+.2f}%')}
    </table>
  </td></tr>
  <tr><td style="height:16px;"></td></tr>

  <tr><td style="background:#111;border:1px solid #ffffff14;border-radius:14px;padding:22px 26px;">
    {_section("How the week went", f'<p style="margin:0;font-size:13px;color:#d1d5db;line-height:1.6;">{recap.get("narrative") or "No narrative generated this week."}</p>')}
    {_section("Every trade this week", trades_html)}
    {_section("Still open", f'<ul style="margin:0;padding-left:18px;">{open_html}</ul>' if open_html else '<p style="margin:0;font-size:13px;color:#6b7280;">Nothing open.</p>')}
    {_section("Every signal — recommended vs. executed", sig_html, "#f59e0b")}
  </td></tr>
  <tr><td style="height:16px;"></td></tr>

  {crypto_card}

  <tr><td style="background:#111;border:1px solid #ffffff14;border-radius:14px;padding:22px 26px;">
    {_section("Measured performance", meas_html, "#a78bfa")}
  </td></tr>
  <tr><td style="height:16px;"></td></tr>

  <tr><td style="background:#0b1220;border:1px solid #60a5fa44;border-radius:14px;padding:22px 26px;">
    {_section("What to change to be more accurate", _list(recap.get("changes"), "No rule changes proposed."), "#60a5fa")}
    {_section("Mistakes", _list(recap.get("mistakes"), "None identified."), "#ef4444")}
    {_section("What worked", _list(recap.get("successes"), "Nothing conclusive yet."), "#22c55e")}
    {_section("Per-trade grading (rules-based)", f'<ul style="margin:0;padding-left:18px;">{grade_html}</ul>' if grade_html else '<p style="margin:0;font-size:13px;color:#6b7280;">No closed trades to grade.</p>')}
  </td></tr>
  <tr><td style="height:16px;"></td></tr>

  <tr><td style="background:#12081c;border:1px solid #a855f744;border-radius:14px;padding:22px 26px;">
    {_section("Long-term thesis board — re-scored today", _thesis_html(recap.get("theses"), recap.get("thesis_regime")), "#c084fc")}
  </td></tr>
  <tr><td style="height:16px;"></td></tr>

  <tr><td style="background:#111;border:1px solid #ffffff14;border-radius:14px;padding:22px 26px;">
    {_section("Pipeline changes to implement", pipe_html, "#a78bfa")}
    {_section("Week ahead — high-impact events", ahead_html, "#f59e0b")}
  </td></tr>
  <tr><td style="padding:14px 4px;font-size:11px;color:#4b5563;text-align:center;">{config.DISCLAIMER}</td></tr>
</table></td></tr></table>
</body></html>"""

    lines = [f"MARKET BRAIN — WEEKLY RECAP ({label})", "",
             f"Week: {ret:+.2f}%  (${st.get('equity_start',0):,.0f} -> ${st.get('equity_end',0):,.0f})",
             f"Realised ${st.get('realized_usd',0):+,.0f} across {st.get('n_closed',0)} closed trade(s)",
             f"Win rate: {wr if wr is not None else 'n/a'}  ·  best {st.get('best','—')} / worst {st.get('worst','—')}",
             f"Opened this week: {opened_n} / {target} target",
             f"Trade mix: {_mix_parts(st.get('mix_opened'))}  "
             f"(wants {_mix_parts(st.get('mix_target') or config.WEEKLY_MIX)})",
             "", "HOW THE WEEK WENT", recap.get("narrative") or "—", "", "TRADES CLOSED"]
    for c in recap.get("closed", []) or []:
        lines.append(f"  {c.get('ticker')} [{c.get('trade_type') or '—'}] conf {c.get('confidence')} "
                     f"{c.get('entry')} -> {c.get('exit')} {c.get('pnl_pct',0):+.1f}% "
                     f"(${c.get('pnl_usd',0):+,.0f}) — {c.get('reason','')}")
    if not recap.get("closed"):
        lines.append("  none")

    cr = recap.get("crypto")
    if cr:
        cst = cr.get("stats", {})
        lines += ["", "CRYPTO BOOK — SAME WEEK, OTHER LEDGER",
                  f"  Week: {cst.get('week_return_pct',0):+.2f}%  "
                  f"(${cst.get('equity_start',0):,.0f} -> ${cst.get('equity_end',0):,.0f})",
                  f"  Realised ${cst.get('realized_usd',0):+,.0f} across "
                  f"{cst.get('n_closed',0)} closed trade(s)",
                  f"  Opened this week: {cst.get('n_opened',0)} / "
                  f"{cst.get('weekly_trade_target',0)} target  ·  still open "
                  f"{cst.get('n_open',0)}",
                  f"  Trade mix: {_mix_parts(cst.get('mix_opened'))}  "
                  f"(wants {_mix_parts(cst.get('mix_target') or config.CRYPTO_WEEKLY_MIX)})"]
        for c in cr.get("closed") or []:
            lines.append(f"    {c.get('ticker')} [{c.get('trade_type') or '—'}] "
                         f"{c.get('entry')} -> {c.get('exit')} {c.get('pnl_pct',0):+.1f}% "
                         f"(${c.get('pnl_usd',0):+,.0f}) — {c.get('reason','')}")
        if not cr.get("closed"):
            lines.append("    none")

    sig = an.get("signals") or {}
    if sig.get("n_signals"):
        lines += ["", "SIGNALS — RECOMMENDED VS EXECUTED",
                  f"  {sig['n_signals']} signal(s): {sig['n_executed']} executed, "
                  f"{sig['n_rejected']} rejected, {sig['n_advisory']} advisory"]
        if recap.get("signal_review"):
            lines.append(f"  {recap['signal_review']}")
        for r in sig.get("recommended", [])[:20]:
            f1 = r.get("forward_1d")
            lines.append(f"  {r.get('ticker')} {r.get('direction') or ''} "
                         f"[{r.get('trade_type') or '—'}] conf {r.get('confidence')} "
                         f"-> {r.get('outcome') or 'advisory'}"
                         + (f", 1d after {f1:+.2f}%" if f1 is not None else ""))
        if sig.get("rejection_reasons"):
            lines.append("  rejections: " + ", ".join(
                f"{x['reason']} x{x['n']}" for x in sig["rejection_reasons"]))
        if sig.get("gate_verdict"):
            lines.append(f"  {sig['gate_verdict']}")

    if an:
        lines += ["", "MEASURED PERFORMANCE"]
        if an.get("sample_note"):
            lines.append(f"  ({an['sample_note']})")
        for c in an.get("by_confidence") or []:
            lines.append(f"  conf {c['bucket']}: n={c['n']} won {c['win_rate']}% "
                         f"vs {c['implied_win_rate']}% implied ({c['gap_pts']:+.1f} pts, {c['read']})")
        for title, key, nk in (("indicators", "by_indicator", "indicator"),
                               ("trade type", "by_trade_type", "name"),
                               ("exit kind", "by_exit_kind", "name"),
                               ("sector", "by_sector", "name")):
            rows = an.get(key) or []
            if rows:
                lines.append(f"  {title}: " + "; ".join(
                    f"{r[nk]} n={r['n']} win {r['win_rate']}% avg {r['avg_pnl_pct']}%"
                    for r in rows[:10]))

    lines += ["", "WHAT TO CHANGE"]
    lines += [f"  {i}. {c}" for i, c in enumerate(recap.get("changes") or [], 1)] or ["  none"]
    if recap.get("mistakes"):
        lines += ["", "MISTAKES"] + [f"  - {m}" for m in recap["mistakes"]]
    if recap.get("successes"):
        lines += ["", "WHAT WORKED"] + [f"  - {s}" for s in recap["successes"]]
    if recap.get("pipeline_changes"):
        lines += ["", "PIPELINE CHANGES TO IMPLEMENT"]
        for p in recap["pipeline_changes"]:
            lines.append(f"  - {p.get('change')}"
                         + (f" ({p['effort']})" if p.get("effort") else ""))
            if p.get("why"):
                lines.append(f"      why: {p['why']}")
    if recap.get("theses"):
        lines += ["", "LONG-TERM THESIS BOARD (1-3 YEARS)"]
        for t in recap["theses"]:
            lines.append(f"  [{t.get('conviction')}/100] {t.get('theme')} "
                         f"({', '.join(t.get('tickers') or []) or 'no ticker'})")
            if t.get("thesis"):
                lines.append(f"      {t['thesis']}")
    ahead = recap.get("week_ahead") or {}
    if ahead.get("events") or ahead.get("speeches"):
        lines += ["", "WEEK AHEAD"]
        lines += [f"  {e.get('when')} — {e.get('title')} ({e.get('country')})"
                  for e in ahead.get("events") or []]
        lines += [f"  {s.get('when')} — speech: {s.get('title')}"
                  for s in ahead.get("speeches") or []]
    lines += ["", config.DISCLAIMER]

    subject = (f"Market Brain: trading week {ret:+.2f}% — "
               f"{st.get('n_closed',0)} closed, {label}")
    return subject, "\n".join(lines), html


def _thesis_html(theses, regime=None):
    """The long-horizon board: what the brain thinks is structurally true over 1-3 years.

    Rendered with the invalidation conditions visible, because a thesis you cannot see the
    falsifier for is one you cannot argue with.
    """
    head = ""
    if regime:
        head = (f'<p style="margin:0 0 12px;font-size:13px;color:#d1d5db;line-height:1.6;">'
                f'{regime}</p>')
    if not theses:
        return head + ('<p style="margin:0;font-size:13px;color:#6b7280;">'
                       'No structural theses on the board yet. The brain would rather hold none '
                       'than invent one.</p>')
    cards = []
    for t in theses:
        conv = t.get("conviction") or 0
        ccolor = "#22c55e" if conv >= 70 else ("#f59e0b" if conv >= 50 else "#6b7280")
        tickers = _chips(t.get("tickers") or [], "#a855f7")
        age = t.get("age_days")
        age_s = f"{age}d on the board" if isinstance(age, int) else "new this week"
        stale = ('<span style="color:#f59e0b;"> · evidence has gone quiet</span>'
                 if t.get("needs_review") else "")
        inval = "".join(
            f'<li style="margin:3px 0;font-size:12px;color:#9ca3af;">{i}</li>'
            for i in (t.get("invalidation") or [])[:3])
        inval_html = (f'<p style="margin:10px 0 3px;font-size:10px;color:#4b5563;'
                      f'text-transform:uppercase;letter-spacing:0.06em;">Wrong if</p>'
                      f'<ul style="margin:0;padding-left:16px;">{inval}</ul>' if inval else "")
        miles = "".join(
            f'<li style="margin:3px 0;font-size:12px;color:#9ca3af;">{i}</li>'
            for i in (t.get("milestones") or [])[:3])
        miles_html = (f'<p style="margin:10px 0 3px;font-size:10px;color:#4b5563;'
                      f'text-transform:uppercase;letter-spacing:0.06em;">Watch for</p>'
                      f'<ul style="margin:0;padding-left:16px;">{miles}</ul>' if miles else "")
        cards.append(
            f'<div style="background:#12081c;border:1px solid #a855f733;border-radius:11px;'
            f'padding:15px 17px;margin:0 0 11px;">'
            f'<p style="margin:0 0 3px;font-size:15px;font-weight:700;color:#e9d5ff;">'
            f'{t.get("theme")}</p>'
            f'<p style="margin:0 0 9px;font-size:11px;color:#6b7280;">'
            f'{t.get("horizon","1-3 years")} · conviction '
            f'<span style="color:{ccolor};font-weight:700;">{conv}/100</span> · {age_s}{stale}</p>'
            f'{tickers}'
            f'<p style="margin:9px 0 0;font-size:13px;color:#d1d5db;line-height:1.55;">'
            f'{t.get("thesis") or t.get("driver") or ""}</p>'
            f'{inval_html}{miles_html}</div>')
    return head + "".join(cards)


def _digest_stat(label, value, sub=None, color="#e5e7eb"):
    """One cell of the digest's top-line strip."""
    subline = (f'<p style="margin:2px 0 0;font-size:11px;color:#6b7280;">{sub}</p>'
               if sub else "")
    return (f'<td width="33%" style="padding:0 6px;vertical-align:top;">'
            f'<p style="margin:0;font-size:10px;color:#4b5563;text-transform:uppercase;'
            f'letter-spacing:0.07em;">{label}</p>'
            f'<p style="margin:4px 0 0;font-size:19px;font-weight:800;color:{color};">{value}</p>'
            f'{subline}</td>')


def when_parts(when):
    """('Mon Jul 27', '23:05') from the calendar's ISO timestamp.

    The feed hands back a full ISO string with an offset. Printed raw it reads as machine output,
    which is most of what made the old digest unreadable — a wall of `2026-07-29T14:00:00-04:00`.
    """
    s = str(when or "").strip()
    try:
        dt = datetime.fromisoformat(s)
    except (TypeError, ValueError):
        head, _, tail = s.partition(" ")
        return (head or "Scheduled"), tail
    return dt.strftime("%a %b %d"), dt.strftime("%H:%M")


def _calendar_items(events, speeches):
    """Events and speeches as one chronological list, de-duplicated.

    ForexFactory lists an FOMC press conference in both feeds; showing it twice under the same
    minute reads as a bug even though both sources are right.
    """
    items, seen = [], set()
    for kind, item in ([("event", e) for e in events or []]
                       + [("speech", s) for s in speeches or []]):
        key = (str(item.get("when")), (item.get("title") or "").strip().lower())
        if key in seen:
            continue
        seen.add(key)
        items.append((kind, item))
    return sorted(items, key=lambda kv: str(kv[1].get("when") or ""))


def _calendar_html(events, speeches):
    """The week's catalysts, grouped by day rather than dumped as one flat list."""
    items = _calendar_items(events, speeches)
    if not items:
        return ('<p style="margin:0;font-size:13px;color:#6b7280;">'
                'No high-impact events scheduled — the tape sets its own agenda this week.</p>')
    by_day, order = {}, []
    for kind, item in items:
        day, when = when_parts(item.get("when"))
        if day not in by_day:
            by_day[day] = []
            order.append(day)
        by_day[day].append((kind, item, when))
    out = []
    for day in order:
        rows = []
        for kind, item, when in by_day[day]:
            title = (f'🎙 {item.get("title")}' if kind == "speech"
                     else f'<b>{item.get("title")}</b> '
                          f'<span style="color:#6b7280;">{item.get("country") or ""}</span>')
            rows.append(
                f'<tr><td style="padding:4px 10px 4px 0;font-size:12px;color:#6b7280;'
                f'white-space:nowrap;vertical-align:top;">{when or "—"}</td>'
                f'<td style="padding:4px 0;font-size:13px;'
                f'color:{"#d1d5db" if kind == "speech" else "#e5e7eb"};">{title}</td></tr>')
        out.append(
            f'<p style="margin:14px 0 5px;font-size:11px;font-weight:700;color:#f59e0b;">{day}</p>'
            f'<table width="100%" cellpadding="0" cellspacing="0">{"".join(rows)}</table>')
    return "".join(out)


def _crypto_standing_html(cs):
    """Where the other ledger stands going into the week.

    Read off the crypto summary's own caps, never the equity constants — the two books have
    never had the same limits and rendering "3/8" against the wrong one is worse than silence.
    """
    if not cs:
        return ('<p style="margin:0;font-size:13px;color:#6b7280;">'
                'Crypto ledger unavailable this run.</p>')
    ret = cs.get("total_return_pct", 0) or 0
    rc = "#22c55e" if ret >= 0 else "#ef4444"
    rows = (_row("Paper equity",
                 f'${cs.get("equity", 0):,.0f} '
                 f'<span style="color:{rc};">({ret:+.1f}%)</span>')
            + _row("Open positions",
                   f'{cs.get("n_open", 0)}/{cs.get("max_positions", "—")}')
            + _row("New trades used this week",
                   f'{cs.get("new_trades_this_week", 0)}/'
                   f'{cs.get("max_new_trades_per_week", "—")}'))
    pos = "".join(
        f'<li style="margin:4px 0;font-size:13px;color:#d1d5db;">'
        f'<b style="color:#e5e7eb;font-family:monospace;">{p["ticker"]}</b> '
        f'{p.get("trade_type") or "—"} · '
        f'<span style="color:{"#22c55e" if p["pnl_pct"] >= 0 else "#ef4444"};">'
        f'{p["pnl_pct"]:+.1f}%</span></li>'
        for p in cs.get("open_positions", []) or [])
    carried = (f'<ul style="margin:10px 0 0;padding-left:18px;">{pos}</ul>' if pos else
               '<p style="margin:10px 0 0;font-size:13px;color:#6b7280;">'
               'Flat into the week — nothing to defend overnight.</p>')
    return (f'<table width="100%" cellpadding="0" cellspacing="0">{rows}</table>{carried}'
            '<p style="margin:12px 0 0;font-size:12px;color:#6b7280;line-height:1.6;">'
            'New crypto entries follow the US session too. Stops, trailing stops and the '
            '24-hour scalp clock keep running hourly through the night and the weekend.</p>')


def build_digest_email(digest, now_et):
    """The week-ahead digest — the one email that arrives before the week starts.

    It used to be a `<pre>` block of monospace text, which is why Lind asked for something
    "more organized and more readable with a better design not just text all over the place".
    Same information, laid out as: where the book stands, what the brain is structurally betting
    on over years, what the calendar will do to the tape, and what it is watching this week.
    """
    psum = digest.get("portfolio") or {}
    label = digest.get("week_label") or now_et.strftime("%b %d, %Y")
    ret = psum.get("total_return_pct", 0) or 0
    rc = "#22c55e" if ret >= 0 else "#ef4444"
    last = digest.get("last_week") or {}
    theses = digest.get("theses") or []

    strip = (
        '<table width="100%" cellpadding="0" cellspacing="0"><tr>'
        + _digest_stat("Paper equity", f'${psum.get("equity", 0):,.0f}',
                       f"{ret:+.1f}% all time", "#fff")
        + _digest_stat("Open positions",
                       f'{psum.get("n_open", 0)}/{config.MAX_POSITIONS}',
                       f'{psum.get("new_trades_this_week", 0)}/'
                       f'{config.MAX_NEW_TRADES_PER_WEEK} new trades used')
        + _digest_stat("Long-term theses", f"{len(theses)}",
                       digest.get("thesis_delta_line") or "board carried forward", "#e9d5ff")
        + '</tr></table>')

    pos_rows = "".join(
        f'<tr><td style="padding:5px 0;font-size:13px;color:#e5e7eb;font-weight:600;">'
        f'{p["ticker"]}</td>'
        f'<td style="padding:5px 6px;font-size:12px;color:#6b7280;">{p.get("trade_type") or "—"}</td>'
        f'<td style="padding:5px 0;font-size:13px;text-align:right;font-weight:700;'
        f'color:{"#22c55e" if p["pnl_pct"] >= 0 else "#ef4444"};">{p["pnl_pct"]:+.1f}%</td>'
        f'<td style="padding:5px 0 5px 12px;font-size:12px;color:#9ca3af;text-align:right;">'
        f'${p["value"]:,.0f}</td></tr>'
        for p in psum.get("open_positions", []) or [])
    pos_html = (f'<table width="100%" cellpadding="0" cellspacing="0">{pos_rows}</table>'
                if pos_rows else
                '<p style="margin:0;font-size:13px;color:#6b7280;">'
                'Flat into the week — no open positions to defend.</p>')

    lw_html = ""
    if last:
        lw_html = _section(
            "Last week, in one line",
            f'<p style="margin:0;font-size:13px;color:#d1d5db;line-height:1.6;">'
            f'{last.get("n_signals", 0)} signal(s) published — '
            f'<span style="color:#22c55e;">{last.get("n_executed", 0)} executed</span>, '
            f'{last.get("n_rejected", 0)} blocked by the gates, '
            f'{last.get("n_advisory", 0)} advisory'
            + (f', {last["n_duplicate"]} held back as repeats'
               if last.get("n_duplicate") else "")
            + '.</p>', "#60a5fa")

    watch_html = ""
    if digest.get("watchlist"):
        watch_html = _section("On watch this week", _chips(digest["watchlist"], "#60a5fa"))

    plan = digest.get("session_plan") or []
    plan_html = "".join(
        f'<li style="margin:5px 0;font-size:13px;color:#d1d5db;">{p}</li>' for p in plan)

    html = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#0a0a0a;font-family:-apple-system,'Segoe UI',Helvetica,Arial,sans-serif;color:#e5e7eb;">
<table width="100%" cellpadding="0" cellspacing="0"><tr><td align="center" style="padding:32px 16px;">
<table width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;">
  <tr><td style="padding:0 0 18px;">
    <p style="margin:0;font-size:11px;color:#6b7280;text-transform:uppercase;letter-spacing:0.1em;">Market Brain · Week ahead · Paper trading only</p>
    <h1 style="margin:8px 0 4px;font-size:26px;font-weight:800;color:#fff;">Week of {label}</h1>
    <p style="margin:0;font-size:12px;color:#9ca3af;">{digest.get("session_line", "")}</p>
  </td></tr>

  <tr><td style="background:#0f172a;border:1px solid #1e293b;border-radius:14px;padding:22px 20px;">
    {strip}
  </td></tr>
  <tr><td style="height:16px;"></td></tr>

  <tr><td style="background:#12081c;border:1px solid #a855f744;border-radius:14px;padding:22px 26px;">
    {_section("Long-term positioning — 1-3 year theses", _thesis_html(theses, digest.get("regime")), "#c084fc")}
  </td></tr>
  <tr><td style="height:16px;"></td></tr>

  <tr><td style="background:#141005;border:1px solid #f59e0b44;border-radius:14px;padding:22px 26px;">
    {_section("What moves the tape this week", _calendar_html(digest.get("events"), digest.get("speeches")), "#f59e0b")}
  </td></tr>
  <tr><td style="height:16px;"></td></tr>

  <tr><td style="background:#111;border:1px solid #ffffff14;border-radius:14px;padding:22px 26px;">
    {_section("The book going in", pos_html)}
    {_section("Crypto book going in", _crypto_standing_html(digest.get("crypto_portfolio")), "#34d399")}
    {lw_html}
    {watch_html}
    {_section("How this week runs", f'<ul style="margin:0;padding-left:18px;">{plan_html}</ul>' if plan_html else '')}
  </td></tr>
  <tr><td style="padding:14px 4px;font-size:11px;color:#4b5563;text-align:center;">{config.DISCLAIMER}</td></tr>
</table></td></tr></table>
</body></html>"""

    lines = [f"MARKET BRAIN — WEEK AHEAD ({label})", "",
             digest.get("session_line", ""), "",
             f"Paper equity ${psum.get('equity', 0):,.0f} ({ret:+.1f}%), "
             f"{psum.get('n_open', 0)}/{config.MAX_POSITIONS} open, "
             f"{len(theses)} long-term thesis/theses on the board.", ""]
    cs = digest.get("crypto_portfolio")
    if cs:
        cret = cs.get("total_return_pct", 0) or 0
        lines += ["CRYPTO BOOK GOING IN",
                  f"  Paper equity ${cs.get('equity', 0):,.0f} ({cret:+.1f}%), "
                  f"{cs.get('n_open', 0)}/{cs.get('max_positions', '-')} open, "
                  f"{cs.get('new_trades_this_week', 0)}/"
                  f"{cs.get('max_new_trades_per_week', '-')} new trades used."]
        for p in cs.get("open_positions", []) or []:
            lines.append(f"    {p['ticker']} {p.get('trade_type') or '-'} {p['pnl_pct']:+.1f}%")
        lines.append("")
    if digest.get("regime"):
        lines += ["STRUCTURAL READ", f"  {digest['regime']}", ""]
    if theses:
        lines.append("LONG-TERM THESES (1-3 YEARS)")
        for t in theses:
            lines.append(f"  [{t.get('conviction')}/100] {t.get('theme')} "
                         f"({', '.join(t.get('tickers') or []) or 'no ticker'})")
            if t.get("thesis"):
                lines.append(f"      {t['thesis']}")
            for i in (t.get("invalidation") or [])[:2]:
                lines.append(f"      wrong if: {i}")
        lines.append("")
    items = _calendar_items(digest.get("events"), digest.get("speeches"))
    if items:
        lines.append("WHAT MOVES THE TAPE THIS WEEK")
        day_shown = None
        for kind, item in items:
            day, when = when_parts(item.get("when"))
            if day != day_shown:
                lines.append(f"  {day}")
                day_shown = day
            tag = "speech: " if kind == "speech" else ""
            country = f" ({item.get('country')})" if item.get("country") else ""
            lines.append(f"    {when:>5}  {tag}{item.get('title')}{country}")
        lines.append("")
    if psum.get("open_positions"):
        lines.append("OPEN POSITIONS")
        for p in psum["open_positions"]:
            lines.append(f"  {p['ticker']} {p['pnl_pct']:+.1f}% (${p['value']:,.0f})")
        lines.append("")
    if last:
        lines += ["LAST WEEK",
                  f"  {last.get('n_signals', 0)} signal(s): {last.get('n_executed', 0)} executed, "
                  f"{last.get('n_rejected', 0)} blocked, {last.get('n_advisory', 0)} advisory, "
                  f"{last.get('n_duplicate', 0)} held back as repeats", ""]
    if plan:
        lines += ["HOW THIS WEEK RUNS"] + [f"  - {p}" for p in plan] + [""]
    lines.append(config.DISCLAIMER)

    subject = f"Market Brain: week ahead — {label}"
    return subject, "\n".join(lines), html


def send_email(subject, text, html):
    if not config.GMAIL_USER or not config.GMAIL_PASS:
        print("[warn] GMAIL_USER / GMAIL_APP_PASSWORD unset — email skipped", file=sys.stderr)
        return False
    msg = MIMEMultipart("alternative")
    msg["From"] = f'"Market Brain" <{config.GMAIL_USER}>'
    msg["To"] = config.RECIPIENT
    msg["Subject"] = subject
    msg.attach(MIMEText(text, "plain"))
    msg.attach(MIMEText(html, "html"))
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(config.GMAIL_USER, config.GMAIL_PASS)
            smtp.sendmail(config.GMAIL_USER, config.RECIPIENT, msg.as_string())
        print(f"[email] sent: {subject} -> {config.RECIPIENT}")
        return True
    except Exception as e:  # pragma: no cover - network
        print(f"[warn] send_email failed: {e}", file=sys.stderr)
        return False
