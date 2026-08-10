#!/usr/bin/env bash
# Market Brain — install (or re-install) the brain's cron block. Idempotent.
#
# The instruction in the header of vps/crontab.txt is:
#
#     crontab -l | cat - /root/trading-brain/vps/crontab.txt | crontab -
#
# which *appends*. Run it twice and every job is scheduled twice: two deep runs per tick, two
# emails, two sets of proposed trades off one session. It also cannot update anything — when the
# crontab.txt in the repo gains a line, as it did with the three digest ticks, there is no way to
# pick that up short of hand-editing `crontab -e`. The box was found on 2026-08-10 running an
# older crontab for exactly that reason.
#
# So this replaces rather than appends. It fences the brain's lines between sentinel comments,
# strips anything it previously wrote, also strips legacy un-fenced market_brain lines from the
# old append-style installs, and leaves every unrelated cron entry (the stock and futures signal
# engines) untouched.
#
#   bash /root/trading-brain/vps/install-cron.sh          # install / update
#   bash /root/trading-brain/vps/install-cron.sh --dry-run # print the result, change nothing
set -euo pipefail

BRAIN="${BRAIN:-/root/trading-brain}"
SRC="$BRAIN/vps/crontab.txt"
BEGIN="# >>> market-brain (managed by vps/install-cron.sh — do not edit between sentinels) >>>"
END="# <<< market-brain <<<"
DRY=0
[ "${1:-}" = "--dry-run" ] && DRY=1

[ -f "$SRC" ] || { echo "error: $SRC not found" >&2; exit 1; }

BACKUP="/root/crontab.backup-$(date +%Y%m%d-%H%M%S)"
crontab -l > "$BACKUP" 2>/dev/null || : > "$BACKUP"
echo "existing crontab backed up to $BACKUP ($(wc -l < "$BACKUP") lines)"

# Everything that is NOT ours: drop our fenced block, then drop any stray market_brain or
# watchdog line left over from an append-style install. The signal engines survive both passes
# because neither pattern matches them.
KEPT=$(awk -v b="$BEGIN" -v e="$END" '
  index($0, "# >>> market-brain") == 1 { skip = 1; next }
  index($0, "# <<< market-brain") == 1 { skip = 0; next }
  !skip { print }
' "$BACKUP" | grep -vE 'market_brain\.py|vps/watchdog\.sh' || true)

NEW=$(printf '%s\n%s\n%s\n%s\n' "$KEPT" "$BEGIN" "$(cat "$SRC")" "$END")

if [ "$DRY" -eq 1 ]; then
  echo "--- crontab that WOULD be installed ---"
  printf '%s\n' "$NEW"
  exit 0
fi

printf '%s\n' "$NEW" | crontab -

# Verify against what actually landed, not against what we meant to install.
INSTALLED=$(crontab -l 2>/dev/null | grep -c 'market_brain\.py' || true)
WANTED=$(grep -c 'market_brain\.py' "$SRC" || true)
DUPES=$(crontab -l 2>/dev/null | grep -E 'market_brain\.py|watchdog\.sh' | sort | uniq -d | wc -l)
WD=$(crontab -l 2>/dev/null | grep -c 'watchdog\.sh' || true)

echo "installed: $INSTALLED market_brain entries (crontab.txt defines $WANTED), watchdog=$WD, duplicates=$DUPES"
if [ "$INSTALLED" -ne "$WANTED" ] || [ "$DUPES" -ne 0 ]; then
  echo "MISMATCH — restore with:  crontab $BACKUP" >&2
  exit 1
fi
echo "OK. Next ticks:"
crontab -l | grep -E 'market_brain\.py|watchdog\.sh' | awk '{printf "  %s %s %s %s %s\n", $1,$2,$3,$4,$5}' | sort -u
