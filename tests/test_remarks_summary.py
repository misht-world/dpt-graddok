"""Summary of sheets: alike items in one row, answers merged, chronology with «снято»; Word in both views."""

import io

from app.remarks_summary import build_summary, build_summary_word

A1 = "В расчетах транспортных потоков отсутствует уровень загрузки и уровень обслуживания Киевского шоссе."
A2 = "В расчётах транспортных потоков отсутствует уровень загрузки и уровень обслуживания Киевского шоссе!"


def item(pos, remark, answer="", status="work", note=""):
    return {"pos": pos, "remark": remark, "answer": answer, "note": note, "status": status,
            "answer_images": [], "note_images": []}


def sheet(sid, org, date, items):
    return {"sheet": {"id": sid, "letter_date": date, "letter_no": ""},
            "doc": {"id": sid * 10, "org_code": org, "project_type": "ППТЛ", "project_name": "Магистраль 1", "file_name": f"{org}.pdf"},
            "items": items}


def test_summary_and_chronology():
    kt1 = sheet(1, "КТ", "2026-10-21", [item(1, A1, "Уровень загрузки добавлен.", "done"), item(2, "Рисунок 2 нечитаем, заменить рисунок.", "Заменён.", "done")])
    krti = sheet(2, "КРТИ", "2026-10-22", [item(1, A2, ""), item(2, "Нет согласования с владельцами сетей связи.", "Согласовано письмом 15.")])
    kt2 = sheet(3, "КТ", "2026-12-12", [item(1, A2, "Уровень загрузки добавлен!", "done"), item(2, "Не учтены остановки трамвая на Киевском шоссе.")])
    s = build_summary([kt2, krti, kt1])                 # any order: letters go by date
    assert [l["org"] for l in s["letters"]] == ["КТ", "КРТИ", "КТ"]
    rows = s["rows"]
    assert len(rows) == 4
    a = rows[0]
    assert a["who"] == ["КТ 21-10-2026", "КРТИ 22-10-2026", "КТ 12-12-2026"]
    assert a["answer"] == "Уровень загрузки добавлен!"          # alike answers: one, the newest; the empty one skipped
    assert [c["state"] if c else None for c in a["cells"]] == ["new", "repeat", "repeat"]
    b = rows[1]
    assert [c["state"] if c else None for c in b["cells"]] == ["new", None, "dropped"]   # КТ did not repeat it
    c = rows[2]
    assert c["who"] == ["КРТИ 22-10-2026"] and c["cells"][2] is None                      # another committee: not «снято»
    assert rows[3]["cells"][2]["state"] == "new" and not rows[3]["done"]


def test_different_answers_are_labelled():
    kt = sheet(1, "КТ", "2026-10-21", [item(1, A1, "Добавлен раздел 4.")])
    krti = sheet(2, "КРТИ", "2026-10-22", [item(1, A2, "Учтено в томе 2, лист 7 с расчётом интенсивности.")])
    row = build_summary([kt, krti])["rows"][0]
    assert row["answer"] == "КТ 21-10-2026: Добавлен раздел 4.\n\nКРТИ 22-10-2026: Учтено в томе 2, лист 7 с расчётом интенсивности."


def test_word_both_views():
    from docx import Document

    s = build_summary([sheet(1, "КТ", "2026-10-21", [item(1, A1, "Да.")]), sheet(2, "КТ", "2026-12-12", [item(1, A2, "Да.")])])
    chrono = Document(io.BytesIO(build_summary_word(s, "chrono", True, lambda sha: None)))
    assert [c.text for c in chrono.tables[0].rows[0].cells] == ["№", "Замечание", "КТ 21-10-2026", "КТ 12-12-2026"]
    assert chrono.tables[0].rows[1].cells[3].text == "повторно. Да."
    plain = Document(io.BytesIO(build_summary_word(s, "combined", False, lambda sha: None)))
    assert [c.text for c in plain.tables[0].rows[0].cells] == ["№", "Замечание", "Кто", "Ответ"]


def test_word_names():
    from app.remarks import word_name

    L = lambda t, n, o, d, s=None: dict(project_type=t, project_name=n, org=o, letter_date=d, stage=s)  # noqa: E731
    assert word_name("answers", [L("ППТЛ", "Красносельское шоссе", "КТ", "2026-11-12")]) == "Ответы ППТЛ Красносельское шоссе КТ 12.11.docx"
    assert word_name("answers", [L("ДПТ", "Далта", "КГА", "2026-11-05", "2")], False) == "Ответы ДПТ Далта эт2 КГА 05.11 без прим.docx"
    assert word_name("combined", [L("ППТЛ", "Красносельское шоссе", "КТ", "2026-11-12"),
                                  L("ППТЛ", "Красносельское шоссе", "КРТИ", "2026-11-13")]) == "Сводная ППТЛ Красносельское шоссе КТ+КРТИ.docx"
    assert word_name("chrono", [L("ППТЛ", "Магистраль 1", "КТ", "2026-10-21"), L("ППТЛ", "Магистраль 2", "КТ", "2026-12-12")]) == \
        "Хронология ППТЛ 2 проекта КТ.docx"
