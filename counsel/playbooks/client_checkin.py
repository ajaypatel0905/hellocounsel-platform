from ..actions import Complete, RecordUpdate, RequestHuman, SendMessage, WaitUntil
from ..context import StepContext
from ..models import Trigger
from . import register
from .base import Playbook, mentions, shared_human_decision


@register
class ClientCheckIn(Playbook):
    key = "client_checkin"
    name = "Client check-in"
    description = ("Periodically check in with the client through their case: how they are feeling, treatment "
                   "changes, new providers, anything the firm should know. Never give legal advice.")
    counterparty_kind = "client"
    channels = ["sms", "voice", "email"]
    default_cadence_hours = 24 * 14
    max_unanswered_attempts = 2
    approval_rules = ["first_message", "sensitive_terms"]
    sensitive_terms = ["settlement", "settle", "offer", "case value", "worth", "liability", "fault", "lawsuit"]
    state_fields = {
        "wellbeing": "improving | same | worse | unknown",
        "treatment_status": "ongoing | discharged | new_provider | surgery_scheduled | unknown",
        "new_providers": "list of any new doctors/facilities the client mentions",
        "last_contact": "ISO date of last successful contact",
        "open_client_questions": "questions the client asked that the firm needs to answer",
    }
    instructions = """- Check-ins are short and human: ask how they are doing and whether anything changed with treatment or work.
- New treatment, surgery, new provider, missed work, worsening pain -> meaningful update.
- Client asks about their case (value, timeline, settlement, whether they should talk to insurer) -> record the question,
  reply that you'll have the team get back to them, and request_human with the question. Do not answer it.
- Client mentions another lawyer, wanting to drop the case, or distress -> urgent update + request_human.
- Client doing fine -> routine update, wait for the next cadence.
- Complete when the firm closes the engagement or params.end_date has passed."""

    def offline_step(self, ctx: StepContext):
        e, c, m = ctx.engagement, ctx.contact, ctx.matter
        ch = ctx.preferred_channel()
        first = c.name.split()[0]
        checkin = (f"Hi {first}, this is the team at {m.owner}'s office. Just checking in: how are you feeling this week, "
                   f"and has anything changed with your treatment or work? Reply anytime.")
        if ctx.trigger in (Trigger.CREATED, Trigger.SCHEDULED):
            if e.params.get("end_date") and str(ctx.now.date()) >= str(e.params["end_date"]):
                return [Complete("period_ended", "Check-in period ended.")]
            return [SendMessage(ch, checkin, "check_in"),
                    WaitUntil(72, "give the client a few days to reply")]

        if ctx.trigger == Trigger.CHANNEL_EVENT:
            return [RecordUpdate(f"Could not reach {c.name}: {ctx.payload.get('reason', 'delivery failed')}", "routine"),
                    WaitUntil(48, "retry in two days")]

        if ctx.trigger == Trigger.INBOUND:
            t = ctx.inbound_text
            today = ctx.now.date().isoformat()
            if mentions(t, "another lawyer", "other attorney", "drop the case", "fire", "give up", "can't take this"):
                return [RecordUpdate(f"{c.name} raised a retention/distress concern: \"{t[:160]}\"", "urgent",
                                     {"last_contact": today}),
                        RequestHuman(f"{c.name} said: \"{t[:200]}\". This needs a person from the firm today.",
                                     ["I'll call them", "Handled"], "high")]
            if mentions(t, "settle", "settlement", "worth", "how much", "offer", "insurance called", "adjuster", "should i", "lawsuit", "court"):
                return [SendMessage(ch, f"Thanks {first}, that's a good question. I'll have {m.owner} get back to you directly on it.", "reply"),
                        RecordUpdate(f"{c.name} asked a case question: \"{t[:160]}\"", "meaningful",
                                     {"open_client_questions": t[:200], "last_contact": today}),
                        RequestHuman(f"{c.name} asked: \"{t[:200]}\". I told them the team will follow up. Please answer them.",
                                     ["Answered", "I'll handle it"], "normal")]
            if mentions(t, "surgery", "operation", "hospital", "er ", "emergency", "worse", "more pain", "new doctor", "referred", "specialist", "mri", "physical therapy", "pt "):
                state = {"last_contact": today, "wellbeing": "worse" if mentions(t, "worse", "more pain") else "same",
                         "treatment_status": "surgery_scheduled" if mentions(t, "surgery", "operation") else "new_provider"
                         if mentions(t, "new doctor", "referred", "specialist") else "ongoing"}
                return [SendMessage(ch, f"Thank you for letting us know, {first}. I've passed this to {m.owner}. Please keep any paperwork or bills from this; we'll need them.", "reply"),
                        RecordUpdate(f"{c.name}: treatment change: \"{t[:160]}\"", "meaningful", state),
                        WaitUntil(self.default_cadence_hours, "next scheduled check-in")]
            if mentions(t, "fine", "ok", "okay", "good", "better", "same", "alright", "no change", "nothing new"):
                return [SendMessage(ch, f"Glad to hear it, {first}. We'll check in again in a couple of weeks. Reach out anytime.", "reply"),
                        RecordUpdate(f"{c.name} is doing {'better' if 'better' in t.lower() else 'okay'}, no changes", "routine",
                                     {"wellbeing": "improving" if "better" in t.lower() else "same", "last_contact": today}),
                        WaitUntil(self.default_cadence_hours, "next scheduled check-in")]
            return [RecordUpdate(f"{c.name} replied: \"{t[:160]}\"", "routine", {"last_contact": today}),
                    RequestHuman(f"{c.name} replied \"{t[:200]}\" and I'm not sure how to respond. Guidance?",
                                 ["Just thank them", "I'll handle it"], "low")]

        if ctx.trigger == Trigger.HUMAN_DECISION:
            d = ctx.decision.lower()
            if d.startswith("answered") or d.startswith("just thank"):
                if d.startswith("just thank"):
                    return [SendMessage(ch, f"Thanks {first}, noted. We'll be in touch.", "reply"),
                            WaitUntil(self.default_cadence_hours, "next scheduled check-in")]
                return [RecordUpdate("Firm answered the client's question", "routine", {"open_client_questions": ""}),
                        WaitUntil(self.default_cadence_hours, "next scheduled check-in")]
            if d.startswith("i'll call them"):
                return [RecordUpdate("Firm is calling the client directly", "routine"),
                        WaitUntil(self.default_cadence_hours, "firm is handling; resume cadence after")]
            shared = shared_human_decision(ctx, checkin)
            if shared:
                return shared
            return [WaitUntil(self.default_cadence_hours, f"unrecognised decision '{ctx.decision}'")]

        if ctx.trigger == Trigger.FIRM_REQUEST:
            instr = ctx.payload.get("instruction", "")
            ch = ctx.payload.get("channel") or ch
            return [SendMessage(ch, f"Hi {first}, {m.owner}'s office here. {instr}", "firm_request"),
                    WaitUntil(72, "sent per firm instruction")]
        return [WaitUntil(self.default_cadence_hours, "no rule matched")]
