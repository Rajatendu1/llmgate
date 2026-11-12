# llmgate
An OpenAI-compatible gateway in Python and FastAPI, with routing, failover, streaming, per-key limits and usage telemetry.

Built by Instinct, an AI assistant, under Rajatendu Dey's direction. This is a tested prototype, not a production-certified service.

## Run locally
```sh
pip install -e '.[dev]'
pytest -q
uvicorn llmgate.demo_app:app --port 8000
```

Or install the CLI from GitHub and serve your own config:

```sh
pip install 'git+https://github.com/Rajatendu1/llmgate.git'
export LLMGATE_KEYS='{"my-key": {"name": "me", "rate": 10, "burst": 20}}'
llmgate serve --config examples.config.json --port 8000
```

The JSON config lists providers (see examples.config.json). A real upstream
route activates only when its `api_key_env` credential is set in the
environment; otherwise that route is skipped with a logged warning and mock
providers, which need no credentials, keep serving. See docs/DEPLOYMENT.md for
the full configuration reference.

The local demo uses simulated providers. See examples.config.json and docs/DEPLOYMENT.md for configuration. Real upstreams require your own credentials; do not commit secrets. Ambiguous-failure failover is off by default; enabling it can duplicate generation or charges.

## Verification
The November 11, 2026 Redis-settlement maintenance candidate passed 274 pytest
cases, 61 local end-to-end HTTP checks, and JavaScript smoke checks in both
simulated-only and real-route-configured modes. The latter uses a local dummy
credential and does not call a real provider. Redis limiter tests execute the
actual Lua 5.1 scripts rather than reimplement their arithmetic.

These are dated local results, not a claim that every future commit passes.
See [GitHub Actions](https://github.com/Rajatendu1/llmgate/actions) for hosted
results at a particular head. The benchmarks use simulated providers and are
not capacity claims. Optional real-provider tests skip without credentials.

## How llmgate uses Actas

llmgate embeds the recovered Actas 0.14.1 Python runtime as a pinned, in-process
dependency. It does not call the standalone Actas website. The optional scoped
token path uses Actas to sign and verify delegation tokens; gateway configuration
maps their scopes to allowed model aliases and sets budgets. Static API keys
remain separate.

`POST /demo/delegate` issues a root token, delegates to a parent agent and then
a narrower sub-agent, and tests the resulting permissions against this same
gateway. The sub-agent can use the cheap simulated alias but cannot use the
broader simulated alias or widen its scope. This shows why an agent should get
only the model permissions it needs instead of a shared broad key.

The standalone Actas app, workbench and administration routes are not mounted.
This is one embedded trust domain, not remote production authorization. Scoped
token routes are isolated from real providers; sender-bound tokens are refused
because the gateway does not verify their proofs. See [vendor/ACTAS.md](vendor/ACTAS.md)
for exact runtime provenance, byte hashes and limits, including the missing
original Actas tests and the gateway's separate regression checks.

## Demo and limits
https://llmgate-demo.vercel.app/
The public demo labels simulated and real-model paths. Redis-backed limits are optional; breaker and latency state are per process. Usage telemetry is not billing-grade. The default simulated Docker image was built and checked on hosted CI on 11 November 2026, including installed-package imports, non-root execution, authentication, streaming and forced-primary failover. The exact default Docker Compose recipe was also checked on hosted CI on 11 November 2026 with Docker Engine 28.0.4 and Compose v2.38.2: runtime inspection confirmed only `127.0.0.1:8000`, and the healthy simulated HTTP checks passed. Compose forced-failover and mounted real-provider configurations remain unverified.

## Repository setup
GitHub is the source of truth for project changes. The active
[workflow](.github/workflows/ci.yml) runs pytest, local HTTP end-to-end tests
and a local JavaScript smoke test for pull requests and pushes to main.
Protected main requires its source check before normal merge. Vercel is
connected to this repository; preview builds are distinct from production.
Verify a staged production build before promotion, then check live workflows
end to end, including the changed behavior and failure cases. A ready build
or health probe alone is not proof of token settlement or real-provider access.

The historical example at `docs/ci.example.yml` is not the active workflow.
No license has been added to this repository.
