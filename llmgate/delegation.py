"""/demo/delegate: one self-contained, end-to-end delegation exchange.

One POST issues a root user token, exchanges it down to a parent agent and
then a narrower sub-agent (RFC 8693, via the vendored Actas 0.14.1
TokenExchangeService), then runs the real consequences through this very app
instance over ASGI: the sub-agent reaches a cheap alias and is REFUSED an
expensive alias that its static key peers can reach; a scope-widening
exchange is denied; the tamper-evident audit chain is verified.

Honest labels: the issuer is embedded in this process (one trust domain).
Demo tokens are explicitly unbound bearer tokens; cnf-bound tokens are
rejected at verification (no proof channel exists here). Every request in
the flow is issued inside this call: cold starts rotate the embedded signing
key, which changes nothing for this flow because it is self-contained per
request. No claim of production authorization.
"""
from __future__ import annotations

import asyncio

import httpx

from actas.audit import AuditLog
from actas.errors import TokenExchangeError
from actas.exchange import GRANT_TYPE, TOKEN_TYPE_JWT, TokenExchangeService
from actas.keys import KeyStore
from actas.policy import AgentPolicy, PolicyEngine
from actas.registry import TokenRegistry
from actas.tokens import issue_token


class EmbeddedDemo:
    """Embedded issuer + policy for the demo flow. One per process; the
    flow is self-contained per request, so key reuse across warm requests is
    fine and cold-start key rotation is irrelevant to it."""

    def __init__(self, issuer: str, audience: str, keystore: KeyStore,
                 audit_cap: int = 512):
        self.issuer = issuer
        self.audience = audience
        # Shared with the app's verifier: one embedded trust domain. Tokens
        # minted here must verify there.
        self.keystore = keystore
        # Warm processes would otherwise accumulate one chain (and one token
        # registry) per request forever. Cap them: before a run that would
        # pass the cap, start a fresh chain with a log.rotated marker and a
        # fresh registry. Response payloads already tail-bounded; this bounds
        # the retained memory too.
        self.audit_cap = audit_cap
        policy = PolicyEngine({p.client_id: p for p in [
            AgentPolicy(client_id="parent-agent",
                        allowed_scopes=frozenset({"llm:complete", "llm:expensive-models"}),
                        allowed_audiences=frozenset({audience}),
                        max_delegation_depth=2, max_token_ttl=900),
            AgentPolicy(client_id="sub-agent",
                        allowed_scopes=frozenset({"llm:complete"}),
                        allowed_audiences=frozenset({audience}),
                        max_delegation_depth=2, max_token_ttl=300),
        ]})
        self._policy = policy
        self._reset_audit()
        self._lock = asyncio.Lock()

    def _reset_audit(self):
        self.audit = AuditLog()
        self.service = TokenExchangeService(
            keystore=self.keystore, policy=self._policy, audit=self.audit,
            issuer=self.issuer, registry=TokenRegistry())

    async def run(self, cfg, cheap_model: str, expensive_model: str, app) -> dict:
        """Run the full flow against the live app over ASGI. cfg is a
        tokenauth.TokenAuthConfig; kept as a duck-typed parameter to avoid an
        import cycle (tokenauth imports app)."""
        async with self._lock:  # serialise: the audit log is append-only, keep it linear
            if len(self.audit) >= self.audit_cap:
                self._reset_audit()
                self.audit.append("log.rotated", {
                    "reason": "bounded memory in a warm process",
                    "cap": self.audit_cap})
            root, _ = issue_token(
                self.keystore, issuer=self.issuer, subject="demo-user",
                scope="llm:complete llm:expensive-models",
                audience=[self.issuer], expires_in=600)
            parent = self.service.exchange(
                grant_type=GRANT_TYPE, subject_token=root,
                subject_token_type=TOKEN_TYPE_JWT, client_id="parent-agent",
                scope="llm:complete llm:expensive-models", audience=self.audience)
            sub = self.service.exchange(
                grant_type=GRANT_TYPE, subject_token=parent.access_token,
                subject_token_type=TOKEN_TYPE_JWT, client_id="sub-agent",
                scope="llm:complete", audience=self.audience)

            steps = []

            async def call(name: str, token: str, model: str):
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(transport=transport, base_url="http://embedded") as c:
                    r = await c.post(
                        "/v1/chat/completions",
                        headers={"Authorization": f"Bearer {token}"},
                        json={"model": model,
                              "messages": [{"role": "user", "content": "ping"}],
                              "max_tokens": 1})
                if name == "sub-cheap":
                    ok = r.status_code == 200
                    detail = f"sub-agent -> {model}: HTTP {r.status_code} (expected 200)"
                elif name == "sub-expensive":
                    ok = r.status_code == 403 and "insufficient_scope" in r.text
                    detail = f"sub-agent -> {model}: HTTP {r.status_code} (expected 403 insufficient_scope)"
                else:  # parent-expensive
                    ok = r.status_code == 200
                    detail = f"parent-agent -> {model}: HTTP {r.status_code} (expected 200)"
                steps.append({"name": name, "status": "pass" if ok else "fail",
                              "detail": detail})
                return ok

            ok1 = await call("sub-cheap", sub.access_token, cheap_model)
            ok2 = await call("sub-expensive", sub.access_token, expensive_model)
            ok3 = await call("parent-expensive", parent.access_token, expensive_model)

            widening_status = None
            try:
                self.service.exchange(
                    grant_type=GRANT_TYPE, subject_token=parent.access_token,
                    subject_token_type=TOKEN_TYPE_JWT, client_id="sub-agent",
                    scope="llm:complete llm:expensive-models", audience=self.audience)
            except TokenExchangeError as e:  # deny, don't narrow
                widening_status = e.error
            ok4 = widening_status == "invalid_scope"
            steps.append({"name": "widening-denied",
                          "status": "pass" if ok4 else "fail",
                          "detail": f"scope-widening exchange rejected as {widening_status}"})

            claims = cfg.verifier.verify(sub.access_token)
            chain_ok, _ = self.audit.verify()
            labels = [
                "issuer is embedded in this process (one trust domain); no remote issuer or JWKS involved",
                "demo tokens are explicitly unbound bearer tokens; cnf-bound tokens are rejected (proof not verified here)",
                "every request in this flow is issued inside this call; cold-start key rotation is irrelevant to it",
                "budgets for token identities come from server configuration, never from claims",
                "this demonstrates a narrower envelope than configured static keys; it is not production authorization",
            ]
            return {"ok": all([ok1, ok2, ok3, ok4, chain_ok]),
                    "sub_agent_identity": f"jwt:{claims.chain[-1]}" if claims.chain else f"jwt:{claims.sub}",
                    "delegation_chain": list(claims.chain),
                    "sub_agent_scopes": sorted(claims.scope),
                    "allowed_models": sorted(cfg.allowed_models(claims.scope)),
                    "audit_chain_verifies": chain_ok,
                    "steps": steps,
                    "audit": self.audit.tail(16),
                    "labels": labels}


_demos: dict[int, EmbeddedDemo] = {}


def install_delegate_demo(app, cfg, cheap_model: str, expensive_model: str, keystore: KeyStore):
    demo = _demos.setdefault(
        id(app), EmbeddedDemo(cfg.verifier.issuer, cfg.verifier.audience, keystore))

    @app.post("/demo/delegate")
    async def delegate_demo():
        return await demo.run(cfg, cheap_model, expensive_model, app)
