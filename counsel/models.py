from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


class EngagementStatus(StrEnum):
    ACTIVE = "active"          # a step is due / running
    WAITING = "waiting"        # asleep until a wakeup or an inbound reply
    BLOCKED = "blocked"        # needs a human before it can continue
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class Trigger(StrEnum):
    CREATED = "created"
    SCHEDULED = "scheduled"
    INBOUND = "inbound"              # reply / call transcript from the counterparty
    CHANNEL_EVENT = "channel_event"  # delivery failure, no-answer, etc.
    HUMAN_DECISION = "human_decision"
    FIRM_REQUEST = "firm_request"    # someone at the firm nudged or instructed the agent


class InterventionKind(StrEnum):
    QUESTION = "question"      # agent asked the firm something
    APPROVAL = "approval"      # policy requires sign-off before an action executes
    ESCALATION = "escalation"  # runtime guardrail tripped (unresponsive, error, overdue)


class Significance(StrEnum):
    ROUTINE = "routine"
    MEANINGFUL = "meaningful"
    URGENT = "urgent"


@dataclass
class Matter:
    id: str
    client_name: str
    case_type: str
    incident_date: str
    owner: str                      # paralegal / case manager at the firm
    notes: str = ""


@dataclass
class Contact:
    id: str
    kind: str                       # "client" | "provider" | "insurer" ...
    name: str
    matter_id: str
    phone: str = ""
    email: str = ""
    preferred_channel: str = "email"
    notes: str = ""


@dataclass
class Engagement:
    id: str
    playbook: str
    matter_id: str
    contact_id: str
    goal: str
    owner: str
    status: EngagementStatus = EngagementStatus.ACTIVE
    params: dict[str, Any] = field(default_factory=dict)   # playbook-specific inputs
    state: dict[str, Any] = field(default_factory=dict)    # agent-maintained structured status
    unanswered_attempts: int = 0
    next_wake_at: datetime | None = None
    outcome: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    completed_at: datetime | None = None


@dataclass
class Event:
    id: str
    engagement_id: str
    ts: datetime
    type: str
    actor: str                      # agent | human | system | counterparty
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class Wakeup:
    id: str
    engagement_id: str
    due_at: datetime
    trigger: Trigger
    payload: dict[str, Any] = field(default_factory=dict)
    consumed_at: datetime | None = None


@dataclass
class Intervention:
    id: str
    engagement_id: str
    kind: InterventionKind
    question: str
    options: list[str] = field(default_factory=list)
    payload: dict[str, Any] = field(default_factory=dict)   # e.g. the gated action
    urgency: str = "normal"
    status: str = "open"
    created_at: datetime | None = None
    resolved_at: datetime | None = None
    resolution: dict[str, Any] | None = None


@dataclass
class Run:
    id: str
    engagement_id: str
    trigger: Trigger
    brain: str
    started_at: datetime
    finished_at: datetime | None = None
    actions: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None


@dataclass
class Call:
    """A voice call session. Lives across the Twilio websocket and status callbacks."""
    id: str
    engagement_id: str
    to: str
    purpose: str
    opening: str
    status: str = "placing"
    provider_sid: str | None = None
    transcript: list[dict[str, str]] = field(default_factory=list)
    created_at: datetime | None = None
    ended_at: datetime | None = None
