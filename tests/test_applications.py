"""Reading «Заявление №…» from the services portal (text as pypdf extracts it)."""

from pathlib import Path

from app.applications import is_application, parse_text
from app.config import load_config

ROOT = Path(__file__).resolve().parent.parent

IOGV = """Заявление № 172889115
Услуга Согласование документации ППТ с ИОГВ
Место подачи заявления РПГУ
Дата подачи заявления 14.04.2026
Дата планового ответа 05.05.2026
Номер документа основания 1 19 139
Дата документа основания 28.05.2025
Исполнительные органы государственной власти Санкт-Петербурга, с которыми
необходимо согласовать документацию
Комитет по благоустройству Нет
Комитет по развитию
транспортной инфраструктуры
Да
Комитет по транспорту Да
Комитет по энергетике и
инженерному обеспечению
Нет
3
            Заявление подписано простой ЭП
Иванов Иван Иванович
Документация
Администрация Колпинского
района
Нет
"""

KGA = """Заявление № 176699650
Услуга Проведение проверки документации по планировке территории и
подготовки по результатам такой проверки соответствующего
заключения
Место подачи заявления РПГУ
Дата подачи заявления 05.11.2026
Дата планового ответа 03.12.2026
Этап проверки документации по
планировке территории
II этап (документация)
Реквизиты распоряжения КГА о подготовке документации ППТ
Номер 1-20-185
Дата 27.07.2026
"""


def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    return load_config(ROOT / "config.example.yaml")


def test_iogv_application(tmp_path, monkeypatch):
    a = parse_text(IOGV, cfg(tmp_path, monkeypatch))
    assert (a.number, a.filed, a.due) == ("172889115", "2026-04-14", "2026-05-05")
    assert a.service == "Согласование документации ППТ с ИОГВ"
    assert a.committees == ["КРТИ", "КТ"] and not a.unknown
    assert a.order == "1-19-139 от 28.05.2025"


def test_kga_application(tmp_path, monkeypatch):
    a = parse_text(KGA, cfg(tmp_path, monkeypatch))
    assert a.committees == ["КГА"]
    assert (a.stage, a.stage_text) == ("2", "II этап (документация)")
    assert a.order == "1-20-185 от 27.07.2026"
    assert a.due == "2026-12-03"


def test_only_root_applications():
    assert is_application("Заявление №176699650.pdf")
    assert not is_application("Письма/Заявление №1.pdf")
    assert not is_application("Заявление.docx")
