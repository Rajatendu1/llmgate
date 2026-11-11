"""Scope parsing and set algebra.

Scopes are space-delimited strings per RFC 6749 section 3.3. The core invariant
of this toolkit: a delegated token's scope must always be a (non-strict is fine)
subset of the scope its issuer could confer. Widening is never allowed.
"""


def parse(scope: str | None) -> frozenset:
    """Parse a scope string into a set, dropping empties and duplicates."""
    if not scope:
        return frozenset()
    return frozenset(part for part in scope.split() if part)


def render(scope_set) -> str:
    """Render a scope set canonically (sorted, space-delimited)."""
    return " ".join(sorted(scope_set))


def is_subset(requested, available) -> bool:
    return set(requested) <= set(available)


def missing(requested, available) -> frozenset:
    return frozenset(set(requested) - set(available))
