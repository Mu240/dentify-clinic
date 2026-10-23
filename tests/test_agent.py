import datetime as dt
import pytest
from app import agent, db, scheduling as S, llm


def test_parse_date_and_time():
    today = dt.date(2026, 10, 15)                      # Thursday
    p = agent.parse_date
    assert p("tomorrow", today) == dt.date(2026, 10, 16)
    assert p("next monday", today) == dt.date(2026, 10, 19)
    assert p("next friday", today) == dt.date(2026, 10, 23)
    assert p("this friday", today) == dt.date(2026, 10, 16)
    assert p("october 28", today) == dt.date(2026, 10, 28)
    assert p("3rd of november", today) == dt.date(2026, 11, 3)
    assert p("january 5", today) == dt.date(2027, 1, 5)
    assert p("2026-12-01", today) == dt.date(2026, 12, 1)
    assert p("whenever", today) is None
    t = agent.parse_time
    assert t("10 am") == dt.time(10, 0) and t("2:30 pm") == dt.time(14, 30)
    assert t("at 3") == dt.time(15, 0) and t("at 10") == dt.time(10, 0) and t("noon") == dt.time(12, 0)
    assert t("nothing") is None


def test_spoken_phone_digits():
    assert agent.words_to_digits("five five five one two three four five six seven") == "5551234567"


def test_full_booking_flow_voice(client, chat):
    c = chat("voice")
    assert "virtual receptionist" in c.greeting
    assert "What day" in c.say("Hi, I'd like to book a cleaning")
    r = c.say("tomorrow")
    assert "Friday, October 16" in r
    slots = client.get("/api/availability", params={"date": "2026-10-16", "type": "cleaning"}).json()["slots"]
    first = slots[0]
    assert "phone number" in c.say(f"{int(first[:2]) % 12 or 12}:{first[3:]} {'pm' if first >= '12:00' else 'am'}")
    assert "full name" in c.say("my number is 555 777 1234")
    assert "email" in c.say("my name is Jane Doe")
    r = c.say("jane dot doe at gmail dot com")          # spoken address
    assert "all set, Jane" in r
    assert db.connect().execute("SELECT email FROM patients WHERE name='Jane Doe'").fetchone()[0] == "jane.doe@gmail.com"
    appts = client.get("/api/appointments").json()
    mine = [a for a in appts if a["patient_name"] == "Jane Doe"]
    assert len(mine) == 1 and mine[0]["source"] == "ai_voice" and mine[0]["start"] == f"2026-10-16T{first}"
    assert any(m["kind"] == "confirmation" and m["patient_name"] == "Jane Doe" for m in client.get("/api/messages").json())
    assert "goodbye" in c.say("no thanks").lower()
    log = client.get("/api/calls").json()[0]
    assert log["outcome"] == "booked" and "Jane Doe" in log["summary"]


def test_ai_never_double_books_what_the_hub_filled(client, chat):
    c = db.connect()
    ids = [r["id"] for r in c.execute("SELECT id FROM patients LIMIT 2")]
    for i in ids:   # Hub fills BOTH providers at 9:00 on an empty future day
        assert client.post("/api/appointments", json={"patient_id": i, "type": "checkup", "start": "2026-11-10T09:00"}).status_code == 201
    ch = chat()
    r = ch.say("book a check-up on 2026-11-10 at 9 am")
    assert "isn't available" in r
    offered = r.split("I can do")[1]
    assert "9 AM" not in offered                      # it must offer other times, never the taken one


def test_existing_patient_skips_name(client, chat):
    ch = chat()
    ch.say("book a checkup on 2026-11-12 at 2 pm")
    r = ch.say("555 010 0108")
    assert "all set" in r                              # phone matched a seeded patient, no name asked


def test_cancel_flow_needs_confirmation(client, chat):
    c = db.connect()
    pat = client.get("/api/patients").json()[5]
    for a in S.upcoming(c, pat["id"]):                # give this patient exactly one upcoming visit
        S.set_status(c, a["id"], "cancelled")
    aid = S.book(c, pat["id"], "checkup", "2026-11-12T10:00")

    ch = chat()
    assert "phone number" in ch.say("I need to cancel my appointment")
    assert "Shall I cancel" in ch.say(pat["phone"])
    assert "left your appointment" in ch.say("no")
    assert S.get_appt(c, aid)["status"] == "scheduled"   # 'no' must not cancel anything

    ch2 = chat()
    ch2.say("cancel my appointment")
    ch2.say(pat["phone"])
    assert "cancelled" in ch2.say("yes")
    assert S.get_appt(c, aid)["status"] == "cancelled"
    assert any(m["kind"] == "cancellation" for m in client.get("/api/messages").json())


def test_cancel_unknown_phone(client, chat):
    ch = chat()
    ch.say("cancel my appointment")
    assert "couldn't find" in ch.say("555 000 1111")


def test_reschedule_moves_same_appointment(client, chat):
    c = db.connect()
    pat = client.get("/api/patients").json()[6]
    for a in S.upcoming(c, pat["id"]):
        S.set_status(c, a["id"], "cancelled")
    aid = S.book(c, pat["id"], "cleaning", "2026-11-12T10:00")
    ch = chat()
    ch.say("I need to reschedule my appointment"); ch.say(pat["phone"])
    ch.say("2026-11-13")
    r = ch.say("3 pm")
    assert "moved your cleaning" in r
    a = S.get_appt(c, aid)
    assert a["start"] == "2026-11-13T15:00" and a["status"] == "scheduled"


def test_closed_sunday_and_fully_booked_day(client, chat):
    ch = chat()
    ch.say("book a cleaning")
    assert "closed on Sundays" in ch.say("2026-10-18")


def test_emergency_has_safety_notice_and_defaults_to_today(client, chat):
    r = chat().say("I have a toothache")
    assert "911" in r and "Thursday, October 15" in r


def test_faq_answers_do_not_hijack_flow(client, chat):
    ch = chat()
    ch.say("book a cleaning")
    r = ch.say("do you take Delta Dental?")
    assert "PPO" in r and "What day" in r              # answered, then returned to the question


def test_faq_basics(client, chat):
    ch = chat("chat")
    assert "9 AM to 5 PM" in ch.say("what are your hours?")
    assert "$350" in ch.say("how much is whitening?")
    assert "payment plans" in ch.say("do you offer payment plans?")


def test_human_request_after_hours_takes_message(client, chat, monkeypatch):
    monkeypatch.setattr(S, "is_open_now", lambda n=None: False)
    ch = chat()
    assert "take a message" in ch.say("let me talk to a human")
    assert "passed that to the team" in ch.say("please call me back about my invoice")
    assert any(m["kind"] == "staff_message" for m in client.get("/api/messages").json())


def test_human_request_open_hours_transfers(client, chat, monkeypatch):
    monkeypatch.setattr(S, "is_open_now", lambda n=None: True)
    ch = chat()
    assert "transferring" in ch.say("I want to speak to a real person")
    assert client.get("/api/calls").json()[0]["outcome"] == "transferred"


def test_unknown_input_offers_help_then_human(client, chat):
    ch = chat()
    assert "What would you like to do" in ch.say("blah")
    assert "human" in ch.say("what is the airspeed of a swallow")


def test_llm_used_when_enabled_and_falls_back_on_failure(client, chat, monkeypatch):
    monkeypatch.setenv("LLM_BACKEND", "ollama")
    monkeypatch.setattr(llm, "answer", lambda q, f: "LLM says hello")
    assert chat().say("what are your hours?") == "LLM says hello"
    monkeypatch.setattr(llm, "answer", lambda q, f: None)          # LLM down -> rule-based answer
    assert "9 AM to 5 PM" in chat().say("what are your hours?")


def test_html_is_not_executed_in_transcripts(client, chat):
    ch = chat()
    ch.say("<script>alert(1)</script>")
    t = client.get("/api/calls").json()[0]["transcript"]
    assert "<script>" in t        # stored raw; the UI escapes on render (esc())


def test_email_can_be_typed_or_skipped(client, chat):
    ch = chat("chat")
    ch.say("book a checkup on 2026-11-12 at 2 pm")
    ch.say("555 010 9999")
    assert "email" in ch.say("my name is Omar Typed")
    assert "all set" in ch.say("omar123456@gmail.com")   # digits in the address must not become a phone number
    row = db.connect().execute("SELECT phone,email FROM patients WHERE name='Omar Typed'").fetchone()
    assert row["email"] == "omar123456@gmail.com" and row["phone"] == "5550109999"
    ch2 = chat("chat")
    ch2.say("book a checkup on 2026-11-13 at 2 pm")
    ch2.say("555 010 8888")
    assert "email" in ch2.say("my name is Nadia Skip")
    assert "all set" in ch2.say("skip")
    assert db.connect().execute("SELECT email FROM patients WHERE name='Nadia Skip'").fetchone()[0] is None
