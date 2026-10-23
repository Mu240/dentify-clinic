"""Outbound messages. Every SMS/email is written to the `messages` table (a visible 'outbox').
SMS stays in the outbox until you plug in Twilio. Confirmation EMAILS are really sent through
Gmail SMTP when GMAIL_USER and GMAIL_APP_PASSWORD are set (see README); otherwise they only
appear in the outbox, so the demo still works with zero setup."""
import datetime as dt
import html
import logging
import os
import re
import smtplib
import ssl
import threading
from email.message import EmailMessage
from email.utils import formataddr
from . import scheduling as S

log = logging.getLogger("dentify.comms")
EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")

PRACTICE = os.environ.get("PRACTICE_NAME", "Bright Smile Dental")
REVIEW_LINK = os.environ.get("REVIEW_LINK", "https://g.page/r/YOUR-GOOGLE-REVIEW-LINK")


def send(conn, patient_id, body, kind, appt_id=None, channel="sms"):
    conn.execute(
        "INSERT INTO messages(patient_id,appointment_id,channel,kind,body,created_at) VALUES(?,?,?,?,?,?)",
        (patient_id, appt_id, channel, kind, body, S.iso(S.now())))
    conn.commit()


def when(start):
    d = S.parse(start)
    return d.strftime("%a %b %d at %I:%M %p").replace(" 0", " ")


def email_enabled():
    return bool(os.environ.get("GMAIL_USER") and os.environ.get("GMAIL_APP_PASSWORD"))


def _smtp_send(msg):
    """The only function that touches the network (tests replace it)."""
    user, pw = os.environ["GMAIL_USER"], os.environ["GMAIL_APP_PASSWORD"].replace(" ", "")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context(), timeout=15) as srv:
        srv.login(user, pw)
        srv.send_message(msg)


def _spawn(fn):
    """Send in the background so a slow/failed Gmail call never delays or breaks a booking."""
    threading.Thread(target=fn, daemon=True).start()


def _ics(a):
    start, end = S.parse(a["start"]), S.parse(a["end"])
    f = lambda d: d.strftime("%Y%m%dT%H%M%S")
    return ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//Dentify//EN\r\nMETHOD:PUBLISH\r\n"
            f"BEGIN:VEVENT\r\nUID:appt-{a['id']}@dentify\r\nDTSTAMP:{f(S.now())}\r\n"
            f"DTSTART:{f(start)}\r\nDTEND:{f(end)}\r\n"
            f"SUMMARY:{S.type_name(a['type'])} - {PRACTICE}\r\n"
            f"DESCRIPTION:With {a['provider_name']}\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")


def build_confirmation_email(a, to_addr):
    first = a["patient_name"].split()[0]
    what, whn = S.type_name(a["type"]), when(a["start"])
    office = " | ".join(x for x in (os.environ.get("PRACTICE_PHONE"), os.environ.get("PRACTICE_ADDRESS")) if x)
    text = (f"Hi {first},\n\nYour appointment at {PRACTICE} is confirmed.\n\n"
            f"  Visit:  {what}\n  When:   {whn}\n  With:   {a['provider_name']}\n\n"
            "Need to change or cancel? Just reply to this email or call the office.\n"
            "A calendar file is attached so you can add it to your calendar.\n\n"
            f"See you soon,\n{PRACTICE}" + (f"\n{office}" if office else ""))
    h = html.escape
    page = (f'<div style="font-family:Arial,sans-serif;max-width:480px;margin:auto;color:#1f2937">'
            f'<h2 style="color:#2563eb;margin-bottom:4px">{h(PRACTICE)}</h2>'
            f'<p>Hi {h(first)}, your appointment is <b>confirmed</b>.</p>'
            f'<table style="border-collapse:collapse;margin:12px 0">'
            f'<tr><td style="padding:4px 14px 4px 0;color:#6b7280">Visit</td><td><b>{h(what)}</b></td></tr>'
            f'<tr><td style="padding:4px 14px 4px 0;color:#6b7280">When</td><td><b>{h(whn)}</b></td></tr>'
            f'<tr><td style="padding:4px 14px 4px 0;color:#6b7280">With</td><td><b>{h(a["provider_name"])}</b></td></tr>'
            f'</table><p>Need to change or cancel? Reply to this email or call the office.</p>'
            f'<p style="color:#6b7280;font-size:13px">{h(office)}</p></div>')
    msg = EmailMessage()
    msg["Subject"] = f"Appointment confirmed: {what}, {whn}"
    msg["From"] = formataddr((os.environ.get("EMAIL_FROM_NAME", PRACTICE), os.environ["GMAIL_USER"]))
    msg["To"] = to_addr
    msg["Reply-To"] = os.environ["GMAIL_USER"]
    msg.set_content(text)
    msg.add_alternative(page, subtype="html")
    msg.add_attachment(_ics(a).encode(), maintype="text", subtype="calendar",
                       filename="appointment.ics", params={"method": "PUBLISH"})
    return msg, text


def deliver_email(conn, pid, to, build, summary, kind, appt_id=None):
    """Write the outbox entry, then (if Gmail is configured) send in the background.
    build() -> EmailMessage is only called when Gmail is configured. Returns True if queued to send."""
    msg = build() if email_enabled() else None
    note = "" if msg else " [Gmail not configured - not sent]"
    send(conn, pid, f"To {to}: {summary}{note}", kind, appt_id, channel="email")
    if not msg:
        return False

    def job():
        try:
            _smtp_send(msg)
        except Exception as e:  # never let email trouble touch the booking
            log.warning("%s email to %s failed: %s", kind, to, e)
            from . import db
            c = db.connect()
            try:
                send(c, pid, f"Email to {to} FAILED: {e}", "email_failed", appt_id, channel="note")
            finally:
                c.close()
    _spawn(job)
    return True


def email_confirmation(conn, appt_id):
    """Email the patient their confirmation. Returns True if an email was queued."""
    a = S.get_appt(conn, appt_id)
    row = conn.execute("SELECT email FROM patients WHERE id=?", (a["patient_id"],)).fetchone()
    to = ((row["email"] if row else "") or "").strip()
    if not to or not EMAIL_RE.fullmatch(to):
        return False
    return deliver_email(conn, a["patient_id"], to, lambda: build_confirmation_email(a, to)[0],
                         f"{PRACTICE} appointment confirmed for {when(a['start'])}.", "confirmation", appt_id)


def email_plan(conn, patient, url, est):
    """Email the patient a link to their treatment plan and estimate."""
    to = (patient.get("email") or "").strip()
    if not to or not EMAIL_RE.fullmatch(to):
        return False
    t, first, h = est["totals"], patient["name"].split()[0], html.escape

    def build():
        msg = EmailMessage()
        msg["Subject"] = f"Your treatment plan from {PRACTICE}"
        msg["From"] = formataddr((os.environ.get("EMAIL_FROM_NAME", PRACTICE), os.environ["GMAIL_USER"]))
        msg["To"], msg["Reply-To"] = to, os.environ["GMAIL_USER"]
        msg.set_content(f"Hi {first},\n\nYour dentist has prepared a treatment plan. Estimated total ${t['fee']:,.2f}; "
                        f"your share is about ${t['patient']:,.2f} after insurance.\n\nReview and accept it here:\n{url}\n\n"
                        f"Questions? Just reply to this email.\n\n{PRACTICE}")
        msg.add_alternative(
            f'<div style="font-family:Arial,sans-serif;max-width:480px;margin:auto;color:#1f2937"><h2 style="color:#2563eb">{h(PRACTICE)}</h2>'
            f'<p>Hi {h(first)}, your treatment plan is ready.</p><p>Estimated total <b>${t["fee"]:,.2f}</b><br>'
            f'Your share after insurance: <b>${t["patient"]:,.2f}</b></p>'
            f'<p><a href="{h(url)}" style="background:#2563eb;color:#fff;padding:12px 20px;border-radius:8px;text-decoration:none">'
            f'Review &amp; accept your plan</a></p><p style="color:#6b7280;font-size:13px">Estimates only; insurance makes the final decision.</p></div>',
            subtype="html")
        return msg
    return deliver_email(conn, patient["id"], to, build, f"treatment plan link sent (your share about ${t['patient']:,.2f}).", "treatment_plan")


def confirmation(conn, appt_id):
    a = S.get_appt(conn, appt_id)
    send(conn, a["patient_id"],
         f"{PRACTICE}: your {S.type_name(a['type']).lower()} is confirmed for {when(a['start'])} "
         f"with {a['provider_name']}. Reply C to cancel.", "confirmation", appt_id)
    email_confirmation(conn, appt_id)


def run_reminders(conn):
    n, sent = S.now(), 0
    windows = [("48h", n + dt.timedelta(hours=2), n + dt.timedelta(hours=48)),
               ("2h", n, n + dt.timedelta(hours=2))]
    for label, lo, hi in windows:
        rows = conn.execute(
            "SELECT id FROM appointments WHERE status='scheduled' AND start > ? AND start <= ?",
            (S.iso(lo), S.iso(hi))).fetchall()
        for r in rows:
            kind = f"reminder_{label}"
            if conn.execute("SELECT 1 FROM messages WHERE appointment_id=? AND kind=?",
                            (r["id"], kind)).fetchone():
                continue
            a = S.get_appt(conn, r["id"])
            send(conn, a["patient_id"],
                 f"{PRACTICE} reminder: you have a {S.type_name(a['type']).lower()} on "
                 f"{when(a['start'])}. Reply C to confirm or R to reschedule.", kind, a["id"])
            sent += 1
    return sent


def run_recall(conn, months=6):
    n, sent = S.now(), 0
    cutoff = S.iso(n - dt.timedelta(days=30 * months))
    for p in conn.execute("SELECT * FROM patients").fetchall():
        last = conn.execute("SELECT MAX(start) m FROM appointments WHERE patient_id=? AND status='completed'",
                            (p["id"],)).fetchone()["m"]
        if not last or last >= cutoff:
            continue
        if S.upcoming(conn, p["id"]):
            continue
        if conn.execute("SELECT 1 FROM messages WHERE patient_id=? AND kind='recall' AND created_at>?",
                        (p["id"], S.iso(n - dt.timedelta(days=90)))).fetchone():
            continue
        send(conn, p["id"],
             f"Hi {p['name'].split()[0]}, it's been a while since your last visit to {PRACTICE}. "
             f"Reply YES and we'll find a time for your check-up and cleaning.", "recall")
        sent += 1
    return sent


def review_invite(conn, appt_id):
    if conn.execute("SELECT 1 FROM messages WHERE appointment_id=? AND kind='review_invite'",
                    (appt_id,)).fetchone():
        return False
    a = S.get_appt(conn, appt_id)
    send(conn, a["patient_id"],
         f"Thanks for visiting {PRACTICE}, {a['patient_name'].split()[0]}! "
         f"If we earned it, a quick Google review helps a lot: {REVIEW_LINK}",
         "review_invite", appt_id)
    return True
