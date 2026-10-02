#!/bin/bash
# pid 1 entrypoint for the taxbuddy-hermes tools sidecar (runs as uid 10000).
set -euo pipefail

export HOME="${HOME:-/opt/data/tools/home}"
export MOSHI_STATE_DIR="${MOSHI_STATE_DIR:-/opt/data/tools/moshi-state}"
export MOSHI_CONFIG_DIR="${MOSHI_CONFIG_DIR:-/opt/data/tools/moshi-config}"

# Scrub inherited HERDR_* env vars; a foreign HERDR_SOCKET_PATH makes
# `herdr server` refuse to start.
while IFS= read -r var; do
    unset "$var"
done < <(env | cut -d= -f1 | grep '^HERDR_' || true)

mkdir -p /run/moshi "$HOME/.local/bin" /opt/data/tools/ssh \
    "$MOSHI_STATE_DIR" "$MOSHI_CONFIG_DIR"
# /run/sshd (sshd's privilege-separation dir) is NOT created here: it must be
# root-owned, so the pod mounts a root-owned emptyDir there instead.

# herdr's remote capability probe looks for the binary in ~/.local/bin.
ln -sf /usr/local/bin/herdr "$HOME/.local/bin/herdr"

# Persistent SSH host key on the PVC so clients see a stable fingerprint.
if [ ! -f /opt/data/tools/ssh/ssh_host_ed25519_key ]; then
    ssh-keygen -t ed25519 -f /opt/data/tools/ssh/ssh_host_ed25519_key -N ""
fi

# tini as pid 1 reaps the orphaned background processes (sshd login
# children would otherwise linger as zombies — herdr server does not reap).
/usr/sbin/sshd -D -e -f /opt/tools/sshd_config &
moshi-hook serve &

exec tini -- herdr server
