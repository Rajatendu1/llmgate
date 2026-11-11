"""Bounded application intake, not a claim about ASGI/server buffering."""
import httpx
import pytest
from llmgate.app import ApiKey, create_app, read_bounded, MAX_BODY
from llmgate.demo_try import install_demo_try, TRY_BODY_CAP
from llmgate.providers import MockProvider
from llmgate.router import Route, Router


class RequestChunks:
    def __init__(self, chunks, headers=None):
        self.headers = headers or {}
        self.chunks = chunks
        self.pulled = 0

    async def stream(self):
        for chunk in self.chunks:
            self.pulled += 1
            yield chunk


async def test_declared_oversize_reads_no_chunks():
    r = RequestChunks([b'x'], {'content-length': '11'})
    assert await read_bounded(r, 10) is None
    assert r.pulled == 0


@pytest.mark.parametrize('headers', [{}, {'content-length': '1'}, {'content-length': 'garbled'}])
async def test_actual_bytes_enforced_without_trusting_length(headers):
    r = RequestChunks([b'12345', b'678901', b'NEVER'], headers)
    assert await read_bounded(r, 10) is None
    assert r.pulled == 2


@pytest.mark.parametrize('chunks', [[b'12345', b'67890'], [b'1234567890'], [b'', b'1234567890', b'']])
async def test_exact_boundary_and_empty_chunks(chunks):
    assert await read_bounded(RequestChunks(chunks), 10) == b'1234567890'


async def test_limit_is_bytes_not_unicode_character_count():
    assert await read_bounded(RequestChunks(['é'.encode()]), 1) is None


def app_and_provider():
    p = MockProvider('sim', ['gpt-sim'], latency=0)
    app = create_app(Router([Route(p)]), {'k': ApiKey('test')})
    install_demo_try(app, model='gpt-sim')
    return app, p


async def test_chat_declared_oversize_returns_413_without_upstream():
    app, p = app_and_provider()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://t') as c:
        r = await c.post('/v1/chat/completions', headers={'Authorization':'Bearer k'},
                         content=b'x'*(MAX_BODY+1))
        assert r.status_code == 413
        assert 'admission' in r.headers['server-timing']
        assert p.calls == 0


async def test_chat_chunked_oversize_stops_at_crossing_chunk():
    app, p = app_and_provider()
    pulled = []
    async def chunks():
        for n in range(4):
            pulled.append(n)
            yield b'x'*500_001
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://t') as c:
        r = await c.post('/v1/chat/completions', headers={'Authorization':'Bearer k'}, content=chunks())
        assert r.status_code == 413
        assert pulled == [0, 1]
        assert p.calls == 0


@pytest.mark.parametrize('content_type', ['application/json', 'text/plain'])
async def test_try_oversize_cannot_bypass_with_content_type(content_type):
    app, p = app_and_provider()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://t') as c:
        r = await c.post('/demo/try', headers={'content-type':content_type}, content=b'x'*(TRY_BODY_CAP+1))
        assert r.status_code == 413
        assert p.calls == 0
