from ..actions import Complete, RecordUpdate, RequestHuman, SendMessage, WaitUntil
from ..context import StepContext
from ..models import Trigger
from . import register
from .base import Playbook, mentions, parse_days, shared_human_decision


@register
class MedicalRecordsFollowUp(Playbook):
    key = "medical_records"
    name = "Medical records follow-up"
    description = ("Chase a medical provider until the requested records (and bills) for the client are "
                   "released to the firm. Report status changes, fees and blockers back to the firm.")
    counterparty_kind = "provider"
    channels = ["voice", "email"]
    default_cadence_hours = 72
    max_unanswered_attempts = 3
    approval_rules = []
    state_fields = {
        "stage": "requested | processing | fee_requested | needs_authorization | records_sent | received",
        "expected_by": "ISO date the provider committed to, if any",
        "fee_amount": "fee quoted by the provider, if any",
        "provider_reference": "any ticket / request number the provider gives",
    }
    instructions = """- On creation, send the initial request (client name, DOB, date range, what is needed), record stage=requested, wait ~3 days.
- On a scheduled wake with no reply, follow up once on the preferred channel and wait again. The runtime escalates for you after too many unanswered attempts.
- Fee quoted -> record meaningful update with fee_amount, then request_human with options: Approve fee / Decline / Negotiate.
- Provider needs a signed authorization -> request_human so the firm can send it.
- Records sent/mailed/faxed -> record meaningful update stage=records_sent, then request_human asking the firm to confirm receipt (options: Received, close / Not received, follow up).
- Provider gives a date -> record routine update with expected_by and wait until then.
- Firm confirms receipt -> complete with outcome records_received."""

    def offline_step(self, ctx: StepContext):
        e, c, m = ctx.engagement, ctx.contact, ctx.matter
        ch = ctx.preferred_channel()
        follow_up = (f"Hi, following up on our records request for {m.client_name} (incident {m.incident_date}). "
                     f"Could you share the current status and an expected release date?")
        if ctx.trigger == Trigger.CREATED:
            body = (f"Hello, this is the office of {m.owner} at our firm, calling about a records request for our client "
                    f"{m.client_name}, incident date {m.incident_date}. We need complete medical records and itemized bills "
                    f"for treatment from {e.params.get('date_range', 'the incident date onward')}. Could you tell me the status?")
            return [SendMessage(ch, body, "initial_request"),
                    RecordUpdate(f"Initial records request made to {c.name} via {ch}", "routine",
                                 {"stage": "requested"}),
                    WaitUntil(self.default_cadence_hours, "give the provider a few days")]

        if ctx.trigger == Trigger.SCHEDULED:
            return [SendMessage(ch, follow_up, "follow_up"),
                    WaitUntil(self.default_cadence_hours, "waiting for reply to follow-up")]

        if ctx.trigger == Trigger.CHANNEL_EVENT:
            return [RecordUpdate(f"Could not reach {c.name}: {ctx.payload.get('reason', 'delivery failed')}", "routine"),
                    WaitUntil(24, "retry tomorrow")]

        if ctx.trigger == Trigger.INBOUND:
            t = ctx.inbound_text
            if mentions(t, "fee", "invoice", "payment", "$", "charge", "cost"):
                amt = _amount(t)
                return [RecordUpdate(f"{c.name} requires a fee{f' of {amt}' if amt else ''} before releasing records",
                                     "meaningful", {"stage": "fee_requested", "fee_amount": amt}),
                        RequestHuman(f"{c.name} is asking for a records fee{f' of {amt}' if amt else ''}. "
                                     f"Should I approve payment?", ["Approve fee", "Decline", "Negotiate"], "normal")]
            if mentions(t, "authorization", "hipaa", "release form", "signed"):
                return [RecordUpdate(f"{c.name} needs a signed HIPAA authorization before release", "meaningful",
                                     {"stage": "needs_authorization"}),
                        RequestHuman(f"{c.name} needs a signed HIPAA authorization for {m.client_name}. "
                                     f"Please send it to them and tell me when done.", ["Sent authorization", "Close"], "normal")]
            if mentions(t, "sent", "mailed", "faxed", "uploaded", "released", "shipped", "on its way"):
                return [RecordUpdate(f"{c.name} says the records were sent", "meaningful", {"stage": "records_sent"}),
                        RequestHuman(f"{c.name} says {m.client_name}'s records have been sent. Can you confirm the firm received them?",
                                     ["Received, close", "Not received, follow up"], "normal")]
            if mentions(t, "processing", "pending", "working on", "in progress", "week", "days", "tomorrow", "queue", "backlog"):
                hours = parse_days(t, self.default_cadence_hours * 2)
                return [RecordUpdate(f"{c.name}: still processing, expected in about {int(hours // 24)} day(s)", "routine",
                                     {"stage": "processing", "expected_by": _iso_in(ctx, hours)}),
                        WaitUntil(hours, "provider gave a timeline")]
            return [RequestHuman(f"I couldn't interpret {c.name}'s reply: \"{t[:200]}\". How should I proceed?",
                                 ["Follow up again", "I'll handle it", "Close"], "normal")]

        if ctx.trigger == Trigger.HUMAN_DECISION:
            d = ctx.decision.lower()
            if d.startswith("approve fee"):
                return [SendMessage(ch, f"Thank you. The fee is approved; please proceed with the release and send the "
                                        f"invoice to our office. Could you confirm the expected release date?", "fee_approved"),
                        RecordUpdate("Firm approved the records fee", "routine", {"stage": "processing"}),
                        WaitUntil(self.default_cadence_hours, "fee approved; waiting for release")]
            if d.startswith("decline"):
                return [SendMessage(ch, "Thank you. We are unable to pay a fee for these records. Could you release them "
                                        "under the patient's right of access, or send an itemized justification?", "fee_declined"),
                        WaitUntil(self.default_cadence_hours, "asked provider to waive fee")]
            if d.startswith("negotiate"):
                return [RequestHuman("What fee amount should I propose?", ["Propose half", "I'll handle it"], "normal")]
            if d.startswith("received"):
                return [Complete("records_received", f"Records from {c.name} received by the firm.")]
            if d.startswith("not received"):
                return [SendMessage(ch, f"We have not yet received the records you sent for {m.client_name}. "
                                        f"Could you confirm the date, method and tracking if any?", "follow_up"),
                        WaitUntil(self.default_cadence_hours, "checking on shipment")]
            if d.startswith("sent authorization"):
                return [SendMessage(ch, f"The signed authorization for {m.client_name} has been sent to you. "
                                        f"Please proceed with the release and let us know the expected date.", "authorization_sent"),
                        RecordUpdate("Authorization sent to provider", "routine", {"stage": "processing"}),
                        WaitUntil(self.default_cadence_hours, "authorization sent; waiting")]
            if d.startswith("follow up again"):
                return [SendMessage(ch, follow_up, "follow_up"), WaitUntil(self.default_cadence_hours, "follow-up per firm")]
            shared = shared_human_decision(ctx, follow_up)
            if shared:
                return shared
            return [WaitUntil(self.default_cadence_hours, f"unrecognised decision '{ctx.decision}'")]

        if ctx.trigger == Trigger.FIRM_REQUEST:
            instr = ctx.payload.get("instruction", "")
            return [SendMessage(ch, f"Hi, following up on {m.client_name}'s records request. {instr}", "firm_request"),
                    WaitUntil(self.default_cadence_hours, "sent per firm instruction")]
        return [WaitUntil(self.default_cadence_hours, "no rule matched")]


def _amount(text: str) -> str | None:
    import re
    m = re.search(r"\$\s?(\d+(?:\.\d{2})?)", text)
    return f"${m.group(1)}" if m else None


def _iso_in(ctx: StepContext, hours: float) -> str:
    from datetime import timedelta
    return (ctx.now + timedelta(hours=hours)).date().isoformat()
