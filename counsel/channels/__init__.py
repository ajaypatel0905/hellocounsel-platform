"""Channels carry a SendMessage to a counterparty. One interface; delivery is the adapter's problem.

Email and SMS are simulated here (delivery is recorded, replies are injected through the
inbound webhook exactly as a real provider callback would). Voice is in voice.py and has a
simulated and a real (Twilio ConversationRelay) transport behind the same channel.
"""
from __future__ import annotations

from typing import Any, Protocol

from ..actions import SendMessage
from ..models import Contact, Engagement


class Channel(Protocol):
    name: str

    def send(self, engagement: Engagement, contact: Contact, action: SendMessage) -> dict[str, Any]:
        """Deliver (or start delivering). Returns the payload recorded on the timeline."""
        ...


class SimulatedMessagingChannel:
    """Stands in for SendGrid / Twilio SMS. Keeps an outbox so tests and the demo can see what went out."""

    def __init__(self, name: str):
        self.name = name
        self.outbox: list[dict[str, Any]] = []

    def send(self, engagement: Engagement, contact: Contact, action: SendMessage) -> dict[str, Any]:
        to = contact.email if self.name == "email" else contact.phone
        item = {"channel": self.name, "to": to, "body": action.body, "purpose": action.purpose,
                "engagement_id": engagement.id, "delivery": "simulated"}
        self.outbox.append(item)
        return item


class ChannelSet:
    def __init__(self, channels: list[Channel]):
        self._by_name = {c.name: c for c in channels}

    def get(self, name: str) -> Channel:
        if name not in self._by_name:
            raise KeyError(f"no channel adapter for '{name}'")
        return self._by_name[name]

    def names(self) -> list[str]:
        return list(self._by_name)
