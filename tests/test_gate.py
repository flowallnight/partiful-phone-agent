"""Commit-gate unit tests (CLAUDE.md rule 3) plus the ticket-on-receipt
invariant (rule 1).

The gate must fail from every state except AUTHORIZE and with each of the five
conditions individually false — and a failing gate must produce zero side
effects (no `>>> INTERNAL API` line ever prints on a raising path).
"""
import dataclasses
from datetime import datetime, timedelta, timezone

import pytest

from agent import mock_api
from agent.mock_api import ApiFailure, AuthorizationError, AuthorizationRecord
from agent.state_machine import State, StateMachine
from agent.store import ThreadStore
from agent.ticket_store import TicketStore

NOW = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)

# A self-contained user DB so the suite never goes stale as real time passes.
USERS = {
    "+14155550111": {"user_id": "u_1001", "name": "Jordan Rivera",
                     "last_active": "2025-11-20T18:30:00+00:00"},
    "+14155550122": {"user_id": "u_1002", "name": "Priya Natarajan",
                     "last_active": "2026-08-09T21:15:00+00:00"},   # 2 days ago — activity never gates
    "+14155550144": {"user_id": "u_1004", "name": "🎉 Mari 🎉",
                     "last_active": "2026-01-15T12:00:00+00:00"},
    "+14155550166": {"user_id": "u_1006", "name": "Lena Fischer",
                     "last_active": "2026-02-10T16:20:00+00:00"},   # "already in use" target
}


def iso(dt):
    return dt.isoformat()


def valid_idv():
    return {"verified": True, "ref": "idv_test_001",
            "verified_at": iso(NOW - timedelta(minutes=5)),
            "expires_at": iso(NOW + timedelta(minutes=25))}


def valid_otp():
    return {"passed": True,
            "verified_at": iso(NOW - timedelta(minutes=1)),
            "expires_at": iso(NOW + timedelta(minutes=9))}


def valid_record(**overrides):
    base = dict(
        ticket_id="PF-0001",
        state=State.AUTHORIZE.name,
        old_number="+14155550111",
        new_number="+14155550187",
        idv_result=valid_idv(),
        otp_result=valid_otp(),
        change_already_completed=False,
    )
    base.update(overrides)
    return AuthorizationRecord(**base)


def commit(record):
    return mock_api.change_phone_number(record, users=USERS, now=NOW)


# ------------------------------------------------------------- happy path

def test_commit_succeeds_when_all_conditions_true(capsys):
    evidence = commit(valid_record())
    out = capsys.readouterr().out
    assert out.count(">>> INTERNAL API") == 1
    assert '>>> INTERNAL API: POST /internal/users/u_1001/phone_number {"new": "+14155550187"}' in out
    assert evidence["user_id"] == "u_1001"
    assert all(evidence["authorize_checks"].values())


# ------------------------------------- fails from every non-AUTHORIZE state

@pytest.mark.parametrize("state", [s for s in State if s is not State.AUTHORIZE])
def test_gate_fails_from_every_other_state(state, capsys):
    with pytest.raises(AuthorizationError) as excinfo:
        commit(valid_record(state=state.name))
    assert excinfo.value.reason == "invalid_state"
    assert capsys.readouterr().out == ""


# ------------------------------------- each condition individually false

def expired(result):
    return dict(result, expires_at=iso(NOW - timedelta(minutes=1)))


@pytest.mark.parametrize("overrides,reason", [
    # 1. no unique account found by old number
    ({"old_number": "+19998887777"}, "account_not_found"),
    # 2. IDV: not verified / expired / absent
    ({"idv_result": dict(valid_idv(), verified=False)}, "identity_verification_failed"),
    ({"idv_result": expired(valid_idv())}, "identity_verification_failed"),
    ({"idv_result": None}, "identity_verification_failed"),
    # 3. OTP: not passed / expired / absent
    ({"otp_result": dict(valid_otp(), passed=False)}, "otp_failed"),
    ({"otp_result": expired(valid_otp())}, "otp_failed"),
    ({"otp_result": None}, "otp_failed"),
    # 4. new number already on another account (Lena's)
    ({"new_number": "+14155550166"}, "number_unavailable"),
    # 5. a completed change already exists on this ticket
    ({"change_already_completed": True}, "already_completed"),
], ids=[
    "no-account", "idv-not-verified", "idv-expired", "idv-missing",
    "otp-not-passed", "otp-expired", "otp-missing",
    "number-unavailable", "already-completed",
])
def test_each_condition_individually_false(overrides, reason, capsys):
    with pytest.raises(AuthorizationError) as excinfo:
        commit(valid_record(**overrides))
    assert excinfo.value.reason == reason
    # A failing gate has zero side effects.
    assert ">>> INTERNAL API" not in capsys.readouterr().out


# --------------------------------------------------------- names never gate

def test_names_never_gate(capsys):
    # The record has no name field at all — a name cannot be an authorization
    # input (scoping doc §5) — and an emoji-named account commits fine.
    assert "name" not in {f.name for f in dataclasses.fields(AuthorizationRecord)}
    commit(valid_record(old_number="+14155550144"))
    assert capsys.readouterr().out.count(">>> INTERNAL API") == 1


def test_recent_activity_never_gates(capsys):
    # Priya was active 2 days ago; recent activity is deliberately NOT a gate
    # condition (scoping doc §12 decision trail), so the commit proceeds.
    commit(valid_record(old_number="+14155550122"))
    assert capsys.readouterr().out.count(">>> INTERNAL API") == 1


# ------------------------------------------------------------- stubbed 500

def test_stubbed_500_raises_after_gate_with_no_success_output(capsys):
    mock_api.fail_next_call()
    with pytest.raises(ApiFailure):
        commit(valid_record())
    assert ">>> INTERNAL API" not in capsys.readouterr().out
    # One-shot: the next commit works again.
    commit(valid_record())
    assert capsys.readouterr().out.count(">>> INTERNAL API") == 1


# ------------------------------------------------------------ real fixture

def test_fixture_personas():
    users = mock_api.load_users()
    assert len(users) == 6
    assert users["+14155550111"]["name"] == "Jordan Rivera"
    # last_active is retained on every account as data for future production
    # risk signals (scoping doc §16); nothing in the MVP gate reads it.
    assert all("last_active" in u for u in users.values())
    assert "+14155550187" not in users                                  # happy-path NEW number is free


# ------------------------------------------------- rule 1: ticket on receipt

class ExplodingClassifier:
    """Fails every call — and records what tickets existed when it ran."""

    def __init__(self, tickets_root):
        self.tickets_root = tickets_root
        self.tickets_seen_at_classify = None

    def classify(self, subject, body):
        self.tickets_seen_at_classify = sorted(
            p.name for p in self.tickets_root.rglob("PF-*.json"))
        raise RuntimeError("classifier down")


class StubDrafter:
    def draft(self, intent, facts):
        return f"[{intent}]"


class StubIdv:
    def create_session(self, name, user, now):
        raise AssertionError("IDV must not run in this test")

    def result(self, session_id, now):
        raise AssertionError("IDV must not run in this test")


class StubOtp:
    def send(self, new_number, now):
        raise AssertionError("OTP must not run in this test")

    def check(self, pending, candidate, now):
        raise AssertionError("OTP must not run in this test")


class ScriptedClassifier:
    """Returns canned classification results in order; repeats the last one."""

    def __init__(self, *results):
        self.results = list(results)

    def classify(self, subject, body):
        return self.results.pop(0) if len(self.results) > 1 else self.results[0]


def classification(**overrides):
    base = {"category": "phone_number_change", "confidence": 0.95,
            "fields": {}, "wants_human": False, "references_prior_issue": False}
    base.update(overrides)
    return base


def build_machine(tmp_path, classifier):
    tickets = TicketStore(root=tmp_path / "tickets")
    threads = ThreadStore(root=tmp_path / "state")
    machine = StateMachine(tickets, threads, classifier, StubDrafter(), StubIdv(), StubOtp())
    return machine, tickets, threads


def test_wants_human_escalates_from_any_state(tmp_path):
    machine, tickets, threads = build_machine(tmp_path, ScriptedClassifier(
        classification(),                    # message 1: normal → ASK_OLD_ACCESS
        classification(wants_human=True),    # message 2: explicit ask for a person
    ))
    email = {"from": "jordan.riverademo@yahoo.com", "subject": "help", "body": "…"}

    machine.handle_message("thread-1", email)
    assert threads.get("thread-1").state == "ASK_OLD_ACCESS"

    machine.handle_message("thread-1", email)
    assert threads.get("thread-1").state == "ESCALATED"
    ticket = tickets.get(threads.get("thread-1").ticket_id)
    assert ticket.status == "escalated"
    assert ticket.reason_code == "user_requested_human"
    assert ticket.queue == "general"


def test_missing_wants_human_is_malformed(tmp_path):
    # wants_human is a required key: output without it is malformed →
    # retry once, then model_error.
    result = classification()
    del result["wants_human"]
    machine, tickets, threads = build_machine(tmp_path, ScriptedClassifier(result))

    machine.handle_message("thread-1", {"from": "a@b.c", "subject": "s", "body": "b"})
    ticket = tickets.get(threads.get("thread-1").ticket_id)
    assert ticket.status == "escalated"
    assert ticket.reason_code == "model_error"


def test_ticket_opened_before_classification(tmp_path):
    tickets_root = tmp_path / "tickets"
    tickets = TicketStore(root=tickets_root)
    threads = ThreadStore(root=tmp_path / "state")
    classifier = ExplodingClassifier(tickets_root)
    machine = StateMachine(tickets, threads, classifier, StubDrafter(), StubIdv(), StubOtp())

    machine.handle_message("thread-1", {"from": "jordan.riverademo@yahoo.com",
                                        "subject": "help", "body": "hi"})

    # The ticket existed on disk before the classifier ever ran (rule 1)...
    assert classifier.tickets_seen_at_classify == ["PF-0001.json"]
    # ...and the classifier blowing up still left a clean paper trail.
    ticket = tickets.get(threads.get("thread-1").ticket_id)
    assert ticket.status == "escalated"
    assert ticket.reason_code == "model_error"
    assert ticket.case_file
