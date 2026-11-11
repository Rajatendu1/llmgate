#!/usr/bin/env bash
# Dashboard JS smoke gate: boot the serverless demo locally with a dummy
# upstream key (so all four providers, including both real-mode ones, render),
# then drive headless Chrome against the page and fail on uncaught JS errors.
# Localhost only; the dummy key never leaves the machine.
set -u
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"
PORT=8078

export GROQ_API_KEY="dummy-local-smoke-key"
export LLMGATE_KEYS='{"demo-key": {"name": "demo", "rate": 50, "burst": 100}, "admin-key": {"name": "admin", "admin": true}}'

python3 -m uvicorn index:app --port $PORT > /tmp/js-smoke-server.log 2>&1 &
SERVER_PID=$!
cleanup() { kill $SERVER_PID 2>/dev/null; }
trap cleanup EXIT

for i in $(seq 1 40); do
  curl -sf "http://127.0.0.1:$PORT/healthz" > /dev/null 2>&1 && break
  sleep 0.25
done

node scripts/js_smoke.mjs "http://127.0.0.1:$PORT/"
