"""Scoped bearer-token auth (embedded actas issuer) + /demo/delegate.

Second, additive credential path: issued tokens can only ever carry a
NARROWER envelope than configured static keys. Covers: signature/issuer/
audience/expiry verification, cnf rejection, required-scope enforcement,
the model allowlist, static-key regression, budget-from-config (never from
claims), log redaction, and the self-contained delegation demo.
"""
import time

import pytest
from httpx import ASGITransport, AsyncClient

from actas.keys import KeyStore
from actas.tokens import issue_token
from llmgate.app import ApiKey, create_app
from llmgate.config import build_app
from llmgate.providers import MockProvider
from llmgate.router import Route, Router
from llmgate.tokenauth import ActasVerifier, TokenAuthConfig, TokenError

ISSUER = "llmgate-embedded"
AUD = "llmgate"
CHEAP, EXPENSIVE = "gpt-sim", "gpt-sim-pro"


def make_router():
    a = MockProvider("sim-a", [CHEAP, EXPENSIVE], latency=0.0)
    b = MockProvider("sim-b", [CHEAP, EXPENSIVE], latency=0.0)
    return Router([Route(a, 0), Route(b, 0)])


def make_app(extra_scopes=None):
    """Static keys + token path. scope->models maps llm:complete to the cheap
    alias and llm:expensive-models to the expensive one."""
    ks = KeyStore()
    scope_models = {"llm:complete": [CHEAP], "llm:expensive-models": [EXPENSIVE]}
    if extra_scopes:
        scope_models.update(extra_scopes)
    cfg = TokenAuthConfig(verifier=ActasVerifier(ks, ISSUER, AUD),
                          scope_models=scope_models)
    app = create_app(make_router(), {"k-static": ApiKey(name="static")},
                     token_auth=cfg)
    return app, ks


def tok(ks, sub="user-1", scope=("llm:complete",), ttl=600, aud=AUD, iss=ISSUER, extra=None):
    encoded, _ = issue_token(ks, issuer=iss, subject=sub, scope=" ".join(scope),
                             audience=[aud], expires_in=ttl, extra=extra)
    return encoded


async def post(app, token, model=CHEAP, content="ping"):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        return await c.post("/v1/chat/completions",
                            headers={"Authorization": f"Bearer {token}"},
                            json={"model": model,
                                  "messages": [{"role": "user", "content": content}],
                                  "max_tokens": 1})


@pytest.mark.asyncio
async def test_valid_token_reaches_scoped_model():
    app, ks = make_app()
    r = await post(app, tok(ks))
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_valid_token_refused_outside_allowlist():
    app, ks = make_app()
    r = await post(app, tok(ks), model=EXPENSIVE)
    assert r.status_code == 403
    assert "insufficient_scope" in r.json()["error"]["type"]


@pytest.mark.asyncio
async def test_expanded_scope_reaches_expensive_model():
    app, ks = make_app()
    t = tok(ks, scope=("llm:complete", "llm:expensive-models"))
    r = await post(app, t, model=EXPENSIVE)
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_wrong_audience_rejected():
    app, ks = make_app()
    r = await post(app, tok(ks, aud="other-service"))
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_wrong_issuer_rejected():
    app, ks = make_app()
    r = await post(app, tok(ks, iss="someone-else"))
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_expired_rejected():
    app, ks = make_app()
    r = await post(app, tok(ks, ttl=-60))
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_bad_signature_rejected():
    app, ks = make_app()
    other = KeyStore()  # different keypair: unknown kid / bad signature
    r = await post(app, tok(other))
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_tampered_payload_rejected():
    app, ks = make_app()
    t = tok(ks)
    head, payload, sig = t.split(".")
    # tamper with the payload segment: signature no longer matches
    r = await post(app, ".".join([head, payload[:-2] + "AA", sig]))
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_cnf_bound_token_hard_rejected():
    """A proof-of-possession token is NOT accepted as a plain bearer token:
    bound tokens need a proof this deployment does not verify."""
    app, ks = make_app()
    t = tok(ks, extra={"cnf": {"jkt": "A" * 43}})
    r = await post(app, t)
    assert r.status_code == 401
    assert "proof" in r.json()["error"]["message"]


@pytest.mark.asyncio
async def test_missing_required_scope_denied_everything():
    app, ks = make_app()
    t = tok(ks, scope=("llm:expensive-models",))  # no llm:complete
    r = await post(app, t, model=EXPENSIVE)
    assert r.status_code == 403
    r = await post(app, t, model=CHEAP)
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_empty_scope_denied_everything():
    app, ks = make_app()
    r = await post(app, tok(ks, scope=()))
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_unmapped_scope_is_empty_allowlist():
    app, ks = make_app()
    t = tok(ks, scope=("llm:complete", "nonexistent:scope"))
    assert (await post(app, t, model=CHEAP)).status_code == 200
    assert (await post(app, t, model=EXPENSIVE)).status_code == 403


@pytest.mark.asyncio
async def test_static_keys_unchanged():
    """Static key keeps its full envelope: both aliases, no allowlist."""
    app, ks = make_app()
    assert (await post(app, "k-static", model=CHEAP)).status_code == 200
    assert (await post(app, "k-static", model=EXPENSIVE)).status_code == 200


@pytest.mark.asyncio
async def test_garbage_bearer_rejected():
    app, _ = make_app()
    assert (await post(app, "not-a-token")).status_code == 401
    assert (await post(app, "a.b.c.d")).status_code == 401
    assert (await post(app, "a.b")).status_code == 401


@pytest.mark.asyncio
async def test_token_budgets_from_config_not_claims():
    """Claims never set their own budget: a token asking for a huge budget
    still gets the configured 2 rps / burst 4."""
    app, ks = make_app()
    t = tok(ks, extra={"budget_rps": 99999, "rate": 99999})
    codes = [(await post(app, t)).status_code for _ in range(6)]
    assert codes[:4] == [200, 200, 200, 200]
    assert 429 in codes[4:]


@pytest.mark.asyncio
async def test_log_redaction_and_identity():
    """No token string anywhere in the request log; identity is jwt:<sub>."""
    app, ks = make_app()
    t = tok(ks, sub="redaction-check")
    await post(app, t)
    events = app.state.reqlog.latest(20)
    blob = repr(events)
    assert t not in blob and t.split(".")[1] not in blob
    assert "jwt:redaction-check" in blob


@pytest.mark.asyncio
async def test_real_model_alias_isolated_from_token_identities():
    """Isolation: a router serving a real-mode alias (gpt-real) that is NOT in
    the scope->models mapping is unreachable by token identities no matter
    which scopes they carry - while a static key still reaches it. This is
    the property that must hold before any token path may coexist with a
    real upstream: tokens can never widen past the configured mapping."""
    a = MockProvider("sim-a", [CHEAP, EXPENSIVE], latency=0.0)
    real = MockProvider("groq", ["gpt-real"], latency=0.0)
    router = Router([Route(a, 0), Route(real, 0)])
    ks = KeyStore()
    cfg = TokenAuthConfig(verifier=ActasVerifier(ks, ISSUER, AUD),
                          scope_models={"llm:complete": [CHEAP],
                                        "llm:expensive-models": [EXPENSIVE]})
    app = create_app(router, {"k-static": ApiKey(name="static")}, token_auth=cfg)
    t = tok(ks, scope=("llm:complete", "llm:expensive-models"))
    assert (await post(app, t, model="gpt-real")).status_code == 403
    assert (await post(app, t, model=CHEAP)).status_code == 200
    assert (await post(app, "k-static", model="gpt-real")).status_code == 200


@pytest.mark.asyncio
async def test_token_bucket_isolated_from_static_key_bucket():
    """A token identity's rate budget is its own bucket: hammering it does
    not consume the static key's budget and vice versa."""
    app, ks = make_app()
    t = tok(ks)
    codes = [(await post(app, t)).status_code for _ in range(6)]
    assert 429 in codes  # token identity (2 rps / burst 4) is now limited
    assert (await post(app, "k-static")).status_code == 200  # static key unaffected


def _write_cfg(tmp_cfg: dict) -> str:
    import json as _json
    import os
    import tempfile
    os.environ.setdefault("LLMGATE_TEST_KEYS_TOKENAUTH", _json.dumps({"k-static": {"name": "static"}}))
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        _json.dump(tmp_cfg, f)
        return f.name


def test_misconfiguration_scope_models_to_real_alias_fails_boot():
    """Fail fast, no silent narrow: a scope->models mapping that references a
    real-provider alias must be rejected at boot, not enforced later."""
    cfg = {
        "providers": [{"name": "sim-a", "type": "mock", "models": [CHEAP], "latency": 0.0},
                      {"name": "groq", "type": "openai_compatible",
                       "base_url": "https://example.invalid/v1",
                       "api_key_env": "LLMGATE_TEST_DUMMY_UPSTREAM",
                       "models": ["gpt-real"]}],
        "keys_env": "LLMGATE_TEST_KEYS_TOKENAUTH",
        "token_auth": {"enabled": True, "issuer": ISSUER, "audience": AUD,
                       "scope_models": {"llm:complete": [CHEAP, "gpt-real"]}},
    }
    import os
    os.environ["LLMGATE_TEST_DUMMY_UPSTREAM"] = "dummy-not-used"
    with pytest.raises(ValueError, match="real-provider"):
        build_app(_write_cfg(cfg))


def test_misconfiguration_demo_alias_to_real_alias_fails_boot():
    """The demo's expensive alias pointing at a real provider is the same
    leak class: rejected at boot."""
    cfg = {
        "providers": [{"name": "sim-a", "type": "mock", "models": [CHEAP], "latency": 0.0},
                      {"name": "groq", "type": "openai_compatible",
                       "base_url": "https://example.invalid/v1",
                       "api_key_env": "LLMGATE_TEST_DUMMY_UPSTREAM",
                       "models": ["gpt-real"]}],
        "keys_env": "LLMGATE_TEST_KEYS_TOKENAUTH",
        "token_auth": {"enabled": True, "issuer": ISSUER, "audience": AUD,
                       "scope_models": {"llm:complete": [CHEAP]},
                       "demo": {"enabled": True, "cheap_model": CHEAP,
                                "expensive_model": "gpt-real"}},
    }
    import os
    os.environ["LLMGATE_TEST_DUMMY_UPSTREAM"] = "dummy-not-used"
    with pytest.raises(ValueError, match="real-provider"):
        build_app(_write_cfg(cfg))


@pytest.mark.asyncio
async def test_demo_delegate_endpoint():
    """The self-contained exchange flow: sub-agent narrowed to the cheap
    alias, refused the expensive one; widening denied; audit chain verifies."""
    import json as _json
    import tempfile
    cfg = {
        "providers": [{"name": "sim-a", "type": "mock", "models": [CHEAP, EXPENSIVE],
                       "latency": 0.0},
                      {"name": "sim-b", "type": "mock", "models": [CHEAP, EXPENSIVE],
                       "latency": 0.0}],
        "keys_env": "LLMGATE_TEST_KEYS_TOKENAUTH",
        "token_auth": {
            "enabled": True, "issuer": ISSUER, "audience": AUD,
            "scope_models": {"llm:complete": [CHEAP], "llm:expensive-models": [EXPENSIVE]},
            "demo": {"enabled": True, "cheap_model": CHEAP, "expensive_model": EXPENSIVE},
        },
    }
    import os
    os.environ["LLMGATE_TEST_KEYS_TOKENAUTH"] = _json.dumps(
        {"k-static": {"name": "static"}})
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        _json.dump(cfg, f)
        path = f.name
    app = build_app(path)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.post("/demo/delegate")
    assert r.status_code == 200
    d = r.json()
    assert d["ok"], d
    assert d["delegation_chain"] == ["parent-agent", "sub-agent"]
    assert d["sub_agent_identity"] == "jwt:sub-agent"
    assert d["audit_chain_verifies"] is True
    by_name = {s["name"]: s for s in d["steps"]}
    assert by_name["sub-cheap"]["status"] == "pass"
    assert by_name["sub-expensive"]["status"] == "pass"
    assert by_name["parent-expensive"]["status"] == "pass"
    assert by_name["widening-denied"]["status"] == "pass"
    assert any("unbound" in l for l in d["labels"])
    # the audit tail records grants and the widening denial
    events = {e.get("event") for e in d["audit"]}
    assert "exchange.granted" in events and "exchange.denied" in events


@pytest.mark.asyncio
async def test_delegate_audit_log_is_bounded_in_a_warm_process():
    """Repeated /demo/delegate calls rotate the chain instead of growing it
    forever: a log.rotated marker appears, the chain still verifies, and the
    retained entries stay bounded."""
    import json as _json
    import tempfile
    cfg = {
        "providers": [{"name": "sim-a", "type": "mock", "models": [CHEAP, EXPENSIVE],
                       "latency": 0.0},
                      {"name": "sim-b", "type": "mock", "models": [CHEAP, EXPENSIVE],
                       "latency": 0.0}],
        "keys_env": "LLMGATE_TEST_KEYS_TOKENAUTH",
        "token_auth": {
            "enabled": True, "issuer": ISSUER, "audience": AUD,
            "scope_models": {"llm:complete": [CHEAP], "llm:expensive-models": [EXPENSIVE]},
            "demo": {"enabled": True, "cheap_model": CHEAP, "expensive_model": EXPENSIVE},
        },
    }
    import os
    os.environ["LLMGATE_TEST_KEYS_TOKENAUTH"] = _json.dumps(
        {"k-static": {"name": "static"}})
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        _json.dump(cfg, f)
        path = f.name
    app = build_app(path)
    from llmgate.delegation import _demos
    demo = _demos[id(app)]
    demo.audit_cap = 1  # rotate before the next run, however small the log
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r1 = await c.post("/demo/delegate")
        r2 = await c.post("/demo/delegate")
    assert r1.status_code == 200 and r1.json()["ok"]
    d2 = r2.json()
    assert d2["ok"], d2
    assert d2["audit_chain_verifies"] is True
    events = [e.get("event") for e in d2["audit"]]
    assert events[0] == "log.rotated"  # rotation marker heads the fresh chain
    # bounded: one rotation marker + one run's entries, far below 2 runs' worth
    assert len(demo.audit) < 2 * len([e for e in events])
