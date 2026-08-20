"""HTTP side of the sidecar: agentgateway's LLM GUARDRAIL WEBHOOK seam + health + debug tap.

This is the AGENTIC / CHATBOT surface (GCP Vertex/Gemini, ChatGPT, Databricks, custom and
productivity agents). agentgateway POSTs a flattened, provider-agnostic payload:
  /agentic/request   {"body":{"messages":[{"role","content"}...]}}
  /agentic/response  {"body":{"choices":[{"message":{"role","content"}}...]}}
and we answer {"action": {...}}: {"reason"} = pass, {"body","status_code","reason"} = reject.

Why this seam is right for this surface and wrong for coding agents: role+content is
exactly what the /detect/webhook (kong-gateway format) contract consumes — but tool_use /
tool_result are flattened away, so coding agents go through ExtProc instead.

Identity/session/model arrive as CEL-injected headers from agentgateway config:
  x-straiker-user, x-straiker-session, x-straiker-model, x-straiker-app (optional key selector).
"""
from __future__ import annotations

import asyncio
import collections
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

import extmcp
import extproc
import wire
from config import Settings
from detect_client import DetectClient, block_reason, is_block

log = logging.getLogger("straiker.app")
VERSION = "0.1.0"

# ---- app-key selector: x-straiker-app header (CEL apiKey.metadata.straiker_app) -> env STRAIKER_APP_<NAME>_KEY
def _key_for_app(settings: Settings, app: str | None, default: str) -> str:
    return settings.key_for(app, default)


def _last_user_text(messages: list[dict[str, Any]]) -> str:
    for m in reversed(messages or []):
        if isinstance(m, dict) and m.get("role") == "user":
            c = m.get("content")
            if isinstance(c, str):
                return c
            if isinstance(c, list):
                return "\n".join(p.get("text", "") for p in c if isinstance(p, dict) and p.get("type") == "text")
    return ""


def _choices_text(choices: list[dict[str, Any]]) -> str:
    out = []
    for ch in choices or []:
        msg = (ch or {}).get("message") or {}
        c = msg.get("content")
        if isinstance(c, str):
            out.append(c)
        elif isinstance(c, list):
            out.extend(p.get("text", "") for p in c if isinstance(p, dict))
    return "\n".join(out)


def webhook_payload(event_type: str, *, body: Any, text: str, user: str, session_id: str, client_ip: str,
                    model: str | None, llm_format: str | None, consumer: dict[str, Any] | None = None,
                    response: dict[str, Any] | None = None) -> dict[str, Any]:
    """The kong-gateway webhook envelope (mirrors kong straiker/helpers.lua build_webhook_payload)."""
    p: dict[str, Any] = {
        "eventType": event_type,
        "request": {"body": body, "text": text},
        "userInfo": {"id": user, "role": "public"},
        "consumer": consumer or {"id": user, "username": user, "custom_id": user},
        "metadata": {"session_id": session_id, "client_ip": client_ip},
        "aiContext": {"llm_format": llm_format or "openai", "route_type": "llm/v1/chat", "genai_category": "text/generation", "model": model or "unknown"},
    }
    if response is not None:
        p["response"] = response
    return p


class State:
    def __init__(self) -> None:
        self.settings = Settings()
        self.client: httpx.AsyncClient | None = None
        self.detect: DetectClient | None = None
        self.tap: collections.deque = collections.deque(maxlen=200)
        self.grpc_servers: list = []
        # request-side memory so the /response webhook can be paired with its prompt:
        # (user, session) -> last prompt text + body. Small TTL map.
        self.last_prompt: dict[str, tuple[float, str, Any, str | None]] = {}


state = State()


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = state.settings
    state.client = httpx.AsyncClient(timeout=s.detect_timeout, limits=httpx.Limits(max_connections=200, max_keepalive_connections=50))
    state.detect = DetectClient(s, state.client)
    tap = state.tap if s.debug_tap else None
    # gRPC seams share this event loop and this DetectClient (one process, three seams)
    state.grpc_servers = [await extproc.serve(s, state.detect, tap), await extmcp.serve(s, state.detect, tap)]
    log.info("straiker sidecar v%s up: http=%s:%s extproc=%s extmcp=%s detect=%s x_tool=%s mode=%s fail_open=%s",
             VERSION, s.http_host, s.http_port, s.extproc_port, s.extmcp_port, s.detect_url, s.x_tool, s.mode, s.fail_open)
    try:
        yield
    finally:
        for g in state.grpc_servers:
            await g.stop(grace=2)
        await state.client.aclose()


app = FastAPI(title="Straiker agentgateway sidecar", version=VERSION, lifespan=lifespan)


_LANDING = ""
try:
    with open(os.path.join(os.path.dirname(__file__), "landing.html")) as _f:
        _LANDING = _f.read()
except OSError:
    _LANDING = "<h1>Straiker AI Security Gateway</h1>"


@app.get("/landing")
async def landing():
    from fastapi.responses import HTMLResponse
    return HTMLResponse(_LANDING)


@app.get("/health")
@app.get("/ready")
async def health():
    return {"ok": True, "version": VERSION, "mode": state.settings.mode, "x_tool": state.settings.x_tool}


@app.get("/version")
async def version():
    return {"version": VERSION, "agentgateway_pin": os.environ.get("AGENTGATEWAY_VERSION", "v1.4.1")}


@app.get("/debug/last")
async def debug_last():
    if not state.settings.debug_tap:
        return JSONResponse({"error": "set STRAIKER_DEBUG_TAP=1"}, status_code=404)
    return list(state.tap)


def _pass(reason: str = "allow") -> dict[str, Any]:
    return {"action": {"reason": reason}}


def _reject(reason: str, status: int = 403) -> dict[str, Any]:
    return {"action": {"body": reason, "status_code": status, "reason": reason}}


def _ctx(req: Request) -> tuple[str, str, str | None, str | None, str]:
    h = {k.lower(): v for k, v in req.headers.items()}
    s = state.settings
    user = h.get("x-straiker-user") or h.get("x-consumer-username") or s.default_user
    sess = h.get("x-straiker-session") or h.get("x-claude-code-session-id") or h.get("x-session-id") or f"agw-{user}"
    return user, sess, h.get("x-straiker-model"), h.get("x-straiker-app"), (req.client.host if req.client else "127.0.0.1")


def _remember(key: str, text: str, body: Any, model: str | None) -> None:
    now = time.monotonic()
    state.last_prompt[key] = (now, text, body, model)
    if len(state.last_prompt) > 10000:
        for k in [k for k, (t, *_ ) in state.last_prompt.items() if now - t > 1800]:
            state.last_prompt.pop(k, None)


def _source(req: Request, user: str, app_name: str | None) -> str:
    """App identity for /detect?agentic. Client names its app via x-straiker-source; else the
    per-consumer app metadata; else a stable default. Same precedence as the ExtProc path."""
    h = {k.lower(): v for k, v in req.headers.items()}
    return h.get("x-straiker-source") or app_name or user or "agentgateway"


def _kong_webhook(event_type: str, *, llm_body: dict, text: str, resp_body: dict | None, resp_text: str | None,
                  user: str, sess: str, src: str, model: str | None, ip: str) -> dict:
    """The kong-gateway webhook envelope — the SAME contract litellm and portkey use. The backend
    correlates a pre_call (request only) with its post_call (request + response) by session into ONE
    turn in the Console: input guardrail (incoming) + output guardrail (outbound), not two cards.
    `metadata.source` auto-enumerates the app (agentic, type AI)."""
    payload: dict = {
        "eventType": event_type,
        "request": {"body": llm_body, "text": text},
        "userInfo": {"id": user, "role": "public"},
        "metadata": {"session_id": sess, "source": src, "app_name": src, "user_name": user, "client_ip": ip},
        "aiContext": {"llm_format": "anthropic" if "anthropic" in (model or "") or "claude" in (model or "") else "openai",
                      "route_type": "chat", "genai_category": "chat", "model": model},
    }
    if resp_body is not None:
        payload["response"] = {"stream": False, "body": resp_body, "text": resp_text}
    return payload


@app.post("/agentic/request")
@app.post("/request")
async def guard_request(req: Request):
    """agentgateway LLM guardrail webhook (request / INPUT guardrail). Posts a kong-gateway pre_call —
    same contract as litellm/portkey — so the incoming turn is scored before the model and pairs with
    its response into one Console card."""
    s = state.settings
    t0 = time.monotonic()
    try:
        payload = await req.json()
    except ValueError:
        return _pass("unparseable")
    user, sess, model, app_name, ip = _ctx(req)
    body = (payload or {}).get("body") or {}
    text = _last_user_text(body.get("messages") or [])
    _remember(f"{user}|{sess}", text, body, model)
    if not (body.get("messages")):
        return _pass("empty")
    src = _source(req, user, app_name)
    key = _key_for_app(s, app_name, s.agentic_key)
    wh = _kong_webhook("pre_call", llm_body=body, text=text, resp_body=None, resp_text=None,
                       user=user, sess=sess, src=src, model=model, ip=ip)
    try:
        verdict = await asyncio.wait_for(state.detect.post_webhook(wh, key=key), timeout=s.handler_deadline)
    except asyncio.TimeoutError:
        verdict = None
    if s.debug_tap:
        state.tap.append({"t": time.time(), "seam": "webhook", "phase": "input", "source": src, "user": user, "session": sess, "verdict": verdict, "ms": int((time.monotonic() - t0) * 1000)})
    if s.enforce and is_block(verdict):
        reason = block_reason(verdict)
        log.warning("BLOCK input(incoming) source=%s user=%s session=%s reason=%r", src, user, sess, reason)
        return _reject(reason)
    return _pass()


@app.post("/agentic/response")
@app.post("/response")
async def guard_response(req: Request):
    """LLM guardrail webhook (response / OUTPUT guardrail). Posts a kong-gateway post_call carrying the
    SAME request plus the response, so the backend pairs it with the pre_call into one turn."""
    s = state.settings
    t0 = time.monotonic()
    try:
        payload = await req.json()
    except ValueError:
        return _pass("unparseable")
    user, sess, model, app_name, ip = _ctx(req)
    body = (payload or {}).get("body") or {}
    answer = _choices_text(body.get("choices") or [])
    if not answer.strip():
        return _pass("empty")
    _, prompt_text, req_body, req_model = state.last_prompt.get(f"{user}|{sess}", (0, "", {}, None))
    req_body = req_body if isinstance(req_body, dict) else {}
    src = _source(req, user, app_name)
    key = _key_for_app(s, app_name, s.agentic_key)
    wh = _kong_webhook("post_call", llm_body=req_body, text=prompt_text, resp_body=body, resp_text=answer,
                       user=user, sess=sess, src=src, model=model or req_model, ip=ip)
    try:
        verdict = await asyncio.wait_for(state.detect.post_webhook(wh, key=key), timeout=s.handler_deadline)
    except asyncio.TimeoutError:
        verdict = None
    if s.debug_tap:
        state.tap.append({"t": time.time(), "seam": "webhook", "phase": "output", "source": src, "user": user, "session": sess, "verdict": verdict, "ms": int((time.monotonic() - t0) * 1000)})
    if s.enforce and is_block(verdict):
        reason = block_reason(verdict)
        log.warning("BLOCK output(outbound) source=%s user=%s session=%s reason=%r", src, user, sess, reason)
        return _reject(reason)
    return _pass()
