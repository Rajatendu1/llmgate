PY ?= python3

.PHONY: test e2e js-smoke verify-real demo bench docker-build docker-run compose-up
test:        ## run the 139-test suite
	$(PY) -m pytest -q
e2e:         ## run the end-to-end demo (boots a local gateway, ~30s)
js-smoke:    ## headless-Chrome dashboard JS gate (boots a local gateway, ~10s)
	bash e2e/js_smoke.sh
	bash e2e/run_e2e.sh
verify-real: ## live-check HTTPProvider (needs LLMGATE_REAL_BASE_URL/API_KEY/MODEL; SKIP otherwise)
	$(PY) scripts/verify_real_upstream.py
demo:        ## 1-minute failover story with simulated providers
	bash bench/demo.sh
bench:       ## reproduce the README benchmark table
	bash bench/run_bench.sh
docker-build: ## build the demo container image
	docker build -t llmgate .
docker-run:  ## run the demo container on localhost:8000
	docker run --rm -p 8000:8000 llmgate
compose-up:  ## same thing through docker compose
	docker compose up --build
