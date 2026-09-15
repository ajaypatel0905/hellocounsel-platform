# Design note

## The problem, as I understood it

A personal injury case runs three to four months. For most of that time the work is not clever, it
is persistent: call the hospital again, ask billing for the itemized statement again, check whether
the client had the surgery, notice when something changes and tell the case manager. Nothing
finishes in one interaction. Replies arrive days later on a different channel. Sometimes the agent
hits something only a person can decide: a records fee, a client asking what their case is worth.

So the platform question is not "how do I build a good records agent". It is "what is the smallest
set of things that lets a firm run many kinds of slow, interrupted, human-supervised agents, and
add a new kind without touching the runtime".

## What I chose

**An agent is not a process. It is a series of short steps over durable state.** Nothing here runs
for three months. An *engagement* sleeps in a database. A trigger wakes it (a timer, an inbound
reply, a human's decision, someone at the firm asking for something). The runtime builds a
context from the engagement's timeline, asks the brain for a handful of actions, applies them,
and the engagement goes back to sleep. A crash between steps loses nothing. A retry never sends
twice because wake-ups are claimed once.

**The brain speaks five words.** `send_message`, `record_update`, `request_human`, `wait_until`,
`complete`. Every playbook, channel and brain is written against that vocabulary. If a new use
case needed a sixth action I would treat that as a design smell before adding it. This is what
lets a scripted policy and Gemini drive the same runtime unchanged, and it is what keeps the
review queue and dashboard generic.

**Everything a use case knows lives in a playbook.** Who the counterparty is, which channels are
allowed and in what order, the cadence, the structured state to maintain, which outbound
messages need sign-off, and the instructions the model gets. Plus an offline policy, a small
rule set that stands in for the model. Adding the third playbook (itemized bill follow-up) was
one file and no other change; a test defines a fourth inside the test body to prove it.

**Guardrails are the runtime's job, not the model's.** The playbook tells the model "don't chase
forever". The runtime does not trust that. It counts unanswered attempts and, on the attempt past
the limit, turns the send into an escalation for a person. Same for sign-off: first message to a
client, or any message mentioning settlement or case value, becomes an approval item with an
editable draft, regardless of what the model intended. The model can be wrong; the platform
should not be.

**Voice is a channel, not a separate agent.** The same brain that decides "follow up by email"
also holds the phone call, one turn at a time, over Twilio ConversationRelay (Twilio does speech
to text and text to speech; we exchange text over a websocket). The transcript comes back to the
engagement as an inbound reply, so a phone call and an email are the same thing to the
runtime. The call session is transport-agnostic: the demo drives it with typed lines, Twilio
drives it with a websocket, the code in between is identical.

**Humans enter through one door.** An *intervention* is opened by the agent (a question), by
policy (an approval), or by a guardrail or error (an escalation). The engagement is blocked
until someone resolves it. Replies that arrive while blocked are held, not acted on. The
resolution wakes the engagement with the decision in its context, and for approvals the runtime
sends the approved (possibly edited) draft itself before waking the agent.

## The primitives

| Primitive | What it is | Why it exists |
|---|---|---|
| Engagement | An agent assigned to one counterparty on one matter with a goal, status, structured state, attempts counter | The unit of long-running work. Status: active, waiting, blocked, completed, cancelled. |
| Trigger and Wakeup | A durable "run this engagement at time T because X" | Replaces a long-lived process. Claimed once. Cancelled when the engagement blocks. |
| Step / Run | One bounded brain decision plus its applied actions, recorded | Auditable, retry-safe, cheap to reason about. |
| Action | The five-verb vocabulary | Contract between brain, runtime, playbook and channel. |
| Playbook | Declarative use case plus offline policy | The only thing that changes per use case. |
| Policy | Runtime checks on outbound actions | Guardrails the model cannot bypass. |
| Channel | Adapter that delivers a `send_message` | Email/SMS simulated, voice real, one interface. |
| Intervention | The human-in-the-loop item | One queue for questions, approvals and escalations. |
| Event timeline | Append-only log per engagement | The agent's memory, the firm's audit trail, the dashboard's source. |
| Update | An event with a significance level and state delta | What the firm actually reads. Meaningful and urgent updates form the feed. |
| Command | Natural-language instruction from the firm | "Chase City General for the bill" becomes an engagement or a nudge. |

## What is real vs stubbed

Real: the runtime, scheduler, event log, policies, review queue, three playbooks, the dashboard,
the command box, the Gemini brain (default; every decision and every live call turn), a Claude brain
behind the same interface, and outbound voice calls through Twilio ConversationRelay including live
turn-taking and transcript capture.

Simulated: email and SMS delivery (recorded in an outbox; replies injected through the same
inbound endpoint a provider webhook would hit), the counterparty on voice when Twilio is not
configured, and firm data (seeded matters and contacts instead of a case management system).

Deterministic stand-in: the scripted brain. It exists so the platform can be demoed and tested
with no key and no variance. It is not the product; the interesting behaviour comes from the model.

## Where it breaks today

- **Inbound routing by address is naive.** A reply is matched to the most recently touched live
  engagement for that phone or email. Two engagements with the same provider on the same channel
  will collide. Real fix: threading identifiers per channel (email Message-ID, Twilio conversation SID).
- **No takeover.** A person can answer the agent and approve drafts, but cannot step into a live
  call or thread, speak as themselves, and hand it back. That is the next HITL feature.
- **Single worker, SQLite.** Fine for one firm on one box. Many firms or many workers need a real
  queue with leases (Postgres `SKIP LOCKED`, or Temporal) and the same step function.
- **Context is the last 40 events.** A four-month engagement will outgrow that. Needs a rolling
  summary maintained by the agent as part of its state.
- **Model output is trusted structurally, not semantically.** Actions are schema-validated, but
  nothing checks that an outbound message is truthful or on-brand beyond the term-based gate.
  Needs an evaluation harness with recorded engagements and a second-model review on sensitive sends.
- **No auth, no tenancy, no PHI controls** beyond the audit log. Out of scope for two days,
  not out of mind.
- **Voice is trial-grade.** Trial Twilio only calls verified numbers, the greeting is not
  interruptible, no voicemail detection, no retry policy on carrier failures.

## How a new use case fits

Say the firm wants an insurance adjuster follow-up: confirm the claim was received, get the
adjuster's name, chase the liability decision. It is a new playbook class with a key, a
counterparty kind (`insurer`), channels (`voice`, `email`), a cadence, state fields
(`claim_number`, `adjuster`, `liability_decision`), approval rules (anything mentioning demand
or settlement), instructions for the model, and an offline policy of ten to twenty lines. Register
it. The dashboard lists it, the command box routes to it, the review queue shows its items, and
the runtime, scheduler, channels and guardrails are untouched. If the adjuster workflow needs a
new *action*, that is the moment to stop and ask whether the vocabulary is wrong.

## What I would build next

1. Real email and SMS adapters with threading, so inbound routing is exact.
2. Human takeover of a live thread or call, then hand back to the agent with the human's turns in context.
3. Rolling engagement summaries so long timelines stay cheap and the firm gets a one-paragraph status.
4. An evaluation harness: recorded engagements replayed against a brain, scored on outcomes and on
   whether the right things reached a human.
5. Per-firm playbook configuration in the UI (cadence, approval rules, sensitive terms) so a case
   manager tunes behaviour without a deploy.
6. A proper queue and multi-tenancy once more than one firm is on it.
