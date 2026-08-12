"""Assertion runner: drives all 20 scenarios from scenarios.json through the
agent on the SimulatedProvider and asserts, per scenario (scoping doc §13):

  - terminal state
  - ticket status + reason_code + queue (+ priority / closed_by where pinned)
  - required action calls with exact counts (commit ×1 in scenarios 2, 8 & 16
    ONLY, counted as printed `>>> INTERNAL API: POST` lines)
  - forbidden calls (commit ×0 everywhere else; attachment processing ×0)
  - the reply type (intent) at each step
  - content rules where deterministic: no OTP code and no new number in any
    outbound email body; attachments always redacted

Uses the REAL classifier (Haiku) and drafter (Sonnet) — requires
ANTHROPIC_API_KEY. Scenario 14 swaps in a stubbed malformed classifier per the
spec. Run as:  python tests/run_suite.py  [scenario ids...]
"""
from __future__ import annotations

import contextlib
import io
import json
import random
import re
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Scenario names and action lines use arrows; Windows consoles often default
# to cp1252, which cannot encode them.
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from agent import config, mock_api
from agent.classifier import Classifier
from agent.drafter import Drafter
from agent.email_provider import SimulatedProvider
from agent.idv import IdvProvider
from agent.otp import Otp
from agent.state_machine import QUOTE_SEPARATOR, StateMachine
from agent.store import ThreadStore
from agent.ticket_store import TicketStore

NOW = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)  # frozen clock: deterministic records
USER_EMAIL = "jordan.riverademo@yahoo.com"
MONITORED_ACTIONS = ("idv_link_sent", "idv_webhook_received", "otp_sent",
                     "otp_verified", "otp_attempt_failed", "authorize_evaluated",
                     "attachment_logged", "linked_ticket")
SCENARIOS_PATH = Path(__file__).parent / "scenarios.json"


class MalformedClassifier:
    """Scenario 14: strict-JSON contract violated on every call."""

    def classify(self, subject, body):
        return {"category": "phone_number_change", "confidence": "very sure"}


def _wrong_code(code):
    return "000000" if code != "000000" else "111111"


def _fill_placeholders(body, threads, thread_id):
    if "{{OTP_CODE}}" in body or "{{WRONG_OTP}}" in body:
        pending = threads.get(thread_id).otp_pending
        if not pending:
            raise AssertionError("scripted reply needs an OTP code but none was sent")
        body = body.replace("{{OTP_CODE}}", pending["code"])
        body = body.replace("{{WRONG_OTP}}", _wrong_code(pending["code"]))
    return body


def run_scenario(sc, classifier, drafter):
    """Returns (failures, captured_stdout)."""
    tmp = Path(tempfile.mkdtemp(prefix=f"pf-suite-{sc['id']:02d}-"))
    tickets = TicketStore(root=tmp / "tickets")
    threads = ThreadStore(root=tmp / "state")
    provider = SimulatedProvider()
    machine = StateMachine(
        tickets, threads,
        MalformedClassifier() if sc.get("classifier") == "malformed" else classifier,
        drafter,
        IdvProvider(),
        Otp(random.Random(42)),
        now_fn=lambda: NOW,
    )
    mock_api._fail_next_call = False  # isolate the one-shot 500 between scenarios

    # Scenario 17: a previously closed ticket for the same requester must
    # already exist; it is seeded directly (not via the email flow) and must
    # come out of the scenario byte-identical.
    seeded_path = seeded_bytes = None
    if sc.get("setup") == "seed_closed_ticket":
        with contextlib.redirect_stdout(io.StringIO()):
            seeded = tickets.open_ticket(USER_EMAIL, now=NOW)
            tickets.close(seeded, evidence={"note": "seeded prior resolution"}, now=NOW)
        seeded_path = tickets.root / "closed" / f"{seeded.id}.json"
        seeded_bytes = seeded_path.read_bytes()

    thread_id = f"thread-{sc['id']:02d}"
    captured = io.StringIO()
    states_after = []
    otp_codes_seen = set()
    turns = []  # per turn, the list of intents flushed in that turn

    def flush_turn(tid, replies):
        for reply in replies:
            provider.send(tid, reply["body"], intent=reply["intent"],
                          ticket_id=reply.get("ticket_id"))
        turns.append([reply["intent"] for reply in replies])
        pending = threads.get(thread_id).otp_pending
        if pending:
            otp_codes_seen.add(pending["code"])

    def pump():
        for tid, email in provider.fetch():
            with contextlib.redirect_stdout(captured):
                replies = machine.handle_message(tid, email)
            flush_turn(tid, replies)
            states_after.append(threads.get(tid).state)
        # The IDV verdict is its own event/turn — the scripted stand-in for
        # the provider webhook, delivered only after the prior turn flushed.
        ts = threads.get(thread_id)
        if ts.state == "VERIFY_IDENTITY" and ts.idv_session_id and ts.idv_result is None:
            with contextlib.redirect_stdout(captured):
                replies = machine.handle_idv_result(thread_id, bool(sc.get("idv_result")))
            flush_turn(thread_id, replies)

    opener = {
        "from": USER_EMAIL,
        "subject": sc["opener"]["subject"],
        "body": sc["opener"]["body"],
        "attachments": sc["opener"].get("attachments", []),
    }
    provider.deliver(thread_id, opener)
    pump()

    for i, reply_body in enumerate(sc.get("replies", [])):
        if sc.get("fail_api_before_reply") == i:
            mock_api.fail_next_call()
        provider.deliver(thread_id, {
            "from": USER_EMAIL,
            "subject": f"Re: {sc['opener']['subject']}",
            "body": _fill_placeholders(reply_body, threads, thread_id),
            "attachments": [],
        })
        pump()

    failures = _check(sc["expect"], threads.get(thread_id), tickets, provider,
                      captured.getvalue(), states_after, otp_codes_seen,
                      turns, sc["opener"]["body"])

    # Scenario 16: replies after close → zero outbound, closed record
    # byte-identical, message preserved on the thread's post-close log.
    post_replies = sc.get("post_close_replies", [])
    if post_replies:
        ticket_id = threads.get(thread_id).ticket_id
        closed_path = tickets.root / "closed" / f"{ticket_id}.json"
        before_bytes = closed_path.read_bytes()
        outbox_before = len(provider.outbox)
        for body in post_replies:
            provider.deliver(thread_id, {
                "from": USER_EMAIL,
                "subject": f"Re: {sc['opener']['subject']}",
                "body": body,
                "attachments": [],
            })
            pump()
        if len(provider.outbox) != outbox_before:
            failures.append("outbound email was sent on a closed thread")
        if closed_path.read_bytes() != before_bytes:
            failures.append("closed ticket record changed after a post-close reply")
        pcm = threads.get(thread_id).post_close_messages
        want = sc["expect"].get("post_close_count", len(post_replies))
        if len(pcm) != want:
            failures.append(f"post_close_messages count {len(pcm)} != {want}")
        elif pcm and pcm[-1]["body"] != post_replies[-1]:
            failures.append("post-close message body was not preserved verbatim")
        commits_now = captured.getvalue().count(">>> INTERNAL API: POST")
        if commits_now != sc["expect"]["commit_count"]:
            failures.append(f"commit count changed to {commits_now} after post-close reply")

    if seeded_bytes is not None and seeded_path.read_bytes() != seeded_bytes:
        failures.append("seeded closed ticket was modified by the new thread")

    return failures, captured.getvalue()


def _check(expect, ts, tickets, provider, stdout, states_after, otp_codes_seen,
           turns, opener_body):
    failures = []

    def need(cond, msg):
        if not cond:
            failures.append(msg)

    ticket = tickets.get(ts.ticket_id) if ts.ticket_id else None
    need(ticket is not None, "no ticket was opened")
    if ticket is None:
        return failures

    # Terminal state + ticket routing fields.
    need(ts.state == expect["state"], f"state {ts.state!r} != {expect['state']!r}")
    need(ticket.status == expect["status"], f"status {ticket.status!r} != {expect['status']!r}")
    want_reason = expect.get("reason_code")
    if isinstance(want_reason, list):
        need(ticket.reason_code in want_reason,
             f"reason_code {ticket.reason_code!r} not in {want_reason}")
    else:
        need(ticket.reason_code == want_reason,
             f"reason_code {ticket.reason_code!r} != {want_reason!r}")
    need(ticket.queue == expect.get("queue", "general"),
         f"queue {ticket.queue!r} != {expect.get('queue', 'general')!r}")
    if "priority" in expect:
        need(ticket.priority == expect["priority"],
             f"priority {ticket.priority!r} != {expect['priority']!r}")
    if "closed_by" in expect:
        need(ticket.closed_by == expect["closed_by"],
             f"closed_by {ticket.closed_by!r} != {expect['closed_by']!r}")

    # Reply type at each step.
    intents = [entry["intent"] for entry in provider.outbox]
    need(intents == expect["intents"], f"intents {intents} != {expect['intents']}")

    # Ticket visibility: the first reply announces the ticket, every subject
    # carries the [PF-####] tag, and any body mention of the id uses the exact
    # "Ticket Reference:" phrase. Quoted user text (below the separator) is
    # the user's own words — content rules apply to what the AGENT says.
    all_bodies = [entry["body"] for entry in provider.outbox]
    agent_bodies = [b.split(QUOTE_SEPARATOR)[0] for b in all_bodies]
    if all_bodies:
        need(f"Ticket Reference: {ticket.id}" in agent_bodies[0],
             f"first reply lacks the exact 'Ticket Reference: {ticket.id}' line")
    for entry in provider.outbox:
        need(entry["subject"].startswith("Re: ") and f"[{ticket.id}]" in entry["subject"],
             f"subject {entry['subject']!r} is not tagged 'Re: … [{ticket.id}]'")
    for body in agent_bodies:
        for m in re.finditer(re.escape(ticket.id), body):
            prefix = body[max(0, m.start() - len("Ticket Reference: ")):m.start()]
            need(prefix == "Ticket Reference: ",
                 f"bare ticket id mention without 'Ticket Reference:' phrase: "
                 f"…{body[max(0, m.start() - 25):m.start() + 8]!r}…")

    # Quoted opener: deterministically appended to the FIRST reply only.
    if all_bodies:
        need(QUOTE_SEPARATOR in all_bodies[0] and "wrote:" in all_bodies[0],
             "first reply lacks the quoted original message")
        for line in (opener_body or "").splitlines():
            need(f"> {line}" in all_bodies[0],
                 f"opener line not quoted verbatim: {line[:40]!r}")
        for body in all_bodies[1:]:
            need(QUOTE_SEPARATOR not in body, "a later reply re-quoted the original")

    # Event-driven IDV: the link email flushes in its own turn; the OTP prompt
    # (or the escalation) appears only in a post-verdict turn.
    intents_flat = [i for turn in turns for i in turn]
    if "idv_link" in intents_flat:
        link_turn = next(i for i, turn in enumerate(turns) if "idv_link" in turn)
        need(turns[link_turn][-1] == "idv_link",
             f"idv_link did not end its turn: {turns[link_turn]}")
        need(not {"otp_prompt", "escalation_generic"} & set(turns[link_turn]),
             f"verdict-side reply in the idv_link turn: {turns[link_turn]}")
        after = [i for turn in turns[link_turn + 1:] for i in turn]
        need(bool({"otp_prompt", "escalation_generic"} & set(after)),
             "no post-verdict turn followed the idv_link turn")

    # E.164 normalization: varied inbound formats must land normalized in
    # thread state and (when committed) in the ticket's evidence bundle.
    for key, val in expect.get("e164_fields", {}).items():
        need(ts.fields.get(key) == val,
             f"field {key} = {ts.fields.get(key)!r}, expected E.164 {val!r}")
        if expect.get("commit_count") == 1:
            need(ticket.evidence.get(key) == val,
                 f"evidence {key} = {ticket.evidence.get(key)!r}, expected {val!r}")

    # Partial account identifiers: the idv_link email names BOTH endpoints by
    # last four digits; the FULL numbers (old and new, any common formatting)
    # never appear in any outbound email; last-four substrings are permitted
    # in the idv_link email ONLY.
    e164 = expect.get("e164_fields", {})
    number_variants = set()
    for num in filter(None, (e164.get("old_number"), e164.get("new_number"))):
        digits = re.sub(r"\D", "", num)
        national = digits[-10:]
        number_variants |= {num, digits, national,
                            f"{national[:3]}-{national[3:6]}-{national[6:]}",
                            f"({national[:3]}) {national[3:6]}-{national[6:]}",
                            f"{national[:3]} {national[3:6]} {national[6:]}"}
    for variant in sorted(number_variants):
        need(all(variant not in b for b in agent_bodies),
             f"full number variant {variant!r} appeared in an outbound email")
    if e164.get("old_number") and e164.get("new_number"):
        last4s = [re.sub(r"\D", "", e164["old_number"])[-4:],
                  re.sub(r"\D", "", e164["new_number"])[-4:]]
        link_idx = intents.index("idv_link") if "idv_link" in intents else None
        if link_idx is not None:
            need(all(l4 in all_bodies[link_idx] for l4 in last4s),
                 f"idv_link email lacks a last-four identifier ({last4s})")
        for i, body in enumerate(agent_bodies):
            if i == link_idx:
                continue
            for l4 in last4s:
                need(l4 not in body,
                     f"last-four {l4!r} appeared outside the idv_link email (reply #{i})")

    # Cross-ticket linking (fresh email referencing a closed ticket).
    if "linked_ticket" in expect:
        need(ticket.linked_ticket == expect["linked_ticket"],
             f"linked_ticket {ticket.linked_ticket!r} != {expect['linked_ticket']!r}")

    # Required action calls with exact counts; forbidden calls are exact zeros.
    counts = {name: sum(1 for a in ticket.actions if a["action"] == name)
              for name in MONITORED_ACTIONS}
    wanted = {name: expect.get("actions", {}).get(name, 0) for name in MONITORED_ACTIONS}
    need(counts == wanted, f"action counts {counts} != {wanted}")

    # The commit: exactly the expected number of printed internal API POSTs.
    commits = stdout.count(">>> INTERNAL API: POST")
    need(commits == expect["commit_count"],
         f"commit count {commits} != {expect['commit_count']}")

    # Content rules (rule 7 + drafter hard rules), asserted deterministically.
    # OTP codes are checked against FULL bodies (quotes included — a code must
    # never appear anywhere); the say-nothing rules use agent_bodies, because
    # the quoted opener is the user's own text echoed back.
    for code in otp_codes_seen:
        need(all(code not in b for b in all_bodies), f"OTP code {code} leaked into an email")
    for secret in expect.get("must_not_appear", []):
        need(all(secret.lower() not in b.lower() for b in agent_bodies),
             f"forbidden text {secret!r} appeared in an outbound email")
    # Scoped to the FINAL reply: e.g. the post-500 handoff email must not
    # claim success, while earlier emails may legitimately use the same words
    # ("once verification is complete…").
    for secret in expect.get("must_not_appear_last", []):
        need(bool(agent_bodies) and secret.lower() not in agent_bodies[-1].lower(),
             f"forbidden text {secret!r} appeared in the final outbound email")
    for check in expect.get("body_checks", []):
        idx = check["index"]
        need(idx < len(agent_bodies) and check["contains"].lower() in agent_bodies[idx].lower(),
             f"reply #{idx} does not contain {check['contains']!r}")

    # Escalation replies set the response-time expectation: the exact
    # "{SLA_BUSINESS_DAYS} business days" phrase (doc §7 placeholder).
    sla_phrase = f"{config.SLA_BUSINESS_DAYS} business days"
    for i, intent in enumerate(intents):
        if intent == "escalation_generic":
            need(sla_phrase in agent_bodies[i],
                 f"escalation reply #{i} lacks the '{sla_phrase}' expectation")

    # Attachments: logged with redacted=true, never anything else (rule 8).
    need(all(a.get("redacted") is True for a in ticket.attachments),
         "attachment recorded without redacted=true")
    if "attachments" in expect:
        need(ticket.attachments == expect["attachments"],
             f"attachments {ticket.attachments} != {expect['attachments']}")

    # Evidence bundle / authorize checks.
    for key in expect.get("evidence_has", []):
        need(key in ticket.evidence, f"evidence missing {key!r}")
    if "authorize_check_failures" in expect:
        checks = ticket.evidence.get("authorize_checks", {})
        failed = sorted(name for name, ok in checks.items() if not ok)
        need(failed == sorted(expect["authorize_check_failures"]),
             f"failed authorize checks {failed} != {expect['authorize_check_failures']}")

    # Escalations carry a case file; solved/closed/open tickets stay intact.
    if expect["status"] == "escalated":
        need(bool(ticket.case_file), "escalated ticket has no case file")
    if expect.get("ticket_intact"):
        need(len(ticket.transcript) >= 2 and len(ticket.state_history) >= 1,
             "ticket is not intact (transcript/state history missing)")

    # Printed side effects.
    for text in expect.get("required_stdout", []):
        need(text in stdout, f"expected stdout text {text!r} not found")
    for text in expect.get("forbidden_stdout", []):
        need(text not in stdout, f"forbidden stdout text {text!r} found")
    if "states_after" in expect:
        need(states_after == expect["states_after"],
             f"per-message states {states_after} != {expect['states_after']}")

    return failures


def main():
    scenarios = json.loads(SCENARIOS_PATH.read_text(encoding="utf-8"))
    only = {int(a) for a in sys.argv[1:]} or None
    classifier, drafter = Classifier(), Drafter()

    results = []
    for sc in scenarios:
        if only and sc["id"] not in only:
            continue
        start = time.time()
        try:
            failures, stdout = run_scenario(sc, classifier, drafter)
        except Exception as exc:  # a crash is a failure, not a suite abort
            failures, stdout = [f"crashed: {type(exc).__name__}: {exc}"], ""
        elapsed = time.time() - start
        ok = not failures
        results.append(ok)
        print(f"{'PASS' if ok else 'FAIL'}  scenario {sc['id']:>2}  "
              f"({elapsed:4.1f}s)  {sc['name']}")
        for f in failures:
            print(f"      - {f}")
        if failures and stdout:
            print("      --- captured agent output ---")
            for line in stdout.splitlines():
                print(f"      | {line}")

    passed = sum(results)
    print(f"\n{passed}/{len(results)} scenarios passed")
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    main()
