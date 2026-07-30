from fastapi.testclient import TestClient

from app.main import app


def test_app_importable() -> None:
    assert app is not None


def test_health_returns_200() -> None:
    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
