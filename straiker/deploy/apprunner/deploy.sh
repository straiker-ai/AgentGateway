#!/usr/bin/env bash
# Build the combined agentgateway+sidecar image, push to ECR, create/update the App Runner service.
# Forked from /apprunner/deploy.sh — same account, same roles.
#
# SECRETS: passed as RUNTIME ENV VARS (base64 where multi-line). NOT App Runner RuntimeEnvironmentSecrets:
# Secrets-Manager injection failed CREATE_FAILED in ~19s with NO application log group (App Runner
# resolves secrets before the container runs, so nothing is logged) even with a validated instance role.
# Env vars work and are encrypted at rest. Do not "fix" this back to Secrets Manager without re-testing.
#
# Requires: fresh AWS creds (ECR + App Runner + Route53), Straiker Projects/.env sourced, deploy/agent-keys.env.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"; REPO="$(cd "$HERE/../.." && pwd)"
REGION="${AWS_REGION:-us-east-1}"; export AWS_PAGER=""
ACCT="$(aws sts get-caller-identity --query Account --output text)"
ECR="$ACCT.dkr.ecr.$REGION.amazonaws.com"
IMG="${SERVICE_NAME:-straiker-agentgateway}"; SVC="${SERVICE_NAME:-straiker-agentgateway}"; DOMAIN="${STRAIKER_GATEWAY_DOMAIN:?set STRAIKER_GATEWAY_DOMAIN (e.g. gateway.example.com)}"
TAG="$(git -C "$REPO" rev-parse --short HEAD 2>/dev/null || echo latest)"
AGW_VERSION="$(tr -d '[:space:]' < "$REPO/gateway/VERSION")"
: "${STRAIKER_AGENTGATEWAY_KEY:?source Straiker Projects/.env}"; : "${ANTHROPIC_KEY:?}"

echo "== 0. preflight: rendered config validates against the PINNED binary ($AGW_VERSION) =="
RENDERED="$(mktemp -d)"; "$REPO/.venv/bin/python" "$REPO/deploy/render_config.py" > "$RENDERED/config.yaml"
docker run --rm -e OPENAI_API_KEY=x -e ANTHROPIC_API_KEY=x -e GEMINI_API_KEY=x -e VERTEX_PROJECT=x -e DATABRICKS_HOST=https://x -e DATABRICKS_TOKEN=x -e AWS_ACCESS_KEY_ID=x -e AWS_SECRET_ACCESS_KEY=x -e AWS_SESSION_TOKEN=x -e AWS_REGION=us-east-1 -e AZURE_OPENAI_API_KEY=x -e AZURE_RESOURCE_NAME=x -e AZURE_OPENAI_API_VERSION=x -e STRAIKER_AGENTGATEWAY_KEY=x -e STRAIKER_AGENTGATEWAY_CODING_KEY=x -v "$RENDERED:/cfg" -v "$REPO/deploy/htpasswd:/tmp/agw/htpasswd:ro" \
  "${AGW_BASE:-straiker-agentgateway-fork:local}" -f /cfg/config.yaml --validate-only
grep -q "ARG AGW_VERSION=$AGW_VERSION" "$REPO/gateway/Dockerfile" || { echo "Dockerfile pin != gateway/VERSION"; exit 1; }
CFG_B64="$(base64 < "$RENDERED/config.yaml" | tr -d '\n')"; rm -rf "$RENDERED"
HTPASSWD_B64="$(base64 < "$REPO/deploy/htpasswd" | tr -d '\n')"

echo "== 1. ECR repo + login =="
aws ecr describe-repositories --repository-names "$IMG" --region "$REGION" >/dev/null 2>&1 \
  || aws ecr create-repository --repository-name "$IMG" --region "$REGION" >/dev/null
aws ecr get-login-password --region "$REGION" | docker login --username AWS --password-stdin "$ECR" >/dev/null

echo "== 2. build (linux/amd64 for App Runner) + push =="
docker buildx build --platform linux/amd64 --provenance=false --sbom=false --load \
  -f "$REPO/gateway/Dockerfile" --build-arg "AGW_VERSION=$AGW_VERSION" \
  --build-arg "AGW_BASE=${AGW_BASE:-$ECR/straiker-agentgateway-fork:v1.4.1-straiker}" \
  -t "$ECR/$IMG:$TAG" -t "$ECR/$IMG:latest" "$REPO"
docker push "$ECR/$IMG:$TAG" >/dev/null; docker push "$ECR/$IMG:latest" >/dev/null
echo "  pushed $ECR/$IMG:$TAG"

echo "== 3. IAM =="
ACCESS_ROLE_ARN=$(aws iam get-role --role-name AppRunnerECRAccessRole --query Role.Arn --output text)

echo "== 4. create/update App Runner service =="
ENV_JSON=$(HTPASSWD_B64_ENV="$HTPASSWD_B64" python3 - "$CFG_B64" <<'PY'
import json, os, sys
env = {
  "AGW_CONFIG_B64": sys.argv[1],
  "AGW_HTPASSWD_B64": os.environ.get("HTPASSWD_B64_ENV", ""),
  "AGENTGATEWAY_VERSION": os.environ.get("AGW_VERSION_ENV", ""),
  "STRAIKER_AGENTGATEWAY_KEY": os.environ["STRAIKER_AGENTGATEWAY_KEY"],
  "STRAIKER_AGENTGATEWAY_CODING_KEY": os.environ.get("STRAIKER_AGENTGATEWAY_CODING_KEY", ""),
  "STRAIKER_X_TOOL": os.environ.get("STRAIKER_X_TOOL", "kong-claude-code"),
  "STRAIKER_MODE": os.environ.get("STRAIKER_MODE", "block"),
  "STRAIKER_FAIL_OPEN": "true",
  "STRAIKER_DEBUG_TAP": os.environ.get("STRAIKER_DEBUG_TAP", "0"),
  "ANTHROPIC_API_KEY": os.environ["ANTHROPIC_KEY"],
  "OPENAI_API_KEY": os.environ.get("OPENAI_API_KEY", "unset"),
  "GEMINI_API_KEY": os.environ.get("GEMINI_API_KEY", "unset"),
  # agentic multi-provider surface (env-interpolated by agentgateway at load)
  "VERTEX_PROJECT": os.environ.get("VERTEX_PROJECT", ""),
  "DATABRICKS_HOST": os.environ.get("DATABRICKS_HOST", "unset"),
  "DATABRICKS_TOKEN": os.environ.get("DATABRICKS_TOKEN", "unset"),
  # Bedrock SigV4 (STS session creds; rotate when they expire)
  "AWS_ACCESS_KEY_ID": os.environ.get("AWS_ACCESS_KEY_ID", ""),
  "AWS_SECRET_ACCESS_KEY": os.environ.get("AWS_SECRET_ACCESS_KEY", ""),
  "AWS_SESSION_TOKEN": os.environ.get("AWS_SESSION_TOKEN", ""),
  "AWS_REGION": "us-east-1",
  # Azure AI Foundry
  "AZURE_OPENAI_API_KEY": os.environ.get("AZURE_OPENAI_API_KEY", "unset"),
  "AZURE_RESOURCE_NAME": os.environ.get("AZURE_RESOURCE_NAME", "unset"),
  "AZURE_OPENAI_API_VERSION": os.environ.get("AZURE_OPENAI_API_VERSION", "2025-01-01-preview"),
}
print(json.dumps(env))
PY
)
SRC=$(cat <<EOF2
{ "ImageRepository": { "ImageIdentifier":"$ECR/$IMG:$TAG", "ImageRepositoryType":"ECR",
    "ImageConfiguration":{ "Port":"3000", "RuntimeEnvironmentVariables":$ENV_JSON } },
  "AutoDeploymentsEnabled": false,
  "AuthenticationConfiguration": { "AccessRoleArn":"$ACCESS_ROLE_ARN" } }
EOF2
)
ARN=$(aws apprunner list-services --region "$REGION" --query "ServiceSummaryList[?ServiceName=='$SVC'].ServiceArn" --output text)
HEALTH='{"Protocol":"TCP","Interval":10,"Timeout":5,"HealthyThreshold":1,"UnhealthyThreshold":5}'
INSTANCE='{"Cpu":"1024","Memory":"2048"}'
if [ -n "$ARN" ]; then
  aws apprunner update-service --service-arn "$ARN" --source-configuration "$SRC" --instance-configuration "$INSTANCE" --region "$REGION" >/dev/null
  echo "  updated $ARN"
else
  ARN=$(aws apprunner create-service --service-name "$SVC" --source-configuration "$SRC" --instance-configuration "$INSTANCE" \
        --health-check-configuration "$HEALTH" --region "$REGION" --query Service.ServiceArn --output text)
  echo "  created $ARN"
fi
printf "  waiting for RUNNING"
until s=$(aws apprunner describe-service --service-arn "$ARN" --region "$REGION" --query 'Service.Status' --output text 2>/dev/null); [ "$s" != "OPERATION_IN_PROGRESS" ]; do printf "."; sleep 15; done
echo " -> $s"; [ "$s" = "RUNNING" ] || { echo "service is $s — check App Runner application logs"; exit 1; }
URL=$(aws apprunner describe-service --service-arn "$ARN" --region "$REGION" --query Service.ServiceUrl --output text)
echo "== DONE ==  https://$URL   (custom: https://$DOMAIN after ./setup_domain.sh)"
