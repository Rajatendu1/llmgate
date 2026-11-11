"""RSA key management: generation, rotation, and JWKS publication (RFC 7517)."""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


def _b64url_uint(n: int) -> str:
    import base64

    raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


@dataclass
class KeyPair:
    kid: str
    private_key: object
    public_key: object
    created_at: float = field(default_factory=time.time)

    def private_pem(self) -> str:
        return self.private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode("ascii")

    def jwk(self) -> dict:
        numbers = self.public_key.public_numbers()
        return {
            "kty": "RSA",
            "use": "sig",
            "alg": "RS256",
            "kid": self.kid,
            "n": _b64url_uint(numbers.n),
            "e": _b64url_uint(numbers.e),
        }


class KeyStore:
    """Holds current and retired signing keys.

    Rotation keeps old public keys in the JWKS document so already-issued
    tokens remain verifiable until they expire, while new tokens are signed
    with the current key. This mirrors how real JWKS endpoints roll keys.
    """

    def __init__(self, key_size: int = 2048, persist=None):
        self._key_size = key_size
        self._keys: dict[str, KeyPair] = {}
        self._current_kid: str | None = None
        self._persist = persist
        loaded = persist.load_keys() if persist else []
        for kid, priv, created, is_current in loaded:
            self._keys[kid] = KeyPair(kid=kid, private_key=priv,
                                      public_key=priv.public_key(), created_at=created)
            if is_current:
                self._current_kid = kid
        if self._current_kid is None:
            self.rotate()

    def _generate(self) -> KeyPair:
        private_key = rsa.generate_private_key(public_exponent=65537, key_size=self._key_size)
        public_key = private_key.public_key()
        pub_der = public_key.public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        kid = hashlib.sha256(pub_der).hexdigest()[:16]
        return KeyPair(kid=kid, private_key=private_key, public_key=public_key)

    def rotate(self) -> str:
        pair = self._generate()
        self._keys[pair.kid] = pair
        self._current_kid = pair.kid
        if self._persist:
            self._persist.save_key(pair, current=True)
        return pair.kid

    def retire_older_than(self, seconds: float, now: float | None = None) -> list[str]:
        """Drop retired keys older than `seconds`. The current key never retires.

        Only safe once every token signed by the retired key has expired:
        callers should pick a window at least the longest token TTL in use.
        """
        now = time.time() if now is None else now
        doomed = [kid for kid, kp in self._keys.items()
                  if kid != self._current_kid and now - kp.created_at > seconds]
        for kid in doomed:
            del self._keys[kid]
            if self._persist:
                self._persist.delete_key(kid)
        return doomed

    @property
    def current(self) -> KeyPair:
        return self._keys[self._current_kid]

    def get(self, kid: str) -> KeyPair | None:
        return self._keys.get(kid)

    def jwks(self) -> dict:
        # Publish public keys only, newest last for stable ordering.
        return {"keys": [kp.jwk() for kp in self._keys.values()]}

    def kid_count(self) -> int:
        return len(self._keys)
