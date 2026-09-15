"""The engagement runtime. One bounded step per trigger; durable state in between.

    trigger -> build context -> brain.decide() -> apply actions through policies -> persist
                                                   |-- send via channel
                                                   |-- record update
                                                   |-- open intervention (blocked)
                                                   |-- schedule wakeup (waiting)
                                                   '-- complete
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from . import playbooks
from .actions import (Action, Complete, RecordUpdate, RequestHuman, SendMessage, WaitUntil, action_from_tool_call,
                      action_to_dict)
from .brain import Brain
from .channels import ChannelSet
from .clock import Clock
from .context import StepContext
from .models import (Engagement, EngagementStatus as S, Event, Intervention, InterventionKind as K, Run, Trigger,
                     Wakeup, new_id)
from .policies import ESCALATION_OPTIONS, check_send
from .store import Store

TIMELINE_WINDOW = 40


class Runtime:
    def __init__(self, store: Store, clock: Clock, brain: Brain, channels: ChannelSet):
        self.store, self.clock, self.brain, self.channels = store, clock, brain, channels

    # ------------------------------------------------------------ lifecycle
    def create_engagement(self, playbook: str, matter_id: str, contact_id: str, goal: str, owner: str,
                          params: dict | None = None) -> Engagement:
        pb = playbooks.get(playbook)
        contact = self.store.get_contact(contact_id)
        if contact is None or self.store.get_matter(matter_id) is None:
            raise KeyError("unknown matter or contact")
        if contact.kind != pb.counterparty_kind:
            raise ValueError(f"playbook '{playbook}' expects a {pb.counterparty_kind}, got {contact.kind}")
        now = self.clock.now()
        e = Engagement(id=new_id("eng"), playbook=playbook, matter_id=matter_id, contact_id=contact_id, goal=goal,
                       owner=owner, params=params or {}, created_at=now, updated_at=now)
        self.store.put_engagement(e)
        self._event(e.id, "engagement_created", "system", {"playbook": playbook, "goal": goal})
        self._wake(e, Trigger.CREATED, {}, now)
        return e

    def nudge(self, engagement_id: str, instruction: str, by: str = "firm") -> Engagement:
        e = self._get(engagement_id)
        self._event(e.id, "firm_request", "human", {"instruction": instruction, "by": by})
        if e.status == S.BLOCKED:
            return e  # held; the open intervention comes first
        if e.status in (S.COMPLETED, S.CANCELLED):
            raise ValueError("engagement is closed")
        self.store.cancel_pending_wakeups(e.id, self.clock.now())
        self._wake(e, Trigger.FIRM_REQUEST, {"instruction": instruction}, self.clock.now())
        return e

    def cancel(self, engagement_id: str, reason: str = "") -> Engagement:
        e = self._get(engagement_id)
        e.status, e.outcome, e.completed_at = S.CANCELLED, reason or "cancelled", self.clock.now()
        self.store.cancel_pending_wakeups(e.id, self.clock.now())
        for i in self.store.list_interventions(status="open", engagement_id=e.id):
            i.status, i.resolved_at, i.resolution = "resolved", self.clock.now(), {"decision": "cancelled"}
            self.store.put_intervention(i)
        self._save(e)
        self._event(e.id, "status_changed", "human", {"status": "cancelled", "reason": reason})
        return e

    # ------------------------------------------------------------------ step
    def step(self, engagement_id: str, trigger: Trigger, payload: dict[str, Any]) -> Run | None:
        e = self._get(engagement_id)
        if e.status in (S.COMPLETED, S.CANCELLED):
            return None
        if e.status == S.BLOCKED and trigger != Trigger.HUMAN_DECISION:
            self._event(e.id, "deferred", "system", {"trigger": trigger.value, "why": "waiting on a human"})
            return None
        pb = playbooks.get(e.playbook)
        matter, contact = self.store.get_matter(e.matter_id), self.store.get_contact(e.contact_id)
        events = self.store.list_events(e.id, limit=TIMELINE_WINDOW)
        now = self.clock.now()
        ctx = StepContext(e, matter, contact, pb, trigger, payload, events, now)
        run = Run(id=new_id("run"), engagement_id=e.id, trigger=trigger, brain=self.brain.name, started_at=now)
        e.status = S.ACTIVE
        try:
            actions = self.brain.decide(ctx)
        except Exception as ex:  # the model failed; a person picks it up rather than the engagement dying quietly
            run.error = f"{type(ex).__name__}: {ex}"
            self._event(e.id, "run_error", "system", {"error": run.error})
            self._block(e, K.ESCALATION, f"The agent hit an error and needs a hand: {run.error[:200]}",
                        ["Retry", "I'll handle it", "Close"], urgency="high",
                        payload={"retry": {"trigger": trigger.value, "payload": payload}})
            run.finished_at = self.clock.now()
            self.store.put_run(run)
            self._save(e)
            return run
        run.actions = [action_to_dict(a) for a in actions]
        self._apply(e, pb, contact, actions)
        run.finished_at = self.clock.now()
        self.store.put_run(run)
        self._save(e)
        return run

    def _apply(self, e: Engagement, pb, contact, actions: list[Action]) -> None:
        ended = False
        for a in actions:
            if isinstance(a, SendMessage):
                verdict = check_send(pb, e, a, self.store.list_events(e.id))
                if verdict.kind == "approval":
                    self._block(e, K.APPROVAL, f"Approve this {a.channel} to {contact.name}? {verdict.reason}.",
                                ["Approve", "Reject"], payload={"action": action_to_dict(a), "reason": verdict.reason})
                    ended = True; break
                if verdict.kind == "escalation":
                    self._block(e, K.ESCALATION, f"{verdict.reason} from {contact.name}. What should I do?",
                                ESCALATION_OPTIONS, payload={"blocked_action": action_to_dict(a), "reason": verdict.reason})
                    ended = True; break
                self._send(e, contact, a)
            elif isinstance(a, RecordUpdate):
                self._record(e, a)
            elif isinstance(a, RequestHuman):
                self._block(e, K.QUESTION, a.question, a.options, urgency=a.urgency)
                ended = True; break
            elif isinstance(a, WaitUntil):
                self._schedule(e, a.hours, a.reason)
                ended = True; break
            elif isinstance(a, Complete):
                self._complete(e, a)
                ended = True; break
        if not ended and e.status not in (S.BLOCKED, S.COMPLETED, S.CANCELLED):
            self._event(e.id, "note", "system", {"text": "step ended without a terminal action; using default cadence"})
            self._schedule(e, pb.default_cadence_hours, "default cadence")

    # --------------------------------------------------------- action effects
    def _send(self, e: Engagement, contact, a: SendMessage) -> None:
        payload = self.channels.get(a.channel).send(e, contact, a)
        self._event(e.id, "call_placed" if a.channel == "voice" else "message_out", "agent", payload)
        if a.purpose != "reply":          # answering the counterparty is not another unanswered attempt
            e.unanswered_attempts += 1
        e.status = S.WAITING
        if payload.get("error"):
            self._wake(e, Trigger.CHANNEL_EVENT, {"channel": a.channel, "reason": payload["error"]}, self.clock.now())

    def _record(self, e: Engagement, a: RecordUpdate) -> None:
        e.state.update({k: v for k, v in (a.state or {}).items() if v not in (None, "")})
        self._event(e.id, "update", "agent", {"summary": a.summary, "significance": a.significance, "state": a.state})

    def _block(self, e: Engagement, kind: K, question: str, options: list[str], payload: dict | None = None,
               urgency: str = "normal") -> Intervention:
        now = self.clock.now()
        i = Intervention(id=new_id("int"), engagement_id=e.id, kind=kind, question=question, options=options,
                         payload=payload or {}, urgency=urgency, created_at=now)
        self.store.put_intervention(i)
        self.store.cancel_pending_wakeups(e.id, now)
        e.status, e.next_wake_at = S.BLOCKED, None
        self._event(e.id, "intervention_opened", "agent" if kind == K.QUESTION else "system",
                    {"intervention_id": i.id, "kind": kind.value, "question": question, "options": options, "urgency": urgency})
        return i

    def _schedule(self, e: Engagement, hours: float, reason: str) -> None:
        due = self.clock.now() + timedelta(hours=max(hours, 0.01))
        self._wake(e, Trigger.SCHEDULED, {"reason": reason}, due)
        e.status, e.next_wake_at = S.WAITING, due
        self._event(e.id, "wait_scheduled", "agent", {"until": due.isoformat(timespec="minutes"), "hours": hours, "reason": reason})

    def _complete(self, e: Engagement, a: Complete) -> None:
        e.status, e.outcome, e.completed_at, e.next_wake_at = S.COMPLETED, a.outcome, self.clock.now(), None
        self.store.cancel_pending_wakeups(e.id, self.clock.now())
        self._event(e.id, "completed", "agent", {"outcome": a.outcome, "summary": a.summary})

    # ------------------------------------------------------- external inputs
    def handle_inbound(self, channel: str, body: str, address: str | None = None, engagement_id: str | None = None,
                       extra: dict | None = None) -> Engagement | None:
        e = self._route_inbound(channel, address, engagement_id)
        if e is None:
            return None
        payload = {"channel": channel, "from": address, "body": body, **(extra or {})}
        self._event(e.id, "message_in", "counterparty", payload)
        e.unanswered_attempts = 0
        if e.status == S.BLOCKED:
            self._event(e.id, "held_for_human", "system", {"why": "reply arrived while waiting on the firm"})
            self._save(e)
            return e
        now = self.clock.now()
        self.store.cancel_pending_wakeups(e.id, now)
        e.status = S.ACTIVE
        self._save(e)
        self._wake(e, Trigger.INBOUND, payload, now)
        return e

    def _route_inbound(self, channel: str, address: str | None, engagement_id: str | None) -> Engagement | None:
        live = (S.ACTIVE, S.WAITING, S.BLOCKED)
        if engagement_id:
            e = self.store.get_engagement(engagement_id)
            return e if e and e.status in live else None
        if not address:
            return None
        for c in self.store.find_contact_by_address(address):
            cands = [x for x in self.store.list_engagements(contact_id=c.id, status=live)
                     if channel in playbooks.get(x.playbook).channels]
            if cands:  # most recently contacted first
                cands.sort(key=lambda x: x.updated_at or x.created_at, reverse=True)
                return cands[0]
        return None

    def resolve_intervention(self, intervention_id: str, decision: str, text: str = "", edited_body: str | None = None,
                             by: str = "human") -> Engagement:
        i = self.store.get_intervention(intervention_id)
        if i is None or i.status != "open":
            raise ValueError("intervention not open")
        now = self.clock.now()
        i.status, i.resolved_at = "resolved", now
        i.resolution = {"decision": decision, "text": text, "edited_body": edited_body, "by": by}
        self.store.put_intervention(i)
        e = self._get(i.engagement_id)
        self._event(e.id, "intervention_resolved", "human", {"intervention_id": i.id, "kind": i.kind.value,
                                                               "decision": decision, "text": text, "by": by})
        e.status = S.ACTIVE
        if i.kind == K.APPROVAL and decision.lower().startswith("approve"):
            spec = dict(i.payload["action"]); name = spec.pop("action")
            a = action_from_tool_call(name, spec)
            if edited_body:
                a.body = edited_body
            self._send(e, self.store.get_contact(e.contact_id), a)   # approved: bypasses the gate on purpose
            e.status = S.ACTIVE
        if i.kind == K.ESCALATION:
            e.unanswered_attempts = 0
            if decision.lower() == "retry" and i.payload.get("retry"):
                self._save(e)
                self._wake(e, Trigger(i.payload["retry"]["trigger"]), i.payload["retry"]["payload"], now)
                return e
        self._save(e)
        self._wake(e, Trigger.HUMAN_DECISION, {"kind": i.kind.value, "decision": decision, "text": text,
                                                "question": i.question, "intervention_id": i.id}, now)
        return e

    # ---------------------------------------------------------------- worker
    def tick(self) -> int:
        due = self.store.claim_due_wakeups(self.clock.now())
        for w in due:
            if w.payload.get("cancelled"):
                continue
            self.step(w.engagement_id, w.trigger, w.payload)
        return len(due)

    def drain(self, max_rounds: int = 100) -> int:
        total = 0
        for _ in range(max_rounds):
            n = self.tick()
            total += n
            if n == 0:
                break
        return total

    # ----------------------------------------------------------------- utils
    def _get(self, engagement_id: str) -> Engagement:
        e = self.store.get_engagement(engagement_id)
        if e is None:
            raise KeyError(f"no engagement {engagement_id}")
        return e

    def _save(self, e: Engagement) -> None:
        e.updated_at = self.clock.now()
        self.store.put_engagement(e)

    def _event(self, engagement_id: str, type: str, actor: str, payload: dict[str, Any]) -> Event:
        return self.store.add_event(Event(id=new_id("ev"), engagement_id=engagement_id, ts=self.clock.now(),
                                          type=type, actor=actor, payload=payload))

    def _wake(self, e: Engagement, trigger: Trigger, payload: dict[str, Any], due) -> Wakeup:
        return self.store.add_wakeup(Wakeup(id=new_id("wk"), engagement_id=e.id, due_at=due, trigger=trigger, payload=payload))
