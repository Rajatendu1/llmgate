"""Real-upstream harness: HTTPProvider verified against Groq-shaped behavior.

Two layers:
1. Mocked-transport tests (this file, always run): the exact request shape,
   auth header, streaming parse, and error categorization against recorded
   OpenAI-compatible responses, including a full router integration pass
   (429 -> same-provider retry -> failover, breaker trips, no retry of an
   AMBIGUOUS failure).
2. Live verification (scripts/verify_real_upstream.py): the same client
   against the real service, gated on env credentials so CI/dev never needs
   a key and never touches the network.
"""
from __future__ import annotations
import json

import httpx
import pytest

from llmgate.providers import HTTPProvider, MockProvider, ProviderError
from llmgate.router import Route, Router
from llmgate.resilience import CircuitBreaker

BASE = "https://api.groq.com/openai/v1"
MODEL = "openai/gpt-oss-120b"


def _completion(text="Hello from the upstream.", pt=7, ct=5):
    return {"id": "chatcmpl-1", "object": "chat.completion", "created": 1,
            "model": MODEL,
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": text}}],
            "usage": {"prompt_tokens": pt, "completion_tokens": ct, "total_tokens": pt + ct}}


def _provider(handler) -> HTTPProvider:
    return HTTPProvider("groq", BASE, "gsk_test_key", [MODEL],
                        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def _body():
    return {"model": MODEL, "max_tokens": 16,
            "messages": [{"role": "user", "content": "Say hello."}]}


@pytest.mark.asyncio
async def test_request_shape_and_auth():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=_completion())

    prov = _provider(handler)
    res = await prov.complete(_body())
    assert seen["url"] == f"{BASE}/chat/completions"
    assert seen["auth"] == "Bearer gsk_test_key"
    assert seen["body"]["stream"] is False
    assert seen["body"]["model"] == MODEL
    assert res["usage"]["total_tokens"] == 12
    assert res["choices"][0]["message"]["content"] == "Hello from the upstream."


@pytest.mark.asyncio
async def test_error_categories():
    cases = [(429, ProviderError.DECLINED), (401, ProviderError.FATAL),
             (403, ProviderError.FATAL), (408, ProviderError.AMBIGUOUS),
             (500, ProviderError.AMBIGUOUS), (503, ProviderError.AMBIGUOUS)]
    for status, category in cases:
        prov = _provider(lambda r, s=status: httpx.Response(s, json={"error": {"message": "boom"}}))
        with pytest.raises(ProviderError) as ei:
            await prov.complete(_body())
        assert ei.value.category == category, status
        assert "boom" in str(ei.value)


@pytest.mark.asyncio
async def test_connect_failure_is_declined():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    prov = _provider(handler)
    with pytest.raises(ProviderError) as ei:
        await prov.complete(_body())
    assert ei.value.category == ProviderError.DECLINED


@pytest.mark.asyncio
async def test_read_timeout_is_ambiguous():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    prov = _provider(handler)
    with pytest.raises(ProviderError) as ei:
        await prov.complete(_body())
    assert ei.value.category == ProviderError.AMBIGUOUS


@pytest.mark.asyncio
async def test_stream_parses_sse_and_usage():
    chunks = [
        {"object": "chat.completion.chunk", "model": MODEL,
         "choices": [{"index": 0, "delta": {"content": "Hello"}, "finish_reason": None}]},
        {"object": "chat.completion.chunk", "model": MODEL,
         "choices": [{"index": 0, "delta": {"content": " world"}, "finish_reason": None}]},
        {"object": "chat.completion.chunk", "model": MODEL,
         "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
         "usage": {"prompt_tokens": 7, "completion_tokens": 2, "total_tokens": 9}},
    ]
    sse = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"})

    prov = _provider(handler)
    got = [c async for c in prov.stream(_body())]
    assert seen["body"]["stream"] is True
    assert seen["body"]["stream_options"] == {"include_usage": True}
    assert got[0]["choices"][0]["delta"]["content"] == "Hello"
    assert got[-1]["usage"]["total_tokens"] == 9


@pytest.mark.asyncio
async def test_stream_error_status_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": {"message": "rate limited"}})

    prov = _provider(handler)
    with pytest.raises(ProviderError) as ei:
        _ = [c async for c in prov.stream(_body())]
    assert ei.value.category == ProviderError.DECLINED


@pytest.mark.asyncio
async def test_router_429_retries_then_failover():
    """Groq-shaped 429 storm on the real primary: same-provider retry with
    backoff, then failover to the backup - and the breaker opens."""
    calls = {"real": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["real"] += 1
        return httpx.Response(429, json={"error": {"message": "rate limited"}})

    real = HTTPProvider("groq", BASE, "gsk_test_key", [MODEL],
                        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    backup = MockProvider("sim-b", [MODEL])
    router = Router([Route(real, priority=0, breaker=CircuitBreaker(2, 30), retries=1),
                     Route(backup, priority=1, breaker=CircuitBreaker(3, 30))],
                    sleep=_instant)
    prov, res, tried = await router.complete(_body())
    assert prov == "sim-b"                      # failover served it
    assert calls["real"] == 2                   # 1 initial + 1 DECLINED retry
    assert tried == ["groq", "sim-b"]
    assert router.routes[0].breaker.state == "open"  # threshold 2 reached
    assert res["choices"][0]["message"]["content"].startswith("[sim-b]")


@pytest.mark.asyncio
async def test_router_never_retries_ambiguous():
    calls = {"real": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["real"] += 1
        return httpx.Response(500, json={"error": {"message": "internal"}})

    real = HTTPProvider("groq", BASE, "gsk_test_key", [MODEL],
                        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    router = Router([Route(real, priority=0, breaker=CircuitBreaker(9, 30), retries=3)],
                    sleep=_instant)
    # duplicate-averse default: an AMBIGUOUS failure surfaces instead of failing over
    with pytest.raises(Exception):
        await router.complete(_body())
    assert calls["real"] == 1  # exactly one upstream attempt, no retry storm


async def _instant(_delay):
    return None


BACKUP_MODEL = "openai/gpt-oss-20b"
ALIAS = "gpt-real"


@pytest.mark.asyncio
async def test_model_map_rewrites_upstream_model():
    """The public alias is routed by the gateway; each provider sends its own
    upstream model id. Unmapped models pass through unchanged."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=_completion())

    prov = HTTPProvider("groq", BASE, "gsk_test_key", [ALIAS],
                        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
                        model_map={ALIAS: MODEL})
    await prov.complete({"model": ALIAS, "max_tokens": 16,
                         "messages": [{"role": "user", "content": "hi"}]})
    assert seen["body"]["model"] == MODEL

    await prov.complete({"model": "unmapped-model", "max_tokens": 16,
                         "messages": [{"role": "user", "content": "hi"}]})
    assert seen["body"]["model"] == "unmapped-model"


@pytest.mark.asyncio
async def test_model_map_rewrites_stream_too():
    sse = "data: " + json.dumps({"object": "chat.completion.chunk", "model": MODEL,
                                 "choices": [{"index": 0, "delta": {"content": "hi"},
                                              "finish_reason": "stop"}],
                                 "usage": {"prompt_tokens": 1, "completion_tokens": 1,
                                           "total_tokens": 2}}) + "\n\ndata: [DONE]\n\n"
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"})

    prov = HTTPProvider("groq", BASE, "gsk_test_key", [ALIAS],
                        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
                        model_map={ALIAS: MODEL})
    got = [c async for c in prov.stream({"model": ALIAS, "messages": []})]
    assert seen["body"]["model"] == MODEL
    assert got[0]["choices"][0]["delta"]["content"] == "hi"


@pytest.mark.asyncio
async def test_router_same_provider_backup_model():
    """Same-provider backup: primary 429s on the alias, the backup serves the
    same alias with its own upstream model id. This is MODEL failover within
    one provider, not an independent-provider outage."""
    seen = {}

    def primary_handler(request: httpx.Request) -> httpx.Response:
        seen["primary"] = json.loads(request.content)
        return httpx.Response(429, json={"error": {"message": "rate limited"}})

    def backup_handler(request: httpx.Request) -> httpx.Response:
        seen["backup"] = json.loads(request.content)
        return httpx.Response(200, json=_completion(text="Hello from the backup model."))

    primary = HTTPProvider("groq", BASE, "gsk_test_key", [ALIAS],
                           client=httpx.AsyncClient(transport=httpx.MockTransport(primary_handler)),
                           model_map={ALIAS: MODEL})
    backup = HTTPProvider("groq-backup", BASE, "gsk_test_key", [ALIAS],
                          client=httpx.AsyncClient(transport=httpx.MockTransport(backup_handler)),
                          model_map={ALIAS: BACKUP_MODEL})
    router = Router([Route(primary, priority=0, breaker=CircuitBreaker(3, 30), retries=1),
                     Route(backup, priority=1, breaker=CircuitBreaker(3, 30))],
                    sleep=_instant)
    body = {"model": ALIAS, "max_tokens": 16,
            "messages": [{"role": "user", "content": "Say hello."}]}
    prov, res, tried = await router.complete(body)
    assert prov == "groq-backup"
    assert tried == ["groq", "groq-backup"]
    assert seen["primary"]["model"] == MODEL          # primary sent its own id
    assert seen["backup"]["model"] == BACKUP_MODEL    # backup sent its own id
    assert res["choices"][0]["message"]["content"] == "Hello from the backup model."
