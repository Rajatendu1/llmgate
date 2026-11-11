"""A standalone resource server: separate process, no shared keys.

It verifies bearer tokens by fetching the issuer's JWKS over HTTP, proving
the reference works across a network boundary - the way a real resource
server would. Run beside the AS:

    uvicorn actas.app:app --port 8080                       # issuer
    ACTAS_JWKS_URL=http://127.0.0.1:8080/.well-known/jwks.json \
        uvicorn actas.resource_server:app --port 8081       # resource
"""

from __future__ import annotations

import os

import jwt as pyjwt
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse

from . import demo_fixture, dpop, scopes
from .errors import TokenExchangeError
from .jwks_client import JwksClient

ISSUER = os.environ.get("ACTAS_ISSUER", demo_fixture.ISSUER)
JWKS_URL = os.environ.get(
    "ACTAS_JWKS_URL", "http://127.0.0.1:8080/.well-known/jwks.json"
)

jwks = JwksClient(JWKS_URL)
proof_replay = dpop.ReplayCache()
# ACTAS_DPOP_NONCE=1: this server issues and requires its own nonces (the
# resource server is the nonce issuer per RFC 9449, not the token issuer).
nonce_store = dpop.NonceStore() if os.environ.get("ACTAS_DPOP_NONCE") == "1" else None
REVOKED_URL = os.environ.get("ACTAS_REVOKED_URL")  # e.g. http://127.0.0.1:8080/oauth/revoked
REVOKED_TTL = float(os.environ.get("ACTAS_REVOKED_TTL", "5"))
_revoked = {"jtis": frozenset(), "at": 0.0}


def _revoked_set(now: float | None = None) -> frozenset:
    """Poll the issuer's revocation feed, cached for REVOKED_TTL seconds.

    Fails closed: if the feed cannot be refreshed and the cache is older than
    three TTLs, requests are refused rather than served on stale revocation data.
    """
    import json
    import time
    import urllib.request

    now = time.time() if now is None else now
    if now - _revoked["at"] < REVOKED_TTL:
        return _revoked["jtis"]
    try:
        with urllib.request.urlopen(REVOKED_URL, timeout=2) as r:
            _revoked.update(jtis=frozenset(json.loads(r.read())["jtis"]), at=now)
    except Exception:
        if now - _revoked["at"] > 3 * REVOKED_TTL:
            raise TokenExchangeError(
                "temporarily_unavailable", "revocation feed unreachable; refusing", 503)
    return _revoked["jtis"]
app = FastAPI(title="actas remote resource server", version="0.1.0")


@app.exception_handler(TokenExchangeError)
async def pop_error_handler(_request: Request, exc: TokenExchangeError):
    return JSONResponse(status_code=exc.status_code, content=exc.as_response(),
                        headers=exc.headers)


@app.exception_handler(pyjwt.InvalidTokenError)
async def jwt_error_handler(_request: Request, exc: pyjwt.InvalidTokenError):
    return JSONResponse(status_code=401, content={"error": "invalid_token", "detail": str(exc)})


def _bearer_claims(request: Request) -> dict:
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise pyjwt.InvalidTokenError("missing bearer token")
    token = auth.split(None, 1)[1]
    claims = jwks.verify(token, issuer=ISSUER, audience=demo_fixture.CALENDAR_API)
    if REVOKED_URL and claims.get("jti") in _revoked_set():
        raise TokenExchangeError("invalid_token", "token has been revoked", 401)
    jkt = claims.get("cnf", {}).get("jkt")
    if jkt:
        proof = request.headers.get("dpop", "")
        if not proof:
            raise TokenExchangeError(
                "invalid_token", "token is sender-constrained; DPoP proof required", 401
            )
        dpop.verify_proof(
            proof, expected_jkt=jkt, method=request.method,
            uri=str(request.url), access_token=token, replay=proof_replay,
            nonce_store=nonce_store,
        )
    return claims


@app.get("/healthz")
def healthz():
    return {"status": "ok", "jwks_url": JWKS_URL}


@app.get("/events")
def events(claims: dict = Depends(_bearer_claims)):
    if "read:calendar" not in scopes.parse(claims.get("scope")):
        return JSONResponse(
            status_code=403,
            content={"error": "insufficient_scope", "required": "read:calendar"},
        )
    return {
        "events": demo_fixture.CALENDAR_EVENTS,
        "served_for": claims["sub"],
        "verified_via": "remote JWKS",
    }
