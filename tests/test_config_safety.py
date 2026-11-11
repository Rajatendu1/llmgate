"""Ambiguous failover is explicit authority, never Python truthiness."""
import json
from unittest.mock import Mock

import httpx
import pytest
from llmgate.config import build_app


def config_path(tmp_path, monkeypatch, **overrides):
    monkeypatch.setenv('CONFIG_SAFETY_KEYS', '{"k":{"name":"test"}}')
    monkeypatch.delenv('LLMGATE_KEYS_EXTRA', raising=False)
    cfg = {'keys_env': 'CONFIG_SAFETY_KEYS', 'providers': [
        {'type': 'mock', 'name': 'primary', 'models': ['m'], 'fail_rate': 1, 'latency': 0, 'priority': 0},
        {'type': 'mock', 'name': 'backup', 'models': ['m'], 'latency': 0, 'priority': 1}]}
    cfg.update(overrides)
    path = tmp_path/'config.json'
    path.write_text(json.dumps(cfg))
    return str(path)


@pytest.mark.parametrize('invalid', ['false', 'true', '', 0, 1, None, [], {}])
def test_invalid_json_flag_rejected_before_provider_allocation(tmp_path, monkeypatch, invalid):
    allocate = Mock(side_effect=AssertionError('provider allocated before validation'))
    monkeypatch.setattr('llmgate.config.MockProvider', allocate)
    with pytest.raises(ValueError, match='JSON boolean'):
        build_app(config_path(tmp_path, monkeypatch, failover_ambiguous=invalid))
    allocate.assert_not_called()


@pytest.mark.parametrize('raw,expected', [('0', False), ('false', False), ('no', False),
                                         ('1', True), ('true', True), ('yes', True),
                                         (' FALSE ', False), ('TRUE', True)])
def test_explicit_environment_values(tmp_path, monkeypatch, raw, expected):
    monkeypatch.setenv('LLMGATE_FAILOVER_AMBIGUOUS', raw)
    assert build_app(config_path(tmp_path, monkeypatch)).state.router.failover_ambiguous is expected


@pytest.mark.parametrize('raw', ['', 'flase', 'off', '2', 'null'])
def test_malformed_environment_rejected(tmp_path, monkeypatch, raw):
    monkeypatch.setenv('LLMGATE_FAILOVER_AMBIGUOUS', raw)
    with pytest.raises(ValueError, match='LLMGATE_FAILOVER_AMBIGUOUS must be'):
        build_app(config_path(tmp_path, monkeypatch))


async def test_omitted_flag_is_duplicate_averse(tmp_path, monkeypatch):
    monkeypatch.delenv('LLMGATE_FAILOVER_AMBIGUOUS', raising=False)
    app = build_app(config_path(tmp_path, monkeypatch))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://t') as c:
            r = await c.post('/v1/chat/completions', headers={'Authorization':'Bearer k'},
                             json={'model':'m','messages':[{'role':'user','content':'hi'}]})
            assert r.status_code == 502
            assert app.state.router.routes[1].provider.calls == 0


@pytest.mark.parametrize('flag,status,backup_calls', [(False, 502, 0), (True, 200, 1)])
async def test_json_boolean_controls_actual_failover_and_overrides_env(tmp_path, monkeypatch, flag, status, backup_calls):
    monkeypatch.setenv('LLMGATE_FAILOVER_AMBIGUOUS', 'not-read-when-json-is-explicit')
    app = build_app(config_path(tmp_path, monkeypatch, failover_ambiguous=flag))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://t') as c:
            r = await c.post('/v1/chat/completions', headers={'Authorization':'Bearer k'},
                             json={'model':'m','messages':[{'role':'user','content':'hi'}]})
            assert r.status_code == status
            assert app.state.router.routes[1].provider.calls == backup_calls
