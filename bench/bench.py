"""Load test against a running gateway (simulated providers).
usage: python bench/bench.py --url http://127.0.0.1:8000 --n 4000 --c 64 [--stream]
Reports throughput and latency percentiles. Gateway overhead is measured against the
simulated upstream latency, so read p50 minus ~30ms as the overhead estimate."""
import argparse, asyncio, json, statistics, time
import httpx


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8000")
    ap.add_argument("--n", type=int, default=4000)
    ap.add_argument("--c", type=int, default=64)
    ap.add_argument("--stream", action="store_true")
    a = ap.parse_args()
    body = {"model": "gpt-sim", "stream": a.stream, "messages": [{"role": "user", "content": "benchmark prompt"}]}
    lat, codes, prov = [], {}, {}
    sem = asyncio.Semaphore(a.c)
    limits = httpx.Limits(max_connections=a.c)
    async with httpx.AsyncClient(base_url=a.url, limits=limits, timeout=30,
                                 headers={"Authorization": "Bearer demo-key"}) as cl:
        async def one():
            async with sem:
                t = time.perf_counter()
                if a.stream:
                    async with cl.stream("POST", "/v1/chat/completions", json=body) as r:
                        async for _ in r.aiter_lines(): pass
                else:
                    r = await cl.post("/v1/chat/completions", json=body)
                lat.append((time.perf_counter() - t) * 1000)
                codes[r.status_code] = codes.get(r.status_code, 0) + 1
                p = r.headers.get("x-gateway-provider", "-"); prov[p] = prov.get(p, 0) + 1
        t0 = time.perf_counter()
        await asyncio.gather(*[one() for _ in range(a.n)])
        dur = time.perf_counter() - t0
    lat.sort(); q = lambda p: lat[min(len(lat) - 1, int(len(lat) * p))]
    print(json.dumps({"mode": "stream" if a.stream else "non-stream", "requests": a.n, "concurrency": a.c,
                      "rps": round(a.n / dur, 1), "p50_ms": round(q(.5), 1), "p95_ms": round(q(.95), 1),
                      "p99_ms": round(q(.99), 1), "status": codes, "served_by": prov}))

asyncio.run(main())
