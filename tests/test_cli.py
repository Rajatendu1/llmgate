"""CLI: argument handling and config-driven serve wiring.

uvicorn.run is patched out; these checks cover the CLI's own behavior, not a
running server (the end-to-end HTTP suites cover that)."""
import json

import pytest

from llmgate.cli import main


def _config(tmp_path):
    path = tmp_path / "sim.config.json"
    path.write_text(json.dumps({
        "keys_env": "LLMGATE_KEYS",
        "providers": [
            {"name": "sim", "type": "mock", "models": ["some-model"], "priority": 0},
        ],
    }))
    return str(path)


def test_serve_builds_app_from_config_and_runs(monkeypatch, tmp_path):
    monkeypatch.setenv("LLMGATE_KEYS", json.dumps({"k1": {"name": "t", "rate": 5, "burst": 10}}))
    calls = {}
    import uvicorn
    monkeypatch.setattr(uvicorn, "run",
                        lambda app, host=None, port=None: calls.update(app=app, host=host, port=port))
    main(["serve", "--config", _config(tmp_path), "--host", "0.0.0.0", "--port", "8123"])
    assert calls["host"] == "0.0.0.0"
    assert calls["port"] == 8123
    assert calls["app"] is not None


def test_serve_defaults(monkeypatch, tmp_path):
    monkeypatch.setenv("LLMGATE_KEYS", json.dumps({"k1": {"name": "t", "rate": 5, "burst": 10}}))
    calls = {}
    import uvicorn
    monkeypatch.setattr(uvicorn, "run",
                        lambda app, host=None, port=None: calls.update(host=host, port=port))
    main(["serve", "--config", _config(tmp_path)])
    assert calls == {"host": "127.0.0.1", "port": 8000}


def test_missing_config_rejected(tmp_path):
    with pytest.raises(SystemExit) as exc:
        main(["serve", "--config", str(tmp_path / "nope.json")])
    assert exc.value.code == 2


def test_command_required():
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code == 2
