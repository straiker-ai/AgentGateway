"""Pure wire helpers: identity, session, model, stream flag, SSE/JSON response parsing.

No network, no gRPC — unit-testable against the Kong fixture corpus.

Identity MUST be stable for a whole session: the backend keys the event trace on
(straiker_id, app_id, user_name, session_id). A value that changes mid-session splits one
conversation into traces that never pair a prompt with its tool calls. Resolution order
mirrors the gateway_coding_agent driver: explicit header > agentgateway apiKey metadata
(CEL-injected) > default. Never derive from anything per-request.
"""
from __future__ import annotations

import json
import re
from typing import Any

# ---- session -----------------------------------------------------------------------------

def session_id_from(headers: dict[str, str], body: dict[str, Any] | None) -> str | None:
    """Claude Code: header x-claude-code-session-id, else metadata.user_id packed as a JSON
    string {"device_id","session_id"}; Codex: session-id / session_id; OpenCode: x-session-id."""
    for h in ("x-claude-code-session-id", "session-id", "session_id", "x-session-id", "x-straiker-session-id"):
        v = headers.get(h)
        if v:
            return v.strip()
    if not body:
        return None
    md = body.get("metadata")
    if isinstance(md, dict):
        uid = md.get("user_id")
        if isinstance(uid, str):
            try:
                j = json.loads(uid)
                if isinstance(j, dict) and isinstance(j.get("session_id"), str):
                    return j["session_id"]
            except ValueError:
                pass
        for k in ("session_id", "sessionId"):
            if isinstance(md.get(k), str):
                return md[k]
    # OpenAI-format callers often carry `user`; treat a stable `user` as a session anchor only
    # when nothing better exists (Codex/OpenCode set proper headers above).
    return None


def user_from(headers: dict[str, str], default: str) -> str:
    for h in ("x-straiker-user", "x-consumer-username", "x-straiker-user-name"):
        v = headers.get(h)
        if v and v.strip():
            return v.strip()
    return default


# ---- request body ------------------------------------------------------------------------

def parse_json(raw: bytes) -> dict[str, Any] | None:
    try:
        j = json.loads(raw)
        return j if isinstance(j, dict) else None
    except ValueError:
        return None


def wants_stream(body: dict[str, Any] | None) -> bool:
    return bool(body and body.get("stream") is True)


def model_of(body: dict[str, Any] | None, headers: dict[str, str], path: str = "") -> str | None:
    if body and isinstance(body.get("model"), str):
        return body["model"]
    v = headers.get("x-straiker-model")
    if v:
        return v
    # Bedrock: model is a URL path segment — /model/<id>/invoke[-with-response-stream]
    m = re.search(r"/model/([^/]+)/", path or "")
    return m.group(1) if m else None


def wire_format(path: str, body: dict[str, Any] | None) -> str:
    """anthropic | openai-chat | openai-responses | bedrock | unknown — used only for block-body
    rendering and logging. the Straiker backend owns real wire-format detection on the central-parse path."""
    p = path or ""
    if "/v1/messages" in p or (body and "max_tokens" in body and isinstance(body.get("messages"), list) and "system" in body):
        return "anthropic"
    if "/v1/responses" in p or (body and "input" in body and "messages" not in body):
        return "openai-responses"
    if "/chat/completions" in p or (body and isinstance(body.get("messages"), list) and any(isinstance(m, dict) and m.get("role") == "system" for m in body.get("messages", []))):
        return "openai-chat"
    if "/model/" in p and "/invoke" in p:
        return "bedrock"
    if body and isinstance(body.get("messages"), list):
        return "anthropic" if "max_tokens" in body else "openai-chat"
    return "unknown"


# ---- response body -----------------------------------------------------------------------

def is_sse(headers: dict[str, str], raw: bytes) -> bool:
    ct = (headers.get("content-type") or "").lower()
    if "text/event-stream" in ct:
        return True
    head = raw[:64].lstrip()
    return head.startswith(b"event:") or head.startswith(b"data:")


def response_has_tool_use(raw: bytes, sse: bool) -> bool:
    """Cheap pre-check: does this response contain a tool call at all? Lets the ExtProc path
    skip the synchronous response-sync round trip on pure-text turns (the latency lever)."""
    if sse:
        return b'"tool_use"' in raw or b'"tool_calls"' in raw or b'"function_call"' in raw
    j = parse_json(raw)
    if not j:
        return False
    content = j.get("content")
    if isinstance(content, list) and any(isinstance(c, dict) and c.get("type") == "tool_use" for c in content):
        return True
    for ch in j.get("choices") or []:
        msg = (ch or {}).get("message") or {}
        if msg.get("tool_calls"):
            return True
    for item in j.get("output") or []:  # OpenAI Responses
        if isinstance(item, dict) and item.get("type") == "function_call":
            return True
    return False


def response_envelope(phase: str, raw: bytes, model: str | None, request_body: dict[str, Any] | None) -> bytes:
    """The response-phase body for central-parse:
    {"straiker_phase": <phase>, "sse": <buffered SSE or JSON body AS A STRING>, "model": ..., "request": {...}}
    `request` lets the Straiker backend gate sidecars (title-gen/suggestion calls) and copy system_prompt +
    subagent identity onto response-side events. Always attach it — we hold it in hand."""
    env: dict[str, Any] = {"straiker_phase": phase, "sse": raw.decode("utf-8", "replace")}
    if model:
        env["model"] = model
    if request_body is not None:
        env["request"] = request_body
    return json.dumps(env, separators=(",", ":"), ensure_ascii=False).encode()


# ---- agentic mapping: OpenAI/Anthropic body -> Straiker agentic `messages` (full trace) ---------

def _flatten_openai_tool_calls(tcs):
    """OpenAI nested {id,type,function:{name,arguments(str)}} -> Straiker flat {id,name,input(obj)}."""
    out = []
    for tc in tcs or []:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") or {}
        name = fn.get("name") or tc.get("name")
        args = fn.get("arguments") if "arguments" in fn else tc.get("input")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                args = {"_raw": args}
        out.append({"id": tc.get("id"), "name": name, "input": args if args is not None else {}})
    return out


def _mcp_tool_name(name):
    """mcp__server__tool -> ('server','tool'); else (None, name)."""
    if isinstance(name, str) and name.startswith("mcp__"):
        srv, _, tool = name[5:].partition("__")
        if tool:
            return srv, tool
    return None, name


def openai_chat_to_agentic(body):
    """Straiker agentic `messages` from an OpenAI Chat Completions body. Preserves the FULL trace:
    system prompt, user turns, assistant tool_calls (flattened), tool results, MCP tool names."""
    msgs = []
    for m in body.get("messages") or []:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        out = {"role": role}
        content = m.get("content")
        if isinstance(content, list):
            out["content"] = "\n".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") in ("text", "input_text"))
        elif content is not None:
            out["content"] = content
        if m.get("tool_calls"):
            tcs = _flatten_openai_tool_calls(m["tool_calls"])
            out["tool_calls"] = tcs
            for tc in tcs:
                srv, tool = _mcp_tool_name(tc.get("name"))
                if srv:
                    tc["mcp_server_name"], tc["mcp_tool_name"] = srv, tool
        if role in ("tool", "function"):
            if m.get("tool_call_id"):
                out["tool_call_id"] = m["tool_call_id"]
            tn = m.get("name") or m.get("tool_name")
            if tn:
                srv, tool = _mcp_tool_name(tn)
                out["tool_name"] = tn
                if srv:
                    out["mcp_server_name"], out["mcp_tool_name"] = srv, tool
        msgs.append(out)
    return msgs, body.get("tools") or []


def anthropic_to_agentic(body):
    """Same, from an Anthropic Messages body (system top-level; content blocks tool_use/tool_result)."""
    msgs = []
    sys = body.get("system")
    if isinstance(sys, str) and sys:
        msgs.append({"role": "system", "content": sys})
    elif isinstance(sys, list):
        msgs.append({"role": "system", "content": "\n".join(b.get("text", "") for b in sys if isinstance(b, dict) and b.get("type") == "text")})
    for m in body.get("messages") or []:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        c = m.get("content")
        if isinstance(c, str):
            msgs.append({"role": role, "content": c})
            continue
        text_parts, tcs, tool_results = [], [], []
        for b in c if isinstance(c, list) else []:
            if not isinstance(b, dict):
                continue
            bt = b.get("type")
            if bt == "text":
                text_parts.append(b.get("text", ""))
            elif bt == "tool_use":
                srv, tool = _mcp_tool_name(b.get("name"))
                tc = {"id": b.get("id"), "name": b.get("name"), "input": b.get("input") or {}}
                if srv:
                    tc["mcp_server_name"], tc["mcp_tool_name"] = srv, tool
                tcs.append(tc)
            elif bt == "tool_result":
                rc = b.get("content")
                if isinstance(rc, list):
                    rc = "\n".join(x.get("text", "") for x in rc if isinstance(x, dict) and x.get("type") == "text")
                tool_results.append({"role": "tool", "tool_call_id": b.get("tool_use_id"), "content": rc if isinstance(rc, str) else json.dumps(rc)})
        out = {"role": role}
        if text_parts:
            out["content"] = "\n".join(text_parts)
        if tcs:
            out["tool_calls"] = tcs
        if len(out) > 1:  # skip a bare tool_result-only turn (no text, no tool_calls)
            msgs.append(out)
        msgs.extend(tool_results)
    return msgs, body.get("tools") or []


def answer_text(raw: bytes, sse: bool) -> str:
    """Assistant answer text from a response body (OpenAI/Anthropic, SSE or JSON) for the agentic trace."""
    if sse:
        import re as _re
        parts = []
        for line in raw.split(b"\n"):
            if not line.startswith(b"data:"):
                continue
            payload = line[5:].strip()
            if payload in (b"[DONE]", b""):
                continue
            try:
                d = json.loads(payload)
            except ValueError:
                continue
            for ch in d.get("choices") or []:
                delta = (ch or {}).get("delta") or {}
                if isinstance(delta.get("content"), str):
                    parts.append(delta["content"])
            de = d.get("delta") or {}
            if d.get("type") == "content_block_delta" and de.get("type") == "text_delta":
                parts.append(de.get("text", ""))
        return "".join(parts)
    j = parse_json(raw)
    if not j:
        return ""
    for ch in j.get("choices") or []:
        msg = (ch or {}).get("message") or {}
        if isinstance(msg.get("content"), str):
            return msg["content"]
    if isinstance(j.get("content"), list):
        return "\n".join(b.get("text", "") for b in j["content"] if isinstance(b, dict) and b.get("type") == "text")
    return ""
