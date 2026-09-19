from app.db.session import Database


def test_health_degraded_when_db_is_down(client):
    res = client.get("/health")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "degraded"
    assert body["database"] == "unavailable"


def test_health_ok_when_db_is_up(client, monkeypatch):
    async def fake_ping(self, timeout=3.0):
        return True

    monkeypatch.setattr(Database, "ping", fake_ping)
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json()["status"] == "ok"
    assert res.json()["database"] == "connected"
