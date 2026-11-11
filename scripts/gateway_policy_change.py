#!/usr/bin/env python3
"""Compare actual local gateway responses for supplied scoped-token request cases."""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import socket
import subprocess
import inspect
import tempfile
from unittest.mock import patch

BOUNDARY = ('Supplied-case comparison on native mock-provider gateway requests with fresh local bearer tokens. '
            'Not a proof of production access, all possible requests, upstream health, distributed revocation, '
            'proof of possession or deployment safety. No configuration is installed.')
IDENT = re.compile(r'[A-Za-z0-9][A-Za-z0-9._~:/@+-]{0,127}\Z')


def read_json(path):
    raw = Path(path).read_bytes()
    if len(raw) > 65536:
        raise ValueError('input exceeds 64 KiB')
    def unique(pairs):
        d = {}
        for k, v in pairs:
            if k in d:
                raise ValueError('duplicate JSON field')
            d[k] = v
        return d
    return json.loads(raw, object_pairs_hook=unique), hashlib.sha256(raw).hexdigest()


def text(x):
    return isinstance(x, str) and bool(IDENT.fullmatch(x))


def validate_config(cfg):
    if not isinstance(cfg, dict) or not isinstance(cfg.get('providers'), list) or not 1 <= len(cfg['providers']) <= 8:
        raise ValueError('expected 1-8 mock providers')
    # Never instantiate an HTTPProvider or silently remove a declared real route.
    for p in cfg['providers']:
        if not isinstance(p, dict) or p.get('type') != 'mock':
            raise ValueError('only all-mock configurations are supported; real/mixed routes are not projected or activated')
        if not text(p.get('name')) or not isinstance(p.get('models'), list) or not 1 <= len(p['models']) <= 32 or not all(text(m) for m in p['models']):
            raise ValueError('invalid mock provider identifiers/models')
        if p.get('latency', .02) != 0 or p.get('fail_rate', 0) != 0:
            raise ValueError('set mock latency and fail_rate to zero for deterministic permission checks')
    ta = cfg.get('token_auth')
    if not isinstance(ta, dict) or ta.get('enabled') is not True or not text(ta.get('issuer')) or not text(ta.get('audience')):
        raise ValueError('enabled native token_auth with bounded issuer/audience required')
    if type(ta.get('demo', {}).get('enabled', False)) is not bool or ta.get('demo', {}).get('enabled', False):
        raise ValueError('delegation demo must be disabled for this local preflight')
    if not isinstance(ta.get('scope_models'), dict) or len(ta['scope_models']) > 32:
        raise ValueError('invalid scope mapping')
    for scope, models in ta['scope_models'].items():
        if not text(scope) or not isinstance(models, list) or len(models) > 32 or not all(text(m) for m in models):
            raise ValueError('invalid scope mapping values')
    if not text(ta.get('required_scope', 'llm:complete')):
        raise ValueError('invalid required scope')


def validate_cases(cases):
    if not isinstance(cases, list) or not 1 <= len(cases) <= 32:
        raise ValueError('expected 1-32 request cases')
    names = set()
    for c in cases:
        if not isinstance(c, dict) or set(c) != {'name', 'scope', 'model', 'expect_status'}:
            raise ValueError('each case needs exactly name, scope, model and expect_status')
        if not text(c['name']) or c['name'] in names or not text(c['model']):
            raise ValueError('invalid or duplicate case identity')
        names.add(c['name'])
        if not isinstance(c['scope'], list) or len(c['scope']) > 32 or not all(text(s) for s in c['scope']) or len(set(c['scope'])) != len(c['scope']):
            raise ValueError('invalid case scopes')
        if type(c['expect_status']) is not int or c['expect_status'] not in (200, 401, 403, 404):
            raise ValueError('unsupported expected status')


async def one_request(cfg, case):
    import httpx
    import llmgate.config as native_config
    from actas.tokens import issue_token
    original_create = native_config.create_app
    captured = {}
    def create(*args, **kw):
        captured['token_auth'] = kw['token_auth']
        return original_create(*args, **kw)
    with tempfile.TemporaryDirectory(prefix='gateway-policy-') as tmp:
        path = Path(tmp, 'config.json')
        path.write_text(json.dumps(cfg))
        with patch.dict(os.environ, {cfg.get('keys_env', 'LLMGATE_KEYS'): '{}'}, clear=False), patch.object(native_config, 'create_app', create):
            app = native_config.build_app(str(path))
        ta = captured['token_auth']
        token, _ = issue_token(ta.verifier.keystore, issuer=cfg['token_auth']['issuer'],
                               subject='local-preflight-worker', scope=' '.join(case['scope']),
                               audience=[cfg['token_auth']['audience']], expires_in=60)
        # Native app lifecycle, auth verification, required-scope check, allowlist,
        # known-route check and actual MockProvider completion all execute.
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://local-preflight') as client:
                response = await client.post('/v1/chat/completions', headers={'Authorization': 'Bearer ' + token},
                                             json={'model': case['model'], 'messages': [{'role': 'user', 'content': 'local permission check'}], 'max_tokens': 1})
        calls = sum(route.provider.calls for route in app.state.router.routes)
        if response.status_code not in (200, 401, 403, 404):
            raise ValueError('native request produced an unsupported operational status')
        if (response.status_code == 200) != (calls > 0):
            raise ValueError('native response/provider execution evidence disagrees')
        return {'status': response.status_code, 'provider_executed': calls > 0}


async def compare(baseline, candidate, cases):
    validate_config(baseline); validate_config(candidate); validate_cases(cases)
    changes = []
    # No caller credentials/store/env-based provider activation inherited.
    def no_network(*args, **kwargs):
        raise ValueError('network access is disabled during this local comparison')
    with patch.dict(os.environ, {}, clear=True), patch.object(socket.socket, 'connect', no_network), patch.object(socket.socket, 'connect_ex', no_network), patch.object(socket, 'create_connection', no_network):
        for case in cases:
            before = await one_request(baseline, case)
            after = await one_request(candidate, case)
            transition = 'newly_reachable' if before['status'] != 200 and after['status'] == 200 else 'newly_refused' if before['status'] == 200 and after['status'] != 200 else 'unchanged' if before['status'] == after['status'] else 'refusal_changed'
            changes.append({'name': case['name'], 'before': before, 'after': after, 'transition': transition,
                            'candidate_expectation_matched': after['status'] == case['expect_status']})
    changed = any(x['before']['status'] != x['after']['status'] for x in changes)
    matched = all(x['candidate_expectation_matched'] for x in changes)
    return {'schema_version': 1, 'state': 'review_required' if changed or not matched else 'pass',
            'total': len(changes), 'newly_reachable': sum(x['transition'] == 'newly_reachable' for x in changes),
            'newly_refused': sum(x['transition'] == 'newly_refused' for x in changes),
            'refusal_changed': sum(x['transition'] == 'refusal_changed' for x in changes),
            'candidate_matched': sum(x['candidate_expectation_matched'] for x in changes),
            'cases': changes, 'boundary': BOUNDARY}


def source_identity():
    import llmgate.config
    import llmgate.tokenauth
    import actas.tokens
    root = Path(inspect.getfile(llmgate.config)).resolve().parents[1]
    git = subprocess.run(['git', '-C', str(root), 'rev-parse', 'HEAD'], capture_output=True, text=True, timeout=10)
    status = subprocess.run(['git', '-C', str(root), 'status', '--porcelain'], capture_output=True, text=True, timeout=10)
    if git.returncode or status.returncode or status.stdout:
        raise ValueError('clean gateway Git source required')
    modules = {}
    for module in (llmgate.config, llmgate.tokenauth, actas.tokens):
        path = Path(inspect.getfile(module)).resolve()
        modules[module.__name__] = {'relative_path': str(path.relative_to(root)), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
    return {'gateway_git_head': git.stdout.strip(), 'native_imports': modules}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for arg in ('baseline', 'candidate', 'cases'):
        p.add_argument('--' + arg, required=True)
    p.add_argument('--gateway-source', required=True, help='Clean gateway checkout containing its embedded Actas runtime')
    args = p.parse_args()
    try:
        expected_source = Path(args.gateway_source).resolve(strict=True)
        sys.path.insert(0, str(expected_source))
        baseline, bh = read_json(args.baseline); candidate, ch = read_json(args.candidate); cases, qh = read_json(args.cases)
        identity = source_identity()
        import llmgate.config
        if Path(inspect.getfile(llmgate.config)).resolve().parents[1] != expected_source:
            raise ValueError('native gateway import mismatch')
        report = asyncio.run(compare(baseline, candidate, cases))
        if source_identity() != identity:
            raise ValueError('native source changed during comparison')
        report['source'] = identity
        report['input_sha256'] = {'baseline': bh, 'candidate': ch, 'cases': qh}
    except Exception as exc:
        # Ordinary dependency/native execution failures are not policy regressions.
        # BaseException (KeyboardInterrupt/SystemExit) is deliberately not caught.
        report = {'schema_version': 1, 'state': 'error', 'error_type': type(exc).__name__, 'boundary': BOUNDARY}
    print(json.dumps(report, indent=2))
    return {'pass': 0, 'review_required': 1, 'error': 2}[report['state']]


if __name__ == '__main__':
    sys.exit(main())
