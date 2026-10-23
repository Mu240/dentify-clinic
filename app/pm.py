"""Practice-management layer, the things a system like Curve Dental gives a front desk and a dentist:

  * fee schedule with procedure codes (CDT)         * medical alerts (allergies, blood thinners, premed)
  * insurance benefits + treatment estimates         * claims: draft -> submitted -> paid / denied
  * signed SOAP clinical notes                       * printable treatment plan the patient can accept online
  * morning huddle and practice KPIs (production, collections, case acceptance, A/R aging)

Money flow: finishing a treatment raises a claim for the insurance share and an invoice for the patient
share. A denied or short-paid claim moves the unpaid part onto the patient's account automatically."""
import datetime as dt
import hmac
import html
import os
import re
import secrets
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from . import comms, db
from . import scheduling as S

router = APIRouter()
PCT_CLASSES = ("preventive", "basic", "major")      # cosmetic is never covered
# What the schedule books for each appointment type, used to estimate production for the day.
TYPE_CODE = {"checkup": "D0120", "cleaning": "D1110", "filling": "D2391", "whitening": "D9972",
             "implant": "D0150", "rootcanal": "D3330", "emergency": "D9110"}
THINNERS = ("warfarin", "coumadin", "eliquis", "apixaban", "xarelto", "rivaroxaban", "clopidogrel", "plavix")


class PMError(ValueError):
    pass


def _rows(conn, q, args=()):
    return [dict(r) for r in conn.execute(q, args).fetchall()]


def _one(conn, q, args=()):
    r = conn.execute(q, args).fetchone()
    return dict(r) if r else None


def _need_patient(conn, pid):
    p = _one(conn, "SELECT * FROM patients WHERE id=?", (pid,))
    if not p:
        raise HTTPException(404, "patient not found")
    return p


def money(x):
    return f"${x:,.2f}"


# ------------------------------------------------------------------ medical alerts
def alerts_for(m):
    """Short red flags a clinician must see before touching the patient."""
    if not m:
        return []
    out = []
    for a in re.split(r"[;,\n]+", m.get("allergies") or ""):
        if a.strip():
            out.append(f"Allergy: {a.strip()}")
    meds = (m.get("medications") or "").lower()
    if any(t in meds for t in THINNERS):
        out.append("On blood thinner")
    if m.get("premed"):
        out.append("Antibiotic premed required")
    for c in re.split(r"[;,\n]+", m.get("conditions") or ""):
        if c.strip():
            out.append(c.strip())
    return out


# ------------------------------------------------------------------ insurance estimates
def get_plan(conn, pid):
    return _one(conn, "SELECT * FROM insurance_plans WHERE patient_id=?", (pid,))


def estimate_items(plan, items):
    """items: [{fee, cls}] in treatment order. Applies the deductible (not to preventive care),
    the plan's coverage % per class, and the remaining annual maximum. Estimates only."""
    out = []
    left = max(0.0, plan["annual_max"] - plan["used"]) if plan else 0.0
    ded = max(0.0, plan["deductible"] - plan["deductible_met"]) if plan else 0.0
    for it in items:
        fee, cls = float(it["fee"]), it.get("cls")
        pct = (plan[f"{cls}_pct"] if plan and cls in PCT_CLASSES else 0) / 100
        d = 0.0
        base = fee
        if pct and cls != "preventive" and ded > 0:
            d = min(ded, fee)
            ded -= d
            base = fee - d
        ins = round(min(base * pct, left), 2)
        left = round(left - ins, 2)
        out.append({**it, "insurance": ins, "patient": round(fee - ins, 2), "deductible_applied": round(d, 2)})
    return out


def _tx_rows(conn, pid, statuses):
    q = ",".join("?" * len(statuses))
    return _rows(conn, f"SELECT t.*, f.coverage_class AS cls, f.name AS code_name FROM treatments t "
                       f"LEFT JOIN fee_schedule f ON f.code=t.code WHERE t.patient_id=? AND t.status IN ({q}) "
                       f"ORDER BY t.id", (pid, *statuses))


def plan_estimate(conn, pid):
    plan = get_plan(conn, pid)
    items = estimate_items(plan, _tx_rows(conn, pid, ("planned", "accepted")))
    return {"plan": plan, "items": items,
            "totals": {"fee": round(sum(i["fee"] for i in items), 2),
                       "insurance": round(sum(i["insurance"] for i in items), 2),
                       "patient": round(sum(i["patient"] for i in items), 2)},
            "benefits": ({"annual_max": plan["annual_max"], "remaining": round(plan["annual_max"] - plan["used"], 2),
                          "deductible_remaining": round(max(0, plan["deductible"] - plan["deductible_met"]), 2)}
                         if plan else None)}


# ------------------------------------------------------------------ treatment -> claim + invoice
def complete_treatment(conn, tid, at=None):
    """Mark a treatment done and raise the money documents. Idempotent."""
    t = _one(conn, "SELECT t.*, f.coverage_class AS cls FROM treatments t "
                   "LEFT JOIN fee_schedule f ON f.code=t.code WHERE t.id=?", (tid,))
    if not t:
        raise HTTPException(404, "treatment not found")
    claim = _one(conn, "SELECT id FROM claims WHERE treatment_id=?", (tid,))
    if t["status"] == "done":
        return {"invoice_id": t["invoice_id"], "claim_id": claim["id"] if claim else None}
    at = at or S.iso(S.now())
    plan = get_plan(conn, t["patient_id"])
    est = estimate_items(plan, [{"fee": t["fee"], "cls": t["cls"]}])[0]
    label = t["procedure"] + (f" (tooth #{t['tooth']})" if t["tooth"] else "")
    claim_id = None
    if est["insurance"] > 0:
        cur = conn.execute("INSERT INTO claims(patient_id,treatment_id,carrier,code,description,billed,expected,created_at) "
                           "VALUES(?,?,?,?,?,?,?,?)",
                           (t["patient_id"], tid, plan["carrier"], t["code"], label, t["fee"], est["insurance"], at))
        claim_id = cur.lastrowid
        conn.execute("UPDATE insurance_plans SET deductible_met=deductible_met+? WHERE patient_id=?",
                     (est["deductible_applied"], t["patient_id"]))
    invoice_id = None
    if est["patient"] > 0:
        cur = conn.execute("INSERT INTO invoices(patient_id,description,amount,created_at) VALUES(?,?,?,?)",
                           (t["patient_id"], label + (" - patient portion" if claim_id else ""), est["patient"], at))
        invoice_id = cur.lastrowid
    conn.execute("UPDATE treatments SET status='done', invoice_id=? WHERE id=?", (invoice_id, tid))
    conn.commit()
    return {"invoice_id": invoice_id, "claim_id": claim_id}


def _claim(conn, cid):
    c = _one(conn, "SELECT * FROM claims WHERE id=?", (cid,))
    if not c:
        raise HTTPException(404, "claim not found")
    return c


def submit_claim(conn, cid, at=None):
    c = _claim(conn, cid)
    if c["status"] != "draft":
        raise PMError(f"claim is already {c['status']}")
    conn.execute("UPDATE claims SET status='submitted', submitted_at=? WHERE id=?", (at or S.iso(S.now()), cid))
    conn.commit()


def _bill_patient(conn, pid, desc, amount, at):
    if amount > 0.004:
        conn.execute("INSERT INTO invoices(patient_id,description,amount,created_at) VALUES(?,?,?,?)",
                     (pid, desc, round(amount, 2), at))


def pay_claim(conn, cid, amount=None, at=None):
    c = _claim(conn, cid)
    if c["status"] != "submitted":
        raise PMError("only a submitted claim can be marked paid")
    amount = c["expected"] if amount is None else float(amount)
    if amount < 0 or amount > c["billed"]:
        raise PMError("payment must be between $0 and the billed amount")
    at = at or S.iso(S.now())
    conn.execute("UPDATE claims SET status='paid', paid=?, resolved_at=? WHERE id=?", (amount, at, cid))
    conn.execute("UPDATE insurance_plans SET used=used+? WHERE patient_id=?", (amount, c["patient_id"]))
    _bill_patient(conn, c["patient_id"], f"Insurance shortfall, claim #{cid} ({c['description']})", c["expected"] - amount, at)
    conn.commit()


def deny_claim(conn, cid, reason, at=None):
    c = _claim(conn, cid)
    if c["status"] != "submitted":
        raise PMError("only a submitted claim can be denied")
    at = at or S.iso(S.now())
    conn.execute("UPDATE claims SET status='denied', note=?, resolved_at=? WHERE id=?", (reason or "No reason given", at, cid))
    _bill_patient(conn, c["patient_id"], f"Insurance denied claim #{cid} ({c['description']})", c["expected"], at)
    conn.commit()


def _guard(fn, *a):
    try:
        return fn(*a)
    except PMError as e:
        raise HTTPException(409, str(e))


# ------------------------------------------------------------------ fee schedule
@router.get("/api/fees")
def fees():
    return _rows(db.connect(), "SELECT * FROM fee_schedule ORDER BY code")


# ------------------------------------------------------------------ patient profile: medical, insurance, notes
@router.get("/api/patients/{pid}/profile")
def profile(pid: int):
    c = db.connect()
    _need_patient(c, pid)
    med = _one(c, "SELECT * FROM patient_medical WHERE patient_id=?", (pid,)) or \
        {"allergies": "", "conditions": "", "medications": "", "premed": 0}
    plan = get_plan(c, pid)
    return {"medical": med, "alerts": alerts_for(med), "insurance": plan,
            "benefits": ({"remaining": round(plan["annual_max"] - plan["used"], 2),
                          "deductible_remaining": round(max(0, plan["deductible"] - plan["deductible_met"]), 2)}
                         if plan else None),
            "balance": _one(c, "SELECT COALESCE(SUM(amount),0) AS b FROM invoices WHERE patient_id=? AND status='unpaid'", (pid,))["b"],
            "claims": _rows(c, "SELECT * FROM claims WHERE patient_id=? ORDER BY id DESC", (pid,)),
            "notes": _rows(c, "SELECT n.*, p.name AS provider_name FROM clinical_notes n "
                              "LEFT JOIN providers p ON p.id=n.provider_id WHERE n.patient_id=? ORDER BY n.id DESC", (pid,))}


class MedicalIn(BaseModel):
    allergies: Optional[str] = ""
    conditions: Optional[str] = ""
    medications: Optional[str] = ""
    premed: bool = False


@router.put("/api/patients/{pid}/medical")
def set_medical(pid: int, b: MedicalIn):
    c = db.connect()
    _need_patient(c, pid)
    c.execute("INSERT INTO patient_medical(patient_id,allergies,conditions,medications,premed,updated_at) VALUES(?,?,?,?,?,?) "
              "ON CONFLICT(patient_id) DO UPDATE SET allergies=excluded.allergies, conditions=excluded.conditions, "
              "medications=excluded.medications, premed=excluded.premed, updated_at=excluded.updated_at",
              (pid, (b.allergies or "").strip(), (b.conditions or "").strip(), (b.medications or "").strip(),
               int(b.premed), S.iso(S.now())))
    c.commit()
    return {"ok": True, "alerts": alerts_for(_one(c, "SELECT * FROM patient_medical WHERE patient_id=?", (pid,)))}


class InsuranceIn(BaseModel):
    carrier: str
    member_id: Optional[str] = None
    group_no: Optional[str] = None
    subscriber: Optional[str] = None
    annual_max: float = 1500
    used: float = 0
    deductible: float = 50
    deductible_met: float = 0
    preventive_pct: int = 100
    basic_pct: int = 80
    major_pct: int = 50


@router.put("/api/patients/{pid}/insurance")
def set_insurance(pid: int, b: InsuranceIn):
    if not b.carrier.strip():
        raise HTTPException(422, "carrier is required")
    if min(b.annual_max, b.used, b.deductible, b.deductible_met) < 0:
        raise HTTPException(422, "amounts cannot be negative")
    if not all(0 <= x <= 100 for x in (b.preventive_pct, b.basic_pct, b.major_pct)):
        raise HTTPException(422, "coverage percentages must be 0-100")
    c = db.connect()
    _need_patient(c, pid)
    c.execute("INSERT INTO insurance_plans(patient_id,carrier,member_id,group_no,subscriber,annual_max,used,deductible,"
              "deductible_met,preventive_pct,basic_pct,major_pct,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) "
              "ON CONFLICT(patient_id) DO UPDATE SET carrier=excluded.carrier, member_id=excluded.member_id, "
              "group_no=excluded.group_no, subscriber=excluded.subscriber, annual_max=excluded.annual_max, used=excluded.used, "
              "deductible=excluded.deductible, deductible_met=excluded.deductible_met, preventive_pct=excluded.preventive_pct, "
              "basic_pct=excluded.basic_pct, major_pct=excluded.major_pct, updated_at=excluded.updated_at",
              (pid, b.carrier.strip(), b.member_id, b.group_no, b.subscriber, b.annual_max, b.used, b.deductible,
               b.deductible_met, b.preventive_pct, b.basic_pct, b.major_pct, S.iso(S.now())))
    c.commit()
    return {"ok": True}


@router.delete("/api/patients/{pid}/insurance")
def remove_insurance(pid: int):
    c = db.connect()
    _need_patient(c, pid)
    c.execute("DELETE FROM insurance_plans WHERE patient_id=?", (pid,))
    c.commit()
    return {"ok": True}


class NoteIn(BaseModel):
    subjective: Optional[str] = ""
    objective: Optional[str] = ""
    assessment: Optional[str] = ""
    plan: Optional[str] = ""
    appointment_id: Optional[int] = None


def _note_ok(b):
    if not any((getattr(b, f) or "").strip() for f in ("subjective", "objective", "assessment", "plan")):
        raise HTTPException(422, "write something in at least one section")


@router.post("/api/patients/{pid}/notes", status_code=201)
def add_note(pid: int, b: NoteIn):
    _note_ok(b)
    c = db.connect()
    _need_patient(c, pid)
    prov = None
    if b.appointment_id:
        a = _one(c, "SELECT provider_id FROM appointments WHERE id=? AND patient_id=?", (b.appointment_id, pid))
        if not a:
            raise HTTPException(404, "appointment not found for this patient")
        prov = a["provider_id"]
    if prov is None:
        first = _one(c, "SELECT id FROM providers ORDER BY id LIMIT 1")
        prov = first["id"] if first else None
    cur = c.execute("INSERT INTO clinical_notes(patient_id,appointment_id,provider_id,subjective,objective,assessment,plan,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (pid, b.appointment_id, prov, b.subjective, b.objective, b.assessment, b.plan, S.iso(S.now())))
    c.commit()
    return {"id": cur.lastrowid}


@router.put("/api/notes/{nid}")
def edit_note(nid: int, b: NoteIn):
    _note_ok(b)
    c = db.connect()
    n = _one(c, "SELECT * FROM clinical_notes WHERE id=?", (nid,))
    if not n:
        raise HTTPException(404, "note not found")
    if n["signed_at"]:
        raise HTTPException(409, "this note is signed and locked")
    c.execute("UPDATE clinical_notes SET subjective=?, objective=?, assessment=?, plan=? WHERE id=?",
              (b.subjective, b.objective, b.assessment, b.plan, nid))
    c.commit()
    return {"ok": True}


@router.post("/api/notes/{nid}/sign")
def sign_note(nid: int):
    c = db.connect()
    n = _one(c, "SELECT * FROM clinical_notes WHERE id=?", (nid,))
    if not n:
        raise HTTPException(404, "note not found")
    if not n["signed_at"]:
        c.execute("UPDATE clinical_notes SET signed_at=? WHERE id=?", (S.iso(S.now()), nid))
        c.commit()
    return {"ok": True}


# ------------------------------------------------------------------ estimate, claims
@router.get("/api/patients/{pid}/estimate")
def estimate(pid: int):
    c = db.connect()
    _need_patient(c, pid)
    return plan_estimate(c, pid)


@router.get("/api/claims")
def claims(status: str = ""):
    c = db.connect()
    q = "SELECT cl.*, p.name AS patient_name FROM claims cl JOIN patients p ON p.id=cl.patient_id"
    args = ()
    if status:
        q, args = q + " WHERE cl.status=?", (status,)
    return _rows(c, q + " ORDER BY cl.id DESC LIMIT 200", args)


class PayIn(BaseModel):
    amount: Optional[float] = None


class DenyIn(BaseModel):
    reason: Optional[str] = None


@router.post("/api/claims/{cid}/submit")
def claim_submit(cid: int):
    _guard(submit_claim, db.connect(), cid)
    return {"ok": True}


@router.post("/api/claims/{cid}/pay")
def claim_pay(cid: int, b: PayIn = PayIn()):
    _guard(pay_claim, db.connect(), cid, b.amount)
    return {"ok": True}


@router.post("/api/claims/{cid}/deny")
def claim_deny(cid: int, b: DenyIn = DenyIn()):
    _guard(deny_claim, db.connect(), cid, b.reason)
    return {"ok": True}


# ------------------------------------------------------------------ morning huddle
def unscheduled_treatment(conn):
    """Accepted treatment with no future appointment: money the practice has earned the right to book."""
    return _rows(conn, """
        SELECT p.id AS patient_id, p.name, ROUND(SUM(t.fee),2) AS amount, COUNT(*) AS n
        FROM treatments t JOIN patients p ON p.id=t.patient_id
        WHERE t.status='accepted' AND NOT EXISTS (
            SELECT 1 FROM appointments a WHERE a.patient_id=t.patient_id AND a.status='scheduled' AND a.start>=?)
        GROUP BY p.id ORDER BY amount DESC""", (S.iso(S.now()),))


@router.get("/api/huddle")
def huddle(date: Optional[str] = None):
    c = db.connect()
    d = dt.date.fromisoformat(date) if date else S.now().date()

    def day_rows(day):
        return _rows(c, "SELECT a.*, p.name AS patient_name, p.phone, pr.name AS provider_name, pr.color AS provider_color "
                        "FROM appointments a JOIN patients p ON p.id=a.patient_id JOIN providers pr ON pr.id=a.provider_id "
                        "WHERE a.start LIKE ? AND a.status IN ('scheduled','completed') ORDER BY a.start", (day.isoformat() + "%",))
    appts = day_rows(d)
    for _ in range(8 if not date else 0):          # nothing today (e.g. Sunday): show the next busy day
        if appts:
            break
        d += dt.timedelta(days=1)
        appts = day_rows(d)
    prod = 0.0
    out = []
    for a in appts:
        pid = a["patient_id"]
        med = _one(c, "SELECT * FROM patient_medical WHERE patient_id=?", (pid,))
        plan = get_plan(c, pid)
        fee = _one(c, "SELECT fee FROM fee_schedule WHERE code=?", (TYPE_CODE.get(a["type"], ""),))
        prod += fee["fee"] if fee else 0
        out.append({"id": a["id"], "start": a["start"], "type": a["type"], "status": a["status"],
                    "patient_id": pid, "patient_name": a["patient_name"], "provider_name": a["provider_name"],
                    "provider_color": a["provider_color"], "alerts": alerts_for(med),
                    "balance": _one(c, "SELECT COALESCE(SUM(amount),0) AS b FROM invoices WHERE patient_id=? AND status='unpaid'", (pid,))["b"],
                    "tx_accepted": _one(c, "SELECT COALESCE(SUM(fee),0) AS b FROM treatments WHERE patient_id=? AND status='accepted'", (pid,))["b"],
                    "carrier": plan["carrier"] if plan else None,
                    "benefits_left": round(plan["annual_max"] - plan["used"], 2) if plan else None})
    unsched = unscheduled_treatment(c)
    return {"date": d.isoformat(), "is_today": d == S.now().date(), "appointments": out,
            "production": round(prod, 2), "alerts_count": sum(1 for a in out if a["alerts"]),
            "unscheduled": unsched[:6], "unscheduled_total": round(sum(u["amount"] for u in unsched), 2)}


# ------------------------------------------------------------------ KPIs
def _aging(rows_, n):
    b = {"0-30": 0.0, "31-60": 0.0, "61-90": 0.0, "90+": 0.0}
    for amount, date in rows_:
        days = (n - S.parse(date)).days
        b["0-30" if days <= 30 else "31-60" if days <= 60 else "61-90" if days <= 90 else "90+"] += amount
    return {k: round(v, 2) for k, v in b.items()}


@router.get("/api/kpis")
def kpis():
    c = db.connect()
    n = S.now()
    d30 = S.iso(n - dt.timedelta(days=30))
    one = lambda q, a=(): c.execute(q, a).fetchone()[0] or 0
    production = one("SELECT SUM(amount) FROM invoices WHERE created_at>=?", (d30,)) + \
        one("SELECT SUM(expected) FROM claims WHERE created_at>=?", (d30,))
    collections = one("SELECT SUM(amount) FROM invoices WHERE status='paid' AND created_at>=?", (d30,)) + \
        one("SELECT SUM(paid) FROM claims WHERE status='paid' AND resolved_at>=?", (d30,))
    acc = lambda col, sts: one(f"SELECT SUM({col}) FROM treatments WHERE status IN ({','.join('?' * len(sts))})", sts)
    presented = acc("fee", ("planned", "accepted", "done", "declined"))
    accepted = acc("fee", ("accepted", "done"))
    unsched = unscheduled_treatment(c)
    prov = _rows(c, """SELECT pr.name AS provider, pr.color, COUNT(*) AS visits,
                              ROUND(SUM(COALESCE(f.fee,0)),2) AS production
                       FROM appointments a JOIN providers pr ON pr.id=a.provider_id
                       LEFT JOIN fee_schedule f ON f.code = CASE a.type WHEN 'checkup' THEN 'D0120' WHEN 'cleaning' THEN 'D1110'
                            WHEN 'filling' THEN 'D2391' WHEN 'whitening' THEN 'D9972' WHEN 'implant' THEN 'D0150'
                            WHEN 'rootcanal' THEN 'D3330' ELSE 'D9110' END
                       WHERE a.status='completed' AND a.start>=? GROUP BY pr.id ORDER BY production DESC""", (d30,))
    return {"production_30d": round(production, 2), "collections_30d": round(collections, 2),
            "collection_ratio": round(100 * collections / production, 1) if production else 0,
            "case_acceptance": round(100 * accepted / presented, 1) if presented else 0,
            "presented_total": round(presented, 2), "accepted_total": round(accepted, 2),
            "declined_total": round(acc("fee", ("declined",)), 2),
            "unscheduled_total": round(sum(u["amount"] for u in unsched), 2),
            "patient_ar": _aging([(r["amount"], r["created_at"]) for r in
                                  _rows(c, "SELECT amount, created_at FROM invoices WHERE status='unpaid'")], n),
            "insurance_ar": _aging([(r["expected"], r["submitted_at"]) for r in
                                    _rows(c, "SELECT expected, submitted_at FROM claims WHERE status='submitted'")], n),
            "unbilled_claims": round(one("SELECT SUM(expected) FROM claims WHERE status='draft'"), 2),
            "claims_open": one("SELECT COUNT(*) FROM claims WHERE status IN ('draft','submitted')"),
            "by_provider": prov}


# ------------------------------------------------------------------ patient-facing treatment plan (shareable link)
def _secret():
    """Signing key for patient links: PORTAL_SECRET if set, otherwise a random key generated once and kept in
    the database (so links stay valid across restarts and cannot be guessed from the source code)."""
    if os.environ.get("PORTAL_SECRET"):
        return os.environ["PORTAL_SECRET"]
    c = db.connect()
    try:
        c.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('portal_secret', ?)", (secrets.token_hex(32),))
        c.commit()
        return c.execute("SELECT value FROM settings WHERE key='portal_secret'").fetchone()[0]
    finally:
        c.close()


def plan_token(pid):
    return hmac.new(_secret().encode(), f"plan:{pid}".encode(), "sha256").hexdigest()[:20]


def plan_url(pid, request=None):
    """Absolute link: PUBLIC_URL if configured, otherwise the address this request came in on."""
    base = os.environ.get("PUBLIC_URL", "").rstrip("/") or (str(request.base_url).rstrip("/") if request else "")
    return f"{base}/plan/{pid}/{plan_token(pid)}"


def _check_token(pid, token):
    if not hmac.compare_digest(token, plan_token(pid)):
        raise HTTPException(404, "link not found")


@router.get("/api/patients/{pid}/plan-link")
def plan_link(pid: int, request: Request):
    _need_patient(db.connect(), pid)
    return {"url": plan_url(pid, request), "path": f"/plan/{pid}/{plan_token(pid)}",
            "public": bool(os.environ.get("PUBLIC_URL"))}


@router.post("/api/patients/{pid}/plan/send")
def plan_send(pid: int, request: Request):
    """Email the patient their treatment plan link (real Gmail send when configured)."""
    c = db.connect()
    p = _need_patient(c, pid)
    est = plan_estimate(c, pid)
    if not est["items"]:
        raise HTTPException(409, "there is no planned treatment to send")
    url = plan_url(pid, request)
    return {"emailed": comms.email_plan(c, p, url, est), "email": p.get("email"),
            "local_link": any(h in url for h in ("localhost", "127.0.0.1", "testserver"))}


def _page(title, body):
    e = html.escape
    return (f"<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{e(title)}</title><style>body{{font-family:system-ui,Segoe UI,Arial,sans-serif;max-width:760px;margin:32px auto;"
            "padding:0 16px;color:#0f172a}h1{margin:0;color:#2563eb}table{width:100%;border-collapse:collapse;margin:14px 0}"
            "th,td{text-align:left;padding:8px;border-bottom:1px solid #e2e8f0}th{font-size:12px;color:#64748b;text-transform:uppercase}"
            "td.r,th.r{text-align:right}.tot td{font-weight:700;border-top:2px solid #0f172a}.mute{color:#64748b;font-size:13px}"
            ".btn{background:#2563eb;color:#fff;border:0;border-radius:8px;padding:12px 22px;font-size:16px;cursor:pointer;margin-right:8px}"
            ".btn.alt{background:#e2e8f0;color:#0f172a}.ok{color:#059669;font-weight:600}.sig{margin-top:40px;border-top:1px solid #0f172a;"
            "width:300px;padding-top:4px;font-size:13px;color:#64748b}@media print{.noprint{display:none}}</style></head>"
            f"<body>{body}</body></html>")


@router.get("/plan/{pid}/{token}", response_class=HTMLResponse)
def plan_page(pid: int, token: str):
    _check_token(pid, token)
    c = db.connect()
    p = _need_patient(c, pid)
    est = plan_estimate(c, pid)
    e = html.escape
    rows_ = "".join(
        f"<tr><td>{e(i['code'] or '')}</td><td>{e(i['procedure'])}{' (tooth #%d)' % i['tooth'] if i['tooth'] else ''}</td>"
        f"<td class='r'>{money(i['fee'])}</td><td class='r'>{money(i['insurance'])}</td><td class='r'><b>{money(i['patient'])}</b></td>"
        f"<td>{e(i['status'])}</td></tr>" for i in est["items"]) or "<tr><td colspan=6 class='mute'>No treatment is planned right now.</td></tr>"
    t = est["totals"]
    ins = (f"<p class='mute'>Insurance on file: <b>{e(est['plan']['carrier'])}</b>. Estimates apply your plan's coverage and remaining "
           f"benefits ({money(est['benefits']['remaining'])} left this year).</p>" if est["plan"] else
           "<p class='mute'>No insurance on file, so the full fee is your responsibility.</p>")
    open_ = any(i["status"] == "planned" for i in est["items"])
    action = ("<p class='noprint'><button class='btn' id='acc'>Accept treatment plan</button>"
              "<button class='btn alt' onclick='window.print()'>Print</button></p>" if est["items"] else "")
    body = (f"<h1>{e(comms.PRACTICE)}</h1><p class='mute'>Treatment plan &amp; estimate for <b>{e(p['name'])}</b> &middot; "
            f"{S.now().strftime('%b %d, %Y')}</p>{ins}"
            f"<table><thead><tr><th>Code</th><th>Procedure</th><th class='r'>Fee</th><th class='r'>Insurance est.</th>"
            f"<th class='r'>Your share</th><th>Status</th></tr></thead><tbody>{rows_}"
            f"<tr class='tot'><td colspan=2>Total</td><td class='r'>{money(t['fee'])}</td><td class='r'>{money(t['insurance'])}</td>"
            f"<td class='r'>{money(t['patient'])}</td><td></td></tr></tbody></table>"
            "<p class='mute'>This is an estimate, not a guarantee of payment. Your insurance company makes the final decision.</p>"
            f"{action}<p id='done' class='ok'>{'' if open_ else ('Your plan is accepted. Thank you!' if est['items'] else '')}</p>"
            "<div class='sig'>Patient signature / date</div>"
            f"<script>const b=document.getElementById('acc');if(b){{b.onclick=async()=>{{b.disabled=true;"
            f"const r=await fetch('/api/public/plan/{pid}/{token}/accept',{{method:'POST'}});"
            "const j=await r.json();document.getElementById('done').textContent=r.ok?'Thank you! Your treatment plan is accepted and "
            "our team will call you to book your visit.':(j.detail||'Something went wrong');};}</script>")
    return _page("Treatment plan", body)


@router.post("/api/public/plan/{pid}/{token}/accept")
def plan_accept(pid: int, token: str):
    _check_token(pid, token)
    c = db.connect()
    cur = c.execute("UPDATE treatments SET status='accepted' WHERE patient_id=? AND status='planned'", (pid,))
    c.commit()
    return {"accepted": cur.rowcount}


@router.get("/statement/{pid}", response_class=HTMLResponse)
def statement(pid: int):
    c = db.connect()
    p = _need_patient(c, pid)
    e = html.escape
    inv = _rows(c, "SELECT * FROM invoices WHERE patient_id=? ORDER BY id DESC", (pid,))
    cl = _rows(c, "SELECT * FROM claims WHERE patient_id=? AND status IN ('draft','submitted') ORDER BY id DESC", (pid,))
    due = sum(i["amount"] for i in inv if i["status"] == "unpaid")
    rows_ = "".join(f"<tr><td>{e(i['created_at'][:10])}</td><td>#{i['id']} {e(i['description'])}</td>"
                    f"<td class='r'>{money(i['amount'])}</td><td>{e(i['status'])}</td></tr>" for i in inv) or \
        "<tr><td colspan=4 class='mute'>No charges yet.</td></tr>"
    pend = "".join(f"<tr><td>{e((x['submitted_at'] or x['created_at'])[:10])}</td><td>{e(x['carrier'])}: {e(x['description'])}</td>"
                   f"<td class='r'>{money(x['expected'])}</td><td>{e(x['status'])}</td></tr>" for x in cl)
    body = (f"<h1>{e(comms.PRACTICE)}</h1><p class='mute'>Account statement for <b>{e(p['name'])}</b> &middot; {S.now().strftime('%b %d, %Y')}</p>"
            f"<h3>Charges</h3><table><thead><tr><th>Date</th><th>Description</th><th class='r'>Amount</th><th>Status</th></tr></thead>"
            f"<tbody>{rows_}<tr class='tot'><td colspan=2>Balance due</td><td class='r'>{money(due)}</td><td></td></tr></tbody></table>"
            + (f"<h3>Pending with insurance <span class='mute'>(not your balance)</span></h3><table><tbody>{pend}</tbody></table>" if pend else "")
            + "<p class='noprint'><button class='btn alt' onclick='window.print()'>Print</button></p>")
    return _page("Statement", body)
