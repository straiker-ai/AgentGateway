#!/usr/bin/env bash
# Live QA against the deployed gateway. Offline suites prove the sidecar; this proves the deployed thing
# serves, guards, and streams — per capability.      GW=... AGW_KEY=... ./live_qa.sh
set -uo pipefail
GW="${GW:?set GW (your gateway URL)}"; AGENTIC="${AGENTIC:-$GW}"
KEY="${AGW_KEY:-$(grep '^dev@example.com=' "$(dirname "$0")/deploy/agent-keys.env" 2>/dev/null | cut -d= -f2)}"
[ -n "$KEY" ] || { echo "set AGW_KEY"; exit 1; }
pass=0; fail=0; ok(){ printf "  \033[32mPASS\033[0m %s\n" "$1"; pass=$((pass+1)); }; no(){ printf "  \033[31mFAIL\033[0m %s -> %s\n" "$1" "$2"; fail=$((fail+1)); }

echo "== auth =="
c=$(curl -m 20 -s -o /dev/null -w '%{http_code}' -X POST "$GW/v1/messages" -H 'content-type: application/json' -d '{}'); [ "$c" = "401" ] && ok "anonymous rejected (401)" || no "anonymous rejected" "$c"
c=$(curl -m 20 -s -o /dev/null -w '%{http_code}' -X POST "$GW/v1/messages" -H 'content-type: application/json' -H 'apikey: bogus' -d '{}'); [ "$c" = "401" ] && ok "invalid key rejected (401)" || no "invalid key" "$c"
B=$(curl -m 25 -s -X POST "$GW/v1/messages" -H 'content-type: application/json' -H "apikey: $KEY" -d '{}'); echo "$B" | grep -q "x-api-key header is required" && ok "valid key forwards to Anthropic" || no "forwarding" "$(echo "$B" | head -c 120)"

echo "== real Claude Code through the gateway (ExtProc seam) =="
R=$(ANTHROPIC_BASE_URL="$GW" ANTHROPIC_CUSTOM_HEADERS="apikey: $KEY" claude -p "reply with exactly: LIVE-OK" </dev/null 2>/dev/null); [ "$R" = "LIVE-OK" ] && ok "simple turn" || no "simple turn" "$R"
T=$(mktemp -d); echo "quota: 1000" > "$T/config.yaml"
R=$(cd "$T" && ANTHROPIC_BASE_URL="$GW" ANTHROPIC_CUSTOM_HEADERS="apikey: $KEY" claude -p "read config.yaml and state the quota value only" --dangerously-skip-permissions </dev/null 2>/dev/null); rm -rf "$T"
echo "$R" | grep -q "1000" && ok "tool-using turn (Read -> PreToolUse/PostToolUse)" || no "tool-using turn" "$R"

echo "== streaming preset preserves TTFT =="
TTFT=$(python3 - "$GW" "$KEY" <<'PY'
import sys,time,json,urllib.request,os
gw,key=sys.argv[1],sys.argv[2]; ak=os.environ.get("ANTHROPIC_KEY") or os.environ.get("ANTHROPIC_API_KEY") or ""
body=json.dumps({"model":"claude-sonnet-4-5-20250929","max_tokens":300,"stream":True,"messages":[{"role":"user","content":"Explain TCP congestion control in 200 words."}]}).encode()
h={"content-type":"application/json","x-api-key":ak,"anthropic-version":"2023-06-01","apikey":key}
t0=time.time(); first=None
try:
    with urllib.request.urlopen(urllib.request.Request(gw+"/streaming/v1/messages",data=body,headers=h,method="POST"),timeout=120) as x:
        for line in x:
            if line.startswith(b"data:") and b"text_delta" in line: first=time.time()-t0; break
    print(f"{first:.2f}" if first else "none")
except Exception as e: print("err:"+str(e)[:60])
PY
)
case "$TTFT" in err*|none) no "streaming TTFT" "$TTFT";; *) awk -v t="$TTFT" 'BEGIN{exit !(t<6)}' && ok "streaming route: first token in ${TTFT}s" || no "streaming TTFT (<6s)" "${TTFT}s";; esac

echo "== per-agent routes gated + forwarding =="
for a in codex cursor opencode copilot; do p="v1/chat/completions"; [ "$a" = "codex" ] && p="v1/responses"
  n=$(curl -m 20 -s -o /dev/null -w '%{http_code}' -X POST "$GW/$a/$p" -H 'content-type: application/json' -d '{}')
  w=$(curl -m 20 -s -X POST "$GW/$a/$p" -H 'content-type: application/json' -H "apikey: $KEY" -d '{}')
  if [ "$n" = "401" ] && echo "$w" | grep -qi "api key\|authentication\|invalid"; then ok "/$a gated + forwarding to OpenAI"; else no "/$a" "nokey=$n with=$(echo "$w" | head -c 60)"; fi
done

echo "== MCP through the gateway (ExtMCP seam) =="
if [ -x "$(dirname "$0")/.venv/bin/python" ]; then
  M=$("$(dirname "$0")/.venv/bin/python" "$(dirname "$0")/lab/mcp_client.py" --url "$GW/mcp" --key "$KEY" call list_tasks '{}' 2>/dev/null | grep -E "^(OK|ERROR|BLOCKED)")
  echo "$M" | grep -q "^OK" && ok "tools/call through /mcp ($(echo "$M" | head -c 40)...)" || no "mcp tools/call" "$M"
fi

echo "== agentic surface (guardrail webhook seam) =="
A=$(curl -m 60 -s -X POST "$AGENTIC/v1/chat/completions" -H "apikey: $KEY" -H 'content-type: application/json' -d '{"model":"claude-sonnet-4-5-20250929","max_tokens":20,"messages":[{"role":"user","content":"Say OK."}]}')
echo "$A" | grep -q '"choices"' && ok "agentic chat completion (pre_call + post_call)" || no "agentic chat" "$(echo "$A" | head -c 120)"

printf "\n%s  pass=%d fail=%d\n" "$([ $fail -eq 0 ] && printf '\033[32mLIVE QA GREEN\033[0m' || printf '\033[31mLIVE QA RED\033[0m')" "$pass" "$fail"
exit $([ $fail -eq 0 ] && echo 0 || echo 1)
