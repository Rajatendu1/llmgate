"""Landing copy must not infer provider access, price or model support."""
from llmgate.demo_html import DEMO_HTML


def test_initial_landing_copy_does_not_assert_real_access_or_free_pricing():
    intro = DEMO_HTML.split('<p class="sub">')[1].split('</p>')[0]
    assert 'is connected' not in intro
    assert 'No paid model' not in intro
    assert 'matching configured route' in intro
    assert 'pricing' in intro


def test_real_control_requires_matching_alias_not_any_real_provider():
    assert "const hasReal = realProvs.some(p => (p.models || []).includes('gpt-real'));" in DEMO_HTML
    assert "const hasBackup = realProvs.some(p => (p.models || []).includes('gpt-real-backup'));" in DEMO_HTML
    assert 'This deployment is simulated-only. No real-provider route is configured.' in DEMO_HTML


def test_copy_does_not_infer_real_configuration_from_auth_rejection():
    assert 'the real route refuses them (403)' not in DEMO_HTML
    assert 'rejection of an unavailable alias does not prove a real route is configured' in DEMO_HTML
    assert 'server-side visitor key you never see' not in DEMO_HTML
    assert 'an in-process visitor identity, not a reusable bearer key' in DEMO_HTML


def test_shared_store_and_price_claims_are_conditional():
    assert 'Per-key rate limits and usage totals are kept in a shared Redis store' not in DEMO_HTML
    assert 'in-memory limits do not' in DEMO_HTML
    assert 'account and are not verified by this page' in DEMO_HTML
    assert 'free tier, $0.00' not in DEMO_HTML
