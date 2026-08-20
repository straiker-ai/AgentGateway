# agentgateway extension seams — what Straiker uses and why

agentgateway (Rust, Linux Foundation, ex-Solo.io) has **no WASM, no Lua, no dynamically-loaded
plugin API** — its built-in policies are compiled into the binary. Third-party guardrails attach
through *external* in-path seams. There are four; Straiker uses three, from **one sidecar process**.

| Seam | Transport | What we receive | Blocks | Straiker uses it for |
|---|---|---|---|---|
| **ExtProc** (Envoy `ext_proc` v3) | gRPC | raw verbatim request+response bytes, both directions, on one bidi stream | yes — `ImmediateResponse` | **coding agents** (Claude Code, Codex, Cursor, OpenCode, Copilot) |
| **ExtMCP** (`ext_mcp.proto`) | gRPC | raw JSON-RPC: `tools/call` name+args, and the tool result | yes — `AuthorizationError` → JSON-RPC `-32001` | **MCP** tool calls + results |
| **LLM guardrail webhook** | HTTP `/request` `/response` | provider-normalized `{messages:[{role,content}]}` / `{choices:[…]}` | yes — `RejectAction` | **agentic / chatbot** (Vertex/Gemini, ChatGPT-style, Databricks, custom) |
| ExtAuthz | gRPC/HTTP | request only, body ≤ 8KB | allow/deny | *not used — too weak* |

## Why coding agents MUST use ExtProc, not the guardrail webhook

The guardrail webhook flattens content to `role`+`content` strings. That destroys `tool_use` /
`tool_result` / `tools[]` — the exact blocks Straiker's central-parse (`x-tool: kong-claude-code`,
`POST /api/v1/detect`) walks to synthesize `PreToolUse`/`PostToolUse` events. RCE-in-Bash and
IPI-in-tool-result are structurally unreachable on that seam.

ExtProc hands us the **verbatim bytes** on request and response. That is byte-for-byte the shape
Kong's `access`+`response` relay produces, so the same the Straiker backend pipeline sees the same events. Proven:
`spec/parity_check.py` replays the 19 real Kong captures through this seam to prod and gets **100%
recall** of every native hook event.

## Buffered vs streaming is one config field

ExtProc body mode is per-route YAML: `responseBodyMode: buffered` (we hold the response and can
block a tool call **before the client executes it** — `x-straiker-phase: response-sync`, `enforce=True`)
vs `fullDuplexStreamed` (tokens stream, we score async, monitor-only). Both routes ship:
`/v1/messages` (buffered, enforceable) and `/streaming/v1/messages` (streamed, TTFT preserved). In
Kong this required two separate plugins because OpenResty decides buffering before plugin code runs.

## Identity

CEL header/attribute injection on each seam turns the authenticated apiKey into `x-straiker-user`
(`requestAttributes: { x-straiker-user: 'apiKey.user' }` on ExtProc, `metadata: { user: 'apiKey.user' }`
on ExtMCP, `headers` on the webhook). The Console shows the real developer, not a generic gateway user.
Identity must be **stable per session** or the backend's event trace forks; it comes from the apiKey,
never from anything per-request.

## Deployment: one container

App Runner exposes one HTTP/1.1 port and has **no gRPC ingress**. Two of the three seams are gRPC, so
agentgateway and the sidecar run in the same container: agentgateway binds the public port (3000), the
sidecar binds `127.0.0.1:8080` (HTTP) / `:9000` (ExtProc) / `:9001` (ExtMCP). gRPC never crosses the
public boundary. (Build note: push a **single-arch** image — App Runner cannot run an OCI image index,
which buildx emits by default via attestation manifests. `--provenance=false --sbom=false`.)
