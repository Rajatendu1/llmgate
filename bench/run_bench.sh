#!/usr/bin/env bash
# Reproduces all numbers in README. Needs: pip install -e . (or fastapi uvicorn httpx)
set -e
PY=${PY:-python}
run() { # label, env
  env DEMO_RATE=1000000 $2 $PY -m uvicorn llmgate.demo_app:app --port 8000 --log-level warning & PID=$!
  sleep 1.5; echo "== $1"; $PY bench/bench.py "${@:3}"; kill $PID; wait $PID 2>/dev/null || true
}
run "healthy, non-stream" "SIM_A_FAIL=0" --n 4000 --c 32
run "healthy, stream" "SIM_A_FAIL=0" --n 2000 --c 32 --stream
run "primary 30% errors (failover)" "SIM_A_FAIL=0.3" --n 4000 --c 32
run "primary 100% errors (breaker open)" "SIM_A_FAIL=1.0" --n 4000 --c 32
