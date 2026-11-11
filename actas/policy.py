"""Delegation policy: who may act, where, and with how much scope.

Policy is evaluated on every exchange, independently of what the subject
token carries. A token proves what the previous hop had; policy bounds
what the next hop may receive. Both must agree.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class AgentPolicy:
    client_id: str
    allowed_scopes: frozenset
    allowed_audiences: frozenset
    max_delegation_depth: int = 3
    max_token_ttl: int = 900


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    reason: str
    granted_scope: frozenset = field(default_factory=frozenset)


class PolicyEngine:
    def __init__(self, policies: dict[str, AgentPolicy] | None = None):
        self._policies: dict[str, AgentPolicy] = dict(policies or {})

    def register(self, policy: AgentPolicy) -> None:
        self._policies[policy.client_id] = policy

    def get(self, client_id: str) -> AgentPolicy | None:
        return self._policies.get(client_id)

    def decide(
        self,
        *,
        client_id: str,
        requested_scope: frozenset,
        audience: str,
        subject_scope: frozenset,
        delegation_depth: int,
        requested_ttl: int,
    ) -> PolicyDecision:
        policy = self._policies.get(client_id)
        if policy is None:
            return PolicyDecision(False, f"unknown client: {client_id}")
        if delegation_depth > policy.max_delegation_depth:
            return PolicyDecision(
                False,
                f"delegation depth {delegation_depth} exceeds limit {policy.max_delegation_depth}",
            )
        if audience not in policy.allowed_audiences:
            return PolicyDecision(False, f"audience '{audience}' not allowed for {client_id}")
        from .scopes import is_subset, missing

        if not is_subset(requested_scope, subject_scope):
            over = missing(requested_scope, subject_scope)
            return PolicyDecision(
                False,
                "scope widening requested: "
                + ", ".join(sorted(over))
                + " not present in subject token",
            )
        if not is_subset(requested_scope, policy.allowed_scopes):
            over = missing(requested_scope, policy.allowed_scopes)
            return PolicyDecision(
                False,
                "scope " + ", ".join(sorted(over)) + f" outside policy for {client_id}",
            )
        if requested_ttl > policy.max_token_ttl:
            return PolicyDecision(
                False, f"requested ttl {requested_ttl}s exceeds limit {policy.max_token_ttl}s"
            )
        return PolicyDecision(True, "allowed", granted_scope=frozenset(requested_scope))
