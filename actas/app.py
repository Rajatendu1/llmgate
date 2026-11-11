"""FastAPI application: exchange endpoint, JWKS, demo resource APIs, demo UI.

Everything is in-memory and single-process: this is a runnable reference,
not a deployment topology.
"""

from __future__ import annotations

import pathlib

from fastapi import FastAPI, Form, Request
from fastapi.responses import FileResponse, JSONResponse

from . import demo_fixture, errors, scopes, tokens
from .exchange import GRANT_TYPE

import hmac
import os
import time

from . import persist as persist_mod
from .observability import Metrics, clean_request_id, log_line

# ACTAS_DATA_DIR enables durable keys, audit chain and token registry in one
# SQLite file. Unset: everything is in memory and resets on restart.
_data_dir = os.environ.get("ACTAS_DATA_DIR")
_persist = None
if _data_dir:
    os.makedirs(_data_dir, exist_ok=True)
    _persist = persist_mod.Persistence(
        os.path.join(_data_dir, "actas.db"), os.environ.get("ACTAS_KEY_PASSPHRASE"))
service, keystore, audit_log = demo_fixture.build_service(persist=_persist)
registry = service.registry
from .dpop import ReplayCache  # noqa: E402

# In durable mode the replay cache lives in the same SQLite file, so a used
# proof stays used across a restart. In memory mode it is per process.
from .dpop import DurableReplayCache, NonceStore  # noqa: E402

proof_replay = DurableReplayCache(_persist.conn) if _persist else ReplayCache()

# ACTAS_DPOP_NONCE=1 makes resource APIs demand a fresh server nonce in every
# proof (RFC 9449 section 8 challenge flow). Off by default.
nonce_store = NonceStore() if os.environ.get("ACTAS_DPOP_NONCE") == "1" else None
from .clients import ClientAuthenticator, credentials_from  # noqa: E402

# ACTAS_REQUIRE_CLIENT_AUTH=1 makes the token endpoint demand a registered
# client's secret. Register clients with `python -m actas.register_client`.
client_auth = ClientAuthenticator(
    _persist.conn if _persist else None,
    required=os.environ.get("ACTAS_REQUIRE_CLIENT_AUTH") == "1")
ADMIN_TOKEN = os.environ.get("ACTAS_ADMIN_TOKEN")  # unset: admin endpoints disabled

app = FastAPI(title="actas reference server", version="0.8.1")

metrics = Metrics()


@app.middleware("http")
async def request_logging_middleware(request: Request, call_next):
    """One structured JSON log line and one metrics tick per request.

    Records method, path, status and duration only - never headers, query
    strings or bodies, so token material cannot land in the request log.
    """
    start = time.monotonic()
    request_id = clean_request_id(request.headers.get("x-request-id"))
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
    finally:
        entry = metrics.record(request.method, request.url.path, status,
                               (time.monotonic() - start) * 1000, request_id)
        log_line(entry)
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    if request.url.path == "/":
        # the demo page uses inline style/script and Google Fonts; same-origin fetches
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src https://fonts.gstatic.com; connect-src 'self'; "
            "img-src 'self'; frame-ancestors 'none'; base-uri 'none'")
    return response

DEMO_DIR = pathlib.Path(__file__).resolve().parent.parent / "demo"


@app.exception_handler(errors.TokenExchangeError)
async def exchange_error_handler(_request: Request, exc: errors.TokenExchangeError):
    return JSONResponse(status_code=exc.status_code, content=exc.as_response(),
                        headers=exc.headers)


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/metrics")
def metrics_endpoint():
    """Aggregate request counters. No per-client data; per-process only."""
    return metrics.snapshot()


@app.get("/metrics/recent")
def metrics_recent(request: Request, n: int = 50):
    """Recent request entries. Admin-gated: timing data is operational."""
    _admin(request)
    return {"entries": metrics.recent(n)}


@app.get("/.well-known/jwks.json")
def jwks():
    return keystore.jwks()


@app.post("/oauth/token")
def token_endpoint(
    request: Request,
    client_secret: str | None = Form(default=None),
    grant_type: str | None = Form(default=None),
    subject_token: str | None = Form(default=None),
    subject_token_type: str | None = Form(default=None),
    actor_token: str | None = Form(default=None),
    actor_token_type: str | None = Form(default=None),
    client_id: str | None = Form(default=None),
    scope: str | None = Form(default=None),
    audience: str | None = Form(default=None),
    requested_ttl: int | None = Form(default=None),
    cnf_jwk: str | None = Form(default=None),
):
    if client_auth.required:
        cid, sec = credentials_from(dict(request.headers), client_id, client_secret)
        if not client_auth.verify(cid, sec):
            audit_log.append("client.auth_failed", {"client_id": (cid or "")[:80]})
            raise errors.TokenExchangeError(
                "invalid_client", "client authentication failed", 401)
        client_id = cid
    # RFC 9449 binding path: a DPoP proof on the token request binds the
    # issued token to the proof's key, as an alternative to the cnf_jwk form
    # field. The proof is verified here; the exchange then records cnf.jkt.
    dpop_proof = request.headers.get("dpop")
    if dpop_proof:
        import json as _json

        from . import dpop as _dpop
        jkt, proof_jwk = _dpop.proof_jkt(dpop_proof)
        _dpop.verify_proof(
            dpop_proof, expected_jkt=jkt, method=request.method,
            uri=str(request.url), replay=proof_replay, nonce_store=nonce_store)
        if cnf_jwk is not None:
            try:
                if _dpop.thumbprint(_json.loads(cnf_jwk)) != jkt:
                    raise errors.invalid_request(
                        "cnf_jwk and the DPoP proof carry different keys")
            except ValueError:
                raise errors.invalid_request("invalid cnf_jwk")
        cnf_jwk = _json.dumps(proof_jwk)
    result = service.exchange(
        grant_type=grant_type,
        subject_token=subject_token,
        subject_token_type=subject_token_type,
        actor_token=actor_token,
        actor_token_type=actor_token_type,
        client_id=client_id,
        scope=scope,
        audience=audience,
        requested_ttl=requested_ttl,
        cnf_jwk=cnf_jwk,
    )
    return result.as_response()


def _admin(request: Request) -> None:
    """Admin endpoints need ACTAS_ADMIN_TOKEN configured and presented."""
    if not ADMIN_TOKEN:
        raise errors.TokenExchangeError(
            "access_denied", "admin endpoints are disabled on this server", 403)
    auth = request.headers.get("authorization", "")
    given = auth.split(None, 1)[1] if auth.lower().startswith("bearer ") else ""
    if not hmac.compare_digest(given.encode(), ADMIN_TOKEN.encode()):
        raise errors.TokenExchangeError("invalid_client", "admin authentication failed", 401)


@app.post("/oauth/revoke")
def revoke_endpoint(request: Request, token: str = Form(...), reason: str = Form(default="")):
    """RFC 7009-style revocation. Revokes the token and everything derived from it.

    Like the RFC, an unrecognizable token is not an error: the response is 200.
    """
    _admin(request)
    try:
        claims = tokens.verify_token(keystore, token, issuer=demo_fixture.ISSUER)
    except errors.TokenExchangeError:
        return {"revoked": False}
    new = registry.revoke(claims["jti"], reason)
    if new:
        audit_log.append("token.revoked", {"jti": claims["jti"], "sub": claims["sub"],
                                            "reason": reason[:200]})
    return {"revoked": True, "newly_revoked": new}


@app.get("/oauth/revoked")
def revoked_feed():
    """Public CRL-style feed: opaque jtis of unexpired revoked tokens.

    Lets resource servers that verify JWTs offline close the revocation gap
    by polling. It reveals no claims; jtis are random UUIDs.
    """
    return {"jtis": registry.revoked_unexpired(), "as_of": int(__import__("time").time())}


@app.post("/oauth/introspect")
def introspect_endpoint(request: Request, token: str = Form(...)):
    """RFC 7662-style introspection: active, plus claims when active."""
    _admin(request)
    try:
        claims = tokens.verify_token(keystore, token, issuer=demo_fixture.ISSUER)
    except errors.TokenExchangeError:
        return {"active": False}
    if registry.is_revoked(claims.get("jti")):
        return {"active": False}
    return {"active": True, **claims}


def _require(scope_needed: str, audience: str):
    """Dependency factory: validate the bearer token for a demo resource API."""

    def checker(request: Request) -> dict:
        auth = request.headers.get("authorization", "")
        if not auth.lower().startswith("bearer "):
            raise errors.TokenExchangeError(
                "invalid_request", "missing bearer token", 401
            )
        token = auth.split(None, 1)[1]
        claims = tokens.verify_token(keystore, token, issuer=demo_fixture.ISSUER, audience=audience)
        if registry.is_revoked(claims.get("jti")):
            raise errors.TokenExchangeError("invalid_token", "token has been revoked", 401)
        cnf = claims.get("cnf", {})
        if cnf.get("jkt"):
            from . import dpop

            proof = request.headers.get("dpop", "")
            if not proof:
                raise errors.TokenExchangeError(
                    "invalid_token", "token is sender-constrained; DPoP proof required", 401
                )
            dpop.verify_proof(
                proof,
                expected_jkt=cnf["jkt"],
                method=request.method,
                uri=str(request.url),
                access_token=token,
                replay=proof_replay,
                nonce_store=nonce_store,
            )
        have = scopes.parse(claims.get("scope"))
        if scope_needed not in have:
            raise errors.TokenExchangeError(
                "insufficient_scope", f"requires scope {scope_needed}", 403
            )
        return claims

    return checker


from fastapi import Depends  # noqa: E402


@app.get("/api/calendar/events")
def calendar_events(claims: dict = Depends(_require("read:calendar", demo_fixture.CALENDAR_API))):
    return {
        "events": demo_fixture.CALENDAR_EVENTS,
        "served_for": claims["sub"],
        "acted_by": tokens.act_chain(claims.get("act")),
    }


@app.post("/api/notes")
def create_note(
    body: dict,
    claims: dict = Depends(_require("write:notes", demo_fixture.NOTES_API)),
):
    return {
        "created": True,
        "note": {"id": "note-1", "text": body.get("text", "")},
        "served_for": claims["sub"],
        "acted_by": tokens.act_chain(claims.get("act")),
    }


@app.get("/audit/tail")
def audit_tail(n: int = 50):
    return {"entries": audit_log.tail(n), "head": audit_log.head_hash()}


@app.get("/audit/checkpoint")
def audit_checkpoint():
    """A signed (seq, head hash, time) statement for an external witness to keep.

    Store it somewhere the operator cannot edit (another party, an append-only
    store). Later, `consistent_with(seq, hash)` shows whether history was
    rewritten. The signature shows the issuer produced it; it does NOT make the
    log tamper-proof: the protection comes entirely from where it is stored.
    """
    import time as _t
    seq = len(audit_log)
    head = audit_log.head_hash()
    if not head:
        raise errors.TokenExchangeError("invalid_request", "log is empty", 409)
    encoded, claims = tokens.issue_token(
        keystore, issuer=demo_fixture.ISSUER, subject="audit-checkpoint",
        scope="audit:checkpoint", audience=["witness"], expires_in=10 * 365 * 86400,
        extra={"seq": seq, "head": head})
    return {"seq": seq, "head": head, "signed": encoded}


@app.get("/audit/verify")
def audit_verify():
    ok, bad = audit_log.verify()
    return {"ok": ok, "first_bad_seq": bad, "length": len(audit_log)}


@app.get("/demo/scenario")
def run_scenario():
    """Run the scripted fictional delegation story and return the full trace.

    Hop 0: Asha holds a user token (issued by the AS for the demo).
    Hop 1: PlannerBot exchanges it for a token to read her calendar and
           write notes, narrowed by policy.
    Hop 2: PlannerBot passes work to Summarizer, which exchanges again and
           can only keep read:calendar - write:notes does not survive.
    Hop 3 (denied): PlannerBot tries to widen to admin:all and is refused.
    """
    from . import exchange as exchange_mod

    # isolated per call: public visitors cannot grow shared state or see
    # each other's audit entries
    service, keystore, audit_log = demo_fixture.build_service()

    trace: list[dict] = []

    user_token, user_claims = tokens.issue_token(
        keystore,
        issuer=demo_fixture.ISSUER,
        subject=demo_fixture.USER_ASHA,
        scope=scopes.render(demo_fixture.USER_FULL_SCOPE),
        audience=[demo_fixture.ISSUER],
        expires_in=3600,
    )
    trace.append({"hop": 0, "label": "user token issued", "claims": user_claims})

    r1 = service.exchange(
        grant_type=GRANT_TYPE,
        subject_token=user_token,
        subject_token_type=exchange_mod.TOKEN_TYPE_JWT,
        client_id=demo_fixture.PLANNER_BOT,
        scope="read:calendar write:notes",
        audience=demo_fixture.CALENDAR_API,
        requested_ttl=600,
    )
    trace.append({"hop": 1, "label": "planner-bot acts for Asha", "claims": r1.claims})

    # write:notes cannot survive into hop 2: summarizer policy allows only
    # read:calendar, so delegation narrows again at each hop.
    r2 = service.exchange(
        grant_type=GRANT_TYPE,
        subject_token=r1.access_token,
        subject_token_type=exchange_mod.TOKEN_TYPE_JWT,
        client_id=demo_fixture.SUMMARIZER,
        scope="read:calendar",
        audience=demo_fixture.CALENDAR_API,
        requested_ttl=300,
    )
    trace.append({"hop": 2, "label": "summarizer-tool nested delegation", "claims": r2.claims})

    try:
        service.exchange(
            grant_type=GRANT_TYPE,
            subject_token=user_token,
            subject_token_type=exchange_mod.TOKEN_TYPE_JWT,
            client_id=demo_fixture.PLANNER_BOT,
            scope="admin:all",
            audience=demo_fixture.CALENDAR_API,
        )
    except errors.TokenExchangeError as exc:
        trace.append(
            {"hop": 3, "label": "widening attempt denied", "error": exc.error,
             "reason": exc.description}
        )

    # Hop 4: sender-constraining. The token is bound to a key (cnf.jkt);
    # both outcomes below are real verifications run server-side now.
    import json as _json

    from . import dpop

    holder_priv, holder_jwk = dpop.generate_key()
    thief_priv, _ = dpop.generate_key()
    r4 = service.exchange(
        grant_type=GRANT_TYPE,
        subject_token=user_token,
        subject_token_type=exchange_mod.TOKEN_TYPE_JWT,
        client_id=demo_fixture.PLANNER_BOT,
        scope="read:calendar",
        audience=demo_fixture.CALENDAR_API,
        cnf_jwk=_json.dumps(holder_jwk),
    )
    uri = "https://calendar.demo.example/events"
    jkt = r4.claims["cnf"]["jkt"]
    holder_ok = True
    try:
        dpop.verify_proof(
            dpop.make_proof(holder_priv, method="GET", uri=uri,
                            access_token=r4.access_token),
            expected_jkt=jkt, method="GET", uri=uri,
            access_token=r4.access_token,
        )
    except errors.TokenExchangeError:
        holder_ok = False
    thief_rejected = False
    try:
        dpop.verify_proof(
            dpop.make_proof(thief_priv, method="GET", uri=uri,
                            access_token=r4.access_token),
            expected_jkt=jkt, method="GET", uri=uri,
            access_token=r4.access_token,
        )
    except errors.TokenExchangeError:
        thief_rejected = True
    trace.append({
        "hop": 4,
        "label": "key-bound token and a theft attempt",
        "claims": r4.claims,
        "pop": {"jkt": jkt, "holder_accepted": holder_ok,
                "thief_rejected": thief_rejected},
    })

    ok, bad = audit_log.verify()
    return {
        "trace": trace,
        "audit": {"ok": ok, "first_bad_seq": bad, "entries": audit_log.tail(20)},
    }


@app.get("/demo/usertoken", response_class=JSONResponse)
def demo_usertoken():
    """Demo-only convenience: mint a fresh user token for the fictional user.

    A real deployment never exposes this - user tokens come from the sign-in
    flow. It exists so the playground on the demo page can run exchanges.
    """
    from fastapi.responses import PlainTextResponse

    encoded, _ = tokens.issue_token(
        keystore,
        issuer=demo_fixture.ISSUER,
        subject=demo_fixture.USER_ASHA,
        scope=scopes.render(demo_fixture.USER_FULL_SCOPE),
        audience=[demo_fixture.ISSUER],
        expires_in=3600,
    )
    return PlainTextResponse(encoded)


class _Limiter:
    """Per-process sliding window. A courtesy bound for a public demo, not a
    global quota: separate instances each keep their own counts."""

    def __init__(self, limit: int, window: float):
        self.limit, self.window, self.hits = limit, window, {}

    def allow(self, key: str, now: float | None = None) -> bool:
        import time
        now = time.monotonic() if now is None else now
        if len(self.hits) > 2048:
            self.hits.clear()
        recent = [t for t in self.hits.get(key, []) if now - t < self.window]
        ok = len(recent) < self.limit
        if ok:
            recent.append(now)
        self.hits[key] = recent
        return ok


lab_limiter = _Limiter(limit=20, window=60.0)
LAB_MAX_BODY = 4096


@app.post("/demo/lab")
async def demo_lab(request: Request):
    """Run a visitor-designed chain on an isolated, throwaway service."""
    from . import lab

    client = request.client.host if request.client else "?"
    fwd = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    if not lab_limiter.allow(fwd or client):
        return JSONResponse(
            {"error": "rate_limited", "error_description": "demo limit: 20 runs per minute"},
            status_code=429, headers={"Retry-After": "30"})
    raw = await request.body()
    if len(raw) > LAB_MAX_BODY:
        raise errors.invalid_request("body too large")
    import json as _j
    try:
        payload = _j.loads(raw)
    except ValueError:
        raise errors.invalid_request("body must be JSON")
    return lab.run_lab(payload)


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(DEMO_DIR / "index.html")
