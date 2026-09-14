"""Runtime guardrails. Enforced by the platform on every outbound action, regardless of brain.

A model can be told "don't chase forever" and "get sign-off before talking settlement". A
platform should not have to trust that. These checks run in the runtime, before a message
leaves, and turn the action into an approval request or an escalation instead.
"""
from __future__ import annotations

from dataclasses import dataclass

from .actions import SendMessage
from .models import Engagement, Event
from .playbooks.base import Playbook


@dataclass
class Verdict:
    kind: str            # allow | approval | escalation
    reason: str = ""


def check_send(pb: Playbook, e: Engagement, action: SendMessage, events: list[Event]) -> Verdict:
    if action.channel not in pb.channels:
        return Verdict("escalation", f"Agent tried channel '{action.channel}', not allowed for this playbook")
    if e.unanswered_attempts >= pb.max_unanswered_attempts:
        return Verdict("escalation", f"No response after {e.unanswered_attempts} attempts")
    if "first_message" in pb.approval_rules and not any(ev.type == "message_out" for ev in events):
        return Verdict("approval", "First outbound message on this engagement requires sign-off")
    if "sensitive_terms" in pb.approval_rules:
        body = action.body.lower()
        hit = [w for w in pb.sensitive_terms if w in body]
        if hit:
            return Verdict("approval", f"Message mentions {', '.join(hit)}")
    return Verdict("allow")


ESCALATION_OPTIONS = ["Keep trying", "Try phone", "I'll handle it", "Close"]
