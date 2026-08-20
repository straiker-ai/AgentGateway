"""Env-driven settings. One process serves all three agentgateway seams, so one Settings.

The two fields that matter most:

* ``x_tool`` — the central-parse platform label. ``kong-claude-code`` is what prod accepts
  today (verified 2026-08-19: 200 with the gateway contract; ``agentgateway-claude-code``
  returns 400 until the Straiker backend registers it). The Bearer key, not x-tool, selects the Console
  app, so this is correct attribution from day one. Flip to ``agentgateway-claude-code``
  by env when the backend update lands — the APIM ``straikerXTool`` pattern.
* ``coding_key`` vs ``agentic_key`` — separate Straiker app keys per surface, because the
  backend files an app under Coding Agents or Custom Agents from the traffic it sees; one
  key serving both mixes them. The user minted a dedicated agentgateway key for this.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _bool(name: str, default: bool) -> bool:
    v = _env(name)
    return default if v == "" else v.lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    # Straiker
    detect_url: str = field(default_factory=lambda: _env("STRAIKER_DETECT_URL", "https://api.prod.straiker.ai/api/v1/detect"))
    webhook_url: str = field(default_factory=lambda: _env("STRAIKER_WEBHOOK_URL", "https://api.prod.straiker.ai/api/v1/detect/webhook"))
    coding_key: str = field(default_factory=lambda: _env("STRAIKER_AGENTGATEWAY_CODING_KEY") or _env("STRAIKER_AGENTGATEWAY_KEY") or _env("STRAIKER_CODING_KEY"))
    agentic_key: str = field(default_factory=lambda: _env("STRAIKER_AGENTGATEWAY_AGENTIC_KEY") or _env("STRAIKER_AGENTGATEWAY_KEY") or _env("STRAIKER_API_KEY"))
    mcp_key: str = field(default_factory=lambda: _env("STRAIKER_AGENTGATEWAY_MCP_KEY") or _env("STRAIKER_AGENTGATEWAY_KEY") or _env("STRAIKER_CODING_KEY"))
    x_tool: str = field(default_factory=lambda: _env("STRAIKER_X_TOOL", "kong-claude-code"))
    mcp_x_tool: str = field(default_factory=lambda: _env("STRAIKER_MCP_X_TOOL", "claude-code"))
    # enforcement posture: monitor (score, never deny) | block (deny on action==block / permissionDecision==deny)
    mode: str = field(default_factory=lambda: _env("STRAIKER_MODE", "block"))
    fail_open: bool = field(default_factory=lambda: _bool("STRAIKER_FAIL_OPEN", True))
    # agentgateway hardcodes a 10s webhook timeout; our whole handler must fit inside it.
    detect_timeout: float = field(default_factory=lambda: float(_env("STRAIKER_DETECT_TIMEOUT", "6.0")))
    handler_deadline: float = field(default_factory=lambda: float(_env("STRAIKER_HANDLER_DEADLINE", "8.0")))
    sign_payloads: bool = field(default_factory=lambda: _bool("STRAIKER_SIGN_PAYLOADS", True))
    default_user: str = field(default_factory=lambda: _env("STRAIKER_DEFAULT_USER", "agentgateway-user"))
    # listeners
    http_host: str = field(default_factory=lambda: _env("SIDECAR_HTTP_HOST", "127.0.0.1"))
    http_port: int = field(default_factory=lambda: int(_env("SIDECAR_HTTP_PORT", "8080")))
    extproc_port: int = field(default_factory=lambda: int(_env("SIDECAR_EXTPROC_PORT", "9000")))
    extmcp_port: int = field(default_factory=lambda: int(_env("SIDECAR_EXTMCP_PORT", "9001")))
    grpc_host: str = field(default_factory=lambda: _env("SIDECAR_GRPC_HOST", "127.0.0.1"))
    debug_tap: bool = field(default_factory=lambda: _bool("STRAIKER_DEBUG_TAP", False))
    max_body_bytes: int = field(default_factory=lambda: int(_env("STRAIKER_MAX_BODY_BYTES", str(8 * 1024 * 1024))))

    def __post_init__(self) -> None:
        # The webhook path is a different contract (non-enforcing for coding agents). Never let
        # a misconfig point the coding relay at it — same guard as the Kong schema.
        if self.detect_url.rstrip("/").endswith("/webhook"):
            raise ValueError("STRAIKER_DETECT_URL must be /api/v1/detect, not /detect/webhook")
        if self.mode not in ("monitor", "block"):
            raise ValueError("STRAIKER_MODE must be monitor|block")

    @property
    def enforce(self) -> bool:
        return self.mode == "block"

    def key_for(self, app_ref: str | None, default: str) -> str:
        """Per-consumer Straiker key. A gateway consumer's apiKey metadata carries `straiker_app: <ref>`
        (CEL-injected as x-straiker-app); we map <ref> -> env STRAIKER_APP_<REF>_KEY. This is how a
        CUSTOMER points the shared gateway at THEIR OWN Straiker tenant/app: their guardrail key lives in
        env, their traffic lands in their Console, not ours. Falls back to the surface default key."""
        if app_ref:
            k = os.environ.get(f"STRAIKER_APP_{app_ref.upper().replace('-', '_').replace('.', '_')}_KEY", "").strip()
            if k:
                return k
        return default
