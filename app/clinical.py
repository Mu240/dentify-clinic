"""Clinical records: tooth chart (Universal numbering 1-32) and treatment plans.
Marking a treatment 'done' automatically raises the invoice, so charting and billing never drift."""
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import db, pm
from . import scheduling as S

router = APIRouter()
CONDITIONS = ("healthy", "watch", "cavity", "filled", "crown", "root_canal", "implant", "missing")
STATUSES = ("planned", "accepted", "done", "declined")


def _rows(conn, q, args=()):
    return [dict(r) for r in conn.execute(q, args).fetchall()]


def _need_patient(conn, pid):
    if not conn.execute("SELECT 1 FROM patients WHERE id=?", (pid,)).fetchone():
        raise HTTPException(404, "patient not found")


@router.get("/api/patients/{pid}/chart")
def chart(pid: int):
    c = db.connect()
    _need_patient(c, pid)
    return {"conditions": CONDITIONS,
            "teeth": _rows(c, "SELECT tooth, condition, note, updated_at FROM tooth_records WHERE patient_id=? ORDER BY tooth", (pid,)),
            "treatments": _rows(c, "SELECT * FROM treatments WHERE patient_id=? ORDER BY id DESC", (pid,))}


class ToothIn(BaseModel):
    condition: str
    note: Optional[str] = None


@router.put("/api/patients/{pid}/teeth/{tooth}")
def set_tooth(pid: int, tooth: int, b: ToothIn):
    if not 1 <= tooth <= 32:
        raise HTTPException(422, "tooth must be 1-32 (Universal numbering)")
    if b.condition not in CONDITIONS:
        raise HTTPException(422, "unknown condition")
    c = db.connect()
    _need_patient(c, pid)
    c.execute("INSERT INTO tooth_records(patient_id,tooth,condition,note,updated_at) VALUES(?,?,?,?,?) "
              "ON CONFLICT(patient_id,tooth) DO UPDATE SET condition=excluded.condition, "
              "note=excluded.note, updated_at=excluded.updated_at",
              (pid, tooth, b.condition, b.note, S.iso(S.now())))
    c.commit()
    return {"ok": True}


class TreatmentIn(BaseModel):
    procedure: str = ""
    tooth: Optional[int] = None
    fee: Optional[float] = None
    code: Optional[str] = None          # procedure code from the fee schedule; fills in name + fee


@router.post("/api/patients/{pid}/treatments", status_code=201)
def add_treatment(pid: int, b: TreatmentIn):
    c = db.connect()
    _need_patient(c, pid)
    proc, fee, code = b.procedure.strip(), b.fee, (b.code or "").strip() or None
    if code:
        f = c.execute("SELECT * FROM fee_schedule WHERE code=?", (code,)).fetchone()
        if not f:
            raise HTTPException(422, "unknown procedure code")
        proc, fee = proc or f["name"], f["fee"] if fee is None else fee
    if not proc:
        raise HTTPException(422, "procedure is required")
    fee = 0 if fee is None else fee
    if b.tooth is not None and not 1 <= b.tooth <= 32:
        raise HTTPException(422, "tooth must be 1-32")
    if fee < 0:
        raise HTTPException(422, "fee cannot be negative")
    cur = c.execute("INSERT INTO treatments(patient_id,tooth,procedure,fee,created_at,code) VALUES(?,?,?,?,?,?)",
                    (pid, b.tooth, proc, fee, S.iso(S.now()), code))
    c.commit()
    return {"id": cur.lastrowid}


class TreatmentPatch(BaseModel):
    status: str


@router.patch("/api/treatments/{tid}")
def update_treatment(tid: int, b: TreatmentPatch):
    if b.status not in STATUSES:
        raise HTTPException(422, "bad status")
    c = db.connect()
    t = c.execute("SELECT * FROM treatments WHERE id=?", (tid,)).fetchone()
    if not t:
        raise HTTPException(404, "treatment not found")
    if b.status == "done":
        r = pm.complete_treatment(c, tid)       # raises the claim (insurance share) + invoice (patient share)
        return {"ok": True, "invoice_id": r["invoice_id"], "claim_id": r["claim_id"]}
    c.execute("UPDATE treatments SET status=? WHERE id=?", (b.status, tid))
    c.commit()
    return {"ok": True, "invoice_id": t["invoice_id"]}
