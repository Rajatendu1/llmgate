"""Gateway integration checks for the recovered, unmodified Actas 0.14.1 pin.

These are gateway regressions, not the lost original Actas development suite.
No network or real-model calls.
"""
import hashlib
import json
from pathlib import Path

import jwt
import pytest

from actas import __version__
from actas.keys import KeyStore
from actas.tokens import issue_token
from test_tokenauth import AUD, ISSUER, make_app, post, tok


def test_vendored_package_exactly_matches_recovered_manifest():
    root = Path(__file__).resolve().parents[1]
    manifest = json.loads((root / 'vendor/actas-0.14.1-files.sha256.json').read_text())
    assert __version__ == '0.14.1'
    assert len(manifest) == 22
    assert set(manifest) == {str(p.relative_to(root)) for p in (root / 'actas').glob('*.py')}
    for path, digest in manifest.items():
        assert hashlib.sha256((root / path).read_bytes()).hexdigest() == digest


def signed(ks, **changes):
    _, claims = issue_token(ks, issuer=ISSUER, subject='profile-regression',
                            scope='llm:complete', audience=[AUD], expires_in=600)
    for key, value in changes.items():
        if value is None:
            claims.pop(key, None)
        else:
            claims[key] = value
    return jwt.encode(claims, ks.current.private_pem(), algorithm='RS256',
                      headers={'kid': ks.current.kid})


@pytest.mark.parametrize('changes', [
    {'jti': None}, {'aud': None}, {'scope': ['llm:complete']},
    {'scope': 'llm:complete  llm:expensive-models'},
    {'act': {'sub': ['not-an-actor']}}, {'act': 'not-a-chain'},
    {'cnf': {'jkt': 'malformed'}}, {'exp': True}, {'iat': 'not-a-date'},
])
async def test_malformed_signed_profile_is_401_not_server_error(changes):
    app, ks = make_app()
    r = await post(app, signed(ks, **changes))
    assert r.status_code == 401
    assert all(r.provider.calls == 0 for r in app.state.router.routes)


async def test_overdeep_signed_chain_is_refused_at_gateway_boundary():
    app, ks = make_app()
    node = {'sub': 'oldest'}
    for i in range(32):
        node = {'sub': f'actor-{i}', 'act': node}
    assert (await post(app, signed(ks, act=node))).status_code == 401


async def test_rotated_embedded_key_keeps_old_token_until_expiry():
    app, ks = make_app()
    before = tok(ks)
    ks.rotate()
    after = tok(ks)
    assert (await post(app, before)).status_code == 200
    assert (await post(app, after)).status_code == 200


async def test_unbound_token_does_not_gain_claim_supplied_models_or_budget():
    app, ks = make_app()
    token = signed(ks, models=['gpt-sim-pro'], rate=999999, burst=999999)
    assert (await post(app, token, model='gpt-sim-pro')).status_code == 403
    responses = [(await post(app, token)).status_code for _ in range(6)]
    assert 429 in responses
