#!/usr/bin/env bash
# Rotate SIGNAL_INGEST_KEY in /root/.secrets/signal.env.
#
# The ingest key is the write credential for every dashboard table — the brain's
# (sd_brain_scans / sd_brain_signals / sd_portfolio / sd_trades) and the stock engine's
# (sd_scans / sd_signals). Both read it from this one env file, so rotating here covers both
# engines at once.
#
# Rotation is only half the job: the new key must also be written into the Supabase policies,
# or every insert starts failing with 42501. Run this, then immediately:
#
#     bash vps/render_ingest_policy.sh      # paste the output into the Supabase SQL editor
#
# Prints no secret. The previous file is kept as signal.env.bak-<timestamp> so a bad rotation
# can be undone; delete those once the new key is confirmed working.
set -euo pipefail

ENV_FILE="${SIGNAL_ENV:-/root/.secrets/signal.env}"
[ -w "$ENV_FILE" ] || { echo "cannot write $ENV_FILE (run with sudo)" >&2; exit 1; }

BACKUP="${ENV_FILE}.bak-$(date -u +%Y%m%d-%H%M%S)"
cp -p "$ENV_FILE" "$BACKUP"
chmod 600 "$BACKUP"

NEW="sdk_$(openssl rand -hex 24)"

if grep -q '^export SIGNAL_INGEST_KEY=' "$ENV_FILE"; then
  # `sed s|...|` with a hex key is safe: the generated key has no delimiter characters in it.
  sed -i "s|^export SIGNAL_INGEST_KEY=.*|export SIGNAL_INGEST_KEY=${NEW}|" "$ENV_FILE"
else
  printf 'export SIGNAL_INGEST_KEY=%s\n' "$NEW" >> "$ENV_FILE"
fi

# Verify by shape, never by value.
# shellcheck disable=SC1090
LEN=$(. "$ENV_FILE"; printf '%s' "${#SIGNAL_INGEST_KEY}")
if [ "$LEN" -ne 52 ]; then
  cp -p "$BACKUP" "$ENV_FILE"
  echo "rotation produced a ${LEN}-char key, expected 52 — restored $BACKUP" >&2
  exit 1
fi

echo "rotated: SIGNAL_INGEST_KEY now ${LEN} chars"
echo "backup:  $BACKUP"
echo "next:    bash $(dirname "$0")/render_ingest_policy.sh  ->  paste into Supabase SQL editor"
