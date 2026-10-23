"""Sample data so the demo looks alive on first run. Dates are relative to 'today'."""
import datetime as dt
import json
import random

from . import scheduling as S

PATIENTS = [
    ("Emily Carter", "emily.carter@example.com"), ("James Wilson", "jwilson@example.com"),
    ("Olivia Martinez", "olivia.m@example.com"), ("Daniel Brooks", "dbrooks@example.com"),
    ("Sophia Nguyen", "sophia.n@example.com"), ("Liam Thompson", "liam.t@example.com"),
    ("Ava Patel", "ava.patel@example.com"), ("Noah Johnson", "noah.j@example.com"),
    ("Mia Robinson", "mia.r@example.com"), ("Ethan Clark", "ethan.c@example.com"),
]
PRICES = {"checkup": 90, "cleaning": 150, "filling": 220, "whitening": 400,
          "implant": 120, "rootcanal": 950, "emergency": 130}


def run(conn):
    rnd = random.Random(7)
    n = S.now()
    conn.executemany("INSERT INTO providers(name,color) VALUES(?,?)",
                     [("Dr. Sarah Khan", "#2563eb"), ("Dr. Michael Reed", "#059669")])
    for i, (name, email) in enumerate(PATIENTS):
        conn.execute("INSERT INTO patients(name,phone,email,notes,created_at) VALUES(?,?,?,?,?)",
                     (name, f"55501001{i:02d}", email, "", S.iso(n - dt.timedelta(days=rnd.randint(20, 400)))))
    conn.commit()
    ids = [r["id"] for r in conn.execute("SELECT id FROM patients ORDER BY id")]
    recall_ids, regular = ids[:3], ids[3:]

    # recall candidates: one old completed visit (> 6 months ago), nothing upcoming
    for pid in recall_ids:
        d = (n - dt.timedelta(days=rnd.randint(200, 260))).date()
        while not S.is_open_day(d):
            d -= dt.timedelta(days=1)
        S.book(conn, pid, "cleaning", dt.datetime.combine(d, dt.time(10)), "hub", allow_past=True, status="completed")

    def place(day, status, source):
        typ = rnd.choice(list(S.TYPES))
        for _ in range(8):
            o, c = S.HOURS[day.weekday()]
            hh = rnd.randrange(o * 2, c * 2) / 2
            start = dt.datetime.combine(day, dt.time(int(hh), int(hh % 1 * 60)))
            try:
                return S.book(conn, rnd.choice(regular), typ, start, source, allow_past=True, status=status), typ
            except ValueError:
                continue
        return None, typ

    # history: past 90 days
    for off in range(-90, 0):
        day = (n + dt.timedelta(days=off)).date()
        if not S.is_open_day(day):
            continue
        for _ in range(rnd.choice([0, 1, 2, 3])):
            status = rnd.choices(["completed", "no_show", "cancelled"], [86, 9, 5])[0]
            source = rnd.choice(["hub", "hub", "ai_voice", "ai_chat"])
            appt, typ = place(day, status, source)
            if appt and status == "completed":
                conn.execute("INSERT INTO invoices(patient_id,description,amount,status,created_at) "
                             "SELECT patient_id, ?, ?, 'paid', ? FROM appointments WHERE id=?",
                             (S.type_name(typ), PRICES[typ], S.iso(S.parse(S.iso(n)) + dt.timedelta(days=off)), appt))

    # upcoming: next 10 days (some booked by the AI)
    for off in range(0, 11):
        day = (n + dt.timedelta(days=off)).date()
        if not S.is_open_day(day):
            continue
        for _ in range(rnd.choice([2, 3, 4])):
            start = dt.datetime.combine(day, dt.time(9)) + dt.timedelta(minutes=30 * rnd.randrange(0, 14))
            if start <= n:
                continue
            typ = rnd.choice(["checkup", "cleaning", "filling", "whitening", "implant"])
            try:
                S.book(conn, rnd.choice(regular), typ, start, rnd.choice(["hub", "hub", "ai_voice"]))
            except ValueError:
                pass

    conn.execute("INSERT INTO invoices(patient_id,description,amount,status,created_at) VALUES(?,?,?,?,?)",
                 (regular[0], "Filling", 220, "unpaid", S.iso(n - dt.timedelta(days=2))))
    conn.execute("INSERT INTO invoices(patient_id,description,amount,status,created_at) VALUES(?,?,?,?,?)",
                 (regular[1], "Whitening", 400, "unpaid", S.iso(n - dt.timedelta(days=1))))

    sample = [
        ("voice", "5550100103", "booked", "Daniel Brooks: Booked cleaning (sample call).",
         [["caller", "Hi, I need a cleaning next week"], ["agent", "Sure, what day works best?"]]),
        ("voice", None, "info", "Unknown caller: Answered question (insurance).",
         [["caller", "Do you take Delta Dental?"], ["agent", "We accept most major PPO plans, including Delta Dental."]]),
        ("chat", "5550100105", "rescheduled", "Liam Thompson: Rescheduled check-up (sample chat).",
         [["caller", "Can I move my appointment to Friday?"], ["agent", "Done! Your appointment is now Friday."]]),
    ]
    for i, (ch, ph, outcome, summary, tr) in enumerate(sample):
        conn.execute("INSERT INTO calls(started_at,channel,caller_phone,transcript,summary,outcome) VALUES(?,?,?,?,?,?)",
                     (S.iso(n - dt.timedelta(hours=5 * (i + 1))), ch, ph, json.dumps(tr), summary, outcome))
    conn.commit()


# ---------------------------------------------------------------- practice-management sample data
FEES = [  # code, name, category, coverage class, fee
    ("D0120", "Periodic exam", "Diagnostic", "preventive", 65), ("D0150", "Comprehensive exam", "Diagnostic", "preventive", 105),
    ("D0210", "Full-mouth X-rays", "Diagnostic", "preventive", 135), ("D0274", "Bitewing X-rays (4)", "Diagnostic", "preventive", 85),
    ("D1110", "Adult cleaning", "Preventive", "preventive", 120), ("D1206", "Fluoride varnish", "Preventive", "preventive", 45),
    ("D1351", "Sealant (per tooth)", "Preventive", "preventive", 55),
    ("D2391", "Composite filling, 1 surface (back)", "Restorative", "basic", 195),
    ("D2392", "Composite filling, 2 surfaces (back)", "Restorative", "basic", 245),
    ("D2393", "Composite filling, 3 surfaces (back)", "Restorative", "basic", 295),
    ("D2740", "Porcelain crown", "Restorative", "major", 1150), ("D2950", "Core buildup", "Restorative", "major", 325),
    ("D3310", "Root canal, front tooth", "Endodontics", "basic", 850), ("D3320", "Root canal, premolar", "Endodontics", "basic", 950),
    ("D3330", "Root canal, molar", "Endodontics", "basic", 1150),
    ("D4341", "Deep cleaning (per quadrant)", "Periodontics", "basic", 230), ("D4910", "Periodontal maintenance", "Periodontics", "basic", 145),
    ("D7140", "Simple extraction", "Oral surgery", "basic", 175), ("D7210", "Surgical extraction", "Oral surgery", "basic", 320),
    ("D6010", "Implant placement", "Implants", "major", 1900), ("D6058", "Implant crown", "Implants", "major", 1450),
    ("D9110", "Emergency pain relief", "Emergency", "basic", 120), ("D9972", "Teeth whitening (per arch)", "Cosmetic", "cosmetic", 350),
]
PLANS = [  # carrier, annual max, used so far this year, deductible met
    ("Delta Dental PPO", 1500, 420, 50), ("MetLife Dental", 2000, 0, 0), ("Cigna Dental", 1500, 1150, 50),
    ("Aetna Dental", 1000, 160, 50), ("Guardian", 2500, 300, 0), ("Delta Dental PPO", 1500, 680, 50),
    ("MetLife Dental", 1500, 90, 50),
]
MEDICAL = {  # index into the first 10 patients -> allergies, conditions, medications, premed
    1: ("Penicillin", "Hypertension", "Lisinopril", 0), 2: ("Latex", "", "", 0),
    3: ("", "Type 2 diabetes", "Metformin", 0), 4: ("", "Atrial fibrillation", "Warfarin", 0),
    5: ("Codeine", "Prosthetic heart valve", "", 1), 6: ("Sulfa drugs", "Asthma", "Albuterol inhaler", 0),
}
# patient index -> [(code, tooth, status, days_ago_done, claim_path)]; claim_path: draft | submit | paid | deny
TX = {
    0: [("D2740", 14, "planned", 0, ""), ("D2950", 14, "planned", 0, ""), ("D2392", 19, "accepted", 0, "")],
    1: [("D3330", 30, "accepted", 0, ""), ("D2740", 30, "accepted", 0, ""), ("D4341", None, "planned", 0, "")],
    2: [("D2391", 3, "done", 9, "submit"), ("D1351", 2, "planned", 0, "")],
    3: [("D4341", None, "planned", 0, ""), ("D2393", 18, "declined", 0, "")],
    4: [("D6010", 19, "accepted", 0, ""), ("D6058", 19, "planned", 0, "")],
    5: [("D2392", 12, "done", 41, "paid"), ("D0274", None, "done", 41, "paid"), ("D2740", 8, "planned", 0, "")],
    6: [("D3320", 12, "done", 3, "draft"), ("D2950", 12, "planned", 0, "")],
    7: [("D2391", 8, "done", 46, ""), ("D9972", None, "planned", 0, "")],
    8: [("D7210", 17, "done", 100, ""), ("D2391", 30, "accepted", 0, "")],
    9: [("D2740", 3, "declined", 0, "")],
}
NOTE_TEXT = {
    "checkup": ("Routine exam. No pain reported.", "Soft tissue normal. Occlusion stable. BWX taken, no new caries seen.",
                "Good oral health. Mild gingivitis in lower anteriors.", "Cleaning today. Review home care. Recall 6 months."),
    "cleaning": ("No complaints. Brushes 2x daily, flosses occasionally.", "Moderate calculus lower lingual. Light bleeding on probing.",
                 "Generalized gingivitis, improving.", "Prophy completed. OHI given. Recall 6 months."),
    "filling": ("Sensitivity to cold on upper right.", "Occlusal decay visible on tooth, confirmed on radiograph.",
                "Caries limited to enamel/dentin.", "Anesthesia, caries removed, composite placed. Post-op instructions given."),
    "rootcanal": ("Spontaneous throbbing pain, worse at night.", "Positive percussion, lingering cold response.",
                  "Irreversible pulpitis.", "Pulp extirpated, canals shaped and obturated. Crown recommended."),
    "whitening": ("Wants a brighter smile.", "Shade A3, no active decay.", "Suitable for whitening.", "In-office whitening, shade A1 achieved."),
    "implant": ("Missing tooth, interested in an implant.", "Adequate ridge width on CBCT.", "Good implant candidate.", "Treatment plan and estimate presented."),
    "emergency": ("Acute pain for two days.", "Swelling and tenderness, tooth fractured.", "Fractured tooth, pulpal involvement.", "Palliative treatment; follow-up booked."),
}


def run_fees(conn):
    conn.executemany("INSERT OR IGNORE INTO fee_schedule(code,name,category,coverage_class,fee) VALUES(?,?,?,?,?)", FEES)
    conn.commit()


def run_pm(conn):
    """Insurance, medical alerts, treatment plans, claims in every stage, aged balances and clinical notes."""
    from . import pm
    n = S.now()
    ids = [r["id"] for r in conn.execute("SELECT id FROM patients ORDER BY id LIMIT 10")]
    if not ids:
        return
    ago = lambda days: S.iso(n - dt.timedelta(days=days))
    for i, (carrier, mx, used, ded_met) in enumerate(PLANS[:len(ids)]):
        conn.execute("INSERT INTO insurance_plans(patient_id,carrier,member_id,group_no,subscriber,annual_max,used,deductible,deductible_met,updated_at)"
                     " VALUES(?,?,?,?,?,?,?,?,?,?)",
                     (ids[i], carrier, f"{carrier[:2].upper()}{4400100 + i * 37}", f"G{7000 + i * 11}", "Self", mx, used, 50, ded_met, S.iso(n)))
    for i, (al, co, me, pre) in MEDICAL.items():
        if i < len(ids):
            conn.execute("INSERT INTO patient_medical(patient_id,allergies,conditions,medications,premed,updated_at) VALUES(?,?,?,?,?,?)",
                         (ids[i], al, co, me, pre, S.iso(n)))
    prices = {r["code"]: r["fee"] for r in conn.execute("SELECT code, fee FROM fee_schedule")}
    names = {r["code"]: r["name"] for r in conn.execute("SELECT code, name FROM fee_schedule")}
    for i, items in TX.items():
        if i >= len(ids):
            continue
        for code, tooth, status, days, path in items:
            cur = conn.execute("INSERT INTO treatments(patient_id,tooth,procedure,fee,status,created_at,code) VALUES(?,?,?,?,?,?,?)",
                               (ids[i], tooth, names[code], prices[code], "planned" if status == "done" else status, ago(days + 5), code))
            if status != "done":
                continue
            r = pm.complete_treatment(conn, cur.lastrowid, ago(days))
            if r["claim_id"] and path in ("submit", "paid"):
                pm.submit_claim(conn, r["claim_id"], ago(days - 1))
            if r["claim_id"] and path == "paid":
                pm.pay_claim(conn, r["claim_id"], None, ago(max(days - 15, 0)))
    # old patient balances that were paid (keeps aging honest): only the 46-day and 100-day ones stay unpaid
    conn.execute("UPDATE invoices SET status='paid' WHERE description LIKE '%patient portion%' AND created_at<?", (ago(30),))
    # signed + draft SOAP notes on recent completed visits
    done = conn.execute("SELECT id, patient_id, provider_id, type, start FROM appointments WHERE status='completed' "
                        "ORDER BY start DESC LIMIT 8").fetchall()
    for k, a in enumerate(done):
        s_, o_, a_, p_ = NOTE_TEXT[a["type"]]
        conn.execute("INSERT INTO clinical_notes(patient_id,appointment_id,provider_id,subjective,objective,assessment,plan,signed_at,created_at)"
                     " VALUES(?,?,?,?,?,?,?,?,?)", (a["patient_id"], a["id"], a["provider_id"], s_, o_, a_, p_,
                                                    None if k < 2 else a["start"], a["start"]))
    conn.commit()
