"""Application shutdown owns created resources, not borrowed client lifetimes."""
import httpx
import pytest
from llmgate.app import ApiKey, create_app
from llmgate.providers import HTTPProvider, MockProvider
from llmgate.router import Route, Router
from llmgate.demo_keys import install_demo_keys


def app_for(*providers, **kwargs):
    return create_app(Router([Route(p) for p in providers]), {'k': ApiKey('test')}, **kwargs)


async def test_provider_owned_client_open_until_lifespan_exit():
    p = HTTPProvider('up', 'https://up.test/v1', 'secret', ['m'])
    app = app_for(p)
    async with app.router.lifespan_context(app):
        assert not p.client.is_closed
    assert p.client.is_closed
    await p.aclose()  # idempotent


async def test_borrowed_provider_client_stays_open():
    async with httpx.AsyncClient() as client:
        p = HTTPProvider('up', 'https://up.test/v1', 'secret', ['m'], client=client)
        app = app_for(p)
        async with app.router.lifespan_context(app):
            pass
        assert not client.is_closed


async def test_same_provider_on_two_routes_closes_once():
    class Count(MockProvider):
        closed = 0
        async def aclose(self):
            self.closed += 1
    p = Count('p', ['m'])
    app = app_for(p, p)
    async with app.router.lifespan_context(app):
        pass
    assert p.closed == 1


async def test_cleanup_continues_after_failure_without_leaking_error():
    class Bad(MockProvider):
        async def aclose(self):
            raise RuntimeError('credential-secret-do-not-report')
    p = HTTPProvider('up', 'https://up.test/v1', 'secret', ['m'])
    app = app_for(p, Bad('bad', ['m']))
    with pytest.raises(RuntimeError, match='1 gateway resource cleanup') as error:
        async with app.router.lifespan_context(app):
            pass
    assert 'credential' not in str(error.value)
    assert p.client.is_closed


async def test_shutdown_runs_when_application_body_raises():
    p = HTTPProvider('up', 'https://up.test/v1', 'secret', ['m'])
    app = app_for(p)
    with pytest.raises(ValueError):
        async with app.router.lifespan_context(app):
            raise ValueError('body failure')
    assert p.client.is_closed


async def test_default_shared_limiter_and_demo_store_clients_close(monkeypatch):
    monkeypatch.setenv('UPSTASH_REDIS_REST_URL', 'https://store.test')
    monkeypatch.setenv('UPSTASH_REDIS_REST_TOKEN', 'test-secret')
    app = app_for(MockProvider('sim', ['gpt-sim']))
    install_demo_keys(app)
    limiter = app.state.limiter
    store = app.state.dynamic_keys
    assert app.state.meter._client is limiter._client
    assert app.state.reqlog._client is limiter._client
    async with app.router.lifespan_context(app):
        assert not limiter._client.is_closed
        assert not store._rl._client.is_closed
    assert limiter._client.is_closed
    assert store._rl._client.is_closed


async def test_injected_resources_remain_caller_owned():
    class Borrowed:
        closed = False
        async def aclose(self):
            self.closed = True
    limiter, meter, store = Borrowed(), Borrowed(), Borrowed()
    app = app_for(MockProvider('sim', ['gpt-sim']), limiter=limiter, meter=meter)
    install_demo_keys(app, store=store)
    async with app.router.lifespan_context(app):
        pass
    assert not limiter.closed and not meter.closed and not store.closed
