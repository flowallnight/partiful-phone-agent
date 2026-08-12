"""JSON ticket records under tickets/{open,solved,escalated,closed}/ plus
printed Zendesk-shaped API calls (scoping doc §7).

The ticket is the operating record: it opens before classification (rule 1)
and accumulates the full transcript, state history, actions, and evidence.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

STATUSES = ("open", "solved", "escalated", "closed")


def _compact(payload):
    """Printed ticket-API payloads use compact JSON, matching the spec's
    example blocks byte-for-byte (`{"status":"closed","closed_by":"agent",…}`)."""
    return json.dumps(payload, separators=(",", ":"))


def _now_iso(now=None):
    return (now or datetime.now(timezone.utc)).isoformat()


@dataclass
class Ticket:
    id: str
    opened_at: str
    requester_email: str            # contact channel — never identity (§5)
    transcript: list = field(default_factory=list)
    state_history: list = field(default_factory=list)
    actions: list = field(default_factory=list)
    attachments: list = field(default_factory=list)
    status: str = "open"
    queue: str = "general"
    priority: str = "normal"
    reason_code: str | None = None
    evidence: dict = field(default_factory=dict)
    closed_by: str | None = None
    case_file: str | None = None    # populated on escalation
    linked_ticket: str | None = None  # a fresh ticket referencing a closed one

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, d):
        return cls(**d)


class TicketStore:
    def __init__(self, root="tickets"):
        self.root = Path(root)
        self._cache: dict[str, Ticket] = {}

    def _next_id(self):
        self.root.mkdir(parents=True, exist_ok=True)
        counter = self.root / ".counter"
        n = int(counter.read_text().strip() or 0) if counter.exists() else 0
        n += 1
        counter.write_text(str(n))
        return f"PF-{n:04d}"

    def open_ticket(self, requester_email, now=None):
        ticket = Ticket(
            id=self._next_id(),
            opened_at=_now_iso(now),
            requester_email=requester_email,
        )
        print(f">>> TICKET API: POST /tickets {_compact({'requester': requester_email, 'status': 'open'})}")
        self.save(ticket)
        return ticket

    def get(self, ticket_id):
        if ticket_id in self._cache:
            return self._cache[ticket_id]
        for status in STATUSES:
            path = self.root / status / f"{ticket_id}.json"
            if path.exists():
                ticket = Ticket.from_dict(json.loads(path.read_text(encoding="utf-8")))
                self._cache[ticket.id] = ticket
                return ticket
        return None

    def find_recent_closed(self, requester_email):
        """Most recently opened CLOSED ticket for this requester, or None.
        Used to link a fresh ticket that references a prior resolved issue;
        the closed record itself is never touched."""
        closed_dir = self.root / "closed"
        best = None
        if closed_dir.exists():
            for path in closed_dir.glob("PF-*.json"):
                t = Ticket.from_dict(json.loads(path.read_text(encoding="utf-8")))
                if t.requester_email == requester_email and (
                        best is None or t.opened_at > best.opened_at):
                    best = t
        return best

    def save(self, ticket):
        target_dir = self.root / ticket.status
        target_dir.mkdir(parents=True, exist_ok=True)
        for status in STATUSES:
            if status != ticket.status:
                stale = self.root / status / f"{ticket.id}.json"
                if stale.exists():
                    stale.unlink()
        (target_dir / f"{ticket.id}.json").write_text(
            json.dumps(ticket.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
        )
        self._cache[ticket.id] = ticket

    def add_message(self, ticket, direction, body, now=None):
        ticket.transcript.append({"at": _now_iso(now), "direction": direction, "body": body})
        self.save(ticket)

    def record_state(self, ticket, state, now=None):
        ticket.state_history.append({"at": _now_iso(now), "state": state})
        self.save(ticket)

    def record_action(self, ticket, action, reason, now=None):
        ticket.actions.append({"at": _now_iso(now), "action": action, "reason": reason})
        self.save(ticket)

    def add_attachment(self, ticket, filename, now=None):
        # Rule 8: filename only, always redacted, contents never touched.
        ticket.attachments.append({"filename": filename, "redacted": True})
        self.record_action(ticket, "attachment_logged", f"attachment '{filename}' recorded, never opened", now)

    def solve(self, ticket, now=None):
        ticket.status = "solved"
        print(f">>> TICKET API: PUT /tickets/{ticket.id} {_compact({'status': 'solved'})}")
        self.record_action(ticket, "ticket_solved", "self-serve link sent; a reply reopens", now)

    def reopen(self, ticket, now=None):
        ticket.status = "open"
        print(f">>> TICKET API: PUT /tickets/{ticket.id} {_compact({'status': 'open'})}")
        self.record_action(ticket, "ticket_reopened", "user replied after solved", now)

    def close(self, ticket, evidence=None, closed_by="agent", reason="phone_change_completed", now=None):
        # Rule 9: the agent closes only on phone_change_completed.
        ticket.status = "closed"
        ticket.closed_by = closed_by
        ticket.reason_code = reason
        if evidence:
            ticket.evidence.update(evidence)
        print(
            f">>> TICKET API: PUT /tickets/{ticket.id} "
            f"{_compact({'status': 'closed', 'closed_by': closed_by, 'reason': reason})} + evidence bundle"
        )
        self.record_action(ticket, "ticket_closed", reason, now)

    def escalate(self, ticket, reason_code, queue, case_file, tags=None, now=None):
        # Ownership transfers to a human; only a human closes it from here (rule 9).
        # Real reasons live on the ticket; the user-facing reply stays generic (rule 7).
        ticket.status = "escalated"
        ticket.queue = queue
        ticket.priority = "high" if queue == "security_review" else "normal"
        ticket.reason_code = reason_code
        ticket.case_file = case_file
        tags = tags or ["phone_change", reason_code]
        print(
            f">>> TICKET API: PUT /tickets/{ticket.id} "
            f"{_compact({'status': 'open', 'group': queue, 'priority': ticket.priority, 'tags': tags})}"
        )
        print(f">>> TICKET API: internal note on {ticket.id} — case file ({case_file})")
        print(
            f'>>> HELPDESK TRIGGER → assignee email: "Ticket {ticket.id} assigned: '
            f'phone change — {reason_code}" + link (pointer only, never the payload)'
        )
        self.record_action(ticket, "escalated", reason_code, now)
