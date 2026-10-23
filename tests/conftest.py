import datetime as dt
import pytest
from fastapi.testclient import TestClient

FROZEN = dt.datetime(2026, 10, 15, 10, 0)   # a Thursday, mid-morning


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DENTIFY_DB", str(tmp_path / "test.db"))
    monkeypatch.setenv("LLM_BACKEND", "none")
    from app import scheduling, agent
    monkeypatch.setattr(scheduling, "now", lambda: FROZEN)
    agent.SESSIONS.clear()
    from app import routes_social
    routes_social._HITS.clear()
    from app.main import app
    with TestClient(app) as c:
        yield c


class Chat:
    """Helper that drives the receptionist through the real HTTP API."""
    def __init__(self, client, channel="voice"):
        self.c = client
        r = client.post("/api/agent/start", json={"channel": channel}).json()
        self.sid, self.greeting = r["session_id"], r["reply"]

    def say(self, text):
        return self.c.post("/api/agent/message", json={"session_id": self.sid, "text": text}).json()["reply"]

    def end(self):
        return self.c.post("/api/agent/end", json={"session_id": self.sid}).json()


@pytest.fixture()
def chat(client):
    return lambda channel="voice": Chat(client, channel)
