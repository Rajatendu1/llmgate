#!/usr/bin/env bash
# 60-second story: healthy -> primary dies -> breaker opens -> traffic moves to backup -> usage shows it.
# Needs the package installed. Uses only simulated providers.
set -e
PY=${PY:-python}
SIM_A_FAIL=0 $PY -m uvicorn llmgate.demo_app:app --port 8011 --log-level warning & PID=$!
trap 'kill $PID 2>/dev/null' EXIT; sleep 1.5
call() { curl -s -D- -o /dev/null -H 'Authorization: Bearer demo-key' -H 'content-type: application/json' \
  -d '{"model":"gpt-sim","messages":[{"role":"user","content":"hi"}]}' localhost:8011/v1/chat/completions | grep -i -E "^HTTP|x-gateway-provider" | tr -d '\r' | paste -sd' '; }
echo "1) healthy:"; call; call
kill $PID; wait $PID 2>/dev/null || true
SIM_A_FAIL=1 $PY -m uvicorn llmgate.demo_app:app --port 8011 --log-level warning & PID=$!; sleep 1.5
echo "2) primary now failing 100% - client still gets 200 via backup:"; call; call; call; call
echo "3) breaker state:"; curl -s localhost:8011/healthz; echo
echo "4) usage (admin key):"; curl -s -H 'Authorization: Bearer admin-key' localhost:8011/admin/usage; echo
echo "5) metrics scrape (admin key):"; curl -s -H 'Authorization: Bearer admin-key' localhost:8011/metrics | grep -v '^#'
