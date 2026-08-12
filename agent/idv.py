"""Mock IDV provider — session creation only, modeled on a hosted-link vendor
(rule 4 — no real writes; documents never touch email or the ticket).

`create_session` prints the session-creation API call and returns a session
id. The state machine emails the user the (clearly mock) hosted link
containing that id, and the id becomes the ticket's `idv_ref`.

The VERDICT is not this module's job: it arrives later as a separate event —
production: the vendor's webhook; MVP: `StateMachine.handle_idv_result`, fed
by the scenario in simulated mode, or by the console prompt in gmail mode
(the operator playing the webhook).
"""
from __future__ import annotations

import json
import uuid

VERIFICATION_LINK_BASE = "https://verify.partiful-demo.example/session/"


class IdvProvider:
    def create_session(self, name, user, now):
        session_id = f"idv_{uuid.uuid4().hex[:8]}"
        payload = {"name": name, "user_id": user["user_id"]}
        print(f">>> IDV API: POST /verification_sessions "
              f"{json.dumps(payload, ensure_ascii=False)} → session {session_id}")
        return session_id
