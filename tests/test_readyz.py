"""Readiness probe: binary 200/503 for monitors, dependency-aware, secret-free."""
import httpx
from llmgate.app import ApiKey, create_app
from llmgate.metering import Meter
from llmgate.providers import MockProvider
from llmgate.redislimit import RedisRateLimiter
from llmgate.resilience import RateLimiter
from llmgate.router import Route, Router


def make(limiter=None):
    router = Router([Route(MockProvider("a", ["m"], latency=0))])
    app = create_app(router, {"supersecret-value": ApiKey("t")}, limiter=limiter or RateLimiter(), meter=Meter())
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


async def test_readyz_200_with_healthy_backends():
    c = make()
    r = await c.get("/readyz")
    assert r.status_code == 200 and r.json() == {"ok": True}


async def test_readyz_503_when_shared_store_unreachable():
    limiter = RedisRateLimiter.__new__(RedisRateLimiter)  # shared backend, no real client

    async def down():
        return False

    limiter.ping = down
    c = make(limiter=limiter)
    r = await c.get("/readyz")
    assert r.status_code == 503
    body = r.json()
    assert body["ok"] is False and "rate_limit_store_unreachable" in body["problems"]
    assert "supersecret" not in r.text  # no secrets, no key material
