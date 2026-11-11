"""Client authentication for the token endpoint.

Registered clients hold a secret; only a salted PBKDF2-SHA256 hash is stored.
When client auth is required, the acting agent must prove who it is before an
exchange runs, so `client_id` is no longer just a claim a caller can make up.
Secrets arrive as `client_secret` (form) or HTTP Basic, per RFC 6749 2.3.1.

rotate() swaps in a new secret and can keep the old one valid for a grace
window, so a client can be reconfigured without downtime. After the window
the old secret dies even though the server never saw it leave.

Honest limit: shared-secret authentication only. No mTLS, no private_key_jwt.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import sqlite3
import threading
import time

ITERATIONS = 200_000


def _hash(secret: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", secret.encode(), salt, ITERATIONS)


class ClientAuthenticator:
    def __init__(self, conn: sqlite3.Connection | None = None, required: bool = False):
        self.required = required
        self._lock = threading.Lock()
        self._conn = conn or sqlite3.connect(":memory:", check_same_thread=False)
        with self._lock:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS clients (client_id TEXT PRIMARY KEY, salt BLOB, hash BLOB)")
            cols = {r[1] for r in self._conn.execute("PRAGMA table_info(clients)")}
            for col, ddl in (("prev_salt", "BLOB"), ("prev_hash", "BLOB"),
                             ("prev_expires", "REAL DEFAULT 0")):
                if col not in cols:
                    self._conn.execute(f"ALTER TABLE clients ADD COLUMN {col} {ddl}")
            self._conn.commit()

    def register(self, client_id: str, secret: str) -> None:
        if len(secret) < 16:
            raise ValueError("client secret must be at least 16 characters")
        salt = os.urandom(16)
        with self._lock:
            self._conn.execute("INSERT OR REPLACE INTO clients VALUES (?,?,?,NULL,NULL,0)",
                               (client_id, salt, _hash(secret, salt)))
            self._conn.commit()

    def rotate(self, client_id: str, new_secret: str, grace_seconds: float = 0,
               now: float | None = None) -> None:
        """Swap a client's secret, optionally keeping the old one valid briefly."""
        if len(new_secret) < 16:
            raise ValueError("client secret must be at least 16 characters")
        now = time.time() if now is None else now
        with self._lock:
            row = self._conn.execute(
                "SELECT salt, hash FROM clients WHERE client_id=?", (client_id,)).fetchone()
            if not row:
                raise KeyError(f"unknown client {client_id!r}")
            salt = os.urandom(16)
            if grace_seconds > 0:
                self._conn.execute(
                    "UPDATE clients SET salt=?, hash=?, prev_salt=?, prev_hash=?, prev_expires=? "
                    "WHERE client_id=?",
                    (salt, _hash(new_secret, salt), row[0], row[1],
                     now + grace_seconds, client_id))
            else:
                self._conn.execute(
                    "UPDATE clients SET salt=?, hash=?, prev_salt=NULL, prev_hash=NULL, "
                    "prev_expires=0 WHERE client_id=?",
                    (salt, _hash(new_secret, salt), client_id))
            self._conn.commit()

    def verify(self, client_id: str | None, secret: str | None, now: float | None = None) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT salt, hash, prev_salt, prev_hash, prev_expires FROM clients "
                "WHERE client_id=?", (client_id or "",)).fetchone()
        # always do the current-secret hash work so unknown ids and wrong
        # secrets cost the same
        salt, stored = (row[0], row[1]) if row else (b"\0" * 16, b"\0" * 32)
        candidate = _hash(secret or "", bytes(salt))
        if row and hmac.compare_digest(candidate, bytes(stored)):
            return True
        if row and row[2] is not None and (row[4] or 0) > (time.time() if now is None else now):
            prev_candidate = _hash(secret or "", bytes(row[2]))
            return hmac.compare_digest(prev_candidate, bytes(row[3]))
        return False


def credentials_from(request_headers: dict, form_id: str | None, form_secret: str | None):
    auth = request_headers.get("authorization", "")
    if auth.lower().startswith("basic "):
        try:
            raw = base64.b64decode(auth.split(None, 1)[1]).decode()
            cid, _, sec = raw.partition(":")
            return cid, sec
        except Exception:
            return None, None
    return form_id, form_secret
