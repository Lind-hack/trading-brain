"""Deterministic data collection — the free, no-LLM half of the brain.

Pulls price bars (yfinance), computes indicators, runs chart-pattern detectors,
gathers news (Google News RSS + Finnhub), reads the ForexFactory economic
calendar, and fetches fundamentals (Finnhub). Everything degrades gracefully:
a missing API key or a flaky feed yields an empty section, never a crash.
"""
from __future__ import annotations

import sys
import time
import math
import urllib.parse
import xml.etree.ElementTree as ET_xml
from datetime import datetime, timedelta

import requests

from . import config

try:
    import yfinance as yf
except ImportError:  # pragma: no cover - install shim, same pattern as stock_signal.py
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "yfinance", "-q"])
    import yfinance as yf

try:
    import pandas as pd
except ImportError:  # pragma: no cover
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "pandas", "-q"])
    import pandas as pd

_HTTP_TIMEOUT = 15
_UA = {"User-Agent": "Mozilla/5.0 (compatible; MarketBrain/1.0; paper-trading research)"}


# ── Indicators ──────────────────────────────────────────────────────────────────

def ema(series, span):
    return series.ewm(span=span, adjust=False).mean()


def macd(close, fast=12, slow=26, sig=9):
    line = ema(close, fast) - ema(close, slow)
    signal = ema(line, sig)
    return line, signal, line - signal


def rsi(close, period=14):
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, float("nan"))
    return 100 - 100 / (1 + rs)


def atr(bars, period=14):
    high, low, close = bars["High"], bars["Low"], bars["Close"]
    prev_close = close.shift(1)
    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()


def bollinger(close, period=20, mult=2.0):
    mid = close.rolling(period).mean()
    sd = close.rolling(period).std()
    return mid, mid + mult * sd, mid - mult * sd, sd


def session_vwap(bars):
    """VWAP over the last trading day's bars (native exchange tz preserved)."""
    if bars.empty:
        return float("nan")
    last_day = bars.index[-1].date()
    sess = bars[[ts.date() == last_day for ts in bars.index]]
    if len(sess) < 2:
        sess = bars.tail(26)
    tp = (sess["High"] + sess["Low"] + sess["Close"]) / 3
    vol = sess["Volume"]
    if vol.sum() > 0:
        return float((tp * vol).sum() / vol.sum())
    return float(tp.mean())


# ── Price bars ──────────────────────────────────────────────────────────────────

def fetch_daily(ticker, period="1y", attempts=2):
    for i in range(attempts):
        try:
            df = yf.Ticker(ticker).history(interval="1d", period=period, auto_adjust=False)
            if not df.empty:
                return df
        except Exception as e:  # pragma: no cover - network
            print(f"[warn] {ticker} daily fetch {i+1}: {e}", file=sys.stderr)
        time.sleep(2 * (i + 1))
    return pd.DataFrame()


def fetch_intraday(ticker, interval="15m", period="5d", attempts=2):
    for i in range(attempts):
        try:
            df = yf.Ticker(ticker).history(interval=interval, period=period, prepost=False)
            if not df.empty:
                # drop the in-progress bar
                step = {"15m": 15, "30m": 30, "60m": 60, "1h": 60}.get(interval, 15)
                now = datetime.now(config.UTC)
                if df.index[-1] + timedelta(minutes=step) > now:
                    df = df.iloc[:-1]
                return df
        except Exception as e:  # pragma: no cover - network
            print(f"[warn] {ticker} {interval} fetch {i+1}: {e}", file=sys.stderr)
        time.sleep(2 * (i + 1))
    return pd.DataFrame()


# ── Chart-pattern detectors (deterministic) ─────────────────────────────────────

def _pivot_levels(daily, lookback=60, tol=0.015):
    """Cluster recent swing highs/lows into support/resistance levels."""
    if len(daily) < 10:
        return []
    window = daily.tail(lookback)
    highs, lows = window["High"], window["Low"]
    pivots = []
    for i in range(2, len(window) - 2):
        h = highs.iloc[i]
        if h >= highs.iloc[i - 2:i + 3].max():
            pivots.append(("R", float(h)))
        l = lows.iloc[i]
        if l <= lows.iloc[i - 2:i + 3].min():
            pivots.append(("S", float(l)))
    # merge nearby pivots
    merged = []
    for kind, lvl in sorted(pivots, key=lambda x: x[1]):
        if merged and abs(lvl - merged[-1][1]) / merged[-1][1] < tol:
            merged[-1] = (merged[-1][0], (merged[-1][1] + lvl) / 2)
        else:
            merged.append((kind, lvl))
    return merged


def _last_cross_age(above_series):
    flips = above_series.ne(above_series.shift(1))
    idx = flips[flips].index
    if len(idx) == 0:
        return None
    pos = above_series.index.get_loc(idx[-1])
    return len(above_series) - 1 - pos


def detect_patterns(ticker, daily, intraday):
    """Return a list of named chart patterns with numbers. Pure price geometry."""
    patterns = []
    if daily is None or len(daily) < 30:
        return patterns

    close = daily["Close"]
    price = float(close.iloc[-1])
    vol = daily["Volume"]
    avg_vol = float(vol.tail(20).mean()) if vol.tail(20).sum() > 0 else 0.0
    last_vol = float(vol.iloc[-1])
    vol_mult = (last_vol / avg_vol) if avg_vol else 0.0

    # 1. N-day breakout / breakdown with volume confirmation
    lb = config.SCREEN_BREAKOUT_LOOKBACK
    prior_high = float(daily["High"].iloc[-lb - 1:-1].max())
    prior_low = float(daily["Low"].iloc[-lb - 1:-1].min())
    if price > prior_high:
        patterns.append({
            "name": f"{lb}-day breakout",
            "detail": f"close {price:.2f} > prior {lb}d high {prior_high:.2f}"
                      + (f" on {vol_mult:.1f}x volume" if vol_mult else ""),
            "bias": "bullish", "strength": 3 if vol_mult >= config.SCREEN_VOL_MULT else 2,
        })
    elif price < prior_low:
        patterns.append({
            "name": f"{lb}-day breakdown",
            "detail": f"close {price:.2f} < prior {lb}d low {prior_low:.2f}"
                      + (f" on {vol_mult:.1f}x volume" if vol_mult else ""),
            "bias": "bearish", "strength": 3 if vol_mult >= config.SCREEN_VOL_MULT else 2,
        })

    # 2. Gap up / down vs prior close
    if len(daily) >= 2:
        prev_close = float(close.iloc[-2])
        today_open = float(daily["Open"].iloc[-1])
        gap = (today_open - prev_close) / prev_close * 100 if prev_close else 0.0
        if abs(gap) >= config.SCREEN_GAP_PCT:
            patterns.append({
                "name": f"gap {'up' if gap > 0 else 'down'}",
                "detail": f"{gap:+.1f}% open gap ({prev_close:.2f} -> {today_open:.2f})",
                "bias": "bullish" if gap > 0 else "bearish", "strength": 2,
            })

    # 3. MA(20/50) cross
    if len(close) >= 55:
        ma20, ma50 = close.rolling(20).mean(), close.rolling(50).mean()
        above = ma20 > ma50
        age = _last_cross_age(above.dropna())
        if age is not None and age <= 3:
            patterns.append({
                "name": f"MA20/50 {'golden' if above.iloc[-1] else 'death'} cross",
                "detail": f"{age} day(s) ago; MA20 {ma20.iloc[-1]:.2f} vs MA50 {ma50.iloc[-1]:.2f}",
                "bias": "bullish" if above.iloc[-1] else "bearish", "strength": 2,
            })

    # 4. RSI divergence (price higher-high but RSI lower-high, or the mirror)
    r = rsi(close).dropna()
    if len(r) >= 20:
        recent_close, recent_rsi = close.tail(14), r.tail(14)
        p_now, p_prev = float(recent_close.iloc[-1]), float(recent_close.iloc[:-5].max())
        r_now, r_prev = float(recent_rsi.iloc[-1]), float(recent_rsi.iloc[:-5].max())
        if p_now >= p_prev and r_now < r_prev - 3:
            patterns.append({
                "name": "bearish RSI divergence",
                "detail": f"price made a higher high but RSI fell to {r_now:.1f} (was {r_prev:.1f})",
                "bias": "bearish", "strength": 2,
            })
        p_low_now, p_low_prev = float(recent_close.iloc[-1]), float(recent_close.iloc[:-5].min())
        r_low_now, r_low_prev = float(recent_rsi.iloc[-1]), float(recent_rsi.iloc[:-5].min())
        if p_low_now <= p_low_prev and r_low_now > r_low_prev + 3:
            patterns.append({
                "name": "bullish RSI divergence",
                "detail": f"price made a lower low but RSI rose to {r_low_now:.1f} (was {r_low_prev:.1f})",
                "bias": "bullish", "strength": 2,
            })

    # 5. Bollinger squeeze (volatility compression — breakout pending)
    mid, up, lo, sd = bollinger(close)
    if not math.isnan(sd.iloc[-1]) and mid.iloc[-1]:
        band_w = (up.iloc[-1] - lo.iloc[-1]) / mid.iloc[-1]
        band_hist = ((up - lo) / mid).dropna().tail(120)
        if len(band_hist) > 20 and band_w <= band_hist.quantile(0.15):
            patterns.append({
                "name": "Bollinger squeeze",
                "detail": f"band width {band_w*100:.1f}% — tightest 15% of the last 120 days",
                "bias": "neutral", "strength": 2,
            })

    # 6. 52-week-high / low proximity
    hi52 = float(daily["High"].tail(252).max())
    lo52 = float(daily["Low"].tail(252).min())
    if hi52 and (hi52 - price) / hi52 <= config.SCREEN_52W_PROXIMITY:
        patterns.append({
            "name": "near 52-week high",
            "detail": f"{price:.2f} within {(hi52-price)/hi52*100:.1f}% of 52w high {hi52:.2f}",
            "bias": "bullish", "strength": 1,
        })
    elif lo52 and (price - lo52) / lo52 <= config.SCREEN_52W_PROXIMITY:
        patterns.append({
            "name": "near 52-week low",
            "detail": f"{price:.2f} within {(price-lo52)/lo52*100:.1f}% of 52w low {lo52:.2f}",
            "bias": "bearish", "strength": 1,
        })

    # 7. Support/resistance pivot proximity
    for kind, lvl in _pivot_levels(daily):
        if lvl and abs(price - lvl) / lvl < 0.01:
            patterns.append({
                "name": f"testing {'resistance' if kind == 'R' else 'support'}",
                "detail": f"price {price:.2f} at {'resistance' if kind == 'R' else 'support'} {lvl:.2f}",
                "bias": "bearish" if kind == "R" else "bullish", "strength": 1,
            })
            break

    return patterns


def indicator_snapshot(daily, intraday):
    """Named indicator values for the email + deep-run packet."""
    snap = {}
    if daily is not None and len(daily) >= 30:
        close = daily["Close"]
        snap["price"] = round(float(close.iloc[-1]), 2)
        snap["rsi14_d"] = round(float(rsi(close).iloc[-1]), 1)
        line, sigl, hist = macd(close)
        snap["macd_d"] = f"{'bullish' if line.iloc[-1] > sigl.iloc[-1] else 'bearish'}, hist {hist.iloc[-1]:+.2f}"
        snap["ma20"] = round(float(close.rolling(20).mean().iloc[-1]), 2)
        snap["ma50"] = round(float(close.rolling(50).mean().iloc[-1]), 2) if len(close) >= 50 else None
        snap["atr14"] = round(float(atr(daily).iloc[-1]), 2)
        vol = daily["Volume"]
        avg = float(vol.tail(20).mean()) if vol.tail(20).sum() else 0.0
        snap["vol_vs_avg"] = round(float(vol.iloc[-1]) / avg, 2) if avg else None
    if intraday is not None and not intraday.empty:
        vwap = session_vwap(intraday)
        snap["vwap_intraday"] = round(vwap, 2)
        px = float(intraday["Close"].iloc[-1])
        snap["price_vs_vwap"] = "above" if px > vwap else "below"
        snap["rsi14_intraday"] = round(float(rsi(intraday["Close"]).iloc[-1]), 1)
    return snap


# ── News ────────────────────────────────────────────────────────────────────────

def google_news(query, limit=6):
    """Google News RSS — no key needed. Returns [{title, source, published, link}]."""
    url = ("https://news.google.com/rss/search?q="
           + urllib.parse.quote(query)
           + "&hl=en-US&gl=US&ceid=US:en")
    out = []
    try:
        r = requests.get(url, headers=_UA, timeout=_HTTP_TIMEOUT)
        r.raise_for_status()
        root = ET_xml.fromstring(r.content)
        for item in root.findall(".//item")[:limit]:
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            pub = (item.findtext("pubDate") or "").strip()
            src_el = item.find("{*}source")
            source = (src_el.text if src_el is not None else "") or ""
            # Google appends " - Source" to titles; split it out when present
            if not source and " - " in title:
                title, source = title.rsplit(" - ", 1)
            out.append({"title": title, "source": source, "published": pub, "link": link})
    except Exception as e:  # pragma: no cover - network
        print(f"[warn] google_news '{query}': {e}", file=sys.stderr)
    return out


def finnhub_news(ticker, days=5, limit=6):
    """Finnhub company news (needs FINNHUB_API_KEY). Empty list without a key."""
    if not config.FINNHUB_API_KEY:
        return []
    to = datetime.now(config.UTC).date()
    frm = to - timedelta(days=days)
    try:
        r = requests.get(
            "https://finnhub.io/api/v1/company-news",
            params={"symbol": ticker, "from": str(frm), "to": str(to),
                    "token": config.FINNHUB_API_KEY},
            headers=_UA, timeout=_HTTP_TIMEOUT)
        r.raise_for_status()
        rows = r.json() or []
        out = []
        for n in rows[:limit]:
            out.append({
                "title": (n.get("headline") or "").strip(),
                "source": n.get("source") or "Finnhub",
                "published": datetime.fromtimestamp(n.get("datetime", 0), config.UTC).isoformat(),
                "link": n.get("url") or "",
            })
        return out
    except Exception as e:  # pragma: no cover - network
        print(f"[warn] finnhub_news {ticker}: {e}", file=sys.stderr)
        return []


# ── Fundamentals (Finnhub) ──────────────────────────────────────────────────────

def finnhub_fundamentals(ticker):
    """Latest quarterly revenue/EPS + surprise + next earnings date. {} without a key."""
    if not config.FINNHUB_API_KEY:
        return {}
    out = {}
    tok = config.FINNHUB_API_KEY
    try:
        r = requests.get("https://finnhub.io/api/v1/stock/earnings",
                         params={"symbol": ticker, "limit": 4, "token": tok},
                         headers=_UA, timeout=_HTTP_TIMEOUT)
        r.raise_for_status()
        earn = r.json() or []
        if earn:
            last = earn[0]
            out["last_eps_actual"] = last.get("actual")
            out["last_eps_estimate"] = last.get("estimate")
            out["last_eps_surprise_pct"] = last.get("surprisePercent")
            out["last_eps_period"] = last.get("period")
    except Exception as e:  # pragma: no cover - network
        print(f"[warn] finnhub earnings {ticker}: {e}", file=sys.stderr)
    try:
        r = requests.get("https://finnhub.io/api/v1/stock/metric",
                         params={"symbol": ticker, "metric": "all", "token": tok},
                         headers=_UA, timeout=_HTTP_TIMEOUT)
        r.raise_for_status()
        m = (r.json() or {}).get("metric", {}) or {}
        out["revenue_ttm"] = m.get("revenuePerShareTTM")
        out["pe_ttm"] = m.get("peTTM")
        out["profit_margin"] = m.get("netProfitMarginTTM")
        out["52w_high"] = m.get("52WeekHigh")
        out["52w_low"] = m.get("52WeekLow")
    except Exception as e:  # pragma: no cover - network
        print(f"[warn] finnhub metric {ticker}: {e}", file=sys.stderr)
    return out


def finnhub_earnings_calendar(days=10):
    """Upcoming earnings for the focus list in the next `days`. [] without a key."""
    if not config.FINNHUB_API_KEY:
        return []
    frm = datetime.now(config.UTC).date()
    to = frm + timedelta(days=days)
    watch = set(config.FOCUS_TICKERS)
    try:
        r = requests.get("https://finnhub.io/api/v1/calendar/earnings",
                         params={"from": str(frm), "to": str(to),
                                 "token": config.FINNHUB_API_KEY},
                         headers=_UA, timeout=_HTTP_TIMEOUT)
        r.raise_for_status()
        rows = (r.json() or {}).get("earningsCalendar", []) or []
        return [{"ticker": e.get("symbol"), "date": e.get("date"),
                 "hour": e.get("hour"), "eps_estimate": e.get("epsEstimate")}
                for e in rows if e.get("symbol") in watch]
    except Exception as e:  # pragma: no cover - network
        print(f"[warn] finnhub earnings calendar: {e}", file=sys.stderr)
        return []


# ── ForexFactory economic calendar (official JSON feed, no scraping) ────────────

def _parse_ff_dt(raw):
    if not raw:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.strptime(raw, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=config.UTC)
            return dt
        except ValueError:
            continue
    return None


def forexfactory_calendar(include_next_week=False):
    """High-impact USD events + speeches from ForexFactory's official weekly JSON feed.

    Returns {"events": [...], "speeches": [...], "imminent": [...]} where each event has
    title/country/impact/when(UTC ISO)/hours_away/actual/forecast/previous. Speeches
    ("... Speaks", Trump/Powell testimony) are split out so callers can trigger targeted
    news gathering ahead of them.
    """
    now = datetime.now(config.UTC)
    urls = [config.FF_FEED_URL]
    if include_next_week and config.FF_NEXTWEEK_URL:
        urls.append(config.FF_NEXTWEEK_URL)
    events, speeches = [], []
    seen = set()
    for url in urls:
        try:
            r = requests.get(url, headers=_UA, timeout=_HTTP_TIMEOUT)
            r.raise_for_status()
            rows = r.json() or []
        except Exception as e:  # pragma: no cover - network
            print(f"[warn] forexfactory {url}: {e}", file=sys.stderr)
            continue
        for ev in rows:
            title = (ev.get("title") or "").strip()
            country = (ev.get("country") or "").strip()
            impact = (ev.get("impact") or "").strip()
            when = _parse_ff_dt(ev.get("date"))
            key = (title, country, ev.get("date"))
            if key in seen:
                continue
            seen.add(key)
            is_speech = any(k.lower() in title.lower() for k in config.SPEECH_KEYWORDS)
            is_watched = any(k.lower() in title.lower() for k in config.FF_WATCH_KEYWORDS)
            is_high = impact.lower() in ("high", "holiday") or is_watched
            if not (is_high or is_speech):
                continue
            hours_away = (when - now).total_seconds() / 3600 if when else None
            rec = {
                "title": title, "country": country, "impact": impact,
                "when": when.isoformat() if when else None,
                "hours_away": round(hours_away, 1) if hours_away is not None else None,
                "actual": ev.get("actual"), "forecast": ev.get("forecast"),
                "previous": ev.get("previous"),
            }
            if is_speech:
                speeches.append(rec)
            events.append(rec)
    # imminent = high-impact and inside the screener horizon, or a speech within 24h
    imminent = [e for e in events
                if e["hours_away"] is not None
                and -0.5 <= e["hours_away"] <= config.SCREEN_EVENT_HORIZON_H]
    trump_soon = [s for s in speeches
                  if s["hours_away"] is not None and -2 <= s["hours_away"] <= 24
                  and "trump" in s["title"].lower()]
    return {"events": events, "speeches": speeches,
            "imminent": imminent, "trump_soon": trump_soon}


# ── Per-ticker + market assembly ────────────────────────────────────────────────

def collect_ticker(ticker, with_news=False, with_fundamentals=False):
    """Full snapshot for one ticker: bars-derived indicators, patterns, optional news/fundamentals."""
    daily = fetch_daily(ticker)
    intraday = fetch_intraday(ticker) if not ticker.startswith("^") and "=" not in ticker else pd.DataFrame()
    snap = {
        "ticker": ticker,
        "indicators": indicator_snapshot(daily, intraday if not intraday.empty else None),
        "patterns": detect_patterns(ticker, daily, intraday if not intraday.empty else None),
    }
    if with_news:
        company = ticker
        snap["news"] = (finnhub_news(ticker) or google_news(f"{company} stock", limit=5))
    if with_fundamentals:
        snap["fundamentals"] = finnhub_fundamentals(ticker)
    return snap


def collect_market(tickers=None, with_news=False, with_fundamentals=False, max_workers=6):
    """Snapshot the focus list + market context. Returns a dict keyed by ticker."""
    tickers = tickers or (config.MARKET_CONTEXT + config.FOCUS_TICKERS)
    from concurrent.futures import ThreadPoolExecutor

    def _job(t):
        try:
            return t, collect_ticker(t, with_news=with_news, with_fundamentals=with_fundamentals)
        except Exception as e:  # pragma: no cover - network
            print(f"[warn] collect {t}: {e}", file=sys.stderr)
            return t, {"ticker": t, "indicators": {}, "patterns": [], "error": str(e)}

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        return dict(ex.map(_job, tickers))
