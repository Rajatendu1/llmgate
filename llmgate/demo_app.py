"""DEMO ONLY: hard-coded keys. Real use: build keys from env/secret store (see keys_from_env).
Demo wiring: two simulated providers for one model (A flaky/primary, B backup)."""
import os
from .app import ApiKey, create_app
from .providers import MockProvider
from .resilience import CircuitBreaker
from .router import Price, Route, Router

a = MockProvider("sim-a", ["gpt-sim"], latency=0.03, token_delay=float(os.getenv("SIM_TOKEN_DELAY", "0.002")), fail_rate=float(os.getenv("SIM_A_FAIL", "0.0")))
b = MockProvider("sim-b", ["gpt-sim"], latency=0.05)
# Simulated list prices (USD per 1M tokens) so cost metering is visible in the
# demo. The backup is priced higher: failover keeps answers flowing at a higher
# estimated cost, and /admin/usage shows that trade-off.
# Availability-first failover is explicitly opted into: every provider here
# is simulated, so a duplicated "generation" costs nothing.
router = Router([Route(a, priority=0, breaker=CircuitBreaker(3, 5), price=Price(0.40, 1.20),
                          timeout=10, timeout_per_1k=2.0, timeout_max=30),
                 Route(b, priority=1, breaker=CircuitBreaker(3, 5), price=Price(0.80, 2.40),
                          timeout=10, timeout_per_1k=2.0, timeout_max=30)])
_tr = os.getenv("DEMO_TOKEN_RATE")  # optional tokens/sec budget for the demo key
router.failover_ambiguous = True
app = create_app(router, {"demo-key": ApiKey("demo", rate=float(os.getenv("DEMO_RATE", "50")), burst=float(os.getenv("DEMO_RATE", "50")) * 2,
                                             token_rate=float(_tr) if _tr else None),
                          "admin-key": ApiKey("admin", admin=True)})

# The local demo gets the same public stats endpoint as the serverless entry
# point (index.py), so the landing-page dashboard works on localhost too.
from .demo_events import events_payload  # noqa: E402
from .demo_stats import stats_payload  # noqa: E402
from .demo_html import DEMO_HTML  # noqa: E402
from fastapi.responses import HTMLResponse  # noqa: E402

# The API's baseline CSP forbids inline assets; the landing page needs its own.
_LANDING_HEADERS = {"Cache-Control": "no-store", "Content-Security-Policy":
                    "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
                    "connect-src 'self'"}


@app.get("/", response_class=HTMLResponse)
async def _demo_landing():
    return HTMLResponse(DEMO_HTML, headers=_LANDING_HEADERS)


@app.get("/demo/stats")
async def _demo_stats():
    return await stats_payload(app)


@app.get("/demo/events")
async def _demo_events():
    return await events_payload(app)


from .demo_try import install_demo_try  # noqa: E402
from .demo_keys import install_demo_keys  # noqa: E402

install_demo_try(app, model="gpt-sim")
install_demo_keys(app)
