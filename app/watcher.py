"""Наблюдатель за папками согласований.

Ничего не публикует сам: при изменениях выставляет флаг, по которому
в админ-панели подсвечивается кнопка «Актуализировать».
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from pathlib import Path

from .config import Config
from .scanner import Source, fingerprint, watch_dirs
from .store import Store

log = logging.getLogger("watcher")

DEBOUNCE_SECONDS = 3.0

try:
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer

    WATCHDOG_AVAILABLE = True
except ImportError:  # наблюдатель необязателен — остаётся ручная проверка
    WATCHDOG_AVAILABLE = False
    FileSystemEventHandler = object  # type: ignore[assignment]
    Observer = None  # type: ignore[assignment]


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class _Handler(FileSystemEventHandler):  # type: ignore[misc]
    def __init__(self, on_event):
        self._on_event = on_event

    def on_any_event(self, event):  # noqa: D102
        if getattr(event, "is_directory", False) and event.event_type == "modified":
            return  # шум от обновления времени папки
        self._on_event(getattr(event, "src_path", ""))


class ChangeWatcher:
    """Следит за ФС и сравнивает её отпечаток с опубликованным."""

    def __init__(self, config: Config, store: Store):
        self.config = config
        self.store = store
        self._lock = threading.RLock()
        self._timer: threading.Timer | None = None
        self._observer = None
        self._watched: list[Path] = []
        self._pending = False
        self._detected_at: str | None = None
        self._last_event: str | None = None
        self._current_fp: str | None = None
        self._checking = False

    def _sources(self) -> list[Source]:
        return [Source.from_row(r) for r in self.store.sources(enabled_only=True)]

    # ------------------------------------------------------------- lifecycle

    def start(self) -> None:
        self.recheck(force=True)
        if not WATCHDOG_AVAILABLE:
            log.warning("watchdog не установлен — автоопределение изменений выключено")
            return
        self._observer = Observer()
        self._schedule()
        self._observer.start()

    def stop(self) -> None:
        with self._lock:
            if self._timer:
                self._timer.cancel()
                self._timer = None
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=5)
            self._observer = None

    def refresh_watches(self) -> None:
        """Перечитать список отслеживаемых папок (после публикации)."""
        if self._observer is None:
            return
        self._observer.unschedule_all()
        self._schedule()

    def _schedule(self) -> None:
        handler = _Handler(self._on_event)
        self._watched = []
        source_paths = {str(s.path).lower() for s in self._sources()}
        for path in watch_dirs(self.config, self._sources()):
            try:
                # Папка-источник — без рекурсии (иначе обходим весь архив
                # проектов), блоки согласований — с рекурсией.
                recursive = str(path).lower() not in source_paths
                self._observer.schedule(handler, str(path), recursive=recursive)
                self._watched.append(path)
            except OSError as exc:
                log.warning("не удалось отслеживать %s: %s", path, exc)

    # ---------------------------------------------------------------- events

    def _on_event(self, src_path: str) -> None:
        with self._lock:
            self._last_event = src_path
            if self._timer:
                self._timer.cancel()
            self._timer = threading.Timer(DEBOUNCE_SECONDS, self.recheck)
            self._timer.daemon = True
            self._timer.start()

    def recheck(self, force: bool = False) -> bool:
        """Пересчитать отпечаток и сравнить с опубликованным."""
        with self._lock:
            if self._checking and not force:
                return self._pending
            self._checking = True
        try:
            current = fingerprint(self.config, self._sources())
            published = self.store.get_meta("published_fingerprint")
            with self._lock:
                self._current_fp = current
                changed = current != published
                if changed and not self._pending:
                    self._detected_at = _now()
                if not changed:
                    self._detected_at = None
                self._pending = changed
                return changed
        except Exception:
            log.exception("ошибка проверки изменений")
            return self._pending
        finally:
            with self._lock:
                self._checking = False

    def mark_published(self, fp: str) -> None:
        with self._lock:
            self._current_fp = fp
            self._pending = False
            self._detected_at = None
        self.refresh_watches()

    @property
    def state(self) -> dict:
        with self._lock:
            return {
                "pending": self._pending,
                "detected_at": self._detected_at,
                "last_event": Path(self._last_event).name if self._last_event else None,
                "watching": [str(p) for p in self._watched],
                "auto_detect": WATCHDOG_AVAILABLE and self._observer is not None,
            }
