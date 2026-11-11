# Local cross-project preflight

This local command checks two specific release questions together: does the gateway execute its reviewed Lua limiter contract, and do supplied Actas policy decisions change from a kept baseline? It is not a full release-acceptance gate or a replacement for end-to-end live testing.

Use a clean checkout of this repository and a clean checkout of the standalone Actas repository. Install each project and its test dependencies in the Python environment you select. Use Python 3.10 or newer, Git and the existing dev dependency `lupa==2.8`. The command rejects dirty source trees and unsupported native test-contract revisions.

```sh
python scripts/preflight_acceptance.py \
  --gateway /path/to/llmgate \
  --actas /path/to/actas \
  --python /path/to/venv/bin/python \
  --candidate candidate.json --baseline baseline.json > acceptance.json
```

The two JSON files are non-sensitive policy packs exported from the Actas workbench. Both policies are compared against the candidate's cases. A changed expectation cannot conceal a newly allowed decision.

Exit codes:
- 0: both selected native gates pass, with no changed supplied-case decision.
- 1: a native assertion fails or a supplied policy decision changes.
- 2: invalid input, unsupported contract, dependency/import error, timeout, dirty/changing source, or missing/inconsistent native evidence.

The report records both Git heads, source cleanliness, input hashes, Python version/binary hash, verified relative native import origins, actual child exits, gateway assertion counts and policy transition counts. Raw stdout/stderr, local paths, case names and scopes are not included. Hashes are not anonymization: small or predictable inputs can be guessed. Keep reports private unless their inputs are explicitly safe to share.

## Execution boundaries

The gateway entry point is its existing `tests/test_redislimit.py`, pinned to the exact reviewed SHA256 in the runner. Those tests execute production Lua under Lua 5.1 through a small Redis-command adapter. Historical Python-emulated tests are unsupported, even if they return green. An edited native contract requires review and a new explicit pin, not a minimum test-count workaround.

The Actas entry point is the supplied checkout's `source/actas/workbench.py`, with native comparison output checked against the exit code and consistent nonzero counts. The runner verifies that native imports resolve to the supplied source trees, disables automatic pytest plugin discovery and explicitly loads the asyncio plugin. Child processes do not inherit provider/Redis credentials or caller Python import overrides. This is not a sandbox: supplied code, the selected interpreter, installed dependencies and explicit plugins still execute with local process permissions. Run only source you trust.

This does not contact deployed services, activate providers or deploy changes. It does not prove real Redis networking/concurrency, gateway model admissibility, token signatures, remote issuer trust, proof of possession, cross-server replay/revocation, exhaustive policy equivalence, real-provider availability, browser behavior or full native suite coverage. Gateway embeds its own pinned Actas runtime; running the standalone comparison beside it does not replace that runtime or create cross-service authorization.

After every production deployment, independently verify source/alias identity, full affected live workflows, changed behavior and failure cases, and desktop/mobile pixels. Report any no-spend or free-tier exclusions. Do not treat this local preflight as that live evidence.

## Regression evidence

The first working slice was checked against gateway a180772a405210a9e1344fd32e62b500a5205011 and Actas 9cf553dbb31a3fc716d97bfc41f7f263c21bc08b on 11 November 2026. Identical packs returned 0 with 13 Lua assertions and two unchanged cases; an added write permission returned 1 despite all new expectations matching; duplicate JSON members returned 2. A local missing-parenthesis fixture returned 1 with five native failures. The actual historical gateway 0334f464bbcc666263c12a5be2e167532d34faef returned 2 because its old emulated test contract is unsupported. These are bounded development checks, not external user validation or a security audit.
