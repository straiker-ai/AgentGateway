"""Block bodies the CLIENT will actually render. Ported from kong straiker-coding/core.lua.

The two client-rendering fixes carried here:
* a streaming Anthropic client needs a COMPLETE, well-formed SSE message (message_start with
  content=[] — an empty ARRAY not {} — through message_stop) or Claude Code discards the body
  and keeps talking;
* the reason must be non-empty readable text (Straiker may return reason=null).
"""
from __future__ import annotations

import json

DEFAULT_REASON = "Request blocked by Straiker guardrails."


def _reason(r: str | None) -> str:
    return r if isinstance(r, str) and r.strip() else DEFAULT_REASON


def anthropic_json(reason: str | None) -> bytes:
    return json.dumps({
        "id": "msg_blocked", "type": "message", "role": "assistant", "model": "claude",
        "stop_reason": "end_turn", "stop_sequence": None,
        "content": [{"type": "text", "text": _reason(reason)}],
        "usage": {"input_tokens": 0, "output_tokens": 0},
    }).encode()


def anthropic_sse(reason: str | None) -> bytes:
    def ev(name: str, data: dict) -> str:
        return f"event: {name}\ndata: {json.dumps(data)}\n\n"
    r = _reason(reason)
    return "".join([
        ev("message_start", {"type": "message_start", "message": {
            "id": "msg_blocked", "type": "message", "role": "assistant", "model": "claude",
            "content": [], "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0}}}),
        ev("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
        ev("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": r}}),
        ev("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ev("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 0}}),
        ev("message_stop", {"type": "message_stop"}),
    ]).encode()


def openai_chat_json(reason: str | None) -> bytes:
    return json.dumps({
        "id": "chatcmpl-blocked", "object": "chat.completion", "created": 0, "model": "straiker",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": _reason(reason)}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }).encode()


def openai_chat_sse(reason: str | None) -> bytes:
    r = _reason(reason)
    chunk = {"id": "chatcmpl-blocked", "object": "chat.completion.chunk", "created": 0, "model": "straiker",
             "choices": [{"index": 0, "delta": {"role": "assistant", "content": r}, "finish_reason": None}]}
    done = {"id": "chatcmpl-blocked", "object": "chat.completion.chunk", "created": 0, "model": "straiker",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
    return f"data: {json.dumps(chunk)}\n\ndata: {json.dumps(done)}\n\ndata: [DONE]\n\n".encode()


def openai_responses_json(reason: str | None) -> bytes:
    r = _reason(reason)
    return json.dumps({
        "id": "resp_blocked", "object": "response", "created_at": 0, "status": "completed", "model": "straiker",
        "output": [{"type": "message", "id": "msg_blocked", "status": "completed", "role": "assistant",
                    "content": [{"type": "output_text", "text": r, "annotations": []}]}],
        "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
    }).encode()


def openai_responses_sse(reason: str | None) -> bytes:
    r = _reason(reason)
    resp = json.loads(openai_responses_json(r))
    evs = [
        ("response.created", {"type": "response.created", "response": {**resp, "status": "in_progress", "output": []}}),
        ("response.output_text.delta", {"type": "response.output_text.delta", "item_id": "msg_blocked", "output_index": 0, "content_index": 0, "delta": r}),
        ("response.completed", {"type": "response.completed", "response": resp}),
    ]
    return "".join(f"event: {n}\ndata: {json.dumps(d)}\n\n" for n, d in evs).encode()


def render(fmt: str, stream: bool, reason: str | None) -> tuple[bytes, str]:
    """-> (body, content-type) for the client's wire format."""
    if fmt == "openai-chat":
        return (openai_chat_sse(reason), "text/event-stream") if stream else (openai_chat_json(reason), "application/json")
    if fmt == "openai-responses":
        return (openai_responses_sse(reason), "text/event-stream") if stream else (openai_responses_json(reason), "application/json")
    # anthropic, bedrock(anthropic-shaped JSON when non-stream), unknown -> anthropic shape
    return (anthropic_sse(reason), "text/event-stream") if stream else (anthropic_json(reason), "application/json")
