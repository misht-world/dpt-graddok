"""Releases: what was sent, to whom and when.

A release is one folder inside a dpt-backup snapshot, e.g.
  .../ДПТ - Далта/2026-11-05_13-10-20_КГА/09_Выпуск/Этап 02 - 02_КГА/ДПТ-Далта_Эт2_КГА
Its files are copied byte for byte into a content-addressed store
(data/blobs/ab/<sha256>), names and paths live in data/releases.db.
Nothing is ever written to the source folders.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import uuid
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePath

from .config import Config
from .parsing import build_org_matchers, match_org, normalize

# Files never taken from a release (decision of the owner): archives and signatures,
# plus temporary and auxiliary files of CAD/Office/Windows.
SKIP_EXTENSIONS = {
    ".zip", ".sig",
    ".bak", ".dwl", ".dwl2", ".sv$", ".ac$", ".$$$", ".tmp", ".temp", ".log", ".err", ".lck",
    ".plt", ".plo",
}
SKIP_NAMES = {"thumbs.db", "desktop.ini", ".ds_store", "plot.log"}
SKIP_PREFIXES = ("~$", ".~", ".", "~")

_SNAPSHOT_RX = re.compile(r"^(\d{4})-(\d{2})-(\d{2})_(\d{2})-(\d{2})-(\d{2})(?:_(.*))?$")
_STAGE_RX = re.compile(
    r"(?:этап\s*[№#]?\s*0*(\d{1,2})(?!\d))|(?:(?<![а-яёa-z])эт\.?\s*0*(\d{1,2})(?!\d))|(?:(\d)\s*[-–]?\s*(?:й\s*)?этап)",
    re.IGNORECASE,
)
_PROJECT_RX = re.compile(r"^\s*([A-Za-zА-Яа-яЁё]{2,10})\s*-\s*(.+?)\s*$")
_IOGV_RX = re.compile(r"(?<![А-Яа-яЁё])ИОГВ(?![А-Яа-яЁё])", re.IGNORECASE)
_COPY_SUFFIX_RX = re.compile(r"_\d+$")


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


# --------------------------------------------------------------------- parsing

@dataclass
class Snapshot:
    when: datetime
    label: str


def parse_snapshot_name(name: str) -> Snapshot | None:
    """`2026-02-16_01-22-30_КЭИО_КТ` → date and the free-text label."""
    m = _SNAPSHOT_RX.match(normalize(name))
    if not m:
        return None
    y, mo, d, h, mi, s, label = m.groups()
    try:
        when = datetime(int(y), int(mo), int(d), int(h), int(mi), int(s))
    except ValueError:
        return None
    return Snapshot(when, (label or "").strip())


def find_stage(parts: list[str]) -> str | None:
    """«Этап 02 - 01_ИОГВ», «2 этап», «..._Эт2_...» → "2"."""
    for part in parts:
        m = _STAGE_RX.search(part)
        if m:
            return str(int(next(g for g in m.groups() if g)))
    return None


def split_project(folder: str, config: Config) -> tuple[str | None, str]:
    """`ДПТ - Далта` → ("ДПТ", "Далта"); unknown type → (None, folder)."""
    name = normalize(folder)
    m = _PROJECT_RX.match(name)
    if m and m.group(1).upper() in config.project_types:
        return m.group(1).upper(), m.group(2)
    return None, name


def find_orgs(text: str, matchers) -> list[str]:
    """All committee codes mentioned in a free-text label, in order, without repeats."""
    found: list[str] = []
    rest = normalize(text).replace("_", " ")
    while rest:
        m = match_org(rest, matchers)
        if not m:
            break
        if m.code not in found:
            found.append(m.code)
        rest = rest[m.span[1]:]
    return found


@dataclass
class Suggestion:
    """What the registration form is pre-filled with; the admin confirms or edits."""
    path: str
    snapshot: str | None = None
    released_at: str | None = None        # ISO date-time from the snapshot name
    label: str = ""
    project_folder: str | None = None     # folder name the release belongs to
    project_type: str | None = None
    project_name: str | None = None
    stage: str | None = None
    title: str = ""
    addressees: list[str] = field(default_factory=list)
    iogv: bool = False                    # label said «ИОГВ»: expanded to required/active committees
    notes: list[str] = field(default_factory=list)


def suggest(path: Path, config: Config, required: dict[str, list[str]] | None = None) -> Suggestion:
    parts = list(PurePath(path).parts)
    sug = Suggestion(path=str(path), title=normalize(parts[-1]) if parts else "")

    snap_idx = None
    for i, part in enumerate(parts):
        if parse_snapshot_name(part):
            snap_idx = i
    if snap_idx is None:
        sug.notes.append("В пути нет папки среза вида ГГГГ-ММ-ДД_чч-мм-сс_МЕТКА — дату укажите вручную.")
        after = parts
        container = None
    else:
        snap = parse_snapshot_name(parts[snap_idx])
        sug.snapshot = parts[snap_idx]
        sug.released_at = snap.when.isoformat(timespec="minutes")
        sug.label = _COPY_SUFFIX_RX.sub("", snap.label)
        container = parts[snap_idx - 1] if snap_idx > 0 else None
        after = parts[snap_idx + 1:]

    # Project: a «TYPE - name» folder right after the snapshot (ППТЛ container
    # with sub-projects), otherwise the folder that holds the snapshots.
    project_folder = None
    if after and split_project(after[0], config)[0]:
        project_folder = after[0]
    elif container:
        project_folder = container
    if project_folder:
        sug.project_folder = normalize(project_folder)
        sug.project_type, sug.project_name = split_project(project_folder, config)

    sug.stage = find_stage(after)

    matchers = build_org_matchers(config)
    orgs = find_orgs(sug.label, matchers) if sug.label else []
    if sug.label and _IOGV_RX.search(sug.label):
        sug.iogv = True
        req = (required or {}).get(sug.project_folder or "") if required else None
        pool = req or [c.code for c in config.committees if c.active and not c.final]
        for code in pool:
            if code not in orgs:
                orgs.append(code)
    if not orgs:
        # nothing in the snapshot label — look at the release folders («02_КГА»)
        for part in after[1:]:
            orgs = find_orgs(part, matchers)
            if orgs:
                break
    if not orgs:
        sug.notes.append("Адресаты не распознаны — отметьте их вручную.")
    sug.addressees = orgs
    return sug


# ---------------------------------------------------------------- file listing

def wanted(path: Path) -> bool:
    name = path.name.lower()
    return (not path.name.startswith(SKIP_PREFIXES) and path.suffix.lower() not in SKIP_EXTENSIONS
            and name not in SKIP_NAMES and not name.endswith("_plot.log"))


def list_release_files(root: Path) -> list[Path]:
    files = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for name in sorted(filenames):
            p = Path(dirpath) / name
            if wanted(p):
                files.append(p)
    return files


# ---------------------------------------------------------------- blob store

class BlobStore:
    """Content-addressed copies: data/blobs/<first 2 hex>/<sha256>."""

    # A copy written less than this long ago is kept even if nothing refers to it yet:
    # a registration or a scan may be about to record it (they commit in batches).
    FRESH_SECONDS = 3600

    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._fresh: dict[str, float] = {}

    def fresh(self, sha: str) -> bool:
        import time as _t
        t = self._fresh.get(sha)
        return t is not None and _t.time() - t < self.FRESH_SECONDS

    def path(self, sha: str) -> Path:
        return self.root / sha[:2] / sha

    def has(self, sha: str) -> bool:
        return self.path(sha).is_file()

    def delete(self, sha: str) -> bool:
        try:
            self.path(sha).unlink()
            return True
        except FileNotFoundError:
            return False

    def put(self, src: Path, chunk: int = 1 << 20) -> tuple[str, int, bool]:
        """Copies a file in while hashing it. Returns (sha256, size, was_new)."""
        tmp = self.root / f".tmp-{uuid.uuid4().hex}"
        h = hashlib.sha256()
        size = 0
        try:
            with open(src, "rb") as fin, open(tmp, "wb") as fout:
                while True:
                    buf = fin.read(chunk)
                    if not buf:
                        break
                    h.update(buf)
                    fout.write(buf)
                    size += len(buf)
            sha = h.hexdigest()
            import time as _t
            self._fresh[sha] = _t.time()
            dest = self.path(sha)
            if dest.is_file():
                tmp.unlink()
                return sha, size, False
            dest.parent.mkdir(parents=True, exist_ok=True)
            os.replace(tmp, dest)
            return sha, size, True
        finally:
            if tmp.exists():
                tmp.unlink()


# --------------------------------------------------------------------- storage

SCHEMA = """
CREATE TABLE IF NOT EXISTS releases (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    project_folder TEXT,
    project_type   TEXT,
    project_name   TEXT,
    stage          TEXT,
    title          TEXT NOT NULL,
    released_at    TEXT,
    snapshot       TEXT,
    label          TEXT,
    source_path    TEXT NOT NULL,
    files_count    INTEGER NOT NULL DEFAULT 0,
    size_total     INTEGER NOT NULL DEFAULT 0,
    note           TEXT,
    status         TEXT NOT NULL DEFAULT 'ready',
    created_at     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS release_addressees (
    release_id INTEGER NOT NULL REFERENCES releases(id) ON DELETE CASCADE,
    org_code   TEXT NOT NULL,
    PRIMARY KEY (release_id, org_code)
);
CREATE TABLE IF NOT EXISTS release_files (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    release_id INTEGER NOT NULL REFERENCES releases(id) ON DELETE CASCADE,
    rel_path   TEXT NOT NULL,
    sha256     TEXT NOT NULL,
    size       INTEGER NOT NULL,
    mtime      TEXT
);
CREATE INDEX IF NOT EXISTS ix_release_files_release ON release_files(release_id);
"""


def _decode(r: dict) -> dict:
    raw = r.get("applications")
    r["applications"] = json.loads(raw) if raw else ([] if raw == "" else None)
    return r


class ReleaseStore:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(releases)")}
        if "applications" not in cols:   # added after stage 1 went live
            self._conn.execute("ALTER TABLE releases ADD COLUMN applications TEXT")
        if "sendings_made" not in cols:   # stage 2: sendings were created from this release
            self._conn.execute("ALTER TABLE releases ADD COLUMN sendings_made INTEGER")
        self._conn.commit()
        self._lock = threading.RLock()

    def create(self, meta: dict, addressees: list[str]) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO releases(project_folder, project_type, project_name, stage, title, released_at,"
                " snapshot, label, source_path, note, status, created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?, 'copying', ?)",
                (meta.get("project_folder"), meta.get("project_type"), meta.get("project_name"),
                 meta.get("stage"), meta["title"], meta.get("released_at"), meta.get("snapshot"),
                 meta.get("label"), meta["source_path"], meta.get("note"), _now()),
            )
            rid = cur.lastrowid
            self._conn.executemany(
                "INSERT OR IGNORE INTO release_addressees(release_id, org_code) VALUES(?,?)",
                [(rid, code) for code in addressees],
            )
            self._conn.commit()
            return rid

    def add_files(self, release_id: int, rows: list[tuple[str, str, int, str]]) -> None:
        with self._lock:
            self._conn.executemany(
                "INSERT INTO release_files(release_id, rel_path, sha256, size, mtime) VALUES(?,?,?,?,?)",
                [(release_id, *r) for r in rows],
            )
            self._conn.commit()

    def finish(self, release_id: int, status: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE releases SET status=?,"
                " files_count=(SELECT COUNT(*) FROM release_files WHERE release_id=?),"
                " size_total=(SELECT COALESCE(SUM(size),0) FROM release_files WHERE release_id=?)"
                " WHERE id=?",
                (status, release_id, release_id, release_id),
            )
            self._conn.commit()

    def update(self, release_id: int, meta: dict, addressees: list[str] | None) -> None:
        allowed = ("project_folder", "project_type", "project_name", "stage", "title", "released_at", "note")
        sets = {k: meta[k] for k in allowed if k in meta}
        with self._lock:
            if sets:
                self._conn.execute(
                    f"UPDATE releases SET {', '.join(k + '=?' for k in sets)} WHERE id=?",
                    (*sets.values(), release_id),
                )
            if addressees is not None:
                self._conn.execute("DELETE FROM release_addressees WHERE release_id=?", (release_id,))
                self._conn.executemany(
                    "INSERT OR IGNORE INTO release_addressees(release_id, org_code) VALUES(?,?)",
                    [(release_id, c) for c in addressees],
                )
            self._conn.commit()

    def set_status(self, release_id: int, status: str) -> None:
        with self._lock:
            self._conn.execute("UPDATE releases SET status=? WHERE id=?", (status, release_id))
            self._conn.commit()

    def mark_sendings_made(self, release_id: int) -> None:
        with self._lock:
            self._conn.execute("UPDATE releases SET sendings_made=1 WHERE id=?", (release_id,))
            self._conn.commit()

    def uses_blob(self, sha: str) -> bool:
        with self._lock:
            return self._conn.execute("SELECT 1 FROM release_files WHERE sha256=? LIMIT 1", (sha,)).fetchone() is not None

    def set_applications(self, release_id: int, apps: list[dict]) -> None:
        with self._lock:
            self._conn.execute("UPDATE releases SET applications=? WHERE id=?",
                               (json.dumps(apps, ensure_ascii=False), release_id))
            self._conn.commit()

    def delete(self, release_id: int) -> None:
        # Blobs stay: other releases may share them; they are copies of what is on E: anyway.
        with self._lock:
            self._conn.execute("DELETE FROM releases WHERE id=?", (release_id,))
            self._conn.commit()

    def _addressees(self) -> dict[int, list[str]]:
        out: dict[int, list[str]] = {}
        for r in self._conn.execute("SELECT release_id, org_code FROM release_addressees ORDER BY rowid"):
            out.setdefault(r["release_id"], []).append(r["org_code"])
        return out

    def releases(self) -> list[dict]:
        with self._lock:
            addr = self._addressees()
            rows = [dict(r) for r in self._conn.execute(
                "SELECT * FROM releases ORDER BY released_at DESC, id DESC")]
        for r in rows:
            r["addressees"] = addr.get(r["id"], [])
            _decode(r)
        return rows

    def release(self, release_id: int) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM releases WHERE id=?", (release_id,)).fetchone()
            if not row:
                return None
            r = dict(row)
            r["addressees"] = self._addressees().get(release_id, [])
            return _decode(r)

    def files(self, release_id: int) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(
                "SELECT id, rel_path, sha256, size, mtime FROM release_files WHERE release_id=? ORDER BY rel_path",
                (release_id,))]

    def file(self, release_id: int, file_id: int) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT id, rel_path, sha256, size, mtime FROM release_files WHERE release_id=? AND id=?",
                (release_id, file_id)).fetchone()
            return dict(row) if row else None


# ------------------------------------------------------------------ registration

class Registrar:
    """Copies a release in a background thread; the page polls its progress."""

    def __init__(self, store: ReleaseStore, blobs: BlobStore, read_applications=None):
        self.store = store
        self.blobs = blobs
        # callable(release_id) that reads «Заявление» PDFs after the copy; optional
        self.read_applications = read_applications
        self.jobs: dict[str, dict] = {}
        self._lock = threading.Lock()

    def start(self, root: Path, meta: dict, addressees: list[str]) -> str:
        files = list_release_files(root)
        total = sum(f.stat().st_size for f in files)
        rid = self.store.create({**meta, "source_path": str(root)}, addressees)
        job_id = uuid.uuid4().hex[:12]
        job = {"id": job_id, "release_id": rid, "state": "copying", "done": 0, "files": len(files),
               "bytes_done": 0, "bytes": total, "new_bytes": 0, "error": None}
        with self._lock:
            self.jobs[job_id] = job
        threading.Thread(target=self._run, args=(job, root, files), daemon=True).start()
        return job_id

    def resume(self, release_id: int) -> str:
        """Copies what is missing of a release whose copy broke off (e.g. the PC went off)."""
        r = self.store.release(release_id)
        root = Path(r["source_path"])
        done = {f["rel_path"] for f in self.store.files(release_id)}
        files = [f for f in list_release_files(root) if f.relative_to(root).as_posix() not in done]
        total = sum(f.stat().st_size for f in files)
        self.store.set_status(release_id, "copying")
        job_id = uuid.uuid4().hex[:12]
        job = {"id": job_id, "release_id": release_id, "state": "copying", "done": 0, "files": len(files),
               "bytes_done": 0, "bytes": total, "new_bytes": 0, "error": None}
        with self._lock:
            self.jobs[job_id] = job
        threading.Thread(target=self._run, args=(job, root, files), daemon=True).start()
        return job_id

    def _run(self, job: dict, root: Path, files: list[Path]) -> None:
        rows: list[tuple[str, str, int, str]] = []
        try:
            for f in files:
                sha, size, new = self.blobs.put(f)
                mtime = datetime.fromtimestamp(f.stat().st_mtime).isoformat(timespec="seconds")
                rows.append((f.relative_to(root).as_posix(), sha, size, mtime))
                job["done"] += 1
                job["bytes_done"] += size
                if new:
                    job["new_bytes"] += size
                if len(rows) >= 200:
                    self.store.add_files(job["release_id"], rows)
                    rows = []
            self.store.add_files(job["release_id"], rows)
            self.store.finish(job["release_id"], "ready")
            if self.read_applications:
                self.read_applications(job["release_id"])
            job["state"] = "ready"
        except Exception as exc:  # noqa: BLE001 — reported to the page
            self.store.add_files(job["release_id"], rows)
            self.store.finish(job["release_id"], "failed")
            job["state"] = "failed"
            job["error"] = str(exc)

    def job(self, job_id: str) -> dict | None:
        return self.jobs.get(job_id)


# ------------------------------------------------------------------ zip streaming

class _Sink:
    """Write-only buffer that zipfile writes into; the bytes are handed out in chunks."""

    def __init__(self):
        self._parts: list[bytes] = []
        self._pos = 0

    def write(self, data) -> int:
        self._parts.append(bytes(data))
        self._pos += len(data)
        return len(data)

    def tell(self) -> int:
        return self._pos

    def flush(self) -> None:
        pass

    def drain(self) -> bytes:
        out = b"".join(self._parts)
        self._parts.clear()
        return out


def _zip_info(name: str, mtime: str | None) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name)
    if mtime:
        try:
            t = datetime.fromisoformat(mtime)
            info.date_time = (max(t.year, 1980), t.month, t.day, t.hour, t.minute, t.second)
        except ValueError:
            pass
    info.compress_type = zipfile.ZIP_STORED
    return info


def _write_entries(zf: zipfile.ZipFile, entries, sink: _Sink, chunk: int):
    """Writes entries into zf, yielding whatever the outermost sink has collected.

    An entry is (name, file on disk, ISO mtime) or (name.zip, [entries...], ISO mtime):
    the second kind becomes a zip inside the zip, built on the fly as well.
    """
    for name, src, mtime in entries:
        info = _zip_info(name, mtime)
        if isinstance(src, list):
            # size unknown in advance: zipfile writes a data descriptor (the sink cannot seek)
            with zf.open(info, "w", force_zip64=True) as dst:
                with zipfile.ZipFile(dst, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as inner:
                    yield from _write_entries(inner, src, sink, chunk)
                data = sink.drain()
                if data:
                    yield data
        else:
            info.file_size = src.stat().st_size
            with open(src, "rb") as fin, zf.open(info, "w", force_zip64=True) as dst:
                while True:
                    buf = fin.read(chunk)
                    if not buf:
                        break
                    dst.write(buf)
                    data = sink.drain()
                    if data:
                        yield data
        data = sink.drain()
        if data:
            yield data


def stream_zip(entries: list, chunk: int = 1 << 20):
    """Yields a ZIP (stored, no compression) built on the fly: no temp file, no size limit.

    entries: (name inside the archive, file on disk, ISO mtime or None), or
    (name.zip, [entries], mtime) for a nested zip.
    """
    sink = _Sink()
    with zipfile.ZipFile(sink, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as zf:
        yield from _write_entries(zf, entries, sink, chunk)
    data = sink.drain()
    if data:
        yield data


def nest_folders(entries: list[tuple[str, Path, str | None]], top: str) -> list:
    """Whole-release archive: files in the release root stay as they are, every
    top-level folder becomes «<folder>.zip» (holding «<folder>/...», like the zips
    that are actually sent). entries are relative to the release root."""
    out: list = []
    groups: dict[str, list] = {}
    for rel, path, mtime in entries:
        head, sep, rest = rel.partition("/")
        if not sep:
            out.append((f"{top}/{rel}", path, mtime))
            continue
        if head not in groups:
            groups[head] = []
            out.append((f"{top}/{head}.zip", groups[head], None))
        groups[head].append((rel, path, mtime))
    # a folder zip carries the newest file time inside it
    return [(n, s, max((x[2] for x in s if x[2]), default=None)) if isinstance(s, list) else (n, s, m)
            for n, s, m in out]


def human_size(n: int) -> str:
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if n < 1024 or unit == "ГБ":
            return f"{n:.0f} {unit}" if unit == "Б" else f"{n:.1f} {unit}".replace(".", ",")
        n /= 1024
    return str(n)


def copy_tree_size(root: Path) -> tuple[int, int, int]:
    """(files taken, bytes taken, files skipped) — for the preview."""
    taken = skipped = size = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            p = Path(dirpath) / name
            if wanted(p):
                taken += 1
                size += p.stat().st_size
            else:
                skipped += 1
    return taken, size, skipped


__all__ = [
    "BlobStore", "Registrar", "ReleaseStore", "Suggestion", "copy_tree_size", "find_orgs", "find_stage",
    "human_size", "list_release_files", "nest_folders", "parse_snapshot_name", "split_project", "stream_zip", "suggest",
]


def compare_files(old: list[dict], new: list[dict]) -> dict:
    """What changed from one release to another, by path and content (sha256).

    added / removed — paths present in one release only (moves are reported apart);
    changed — same path, different content; moved — same content under another path.
    """
    o = {f["rel_path"]: f for f in old}
    n = {f["rel_path"]: f for f in new}
    added = [n[p] for p in n if p not in o]
    removed = [o[p] for p in o if p not in n]
    changed = [{"old": o[p], "new": n[p]} for p in n if p in o and o[p]["sha256"] != n[p]["sha256"]]
    same = sum(1 for p in n if p in o and o[p]["sha256"] == n[p]["sha256"])
    moved = []
    gone_by_sha: dict[str, list[dict]] = {}
    for f in removed:
        gone_by_sha.setdefault(f["sha256"], []).append(f)
    for f in list(added):
        if gone_by_sha.get(f["sha256"]):
            src = gone_by_sha[f["sha256"]].pop(0)
            moved.append({"old": src, "new": f})
            added.remove(f)
            removed.remove(src)
    return {"added": added, "removed": removed, "changed": changed, "moved": moved, "same": same}
