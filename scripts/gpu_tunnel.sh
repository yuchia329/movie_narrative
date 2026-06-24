#!/usr/bin/env bash
# Open the SSH tunnel the platform needs to reach gpud + the on-demand instance pool on
# the GPU box. Forwards gpud (50050) + every port in GPUD_PORT_RANGE, so growing the pool
# (raising GPUD_ASR_MAX / GPUD_TTS_MAX) needs NO change here as long as it stays in range.
#
#   GPUD_PORT_RANGE=50060-50099 bash scripts/gpu_tunnel.sh nlp
#   (then set GPU_SUPERVISOR_TARGET=localhost:50050)
#
# Prod runs the same flag list under autossh (see deploy/). SSH takes one -L per port;
# for very wide ranges consider `sshuttle` instead.
set -euo pipefail

HOST="${1:-nlp}"
GPUD_PORT="${GPUD_PORT:-50050}"
RANGE="${GPUD_PORT_RANGE:-50060-50099}"
LO="${RANGE%-*}"
HI="${RANGE#*-}"
# Bind address for the local forwards. Default 127.0.0.1; set BIND=0.0.0.0 so Docker
# containers can reach the tunnel via host.docker.internal (compose-local + gpud).
BIND="${BIND:-127.0.0.1}"

# Ensure gpud is up on the box BEFORE tunnelling to it — idempotent, so it's a no-op when
# gpud is already running (prod + local dev share ONE gpud). The launch needs box-side
# secrets, so it's delegated to a launcher ON THE BOX (server/start_gpud.sh + ~/jieshuo/
# gpud.env). Set ENSURE_GPUD=0 to skip (e.g. when you manage gpud yourself).
if [[ "${ENSURE_GPUD:-1}" == "1" ]]; then
  echo "ensuring gpud on ${HOST}…"
  # The literal ~ stays unexpanded in the quotes and expands on the BOX; GPUD_REMOTE_DIR is a
  # client-side override of where ~/jieshuo lives on the box.
  # shellcheck disable=SC2029
  ssh "$HOST" "bash ${GPUD_REMOTE_DIR:-~/jieshuo}/server/start_gpud.sh" \
    || echo "WARN: could not ensure gpud on ${HOST}; start it manually (see server/README_deploy.md)" >&2
fi

flags=( -N -L "${BIND}:${GPUD_PORT}:localhost:${GPUD_PORT}" )
for p in $(seq "$LO" "$HI"); do
  flags+=( -L "${BIND}:${p}:localhost:${p}" )
done

echo "tunneling gpud ${GPUD_PORT} + range ${LO}-${HI} to ${HOST} on ${BIND} ($((HI-LO+2)) ports)…"
exec ssh "${flags[@]}" "$HOST"
