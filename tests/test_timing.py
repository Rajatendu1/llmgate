"""Measured per-stage timings: the Server-Timing response header on chat
requests and the Try route's forwarded timings_ms."""
import httpx

from llmgate.app import ApiKey, create_app
from llmgate.demo_try import install_demo_try
from llmgate.providers import MockProvider
from llmgate.resilience import CircuitBreaker
from llmgate.router import Route, Router


def _app():
    sim = MockProvider("sim-a", ["gpt-sim"], latency=0.05)
    app = create_app(Router([Route(sim, priority=0, breaker=CircuitBreaker(3, 5))]),
                     {"demo-key": ApiKey("demo", rate=50, burst=100)})
    install_demo_try(app, model="gpt-sim")
    return app


def _parse(header):
    out = {}
    for part in header.split(","):
        if ";dur=" in part:
            name, dur = part.split(";dur=", 1)
            out[name.strip()] = float(dur)
    return out


async def test_server_timing_on_completion():
    app = _app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as cl:
        r = await cl.post("/v1/chat/completions", headers={"Authorization": "Bearer demo-key"},
                          json={"model": "gpt-sim", "messages": [{"role": "user", "content": "hi"}]})
        assert r.status_code == 200, r.text
        t = _parse(r.headers["server-timing"])
        assert set(t) == {"auth", "admission", "routing", "provider", "metering"}
        assert all(v >= 0 for v in t.values())
        assert t["provider"] >= 40  # the mock's 50 ms latency dominates


async def test_server_timing_on_refusal_stops_at_the_refusing_stage():
    app = _app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as cl:
        r = await cl.post("/v1/chat/completions", headers={"Authorization": "Bearer demo-key"},
                          json={"model": "nope", "messages": [{"role": "user", "content": "hi"}]})
        assert r.status_code == 404
        t = _parse(r.headers["server-timing"])
        assert "auth" in t and "admission" in t
        assert "provider" not in t and "metering" not in t


async def test_server_timing_on_stream_is_partial():
    app = _app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as cl:
        r = await cl.post("/v1/chat/completions", headers={"Authorization": "Bearer demo-key"},
                          json={"model": "gpt-sim", "stream": True,
                                "messages": [{"role": "user", "content": "hi"}]})
        assert r.status_code == 200, r.text
        t = _parse(r.headers["server-timing"])
        assert {"auth", "admission", "routing", "provider"} <= set(t)
        assert "metering" not in t  # headers precede the stream body


async def test_try_route_forwards_measured_timings():
    app = _app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as cl:
        r = await cl.post("/demo/try")
        assert r.status_code == 200, r.text
        t = r.json()["timings_ms"]
        assert set(t) == {"auth", "admission", "routing", "provider", "metering"}
        assert t["provider"] >= 40  # same measured span the header carried
