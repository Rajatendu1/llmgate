#!/usr/bin/env bash
# llmgate end-to-end demo.
#
# Boots the gateway against its two simulated providers and verifies the whole
# request story over real HTTP on localhost: authentication, routing, SSE
# streaming, request and token rate limits, failover with the circuit breaker,
# usage and cost metering and the Prometheus scrape. TAP output; the transcript
# is mirrored to e2e/E2E_TRANSCRIPT.txt.
#
# No real provider and no paid API calls are involved: the upstreams are the
# project's own MockProvider fakes.
#
# Usage:  bash e2e/run_e2e.sh        (from anywhere; finds the repo itself)
#         PORT=8072 bash e2e/run_e2e.sh
set -u
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"
PY=${PY:-python3}
PORT=${PORT:-8071}
BASE="http://127.0.0.1:$PORT"
TMPD=$(mktemp -d)
TRANSCRIPT="$REPO_ROOT/e2e/E2E_TRANSCRIPT.txt"

n=0; fails=0
ok()  { n=$((n+1)); echo "ok $n - $1"; }
bad() { n=$((n+1)); fails=$((fails+1)); echo "not ok $n - $1"; [ -n "${2:-}" ] && echo "# $2"; }
check() { # description expected actual
  if [ "$2" = "$3" ]; then ok "$1"; else bad "$1" "expected [$2], got [$3]"; fi
}

PID=""
start_gateway() { # start_gateway KEY=VAL ...
  env "$@" "$PY" -m uvicorn llmgate.demo_app:app --port "$PORT" --log-level warning >"$TMPD/server.log" 2>&1 &
  PID=$!
  for _ in $(seq 1 60); do
    curl -s -o /dev/null "$BASE/healthz" && return 0
    sleep 0.25
  done
  echo "# gateway failed to start; server log:"; cat "$TMPD/server.log"
  return 1
}
stop_gateway() { if [ -n "$PID" ]; then kill "$PID" 2>/dev/null; wait "$PID" 2>/dev/null; PID=""; fi; }
cleanup() { stop_gateway; rm -rf "$TMPD"; }
trap cleanup EXIT

jget() { "$PY" -c "import sys,json; d=json.load(open('$1')); print($2)" 2>/dev/null; }

req() { # req METHOD PATH [KEY] [JSONBODY]; sets CODE and BODY_FILE, headers in $TMPD/h
  local m=$1 p=$2 k=${3:-} b=${4:-}
  local args=(-s -o "$TMPD/body" -w '%{http_code}' -D "$TMPD/h" -X "$m")
  [ -n "$k" ] && args+=(-H "Authorization: Bearer $k")
  [ -n "$b" ] && args+=(-H 'content-type: application/json' -d "$b")
  CODE=$(curl "${args[@]}" "$BASE$p")
  BODY_FILE="$TMPD/body"
}
hdr() { grep -i "^$1:" "$TMPD/h" | tr -d '\r' | awk '{print $2}'; }
yn() { if "$@" >/dev/null 2>&1; then echo yes; else echo no; fi; }

main() {
  echo "# llmgate end-to-end demo - $(date -u '+%Y-%m-%d %H:%M:%S UTC')"
  echo "# $($PY --version 2>&1), uvicorn $($PY -c 'import uvicorn; print(uvicorn.__version__)' 2>/dev/null), port $PORT"
  echo "# upstreams: simulated MockProvider fakes only; no real provider, no paid API calls"
  echo
  echo "# --- phase A: healthy gateway (sim-a primary, sim-b backup) ---"
  start_gateway SIM_A_FAIL=0 DEMO_RATE=50 || return 1

  req GET /healthz
  check "healthz answers 200" 200 "$CODE"
  check "healthz reports ok" True "$(jget "$BODY_FILE" "d['ok']")"

  req POST /v1/chat/completions "" '{"model":"gpt-sim","messages":[{"role":"user","content":"hi"}]}'
  check "missing key gets 401" 401 "$CODE"
  req POST /v1/chat/completions wrong-key '{"model":"gpt-sim","messages":[{"role":"user","content":"hi"}]}'
  check "wrong key gets 401" 401 "$CODE"

  req GET /v1/models demo-key
  check "models list 200 with demo key" 200 "$CODE"
  check "models list contains gpt-sim" True "$(jget "$BODY_FILE" "'gpt-sim' in [m['id'] for m in d['data']]")"

  req POST /v1/chat/completions demo-key '{"model":"gpt-sim","messages":[{"role":"user","content":"hi"}]}'
  check "chat completion 200" 200 "$CODE"
  check "served by primary sim-a" sim-a "$(hdr x-gateway-provider)"
  check "usage reported on the response" True "$(jget "$BODY_FILE" "d.get('usage',{}).get('total_tokens',0) > 0")"
  check "completion carries measured Server-Timing stages" yes "$(yn grep -qi '^server-timing: .*auth;dur=.*admission;dur=.*routing;dur=.*provider;dur=.*metering;dur=' "$TMPD/h")"

  req POST /v1/chat/completions demo-key '{"model":"gpt-sim","stream":true,"messages":[{"role":"user","content":"hi"}]}'
  check "streaming completion 200" 200 "$CODE"
  check "stream carries SSE data frames" yes "$(yn grep -q '^data: {' "$BODY_FILE")"
  check "stream terminates with [DONE]" yes "$(yn grep -q 'data: \[DONE\]' "$BODY_FILE")"

  req POST /v1/chat/completions demo-key '{"model":"nope","messages":[{"role":"user","content":"hi"}]}'
  check "unknown model gets 404" 404 "$CODE"
  req POST /v1/chat/completions demo-key '{"model":"gpt-sim"}'
  check "body without messages gets 400" 400 "$CODE"

  req GET /admin/usage demo-key
  check "usage rejects a non-admin key" 401 "$CODE"
  req GET /admin/usage admin-key
  check "usage answers the admin key" 200 "$CODE"
  # the 400/404 responses above are rejected before metering, so exactly the
  # non-stream and the streaming completion are on the meter for the demo key
  check "usage meters both completed calls" True "$(jget "$BODY_FILE" "d['keys'].get('demo',{}).get('requests',0) == 2 and d['keys']['demo'].get('status_ok',0) == 2")"

  req GET /metrics demo-key
  check "metrics rejects a non-admin key" 401 "$CODE"
  req GET /metrics admin-key
  check "metrics answers the admin key" 200 "$CODE"
  check "metrics exposes request counters" yes "$(yn grep -q '^llmgate_requests_total{' "$BODY_FILE")"
  req GET /demo/stats
  check "public demo stats answer 200" 200 "$CODE"
  check "demo stats show the metered traffic" True "$(jget "$BODY_FILE" "d['ok'] and d['totals']['requests'] >= 2")"
  stop_gateway

  echo
  echo "# --- phase B: request rate limit (2 req/s, burst 4) ---"
  start_gateway SIM_A_FAIL=0 DEMO_RATE=2 || return 1
  STATUSES=""; RETRY=""
  for _ in $(seq 1 8); do
    req POST /v1/chat/completions demo-key '{"model":"gpt-sim","messages":[{"role":"user","content":"hi"}]}'
    STATUSES="$STATUSES $CODE"
    if [ "$CODE" = 429 ] && [ -z "$RETRY" ]; then RETRY=$(hdr retry-after); fi
  done
  echo "# statuses over 8 rapid requests:$STATUSES"
  C200=$(echo $STATUSES | tr ' ' '\n' | grep -c '^200$')
  C429=$(echo $STATUSES | tr ' ' '\n' | grep -c '^429$')
  check "burst admits the first requests" yes "$([ "$C200" -ge 1 ] && echo yes || echo no)"
  check "excess requests get 429" yes "$([ "$C429" -ge 1 ] && echo yes || echo no)"
  check "429 carries Retry-After" yes "$([ -n "$RETRY" ] && echo yes || echo no)"
  stop_gateway

  echo
  echo "# --- phase C: primary down - failover and circuit breaker ---"
  start_gateway SIM_A_FAIL=1 DEMO_RATE=50 || return 1
  STATUSES=""; PROVS=""
  for _ in 1 2 3; do
    req POST /v1/chat/completions demo-key '{"model":"gpt-sim","messages":[{"role":"user","content":"hi"}]}'
    STATUSES="$STATUSES $CODE"
    PROVS="$PROVS $(hdr x-gateway-provider)"
  done
  check "clients still get 200 with the primary down" "200 200 200" "$(echo $STATUSES)"
  check "backup sim-b serves all of them" "sim-b sim-b sim-b" "$(echo $PROVS)"
  req GET /healthz
  check "breaker for sim-a is open" open "$(jget "$BODY_FILE" "d['providers']['sim-a']")"
  check "breaker for sim-b stays closed" closed "$(jget "$BODY_FILE" "d['providers']['sim-b']")"
  req GET /metrics admin-key
  check "breaker gauge shows sim-a open" yes "$(yn grep -q 'llmgate_provider_breaker_state{provider="sim-a"} 2' "$BODY_FILE")"
  req GET /admin/usage admin-key
  check "usage shows the backup serving" True "$(jget "$BODY_FILE" "d['providers'].get('sim-b',{}).get('status_ok',0) >= 3")"
  stop_gateway

  echo
  echo "# --- phase D: token rate limit (5 tokens/s, burst 10) ---"
  start_gateway SIM_A_FAIL=0 DEMO_RATE=50 DEMO_TOKEN_RATE=5 || return 1
  BIGPROMPT=$("$PY" -c "print(' '.join(['word']*30))")
  req POST /v1/chat/completions demo-key "{\"model\":\"gpt-sim\",\"messages\":[{\"role\":\"user\",\"content\":\"$BIGPROMPT\"}]}"
  check "30-token prompt exceeds the 10-token bucket" 429 "$CODE"
  check "token 429 explains the estimate" yes "$(yn grep -q 'token rate limit exceeded' "$BODY_FILE")"
  check "token 429 carries Retry-After" yes "$([ -n "$(hdr retry-after)" ] && echo yes || echo no)"
  req POST /v1/chat/completions demo-key '{"model":"gpt-sim","messages":[{"role":"user","content":"hi"}]}'
  check "small prompt still passes" 200 "$CODE"
  req POST /v1/chat/completions demo-key '{"model":"gpt-sim","max_tokens":20,"messages":[{"role":"user","content":"hi"}]}'
  check "max_tokens counts toward the admission estimate" 429 "$CODE"
  req POST /v1/chat/completions demo-key '{"model":"gpt-sim","max_tokens":2,"messages":[{"role":"user","content":"hi"}]}'
  check "a modest max_tokens still passes" 200 "$CODE"
  stop_gateway

  echo
  echo "# --- phase E: cost metering (simulated list prices) ---"
  start_gateway SIM_A_FAIL=0 DEMO_RATE=50 || return 1
  req POST /v1/chat/completions demo-key '{"model":"gpt-sim","messages":[{"role":"user","content":"hello there world"}]}'
  check "priced chat completion 200" 200 "$CODE"
  req GET /admin/usage admin-key
  check "usage carries an estimated cost" True "$(jget "$BODY_FILE" "d['keys'].get('demo',{}).get('cost_usd',0) > 0")"
  # 3 prompt + 5 completion tokens at sim-a's 0.40/1.20 USD per million
  check "cost matches the price table" True "$(jget "$BODY_FILE" "abs(d['keys']['demo']['cost_usd'] - (3*0.40 + 5*1.20)/1e6) < 1e-12")"
  req GET /metrics admin-key
  check "metrics exposes the cost counter" yes "$(yn grep -q '^llmgate_cost_usd_total{key="demo"} ' "$BODY_FILE")"
  start_gateway SIM_A_FAIL=0 DEMO_RATE=50 || return 1
  req POST /demo/try
  check "visitor try served" True "$(jget "$BODY_FILE" "d.get('ok') is True")"
  check "visitor try reports the actual provider" True "$(jget "$BODY_FILE" "d.get('provider') in ('sim-a','sim-b')")"
  check "visitor try exposes no key material" yes "$(grep -qE 'demo-key|invalid-sample-simulated-bearer' "$BODY_FILE" && echo no || echo yes)"
  req GET /demo/events
  check "public request log answers 200" 200 "$CODE"
  check "request log recorded the visitor try" True "$(jget "$BODY_FILE" "d['ok'] and any(e['key']=='visitor' and e['status']=='ok' for e in d['events'])")"
  check "request log labels the serving mode" True "$(jget "$BODY_FILE" "d['events'][0]['mode'] == 'simulated'")"
  check "request log reports real latency and attempts" True "$(jget "$BODY_FILE" "d['events'][0]['latency_ms'] > 0 and d['events'][0]['attempts'] >= 1")"
  check "request log carries no prompt content or key material" yes "$(grep -qE 'Say hello|demo-key|invalid-sample-simulated-bearer|invalid-sample-real-bearer' "$BODY_FILE" && echo no || echo yes)"
  req POST '/demo/try?model=real'
  check "real-model try degrades cleanly with no real provider" 404 "$CODE"
  check "real-model try error carries no key material" yes "$(grep -qE 'invalid-sample-real-bearer|demo-key' "$BODY_FILE" && echo no || echo yes)"
  req POST '/demo/try?model=real-backup'
  check "backup-model try degrades cleanly with no backup route" 404 "$CODE"
  check "backup-model try error carries no key material" yes "$(grep -qE 'invalid-sample-real-bearer|demo-key' "$BODY_FILE" && echo no || echo yes)"
  req POST /demo/try '' '{"prompt":"Reply with exactly: ok"}'
  check "visitor try accepts a custom prompt on the simulated route" True "$(jget "$BODY_FILE" "d.get('ok') is True and d.get('mode') == 'simulated'")"
  req POST /demo/keys
  VGKEY=$(jget "$BODY_FILE" "d.get('key') or ''")
  check "self-serve key mints with the vg_ prefix" yes "$(printf '%s' "$VGKEY" | grep -q '^vg_' && echo yes || echo no)"
  req POST /v1/chat/completions "$VGKEY" '{"model":"gpt-sim","messages":[{"role":"user","content":"hi"}]}'
  check "minted key serves the simulated route" 200 "$CODE"
  req POST /v1/chat/completions vg_unknown-key '{"model":"gpt-sim","messages":[{"role":"user","content":"hi"}]}'
  check "unknown minted-shape key is rejected" 401 "$CODE"
  curl -s -o "$TMPD/body" -w '%{http_code}' -D "$TMPD/h" -X POST -H "Authorization: Bearer $VGKEY" -H 'content-type: application/json' -H 'X-Simulate-Failure: 500' -d '{"model":"gpt-sim","messages":[{"role":"user","content":"hi"}]}' "$BASE/v1/chat/completions" > "$TMPD/code"
  check "injected 500 fails over to the backup provider" sim-b "$(hdr x-gateway-provider)"
  check "fault header rejected for a static key" 400 "$(curl -s -o /dev/null -w '%{http_code}' -X POST -H 'Authorization: Bearer demo-key' -H 'content-type: application/json' -H 'X-Simulate-Failure: 500' -d '{"model":"gpt-sim","messages":[{"role":"user","content":"hi"}]}' "$BASE/v1/chat/completions")"
  req GET /demo/usage "$VGKEY"
  check "own-key usage reads back the minted key's traffic" True "$(jget "$BODY_FILE" "d.get('ok') is True and d['usage'].get('requests', 0) >= 1")"
  stop_gateway

  echo
  echo "1..$n"
  if [ "$fails" -eq 0 ]; then
    echo "# E2E PASS: all $n checks"
    return 0
  fi
  echo "# E2E FAIL: $fails of $n checks failed"
  return 1
}

main 2>&1 | tee "$TRANSCRIPT"
exit "${PIPESTATUS[0]}"
