"""The only networked module. Ported from portkey-plugin-straiker/middleware/detect_client.py.

Two endpoints, two contracts:

* ``post_gateway`` — central-parse. Verbatim LLM body (or the response-phase envelope) to
  ``/api/v1/detect`` with ``x-tool`` + ``x-straiker-phase`` + identity headers. the Straiker backend
  normalizes into N hook events and returns ONE aggregated verdict
  (``hookSpecificOutput`` + a ``straiker`` debug block).
* ``post_hook`` — native hook contract (one pre-formed hook event), used for MCP events
  synthesized from JSON-RPC. ``Straiker-Debug: TRUE`` so ``action`` is visible for
  prompt-level decisions (the enforce path returns ``{}`` for non-tool events).
* ``post_webhook`` — the agentic/chatbot contract (``/detect/webhook``,
  ``X-Straiker-Webhook-Format: kong-gateway``). Blocks on ``action == "block"``.

Every call returns ``None`` on transport failure; callers fail open on None.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from typing import Any

import httpx

from config import Settings

log = logging.getLogger("straiker.detect")


class DetectClient:
    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        self.s = settings
        self.c = client

    def _sign(self, key: str, payload: str) -> dict[str, str]:
        if not self.s.sign_payloads:
            return {}
        ts = str(int(time.time()))
        sig = hmac.new(key.encode(), f"{ts}.{payload}".encode(), hashlib.sha256).hexdigest()
        return {"X-Straiker-Webhook-Signature": sig, "X-Straiker-Webhook-Timestamp": ts}

    async def _post(self, url: str, key: str, payload: str, headers: dict[str, str], what: str) -> dict[str, Any] | None:
        h = {"Content-Type": "application/json", "Authorization": f"Bearer {key}", **headers, **self._sign(key, payload)}
        t0 = time.monotonic()
        try:
            r = await self.c.post(url, content=payload, headers=h, timeout=self.s.detect_timeout)
            dt = (time.monotonic() - t0) * 1000
            if r.status_code >= 300:
                # unconditional non-2xx logging — a silent 4xx looks exactly like "no detections"
                log.warning("straiker %s -> HTTP %s in %.0fms: %s", what, r.status_code, dt, r.text[:300])
                return None
            out = r.json()
            log.info("straiker %s -> %s in %.0fms", what, r.status_code, dt)
            return out if isinstance(out, dict) else None
        except (httpx.HTTPError, ValueError) as e:
            log.warning("straiker %s -> transport error after %.0fms: %s", what, (time.monotonic() - t0) * 1000, e)
            return None

    async def post_gateway(self, body: bytes, *, phase: str, session_id: str | None, user: str | None,
                           model: str | None = None, key: str | None = None) -> dict[str, Any] | None:
        """Central-parse relay. ``body`` is the VERBATIM client request (phase=request) or the
        ``{"straiker_phase","sse","model","request"}`` envelope (phase=response|response-sync)."""
        h = {"x-tool": self.s.x_tool, "x-straiker-phase": phase}
        if session_id:
            h["x-claude-code-session-id"] = session_id
        if user:
            h["x-straiker-user"] = user
        if model:
            h["x-straiker-model"] = model
        return await self._post(self.s.detect_url, key or self.s.coding_key, body.decode("utf-8", "replace"), h, f"gateway/{phase}")

    async def post_hook(self, event: dict[str, Any], *, x_tool: str | None = None, key: str | None = None) -> dict[str, Any] | None:
        """Native hook contract: one pre-formed hook event (used for MCP-synthesized events)."""
        payload = json.dumps(event, separators=(",", ":"), ensure_ascii=False)
        h = {"x-tool": x_tool or self.s.mcp_x_tool, "Straiker-Debug": "TRUE"}
        return await self._post(self.s.detect_url, key or self.s.mcp_key, payload, h, f"hook/{event.get('hook_event_name')}")

    async def post_standard(self, *, prompt: str | None, app_response: str | None, session_id: str | None,
                            user: str | None, model: str | None = None, key: str | None = None) -> dict[str, Any] | None:
        """Agentic/chatbot scoring via the STANDARD detect contract (not /detect/webhook).

        /detect/webhook (kong-gateway format) was measured NOT to enforce block mode reliably on this
        backend; the standard /api/v1/detect with {prompt, app_response} does, and it is the contract the
        whole product is built on. No x-tool -> the normal app pipeline. Straiker-Debug:TRUE so
        action/score come back (they encode the block decision). Blocks on action == "block".
        """
        body: dict[str, Any] = {}
        if prompt: body["prompt"] = prompt
        if app_response: body["app_response"] = app_response
        if session_id: body["session_id"] = session_id
        if user: body["user_name"] = user
        if not body: return None
        payload = json.dumps(body, separators=(",", ":"), ensure_ascii=False)
        return await self._post(self.s.detect_url, key or self.s.agentic_key, payload,
                                {"Straiker-Debug": "TRUE"}, f"standard/{'resp' if app_response else 'req'}")

    async def post_agentic(self, *, messages, source, session_id, user, model=None, destination=None,
                           hook=None, key=None) -> dict | None:
        """AGENTIC contract: POST /api/v1/detect?agentic with the full `messages` trace (system prompt,
        tool_calls, tool results, MCP). This is what makes an app auto-enumerate as AGENTIC with a full
        trace in the Console (vs the flat {prompt,app_response} traditional path). `source` maps/creates
        the app; metadata stitches the session and multi-agent hops."""
        if not messages:
            return None
        body: dict = {"messages": messages, "source": source,
                      "metadata": {k: v for k, v in {"user_name": user, "session_id": session_id,
                                                     "app_name": source, "source": "agentgateway"}.items() if v}}
        if destination:
            body["destination"] = destination
        ann = {k: v for k, v in {"model": model, "hook": hook, "source": "agentgateway"}.items() if v}
        if ann:
            body["annotations"] = ann
        payload = json.dumps(body, separators=(",", ":"), ensure_ascii=False)
        url = self.s.detect_url + ("&" if "?" in self.s.detect_url else "?") + "agentic"
        return await self._post(url, key or self.s.agentic_key, payload, {"Straiker-Debug": "TRUE"},
                                f"agentic/{source}")

    async def post_webhook(self, payload: dict[str, Any], *, key: str | None = None) -> dict[str, Any] | None:
        """Agentic/chatbot contract (kong-gateway webhook format)."""
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        h = {"X-Straiker-Webhook-Format": "kong-gateway"}
        return await self._post(self.s.webhook_url, key or self.s.agentic_key, body, h, f"webhook/{payload.get('eventType')}")


# ---- verdict helpers (shared by all seams) ------------------------------------------------

def is_block(verdict: dict[str, Any] | None) -> bool:
    """True when Straiker says block. Keys on BOTH signals: the scoring ``action`` (carries the
    killswitch for prompt-level turns, which hookSpecificOutput omits) and the enforce
    ``permissionDecision``. Either one means block."""
    if not verdict:
        return False
    if verdict.get("action") == "block":
        return True
    hso = verdict.get("hookSpecificOutput") or {}
    if hso.get("permissionDecision") == "deny":
        return True
    if verdict.get("continue") is False:
        return True
    return False


def block_reason(verdict: dict[str, Any] | None, default: str = "Request blocked by Straiker guardrails.") -> str:
    """Human-facing reason. Straiker may return reason=null/empty — always return readable text."""
    if not verdict:
        return default
    for k in ("stopReason", "reason"):
        v = verdict.get(k)
        if isinstance(v, str) and v.strip():
            return v
    hso = verdict.get("hookSpecificOutput") or {}
    v = hso.get("permissionDecisionReason")
    if isinstance(v, str) and v.strip():
        return v
    return default
