"""Projects kept by hand (АГПТ, Концепция, «Проект»): registry, customer, release without a snapshot."""

import os
import shutil
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import load_config
from app.server import create_app

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def client(tmp_path, monkeypatch):
    shutil.copy(ROOT / "config.example.yaml", tmp_path / "config.yaml")
    shutil.copy(ROOT / "committees.yaml", tmp_path / "committees.yaml")
    work = tmp_path / "work" / "АЕ" / "АГПТ - Далта" / "04_Выпуск" / "АГПТ Далта - Выпуск"
    work.mkdir(parents=True)
    (work / "Альбом.pdf").write_bytes(b"%PDF album")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("BROWSE_ROOTS", str(tmp_path / "work"))
    monkeypatch.delenv("CONFIG_FILE", raising=False)
    monkeypatch.delenv("COMMITTEES_FILE", raising=False)
    cfg = load_config(tmp_path / "config.yaml")
    cfg.admin_password = "a"
    app = create_app(cfg)
    with TestClient(app) as c:
        assert c.post("/api/login", data={"password": "a"}).status_code == 200
        c.work = tmp_path / "work"
        yield c


def test_manual_project_release_and_customer(client):
    folder = client.work / "АЕ" / "АГПТ - Далта"
    r = client.post("/api/admin/projects", json={"project_type": "АГПТ", "project_name": "Далта", "pc_path": str(folder)})
    assert r.status_code == 200, r.text
    p = next(x for x in r.json()["projects"] if x["folder"] == "АГПТ - Далта")
    assert p["manual"] and not p["stages"] and p["required"] == ["Заказчик"]
    assert client.post("/api/admin/projects", json={"project_type": "ДПТ", "project_name": "X"}).status_code == 400

    rel = folder / "04_Выпуск" / "АГПТ Далта - Выпуск"
    pv = client.get("/api/admin/releases/preview", params={"path": str(rel)}).json()
    assert (pv["project_type"], pv["project_name"], pv["stage"]) == ("АГПТ", "Далта", None)
    assert pv["addressees"] == ["Заказчик"] and pv["released_at"]

    body = {k: pv[k] for k in ("title", "released_at", "project_type", "project_name", "project_folder", "stage", "label")}
    job = client.post("/api/admin/releases", json={**body, "path": str(rel), "addressees": ["Заказчик", "КС"]}).json()
    for _ in range(100):
        if client.get(f"/api/admin/releases/jobs/{job['id']}").json()["state"] != "copying":
            break
        time.sleep(0.05)
    sendings = {s["org_code"]: s for s in client.get("/api/sendings").json()["sendings"]}
    assert sendings["Заказчик"]["due"] is None and not sendings["Заказчик"]["overdue"]   # no term
    assert sendings["КС"]["due"]                                                         # committee: 30 days

    # archive: out of the lists
    client.patch("/api/admin/projects/" + "АГПТ - Далта", json={"archived": True})
    data = client.get("/api/sendings").json()
    assert "АГПТ - Далта" in data["archived"]
    assert all(x["folder"] != "АГПТ - Далта" for x in data["projects"])


def test_customer_term_can_be_set(client):
    r = client.patch("/api/admin/committees/" + "Заказчик", json={"term_days": 14})
    assert next(c for c in r.json()["committees"] if c["code"] == "Заказчик")["term"] == 14
    r = client.patch("/api/admin/committees/" + "КТ", json={"term_days": 0})
    assert next(c for c in r.json()["committees"] if c["code"] == "КТ")["term"] is None


def test_release_edit_updates_sendings(client):
    folder = client.work / "АЕ" / "АГПТ - Далта"
    client.post("/api/admin/projects", json={"project_type": "АГПТ", "project_name": "Далта", "pc_path": str(folder)})
    rel = folder / "04_Выпуск" / "АГПТ Далта - Выпуск"
    pv = client.get("/api/admin/releases/preview", params={"path": str(rel)}).json()
    body = {k: pv[k] for k in ("title", "released_at", "project_type", "project_name", "project_folder", "stage", "label")}
    job = client.post("/api/admin/releases", json={**body, "path": str(rel), "addressees": ["КЭИО"]}).json()
    for _ in range(100):
        if client.get(f"/api/admin/releases/jobs/{job['id']}").json()["state"] != "copying":
            break
        time.sleep(0.05)
    rid = job["release_id"]
    s = client.get("/api/sendings").json()["sendings"]
    keio = next(x for x in s if x["org_code"] == "КЭИО")
    client.patch(f"/api/admin/sendings/{keio['id']}", json={"note": "проверка"})
    # wrong addressee fixed in the release: КЭИО → КТ, КРТИ, and the date moved
    client.patch(f"/api/admin/releases/{rid}", json={"addressees": ["КТ", "КРТИ"], "released_at": "2026-10-25T14:08"})
    s = client.get(f"/api/releases/{rid}/sendings").json()["sendings"]
    assert sorted(x["org_code"] for x in s) == ["КРТИ", "КТ"]
    assert {x["sent_at"] for x in s} == {"2026-10-25"}
    # deleting the release takes its sendings away from «Сроки» (12.11)
    assert client.delete(f"/api/admin/releases/{rid}").status_code == 200
    assert not [x for x in client.get("/api/sendings").json()["sendings"] if x.get("release_id") == rid or x["org_code"] in ("КТ", "КРТИ")]
