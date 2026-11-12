"""HTTP-сервер: каталог согласований и админ-панель."""

from __future__ import annotations

import csv
import io
import logging
import re
import hashlib
import os
import string
import threading
import time
import uuid
import zipfile
from datetime import datetime
from collections import defaultdict
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import Body, Cookie, Depends, FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from .config import (
    BASE_DIR, CUSTOMER, Config, ConfigError, add_committee, load_config, set_committee_field, set_committee_flags,
)
from .scanner import Source, fingerprint, iter_projects, project_groups, scan, sha256
from .releases import BlobStore, stream_zip
from .backup import Backup, stale as backup_stale
from .store import Store, project_folder
from .users import UserError, Users
from .notify import TelegramError, TelegramNotifier
from .watcher import ChangeWatcher
from .releases_api import register_release_routes
from .shares import register_share_routes
from .uploads import decorate as decorate_uploads, is_upload, pc_path, register_upload_routes

log = logging.getLogger("server")

COOKIE_NAME = "dpt_session"
STATIC_DIR = BASE_DIR / "static"

# Страницы всегда перепроверяются на сервере: иначе после обновления
# программы браузер продолжит показывать старую вёрстку из кэша.
PAGE_HEADERS = {"Cache-Control": "no-cache, must-revalidate"}

# Ограничения на архив, чтобы случайный выбор «всё» не положил сервер.
MAX_ZIP_FILES = 5000
ZIP_TTL_SECONDS = 30 * 60

_BAD_NAME_CHARS = re.compile('[<>:"/\\|?*]|[\x00-\x1f]')


def safe_name(value: str, fallback: str = "без имени") -> str:
    """Имя, пригодное для пути внутри архива."""
    cleaned = _BAD_NAME_CHARS.sub("_", (value or "").strip()).strip(". ")
    return cleaned or fallback


class App:
    """Общее состояние: конфиг, индекс, наблюдатель, сессии."""

    def __init__(self, config: Config):
        self.config = config
        self.store = Store(config.data_dir / "index.db")
        # copies of catalog documents and release files (content-addressed, shared)
        self.blobs = BlobStore(config.data_dir / "blobs")
        self.users = Users(self.store._conn, self.store._lock)
        self.telegram = TelegramNotifier(config, self.store)
        self.releases = None   # set by register_release_routes
        self.ensure_customer()
        target = os.environ.get("BACKUP_DIR", "").strip()
        self.backup = Backup(config.data_dir, Path(target) if target else None,
                             self.store.get_meta, self.store.set_meta,
                             extra_files=[config.path.parent / "committees.yaml"])
        self.watcher = ChangeWatcher(config, self.store)
        self.serializer = URLSafeTimedSerializer(config.secret_key, salt="dpt-session")
        self.max_age = config.session_hours * 3600
        self._attempts: dict[str, list[float]] = defaultdict(list)
        self._publishing = False
        self.zip_dir = config.data_dir / "zip"
        self.zip_dir.mkdir(parents=True, exist_ok=True)
        self._zips: dict[str, dict] = {}
        self._zip_lock = threading.Lock()

    # --------------------------------------------------------------- sources

    def sources(self) -> list[Source]:
        return [Source.from_row(r) for r in self.store.sources(enabled_only=True)]

    def seed_sources(self) -> None:
        """Первый запуск: переносим папки из config.yaml в индекс."""
        if self.store.sources():
            return
        for path in self.config.sources:
            if path.is_dir():
                self.store.add_source(str(path))
                log.info("источник из config.yaml: %s", path)

    # ------------------------------------------------------------------ auth

    def _pw_tag(self, role: str) -> str:
        """Short fingerprint of the role's current password: changing it ends old sessions."""
        pw = self.config.admin_password if role == "admin" else self.config.viewer_password
        return hashlib.sha256(("graddok:" + (pw or "")).encode("utf-8")).hexdigest()[:12]

    def issue(self, role: str, user: dict | None = None) -> str:
        if user:
            return self.serializer.dumps({"uid": user["id"], "t": self.users.session_tag(user)})
        return self.serializer.dumps({"role": role, "pw": self._pw_tag(role)})

    def who(self, token: str | None) -> dict | None:
        """{role, name, uid} of the session, or None (expired, password changed, person disabled)."""
        if not token:
            return None
        try:
            data = self.serializer.loads(token, max_age=self.max_age)
        except (BadSignature, SignatureExpired):
            return None
        if "uid" in data:
            user = self.users.get(data["uid"])
            if not user or not user["active"] or data.get("t") != self.users.session_tag(user):
                return None
            return {"role": user["role"], "name": user["name"] or user["login"], "uid": user["id"]}
        role = data.get("role")
        if role not in ("viewer", "admin") or data.get("pw") != self._pw_tag(role):
            return None
        if role == "viewer" and self.users.any_active():
            return None   # the shared viewing password ends once personal accounts exist
        return {"role": role, "name": "Владелец" if role == "admin" else "Общий вход", "uid": None}

    def role_of(self, token: str | None) -> str | None:
        who = self.who(token)
        return who["role"] if who else None

    def throttled(self, *keys: str) -> bool:
        window = time.time() - 900
        for key in keys:
            self._attempts[key] = [t for t in self._attempts[key] if t > window]
        return any(len(self._attempts[k]) >= self.config.max_login_attempts for k in keys)

    def record_failure(self, *keys: str) -> None:
        for key in keys:
            self._attempts[key].append(time.time())

    def check_password(self, password: str) -> str | None:
        import secrets as _s

        admin = self.config.admin_password
        viewer = self.config.viewer_password
        given = password.encode("utf-8")
        if admin and _s.compare_digest(given, admin.encode("utf-8")):
            return "admin"
        if viewer and _s.compare_digest(given, viewer.encode("utf-8")) and not self.users.any_active():
            return "viewer"
        return None

    # ----------------------------------------------------------- комитеты

    @property
    def committees_file(self) -> Path:
        return self.config.path.parent / "committees.yaml"

    def ensure_customer(self) -> None:
        """«Заказчик» is always in the directory: an organization without an answer term."""
        if any(c.code == CUSTOMER for c in self.config.committees):
            return
        try:
            known = {x.lower() for c in self.config.committees for x in c.all_codes}
            add_committee(self.committees_file, CUSTOMER, "Заказчик", ["заказчика", "заказчику"], known)
            self.reload_committees()
        except (OSError, ConfigError) as exc:
            log.warning("не удалось добавить «Заказчик» в справочник: %s", exc)

    def reload_committees(self) -> None:
        """Перечитывает справочник после правки, не перезапуская сервер."""
        fresh = load_config(self.config.path)
        self.config.committees = fresh.committees

    # ------------------------------------------------------------- архивы

    def sweep_zips(self) -> None:
        """Убирает архивы, за которыми не пришли."""
        deadline = time.time() - ZIP_TTL_SECONDS
        with self._zip_lock:
            stale = [t for t, info in self._zips.items() if info["created"] < deadline]
            for token in stale:
                self._zips.pop(token, None)
        for path in self.zip_dir.glob("*.zip"):
            try:
                if path.stat().st_mtime < deadline:
                    path.unlink()
            except OSError:
                pass

    def register_zip(self, token: str, path: Path | None, name: str, items: list | None = None) -> None:
        with self._zip_lock:
            self._zips[token] = {"path": path, "name": name, "items": items or [], "created": time.time()}

    def take_zip(self, token: str) -> dict | None:
        with self._zip_lock:
            return self._zips.get(token)

    def drop_zip(self, token: str) -> None:
        with self._zip_lock:
            info = self._zips.pop(token, None)
        if info and info.get("path"):
            try:
                info["path"].unlink(missing_ok=True)
            except OSError:
                pass

    # --------------------------------------------------------------- copies

    def copy_documents(self, docs: list[dict]) -> None:
        """Hashes every found document and keeps a copy on the server.

        Unchanged files (same path, size and time, copy present) are not read again;
        documents deleted from the catalog are only hashed (needed to follow renames),
        never copied.
        """
        known = self.store.all_by_path()
        copied = 0
        for doc in docs:
            prev = known.get(doc["abs_path"])
            path = Path(doc["abs_path"])
            try:
                if (prev and prev.get("sha256") and prev["size"] == doc["size"]
                        and same_time(prev["mtime"], doc["mtime"]) and self.blobs.has(prev["sha256"])):
                    doc["sha256"] = prev["sha256"]
                elif prev and prev.get("hidden"):
                    doc["sha256"] = sha256(path)
                else:
                    doc["sha256"], _size, new = self.blobs.put(path)
                    copied += new
            except OSError as exc:
                log.warning("не удалось скопировать %s: %s", path, exc)
                doc["sha256"] = (prev or {}).get("sha256") or ""
        if copied:
            log.info("скопировано на сервер новых файлов: %s", copied)

    def fill_copies(self) -> int:
        """Copies documents already in the index whose copy is missing (no new publication).

        Needed once after the update that introduced copies, and whenever the PC was
        off during a scan. A file whose content changed is left to the next publication.
        """
        done = 0
        down = set(self.unreachable_sources())
        for doc in self.store.documents():
            sha = doc.get("sha256")
            if not sha or self.blobs.has(sha):
                continue
            if any(str(doc.get("abs_path", "")).startswith(root) for root in down):
                continue
            try:
                new_sha, _size, _new = self.blobs.put(Path(doc["abs_path"]))
            except OSError:
                continue   # PC off or file gone: try next time
            if new_sha != sha:
                self.purge_blob(new_sha)
            else:
                done += 1
        if done:
            log.info("дозаполнены копии: %s", done)
        return done

    def purge_blob(self, sha: str | None) -> bool:
        """Deletes a copy that nothing (catalog or release) needs any more."""
        if not sha or self.store.uses_blob(sha) or (self.releases and self.releases.uses_blob(sha)):
            return False
        if self.blobs.fresh(sha):
            return False   # just written: a registration or scan may not have recorded it yet
        return self.blobs.delete(sha)

    # --------------------------------------------------------------- publish

    def notify_telegram(self, pub_id: int) -> None:
        """Telegram message about new / changed / removed letters, in the background.

        Hidden (deleted from the catalog) documents are not mentioned. If sending fails,
        the summary waits and goes with the next publication.
        """
        if not self.telegram.enabled:
            return
        new_rows, changed_rows, removed_rows = [], [], []
        for row in self.store.documents(include_removed=True):
            if row.get("hidden"):
                continue
            if not row["present"]:
                if row["last_pub_id"] == pub_id and row["status"] == "removed":
                    removed_rows.append(row)
            elif row["first_pub_id"] == pub_id:
                new_rows.append(row)
            elif row["changed_pub_id"] == pub_id:
                changed_rows.append(row)
        if new_rows or changed_rows or removed_rows:
            self.telegram.remember(new_rows, changed_rows, removed_rows)
        if self.telegram.pending().empty:
            return

        def send():
            try:
                result = self.telegram.send_pending()
                if result:
                    log.info("телеграм: отправлено в %s чат(ов), документов %s", len(result["sent"]), result["total"])
            except TelegramError as exc:
                log.warning("телеграм: %s — сводка ждёт следующего «Актуализировать»", exc)
        threading.Thread(target=send, name="telegram", daemon=True).start()

    def unreachable_sources(self) -> list[str]:
        """Enabled project folders that cannot be read now (PC off, share not mounted)."""
        out = []
        for s in self.sources():
            try:
                ok = s.path.is_dir()
            except OSError:
                ok = False
            if not ok:
                out.append(str(s.path))
        return out

    def pending_changes(self) -> dict:
        """What «Актуализировать» would change — files only listed, nothing read or written."""
        missing = self.unreachable_sources()
        if missing:
            return {"unreachable": missing, "items": []}
        docs, _projects = scan(self.config, self.sources(), with_hash=False)
        known = self.store.all_by_path()
        items, seen = [], set()

        def item(kind: str, row: dict) -> dict:
            return {"kind": kind, "project": project_folder(row.get("project_key") or ""),
                    "path": row.get("rel_path") or row.get("file_name"), "hidden": bool(row.get("hidden"))}

        for doc in docs:
            seen.add(doc["abs_path"])
            prev = known.get(doc["abs_path"])
            if prev is None:
                items.append(item("new", doc))
            elif not prev["present"]:
                items.append(item("back", doc))
            elif prev["size"] != doc["size"] or not same_time(prev["mtime"], doc["mtime"]):
                items.append(item("changed", doc))
        enabled = {s.id for s in self.sources()}
        for path, row in known.items():
            if row["present"] and path not in seen and row.get("source_id") in enabled:
                items.append(item("gone", row))
        order = {"new": 0, "back": 1, "changed": 2, "gone": 3}
        items.sort(key=lambda x: (order[x["kind"]], x["project"], x["path"]))
        return {"unreachable": [], "items": items}

    def publish(self, note: str = "") -> dict:
        if self._publishing:
            raise HTTPException(409, "Обновление уже выполняется")
        missing = self.unreachable_sources()
        if missing:
            # Without this an unreachable PC looks like «every document was deleted».
            raise HTTPException(
                409, "Папки ПК недоступны (ПК выключен?) — каталог не обновлён, всё осталось как было. "
                     "Недоступно: " + "; ".join(missing))
        self._publishing = True
        try:
            sources = self.sources()
            docs, _projects = scan(self.config, sources, with_hash=False)
            self.copy_documents(docs)
            fp = fingerprint(self.config, sources)
            stats = self.store.publish(docs, fp, note)
            for old_id, new_id in stats.pop("moved_ids", []):
                if getattr(self, "sendings", None):
                    self.sendings.remap_answer(old_id, new_id)
            self.store.set_meta("published_fingerprint", fp)
            self.watcher.mark_published(fp)
            log.info("публикация #%s: %s", stats["publication_id"], stats)
            self.notify_telegram(stats["publication_id"])
            return stats
        finally:
            self._publishing = False


def same_time(a: str | None, b: str | None) -> bool:
    """Modification times as the same instant: «…09:02:05+00:00» equals «…11:02:05+02:00»."""
    if a == b:
        return True
    try:
        return datetime.fromisoformat(a) == datetime.fromisoformat(b)
    except (TypeError, ValueError):
        return False


def client_ip(request: Request) -> str:
    """Real client address; behind tsdproxy it comes in X-Forwarded-For.

    Without this every login attempt would look like it came from the proxy
    and the attempt limit would lock everyone out at once.
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "?"


def browse_roots() -> list[str]:
    """Top level of the folder browser.

    On the server only the mounted read-only sources are offered
    (BROWSE_ROOTS=/sources/...), on a Windows PC — the drive letters.
    """
    env = os.environ.get("BROWSE_ROOTS", "")
    roots = [r for r in env.split(os.pathsep if os.name == "nt" else ":") if r and Path(r).is_dir()]
    if roots:
        return roots
    drives = [f"{letter}:\\" for letter in string.ascii_uppercase if Path(f"{letter}:\\").exists()]
    return drives or ["/"]


def create_app(config: Config | None = None) -> FastAPI:
    cfg = config or load_config()
    state = App(cfg)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        state.seed_sources()
        # Everything that touches the PC shares runs in the background: with the PC off each
        # access waits for the mount timeout, and the site must come up at once (e.g. after
        # the Pi's nightly reboot).
        def warm_up():
            try:
                if state.store.latest_publication() is None and state.store.sources(enabled_only=True) \
                        and not state.unreachable_sources():
                    log.info("индекс пуст — выполняю первичное сканирование")
                    state.publish("первичное сканирование")
                state.watcher.start()
                state.fill_copies()
            except Exception:  # noqa: BLE001
                log.exception("фоновый запуск")
        threading.Thread(target=warm_up, name="warm-up", daemon=True).start()
        state.backup.start()
        try:
            yield
        finally:
            state.watcher.stop()
            state.store.close()

    api = FastAPI(title="ГРАДДОК", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    # ------------------------------------------------------------ зависимости

    def current_role(session: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> str | None:
        return state.role_of(session)

    def require_viewer(role: str | None = Depends(current_role)) -> str:
        if role is None:
            raise HTTPException(401, "Требуется вход")
        return role

    def require_admin(role: str | None = Depends(current_role)) -> str:
        if role != "admin":
            raise HTTPException(403, "Требуются права администратора")
        return role

    def current_who(session: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> dict | None:
        return state.who(session)

    # ------------------------------------------------------------ пользователи

    @api.get("/api/admin/users")
    def admin_users(role: str = Depends(require_admin), me: dict | None = Depends(current_who)):
        return {"users": state.users.list(), "me": me and me["uid"],
                "shared_viewer": bool(cfg.viewer_password) and not state.users.any_active()}

    @api.post("/api/admin/users")
    def admin_add_user(role: str = Depends(require_admin), payload: dict = Body(...)):
        try:
            user = state.users.add(payload.get("login", ""), payload.get("name", ""),
                                   payload.get("role", "viewer"), payload.get("password", ""))
        except UserError as exc:
            raise HTTPException(400, str(exc)) from None
        log.info("добавлен пользователь %s (%s)", user["login"], user["role"])
        return {"user": user, "users": state.users.list()}

    @api.patch("/api/admin/users/{user_id}")
    def admin_edit_user(user_id: int, role: str = Depends(require_admin),
                        me: dict | None = Depends(current_who), payload: dict = Body(...)):
        if me and me["uid"] == user_id and (payload.get("active") is False or payload.get("role") == "viewer"):
            raise HTTPException(400, "Нельзя отключить себя или снять с себя права администратора")
        allowed = {k: payload[k] for k in ("name", "role", "active", "password") if k in payload}
        try:
            state.users.update(user_id, allowed)
        except UserError as exc:
            raise HTTPException(400, str(exc)) from None
        return {"users": state.users.list()}

    @api.delete("/api/admin/users/{user_id}")
    def admin_delete_user(user_id: int, role: str = Depends(require_admin), me: dict | None = Depends(current_who)):
        if me and me["uid"] == user_id:
            raise HTTPException(400, "Нельзя удалить себя")
        state.users.delete(user_id)
        return {"users": state.users.list()}

    # ------------------------------------------------------------------ вход

    @api.get("/login", response_class=HTMLResponse)
    def login_page(role: str | None = Depends(current_role)):
        if role:
            return RedirectResponse("/", status_code=303)
        return FileResponse(STATIC_DIR / "login.html", headers=PAGE_HEADERS)

    @api.post("/api/login")
    def login(request: Request, password: str = Form(...), login: str = Form("")):
        ip = client_ip(request)
        login = login.strip().lower()
        keys = (ip, "login:" + login) if login else (ip,)
        if state.throttled(*keys):
            raise HTTPException(429, "Слишком много попыток. Попробуйте через 15 минут.")
        user = None
        if login:
            user = state.users.authenticate(login, password)
            role = user["role"] if user else None
        else:
            role = state.check_password(password)
        if role is None:
            state.record_failure(*keys)
            raise HTTPException(401, "Неверный e-mail или пароль" if login else "Неверный пароль")
        if user:
            state.users.touch(user["id"], ip)
            log.info("вход: %s (%s) с %s", user["login"], user["role"], ip)
        response = JSONResponse({"ok": True, "role": role})
        secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
        response.set_cookie(
            COOKIE_NAME, state.issue(role, user), max_age=state.max_age,
            httponly=True, samesite="lax", secure=secure, path="/",
        )
        return response

    @api.post("/api/logout")
    def logout():
        response = JSONResponse({"ok": True})
        response.delete_cookie(COOKIE_NAME, path="/")
        return response

    # --------------------------------------------------------------- страницы

    @api.get("/", response_class=HTMLResponse)
    def index(role: str | None = Depends(current_role)):
        if role is None:
            return RedirectResponse("/login", status_code=303)
        return FileResponse(STATIC_DIR / "index.html", headers=PAGE_HEADERS)

    @api.get("/admin", response_class=HTMLResponse)
    def admin_page(role: str | None = Depends(current_role)):
        if role is None:
            return RedirectResponse("/login", status_code=303)
        if role != "admin":
            return RedirectResponse("/", status_code=303)
        return FileResponse(STATIC_DIR / "admin.html", headers=PAGE_HEADERS)

    def mark_document(row: dict, latest_id: int | None) -> dict:
        """Проставляет признаки «новое» / «обновлён».

        Ручная пометка сильнее автоматического расчёта: файл мог сохраниться
        заново и потерять новизну, либо наоборот — его уже посмотрели.
        """
        auto_new = row["first_pub_id"] == latest_id
        manual = row.get("manual_new")
        row["is_new"] = bool(manual) if manual is not None else auto_new
        # SQLite хранит 1/0 — приводим к настоящим true/false,
        # иначе строгие сравнения в интерфейсе не срабатывают.
        row["manual_new"] = None if manual is None else bool(manual)
        row["is_changed"] = (
            row["changed_pub_id"] == latest_id and row["first_pub_id"] != latest_id
            and not row["is_new"]
        )
        return row

    def committees_payload() -> list[dict]:
        """Справочник для матрицы: столбцы строятся по нему, а не только
        по тем органам, от которых уже что-то пришло."""
        return [
            {
                "code": c.code, "name": c.name, "active": c.active,
                "parent": c.parent, "final": c.final, "district": c.district,
                "aliases": c.aliases, "custom": c.custom,
                "term": c.term, "term_days": c.term_days,
            }
            for c in cfg.committees
        ]

    # ------------------------------------------------------------------- API

    @api.get("/api/meta")
    def meta(role: str = Depends(require_viewer), me: dict | None = Depends(current_who)):
        return {
            "role": role,
            "user": me and me["name"],
            "projects": state.store.projects(),
            "groups": [{"id": g.id, "title": g.title, "matrix": g.matrix} for g in cfg.enabled_groups],
            "decisions": [{"id": d.id, "label": d.label, "kind": d.kind} for d in cfg.decisions],
            "final_orgs": cfg.final_orgs,
            "publication": state.store.latest_publication(),
        }

    @api.get("/api/documents")
    def documents(role: str = Depends(require_viewer)):
        pub = state.store.latest_publication()
        latest_id = pub["id"] if pub else None
        rows = state.store.documents()
        decorate_uploads(rows)
        for row in rows:
            mark_document(row, latest_id)
            for hidden in ("sha256", "abs_path", "rel_path", "source_id"):
                row.pop(hidden, None)
        return {
            "documents": rows,
            "publication": pub,
            "final_orgs": cfg.final_orgs,
            "committees": committees_payload(),
            "groups": [
                {"id": g.id, "title": g.title, "matrix": g.matrix}
                for g in cfg.enabled_groups
            ],
            # by catalog project key, as the matrix rows are; missing = standard set
            "requirements": requirements_by_key(rows),
            "default_orgs": [c.code for c in cfg.committees if c.active],
            "archived": [p["folder"] for p in project_list() if p["archived"]],
        }

    @api.get("/api/status")
    def status(role: str = Depends(require_viewer)):
        payload = {"publication": state.store.latest_publication(), "role": role}
        if role == "admin":
            payload["watcher"] = state.watcher.state
        return payload

    @api.get("/api/export.csv")
    def export_csv(role: str = Depends(require_viewer)):
        rows = state.store.documents()
        buf = io.StringIO()
        writer = csv.writer(buf, delimiter=";", lineterminator="\r\n")
        writer.writerow([
            "Проект", "Тип", "Этап", "Группа", "Решение", "Орган",
            "Наименование органа", "Уточнение", "Примечание",
            "Файл", "Подпапка", "Сохранён на ПК", "Дата в имени файла",
            "Появился в каталоге",
        ])
        for r in rows:
            writer.writerow([
                r["project_name"], r["project_type"] or "", r["stage"] or "",
                r["group_title"], r["decision_label"], r["org_code"] or "",
                r["org_name"] or "", r["org_detail"] or "", r["note"] or "",
                r["file_name"], r["subfolder"] or "",
                (r["mtime"] or "")[:10], r["doc_date"] or "",
                (r["first_seen_at"] or "")[:10],
            ])
        return Response(
            ("﻿" + buf.getvalue()).encode("utf-8"),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="soglasovaniya.csv"'},
        )

    # -------------------------------------------------------------- файлы PDF

    def resolve(doc_id: int) -> tuple[dict, Path]:
        doc = state.store.document_any(doc_id)
        if doc is None:
            raise HTTPException(404, "Документ не найден")
        # Удалённый документ не отдаём и по старой прямой ссылке.
        if doc.get("hidden"):
            raise HTTPException(404, "Документ удалён из каталога")
        # the copy on the server first: it opens even when the PC is off or the file is gone from it
        if doc.get("sha256") and state.blobs.has(doc["sha256"]):
            return doc, state.blobs.path(doc["sha256"])
        if not doc.get("present"):
            raise HTTPException(410, "Файла больше нет на ПК, копии на сервере тоже нет")
        path = Path(doc["abs_path"]).resolve()
        # Путь берётся из индекса, но дополнительно проверяем, что он лежит
        # внутри одной из подключённых сейчас папок.
        allowed = [s.path.resolve() for s in state.sources()]
        if not any(root == path or root in path.parents for root in allowed):
            raise HTTPException(403, "Путь вне подключённых папок")
        if not path.is_file():
            raise HTTPException(410, "Файл больше не доступен на диске")
        return doc, path

    def send(doc_id: int, disposition: str) -> FileResponse:
        doc, path = resolve(doc_id)
        header = f"{disposition}; filename*=UTF-8''{quote(doc['file_name'])}"
        return FileResponse(
            path,
            media_type="application/pdf",
            headers={"Content-Disposition": header, "Cache-Control": "private, max-age=60"},
        )

    state.send_doc = send   # share links (app/shares.py)

    @api.get("/file/{doc_id}")
    def view_file(doc_id: int, role: str = Depends(require_viewer)):
        return send(doc_id, "inline")

    @api.get("/download/{doc_id}")
    def download_file(doc_id: int, role: str = Depends(require_viewer)):
        return send(doc_id, "attachment")

    # ----------------------------------------------- скачивание нескольких

    @api.post("/api/zip")
    def build_zip(role: str = Depends(require_viewer), payload: dict = Body(...)):
        raw_ids = payload.get("ids")
        if not isinstance(raw_ids, list) or not raw_ids:
            raise HTTPException(400, "Не выбрано ни одного документа")
        if len(raw_ids) > MAX_ZIP_FILES:
            raise HTTPException(
                400, f"За один раз можно скачать не больше {MAX_ZIP_FILES} документов"
            )

        state.sweep_zips()

        items: list[tuple[str, Path]] = []
        total = 0
        used: set[str] = set()
        for raw in raw_ids:
            try:
                doc, path = resolve(int(raw))
            except (HTTPException, TypeError, ValueError):
                continue  # документ исчез или недоступен — пропускаем
            total += path.stat().st_size

            # Раскладываем по папкам проект / этап, как в каталоге.
            parts = [safe_name(
                (doc["project_type"] + " - " if doc["project_type"] else "") + doc["project_name"],
                "Проект",
            )]
            if doc["stage"]:
                parts.append(safe_name(doc["stage"] + " этап"))
            name = safe_name(doc["file_name"], "документ.pdf")
            entry = "/".join(parts + [name])

            # Одинаковые имена в разных папках возможны — разводим суффиксом.
            if entry in used:
                stem, dot, ext = name.rpartition(".")
                n = 2
                while entry in used:
                    suffix = f" ({n})"
                    entry = "/".join(parts + [f"{stem}{suffix}{dot}{ext}" if dot else f"{name}{suffix}"])
                    n += 1
            used.add(entry)
            items.append((entry, path))

        if not items:
            raise HTTPException(410, "Выбранные документы больше не доступны")

        # The archive is built on the fly while it downloads (no temp file, no size limit);
        # the token only remembers what goes in, for half an hour.
        token = uuid.uuid4().hex
        name = f"Согласования {datetime.now().strftime('%Y-%m-%d')}.zip"
        state.register_zip(token, None, name, items)
        return {"token": token, "name": name, "count": len(items), "size": total}

    @api.get("/zip/{token}")
    def download_zip(token: str, role: str = Depends(require_viewer)):
        state.sweep_zips()
        info = state.take_zip(token)
        if info is None:
            raise HTTPException(404, "Архив не найден или устарел — соберите заново")
        header = f"attachment; filename*=UTF-8''{quote(info['name'])}"
        entries = [(entry, path, None) for entry, path in info["items"] if path.is_file()]
        return StreamingResponse(stream_zip(entries), media_type="application/zip",
                                 headers={"Content-Disposition": header})

    # ------------------------------------------------------------ админ-часть

    def describe_source(row: dict, stats: dict) -> dict:
        path = Path(row["path"])
        info = dict(row)
        info["exists"] = path.is_dir()
        info["docs"] = stats.get(row["id"], {}).get("docs", 0)
        info["projects"] = stats.get(row["id"], {}).get("projects", 0)
        return info

    @api.get("/api/admin/sources")
    def admin_sources(role: str = Depends(require_admin)):
        stats = state.store.stats_by_source()
        rows = [describe_source(r, stats) for r in state.store.sources()]
        return {"sources": rows}

    @api.post("/api/admin/sources")
    def admin_add_source(role: str = Depends(require_admin), payload: dict = Body(...)):
        raw = str(payload.get("path", "")).strip().strip('"')
        if not raw:
            raise HTTPException(400, "Укажите путь к папке")
        path = Path(raw).expanduser()
        if not path.is_dir():
            raise HTTPException(400, f"Папка не найдена: {path}")

        resolved = path.resolve()
        for existing in state.store.sources():
            other = Path(existing["path"]).resolve()
            if other == resolved:
                continue
            if other in resolved.parents:
                raise HTTPException(400, f"Эта папка уже входит в «{other}»")
            if resolved in other.parents:
                raise HTTPException(400, f"Внутри неё уже подключена «{other}»")

        row = state.store.add_source(str(resolved), str(payload.get("label", "")).strip())
        probe = Source.from_row(row)
        projects = iter_projects(cfg, [probe])
        with_groups = [p for p in projects if p.groups]
        state.watcher.refresh_watches()
        state.watcher.recheck(force=True)
        return {
            "source": describe_source(row, state.store.stats_by_source()),
            "found_projects": len(projects),
            "found_with_approvals": len(with_groups),
            "sample": [p.dir.name for p in with_groups[:5]],
        }

    @api.patch("/api/admin/sources/{source_id}")
    def admin_update_source(
        source_id: int, role: str = Depends(require_admin), payload: dict = Body(...)
    ):
        if state.store.source(source_id) is None:
            raise HTTPException(404, "Папка не найдена")
        if "enabled" in payload:
            state.store.set_source_enabled(source_id, bool(payload["enabled"]))
        if "label" in payload:
            state.store.set_source_label(source_id, str(payload["label"]).strip())
        state.watcher.refresh_watches()
        state.watcher.recheck(force=True)
        return describe_source(state.store.source(source_id), state.store.stats_by_source())

    @api.delete("/api/admin/sources/{source_id}")
    def admin_delete_source(source_id: int, role: str = Depends(require_admin)):
        if state.store.source(source_id) is None:
            raise HTTPException(404, "Папка не найдена")
        state.store.remove_source(source_id)
        state.watcher.refresh_watches()
        state.watcher.recheck(force=True)
        return {"ok": True}

    @api.get("/health")
    def health():
        return {"ok": True}

    @api.get("/api/admin/browse")
    def admin_browse(role: str = Depends(require_admin), path: str = ""):
        """Обзор папок этого ПК — чтобы не вводить путь вручную."""
        if not path:
            roots = browse_roots()
            return {"current": "", "parent": None, "dirs": [{"name": d, "path": d} for d in roots]}

        target = Path(path).expanduser()
        if not target.is_dir():
            raise HTTPException(400, f"Папка не найдена: {target}")
        target = target.resolve()
        roots = [Path(r).resolve() for r in browse_roots()]
        if not any(target == r or r in target.parents for r in roots):
            raise HTTPException(403, "Папка вне подключённых источников")

        dirs = []
        try:
            for entry in sorted(target.iterdir(), key=lambda p: p.name.lower()):
                if entry.is_dir() and not entry.name.startswith((".", "$")):
                    dirs.append({"name": entry.name, "path": str(entry)})
        except PermissionError:
            raise HTTPException(403, "Нет доступа к этой папке")

        parent = str(target.parent) if target.parent != target and target not in roots else ""
        # Подсказка «это сама папка проекта»: внутри нашлась хотя бы одна
        # из групп документов.
        has_approvals = bool(project_groups(cfg, target))
        return {
            "current": str(target),
            "parent": parent,
            "dirs": dirs,
            "looks_like_project": has_approvals,
        }

    @api.post("/api/admin/publish")
    def admin_publish(role: str = Depends(require_admin)):
        """Перечитать папки ПК и обновить каталог."""
        # Если все папки выключены — публикуем пустой результат: это
        # осознанное действие, каталог должен опустеть.
        if not state.store.sources():
            raise HTTPException(400, "Не указано ни одной папки с проектами")

        # Пересобираем индекс, только если на диске действительно что-то
        # изменилось. Иначе повторное нажатие создавало бы пустую публикацию
        # и сбрасывало пометки «НОВОЕ» у документов прошлой.
        changed = state.watcher.recheck(force=True)
        first_run = state.store.latest_publication() is None
        copies = state.fill_copies()
        if changed or first_run:
            stats = state.publish("ручное обновление")
            stats["reindexed"] = True
        else:
            stats = dict(state.store.latest_publication() or {})
            stats["reindexed"] = False

        stats["copies_filled"] = copies
        return stats

    @api.get("/api/admin/telegram")
    def admin_telegram(role: str = Depends(require_admin)):
        pending = state.telegram.pending()
        return {"settings": state.telegram.check(),
                "pending": {"total": len(pending.items), "since": pending.since,
                            "files": [x["file"] for x in pending.items[:20]]}}

    @api.post("/api/admin/telegram/test")
    def admin_telegram_test(role: str = Depends(require_admin)):
        try:
            return state.telegram.send("\n\n".join([
                "Проверка связи.",
                "ГРАДДОК настроен: сюда приходят сообщения о новых письмах после «Актуализировать».",
                state.telegram.link]))
        except TelegramError as exc:
            raise HTTPException(400, str(exc)) from exc

    @api.post("/api/admin/telegram/send")
    def admin_telegram_send(role: str = Depends(require_admin)):
        """Send the waiting summary now (e.g. after a failure)."""
        try:
            return state.telegram.send_pending() or {"sent": [], "total": 0}
        except TelegramError as exc:
            raise HTTPException(400, str(exc)) from exc

    @api.get("/api/admin/pending-changes")
    def admin_pending_changes(role: str = Depends(require_admin)):
        return state.pending_changes()

    @api.post("/api/admin/recheck")
    def admin_recheck(role: str = Depends(require_admin)):
        state.watcher.recheck(force=True)
        return state.watcher.state

    def requirements_by_key(rows: list[dict]) -> dict[str, list[str]]:
        req = state.store.requirements()
        out = {}
        for r in rows:
            codes = req.get(project_folder(r["project_key"]))
            if codes is not None:
                out[r["project_key"]] = codes
        return out

    def project_list() -> list[dict]:
        """Every project the program knows: catalog folders, projects kept by hand, releases, sendings.

        project_key — what uploaded documents get: the catalog key for a folder-scanned project
        (so they share a matrix row with its documents), the folder name otherwise."""
        found: dict[str, dict] = {}
        types = cfg.project_types

        def add(folder, ptype, pname, key=None):
            if not folder:
                return None
            if folder not in found:
                found[folder] = {"folder": folder, "project_type": ptype, "project_name": pname or folder,
                                 "project_key": key or folder, "scanned": False, "used": set()}
            return found[folder]

        for d in state.store.documents(include_removed=True):
            p = add(project_folder(d["project_key"]), d.get("project_type"), d.get("project_name"), d["project_key"])
            if p and d.get("present") and d.get("source_id") is not None:
                p["scanned"] = True
                p["project_key"] = d["project_key"]
            if p and d.get("org_code") and d.get("present"):
                p["used"].add(d["org_code"])
        rows = state.store.project_rows()
        for folder, r in rows.items():
            add(folder, r["project_type"], r["project_name"])
        if state.releases:
            for r in state.releases.releases():
                add(r.get("project_folder"), r.get("project_type"), r.get("project_name"))
        if getattr(state, "sendings", None):
            for s in state.sendings.all():
                folder = f"{s['project_type']} - {s['project_name']}" if s.get("project_type") else s["project_name"]
                p = add(folder, s.get("project_type"), s["project_name"])
                if p:
                    p["used"].add(s["org_code"])
        req = state.store.requirements()
        out = []
        for p in sorted(found.values(), key=lambda x: x["folder"].lower()):
            r = rows.get(p["folder"]) or {}
            t = types.get(p["project_type"] or "")
            p["manual"] = bool(r.get("manual")) or bool(t and t.manual)
            p["stages"] = bool(t and t.stages)
            p["archived"] = bool(r.get("archived"))
            p["pc_path"] = r.get("pc_path")
            p["pc_path_shown"] = pc_path(r["pc_path"]) if r.get("pc_path") else None
            p["required"] = req.get(p["folder"])
            p["used"] = sorted(p["used"])
            out.append(p)
        return out

    state.project_list = project_list

    @api.get("/api/admin/projects")
    def admin_projects(role: str = Depends(require_admin)):
        return {
            "projects": project_list(),
            "types": [{"code": t.code, "title": t.title, "manual": t.manual, "stages": t.stages}
                      for t in cfg.project_types.values()],
        }

    @api.post("/api/admin/projects")
    def admin_new_project(role: str = Depends(require_admin), payload: dict = Body(...)):
        """A project kept by hand: АГПТ, Концепция or «Проект» — a name and, optionally, its folder."""
        ptype = str(payload.get("project_type") or "").strip()
        name = " ".join(str(payload.get("project_name") or "").split())
        t = cfg.project_types.get(ptype)
        if not t or not t.manual:
            raise HTTPException(400, "Вручную ведутся только АГПТ, Концепция и «Проект»")
        if not name or any(ch in name for ch in '\\/:*?"<>|'):
            raise HTTPException(400, "Название не может быть пустым или содержать \\ / : * ? \" < > |")
        folder = f"{ptype} - {name}"
        if any(p["folder"].lower() == folder.lower() for p in project_list()):
            raise HTTPException(400, f"Проект «{folder}» уже есть")
        path = str(payload.get("pc_path") or "").strip() or None
        if path:
            check_inside_sources(path)
        state.store.save_project(folder, project_type=ptype, project_name=name, manual=1, pc_path=path)
        # by default a project kept by hand answers to the customer only
        state.store.set_requirements(folder, [CUSTOMER])
        return {"projects": project_list()}

    @api.patch("/api/admin/projects/{folder:path}")
    def admin_edit_project(folder: str, role: str = Depends(require_admin), payload: dict = Body(...)):
        p = next((x for x in project_list() if x["folder"] == folder), None)
        if not p:
            raise HTTPException(404, "Проект не найден")
        fields = {"project_type": p["project_type"], "project_name": p["project_name"], "manual": int(p["manual"])}
        if "archived" in payload:
            fields["archived"] = 1 if payload["archived"] else 0
        if "pc_path" in payload:
            path = str(payload.get("pc_path") or "").strip() or None
            if path:
                check_inside_sources(path)
            fields["pc_path"] = path
        state.store.save_project(folder, **fields)
        return {"projects": project_list()}

    def check_inside_sources(path: str) -> None:
        target = Path(path).resolve()
        roots = [Path(r).resolve() for r in browse_roots()]
        if not any(target == r or r in target.parents for r in roots):
            raise HTTPException(400, "Папка вне подключённых источников")

    @api.get("/api/admin/project-orgs")
    def admin_project_orgs(role: str = Depends(require_admin)):
        return {
            "projects": project_list(),
            "committees": committees_payload(),
            "default_orgs": [c.code for c in cfg.committees if c.active],
        }

    @api.put("/api/admin/project-orgs/{folder:path}")
    def admin_set_project_orgs(folder: str, role: str = Depends(require_admin), payload: dict = Body(...)):
        """Organizations the project needs; null = back to the standard set."""
        codes = payload.get("codes")
        if codes is not None:
            if not isinstance(codes, list):
                raise HTTPException(400, "codes — список или null")
            known = {c.code for c in cfg.committees}
            unknown = [c for c in codes if c not in known]
            if unknown:
                raise HTTPException(400, "Нет в справочнике: " + ", ".join(unknown))
            codes = [str(c) for c in codes]
        state.store.set_requirements(folder, codes)
        return {"projects": project_list()}

    @api.post("/api/admin/committees")
    def admin_add_committee(role: str = Depends(require_admin), payload: dict = Body(...)):
        """A new organization (РЖД, Ленводхоз, соседи…) for the whole directory."""
        known = {x.lower() for c in cfg.committees for x in c.all_codes}
        aliases = payload.get("aliases") or []
        if isinstance(aliases, str):
            aliases = [a for a in re.split(r"[,;]", aliases)]
        try:
            add_committee(state.committees_file, str(payload.get("code") or ""), str(payload.get("name") or ""),
                          [str(a) for a in aliases], known)
            state.reload_committees()
        except ConfigError as exc:
            raise HTTPException(400, str(exc)) from exc
        except OSError as exc:
            raise HTTPException(500, f"Не удалось сохранить справочник: {exc}") from exc
        return {"committees": committees_payload()}

    @api.patch("/api/admin/documents")
    def admin_mark_documents(role: str = Depends(require_admin), payload: dict = Body(...)):
        """Пометка «новое» или удаление из каталога сразу для выделенных."""
        ids = payload.get("ids")
        if not isinstance(ids, list) or not ids:
            raise HTTPException(400, "Не выбрано ни одного документа")
        has_mark = "manual_new" in payload
        has_hidden = "hidden" in payload
        if not (has_mark or has_hidden):
            raise HTTPException(400, "Нечего менять")

        value = payload.get("manual_new")
        if has_mark and value is not None and not isinstance(value, bool):
            raise HTTPException(400, "manual_new должен быть true, false или null")
        hide = payload.get("hidden")
        if has_hidden and not isinstance(hide, bool):
            raise HTTPException(400, "hidden должен быть true или false")

        pub = state.store.latest_publication()
        latest_id = pub["id"] if pub else None
        updated, forgotten = [], []
        for raw in ids:
            try:
                doc_id = int(raw)
            except (TypeError, ValueError):
                continue
            row = None
            if has_mark:
                row = state.store.set_manual_new(doc_id, value)
            if has_hidden:
                current = state.store.document(doc_id)
                if hide and current and is_upload(current):
                    # an upload exists only here: deleting it means deleting it for good
                    state.store.delete_document(doc_id)
                    state.purge_blob(current.get("sha256"))
                    continue
                row = state.store.set_hidden(doc_id, hide)
                if row is not None and hide:
                    forgotten.append(row)
                    # deleted from the catalog = its copy leaves the server too
                    state.purge_blob(row.get("sha256"))
            if row is None:
                continue
            mark_document(row, latest_id)
            row["hidden"] = bool(row.get("hidden"))
            for secret in ("sha256", "abs_path", "rel_path", "source_id"):
                row.pop(secret, None)
            updated.append(row)
        return {"documents": updated, "hidden_total": len(state.store.hidden_documents())}

    @api.get("/api/admin/documents/hidden")
    def admin_hidden_documents(role: str = Depends(require_admin)):
        """Что удалено из каталога — чтобы это можно было вернуть."""
        rows = []
        for row in state.store.hidden_documents():
            rows.append({
                "id": row["id"],
                "project_name": row["project_name"],
                "project_type": row["project_type"],
                "stage": row["stage"],
                "file_name": row["file_name"],
                "decision_label": row["decision_label"],
                "org_code": row["org_code"],
                "subfolder": row["subfolder"],
            })
        return {"documents": rows}

    @api.patch("/api/admin/documents/{doc_id}")
    def admin_mark_document(
        doc_id: int, role: str = Depends(require_admin), payload: dict = Body(...)
    ):
        if "valid_until" in payload:
            raw = payload["valid_until"] or None
            if raw is not None:
                try:
                    raw = datetime.strptime(str(raw)[:10], "%Y-%m-%d").date().isoformat()
                except ValueError:
                    raise HTTPException(400, "Дата: ГГГГ-ММ-ДД") from None
            row = state.store.set_valid_until(doc_id, raw)
        elif "manual_new" in payload:
            value = payload["manual_new"]
            if value is not None and not isinstance(value, bool):
                raise HTTPException(400, "manual_new должен быть true, false или null")
            row = state.store.set_manual_new(doc_id, value)
        else:
            raise HTTPException(400, "Нечего менять")
        if row is None:
            raise HTTPException(404, "Документ не найден")
        pub = state.store.latest_publication()
        mark_document(row, pub["id"] if pub else None)
        for hidden in ("sha256", "abs_path", "rel_path", "source_id"):
            row.pop(hidden, None)
        return row

    @api.get("/api/admin/committees")
    def admin_committees(role: str = Depends(require_admin)):
        return {"committees": committees_payload()}

    @api.patch("/api/admin/committees/{code}")
    def admin_update_committee(
        code: str, role: str = Depends(require_admin), payload: dict = Body(...)
    ):
        known = {c.code for c in cfg.committees}
        if code not in known:
            raise HTTPException(404, f"В справочнике нет комитета «{code}»")
        if "term_days" in payload:
            raw = payload["term_days"]
            try:
                days = None if raw in (None, "") else max(0, int(raw))
            except (TypeError, ValueError):
                raise HTTPException(400, "Срок — число дней или пусто") from None
            try:
                set_committee_field(state.committees_file, code, "term_days", None if days is None else str(days))
                state.reload_committees()
            except (OSError, ConfigError) as exc:
                raise HTTPException(500, f"Не удалось сохранить справочник: {exc}") from exc
            return {"committees": committees_payload()}
        active = payload.get("active")
        final = payload.get("final")
        if active is None and final is None:
            raise HTTPException(400, "Нечего менять")
        try:
            set_committee_flags(
                state.committees_file, code,
                active=None if active is None else bool(active),
                final=None if final is None else bool(final),
            )
            state.reload_committees()
        except OSError as exc:
            raise HTTPException(500, f"Не удалось сохранить справочник: {exc}") from exc
        return {"committees": committees_payload()}

    @api.get("/api/admin/backup")
    def admin_backup(role: str = Depends(require_admin)):
        st = state.backup.status()
        st["stale"] = backup_stale(st)
        return st

    @api.post("/api/admin/backup")
    def admin_backup_now(role: str = Depends(require_admin)):
        if state.backup.running:
            raise HTTPException(409, "Копирование уже идёт")
        threading.Thread(target=state.backup.run, name="backup-now", daemon=True).start()
        return {"started": True}

    @api.get("/api/admin/overview")
    def admin_overview(role: str = Depends(require_admin)):
        stats = state.store.stats_by_source()
        return {
            "publications": state.store.publications(),
            "watcher": state.watcher.state,
            "sources": [describe_source(r, stats) for r in state.store.sources()],
            "config": str(cfg.path),
        }

    register_release_routes(api, state, require_viewer, require_admin, browse_roots)
    register_upload_routes(api, state, require_viewer, require_admin)
    register_share_routes(api, state, require_admin, COOKIE_NAME, STATIC_DIR, PAGE_HEADERS)

    api.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return api
