"""Narrated end-to-end walkthrough with a simulated clock. No API keys needed.

    .venv/bin/python scripts/demo.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from counsel.models import EngagementStatus as S  # noqa: E402
from counsel.seed import seed  # noqa: E402
from counsel.wiring import Settings, build  # noqa: E402

app = build(Settings(brain="scripted", clock="sim", db_path=":memory:"))
seed(app.store, app.runtime, create_engagements=False)
st, rt, clock = app.store, app.runtime, app.clock


def say(title):
    print(f"\n=== {title}  [sim time {clock.now().isoformat(timespec='minutes')}]")


def show(e_id):
    e = st.get_engagement(e_id)
    print(f"  status={e.status.value:9} attempts={e.unanswered_attempts} state={e.state}")
    for i in st.list_interventions(status="open", engagement_id=e.id):
        print(f"  REVIEW NEEDED [{i.kind.value}] {i.question}\n     options: {i.options}")


def ring():
    for e in st.list_engagements(status=(S.WAITING, S.ACTIVE)):
        for c in st.list_calls(e.id):
            if c.status == "simulated_ringing":
                return c
    return None


say("Firm assigns the agent: chase City General for Priya's records (provider prefers phone)")
rec = rt.create_engagement("medical_records", "mat_sharma", "con_citygen", "Get Priya Sharma's records and bills", "Rohan Mehta")
rt.drain(); show(rec.id)
c = ring(); print(f"  -> agent placed a call. Opening line: \"{c.opening[:90]}...\"")

say("Nobody picks up. Three days pass, the agent tries again")
app.calls.mark_status(c.id, "no-answer"); rt.drain()
clock.advance(days=3); rt.drain(); show(rec.id)

say("This time the HIM clerk answers and quotes a fee")
c = ring()
t = app.calls.simulate(c.id, ["Yes we have it. There's a $45 fee before we can release anything.", "Probably five business days after payment."])
for turn in t:
    print(f"  {turn['role']:>6}: {turn['text']}")
rt.drain(); show(rec.id)

say("Paralegal approves the fee from the review queue")
[i] = st.list_interventions(status="open", engagement_id=rec.id)
rt.resolve_intervention(i.id, "Approve fee", by="Rohan Mehta"); rt.drain(); show(rec.id)
c = ring(); print(f"  -> agent calls back to confirm: \"{c.opening[:80]}...\"")
app.calls.simulate(c.id, ["Got it, we'll process it. Should go out in about 5 days."]); rt.drain(); show(rec.id)

say("A week later the records are mailed; agent asks the firm to confirm receipt")
clock.advance(days=7); rt.drain()
c = ring()
if c:
    app.calls.simulate(c.id, ["Those were mailed to your office on Tuesday."]); rt.drain()
show(rec.id)
[i] = st.list_interventions(status="open", engagement_id=rec.id)
rt.resolve_intervention(i.id, "Received, close", by="Rohan Mehta"); rt.drain(); show(rec.id)

say("Meanwhile: client check-in. First message to a client needs sign-off (policy, not the model)")
chk = rt.create_engagement("client_checkin", "mat_sharma", "con_priya", "Fortnightly check-in through treatment", "Rohan Mehta")
rt.drain(); show(chk.id)
[i] = st.list_interventions(status="open", engagement_id=chk.id)
rt.resolve_intervention(i.id, "Approve", edited_body="Hi Priya, Rohan's office here. How are you feeling this week? Anything change with treatment?")
rt.drain(); print(f"  -> sms sent: \"{app.sms.outbox[-1]['body']}\"")

say("Priya replies with a case question. Agent deflects, records it, and hands it to the firm")
rt.handle_inbound("sms", "Doing ok. The insurance adjuster called me, should I talk to them? And how much is my case worth?", engagement_id=chk.id)
rt.drain(); print(f"  -> agent replied: \"{app.sms.outbox[-1]['body']}\""); show(chk.id)

say("Guardrail demo: billing office ignores email three times; runtime escalates instead of chasing forever")
bill = rt.create_engagement("bill_followup", "mat_sharma", "con_sunrise", "Final itemized PT bill", "Rohan Mehta"); rt.drain()
for _ in range(3):
    clock.advance(days=6); rt.drain()
show(bill.id)

say("Firm-wide feed of meaningful updates")
for ev in reversed(st.list_events_by_type("update")):
    if ev.payload["significance"] != "routine":
        print(f"  [{ev.payload['significance']:10}] {ev.payload['summary']}")

say("Command box: 'check in with Daniel this week'")
from counsel.commands import CommandService  # noqa: E402
print("  ->", CommandService(rt).run("check in with Daniel this week", matter_id="mat_reyes").message)
print("\nDone.")
