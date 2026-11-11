#!/usr/bin/env python3
"""Run bounded local gateway and Actas contract checks. No deployment is performed."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

SUPPORTED_GATEWAY_CONTRACT_SHA256 = '7c087a3512e664983505842ff641e294ce0a746934dfecd9f6631fc52319024b'

BOUNDARY = ("Local preflight only: actual gateway Lua limiter tests and supplied Actas policy cases. "
            "Not live deployment acceptance, exhaustive policy equivalence, token validation, "
            "remote authorization, provider availability or a security audit. No deployment is performed.")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def clean_env():
    # Never forward provider/API/Redis credentials or caller Python import overrides.
    return {k: os.environ[k] for k in ("PATH", "SYSTEMROOT", "LANG", "LC_ALL", "TMPDIR") if k in os.environ} | {"PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1", "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}


def identity(root):
    def git(*args):
        p = subprocess.run(["git", "-C", str(root), *args], capture_output=True, timeout=10, env=clean_env())
        if p.returncode:
            raise ValueError("source must be a Git working tree")
        return p.stdout.decode().strip()
    return {"head": git("rev-parse", "HEAD"), "dirty": bool(git("status", "--porcelain", "--untracked-files=normal"))}


def execute(name, argv, cwd, timeout):
    started = time.monotonic()
    try:
        # Disk-backed captures keep child output out of the public report and memory.
        with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
            p = subprocess.Popen(argv, cwd=cwd, env=clean_env(), stdout=out, stderr=err, start_new_session=True)
            try:
                rc = p.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                import signal
                os.killpg(p.pid, signal.SIGKILL)
                p.wait()
                rc = None
            streams = {}
            policy_counts = None
            policy_invalid = False
            if name == 'actas-policy-comparison':
                try:
                    out.seek(0)
                    raw = out.read(1048577)
                    if len(raw) > 1048576:
                        raise ValueError('oversized native report')
                    report = json.loads(raw)
                    policy_counts = {k: report[k] for k in ('total', 'newly_allowed', 'newly_denied', 'unchanged')}
                    policy_counts['candidate_matched'] = report['candidate']['matched']
                    if any(type(v) is not int or v < 0 for v in policy_counts.values()):
                        raise ValueError('invalid native counts')
                    total = policy_counts['total']
                    if total < 1 or total > 64 or policy_counts['candidate_matched'] > total or sum(policy_counts[k] for k in ('newly_allowed', 'newly_denied', 'unchanged')) != total:
                        raise ValueError('inconsistent native counts')
                    expected_rc = 0 if policy_counts['newly_allowed'] == policy_counts['newly_denied'] == 0 and policy_counts['candidate_matched'] == total else 1
                    if rc != expected_rc:
                        raise ValueError('native exit contradicts policy counts')
                except (ValueError, KeyError, TypeError):
                    policy_invalid = True
            for label, stream in (("stdout", out), ("stderr", err)):
                stream.seek(0)
                h = hashlib.sha256()
                count = 0
                while block := stream.read(65536):
                    h.update(block)
                    count += len(block)
                streams[label] = {"sha256": h.hexdigest(), "bytes": count}
        state = "pass" if rc == 0 else "regression" if rc == 1 else "error"
        if name == 'actas-policy-comparison' and policy_invalid and rc in (0, 1):
            state = 'error'
        result = {"name": name, "state": state, "exit_code": rc, "timed_out": rc is None,
                  "duration_seconds": round(time.monotonic() - started, 3), **streams}
        if policy_counts is not None:
            result['policy_counts'] = policy_counts
        return result
    except (OSError, ValueError) as exc:
        return {"name": name, "state": "error", "exit_code": None, "error_type": type(exc).__name__}


def gateway_contract(python, gateway, timeout):
    import xml.etree.ElementTree as ET
    contract = Path(gateway, 'tests/test_redislimit.py')
    if not contract.is_file() or digest(contract.read_bytes()) != SUPPORTED_GATEWAY_CONTRACT_SHA256:
        return {'name': 'gateway-native-lua', 'state': 'error', 'exit_code': None,
                'evidence_error': 'Unsupported native contract revision; review and pin its exact content before use'}
    with tempfile.TemporaryDirectory(prefix="acceptance-") as tmp:
        result_path = Path(tmp, "junit.xml")
        step = execute("gateway-native-lua", [python, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "pytest_asyncio.plugin", "tests/test_redislimit.py", "--junitxml=" + str(result_path)], gateway, timeout)
        try:
            suites = ET.parse(result_path).getroot()
            counts = {k: sum(int(s.get(k, 0)) for s in suites.iter('testsuite')) for k in ('tests', 'failures', 'errors', 'skipped')}
            step['native_test_counts'] = counts
            # A successful child exit without exercised assertions is not evidence.
            if counts['tests'] == 0 or counts['skipped'] or counts['errors'] or (step['state'] == 'pass' and counts['failures']):
                step['state'] = 'error'
        except (OSError, ET.ParseError, ValueError):
            step['state'] = 'error'
            step['evidence_error'] = 'Native test-count report missing or invalid'
        return step


def runtime_provenance(python, gateway, actas):
    probes = [('gateway', gateway, ['llmgate', 'llmgate.redislimit']),
              ('actas', actas / 'source', ['actas', 'actas.workbench'])]
    result = {'python_binary_sha256': digest(Path(python).read_bytes()), 'imports': {}}
    for label, cwd, modules in probes:
        code = ('import importlib,json,sys; names=' + repr(modules) +
                '; print(json.dumps({"version":sys.version,"executable":sys.executable,"origins":{n:importlib.import_module(n).__file__ for n in names}}))')
        p = subprocess.run([python, '-c', code], cwd=cwd, env=clean_env(), capture_output=True, timeout=20)
        if p.returncode != 0 or len(p.stdout) > 16384:
            raise ValueError('Native import provenance unavailable')
        info = json.loads(p.stdout)
        result['python_version'] = info['version']
        if os.path.abspath(info['executable']) != python:
            raise ValueError('Python executable mismatch')
        result['imports'][label] = {}
        for module, path in info['origins'].items():
            origin = Path(path).resolve(strict=True)
            expected = (cwd / module.replace('.', '/'))
            expected = expected / '__init__.py' if expected.is_dir() else expected.with_suffix('.py')
            if origin != expected.resolve(strict=True):
                raise ValueError('Native imports do not come from supplied source')
            result['imports'][label][module] = {'relative_path': origin.relative_to(cwd).as_posix(), 'sha256': digest(origin.read_bytes())}
    return result


def resolve_file(value):
    p = Path(value).resolve(strict=True)
    if not p.is_file() or p.stat().st_size > 65536:
        raise ValueError("policy inputs must be files no larger than 64 KiB")
    return p


def run(args):
    gateway, actas = Path(args.gateway).resolve(strict=True), Path(args.actas).resolve(strict=True)
    python = os.path.abspath(os.path.expanduser(args.python))
    if not Path(python).is_file():
        raise ValueError("Python executable is unavailable")
    candidate, baseline = resolve_file(args.candidate), resolve_file(args.baseline)
    before = {"gateway": identity(gateway), "actas": identity(actas)}
    if any(s["dirty"] for s in before.values()):
        raise ValueError("clean source trees required for source-attributed evidence")
    if not (gateway / "tests/test_redislimit.py").is_file() or not (actas / "source/actas/workbench.py").is_file():
        raise ValueError("expected native contract entry points are missing")
    provenance = runtime_provenance(python, gateway, actas)
    inputs = {"candidate_sha256": digest(candidate.read_bytes()), "baseline_sha256": digest(baseline.read_bytes())}
    environment = execute("native-dependencies", [python, "-c", "import pytest, lupa.lua51, fastapi, httpx, jwt"], gateway, args.timeout)
    if environment['state'] != 'pass':
        environment['state'] = 'error'
        return {"schema_version": 1, "state": "error", "sources": before, "inputs": inputs, "steps": [environment], "boundary": BOUNDARY}
    steps = [gateway_contract(python, gateway, args.timeout),
             execute("actas-policy-comparison", [python, "-m", "actas.workbench", str(candidate), str(baseline)], actas / "source", args.timeout)]
    after = {"gateway": identity(gateway), "actas": identity(actas)}
    stable = before == after and inputs == {"candidate_sha256": digest(candidate.read_bytes()), "baseline_sha256": digest(baseline.read_bytes())}
    state = "error" if not stable or any(s["state"] == "error" for s in steps) else "regression" if any(s["state"] == "regression" for s in steps) else "pass"
    return {"schema_version": 1, "state": state, "sources": before, "inputs": inputs,
            "source_and_input_stable": stable, "runtime_provenance": provenance, "steps": steps, "boundary": BOUNDARY,
            "excluded": ["live HTTP workflows", "real providers", "cross-service token enforcement", "browser pixels", "config-to-policy adapter", "full native suites"]}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for opt in ("gateway", "actas", "candidate", "baseline"):
        p.add_argument("--" + opt, required=True)
    p.add_argument("--python", default=sys.executable)
    p.add_argument("--timeout", type=int, default=120)
    args = p.parse_args()
    if not 1 <= args.timeout <= 900:
        p.error("timeout must be 1-900 seconds")
    try:
        report = run(args)
    except (OSError, ValueError, subprocess.SubprocessError):
        report = {"schema_version": 1, "state": "error", "error": "Invalid inputs, dirty/unavailable source or Git execution failure", "boundary": BOUNDARY}
    print(json.dumps(report, indent=2))
    return {"pass": 0, "regression": 1, "error": 2}[report["state"]]


if __name__ == "__main__":
    sys.exit(main())
