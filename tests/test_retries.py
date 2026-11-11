"""Same-provider retries with backoff apply ONLY to declined requests (connect
errors and 429s, which provably never reached generation). Ambiguous failures
(timeout, 408, 5xx) are never retried in place - the request may have generated
upstream - and fail over only when failover_ambiguous is on. Never retried
mid-stream; always bounded; failover preserved when retries are exhausted."""
import httpx, pytest
from llmgate.app import ApiKey, create_app
from llmgate.providers import MockProvider, ProviderError
from llmgate.router import Route, Router

H = {"Authorization": "Bearer k"}
BODY = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}


class DeclinedProvider(MockProvider):
    """Fails with a DECLINED error (e.g. connect failure / 429) `fails` times, then answers."""

    def __init__(self, name, models, fails=0, **kw):
        super().__init__(name, models, latency=0, token_delay=0, **kw)
        self.fails_left = fails

    def _maybe_fail(self):
        self.calls += 1
        if self.fails_left > 0:
            self.fails_left -= 1
            raise ProviderError(f"{self.name} rate limited (429)", category=ProviderError.DECLINED)


class AmbiguousProvider(MockProvider):
    """Fails with a 500: the request may have generated upstream."""

    def _maybe_fail(self):
        self.calls += 1
        raise ProviderError(f"{self.name} transient error (500)", category=ProviderError.AMBIGUOUS)


class TimeoutProvider(MockProvider):
    def _maybe_fail(self):
        self.calls += 1
        import asyncio
        raise asyncio.TimeoutError  # surfaced through wait_for as well


def make(primary, backup=None, retries=1, sleeps=None, failover_ambiguous=False):
    sleeps_box = sleeps if sleeps is not None else []

    async def fake_sleep(d):
        sleeps_box.append(d)

    routes = [Route(primary, priority=0, retries=retries, retry_backoff=0.25)]
    if backup is not None:
        routes.append(Route(backup, priority=1))
    router = Router(routes, sleep=fake_sleep, failover_ambiguous=failover_ambiguous)
    app = create_app(router, {"k": ApiKey("t", 50, 50)})
    return app, httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


async def test_declined_request_retried_same_provider_then_succeeds():
    p = DeclinedProvider("a", ["m"], fails=2)
    app, c = make(p, retries=2)
    r = await c.post("/v1/chat/completions", json=BODY, headers=H)
    assert r.status_code == 200
    assert p.calls == 3  # initial + 2 retries
    assert r.headers["x-gateway-attempts"] == "1"  # one provider tried, retried in place


async def test_retry_uses_backoff_and_is_bounded():
    p = DeclinedProvider("a", ["m"], fails=99)
    b = MockProvider("b", ["m"], latency=0, seed=2)
    sleeps = []
    app, c = make(p, backup=b, retries=2, sleeps=sleeps)
    r = await c.post("/v1/chat/completions", json=BODY, headers=H)
    assert r.status_code == 200 and r.headers["x-gateway-provider"] == "b"  # fell over
    assert p.calls == 3  # 1 + 2 retries, then gave up
    assert len(sleeps) == 2 and 0 < sleeps[0] < sleeps[1] * 2  # growing, jittered


async def test_ambiguous_5xx_never_retried_in_place():
    p = AmbiguousProvider("a", ["m"])
    b = MockProvider("b", ["m"], latency=0, seed=2)
    app, c = make(p, backup=b, retries=3, failover_ambiguous=True)  # availability opt-in
    r = await c.post("/v1/chat/completions", json=BODY, headers=H)
    assert r.status_code == 200 and r.headers["x-gateway-provider"] == "b"
    assert p.calls == 1  # one attempt only: no in-place retry of a possible generation


async def test_ambiguous_failure_with_failover_disabled_surfaces_error():
    p = AmbiguousProvider("a", ["m"])
    b = MockProvider("b", ["m"], latency=0, seed=2)
    app, c = make(p, backup=b, retries=3, failover_ambiguous=False)
    r = await c.post("/v1/chat/completions", json=BODY, headers=H)
    assert r.status_code == 502  # upstream detail stays out of the client response
    assert p.calls == 1 and b.calls == 0  # no duplicate generation anywhere


async def test_no_retry_on_timeout():
    p = TimeoutProvider("a", ["m"])
    b = MockProvider("b", ["m"], latency=0, seed=2)
    app, c = make(p, backup=b, retries=3, failover_ambiguous=True)
    r = await c.post("/v1/chat/completions", json=BODY, headers=H)
    assert r.status_code == 200 and r.headers["x-gateway-provider"] == "b"
    assert p.calls == 1  # timeout is ambiguous: no same-provider retry


async def test_non_retryable_error_still_raises_immediately():
    class BadRequest(MockProvider):
        def _maybe_fail(self):
            self.calls += 1
            raise ProviderError("bad request (400)", category=ProviderError.FATAL)

    p = BadRequest("a", ["m"])
    b = MockProvider("b", ["m"], latency=0, seed=2)
    app, c = make(p, backup=b, retries=3)
    r = await c.post("/v1/chat/completions", json=BODY, headers=H)
    assert r.status_code == 502 and p.calls == 1 and b.calls == 0


async def test_stream_declined_failure_retried_once_then_fails_over():
    p = DeclinedProvider("a", ["m"], fails=5)
    b = MockProvider("b", ["m"], latency=0, token_delay=0, seed=2)
    app, c = make(p, backup=b, retries=2)
    body = {**BODY, "stream": True}
    r = await c.post("/v1/chat/completions", json=body, headers=H)
    assert r.status_code == 200 and r.headers["x-gateway-provider"] == "b"
    assert p.calls == 3
    text = r.text
    assert "echo: " in text and "[DONE]" in text  # chunks split words


async def test_stream_ambiguous_failure_not_retried_in_place():
    p = AmbiguousProvider("a", ["m"])
    b = MockProvider("b", ["m"], latency=0, token_delay=0, seed=2)
    app, c = make(p, backup=b, retries=2, failover_ambiguous=True)
    body = {**BODY, "stream": True}
    r = await c.post("/v1/chat/completions", json=body, headers=H)
    assert r.status_code == 200 and r.headers["x-gateway-provider"] == "b"
    assert p.calls == 1


async def test_router_default_is_duplicate_averse():
    # Plain Router(...) (no opt-in) must NOT fail over after an ambiguous failure.
    router = Router([Route(AmbiguousProvider("a", ["m"])),
                     Route(MockProvider("b", ["m"], latency=0, seed=2), priority=1)])
    assert router.failover_ambiguous is False
