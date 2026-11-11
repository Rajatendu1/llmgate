"""Policy lab: run a visitor-designed delegation chain on an isolated service.

Every call builds a fresh in-memory service (own keys, own audit log), so a
visitor cannot touch shared state or any other visitor's tokens. Inputs are
validated against fixed fictional vocabularies and hard size caps; nothing
here accepts free text that reaches a verifier or a log unchecked.
"""

from __future__ import annotations

import json

from . import demo_fixture, dpop, errors, scopes, tokens
from .exchange import GRANT_TYPE, TOKEN_TYPE_JWT

ACTORS = (demo_fixture.PLANNER_BOT, demo_fixture.SUMMARIZER)
SCOPES = ("read:calendar", "write:notes", "read:profile", "admin:all")
USER_SCOPES = tuple(s for s in SCOPES if s != "admin:all")
AUDIENCES = (demo_fixture.CALENDAR_API, demo_fixture.NOTES_API, "billing-api")
MAX_HOPS = 3
ACTIONS = (
    ("read calendar events", "read:calendar", demo_fixture.CALENDAR_API),
    ("write a note", "write:notes", demo_fixture.NOTES_API),
)


def _bad(msg: str):
    raise errors.invalid_request(msg)


def _check_payload(p: dict) -> tuple[list[str], list[dict], bool, int]:
    if not isinstance(p, dict):
        _bad("body must be an object")
    user = p.get("user_scopes", [])
    if not isinstance(user, list) or any(s not in USER_SCOPES for s in user):
        _bad("user_scopes must be a list drawn from the fictional scope set")
    hops = p.get("hops")
    if not isinstance(hops, list) or not 1 <= len(hops) <= MAX_HOPS:
        _bad(f"hops must be a list of 1 to {MAX_HOPS}")
    clean = []
    for h in hops:
        if not isinstance(h, dict):
            _bad("each hop must be an object")
        actor, aud = h.get("actor"), h.get("audience")
        sc, ttl = h.get("scope", []), h.get("ttl", 300)
        if actor not in ACTORS:
            _bad("actor must be planner-bot or summarizer-tool")
        if aud not in AUDIENCES:
            _bad("unknown audience")
        if not isinstance(sc, list) or not sc or any(s not in SCOPES for s in sc):
            _bad("scope must be a non-empty list drawn from the scope set")
        if not isinstance(ttl, int) or isinstance(ttl, bool) or not 1 <= ttl <= 3600:
            _bad("ttl must be an integer between 1 and 3600")
        clean.append({"actor": actor, "audience": aud, "scope": sorted(set(sc)), "ttl": ttl})
    rv = p.get("revoke_hop", 0)
    if not isinstance(rv, int) or isinstance(rv, bool) or not 0 <= rv <= len(clean):
        _bad("revoke_hop must be 0 (none) or a hop number in the chain")
    return sorted(set(user)), clean, bool(p.get("bind_final", False)), rv


def run_lab(payload: dict) -> dict:
    user_scopes, hops, bind, revoke_hop = _check_payload(payload)
    service, keystore, audit = demo_fixture.build_service()
    subject, _ = tokens.issue_token(
        keystore, issuer=demo_fixture.ISSUER, subject=demo_fixture.USER_ASHA,
        scope=scopes.render(user_scopes), audience=[demo_fixture.ISSUER],
        expires_in=3600,
    )
    steps = [{"hop": 0, "label": "Asha's grant", "ok": True, "scope": user_scopes,
              "actor": None, "audience": demo_fixture.ISSUER}]
    token = subject
    final = None
    jtis = {}
    holder = holder_priv = None
    for i, h in enumerate(hops, start=1):
        extra = {}
        if bind and i == len(hops):
            holder_priv, holder = dpop.generate_key()
            extra["cnf_jwk"] = json.dumps(holder)
        try:
            r = service.exchange(
                grant_type=GRANT_TYPE, subject_token=token,
                subject_token_type=TOKEN_TYPE_JWT, client_id=h["actor"],
                scope=" ".join(h["scope"]), audience=h["audience"],
                requested_ttl=h["ttl"], **extra,
            )
        except errors.TokenExchangeError as exc:
            steps.append({"hop": i, "label": h["actor"], "ok": False, "actor": h["actor"],
                          "requested": h["scope"], "audience": h["audience"],
                          "error": exc.error, "reason": exc.description})
            break
        token, final = r.access_token, r
        jtis[i] = r.claims["jti"]
        steps.append({"hop": i, "label": h["actor"], "ok": True, "actor": h["actor"],
                      "requested": h["scope"], "audience": h["audience"],
                      "scope": sorted(scopes.parse(r.claims["scope"])),
                      "expires_in": r.expires_in,
                      "chain": tokens.act_chain(r.claims.get("act")),
                      "bound": "cnf" in r.claims})

    revoked_final = False
    if revoke_hop and revoke_hop in jtis:
        service.registry.revoke(jtis[revoke_hop], "lab")
        audit.append("token.revoked", {"jti": jtis[revoke_hop], "hop": revoke_hop})
        for st in steps:
            if st["ok"] and st["hop"] in jtis:
                st["revoked"] = service.registry.is_revoked(jtis[st["hop"]])
        revoked_final = service.registry.is_revoked(final.claims["jti"]) if final else False
        # a revoked token also cannot be exchanged any further
        if final:
            try:
                service.exchange(
                    grant_type=GRANT_TYPE, subject_token=final.access_token,
                    subject_token_type=TOKEN_TYPE_JWT, client_id=hops[-1]["actor"],
                    scope=" ".join(hops[-1]["scope"]), audience=hops[-1]["audience"],
                    requested_ttl=hops[-1]["ttl"])
                reuse = "accepted"
            except errors.TokenExchangeError:
                reuse = "refused"
        else:
            reuse = None
    else:
        reuse = None
    outcome = {"actions": [], "pop": None, "revoked": revoked_final, "reexchange": reuse}
    reached = len(steps) - 1 == len(hops) and all(s["ok"] for s in steps)
    if reached:
        claims = final.claims
        have = scopes.parse(claims["scope"])
        auds = claims["aud"] if isinstance(claims["aud"], list) else [claims["aud"]]
        for name, need, aud in ACTIONS:
            allowed = need in have and aud in auds and not revoked_final
            why = "" if allowed else ("token revoked" if revoked_final else
                f"token audience is {', '.join(auds)}" if aud not in auds
                else f"missing scope {need}")
            outcome["actions"].append({"action": name, "allowed": allowed, "why": why})
        if "cnf" in claims:
            uri = "https://calendar.demo.example/events"
            thief_priv, _ = dpop.generate_key()

            def attempt(key):
                try:
                    dpop.verify_proof(
                        dpop.make_proof(key, method="GET", uri=uri, access_token=token),
                        expected_jkt=claims["cnf"]["jkt"], method="GET", uri=uri,
                        access_token=token)
                    return True
                except errors.TokenExchangeError:
                    return False

            cache = dpop.ReplayCache(max_entries=16)
            reused = dpop.make_proof(holder_priv, method="GET", uri=uri, access_token=token)

            def present(proof):
                try:
                    dpop.verify_proof(proof, expected_jkt=claims["cnf"]["jkt"], method="GET",
                                      uri=uri, access_token=token, replay=cache)
                    return True
                except errors.TokenExchangeError:
                    return False

            outcome["pop"] = {"holder_accepted": attempt(holder_priv),
                              "thief_accepted": attempt(thief_priv),
                              "first_use_accepted": present(reused),
                              "replay_accepted": present(reused)}
    ok, bad = audit.verify()
    return {"steps": steps, "outcome": outcome, "reached_end": reached,
            "audit": {"ok": ok, "first_bad_seq": bad, "entries": audit.tail(20)}}
