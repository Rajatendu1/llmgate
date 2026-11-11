"""Visitor-controlled fault injection for the simulated fleet.

A self-serve demo key may send X-Simulate-Failure on /v1/chat/completions to
make the SIMULATED provider misbehave on the first attempt of that one
request, so an LLM client can be regression-tested against the gateway's real
retry, failover, timeout and mid-stream-abort behavior over live HTTP.

Truthfulness rules:
- the fault is consumed by the first provider attempt; any retry or failover
  leg runs clean, so what the client observes is the gateway's genuine policy
  (retry in place, fail over, or refuse), not a scripted story;
- only minted demo keys may inject, and their model allowlist already limits
  them to simulated aliases - a real upstream can never see an injected fault;
- "slow" is capped at SLOW_EXTRA_S and "truncate" is stream-only, so injection
  cannot hold resources or corrupt a non-stream response.
"""
from __future__ import annotations
import contextvars

ALLOWED = ("429", "500", "slow", "truncate")
SLOW_EXTRA_S = 3.0

class _Fault:
    """Mutable holder: each provider attempt runs in its own task (the router
    wraps attempts in asyncio.wait_for), and a new task COPIES the context -
    a contextvar set inside one attempt would be invisible to the next. The
    copied binding still references the same holder, so `used` is seen by
    every attempt of the request."""
    __slots__ = ("kind", "used")

    def __init__(self, kind: str):
        self.kind, self.used = kind, False


_current: contextvars.ContextVar[_Fault | None] = contextvars.ContextVar(
    "llmgate_fault", default=None)


def arm(value: str) -> None:
    _current.set(_Fault(value))


def peek() -> str | None:
    f = _current.get()
    return f.kind if f is not None and not f.used else None


def take() -> str | None:
    """Consume-once: the first provider attempt of this request faults, later
    attempts (retry/failover legs) see a clean provider."""
    f = _current.get()
    if f is None or f.used:
        return None
    f.used = True
    return f.kind
