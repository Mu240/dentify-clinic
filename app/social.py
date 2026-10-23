"""Social-media booking: Instagram DMs, Facebook Messenger (Meta Graph API) and any other channel
(LinkedIn, WhatsApp bridges, Zapier/Make/ManyChat...) through one generic inbound endpoint.

Every inbound message goes through the SAME receptionist brain and calendar as the phone/chat
widget, so a booking made in an Instagram DM lands on the Calendar like any other.

Environment variables
  META_APP_SECRET        Meta app secret - used to verify webhook signatures (REQUIRED in production)
  META_VERIFY_TOKEN      any string you choose; paste the same value into the Meta webhook settings
  META_PAGE_TOKEN        Page access token (Facebook Page, or the Page linked to the Instagram account)
  META_API_VERSION       Graph API version, default v21.0
  SOCIAL_INBOUND_SECRET  shared secret for the generic /webhooks/inbound endpoint
  SOCIAL_ALLOW_UNSIGNED  set to 1 ONLY for local testing, skips signature checks
  PUBLIC_URL             public https base URL of this app (used to print your booking links)
"""
import hashlib
import hmac
import json
import os
import urllib.request

from . import agent, comms
from . import scheduling as S

CHANNELS = ("instagram", "facebook", "linkedin", "other")
LABELS = {"instagram": "Instagram", "facebook": "Facebook", "linkedin": "LinkedIn", "other": "Other"}


# ------------------------------------------------------------------ config / security
def env(name, default=""):
    return os.environ.get(name, default)


def signature_ok(raw: bytes, header) -> bool:
    """Meta signs every webhook POST: X-Hub-Signature-256 = sha256 HMAC of the raw body."""
    secret = env("META_APP_SECRET")
    if not secret:
        return env("SOCIAL_ALLOW_UNSIGNED") == "1"
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header[7:])


def inbound_secret_ok(provided) -> bool:
    secret = env("SOCIAL_INBOUND_SECRET")
    return bool(secret) and hmac.compare_digest(secret, provided or "")


def status():
    base = env("PUBLIC_URL").rstrip("/")
    links = {c: f"{base}/book?src={c}" for c in ("instagram", "facebook", "linkedin")}
    return {
        "public_url": base or None,
        "meta_signature_check": bool(env("META_APP_SECRET")),
        "meta_verify_token": bool(env("META_VERIFY_TOKEN")),
        "meta_can_reply": bool(env("META_PAGE_TOKEN")),
        "inbound_endpoint": bool(env("SOCIAL_INBOUND_SECRET")),
        "booking_links": links,
    }


# ------------------------------------------------------------------ Meta payload / sending
def parse_meta(payload):
    """Meta webhook JSON -> list of plain text messages (ignores echoes, reads, attachments)."""
    channel = {"instagram": "instagram", "page": "facebook"}.get((payload or {}).get("object"))
    if not channel:
        return []
    out = []
    for entry in payload.get("entry") or []:
        for ev in entry.get("messaging") or []:
            msg = ev.get("message") or {}
            sender = (ev.get("sender") or {}).get("id")
            if not sender or msg.get("is_echo") or not msg.get("text"):
                continue
            out.append({"channel": channel, "sender_id": str(sender), "text": msg["text"],
                        "external_id": msg.get("mid")})
    return out


def _graph_post(payload):
    """POST to the Graph API Send endpoint. Isolated so tests can replace it."""
    url = f"https://graph.facebook.com/{env('META_API_VERSION', 'v21.0')}/me/messages"
    req = urllib.request.Request(
        url, json.dumps(payload).encode(),
        {"Content-Type": "application/json", "Authorization": f"Bearer {env('META_PAGE_TOKEN')}"},
        method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read() or b"{}")


def deliver(channel, sender_id, text):
    """Try to send a reply on the platform. Returns (delivered: bool, error: str|None)."""
    if channel not in ("instagram", "facebook"):
        return False, f"{LABELS.get(channel, channel)} has no reply API here - the reply is returned to your automation tool or copy it manually"
    if not env("META_PAGE_TOKEN"):
        return False, "META_PAGE_TOKEN not set (demo mode: reply saved in the inbox only)"
    try:
        _graph_post({"recipient": {"id": sender_id}, "messaging_type": "RESPONSE",
                     "message": {"text": text[:1000]}})
        return True, None
    except Exception as e:  # network / expired token / outside the 24h messaging window
        return False, f"send failed: {e}"[:300]


# ------------------------------------------------------------------ threads
def _now():
    return S.iso(S.now())


def get_thread(conn, channel, sender_id, name=None):
    r = conn.execute("SELECT * FROM social_threads WHERE channel=? AND sender_id=?",
                     (channel, sender_id)).fetchone()
    if r:
        if name and not r["sender_name"]:
            conn.execute("UPDATE social_threads SET sender_name=? WHERE id=?", (name, r["id"]))
            conn.commit()
        return dict(r)
    conn.execute("INSERT INTO social_threads(channel,sender_id,sender_name,last_at,created_at) VALUES(?,?,?,?,?)",
                 (channel, sender_id, name, _now(), _now()))
    conn.commit()
    return get_thread(conn, channel, sender_id)


def log_message(conn, thread_id, direction, author, body, external_id=None, delivered=1, error=None):
    """Returns the new row id, or None if external_id was already seen (webhook retry)."""
    try:
        cur = conn.execute(
            "INSERT INTO social_messages(thread_id,direction,author,body,external_id,delivered,error,created_at)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (thread_id, direction, author, body, external_id, int(delivered), error, _now()))
    except Exception:
        return None
    conn.commit()
    return cur.lastrowid


def _load_session(conn, th):
    if th["session_json"]:
        sess = json.loads(th["session_json"])
        sess["ex"] = {}
        return sess
    sess = agent.new_session(th["channel"])
    agent.SESSIONS.pop(sess["id"], None)          # state lives in the DB, not in process memory
    if th["patient_id"]:
        pat = conn.execute("SELECT * FROM patients WHERE id=?", (th["patient_id"],)).fetchone()
        if pat:
            sess["slots"].update(phone=pat["phone"], name=pat["name"])
            sess["patient"] = dict(pat)
    return sess


def _save_session(conn, th_id, sess, patient_id=None, handoff=None):
    keep = {k: v for k, v in sess.items() if k != "ex"}
    sets, args = ["session_json=?", "last_at=?"], [None if sess["done"] else json.dumps(keep), _now()]
    if patient_id:
        sets.append("patient_id=?")
        args.append(patient_id)
    if handoff is not None:
        sets.append("handoff=?")
        args.append(int(handoff))
    conn.execute(f"UPDATE social_threads SET {','.join(sets)} WHERE id=?", (*args, th_id))
    conn.commit()


def process_inbound(conn, channel, sender_id, text, name=None, external_id=None, send=True):
    """Handle one customer message end to end. Returns the bot's reply text, or None when the
    message was a duplicate or a human has taken over the thread."""
    channel = channel if channel in CHANNELS else "other"
    text = (text or "").strip()[:1000]
    if not text:
        return None
    th = get_thread(conn, channel, sender_id, name)
    if log_message(conn, th["id"], "in", "customer", text, external_id) is None:
        return None                                        # duplicate delivery from Meta
    conn.execute("UPDATE social_threads SET unread=unread+1, last_at=? WHERE id=?", (_now(), th["id"]))
    conn.commit()
    if th["handoff"]:
        return None                                        # staff are handling this chat

    sess = _load_session(conn, th)
    sess["require_verified"], sess["verified"] = True, bool(th["patient_id"])
    seen = len(sess["events"])
    reply = agent.handle(conn, sess, text)

    handoff = None
    if sess["outcome"] == "transferred":
        handoff = True
        reply = "I've flagged this for our team and someone will reply here shortly."
        sess["history"][-1][1] = reply

    # Link the thread to a patient only if this chat created them (a stranger typing someone
    # else's phone number must not gain control over that person's appointments).
    patient_id = None
    phone = sess["slots"].get("phone")
    if phone and not th["patient_id"]:
        pat = S.patient_by_phone(conn, phone)
        if pat and pat["created_at"] >= sess["started"]:
            patient_id = pat["id"]
    pid = patient_id or th["patient_id"] or (sess.get("patient") or {}).get("id")
    for ev in sess["events"][seen:]:                       # staff notification per booking/cancel/etc
        comms.send(conn, pid, f"{LABELS[channel]} DM: {ev}", "social_event", channel="note")
    _save_session(conn, th["id"], sess, patient_id, handoff)

    ok, err = (False, None)
    if send:
        ok, err = deliver(channel, sender_id, reply)
    log_message(conn, th["id"], "out", "bot", reply, delivered=ok, error=err)
    return reply


def staff_reply(conn, thread_id, text):
    th = conn.execute("SELECT * FROM social_threads WHERE id=?", (thread_id,)).fetchone()
    if not th:
        return None
    ok, err = deliver(th["channel"], th["sender_id"], text)
    log_message(conn, thread_id, "out", "staff", text, delivered=ok, error=err)
    conn.execute("UPDATE social_threads SET handoff=1, unread=0, last_at=? WHERE id=?", (_now(), thread_id))
    conn.commit()
    return {"delivered": ok, "error": err}
