import base64
import hashlib
import hmac
import json

import pytest

from app import social

SECRET = "test-app-secret"


@pytest.fixture()
def sent(monkeypatch):
    """Capture outbound Graph API calls instead of hitting the network."""
    out = []
    monkeypatch.setattr(social, "_graph_post", lambda payload: out.append(payload) or {})
    monkeypatch.setenv("META_PAGE_TOKEN", "tok")
    return out


@pytest.fixture()
def meta(client, monkeypatch):
    monkeypatch.setenv("META_APP_SECRET", SECRET)
    monkeypatch.setenv("META_VERIFY_TOKEN", "verify-me")

    def post(text, sender="IG123", obj="instagram", mid=None, secret=SECRET, extra=None):
        body = {"object": obj, "entry": [{"id": "PAGE", "messaging": [
            {"sender": {"id": sender}, "recipient": {"id": "PAGE"},
             "message": {"mid": mid or f"m-{hash((text, sender)) & 0xffff}", "text": text, **(extra or {})}}]}]}
        raw = json.dumps(body).encode()
        sig = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        return client.post("/webhooks/meta", content=raw,
                           headers={"x-hub-signature-256": sig, "content-type": "application/json"})
    return post


def first_slot(client, date="2026-10-16", typ="cleaning"):
    s = client.get("/api/availability", params={"date": date, "type": typ}).json()["slots"][0]
    return f"{int(s[:2]) % 12 or 12}:{s[3:]} {'pm' if s >= '12:00' else 'am'}"


def test_webhook_verification_handshake(client, meta):
    ok = client.get("/webhooks/meta", params={"hub.mode": "subscribe", "hub.verify_token": "verify-me", "hub.challenge": "777"})
    assert ok.status_code == 200 and ok.text == "777"
    bad = client.get("/webhooks/meta", params={"hub.mode": "subscribe", "hub.verify_token": "nope", "hub.challenge": "777"})
    assert bad.status_code == 403


def test_bad_signature_rejected_and_unsigned_blocked_by_default(client, meta, monkeypatch):
    assert meta("hi", secret="wrong-secret").status_code == 403
    monkeypatch.delenv("META_APP_SECRET")
    assert client.post("/webhooks/meta", json={"object": "instagram", "entry": []}).status_code == 403


def test_instagram_dm_books_into_real_calendar(client, meta, sent):
    assert meta("Hi, I'd like to book a cleaning", mid="a1").status_code == 200
    meta("tomorrow", mid="a2")
    meta(first_slot(client), mid="a3")
    meta("my number is 555 888 2000", mid="a4")
    meta("my name is Sara Instagram", mid="a5")
    meta("sara@example.com", mid="a6")

    appts = client.get("/api/appointments").json()
    mine = [a for a in appts if a["patient_name"] == "Sara Instagram"]
    assert len(mine) == 1 and mine[0]["source"] == "ai_instagram"
    # replies were pushed back to the sender through the Graph API
    assert sent and all(p["recipient"]["id"] == "IG123" for p in sent)
    assert any("all set, Sara" in p["message"]["text"] for p in sent)

    threads = client.get("/api/inbox").json()
    assert threads[0]["channel"] == "instagram" and threads[0]["patient_name"] == "Sara Instagram"
    detail = client.get(f"/api/inbox/{threads[0]['id']}").json()
    assert [m["author"] for m in detail["messages"]][:2] == ["customer", "bot"]
    # staff get a note in the outbox
    assert any(m["kind"] == "social_event" and "Instagram" in m["body"] for m in client.get("/api/messages").json())
    # shows up in reports
    assert client.get("/api/reports").json()["social_booked_30d"] >= 1


def test_duplicate_delivery_is_ignored(client, meta, sent):
    meta("hello", mid="dup")
    meta("hello", mid="dup")
    t = client.get("/api/inbox").json()[0]
    msgs = client.get(f"/api/inbox/{t['id']}").json()["messages"]
    assert [m["author"] for m in msgs].count("customer") == 1


def test_echo_and_non_text_events_ignored(client, meta, sent):
    meta("echo of our own message", extra={"is_echo": True})
    assert client.get("/api/inbox").json() == []


def test_staff_takeover_silences_bot_and_replies_via_graph(client, meta, sent):
    meta("hello there", sender="FB9", obj="page", mid="h1")
    tid = client.get("/api/inbox").json()[0]["id"]
    n = len(sent)
    r = client.post(f"/api/inbox/{tid}/reply", json={"text": "Hi, this is the front desk"}).json()
    assert r["delivered"] is True and sent[-1]["message"]["text"] == "Hi, this is the front desk"
    meta("thanks! can I book?", sender="FB9", obj="page", mid="h2")
    assert len(sent) == n + 1                       # bot stayed quiet after the takeover
    client.post(f"/api/inbox/{tid}/handoff", json={"on": False})
    meta("book a checkup", sender="FB9", obj="page", mid="h3")
    assert len(sent) == n + 2


def test_unlinked_chat_cannot_cancel_someone_elses_appointment(client, meta, sent):
    meta("I need to cancel my appointment", sender="STRANGER", mid="s1")
    meta("555 010 0103", sender="STRANGER", mid="s2")        # a seeded patient's number
    texts = " ".join(p["message"]["text"] for p in sent)
    assert "for your security" in texts.lower()
    assert all(a["status"] != "cancelled" for a in client.get("/api/appointments").json())


def test_returning_customer_is_recognised_after_first_booking(client, meta, sent):
    for i, t in enumerate(["book a checkup", "tomorrow", first_slot(client, typ="checkup"),
                           "555 321 0001", "my name is Ravi Kumar"]):
        meta(t, sender="RET1", mid=f"r{i}")
    meta("no that's all", sender="RET1", mid="r-end")
    sent.clear()
    meta("I need to cancel my appointment", sender="RET1", mid="r-c1")     # linked thread: allowed
    assert "found your" in sent[-1]["message"]["text"]


def test_human_request_flags_thread_for_staff(client, meta, sent):
    meta("can I talk to a real person", sender="H1", mid="x1")
    t = client.get("/api/inbox").json()[0]
    assert t["handoff"] == 1
    assert "flagged this for our team" in sent[-1]["message"]["text"]


def test_generic_inbound_for_linkedin_and_automation_tools(client, monkeypatch):
    assert client.post("/webhooks/inbound", json={"sender_id": "u1", "text": "hi"}).status_code == 503
    monkeypatch.setenv("SOCIAL_INBOUND_SECRET", "s3cret")
    body = {"channel": "linkedin", "sender_id": "li-42", "text": "Hi, do you take Delta Dental?", "sender_name": "Pat"}
    assert client.post("/webhooks/inbound", json=body).status_code == 403
    r = client.post("/webhooks/inbound", json=body, headers={"x-inbound-secret": "s3cret"})
    assert r.status_code == 200 and "Delta Dental" in r.json()["reply"]
    assert client.get("/api/inbox").json()[0]["channel"] == "linkedin"


# ------------------------------------------------------------------ public booking form
def test_public_form_books_and_tags_the_source(client):
    cfg = client.get("/api/public/config").json()
    assert "emergency" not in cfg["types"]
    slot = client.get("/api/public/availability", params={"date": "2026-10-16", "type": "checkup"}).json()["slots"][0]
    r = client.post("/api/public/book", json={"name": "Lina Web", "phone": "(555) 999-1111", "type": "checkup",
                                              "email": "lina@example.com", "start": f"2026-10-16T{slot}", "src": "linkedin"})
    assert r.status_code == 201 and r.json()["ok"]
    a = [x for x in client.get("/api/appointments").json() if x["patient_name"] == "Lina Web"][0]
    assert a["source"] == "web_linkedin"
    # same slot again -> conflict only if no provider is free; a different patient can still get the 2nd doctor
    r2 = client.post("/api/public/book", json={"name": "Max Web", "phone": "5559991112", "email": "max@example.com", "type": "checkup",
                                               "start": f"2026-10-16T{slot}"})
    assert r2.status_code == 201
    r3 = client.post("/api/public/book", json={"name": "Zed Web", "phone": "5559991113", "email": "zed@example.com", "type": "checkup",
                                               "start": f"2026-10-16T{slot}"})
    assert r3.status_code == 409


def test_public_form_validation_and_abuse_protection(client):
    base = {"name": "Eve", "phone": "5551110000", "type": "checkup", "start": "2026-10-16T09:00"}
    assert client.post("/api/public/book", json={**base, "type": "emergency"}).status_code == 422
    assert client.post("/api/public/book", json={**base, "phone": "12"}).status_code == 422
    assert client.post("/api/public/book", json={**base, "start": "2026-10-11T09:00"}).status_code == 422   # Sunday
    assert client.post("/api/public/book", json={**base, "email": "not-an-email"}).status_code == 422
    n = len(client.get("/api/appointments").json())
    r = client.post("/api/public/book", json={**base, "website": "http://spam"})      # honeypot
    assert r.status_code == 201 and len(client.get("/api/appointments").json()) == n


def test_book_page_is_served(client):
    r = client.get("/book?src=instagram")
    assert r.status_code == 200 and "Book" in r.text


# ------------------------------------------------------------------ clinical
def test_tooth_chart_and_treatment_to_invoice(client):
    pid = client.get("/api/patients").json()[0]["id"]
    assert client.put(f"/api/patients/{pid}/teeth/14", json={"condition": "cavity", "note": "mesial"}).status_code == 200
    assert client.put(f"/api/patients/{pid}/teeth/14", json={"condition": "filled"}).status_code == 200   # upsert
    assert client.put(f"/api/patients/{pid}/teeth/33", json={"condition": "cavity"}).status_code == 422
    assert client.put(f"/api/patients/{pid}/teeth/1", json={"condition": "bogus"}).status_code == 422
    chart = client.get(f"/api/patients/{pid}/chart").json()
    assert chart["teeth"] == [{"tooth": 14, "condition": "filled", "note": None, "updated_at": chart["teeth"][0]["updated_at"]}]

    tid = client.post(f"/api/patients/{pid}/treatments", json={"procedure": "Crown", "tooth": 14, "fee": 1100}).json()["id"]
    before = len(client.get("/api/invoices").json())
    r = client.patch(f"/api/treatments/{tid}", json={"status": "done"}).json()
    assert r["invoice_id"]
    client.patch(f"/api/treatments/{tid}", json={"status": "done"})                 # idempotent
    inv = client.get("/api/invoices").json()
    assert len(inv) == before + 1 and "tooth #14" in inv[0]["description"] and inv[0]["amount"] == 1100


# ------------------------------------------------------------------ staff login
def test_admin_password_protects_staff_api_but_not_public_routes(client, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "pw123")
    assert client.get("/api/patients").status_code == 401
    assert client.get("/api/public/config").status_code == 200
    assert client.get("/book").status_code == 200
    good = {"authorization": "Basic " + base64.b64encode(b"admin:pw123").decode()}
    bad = {"authorization": "Basic " + base64.b64encode(b"admin:wrong").decode()}
    assert client.get("/api/patients", headers=good).status_code == 200
    assert client.get("/api/patients", headers=bad).status_code == 401
    assert client.get("/api/inbox").status_code == 401


def test_public_form_is_rate_limited(client):
    base = {"name": "Eve", "phone": "5551110000", "type": "checkup", "start": "2026-10-11T09:00"}   # Sunday -> 422
    codes = [client.post("/api/public/book", json=base).status_code for _ in range(10)]
    assert codes[:8] == [422] * 8 and codes[8:] == [429, 429]


def test_public_form_requires_an_email(client):
    slot = client.get("/api/public/availability", params={"date": "2026-10-16", "type": "checkup"}).json()["slots"][0]
    base = {"name": "No Mail", "phone": "5559990000", "type": "checkup", "start": f"2026-10-16T{slot}"}
    assert client.post("/api/public/book", json=base).status_code == 422
    assert client.post("/api/public/book", json={**base, "email": "nope"}).status_code == 422
    assert client.post("/api/public/book", json={**base, "email": "ok@example.com"}).status_code == 201
