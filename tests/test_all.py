import json, random
import httpx, pytest
from llmgate.app import ApiKey, create_app
from llmgate.providers import MockProvider, HTTPProvider
from llmgate.resilience import CircuitBreaker, TokenBucket
from llmgate.router import Route, Router

H = {"Authorization": "Bearer k"}
BODY = {"model": "m", "messages": [{"role": "user", "content": "hello there world"}]}


class Clock:
    t = 0.0
    def __call__(self): return self.t


def client(routes, clock=None, rate=100, burst=100, keykw=None):
    # simulated providers: availability-first failover is explicitly opted into
    app = create_app(Router(routes, random.Random(1), failover_ambiguous=True),
                     {"k": ApiKey("tenant", rate, burst, **(keykw or {})), "adm": ApiKey("adm", admin=True)},
                     **({"clock": clock} if clock else {}))
    return app, httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


def mp(name, **kw):
    return MockProvider(name, ["m"], latency=0, token_delay=0, seed=1, **kw)


# --- unit
def test_breaker_lifecycle():
    c = Clock(); b = CircuitBreaker(2, 10, c)
    b.failure(); assert b.allow()
    b.failure(); assert b.state == "open" and not b.allow()
    c.t = 11; assert b.state == "half_open" and b.allow() and not b.allow()  # single probe
    b.failure(); assert b.state == "open"
    c.t = 22; assert b.allow(); b.success(); assert b.state == "closed"


def test_token_bucket():
    c = Clock(); tb = TokenBucket(1, 2, c)
    assert tb.take()[0] and tb.take()[0]
    ok, wait = tb.take(); assert not ok and wait == pytest.approx(1.0)
    c.t = 1; assert tb.take()[0]


def test_priority_and_weights():
    r = Router([Route(mp("lo"), priority=1), Route(mp("hi1"), 0, 1), Route(mp("hi2"), 0, 9)], random.Random(0))
    firsts = [r.order("m")[0].provider.name for _ in range(2000)]
    assert "lo" not in firsts and firsts.count("hi2") > 4 * firsts.count("hi1")
    assert all(r.order("m")[-1].provider.name == "lo" for _ in range(50))


# --- integration
async def test_auth_and_validation():
    _, c = client([Route(mp("a"))])
    assert (await c.post("/v1/chat/completions", json=BODY)).status_code == 401
    assert (await c.post("/v1/chat/completions", json=BODY, headers={"Authorization": "Bearer x"})).status_code == 401
    assert (await c.post("/v1/chat/completions", json={"model": "m"}, headers=H)).status_code == 400
    assert (await c.post("/v1/chat/completions", json={**BODY, "model": "zz"}, headers=H)).status_code == 404
    assert (await c.get("/admin/usage", headers=H)).status_code == 401


async def test_basic_completion_and_metering():
    app, c = client([Route(mp("a"))])
    r = await c.post("/v1/chat/completions", json=BODY, headers=H)
    assert r.status_code == 200 and r.headers["x-gateway-provider"] == "a"
    assert r.json()["usage"]["prompt_tokens"] == 3
    u = (await c.get("/admin/usage", headers={"Authorization": "Bearer adm"})).json()
    assert u["keys"]["tenant"]["requests"] == 1 and u["keys"]["tenant"]["prompt_tokens"] == 3


async def test_failover_non_stream_and_breaker_opens():
    bad, good = mp("bad", down=True), mp("good")
    app, c = client([Route(bad, 0, breaker=CircuitBreaker(2, 100)), Route(good, 1)])
    for _ in range(5):
        r = await c.post("/v1/chat/completions", json=BODY, headers=H)
        assert r.status_code == 200 and r.headers["x-gateway-provider"] == "good"
    assert bad.calls == 2  # breaker opened after 2 failures, then bad skipped
    assert (await c.get("/healthz")).json()["providers"]["bad"] == "open"


async def test_all_down_gives_502():
    _, c = client([Route(mp("a", down=True)), Route(mp("b", down=True))])
    r = await c.post("/v1/chat/completions", json=BODY, headers=H)
    assert r.status_code == 502 and "a:" not in r.json()["error"]["message"]  # no upstream details leak


def sse(text):
    return [json.loads(l[6:]) for l in text.split("\n\n") if l.startswith("data: ") and l != "data: [DONE]"], text.rstrip().endswith("[DONE]")


async def test_stream_and_failover_before_first_chunk():
    app, c = client([Route(mp("bad", down=True), 0), Route(mp("good"), 1)])
    r = await c.post("/v1/chat/completions", json={**BODY, "stream": True}, headers=H)
    chunks, done = sse(r.text)
    assert r.headers["content-type"].startswith("text/event-stream") and done
    assert r.headers["x-gateway-attempts"] == "2"
    assert "".join(ch["choices"][0]["delta"].get("content", "") for ch in chunks) == "[good] echo: hello there world"
    assert app.state.meter.snapshot("tenant")["completion_tokens"] > 0


class DiesMidStream(MockProvider):
    async def stream(self, body):
        from llmgate.providers import ProviderError
        async for i, ch in _enum(super().stream(body)):
            if i == 2:
                raise ProviderError("boom")
            yield ch

async def _enum(it):
    i = 0
    async for x in it:
        yield i, x; i += 1


async def test_midstream_failure_is_surfaced_not_spliced():
    other = mp("other")
    _, c = client([Route(DiesMidStream("dies", ["m"], latency=0, token_delay=0), 0), Route(other, 1)])
    r = await c.post("/v1/chat/completions", json={**BODY, "stream": True}, headers=H)
    assert "stream_error" in r.text and "[DONE]" not in r.text and other.calls == 0


async def test_rate_limit_per_key():
    clk = Clock()
    app, c = client([Route(mp("a"))], clock=clk, rate=1, burst=2)
    codes = [(await c.post("/v1/chat/completions", json=BODY, headers=H)).status_code for _ in range(3)]
    assert codes == [200, 200, 429]
    r = await c.post("/v1/chat/completions", json=BODY, headers=H)
    assert r.headers["retry-after"] == "1"
    clk.t = 5
    assert (await c.post("/v1/chat/completions", json=BODY, headers=H)).status_code == 200
    assert app.state.meter.snapshot("tenant")["status_rate_limited"] == 2


async def test_http_provider_against_fake_openai_upstream():
    def handler(req: httpx.Request):
        b = json.loads(req.content)
        assert req.headers["authorization"] == "Bearer up-key"
        if b.get("stream"):
            lines = [json.dumps({"choices": [{"delta": {"content": "hi"}}]}),
                     json.dumps({"choices": [], "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})]
            return httpx.Response(200, text="".join(f"data: {l}\n\n" for l in lines) + "data: [DONE]\n\n")
        return httpx.Response(200, json={"choices": [{"message": {"content": "hi"}}], "usage": {"prompt_tokens": 1, "completion_tokens": 1}})
    p = HTTPProvider("up", "http://up/v1", "up-key", ["m"], client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    _, c = client([Route(p)])
    assert (await c.post("/v1/chat/completions", json=BODY, headers=H)).json()["choices"][0]["message"]["content"] == "hi"
    chunks, done = sse((await c.post("/v1/chat/completions", json={**BODY, "stream": True}, headers=H)).text)
    assert done and chunks[-1]["usage"]["total_tokens"] == 2


async def test_http_provider_error_classification():
    p = HTTPProvider("up", "http://up/v1", "k", ["m"], client=httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(400, text="bad"))))
    _, c = client([Route(p), Route(mp("backup"), 1)])
    r = await c.post("/v1/chat/completions", json=BODY, headers=H)
    assert r.status_code == 502  # 400 is not retryable: no failover


async def test_oversize_and_bad_stream_flag_rejected():
    _, c = client([Route(mp("a"))])
    big = {**BODY, "messages": [{"role": "user", "content": "x" * 1_100_000}]}
    assert (await c.post("/v1/chat/completions", json=big, headers=H)).status_code == 413
    assert (await c.post("/v1/chat/completions", json={**BODY, "stream": "yes"}, headers=H)).status_code == 400
    assert (await c.post("/v1/chat/completions", json={**BODY, "messages": ["hi"]}, headers=H)).status_code == 400


async def test_upstream_secret_not_leaked_in_error():
    from llmgate.providers import ProviderError
    class Leaky(MockProvider):
        async def complete(self, body): raise ProviderError("upstream 500: key sk-SECRET123")
    _, c = client([Route(Leaky("l", ["m"]))])
    r = await c.post("/v1/chat/completions", json=BODY, headers=H)
    assert r.status_code == 502 and "SECRET" not in r.text


async def test_early_stream_close_releases_upstream_and_meters():
    closed = []
    class Slow(MockProvider):
        async def stream(self, body):
            try:
                async for ch in super().stream(body): yield ch
            finally:
                closed.append(True)
    app, c = client([Route(Slow("s", ["m"], latency=0, token_delay=0))])
    async with c.stream("POST", "/v1/chat/completions", json={**BODY, "stream": True}, headers=H) as r:
        async for _ in r.aiter_lines():
            break  # client walks away after first line
    import asyncio; await asyncio.sleep(0.05)
    assert closed  # upstream generator was finalised


def test_keys_from_env(monkeypatch):
    from llmgate.app import keys_from_env
    monkeypatch.setenv("LLMGATE_KEYS", '{"s3cret": {"name": "t", "rate": 5, "burst": 5, "admin": true}}')
    k = keys_from_env()["s3cret"]; assert k.admin and k.rate == 5


class Hang(MockProvider):
    async def complete(self, body):
        import asyncio; await asyncio.sleep(10)
    async def stream(self, body):
        import asyncio; await asyncio.sleep(10); yield {}


async def test_hung_provider_times_out_and_fails_over():
    good = mp("good")
    app, c = client([Route(Hang("hang", ["m"]), 0, timeout=0.05), Route(good, 1)])
    r = await c.post("/v1/chat/completions", json=BODY, headers=H)
    assert r.status_code == 200 and r.headers["x-gateway-provider"] == "good"
    r = await c.post("/v1/chat/completions", json={**BODY, "stream": True}, headers=H)
    assert r.headers["x-gateway-provider"] == "good" and r.text.rstrip().endswith("[DONE]")


def test_config_loader(tmp_path, monkeypatch):
    import json as j
    from llmgate.config import build_app
    monkeypatch.setenv("LLMGATE_KEYS", '{"k": {"name": "t"}}')
    monkeypatch.setenv("UP_KEY", "x")
    p = tmp_path / "c.json"
    p.write_text(j.dumps({"providers": [
        {"name": "up", "type": "openai_compatible", "base_url": "http://up/v1", "api_key_env": "UP_KEY", "models": ["m"]},
        {"name": "sim", "type": "mock", "models": ["m"], "priority": 1}]}))
    app = build_app(str(p))
    assert [r.provider.name for r in app.state.router.routes] == ["up", "sim"]


class StallsAfterTwo(MockProvider):
    closed = False
    async def stream(self, body):
        import asyncio
        try:
            for i in range(2):
                yield {"choices": [{"delta": {"content": f"w{i} "}, "finish_reason": None}]}
            await asyncio.sleep(30)  # stalls mid-stream
            yield {}
        finally:
            StallsAfterTwo.closed = True


async def test_stalled_stream_hits_idle_timeout_cleanly():
    import time
    other = mp("other")
    stall = StallsAfterTwo("stall", ["m"], latency=0, token_delay=0)
    app, c = client([Route(stall, 0, idle_timeout=0.1), Route(other, 1)])
    t = time.perf_counter()
    r = await c.post("/v1/chat/completions", json={**BODY, "stream": True}, headers=H)
    assert time.perf_counter() - t < 2                      # did not hang for 30 s
    assert "w0" in r.text and "w1" in r.text                # client got what was produced
    assert "stream_error" in r.text and "[DONE]" not in r.text
    assert "other" not in r.text and other.calls == 0       # never spliced from another provider
    assert StallsAfterTwo.closed                            # upstream generator cleaned up
    assert app.state.meter.snapshot("tenant")["status_stream_error"] == 1


async def test_slow_but_steady_stream_is_not_cut_off():
    # each gap (0.03 s) is below idle_timeout (0.2 s) even though the whole stream is longer
    slow = MockProvider("slow", ["m"], latency=0, token_delay=0.03)
    _, c = client([Route(slow, idle_timeout=0.2)])
    r = await c.post("/v1/chat/completions", json={**BODY, "stream": True}, headers=H)
    assert r.text.rstrip().endswith("[DONE]") and "stream_error" not in r.text


# --- token-aware rate limits
def test_estimate_prompt_tokens():
    from llmgate.app import estimate_prompt_tokens
    assert estimate_prompt_tokens({"messages": [{"content": "one two"}, {"content": "three"}]}) == 3
    assert estimate_prompt_tokens({"messages": []}) == 1  # floor of 1


async def test_token_limit_preflight_and_settlement():
    clk = Clock()
    app, c = client([Route(mp("a"))], clock=clk, keykw={"token_rate": 5, "token_burst": 10})
    big = {"model": "m", "messages": [{"role": "user", "content": " ".join(["w"] * 20)}]}
    r = await c.post("/v1/chat/completions", json=big, headers=H)
    assert r.status_code == 429 and r.json()["error"]["type"] == "rate_limit_error"
    assert r.headers["retry-after"] == "2"  # (20 - 10) / 5 = 2 s
    # est 3 fits; usage is prompt 3 + completion 5 = 8, so 5 more settle after the response
    r = await c.post("/v1/chat/completions", json=BODY, headers=H)
    assert r.status_code == 200
    # balance now 10 - 3 - 5 = 2, so the next estimated 3 no longer fits
    r = await c.post("/v1/chat/completions", json=BODY, headers=H)
    assert r.status_code == 429 and r.headers["retry-after"] == "1"
    clk.t = 5  # refill
    assert (await c.post("/v1/chat/completions", json=BODY, headers=H)).status_code == 200
    assert app.state.meter.snapshot("tenant")["status_rate_limited_tokens"] == 2


async def test_token_budget_charged_after_stream():
    clk = Clock()
    app, c = client([Route(mp("a"))], clock=clk, keykw={"token_rate": 1, "token_burst": 8})
    r = await c.post("/v1/chat/completions", json={**BODY, "stream": True}, headers=H)
    assert r.text.rstrip().endswith("[DONE]")
    # est 3 taken up front; stream usage prompt 3 + completion 5 = 8, settle 5 -> balance 0
    r = await c.post("/v1/chat/completions", json=BODY, headers=H)
    assert r.status_code == 429 and "token" in r.json()["error"]["message"]


def test_keys_from_env_token_fields(monkeypatch):
    from llmgate.app import keys_from_env
    monkeypatch.setenv("LLMGATE_KEYS", '{"s": {"name": "t", "token_rate": 100, "token_burst": 200}}')
    k = keys_from_env()["s"]
    assert k.token_rate == 100 and k.token_burst == 200
    monkeypatch.setenv("LLMGATE_KEYS", '{"s": {"name": "t"}}')
    assert keys_from_env()["s"].token_rate is None  # no token limit unless configured
