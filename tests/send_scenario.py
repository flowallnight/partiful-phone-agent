"""Live-demo harness: send scenario openers from the user side (Jordan Rivera).

Pulls opener subjects/bodies straight from tests/scenarios.json so this harness
and the automated suite can never drift apart.

  python tests/send_scenario.py --list      # id, name, subject for every scenario
  python tests/send_scenario.py --show 2    # print subject + body for copy-paste
  python tests/send_scenario.py --send 2    # send via Yahoo SMTP (needs USER_APP_PASSWORD)

--send is send-only SMTP (no IMAP); mid-thread replies (answers, OTP codes) are
sent live from the Yahoo web UI during the demo. If Yahoo won't issue an app
password, --show is the supported fallback: paste the printed subject + body
into the Yahoo web UI addressed to the support inbox.
"""
from __future__ import annotations

import argparse
import json
import smtplib
import sys
from email.message import EmailMessage
from email.utils import make_msgid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Scenario names use arrows; Windows consoles often default to cp1252,
# which cannot encode them.
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from agent import config

SCENARIOS_PATH = Path(__file__).resolve().parent / "scenarios.json"
LIVE_DEMO_IDS = {1, 2, 4, 7, 10}


def load_scenarios() -> list[dict]:
    with open(SCENARIOS_PATH, encoding="utf-8") as f:
        return json.load(f)


def find_scenario(scenarios: list[dict], scenario_id: int) -> dict:
    for scenario in scenarios:
        if scenario["id"] == scenario_id:
            return scenario
    ids = ", ".join(str(s["id"]) for s in scenarios)
    print(f"error: no scenario {scenario_id} (available: {ids})")
    sys.exit(2)


def do_list(scenarios: list[dict]) -> None:
    print(f"{len(scenarios)} scenarios (* = live-demo subset):")
    for scenario in scenarios:
        star = "*" if scenario["id"] in LIVE_DEMO_IDS else " "
        subject = scenario["opener"]["subject"]
        print(f" {star} {scenario['id']:>2}  {scenario['name']}  —  \"{subject}\"")


def do_show(scenario: dict) -> None:
    opener = scenario["opener"]
    print(f"Scenario {scenario['id']}: {scenario['name']}")
    print()
    print(f"To:      {config.SUPPORT_EMAIL}")
    print(f"Subject: {opener['subject']}")
    for filename in opener.get("attachments", []):
        print(f"Attach:  {filename}  (any small file renamed to this — content is never opened)")
    print()
    print(opener["body"])
    replies = scenario.get("replies", [])
    if replies:
        print()
        print("--- scripted replies (send manually from the Yahoo web UI at each step) ---")
        for i, reply in enumerate(replies, 1):
            print(f"[reply {i}] {reply}")


def do_send(scenario: dict) -> None:
    if not config.USER_APP_PASSWORD:
        print("error: USER_APP_PASSWORD is not set — cannot send via Yahoo SMTP.")
        print()
        print("Yahoo would not issue an app password for this account. Use the fallback:")
        print(f"  python tests/send_scenario.py --show {scenario['id']}")
        print(f"then copy-paste the subject + body into the Yahoo web UI, addressed to")
        print(f"{config.SUPPORT_EMAIL}. The demo is unaffected.")
        sys.exit(1)

    opener = scenario["opener"]
    msg = EmailMessage()
    msg["From"] = config.USER_EMAIL
    msg["To"] = config.SUPPORT_EMAIL
    msg["Subject"] = opener["subject"]
    msg["Message-ID"] = make_msgid()
    msg.set_content(opener["body"])
    for filename in opener.get("attachments", []):
        # Placeholder bytes only — the agent never opens attachment content
        # (rule 8), so a stand-in payload exercises the exact same path.
        msg.add_attachment(
            b"placeholder attachment for demo",
            maintype="application",
            subtype="octet-stream",
            filename=filename,
        )

    try:
        with smtplib.SMTP_SSL(config.USER_SMTP_HOST, 465) as smtp:
            smtp.login(config.USER_EMAIL, config.USER_APP_PASSWORD)
            smtp.send_message(msg)
    except (smtplib.SMTPException, OSError) as exc:
        print(f"error: Yahoo SMTP send failed: {exc}")
        print(f"fallback:  python tests/send_scenario.py --show {scenario['id']}")
        sys.exit(1)

    print(f"sent scenario {scenario['id']} opener → {config.SUPPORT_EMAIL}")
    print(f"  subject: {opener['subject']}")
    if scenario.get("replies"):
        print("  next: send the scripted replies from the Yahoo web UI "
              f"(see --show {scenario['id']})")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Send or print scenario openers for the live email demo."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--list", action="store_true", help="list all scenarios")
    group.add_argument("--show", type=int, metavar="N",
                       help="print subject + body of scenario N for copy-paste")
    group.add_argument("--send", type=int, metavar="N",
                       help="send scenario N's opener via Yahoo SMTP")
    args = parser.parse_args()

    scenarios = load_scenarios()
    if args.list:
        do_list(scenarios)
    elif args.show is not None:
        do_show(find_scenario(scenarios, args.show))
    else:
        do_send(find_scenario(scenarios, args.send))


if __name__ == "__main__":
    main()
