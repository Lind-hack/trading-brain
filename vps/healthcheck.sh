#!/usr/bin/env bash
# Market Brain — one-shot health check for the VPS.
#
# Written after the 2026-08-10 outage, where the brain had in fact been dead since 2026-08-04 and
# nothing anywhere said so. The dashboard was stale, the emails stopped, Claude usage read 0%, and
# the only way to tell the difference between "cron stopped", "the venv broke", "a lock is stuck"
# and "the model tiers are unreachable" was to go and look at five different things by hand.
#
# So this looks at all of them at once, in the order that matters: the first FAIL usually explains
# every symptom below it. Read it top to bottom and stop at the first one.
#
#   bash /root/trading-brain/vps/healthcheck.sh
#
# Read-only. It starts nothing, kills nothing and writes nothing except one temp file it removes.
# Safe to run mid-session.
set -uo pipefail          # deliberately NOT -e: a failing check must not stop the report

BRAIN="${BRAIN:-/root/trading-brain}"
LOGD="${LOGD:-/root/signal-deck-logs}"
ENVF="${ENVF:-/root/.secrets/signal.env}"
PY="${PY:-$BRAIN/.venv/bin/python}"
# How stale the cycle log may be before it counts as a failure. The cycle runs :03/:33 inside the
# session, so anything past ~90 minutes on a weekday means ticks are being missed.
STALE_MIN="${STALE_MIN:-90}"

pass() { printf '  \033[32mPASS\033[0m  %s\n' "$1"; }
fail() { printf '  \033[31mFAIL\033[0m  %s\n' "$1"; FAILED=$((FAILED + 1)); }
warn() { printf '  \033[33mWARN\033[0m  %s\n' "$1"; }
head_() { printf '\n\033[1m== %s ==\033[0m\n' "$1"; }
FAILED=0

printf '\033[1mMarket Brain health check\033[0m — %s on %s\n' "$(date -u '+%F %T UTC')" "$(hostname)"

# ── 1. cron: is the brain even scheduled? ───────────────────────────────────────
head_ "1. crontab"
CRON_LINES=$(crontab -l 2>/dev/null | grep -c 'market_brain' || true)
if [ "${CRON_LINES:-0}" -eq 0 ]; then
  fail "no market_brain entries in root's crontab — nothing is scheduled. This alone explains a total outage."
else
  pass "$CRON_LINES market_brain cron entries installed"
  # Duplicates are their own bug: the documented install command appends rather than replaces, so
  # running it twice doubles every job and every email.
  DUPES=$(crontab -l 2>/dev/null | grep 'market_brain' | sort | uniq -d | wc -l)
  [ "${DUPES:-0}" -gt 0 ] && warn "$DUPES duplicated cron line(s) — every affected job runs twice"
  CYC=$(crontab -l 2>/dev/null | grep -c -- '--cycle' || true)
  CRY=$(crontab -l 2>/dev/null | grep -c -- '--crypto-cycle' || true)
  DIG=$(crontab -l 2>/dev/null | grep -c -- '--digest' || true)
  printf '        cycle=%s crypto=%s digest=%s\n' "$CYC" "$CRY" "$DIG"
  [ "${DIG:-0}" -lt 5 ] && warn "only $DIG digest ticks (crontab.txt ships 5) — the box is on an older crontab"
fi

head_ "2. cron daemon"
if systemctl is-active --quiet cron 2>/dev/null || systemctl is-active --quiet crond 2>/dev/null; then
  pass "cron daemon is active"
else
  fail "cron daemon is NOT active — nothing scheduled can fire"
fi
printf '        recent cron syslog:\n'
grep -h 'CRON' /var/log/syslog 2>/dev/null | tail -3 | sed 's/^/          /' || printf '          (no syslog access)\n'

# ── 3. liveness: the log mtimes are the ground truth ────────────────────────────
# Every tick appends here — including a gated-out one, including one where Claude fails. So a
# stale mtime separates "cron stopped" from "the analysis was quiet", which no other signal does.
head_ "3. log freshness (the real liveness signal)"
if [ ! -d "$LOGD" ]; then
  fail "$LOGD does not exist"
else
  for f in brain-cycle brain-crypto brain-anchor brain-weekly brain-digest; do
    p="$LOGD/$f.log"
    if [ ! -f "$p" ]; then
      warn "$f.log missing (never run, or logs rotated)"
      continue
    fi
    age_min=$(( ( $(date +%s) - $(stat -c %Y "$p") ) / 60 ))
    msg="$f.log last written ${age_min}m ago ($(stat -c %y "$p" | cut -d. -f1))"
    if [ "$f" = "brain-cycle" ] || [ "$f" = "brain-crypto" ]; then
      if [ "$age_min" -gt "$STALE_MIN" ]; then fail "$msg"; else pass "$msg"; fi
    else
      printf '        %s\n' "$msg"
    fi
  done
fi

head_ "4. what the logs actually say"
for f in brain-cycle brain-crypto; do
  p="$LOGD/$f.log"
  [ -f "$p" ] || continue
  printf '\n  --- %s.log (last 25) ---\n' "$f"
  tail -25 "$p" | sed 's/^/    /'
done
printf '\n  --- error signatures across all brain logs ---\n'
grep -hoE '\[(gate|warn|err)\][^|]{0,110}' "$LOGD"/brain-*.log 2>/dev/null | sort | uniq -c | sort -rn | head -12 | sed 's/^/    /'
printf '  --- tracebacks ---\n'
grep -hA3 'Traceback' "$LOGD"/brain-*.log 2>/dev/null | tail -12 | sed 's/^/    /' || printf '    none\n'

# ── 5. stale locks ──────────────────────────────────────────────────────────────
# `flock -n` exits silently when the lock is held. A zombie holding one produces exactly the
# symptom seen on 2026-08-10: a clean stop with no error anywhere.
head_ "5. flock locks"
FOUND_LOCK=0
for l in /tmp/brain-*.lock; do
  [ -e "$l" ] || continue
  FOUND_LOCK=1
  holder=$(fuser "$l" 2>/dev/null | tr -d ' ')
  if [ -n "$holder" ]; then
    fail "$l is HELD by pid(s) $holder — every later tick of this job exits silently"
    ps -o pid,etime,cmd -p "$holder" 2>/dev/null | tail -n +2 | sed 's/^/        /'
  else
    pass "$l exists but is not held (normal)"
  fi
done
[ "$FOUND_LOCK" -eq 0 ] && printf '        no lock files present (normal if nothing has run since reboot)\n'

# ── 6. the interpreter ──────────────────────────────────────────────────────────
head_ "6. venv + imports"
if [ ! -x "$PY" ]; then
  fail "$PY missing or not executable — every cron job dies instantly at startup"
else
  pass "$PY exists"
  if err=$("$PY" -c "import sys; sys.path.insert(0,'$BRAIN'); import brain.config as c; print(c.CLAUDE_DEEP_MODEL, c.CLAUDE_LITE_MODEL)" 2>&1); then
    pass "brain.config imports — models: $err"
  else
    fail "brain.config will not import:"
    printf '%s\n' "$err" | tail -5 | sed 's/^/        /'
  fi
fi

head_ "7. disk + memory"
USE=$(df -P / | awk 'NR==2{gsub(/%/,"",$5); print $5}')
if [ "${USE:-0}" -ge 95 ]; then fail "root filesystem ${USE}% full"; else pass "root filesystem ${USE}% used"; fi
df -h / | tail -1 | sed 's/^/        /'
free -m 2>/dev/null | sed -n '2p' | sed 's/^/        /'

head_ "8. secrets file"
if [ ! -f "$ENVF" ]; then
  fail "$ENVF missing — cron jobs source it before every run"
else
  pass "$ENVF present"
  # shellcheck disable=SC1090
  . "$ENVF" 2>/dev/null
  # The Alpaca names matter: broker.py:48 enables the paper mirror only when BOTH
  # ALPACA_PAPER_KEY_ID and ALPACA_PAPER_SECRET_KEY are set. This check used to probe
  # ALPACA_API_KEY, a name that appears nowhere else in the project, and so reported a missing
  # key on a box where the mirror was working.
  for k in GMAIL_USER GMAIL_APP_PASSWORD ALPACA_PAPER_KEY_ID ALPACA_PAPER_SECRET_KEY LIND_BRAIN; do
    if [ -n "${!k:-}" ]; then printf '        %-22s set\n' "$k"; else warn "$k is EMPTY or unset"; fi
  done
fi

# ── 9. the Claude CLI, as the user cron actually runs as ────────────────────────
# The repo has no ANTHROPIC_API_KEY and cannot have one, so the subscription CLI is the only path
# to both tiers. Auth lives in the invoking user's home — if it was set up as `ubuntu`, root's
# cron cannot use it, and both Haiku and Opus fail while everything else looks healthy.
head_ "9. claude CLI (running as $(id -un))"
CLAUDE_BIN="${CLAUDE_BIN:-claude}"
if ! command -v "$CLAUDE_BIN" >/dev/null 2>&1; then
  fail "'$CLAUDE_BIN' not on PATH for $(id -un) — both model tiers are unreachable"
else
  pass "claude found at $(command -v "$CLAUDE_BIN") ($("$CLAUDE_BIN" --version 2>&1 | head -1))"
  printf '        probing Haiku (up to 60s)…\n'
  if out=$(timeout 60 "$CLAUDE_BIN" -p --model claude-haiku-4-5-20251001 --output-format json \
            <<< 'Reply with the single word OK.' 2>&1); then
    if grep -qi '"is_error"[[:space:]]*:[[:space:]]*true' <<< "$out"; then
      fail "CLI returned an error envelope — this is the auth/quota wall:"
      printf '%s\n' "$out" | head -4 | sed 's/^/        /'
    else
      pass "claude -p responded — the subscription works for $(id -un)"
    fi
  else
    fail "claude -p failed or timed out for $(id -un):"
    printf '%s\n' "$out" | head -4 | sed 's/^/        /'
  fi
fi

# ── 10. state files + the evidence the analyst reasons over ─────────────────────
head_ "10. brain state"
cd "$BRAIN" 2>/dev/null || { fail "cannot cd $BRAIN"; exit 1; }
for f in brain-memory/.run-log.json brain-memory/.deep-runs.json; do
  if [ -f "$f" ]; then
    printf '        %-34s %s\n' "$(basename "$f")" "$(stat -c %y "$f" | cut -d. -f1)"
    head -c 220 "$f" | tr -d '\n' | sed 's/^/          /'; printf '\n'
  else
    warn "$f missing — no deep run has ever been recorded on this box"
  fi
done
EV=brain-memory/EVENT-LOG.jsonl
if [ -f "$EV" ]; then
  n=$(wc -l < "$EV")
  # The third confidence leg ("support") needs an analog with a real sample. memory.analogs_for()
  # reads this file into the packet; if it is thin, no analog can be cited and 70-79 is the
  # structural ceiling for anything not resting on fundamentals.
  if [ "$n" -lt 50 ]; then
    warn "EVENT-LOG.jsonl has only $n events — historical analogs cannot carry a confidence leg yet"
  else
    pass "EVENT-LOG.jsonl has $n events"
  fi
else
  warn "EVENT-LOG.jsonl missing — the analog leg is unavailable"
fi
for L in PORTFOLIO PORTFOLIO_CRYPTO; do
  p="brain-memory/$L.json"
  [ -f "$p" ] && "$PY" -c "
import json,sys
d=json.load(open('$p'))
p=d.get('positions',{})
print('        $L: %d open, cash %.0f, updated %s' % (len(p), d.get('cash',0), d.get('updated')))
" 2>/dev/null
done

head_ "summary"
if [ "$FAILED" -eq 0 ]; then
  printf '  \033[32mno failures\033[0m — if the brain is still stale, the cause is above the infrastructure;\n'
  printf '  run:  %s market_brain.py --cycle --ignore-market-hours --dry-run\n' "$PY"
else
  printf '  \033[31m%d check(s) failed\033[0m — fix the FIRST one; the rest are usually downstream of it.\n' "$FAILED"
fi
exit 0
