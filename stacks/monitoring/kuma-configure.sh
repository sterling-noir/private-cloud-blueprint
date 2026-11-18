#!/usr/bin/env bash
# Runs kuma-configure.py in a throwaway python container with host networking (server side).
# Usage on the server: ~/monitoring/kuma-configure.sh   (or "... test" to send one test alert)
set -euo pipefail
SCRIPT=kuma-configure.py; [ "${1:-}" = test ] && SCRIPT=kuma-test-alert.py
docker run --rm --network host -e SCRIPT="$SCRIPT" \
  -v "$HOME/.config/monitoring:/cfg:ro" -v "$HOME/monitoring:/m:ro" \
  python@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b \
  sh -c 'pip install -q --disable-pip-version-check --root-user-action=ignore "python-socketio[client]==5.*" websocket-client 2>&1 | grep -v -i warning; python -u /m/$SCRIPT'
