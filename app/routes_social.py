"""HTTP routes for the social channels, the staff inbox and the public booking form.

Public (no login, safe to expose):  /book, /api/public/*, /webhooks/*
Staff (behind ADMIN_PASSWORD):       /api/inbox/*, /api/social/status
"""
import datetime as dt
import json
import os
import re
import time
from collections import defaultdict
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel

from . import comms, db, social
from . import scheduling as S

router = APIRouter()
STATIC = os.path.join(os.path.dirname(__file__), "static")
SOURCES = {"instagram", "facebook", "linkedin", "google", "web"}


def _rows(conn, q, args=()):
    return [dict(r) for r in conn.execute(q, args).fetchall()]


# ------------------------------------------------------------------ Meta (Instagram + Facebook)
@router.get("/webhooks/meta")
def meta_verify(request: Request):
    """Meta calls this once when you register the webhook."""
    q = request.query_params
    token = social.env("META_VERIFY_TOKEN")
    if token and q.get("hub.mode") == "subscribe" and q.get("hub.verify_token") == token:
        return PlainTextResponse(q.get("hub.challenge", ""))
    raise HTTPException(403, "verification failed")


def _process_batch(items):
    conn = db.connect()
    try:
        for m in items:
            try:
                social.process_inbound(conn, m["channel"], m["sender_id"], m["text"],
                                       external_id=m.get("external_id"))
            except Exception:                       # one bad message must not drop the rest
                import logging
                logging.getLogger("dentify.social").exception("failed to process inbound message")
    finally:
        conn.close()


@router.post("/webhooks/meta")
async def meta_inbound(request: Request, background: BackgroundTasks):
    raw = await request.body()
    if not social.signature_ok(raw, request.headers.get("x-hub-signature-256")):
        raise HTTPException(403, "bad signature")
    try:
        payload = json.loads(raw or b"{}")
    except ValueError:
        raise HTTPException(400, "invalid json")
    items = social.parse_meta(payload)
    if items:
        background.add_task(_process_batch, items)   # Meta wants a fast 200; replies go out after
    return {"received": len(items)}


# ------------------------------------------------------------------ generic channel (LinkedIn, Zapier, Make, ManyChat...)
class InboundIn(BaseModel):
    channel: str = "other"
    sender_id: str
    text: str
    sender_name: Optional[str] = None
    external_id: Optional[str] = None


@router.post("/webhooks/inbound")
def generic_inbound(b: InboundIn, x_inbound_secret: Optional[str] = Header(None)):
    """Any tool that can POST JSON can feed the receptionist. The reply comes back in the response;
    your automation sends it to the customer."""
    if not social.env("SOCIAL_INBOUND_SECRET"):
        raise HTTPException(503, "inbound endpoint disabled: set SOCIAL_INBOUND_SECRET")
    if not social.inbound_secret_ok(x_inbound_secret):
        raise HTTPException(403, "bad secret")
    conn = db.connect()
    try:
        reply = social.process_inbound(conn, b.channel, b.sender_id[:128], b.text, b.sender_name,
                                       b.external_id, send=False)
        th = social.get_thread(conn, b.channel if b.channel in social.CHANNELS else "other", b.sender_id[:128])
        return {"reply": reply, "handoff": bool(th["handoff"])}
    finally:
        conn.close()


# ------------------------------------------------------------------ staff inbox
@router.get("/api/social/status")
def social_status():
    return social.status()


@router.get("/api/inbox")
def inbox():
    c = db.connect()
    return _rows(c, """
        SELECT t.id, t.channel, t.sender_name, t.patient_id, t.handoff, t.unread, t.last_at, p.name AS patient_name,
          (SELECT body FROM social_messages m WHERE m.thread_id=t.id ORDER BY m.id DESC LIMIT 1) AS last_body
        FROM social_threads t LEFT JOIN patients p ON p.id=t.patient_id
        ORDER BY t.last_at DESC LIMIT 100""")


@router.get("/api/inbox/{tid}")
def inbox_thread(tid: int):
    c = db.connect()
    t = c.execute("SELECT * FROM social_threads WHERE id=?", (tid,)).fetchone()
    if not t:
        raise HTTPException(404, "thread not found")
    c.execute("UPDATE social_threads SET unread=0 WHERE id=?", (tid,))
    c.commit()
    return {"thread": {k: t[k] for k in ("id", "channel", "sender_name", "handoff", "patient_id")},
            "messages": _rows(c, "SELECT * FROM social_messages WHERE thread_id=? ORDER BY id", (tid,))}


class ReplyIn(BaseModel):
    text: str


@router.post("/api/inbox/{tid}/reply")
def inbox_reply(tid: int, b: ReplyIn):
    if not b.text.strip():
        raise HTTPException(422, "empty message")
    out = social.staff_reply(db.connect(), tid, b.text.strip())
    if out is None:
        raise HTTPException(404, "thread not found")
    return out


class HandoffIn(BaseModel):
    on: bool


@router.post("/api/inbox/{tid}/handoff")
def inbox_handoff(tid: int, b: HandoffIn):
    c = db.connect()
    if not c.execute("SELECT 1 FROM social_threads WHERE id=?", (tid,)).fetchone():
        raise HTTPException(404, "thread not found")
    c.execute("UPDATE social_threads SET handoff=? WHERE id=?", (int(b.on), tid))
    c.commit()
    return {"handoff": b.on}


# ------------------------------------------------------------------ public booking form
@router.get("/book")
def book_page():
    return FileResponse(os.path.join(STATIC, "book.html"))


@router.get("/api/public/config")
def public_config():
    types = {k: {"name": v[0], "minutes": v[1]} for k, v in S.TYPES.items() if k != "emergency"}
    return {"practice": comms.PRACTICE, "types": types}


@router.get("/api/public/availability")
def public_availability(date: str, type: str = "checkup"):
    if type not in S.TYPES or type == "emergency":
        raise HTTPException(422, "unknown type")
    try:
        d = dt.date.fromisoformat(date)
    except ValueError:
        raise HTTPException(422, "bad date")
    if d > S.now().date() + dt.timedelta(days=120):
        return {"slots": []}
    return {"slots": S.free_slots(db.connect(), d, type)}


_HITS = defaultdict(list)


def _rate_limited(ip, limit=8, window=3600):
    now = time.time()
    _HITS[ip] = [t for t in _HITS[ip] if now - t < window]
    if len(_HITS[ip]) >= limit:
        return True
    _HITS[ip].append(now)
    return False


class PublicBook(BaseModel):
    name: str
    phone: str
    email: Optional[str] = None
    type: str
    start: str
    src: str = "web"
    website: str = ""          # honeypot: real people never fill this in


@router.post("/api/public/book", status_code=201)
def public_book(b: PublicBook, request: Request):
    if b.website:                                   # a bot filled the hidden field
        return {"ok": True}
    ip = request.client.host if request.client else "?"
    if _rate_limited(ip):
        raise HTTPException(429, "too many requests, please call the office")
    name = b.name.strip()
    if not 2 <= len(name) <= 80:
        raise HTTPException(422, "please enter your full name")
    email = (b.email or "").strip()
    if not email:
        raise HTTPException(422, "please enter your email so we can send your confirmation")
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise HTTPException(422, "invalid email")
    if b.type not in S.TYPES or b.type == "emergency":
        raise HTTPException(422, "for emergencies please call the office")
    src = b.src if b.src in SOURCES else "web"
    c = db.connect()
    pat = S.patient_by_phone(c, b.phone)
    if pat and len(S.upcoming(c, pat["id"])) >= 3:
        raise HTTPException(409, "you already have several upcoming visits, please call the office")
    try:
        pid = pat["id"] if pat else S.create_patient(c, name, b.phone, email)
        if pat and not pat.get("email"):           # existing patient with no email on file
            c.execute("UPDATE patients SET email=? WHERE id=?", (email, pid))
            c.commit()
        aid = S.book(c, pid, b.type, b.start, f"web_{src}")
    except S.Conflict as e:
        raise HTTPException(409, str(e))
    except ValueError as e:
        raise HTTPException(422, str(e))
    comms.confirmation(c, aid)
    a = S.get_appt(c, aid)
    comms.send(c, pid, f"New online booking ({src}): {S.type_name(b.type)} on {comms.when(a['start'])}",
               "social_event", aid, channel="note")
    return {"ok": True, "when": comms.when(a["start"]), "type": S.type_name(b.type),
            "provider": a["provider_name"]}
