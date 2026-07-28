"""Deterministic data collection — the free, no-LLM half of the brain.

Pulls price bars (yfinance), computes indicators, runs chart-pattern detectors,
gathers news (Google News RSS + Finnhub), reads the ForexFactory economic
calendar, and fetches fundamentals (Finnhub). Everything degrades gracefully:
a missing API key or a flaky feed yields an empty section, never a crash.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import math
import urllib.parse
import xml.etree.ElementTree as ET_xml
from datetime import datetime, timedelta

import requests

from . import config, market_hours

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

def _drop_blank_bars(df):
    """Remove rows with no close.

    yfinance intermittently returns a placeholder row for the current session (and sometimes
    for a halt) with NaN OHLC. One such row at the tail is enough to make every rolling mean
    NaN, which turned into an invented "MA20/50 death cross (MA20 nan vs MA50 nan)" on MU and
    escalated a paid deep run on garbage. A bar with no close is not a bar.
    """
    if df is None or df.empty or "Close" not in df:
        return df
    return df.dropna(subset=["Close"])


def fetch_daily(ticker, period="1y", attempts=2):
    for i in range(attempts):
        try:
            df = _drop_blank_bars(
                yf.Ticker(ticker).history(interval="1d", period=period, auto_adjust=False,
                                          timeout=config.YF_TIMEOUT))
            if not df.empty:
                return df
        except Exception as e:  # pragma: no cover - network
            print(f"[warn] {ticker} daily fetch {i+1}: {e}", file=sys.stderr)
        time.sleep(2 * (i + 1))
    return pd.DataFrame()


def fetch_intraday(ticker, interval="15m", period="5d", attempts=2):
    for i in range(attempts):
        try:
            df = _drop_blank_bars(
                yf.Ticker(ticker).history(interval=interval, period=period, prepost=False,
                                          timeout=config.YF_TIMEOUT))
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


# ── Daily-bar cache ─────────────────────────────────────────────────────────────
#
# A 1-year daily frame is ~250 bars, and exactly one of them — today's — can still change. The
# pipeline was re-downloading all 250 for ~50 tickers every 30 minutes to learn one number it
# already has from the intraday fetch it makes in the same breath. So: keep the frame for the
# session, and rebuild today's bar locally from the intraday bars.
#
# The splice is what makes this safe. Without it a cached frame would freeze `indicators.price`,
# and that price is what marks the portfolio and fires the -7% hard stop — a stale quote there is
# not a slow cache, it is a wrong risk decision.

_bars_mem = {}                     # ticker -> (session date, frame) for this process
_bars_lock = threading.Lock()


def _et_date(ts):
    """The ET calendar date of a bar timestamp, tz-aware or not."""
    try:
        return (ts.tz_convert(config.ET) if ts.tzinfo else ts).date()
    except (AttributeError, TypeError):     # pragma: no cover - defensive
        return None


def _bars_path(ticker):
    safe = "".join(c for c in ticker if c.isalnum() or c in "-.^=") or "_"
    return config.BARS_CACHE_DIR / f"{safe}.csv"


def _read_cached_daily(ticker):
    """(session_date, frame) from disk, or (None, None). A corrupt file is a miss, not a crash."""
    path = _bars_path(ticker)
    try:
        if not path.exists():
            return None, None
        df = pd.read_csv(path, index_col=0)
        df.index = pd.to_datetime(df.index, utc=True, format="mixed").tz_convert(config.ET)
        if df.empty or "Close" not in df:
            return None, None
        # The session the cache was written for is stamped in the filename's mtime rather than
        # the frame, because a frame fetched pre-market legitimately has no bar for today yet.
        fetched = datetime.fromtimestamp(path.stat().st_mtime, config.ET)
        return fetched.date(), df
    except Exception as e:  # pragma: no cover - disk/parse
        print(f"[warn] bars cache read {ticker}: {e}", file=sys.stderr)
        return None, None


def _write_cached_daily(ticker, df):
    try:
        config.BARS_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path = _bars_path(ticker)
        tmp = path.with_suffix(".tmp")
        df.to_csv(tmp)
        os.replace(tmp, path)
    except Exception as e:  # pragma: no cover - disk
        print(f"[warn] bars cache write {ticker}: {e}", file=sys.stderr)


def _splice_today(daily, intraday, now_et=None):
    """Rebuild today's daily bar from today's intraday bars.

    Returns a new frame; the input is never mutated. If the intraday feed has nothing for today
    (pre-market, or a name with no intraday coverage) the frame comes back unchanged and the
    caller falls back to a real fetch rather than serving yesterday's close as today's price.
    """
    if daily is None or daily.empty or intraday is None or intraday.empty:
        return daily, False
    today = (now_et or datetime.now(config.ET)).date()
    rows = intraday[[_et_date(ts) == today for ts in intraday.index]]
    if rows.empty:
        return daily, False
    out = daily.copy()
    if out.index.size and _et_date(out.index[-1]) == today:
        out = out.iloc[:-1]                      # replace the stale partial bar
    stamp = pd.Timestamp(datetime.combine(today, datetime.min.time()), tz=config.ET)
    bar = {"Open": float(rows["Open"].iloc[0]), "High": float(rows["High"].max()),
           "Low": float(rows["Low"].min()), "Close": float(rows["Close"].iloc[-1]),
           "Volume": float(rows["Volume"].sum())}
    for col in out.columns:
        if col not in bar:
            bar[col] = 0.0                       # Dividends / Stock Splits
    out.loc[stamp] = [bar[c] for c in out.columns]
    return out, True


def daily_bars(ticker, intraday=None, period="1y", now_et=None):
    """Today's daily frame, from cache when the cache can be made current, else from the wire."""
    if not config.BARS_CACHE:
        return fetch_daily(ticker, period=period)
    # Crypto goes straight to the wire. The cache below is built on the ET trading day: it splices
    # today's intraday bars into today's daily bar and, failing that, trusts yesterday's frame
    # whenever `market_hours.is_open()` says the session is shut. Both halves are wrong for a token.
    # Yahoo stamps crypto daily bars at 00:00 UTC, which is the *previous* ET date, so the splice
    # would append a second bar for the same day instead of replacing it and the frame would grow a
    # phantom bar per cycle. Worse, the equity market is shut for most of crypto's week, so the
    # fallback would serve a frozen price to a book whose stops run 24/7 — a stale quote there is
    # not a slow cache, it is a wrong risk decision. Three tickers on the wire per cycle is the
    # cheaper side of that trade by a wide margin.
    if config.is_crypto(ticker):
        return fetch_daily(ticker, period=period)
    now_et = now_et or datetime.now(config.ET)
    today = now_et.date()

    with _bars_lock:
        hit = _bars_mem.get(ticker)
    if hit is None:
        day, df = _read_cached_daily(ticker)
        hit = (day, df) if df is not None else None
        if hit:
            with _bars_lock:
                _bars_mem[ticker] = hit

    if hit and hit[0] == today and hit[1] is not None and len(hit[1]) >= 30:
        spliced, ok = _splice_today(hit[1], intraday, now_et)
        if ok:
            return spliced
        # Nothing to splice with. Before the open that is correct — yesterday's frame IS the
        # frame — but once the session is running a missing today bar means a stale price.
        if not market_hours.is_open(now_et):
            return hit[1]

    df = fetch_daily(ticker, period=period)
    if not df.empty:
        with _bars_lock:
            _bars_mem[ticker] = (today, df)
        _write_cached_daily(ticker, df)
    return df


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
    # Venue-specific bars. Dispatched on the symbol so a crypto snapshot can never be measured
    # against the equity numbers — a 1% "gap" is ordinary drift on a token that never closes.
    th = config.thresholds_for(ticker)

    close = daily["Close"]
    price = float(close.iloc[-1])
    if not math.isfinite(price):
        return patterns   # no usable last close — every "pattern" here would be NaN theatre
    vol = daily["Volume"]
    avg_vol = float(vol.tail(20).mean()) if vol.tail(20).sum() > 0 else 0.0
    last_vol = float(vol.iloc[-1])
    vol_mult = (last_vol / avg_vol) if avg_vol else 0.0

    # 1. N-day breakout / breakdown with volume confirmation
    lb = th.breakout_lookback
    prior_high = float(daily["High"].iloc[-lb - 1:-1].max())
    prior_low = float(daily["Low"].iloc[-lb - 1:-1].min())
    if price > prior_high:
        patterns.append({
            "name": f"{lb}-day breakout",
            "detail": f"close {_p(price)} > prior {lb}d high {_p(prior_high)}"
                      + (f" on {vol_mult:.1f}x volume" if vol_mult else ""),
            "bias": "bullish", "strength": 3 if vol_mult >= th.vol_mult else 2,
        })
    elif price < prior_low:
        patterns.append({
            "name": f"{lb}-day breakdown",
            "detail": f"close {_p(price)} < prior {lb}d low {_p(prior_low)}"
                      + (f" on {vol_mult:.1f}x volume" if vol_mult else ""),
            "bias": "bearish", "strength": 3 if vol_mult >= th.vol_mult else 2,
        })

    # 2. Gap up / down vs prior close
    if len(daily) >= 2:
        prev_close = float(close.iloc[-2])
        today_open = float(daily["Open"].iloc[-1])
        gap = (today_open - prev_close) / prev_close * 100 if prev_close else 0.0
        if abs(gap) >= th.gap_pct:
            patterns.append({
                "name": f"gap {'up' if gap > 0 else 'down'}",
                "detail": f"{gap:+.1f}% open gap ({_p(prev_close)} -> {_p(today_open)})",
                "bias": "bullish" if gap > 0 else "bearish", "strength": 2,
            })

    # 3. MA(20/50) cross
    if len(close) >= 55:
        ma20, ma50 = close.rolling(20).mean(), close.rolling(50).mean()
        # `ma20 > ma50` is False (not NaN) when either side is NaN, so a blank bar reads as a
        # flip and reports a cross "0 days ago" between two NaN averages. Compare only where
        # both averages exist.
        both = ma20.notna() & ma50.notna()
        above = (ma20 > ma50)[both]
        age = _last_cross_age(above)
        if age is not None and age <= 3 and math.isfinite(ma20.iloc[-1]) \
                and math.isfinite(ma50.iloc[-1]):
            patterns.append({
                "name": f"MA20/50 {'golden' if above.iloc[-1] else 'death'} cross",
                "detail": f"{age} day(s) ago; MA20 {_p(ma20.iloc[-1])} vs MA50 {_p(ma50.iloc[-1])}",
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
    if hi52 and (hi52 - price) / hi52 <= th.prox_52w:
        patterns.append({
            "name": "near 52-week high",
            "detail": f"{_p(price)} within {(hi52-price)/hi52*100:.1f}% of 52w high {_p(hi52)}",
            "bias": "bullish", "strength": 1,
        })
    elif lo52 and (price - lo52) / lo52 <= th.prox_52w:
        patterns.append({
            "name": "near 52-week low",
            "detail": f"{_p(price)} within {(price-lo52)/lo52*100:.1f}% of 52w low {_p(lo52)}",
            "bias": "bearish", "strength": 1,
        })

    # 7. Support/resistance pivot proximity
    for kind, lvl in _pivot_levels(daily):
        if lvl and abs(price - lvl) / lvl < 0.01:
            patterns.append({
                "name": f"testing {'resistance' if kind == 'R' else 'support'}",
                "detail": f"price {_p(price)} at {'resistance' if kind == 'R' else 'support'} {_p(lvl)}",
                "bias": "bearish" if kind == "R" else "bullish", "strength": 1,
            })
            break

    return patterns


def _num(v, digits=2):
    """Round to a plain float, or None when the value isn't a real number.

    A NaN reaches here whenever yfinance serves a halted symbol or a gappy session, and
    round(nan) is still nan. Letting that through puts "nan" in the model's packet and, via
    indicators.price, into the portfolio's mark-to-market. None is the honest answer.

    The rounding is magnitude-aware because the meme sector trades five and six decimals below a
    cent: a flat `round(f, 2)` handed the model an indicator block of pure zeros for BONK and PEPE —
    price 0.0, ma20 0.0, ATR 0.0 — and the screener then made confident chart claims out of
    arithmetic on nothing. Nothing at or above $1 changes. See config.round_price.
    """
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return config.round_price(f, digits) if math.isfinite(f) else None


def _p(x):
    """A price inside a human-readable pattern detail. See config.format_price.

    Every one of these strings used to be `:.2f`, which on a sub-cent token produced details like
    "close 0.00 < prior 20d low 0.00" — the detail is the only evidence the email carries for a
    rules-only signal, so a row of zeros there is not a cosmetic problem.
    """
    return config.format_price(x)


def _signed(x):
    """Same, for a MACD histogram, which is meaningful only with its sign."""
    try:
        f = float(x)
    except (TypeError, ValueError):
        return "—"
    return f"{'+' if f >= 0 else '-'}{config.format_price(abs(f))}"


def indicator_snapshot(daily, intraday):
    """Named indicator values for the email + deep-run packet."""
    snap = {}
    if daily is not None and len(daily) >= 30:
        close = daily["Close"]
        snap["price"] = _num(close.iloc[-1])
        snap["rsi14_d"] = _num(rsi(close).iloc[-1], 1)
        line, sigl, hist = macd(close)
        snap["macd_d"] = f"{'bullish' if line.iloc[-1] > sigl.iloc[-1] else 'bearish'}, hist {_signed(hist.iloc[-1])}"
        snap["ma20"] = _num(close.rolling(20).mean().iloc[-1])
        snap["ma50"] = _num(close.rolling(50).mean().iloc[-1]) if len(close) >= 50 else None
        snap["atr14"] = _num(atr(daily).iloc[-1])
        vol = daily["Volume"]
        avg = float(vol.tail(20).mean()) if vol.tail(20).sum() else 0.0
        snap["vol_vs_avg"] = _num(float(vol.iloc[-1]) / avg) if avg else None
    if intraday is not None and not intraday.empty:
        vwap = _num(session_vwap(intraday))
        snap["vwap_intraday"] = vwap
        px = _num(intraday["Close"].iloc[-1])
        if px is not None and vwap:
            snap["price_vs_vwap"] = "above" if px > vwap else "below"
        snap["rsi14_intraday"] = _num(rsi(intraday["Close"]).iloc[-1], 1)
    return snap


# ── Finnhub rate limit ──────────────────────────────────────────────────────────
#
# One sliding-minute window shared by every Finnhub caller in the process. Both the news scrape
# and the fundamentals pull run six threads wide, so without this the first two seconds of an
# anchor cycle can spend the whole minute's allowance and every later call in that cycle comes
# back 429 — including the earnings calendar the LONG_TERM theses depend on.

_finnhub_calls = []
_finnhub_rate_lock = threading.Lock()


def _finnhub_throttle():
    """Block until one more Finnhub call fits under the per-minute ceiling."""
    while True:
        with _finnhub_rate_lock:
            now = time.monotonic()
            while _finnhub_calls and now - _finnhub_calls[0] > 60:
                _finnhub_calls.pop(0)
            if len(_finnhub_calls) < config.FINNHUB_MAX_PER_MIN:
                _finnhub_calls.append(now)
                return
            wait = 60 - (now - _finnhub_calls[0]) + 0.05
        time.sleep(min(max(wait, 0.05), 5))


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
    _finnhub_throttle()
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
            # `summary` is the lede Finnhub already paid for and the old code threw away — it is
            # what lets the news pass tell a real disclosure from a headline that only sounds like one.
            summary = (n.get("summary") or "").strip()
            out.append({
                "title": (n.get("headline") or "").strip(),
                "source": n.get("source") or "Finnhub",
                "published": datetime.fromtimestamp(n.get("datetime", 0), config.UTC).isoformat(),
                "link": n.get("url") or "",
                "summary": summary[:400],
            })
        return out
    except Exception as e:  # pragma: no cover - network
        print(f"[warn] finnhub_news {ticker}: {e}", file=sys.stderr)
        return []


# ── SEC EDGAR (free, no key, and nobody has read it yet) ────────────────────────
# The one genuinely under-covered source available: an 8-K is public the minute it is filed and
# the aggregators rewrite it hours later. No key, no quota — SEC asks only for a declared
# contact in the User-Agent and ≤10 requests/second, both of which we honour.

_SEC_UA = {"User-Agent": f"MarketBrain/1.0 ({config.SEC_CONTACT})",
           "Accept-Encoding": "gzip, deflate", "Host": "www.sec.gov"}
_SEC_DATA_UA = dict(_SEC_UA, Host="data.sec.gov")
_SEC_MAP_PATH = config.MEMORY_DIR / "SEC-CIK-MAP.json"
_SEC_MAP_TTL_DAYS = 30
_sec_map_cache = None

# The 8-K items that actually move a stock. An 8-K citing only 9.01 (exhibits) is bookkeeping.
_EIGHT_K_ITEMS = {
    "1.01": "material agreement", "1.02": "agreement terminated", "1.03": "bankruptcy",
    "2.01": "acquisition/disposition", "2.02": "results of operations", "2.03": "new obligation",
    "2.04": "acceleration of obligation", "2.05": "exit/restructuring costs",
    "2.06": "material impairment", "3.01": "delisting notice", "3.02": "unregistered equity sale",
    "4.01": "auditor change", "4.02": "prior statements not reliable",
    "5.01": "change in control", "5.02": "executive/director change", "5.03": "bylaw amendment",
    "7.01": "regulation FD disclosure", "8.01": "other events",
}


def sec_ticker_map(force=False):
    """{TICKER: 10-digit CIK}. Cached on disk for a month — the file changes slowly."""
    global _sec_map_cache
    if _sec_map_cache is not None and not force:
        return _sec_map_cache
    if not force:
        try:
            stat = _SEC_MAP_PATH.stat()
            fresh = (time.time() - stat.st_mtime) < _SEC_MAP_TTL_DAYS * 86400
            if fresh:
                with open(_SEC_MAP_PATH, "r", encoding="utf-8") as fh:
                    _sec_map_cache = json.load(fh)
                return _sec_map_cache
        except (OSError, ValueError):
            pass
    try:
        r = requests.get("https://www.sec.gov/files/company_tickers.json",
                         headers=_SEC_UA, timeout=_HTTP_TIMEOUT)
        r.raise_for_status()
        rows = r.json() or {}
        mapping = {}
        for row in rows.values():
            tk = (row.get("ticker") or "").upper()
            cik = row.get("cik_str")
            if tk and cik is not None:
                mapping[tk] = str(cik).zfill(10)
        if mapping:
            _SEC_MAP_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(_SEC_MAP_PATH, "w", encoding="utf-8") as fh:
                json.dump(mapping, fh)
            _sec_map_cache = mapping
        return _sec_map_cache or {}
    except Exception as e:  # pragma: no cover - network
        print(f"[warn] sec ticker map: {e}", file=sys.stderr)
        return _sec_map_cache or {}


def sec_filings(ticker, days=5, limit=4, forms=None):
    """Recent EDGAR filings for one name, shaped like a headline so it merges with the news feed."""
    forms = set(forms or config.NEWS_SEC_FORMS)
    cik = (sec_ticker_map() or {}).get(ticker.upper())
    if not cik:
        return []
    cutoff = (datetime.now(config.UTC).date() - timedelta(days=days)).isoformat()
    try:
        r = requests.get(f"https://data.sec.gov/submissions/CIK{cik}.json",
                         headers=_SEC_DATA_UA, timeout=_HTTP_TIMEOUT)
        r.raise_for_status()
        data = r.json() or {}
    except Exception as e:  # pragma: no cover - network
        print(f"[warn] sec filings {ticker}: {e}", file=sys.stderr)
        return []
    recent = ((data.get("filings") or {}).get("recent") or {})
    company = data.get("name") or ticker.upper()
    out = []
    n_rows = len(recent.get("form") or [])
    for i in range(n_rows):
        form = (recent["form"][i] or "").strip()
        filed = (recent.get("filingDate") or [""] * n_rows)[i] or ""
        if filed < cutoff:
            break                      # EDGAR returns newest-first, so the window is done
        if form not in forms:
            continue
        items_raw = (recent.get("items") or [""] * n_rows)[i] or ""
        codes = [c.strip() for c in items_raw.split(",") if c.strip()]
        named = [f"item {c} {_EIGHT_K_ITEMS[c]}" for c in codes if c in _EIGHT_K_ITEMS]
        if form == "8-K" and codes and not named:
            continue                   # exhibits-only 8-K: a filing, not news
        detail = f" — {'; '.join(named)}" if named else ""
        acc = (recent.get("accessionNumber") or [""] * n_rows)[i] or ""
        doc = (recent.get("primaryDocument") or [""] * n_rows)[i] or ""
        link = (f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc.replace('-', '')}/{doc}"
                if acc and doc else f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}")
        out.append({
            "title": f"{company} filed {form}{detail}",
            "source": "SEC EDGAR",
            "published": filed,
            "link": link,
            "summary": (recent.get("primaryDocDescription") or [""] * n_rows)[i] or "",
            "form": form,
        })
        if len(out) >= limit:
            break
    return out


# ── Fundamentals (Finnhub) ──────────────────────────────────────────────────────

# Finnhub's free tier allows 60 calls/minute, and the brain runs 14 names an anchor. Nothing here
# changes hour to hour, so every endpoint is cached on disk with a TTL matched to how fast the
# underlying fact can actually move: a balance sheet is a day old at worst, an earnings print
# hours. The cache is what makes the wider pull affordable rather than a rate-limit problem.
_FUND_CACHE_PATH = config.MEMORY_DIR / "FUNDAMENTALS-CACHE.json"
_FUND_TTL = {
    "metric": 24 * 3600,          # ratios move on filings, not on ticks
    "earnings": 6 * 3600,         # the last print does not change; the next one lands overnight
    "recommendation": 24 * 3600,  # analysts revise monthly
    "insider": 12 * 3600,         # Form 4 has a 2-day filing deadline
    "calendar": 6 * 3600,         # one bulk pull serves every ticker
}
_fund_lock = threading.Lock()
_fund_cache = None


def _fund_cache_load():
    global _fund_cache
    if _fund_cache is None:
        try:
            with open(_FUND_CACHE_PATH, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            _fund_cache = data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            _fund_cache = {}
    return _fund_cache


def fundamentals_cache_save():
    """Write the cache once per run — the collectors themselves only mutate it in memory."""
    with _fund_lock:
        if _fund_cache is None:
            return
        try:
            _FUND_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp = _FUND_CACHE_PATH.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(_fund_cache, fh)
            os.replace(tmp, _FUND_CACHE_PATH)
        except Exception as e:  # pragma: no cover - disk
            print(f"[warn] fundamentals cache save: {e}", file=sys.stderr)


def _finnhub_json(kind, cache_key, path, params, force=False):
    """One cached Finnhub GET. Returns the parsed body, or None when unavailable."""
    key = f"{kind}:{cache_key}"
    now = time.time()
    with _fund_lock:
        cache = _fund_cache_load()
        hit = cache.get(key)
        if hit and not force and (now - hit.get("at", 0)) < _FUND_TTL.get(kind, 3600):
            return hit.get("data")
    if not config.FINNHUB_API_KEY:
        return None
    _finnhub_throttle()
    try:
        r = requests.get(f"https://finnhub.io/api/v1{path}",
                         params={**params, "token": config.FINNHUB_API_KEY},
                         headers=_UA, timeout=_HTTP_TIMEOUT)
        if r.status_code in (401, 403):
            # A premium-only endpoint on a free key. Cache the refusal so we stop asking.
            with _fund_lock:
                _fund_cache_load()[key] = {"at": now, "data": None}
            return None
        r.raise_for_status()
        data = r.json()
    except Exception as e:  # pragma: no cover - network
        print(f"[warn] finnhub {kind} {cache_key}: {e}", file=sys.stderr)
        return None
    with _fund_lock:
        _fund_cache_load()[key] = {"at": now, "data": data}
    return data


# The `stock/metric?metric=all` response carries 100+ fields and the old code kept five of them,
# which is the whole reason a LONG_TERM thesis had nothing to stand on. These are the ones that
# actually bear on whether a company can keep earning — and they cost no extra call.
_METRIC_MAP = {
    "pe_ttm": "peTTM",
    "ps_ttm": "psTTM",
    "pb": "pbQuarterly",
    "revenue_per_share_ttm": "revenuePerShareTTM",
    "gross_margin": "grossMarginTTM",
    "operating_margin": "operatingMarginTTM",
    "profit_margin": "netProfitMarginTTM",
    "roe": "roeTTM",
    "roa": "roaTTM",
    "debt_to_equity": "totalDebt/totalEquityQuarterly",
    "current_ratio": "currentRatioQuarterly",
    "quick_ratio": "quickRatioQuarterly",
    "revenue_growth_yoy": "revenueGrowthTTMYoy",
    "eps_growth_yoy": "epsGrowthTTMYoy",
    "revenue_growth_5y": "revenueGrowth5Y",
    "beta": "beta",
    "dividend_yield": "dividendYieldIndicatedAnnual",
    "52w_high": "52WeekHigh",
    "52w_low": "52WeekLow",
    "52w_return": "52WeekPriceReturnDaily",
    "avg_volume_10d": "10DayAverageTradingVolume",
}


def earnings_calendar_map(days=14):
    """{TICKER: {date, hour, eps_estimate}} for the focus list — one bulk call for every name."""
    frm = datetime.now(config.UTC).date()
    to = frm + timedelta(days=days)
    data = _finnhub_json("calendar", f"{frm}:{days}", "/calendar/earnings",
                         {"from": str(frm), "to": str(to)})
    rows = ((data or {}).get("earningsCalendar") or [])
    watch = set(config.FOCUS_TICKERS)
    out = {}
    for e in rows:
        tk = e.get("symbol")
        if tk not in watch or not e.get("date"):
            continue
        prev = out.get(tk)
        if prev is None or e["date"] < prev["date"]:      # keep the nearest print
            out[tk] = {"date": e.get("date"), "hour": e.get("hour"),
                       "eps_estimate": _num(e.get("epsEstimate"), 2)}
    return out


def finnhub_earnings_calendar(days=10):
    """Upcoming focus-list earnings as a flat list, nearest first."""
    rows = [dict(v, ticker=t) for t, v in earnings_calendar_map(days).items()]
    return sorted(rows, key=lambda e: e["date"])


def _summarize_insiders(rows, days=90):
    """Net insider buying/selling from Form 4 data — open-market trades only.

    Grants (code A) and option exercises (M) are compensation, not conviction, so counting them
    would show "insider buying" every vesting date. Only P (purchase) and S (sale) mean anything.
    """
    cutoff = (datetime.now(config.UTC).date() - timedelta(days=days)).isoformat()
    bought = sold = 0
    buyers, sellers = set(), set()
    for r in rows or []:
        when = (r.get("transactionDate") or r.get("filingDate") or "")
        if when < cutoff:
            continue
        code = (r.get("transactionCode") or "").upper()
        share = abs(float(r.get("change") or 0))
        if code == "P":
            bought += share
            buyers.add(r.get("name"))
        elif code == "S":
            sold += share
            sellers.add(r.get("name"))
    if not (bought or sold):
        return None
    net = bought - sold
    return {
        "window_days": days,
        "shares_bought": int(bought), "shares_sold": int(sold),
        "net_shares": int(net),
        "buyers": len(buyers), "sellers": len(sellers),
        "read": "net buying" if net > 0 else ("net selling" if net < 0 else "flat"),
    }


def _summarize_recommendations(rows):
    """Latest analyst spread plus the one-month change — the revision is the signal."""
    if not rows:
        return None
    rows = sorted(rows, key=lambda r: r.get("period") or "", reverse=True)
    cur = rows[0]

    def _score(r):
        return ((r.get("strongBuy") or 0) * 2 + (r.get("buy") or 0)
                - (r.get("sell") or 0) - (r.get("strongSell") or 0) * 2)

    out = {"period": cur.get("period"),
           "strong_buy": cur.get("strongBuy"), "buy": cur.get("buy"),
           "hold": cur.get("hold"), "sell": cur.get("sell"),
           "strong_sell": cur.get("strongSell"),
           "score": _score(cur)}
    if len(rows) > 1:
        out["score_change_1m"] = _score(cur) - _score(rows[1])
        out["prior_period"] = rows[1].get("period")
    return out


def finnhub_fundamentals(ticker, with_insiders=True, with_analysts=True):
    """Company facts that move a stock: earnings, ratios, growth, analysts, insiders.

    Every field runs through `_num()`, so a NaN or an unparseable string from the API becomes
    None here rather than poisoning `json.dumps` on the way into the model packet.
    """
    out = {}
    earn = _finnhub_json("earnings", ticker, "/stock/earnings", {"symbol": ticker, "limit": 4})
    if earn:
        last = earn[0]
        out["last_eps_actual"] = _num(last.get("actual"), 2)
        out["last_eps_estimate"] = _num(last.get("estimate"), 2)
        out["last_eps_surprise_pct"] = _num(last.get("surprisePercent"), 1)
        out["last_eps_period"] = last.get("period")
        beats = [e for e in earn if (e.get("surprisePercent") or 0) > 0]
        out["eps_beats_last_4"] = f"{len(beats)}/{len(earn)}"

    metric = _finnhub_json("metric", ticker, "/stock/metric",
                           {"symbol": ticker, "metric": "all"})
    m = (metric or {}).get("metric") or {}
    for name, key in _METRIC_MAP.items():
        val = _num(m.get(key), 3)
        if val is not None:
            out[name] = val
    out["revenue_ttm"] = out.get("revenue_per_share_ttm")   # back-compat with the old field name

    nxt = earnings_calendar_map().get(ticker.upper())
    if nxt:
        out["next_earnings_date"] = nxt["date"]
        out["next_earnings_hour"] = nxt.get("hour")
        out["next_eps_estimate"] = nxt.get("eps_estimate")
        try:
            d = datetime.strptime(nxt["date"], "%Y-%m-%d").date()
            out["days_to_earnings"] = (d - datetime.now(config.UTC).date()).days
        except (TypeError, ValueError):
            pass

    if with_analysts:
        rec = _summarize_recommendations(
            _finnhub_json("recommendation", ticker, "/stock/recommendation", {"symbol": ticker}))
        if rec:
            out["analysts"] = rec
    if with_insiders:
        frm = (datetime.now(config.UTC).date() - timedelta(days=90)).isoformat()
        data = _finnhub_json("insider", ticker, "/stock/insider-transactions",
                             {"symbol": ticker, "from": frm,
                              "to": datetime.now(config.UTC).date().isoformat()})
        ins = _summarize_insiders((data or {}).get("data"))
        if ins:
            out["insiders"] = ins
    return out


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
    # imminent = a US high-impact print inside the screener horizon. Country matters here and not
    # in `events`: the keyword list matches titles, and "Spanish Unemployment Rate" and "BOJ Core
    # CPI y/y" both matched it while tagged Low impact. Both forced deep runs. They stay in the
    # packet — the analyst should see them — but they no longer buy a cycle.
    imminent = [e for e in events
                if e["hours_away"] is not None
                and -0.5 <= e["hours_away"] <= config.SCREEN_EVENT_HORIZON_H
                and (e["country"] or "").upper() in config.CALENDAR_ESCALATE_COUNTRIES]
    trump_soon = [s for s in speeches
                  if s["hours_away"] is not None and -2 <= s["hours_away"] <= 24
                  and "trump" in s["title"].lower()]
    return {"events": events, "speeches": speeches,
            "imminent": imminent, "trump_soon": trump_soon}


# ── Per-ticker + market assembly ────────────────────────────────────────────────

def collect_ticker(ticker, with_news=False, with_fundamentals=False):
    """Full snapshot for one ticker: bars-derived indicators, patterns, optional news/fundamentals."""
    # Intraday first: the daily cache needs today's intraday bars to rebuild today's daily bar.
    intraday = fetch_intraday(ticker) if not ticker.startswith("^") and "=" not in ticker else pd.DataFrame()
    daily = daily_bars(ticker, intraday=intraday)
    snap = {
        "ticker": ticker,
        "indicators": indicator_snapshot(daily, intraday if not intraday.empty else None),
        "patterns": detect_patterns(ticker, daily, intraday if not intraday.empty else None),
    }
    if with_news:
        if config.is_crypto(ticker):
            # Finnhub company-news is keyed on an equity symbol and returns nothing for a token,
            # so there is no fallback here — Google News RSS is the crypto track's only feed.
            snap["news"] = google_news(config.CRYPTO_NEWS_QUERIES.get(ticker)
                                       or f"{config.label_for(ticker)} crypto", limit=6)
        else:
            company = ticker
            snap["news"] = (finnhub_news(ticker) or google_news(f"{company} stock", limit=5))
    if with_fundamentals and not config.is_crypto(ticker):
        # A token has no earnings date, no margin, no insider Form 4 and no analyst spread. The
        # field is left *absent* rather than filled with zeros or nulls, because CLAUDE.md tells the
        # analyst "a missing field could not be computed, it is not a zero" — and a zeroed
        # fundamentals block would read as a company with no revenue and no growth, which is a
        # bearish fact about a stock rather than the truth, which is that the concept does not apply.
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


def collect_crypto_market(with_news=False, max_workers=4):
    """Snapshot the crypto universe plus its own market context.

    Deliberately a thin wrapper rather than a flag on collect_market: the two venues differ in what
    context they need, not just in which tickers they hold. BTC and ETH are the crypto tape's own
    regime indicators — SUI does not trade independently of them in a risk-off hour, so they appear
    twice, once as context and once as candidates. SPY/QQQ/ES/NQ are dropped because equity index
    futures say little about a Sunday-morning token move, while DXY and the VIX stay: crypto still
    reacts to the dollar and to risk appetite, and that cross-asset read is exactly what a
    token-only packet would miss.

    `with_fundamentals` is not a parameter at all. There is nothing to fetch.
    """
    tickers = list(dict.fromkeys(config.CRYPTO_MARKET_CONTEXT + config.CRYPTO_TICKERS))
    return collect_market(tickers, with_news=with_news, with_fundamentals=False,
                          max_workers=max_workers)
