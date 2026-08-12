"""Entrypoint: python -m agent.main --provider simulated|gmail

- gmail: poll the support inbox over IMAP every POLL_SECONDS, run each new
  message through the state machine, send replies over SMTP. IDV results are
  answered on this console (`IDV result [pass/fail]:`).
- simulated: interactive dev loop — type an email at the console, see the
  agent's replies inline. Same state machine, in-memory transport.
"""
from __future__ import annotations

import argparse
import sys
import time

# The demo output uses arrows/bullets; Windows consoles often default to
# cp1252, which would crash the first `>>> SMS →` print.
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from agent import config
from agent.classifier import Classifier
from agent.drafter import Drafter
from agent.email_provider import GmailProvider, SimulatedProvider
from agent.idv import IdvProvider
from agent.otp import Otp
from agent.state_machine import StateMachine
from agent.store import ThreadStore
from agent.ticket_store import TicketStore


def build_machine(tickets=None):
    return StateMachine(
        ticket_store=tickets if tickets is not None else TicketStore(),
        thread_store=ThreadStore(),
        classifier=Classifier(),
        drafter=Drafter(),
        idv=IdvProvider(),
        otp=Otp(),
    )


def _handle(machine, provider, thread_id, message):
    print(f"\n[inbound] {message['from']} · subject: {message['subject']!r}"
          + (f" · attachments: {message['attachments']}" if message["attachments"] else ""))
    replies = machine.handle_message(thread_id, message)
    for reply in replies:
        provider.send(thread_id, reply["body"], reply["intent"], reply.get("ticket_id"))
    _maybe_idv_webhook(machine, provider, thread_id)


def _maybe_idv_webhook(machine, provider, thread_id):
    """After a turn's replies have actually been sent: if the thread is
    awaiting an IDV verdict, the console operator plays the provider webhook.
    (The link email is already out — this is the event that follows it.)"""
    ts = machine.threads.get(thread_id)
    while ts.state == "VERIFY_IDENTITY" and ts.idv_session_id and ts.idv_result is None:
        answer = input(
            f"IDV webhook for session {ts.idv_session_id} [pass/fail]: ").strip().lower()
        if answer not in ("pass", "fail"):
            print("please type 'pass' or 'fail'")
            continue
        replies = machine.handle_idv_result(thread_id, answer == "pass")
        for reply in replies:
            provider.send(thread_id, reply["body"], reply["intent"], reply.get("ticket_id"))
        break


def run_gmail():
    # The provider shares the ticket store so its subject-tag fallback can
    # check a tag against the ticket's requester before trusting it.
    tickets = TicketStore()
    provider = GmailProvider(ticket_store=tickets)
    machine = build_machine(tickets)
    print(f"Polling {config.SUPPORT_EMAIL} every {config.POLL_SECONDS}s — Ctrl+C to stop")
    while True:
        try:
            for thread_id, message in provider.fetch():
                _handle(machine, provider, thread_id, message)
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            # Transient transport errors (IMAP/SMTP hiccups) must not kill the
            # loop; message-level failures already escalate inside the machine.
            print(f"[warn] poll failed ({type(exc).__name__}: {exc}); retrying")
        time.sleep(config.POLL_SECONDS)


def run_simulated():
    provider = SimulatedProvider()
    machine = build_machine()
    thread_n = 1
    print("Simulated dev loop — type '/new' as the subject to start a fresh thread; "
          "Ctrl+C to quit")
    while True:
        subject = input("\nSubject: ").strip()
        if subject == "/new":
            thread_n += 1
            print(f"(new thread sim-{thread_n})")
            continue
        print("Body (finish with an empty line):")
        lines = []
        while (line := input()) != "":
            lines.append(line)
        provider.deliver(f"sim-{thread_n}", {
            "from": "jordan.riverademo@yahoo.com",
            "subject": subject,
            "body": "\n".join(lines),
            "attachments": [],
        })
        for thread_id, message in provider.fetch():
            _handle(machine, provider, thread_id, message)
        while provider.outbox:
            reply = provider.outbox.pop(0)
            print(f"--- agent reply ({reply['intent']}) ---\n{reply['body']}\n")


def main():
    parser = argparse.ArgumentParser(description="Partiful phone-change support agent")
    parser.add_argument("--provider", choices=("simulated", "gmail"), default="simulated")
    args = parser.parse_args()
    try:
        run_gmail() if args.provider == "gmail" else run_simulated()
    except (KeyboardInterrupt, EOFError):
        print("\nbye")


if __name__ == "__main__":
    main()
