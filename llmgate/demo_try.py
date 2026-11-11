"""POST /demo/try: one bounded visitor request through the real gateway pipeline.

The visitor never holds a key: this endpoint uses an in-process identity
with its own tight rate limits and sends one small, non-streaming completion
through auth, admission control, routing, failover and metering - then reports
what happened. For this individual request the routing headers are
authoritative (unlike the aggregate counters, which cannot show one request's
path). Default/custom prompts use simulated-only aliases. Explicit real
selectors use a fixed prompt and a separate tighter identity.

Abuse bounds (defense in depth, none of them absolute on their own):
- PER_IP_PER_MIN per instance, plus the platform edge rule (60 req/min/IP);
- the visitor identity's own buckets (shared across all visitors, and shared across
  instances when the store is configured): sustained 0.2 req/s, burst 3, and a
  small token budget, so a scripted loop degrades to 429s quickly;
- max_tokens is fixed small and streaming is off.
The absolute backstop is the vendor plan's own hard quota, not this code."""
from __future__ import annotations
import time

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse

from .app import ApiKey, read_bounded

PER_IP_PER_MIN = 10
MAX_TOKENS = 24
REAL_MAX_TOKENS = 256  # a reasoning model spends completion tokens on reasoning first
PROMPT = "Say hello in one short sentence."
MAX_PROMPT_CHARS = 280  # custom prompts stay small and simulated-only
TRY_BODY_CAP = 16_384  # bytes: a Try body is one small prompt; bounded read,
                       # same intake rule as the chat endpoint


def install_demo_try(app, model: str | None = None) -> None:
    # Tight global budget for all visitor traffic: 0.2 req/s sustained with a
    # burst of 3, and ~10 tokens/s so the fixed max_tokens cap settles cheaply.
    visitor_identity = ApiKey("visitor", rate=0.2, burst=3,
                                  token_rate=10.0, token_burst=100.0)
    # Real-model visitor traffic is much tighter: the budgets stay under the
    # provider's free-tier hard quotas (1K requests/day, 200K tokens/day) with
    # margin even if saturated around the clock - ~864 requests and ~173K
    # tokens a day worst case. The vendor quota remains the absolute backstop.
    real_identity = ApiKey("visitor-real", rate=0.01, burst=3,
                                       token_rate=2.0, token_burst=600.0)
    app.state.internal_chat_identities.extend([visitor_identity, real_identity])

    # No reusable bearer exists for Try. An in-process transport adds an
    # object identity to the ASGI scope, which cannot be supplied over HTTP.
    def internal_transport(identity):
        async def dispatch(scope, receive, send):
            if scope.get("type") != "http" or scope.get("method") != "POST" or scope.get("path") != "/v1/chat/completions":
                raise RuntimeError("internal chat dispatch target refused")
            await app({**scope, "llmgate.internal_chat_identity": identity}, receive, send)
        return httpx.ASGITransport(app=dispatch)

    def simulated_alias(alias):
        routes = [r for r in app.state.router.routes if alias in r.provider.models]
        return bool(routes) and all(getattr(r.provider, "mode", "real") == "simulated" for r in routes)

    hits: dict[str, list[float]] = {}
    app.state.demo_try_hits = hits

    # The install-time default model (index.py passes gpt-sim). The handler's
    # query param is also named `model` and shadows the argument, so keep the
    # default under its own name: without it the default fell through to the
    # first ALPHABETICAL alias, which sends the "simulated" button to a real
    # provider whenever one is active (gpt-real sorts before gpt-sim).
    default_model = model

    @app.post("/demo/try")
    async def _demo_try(request: Request, model: str | None = None) -> JSONResponse:
        fwd = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
        ip = fwd or (request.client.host if request.client else "unknown")
        now = time.monotonic()
        recent = [t for t in hits.get(ip, []) if now - t < 60.0]
        if len(recent) >= PER_IP_PER_MIN:
            return JSONResponse(
                {"ok": False, "error": f"visitor limit: {PER_IP_PER_MIN} test requests per minute"},
                status_code=429)
        recent.append(now)
        hits[ip] = recent

        if model not in (None, "real", "real-backup"):
            return JSONResponse({"ok": False, "error": "unknown Try selector"}, status_code=400)

        # A custom prompt is accepted for the default SIMULATED route only:
        # bounded length, never sent to a real provider (the real buttons keep
        # their fixed one-line prompt, so real quota is spent on exactly one
        # known shape).
        prompt = PROMPT
        # Cap intake even when the content type is not JSON (its content is
        # ignored below, but must not bypass the same byte limit).
        raw = await read_bounded(request, TRY_BODY_CAP)
        if raw is None:
            return JSONResponse(
                {"ok": False, "error": f"request body too large (over {TRY_BODY_CAP} bytes)"},
                status_code=413)
        if (request.headers.get("content-type") or "").startswith("application/json"):
            try:
                import json as _json
                body = _json.loads(raw)
            except Exception:
                body = None
            if isinstance(body, dict) and body.get("prompt") is not None:
                if model in ("real", "real-backup"):
                    return JSONResponse(
                        {"ok": False, "error": "custom prompts go to the simulated route only"},
                        status_code=400)
                prompt = str(body["prompt"]).strip()
                if not prompt or len(prompt) > MAX_PROMPT_CHARS:
                    return JSONResponse(
                        {"ok": False, "error": f"prompt must be 1-{MAX_PROMPT_CHARS} characters"},
                        status_code=400)

        # Only the two explicit real selectors can reach a live alias;
        # the default path is checked against every route for its alias. A real model exists only
        # when a provider with mode "real" is active (keyed in the deployment).
        identity = visitor_identity
        mdl = default_model or next((m for m in sorted(app.state.router.models()) if simulated_alias(m)), None)
        if model is None and not simulated_alias(mdl):
            return JSONResponse({"ok": False, "error": "no simulated-only Try model on this deployment"}, status_code=404)
        payload = {"model": mdl, "max_tokens": MAX_TOKENS,
                   "messages": [{"role": "user", "content": prompt}]}
        if model in ("real", "real-backup"):
            # ?model=real: the first alias a real-mode provider serves (the
            # primary path). ?model=real-backup: the alias only the same-
            # provider backup serves - a direct backup model call, not a
            # failover. Both share the one tight visitor-real budget above.
            wanted = "gpt-real-backup" if model == "real-backup" else None
            real_model = next(
                (m for route in app.state.router.routes
                 if getattr(route.provider, "mode", "real") == "real"
                 for m in sorted(route.provider.models)
                 if wanted is None or m == wanted), None)
            if real_model is None:
                return JSONResponse(
                    {"ok": False, "error": ("no same-provider backup model on this deployment"
                                            if wanted else
                                            "no real provider is active on this deployment")},
                    status_code=404)
            identity = real_identity
            mdl = real_model  # cost and logging use the resolved alias, not "real"
            payload = {"model": real_model, "max_tokens": REAL_MAX_TOKENS,
                       # gpt-oss spends tokens on reasoning first; low effort
                       # keeps the demo answer short, fast and cheap.
                       "reasoning_effort": "low",
                       "messages": [{"role": "user", "content": PROMPT}]}
        t0 = time.monotonic()
        async with httpx.AsyncClient(transport=internal_transport(identity),
                                     base_url="http://demo-try.internal") as cl:
            r = await cl.post("/v1/chat/completions", json=payload)
        latency_ms = round((time.monotonic() - t0) * 1000)
        provider = r.headers.get("X-Gateway-Provider", "")
        mode = app.state.router.mode_for(provider) if provider else None
        out = {"ok": r.status_code == 200, "status": r.status_code,
               "provider": provider or None, "mode": mode,
               "attempts": int(r.headers.get("X-Gateway-Attempts", "1")),
               "latency_ms": latency_ms}
        # Forward the gateway's measured per-stage timings (Server-Timing) so
        # the page can label each pipeline stage with its real share of THIS
        # request instead of an illustrative sweep.
        timings = {}
        for part in r.headers.get("server-timing", "").split(","):
            if ";dur=" in part:
                name, dur = part.split(";dur=", 1)
                try:
                    timings[name.strip()] = round(float(dur), 1)
                except ValueError:
                    pass
        if timings:
            out["timings_ms"] = timings
        if r.status_code == 200:
            body = r.json()
            # The upstream's own model id, when it differs from the public
            # alias - shows a same-provider backup serving under the alias.
            upstream = body.get("model")
            if upstream and upstream != mdl:
                out["upstream_model"] = upstream
            usage = body.get("usage") or {}
            out["usage"] = {"prompt_tokens": usage.get("prompt_tokens", 0),
                            "completion_tokens": usage.get("completion_tokens", 0)}
            price = app.state.router.price_for(provider, mdl) if provider else None
            if price and usage:
                out["cost_usd"] = price.cost(usage)
            try:
                out["text"] = body["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError):
                pass
        else:
            try:
                out["error"] = str(r.json().get("error", {}).get("message", "request failed"))[:200]
            except ValueError:
                out["error"] = "request failed"
        return JSONResponse(out)
