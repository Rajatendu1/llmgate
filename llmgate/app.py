from __future__ import annotations
import hashlib, hmac, inspect, json, logging, os, time, uuid
from dataclasses import dataclass
from contextlib import asynccontextmanager
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from .metering import Meter
from .metrics import CONTENT_TYPE as METRICS_CONTENT_TYPE, render as render_metrics
from .providers import ProviderError, estimate_prompt_tokens as _estimate_prompt_tokens, estimate_request_tokens as _estimate_request_tokens
from .redislimit import RateLimiterUnavailable, RedisRateLimiter
from .redismeter import MeterUnavailable, RedisMeter
from . import faults
from .request_log import LogUnavailable, MemoryRequestLog, RedisRequestLog, make_event
from .resilience import RateLimiter
from .router import NoProviderAvailable, Router

log = logging.getLogger('llmgate')
MAX_BODY = 1_000_000  # bytes


async def read_bounded(request, limit: int = MAX_BODY) -> bytes | None:
    """Read a request body with a hard size bound. A declared Content-Length
    over the limit is rejected WITHOUT reading the body, and a chunked or
    undeclared body is cut off as soon as it crosses the limit - an oversized
    upload is not accumulated by this helper beyond the limit. ASGI/server
    buffering and the size of a single delivered chunk are outside this
    bound. Returns None when the body exceeds the limit (caller answers 413)."""
    cl = request.headers.get("content-length")
    if cl is not None:
        try:
            if int(cl) > limit:
                return None
        except ValueError:
            pass  # a garbled length header is bounded by the stream read below
    data = bytearray()
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            return None
        data.extend(chunk)
    return bytes(data)


async def _aw(x):
    """Await limiter results when the shared (Redis) backend is in use; the
    in-memory backend returns plain values. Keeps one code path for both."""
    return await x if inspect.isawaitable(x) else x


@dataclass
class ApiKey:
    name: str
    rate: float = 20.0   # requests/sec
    burst: float = 40.0
    admin: bool = False
    token_rate: float | None = None   # sustained tokens/sec budget; None = no token limit
    token_burst: float | None = None  # token bucket capacity; defaults to 2x token_rate
    models: frozenset | None = None   # None = reach every configured alias (static-key default);
                                      # a frozenset = scoped token identity, allowlist enforced per request
    expires_at: float | None = None   # minted demo keys only; the resolver middleware evicts past due
    meter_id: str | None = None       # metering/rate-bucket identity when it must differ from the
                                      # public label: minted demo keys all share the label
                                      # "self-serve" in the public log but get private per-visitor
                                      # budgets and usage rows under their own meter_id


# Re-exported: the estimator lives in providers.py so the router can size
# timeouts with the same heuristic admission control uses.
estimate_prompt_tokens = _estimate_prompt_tokens
estimate_request_tokens = _estimate_request_tokens


def _err(status, msg, typ="gateway_error", headers=None):
    return JSONResponse({"error": {"message": msg, "type": typ}}, status_code=status, headers=headers)


# Baseline headers on every response. The landing page sets its own CSP.
SEC_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
}


def _init_error_tracking():
    """Optional Sentry error tracking, enabled by SENTRY_DSN. Privacy defaults:
    no client PII, no request bodies/headers/query/cookies (the whole request
    block is dropped), no local variables in stack frames (a local could hold a
    prompt), no performance tracing. Provider error text, which is already
    capped at 200 chars server-side, is the only upstream content in an event."""
    dsn = os.getenv("SENTRY_DSN", "").strip()
    if not dsn:
        return
    try:
        import sentry_sdk

        def _drop_request(event, hint):
            event.pop("request", None)
            return event

        sentry_sdk.init(dsn=dsn, send_default_pii=False, max_request_body_size="never",
                        include_local_variables=False, traces_sample_rate=0.0,
                        before_send=_drop_request)
        log.info("error tracking enabled (request data, locals and PII excluded)")
    except Exception as e:  # tracking must never break the gateway
        log.error("error-tracking init failed (continuing without it): %s", e)


def create_app(router: Router, keys: dict[str, ApiKey], clock=time.monotonic,
               limiter=None, meter=None, token_auth=None) -> FastAPI:
    _init_error_tracking()
    # The app adopts provider lifetimes; provider aclose() preserves borrowed
    # clients. Injected limiter/meter/store lifetimes remain caller-owned.
    owned_resources = [r.provider for r in router.routes]

    @asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            failures, seen = 0, set()
            for resource in reversed(app.state.owned_resources):
                if id(resource) in seen:
                    continue
                seen.add(id(resource))
                close = getattr(resource, "aclose", None)
                if close is not None:
                    try:
                        await close()
                    except Exception:
                        # Do not leak upstream errors, URLs or credentials.
                        failures += 1
            if failures:
                raise RuntimeError(f"{failures} gateway resource cleanup(s) failed") from None

    app = FastAPI(title="llmgate", lifespan=lifespan)
    app.state.owned_resources = owned_resources
    # Shared Redis-backed limiting when the store is configured (multi-instance
    # deployments); per-process in-memory buckets otherwise (single-process dev).
    if limiter is None:
        limiter = RedisRateLimiter.from_env() or RateLimiter(clock)
        owned_resources.append(limiter)
    # Same split for usage metering: shared counters when a store is configured
    # (reusing the limiter's connection), per process otherwise.
    if meter is None:
        meter = None
        if isinstance(limiter, RedisRateLimiter):
            meter = RedisMeter.from_env(client=limiter._client)
        meter = meter or Meter()
    # Recent-request log with the same backend split: shared capped Redis list
    # when the store is configured, per-process ring buffer otherwise.
    reqlog = RedisRequestLog.from_env(client=getattr(limiter, "_client", None)) if isinstance(limiter, RedisRateLimiter) else None
    reqlog = reqlog or MemoryRequestLog()
    app.state.meter, app.state.router, app.state.limiter = meter, router, limiter
    app.state.reqlog = reqlog
    app.state.keys = keys
    app.state.internal_chat_identities = []

    @app.middleware("http")
    async def _request_context(request: Request, call_next):
        rid = request.headers.get("x-request-id", "")[:128].strip() or uuid.uuid4().hex
        request.state.rid = rid
        t0 = time.perf_counter()
        resp = await call_next(request)
        resp.headers["X-Request-ID"] = rid
        for h, v in SEC_HEADERS.items():
            resp.headers.setdefault(h, v)
        log.info("%s %s -> %d %.1fms rid=%s", request.method, request.url.path,
                 resp.status_code, (time.perf_counter() - t0) * 1000, rid)
        return resp

    def _cost(prov, model, usage):
        """Estimated USD for one served call: the serving route's list price
        applied to upstream-reported usage. None when the route has no price
        configured or no usage was reported - no estimate is invented then."""
        price = router.price_for(prov, model)
        return price.cost(usage) if price else None

    async def _record(*args, attempts=None, rid="", meter_key=None, **kw):
        """Meter an event and append it to the recent-request log. Fail-open for
        the shared backend (observability, not enforcement): a store error is
        logged and counted on /healthz, the request it came from is unaffected.
        meter_key overrides the metered identity only; the public log keeps the
        anonymous label in args[0]."""
        try:
            if meter_key:
                args = (meter_key,) + tuple(args[1:])
            await _aw(meter.record(*args, **kw))
        except MeterUnavailable as e:
            meter.errors += 1
            log.error("metering record failed (event lost): %s", e)
        key, prov, model, usage = (list(args) + [None] * 4)[:4]
        status = args[4] if len(args) > 4 else kw.get("status", "ok")
        latency = args[5] if len(args) > 5 else kw.get("latency")
        event = make_event(key or "-", prov or "-", model or "-", usage, status,
                           latency, kw.get("cost"),
                           router.mode_for(prov) if prov and prov != "-" else "simulated",
                           attempts if attempts is not None else 1, rid)
        try:
            await _aw(reqlog.record(event))
        except LogUnavailable as e:
            reqlog.errors += 1
            log.error("request-log record failed (event lost): %s", e)

    async def _snapshot(key=None):
        """Read usage. Fail-closed for the shared backend: no answer is better
        than a partial one presented as complete."""
        try:
            return await _aw(meter.snapshot(key))
        except MeterUnavailable as e:
            log.error("metering snapshot failed: %s", e)
            raise MeterUnavailable from e

    def limiter_unavailable():
        return _err(503, "shared rate-limit store unavailable; try again shortly",
                    "rate_limiter_unavailable", {"Retry-After": "2"})

    def auth(authorization: str | None, internal_identity=None) -> ApiKey:
        # Object identity in the in-process ASGI scope, never an HTTP header
        # or serialized bearer. Only the internal chat dispatch can set it.
        if internal_identity is not None and any(
                internal_identity is k for k in app.state.internal_chat_identities):
            return internal_identity
        if not authorization or not authorization.lower().startswith("bearer "):
            raise HTTPException(401, "missing bearer token")
        token = authorization[7:].strip()
        # Stored keys may be plaintext (dev) or "sha256:<hex>" (at-rest hashing:
        # a leaked config/env dump then yields no usable key). Both compare in
        # constant time against every entry.
        token_hash = "sha256:" + hashlib.sha256(token.encode()).hexdigest()
        found = None
        for secret, k in keys.items():
            if secret.startswith("sha256:"):
                if hmac.compare_digest(secret, token_hash):
                    found = k
            elif hmac.compare_digest(secret.encode(), token.encode()):
                found = k
        if not found:
            # Additive second credential path: an embedded-issuer scoped token
            # (JWT-shaped). Static keys above are unchanged and primary; a token
            # can only ever carry a NARROWER envelope than configured keys.
            if token_auth is not None and token.count(".") == 2:
                from .tokenauth import TokenError, claims_to_apikey
                try:
                    claims = token_auth.verifier.verify(token)
                except TokenError as e:
                    raise HTTPException(401, f"invalid_token ({e})")
                if token_auth.required_scope not in claims.scope:
                    raise HTTPException(403, f"insufficient_scope (missing {token_auth.required_scope})")
                return claims_to_apikey(claims, token_auth)
            raise HTTPException(401, "invalid api key")
        return found

    @app.exception_handler(HTTPException)
    async def _h(_, exc):
        return _err(exc.status_code, exc.detail, "auth_error" if exc.status_code == 401 else "gateway_error")

    @app.get("/healthz")
    async def healthz():
        shared = isinstance(limiter, RedisRateLimiter)
        rl = {"backend": "redis" if shared else "memory", "shared": shared}
        if shared:
            rl["reachable"] = await limiter.ping()
        m_shared = isinstance(meter, RedisMeter)
        mt = {"backend": "redis" if m_shared else "memory", "shared": m_shared}
        if m_shared:
            mt["reachable"] = await meter.ping()
            mt["errors"] = meter.errors
        return {"ok": True, "providers": {r.provider.name: r.breaker.state for r in router.routes},
                "rate_limit": rl, "metering": mt}

    @app.get("/readyz")
    async def readyz():
        """Binary readiness for monitors: 200 only when the app can serve.
        Unauthenticated and secret-free: booleans and short labels only.
        Cost per probe: at most one cheap Redis command per shared backend."""
        problems = []
        if not router.routes:
            problems.append("no providers configured")
        if isinstance(limiter, RedisRateLimiter) and not await limiter.ping():
            problems.append("rate_limit_store_unreachable")
        if isinstance(meter, RedisMeter) and not await meter.ping():
            problems.append("metering_store_unreachable")
        if problems:
            return JSONResponse({"ok": False, "problems": problems}, status_code=503)
        return {"ok": True}

    @app.get("/v1/models")
    async def models(authorization: str | None = Header(None)):
        auth(authorization)
        return {"object": "list", "data": [{"id": m, "object": "model"} for m in router.models()]}

    @app.get("/admin/usage")
    async def usage(authorization: str | None = Header(None)):
        k = auth(authorization)
        if not k.admin:
            raise HTTPException(401, "admin key required")
        try:
            return await _snapshot()
        except MeterUnavailable:
            return _err(503, "shared metering store unavailable; try again shortly",
                        "metering_unavailable", {"Retry-After": "2"})

    @app.get("/metrics")
    async def metrics(authorization: str | None = Header(None)):
        """Prometheus scrape endpoint. Admin key required, like /admin/usage: per-key
        usage is not public data. Scrape it with a bearer token (Prometheus
        `authorization` config), or expose it only on an internal network."""
        k = auth(authorization)
        if not k.admin:
            raise HTTPException(401, "admin key required")
        refresh = getattr(meter, "refresh", None)  # shared backend: pull fleet counters first
        if refresh is not None:
            try:
                await _aw(refresh())
            except MeterUnavailable as e:
                log.error("metering refresh failed: %s", e)
                return _err(503, "shared metering store unavailable; try again shortly",
                            "metering_unavailable", {"Retry-After": "2"})
        return PlainTextResponse(render_metrics(meter, router), media_type=METRICS_CONTENT_TYPE)

    @app.post("/v1/chat/completions")
    async def chat(request: Request, authorization: str | None = Header(None)):
        # Measured per-stage timings, surfaced as a standard Server-Timing
        # header: auth, admission (rate/token buckets, body and scope checks),
        # routing (candidate order, breakers, retry backoff), provider (inside
        # upstream calls; for streams, time to first chunk), metering (settle +
        # record; absent on streams - headers precede the stream).
        st: dict[str, float] = {}
        perf = time.perf_counter
        adm0: float | None = None  # admission span start, once entered

        def sth(extra=None):
            # A refusal inside admission returns before the span's closing
            # stamp; stamp it lazily so the header still carries it.
            if adm0 is not None and "admission" not in st:
                st["admission"] = perf() - adm0
            h = {"Server-Timing": ", ".join(f"{k};dur={v * 1000:.1f}" for k, v in st.items())}
            return {**h, **(extra or {})}

        a0 = perf()
        key = auth(authorization, request.scope.get("llmgate.internal_chat_identity"))
        st["auth"] = perf() - a0
        ident = key.meter_id or key.name  # per-visitor budgets for minted keys
        adm0 = perf()
        # admission span: limiter, body/scope/fault checks, token bucket
        try:
            ok, retry = await _aw(limiter.check(ident, key.rate, key.burst))
        except RateLimiterUnavailable as e:
            log.error("rate limiter check failed: %s", e)
            return limiter_unavailable()
        if not ok:
            await _record(key.name, "-", "-", None, "rate_limited", attempts=0, rid=request.state.rid, meter_key=key.meter_id)
            return _err(429, "rate limit exceeded", "rate_limit_error",
                        sth({"Retry-After": str(max(1, int(retry + 0.999)))}))
        raw = await read_bounded(request)
        if raw is None:
            return _err(413, "request body too large", "invalid_request", sth())
        try:
            body = json.loads(raw)
            # Request validation must survive python -O / PYTHONOPTIMIZE.
            # JSON numbers 0/1 compare equal to bools, but are not booleans.
            if not (isinstance(body, dict)
                    and isinstance(body.get("model"), str)
                    and isinstance(body.get("messages"), list)
                    and body["messages"]
                    and all(isinstance(m, dict) for m in body["messages"])
                    and type(body.get("stream", False)) is bool):
                raise ValueError("invalid chat request shape")
        except Exception:
            return _err(400, "body must be JSON with 'model', non-empty 'messages' and boolean 'stream'",
                        "invalid_request", sth())
        if body["model"] not in router.models():
            return _err(404, f"unknown model {body['model']}", "invalid_request", sth())
        # Scoped token identities: the model allowlist is server-side config
        # (scope -> aliases), verified at request time. Static keys (None)
        # keep their full-envelope behaviour.
        if key.models is not None and body["model"] not in key.models:
            await _record(key.name, "-", body["model"], None, "scope_denied",
                          attempts=0, rid=request.state.rid, meter_key=key.meter_id)
            return _err(403, f"insufficient_scope (model {body['model']} not in token scope)",
                        "insufficient_scope", sth())
        # Idempotency-Key: forwarded to every upstream attempt EXACTLY as sent.
        # Never stripped, truncated or regenerated - altering the value would
        # merge distinct client operations into one vendor-side identity, so an
        # invalid key is rejected honestly instead. 1-255 chars, the convention
        # Stripe documents and OpenAI-compatible vendors follow.
        idem = request.headers.get("idempotency-key")
        if idem is not None and not 1 <= len(idem) <= 255:
            return _err(400, "invalid Idempotency-Key: must be 1-255 characters",
                        "invalid_request", sth())
        up_headers = {"Idempotency-Key": idem} if idem else None
        # Visitor fault injection: self-serve demo keys only (their allowlist
        # already limits them to simulated aliases, so no real upstream can
        # ever see an injected fault). Anything else asking for a fault gets
        # an honest 400 rather than a silently ignored header.
        fault = request.headers.get("x-simulate-failure", "").strip().lower()
        if fault:
            if key.name != "self-serve":
                return _err(400, "X-Simulate-Failure is honored only for self-serve demo keys",
                            "invalid_request", sth())
            if fault not in faults.ALLOWED:
                return _err(400, f"X-Simulate-Failure must be one of: {', '.join(faults.ALLOWED)}",
                            "invalid_request", sth())
            if fault == "truncate" and not body.get("stream"):
                return _err(400, "X-Simulate-Failure: truncate applies to stream requests",
                            "invalid_request", sth())
            faults.arm(fault)
        tb, est = None, 0
        if key.token_rate:
            est = estimate_request_tokens(body)
            tb = limiter.bucket(ident, key.token_rate, key.token_burst or 2 * key.token_rate)
            try:
                tok_ok, tok_retry = await _aw(tb.take(est))
            except RateLimiterUnavailable as e:
                log.error("rate limiter take failed: %s", e)
                return limiter_unavailable()
            if not tok_ok:
                await _record(key.name, "-", body["model"], None, "rate_limited_tokens", meter_key=key.meter_id,
                              attempts=0, rid=request.state.rid)
                return _err(429, f"token rate limit exceeded (estimated {est} prompt tokens)",
                            "rate_limit_error", sth({"Retry-After": str(max(1, int(tok_retry + 0.999)))}))
        st["admission"] = perf() - adm0

        async def settle(usage):
            """Settle the token bucket against reported usage: charge whatever the
            estimate under-counted, credit back whatever it over-counted (a
            max_tokens cap the response did not use). The response is already
            produced when this runs, so a store failure here is logged, not
            surfaced: the bucket keeps the admission estimate once and the next
            admission still sees the last durably stored balance."""
            if tb is not None and usage:
                try:
                    await _aw(tb.charge(usage.get("total_tokens", 0) - est))
                except RateLimiterUnavailable as e:
                    log.error("rate limiter settlement failed (balance kept the estimate once): %s", e)

        t0 = time.perf_counter()

        if not body.get("stream"):
            timings: dict[str, float] = {}
            try:
                prov, res, tried = await router.complete(body, timings=timings, headers=up_headers)
            except (NoProviderAvailable, ProviderError) as e:
                st.update(timings)
                log.warning("upstream failed: %s", e)
                await _record(key.name, "-", body["model"], None, "upstream_failed", time.perf_counter() - t0, meter_key=key.meter_id,
                              rid=request.state.rid)
                return _err(502, "no upstream provider could serve this request", headers=sth())
            st.update(timings)
            a0 = perf()
            await settle(res.get("usage"))
            await _record(key.name, prov, body["model"], res.get("usage"), "ok",
                          time.perf_counter() - t0, meter_key=key.meter_id,
                          cost=_cost(prov, body["model"], res.get("usage")),
                          attempts=len(tried), rid=request.state.rid)
            st["metering"] = perf() - a0
            return JSONResponse(res, headers=sth({"X-Gateway-Provider": prov,
                                                  "X-Gateway-Attempts": str(len(tried))}))

        timings = {}
        try:
            prov, first, rest, tried = await router.stream(body, timings=timings, headers=up_headers)
        except (NoProviderAvailable, ProviderError) as e:
            st.update(timings)
            log.warning("upstream failed: %s", e)
            await _record(key.name, "-", body["model"], None, "upstream_failed", time.perf_counter() - t0, meter_key=key.meter_id,
                          rid=request.state.rid)
            return _err(502, "no upstream provider could serve this request", headers=sth())
        st.update(timings)  # provider span here is time to first chunk

        async def gen():
            usage, status = None, "ok"
            try:
                if first is not None:
                    usage = first.get("usage") or usage
                    yield f"data: {json.dumps(first)}\n\n"
                async for chunk in rest:
                    usage = chunk.get("usage") or usage
                    yield f"data: {json.dumps(chunk)}\n\n"
                yield "data: [DONE]\n\n"
            except ProviderError as e:  # mid-stream: cannot fail over
                log.warning("mid-stream failure: %s", e)
                status = "stream_error"
                yield f"data: {json.dumps({'error': {'message': 'upstream failed mid-stream', 'type': 'stream_error'}})}\n\n"
            except BaseException:  # client disconnect / cancellation
                status = "client_disconnected"
                raise
            finally:
                aclose = getattr(rest, "aclose", None)
                if aclose:  # release the upstream connection
                    await aclose()
                await settle(usage)
                await _record(key.name, prov, body["model"], usage, status, time.perf_counter() - t0, meter_key=key.meter_id,
                              cost=_cost(prov, body["model"], usage),
                              attempts=len(tried), rid=request.state.rid)

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers=sth({"X-Gateway-Provider": prov, "X-Gateway-Attempts": str(len(tried))}))

    return app


def keys_from_env(var="LLMGATE_KEYS") -> dict[str, ApiKey]:
    """JSON in an env var: {"<secret>": {"name": "tenant-a", "rate": 10, "burst": 20, "admin": false}}"""
    import os
    raw = json.loads(os.environ[var])
    return {s: ApiKey(**cfg) for s, cfg in raw.items()}
