"""«Распоряжение КГА»: found in 02_Исходные данные/01_КГА, number from the name, valid-until kept, in /api/sendings."""

import shutil
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import load_config
from app.server import create_app

ROOT = Path(__file__).resolve().parent.parent


def test_orders(tmp_path, monkeypatch):
    shutil.copy(ROOT / "config.example.yaml", tmp_path / "config.yaml")
    shutil.copy(ROOT / "committees.yaml", tmp_path / "committees.yaml")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("CONFIG_FILE", raising=False)
    monkeypatch.delenv("COMMITTEES_FILE", raising=False)
    proj = tmp_path / "work" / "ППТЛ - Далта" / "ППТЛ - Киевское шоссе"
    kga = proj / "02_Исходные данные" / "01_КГА"
    (kga / "Архив").mkdir(parents=True)
    (kga / "01-20-2-185_26.pdf").write_bytes(b"%PDF order")
    (kga / "Архив" / "01-19-139.pdf").write_bytes(b"%PDF old order")
    iogv = proj / "08_Согласования" / "02_Согласования ИОГВ"
    iogv.mkdir(parents=True)
    (iogv / "Согласование КТ.pdf").write_bytes(b"%PDF ok")
    cfg = load_config(tmp_path / "config.yaml")
    cfg.admin_password = "a"
    app = create_app(cfg)
    with TestClient(app) as c:
        assert c.post("/api/login", data={"password": "a"}).status_code == 200
        assert c.post("/api/admin/sources", json={"path": str(proj.parent)}).status_code == 200
        assert c.post("/api/admin/publish").status_code == 200
        docs = c.get("/api/documents").json()["documents"]
        orders = [d for d in docs if d["decision_id"] == "order"]
        assert [(d["file_name"], d["org_code"], d["note"], d["group_id"]) for d in orders] == [
            ("01-20-2-185_26.pdf", "КГА", "№ 01-20-2-185/26", "order")]
        doc_id = orders[0]["id"]
        assert c.patch(f"/api/admin/documents/{doc_id}", json={"valid_until": "31.12.2027"}).status_code == 400
        r = c.patch(f"/api/admin/documents/{doc_id}", json={"valid_until": "2027-12-31"})
        assert r.status_code == 200 and r.json()["valid_until"] == "2027-12-31"
        # a new publication keeps the date
        assert c.post("/api/admin/publish").status_code == 200
        again = next(d for d in c.get("/api/documents").json()["documents"] if d["id"] == doc_id)
        assert again["valid_until"] == "2027-12-31"
        o = c.get("/api/sendings").json()["orders"]
        assert o == [{"project_type": again["project_type"], "project_name": again["project_name"],
                      "number": "01-20-2-185/26", "file_name": "01-20-2-185_26.pdf", "doc_id": doc_id,
                      "valid_until": "2027-12-31"}]
