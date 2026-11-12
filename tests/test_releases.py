"""Release parsing, blob store and streaming zip."""

import io
import zipfile
from pathlib import Path

import pytest

from app.config import load_config
from app.releases import (
    BlobStore, ReleaseStore, Registrar, find_stage, list_release_files, parse_snapshot_name, stream_zip, suggest,
)

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("CONFIG_FILE", raising=False)
    return load_config(ROOT / "config.example.yaml")


def test_snapshot_name():
    s = parse_snapshot_name("2026-02-16_01-22-30_КЭИО_КТ")
    assert s.when.isoformat() == "2026-02-16T01:22:30"
    assert s.label == "КЭИО_КТ"
    assert parse_snapshot_name("2026-02-19_10-46-41_КТ КРТИ_2").label == "КТ КРТИ_2"
    assert parse_snapshot_name("09_Выпуск") is None


@pytest.mark.parametrize("text, stage", [
    ("Этап 02 - 01_ИОГВ", "2"), ("Этап 1", "1"), ("2 этап", "2"), ("ДПТ-Далта_Эт2_ИОГВ", "2"), ("09_Выпуск", None),
])
def test_stage(text, stage):
    assert find_stage([text]) == stage


def test_suggest_real_layout(cfg):
    p = Path("/sources/backup/ДПТ - Далта/2026-11-05_13-10-20_КГА/09_Выпуск/Этап 02 - 02_КГА/ДПТ-Далта_Эт2_КГА")
    s = suggest(p, cfg)
    assert s.released_at == "2026-11-05T13:10"
    assert (s.project_type, s.project_name, s.stage) == ("ДПТ", "Далта", "2")
    assert s.addressees == ["КГА"]
    assert s.title == "ДПТ-Далта_Эт2_КГА"


def test_suggest_two_committees_and_copy_suffix(cfg):
    p = Path("/b/ДПТ - Далта/2026-02-19_10-46-41_КТ КРТИ_2/09_Выпуск/Этап 1/x")
    s = suggest(p, cfg)
    assert s.addressees == ["КТ", "КРТИ"]
    assert s.label == "КТ КРТИ"


def test_suggest_pptl_subproject_and_iogv(cfg):
    p = Path("/b/ППТЛ - Далта/2026-03-01_10-00-00_ИОГВ/ППТЛ - Магистраль 1/09_Выпуск/М1")
    s = suggest(p, cfg, {"ППТЛ - Магистраль 1": ["КТ", "КБ"]})
    assert (s.project_type, s.project_name) == ("ППТЛ", "Магистраль 1")
    assert s.iogv and s.addressees == ["КТ", "КБ"]


def test_files_skip_zip_and_sig(tmp_path):
    (tmp_path / "a").mkdir()
    for name in ("a/1.pdf", "2.zip", "2.zip.sig", "3.pdf.sig", "~$x.docx", "4.dwg", "a/4.bak", "a/4.dwl",
                 "a/4.dwl2", "Thumbs.db", "plot.log", "a/Лист_plot.log", "x.tmp", "5.xlsx"):
        (tmp_path / name).write_bytes(b"x")
    names = sorted(p.relative_to(tmp_path).as_posix() for p in list_release_files(tmp_path))
    assert names == ["4.dwg", "5.xlsx", "a/1.pdf"]


def test_blob_store_dedup(tmp_path):
    blobs = BlobStore(tmp_path / "blobs")
    f1, f2 = tmp_path / "1.pdf", tmp_path / "2.pdf"
    f1.write_bytes(b"same"); f2.write_bytes(b"same")
    sha1, size, new1 = blobs.put(f1)
    sha2, _, new2 = blobs.put(f2)
    assert sha1 == sha2 and size == 4 and new1 and not new2
    assert blobs.path(sha1).read_bytes() == b"same"
    assert not list((tmp_path / "blobs").glob(".tmp-*"))


def test_registrar_copies_release(tmp_path):
    src = tmp_path / "rel"
    (src / "DWG").mkdir(parents=True)
    (src / "DWG" / "a.dwg").write_bytes(b"dwg")
    (src / "letter.pdf").write_bytes(b"pdf")
    (src / "big.zip").write_bytes(b"zip")
    store = ReleaseStore(tmp_path / "r.db")
    reg = Registrar(store, BlobStore(tmp_path / "blobs"))
    job_id = reg.start(src, {"title": "R1"}, ["КТ"])
    import time
    for _ in range(100):
        if reg.job(job_id)["state"] != "copying":
            break
        time.sleep(0.02)
    rid = reg.job(job_id)["release_id"]
    r = store.release(rid)
    assert r["status"] == "ready" and r["files_count"] == 2 and r["addressees"] == ["КТ"]
    assert [f["rel_path"] for f in store.files(rid)] == ["DWG/a.dwg", "letter.pdf"]


def test_stream_zip_roundtrip(tmp_path):
    a, b = tmp_path / "a.pdf", tmp_path / "b.txt"
    a.write_bytes(b"A" * 3_000_000)
    b.write_bytes("текст".encode())
    data = b"".join(stream_zip([("R/папка/a.pdf", a, "2026-11-05T13:10:20"), ("R/b.txt", b, None)], chunk=1 << 16))
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        assert zf.testzip() is None
        assert zf.read("R/папка/a.pdf") == b"A" * 3_000_000
        assert zf.read("R/b.txt").decode() == "текст"


def test_nested_zip_for_folders(tmp_path):
    from app.releases import nest_folders
    a, b, c = tmp_path / "a.pdf", tmp_path / "b.dwg", tmp_path / "c.dwg"
    a.write_bytes(b"letter"); b.write_bytes(b"B" * 200_000); c.write_bytes(b"C")
    entries = nest_folders([("a.pdf", a, None), ("DWG/b.dwg", b, "2026-11-05T12:00:00"),
                            ("DWG/sub/c.dwg", c, "2026-11-04T12:00:00")], "R")
    data = b"".join(stream_zip(entries, chunk=1 << 14))
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        assert zf.testzip() is None
        assert sorted(zf.namelist()) == ["R/DWG.zip", "R/a.pdf"]
        with zipfile.ZipFile(io.BytesIO(zf.read("R/DWG.zip"))) as inner:
            assert inner.testzip() is None
            assert inner.read("DWG/b.dwg") == b"B" * 200_000
            assert inner.read("DWG/sub/c.dwg") == b"C"


def test_compare_files():
    from app.releases import compare_files
    f = lambda p, sha: {"rel_path": p, "sha256": sha, "size": 1, "id": 0}
    old = [f("a.pdf", "1"), f("b.pdf", "2"), f("DWG/c.dwg", "3"), f("gone.pdf", "4")]
    new = [f("a.pdf", "1"), f("b.pdf", "22"), f("DWG/new/c.dwg", "3"), f("new.pdf", "5")]
    d = compare_files(old, new)
    assert d["same"] == 1
    assert [x["new"]["rel_path"] for x in d["changed"]] == ["b.pdf"]
    assert [(x["old"]["rel_path"], x["new"]["rel_path"]) for x in d["moved"]] == [("DWG/c.dwg", "DWG/new/c.dwg")]
    assert [x["rel_path"] for x in d["added"]] == ["new.pdf"]
    assert [x["rel_path"] for x in d["removed"]] == ["gone.pdf"]


def test_resume_copies_only_missing(tmp_path):
    import time
    src = tmp_path / "rel"
    src.mkdir()
    (src / "a.pdf").write_bytes(b"a")
    store = ReleaseStore(tmp_path / "r.db")
    reg = Registrar(store, BlobStore(tmp_path / "blobs"))
    rid = store.create({"title": "R", "source_path": str(src)}, [])
    store.add_files(rid, [("a.pdf", "x" * 64, 1, None)])
    store.finish(rid, "failed")
    (src / "b.pdf").write_bytes(b"b")
    job = reg.resume(rid)
    for _ in range(100):
        if reg.job(job)["state"] != "copying":
            break
        time.sleep(0.02)
    assert reg.job(job)["files"] == 1
    r = store.release(rid)
    assert r["status"] == "ready" and r["files_count"] == 2
