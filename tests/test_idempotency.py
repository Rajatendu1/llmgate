"""End-to-end Idempotency-Key forwarding: a client-supplied key is passed
verbatim to EVERY upstream attempt of its request - same-provider retries and
failover legs alike - so a provider that honors it can dedupe a duplicated
generation. Absent header => nothing forwarded. HTTPProvider merges it with
its own auth headers (auth wins on conflict)."""
import httpx
from llmgate.app import ApiKey, create_app
from llmgate.providers import HTTPProvider, MockProvider, ProviderError
from llmgate.router import Route, Router

H = {"Authorization": "Bearer k"}
BODY = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
KEY = {"Idempotency-Key": "order-123-v1"}


class DeclinedOnce(MockProvider):
    def _maybe_fail(self):
        self.calls += 1
        if self.calls == 1:
            raise ProviderError(f"{self.name} rate limited (429)", category=ProviderError.DECLINED)


class AlwaysDeclined(MockProvider):
    def _maybe_fail(self):
        self.calls += 1
        raise ProviderError(f"{self.name} rate limited (429)", category=ProviderError.DECLINED)


def make(primary, backup=None, retries=0):
    routes = [Route(primary, priority=0, retries=retries)]
    if backup is not None:
        routes.append(Route(backup, priority=1))
    app = create_app(Router(routes), {"k": ApiKey("t", 50, 50)})
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


async def test_key_forwarded_on_completion():
    p = MockProvider("a", ["m"], latency=0, token_delay=0)
    c = make(p)
    r = await c.post("/v1/chat/completions", json=BODY, headers={**H, **KEY})
    assert r.status_code == 200
    assert p.received_headers == [KEY]


async def test_key_forwarded_on_stream():
    p = MockProvider("a", ["m"], latency=0, token_delay=0)
    c = make(p)
    async with c.stream("POST", "/v1/chat/completions", json={**BODY, "stream": True},
                        headers={**H, **KEY}) as r:
        assert r.status_code == 200
        await r.aread()
    assert p.received_headers == [KEY]


async def test_key_reaches_retry_and_failover_legs():
    p = AlwaysDeclined("a", ["m"], latency=0, token_delay=0)
    b = MockProvider("b", ["m"], latency=0, token_delay=0)
    c = make(p, b, retries=2)
    r = await c.post("/v1/chat/completions", json=BODY, headers={**H, **KEY})
    assert r.status_code == 200
    # primary attempted 3 times (2 retries), backup once; the SAME key on all
    assert p.received_headers == [KEY, KEY, KEY]
    assert b.received_headers == [KEY]


async def test_no_header_means_nothing_forwarded():
    p = DeclinedOnce("a", ["m"], latency=0, token_delay=0)
    c = make(p, retries=1)
    r = await c.post("/v1/chat/completions", json=BODY, headers=H)
    assert r.status_code == 200
    assert p.received_headers == [{}, {}]


async def test_key_forwarded_exactly_as_sent():
    p = MockProvider("a", ["m"], latency=0, token_delay=0)
    c = make(p)
    quirky = "order 123/v1 :: a*b"  # never stripped, trimmed or normalized
    r = await c.post("/v1/chat/completions", json=BODY,
                     headers={**H, "Idempotency-Key": quirky})
    assert r.status_code == 200
    assert p.received_headers == [{"Idempotency-Key": quirky}]


async def test_boundary_255_accepted_256_rejected():
    p = MockProvider("a", ["m"], latency=0, token_delay=0)
    c = make(p)
    ok = await c.post("/v1/chat/completions", json=BODY,
                      headers={**H, "Idempotency-Key": "x" * 255})
    assert ok.status_code == 200
    assert p.received_headers == [{"Idempotency-Key": "x" * 255}]
    p.received_headers.clear()
    bad = await c.post("/v1/chat/completions", json=BODY,
                       headers={**H, "Idempotency-Key": "x" * 256})
    assert bad.status_code == 400
    assert p.received_headers == []  # rejected before any upstream call


async def test_empty_key_rejected():
    p = MockProvider("a", ["m"], latency=0, token_delay=0)
    c = make(p)
    r = await c.post("/v1/chat/completions", json=BODY,
                     headers={**H, "Idempotency-Key": ""})
    assert r.status_code == 400
    assert p.received_headers == []


async def test_http_provider_merges_key_with_auth():
    captured = {}

    def handler(req):
        captured.update(req.headers)
        return httpx.Response(200, json={
            "id": "x", "choices": [{"message": {"role": "assistant", "content": "hi"},
                                    "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})

    p = HTTPProvider("up", "http://up/v1", "up-secret", ["m"],
                     client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    await p.complete(BODY, headers=KEY)
    assert captured["idempotency-key"] == "order-123-v1"
    assert captured["authorization"] == "Bearer up-secret"  # provider auth preserved


async def test_http_provider_own_headers_win_on_conflict():
    captured = {}

    def handler(req):
        captured.update(req.headers)
        return httpx.Response(200, json={
            "id": "x", "choices": [{"message": {"role": "assistant", "content": "hi"},
                                    "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})

    p = HTTPProvider("up", "http://up/v1", "up-secret", ["m"],
                     client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    await p.complete(BODY, headers={"Authorization": "Bearer evil", "Idempotency-Key": "ik"})
    assert captured["authorization"] == "Bearer up-secret"
    assert captured["idempotency-key"] == "ik"


class OldSignatureProvider:
    """A custom provider written before end-to-end headers existed."""
    name, models, mode = "old", {"m"}, "simulated"

    async def complete(self, body):
        return {"id": "x", "choices": [{"message": {"role": "assistant", "content": "hi"},
                                        "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}


class KwargsProvider(MockProvider):
    """Takes **kwargs: must receive the key like a modern provider."""
    async def complete(self, body, **kw):
        return await super().complete(body, **kw)


async def test_old_signature_provider_still_serves_keyed_requests():
    c = make(OldSignatureProvider())
    r = await c.post("/v1/chat/completions", json=BODY, headers={**H, **KEY})
    assert r.status_code == 200
    assert r.json()["choices"][0]["message"]["content"] == "hi"


async def test_kwargs_provider_receives_the_key():
    p = KwargsProvider("a", ["m"], latency=0, token_delay=0)
    c = make(p)
    r = await c.post("/v1/chat/completions", json=BODY, headers={**H, **KEY})
    assert r.status_code == 200
    assert p.received_headers == [KEY]


class DeclinedOldSignatureProvider(OldSignatureProvider):
    """Old signature AND always rate-limited: keyed requests must still fail over."""
    async def complete(self, body):
        raise ProviderError(f"{self.name} rate limited (429)", category=ProviderError.DECLINED)


async def test_failover_from_old_to_new_still_delivers_the_key():
    b = MockProvider("b", ["m"], latency=0, token_delay=0)
    c = make(DeclinedOldSignatureProvider(), b)
    r = await c.post("/v1/chat/completions", json=BODY, headers={**H, **KEY})
    assert r.status_code == 200
    assert b.received_headers == [KEY]
