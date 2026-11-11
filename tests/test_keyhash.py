"""API keys stored as sha256:<hex> authenticate identically to plaintext keys,
so a leaked env/config dump yields no usable key. Mixed dicts work during
rotation. Comparisons stay constant-time over every entry."""
import hashlib
import httpx
from llmgate.app import ApiKey, create_app
from llmgate.providers import MockProvider
from llmgate.router import Route, Router

BODY = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}


def _h(secret):
    return "sha256:" + hashlib.sha256(secret.encode()).hexdigest()


def app_with(keys):
    app = create_app(Router([Route(MockProvider("a", ["m"], latency=0, token_delay=0, seed=1))]), keys)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


async def test_hashed_key_authenticates():
    c = app_with({_h("real-secret-1"): ApiKey("tenant", 5, 5)})
    r = await c.post("/v1/chat/completions", json=BODY, headers={"Authorization": "Bearer real-secret-1"})
    assert r.status_code == 200
    assert (await c.get("/v1/models", headers={"Authorization": "Bearer real-secret-1"})).status_code == 200


async def test_hashed_key_rejects_wrong_and_raw_hash():
    c = app_with({_h("real-secret-1"): ApiKey("tenant", 5, 5)})
    assert (await c.get("/v1/models", headers={"Authorization": "Bearer wrong"})).status_code == 401
    # presenting the stored hash itself must not authenticate
    assert (await c.get("/v1/models", headers={"Authorization": "Bearer " + _h("real-secret-1")})).status_code == 401


async def test_mixed_plaintext_and_hashed_during_rotation():
    c = app_with({"old-plain": ApiKey("old", 5, 5), _h("new-secret"): ApiKey("new", 5, 5)})
    assert (await c.get("/v1/models", headers={"Authorization": "Bearer old-plain"})).status_code == 200
    assert (await c.get("/v1/models", headers={"Authorization": "Bearer new-secret"})).status_code == 200
