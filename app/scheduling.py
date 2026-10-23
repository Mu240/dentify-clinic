"""Single source of truth for the calendar. The Hub UI *and* the AI receptionist both
call these functions, so they can never disagree about what is free."""
import datetime as dt
import re

HOURS = {0: (9, 17), 1: (9, 17), 2: (9, 17), 3: (9, 17), 4: (9, 17), 5: (9, 13)}  # Mon=0 ... Sat=5
SLOT = dt.timedelta(minutes=30)
FMT = "%Y-%m-%dT%H:%M"

# key: (display name, minutes, color)
TYPES = {
    "checkup":   ("Check-up & exam", 30, "#2563eb"),
    "cleaning":  ("Cleaning", 60, "#059669"),
    "filling":   ("Filling", 60, "#d97706"),
    "whitening": ("Whitening", 60, "#7c3aed"),
    "implant":   ("Implant consultation", 60, "#0891b2"),
    "rootcanal": ("Root canal", 90, "#dc2626"),
    "emergency": ("Emergency visit", 30, "#be123c"),
}


class Conflict(ValueError):
    pass


def now():
    return dt.datetime.now().replace(second=0, microsecond=0)


def iso(d):
    return d.strftime(FMT)


def parse(s):
    return dt.datetime.strptime(s[:16], FMT)


def type_name(t):
    return TYPES[t][0]


def duration(t):
    return dt.timedelta(minutes=TYPES[t][1])


def is_open_day(d):
    return d.weekday() in HOURS


def is_open_now(n=None):
    n = n or now()
    h = HOURS.get(n.weekday())
    return bool(h) and h[0] <= n.hour + n.minute / 60 < h[1]


# ---------- patients ----------
def norm_phone(s):
    digits = re.sub(r"\D", "", s or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits[-10:] if len(digits) > 10 else digits


def fmt_phone(d):
    return f"({d[:3]}) {d[3:6]}-{d[6:]}" if len(d) == 10 else d


def patient_by_phone(conn, phone):
    p = norm_phone(phone)
    if len(p) < 7:
        return None
    r = conn.execute("SELECT * FROM patients WHERE phone=?", (p,)).fetchone()
    return dict(r) if r else None


def create_patient(conn, name, phone, email=None, notes=None):
    p = norm_phone(phone)
    if len(p) < 7:
        raise ValueError("invalid phone number")
    try:
        cur = conn.execute(
            "INSERT INTO patients(name,phone,email,notes,created_at) VALUES(?,?,?,?,?)",
            (name.strip(), p, email, notes, iso(now())))
    except Exception as e:  # sqlite3.IntegrityError
        raise Conflict("a patient with this phone number already exists") from e
    conn.commit()
    return cur.lastrowid


# ---------- availability ----------
def _busy(conn, provider_id, start, end, exclude_id=None):
    q = ("SELECT 1 FROM appointments WHERE provider_id=? AND status='scheduled' "
         "AND start < ? AND end > ?")
    args = [provider_id, iso(end), iso(start)]
    if exclude_id:
        q += " AND id != ?"
        args.append(exclude_id)
    return conn.execute(q, args).fetchone() is not None


def find_provider(conn, start, end, exclude_id=None, prefer=None):
    ids = [r["id"] for r in conn.execute("SELECT id FROM providers ORDER BY id")]
    if prefer in ids:
        ids.remove(prefer)
        ids.insert(0, prefer)
    for pid in ids:
        if not _busy(conn, pid, start, end, exclude_id):
            return pid
    return None


def free_slots(conn, d, type_, exclude_id=None, allow_past=False):
    if d.weekday() not in HOURS:
        return []
    o, c = HOURS[d.weekday()]
    t = dt.datetime.combine(d, dt.time(o))
    close = dt.datetime.combine(d, dt.time(c))
    dur, n, out = duration(type_), now(), []
    while t + dur <= close:
        if (allow_past or t > n) and find_provider(conn, t, t + dur, exclude_id):
            out.append(t.strftime("%H:%M"))
        t += SLOT
    return out


def next_available_date(conn, type_, from_date, exclude_id=None, horizon=30):
    for i in range(horizon):
        d = from_date + dt.timedelta(days=i)
        if free_slots(conn, d, type_, exclude_id):
            return d
    return None


def _validate(start, end, allow_past):
    h = HOURS.get(start.weekday())
    if not h:
        raise ValueError("the practice is closed that day")
    if start.hour < h[0] or end > dt.datetime.combine(start.date(), dt.time(h[1])):
        raise ValueError("outside office hours")
    if start.minute % 30:
        raise ValueError("appointments start on the hour or half hour")
    if not allow_past and start <= now():
        raise ValueError("that time is in the past")


def book(conn, patient_id, type_, start, source="hub", provider_id=None,
         allow_past=False, status="scheduled"):
    if type_ not in TYPES:
        raise ValueError("unknown appointment type")
    start = parse(start) if isinstance(start, str) else start
    end = start + duration(type_)
    _validate(start, end, allow_past)
    if provider_id:
        if _busy(conn, provider_id, start, end):
            raise Conflict("that provider is already booked at that time")
        pid = provider_id
    else:
        pid = find_provider(conn, start, end)
        if pid is None:
            raise Conflict("no provider is free at that time")
    cur = conn.execute(
        "INSERT INTO appointments(patient_id,provider_id,type,start,end,status,source,created_at) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (patient_id, pid, type_, iso(start), iso(end), status, source, iso(now())))
    conn.commit()
    return cur.lastrowid


def get_appt(conn, appt_id):
    r = conn.execute(
        "SELECT a.*, p.name AS patient_name, p.phone AS patient_phone, pr.name AS provider_name "
        "FROM appointments a JOIN patients p ON p.id=a.patient_id "
        "JOIN providers pr ON pr.id=a.provider_id WHERE a.id=?", (appt_id,)).fetchone()
    return dict(r) if r else None


def reschedule(conn, appt_id, new_start, provider_id=None):
    a = get_appt(conn, appt_id)
    if not a:
        raise ValueError("appointment not found")
    start = parse(new_start) if isinstance(new_start, str) else new_start
    end = start + duration(a["type"])
    _validate(start, end, allow_past=False)
    if provider_id:
        if _busy(conn, provider_id, start, end, exclude_id=appt_id):
            raise Conflict("that provider is already booked at that time")
        pid = provider_id
    else:
        pid = find_provider(conn, start, end, exclude_id=appt_id, prefer=a["provider_id"])
        if pid is None:
            raise Conflict("no provider is free at that time")
    conn.execute("UPDATE appointments SET start=?, end=?, provider_id=? WHERE id=?",
                 (iso(start), iso(end), pid, appt_id))
    conn.commit()


def set_status(conn, appt_id, status):
    conn.execute("UPDATE appointments SET status=? WHERE id=?", (status, appt_id))
    conn.commit()


def upcoming(conn, patient_id):
    rows = conn.execute(
        "SELECT * FROM appointments WHERE patient_id=? AND status='scheduled' AND start>=? "
        "ORDER BY start", (patient_id, iso(now()))).fetchall()
    return [dict(r) for r in rows]
