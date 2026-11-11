"""Append-only audit log with a SHA-256 hash chain.

Each entry commits to the hash of its predecessor, so editing, deleting or
reordering history breaks the chain and verification fails. This gives
tamper evidence: it detects modification of stored entries. It is not
tamper-proof storage - an attacker who rewrites the whole log and its
anchor can recompute a valid-looking chain. Honest deployments would pin
the head hash to an external witness.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field

GENESIS = "0" * 64


@dataclass
class AuditEntry:
    seq: int
    ts: float
    event: str
    detail: dict
    prev_hash: str
    hash: str = field(default="", init=False)

    def __post_init__(self):
        self.hash = self.compute_hash()

    def payload(self) -> dict:
        return {
            "seq": self.seq,
            "ts": self.ts,
            "event": self.event,
            "detail": self.detail,
            "prev_hash": self.prev_hash,
        }

    def compute_hash(self) -> str:
        canonical = json.dumps(self.payload(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def as_dict(self) -> dict:
        out = self.payload()
        out["hash"] = self.hash
        return out


class AuditLog:
    def __init__(self, persist=None):
        self._entries: list[AuditEntry] = []
        self._persist = persist
        for seq, ts, event, detail, prev, stored in (persist.load_audit() if persist else []):
            entry = AuditEntry(seq=seq, ts=ts, event=event, detail=detail, prev_hash=prev)
            entry.hash = stored  # keep the stored hash so tampering shows in verify()
            self._entries.append(entry)

    def append(self, event: str, detail: dict, ts: float | None = None) -> AuditEntry:
        prev = self._entries[-1].hash if self._entries else GENESIS
        entry = AuditEntry(
            seq=len(self._entries) + 1,
            ts=time.time() if ts is None else ts,
            event=event,
            detail=detail,
            prev_hash=prev,
        )
        self._entries.append(entry)
        if self._persist:
            self._persist.save_audit(entry)
        return entry

    def verify(self) -> tuple[bool, int | None]:
        """Recompute the chain. Returns (ok, seq_of_first_bad_entry)."""
        prev = GENESIS
        for entry in self._entries:
            if entry.prev_hash != prev:
                return False, entry.seq
            if entry.compute_hash() != entry.hash:
                return False, entry.seq
            prev = entry.hash
        return True, None

    def tail(self, n: int = 50) -> list[dict]:
        return [e.as_dict() for e in self._entries[-n:]]

    def head_hash(self) -> str | None:
        return self._entries[-1].hash if self._entries else None

    def __len__(self) -> int:
        return len(self._entries)

    def hash_at(self, seq: int) -> str | None:
        return self._entries[seq - 1].hash if 1 <= seq <= len(self._entries) else None

    def consistent_with(self, seq: int, hash_: str) -> bool:
        """True if this log still contains the entry a witness remembered.

        A witness stores (seq, hash) somewhere the log's operator cannot edit.
        If the operator later rewrites history, the hash at that seq changes
        or the log gets shorter, and this returns False.
        """
        return self.hash_at(seq) == hash_ and self.verify()[0]
