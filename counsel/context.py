"""What a brain sees when it is asked to decide. Built by the runtime, rendered for an LLM."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from .models import Call, Contact, Engagement, Event, Matter, Trigger

if TYPE_CHECKING:
    from .playbooks.base import Playbook


@dataclass
class StepContext:
    engagement: Engagement
    matter: Matter
    contact: Contact
    playbook: "Playbook"
    trigger: Trigger
    payload: dict[str, Any]
    events: list[Event]
    now: datetime

    # ---- convenience for scripted policies ------------------------------
    @property
    def inbound_text(self) -> str:
        return (self.payload.get("body") or "").strip()

    @property
    def decision(self) -> str:
        return (self.payload.get("decision") or "").strip()

    @property
    def decision_text(self) -> str:
        return (self.payload.get("text") or "").strip()

    @property
    def intervention_kind(self) -> str:
        return self.payload.get("kind") or ""

    def has_event(self, type: str) -> bool:
        return any(e.type == type for e in self.events)

    def preferred_channel(self) -> str:
        pref = self.contact.preferred_channel
        return pref if pref in self.playbook.channels else self.playbook.channels[0]

    def render(self) -> str:
        e, m, c = self.engagement, self.matter, self.contact
        lines = [
            f"NOW: {self.now.isoformat(timespec='minutes')}",
            f"MATTER: {m.client_name} (DOB {m.client_dob or 'unknown'}) | {m.case_type} | incident {m.incident_date} | firm owner {m.owner}",
            f"  notes: {m.notes}" if m.notes else "",
            f"COUNTERPARTY: {c.name} ({c.kind}) phone={c.phone or '-'} email={c.email or '-'} preferred={c.preferred_channel}",
            f"  notes: {c.notes}" if c.notes else "",
            f"ENGAGEMENT {e.id}: goal = {e.goal}",
            f"  params: {e.params}" if e.params else "",
            f"  state: {e.state}",
            f"  unanswered attempts so far: {e.unanswered_attempts} (limit {self.playbook.max_unanswered_attempts})",
            f"  created: {e.created_at.isoformat(timespec='minutes') if e.created_at else '-'}",
            "",
            "TIMELINE (oldest first):",
        ]
        for ev in self.events:
            lines.append(f"  [{ev.ts.isoformat(timespec='minutes')}] {ev.type} ({ev.actor}): {_short(ev.payload)}")
        lines += ["", f"TRIGGER: {self.trigger.value}", f"  payload: {_short(self.payload, 1200)}"]
        return "\n".join(l for l in lines if l is not None and l != "")


@dataclass
class CallContext:
    engagement: Engagement
    matter: Matter
    contact: Contact
    playbook: "Playbook"
    call: Call
    now: datetime

    def render(self) -> str:
        e, m, c = self.engagement, self.matter, self.contact
        out = [
            f"You are on a live phone call with {c.name} ({c.kind}) on behalf of the firm (owner {m.owner}).",
            f"Client: {m.client_name}, DOB {m.client_dob or 'unknown'}, {m.case_type}, incident {m.incident_date}.",
            f"Engagement goal: {e.goal}",
            f"Current state: {e.state}",
            f"Purpose of this call: {self.call.purpose}",
            "Transcript so far:",
        ]
        for t in self.call.transcript:
            out.append(f"  {t['role']}: {t['text']}")
        return "\n".join(out)


@dataclass
class CallTurn:
    say: str
    end_call: bool = False
    note: str = ""    # optional internal note (not spoken)


def _short(d: Any, n: int = 300) -> str:
    s = str(d)
    return s if len(s) <= n else s[: n - 3] + "..."
