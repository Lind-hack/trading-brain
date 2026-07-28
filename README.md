# Market Brain — Signal Deck v2

A 24/7 AI trading-**analysis** engine. It watches the US equity market, and when something
technically or fundamentally interesting happens, it asks Claude (Opus, via the Claude Code
subscription) for a disciplined read — then emails Lind detailed trade ideas across three
horizons and books them into a simulated $10,000 paper portfolio.

> **Paper trading only. The only broker connection is an Alpaca *paper* account, and
> `brain/broker.py` re-verifies the paper host before every request — a live host or a live key
> fails closed. No real order can be placed. Educational analysis from delayed data — not
> financial advice.**

## How it works

```
collect (free)            news quality (free, Python)    screen (free, Python)          deep run (subscription)
yfinance bars + patterns  dedupe across outlets, score   score vs thresholds:           only if the screener fires
SEC EDGAR filings         crowding + age + source tier,  breakout+vol, gap, RSI edge,   OR at an anchor:
Google News / Finnhub     drop listicles and clickbait   event <2h out, news on a held  `claude -p --model opus`
ForexFactory calendar        ↓                           name → escalate, else log&exit  over a data packet
Finnhub fundamentals      Haiku 4.5 reads what survived  ────────────────────────────►  → strict JSON: signals[]
                          → per-ticker sentiment /                                        (type/why/indicators/
                            materiality + macro_read                                       chart/news/analog/sources)
                                                                                         → gates enforced in code
                                                                                         → email + Obsidian
                                                                                           + Alpaca paper fill
                                                                                           + Supabase dashboard
                                                                                           + git-committed memory
```

- **Hybrid & cheap.** A deterministic Python screener runs every 30 min for $0. The paid-attention
  deep run (Opus) only fires when the screener escalates or at 3 daily anchors (pre-market, midday,
  close). No Anthropic API key — Lind is region-ineligible, so the engine shells out to the
  authenticated `claude` CLI on his subscription.
- **News the crowd hasn't read.** `brain/news_quality.py` clusters every headline across outlets and
  scores it on **crowding** (how many outlets ran the same story), **age** (measured from the
  article's own publish time, so a three-day-old story is stale on first sight), and **source tier**
  (the SEC filing itself > wire > mainstream > aggregator rewrite). Listicles, advice-bait, hype and
  content-farm rewrites are dropped outright; only what survives reaches Haiku, and each surviving
  headline carries its `crowding` / `source_tier` forward so Opus can set `news_edge` — what this
  trade knows that the tape hasn't priced. A saturated story is context, never an entry.
- **Company facts, not just a price ratio.** Finnhub supplies margins, ROE/ROA, leverage,
  revenue/EPS growth, the 52-week range, `eps_beats_last_4`, `days_to_earnings`, the analyst spread
  with its one-month revision, and open-market Form 4 insider buying and selling (grants and option
  exercises are compensation, not conviction). A shared sliding-window rate limiter keeps every
  caller in the process under the free tier's minute cap.
- **Pattern + news memory.** Every pattern/event is logged to `brain-memory/EVENT-LOG.jsonl` with a
  market snapshot; later cycles backfill 1h/4h/1d/5d forward returns, building a historical-analog
  database Claude cites ("last 6 hot-CPI prints → avg −0.8% SPY over 4h").
- **A worldview that outlives the cycle.** `brain/thesis.py` keeps a **long-term thesis board** in
  `brain-memory/THESES.json` — up to 8 structural 1–3 year bets (the demand shift, the companies
  that capture it, what would prove it wrong), each with a conviction score. Cycles may only hang
  *evidence* on a thesis; conviction itself moves twice a week, at the Monday digest and the Friday
  recap, so a noisy tape cannot talk the board out of a multi-year view. Any `LONG_TERM` signal is
  annotated with the thesis it belongs to, and an idea that fits no thesis has to justify itself on
  its own.
- **The same idea is not emailed twice.** `brain/dedupe.py` suppresses a signal that repeats one
  already sent, per horizon: 3 h for a scalp, 24 h for a swing, 120 h for a long-term call. It
  re-emits early only when the setup has genuinely changed — the entry has drifted more than 1.5%,
  confidence has moved 15 points, or the direction has flipped. Suppressed ideas still reach the
  dashboard, marked `duplicate`; they just stop filling the inbox. The one repeat that always gets
  through is a repeat being **executed**: if the earlier call was blocked by the gates and never
  became a position, the idea is not old news and the email is the first time you hear it was
  taken. Suppression exists to stop repetition, not to stop trades.
- **Discipline in code.** `brain/portfolio.py` enforces max 8 positions, ≤15%/name, ≤6 new
  trades/week, −7% hard stop, a tightening trailing stop, and sector lockouts. Claude only proposes;
  the code disposes.
- **The account holds the plan, not an approximation of it.** An approved equity buy reaches Alpaca
  as a **bracket** — a limit at the quoted entry, `stop_loss` at the stop the book will honour,
  `take_profit` at target1 — so the stop survives an overnight gap with no code running, instead of
  existing only in a poll that fires every 30 minutes. Alpaca takes no bracket on crypto, under one
  whole share, or with a stop the wrong side of the entry; those degrade to the previous market
  order and the email names the reason. Because a limit is a request and not a fill, every cycle
  reconciles the book against the broker before marking to market: an entry that never filled is
  backed out — cash returned, pace credited back, logged to `unfilled` and deliberately kept out of
  `closed_trades`, where a phantom 0.0% row would corrupt every win-rate the recap computes.
- **A pace target, not a quota.** The analyst is aimed at roughly 5 new trades a week — about one
  per session — and every deep packet carries `pace`: trades taken, sessions left, and whether the
  week is behind. It is shown, never enforced. No rule fires a trade to hit a number, because the
  only way to guarantee a daily trade is to buy the least-bad thing on a dead tape. The caps sit
  *above* the target so they brake churn instead of blocking a good Thursday setup.
- **It only runs when the market is open.** See below — the crontab proposes, `brain/market_hours.py`
  decides.

## When it runs

Monday–Friday, US market hours only. No weekend runs, no holiday runs, nothing overnight.

**The crontab proposes; the gate decides.** `brain/market_hours.py::gate(mode, now)` returns
`(allowed, reason)` against the *real* session, so it reasons about DST, the federal holiday
calendar (Good Friday included, via computus) and 1:00 PM half-days rather than about clock times.
An early close moves the close anchor with it; a DST change moves nothing at all.

| Mode | When it is allowed |
|---|---|
| `--cycle` | every 30 min while the session is open |
| `--anchor pre` | up to 3.5 h before the open |
| `--anchor mid` | mid-session |
| `--anchor close` | up to 4 h after the close |
| `--weekly-review` | **Friday only**, after the week's final close |
| `--digest` | **Monday only** (or the week's first trading day), before the open |

Debian's cron has no `CRON_TZ` (verified on the box: `grep -ac CRON_TZ /usr/sbin/cron` → `0`), so
`vps/crontab.txt` is written in **UTC** and offers each anchor at *both* of its possible UTC times —
one is right in EDT, the other in EST. In one DST regime both candidates are legal, so
`brain/runlog.py` records each once-a-day mode after it succeeds and the duplicate tick skips
itself. Recording happens *after* success, never before, so a crashed anchor is retried by the next
candidate tick instead of being silently lost. `tests/test_runlog.py` walks the shipped crontab
times through the shipped gate in both halves of the year and asserts exactly one run survives.

`--ignore-market-hours` overrides both the gate and the run-log, for manual backfills.

So the week has a shape: **one week-ahead digest before Monday's open**, then signals only while a
session is actually open, then **one recap after Friday's close**. Between the close and the next
open the inbox stays quiet — no evening signals, nothing on Saturday or Sunday.

## The Monday week-ahead digest

`--digest` sends the one email that arrives before the week starts: the macro calendar the week
turns on, the names already held and what has to happen to them, the watchlist with its triggers,
and the long-term thesis board after its first of two weekly re-scores. It is laid out as scannable
sections — a regime line, a calendar strip, per-name cards — rather than a wall of text.

## The Friday recap

When the week's final close passes, `--weekly-review` emails and journals: every trade taken with
its grading, every signal sent **and** every signal merely recommended, which indicators showed up
on the winners versus the losers, whether confidence numbers were calibrated, and concrete pipeline
changes to trade better next week. It also re-scores the thesis board for the second time that
week. Its conclusions are written to `brain-memory/STRATEGY.md` and come back to Opus in the next
week's packets as `strategy_lessons` — so the corrections compound.

## The dashboard

[signal-deck-bice.vercel.app](https://signal-deck-bice.vercel.app) reads Supabase directly and
refreshes every 60 s. The brain's half of the page is four labelled sections, because a trade the
engine **took** and one it merely **recommended** must not look alike:

| Section | What it holds |
|---|---|
| 🧠 AI Market Brain | the paper book — equity, cash, open positions, the current outlook |
| 🏦 Long-term thesis board | the structural 1–3 year bets with their conviction, tickers, and "wrong if" |
| ✅ Trades taken | every paper fill, joined back to the signal that caused it |
| 💡 Recommended — not taken | ideas that were blocked (with the rule that blocked them) or advisory only |

Every card splits its reasoning into a **📉 Technical read** pane (chart structure, indicator
values, indicators used) and a **📰 News read** pane (what Haiku concluded, and the under-covered
angle behind `news_edge`), with the confidence rationale, pipeline stages and historical analog
folded into a collapsed *Deeper* block. The verdict comes from the `outcome` column
(`executed` / `rejected` / `advisory`); rows written before that column existed fall back to
matching the fill tape.

## Layout

| Path | What |
|---|---|
| `market_brain.py` | CLI orchestrator (`--cycle`, `--anchor pre\|mid\|close`, `--research T`, `--weekly-review`, `--digest`, `--dry-run`, `--no-claude`, `--ignore-market-hours`) |
| `brain/market_hours.py` | the time authority — NYSE sessions, holidays, half-days, per-mode `gate()` |
| `brain/runlog.py` | once-a-day marker so paired UTC cron ticks can't double-fire an anchor |
| `brain/collect.py` | bars + daily-bar cache, indicators, chart-pattern detectors, news, ForexFactory, Finnhub, SEC EDGAR |
| `brain/news_quality.py` | crowding / age / source-tier scoring, clickbait drop, seen-store |
| `brain/news_intel.py` | tier 1 — Haiku 4.5 reads the surviving headlines |
| `brain/screen.py` | deterministic 30-min screener (no LLM) |
| `brain/deep.py` | tier 2 — builds the packet + prompt, invokes `claude -p`, parses JSON, fallback |
| `brain/dedupe.py` | per-horizon signal cooldowns — stops the same idea being emailed twice |
| `brain/thesis.py` | the long-term thesis board: 1–3 year structural bets, re-scored twice a week |
| `brain/portfolio.py` | paper-portfolio gates + mechanical exits |
| `brain/broker.py` | Alpaca **paper** mirror — equity buys go as brackets (limit entry + stop + target); paper host re-verified per request, fails closed |
| `brain/journal.py` | per-trade Obsidian notes, exit grading, weekly recap analytics |
| `brain/notify.py` | detailed HTML email (scalp / short-term / long-term cards) |
| `brain/obsidian.py` | writes every run to the Obsidian second brain |
| `brain/supabase.py` | dashboard feed + heartbeat |
| `brain/memory.py` | EVENT-LOG + historical analogs + git-as-memory |
| `prompts/*.md` | per-mode deep-run prompt templates |
| `brain-memory/` | **tracked** state: portfolio, event log, `STRATEGY.md`, `THESES.json`, logs (committed each run) |
| `tests/` | 383 tests — gates, gate windows, run-log/DST, news quality, dedupe, theses, dashboard feed, bars cache, wiring |
| `vps/` | `setup.sh`, `crontab.txt` (UTC), `brain.sql` |
| `CLAUDE.md` | the analyst rulebook (auto-loads when `claude -p` runs here) |

## Local quick start

```bash
pip install -r requirements.txt
python market_brain.py --anchor mid --dry-run     # full pipeline, no email/dashboard/push
python market_brain.py --research NVDA --dry-run   # single-ticker deep dive
python market_brain.py --digest --dry-run --ignore-market-hours   # the week-ahead email
python -m pytest tests/ -q                          # 383 tests
```

Off-hours, every mode exits at the gate. Add `--ignore-market-hours` to run anyway:

```bash
python market_brain.py --cycle --ignore-market-hours --dry-run --no-claude
```

`--dry-run` prints the screener verdict and the email that *would* send. `--no-claude` skips the
subscription call and uses the deterministic fallback (handy with no `claude` CLI available).

## Configuration (all via env; every key is optional)

| Env var | Purpose | Without it |
|---|---|---|
| `GMAIL_USER`, `GMAIL_APP_PASSWORD` | send the email | no email sent |
| `BRAIN_RECIPIENT` | who gets the mail (default `lindsylqa@gmail.com`) | — |
| `FINNHUB_API_KEY` | fundamentals, earnings, analysts, insiders, company news (finnhub.io free) | fundamentals section empty |
| `BRAIN_FINNHUB_RPM` | shared minute cap across all callers (default `55`) | — |
| `ALPACA_PAPER_KEY_ID`, `ALPACA_PAPER_SECRET_KEY` | mirror approved trades to the Alpaca **paper** account | internal simulation only |
| `LIND_BRAIN` | Obsidian vault root | no vault write |
| `SUPABASE_URL`, `SUPABASE_ANON_KEY`, `SIGNAL_INGEST_KEY` | dashboard feed | no dashboard push |
| `BRAIN_DEEP_MODEL` (default `claude-opus-5`), `BRAIN_LITE_MODEL` (Haiku 4.5), `CLAUDE_BIN` | model IDs / CLI path | — |
| `BRAIN_NEWS_MIN_NOVELTY` (default `30`), `BRAIN_NEWS_SEC` (default on) | how aggressively recycled news is dropped; SEC EDGAR on/off | — |
| `BRAIN_BARS_CACHE` (default on), `BRAIN_YF_TIMEOUT` (default `20`) | daily-bar disk cache; per-fetch socket timeout | — |
| `BRAIN_COOLDOWN_SCALP_H` (`3`), `BRAIN_COOLDOWN_SWING_H` (`24`), `BRAIN_COOLDOWN_LONG_H` (`120`) | how long a sent signal suppresses its repeat, per horizon | — |
| `BRAIN_SIGNAL_DRIFT_PCT` (`1.5`), `BRAIN_SIGNAL_CONF_JUMP` (`15`) | what counts as a *changed* setup, so it may re-send inside the cooldown | — |
| `BRAIN_WEEKLY_TRADE_TARGET` (`5`) | the pace shown to the analyst — displayed, never enforced | no pace context |
| `BRAIN_MAX_POSITIONS` (`8`), `BRAIN_MAX_NEW_TRADES` (`6`), `BRAIN_MAX_POSITION_PCT` (`15`), `BRAIN_DEFAULT_POSITION_PCT` (`12`) | the portfolio gates and the fallback size for an unsized proposal | — |
| `BRAIN_SCREEN_GAP_PCT` (`1.0`), `BRAIN_SCREEN_VOL_MULT` (`1.5`), `BRAIN_SCREEN_BREAKOUT` (`15`), `BRAIN_SCREEN_RSI_HOT`/`_COLD` (`70`/`30`) | how easily the screener escalates — wider means more looks, not looser trades | — |
| `BRAIN_NEWS_ESCALATE_MATERIALITY` (`high,medium`), `BRAIN_NEWS_ESCALATE_SENTIMENT` (`45`) | when Haiku's read alone buys an Opus run | — |
| `BRAIN_NEWS_TICKERS` (`20`), `BRAIN_NEWS_DISCOVERY_RESERVE` (`4`) | how many names Haiku reads per cycle, and how many of those places are held back for names the *chart* is silent about — the only route to a news-first idea | — |
| `BRAIN_THESIS_MAX` (`8`), `BRAIN_THESIS_MIN_CONVICTION` (`55`), `BRAIN_THESIS_STALE_DAYS` (`45`) | thesis-board size, the conviction floor below which a thesis is retired, and how long without evidence makes one stale | — |

See `brain/config.py` for the full list (thresholds, focus tickers, portfolio limits).

## Deploy (Oracle VPS)

```bash
sudo bash vps/setup.sh          # Node + claude-code, clone, venv, env additions
claude setup-token              # USER — authenticate the subscription (paste the code)
# add FINNHUB_API_KEY + ALPACA_PAPER_* to /root/.secrets/signal.env; paste vps/brain.sql in Supabase
# (brain.sql is idempotent — re-paste it after any schema change; it adds sd_theses and the
#  outcome / gate_reason / thesis_* columns the dashboard splits on. Substitute the ingest key.)
crontab -l > /root/crontab-backup-$(date +%Y%m%d-%H%M%S).txt      # always back up first
crontab -l | cat - /root/trading-brain/vps/crontab.txt | crontab -
```

`vps/crontab.txt` is UTC and offers each anchor twice (see **When it runs**). Runs alongside the
existing stock/futures engines — staggered cron minutes, nothing shared touched.
