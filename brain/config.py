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
# The free tier allows 60 calls/minute and the collectors run six threads wide, so a burst can
# breach the ceiling in a couple of seconds and get the key throttled for everything else in the
# cycle. Stay under it deliberately rather than discovering the limit through 429s.
FINNHUB_MAX_PER_MIN = int(os.environ.get("BRAIN_FINNHUB_RPM", "55"))

# ── Price bars (yfinance) ───────────────────────────────────────────────────────
# yfinance defaults to a 10s socket timeout but retries internally; a hung fetch used to be able
# to stall a whole cycle past its 30-minute slot. Explicit, and short enough that two attempts
# still finish inside the window.
YF_TIMEOUT = int(os.environ.get("BRAIN_YF_TIMEOUT", "20"))
# A 1-year daily frame is ~250 bars of which exactly one — today's — can still change. Cache the
# frame for the session and splice today's bar in from the intraday fetch we already make, and
# the daily pull drops from ~50 calls every 30 minutes to ~50 calls a day.
BARS_CACHE = os.environ.get("BRAIN_BARS_CACHE", "1") != "0"

# ── Obsidian second brain ───────────────────────────────────────────────────────
# Vault root. PC: D:\Obsidian\Lind Brain. VPS: its Syncthing copy. Empty -> no vault write.
LIND_BRAIN = os.environ.get("LIND_BRAIN", "")
BRAIN_VAULT_SUBFOLDER = "1000 - Stocks & Markets/Trading Brain"

# ── Git-as-memory (brain-memory/) ───────────────────────────────────────────────
REPO_ROOT = Path(__file__).resolve().parent.parent
MEMORY_DIR = Path(os.environ.get("BRAIN_MEMORY_DIR", REPO_ROOT / "brain-memory"))
BARS_CACHE_DIR = MEMORY_DIR / "bars"     # regenerable price mirrors, gitignored

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
# `medium` is included: at one entry per session the cost of missing a real setup outweighs the
# cost of an Opus run that concludes nothing. The saturation guard below still blocks the case
# this was really protecting against — a loud story every outlet has already run.
NEWS_ESCALATE_MATERIALITY = tuple(
    s.strip() for s in os.environ.get("BRAIN_NEWS_ESCALATE_MATERIALITY", "high,medium").split(",")
    if s.strip())
NEWS_ESCALATE_ABS_SENTIMENT = int(os.environ.get("BRAIN_NEWS_ESCALATE_SENTIMENT", "45"))

# ── News quality gate (brain/news_quality.py) ───────────────────────────────────
# Scrape wide, then keep only the least-saturated few. Candidates cost nothing extra at
# Finnhub (one call returns the whole window regardless of `limit`) and Google News RSS is
# unkeyed, so the budget spent here is latency, not quota.
NEWS_CANDIDATES_PER_TICKER = int(os.environ.get("BRAIN_NEWS_CANDIDATES", "14"))
NEWS_MIN_NOVELTY = int(os.environ.get("BRAIN_NEWS_MIN_NOVELTY", "30"))  # 0-100, below = recycled
NEWS_SEC_FILINGS = os.environ.get("BRAIN_NEWS_SEC", "1") != "0"        # EDGAR is free and primary
NEWS_SEC_FORMS = ("8-K", "10-Q", "10-K", "SC 13D", "SC 13D/A", "425", "S-4")
# SEC requires a declared contact in the User-Agent; it is the operator's own address.
SEC_CONTACT = os.environ.get("BRAIN_SEC_CONTACT", RECIPIENT)

# ── Paper broker (Alpaca PAPER endpoint only) ───────────────────────────────────
# Hardcoded on purpose: paper-api.alpaca.markets is a different host from the live
# api.alpaca.markets, so no env var, config edit, or model output can retarget this at a
# real-money account. broker.py refuses to send anything if this host ever stops matching.
ALPACA_PAPER_BASE = "https://paper-api.alpaca.markets"
ALPACA_KEY_ID = os.environ.get("ALPACA_PAPER_KEY_ID", "")
ALPACA_SECRET = os.environ.get("ALPACA_PAPER_SECRET_KEY", "")

# ── Paper portfolio (PDF Part 2 discipline rules) ───────────────────────────────
STARTING_CASH = float(os.environ.get("BRAIN_START_CASH", "10000"))
# Sized for roughly one entry per session. The old 3/week cap was the binding constraint on a
# fast tape: it was spent by Tuesday and every Wednesday–Friday setup was rejected before it was
# even judged. The cap now sits ABOVE the pace target so it stops runaway churn without ever
# being the reason a good Thursday trade is refused.
MAX_POSITIONS = int(os.environ.get("BRAIN_MAX_POSITIONS", "8"))
MAX_POSITION_PCT = float(os.environ.get("BRAIN_MAX_POSITION_PCT", "15"))   # ≤15% of equity per name
MAX_NEW_TRADES_PER_WEEK = int(os.environ.get("BRAIN_MAX_NEW_TRADES", "6"))
DEFAULT_POSITION_PCT = float(os.environ.get("BRAIN_DEFAULT_POSITION_PCT", "12"))
# The pace the analyst is measured against — shown in its packet, never enforced. A floor cannot
# be enforced without buying the least-bad thing on a dead tape, which is how an account bleeds.
# This makes the target visible so a quiet week is a deliberate call rather than an oversight.
WEEKLY_TRADE_TARGET = int(os.environ.get("BRAIN_WEEKLY_TRADE_TARGET", "5"))
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
# Loosened for a one-entry-per-session pace. These decide when Opus is allowed to *look*, not
# what it may trade — a wider net costs deep runs that conclude "nothing here", which is the
# cheap failure. The expensive failure is a setup that never reached the analyst at all.
SCREEN_GAP_PCT = float(os.environ.get("BRAIN_SCREEN_GAP_PCT", "1.0"))    # |open gap| ≥ this ⇒ flag
SCREEN_VOL_MULT = float(os.environ.get("BRAIN_SCREEN_VOL_MULT", "1.5"))  # ≥ this × 20-bar avg vol
SCREEN_BREAKOUT_LOOKBACK = int(os.environ.get("BRAIN_SCREEN_BREAKOUT", "15"))  # N-bar high/low
SCREEN_RSI_HOT = int(os.environ.get("BRAIN_SCREEN_RSI_HOT", "70"))
SCREEN_RSI_COLD = int(os.environ.get("BRAIN_SCREEN_RSI_COLD", "30"))
SCREEN_EVENT_HORIZON_H = 3.0   # high-impact calendar event within this many hours ⇒ flag
SCREEN_52W_PROXIMITY = 0.03    # within 3% of the 52-week high/low ⇒ flag

# ── Signal de-duplication (brain/dedupe.py) ─────────────────────────────────────
# The screener fires on a *condition*, and a condition persists. A 20-day breakout is still a
# 20-day breakout thirty minutes later, so without a memory of what was already sent the same
# idea gets re-proposed and re-emailed every cycle it stays true.
#
# Cooldowns are per trade type because "still the same idea" means different things at different
# horizons: an intraday setup genuinely does change within hours, a multi-quarter thesis does not.
SIGNAL_COOLDOWN_H = {
    "SCALP": float(os.environ.get("BRAIN_COOLDOWN_SCALP_H", "3")),
    "SHORT_TERM": float(os.environ.get("BRAIN_COOLDOWN_SWING_H", "24")),
    "LONG_TERM": float(os.environ.get("BRAIN_COOLDOWN_LONG_H", "120")),
}
# What counts as a genuinely different idea rather than the same one repeated: the entry moved
# by more than this, or conviction moved by more than this. Either re-opens the cooldown.
SIGNAL_ENTRY_DRIFT_PCT = float(os.environ.get("BRAIN_SIGNAL_DRIFT_PCT", "1.5"))
SIGNAL_CONFIDENCE_JUMP = int(os.environ.get("BRAIN_SIGNAL_CONF_JUMP", "15"))

# ── Long-horizon thesis board (brain/thesis.py) ─────────────────────────────────
# Structural bets live on a different clock from trades: a demand shift like the 2023-24 DRAM
# squeeze plays out over quarters, so it has to be *carried* between runs rather than
# rediscovered. The board is reviewed on the Monday digest and the Friday recap only.
THESIS_MAX_ACTIVE = int(os.environ.get("BRAIN_THESIS_MAX", "8"))
THESIS_HORIZON_YEARS = (1, 3)
THESIS_MIN_CONVICTION = int(os.environ.get("BRAIN_THESIS_MIN_CONVICTION", "55"))
# A thesis with no supporting evidence in this many days is stale — it gets flagged for review
# rather than quietly kept, because an unfalsified thesis nobody revisits is just a bias.
THESIS_STALE_DAYS = int(os.environ.get("BRAIN_THESIS_STALE_DAYS", "45"))

DISCLAIMER = (
    "Educational paper-trading analysis from delayed data. Simulated portfolio, mirrored to an "
    "Alpaca PAPER account — no real-money orders are placed. Not financial advice."
)
