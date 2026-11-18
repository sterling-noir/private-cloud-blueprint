#!/bin/bash
BB_HEX="43D45764:04D3"   # 100.64.0.20:1235 as it appears in /proc/net/tcp
LOG=$HOME/logs/imessage-watchdog.log
cd $HOME/matrix || exit 0
docker ps --format "{{.Names}}" | grep -q "^mautrix-imessage$" || exit 0
# If the Mac itself is unreachable, restarting would be pointless; just note it.
if ! curl -s -m 5 -o /dev/null http://100.64.0.20:1235/api/v1/ping; then
  echo "$(date -Is) Mac unreachable, skipping" >> "$LOG"; exit 0
fi
if docker exec mautrix-imessage sh -c 'awk "NR>1 && \$4==\"01\" {print \$3}" /proc/net/tcp' 2>/dev/null | grep -q "$BB_HEX"; then
  exit 0   # websocket present, all good
fi
started=$(docker inspect --format "{{.State.StartedAt}}" mautrix-imessage)
age=$(( $(date +%s) - $(date -d "$started" +%s) ))
[ "$age" -lt 120 ] && exit 0   # freshly started, give it a moment
echo "$(date -Is) websocket to BlueBubbles missing (bridge up ${age}s) -> restarting bridge" >> "$LOG"
docker compose restart mautrix-imessage >> "$LOG" 2>&1
