"""Manage client secrets against a durable data directory.

  ACTAS_DATA_DIR=... python -m actas.register_client <client_id>
  ACTAS_DATA_DIR=... python -m actas.register_client rotate <client_id> [--grace S]

Generates a random secret, stores only its hash, and prints the secret once.
`rotate` swaps the secret; --grace keeps the old secret valid that many
seconds so the client can be reconfigured without downtime.
"""
import os
import secrets
import sys

from . import persist as persist_mod
from .clients import ClientAuthenticator


def main() -> None:
    args = sys.argv[1:]
    if not os.environ.get("ACTAS_DATA_DIR"):
        sys.exit("usage: ACTAS_DATA_DIR=dir python -m actas.register_client [rotate] <client_id> "
                 "[--grace S]")
    d = os.environ["ACTAS_DATA_DIR"]
    os.makedirs(d, exist_ok=True)
    p = persist_mod.Persistence(os.path.join(d, "actas.db"), os.environ.get("ACTAS_KEY_PASSPHRASE"))
    auth = ClientAuthenticator(p.conn)
    secret = secrets.token_urlsafe(32)
    if args[:1] == ["rotate"]:
        client_id = args[1] if len(args) >= 2 else None
        grace = 0.0
        if len(args) == 4 and args[2] == "--grace":
            try:
                grace = float(args[3])
            except ValueError:
                sys.exit("--grace takes seconds")
        elif len(args) != 2:
            sys.exit("usage: ... register_client rotate <client_id> [--grace S]")
        try:
            auth.rotate(client_id, secret, grace_seconds=grace)
        except KeyError as exc:
            sys.exit(str(exc))
        print(secret)
        if grace > 0:
            print(f"old secret stays valid for {int(grace)}s", file=sys.stderr)
    elif len(args) == 1:
        auth.register(args[0], secret)
        print(secret)
    else:
        sys.exit("usage: ACTAS_DATA_DIR=dir python -m actas.register_client [rotate] <client_id> "
                 "[--grace S]")


if __name__ == "__main__":
    main()
