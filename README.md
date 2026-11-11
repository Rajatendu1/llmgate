# llmgate
An OpenAI-compatible gateway in Python and FastAPI, with routing, failover, streaming, per-key limits and usage telemetry.

Built by Instinct, an AI assistant, under Rajatendu Dey's direction. This is a tested prototype, not a production-certified service.

## Run locally
```sh
pip install -e '.[dev]'
pytest -q
uvicorn llmgate.demo_app:app --port 8000
```

The local demo uses simulated providers. See examples.config.json and docs/DEPLOYMENT.md for configuration. Real upstreams require your own credentials; do not commit secrets. Ambiguous-failure failover is off by default; enabling it can duplicate generation or charges.

## Verification
The Actas 0.14.1 integration candidate passed 266 pytest cases, 61 end-to-end HTTP checks and a JavaScript smoke gate locally. The benchmarks use simulated providers and are not capacity claims. Optional real-provider tests skip without credentials.

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
The public demo labels simulated and real-model paths. Redis-backed limits are optional; breaker and latency state are per process. Usage telemetry is not billing-grade. The Docker packaging has not been built or verified.

## Repository setup
This initial publication does not connect GitHub to Vercel or enable GitHub Actions. The existing CI definition is retained as docs/ci.example.yml, outside the active workflow directory. No license has been added to this repository.
