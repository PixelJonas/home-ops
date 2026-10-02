#!/bin/sh
# Open an interactive Hermes TUI inside the sibling `hermes` container of the
# same pod. kubectl uses the in-cluster ServiceAccount automatically.
#
# `kubectl exec` lands as root (the container default), but the gateway runs
# as uid 10000 (hermes) and /opt/data is 0700 hermes:hermes — a root-run TUI
# would leave root-owned SQLite -wal/history files behind that the gateway
# can no longer write. Drop to the hermes user via su (the image's
# s6-overlay does the same for the gateway).
exec kubectl exec -it -n taxbuddy-hermes deploy/taxbuddy-hermes-app -c hermes -- \
  env "TERM=${TERM:-xterm-256color}" su -s /bin/bash hermes -c 'exec env HOME=/opt/data hermes'
