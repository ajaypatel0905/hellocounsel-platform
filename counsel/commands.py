"""The firm's command box: "chase City General for the final bill" -> an engagement is created or nudged.

Interpretation is a small structured-output problem. Scripted keyword matching is the offline
path; a brain that exposes interpret_command() is used when configured.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass

from . import playbooks
from .models import EngagementStatus as S
from .runtime import Runtime

COMMAND_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["create", "nudge", "cancel", "unclear"]},
        "playbook": {"type": "string"},
        "contact_id": {"type": "string"},
        "instruction": {"type": "string", "description": "what the agent should do or say, in the firm's words"},
    },
    "required": ["action"],
}


@dataclass
class CommandResult:
    action: str
    engagement_id: str | None = None
    message: str = ""
    interpreted: dict | None = None


class CommandService:
    def __init__(self, rt: Runtime):
        self.rt = rt

    def run(self, text: str, matter_id: str | None = None, by: str = "firm") -> CommandResult:
        plan = self._interpret(text, matter_id)
        act = plan.get("action")
        if act == "unclear" or not plan.get("contact_id") or not plan.get("playbook"):
            return CommandResult("unclear", message="I couldn't tell who to contact or what for. Try: "
                                 "\"ask City General for Priya's records\" or \"check in with Daniel this week\".", interpreted=plan)
        contact = self.rt.store.get_contact(plan["contact_id"])
        live = [e for e in self.rt.store.list_engagements(contact_id=contact.id, status=(S.ACTIVE, S.WAITING, S.BLOCKED))
                if e.playbook == plan["playbook"]]
        if act == "cancel":
            if not live:
                return CommandResult("noop", message=f"Nothing running for {contact.name} on {plan['playbook']}.", interpreted=plan)
            e = self.rt.cancel(live[0].id, f"cancelled via command: {text}")
            return CommandResult("cancel", e.id, f"Stopped {playbooks.get(e.playbook).name} with {contact.name}.", plan)
        if live:
            e = self.rt.nudge(live[0].id, plan.get("instruction") or text, by)
            held = e.status == S.BLOCKED
            return CommandResult("nudge", e.id, f"Passed to the agent on the existing {playbooks.get(e.playbook).name} "
                                 f"with {contact.name}." + (" It is waiting on a review item first." if held else ""), plan)
        e = self.rt.create_engagement(plan["playbook"], contact.matter_id, contact.id, plan.get("instruction") or text,
                                      self.rt.store.get_matter(contact.matter_id).owner)
        return CommandResult("create", e.id, f"Started {playbooks.get(e.playbook).name} with {contact.name}.", plan)

    # -------------------------------------------------------------- interpret
    def _interpret(self, text: str, matter_id: str | None) -> dict:
        contacts = self.rt.store.list_contacts(matter_id)
        brain = self.rt.brain
        if hasattr(brain, "interpret_command"):
            try:
                out = brain.interpret_command(text, contacts, playbooks.all_playbooks(), COMMAND_SCHEMA)
                if out.get("contact_id") in {c.id for c in contacts} and out.get("playbook") in {p.key for p in playbooks.all_playbooks()}:
                    return out
            except Exception:
                pass
        return self._scripted(text, contacts)

    def _scripted(self, text: str, contacts) -> dict:
        t = text.lower()
        action = "cancel" if re.search(r"\b(stop|cancel|pause|halt)\b", t) else "create"
        if re.search(r"\bbill|invoice|statement\b", t):
            pb = "bill_followup"
        elif re.search(r"\brecord|report|imaging|chart\b", t):
            pb = "medical_records"
        elif re.search(r"\bcheck.?in|how (is|are)|client|feeling|doing\b", t):
            pb = "client_checkin"
        else:
            pb = None
        kind = playbooks.get(pb).counterparty_kind if pb else None
        scored = []
        for c in contacts:
            words = [w.lower() for w in re.findall(r"[A-Za-z]{3,}", c.name) if w.lower() not in ("the", "and", "hospital", "records", "billing")]
            hits = sum(1 for w in words if re.search(rf"\b{re.escape(w)}", t))
            if hits:
                scored.append((hits, c))
        if kind:
            scored = [(h, c) for h, c in scored if c.kind == kind] or scored
        scored.sort(key=lambda x: -x[0])
        if not scored and kind:
            same = [c for c in contacts if c.kind == kind]
            scored = [(0, same[0])] if len(same) == 1 else []
        if not pb and scored:
            pb = "client_checkin" if scored[0][1].kind == "client" else "medical_records"
        return {"action": action if (pb and scored) else "unclear", "playbook": pb,
                "contact_id": scored[0][1].id if scored else None, "instruction": text}
