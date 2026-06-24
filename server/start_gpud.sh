#!/usr/bin/env bash
# Idempotently ensure gpud is running on the GPU box.
#
#   - If gpud is ALREADY running, this is a no-op (prints the pid and exits 0) — so production
#     and local dev SHARE one gpud instead of standing up a second supervisor.
#   - Otherwise it launches gpud DETACHED (survives the SSH session) with the box's env.
#
# Lives ON THE BOX (scp to ~/jieshuo/server/). `scripts/gpu_tunnel.sh` calls it over SSH so a
# local `docker compose` dev session gets GPU without a second gpud. Box-specific secrets/config
# (HF_TOKEN, TTS_REF_WAV, TTS_REF_TEXT, optional ASR_MODEL / GPUD_* overrides) live in
# ~/jieshuo/gpud.env (gitignored on the box) — see README_deploy.md for the full launch env.
set -euo pipefail

# Non-interactive SSH shells often don't have uv on PATH (it installs to ~/.local/bin).
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"

JIESHUO_HOME="${JIESHUO_HOME:-$HOME/jieshuo}"
cd "$JIESHUO_HOME"

if pgrep -f "server/gpud.py" >/dev/null 2>&1; then
  echo "gpud already running (pid $(pgrep -f 'server/gpud.py' | head -1)) — sharing it"
  exit 0
fi

# gpud can't launch ASR/TTS instances without the box secrets, so require the env file.
ENV_FILE="${GPUD_ENV_FILE:-$JIESHUO_HOME/gpud.env}"
if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a
else
  echo "ERROR: gpud is not running and there is no env file at $ENV_FILE." >&2
  echo "       gpud needs HF_TOKEN + TTS_REF_WAV/TTS_REF_TEXT to launch ASR/TTS instances." >&2
  echo "       Create $ENV_FILE (see server/README_deploy.md) or start gpud manually." >&2
  exit 1
fi

export PYTHONPATH="$JIESHUO_HOME"
: "${GPUD_PORT_RANGE:=50060-50099}"; export GPUD_PORT_RANGE
: "${GPUD_ASR_MAX:=3}"; export GPUD_ASR_MAX
: "${GPUD_TTS_MAX:=3}"; export GPUD_TTS_MAX
GPUD_PORT="${GPUD_PORT:-50050}"

echo "starting gpud (range $GPUD_PORT_RANGE, asr_max=$GPUD_ASR_MAX, tts_max=$GPUD_TTS_MAX) -> :$GPUD_PORT"
# setsid + nohup + </dev/null fully detach so closing the SSH session doesn't kill gpud.
setsid nohup uv run --no-sync --project server/asr python server/gpud.py \
  >"$JIESHUO_HOME/gpud.log" 2>&1 </dev/null &
disown 2>/dev/null || true

# Wait until gpud binds its control port (or give up with a pointer to the log).
for _ in $(seq 1 20); do
  if pgrep -f "server/gpud.py" >/dev/null 2>&1 \
     && { ss -ltn 2>/dev/null || netstat -ltn 2>/dev/null; } | grep -q ":$GPUD_PORT "; then
    echo "gpud up on :$GPUD_PORT"
    exit 0
  fi
  sleep 1
done
echo "WARN: gpud did not open :$GPUD_PORT within 20s — check $JIESHUO_HOME/gpud.log" >&2
exit 1
