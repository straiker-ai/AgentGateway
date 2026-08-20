# Changelog

## 0.1.0 — 2026-08-19
Initial Straiker × agentgateway integration (demo tenant, app `AgentGateway`). Pinned to
`ghcr.io/agentgateway/agentgateway:v1.4.1`.
- **ExtProc** seam (coding agents, central-parse `x-tool: kong-claude-code`): Claude Code end-to-end;
  Codex/Cursor/OpenCode/Copilot routes; buffered (enforceable) + streaming presets. Parity 100% recall
  vs the the recorded fixtures. Detect p50 ~190ms / p95 ~265ms.
- **ExtMCP** seam (MCP tools/call + results → PreToolUse/PostToolUse; block → JSON-RPC -32001). Measured
  the undocumented v1.4.1 params-only/result-only contract; pair by (session, service) FIFO.
- **LLM guardrail webhook** seam (agentic/chatbot → /detect/webhook, pre_call + post_call).
- One container (App Runner has no gRPC ingress; gRPC on loopback). `enableIpv6:false` so it binds IPv4.
- Gates: `spec/qa.py` (50 asserts + relay byte-identity), `spec/parity_check.py`, `spec/latency.py`,
  `promote.sh` (config validates against the pinned binary), `live_qa.sh`.
