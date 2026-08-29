// Straiker DefendAI — a first-class LLM guardrail.
//
// Straiker scores a prompt (request) or completion (response) against the tenant's runtime
// guardrail policy and returns an action. This guard calls Straiker's Detect API directly
// (no sidecar): POST {base_url}/api/v1/detect/webhook with the kong-gateway event format —
// the SAME contract the Straiker Kong / LiteLLM / Portkey integrations use — so request and
// response of one turn correlate (by session) into a single Console record with the full
// agentic trace. `action == "block"` becomes the guard's configured rejection.
use agent_core::strng;
use itertools::Itertools;
use serde::Deserialize;

use crate::json;
use crate::llm::RequestType;
use crate::llm::policy::{Straiker, with_default_timeout};
use crate::llm::policy::webhook::ResponseChoice;
use crate::proxy::httpproxy::PolicyClient;
use crate::telemetry::metrics::{OutboundCallKind, OutboundCallSubtype};
use crate::types::agent::{Backend, BackendTrafficPolicy, ResourceName};

const DEFAULT_BASE_URL: &str = "https://api.prod.straiker.ai";
const WEBHOOK_FORMAT: &str = "kong-gateway";

/// Straiker's scoring response. `/api/v1/detect/webhook` with `Straiker-Debug: TRUE` returns the
/// decision fields; extra fields are ignored so the contract can evolve without breaking us.
#[derive(Debug, Clone, Default, Deserialize)]
#[serde(default)]
pub struct StraikerVerdict {
	pub action: Option<String>,
	pub score: Option<f64>,
	pub score_category: Option<String>,
	pub reason: Option<String>,
}

impl StraikerVerdict {
	/// A block decision. Straiker encodes it as `action == "block"`.
	pub fn is_block(&self) -> bool {
		matches!(self.action.as_deref(), Some(a) if a.eq_ignore_ascii_case("block"))
	}
}

fn base_url(s: &Straiker) -> String {
	let b = s
		.base_url
		.as_ref()
		.map(|u| u.as_str())
		.unwrap_or(DEFAULT_BASE_URL);
	format!("{}/api/v1/detect/webhook", b.trim_end_matches('/'))
}

fn hdr<'a>(h: &'a ::http::HeaderMap, name: &str) -> Option<&'a str> {
	h.get(name).and_then(|v| v.to_str().ok())
}

/// Stable per-turn session so pre_call (input) and post_call (output) pair into one Console record.
/// Priority: an explicit client session header; else the W3C `traceparent` trace-id, which
/// agentgateway propagates per request and is therefore identical for this turn's request guard and
/// response guard (so even a Chat Playground turn with no session header pairs correctly); else a
/// content hash (last resort — may not pair request with response).
fn session_id(h: &::http::HeaderMap, seed: &str) -> String {
	for k in ["x-straiker-session", "x-claude-code-session-id", "x-session-id"] {
		if let Some(v) = hdr(h, k)
			&& !v.is_empty()
		{
			return v.to_string();
		}
	}
	// traceparent = "00-<32-hex trace-id>-<span-id>-<flags>"; the trace-id is stable per request.
	if let Some(tp) = hdr(h, "traceparent") {
		let parts: Vec<&str> = tp.split('-').collect();
		if parts.len() >= 2 && parts[1].len() == 32 {
			return format!("agw-{}", parts[1]);
		}
	}
	use std::hash::{Hash, Hasher};
	let mut hh = std::collections::hash_map::DefaultHasher::new();
	seed.hash(&mut hh);
	format!("agw-{:016x}", hh.finish())
}

fn source(s: &Straiker, h: &::http::HeaderMap) -> String {
	hdr(h, "x-straiker-source")
		.filter(|v| !v.is_empty())
		.map(str::to_string)
		.or_else(|| s.source.as_ref().map(|v| v.to_string()))
		.unwrap_or_else(|| "agentgateway".to_string())
}

fn user_name(h: &::http::HeaderMap) -> Option<String> {
	for k in ["x-straiker-user", "x-consumer-username"] {
		if let Some(v) = hdr(h, k)
			&& !v.is_empty()
		{
			return Some(v.to_string());
		}
	}
	None
}

fn ai_context(h: &::http::HeaderMap) -> serde_json::Value {
	// Only forward what we actually know. The guard sees normalized messages, not the upstream
	// route, so asserting a specific wire format here (e.g. "openai") would be wrong for Anthropic/
	// Bedrock/Gemini routes; let the backend infer it. `model` is forwarded when the route sets it.
	serde_json::json!({ "genai_category": "chat", "model": hdr(h, "x-straiker-model") })
}

fn last_user_text(messages: &[serde_json::Value]) -> String {
	messages
		.iter()
		.rev()
		.find(|m| m.get("role").and_then(|r| r.as_str()) == Some("user"))
		.and_then(|m| m.get("content").and_then(|c| c.as_str()))
		.unwrap_or("")
		.to_string()
}

async fn post(
	client: &PolicyClient,
	s: &Straiker,
	payload: serde_json::Value,
) -> anyhow::Result<StraikerVerdict> {
	let mut pols = vec![BackendTrafficPolicy::BackendTLS(
		crate::http::backendtls::SYSTEM_TRUST.clone(),
	)];
	pols.extend(s.policies.iter().cloned());
	let req = ::http::Request::builder()
		.uri(base_url(s))
		.method(::http::Method::POST)
		.header(::http::header::CONTENT_TYPE, "application/json")
		.header(::http::header::AUTHORIZATION, format!("Bearer {}", s.api_key))
		.header("X-Straiker-Webhook-Format", WEBHOOK_FORMAT)
		.header("Straiker-Debug", "TRUE")
		.body(crate::http::Body::from(serde_json::to_vec(&payload)?))?;
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

/// INPUT guardrail (pre_call): score the incoming prompt before it reaches the model.
pub(super) async fn send_request(
	req: &mut dyn RequestType,
	client: &PolicyClient,
	s: &Straiker,
	http_headers: &::http::HeaderMap,
) -> anyhow::Result<StraikerVerdict> {
	let messages: Vec<serde_json::Value> = req
		.get_messages()
		.into_iter()
		.map(|m| serde_json::json!({"role": m.role.as_str(), "content": m.content.as_str()}))
		.collect_vec();
	let text = last_user_text(&messages);
	let sess = session_id(http_headers, &text);
	let payload = serde_json::json!({
		"eventType": "pre_call",
		"request": {"body": {"messages": messages}, "text": text},
		"userInfo": {"id": user_name(http_headers), "role": "public"},
		"metadata": {
			"session_id": sess,
			"source": source(s, http_headers),
			"app_name": source(s, http_headers),
			"user_name": user_name(http_headers),
		},
		"aiContext": ai_context(http_headers),
	});
	post(client, s, payload).await
}

/// OUTPUT guardrail (post_call): score the model's answer, paired to the same session.
pub(super) async fn send_response(
	resp: &mut dyn crate::llm::ResponseType,
	client: &PolicyClient,
	s: &Straiker,
	http_headers: &::http::HeaderMap,
) -> anyhow::Result<StraikerVerdict> {
	let choices: Vec<ResponseChoice> = resp.to_webhook_choices();
	let answer = choices
		.first()
		.map(|c| c.message.content.as_str().to_string())
		.unwrap_or_default();
	let choice_json = choices
		.iter()
		.map(|c| serde_json::json!({"message": {"role": c.message.role.as_str(), "content": c.message.content.as_str()}}))
		.collect_vec();
	let sess = session_id(http_headers, &answer);
	// A response guard only sees the completion (choices), not the originating request, so the
	// request block is empty here; the backend pairs this post_call with its pre_call by session.
	let payload = serde_json::json!({
		"eventType": "post_call",
		"request": {"body": {"messages": []}, "text": ""},
		"response": {"stream": false, "body": {"choices": choice_json}, "text": answer},
		"userInfo": {"id": user_name(http_headers), "role": "public"},
		"metadata": {
			"session_id": sess,
			"source": source(s, http_headers),
			"app_name": source(s, http_headers),
			"user_name": user_name(http_headers),
		},
		"aiContext": ai_context(http_headers),
	});
	post(client, s, payload).await
}

#[cfg(test)]
mod tests {
	use ::http::{HeaderMap, HeaderName, HeaderValue};
	use agent_core::strng;

	use super::*;
	use crate::llm::policy::{FailureMode, Straiker};

	fn hdrs(pairs: &[(&'static str, &'static str)]) -> HeaderMap {
		let mut h = HeaderMap::new();
		for (k, v) in pairs {
			h.insert(HeaderName::from_static(k), HeaderValue::from_static(v));
		}
		h
	}

	fn guard(base: Option<&'static str>) -> Straiker {
		Straiker {
			api_key: strng::literal!("test-key"),
			base_url: base.map(strng::new),
			source: None,
			failure_mode: FailureMode::FailOpen,
			policies: vec![],
		}
	}

	#[test]
	fn is_block_matches_only_block() {
		let mut v = StraikerVerdict::default();
		assert!(!v.is_block());
		v.action = Some("allow".into());
		assert!(!v.is_block());
		v.action = Some("BLOCK".into());
		assert!(v.is_block());
	}

	#[test]
	fn verdict_deserializes_and_ignores_extra_fields() {
		let v: StraikerVerdict =
			serde_json::from_str(r#"{"action":"block","score":0.97,"unknown":"x"}"#).unwrap();
		assert!(v.is_block());
		assert_eq!(v.score, Some(0.97));
	}

	#[test]
	fn base_url_defaults_and_trims_trailing_slash() {
		assert_eq!(base_url(&guard(None)), format!("{DEFAULT_BASE_URL}/api/v1/detect/webhook"));
		assert_eq!(
			base_url(&guard(Some("https://tenant.example.com/"))),
			"https://tenant.example.com/api/v1/detect/webhook"
		);
	}

	#[test]
	fn session_id_priority_header_then_traceparent_then_hash() {
		// explicit session header wins
		assert_eq!(session_id(&hdrs(&[("x-straiker-session", "S1")]), "seed"), "S1");
		// else the traceparent trace-id (stable across a turn's request + response guards)
		let tp = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01";
		assert_eq!(
			session_id(&hdrs(&[("traceparent", tp)]), "seed"),
			"agw-0af7651916cd43dd8448eb211c80319c"
		);
		// else a stable content hash; same seed -> same id
		assert_eq!(session_id(&HeaderMap::new(), "seed"), session_id(&HeaderMap::new(), "seed"));
	}

	#[test]
	fn last_user_text_picks_the_latest_user_message() {
		let msgs = vec![
			serde_json::json!({"role": "system", "content": "sys"}),
			serde_json::json!({"role": "user", "content": "first"}),
			serde_json::json!({"role": "assistant", "content": "reply"}),
			serde_json::json!({"role": "user", "content": "second"}),
		];
		assert_eq!(last_user_text(&msgs), "second");
		assert_eq!(last_user_text(&[]), "");
	}
}
