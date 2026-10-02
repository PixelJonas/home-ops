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

# Copy authorized_keys off the ConfigMap mount: OpenShift injects an fsGroup
# onto volumes, making the ConfigMap dir group-writable, which sshd's
# StrictModes rejects ("bad ownership or modes"). The copy lives on the PVC,
# owned by uid 10000 (this script's uid), mode 0600.
install -m 0600 /opt/tools-ssh/authorized_keys /opt/data/tools/ssh/authorized_keys

# tini as pid 1 reaps the orphaned background processes (sshd login
# children would otherwise linger as zombies — herdr server does not reap).
/usr/sbin/sshd -D -e -f /opt/tools/sshd_config &

# Run the daemon from the SHARED binary copy (populated by the pod's
# tools-bin-init container), not /usr/local/bin: the moshi-hooks plugin in
# the hermes container embeds HELPER=/opt/tools-bin/moshi-hook at install
# time, and the daemon's staleness check compares the installed plugin
# against what ITS OWN binary path would generate — running serve from the
# shared path keeps the two in agreement ("__init__.py differs" warning).
/opt/tools-bin/moshi-hook serve &

# herdr server runs as a BACKGROUND process, not as tini's direct child:
# `herdr machine add` (and `herdr update --handoff`) replace the server via
# live handoff, which stops the old server process. If the server were pid
# 1's only child, tini would exit with it and kill the whole container —
# taking the freshly handoff'd server down too and failing the client's
# post-handoff readiness probe ("remote server is not ready for saved
# machines"). With `sleep infinity` as tini's child the container survives
# the handoff and the new server is reparented to (and reaped by) tini.
herdr server &

exec tini -- sleep infinity
