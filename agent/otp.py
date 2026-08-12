"""Mock new-number OTP: generate, print, verify, expire (rule 4 — print only).

The state machine owns attempt counting, re-send-on-expiry, and the gate-facing
`otp_result`; this module only generates codes and checks candidates. Returned
dicts are strings-only so they survive the JSON round-trip through `.state/`.
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta

from agent import config


class Otp:
    def __init__(self, rng=None):
        # Injectable RNG so the test suite can run deterministically.
        self.rng = rng or random.Random()

    def send(self, new_number, now=None):
        code = f"{self.rng.randrange(10**6):06d}"
        print(f">>> SMS → new number: your Partiful code is {code}")
        expires_at = now + timedelta(minutes=config.OTP_TTL_MINUTES)
        return {"code": code, "expires_at": expires_at.isoformat()}

    def check(self, pending, candidate, now=None):
        if pending is None or candidate is None:
            return "missing"
        if now > datetime.fromisoformat(pending["expires_at"]):
            return "expired"
        return "pass" if candidate == pending["code"] else "wrong"
