#!/usr/bin/env bash
# Home Assistant auto-update (Phase 8a). Lives on the server as ~/bin/ha-update.sh, cron daily 16:00 UTC.
#
# When the "stable" Home Assistant image has a new release (and it has been out SOAK_DAYS days):
#   1. pulls it, stops HA, takes a local copy of ~/homeassistant AND a restic snapshot to B2
#      (tag ha-update); if the backup fails nothing is changed;
#   2. pins the new digest in ~/homeassistant-compose/compose.yml (old file kept as
#      compose.yml.bak-ha-update-<date>) and recreates only the homeassistant container;
#   3. checks HA comes back (container running + web page answers, still fine 2 min later);
#   4. if not: puts the old config folder and old compose.yml back, recreates HA on the old image,
#      and never tries that release again.
# Every outcome is reported to the phone via ntfy. Bridges and Tuwunel are NOT touched.
#
# Options:  --dry-run        show what would happen, change nothing
#           --now            ignore the waiting period (SOAK_DAYS)
#           --test-rollback  full run with the CURRENT image, then pretend the health check
#                            failed - proves backup + rollback work without changing versions
# Logs: ~/ha-update-logs/YYYY-MM-DD.log (60 days).
#
# Undo / turn off: remove the cron line; to go back one version: cp the newest
#   compose.yml.bak-ha-update-* over compose.yml and `docker compose up -d` in ~/homeassistant-compose.
set -uo pipefail
export PATH="$HOME/bin:/usr/local/bin:/usr/bin:/bin"

SOAK_DAYS=2
<REDACTED>
IMAGE=ghcr.io/$REPO
DIR="$HOME/homeassistant-compose"
COMPOSE="$DIR/compose.yml"
CONF="$HOME/homeassistant"
BACKUPS="$HOME/ha-update-backups"
STATE="$HOME/.local/state/ha-update"
LOG_DIR="$HOME/ha-update-logs"
CFG="$HOME/.config/monitoring"

DRY=0; NOW=0; TEST_RB=0
for a in "$@"; do case $a in
  --dry-run) DRY=1;; --now) NOW=1;; --test-rollback) TEST_RB=1; NOW=1;;
  *) echo "unknown option $a"; exit 2;; esac; done

mkdir -p "$STATE" "$LOG_DIR" "$BACKUPS"
if [ $DRY = 0 ]; then exec > >(tee -a "$LOG_DIR/$(date -u +%F).log") 2>&1; fi
echo "===== $(date -u '+%F %T UTC') ha-update $*"

exec 9>"$STATE/lock"; flock -n 9 || { echo "another run is active"; exit 0; }

hhmm=$(date -u +%H%M)
if [ $DRY = 0 ] && [ "$hhmm" -ge 0715 ] && [ "$hhmm" -le 0845 ]; then
  echo "inside the nightly-backup window (07:15-08:45 UTC); not running"; exit 0
fi

ntfy() {  # title message priority tag
  [ $DRY = 1 ] && { echo "[would notify] $1 - $2"; return; }
  local body
  body=$(jq -nc --arg t "$(cat "$CFG/topic")" --arg ti "$1" --arg m "$2" --argjson p "$3" --arg tag "$4" \
    '{topic:$t,title:$ti,message:$m,priority:$p,tags:[$tag]}')
  curl -fsS -m 10 -o /dev/null -H "Authorization: Bearer $(cat "$CFG/ntfy-token")" \
    -H "Content-Type: application/json" -d "$body" http://127.0.0.1:2586/ || echo "!! ntfy send failed"
}

# --- what runs now, what is "stable" upstream <REDACTED>
cur=$(grep -oE "$IMAGE@sha256:[0-9a-f]{64}" "$COMPOSE" | head -1 | cut -d@ -f2)
[ -n "$cur" ] || { echo "!! no pinned digest found in $COMPOSE"; exit 1; }
cur_ver=$(docker image inspect --format '{{ index .Config.Labels "org.opencontainers.image.version" }}' "$IMAGE@$cur" 2>/dev/null || echo "?")

ACC="Accept: application/vnd.oci.image.index.v1+json,application/vnd.docker.distribution.manifest.list.v2+json,application/vnd.oci.image.manifest.v1+json,application/vnd.docker.distribution.manifest.v2+json"
tok=$(curl -fsS -m 20 "https://ghcr.io/token?scope=repository:$REPO:pull" | jq -r .token)
reg() { curl -fsS -m 30 -H "Authorization: Bearer $tok" -H "$ACC" "$@"; }
new=$(reg -I "https://ghcr.io/v2/$REPO/manifests/stable" | tr -d '\r' | awk 'tolower($1)=="docker-content-digest:" {print $2}')
[ -n "$new" ] || { echo "!! could not read the stable digest from ghcr.io"; exit 1; }
md=$(reg "https://ghcr.io/v2/$REPO/manifests/$new" | jq -r '.manifests[] | select(.platform.architecture=="amd64" and .platform.os=="linux") | .digest' | head -1)
cfgd=$(reg "https://ghcr.io/v2/$REPO/manifests/$md" | jq -r .config.digest)
new_ver=$(reg -L "https://ghcr.io/v2/$REPO/blobs/$cfgd" | jq -r '.config.Labels["org.opencontainers.image.version"] // "?"')
echo "running: $cur_ver ($cur)"
echo "stable:  $new_ver ($new)"

if [ $TEST_RB = 1 ]; then
  new=$cur; new_ver=$cur_ver; echo "TEST: re-deploying the current image, then forcing a rollback"
elif [ "$new" = "$cur" ]; then
  echo "up to date"; exit 0
fi
if [ $TEST_RB = 0 ] && grep -qx "$new" "$STATE/bad-digests" 2>/dev/null; then
  echo "stable $new_ver failed before and was rolled back; waiting for the next release"; exit 0
fi

# --- waiting period: let a new release be out SOAK_DAYS days before installing it -------------
if [ $NOW = 0 ]; then
  read -r cand since 2>/dev/null <"$STATE/candidate" || cand=""
  if [ "$cand" != "$new" ]; then
    [ $DRY = 0 ] && echo "$new $(date +%s)" >"$STATE/candidate"
    echo "new release $new_ver seen for the first time; installing after $SOAK_DAYS days"; exit 0
  fi
  age=$(( ($(date +%s) - since) / 86400 ))
  if [ $age -lt $SOAK_DAYS ]; then echo "$new_ver seen $age day(s) ago; waiting until $SOAK_DAYS"; exit 0; fi
fi

if [ $DRY = 1 ]; then
  echo "[dry run] would: pull $new_ver, stop HA, back up (local copy + restic tag ha-update),"
  echo "          pin $new in $COMPOSE (keeping a .bak), recreate homeassistant, health-check,"
  echo "          and roll back to $cur_ver if unhealthy."
  ntfy "Home Assistant update (dry run)" "Would update $cur_ver to $new_ver." 2 test_tube
  exit 0
fi

# --- 1. pull + backup <REDACTED>
echo "-- pulling $IMAGE@$new"
docker pull -q "$IMAGE@$new" >/dev/null || { ntfy "Home Assistant update skipped" "Could not download Home Assistant $new_ver. Nothing changed; it will try again tomorrow." 2 warning; exit 1; }

stamp=$(date -u +%Y%m%d-%H%M)
tarball="$BACKUPS/ha-config-$stamp-$cur_ver.tgz"
echo "-- stopping Home Assistant"
docker compose -f "$COMPOSE" stop homeassistant
restart_old() { docker compose -f "$COMPOSE" start homeassistant; }

echo "-- local copy of $CONF -> $tarball"
tb="/b/$(basename "$tarball")"   # holds secrets.yaml: owner-only
if ! docker run --rm -v "$CONF:/c:ro" -v "$BACKUPS:/b" alpine:latest \
     sh -c "tar -C /c -czpf '$tb' . && chown $(id -u):$(id -g) '$tb' && chmod 600 '$tb'"; then
  restart_old; ntfy "Home Assistant update skipped" "The local backup before updating to $new_ver failed. Home Assistant is unchanged and running. Ask Claude to look at ~/ha-update-logs." 3 warning; exit 1
fi

echo "-- restic snapshot (tag ha-update)"
set -a; . "$HOME/.config/restic/home.env"; set +a
mkdir -p "$HOME/.cache/restic-root"
docker run --rm --hostname "$(hostname)" \
  -e B2_ACCOUNT_ID -e B2_ACCOUNT_KEY -e RESTIC_REPOSITORY -e RESTIC_PASSWORD -e RESTIC_CACHE_DIR=/cache \
  -v "$HOME/bin/restic:/usr/local/bin/restic:ro" -v "$HOME/.cache/restic-root:/cache" \
  -v "$CONF:$CONF:ro" -v "$DIR:$DIR:ro" \
  alpine:latest restic backup "$CONF" "$DIR" --tag ha-update --exclude "$CONF/home-assistant.log*"
rc=$?
if [ $rc -ne 0 ]; then
  restart_old; ntfy "Home Assistant update skipped" "The backup to Backblaze before updating to $new_ver failed (restic exit $rc). Home Assistant is unchanged and running. Ask Claude to look at ~/ha-update-logs." 3 warning; exit 1
fi
restic forget --tag ha-update --keep-last 3 >/dev/null || true

# --- 2. pin + recreate <REDACTED>
bak="$COMPOSE.bak-ha-update-$stamp"
cp -p "$COMPOSE" "$bak"
sed -i -E "s|$IMAGE@sha256:[0-9a-f]{64}( *# *[^ ]+)?|$IMAGE@$new  # $new_ver|" "$COMPOSE"
echo "-- compose now: $(grep -oE "$IMAGE@[^ ]+ +# .*" "$COMPOSE")"
docker compose -f "$COMPOSE" up -d homeassistant

# --- 3. health check <REDACTED>
healthy() {  # up to 5 min for the web page, then still fine 2 min later
  local i
  for i in $(seq 60); do
    [ "$(docker inspect -f '{{.State.Running}}' homeassistant 2>/dev/null)" = true ] &&
      curl -fsS -m 5 -o /dev/null http://127.0.0.1:8123/manifest.json && break
    sleep 5
  done
  [ "$i" -lt 60 ] || return 1
  local rc0; rc0=$(docker inspect -f '{{.RestartCount}}' homeassistant)
  sleep 120
  [ "$(docker inspect -f '{{.State.Running}}' homeassistant)" = true ] &&
    [ "$(docker inspect -f '{{.RestartCount}}' homeassistant)" = "$rc0" ] &&
    curl -fsS -m 5 -o /dev/null http://127.0.0.1:8123/manifest.json
}

ok=0; healthy && ok=1
[ $TEST_RB = 1 ] && { echo "TEST: pretending the health check failed"; ok=0; }

if [ $ok = 1 ]; then
  echo "-- healthy on $new_ver"
  rm -f "$STATE/candidate"
  ls -1t "$BACKUPS"/ha-config-*.tgz 2>/dev/null | tail -n +4 | xargs -r rm -f
  ls -1t "$DIR"/compose.yml.bak-ha-update-* 2>/dev/null | tail -n +6 | xargs -r rm -f
  # keep the current and the previous HA image, remove older ones
  keep="$(docker image inspect -f '{{.Id}}' "$IMAGE@$new") $(docker image inspect -f '{{.Id}}' "$IMAGE@$cur" 2>/dev/null)"
  for id in $(docker images --no-trunc -q "$IMAGE" | sort -u); do
    case " $keep " in *" $id "*) ;; *) docker rmi "$id" >/dev/null 2>&1 && echo "removed old image $id";; esac
  done
  ntfy "Home Assistant updated to $new_ver" "Updated from $cur_ver to $new_ver. Backup taken first; it is running normally. Nothing to do." 2 white_check_mark
  echo "===== done"; exit 0
fi

# --- 4. rollback <REDACTED>
echo "!! unhealthy - rolling back to $cur_ver"
docker compose -f "$COMPOSE" stop homeassistant
cp -p "$bak" "$COMPOSE"
docker run --rm -v "$CONF:/c" -v "$BACKUPS:/b:ro" alpine:latest \
  sh -c "find /c -mindepth 1 -delete && tar -C /c -xzpf '/b/$(basename "$tarball")'"
docker compose -f "$COMPOSE" up -d homeassistant
if [ $TEST_RB = 0 ]; then echo "$new" >>"$STATE/bad-digests"; fi
if healthy; then
  if [ $TEST_RB = 1 ]; then
    ntfy "Home Assistant rollback test passed" "Backup, update and rollback all worked; Home Assistant is running $cur_ver as before. Nothing to do." 2 white_check_mark
  else
    ntfy "Home Assistant update rolled back" "Home Assistant $new_ver did not start properly, so it was put back to $cur_ver with the config from just before. It is running normally. It will skip $new_ver and try the next release. Mention it to Claude." 3 leftwards_arrow_with_hook
  fi
  echo "===== rolled back"; exit 1
fi
ntfy "Home Assistant is down" "An update to $new_ver failed AND the rollback to $cur_ver did not come back up. Lights and plugs still work by hand. Ask Claude to fix it (log: ~/ha-update-logs)." 5 rotating_light
echo "!! rollback unhealthy"; exit 1
