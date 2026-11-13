"""Approvals uploaded by hand (e.g. from a phone when the PC is off).

The file goes straight to the server's copy store and appears in the catalog marked
«только на сервере» until the same file (byte for byte) is found on the PC — then the
scan merges the two (see Store.publish: it is treated as a move). A different file
that looks like the same approval (same project, stage, committee, decision, close
date) is only marked as a possible duplicate; the admin decides.
"""

from __future__ import annotations

import os
import re
from types import SimpleNamespace
import uuid
from datetime import date, datetime
from pathlib import Path, PurePosixPath

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile

from .parsing import build_org_matchers, normalize, parse_name
from .store import UPLOAD_PREFIX

MAX_UPLOAD = 200 * 1024 * 1024
# An incoming letter without a decision (customer's letter, a request…): kept, not an answer in «Сроки».
LETTER = SimpleNamespace(id="letter", label="Письмо", kind="info")
SIMILAR_DAYS = 14
_BAD_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')


def is_upload(row: dict) -> bool:
    return str(row.get("abs_path") or "").startswith(UPLOAD_PREFIX)


def _day(row: dict) -> date | None:
    raw = row.get("doc_date") or (row.get("mtime") or "")[:10]
    try:
        return date.fromisoformat(raw[:10]) if raw else None
    except ValueError:
        return None


def similar(upload: dict, rows: list[dict]) -> list[dict]:
    """Documents from the PC that look like the same approval as an upload."""
    d = _day(upload)
    out = []
    for r in rows:
        if is_upload(r) or r["id"] == upload["id"] or r.get("hidden"):
            continue
        if (r.get("project_key"), r.get("org_code"), r.get("decision_kind"), r.get("stage") or "") != \
                (upload.get("project_key"), upload.get("org_code"), upload.get("decision_kind"), upload.get("stage") or ""):
            continue
        rd = _day(r)
        if d and rd and abs((rd - d).days) > SIMILAR_DAYS:
            continue
        out.append({"id": r["id"], "file_name": r["file_name"]})
    return out


def suggested_name(decision_label: str | None, org_code: str | None, note: str = "") -> str | None:
    """«Исх-22239-223.pdf» → «Согласование КТ.pdf»: a name the catalog will parse back the same way."""
    if not decision_label or not org_code:
        return None
    # a note made only of numbers (an outgoing number «22239 223») is noise in a name
    if not re.search(r"[A-Za-zА-Яа-яЁё]{3}", note or ""):
        note = ""
    return clean_name(" ".join(x for x in (decision_label, org_code, note) if x)) + ".pdf"


def clean_name(stem: str) -> str:
    return re.sub(r"\s+", " ", _BAD_CHARS.sub(" ", stem)).strip(" .") or "Документ"


def pc_path(server_path: str) -> str:
    """/sources/work/АЕ/… → a Windows path on the PC, using PC_PATHS="/sources/work=D:/Projects;…"."""
    for pair in filter(None, os.environ.get("PC_PATHS", "").split(";")):
        prefix, _, target = pair.partition("=")
        if prefix and server_path.replace("\\", "/").startswith(prefix.rstrip("/") + "/"):
            rest = server_path.replace("\\", "/")[len(prefix.rstrip("/")) + 1:]
            return target.replace("/", "\\").rstrip("\\") + "\\" + rest.replace("/", "\\")
    return server_path


def register_upload_routes(api: FastAPI, state, require_viewer, require_admin) -> None:
    cfg = state.config

    def projects() -> list[dict]:
        """All projects that are not archived, including the ones kept by hand."""
        return [p for p in state.project_list() if not p["archived"]]

    def decision_of(decision_id: str):
        if decision_id == LETTER.id:
            return LETTER
        return next((d for d in cfg.decisions if d.id == decision_id), None)

    def folder_hint(project_key: str, stage: str | None) -> str | None:
        """Where on the PC the approvals of this project lie — to save the file there later."""
        own = next((p for p in projects() if p["project_key"] == project_key), None)
        if own and own.get("pc_path"):
            return pc_path(own["pc_path"])
        rows = [r for r in state.store.documents() if r.get("project_key") == project_key and not is_upload(r)]
        same_stage = [r for r in rows if (r.get("stage") or "") == (stage or "")] or rows
        approvals = [r for r in same_stage if r.get("decision_kind") in ("positive", "negative", "neutral")] or same_stage
        if not approvals:
            return None
        sources = {s["id"]: s for s in state.store.sources()}
        r = approvals[0]
        rel = PurePosixPath(str(r.get("rel_path") or "").replace("\\", "/")).parent
        src = sources.get(r.get("source_id")) or {}
        root = str(src.get("path") or "")
        full = root.rstrip("/\\") + ("/" + str(rel) if str(rel) != "." else "")
        return pc_path(full)

    @api.post("/api/admin/upload/parse")
    def parse(payload: dict, role: str = Depends(require_admin)):
        """Fields read from the file name, a cleaner name and the lists for the form."""
        name = str(payload.get("file_name") or "")
        stem = name[:-4] if name.lower().endswith(".pdf") else name
        p = parse_name(stem, cfg, build_org_matchers(cfg))
        return {
            "decision_id": p.decision_id, "org_code": p.org.code if p.org else None,
            "stage": p.stage, "doc_date": p.doc_date.isoformat() if p.doc_date else None,
            "note": p.note, "suggested": suggested_name(p.decision_label, p.org.code if p.org else None, p.note),
            "projects": [{"project_key": x["project_key"], "project_type": x["project_type"],
                          "project_name": x["project_name"], "stages": x["stages"], "manual": x["manual"]}
                         for x in projects()],
            "decisions": [{"id": d.id, "label": d.label, "kind": d.kind} for d in [*cfg.decisions, LETTER]],
            "committees": [{"code": c.code, "name": c.name, "active": c.active} for c in cfg.committees],
        }

    @api.get("/api/admin/upload/hint")
    def hint(project_key: str, stage: str = "", role: str = Depends(require_admin)):
        return {"folder": folder_hint(project_key, stage or None)}

    @api.post("/api/admin/upload")
    async def upload(
        file: UploadFile = File(...),
        project_key: str = Form(...),
        file_name: str = Form(...),
        decision_id: str = Form(""),
        org_code: str = Form(""),
        stage: str = Form(""),
        doc_date: str = Form(""),
        note: str = Form(""),
        role: str = Depends(require_admin),
    ):
        project = next((p for p in projects() if p["project_key"] == project_key), None)
        if not project:
            raise HTTPException(400, "Проект не найден")
        name = clean_name(file_name[:-4] if file_name.lower().endswith(".pdf") else file_name) + ".pdf"
        decision = decision_of(decision_id)
        org = next((c for c in cfg.committees if c.code == org_code), None)
        if doc_date:
            try:
                date.fromisoformat(doc_date)
            except ValueError:
                raise HTTPException(400, "Неверная дата документа") from None

        tmp = cfg.data_dir / f".upload-{uuid.uuid4().hex}"
        size = 0
        try:
            with open(tmp, "wb") as out:
                head = b""
                while chunk := await file.read(1 << 20):
                    if not head:
                        head = chunk[:5]
                    size += len(chunk)
                    if size > MAX_UPLOAD:
                        raise HTTPException(400, "Файл больше 200 МБ")
                    out.write(chunk)
            if head != b"%PDF-":
                raise HTTPException(400, "Это не PDF")
            sha, size, _new = state.blobs.put(tmp)
        finally:
            tmp.unlink(missing_ok=True)

        # the catalog group of approvals («matrix» group) and an existing row of the project for its fields
        group = next((g for g in cfg.enabled_groups if g.matrix), None) or (cfg.enabled_groups or [None])[0]
        doc = {
            "abs_path": f"{UPLOAD_PREFIX}{uuid.uuid4().hex}/{name}",
            "source_id": None, "rel_path": None,
            "project_key": project["project_key"], "project_type": project["project_type"],
            "project_name": project["project_name"],
            "group_id": group.id if group else None, "group_title": group.title if group else None,
            "stage": stage.strip() or None,
            "decision_id": decision.id if decision else None,
            "decision_label": decision.label if decision else None,
            "decision_kind": decision.kind if decision else "unknown",
            "org_code": org.code if org else None, "org_name": org.name if org else None,
            "org_detail": None, "org_parent": org.parent if org else None,
            "note": normalize(note) or None, "file_name": name, "subfolder": "загружено вручную",
            "size": size, "mtime": datetime.now().isoformat(timespec="seconds"),
            "doc_date": doc_date or None, "sha256": sha,
        }
        doc_id = state.store.add_upload(doc)
        return {"id": doc_id, "folder": folder_hint(project["project_key"], doc["stage"])}

    @api.post("/api/admin/uploads/{doc_id}/merge")
    def merge(doc_id: int, payload: dict, role: str = Depends(require_admin)):
        """«Это тот же документ»: keep the PC file, move links to it, drop the upload."""
        up = state.store.document(doc_id)
        target = state.store.document(int(payload.get("target_id") or 0))
        if not up or not is_upload(up) or not target or is_upload(target):
            raise HTTPException(400, "Нечего объединять")
        if getattr(state, "sendings", None):
            state.remap_document(up["id"], target["id"])
        state.store.delete_document(up["id"])
        state.purge_blob(up.get("sha256"))
        return {"ok": True}

    @api.post("/api/admin/uploads/{doc_id}/distinct")
    def distinct(doc_id: int, role: str = Depends(require_admin)):
        """«Разные документы»: stop marking the upload as a possible duplicate."""
        up = state.store.document(doc_id)
        if not up or not is_upload(up):
            raise HTTPException(404, "Документ не найден")
        state.store.set_similar_ok(doc_id)
        return {"ok": True}


def decorate(rows: list[dict]) -> None:
    """Adds «uploaded» and «similar» (possible duplicates on the PC) to catalog rows, before secrets are removed."""
    for r in rows:
        r["uploaded"] = is_upload(r)
    for r in rows:
        r["similar"] = similar(r, rows) if r["uploaded"] and not r.get("similar_ok") else []
