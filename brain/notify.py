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
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from . import config

_TYPE_META = {
    "SCALP": ("⚡ SCALP", "#f59e0b", "minutes–hours", "Quick in-and-out on momentum. Small size, tight stop, take profit fast."),
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


def build_card(sig, now_et, intel_ticker=None):
    ttype = (sig.get("trade_type") or "SHORT_TERM").upper()
    label, tcolor, default_hold, blurb = _TYPE_META.get(ttype, _TYPE_META["SHORT_TERM"])
    direction = (sig.get("direction") or "LONG").upper()
    dcolor = "#22c55e" if direction == "LONG" else "#ef4444"
    demoji = "🟢" if direction == "LONG" else "🔴"
    ticker = sig.get("ticker", "?")
    conf = sig.get("confidence")
    hold = sig.get("holding_period") or default_hold

    def fmt(x):
        return f"{x:,.2f}" if isinstance(x, (int, float)) else "—"

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

    return f"""
  <tr><td style="background:#111;border:1px solid {dcolor}55;border-radius:14px;padding:24px 26px;">
    <div style="display:inline-block;background:{tcolor};color:#0a0a0a;font-weight:800;font-size:12px;
         padding:5px 12px;border-radius:999px;letter-spacing:0.04em;">{label} · {hold}</div>
    <p style="margin:10px 0 2px;font-size:12px;color:#9ca3af;">{blurb}</p>
    <h1 style="margin:12px 0 2px;font-size:30px;font-weight:800;color:{dcolor};">{demoji} {ticker} {direction}</h1>
    <p style="margin:0 0 14px;font-size:12px;color:#6b7280;">
      {now_et.strftime('%a %b %d, %I:%M %p ET')} · confidence {conf if conf is not None else '—'}/100 · data delayed ~15 min</p>

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


def _portfolio_card(psum):
    if not psum:
        return ""
    rows = "".join(
        f'<tr><td style="padding:4px 0;font-size:13px;color:#e5e7eb;">{p["ticker"]}</td>'
        f'<td style="padding:4px 0;font-size:13px;text-align:right;color:{"#22c55e" if p["pnl_pct"]>=0 else "#ef4444"};">'
        f'{p["pnl_pct"]:+.1f}%</td>'
        f'<td style="padding:4px 0;font-size:13px;text-align:right;color:#9ca3af;">${p["value"]:,.0f}</td></tr>'
        for p in psum.get("open_positions", []))
    if not rows:
        rows = '<tr><td colspan="3" style="padding:4px 0;font-size:13px;color:#6b7280;">No open positions.</td></tr>'
    ret = psum.get("total_return_pct", 0)
    rc = "#22c55e" if ret >= 0 else "#ef4444"
    return f"""
  <tr><td style="background:#0f172a;border:1px solid #1e293b;border-radius:14px;padding:22px 26px;">
    <p style="margin:0 0 4px;font-size:11px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;color:#60a5fa;">Paper portfolio (simulated)</p>
    <p style="margin:0;font-size:26px;font-weight:800;color:#fff;">${psum.get("equity",0):,.0f}
      <span style="font-size:15px;color:{rc};">({ret:+.1f}%)</span></p>
    <p style="margin:2px 0 12px;font-size:12px;color:#6b7280;">
      cash ${psum.get("cash",0):,.0f} · {psum.get("n_open",0)}/{config.MAX_POSITIONS} positions ·
      {psum.get("new_trades_this_week",0)}/{config.MAX_NEW_TRADES_PER_WEEK} new trades this week ·
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


def build_email(analysis, portfolio_summary, mode, now_et, intel=None, applied=None, exits=None):
    signals = analysis.get("signals", []) or []
    outlook = analysis.get("market_outlook", "")
    degraded = analysis.get("degraded")
    intel_tickers = (intel or {}).get("tickers") or {}

    scalps = [s for s in signals if (s.get("trade_type") or "").upper() == "SCALP"]
    shorts = [s for s in signals if (s.get("trade_type") or "").upper() == "SHORT_TERM"]
    longs = [s for s in signals if (s.get("trade_type") or "").upper() == "LONG_TERM"]
    ordered = scalps + shorts + longs + [s for s in signals if s not in scalps + shorts + longs]

    if signals:
        tags = ", ".join(f"{s.get('ticker')} {s.get('direction','')}" for s in ordered[:5])
        prefix = "⚠️ " if degraded else ""
        subject = f"{prefix}Market Brain: {tags} — {now_et.strftime('%I:%M %p ET')}"
    else:
        subject = f"Market Brain: {mode} recap — {now_et.strftime('%I:%M %p ET')}"

    header = f"""
  <tr><td style="padding:0 0 18px;">
    <p style="margin:0;font-size:11px;color:#6b7280;text-transform:uppercase;letter-spacing:0.1em;">Market Brain · {mode.upper()} · Paper trading only</p>
    <p style="margin:8px 0 0;font-size:14px;color:#d1d5db;line-height:1.5;">{outlook}</p>
  </td></tr>"""

    cards = "".join(build_card(s, now_et, intel_tickers.get(s.get("ticker"))) for s in ordered)
    pcard = _portfolio_card(portfolio_summary)
    mcard = _macro_card(intel)
    acard = _applied_card(applied, exits, (portfolio_summary or {}).get("broker"))

    html = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#0a0a0a;font-family:-apple-system,'Segoe UI',Helvetica,Arial,sans-serif;color:#e5e7eb;">
<table width="100%" cellpadding="0" cellspacing="0"><tr><td align="center" style="padding:32px 16px;">
<table width="560" cellpadding="0" cellspacing="0" style="max-width:560px;width:100%;">
{header}{mcard}{acard}{cards}{pcard}
<tr><td style="padding:8px 4px;font-size:11px;color:#4b5563;text-align:center;">{config.DISCLAIMER}</td></tr>
</table></td></tr></table>
</body></html>"""

    # plain-text fallback
    lines = [f"MARKET BRAIN — {mode.upper()} ({now_et.strftime('%a %b %d %I:%M %p ET')})", "", outlook, ""]
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
    for s in ordered:
        lines.append(f"[{(s.get('trade_type') or '').upper()}] {s.get('ticker')} {s.get('direction')} "
                     f"(conf {s.get('confidence')}) — hold {s.get('holding_period')}")
        lines.append(f"  Why: {s.get('why','')}")
        lines.append(f"  Confidence: {s.get('confidence')} — {s.get('confidence_rationale','')}")
        lines.append(f"  Plan: entry {s.get('entry')} stop {s.get('stop')} T1 {s.get('target1')} T2 {s.get('target2')}")
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
    if portfolio_summary:
        lines.append(f"PAPER PORTFOLIO: ${portfolio_summary.get('equity',0):,.0f} "
                     f"({portfolio_summary.get('total_return_pct',0):+.1f}%)")
    lines.append(config.DISCLAIMER)
    return subject, "\n".join(lines), html


def build_weekly_email(recap, now_et):
    """The Sunday recap: how the week went, every trade, and what to change to be more accurate."""
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
      {_row("Closed trades", st.get("n_closed", 0))}
      {_row("Opened this week", st.get("n_opened", 0))}
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
  </td></tr>
  <tr><td style="height:16px;"></td></tr>

  <tr><td style="background:#0b1220;border:1px solid #60a5fa44;border-radius:14px;padding:22px 26px;">
    {_section("What to change to be more accurate", _list(recap.get("changes"), "No rule changes proposed."), "#60a5fa")}
    {_section("Mistakes", _list(recap.get("mistakes"), "None identified."), "#ef4444")}
    {_section("What worked", _list(recap.get("successes"), "Nothing conclusive yet."), "#22c55e")}
    {_section("Per-trade grading (rules-based)", f'<ul style="margin:0;padding-left:18px;">{grade_html}</ul>' if grade_html else '<p style="margin:0;font-size:13px;color:#6b7280;">No closed trades to grade.</p>')}
  </td></tr>
  <tr><td style="padding:14px 4px;font-size:11px;color:#4b5563;text-align:center;">{config.DISCLAIMER}</td></tr>
</table></td></tr></table>
</body></html>"""

    lines = [f"MARKET BRAIN — WEEKLY RECAP ({label})", "",
             f"Week: {ret:+.2f}%  (${st.get('equity_start',0):,.0f} -> ${st.get('equity_end',0):,.0f})",
             f"Realised ${st.get('realized_usd',0):+,.0f} across {st.get('n_closed',0)} closed trade(s)",
             f"Win rate: {wr if wr is not None else 'n/a'}  ·  best {st.get('best','—')} / worst {st.get('worst','—')}",
             "", "HOW THE WEEK WENT", recap.get("narrative") or "—", "", "TRADES CLOSED"]
    for c in recap.get("closed", []) or []:
        lines.append(f"  {c.get('ticker')} [{c.get('trade_type') or '—'}] conf {c.get('confidence')} "
                     f"{c.get('entry')} -> {c.get('exit')} {c.get('pnl_pct',0):+.1f}% "
                     f"(${c.get('pnl_usd',0):+,.0f}) — {c.get('reason','')}")
    if not recap.get("closed"):
        lines.append("  none")
    lines += ["", "WHAT TO CHANGE"]
    lines += [f"  {i}. {c}" for i, c in enumerate(recap.get("changes") or [], 1)] or ["  none"]
    if recap.get("mistakes"):
        lines += ["", "MISTAKES"] + [f"  - {m}" for m in recap["mistakes"]]
    if recap.get("successes"):
        lines += ["", "WHAT WORKED"] + [f"  - {s}" for s in recap["successes"]]
    lines += ["", config.DISCLAIMER]

    subject = (f"Market Brain: trading week {ret:+.2f}% — "
               f"{st.get('n_closed',0)} closed, {label}")
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
