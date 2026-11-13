"""Remarks of a committee letter: split into items, answers written in ГРАДДОК, exported to Word.

A sheet grows from a catalog document («Замечания», «Отказ»): its copy on the server is parsed
(text layer only, no OCR) with the parser copied from dpt-notes. Items carry the remark, the answer,
a note, a status and pictures in the answer and the note. Admins and «Исполнитель» edit, everyone
with an account reads. Word: «№ | Замечание | Ответ | Примечание», with or without the note column.
"""

from __future__ import annotations

import io
import json
import logging
import re
import sqlite3
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from fastapi import Body, Depends, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse

log = logging.getLogger("remarks")

STATUSES = {"work": "в работе", "done": "учтено", "disagree": "не согласны / пояснение"}
REMARK_KINDS = ("negative", "neutral")         # refusal, remarks: the documents that get a sheet
MAX_IMAGE = 10 * 1024 * 1024
IMAGE_TYPES = {b"\x89PNG": "image/png", b"\xff\xd8\xff": "image/jpeg", b"GIF8": "image/gif"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS remark_sheets (
    id          INTEGER PRIMARY KEY,
    doc_id      INTEGER NOT NULL UNIQUE,    -- the letter in the catalog
    letter_no   TEXT NOT NULL DEFAULT '',
    letter_date TEXT,
    text_ok     INTEGER NOT NULL DEFAULT 1, -- 0: no text layer (a scan), items are entered by hand
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS remark_items (
    id            INTEGER PRIMARY KEY,
    sheet_id      INTEGER NOT NULL REFERENCES remark_sheets(id) ON DELETE CASCADE,
    pos           INTEGER NOT NULL,
    remark        TEXT NOT NULL DEFAULT '',
    answer        TEXT NOT NULL DEFAULT '',
    note          TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL DEFAULT 'work',
    answer_images TEXT NOT NULL DEFAULT '[]',   -- [{"sha": …, "type": "image/png"}]
    note_images   TEXT NOT NULL DEFAULT '[]',
    repeat        TEXT NOT NULL DEFAULT '',     -- JSON: the same remark in an earlier letter (snapshot)
    note_blocks   TEXT NOT NULL DEFAULT ''      -- JSON: the note as text and pictures in order (13.11)
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def image_type(head: bytes) -> str | None:
    return next((t for magic, t in IMAGE_TYPES.items() if head.startswith(magic)), None)


# ------------------------------------------------------------------- parsing

_SPACED_LETTER = re.compile(r"(?<=[а-яё]) ([её]) (?=[а-яё])")   # «ч е ткость» → «чёткость»
_GOOD_NUMBER = re.compile(r"^[0-9][0-9A-Za-zА-Яа-я]*([-/][0-9A-Za-zА-Яа-я]+)+$")


def clean(text: str) -> str:
    text = _SPACED_LETTER.sub(lambda m: "ё" if m.group(1) in "её" else m.group(1), text or "")
    return re.sub(r"[ \t]+", " ", text).strip()


def parse_letter(path: Path) -> dict:
    """{text_ok, items: [str], number}; a scan gives text_ok=False and no items."""
    from .notes_parser import PDFParser

    parser = PDFParser(str(path))
    text = parser.extract_text() or ""
    if len(text.strip()) < 100:
        return {"text_ok": False, "items": [], "number": ""}
    meta = parser.extract_metadata() or {}
    items = [clean(r.get("text", "")) for r in parser.parse_remarks_by_paragraphs()]
    number = (meta.get("document_number") or "").strip()
    # the parser sometimes takes «Магистраль № 3»: only real letter numbers (01-19-12716/25) are kept
    return {"text_ok": True, "items": [x for x in items if x], "number": number if _GOOD_NUMBER.match(number) else ""}


# ------------------------------------------------------------------- repeats

REPEAT_RATIO = 0.9          # how alike two remarks must be to count as the same one (owner, 13.11)


def _norm(text: str) -> str:
    return " ".join(re.findall(r"[0-9a-zа-яё]+", (text or "").lower().replace("ё", "е")))


def similarity(a: str, b: str) -> float:
    from difflib import SequenceMatcher

    a, b = _norm(a), _norm(b)
    if not a or not b:
        return 0.0
    # quick reject: very different lengths cannot be the same remark
    if min(len(a), len(b)) / max(len(a), len(b)) < 0.5:
        return 0.0
    return SequenceMatcher(None, a, b, autojunk=False).ratio()


def find_repeats(items: list[dict], earlier: list[dict]) -> int:
    """Marks items that repeat a remark of an earlier letter; fills an empty answer / note from it.

    earlier: [{"sheet": sheet row, "items": [...]}] — letters of the same committee and project, older first.
    The newest matching letter wins. Returns how many items were marked.
    """
    found = 0
    for it in items:
        best = None
        for prev in earlier:
            for old in prev["items"]:
                r = similarity(it.get("remark", ""), old["remark"])
                if r >= REPEAT_RATIO and (best is None or r >= best[0]):
                    best = (r, prev["sheet"], old)
        if not best:
            continue
        ratio, sheet, old = best
        it["repeat"] = {"sheet_id": sheet["id"], "letter_no": sheet.get("letter_no") or "",
                        "letter_date": sheet.get("letter_date"), "remark": old["remark"], "answer": old["answer"],
                        "similarity": round(ratio, 2)}
        ref = "Повторное. См. письмо" + (f" № {sheet['letter_no']}" if sheet.get("letter_no") else "") \
            + (f" от {_ddmmyyyy(sheet.get('letter_date'))}" if sheet.get("letter_date") else "")
        if not (it.get("note") or "").strip():
            it["note"] = ref
            it["note_blocks"] = [{"t": "text", "v": ref}] + [b for b in note_blocks_of({**it, "note": ""}) if b["t"] == "img"]
        if not (it.get("answer") or "").strip() and old["answer"]:
            it["answer"] = old["answer"]          # a draft: the status stays «в работе» until checked
            it["answer_images"] = list(old.get("answer_images") or [])
        found += 1
    return found


def note_blocks_of(it: dict) -> list[dict]:
    """The note as [{"t": "text", "v": …} | {"t": "img", "sha": …, "type": …}].

    Sheets written before 13.11 have only the text and the pictures under it: they become
    «text, then the pictures», nothing is lost.
    """
    raw = it.get("note_blocks")
    if isinstance(raw, list):
        out = []
        for b in raw:
            if not isinstance(b, dict):
                continue
            if b.get("t") == "text" and isinstance(b.get("v"), str):
                if out and out[-1]["t"] == "text":
                    out[-1]["v"] += b["v"]
                else:
                    out.append({"t": "text", "v": b["v"]})
            elif b.get("t") == "img" and re.fullmatch(r"[0-9a-f]{64}", str(b.get("sha", ""))):
                out.append({"t": "img", "sha": b["sha"], "type": b.get("type")})
        return out
    out = [{"t": "text", "v": str(it.get("note") or "")}] if (it.get("note") or "").strip() else []
    out += [{"t": "img", "sha": i["sha"], "type": i.get("type")} for i in (it.get("note_images") or [])
            if isinstance(i, dict) and i.get("sha")]
    return out


# --------------------------------------------------------------------- store

class Remarks:
    def __init__(self, conn: sqlite3.Connection, lock: threading.RLock):
        self._conn = conn
        self._lock = lock
        with self._lock:
            self._conn.executescript(SCHEMA)
            cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(remark_items)")}
            if "repeat" not in cols:
                self._conn.execute("ALTER TABLE remark_items ADD COLUMN repeat TEXT NOT NULL DEFAULT ''")
            if "note_blocks" not in cols:
                self._conn.execute("ALTER TABLE remark_items ADD COLUMN note_blocks TEXT NOT NULL DEFAULT ''")
            self._conn.commit()

    def sheet(self, sid: int) -> dict | None:
        row = self._conn.execute("SELECT * FROM remark_sheets WHERE id=?", (sid,)).fetchone()
        return dict(row) if row else None

    def sheet_for_doc(self, doc_id: int) -> dict | None:
        row = self._conn.execute("SELECT * FROM remark_sheets WHERE doc_id=?", (doc_id,)).fetchone()
        return dict(row) if row else None

    def items(self, sid: int) -> list[dict]:
        rows = self._conn.execute("SELECT * FROM remark_items WHERE sheet_id=? ORDER BY pos", (sid,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["answer_images"] = json.loads(d["answer_images"] or "[]")
            d["note_images"] = json.loads(d["note_images"] or "[]")
            d["repeat"] = json.loads(d["repeat"]) if d.get("repeat") else None
            d["note_blocks"] = note_blocks_of({**d, "note_blocks": json.loads(d["note_blocks"]) if d.get("note_blocks") else None})
            out.append(d)
        return out

    def summary(self) -> dict[int, dict]:
        """doc_id → {sheet_id, total, done}: for the letters list and «Сроки»."""
        rows = self._conn.execute(
            "SELECT s.id, s.doc_id, COUNT(i.id) AS total, SUM(CASE WHEN i.status!='work' THEN 1 ELSE 0 END) AS done "
            "FROM remark_sheets s LEFT JOIN remark_items i ON i.sheet_id=s.id GROUP BY s.id").fetchall()
        return {r["doc_id"]: {"sheet_id": r["id"], "total": r["total"], "done": r["done"] or 0} for r in rows}

    def sheets(self) -> list[dict]:
        return [dict(r) for r in self._conn.execute("SELECT * FROM remark_sheets").fetchall()]

    def create(self, doc_id: int, parsed: dict, letter_date: str | None) -> int:
        now = _now()
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO remark_sheets(doc_id,letter_no,letter_date,text_ok,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                (doc_id, parsed["number"], letter_date, 1 if parsed["text_ok"] else 0, now, now))
            sid = cur.lastrowid
            self._write_items(sid, [{"remark": t} for t in parsed["items"]])
            self._conn.commit()
        return sid

    def _write_items(self, sid: int, items: list[dict]) -> None:
        self._conn.execute("DELETE FROM remark_items WHERE sheet_id=?", (sid,))
        for pos, it in enumerate(items, 1):
            status = it.get("status") if it.get("status") in STATUSES else "work"
            # the note: text and pictures in order; its plain text and picture list are kept alongside
            # (they serve the summary, repeats and the check that a picture is still in use)
            blocks = note_blocks_of(it)
            note = "\n".join(b["v"] for b in blocks if b["t"] == "text").strip()
            note_images = [{"sha": b["sha"], "type": b.get("type")} for b in blocks if b["t"] == "img"]
            self._conn.execute(
                "INSERT INTO remark_items(sheet_id,pos,remark,answer,note,status,answer_images,note_images,repeat,note_blocks) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (sid, pos, str(it.get("remark") or ""), str(it.get("answer") or ""), note, status,
                 json.dumps(it.get("answer_images") or [], ensure_ascii=False),
                 json.dumps(note_images, ensure_ascii=False),
                 json.dumps(it["repeat"], ensure_ascii=False) if isinstance(it.get("repeat"), dict) else "",
                 json.dumps(blocks, ensure_ascii=False)))

    def save(self, sid: int, version: str | None, fields: dict, items: list[dict] | None) -> dict:
        """Optimistic: the version is the sheet's updated_at seen by the editor."""
        with self._lock:
            sheet = self.sheet(sid)
            if not sheet:
                raise HTTPException(404, "Лист замечаний не найден")
            if version and version != sheet["updated_at"]:
                raise HTTPException(409, "Лист изменил кто-то другой — обновите страницу, ваши последние правки не сохранены")
            sets = {k: fields[k] for k in ("letter_no", "letter_date") if k in fields}
            sets["updated_at"] = _now()
            if sets["updated_at"] == sheet["updated_at"]:          # two saves within a second
                sets["updated_at"] = sets["updated_at"][:19] + ".1" + sets["updated_at"][19:]
            self._conn.execute("UPDATE remark_sheets SET " + ", ".join(f"{k}=?" for k in sets) + " WHERE id=?",
                               (*sets.values(), sid))
            if items is not None:
                self._write_items(sid, items)
            self._conn.commit()
        return self.sheet(sid)

    def set_items(self, sid: int, items: list[dict]) -> None:
        with self._lock:
            self._write_items(sid, items)
            self._conn.execute("UPDATE remark_sheets SET updated_at=? WHERE id=?", (_now(), sid))
            self._conn.commit()

    def replace_items(self, sid: int, parsed: dict) -> None:
        with self._lock:
            self._write_items(sid, [{"remark": t} for t in parsed["items"]])
            self._conn.execute("UPDATE remark_sheets SET text_ok=?, updated_at=? WHERE id=?",
                               (1 if parsed["text_ok"] else 0, _now(), sid))
            self._conn.commit()

    def remap_doc(self, old_id: int, new_id: int) -> None:
        """The letter got a new id in the catalog (renamed on the PC): the sheet stays with it."""
        with self._lock:
            if self.sheet_for_doc(new_id):        # the new id has its own sheet already: keep both apart
                return
            self._conn.execute("UPDATE remark_sheets SET doc_id=? WHERE doc_id=?", (new_id, old_id))
            self._conn.commit()

    def delete(self, sid: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM remark_items WHERE sheet_id=?", (sid,))
            self._conn.execute("DELETE FROM remark_sheets WHERE id=?", (sid,))
            self._conn.commit()

    def uses_blob(self, sha: str) -> bool:
        like = f'%"{sha}"%'
        return self._conn.execute(
            "SELECT 1 FROM remark_items WHERE answer_images LIKE ? OR note_images LIKE ? LIMIT 1", (like, like)
        ).fetchone() is not None


# ---------------------------------------------------------------------- Word

def build_word(sheet: dict, doc: dict, items: list[dict], blob_path, with_notes: bool) -> bytes:
    """The table as in dpt-notes: landscape, Cambria, header row repeated, rows not split."""
    from docx import Document
    from docx.enum.section import WD_ORIENT
    from docx.oxml import OxmlElement
    from docx.shared import Cm, Pt

    word = Document()
    section = word.sections[0]
    section.orientation = WD_ORIENT.LANDSCAPE
    section.page_width, section.page_height = Cm(27.94), Cm(21.59)
    section.top_margin, section.bottom_margin = Cm(1.25), Cm(3.17)
    section.left_margin = section.right_margin = Cm(2.54)

    def meta(label: str, value: str) -> None:
        if not value:
            return
        p = word.add_paragraph()
        p.paragraph_format.space_before = Pt(0)
        p.paragraph_format.space_after = Pt(0)
        run = p.add_run(label + ": ")
        run.bold = True
        run.font.name = "Cambria"
        p.add_run(value).font.name = "Cambria"

    project = " ".join(x for x in (doc.get("project_type"), doc.get("project_name")) if x)
    meta("Проект", project + (f", {doc['stage']} этап" if doc.get("stage") else ""))
    meta("Номер документа", sheet.get("letter_no") or "")
    meta("Дата", _ddmmyyyy(sheet.get("letter_date")))
    meta("Организация", doc.get("org_name") or doc.get("org_code") or "")

    widths = [1.0, 10.0, 7.0, 5.0] if with_notes else [1.0, 12.0, 10.0]
    heads = ["№", "Замечание", "Ответ", "Примечание"][:len(widths)]
    table = word.add_table(rows=1, cols=len(widths))
    table.style = "Light Grid Accent 1"
    for i, w in enumerate(widths):
        table.columns[i].width = Cm(w)
    for cell, text, w in zip(table.rows[0].cells, heads, widths):
        cell.width = Cm(w)
        cell.text = text
        for run in cell.paragraphs[0].runs:
            run.font.bold = True
            run.font.name = "Cambria"
    table.rows[0]._element.get_or_add_trPr().append(OxmlElement("w:tblHeader"))

    def fill(cell, text: str, images: list[dict], width_cm: float) -> None:
        cell.text = text or ""
        for run in cell.paragraphs[0].runs:
            run.font.name = "Cambria"
        for img in images or []:
            path = blob_path(img.get("sha", ""))
            if not path:
                continue
            try:
                cell.add_paragraph().add_run().add_picture(io.BytesIO(path.read_bytes()), width=Cm(width_cm - 0.4))  # a stream: the copy has no extension, Word needs «.png»
            except Exception as exc:  # noqa: BLE001  a broken picture must not break the export
                log.warning("картинка %s не вставлена: %s", img.get("sha"), exc)

    def fill_blocks(cell, blocks: list[dict], width_cm: float) -> None:
        """Text and pictures in the order they were written."""
        first = True
        for b in blocks:
            if b["t"] == "text":
                for line in b["v"].strip("\n").split("\n") if b["v"].strip() else []:
                    para = cell.paragraphs[0] if first else cell.add_paragraph()
                    para.add_run(line).font.name = "Cambria"
                    first = False
            else:
                path = blob_path(b.get("sha", ""))
                if not path:
                    continue
                para = cell.paragraphs[0] if first else cell.add_paragraph()
                try:
                    para.add_run().add_picture(io.BytesIO(path.read_bytes()), width=Cm(width_cm - 0.4))
                    first = False
                except Exception as exc:  # noqa: BLE001
                    log.warning("картинка %s не вставлена: %s", b.get("sha"), exc)

    for idx, it in enumerate(items, 1):
        row = table.add_row()
        cells = row.cells
        for cell, w in zip(cells, widths):
            cell.width = Cm(w)
        fill(cells[0], str(idx), [], widths[0])
        fill(cells[1], it["remark"], [], widths[1])
        fill(cells[2], it["answer"], it["answer_images"], widths[2])
        if with_notes:
            fill_blocks(cells[3], it.get("note_blocks") or note_blocks_of(it), widths[3])
        row._element.get_or_add_trPr().append(OxmlElement("w:cantSplit"))

    buf = io.BytesIO()
    word.save(buf)
    return buf.getvalue()


def word_name(kind: str, letters: list[dict], with_notes: bool = True) -> str:
    """Short names of the Word files (owner, 13.11: less information, but concise).

      Ответы ППТЛ Красносельское шоссе КТ 12.11.docx
      Сводная ППТЛ Красносельское шоссе КТ+КРТИ.docx
      Хронология ППТЛ Красносельское шоссе КТ.docx
      … «без прим» at the end when the note column is left out.
    The date (day.month) only for the answers to one letter: round 1 and round 2 differ by it.
    """
    types: list[str] = []
    names: list[str] = []
    for l in letters:
        if l.get("project_type") and l["project_type"] not in types:
            types.append(l["project_type"])
        name = (l.get("project_name") or "") + (f" эт{l['stage']}" if l.get("stage") else "")
        if name and name not in names:
            names.append(name)
    project = names[0] if len(names) == 1 else (f"{len(names)} проекта" if 1 < len(names) < 5 else
                                                f"{len(names)} проектов" if names else "")
    orgs: list[str] = []
    for l in letters:
        if l.get("org") and l["org"] not in orgs:
            orgs.append(l["org"])
    when = ""
    if kind == "answers" and letters and letters[0].get("letter_date"):
        m = re.match(r"^\d{4}-(\d{2})-(\d{2})", letters[0]["letter_date"])
        when = f"{m.group(2)}.{m.group(1)}" if m else ""
    what = {"answers": "Ответы", "combined": "Сводная", "chrono": "Хронология"}[kind]
    parts = [what, "+".join(types), project, "+".join(orgs[:4]) + ("+…" if len(orgs) > 4 else ""), when,
             "" if with_notes else "без прим"]
    name = " ".join(x for x in parts if x)
    name = re.sub(r'[\\/:*?"<>|]+', "_", name).strip(" .") or what
    return name[:120] + ".docx"


def _ddmmyyyy(iso: str | None) -> str:
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", iso or "")
    return f"{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else (iso or "")


# -------------------------------------------------------------------- routes

def register_remark_routes(api: FastAPI, state, require_viewer, require_editor, require_admin) -> None:
    store = Remarks(state.store._conn, state.store._lock)
    state.remarks = store

    def doc_or_404(doc_id: int) -> dict:
        doc = state.store.document_any(doc_id)
        if not doc or doc.get("hidden"):
            raise HTTPException(404, "Письмо не найдено")
        return doc

    def letter_path(doc: dict) -> Path:
        if doc.get("sha256") and state.blobs.has(doc["sha256"]):
            return state.blobs.path(doc["sha256"])
        path = Path(doc.get("abs_path") or "")
        if doc.get("present") and path.is_file():
            return path
        raise HTTPException(410, "Нет копии письма на сервере — нажмите «Актуализировать»")

    def doc_brief(doc: dict) -> dict:
        return {k: doc.get(k) for k in ("id", "file_name", "project_type", "project_name", "project_key", "stage",
                                        "org_code", "org_name", "decision_label", "decision_kind", "doc_date", "mtime")}

    def blob_path(sha: str):
        if re.fullmatch(r"[0-9a-f]{64}", sha or "") and state.blobs.has(sha):
            return state.blobs.path(sha)
        return None

    def earlier_sheets(sheet_id: int | None, doc: dict, letter_date: str | None) -> list[dict]:
        """Sheets of the same committee and project, from letters before this one (older first)."""
        out = []
        for sh in store.sheets():
            if sh["id"] == sheet_id:
                continue
            other = state.store.document_any(sh["doc_id"]) or {}
            if (other.get("org_code") != doc.get("org_code") or other.get("project_name") != doc.get("project_name")
                    or (other.get("project_type") or "") != (doc.get("project_type") or "")):
                continue
            if letter_date and sh.get("letter_date") and sh["letter_date"] >= letter_date:
                continue
            out.append({"sheet": sh, "items": store.items(sh["id"])})
        out.sort(key=lambda x: x["sheet"].get("letter_date") or "")
        return out

    def rounds(doc_id: int) -> dict:
        """Which sending this letter answers, and whether it was sent again after it."""
        computed = getattr(state, "sendings_computed", None)
        if not computed:
            return {}
        rows = computed()
        mine = next((s for s in rows if (s.get("answer") or {}).get("id") == doc_id), None)
        if not mine:
            return {}
        nxt = next((s for s in rows if s["org_code"] == mine["org_code"] and s["project_name"] == mine["project_name"]
                    and (s.get("stage") or "") == (mine.get("stage") or "") and s["round"] == mine["round"] + 1), None)
        return {"round": mine["round"], "sent_at": mine["sent_at"], "release_id": mine.get("release_id"),
                "next_sent_at": nxt["sent_at"] if nxt else None, "next_release_id": nxt.get("release_id") if nxt else None}

    def full(sid: int, role: str) -> dict:
        sheet = store.sheet(sid)
        if not sheet:
            raise HTTPException(404, "Лист замечаний не найден")
        doc = state.store.document_any(sheet["doc_id"]) or {}
        return {"sheet": sheet, "doc": doc_brief(doc), "items": store.items(sid), "statuses": STATUSES,
                "sending": rounds(sheet["doc_id"]),
                "can_edit": role in ("admin", "editor"), "can_delete": role == "admin"}

    @api.get("/api/remarks")
    def listing(role: str = Depends(require_viewer)):
        out = []
        summary = store.summary()
        for doc_id, s in summary.items():
            doc = state.store.document_any(doc_id)
            if not doc or doc.get("hidden"):
                continue
            sheet = store.sheet(s["sheet_id"])
            out.append({**s, "doc": doc_brief(doc), "letter_no": sheet["letter_no"], "letter_date": sheet["letter_date"],
                        "text_ok": bool(sheet["text_ok"]), "updated_at": sheet["updated_at"]})
        out.sort(key=lambda x: x["letter_date"] or "", reverse=True)
        return {"sheets": out, "can_edit": role in ("admin", "editor")}

    @api.post("/api/remarks")
    def create(payload: dict = Body(...), role: str = Depends(require_editor)):
        try:
            doc_id = int(payload.get("doc_id"))
        except (TypeError, ValueError):
            raise HTTPException(400, "Не указано письмо") from None
        have = store.sheet_for_doc(doc_id)
        if have:
            return {"id": have["id"], "created": False}
        doc = doc_or_404(doc_id)
        parsed = parse_letter(letter_path(doc))
        letter_date = (doc.get("doc_date") or doc.get("mtime") or "")[:10] or None
        sid = store.create(doc_id, parsed, letter_date)
        items = store.items(sid)
        found = find_repeats(items, earlier_sheets(sid, doc, letter_date))
        if found:
            store.set_items(sid, items)
        log.info("лист замечаний #%s: %s, пунктов %s, повторных %s", sid, doc.get("file_name"), len(items), found)
        return {"id": sid, "created": True, "text_ok": parsed["text_ok"], "count": len(items), "repeats": found}

    @api.post("/api/remarks/{sid}/repeats")
    def repeats(sid: int, role: str = Depends(require_editor)):
        """Look for repeats again (e.g. the earlier letter was answered later). Filled answers stay."""
        sheet = store.sheet(sid)
        if not sheet:
            raise HTTPException(404, "Лист замечаний не найден")
        doc = doc_or_404(sheet["doc_id"])
        items = store.items(sid)
        for it in items:
            it["repeat"] = None
        found = find_repeats(items, earlier_sheets(sid, doc, sheet.get("letter_date")))
        store.set_items(sid, items)
        return {**full(sid, role), "found": found}

    @api.get("/api/remarks/{sid}")
    def get_sheet(sid: int, role: str = Depends(require_viewer)):
        return full(sid, role)

    @api.put("/api/remarks/{sid}")
    def save(sid: int, payload: dict = Body(...), role: str = Depends(require_editor)):
        items = payload.get("items")
        if items is not None and not isinstance(items, list):
            raise HTTPException(400, "Неверный список пунктов")
        for it in items or []:
            for key in ("answer_images", "note_images"):
                it[key] = [{"sha": i["sha"], "type": i.get("type")} for i in (it.get(key) or [])
                           if isinstance(i, dict) and blob_path(i.get("sha", ""))]
            if isinstance(it.get("note_blocks"), list):
                it["note_blocks"] = [b for b in it["note_blocks"]
                                     if isinstance(b, dict) and (b.get("t") != "img" or blob_path(str(b.get("sha", ""))))]
        fields = {}
        if "letter_no" in payload:
            fields["letter_no"] = str(payload["letter_no"] or "").strip()[:100]
        if "letter_date" in payload:
            raw = str(payload["letter_date"] or "")[:10]
            fields["letter_date"] = raw if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw) else None
        sheet = store.save(sid, payload.get("version"), fields, items)
        return {"version": sheet["updated_at"]}

    @api.post("/api/remarks/{sid}/reparse")
    def reparse(sid: int, role: str = Depends(require_editor)):
        sheet = store.sheet(sid)
        if not sheet:
            raise HTTPException(404, "Лист замечаний не найден")
        parsed = parse_letter(letter_path(doc_or_404(sheet["doc_id"])))
        store.replace_items(sid, parsed)
        return full(sid, role)

    @api.delete("/api/remarks/{sid}")
    def delete(sid: int, role: str = Depends(require_admin)):
        store.delete(sid)
        return {"ok": True}

    @api.post("/api/remarks-images")
    async def upload_image(file: UploadFile = File(...), role: str = Depends(require_editor)):
        data = await file.read(MAX_IMAGE + 1)
        if len(data) > MAX_IMAGE:
            raise HTTPException(400, "Картинка больше 10 МБ")
        kind = image_type(data[:8])
        if not kind:
            raise HTTPException(400, "Только картинки PNG, JPEG или GIF (их понимает Word)")
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp.write(data)
        try:
            sha, _size, _new = state.blobs.put(Path(tmp.name))
        finally:
            Path(tmp.name).unlink(missing_ok=True)
        return {"sha": sha, "type": kind}

    @api.get("/remarks-image/{sha}")
    def image(sha: str, role: str = Depends(require_viewer)):
        path = blob_path(sha)
        if not path:
            raise HTTPException(404, "Картинка не найдена")
        with open(path, "rb") as fh:
            kind = image_type(fh.read(8))
        if not kind:
            raise HTTPException(404, "Картинка не найдена")
        return FileResponse(path, media_type=kind, headers={"Cache-Control": "private, max-age=86400",
                                                            "X-Content-Type-Options": "nosniff"})

    def summary_of(ids: str) -> dict:
        from .remarks_summary import build_summary

        try:
            wanted = [int(x) for x in ids.split(",") if x.strip()]
        except ValueError:
            raise HTTPException(400, "Неверный список листов") from None
        if not wanted:
            raise HTTPException(400, "Не выбраны листы")
        sheets = []
        for sid in dict.fromkeys(wanted):
            sheet = store.sheet(sid)
            if not sheet:
                raise HTTPException(404, f"Лист {sid} не найден")
            sheets.append({"sheet": sheet, "doc": state.store.document_any(sheet["doc_id"]) or {}, "items": store.items(sid)})
        return build_summary(sheets)

    @api.get("/api/remarks-summary")
    def summary(ids: str, role: str = Depends(require_viewer)):
        return summary_of(ids)

    @api.get("/remarks-summary/word")
    def summary_word(ids: str, mode: str = "combined", notes: int = 1, role: str = Depends(require_viewer)):
        from .remarks_summary import build_summary_word

        mode = "chrono" if mode == "chrono" else "combined"
        table = summary_of(ids)
        name = word_name(mode, [{"project_type": l.get("project_type"), "project_name": l.get("project_name"),
                                 "stage": l.get("stage"), "org": l.get("org"), "letter_date": l.get("letter_date")}
                                for l in table["letters"]], bool(notes) or mode == "chrono")
        data = build_summary_word(table, mode, bool(notes), blob_path)
        return StreamingResponse(io.BytesIO(data),
                                 media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                                 headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"})

    @api.get("/remarks/{sid}/word")
    def word(sid: int, notes: int = 1, role: str = Depends(require_viewer)):
        sheet = store.sheet(sid)
        if not sheet:
            raise HTTPException(404, "Лист замечаний не найден")
        doc = state.store.document_any(sheet["doc_id"]) or {}
        data = build_word(sheet, doc, store.items(sid), blob_path, bool(notes))
        name = word_name("answers", [{"project_type": doc.get("project_type"), "project_name": doc.get("project_name"),
                                      "stage": doc.get("stage"), "org": doc.get("org_code"),
                                      "letter_date": sheet.get("letter_date")}], bool(notes))
        return StreamingResponse(io.BytesIO(data),
                                 media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                                 headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"})
