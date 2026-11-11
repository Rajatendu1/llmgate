"""The public demo stats endpoint behind the landing-page dashboard: aggregated
totals and breaker states, no per-key data, no secrets - plus the short
server-side cache that keeps concurrent viewers from multiplying store reads."""
import httpx
import pytest

from llmgate.demo_app import app

H = {"Authorization": "Bearer demo-key"}
BODY = {"model": "gpt-sim", "messages": [{"role": "user", "content": "hello there world"}]}


@pytest.fixture(autouse=True)
def _fresh_stats():
    app.state.demo_stats_cache = None
    yield
    app.state.demo_stats_cache = None


def c():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


async def test_stats_shape_and_no_per_key_data():
    r = await c().get("/demo/stats")
    assert r.status_code == 200
    j = r.json()
    assert j["ok"] is True and j["metering"] in ("memory", "redis")
    assert j["cache_ttl_s"] > 0
    assert set(j["totals"]) == {"requests", "ok", "rate_limited", "errors",
                                "prompt_tokens", "completion_tokens", "cost_usd"}
    names = [p["name"] for p in j["providers"]]
    assert names == ["sim-a", "sim-b"]  # priority order
    assert all(p["breaker"] in ("closed", "half_open", "open") for p in j["providers"])
    # public by design: no per-key rows, and no key name or secret appears anywhere
    assert "keys" not in j
    assert "demo-key" not in r.text and "admin-key" not in r.text and '"demo"' not in r.text


async def test_stats_reflect_real_traffic_and_cost():
    cl = c()
    before = (await cl.get("/demo/stats")).json()["totals"]
    assert (await cl.post("/v1/chat/completions", json=BODY, headers=H)).status_code == 200
    app.state.demo_stats_cache = None  # a fresh poll after the cache window
    after = (await cl.get("/demo/stats")).json()["totals"]
    assert after["requests"] == before["requests"] + 1
    assert after["ok"] == before["ok"] + 1
    # 3 prompt + 5 completion tokens at sim-a's simulated 0.40/1.20 per 1M
    assert abs((after["cost_usd"] - before["cost_usd"]) - (3 * 0.40 + 5 * 1.20) / 1e6) < 1e-12


async def test_stats_reports_failover_to_the_backup():
    """With the primary down, per-provider counters show the backup serving."""
    from llmgate.demo_app import a as sim_a
    sim_a.down = True
    try:
        cl = c()
        assert (await cl.post("/v1/chat/completions", json=BODY, headers=H)).status_code == 200
        app.state.demo_stats_cache = None
        j = (await cl.get("/demo/stats")).json()
        sim_b = next(p for p in j["providers"] if p["name"] == "sim-b")
        assert sim_b["ok"] >= 1
    finally:
        sim_a.down = False


async def test_stats_cache_serves_repeat_polls_without_rereading(monkeypatch):
    """Polls inside the cache window share one store read; the window expires."""
    calls = 0
    meter = app.state.meter
    real = meter.snapshot

    def counting():
        nonlocal calls
        calls += 1
        return real()

    monkeypatch.setattr(meter, "snapshot", counting)
    cl = c()
    j1 = (await cl.get("/demo/stats")).json()
    j2 = (await cl.get("/demo/stats")).json()
    assert calls == 1 and j1 == j2
    app.state.demo_stats_cache = None
    await cl.get("/demo/stats")
    assert calls == 2
