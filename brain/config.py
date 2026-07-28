"""Shared configuration for the Market Brain.

All secrets come from environment variables (loaded from /root/.secrets/signal.env on the
VPS, or the user's shell locally). Nothing here is a hard dependency: every consumer
degrades gracefully when a key is absent, so the brain still runs on free data alone.
"""
import os
import sys
from datetime import datetime, timezone
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
# 14 was sized for a 6-position book and a tight screener that flagged 2-4 names a cycle. Raising
# MAX_POSITIONS to 8 and loosening the screener (a live run now flags ~18) broke that arithmetic:
# held + triggered filled every place, and the focus-list tail — the only group that can surface a
# name the chart is silent about — got nothing. 20 restores it. The ceiling is not the API (20
# Finnhub calls a cycle against a 55/min budget); the cost is Haiku tokens on every cycle.
NEWS_TICKERS_PER_CYCLE = int(os.environ.get("BRAIN_NEWS_TICKERS", "20"))
# Places held back for names with no position and no screener trigger, so a busy tape can never
# make news-first discovery impossible. A floor, not a quota — see market_brain._news_watchlist.
NEWS_DISCOVERY_RESERVE = int(os.environ.get("BRAIN_NEWS_DISCOVERY_RESERVE", "4"))
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

# ── Scalp focus window ──────────────────────────────────────────────────────────
# A dated tilt toward intraday ideas, not a permanent one. Set BRAIN_SCALP_FOCUS_UNTIL to an ET
# date (YYYY-MM-DD, inclusive) and every prompt built up to the end of that day asks the analyst
# to look at intraday setups first. It expires by itself: a standing scalp bias would quietly turn
# a book meant to hold multi-week positions into a day-trading account, and nobody would notice
# the drift because each individual run would look reasonable.
SCALP_FOCUS_UNTIL = os.environ.get("BRAIN_SCALP_FOCUS_UNTIL", "").strip()


def scalp_focus_active(now_et=None):
    """Is the dated scalp tilt in force right now? Unparseable dates are treated as off."""
    if not SCALP_FOCUS_UNTIL:
        return False
    try:
        until = datetime.strptime(SCALP_FOCUS_UNTIL, "%Y-%m-%d").date()
    except ValueError:
        print(f"[warn] BRAIN_SCALP_FOCUS_UNTIL={SCALP_FOCUS_UNTIL!r} is not YYYY-MM-DD — ignoring",
              file=sys.stderr)
        return False
    return (now_et or datetime.now(ET)).date() <= until

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

# ── Scalps: the horizon that is enforced, not just labelled ─────────────────────
# `trade_type: "SCALP"` already existed, but it was only ever a *label*. Nothing closed a scalp;
# it inherited the swing book's −7% stop and 10% trail and then sat there for a week, and the
# only thing that noticed was journal.py grading the mislabel long after the money was tied up.
# A horizon that nothing enforces is a horizon the model can claim and then quietly not honour.
#
# So a scalp now gets three things of its own, all applied mechanically in portfolio.py:
#
#   1. A TIME STOP. Held past `scalp_max_hold_h`, the position is closed at the next cycle at
#      whatever the tape says — win, lose or flat. This is the whole point of the horizon: a
#      scalp that has not worked within its window is not a slow scalp, it is a failed one that
#      has silently become a swing trade nobody sized for.
#   2. A tighter hard stop. Sitting a scalp behind a −7% (equity) or −15% (crypto) cut means a
#      trade meant to last hours can lose what a multi-week position risks. Small size, tight
#      stop, out fast — the profile the rulebook already describes, now actually applied.
#   3. A tighter trail, so a scalp that works gives back hours of gain rather than days of it.
#
# Equity scalps get 8 hours rather than 24 because the wall clock is not the constraint there —
# the bell is. 8h covers a full session with room either side, and since the equity cycle is
# session-gated the close lands at the next open if a late entry runs past it. Crypto gets the
# full 24, which is the window Lind asked for and the only one a 24/7 venue can honour literally.
SCALP_MAX_HOLD_H = float(os.environ.get("BRAIN_SCALP_MAX_HOLD_H", "8"))
SCALP_HARD_STOP_PCT = float(os.environ.get("BRAIN_SCALP_HARD_STOP_PCT", "-3"))
SCALP_TRAIL_PCT = float(os.environ.get("BRAIN_SCALP_TRAIL_PCT", "4"))
SCALP_POSITION_PCT = float(os.environ.get("BRAIN_SCALP_POSITION_PCT", "6"))


# ── Rule sets: one book per asset class ─────────────────────────────────────────
# The constants above stay exactly as they were — they are the stock book's rules and half the
# codebase reads them directly. What changes is that the *portfolio* no longer reads them from
# the module; it takes a RuleSet. That is what lets a second book exist at all.
#
# Crypto could not share the stock numbers even if we wanted it to. A −7% hard stop is a
# considered level on a name that moves 1–2% a day and pure noise on one that routinely moves 5%
# in an afternoon: it would not be managing risk, it would be donating the position to the first
# ordinary Tuesday. Every gate below is re-derived for the asset, not scaled from the equity one.
class RuleSet:
    """The gates one paper book runs under. Instances are read-only by convention."""

    def __init__(self, name, ledger, starting_cash, max_positions, max_position_pct,
                 default_position_pct, max_new_trades_per_week, weekly_trade_target,
                 hard_stop_pct, trail_base_pct, trail_tight_15, trail_tight_20,
                 sector_fail_limit, sectors, tickers, always_open,
                 scalp_max_hold_h=SCALP_MAX_HOLD_H, scalp_hard_stop_pct=SCALP_HARD_STOP_PCT,
                 scalp_trail_pct=SCALP_TRAIL_PCT, scalp_position_pct=SCALP_POSITION_PCT):
        self.name = name
        self.ledger = ledger                       # filename inside MEMORY_DIR
        self.starting_cash = starting_cash
        self.max_positions = max_positions
        self.max_position_pct = max_position_pct
        self.default_position_pct = default_position_pct
        self.max_new_trades_per_week = max_new_trades_per_week
        self.weekly_trade_target = weekly_trade_target
        self.hard_stop_pct = hard_stop_pct
        self.trail_base_pct = trail_base_pct
        self.trail_tight_15 = trail_tight_15
        self.trail_tight_20 = trail_tight_20
        self.sector_fail_limit = sector_fail_limit
        self.sectors = sectors                     # ticker -> sector label
        self.tickers = tickers                     # the book's universe
        self.always_open = always_open             # True ⇒ no session gate; the venue never shuts
        # Scalp overrides. Every one of these replaces its swing-horizon counterpart above for a
        # position tagged SCALP; see the SCALP_* block for why a horizon has to be enforced.
        self.scalp_max_hold_h = scalp_max_hold_h
        self.scalp_hard_stop_pct = scalp_hard_stop_pct
        self.scalp_trail_pct = scalp_trail_pct
        self.scalp_position_pct = scalp_position_pct

    def sector_of(self, ticker):
        return self.sectors.get(ticker, "Other")

    def is_scalp(self, trade_type):
        """Is this horizon the enforced intraday one? Case- and whitespace-tolerant, because the
        value arrives from model JSON and `" scalp "` must not silently become a swing trade."""
        return str(trade_type or "").strip().upper() == "SCALP"

    def stop_pct_for(self, trade_type):
        """The hard cut this horizon runs under."""
        return self.scalp_hard_stop_pct if self.is_scalp(trade_type) else self.hard_stop_pct

    def default_pct_for(self, trade_type):
        """The default position weight for this horizon — scalps are sized smaller by design."""
        return self.scalp_position_pct if self.is_scalp(trade_type) else self.default_position_pct

    def weight_cap_for(self, trade_type):
        """The most of the book this horizon may hold in one name.

        A scalp is capped at its own size rather than at the book's, so "small size" is enforced
        the same way every other rule here is instead of being a sentence in the rulebook that
        the model is trusted to honour. A scalp asking for 25% is not a scalp.
        """
        return self.scalp_position_pct if self.is_scalp(trade_type) else self.max_position_pct

    def __repr__(self):
        return f"<RuleSet {self.name}>"


# Minimal sector map for the focus list; anything unmapped is "Other".
STOCK_SECTORS = {
    "AAPL": "Tech", "MSFT": "Tech", "NVDA": "Semis", "AMD": "Semis", "AVGO": "Semis",
    "TSM": "Semis", "MU": "Semis", "SMCI": "Semis", "ORCL": "Tech", "ADBE": "Tech",
    "CRM": "Tech", "PANW": "Tech", "SNOW": "Tech", "PLTR": "Tech",
    "TSLA": "Auto", "AMZN": "Consumer", "META": "Tech", "GOOGL": "Tech", "NFLX": "Media",
    "DIS": "Media", "UBER": "Tech", "COIN": "Crypto", "HOOD": "Fintech", "MSTR": "Crypto",
    "MARA": "Crypto", "SOFI": "Fintech", "JPM": "Banks", "GS": "Banks", "BAC": "Banks",
    "XOM": "Energy", "CVX": "Energy", "CEG": "Energy", "LLY": "Health", "UNH": "Health",
    "GE": "Industrial", "BA": "Industrial", "CAT": "Industrial",
}

# ── Crypto universe ─────────────────────────────────────────────────────────────
# Yahoo tickers, verified live 2026-07-27. Note SUI: the obvious `SUI-USD` resolves to nothing
# ("possibly delisted"), because Yahoo disambiguates the token from an unrelated listing by
# appending its CoinMarketCap id. `SUI20947-USD` is the working symbol and returns a full year of
# daily bars. Anyone "correcting" this back to SUI-USD silently drops SUI from every run.
CRYPTO_TICKERS = [
    os.environ.get("BRAIN_BTC_TICKER", "BTC-USD"),
    os.environ.get("BRAIN_ETH_TICKER", "ETH-USD"),
    os.environ.get("BRAIN_SOL_TICKER", "SOL-USD"),
    os.environ.get("BRAIN_SUI_TICKER", "SUI20947-USD"),
]
# Display names, because "SUI20947-USD" in an email subject line is unreadable.
CRYPTO_LABELS = {"BTC-USD": "BTC", "ETH-USD": "ETH", "SOL-USD": "SOL", "SUI20947-USD": "SUI"}
# BTC and ETH are the crypto tape's own regime indicators — neither SOL nor SUI trades
# independently of them in a risk-off hour. DXY and the VIX stay because crypto still reacts to
# dollar and equity risk appetite, which is exactly the kind of cross-asset read a token-only
# packet would miss.
CRYPTO_MARKET_CONTEXT = ["BTC-USD", "ETH-USD", "DX-Y.NYB", "^VIX"]

# Each token is its own sector. The alternative — labelling them all "Crypto" — means two
# consecutive losers anywhere locks the entire book out of new entries, which on a four-asset
# universe is not a sector brake, it is a shutdown.
CRYPTO_SECTORS = {"BTC-USD": "BTC", "ETH-USD": "ETH", "SOL-USD": "SOL", "SUI20947-USD": "SUI"}

# Search strings for Google News RSS. Finnhub's company-news endpoint is keyed on an equity symbol
# and returns nothing for a token, so this is the only headline source the crypto track has — the
# query has to carry the work. Each pairs the full name with the ticker because the name alone
# drags in unrelated stories ("Sui" is a common word) and the ticker alone is too sparse to fill a
# packet. `-stock` on BTC/ETH keeps out the MSTR/COIN/ETF coverage that is about an equity
# reacting to crypto rather than about the token itself.
CRYPTO_NEWS_QUERIES = {
    "BTC-USD": "Bitcoin BTC price OR analysis -stock",
    "ETH-USD": "Ethereum ETH price OR analysis -stock",
    "SOL-USD": "Solana SOL price OR analysis -stock",
    "SUI20947-USD": "\"Sui\" (SUI crypto OR blockchain OR token)",
}

STOCK_RULES = RuleSet(
    name="stock",
    ledger="PORTFOLIO.json",
    starting_cash=STARTING_CASH,
    max_positions=MAX_POSITIONS,
    max_position_pct=MAX_POSITION_PCT,
    default_position_pct=DEFAULT_POSITION_PCT,
    max_new_trades_per_week=MAX_NEW_TRADES_PER_WEEK,
    weekly_trade_target=WEEKLY_TRADE_TARGET,
    hard_stop_pct=HARD_STOP_PCT,
    trail_base_pct=TRAIL_BASE_PCT,
    trail_tight_15=TRAIL_TIGHT_15,
    trail_tight_20=TRAIL_TIGHT_20,
    sector_fail_limit=SECTOR_FAIL_LIMIT,
    sectors=STOCK_SECTORS,
    tickers=None,          # set below, once FOCUS_TICKERS exists
    always_open=False,
)

CRYPTO_RULES = RuleSet(
    name="crypto",
    ledger="PORTFOLIO_CRYPTO.json",
    # Its own $10k rather than a share of the stock book's, so the two equity curves are directly
    # comparable and a crypto drawdown can never consume a slot an equity setup needed.
    starting_cash=float(os.environ.get("BRAIN_CRYPTO_START_CASH", "10000")),
    # Four assets, so "max positions" is nearly moot — but matching the cap to the universe makes
    # it state the universe rather than imply headroom that does not exist. Keep this in step
    # with CRYPTO_TICKERS: a cap below the universe silently refuses the last token's entries.
    max_positions=int(os.environ.get("BRAIN_CRYPTO_MAX_POSITIONS", str(len(CRYPTO_TICKERS)))),
    # Higher per name than equities *because* the universe is shallow: at 15% the book would
    # sit 55% in cash permanently and never express a view it actually held.
    max_position_pct=float(os.environ.get("BRAIN_CRYPTO_MAX_POSITION_PCT", "25")),
    default_position_pct=float(os.environ.get("BRAIN_CRYPTO_DEFAULT_POSITION_PCT", "20")),
    # A 24/7 venue offers roughly three times the equity market's decision hours, so the weekly
    # brake sits higher — but only slightly, since there are only three things to trade.
    max_new_trades_per_week=int(os.environ.get("BRAIN_CRYPTO_MAX_NEW_TRADES", "8")),
    weekly_trade_target=int(os.environ.get("BRAIN_CRYPTO_WEEKLY_TARGET", "3")),
    # The numbers that matter most. BTC's ordinary daily range swallows a −7% stop whole, so an
    # equity stop here would exit on noise and call it discipline. Widened to match the asset's
    # actual volatility, with the trail widened in proportion.
    hard_stop_pct=float(os.environ.get("BRAIN_CRYPTO_HARD_STOP_PCT", "-15")),
    trail_base_pct=float(os.environ.get("BRAIN_CRYPTO_TRAIL_BASE", "20")),
    trail_tight_15=float(os.environ.get("BRAIN_CRYPTO_TRAIL_15", "15")),
    trail_tight_20=float(os.environ.get("BRAIN_CRYPTO_TRAIL_20", "10")),
    sector_fail_limit=int(os.environ.get("BRAIN_CRYPTO_SECTOR_FAIL_LIMIT", "2")),
    sectors=CRYPTO_SECTORS,
    tickers=CRYPTO_TICKERS,
    always_open=True,
    # The 24-hour scalp window, literally 24 hours because this venue has no bell to close
    # against. The stop and trail are wider than the equity scalp's for the same reason the swing
    # numbers are — a 3% cut on BTC is a coin flip on an ordinary hour, not a risk decision — but
    # far tighter than this book's own −15%/20% swing pair, which is what makes it a scalp.
    scalp_max_hold_h=float(os.environ.get("BRAIN_CRYPTO_SCALP_MAX_HOLD_H", "24")),
    scalp_hard_stop_pct=float(os.environ.get("BRAIN_CRYPTO_SCALP_HARD_STOP_PCT", "-6")),
    scalp_trail_pct=float(os.environ.get("BRAIN_CRYPTO_SCALP_TRAIL_PCT", "7")),
    scalp_position_pct=float(os.environ.get("BRAIN_CRYPTO_SCALP_POSITION_PCT", "12")),
)

RULE_SETS = {"stock": STOCK_RULES, "crypto": CRYPTO_RULES}


def rules_for(name):
    """Look up a book's rules by name, failing loudly on a typo.

    A silent fall back to the stock rules would run crypto on a −7% stop, which is the single
    most expensive way this could go wrong quietly.
    """
    try:
        return RULE_SETS[name]
    except KeyError:
        raise ValueError(f"unknown rule set {name!r} — expected one of {sorted(RULE_SETS)}")

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
# Deferred because RuleSet is declared above the focus list; kept here so there is exactly one
# definition of the stock universe rather than a copy that can drift out of sync.
STOCK_RULES.tickers = FOCUS_TICKERS

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

# ── Crypto screener thresholds ──────────────────────────────────────────────────
# The equity numbers are calibrated to an asset that gaps 1% on news and closes overnight. Crypto
# does neither: it never gaps because it never closes, and a 1% move is ten minutes of ordinary
# drift. Left unchanged, the screener would flag all three tokens on essentially every cycle,
# escalate to Opus every time, and turn a 24/7 track into a 24/7 bill for the word "nothing".
#
# RSI is the subtler one. A trending token can hold RSI above 70 for days without it meaning
# anything is stretched, so the bands widen rather than shift — 78/22 marks a genuine extreme on
# this tape where 70/30 marks a Tuesday.
CRYPTO_SCREEN_GAP_PCT = float(os.environ.get("BRAIN_CRYPTO_GAP_PCT", "3.0"))
CRYPTO_SCREEN_VOL_MULT = float(os.environ.get("BRAIN_CRYPTO_VOL_MULT", "2.0"))
CRYPTO_SCREEN_BREAKOUT_LOOKBACK = int(os.environ.get("BRAIN_CRYPTO_BREAKOUT", "20"))
CRYPTO_SCREEN_RSI_HOT = int(os.environ.get("BRAIN_CRYPTO_RSI_HOT", "78"))
CRYPTO_SCREEN_RSI_COLD = int(os.environ.get("BRAIN_CRYPTO_RSI_COLD", "22"))
CRYPTO_SCREEN_52W_PROXIMITY = 0.05     # a token 5% off its year high is "at" it in practice


class Thresholds:
    """One venue's screener bar. Bundled so the numbers travel together and cannot half-apply."""

    def __init__(self, name, gap_pct, vol_mult, breakout_lookback, rsi_hot, rsi_cold, prox_52w):
        self.name = name
        self.gap_pct = gap_pct
        self.vol_mult = vol_mult
        self.breakout_lookback = breakout_lookback
        self.rsi_hot = rsi_hot
        self.rsi_cold = rsi_cold
        self.prox_52w = prox_52w

    def __repr__(self):
        return f"<Thresholds {self.name}>"


STOCK_SCREEN = Thresholds("stock", SCREEN_GAP_PCT, SCREEN_VOL_MULT, SCREEN_BREAKOUT_LOOKBACK,
                          SCREEN_RSI_HOT, SCREEN_RSI_COLD, SCREEN_52W_PROXIMITY)
CRYPTO_SCREEN = Thresholds("crypto", CRYPTO_SCREEN_GAP_PCT, CRYPTO_SCREEN_VOL_MULT,
                           CRYPTO_SCREEN_BREAKOUT_LOOKBACK, CRYPTO_SCREEN_RSI_HOT,
                           CRYPTO_SCREEN_RSI_COLD, CRYPTO_SCREEN_52W_PROXIMITY)


def is_crypto(ticker):
    """Does this symbol trade on the 24/7 venue?

    Yahoo names every spot crypto pair `<TOKEN>-USD`, so the suffix is the test rather than
    membership of CRYPTO_TICKERS — that way a token added to the universe, or one that only ever
    appears as market context, is still recognised as crypto by everything downstream.

    The equity symbols cannot collide with it: index tickers carry a `^` and futures an `=F`, and
    the dollar index is `DX-Y.NYB`. Nothing in the equity universe ends in `-USD`.
    """
    return bool(ticker) and str(ticker).upper().endswith("-USD")


def thresholds_for(ticker):
    """The screener bar for whichever venue this ticker trades on.

    Dispatching on the symbol rather than on a parameter passed down the call chain is deliberate:
    the failure it prevents is a crypto snapshot scored against equity thresholds because one call
    site forgot to forward the flag, which would show up as three tokens flagged on every cycle and
    would look like a market condition rather than a bug.
    """
    return CRYPTO_SCREEN if is_crypto(ticker) else STOCK_SCREEN


def label_for(ticker):
    """Human-readable name for an email subject or a dashboard card — `SUI20947-USD` reads as SUI."""
    return CRYPTO_LABELS.get(ticker, ticker)

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
