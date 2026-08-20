#!/usr/bin/env bash
# 1) materialize config from base64 env (App Runner: env vars, NOT Secrets Manager — see deploy.sh)
# 2) start the Straiker sidecar on loopback, wait for /health (bounded — a silently absent
#    guardrail that fails open is worse than a failed deploy)
# 3) exec agentgateway in the foreground (PID 1 semantics, logs to stdout)
set -euo pipefail
CFG_DIR="${AGW_CFG_DIR:-/tmp/agw}"; mkdir -p "$CFG_DIR"
if [ -n "${AGW_HTPASSWD_B64:-}" ]; then
  echo "$AGW_HTPASSWD_B64" | base64 -d > "$CFG_DIR/htpasswd"
fi
if [ -n "${AGW_CONFIG_B64:-}" ]; then
  echo "$AGW_CONFIG_B64" | base64 -d > "$CFG_DIR/config.yaml"
else
  cp "${AGW_CONFIG_FILE:-/srv/config/agentgateway.yaml}" "$CFG_DIR/config.yaml"
fi
# agentgateway reads provider keys from env (ANTHROPIC_API_KEY, OPENAI_API_KEY, ...) unless the
# config pins them; pass-through of the client's own key is the default for the coding routes.

export SIDECAR_HTTP_HOST="${SIDECAR_HTTP_HOST:-127.0.0.1}" SIDECAR_GRPC_HOST="${SIDECAR_GRPC_HOST:-127.0.0.1}"
cd /srv && python -m straiker_sidecar &
SIDECAR_PID=$!
for i in $(seq 1 60); do
  if curl -sf -m 1 "http://127.0.0.1:${SIDECAR_HTTP_PORT:-8080}/health" >/dev/null 2>&1; then break; fi
  if ! kill -0 $SIDECAR_PID 2>/dev/null; then echo "sidecar died during startup"; exit 1; fi
  sleep 0.5
done
curl -sf -m 1 "http://127.0.0.1:${SIDECAR_HTTP_PORT:-8080}/health" >/dev/null || { echo "sidecar never became healthy"; exit 1; }
echo "straiker sidecar healthy; starting agentgateway ${AGENTGATEWAY_VERSION:-?} with $CFG_DIR/config.yaml"
agentgateway -f "$CFG_DIR/config.yaml" --validate-only
# if agentgateway exits, the container exits (App Runner restarts it); sidecar dies with it.
trap 'kill $SIDECAR_PID 2>/dev/null || true' EXIT
exec agentgateway -f "$CFG_DIR/config.yaml"
