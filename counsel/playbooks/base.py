"""A Playbook is what makes one use case different from another. Nothing else should.

It declares: who the counterparty is, which channels are allowed, how often to follow up,
what structured state to track, when the runtime should force a human in, and the
instructions the LLM brain gets. It also carries an offline policy (a deterministic
stand-in for the LLM) so the platform runs and tests without a model.
"""
from __future__ import annotations

import re
from datetime import timedelta

from ..actions import Action, Complete, RecordUpdate, RequestHuman, SendMessage, WaitUntil
from ..context import CallContext, CallTurn, StepContext
from ..models import Trigger

PLATFORM_PROMPT = """You are an agent working for a plaintiff law firm. You run one bounded step at a time:
you are woken by a trigger, you look at the engagement's timeline, you take a few actions using
the tools, and you end the step with exactly one of: wait_until, request_human, or complete.

Rules that apply to every playbook:
- Be brief, warm and professional in messages. Never give legal advice or discuss case value,
  liability, or settlement with anyone; if asked, request_human.
- Record a meaningful or urgent update whenever something changes that the firm would want to know.
- Do not repeat a message the timeline shows you already sent unless the trigger is 'scheduled'.
- If a reply is ambiguous, request_human rather than guess.
- If the counterparty asks for money, a signed document, or anything you cannot provide, request_human.
- When the trigger is human_decision, the payload holds the firm's answer; act on it.
- When the trigger is inbound and the body is a phone call transcript, treat it as the counterparty's reply.
"""


class Playbook:
    key: str = ""
    name: str = ""
    description: str = ""
    counterparty_kind: str = "provider"
    channels: list[str] = ["email"]
    default_cadence_hours: float = 72
    max_unanswered_attempts: int = 3
    approval_rules: list[str] = []          # "first_message" | "sensitive_terms"
    sensitive_terms: list[str] = []
    state_fields: dict[str, str] = {}
    instructions: str = ""

    # ------------------------------------------------------------------ LLM
    def system_prompt(self) -> str:
        fields = "\n".join(f"  - {k}: {v}" for k, v in self.state_fields.items())
        return (
            f"{PLATFORM_PROMPT}\nPLAYBOOK: {self.name}\n{self.description}\n\n"
            f"Allowed channels (in order of preference): {', '.join(self.channels)}\n"
            f"Default cadence between follow-ups: {self.default_cadence_hours} hours\n"
            f"State fields to maintain via record_update.state:\n{fields}\n\n"
            f"Playbook instructions:\n{self.instructions}\n"
        )

    def call_prompt(self) -> str:
        return (
            "You are speaking on a live phone call on behalf of a plaintiff law firm. Keep each turn to one or two "
            "short sentences. Confirm what you hear. Ask for a concrete date when one is missing. Never give legal "
            "advice. You have no authority to agree to fees, payments, deadlines or releases: if asked, say you will "
            "confirm with the office and get back to them. When you have what you need, or the person cannot help, "
            "thank them and end the call.\n"
            f"Playbook: {self.name}. {self.description}"
        )

    # ------------------------------------------------------------- offline
    def offline_step(self, ctx: StepContext) -> list[Action]:
        raise NotImplementedError

    def offline_call_turn(self, ctx: CallContext, utterance: str) -> CallTurn:
        user_turns = [t for t in ctx.call.transcript if t["role"] == "them"]
        if len(user_turns) <= 1:
            return CallTurn(say=f"Thanks. I have noted: {utterance.rstrip('.')}. Is there a date I can give the firm?")
        return CallTurn(say="Got it, thank you. I'll pass that along. Have a good day.", end_call=True)

    # ---------------------------------------------------------------- misc
    def cadence(self) -> timedelta:
        return timedelta(hours=self.default_cadence_hours)


# ------------------------------------------------------------------ helpers
def mentions(text: str, *words: str) -> bool:
    t = text.lower()
    return any(re.search(rf"\b{re.escape(w)}", t) for w in words)


def parse_days(text: str, default: float) -> float:
    """'in 2 weeks' / '5 business days' / 'next week' -> hours."""
    t = text.lower()
    m = re.search(r"(\d+)\s*(business\s+)?(day|week)", t)
    if m:
        n = int(m.group(1)); unit = m.group(3)
        return n * (24 if unit == "day" else 24 * 7)
    if "tomorrow" in t:
        return 24
    if "next week" in t:
        return 24 * 7
    if "end of the month" in t or "end of month" in t:
        return 24 * 14
    return default


def shared_human_decision(ctx: StepContext, follow_up_body: str) -> list[Action] | None:
    """Decisions every playbook handles the same way. Returns None if not one of them."""
    d = ctx.decision.lower()
    kind = ctx.intervention_kind
    if kind == "approval":
        if d.startswith("approve"):
            return [WaitUntil(ctx.playbook.default_cadence_hours, "approved message sent; waiting for reply")]
        return [RecordUpdate(f"Firm rejected the draft: {ctx.decision_text or 'no reason given'}", "routine"),
                WaitUntil(ctx.playbook.default_cadence_hours, "draft rejected; will revisit")]
    if kind == "escalation":
        if d.startswith("keep trying"):
            return [SendMessage(ctx.preferred_channel(), follow_up_body, "follow_up"),
                    WaitUntil(ctx.playbook.default_cadence_hours, "retrying after escalation")]
        if d.startswith("try phone") or d.startswith("call"):
            return [SendMessage("voice", follow_up_body, "follow_up_call"),
                    WaitUntil(24, "waiting for call outcome")]
    if d in ("i'll handle it", "i will handle it", "handled"):
        return [Complete("handed_to_firm", "Firm took over this engagement.")]
    if d in ("close", "cancel", "stop"):
        return [Complete("closed_by_firm", ctx.decision_text or "Closed by the firm.")]
    return None
