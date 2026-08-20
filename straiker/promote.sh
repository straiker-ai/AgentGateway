#!/usr/bin/env bash
# Promote to the shared gateway (your gateway host). 
# Gates run BEFORE the image is built; the live gateway is smoke-tested AFTER.
#   ./promote.sh --check     what's deployed vs this branch (no changes)
#   ./promote.sh             gate -> build -> deploy -> live verify
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"; REGION="${AWS_REGION:-us-east-1}"; SVC="${SERVICE_NAME:-straiker-agentgateway}"; GW="${STRAIKER_GATEWAY_URL:-http://localhost:3000}"
export AWS_PAGER=""
CHECK_ONLY=0; [ "${1:-}" = "--check" ] && CHECK_ONLY=1
step(){ printf "\n\033[1m== %s ==\033[0m\n" "$1"; }; fail(){ printf "  \033[31mFAIL\033[0m %s\n" "$1"; exit 1; }; ok(){ printf "  \033[32mok\033[0m   %s\n" "$1"; }
LOCAL_SHA=$(git -C "$HERE" rev-parse --short HEAD); PIN=$(tr -d '[:space:]' < "$HERE/gateway/VERSION")

step "what is deployed vs local"
ARN=$(aws apprunner list-services --region "$REGION" --query "ServiceSummaryList[?ServiceName=='$SVC'].ServiceArn" --output text 2>/dev/null)
if [ -n "$ARN" ]; then
  DEP=$(aws apprunner describe-service --service-arn "$ARN" --region "$REGION" --query 'Service.SourceConfiguration.ImageRepository.ImageIdentifier' --output text 2>/dev/null)
  echo "  deployed image : ${DEP##*:}"; echo "  local branch   : $LOCAL_SHA  (agentgateway $PIN)"
  [ "${DEP##*:}" = "$LOCAL_SHA" ] && ok "gateway is CURRENT" || echo "  -> gateway is BEHIND this branch"
else echo "  (no service / cannot reach App Runner — expired creds?)"; fi
[ "$CHECK_ONLY" = "1" ] && exit 0

step "1/6 working tree committed"
[ -z "$(git -C "$HERE" status --porcelain)" ] || fail "uncommitted changes — commit first so the image tag matches the code"; ok "clean at $LOCAL_SHA"
step "2/6 python compiles"
"$HERE/.venv/bin/python" -m compileall -q "$HERE/straiker_sidecar" "$HERE/spec" "$HERE/lab" >/dev/null || fail "compile error"; ok "compileall"
step "3/6 behavioural QA + relay fidelity (offline)"
"$HERE/.venv/bin/python" "$HERE/spec/qa.py" > /tmp/agw_qa.log 2>&1 || { grep -E "FAIL|QA RED" /tmp/agw_qa.log | head; fail "qa.py red"; }
ok "$(grep -oE 'pass=[0-9]+ fail=[0-9]+' /tmp/agw_qa.log | tail -1)"
step "4/6 config preflight against the PINNED binary ($PIN)  <- the analogue of Kong's KONG_PLUGINS gate"
# agentgateway rejecting a config at load looks exactly like a code bug (proxy serves nothing / last-good). Catch it here.
T=$(mktemp -d); "$HERE/.venv/bin/python" "$HERE/deploy/render_config.py" > "$T/config.yaml" || fail "render_config failed"
docker run --rm -e OPENAI_API_KEY=x -e ANTHROPIC_API_KEY=x -e GEMINI_API_KEY=x -e VERTEX_PROJECT=x -e DATABRICKS_HOST=https://x -e DATABRICKS_TOKEN=x -e AWS_ACCESS_KEY_ID=x -e AWS_SECRET_ACCESS_KEY=x -e AWS_SESSION_TOKEN=x -e AWS_REGION=us-east-1 -e AZURE_OPENAI_API_KEY=x -e AZURE_RESOURCE_NAME=x -e AZURE_OPENAI_API_VERSION=x -e STRAIKER_AGENTGATEWAY_KEY=x -e STRAIKER_AGENTGATEWAY_CODING_KEY=x -v "$T:/cfg" -v "$HERE/deploy/htpasswd:/tmp/agw/htpasswd:ro" "${AGW_BASE:-straiker-agentgateway-fork:local}" -f /cfg/config.yaml --validate-only >/dev/null 2>&1 || fail "rendered config rejected by agentgateway $PIN"
grep -q "ARG AGW_VERSION=$PIN" "$HERE/gateway/Dockerfile" || fail "Dockerfile pin != gateway/VERSION ($PIN)"
grep -q "$PIN" "$HERE/straiker_sidecar/proto/PROVENANCE.md" || fail "proto provenance not at $PIN — re-copy protos + regen"
rm -rf "$T"; ok "config valid on $PIN; Dockerfile + proto provenance pinned to $PIN"
step "5/6 build + deploy"
"$HERE/deploy/apprunner/deploy.sh" > /tmp/agw_deploy.log 2>&1 || { tail -25 /tmp/agw_deploy.log; fail "deploy failed"; }
ok "image pushed and service RUNNING"
step "6/6 live verify"
"$HERE/live_qa.sh" || fail "live QA red"
printf "\n\033[32mPROMOTED\033[0m  %s -> %s  (agentgateway %s)\n" "$LOCAL_SHA" "$GW" "$PIN"
