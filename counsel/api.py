"""HTTP surface: firm dashboard API, inbound webhooks, review queue, demo controls, Twilio voice hooks."""
from __future__ import annotations

import json
import os
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import asdict, is_dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

import base64
import secrets

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel

from . import playbooks
from .clock import SimClock
from .commands import CommandService
from .models import Contact, EngagementStatus as S, Matter, new_id
from .seed import seed
from .wiring import App, build

UI = Path(__file__).parent / "ui" / "index.html"


class NewMatter(BaseModel):
    client_name: str; case_type: str; incident_date: str; owner: str; notes: str = ""; client_dob: str = ""


class NewContact(BaseModel):
    matter_id: str; kind: str; name: str; phone: str = ""; email: str = ""; preferred_channel: str = "email"; notes: str = ""


class NewEngagement(BaseModel):
    playbook: str; matter_id: str; contact_id: str; goal: str; params: dict = {}


class Nudge(BaseModel):
    instruction: str


class Resolve(BaseModel):
    decision: str; text: str = ""; edited_body: str | None = None; by: str = "dashboard"


class Command(BaseModel):
    text: str; matter_id: str | None = None


class Inbound(BaseModel):
    channel: str; body: str; from_: str | None = None; engagement_id: str | None = None
    model_config = {"populate_by_name": True}


class Advance(BaseModel):
    hours: float


class Simulate(BaseModel):
    utterances: list[str]



def jsonable(o: Any) -> Any:
    if is_dataclass(o):
        return jsonable(asdict(o))
    if isinstance(o, dict):
        return {k: jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [jsonable(v) for v in o]
    if isinstance(o, datetime):
        return o.isoformat(timespec="minutes")
    if isinstance(o, Enum):
        return o.value
    return o


def create_app(app: App | None = None, do_seed: bool = True) -> FastAPI:
    ctx = app or build()
    if do_seed:
        seed(ctx.store, ctx.runtime)
    commands = CommandService(ctx.runtime)
    stop = threading.Event()

    def worker():
        while not stop.is_set():
            try:
                ctx.runtime.tick()
            except Exception as ex:  # keep the loop alive; the failing step already escalated
                print("worker error:", ex)
            time.sleep(1)

    @asynccontextmanager
    async def lifespan(_):
        # The worker runs in both clock modes: with a sim clock nothing becomes due on its own, but real
        # inbound events (a Twilio call ending) still need to be processed promptly. The initial drain
        # runs there too, so the server is reachable while the seeded engagements take their first step.
        threading.Thread(target=worker, daemon=True).start()
        yield
        stop.set()

    api = FastAPI(title="HelloCounsel long-running agents", lifespan=lifespan)
    api.state.ctx = ctx
    st, rt = ctx.store, ctx.runtime

    if ctx.settings.dashboard_password:
        expected = base64.b64encode(f"{ctx.settings.dashboard_user}:{ctx.settings.dashboard_password}".encode()).decode()

        @api.middleware("http")
        async def basic_auth(request: Request, call_next):
            if request.url.path.startswith("/voice/"):          # Twilio callbacks carry no credentials
                return await call_next(request)
            header = request.headers.get("authorization", "")
            if header.startswith("Basic ") and secrets.compare_digest(header[6:], expected):
                return await call_next(request)
            return Response("Sign in to the firm dashboard", status_code=401,
                            headers={"WWW-Authenticate": 'Basic realm="Agent Desk"'})

    # ----------------------------------------------------------------- views
    def eng_view(e) -> dict:
        c, m = st.get_contact(e.contact_id), st.get_matter(e.matter_id)
        updates = [ev for ev in st.list_events(e.id) if ev.type == "update"]
        opens = st.list_interventions(status="open", engagement_id=e.id)
        return {**jsonable(e), "contact": jsonable(c), "matter_client": m.client_name if m else None,
                "playbook_name": playbooks.get(e.playbook).name,
                "latest_update": jsonable(updates[-1].payload | {"ts": updates[-1].ts}) if updates else None,
                "open_intervention": jsonable(opens[0]) if opens else None}

    def intervention_view(i) -> dict:
        e = st.get_engagement(i.engagement_id); c = st.get_contact(e.contact_id); m = st.get_matter(e.matter_id)
        return {**jsonable(i), "contact_name": c.name, "matter_client": m.client_name, "matter_id": m.id,
                "playbook_name": playbooks.get(e.playbook).name, "engagement_goal": e.goal}

    @api.get("/", response_class=HTMLResponse)
    def index():
        return UI.read_text()

    @api.get("/api/status")
    def status():
        return {"brain": rt.brain.name, "clock": "sim" if isinstance(ctx.clock, SimClock) else "real",
                "now": jsonable(ctx.clock.now()), "voice_transport": ctx.voice_transport,
                "demo_phone": os.environ.get("DEMO_PHONE", ""),
                "pending_wakeups": st.count_pending_wakeups(), "open_reviews": len(st.list_interventions(status="open")),
                "channels": ctx.channels.names()}

    @api.get("/api/playbooks")
    def list_playbooks():
        return [{"key": p.key, "name": p.name, "description": p.description, "counterparty_kind": p.counterparty_kind,
                 "channels": p.channels, "default_cadence_hours": p.default_cadence_hours,
                 "max_unanswered_attempts": p.max_unanswered_attempts, "approval_rules": p.approval_rules,
                 "state_fields": p.state_fields} for p in playbooks.all_playbooks()]

    @api.get("/api/matters")
    def list_matters():
        out = []
        for m in st.list_matters():
            engs = st.list_engagements(matter_id=m.id)
            out.append({**jsonable(m), "engagements": [eng_view(e) for e in engs],
                        "contacts": jsonable(st.list_contacts(m.id)),
                        "needs_review": sum(1 for e in engs if e.status == S.BLOCKED)})
        return out

    @api.post("/api/matters")
    def create_matter(body: NewMatter):
        m = st.put_matter(Matter(new_id("mat"), body.client_name, body.case_type, body.incident_date, body.owner,
                                 body.notes, body.client_dob))
        return jsonable(m)

    @api.post("/api/contacts")
    def create_contact(body: NewContact):
        if not st.get_matter(body.matter_id):
            raise HTTPException(404, "matter")
        if body.kind not in ("client", "provider", "insurer"):
            raise HTTPException(400, "kind must be client, provider or insurer")
        if not (body.phone or body.email):
            raise HTTPException(400, "a phone or an email is required")
        c = st.put_contact(Contact(new_id("con"), body.kind, body.name, body.matter_id, body.phone, body.email,
                                   body.preferred_channel, body.notes))
        return jsonable(c)

    @api.get("/api/engagements/{eid}")
    def get_engagement(eid: str):
        e = st.get_engagement(eid)
        if not e:
            raise HTTPException(404)
        return {**eng_view(e), "matter": jsonable(st.get_matter(e.matter_id)), "events": jsonable(st.list_events(e.id)),
                "interventions": jsonable(st.list_interventions(engagement_id=e.id)), "runs": jsonable(st.list_runs(e.id)),
                "calls": jsonable(st.list_calls(e.id)), "playbook": next(p for p in list_playbooks() if p["key"] == e.playbook)}


    @api.post("/api/engagements")
    def create_engagement(body: NewEngagement):
        m = st.get_matter(body.matter_id)
        if not m:
            raise HTTPException(404, "matter")
        try:
            e = rt.create_engagement(body.playbook, body.matter_id, body.contact_id, body.goal, m.owner, body.params)
        except (KeyError, ValueError) as ex:
            raise HTTPException(400, str(ex))
        rt.drain()
        return eng_view(st.get_engagement(e.id))


    @api.post("/api/engagements/{eid}/nudge")
    def nudge(eid: str, body: Nudge):
        try:
            rt.nudge(eid, body.instruction)
        except (KeyError, ValueError) as ex:
            raise HTTPException(400, str(ex))
        rt.drain()
        return eng_view(st.get_engagement(eid))

    @api.post("/api/engagements/{eid}/cancel")
    def cancel(eid: str):
        rt.cancel(eid, "cancelled from dashboard")
        return eng_view(st.get_engagement(eid))

    @api.get("/api/interventions")
    def list_interventions(status: str = "open"):
        items = st.list_interventions(status=None if status == "all" else status)
        return [intervention_view(i) for i in items]


    @api.post("/api/interventions/{iid}/resolve")
    def resolve(iid: str, body: Resolve):
        try:
            e = rt.resolve_intervention(iid, body.decision, body.text, body.edited_body, body.by)
        except ValueError as ex:
            raise HTTPException(409, str(ex))
        rt.drain()
        return eng_view(st.get_engagement(e.id))

    @api.get("/api/updates")
    def updates(significance: str = "meaningful,urgent", limit: int = 50):
        want = set(significance.split(","))
        out = []
        for ev in st.list_events_by_type("update", limit=400):
            if ev.payload.get("significance") not in want:
                continue
            e = st.get_engagement(ev.engagement_id); c = st.get_contact(e.contact_id); m = st.get_matter(e.matter_id)
            out.append({**jsonable(ev), "contact_name": c.name, "matter_client": m.client_name, "matter_id": m.id,
                        "playbook_name": playbooks.get(e.playbook).name})
            if len(out) >= limit:
                break
        return out


    @api.post("/api/commands")
    def command(body: Command):
        r = commands.run(body.text, body.matter_id)
        rt.drain()
        return jsonable(r)


    @api.post("/api/inbound")
    def inbound(body: Inbound):
        """Where a real email/SMS provider's webhook would land. The dashboard uses it to simulate replies."""
        e = rt.handle_inbound(body.channel, body.body, address=body.from_, engagement_id=body.engagement_id)
        if not e:
            raise HTTPException(404, "no live engagement for that address")
        rt.drain()
        return eng_view(st.get_engagement(e.id))

    # ------------------------------------------------------------ demo/sim

    @api.post("/api/demo/advance")
    def advance(body: Advance):
        if not isinstance(ctx.clock, SimClock):
            raise HTTPException(400, "clock is real; nothing to advance")
        ctx.clock.advance(hours=body.hours)
        n = rt.drain()
        return {"now": jsonable(ctx.clock.now()), "steps_run": n}

    @api.post("/api/tick")
    def tick():
        return {"steps_run": rt.drain()}

    @api.get("/api/demo/calls")
    def pending_calls():
        out = []
        for e in st.list_engagements(status=(S.WAITING, S.ACTIVE)):
            for c in st.list_calls(e.id):
                if c.status in ("simulated_ringing", "ringing", "placing"):
                    out.append({**jsonable(c), "contact_name": st.get_contact(e.contact_id).name})
        return out


    @api.post("/api/demo/calls/{call_id}/simulate")
    def simulate_call(call_id: str, body: Simulate):
        if not st.get_call(call_id):
            raise HTTPException(404)
        transcript = ctx.calls.simulate(call_id, body.utterances)
        rt.drain()
        return {"transcript": transcript}

    # -------------------------------------------------------- twilio voice
    @api.websocket("/voice/relay/{call_id}")
    async def relay(ws: WebSocket, call_id: str):
        await ws.accept()
        try:
            while True:
                msg = json.loads(await ws.receive_text())
                t = msg.get("type")
                if t == "setup":
                    ctx.calls.mark_status(call_id, "in-progress", msg.get("callSid"))
                elif t == "prompt" and msg.get("last", True):
                    turn = await run_in_threadpool(ctx.calls.on_utterance, call_id, msg.get("voicePrompt", ""))
                    await ws.send_text(json.dumps({"type": "text", "token": turn.say, "last": True}))
                    if turn.end_call:
                        await ws.send_text(json.dumps({"type": "end", "handoffData": json.dumps({"reason": "done"})}))
                elif t == "error":
                    print("relay error:", msg.get("description"))
        except WebSocketDisconnect:
            pass
        finally:
            await run_in_threadpool(ctx.calls.finish, call_id, "completed")
            rt.drain()

    @api.post("/voice/status/{call_id}")
    async def voice_status(call_id: str, req: Request):
        form = await req.form()
        ctx.calls.mark_status(call_id, form.get("CallStatus", ""), form.get("CallSid"))
        rt.drain()
        return Response("")

    @api.post("/voice/action/{call_id}")
    async def voice_action(call_id: str, req: Request):
        ctx.calls.finish(call_id, "completed")
        rt.drain()
        return Response("<Response><Hangup/></Response>", media_type="text/xml")

    return api

