import datetime as dt
import os
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import agent, auth, clinical, comms, db, pm, routes_social
from . import scheduling as S

STATIC = os.path.join(os.path.dirname(__file__), "static")


@asynccontextmanager
async def lifespan(app):
    db.init(seed=True).close()
    print("Staff login: " + ("ON (user '" + os.environ.get("ADMIN_USER", "admin") + "', password from ADMIN_PASSWORD)"
                              if auth.enabled() else "OFF (anyone who can reach this address can use the app)"))
    print("Confirmation emails: " + ("ON via Gmail (" + os.environ["GMAIL_USER"] + ")" if comms.email_enabled()
          else "OFF - set GMAIL_USER and GMAIL_APP_PASSWORD (see README)"))
    yield


app = FastAPI(title="Dentify Demo - AI Receptionist + Practice Hub", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")
app.middleware("http")(auth.middleware)
app.include_router(routes_social.router)
app.include_router(clinical.router)
app.include_router(pm.router)


def rows(conn, q, args=()):
    return [dict(r) for r in conn.execute(q, args).fetchall()]


def conn_():
    return db.connect()


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC, "index.html"))


@app.get("/api/health")
def health():
    return {"ok": True, "now": S.iso(S.now()), "llm": os.environ.get("LLM_BACKEND", "none"), "auth": auth.enabled()}


@app.get("/api/config")
def config():
    return {"practice": comms.PRACTICE,
            "types": {k: {"name": v[0], "minutes": v[1], "color": v[2]} for k, v in S.TYPES.items()},
            "hours": {str(k): v for k, v in S.HOURS.items()}}


# ---------------------------------------------------------------- providers / patients
@app.get("/api/providers")
def providers():
    c = conn_()
    return rows(c, "SELECT * FROM providers ORDER BY id")


class PatientIn(BaseModel):
    name: str
    phone: str
    email: Optional[str] = None
    notes: Optional[str] = None


@app.get("/api/patients")
def patients(q: str = ""):
    c = conn_()
    like = f"%{q}%"
    return rows(c, """
        SELECT p.*,
          (SELECT MAX(start) FROM appointments a WHERE a.patient_id=p.id AND a.status='completed') AS last_visit,
          (SELECT MIN(start) FROM appointments a WHERE a.patient_id=p.id AND a.status='scheduled' AND a.start>=?) AS next_appt
        FROM patients p WHERE p.name LIKE ? OR p.phone LIKE ? ORDER BY p.name""", (S.iso(S.now()), like, like))


@app.post("/api/patients", status_code=201)
def add_patient(p: PatientIn):
    c = conn_()
    try:
        pid = S.create_patient(c, p.name, p.phone, p.email, p.notes)
    except S.Conflict as e:
        raise HTTPException(409, str(e))
    except ValueError as e:
        raise HTTPException(422, str(e))
    return {"id": pid}


@app.get("/api/patients/{pid}")
def patient_detail(pid: int):
    c = conn_()
    p = c.execute("SELECT * FROM patients WHERE id=?", (pid,)).fetchone()
    if not p:
        raise HTTPException(404, "patient not found")
    return {"patient": dict(p),
            "appointments": rows(c, "SELECT * FROM appointments WHERE patient_id=? ORDER BY start DESC", (pid,)),
            "invoices": rows(c, "SELECT * FROM invoices WHERE patient_id=? ORDER BY id DESC", (pid,)),
            "messages": rows(c, "SELECT * FROM messages WHERE patient_id=? ORDER BY id DESC", (pid,))}


# ---------------------------------------------------------------- appointments
class ApptIn(BaseModel):
    patient_id: int
    type: str
    start: str
    provider_id: Optional[int] = None


class ApptPatch(BaseModel):
    start: Optional[str] = None
    provider_id: Optional[int] = None
    status: Optional[str] = None


@app.get("/api/availability")
def availability(date: str, type: str = "checkup"):
    if type not in S.TYPES:
        raise HTTPException(422, "unknown type")
    c = conn_()
    return {"date": date, "type": type, "slots": S.free_slots(c, dt.date.fromisoformat(date), type)}


@app.get("/api/appointments")
def appointments(start: Optional[str] = None, end: Optional[str] = None):
    c = conn_()
    q = ("SELECT a.*, p.name AS patient_name, p.phone AS patient_phone, pr.name AS provider_name, "
         "pr.color AS provider_color FROM appointments a JOIN patients p ON p.id=a.patient_id "
         "JOIN providers pr ON pr.id=a.provider_id WHERE a.status != 'cancelled'")
    args = []
    if start:
        q += " AND a.end > ?"
        args.append(start[:16])
    if end:
        q += " AND a.start < ?"
        args.append(end[:16])
    return rows(c, q + " ORDER BY a.start", args)


@app.post("/api/appointments", status_code=201)
def create_appt(a: ApptIn):
    c = conn_()
    try:
        aid = S.book(c, a.patient_id, a.type, a.start, "hub", a.provider_id)
    except S.Conflict as e:
        raise HTTPException(409, str(e))
    except ValueError as e:
        raise HTTPException(422, str(e))
    comms.confirmation(c, aid)
    return {"id": aid}


@app.patch("/api/appointments/{aid}")
def patch_appt(aid: int, p: ApptPatch):
    c = conn_()
    if not S.get_appt(c, aid):
        raise HTTPException(404, "appointment not found")
    try:
        if p.start:
            S.reschedule(c, aid, p.start, p.provider_id)
        if p.status:
            if p.status not in ("scheduled", "completed", "no_show", "cancelled"):
                raise HTTPException(422, "bad status")
            S.set_status(c, aid, p.status)
            if p.status == "completed":
                comms.review_invite(c, aid)
    except S.Conflict as e:
        raise HTTPException(409, str(e))
    except ValueError as e:
        raise HTTPException(422, str(e))
    return S.get_appt(c, aid)


@app.delete("/api/appointments/{aid}")
def cancel_appt(aid: int):
    c = conn_()
    if not S.get_appt(c, aid):
        raise HTTPException(404, "appointment not found")
    S.set_status(c, aid, "cancelled")
    return {"ok": True}


# ---------------------------------------------------------------- billing
class InvoiceIn(BaseModel):
    patient_id: int
    description: str
    amount: float


@app.get("/api/invoices")
def invoices():
    c = conn_()
    return rows(c, "SELECT i.*, p.name AS patient_name FROM invoices i JOIN patients p ON p.id=i.patient_id ORDER BY i.id DESC LIMIT 100")


@app.post("/api/invoices", status_code=201)
def add_invoice(i: InvoiceIn):
    c = conn_()
    cur = c.execute("INSERT INTO invoices(patient_id,description,amount,created_at) VALUES(?,?,?,?)",
                    (i.patient_id, i.description, i.amount, S.iso(S.now())))
    c.commit()
    return {"id": cur.lastrowid}


@app.post("/api/invoices/{iid}/pay")
def pay_invoice(iid: int):
    c = conn_()
    c.execute("UPDATE invoices SET status='paid' WHERE id=?", (iid,))
    c.commit()
    return {"ok": True}


@app.get("/pay/{iid}", response_class=HTMLResponse)
def pay_page(iid: int):
    c = conn_()
    r = c.execute("SELECT i.*, p.name AS pn FROM invoices i JOIN patients p ON p.id=i.patient_id WHERE i.id=?", (iid,)).fetchone()
    if not r:
        raise HTTPException(404, "invoice not found")
    if r["status"] == "paid":
        action = '<p style="color:green">Paid. Thank you!</p>'
    else:
        action = ('<button style="padding:12px 28px;font-size:18px" '
                  'onclick="fetch(\'/api/invoices/ID/pay\',{method:\'POST\'}).then(()=>location.reload())">'
                  'Pay now (demo)</button>').replace("ID", str(iid))
    return ('<body style="font-family:system-ui;max-width:420px;margin:60px auto;text-align:center">'
            f'<h2>{comms.PRACTICE}</h2><p>Invoice #{iid} for {r["pn"]}</p>'
            f'<h1>${r["amount"]:.2f}</h1><p>{r["description"]}</p>{action}</body>')


# ---------------------------------------------------------------- outbox, reminders, reports
@app.get("/api/messages")
def messages():
    c = conn_()
    return rows(c, "SELECT m.*, p.name AS patient_name FROM messages m LEFT JOIN patients p ON p.id=m.patient_id ORDER BY m.id DESC LIMIT 100")


@app.post("/api/reminders/run")
def run_reminders():
    return {"sent": comms.run_reminders(conn_())}


@app.post("/api/recall/run")
def run_recall():
    return {"sent": comms.run_recall(conn_())}


@app.get("/api/calls")
def calls():
    c = conn_()
    return rows(c, "SELECT * FROM calls ORDER BY id DESC LIMIT 50")


@app.get("/api/reports")
def reports():
    c = conn_()
    n = S.now()
    d30, d90 = S.iso(n - dt.timedelta(days=30)), S.iso(n - dt.timedelta(days=90))
    one = lambda q, a=(): c.execute(q, a).fetchone()[0] or 0
    done = one("SELECT COUNT(*) FROM appointments WHERE status='completed' AND start>=?", (d90,))
    ns = one("SELECT COUNT(*) FROM appointments WHERE status='no_show' AND start>=?", (d90,))
    return {
        "upcoming_7d": one("SELECT COUNT(*) FROM appointments WHERE status='scheduled' AND start>? AND start<=?",
                           (S.iso(n), S.iso(n + dt.timedelta(days=7)))),
        "new_patients_30d": one("SELECT COUNT(*) FROM patients WHERE created_at>=?", (d30,)),
        "no_show_rate": round(100 * ns / (done + ns), 1) if done + ns else 0,
        "calls_30d": one("SELECT COUNT(*) FROM calls WHERE started_at>=?", (d30,)),
        "ai_booked_total": one("SELECT COUNT(*) FROM appointments WHERE source LIKE 'ai%' AND status!='cancelled' AND start>=?", (d30,)),
        "revenue_30d": one("SELECT SUM(amount) FROM invoices WHERE status='paid' AND created_at>=?", (d30,)),
        "outstanding": one("SELECT SUM(amount) FROM invoices WHERE status='unpaid'"),
        "social_booked_30d": one("SELECT COUNT(*) FROM appointments WHERE status!='cancelled' AND start>=? AND "
                                 "(source LIKE 'web_%' OR source IN ('ai_instagram','ai_facebook','ai_linkedin','ai_other'))", (d30,)),
        "unread_inbox": one("SELECT COUNT(*) FROM social_threads WHERE unread>0"),
        "by_source": rows(c, "SELECT source, COUNT(*) AS n FROM appointments WHERE status!='cancelled' AND start>=? GROUP BY source ORDER BY n DESC", (d30,)),
        "by_type": rows(c, "SELECT type, COUNT(*) AS n FROM appointments WHERE status IN ('completed','scheduled') AND start>=? GROUP BY type ORDER BY n DESC", (d90,)),
    }


# ---------------------------------------------------------------- AI receptionist
class StartIn(BaseModel):
    channel: str = "voice"
    caller_phone: Optional[str] = None


class MsgIn(BaseModel):
    session_id: str
    text: str


class EndIn(BaseModel):
    session_id: str


def _sess(sid):
    s = agent.SESSIONS.get(sid)
    if not s:
        raise HTTPException(404, "unknown session")
    return s


@app.post("/api/agent/start")
def agent_start(b: StartIn):
    c = conn_()
    s = agent.new_session(b.channel if b.channel in ("voice", "chat") else "voice", b.caller_phone)
    reply = agent.greeting(s)
    s["history"].append(["agent", reply])
    agent.save_call(c, s)
    return {"session_id": s["id"], "reply": reply}


@app.post("/api/agent/message")
def agent_message(b: MsgIn):
    s = _sess(b.session_id)
    reply = agent.handle(conn_(), s, b.text)
    return {"reply": reply, "done": s["done"], "intent": s["intent"], "expect": s["expect"]}


@app.post("/api/agent/end")
def agent_end(b: EndIn):
    s = _sess(b.session_id)
    c = conn_()
    agent.save_call(c, s)
    out = {"summary": agent.summarize(s), "outcome": s["outcome"] or "no_action"}
    agent.SESSIONS.pop(b.session_id, None)
    return out
