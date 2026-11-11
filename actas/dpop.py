"""Proof-of-possession: binding tokens to a client key (a DPoP subset).

Bearer tokens are stealable. A sender-constrained token carries a
confirmation claim (cnf.jkt, RFC 7800) holding the thumbprint of the
client's public key. The client then proves possession on every resource
call with a signed proof JWT (RFC 9449 style) carrying:

  htm - the HTTP method it is used with
  htu - the exact URI it is used on
  iat - freshness (rejected outside a short window)
  jti - replay identifier
  ath - SHA-256 of the access token it accompanies
  nonce - optional server-issued challenge (RFC 9449 section 8 subset)

This module implements that subset: EC P-256 keys, ES256 proofs, jkt
thumbprints per RFC 7638, a single-use jti replay cache (in-memory or
durable SQLite), and opt-in single-use server nonces with the spec's
use_dpop_nonce challenge. It is deliberately small; the honest limits in
the README say what is not covered.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
import uuid

import jwt as pyjwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from .errors import TokenExchangeError

PROOF_MAX_AGE = 60  # seconds
NONCE_MAX_AGE = 120  # seconds


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def generate_key() -> tuple[ec.EllipticCurvePrivateKey, dict]:
    """Generate a P-256 key pair; returns (private_key, public_jwk)."""
    private_key = ec.generate_private_key(ec.SECP256R1())
    numbers = private_key.public_key().public_numbers()
    jwk = {
        "kty": "EC",
        "crv": "P-256",
        "x": _b64url(numbers.x.to_bytes(32, "big")),
        "y": _b64url(numbers.y.to_bytes(32, "big")),
    }
    return private_key, jwk


def thumbprint(jwk: dict) -> str:
    """RFC 7638 JWK SHA-256 thumbprint (the cnf.jkt value)."""
    canonical = json.dumps(
        {"crv": jwk["crv"], "kty": jwk["kty"], "x": jwk["x"], "y": jwk["y"]},
        sort_keys=True, separators=(",", ":"),
    )
    return _b64url(hashlib.sha256(canonical.encode()).digest())


def public_jwk_of(private_key: ec.EllipticCurvePrivateKey) -> dict:
    numbers = private_key.public_key().public_numbers()
    return {
        "kty": "EC",
        "crv": "P-256",
        "x": _b64url(numbers.x.to_bytes(32, "big")),
        "y": _b64url(numbers.y.to_bytes(32, "big")),
    }


def proof_jkt(proof: str) -> tuple[str, dict]:
    """Return (thumbprint, public jwk) from a proof's unverified header.

    Used when a proof arrives without a prior binding to check against - for
    example on the token endpoint, where the proof key becomes the binding.
    The caller must still verify the proof signature with verify_proof.
    """
    try:
        header = pyjwt.get_unverified_header(proof)
        jwk = header.get("jwk")
        if not jwk or jwk.get("kty") != "EC" or "x" not in jwk or "y" not in jwk:
            raise ValueError("proof header must carry a public EC jwk")
        return thumbprint(jwk), jwk
    except TokenExchangeError:
        raise
    except Exception as exc:
        raise TokenExchangeError("invalid_request", f"malformed DPoP proof: {exc}", 400)


def make_proof(
    private_key: ec.EllipticCurvePrivateKey,
    *,
    method: str,
    uri: str,
    access_token: str | None = None,
    nonce: str | None = None,
    now: float | None = None,
) -> str:
    claims = {
        "htu": uri,
        "htm": method.upper(),
        "iat": int(time.time() if now is None else now),
        "jti": str(uuid.uuid4()),
    }
    if access_token is not None:
        claims["ath"] = _b64url(hashlib.sha256(access_token.encode()).digest())
    if nonce is not None:
        claims["nonce"] = nonce
    return pyjwt.encode(
        claims,
        private_key,
        algorithm="ES256",
        headers={"jwk": public_jwk_of(private_key), "typ": "dpop+jwt"},
    )


class ReplayCache:
    """Remembers (key thumbprint, proof jti) so one proof works once.

    Entries live 2 x PROOF_MAX_AGE (a proof older than that is rejected as stale
    anyway). Bounded: when full of live entries it refuses new proofs rather
    than forgetting old ones, so an attacker cannot flush the cache to replay.

    Honest limit: per process. Several instances need a shared store (for
    example Redis SET NX with expiry) for the guarantee to hold across them;
    DurableReplayCache extends it across restarts on one instance.
    """

    def __init__(self, max_entries: int = 10_000):
        import threading
        self.max_entries = max_entries
        self._seen: dict[tuple[str, str], float] = {}
        self._lock = threading.Lock()

    def check_and_add(self, jkt: str, jti: str, now: float) -> bool:
        """True if this proof is new (and now recorded); False if replayed."""
        with self._lock:
            key = (jkt, jti)
            cutoff = now - 2 * PROOF_MAX_AGE
            if key in self._seen and self._seen[key] > cutoff:
                return False
            if len(self._seen) >= self.max_entries:
                self._seen = {k: t for k, t in self._seen.items() if t > cutoff}
                if len(self._seen) >= self.max_entries:
                    raise TokenExchangeError(
                        "temporarily_unavailable", "proof replay cache is full", 503)
            self._seen[key] = now
            return True


class DurableReplayCache:
    """ReplayCache backed by SQLite, so single-use survives a restart.

    Same contract as ReplayCache: live entries are (jkt, jti) pairs younger
    than 2 x PROOF_MAX_AGE, and when full of live entries it refuses new
    proofs rather than forgetting old ones. Shares the server's database
    file. Honest limit: one database means one writer, so this extends the
    guarantee across restarts, not across parallel instances.
    """

    def __init__(self, conn, max_entries: int = 10_000):
        import sqlite3  # noqa: F401
        import threading
        self.max_entries = max_entries
        self._conn = conn
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS replay "
                "(jkt TEXT, jti TEXT, seen_at REAL, PRIMARY KEY (jkt, jti))")
            self._conn.commit()

    def check_and_add(self, jkt: str, jti: str, now: float) -> bool:
        """True if this proof is new (and now recorded); False if replayed."""
        cutoff = now - 2 * PROOF_MAX_AGE
        with self._lock:
            row = self._conn.execute(
                "SELECT seen_at FROM replay WHERE jkt=? AND jti=?", (jkt, jti)).fetchone()
            if row and row[0] > cutoff:
                return False
            live = self._conn.execute(
                "SELECT COUNT(*) FROM replay WHERE seen_at > ?", (cutoff,)).fetchone()[0]
            if live >= self.max_entries:
                self._conn.execute("DELETE FROM replay WHERE seen_at <= ?", (cutoff,))
                live = self._conn.execute(
                    "SELECT COUNT(*) FROM replay WHERE seen_at > ?", (cutoff,)).fetchone()[0]
                if live >= self.max_entries:
                    self._conn.commit()
                    raise TokenExchangeError(
                        "temporarily_unavailable", "proof replay cache is full", 503)
            self._conn.execute("INSERT OR REPLACE INTO replay VALUES (?,?,?)",
                               (jkt, jti, now))
            self._conn.commit()
            return True


class NonceStore:
    """Server-issued single-use nonces (RFC 9449 section 8 subset).

    A resource server can demand that every proof carries a fresh nonce it
    issued. A missing or stale nonce is rejected with `use_dpop_nonce` and a
    fresh nonce in the DPoP-Nonce header, and the client retries - the
    spec's challenge flow. Each nonce works once and lives NONCE_MAX_AGE
    seconds. Bounded like the replay cache: full of live entries, it refuses
    to issue rather than forgetting.
    """

    def __init__(self, max_entries: int = 10_000):
        import threading
        self.max_entries = max_entries
        self._live: dict[str, float] = {}
        self._lock = threading.Lock()

    def issue(self, now: float | None = None) -> str:
        now = time.time() if now is None else now
        with self._lock:
            if len(self._live) >= self.max_entries:
                cutoff = now - NONCE_MAX_AGE
                self._live = {n: t for n, t in self._live.items() if t > cutoff}
                if len(self._live) >= self.max_entries:
                    raise TokenExchangeError(
                        "temporarily_unavailable", "nonce store is full", 503)
            nonce = _b64url(uuid.uuid4().bytes)
            self._live[nonce] = now
            return nonce

    def verify_and_consume(self, nonce: str, now: float | None = None) -> bool:
        """True if the nonce is live and fresh; consumes it either way."""
        now = time.time() if now is None else now
        with self._lock:
            issued = self._live.pop(nonce, None)
        return issued is not None and now - issued <= NONCE_MAX_AGE


def verify_proof(
    proof: str,
    *,
    expected_jkt: str,
    method: str,
    uri: str,
    access_token: str | None = None,
    now: float | None = None,
    replay: "ReplayCache | DurableReplayCache | None" = None,
    nonce_store: NonceStore | None = None,
) -> dict:
    """Verify a proof JWT against the token's cnf.jkt and the request.

    Raises TokenExchangeError on any mismatch.
    """
    now = time.time() if now is None else now

    def deny(reason: str):
        raise TokenExchangeError("invalid_token", f"proof of possession failed: {reason}", 401)

    try:
        header = pyjwt.get_unverified_header(proof)
        jwk = header.get("jwk")
        if not jwk or jwk.get("kty") != "EC":
            deny("proof header must carry a public EC jwk")
        if thumbprint(jwk) != expected_jkt:
            deny("proof key does not match the token's cnf.jkt")
        numbers = ec.EllipticCurvePublicNumbers(
            x=int.from_bytes(base64.urlsafe_b64decode(jwk["x"] + "=" * (-len(jwk["x"]) % 4)), "big"),
            y=int.from_bytes(base64.urlsafe_b64decode(jwk["y"] + "=" * (-len(jwk["y"]) % 4)), "big"),
            curve=ec.SECP256R1(),
        )
        public_pem = numbers.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        claims = pyjwt.decode(
            proof, key=public_pem, algorithms=["ES256"],
            options={"require": ["iat", "jti", "htm", "htu"]},
        )
    except TokenExchangeError:
        raise
    except Exception as exc:
        deny(f"proof did not verify: {exc}")

    if claims["htm"] != method.upper():
        deny(f"htm mismatch: proof is for {claims['htm']}, request is {method.upper()}")
    if claims["htu"] != uri:
        deny(f"htu mismatch: proof is for {claims['htu']!r}, request is {uri!r}")
    if abs(now - claims["iat"]) > PROOF_MAX_AGE:
        deny("proof is stale")
    if access_token is not None:
        expected_ath = _b64url(hashlib.sha256(access_token.encode()).digest())
        if claims.get("ath") != expected_ath:
            deny("ath does not match the access token")
    # nonce second-to-last and replay last, so only fully valid proofs
    # consume a nonce or a jti (garbage cannot fill either store)
    if nonce_store is not None:
        nonce = claims.get("nonce")
        if not nonce or not nonce_store.verify_and_consume(str(nonce)):
            raise TokenExchangeError(
                "use_dpop_nonce", "proof needs a fresh server nonce", 401,
                headers={"DPoP-Nonce": nonce_store.issue()})
    if replay is not None and not replay.check_and_add(expected_jkt, str(claims["jti"]), now):
        deny("proof was already used (replay)")
    return claims
