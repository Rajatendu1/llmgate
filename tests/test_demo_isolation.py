"""Visitor aliases cannot contain a real/unknown backup, even with duplicate names."""
import time
import httpx
import pytest
from llmgate.app import ApiKey, create_app
from llmgate.demo_keys import install_demo_keys, _MemoryStore, digest_of
from llmgate.providers import MockProvider, ProviderError
from llmgate.router import Router, Route


class Declined(MockProvider):
    def _maybe_fail(self):
        self.calls += 1
        raise ProviderError('local declined fixture', category=ProviderError.DECLINED)


def make(mode='real', duplicate_name=False):
    sim = Declined('same' if duplicate_name else 'sim', ['mixed'], latency=0)
    other = MockProvider('same' if duplicate_name else 'other', ['mixed'], latency=0)
    other.mode = mode
    safe = MockProvider('safe', ['safe'], latency=0)
    app = create_app(Router([Route(sim, priority=0, retries=0), Route(other, priority=1), Route(safe)]),
                     {'static': ApiKey('test')})
    return app, other


@pytest.mark.parametrize('mode', ['real', 'unknown', None])
@pytest.mark.parametrize('duplicate_name', [False, True])
def test_alias_with_any_non_simulated_route_is_not_mintable(mode, duplicate_name):
    app, _ = make(mode, duplicate_name)
    with pytest.raises(ValueError, match='no simulated alias'):
        install_demo_keys(app, models=('mixed',))


async def test_mixed_alias_filtered_safe_alias_usable_and_static_scope_unchanged():
    app, other = make()
    install_demo_keys(app, models=('mixed', 'safe'))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://t') as c:
        body = (await c.post('/demo/keys')).json()
        assert body['models'] == ['safe']
        h = {'Authorization':'Bearer '+body['key']}
        denied = await c.post('/v1/chat/completions',headers=h,
                              json={'model':'mixed','messages':[{'role':'user','content':'hi'}]})
        assert denied.status_code == 403
        assert other.calls == 0
        safe = await c.post('/v1/chat/completions',headers=h,
                            json={'model':'safe','messages':[{'role':'user','content':'hi'}]})
        assert safe.status_code == 200
        # Static keys retain their separate existing permission envelope.
        static = await c.post('/v1/chat/completions',headers={'Authorization':'Bearer static'},
                              json={'model':'mixed','messages':[{'role':'user','content':'hi'}]})
        assert static.status_code == 200 and other.calls == 1


async def test_old_persisted_record_cannot_reintroduce_unsafe_alias():
    app, other = make()
    store = _MemoryStore()
    token = 'vg_old-fleet-key'
    await store.put(digest_of(token), {'name':'self-serve','rate':50,'burst':50,
                                    'models':['mixed','safe'],'expires':time.time()+1000},1000)
    install_demo_keys(app, models=('mixed','safe'),store=store)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://t') as c:
        r=await c.post('/v1/chat/completions',headers={'Authorization':'Bearer '+token},
                       json={'model':'mixed','messages':[{'role':'user','content':'hi'}]})
        assert r.status_code == 401
        assert other.calls == 0
        assert token not in app.state.keys


async def test_all_simulated_alias_still_fails_over():
    app, other = make(mode='simulated')
    install_demo_keys(app, models=('mixed',))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://t') as c:
        key=(await c.post('/demo/keys')).json()['key']
        r=await c.post('/v1/chat/completions',headers={'Authorization':'Bearer '+key},
                       json={'model':'mixed','messages':[{'role':'user','content':'hi'}]})
        assert r.status_code == 200 and other.calls == 1


async def test_stream_request_to_mixed_alias_rejected_before_upstream():
    app, other = make()
    install_demo_keys(app, models=('mixed','safe'))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://t') as c:
        key=(await c.post('/demo/keys')).json()['key']
        r=await c.post('/v1/chat/completions',headers={'Authorization':'Bearer '+key},
                       json={'model':'mixed','stream':True,'messages':[{'role':'user','content':'hi'}]})
        assert r.status_code == 403 and other.calls == 0


async def test_revalidation_rejects_record_with_new_unsafe_scope():
    app, other = make()
    store = _MemoryStore()
    install_demo_keys(app, models=('mixed','safe'),store=store,revalidate_s=0)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://t') as c:
        key=(await c.post('/demo/keys')).json()['key']
        h={'Authorization':'Bearer '+key}
        body={'model':'safe','messages':[{'role':'user','content':'hi'}]}
        assert (await c.post('/v1/chat/completions',headers=h,json=body)).status_code == 200
        store._recs[digest_of(key)]['models'].append('mixed')
        assert (await c.post('/v1/chat/completions',headers=h,json=body)).status_code == 401
        assert other.calls == 0 and key not in app.state.keys
