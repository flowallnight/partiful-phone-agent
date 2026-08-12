"""EmailProvider ABC + SimulatedProvider + GmailProvider (Phase 3).

The provider is the only transport layer: the state machine returns replies,
the provider sends them. Message shape consumed by the state machine:
    {"from": str, "subject": str, "body": str, "attachments": [filename, ...]}
"""
from __future__ import annotations

import email
import email.policy
import imaplib
import json
import re
import smtplib
from abc import ABC, abstractmethod
from email.message import EmailMessage
from email.utils import make_msgid, parseaddr
from pathlib import Path

from agent import config

_TICKET_TAG_RE = re.compile(r"\[(PF-\d+)\]")


def tag_subject(subject, ticket_id):
    """`Re: {original subject} [PF-####]` with exactly ONE tag, ever: any
    pre-existing [PF-####] tags are stripped before the current one is
    appended. Threading never depends on the subject (see GmailProvider);
    the tag is for humans."""
    base = _TICKET_TAG_RE.sub("", (subject or "(no subject)")).strip()
    base = re.sub(r"\s{2,}", " ", base) or "(no subject)"
    if not base.lower().startswith("re:"):
        base = f"Re: {base}"
    if ticket_id:
        base = f"{base} [{ticket_id}]"
    return base


class EmailProvider(ABC):
    @abstractmethod
    def fetch(self):
        """Return new inbound messages as a list of (thread_id, email_dict)."""

    @abstractmethod
    def send(self, thread_id, body, intent=None, ticket_id=None):
        """Send a reply on the given thread; ticket_id feeds the subject tag."""


class SimulatedProvider(EmailProvider):
    """In-memory inbox/outbox: development, the automated suite, demo fallback."""

    def __init__(self):
        self._queue = []
        self.outbox = []
        self._subjects = {}  # thread_id → original opener subject

    def deliver(self, thread_id, email):
        """Test/demo hook: enqueue an inbound message as if it just arrived."""
        self._subjects.setdefault(thread_id, email.get("subject", ""))
        self._queue.append((thread_id, email))

    def fetch(self):
        messages, self._queue = self._queue, []
        return messages

    def send(self, thread_id, body, intent=None, ticket_id=None):
        self.outbox.append({
            "thread_id": thread_id, "intent": intent, "body": body,
            "subject": tag_subject(self._subjects.get(thread_id, ""), ticket_id),
        })


_UIDVALIDITY_RE = re.compile(rb"UIDVALIDITY (\d+)")
_UIDNEXT_RE = re.compile(rb"UIDNEXT (\d+)")
_THRID_RE = re.compile(rb"X-GM-THRID (\d+)")


class GmailProvider(EmailProvider):
    """IMAP poll + SMTP on the support inbox, app-password auth (no OAuth).

    - Conversation identity is OURS, not Gmail's: every seen Message-ID —
      inbound and our own outbound (minted with make_msgid on every send) —
      maps to a stable internal thread key in a persistent index. Gmail
      groups conversations by subject as well as References, so our tagged
      subjects fork X-GM-THRID; it is recorded only as a hint, never keyed on.
    - Inbound resolution order: (a) any Message-ID from In-Reply-To/References
      found in the index; (b) a [PF-####] subject tag whose ticket's requester
      matches the sender (fallback for clients that strip References);
      (c) a freshly minted thread.
    - Restart safety: UIDVALIDITY + highest processed UID + the whole
      conversation index persisted under .state/.
    - Attachments (rule 8): filenames recorded from part headers only — part
      content is never decoded.
    Credentials come from config (.env) and are never printed or logged.
    """

    _STATE_DEFAULTS = {"uidvalidity": None, "last_uid": 0, "next_conv": 1,
                       "threads": {}, "message_index": {}, "tickets": {}}

    def __init__(self, state_dir=".state", ticket_store=None):
        if not config.SUPPORT_EMAIL or not config.SUPPORT_APP_PASSWORD:
            raise SystemExit(
                "GmailProvider needs SUPPORT_EMAIL and SUPPORT_APP_PASSWORD in .env "
                "(Gmail: enable 2FA, then create one at myaccount.google.com/apppasswords)"
            )
        # Used only to check a subject-tag fallback against the ticket's
        # requester; without it, path (b) is disabled (fail toward new thread).
        self._ticket_store = ticket_store
        self._state_path = Path(state_dir) / "gmail_provider.json"
        self._state = self._load_state()

    # ------------------------------------------------------------ persistence

    def _load_state(self):
        state = dict(self._STATE_DEFAULTS)
        if self._state_path.exists():
            state.update(json.loads(self._state_path.read_text(encoding="utf-8")))
        return state

    def _save_state(self):
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        self._state_path.write_text(
            json.dumps(self._state, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    # ------------------------------------------------------------------ fetch

    def fetch(self):
        imap = imaplib.IMAP4_SSL(config.SUPPORT_IMAP_HOST)
        try:
            imap.login(config.SUPPORT_EMAIL, config.SUPPORT_APP_PASSWORD)
            typ, data = imap.select("INBOX", readonly=False)
            if typ != "OK":
                raise RuntimeError("could not select INBOX")

            typ, status = imap.status("INBOX", "(UIDVALIDITY UIDNEXT)")
            match = _UIDVALIDITY_RE.search(status[0] or b"")
            uidvalidity = int(match.group(1)) if match else None
            if uidvalidity != self._state.get("uidvalidity"):
                # First run or reissued UIDs: baseline to the current end of
                # the mailbox so pre-existing mail is never (re)processed —
                # only messages arriving from now on count.
                next_match = _UIDNEXT_RE.search(status[0] or b"")
                self._state["uidvalidity"] = uidvalidity
                self._state["last_uid"] = int(next_match.group(1)) - 1 if next_match else 0
                self._save_state()

            last_uid = int(self._state.get("last_uid") or 0)
            typ, found = imap.uid("SEARCH", None, f"UID {last_uid + 1}:*")
            if typ != "OK":
                raise RuntimeError("UID SEARCH failed")
            # `N:*` always matches the highest-UID message, so filter properly.
            uids = sorted(int(u) for u in (found[0] or b"").split() if int(u) > last_uid)

            messages = []
            for uid in uids:
                fetched = self._fetch_one(imap, uid)
                if fetched is not None:
                    messages.append(fetched)
                self._state["last_uid"] = uid
            if uids:
                self._save_state()
            return messages
        finally:
            try:
                imap.logout()
            except Exception:
                pass

    def _fetch_one(self, imap, uid):
        typ, data = imap.uid("FETCH", str(uid), "(X-GM-THRID BODY.PEEK[])")
        if typ != "OK":
            raise RuntimeError(f"UID FETCH {uid} failed")
        header_meta, raw = next(
            ((item[0], item[1]) for item in data if isinstance(item, tuple)), (None, None)
        )
        if raw is None:
            return None
        thrid_match = _THRID_RE.search(header_meta or b"")
        gm_thrid = thrid_match.group(1).decode() if thrid_match else None

        msg = email.message_from_bytes(raw, policy=email.policy.default)
        sender = parseaddr(msg.get("From", ""))[1]
        if sender.lower() == config.SUPPORT_EMAIL.lower():
            return None  # our own mail — never self-process

        thread_id, how = self._ingest(msg, sender, gm_thrid=gm_thrid)
        print(f"[gmail] inbound resolved → {thread_id} (via {how})")
        body, attachments = self._extract_content(msg)
        return thread_id, {
            "from": sender,
            "subject": msg.get("Subject", "") or "",
            "body": body,
            "attachments": attachments,
        }

    # -------------------------------------------------- conversation identity

    def _ingest(self, msg, sender, gm_thrid=None):
        """Resolve the message to an internal thread key, index its
        Message-ID, and refresh reply metadata. Returns (thread_id, how)."""
        thread_id, how = self._resolve_thread(msg, sender)
        message_id = (msg.get("Message-ID") or "").strip()
        if message_id:
            self._state["message_index"][message_id] = thread_id
        self._remember_thread(thread_id, msg, sender, gm_thrid)
        return thread_id, how

    def _resolve_thread(self, msg, sender):
        # (a) any referenced Message-ID we have seen (ours or theirs).
        refs = f"{msg.get('References') or ''} {msg.get('In-Reply-To') or ''}".split()
        for ref in refs:
            thread_id = self._state["message_index"].get(ref)
            if thread_id:
                return thread_id, "references"
        # (b) subject tag, only when the sender matches the ticket's requester
        # (a tag alone is spoofable — enumeration-safe fallback, not trust).
        tag = _TICKET_TAG_RE.search(msg.get("Subject") or "")
        if tag:
            ticket_id = tag.group(1)
            thread_id = self._state["tickets"].get(ticket_id)
            requester = self._requester_of(ticket_id)
            if thread_id and requester and requester.lower() == sender.lower():
                return thread_id, "subject-tag"
        # (c) a genuinely new conversation.
        return self._mint_thread(), "new"

    def _mint_thread(self):
        n = int(self._state.get("next_conv") or 1)
        self._state["next_conv"] = n + 1
        return f"conv-{n:04d}"

    def _requester_of(self, ticket_id):
        if self._ticket_store is None:
            return None
        ticket = self._ticket_store.get(ticket_id)
        return getattr(ticket, "requester_email", None) if ticket else None

    @staticmethod
    def _extract_content(msg):
        """First text/plain part + attachment filenames. Attachment parts are
        identified by their headers and NEVER decoded (rule 8)."""
        body = None
        attachments = []
        for part in msg.walk():
            if part.get_content_maintype() == "multipart":
                continue
            filename = part.get_filename()
            if filename or part.get_content_disposition() == "attachment":
                attachments.append(filename or "unnamed-attachment")
                continue
            if part.get_content_type() == "text/plain" and body is None:
                try:
                    body = part.get_content()
                except Exception:
                    body = None
        return (body or "").strip(), attachments

    def _remember_thread(self, thread_id, msg, sender, gm_thrid=None):
        """Keep what a threaded reply needs: recipient, subject, and the
        accumulated Message-ID/References chain. X-GM-THRID is a hint only."""
        meta = self._state["threads"].get(thread_id) or {}
        message_id = (msg.get("Message-ID") or "").strip()
        references = list(meta.get("references") or [])
        for ref in (msg.get("References") or "").split() + ([message_id] if message_id else []):
            if ref not in references:
                references.append(ref)
        meta.update({
            "to": sender,
            "subject": msg.get("Subject", "") or "(no subject)",
            "last_message_id": message_id or meta.get("last_message_id", ""),
            "references": references,
        })
        if gm_thrid:
            meta["gm_thrid_hint"] = gm_thrid
        self._state["threads"][thread_id] = meta

    def _record_outbound(self, thread_id, message_id, ticket_id):
        """Index our own outbound Message-ID (the user's reply will reference
        it) and remember which thread a ticket's tag belongs to."""
        meta = self._state["threads"].get(thread_id) or {}
        self._state["message_index"][message_id] = thread_id
        refs = list(meta.get("references") or [])
        if message_id not in refs:
            refs.append(message_id)
        meta["references"] = refs
        self._state["threads"][thread_id] = meta
        if ticket_id:
            self._state["tickets"][ticket_id] = thread_id
        self._save_state()

    # ------------------------------------------------------------------- send

    def send(self, thread_id, body, intent=None, ticket_id=None):
        meta = self._state["threads"].get(thread_id)
        if meta is None:
            print(f"[gmail] no thread metadata for {thread_id}; cannot send reply")
            return

        reply = EmailMessage()
        reply["From"] = config.SUPPORT_EMAIL
        reply["To"] = meta["to"]
        reply["Subject"] = tag_subject(meta["subject"], ticket_id)
        message_id = make_msgid()
        reply["Message-ID"] = message_id
        if meta.get("last_message_id"):
            reply["In-Reply-To"] = meta["last_message_id"]
        if meta.get("references"):
            reply["References"] = " ".join(meta["references"])
        reply.set_content(body)

        with smtplib.SMTP_SSL(config.SUPPORT_SMTP_HOST, 465) as smtp:
            smtp.login(config.SUPPORT_EMAIL, config.SUPPORT_APP_PASSWORD)
            smtp.send_message(reply)
        self._record_outbound(thread_id, message_id, ticket_id)
        print(f"[gmail] reply sent ({intent or 'reply'}) → {meta['to']}")
