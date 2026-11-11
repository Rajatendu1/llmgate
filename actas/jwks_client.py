"""A JWKS client for resource servers that verify tokens remotely.

Fetches the issuer's JWKS document over HTTP and verifies RS256 JWTs with
no shared state: the resource server never sees a private key, only the
public keys the issuer publishes. Keys are cached with a short TTL and
refreshed on unknown kid, which is how real JWKS clients ride key rotation.
"""

from __future__ import annotations

import time

import httpx
import jwt as pyjwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


class JwksClient:
    def __init__(self, jwks_url: str, *, cache_ttl: int = 300):
        self.jwks_url = jwks_url
        self.cache_ttl = cache_ttl
        self._keys: dict[str, object] = {}
        self._fetched_at = 0.0

    @staticmethod
    def _jwk_to_public_key(jwk: dict):
        def b64u(s: str) -> int:
            import base64

            pad = "=" * (-len(s) % 4)
            return int.from_bytes(base64.urlsafe_b64decode(s + pad), "big")

        numbers = rsa.RSAPublicNumbers(e=b64u(jwk["e"]), n=b64u(jwk["n"]))
        return numbers.public_key()

    def _refresh(self) -> None:
        doc = httpx.get(self.jwks_url, timeout=5).json()
        self._keys = {
            k["kid"]: self._jwk_to_public_key(k)
            for k in doc.get("keys", [])
            if k.get("kty") == "RSA"
        }
        self._fetched_at = time.time()

    def public_key_for(self, kid: str):
        stale = time.time() - self._fetched_at > self.cache_ttl
        if stale or not self._keys:
            self._refresh()
        key = self._keys.get(kid)
        if key is None:
            # unknown kid: maybe the issuer rotated; one forced refetch
            self._refresh()
            key = self._keys.get(kid)
        return key

    def verify(self, token: str, *, issuer: str, audience: str) -> dict:
        header = pyjwt.get_unverified_header(token)
        key = self.public_key_for(header.get("kid", ""))
        if key is None:
            raise pyjwt.InvalidTokenError("unknown signing key")
        return pyjwt.decode(
            token,
            key=key.public_bytes(
                serialization.Encoding.PEM,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            ),
            algorithms=["RS256"],
            issuer=issuer,
            audience=audience,
            options={"require": ["exp", "iat", "iss", "sub"]},
        )
