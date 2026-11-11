"""Size-aware admission and timeouts: max_tokens counts toward the admission
estimate (and settlement credits the unused part back), and a route's timeout
can grow with the estimated request size instead of being one fixed number."""
import httpx
from llmgate.app import ApiKey, create_app
from llmgate.providers import MockProvider, estimate_request_tokens
from llmgate.resilience import TokenBucket
from llmgate.router import Route, Router

H = {"Authorization": "Bearer k"}
ADM = {"Authorization": "Bearer a"}
SMALL = {"model": "m", "messages": [{"role": "user", "content": "hello there world"}]}  # 3 tokens
BIG = {"model": "m", "messages": [{"role": "user", "content": " ".join(["w"] * 200)}]}  # 200 tokens


def client(routes, keys=None):
    app = create_app(Router(routes, failover_ambiguous=True),
                     keys or {"k": ApiKey("tenant"), "a": ApiKey("a", admin=True)})
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


def slow(name, latency=0.1, **kw):
    return MockProvider(name, ["m"], latency=latency, token_delay=0, seed=1, **kw)


def test_estimate_request_tokens_includes_max_tokens():
    assert estimate_request_tokens(SMALL) == 3
    assert estimate_request_tokens({**SMALL, "max_tokens": 50}) == 53
    assert estimate_request_tokens({**SMALL, "max_tokens": -5}) == 3      # invalid ignored
    assert estimate_request_tokens({**SMALL, "max_tokens": "lots"}) == 3  # wrong type ignored


async def test_timeout_grows_with_prompt_size():
    """latency 0.1s; timeout 0.05s + 0.5s per 1k tokens. A 3-token prompt gets
    ~0.05s and times out (fails over); a 200-token prompt gets ~0.15s and is served."""
    routes = [Route(slow("primary"), priority=0, timeout=0.05, timeout_per_1k=0.5),
              Route(slow("backup", latency=0.01), priority=1)]
    c = client(routes)
    r = await c.post("/v1/chat/completions", json=SMALL, headers=H)
    assert r.status_code == 200 and r.headers["x-gateway-provider"] == "backup"
    r = await c.post("/v1/chat/completions", json=BIG, headers=H)
    assert r.status_code == 200 and r.headers["x-gateway-provider"] == "primary"


async def test_timeout_max_caps_the_scaled_timeout():
    """Same scaling, but capped at 0.08s: even the big prompt times out."""
    routes = [Route(slow("primary"), priority=0, timeout=0.05, timeout_per_1k=100.0, timeout_max=0.08),
              Route(slow("backup", latency=0.01), priority=1)]
    c = client(routes)
    r = await c.post("/v1/chat/completions", json=BIG, headers=H)
    assert r.status_code == 200 and r.headers["x-gateway-provider"] == "backup"


async def test_fixed_timeout_is_the_default():
    """timeout_per_1k unset: the timeout is the fixed value for any prompt size."""
    routes = [Route(slow("primary"), priority=0, timeout=0.05),
              Route(slow("backup", latency=0.01), priority=1)]
    c = client(routes)
    r = await c.post("/v1/chat/completions", json=BIG, headers=H)
    assert r.headers["x-gateway-provider"] == "backup"  # still times out at 0.05


async def test_max_tokens_counts_toward_admission():
    keys = {"k": ApiKey("tenant", token_rate=1, token_burst=10), "a": ApiKey("a", admin=True)}
    c = client([Route(MockProvider("a", ["m"], latency=0, token_delay=0, seed=1))], keys)
    r = await c.post("/v1/chat/completions", json={**SMALL, "max_tokens": 20}, headers=H)
    assert r.status_code == 429  # 3 + 20 > burst 10
    assert "token rate limit exceeded" in r.json()["error"]["message"]


async def test_unused_max_tokens_credited_back_at_settlement():
    """Admission debits prompt estimate + max_tokens; the response uses less, so
    settlement credits the difference. Burst 20: 3 + 14 = 17 debited, 8 actually
    used, 9 credited back -> balance 12, so a 10-token follow-up passes."""
    keys = {"k": ApiKey("tenant", token_rate=1, token_burst=20), "a": ApiKey("a", admin=True)}
    c = client([Route(MockProvider("a", ["m"], latency=0, token_delay=0, seed=1))], keys)
    r = await c.post("/v1/chat/completions", json={**SMALL, "max_tokens": 14}, headers=H)
    assert r.status_code == 200
    follow = {"model": "m", "messages": [{"role": "user", "content": " ".join(["w"] * 10)}]}
    assert (await c.post("/v1/chat/completions", json=follow, headers=H)).status_code == 200


def test_charge_credit_never_exceeds_burst():
    b = TokenBucket(rate_per_sec=1, burst=20)
    b.charge(-50)  # a credit larger than the whole bucket
    assert b.tokens == 20
