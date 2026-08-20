# Latency — buffered enforcement vs streaming

Measured against **prod** `/api/v1/detect` on the 19 real Claude Code capture bodies (`spec/latency.py`,
3 samples each, 210 posts/phase). Bodies range 2.4KB (title calls) → 164KB (main-loop turns), p50 140KB.

| phase | n | p50 | p95 | max |
|---|---|---|---|---|
| `request` (pre-upstream) | 210 | **190ms** | 268ms | 3660ms |
| `response-sync` (buffered, holds client before tool runs) | 210 | **193ms** | 261ms | 1247ms |

Budget: our handler deadline is 6s and agentgateway hard-caps webhook calls at **10s**. p95 ≈ 265ms
sits comfortably inside both. The rare multi-second max is a cold-path prod call, still < the deadline;
on the deadline we **fail open** (never a false block from our own slowness).

## The buffered ↔ streaming trade

- **Buffered** (`responseBodyMode: buffered`, `/v1/messages`): the gateway holds the full response,
  Straiker scores the `tool_use` blocks, and a dangerous call is replaced with a block **before the
  client can execute it**. Adds the response-sync round-trip (~190ms p50) and delays time-to-first-token
  until the response is scored. This is the only mode that *prevents* a command rather than reporting it.
- **Streaming** (`fullDuplexStreamed`, `/streaming/v1/messages`): tokens flow to the client untouched;
  Straiker scores asynchronously. TTFT is preserved; enforcement is monitor-only because bytes already
  sent cannot be recalled.

Recommendation for a PoV: default **monitor** (either route), demo **block** on the buffered route where
a `curl … | sh` tool call is stopped mid-turn. Match Kong's guidance: killswitch/enforcement *feels*
better on the buffered route because it cleanly cuts the turn off.
