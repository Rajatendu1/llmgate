"""Build an app from a JSON config. API keys for upstreams are read from environment
variables named in the file; secrets never live in the config itself.

{
  "keys_env": "LLMGATE_KEYS",
  "providers": [
    {"name": "primary", "type": "openai_compatible", "base_url": "https://api.example.com/v1",
     "api_key_env": "PRIMARY_API_KEY", "models": ["some-model"], "priority": 0, "weight": 1, "timeout": 30, "idle_timeout": 30},
    {"name": "sim", "type": "mock", "models": ["some-model"], "priority": 1}
  ]
}"""
import json, os
from .app import create_app, keys_from_env
from .providers import HTTPProvider, MockProvider
from .resilience import CircuitBreaker
from .router import Price, Route, Router


def build_app(path: str):
    with open(path) as config_file:
        cfg = json.load(config_file)
    # This flag authorizes re-sending a possibly-generated request. Never
    # coerce a string such as "false" to True, and validate before clients
    # are allocated. JSON uses booleans; env values are explicit tokens.
    if "failover_ambiguous" in cfg:
        failover_ambiguous = cfg["failover_ambiguous"]
        if type(failover_ambiguous) is not bool:
            raise ValueError("failover_ambiguous must be a JSON boolean")
    else:
        raw = os.getenv("LLMGATE_FAILOVER_AMBIGUOUS", "0").strip().lower()
        if raw not in {"0", "false", "no", "1", "true", "yes"}:
            raise ValueError("LLMGATE_FAILOVER_AMBIGUOUS must be 0/1, false/true or no/yes")
        failover_ambiguous = raw in {"1", "true", "yes"}
    routes = []
    for p in cfg["providers"]:
        if p["type"] == "mock":
            prov = MockProvider(p["name"], p["models"], latency=p.get("latency", 0.02), fail_rate=p.get("fail_rate", 0.0))
        elif p["type"] == "openai_compatible":
            # A real upstream with no key in the environment is skipped, not
            # fatal: the same config ships in the public source, and the route
            # activates only when the deployment provides the key. Logged so a
            # misconfigured real deploy is visible rather than silently absent.
            key = os.getenv(p["api_key_env"])
            if not key:
                import logging
                logging.getLogger("llmgate").warning(
                    "provider %s skipped: %s is not set", p["name"], p["api_key_env"])
                continue
            prov = HTTPProvider(p["name"], p["base_url"], key, p["models"],
                                model_map=p.get("model_map"))
        else:
            raise ValueError(f"unknown provider type {p['type']!r}")
        price = None
        if "price" in p:  # optional list price, USD per 1M tokens: {"prompt_per_mtok": x, "completion_per_mtok": y}
            price = Price(p["price"].get("prompt_per_mtok", 0.0),
                          p["price"].get("completion_per_mtok", 0.0))
        routes.append(Route(prov, p.get("priority", 0), p.get("weight", 1.0),
                            CircuitBreaker(p.get("breaker_threshold", 3), p.get("breaker_cooldown", 10.0)),
                            timeout=p.get("timeout", 30.0), idle_timeout=p.get("idle_timeout", 30.0),
                            price=price, timeout_per_1k=p.get("timeout_per_1k", 0.0),
                            timeout_max=p.get("timeout_max")))
    # AMBIGUOUS failures (timeout/408/5xx) may have generated upstream; failover
    # then can duplicate generation (a duplicate charge with a real provider).
    # Duplicate-averse default: availability-first failover must be opted into via
    # the config's "failover_ambiguous": true or LLMGATE_FAILOVER_AMBIGUOUS=1.
    router = Router(routes, failover_ambiguous=failover_ambiguous)
    keys = keys_from_env(cfg.get("keys_env", "LLMGATE_KEYS"))
    # Optional extra keys merged from a second env var: a temporary verification
    # or per-client key can be added/removed without touching the main secret.
    # On a duplicate entry the main set wins.
    extra_env = cfg.get("extra_keys_env", "LLMGATE_KEYS_EXTRA")
    if os.getenv(extra_env):
        extra = keys_from_env(extra_env)
        for k, v in extra.items():
            keys.setdefault(k, v)
    # Optional scoped bearer-token auth (second, additive credential path).
    # The issuer is EMBEDDED in this process (vendored actas): one trust
    # domain, no remote issuer. Config pins expected issuer + audience and
    # the scope -> public-alias allowlist; claims never widen it. When
    # "demo" is enabled, /demo/delegate runs a self-contained exchange flow.
    token_auth = None
    ta = cfg.get("token_auth")
    if ta and ta.get("enabled"):
        from actas.keys import KeyStore
        from .tokenauth import ActasVerifier, TokenAuthConfig
        # Isolation guarantee, enforced at boot (fail fast, no silent narrow):
        # token identities may never reach a REAL provider. The scope->models
        # mapping and the demo's aliases must reference simulated providers
        # only; a config that points either at a real-mode route is rejected
        # here, so a misconfigured deploy fails loudly instead of leaking
        # real-upstream spend to a scoped token.
        real_models = set()
        for r in router.routes:
            if getattr(r.provider, "mode", "real") == "real":
                real_models |= set(r.provider.models)
        mapped = {m for ms in ta["scope_models"].values() for m in ms}
        demo_models = {ta.get("demo", {}).get("cheap_model"),
                       ta.get("demo", {}).get("expensive_model")} - {None}
        leaked = (mapped | demo_models) & real_models
        if leaked:
            raise ValueError(
                f"token_auth may not reference real-provider model(s) {sorted(leaked)}: "
                "scoped tokens and the delegation demo are isolated to simulated providers")
        # One keystore shared by the app's verifier and the demo issuer:
        # a single embedded trust domain. Per-process; a cold start rotates
        # keys, which is irrelevant to the demo because its flow issues and
        # consumes every token inside one request.
        keystore = KeyStore()
        verifier = ActasVerifier(keystore, ta["issuer"], ta["audience"])
        token_auth = TokenAuthConfig(
            verifier=verifier, scope_models=ta["scope_models"],
            required_scope=ta.get("required_scope", "llm:complete"),
            rate=ta.get("rate", 2.0), burst=ta.get("burst", 4.0),
            token_rate=ta.get("token_rate"), token_burst=ta.get("token_burst"))
    app = create_app(router, keys, token_auth=token_auth)
    if ta and ta.get("enabled") and ta.get("demo", {}).get("enabled"):
        from .delegation import install_delegate_demo as _install
        _install(app, token_auth, ta["demo"]["cheap_model"], ta["demo"]["expensive_model"],
                 keystore)
    return app


def app():  # uvicorn --factory llmgate.config:app   (reads LLMGATE_CONFIG)
    return build_app(os.environ["LLMGATE_CONFIG"])
