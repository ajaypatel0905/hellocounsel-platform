"""Live call sessions. Transport-agnostic: the Twilio websocket and the demo simulator both
drive the same three methods. The brain that talks on the phone is the brain that runs the
rest of the platform; the call's transcript comes back to the engagement as an inbound reply.
"""
from __future__ import annotations

from . import playbooks
from .context import CallContext, CallTurn
from .models import Trigger
from .runtime import Runtime

FAILED_STATUSES = {"busy", "no-answer", "failed", "canceled"}


class CallManager:
    def __init__(self, runtime: Runtime):
        self.rt = runtime

    def greeting(self, call_id: str) -> str:
        return self.rt.store.get_call(call_id).opening

    def on_utterance(self, call_id: str, text: str) -> CallTurn:
        call = self.rt.store.get_call(call_id)
        e = self.rt.store.get_engagement(call.engagement_id)
        ctx = CallContext(e, self.rt.store.get_matter(e.matter_id), self.rt.store.get_contact(e.contact_id),
                          playbooks.get(e.playbook), call, self.rt.clock.now())
        call.transcript.append({"role": "them", "text": text})
        call.status = "in_progress"
        try:
            turn = self.rt.brain.converse(ctx, text)
        except Exception as ex:
            turn = CallTurn(say="I'm sorry, I'm having trouble on my end. Someone from the firm will follow up. Goodbye.",
                            end_call=True, note=f"brain error: {ex}")
        call.transcript.append({"role": "agent", "text": turn.say})
        if turn.note:
            call.transcript.append({"role": "note", "text": turn.note})
        self.rt.store.put_call(call)
        return turn

    def mark_status(self, call_id: str, provider_status: str, provider_sid: str | None = None) -> None:
        call = self.rt.store.get_call(call_id)
        if call is None or call.ended_at:
            return
        if provider_sid:
            call.provider_sid = provider_sid
        call.status = provider_status
        self.rt.store.put_call(call)
        if provider_status in FAILED_STATUSES:
            self.finish(call_id, provider_status)
        elif provider_status == "completed":
            self.finish(call_id, "completed")

    def finish(self, call_id: str, status: str = "completed") -> None:
        """Idempotent. Called from the websocket close, the Connect action callback, or the status callback."""
        call = self.rt.store.get_call(call_id)
        if call is None or call.ended_at:
            return
        now = self.rt.clock.now()
        call.status, call.ended_at = status, now
        self.rt.store.put_call(call)
        them = [t["text"] for t in call.transcript if t["role"] == "them"]
        if status == "completed" and them:
            body = " ".join(them)
            self.rt._event(call.engagement_id, "call_completed", "counterparty",
                           {"call_id": call.id, "transcript": call.transcript, "turns": len(them)})
            self.rt.handle_inbound("voice", body, engagement_id=call.engagement_id,
                                   extra={"call_id": call.id, "transcript": call.transcript})
        else:
            reason = status if status != "completed" else "answered but nothing was said"
            self.rt._event(call.engagement_id, "call_failed", "system", {"call_id": call.id, "reason": reason})
            e = self.rt.store.get_engagement(call.engagement_id)
            if e and e.status.value in ("waiting", "active"):
                self.rt.store.cancel_pending_wakeups(e.id, now)
                self.rt._wake(e, Trigger.CHANNEL_EVENT, {"channel": "voice", "reason": f"call {reason}"}, now)

    def simulate(self, call_id: str, utterances: list[str]) -> list[dict]:
        """Demo/test transport: play the counterparty's lines against the live agent."""
        for u in utterances:
            turn = self.on_utterance(call_id, u)
            if turn.end_call:
                break
        self.finish(call_id, "completed")
        return self.rt.store.get_call(call_id).transcript
