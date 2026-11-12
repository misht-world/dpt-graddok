"""Catalog documents are copied to the server; deleting removes the copy unless still needed."""

from pathlib import Path

from app.config import load_config
from app.server import App

ROOT = Path(__file__).resolve().parent.parent


def make_app(tmp_path, monkeypatch):
    import shutil
    # a copy: the app may write to committees.yaml next to its config
    shutil.copy(ROOT / "config.example.yaml", tmp_path / "config.yaml")
    shutil.copy(ROOT / "committees.yaml", tmp_path / "committees.yaml")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("CONFIG_FILE", raising=False)
    monkeypatch.delenv("COMMITTEES_FILE", raising=False)
    return App(load_config(tmp_path / "config.yaml"))


def doc(path: Path) -> dict:
    st = path.stat()
    return {"abs_path": str(path), "size": st.st_size, "mtime": str(int(st.st_mtime))}


def test_copy_reuse_and_purge(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)
    pc = tmp_path / "pc"
    pc.mkdir()
    a = pc / "Согласование КТ.pdf"
    a.write_bytes(b"%PDF approval")
    d = doc(a)
    app.copy_documents([d])
    sha = d["sha256"]
    assert app.blobs.has(sha) and app.blobs.path(sha).read_bytes() == b"%PDF approval"

    # just written: kept even though nothing refers to it yet (a scan may be about to record it)
    a.unlink()
    assert not app.purge_blob(sha) and app.blobs.has(sha)
    # later, still unreferenced → deleted
    app.blobs._fresh.clear()
    assert app.purge_blob(sha) and not app.blobs.has(sha)


def test_hidden_documents_are_not_copied(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)
    f = tmp_path / "x.pdf"
    f.write_bytes(b"secret")
    monkeypatch.setattr(app.store, "all_by_path", lambda: {str(f): {"sha256": "", "size": 0, "mtime": "", "hidden": 1}})
    d = doc(f)
    app.copy_documents([d])
    assert d["sha256"] and not app.blobs.has(d["sha256"])


def test_purge_keeps_blob_used_by_release(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)
    f = tmp_path / "y.pdf"
    f.write_bytes(b"shared")
    sha, _, _ = app.blobs.put(f)
    app.blobs._fresh.clear()
    app.releases = type("R", (), {"uses_blob": lambda self, s: s == sha})()
    assert not app.purge_blob(sha) and app.blobs.has(sha)


def test_fill_copies_for_indexed_documents(tmp_path, monkeypatch):
    from app.scanner import sha256
    app = make_app(tmp_path, monkeypatch)
    f = tmp_path / "z.pdf"
    f.write_bytes(b"indexed before copies existed")
    sha = sha256(f)
    monkeypatch.setattr(app.store, "documents", lambda: [{"sha256": sha, "abs_path": str(f)}])
    assert app.fill_copies() == 1 and app.blobs.has(sha)
    assert app.fill_copies() == 0


def test_publish_refused_when_pc_is_off(tmp_path, monkeypatch):
    import pytest
    from fastapi import HTTPException
    app = make_app(tmp_path, monkeypatch)
    app.store.add_source(str(tmp_path / "pc-share-not-mounted"))
    with pytest.raises(HTTPException) as e:
        app.publish("test")
    assert e.value.status_code == 409 and "недоступны" in e.value.detail


def test_login_cyrillic_and_password_change(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)
    app.config.admin_password = "пароль-админа"
    assert app.check_password("пароль-админа") == "admin"
    assert app.check_password("не тот") is None
    token = app.issue("admin")
    assert app.role_of(token) == "admin"
    app.config.admin_password = "новый"
    assert app.role_of(token) is None


def test_same_time_ignores_time_zone_notation():
    from app.server import same_time
    assert same_time("2026-09-17T09:02:05+00:00", "2026-09-17T11:02:05+02:00")
    assert not same_time("2026-09-17T09:02:05+00:00", "2026-09-17T09:02:06+00:00")
    assert not same_time(None, "2026-09-17T09:02:05+00:00")


def test_pending_changes_lists_without_touching(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)
    block = tmp_path / "work" / "ДПТ - Далта" / "08_Согласования" / "02_Согласования ИОГВ"
    block.mkdir(parents=True)
    (block / "Согласование КТ.pdf").write_bytes(b"%PDF one")
    app.store.add_source(str(tmp_path / "work"))
    app.publish("first")
    assert app.pending_changes()["items"] == []
    (block / "Согласование КС.pdf").write_bytes(b"%PDF two")
    (block / "Согласование КТ.pdf").unlink()
    items = app.pending_changes()["items"]
    assert [(i["kind"], i["path"].split("/")[-1]) for i in items] == [
        ("new", "Согласование КС.pdf"), ("gone", "Согласование КТ.pdf")]
    assert app.store.latest_publication()["files_total"] == 1   # nothing published


def test_archive_folders_and_plain_negative(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)
    block = tmp_path / "work" / "ППТЛ - Красносельское шоссе" / "08_Согласования" / "02_Согласования ИОГВ" / "00_КТ"
    (block / "Архив").mkdir(parents=True)
    (block / "Отрицательное КТ Красносельское.pdf").write_bytes(b"%PDF no")
    (block / "Архив" / "Согласование КТ старое.pdf").write_bytes(b"%PDF old")
    app.store.add_source(str(tmp_path / "work"))
    app.publish("first")
    docs = app.store.documents()
    assert [(d["file_name"], d["decision_id"], d["org_code"]) for d in docs] == [
        ("Отрицательное КТ Красносельское.pdf", "refusal", "КТ")]
