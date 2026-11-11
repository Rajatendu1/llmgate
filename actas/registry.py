"""Issued-token registry and revocation with cascade.

Every exchange records an edge child_jti -> parent_jti (the subject token it
was derived from). Revoking a token therefore also kills everything derived
from it, however deep the delegation chain: if Asha's grant to planner-bot is
revoked, so is the summarizer token planner-bot handed out.

Honest limit: this is enforced wherever the registry is consulted (the
exchange endpoint, introspection, resource servers that call it). A resource
server that validates JWTs offline cannot see a revocation until the token
expires, which is why lifetimes stay short.
"""

from __future__ import annotations

import sqlite3
import threading
import time

MAX_DEPTH = 32  # defensive bound on parent walking


class TokenRegistry:
    def __init__(self, conn: sqlite3.Connection | None = None):
        self._lock = threading.Lock()
        self._conn = conn or sqlite3.connect(":memory:", check_same_thread=False)
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS issued (
                    jti TEXT PRIMARY KEY, parent_jti TEXT, sub TEXT, actor TEXT,
                    scope TEXT, exp REAL, issued_at REAL);
                CREATE TABLE IF NOT EXISTS revoked (
                    jti TEXT PRIMARY KEY, revoked_at REAL, reason TEXT);
                """
            )
            self._conn.commit()

    def record(self, *, jti, parent_jti, sub, actor, scope, exp) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO issued VALUES (?,?,?,?,?,?,?)",
                (jti, parent_jti, sub, actor, scope, exp, time.time()),
            )
            self._conn.commit()

    def revoke(self, jti: str, reason: str = "") -> bool:
        """Revoke one token (and, implicitly, its descendants). True if new."""
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO revoked VALUES (?,?,?)", (jti, time.time(), reason[:200])
            )
            self._conn.commit()
            return cur.rowcount == 1

    def is_revoked(self, jti: str | None) -> bool:
        node, hops = jti, 0
        with self._lock:
            while node and hops < MAX_DEPTH:
                if self._conn.execute("SELECT 1 FROM revoked WHERE jti=?", (node,)).fetchone():
                    return True
                row = self._conn.execute(
                    "SELECT parent_jti FROM issued WHERE jti=?", (node,)).fetchone()
                node = row[0] if row else None
                hops += 1
        return False

    def known(self, jti: str) -> bool:
        with self._lock:
            return self._conn.execute(
                "SELECT 1 FROM issued WHERE jti=?", (jti,)).fetchone() is not None

    def revoked_unexpired(self, now: float | None = None) -> list[str]:
        """jtis of unexpired tokens that are revoked, descendants included.

        Expired tokens are omitted: they are dead anyway, which keeps the list
        small (a CRL-style feed for resource servers that validate offline).
        """
        now = time.time() if now is None else now
        with self._lock:
            rows = [r[0] for r in self._conn.execute(
                "SELECT jti FROM issued WHERE exp > ?", (now,))]
        return sorted(j for j in rows if self.is_revoked(j))
