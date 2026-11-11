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
The curated publication snapshot passed 253 pytest cases, 61 end-to-end HTTP checks and a JavaScript smoke gate locally. The benchmarks use simulated providers and are not capacity claims. Optional real-provider tests skip without credentials.

## Included Actas component
This snapshot includes the pinned Actas 0.8.1 component used for embedded delegation. It is not the current standalone Actas project. See vendor/ACTAS.md.

## Demo and limits
https://llmgate-demo.vercel.app/
The public demo labels simulated and real-model paths. Redis-backed limits are optional; breaker and latency state are per process. Usage telemetry is not billing-grade. The Docker packaging has not been built or verified.

## Repository setup
This initial publication does not connect GitHub to Vercel or enable GitHub Actions. The existing CI definition is retained as docs/ci.example.yml, outside the active workflow directory. No license has been added to this repository.
