"""Gemini brain: manual function-calling loop over the platform's tool specs."""
from __future__ import annotations

import copy
import json
import re
import time

from google import genai
from google.genai import errors, types

from ..actions import TERMINAL, TOOL_SPECS, Action, action_from_tool_call
from ..context import CallContext, CallTurn, StepContext

MAX_TOOL_ROUNDS = 6

CALL_TURN_SCHEMA = {
    "type": "object",
    "properties": {
        "say": {"type": "string", "description": "what to say next, 1-2 short sentences"},
        "end_call": {"type": "boolean", "description": "true if you have what you need or should hang up"},
        "note": {"type": "string", "description": "internal note for the case file, not spoken"},
    },
    "required": ["say", "end_call"],
}


def _gemini_schema(spec: dict) -> dict:
    """Gemini's function schema dislikes free-form objects; carry them as JSON strings."""
    s = copy.deepcopy(spec["parameters"])
    for k, p in s.get("properties", {}).items():
        if p.get("type") == "object" and not p.get("properties"):
            s["properties"][k] = {"type": "string", "description": (p.get("description", "") + " (JSON object as a string)").strip()}
    return s


class GeminiBrain:
    def __init__(self, api_key: str, model: str = "gemini-3.6-flash", call_model: str | None = None):
        self.client = genai.Client(api_key=api_key)
        self.model = model
        self.call_model = call_model or "gemini-flash-lite-latest"   # separate quota, lower latency on the phone
        self.name = f"gemini:{model}"
        self._tools = [types.Tool(function_declarations=[
            types.FunctionDeclaration(name=s["name"], description=s["description"], parameters_json_schema=_gemini_schema(s))
            for s in TOOL_SPECS])]

    def _generate(self, model: str | None = None, wait_budget: float = 60.0, **kwargs):
        """429/503 are routine on the free tier. Offline steps can wait for the quota window; a live
        call turn cannot, so callers pass a small wait_budget and fall back to a holding line."""
        waited, delay = 0.0, 1.5
        while True:
            try:
                return self.client.models.generate_content(model=model or self.model, **kwargs)
            except errors.APIError as ex:
                code = getattr(ex, "code", None)
                if code not in (429, 500, 503):
                    raise
                hint = re.search(r"retry in ([\d.]+)s", str(ex))
                sleep_for = min(float(hint.group(1)) + 1 if hint else delay, wait_budget - waited)
                if sleep_for <= 0:
                    raise
                time.sleep(sleep_for); waited += sleep_for; delay *= 2

    def decide(self, ctx: StepContext) -> list[Action]:
        actions: list[Action] = []
        contents = [types.Content(role="user", parts=[types.Part.from_text(
            text=ctx.render() + "\n\nDecide what to do for this step using the tools. "
                 "Finish with exactly one of wait_until / request_human / complete.")])]
        config = types.GenerateContentConfig(
            system_instruction=ctx.playbook.system_prompt(), tools=self._tools, temperature=0.2,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True))
        for _ in range(MAX_TOOL_ROUNDS):
            resp = self._generate(contents=contents, config=config)
            cand = resp.candidates[0]
            contents.append(cand.content)
            calls = resp.function_calls or []
            if not calls:
                break
            parts, done = [], False
            for fc in calls:
                args = dict(fc.args or {})
                for k, v in list(args.items()):
                    if k == "state" and isinstance(v, str):
                        try:
                            args[k] = json.loads(v)
                        except json.JSONDecodeError:
                            args[k] = {"note": v}
                a = action_from_tool_call(fc.name, args)
                actions.append(a)
                parts.append(types.Part.from_function_response(name=fc.name, response={"result": "ok"}))
                if isinstance(a, TERMINAL):
                    done = True
            if done:
                break
            contents.append(types.Content(role="tool", parts=parts))
        return actions

    def converse(self, ctx: CallContext, utterance: str) -> CallTurn:
        resp = self._generate(
            model=self.call_model, wait_budget=6.0,
            contents=ctx.render() + f"\n  them: {utterance}\n\nYour next turn:",
            config=types.GenerateContentConfig(system_instruction=ctx.playbook.call_prompt(), temperature=0.3,
                                               response_mime_type="application/json", response_json_schema=CALL_TURN_SCHEMA))
        d = json.loads(resp.text)
        return CallTurn(say=d["say"], end_call=bool(d.get("end_call")), note=d.get("note", ""))

    def interpret_command(self, text: str, contacts, playbooks, schema: dict) -> dict:
        menu = "\n".join(f"- {c.id}: {c.name} ({c.kind}, matter {c.matter_id})" for c in contacts)
        pbs = "\n".join(f"- {p.key}: {p.name} (counterparty: {p.counterparty_kind})" for p in playbooks)
        resp = self._generate(
            model=self.call_model, wait_budget=15.0,
            contents=f"Someone at the firm typed: \"{text}\"\n\nContacts:\n{menu}\n\nPlaybooks:\n{pbs}\n\n"
                     "Map it to an action (create a new engagement, nudge the running one, cancel it, or unclear), "
                     "the playbook, the contact_id, and the instruction for the agent in the firm's words.",
            config=types.GenerateContentConfig(temperature=0, response_mime_type="application/json", response_json_schema=schema))
        return json.loads(resp.text)
