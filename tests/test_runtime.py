from counsel.models import EngagementStatus as S, Trigger
from counsel.store import Store
from counsel.clock import utcnow
from counsel.models import Engagement, Wakeup, new_id
from conftest import open_interventions, pending_call


def test_records_lifecycle_over_voice_with_fee_and_receipt(app):
    rt = app.runtime
    e = rt.create_engagement("medical_records", "m1", "hospital", "Get Priya's records", "Rohan Mehta")
    rt.drain()
    e = app.store.get_engagement(e.id)
    assert e.status == S.WAITING and e.state["stage"] == "requested" and e.unanswered_attempts == 1
    call = pending_call(app, e.id)

    # The provider answers the live call and quotes a fee -> agent asks the firm.
    transcript = app.calls.simulate(call.id, ["Yes, that request is here. There is a $45 processing fee before we release."])
    rt.drain()
    assert any(t["role"] == "agent" for t in transcript)
    e = app.store.get_engagement(e.id)
    assert e.status == S.BLOCKED and e.state["stage"] == "fee_requested" and e.state["fee_amount"] == "$45"
    [i] = open_interventions(app, e.id)
    assert i.kind.value == "question" and "Approve fee" in i.options

    # Firm approves; agent confirms with the provider (another call) and waits.
    rt.resolve_intervention(i.id, "Approve fee"); rt.drain()
    e = app.store.get_engagement(e.id)
    assert e.status == S.WAITING and e.state["stage"] == "processing"
    call2 = pending_call(app, e.id)
    app.calls.simulate(call2.id, ["Great, the records were mailed to your office yesterday."]); rt.drain()
    e = app.store.get_engagement(e.id)
    assert e.status == S.BLOCKED and e.state["stage"] == "records_sent"
    [i2] = open_interventions(app, e.id)
    rt.resolve_intervention(i2.id, "Received, close"); rt.drain()
    e = app.store.get_engagement(e.id)
    assert e.status == S.COMPLETED and e.outcome == "records_received"
    types = [ev.type for ev in app.store.list_events(e.id)]
    assert types[0] == "engagement_created" and types[-1] == "completed"
    assert "call_placed" in types and "call_completed" in types and "intervention_resolved" in types


def test_unanswered_attempts_guardrail_escalates_regardless_of_brain(app):
    rt = app.runtime
    e = rt.create_engagement("bill_followup", "m1", "billing", "Final PT bill", "Rohan Mehta")
    rt.drain()
    for _ in range(2):
        app.clock.advance(days=6); rt.drain()
    e = app.store.get_engagement(e.id)
    assert e.unanswered_attempts == 3 and e.status == S.WAITING
    app.clock.advance(days=6); rt.drain()          # 4th send is blocked by the runtime, not the playbook
    e = app.store.get_engagement(e.id)
    assert e.status == S.BLOCKED
    [i] = open_interventions(app, e.id)
    assert i.kind.value == "escalation" and "No response after 3 attempts" in i.question
    rt.resolve_intervention(i.id, "Try phone"); rt.drain()
    e = app.store.get_engagement(e.id)
    assert e.status == S.WAITING and e.unanswered_attempts == 1
    assert app.store.list_calls(e.id), "escalation resolution should have switched to voice"


def test_first_client_message_requires_approval_and_can_be_edited(app):
    rt = app.runtime
    e = rt.create_engagement("client_checkin", "m1", "client", "Fortnightly check-in", "Rohan Mehta")
    rt.drain()
    e = app.store.get_engagement(e.id)
    assert e.status == S.BLOCKED and app.sms.outbox == []
    [i] = open_interventions(app, e.id)
    assert i.kind.value == "approval" and i.payload["action"]["channel"] == "sms"
    rt.resolve_intervention(i.id, "Approve", edited_body="Hi Priya, Rohan's office here. How are you feeling this week?")
    rt.drain()
    e = app.store.get_engagement(e.id)
    assert e.status == S.WAITING
    assert app.sms.outbox[-1]["body"].startswith("Hi Priya, Rohan's office here")


def test_client_case_question_is_deflected_and_escalated(app):
    rt = app.runtime
    e = rt.create_engagement("client_checkin", "m1", "client", "check-in", "Rohan Mehta"); rt.drain()
    [i] = open_interventions(app, e.id); rt.resolve_intervention(i.id, "Approve"); rt.drain()
    rt.handle_inbound("sms", "hey how much do you think my case is worth? the adjuster called me", address="+15550001111")
    rt.drain()
    e = app.store.get_engagement(e.id)
    assert e.status == S.BLOCKED
    assert "get back to you" in app.sms.outbox[-1]["body"]
    [q] = open_interventions(app, e.id)
    assert q.kind.value == "question" and "worth" in q.question
    updates = [ev for ev in app.store.list_events(e.id) if ev.type == "update"]
    assert updates[-1].payload["significance"] == "meaningful"


def test_sensitive_terms_from_firm_instruction_still_need_signoff(app):
    rt = app.runtime
    e = rt.create_engagement("client_checkin", "m1", "client", "check-in", "Rohan Mehta"); rt.drain()
    [i] = open_interventions(app, e.id); rt.resolve_intervention(i.id, "Approve"); rt.drain()
    rt.nudge(e.id, "Tell her the settlement offer came in and we will call tomorrow"); rt.drain()
    e = app.store.get_engagement(e.id)
    assert e.status == S.BLOCKED
    [g] = open_interventions(app, e.id)
    assert g.kind.value == "approval" and "settlement" in g.question


def test_inbound_while_blocked_is_held_not_acted_on(app):
    rt = app.runtime
    e = rt.create_engagement("client_checkin", "m1", "client", "check-in", "Rohan Mehta"); rt.drain()
    runs_before = len(app.store.list_runs(e.id))
    rt.handle_inbound("sms", "hello?", address="+15550001111"); rt.drain()
    assert len(app.store.list_runs(e.id)) == runs_before
    assert app.store.list_events(e.id)[-1].type == "held_for_human"


def test_brain_failure_becomes_an_escalation_not_a_dead_engagement(app):
    class Broken:
        name = "broken"
        def decide(self, ctx): raise RuntimeError("model unavailable")
        def converse(self, ctx, u): raise RuntimeError("x")
    app.runtime.brain = Broken()
    e = app.runtime.create_engagement("bill_followup", "m1", "billing", "bill", "Rohan"); app.runtime.drain()
    e = app.store.get_engagement(e.id)
    assert e.status == S.BLOCKED
    [i] = open_interventions(app, e.id)
    assert i.kind.value == "escalation" and i.urgency == "high"
    assert app.store.list_runs(e.id)[0].error.startswith("RuntimeError")


def test_wakeups_are_claimed_once():
    s = Store(); now = utcnow()
    s.put_engagement(Engagement("e1", "bill_followup", "m", "c", "g", "o"))
    s.add_wakeup(Wakeup(new_id("wk"), "e1", now, Trigger.CREATED))
    assert len(s.claim_due_wakeups(now)) == 1
    assert s.claim_due_wakeups(now) == []


def test_new_playbook_is_a_file_not_a_runtime_change(app):
    from counsel import playbooks
    from counsel.playbooks.base import Playbook
    from counsel.actions import Complete, RecordUpdate, SendMessage, WaitUntil

    @playbooks.register
    class LienBalance(Playbook):
        key = "lien_balance"; name = "Lien balance confirmation"; counterparty_kind = "provider"
        channels = ["email"]; default_cadence_hours = 48
        def offline_step(self, ctx):
            if ctx.trigger == Trigger.CREATED:
                return [SendMessage("email", "Please confirm the current lien balance.", "initial_request"), WaitUntil(48)]
            if ctx.trigger == Trigger.INBOUND:
                return [RecordUpdate("Lien balance confirmed", "meaningful", {"balance": ctx.inbound_text}),
                        Complete("balance_confirmed")]
            return [WaitUntil(48)]

    rt = app.runtime
    e = rt.create_engagement("lien_balance", "m1", "billing", "Confirm lien", "Rohan"); rt.drain()
    rt.handle_inbound("email", "$1,240.00", address="billing@spt.example"); rt.drain()
    e = app.store.get_engagement(e.id)
    assert e.status == S.COMPLETED and e.state["balance"] == "$1,240.00"
    assert "lien_balance" in [p.key for p in playbooks.all_playbooks()]


def test_command_box_creates_then_nudges(app):
    from counsel.commands import CommandService
    svc = CommandService(app.runtime)
    r1 = svc.run("ask City General for Priya's records", matter_id="m1"); app.runtime.drain()
    assert r1.action == "create" and app.store.get_engagement(r1.engagement_id).playbook == "medical_records"
    r2 = svc.run("call City General again about the records and ask for a date", matter_id="m1")
    assert r2.action == "nudge" and r2.engagement_id == r1.engagement_id
    r3 = svc.run("stop chasing City General records", matter_id="m1")
    assert r3.action == "cancel" and app.store.get_engagement(r1.engagement_id).status == S.CANCELLED
    assert svc.run("do the thing", matter_id="m1").action == "unclear"


def test_failed_call_wakes_agent_with_channel_event(app):
    rt = app.runtime
    e = rt.create_engagement("medical_records", "m1", "hospital", "records", "Rohan"); rt.drain()
    call = pending_call(app, e.id)
    app.calls.mark_status(call.id, "no-answer"); rt.drain()
    events = [ev.type for ev in app.store.list_events(e.id)]
    assert "call_failed" in events
    runs = app.store.list_runs(e.id)
    assert runs[-1].trigger == Trigger.CHANNEL_EVENT
    assert app.store.get_engagement(e.id).status == S.WAITING


def test_retry_after_agent_error_reruns_the_failed_trigger(app):
    calls = {"n": 0}
    real = app.runtime.brain
    class Flaky:
        name = "flaky"
        def decide(self, ctx):
            calls["n"] += 1
            if calls["n"] == 1: raise RuntimeError("503 spike")
            return real.decide(ctx)
        def converse(self, ctx, u): return real.converse(ctx, u)
    app.runtime.brain = Flaky()
    e = app.runtime.create_engagement("bill_followup", "m1", "billing", "bill", "Rohan"); app.runtime.drain()
    [i] = open_interventions(app, e.id)
    app.runtime.resolve_intervention(i.id, "Retry"); app.runtime.drain()
    e = app.store.get_engagement(e.id)
    assert e.status == S.WAITING and e.state["stage"] == "requested"
    assert [r.trigger for r in app.store.list_runs(e.id)] == [Trigger.CREATED, Trigger.CREATED]


def test_firm_request_can_force_a_channel(app):
    rt = app.runtime
    e = rt.create_engagement("bill_followup", "m1", "billing", "bill", "Rohan"); rt.drain()
    assert not app.store.list_calls(e.id)
    rt.nudge(e.id, "Call them and ask for a date", channel="voice"); rt.drain()
    assert app.store.list_calls(e.id), "channel hint should have produced a call"
    assert app.store.list_runs(e.id)[-1].trigger == Trigger.FIRM_REQUEST
