# Deploying and demoing llmgate

Scope: this packages the gateway for **local and demo use**. Nothing here
publishes an image to a registry, terminates TLS, or talks to a paid provider.
The container runs the same simulated providers as `llmgate.demo_app:app`
unless you mount a real config.

## Three ways to run it

### 1. Local Python (development)

```bash
pip install -e '.[dev]'
pytest
uvicorn llmgate.demo_app:app --port 8000
```

### 2. Docker

```bash
docker build -t llmgate .
docker run --rm -p 8000:8000 llmgate
```

The image is `python:3.11-slim` with the package installed, running as a
non-root user, with a `/healthz` HEALTHCHECK. The default command is the demo
app (two simulated providers, model `gpt-sim`, keys `demo-key` / `admin-key`).

Real upstreams: mount a config built from `examples.config.json` and override
the command with the config factory. Upstream secrets and client keys come
from environment variables only - never from the image or the config file:

```bash
docker run --rm -p 8000:8000 \
  -e LLMGATE_CONFIG=/cfg/prod.json -e LLMGATE_KEYS -e PRIMARY_API_KEY \
  -v "$PWD/my.json:/cfg/prod.json:ro" \
  llmgate python -m uvicorn --factory llmgate.config:app --host 0.0.0.0 --port 8000
```

### 3. docker compose

```bash
docker compose up --build   # gateway on http://localhost:8000
```

`docker-compose.yml` is a single service in demo mode. Set `SIM_A_FAIL=1` in
its environment to watch traffic fail over to the backup provider.

## End-to-end demo

```bash
bash e2e/run_e2e.sh     # or: make e2e
```

One command, about 30 seconds, no setup beyond `pip install -e .`. It boots
the gateway on localhost in five phases and runs end-to-end checks over real
HTTP, TAP-style:

- **A. Healthy:** auth rejections (401), model listing, a non-stream and a
  streaming completion, 404/400 validation, admin gating, the Prometheus
  scrape and the public demo-stats endpoint behind the landing dashboard.
- **B. Request rate limit:** a 2 req/s key with burst 4 admits the burst,
  then returns 429 with `Retry-After`.
- **C. Failover:** with the primary failing 100%, clients still get 200 from
  the backup; the breaker for the dead provider opens (`/healthz` and the
  `llmgate_provider_breaker_state` gauge show it) and usage shows the backup
  serving.
- **D. Token rate limit:** an over-budget prompt gets a 429 naming the
  estimated cost; a small prompt still passes. A `max_tokens` cap counts toward
  the admission estimate: 20 over a 10-token bucket is rejected, 2 is not.
- **E. Cost metering:** after a priced call, `/admin/usage` carries a
  `cost_usd` estimate matching the demo's simulated price table, and
  `/metrics` exposes the `llmgate_cost_usd_total` counter.

The run finishes on the dashboard surface: the visitor Try (served, its route
reported from the gateway's routing headers, no key material in the response),
the public request log (records the Try, labels its mode, carries latency and
attempt counts, and no prompt content or key material), and the real-model and
backup-model Try paths failing closed with a clean 404 when no real route is
configured.

The run is mirrored to `e2e/E2E_TRANSCRIPT.txt`. The committed transcript records
a historical local run. The active workflow in `.github/workflows/` runs pytest,
this HTTP suite and the local JavaScript smoke test. `docs/ci.example.yml` is
only a historical example. Run the same gates locally with:

```bash
python -m pytest -q
bash e2e/run_e2e.sh
bash e2e/js_smoke.sh
bash e2e/js_smoke.sh --simulated-only
```

## GitHub-backed deployment checks

Use a pull request and the required source check before normal merge to main.
Do not bypass branch protection. Vercel builds from the repository; preview
and production environments can have different provider and store settings.
Check the exact source head, staged production runtime and configuration before
promotion. After promotion, check the live workflows end to end, including the
changed behavior and relevant failures, and inspect desktop/mobile layouts.
Keep the previous deployment available for rollback.

A ready build or `/healthz` response alone does not establish token settlement,
upstream model availability or pricing. For a settlement change, use a bounded
simulated-only key to check two large token reservations after small actual
responses. Confirm the second admission occurs too soon for ordinary refill
to explain it. If the result is inconclusive, say so. Do not call real providers
or spend money just to turn an untested path into a passing check.

## Environment knobs

| variable | default | effect |
|---|---|---|
| `SIM_A_FAIL` | `0.0` | primary sim-a failure probability (`1` = down) |
| `SIM_TOKEN_DELAY` | `0.002` | seconds between streamed tokens |
| `DEMO_RATE` | `50` | demo-key requests/sec (burst = 2x) |
| `DEMO_TOKEN_RATE` | unset | demo-key tokens/sec budget (burst = 2x) |
| `GROQ_API_KEY` | unset | public-demo entry point only (`index.py` + `deploy.config.json`): activates the real `groq` (gpt-oss-120b) and `groq-backup` (gpt-oss-20b) providers behind the `gpt-real` / `gpt-real-backup` aliases and the landing page's two real Try buttons; unset = real nodes shown as not active, simulated traffic only. The demo app itself is always simulated |
| `LLMGATE_KEYS` | - | JSON client keys for config-factory mode |
| `LLMGATE_CONFIG` | - | path to a JSON config for config-factory mode |

## Scoped bearer tokens (optional)

A config may add a `token_auth` section enabling a second, additive credential
path: an EMBEDDED issuer (vendored Actas 0.14.1, RFC 8693 token exchange; see
`vendor/ACTAS.md` for the pinned snapshot and its SHA256) issues JWTs whose
scopes map - server-side, in this config - to a subset of the public model
aliases. Static keys are unchanged and stay primary.

```json
"token_auth": {
  "enabled": true,
  "issuer": "llmgate-embedded",
  "audience": "llmgate",
  "scope_models": {"llm:complete": ["gpt-sim"], "llm:expensive-models": ["gpt-sim-pro"]},
  "required_scope": "llm:complete",
  "rate": 20.0, "burst": 40.0,
  "demo": {"enabled": true, "cheap_model": "gpt-sim", "expensive_model": "gpt-sim-pro"}
}
```

Enforcement: RS256 signature (algorithm pinned inside actas), issuer/audience
(pinned here), expiry and `required_scope` are verified per request; the model
allowlist is enforced per request with 403 `insufficient_scope`; budgets come
from this config, never from claims; a `cnf`-bound token is rejected outright
(no proof channel exists). Boot-time isolation guard: `scope_models` and the
demo aliases may not reference any model served by a real-mode provider - a
config that tries fails to boot with a clear error rather than leaking
real-upstream spend to a scoped token. `demo.enabled` mounts
`POST /demo/delegate`, a self-contained end-to-end exchange flow (root token
exchanged down to a parent agent and a narrower sub-agent, consequences
exercised against this same app, hash-chained audit verified) - simulated
aliases only. The public demo config (`deploy.config.json`) enables both the
section and the demo against `gpt-sim` / `gpt-sim-pro`.

## Honest limits

- On 11 November 2026, [hosted CI](https://github.com/Rajatendu1/llmgate/actions/runs/42977091731/job/130378576049?pr=8) built the default Python 3.11 simulated image and checked installed gateway/Actas imports from site-packages, actual non-root execution, loopback HTTP health, authentication, completion, SSE, unknown-model rejection, admin isolation and usage in healthy and forced-primary-failure modes. This verifies the default demo image only. Docker Compose and mounted real-provider configurations remain unverified; no image was pushed or deployed.
- Usage metering and rate limits are per process by default and can use a shared Redis store when configured. Rate limiting fails closed when the store is unreachable. Circuit-breaker and latency state remain per process. Usage telemetry is not billing-grade.
- No TLS inside the container; put it behind a proxy that terminates TLS.
- Demo keys are hard-coded in `llmgate/demo_app.py`; real deployments use
  `LLMGATE_KEYS`.
