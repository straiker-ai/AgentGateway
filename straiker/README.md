# Straiker × agentgateway

Straiker Defend guardrails for [agentgateway](https://agentgateway.dev) (the Linux Foundation AI-native proxy,
ex-Solo.io) — coding agents, MCP, and agentic/chatbot traffic, enforced in-path.

**Status:** working end-to-end locally against the Straiker demo tenant (app `AgentGateway`). Shared
instance: `your gateway host` (App Runner). Pinned to `ghcr.io/agentgateway/agentgateway:v1.4.1`.

## What it is

agentgateway has no WASM/Lua/plugin API — policies are compiled into the Rust binary. What it has are
**external in-path seams**, and we use three of them from ONE sidecar process:

| Seam | Transport | Carries | Straiker contract | Can block |
|---|---|---|---|---|
| **ExtProc** (Envoy ext_proc v3) | gRPC `127.0.0.1:9000` | **coding agents** — Claude Code, Codex, Cursor, OpenCode, Copilot. Raw verbatim bytes both ways. | central-parse: `POST /api/v1/detect`, `x-tool: kong-claude-code`, `x-straiker-phase: request\|response\|response-sync` | yes — `ImmediateResponse` with a well-formed Anthropic/OpenAI block body |
| **ExtMCP** | gRPC `127.0.0.1:9001` | **MCP** `tools/call` (name + args) and results | synthesized `PreToolUse`/`PostToolUse` hook events | yes — JSON-RPC `-32001` |
| **LLM guardrail webhook** | HTTP `127.0.0.1:8080` | **agentic/chatbot** — GCP Vertex/Gemini, OpenAI/ChatGPT-style, Databricks, custom + productivity agents | `/api/v1/detect/webhook` (`kong-gateway` format) | yes — `RejectAction` |

Why not the guardrail webhook for coding agents: it flattens content to `role`+`content` strings, destroying
`tool_use`/`tool_result`. Central-parse needs the verbatim body. ExtProc is byte-for-byte Kong's relay shape.

Why one container: App Runner is HTTP/1.1-only with no gRPC ingress; gRPC stays on loopback.

## Layout
```
straiker_sidecar/   Python 3.12 — app.py (HTTP webhook seam) · extproc.py · extmcp.py · detect_client.py · wire.py · blocking.py · config.py
gateway/            Dockerfile (agentgateway binary + sidecar) · entrypoint.sh · config/agentgateway.yaml (PoV reference, validated) · VERSION
deploy/apprunner/   deploy.sh · setup_domain.sh      (forked from )
spec/               extproc_replay.py (gRPC replay of real Claude Code captures) · mock_detect.py · qa.py · parity_check.py
lab/                tasks_server.py (MCP demo target) · live runners
docs/               seams, latency, customer setup, engineering handoff
```

## Quickstart (local)
```bash
set -a; source "Straiker Projects/.env"; set +a          # STRAIKER_AGENTGATEWAY_KEY, ANTHROPIC_KEY
docker build -f gateway/Dockerfile -t straiker-agentgateway:local .
docker run -d --name agw -p 3000:3000 -p 3001:3001 -e STRAIKER_AGENTGATEWAY_KEY -e STRAIKER_MODE=block straiker-agentgateway:local
# Claude Code through it:
export ANTHROPIC_BASE_URL=http://127.0.0.1:3000 ANTHROPIC_CUSTOM_HEADERS="apikey: dev-key-1"
claude -p "reply with exactly: AGW-OK"
```

## Contract notes
* **Day-1 `x-tool` is `kong-claude-code`** (prod accepts it; `agentgateway-claude-code` is not yet registered in
  the Straiker backend → 400). The Bearer key selects the Console app, so attribution is correct regardless. Flip
  `STRAIKER_X_TOOL` when the backend update lands.
* Buffered vs streaming is a per-route field: `responseBodyMode: buffered` (enforceable) vs
  `fullDuplexStreamed` (TTFT preserved, monitor-only). Both routes ship (`/v1/messages`, `/streaming/v1/messages`).
* Fail-open everywhere a guardrail outage could take down a developer session. Enforcement posture
  (detect/block/killswitch) is controlled in the Straiker Console per control.
