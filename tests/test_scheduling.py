import datetime as dt
import pytest
from app import scheduling as S, db


def test_seeded_and_open_hours(client):
    c = db.connect()
    assert c.execute("SELECT COUNT(*) FROM patients").fetchone()[0] == 10
    sunday = dt.date(2026, 10, 18)
    assert S.free_slots(c, sunday, "checkup") == []
    sat = S.free_slots(c, dt.date(2026, 10, 17), "rootcanal")
    assert all(s <= "11:30" for s in sat)          # 90 min must finish by 1 PM


def test_no_past_slots_today(client):
    c = db.connect()
    assert all(s > "10:00" for s in S.free_slots(c, dt.date(2026, 10, 15), "checkup"))


def test_double_booking_blocked_across_two_providers(client):
    c = db.connect()
    start = "2026-11-03T09:00"                       # far future, empty
    ids = [r["id"] for r in c.execute("SELECT id FROM patients LIMIT 3")]
    S.book(c, ids[0], "cleaning", start)
    S.book(c, ids[1], "cleaning", start)             # second provider
    with pytest.raises(S.Conflict):
        S.book(c, ids[2], "cleaning", start)         # nobody left
    assert "09:00" not in S.free_slots(c, dt.date(2026, 11, 3), "cleaning")


def test_rejects_outside_hours_and_past(client):
    c = db.connect()
    with pytest.raises(ValueError):
        S.book(c, 1, "checkup", "2026-11-03T08:00")
    with pytest.raises(ValueError):
        S.book(c, 1, "checkup", "2026-10-15T09:00")


def test_api_book_move_complete(client):
    pid = client.get("/api/patients").json()[0]["id"]
    r = client.post("/api/appointments", json={"patient_id": pid, "type": "checkup", "start": "2026-11-04T10:00"})
    assert r.status_code == 201
    aid = r.json()["id"]
    assert client.patch(f"/api/appointments/{aid}", json={"start": "2026-11-04T11:00"}).status_code == 200
    client.patch(f"/api/appointments/{aid}", json={"status": "completed"})
    kinds = [m["kind"] for m in client.get("/api/messages").json()]
    assert "confirmation" in kinds and "review_invite" in kinds
    # completing twice must not send a second review invite
    client.patch(f"/api/appointments/{aid}", json={"status": "completed"})
    assert [m["kind"] for m in client.get("/api/messages").json()].count("review_invite") == 1


def test_api_conflict_returns_409(client):
    c = db.connect()
    ids = [r["id"] for r in c.execute("SELECT id FROM patients LIMIT 3")]
    for i in ids[:2]:
        assert client.post("/api/appointments", json={"patient_id": i, "type": "checkup", "start": "2026-11-05T09:00"}).status_code == 201
    assert client.post("/api/appointments", json={"patient_id": ids[2], "type": "checkup", "start": "2026-11-05T09:00"}).status_code == 409


def test_reminders_not_duplicated(client):
    pid = client.get("/api/patients").json()[0]["id"]
    client.post("/api/appointments", json={"patient_id": pid, "type": "checkup", "start": "2026-10-16T09:00"})
    first = client.post("/api/reminders/run").json()["sent"]
    assert first >= 1
    assert client.post("/api/reminders/run").json()["sent"] == 0


def test_recall_targets_lapsed_patients_once(client):
    n = client.post("/api/recall/run").json()["sent"]
    assert n == 3                                     # the 3 seeded lapsed patients
    assert client.post("/api/recall/run").json()["sent"] == 0


def test_reports_and_pages(client):
    r = client.get("/api/reports").json()
    assert {"upcoming_7d", "no_show_rate", "by_type"} <= set(r)
    assert client.get("/").status_code == 200
    assert client.get("/static/vendor/fullcalendar.min.js").status_code == 200
    assert client.get("/api/health").json()["ok"] is True


def test_duplicate_patient_phone(client):
    body = {"name": "Test Person", "phone": "(555) 200-3000"}
    assert client.post("/api/patients", json=body).status_code == 201
    assert client.post("/api/patients", json=body).status_code == 409
