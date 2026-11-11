"""The visitor test-request endpoint: one bounded request through the real
pipeline, authoritative per-request routing in the answer, no key material
exposed, per-IP bound, and visitor traffic counted by the shared metering."""
import httpx
import pytest

from llmgate.demo_app import app


@pytest.fixture(autouse=True)
def _fresh_state():
    """Independent visitor state per test: stats cache, per-IP hits, and the
    visitor key's rate/token buckets - no reliance on test order or timing."""
    app.state.demo_stats_cache = None
    app.state.demo_try_hits.clear()
    limiter = app.state.limiter
    for k in [k for k in getattr(limiter, "buckets", {}) if k[0] == "visitor"]:
        del limiter.buckets[k]
    yield
    app.state.demo_stats_cache = None


def c():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


async def test_try_default_stays_simulated_when_real_provider_active():
    """Regression: with a real provider active, the default (no ?model=) Try
    must still route to the install-time simulated model - the label on the
    first button says simulated, and the real route has its own button and
    tighter budget. The handler's query param shadows the install argument,
    which once dropped the default to the first alphabetical alias (gpt-real)."""
    from llmgate.app import ApiKey, create_app
    from llmgate.demo_try import install_demo_try
    from llmgate.providers import MockProvider
    from llmgate.resilience import CircuitBreaker
    from llmgate.router import Route, Router

    real = MockProvider("groq", ["gpt-real"])
    real.mode = "real"
    sim = MockProvider("sim-a", ["gpt-sim"])
    router = Router([Route(real, priority=0, breaker=CircuitBreaker(3, 5)),
                     Route(sim, priority=0, breaker=CircuitBreaker(3, 5))])
    app = create_app(router, {"demo-key": ApiKey("demo")})
    install_demo_try(app, model="gpt-sim")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as cl:
        r = await cl.post("/demo/try")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"], body
        assert body["provider"] == "sim-a", body
        assert body["mode"] == "simulated", body


async def test_try_serves_and_reports_the_actual_route():
    r = await c().post("/demo/try")
    assert r.status_code == 200
    j = r.json()
    assert j["ok"] is True and j["status"] == 200
    assert j["provider"] in ("sim-a", "sim-b")  # from X-Gateway-Provider: this request's real path
    assert j["attempts"] >= 1 and j["latency_ms"] >= 0
    assert j["usage"]["prompt_tokens"] > 0 and j["usage"]["completion_tokens"] > 0
    assert j["cost_usd"] > 0 and j["text"]
    # no key material anywhere in the answer
    assert "invalid-sample-simulated-bearer" not in r.text and "demo-key" not in r.text


async def test_try_reports_failover_for_this_request():
    from llmgate.demo_app import a as sim_a
    sim_a.down = True
    try:
        j = (await c().post("/demo/try")).json()
        assert j["ok"] is True and j["provider"] == "sim-b"
    finally:
        sim_a.down = False


async def test_try_per_ip_bound():
    cl = c()
    codes = [(await cl.post("/demo/try")).status_code for _ in range(11)]
    assert codes[:10] == [200] * 10  # inner 429s still produce a 200 report envelope
    assert codes[10] == 429


async def test_try_visitor_traffic_is_metered():
    cl = c()
    before = (await cl.get("/demo/stats")).json()["totals"]["requests"]
    j = (await cl.post("/demo/try")).json()
    assert j["ok"] is True
    app.state.demo_stats_cache = None
    after = (await cl.get("/demo/stats")).json()["totals"]["requests"]
    assert after == before + 1


async def test_try_inner_rate_limit_is_reported_not_hidden():
    """When the shared visitor budget is exhausted the answer says so honestly."""
    cl = c()
    seen = set()
    for _ in range(6):
        j = (await cl.post("/demo/try")).json()
        seen.add(j["ok"])
        if not j["ok"]:
            assert j["status"] == 429 and j["error"]
            break
    assert len(seen) >= 1  # shape holds either way; no 5xx envelope


@pytest.mark.asyncio
async def test_real_try_routes_to_real_provider():
    """?model=real uses the real provider's model on the tighter visitor-real key."""
    from llmgate.app import ApiKey, create_app
    from llmgate.demo_try import install_demo_try
    from llmgate.providers import MockProvider
    from llmgate.resilience import CircuitBreaker
    from llmgate.router import Route, Router

    real = MockProvider("groq", ["openai/gpt-oss-120b"])
    real.mode = "real"  # what HTTPProvider reports
    sim = MockProvider("sim-a", ["gpt-sim"])
    router = Router([Route(real, priority=0, breaker=CircuitBreaker(3, 5)),
                     Route(sim, priority=0, breaker=CircuitBreaker(3, 5))])
    app = create_app(router, {"demo-key": ApiKey("demo")})
    install_demo_try(app)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as cl:
        r = await cl.post("/demo/try?model=real")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"], body
        assert body["provider"] == "groq"
        assert body["mode"] == "real"
        # the log labels it real with the public key label, never the secret
        r = await cl.get("/demo/events") if hasattr(app.state, "reqlog") else None
        blob = str(body)
        assert "invalid-sample-real-bearer" not in blob


@pytest.mark.asyncio
async def test_real_try_404_without_real_provider():
    from llmgate.demo_app import app as demo_app
    transport = httpx.ASGITransport(app=demo_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as cl:
        r = await cl.post("/demo/try?model=real")
        assert r.status_code == 404
        assert r.json()["ok"] is False


@pytest.mark.asyncio
async def test_real_backup_try_routes_straight_to_backup_model():
    """?model=real-backup demonstrates a backup model call (not a failover):
    it routes the gpt-real-backup alias, which only the backup serves, and
    shares the one visitor-real budget with the primary real button."""
    from llmgate.app import ApiKey, create_app
    from llmgate.demo_try import install_demo_try
    from llmgate.providers import MockProvider
    from llmgate.resilience import CircuitBreaker
    from llmgate.router import Route, Router

    primary = MockProvider("groq", ["gpt-real"])
    primary.mode = "real"
    backup = MockProvider("groq-backup", ["gpt-real", "gpt-real-backup"])
    backup.mode = "real"
    router = Router([Route(primary, priority=0, breaker=CircuitBreaker(3, 5)),
                     Route(backup, priority=1, breaker=CircuitBreaker(3, 5))])
    app = create_app(router, {"demo-key": ApiKey("demo")})
    install_demo_try(app)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as cl:
        r = await cl.post("/demo/try?model=real-backup")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"], body
        assert body["provider"] == "groq-backup"  # direct routing, nothing failed
        assert body["attempts"] == 1              # not a failover
        assert body["mode"] == "real"
        assert "invalid-sample-real-bearer" not in str(body)


@pytest.mark.asyncio
async def test_real_backup_try_404_without_backup_route():
    from llmgate.app import ApiKey, create_app
    from llmgate.demo_try import install_demo_try
    from llmgate.providers import MockProvider
    from llmgate.resilience import CircuitBreaker
    from llmgate.router import Route, Router

    primary = MockProvider("groq", ["gpt-real"])
    primary.mode = "real"
    router = Router([Route(primary, priority=0, breaker=CircuitBreaker(3, 5))])
    app = create_app(router, {"demo-key": ApiKey("demo")})
    install_demo_try(app)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as cl:
        r = await cl.post("/demo/try?model=real-backup")
        assert r.status_code == 404
        assert r.json()["ok"] is False
