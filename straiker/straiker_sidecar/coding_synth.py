"""Edge-parse OpenAI-dialect coding agents into Claude Code hook events.

Argus central-parse (``x-tool: kong-claude-code``) synthesizes coding-agent events from the
Anthropic wire (Claude Code) but not from the OpenAI dialects. Codex (OpenAI Responses),
Cursor and OpenCode (OpenAI Chat Completions) therefore need the events synthesized at the
gateway and posted on the native contract (``x-tool: claude-code``), the same approach the Kong
plugin and the LiteLLM guardrail use.

One request+response cycle yields, in order:
  UserPromptSubmit  - the latest user message in the request
  PostToolUse       - each tool result the request replays since the last user turn
  PreToolUse        - each tool call the response proposes (the enforceable point)
  Stop              - the final assistant answer, when the response carries no tool call

The functions are pure: they take parsed JSON and return a list of hook-event dicts.
"""
from __future__ import annotations

import json
from typing import Any


def _mcp_split(name: str | None):
    if isinstance(name, str) and name.startswith("mcp__"):
        srv, _, tool = name[5:].partition("__")
        if tool:
            return srv, tool
    return None, None


def _args_obj(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            v = json.loads(raw)
            return v if isinstance(v, dict) else {"_raw": raw}
        except ValueError:
            return {"_raw": raw}
    return {}


def _pre_tool(session, tid, name, args) -> dict:
    ev = {"hook_event_name": "PreToolUse", "session_id": session, "tool_name": name,
          "tool_input": _args_obj(args), "tool_use_id": tid}
    srv, tool = _mcp_split(name)
    if srv:
        ev["mcp_server_name"], ev["mcp_tool_name"] = srv, tool
    return ev


def _post_tool(session, tid, name, result) -> dict:
    if isinstance(result, (dict, list)):
        result = json.dumps(result)
    return {"hook_event_name": "PostToolUse", "session_id": session, "tool_name": name,
            "tool_input": {}, "tool_use_id": tid,
            "tool_response": {"content": result if isinstance(result, str) else str(result)},
            "is_error": False}


def _last_user_and_results_openai_chat(messages: list[dict]):
    """Return (last user text, [tool results after it]) from OpenAI Chat messages."""
    last_user_idx = -1
    for i, m in enumerate(messages):
        if isinstance(m, dict) and m.get("role") == "user":
            last_user_idx = i
    prompt = ""
    if last_user_idx >= 0:
        c = messages[last_user_idx].get("content")
        prompt = c if isinstance(c, str) else "\n".join(
            p.get("text", "") for p in c if isinstance(p, dict) and p.get("type") in ("text", "input_text")
        ) if isinstance(c, list) else ""
    results = []
    for m in messages[last_user_idx + 1:]:
        if isinstance(m, dict) and m.get("role") in ("tool", "function"):
            results.append((m.get("tool_call_id"), m.get("name") or m.get("tool_name"), m.get("content")))
    return prompt, results


def _response_tool_calls_openai_chat(resp_json: dict):
    """(list of (id,name,args) proposed tool calls, final assistant text or None)."""
    calls, answer = [], None
    for ch in resp_json.get("choices") or []:
        msg = (ch or {}).get("message") or {}
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            calls.append((tc.get("id"), fn.get("name"), fn.get("arguments")))
        if isinstance(msg.get("content"), str) and msg["content"]:
            answer = msg["content"]
    return calls, answer


def _response_tool_calls_openai_responses(resp_json: dict):
    """Codex Responses: output items of type function_call, else output_text."""
    calls, answer_parts = [], []
    for item in resp_json.get("output") or []:
        if not isinstance(item, dict):
            continue
        t = item.get("type")
        if t == "function_call":
            calls.append((item.get("call_id") or item.get("id"), item.get("name"), item.get("arguments")))
        elif t == "message":
            for c in item.get("content") or []:
                if isinstance(c, dict) and c.get("type") in ("output_text", "text"):
                    answer_parts.append(c.get("text", ""))
    return calls, ("\n".join(answer_parts) or None)


def _prompt_openai_responses(req_json: dict):
    """(last user text, [tool results]) from a Codex Responses request `input`."""
    inp = req_json.get("input")
    if isinstance(inp, str):
        return inp, []
    prompt, results = "", []
    for item in inp or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "function_call_output":
            results.append((item.get("call_id"), None, item.get("output")))
        elif item.get("role") == "user":
            c = item.get("content")
            prompt = c if isinstance(c, str) else "\n".join(
                p.get("text", "") for p in c if isinstance(p, dict) and p.get("type") in ("input_text", "text")
            ) if isinstance(c, list) else prompt
    return prompt, results


def synth_events(req_json: dict, resp_json: dict | None, fmt: str, session_id: str,
                 user: str | None, model: str | None) -> list[dict]:
    """Build ordered Claude Code hook events for one OpenAI-dialect coding turn."""
    if fmt == "openai-responses":
        prompt, results = _prompt_openai_responses(req_json or {})
        calls, answer = _response_tool_calls_openai_responses(resp_json or {})
    else:  # openai-chat (cursor / opencode)
        prompt, results = _last_user_and_results_openai_chat((req_json or {}).get("messages") or [])
        calls, answer = _response_tool_calls_openai_chat(resp_json or {})

    events: list[dict] = []
    if prompt.strip():
        events.append({"hook_event_name": "UserPromptSubmit", "session_id": session_id,
                       "user_name": user, "prompt": prompt})
    for tid, name, result in results:
        events.append(_post_tool(session_id, tid, name, result))
    for tid, name, args in calls:
        events.append(_pre_tool(session_id, tid, name, args))
    if answer and not calls:
        ev = {"hook_event_name": "Stop", "session_id": session_id, "app_response": answer,
              "stop_reason": "end_turn"}
        if model:
            ev["model"] = model
        events.append(ev)
    for e in events:
        if user and "user_name" not in e:
            e["user_name"] = user
    return events
