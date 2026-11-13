"""Remarks: a sheet from a letter, answers by «Исполнитель», pictures, Word with and without notes."""

import io
import os
import shutil
import struct
import zlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.remarks as remarks_mod
from app.config import load_config
from app.server import create_app

ROOT = Path(__file__).resolve().parent.parent
# real letters on the owner's PC: GRADDOK_LETTERS = the folder with «ППТЛ - Далта» (the tests are skipped without it)
LETTERS = Path(os.environ.get("GRADDOK_LETTERS", "")) if os.environ.get("GRADDOK_LETTERS") else None
IOGV = Path("08_Согласования") / "02_Согласования ИОГВ"
REAL = LETTERS / "ППТЛ - Далта" / "ППТЛ - Магистраль 3" / IOGV / "Замечания КТ М3_26.pdf" if LETTERS else Path("-")


def png() -> bytes:
    raw = b"\x00\xff\x00\x00"
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


@pytest.fixture()
def env(tmp_path, monkeypatch):
    shutil.copy(ROOT / "config.example.yaml", tmp_path / "config.yaml")
    shutil.copy(ROOT / "committees.yaml", tmp_path / "committees.yaml")
    block = tmp_path / "work" / "ППТЛ - Магистраль 3" / "08_Согласования" / "02_Согласования ИОГВ"
    block.mkdir(parents=True)
    (block / "Замечания КТ М3_26.pdf").write_bytes(b"%PDF remarks")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("CONFIG_FILE", raising=False)
    monkeypatch.delenv("COMMITTEES_FILE", raising=False)
    monkeypatch.setattr(remarks_mod, "parse_letter",
                        lambda path: {"text_ok": True, "items": ["Нет расчёта.", "Рисунок нечитаем."], "number": ""})
    cfg = load_config(tmp_path / "config.yaml")
    cfg.admin_password = "a"
    app = create_app(cfg)
    with TestClient(app) as admin:
        assert admin.post("/api/login", data={"password": "a"}).status_code == 200
        admin.post("/api/admin/sources", json={"path": str(tmp_path / "work")})
        admin.post("/api/admin/publish")
        admin.post("/api/admin/users", json={"login": "ivan@mail.ru", "name": "Иван", "role": "editor", "password": "long-enough"})
        admin.post("/api/admin/users", json={"login": "olga@mail.ru", "name": "Ольга", "role": "viewer", "password": "long-enough"})
        yield admin, app


def test_sheet_answers_pictures_word(env):
    admin, app = env
    doc = next(d for d in admin.get("/api/documents").json()["documents"] if d["decision_id"] == "remarks")
    with TestClient(app) as ivan, TestClient(app) as olga:
        ivan.post("/api/login", data={"login": "ivan@mail.ru", "password": "long-enough"})
        olga.post("/api/login", data={"login": "olga@mail.ru", "password": "long-enough"})
        assert olga.post("/api/remarks", json={"doc_id": doc["id"]}).status_code == 403   # viewer reads only
        r = ivan.post("/api/remarks", json={"doc_id": doc["id"]}).json()
        sid = r["id"]
        assert r["created"] and r["count"] == 2
        assert ivan.post("/api/remarks", json={"doc_id": doc["id"]}).json() == {"id": sid, "created": False}

        sheet = olga.get(f"/api/remarks/{sid}").json()
        assert not sheet["can_edit"] and [i["remark"] for i in sheet["items"]] == ["Нет расчёта.", "Рисунок нечитаем."]

        pic = ivan.post("/api/remarks-images", files={"file": ("s.png", png(), "image/png")}).json()
        assert pic["type"] == "image/png"
        assert ivan.post("/api/remarks-images", files={"file": ("x.html", b"<html>", "text/html")}).status_code == 400
        items = sheet["items"]
        items[0].update(answer="Расчёт добавлен.", status="done", answer_images=[pic])
        # the note as text and a picture in order (13.11): «см. лист 3», the picture, «и далее»
        items[1].update(answer="Рисунок заменён.", status="disagree",
                        note_blocks=[{"t": "text", "v": "см. лист 3"}, {"t": "img", **pic}, {"t": "text", "v": "и далее"}])
        v = ivan.put(f"/api/remarks/{sid}", json={"version": sheet["sheet"]["updated_at"], "letter_no": "01-19-1/26",
                                                   "items": items}).json()["version"]
        # someone else saved meanwhile: the old version is refused
        assert ivan.put(f"/api/remarks/{sid}", json={"version": sheet["sheet"]["updated_at"], "items": items}).status_code == 409
        assert olga.put(f"/api/remarks/{sid}", json={"version": v, "items": items}).status_code == 403
        assert olga.get(f"/remarks-image/{pic['sha']}").content == png()

        docs = olga.get("/api/documents").json()["documents"]
        assert next(d for d in docs if d["id"] == doc["id"])["remarks"] == {"sheet_id": sid, "total": 2, "done": 2}

        from docx import Document
        full = Document(io.BytesIO(olga.get(f"/remarks/{sid}/word?notes=1").content))
        t = full.tables[0]
        assert [c.text for c in t.rows[0].cells] == ["№", "Замечание", "Ответ", "Примечание"]
        assert t.rows[1].cells[2].text.startswith("Расчёт добавлен.") and len(full.inline_shapes) == 2
        # the picture inside the file has an extension: without it Word shows «не удаётся отобразить рисунок»
        import zipfile
        media = [n for n in zipfile.ZipFile(io.BytesIO(olga.get(f"/remarks/{sid}/word?notes=1").content)).namelist() if "/media/" in n]
        assert media and all(n.endswith(".png") for n in media), media
        note_cell = t.rows[2].cells[3]
        assert [p.text for p in note_cell.paragraphs] == ["см. лист 3", "", "и далее"]       # the picture between
        assert len(note_cell._element.xpath(".//pic:pic")) == 1
        saved = olga.get(f"/api/remarks/{sid}").json()["items"][1]
        assert saved["note"] == "см. лист 3" + chr(10) + "и далее" and saved["note_images"] == [pic]
        assert [b["t"] for b in saved["note_blocks"]] == ["text", "img", "text"]
        short = Document(io.BytesIO(olga.get(f"/remarks/{sid}/word?notes=0").content))
        assert [c.text for c in short.tables[0].rows[0].cells] == ["№", "Замечание", "Ответ"]

        assert ivan.delete(f"/api/remarks/{sid}").status_code == 403    # only the admin deletes a sheet
        assert admin.delete(f"/api/remarks/{sid}").status_code == 200


@pytest.mark.skipif(not REAL.is_file(), reason="the real letter is only on the owner's PC")
def test_parse_real_letter():
    from importlib import reload
    mod = reload(remarks_mod)
    out = mod.parse_letter(REAL)
    assert out["text_ok"] and len(out["items"]) >= 10 and out["number"] == ""
    assert not any(" е " in x for x in out["items"])


def test_repeats_and_round(tmp_path, monkeypatch):
    shutil.copy(ROOT / "config.example.yaml", tmp_path / "config.yaml")
    shutil.copy(ROOT / "committees.yaml", tmp_path / "committees.yaml")
    block = tmp_path / "work" / "ППТЛ - Магистраль 3" / "08_Согласования" / "02_Согласования ИОГВ"
    block.mkdir(parents=True)
    (block / "Замечания КТ М3 20261021.pdf").write_bytes(b"%PDF first")
    (block / "Замечания КТ М3 20261201.pdf").write_bytes(b"%PDF second")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("CONFIG_FILE", raising=False)
    monkeypatch.delenv("COMMITTEES_FILE", raising=False)
    texts = {
        "20261021": ["В расчетах транспортных потоков отсутствует уровень загрузки Киевского шоссе.", "Рисунок 2 нечитаем."],
        "20261201": ["В расчётах транспортных потоков отсутствует уровень загрузки Киевского шоссе!", "Совсем новое замечание про остановки."],
    }
    cfg = load_config(tmp_path / "config.yaml")
    cfg.admin_password = "a"
    app = create_app(cfg)
    with TestClient(app) as c:
        c.post("/api/login", data={"password": "a"})
        c.post("/api/admin/sources", json={"path": str(tmp_path / "work")})
        c.post("/api/admin/publish")
        docs = sorted((d for d in c.get("/api/documents").json()["documents"] if d["decision_id"] == "remarks"),
                      key=lambda d: d["doc_date"])
        assert [d["doc_date"] for d in docs] == ["2026-10-21", "2026-12-01"]
        # the parser stub gives the text of the letter being parsed
        monkeypatch.setattr(remarks_mod, "parse_letter", lambda path: {"text_ok": True, "number": "", "items": texts[current[0]]})
        current = ["20261021"]
        first = c.post("/api/remarks", json={"doc_id": docs[0]["id"]}).json()
        sheet = c.get(f"/api/remarks/{first['id']}").json()
        items = sheet["items"]
        items[0].update(answer="Уровень загрузки добавлен в раздел 4.", status="done")
        c.put(f"/api/remarks/{first['id']}", json={"version": sheet["sheet"]["updated_at"], "letter_no": "01-19-1/26", "items": items})

        current[0] = "20261201"
        second = c.post("/api/remarks", json={"doc_id": docs[1]["id"]}).json()
        assert second["repeats"] == 1
        got = c.get(f"/api/remarks/{second['id']}").json()["items"]
        assert got[0]["repeat"]["sheet_id"] == first["id"] and got[0]["status"] == "work"
        assert got[0]["answer"] == "Уровень загрузки добавлен в раздел 4."
        assert got[0]["note"] == "Повторное. См. письмо № 01-19-1/26 от 21-10-2026"
        assert got[1]["repeat"] is None and got[1]["note"] == ""
        # the mark survives a save from the editor
        sh = c.get(f"/api/remarks/{second['id']}").json()
        c.put(f"/api/remarks/{second['id']}", json={"version": sh["sheet"]["updated_at"], "items": sh["items"]})
        assert c.get(f"/api/remarks/{second['id']}").json()["items"][0]["repeat"]["similarity"] >= 0.8
        # «Сроки» know the sheet of the answer letter
        assert str(docs[0]["id"]) in c.get("/api/sendings").json()["remarks"]
        # the summary table downloads in both views, named by the common pattern
        for mode, notes in (("combined", 1), ("combined", 0), ("chrono", 1)):
            r = c.get(f"/remarks-summary/word?ids={first['id']},{second['id']}&mode={mode}&notes={notes}")
            assert r.status_code == 200 and r.content[:2] == b"PK", (mode, notes, r.text[:200])
            assert "%D0%9C%D0%B0%D0%B3%D0%B8%D1%81%D1%82%D1%80%D0%B0%D0%BB%D1%8C" in r.headers["content-disposition"]  # «Магистраль»
        r = c.get(f"/remarks/{first['id']}/word?notes=0")
        assert r.status_code == 200 and r.content[:2] == b"PK"


KR = LETTERS / "ППТЛ - Далта" / "ППТЛ - Красносельское шоссе" / IOGV / "Замечания КТ Кр_26.pdf" if LETTERS else Path("-")


@pytest.mark.skipif(not KR.is_file(), reason="the real letter is only on the owner's PC")
def test_parse_appendix_from_the_start():
    """Remarks in an appendix start after its heading, not at a sub-heading in the middle (13.11)."""
    from importlib import reload
    out = reload(remarks_mod).parse_letter(KR)
    assert out["items"][0].startswith("Прямые участки магистрали")
    # a dot inside «…» (a standard's title) does not cut the remark
    assert not any(x.rstrip().endswith("населенных пунктов.") for x in out["items"])


def test_renamed_letter_keeps_sheet_and_link(env, tmp_path):
    """Audit 13.11: a letter renamed on the PC gets a new id; its sheet and share link follow it."""
    admin, app = env
    doc = next(d for d in admin.get("/api/documents").json()["documents"] if d["decision_id"] == "remarks")
    sid = admin.post("/api/remarks", json={"doc_id": doc["id"]}).json()["id"]
    share = admin.post("/api/admin/shares", json={"kind": "doc", "id": doc["id"]}).json()
    block = tmp_path / "work" / "ППТЛ - Магистраль 3" / "08_Согласования" / "02_Согласования ИОГВ"
    (block / "Замечания КТ М3_26.pdf").rename(block / "Замечания КТ М3_26 исх.pdf")
    admin.post("/api/admin/publish")
    new = next(d for d in admin.get("/api/documents").json()["documents"] if d["decision_id"] == "remarks")
    assert new["id"] != doc["id"] and new["remarks"]["sheet_id"] == sid
    assert admin.get(f"/api/remarks/{sid}").json()["doc"]["file_name"] == "Замечания КТ М3_26 исх.pdf"
    guest = TestClient(app)          # no lifespan: the app is already running for «admin»
    assert guest.get(f"/s/{share['token']}/doc").status_code == 200
