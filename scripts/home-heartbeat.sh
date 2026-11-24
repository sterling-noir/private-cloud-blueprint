#!/usr/bin/env bash
# Phase 8a heartbeat. Lives on the server as ~/bin/home-heartbeat.sh, cron every 5 minutes.
#
#   1. Pings healthchecks.io ("home-server heartbeat"). If the pings stop (server off, frozen,
#      or the house internet down), healthchecks.io emails Owner - the one alert that works
#      when this box can't send anything itself.
#   2. Checks that ntfy and Uptime Kuma themselves are running. If not, it sends a /fail ping,
#      so the email also covers "the monitoring is broken".
#   3. Disk: ntfy alert at 85% full on / (cleared below 80%).
#   4. Memory: ntfy alert when under 800 MiB is available for 15 minutes (cleared above 1200).
#
# Config: ~/.config/monitoring/heartbeat.env (HC_HEARTBEAT_URL=https://hc-ping.com/...), plus the
# ntfy topic/token files written by ~/monitoring/install.sh. State: ~/.local/state/home-monitor.
set -uo pipefail
export PATH=/usr/local/bin:/usr/bin:/bin
CFG="$HOME/.config/monitoring"
STATE="$HOME/.local/state/home-monitor"
mkdir -p "$STATE"
HC_HEARTBEAT_URL=""
# shellcheck disable=SC1091
[ -r "$CFG/heartbeat.env" ] && . "$CFG/heartbeat.env"

ntfy() {  # title message priority tag
  local body
  body=$(jq -nc --arg t "$(cat "$CFG/topic")" --arg ti "$1" --arg m "$2" --argjson p "$3" --arg tag "$4" \
    '{topic:$t,title:$ti,message:$m,priority:$p,tags:[$tag]}')
  # The token goes to curl through a file descriptor (-H @file), never on its command line,
  # so it cannot be seen in the process list.
  curl -fsS -m 10 -o /dev/null -H @<(printf 'Authorization: Bearer %s\n' "$(<"$CFG/ntfy-token")") \
    -H "Content-Type: application/json" -d "$body" http://127.0.0.1:2586/ || true
}

problems=""
curl -fsS -m 5 -o /dev/null http://127.0.0.1:2586/v1/health || problems+="ntfy (phone alerts) is not answering. "
curl -fsS -m 5 -o /dev/null http://127.0.0.1:3001/ || problems+="Uptime Kuma is not answering. "

# Disk
use=$(df --output=pcent / | tail -1 | tr -dc 0-9)
left=$(df -h --output=avail / | tail -1 | tr -d ' ')
if [ "$use" -ge 85 ] && [ ! -e "$STATE/disk-alerted" ]; then
  ntfy "Server disk ${use}% full" "Only ${left} left on the server. Nothing breaks yet, but ask Claude this week to clean up (old Docker images, Element photos, logs)." 4 floppy_disk
  touch "$STATE/disk-alerted"
elif [ "$use" -lt 80 ] && [ -e "$STATE/disk-alerted" ]; then
  ntfy "Server disk back to ${use}%" "Disk space is fine again. Nothing to do." 2 white_check_mark
  rm -f "$STATE/disk-alerted"
fi

# Memory
avail=$(awk '/^MemAvailable:/ {print int($2/1024)}' /proc/meminfo)
low=$(cat "$STATE/mem-low-count" 2>/dev/null || echo 0)
if [ "$avail" -lt 800 ]; then
  low=$((low + 1)); echo "$low" >"$STATE/mem-low-count"
  if [ "$low" -eq 3 ]; then
    ntfy "Server memory low" "Only ${avail} MB of memory has been free for 15 minutes. A bridge may get killed and restart by itself. Ask Claude to check which container grew." 4 warning
    touch "$STATE/mem-alerted"
  fi
elif [ "$avail" -gt 1200 ]; then
  echo 0 >"$STATE/mem-low-count"
  if [ -e "$STATE/mem-alerted" ]; then
    ntfy "Server memory OK again" "${avail} MB free. Nothing to do." 2 white_check_mark
    rm -f "$STATE/mem-alerted"
  fi
fi

# Crash loops: a container that dies and restarts quickly never looks "down" to Uptime Kuma
# (found 2026-11-14: the iMessage bridge was OOM-killed every 2-3 min for hours). Count "die"
# events per container in the last 30 min; 3 or more = alert once, cleared when it's quiet again.
# (The nightly backup and the HA update stop each container only once.)
dies=$(docker events --since 30m --until 0s --filter type=container --filter event=die \
         --format '{{.Actor.Attributes.name}}' 2>/dev/null | sort | uniq -c)
for f in "$STATE"/loop-*; do   # clear old alerts
  [ -e "$f" ] || continue
  c=${f##*/loop-}
  if ! awk -v c="$c" '$2==c && $1>=1 {found=1} END {exit !found}' <<<"$dies"; then
    ntfy "$c is stable again" "No more restarts in the last 30 minutes. Nothing to do." 2 white_check_mark
    rm -f "$f"
  fi
done
while read -r n c; do
  [ -n "${c:-}" ] && [ "$n" -ge 3 ] || continue
  [ -e "$STATE/loop-$c" ] && continue
  oom=""
  [ "$(docker inspect -f '{{.State.OOMKilled}}' "$c" 2>/dev/null)" = true ] && oom=" It is running out of memory."
  ntfy "$c keeps restarting" "$c crashed and restarted $n times in the last 30 minutes.$oom It is up between crashes, so things work on and off. Ask Claude to look (docker logs $c)." 4 repeat
  touch "$STATE/loop-$c"
done <<<"$dies"

# Heartbeat (last, so it also reports the monitoring's own health)
if [ -n "$HC_HEARTBEAT_URL" ]; then
  if [ -n "$problems" ]; then
    curl -fsS -m 10 --retry 3 -o /dev/null --data-raw "$problems Ask Claude to look (docker compose -f ~/monitoring/compose.yml ps)." "$HC_HEARTBEAT_URL/fail" || true
  else
    curl -fsS -m 10 --retry 3 -o /dev/null "$HC_HEARTBEAT_URL" || true
  fi
fi
