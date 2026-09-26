# Highmark PoV closeout — Straiker × AgentGateway

**Status:** PoV complete (Sep 2026). AWS resources in `us-east-2` torn down; code and findings retained here.
**Audience:** the next SE or Eng who picks up AgentGateway. Written so the deployment can be rebuilt from scratch
without this environment.

---

## 1. What was delivered

Straiker guarding AgentGateway (Linux Foundation AI-native proxy) across three surfaces, with the coding surface
moved from a Python sidecar onto a **compiled-in Rust route policy** that calls Straiker's Detect API directly.

| Surface | Mechanism | Enforcement |
|---|---|---|
| Coding — Claude Code (`/v1/messages`) | compiled `straikerCoding` route policy | `block` — prompt and tool-call adjudicated inline |
| Coding — Claude Code streaming (`/streaming/v1/messages`) | same policy, `monitor` mode | detect-only; response streams untouched (TTFT preserved) |
| Coding — OpenAI dialects (Codex/Cursor/OpenCode/Copilot) | sidecar ExtProc (`127.0.0.1:9000`) | block — pending Argus central-parse for those wires |
| MCP (`/mcp`) | ExtMCP sidecar (`127.0.0.1:9001`) | block via JSON-RPC `-32001` |
| Agentic / chatbot LLM API | native `straiker` LLM guardrail | block per Console control |

The customer-visible win: **no sidecar on the Claude Code path.** The guard is compiled into the binary and calls
`api.prod.straiker.ai/api/v1/detect` through the gateway's own egress, and appears in the UI as a branded
**Straiker (coding)** route policy under the AI group.

## 2. Why the PoV stalled, and the engineering lesson

The integration worked end-to-end *except* the final assistant response never reached the Console. It took four
deploy cycles to find, because **three separate faults stacked on the same code path and every one failed silently** —
each was guarded by a check that returned `false`/`None`, so the logs were clean while the feature was dead.

| # | Fault | Why it was invisible |
|---|---|---|
| 1 | `Stop` event never implemented | pure-text responses returned early with no post |
| 2 | Response body is **gzipped** | `serde_json::from_slice` on compressed bytes → `None` |
| 3 | Body is **SSE**, not JSON (Claude Code sends `stream: true`) | parser only handled the buffered shape → `None` |
| 4 | **`answer_text(&bytes)` still read the raw buffer** after the gzip fix switched the other three call sites to `&decoded` | the `if let` simply didn't match — no error, no log |

Fault 4 is the one that mattered: it made fixes 2 and 3 look ineffective. It was found only by **instrumenting the
branch inputs** — the log reported `has_answer=true` (measured against `decoded`) while the code evaluated
`answer_text(&bytes)` (raw). That discrepancy named the bug in one build.

**Lessons worth carrying forward:**
- Verifying "no errors in the logs" proves nothing when the failure path is a non-matching guard. Verify the
  **event exists**, not that nothing complained.
- Dump one real response body off the wire *before* theorising. gzip + SSE would both have been visible in 30 seconds.
- When a fix changes a buffer, **audit every call site that reads it**. One missed identifier cost three build cycles.
- `contains_tool_use` survived gzip+SSE because it is a substring scan — which is precisely why tool events flowed
  while the answer never did, and why the symptom looked like "Stop is broken".

## 3. Verification method (reusable)

Do not eyeball the Console. Query the tenant's own activity API:

```bash
# PAT -> short-lived access token (note: /auth/token is at the ROOT, not under /api/v3)
TOKEN=$(curl -s -X POST "https://api.prod.straiker.ai/auth/token" \
  -d 'grant_type=urn:ietf:params:oauth:grant-type:token-exchange' \
  -d 'subject_token_type=urn:straiker:params:oauth:token-type:pat' \
  --data-urlencode "subject_token=$STRAIKER_PAT" | jq -r .access_token)

# sessions, then the full transcript (mode=full — the default `focused` hides unflagged turns)
curl -s -H "Authorization: Bearer $TOKEN" \
  "https://api.prod.straiker.ai/api/v3/activity/sessions?application_id=<APP_ID>&limit=30"
curl -s -H "Authorization: Bearer $TOKEN" \
  "https://api.prod.straiker.ai/api/v3/activity/sessions/<SESSION_ID>/transcript?mode=full"
```

A correct coding turn contains `message/user → tool_call/assistant → tool_result/tool → message/assistant`.
**`message/assistant` is the Stop.** Its absence is the regression signature.

## 4. Final measured state (Highmark tenant, app 14916)

Verified via the activity API across 60 sessions:

| Metric | Count |
|---|---|
| Sessions with events | 60 |
| With a tool call | 42 |
| With assistant Stop | 22 |
| Full trace (tool + Stop) | 17 |
| Multi-turn sessions | 16 |

MCP tool calls across three servers (`tasks`, `claims`, `knowledge`): 20.
Findings generated: File Access Boundary Violation ×5, Remote Code Execution ×4, Data Exfiltration ×3,
Destructive Commands ×3, Indirect Prompt Injection ×1.

*(22 Stops vs 42 tool-call sessions: the gap is sessions created before the fix commit `cc6ee16c`. Everything after
it carries a Stop.)*

## 5. Known gaps carried forward

- **Highmark's own fleet is Vertex-locked.** Their Claude Code is pinned to Google Vertex AI by managed config that
  users cannot override, so it never traverses the Anthropic `/v1/messages` route. The demo data was generated with
  an SE consumer key. **Fronting Vertex through the gateway is the real integration for them** and was never built.
- **User attribution ordering.** The gateway's `requestHeaderModifier` sets `x-straiker-user` from `apiKey.user`
  *after* the relay runs, so the relay reads the API-key claims off the request extensions instead. Fixed, but the
  ordering asymmetry between ExtProc (CEL `apiKey.user`) and compiled policies is still latent.
- **OpenAI coding dialects remain on the sidecar** until Argus central-parse covers the OpenAI Chat/Responses wires
  (see `ENG-HANDOFF-central-parse-openai-dialects.md`).
- **Streaming is detect-only by construction.** Buffering a stream to score it destroys TTFT; enforcement on tool
  calls requires the buffered route.

## 6. Code state at closeout

**The fixes are NOT all on `main`.** `main` = `08999446` (Stop code + automatic user, PR #3) but **without** the
gzip, SSE and decoded-buffer fixes — i.e. building from `main` today yields a gateway whose Stop silently never fires.

Branch `feat/straiker-coding-streaming-detect` carries 8 commits ahead of main:

```
28b08bf6  fix(straiker/lab): add stdio entrypoint to claims + knowledge MCP servers
60a1800b  feat(straiker/lab): add claims + knowledge MCP servers as additional /mcp targets
cc6ee16c  fix(straiker-coding): extract the Stop answer from the DECODED body   <-- the real fix
6e9dec65  chore(straiker-coding): instrument the response phase
7cc8f08a  style: rustfmt the SSE answer_text test
f26ab78d  fix(straiker-coding): reassemble SSE to recover the assistant answer
d14c8a02  style: rustfmt the gzip decode test
1a8b9c91  fix(straiker-coding): decode gzipped responses before inspecting them
```

**Action required:** merge this branch to `main`, or `main` remains in a state that looks correct and is not.

## 7. Rebuilding the deployment

1. Build the fork base (Rust + UI, ~20 min) — CodeBuild, never locally:
   `docker build --build-arg VERSION=<tag> --build-arg GIT_REVISION=<sha> -t <ecr>/straiker-agentgateway-fork:<tag> .`
2. Build the runtime image (base + Python sidecar, ~3 min): `straiker/gateway/Dockerfile` with `--build-arg AGW_BASE=<fork image>`.
3. Render config: `straiker/deploy/render_config.py` (substitutes `deploy/agent-keys.env` into the `apiKey.keys` block),
   base64 it into `AGW_CONFIG_B64`.
4. Deploy: `straiker/deploy/apprunner/deploy.sh` with `STRAIKER_GATEWAY_DOMAIN` and `AGW_BASE` set.

Gotchas that cost time:
- ECR-login **before** running `deploy.sh` — its preflight `--validate-only` pulls `AGW_BASE` before the script's own login.
- App Runner caches on image tag; a redeploy needs a **fresh tag**.
- App Runner exposes **one port** — all surfaces share `:3000`; extra `gateways:` listeners are unreachable.
- App Runner drops empty-string env vars (a missing `VERTEX_PROJECT` caused `CREATE_FAILED`).
- The UI renders the running config **including consumer keys** — use a dedicated deploy per customer, or hash keys.
- An MCP stdio server without `mcp.run(transport="stdio")` exits immediately and takes the **whole `/mcp` route** down.

## 8. AWS resources (us-east-2) — torn down at closeout

Deleted:

| Resource | Name |
|---|---|
| App Runner service | `straiker-agentgateway` |
| ECR repository | `straiker-agentgateway` |
| ECR repository | `straiker-agentgateway-fork` |
| CodeBuild project | `agentgateway-fork-build` |
| CodeBuild project | `agentgateway-check` |
| IAM role | `agentgateway-codebuild` |
| CloudWatch log groups | `/aws/apprunner/straiker-agentgateway/*` (application, service) |
| CloudWatch log groups | `/aws/codebuild/agentgateway-{check,fork-build}` |
| Route53 records | `agw.dev.straiker.ai` + 3 ACM validation CNAMEs |

**Deliberately retained** (shared or unrelated — deleting these breaks other services):

- IAM role `AppRunnerECRAccessRole` — **shared** with `straiker-shared-kong-gateway` and
  `straiker-portkey-coding-middleware`.
- App Runner services `ascend-assist`, `estate-universe`, `straiker-shared-kong-gateway`,
  `straiker-portkey-coding-middleware`, `doppelganger` and their ECR repos and instance roles.
- CodeBuild `doppelganger-build` and role `doppelganger-codebuild-role`.
