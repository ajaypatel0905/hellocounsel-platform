# Long-running agents for a plaintiff law firm

A working slice of a platform that runs agents over weeks and months: chasing medical providers
for records and bills, checking in with clients, and pulling a person in when the agent is blocked.

Two shipped use cases (medical records follow-up, client check-in), a third small one (itemized bill
follow-up) that exists to show what adding a use case costs, a real voice channel over Twilio
ConversationRelay, simulated email/SMS on the same interface, a review queue, and a firm dashboard.

Read [DESIGN.md](DESIGN.md) for the reasoning. This file is how to run it.

## Run it (no API keys needed)

```bash
uv venv --python 3.12 .venv && uv pip install -e ".[dev]"
cp .env.example .env                 # defaults: scripted brain, simulated clock, simulated channels
.venv/bin/uvicorn counsel.api:create_app --factory --port 8010
open http://localhost:8010
```

The dashboard seeds two matters and four engagements. Use the demo controls to advance the
simulated clock, answer the ringing call as the hospital clerk, reply as the client, and resolve
items in the review queue.

Narrated walkthrough in the terminal, same code paths:

```bash
.venv/bin/python scripts/demo.py
```

Tests (runtime, guardrails, review path, a fourth playbook defined inside a test):

```bash
.venv/bin/python -m pytest -q
```

## Turn on the real pieces

Everything is a flag in `.env`.

| Setting | Values | What changes |
|---|---|---|
| `BRAIN` | `scripted` (default), `gemini`, `anthropic` | Who decides. Scripted is deterministic and free. |
| `CLOCK` | `sim` (default), `real` | `sim` lets the dashboard move time. `real` runs a background worker that ticks every second. |
| `TWILIO_*` + `PUBLIC_BASE_URL` | set all five | Voice calls become real outbound calls via ConversationRelay. Unset, the voice channel is simulated. |

Real voice, locally:

```bash
ngrok http 8010                       # copy the https URL into PUBLIC_BASE_URL
# .env: CLOCK=real, BRAIN=gemini, GEMINI_API_KEY=..., TWILIO_ACCOUNT_SID/API_KEY_SID/API_KEY_SECRET/FROM_NUMBER, PUBLIC_BASE_URL=https://....ngrok-free.dev
.venv/bin/uvicorn counsel.api:create_app --factory --port 8010
```

Then assign a records follow-up to a provider whose preferred channel is `voice`. The phone rings,
Twilio streams speech as text to `/voice/relay/{call_id}`, the same brain answers turn by turn, and
the transcript lands on the engagement timeline as an inbound reply. On a Twilio trial the callee
must be a verified number.

## Layout

```
counsel/
  actions.py      the five actions every brain, playbook and channel speak
  models.py       Matter, Contact, Engagement, Event, Wakeup, Intervention, Run, Call
  store.py        SQLite persistence (append-only events, claim-once wakeups)
  runtime.py      one bounded step per trigger; applies actions through policies
  policies.py     runtime guardrails: attempts limit, approval gates
  calls.py        live call sessions, transport-agnostic
  playbooks/      base + medical_records, client_checkin, bill_followup
  brain/          scripted, gemini, anthropic  (decide + converse)
  channels/       simulated email/sms; voice with simulated and Twilio transports
  commands.py     "chase City General for the bill" -> create or nudge an engagement
  api.py          FastAPI: dashboard API, inbound webhook, review queue, Twilio hooks
  ui/index.html   the firm dashboard
scripts/demo.py   narrated end-to-end run
tests/            11 tests, all offline
```
