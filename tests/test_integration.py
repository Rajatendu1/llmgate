"""Gateway -> HTTPProvider -> a REAL local HTTP server (sockets, SSE) that misbehaves on demand.
This exercises the real network client path that the MockTransport tests skip. The upstream is a
fake OpenAI-compatible server written for these tests, not a real provider."""
import asyncio, json, socket, threading, time
import httpx, pytest, uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from llmgate.app import ApiKey, create_app
from llmgate.providers import HTTPProvider, MockProvider
from llmgate.resilience import CircuitBreaker
from llmgate.router import Route, Router

up = FastAPI()
hits = {"n": 0}


def chunk(t):
    return f"data: {json.dumps({'choices': [{'delta': {'content': t}}]})}\n\n"


@up.post("/v1/chat/completions")
async def completions(req: Request):
    assert req.headers["authorization"] == "Bearer upstream-secret"
    body = await req.json()
    hits["n"] += 1
    m = body["model"]
    if m == "rate-limited":
        return JSONResponse({"error": "slow down"}, status_code=429)
    if m == "bad-request":
        return JSONResponse({"error": "nope"}, status_code=400)
    if m == "stall":
        async def stall():
            yield chunk("a")
            await asyncio.sleep(30)
        return StreamingResponse(stall(), media_type="text/event-stream")
    if m == "hang":
        await asyncio.sleep(30)
    if not body.get("stream"):
        return {"choices": [{"message": {"content": "ok"}}], "usage": {"prompt_tokens": 2, "completion_tokens": 1}}

    async def gen():
        yield chunk("a")
        if m == "dies":
            await asyncio.sleep(0.05)
            raise RuntimeError("upstream crashed mid-stream")  # connection is cut
        yield chunk("b")
        yield 'data: ' + json.dumps({"choices": [], "usage": {"prompt_tokens": 2, "completion_tokens": 2}}) + "\n\n"
        yield "data: [DONE]\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream")


@pytest.fixture(scope="module")
def upstream():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    server = uvicorn.Server(uvicorn.Config(up, host="127.0.0.1", port=port, log_level="error"))
    t = threading.Thread(target=server.run, daemon=True); t.start()
    for _ in range(100):
        if server.started: break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}/v1"
    server.should_exit = True; t.join(5)


def gw(upstream, models, timeout=2.0, idle=2.0):
    real = HTTPProvider("real", upstream, "upstream-secret", models)
    backup = MockProvider("backup", models, latency=0, token_delay=0)
    app = create_app(Router([Route(real, 0, breaker=CircuitBreaker(5, 60), timeout=timeout, idle_timeout=idle), Route(backup, 1)]),
                     {"k": ApiKey("t", 1000, 1000)})
    return app, httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://g")


H = {"Authorization": "Bearer k"}
def body(m, stream=False): return {"model": m, "stream": stream, "messages": [{"role": "user", "content": "hi"}]}


async def test_real_http_nonstream_and_stream(upstream):
    app, c = gw(upstream, ["good"])
    r = await c.post("/v1/chat/completions", json=body("good"), headers=H)
    assert r.headers["x-gateway-provider"] == "real" and r.json()["choices"][0]["message"]["content"] == "ok"
    r = await c.post("/v1/chat/completions", json=body("good", True), headers=H)
    assert r.headers["x-gateway-provider"] == "real" and r.text.rstrip().endswith("[DONE]") and '"a"' in r.text
    assert app.state.meter.snapshot("t")["completion_tokens"] == 3  # 1 + 2 from upstream usage


async def test_upstream_429_fails_over_over_real_socket(upstream):
    _, c = gw(upstream, ["rate-limited"])
    r = await c.post("/v1/chat/completions", json=body("rate-limited"), headers=H)
    assert r.status_code == 200 and r.headers["x-gateway-provider"] == "backup"


async def test_upstream_400_not_retried(upstream):
    _, c = gw(upstream, ["bad-request"])
    r = await c.post("/v1/chat/completions", json=body("bad-request"), headers=H)
    assert r.status_code == 502


async def test_real_hang_times_out_and_fails_over(upstream):
    _, c = gw(upstream, ["hang"], timeout=0.3)
    t = time.perf_counter()
    r = await c.post("/v1/chat/completions", json=body("hang"), headers=H)
    assert r.status_code == 200 and r.headers["x-gateway-provider"] == "backup" and time.perf_counter() - t < 2


async def test_real_midstream_connection_cut_is_reported(upstream):
    app, c = gw(upstream, ["dies"])
    r = await c.post("/v1/chat/completions", json=body("dies", True), headers=H)
    assert '"a"' in r.text and "stream_error" in r.text and "[DONE]" not in r.text
    assert "backup" not in r.text  # no splice from the backup model
    assert app.state.meter.snapshot("t")["status_stream_error"] == 1


async def test_upstream_dead_port_fails_over():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()  # nothing listens
    _, c = gw(f"http://127.0.0.1:{port}/v1", ["good"])
    r = await c.post("/v1/chat/completions", json=body("good"), headers=H)
    assert r.status_code == 200 and r.headers["x-gateway-provider"] == "backup"


async def test_real_stalled_stream_over_socket_is_cut_and_reported(upstream):
    app, c = gw(upstream, ["stall"], idle=0.3)
    t = time.perf_counter()
    r = await c.post("/v1/chat/completions", json=body("stall", True), headers=H)
    assert time.perf_counter() - t < 3 and '"a"' in r.text and "stream_error" in r.text
    assert "backup" not in r.text and app.state.meter.snapshot("t")["status_stream_error"] == 1
