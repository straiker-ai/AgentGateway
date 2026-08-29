# Eng/beta hand-off: extend Argus central-parse to the OpenAI coding dialects

## Context (what the SE alpha delivered)

agentgateway now guards **Claude Code** coding traffic with a **compiled-in Straiker route policy**
(`straikerCoding`, `crates/agentgateway/src/http/straiker_coding.rs`) that calls
`POST /api/v1/detect` **directly** — no sidecar, no `127.0.0.1:9000`. The gateway relays the
**verbatim** request/response and Argus **central-parse** reconstructs the coding hook events
(`UserPromptSubmit`, `PostToolUse`, `PreToolUse`) from the Anthropic wire. This works today because
central-parse already covers the Anthropic wire (`x-tool: kong-claude-code`, verified in prod).

This aligns the gateway with the product direction (central-parse) and removes the Python sidecar
from the Claude Code path. It is the SE-owned **alpha**.

## The ask (the beta Eng owns)

**Extend Argus central-parse to the OpenAI coding dialects** so the same compiled route policy can
guard them and the sidecar can be retired entirely:

- **OpenAI Chat Completions** wire (Cursor, OpenCode, Copilot).
- **OpenAI Responses** wire (Codex).

Concretely: when `POST /api/v1/detect` receives a coding turn on one of these wires (identified by an
`x-tool` value — reuse `kong-claude-code`, or register a new tag), Argus should reconstruct the same
Claude Code hook events it already produces from the Anthropic wire:
- Request phase (verbatim body) → `UserPromptSubmit` + `PostToolUse` (tool results replayed since the
  last user turn).
- Response phase (envelope, tool-call turns) → `PreToolUse` for each proposed tool call.

The wire-shape mapping is already implemented and proven in the gateway sidecar today
(`straiker/straiker_sidecar/coding_synth.py` — `_last_user_and_results_openai_chat`,
`_response_tool_calls_openai_chat`, `_prompt_openai_responses`,
`_response_tool_calls_openai_responses`). That is the reference for the parse Argus needs.

## Verified state (evidence)

- Anthropic-wire central-parse: **works in prod** (`x-tool: kong-claude-code`; verified 2026-08-19/20,
  100% recall vs Kong's 19 fixtures).
- OpenAI-dialect central-parse: **returns zero events today** — the backend has no x-tool that
  reconstructs the OpenAI Chat/Responses wires, which is why the gateway still edge-parses them in the
  sidecar.

## What flips when this lands (the migration)

1. Point the `codex`/`cursor`/`opencode`/`copilot` routes at the compiled `straikerCoding` policy
   (config-only change — swap `extProc` for `straikerCoding`, same as the `claude-code` route already
   does in `straiker/gateway/config/agentgateway.yaml`).
2. Delete the sidecar edge-parse for the OpenAI dialects; the sidecar can be dropped from the coding
   path entirely (MCP, if still sidecar-based, is separate).
3. Result: one compiled Straiker coding guard for every dialect, no local process — the **prod** shape
   Product can GA and upstream to `agentgateway/agentgateway`.

## Ownership
- **SE (alpha, done):** the compiled `straikerCoding` route policy + Claude Code central-parse relay.
- **Eng (beta, this ask):** OpenAI-dialect central-parse in Argus.
- **Product (prod):** GA central-parse + upstream the native guard and this route policy to the LF
  `agentgateway/agentgateway` repo.
