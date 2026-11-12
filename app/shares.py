"""Links for people without an account: one letter, one release or one file of a release, for 30 days.

The token is random (128 bits) and is the only key: who made a link is not stored (decision of 12.11).
Someone who is signed in is sent to the same object inside the program; anyone else gets a small page
with only that object. Expired or disabled links answer «срок истёк» / «ссылка отключена».
"""

from __future__ import annotations

import secrets
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

from fastapi import Body, Cookie, Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse, RedirectResponse

DAYS = 30
KINDS = ("doc", "release", "rfile")

SCHEMA = """
CREATE TABLE IF NOT EXISTS shares (
    token      TEXT PRIMARY KEY,
    kind       TEXT NOT NULL,             -- doc | release | rfile
    target_id  INTEGER NOT NULL,          -- document id, release id or release file id
    release_id INTEGER,                   -- for rfile
    title      TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked    INTEGER NOT NULL DEFAULT 0,
    hits       INTEGER NOT NULL DEFAULT 0,
    last_hit   TEXT
);
"""


def _now() -> datetime:
    return datetime.now(timezone.utc).astimezone()


class Shares:
    def __init__(self, conn: sqlite3.Connection, lock: threading.RLock):
        self._conn = conn
        self._lock = lock
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    @staticmethod
    def state(row: dict) -> str:
        if row["revoked"]:
            return "revoked"
        return "expired" if datetime.fromisoformat(row["expires_at"]) < _now() else "active"

    def get(self, token: str) -> dict | None:
        row = self._conn.execute("SELECT * FROM shares WHERE token=?", (token,)).fetchone()
        return dict(row) if row else None

    def create(self, kind: str, target_id: int, release_id: int | None, title: str) -> dict:
        """An active link to the same object is reused: one object — one link at a time."""
        with self._lock:
            for row in self._conn.execute(
                    "SELECT * FROM shares WHERE kind=? AND target_id=? AND revoked=0", (kind, target_id)).fetchall():
                if self.state(dict(row)) == "active":
                    return dict(row)
            now = _now()
            token = secrets.token_urlsafe(16)
            self._conn.execute(
                "INSERT INTO shares(token,kind,target_id,release_id,title,created_at,expires_at) VALUES(?,?,?,?,?,?,?)",
                (token, kind, target_id, release_id, title, now.isoformat(timespec="seconds"),
                 (now + timedelta(days=DAYS)).isoformat(timespec="seconds")))
            self._conn.commit()
        return self.get(token)

    def revoke(self, token: str) -> None:
        with self._lock:
            self._conn.execute("UPDATE shares SET revoked=1 WHERE token=?", (token,))
            self._conn.commit()

    def hit(self, token: str) -> None:
        with self._lock:
            self._conn.execute("UPDATE shares SET hits=hits+1, last_hit=? WHERE token=?",
                               (_now().isoformat(timespec="seconds"), token))
            self._conn.commit()

    def all(self) -> list[dict]:
        rows = [dict(r) for r in self._conn.execute("SELECT * FROM shares ORDER BY created_at DESC").fetchall()]
        for r in rows:
            r["state"] = self.state(r)
        return rows


def register_share_routes(api: FastAPI, state, require_admin, cookie_name: str, static_dir, page_headers) -> None:
    shares = Shares(state.store._conn, state.store._lock)
    state.shares = shares

    def project_text(row: dict) -> str:
        return " · ".join(x for x in (row.get("project_type"), row.get("project_name")) if x)

    def describe(kind: str, target_id: int, release_id: int | None) -> tuple[str, int | None]:
        """Title of the object and its release; 404 when it is gone."""
        if kind == "doc":
            doc = state.store.document_any(target_id)
            if not doc or doc.get("hidden"):
                raise HTTPException(404, "Документ не найден")
            return f"{doc['file_name']} ({project_text(doc)})", None
        rid = target_id if kind == "release" else release_id
        r = state.releases.release(rid) if rid else None
        if not r:
            raise HTTPException(404, "Выпуск не найден")
        rtitle = r.get("title") or project_text(r)
        if kind == "release":
            return f"Выпуск {rtitle}", rid
        f = state.releases.file(rid, target_id)
        if not f:
            raise HTTPException(404, "Файл не найден")
        return f"{f['rel_path'].rsplit('/', 1)[-1]} (выпуск {rtitle})", rid

    def public(row: dict) -> dict:
        return {"token": row["token"], "url": "/s/" + row["token"], "kind": row["kind"], "title": row["title"],
                "created_at": row["created_at"], "expires_at": row["expires_at"],
                "state": shares.state(row), "hits": row["hits"], "last_hit": row["last_hit"]}

    def active(token: str) -> dict:
        row = shares.get(token)
        if not row:
            raise HTTPException(404, "Ссылка не найдена")
        st = shares.state(row)
        if st == "revoked":
            raise HTTPException(410, "Ссылка отключена")
        if st == "expired":
            raise HTTPException(410, "Срок действия ссылки истёк")
        return row

    # ---------------------------------------------------------------- admin

    @api.post("/api/admin/shares")
    def create(payload: dict = Body(...), role: str = Depends(require_admin)):
        kind = payload.get("kind")
        if kind not in KINDS:
            raise HTTPException(400, "Неизвестный вид ссылки")
        try:
            target = int(payload.get("id"))
            rid = int(payload["release_id"]) if payload.get("release_id") else None
        except (TypeError, ValueError):
            raise HTTPException(400, "Неверный номер") from None
        title, rid = describe(kind, target, rid)
        return public(shares.create(kind, target, rid, title))

    @api.get("/api/admin/shares")
    def listing(role: str = Depends(require_admin)):
        return {"shares": [public(r) for r in shares.all()], "days": DAYS}

    @api.post("/api/admin/shares/{token}/revoke")
    def revoke(token: str, role: str = Depends(require_admin)):
        if not shares.get(token):
            raise HTTPException(404, "Ссылка не найдена")
        shares.revoke(token)
        return {"shares": [public(r) for r in shares.all()]}

    # ---------------------------------------------------------------- public

    @api.get("/s/{token}")
    def open_link(token: str, session: str | None = Cookie(default=None, alias=cookie_name)):
        row = shares.get(token)
        if row and shares.state(row) == "active" and state.role_of(session):
            # someone with an account: the same object inside the program
            target = {"doc": f"/#doc={row['target_id']}", "release": f"/#release={row['target_id']}",
                      "rfile": f"/#release={row['release_id']}&file={row['target_id']}"}[row["kind"]]
            return RedirectResponse(target, status_code=303)
        if row and shares.state(row) == "active":
            shares.hit(token)
        # the page itself reads /api/s/{token} and says if the link is expired or disabled
        return FileResponse(static_dir / "share.html", headers={**page_headers, "X-Robots-Tag": "noindex"})

    @api.get("/api/s/{token}")
    def link_data(token: str):
        row = active(token)
        out = {"kind": row["kind"], "expires_at": row["expires_at"]}
        if row["kind"] == "doc":
            doc = state.store.document_any(row["target_id"])
            if not doc or doc.get("hidden"):
                raise HTTPException(404, "Документ удалён из каталога")
            out["doc"] = {"file_name": doc["file_name"], "project": project_text(doc), "org": doc.get("org_code"),
                          "decision": doc.get("decision_label"), "stage": doc.get("stage"),
                          "date": doc.get("doc_date") or doc.get("mtime"), "size": doc.get("size")}
            return out
        r = state.releases.release(row["release_id"] if row["kind"] == "rfile" else row["target_id"])
        if not r:
            raise HTTPException(404, "Выпуск удалён")
        out["release"] = {"title": r.get("title"), "project": project_text(r), "stage": r.get("stage"),
                          "released_at": r.get("released_at")}
        files = state.releases.files(r["id"])
        if row["kind"] == "rfile":
            files = [f for f in files if f["id"] == row["target_id"]]
            if not files:
                raise HTTPException(404, "Файл удалён из выпуска")
        out["files"] = [{"id": f["id"], "rel_path": f["rel_path"], "size": f["size"]} for f in files]
        return out

    def check_file(row: dict, file_id: int | None) -> int:
        """The release id the file belongs to, if this link gives that file."""
        if row["kind"] == "release":
            if file_id is None or not state.releases.file(row["target_id"], file_id):
                raise HTTPException(404, "Файл не найден")
            return row["target_id"]
        if row["kind"] == "rfile" and file_id == row["target_id"]:
            return row["release_id"]
        raise HTTPException(404, "Файл не найден")

    @api.get("/s/{token}/doc")
    def link_doc(token: str, dl: int = 0):
        row = active(token)
        if row["kind"] != "doc":
            raise HTTPException(404, "Файл не найден")
        return state.send_doc(row["target_id"], "attachment" if dl else "inline")

    @api.get("/s/{token}/file/{file_id}")
    def link_file(token: str, file_id: int, dl: int = 0):
        row = active(token)
        rid = check_file(row, file_id)
        return state.release_file_response(rid, file_id, dl=dl, role="viewer")

    @api.get("/s/{token}/zip")
    def link_zip(token: str):
        row = active(token)
        if row["kind"] != "release":
            raise HTTPException(404, "Архив доступен только по ссылке на выпуск")
        return state.release_zip_response(row["target_id"], prefix="", flat=0, role="viewer")
