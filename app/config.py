"""Загрузка config.yaml и committees.yaml."""

from __future__ import annotations

import os
import json
import re
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

BASE_DIR = Path(__file__).resolve().parent.parent


@dataclass
class Group:
    """Где внутри проекта лежат документы одной группы.

    path — цепочка папок от корня проекта, по регулярному выражению
    на каждый уровень. Это позволяет описывать и «08_Согласования /
    02_Согласования ИОГВ», и просто «09_Выпуск», не меняя код.
    """

    id: str
    title: str
    path: list[re.Pattern]
    enabled: bool
    files: re.Pattern | None = None
    exclude: list[re.Pattern] = field(default_factory=list)
    matrix: bool = False
    # decision for files whose name says nothing (e.g. «01-20-136.pdf» in 01_КГА → «Распоряжение»)
    decision: str | None = None
    org: str | None = None          # organization when neither the name nor the folders say
    # document number taken from the file name into the note («01-20-2-185_26» → «№ 01-20-2-185/26»)
    number: re.Pattern | None = None


@dataclass
class ProjectType:
    code: str
    title: str
    stages: bool
    manual: bool = False   # letters and releases are entered by hand; folders are not scanned


# Kinds of work kept by hand (decision of 11.11): no stages, no folder scanning.
MANUAL_TYPES = {
    "АГПТ": "Архитектурно-градостроительный проект",
    "Концепция": "Концепция",
    "Проект": "Проект",
}
CUSTOMER = "Заказчик"
DEFAULT_TERM_DAYS = 30


@dataclass
class Decision:
    id: str
    label: str
    kind: str
    patterns: list[re.Pattern]


@dataclass
class Committee:
    code: str
    name: str
    aliases: list[str] = field(default_factory=list)
    active: bool = True
    parent: str | None = None
    district: bool = False
    final: bool = False   # согласование этого органа завершает проект
    custom: bool = False  # added in the admin panel (РЖД, Ленводхоз…), not in the standard set
    term_days: int | None = None   # answer term; 0 = no term; None = default (30, or none for custom)

    @property
    def term(self) -> int | None:
        """Days for the planned answer date, or None when the organization has no term."""
        if self.term_days is not None:
            return self.term_days or None
        return None if self.custom else DEFAULT_TERM_DAYS

    @property
    def all_codes(self) -> list[str]:
        return [self.code, *self.aliases]


@dataclass
class Config:
    sources: list[Path]
    host: str
    port: int
    viewer_password: str
    admin_password: str
    session_hours: int
    max_login_attempts: int
    data_dir: Path
    extensions: set[str]
    groups: list[Group]
    project_types: dict[str, ProjectType]
    decisions: list[Decision]
    committees: list[Committee]
    secret_key: str
    path: Path
    telegram: dict = field(default_factory=dict)

    @property
    def enabled_groups(self) -> list[Group]:
        return [g for g in self.groups if g.enabled]

    def project_type(self, code: str | None) -> ProjectType | None:
        return self.project_types.get(code) if code else None

    @property
    def final_orgs(self) -> list[str]:
        return [c.code for c in self.committees if c.final]

    @property
    def matrix_groups(self) -> set[str]:
        """Группы, документы которых попадают в сводную матрицу."""
        return {g.id for g in self.enabled_groups if g.matrix}


class ConfigError(Exception):
    """Понятная человеку ошибка чтения настроек."""


def _read_yaml(path: Path) -> dict[str, Any]:
    """Читает YAML, переживая сохранение файла в другой кодировке.

    Блокнот и старые редакторы легко сохраняют файл в ANSI (cp1251) —
    без этого пользователь получил бы невнятную ошибку разбора.
    """
    data = path.read_bytes()
    text = None
    for encoding in ("utf-8-sig", "utf-8", "cp1251"):
        try:
            text = data.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise ConfigError(f"Не удалось прочитать {path.name}: неизвестная кодировка файла.")

    try:
        return yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        where = ""
        mark = getattr(exc, "problem_mark", None)
        if mark is not None:
            where = f" (строка {mark.line + 1}, столбец {mark.column + 1})"
        raise ConfigError(
            f"Ошибка в файле {path.name}{where}.\n"
            "Проверьте отступы и кавычки. Файл нужно сохранять в кодировке UTF-8."
        ) from exc


def _rx(pattern: str) -> re.Pattern:
    return re.compile(pattern, re.IGNORECASE | re.UNICODE)


def _group(raw: dict[str, Any]) -> Group:
    path = raw.get("path")
    if isinstance(path, str):
        path = [path]
    if not path:
        raise ConfigError(
            f"У группы «{raw.get('id', '?')}» не задан path — "
            "список папок от корня проекта"
        )
    return Group(
        id=str(raw["id"]),
        title=str(raw.get("title", raw["id"])),
        path=[_rx(str(x)) for x in path],
        enabled=bool(raw.get("enabled", False)),
        files=_rx(str(raw["files"])) if raw.get("files") else None,
        exclude=[_rx(str(x)) for x in (raw.get("exclude") or [])],
        matrix=bool(raw.get("matrix", False)),
        decision=str(raw["decision"]) if raw.get("decision") else None,
        org=str(raw["org"]) if raw.get("org") else None,
        number=_rx(str(raw["number"])) if raw.get("number") else None,
    )


def _resolve(base: Path, value: str) -> Path:
    p = Path(str(value).strip())
    return p if p.is_absolute() else (base / p).resolve()


def _secret_key(data_dir: Path) -> str:
    """Ключ подписи cookie. Создаётся один раз, чтобы сессии переживали рестарт."""
    key_file = data_dir / "secret.key"
    if key_file.exists():
        value = key_file.read_text(encoding="utf-8").strip()
        if value:
            return value
    data_dir.mkdir(parents=True, exist_ok=True)
    value = secrets.token_urlsafe(48)
    key_file.write_text(value, encoding="utf-8")
    if os.name == "nt":  # hide from a casual glance in Explorer
        try:
            os.system(f'attrib +h "{key_file}"')
        except Exception:
            pass
    return value


def _with_manual_types(types: dict) -> dict:
    """АГПТ, Концепция and «Проект» are always known and always kept by hand."""
    for code, title in MANUAL_TYPES.items():
        t = types.get(code)
        types[code] = ProjectType(code=code, title=t.title if t else title, stages=False, manual=True)
    return types


def load_config(path: str | Path | None = None) -> Config:
    # On the server the config lives outside the image: CONFIG_FILE=/config/config.yaml
    cfg_path = Path(path or os.environ.get("CONFIG_FILE") or BASE_DIR / "config.yaml")
    cfg_path = cfg_path.resolve()
    if not cfg_path.is_file():
        raise ConfigError(f"Не найден файл настроек: {cfg_path}")
    raw: dict[str, Any] = _read_yaml(cfg_path)

    base = cfg_path.parent
    data_dir = _resolve(base, os.environ.get("DATA_DIR") or raw.get("data_dir", "./data"))
    data_dir.mkdir(parents=True, exist_ok=True)

    server = raw.get("server") or {}
    auth = raw.get("auth") or {}
    scan = raw.get("scan") or {}

    committees_file = Path(os.environ.get("COMMITTEES_FILE") or base / "committees.yaml")
    if not committees_file.is_file():
        raise ConfigError(f"Не найден справочник комитетов: {committees_file}")
    committees_raw = _read_yaml(committees_file)
    committees = [
        Committee(
            code=str(c["code"]).strip(),
            name=str(c.get("name", c["code"])).strip(),
            aliases=[str(a).strip() for a in (c.get("aliases") or [])],
            active=bool(c.get("active", True)),
            parent=(str(c["parent"]).strip() if c.get("parent") else None),
            district=bool(c.get("district", False)),
            final=bool(c.get("final", False)),
            custom=bool(c.get("custom", False)),
            term_days=(int(c["term_days"]) if c.get("term_days") not in (None, "") else None),
        )
        for c in (committees_raw.get("committees") or [])
    ]

    return Config(
        sources=[_resolve(base, str(x)) for x in (raw.get("sources") or []) if str(x).strip()],
        host=str(os.environ.get("HOST") or server.get("host", "127.0.0.1")),
        port=int(os.environ.get("PORT") or server.get("port", 8080)),
        viewer_password=str(auth.get("viewer_password", "")),
        admin_password=str(auth.get("admin_password", "")),
        session_hours=int(auth.get("session_hours", 72)),
        max_login_attempts=int(auth.get("max_login_attempts", 10)),
        data_dir=data_dir,
        extensions={str(e).lower() for e in (scan.get("extensions") or [".pdf"])},
        groups=[_group(g) for g in (scan.get("groups") or [])],
        project_types=_with_manual_types({
            str(p["code"]): ProjectType(
                code=str(p["code"]),
                title=str(p.get("title", p["code"])),
                stages=bool(p.get("stages", False)),
            )
            for p in (raw.get("project_types") or [])
        }),
        decisions=[
            Decision(
                id=str(d["id"]),
                label=str(d.get("label", d["id"])),
                kind=str(d.get("kind", "neutral")),
                patterns=[_rx(p) for p in (d.get("patterns") or [])],
            )
            for d in (raw.get("decisions") or [])
        ],
        committees=committees,
        secret_key=_secret_key(data_dir),
        telegram=dict(raw.get("telegram") or {}),
        path=cfg_path,
    )


# ---------------------------------------------------------------------------
# Правка справочника комитетов из панели управления.
#
# Файл редактируется построчно, а не перезаписывается через yaml.dump:
# так сохраняются комментарии, порядок и оформление, которые человек может
# править руками.
# ---------------------------------------------------------------------------

_ENTRY_RX = re.compile(r"^(\s*)-\s+code:\s*(.+?)\s*$")
_FIELD_RX = re.compile(r"^(\s*)(\w+):\s*(.*?)\s*$")


def set_committee_field(committees_file: Path, code: str, name: str, value: str | None) -> None:
    """Sets (or removes, value=None) one plain field of a committee in committees.yaml."""
    lines = _read_text(committees_file).splitlines()
    start = next((i for i, line in enumerate(lines)
                  if (m := _ENTRY_RX.match(line)) and m.group(2).strip().strip('"\'') == code), None)
    if start is None:
        raise ConfigError(f"В справочнике нет «{code}»")
    indent = _ENTRY_RX.match(lines[start]).group(1)
    end = next((i for i in range(start + 1, len(lines))
                if (m := _ENTRY_RX.match(lines[i])) and len(m.group(1)) <= len(indent)), len(lines))
    while end > start + 1 and not lines[end - 1].strip():
        end -= 1
    pos = next((i for i in range(start + 1, end) if (fm := _FIELD_RX.match(lines[i])) and fm.group(2) == name), None)
    if value is None:
        if pos is not None:
            del lines[pos]
    elif pos is not None:
        lines[pos] = f"{indent}  {name}: {value}"
    else:
        lines.insert(end, f"{indent}  {name}: {value}")
    committees_file.write_text("\n".join(lines) + "\n", encoding="utf-8")


def set_committee_flags(
    committees_file: Path, code: str, *, active: bool | None = None, final: bool | None = None
) -> None:
    """Меняет active / final у одного комитета прямо в committees.yaml."""
    lines = _read_text(committees_file).splitlines()

    start = None
    indent = "  "
    for i, line in enumerate(lines):
        m = _ENTRY_RX.match(line)
        if m and m.group(2).strip().strip('"\'') == code:
            start = i
            indent = m.group(1)
            break
    if start is None:
        raise ConfigError(f"В справочнике нет комитета «{code}»")

    end = len(lines)
    for i in range(start + 1, len(lines)):
        m = _ENTRY_RX.match(lines[i])
        if m and len(m.group(1)) <= len(indent):
            end = i
            break

    field_indent = indent + "  "
    for name, value in (("active", active), ("final", final)):
        if value is None:
            continue
        text = f"{field_indent}{name}: {'true' if value else 'false'}"
        for i in range(start + 1, end):
            fm = _FIELD_RX.match(lines[i])
            if fm and fm.group(2) == name:
                lines[i] = text
                break
        else:
            lines.insert(end, text)
            end += 1

    committees_file.write_text("\n".join(lines) + "\n", encoding="utf-8")


_CODE_RX = re.compile(r"^[0-9A-Za-zА-Яа-яЁё][0-9A-Za-zА-Яа-яЁё .\-]{0,29}$")


def add_committee(committees_file: Path, code: str, name: str, aliases: list[str], known: set[str]) -> None:
    """Appends an organization (e.g. РЖД, Ленводхоз) to committees.yaml.

    It is «custom» and not in the standard set: a project gets it only when ticked there.
    """
    code = code.strip()
    if not _CODE_RX.match(code):
        raise ConfigError("Сокращение: буквы и цифры, до 30 знаков")
    for x in [code, *aliases]:
        if x.strip().lower() in known:
            raise ConfigError(f"«{x.strip()}» уже есть в справочнике")
    q = lambda v: json.dumps(v, ensure_ascii=False)  # JSON strings are valid YAML
    block = [f"  - code: {q(code)}", f"    name: {q(name.strip() or code)}"]
    clean = [a.strip() for a in aliases if a.strip()]
    if clean:
        block.append("    aliases: [" + ", ".join(q(a) for a in clean) + "]")
    block += ["    active: false", "    custom: true"]
    text = _read_text(committees_file).rstrip("\n")
    committees_file.write_text(text + "\n\n" + "\n".join(block) + "\n", encoding="utf-8")


def _read_text(path: Path) -> str:
    """Чтение с той же терпимостью к кодировке, что и у остальных настроек."""
    data = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "cp1251"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ConfigError(f"Не удалось прочитать {path.name}: неизвестная кодировка")
