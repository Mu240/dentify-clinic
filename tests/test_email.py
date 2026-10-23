import pytest

from app import comms, db
from tests.test_social import first_slot  # noqa: F401  (same helper style)


@pytest.fixture()
def mail(monkeypatch):
    out = []
    monkeypatch.setenv("GMAIL_USER", "clinic@gmail.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "abcd efgh ijkl mnop")
    monkeypatch.setattr(comms, "_smtp_send", lambda m: out.append(m))
    monkeypatch.setattr(comms, "_spawn", lambda fn: fn())   # run synchronously in tests
    return out


def _book(client, email, phone="5551230001"):
    slot = client.get("/api/availability", params={"date": "2026-10-16", "type": "cleaning"}).json()["slots"][0]
    return client.post("/api/public/book", json={
        "name": "Sara Khan", "phone": phone, "email": email, "type": "cleaning",
        "start": f"2026-10-16T{slot}", "src": "web"})


def test_confirmation_email_sent(client, mail):
    r = _book(client, "sara@example.com")
    assert r.status_code == 201
    assert len(mail) == 1
    m = mail[0]
    assert m["To"] == "sara@example.com" and "confirmed" in m["Subject"].lower()
    assert "Cleaning" in m.get_body(("plain",)).get_content()
    assert any(p.get_content_type() == "text/calendar" for p in m.iter_attachments())


def test_no_email_address_means_no_email(client, mail):
    pid = client.post("/api/patients", json={"name": "Walk In", "phone": "5551110000"}).json()["id"]
    r = client.post("/api/appointments", json={"patient_id": pid, "type": "checkup", "start": "2026-10-16T09:00"})
    assert r.status_code == 201
    assert mail == []


def test_not_configured_is_outbox_only(client, monkeypatch):
    monkeypatch.delenv("GMAIL_USER", raising=False)
    monkeypatch.delenv("GMAIL_APP_PASSWORD", raising=False)
    assert _book(client, "sara@example.com").status_code == 201
    msgs = client.get("/api/messages").json()
    assert any(m["channel"] == "email" and "not sent" in m["body"] for m in msgs)


def test_gmail_failure_never_breaks_booking(client, mail, monkeypatch):
    def boom(m):
        raise OSError("smtp down")
    monkeypatch.setattr(comms, "_smtp_send", boom)
    assert _book(client, "sara@example.com").status_code == 201
    assert any(m["kind"] == "email_failed" for m in client.get("/api/messages").json())
