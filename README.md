# Market Brain — Signal Deck v2

A 24/7 AI trading-**analysis** engine. It watches the US equity market, and when something
technically or fundamentally interesting happens, it asks Claude (Opus, via the Claude Code
subscription) for a disciplined read — then emails Lind detailed trade ideas across three
horizons and books them into a simulated $10,000 paper portfolio.

> **Paper trading only. No broker is connected and no real orders are ever placed. Educational
> analysis from delayed data — not financial advice.**

## How it works

```
collect (free)            screen (free, Python)          deep run (Claude Code subscription)
yfinance bars + patterns  score vs thresholds:           only if the screener fires OR at an anchor:
Google News / Finnhub     breakout+vol, gap, RSI edge,   `claude -p --model opus` over a data packet
ForexFactory calendar     event <2h out, news on a held  → strict JSON: signals[] (type/why/indicators/
Finnhub fundamentals      name → escalate, else log&exit    chart/news/analog/sources), portfolio_actions[]
                                                          → gates enforced in code → email + Obsidian
                                                            + Supabase dashboard + git-committed memory
```

- **Hybrid & cheap.** A deterministic Python screener runs every 30 min for $0. The paid-attention
  deep run (Opus) only fires when the screener escalates or at 3 daily anchors (pre-market, midday,
  close). No Anthropic API key — Lind is region-ineligible, so the engine shells out to the
  authenticated `claude` CLI on his subscription.
- **Pattern + news memory.** Every pattern/event is logged to `brain-memory/EVENT-LOG.jsonl` with a
  market snapshot; later cycles backfill 1h/4h/1d/5d forward returns, building a historical-analog
  database Claude cites ("last 6 hot-CPI prints → avg −0.8% SPY over 4h").
- **Discipline in code.** `brain/portfolio.py` enforces max 6 positions, ≤20%/name, ≤3 new
  trades/week, −7% hard stop, a tightening trailing stop, and sector lockouts. Claude only proposes;
  the code disposes.

## Layout

| Path | What |
|---|---|
| `market_brain.py` | CLI orchestrator (`--cycle`, `--anchor pre\|mid\|close`, `--research T`, `--weekly-review`, `--digest`, `--dry-run`) |
| `brain/collect.py` | bars, indicators, chart-pattern detectors, news, ForexFactory, Finnhub |
| `brain/screen.py` | deterministic 30-min screener (no LLM) |
| `brain/deep.py` | builds the packet + prompt, invokes `claude -p`, parses JSON, fallback |
| `brain/portfolio.py` | paper-portfolio gates + mechanical exits |
| `brain/notify.py` | detailed HTML email (scalp / short-term / long-term cards) |
| `brain/obsidian.py` | writes every run to the Obsidian second brain |
| `brain/supabase.py` | dashboard feed |
| `brain/memory.py` | EVENT-LOG + historical analogs + git-as-memory |
| `prompts/*.md` | per-mode deep-run prompt templates |
| `brain-memory/` | **tracked** state: portfolio, event log, logs (committed each run) |
| `vps/` | `setup.sh`, `crontab.txt`, `brain.sql` |
| `CLAUDE.md` | the analyst rulebook (auto-loads when `claude -p` runs here) |

## Local quick start

```bash
pip install -r requirements.txt
python market_brain.py --anchor mid --dry-run     # full pipeline, no email/dashboard/push
python market_brain.py --research NVDA --dry-run   # single-ticker deep dive
python -m pytest tests/ -q                          # portfolio-gate unit tests
```

`--dry-run` prints the screener verdict and the email that *would* send. `--no-claude` skips the
subscription call and uses the deterministic fallback (handy with no `claude` CLI available).

## Configuration (all via env; every key is optional)

| Env var | Purpose | Without it |
|---|---|---|
| `GMAIL_USER`, `GMAIL_APP_PASSWORD` | send the email | no email sent |
| `FINNHUB_API_KEY` | fundamentals + company news (finnhub.io free) | fundamentals section empty |
| `LIND_BRAIN` | Obsidian vault root | no vault write |
| `SUPABASE_URL`, `SUPABASE_ANON_KEY`, `SIGNAL_INGEST_KEY` | dashboard feed | no dashboard push |
| `BRAIN_DEEP_MODEL` (default `opus`), `CLAUDE_BIN` (default `claude`) | deep-run model / CLI path | — |

See `brain/config.py` for the full list (thresholds, focus tickers, portfolio limits).

## Deploy (Oracle VPS)

```bash
sudo bash vps/setup.sh          # Node + claude-code, clone, venv, env additions
claude setup-token              # USER — authenticate the subscription (paste the code)
# add FINNHUB_API_KEY to /root/.secrets/signal.env; paste vps/brain.sql in Supabase
crontab -l | cat - /root/trading-brain/vps/crontab.txt | crontab -
```

Runs alongside the existing stock/futures engines (staggered cron minutes; nothing shared touched).
