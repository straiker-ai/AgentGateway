"""ExtMCP servicer — the MCP seam. Unary, BLOCKING, raw JSON-RPC in both directions.

This is the capability no other gateway in the portfolio has: agentgateway hands us the
full `tools/call` (tool name + arguments) BEFORE it reaches the MCP server, and the full
result on the way back. We synthesize native hook events — exactly what a Claude Code hook
would post — and score them on /api/v1/detect:

  tools/call  request  -> PreToolUse  {tool_name: mcp__<server>__<tool>, tool_input: args,
                                       mcp_server_name, mcp_tool_name}     ENFORCEABLE
  tools/call  response -> PostToolUse {tool_name, tool_input, tool_response: {content, is_error}}
                                       (IPI control point: poisoned results blocked before the
                                        model consumes them)                 ENFORCEABLE
Block -> AuthorizationError(PERMISSION_DENIED) -> agentgateway returns JSON-RPC error -32001
to the agent. `mcp_error` lets us shape the JSON-RPC error body the agent renders.

MEASURED CONTRACT (agentgateway v1.4.1, 2026-08-19 — not documented upstream):
  CheckRequest.mcp_request   = the JSON-RPC `params` ONLY:  {"name": "...", "arguments": {...}}
  CheckResponse.mcp_response = the JSON-RPC `result` ONLY:  {"content": [...], "structuredContent": ..., "isError": bool}
  There is NO envelope and NO JSON-RPC id on either side. Headers (request only) carry
  `mcp-session-id`; `metadata_context` carries our CEL `metadata:` map (e.g. user).
Pairing Pre->Post therefore uses (session, service) in arrival order — MCP tool calls on a
session are sequential in every client we have measured (Claude Code, Cursor, the SDK).
Session: `mcp-session-id` header (request) -> remembered per service for the response side.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from typing import Any

import grpc
from google.protobuf.json_format import MessageToDict

from proto.gen import ext_mcp_pb2 as pb
from proto.gen import ext_mcp_pb2_grpc as pbg

from config import Settings
from detect_client import DetectClient, block_reason, is_block

log = logging.getLogger("straiker.extmcp")

_SESSION_HDRS = ("mcp-session-id", "x-claude-code-session-id", "x-session-id", "x-straiker-session-id", "session-id")
_USER_HDRS = ("x-straiker-user", "x-consumer-username")


def _headers(req: pb.McpRequest) -> dict[str, str]:
    return {h.key.lower(): h.value.decode("utf-8", "replace") for h in req.headers}


def _jsonrpc(raw: bytes) -> dict[str, Any] | None:
    try:
        j = json.loads(raw)
        return j if isinstance(j, dict) else None
    except ValueError:
        return None


def split_mcp_tool(server: str | None, tool: str) -> tuple[str, str, str]:
    """-> (hook tool_name, mcp_server_name, mcp_tool_name). Hook convention is
    mcp__<server>__<tool> with a DOUBLE underscore; server names may contain single ones."""
    if tool.startswith("mcp__"):
        rest = tool[5:]
        srv, _, t = rest.partition("__")
        if t:
            return tool, srv, t
    srv = server or "mcp"
    return f"mcp__{srv}__{tool}", srv, tool


def _tool_response(result: dict[str, Any] | None, error: dict[str, Any] | None) -> dict[str, Any]:
    if error:
        return {"content": json.dumps(error, ensure_ascii=False), "is_error": True}
    if not isinstance(result, dict):
        return {"content": json.dumps(result, ensure_ascii=False) if result is not None else "", "is_error": False}
    parts: list[str] = []
    for c in result.get("content") or []:
        if isinstance(c, dict):
            if c.get("type") == "text" and isinstance(c.get("text"), str):
                parts.append(c["text"])
            elif c.get("type") == "resource" and isinstance(c.get("resource"), dict) and isinstance(c["resource"].get("text"), str):
                parts.append(c["resource"]["text"])
    if not parts and result.get("structuredContent") is not None:
        parts.append(json.dumps(result["structuredContent"], ensure_ascii=False))
    return {"content": "\n".join(parts) if parts else json.dumps(result, ensure_ascii=False), "is_error": bool(result.get("isError"))}


class _Pending:
    """tools/call request context kept until its response arrives (keyed by JSON-RPC id)."""
    __slots__ = ("tool_name", "tool_input", "session_id", "user", "server", "t", "tool_use_id")

    def __init__(self, tool_name: str, tool_input: Any, session_id: str, user: str, server: str) -> None:
        self.tool_name, self.tool_input, self.session_id, self.user, self.server, self.t = tool_name, tool_input, session_id, user, server, time.monotonic()


class StraikerExtMcp(pbg.ExtMcpServicer):
    def __init__(self, settings: Settings, detect: DetectClient, tap=None) -> None:
        self.s = settings
        self.d = detect
        self.tap = tap
        self._queue: dict[tuple[str, str], list[_Pending]] = {}        # (session, service) -> FIFO of in-flight calls
        self._sess_by_service: dict[tuple[str, str], str] = {}        # (user, service) -> last session id

    # ---------------------------------------------------------------- identity
    def _identity(self, hdrs: dict[str, str], meta: dict[str, Any], server: str) -> tuple[str, str]:
        user = next((hdrs[h] for h in _USER_HDRS if hdrs.get(h)), None) or \
               next((str(meta[k]) for k in ("user", "x-straiker-user", "consumer") if meta.get(k)), None) or self.s.default_user
        sess = next((hdrs[h] for h in _SESSION_HDRS if hdrs.get(h)), None) or \
               next((str(meta[k]) for k in ("session_id", "session", "mcp-session-id") if meta.get(k)), None)
        if not sess:
            # stable per (user, server) anchor so Pre/Post pair and the Console shows one trace
            sess = "mcp-" + hashlib.sha256(f"{user}|{server}".encode()).hexdigest()[:16]
        return user, sess

    @staticmethod
    def _deny(reason: str, rpc_id: Any) -> pb.AuthorizationError:
        err = {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": -32001, "message": reason, "data": {"provider": "straiker"}}}
        return pb.AuthorizationError(code=pb.AuthorizationError.PERMISSION_DENIED, reason=reason,
                                     mcp_error=json.dumps(err).encode())

    # ---------------------------------------------------------------- request
    async def CheckRequest(self, request: pb.McpRequest, context: grpc.aio.ServicerContext) -> pb.McpRequestResult:
        try:
            if self.tap is not None:
                self.tap.append({"t": time.time(), "seam": "extmcp-raw", "dir": "request", "method": request.method,
                                 "services": list(request.service_names), "headers": _headers(request),
                                 "meta": MessageToDict(request.metadata_context) if request.HasField("metadata_context") else {},
                                 "raw": request.mcp_request[:2000].decode("utf-8", "replace") if request.mcp_request else None})
            if request.method != "tools/call" or not request.mcp_request:
                return _pass_req()
            msg = _jsonrpc(request.mcp_request)
            if not msg:
                return _pass_req()
            # params-only (measured) — tolerate a full envelope too
            params = msg.get("params") if isinstance(msg.get("params"), dict) else msg
            raw_tool = str(params.get("name") or "")
            args = params.get("arguments")
            server = (list(request.service_names) or ["mcp"])[0]
            hdrs = _headers(request)
            meta = MessageToDict(request.metadata_context) if request.HasField("metadata_context") else {}
            user, sess = self._identity(hdrs, meta, server)
            app_ref = meta.get("straiker_app") or hdrs.get("x-straiker-app")
            self._sess_by_service[(user, server)] = sess  # response side has no headers; remember
            tool_name, srv, t = split_mcp_tool(server, raw_tool)
            tool_use_id = f"mcp-{hashlib.sha256(request.mcp_request + sess.encode()).hexdigest()[:12]}"
            event = {"hook_event_name": "PreToolUse", "session_id": sess, "user_name": user,
                     "tool_name": tool_name, "tool_input": args if args is not None else {},
                     "tool_use_id": tool_use_id, "mcp_server_name": srv, "mcp_tool_name": t,
                     "cwd": meta.get("cwd") or "/", "transcript_path": ""}
            pend = _Pending(tool_name, event["tool_input"], sess, user, srv)
            pend.tool_use_id = tool_use_id
            self._queue.setdefault((sess, server), []).append(pend)
            self._gc()
            verdict = await self._guarded(self.d.post_hook(event, key=self.s.key_for(app_ref, self.s.mcp_key)))
            self._tap("tools/call", event, verdict)
            if self.s.enforce and is_block(verdict):
                reason = block_reason(verdict)
                log.warning("BLOCK mcp tools/call %s session=%s user=%s reason=%r", tool_name, sess, user, reason)
                return pb.McpRequestResult(error=self._deny(reason, msg.get("id")))
            return _pass_req()
        except Exception as e:  # fail open
            log.exception("extmcp CheckRequest error — failing open: %s", e)
            return _pass_req()

    # ---------------------------------------------------------------- response
    async def CheckResponse(self, response: pb.McpResponse, context: grpc.aio.ServicerContext) -> pb.McpResponseResult:
        try:
            if self.tap is not None:
                self.tap.append({"t": time.time(), "seam": "extmcp-raw", "dir": "response", "method": response.method,
                                 "services": list(response.service_names),
                                 "meta": MessageToDict(response.metadata_context) if response.HasField("metadata_context") else {},
                                 "raw": response.mcp_response[:2000].decode("utf-8", "replace") if response.mcp_response else None})
            if response.method != "tools/call" or not response.mcp_response:
                return _pass_resp()
            msg = _jsonrpc(response.mcp_response)
            if not msg:
                return _pass_resp()
            meta = MessageToDict(response.metadata_context) if response.HasField("metadata_context") else {}
            server = (list(response.service_names) or ["mcp"])[0]
            user = next((str(meta[k]) for k in ("user", "x-straiker-user", "consumer") if meta.get(k)), None) or self.s.default_user
            sess = self._sess_by_service.get((user, server))
            q = self._queue.get((sess, server)) if sess else None
            pend = q.pop(0) if q else None
            if pend is None:
                sess = sess or ("mcp-" + hashlib.sha256(f"{user}|{server}".encode()).hexdigest()[:16])
                pend = _Pending(f"mcp__{server}__unknown", {}, sess, user, server)
                pend.tool_use_id = None
            # result-only (measured) — tolerate a full envelope too
            result = msg.get("result") if "result" in msg else (None if "error" in msg else msg)
            tr = _tool_response(result, msg.get("error"))
            event = {"hook_event_name": "PostToolUse", "session_id": pend.session_id, "user_name": pend.user,
                     "tool_name": pend.tool_name, "tool_input": pend.tool_input, "tool_response": tr,
                     "tool_use_id": pend.tool_use_id, "is_error": tr["is_error"],
                     "mcp_server_name": pend.server, "mcp_tool_name": pend.tool_name.split("__", 2)[-1], "cwd": "/"}
            event = {k: v for k, v in event.items() if v is not None}
            verdict = await self._guarded(self.d.post_hook(event))
            self._tap("tools/call:response", event, verdict)
            if self.s.enforce and is_block(verdict):
                reason = block_reason(verdict)
                log.warning("BLOCK mcp tools/call RESULT %s session=%s reason=%r", pend.tool_name, pend.session_id, reason)
                return pb.McpResponseResult(error=self._deny(reason, None))
            return _pass_resp()
        except Exception as e:
            log.exception("extmcp CheckResponse error — failing open: %s", e)
            return _pass_resp()

    # ---------------------------------------------------------------- helpers
    async def _guarded(self, coro):
        try:
            return await asyncio.wait_for(coro, timeout=self.s.handler_deadline)
        except asyncio.TimeoutError:
            log.warning("detect exceeded handler deadline — failing open")
            return None

    def _gc(self) -> None:
        now = time.monotonic()
        if sum(len(q) for q in self._queue.values()) > 5000:
            for k, q in list(self._queue.items()):
                q[:] = [p for p in q if now - p.t < 600]
                if not q:
                    self._queue.pop(k, None)
        if len(self._sess_by_service) > 10000:
            self._sess_by_service.clear()

    def _tap(self, kind: str, event: dict, verdict) -> None:
        if self.tap is not None:
            self.tap.append({"t": time.time(), "seam": "extmcp", "kind": kind, "event": event, "verdict": verdict})


def _pass_req() -> pb.McpRequestResult:
    r = pb.McpRequestResult()
    getattr(r, "pass").SetInParent()
    return r


def _pass_resp() -> pb.McpResponseResult:
    r = pb.McpResponseResult()
    getattr(r, "pass").SetInParent()
    return r


async def serve(settings: Settings, detect: DetectClient, tap=None) -> grpc.aio.Server:
    server = grpc.aio.server()
    pbg.add_ExtMcpServicer_to_server(StraikerExtMcp(settings, detect, tap), server)
    addr = f"{settings.grpc_host}:{settings.extmcp_port}"
    server.add_insecure_port(addr)  # h2c on loopback
    await server.start()
    log.info("ExtMCP (MCP tools/call) listening on %s mode=%s", addr, settings.mode)
    return server
