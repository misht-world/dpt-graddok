"""Обход файловой системы и построение списка документов."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .config import Config, Group
from .parsing import (
    build_org_matchers,
    match_decision,
    match_org,
    match_stage,
    normalize,
    parse_name,
)

_PROJECT_RX = re.compile(r"^\s*([A-Za-zА-Яа-яЁё]{2,10})\s*-\s*(.+?)\s*$")
_SKIP_DIRS = {"__pycache__", ".git", ".svn", "RECYCLE.BIN", "System Volume Information"}
_SKIP_FILE_PREFIXES = ("~$", ".~")


@dataclass
class Source:
    """Папка, из которой собираются проекты."""

    id: int
    path: Path
    label: str = ""

    @classmethod
    def from_row(cls, row: dict) -> "Source":
        return cls(id=int(row["id"]), path=Path(row["path"]), label=row.get("label") or "")


@dataclass
class Project:
    key: str  # уникальный идентификатор: "<источник>:<имя папки>"
    dir: Path
    source: Source
    type_code: str | None
    type_title: str | None
    name: str
    has_stages: bool
    groups: dict[str, Path] = field(default_factory=dict)
    # Папки верхнего уровня, за которыми следит наблюдатель.
    watch: list[Path] = field(default_factory=list)


def _is_hidden(p: Path) -> bool:
    return p.name.startswith(".") or p.name in _SKIP_DIRS


def _child_dir(parent: Path, pattern) -> Path | None:
    """Первая подпапка, чьё имя подходит под выражение."""
    try:
        for sub in sorted(parent.iterdir(), key=lambda p: p.name.lower()):
            if sub.is_dir() and not _is_hidden(sub) and pattern.search(normalize(sub.name)):
                return sub
    except OSError:
        return None
    return None


def resolve_group_dir(project_dir: Path, group: Group) -> Path | None:
    """Идёт по цепочке папок группы от корня проекта."""
    current = project_dir
    for pattern in group.path:
        current = _child_dir(current, pattern)
        if current is None:
            return None
    return current


def project_groups(config: Config, project_dir: Path) -> dict[str, Path]:
    found: dict[str, Path] = {}
    for group in config.enabled_groups:
        target = resolve_group_dir(project_dir, group)
        if target is not None:
            found[group.id] = target
    return found


def _make_project(config: Config, source: Source, entry: Path) -> Project:
    name = normalize(entry.name)
    type_code = type_title = None
    display = name
    m = _PROJECT_RX.match(name)
    if m:
        code = m.group(1).upper()
        ptype = config.project_types.get(code)
        if ptype:
            type_code, type_title, display = ptype.code, ptype.title, m.group(2)

    ptype = config.project_type(type_code)
    project = Project(
        key=f"{source.id}:{entry.name}",
        dir=entry,
        source=source,
        type_code=type_code,
        type_title=type_title,
        name=display,
        has_stages=bool(ptype.stages) if ptype else False,
    )

    project.groups = project_groups(config, entry)
    # Следим за верхними папками групп (08_Согласования, 09_Выпуск и т.п.),
    # а не за всей папкой проекта — иначе обходили бы весь архив.
    tops: dict[str, Path] = {}
    for group in config.enabled_groups:
        top = _child_dir(entry, group.path[0])
        if top is not None:
            tops[str(top).lower()] = top
    project.watch = list(tops.values())
    return project


def iter_projects(config: Config, sources: list[Source]) -> list[Project]:
    """Собирает проекты из всех указанных папок.

    Папка-источник может быть либо самим проектом (если внутри есть блок
    согласований), либо контейнером, подпапки которого — проекты.
    """
    projects: list[Project] = []
    for source in sources:
        if not source.path.is_dir():
            continue

        # Папка-источник может быть самим проектом: тогда группы найдутся
        # прямо в ней, и подпапки как проекты уже не разбираем.
        if project_groups(config, source.path):
            projects.append(_make_project(config, source, source.path))
            continue

        try:
            entries = sorted(source.path.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            continue
        for entry in entries:
            if not entry.is_dir() or _is_hidden(entry):
                continue
            projects.append(_make_project(config, source, entry))
    return projects


def watch_dirs(config: Config, sources: list[Source]) -> list[Path]:
    """Что отслеживать наблюдателю: сами источники + блоки согласований."""
    dirs: list[Path] = []
    for source in sources:
        if source.path.is_dir():
            dirs.append(source.path)
    for project in iter_projects(config, sources):
        for path in project.watch:
            if path.is_dir():
                dirs.append(path)
    # Сохраняем порядок, убирая дубли (источник может совпадать с проектом).
    seen: set[str] = set()
    unique: list[Path] = []
    for path in dirs:
        key = str(path).lower()
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def sha256(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone().isoformat(timespec="seconds")


def _stage_from_path(parts: list[str]) -> str | None:
    for part in parts:
        stage, _ = match_stage(part)
        if stage:
            return stage
    return None


def _skip_file(path: Path) -> bool:
    for prefix in _SKIP_FILE_PREFIXES:
        if path.name.startswith(prefix):
            return True
    return path.name.startswith(".")


def iter_group_files(config: Config, group: Group, group_dir: Path):
    """Файлы группы с учётом расширений, исключённых папок и фильтра имён.

    Один и тот же отбор нужен и сканеру, и отпечатку: иначе файл, который
    в каталог не попадает, всё равно считался бы изменением — и кнопка
    «Актуализировать» горела бы вечно.
    """
    for path in sorted(group_dir.rglob("*"), key=lambda p: str(p).lower()):
        if not path.is_file() or path.suffix.lower() not in config.extensions:
            continue
        if _skip_file(path):
            continue
        folder_parts = [normalize(x) for x in path.relative_to(group_dir).parts[:-1]]
        if any(x.startswith(".") for x in folder_parts):
            continue
        if any(rx.search(part) for part in folder_parts for rx in group.exclude):
            continue
        if group.files and not group.files.search(normalize(path.stem)):
            continue
        yield path, folder_parts


def scan_project_group(
    config: Config,
    project: Project,
    group: Group,
    group_dir: Path,
    matchers,
) -> list[dict]:
    docs: list[dict] = []

    for path, folder_parts in iter_group_files(config, group, group_dir):
        parsed = parse_name(path.stem, config, matchers)

        # Этап: приоритет у структуры папок, имя файла — запасной вариант.
        stage = _stage_from_path(folder_parts) or parsed.stage

        # Организация: если в имени файла её нет — ищем по папкам, снизу вверх.
        org = parsed.org
        if org is None:
            for part in reversed(folder_parts):
                org = match_org(part, matchers)
                if org:
                    break
        if org is None and group.org:
            org = match_org(group.org, matchers)

        # Решение: аналогичный запасной поиск по именам папок.
        decision_id = parsed.decision_id
        decision_label = parsed.decision_label
        decision_kind = parsed.decision_kind
        if decision_id is None:
            for part in reversed(folder_parts):
                dec, _ = match_decision(part, config.decisions)
                if dec:
                    decision_id, decision_label, decision_kind = dec.id, dec.label, dec.kind
                    break
        if decision_id is None and group.decision:
            dec = next((d for d in config.decisions if d.id == group.decision), None)
            if dec:
                decision_id, decision_label, decision_kind = dec.id, dec.label, dec.kind

        note = parsed.note
        if group.number:
            found = group.number.search(path.stem)
            if found:
                note = "№ " + found.group(0).replace("_", "/")

        try:
            stat = path.stat()
        except OSError:
            continue

        try:
            rel_path = path.relative_to(project.source.path).as_posix()
        except ValueError:
            rel_path = path.name

        docs.append(
            {
                "abs_path": str(path.resolve()),
                "rel_path": rel_path,
                "source_id": project.source.id,
                "project_key": project.key,
                "project_type": project.type_code,
                "project_name": project.name,
                "group_id": group.id,
                "group_title": group.title,
                "stage": stage,
                "decision_id": decision_id or "unknown",
                "decision_label": decision_label or "Не определено",
                "decision_kind": decision_kind or "unknown",
                "org_code": org.code if org else None,
                "org_name": org.name if org else None,
                "org_detail": org.detail if org else None,
                "org_parent": org.parent if org else None,
                "note": note,
                "file_name": path.name,
                "subfolder": "/".join(folder_parts),
                "size": stat.st_size,
                "mtime": _iso(stat.st_mtime),
                "doc_date": parsed.doc_date.isoformat() if parsed.doc_date else None,
            }
        )
    return docs


def scan(
    config: Config, sources: list[Source], with_hash: bool = True
) -> tuple[list[dict], list[Project]]:
    """Полный обход. Возвращает документы и список проектов."""
    matchers = build_org_matchers(config)
    projects = iter_projects(config, sources)
    docs: list[dict] = []

    groups_by_id = {g.id: g for g in config.enabled_groups}
    for project in projects:
        for group_id, group_dir in project.groups.items():
            group = groups_by_id.get(group_id)
            if group:
                docs.extend(scan_project_group(config, project, group, group_dir, matchers))

    if with_hash:
        for doc in docs:
            try:
                doc["sha256"] = sha256(Path(doc["abs_path"]))
            except OSError:
                doc["sha256"] = ""
    return docs, projects


def fingerprint(config: Config, sources: list[Source]) -> str:
    """Дешёвая контрольная сумма состояния папок — без чтения содержимого файлов.

    Наблюдатель сравнивает её с опубликованной, чтобы отличить реальное
    изменение от шума файловой системы.
    """
    h = hashlib.sha256()
    groups_by_id = {g.id: g for g in config.enabled_groups}
    for project in iter_projects(config, sources):
        for group_id, group_dir in sorted(project.groups.items()):
            group = groups_by_id.get(group_id)
            if group is None:
                continue
            h.update(("P:" + project.key + "|" + group_id).encode("utf-8"))
            for path, _parts in iter_group_files(config, group, group_dir):
                try:
                    stat = path.stat()
                except OSError:
                    continue
                h.update(
                    (str(path) + "|" + str(stat.st_size) + "|" + str(int(stat.st_mtime))).encode("utf-8")
                )
    return h.hexdigest()
