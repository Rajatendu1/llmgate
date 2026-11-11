"""Prometheus text exposition (v0.0.4) for the in-memory meter.

No client library: the exposition is a few counters, one gauge and one summary,
so a dependency was not justified. The format is simple but strict - label values
are escaped (backslash, double quote, newline) and every series is preceded by
HELP/TYPE lines.

What is exposed:
- llmgate_requests_total{key,status}        counter, per API key name and outcome
- llmgate_tokens_total{key,kind}            counter, prompt/completion tokens per key
- llmgate_provider_requests_total{provider,status}  counter, per upstream provider
- llmgate_provider_breaker_state{provider}  gauge, 0=closed 1=half_open 2=open
- llmgate_request_latency_seconds           summary, end-to-end gateway latency

Honest edges:
- count/sum of the latency summary are exact all-time accumulators; the quantiles
  are computed at scrape time over a bounded in-memory window (the meter retains
  the most recent ~50k samples, halved when full). On a busy long-lived process
  the quantiles describe recent traffic, not the whole process lifetime.
- Requests rejected or failed before/without routing (rate limited, upstream
  failed) are metered against provider "-"; they appear in the per-key series
  only and are omitted from the per-provider series, so provider rows always
  name a real upstream.
- Everything is per process, like the rest of the state.
"""
from __future__ import annotations
import math

CONTENT_TYPE = "text/plain; version=0.0.4"

_BREAKER_NUM = {"closed": 0, "half_open": 1, "open": 2}


def _esc(s: str) -> str:
    return s.replace("\\", r"\\").replace('"', r"\"").replace("\n", r"\n")


def _quantile(sorted_vals: list[float], q: float) -> float:
    if not sorted_vals:
        return 0.0
    return sorted_vals[max(0, math.ceil(q * len(sorted_vals)) - 1)]


def render(meter, router) -> str:
    out = []

    out += ['# HELP llmgate_requests_total Requests handled by the gateway, by API key name and outcome status.',
            '# TYPE llmgate_requests_total counter']
    for key in sorted(meter.usage):
        for counter, n in sorted(meter.usage[key].items()):
            if counter.startswith("status_"):
                out.append(f'llmgate_requests_total{{key="{_esc(key)}",status="{_esc(counter[7:])}"}} {n}')

    out += ['# HELP llmgate_tokens_total Upstream-reported tokens, by API key name and kind.',
            '# TYPE llmgate_tokens_total counter']
    for key in sorted(meter.usage):
        u = meter.usage[key]
        for kind in ("prompt", "completion"):
            out.append(f'llmgate_tokens_total{{key="{_esc(key)}",kind="{kind}"}} {u.get(kind + "_tokens", 0)}')

    out += ['# HELP llmgate_cost_usd_total Estimated spend: upstream-reported tokens priced at the '
            'config table (list prices; not billing-grade), by API key name.',
            '# TYPE llmgate_cost_usd_total counter']
    for key in sorted(meter.usage):
        cost = meter.usage[key].get("cost_usd", 0)
        if cost:
            out.append(f'llmgate_cost_usd_total{{key="{_esc(key)}"}} {cost:.10g}')

    out += ['# HELP llmgate_provider_requests_total Requests sent to each upstream provider, by outcome status.',
            '# TYPE llmgate_provider_requests_total counter']
    for prov in sorted(meter.by_provider):
        if prov == "-":  # rejected/failed before routing: no real upstream to attribute
            continue
        for counter, n in sorted(meter.by_provider[prov].items()):
            if counter.startswith("status_"):
                out.append(f'llmgate_provider_requests_total{{provider="{_esc(prov)}",status="{_esc(counter[7:])}"}} {n}')

    out += ['# HELP llmgate_provider_breaker_state Circuit breaker state per provider (0=closed, 1=half_open, 2=open).',
            '# TYPE llmgate_provider_breaker_state gauge']
    for r in sorted(router.routes, key=lambda r: r.provider.name):
        out.append(f'llmgate_provider_breaker_state{{provider="{_esc(r.provider.name)}"}} {_BREAKER_NUM[r.breaker.state]}')

    lat = sorted(meter.latencies)
    out += ['# HELP llmgate_request_latency_seconds End-to-end gateway latency. count and sum are exact; '
            'quantiles are computed at scrape time over the most recent retained samples (bounded in-memory window).',
            '# TYPE llmgate_request_latency_seconds summary']
    for q in (0.5, 0.95, 0.99):
        out.append(f'llmgate_request_latency_seconds{{quantile="{q}"}} {_quantile(lat, q):.6f}')
    out.append(f'llmgate_request_latency_seconds_count {meter.latency_count}')
    out.append(f'llmgate_request_latency_seconds_sum {meter.latency_sum:.6f}')

    return "\n".join(out) + "\n"
