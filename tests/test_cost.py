"""Estimated cost metering: the config price table applied to upstream-reported
usage, plus the settlement edges the docs promise (the admission estimate is not
refunded on upstream failure; a stream without a usage chunk settles nothing)."""
import httpx
from llmgate.app import ApiKey, create_app
from llmgate.providers import MockProvider
from llmgate.router import Price, Route, Router

H = {"Authorization": "Bearer k"}
ADM = {"Authorization": "Bearer a"}
BODY = {"model": "m", "messages": [{"role": "user", "content": "hello there world"}]}
# MockProvider replies "[a] echo: hello there world": 3 prompt + 5 completion tokens
EXPECTED_COST = (3 * 0.40 + 5 * 1.20) / 1e6


def client(routes, keys=None):
    app = create_app(Router(routes, failover_ambiguous=True),  # simulated providers
                     keys or {"k": ApiKey("tenant"), "a": ApiKey("a", admin=True)})
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


def token_client(routes):
    return client(routes, {"k": ApiKey("tenant", token_rate=1, token_burst=20),
                           "a": ApiKey("a", admin=True)})


def mp(name, **kw):
    return MockProvider(name, ["m"], latency=0, token_delay=0, seed=1, **kw)


async def test_price_math():
    p = Price(0.40, 1.20)
    assert p.cost({"prompt_tokens": 1_000_000, "completion_tokens": 500_000}) == 1.0
    assert p.cost(None) == 0.0 and p.cost({}) == 0.0


async def test_usage_reports_estimated_cost():
    c = client([Route(mp("a"), price=Price(0.40, 1.20))])
    assert (await c.post("/v1/chat/completions", json=BODY, headers=H)).status_code == 200
    usage = (await c.get("/admin/usage", headers=ADM)).json()["keys"]["tenant"]
    assert abs(usage["cost_usd"] - EXPECTED_COST) < 1e-12


async def test_unpriced_route_records_no_cost():
    c = client([Route(mp("a"))])
    assert (await c.post("/v1/chat/completions", json=BODY, headers=H)).status_code == 200
    usage = (await c.get("/admin/usage", headers=ADM)).json()["keys"]["tenant"]
    assert "cost_usd" not in usage


async def test_failover_is_charged_at_the_backup_price():
    routes = [Route(mp("cheap", down=True), priority=0, price=Price(0.40, 1.20)),
              Route(mp("pricey"), priority=1, price=Price(0.80, 2.40))]
    c = client(routes)
    r = await c.post("/v1/chat/completions", json=BODY, headers=H)
    assert r.status_code == 200 and r.headers["x-gateway-provider"] == "pricey"
    usage = (await c.get("/admin/usage", headers=ADM)).json()["keys"]["tenant"]
    # "[pricey] echo: hello there world": 5 completion tokens, at the backup's price
    assert abs(usage["cost_usd"] - (3 * 0.80 + 5 * 2.40) / 1e6) < 1e-12


async def test_stream_records_cost_from_usage_chunk():
    c = client([Route(mp("a"), price=Price(0.40, 1.20))])
    async with c.stream("POST", "/v1/chat/completions", json={**BODY, "stream": True}, headers=H) as r:
        assert r.status_code == 200
        await r.aread()
    usage = (await c.get("/admin/usage", headers=ADM)).json()["keys"]["tenant"]
    assert abs(usage["cost_usd"] - EXPECTED_COST) < 1e-12


async def test_metrics_exposes_cost_counter():
    c = client([Route(mp("a"), price=Price(0.40, 1.20))])
    await c.post("/v1/chat/completions", json=BODY, headers=H)
    text = (await c.get("/metrics", headers=ADM)).text
    line = next(l for l in text.splitlines() if l.startswith("llmgate_cost_usd_total{"))
    assert line.startswith('llmgate_cost_usd_total{key="tenant"} ')
    assert abs(float(line.split()[-1]) - EXPECTED_COST) < 1e-12


async def test_prompt_estimate_not_refunded_on_upstream_failure():
    """Documented edge: the pre-flight token estimate is debited at admission and
    not refunded when the upstream fails. Burst 20, estimate 3 -> 17 left, so an
    18-token follow-up is rejected even though the first call never generated."""
    c = token_client([Route(mp("a", down=True))])
    r = await c.post("/v1/chat/completions", json=BODY, headers=H)
    assert r.status_code == 502
    big = {"model": "m", "messages": [{"role": "user", "content": " ".join(["w"] * 18)}]}
    assert (await c.post("/v1/chat/completions", json=big, headers=H)).status_code == 429


class NoUsageStreamProvider(MockProvider):
    """Streams chunks and a stop frame but never reports usage - some providers
    don't, and failures or disconnects can lose the usage chunk too."""
    async def stream(self, body):
        for w in self._reply(body).split(" "):
            yield {"object": "chat.completion.chunk", "model": body["model"],
                   "choices": [{"index": 0, "delta": {"content": w + " "}, "finish_reason": None}]}
        yield {"object": "chat.completion.chunk", "model": body["model"],
               "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}  # no usage chunk


FOLLOW_UP = {"model": "m", "messages": [{"role": "user", "content": " ".join(["w"] * 13)}]}


async def test_stream_without_usage_settles_nothing_beyond_the_estimate():
    """No usage chunk -> no settlement: only the admission estimate (3) is
    debited, so 17 remain and a 13-token follow-up still passes."""
    c = token_client([Route(NoUsageStreamProvider("a", ["m"]))])
    async with c.stream("POST", "/v1/chat/completions", json={**BODY, "stream": True}, headers=H) as r:
        assert r.status_code == 200
        await r.aread()
    assert (await c.post("/v1/chat/completions", json=FOLLOW_UP, headers=H)).status_code == 200


async def test_stream_with_usage_settles_the_full_reported_total():
    """Contrast case: the mock reports 3 prompt + 5 completion = 8 tokens, settled
    post-response, so 12 remain and the same 13-token follow-up is rejected."""
    c = token_client([Route(mp("a"))])
    async with c.stream("POST", "/v1/chat/completions", json={**BODY, "stream": True}, headers=H) as r:
        assert r.status_code == 200
        await r.aread()
    assert (await c.post("/v1/chat/completions", json=FOLLOW_UP, headers=H)).status_code == 429
