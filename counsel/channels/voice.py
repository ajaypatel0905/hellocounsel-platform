"""Voice channel. The engagement asks for a call; a transport places it; a CallSession (in
runtime.calls) holds the live conversation turn by turn using the same brain that drives the
rest of the platform. The transcript comes back to the engagement as an inbound message.
"""
from __future__ import annotations

import html
from typing import Any, Protocol

from ..actions import SendMessage
from ..clock import Clock
from ..models import Call, Contact, Engagement, new_id
from ..store import Store


class VoiceTransport(Protocol):
    name: str

    def place(self, call: Call, contact: Contact) -> str | None:
        """Start the call. Returns the provider's call id, or None for simulated."""
        ...


class SimulatedVoiceTransport:
    """No phone rings. The demo drives the conversation via POST /demo/calls/{id}/simulate."""
    name = "simulated"

    def place(self, call: Call, contact: Contact) -> str | None:
        return None


class TwilioConversationRelayTransport:
    """Real outbound call. Twilio does STT/TTS and streams text to our websocket (/voice/relay/{call_id})."""
    name = "twilio_conversation_relay"

    def __init__(self, account_sid: str, key_sid: str, key_secret: str, from_number: str, public_base_url: str,
                 tts_provider: str = "Google", voice: str = "en-US-Journey-F"):
        from twilio.rest import Client
        self._client = Client(key_sid, key_secret, account_sid)
        self._from = from_number
        self._base = public_base_url.rstrip("/")
        self._tts_provider, self._voice = tts_provider, voice

    def twiml(self, call: Call) -> str:
        wss = self._base.replace("https://", "wss://") + f"/voice/relay/{call.id}"
        greeting = html.escape(call.opening, quote=True)
        return (
            "<Response>"
            f"<Connect action=\"{self._base}/voice/action/{call.id}\">"
            f"<ConversationRelay url=\"{wss}\" welcomeGreeting=\"{greeting}\" welcomeGreetingInterruptible=\"none\" "
            f"language=\"en-US\" ttsProvider=\"{self._tts_provider}\" voice=\"{self._voice}\" "
            "transcriptionProvider=\"Deepgram\" interruptible=\"speech\" />"
            "</Connect></Response>"
        )

    def place(self, call: Call, contact: Contact) -> str | None:
        c = self._client.calls.create(
            to=contact.phone, from_=self._from, twiml=self.twiml(call),
            status_callback=f"{self._base}/voice/status/{call.id}",
            status_callback_event=["initiated", "ringing", "answered", "completed"],
            status_callback_method="POST",
        )
        return c.sid


class VoiceChannel:
    name = "voice"

    def __init__(self, store: Store, clock: Clock, transport: VoiceTransport):
        self.store, self.clock, self.transport = store, clock, transport

    def send(self, engagement: Engagement, contact: Contact, action: SendMessage) -> dict[str, Any]:
        now = self.clock.now()
        for stale in self.store.list_calls(engagement.id):   # a call nobody answered before the next attempt
            if stale.ended_at is None and stale.status in ("placing", "ringing", "simulated_ringing", "in-progress"):
                stale.status, stale.ended_at = "no-answer", now
                self.store.put_call(stale)
        call = Call(id=new_id("call"), engagement_id=engagement.id, to=contact.phone, purpose=action.purpose,
                    opening=action.body, status="placing", created_at=now)
        self.store.put_call(call)
        try:
            sid = self.transport.place(call, contact)
            call.provider_sid, call.status = sid, ("ringing" if sid else "simulated_ringing")
        except Exception as ex:  # provider rejected the call; the runtime turns this into a channel_event later
            call.status = "failed"
            self.store.put_call(call)
            return {"channel": "voice", "to": contact.phone, "call_id": call.id, "purpose": action.purpose,
                    "opening": action.body, "delivery": self.transport.name, "error": str(ex)}
        self.store.put_call(call)
        return {"channel": "voice", "to": contact.phone, "call_id": call.id, "provider_sid": sid,
                "purpose": action.purpose, "opening": action.body, "delivery": self.transport.name}
