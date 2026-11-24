#!/bin/bash
# Network watchdog (Phase 8a, 2026-11-14). Installed as /usr/local/sbin/net-watchdog.sh, run every
# minute by net-watchdog.timer (as root).
#
# Why: in the plug test, pulling the Ethernet cable made the hub's USB Ethernet adapter (r8152, on
# the Thunderbolt USB controller 0000:07:00.0) vanish; it only came back after a reboot.
#
#   network fine (adapter present + router 192.168.1.1 answers)  -> nothing
#   down 5 min   -> reset the hub's USB connection (port 4-2, then the whole Thunderbolt USB
#                   controller). The keyboard is on another controller and is not affected.
#   down 10 min AND the adapter is still missing -> reboot (at most once per 6 h, never within
#                   30 min of boot). If the adapter is there but the router doesn't answer,
#                   the router/cable is the problem, so it never reboots for that.
#   back again after an action -> phone notice via ntfy.
# Log: journalctl -t net-watchdog.  Undo: sudo systemctl disable --now net-watchdog.timer
IF=enxXXXXXXXXXXXX
GW=192.168.1.1
HUBPORT=4-2
XHCI=0000:07:00.0
ST=/var/lib/net-watchdog
CFG=/home/homelab/.config/monitoring
mkdir -p "$ST"
log() { logger -t net-watchdog "$*"; }
now=$(date +%s)

ntfy() {
  local body
  body=$(jq -nc --arg t "$(cat "$CFG/topic")" --arg ti "$1" --arg m "$2" \
    '{topic:$t,title:$ti,message:$m,priority:3,tags:["electric_plug"]}')
  # The token goes to curl through a file descriptor (-H @file), never on its command line,
  # so it cannot be seen in the process list.
  curl -fsS -m 10 -o /dev/null -H @<(printf 'Authorization: Bearer %s\n' "$(<"$CFG/ntfy-token")") \
    -H "Content-Type: application/json" -d "$body" http://127.0.0.1:2586/ || true
}

if [ -e "/sys/class/net/$IF" ] && ping -c 2 -W 2 -I "$IF" "$GW" >/dev/null 2>&1; then
  if [ -e "$ST/down_since" ]; then
    mins=$(( (now - $(cat "$ST/down_since")) / 60 ))
    log "network back after ~${mins} min"
    if [ -e "$ST/stage" ]; then
      ntfy "Server network came back" "The server's network was down for about ${mins} minutes and came back after an automatic reset of the Ethernet adapter. Nothing to do; if this happens often, the hub or cable may be failing."
    fi
    rm -f "$ST/down_since" "$ST/stage"
  fi
  exit 0
fi

[ -e "$ST/down_since" ] || { echo "$now" >"$ST/down_since"; log "network down (adapter $( [ -e /sys/class/net/$IF ] && echo present || echo MISSING ))"; }
age=$(( now - $(cat "$ST/down_since") ))
stage=$(cat "$ST/stage" 2>/dev/null || echo 0)

if [ $age -ge 300 ] && [ "$stage" -lt 1 ]; then
  echo 1 >"$ST/stage"
  if [ -e "/sys/bus/usb/devices/$HUBPORT/authorized" ]; then
    log "down 5 min: re-authorizing USB hub port $HUBPORT"
    echo 0 >"/sys/bus/usb/devices/$HUBPORT/authorized"; sleep 3
    echo 1 >"/sys/bus/usb/devices/$HUBPORT/authorized"; sleep 15
  fi
  if [ ! -e "/sys/class/net/$IF" ]; then
    log "adapter still missing: rebinding USB controller $XHCI"
    [ -e "/sys/bus/pci/drivers/xhci_hcd/$XHCI" ] && echo "$XHCI" ><REDACTED>
    sleep 3
    echo "$XHCI" ><REDACTED> 2>/dev/null || echo 1 >/sys/bus/pci/rescan
  fi
  exit 0
fi

if [ $age -ge 600 ] && [ "$stage" -lt 2 ] && [ ! -e "/sys/class/net/$IF" ]; then
  up=$(cut -d. -f1 /proc/uptime)
  last=$(cat "$ST/last_reboot" 2>/dev/null || echo 0)
  if [ "$up" -ge 1800 ] && [ $(( now - last )) -ge 21600 ]; then
    echo 2 >"$ST/stage"; echo "$now" >"$ST/last_reboot"
    log "adapter missing for 10 min after USB reset: rebooting"
    systemctl reboot
  fi
fi
