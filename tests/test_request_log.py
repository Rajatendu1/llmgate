"""Request log: redaction contract, ring/list behavior, endpoint wiring."""
from __future__ import annotations
import json

import httpx
import pytest

from llmgate.demo_app import app as demo_app
from llmgate.request_log import (MAX_EVENTS, MemoryRequestLog, RedisRequestLog,
                                 make_event)


def test_event_shape_is_secret_free():
    e = make_event("visitor", "sim-a", "gpt-sim",
                   {"prompt_tokens": 6, "completion_tokens": 9}, "ok",
                   0.052, 0.000012, "simulated", 1, "abcdef1234567890extra")
    assert set(e) == {"ts", "rid", "key", "provider", "model", "status",
                      "latency_ms", "prompt_tokens", "completion_tokens",
                      "cost_usd", "mode", "attempts"}
    assert e["rid"] == "abcdef1234567890"  # capped at 16 chars
    assert e["latency_ms"] == 52.0
    # nothing in the serialized event can carry prompt text or secrets
    blob = json.dumps(e)
    for forbidden in ("Say hello", "invalid-sample-simulated-bearer", "Bearer", "content"):
        assert forbidden not in blob


def test_memory_log_ring():
    log = MemoryRequestLog(capacity=3)
    for i in range(5):
        log.record(make_event("k", "p", "m", None, "ok", 0.01, None, "simulated", 1, f"r{i}"))
    latest = log.latest(10)
    assert len(latest) == 3
    assert [e["rid"] for e in latest] == ["r4", "r3", "r2"]  # newest first


@pytest.mark.asyncio
async def test_redis_log_push_cap_and_read():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        parts = json.loads(request.content)
        calls.append(parts[0])
        if parts[0] == "EVAL":
            return httpx.Response(200, json={"result": 1})
        if parts[0] == "LRANGE":
            ev = make_event("visitor", "sim-a", "gpt-sim", None, "ok", 0.05, 0.0, "simulated", 1, "r1")
            return httpx.Response(200, json={"result": [json.dumps(ev), "not-json{"]})
        return httpx.Response(200, json={"result": "PONG"})

    log = RedisRequestLog("https://upstash.example", "tok", capacity=50,
                          client=httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                                   base_url="https://upstash.example"))
    await log.record(make_event("visitor", "sim-a", "gpt-sim", None, "ok", 0.05, 0.0, "simulated", 1, "r1"))
    latest = await log.latest(20)
    assert calls == ["EVAL", "LRANGE"]
    assert len(latest) == 1 and latest[0]["rid"] == "r1"
    assert log.errors == 1  # the corrupt entry was skipped, not served


@pytest.mark.asyncio
async def test_demo_app_records_and_serves_events():
    """End to end through the real demo app: a Try request lands in the log and
    the public endpoint serves it, secret-free."""
    transport = httpx.ASGITransport(app=demo_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as cl:
        r = await cl.post("/demo/try")
        assert r.status_code == 200
        r = await cl.get("/demo/events")
        assert r.status_code == 200
        body = r.json()
    assert body["ok"] and body["backend"] == "memory"
    assert body["events"], "expected at least the Try request in the log"
    ev = body["events"][0]
    assert ev["key"] == "visitor"
    assert ev["mode"] == "simulated"
    assert ev["status"] == "ok"
    assert ev["attempts"] >= 1
    assert ev["latency_ms"] is not None and ev["latency_ms"] > 0
    blob = json.dumps(body)
    assert "invalid-sample-simulated-bearer" not in blob
    assert "Say hello" not in blob


@pytest.mark.asyncio
async def test_ring_capacity_is_bounded():
    log = MemoryRequestLog()
    for i in range(MAX_EVENTS * 3):
        log.record(make_event("k", "p", "m", None, "ok", 0.01, None, "simulated", 1, f"r{i}"))
    assert len(log.latest(MAX_EVENTS * 2)) == MAX_EVENTS
