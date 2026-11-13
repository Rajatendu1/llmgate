"""HTTP admission remains enforced in normal and optimized Python processes."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


# Use real child interpreters, not pytest assertions compiled under -O. The
# child reports HTTP/provider facts; the parent independently checks them.
PROBE = r'''
import asyncio, json, httpx
from llmgate.app import ApiKey, create_app
from llmgate.providers import MockProvider
from llmgate.router import Route, Router

async def main():
    cases = json.loads(input())
    results = []
    for body in cases:
        provider = MockProvider('sim', ['m'], latency=0, token_delay=0)
        app = create_app(Router([Route(provider)]), {'k': ApiKey('test')})
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(
                    app=app, raise_app_exceptions=False), base_url='http://local') as client:
                response = await client.post('/v1/chat/completions',
                    headers={'Authorization': 'Bearer k'}, json=body)
        results.append({'status': response.status_code, 'calls': provider.calls,
                        'type': response.headers.get('content-type'),
                        'body': response.text})
    print(json.dumps(results))

asyncio.run(main())
'''

INVALID = [None, [], 'not an object', {},
           {'model': 1, 'messages': [{}]},
           {'model': 'm'}, {'model': 'm', 'messages': []},
           {'model': 'm', 'messages': 'text'},
           {'model': 'm', 'messages': ['text']},
           {'model': 'm', 'messages': [None]}]
INVALID += [{'model': 'm', 'messages': [{}], 'stream': flag}
            for flag in [0, 1, 0.0, 1.0, 'false', 'true', None, [], {}]]
VALID = [{'model': 'm', 'messages': [{'role': 'user', 'content': 'hello'}], **extra}
         for extra in [{}, {'stream': False}, {'stream': True}]]


@pytest.mark.parametrize('optimized', [False, True])
def test_actual_process_admission_and_valid_completion(optimized):
    root = Path(__file__).resolve().parents[1]
    # No inherited provider, store or error-tracking credentials. Only this
    # trusted checkout and installed dependencies are needed for local mocks.
    env = {'PATH': os.environ.get('PATH', ''), 'PYTHONPATH': str(root)}
    args = [sys.executable] + (['-O'] if optimized else []) + ['-c', PROBE]
    child = subprocess.run(args, input=json.dumps(INVALID + VALID), text=True,
                           capture_output=True, env=env, cwd=root, timeout=30)
    assert child.returncode == 0, child.stderr
    rows = json.loads(child.stdout)
    assert len(rows) == len(INVALID) + len(VALID)
    for body, row in zip(INVALID, rows):
        assert row['status'] == 400, (body, row)
        assert row['calls'] == 0, (body, row)
        assert json.loads(row['body'])['error']['type'] == 'invalid_request'
    for row in rows[len(INVALID):]:
        assert row['status'] == 200, row
        assert row['calls'] == 1, row
    assert rows[-1]['type'].startswith('text/event-stream')
    assert 'data: [DONE]' in rows[-1]['body']
