#!/usr/bin/env bash
# Market Brain — Oracle VPS setup. Idempotent; safe to re-run.
#
#   sudo bash vps/setup.sh
#
# Prereqs already on the VPS from the stock engine: /root/.secrets/signal.env (GMAIL_* +
# SUPABASE_* keys), root crontab, /root/signal-deck-logs/. This script is ADDITIVE — it never
# touches futures_signal / stock_signal or their crons.
#
# One-time USER steps this script cannot do (it will remind you):
#   1. `claude setup-token`  — authenticate the VPS to Lind's Claude subscription (paste the code).
#   2. Add FINNHUB_API_KEY to /root/.secrets/signal.env  (free key from finnhub.io).
#   3. Paste vps/brain.sql into the Supabase SQL editor.
set -euo pipefail

REPO_DIR="${BRAIN_REPO_DIR:-/root/trading-brain}"
REPO_URL="${BRAIN_REPO_URL:-https://github.com/lindsylqa/trading-brain.git}"
SECRETS="/root/.secrets/signal.env"
LOG_DIR="/root/signal-deck-logs"
VAULT="${LIND_BRAIN:-/home/ubuntu/.hermes/syncthing-sync/lind-brain}"

echo "== Market Brain VPS setup =="

# ── 1. Node 20 + Claude Code CLI ────────────────────────────────────────────────
if ! command -v node >/dev/null 2>&1 || [ "$(node -v | cut -c2-3)" -lt 20 ]; then
  echo "-- installing Node 20"
  curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
  apt-get install -y nodejs
fi
if ! command -v claude >/dev/null 2>&1; then
  echo "-- installing @anthropic-ai/claude-code"
  npm install -g @anthropic-ai/claude-code
fi
echo "node $(node -v) / claude $(claude --version 2>/dev/null || echo '?')"

# ── 2. Clone / update the repo ───────────────────────────────────────────────────
if [ -d "$REPO_DIR/.git" ]; then
  echo "-- updating $REPO_DIR"
  git -C "$REPO_DIR" pull --rebase --autostash || true
else
  echo "-- cloning into $REPO_DIR"
  git clone "$REPO_URL" "$REPO_DIR"
fi

# ── 3. Python venv + deps ────────────────────────────────────────────────────────
cd "$REPO_DIR"
if [ ! -d .venv ]; then
  # `python3 -m venv` needs the python3-venv package with ensurepip — the VPS's system
  # python3 is minimal (the stock engine hit "no pip" here). Install it before venv creation
  # or this aborts under `set -e`. Idempotent: only runs on first setup (no .venv yet).
  apt-get install -y python3-venv python3-pip
  python3 -m venv .venv
fi
./.venv/bin/pip install --quiet --upgrade pip
./.venv/bin/pip install --quiet -r requirements.txt
echo "-- venv ready: $(./.venv/bin/python --version)"

mkdir -p "$LOG_DIR"

# ── 4. Brain-specific env (append once; never duplicate) ────────────────────────
touch "$SECRETS"
add_env() {  # add_env KEY value  — only if KEY not already present
  local key="$1" val="$2"
  if ! grep -q "^export ${key}=" "$SECRETS" 2>/dev/null; then
    echo "export ${key}=${val}" >> "$SECRETS"
    echo "   + added ${key}"
  fi
}
add_env LIND_BRAIN "\"$VAULT\""
add_env BRAIN_MEMORY_DIR "\"$REPO_DIR/brain-memory\""
add_env CLAUDE_BIN "\"$(command -v claude)\""
echo "-- env additions done ($SECRETS)"

# ── 5. Reminders for the manual steps ────────────────────────────────────────────
echo
echo "== NEXT (manual, one-time) =="
grep -q "^export FINNHUB_API_KEY=" "$SECRETS" \
  && echo "  [ok] FINNHUB_API_KEY present" \
  || echo "  [ ] add FINNHUB_API_KEY to $SECRETS (free key: finnhub.io)"
echo "  [ ] run:  claude setup-token   (as the user who owns the subscription; paste the code)"
echo "  [ ] paste vps/brain.sql into the Supabase SQL editor"
echo "  [ ] install cron lines:  crontab -l | cat - $REPO_DIR/vps/crontab.txt | crontab -"
echo
echo "Smoke test once the above are done:"
echo "  cd $REPO_DIR && . $SECRETS && ./.venv/bin/python market_brain.py --anchor mid --dry-run"
echo "Done."
