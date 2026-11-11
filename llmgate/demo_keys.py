"""POST /demo/keys: mint a temporary, tightly-scoped API key for visitors.

This is what turns the demo into a usable tool: a visitor gets a real key
they can paste into any OpenAI-compatible client and drive the running
gateway end to end - auth, admission, routing, failover, streaming,
metering - against the SIMULATED fleet only.

Hard bounds, by construction:
- minted keys carry a model allowlist of the simulated aliases only
  (enforced per request in app.chat, the same check the scoped-JWT path
  uses); the real Groq route is unreachable with them;
- tiny per-key budgets (requests and tokens), a 24h TTL, a per-IP mint
  limit and a global daily mint cap;
- the record store keeps only the key's SHA-256 digest; the plaintext key
  is returned once and never stored, logged or recoverable.
- DELETE /demo/keys revokes the presented key: the store record is deleted,
  and every instance drops it within the cache revalidation window
  (revalidate_s, 60s by default) - bounded staleness, honestly disclosed. A
  store error during due revalidation returns 503, never extends cached
  authorization. A failed deletion returns 503, not a claimed revocation.
  In-flight requests are not cancelled by revocation.
The shared store (Upstash REST) makes minted keys work across function
instances; without it records live per process and expire with it, which
the mint response discloses honestly.
"""
from __future__ import annotations
import hashlib
import logging
import json
import secrets
import time

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from .app import ApiKey

log = logging.getLogger("llmgate.demo_keys")
from .redislimit import RedisRateLimiter, store_credentials_from_env
from .resilience import RateLimiter

KEY_PREFIX = "vg_"
DEFAULT_TTL_S = 24 * 3600
KEY_RATE, KEY_BURST = 0.5, 6.0            # sustained req/s, small burst
KEY_TOKEN_RATE, KEY_TOKEN_BURST = 20.0, 400.0
PER_IP_PER_HOUR = 3
GLOBAL_PER_DAY = 500
STORE_PREFIX = "llmgate:dynkey:"


def digest_of(token: str) -> str:
    return "sha256:" + hashlib.sha256(token.encode()).hexdigest()


class _MemoryStore:
    """Per-process records; shared-store semantics hold within one instance."""
    shared = False

    def __init__(self):
        self._recs: dict[str, dict] = {}
        self._limiter = RateLimiter()

    async def get(self, digest: str) -> dict | None:
        rec = self._recs.get(digest)
        if rec and rec["expires"] > time.time():
            return rec
        self._recs.pop(digest, None)
        return None

    async def get_or_raise(self, digest: str) -> dict | None:
        """Like get, but store errors raise instead of reading as a miss."""
        return await self.get(digest)

    async def put(self, digest: str, record: dict, ttl_s: int) -> None:
        self._recs[digest] = record

    async def delete(self, digest: str) -> None:
        self._recs.pop(digest, None)

    async def take(self, name: str, rate: float, burst: float) -> bool:
        ok, _ = self._limiter.bucket(name, rate, burst).take()
        return ok


class _RedisStore:
    """Shared records via the same Upstash REST store the limiter uses."""
    shared = True

    def __init__(self, limiter: RedisRateLimiter):
        self._rl = limiter

    async def aclose(self):
        """This store owns the limiter/client built by store_from_env()."""
        await self._rl.aclose()

    async def _cmd_strict(self, *args):
        import asyncio
        for attempt in range(2):
            try:
                r = await self._rl._client.post("/", json=list(args))
                body = r.json()
                if r.status_code == 200 and isinstance(body, dict) and "error" not in body and "result" in body:
                    return body["result"]
            except Exception:
                pass
            if attempt == 0:
                await asyncio.sleep(0.15)
        # Never expose a provider payload or credential in this error.
        raise RuntimeError("demo-key store unavailable")

    async def get_or_raise(self, digest: str) -> dict | None:
        raw = await self._cmd_strict("GET", STORE_PREFIX + digest)
        if raw is None:
            return None
        rec = json.loads(raw)
        return rec if rec.get("expires", 0) > time.time() else None

    async def get(self, digest: str) -> dict | None:
        try:
            return await self.get_or_raise(digest)
        except Exception:
            return None

    async def put(self, digest: str, record: dict, ttl_s: int) -> None:
        result = await self._cmd_strict("SETEX", STORE_PREFIX + digest, str(int(ttl_s)), json.dumps(record))
        if result != "OK":
            raise RuntimeError("demo-key store write not acknowledged")

    async def delete(self, digest: str) -> None:
        result = await self._cmd_strict("DEL", STORE_PREFIX + digest)
        if type(result) is not int or result not in (0, 1):
            raise RuntimeError("demo-key deletion not acknowledged")

    async def take(self, name: str, rate: float, burst: float) -> bool:
        ok, _ = await self._rl.bucket(name, rate, burst).take()
        return ok


def store_from_env() -> "_RedisStore | _MemoryStore":
    creds = store_credentials_from_env()
    if creds:
        return _RedisStore(RedisRateLimiter(*creds))
    return _MemoryStore()


def install_demo_keys(app, models=("gpt-sim", "gpt-sim-pro"), ttl_s: int = DEFAULT_TTL_S,
                      store=None, revalidate_s: float = 60.0, clock=time.monotonic) -> None:
    """Mint endpoint + the middleware that resolves minted keys for auth().
    models must be simulated aliases; the install refuses anything else by
    construction (callers pass only demo aliases - deploy.config.json marks
    each provider's mode, and this function checks the router's own map)."""
    router = app.state.router
    # An alias is safe only if EVERY route able to serve it is explicitly
    # simulated. Looking at the first provider permits a real backup leg.
    allowed = frozenset(
        m for m in models
        if m in router.models() and all(
            getattr(r.provider, "mode", "real") == "simulated"
            for r in router.routes if m in r.provider.models))
    if not allowed:
        raise ValueError("demo keys: no simulated alias to scope keys to")
    st = store if store is not None else store_from_env()
    if store is None:
        app.state.owned_resources.append(st)
    app.state.dynamic_keys = st
    keys = app.state.keys
    cached_at: dict[str, float] = {}  # token -> when this instance last saw it in the store

    @app.middleware("http")
    async def _resolve_minted_keys(request: Request, call_next):
        authz = request.headers.get("authorization", "")
        if authz.lower().startswith("bearer " + KEY_PREFIX):
            token = authz[7:].strip()
            existing = keys.get(token)
            if existing is not None and existing.expires_at is not None \
                    and existing.expires_at <= time.time():
                keys.pop(token, None)
                cached_at.pop(token, None)
                existing = None
            if existing is not None and clock() - cached_at.get(token, 0) >= revalidate_s:
                # Once the cache is due, an unavailable store cannot extend
                # authorization. Keep the record for recovery, but serve 503.
                checked_at = clock()
                try:
                    rec = await st.get_or_raise(digest_of(token))
                except Exception:
                    return JSONResponse({"ok": False, "error": "demo-key store unavailable"},
                                        status_code=503, headers={"Retry-After": "2"})
                if rec is not None and not set(rec.get("models", ())).issubset(allowed):
                    rec = None
                if rec is None:
                    keys.pop(token, None)
                    cached_at.pop(token, None)
                    existing = None
                else:
                    cached_at[token] = checked_at
            if existing is None:
                checked_at = clock()
                try:
                    rec = await st.get_or_raise(digest_of(token))
                except Exception:
                    return JSONResponse({"ok": False, "error": "demo-key store unavailable"},
                                        status_code=503, headers={"Retry-After": "2"})
                if rec is not None and not set(rec.get("models", ())).issubset(allowed):
                    # A record minted under an older/different fleet cannot
                    # preserve access to an alias that now has a real route.
                    rec = None
                if rec is not None:
                    # Compatibility: records minted before per-visitor
                    # identities shipped carry no meter_id. Derive the same
                    # value the mint path computes - it is a pure function of
                    # the key's digest, which is exactly what the store holds -
                    # so old keys keep working with their own usage row and
                    # budget, no limits reset and no scope widened.
                    meter_id = rec.get("meter_id") or (
                        "self-serve:" + digest_of(token).split(":", 1)[1][:12])
                    keys[token] = ApiKey(
                        name=rec["name"], rate=rec["rate"], burst=rec["burst"],
                        token_rate=rec.get("token_rate"), token_burst=rec.get("token_burst"),
                        models=frozenset(rec["models"]), expires_at=rec["expires"],
                        meter_id=meter_id)
                    cached_at[token] = checked_at
        return await call_next(request)

    @app.get("/demo/usage")
    async def _demo_usage(request: Request):
        """Own-key usage for minted demo keys: a visitor reads the usage of
        exactly the key they present, and nothing else - one visitor's
        consumption is never visible to another. Fail-closed like the other
        meter reads: no partial answer presented as complete."""
        from .app import _err  # shared error shape
        authz = request.headers.get("authorization", "")
        if not authz.lower().startswith("bearer " + KEY_PREFIX):
            raise HTTPException(401, "a self-serve demo key is required")
        token = authz[7:].strip()
        k = keys.get(token)
        if k is None or not (k.meter_id or "").startswith("self-serve:"):
            raise HTTPException(401, "unknown or expired demo key")
        from .app import _aw
        try:
            usage = await _aw(app.state.meter.snapshot(k.meter_id))
        except Exception:
            return JSONResponse({"ok": False, "error": "metering store unavailable; try again shortly"},
                                status_code=503, headers={"Retry-After": "2"})
        return {"ok": True, "usage": usage,
                "note": "usage of the presented key only, from the same metering the public counters use"}

    @app.delete("/demo/keys")
    async def _demo_revoke(request: Request):
        """Revoke the presented demo key: the store record is deleted and this
        instance's cache is evicted, so the key stops authenticating here at
        once and everywhere else within the cache revalidation window."""
        authz = request.headers.get("authorization", "")
        if not authz.lower().startswith("bearer " + KEY_PREFIX):
            raise HTTPException(401, "a self-serve demo key is required")
        token = authz[7:].strip()
        digest = digest_of(token)
        try:
            known = keys.get(token) is not None or await st.get_or_raise(digest) is not None
            if known:
                await st.delete(digest)
        except Exception:
            return JSONResponse({"ok": False, "error": "revocation not confirmed; retry shortly"},
                                status_code=503, headers={"Retry-After": "2"})
        if not known:
            return {"ok": True, "revoked": False, "note": "unknown or already-expired key"}
        keys.pop(token, None)
        cached_at.pop(token, None)
        return {"ok": True, "revoked": True,
                "note": f"immediate on this instance; other instances drop it within {int(revalidate_s)}s"}

    @app.post("/demo/keys")
    async def _demo_keys(request: Request):
        fwd = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
        ip = fwd or (request.client.host if request.client else "unknown")
        # Per-IP mint budget, then a global daily cap - both shared when the
        # store is, so serverless instances cannot each hand out their own.
        if not await st.take(f"mint:ip:{ip}", PER_IP_PER_HOUR / 3600, float(PER_IP_PER_HOUR)):
            return JSONResponse({"ok": False, "error": "mint limit: a few keys per hour per address"},
                                status_code=429)
        if not await st.take("mint:global", GLOBAL_PER_DAY / 86400, float(GLOBAL_PER_DAY)):
            return JSONResponse({"ok": False, "error": "the demo handed out its keys for today"},
                                status_code=429)
        token = KEY_PREFIX + secrets.token_urlsafe(18)
        meter_id = "self-serve:" + digest_of(token).split(":", 1)[1][:12]
        record = {"name": "self-serve", "meter_id": meter_id,
                  "rate": KEY_RATE, "burst": KEY_BURST,
                  "token_rate": KEY_TOKEN_RATE, "token_burst": KEY_TOKEN_BURST,
                  "models": sorted(allowed), "created": time.time(),
                  "expires": time.time() + ttl_s}
        try:
            await st.put(digest_of(token), record, ttl_s)
        except Exception:
            return JSONResponse({"ok": False, "error": "key creation not confirmed; retry shortly"},
                                status_code=503, headers={"Retry-After": "2"})
        base = str(request.base_url).rstrip("/")
        return {"ok": True, "key": token, "expires_in_s": ttl_s,
                "models": sorted(allowed), "base_url": base + "/v1",
                "shared_store": st.shared,
                "limits": f"{KEY_RATE:g} req/s and {KEY_TOKEN_RATE:g} tokens/s, simulated models only"}
