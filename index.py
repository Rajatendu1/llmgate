"""Public-demo entry point for serverless/container deploys.

Uses the project's config factory. Simulated providers are always configured;
real routes activate only when their required upstream credential is present. Client keys come from the LLMGATE_KEYS environment variable
(see llmgate.app.keys_from_env); nothing secret lives in this file.
Adds a static landing page at GET / so the root URL is usable in a browser.
"""
import os
from fastapi.responses import HTMLResponse
from llmgate.config import build_app
from demo_page import DEMO_HTML
from llmgate.demo_events import events_payload
from llmgate.demo_stats import stats_payload
from llmgate.demo_try import install_demo_try
from llmgate.demo_keys import install_demo_keys

app = build_app(os.path.join(os.path.dirname(os.path.abspath(__file__)), "deploy.config.json"))


# The API's baseline CSP forbids inline assets; the landing page needs its own.
_LANDING_HEADERS = {"Cache-Control": "no-store", "Content-Security-Policy":
                    "default-src 'none'; style-src 'unsafe-inline' https://fonts.googleapis.com; "
                    "font-src https://fonts.gstatic.com; img-src data:; "
                    "script-src 'unsafe-inline'; connect-src 'self'"  # the page's own status check fetches /healthz
                    }


@app.get("/", response_class=HTMLResponse)
async def demo_landing():
    return HTMLResponse(DEMO_HTML, headers=_LANDING_HEADERS)


@app.get("/demo/stats")
async def demo_stats():
    return await stats_payload(app)


@app.get("/demo/events")
async def demo_events():
    return await events_payload(app)


install_demo_try(app, model="gpt-sim")
install_demo_keys(app)
