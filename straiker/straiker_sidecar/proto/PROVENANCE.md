# Proto provenance
Copied verbatim from https://github.com/agentgateway/agentgateway at tag **v1.4.1**:
- `crates/protos/proto/ext_proc.proto` — Envoy `envoy.service.ext_proc.v3` (API-compatible)
- `crates/protos/proto/ext_mcp.proto` — `agentgateway.dev.ext_mcp.ExtMcp`
- `crates/protos/proto/shared_envoy.proto` — shared Envoy types

Bump = re-copy at the new tag + `./regen.sh`; `promote.sh` cross-checks the tag against `gateway/VERSION`.
