"""Secret-free recent-request feed for the public demo dashboard (GET /demo/events).

Serves the newest events from the request log (see request_log.py for the
redaction contract). Public by design: aggregate-style fields only, no prompt
content, no key secrets, no client IPs. Cached a few seconds like /demo/stats
so concurrent viewers on one instance share one store read.
"""
from __future__ import annotations
import inspect
import time

from fastapi.responses import JSONResponse

from .request_log import LogUnavailable, RedisRequestLog

EVENTS_CACHE_TTL_S = 8.0
PUBLIC_EVENTS = 20


async def events_payload(app) -> JSONResponse:
    now = time.monotonic()
    cached = getattr(app.state, "demo_events_cache", None)
    if cached and now - cached[0] < EVENTS_CACHE_TTL_S:
        return JSONResponse(cached[1])
    reqlog = app.state.reqlog
    try:
        latest = reqlog.latest(PUBLIC_EVENTS)
        if inspect.isawaitable(latest):
            latest = await latest
    except LogUnavailable:
        return JSONResponse({"ok": False, "error": "request-log store unavailable"}, status_code=503)
    payload = {"ok": True, "backend": "redis" if isinstance(reqlog, RedisRequestLog) else "memory",
               "cache_ttl_s": EVENTS_CACHE_TTL_S, "events": latest}
    app.state.demo_events_cache = (now, payload)
    return JSONResponse(payload)
