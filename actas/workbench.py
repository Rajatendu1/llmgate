"""Bounded, stateless policy regression runner. No tokens or issuer trust implied."""
from __future__ import annotations

import re
from .policy import AgentPolicy, PolicyEngine
from . import errors

SCHEMA_VERSION = 1
IDENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._~:/@+-]{0,127}\Z")


def bad(message):
    raise errors.invalid_request(message)


def load_pack_json(raw):
    """Reject ambiguous object members before schema checks or normalization."""
    import json

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                bad('Duplicate JSON object field. Use each field only once.')
            result[key] = value
        return result

    return json.loads(raw, object_pairs_hook=unique_object)


def obj(value, keys, path):
    if not isinstance(value, dict) or set(value) != set(keys):
        bad(f"{path}: expected exactly {', '.join(keys)}")
    return value


def identifier(value, path):
    if not isinstance(value, str) or not IDENT.fullmatch(value):
        bad(f"{path}: use 1-128 identifier characters (letters, digits, . _ ~ : / @ + -)")
    return value


def integer(value, low, high, path):
    if type(value) is not int or not low <= value <= high:
        bad(f"{path}: expected integer {low}-{high}")
    return value


def identifiers(value, path, minimum=0):
    if not isinstance(value, list) or not minimum <= len(value) <= 32:
        bad(f"{path}: expected {minimum}-32 identifiers")
    for entry in value:
        identifier(entry, path)
    if len(set(value)) != len(value):
        bad(f"{path}: duplicate identifiers")
    return frozenset(value)


def run_pack(payload):
    obj(payload, ('schema_version', 'policies', 'cases'), 'pack')
    if type(payload['schema_version']) is not int or payload['schema_version'] != SCHEMA_VERSION:
        bad('schema_version must be 1')
    policies, cases = payload['policies'], payload['cases']
    if not isinstance(policies, list) or not 1 <= len(policies) <= 8:
        bad('policies must contain 1-8 entries')
    if not isinstance(cases, list) or not 1 <= len(cases) <= 64:
        bad('cases must contain 1-64 entries')
    engine = PolicyEngine()
    for i, p in enumerate(policies):
        path = f'policies[{i}]'
        obj(p, ('client_id', 'allowed_scopes', 'allowed_audiences', 'max_delegation_depth', 'max_token_ttl'), path)
        cid = identifier(p['client_id'], path + '.client_id')
        if engine.get(cid):
            bad(f'{path}: duplicate client_id')
        engine.register(AgentPolicy(
            client_id=cid,
            allowed_scopes=identifiers(p['allowed_scopes'], path + '.allowed_scopes'),
            allowed_audiences=identifiers(p['allowed_audiences'], path + '.allowed_audiences', 1),
            max_delegation_depth=integer(p['max_delegation_depth'], 1, 16, path + '.max_delegation_depth'),
            max_token_ttl=integer(p['max_token_ttl'], 1, 86400, path + '.max_token_ttl'),
        ))
    results, names = [], set()
    for i, c in enumerate(cases):
        path = f'cases[{i}]'
        obj(c, ('name', 'client_id', 'requested_scope', 'subject_scope', 'audience', 'delegation_depth', 'requested_ttl', 'expect_allowed'), path)
        name = identifier(c['name'], path + '.name')
        if name in names:
            bad(f'{path}: duplicate case name')
        names.add(name)
        if type(c['expect_allowed']) is not bool:
            bad(f'{path}.expect_allowed must be true or false')
        decision = engine.decide(
            client_id=identifier(c['client_id'], path + '.client_id'),
            requested_scope=identifiers(c['requested_scope'], path + '.requested_scope'),
            subject_scope=identifiers(c['subject_scope'], path + '.subject_scope'),
            audience=identifier(c['audience'], path + '.audience'),
            delegation_depth=integer(c['delegation_depth'], 1, 17, path + '.delegation_depth'),
            requested_ttl=integer(c['requested_ttl'], 1, 86401, path + '.requested_ttl'),
        )
        results.append(dict(name=name, allowed=decision.allowed,
                            expected=c['expect_allowed'], matched=decision.allowed == c['expect_allowed'],
                            reason=decision.reason, granted_scope=sorted(decision.granted_scope)))
    covered = {c['client_id'] for c in cases}
    warnings = [f'No cases cover client {p["client_id"]}' for p in policies if p['client_id'] not in covered]
    if not any(c['expect_allowed'] for c in cases):
        warnings.append('No expected-allow case: a deny-all policy could pass this suite.')
    if not any(not c['expect_allowed'] for c in cases):
        warnings.append('No expected-deny case: this suite does not exercise refusals.')
    return dict(schema_version=1, engine_version='0.14.1', total=len(results),
                matched=sum(r['matched'] for r in results), results=results, warnings=warnings,
                boundary='Policy evaluation only. Token signatures, issuer trust, expiry, revocation and proof of possession are not checked here.')


def compare_pack(payload):
    """Compare policy revisions against identical candidate cases, not two suites."""
    obj(payload, ('baseline_policies', 'candidate'), 'comparison')
    candidate = payload['candidate']
    after = run_pack(candidate)
    before = run_pack(dict(schema_version=1, policies=payload['baseline_policies'],
                           cases=candidate['cases']))
    changes = []
    for old, new in zip(before['results'], after['results']):
        transition = ('newly_allowed' if new['allowed'] else 'newly_denied') if old['allowed'] != new['allowed'] else 'unchanged'
        changes.append(dict(name=new['name'], transition=transition,
                            before_allowed=old['allowed'], after_allowed=new['allowed'],
                            before_reason=old['reason'], after_reason=new['reason'],
                            expected=new['expected'], candidate_matched=new['matched']))
    return dict(schema_version=1, engine_version='0.14.1', total=len(changes),
                newly_allowed=sum(c['transition'] == 'newly_allowed' for c in changes),
                newly_denied=sum(c['transition'] == 'newly_denied' for c in changes),
                unchanged=sum(c['transition'] == 'unchanged' for c in changes),
                baseline=before, candidate=after, changes=changes,
                boundary='Only the candidate cases were compared under both policies. No exhaustive policy equivalence or safety claim. No token validation.')


def main():
    """Offline runner for the same JSON packs the web application exports."""
    import json
    import sys
    from pathlib import Path
    if len(sys.argv) not in (2, 3):
        print('usage: python -m actas.workbench candidate-pack.json [baseline-pack.json]', file=sys.stderr)
        raise SystemExit(2)
    try:
        raw = Path(sys.argv[1]).read_bytes()
        if len(raw) > 65536:
            bad('policy pack exceeds 64 KiB')
        candidate = load_pack_json(raw)
        if len(sys.argv) == 3:
            baseline_raw = Path(sys.argv[2]).read_bytes()
            if len(baseline_raw) + len(raw) > 65536:
                bad('comparison exceeds 64 KiB')
            baseline = load_pack_json(baseline_raw)
            # Validate the imported baseline pack too, before using its policies.
            run_pack(baseline)
            report = compare_pack(dict(baseline_policies=baseline['policies'], candidate=candidate))
        else:
            report = run_pack(candidate)
    except (OSError, ValueError, RecursionError, errors.TokenExchangeError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2)
    print(json.dumps(report, indent=2))
    if len(sys.argv) == 3:
        # A changed decision needs review even when the new expectations match.
        ok = report['newly_allowed'] == report['newly_denied'] == 0 and report['candidate']['matched'] == report['total']
    else:
        ok = report['matched'] == report['total']
    raise SystemExit(0 if ok else 1)


if __name__ == '__main__':
    main()
