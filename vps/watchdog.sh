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
rows=""
stale=0

human() {   # minutes -> something a person reads at a glance
  # "8888m" is a number you have to do arithmetic on before it means anything, and this is the
  # first thing on the page. Six days should say six days.
  local m="$1"
  if   [ "$m" -ge 1440 ]; then echo "$((m / 1440))d $(( (m % 1440) / 60 ))h"
  elif [ "$m" -ge 60 ];   then echo "$((m / 60))h $((m % 60))m"
  else                         echo "${m}m"
  fi
}

check() {   # name -> appends to $detail, sets $stale when past the limit
  # Three statements, not one. Bash expands every word on a command line before `local` assigns
  # any of them, so `local name="$1" p="$LOGD/$name.log"` expands $name while it is still unset —
  # which under `set -u` aborts the script on line one of its only real function.
  local name="$1"
  local limit="$2"
  # Only the decisive log can raise the alarm. The others are printed for context, and a missing
  # or rotated context log must never be the reason an alert fires — a false page from the
  # watchdog costs more than it sounds, because the first one you ignore is the one that was real.
  local decisive="${3:-0}"
  local p="$LOGD/$name.log"
  if [ ! -f "$p" ]; then
    detail="${detail}  ${name}.log: MISSING"$'\n'
    rows="${rows}${name}.log	missing	never written	${decisive}"$'\n'
    [ "$decisive" = "1" ] && stale=1
    return 0
  fi
  local age=$(( (now - $(stat -c %Y "$p")) / 60 ))
  local hum
  hum=$(human "$age")
  detail="${detail}  ${name}.log: ${hum} old ($(stat -c %y "$p" | cut -d. -f1))"$'\n'
  # Same facts, tab-separated, for the HTML renderer to lay out as label/value rows. Built here
  # rather than parsed back out of `detail`, so the two views can never disagree about a number.
  rows="${rows}${name}.log	${hum} ago	$(stat -c %y "$p" | cut -d. -f1)	${decisive}"$'\n'
  [ "$decisive" = "1" ] && [ "$age" -gt "$limit" ] && stale=1
  return 0
}

# The crypto log is the ungated one and carries the verdict. The others are reported for context
# only — a stale brain-cycle.log at the weekend is correct behaviour, not a fault.
check brain-crypto "$MAX_MIN" 1
check brain-cycle 0 0
check brain-anchor 0 0

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

SUBJ="Market Brain: no cycle in $(human "$MAX_MIN") — pipeline may be down"
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

# The heredoc is QUOTED (<<'PY') so the Python below is literal source. It used to be unquoted,
# which meant bash interpolated $BODY — twenty lines of log output — straight into a Python string
# literal. Those lines carry dict reprs, quotes and backslashes, so a stray sequence broke the
# parse, and a backtick or $(...) in a log line would have been executed by the shell. The body
# now travels as data in the environment, where none of it is ever parsed as code.
#
# Credentials are passed explicitly rather than inherited, so this works whether signal.env
# exports its variables or merely assigns them — and a missing one exits with a sentence instead
# of a KeyError traceback, which matters for the one script whose whole job is to not fail quietly.
WATCHDOG_BODY="$BODY" WATCHDOG_TO="$TO" WATCHDOG_SUBJ="$SUBJ" \
WATCHDOG_ROWS="$rows" WATCHDOG_TAIL="$tail_lines" WATCHDOG_CRON="$cron_n" \
WATCHDOG_LOCKS="${locks:-}" WATCHDOG_TEST="$TEST_MODE" WATCHDOG_BRAIN="$BRAIN" \
GMAIL_USER="${GMAIL_USER:-}" GMAIL_APP_PASSWORD="${GMAIL_APP_PASSWORD:-}" \
python3 - <<'PY'
# The HTML is composed here rather than in shell for one reason: log lines are attacker-adjacent
# text — they contain angle brackets, ampersands and quotes — and html.escape is the only honest
# way to put them in a document. Building the markup in bash would mean quoting them by hand.
import html as H
import os
import smtplib
import sys
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

user = os.environ.get("GMAIL_USER")
pw = os.environ.get("GMAIL_APP_PASSWORD")
if not user or not pw:
    sys.exit("GMAIL_USER / GMAIL_APP_PASSWORD not visible to python3")

e = H.escape
rows = [ln.split("\t") for ln in os.environ.get("WATCHDOG_ROWS", "").splitlines() if ln.strip()]
tail = os.environ.get("WATCHDOG_TAIL", "").strip()
cron_n = os.environ.get("WATCHDOG_CRON", "?")
locks = os.environ.get("WATCHDOG_LOCKS", "").strip()
is_test = os.environ.get("WATCHDOG_TEST") == "1"
brain = os.environ.get("WATCHDOG_BRAIN", "/root/trading-brain")

# The one number worth reading first: how long the decisive log has been silent. The rest of the
# email is evidence for it. Same role the return figure plays at the top of the weekly recap.
lead = next((r for r in rows if len(r) > 3 and r[3] == "1"), None)
headline = lead[1] if lead else "unknown"
accent = "#f59e0b" if is_test else "#ef4444"

row_html = "".join(
    '<tr>'
    f'<td style="padding:5px 0;color:#9ca3af;font-size:13px;vertical-align:top;">{e(r[0])}</td>'
    f'<td style="padding:5px 0;font-size:13px;text-align:right;padding-left:16px;'
    f'color:{accent if len(r) > 3 and r[3] == "1" else "#e5e7eb"};">{e(r[1])}'
    f'<span style="color:#6b7280;"> · {e(r[2])}</span></td></tr>'
    for r in rows if len(r) >= 3)

def section(title, body, color="#60a5fa"):
    return (f'<p style="margin:18px 0 6px;font-size:11px;font-weight:700;letter-spacing:0.08em;'
            f'text-transform:uppercase;color:{color};">{title}</p>{body}')

# Monospace earns its place here: this is literal log output, where column alignment and exact
# characters are the content. It is not a costume for "technical".
tail_html = (
    f'<pre style="margin:0;padding:14px 16px;background:#0a0a0a;border:1px solid #ffffff14;'
    f'border-radius:10px;overflow-x:auto;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,'
    f'monospace;font-size:11px;line-height:1.55;color:#9ca3af;white-space:pre-wrap;'
    f'word-break:break-word;">{e(tail) or "(no log output)"}</pre>')

locks_html = (f'<p style="margin:0;font-size:13px;color:#fca5a5;">{e(locks)}</p>'
              if locks else
              '<p style="margin:0;font-size:13px;color:#6b7280;">None held.</p>')

kicker = ("Market Brain · Watchdog · Test send" if is_test
          else "Market Brain · Watchdog · Automated alert")
title = "Watchdog test" if is_test else "The brain has stopped reporting"
strap = ("This is a manual test send. Nothing is wrong — you asked for it."
         if is_test else
         "No cycle has been recorded recently. The crypto cycle is ungated and runs hourly, so a "
         "stale timestamp here means the scheduler stopped rather than the market being quiet.")

html_doc = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#0a0a0a;font-family:-apple-system,'Segoe UI',Helvetica,Arial,sans-serif;color:#e5e7eb;">
<table width="100%" cellpadding="0" cellspacing="0"><tr><td align="center" style="padding:32px 16px;">
<table width="560" cellpadding="0" cellspacing="0" style="max-width:560px;width:100%;">
  <tr><td style="padding:0 0 18px;">
    <p style="margin:0;font-size:11px;color:#6b7280;text-transform:uppercase;letter-spacing:0.1em;">{kicker}</p>
    <h1 style="margin:8px 0 0;font-size:26px;font-weight:800;color:#fff;">{title}</h1>
  </td></tr>

  <tr><td style="background:#0f172a;border:1px solid #1e293b;border-radius:14px;padding:22px 26px;">
    <p style="margin:0;font-size:34px;font-weight:800;color:{accent};">{e(headline)}</p>
    <p style="margin:2px 0 14px;font-size:13px;color:#9ca3af;line-height:1.6;">{strap}</p>
    <table width="100%" cellpadding="0" cellspacing="0">{row_html}</table>
  </td></tr>
  <tr><td style="height:16px;"></td></tr>

  <tr><td style="background:#111;border:1px solid #ffffff14;border-radius:14px;padding:22px 26px;">
    {section("Scheduler", f'<table width="100%" cellpadding="0" cellspacing="0"><tr><td style="padding:5px 0;color:#9ca3af;font-size:13px;">market_brain cron entries</td><td style="padding:5px 0;font-size:13px;text-align:right;color:{"#ef4444" if cron_n in ("0", "?") else "#e5e7eb"};">{e(cron_n)}</td></tr></table>')}
    {section("Held locks", locks_html, "#f59e0b")}
    {section("Last lines of brain-crypto.log", tail_html, "#a78bfa")}
  </td></tr>
  <tr><td style="height:16px;"></td></tr>

  <tr><td style="background:#0b1220;border:1px solid #60a5fa44;border-radius:14px;padding:20px 26px;">
    {section("What to run", f'<p style="margin:0;font-size:13px;color:#d1d5db;line-height:1.7;"><code style="font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12px;color:#93c5fd;">bash {e(brain)}/vps/healthcheck.sh</code><br><span style="color:#9ca3af;">Ten checks in order — the first failure explains the rest.</span></p>')}
  </td></tr>
  <tr><td style="height:16px;"></td></tr>

  <tr><td style="padding:8px 4px;font-size:11px;color:#4b5563;text-align:center;line-height:1.6;">
    Sent by the watchdog cron, which runs independently of the brain so it survives whatever
    stops it. At most one alert per day.</td></tr>
</table></td></tr></table>
</body></html>"""

msg = MIMEMultipart("alternative")
msg["Subject"] = os.environ.get("WATCHDOG_SUBJ", "Market Brain watchdog")
msg["From"], msg["To"] = user, os.environ["WATCHDOG_TO"]
# Plain part first: it is the fallback, and it is what a phone lock screen previews.
msg.attach(MIMEText(os.environ.get("WATCHDOG_BODY", "(no body)"), "plain", "utf-8"))
msg.attach(MIMEText(html_doc, "html", "utf-8"))
try:
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
        s.login(user, pw)
        s.send_message(msg)
except Exception as ex:
    sys.exit(f"SMTP failed: {type(ex).__name__}: {ex}")
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
