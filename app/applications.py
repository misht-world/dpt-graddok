"""«Заявление №…» PDFs from the St. Petersburg services portal, found in a release.

Only what the release card needs is taken: number, service, filing date, planned
answer date, stage, the planning order (распоряжение) and the committees marked «Да».
Personal data on the form (passport, birth date, address) is never read out or stored.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .config import Config

APPLICATION_RX = re.compile(r"^заявлени[ея].*\.pdf$", re.IGNORECASE)

_NUM_RX = re.compile(r"Заявление\s*№\s*(\d+)")
_FILED_RX = re.compile(r"Дата подачи заявления\s*(\d{2})\.(\d{2})\.(\d{4})")
_DUE_RX = re.compile(r"Дата планового ответа\s*(\d{2})\.(\d{2})\.(\d{4})")
_STAGE_RX = re.compile(r"\b([IVX]+)\s+этап\b(?:\s*\(([^)]*)\))?")
_ORDER_RX = re.compile(
    r"(?:Номер документа основания|Реквизиты распоряжения[^\n]*\n\s*Номер)\s*([0-9][0-9 \-]*[0-9])\s*\n"
    r"\s*Дата(?: документа основания)?\s*(\d{2})\.(\d{2})\.(\d{4})")
_ORG_START = ("Комитет", "Администрация", "Служба", "Управление", "Инспекция", "Жилищный", "Архивный",
              "Государственн", "Санкт-Петербургское", "СПб ГКУ", "СПб ГБУ")
_ROMAN = {"I": 1, "II": 2, "III": 3, "IV": 4, "V": 5}


@dataclass
class Application:
    file: str                                  # path inside the release
    number: str | None = None
    service: str | None = None
    filed: str | None = None                   # ISO date
    due: str | None = None                     # planned answer date, ISO
    stage: str | None = None                   # "2"
    stage_text: str | None = None              # «II этап (документация)»
    order: str | None = None                   # «1-20-185 от 27.07.2026»
    committees: list[str] = field(default_factory=list)   # codes marked «Да»
    unknown: list[str] = field(default_factory=list)      # «Да» names not in committees.yaml
    error: str | None = None


def _iso(m: re.Match | None) -> str | None:
    return f"{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else None


def _key(name: str) -> str:
    s = name.lower().replace("ё", "е")
    s = re.sub(r"санкт-петербурга?|спб", " ", s)
    return re.sub(r"[^а-яa-z]+", "", s)


def _yes_orgs(text: str) -> list[str]:
    """Names in the «органы … с которыми необходимо согласовать» list that say «Да»."""
    start = text.find("Исполнительные органы")
    if start < 0:
        return []
    found: list[str] = []
    buf: list[str] | None = None
    for raw in text[start:].splitlines():
        line = raw.strip()
        if line.startswith(_ORG_START):
            buf = []
        if buf is None:
            continue
        m = re.match(r"^(.*?)\s*\b(Да|Нет|Не)$", line)
        if m:
            buf.append(m.group(1))
            name = " ".join(x for x in buf if x).strip()
            if m.group(2) == "Да" and name:
                found.append(re.sub(r"\s+", " ", name))
            buf = None
        else:
            buf.append(line)
    return found


def parse_text(text: str, config: Config, rel_path: str = "") -> Application:
    app = Application(file=rel_path)
    if m := _NUM_RX.search(text):
        app.number = m.group(1)
    if (i := text.find("Услуга ")) >= 0:
        lines = []
        for line in text[i + len("Услуга "):].splitlines():
            if line.startswith(("Место подачи", "Дата подачи")) or len(lines) >= 4:
                break
            lines.append(line.strip())
        app.service = " ".join(lines).strip() or None
    app.filed = _iso(_FILED_RX.search(text))
    app.due = _iso(_DUE_RX.search(text))
    if m := _STAGE_RX.search(text):
        app.stage_text = m.group(0).strip()
        if m.group(1) in _ROMAN:
            app.stage = str(_ROMAN[m.group(1)])
    if m := _ORDER_RX.search(text):
        app.order = f"{m.group(1).replace(' ', '-')} от {m.group(2)}.{m.group(3)}.{m.group(4)}"

    by_key = {_key(c.name): c.code for c in config.committees if c.name}
    for name in _yes_orgs(text):
        k = _key(name)
        code = by_key.get(k) or next((c for kk, c in by_key.items() if kk.startswith(k) or k.startswith(kk)), None)
        if code and code not in app.committees:
            app.committees.append(code)
        elif not code:
            app.unknown.append(name)
    # the check of the documentation itself goes to the final body (КГА)
    if not app.committees and app.service and "проверки документации" in app.service.lower():
        app.committees = [c.code for c in config.committees if c.final][:1]
    return app


def read_pdf_text(path: Path) -> str:
    from pypdf import PdfReader  # imported lazily: only needed when a release has an application
    return "\n".join((p.extract_text() or "") for p in PdfReader(str(path)).pages)


def parse_application(path: Path, config: Config, rel_path: str) -> dict:
    try:
        app = parse_text(read_pdf_text(path), config, rel_path)
    except Exception as exc:  # noqa: BLE001 — a broken PDF must not break registration
        app = Application(file=rel_path, error=f"не удалось прочитать: {exc}")
    return asdict(app)


def is_application(rel_path: str) -> bool:
    """Only applications lying in the release root count (not in subfolders)."""
    return "/" not in rel_path and bool(APPLICATION_RX.match(rel_path))
