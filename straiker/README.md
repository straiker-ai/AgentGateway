# Straiker × agentgateway

Straiker Defend guardrails for [agentgateway](https://agentgateway.dev) (the Linux Foundation AI-native proxy,
ex-Solo.io) — coding agents, MCP, and agentic/chatbot traffic, enforced in-path.

**Status:** verified end-to-end against Straiker (coding agents, MCP, agentic). Runs the **forked
agentgateway binary built from this repository** (base `v1.4.1` plus the native Straiker guard), not the
stock upstream image.

## What it is

Straiker is compiled into the agentgateway binary as **first-class policies** that call Straiker's
Detect API directly — no sidecar for the primary paths:

1. **Native `straiker` guard (chatbot/agent LLM traffic).** A first-class guardrail kind
   (`crates/agentgateway/src/llm/policy/straiker.rs` + UI). In the console, Guardrails → Add guard →
   **Straiker**: paste a Straiker Defend key; every guarded prompt/response is scored inline.
2. **`straikerCoding` route policy (coding agents — Claude Code).** A first-class route policy
   (`crates/agentgateway/src/http/straiker_coding.rs` + UI, in Routes → Route policies → **Straiker
   (coding)**) attached to the Claude Code route (`/v1/messages`). It relays the **verbatim**
   request/response to `POST /api/v1/detect` (`x-tool: kong-claude-code`) and Argus central-parse
   reconstructs the hook events — **no sidecar, no `127.0.0.1:9000`.** The request-phase block stops a
   prompt before the model; a buffered tool-call response is adjudicated before the client sees it.

Both call Straiker directly through the gateway's normal egress. The Straiker mark appears on every
Straiker surface.

**Sidecar seams (the surfaces not yet on a compiled policy).** For the following, agentgateway's
external in-path seams are still served by ONE Python sidecar process:

| Seam | Transport | Carries | Straiker contract | Can block |
|---|---|---|---|---|
| **ExtProc** (Envoy ext_proc v3) | gRPC `127.0.0.1:9000` | **coding agents — OpenAI dialects only**: Codex (Responses), Cursor/OpenCode/Copilot (Chat). Edge-parsed to hook events until Argus central-parse covers these wires (see `docs/ENG-HANDOFF-central-parse-openai-dialects.md`). | `POST /api/v1/detect`, `x-tool: claude-code` (pre-formed hook events) | yes — `ImmediateResponse` |
| **ExtMCP** | gRPC `127.0.0.1:9001` | **MCP** `tools/call` (name + args) and results | synthesized `PreToolUse`/`PostToolUse` hook events | yes — JSON-RPC `-32001` |
| **LLM guardrail webhook** | HTTP `127.0.0.1:8080` | fallback path for the agentic webhook contract | `/api/v1/detect/webhook` (`kong-gateway` format) | yes — `RejectAction` |

Why the OpenAI coding dialects still use the sidecar: Argus central-parse reconstructs coding hook
events from the **Anthropic** wire today, but not yet from the OpenAI Chat/Responses wires — so those
are edge-parsed in the sidecar until the backend catches up. When it does, they move onto the same
`straikerCoding` route policy (config swap) and the sidecar drops out of the coding path entirely.

Why one container: App Runner is HTTP/1.1-only with no gRPC ingress; the remaining gRPC seams stay on loopback.

## Layout
```
straiker_sidecar/   Python 3.12 — app.py (HTTP webhook seam) · extproc.py · extmcp.py · detect_client.py · wire.py · blocking.py · config.py
gateway/            Dockerfile (agentgateway binary + sidecar) · entrypoint.sh · config/agentgateway.yaml (PoV reference, validated) · VERSION
deploy/apprunner/   deploy.sh · setup_domain.sh      (AWS App Runner reference deployment)
spec/               extproc_replay.py (gRPC replay of real Claude Code captures) · mock_detect.py · qa.py · parity_check.py
lab/                tasks_server.py (MCP demo target) · live runners
docs/               seams, latency, customer setup, engineering handoff
```

## Quickstart (local)

The gateway image is built in two steps: first the forked agentgateway binary (repository root, long Rust
build the first time), then the runtime image that layers the Straiker sidecar on top of it.

```bash
# 1. Build the forked agentgateway (from the REPOSITORY ROOT — native Straiker guard + UI, both default)
docker build -t straiker-agentgateway-fork:local .

# 2. Build the runtime image (from this straiker/ directory)
cd straiker
cp .env.example .env      # fill in STRAIKER_AGENTGATEWAY_KEY (your Straiker Defend application key)
set -a; source .env; set +a
docker build -f gateway/Dockerfile --build-arg AGW_BASE=straiker-agentgateway-fork:local \
  -t straiker-agentgateway:local .

# 3. Run it
docker run -d --name agw -p 3000:3000 -p 3001:3001 \
  -e STRAIKER_AGENTGATEWAY_KEY -e STRAIKER_MODE=block straiker-agentgateway:local

# Claude Code through it:
export ANTHROPIC_BASE_URL=http://127.0.0.1:3000 ANTHROPIC_CUSTOM_HEADERS="apikey: dev-key-1"
claude -p "reply with exactly: AGW-OK"
```

`dev-key-1` is the reference consumer key shipped in `gateway/config/agentgateway.yaml`; replace the key
list with your own before exposing the gateway anywhere.

## Contract notes
* **Day-1 `x-tool` is `kong-claude-code`** (prod accepts it; `agentgateway-claude-code` is not yet registered in
  the Straiker backend → 400). The Bearer key selects the Console app, so attribution is correct regardless. Flip
  `STRAIKER_X_TOOL` when the backend update lands.
* Buffered vs streaming is a per-route field: `responseBodyMode: buffered` (enforceable) vs
  `fullDuplexStreamed` (TTFT preserved, monitor-only). Both routes ship (`/v1/messages`, `/streaming/v1/messages`).
* Fail-open everywhere a guardrail outage could take down a developer session. Enforcement posture
  (detect/block/killswitch) is controlled in the Straiker Console per control.
