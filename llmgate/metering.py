from __future__ import annotations
from collections import defaultdict


class Meter:
    def __init__(self):
        self.usage = defaultdict(lambda: defaultdict(int))  # key -> counter -> n
        self.by_provider = defaultdict(lambda: defaultdict(int))
        self.latencies: list[float] = []
        self.latency_count, self.latency_sum = 0, 0.0

    def record(self, key, provider, model, usage, status="ok", latency=None, cost=None):
        u = self.usage[key]
        u["requests"] += 1
        u[f"status_{status}"] += 1
        u["prompt_tokens"] += (usage or {}).get("prompt_tokens", 0)
        u["completion_tokens"] += (usage or {}).get("completion_tokens", 0)
        if cost:
            u["cost_usd"] += cost  # float estimate from the config price table
        p = self.by_provider[provider]
        p["requests"] += 1
        p[f"status_{status}"] += 1
        if latency is not None:
            self.latencies.append(latency)
            self.latency_count += 1
            self.latency_sum += latency
            if len(self.latencies) > 50000:
                del self.latencies[:25000]

    def snapshot(self, key=None):
        if key:
            return dict(self.usage.get(key, {}))
        return {"keys": {k: dict(v) for k, v in self.usage.items()},
                "providers": {k: dict(v) for k, v in self.by_provider.items()}}
