"""HTTP routes for releases (stage 1): register by folder, list, tree, files, zip."""

from __future__ import annotations

import mimetypes
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from fastapi import Body, Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse

from .applications import is_application, parse_application
from .config import CUSTOMER
from .sendings_api import register_sending_routes
from .releases import (
    BlobStore, Registrar, ReleaseStore, copy_tree_size, list_release_files, nest_folders, parse_snapshot_name,
    split_project, stream_zip, suggest,
)


# an application filed this many days before the snapshot is probably left over from an earlier sending
STALE_DAYS = 3


def _days_before(iso: str, days: int) -> str:
    from datetime import date, timedelta
    return (date.fromisoformat(iso[:10]) - timedelta(days=days)).isoformat()


def _ru(iso: str) -> str:
    return f"{iso[8:10]}.{iso[5:7]}.{iso[:4]}"


INLINE_TYPES = {"application/pdf", "image/png", "image/jpeg", "image/gif", "image/webp", "text/plain"}


def _disposition(kind: str, name: str) -> str:
    return f"{kind}; filename*=UTF-8''{quote(name)}"


def register_release_routes(api: FastAPI, state, require_viewer, require_admin, browse_roots) -> None:
    cfg = state.config
    store = ReleaseStore(cfg.data_dir / "releases.db")
    blobs = state.blobs

    def read_applications(release_id: int) -> list[dict]:
        """Reads every «Заявление*.pdf» in the release root (from the stored copies)."""
        apps = []
        for f in store.files(release_id):
            if is_application(f["rel_path"]) and blobs.has(f["sha256"]):
                a = parse_application(blobs.path(f["sha256"]), cfg, f["rel_path"])
                a["file_id"] = f["id"]
                apps.append(a)
        apps.sort(key=lambda a: (a.get("filed") or "", a.get("number") or ""))
        store.set_applications(release_id, apps)
        return apps

    def after_copy(release_id: int) -> None:
        read_applications(release_id)
        state.make_sendings(release_id)

    registrar = Registrar(store, blobs, after_copy)
    state.releases = store
    register_sending_routes(api, state, store, require_viewer, require_admin)
    # releases registered before stage 2 get their applications read and sendings made now
    for r in store.releases():
        if r["status"] == "ready" and not r.get("sendings_made"):
            if r["applications"] is None:
                read_applications(r["id"])
            state.make_sendings(r["id"])

    def for_role(r: dict, role: str) -> dict:
        if role != "admin":   # where the copy came from is of no use to a viewer
            r = {k: v for k, v in r.items() if k not in ("source_path", "snapshot", "label")}
        return r

    def allowed_dir(raw: str) -> Path:
        path = Path(str(raw or "").strip().strip('"')).expanduser()
        if not path.is_dir():
            raise HTTPException(400, f"Папка не найдена: {path}")
        path = path.resolve()
        roots = [Path(r).resolve() for r in browse_roots()]
        if not any(path == r or r in path.parents for r in roots):
            raise HTTPException(403, "Папка вне подключённых источников")
        return path

    def required_by_folder() -> dict[str, list[str]]:
        return state.store.requirements()

    # ------------------------------------------------------------ admin

    @api.get("/api/admin/releases/preview")
    def preview(path: str, role: str = Depends(require_admin)):
        root = allowed_dir(path)
        sug = suggest(root, cfg, required_by_folder())
        if not sug.snapshot:
            # Not a dpt-backup snapshot (e.g. a project kept by hand): the date is when the
            # release folder last changed; the project is the registered one whose folder holds it,
            # or a «TYPE - name» folder on the way.
            sug.released_at = datetime.fromtimestamp(root.stat().st_mtime).isoformat(timespec="minutes")
            sug.notes = [n for n in sug.notes if "ГГГГ-ММ-ДД" not in n]
            sug.notes.append("Это не срез dpt-backup: дата отправки взята по времени изменения папки — проверьте.")
            known = [x for x in state.project_list() if x.get("pc_path")]
            hit = next((x for x in known if root == Path(x["pc_path"]) or Path(x["pc_path"]) in root.parents), None)
            if hit:
                sug.project_folder, sug.project_type, sug.project_name = hit["folder"], hit["project_type"], hit["project_name"]
            else:
                for part in reversed(root.parts[:-1]):
                    t, n = split_project(part, cfg)
                    if t:
                        sug.project_folder, sug.project_type, sug.project_name = f"{t} - {n}", t, n
                        break
        ptype = cfg.project_types.get(sug.project_type or "")
        if ptype and ptype.manual:
            sug.stage = None
            if not sug.addressees:
                sug.addressees = [CUSTOMER]
                sug.notes = [n for n in sug.notes if "Адресаты не распознаны" not in n]
        files, size, skipped = copy_tree_size(root)
        same = [r for r in store.releases() if r["source_path"] == str(root)]
        apps = [parse_application(f, cfg, f.name) for f in list_release_files(root)
                if f.parent == root and is_application(f.name)]
        if sug.released_at:
            for a in apps:
                if a["filed"] and a["filed"] < _days_before(sug.released_at, STALE_DAYS):
                    sug.notes.append(f"Заявление №{a['number']} подано {_ru(a['filed'])} — намного раньше среза; "
                                     "возможно, оно осталось от прошлой отправки.")
        from_apps = [c for a in apps for c in a["committees"]
                     if not (sug.released_at and a["filed"] and a["filed"] < _days_before(sug.released_at, STALE_DAYS))]
        if from_apps:
            if set(from_apps) != set(sug.addressees):
                sug.notes.append("Адресаты взяты из заявлений: " + ", ".join(dict.fromkeys(from_apps))
                                 + (" (по имени среза: " + ", ".join(sug.addressees) + ")" if sug.addressees else ""))
            sug.addressees = list(dict.fromkeys(from_apps))
        stages = {a["stage"] for a in apps if a["stage"]}
        if len(stages) == 1 and not sug.stage:
            sug.stage = stages.pop()
        return {
            **sug.__dict__,
            "applications": apps,
            "files": files, "size": size, "skipped": skipped,
            "already": [{"id": r["id"], "title": r["title"]} for r in same],
            "committees": [
                {"code": c.code, "name": c.name, "active": c.active, "final": c.final}
                for c in cfg.committees
            ],
            "project_types": list(cfg.project_types),
        }

    @api.post("/api/admin/releases")
    def create(payload: dict = Body(...), role: str = Depends(require_admin)):
        root = allowed_dir(payload.get("path", ""))
        title = str(payload.get("title") or root.name).strip()
        snap = None
        for part in root.parts:
            if parse_snapshot_name(part):
                snap = part
        meta = {
            "title": title,
            "released_at": (payload.get("released_at") or None),
            "project_folder": payload.get("project_folder") or None,
            "project_type": payload.get("project_type") or None,
            "project_name": payload.get("project_name") or None,
            "stage": str(payload.get("stage") or "").strip() or None,
            "snapshot": snap,
            "label": payload.get("label") or None,
            "note": (payload.get("note") or "").strip() or None,
        }
        codes = [str(c) for c in (payload.get("addressees") or []) if str(c).strip()]
        job_id = registrar.start(root, meta, codes)
        return registrar.job(job_id)

    @api.get("/api/admin/releases/jobs/{job_id}")
    def job(job_id: str, role: str = Depends(require_admin)):
        j = registrar.job(job_id)
        if not j:
            raise HTTPException(404, "Задача не найдена")
        return j

    @api.patch("/api/admin/releases/{release_id}")
    def update(release_id: int, payload: dict = Body(...), role: str = Depends(require_admin)):
        if not store.release(release_id):
            raise HTTPException(404, "Выпуск не найден")
        addressees = payload.pop("addressees", None)
        store.update(release_id, payload, addressees)
        state.sync_release_sendings(release_id)     # sendings follow the corrected release
        return store.release(release_id)

    @api.post("/api/admin/releases/{release_id}/resume")
    def resume(release_id: int, role: str = Depends(require_admin)):
        r = store.release(release_id)
        if not r:
            raise HTTPException(404, "Выпуск не найден")
        if r["status"] != "failed":
            raise HTTPException(400, "Выпуск скопирован полностью")
        allowed_dir(r["source_path"])          # the PC must be on and the folder still there
        return registrar.job(registrar.resume(release_id))

    @api.delete("/api/admin/releases/{release_id}")
    def delete(release_id: int, role: str = Depends(require_admin)):
        if not store.release(release_id):
            raise HTTPException(404, "Выпуск не найден")
        shas = {f["sha256"] for f in store.files(release_id)}
        # its sendings go with it: otherwise they stay in «Сроки» without a release (12.11)
        sendings = getattr(state, "sendings", None)
        if sendings:
            for snd in sendings.for_release(release_id):
                sendings.delete(snd["id"])
        store.delete(release_id)
        # copies no other release or catalog document needs are deleted from the server
        freed = sum(1 for sha in shas if state.purge_blob(sha))
        return {"ok": True, "files_deleted": freed}

    # ------------------------------------------------------------ viewers

    @api.get("/api/releases")
    def releases(role: str = Depends(require_viewer)):
        return {"releases": [for_role(r, role) for r in store.releases()]}

    @api.get("/api/releases/{release_id}")
    def release(release_id: int, role: str = Depends(require_viewer)):
        r = store.release(release_id)
        if not r:
            raise HTTPException(404, "Выпуск не найден")
        if r["applications"] is None and r["status"] == "ready":   # registered before applications were read
            r["applications"] = read_applications(release_id)
        return {"release": for_role(r, role), "files": store.files(release_id)}

    @api.get("/api/releases/{release_id}/compare/{other_id}")
    def compare(release_id: int, other_id: int, role: str = Depends(require_viewer)):
        """other_id is the older release; the result says what release_id brought."""
        from .releases import compare_files
        new, old = store.release(release_id), store.release(other_id)
        if not new or not old:
            raise HTTPException(404, "Выпуск не найден")
        diff = compare_files(store.files(other_id), store.files(release_id))
        return {"new": for_role(new, role), "old": for_role(old, role), **diff}

    def blob_file(release_id: int, file_id: int) -> tuple[dict, Path]:
        f = store.file(release_id, file_id)
        if not f:
            raise HTTPException(404, "Файл не найден")
        path = blobs.path(f["sha256"])
        if not path.is_file():
            raise HTTPException(410, "Копия файла отсутствует в хранилище")
        return f, path

    @api.get("/release/{release_id}/file/{file_id}")
    def release_file(release_id: int, file_id: int, dl: int = 0, role: str = Depends(require_viewer)):
        f, path = blob_file(release_id, file_id)
        name = f["rel_path"].rsplit("/", 1)[-1]
        media = mimetypes.guess_type(name)[0] or "application/octet-stream"
        # Only these open in the browser; anything else (html, svg, …) is always a download,
        # so a file from a release can never run as a page of the site.
        inline_ok = media in INLINE_TYPES
        return FileResponse(path, media_type=media if inline_ok or dl else "application/octet-stream", headers={
            "Content-Disposition": _disposition("attachment" if dl or not inline_ok else "inline", name),
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, max-age=3600",
        })

    @api.get("/release/{release_id}/zip")
    def release_zip(release_id: int, prefix: str = "", flat: int = 0, role: str = Depends(require_viewer)):
        r = store.release(release_id)
        if not r:
            raise HTTPException(404, "Выпуск не найден")
        prefix = prefix.strip("/")
        files = [f for f in store.files(release_id)
                 if not prefix or f["rel_path"] == prefix or f["rel_path"].startswith(prefix + "/")]
        if not files:
            raise HTTPException(404, "В этой папке нет файлов")
        top = r["title"] if not prefix else prefix.rsplit("/", 1)[-1]
        entries = []
        for f in files:
            rel = f["rel_path"][len(prefix) + 1:] if prefix else f["rel_path"]
            path = blobs.path(f["sha256"])
            if path.is_file():
                entries.append((rel, path, f["mtime"]))
        if not prefix and not flat:
            # whole release: each top-level folder goes in as its own zip (DWG.zip, …)
            entries = nest_folders(entries, top)
        else:
            entries = [(f"{top}/{rel}", path, mtime) for rel, path, mtime in entries]
        return StreamingResponse(
            stream_zip(entries), media_type="application/zip",
            headers={"Content-Disposition": _disposition("attachment", top + ".zip")},
        )

    # share links (app/shares.py) give the same files to people without an account
    state.release_file_response = release_file
    state.release_zip_response = release_zip
