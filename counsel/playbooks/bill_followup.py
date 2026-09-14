"""Third playbook, added to show what a new use case costs: this file, nothing else."""
from ..actions import Complete, RecordUpdate, RequestHuman, SendMessage, WaitUntil
from ..context import StepContext
from ..models import Trigger
from . import register
from .base import Playbook, mentions, parse_days, shared_human_decision


@register
class BillFollowUp(Playbook):
    key = "bill_followup"
    name = "Itemized bill follow-up"
    description = "Obtain the final itemized bill from a provider's billing department once treatment is complete."
    counterparty_kind = "provider"
    channels = ["email", "voice"]
    default_cadence_hours = 24 * 5
    max_unanswered_attempts = 3
    state_fields = {
        "stage": "requested | treatment_ongoing | bill_sent | received",
        "bill_total": "total billed amount if quoted",
        "expected_by": "date the billing office committed to",
    }
    instructions = """- Ask billing for the final itemized statement (CPT codes, dates of service, totals) for the client.
- If treatment is still ongoing, record that and wait ~2 weeks.
- If a total is quoted or the bill is sent, record a meaningful update and ask the firm to confirm receipt.
- Anything about balance collection from the client, liens or payment plans -> request_human."""

    def offline_step(self, ctx: StepContext):
        e, c, m = ctx.engagement, ctx.contact, ctx.matter
        ch = ctx.preferred_channel()
        ask = (f"Hello, we represent {m.client_name} (incident {m.incident_date}) and request the final itemized "
               f"statement for their treatment, with dates of service and totals. Could you share it or an expected date?")
        if ctx.trigger == Trigger.CREATED:
            return [SendMessage(ch, ask, "initial_request"),
                    RecordUpdate(f"Requested itemized bill from {c.name}", "routine", {"stage": "requested"}),
                    WaitUntil(self.default_cadence_hours)]
        if ctx.trigger == Trigger.SCHEDULED:
            return [SendMessage(ch, ask, "follow_up"), WaitUntil(self.default_cadence_hours)]
        if ctx.trigger == Trigger.INBOUND:
            t = ctx.inbound_text
            if mentions(t, "lien", "collection", "payment plan", "balance due", "patient responsibility"):
                return [RequestHuman(f"{c.name} raised a billing question that needs the firm: \"{t[:200]}\"",
                                     ["I'll handle it", "Close"], "normal")]
            if mentions(t, "still treating", "ongoing", "not final", "not yet complete", "still in treatment"):
                return [RecordUpdate("Treatment still ongoing; final bill not available", "routine", {"stage": "treatment_ongoing"}),
                        WaitUntil(24 * 14, "treatment ongoing")]
            if mentions(t, "$", "total", "attached", "sent", "mailed", "emailed"):
                import re
                amt = re.search(r"\$\s?([\d,]+(?:\.\d{2})?)", t)
                return [RecordUpdate(f"{c.name} sent the itemized bill{f' totalling ${amt.group(1)}' if amt else ''}", "meaningful",
                                     {"stage": "bill_sent", "bill_total": f"${amt.group(1)}" if amt else None}),
                        RequestHuman(f"{c.name} says the itemized bill was sent. Confirm receipt?",
                                     ["Received, close", "Not received, follow up"], "normal")]
            if mentions(t, "week", "days", "processing"):
                h = parse_days(t, self.default_cadence_hours)
                return [RecordUpdate("Billing office gave a timeline", "routine", {"stage": "requested"}), WaitUntil(h)]
            return [RequestHuman(f"Unclear reply from {c.name}: \"{t[:200]}\"", ["Follow up again", "I'll handle it"], "low")]
        if ctx.trigger == Trigger.HUMAN_DECISION:
            d = ctx.decision.lower()
            if d.startswith("received"):
                return [Complete("bill_received", f"Itemized bill from {c.name} received.")]
            if d.startswith("not received") or d.startswith("follow up again"):
                return [SendMessage(ch, ask, "follow_up"), WaitUntil(self.default_cadence_hours)]
            return shared_human_decision(ctx, ask) or [WaitUntil(self.default_cadence_hours)]
        if ctx.trigger == Trigger.FIRM_REQUEST:
            return [SendMessage(ch, f"Regarding {m.client_name}'s account: {ctx.payload.get('instruction', '')}", "firm_request"),
                    WaitUntil(self.default_cadence_hours)]
        return [WaitUntil(self.default_cadence_hours)]
