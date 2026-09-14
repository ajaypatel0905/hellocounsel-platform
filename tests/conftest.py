import pytest

from counsel.models import Contact, Matter
from counsel.wiring import Settings, build


@pytest.fixture
def app():
    a = build(Settings(brain="scripted", clock="sim", db_path=":memory:"))
    s = a.store
    s.put_matter(Matter("m1", "Priya Sharma", "MVC", "2026-06-02", "Rohan Mehta"))
    s.put_contact(Contact("client", "client", "Priya Sharma", "m1", phone="+15550001111", email="priya@example.com", preferred_channel="sms"))
    s.put_contact(Contact("hospital", "provider", "City General Hospital", "m1", phone="+15550002222", email="him@cg.example", preferred_channel="voice"))
    s.put_contact(Contact("billing", "provider", "Sunrise PT Billing", "m1", phone="+15550003333", email="billing@spt.example", preferred_channel="email"))
    return a


def open_interventions(app, eng_id=None):
    return app.store.list_interventions(status="open", engagement_id=eng_id)


def pending_call(app, eng_id):
    calls = [c for c in app.store.list_calls(eng_id) if c.status in ("simulated_ringing", "ringing")]
    assert calls, "expected a call to be ringing"
    return calls[-1]
