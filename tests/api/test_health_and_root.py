from __future__ import annotations

from app.core.config import FRONTEND_DIR


def test_application_imports_and_uses_the_new_entry_point():
    import app.main as main
    import server

    assert main.app.title == "ScamShield AI"
    assert server.app is main.app          # compatibility shim: `uvicorn server:app` still works


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


def test_root_serves_the_existing_frontend_unchanged(client):
    r = client.get("/")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert r.content == (FRONTEND_DIR / "index.html").read_bytes()
    assert "ScamShield AI" in r.text


def test_root_reports_404_when_frontend_is_missing(client, monkeypatch, tmp_path):
    import app.main as main

    monkeypatch.setattr(main, "FRONTEND_DIR", tmp_path)
    r = client.get("/")
    assert r.status_code == 404 and r.json() == {"detail": "The web interface is not available.", "code": "NOT_FOUND"}


def test_all_expected_paths_exist(client):
    paths = set(client.get("/openapi.json").json()["paths"])
    assert {"/", "/health", "/api/analyze-text", "/api/analyze-audio",
            "/api/v1/analyze/text", "/api/v1/analyze/audio"} <= paths


def test_startup_and_shutdown_run_and_close_the_provider(app, provider):
    from fastapi.testclient import TestClient

    with TestClient(app) as running:        # context manager runs the lifespan
        assert running.get("/health").status_code == 200
        assert provider.closed is False
    assert provider.closed is True
