"""The AI receptionist 'brain'.

Design: booking / cancelling / rescheduling run as a deterministic slot-filling state machine,
so it never invents an appointment time. Open questions are answered from the practice FAQ
(optionally re-phrased by a free LLM, see llm.py). It talks to the same SQLite calendar as the Hub.
"""
import datetime as dt
import json
import os
import re
import uuid

from . import comms, kb, llm
from . import scheduling as S

PRACTICE = comms.PRACTICE
SESSIONS = {}

NUM_WORDS = {"zero": "0", "oh": "0", "one": "1", "two": "2", "three": "3", "four": "4",
             "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9"}
_DW = "|".join(NUM_WORDS)
_DW_RE = re.compile(rf"\b(?:(?:{_DW})\b[\s,.\-]*){{7,}}")

MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"], 1)}
MONTHS.update({"jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8,
               "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12})
_MON = "|".join(sorted(MONTHS, key=len, reverse=True))
WEEKDAYS = {"monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1, "wednesday": 2, "wed": 2,
            "thursday": 3, "thu": 3, "thur": 3, "thurs": 3, "friday": 4, "fri": 4,
            "saturday": 5, "sat": 5, "sunday": 6, "sun": 6}
_WD = "|".join(sorted(WEEKDAYS, key=len, reverse=True))

TYPE_PATTERNS = [
    ("emergency", r"\b(emergency|urgent|toothache|tooth ache|knocked out|broken tooth|chipped tooth|swollen|swelling|severe pain|abscess)\b"),
    ("rootcanal", r"root canal"),
    ("implant", r"\bimplants?\b"),
    ("whitening", r"\bwhiten(?:ing)?\b|\bbleach"),
    ("cleaning", r"\bclean(?:ing|ings)?\b|hygien"),
    ("checkup", r"check[\s-]?ups?\b|\bexam\b|\bconsult(?:ation)?\b|\bchecked\b"),
    ("filling", r"\bfillings?\b|\bcavit(?:y|ies)\b"),
]
STOP = set("""a an the to and or but my me i we you it in on at for of with about so very really just also
here there back still not no yes new calling looking trying wanting needing need want would like book
booking schedule appointment appointments cancel reschedule fine good ok okay sorry interested wondering
having happy glad hi hello hey please thanks thank calling pain tooth teeth dentist cleaning checkup
check filling whitening implant emergency tomorrow today""".split())

SAFETY = ("If you have severe swelling, trouble breathing or swallowing, or bleeding that won't stop, "
          "please call 911 or go to the ER right away. Otherwise, I'll find you the soonest slot. ")


# ------------------------------------------------------------------ parsing
def words_to_digits(s):
    return _DW_RE.sub(lambda m: "".join(NUM_WORDS[w] for w in re.findall(_DW, m.group(0))), s)


def parse_date(low, today):
    m = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", low)
    if m:
        try:
            return dt.date(*map(int, m.groups()))
        except ValueError:
            return None
    if "day after tomorrow" in low:
        return today + dt.timedelta(days=2)
    if re.search(r"\btomorrow\b", low):
        return today + dt.timedelta(days=1)
    if re.search(r"\b(today|tonight|this evening)\b", low):
        return today
    mon = day = None
    m = re.search(rf"\b({_MON})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\b", low)
    if m:
        mon, day = MONTHS[m.group(1)], int(m.group(2))
    else:
        m = re.search(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?({_MON})\b", low)
        if m:
            mon, day = MONTHS[m.group(2)], int(m.group(1))
    if mon:
        for year in (today.year, today.year + 1):
            try:
                d = dt.date(year, mon, day)
            except ValueError:
                return None
            if d >= today:
                return d
        return None
    m = re.search(r"\b(?:the\s+)?(\d{1,2})(?:st|nd|rd|th)\b", low)
    if m:
        day = int(m.group(1))
        y, mo = today.year, today.month
        for _ in range(2):
            try:
                d = dt.date(y, mo, day)
                if d >= today:
                    return d
            except ValueError:
                pass
            mo, y = (1, y + 1) if mo == 12 else (mo + 1, y)
        return None
    m = re.search(rf"\b(next\s+|this\s+|on\s+)?({_WD})\b", low)
    if m:
        wd = WEEKDAYS[m.group(2)]
        if (m.group(1) or "").strip() == "next":
            return today - dt.timedelta(days=today.weekday()) + dt.timedelta(days=7 + wd)
        return today + dt.timedelta(days=(wd - today.weekday()) % 7 or 7)
    return None


def _heuristic_hour(h, mi):
    if 13 <= h <= 23:
        return dt.time(h, mi)
    if 1 <= h <= 6:
        return dt.time(h + 12, mi)
    if 7 <= h <= 11:
        return dt.time(h, mi)
    if h == 12:
        return dt.time(12, mi)
    return None


def parse_time(low):
    m = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)(?![a-z])", low)
    if m:
        h, mi, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3)[0]
        if 1 <= h <= 12 and mi < 60:
            return dt.time(h % 12 + (12 if ap == "p" else 0), mi)
        return None
    if re.search(r"\bnoon\b", low):
        return dt.time(12, 0)
    m = re.search(r"\b([01]?\d|2[0-3]):([0-5]\d)\b", low)
    if m:
        return _heuristic_hour(int(m.group(1)), int(m.group(2)))
    m = re.search(r"\bat\s+(\d{1,2})\b(?!\s*(?:st|nd|rd|th|\d))", low)
    if m:
        return _heuristic_hour(int(m.group(1)), 0)
    return None


def detect_type(low):
    for key, pat in TYPE_PATTERNS:
        if re.search(pat, low):
            return key
    return None


def extract_name(t, expect):
    m = re.search(r"\b(?:my name is|my name's|name is|this is|call me)\s+([a-z][a-z'\-]+(?:\s+[a-z][a-z'\-]+){0,2})", t, re.I)
    if not m and expect == "name":
        m = re.search(r"\b(?:i am|i'm|im|it's|it is)\s+([a-z][a-z'\-]+(?:\s+[a-z][a-z'\-]+){0,2})", t, re.I)
    cand = m.group(1) if m else None
    if not cand and expect == "name":
        words = re.sub(r"[^A-Za-z'\- ]", " ", t).split()
        if 1 <= len(words) <= 4:
            cand = " ".join(words)
    if not cand:
        return None
    out = []
    for w in cand.split():
        if w.lower() in STOP:
            break
        out.append(w)
    return " ".join(w.capitalize() if w.islower() else w for w in out) or None


def yes_no(low):
    if re.search(r"\b(no|nope|nah|don'?t|do not|never ?mind|keep it|leave it)\b", low):
        return False
    if re.search(r"\b(yes|yeah|yep|yup|sure|ok|okay|correct|please do|go ahead|that works|sounds good|confirm|do it)\b", low):
        return True
    return None


def is_question(low):
    return "?" in low or bool(re.match(
        r"\s*(how|what|what's|when|where|which|why|who|do you|does|can you tell|is there|are you|are there|will|would)\b", low))


# ------------------------------------------------------------------ formatting
def fmt_time(hhmm):
    h, m = map(int, hhmm.split(":"))
    return f"{h % 12 or 12}{':%02d' % m if m else ''} {'PM' if h >= 12 else 'AM'}"


def fmt_day(d, today=None):
    return f"{d.strftime('%A')}, {d.strftime('%B')} {d.day}"


def join(opts):
    o = [fmt_time(x) for x in opts]
    return o[0] if len(o) == 1 else ", ".join(o[:-1]) + " or " + o[-1]


def pick_options(slots, period=None):
    if period == "am":
        slots = [s for s in slots if s < "12:00"] or slots
    elif period == "pm":
        slots = [s for s in slots if s >= "12:00"] or slots
    if len(slots) <= 3:
        return slots
    return [slots[0], slots[len(slots) // 2], slots[-1]]


# ------------------------------------------------------------------ sessions
def new_session(channel="voice", caller_phone=None):
    sess = dict(id=uuid.uuid4().hex[:12], channel=channel, started=S.iso(S.now()), history=[],
                intent=None, slots={}, expect=None, proposed=[], patient=None, events=[],
                outcome=None, done=False, fallbacks=0, call_id=None, ex={})
    if caller_phone:
        sess["slots"]["phone"] = S.norm_phone(caller_phone)
    SESSIONS[sess["id"]] = sess
    return sess


def greeting(sess):
    if sess["channel"] == "voice":
        return f"Thank you for calling {PRACTICE}. I'm the virtual receptionist. How can I help you today?"
    return f"Hi! Welcome to {PRACTICE}. I can book, change or cancel appointments and answer questions. How can I help?"


def _ask(sess, field, text):
    if sess["expect"] == field and not sess["ex"]:
        text = "Sorry, I didn't catch that. " + text
    sess["expect"] = field
    return text


def _event(sess, text, outcome=None):
    sess["events"].append(text)
    if outcome:
        sess["outcome"] = outcome


def _finish_flow(sess, keep=("phone", "name", "email", "email_skipped")):
    sess["slots"] = {k: v for k, v in sess["slots"].items() if k in keep}
    sess.update(intent=None, proposed=[], expect="more")
    for k in ("cancel_appt", "resched_appt", "resched_type", "emergency_noted"):
        sess.pop(k, None)


# ------------------------------------------------------------------ extraction
_EMAIL = r"[a-z0-9._%+\-]+@[a-z0-9\-]+(?:\.[a-z0-9\-]+)+"


def extract_email(t, expect=None):
    """Typed ('sara@gmail.com') or spoken ('sara at gmail dot com') email address."""
    m = re.search(_EMAIL, t.lower())
    if m:
        return m.group(0).strip(".")
    if expect != "email":
        return None
    x = re.sub(r"^.*?\b(?:e-?mail(?: address)?|it'?s|it is|is)\s+", "", t.lower().strip(), count=1)
    x = re.sub(r"\s+(?:at the rate(?: of)?|at)\s+", "@", x)
    x = re.sub(r"\s*\b(?:dot|period)\b\s*", ".", x)
    x = re.sub(r"\bunderscore\b", "_", x)
    x = re.sub(r"\b(?:dash|hyphen)\b", "-", x)
    x = re.sub(r"\s+", "", x)
    m = re.search(_EMAIL, x)
    return m.group(0).strip(".") if m else None


def extract(sess, t):
    s, today, got = sess["slots"], S.now().date(), {}
    if sess["expect"] == "email":          # only an address (or a 'skip') makes sense here
        em = extract_email(t, "email")
        if em:
            s["email"] = em
            got["email"] = em
        elif re.search(r"\b(no|skip|none|not now|no thanks|don'?t have|do not have|rather not|prefer not)\b", t.lower()):
            s["email_skipped"] = True
            got["email_skipped"] = True
        return got
    low = words_to_digits(t.lower())
    em = extract_email(t)
    if em:
        s["email"] = em
        got["email"] = em
        low = low.replace(em, " ")         # digits inside an address must not be read as a phone number
    typ = detect_type(low)
    if typ:
        s["type"] = typ
        got["type"] = typ
    d = parse_date(low, today)
    if d:
        s["date"] = d.isoformat()
        got["date"] = d
    tm = parse_time(low)
    if not tm and sess["expect"] == "time":
        m = re.fullmatch(r"\s*(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(?:o'?clock)?\s*", low)
        if m:
            tm = _heuristic_hour(int(m.group(1)), int(m.group(2) or 0))
    if tm:
        s["time"] = tm.strftime("%H:%M")
        got["time"] = tm
    elif re.search(r"\bmorning\b", low):
        s["period"] = "am"
    elif re.search(r"\b(afternoon|evening|after lunch)\b", low):
        s["period"] = "pm"
    if sess["expect"] == "time" and sess["proposed"] and "time" not in got:
        pr = sess["proposed"]
        if re.search(r"\b(first|1st|earliest|earlier)\b", low):
            s["time"] = pr[0]
        elif re.search(r"\b(second|2nd)\b", low) and len(pr) > 1:
            s["time"] = pr[1]
        elif re.search(r"\b(third|3rd|last|latest|later)\b", low):
            s["time"] = pr[-1]
        elif yes_no(low) is True:
            s["time"] = pr[0]
        if "time" in s and s["time"] in pr:
            got["time"] = s["time"]
    cleaned = re.sub(r"\d{4}-\d{2}-\d{2}", " ", low)
    m = re.search(r"\+?\d[\d\s\-().]{5,}\d", cleaned)
    if m and len(re.sub(r"\D", "", m.group(0))) >= 7:
        s["phone"] = S.norm_phone(m.group(0))
        got["phone"] = s["phone"]
        sess["patient"] = None
    name = extract_name(t, sess["expect"])
    if name and "phone" not in got:
        s["name"] = name
        got["name"] = name
    return got


# ------------------------------------------------------------------ flows
def _lookup(conn, sess):
    s = sess["slots"]
    pat = S.patient_by_phone(conn, s["phone"]) if s.get("phone") else None
    sess["patient"] = pat
    if pat:
        s["name"] = pat["name"]
    return pat


def _schedule_step(conn, sess, type_, exclude_id=None, date_prompt=None):
    """Returns a reply while date/time is still unsettled, or None when s['date'], s['time'] are valid."""
    s, today = sess["slots"], S.now().date()
    nice = S.type_name(type_).lower()
    if not s.get("date"):
        return _ask(sess, "date", date_prompt or f"Sure, a {nice}. What day works best for you?")
    d = dt.date.fromisoformat(s["date"])
    if d < today:
        s.pop("date"), s.pop("time", None)
        return _ask(sess, "date", "That date has already passed. What day would you like instead?")
    if not S.is_open_day(d):
        s.pop("date"), s.pop("time", None)
        return _ask(sess, "date", "We're closed on Sundays. We're open Monday to Friday 9 to 5 and Saturday 9 to 1. "
                                  "What other day works?")
    slots = S.free_slots(conn, d, type_, exclude_id)
    if not slots:
        s.pop("time", None)
        nd = S.next_available_date(conn, type_, d + dt.timedelta(days=1), exclude_id)
        if nd is None:
            s.pop("date")
            return _ask(sess, "date", "I'm not seeing any openings in the next month. "
                                      "Say 'human' and I'll connect you with the team.")
        s["date"] = nd.isoformat()
        opts = pick_options(S.free_slots(conn, nd, type_, exclude_id), s.get("period"))
        sess["proposed"] = opts
        return _ask(sess, "time", f"We're fully booked on {fmt_day(d)}. The next opening is {fmt_day(nd)}, "
                                  f"with {join(opts)} available. Does one of those work?")
    if not s.get("time"):
        opts = pick_options(slots, s.get("period"))
        sess["proposed"] = opts
        return _ask(sess, "time", f"On {fmt_day(d)} I have {join(opts)}. Which would you like?")
    if s["time"] not in slots:
        bad = s.pop("time")
        opts = pick_options(slots, s.get("period"))
        sess["proposed"] = opts
        return _ask(sess, "time", f"Sorry, {fmt_time(bad)} isn't available on {fmt_day(d)}. I can do {join(opts)}.")
    return None


def _book(conn, sess):
    s, today = sess["slots"], S.now().date()
    if not s.get("type"):
        return _ask(sess, "type", "Happy to help. What kind of visit do you need? For example a cleaning, "
                                  "check-up, filling, whitening, implant consultation, or an emergency visit.")
    typ, prefix = s["type"], ""
    if typ == "emergency" and not sess.get("emergency_noted"):
        sess["emergency_noted"] = True
        prefix = SAFETY
        s.setdefault("date", today.isoformat())
    r = _schedule_step(conn, sess, typ)
    if r:
        return prefix + r
    d = dt.date.fromisoformat(s["date"])
    if not s.get("phone"):
        return prefix + _ask(sess, "phone", f"{fmt_time(s['time'])} on {fmt_day(d)} is open. "
                                            "What's the best phone number for the booking?")
    pat = S.patient_by_phone(conn, s["phone"])
    if pat:
        sess["patient"], s["name"] = pat, pat["name"]
    elif not s.get("name"):
        return _ask(sess, "name", "Thanks. And may I have your full name?")
    if not (pat or {}).get("email") and not s.get("email") and not s.get("email_skipped"):
        return _ask(sess, "email", "And what's your email address? I'll send your confirmation there. "
                                   "You can say skip if you'd rather not.")
    try:
        pid = pat["id"] if pat else S.create_patient(conn, s["name"], s["phone"], s.get("email"))
        if pat and not pat.get("email") and s.get("email"):
            conn.execute("UPDATE patients SET email=? WHERE id=?", (s["email"], pid))
            conn.commit()
        appt = S.book(conn, pid, typ, f"{s['date']}T{s['time']}", source=f"ai_{sess['channel']}")
    except S.Conflict:
        s.pop("time", None)
        return _schedule_step(conn, sess, typ) or "Sorry, that slot just filled up. Which other time works?"
    a = S.get_appt(conn, appt)
    comms.confirmation(conn, appt)
    first = a["patient_name"].split()[0]
    em = conn.execute("SELECT email FROM patients WHERE id=?", (pid,)).fetchone()["email"]
    _event(sess, f"Booked {S.type_name(typ).lower()} for {a['patient_name']} on {comms.when(a['start'])}", "booked")
    _finish_flow(sess)
    sess["slots"]["name"] = a["patient_name"]
    sent = "I'm texting a confirmation to your phone" + (f" and emailing it to {em}." if em and comms.email_enabled() else ".")
    return (f"You're all set, {first}! {S.type_name(typ)} on {fmt_day(d)} at {fmt_time(a['start'][11:16])} "
            f"with {a['provider_name']}. {sent} Anything else I can help with?")


def _thanks(sess):
    return "Thank you for calling" if sess["channel"] == "voice" else "Thanks for contacting"


def _find_appt(conn, sess, verb):
    s = sess["slots"]
    # Social/web channels can't prove who owns a phone number, so cancelling/moving is only allowed
    # from a thread that is already linked to the patient (see social.py).
    if sess.get("require_verified") and not sess.get("verified"):
        _finish_flow(sess)
        return None, ("For your security I can only change or cancel appointments from the account that "
                      "booked them. Please call the office, or say 'human' and the team will help.")
    if not s.get("phone"):
        return None, _ask(sess, "phone", f"I can help with that. What phone number is the appointment under?")
    pat = _lookup(conn, sess)
    if not pat:
        s.pop("phone")
        return None, _ask(sess, "phone", "I couldn't find a patient with that number. Could you say it again? "
                                         "Or say 'human' and I'll connect you with the team.")
    appts = S.upcoming(conn, pat["id"])
    if not appts:
        _finish_flow(sess)
        return None, f"Thanks, {pat['name'].split()[0]}. I don't see any upcoming appointments for you. Anything else I can help with?"
    return appts[0], None


def _cancel(conn, sess):
    low = sess["ex"].get("_low", "")
    if "cancel_appt" not in sess:
        a, reply = _find_appt(conn, sess, "cancel")
        if reply:
            return reply
        sess["cancel_appt"] = a["id"]
        return _ask(sess, "confirm", f"I found your {S.type_name(a['type']).lower()} on "
                                     f"{fmt_day(S.parse(a['start']).date())} at {fmt_time(a['start'][11:16])}. "
                                     "Shall I cancel it?")
    yn = yes_no(re.sub(r"\bcancel\w*", "", low))
    a = S.get_appt(conn, sess["cancel_appt"])
    if yn is True:
        S.set_status(conn, a["id"], "cancelled")
        comms.send(conn, a["patient_id"], f"{PRACTICE}: your appointment on {comms.when(a['start'])} has been cancelled. "
                   "Reply or call any time to rebook.", "cancellation", a["id"])
        _event(sess, f"Cancelled {S.type_name(a['type']).lower()} on {comms.when(a['start'])}", "cancelled")
        _finish_flow(sess)
        sess["offer_book"] = True
        return "Done, it's cancelled. I've texted you a confirmation. Would you like to book a new time?"
    if yn is False:
        _event(sess, "Caller decided to keep appointment", "info")
        _finish_flow(sess)
        return "No problem, I've left your appointment as it is. Anything else?"
    return _ask(sess, "confirm", "Sorry, should I cancel that appointment? Please say yes or no.")


def _resched(conn, sess):
    prefix = ""
    if "resched_appt" not in sess:
        a, reply = _find_appt(conn, sess, "reschedule")
        if reply:
            return reply
        sess["resched_appt"], sess["resched_type"] = a["id"], a["type"]
        prefix = (f"I found your {S.type_name(a['type']).lower()} on {fmt_day(S.parse(a['start']).date())} "
                  f"at {fmt_time(a['start'][11:16])}. ")
    s = sess["slots"]
    r = _schedule_step(conn, sess, sess["resched_type"], sess["resched_appt"],
                       date_prompt="What day would you like to move it to?")
    if r:
        return prefix + r
    old = S.get_appt(conn, sess["resched_appt"])
    try:
        S.reschedule(conn, sess["resched_appt"], f"{s['date']}T{s['time']}")
    except S.Conflict:
        s.pop("time", None)
        return _schedule_step(conn, sess, sess["resched_type"], sess["resched_appt"]) or "That slot just filled up."
    new = S.get_appt(conn, sess["resched_appt"])
    comms.send(conn, new["patient_id"], f"{PRACTICE}: your appointment moved to {comms.when(new['start'])} "
               f"with {new['provider_name']}.", "reschedule", new["id"])
    _event(sess, f"Rescheduled {S.type_name(new['type']).lower()} from {comms.when(old['start'])} to {comms.when(new['start'])}", "rescheduled")
    _finish_flow(sess)
    d = S.parse(new["start"])
    return (f"Done! I moved your {S.type_name(new['type']).lower()} from {comms.when(old['start'])} to "
            f"{fmt_day(d.date())} at {fmt_time(new['start'][11:16])}. I've texted you the new details. Anything else?")


def _transfer(conn, sess):
    if S.is_open_now():
        _event(sess, "Caller asked for a human, transferred to front desk", "transferred")
        sess["done"] = True
        return "Of course. I'm transferring you to our front desk now. One moment please."
    sess["expect"] = "message"
    return ("Our team has left for the day, but I can take a message and they'll call you back first thing. "
            "What would you like me to pass along?")


def _take_message(conn, sess, text):
    who = sess["slots"].get("phone") or "unknown number"
    comms.send(conn, (sess.get("patient") or {}).get("id"), f"MESSAGE FOR STAFF from {who}: {text}",
               "staff_message", channel="note")
    _event(sess, "Took a message for staff", "message_taken")
    _finish_flow(sess)
    return "Got it. I've passed that to the team and they'll get back to you. Anything else I can help with?"


def _faq(sess, low, text):
    hit = kb.search(low)
    if llm.enabled():
        out = llm.answer(text, kb.all_facts())
        if out:
            _event(sess, f"Answered question ({hit['id'] if hit else 'open question'})", sess["outcome"] or "info")
            return out
    if hit:
        _event(sess, f"Answered question ({hit['id']})", sess["outcome"] or "info")
        return hit["answer"]
    return None


def _reprompt(sess):
    return {"type": "What kind of visit would you like?", "date": "What day works for you?",
            "time": "What time would you like?", "phone": "What's the best phone number?",
            "name": "May I have your full name?", "email": "What's your email address? Or say skip.", "confirm": "Is that a yes or a no?"}.get(sess["expect"], "")


# ------------------------------------------------------------------ main entry
def handle(conn, sess, text):
    text = (text or "").strip()
    if not text:
        return "Sorry, I didn't catch that. Could you say that again?"
    sess["history"].append(["caller", text])
    reply = _route(conn, sess, text)
    sess["history"].append(["agent", reply])
    save_call(conn, sess)
    return reply


def _route(conn, sess, t):
    low = t.lower()
    sess["ex"] = {"_low": low}

    if sess["expect"] == "message":
        return _take_message(conn, sess, t)

    if re.search(r"\b(human|real person|operator|speak (?:to|with) (?:someone|a person|somebody|the (?:front )?desk)|talk to (?:someone|a person))\b", low):
        return _transfer(conn, sess)

    if sess["intent"] and re.search(r"\b(bye|goodbye|never ?mind|forget it)\b", low):
        _event(sess, "Caller ended the call before finishing", "abandoned")
        sess["done"] = True
        return f"No problem. {_thanks(sess)} {PRACTICE}. Goodbye!"

    # explicit intent
    intent = None
    if re.search(r"\bcancel", low) and not re.search(r"polic|fee|charge", low):
        intent = "cancel"
    elif re.search(r"re-?schedul|rebook|move my|change my (?:appointment|visit|booking)|push (?:it|my)", low):
        intent = "reschedule"
    elif re.search(r"\b(book|schedule|reserve|make an appointment|set up an appointment|come in|see the dentist|see a dentist)\b|\bappointment\b", low) \
            and not is_question(low):
        intent = "book"
    elif re.search(r"\bcan you (?:book|schedule)|\bcould you (?:book|schedule)|\bi(?:'d| would) like to (?:book|schedule)", low):
        intent = "book"
    if intent and intent != sess["intent"]:
        for k in ("cancel_appt", "resched_appt", "resched_type"):
            sess.pop(k, None)
        sess["intent"], sess["expect"] = intent, None

    # questions are answered without hijacking the flow
    if not intent and is_question(low):
        ans = _faq(sess, low, t)
        if ans:
            tail = ""
            if sess["intent"]:
                tail = " " + (_reprompt(sess) or "")
            elif not sess.get("nudged") and not llm.enabled():
                sess["nudged"] = True
                tail = " Would you like me to book an appointment?"
            return ans + tail

    got = extract(sess, t)
    sess["ex"].update({k: v for k, v in got.items()})
    ex_keys = [k for k in sess["ex"] if k != "_low"]
    sess["ex"] = {k: sess["ex"][k] for k in ex_keys}
    sess["ex"]["_low"] = low

    # closing / yes-no after a finished task
    if not sess["intent"] and sess["expect"] == "more":
        yn = yes_no(low)
        if yn is False or re.search(r"\b(bye|goodbye|that'?s all|that is all|nothing|i'?m good)\b", low):
            sess["done"] = True
            return f"{_thanks(sess)} {PRACTICE}. Have a wonderful day, goodbye!"
        if yn is True and not got:
            sess["expect"] = None
            if sess.pop("offer_book", False):
                sess["intent"] = "book"
                return _book(conn, sess)
            return "Of course. What can I help you with?"

    if not sess["intent"] and got.get("type") and not is_question(low):
        sess["intent"] = "book"

    if sess["intent"] == "book":
        return _book(conn, sess)
    if sess["intent"] == "cancel":
        return _cancel(conn, sess)
    if sess["intent"] == "reschedule":
        return _resched(conn, sess)

    if re.search(r"\b(bye|goodbye|that'?s all|thank you|thanks)\b", low):
        sess["done"] = True
        return f"You're welcome! {_thanks(sess)} {PRACTICE}. Goodbye!"
    if re.search(r"\b(hi|hello|hey|good (?:morning|afternoon|evening))\b", low):
        return "Hello! How can I help you today? I can book, change or cancel an appointment, or answer questions."
    if is_question(low) and llm.enabled():
        out = llm.answer(t, kb.all_facts())
        if out:
            return out
    sess["fallbacks"] += 1
    if sess["fallbacks"] >= 2:
        return ("I'm sorry, I'm not sure about that one. Say 'human' and I'll get a team member, "
                "or I can book an appointment for you.")
    return ("I can book, reschedule or cancel appointments and answer questions about the practice, "
            "like hours, insurance and pricing. What would you like to do?")


# ------------------------------------------------------------------ call log
def summarize(sess):
    who = sess["slots"].get("name") or (sess["slots"].get("phone") and S.fmt_phone(sess["slots"]["phone"])) or "Unknown caller"
    body = "; ".join(sess["events"]) or "No action taken"
    return f"{who}: {body}."


def save_call(conn, sess):
    transcript = json.dumps(sess["history"])
    summary, outcome = summarize(sess), sess["outcome"] or "no_action"
    if sess["call_id"] is None:
        cur = conn.execute("INSERT INTO calls(started_at,channel,caller_phone,transcript,summary,outcome) VALUES(?,?,?,?,?,?)",
                           (sess["started"], sess["channel"], sess["slots"].get("phone"), transcript, summary, outcome))
        sess["call_id"] = cur.lastrowid
    else:
        conn.execute("UPDATE calls SET caller_phone=?, transcript=?, summary=?, outcome=? WHERE id=?",
                     (sess["slots"].get("phone"), transcript, summary, outcome, sess["call_id"]))
    conn.commit()
