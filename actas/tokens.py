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
MAX_TOKEN_BYTES = 16384


def validate_claim_shapes(claims: dict) -> None:
    """Actas token profile. Validate before claims reach policy/SQLite/resources.

    This is stricter than generic JWT: RFCs allow other profiles, but this
    application consumes string scopes, revocable IDs and bounded act chains.
    """
    def text(value, label, maximum=2048):
        if not isinstance(value, str) or not value or len(value) > maximum:
            raise ValueError(f"{label} must be a nonempty bounded string")
        value.encode('utf-8')
    for field in ('iss', 'sub'):
        text(claims.get(field), field)
    for field in ('exp', 'iat'):
        if type(claims.get(field)) is not int:
            raise ValueError(f'{field} must be an integer NumericDate')
    if claims['exp'] <= claims['iat']:
        raise ValueError('exp must be later than iat')
    if 'jti' in claims:
        text(claims['jti'], 'jti')
    if 'scope' in claims:
        scope = claims['scope']
        if not isinstance(scope, str) or len(scope) > 4096:
            raise ValueError('scope must be a bounded string')
        # RFC 6749 scope-token: printable ASCII excluding quote and backslash.
        if scope and (any(not part for part in scope.split(' ')) or any(
                c != ' ' and not (ord(c) == 33 or 35 <= ord(c) <= 91 or 93 <= ord(c) <= 126)
                for c in scope)):
            raise ValueError('scope must use space-delimited OAuth scope tokens')
    if 'aud' in claims:
        audience = claims['aud']
        audiences = audience if isinstance(audience, list) else [audience]
        if not 1 <= len(audiences) <= 32:
            raise ValueError('aud must contain 1-32 audiences')
        for entry in audiences:
            text(entry, 'aud')
    if 'cnf' in claims:
        cnf = claims['cnf']
        if not isinstance(cnf, dict) or set(cnf) != {'jkt'}:
            raise ValueError('cnf must contain a jkt binding')
        import re
        if not isinstance(cnf['jkt'], str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}', cnf['jkt']):
            raise ValueError('cnf.jkt must be a SHA-256 base64url thumbprint')
    if 'act' in claims:
        node, depth = claims['act'], 0
        while True:
            if not isinstance(node, dict) or 'sub' not in node:
                raise ValueError('act must contain a string sub at every hop')
            text(node['sub'], 'act.sub')
            depth += 1
            if depth > 32:
                raise ValueError('act chain exceeds 32 hops')
            if 'act' not in node:
                break
            node = node['act']



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
        if not isinstance(token, str) or not token or len(token.encode('utf-8')) > MAX_TOKEN_BYTES:
            raise ValueError('token size or type invalid')
        header = pyjwt.get_unverified_header(token)
        if header.get('alg') != ALG or not isinstance(header.get('kid'), str) or not 1 <= len(header['kid']) <= 128:
            raise ValueError('unsupported algorithm or key identifier')
    except (pyjwt.InvalidTokenError, UnicodeError, ValueError, TypeError, RecursionError):
        raise invalid_grant("subject token is not a well-formed JWT")
    key_pair = keystore.get(header.get("kid", ""))
    if key_pair is None:
        raise invalid_grant("subject token signed by an unknown key")
    try:
        kwargs = dict(
            key=key_pair.public_key,
            algorithms=[ALG],
            issuer=issuer,
            options={"require": ["exp", "iat", "iss", "sub", "jti", "aud"]},
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
    except (ValueError, TypeError, OverflowError, RecursionError):
        raise invalid_grant('subject token contains malformed claims')
    try:
        validate_claim_shapes(claims)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise invalid_grant('subject token contains malformed claims')
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
