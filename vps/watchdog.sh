#!/usr/bin/env bash
# Market Brain — dead-man's switch.
#
# On 2026-08-10 Lind noticed the brain was stale. It had actually stopped on 2026-08-04: six days
# of no scans, no news pass, no signals, and not one thing anywhere said so. Every other alert in
# this system is *produced by* the pipeline, so when the pipeline dies the alerts die with it.
# That is the hole this fills, and why it is written to share as little as possible with what it
# watches: plain shell, system python for SMTP, its own cron entry, its own lock, no `brain`
# import, no venv. If the venv is what broke, this still runs.
#
#   bash /root/trading-brain/vps/watchdog.sh          # normal (cron)
#   bash /root/trading-brain/vps/watchdog.sh --test   # send now regardless of freshness
#
# The liveness signal is the mtime of the crypto cycle log. That job is the right one to watch
# because it is the only one with no gate: `--crypto-cycle` routes around market_hours and runs
# hourly, every hour, weekends and holidays included. So "older than an hour or two" means the
# scheduler stopped — with no session, DST or holiday reasoning needed to interpret it.
set -uo pipefail

BRAIN="${BRAIN:-/root/trading-brain}"
LOGD="${LOGD:-/root/signal-deck-logs}"
ENVF="${ENVF:-/root/.secrets/signal.env}"
STATE="${STATE:-/root/.brain-watchdog.state}"
# The crypto cycle is hourly, so 150 minutes is two missed ticks plus slack for a slow run. Long
# enough that one late cron tick is not an alert; short enough to catch a real stop the same
# morning rather than the next week.
MAX_MIN="${BRAIN_WATCHDOG_MAX_MIN:-150}"

[ -f "$ENVF" ] && . "$ENVF" 2>/dev/null
TO="${BRAIN_RECIPIENT:-lindsylqa@gmail.com}"
TEST_MODE=0
[ "${1:-}" = "--test" ] && TEST_MODE=1

now=$(date +%s)
detail=""
stale=0

check() {   # name -> appends to $detail, sets $stale when past the limit
  local name="$1" limit="$2" p="$LOGD/$name.log"
  if [ ! -f "$p" ]; then
    detail="${detail}  ${name}.log: MISSING"$'\n'
    stale=1
    return
  fi
  local age=$(( (now - $(stat -c %Y "$p")) / 60 ))
  detail="${detail}  ${name}.log: ${age}m old ($(stat -c %y "$p" | cut -d. -f1))"$'\n'
  [ "$age" -gt "$limit" ] && stale=1
}

# The crypto log is the ungated one and carries the verdict. The others are reported for context
# only — a stale brain-cycle.log at the weekend is correct behaviour, not a fault.
check brain-crypto "$MAX_MIN"
check brain-cycle 100000
check brain-anchor 100000

if [ "$stale" -eq 0 ] && [ "$TEST_MODE" -eq 0 ]; then
  exit 0
fi

# One alert per day. A six-day outage should produce six emails, not two thousand — and the day
# stamp resets on its own, so a fixed pipeline needs no cleanup here.
today=$(date +%F)
if [ "$TEST_MODE" -eq 0 ] && [ "$(cat "$STATE" 2>/dev/null)" = "$today" ]; then
  exit 0
fi

tail_lines=$(tail -20 "$LOGD/brain-crypto.log" 2>/dev/null || echo "(no crypto log)")
cron_n=$(crontab -l 2>/dev/null | grep -c market_brain || true)
locks=$(for l in /tmp/brain-*.lock; do [ -e "$l" ] || continue
          h=$(fuser "$l" 2>/dev/null | tr -d ' '); [ -n "$h" ] && echo "  $l HELD by $h"; done)

SUBJ="Market Brain: no cycle in ${MAX_MIN}m — pipeline may be down"
[ "$TEST_MODE" -eq 1 ] && SUBJ="Market Brain: watchdog test"

BODY="The Market Brain watchdog did not see a recent run.

Log freshness:
${detail}
market_brain cron entries: ${cron_n}
Held locks:
${locks:-  none}

Last 20 lines of brain-crypto.log:
${tail_lines}

Diagnose with:
  bash ${BRAIN}/vps/healthcheck.sh

This alert is sent at most once a day. It is produced by a cron entry independent of the brain,
so it keeps working when the brain does not.
"

if [ -z "${GMAIL_USER:-}" ] || [ -z "${GMAIL_APP_PASSWORD:-}" ]; then
  # No credentials is itself worth recording — silently doing nothing is the failure mode this
  # whole script exists to end.
  echo "[watchdog] $(date -u '+%F %T') stale but GMAIL_USER/GMAIL_APP_PASSWORD unset; cannot email" \
    >> "$LOGD/brain-watchdog.log"
  exit 1
fi

python3 - "$TO" "$SUBJ" <<PY
import os, smtplib, sys
from email.mime.text import MIMEText
to, subj = sys.argv[1], sys.argv[2]
msg = MIMEText("""$BODY""")
msg["Subject"], msg["From"], msg["To"] = subj, os.environ["GMAIL_USER"], to
with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
    s.login(os.environ["GMAIL_USER"], os.environ["GMAIL_APP_PASSWORD"])
    s.send_message(msg)
print("sent")
PY
rc=$?

if [ "$rc" -eq 0 ]; then
  [ "$TEST_MODE" -eq 0 ] && echo "$today" > "$STATE"
  echo "[watchdog] $(date -u '+%F %T') alert sent to $TO" >> "$LOGD/brain-watchdog.log"
else
  echo "[watchdog] $(date -u '+%F %T') SMTP send failed rc=$rc" >> "$LOGD/brain-watchdog.log"
fi
exit "$rc"
