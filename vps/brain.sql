-- Market Brain dashboard tables (Signal Deck "AI Brain" section)
-- Paste this whole file into the Supabase SQL editor of project ehluzgywbirnkmjlajrl once.
-- Same x-signal-key ingest pattern as the existing stock-engine dashboard tables:
--   * anon may INSERT only when the request carries header  x-signal-key = <ingest key>
--   * anon may SELECT freely (the public dashboard reads with the anon key)
--
-- The brain writes here best-effort (brain/supabase.py); a dashboard outage never breaks a cycle.

-- ── heartbeat: one row per brain run ────────────────────────────────────────────
create table if not exists public.sd_brain_scans (
  id          bigint generated always as identity primary key,
  ts          timestamptz not null default now(),
  mode        text,                 -- cycle | pre | mid | close | research | weekly | digest
  escalated   boolean,
  n_signals   int,
  outlook     text,
  screen_why  jsonb,
  degraded    boolean default false,
  created_at  timestamptz not null default now()
);

-- ── one row per AI signal ───────────────────────────────────────────────────────
create table if not exists public.sd_brain_signals (
  id                bigint generated always as identity primary key,
  ts                timestamptz not null default now(),
  mode              text,
  ticker            text,
  direction         text,           -- LONG | SHORT
  trade_type        text,           -- SCALP | SHORT_TERM | LONG_TERM
  holding_period    text,
  confidence        int,
  entry             double precision,
  stop              double precision,
  target1           double precision,
  target2           double precision,
  why               text,
  analysis_done     text,
  indicators        text,
  chart_read        text,
  news              jsonb,
  historical_analog text,
  data_sources      jsonb,
  created_at        timestamptz not null default now()
);

-- New fields from the two-model pipeline (safe to re-run on an existing table).
alter table public.sd_brain_signals add column if not exists confidence_rationale text;
alter table public.sd_brain_signals add column if not exists indicators_used jsonb;
alter table public.sd_brain_signals add column if not exists news_read text;

-- The verdict the gates reached on each idea. Without it the dashboard cannot tell a trade the
-- brain TOOK from one it merely recommended — they arrived in the same list and rendered the
-- same way, which is exactly the confusion Lind reported.
--   executed  the gates approved it and a paper fill exists in sd_trades
--   rejected  a discipline rule blocked it (gate_reason says which)
--   advisory  the model raised the idea but proposed no action on it
alter table public.sd_brain_signals add column if not exists outcome text;
alter table public.sd_brain_signals add column if not exists gate_reason text;
alter table public.sd_brain_signals add column if not exists proposed_action text;
alter table public.sd_brain_signals add column if not exists news_edge text;
alter table public.sd_brain_signals add column if not exists thesis_id text;
alter table public.sd_brain_signals add column if not exists thesis_theme text;

-- ── the long-term thesis board ──────────────────────────────────────────────────
-- One row per re-score (Monday digest, Friday recap), carrying the whole board as jsonb. The
-- dashboard reads the newest row only. Unlike everything else here the board is a persistent
-- worldview: cycles may hang evidence on it, but conviction moves twice a week and no oftener.
create table if not exists public.sd_theses (
  id          bigint generated always as identity primary key,
  ts          timestamptz not null default now(),
  mode        text,                 -- digest | weekly
  regime      text,                 -- the structural read that framed this re-score
  n_active    int,
  board       jsonb,                -- [{theme, driver, thesis, tickers, horizon, conviction, …}]
  created_at  timestamptz not null default now()
);

-- ── live trade tape: one row per paper fill / exit ───────────────────────────────
-- This is what makes trades appear on signal-deck the moment they happen, rather than only
-- inside the next portfolio snapshot. Mirrors brain-memory/PORTFOLIO.json, which stays the
-- source of truth. Simulated fills only — mirrored to an Alpaca PAPER account, never live.
create table if not exists public.sd_trades (
  id               bigint generated always as identity primary key,
  ts               timestamptz not null default now(),
  event            text not null,        -- OPEN | ADD | CLOSE
  ticker           text not null,
  sector           text,
  direction        text,                 -- LONG | SHORT
  trade_type       text,                 -- SCALP | SHORT_TERM | LONG_TERM
  holding_period   text,
  confidence       int,
  shares           double precision,
  entry            double precision,
  exit             double precision,
  stop             double precision,
  target           double precision,
  pnl_usd          double precision,
  pnl_pct          double precision,
  reason           text,                 -- why opened, or why closed
  thesis           text,
  indicators_used  jsonb,
  news             jsonb,
  broker_mirrored  boolean default false,
  broker_order_id  text,
  opened_at        timestamptz,
  closed_at        timestamptz,
  equity_after     double precision,
  created_at       timestamptz not null default now()
);

-- ── latest paper-portfolio snapshot (dashboard portfolio card) ──────────────────
create table if not exists public.sd_portfolio (
  id               bigint generated always as identity primary key,
  ts               timestamptz not null default now(),
  equity           double precision,
  cash             double precision,
  total_return_pct double precision,
  n_open           int,
  win_rate         double precision,
  positions        jsonb,
  created_at       timestamptz not null default now()
);

create index if not exists sd_brain_scans_ts_idx   on public.sd_brain_scans   (ts desc);
create index if not exists sd_brain_signals_ts_idx on public.sd_brain_signals (ts desc);
create index if not exists sd_portfolio_ts_idx      on public.sd_portfolio      (ts desc);
create index if not exists sd_trades_ts_idx         on public.sd_trades         (ts desc);
create index if not exists sd_trades_ticker_idx     on public.sd_trades         (ticker, ts desc);
create index if not exists sd_theses_ts_idx         on public.sd_theses         (ts desc);

-- ── RLS ─────────────────────────────────────────────────────────────────────────
alter table public.sd_brain_scans   enable row level security;
alter table public.sd_brain_signals enable row level security;
alter table public.sd_portfolio      enable row level security;
alter table public.sd_trades         enable row level security;
alter table public.sd_theses         enable row level security;

-- helper: the ingest key carried in the request header.
-- Substitute the real SIGNAL_INGEST_KEY when pasting this into the SQL editor — it is NOT
-- committed here, because this repo is pushed to GitHub and the key is a write credential.
create or replace function public._brain_has_ingest_key() returns boolean
language sql stable as $$
  select coalesce(
    (current_setting('request.headers', true)::json ->> 'x-signal-key'),
    ''
  ) = '<PASTE SIGNAL_INGEST_KEY HERE>'
$$;

do $$
declare t text;
begin
  foreach t in array array['sd_brain_scans','sd_brain_signals','sd_portfolio','sd_trades','sd_theses'] loop
    execute format('drop policy if exists "%s_read"   on public.%I;', t, t);
    execute format('drop policy if exists "%s_ingest" on public.%I;', t, t);
    -- public read for the dashboard
    execute format(
      'create policy "%s_read" on public.%I for select to anon, authenticated using (true);',
      t, t);
    -- insert only with the ingest key header
    execute format(
      'create policy "%s_ingest" on public.%I for insert to anon, authenticated with check (public._brain_has_ingest_key());',
      t, t);
  end loop;
end $$;
