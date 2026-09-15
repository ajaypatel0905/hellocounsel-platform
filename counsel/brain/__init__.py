"""The brain decides; the runtime acts. Two methods, three implementations, one contract."""
from __future__ import annotations

from typing import Protocol

from ..actions import Action
from ..context import CallContext, CallTurn, StepContext


class Brain(Protocol):
    name: str

    def decide(self, ctx: StepContext) -> list[Action]: ...
    def converse(self, ctx: CallContext, utterance: str) -> CallTurn: ...


def make_brain(kind: str, **cfg) -> Brain:
    if kind == "scripted":
        from .scripted import ScriptedBrain
        return ScriptedBrain()
    if kind == "gemini":
        from .gemini import GeminiBrain
        return GeminiBrain(api_key=cfg["gemini_api_key"], model=cfg.get("gemini_model") or "gemini-3.6-flash",
                           call_model=cfg.get("gemini_call_model") or None,
                           fallbacks=[m.strip() for m in (cfg.get("gemini_fallback_models") or "").split(",") if m.strip()] or None)
    if kind == "anthropic":
        from .anthropic_ import AnthropicBrain
        return AnthropicBrain(api_key=cfg.get("anthropic_api_key"), model=cfg.get("anthropic_model") or "claude-opus-5")
    raise ValueError(f"unknown brain '{kind}'")
