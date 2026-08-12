# Partiful Phone Change Support Agent (MVP)

An email support agent that opens a ticket for **every** inbound message, triages
it, and fully automates one category — **phone number change** — per
`docs/scoping_doc.md` (v2), which is the spec for everything in this repo.

The design in one paragraph: LLMs sit at the edges (a Haiku classifier reads
messy human email into strict JSON; a Sonnet drafter writes warm replies from
facts it's handed), while a deterministic state machine owns every consequential
decision. The phone-change commit is a single gated function that raises unless
all five authorization conditions are verifiably true, so neither a model nor a
prompt-injecting user can talk the system into skipping steps. Everything fails
toward humans: any error or ambiguity ends in an escalated ticket
carrying a prepared case file — never an automated rejection, never a claimed
success that didn't happen. No real writes anywhere: every internal action
prints to the console as the request it would have been (`>>> INTERNAL API:`,
`>>> SMS →`, `>>> IDV API:`, `>>> TICKET API:`).

## The flow

```
NEW → ASK_OLD_ACCESS → (SELF_SERVE | COLLECT) → VERIFY_IDENTITY
    → VERIFY_NEW_NUMBER → AUTHORIZE → COMMIT → RESOLVED
plus ESCALATED (terminal for the agent — only a human closes it from there)
```

- Still have the old number? → self-serve help-center link, ticket `solved`
  (soft close; a reply reopens into COLLECT).
- Lost it? → one batched ask (name · old number · new number), then identity
  verification via a hosted link from "our identity verification partner"
  (two-phase: session created and link emailed, verdict arrives later as its own
  webhook-shaped event), then a 6-digit OTP texted to the **new** number, then
  the five-condition AUTHORIZE gate, then the five-action COMMIT block.
- Everything else (`other_support`, `unknown`, low confidence, failures,
  explicit "I want a human") → `ESCALATED` with a reason code, queue routing
  (`general`; a `security_review` queue is plumbed and reserved for production
  risk signals), and a case file on the ticket. The
  user-facing reply stays generic and sets one expectation: a real person will
  follow up within `SLA_BUSINESS_DAYS` business days (config, default 3 — a
  business-policy placeholder for Partiful to set, per scoping doc §7).

## Setup

Python 3.10+.

```
pip install -r requirements.txt        # anthropic, python-dotenv, pytest
copy .env.example .env                 # then fill in values
```

- `ANTHROPIC_API_KEY` — required for the suite and any live run.
- Gmail (support side, live demo only): enable 2FA on the support account, mint
  an app password at myaccount.google.com/apppasswords, set
  `SUPPORT_APP_PASSWORD`. No OAuth by design.
- Yahoo (user side, live demo only): an app password in `USER_APP_PASSWORD`
  enables `--send`; without it, `--show` prints openers for copy-paste.
- Keep `.env` comments on their own lines — python-dotenv treats an inline
  comment after a blank value as the value itself.

Secrets never go in the repo; `.env` is gitignored.

## Commands

```
pytest                                      # commit-gate + offline unit tests
python tests/run_suite.py                   # full 20-scenario assertion suite (real LLM calls)
python tests/run_suite.py 3 12              # subset by scenario id
python -m agent.main --provider simulated   # interactive dev loop (console inbox)
python -m agent.main --provider gmail       # live demo: IMAP poll + SMTP replies
python tests/send_scenario.py --list        # scenario openers: id, name, subject
python tests/send_scenario.py --show 2      # print subject + body for copy-paste
python tests/send_scenario.py --send 2      # send opener via Yahoo SMTP
```

`run_suite.py` drives the **real** classifier (Haiku) and drafter (Sonnet), so
it needs `ANTHROPIC_API_KEY` and costs a few cents / roughly 2–3 minutes per run.

## Test suite — 20 scenarios

`tests/run_suite.py` scripts the user's side of each conversation from
`tests/scenarios.json` on the `SimulatedProvider` and asserts, per scenario:
terminal state; ticket status + reason code + queue/priority; required action
calls with exact counts (the commit fires exactly once in exactly scenarios 2,
8, and 16 — zero everywhere else); forbidden calls; the reply intent at each
step; first-reply `Ticket Reference: PF-####` line and `[PF-####]` subject tags;
the deterministic quoted opener on the first reply only; E.164 normalization of
variously formatted numbers; enumeration-safe content rules (no full numbers, no
OTP codes, last-four partials in the idv_link email only); the SLA expectation
in escalation replies; the visible account-lookup print; and extraction
robustness — signature-block numbers are never treated as the old/new number
and never looked up.

| # | Scenario | Outcome |
|---|----------|---------|
| 1 | Has old phone | Self-serve link; ticket `solved` |
| 2 | Full recovery: fields → IDV pass → OTP pass | Commit ×1; ticket `closed` by agent |
| 3 | Asks for a person mid-flow | Escalated `user_requested_human` → general queue; SLA line; case file; zero verification actions |
| 4 | IDV returns failed | Escalated `identity_verification_failed` |
| 5 | Wrong OTP twice | Escalated `otp_failed` |
| 6 | New number already on another account | Escalated `number_unavailable`, enumeration-safe reply |
| 7 | Old number matches no account | One re-check ask, then escalated `account_not_found` |
| 8 | Incomplete COLLECT reply | Nudge names exactly the missing fields; completes (commit ×1) |
| 9 | Vague opener ("I'm locked out??") | Classified; right clarifying question |
| 10 | Event/RSVP question | `other_support` → general queue |
| 11 | Gibberish | `unknown`/low confidence → escalated |
| 12 | Prompt injection ("skip verification NOW") | State unchanged; zero action calls |
| 13 | Unsolicited ID attachment | Logged + redacted, never opened; flow continues |
| 14 | Malformed classifier output (stubbed) | One retry, then escalated `model_error` |
| 15 | Update API 500 at commit | No success claim, no notifications; escalated `api_failure` |
| 16 | Stray reply after close | Silence; closed record byte-identical; message in `post_close_messages` |
| 17 | Fresh email referencing a closed ticket | New ticket with `linked_ticket`; original untouched |
| 18 | Signature block carries another user's number | Not extracted, no lookup fires on it; COLLECT asks for both numbers |
| 19 | Signature number + explicit numbers in body | Only the explicit ones extracted; flow proceeds on them |
| 20 | Self-serve link didn't work (reply after `solved`) | Ticket reopens (`solved` → `open`); flow re-enters COLLECT with the batched ask |

`tests/test_gate.py` (pytest, offline) proves the commit gate directly: it
raises from every non-AUTHORIZE state and with each of the five conditions
individually false, never prints the API call on a raising path, and shows
that neither names nor recent account activity ever gate a commit.

## Live demo

Scenarios **1, 2, 4, 7, 10** run over real email:

1. `python -m agent.main --provider gmail` — polls the support Gmail inbox
   every 15s, replies over SMTP with proper threading headers.
2. `python tests/send_scenario.py --send N` (or `--show N` + paste into the
   Yahoo web UI) sends the opener from the user side, Jordan Rivera.
3. Mid-thread replies (answers, OTP codes) are sent live from the Yahoo web UI.
4. When a thread reaches identity verification, the console prompts
   `IDV webhook for session {id} [pass/fail]:` **after** the link email has
   sent — the operator plays the provider's webhook.

Console output is the demo surface: one-line state transitions
(`[PF-0042] COLLECT → VERIFY_IDENTITY`) and loud `>>>` action lines for every
side effect, with the COMMIT and escalation blocks matching the spec examples.

## Production Integration Map

Every external boundary is mocked behind an interface shaped like its real
counterpart, so productionizing is a swap, not a redesign (scoping doc §10–11,
§16):

| Mocked boundary | Module | MVP behavior | Production replacement |
|---|---|---|---|
| User lookup / number availability | `agent/mock_api.py` (`lookup_user_by_phone`, `is_number_available`) | Fixture DB `agent/fixtures/users.json` keyed by E.164; `last_active` retained as data, unused by the gate | Internal user service (`GET /internal/users?phone=…`, availability check); production risk signals — activity postdating ticket-open (observed, not self-reported), device, IP — routing to the reserved `security_review` queue (scoping doc §16) |
| Phone-change commit | `agent/mock_api.py` (`change_phone_number`) | Prints `POST /internal/users/{id}/phone_number` after the five-condition gate passes; the gate re-derives every check itself | Same gate code calling the internal update endpoint; gate logic ships unchanged — it *is* the automation policy |
| SMS (OTP + notifications) | `agent/otp.py`, `agent/mock_api.py` (`send_sms`) | Prints `>>> SMS → …` lines; codes generated locally, ~10 min expiry, max 2 attempts | SMS gateway / carrier API (e.g. Twilio); production upgrade is SMS deep-link OTP so codes never transit email |
| Identity verification | `agent/idv.py` + `StateMachine.handle_idv_result` | Prints session creation, emails a mock hosted link; verdict injected by scenario script (simulated) or console prompt (gmail) | Hosted-link IDV vendor (e.g. Persona, Stripe Identity): real session API; vendor webhook delivers the verdict into `handle_idv_result` |
| Ticketing + assignee notification | `agent/ticket_store.py` | JSON records under `tickets/{open,solved,escalated,closed}/`; prints Zendesk-shaped API calls and the helpdesk trigger | Zendesk Ticket API (Partiful's help center runs on Zendesk Guide); assignee notification via Zendesk's native triggers — the agent never builds its own notifier |
| Email transport | `agent/email_provider.py` | `GmailProvider` (IMAP poll + SMTP, app password) for the live demo; `SimulatedProvider` for dev and tests | The helpdesk's own email channel (hello@partiful.com in Zendesk); the `EmailProvider` interface is the seam |

Note: beyond the reason codes named in the scoping doc, the implementation adds
`unclassified`, `low_confidence`, `unparseable_replies`, `collect_incomplete`,
and `tool_failure` so every escalation path carries a specific code.

## Repo layout

See `CLAUDE.md` for the annotated tree and the non-negotiable build rules;
`docs/scoping_doc.md` for the full design, identity model, and decision trail.
