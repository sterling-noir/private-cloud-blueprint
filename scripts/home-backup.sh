#!/usr/bin/env bash
# Nightly backup of the home server (Phase 7a). Lives on the server as ~/bin/home-backup.sh,
# run by cron at 08:00 UTC (3 a.m. EST / 4 a.m. EDT).
#
# What it does:
#   1. Stops Tuwunel, the three bridges and Home Assistant (about a minute of downtime),
#      so the databases are copied in a consistent state.
#   2. restic-backs-up ~/matrix, ~/homeassistant, ~/homeassistant-compose, ~/homelab (config repo, added 2026-11-15), ~/goose, ~/monitoring, ~/vault and ~/.config (added 2026-11-17) to Backblaze B2
#      (encrypted on the server before upload).
#   3. Starts everything again — always, even if the backup failed.
#   4. Thins old snapshots (keeps 14 daily); on Sundays also prunes and
#      verifies part of the data.
#   5. Pings healthchecks.io if a URL is configured, so silence gets noticed.
#
# Secrets are read from ~/.config/restic/home.env (mode 600), written by home-backup-setup.sh.
# Logs: ~/backup-logs/YYYY-MM-DD.log (kept 60 days).

set -uo pipefail
export PATH="$HOME/bin:/usr/local/bin:/usr/bin:/bin"   # restic lives in ~/bin (official release binary)

ENV_FILE="$HOME/.config/restic/home.env"
LOG_DIR="$HOME/backup-logs"
MATRIX_COMPOSE="$HOME/matrix/compose.yml"
HA_COMPOSE="$<REDACTED>.yml"

mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/$(date -u +%F).log"
exec >>"$LOG" 2>&1
echo "===== $(date -u '+%F %T UTC') backup start"

# shellcheck disable=SC1090
set -a; . "$ENV_FILE"; set +a

ping_hc() {  # $1 = "" | /start | /fail
  [ -n "${HC_URL:-}" ] && curl -fsS -m 10 --retry 3 -o /dev/null "${HC_URL}$1" || true
}

start_all() {
  echo "-- starting containers"
  docker compose -f "$MATRIX_COMPOSE" start
  docker compose -f "$HA_COMPOSE" start
}

ping_hc /start

echo "-- stopping containers"
docker compose -f "$MATRIX_COMPOSE" stop
docker compose -f "$HA_COMPOSE" stop
trap start_all EXIT   # containers come back up whatever happens below

# The bridges' config files belong to uid 1337 and Home Assistant's .storage to root, so the
# backup itself runs as root inside a throwaway alpine container (same ~/bin/restic binary,
# data mounted read-only at the same paths, so snapshot paths match the host).
restic_as_root() {
  mkdir -p "$HOME/.cache/restic-root"
  docker run --rm --hostname "$(hostname)" \
    -e B2_ACCOUNT_ID -e B2_ACCOUNT_KEY -e RESTIC_REPOSITORY -e RESTIC_PASSWORD \
    -e RESTIC_CACHE_DIR=/cache \
    -v "$HOME/bin/restic:/usr/local/bin/restic:ro" \
    -v "$HOME/.cache/restic-root:/cache" \
    -v "$HOME/matrix:$HOME/matrix:ro" \
    -v "$HOME/homeassistant:$HOME/homeassistant:ro" \
    -v "$HOME/homeassistant-compose:$HOME/homeassistant-compose:ro" \
    -v "$HOME/homelab:$HOME/homelab:ro" \
    -v "$HOME/goose:$HOME/goose:ro" \
    -v "$HOME/monitoring:$HOME/monitoring:ro" \
    -v "$HOME/vault:$HOME/vault:ro" \
    -v "$HOME/.config:$HOME/.config:ro" \
    alpine:latest restic "$@"
}

restic_as_root backup \
  "$HOME/matrix" "$HOME/homeassistant" "$HOME/homeassistant-compose" "$HOME/homelab" \
  "$HOME/goose" "$HOME/monitoring" "$HOME/vault" "$HOME/.config" \
  --tag nightly \
  --exclude "$HOME/matrix/*/logs" \
  --exclude "$<REDACTED>.log*"
rc=$?

start_all
trap - EXIT

if [ $rc -ne 0 ]; then   # includes 3 = some files could not be read: treat as a failure
  echo "!! restic backup failed with exit code $rc"
  ping_hc /fail
  exit $rc
fi

# 14 daily snapshots only (decided 2026-11-14): Element purges messages at 30 days, so backups
# should not keep them for a year. Worst case a purged message lingers ~30 + 14 + 30 days
# (the last 30 = B2 keeping deleted files, the ransomware safety net).
restic forget --tag nightly --keep-daily 14
if [ "$(date -u +%u)" = 7 ]; then
  restic prune
  restic check --read-data-subset=5% || { ping_hc /fail; exit 1; }
fi

find "$LOG_DIR" -name '*.log' -mtime +60 -delete
echo "===== $(date -u '+%F %T UTC') backup done (restic exit $rc)"
ping_hc ""
