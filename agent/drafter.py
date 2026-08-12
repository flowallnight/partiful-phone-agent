"""Reply drafter: Claude Sonnet, warm Partiful voice, no tools — text only.

Input is (intent, facts) from the state machine; output is the email body.
Hard content rules live in the system prompt and are asserted in the test
suite where deterministic (no codes, no new number, enumeration-safe).
"""
from __future__ import annotations

import json

import anthropic

from agent import config

_SYSTEM = """You draft outbound emails for Partiful Support (Partiful is a fun \
party/event platform). Voice: warm, casual, human, concise — like a friendly \
teammate, never corporate boilerplate. A short greeting is fine. Sign off as \
"Partiful Support".

Output ONLY the email body as plain text. No subject line, no markdown code \
fences, no commentary.

HARD RULES — never break these, no matter what the task says:
1. Never confirm or deny that any phone number has (or doesn't have) a \
Partiful account. Don't say "we couldn't find an account for that number" or \
"that number is already in use".
2. Never include verification codes, full phone numbers, or account details \
(user IDs, activity) in the email. The ONLY permitted identifiers are \
last-four partials supplied in facts (e.g. "ending in 1111"), copied exactly \
as given, and only when the task asks for them.
3. Escalation/handoff emails stay generic: a teammate will take it from here. \
Never state the internal reason (failed checks, lookups, errors).
4. Never invent links, policies, or promises. Only include a URL when the task \
gives you one, and copy it exactly.
5. Whenever you mention the support ticket, write it EXACTLY as \
"Ticket Reference: PF-XXXX" (using the id from facts['ticket_reference']) — \
never a bare ticket id, never any other phrasing of the id. Mention the ticket \
only when a rule or the task calls for it.
6. If facts include announce_ticket=true, weave in ONE warm line letting them \
know a support ticket has been opened to track this, containing exactly \
"Ticket Reference: {facts['ticket_reference']}"."""

# What each intent needs to convey. The state machine supplies `facts`.
_INTENTS = {
    "ask_old_access": (
        "Ask one simple question: can they still receive texts on their old "
        "number? Explain in a few words that the answer decides the fastest "
        "way to update their number. If facts include repeat=true, gently "
        "re-ask because the last reply didn't answer it — a plain yes or no works."
    ),
    "self_serve_link": (
        "Good news: since they can still get texts on the old number, they can "
        "change it themselves in the app in about a minute. Include the URL "
        "from facts exactly as given. Mention the ticket (per the Ticket "
        "Reference rule) and say we're marking this as resolved on our side — "
        "AND explicitly invite them to just reply to this email if the link "
        "doesn't work out or they can't access the old number after all, and "
        "we'll pick it right back up. Never say 'do not reply' here."
    ),
    "collect_ask": (
        "To start the secure number change, ask for everything in one go: the "
        "items listed in facts['missing'] (name = their full name, old_number "
        "= the phone number currently on the account, new_number = the number "
        "they're switching to). Present the missing items as a short fill-in "
        "template they can copy into their reply, using these labels exactly "
        "for the missing items: 'Name:', 'Old number on the account:', "
        "'New number:' — and note a normal written-out reply works too."
    ),
    "collect_nudge": (
        "Friendly nudge: they're nearly there, but the items in "
        "facts['missing'] are still needed (name = full name, old_number = "
        "number currently on the account, new_number = the number they're "
        "switching to). Ask ONLY for the missing items, nothing else."
    ),
    "number_recheck": (
        "Ask them to double-check the old phone number they sent (typos "
        "happen — digit order, country code) and resend it. Do NOT say or "
        "imply anything about whether an account was found."
    ),
    "idv_link": (
        "Next step: identity verification. Open by confirming what this is "
        "about, in present-accurate tense: we received a request to update "
        "the Partiful account connected to the phone number ending in "
        "facts['old_number_last4'] over to a new number ending in "
        "facts['new_number_last4'] — so we're reaching out to help make that "
        "happen safely. The account IS still on the old number right now — "
        "never say 'previously' or imply the change has already happened. "
        "Copy both last-four partials EXACTLY as given; never write any full "
        "phone number. "
        "Warmly explain that to keep the account safe we verify identity "
        "through our identity verification partner (name it only that "
        "generically — no vendor names), via the secure link in "
        "facts['verification_link'] — include that URL exactly as given. "
        "State plainly: they do NOT need access to the old number to complete "
        "this step — identity verification confirms who they are, and "
        "afterward a code texted to their NEW number confirms control of that "
        "number. Do not say or imply that the verification partner checks "
        "phone numbers. Remind them to please never send ID documents or "
        "photos by email; the secure link is the only place verification "
        "happens."
    ),
    "otp_prompt": (
        "Tell them a 6-digit verification code was just texted to their new "
        "number, and to reply to this email with that code (it expires in "
        "about 10 minutes). If facts include repeat=true, note that the last "
        "reply didn't contain a code and ask them to reply with just the "
        "6 digits."
    ),
    "otp_retry": (
        "The code didn't check out (facts['reason'] is 'wrong' or 'expired' — "
        "do not use those exact internal words; phrase it naturally). If "
        "expired, a fresh code was just texted to the new number. Ask them to "
        "reply with the 6-digit code. One more try."
    ),
    "commit_success": (
        "Their phone number change is complete and their account now works "
        "with the new number. Do not include any phone numbers or account "
        "details. Cheerful, then END the email with a closing note conveying "
        "exactly this (warm phrasing welcome, content fixed): 'Ticket "
        "Reference: {facts[ticket_reference]}' is resolved and closed, so "
        "please don't reply to this email — for anything else, just email "
        "hello@partiful.com and we'll open a fresh ticket."
    ),
    "attachment_redirect": (
        "They attached a file (facts['filename']) — reassure them briefly: we "
        "never open or store ID documents sent by email, so it wasn't viewed. "
        "Identity verification happens only through our secure verification "
        "link when we get to that step. Keep it to a sentence or two; this "
        "note is sent alongside the normal reply."
    ),
    "escalation_generic": (
        "Thank them, and let them know a teammate from the support crew will "
        "take it from here. Include ONE warm sentence setting the "
        "response-time expectation: a real person will follow up on this "
        "thread within facts['sla_business_days'] business days — write the "
        "timeframe exactly as '{facts[sla_business_days]} business days'. "
        "Do NOT state any reason, result, or account detail. Short and warm."
    ),
    "handoff_after_failure": (
        "Let them know a teammate will finish this up and follow up on this "
        "thread shortly. Do NOT claim anything was completed, and do NOT "
        "state a reason. Reassuring, short."
    ),
}


class Drafter:
    def __init__(self, client=None):
        self.client = client or anthropic.Anthropic()

    def draft(self, intent, facts):
        instruction = _INTENTS.get(intent, "Write a brief, generic, friendly reply.")
        response = self.client.messages.create(
            model=config.DRAFTER_MODEL,
            max_tokens=400,
            system=_SYSTEM,
            messages=[{
                "role": "user",
                "content": (
                    f"Task: {instruction}\n\n"
                    f"facts = {json.dumps(facts, ensure_ascii=False)}\n\n"
                    "Write the email body now."
                ),
            }],
        )
        return next(b.text for b in response.content if b.type == "text").strip()
