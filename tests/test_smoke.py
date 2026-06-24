"""Smoke tests — no network/login (TestClient without lifespan)."""

from fastapi.testclient import TestClient

import app

client = TestClient(app.app)


def test_health():
    assert client.get("/health").json() == {"status": "ok"}


def test_models_listed():
    ids = [m["id"] for m in client.get("/v1/models").json()["data"]]
    assert "gpt-5.4-mini" in ids


def test_v1_info():
    assert client.get("/v1").json()["service"] == "Codex Proxy"


def test_unknown_route_returns_friendly_404():
    resp = client.get("/definitely-not-a-route")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


def test_auth_status_reports_unauthenticated_without_token():
    # Lifespan is not run, so no token is loaded.
    assert client.get("/auth/status").json()["authenticated"] is False


def test_models_env_override(monkeypatch):
    monkeypatch.setenv("CODEX_MODELS", "foo-1, bar-2")
    import importlib

    reloaded = importlib.reload(app)
    try:
        assert reloaded.MODEL_IDS == ["foo-1", "bar-2"]
    finally:
        monkeypatch.delenv("CODEX_MODELS", raising=False)
        importlib.reload(reloaded)
