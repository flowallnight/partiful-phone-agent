# CLAUDE.md — Partiful Phone Change Support Agent (MVP, v2)

## What this is

A take-home project: an email support agent that opens a ticket for every inbound message, triages it, and fully automates one category — **phone number change** — per the scoping doc at `docs/scoping_doc.md` (v2). That doc is the spec. Every state, tool, test, reply behavior, and claim in this build must agree with it. **If you find a mismatch between this file, the doc, or the code, stop and flag it instead of picking one silently.** When editing docs, never markdown-escape characters inside fenced code blocks (e.g. `\[` or `\_` in a mermaid fence) — escapes there are passed through literally and break the block's own parser.

## Non-negotiable rules

1. **Ticket on receipt.** Every inbound email opens a ticket before classification runs. No exceptions — including spam, gibberish, and tool failures. The ticket is the paper trail.
2. **LLMs at the edges, never on the trigger.** Models classify, extract, and draft. Only `state_machine.py` may call action functions.
3. **The commit is gated.** `mock_api.change_phone_number()` accepts an `AuthorizationRecord` and raises unless all five conditions are true: unique account found by old number; IDV result verified + unexpired; new-number OTP passed + unexpired; new number not on another account; no completed change already on this ticket. Unit tests must prove it fails from every other state and with each condition individually false. (A recent-activity risk flag was deliberately removed from the gate — decision trail in scoping doc §6/§12; account activity never gates in the MVP.)
4. **No real writes, ever.** Internal API, SMS, IDV, and ticket-system calls all print to console as the request they would have been (`>>> INTERNAL API:`, `>>> SMS →`, `>>> TICKET`).
5. **Injection can't move state.** Transitions act only on structured classifier/extractor output, never on email prose.
6. **Fail toward humans.** IDV fail, OTP fail ×2, number unavailable, no unique account (after one re-check ask), confidence < 0.8, 2 consecutive un-parseable replies, any tool failure, or an explicit request for a human → `ESCALATED` with reason code + case file. Never auto-reject a user; never claim success after a failure.
7. **Enumeration protection.** Outbound email never confirms or denies that any phone number has a Partiful account, and escalation replies stay generic ("a teammate will take it from here"). Real reasons go on the ticket only.
8. **Attachments are never opened or processed.** Log filename, set `redacted: true` on the ticket, reply redirecting the user to the secure verification flow, continue the normal state flow.
9. **Closing rules — two postures.** The agent closes a ticket only on `phone_change_completed`. *Solved* (self-serve) is a soft close: the email says we're marking it resolved and explicitly invites a reply if the link doesn't work ("we'll pick it right back up" — never do-not-reply language); a reply reopens into COLLECT. *Closed* is terminal and the record immutable: zero writes, never moved out of `tickets/closed/`, `reason_code` never overwritten; the completion email ends with the do-not-reply + hello@partiful.com closing note. Inbound on a closed thread: no reply, no reopen, no escalation — the message is preserved in `post_close_messages` on the thread record; sole exception, attachments still get redacted logging (thread record) + the redirect reply. A fresh thread whose message references a prior resolved issue links to the most recent closed ticket for that requester via `linked_ticket` (noted both directions on the new ticket; original untouched). Escalation transfers ownership — only a human closes an escalated ticket.
10. **No credentials in the repo.** Secrets live in `.env` (gitignored); keep `.env.example` current.

## Repo layout

```
partiful-phone-agent/
├── CLAUDE.md
├── README.md
├── .env.example
├── docs/
│   └── scoping_doc.md         # v2 — the spec
├── agent/
│   ├── main.py                # entrypoint: poll loop; --provider simulated|gmail
│   ├── config.py              # env, model names, thresholds
│   ├── email_provider.py      # EmailProvider ABC + GmailProvider + SimulatedProvider
│   ├── classifier.py          # Haiku: category + confidence + fields (strict JSON)
│   ├── state_machine.py       # ONLY place actions fire; owns all transitions
│   ├── drafter.py             # Sonnet replies; no tools, text only
│   ├── idv.py                 # mock IDV provider: session creation only; verdict via handle_idv_result
│   ├── otp.py                 # mock new-number OTP: generate, print, verify, expire, max 2 tries
│   ├── mock_api.py            # user service + SMS prints + the AUTHORIZE-gated commit
│   ├── ticket_store.py        # JSON tickets under tickets/{open,solved,escalated,closed}/
│   ├── store.py               # per-thread state under .state/
│   └── fixtures/
│       └── users.json         # mock user DB keyed by E.164 phone number
├── tickets/                   # runtime output — gitignored
└── tests/
    ├── scenarios.json         # 20 cases: opener, scripted replies, idv_result, expectations
    ├── run_suite.py           # assertion runner on SimulatedProvider
    ├── send_scenario.py       # CLI: --list, --show N, --send N (live-demo openers)
    └── test_gate.py           # commit-gate unit tests (rule 3)
```

## State machine (from the scoping doc — do not redesign)

`NEW → ASK_OLD_ACCESS → (SELF_SERVE | COLLECT) → VERIFY_IDENTITY → VERIFY_NEW_NUMBER → AUTHORIZE → COMMIT → RESOLVED`, plus `ESCALATED` (terminal for the agent).

- `NEW`: ticket already open. Classify into `phone_number_change` / `other_support` / `unknown`. Extract fields opportunistically from the first message; never re-ask for something already given. Non-phone or low confidence → escalate with reason.
- `ASK_OLD_ACCESS`: "can you still receive texts on the old number?" Yes → `SELF_SERVE`. No → `COLLECT`.
- `SELF_SERVE`: send exactly this link, mark ticket `solved`:
  `https://help.partiful.com/hc/en-us/articles/26025082969243-Can-I-change-my-phone-number`
  A reply saying it didn't work / no access reopens the ticket into `COLLECT`.
- `COLLECT`: one batched ask — full name, old number, new number — including a copyable fill-in template (`Name:` / `Old number on the account:` / `New number:`); free-form replies accepted, numbers normalized to E.164. The name feeds the ticket and the IDV session; it is NEVER an authorization input. Nudge for exactly the missing fields, max 2. Look up the account by old number here, printed as `>>> INTERNAL API: GET /internal/users?phone={old} → {user_id} (account found)` or `→ no account found`: no unique match → one double-check ask → escalate `account_not_found`.
- `VERIFY_IDENTITY` — asynchronous, two-phase (mirrors production webhooks). Phase 1, on entry: create a session — print `>>> IDV API: POST /verification_sessions {...} → session {id}` — queue ONLY the `idv_link` reply carrying `https://verify.partiful-demo.example/session/{id}` ("our identity verification partner", never a vendor name; remind them never to send IDs by email; identify the request by both endpoints, last-four only and present-accurate tense — "the account connected to the number ending in {old last4} over to a new number ending in {new last4}", never "previously"; state that old-number access is NOT needed — IDV confirms who they are, the code texted to the new number afterward confirms control of that number; never imply the IDV provider checks phone numbers), record `idv_link_sent`, and END the turn so the email actually sends; state holds at `VERIFY_IDENTITY`. The session id is the ticket's `idv_ref`. Phase 2: the verdict arrives as its own event via `machine.handle_idv_result(thread_id, verified: bool)`, which records `idv_webhook_received` and continues — pass → `VERIFY_NEW_NUMBER` (OTP send + `otp_prompt`), fail → escalate `identity_verification_failed`. Verdict source: simulated mode delivers the scenario's `idv_result` between turns; gmail mode prompts on the console AFTER the turn's replies have sent (`IDV webhook for session {id} [pass/fail]:`) — the operator plays the provider webhook; nothing blocks inside `handle_message`.
- `VERIFY_NEW_NUMBER`: generate a 6-digit code, print `>>> SMS → new number: your Partiful code is NNNNNN` (~10 min expiry). The user replies with the code. Wrong or expired twice → escalate `otp_failed`. The code never appears in any email.
- `AUTHORIZE`: evaluate all five conditions (rule 3), record each check's result on the ticket. Any fail → escalate with the specific reason to the `general` queue (`security_review` is plumbed but reserved for production risk signals — scoping doc §16).
- `COMMIT`: five printed actions in strict order — API call first; the rest only if it succeeds:

```
>>> INTERNAL API: POST /internal/users/u_123/phone_number {"new": "+14155550187"}
>>> SMS → old number: a number change was made on this account; if this wasn't you, contact support (generic, zero PII)
>>> SMS → new number: this number is now linked to a Partiful account
>>> EMAIL → thread: request complete, ticket #PF-0042 (no account details)
>>> TICKET API: PUT /tickets/PF-0042 {"status":"closed","closed_by":"agent","reason":"phone_change_completed"} + evidence bundle
```

  If the API call fails (test 15 stubs a 500): no notifications, no success messaging; tell the user a teammate will finish; escalate `api_failure`.

- **Escalation output.** Ticketing is modeled on Zendesk (inferred from Partiful's Zendesk Guide help-center URLs). Every path into `ESCALATED` prints three lines — the agent writes the ticket; the helpdesk's native trigger does the notifying:

```
>>> TICKET API: PUT /tickets/PF-0042 {"status":"open","group":"general","priority":"normal","tags":["phone_change","identity_verification_failed"]}
>>> TICKET API: internal note on PF-0042 — case file (who · what they asked · what was verified · what failed · recommended next step)
>>> HELPDESK TRIGGER → assignee email: "Ticket PF-0042 assigned: phone change — identity_verification_failed" + link (pointer only, never the payload)
```

  The agent never builds its own notifier and never copies case files or attachment contents into any email.

## Ticket record (JSON schema)

`id` (PF-####) · `opened_at` · `requester_email` (labeled contact channel — NOT identity) · `transcript[]` (both directions, timestamped) · `state_history[]` · `actions[]` (each with reason) · `attachments[]` (`{filename, redacted: true}`) · `status` (`open|solved|escalated|closed`) · `queue` (`general|security_review`) · `priority` (`normal|high`) · `reason_code` · `evidence` (`{idv_ref, otp_verified_at, authorize_checks{...}}`) · `closed_by` (`agent|human|null`) · `linked_ticket` (fresh ticket referencing a closed one, else null)

Messages arriving after a ticket closes live in `post_close_messages` on the *thread* record (`.state/`), never on the immutable closed ticket.

Escalated tickets must include a case-file summary: who reached out, what they asked, what was verified, what failed, recommended next step.

## LLM usage (Anthropic API)

- Classifier: `claude-haiku-4-5-20251001`, temperature 0, strict JSON only: `{"category": ..., "confidence": ..., "fields": {"old_number": ..., "new_number": ..., "name": ..., "has_old_access": ...}, "wants_human": ..., "references_prior_issue": ...}`. `wants_human` and `references_prior_issue` are booleans, always present, default `false`. `wants_human` is `true` only when the user explicitly asks for a person; the state machine then escalates from any (non-closed) state with reason code `user_requested_human` to the `general` queue. `references_prior_issue` is `true` only when the message says it relates to a previous, already-handled interaction; on a fresh thread it drives the `linked_ticket` flow (rule 9). The classifier runs on every inbound message except closed-thread replies (which are never classified). Number extraction is role-gated: a number counts as `old_number`/`new_number` only when the surrounding text assigns it a role ("my old number is …", "moving to …", filled template fields); numbers in signature blocks or contact-info footers (after sign-offs like "Best,"/"Thanks,", name lines, `Desk:`-style contact lines) are never extracted unless the body explicitly refers to them — an ambiguous number extracts as null and COLLECT asks (asserted by scenarios 18–19). Malformed output (including a missing or non-boolean flag): retry once, then escalate `model_error`.
- Drafter: `claude-sonnet-4-6`. Warm, casual, concise — Partiful's voice, not corporate boilerplate. Sign "Partiful Support". Input: state + facts to convey; output: email body only. Intents: ask_old_access, self_serve_link, collect_ask, collect_nudge, number_recheck, idv_link, otp_prompt, otp_retry, commit_success, attachment_redirect, escalation_generic, handoff_after_failure. Hard content rules (assert in tests where feasible): never confirm/deny account existence for a number; never include codes, full phone numbers, or account details (the sole permitted identifiers are facts-supplied last-four partials like "ending in 1111", in the idv_link email only); escalation replies stay generic but set a response-time expectation — one warm sentence that a real person will follow up within `{SLA_BUSINESS_DAYS}` business days (config, default 3; a business-policy placeholder per doc §7); ticket ids appear only as the exact phrase `Ticket Reference: PF-####`; the first reply on any ticket announces it with that phrase; `commit_success` ends with the resolved-and-closed + don't-reply + hello@partiful.com note. The first reply also carries a deterministic quote of the opening message (separator, `On {date} at {time}, {sender} wrote:`, `> `-prefixed lines) appended in code at compose time — the drafter never sees or reproduces user text; later replies never re-quote.

## Email transport

- **Support side:** `GmailProvider` on `partiful.support.demo@gmail.com` — IMAP poll every 15s + SMTP, Gmail **app password** (2FA required). No OAuth — do not add it. Track processed UIDs so restarts don't reprocess. Replies set `In-Reply-To`/`References`, mint a `Message-ID` via `make_msgid`, and use the subject `Re: {original subject} [PF-####]` (exactly one tag, ever — `tag_subject` strips old tags before appending).
- **Conversation identity (hard-won):** Gmail groups conversations by subject + References, so mutated subjects break `X-GM-THRID` continuity — our tagged reply forked the thread in live testing. Conversation identity therefore comes from the provider's own persistent Message-ID index (`.state/gmail_provider.json`): every seen Message-ID, inbound and outbound, maps to an internal `conv-####` key minted at the opener. Inbound resolution order: (a) In-Reply-To/References hit in the index; (b) `[PF-####]` subject tag AND sender == that ticket's requester (fallback for clients that strip References — a tag alone is spoofable); (c) new thread. Never key on provider thread ids when subjects are tagged; `X-GM-THRID` is logged as a hint at most.
- **User side:** Jordan Rivera, `jordan.riverademo@yahoo.com`. The harness needs only Yahoo SMTP (`smtp.mail.yahoo.com`, 465 SSL, Yahoo app password) to send scenario openers. Mid-thread replies (answers, OTP codes) are sent live from the Yahoo web UI during the demo.
- **Yahoo fallback:** if Yahoo won't issue an app password on the new account, `send_scenario.py --show N` prints subject + body for copy-paste. Demo unaffected.
- Attachments: per rule 8, note filename only — never fetch/decode content beyond that.
- `SimulatedProvider`: same interface, in-memory/JSON inbox; used for development, the full automated suite, and as demo fallback.

## Fixtures (agent/fixtures/users.json)

5–6 accounts keyed by E.164 number, each with `user_id`, `name`, `last_active`. **Jordan Rivera** is the happy-path account (test 2). `last_active` is retained on every account as the data a production risk signal would read (scoping doc §16) — nothing in the MVP gate reads it, and a gate test proves a recently-active account (**Priya Natarajan**, 2 days) commits fine. Include one nickname-style and one emoji name to show names never matter to authorization. One number is reserved as "already in use" for test 6.

## Test suite

`tests/run_suite.py` runs all 20 scenarios on `SimulatedProvider`, scripting the user's replies from `scenarios.json`, and asserts per scenario: terminal state · ticket status + `reason_code` + `queue` · required action calls with exact counts (commit ×1 in tests 2, 8 and 16 ONLY) · forbidden calls (commit ×0 everywhere else; attachment processing ×0 everywhere) · reply type at each step · first-reply `Ticket Reference:` line + `[PF-####]` subject tags · E.164 normalization of varied inbound number formats · visible account-lookup print (test 7 asserts the no-account variant) · escalation replies contain the exact "{SLA_BUSINESS_DAYS} business days" expectation. `tests/test_gate.py` covers rule 3 directly. Scenarios (full expectations in the scoping doc §13): 1 self-serve · 2 full recovery · 3 mid-flow explicit human request → user_requested_human (SLA line, case file, zero verification actions) · 4 IDV fail · 5 OTP fail ×2 · 6 number unavailable · 7 account not found · 8 incomplete collect → nudge → complete · 9 vague opener · 10 other_support · 11 unknown/low confidence · 12 injection attempt · 13 unsolicited ID attachment · 14 malformed classifier output · 15 update API 500 · 16 post-close stray reply (silence; immutable closed record; `post_close_messages`) · 17 fresh email referencing a closed ticket (`linked_ticket`, original untouched) · 18 signature-block number (another user's account) ignored — no lookup fires on it, COLLECT asks for both numbers · 19 signature number + explicit body numbers — only the explicit ones extracted · 20 solved reopen — reply after self-serve/`solved` reopens the ticket (`solved` → `open`) into COLLECT with the batched ask.

Live-demo subset over real email: 1, 2, 4, 7, 10.

## Build phases (work in order; keep tests green before advancing)

1. Scaffold + fixtures + `ticket_store.py` + `state_machine.py` + `store.py`, fully offline with a stubbed classifier. `test_gate.py` green. **No LLM calls yet.**
2. Wire classifier, drafter, `idv.py`, `otp.py`; `run_suite.py` green on all 15.
3. `GmailProvider` live; smoke-test scenarios 1 and 2 over real email.
4. `send_scenario.py` harness (Yahoo SMTP + `--show` fallback).
5. Demo polish: one-line state transitions (`[PF-0042] COLLECT → VERIFY_IDENTITY`), loud `>>>` action lines, and a COMMIT block that matches the example above exactly.

## Commands

```
python -m agent.main --provider simulated   # dev loop
python -m agent.main --provider gmail       # live demo
python tests/run_suite.py                   # full 19-scenario assertion suite
python tests/send_scenario.py --list
python tests/send_scenario.py --show 2      # print subject + body for manual copy-paste
python tests/send_scenario.py --send 2      # send via Yahoo SMTP (if app password works)
pytest
```

## Environment (.env.example)

```
ANTHROPIC_API_KEY=
SUPPORT_EMAIL=partiful.support.demo@gmail.com
# Gmail: enable 2FA first, then myaccount.google.com/apppasswords
SUPPORT_APP_PASSWORD=
SUPPORT_IMAP_HOST=imap.gmail.com
SUPPORT_SMTP_HOST=smtp.gmail.com
USER_EMAIL=jordan.riverademo@yahoo.com
# Yahoo: Account Security → Create app password (optional — see fallback)
USER_APP_PASSWORD=
# port 465 SSL; send-only, no IMAP needed
USER_SMTP_HOST=smtp.mail.yahoo.com
POLL_SECONDS=15
CONFIDENCE_THRESHOLD=0.8
# escalation-reply follow-up window — business-policy placeholder (scoping doc §7)
SLA_BUSINESS_DAYS=3
# NOTE: comments stay on their own lines — python-dotenv treats an inline comment after a BLANK value as the value itself.
```

## Scope discipline

Plain Python + `anthropic` + `python-dotenv` + `pytest`. No database, no web framework, no queues, no Docker, no UI, no Pillow, no attachment parsing. If a feature isn't required by a scenario or the scoping doc, it goes on the post-MVP list, not in the code. When in doubt, choose the version that is easier to demo and easier to explain.
