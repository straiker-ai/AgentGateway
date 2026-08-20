#!/usr/bin/env python3
"""Offline stand-in for api.prod.straiker.ai — speaks BOTH contracts the sidecar uses, so
block plumbing can be proven deterministically without depending on a tenant's severity gate.

  /api/v1/detect  x-tool gateway (kong-claude-code ...): gateway contract
      request phase  : deny if the LAST user message text contains a DENY token
      response phases: deny if the SSE/JSON carries a tool_use whose input contains a DENY token
  /api/v1/detect  x-tool native (claude-code ...): hook contract — deny PreToolUse/PostToolUse w/ token
  /api/v1/detect/webhook: kong-gateway webhook — {"action":"block"} if request.text has a token
Everything else: allow.   DENY_TOKENS env overrides the default list.
  spec/mock_detect.py [port=9999]
"""
import json, os, sys, http.server

DENY = tuple(t for t in os.environ.get("DENY_TOKENS", "curl evil|rm -rf /|id_rsa|/etc/shadow|DENYME").split("|") if t)


def has_tok(s: str):
    for t in DENY:
        if t in s:
            return t
    return None


def last_user_text(body):
    for m in reversed(body.get("messages") or []):
        if isinstance(m, dict) and m.get("role") == "user":
            c = m.get("content")
            if isinstance(c, str):
                return c
            if isinstance(c, list):
                return "\n".join(p.get("text", "") for p in c if isinstance(p, dict) and p.get("type") == "text")
    return ""


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _send(self, obj, code=200):
        out = json.dumps(obj).encode()
        self.send_response(code); self.send_header("content-type", "application/json"); self.send_header("content-length", str(len(out))); self.end_headers(); self.wfile.write(out)

    def do_POST(self):
        n = int(self.headers.get("content-length", 0)); raw = self.rfile.read(n)
        try:
            body = json.loads(raw)
        except Exception:
            body = {}
        xt = (self.headers.get("x-tool") or "").lower()
        if self.path.endswith("/webhook"):
            tok = has_tok(str((body.get("request") or {}).get("text", "")))
            return self._send({"turn_id": "mock", "action": "block" if tok else "allow", "score": 1.0 if tok else 0.0,
                               "reason": f"mock policy: '{tok}'" if tok else None})
        if xt.endswith("-claude-code") and xt != "claude-code":  # gateway contract
            phase = (self.headers.get("x-straiker-phase") or body.get("straiker_phase") or "request")
            if phase == "request":
                tok = has_tok(last_user_text(body)); ev = "UserPromptSubmit"
            else:
                tok = has_tok(str(body.get("sse", ""))); ev = "PreToolUse"
            deny = bool(tok)
            return self._send({
                "hookSpecificOutput": {"hookEventName": ev if deny else "GatewayRequest",
                                       "permissionDecision": "deny" if deny else "allow",
                                       "permissionDecisionReason": f"Blocked by Straiker (mock policy: '{tok}')" if deny else "allow"},
                "continue": not deny, "stopReason": f"Straiker blocked this ({tok})" if deny else None,
                "straiker": {"phase": phase, "call_kind": "main", "events_scored": 1 if deny else 0,
                             "events": [{"hook_event_name": ev, "action": "block", "score": 0.95, "score_category": "Mock", "severity": "critical", "enforced": True}] if deny else []}})
        # native hook contract
        ti = json.dumps(body.get("tool_input") or {}) + json.dumps(body.get("tool_response") or {})
        tok = has_tok(ti) if body.get("hook_event_name") in ("PreToolUse", "PostToolUse") else None
        if (self.headers.get("Straiker-Debug") or "").upper() == "TRUE":
            return self._send({"turn_id": "mock", "score": 0.95 if tok else 0.0, "severity": "critical" if tok else "low",
                               "score_category": "Mock" if tok else None, "action": "block" if tok else "detect",
                               "reason": f"mock policy: '{tok}'" if tok else None,
                               "hookSpecificOutput": {"hookEventName": body.get("hook_event_name"), "permissionDecision": "deny" if tok else "allow",
                                                      "permissionDecisionReason": f"Blocked by Straiker (mock: '{tok}')" if tok else "allow"}})
        return self._send({"hookSpecificOutput": {"hookEventName": body.get("hook_event_name"), "permissionDecision": "deny" if tok else "allow",
                                                  "permissionDecisionReason": f"Blocked by Straiker (mock: '{tok}')" if tok else "allow"}})


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 9999
    print(f"mock detect on :{port}  DENY={DENY}", flush=True)
    http.server.ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
