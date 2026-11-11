"""Upstream providers. MockProvider simulates a model; HTTPProvider talks to any
OpenAI-compatible endpoint. Verification: tests/test_real_upstream.py pins the
request shape and error categorization against Groq-shaped responses on a mocked
transport; scripts/verify_real_upstream.py runs the same client against a live
service when credentials are present in the environment."""
from __future__ import annotations
import asyncio, json, random, time
from typing import AsyncIterator
import httpx

from . import faults


class ProviderError(Exception):
    """Failure categories, by whether the request may have generated upstream.

    DECLINED: the request provably never reached generation - a connect-phase
    failure (connect refused/reset, connect or pool timeout: nothing was ever
    sent) - or a 429, which the major providers emit at admission control
    before inference. The 429 case is a retry POLICY assumption about provider
    behavior, not a guarantee of the OpenAI-compatible contract; a provider
    that can 429 after starting work would make retries duplicates. Same-provider
    retry and failover are safe for true declined attempts.
    AMBIGUOUS: the request may have been processed - a timeout, a 408, or any
    5xx, all of which can arrive after generation started or finished upstream.
    Retrying or failing over can duplicate generation (duplicate billing with a
    real provider). Never retried in place; failover is a documented,
    configurable availability-first choice.
    FATAL: caller/config bug (other 4xx). Surfaced to the client, no failover."""

    DECLINED = "declined"
    AMBIGUOUS = "ambiguous"
    FATAL = "fatal"

    def __init__(self, msg: str, category: str = AMBIGUOUS, retryable: bool | None = None):
        super().__init__(msg)
        if retryable is not None:  # legacy call sites: retryable=False means FATAL
            category = self.AMBIGUOUS if retryable else self.FATAL
        self.category = category

    @property
    def retryable(self) -> bool:  # failover-eligible
        return self.category != self.FATAL


def _tokens(text: str) -> int:
    return max(1, len(text.split()))


def estimate_prompt_tokens(body: dict) -> int:
    """Rough pre-flight estimate: whitespace-split word count over message contents.
    Same heuristic the simulated providers use to report usage, so limits behave
    consistently in tests and demos. Real providers tokenize differently; the
    post-response settlement corrects the estimate with the reported usage."""
    return max(1, sum(len(str(m.get("content", "")).split()) for m in body.get("messages", [])))


def estimate_request_tokens(body: dict) -> int:
    """Admission-cost estimate: prompt estimate plus the caller's max_tokens cap
    (an upper bound on completion length) when the request sets one. Deliberately
    conservative - settlement credits back whatever the response does not use."""
    mt = body.get("max_tokens")
    mt = mt if isinstance(mt, int) and not isinstance(mt, bool) and mt > 0 else 0
    return estimate_prompt_tokens(body) + mt


class Provider:
    name: str
    models: set[str]
    mode = "real"  # dashboard label: a Provider talks to a live upstream by default

    async def complete(self, body: dict, headers: dict | None = None) -> dict: ...
    def stream(self, body: dict, headers: dict | None = None) -> AsyncIterator[dict]: ...


class MockProvider(Provider):
    """Deterministic-ish fake model. Knobs: latency, per-token delay, failure rate.

    Also honors the per-request visitor fault channel (llmgate/faults.py):
    when a self-serve demo key arms X-Simulate-Failure, the first attempt of
    that request faults here exactly as a real misbehaving upstream would."""

    mode = "simulated"

    def __init__(self, name, models=("mock-small",), latency=0.02, token_delay=0.002,
                 fail_rate=0.0, down=False, seed=None):
        self.name, self.models = name, set(models)
        self.latency, self.token_delay = latency, token_delay
        self.fail_rate, self.down = fail_rate, down
        self.rng = random.Random(seed)
        self.calls = 0
        self.received_headers: list[dict] = []  # end-to-end headers per attempt, for tests

    def _reply(self, body):
        last = body["messages"][-1].get("content", "")
        return f"[{self.name}] echo: {last}"

    def _maybe_fail(self):
        self.calls += 1
        # Peek, then consume only failure faults here: "slow" and "truncate"
        # are consumed by _slow()/stream() at their own injection points.
        fault = faults.peek()
        if fault == "429":
            faults.take()
            raise ProviderError(f"{self.name} injected visitor fault (429)",
                                category=ProviderError.DECLINED)
        if fault == "500":
            faults.take()
            raise ProviderError(f"{self.name} injected visitor fault (500)",
                                category=ProviderError.AMBIGUOUS)
        if self.down:
            raise ProviderError(f"{self.name} unavailable (503)")
        if self.rng.random() < self.fail_rate:
            raise ProviderError(f"{self.name} transient error (500)")

    async def complete(self, body, headers=None):
        self.received_headers.append(dict(headers or {}))
        await asyncio.sleep(self.latency)
        self._maybe_fail()
        await self._slow()
        text = self._reply(body)
        pt = sum(_tokens(m.get("content", "")) for m in body["messages"])
        ct = _tokens(text)
        return {"id": f"cmpl-{self.name}-{self.calls}", "object": "chat.completion",
                "created": int(time.time()), "model": body["model"],
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": text}}],
                "usage": {"prompt_tokens": pt, "completion_tokens": ct, "total_tokens": pt + ct}}

    async def _slow(self):
        """An armed 'slow' fault adds a capped delay after the failure checks
        pass - the request still succeeds, just late, exercising client
        timeouts rather than failover."""
        if faults.peek() == "slow":
            faults.take()
            await asyncio.sleep(faults.SLOW_EXTRA_S)

    async def stream(self, body, headers=None):
        self.received_headers.append(dict(headers or {}))
        await asyncio.sleep(self.latency)
        self._maybe_fail()
        await self._slow()
        words = self._reply(body).split(" ")
        truncate = faults.peek() == "truncate"
        if truncate:
            faults.take()
        for i, w in enumerate(words):
            if truncate and i == 1:
                raise ProviderError(f"{self.name} injected visitor fault (mid-stream abort)",
                                    category=ProviderError.AMBIGUOUS)
            await asyncio.sleep(self.token_delay)
            yield {"object": "chat.completion.chunk", "model": body["model"],
                   "choices": [{"index": 0, "delta": {"content": w + (" " if i < len(words) - 1 else "")},
                                "finish_reason": None}]}
        pt = sum(_tokens(m.get("content", "")) for m in body["messages"])
        yield {"object": "chat.completion.chunk", "model": body["model"],
               "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
               "usage": {"prompt_tokens": pt, "completion_tokens": len(words),
                         "total_tokens": pt + len(words)}}


class HTTPProvider(Provider):
    def __init__(self, name, base_url, api_key, models, timeout=60.0, client=None,
                 model_map=None):
        self.name, self.models = name, set(models)
        self.base_url = base_url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {api_key}"}
        self._owns_client = client is None
        self.client = client if client is not None else httpx.AsyncClient(timeout=timeout)
        # Optional alias -> upstream-model rewrite. Lets several providers serve
        # one public model name on different upstream models (a same-provider
        # backup tier): the gateway routes the alias, each provider sends its
        # own upstream id. Models not in the map pass through unchanged.
        self.model_map = dict(model_map or {})

    async def aclose(self):
        """Release only the HTTP client this provider created, never a borrowed one."""
        if self._owns_client and not self.client.is_closed:
            await self.client.aclose()

    def _outgoing(self, body: dict) -> dict:
        mapped = self.model_map.get(body.get("model"))
        return {**body, "model": mapped} if mapped else body

    @staticmethod
    def _check(status: int, text: str):
        if status >= 400:
            category = (ProviderError.DECLINED if status == 429
                        else ProviderError.AMBIGUOUS if status == 408 or status >= 500
                        else ProviderError.FATAL)
            raise ProviderError(f"upstream {status}: {text[:200]}", category=category)

    def _req_headers(self, headers: dict | None) -> dict:
        """Client-supplied end-to-end headers (e.g. Idempotency-Key) ride along
        with the provider's own auth; the provider's headers win on conflict."""
        return {**(headers or {}), **self.headers}

    async def complete(self, body, headers=None):
        try:
            r = await self.client.post(f"{self.base_url}/chat/completions",
                                       json={**self._outgoing(body), "stream": False},
                                       headers=self._req_headers(headers))
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as e:
            # connect-phase failure: nothing was ever sent upstream
            raise ProviderError(f"connect failed (request never sent): {e}",
                                category=ProviderError.DECLINED)
        except httpx.TimeoutException as e:
            # read/write timeout: the request may have been sent and processed
            raise ProviderError(f"network timeout: {e}")  # AMBIGUOUS
        except httpx.HTTPError as e:
            # mid-request transport failure: may have been sent
            raise ProviderError(f"transport failure: {e}")  # AMBIGUOUS
        self._check(r.status_code, r.text)
        return r.json()

    async def stream(self, body, headers=None):
        try:
            async with self.client.stream("POST", f"{self.base_url}/chat/completions",
                                          json={**self._outgoing(body), "stream": True,
                                                "stream_options": {"include_usage": True}},
                                          headers=self._req_headers(headers)) as r:
                if r.status_code >= 400:
                    self._check(r.status_code, (await r.aread()).decode(errors="replace"))
                async for line in r.aiter_lines():
                    if line.startswith("data:"):
                        data = line[5:].strip()
                        if data == "[DONE]":
                            return
                        yield json.loads(data)
        except httpx.HTTPError as e:
            raise ProviderError(f"network: {e}")
