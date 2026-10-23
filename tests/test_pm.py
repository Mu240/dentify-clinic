import sqlite3

import pytest

from app import comms, db

PLAN = {"carrier": "Delta Dental PPO", "annual_max": 1500, "used": 0, "deductible": 50, "deductible_met": 0,
        "preventive_pct": 100, "basic_pct": 80, "major_pct": 50}


def new_patient(client, name="Test Patient", phone="5558880001", email="t@example.com", plan=None):
    pid = client.post("/api/patients", json={"name": name, "phone": phone, "email": email}).json()["id"]
    if plan:
        assert client.put(f"/api/patients/{pid}/insurance", json=plan).status_code == 200
    return pid


def add_tx(client, pid, code, tooth=None, **kw):
    r = client.post(f"/api/patients/{pid}/treatments", json={"code": code, "tooth": tooth, **kw})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_fee_schedule_fills_name_and_fee(client):
    assert any(f["code"] == "D2740" for f in client.get("/api/fees").json())
    pid = new_patient(client)
    add_tx(client, pid, "D2740", 14)
    t = client.get(f"/api/patients/{pid}/chart").json()["treatments"][0]
    assert t["procedure"] == "Porcelain crown" and t["fee"] == 1150 and t["code"] == "D2740"
    assert client.post(f"/api/patients/{pid}/treatments", json={"code": "D9999"}).status_code == 422
    assert client.post(f"/api/patients/{pid}/treatments", json={}).status_code == 422


def test_estimate_applies_class_percent_and_deductible(client):
    pid = new_patient(client, plan=PLAN)
    add_tx(client, pid, "D1110")            # preventive 100%, deductible does not apply
    add_tx(client, pid, "D2740", 14)        # major 50% after the $50 deductible
    e = client.get(f"/api/patients/{pid}/estimate").json()
    clean, crown = e["items"]
    assert clean["insurance"] == 120 and clean["patient"] == 0
    assert crown["insurance"] == 550 and crown["patient"] == 600 and crown["deductible_applied"] == 50
    assert e["totals"] == {"fee": 1270, "insurance": 670, "patient": 600}


def test_annual_maximum_caps_insurance(client):
    pid = new_patient(client, plan={**PLAN, "annual_max": 1000, "used": 900, "deductible": 0})
    add_tx(client, pid, "D2740", 14)
    item = client.get(f"/api/patients/{pid}/estimate").json()["items"][0]
    assert item["insurance"] == 100 and item["patient"] == 1050


def test_cosmetic_and_uncoded_work_is_never_estimated(client):
    pid = new_patient(client, plan=PLAN)
    add_tx(client, pid, "D9972")
    client.post(f"/api/patients/{pid}/treatments", json={"procedure": "Custom thing", "fee": 100})
    assert [i["insurance"] for i in client.get(f"/api/patients/{pid}/estimate").json()["items"]] == [0, 0]


def test_done_with_insurance_makes_claim_and_patient_invoice(client):
    pid = new_patient(client, plan=PLAN)
    tid = add_tx(client, pid, "D2740", 14)
    r = client.patch(f"/api/treatments/{tid}", json={"status": "done"}).json()
    assert r["claim_id"] and r["invoice_id"]
    again = client.patch(f"/api/treatments/{tid}", json={"status": "done"}).json()    # idempotent
    assert again["claim_id"] == r["claim_id"] and again["invoice_id"] == r["invoice_id"]
    claim = [c for c in client.get("/api/claims").json() if c["id"] == r["claim_id"]][0]
    assert claim["expected"] == 550 and claim["billed"] == 1150 and claim["status"] == "draft"
    inv = [i for i in client.get("/api/invoices").json() if i["id"] == r["invoice_id"]][0]
    assert inv["amount"] == 600 and "patient portion" in inv["description"]


def test_done_without_insurance_is_a_plain_invoice(client):
    pid = new_patient(client)
    tid = add_tx(client, pid, "D2740", 14)
    r = client.patch(f"/api/treatments/{tid}", json={"status": "done"}).json()
    assert r["claim_id"] is None
    assert [i for i in client.get("/api/invoices").json() if i["id"] == r["invoice_id"]][0]["amount"] == 1150


def test_claim_lifecycle_paid_short_and_denied(client):
    pid = new_patient(client, plan=PLAN)
    r = client.patch(f"/api/treatments/{add_tx(client, pid, 'D2740', 14)}", json={"status": "done"}).json()
    cid = r["claim_id"]
    assert client.post(f"/api/claims/{cid}/pay").status_code == 409            # must be submitted first
    assert client.post(f"/api/claims/{cid}/submit").status_code == 200
    assert client.post(f"/api/claims/{cid}/submit").status_code == 409
    assert client.post(f"/api/claims/{cid}/pay", json={"amount": 500}).status_code == 200   # $50 short
    prof = client.get(f"/api/patients/{pid}/profile").json()
    assert prof["insurance"]["used"] == 500
    assert prof["balance"] == 600 + 50                                           # patient share + shortfall
    # denial moves the whole insurance share to the patient
    r2 = client.patch(f"/api/treatments/{add_tx(client, pid, 'D2392', 19)}", json={"status": "done"}).json()
    client.post(f"/api/claims/{r2['claim_id']}/submit")
    assert client.post(f"/api/claims/{r2['claim_id']}/deny", json={"reason": "Not covered"}).status_code == 200
    assert any("denied" in i["description"].lower() for i in client.get("/api/invoices").json())
    assert client.post(f"/api/claims/{r2['claim_id']}/pay").status_code == 409


def test_medical_alerts(client):
    pid = new_patient(client)
    client.put(f"/api/patients/{pid}/medical", json={"allergies": "Penicillin, Latex", "conditions": "Hypertension",
                                                     "medications": "Warfarin 5mg", "premed": True})
    a = client.get(f"/api/patients/{pid}/profile").json()["alerts"]
    assert "Allergy: Penicillin" in a and "Allergy: Latex" in a and "On blood thinner" in a
    assert "Antibiotic premed required" in a and "Hypertension" in a


def test_insurance_validation_and_removal(client):
    pid = new_patient(client)
    assert client.put(f"/api/patients/{pid}/insurance", json={**PLAN, "basic_pct": 120}).status_code == 422
    assert client.put(f"/api/patients/{pid}/insurance", json={**PLAN, "carrier": " "}).status_code == 422
    assert client.put(f"/api/patients/{pid}/insurance", json=PLAN).status_code == 200
    assert client.delete(f"/api/patients/{pid}/insurance").status_code == 200
    assert client.get(f"/api/patients/{pid}/profile").json()["insurance"] is None


def test_signed_notes_are_locked(client):
    pid = new_patient(client)
    assert client.post(f"/api/patients/{pid}/notes", json={}).status_code == 422
    nid = client.post(f"/api/patients/{pid}/notes", json={"subjective": "Pain", "plan": "Filling"}).json()["id"]
    assert client.put(f"/api/notes/{nid}", json={"subjective": "Pain on cold"}).status_code == 200
    assert client.post(f"/api/notes/{nid}/sign").status_code == 200
    assert client.put(f"/api/notes/{nid}", json={"subjective": "Edited later"}).status_code == 409
    n = client.get(f"/api/patients/{pid}/profile").json()["notes"][0]
    assert n["signed_at"] and n["subjective"] == "Pain on cold" and n["provider_name"]


def test_treatment_plan_link_accept_and_email(client, monkeypatch):
    sent = []
    monkeypatch.setenv("GMAIL_USER", "clinic@gmail.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "x")
    monkeypatch.setattr(comms, "_smtp_send", lambda m: sent.append(m))
    monkeypatch.setattr(comms, "_spawn", lambda fn: fn())
    pid = new_patient(client, plan=PLAN)
    add_tx(client, pid, "D2740", 14)
    path = client.get(f"/api/patients/{pid}/plan-link").json()["path"]
    page = client.get(path)
    assert page.status_code == 200 and "Porcelain crown" in page.text and "$600.00" in page.text
    assert client.get(path[:-3] + "bad").status_code == 404
    assert client.post(path.replace("/plan/", "/api/public/plan/") + "/accept").json() == {"accepted": 1}
    assert client.get(f"/api/patients/{pid}/chart").json()["treatments"][0]["status"] == "accepted"
    out = client.post(f"/api/patients/{pid}/plan/send").json()
    assert out["emailed"] is True and out["local_link"] is True                  # testserver is not a public address
    body = sent[0].get_body(("plain",)).get_content()
    assert sent[0]["To"] == "t@example.com" and "http://testserver" + path in body  # absolute link in the email
    empty = new_patient(client, "No Plan", "5558880002")
    assert client.post(f"/api/patients/{empty}/plan/send").status_code == 409


def test_huddle_and_kpis_on_seed_data(client):
    h = client.get("/api/huddle").json()
    assert h["appointments"] and h["production"] > 0 and "unscheduled_total" in h
    assert {"alerts", "balance", "carrier", "tx_accepted"} <= set(h["appointments"][0])
    k = client.get("/api/kpis").json()
    assert k["production_30d"] > 0 and 0 <= k["case_acceptance"] <= 100
    assert set(k["patient_ar"]) == {"0-30", "31-60", "61-90", "90+"} and k["patient_ar"]["90+"] > 0
    assert k["claims_open"] >= 1 and k["by_provider"]
    assert any(c["status"] == "paid" for c in client.get("/api/claims").json())


def test_statement_page(client):
    pid = new_patient(client)
    client.patch(f"/api/treatments/{add_tx(client, pid, 'D1110')}", json={"status": "done"})
    r = client.get(f"/statement/{pid}")
    assert r.status_code == 200 and "Balance due" in r.text and "$120.00" in r.text


def test_plan_link_is_public_but_staff_api_is_not(client, monkeypatch):
    pid = new_patient(client)
    add_tx(client, pid, "D1110")
    path = client.get(f"/api/patients/{pid}/plan-link").json()["path"]
    monkeypatch.setenv("ADMIN_PASSWORD", "secret")
    assert client.get(path).status_code == 200                  # patient link works without staff login
    assert client.get("/api/claims").status_code == 401
    assert client.get(f"/statement/{pid}").status_code == 401


def test_old_database_is_upgraded(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript("CREATE TABLE treatments(id INTEGER PRIMARY KEY, patient_id INTEGER NOT NULL, tooth INTEGER, "
                      "procedure TEXT NOT NULL, fee REAL NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'planned', "
                      "invoice_id INTEGER, created_at TEXT NOT NULL);")
    old.commit()
    old.close()
    monkeypatch.setenv("DENTIFY_DB", str(path))
    c = db.init(seed=False)
    assert "code" in {r["name"] for r in c.execute("PRAGMA table_info(treatments)")}
    assert c.execute("SELECT COUNT(*) FROM fee_schedule").fetchone()[0] > 15


def test_plan_links_use_a_private_random_key(client, monkeypatch):
    pid = new_patient(client)
    t1 = client.get(f"/api/patients/{pid}/plan-link").json()["path"]
    assert client.get(f"/api/patients/{pid}/plan-link").json()["path"] == t1      # stable
    forged = f"/plan/{pid}/" + __import__("hmac").new(b"dentify-demo-secret", f"plan:{pid}".encode(), "sha256").hexdigest()[:20]
    assert client.get(forged).status_code == 404                                  # old default key does not work
    monkeypatch.setenv("PORTAL_SECRET", "my-own-secret")
    assert client.get(f"/api/patients/{pid}/plan-link").json()["path"] != t1
