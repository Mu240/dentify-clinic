"""SQLite storage. One file, zero setup - perfect for a demo."""
import os
import sqlite3

DEFAULT_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "dentify.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS providers(
    id INTEGER PRIMARY KEY, name TEXT NOT NULL, color TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS patients(
    id INTEGER PRIMARY KEY, name TEXT NOT NULL, phone TEXT UNIQUE NOT NULL,
    email TEXT, notes TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS appointments(
    id INTEGER PRIMARY KEY, patient_id INTEGER NOT NULL, provider_id INTEGER NOT NULL,
    type TEXT NOT NULL, start TEXT NOT NULL, end TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'scheduled', source TEXT NOT NULL DEFAULT 'hub',
    created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_appt_start ON appointments(start);
CREATE TABLE IF NOT EXISTS calls(
    id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, channel TEXT NOT NULL,
    caller_phone TEXT, transcript TEXT, summary TEXT, outcome TEXT);
CREATE TABLE IF NOT EXISTS messages(
    id INTEGER PRIMARY KEY, patient_id INTEGER, appointment_id INTEGER,
    channel TEXT NOT NULL, kind TEXT NOT NULL, body TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS invoices(
    id INTEGER PRIMARY KEY, patient_id INTEGER NOT NULL, description TEXT NOT NULL,
    amount REAL NOT NULL, status TEXT NOT NULL DEFAULT 'unpaid', created_at TEXT NOT NULL);

-- Social inbox: one thread per (channel, sender). The bot's conversation state is stored as JSON
-- so a chat survives restarts. handoff=1 means a human has taken over and the bot stays silent.
CREATE TABLE IF NOT EXISTS social_threads(
    id INTEGER PRIMARY KEY, channel TEXT NOT NULL, sender_id TEXT NOT NULL, sender_name TEXT,
    patient_id INTEGER, session_json TEXT, handoff INTEGER NOT NULL DEFAULT 0,
    unread INTEGER NOT NULL DEFAULT 0, last_at TEXT NOT NULL, created_at TEXT NOT NULL,
    UNIQUE(channel, sender_id));
CREATE TABLE IF NOT EXISTS social_messages(
    id INTEGER PRIMARY KEY, thread_id INTEGER NOT NULL, direction TEXT NOT NULL,
    author TEXT NOT NULL, body TEXT NOT NULL, external_id TEXT UNIQUE,
    delivered INTEGER NOT NULL DEFAULT 1, error TEXT, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_sm_thread ON social_messages(thread_id);

-- Clinical: current state of each tooth (Universal numbering 1-32) and the treatment plan.
CREATE TABLE IF NOT EXISTS tooth_records(
    id INTEGER PRIMARY KEY, patient_id INTEGER NOT NULL, tooth INTEGER NOT NULL,
    condition TEXT NOT NULL, note TEXT, updated_at TEXT NOT NULL, UNIQUE(patient_id, tooth));
CREATE TABLE IF NOT EXISTS treatments(
    id INTEGER PRIMARY KEY, patient_id INTEGER NOT NULL, tooth INTEGER, procedure TEXT NOT NULL,
    fee REAL NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'planned', invoice_id INTEGER,
    created_at TEXT NOT NULL, code TEXT);

-- Practice-management layer: fee schedule (procedure codes), medical alerts, insurance benefits,
-- claims and signed clinical notes. See app/pm.py.
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS fee_schedule(
    code TEXT PRIMARY KEY, name TEXT NOT NULL, category TEXT NOT NULL,
    coverage_class TEXT NOT NULL, fee REAL NOT NULL);
CREATE TABLE IF NOT EXISTS patient_medical(
    patient_id INTEGER PRIMARY KEY, allergies TEXT, conditions TEXT, medications TEXT,
    premed INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS insurance_plans(
    patient_id INTEGER PRIMARY KEY, carrier TEXT NOT NULL, member_id TEXT, group_no TEXT, subscriber TEXT,
    annual_max REAL NOT NULL DEFAULT 1500, used REAL NOT NULL DEFAULT 0,
    deductible REAL NOT NULL DEFAULT 50, deductible_met REAL NOT NULL DEFAULT 0,
    preventive_pct INTEGER NOT NULL DEFAULT 100, basic_pct INTEGER NOT NULL DEFAULT 80,
    major_pct INTEGER NOT NULL DEFAULT 50, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS claims(
    id INTEGER PRIMARY KEY, patient_id INTEGER NOT NULL, treatment_id INTEGER UNIQUE,
    carrier TEXT NOT NULL, code TEXT, description TEXT NOT NULL, billed REAL NOT NULL,
    expected REAL NOT NULL, paid REAL NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'draft',
    note TEXT, created_at TEXT NOT NULL, submitted_at TEXT, resolved_at TEXT);
CREATE TABLE IF NOT EXISTS clinical_notes(
    id INTEGER PRIMARY KEY, patient_id INTEGER NOT NULL, appointment_id INTEGER, provider_id INTEGER,
    subjective TEXT, objective TEXT, assessment TEXT, plan TEXT, signed_at TEXT, created_at TEXT NOT NULL);
"""


def db_path():
    return os.environ.get("DENTIFY_DB", DEFAULT_PATH)


def connect():
    conn = sqlite3.connect(db_path(), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _migrate(conn):
    """Add columns that older demo databases do not have yet."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(treatments)")}
    if "code" not in cols:
        conn.execute("ALTER TABLE treatments ADD COLUMN code TEXT")


def init(seed=True):
    conn = connect()
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.commit()
    from . import seed as seed_mod
    if seed and conn.execute("SELECT COUNT(*) FROM providers").fetchone()[0] == 0:
        seed_mod.run(conn)
    # Fee schedule is reference data (always loaded once); the sample insurance/claims/notes data is
    # added once, also to demo databases created before these features existed.
    if conn.execute("SELECT COUNT(*) FROM fee_schedule").fetchone()[0] == 0:
        seed_mod.run_fees(conn)
        if seed:
            seed_mod.run_pm(conn)
    return conn
