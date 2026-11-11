"""Routing + failover.

Candidates for a model = providers serving it whose breaker allows traffic.
Strategy: lowest `priority` tier first; inside a tier, weighted-random order.

Failure semantics (see ProviderError for the categories):
- DECLINED errors (connect-phase failure: the request provably was never sent;
  or a 429, assumed rejected at admission control before generation - true for
  the major providers, but a policy assumption, not a contract guarantee) are
  retried in place with a bounded jittered backoff before failing over.
- AMBIGUOUS errors (timeout, 408, 5xx, mid-request transport failure) may have
  generated upstream. They are never retried in place. Failing over can then
  duplicate generation - with a real provider that means a possible duplicate
  charge - so it is controlled by `failover_ambiguous`, default False
  (duplicate-averse: surface the error). Opt in to availability-first failover
  only where a duplicate is acceptable (e.g. simulated providers) or where the
  provider honors the forwarded client Idempotency-Key.
- FATAL errors (other 4xx) are the caller's bug and are surfaced immediately.
  A client-supplied Idempotency-Key is forwarded unchanged to every attempt
  whose provider accepts end-to-end headers; providers written before them
  are served WITHOUT the key, so their retries carry no vendor-side dedupe.

For streams, failover/retry is only possible BEFORE the first chunk is sent to
the client - once bytes have gone out, a mid-stream failure is surfaced as an
error event (we never splice outputs from two models)."""
from __future__ import annotations
import asyncio, inspect, logging, random, time
from dataclasses import dataclass, field
from .providers import Provider, ProviderError, estimate_request_tokens
from .resilience import CircuitBreaker

log = logging.getLogger('llmgate')


def _accepts_headers(fn) -> bool:
    """True when fn can be called with a `headers` keyword: it names the
    parameter or takes **kwargs. Uninspectable callables are assumed modern."""
    try:
        params = inspect.signature(fn).parameters.values()
    except (TypeError, ValueError):
        return True
    return any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params) or \
           any(p.name == "headers" for p in params)


_MODERN = lambda body, headers=None: None  # stand-in for a missing method


class NoProviderAvailable(Exception):
    pass


@dataclass(frozen=True)
class Price:
    """Config-provided list price, USD per 1M tokens (prompt / completion).

    Prices are data, not code: provider list prices change, and real bills add
    discounts, cached-token and batch rates this table does not model. Costs
    computed from it are estimates - they answer "who is spending what,
    roughly", and they are not billing-grade."""
    prompt_per_mtok: float = 0.0
    completion_per_mtok: float = 0.0

    def cost(self, usage: dict | None) -> float:
        if not usage:
            return 0.0
        return (usage.get("prompt_tokens", 0) * self.prompt_per_mtok
                + usage.get("completion_tokens", 0) * self.completion_per_mtok) / 1_000_000


class IdleGuard:
    """Wraps a chunk iterator: if no chunk arrives within `idle` seconds, raise ProviderError."""

    def __init__(self, it, idle):
        self.it, self.idle = it, idle

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return await asyncio.wait_for(self.it.__anext__(), self.idle)
        except asyncio.TimeoutError:
            raise ProviderError(f"stream stalled: no chunk for {self.idle}s")

    async def aclose(self):
        aclose = getattr(self.it, "aclose", None)
        if aclose:
            await aclose()


@dataclass
class Route:
    provider: Provider
    priority: int = 0
    weight: float = 1.0
    breaker: CircuitBreaker = field(default_factory=CircuitBreaker)
    timeout: float = 30.0  # seconds: whole call (non-stream) or time to first chunk (stream)
    idle_timeout: float = 30.0  # seconds allowed between chunks once a stream has started
    retries: int = 1  # same-provider retries before failing over; DECLINED attempts only
    retry_backoff: float = 0.25  # base seconds, doubling per retry, +/-50% jitter
    price: Price | None = None  # list price for this provider+model; None = cost not tracked
    timeout_per_1k: float = 0.0  # extra seconds per 1k estimated request tokens (0 = fixed timeout)
    timeout_max: float | None = None  # ceiling on the size-scaled timeout; None = uncapped


class Router:
    def __init__(self, routes: list[Route], rng=None, sleep=None, failover_ambiguous: bool = False):
        self.routes = routes
        self.rng = rng or random.Random()
        self.sleep = sleep  # injectable for tests; defaults to asyncio.sleep
        # Fail over after AMBIGUOUS failures (timeout/408/5xx)? Default False
        # (duplicate-averse): availability can be opted into where a possible
        # duplicate generation is acceptable.
        self.failover_ambiguous = failover_ambiguous
        # Per-provider capability: does its complete/stream accept the end-to-end
        # `headers` kwarg? Providers written against the older one-argument
        # interface simply do not receive a client Idempotency-Key; their
        # requests must still be served, never fail with a TypeError.
        self._hdr_ok = {id(r.provider): (_accepts_headers(getattr(r.provider, "complete", _MODERN)),
                                         _accepts_headers(getattr(r.provider, "stream", _MODERN)))
                        for r in routes}

    def _hdr_kw(self, provider, which: int, headers: dict | None) -> dict:
        """The kwargs to add for end-to-end headers: only when the client sent
        some AND this provider's signature can receive them."""
        if headers and self._hdr_ok[id(provider)][which]:
            return {"headers": headers}
        if headers:
            log.debug("%s predates end-to-end headers; Idempotency-Key not forwarded to it",
                      getattr(provider, "name", provider))
        return {}

    async def _backoff(self, r: Route, attempt: int):
        delay = r.retry_backoff * (2 ** attempt) * (0.5 + self.rng.random())
        if self.sleep is not None:
            await self.sleep(delay)
        else:
            await asyncio.sleep(delay)

    def models(self):
        return sorted({m for r in self.routes for m in r.provider.models})

    def timeout_for(self, r: Route, body: dict) -> float:
        """Effective timeout for one request: the route's fixed timeout, optionally
        grown by the estimated request size (prompt estimate + max_tokens cap) at
        timeout_per_1k seconds per 1k tokens, capped at timeout_max. Long prompts
        and long completions legitimately take longer; a single fixed timeout
        either kills them early or makes every call wait out the worst case."""
        if r.timeout_per_1k <= 0:
            return r.timeout
        t = r.timeout + r.timeout_per_1k * estimate_request_tokens(body) / 1000.0
        return min(r.timeout_max, t) if r.timeout_max is not None else t

    def mode_for(self, provider_name: str) -> str:
        """"simulated" or "real" for the named provider, for honest dashboard labels."""
        for r in self.routes:
            if r.provider.name == provider_name:
                return getattr(r.provider, "mode", "real")
        return "real"

    def price_for(self, provider_name: str, model: str) -> Price | None:
        """List price for the (provider, model) pair that served a call, if configured."""
        for r in self.routes:
            if r.provider.name == provider_name and model in r.provider.models:
                return r.price
        return None

    def order(self, model: str) -> list[Route]:
        cands = [r for r in self.routes if model in r.provider.models]
        out = []
        for prio in sorted({r.priority for r in cands}):
            tier = [r for r in cands if r.priority == prio]
            # weighted shuffle (Efraimidis-Spirakis)
            tier.sort(key=lambda r: self.rng.random() ** (1.0 / max(r.weight, 1e-9)), reverse=True)
            out.extend(tier)
        return out

    @staticmethod
    def _trip(r: "Route"):
        r.breaker.failure()
        if r.breaker.state == "open":  # one log line per trip, not per failure
            log.warning("circuit breaker OPEN for provider %s", r.provider.name)

    async def complete(self, body: dict, timings: dict | None = None, headers: dict | None = None):
        """Optional `timings` dict is filled (even on failure) with the measured
        split of the router call: provider = time inside upstream calls,
        routing = the rest (candidate ordering, breaker checks, retry backoff).
        Optional `headers` are end-to-end client headers (Idempotency-Key)
        forwarded verbatim to every attempt, so a provider that honors them
        can dedupe a duplicated generation from a retry or failover."""
        r0 = time.perf_counter()
        provider_t = 0.0

        async def _call(route):
            nonlocal provider_t
            p0 = time.perf_counter()
            try:
                return await asyncio.wait_for(
                    route.provider.complete(body, **self._hdr_kw(route.provider, 0, headers)),
                    self.timeout_for(route, body))
            finally:
                provider_t += time.perf_counter() - p0

        try:
            errors, tried = [], []
            for r in self.order(body["model"]):
                if not r.breaker.allow():
                    continue
                tried.append(r.provider.name)
                for attempt in range(1 + r.retries):
                    try:
                        res = await _call(r)
                        r.breaker.success()
                        return r.provider.name, res, tried
                    except asyncio.TimeoutError:
                        # No same-provider retry on timeout: the request may have
                        # executed upstream, and re-sending risks a duplicate
                        # generation. Fail over instead (existing semantics).
                        self._trip(r)
                        errors.append(f"{r.provider.name}: timeout after {self.timeout_for(r, body):.1f}s")
                        break
                    except ProviderError as e:
                        self._trip(r)
                        errors.append(f"{r.provider.name}: {e}")
                        if e.category == ProviderError.FATAL:
                            raise
                        if e.category == ProviderError.AMBIGUOUS:
                            # May have generated upstream: never retry in place.
                            if not self.failover_ambiguous:
                                raise NoProviderAvailable(
                                    "; ".join(errors) + " (ambiguous failure; failover disabled)")
                            break
                        # DECLINED (connect error or 429): provably no generation, so
                        # a same-provider retry with backoff is safe before failover.
                        if attempt < r.retries:
                            await self._backoff(r, attempt)
            raise NoProviderAvailable("; ".join(errors) or "all providers have open circuits")
        finally:
            if timings is not None:
                timings["routing"] = time.perf_counter() - r0 - provider_t
                timings["provider"] = provider_t

    async def stream(self, body: dict, timings: dict | None = None, headers: dict | None = None):
        """Returns (provider_name, first_chunk, async_iter_of_rest, tried). Raises before first chunk.
        Optional `timings` dict: provider = measured time to the first chunk
        (per attempt), routing = the rest, as in complete(). Optional `headers`
        are forwarded verbatim to every attempt, as in complete()."""
        r0 = time.perf_counter()
        provider_t = 0.0
        try:
            errors, tried = [], []
            for r in self.order(body["model"]):
                if not r.breaker.allow():
                    continue
                tried.append(r.provider.name)
                for attempt in range(1 + r.retries):
                    it = r.provider.stream(body, **self._hdr_kw(r.provider, 1, headers)).__aiter__()
                    p0 = time.perf_counter()
                    try:
                        first = await asyncio.wait_for(it.__anext__(), self.timeout_for(r, body))
                    except asyncio.TimeoutError:
                        # same rule as complete(): no retry on timeout
                        provider_t += time.perf_counter() - p0
                        self._trip(r)
                        errors.append(f"{r.provider.name}: no first chunk after {self.timeout_for(r, body):.1f}s")
                        break
                    except ProviderError as e:
                        # still before the first chunk: same rules as complete()
                        provider_t += time.perf_counter() - p0
                        self._trip(r)
                        errors.append(f"{r.provider.name}: {e}")
                        if e.category == ProviderError.FATAL:
                            raise
                        if e.category == ProviderError.AMBIGUOUS:
                            if not self.failover_ambiguous:
                                raise NoProviderAvailable(
                                    "; ".join(errors) + " (ambiguous failure; failover disabled)")
                            break
                        if attempt < r.retries:  # DECLINED only
                            await self._backoff(r, attempt)
                            continue
                        break
                    except StopAsyncIteration:
                        provider_t += time.perf_counter() - p0
                        first = None
                    else:
                        provider_t += time.perf_counter() - p0
                    r.breaker.success()
                    return r.provider.name, first, IdleGuard(it, r.idle_timeout), tried
            raise NoProviderAvailable("; ".join(errors) or "all providers have open circuits")
        finally:
            if timings is not None:
                timings["routing"] = time.perf_counter() - r0 - provider_t
                timings["provider"] = provider_t
