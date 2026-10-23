"""Send a test email to confirm your Gmail settings:  python -m app.email_check you@example.com"""
import sys
from email.message import EmailMessage
from . import comms
import os

if not comms.email_enabled():
    sys.exit("GMAIL_USER / GMAIL_APP_PASSWORD are not set (put them in .env next to README.md).")
to = sys.argv[1] if len(sys.argv) > 1 else os.environ["GMAIL_USER"]
m = EmailMessage()
m["Subject"], m["From"], m["To"] = "Dentify test email", os.environ["GMAIL_USER"], to
m.set_content("If you can read this, confirmation emails will work.")
try:
    comms._smtp_send(m)
    print(f"Sent OK to {to}")
except Exception as e:
    sys.exit(f"FAILED: {e}\nCheck: 2-Step Verification is on, you used an App Password (not your login password), "
             "and GMAIL_USER is the same account.")
