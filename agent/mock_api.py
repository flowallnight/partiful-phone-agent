"""Mock user service, SMS prints, and the AUTHORIZE-gated phone-change commit.

Rule 4: no real writes — every action prints the request it would have been.
Rule 3: change_phone_number() is the only path to a committed change. It
re-derives account lookup and number availability itself rather than trusting
caller-supplied booleans, and it raises before printing anything when any
condition fails.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

from agent import config


class AuthorizationError(Exception):
    """The commit gate rejected an AuthorizationRecord."""

    def __init__(self, reason: str, detail: str = ""):
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason}: {detail}" if detail else reason)


class ApiFailure(Exception):
    """Simulated 5xx from the internal user service (scenario 15)."""


_users_cache = None
_fail_next_call = False


def _parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def load_users(path=None):
    global _users_cache
    if path is None and _users_cache is not None:
        return _users_cache
    with open(path or config.USERS_FIXTURE, encoding="utf-8") as f:
        users = json.load(f)
    if path is None:
        _users_cache = users
    return users


def lookup_user_by_phone(number, users=None, announce=False):
    """`announce=True` prints the GET the COLLECT step would issue; internal
    re-checks (AUTHORIZE, the commit gate) stay quiet."""
    users = users if users is not None else load_users()
    user = users.get(number)
    if announce:
        outcome = f"{user['user_id']} (account found)" if user else "no account found"
        print(f">>> INTERNAL API: GET /internal/users?phone={number} → {outcome}")
    return dict(user, phone=number) if user else None


def is_number_available(number, users=None):
    users = users if users is not None else load_users()
    return number not in users


def send_sms(to_label, body):
    print(f">>> SMS → {to_label}: {body}")


def fail_next_call():
    """Arm a one-shot stubbed 500 on the next commit (scenario 15)."""
    global _fail_next_call
    _fail_next_call = True


@dataclass(frozen=True)
class AuthorizationRecord:
    """Evidence bundle the gate evaluates.

    Deliberately has no name field: the user's name is case-file corroboration
    and an IDV-session input, never an authorization input (scoping doc §5).
    """

    ticket_id: str
    state: str                       # machine state at issuance; gate requires AUTHORIZE
    old_number: str
    new_number: str
    idv_result: dict | None          # {"verified", "ref", "verified_at", "expires_at"}
    otp_result: dict | None          # {"passed", "verified_at", "expires_at"}
    change_already_completed: bool = False


# Check name → escalation reason code, evaluated in this order (first fail wins).
CHECK_REASONS = {
    "unique_account_found": "account_not_found",
    "idv_verified_unexpired": "identity_verification_failed",
    "otp_passed_unexpired": "otp_failed",
    "new_number_available": "number_unavailable",
    "no_prior_completed_change": "already_completed",
}


def _result_ok(result, flag_key, now):
    if not result or not result.get(flag_key):
        return False
    expires_at = result.get("expires_at")
    return bool(expires_at) and _parse_ts(expires_at) > now


def evaluate_authorization(record, users=None, now=None):
    """Evaluate the five commit conditions; returns {check_name: bool}.

    Shared by the AUTHORIZE state (to record every check on the ticket) and by
    the gate itself, so the recorded evidence and the enforcement can't drift.
    """
    users = users if users is not None else load_users()
    now = now or datetime.now(timezone.utc)
    user = lookup_user_by_phone(record.old_number, users)
    return {
        "unique_account_found": user is not None,
        "idv_verified_unexpired": _result_ok(record.idv_result, "verified", now),
        "otp_passed_unexpired": _result_ok(record.otp_result, "passed", now),
        "new_number_available": is_number_available(record.new_number, users),
        "no_prior_completed_change": not record.change_already_completed,
    }


def change_phone_number(record: AuthorizationRecord, users=None, now=None):
    """The gated commit (rule 3). Raises AuthorizationError unless the record
    was minted in AUTHORIZE and all five conditions hold; prints the API call
    only after every check passes."""
    global _fail_next_call
    state_name = record.state.name if isinstance(record.state, Enum) else str(record.state)
    if state_name != "AUTHORIZE":
        raise AuthorizationError("invalid_state", f"commit attempted from {state_name}")

    checks = evaluate_authorization(record, users=users, now=now)
    for check, ok in checks.items():
        if not ok:
            raise AuthorizationError(CHECK_REASONS[check], f"failed check: {check}")

    user = lookup_user_by_phone(record.old_number, users)
    if _fail_next_call:
        # The stubbed 500 raises before the POST line prints so commit counts
        # (suite rule: commit ×1 in scenarios 2 and 8 only) stay unambiguous.
        _fail_next_call = False
        raise ApiFailure(f"500 Internal Server Error: POST /internal/users/{user['user_id']}/phone_number")

    print(
        f">>> INTERNAL API: POST /internal/users/{user['user_id']}/phone_number "
        f"{json.dumps({'new': record.new_number})}"
    )
    now = now or datetime.now(timezone.utc)
    return {
        "user_id": user["user_id"],
        "old_number": record.old_number,
        "new_number": record.new_number,
        "committed_at": now.isoformat(),
        "authorize_checks": checks,
    }
