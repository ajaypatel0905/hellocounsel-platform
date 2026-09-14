"""Deterministic stand-in for the model. Delegates to the playbook's offline policy.

Exists so the platform can be run, demoed and tested with no API key and no variance.
If the platform works with this and with a real model unchanged, the boundary is right.
"""
from ..actions import Action
from ..context import CallContext, CallTurn, StepContext


class ScriptedBrain:
    name = "scripted"

    def decide(self, ctx: StepContext) -> list[Action]:
        return ctx.playbook.offline_step(ctx)

    def converse(self, ctx: CallContext, utterance: str) -> CallTurn:
        return ctx.playbook.offline_call_turn(ctx, utterance)
