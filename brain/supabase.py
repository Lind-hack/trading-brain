"""Dashboard feed — pushes brain runs to Supabase for the Signal Deck AI Brain section.

Same project + x-signal-key RLS pattern as the stock engine's supabase_log(). Four tables
(vps/brain.sql): sd_brain_scans (heartbeat per run), sd_brain_signals (one row per AI
signal), sd_trades (the live trade tape — one row per paper fill/exit, pushed the moment it
happens so signal-deck shows it without waiting for the next snapshot), and sd_portfolio
(latest paper-portfolio snapshot). All writes are best-effort — a dashboard outage never
breaks a cycle.
"""
from __future__ import annotations

import sys
from datetime import datetime

import requests

from . import config


def _headers():
    return {
        "apikey": config.SUPABASE_ANON,
        "Authorization": f"Bearer {config.SUPABASE_ANON}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
        "x-signal-key": config.SIGNAL_INGEST_KEY,
    }


def _enabled():
    return bool(config.SUPABASE_URL and config.SUPABASE_ANON and config.SIGNAL_INGEST_KEY)


_NEW_SIGNAL_COLS = ("confidence_rationale", "indicators_used", "news_read")


def _post_signals(headers, rows):
    """Insert signal rows, degrading gracefully if the migration hasn't been run yet.

    `confidence_rationale`, `indicators_used` and `news_read` are added by the newer
    vps/brain.sql. Against an un-migrated table PostgREST rejects the whole batch with
    PGRST204, which would silently drop every signal from the dashboard until someone runs
    the SQL. Retry once without those columns so the old dashboard keeps working.
    """
    url = f"{config.SUPABASE_URL}/rest/v1/sd_brain_signals"
    r = requests.post(url, headers=headers, timeout=15, json=rows)
    if r.status_code < 400:
        return
    if any(c in r.text for c in _NEW_SIGNAL_COLS):
        print("[warn] sd_brain_signals is missing the newer columns — run vps/brain.sql. "
              "Pushing without them for now.", file=sys.stderr)
        trimmed = [{k: v for k, v in row.items() if k not in _NEW_SIGNAL_COLS} for row in rows]
        requests.post(url, headers=headers, timeout=15, json=trimmed).raise_for_status()
        return
    r.raise_for_status()


def push(analysis, screen_result, portfolio_summary, mode, now_et, escalated):
    if not _enabled():
        return False
    ts = now_et.astimezone(config.UTC).isoformat()
    h = _headers()
    signals = (analysis or {}).get("signals", []) or []
    try:
        requests.post(f"{config.SUPABASE_URL}/rest/v1/sd_brain_scans", headers=h, timeout=15,
                      json={"ts": ts, "mode": mode, "escalated": escalated,
                            "n_signals": len(signals),
                            "outlook": (analysis or {}).get("market_outlook"),
                            "screen_why": screen_result.get("why", []),
                            "degraded": bool((analysis or {}).get("degraded"))}).raise_for_status()
        if signals:
            rows = []
            for s in signals:
                rows.append({
                    "ts": ts, "mode": mode, "ticker": s.get("ticker"),
                    "direction": s.get("direction"), "trade_type": s.get("trade_type"),
                    "holding_period": s.get("holding_period"), "confidence": s.get("confidence"),
                    "entry": s.get("entry"), "stop": s.get("stop"),
                    "target1": s.get("target1"), "target2": s.get("target2"),
                    "why": s.get("why"), "analysis_done": s.get("analysis_done"),
                    "indicators": s.get("indicators"), "chart_read": s.get("chart_read"),
                    "news": s.get("news") or [], "historical_analog": s.get("historical_analog"),
                    "data_sources": s.get("data_sources") or [],
                    "confidence_rationale": s.get("confidence_rationale"),
                    "indicators_used": s.get("indicators_used") or [],
                    "news_read": s.get("news_read"),
                })
            _post_signals(h, rows)
        if portfolio_summary:
            requests.post(f"{config.SUPABASE_URL}/rest/v1/sd_portfolio", headers=h, timeout=15,
                          json={"ts": ts, "equity": portfolio_summary.get("equity"),
                                "cash": portfolio_summary.get("cash"),
                                "total_return_pct": portfolio_summary.get("total_return_pct"),
                                "n_open": portfolio_summary.get("n_open"),
                                "win_rate": portfolio_summary.get("win_rate"),
                                "positions": portfolio_summary.get("open_positions", [])}).raise_for_status()
        print(f"[dashboard] pushed brain {mode} run ({len(signals)} signal(s))")
        return True
    except Exception as e:  # pragma: no cover - network
        print(f"[warn] dashboard push failed: {e}", file=sys.stderr)
        return False


# ── Live trade tape ─────────────────────────────────────────────────────────────

def _post_trade(row):
    if not _enabled():
        return False
    try:
        requests.post(f"{config.SUPABASE_URL}/rest/v1/sd_trades", headers=_headers(),
                      timeout=15, json=row).raise_for_status()
        print(f"[dashboard] live trade: {row['event']} {row['ticker']}")
        return True
    except Exception as e:  # pragma: no cover - network
        print(f"[warn] live trade push failed ({row.get('event')} {row.get('ticker')}): {e}",
              file=sys.stderr)
        return False


def push_trade_open(ticker, position, signal=None, action=None, equity=None, event="OPEN"):
    """Push a fill to the dashboard the instant the gates approve it."""
    sig = signal or {}
    act = action or {}
    mirror = position.get("broker") or {}
    return _post_trade({
        "ts": datetime.now(config.UTC).isoformat(),
        "event": event,
        "ticker": ticker,
        "sector": position.get("sector"),
        "direction": sig.get("direction") or "LONG",
        "trade_type": sig.get("trade_type") or position.get("trade_type"),
        "holding_period": sig.get("holding_period"),
        "confidence": sig.get("confidence", position.get("confidence")),
        "shares": position.get("shares"),
        "entry": position.get("entry"),
        "stop": position.get("hard_stop"),
        "target": sig.get("target1") or position.get("target"),
        "reason": sig.get("why") or act.get("reason"),
        "thesis": position.get("thesis") or sig.get("why"),
        "indicators_used": sig.get("indicators_used") or position.get("indicators_used") or [],
        "news": sig.get("news") or position.get("news_at_entry") or [],
        "broker_mirrored": bool(mirror.get("mirrored")),
        "broker_order_id": str(mirror.get("order_id")) if mirror.get("order_id") else None,
        "opened_at": position.get("opened"),
        "equity_after": equity,
    })


def push_trade_close(exit_rec, equity=None):
    """Push an exit — mechanical stop or proposed sell — as it happens."""
    mirror = exit_rec.get("broker") or {}
    return _post_trade({
        "ts": datetime.now(config.UTC).isoformat(),
        "event": "CLOSE",
        "ticker": exit_rec.get("ticker"),
        "sector": exit_rec.get("sector"),
        "trade_type": exit_rec.get("trade_type"),
        "confidence": exit_rec.get("confidence"),
        "entry": exit_rec.get("entry"),
        "exit": exit_rec.get("exit"),
        "pnl_usd": exit_rec.get("pnl_usd"),
        "pnl_pct": exit_rec.get("pnl_pct"),
        "reason": exit_rec.get("reason"),
        "thesis": exit_rec.get("thesis"),
        "broker_mirrored": bool(mirror.get("mirrored")),
        "broker_order_id": str(mirror.get("order_id")) if mirror.get("order_id") else None,
        "opened_at": exit_rec.get("opened"),
        "closed_at": exit_rec.get("closed") or datetime.now(config.UTC).isoformat(),
        "equity_after": equity,
    })
