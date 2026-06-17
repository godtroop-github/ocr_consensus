#!/usr/bin/env bash
set -euo pipefail

# Deploy the OCR consensus web app to server 199.
#
# Fixed rules:
# - Keep archive paths intact. Do NOT use --strip-components on remote extract.
# - Remote app root is /home/gt/ocr-fx/ocr_system.
# - Web server must run with /home/gt/ocr-fx/venv/bin/python, not system python3.
# - Upload through gw_ssh after gw_env_check so stdin is not consumed by link checks.
# - Restart by killing the process bound to the target port, not by pkill -f.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REMOTE_ROOT="${REMOTE_ROOT:-/home/gt/ocr-fx/ocr_system}"
REMOTE_PYTHON="${REMOTE_PYTHON:-/home/gt/ocr-fx/venv/bin/python}"
REMOTE_HOST="${REMOTE_HOST:-128.18.185.199}"
REMOTE_PORT="${REMOTE_PORT:-8090}"
REMOTE_TMP="${REMOTE_TMP:-/tmp/ocr_consensus_199_deploy.tgz}"
REMOTE_LOG="${REMOTE_LOG:-/tmp/ocr_consensus_8090.log}"
PACKAGE="$(mktemp /tmp/ocr_consensus_199_deploy.XXXXXX.tgz)"

cleanup() {
  rm -f "$PACKAGE"
}
trap cleanup EXIT

cd "$ROOT_DIR"

tar -czf "$PACKAGE" \
  ops/consensus_task_app \
  ops/consensus_task_app_server.py

set +e
source "$ROOT_DIR/ops/199_proxy_gateway.sh"
set -e

gw_env_check >/dev/null

# Important: GW_MODE is already set by gw_env_check. Do not allow gw_ssh to run
# gw_env_check again while stdin is redirected, or the tar stream may be consumed.
gw_ssh "cat > '$REMOTE_TMP'" < "$PACKAGE"

gw_ssh "bash -s" <<REMOTE
set -euo pipefail
cd "$REMOTE_ROOT"
tar -xzf "$REMOTE_TMP"

pid="\$(ss -ltnp 2>/dev/null | awk -v port=":$REMOTE_PORT" '\$4 ~ port {print \$0}' | sed -n 's/.*pid=\([0-9][0-9]*\).*/\1/p' | head -n 1)"
if [ -n "\$pid" ]; then
  kill "\$pid" || true
  sleep 1
fi

nohup "$REMOTE_PYTHON" ops/consensus_task_app_server.py --host 0.0.0.0 --port "$REMOTE_PORT" > "$REMOTE_LOG" 2>&1 < /dev/null &
sleep 1

ss -ltnp | grep ":$REMOTE_PORT" >/dev/null
REMOTE

echo "Deployed to http://$REMOTE_HOST:$REMOTE_PORT/"

