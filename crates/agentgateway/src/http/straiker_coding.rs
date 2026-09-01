// Straiker DefendAI coding guard — a compiled-in, route-level policy for Claude Code traffic.
//
// On a coding route (Claude Code `POST /v1/messages`) this guard relays the verbatim
// request/response of each tool-call turn to Straiker's Detect API for central-parse, so a single
// Console record carries the full agentic trace (UserPromptSubmit + PostToolUse on the request,
// PreToolUse on the response). It calls Straiker directly — no sidecar — reusing the native
// Straiker guard's HTTP egress plumbing (`client.call_with_explicit_policies_list` against a mock
// backend with system-trust TLS).
//
// It is modeled on the ExtProc route policy: a config struct that `build`s a per-request handle
// whose `mutate_request` / `mutate_response` buffer the full body, relay it, and either re-attach
// the identical bytes (allow) or replace them (block). Unlike ExtProc it decides synchronously —
// the request block is returned as a `PolicyResponse` direct-response, which short-circuits before
// the upstream call — so `take_body_immediate_response` is a no-op kept for dispatch symmetry.
//
// Contract (VERIFIED): POST `{base_url}/api/v1/detect` (default `https://api.prod.straiker.ai`).
//   - Request phase: body = the verbatim `/v1/messages` request bytes, unmodified. Header
//     `x-straiker-phase: request`. On block the prompt never reaches the model.
//   - Response phase (only when the body contains a tool call): body = a JSON envelope
//     `{"straiker_phase":"response-sync","sse":<response body as string>,"model":<model>,
//     "request":<original request JSON>}`. Header `x-straiker-phase: response-sync`. On block the
//     tool_use response is replaced with a well-formed Anthropic text message.
use agent_core::strng;
use agent_core::strng::Strng;
use async_compression::tokio::bufread::GzipDecoder;
use bytes::Bytes;
use serde::Deserialize;
use tokio::io::{AsyncReadExt, BufReader};
use tracing::warn;

use crate::cel::RequestSnapshot;
use crate::http::{self, PolicyResponse};
use crate::json;
use crate::llm::policy::FailureMode;
use crate::proxy::ProxyError;
use crate::proxy::httpproxy::PolicyClient;
use crate::telemetry::metrics::{OutboundCallKind, OutboundCallSubtype};
use crate::types::agent::{Backend, BackendTrafficPolicy, ResourceName};
use crate::{apply, schema};

const DEFAULT_BASE_URL: &str = "https://api.prod.straiker.ai";
const DEFAULT_X_TOOL: &str = "kong-claude-code";
/// The final assistant answer is posted as a PRE-FORMED hook event on the native contract, the same
/// tag the sidecar, the Kong plugin and the LiteLLM guardrail use — not the central-parse tag that
/// carries the verbatim relay.
const STOP_X_TOOL: &str = "claude-code";
/// Message surfaced to the coding client when a turn is blocked.
const BLOCK_REQUEST_TEXT: &str = "This prompt was blocked by Straiker DefendAI.";
const BLOCK_RESPONSE_TEXT: &str = "This tool call was blocked by Straiker DefendAI.";

/// Enforcement mode for the coding guard.
#[apply(schema!)]
#[derive(Default, Copy, PartialEq, Eq)]
pub enum StraikerCodingMode {
	/// Score every turn but never block; only record the verdict.
	Monitor,
	/// Block prompts and tool-call responses that Straiker flags (default).
	#[default]
	Block,
}

/// Configuration for the Straiker DefendAI coding guard.
///
/// Scores Claude Code prompts and tool-call responses against the tenant's Straiker runtime
/// guardrail policy by calling the Straiker Detect API directly. The `api_key` selects the
/// tenant + app, so a customer configures this guard entirely in the UI by pasting their key.
#[apply(schema!)]
pub struct StraikerCoding {
	/// Straiker Defend application key (sent as a Bearer token). Selects the tenant and app.
	pub api_key: Strng,
	/// Straiker API base URL. Defaults to `https://api.prod.straiker.ai`.
	#[serde(default, skip_serializing_if = "Option::is_none")]
	pub base_url: Option<Strng>,
	/// Application name for Console auto-enumeration (`x-straiker-source`). A request
	/// `x-straiker-source` header overrides this per call.
	#[serde(default, skip_serializing_if = "Option::is_none")]
	pub source: Option<Strng>,
	/// Integration tag forwarded as the `x-tool` header. Defaults to `kong-claude-code`.
	#[serde(default, skip_serializing_if = "Option::is_none")]
	pub x_tool: Option<Strng>,
	/// Whether to block flagged turns or only record them. Defaults to `block`.
	#[serde(default, skip_serializing_if = "crate::serdes::is_default")]
	pub mode: StraikerCodingMode,
	/// Behaviour when Straiker is unreachable. Defaults to `failOpen` so a guardrail
	/// outage never takes down live coding traffic.
	#[serde(default = "straiker_coding_fail_open")]
	pub failure_mode: FailureMode,
	/// Backend policies used when calling Straiker (TLS, etc.).
	#[serde(
		default,
		deserialize_with = "crate::types::local::de_from_local_backend_policy",
		skip_serializing_if = "Vec::is_empty"
	)]
	#[cfg_attr(
		feature = "schema",
		schemars(with = "Option<crate::types::local::SimpleLocalBackendPolicies>")
	)]
	pub policies: Vec<BackendTrafficPolicy>,
}

fn straiker_coding_fail_open() -> FailureMode {
	FailureMode::FailOpen
}

impl StraikerCoding {
	pub fn build(&self, client: PolicyClient) -> StraikerCodingRequest {
		StraikerCodingRequest {
			guard: self.clone(),
			client,
			captured: None,
		}
	}

	fn detect_url(&self) -> String {
		let b = self
			.base_url
			.as_ref()
			.map(|u| u.as_str())
			.unwrap_or(DEFAULT_BASE_URL);
		format!("{}/api/v1/detect", b.trim_end_matches('/'))
	}

	fn x_tool(&self) -> String {
		self
			.x_tool
			.as_ref()
			.map(|s| s.to_string())
			.unwrap_or_else(|| DEFAULT_X_TOOL.to_string())
	}
}

// This guard is not a CEL-driven policy, but it is stored as a `RequestPolicy<StraikerCoding>` and
// so must satisfy the `HasExpressions` bound; it has no expressions to register.
impl crate::store::HasExpressions for StraikerCoding {}

/// Straiker's Detect verdict. Only the decision fields are read; extra fields are ignored so the
/// contract can evolve. A block is `action == "block"`, or (when present) a Claude-Code-style
/// `permissionDecision == "deny"` / `continue == false`.
#[derive(Debug, Clone, Default, Deserialize)]
#[serde(default)]
pub struct StraikerVerdict {
	pub action: Option<String>,
	pub score: Option<f64>,
	pub score_category: Option<String>,
	pub reason: Option<String>,
	#[serde(rename = "permissionDecision")]
	pub permission_decision: Option<String>,
	#[serde(rename = "continue")]
	pub continue_: Option<bool>,
}

impl StraikerVerdict {
	pub fn is_block(&self) -> bool {
		if matches!(self.action.as_deref(), Some(a) if a.eq_ignore_ascii_case("block")) {
			return true;
		}
		if matches!(self.permission_decision.as_deref(), Some(d) if d.eq_ignore_ascii_case("deny")) {
			return true;
		}
		self.continue_ == Some(false)
	}
}

/// Which side of the turn a Detect call represents.
#[derive(Debug, Clone, Copy)]
enum Phase {
	Request,
	ResponseSync,
}

impl Phase {
	fn as_str(self) -> &'static str {
		match self {
			Phase::Request => "request",
			Phase::ResponseSync => "response-sync",
		}
	}
}

/// The verbatim request captured on the request side so the response guard can pair the turn and
/// rebuild the response envelope. Captured here (rather than from the `RequestSnapshot`) because
/// the exact original bytes are guaranteed present, whereas the snapshot's body is only populated
/// if some other policy buffered it.
#[derive(Debug)]
struct CapturedRequest {
	body: Bytes,
	session: Option<String>,
	user: Option<String>,
	model: Option<String>,
}

/// Per-request handle built from [`StraikerCoding`]. The same instance handles both the request and
/// response phase of a turn, so request context captured in `mutate_request` is available in
/// `mutate_response`.
#[derive(Debug)]
pub struct StraikerCodingRequest {
	guard: StraikerCoding,
	client: PolicyClient,
	captured: Option<CapturedRequest>,
}

impl StraikerCodingRequest {
	/// Relay the verbatim prompt to Straiker before it reaches the model. On a block verdict the
	/// prompt is short-circuited with an Anthropic-shaped message (via the returned `PolicyResponse`,
	/// which is applied before the upstream call).
	pub async fn mutate_request(
		&mut self,
		req: &mut http::Request,
	) -> Result<PolicyResponse, ProxyError> {
		// Buffer the full body so we can relay it verbatim and re-attach the identical bytes.
		let limit = http::buffer_limit(req);
		let body = std::mem::replace(req.body_mut(), http::Body::empty());
		let bytes = match http::read_body_with_limit(body, limit).await {
			Ok(b) => b,
			// The body is consumed and unrecoverable; we cannot safely forward the request.
			Err(e) => {
				return Err(ProxyError::Processing(
					anyhow::Error::new(e).context("straiker coding: buffer request body"),
				));
			},
		};

		let session = session_id(req.headers());
		let user = user_name(req);
		let model = model_from_body(&bytes);

		let headers = detect_headers(
			&self.guard,
			Phase::Request,
			req.headers(),
			session.as_deref(),
			user.as_deref(),
			model.as_deref(),
		);
		let verdict = post(&self.client, &self.guard, headers, bytes.clone()).await;

		// Remember the turn so the response guard can pair it and rebuild the envelope.
		self.captured = Some(CapturedRequest {
			body: bytes.clone(),
			session,
			user,
			model: model.clone(),
		});

		match verdict {
			Ok(v) if v.is_block() && self.guard.mode == StraikerCodingMode::Block => {
				let model = model.as_deref().unwrap_or("unknown");
				let block = anthropic_block_response(model, BLOCK_REQUEST_TEXT);
				Ok(PolicyResponse::default().with_response(block))
			},
			Ok(_) => {
				// Allow: re-attach the identical bytes.
				*req.body_mut() = http::Body::from(bytes);
				Ok(PolicyResponse::default())
			},
			Err(e) => {
				*req.body_mut() = http::Body::from(bytes);
				self.on_guard_error("straiker coding request", e)
			},
		}
	}

	/// Relay a tool-call response to Straiker. On a block verdict the executable `tool_use` response
	/// is replaced in place with a well-formed Anthropic text message so the client never receives
	/// the tool call. Pure-text responses are passed through untouched.
	pub async fn mutate_response(
		&mut self,
		resp: &mut http::Response,
		// Request context is taken from `self.captured` (guaranteed verbatim). The snapshot is kept
		// in the signature to match the ext_proc dispatch surface.
		_request: Option<&RequestSnapshot>,
	) -> Result<PolicyResponse, ProxyError> {
		// Monitor (detect) mode never blocks, so there is nothing to enforce on the response.
		// Skip response handling entirely and let the body flow through untouched. This is what
		// makes the guard safe on a STREAMING route: buffering the response to score it would
		// collect every SSE frame before releasing any, destroying token-by-token streaming/TTFT.
		// Streaming is therefore a detect-only surface — the prompt is scored on the request phase
		// (`mutate_request`, which never blocks in Monitor mode), and the model's response streams
		// back unmodified. Use `Block` mode (buffered route) for full enforcement incl. tool-call blocking.
		if self.guard.mode == StraikerCodingMode::Monitor {
			return Ok(PolicyResponse::default());
		}

		let limit = http::response_buffer_limit(resp);
		let body = std::mem::replace(resp.body_mut(), http::Body::empty());
		let bytes = match http::read_body_with_limit(body, limit).await {
			Ok(b) => b,
			Err(e) => {
				return Err(ProxyError::Processing(
					anyhow::Error::new(e).context("straiker coding: buffer response body"),
				));
			},
		};

		// Upstream returns the body `content-encoding: gzip`, so every inspection below must run on the
		// DECODED bytes: a raw byte scan never matches `tool_use` and a raw parse never yields the
		// answer, which silently skipped BOTH the response-phase enforcement point and the Stop event
		// (no error was logged because each was guarded by a check that simply came back false/None).
		// The client still receives the original, untouched bytes.
		let decoded = decoded_body(&bytes, resp.headers()).await;

		// Instrumentation: every one of these facts was individually verified against real captured
		// bytes, yet no Stop reached the Console — so log the actual branch inputs rather than guess.
		tracing::info!(
			raw_len = bytes.len(),
			decoded_len = decoded.len(),
			has_tool = contains_tool_use(&decoded),
			has_captured = self.captured.is_some(),
			has_answer = answer_text(&decoded).is_some(),
			"straiker coding: response phase"
		);

		// Only enforce on tool-call responses (substring check mirrors the sidecar).
		if !contains_tool_use(&decoded) {
			// Pure-text final answer: post an explicit `Stop` hook event carrying the assistant reply so
			// the answer always lands in the Console as a Stop, completing the turn's trace
			// (UserPromptSubmit -> PostToolUse -> PreToolUse -> Stop) at parity with the sidecar and the
			// other gateway integrations. Stop is a trace event and never blocks, so a failure here is
			// logged and the response is returned untouched.
			if let Some(captured) = self.captured.as_ref()
				&& let Some(answer) = answer_text(&decoded)
			{
				let headers = stop_headers(&self.guard, resp.headers(), captured);
				let payload = stop_event_json(&answer, captured);
				match post(&self.client, &self.guard, headers, payload).await {
					Ok(_) => tracing::info!("straiker coding: stop posted"),
					Err(e) => {
						warn!(error = %e, phase = "straiker coding stop", "straiker coding stop post failed")
					},
				}
			}
			*resp.body_mut() = http::Body::from(bytes);
			return Ok(PolicyResponse::default());
		}
		let Some(captured) = self.captured.as_ref() else {
			// No paired request; cannot build the envelope. Pass the response through unchanged.
			*resp.body_mut() = http::Body::from(bytes);
			return Ok(PolicyResponse::default());
		};

		let envelope = response_envelope(&decoded, captured);
		let headers = detect_headers(
			&self.guard,
			Phase::ResponseSync,
			resp.headers(),
			captured.session.as_deref(),
			captured.user.as_deref(),
			captured.model.as_deref(),
		);
		let verdict = post(&self.client, &self.guard, headers, envelope).await;

		match verdict {
			Ok(v) if v.is_block() && self.guard.mode == StraikerCodingMode::Block => {
				let model = captured.model.as_deref().unwrap_or("unknown");
				replace_response_with_block(resp, model, BLOCK_RESPONSE_TEXT);
				Ok(PolicyResponse::default())
			},
			Ok(_) => {
				*resp.body_mut() = http::Body::from(bytes);
				Ok(PolicyResponse::default())
			},
			Err(e) => {
				*resp.body_mut() = http::Body::from(bytes);
				self.on_guard_error("straiker coding response", e)
			},
		}
	}

	/// Kept to mirror the ext_proc dispatch surface. StraikerCoding decides synchronously and returns
	/// any block via the `PolicyResponse`, so there is never a deferred body-phase response here.
	pub fn take_body_immediate_response(&self) -> Option<http::Response> {
		None
	}

	fn on_guard_error(&self, ctx: &str, e: anyhow::Error) -> Result<PolicyResponse, ProxyError> {
		match self.guard.failure_mode {
			FailureMode::FailOpen => {
				warn!(error = %e, phase = ctx, "straiker coding guard call failed; failing open");
				Ok(PolicyResponse::default())
			},
			FailureMode::FailClosed => Err(ProxyError::Processing(e.context(ctx.to_string()))),
		}
	}
}

/// Replicated from the native Straiker guard: replaces `with_default_timeout` (private to the LLM
/// policy module) so this module stays self-contained.
fn with_default_timeout(mut req: http::Request) -> http::Request {
	req
		.extensions_mut()
		.insert(crate::http::filters::BackendRequestTimeout(
			std::time::Duration::from_secs(10),
		));
	req
}

async fn post(
	client: &PolicyClient,
	guard: &StraikerCoding,
	headers: Vec<(&'static str, String)>,
	body: Bytes,
) -> anyhow::Result<StraikerVerdict> {
	let mut pols = vec![BackendTrafficPolicy::BackendTLS(
		crate::http::backendtls::SYSTEM_TRUST.clone(),
	)];
	pols.extend(guard.policies.iter().cloned());

	let mut builder = ::http::Request::builder()
		.uri(guard.detect_url())
		.method(::http::Method::POST)
		.header(::http::header::CONTENT_TYPE, "application/json")
		.header(
			::http::header::AUTHORIZATION,
			format!("Bearer {}", guard.api_key),
		);
	for (name, value) in &headers {
		builder = builder.header(*name, value.as_str());
	}
	let req = builder.body(http::Body::from(body))?;

	let mock_be = Backend::Dynamic(
		ResourceName::new(strng::literal!("_straiker-detect"), strng::literal!("")),
		None,
	);
	let resp = client
		.with_outbound(OutboundCallKind::Policy, OutboundCallSubtype::Guardrail)
		.call_with_explicit_policies_list(with_default_timeout(req), mock_be, pols)
		.await?;
	let verdict: StraikerVerdict = json::from_response_body(resp).await?;
	Ok(verdict)
}

/// Headers for the `Stop` post. The final answer is a pre-formed hook event, so it goes on the
/// native `x-tool: claude-code` contract with the turn's correlation identity.
fn stop_headers(
	guard: &StraikerCoding,
	incoming: &::http::HeaderMap,
	c: &CapturedRequest,
) -> Vec<(&'static str, String)> {
	let mut h = vec![("x-tool", STOP_X_TOOL.to_string())];
	if let Some(src) = source(guard, incoming) {
		h.push(("x-straiker-source", src));
	}
	if let Some(s) = &c.session {
		h.push(("x-claude-code-session-id", s.clone()));
	}
	if let Some(u) = &c.user {
		h.push(("x-straiker-user", u.clone()));
	}
	if let Some(m) = &c.model {
		h.push(("x-straiker-model", m.clone()));
	}
	h
}

/// Upstream returns `/v1/messages` responses `content-encoding: gzip`, so every inspection the guard
/// does must run on the DECODED bytes. Scanning the compressed body never matches `tool_use` and
/// parsing it never yields the answer, which silently disabled both the response-phase enforcement
/// point and the `Stop` event. Falls back to the raw bytes if decoding fails, so a decode problem
/// degrades the guard rather than dropping the turn. The client always receives the original bytes.
async fn decoded_body(bytes: &Bytes, headers: &::http::HeaderMap) -> Bytes {
	let gzipped = headers
		.get(::http::header::CONTENT_ENCODING)
		.and_then(|v| v.to_str().ok())
		.is_some_and(|v| v.to_ascii_lowercase().contains("gzip"))
		|| bytes.starts_with(&[0x1f, 0x8b]);
	if !gzipped {
		return bytes.clone();
	}
	let mut decoder = GzipDecoder::new(BufReader::new(&bytes[..]));
	let mut out = Vec::new();
	match decoder.read_to_end(&mut out).await {
		Ok(_) => Bytes::from(out),
		Err(e) => {
			warn!(error = %e, "straiker coding: response gunzip failed; inspecting raw bytes");
			bytes.clone()
		},
	}
}

/// The assistant's final text answer from a buffered Anthropic `/v1/messages` response body.
fn answer_text(body: &[u8]) -> Option<String> {
	// Buffered JSON message: `content[]` blocks of type `text`.
	if let Ok(v) = serde_json::from_slice::<serde_json::Value>(body)
		&& let Some(content) = v.get("content").and_then(|c| c.as_array())
	{
		let mut out = String::new();
		for c in content {
			if c.get("type").and_then(|t| t.as_str()) == Some("text")
				&& let Some(t) = c.get("text").and_then(|t| t.as_str())
			{
				out.push_str(t);
			}
		}
		if !out.is_empty() {
			return Some(out);
		}
	}
	// SSE stream — what Claude Code actually receives, since it sends `stream: true`. The answer
	// arrives as `content_block_delta` frames and must be reassembled. Parsing only the buffered
	// shape silently returned `None` on every real turn, so no `Stop` was ever posted.
	let text = std::str::from_utf8(body).ok()?;
	let mut out = String::new();
	for line in text.lines() {
		let Some(payload) = line.trim().strip_prefix("data:") else {
			continue;
		};
		let payload = payload.trim();
		if payload.is_empty() || payload == "[DONE]" {
			continue;
		}
		let Ok(v) = serde_json::from_str::<serde_json::Value>(payload) else {
			continue;
		};
		if v.get("type").and_then(|t| t.as_str()) == Some("content_block_delta")
			&& let Some(t) = v.pointer("/delta/text").and_then(|t| t.as_str())
		{
			out.push_str(t);
		}
	}
	(!out.is_empty()).then_some(out)
}

/// The Claude Code `Stop` hook event carrying the final assistant answer — the same shape the
/// sidecar and the other gateway integrations post, so the reply lands in the Console as a `Stop`
/// rather than relying on the backend to derive one.
fn stop_event_json(answer: &str, c: &CapturedRequest) -> Bytes {
	let mut ev = serde_json::json!({
		"hook_event_name": "Stop",
		"session_id": c.session.clone().unwrap_or_default(),
		"app_response": answer,
		"stop_reason": "end_turn",
	});
	if let Some(u) = &c.user {
		ev["user_name"] = serde_json::Value::String(u.clone());
	}
	if let Some(m) = &c.model {
		ev["model"] = serde_json::Value::String(m.clone());
	}
	Bytes::from(serde_json::to_vec(&ev).unwrap_or_default())
}

/// Builds the Detect request headers for one phase. Header names are static; values are cloned from
/// the incoming request when known. `x-straiker-source` prefers a per-request header, then config.
fn detect_headers(
	guard: &StraikerCoding,
	phase: Phase,
	incoming: &::http::HeaderMap,
	session: Option<&str>,
	user: Option<&str>,
	model: Option<&str>,
) -> Vec<(&'static str, String)> {
	let mut h = vec![
		("x-tool", guard.x_tool()),
		("x-straiker-phase", phase.as_str().to_string()),
	];
	if let Some(src) = source(guard, incoming) {
		h.push(("x-straiker-source", src));
	}
	if let Some(s) = session {
		h.push(("x-claude-code-session-id", s.to_string()));
	}
	if let Some(u) = user {
		h.push(("x-straiker-user", u.to_string()));
	}
	if let Some(m) = model {
		h.push(("x-straiker-model", m.to_string()));
	}
	h
}

fn hdr<'a>(h: &'a ::http::HeaderMap, name: &str) -> Option<&'a str> {
	h.get(name).and_then(|v| v.to_str().ok())
}

/// Stable per-turn session so the request guard and response guard pair into one Console record.
/// Priority: an explicit client session header; else the W3C `traceparent` trace-id (identical for
/// this turn's request and response). Returns `None` if neither is present.
fn session_id(h: &::http::HeaderMap) -> Option<String> {
	for k in [
		"x-straiker-session",
		"x-claude-code-session-id",
		"x-session-id",
	] {
		if let Some(v) = hdr(h, k)
			&& !v.is_empty()
		{
			return Some(v.to_string());
		}
	}
	if let Some(tp) = hdr(h, "traceparent") {
		let parts: Vec<&str> = tp.split('-').collect();
		if parts.len() >= 2 && parts[1].len() == 32 {
			return Some(format!("agw-{}", parts[1]));
		}
	}
	None
}

fn source(guard: &StraikerCoding, h: &::http::HeaderMap) -> Option<String> {
	hdr(h, "x-straiker-source")
		.filter(|v| !v.is_empty())
		.map(str::to_string)
		.or_else(|| guard.source.as_ref().map(|v| v.to_string()))
}

/// Identity for the turn, resolved AUTOMATICALLY from the consumer key: the API-key policy inserts
/// its `Claims` (key + metadata) into the request extensions, so the guard reads the authenticated
/// user directly instead of depending on a header that a later-running policy sets. Falls back to an
/// explicit identity header for callers that supply one themselves.
fn user_name(req: &http::Request) -> Option<String> {
	if let Some(claims) = req.extensions().get::<crate::http::apikey::Claims>()
		&& let Ok(v) = serde_json::to_value(claims)
		&& let Some(u) = v.get("user").and_then(|u| u.as_str())
		&& !u.is_empty()
	{
		return Some(u.to_string());
	}
	for k in ["x-straiker-user", "x-consumer-username"] {
		if let Some(v) = hdr(req.headers(), k)
			&& !v.is_empty()
		{
			return Some(v.to_string());
		}
	}
	None
}

/// Anthropic `/v1/messages` requests carry the model at the top level.
fn model_from_body(body: &[u8]) -> Option<String> {
	let v: serde_json::Value = serde_json::from_slice(body).ok()?;
	v.get("model").and_then(|m| m.as_str()).map(str::to_string)
}

/// Mirrors the sidecar: a response carries a tool call when its body contains `tool_use`.
fn contains_tool_use(body: &[u8]) -> bool {
	// A cheap substring scan over the raw bytes; avoids parsing (and works for streamed frames).
	body.windows(TOOL_USE.len()).any(|w| w == TOOL_USE)
}

const TOOL_USE: &[u8] = b"tool_use";

/// The response-phase envelope the backend reconstructs PreToolUse from.
fn response_envelope(resp_body: &[u8], captured: &CapturedRequest) -> Bytes {
	let sse = String::from_utf8_lossy(resp_body).into_owned();
	let request_json: serde_json::Value =
		serde_json::from_slice(&captured.body).unwrap_or(serde_json::Value::Null);
	let envelope = serde_json::json!({
		"straiker_phase": "response-sync",
		"sse": sse,
		"model": captured.model,
		"request": request_json,
	});
	// Serializing an owned `serde_json::Value` cannot fail.
	Bytes::from(serde_json::to_vec(&envelope).unwrap_or_default())
}

/// A well-formed non-streaming Anthropic `/v1/messages` message with a single text block.
fn anthropic_block_json(model: &str, text: &str) -> Vec<u8> {
	let msg = serde_json::json!({
		"id": "msg_straiker_block",
		"type": "message",
		"role": "assistant",
		"model": model,
		"content": [{"type": "text", "text": text}],
		"stop_reason": "end_turn",
		"stop_sequence": serde_json::Value::Null,
		"usage": {"input_tokens": 0, "output_tokens": 0},
	});
	serde_json::to_vec(&msg).unwrap_or_default()
}

fn anthropic_block_response(model: &str, text: &str) -> http::Response {
	let body = anthropic_block_json(model, text);
	::http::Response::builder()
		.status(::http::StatusCode::OK)
		.header(::http::header::CONTENT_TYPE, "application/json")
		.body(http::Body::from(body))
		.expect("static block response should build")
}

/// Replaces a tool-call response body in place with an Anthropic text message, fixing the framing
/// headers so the client parses the replacement rather than the (now discarded) original.
fn replace_response_with_block(resp: &mut http::Response, model: &str, text: &str) {
	let body = anthropic_block_json(model, text);
	let len = body.len();
	*resp.body_mut() = http::Body::from(body);
	let headers = resp.headers_mut();
	// The replacement is plain JSON: drop any content-encoding, and set an accurate length.
	headers.remove(::http::header::CONTENT_ENCODING);
	headers.remove(::http::header::CONTENT_LENGTH);
	if let Ok(hv) = ::http::HeaderValue::from_str(&len.to_string()) {
		headers.insert(::http::header::CONTENT_LENGTH, hv);
	}
	headers.insert(
		::http::header::CONTENT_TYPE,
		::http::HeaderValue::from_static("application/json"),
	);
}

#[cfg(test)]
mod tests {
	use ::http::{HeaderMap, HeaderName, HeaderValue};

	use super::*;

	fn hdrs(pairs: &[(&'static str, &'static str)]) -> HeaderMap {
		let mut h = HeaderMap::new();
		for (k, v) in pairs {
			h.insert(HeaderName::from_static(k), HeaderValue::from_static(v));
		}
		h
	}

	fn guard(base: Option<&'static str>) -> StraikerCoding {
		StraikerCoding {
			api_key: strng::literal!("test-key"),
			base_url: base.map(strng::new),
			source: None,
			x_tool: None,
			mode: StraikerCodingMode::Block,
			failure_mode: FailureMode::FailOpen,
			policies: vec![],
		}
	}

	#[test]
	fn is_block_matches_block_deny_and_stop() {
		let mut v = StraikerVerdict::default();
		assert!(!v.is_block());
		v.action = Some("allow".into());
		assert!(!v.is_block());
		v.action = Some("BLOCK".into());
		assert!(v.is_block());

		let deny = StraikerVerdict {
			permission_decision: Some("deny".into()),
			..Default::default()
		};
		assert!(deny.is_block());

		let stop = StraikerVerdict {
			continue_: Some(false),
			..Default::default()
		};
		assert!(stop.is_block());
	}

	#[test]
	fn verdict_deserializes_and_ignores_extra_fields() {
		let v: StraikerVerdict =
			serde_json::from_str(r#"{"action":"block","score":0.97,"unknown":"x"}"#).unwrap();
		assert!(v.is_block());
		assert_eq!(v.score, Some(0.97));
	}

	#[test]
	fn detect_url_defaults_and_trims_trailing_slash() {
		assert_eq!(
			guard(None).detect_url(),
			format!("{DEFAULT_BASE_URL}/api/v1/detect")
		);
		assert_eq!(
			guard(Some("https://tenant.example.com/")).detect_url(),
			"https://tenant.example.com/api/v1/detect"
		);
	}

	#[test]
	fn x_tool_defaults_to_kong_claude_code() {
		assert_eq!(guard(None).x_tool(), DEFAULT_X_TOOL);
		let mut g = guard(None);
		g.x_tool = Some(strng::literal!("custom-tool"));
		assert_eq!(g.x_tool(), "custom-tool");
	}

	#[test]
	fn mode_default_is_block() {
		assert_eq!(StraikerCodingMode::default(), StraikerCodingMode::Block);
	}

	#[test]
	fn session_id_priority_header_then_traceparent_then_none() {
		assert_eq!(
			session_id(&hdrs(&[("x-claude-code-session-id", "S1")])).as_deref(),
			Some("S1")
		);
		let tp = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01";
		assert_eq!(
			session_id(&hdrs(&[("traceparent", tp)])).as_deref(),
			Some("agw-0af7651916cd43dd8448eb211c80319c")
		);
		assert_eq!(session_id(&HeaderMap::new()), None);
	}

	#[test]
	fn model_extracted_from_request_body() {
		let body = br#"{"model":"claude-3-5-sonnet","messages":[]}"#;
		assert_eq!(model_from_body(body).as_deref(), Some("claude-3-5-sonnet"));
		assert_eq!(model_from_body(b"not json"), None);
	}

	#[test]
	fn contains_tool_use_detects_tool_calls() {
		assert!(contains_tool_use(
			br#"{"content":[{"type":"tool_use","name":"x"}]}"#
		));
		assert!(!contains_tool_use(
			br#"{"content":[{"type":"text","text":"hi"}]}"#
		));
	}

	#[test]
	fn detect_headers_sets_phase_tool_and_correlation() {
		let g = guard(None);
		let incoming = hdrs(&[("x-straiker-source", "coding-bot")]);
		let h = detect_headers(
			&g,
			Phase::Request,
			&incoming,
			Some("sess-1"),
			Some("alice"),
			Some("claude-3-5-sonnet"),
		);
		let get = |name: &str| h.iter().find(|(k, _)| *k == name).map(|(_, v)| v.as_str());
		assert_eq!(get("x-tool"), Some("kong-claude-code"));
		assert_eq!(get("x-straiker-phase"), Some("request"));
		assert_eq!(get("x-straiker-source"), Some("coding-bot"));
		assert_eq!(get("x-claude-code-session-id"), Some("sess-1"));
		assert_eq!(get("x-straiker-user"), Some("alice"));
		assert_eq!(get("x-straiker-model"), Some("claude-3-5-sonnet"));
	}

	#[test]
	fn detect_headers_response_phase_and_omits_unknown() {
		let g = guard(None);
		let h = detect_headers(&g, Phase::ResponseSync, &HeaderMap::new(), None, None, None);
		let get = |name: &str| h.iter().find(|(k, _)| *k == name).map(|(_, v)| v.as_str());
		assert_eq!(get("x-straiker-phase"), Some("response-sync"));
		assert_eq!(get("x-claude-code-session-id"), None);
		assert_eq!(get("x-straiker-user"), None);
		assert_eq!(get("x-straiker-source"), None);
	}

	#[tokio::test]
	async fn monitor_mode_passes_response_through_untouched() {
		// Monitor (detect) mode must NOT buffer or rewrite the response — that is what keeps a
		// streaming route streaming. Even a tool-call body (which Block mode would score and could
		// replace) must come back byte-for-byte unchanged, with no detect call attempted.
		let g = StraikerCoding {
			mode: StraikerCodingMode::Monitor,
			..guard(None)
		};
		let mut sc = g.build(crate::test_helpers::policy_client());
		let original: &[u8] = br#"{"content":[{"type":"tool_use","name":"Bash"}]}"#;
		let mut resp = ::http::Response::builder()
			.status(200)
			.body(http::Body::from(Bytes::from_static(original)))
			.unwrap();
		// Monitor returns a no-op default PolicyResponse; bind it to satisfy `#[must_use]`.
		let _passthrough = sc
			.mutate_response(&mut resp, None)
			.await
			.expect("monitor response passthrough");
		let body = std::mem::replace(resp.body_mut(), http::Body::empty());
		let out = http::read_body_with_limit(body, 1 << 20).await.unwrap();
		assert_eq!(
			&out[..],
			original,
			"monitor mode must not touch the response body"
		);
	}

	#[tokio::test]
	async fn gzipped_response_is_decoded_before_inspection() {
		// Upstream gzips the response. Inspecting the compressed bytes silently found neither the
		// tool call nor the answer, which disabled the enforcement point AND the Stop event with no
		// error logged. Decoding first must recover both.
		let plain: &[u8] =
			br#"{"content":[{"type":"text","text":"final answer"},{"type":"tool_use","name":"Bash"}]}"#;
		let mut enc = async_compression::tokio::bufread::GzipEncoder::new(BufReader::new(plain));
		let mut gz = Vec::new();
		enc.read_to_end(&mut gz).await.unwrap();
		let gz = Bytes::from(gz);

		// the bug: raw compressed bytes yield nothing
		assert_eq!(answer_text(&gz), None, "raw gzip must not parse");
		assert!(!contains_tool_use(&gz), "raw gzip must not match tool_use");

		// the fix: decoded bytes recover both signals
		let mut h = HeaderMap::new();
		h.insert(
			::http::header::CONTENT_ENCODING,
			HeaderValue::from_static("gzip"),
		);
		let out = decoded_body(&gz, &h).await;
		assert_eq!(answer_text(&out).as_deref(), Some("final answer"));
		assert!(contains_tool_use(&out));

		// an uncompressed body passes through untouched
		let plain_b = Bytes::from_static(plain);
		assert_eq!(decoded_body(&plain_b, &HeaderMap::new()).await, plain_b);
	}

	#[test]
	fn answer_text_reassembles_sse_stream() {
		// Claude Code sends `stream: true`, so a real turn's response is SSE, not a buffered message.
		// Parsing only the buffered shape returned None on every live turn, so no Stop was ever posted.
		let sse = concat!(
			"event: message_start\n",
			"data: {\"type\":\"message_start\",\"message\":{\"model\":\"claude\"}}\n\n",
			"event: content_block_delta\n",
			"data: {\"type\":\"content_block_delta\",\"delta\":{\"type\":\"text_delta\",\"text\":\"quota is \"}}\n\n",
			"event: content_block_delta\n",
			"data: {\"type\":\"content_block_delta\",\"delta\":{\"type\":\"text_delta\",\"text\":\"4242\"}}\n\n",
			"event: message_stop\n",
			"data: {\"type\":\"message_stop\"}\n\n",
		);
		assert_eq!(
			answer_text(sse.as_bytes()).as_deref(),
			Some("quota is 4242")
		);
	}

	#[test]
	fn answer_text_extracts_assistant_reply() {
		let body = br#"{"content":[{"type":"text","text":"Hello "},{"type":"text","text":"world"}]}"#;
		assert_eq!(answer_text(body).as_deref(), Some("Hello world"));
		// a tool-call response carries no text blocks -> no Stop answer
		assert_eq!(
			answer_text(br#"{"content":[{"type":"tool_use","name":"Bash"}]}"#),
			None
		);
		assert_eq!(answer_text(b"not json"), None);
	}

	#[test]
	fn stop_event_carries_answer_session_user_and_model() {
		let captured = CapturedRequest {
			body: Bytes::from_static(b"{}"),
			session: Some("sess-9".into()),
			user: Some("phimmasone@straiker.ai".into()),
			model: Some("claude-sonnet-4-5".into()),
		};
		let ev: serde_json::Value =
			serde_json::from_slice(&stop_event_json("the final answer", &captured)).unwrap();
		assert_eq!(ev["hook_event_name"], "Stop");
		assert_eq!(ev["app_response"], "the final answer");
		assert_eq!(ev["stop_reason"], "end_turn");
		assert_eq!(ev["session_id"], "sess-9");
		assert_eq!(ev["user_name"], "phimmasone@straiker.ai");
		assert_eq!(ev["model"], "claude-sonnet-4-5");
	}

	#[test]
	fn stop_headers_use_the_native_contract_tag() {
		let captured = CapturedRequest {
			body: Bytes::from_static(b"{}"),
			session: Some("s1".into()),
			user: Some("u1".into()),
			model: None,
		};
		let h = stop_headers(&guard(None), &HeaderMap::new(), &captured);
		let get = |n: &str| h.iter().find(|(k, _)| *k == n).map(|(_, v)| v.as_str());
		// Stop is a pre-formed hook event, so it must NOT use the central-parse tag.
		assert_eq!(get("x-tool"), Some(STOP_X_TOOL));
		assert_ne!(get("x-tool"), Some(DEFAULT_X_TOOL));
		assert_eq!(get("x-claude-code-session-id"), Some("s1"));
		assert_eq!(get("x-straiker-user"), Some("u1"));
		assert_eq!(get("x-straiker-model"), None);
	}

	#[test]
	fn response_envelope_wraps_body_and_request() {
		let captured = CapturedRequest {
			body: Bytes::from_static(br#"{"model":"m","messages":[]}"#),
			session: Some("s".into()),
			user: None,
			model: Some("m".into()),
		};
		let env = response_envelope(br#"{"type":"tool_use"}"#, &captured);
		let v: serde_json::Value = serde_json::from_slice(&env).unwrap();
		assert_eq!(v["straiker_phase"], "response-sync");
		assert_eq!(v["sse"], r#"{"type":"tool_use"}"#);
		assert_eq!(v["model"], "m");
		assert_eq!(v["request"]["model"], "m");
	}
}
