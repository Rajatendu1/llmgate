"""Public Try has no public-usable bearer and no arbitrary real alias path."""
import httpx
import pytest
from llmgate.app import ApiKey, create_app
from llmgate.demo_try import install_demo_try, PROMPT, REAL_MAX_TOKENS
from llmgate.providers import MockProvider
from llmgate.router import Router, Route


def make(mixed=False):
    sim=MockProvider('sim',['gpt-sim'],latency=0)
    real=MockProvider('real',['gpt-real']+(['gpt-sim'] if mixed else []),latency=0)
    real.mode='real'
    seen=[]
    original=real.complete
    async def capture(body, headers=None):
        seen.append(body)
        return await original(body,headers=headers)
    real.complete=capture
    keys={'static':ApiKey('static')}
    app=create_app(Router([Route(sim),Route(real)]),keys)
    install_demo_try(app,model='gpt-sim')
    return app,real,seen


@pytest.mark.parametrize('token',['invalid-sample-simulated-bearer','invalid-sample-real-bearer'])
async def test_arbitrary_sample_bearers_do_not_authenticate(token):
    app,real,_=make()
    assert list(app.state.keys)==['static']
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://t') as c:
        for path,method in [('/v1/models','GET'),('/v1/chat/completions','POST')]:
            r=await c.request(method,path,headers={'Authorization':'Bearer '+token},
                              json={'model':'gpt-real','messages':[{'role':'user','content':'arbitrary'}]})
            assert r.status_code==401
        assert real.calls==0


@pytest.mark.parametrize('header',['llmgate.internal_chat_identity','x-llmgate-internal-chat-identity','x-internal-identity'])
async def test_headers_cannot_forge_internal_identity(header):
    app,real,_=make()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://t') as c:
        r=await c.post('/v1/chat/completions',headers={header:'visitor-real'},
                       json={'model':'gpt-real','messages':[{'role':'user','content':'arbitrary'}]})
        assert r.status_code==401 and real.calls==0


@pytest.mark.parametrize('selector',['gpt-real','gpt-sim','other',''])
async def test_arbitrary_query_alias_cannot_reach_real_or_sim_routes(selector):
    app,real,_=make()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://t') as c:
        r=await c.post('/demo/try',params={'model':selector},json={'prompt':'arbitrary'})
        assert r.status_code==400 and real.calls==0


async def test_custom_prompt_cannot_use_mixed_simulated_alias():
    app,real,_=make(mixed=True)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://t') as c:
        r=await c.post('/demo/try',json={'prompt':'arbitrary'})
        assert r.status_code==404 and real.calls==0


async def test_explicit_real_selector_keeps_fixed_prompt_and_bound_budget_shape():
    app,real,seen=make()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://t') as c:
        refused=await c.post('/demo/try?model=real',json={'prompt':'arbitrary'})
        assert refused.status_code==400 and real.calls==0
        r=await c.post('/demo/try?model=real')
        assert r.status_code==200 and r.json()['ok']
        assert seen[0]['messages']==[{'role':'user','content':PROMPT}]
        assert seen[0]['max_tokens']==REAL_MAX_TOKENS
        assert app.state.keys.keys()=={'static'}


async def test_unregistered_scope_object_cannot_authenticate():
    app,real,_=make()
    async def spoof(scope,receive,send):
        await app({**scope,'llmgate.internal_chat_identity':ApiKey('visitor-real')},receive,send)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=spoof),base_url='http://t') as c:
        r=await c.post('/v1/chat/completions',json={'model':'gpt-real','messages':[{'role':'user','content':'hi'}]})
        assert r.status_code==401 and real.calls==0
