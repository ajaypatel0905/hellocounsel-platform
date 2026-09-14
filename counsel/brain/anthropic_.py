"""Claude brain: same contract, Anthropic SDK. Included to show the brain is swappable."""
from __future__ import annotations

import anthropic

from ..actions import TERMINAL, TOOL_SPECS, Action, action_from_tool_call
from ..context import CallContext, CallTurn, StepContext

MAX_TOOL_ROUNDS = 6


class AnthropicBrain:
    def __init__(self, api_key: str | None = None, model: str = "claude-opus-5"):
        self.client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()
        self.model = model
        self.name = f"anthropic:{model}"
        self._tools = [{"name": s["name"], "description": s["description"], "input_schema": s["parameters"]} for s in TOOL_SPECS]

    def decide(self, ctx: StepContext) -> list[Action]:
        actions: list[Action] = []
        messages = [{"role": "user", "content": ctx.render() + "\n\nDecide what to do for this step using the tools. "
                     "Finish with exactly one of wait_until / request_human / complete."}]
        for _ in range(MAX_TOOL_ROUNDS):
            resp = self.client.messages.create(model=self.model, max_tokens=4096, system=ctx.playbook.system_prompt(),
                                               tools=self._tools, messages=messages)
            messages.append({"role": "assistant", "content": resp.content})
            uses = [b for b in resp.content if b.type == "tool_use"]
            if not uses:
                break
            results, done = [], False
            for u in uses:
                a = action_from_tool_call(u.name, dict(u.input))
                actions.append(a)
                results.append({"type": "tool_result", "tool_use_id": u.id, "content": "ok"})
                if isinstance(a, TERMINAL):
                    done = True
            if done:
                break
            messages.append({"role": "user", "content": results})
        return actions

    def converse(self, ctx: CallContext, utterance: str) -> CallTurn:
        tool = {"name": "respond", "description": "Your next spoken turn on the call.",
                "input_schema": {"type": "object", "properties": {
                    "say": {"type": "string"}, "end_call": {"type": "boolean"}, "note": {"type": "string"}},
                    "required": ["say", "end_call"]}}
        resp = self.client.messages.create(
            model=self.model, max_tokens=512, system=ctx.playbook.call_prompt() + " Always answer by calling respond.",
            tools=[tool], messages=[{"role": "user", "content": ctx.render() + f"\n  them: {utterance}"}])
        for b in resp.content:
            if b.type == "tool_use":
                return CallTurn(say=b.input["say"], end_call=bool(b.input.get("end_call")), note=b.input.get("note", ""))
        text = "".join(b.text for b in resp.content if b.type == "text")
        return CallTurn(say=text or "Thank you, goodbye.", end_call=not text)

    def interpret_command(self, text: str, contacts, playbooks, schema: dict) -> dict:
        menu = "\n".join(f"- {c.id}: {c.name} ({c.kind}, matter {c.matter_id})" for c in contacts)
        pbs = "\n".join(f"- {p.key}: {p.name} (counterparty: {p.counterparty_kind})" for p in playbooks)
        tool = {"name": "plan", "description": "The interpreted command.", "input_schema": schema}
        resp = self.client.messages.create(
            model=self.model, max_tokens=512, tools=[tool],
            system="You map a law-firm staffer's instruction to a platform command. Always answer by calling plan.",
            messages=[{"role": "user", "content": f"Instruction: \"{text}\"\n\nContacts:\n{menu}\n\nPlaybooks:\n{pbs}"}])
        for b in resp.content:
            if b.type == "tool_use":
                return dict(b.input)
        return {"action": "unclear"}
