import random
import httpx
from llmgate.app import ApiKey, create_app
from llmgate.providers import MockProvider
from llmgate.resilience import CircuitBreaker
from llmgate.router import Route, Router

ADM = {"Authorization": "Bearer adm"}
H = {"Authorization": "Bearer k"}
BODY = {"model": "m", "messages": [{"role": "user", "content": "hello there world"}]}


def client(routes, keys=None):
    app = create_app(Router(routes, random.Random(1), failover_ambiguous=True),  # simulated providers
                     keys or {"k": ApiKey("tenant"), "adm": ApiKey("adm", admin=True)})
    return app, httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


def mp(name, **kw):
    return MockProvider(name, ["m"], latency=0, token_delay=0, seed=1, **kw)


def parse(text):
    """Minimal exposition parse: {metric[(labelset)]: (labels_dict, value)}, plus comments skipped."""
    import re
    out = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        m = re.fullmatch(r"([a-z_][a-z0-9_]*)(?:\{(.*)\})? (\S+)", line)
        assert m, f"unparseable line: {line!r}"
        name, labels, value = m.group(1), m.group(2) or "", float(m.group(3))
        out.setdefault(name, []).append((labels, value))
    return out


async def test_metrics_requires_admin():
    _, c = client([Route(mp("a"))])
    assert (await c.get("/metrics")).status_code == 401
    assert (await c.get("/metrics", headers=H)).status_code == 401  # normal key is not enough
    r = await c.get("/metrics", headers=ADM)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain; version=0.0.4")


async def test_metrics_counts_requests_by_key_and_status():
    _, c = client([Route(mp("a"))], {"k": ApiKey("tenant", rate=1, burst=1), "adm": ApiKey("adm", admin=True)})
    assert (await c.post("/v1/chat/completions", json=BODY, headers=H)).status_code == 200
    assert (await c.post("/v1/chat/completions", json=BODY, headers=H)).status_code == 429
    m = parse((await c.get("/metrics", headers=ADM)).text)
    labels = dict(m["llmgate_requests_total"])
    assert labels['key="tenant",status="ok"'] == 1
    assert labels['key="tenant",status="rate_limited"'] == 1
    # pre-routing rejections are not attributed to a provider
    assert all('provider="-"' not in l for l, _ in m["llmgate_provider_requests_total"])


async def test_metrics_tokens_and_provider_rows():
    _, c = client([Route(mp("bad", down=True), 0), Route(mp("good"), 1)])
    await c.post("/v1/chat/completions", json=BODY, headers=H)
    m = parse((await c.get("/metrics", headers=ADM)).text)
    tok = dict(m["llmgate_tokens_total"])
    assert tok['key="tenant",kind="prompt"'] == 3 and tok['key="tenant",kind="completion"'] > 0
    prov = dict(m["llmgate_provider_requests_total"])
    assert prov['provider="good",status="ok"'] == 1


async def test_metrics_breaker_gauge():
    bad, good = mp("bad", down=True), mp("good")
    _, c = client([Route(bad, 0, breaker=CircuitBreaker(2, 100)), Route(good, 1)])
    for _ in range(3):
        await c.post("/v1/chat/completions", json=BODY, headers=H)
    m = parse((await c.get("/metrics", headers=ADM)).text)
    g = dict(m["llmgate_provider_breaker_state"])
    assert g['provider="bad"'] == 2 and g['provider="good"'] == 0  # open / closed


async def test_metrics_latency_summary():
    _, c = client([Route(mp("a"))])
    for _ in range(3):
        await c.post("/v1/chat/completions", json=BODY, headers=H)
    m = parse((await c.get("/metrics", headers=ADM)).text)
    count = [v for l, v in m["llmgate_request_latency_seconds_count"] if l == ""][0]
    total = [v for l, v in m["llmgate_request_latency_seconds_sum"] if l == ""][0]
    p95 = [v for l, v in m["llmgate_request_latency_seconds"] if 'quantile="0.95"' in l][0]
    assert count == 3 and total > 0 and p95 >= 0


async def test_metrics_format_and_label_escaping():
    app, c = client([Route(mp("a"))], {'s"cr\\et': ApiKey('ten"ant\\x'), "adm": ApiKey("adm", admin=True)})
    await c.post("/v1/chat/completions", json=BODY, headers={"Authorization": 'Bearer s"cr\\et'})
    r = await c.get("/metrics", headers=ADM)
    text = r.text
    assert 'key="ten\\"ant\\\\x",status="ok"' in text
    # every series has HELP and TYPE; every sample line parses
    names = [l.split()[2] for l in text.splitlines() if l.startswith("# TYPE")]
    helps = [l.split()[2] for l in text.splitlines() if l.startswith("# HELP")]
    assert sorted(names) == sorted(helps) and len(names) == 6
    parse(text)
