"""Settings from .env and the one place that assembles store, clock, brain, channels, runtime."""
from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

from .brain import make_brain
from .calls import CallManager
from .channels import ChannelSet, SimulatedMessagingChannel
from .channels.voice import SimulatedVoiceTransport, TwilioConversationRelayTransport, VoiceChannel
from .clock import RealClock, SimClock
from .runtime import Runtime
from .store import Store


@dataclass
class Settings:
    brain: str = "scripted"
    clock: str = "sim"
    db_path: str = "counsel.db"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-opus-5"
    twilio_account_sid: str = ""
    twilio_key_sid: str = ""
    twilio_key_secret: str = ""
    twilio_from: str = ""
    public_base_url: str = ""

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        g = os.environ.get
        return cls(brain=g("BRAIN", "scripted"), clock=g("CLOCK", "sim"), db_path=g("DB_PATH", "counsel.db"),
                   gemini_api_key=g("GEMINI_API_KEY", ""), gemini_model=g("GEMINI_MODEL", "gemini-2.5-flash"),
                   anthropic_api_key=g("ANTHROPIC_API_KEY", ""), anthropic_model=g("ANTHROPIC_MODEL", "claude-opus-5"),
                   twilio_account_sid=g("TWILIO_ACCOUNT_SID", ""), twilio_key_sid=g("TWILIO_API_KEY_SID", ""),
                   twilio_key_secret=g("TWILIO_API_KEY_SECRET", ""), twilio_from=g("TWILIO_FROM_NUMBER", ""),
                   public_base_url=g("PUBLIC_BASE_URL", ""))

    @property
    def voice_is_real(self) -> bool:
        return all([self.twilio_account_sid, self.twilio_key_sid, self.twilio_key_secret, self.twilio_from, self.public_base_url])


@dataclass
class App:
    settings: Settings
    store: Store
    clock: RealClock | SimClock
    runtime: Runtime
    calls: CallManager
    channels: ChannelSet
    voice_transport: str
    email: SimulatedMessagingChannel
    sms: SimulatedMessagingChannel


def build(settings: Settings | None = None, store: Store | None = None) -> App:
    s = settings or Settings.from_env()
    store = store or Store(s.db_path)
    clock = SimClock() if s.clock == "sim" else RealClock()
    brain = make_brain(s.brain, gemini_api_key=s.gemini_api_key, gemini_model=s.gemini_model,
                       anthropic_api_key=s.anthropic_api_key or None, anthropic_model=s.anthropic_model)
    if s.voice_is_real:
        transport = TwilioConversationRelayTransport(s.twilio_account_sid, s.twilio_key_sid, s.twilio_key_secret,
                                                     s.twilio_from, s.public_base_url)
    else:
        transport = SimulatedVoiceTransport()
    email, sms = SimulatedMessagingChannel("email"), SimulatedMessagingChannel("sms")
    channels = ChannelSet([email, sms, VoiceChannel(store, clock, transport)])
    rt = Runtime(store, clock, brain, channels)
    return App(s, store, clock, rt, CallManager(rt), channels, transport.name, email, sms)
