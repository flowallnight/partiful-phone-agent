"""Per-thread conversation state persisted as JSON under .state/ so a restart
never reprocesses or forgets a thread mid-flow."""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class ThreadState:
    thread_id: str
    state: str = "NEW"
    ticket_id: str | None = None
    fields: dict = field(default_factory=lambda: {
        "name": None, "old_number": None, "new_number": None, "has_old_access": None,
    })
    nudge_count: int = 0
    otp_attempts: int = 0
    unparseable_count: int = 0
    lookup_recheck_done: bool = False
    idv_session_id: str | None = None    # set on link send; verdict pending until idv_result
    idv_result: dict | None = None
    otp_pending: dict | None = None      # {"code", "expires_at"} — lives here, never in email
    otp_result: dict | None = None
    change_completed: bool = False
    ticket_ref_announced: bool = False   # first outbound email announces the ticket ref
    # Messages arriving after the ticket closed: preserved here (the closed
    # ticket record is immutable), so the thread's conversation history stays
    # complete for any human who reviews it.
    post_close_messages: list = field(default_factory=list)

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, d):
        return cls(**d)


class ThreadStore:
    def __init__(self, root=".state"):
        self.root = Path(root)

    def _path(self, thread_id):
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", str(thread_id))
        return self.root / f"{safe}.json"

    def get(self, thread_id) -> ThreadState:
        path = self._path(thread_id)
        if path.exists():
            return ThreadState.from_dict(json.loads(path.read_text(encoding="utf-8")))
        return ThreadState(thread_id=thread_id)

    def put(self, thread_state: ThreadState):
        self.root.mkdir(parents=True, exist_ok=True)
        self._path(thread_state.thread_id).write_text(
            json.dumps(thread_state.to_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
