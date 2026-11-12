"""Sendings: one release (or a manual entry) handed to one committee — and its answer.

The answer is found automatically: the first catalog document of that committee for
the same project and stage dated on/after the sending (and before the next sending to
the same committee — that one is the next round). Status, answer and dates can be
overridden by hand; «none» unlinks the automatic answer.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

DEFAULT_DAYS = 30          # planned answer time when no application says otherwise

STATUSES = {
    "waiting": "ждём",
    "approved": "согласовано",
    "refused": "отказ",          # remarks and refusal are the same thing for the owner
    "withdrawn": "отозвано",
}
# computed only: no answer, and the same committee got the documents again (next round)
AUTO_STATES = {"unanswered": "нет ответа, отправлено повторно"}
# catalog decision kind → sending status; info/unknown documents are not answers
KIND_STATUS = {"positive": "approved", "negative": "refused", "neutral": "refused"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS sendings (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    project_type   TEXT,
    project_name   TEXT NOT NULL,
    stage          TEXT,
    org_code       TEXT NOT NULL,
    sent_at        TEXT NOT NULL,          -- ISO date
    due            TEXT,                   -- planned answer date, ISO
    application_no TEXT,
    release_id     INTEGER REFERENCES releases(id) ON DELETE SET NULL,
    status         TEXT,                   -- manual status; NULL = automatic
    answer_mode    TEXT,                   -- NULL auto, 'manual' = answer_doc_id, 'none' = unlinked
    answer_doc_id  INTEGER,
    answer_date    TEXT,                   -- manual answer date, ISO
    note           TEXT,
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_sendings_key ON sendings(project_name, stage, org_code);
"""

EDITABLE = ("project_type", "project_name", "stage", "org_code", "sent_at", "due", "application_no",
            "status", "answer_mode", "answer_doc_id", "answer_date", "note")


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def plus_days(iso: str, days: int = DEFAULT_DAYS) -> str:
    return (date.fromisoformat(iso[:10]) + timedelta(days=days)).isoformat()


def _norm(s: str | None) -> str:
    return " ".join((s or "").lower().replace("ё", "е").split())


def doc_date(doc: dict) -> str | None:
    """Date of a catalog document: from its name if there is one, else when the file appeared."""
    return (doc.get("doc_date") or (doc.get("mtime") or "")[:10]) or None


class SendingStore:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)
        self._conn.commit()
        self._lock = threading.RLock()

    def all(self) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._conn.execute("SELECT * FROM sendings ORDER BY sent_at, id")]

    def get(self, sid: int) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM sendings WHERE id=?", (sid,)).fetchone()
            return dict(row) if row else None

    def for_release(self, release_id: int) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(
                "SELECT * FROM sendings WHERE release_id=? ORDER BY org_code", (release_id,))]

    def add(self, item: dict) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO sendings(project_type, project_name, stage, org_code, sent_at, due, application_no,"
                " release_id, note, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (item.get("project_type"), item["project_name"], item.get("stage"), item["org_code"],
                 item["sent_at"][:10], item.get("due"), item.get("application_no"),
                 item.get("release_id"), item.get("note"), _now()))
            self._conn.commit()
            return cur.lastrowid

    def update(self, sid: int, changes: dict) -> None:
        sets = {k: (v if v != "" else None) for k, v in changes.items() if k in EDITABLE}
        if not sets:
            return
        with self._lock:
            self._conn.execute(f"UPDATE sendings SET {', '.join(k + '=?' for k in sets)} WHERE id=?",
                               (*sets.values(), sid))
            self._conn.commit()

    def remap_answer(self, old_doc_id: int, new_doc_id: int) -> None:
        """A document got a new id (renamed, or an upload found on the PC): keep manual links."""
        with self._lock:
            self._conn.execute("UPDATE sendings SET answer_doc_id=? WHERE answer_doc_id=?", (new_doc_id, old_doc_id))
            self._conn.commit()

    def delete(self, sid: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM sendings WHERE id=?", (sid,))
            self._conn.commit()


def from_release(release: dict, stale_before: str | None = None, term=lambda code: DEFAULT_DAYS) -> list[dict]:
    """One sending per addressee of a release; number and planned date from its application."""
    sent = (release.get("released_at") or release.get("created_at") or "")[:10]
    if not sent or not release.get("project_name"):
        return []
    apps = [a for a in (release.get("applications") or [])
            if not (stale_before and a.get("filed") and a["filed"] < stale_before)]
    out = []
    for code in release.get("addressees") or []:
        app = next((a for a in apps if code in (a.get("committees") or [])), None)
        out.append({
            "project_type": release.get("project_type"), "project_name": release["project_name"],
            "stage": release.get("stage"), "org_code": code, "sent_at": sent,
            "due": (app or {}).get("due") or (plus_days(sent, term(code)) if term(code) else None),
            "application_no": (app or {}).get("number"), "release_id": release["id"],
        })
    return out


def compute(sendings: list[dict], docs: list[dict], today: str | None = None) -> list[dict]:
    """Adds round, answer, status, days and overdue to every sending."""
    today = today or date.today().isoformat()
    by_id = {d["id"]: d for d in docs}
    # catalog answers per (project, stage, committee), oldest first
    answers: dict[tuple, list[tuple[str, dict]]] = {}
    for d in docs:
        if d.get("decision_kind") not in KIND_STATUS or not d.get("org_code"):
            continue
        dd = doc_date(d)
        if not dd:
            continue
        key = (_norm(d.get("project_type")), _norm(d.get("project_name")), str(d.get("stage") or ""), d["org_code"])
        answers.setdefault(key, []).append((dd, d))
    for v in answers.values():
        v.sort(key=lambda x: (x[0], x[1]["id"]))

    groups: dict[tuple, list[dict]] = {}
    for s in sendings:
        key = (_norm(s.get("project_type")), _norm(s["project_name"]), str(s.get("stage") or ""), s["org_code"])
        groups.setdefault(key, []).append(s)

    out = []
    for key, items in groups.items():
        items.sort(key=lambda s: (s["sent_at"], s["id"]))
        for i, s in enumerate(items):
            nxt = items[i + 1]["sent_at"] if i + 1 < len(items) else None
            r = dict(s)
            r["round"] = i + 1
            doc = None
            if s.get("answer_mode") == "manual" and s.get("answer_doc_id"):
                doc = by_id.get(s["answer_doc_id"])
            elif s.get("answer_mode") != "none":
                doc = next((d for dd, d in answers.get(key, [])
                            if dd >= s["sent_at"] and (nxt is None or dd < nxt)), None)
            r["answer"] = None if not doc else {
                "id": doc["id"], "file_name": doc["file_name"], "decision_label": doc.get("decision_label"),
                "decision_kind": doc.get("decision_kind"), "date": doc_date(doc),
            }
            r["answer_auto"] = bool(doc) and s.get("answer_mode") != "manual"
            r["answered_at"] = s.get("answer_date") or (r["answer"] or {}).get("date")
            r["state"] = s.get("status") or (KIND_STATUS.get(doc.get("decision_kind")) if doc else None) or "waiting"
            if r["state"] == "waiting" and nxt:
                r["state"] = "unanswered"     # sent again before any answer came
            r["state_label"] = STATUSES.get(r["state"]) or AUTO_STATES.get(r["state"], r["state"])
            end = (r["answered_at"] if r["state"] not in ("waiting", "unanswered") and r["answered_at"]
                   else nxt if r["state"] == "unanswered" else today)
            try:
                r["days"] = (date.fromisoformat(end[:10]) - date.fromisoformat(s["sent_at"][:10])).days
            except ValueError:
                r["days"] = None
            r["overdue"] = r["state"] == "waiting" and bool(s.get("due")) and today > s["due"]
            out.append(r)
    out.sort(key=lambda r: (r["project_name"], r.get("stage") or "", r["org_code"], r["round"]))
    return out
