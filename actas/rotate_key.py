"""Rotate the signing key: ACTAS_DATA_DIR=... python -m actas.rotate_key [--retire-older-than S]

Generates a fresh RSA key, makes it the signing key, and keeps every old
public key in the JWKS document so already-issued tokens stay verifiable.
With --retire-older-than SECONDS, keys retired from signing longer ago than that are dropped
from the JWKS - only safe once every token they signed has expired, so pick
a window at least as long as your longest token TTL.
"""
import os
import sys
import time

from . import persist as persist_mod
from .keys import KeyStore


def main() -> None:
    retire = None
    args = sys.argv[1:]
    if args[:1] == ["--retire-older-than"] and len(args) == 2:
        try:
            retire = float(args[1])
            import math
            if not math.isfinite(retire) or retire < 0:
                raise ValueError('finite nonnegative seconds required')
        except ValueError:
            sys.exit("--retire-older-than takes seconds")
    elif args:
        sys.exit("usage: ACTAS_DATA_DIR=dir python -m actas.rotate_key [--retire-older-than S]")
    if not os.environ.get("ACTAS_DATA_DIR"):
        sys.exit("ACTAS_DATA_DIR must point at the server's data directory")
    print("offline operation: stop the single writer before rotation, then restart it to load the new keyset")
    d = os.environ["ACTAS_DATA_DIR"]
    p = persist_mod.Persistence(os.path.join(d, "actas.db"),
                                os.environ.get("ACTAS_KEY_PASSPHRASE"))
    ks = KeyStore(persist=p)
    old_kid = ks.current.kid
    new_kid = ks.rotate()
    print(f"rotated: {old_kid} -> {new_kid}")
    print(f"jwks now publishes {ks.kid_count()} keys; tokens signed by {old_kid} "
          "remain verifiable until they expire")
    if retire is not None:
        doomed = ks.retire_older_than(retire, now=time.time())
        for kid in doomed:
            print(f"retired {kid} (overlap since last signing exceeds {int(retire)}s)")
        if doomed:
            print("warning: tokens signed by retired keys no longer verify; "
                  "retire windows must exceed the longest token TTL")


if __name__ == "__main__":
    main()
