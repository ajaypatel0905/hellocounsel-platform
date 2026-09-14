"""Demo data: two matters, a handful of counterparties, and the engagements the firm assigned."""
from __future__ import annotations

from .models import Contact, Matter
from .runtime import Runtime
from .store import Store

DEMO_PHONE = "+15550100000"   # the verified trial number; every real call in the demo lands here


def seed(store: Store, rt: Runtime, create_engagements: bool = True) -> None:
    if store.list_matters():
        return
    m1 = store.put_matter(Matter("mat_sharma", "Priya Sharma", "Motor vehicle collision", "2026-06-02", "Rohan Mehta",
                                 notes="Rear-ended at a light. Neck and lower back. Treating at City General, PT at Sunrise."))
    m2 = store.put_matter(Matter("mat_reyes", "Daniel Reyes", "Premises liability (slip and fall)", "2026-07-15", "Aisha Khan",
                                 notes="Fell in a grocery store. Fractured wrist, ortho follow-ups."))
    c = {
        "priya": Contact("con_priya", "client", "Priya Sharma", m1.id, phone=DEMO_PHONE, email="priya.sharma@example.com",
                         preferred_channel="sms", notes="Prefers texts. Works shifts; evenings are best."),
        "citygen": Contact("con_citygen", "provider", "City General Hospital (Records)", m1.id, phone=DEMO_PHONE,
                           email="him@citygeneral.example", preferred_channel="voice",
                           notes="HIM department. Usually 10-15 business days. Charges per-page fees."),
        "sunrise": Contact("con_sunrise", "provider", "Sunrise Physical Therapy (Billing)", m1.id, phone=DEMO_PHONE,
                           email="billing@sunrisept.example", preferred_channel="email"),
        "daniel": Contact("con_daniel", "client", "Daniel Reyes", m2.id, phone=DEMO_PHONE, email="d.reyes@example.com",
                          preferred_channel="sms"),
        "mercy": Contact("con_mercy", "provider", "Mercy Orthopedics", m2.id, phone=DEMO_PHONE,
                         email="records@mercyortho.example", preferred_channel="email"),
    }
    for x in c.values():
        store.put_contact(x)
    if not create_engagements:
        return
    rt.create_engagement("medical_records", m1.id, c["citygen"].id,
                         "Obtain complete records and itemized bills for Priya Sharma from City General (2026-06-02 onward)",
                         m1.owner, {"date_range": "2026-06-02 to present"})
    rt.create_engagement("client_checkin", m1.id, c["priya"].id,
                         "Check in with Priya every two weeks through treatment; surface treatment changes and questions",
                         m1.owner, {"end_date": "2026-12-31"})
    rt.create_engagement("bill_followup", m1.id, c["sunrise"].id,
                         "Get the final itemized PT bill for Priya Sharma once discharged", m1.owner)
    rt.create_engagement("medical_records", m2.id, c["mercy"].id,
                         "Obtain ortho records and imaging for Daniel Reyes (2026-07-15 onward)", m2.owner,
                         {"date_range": "2026-07-15 to present"})
