"""The agent's vocabulary. This is the platform's contract with whatever brain drives it.

Every playbook, every channel and every brain speaks in these five actions. Adding a use
case never adds an action; if it did, that would be a sign the abstraction is wrong.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Union


@dataclass
class SendMessage:
    channel: str                 # email | sms | voice
    body: str                    # message text, or the opening line / talking points for a call
    purpose: str = "follow_up"   # initial_request | follow_up | check_in | reply | ...


@dataclass
class RecordUpdate:
    summary: str
    significance: str = "routine"          # routine | meaningful | urgent
    state: dict[str, Any] = field(default_factory=dict)   # merged into engagement.state


@dataclass
class RequestHuman:
    question: str
    options: list[str] = field(default_factory=list)
    urgency: str = "normal"                # low | normal | high


@dataclass
class WaitUntil:
    hours: float
    reason: str = ""


@dataclass
class Complete:
    outcome: str
    summary: str = ""


Action = Union[SendMessage, RecordUpdate, RequestHuman, WaitUntil, Complete]
TERMINAL = (RequestHuman, WaitUntil, Complete)

_BY_NAME = {
    "send_message": SendMessage,
    "record_update": RecordUpdate,
    "request_human": RequestHuman,
    "wait_until": WaitUntil,
    "complete": Complete,
}
_NAME_OF = {v: k for k, v in _BY_NAME.items()}


def action_name(a: Action) -> str:
    return _NAME_OF[type(a)]


def action_to_dict(a: Action) -> dict[str, Any]:
    return {"action": action_name(a), **asdict(a)}


def action_from_tool_call(name: str, args: dict[str, Any]) -> Action:
    cls = _BY_NAME[name]
    allowed = {f for f in cls.__dataclass_fields__}
    return cls(**{k: v for k, v in (args or {}).items() if k in allowed})


# JSON-schema tool specs. Provider-neutral; each brain adapts them to its SDK.
TOOL_SPECS: list[dict[str, Any]] = [
    {
        "name": "send_message",
        "description": (
            "Contact the counterparty on a channel. For email/sms, body is the full message. "
            "For voice, body is the opening line; the call is then held live, turn by turn, "
            "and its transcript comes back to you as an inbound message. Counts as one attempt."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "channel": {"type": "string", "enum": ["email", "sms", "voice"]},
                "body": {"type": "string"},
                "purpose": {"type": "string", "description": "short tag, e.g. initial_request, follow_up, check_in, reply"},
            },
            "required": ["channel", "body", "purpose"],
        },
    },
    {
        "name": "record_update",
        "description": (
            "Report something to the firm and update the engagement's structured state. "
            "significance: routine (logged only), meaningful (shows in the firm's feed), "
            "urgent (feed + flagged). Use the playbook's state fields."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "significance": {"type": "string", "enum": ["routine", "meaningful", "urgent"]},
                "state": {"type": "object", "description": "fields to merge into the engagement state"},
            },
            "required": ["summary", "significance"],
        },
    },
    {
        "name": "request_human",
        "description": (
            "Stop and ask someone at the firm. Use when blocked (fee, authorization, unclear reply), "
            "when a decision is theirs to make, or when asked something you must not answer "
            "(legal advice, case value). Ends this step; you resume when they answer."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {"type": "string"},
                "options": {"type": "array", "items": {"type": "string"}, "description": "2-4 short choices"},
                "urgency": {"type": "string", "enum": ["low", "normal", "high"]},
            },
            "required": ["question", "options"],
        },
    },
    {
        "name": "wait_until",
        "description": "Go to sleep for a number of hours. Ends this step. You will be woken earlier if a reply arrives.",
        "parameters": {
            "type": "object",
            "properties": {"hours": {"type": "number"}, "reason": {"type": "string"}},
            "required": ["hours"],
        },
    },
    {
        "name": "complete",
        "description": "The engagement's goal is met or the firm has taken it over. Ends the engagement.",
        "parameters": {
            "type": "object",
            "properties": {"outcome": {"type": "string"}, "summary": {"type": "string"}},
            "required": ["outcome"],
        },
    },
]
