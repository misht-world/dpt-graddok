"""Summary of several sheets of remarks: alike items in one row; two views of the same rows.

- «Сводная»: № | Замечание | Кто | Ответ | Примечание — e.g. КТ, КРТИ and КЭИО on one project.
- «Хронология»: № | Замечание | one column per letter (oldest first) — e.g. КТ round 1, 2, 3:
  what repeated, what was dropped («снято»), what is new.

Read only (decision of 13.11): answers are written in the sheets. In a row the answers are merged:
the empty ones are skipped, alike ones (≥ 90 %) are given once, different ones each with «from whom».
"""

from __future__ import annotations

import io
import logging

from .remarks import REPEAT_RATIO, STATUSES, _ddmmyyyy, similarity

log = logging.getLogger("remarks")


def _letter_label(s: dict) -> str:
    return " ".join(x for x in (s.get("org") or "—", _ddmmyyyy(s.get("letter_date"))) if x)


def _merge(entries: list[dict], key: str, img_key: str | None) -> tuple[str, list[dict]]:
    """entries: [{label, text, images}] oldest first. Returns the merged text and pictures."""
    kept: list[dict] = []
    for e in entries:
        text = (e.get(key) or "").strip()
        if not text and not (img_key and e.get(img_key)):
            continue
        same = next((k for k in kept if text and similarity(text, k["text"]) >= REPEAT_RATIO), None)
        if same:                                   # alike: keep one, the newest wording
            same.update(text=text, images=list(e.get(img_key) or []) if img_key else [], label=e["label"])
            continue
        kept.append({"label": e["label"], "text": text, "images": list(e.get(img_key) or []) if img_key else []})
    if not kept:
        return "", []
    if len(kept) == 1:
        return kept[0]["text"], kept[0]["images"]
    text = "\n\n".join(f"{k['label']}: {k['text']}" for k in kept)
    images = [img for k in kept for img in k["images"]]
    return text, images


def build_summary(sheets: list[dict]) -> dict:
    """sheets: [{"sheet": row, "doc": doc, "items": [...]}] → letters (columns) and rows (groups)."""
    letters = []
    for s in sorted(sheets, key=lambda x: (x["sheet"].get("letter_date") or "", x["sheet"]["id"])):
        doc = s["doc"]
        letters.append({"sheet_id": s["sheet"]["id"], "org": doc.get("org_code"), "org_name": doc.get("org_name"),
                        "letter_date": s["sheet"].get("letter_date"), "letter_no": s["sheet"].get("letter_no") or "",
                        "file_name": doc.get("file_name"), "doc_id": doc.get("id"),
                        "project": " ".join(x for x in (doc.get("project_type"), doc.get("project_name")) if x),
                        "project_type": doc.get("project_type"), "project_name": doc.get("project_name"),
                        "stage": doc.get("stage"),
                        "items": s["items"]})
    groups: list[dict] = []
    for li, letter in enumerate(letters):
        for it in letter["items"]:
            text = it.get("remark") or ""
            best = None
            for g in groups:
                r = max(similarity(text, m["remark"]) for m in g["members"])
                if r >= REPEAT_RATIO and (best is None or r > best[0]):
                    best = (r, g)
            member = {"letter": li, "pos": it.get("pos"), "remark": text, "status": it.get("status") or "work",
                      "answer": it.get("answer") or "", "note": it.get("note") or "",
                      "answer_images": it.get("answer_images") or [], "note_images": it.get("note_images") or []}
            if best:
                best[1]["members"].append(member)
            else:
                groups.append({"members": [member]})

    rows = []
    for g in groups:
        members = g["members"]
        labelled = [{**m, "label": _letter_label(letters[m["letter"]])} for m in members]
        answer, answer_images = _merge(labelled, "answer", "answer_images")
        note, note_images = _merge(labelled, "note", "note_images")
        present = {m["letter"] for m in members}
        cells = []
        for li, letter in enumerate(letters):
            m = next((x for x in members if x["letter"] == li), None)
            if m:
                cells.append({"state": "repeat" if any(x["letter"] < li for x in members) else "new",
                              "status": m["status"], "answer": m["answer"], "answer_images": m["answer_images"],
                              "pos": m["pos"]})
            elif any(x < li and letters[x]["org"] == letter["org"] for x in present):
                cells.append({"state": "dropped"})        # the same committee did not repeat it: removed
            else:
                cells.append(None)
        orgs = []
        for m in members:
            lab = _letter_label(letters[m["letter"]])
            if lab not in orgs:
                orgs.append(lab)
        rows.append({"remark": members[-1]["remark"], "who": orgs, "answer": answer, "answer_images": answer_images,
                     "note": note, "note_images": note_images,
                     "done": all(m["status"] != "work" for m in members), "cells": cells,
                     "first": min(m["letter"] for m in members)})
    rows.sort(key=lambda r: r["first"])
    for r in rows:
        r.pop("first")
    for letter in letters:
        letter.pop("items")
    return {"letters": letters, "rows": rows, "statuses": STATUSES}


CELL_TEXT = {"new": "", "repeat": "повторно. ", "dropped": "снято"}


def build_summary_word(summary: dict, mode: str, with_notes: bool, blob_path) -> bytes:
    from docx import Document
    from docx.enum.section import WD_ORIENT
    from docx.oxml import OxmlElement
    from docx.shared import Cm, Pt

    word = Document()
    section = word.sections[0]
    section.orientation = WD_ORIENT.LANDSCAPE
    section.page_width, section.page_height = Cm(27.94), Cm(21.59)
    section.top_margin, section.bottom_margin = Cm(1.25), Cm(2.0)
    section.left_margin = section.right_margin = Cm(1.8)
    usable = 27.94 - 3.6

    letters = summary["letters"]
    projects = sorted({x["project"] for x in letters if x["project"]})
    p = word.add_paragraph()
    p.paragraph_format.space_after = Pt(2)
    run = p.add_run(("Хронология замечаний" if mode == "chrono" else "Сводная таблица замечаний")
                    + (": " + ", ".join(projects) if projects else ""))
    run.bold = True
    run.font.name = "Cambria"
    p = word.add_paragraph()
    p.add_run("Письма: " + "; ".join(_letter_label(x) + (f" № {x['letter_no']}" if x["letter_no"] else "")
                                      for x in letters)).font.name = "Cambria"

    if mode == "chrono":
        rest = usable - 1.0 - 8.0
        widths = [1.0, 8.0] + [rest / max(1, len(letters))] * len(letters)
        heads = ["№", "Замечание"] + [_letter_label(x) for x in letters]
    elif with_notes:
        widths = [1.0, 8.5, 2.6, 7.0, usable - 19.1]
        heads = ["№", "Замечание", "Кто", "Ответ", "Примечание"]
    else:
        widths = [1.0, 10.0, 2.6, usable - 13.6]
        heads = ["№", "Замечание", "Кто", "Ответ"]

    table = word.add_table(rows=1, cols=len(widths))
    table.style = "Light Grid Accent 1"
    for cell, text, w in zip(table.rows[0].cells, heads, widths):
        cell.width = Cm(w)
        cell.text = text
        for r in cell.paragraphs[0].runs:
            r.font.bold = True
            r.font.name = "Cambria"
    table.rows[0]._element.get_or_add_trPr().append(OxmlElement("w:tblHeader"))

    def fill(cell, text: str, images: list[dict], width: float) -> None:
        cell.text = text or ""
        for r in cell.paragraphs[0].runs:
            r.font.name = "Cambria"
        for img in images or []:
            path = blob_path(img.get("sha", ""))
            if path:
                try:
                    cell.add_paragraph().add_run().add_picture(io.BytesIO(path.read_bytes()), width=Cm(max(1.0, width - 0.4)))  # stream: blobs have no extension
                except Exception as exc:  # noqa: BLE001
                    log.warning("картинка %s не вставлена: %s", img.get("sha"), exc)

    for idx, row in enumerate(summary["rows"], 1):
        tr = table.add_row()
        cells = tr.cells
        for c, w in zip(cells, widths):
            c.width = Cm(w)
        fill(cells[0], str(idx), [], widths[0])
        fill(cells[1], row["remark"], [], widths[1])
        if mode == "chrono":
            for i, cell in enumerate(row["cells"]):
                if not cell:
                    fill(cells[2 + i], "", [], widths[2 + i])
                    continue
                if cell["state"] == "dropped":
                    fill(cells[2 + i], "снято", [], widths[2 + i])
                    continue
                text = CELL_TEXT[cell["state"]] + (cell["answer"] or STATUSES.get(cell["status"], ""))
                fill(cells[2 + i], text, cell.get("answer_images") or [], widths[2 + i])
        else:
            fill(cells[2], ", ".join(row["who"]), [], widths[2])
            fill(cells[3], row["answer"], row["answer_images"], widths[3])
            if with_notes:
                fill(cells[4], row["note"], row["note_images"], widths[4])
        tr._element.get_or_add_trPr().append(OxmlElement("w:cantSplit"))

    buf = io.BytesIO()
    word.save(buf)
    return buf.getvalue()
