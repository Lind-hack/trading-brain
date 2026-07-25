"""Shared configuration for the Market Brain.

All secrets come from environment variables (loaded from /root/.secrets/signal.env on the
VPS, or the user's shell locally). Nothing here is a hard dependency: every consumer
degrades gracefully when a key is absent, so the brain still runs on free data alone.
"""
import os
import sys
from datetime import timezone
from pathlib import Path
from zoneinfo import ZoneInfo

# Windows consoles default to cp1252 and choke on emoji/em dashes in prints.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ET = ZoneInfo("America/New_York")
UTC = timezone.utc

# ── Identity / delivery ─────────────────────────────────────────────────────────
RECIPIENT = os.environ.get("BRAIN_RECIPIENT", "lindsylqa@gmail.com")
GMAIL_USER = os.environ.get("GMAIL_USER", "")
GMAIL_PASS = os.environ.get("GMAIL_APP_PASSWORD", "").replace(" ", "")

# ── Dashboard feed (Supabase REST) — same project as the stock engine ───────────
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_ANON = os.environ.get("SUPABASE_ANON_KEY", "")
SIGNAL_INGEST_KEY = os.environ.get("SIGNAL_INGEST_KEY", "")

# ── Fundamentals / news API ─────────────────────────────────────────────────────
FINNHUB_API_KEY = os.environ.get("FINNHUB_API_KEY", "")

# ── Obsidian second brain ───────────────────────────────────────────────────────
# Vault root. PC: D:\Obsidian\Lind Brain. VPS: its Syncthing copy. Empty -> no vault write.
LIND_BRAIN = os.environ.get("LIND_BRAIN", "")
BRAIN_VAULT_SUBFOLDER = "1000 - Stocks & Markets/Trading Brain"

# ── Git-as-memory (brain-memory/) ───────────────────────────────────────────────
REPO_ROOT = Path(__file__).resolve().parent.parent
MEMORY_DIR = Path(os.environ.get("BRAIN_MEMORY_DIR", REPO_ROOT / "brain-memory"))

# ── Claude Code subscription (no Anthropic API key — region-ineligible) ─────────
# Deep analysis runs via the `claude` CLI authenticated with Lind's subscription OAuth
# (`claude setup-token`). Model names map the hybrid tiers to CLI --model flags.
# Two-tier pipeline: Haiku 4.5 reads the news every cycle (cheap, high volume), Opus 5 makes
# the trade decision only when the screener or Haiku says something material happened.
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")
CLAUDE_DEEP_MODEL = os.environ.get("BRAIN_DEEP_MODEL", "claude-opus-5")
CLAUDE_LITE_MODEL = os.environ.get("BRAIN_LITE_MODEL", "claude-haiku-4-5-20251001")
CLAUDE_TIMEOUT = int(os.environ.get("BRAIN_CLAUDE_TIMEOUT", "300"))   # seconds per deep run
CLAUDE_LITE_TIMEOUT = int(os.environ.get("BRAIN_LITE_TIMEOUT", "150"))  # seconds per news pass

# ── News intelligence layer (Haiku 4.5, every cycle) ────────────────────────────
NEWS_TICKERS_PER_CYCLE = int(os.environ.get("BRAIN_NEWS_TICKERS", "14"))
NEWS_HEADLINES_PER_TICKER = 5
# Haiku materiality/sentiment thresholds that escalate a cycle to an Opus deep run.
NEWS_ESCALATE_MATERIALITY = ("high",)
NEWS_ESCALATE_ABS_SENTIMENT = 55

# ── Paper broker (Alpaca PAPER endpoint only) ───────────────────────────────────
# Hardcoded on purpose: paper-api.alpaca.markets is a different host from the live
# api.alpaca.markets, so no env var, config edit, or model output can retarget this at a
# real-money account. broker.py refuses to send anything if this host ever stops matching.
ALPACA_PAPER_BASE = "https://paper-api.alpaca.markets"
ALPACA_KEY_ID = os.environ.get("ALPACA_PAPER_KEY_ID", "")
ALPACA_SECRET = os.environ.get("ALPACA_PAPER_SECRET_KEY", "")

# ── Paper portfolio (PDF Part 2 discipline rules) ───────────────────────────────
STARTING_CASH = float(os.environ.get("BRAIN_START_CASH", "10000"))
MAX_POSITIONS = 6
MAX_POSITION_PCT = 20.0     # ≤20% of equity in any one name
MAX_NEW_TRADES_PER_WEEK = 3
HARD_STOP_PCT = -7.0        # cut a loser at -7%
TRAIL_BASE_PCT = 10.0       # initial trailing stop
TRAIL_TIGHT_15 = 7.0        # tighten to 7% once +15%
TRAIL_TIGHT_20 = 5.0        # tighten to 5% once +20%
SECTOR_FAIL_LIMIT = 2       # stop out of a sector after 2 consecutive losers in it

# ── Focus list — liquid, news-active names the brain watches every cycle ────────
# Deliberately a curated subset (not the full 194-ticker BingX scan) so free-data pulls
# stay fast and the deep-run packet stays legible. Market context tickers lead.
MARKET_CONTEXT = ["SPY", "QQQ", "^VIX", "ES=F", "NQ=F", "DX-Y.NYB", "^TNX"]
FOCUS_TICKERS = [
    "AAPL", "MSFT", "NVDA", "AMD", "TSLA", "AMZN", "META", "GOOGL", "NFLX",
    "AVGO", "TSM", "MU", "SMCI", "PLTR", "COIN", "HOOD", "MSTR", "MARA",
    "JPM", "GS", "BAC", "XOM", "CVX", "LLY", "UNH", "CRM", "ORCL", "ADBE",
    "SOFI", "SNOW", "PANW", "CEG", "GE", "UBER", "DIS", "BA", "CAT",
]

# ── ForexFactory official weekly feed (JSON, no scraping) ───────────────────────
FF_FEED_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
# faireconomy retired the nextweek/lastweek feeds (404 as of Jul 2026) — thisweek is the only
# live one. Harmless: its weeks start Sunday, so the Sunday digest already sees the week ahead.
FF_NEXTWEEK_URL = None

# High-impact USD events + speech titles that move the whole tape.
FF_WATCH_KEYWORDS = [
    "CPI", "Core CPI", "PPI", "Non-Farm", "Nonfarm", "Unemployment Rate",
    "FOMC", "Federal Funds", "Fed Chair", "PCE", "GDP", "Retail Sales",
    "ISM", "JOLTS", "Trump", "Powell", "Speaks",
]
SPEECH_KEYWORDS = ["Speaks", "Speech", "Testimony", "Press Conference"]

# ── Screener thresholds (deterministic; no LLM) ─────────────────────────────────
SCREEN_GAP_PCT = 1.5            # |open gap| ≥ this ⇒ flag
SCREEN_VOL_MULT = 1.8          # volume ≥ this × 20-bar avg ⇒ flag
SCREEN_BREAKOUT_LOOKBACK = 20  # N-bar high/low breakout window
SCREEN_RSI_HOT = 72
SCREEN_RSI_COLD = 28
SCREEN_EVENT_HORIZON_H = 2.0   # high-impact calendar event within this many hours ⇒ flag
SCREEN_52W_PROXIMITY = 0.02    # within 2% of the 52-week high/low ⇒ flag

DISCLAIMER = (
    "Educational paper-trading analysis from delayed data. Simulated portfolio, mirrored to an "
    "Alpaca PAPER account — no real-money orders are placed. Not financial advice."
)
