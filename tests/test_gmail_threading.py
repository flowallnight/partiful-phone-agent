"""GmailProvider conversation-identity unit tests (offline — no IMAP/SMTP).

Covers the live regression: Gmail groups conversations by subject as well as
References, so tagged subjects fork X-GM-THRID. Conversation identity must
come from our own Message-ID index:
  (a) In-Reply-To/References hit in the index → same thread
  (b) [PF-####] subject tag + sender matches the ticket's requester → thread
      (fallback for clients that strip References)
  spoof: tag present but sender ≠ requester → NEW thread
  (c) nothing recognizable → new thread
plus the single-tag invariant on outbound subjects.
"""
from __future__ import annotations

import sys
from email.message import EmailMessage
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent import config
from agent.email_provider import GmailProvider, tag_subject

JORDAN = "jordan.riverademo@yahoo.com"

# Allow offline construction on machines without a real .env.
config.SUPPORT_APP_PASSWORD = config.SUPPORT_APP_PASSWORD or "offline-test-only"


class FakeTicket:
    def __init__(self, requester_email):
        self.requester_email = requester_email


class FakeTicketStore:
    def __init__(self, tickets=None):
        self._tickets = tickets or {}

    def get(self, ticket_id):
        return self._tickets.get(ticket_id)


def make_provider(tmp_path, tickets=None):
    return GmailProvider(state_dir=tmp_path, ticket_store=FakeTicketStore(tickets))


def inbound(subject, sender=JORDAN, msgid=None, refs=None, in_reply_to=None):
    msg = EmailMessage()
    msg["From"] = f"Jordan Rivera <{sender}>"
    msg["Subject"] = subject
    if msgid:
        msg["Message-ID"] = msgid
    if refs:
        msg["References"] = " ".join(refs)
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    return msg


def test_references_resolution_survives_subject_mutation(tmp_path):
    # The regression case: our reply retags the subject; the user's reply must
    # still land on the same thread because it references our Message-ID.
    p = make_provider(tmp_path)
    tid, how = p._ingest(inbound("New phone number", msgid="<m1@yahoo>"), JORDAN)
    assert how == "new"
    p._record_outbound(tid, "<out1@partiful>", "PF-0001")

    reply = inbound("Re: New phone number [PF-0001]", msgid="<m2@yahoo>",
                    in_reply_to="<out1@partiful>", refs=["<m1@yahoo>", "<out1@partiful>"])
    tid2, how2 = p._ingest(reply, JORDAN)
    assert (tid2, how2) == (tid, "references")

    # Even a fully mutated subject cannot fork the thread.
    weird = inbound("totally different subject", msgid="<m3@yahoo>",
                    in_reply_to="<out1@partiful>")
    tid3, how3 = p._ingest(weird, JORDAN)
    assert (tid3, how3) == (tid, "references")


def test_references_resolution_survives_restart(tmp_path):
    p = make_provider(tmp_path)
    tid, _ = p._ingest(inbound("Opener", msgid="<m1@yahoo>"), JORDAN)
    p._record_outbound(tid, "<out1@partiful>", "PF-0001")
    p._save_state()

    fresh = make_provider(tmp_path)  # simulated restart: index reloaded
    tid2, how = fresh._ingest(
        inbound("Re: Opener [PF-0001]", msgid="<m2@yahoo>", in_reply_to="<out1@partiful>"),
        JORDAN)
    assert (tid2, how) == (tid, "references")


def test_subject_tag_fallback_when_references_stripped(tmp_path):
    p = make_provider(tmp_path, tickets={"PF-0001": FakeTicket(JORDAN)})
    tid, _ = p._ingest(inbound("Opener", msgid="<m1@yahoo>"), JORDAN)
    p._record_outbound(tid, "<out1@partiful>", "PF-0001")

    stripped = inbound("Re: Opener [PF-0001]", msgid="<m2@yahoo>")  # no refs at all
    tid2, how = p._ingest(stripped, JORDAN)
    assert (tid2, how) == (tid, "subject-tag")


def test_subject_tag_spoof_gets_new_thread(tmp_path):
    p = make_provider(tmp_path, tickets={"PF-0001": FakeTicket(JORDAN)})
    tid, _ = p._ingest(inbound("Opener", msgid="<m1@yahoo>"), JORDAN)
    p._record_outbound(tid, "<out1@partiful>", "PF-0001")

    spoof = inbound("Re: Opener [PF-0001]", sender="attacker@evil.example",
                    msgid="<mx@evil>")
    tid2, how = p._ingest(spoof, "attacker@evil.example")
    assert how == "new"
    assert tid2 != tid


def test_unrelated_mail_mints_new_thread(tmp_path):
    p = make_provider(tmp_path)
    tid1, how1 = p._ingest(inbound("Hello", msgid="<a@yahoo>"), JORDAN)
    tid2, how2 = p._ingest(inbound("Another thing", msgid="<b@yahoo>"), JORDAN)
    assert how1 == how2 == "new"
    assert tid1 != tid2


def test_tag_subject_single_tag_invariant():
    assert tag_subject("New phone number", "PF-0001") == "Re: New phone number [PF-0001]"
    # Idempotent on an already-tagged subject.
    assert tag_subject("Re: New phone number [PF-0001]", "PF-0001") \
        == "Re: New phone number [PF-0001]"
    # Retagging replaces, never stacks — the live regression's failure mode.
    assert tag_subject("Re: Need help [PF-0001]", "PF-0002") == "Re: Need help [PF-0002]"
    assert tag_subject("Re: X [PF-0001] [PF-0002]", "PF-0003") == "Re: X [PF-0003]"
    # No ticket id: tags still stripped, subject still Re:-prefixed.
    assert tag_subject("X [PF-0009]", None) == "Re: X"
    assert tag_subject("", "PF-0001") == "Re: (no subject) [PF-0001]"
