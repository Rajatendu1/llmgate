# Embedded Actas component

Pinned runtime: Actas 0.14.1, replacing the older 0.8.1 snapshot.

The 22 Python files in `actas/` are unmodified copies of the current runtime
recovered from the owner's Vercel deployment. They were built under the same
project owner's direction. The standalone project is
https://github.com/Rajatendu1/actas at commit
`dfe590cb3ab07178f5ada1735db89adde1bf7019` (`source/actas/`). All 22 vendored
modules also match that public commit byte for byte. No third-party license is
asserted here.

Recovery source: the retained inputs of deployment
`dpl_9R2z9U2J26XQsUCWZL4MLyW6XFC9`. The recovered 31-file archive has SHA-256
`5c013a488ad7b11e771f7604e70f8a4c43afa432a720d7e5b43ee17dbd3dd7bd`.
`actas-0.14.1-files.sha256.json` records each vendored Python file's SHA-256;
a gateway test checks those bytes and `actas.__version__`.

This is the recovered runtime, not recovery of Actas's original development
archive. Its original tests and dependency hash lock were not retained in that
deployment. The gateway regression suite is separate and does not replace or
restore that lost suite. The previous snapshot archive hash was
`0eba3ba683998c038313afee202201ca0741ce6e246a3c2179d0e68308fd2559`.

## What llmgate imports

- `llmgate/delegation.py` imports the token exchange service, policy engine,
  signing keys, token registry and audit log. It issues a root token, delegates
  to a parent and then a narrower sub-agent in the self-contained demo.
- `llmgate/tokenauth.py` imports Actas token verification and reads the verified
  delegation chain. Gateway configuration maps token scopes to model aliases
  and sets rate/token budgets. Claims do not set their own limits.

This is an in-process Python dependency, not an HTTP call to the Actas website.
The standalone Actas FastAPI app is not imported or mounted. Its website,
workbench, snapshot CLI, DPoP resource server and administration routes are not
exposed by llmgate. The gateway's optional token path still rejects every
sender-bound token: this integration has no matching proof channel.

Why use it: a sub-agent can receive only the simulated model permissions it
needs instead of sharing a broad static gateway key. The demo proves a narrower
model envelope, a refused scope-widening exchange and a verifiable audit chain.
Static API keys remain an independent authentication path. This is one embedded
trust domain, with per-process signing keys and state, not a remote production
identity service. Revocation at an Actas resource server is not gateway request
revocation; llmgate does not consult Actas's registry on every completion.

Any future update must start from a versioned source, preserve this provenance,
check the pinned bytes and run gateway authentication, delegation and full
regression checks before a separately approved deployment.
