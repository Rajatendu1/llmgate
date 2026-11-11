# Gateway permission-change preflight

## The concrete maintainer task

Before merging a gateway scope-to-model mapping or required-scope change, a maintainer wants to know: "Which of our supplied agent requests starts reaching a model, stops reaching it, or changes its refusal?" A generic Actas policy pack cannot answer that because gateway routing and scope mapping are separate constraints.

This command boots the actual gateway configuration, issues throwaway local tokens with its embedded Actas runtime, and sends actual in-process HTTP requests through signature verification, required-scope checks, known-route checks, scope-to-model mapping and MockProvider completion. It compares baseline and candidate behavior on the same explicit cases. It does not reimplement those decisions in a second policy engine.

## Run

```sh
/path/to/gateway-dev-venv/bin/python scripts/gateway_policy_change.py \
  --gateway-source /path/to/clean/llmgate \
  --baseline baseline.json --candidate candidate.json --cases cases.json \
  > permission-change.json
```

Exit 0 means no supplied-case HTTP status changed and candidate expectations match. Exit 1 means a change or mismatch requires review, even if the changed status was expected. Exit 2 is an input/source/runtime error, not acceptance.

Case schema, bounded to 32 cases:

```json
[{"name":"cheap-worker-expensive-model","scope":["llm:complete"],"model":"expensive","expect_status":403}]
```

The included fixture begins with `llm:complete` mapping only to `cheap`, then adds `expensive` to that same scope. The actual response changes from 403 with no provider execution to 200 with provider execution. The candidate's expected status is also changed to 200. The command still exits 1 and reports one newly reachable alias. That is the central workflow, not a cosmetic wrapper of the earlier runner.

## Executed checks

On clean gateway `ef181d541e1e958ead0f1ad2ea0ef7f89593e914`:

- Unchanged configuration: expected cheap request 200, out-of-scope model 403, missing required scope 403, unknown alias 404.
- Mapping expansion: one supplied request newly reachable, HTTP 403 to 200, candidate expectations all match, exit 1.
- Required-scope change: one newly reachable and one newly refused request, observed on native HTTP responses.
- Removed configured alias: 403 becomes native 404, still flagged for review.
- Real or mixed provider configuration: rejected before any native application construction. It is never silently stripped to a mock-only projection.
- Inherited fake provider/Redis credentials: ignored. The request still completes locally.
- Duplicate JSON members, invalid scope shapes, ambiguous enable flags and nondeterministic mock settings: rejected.

The original eight native-workflow regressions pass. Two additional focused regressions check missing dependencies and ordinary native runtime failures as sanitized error JSON/exit 2, not policy-change exit 1. The CLI fixture separately executes actual signed-token HTTP calls and records native module origins/hashes and the clean Git head. Reports include request case names and should remain private unless their inputs are deliberately shareable.

## Limits and next check

Only all-mock configurations with zero mock latency/failure rate are supported. The real production `deploy.config.json` contains real routes and therefore is explicitly unsupported. A maintainer can use a deliberately authored all-mock release fixture, but the report does not prove anything about declared or activated production real routes.

Tokens are newly issued per baseline/candidate run. This compares configurations for equivalent freshly issued local credentials, not migration compatibility of existing tokens or sessions. No production key is collected. The environment is cleared for native execution, socket connections are blocked, no configuration is installed, and no deployment or real-provider call is performed. MockProvider execution is explicitly part of the observed proof. These environment/socket guards are process-global and intended for trusted, single-process local use, not a concurrently hosted service. This is not a sandbox for malicious Python dependencies. Only trusted local code should run.

This does not assess request budgets, streaming/failover, cross-service issuer trust, PoP, distributed replay/revocation, all possible cases, live browser behavior or production safety. Mandatory post-production end-to-end checks remain separate. It is a concrete internal maintainer use case, not established external demand or a production authorization product.

This command complements the local native-contract preflight in `docs/LOCAL-PREFLIGHT.md`. Its supplied-case change report requires maintainer review; it never automatically approves or installs policy. Bounded gateway/Actas owner reviews checked the native boot, source boundary and claim meaning before integration. External user demand remains unvalidated.
