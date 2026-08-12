"""Configuration: env, model names, thresholds.

Phase 1 is fully offline — this module never imports `anthropic` or opens a
network connection; model names are plain strings consumed in Phase 2.
"""
import os
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # python-dotenv is optional until transports are wired
    pass

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
USERS_FIXTURE = FIXTURES_DIR / "users.json"

# LLM (Phase 2)
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLASSIFIER_MODEL = "claude-haiku-4-5-20251001"
DRAFTER_MODEL = "claude-sonnet-4-6"
CONFIDENCE_THRESHOLD = float(os.getenv("CONFIDENCE_THRESHOLD", "0.8"))

# Email transport (Phase 3)
SUPPORT_EMAIL = os.getenv("SUPPORT_EMAIL", "partiful.support.demo@gmail.com")
SUPPORT_APP_PASSWORD = os.getenv("SUPPORT_APP_PASSWORD", "")
SUPPORT_IMAP_HOST = os.getenv("SUPPORT_IMAP_HOST", "imap.gmail.com")
SUPPORT_SMTP_HOST = os.getenv("SUPPORT_SMTP_HOST", "smtp.gmail.com")
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "15"))

# User-side harness (Phase 4) — send-only, no IMAP
USER_EMAIL = os.getenv("USER_EMAIL", "jordan.riverademo@yahoo.com")
USER_APP_PASSWORD = os.getenv("USER_APP_PASSWORD", "")
USER_SMTP_HOST = os.getenv("USER_SMTP_HOST", "smtp.mail.yahoo.com")

# Flow thresholds (scoping doc §6)
NUDGE_MAX = 2                 # batched COLLECT ask, then at most 2 nudges
OTP_MAX_ATTEMPTS = 2          # wrong/expired twice → escalate otp_failed
OTP_TTL_MINUTES = 10
IDV_TTL_MINUTES = 30
UNPARSEABLE_MAX = 2           # 2 consecutive un-parseable replies → escalate
# Response-time expectation quoted in escalation replies. Business-policy
# placeholder for Partiful to set (scoping doc §7), not an engineering value.
SLA_BUSINESS_DAYS = int(os.getenv("SLA_BUSINESS_DAYS", "3"))

SELF_SERVE_URL = (
    "https://help.partiful.com/hc/en-us/articles/"
    "26025082969243-Can-I-change-my-phone-number"
)
