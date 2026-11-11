"""Offline SQLite snapshot/restore. No HTTP/admin endpoint and no overwrite mode.

Stop every writer first. SQLite backup supplies a consistent database image;
application memory, subsequent revocations and remote witnesses are not captured.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3

from cryptography.hazmat.primitives.serialization import load_pem_private_key
from .audit import AuditLog


def readonly(path):
    p=Path(path).absolute()
    if p.is_symlink() or not p.is_file():
        raise ValueError('source must be an existing regular file, not a symlink')
    return sqlite3.connect(p.as_uri()+'?mode=ro',uri=True)


def inspect(path, passphrase=None):
    conn=readonly(path)
    try:
        if conn.execute('PRAGMA integrity_check').fetchall()!=[('ok',)]:
            raise ValueError('database integrity check failed')
        required={'keys','audit','issued','revoked','clients','replay'}
        tables={r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not required <= tables:
            raise ValueError('not a complete Actas durable database')
        keys=conn.execute('SELECT pem,current FROM keys').fetchall()
        if not keys or sum(bool(r[1]) for r in keys)!=1:
            raise ValueError('exactly one current signing key required')
        password=passphrase.encode() if passphrase else None
        for pem,_ in keys:
            load_pem_private_key(bytes(pem),password)
        class Rows:
            def load_audit(self):
                return [(s,t,e,json.loads(d),p,h) for s,t,e,d,p,h in conn.execute('SELECT seq,ts,event,detail,prev_hash,hash FROM audit ORDER BY seq')]
        audit=AuditLog(persist=Rows())
        if audit.verify()!=(True,None):
            raise ValueError('audit chain verification failed')
        counts={name:conn.execute('SELECT count(*) FROM '+name).fetchone()[0] for name in sorted(required)}
        return dict(counts=counts,audit_head=audit.head_hash())
    finally:
        conn.close()


def copy_snapshot(source,destination,*,passphrase=None):
    """Consistent copy into an exclusive private destination, validate before success.

    Call only after all writers stop. A restore to a new path is this same
    operation, followed by operator-controlled startup. No replacement shortcut.
    """
    destination=Path(destination).absolute()
    if destination.exists() or destination.is_symlink():
        raise ValueError('destination already exists; overwrite is not supported')
    inspect(source,passphrase)
    src=readonly(source)
    fd=None
    created=False
    try:
        fd=os.open(destination,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
        created=True
        os.close(fd);fd=None
        target=sqlite3.connect(destination)
        try:src.backup(target)
        finally:target.close()
        summary=inspect(destination,passphrase)
        digest=hashlib.sha256()
        with open(destination,'rb') as handle:
            for chunk in iter(lambda:handle.read(65536),b''):digest.update(chunk)
        return dict(**summary,sha256=digest.hexdigest(),bytes=destination.stat().st_size)
    except BaseException:
        # Only this operation created the file; pre-existing paths never get here.
        if created and destination.exists():destination.unlink()
        raise
    finally:
        src.close()
        if fd is not None:os.close(fd)


def main():
    import argparse
    parser=argparse.ArgumentParser(description='Offline private Actas snapshot/restore; never overwrites.')
    parser.add_argument('operation',choices=('backup','restore'))
    parser.add_argument('source')
    parser.add_argument('destination')
    parser.add_argument('--server-stopped',action='store_true',required=True,help='acknowledge that all server/CLI writers are stopped')
    args=parser.parse_args()
    try:
        result=copy_snapshot(args.source,args.destination,passphrase=os.environ.get('ACTAS_KEY_PASSPHRASE'))
    except Exception:
        # Avoid exposing key details, secrets, SQL data or filesystem internals.
        parser.exit(2,'snapshot refused: check paths, complete database, passphrase and audit integrity; destination must not exist\n')
    print(json.dumps(dict(operation=args.operation,**result),sort_keys=True))
    print('Offline copy only. Restoring an old snapshot can forget later revocations/replays; reconcile before serving real traffic.')


if __name__=='__main__':main()
