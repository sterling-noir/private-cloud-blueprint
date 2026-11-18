#!/usr/bin/env bash
# Phase 8a one-time setup ON THE SERVER (idempotent: safe to run again).
# Creates the ntfy topic, users and token, the Uptime Kuma admin password, and starts the stack.
# Prints no secrets: passwords/tokens go only into ~/.config/monitoring (mode 600).
set -euo pipefail
umask 077
CFG="$HOME/.config/monitoring"
MON="$HOME/monitoring"
mkdir -p "$CFG" "$MON/ntfy/data" "$MON/kuma/data" "$MON/diun/data"
chmod 700 "$CFG"

rand()   { LC_ALL=C tr -dc "a-z0-9"    </dev/urandom 2>/dev/null | head -c "$1" || true; }
randpw() { LC_ALL=C tr -dc "A-Za-z0-9" </dev/urandom 2>/dev/null | head -c "$1" || true; }

[ -s "$CFG/topic" ] || echo "home-alerts-$(rand 12)" >"$CFG/topic"
TOPIC=$(cat "$CFG/topic")
echo "DIUN_NOTIF_NTFY_TOPIC=$TOPIC" >"$CFG/diun.env"
[ -s "$CFG/kuma-password" ] || randpw 24 >"$CFG/kuma-password"
[ -s "$CFG/ntfy-owner-password" ] || randpw 20 >"$CFG/ntfy-owner-password"
touch "$CFG/ntfy-token"   # compose mounts it; filled below

cd "$MON"
docker compose up -d ntfy
for i in $(seq 30); do curl -fsS -m 3 http://127.0.0.1:2586/v1/health >/dev/null 2>&1 && break; sleep 1; done

ntfy_cli() { docker exec -i ntfy ntfy "$@"; }
users=$(ntfy_cli user list 2>&1 || true)

if ! grep -q '^user server ' <<<"$users"; then
  # the "server" user only ever uses its token; its password is random and thrown away
  randpw 32 | docker exec -i ntfy sh -c 'read -r NTFY_PASSWORD; export NTFY_PASSWORD; ntfy user add server' >/dev/null
fi
if ! grep -q '^user owner ' <<<"$users"; then
  docker exec -i ntfy sh -c 'read -r NTFY_PASSWORD; export NTFY_PASSWORD; ntfy user add owner' <"$CFG/ntfy-owner-password" >/dev/null
fi
ntfy_cli access server "$TOPIC" write-only >/dev/null
ntfy_cli access owner "$TOPIC" read-write >/dev/null

if ! grep -q '^tk_' "$CFG/ntfy-token"; then
  ntfy_cli token add --label="home server scripts" server 2>&1 | grep -o 'tk_[A-Za-z0-9]*' | head -1 | tr -d '\n' >"$CFG/ntfy-token"   # no newline: Diun sends the file as-is
fi
grep -q '^tk_' "$CFG/ntfy-token" || { echo "!! could not create the ntfy token"; exit 1; }

docker compose up -d
echo "Users:"; ntfy_cli user list 2>&1 | grep -E '^user|^- ' | sed -E 's/tk_[A-Za-z0-9]+/tk_…/g'
echo "Topic: $TOPIC"
docker compose ps --format 'table {{.Name}}\t{{.Status}}'
