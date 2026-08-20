# agentgateway + Straiker — customer setup

Point your coding agents and apps at agentgateway; Straiker guards them inline. No endpoint hooks,
no per-developer install.

## Coding agents (Claude Code, Codex, Cursor, OpenCode, Copilot)

Each developer sets two env vars — their base URL to the gateway, and their gateway key:

```bash
# Claude Code
export ANTHROPIC_BASE_URL=https://<your-agentgateway-host>
export ANTHROPIC_CUSTOM_HEADERS="apikey: <your-gateway-key>"
```
The developer's own Anthropic/Bedrock credential still flows to the model — the gateway holds no LLM
key for coding routes; it only adds guardrails and identity. Enforcement mode (detect / block /
killswitch) is set per control in the Straiker Console, not on the gateway.

Streaming vs enforceable is a route choice:
- `https://<host>/v1/messages` — buffered, can block a dangerous tool call **before it runs**.
- `https://<host>/streaming/v1/messages` — tokens stream (lowest latency), monitor-only.

Other agents: `…/codex/v1` (OpenAI Responses), `…/cursor|opencode|copilot/v1` (OpenAI Chat).

## MCP

Front your MCP servers with agentgateway's `/mcp` endpoint. Every `tools/call` is scored before it
reaches the server (and its result before the model sees it); a blocked call returns a JSON-RPC error.

## Chatbots / agents (Vertex/Gemini, ChatGPT-style, Databricks, custom)

Use agentgateway's LLM API (`/v1/chat/completions`, `/v1/responses`). The gateway calls Straiker's
guardrail on the prompt and the response and blocks with your configured message.

## What lands in the Console

One **AgentGateway** application (selected by the Straiker key). Coding turns show prompt + tool calls
with categories (RCE, Destructive Command, Suspicious Outbound, IPI, MCP). Identity is the developer's
gateway key. All three surfaces — coding, MCP, chatbot — attribute to the same app.
