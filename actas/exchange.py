"""OAuth 2.0 Token Exchange (RFC 8693), delegation semantics.

Implements the exchange endpoint logic:

  POST /oauth/token
  grant_type=urn:ietf:params:oauth:grant-type:token-exchange
  subject_token=<jwt>&subject_token_type=urn:ietf:params:oauth:token-type:jwt
  [&actor_token=<jwt>&actor_token_type=...]   delegation (act claim)
  [&client_id=<agent>]                        agent acting on its own behalf
  &scope=<narrower scope>&audience=<resource>

Invariants enforced on every exchange:
  1. The subject token must verify (signature, issuer, expiry).
  2. The resulting scope must be a subset of the subject token's scope
     AND of the acting client's policy. Widening is rejected.
  3. The delegation chain (act claims) cannot exceed the client's depth limit.
  4. The new token never outlives the subject token.
  5. Every attempt, granted or denied, is written to the audit log.
"""

from __future__ import annotations

from dataclasses import dataclass

import jwt as pyjwt

from . import errors, scopes, tokens
from .audit import AuditLog
from .keys import KeyStore
from .policy import PolicyEngine

GRANT_TYPE = "urn:ietf:params:oauth:grant-type:token-exchange"
TOKEN_TYPE_JWT = "urn:ietf:params:oauth:token-type:jwt"
ISSUED_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:jwt"
DEFAULT_TTL = 600


@dataclass
class ExchangeResult:
    access_token: str
    claims: dict
    expires_in: int
    scope: str

    def as_response(self) -> dict:
        return {
            "access_token": self.access_token,
            "issued_token_type": ISSUED_TOKEN_TYPE,
            "token_type": "Bearer",
            "expires_in": self.expires_in,
            "scope": self.scope,
        }


class TokenExchangeService:
    def __init__(
        self,
        *,
        keystore: KeyStore,
        policy: PolicyEngine,
        audit: AuditLog,
        issuer: str,
        registry=None,
    ):
        from .registry import TokenRegistry

        self.registry = registry or TokenRegistry()
        self.keystore = keystore
        self.policy = policy
        self.audit = audit
        self.issuer = issuer

    def exchange(
        self,
        *,
        grant_type: str | None,
        subject_token: str | None,
        subject_token_type: str | None,
        actor_token: str | None = None,
        actor_token_type: str | None = None,
        client_id: str | None = None,
        scope: str | None = None,
        audience: str | None = None,
        requested_ttl: int | None = None,
        cnf_jwk: str | None = None,
        authenticated_client_id: str | None = None,
        verified_dpop_jkt: str | None = None,
    ) -> ExchangeResult:
        try:
            return self._exchange(
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
                authenticated_client_id=authenticated_client_id,
                verified_dpop_jkt=verified_dpop_jkt,
            )
        except errors.TokenExchangeError as exc:
            self.audit.append(
                "exchange.denied",
                {
                    "client_id": client_id,
                    "error": exc.error,
                    "reason": exc.description,
                    "requested_scope": scope,
                    "audience": audience,
                },
            )
            raise

    def _exchange(self, *, grant_type, subject_token, subject_token_type,
                  actor_token, actor_token_type, client_id, scope, audience,
                  requested_ttl, cnf_jwk, authenticated_client_id, verified_dpop_jkt) -> ExchangeResult:
        # --- request shape (RFC 8693 section 2.1) ---
        if grant_type != GRANT_TYPE:
            raise errors.invalid_request(f"unsupported grant_type: {grant_type!r}")
        if not subject_token:
            raise errors.invalid_request("subject_token is required")
        if subject_token_type != TOKEN_TYPE_JWT:
            raise errors.invalid_request(
                f"unsupported subject_token_type: {subject_token_type!r}"
            )
        if not audience:
            raise errors.invalid_request("audience is required")
        if actor_token is not None and actor_token_type != TOKEN_TYPE_JWT:
            raise errors.invalid_request(
                f"unsupported actor_token_type: {actor_token_type!r}"
            )
        if actor_token is None and not client_id:
            raise errors.invalid_request(
                "delegation requires an actor: client_id or actor_token"
            )

        # --- verify subject token ---
        subject_claims = tokens.verify_token(
            self.keystore, subject_token, issuer=self.issuer
        )
        if self.registry.is_revoked(subject_claims.get("jti")):
            raise errors.invalid_grant("subject token has been revoked")
        subject_binding = subject_claims.get('cnf', {}).get('jkt')
        if subject_binding and verified_dpop_jkt != subject_binding:
            raise errors.invalid_grant('sender-bound subject token requires proof from its holder key')
        subject_scope = scopes.parse(subject_claims.get("scope"))

        # --- resolve the actor ---
        # The actor token proves who is acting; the chain that grows is the
        # subject token's. new act = {sub: actor, act: <subject's act>}.
        if actor_token is not None:
            actor_claims = tokens.verify_token(
                self.keystore, actor_token, issuer=self.issuer
            )
            if self.registry.is_revoked(actor_claims.get("jti")):
                raise errors.invalid_grant("actor token has been revoked")
            actor_binding = actor_claims.get('cnf', {}).get('jkt')
            if actor_binding and verified_dpop_jkt != actor_binding:
                raise errors.invalid_grant('sender-bound actor token requires proof from its holder key')
            actor_id = actor_claims["sub"]
        else:
            actor_id = client_id
        if authenticated_client_id is not None and actor_id != authenticated_client_id:
            raise errors.unauthorized_client('actor must match the authenticated client')
        existing_act = subject_claims.get("act")
        new_act = {"sub": actor_id}
        if existing_act is not None:
            new_act = {"sub": actor_id, "act": existing_act}

        depth = tokens.act_depth(new_act)

        # --- scope: default keeps the subject scope; requested must narrow ---
        requested = scopes.parse(scope) if scope else subject_scope
        # TTL: an explicit request above the client's policy limit is denied.
        # When the client does not ask for a lifetime, clamp the default to
        # the client's limit instead of failing a request it never made.
        if requested_ttl is not None:
            ttl = requested_ttl
        else:
            client_policy = self.policy.get(actor_id)
            ttl = DEFAULT_TTL
            if client_policy is not None:
                ttl = min(ttl, client_policy.max_token_ttl)

        decision = self.policy.decide(
            client_id=actor_id,
            requested_scope=requested,
            audience=audience,
            subject_scope=subject_scope,
            delegation_depth=depth,
            requested_ttl=ttl,
        )
        if not decision.allowed:
            reason = decision.reason
            if "scope" in reason:
                raise errors.invalid_scope(reason)
            if "audience" in reason:
                raise errors.invalid_target(reason)
            if "unknown client" in reason:
                raise errors.unauthorized_client(reason)
            raise errors.invalid_request(reason)

        # --- optional proof-of-possession binding (RFC 7800 cnf.jkt) ---
        extra = None
        if cnf_jwk is not None:
            import json

            from . import dpop

            try:
                if len(cnf_jwk) > 4096:
                    raise ValueError('binding key exceeds 4096 characters')
                jwk = json.loads(cnf_jwk)
                if not isinstance(jwk, dict) or jwk.get("kty") != "EC" or jwk.get("crv") != "P-256":
                    raise ValueError("only EC P-256 keys are supported")
                if 'd' in jwk:
                    raise ValueError('public keys only; private key material must not be sent')
                # Check coordinates form a real curve point, not merely hashable JSON.
                from cryptography.hazmat.primitives.asymmetric import ec
                key = pyjwt.algorithms.ECAlgorithm.from_jwk(json.dumps(jwk))
                if not isinstance(key, ec.EllipticCurvePublicKey) or key.curve.name != 'secp256r1':
                    raise ValueError('invalid public P-256 key')
                extra = {"cnf": {"jkt": dpop.thumbprint(jwk)}}
            except (ValueError, KeyError, TypeError, pyjwt.PyJWTError) as exc:
                raise errors.invalid_request(f"invalid cnf_jwk: {exc}")

        if subject_binding:
            if extra is None:
                extra = {'cnf': {'jkt': subject_binding}}
            elif extra['cnf']['jkt'] != subject_binding:
                raise errors.invalid_request('sender-bound subject cannot be rebound to a different key')

        # --- expiry: never outlive the subject token ---
        import time

        now = time.time()
        subject_exp = subject_claims["exp"]
        effective_ttl = int(min(ttl, max(0, subject_exp - now)))
        if effective_ttl <= 0:
            raise errors.invalid_grant("subject token has no remaining lifetime")

        encoded, claims = tokens.issue_token(
            self.keystore,
            issuer=self.issuer,
            subject=subject_claims["sub"],
            scope=scopes.render(decision.granted_scope),
            audience=[audience],
            expires_in=effective_ttl,
            act=new_act,
            client_id=actor_id,
            extra=extra,
        )

        self.registry.record(
            jti=claims["jti"], parent_jti=subject_claims.get("jti"),
            sub=subject_claims["sub"], actor=actor_id,
            scope=scopes.render(decision.granted_scope), exp=claims["exp"])
        self.audit.append(
            "exchange.granted",
            {
                "subject": subject_claims["sub"],
                "actor": actor_id,
                "delegation_depth": depth,
                "chain": tokens.act_chain(new_act),
                "granted_scope": scopes.render(decision.granted_scope),
                "audience": audience,
                "expires_in": effective_ttl,
                "jti": claims["jti"],
                "sender_constrained": extra is not None,
            },
        )
        return ExchangeResult(
            access_token=encoded,
            claims=claims,
            expires_in=effective_ttl,
            scope=scopes.render(decision.granted_scope),
        )
