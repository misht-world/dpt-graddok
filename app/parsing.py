"""Разбор имён файлов и папок: этап, решение, согласующий орган, дата."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime

from .config import Committee, Config, Decision

# Буквы и цифры, которые не должны примыкать к сокращению комитета.
# Благодаря этому "КС" не срабатывает внутри "КСП", а "КТ" — внутри "КТур".
_WORD = r"А-Яа-яЁёA-Za-z0-9"

_ROMAN = {"I": 1, "II": 2, "III": 3, "IV": 4}

_STAGE_RX = re.compile(
    r"(?:(?<![" + _WORD + r"])(\d|I{1,3}V?)\s*[-–]?\s*(?:й|го|ый)?\s*этап"
    r"|этап\s*[№#]?\s*(\d|I{1,3}V?)(?![" + _WORD + r"]))",
    re.IGNORECASE,
)

# Порядковый префикс: "01 ", "01_", "01. ", "1) "
_ORDER_RX = re.compile(r"^\s*\d{1,3}\s*[_.\-)]*\s*")

_DATE_RXS = [
    (re.compile(r"(?<!\d)(\d{4})[-._](\d{1,2})[-._](\d{1,2})(?!\d)"), ("y", "m", "d")),
    (re.compile(r"(?<!\d)(\d{1,2})[-._](\d{1,2})[-._](\d{4})(?!\d)"), ("d", "m", "y")),
    (re.compile(r"(?<!\d)(20\d{2})(\d{2})(\d{2})(?!\d)"), ("y", "m", "d")),
]

_SPB_RX = re.compile(r"\s*(?:г\.?\s*)?(?:СПб|Санкт-Петербурга?)\.?", re.IGNORECASE)

_STOPWORDS = {"от", "г", "гг", "по", "на", "для", "письмо", "исх", "вх", "№", "no"}

_DISTRICT_RX = re.compile(
    r"\s+([А-ЯЁ][А-Яа-яЁё\-]{2,})(?:\s+(?:района|р-на|район))?",
)


@dataclass
class OrgMatch:
    code: str
    name: str
    detail: str | None
    parent: str | None
    span: tuple[int, int]


@dataclass
class ParsedName:
    decision_id: str | None
    decision_label: str | None
    decision_kind: str | None
    org: OrgMatch | None
    stage: str | None
    doc_date: date | None
    note: str


def normalize(text: str) -> str:
    """NFC + единые дефисы и пробелы — имена приходят из разных источников."""
    text = unicodedata.normalize("NFC", text)
    text = text.replace(" ", " ")
    text = re.sub(r"[–—−]", "-", text)
    # Латинские омоглифы в кириллических сокращениях (C, K, P, T, A, O, B, E, H, M, X)
    return re.sub(r"\s+", " ", text).strip()


def build_org_matchers(config: Config) -> list[tuple[re.Pattern, Committee, str]]:
    """Регулярки для сокращений, от самых длинных к коротким.

    Длинные первыми — иначе "КЭП" перехватил бы "КЭПиСП".
    """
    items: list[tuple[re.Pattern, Committee, str]] = []
    for com in config.committees:
        for code in com.all_codes:
            rx = re.compile(
                r"(?<![" + _WORD + r"])" + re.escape(code) + r"(?![" + _WORD + r"])",
                re.IGNORECASE,
            )
            items.append((rx, com, code))
    items.sort(key=lambda it: len(it[2]), reverse=True)
    return items


def match_org(text: str, matchers) -> OrgMatch | None:
    """Находит сокращение организации в строке.

    Из всех совпадений выбираем самое раннее; при равном начале — самое длинное
    сокращение (это отсекает случаи вроде "КЭП" внутри "КЭПиСП").
    """
    best: OrgMatch | None = None
    best_len = -1
    for rx, com, code in matchers:
        m = rx.search(text)
        if not m:
            continue
        start, end = m.span()
        if best is not None and (start, -len(code)) >= (best.span[0], -best_len):
            continue

        detail = None
        spb = _SPB_RX.match(text[end:])
        if spb:
            end += spb.end()
        if com.district:
            dm = _DISTRICT_RX.match(text[end:])
            if dm:
                detail = dm.group(1)
                end += dm.end()

        best = OrgMatch(com.code, com.name, detail, com.parent, (start, end))
        best_len = len(code)
    return best


def match_decision(text: str, decisions: list[Decision]) -> tuple[Decision | None, tuple[int, int] | None]:
    for dec in decisions:
        for rx in dec.patterns:
            m = rx.search(text)
            if m:
                return dec, m.span()
    return None, None


def match_stage(text: str) -> tuple[str | None, tuple[int, int] | None]:
    m = _STAGE_RX.search(text)
    if not m:
        return None, None
    token = (m.group(1) or m.group(2) or "").upper()
    if token.isdigit():
        return token, m.span()
    if token in _ROMAN:
        return str(_ROMAN[token]), m.span()
    return None, None


def match_date(text: str) -> tuple[date | None, tuple[int, int] | None]:
    for rx, order in _DATE_RXS:
        for m in rx.finditer(text):
            parts = dict(zip(order, m.groups()))
            try:
                value = date(int(parts["y"]), int(parts["m"]), int(parts["d"]))
            except ValueError:
                continue
            if 2000 <= value.year <= date.today().year + 1:
                return value, m.span()
    return None, None


def _cut(text: str, spans: list[tuple[int, int]]) -> str:
    """Вырезает распознанные фрагменты, остаток — примечание."""
    keep = []
    last = 0
    for start, end in sorted(s for s in spans if s):
        if start >= last:
            keep.append(text[last:start])
            last = end
        else:
            last = max(last, end)
    keep.append(text[last:])
    rest = " ".join(keep)
    rest = re.sub(r"[_\-–—.,;]+", " ", rest)
    rest = re.sub(r"\s+", " ", rest).strip(" -_.")
    # служебные слова, не несущие смысла в примечании
    words = [w for w in rest.split() if w.lower() not in _STOPWORDS]
    return " ".join(words)


def parse_name(stem: str, config: Config, matchers) -> ParsedName:
    """Разбирает имя файла без расширения."""
    text = normalize(stem)
    text = _ORDER_RX.sub("", text)

    decision, dec_span = match_decision(text, config.decisions)
    stage, stage_span = match_stage(text)
    doc_date, date_span = match_date(text)

    # Организацию ищем в остатке после решения — так "Согласование" не мешает.
    org = match_org(text, matchers)

    spans = [s for s in (dec_span, stage_span, date_span) if s]
    if org:
        spans.append(org.span)

    return ParsedName(
        decision_id=decision.id if decision else None,
        decision_label=decision.label if decision else None,
        decision_kind=decision.kind if decision else None,
        org=org,
        stage=stage,
        doc_date=doc_date,
        note=_cut(text, spans),
    )
