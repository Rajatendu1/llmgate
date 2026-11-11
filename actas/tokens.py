"""JWT issuance and verification (RFC 7519), RS256 only.

The delegation chain lives in the `act` claim (RFC 8693 section 4.1):
each exchange nests the acting party one level deeper, so a token carries
its full chain of custody: user <- agent <- sub-agent.
"""

from __future__ import annotations

import time
import uuid

import jwt as pyjwt

from .errors import invalid_grant
from .keys import KeyStore

ALG = "RS256"


def issue_token(
    keystore: KeyStore,
    *,
    issuer: str,
    subject: str,
    scope: str,
    audience: list[str],
    expires_in: int,
    act: dict | None = None,
    client_id: str | None = None,
    extra: dict | None = None,
    now: float | None = None,
) -> tuple[str, dict]:
    """Issue a signed JWT. Returns (encoded, claims)."""
    now = time.time() if now is None else now
    claims = {
        "iss": issuer,
        "sub": subject,
        "aud": audience if len(audience) != 1 else audience[0],
        "iat": int(now),
        "exp": int(now) + int(expires_in),
        "jti": str(uuid.uuid4()),
        "scope": scope,
    }
    if act is not None:
        claims["act"] = act
    if client_id is not None:
        claims["client_id"] = client_id
    if extra:
        claims.update(extra)
    encoded = pyjwt.encode(
        claims,
        keystore.current.private_pem(),
        algorithm=ALG,
        headers={"kid": keystore.current.kid},
    )
    return encoded, claims


def verify_token(
    keystore: KeyStore,
    token: str,
    *,
    issuer: str,
    audience: str | None = None,
    now: float | None = None,
) -> dict:
    """Verify signature, expiry, issuer and (optionally) audience.

    Raises TokenExchangeError(invalid_grant) on any failure, per RFC 8693
    section 2.2.2's guidance for invalid subject tokens.
    """
    try:
        header = pyjwt.get_unverified_header(token)
    except pyjwt.DecodeError:
        raise invalid_grant("subject token is not a well-formed JWT")
    key_pair = keystore.get(header.get("kid", ""))
    if key_pair is None:
        raise invalid_grant("subject token signed by an unknown key")
    try:
        kwargs = dict(
            key=key_pair.public_key,
            algorithms=[ALG],
            issuer=issuer,
            options={"require": ["exp", "iat", "iss", "sub"]},
        )
        if audience is not None:
            kwargs["audience"] = audience
        else:
            kwargs["options"]["verify_aud"] = False
        claims = pyjwt.decode(token, **kwargs)
    except pyjwt.ExpiredSignatureError:
        raise invalid_grant("subject token is expired")
    except pyjwt.InvalidAudienceError:
        raise invalid_grant("subject token audience does not match")
    except pyjwt.InvalidIssuerError:
        raise invalid_grant("subject token issuer does not match")
    except pyjwt.MissingRequiredClaimError as exc:
        raise invalid_grant(f"subject token is missing claim: {exc.claim}")
    except pyjwt.InvalidTokenError as exc:
        raise invalid_grant(f"subject token failed verification: {exc}")
    if now is not None and claims["exp"] <= now:
        raise invalid_grant("subject token is expired")
    return claims


def act_depth(act: dict | None) -> int:
    """Depth of a delegation chain encoded in nested act claims."""
    depth = 0
    node = act
    while isinstance(node, dict) and "sub" in node:
        depth += 1
        node = node.get("act")
    return depth


def act_chain(act: dict | None) -> list[str]:
    """Ordered list of actors, outermost (most recent) last."""
    chain: list[str] = []
    node = act
    while isinstance(node, dict) and "sub" in node:
        chain.append(node["sub"])
        node = node.get("act")
    chain.reverse()
    return chain
