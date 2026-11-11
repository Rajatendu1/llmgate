# llmgate design walkthrough

A short, honest guide to the decisions in this project: what was chosen, what it costs, and what
would change with real traffic. Written to be explained out loud.

## The problem
Teams call several model providers. Each has its own limits, outages and slow patches. A gateway
gives callers one stable endpoint and handles the messy parts: which provider, what to do when one
fails, who is allowed to call and how much, and who used what.

## Request path
auth (constant-time key match) -> per-key request bucket -> validate body -> per-key token
bucket (estimated prompt cost) -> route (priority tier,
weighted order inside the tier) -> call provider with a timeout -> on retryable failure try the
next candidate -> meter usage -> respond.

## Decisions and their costs
| Decision | Why | Cost / what it gives up |
|---|---|---|
| Priority tiers + weights | Primary/backup and load sharing with one simple rule | No latency- or cost-aware routing |
| Circuit breaker per provider | A dead provider otherwise charges every request its full timeout | Cooldown is fixed; one probe request can be unlucky |
| Fail over only on retryable errors | A 400 is the caller's bug; retrying hides it | A provider that wrongly returns 400 for an outage will not fail over |
| Stream failover only before first chunk | After bytes are sent, switching models would splice two answers | A mid-stream failure reaches the client as an error event |
| Time-to-first-chunk timeout plus an idle timeout between chunks | Long answers are fine; a stuck or stalled connection is not | A stalled stream still fails the request mid-answer; it cannot be repaired, only reported |
| Two buckets per key: requests and tokens | Requests alone price a huge prompt the same as a tiny one | The pre-flight cost is an estimate (word count plus the request's max_tokens cap when set), debited at admission and not refunded on upstream failure. Settlement against reported usage happens after the response: an under-count is charged, an over-count (an unused max_tokens allowance) is credited back. Enforcement is still between requests: one admitted call can overshoot by its full completion length. A stream that ends without a usage chunk (failure, disconnect, provider does not report) settles nothing: prompt-estimate-only for that call |
| Config price table per provider+model, USD per 1M tokens | Cost attribution is the first thing a team asks a gateway for | List prices go stale; real bills add discounts, cached-token and batch rates the table does not model, so `cost_usd` is an estimate, not billing-grade. No usage reported (stream lost its usage chunk) means no cost for that call - nothing is invented |
| In-memory state | Keeps the project small and testable | Multi-worker or multi-node needs a shared store (Redis); limits and breakers are per process |
| Prometheus text format written by hand, admin-gated | The exposition is small enough that a client library was not justified; per-key usage should not be public | Format upkeep is ours; latency quantiles are recomputed per scrape over a bounded window (count/sum are exact); a scrape needs an admin bearer token |
| Upstream errors hidden from clients | Provider messages can leak keys or internals | Debugging needs server logs |

## Verification and limits
- Unit and API tests with simulated providers, including the metrics exposition: admin-only access, counters by key/status, token and cost counters, provider rows, the breaker gauge, the latency summary, label escaping and full-format parse. Cost tests cover the price-table math, per-key and cross-instance aggregation, failover priced at the serving provider, streams, the metrics series, and unpriced routes recording no cost. Later suites pin the real-upstream client on a mocked transport, the public demo-stats/demo-events endpoints and their redaction contract, the visitor Try paths (simulated, real, and the backup alias), and the model-alias map with same-provider backup routing.
- Integration tests through a real local HTTP server over real sockets (7): success and SSE,
  429 -> failover, 400 -> no retry, a hung upstream timing out, a connection cut mid-stream
  (reported, not spliced), a stream that stalls after its first chunk (cut by the idle timeout), and a dead port. The upstream is a fake written for the tests; it is not a
  real provider and does not reproduce real provider quirks.
- Benchmarks use simulated latency on a shared 2-core machine; read them as a sanity check.
- Token-limit tests cover the estimate gate (including max_tokens in the estimate),
  post-response settlement (under-count charged, over-count credited, credit clamped at
  burst capacity), stream settlement and refill, plus the two failure behaviors directly:
  the admission estimate is not refunded on upstream failure, and a stream that reports no
  usage settles nothing beyond the estimate (verified by its contrast case, where reported
  usage settles the full total).
- Timeouts are size-aware: a route can grow its fixed timeout by the estimated request
  size (prompt estimate plus the max_tokens cap) at a configured seconds-per-1k-tokens rate,
  capped at a ceiling. The estimate is the same word-count heuristic admission control uses,
  so a rare long generation still hits the ceiling; sizing knobs are per route and off by
  default (a fixed timeout remains the default). Tested: growth, the cap, and the unchanged
  default.

## Next steps for real traffic
1. Tracing and alerts on breaker opens (the metrics endpoint to alert on now exists).
2. Profile under real concurrency before deciding anything about a faster language. The one profile
   done here showed the gateway's own code at about 12% of request time.

Done since this list was first written: usage costs per model (the price table above), shared
Redis state for limits and metering, and one real free-tier provider (Groq) behind the same
pipeline - a same-provider backup model via per-provider model aliases, live verification of
complete/stream/error handling against the real service, and a visitor-budgeted Try on the
public demo whose dashboard diagram shows the real route next to the simulated one. Real
latency on that path runs a few hundred milliseconds per short completion; the models used
price at $0, so a bill-versus-estimate comparison is not meaningful there - that check still
wants a paid provider.

## Questions worth being ready for
- Why not just retry the same provider? Bounded retries apply only to declined errors. Circuit breakers and failover contain repeat failures; ambiguous failures are not retried in place.
- What happens at 10x load? This has not been measured. Limits and usage metering can share Redis state; breaker and latency state remain per process.
- What remains open? Production hardening, billing-grade accounting, distributed tracing, TLS termination and independently operated provider failover.
