# Design note

Companion to the code. README covers how to run it; this covers what I built, why, what I chose
not to build, and where it will break first.

## 1. The problem as I understood it

A personal injury case runs three to four months. Most of the work in that window is not clever, it
is persistent: call the hospital again, ask billing for the itemized statement again, find out whether
the client had the surgery, notice when something changes and tell the case manager. Nothing finishes
in one interaction. Replies arrive days later, often on a different channel than the one you used.
Some moments need a person: a records fee, a client asking what their case is worth, a provider who
has gone silent for three weeks.

So the platform question is not "how do I build a good records agent". It is: what is the smallest set
of primitives that lets a firm run many kinds of slow, interrupted, human-supervised agents, and add
a new kind without touching the runtime?

Constraints I took from the brief and the conversation: real APIs, not mocks, for the parts that
matter (voice, the model); two days; an engineer and a product person reviewing; extensibility is the
headline criterion.

## 2. The shape of the system

```
                      ┌─────────────────────────────────────────────────────────────┐
  triggers            │  Runtime.step(engagement, trigger, payload)                 │   effects
                      │                                                             │
  scheduled ─────────►│  1. load engagement, matter, contact, last 40 events        │──► channel.send()
  inbound ───────────►│  2. StepContext.render()  →  brain.decide()  →  [actions]   │──► event: update
  human_decision ────►│  3. for each action: policies.check_send() → apply          │──► intervention (blocked)
  firm_request ──────►│  4. exactly one terminal: wait_until | request_human |      │──► wakeup row (waiting)
  channel_event ─────►│     complete  (or default cadence if the brain forgot)      │──► status: completed
  created ───────────►│  5. persist Run(actions, error); persist engagement         │
                      └─────────────────────────────────────────────────────────────┘
```

Everything durable lives in five tables: `engagements`, `events`, `wakeups`, `interventions`, `runs`
(plus `matters`, `contacts`, `calls`). A worker claims due wake-ups and calls `step()`. The dashboard
reads the same tables. The Twilio websocket and the demo simulator both drive `CallManager`, which
appends to the `calls` table and, when the call ends, feeds the transcript back in as an inbound trigger.

## 3. Decisions, with the alternatives I rejected

**D1. An agent is a series of bounded steps over durable state, not a process.**
Alternative: a long-lived agent loop per engagement (LangGraph-style), checkpointed. Rejected because
the interesting time in this domain is the 72 hours between steps, not the 5 seconds inside one. A
loop that sleeps for three days is a liability: it holds memory, it dies on deploy, it needs a
checkpointer that becomes the real system anyway. Wake-up rows in a table are the checkpointer, and
they cost nothing while sleeping. Consequence: the brain has no in-memory state between steps; the
event log is its memory. That is a feature for auditability and a constraint for context size (see §7).

**D2. The brain speaks exactly five actions.** `send_message`, `record_update`, `request_human`,
`wait_until`, `complete`. Alternative: let each playbook define its own tools. Rejected because the
moment tools are per-playbook, the review queue, dashboard, policies and channels all need per-playbook
code, and "add a use case" stops being one file. The vocabulary is small on purpose: if a new use case
seems to need a sixth action, that is a design review, not a pull request. Consequence: playbook-specific
structure lives in `record_update.state` (a JSON object whose fields the playbook declares), not in new verbs.

**D3. Use-case knowledge lives only in playbooks.** A playbook declares counterparty kind, allowed
channels in preference order, cadence, the structured state fields, approval rules, sensitive terms, the
model's instructions, and an offline policy. Everything else is generic. Evidence: the third playbook
(itemized bill follow-up) was one file; a test defines a fourth inside the test body and runs it to
completion with no other change.

**D4. Guardrails are enforced by the runtime, not requested of the model.** The prompt says "don't
chase forever". The runtime does not trust that: it counts unanswered attempts and, on the attempt past
the playbook's limit, converts the send into an escalation for a person. Same for sign-off: the first
message to a client, or any message containing a playbook's sensitive terms, becomes an approval item
with an editable draft, whatever the model intended. Alternative: rely on prompting plus an LLM judge.
Rejected as the primary mechanism because a plaintiff firm cannot explain "the model usually complies"
to a client. Consequence: the model can be wrong and the platform still holds the line.

**D5. One human-in-the-loop primitive with three sources.** An `Intervention` is opened by the agent
(question), by policy (approval), or by the runtime (escalation: attempts exceeded, brain error, channel
misuse). All three land in the same queue with the same resolve endpoint. Resolving wakes the engagement
with the decision in its context; for approvals the runtime sends the approved (possibly edited) draft
itself before waking the agent. Inbound replies that arrive while blocked are held, not acted on: if a
person is in the loop, the person goes first. Alternative: separate approval and escalation systems.
Rejected because the paralegal has one queue, so the platform should too.

**D6. Voice is a channel, and the same brain talks on it.** Twilio ConversationRelay does speech-to-text
and text-to-speech and streams text over a websocket; we return text. Each callee utterance goes through
`brain.converse()` with the engagement context, and the transcript re-enters the runtime as an inbound
message. Alternative A: Gemini Live over Twilio Media Streams (raw audio). Rejected for this slice because
the phone agent becomes a second brain with its own state, and the platform loses the "one brain, many
channels" property. Alternative B: a hosted voice-agent vendor (Vapi, Retell). Rejected because it hides
exactly the part the brief is about. Consequence: call turns run on a lighter, faster model than
decisions, which is a knob the brain exposes.

**D7. Failures become interventions, not dead engagements.** A brain exception, a model quota
exhausted, a channel the playbook does not allow, a call that fails: each opens an escalation with a
Retry option that re-runs the failed trigger. Gemini specifically walks a configurable model ladder on
429/503 because free-tier quotas are per model per day. Alternative: retry loops and alerts. Included
where cheap (backoff with the server's retry hint), but the terminal state is always "a person can see
this", never "silently stuck".

**D8. A deterministic brain is a first-class implementation, not a mock.** `ScriptedBrain` delegates to
each playbook's offline policy. It makes the platform runnable with no key, makes tests deterministic
and sub-second, and proves the boundary: scripted and Gemini drive the runtime with zero runtime changes.
Consequence: the tests test the platform, not the model. Model quality needs a separate evaluation
harness (§8).

**D9. The clock is injectable.** `SimClock` is how a product reviewer walks three months in three
minutes, and how the test suite exercises a 3-attempt escalation without sleeping 18 days. It is not a
test hack; the dashboard's "advance time" button is a product feature for demos and training.

**D10. SQLite and one worker, on purpose.** The store is one class with JSON columns and a claim-once
wake-up query. It is enough for one firm on one box and it keeps the reviewer's setup to one command.
The path to Postgres and multiple workers is `SELECT ... FOR UPDATE SKIP LOCKED` on the same table
shape, or handing the step function to Temporal. The step function does not change.

## 4. The primitives

| Primitive | What it is | Invariants |
|---|---|---|
| Engagement | An agent assigned to one counterparty on one matter, with a goal, params, structured state, an unanswered-attempts counter and a status | Status ∈ {active, waiting, blocked, completed, cancelled}. Blocked ⇒ no pending wake-ups. |
| Trigger / Wakeup | "Run this engagement at time T because X", durable | Claimed exactly once. Cancelled when the engagement blocks or receives a reply. |
| Run | One brain decision and its applied actions, with error if any | One per trigger. Immutable. |
| Action | The five-verb vocabulary | Every step ends in exactly one terminal action; the runtime supplies a default if the brain does not. |
| Playbook | Declarative use case plus offline policy | The only per-use-case code. Registered by key. |
| Policy | Runtime checks on outbound actions | Runs before any send; can convert a send into approval or escalation. |
| Channel | Delivers a `send_message` | Returns a payload for the timeline; failures surface as `channel_event`. |
| Intervention | The human-in-the-loop item | Kinds: question, approval, escalation. Open ⇒ engagement blocked. |
| Event | Append-only timeline entry with actor ∈ {agent, human, counterparty, system} | The agent's memory, the firm's audit trail, the dashboard's source. |
| Update | An event carrying significance ∈ {routine, meaningful, urgent} and a state delta | Meaningful and urgent form the firm-wide feed. State delta merges into the engagement. |
| Command | Natural-language instruction from the firm | Resolves to create / nudge / cancel on a (playbook, contact) pair. |

## 5. Real vs simulated vs stand-in

| | Status | Notes |
|---|---|---|
| Runtime, scheduler, event log, policies, interventions | Real | Covered by tests. |
| Gemini brain | Real, default | Tool-calling steps; JSON-schema call turns; command parsing. Model ladder for quota. |
| Claude brain | Real, optional | Same interface, manual tool loop. Untested against a live key in this slice. |
| Voice | Real | Twilio ConversationRelay, outbound, status callbacks, live turn-taking. Verified with a live call from the platform. |
| Email, SMS | Simulated | Same channel interface; delivery recorded; replies enter through the real inbound endpoint. |
| Counterparty on voice without Twilio | Simulated | Typed lines drive the same `CallManager`. |
| Firm data | Seeded | Two matters, five contacts, four engagements. No CMS integration. |
| Scripted brain | Stand-in | Deterministic; exists for tests and no-key runs. Not the product. |

## 6. Where it breaks today

1. **Inbound routing by address is naive.** A reply is matched to the most recently touched live
   engagement for that phone or email. Two live engagements with the same provider on the same channel
   collide. Real fix: per-channel thread identifiers (email `Message-ID`/`References`, Twilio
   conversation SIDs) stored on the engagement at send time.
2. **No human takeover.** A person can answer the agent and edit drafts, but cannot join a live call or
   thread, speak as themselves, and hand it back with those turns in context.
3. **Context is the last 40 events.** A four-month engagement outgrows that. The fix is a rolling summary
   the agent maintains in its own state at the end of each step, and the renderer prefers it over raw events.
4. **Model output is trusted structurally, not semantically.** Tool arguments are schema-checked; the
   approval gate is term-based. Nothing verifies that an outbound message is truthful or on-brand. Needs
   an evaluation harness over recorded engagements and a second-model review on sensitive sends.
5. **Single writer.** SQLite under WAL handles the API and the worker, not a fleet. See D10.
6. **Free-tier model quotas.** Five requests a minute and twenty a day on the primary model. The ladder
   keeps a demo alive; production needs a paid tier and per-firm budgets.
7. **Voice is trial-grade.** No voicemail detection, no carrier-failure retry policy, the greeting is
   deliberately non-interruptible, and Twilio trial accounts only call verified numbers.
8. **No auth, no tenancy, no PHI controls** beyond the audit log. Out of scope for two days, not out of mind:
   every event already carries an actor, which is the hook for access control and redaction.

## 7. How a new use case fits

Say the firm wants an insurance adjuster follow-up: confirm the claim was received, capture the
adjuster's name and claim number, chase the liability decision.

```python
@register
class AdjusterFollowUp(Playbook):
    key = "adjuster_followup"; name = "Insurance adjuster follow-up"
    counterparty_kind = "insurer"; channels = ["voice", "email"]
    default_cadence_hours = 24 * 7; max_unanswered_attempts = 3
    approval_rules = ["sensitive_terms"]; sensitive_terms = ["demand", "settle", "offer", "policy limits"]
    state_fields = {"claim_number": "...", "adjuster": "...", "liability_decision": "pending | accepted | denied | partial"}
    instructions = "..."                      # what the model is told, on top of the platform prompt
    def offline_step(self, ctx): ...          # 10-20 lines so it runs and tests without a model
```

Register it and: the dashboard lists it, the command box routes "chase the adjuster on Priya's claim"
to it, its questions appear in the same review queue, the attempts guardrail and sensitive-terms gate
apply, and voice works because the counterparty prefers it. Runtime, scheduler, channels, store, API and
UI are untouched. If this use case genuinely needed a new *action* (say, "schedule a deposition"), that
is the moment to stop and ask whether the vocabulary is wrong, or whether it is really a `record_update`
plus a `request_human`.

## 8. What I would build next, in order

1. **Threaded inbound routing** on real email and SMS adapters. Removes the largest correctness gap.
2. **Human takeover and hand-back** on threads and live calls, with the human's turns entering the
   timeline as `actor=human` so the agent resumes with full context.
3. **Rolling engagement summaries** as agent-maintained state, so long timelines stay cheap and the
   dashboard shows a one-paragraph status without reading events.
4. **An evaluation harness**: recorded engagements replayed against a brain, scored on outcome and on
   whether the right moments reached a person. This is what makes model or prompt changes safe.
5. **Per-firm playbook configuration in the UI** (cadence, attempts, approval rules, sensitive terms),
   so a case manager tunes behaviour without a deploy.
6. **Postgres and leased workers**, then tenancy and auth, when more than one firm is on it.

## 9. Numbers from this slice

| | |
|---|---|
| Python, including tests | ~2,600 lines |
| Tests | 12, offline, under one second |
| Decision step (Gemini free tier, lite fallback) | 4 to 7 seconds |
| Live call turn (lite model) | 1 to 2 seconds |
| Third playbook | 1 file, 68 lines including its offline policy |
| Fourth playbook (in a test) | 12 lines |
| Voice cost | Twilio ConversationRelay $0.07/min + carrier |
