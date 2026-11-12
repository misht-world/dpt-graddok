"""Personal accounts: created by the administrator, e-mail + password, roles, disabling."""

import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import load_config
from app.server import create_app

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def app(tmp_path, monkeypatch):
    shutil.copy(ROOT / "config.example.yaml", tmp_path / "config.yaml")
    shutil.copy(ROOT / "committees.yaml", tmp_path / "committees.yaml")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("CONFIG_FILE", raising=False)
    monkeypatch.delenv("COMMITTEES_FILE", raising=False)
    cfg = load_config(tmp_path / "config.yaml")
    cfg.admin_password = "owner-pass"
    cfg.viewer_password = "shared-pass"
    return create_app(cfg)


def login(c, password, email=""):
    return c.post("/api/login", data={"password": password, "login": email})


def test_accounts_lifecycle(app):
    with TestClient(app) as owner, TestClient(app) as shared, TestClient(app) as colleague:
        assert login(owner, "owner-pass").status_code == 200
        assert login(shared, "shared-pass").status_code == 200
        assert owner.get("/api/admin/users").json()["shared_viewer"] is True

        r = owner.post("/api/admin/users", json={"login": "Ivan@Mail.ru", "name": "Иван", "role": "viewer", "password": "short"})
        assert r.status_code == 400
        r = owner.post("/api/admin/users", json={"login": "Ivan@Mail.ru", "name": "Иван", "role": "viewer", "password": "long-enough"})
        assert r.status_code == 200, r.text
        uid = r.json()["user"]["id"]
        assert "pw_hash" not in r.json()["user"]
        assert owner.post("/api/admin/users", json={"login": "ivan@mail.ru", "password": "long-enough"}).status_code == 400

        # the shared viewing password stops working once a personal account exists
        assert shared.get("/api/meta").status_code == 401
        assert login(shared, "shared-pass").status_code == 401

        assert login(colleague, "wrong-pass", "ivan@mail.ru").status_code == 401
        assert login(colleague, "long-enough", " IVAN@mail.ru ").status_code == 200
        meta = colleague.get("/api/meta").json()
        assert meta["role"] == "viewer" and meta["user"] == "Иван"
        assert colleague.get("/api/admin/users").status_code == 403
        assert owner.get("/api/admin/users").json()["users"][0]["last_login"]

        # promotion takes effect at once; password change ends the session
        assert owner.patch(f"/api/admin/users/{uid}", json={"role": "admin"}).status_code == 200
        assert colleague.get("/api/meta").status_code == 401   # role is part of the session tag
        assert login(colleague, "long-enough", "ivan@mail.ru").status_code == 200
        assert colleague.get("/api/admin/users").status_code == 200
        assert colleague.patch(f"/api/admin/users/{uid}", json={"active": False}).status_code == 400  # not yourself

        assert owner.patch(f"/api/admin/users/{uid}", json={"password": "another-one"}).status_code == 200
        assert colleague.get("/api/meta").status_code == 401
        assert login(colleague, "long-enough", "ivan@mail.ru").status_code == 401
        assert login(colleague, "another-one", "ivan@mail.ru").status_code == 200

        assert owner.patch(f"/api/admin/users/{uid}", json={"active": False}).status_code == 200
        assert colleague.get("/api/meta").status_code == 401
        assert login(colleague, "another-one", "ivan@mail.ru").status_code == 401

        # with nobody active the shared password works again; owner always works
        assert login(shared, "shared-pass").status_code == 200
        assert owner.delete(f"/api/admin/users/{uid}").status_code == 200
        assert owner.get("/api/admin/users").json()["users"] == []


def test_login_attempt_limit_per_login(app):
    with TestClient(app) as c:
        app_state_limit = 10
        for _ in range(app_state_limit):
            login(c, "bad-password", "someone@mail.ru")
        assert login(c, "bad-password", "someone@mail.ru").status_code == 429
