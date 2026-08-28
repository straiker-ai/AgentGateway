#!/usr/bin/env python3
"""Behavioural QA — OFFLINE, one assertion block per property we rely on. Runs the real sidecar
modules in-process; no gateway, no network (the detect client is stubbed).

The golden/parity suites prove "nothing changed unintentionally"; this proves "the thing we claim
actually behaves that way": identity precedence, phase selection, block rendering, fail-open,
the measured ExtMCP contract, the session/wire helpers.   Exit non-zero on any failure.
"""
from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "straiker_sidecar"))
os.environ.setdefault("STRAIKER_AGENTGATEWAY_KEY", "qa-key")

import blocking  # noqa: E402
import extmcp  # noqa: E402
import extproc  # noqa: E402
import wire  # noqa: E402
from config import Settings  # noqa: E402
from detect_client import block_reason, is_block  # noqa: E402
from proto.gen import ext_mcp_pb2 as mpb  # noqa: E402
from proto.gen import ext_proc_pb2 as pb  # noqa: E402
from proto.gen import shared_envoy_pb2 as envoy  # noqa: E402

KONG_FIX = os.environ.get("KONG_FIXTURES", os.environ.get("STRAIKER_FIXTURES", ""))

PASS = FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {detail}")


class FakeDetect:
    """Scripted Straiker: returns the verdict you queue; records every post."""
    def __init__(self):
        self.posts = []
        self.queue = []
        self.delay = 0.0

    def _next(self):
        return self.queue.pop(0) if self.queue else None

    async def post_gateway(self, body, *, phase, session_id, user, model=None, key=None):
        if self.delay:
            await asyncio.sleep(self.delay)
        self.posts.append({"kind": "gateway", "phase": phase, "session": session_id, "user": user, "model": model, "body": body})
        return self._next()

    async def post_hook(self, event, *, x_tool=None, key=None):
        self.posts.append({"kind": "hook", "event": event})
        return self._next()

    async def post_webhook(self, payload, *, key=None):
        self.posts.append({"kind": "webhook", "payload": payload})
        return self._next()

    async def post_agentic(self, *, messages, source, session_id, user, model=None, destination=None, hook=None, key=None):
        if self.delay:
            await asyncio.sleep(self.delay)
        self.posts.append({"kind": "agentic", "messages": messages, "source": source, "session": session_id,
                           "user": user, "model": model, "hook": hook})
        return self._next()


def hm(d):
    return pb.HeaderMap(headers=[envoy.HeaderValue(key=k, raw_value=str(v).encode()) for k, v in d.items()])


async def drive(svc, msgs):
    async def it():
        for m in msgs:
            yield m
    out = []
    async for r in svc.Process(it(), None):
        out.append(r)
    # drain the servicer's fire-and-forget posts so one case's async `response` never leaks into the next
    bg = getattr(svc, "_bg", set())
    if bg:
        await asyncio.gather(*list(bg), return_exceptions=True)
    return out


def req_headers(path="/v1/messages", extra=None, attrs=None, mode="coding"):
    # the coding ROUTES set x-straiker-mode:coding in config (agentic is the default), so coding-path
    # QA drives with mode=coding unless a test overrides it. Pass mode=None to exercise the default.
    h = {":path": path, ":method": "POST", "content-type": "application/json"}
    h.update(extra or {})
    m = pb.ProcessingRequest(request_headers=pb.HttpHeaders(headers=hm(h)))
    merged = dict(attrs or {})
    if mode is not None:
        merged.setdefault("x-straiker-mode", mode)
    if merged:
        from google.protobuf.struct_pb2 import Struct
        s = Struct(); s.update(merged)
        m.attributes["straiker"].CopyFrom(s)
    return m


def body_msg(b: bytes, end=True, resp=False, chunks=1):
    msgs = []
    step = max(1, len(b) // chunks)
    for i in range(0, len(b), step):
        part = b[i:i + step]
        eos = end and (i + step >= len(b))
        msgs.append(pb.ProcessingRequest(response_body=pb.HttpBody(body=part, end_of_stream=eos)) if resp
                    else pb.ProcessingRequest(request_body=pb.HttpBody(body=part, end_of_stream=eos)))
    return msgs


def resp_headers(ct="text/event-stream"):
    return pb.ProcessingRequest(response_headers=pb.HttpHeaders(headers=hm({":status": "200", "content-type": ct})))


ANTHRO_REQ = json.dumps({"model": "claude-sonnet-4-5", "max_tokens": 50, "stream": True,
                         "metadata": {"user_id": json.dumps({"device_id": "d", "session_id": "sess-qa-1"})},
                         "messages": [{"role": "user", "content": "hello"}]}).encode()
TOOL_SSE = (b'event: message_start\ndata: {"type":"message_start","message":{"id":"m","model":"claude-sonnet-4-5","content":[]}}\n\n'
            b'event: content_block_start\ndata: {"type":"content_block_start","index":0,"content_block":{"type":"tool_use","id":"toolu_1","name":"Bash","input":{}}}\n\n'
            b'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"input_json_delta","partial_json":"{\\"command\\":\\"ls\\"}"}}\n\n'
            b'event: content_block_stop\ndata: {"type":"content_block_stop","index":0}\n\n'
            b'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"tool_use"}}\n\n'
            b'event: message_stop\ndata: {"type":"message_stop"}\n\n')
TEXT_SSE = (b'event: message_start\ndata: {"type":"message_start","message":{"id":"m","model":"claude-sonnet-4-5","content":[]}}\n\n'
            b'event: content_block_start\ndata: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n'
            b'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"hi"}}\n\n'
            b'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn"}}\n\n'
            b'event: message_stop\ndata: {"type":"message_stop"}\n\n')
DENY = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": "nope"},
        "continue": False, "stopReason": "Blocked (qa)"}
ALLOW = {"hookSpecificOutput": {"hookEventName": "GatewayRequest", "permissionDecision": "allow"}, "straiker": {}}


async def main() -> int:
    print("== verdict helpers ==")
    check("action==block is a block", is_block({"action": "block"}))
    check("permissionDecision deny is a block", is_block({"hookSpecificOutput": {"permissionDecision": "deny"}}))
    check("continue:false is a block", is_block({"continue": False}))
    check("empty/None is not a block", not is_block({}) and not is_block(None))
    check("null reason -> readable default", block_reason({"reason": None}) == "Request blocked by Straiker guardrails.")
    check("stopReason preferred for humans", block_reason(DENY) == "Blocked (qa)")

    print("== wire helpers ==")
    j = json.loads(ANTHRO_REQ)
    check("session from metadata.user_id JSON string", wire.session_id_from({}, j) == "sess-qa-1")
    check("header session beats body", wire.session_id_from({"x-claude-code-session-id": "H"}, j) == "H")
    check("codex session-id header", wire.session_id_from({"session-id": "cx"}, None) == "cx")
    check("user: header beats default", wire.user_from({"x-straiker-user": "a@b"}, "dflt") == "a@b")
    check("bedrock model from path", wire.model_of(None, {}, "/model/anthropic.claude-3-5/invoke") == "anthropic.claude-3-5")
    check("stream flag read from DECODED body (not substring)", not wire.wants_stream({"messages": [{"role": "user", "content": '"stream": true'}]}))
    check("tool_use detected in SSE", wire.response_has_tool_use(TOOL_SSE, True) and not wire.response_has_tool_use(TEXT_SSE, True))
    env = json.loads(wire.response_envelope("response-sync", TOOL_SSE, "m", j))
    check("response envelope: sse is a STRING + request attached", isinstance(env["sse"], str) and env["request"]["model"] == "claude-sonnet-4-5" and env["straiker_phase"] == "response-sync")

    print("== block bodies ==")
    sse = blocking.anthropic_sse(None)
    check("anthropic SSE block: content is [] not {}", b'"content": []' in sse)
    check("anthropic SSE block: complete message (start..stop)", sse.startswith(b"event: message_start") and b"event: message_stop" in sse)
    check("anthropic SSE block: readable text when reason null", blocking.DEFAULT_REASON.encode() in sse)
    for fmt in ("anthropic", "openai-chat", "openai-responses"):
        for st in (True, False):
            b, ct = blocking.render(fmt, st, "r")
            check(f"render {fmt} stream={st} -> {ct}", bool(b) and (ct == "text/event-stream") == st)
    oc = json.loads(blocking.openai_chat_json("r")); check("openai chat block shape", oc["choices"][0]["message"]["content"] == "r" and oc["object"] == "chat.completion")
    orj = json.loads(blocking.openai_responses_json("r")); check("openai responses block shape", orj["output"][0]["content"][0]["text"] == "r")

    print("== ExtProc: identity precedence ==")
    s = Settings(); fd = FakeDetect(); svc = extproc.StraikerExtProc(s, fd)
    fd.queue = [ALLOW, ALLOW]
    await drive(svc, [req_headers(attrs={"x-straiker-user": "attr@x"}), *body_msg(ANTHRO_REQ), resp_headers(), *body_msg(TEXT_SSE, resp=True)])
    check("CEL requestAttributes x-straiker-user reaches identity", fd.posts and fd.posts[0]["user"] == "attr@x", str(fd.posts[:1]))
    fd.posts.clear(); fd.queue = [ALLOW, ALLOW]
    await drive(svc, [req_headers(extra={"x-straiker-user": "hdr@x"}), *body_msg(ANTHRO_REQ), resp_headers(), *body_msg(TEXT_SSE, resp=True)])
    check("x-straiker-user header reaches identity", fd.posts[0]["user"] == "hdr@x")
    fd.posts.clear(); fd.queue = [ALLOW, ALLOW]
    await drive(svc, [req_headers(), *body_msg(ANTHRO_REQ), resp_headers(), *body_msg(TEXT_SSE, resp=True)])
    check("no identity -> default user (stable, not per-request)", fd.posts[0]["user"] == s.default_user)
    check("session id relayed from metadata.user_id", fd.posts[0]["session"] == "sess-qa-1")

    print("== ExtProc: phase selection ==")
    fd.posts.clear(); fd.queue = [ALLOW, ALLOW]
    await drive(svc, [req_headers(), *body_msg(ANTHRO_REQ), resp_headers(), *body_msg(TOOL_SSE, resp=True)])
    await asyncio.sleep(0.05)
    phases = [p["phase"] for p in fd.posts]
    check("buffered + tool_use -> request + response-sync", phases == ["request", "response-sync"], str(phases))
    fd.posts.clear(); fd.queue = [ALLOW, ALLOW]
    await drive(svc, [req_headers(), *body_msg(ANTHRO_REQ), resp_headers(), *body_msg(TEXT_SSE, resp=True)])
    await asyncio.sleep(0.05)
    tags = [p["phase"] if p["kind"] == "gateway" else p["event"]["hook_event_name"] for p in fd.posts]
    check("buffered + text-only -> request + explicit Stop hook (assistant answer lands in Console)", tags == ["request", "Stop"], str(tags))
    stop = [p for p in fd.posts if p["kind"] == "hook" and p["event"]["hook_event_name"] == "Stop"]
    check("Stop hook carries the assistant answer + stop_reason", bool(stop) and stop[0]["event"]["app_response"] == "hi" and stop[0]["event"]["stop_reason"] == "end_turn", str(stop[:1]))
    fd.posts.clear(); fd.queue = [ALLOW, ALLOW]
    await drive(svc, [req_headers(), *body_msg(ANTHRO_REQ), resp_headers(), *body_msg(TEXT_SSE, resp=True, chunks=4)])
    await asyncio.sleep(0.05)
    tags = [p["phase"] if p["kind"] == "gateway" else p["event"]["hook_event_name"] for p in fd.posts]
    check("streamed + text-only -> request + Stop hook (streaming final answer still lands)", tags == ["request", "Stop"], str(tags))
    fd.posts.clear(); fd.queue = [ALLOW, ALLOW]
    await drive(svc, [req_headers(), *body_msg(ANTHRO_REQ), resp_headers(), *body_msg(TOOL_SSE, resp=True, chunks=4)])
    await asyncio.sleep(0.05)
    phases = [p["phase"] for p in fd.posts]
    check("streamed response chunks + tool_use -> response (monitor-only; bytes already flushed)", phases == ["request", "response"], str(phases))

    print("== ExtProc: AGENTIC mode (the default — chatbots/agents to /detect?agentic) ==")
    OA_REQ = json.dumps({"model": "gpt-4o", "messages": [
        {"role": "system", "content": "You are AcmeBot."},
        {"role": "user", "content": "look up 4242"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_1", "type": "function", "function": {"name": "mcp__db__query", "arguments": "{\"sql\":\"SELECT 1\"}"}}]},
        {"role": "tool", "tool_call_id": "call_1", "name": "mcp__db__query", "content": "{\"ssn\":\"078\"}"}]}).encode()
    OA_RESP = json.dumps({"choices": [{"message": {"role": "assistant", "content": "done"}, "finish_reason": "stop"}]}).encode()
    fd.posts.clear(); fd.queue = [ALLOW, ALLOW]
    await drive(svc, [req_headers(path="/v1/chat/completions", mode="agentic", attrs={"x-straiker-source": "AcmeBot"}),
                      *body_msg(OA_REQ), resp_headers(ct="application/json"), *body_msg(OA_RESP, resp=True)])
    await asyncio.sleep(0.05)
    ag = [p for p in fd.posts if p["kind"] == "agentic"]
    check("agentic is the default path (no post_gateway, uses post_agentic)", all(p["kind"] == "agentic" for p in fd.posts) and len(ag) >= 1, str([p["kind"] for p in fd.posts]))
    check("agentic messages carry the full trace (system + flattened tool_call + tool result)",
          ag and ag[0]["messages"][0]["role"] == "system"
          and ag[0]["messages"][2]["tool_calls"][0] == {"id": "call_1", "name": "mcp__db__query", "input": {"sql": "SELECT 1"}, "mcp_server_name": "db", "mcp_tool_name": "query"}
          and ag[0]["messages"][3]["role"] == "tool", str(ag[:1]))
    check("client x-straiker-source names the app (auto-enumerate)", ag and ag[0]["source"] == "AcmeBot", str(ag[:1]))
    check("agentic request hook=pre_call, response hook=post_call", [p["hook"] for p in ag] == ["pre_call", "post_call"], str([p["hook"] for p in ag]))
    fd.posts.clear(); fd.queue = [DENY]
    out = await drive(svc, [req_headers(path="/v1/chat/completions", mode="agentic"), *body_msg(OA_REQ)])
    check("agentic block -> ImmediateResponse (chatbot prompt denied)", out[-1].WhichOneof("response") == "immediate_response")

    print("== webhook (kong-gateway) agentic contract — the litellm/portkey shape ==")
    import app as sidecar_app
    wh_req = sidecar_app._kong_webhook("pre_call", llm_body={"messages": [{"role": "system", "content": "S"}, {"role": "user", "content": "hi"}]},
                                       text="hi", resp_body=None, resp_text=None, user="u@x", sess="sess-9", src="HR-Assistant", model="gpt-4o", ip="1.2.3.4")
    check("pre_call: request only, no response half", wh_req["eventType"] == "pre_call" and "response" not in wh_req)
    check("pre_call: metadata.source auto-enumerates the app", wh_req["metadata"]["source"] == "HR-Assistant" and wh_req["metadata"]["session_id"] == "sess-9")
    wh_resp = sidecar_app._kong_webhook("post_call", llm_body={"messages": [{"role": "user", "content": "hi"}]},
                                        text="hi", resp_body={"choices": [{"message": {"content": "hello"}}]}, resp_text="hello", user="u@x", sess="sess-9", src="HR-Assistant", model="gpt-4o", ip="1.2.3.4")
    check("post_call: SAME session + source (pairs into one card)", wh_resp["metadata"]["session_id"] == "sess-9" and wh_resp["metadata"]["source"] == "HR-Assistant")
    check("post_call: carries request AND response (input+output)", "response" in wh_resp and wh_resp["response"]["text"] == "hello" and wh_resp["request"]["text"] == "hi")
    check("aiContext model recorded", wh_resp["aiContext"]["model"] == "gpt-4o")

    print("== ExtProc: enforcement + fail-open ==")
    fd.posts.clear(); fd.queue = [DENY]
    out = await drive(svc, [req_headers(), *body_msg(ANTHRO_REQ)])
    last = out[-1]
    check("request deny -> ImmediateResponse 200 SSE (streaming client)", last.WhichOneof("response") == "immediate_response" and last.immediate_response.status.code == 200
          and any(h.header.key == "content-type" and h.header.raw_value == b"text/event-stream" for h in last.immediate_response.headers.set_headers))
    fd.posts.clear(); fd.queue = [ALLOW, DENY]
    out = await drive(svc, [req_headers(), *body_msg(ANTHRO_REQ), resp_headers(), *body_msg(TOOL_SSE, resp=True)])
    ib = out[-1].immediate_response.body if out[-1].WhichOneof("response") == "immediate_response" else b""
    ib = ib if isinstance(ib, (bytes, bytearray)) else str(ib).encode()
    check("response-sync deny replaces body with block (client never sees tool_use)", out[-1].WhichOneof("response") == "immediate_response" and b"tool_use" not in ib and b"message_stop" in ib)
    mon = Settings.__new__(Settings); object.__setattr__(mon, "__dict__", {**s.__dict__, "mode": "monitor"})
    svc_mon = extproc.StraikerExtProc(mon, fd); fd.posts.clear(); fd.queue = [DENY]
    out = await drive(svc_mon, [req_headers(), *body_msg(ANTHRO_REQ)])
    check("monitor mode never denies", out[-1].WhichOneof("response") == "request_body")
    fd.posts.clear(); fd.queue = []; fd.delay = 0.3
    fast = Settings.__new__(Settings); object.__setattr__(fast, "__dict__", {**s.__dict__, "handler_deadline": 0.05})
    svc_fast = extproc.StraikerExtProc(fast, fd)
    t0 = time.monotonic(); out = await drive(svc_fast, [req_headers(), *body_msg(ANTHRO_REQ)]); dt = time.monotonic() - t0
    check("detect slower than handler deadline -> fail OPEN within deadline", out[-1].WhichOneof("response") == "request_body" and dt < 0.25, f"{dt:.2f}s")
    fd.delay = 0.0

    class Boom(FakeDetect):
        async def post_gateway(self, *a, **k):
            raise RuntimeError("boom")
    svc_boom = extproc.StraikerExtProc(s, Boom())
    out = await drive(svc_boom, [req_headers(), *body_msg(ANTHRO_REQ), resp_headers(), *body_msg(TOOL_SSE, resp=True)])
    check("exception inside handler -> CONTINUE (never stalls the proxy)", all(r.WhichOneof("response") != "immediate_response" for r in out) and len(out) == 4)
    out = await drive(svc, [req_headers(), *body_msg(b"not json at all")])
    check("non-JSON body relayed unscored", out[-1].WhichOneof("response") == "request_body")

    print("== ExtProc: relay fidelity (byte identity) on the real Kong corpus ==")
    n = 0
    if os.path.isdir(KONG_FIX):
        for fn in sorted(os.listdir(KONG_FIX)):
            if not fn.endswith(".wire.jsonl.gz"):
                continue
            rows = [json.loads(l) for l in gzip.open(os.path.join(KONG_FIX, fn), "rt") if l.strip()]
            for r in rows:
                if "/v1/messages" not in r["path"]:
                    continue
                rb = r["req_body"]; raw = (rb if isinstance(rb, str) else json.dumps(rb)).encode()
                fd.posts.clear(); fd.queue = [ALLOW]
                await drive(svc, [req_headers(r["path"]), *body_msg(raw, chunks=5)])
                relayed = fd.posts[0]["body"]
                if hashlib.sha256(relayed).hexdigest() != hashlib.sha256(raw).hexdigest():
                    check(f"relay fidelity {fn} seq={r['seq']}", False, "sha256 mismatch"); break
                n += 1
        check(f"relay fidelity: {n} real request bodies byte-identical after 5-chunk reassembly", n > 0 and FAIL == 0)
    else:
        print("  SKIP (Kong fixtures not found at", KONG_FIX, ")")

    print("== ExtMCP: measured contract ==")
    fm = FakeDetect(); ms = extmcp.StraikerExtMcp(s, fm)
    fm.queue = [{"action": "detect"}, {"action": "detect"}]
    from google.protobuf.struct_pb2 import Struct
    meta = Struct(); meta.update({"user": "mcp@x"})
    req = mpb.McpRequest(service_names=["tasks"], method="tools/call", metadata_context=meta,
                         mcp_request=json.dumps({"name": "run_maintenance", "arguments": {"command": "ls"}}).encode(),
                         headers=[mpb.McpHeader(key="mcp-session-id", value=b"S1")])
    r1 = await ms.CheckRequest(req, None)
    resp = mpb.McpResponse(service_names=["tasks"], method="tools/call", metadata_context=meta,
                           mcp_response=json.dumps({"content": [{"type": "text", "text": "ok"}], "isError": False}).encode())
    r2 = await ms.CheckResponse(resp, None)
    ev = [p["event"] for p in fm.posts]
    check("params-only request -> PreToolUse mcp__tasks__run_maintenance with args", ev[0]["hook_event_name"] == "PreToolUse" and ev[0]["tool_name"] == "mcp__tasks__run_maintenance" and ev[0]["tool_input"] == {"command": "ls"}, str(ev[:1]))
    check("mcp_server_name / mcp_tool_name split", ev[0]["mcp_server_name"] == "tasks" and ev[0]["mcp_tool_name"] == "run_maintenance")
    check("identity from metadata_context + session from mcp-session-id", ev[0]["user_name"] == "mcp@x" and ev[0]["session_id"] == "S1")
    check("result-only response -> PostToolUse paired (same tool, same session, same tool_use_id)",
          ev[1]["hook_event_name"] == "PostToolUse" and ev[1]["tool_name"] == ev[0]["tool_name"] and ev[1]["session_id"] == "S1" and ev[1]["tool_use_id"] == ev[0]["tool_use_id"])
    check("PostToolUse tool_response shaped {content,is_error}", ev[1]["tool_response"] == {"content": "ok", "is_error": False})
    check("both pass", r1.WhichOneof("result") == "pass" and r2.WhichOneof("result") == "pass")
    fm.queue = [{"action": "block", "reason": "bad tool"}]
    r3 = await ms.CheckRequest(req, None)
    check("block -> AuthorizationError PERMISSION_DENIED with JSON-RPC -32001 body",
          r3.WhichOneof("result") == "error" and r3.error.code == mpb.AuthorizationError.PERMISSION_DENIED and b"-32001" in r3.error.mcp_error)
    fm.queue = [{"action": "detect"}]
    r4 = await ms.CheckRequest(mpb.McpRequest(service_names=["tasks"], method="tools/list", mcp_request=b"{}"), None)
    check("non tools/call methods pass without scoring", r4.WhichOneof("result") == "pass")
    check("mcp__ double-underscore split keeps single underscores in server name", extmcp.split_mcp_tool(None, "mcp__my_server__do_thing") == ("mcp__my_server__do_thing", "my_server", "do_thing"))

    print("== config guards ==")
    try:
        os.environ["STRAIKER_DETECT_URL"] = "https://x/api/v1/detect/webhook"; Settings(); check("detect_url may not be /webhook", False)
    except ValueError:
        check("detect_url may not be /webhook", True)
    finally:
        os.environ.pop("STRAIKER_DETECT_URL", None)
    try:
        os.environ["STRAIKER_MODE"] = "yolo"; Settings(); check("mode must be monitor|block", False)
    except ValueError:
        check("mode must be monitor|block", True)
    finally:
        os.environ.pop("STRAIKER_MODE", None)

    print(f"\n{'QA GREEN' if FAIL == 0 else 'QA RED'}  pass={PASS} fail={FAIL}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
