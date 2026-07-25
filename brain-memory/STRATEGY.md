# Strategy — standing rules of the Market Brain

The durable "how this account trades" doc. The analyst reads the machine-enforced version in
`../CLAUDE.md`; this is the plain-English charter for Lind, and the place to record deliberate
strategy changes over time (append dated notes at the bottom — never rewrite history).

## Objective

Grow a simulated $10,000 account through **disciplined, well-reasoned** paper trades across three
horizons — scalps, short-term swings, and long-term investments — while teaching Lind *why* each
trade is taken. Process quality matters more than any single week's P&L.

## Non-negotiables

1. **Paper only — no real money, ever.** The $10,000 ledger in `PORTFOLIO.json` is the accounting
   truth. Approved trades are also mirrored into an **Alpaca paper account** so the fills are real
   fills against fake money; `brain/broker.py` hardcodes the paper host and re-verifies it before
   every request, so a live host or a missing key fails closed and the run continues on the
   simulation alone. No code path in this repo can reach a real-money broker.
2. **Rules over conviction.** The code enforces the risk gates; the analyst proposes within them.
3. **Selectivity.** No trade is the default. Force nothing.
4. **Honesty.** Real indicator values and real headlines only — never fabricated. Losses are
   logged and learned from, not hidden.
5. **Every trade is journaled and graded.** Entry reasoning goes into the Obsidian trade journal;
   the exit is graded against it in code; Friday's review turns the gradings into lessons appended
   below and fed back into every later analysis run.

## Risk gates (enforced in `brain/portfolio.py`)

- Max **6** open positions; **≤20%** equity per name.
- **≤3** new trades per week.
- **−7%** hard stop (mechanical). Trailing stop **10% → 7% at +15% → 5% at +20%**.
- Sector lockout after **2** consecutive losers in that sector.

## Trade horizons

- ⚡ **Scalp** — minutes to hours, closed intraday. Momentum/mean-reversion on 5–15m structure.
- 📈 **Short-term** — days to weeks. Daily-chart swing with a defined invalidation.
- 🏦 **Long-term** — months+. Trend + supportive fundamentals (revenue/EPS growth).

## Edge sources

- Deterministic chart-pattern detection (breakouts on volume, gaps, MA crosses, RSI/MACD
  divergence, Bollinger squeeze, 52-week proximity, pivot clusters).
- **Two-tier news pipeline.** Haiku 4.5 reads the headlines every 30 minutes (cheap, always on) and
  scores per-ticker materiality; Opus 5 only spends a deep run when the screener or Haiku escalates.
- News + ForexFactory macro calendar (CPI/FOMC/NFP + Trump/Fed speeches).
- **Historical-analog memory** (`EVENT-LOG.jsonl`) — forward returns of repeating events build up
  over time into "last N times this fired, price did X."
- Fundamentals (Finnhub) for the long-term case.

---

## Change log

- _(seed)_ Initial charter. $10k paper account, hybrid screener + Opus deep runs on Lind's Claude
  Code subscription, 3 daily anchors + event-driven escalations.
