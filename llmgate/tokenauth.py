"""Scoped bearer-token auth: a second, additive credential path.

Static API keys (app.py) are unchanged and stay the primary path. This module
lets an issued token carry a NARROWER envelope than any configured key: which
public model aliases the bearer may reach, enforced at request time.

The issuer is EMBEDDED in this process (the vendored Actas 0.14.1
TokenExchangeService, see vendor/ACTAS.md): llmgate issues and verifies in
one trust domain. Nothing here is cross-service authorization, and no remote
issuer is involved or claimed.

Token rules enforced here:
- RS256 signature, issuer, audience and expiry are verified by
  actas.tokens.verify_token (algorithm pinned to RS256 inside actas, and the
  expected issuer/audience pinned here in server configuration).
- The token must carry the required scope (default `llm:complete`).
- A token carrying `cnf` (proof-of-possession binding) is REJECTED: bound
  tokens require a matching proof this phase does not verify, and silently
  treating one as bearer would be a downgrade. Demo tokens are explicitly
  unbound.
- Claims never set their own budgets or widen the configured scope->models
  mapping; a token identity's budgets come from server configuration.
"""
from __future__ import annotations

from dataclasses import dataclass

from actas import tokens as actas_tokens
from actas.errors import TokenExchangeError
from actas.keys import KeyStore


class TokenError(Exception):
    """RFC 6749-style failure: .error is the code, str() the description."""

    def __init__(self, error: str, description: str):
        super().__init__(description)
        self.error = error


@dataclass(frozen=True)
class Claims:
    iss: str
    sub: str
    aud: tuple[str, ...]
    exp: float
    scope: frozenset
    chain: tuple[str, ...] = ()  # delegation chain, outermost first (RFC 8693 act)
    jti: str = ""


class ActasVerifier:
    """Verifies embedded-issuer tokens against the in-process KeyStore."""

    def __init__(self, keystore: KeyStore, issuer: str, audience: str):
        self.keystore = keystore
        self.issuer = issuer
        self.audience = audience

    def verify(self, token: str) -> Claims:
        try:
            raw = actas_tokens.verify_token(
                self.keystore, token, issuer=self.issuer, audience=self.audience)
        except TokenExchangeError as e:
            raise TokenError("invalid_token", e.description)
        if "cnf" in raw:
            raise TokenError(
                "invalid_token",
                "proof-of-possession-bound tokens need a matching proof, "
                "which this deployment does not verify")
        aud = raw.get("aud")
        aud = [aud] if isinstance(aud, str) else list(aud or [])
        return Claims(iss=str(raw.get("iss", "")), sub=str(raw.get("sub", ""))[:64],
                      aud=tuple(aud), exp=float(raw.get("exp", 0)),
                      scope=frozenset(str(raw.get("scope", "")).split()),
                      chain=tuple(actas_tokens.act_chain(raw.get("act"))),
                      jti=str(raw.get("jti", "")))


@dataclass(frozen=True)
class TokenAuthConfig:
    """Server-side policy for token identities. Claims never set their own
    budgets or widen this mapping; they can only land inside it."""
    verifier: ActasVerifier
    scope_models: dict  # scope string -> list of public model aliases it may reach
    required_scope: str = "llm:complete"
    rate: float = 2.0
    burst: float = 4.0
    token_rate: float | None = None
    token_burst: float | None = None

    def allowed_models(self, scope: frozenset) -> frozenset:
        out: set = set()
        for s in scope:
            out |= set(self.scope_models.get(s, ()))
        return frozenset(out)


def claims_to_apikey(claims: Claims, cfg: TokenAuthConfig):
    """Map verified claims to the gateway's ApiKey shape (app.py). Kept here
    to avoid a circular import; app.py calls this with its own ApiKey class.
    The identity name is the deepest actor of the delegation chain when one
    exists, else the subject."""
    from .app import ApiKey  # local import: app imports this module
    actor = claims.chain[-1] if claims.chain else claims.sub
    return ApiKey(name=f"jwt:{actor}", rate=cfg.rate, burst=cfg.burst,
                  token_rate=cfg.token_rate, token_burst=cfg.token_burst,
                  models=cfg.allowed_models(claims.scope))
