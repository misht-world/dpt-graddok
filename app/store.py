"""Хранилище: папки-источники, документы, публикации, отметки «новое»."""

from __future__ import annotations

import logging
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

log = logging.getLogger("store")

SCHEMA_VERSION = "2"
UPLOAD_PREFIX = "upload:"     # abs_path of documents uploaded by hand

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    path     TEXT NOT NULL UNIQUE,
    label    TEXT,
    enabled  INTEGER NOT NULL DEFAULT 1,
    added_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS publications (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at    TEXT NOT NULL,
    fingerprint   TEXT,
    files_total   INTEGER NOT NULL DEFAULT 0,
    files_new     INTEGER NOT NULL DEFAULT 0,
    files_changed INTEGER NOT NULL DEFAULT 0,
    files_moved   INTEGER NOT NULL DEFAULT 0,
    files_removed INTEGER NOT NULL DEFAULT 0,
    note          TEXT
);

CREATE TABLE IF NOT EXISTS documents (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id      INTEGER,
    abs_path       TEXT NOT NULL UNIQUE,
    rel_path       TEXT,
    project_key    TEXT,
    project_type   TEXT,
    project_name   TEXT,
    group_id       TEXT,
    group_title    TEXT,
    stage          TEXT,
    decision_id    TEXT,
    decision_label TEXT,
    decision_kind  TEXT,
    org_code       TEXT,
    org_name       TEXT,
    org_detail     TEXT,
    org_parent     TEXT,
    note           TEXT,
    file_name      TEXT,
    subfolder      TEXT,
    size           INTEGER,
    mtime          TEXT,
    doc_date       TEXT,
    sha256         TEXT,
    first_seen_at  TEXT,
    first_pub_id   INTEGER,
    last_pub_id    INTEGER,
    changed_pub_id INTEGER,
    status         TEXT,
    present        INTEGER NOT NULL DEFAULT 1,
    -- Ручная пометка «новое»: NULL — как посчитала система,
    -- 1 — принудительно новое, 0 — принудительно не новое.
    manual_new     INTEGER,
    -- Удалён из каталога вручную: файл на диске остаётся, но ни в таблицу,
    -- ни в выгрузку, ни в уведомления не попадает.
    hidden         INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_documents_project ON documents(project_key);
CREATE INDEX IF NOT EXISTS idx_documents_present ON documents(present);
CREATE INDEX IF NOT EXISTS idx_documents_sha ON documents(sha256);

-- Какие комитеты нужны проекту по техническому заданию.
-- Пусто или нет записи — требуются все, кто «в работе».
CREATE TABLE IF NOT EXISTS project_requirements (
    project_key   TEXT PRIMARY KEY,
    required_orgs TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

-- Projects known by name: kept by hand (АГПТ, Концепция, «Проект») or archived.
-- Folder-scanned projects need no row unless archived or given a folder.
CREATE TABLE IF NOT EXISTS projects (
    folder       TEXT PRIMARY KEY,      -- «АГПТ - Далта», «ДПТ - Далта»
    project_type TEXT,
    project_name TEXT NOT NULL,
    manual       INTEGER NOT NULL DEFAULT 0,
    pc_path      TEXT,                  -- folder of the project (server path), optional
    archived     INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

_COPY_FIELDS = (
    "source_id", "rel_path",
    "project_key", "project_type", "project_name", "group_id", "group_title",
    "stage", "decision_id", "decision_label", "decision_kind",
    "org_code", "org_name", "org_detail", "org_parent", "note",
    "file_name", "subfolder", "size", "mtime", "doc_date", "sha256",
)


def project_folder(project_key: str) -> str:
    """«6:ДПТ - Далта» (catalog key: source id + folder) → «ДПТ - Далта»."""
    head, sep, rest = (project_key or "").partition(":")
    return rest if sep and head.isdigit() else project_key


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class Store:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connect()

    def _connect(self) -> None:
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()
            self._migrate()
            self._add_missing_columns()

    def _migrate(self) -> None:
        """Схема развивается редко; при несовпадении версии индекс пересобирается.

        Теряется только журнал публикаций — сами документы собираются заново
        первым же сканированием.
        """
        row = self._conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        version = row["value"] if row else None
        if version == SCHEMA_VERSION:
            return
        # Since 10.11 the index holds data that exists nowhere else (uploads, manual marks,
        # ids that sendings point to): it is never dropped. New fields come via
        # _add_missing_columns; a real change of structure needs an explicit migration.
        if version is not None:
            log.warning("версия схемы индекса %s → %s: таблицы сохранены", version, SCHEMA_VERSION)
        self._conn.execute(
            "INSERT INTO meta(key,value) VALUES('schema_version',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (SCHEMA_VERSION,),
        )
        self._conn.commit()

    def _add_missing_columns(self) -> None:
        """Добавляет новые поля, не трогая уже накопленные данные.

        В отличие от смены версии схемы, здесь ничего не пересобирается —
        ручные пометки пользователя переживают обновление программы.
        """
        # 10.11: requirements were keyed «6:ДПТ - Далта»; the source id changes when a folder
        # is removed and added back, so they are keyed by the folder name now.
        for r in self._conn.execute("SELECT project_key FROM project_requirements").fetchall():
            new = project_folder(r["project_key"])
            if new != r["project_key"]:
                self._conn.execute(
                    "UPDATE OR IGNORE project_requirements SET project_key=? WHERE project_key=?",
                    (new, r["project_key"]))
        cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(documents)")}
        for name, ddl in (
            ("manual_new", "INTEGER"),
            ("hidden", "INTEGER NOT NULL DEFAULT 0"),
            # uploaded by hand on the server (abs_path «upload:…»), not found on the PC yet
            ("uploaded_at", "TEXT"),
            # «different documents»: the admin said an upload is not a duplicate
            ("similar_ok", "INTEGER"),
            # «Распоряжение»: valid until (entered by the admin); the red line of the plan-graph
            ("valid_until", "TEXT"),
        ):
            if name not in cols:
                self._conn.execute(f"ALTER TABLE documents ADD COLUMN {name} {ddl}")
        self._conn.commit()

    def set_manual_new(self, doc_id: int, value: bool | None) -> dict | None:
        """Ручная пометка «новое». None возвращает автоматический расчёт."""
        with self._lock:
            self._conn.execute(
                "UPDATE documents SET manual_new=? WHERE id=?",
                (None if value is None else (1 if value else 0), doc_id),
            )
            self._conn.commit()
        return self.document(doc_id)

    def set_valid_until(self, doc_id: int, value: str | None) -> dict | None:
        with self._lock:
            self._conn.execute("UPDATE documents SET valid_until=? WHERE id=?", (value, doc_id))
            self._conn.commit()
        return self.document(doc_id)

    def orders(self) -> list[dict]:
        """Present «Распоряжение» documents (decision id «order»), newest validity first."""
        rows = self._conn.execute(
            "SELECT id, project_key, project_type, project_name, file_name, note, valid_until, mtime "
            "FROM documents WHERE present=1 AND hidden=0 AND decision_id='order' "
            "ORDER BY valid_until IS NULL, valid_until DESC, mtime DESC").fetchall()
        return [dict(r) for r in rows]

    # -------------------------------------------- требуемые согласования

    def project_rows(self) -> dict[str, dict]:
        return {r["folder"]: dict(r) for r in self._conn.execute("SELECT * FROM projects").fetchall()}

    def save_project(self, folder: str, **fields) -> None:
        allowed = {"project_type", "project_name", "manual", "pc_path", "archived"}
        fields = {k: v for k, v in fields.items() if k in allowed}
        with self._lock:
            row = self._conn.execute("SELECT 1 FROM projects WHERE folder=?", (folder,)).fetchone()
            if row:
                if fields:
                    self._conn.execute(f"UPDATE projects SET {', '.join(k + '=?' for k in fields)} WHERE folder=?",
                                       (*fields.values(), folder))
            else:
                fields.setdefault("project_name", folder)
                cols = ["folder", *fields, "created_at"]
                self._conn.execute(f"INSERT INTO projects({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
                                   (folder, *fields.values(), _now()))
            self._conn.commit()

    def requirements(self) -> dict[str, list[str]]:
        """Organizations a project needs, by project folder («ДПТ - Далта»).

        A project without a row uses the standard set (committees «in work»)."""
        rows = self._conn.execute("SELECT * FROM project_requirements").fetchall()
        out: dict[str, list[str]] = {}
        for r in rows:
            codes = [c for c in (r["required_orgs"] or "").split(",") if c]
            out[project_folder(r["project_key"])] = codes
        return out

    def set_requirements(self, project_key: str, codes: list[str] | None) -> None:
        """None removes the setting: the project needs the standard set again."""
        project_key = project_folder(project_key)
        with self._lock:
            if codes is None:
                self._conn.execute(
                    "DELETE FROM project_requirements WHERE project_key=?", (project_key,)
                )
            else:
                self._conn.execute(
                    "INSERT INTO project_requirements(project_key, required_orgs, updated_at) "
                    "VALUES(?,?,?) ON CONFLICT(project_key) DO UPDATE SET "
                    "required_orgs=excluded.required_orgs, updated_at=excluded.updated_at",
                    (project_key, ",".join(codes), _now()),
                )
            self._conn.commit()

    # ------------------------------------------------------------------ meta

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self._conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO meta(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
            self._conn.commit()

    # --------------------------------------------------------------- sources

    def sources(self, enabled_only: bool = False) -> list[dict]:
        sql = "SELECT * FROM sources"
        if enabled_only:
            sql += " WHERE enabled=1"
        sql += " ORDER BY id"
        return [dict(r) for r in self._conn.execute(sql).fetchall()]

    def source(self, source_id: int) -> dict | None:
        row = self._conn.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
        return dict(row) if row else None

    def add_source(self, path: str, label: str = "") -> dict:
        normalized = str(Path(path).expanduser().resolve())
        with self._lock:
            existing = self._conn.execute(
                "SELECT * FROM sources WHERE path=?", (normalized,)
            ).fetchone()
            if existing:
                # Повторное добавление просто включает папку обратно.
                self._conn.execute("UPDATE sources SET enabled=1 WHERE id=?", (existing["id"],))
                self._conn.commit()
                return dict(self._conn.execute(
                    "SELECT * FROM sources WHERE id=?", (existing["id"],)
                ).fetchone())
            cur = self._conn.execute(
                "INSERT INTO sources(path,label,enabled,added_at) VALUES(?,?,1,?)",
                (normalized, label or "", _now()),
            )
            self._conn.commit()
            return dict(self._conn.execute(
                "SELECT * FROM sources WHERE id=?", (cur.lastrowid,)
            ).fetchone())

    def set_source_enabled(self, source_id: int, enabled: bool) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE sources SET enabled=? WHERE id=?", (1 if enabled else 0, source_id)
            )
            self._conn.commit()

    def set_source_label(self, source_id: int, label: str) -> None:
        with self._lock:
            self._conn.execute("UPDATE sources SET label=? WHERE id=?", (label, source_id))
            self._conn.commit()

    def remove_source(self, source_id: int) -> None:
        """Takes a folder out of the catalog. Its documents are kept as «gone» (present=0),
        like after switching the folder off: answers in «Сроки», manual marks and copies stay,
        and adding the folder back brings everything back with its history."""
        with self._lock:
            self._conn.execute(
                "UPDATE documents SET present=0, status='removed' WHERE source_id=? AND present=1", (source_id,))
            self._conn.execute("DELETE FROM sources WHERE id=?", (source_id,))
            self._conn.commit()

    # ----------------------------------------------------------- publication

    def latest_publication(self) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM publications ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None

    def publications(self, limit: int = 50) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM publications ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def publish(self, docs: Iterable[dict], fingerprint: str, note: str = "") -> dict:
        """Переносит результат сканирования в индекс и фиксирует публикацию.

        Возвращает статистику: сколько документов появилось, изменилось,
        переехало и исчезло относительно предыдущей публикации.
        """
        docs = list(docs)
        now = _now()

        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                "INSERT INTO publications(created_at, fingerprint, note) VALUES(?,?,?)",
                (now, fingerprint, note),
            )
            pub_id = cur.lastrowid

            existing = {
                r["abs_path"]: dict(r)
                for r in cur.execute("SELECT * FROM documents").fetchall()
            }
            seen_paths: set[str] = set()
            incoming_paths = {d["abs_path"] for d in docs}
            # Для распознавания переименований: хэш -> документ, которого больше нет.
            orphans_by_sha: dict[str, dict] = {}
            for path, row in existing.items():
                if row["present"] and path not in incoming_paths and row["sha256"]:
                    orphans_by_sha.setdefault(row["sha256"], row)

            counts = {"new": 0, "changed": 0, "moved": 0, "same": 0, "removed": 0}
            moved_pairs: list[tuple[int, str]] = []   # (old id, new path): links must follow

            for doc in docs:
                path = doc["abs_path"]
                seen_paths.add(path)
                prev = existing.get(path)
                sha = doc.get("sha256") or ""

                carried: dict = {}
                if prev is None:
                    moved_from = orphans_by_sha.pop(sha, None) if sha else None
                    if moved_from is not None:
                        status = "moved"
                        first_seen_at = moved_from["first_seen_at"]
                        first_pub_id = moved_from["first_pub_id"]
                        changed_pub_id = moved_from["changed_pub_id"]
                        # Вместе с историей переносим и ручные решения:
                        # переименование файла не повод забыть, что документ
                        # убрали из каталога или пометили новым.
                        carried = {
                            "hidden": moved_from["hidden"],
                            "manual_new": moved_from["manual_new"],
                            "valid_until": moved_from["valid_until"],
                        }
                        # Старая запись исчезает, её история переходит сюда.
                        cur.execute("DELETE FROM documents WHERE id=?", (moved_from["id"],))
                        moved_pairs.append((moved_from["id"], path))
                        existing.pop(moved_from["abs_path"], None)
                    else:
                        status = "new"
                        first_seen_at = now
                        first_pub_id = pub_id
                        changed_pub_id = pub_id
                elif not prev["present"]:
                    # Файл вернулся. Если содержимое то же (папку выключали и
                    # включили обратно) — это не новинка, историю сохраняем.
                    if sha and sha == prev["sha256"]:
                        status = "same"
                        first_seen_at = prev["first_seen_at"]
                        first_pub_id = prev["first_pub_id"]
                        changed_pub_id = prev["changed_pub_id"]
                    else:
                        status = "new"
                        first_seen_at = now
                        first_pub_id = pub_id
                        changed_pub_id = pub_id
                elif sha and prev["sha256"] and sha != prev["sha256"]:
                    status = "changed"
                    first_seen_at = prev["first_seen_at"]
                    first_pub_id = prev["first_pub_id"]
                    changed_pub_id = pub_id
                else:
                    status = "same"
                    first_seen_at = prev["first_seen_at"]
                    first_pub_id = prev["first_pub_id"]
                    changed_pub_id = prev["changed_pub_id"]

                counts[status] += 1

                values = [doc.get(f) for f in _COPY_FIELDS]
                cur.execute(
                    "INSERT INTO documents(abs_path,"
                    + ",".join(_COPY_FIELDS)
                    + ",first_seen_at,first_pub_id,last_pub_id,changed_pub_id,status,present) "
                    "VALUES(" + ",".join(["?"] * (len(_COPY_FIELDS) + 6)) + ",1) "
                    "ON CONFLICT(abs_path) DO UPDATE SET "
                    + ",".join(f"{f}=excluded.{f}" for f in _COPY_FIELDS)
                    + ",first_seen_at=excluded.first_seen_at"
                    ",first_pub_id=excluded.first_pub_id"
                    ",last_pub_id=excluded.last_pub_id"
                    ",changed_pub_id=excluded.changed_pub_id"
                    ",status=excluded.status"
                    ",present=1",
                    [path, *values, first_seen_at, first_pub_id, pub_id, changed_pub_id, status],
                )
                if carried:
                    cur.execute(
                        "UPDATE documents SET hidden=?, manual_new=?, valid_until=? WHERE abs_path=?",
                        (carried["hidden"], carried["manual_new"], carried["valid_until"], path),
                    )

            # Всё, чего не было в обходе, помечаем как удалённое —
            # из каталога такие документы пропадают.
            for path, row in existing.items():
                if path in seen_paths or not row["present"]:
                    continue
                if path.startswith(UPLOAD_PREFIX):
                    continue   # uploaded by hand: lives until it is found on the PC or deleted
                counts["removed"] += 1
                cur.execute(
                    "UPDATE documents SET present=0, status='removed', last_pub_id=? WHERE id=?",
                    (pub_id, row["id"]),
                )

            cur.execute(
                "UPDATE publications SET files_total=?, files_new=?, files_changed=?, "
                "files_moved=?, files_removed=? WHERE id=?",
                (
                    len(docs),
                    counts["new"],
                    counts["changed"],
                    counts["moved"],
                    counts["removed"],
                    pub_id,
                ),
            )
            moved_ids = []
            for old_id, new_path in moved_pairs:
                row = cur.execute("SELECT id FROM documents WHERE abs_path=?", (new_path,)).fetchone()
                if row:
                    moved_ids.append((old_id, row["id"]))
            self._conn.commit()

        return {
            "moved_ids": moved_ids,
            "publication_id": pub_id,
            "created_at": now,
            "files_total": len(docs),
            **{f"files_{k}": v for k, v in counts.items()},
        }

    # ------------------------------------------------------------- documents

    def documents(
        self, include_removed: bool = False, include_hidden: bool = False
    ) -> list[dict]:
        where = []
        if not include_removed:
            where.append("present=1")
        if not include_hidden:
            where.append("hidden=0")
        sql = "SELECT * FROM documents"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY mtime DESC, first_seen_at DESC"
        return [dict(r) for r in self._conn.execute(sql).fetchall()]

    def hidden_documents(self) -> list[dict]:
        """Удалённые вручную — чтобы их было видно и можно было вернуть."""
        rows = self._conn.execute(
            "SELECT * FROM documents WHERE hidden=1 AND present=1 "
            "ORDER BY project_name, file_name"
        ).fetchall()
        return [dict(r) for r in rows]

    def set_hidden(self, doc_id: int, value: bool) -> dict | None:
        """Удаляет документ из каталога или возвращает обратно.

        Файл на диске не трогаем: запись живёт в индексе и переживает
        пересборку, иначе удалённое возвращалось бы при каждом обходе папок.
        """
        with self._lock:
            self._conn.execute(
                "UPDATE documents SET hidden=? WHERE id=?", (1 if value else 0, doc_id)
            )
            self._conn.commit()
        row = self._conn.execute(
            "SELECT * FROM documents WHERE id=? AND present=1", (doc_id,)
        ).fetchone()
        return dict(row) if row else None

    def add_upload(self, doc: dict) -> int:
        """A document uploaded by hand: new in the latest publication, present until found on the PC."""
        pub = self.latest_publication()
        pub_id = pub["id"] if pub else None
        now = _now()
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO documents(abs_path," + ",".join(_COPY_FIELDS)
                + ",first_seen_at,first_pub_id,last_pub_id,changed_pub_id,status,present,uploaded_at) "
                "VALUES(" + ",".join(["?"] * (len(_COPY_FIELDS) + 6)) + ",1,?)",
                [doc["abs_path"], *[doc.get(f) for f in _COPY_FIELDS], now, pub_id, pub_id, pub_id, "new", now],
            )
            self._conn.commit()
            return cur.lastrowid

    def delete_document(self, doc_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM documents WHERE id=?", (doc_id,))
            self._conn.commit()

    def set_similar_ok(self, doc_id: int) -> None:
        with self._lock:
            self._conn.execute("UPDATE documents SET similar_ok=1 WHERE id=?", (doc_id,))
            self._conn.commit()

    def uses_blob(self, sha: str) -> bool:
        """A copy is still needed while a document that is not deleted points to it."""
        row = self._conn.execute(
            "SELECT 1 FROM documents WHERE sha256=? AND hidden=0 LIMIT 1", (sha,)
        ).fetchone()
        return row is not None

    def all_by_path(self) -> dict[str, dict]:
        return {r["abs_path"]: dict(r) for r in self._conn.execute("SELECT * FROM documents").fetchall()}

    def document_any(self, doc_id: int) -> dict | None:
        """Also documents that are gone from the PC (present=0): their copy may still be on the server."""
        row = self._conn.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
        return dict(row) if row else None

    def document(self, doc_id: int) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM documents WHERE id=? AND present=1", (doc_id,)
        ).fetchone()
        # Удалённые тоже возвращаем: их нужно показать в списке удалённых.
        return dict(row) if row else None

    def projects(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT project_key, project_type, project_name, COUNT(*) AS docs "
            "FROM documents WHERE present=1 AND hidden=0 "
            "GROUP BY project_key ORDER BY project_name"
        ).fetchall()
        required = self.requirements()
        out = []
        for r in rows:
            item = dict(r)
            item["required_orgs"] = required.get(project_folder(r["project_key"]))
            out.append(item)
        return out

    def stats_by_source(self) -> dict[int, dict]:
        rows = self._conn.execute(
            "SELECT source_id, COUNT(*) AS docs, COUNT(DISTINCT project_key) AS projects "
            "FROM documents WHERE present=1 AND hidden=0 GROUP BY source_id"
        ).fetchall()
        return {r["source_id"]: dict(r) for r in rows}

    def close(self) -> None:
        with self._lock:
            self._conn.close()
