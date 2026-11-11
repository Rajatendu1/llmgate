"""Self-serve demo keys: minting, simulated-only scoping, expiry, mint bounds,
and the custom-prompt path through /demo/try."""
import time

import httpx
import pytest

from llmgate.app import ApiKey, create_app
from llmgate.demo_keys import _MemoryStore, digest_of, install_demo_keys
from llmgate.demo_try import install_demo_try
from llmgate.providers import MockProvider
from llmgate.resilience import CircuitBreaker
from llmgate.router import Route, Router


def _app(with_real=False, **kw):
    sim = MockProvider("sim-a", ["gpt-sim", "gpt-sim-pro"], latency=0.01)
    routes = [Route(sim, priority=0, breaker=CircuitBreaker(3, 5))]
    if with_real:
        real = MockProvider("groq", ["gpt-real"], latency=0.01)
        real.mode = "real"
        routes.insert(0, Route(real, priority=0, breaker=CircuitBreaker(3, 5)))
    app = create_app(Router(routes), {"demo-key": ApiKey("demo")})
    install_demo_try(app, model="gpt-sim")
    install_demo_keys(app, **kw)
    return app


async def _mint(cl):
    r = await cl.post("/demo/keys")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] and body["key"].startswith("vg_")
    return body


async def test_mint_shape_and_simulated_route_works():
    app = _app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as cl:
        body = await _mint(cl)
        assert body["models"] == ["gpt-sim", "gpt-sim-pro"]
        assert body["expires_in_s"] > 0 and body["base_url"].endswith("/v1")
        r = await cl.post("/v1/chat/completions",
                          headers={"Authorization": f"Bearer {body['key']}"},
                          json={"model": "gpt-sim",
                                "messages": [{"role": "user", "content": "hi"}]})
        assert r.status_code == 200, r.text
        assert r.headers.get("x-gateway-provider") == "sim-a"


async def test_minted_key_cannot_reach_the_real_route():
    app = _app(with_real=True)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as cl:
        body = await _mint(cl)
        assert "gpt-real" not in body["models"]
        r = await cl.post("/v1/chat/completions",
                          headers={"Authorization": f"Bearer {body['key']}"},
                          json={"model": "gpt-real",
                                "messages": [{"role": "user", "content": "hi"}]})
        assert r.status_code == 403, r.text


async def test_unknown_vg_key_is_rejected():
    app = _app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as cl:
        r = await cl.post("/v1/chat/completions",
                          headers={"Authorization": "Bearer vg_nope-nope-nope"},
                          json={"model": "gpt-sim",
                                "messages": [{"role": "user", "content": "hi"}]})
        assert r.status_code == 401


async def test_per_ip_mint_limit():
    app = _app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as cl:
        for _ in range(3):
            await _mint(cl)
        r = await cl.post("/demo/keys")
        assert r.status_code == 429, r.text


async def test_expired_key_is_rejected():
    app = _app(ttl_s=1)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as cl:
        body = await _mint(cl)
        app.state.dynamic_keys._recs[digest_of(body["key"])]["expires"] = time.time() - 1
        r = await cl.post("/v1/chat/completions",
                          headers={"Authorization": f"Bearer {body['key']}"},
                          json={"model": "gpt-sim",
                                "messages": [{"role": "user", "content": "hi"}]})
        assert r.status_code == 401, r.text
        assert body["key"] not in app.state.keys  # evicted from the cache too


async def test_install_refuses_to_scope_keys_to_a_real_fleet():
    real = MockProvider("groq", ["gpt-real"], latency=0.01)
    real.mode = "real"
    app = create_app(Router([Route(real, priority=0, breaker=CircuitBreaker(3, 5))]),
                     {"demo-key": ApiKey("demo")})
    with pytest.raises(ValueError):
        install_demo_keys(app, models=("gpt-real",))


async def test_custom_prompt_on_the_simulated_try_route():
    app = _app(with_real=True)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as cl:
        r = await cl.post("/demo/try", json={"prompt": "Reply with exactly: ok"})
        assert r.status_code == 200 and r.json()["ok"]
        assert r.json()["mode"] == "simulated"


async def test_custom_prompt_bounds():
    app = _app(with_real=True)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as cl:
        r = await cl.post("/demo/try", json={"prompt": ""})
        assert r.status_code == 400
        r = await cl.post("/demo/try", json={"prompt": "x" * 281})
        assert r.status_code == 400
        r = await cl.post("/demo/try?model=real", json={"prompt": "hello"})
        assert r.status_code == 400


def test_sdk_quickstart_is_on_the_page():
    """The mint flow reveals a drop-in OpenAI SDK quickstart (Python + Node),
    prefilled by JS with the minted key and base URL."""
    from llmgate.demo_html import DEMO_HTML
    assert 'id="mint-sdk"' in DEMO_HTML
    assert 'id="mint-py"' in DEMO_HTML
    assert 'id="mint-node"' in DEMO_HTML
    assert "openai-python" in DEMO_HTML and "openai-node" in DEMO_HTML
    assert "$('mint-sdk')" in DEMO_HTML  # the mint handler reveals it
    assert "base_url=" in DEMO_HTML and "baseURL:" in DEMO_HTML


def test_stream_demo_is_on_the_page():
    """The mint flow includes a live SSE stream demo: a button that plays a
    streamed reply chunk by chunk and reports measured timing facts."""
    from llmgate.demo_html import DEMO_HTML
    assert 'id="stream-demo"' in DEMO_HTML
    assert 'id="stream-out"' in DEMO_HTML
    assert 'id="stream-stats"' in DEMO_HTML
    assert "$('stream-demo')" in DEMO_HTML  # wired in JS
    assert "getReader()" in DEMO_HTML  # real ReadableStream consumption
    assert "'stream': true" not in DEMO_HTML  # JS builds the body via JSON.stringify
    assert "stream: true" in DEMO_HTML  # the fetch body asks for SSE


async def test_revoke_stops_the_key_immediately():
    app = _app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as cl:
        body = await _mint(cl)
        h = {"Authorization": f"Bearer {body['key']}"}
        ok = await cl.post("/v1/chat/completions", headers=h,
                           json={"model": "gpt-sim", "messages": [{"role": "user", "content": "hi"}]})
        assert ok.status_code == 200
        r = await cl.delete("/demo/keys", headers=h)
        assert r.status_code == 200 and r.json()["revoked"] is True
        gone = await cl.post("/v1/chat/completions", headers=h,
                             json={"model": "gpt-sim", "messages": [{"role": "user", "content": "hi"}]})
        assert gone.status_code == 401
        again = await cl.delete("/demo/keys", headers=h)
        assert again.json()["revoked"] is False  # honest, not a fresh success


async def test_revoke_requires_a_demo_key():
    app = _app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as cl:
        assert (await cl.delete("/demo/keys")).status_code == 401
        assert (await cl.delete("/demo/keys",
                                headers={"Authorization": "Bearer demo-key"})).status_code == 401


async def test_revocation_on_another_instance_propagates_within_the_window():
    # revalidate_s=0: every request re-checks the store, so a record deleted
    # elsewhere (another instance's revocation) dies on the next request.
    app = _app(revalidate_s=0.0)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as cl:
        body = await _mint(cl)
        h = {"Authorization": f"Bearer {body['key']}"}
        ok = await cl.post("/v1/chat/completions", headers=h,
                           json={"model": "gpt-sim", "messages": [{"role": "user", "content": "hi"}]})
        assert ok.status_code == 200
        await app.state.dynamic_keys.delete(digest_of(body["key"]))  # revoked elsewhere
        gone = await cl.post("/v1/chat/completions", headers=h,
                             json={"model": "gpt-sim", "messages": [{"role": "user", "content": "hi"}]})
        assert gone.status_code == 401


class _Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class _FlakyStore(_MemoryStore):
    def __init__(self):
        super().__init__()
        self.fail = False
        self.strict_reads = 0

    async def get_or_raise(self, digest):
        self.strict_reads += 1
        if self.fail:
            raise RuntimeError("store down")
        return await super().get_or_raise(digest)


async def test_revalidation_window_boundaries_at_the_real_default():
    # Deterministic clock, production revalidate_s=60: revoked elsewhere,
    # the key must live BEFORE the window edge and die AT it.
    clk = _Clock()
    app = _app(clock=clk)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as cl:
        body = await _mint(cl)
        h = {"Authorization": f"Bearer {body['key']}"}
        chat = lambda: cl.post("/v1/chat/completions", headers=h,
                               json={"model": "gpt-sim", "messages": [{"role": "user", "content": "hi"}]})
        t0 = clk.t
        assert (await chat()).status_code == 200          # resolves, cached at t0
        await app.state.dynamic_keys.delete(digest_of(body["key"]))  # revoked elsewhere
        clk.t = t0 + 59.9
        assert (await chat()).status_code == 200          # inside the window: still served
        clk.t = t0 + 60.0
        assert (await chat()).status_code == 401          # at the window edge: re-checked, dead
        clk.t = t0 + 60.1
        assert (await chat()).status_code == 401          # after the edge: still dead


async def test_store_error_denies_due_cached_key_and_retries():
    clk = _Clock()
    store = _FlakyStore()
    app = _app(store=store, clock=clk)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as cl:
        body = await _mint(cl)
        h = {"Authorization": f"Bearer {body['key']}"}
        chat = lambda: cl.post("/v1/chat/completions", headers=h,
                               json={"model": "gpt-sim", "messages": [{"role": "user", "content": "hi"}]})
        assert (await chat()).status_code == 200
        store.fail = True
        clk.t += 120  # past the window: revalidation attempted, store errors
        before = store.strict_reads
        assert (await chat()).status_code == 503          # unavailable is not authorized
        assert (await chat()).status_code == 503
        assert store.strict_reads >= before + 2           # not refreshed: retried every request
        store.fail = False
        assert (await chat()).status_code == 200          # recovered: revalidation succeeds again
        await store.delete(digest_of(body["key"]))        # revoked elsewhere, store healthy
        clk.t += 120                                      # past the refreshed window
        assert (await chat()).status_code == 401          # dies on the next check


async def test_failed_deletion_does_not_claim_revocation():
    class FailedDelete(_MemoryStore):
        async def delete(self, digest):
            raise RuntimeError('store unavailable')
    store = FailedDelete()
    app = _app(store=store)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://t') as cl:
        body = await _mint(cl)
        r = await cl.delete('/demo/keys', headers={'Authorization': f"Bearer {body['key']}"})
        assert r.status_code == 503
        assert r.json()['ok'] is False
        assert 'revoked' not in r.json()
        assert await store.get(digest_of(body['key'])) is not None


@pytest.mark.parametrize('status,payload', [(503, {}), (200, {'error': 'down'}),
                                           (200, {}), (200, {'result': None})])
async def test_redis_delete_requires_a_valid_ack(status, payload):
    from types import SimpleNamespace
    from llmgate.demo_keys import _RedisStore
    client = httpx.AsyncClient(base_url='https://store.test', transport=httpx.MockTransport(
        lambda request: httpx.Response(status, json=payload)))
    async with client:
        store = _RedisStore(SimpleNamespace(_client=client))
        with pytest.raises(RuntimeError):
            await store.delete('sha256:test')


@pytest.mark.parametrize('result', [0, 1])
async def test_redis_delete_acknowledged_even_if_retry_finds_it_gone(result):
    from types import SimpleNamespace
    from llmgate.demo_keys import _RedisStore
    client = httpx.AsyncClient(base_url='https://store.test', transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={'result': result})))
    async with client:
        await _RedisStore(SimpleNamespace(_client=client)).delete('sha256:test')


async def test_cold_key_store_error_is_503_not_unknown_key():
    store = _FlakyStore()
    store.fail = True
    app = _app(store=store)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://t') as cl:
        r = await cl.get('/demo/usage', headers={'Authorization': 'Bearer vg_unknown'})
        assert r.status_code == 503
