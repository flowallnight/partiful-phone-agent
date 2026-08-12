"""The state machine — the ONLY module that fires actions (rule 2).

Transitions consume structured classifier/extractor output exclusively; email
prose never reaches transition logic, so instructions embedded in a message
cannot move state (rule 5). The classifier, drafter, IDV provider, and OTP
provider are all injected, which keeps Phase 1 fully offline (stubs in tests;
real modules arrive in Phase 2).

Injected dependency protocols:
  classifier.classify(subject, body) -> {"category", "confidence", "fields": {...},
      "wants_human": bool, "references_prior_issue": bool}  (all keys required;
      both booleans default false)
  drafter.draft(intent, facts) -> str (email body only, no tools)
  idv.create_session(name, user, now) -> session_id
      (prints '>>> IDV API: POST /verification_sessions ... → session {id}')
      The verdict does NOT come from the provider object: it arrives later as
      a separate event via handle_idv_result(thread_id, verified) — the
      production shape is the vendor's webhook.
  otp.send(new_number, now) -> {"code", "expires_at"}
      (prints '>>> SMS → new number: your Partiful code is NNNNNN')
  otp.check(pending, candidate, now) -> "pass" | "wrong" | "expired" | "missing"
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from enum import Enum

from agent import config, mock_api
from agent.idv import VERIFICATION_LINK_BASE
from agent.mock_api import ApiFailure, AuthorizationError, AuthorizationRecord


class State(Enum):
    NEW = "NEW"
    ASK_OLD_ACCESS = "ASK_OLD_ACCESS"
    SELF_SERVE = "SELF_SERVE"
    COLLECT = "COLLECT"
    VERIFY_IDENTITY = "VERIFY_IDENTITY"
    VERIFY_NEW_NUMBER = "VERIFY_NEW_NUMBER"
    AUTHORIZE = "AUTHORIZE"
    COMMIT = "COMMIT"
    RESOLVED = "RESOLVED"
    ESCALATED = "ESCALATED"


VALID_CATEGORIES = {"phone_number_change", "other_support", "unknown"}
QUOTE_SEPARATOR = "----- original message -----"
REQUIRED_FIELDS = ("name", "old_number", "new_number")
_OTP_CODE_RE = re.compile(r"\b(\d{6})\b")


def extract_otp_code(body):
    """Deterministic extractor: prose can only yield a 6-digit candidate token,
    which is then compared to the stored code — it can never instruct."""
    match = _OTP_CODE_RE.search(body or "")
    return match.group(1) if match else None


class StateMachine:
    def __init__(self, ticket_store, thread_store, classifier, drafter, idv, otp,
                 api=mock_api, now_fn=None):
        self.tickets = ticket_store
        self.threads = thread_store
        self.classifier = classifier
        self.drafter = drafter
        self.idv = idv
        self.otp = otp
        self.api = api
        self.now_fn = now_fn or (lambda: datetime.now(timezone.utc))

    # ------------------------------------------------------------- entrypoint

    def handle_message(self, thread_id, email):
        """Process one inbound email; returns the list of outbound replies
        (dicts with "intent" and "body") for the transport layer to send."""
        ts = self.threads.get(thread_id)

        # Rule 1: ticket on receipt — before classification, no exceptions.
        if ts.ticket_id is None:
            ticket = self.tickets.open_ticket(email.get("from", "unknown"), now=self._now())
            ts.ticket_id = ticket.id
            self.tickets.record_state(ticket, ts.state, now=self._now())
            self.threads.put(ts)
        else:
            ticket = self.tickets.get(ts.ticket_id)

        if ticket.status == "closed":
            # Closed is terminal and the record immutable — zero writes to it.
            return self._handle_post_close(ts, email)

        self.tickets.add_message(ticket, "inbound", email.get("body", ""), now=self._now())

        replies = []
        try:
            self._process(ts, ticket, email, replies)
        finally:
            for reply in replies:
                self.tickets.add_message(ticket, "outbound", reply["body"], now=self._now())
            self.threads.put(ts)
        return replies

    def _handle_post_close(self, ts, email):
        """Inbound on a closed thread: no reply, no reopen, no escalation, no
        classification — the message is preserved on the thread record so the
        conversation history stays complete. Sole exception (safety): an
        attachment still gets the redirect reply, and its filename is logged
        redacted here (never on the immutable closed ticket)."""
        attachments = [{"filename": f, "redacted": True}
                       for f in (email.get("attachments") or [])]
        ts.post_close_messages.append({
            "at": self._now().isoformat(),
            "from": email.get("from", "unknown"),
            "body": email.get("body", ""),
            "attachments": attachments,
        })
        replies = []
        for att in attachments:
            replies.append(self._draft(ts, self.tickets.get(ts.ticket_id),
                                       "attachment_redirect", {
                "filename": att["filename"],
                "note": "attachment not opened; verification happens in the secure flow",
            }))
        self.threads.put(ts)
        return replies

    def _process(self, ts, ticket, email, replies):
        for filename in email.get("attachments") or []:
            # Rule 8: log filename, redact, never open; redirect the user.
            self.tickets.add_attachment(ticket, filename, now=self._now())
            replies.append(self._draft(ts, ticket, "attachment_redirect", {
                "filename": filename,
                "note": "attachment not opened; verification happens in the secure flow",
            }))

        if State(ts.state) is State.ESCALATED:
            # A human owns this ticket now; the agent only records the message.
            return

        result = self._classify(email)
        if result is None:
            self._escalate(ts, ticket, "model_error", "general", replies,
                           failed="classifier returned malformed output twice",
                           next_step="triage this thread manually")
            return

        self._merge_fields(ts, result.get("fields") or {})

        try:
            self._dispatch(ts, ticket, email, result, replies)
        except (ApiFailure, AuthorizationError):
            raise  # handled where the commit fires
        except Exception as exc:
            # Rule 6: any tool failure fails toward humans, never toward the user.
            self._escalate(ts, ticket, "tool_failure", "general", replies,
                           failed=f"tool error: {exc}",
                           next_step="review the error and continue manually")

    def _dispatch(self, ts, ticket, email, result, replies):
        # Checked before state dispatch, so an explicit ask for a person
        # escalates from any state.
        if result["wants_human"]:
            self._escalate(ts, ticket, "user_requested_human", "general", replies,
                           failed="user explicitly asked for a person",
                           next_step="take over the conversation")
            return

        state = State(ts.state)
        if state is State.NEW:
            self._from_new(ts, ticket, result, replies)
        elif state is State.ASK_OLD_ACCESS:
            self._from_ask_old_access(ts, ticket, replies)
        elif state is State.SELF_SERVE:
            self._from_self_serve(ts, ticket, replies)
        elif state is State.COLLECT:
            self._enter_collect(ts, ticket, replies)
        elif state is State.VERIFY_NEW_NUMBER:
            self._from_verify_new_number(ts, ticket, email, replies)
        # RESOLVED is unreachable here: its ticket is closed, and closed
        # threads short-circuit into _handle_post_close before dispatch.

    # ------------------------------------------------------------ classifier

    def _classify(self, email):
        # Malformed output: retry once, then None → escalate model_error.
        for _attempt in (1, 2):
            try:
                raw = self.classifier.classify(email.get("subject", ""), email.get("body", ""))
            except Exception:
                raw = None
            parsed = self._validate_classification(raw)
            if parsed is not None:
                return parsed
        return None

    @staticmethod
    def _validate_classification(raw):
        # Structural validation is deterministic code — the model's output is
        # data, never trusted shape.
        if not isinstance(raw, dict):
            return None
        if raw.get("category") not in VALID_CATEGORIES:
            return None
        confidence = raw.get("confidence")
        if not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
            return None
        fields = raw.get("fields")
        if not isinstance(fields, dict):
            return None
        if not isinstance(raw.get("wants_human"), bool):
            return None
        if not isinstance(raw.get("references_prior_issue"), bool):
            return None
        return {"category": raw["category"], "confidence": float(confidence),
                "fields": fields, "wants_human": raw["wants_human"],
                "references_prior_issue": raw["references_prior_issue"]}

    @staticmethod
    def _merge_fields(ts, fields):
        # Latest non-null extraction wins (a corrected number must overwrite);
        # existing values are never nulled out, so nothing is ever re-asked.
        for key in ("name", "old_number", "new_number", "has_old_access"):
            if fields.get(key) is not None:
                ts.fields[key] = fields[key]

    # ----------------------------------------------------------- state logic

    def _from_new(self, ts, ticket, result, replies):
        # A fresh email may reference a previously resolved issue. Link the
        # tickets (new record only — the closed original is immutable) and
        # continue classifying/routing exactly as normal.
        if result["references_prior_issue"] and ticket.linked_ticket is None:
            prior = self.tickets.find_recent_closed(ticket.requester_email)
            if prior is not None and prior.id != ticket.id:
                ticket.linked_ticket = prior.id
                self.tickets.record_action(
                    ticket, "linked_ticket",
                    f"{ticket.id} references prior closed ticket {prior.id}; "
                    f"{prior.id} is referenced by {ticket.id} (closed record untouched)",
                    now=self._now())

        if result["confidence"] < config.CONFIDENCE_THRESHOLD:
            self._escalate(ts, ticket, "low_confidence", "general", replies,
                           failed=f"classifier confidence {result['confidence']:.2f} "
                                  f"below {config.CONFIDENCE_THRESHOLD}",
                           next_step="triage this thread manually")
        elif result["category"] == "other_support":
            self._escalate(ts, ticket, "other_support", "general", replies,
                           failed="not a phone number change — routed to a human",
                           next_step="handle as a normal support request",
                           tags=["other_support"])
        elif result["category"] == "unknown":
            self._escalate(ts, ticket, "unclassified", "general", replies,
                           failed="message could not be classified",
                           next_step="triage this thread manually")
        else:  # phone_number_change
            has_access = ts.fields.get("has_old_access")
            if has_access is True:
                self._self_serve(ts, ticket, replies)
            elif has_access is False:
                self._enter_collect(ts, ticket, replies)
            else:
                self._set_state(ts, ticket, State.ASK_OLD_ACCESS)
                replies.append(self._draft(ts, ticket, "ask_old_access", {
                    "question": "can you still receive texts on the old number?",
                }))

    def _from_ask_old_access(self, ts, ticket, replies):
        has_access = ts.fields.get("has_old_access")
        if has_access is True:
            ts.unparseable_count = 0
            self._self_serve(ts, ticket, replies)
        elif has_access is False:
            ts.unparseable_count = 0
            self._enter_collect(ts, ticket, replies)
        else:
            ts.unparseable_count += 1
            if ts.unparseable_count >= config.UNPARSEABLE_MAX:
                self._escalate(ts, ticket, "unparseable_replies", "general", replies,
                               failed="two consecutive replies could not be understood",
                               next_step="take over the conversation")
            else:
                replies.append(self._draft(ts, ticket, "ask_old_access", {
                    "question": "can you still receive texts on the old number?",
                    "repeat": True,
                }))

    def _self_serve(self, ts, ticket, replies):
        self._set_state(ts, ticket, State.SELF_SERVE)
        replies.append(self._draft(ts, ticket, "self_serve_link",
                                   {"url": config.SELF_SERVE_URL}))
        self.tickets.solve(ticket, now=self._now())

    def _from_self_serve(self, ts, ticket, replies):
        # A reply after solved means the link didn't work / no access → reopen
        # into COLLECT (rule 9).
        self.tickets.reopen(ticket, now=self._now())
        if ts.fields.get("has_old_access") is not False:
            ts.fields["has_old_access"] = False
        self._enter_collect(ts, ticket, replies)

    def _enter_collect(self, ts, ticket, replies):
        missing = [f for f in REQUIRED_FIELDS if not ts.fields.get(f)]
        if missing:
            if State(ts.state) is State.COLLECT:
                if ts.nudge_count >= config.NUDGE_MAX:
                    self._escalate(ts, ticket, "collect_incomplete", "general", replies,
                                   failed=f"still missing after {config.NUDGE_MAX} nudges: "
                                          f"{', '.join(missing)}",
                                   next_step="collect the remaining details manually")
                    return
                ts.nudge_count += 1
                replies.append(self._draft(ts, ticket, "collect_nudge",
                                           {"missing": missing}))
            else:
                self._set_state(ts, ticket, State.COLLECT)
                replies.append(self._draft(ts, ticket, "collect_ask",
                                           {"missing": missing}))
            return

        user = self.api.lookup_user_by_phone(ts.fields["old_number"], announce=True)
        if user is None:
            if not ts.lookup_recheck_done:
                # One enumeration-safe double-check ask, then escalate.
                ts.lookup_recheck_done = True
                if State(ts.state) is not State.COLLECT:
                    self._set_state(ts, ticket, State.COLLECT)
                replies.append(self._draft(ts, ticket, "number_recheck", {
                    "ask": "mind double-checking the old number?",
                }))
            else:
                self._escalate(ts, ticket, "account_not_found", "general", replies,
                               failed="old number matched no account after one re-check",
                               next_step="verify the account details with the user")
            return

        self._start_verification(ts, ticket, user, replies)

    def _start_verification(self, ts, ticket, user, replies):
        """Phase 1 of VERIFY_IDENTITY: create the session, email the link,
        END the turn. The verdict arrives later via handle_idv_result — so
        the link email actually sends before anything depends on the result."""
        self._set_state(ts, ticket, State.VERIFY_IDENTITY)
        session_id = self.idv.create_session(name=ts.fields.get("name"), user=user,
                                             now=self._now())
        ts.idv_session_id = session_id
        ticket.evidence["idv_ref"] = session_id
        # Enumeration-safe partial identifiers: last four digits of each
        # endpoint, computed here — the drafter never sees a full number.
        replies.append(self._draft(ts, ticket, "idv_link", {
            "verification_link": f"{VERIFICATION_LINK_BASE}{session_id}",
            "old_number_last4": re.sub(r"\D", "", ts.fields["old_number"])[-4:],
            "new_number_last4": re.sub(r"\D", "", ts.fields["new_number"])[-4:],
        }))
        self.tickets.record_action(ticket, "idv_link_sent",
                                   f"verification link emailed for session {session_id}",
                                   now=self._now())

    def handle_idv_result(self, thread_id, verified):
        """Phase 2 of VERIFY_IDENTITY — the provider's verdict as its own
        event (production: the vendor's webhook; MVP: scenario script or the
        console prompt). Rule 5 still holds: the input is a bool, never prose.
        Returns outbound replies exactly like handle_message."""
        ts = self.threads.get(thread_id)
        if (ts.ticket_id is None or State(ts.state) is not State.VERIFY_IDENTITY
                or not ts.idv_session_id or ts.idv_result is not None):
            return []  # not awaiting a verdict — a stray webhook does nothing
        ticket = self.tickets.get(ts.ticket_id)
        replies = []
        try:
            now = self._now()
            ts.idv_result = {
                "verified": bool(verified),
                "ref": ts.idv_session_id,
                "verified_at": now.isoformat(),
                "expires_at": (now + timedelta(minutes=config.IDV_TTL_MINUTES)).isoformat(),
            }
            self.tickets.record_action(
                ticket, "idv_webhook_received",
                f"session {ts.idv_session_id}: verified={bool(verified)}",
                now=self._now())
            if verified:
                self._send_otp(ts, ticket, replies)
            else:
                self._escalate(ts, ticket, "identity_verification_failed", "general",
                               replies,
                               failed="IDV provider returned not-verified",
                               next_step="review the IDV attempt and contact the user")
        finally:
            for reply in replies:
                self.tickets.add_message(ticket, "outbound", reply["body"], now=self._now())
            self.threads.put(ts)
        return replies

    def _send_otp(self, ts, ticket, replies):
        self._set_state(ts, ticket, State.VERIFY_NEW_NUMBER)
        ts.otp_pending = self.otp.send(ts.fields["new_number"], now=self._now())
        self.tickets.record_action(ticket, "otp_sent", "code texted to the new number",
                                   now=self._now())
        # The code lives in thread state and on the (printed) SMS only — never
        # in any email body.
        replies.append(self._draft(ts, ticket, "otp_prompt", {
            "ask": "reply with the 6-digit code we just texted to the new number",
        }))

    def _from_verify_new_number(self, ts, ticket, email, replies):
        candidate = extract_otp_code(email.get("body", ""))
        outcome = self.otp.check(ts.otp_pending, candidate, now=self._now())

        if outcome == "missing":
            ts.unparseable_count += 1
            if ts.unparseable_count >= config.UNPARSEABLE_MAX:
                self._escalate(ts, ticket, "unparseable_replies", "general", replies,
                               failed="two consecutive replies contained no code",
                               next_step="take over the conversation")
            else:
                replies.append(self._draft(ts, ticket, "otp_prompt", {
                    "ask": "reply with just the 6-digit code from the text",
                    "repeat": True,
                }))
            return

        ts.unparseable_count = 0
        if outcome == "pass":
            verified_at = self._now()
            ts.otp_result = {
                "passed": True,
                "verified_at": verified_at.isoformat(),
                "expires_at": (verified_at + timedelta(minutes=config.OTP_TTL_MINUTES)).isoformat(),
            }
            self.tickets.record_action(ticket, "otp_verified", "correct code from new number",
                                       now=self._now())
            self._authorize(ts, ticket, replies)
            return

        # wrong or expired
        ts.otp_attempts += 1
        self.tickets.record_action(ticket, "otp_attempt_failed", outcome, now=self._now())
        if ts.otp_attempts >= config.OTP_MAX_ATTEMPTS:
            self._escalate(ts, ticket, "otp_failed", "general", replies,
                           failed=f"OTP {outcome} on attempt {ts.otp_attempts}",
                           next_step="re-verify control of the new number manually")
            return
        if outcome == "expired":
            ts.otp_pending = self.otp.send(ts.fields["new_number"], now=self._now())
            self.tickets.record_action(ticket, "otp_sent", "fresh code after expiry",
                                       now=self._now())
        replies.append(self._draft(ts, ticket, "otp_retry", {"reason": outcome}))

    # ---------------------------------------------------- authorize + commit

    def _authorize(self, ts, ticket, replies):
        self._set_state(ts, ticket, State.AUTHORIZE)
        record = AuthorizationRecord(
            ticket_id=ticket.id,
            state=State.AUTHORIZE.name,
            old_number=ts.fields["old_number"],
            new_number=ts.fields["new_number"],
            idv_result=ts.idv_result,
            otp_result=ts.otp_result,
            change_already_completed=ts.change_completed,
        )
        checks = self.api.evaluate_authorization(record, now=self._now())
        ticket.evidence["authorize_checks"] = checks
        if ts.otp_result:
            ticket.evidence["otp_verified_at"] = ts.otp_result["verified_at"]
        self.tickets.record_action(
            ticket, "authorize_evaluated",
            "; ".join(f"{name}={'pass' if ok else 'FAIL'}" for name, ok in checks.items()),
            now=self._now(),
        )

        failed = [name for name, ok in checks.items() if not ok]
        if failed:
            reason = mock_api.CHECK_REASONS[failed[0]]
            # All gate failures route to general; security_review is reserved
            # for production risk signals (scoping doc §16).
            self._escalate(ts, ticket, reason, "general", replies,
                           failed=f"authorization checks failed: {', '.join(failed)}",
                           next_step="review the evidence bundle and decide")
            return
        self._commit(ts, ticket, record, replies)

    def _commit(self, ts, ticket, record, replies):
        self._set_state(ts, ticket, State.COMMIT)
        try:
            evidence = self.api.change_phone_number(record, now=self._now())
        except ApiFailure as exc:
            # No notifications, no success messaging — a teammate finishes (rule 6).
            self._escalate(ts, ticket, "api_failure", "general", replies,
                           failed=f"internal API call failed: {exc}",
                           next_step="complete the number change manually",
                           reply_intent="handoff_after_failure")
            return
        except AuthorizationError as exc:
            self._escalate(ts, ticket, exc.reason, "general", replies,
                           failed=f"commit gate rejected the record: {exc.reason}",
                           next_step="review the evidence bundle and decide")
            return

        # Strict order after a successful API call (spec COMMIT block, matched
        # line for line — the parenthetical annotates the content rule).
        self.api.send_sms(
            "old number",
            "a number change was made on this account; if this wasn't you, "
            "contact support (generic, zero PII)",
        )
        self.api.send_sms("new number", "this number is now linked to a Partiful account")
        print(f">>> EMAIL → thread: request complete, ticket #{ticket.id} (no account details)")
        replies.append(self._draft(ts, ticket, "commit_success", {}))
        self.tickets.close(ticket, evidence=evidence, now=self._now())
        ts.change_completed = True
        self._set_state(ts, ticket, State.RESOLVED)

    # -------------------------------------------------------------- plumbing

    def _escalate(self, ts, ticket, reason, queue, replies, failed, next_step,
                  tags=None, reply_intent="escalation_generic"):
        case_file = (
            f"who: {ticket.requester_email} (claimed name: {ts.fields.get('name') or 'n/a'}) · "
            f"what they asked: {self._asked_summary(ts)} · "
            f"what was verified: {self._verified_summary(ts)} · "
            f"what failed: {failed} · "
            f"recommended next step: {next_step}"
        )
        self.tickets.escalate(ticket, reason, queue, case_file, tags=tags, now=self._now())
        self._set_state(ts, ticket, State.ESCALATED)
        # Generic on purpose: real reasons live on the ticket only (rule 7).
        facts = {"note": "a teammate will take it from here"}
        if reply_intent == "escalation_generic":
            # Response-time expectation — business-policy placeholder (doc §7).
            facts["sla_business_days"] = config.SLA_BUSINESS_DAYS
        replies.append(self._draft(ts, ticket, reply_intent, facts))

    @staticmethod
    def _asked_summary(ts):
        old = ts.fields.get("old_number")
        new = ts.fields.get("new_number")
        if old or new:
            return f"phone number change ({old or '?'} → {new or '?'})"
        return "support request (details on transcript)"

    @staticmethod
    def _verified_summary(ts):
        verified = []
        if ts.idv_result and ts.idv_result.get("verified"):
            verified.append("identity (IDV)")
        if ts.otp_result and ts.otp_result.get("passed"):
            verified.append("new-number OTP")
        return ", ".join(verified) or "nothing yet"

    def _draft(self, ts, ticket, intent, facts):
        facts = dict(facts)
        facts["ticket_reference"] = ticket.id
        first_reply = not ts.ticket_ref_announced
        if first_reply:
            # The first outbound email on any ticket announces it (exact
            # phrase "Ticket Reference: PF-####" — asserted in the suite).
            facts["announce_ticket"] = True
            ts.ticket_ref_announced = True
        body = self.drafter.draft(intent, facts)
        if first_reply:
            # Deterministic quote of the opening message, appended in code —
            # the drafter never sees user text, so it can never alter it.
            body = f"{body}\n\n{self._quote_original(ticket)}"
        return {"intent": intent, "body": body, "ticket_id": ticket.id}

    @staticmethod
    def _quote_original(ticket):
        opener = ticket.transcript[0]
        at = datetime.fromisoformat(opener["at"])
        lines = (opener.get("body") or "").splitlines() or [""]
        quoted = "\n".join(f"> {line}" for line in lines)
        return (f"{QUOTE_SEPARATOR}\n"
                f"On {at:%Y-%m-%d} at {at:%H:%M} {at.tzname() or 'UTC'}, "
                f"{ticket.requester_email} wrote:\n{quoted}")

    def _set_state(self, ts, ticket, new_state):
        previous = ts.state
        ts.state = new_state.value
        self.tickets.record_state(ticket, new_state.value, now=self._now())
        print(f"[{ticket.id}] {previous} → {new_state.value}")

    def _now(self):
        return self.now_fn()
