"""Fictional demo world.

A completely fictional scenario used by the demo and the e2e tests.
Any resemblance to real products or companies is unintended: the point is
the delegation mechanics, not the branding.

Dramatis personae:
  - Asha Raman (user id `u-asha`), a product manager.
  - PlannerBot (`planner-bot`), a calendar assistant agent.
  - Summarizer (`summarizer-tool`), a sub-agent PlannerBot delegates to.
  - Two resource APIs: `calendar-api` and `notes-api`.
"""

from __future__ import annotations

from .audit import AuditLog
from .exchange import TokenExchangeService
from .keys import KeyStore
from .registry import TokenRegistry
from .policy import AgentPolicy, PolicyEngine

ISSUER = "https://auth.demo.example"

USER_ASHA = "u-asha"
PLANNER_BOT = "planner-bot"
SUMMARIZER = "summarizer-tool"
CALENDAR_API = "calendar-api"
NOTES_API = "notes-api"

USER_FULL_SCOPE = frozenset({"read:calendar", "write:notes", "read:profile"})

POLICIES = [
    AgentPolicy(
        client_id=PLANNER_BOT,
        allowed_scopes=frozenset({"read:calendar", "write:notes"}),
        allowed_audiences=frozenset({CALENDAR_API, NOTES_API}),
        max_delegation_depth=3,
        max_token_ttl=900,
    ),
    AgentPolicy(
        client_id=SUMMARIZER,
        allowed_scopes=frozenset({"read:calendar"}),
        allowed_audiences=frozenset({CALENDAR_API}),
        max_delegation_depth=3,
        max_token_ttl=300,
    ),
]

CALENDAR_EVENTS = [
    {"id": "evt-1", "title": "Design review", "when": "2026-11-09T10:00:00+05:30"},
    {"id": "evt-2", "title": "1:1 with manager", "when": "2026-11-09T16:30:00+05:30"},
    {"id": "evt-3", "title": "Dentist", "when": "2026-11-10T08:45:00+05:30"},
]


def build_service(persist=None) -> tuple[TokenExchangeService, KeyStore, AuditLog]:
    keystore = KeyStore(persist=persist)
    audit = AuditLog(persist=persist)
    engine = PolicyEngine({p.client_id: p for p in POLICIES})
    service = TokenExchangeService(
        keystore=keystore, policy=engine, audit=audit, issuer=ISSUER,
        registry=TokenRegistry(persist.conn if persist else None),
    )
    return service, keystore, audit
