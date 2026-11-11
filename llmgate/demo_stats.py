"""Secret-free live stats for the public demo dashboard (GET /demo/stats).

Aggregated, per-provider and total counters only: no per-key rows, no
secrets - so the public landing page can poll it. Simulated providers only;
the numbers describe demo traffic, nothing else."""
from __future__ import annotations
import inspect
import time

from fastapi.responses import JSONResponse

from .redismeter import MeterUnavailable, RedisMeter

# The dashboard polls while visible; caching the aggregate for a few seconds
# means concurrent viewers on one instance share a single store read instead
# of multiplying commands against the free-tier quota.
STATS_CACHE_TTL_S = 8.0


async def stats_payload(app) -> JSONResponse:
    """Aggregated demo counters. Public by design: provider and overall totals
    only. Per-key usage stays behind the admin key on /admin/usage.

    One shared-store read per cache window when Redis is configured; a store
    outage degrades the dashboard (503, not cached) without affecting the API."""
    now = time.monotonic()
    cached = getattr(app.state, "demo_stats_cache", None)
    if cached and now - cached[0] < STATS_CACHE_TTL_S:
        return JSONResponse(cached[1])
    meter, router = app.state.meter, app.state.router
    try:
        snap = meter.snapshot()
        if inspect.isawaitable(snap):
            snap = await snap
    except MeterUnavailable:
        return JSONResponse({"ok": False, "error": "metering store unavailable"}, status_code=503)
    totals = {"requests": 0, "ok": 0, "rate_limited": 0, "errors": 0,
              "prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0}
    for u in snap.get("keys", {}).values():
        totals["requests"] += u.get("requests", 0)
        totals["ok"] += u.get("status_ok", 0)
        totals["rate_limited"] += u.get("status_rate_limited", 0) + u.get("status_rate_limited_tokens", 0)
        totals["errors"] += (u.get("status_upstream_failed", 0) + u.get("status_stream_error", 0)
                             + u.get("status_client_disconnected", 0))
        totals["prompt_tokens"] += u.get("prompt_tokens", 0)
        totals["completion_tokens"] += u.get("completion_tokens", 0)
        totals["cost_usd"] += u.get("cost_usd", 0.0)
    providers = []
    for r in sorted(router.routes, key=lambda r: (r.priority, r.provider.name)):
        p = snap.get("providers", {}).get(r.provider.name, {})
        providers.append({
            "name": r.provider.name, "priority": r.priority, "breaker": r.breaker.state,
            "mode": getattr(r.provider, "mode", "real"),
            # public model aliases this provider serves (never upstream ids,
            # never secrets) - the page uses them to offer the backup path
            "models": sorted(r.provider.models),
            "requests": p.get("requests", 0), "ok": p.get("status_ok", 0),
            # simulated list prices, USD per 1M tokens (deploy.config.json, public source)
            "price": {"prompt_per_mtok": r.price.prompt_per_mtok,
                      "completion_per_mtok": r.price.completion_per_mtok} if r.price else None,
        })
    payload = {"ok": True, "metering": "redis" if isinstance(meter, RedisMeter) else "memory",
               "cache_ttl_s": STATS_CACHE_TTL_S, "totals": totals, "providers": providers}
    app.state.demo_stats_cache = (now, payload)
    return JSONResponse(payload)
