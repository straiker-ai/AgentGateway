# Customer access — testing the shared agentgateway instance

The customer tests against `https://your gateway host` with **their own gateway key**, and — because
they have their **own Straiker tenant** — their traffic is scored by **their own guardrail key** and lands
in **their** Console, not ours.

## How per-consumer Straiker routing works

Each gateway `apiKey` carries metadata:
```yaml
- key: <customer gateway key>
  metadata: { user: tester@customer.com, straiker_app: customer }
```
agentgateway injects `straiker_app` as a header/attribute on every seam (CEL: `apiKey.straiker_app`).
The Straiker sidecar maps `straiker_app: customer` → the env var **`STRAIKER_APP_CUSTOMER_KEY`**, and uses
*that* key (the customer's own Straiker Defend key) to call `/api/v1/detect`. Set it at deploy time:

```sh
export STRAIKER_APP_CUSTOMER_KEY=<the customer's Straiker Defend app key>   # from THEIR tenant
```
No `straiker_app` on a key → the surface default key (ours). So our keys → our Console, theirs → theirs.
This is `Settings.key_for()` in `straiker_sidecar/config.py`, threaded through ExtProc, ExtMCP, and the
agentic path.

## What the customer runs

**Coding agents** (Claude Code):
```sh
export ANTHROPIC_BASE_URL=https://your gateway host
export ANTHROPIC_CUSTOM_HEADERS="apikey: <their gateway key>"
claude -p "…"
```
**Chatbots / apps** (OpenAI-compatible): base URL `https://your gateway host`, header `apikey: <key>`,
model = `claude-*`, `gpt-*`, `gemini-*`, `databricks-*`, `us.anthropic.*` (Bedrock).

**MCP**: point the MCP client at `https://your gateway host/mcp`.

**Browser playground**: `https://your gateway host/ui` (apiKey-gated) — self-serve test requests.

## Hardening for a shared instance (do this before handing keys out broadly)

The `/ui` renders the running config, which today lists consumer keys in plaintext. Two options:
1. **Hashed keys** — mint consumer keys as `keyHash` (sha256 of the key) instead of plaintext `key`;
   agentgateway authenticates against the hash and the UI cannot reveal the secret. `render_config.py`
   can emit these (`--hash`).
2. **Dedicated deploy** — if the customer needs the admin UI, give them their own App Runner service
   (`SVC=straiker-agentgateway-<customer>` in `deploy.sh`) so they never see other tenants' config.

For a light PoV where only a few trusted testers hold keys, plaintext + apiKey-gated `/ui` is acceptable;
revoke by removing the key line and re-running `deploy.sh`.

## Issuing / revoking a customer key
- Add a line to `deploy/agent-keys.env`: `tester@customer.com=agw_<random>` with the intended identity.
- (Optional) put their Straiker Defend key in `STRAIKER_APP_<REF>_KEY` and set `straiker_app: <ref>` — the
  renderer wires the metadata.
- `./deploy/apprunner/deploy.sh` bakes the key list into the running config. Revoke = delete the line, redeploy.
