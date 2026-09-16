# Long-running agents for a plaintiff law firm

A working slice of a platform that runs agents over weeks and months: chasing medical providers for
records and bills, checking in with clients, and pulling a person in when the agent is blocked or
about to do something it should not decide alone.

**What is real:** the engagement runtime and scheduler, the append-only event log, runtime-enforced
guardrails, a review queue, three playbooks, a firm dashboard, a Gemini brain that makes every decision
and speaks every phone turn, and outbound voice calls through Twilio ConversationRelay with live
turn-taking and transcript capture. **What is simulated:** email and SMS delivery, and the counterparty
when Twilio is not configured. Details and reasoning in [DESIGN.md](DESIGN.md).

```
  firm dashboard ──► FastAPI ──► Runtime.step(engagement, trigger)
                                     │  build context from the event log
                                     │  brain.decide(ctx) ─► [actions]
                                     │  policies check each send
                                     ▼
                       ┌──── channel.send ──── Twilio / simulated ────┐
                       │     record update ──► event log, state       │
                       │     request human ──► intervention (blocked) │
                       │     wait until ─────► wakeup row             │
                       └──── complete                                 │
      inbound webhook / call transcript / human decision ─────────────┘  (next trigger)
```

## Run it

Requirements: Python 3.12 and [uv](https://docs.astral.sh/uv/). Everything else is `pip`-installable.

```bash
uv venv --python 3.12 .venv && uv pip install -e ".[dev]"
cp .env.example .env            # add GEMINI_API_KEY (free key: https://aistudio.google.com/apikey)
.venv/bin/uvicorn counsel.api:create_app --factory --port 8010
open http://localhost:8010
```

The dashboard seeds two matters and four engagements and runs their first step on startup. From there:
advance the simulated clock, answer the ringing call as the hospital clerk (typed, when Twilio is off),
reply as the client, resolve items in the review queue, or type an instruction into the command box.

```bash
.venv/bin/python scripts/demo.py     # narrated 3-month walkthrough in the terminal, same code paths
.venv/bin/python -m pytest -q        # 12 offline tests, < 1 second
```

No API key at hand? Set `BRAIN=scripted`. The platform then runs on each playbook's deterministic
offline policy, which is also what the tests use.

## Configuration

All configuration is environment variables, read once at startup from `.env`.

| Variable | Default | Effect |
|---|---|---|
| `BRAIN` | `gemini` | `gemini` \| `anthropic` \| `scripted`. Who decides and who talks on calls. |
| `GEMINI_API_KEY` | | Required for `BRAIN=gemini`. |
| `GEMINI_MODEL` | `gemini-3.6-flash` | Step decisions (tool calling). |
| `GEMINI_CALL_MODEL` | `gemini-flash-lite-latest` | Live call turns and command parsing. Lower latency, separate quota. |
| `GEMINI_FALLBACK_MODELS` | `gemini-3.5-flash-lite,gemini-flash-lite-latest,gemini-3-flash-preview` | Tried in order on 429/503. Free-tier quotas are per model, per day. |
| `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL` | , `claude-opus-5` | For `BRAIN=anthropic`. |
| `CLOCK` | `sim` | `sim`: the dashboard advances time and drains due work. `real`: a worker thread ticks every second. |
| `DB_PATH` | `counsel.db` | SQLite file. Use `:memory:` for throwaway runs. |
| `DEMO_PHONE` | `+15550100000` | Number the seeded clients and providers resolve to. On a Twilio trial it must be verified. |
| `TWILIO_ACCOUNT_SID`, `TWILIO_API_KEY_SID`, `TWILIO_API_KEY_SECRET`, `TWILIO_FROM_NUMBER`, `PUBLIC_BASE_URL` | | Set all five and voice calls are real. Any missing and the voice channel is simulated. |

### Real voice, locally

```bash
ngrok http 8010                                  # PUBLIC_BASE_URL = the https URL it prints
.venv/bin/uvicorn counsel.api:create_app --factory --port 8010
```

Any engagement whose counterparty prefers `voice` places a call on its first step. Twilio dials, plays
the agent's opening line, then streams the callee's speech as text to `wss://<PUBLIC_BASE_URL>/voice/relay/{call_id}`.
Each utterance goes through `brain.converse()`; the reply is spoken by Twilio. When the agent ends
the call or the callee hangs up, the transcript is appended to the engagement as an inbound message and the
agent runs its next step. Call status callbacks (`no-answer`, `busy`, `failed`) wake the agent with a
`channel_event` trigger instead.

Measured on the free tiers: a decision step takes 4 to 7 seconds, a phone turn 1 to 2 seconds. Twilio
ConversationRelay is $0.07/min plus carrier minutes.

## HTTP surface

| Method and path | Purpose |
|---|---|
| `GET /` | Dashboard. Deep links: `#view=review`, `#eng=<id>`. |
| `GET /api/status` | Brain, clock, voice transport, pending wake-ups, open reviews. |
| `GET /api/matters` | Matters with contacts, engagements, latest structured state and latest update. |
| `POST /api/engagements` | Assign an agent: `{playbook, matter_id, contact_id, goal, params}`. |
| `GET /api/engagements/{id}` | Full timeline, runs, calls, interventions. |
| `POST /api/engagements/{id}/nudge` | Firm instruction to a running engagement. |
| `POST /api/engagements/{id}/cancel` | Stop it. |
| `GET /api/interventions?status=open` | The review queue. |
| `POST /api/interventions/{id}/resolve` | `{decision, text?, edited_body?}`. Approvals send the (edited) draft, then wake the agent. |
| `GET /api/updates?significance=meaningful,urgent` | Firm-wide feed. |
| `POST /api/commands` | `{text, matter_id?}` → create, nudge, or cancel an engagement. |
| `POST /api/inbound` | Where an email/SMS provider webhook lands: `{channel, body, from_ \| engagement_id}`. |
| `POST /api/demo/advance` | `{hours}`. Sim clock only. Advances and drains due work. |
| `GET /api/demo/calls`, `POST /api/demo/calls/{id}/simulate` | Ringing calls; play the callee's lines against the live agent. |
| `WS /voice/relay/{call_id}`, `POST /voice/status/{call_id}`, `POST /voice/action/{call_id}` | Twilio ConversationRelay hooks. |

## Layout

```
counsel/
  actions.py        the five actions every brain, playbook and channel speak; JSON schemas for tools
  models.py         Matter, Contact, Engagement, Event, Wakeup, Intervention, Run, Call
  store.py          SQLite; JSON columns; claim-once wake-ups; append-only events
  clock.py          RealClock / SimClock
  context.py        StepContext and CallContext: what a brain sees, rendered for a model
  runtime.py        Runtime.step(): context → brain → policies → effects; inbound routing; interventions
  policies.py       guardrails applied to every outbound action
  calls.py          CallManager: live call sessions, transport-agnostic
  playbooks/        base.py + medical_records.py, client_checkin.py, bill_followup.py; registry
  brain/            scripted.py, gemini.py, anthropic_.py behind one Protocol
  channels/         simulated email/sms; voice.py with simulated and Twilio transports
  commands.py       natural-language instruction → create / nudge / cancel
  wiring.py         Settings from env; the one place components are assembled
  seed.py           demo matters, contacts, engagements
  api.py            FastAPI app factory
  ui/index.html     the dashboard (vanilla JS, no build step)
scripts/demo.py     narrated walkthrough
tests/              conftest.py, test_runtime.py
```

About 2,600 lines of Python including tests. Dependencies: fastapi, uvicorn, pydantic, python-dotenv,
twilio, google-genai, anthropic, pytest.

## Adding a use case

Write a `Playbook` subclass, register it, done. The runtime, scheduler, channels, review queue,
dashboard and command box pick it up. See DESIGN.md → "How a new use case fits" for a worked example,
and `tests/test_runtime.py::test_new_playbook_is_a_file_not_a_runtime_change` for a fourth playbook
defined and exercised inside a test.
