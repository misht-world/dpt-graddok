"""Documents uploaded by hand: survive scans, merge when the same file is found on the PC."""

from app.store import Store
from app.uploads import similar, suggested_name


def scanned(path, sha, **kw):
    return {"abs_path": path, "project_key": "1:ДПТ - Далта", "project_type": "ДПТ", "project_name": "Далта",
            "stage": "2", "decision_kind": "positive", "org_code": "КТ", "file_name": path.rsplit("/", 1)[-1],
            "size": 1, "mtime": "2026-11-10T10:00:00", "sha256": sha, **kw}


def test_upload_survives_scan_and_merges_with_same_file(tmp_path):
    st = Store(tmp_path / "i.db")
    st.publish([scanned("/pc/a.pdf", "a")], "fp1")
    up_id = st.add_upload(scanned("upload:x/Согласование КТ.pdf", "same"))
    st.set_manual_new(up_id, False)

    stats = st.publish([scanned("/pc/a.pdf", "a")], "fp2")          # file not on the PC yet
    assert st.document(up_id)["present"] == 1 and stats["files_removed"] == 0

    stats = st.publish([scanned("/pc/a.pdf", "a"), scanned("/pc/Согл КТ.pdf", "same")], "fp3")
    assert st.document(up_id) is None                                # merged into the PC file
    [(old, new)] = stats["moved_ids"]
    merged = st.document(new)
    assert old == up_id and merged["abs_path"] == "/pc/Согл КТ.pdf" and merged["uploaded_at"] is None
    assert merged["manual_new"] == 0                                 # manual marks follow


def test_similar_and_suggested_name():
    up = {"id": 9, "abs_path": "upload:x/u.pdf", "project_key": "p", "org_code": "КТ", "decision_kind": "positive",
          "stage": "2", "doc_date": "2026-11-10"}
    rows = [up,
            {"id": 1, "abs_path": "/pc/1.pdf", "project_key": "p", "org_code": "КТ", "decision_kind": "positive",
             "stage": "2", "doc_date": "2026-11-12", "file_name": "1.pdf"},
            {"id": 2, "abs_path": "/pc/2.pdf", "project_key": "p", "org_code": "КТ", "decision_kind": "positive",
             "stage": "2", "doc_date": "2026-06-01", "file_name": "2.pdf"},
            {"id": 3, "abs_path": "/pc/3.pdf", "project_key": "p", "org_code": "КБ", "decision_kind": "positive",
             "stage": "2", "doc_date": "2026-11-10", "file_name": "3.pdf"}]
    assert [x["id"] for x in similar(up, rows)] == [1]
    assert suggested_name("Согласование", "КТ") == "Согласование КТ.pdf"
    assert suggested_name(None, "КТ") is None


def test_suggested_name_drops_numbers_and_pc_path(monkeypatch):
    from app.uploads import pc_path
    assert suggested_name("Согласование", "КТ", "22239 223") == "Согласование КТ.pdf"
    assert suggested_name("Отказ", "КБ", "по озеленению") == "Отказ КБ по озеленению.pdf"
    monkeypatch.setenv("PC_PATHS", "/sources/work=D:/Projects")
    assert pc_path("/sources/work/АЕ/ДПТ - Далта/08") == r"D:\Projects\АЕ\ДПТ - Далта\08"
    assert pc_path("/other/x") == "/other/x"


def test_remove_source_keeps_documents(tmp_path):
    st = Store(tmp_path / "i.db")
    src = st.add_source(str(tmp_path / "pc"))
    st.publish([{**scanned("/pc/a.pdf", "a"), "source_id": src["id"]}], "fp1")
    doc = st.all_by_path()["/pc/a.pdf"]
    st.set_manual_new(doc["id"], False)
    st.remove_source(src["id"])
    gone = st.document_any(doc["id"])
    assert gone["present"] == 0 and gone["manual_new"] == 0      # kept, just not shown
    src2 = st.add_source(str(tmp_path / "pc"))
    st.publish([{**scanned("/pc/a.pdf", "a"), "source_id": src2["id"]}], "fp2")
    back = st.document_any(doc["id"])
    assert back["present"] == 1 and back["manual_new"] == 0 and back["status"] == "same"
