"""
mailer.py — Gmail SMTP sender for notion-triage. Stdlib only.

Credentials are NOT stored in this project. They live in market-tracker's .env
and are read in place — one place to update when the app password rotates.
Full reference: /home/chakravarti/agents/market-tracker/EMAIL-SETUP.md
and this repo's EMAIL-SETUP.md.

send() never raises. Gmail SMTP from a residential IP on a schedule gets
blocked periodically, so callers treat False as "retry next run".
"""

import os
import smtplib
import time
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path

# First file that exists AND carries the credentials wins. Path.home() covers
# both Pis (/home/chakravarti on Pi 2, /home/rchak007 on Pi 1).
MAIL_ENV_CANDIDATES = [
    Path(os.environ["GMAIL_ENV_FILE"]) if os.environ.get("GMAIL_ENV_FILE") else None,
    Path.home() / "agents" / "market-tracker" / ".env",
    Path("/home/chakravarti/agents/market-tracker/.env"),
    Path("/home/rchak007/agents/market-tracker/.env"),
]
SMTP_HOST, SMTP_PORT = "smtp.gmail.com", 587
FROM_NAME = "Notion Triage"


def mail_env_path():
    for c in MAIL_ENV_CANDIDATES:
        try:
            if c and c.exists() and "GMAIL_APP_PASSWORD" in c.read_text():
                return c
        except Exception:
            continue
    return None


def load_mail_env(path=None):
    """The FILE wins over os.environ. A stale GMAIL_APP_PASSWORD exported in a
    shell would otherwise shadow the right value and give a baffling SMTP 535."""
    path = path or mail_env_path()
    values = {}
    if path:
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            values[k.strip()] = v.strip().strip('"').strip("'")
    for k, v in os.environ.items():
        values.setdefault(k, v)
    return values


def recipients():
    """DIGEST_TO (comma-separated), set in this project's gitignored .env /
    secrets.toml so addresses stay out of the public repo."""
    return [a.strip() for a in os.environ.get("DIGEST_TO", "").split(",") if a.strip()]


def send(subject, body, html=None, to=None, log=print):
    """Send to each recipient. NEVER raises; returns True only if all succeeded."""
    env = load_mail_env()
    address = env.get("GMAIL_ADDRESS", "")
    # Google shows app passwords as four groups of four; the credential is the
    # 16 characters without spaces. With spaces you get a 535 like a bad password.
    password = env.get("GMAIL_APP_PASSWORD", "").replace(" ", "")
    to = to or recipients()

    if not address or not password:
        tried = "\n  ".join(str(c) for c in MAIL_ENV_CANDIDATES if c)
        log(f"no GMAIL_ADDRESS / GMAIL_APP_PASSWORD found. Looked in:\n  {tried}\n"
            "Set GMAIL_ENV_FILE to point at the file.")
        return False
    if not to:
        log("no recipients: set DIGEST_TO in .streamlit/secrets.toml (.env links to it)")
        return False

    try:
        server = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15)
    except Exception as e:
        log(f"SMTP connect failed: {e}")
        return False

    ok = True
    with server:
        try:
            server.starttls()
            server.login(address, password)
        except Exception as e:
            # 535 = wrong credential. 534 5.7.14 = suspicious-sign-in flag; the
            # credential is fine and a browser sign-in on this network clears it.
            log(f"SMTP login failed: {e}")
            return False
        for i, rcpt in enumerate(to):
            if i:
                time.sleep(2)  # pacing; a burst scores on Gmail's abuse heuristics
            msg = EmailMessage()
            msg["Subject"] = subject
            msg["From"] = formataddr((FROM_NAME, address))
            msg["To"] = rcpt
            msg.set_content(body)
            if html:
                msg.add_alternative(html, subtype="html")
            try:
                server.send_message(msg)
                log(f"sent to {rcpt}")
            except Exception as e:
                log(f"send to {rcpt} failed: {e}")
                ok = False
    return ok
