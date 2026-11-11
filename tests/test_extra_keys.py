"""Extra keys env: merged with the main key set, main set wins on duplicates."""
import json, os
import httpx
from llmgate.config import build_app

BODY = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
CFG = {"keys_env": "LLMGATE_KEYS", "providers": [{"name": "a", "type": "mock", "models": ["m"], "latency": 0}]}


def make_app(tmp_path, monkeypatch, main_keys, extra_keys):
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps(CFG))
    monkeypatch.setenv("LLMGATE_KEYS", json.dumps(main_keys))
    if extra_keys is not None:
        monkeypatch.setenv("LLMGATE_KEYS_EXTRA", json.dumps(extra_keys))
    else:
        monkeypatch.delenv("LLMGATE_KEYS_EXTRA", raising=False)
    app = build_app(str(p))
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


async def test_extra_key_authenticates(tmp_path, monkeypatch):
    c = make_app(tmp_path, monkeypatch, {"main": {"name": "m", "rate": 50, "burst": 50}},
                 {"extra": {"name": "v", "rate": 50, "burst": 50}})
    assert (await c.get("/v1/models", headers={"Authorization": "Bearer extra"})).status_code == 200
    assert (await c.get("/v1/models", headers={"Authorization": "Bearer main"})).status_code == 200


async def test_no_extra_env_is_fine(tmp_path, monkeypatch):
    c = make_app(tmp_path, monkeypatch, {"main": {"name": "m", "rate": 50, "burst": 50}}, None)
    assert (await c.get("/v1/models", headers={"Authorization": "Bearer main"})).status_code == 200


async def test_main_set_wins_on_duplicate(tmp_path, monkeypatch):
    c = make_app(tmp_path, monkeypatch,
                 {"dup": {"name": "real", "rate": 50, "burst": 50}},
                 {"dup": {"name": "shadow", "rate": 1, "burst": 1}})
    # the main entry (rate 50) governs: 5 rapid requests all pass
    for _ in range(5):
        r = await c.get("/v1/models", headers={"Authorization": "Bearer dup"})
        assert r.status_code == 200
