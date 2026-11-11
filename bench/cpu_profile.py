"""Where does gateway CPU go? Runs 3000 in-process requests against a zero-latency mock
and prints the top cumulative-time functions. Used to decide whether a Go hot path is justified."""
import asyncio, cProfile, pstats, random, httpx
from llmgate.app import ApiKey, create_app
from llmgate.providers import MockProvider
from llmgate.router import Route, Router

async def main(n=3000):
    app = create_app(Router([Route(MockProvider("a", ["m"], latency=0, token_delay=0))], random.Random(1)),
                     {"k": ApiKey("t", 1e9, 1e9)})
    body = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        for _ in range(n):
            await c.post("/v1/chat/completions", json=body, headers={"Authorization": "Bearer k"})

pr = cProfile.Profile(); pr.enable(); asyncio.run(main()); pr.disable()
s = pstats.Stats(pr); s.sort_stats("cumulative").print_stats(12); print("--- gateway code only (llmgate/*):"); s.sort_stats("cumulative").print_stats("llmgate/", 8)
