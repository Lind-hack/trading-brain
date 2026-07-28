"""Shared configuration for the Market Brain.

All secrets come from environment variables (loaded from /root/.secrets/signal.env on the
VPS, or the user's shell locally). Nothing here is a hard dependency: every consumer
degrades gracefully when a key is absent, so the brain still runs on free data alone.
"""
import math
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
#
# These were "high,medium" and 45, on the reasoning that at one entry per session the cost of
# missing a real setup outweighs the cost of an Opus run that concludes nothing. The logs settled
# it the other way on 2026-07-28: **30 of 30 cycles escalated.** A gate that never closes is not a
# gate, and the deep run it was meant to ration is the most expensive call in the pipeline.
#
# The reason breakdown across those 30 runs — 34 medium-materiality, 26 high, 6 pure-sentiment,
# with 16 runs carrying no high-materiality reason at all — is what picked the new values. `medium`
# fires on essentially any news day, and the saturation guard in news_intel.escalation_reasons does
# not save it: an under-covered story of middling importance still clears, and there is always one.
# 70 on sentiment leaves the tone-only path as a genuine outlier rather than a second front door.
#
# The screener still escalates on its own triggers, so a chart setup with no news attached is
# unaffected — this only stops news *alone* from buying a deep run.
NEWS_ESCALATE_MATERIALITY = tuple(
    s.strip() for s in os.environ.get("BRAIN_NEWS_ESCALATE_MATERIALITY", "high").split(",")
    if s.strip())
NEWS_ESCALATE_ABS_SENTIMENT = int(os.environ.get("BRAIN_NEWS_ESCALATE_SENTIMENT", "70"))

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


# ── Entry quality ───────────────────────────────────────────────────────────────
#
# Lind's read of the first losing week, 2026-07-28: "the news and technical analysis were good, it
# was just that the position entry wasn't good." The ledger, in full — four fills, all four
# underwater:
#
#   JPM  357.50  2026-07-27   20d hi 356.20   range_pos 1.045   ext_atr +2.36   open   -0.4%
#   BAC   62.50  2026-07-27   20d hi  62.13   range_pos 1.072   ext_atr +2.02   open   -0.6%
#   CRM  174.00  2026-07-27   20d hi 173.79   range_pos 1.012   ext_atr +1.00   open   -0.2%
#   CRM  163.66  2026-07-24   20d hi 173.79   range_pos 0.409   ext_atr -0.22   stopped -4.1%
#
# Be honest about what that does and does not show. It does *not* show that range-top entries lose
# more: the only trade closed so far is the mid-range one, and it is the biggest loser of the four.
# Four trades is not a sample and no correlation should be read out of it. What it does show is
# structural and is Lind's actual point — three of the four were bought at the one price on the
# chart with no room left above and the whole 20-day range below as downside. That is a risk shape,
# visible at the moment of entry, independent of how these particular four resolve. The thesis is
# not what these gates check; the analyst and the screener already did that. They check the *price
# being paid for it*.
#
# `range_pos` is where the entry sits in the last 20 daily closes: 0 at the low, 1 at the high.
# `ext_atr` is how far above the 20-day mean it sits, measured in ATRs, which is the same question
# asked in a way that does not care whether the range happens to be wide or narrow. A rejection
# needs only one of them; they fail in different market shapes and catching CRM took the range one.
#
# The cost of this is real and worth stating: a genuine 20-day breakout has `range_pos >= 1.0` by
# construction, so this book will no longer buy breakouts at the breakout. It buys the retest or it
# does not buy. That is the trade-off Lind asked for, and the ledger above is the argument for it.
# Nothing is silently dropped — a refused entry still emails, carrying the price that would pass.
ENTRY_MAX_RANGE_POS = float(os.environ.get("BRAIN_ENTRY_MAX_RANGE_POS", "0.85"))
ENTRY_MAX_EXT_ATR = float(os.environ.get("BRAIN_ENTRY_MAX_EXT_ATR", "2.0"))
# Paying above the last print is a different mistake: not "the chart is extended" but "this quote
# is stale or aspirational". The four real fills came in between 1.16% below and 0.03% above the
# tape, so this bar never fires on an entry that was priced off the packet it was given.
ENTRY_MAX_CHASE_PCT = float(os.environ.get("BRAIN_ENTRY_MAX_CHASE_PCT", "0.25"))


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
                 scalp_trail_pct=SCALP_TRAIL_PCT, scalp_position_pct=SCALP_POSITION_PCT,
                 entry_max_range_pos=ENTRY_MAX_RANGE_POS, entry_max_ext_atr=ENTRY_MAX_EXT_ATR,
                 entry_max_chase_pct=ENTRY_MAX_CHASE_PCT):
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
        # Entry quality. See the ENTRY_* block for the trades that bought these numbers.
        self.entry_max_range_pos = entry_max_range_pos
        self.entry_max_ext_atr = entry_max_ext_atr
        self.entry_max_chase_pct = entry_max_chase_pct

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
# ONE table, four maps derived from it. They used to be four hand-written literals and
# tests/test_crypto_book.py already had to assert two of them agreed, which is the tell that they
# were one thing pretending to be four. Adding a token now means adding a row, and a row that is
# missing a field will not import.
#
# On the Yahoo symbols — this is the part that silently breaks. When a plain ticker is already
# taken on Yahoo, it disambiguates the token by appending its CoinMarketCap id, so the obvious
# `SUI-USD` / `UNI-USD` / `PEPE-USD` form resolves to nothing or, worse, to an unrelated listing.
# Every symbol below was resolved through yf.Search and then verified against its `shortName` on
# 2026-07-28. Two near-misses from that pass, recorded so nobody repeats them: picking the
# highest-volume match for "Ondo" returns AVGOON-USD ("Broadcom Tokenized Stock") and for "Polygon"
# returns MMF21370-USD ("MM Finance"). Volume is not identity — check the name.
#
# On sector labels — the four-token book gave every token its own sector, because a shared "Crypto"
# label meant two consecutive losers anywhere locked the whole book out of new entries. That was a
# shutdown, not a brake. At eighteen tokens across six sectors the brake is a brake again: two
# losing meme trades stop meme trades and leave DeFi, DePIN and the majors open.
#
# On coverage — Lind asked for Solana and its branches. The Solana-native names are SOL, BONK, WIF,
# TRUMP, RENDER, JUP, JTO and PYTH. The last three were briefly left out because Alpaca lists none
# of them, which was the wrong test: Lind trades this book on BingX, where all eighteen are listed
# (checked against the exchange's own symbol feed, 2237 spot pairs, 2026-07-28). Alpaca is the
# auto-execution mirror, not the venue — a name it cannot fill still gets screened, analysed and
# emailed, and broker.submit already declines those with "simulator only" rather than dropping them.
#
# Search strings are for Google News RSS. Finnhub's company-news endpoint is keyed on an equity
# symbol and returns nothing for a token, so this is the only headline source the crypto track has
# and the query has to carry the work. Each pairs the full project name with the ticker: the name
# alone drags in unrelated stories (SUI, WIF and PEPE are all ordinary words, DOGE is also a US
# federal agency, and TRUMP is a minefield), the ticker alone is too sparse to fill a packet.
# `-stock` on the majors keeps out MSTR/COIN/ETF coverage, which is an equity reacting to crypto
# rather than the token itself.
_CRYPTO_UNIVERSE = (
    # Yahoo symbol      label     sector    Google News RSS query
    ("BTC-USD",         "BTC",    "Major",  "Bitcoin BTC price OR analysis -stock"),
    ("ETH-USD",         "ETH",    "Major",  "Ethereum ETH price OR analysis -stock"),
    ("SOL-USD",         "SOL",    "L1",     "Solana SOL price OR analysis -stock"),
    ("SUI20947-USD",    "SUI",    "L1",     "\"Sui\" (SUI crypto OR blockchain OR token)"),
    # Meme. Sub-cent prices make `close × volume` read as a few hundred dollars a day and look
    # broken; raw unit volume is healthy (BONK median 37.5M units, max/median 4.2×) and the
    # screener's trigger is a 2× *ratio*, so absolute scale never enters into it.
    ("BONK-USD",        "BONK",   "Meme",   "Bonk BONK (Solana OR memecoin OR token)"),
    ("WIF-USD",         "WIF",    "Meme",   "dogwifhat WIF (Solana OR memecoin OR token)"),
    ("TRUMP35336-USD",  "TRUMP",  "Meme",   "\"TRUMP memecoin\" OR \"Official Trump\" token"),
    ("DOGE-USD",        "DOGE",   "Meme",   "Dogecoin DOGE (crypto OR token) -\"Government Efficiency\""),
    ("PEPE24478-USD",   "PEPE",   "Meme",   "\"Pepe coin\" PEPE (crypto OR memecoin OR token)"),
    # DeFi.
    ("AAVE-USD",        "AAVE",   "DeFi",   "Aave AAVE (DeFi OR lending OR protocol)"),
    ("UNI7083-USD",     "UNI",    "DeFi",   "Uniswap UNI (DeFi OR DEX OR protocol)"),
    ("CRV-USD",         "CRV",    "DeFi",   "\"Curve Finance\" CRV (DeFi OR stablecoin OR protocol)"),
    # `JUP-USD` resolves on Yahoo and is a *different* Jupiter — it prints $0.00026 against the
    # live $0.183. Both listings carry the shortName "Jupiter USD", so the name check that caught
    # the Ondo and Polygon impostors does not separate these two. The price does.
    ("JUP29210-USD",    "JUP",    "DeFi",   "Jupiter JUP (Solana OR DEX OR perps OR protocol)"),
    # Liquid staking.
    ("LDO-USD",         "LDO",    "LST",    "Lido LDO (liquid staking OR stETH OR protocol)"),
    ("JTO-USD",         "JTO",    "LST",    "Jito JTO (Solana OR liquid staking OR MEV)"),
    # DePIN. Pyth is an oracle rather than physical infrastructure, but it trades as Solana data
    # infra and correlates with this group far more than with DeFi — and correlation is the only
    # thing the sector brake is trying to model.
    ("RENDER-USD",      "RENDER", "DePIN",  "\"Render Network\" RENDER (DePIN OR GPU OR token)"),
    ("FIL-USD",         "FIL",    "DePIN",  "Filecoin FIL (DePIN OR storage OR crypto)"),
    ("PYTH-USD",        "PYTH",   "DePIN",  "\"Pyth Network\" PYTH (oracle OR Solana OR token)"),
)

# `BRAIN_<LABEL>_TICKER` still overrides any symbol — the four that existed before this table
# (BRAIN_BTC_TICKER and friends) keep working unchanged, and the new rows get the same escape
# hatch for the day Yahoo renames one of them mid-week.
def _crypto_symbol(symbol, label):
    return os.environ.get(f"BRAIN_{label}_TICKER", symbol)


CRYPTO_TICKERS = [_crypto_symbol(s, lbl) for s, lbl, _sec, _q in _CRYPTO_UNIVERSE]
# Display names, because "SUI20947-USD" in an email subject line is unreadable.
CRYPTO_LABELS = {_crypto_symbol(s, lbl): lbl for s, lbl, _sec, _q in _CRYPTO_UNIVERSE}
CRYPTO_SECTORS = {_crypto_symbol(s, lbl): sec for s, lbl, sec, _q in _CRYPTO_UNIVERSE}
CRYPTO_NEWS_QUERIES = {_crypto_symbol(s, lbl): q for s, lbl, _sec, q in _CRYPTO_UNIVERSE}

# BTC and ETH are the crypto tape's own regime indicators — nothing in the table above trades
# independently of them in a risk-off hour, and that goes double for the meme sector. DXY and the
# VIX stay because crypto still reacts to dollar and equity risk appetite, which is exactly the
# kind of cross-asset read a token-only packet would miss.
CRYPTO_MARKET_CONTEXT = ["BTC-USD", "ETH-USD", "DX-Y.NYB", "^VIX"]

# How many tokens Haiku reads per crypto cycle. This number is the whole reason the universe could
# grow at all: reading all eighteen every hour would have quintupled the tier-1 bill and undone the
# cut made on 2026-07-28. It is a floor for held names rather than a hard cap — see
# market_brain._crypto_news_watchlist, which never drops a token the book is actually holding.
CRYPTO_NEWS_TICKERS_PER_CYCLE = int(os.environ.get("BRAIN_CRYPTO_NEWS_TICKERS", "6"))

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
    # This was `len(CRYPTO_TICKERS)` while the universe was four tokens, on the reasoning that a
    # cap below the universe silently refuses the last token's entries. That inverts at eighteen:
    # the same expression now reads as headroom for eighteen positions the $10k could not fund
    # anyway, and at a 20% default weight cash binds at five. Pinned to 8, matching the equity
    # book, so the number states a diversification limit rather than restating the table above.
    max_positions=int(os.environ.get("BRAIN_CRYPTO_MAX_POSITIONS", "8")),
    # Higher per name than equities because the *tradeable* set on any given day is shallow —
    # conviction here concentrates in a handful of tokens. At 15% the book would sit deep in cash
    # permanently and never express a view it actually held.
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
    # A token holds the top of its 20-day range for weeks in a trend where an equity would have
    # mean-reverted twice, and its ATR is a larger fraction of price, so both bars sit looser here.
    # Same rule, re-derived for the venue rather than copied across — as with every other number
    # on this book. The only crypto entry so far (ETH 1877, range_pos 0.634, ext_atr +0.23) clears
    # both bars comfortably, so these are not fitted to it.
    entry_max_range_pos=float(os.environ.get("BRAIN_CRYPTO_ENTRY_MAX_RANGE_POS", "0.90")),
    entry_max_ext_atr=float(os.environ.get("BRAIN_CRYPTO_ENTRY_MAX_EXT_ATR", "2.5")),
    entry_max_chase_pct=float(os.environ.get("BRAIN_CRYPTO_ENTRY_MAX_CHASE_PCT", "0.5")),
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

# Which currencies' events may *force* a deep run. The keyword list above matches on title alone,
# and titles are not country-specific: "Unemployment Rate" caught EUR's Spanish print, "CPI" caught
# JPY's BOJ Core CPI, and both were tagged Low impact. Each one forced an Opus cycle on the theory
# that a eurozone regional release reprices Bitcoin. Foreign events stay in the packet as context —
# this list only governs escalation.
CALENDAR_ESCALATE_COUNTRIES = {
    c.strip().upper()
    for c in os.environ.get("BRAIN_CALENDAR_COUNTRIES", "USD").split(",")
    if c.strip()
}

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

# The score one token must reach before the *cycle* escalates. Distinct from the per-ticker flag
# bar, which decides who appears on the ranked list handed to the analyst.
#
# "Any token flagged ⇒ escalate" was a real gate at four tokens and stopped being one at eighteen:
# a Bollinger squeeze fires on most of the book at once whenever crypto vol compresses, so a score
# of 3 became the resting state rather than an event. The floor asks for something that actually
# happened — a breakout on volume plus corroboration, not a pending-breakout pattern and an RSI
# reading. A held name still escalates at any score: a position under stress is the one case where
# paying for a look is always worth it.
CRYPTO_ESCALATE_SCORE = int(os.environ.get("BRAIN_CRYPTO_ESCALATE_SCORE", "5"))


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


def round_price(px, dp=2):
    """Round a price without rounding a sub-cent token down to nothing.

    Lives here rather than in portfolio.py because the collector needs the same rule: `config` is
    the leaf module everything already imports, and two copies of this would drift.

    Two decimals is right for every equity, and was right for a crypto book of BTC, ETH, SOL and
    SUI. It stops being right the moment a token trades below a cent, and it fails in the worst
    available direction. BONK at $0.00000296 rounds to **0.0**, which means:

      - `indicators.price` is 0.0, so the screener's whole indicator block for the token is zeros
        and every chart claim it makes is arithmetic on nothing;
      - the -15% hard stop is `round(0.00000252, 2)` = 0.0, and a stop of zero is not a tight stop,
        it is no stop — `gain <= stop_pct` can never fire, so the position rides to zero;
      - the averaged entry on an ADD becomes 0.0, and then every P&L line divides by zero.

    Below $1 the rounding is significant-figure based instead: enough decimal places to keep six
    meaningful digits wherever the exponent lands. At or above $1 nothing changes at all — this
    must not quietly re-round an equity price that has been correct since the first commit.
    """
    try:
        f = float(px)
    except (TypeError, ValueError):
        return px
    if not math.isfinite(f) or f == 0:
        return px if not math.isfinite(f) else f
    if abs(f) >= 1:
        return round(f, dp)
    return round(f, -math.floor(math.log10(abs(f))) + 5)


def format_price(px):
    """A price as a human reads it: "142.11", "65,000.46", "0.00000296".

    The display counterpart to round_price, and it exists for the same reason. `:,.2f` renders
    every number on a BONK or PEPE card as "0.00" — a trade card with no trade on it, and pattern
    details that read "close 0.00 < prior 20d low 0.00", which look like an engine fault rather
    than a formatting choice. Four significant figures below a cent, two decimals at or above it.
    """
    try:
        f = float(px)
    except (TypeError, ValueError):
        return "—"
    if not math.isfinite(f):
        return "—"
    if f and abs(f) < 0.01:
        # Capped so a rogue value cannot render a forty-character number into the middle of a card.
        digits = min(12, -math.floor(math.log10(abs(f))) + 3)
        return f"{f:,.{digits}f}".rstrip("0").rstrip(".")
    return f"{f:,.2f}"


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
