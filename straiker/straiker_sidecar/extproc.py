"""ExtProc servicer — the CODING-AGENT seam (Envoy ext_proc v3, served to agentgateway).

One bidirectional gRPC stream per proxied HTTP request. agentgateway sends, in order:
request_headers -> request_body (1..n chunks) -> response_headers -> response_body (1..n)
and we answer each with a ProcessingResponse. We may answer any message with
ImmediateResponse to terminate the exchange with our own status/body (= a block).

Contract mapping (central-parse, verbatim relay):
  request_body   complete  -> POST /detect  x-straiker-phase: request          (verbatim bytes)
  response_body  complete  -> POST /detect  x-straiker-phase: response-sync    (envelope: sse+model+request)
                               ONLY when the route is buffered (we can still block) AND the
                               response carries a tool call; otherwise phase: response (async,
                               monitor) so pure-text turns pay no synchronous tax.
Streaming (fullDuplexStreamed) routes: chunks are forwarded untouched as they arrive; the
body is accumulated for a post-hoc async `response` post. Bytes already flushed cannot be
recalled — that is the honest boundary, same as Kong's stream variant.

Fail-open is the contract: any exception inside the handler resolves to CONTINUE.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, AsyncIterator

import grpc
from google.protobuf.json_format import MessageToDict

from proto.gen import ext_proc_pb2 as pb
from proto.gen import ext_proc_pb2_grpc as pbg
from proto.gen import shared_envoy_pb2 as envoy

import blocking
import coding_synth
import wire
from config import Settings
from detect_client import DetectClient, block_reason, is_block

log = logging.getLogger("straiker.extproc")


def _dest_for(path: str) -> str | None:
    p = path or ""
    if "openai" in p or "/chat/completions" in p or "/responses" in p:
        return "api.openai.com"
    if "/v1/messages" in p:
        return "api.anthropic.com"
    return None


def _hdrs(hm: pb.HeaderMap | None) -> dict[str, str]:
    out: dict[str, str] = {}
    if hm is None:
        return out
    for h in hm.headers:
        v = h.raw_value.decode("utf-8", "replace") if h.raw_value else h.value
        out[h.key.lower()] = v
    return out


def _attrs(msg: pb.ProcessingRequest) -> dict[str, str]:
    """CEL `requestAttributes` arrive as ProcessingRequest.attributes: map<string, Struct>. Flatten
    every scalar into a lowercase-keyed dict so identity can come from attributes OR headers."""
    out: dict[str, str] = {}
    try:
        for ns, st in msg.attributes.items():
            d = MessageToDict(st)
            # agentgateway puts {key: value} under a namespace; Envoy puts attribute name -> value
            if isinstance(d, dict):
                for k, v in d.items():
                    if isinstance(v, (str, int, float, bool)):
                        out[str(k).lower()] = str(v)
            if isinstance(ns, str) and ns.lower() not in out and not isinstance(d, dict):
                out[ns.lower()] = str(d)
    except Exception:  # attributes are best-effort identity hints; never fail the stream
        pass
    return out


def _continue_headers() -> pb.ProcessingResponse:
    return pb.ProcessingResponse(request_headers=pb.HeadersResponse(response=pb.CommonResponse()))


def _continue_resp_headers() -> pb.ProcessingResponse:
    return pb.ProcessingResponse(response_headers=pb.HeadersResponse(response=pb.CommonResponse()))


def _continue_req_body() -> pb.ProcessingResponse:
    return pb.ProcessingResponse(request_body=pb.BodyResponse(response=pb.CommonResponse()))


def _continue_resp_body() -> pb.ProcessingResponse:
    return pb.ProcessingResponse(response_body=pb.BodyResponse(response=pb.CommonResponse()))


def _immediate(status: int, body: bytes, content_type: str) -> pb.ProcessingResponse:
    hm = pb.HeaderMutation(set_headers=[
        envoy.HeaderValueOption(header=envoy.HeaderValue(key="content-type", raw_value=content_type.encode())),
        envoy.HeaderValueOption(header=envoy.HeaderValue(key="x-straiker-blocked", raw_value=b"true")),
    ])
    return pb.ProcessingResponse(immediate_response=pb.ImmediateResponse(
        status=envoy.HttpStatus(code=status), headers=hm, body=body, details="straiker-guardrail"))


class _Exchange:
    """Per-request state for one ExtProc stream."""
    __slots__ = ("req_headers", "path", "user", "app_ref", "smode", "source", "req_chunks", "req_body",
                 "req_json", "session_id", "model", "fmt", "stream", "resp_headers", "resp_chunks",
                 "resp_streaming", "t0", "blocked")

    def __init__(self) -> None:
        self.req_headers: dict[str, str] = {}
        self.path = ""
        self.user = ""
        self.app_ref = None
        self.smode = "agentic"  # DEFAULT agentic (/detect?agentic messages trace); coding routes opt in
        self.source = None
        self.req_chunks: list[bytes] = []
        self.req_body: bytes = b""
        self.req_json: dict[str, Any] | None = None
        self.session_id: str | None = None
        self.model: str | None = None
        self.fmt = "unknown"
        self.stream = False
        self.resp_headers: dict[str, str] = {}
        self.resp_chunks: list[bytes] = []
        self.resp_streaming = False
        self.t0 = time.monotonic()
        self.blocked = False


class StraikerExtProc(pbg.ExternalProcessorServicer):
    def __init__(self, settings: Settings, detect: DetectClient, tap=None) -> None:
        self.s = settings
        self.d = detect
        self.tap = tap  # optional debug ring buffer
        self._bg: set[asyncio.Task] = set()

    # ---------------------------------------------------------------- stream handler
    async def Process(self, request_iterator: AsyncIterator[pb.ProcessingRequest], context: grpc.aio.ServicerContext):
        ex = _Exchange()
        async for msg in request_iterator:
            kind = msg.WhichOneof("request")
            try:
                if kind == "request_headers":
                    ex.req_headers = _hdrs(msg.request_headers.headers)
                    # identity: CEL requestAttributes (x-straiker-user: apiKey.user) beat headers beat default
                    attrs = _attrs(msg)
                    for k in ("x-straiker-user", "x-consumer-username"):
                        if attrs.get(k) and not ex.req_headers.get(k):
                            ex.req_headers[k] = attrs[k]
                    ex.path = ex.req_headers.get(":path", "")
                    ex.user = wire.user_from(ex.req_headers, self.s.default_user)
                    ex.app_ref = attrs.get("x-straiker-app") or ex.req_headers.get("x-straiker-app")
                    # route mode: agentic (chatbots/agents -> /detect?agentic) vs coding (central-parse)
                    ex.smode = (attrs.get("x-straiker-mode") or ex.req_headers.get("x-straiker-mode") or "agentic").lower()
                    ex.source = ex.req_headers.get("x-straiker-source") or attrs.get("x-straiker-source")  # client names its app; CEL is the default
                    yield _continue_headers()
                elif kind == "request_body":
                    resp = await self._on_request_body(ex, msg.request_body)
                    yield resp
                    if ex.blocked:
                        return
                elif kind == "response_headers":
                    ex.resp_headers = _hdrs(msg.response_headers.headers)
                    yield _continue_resp_headers()
                elif kind == "response_body":
                    resp = await self._on_response_body(ex, msg.response_body)
                    yield resp
                    if ex.blocked:
                        return
                elif kind == "request_trailers":
                    yield pb.ProcessingResponse(request_trailers=pb.TrailersResponse())
                elif kind == "response_trailers":
                    yield pb.ProcessingResponse(response_trailers=pb.TrailersResponse())
                else:
                    # unknown message kind: never stall the proxy
                    yield pb.ProcessingResponse()
            except Exception as e:  # fail open, always
                log.exception("extproc handler error (%s) — failing open: %s", kind, e)
                if kind == "request_body":
                    yield _continue_req_body()
                elif kind == "response_body":
                    yield _continue_resp_body()
                elif kind == "request_headers":
                    yield _continue_headers()
                elif kind == "response_headers":
                    yield _continue_resp_headers()
                else:
                    yield pb.ProcessingResponse()

    # ---------------------------------------------------------------- request phase
    async def _on_request_body(self, ex: _Exchange, body: pb.HttpBody) -> pb.ProcessingResponse:
        ex.req_chunks.append(body.body)
        if not body.end_of_stream:
            # streamed request: pass chunks through; score when complete. (Claude Code requests
            # are small enough that buffered mode is the norm; this keeps streamed mode safe.)
            return _continue_req_body()
        ex.req_body = b"".join(ex.req_chunks)
        ex.req_chunks = []
        if len(ex.req_body) > self.s.max_body_bytes:
            log.warning("request body %d bytes > cap; relaying unscored", len(ex.req_body))
            return _continue_req_body()
        ex.req_json = wire.parse_json(ex.req_body)
        ex.session_id = wire.session_id_from(ex.req_headers, ex.req_json)
        ex.model = wire.model_of(ex.req_json, ex.req_headers, ex.path)
        ex.fmt = wire.wire_format(ex.path, ex.req_json)
        ex.stream = wire.wants_stream(ex.req_json)
        if ex.req_json is None:
            return _continue_req_body()  # not JSON (health checks etc.) — nothing to score

        # --- AGENTIC mode: chatbots/agents -> /detect?agentic with the full messages trace ----------
        if ex.smode == "agentic":
            messages, _tools = (wire.anthropic_to_agentic(ex.req_json) if ex.fmt == "anthropic"
                                else wire.openai_chat_to_agentic(ex.req_json))
            if not messages:
                return _continue_req_body()
            key = self.s.key_for(ex.app_ref, self.s.agentic_key)
            src = ex.source or (ex.app_ref or "agentgateway")
            verdict = await self._guarded(self.d.post_agentic(
                messages=messages, source=src, session_id=ex.session_id, user=ex.user, model=ex.model,
                destination=_dest_for(ex.path), hook="pre_call", key=key))
            self._tap("agentic:request", ex, verdict)
            if self.s.enforce and is_block(verdict):
                ex.blocked = True
                body_bytes, ct = blocking.render(ex.fmt, ex.stream, block_reason(verdict))
                log.warning("BLOCK agentic request source=%s user=%s reason=%r", src, ex.user, block_reason(verdict))
                return _immediate(200, body_bytes, ct)
            return _continue_req_body()

        # --- CODING mode ------------------------------------------------------------------------------
        # OpenAI dialects (Codex Responses, Cursor/OpenCode Chat) are EDGE-PARSED on the response, where
        # the proposed tool calls live; Argus central-parse only covers the Anthropic wire. So for an
        # OpenAI coding request we relay unscored here and synthesize the full trace at response time.
        if ex.fmt in ("openai-chat", "openai-responses"):
            return _continue_req_body()

        # Anthropic coding (Claude Code): central-parse verbatim relay (x-tool: kong-claude-code).
        key = self.s.key_for(ex.app_ref, self.s.coding_key)
        verdict = await self._guarded(self.d.post_gateway(ex.req_body, phase="request", session_id=ex.session_id,
                                                          user=ex.user, model=ex.model, key=key))
        self._tap("request", ex, verdict)
        if self.s.enforce and is_block(verdict):
            ex.blocked = True
            body_bytes, ct = blocking.render(ex.fmt, ex.stream, block_reason(verdict))
            log.warning("BLOCK request session=%s user=%s fmt=%s reason=%r", ex.session_id, ex.user, ex.fmt, block_reason(verdict))
            return _immediate(200, body_bytes, ct)
        return _continue_req_body()

    # ---------------------------------------------------------------- response phase
    async def _on_response_body(self, ex: _Exchange, body: pb.HttpBody) -> pb.ProcessingResponse:
        ex.resp_chunks.append(body.body)
        if not body.end_of_stream:
            # fullDuplexStreamed: chunks flow now; we only accumulate. Enforcement is impossible
            # for bytes already forwarded — monitor-only by construction.
            ex.resp_streaming = True
            return _continue_resp_body()
        raw = b"".join(ex.resp_chunks)
        ex.resp_chunks = []
        if ex.req_json is None or not raw:
            return _continue_resp_body()
        if len(raw) > self.s.max_body_bytes:
            log.warning("response body %d bytes > cap; relaying unscored", len(raw))
            return _continue_resp_body()
        sse = wire.is_sse(ex.resp_headers, raw)
        has_tool = wire.response_has_tool_use(raw, sse)

        # --- AGENTIC mode: append the assistant answer to the trace, score the full turn (monitor) ---
        if ex.smode == "agentic":
            answer = wire.answer_text(raw, sse)
            messages, _ = (wire.anthropic_to_agentic(ex.req_json) if ex.fmt == "anthropic"
                           else wire.openai_chat_to_agentic(ex.req_json))
            if answer:
                messages = messages + [{"role": "assistant", "content": answer}]
            src = ex.source or (ex.app_ref or "agentgateway")
            self._spawn(self.d.post_agentic(messages=messages, source=src, session_id=ex.session_id,
                                            user=ex.user, model=ex.model, destination=_dest_for(ex.path),
                                            hook="post_call", key=self.s.key_for(ex.app_ref, self.s.agentic_key)))
            return _continue_resp_body()

        # --- CODING, OpenAI dialects: EDGE-PARSE into hook events (x-tool: claude-code) --------------
        if ex.fmt in ("openai-chat", "openai-responses"):
            return await self._edge_parse_openai(ex, raw, sse)

        if ex.resp_streaming or not self.s.enforce or not has_tool:
            # monitor: async post, never holds the client. Pure-text turns go here too — the
            # backend emits Stop (final answer) from this and nothing enforceable is lost.
            env = wire.response_envelope("response", raw, ex.model, ex.req_json)
            self._spawn(self.d.post_gateway(env, phase="response", session_id=ex.session_id, user=ex.user, model=ex.model, key=self.s.key_for(ex.app_ref, self.s.coding_key)))
            return _continue_resp_body()

        # buffered + enforce + tool call present: adjudicate BEFORE the client sees the tool_use.
        env = wire.response_envelope("response-sync", raw, ex.model, ex.req_json)
        verdict = await self._guarded(self.d.post_gateway(env, phase="response-sync", session_id=ex.session_id,
                                                          user=ex.user, model=ex.model, key=self.s.key_for(ex.app_ref, self.s.coding_key)))
        self._tap("response-sync", ex, verdict)
        if is_block(verdict):
            ex.blocked = True
            body_bytes, ct = blocking.render(ex.fmt, sse, block_reason(verdict))
            log.warning("BLOCK response-sync session=%s user=%s reason=%r", ex.session_id, ex.user, block_reason(verdict))
            # Replace the body: the client never receives an executable tool_use.
            return _immediate(200, body_bytes, ct)
        return _continue_resp_body()

    async def _edge_parse_openai(self, ex: _Exchange, raw: bytes, sse: bool) -> pb.ProcessingResponse:
        """Synthesize Claude Code hook events from an OpenAI-dialect coding turn and post them on the
        native contract (x-tool: claude-code). Enforce on the tool events: a blocked PreToolUse
        replaces the response so the client never receives an executable tool call."""
        resp_json = coding_synth.decode_response(raw, ex.fmt)
        if not resp_json:
            log.warning("edge: undecodable response (%d bytes, prefix %r) session=%s", len(raw), raw[:48], ex.session_id)
        events = coding_synth.synth_events(ex.req_json, resp_json, ex.fmt, ex.session_id or "", ex.user, ex.model)
        if not events:
            return _continue_resp_body()
        key = self.s.key_for(ex.app_ref, self.s.coding_key)
        for ev in events:
            enforce = ev["hook_event_name"] in ("PreToolUse", "PostToolUse")
            if enforce:
                verdict = await self._guarded(self.d.post_hook(ev, x_tool="claude-code", key=key))
                self._tap(f"edge/{ev['hook_event_name']}", ex, verdict)
                if self.s.enforce and is_block(verdict):
                    ex.blocked = True
                    body_bytes, ct = blocking.render(ex.fmt, sse, block_reason(verdict))
                    log.warning("BLOCK edge %s session=%s user=%s tool=%s reason=%r",
                                ev["hook_event_name"], ex.session_id, ex.user, ev.get("tool_name"), block_reason(verdict))
                    return _immediate(200, body_bytes, ct)
            else:
                # UserPromptSubmit / Stop never block; post them for the trace without holding the client.
                self._spawn(self.d.post_hook(ev, x_tool="claude-code", key=key))
        return _continue_resp_body()

    # ---------------------------------------------------------------- helpers
    async def _guarded(self, coro):
        try:
            return await asyncio.wait_for(coro, timeout=self.s.handler_deadline)
        except asyncio.TimeoutError:
            log.warning("detect exceeded handler deadline %.1fs — failing open", self.s.handler_deadline)
            return None

    def _spawn(self, coro) -> None:
        t = asyncio.create_task(coro)
        self._bg.add(t)
        t.add_done_callback(self._bg.discard)

    def _tap(self, phase: str, ex: _Exchange, verdict) -> None:
        if self.tap is not None:
            self.tap.append({"t": time.time(), "phase": phase, "path": ex.path, "session": ex.session_id,
                             "user": ex.user, "model": ex.model, "fmt": ex.fmt, "verdict": verdict})


async def serve(settings: Settings, detect: DetectClient, tap=None) -> grpc.aio.Server:
    server = grpc.aio.server(options=[("grpc.max_receive_message_length", settings.max_body_bytes + 1024 * 1024),
                                      ("grpc.max_send_message_length", settings.max_body_bytes + 1024 * 1024)])
    pbg.add_ExternalProcessorServicer_to_server(StraikerExtProc(settings, detect, tap), server)
    addr = f"{settings.grpc_host}:{settings.extproc_port}"
    server.add_insecure_port(addr)  # h2c on loopback — never crosses the public ingress
    await server.start()
    log.info("ExtProc (coding agents) listening on %s  x-tool=%s mode=%s", addr, settings.x_tool, settings.mode)
    return server
