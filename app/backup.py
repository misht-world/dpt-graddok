"""Backup: databases and file copies leave the Pi.

- Every run: a consistent snapshot of index.db and releases.db (SQLite online backup) into
  data/backups/<date>/ on the Pi (14 kept) — protects against a bad update, not a dead disk.
- When the PC share is reachable (BACKUP_DIR, a writable share on the PC's disk E:):
  the same snapshot into <share>/db/<date>/ (60 kept) and the copy store mirrored into
  <share>/blobs/ — new files copied, files deleted on the Pi deleted there too.
- Runs once a day after BACKUP_HOUR (local time); if the PC is off it tries again every
  30 minutes until it succeeds that day. The admin panel can start it at once.
"""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path

log = logging.getLogger("backup")

BACKUP_HOUR = 2            # 02:xx local; 03:30–04:15 is the Pi's update/reboot window
RETRY_SECONDS = 1800
LOCAL_KEEP = 14
REMOTE_KEEP = 60
DATABASES = ("index.db", "releases.db")

README = """Резервная копия ГРАДДОКа (пишет сервер Pi, раз в сутки).

db/ГГГГ-ММ-ДД/   — базы index.db (каталог), releases.db (выпуски, отправки) и справочник committees.yaml.
blobs/          — копии файлов (согласования, выпуски, загруженные вручную), имя = sha256.

Восстановление: остановить ГРАДДОК, положить обе базы из нужного дня в /opt/pi5/graddok/data/,
папку blobs — в /opt/pi5/graddok/data/blobs/, запустить. Подробно: docs/BACKUP.md в dpt-graddok.
"""


def snapshot_db(src: Path, dst: Path) -> None:
    """SQLite online backup (safe while the app writes), verified before it replaces anything."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".tmp")
    tmp.unlink(missing_ok=True)
    s, d = sqlite3.connect(src), sqlite3.connect(tmp)
    try:
        s.backup(d)
        d.execute("PRAGMA journal_mode=DELETE")
        ok = d.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        s.close()
        d.close()   # «with» would not close it, and the file must be free for os.replace
    if not ok:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"копия {src.name} не прошла проверку")
    os.replace(tmp, dst)


def _prune_days(root: Path, keep: int) -> None:
    days = sorted(p for p in root.iterdir() if p.is_dir() and len(p.name) == 10) if root.is_dir() else []
    for old in days[:-keep]:
        shutil.rmtree(old, ignore_errors=True)


def mirror_blobs(src: Path, dst: Path) -> tuple[int, int, int]:
    """Copies new blobs (content-addressed: same name = same content), deletes ones gone on the Pi.
    Returns (copied, bytes copied, deleted)."""
    copied = size = deleted = 0
    have: set[str] = set()
    for p in src.glob("??/*"):
        if p.name.startswith(".") or not p.is_file():
            continue
        have.add(p.name)
        target = dst / p.parent.name / p.name
        if target.is_file() and target.stat().st_size == p.stat().st_size:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(".tmp-" + p.name)
        shutil.copyfile(p, tmp)
        os.replace(tmp, target)
        copied += 1
        size += p.stat().st_size
    if dst.is_dir():
        for p in dst.glob("??/*"):
            if p.is_file() and p.name not in have:
                p.unlink()
                deleted += 1
    return copied, size, deleted


class Backup:
    def __init__(self, data_dir: Path, target: Path | None, meta_get, meta_set, extra_files=()):
        self.data_dir = data_dir
        # small files outside data/ that hold the owner's work (committees.yaml: own organizations)
        self.extra_files = [Path(f) for f in extra_files]
        self.target = target
        self._get, self._set = meta_get, meta_set
        self._lock = threading.Lock()
        self.running = False
        self.allow_same_device = False   # tests only

    def status(self) -> dict:
        return {
            "target": str(self.target) if self.target else None,
            "last_ok": self._get("backup_last_ok"),
            "last_try": self._get("backup_last_try"),
            "last_error": self._get("backup_last_error"),
            "last_summary": self._get("backup_last_summary"),
            "running": self.running,
        }

    def target_ready(self) -> bool:
        if not self.target:
            return False
        try:
            if not (self.target.is_dir() and os.access(self.target, os.W_OK)):
                return False
            # If the PC share is not mounted, the folder is just an empty directory on the
            # Pi itself — a «backup» there would protect nothing. Require another device.
            return os.stat(self.target).st_dev != os.stat(self.data_dir).st_dev or self.allow_same_device
        except OSError:
            return False

    def run(self) -> dict:
        """One backup. Local snapshot always; the PC copy when the share is there."""
        if not self._lock.acquire(blocking=False):
            return {"ok": False, "error": "копирование уже идёт"}
        self.running = True
        now = datetime.now()
        day = now.date().isoformat()
        self._set("backup_last_try", now.isoformat(timespec="seconds"))
        try:
            local = self.data_dir / "backups" / day
            for name in DATABASES:
                if (self.data_dir / name).is_file():
                    snapshot_db(self.data_dir / name, local / name)
            for f in self.extra_files:
                if f.is_file():
                    shutil.copyfile(f, local / f.name)
            _prune_days(self.data_dir / "backups", LOCAL_KEEP)
            if not self.target_ready():
                raise RuntimeError("папка резервной копии на ПК недоступна (ПК выключен или папка не подключена)")
            remote = self.target / "db" / day
            remote.mkdir(parents=True, exist_ok=True)
            for name in [*DATABASES, *(f.name for f in self.extra_files)]:
                if (local / name).is_file():
                    shutil.copyfile(local / name, remote / (name + ".tmp"))
                    os.replace(remote / (name + ".tmp"), remote / name)
            _prune_days(self.target / "db", REMOTE_KEEP)
            copied, size, deleted = mirror_blobs(self.data_dir / "blobs", self.target / "blobs")
            (self.target / "README.txt").write_text(README, encoding="utf-8")
            summary = f"базы за {day}; файлов скопировано {copied} ({size / 2**20:.0f} МБ), удалено {deleted}"
            self._set("backup_last_ok", now.isoformat(timespec="seconds"))
            self._set("backup_last_error", "")
            self._set("backup_last_summary", summary)
            log.info("резервная копия: %s", summary)
            return {"ok": True, "summary": summary}
        except Exception as exc:  # noqa: BLE001 — shown in the admin panel, retried later
            self._set("backup_last_error", str(exc))
            log.warning("резервная копия не сделана: %s", exc)
            return {"ok": False, "error": str(exc)}
        finally:
            self.running = False
            self._lock.release()

    def due(self, now: datetime | None = None) -> bool:
        now = now or datetime.now()
        last = (self._get("backup_last_ok") or "")[:10]
        return now.hour >= BACKUP_HOUR and last != now.date().isoformat()

    def loop(self) -> None:
        while True:
            try:
                if self.due():
                    self.run()
            except Exception as exc:  # noqa: BLE001 — never stop the scheduler
                log.warning("планировщик копий: %s", exc)
            time.sleep(RETRY_SECONDS)

    def start(self) -> None:
        threading.Thread(target=self.loop, name="backup", daemon=True).start()


def stale(status: dict, days: int = 3) -> bool:
    """No successful copy for several days — the admin panel warns."""
    last = (status.get("last_ok") or "")[:10]
    return not last or date.fromisoformat(last) < date.today() - timedelta(days=days)
