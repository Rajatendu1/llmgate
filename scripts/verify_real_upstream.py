#!/usr/bin/env python3
"""Live verification of HTTPProvider against a real OpenAI-compatible upstream.

Runs ONLY when all three are set in the environment (never in CI by default):
  LLMGATE_REAL_BASE_URL   e.g. https://api.groq.com/openai/v1
  LLMGATE_REAL_API_KEY    the upstream key (from the secret store, never committed)
  LLMGATE_REAL_MODEL      e.g. openai/gpt-oss-120b

Checks, in order, against the live service:
  1. non-streaming completion: response shape + reported usage
  2. streaming completion: chunks arrive, final usage chunk present
  3. an unknown-model call: categorized FATAL (4xx handling against the real API)

Exits 0 on SKIP (no credentials) or PASS; 1 on any FAILED check. The key is
read from the environment and never printed; upstream error text is capped.
"""
from __future__ import annotations
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from llmgate.providers import HTTPProvider, ProviderError  # noqa: E402


async def main() -> int:
    base, key, model = (os.getenv("LLMGATE_REAL_BASE_URL"), os.getenv("LLMGATE_REAL_API_KEY"),
                        os.getenv("LLMGATE_REAL_MODEL"))
    if not (base and key and model):
        print("SKIP: LLMGATE_REAL_BASE_URL / LLMGATE_REAL_API_KEY / LLMGATE_REAL_MODEL not all set")
        return 0

    prov = HTTPProvider("real-under-test", base, key, [model], timeout=45.0)
    failures = []

    # 1. non-streaming completion. Reasoning models (e.g. gpt-oss) spend
    # completion tokens on reasoning first, so a tiny cap can yield empty
    # content; 256 leaves room for both.
    try:
        res = await prov.complete({"model": model, "max_tokens": 256,
                                   "messages": [{"role": "user", "content": "Say hello in one short sentence."}]})
        text = res["choices"][0]["message"]["content"]
        usage = res.get("usage") or {}
        assert text.strip(), "empty completion text"
        assert usage.get("total_tokens", 0) > 0, "no usage reported"
        print(f"PASS complete: {usage.get('prompt_tokens')}+{usage.get('completion_tokens')} tokens, "
              f"text={text[:60]!r}")
    except Exception as e:
        failures.append(f"complete: {type(e).__name__}: {str(e)[:200]}")
        print(f"FAIL complete: {type(e).__name__}: {str(e)[:200]}")

    # 2. streaming completion with usage
    try:
        chunks, usage = 0, None
        async for c in prov.stream({"model": model, "max_tokens": 256,
                                    "messages": [{"role": "user", "content": "Count to three."}]}):
            chunks += 1
            usage = c.get("usage") or usage
        assert chunks >= 2, f"only {chunks} chunk(s)"
        assert usage and usage.get("total_tokens", 0) > 0, "no usage chunk"
        print(f"PASS stream: {chunks} chunks, total_tokens={usage['total_tokens']}")
    except Exception as e:
        failures.append(f"stream: {type(e).__name__}: {str(e)[:200]}")
        print(f"FAIL stream: {type(e).__name__}: {str(e)[:200]}")

    # 3. unknown model must be FATAL, not retryable
    try:
        await prov.complete({"model": "definitely-not-a-model", "max_tokens": 1,
                             "messages": [{"role": "user", "content": "hi"}]})
        failures.append("error-category: unknown model did not raise")
        print("FAIL error-category: unknown model did not raise")
    except ProviderError as e:
        if e.category == ProviderError.FATAL:
            print(f"PASS error-category: unknown model -> FATAL ({str(e)[:80]})")
        else:
            failures.append(f"error-category: unknown model -> {e.category}, want FATAL")
            print(f"FAIL error-category: unknown model -> {e.category}, want FATAL")
    except Exception as e:
        failures.append(f"error-category: {type(e).__name__}: {str(e)[:200]}")
        print(f"FAIL error-category: {type(e).__name__}: {str(e)[:200]}")

    print("RESULT:", "FAIL" if failures else "PASS")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
