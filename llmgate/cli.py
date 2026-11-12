"""Command line interface: build the gateway from a JSON config and serve it.

Real upstream routes activate only when their required environment credential
is set (see docs/DEPLOYMENT.md); secrets never live in the config itself.
"""
import argparse


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(
        prog="llmgate",
        description="An OpenAI-compatible LLM gateway: routing, failover, "
                    "streaming, per-key rate limits and usage metering.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser(
        "serve", help="Build the gateway from a JSON config and serve it.")
    serve.add_argument("--config", required=True,
                       help="Path to a JSON config (see examples.config.json).")
    serve.add_argument("--host", default="127.0.0.1",
                       help="Bind host (default 127.0.0.1).")
    serve.add_argument("--port", type=int, default=8000,
                       help="Bind port (default 8000).")
    args = parser.parse_args(argv)

    if args.command == "serve":
        import os
        if not os.path.isfile(args.config):
            parser.error(f"config not found: {args.config}")
        # Imported here so `llmgate --help` stays light.
        import uvicorn
        from .config import build_app
        app = build_app(args.config)
        uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
