"""Optional SQLite persistence: signing keys, audit chain, token registry.

One file. Private keys are encrypted at rest when ACTAS_KEY_PASSPHRASE is set
(PKCS8 + BestAvailableEncryption); otherwise they are stored as plain PKCS8
PEM and the file is created with mode 0600, so protect the volume it lives on.
The audit rows keep their stored hashes: verify() recomputes the chain from
the stored fields, so editing a row in the database file is detected.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading

from cryptography.hazmat.primitives import serialization


class Persistence:
    def __init__(self, path: str, passphrase: str | None = None):
        fresh = not os.path.exists(path)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        if fresh:
            os.chmod(path, 0o600)
        self._lock = threading.Lock()
        self._pass = passphrase.encode() if passphrase else None
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS keys (kid TEXT PRIMARY KEY, pem BLOB, created_at REAL,
                                             current INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS audit (seq INTEGER PRIMARY KEY, ts REAL, event TEXT,
                                              detail TEXT, prev_hash TEXT, hash TEXT);
            """
        )
        self.conn.commit()

    # keys ---------------------------------------------------------------
    def save_key(self, pair, current: bool) -> None:
        enc = (serialization.BestAvailableEncryption(self._pass)
               if self._pass else serialization.NoEncryption())
        pem = pair.private_key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, enc)
        with self._lock:
            if current:
                self.conn.execute("UPDATE keys SET current=0")
            self.conn.execute("INSERT OR REPLACE INTO keys VALUES (?,?,?,?)",
                              (pair.kid, pem, pair.created_at, int(current)))
            self.conn.commit()

    def delete_key(self, kid: str) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM keys WHERE kid=?", (kid,))
            self.conn.commit()

    def load_keys(self) -> list[tuple[str, object, float, bool]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT kid, pem, created_at, current FROM keys ORDER BY created_at").fetchall()
        return [(k, serialization.load_pem_private_key(bytes(p), self._pass), c, bool(cur))
                for k, p, c, cur in rows]

    # audit --------------------------------------------------------------
    def save_audit(self, entry) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO audit VALUES (?,?,?,?,?,?)",
                (entry.seq, entry.ts, entry.event,
                 json.dumps(entry.detail, sort_keys=True), entry.prev_hash, entry.hash))
            self.conn.commit()

    def load_audit(self) -> list[tuple]:
        with self._lock:
            return [(s, t, e, json.loads(d), p, h) for s, t, e, d, p, h in self.conn.execute(
                "SELECT seq, ts, event, detail, prev_hash, hash FROM audit ORDER BY seq")]
