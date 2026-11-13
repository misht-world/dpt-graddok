"""HTTP routes for sendings and their answers (stage 2a, «Сроки»)."""

from __future__ import annotations

from datetime import date, timedelta

from fastapi import Body, Depends, FastAPI, HTTPException

from .sendings import DEFAULT_DAYS, STATUSES, SendingStore, compute, from_release, plus_days

STALE_DAYS = 3


def register_sending_routes(api: FastAPI, state, releases, require_viewer, require_admin) -> SendingStore:
    cfg = state.config
    store = SendingStore(cfg.data_dir / "releases.db")

    def make_for_release(release_id: int) -> None:
        """Creates the sendings of a release once (after its copy and applications are read)."""
        r = releases.release(release_id)
        if not r or r.get("sendings_made"):
            return
        stale = None
        if r.get("released_at"):
            stale = (date.fromisoformat(r["released_at"][:10]) - timedelta(days=STALE_DAYS)).isoformat()
        for item in from_release(r, stale, term_of):
            store.add(item)
        releases.mark_sendings_made(release_id)

    def sync_release(release_id: int) -> None:
        """After the admin edits a release: its sendings follow it — one per addressee,
        same project, stage and date. Manual answers and statuses of kept ones stay."""
        r = releases.release(release_id)
        if not r or not r.get("sendings_made"):
            return
        wanted = {item["org_code"]: item for item in from_release(r, None, term_of)}
        current = store.for_release(release_id)
        for s in current:
            if s["org_code"] not in wanted:
                store.delete(s["id"])
                continue
            item = wanted.pop(s["org_code"])
            changes = {k: item[k] for k in ("project_type", "project_name", "stage") if s.get(k) != item[k]}
            if s["sent_at"] != item["sent_at"]:
                changes["sent_at"] = item["sent_at"]
                if not s.get("application_no"):        # a planned date from an application stays
                    changes["due"] = item["due"]
            if changes:
                store.update(s["id"], changes)
        for item in wanted.values():
            store.add(item)

    state.sync_release_sendings = sync_release

    def term_of(code: str) -> int | None:
        c = next((x for x in cfg.committees if x.code == code), None)
        return c.term if c else DEFAULT_DAYS

    def current_orders() -> list[dict]:
        """One per project: the one valid longest; without a date — the newest file."""
        out: dict[tuple, dict] = {}
        for o in state.store.orders():
            key = (o["project_type"] or "", o["project_name"] or "")
            if key not in out:
                out[key] = {"project_type": o["project_type"], "project_name": o["project_name"],
                            "number": (o["note"] or "").removeprefix("№ ") or o["file_name"],
                            "file_name": o["file_name"], "doc_id": o["id"], "valid_until": o["valid_until"]}
        return list(out.values())

    def computed() -> list[dict]:
        # documents gone from the PC still count as answers (their copy stays on the server)
        return compute(store.all(), state.store.documents(include_removed=True))

    def check(payload: dict) -> dict:
        if "status" in payload and payload["status"] not in (None, "", *STATUSES):
            raise HTTPException(400, "Неизвестный статус")
        if payload.get("answer_mode") not in (None, "", "manual", "none"):
            raise HTTPException(400, "Неизвестный режим ответа")
        for k in ("sent_at", "due", "answer_date"):
            v = payload.get(k)
            if v:
                try:
                    date.fromisoformat(str(v)[:10])
                except ValueError:
                    raise HTTPException(400, f"Неверная дата: {v}") from None
                payload[k] = str(v)[:10]
        return payload

    @api.get("/api/sendings")
    def sendings(role: str = Depends(require_viewer)):
        return {
            "sendings": computed(),
            # sheets of answers to remarks by letter id: «ответы 3 из 16» next to the answer
            "remarks": {str(k): v for k, v in (state.remarks.summary() if getattr(state, "remarks", None) else {}).items()},
            "statuses": STATUSES,
            "default_days": DEFAULT_DAYS,
            "today": date.today().isoformat(),
            "projects": [{"project_type": p["project_type"], "project_name": p["project_name"], "folder": p["folder"]}
                         for p in state.project_list() if not p["archived"]],
            "archived": [p["folder"] for p in state.project_list() if p["archived"]],
            # the current «Распоряжение» of each project: the red line of the plan-graph
            "orders": current_orders(),
            "committees": [{"code": c.code, "name": c.name, "active": c.active, "final": c.final}
                           for c in cfg.committees],
        }

    @api.get("/api/releases/{release_id}/sendings")
    def release_sendings(release_id: int, role: str = Depends(require_viewer)):
        return {"sendings": [s for s in computed() if s.get("release_id") == release_id]}

    @api.post("/api/admin/sendings")
    def add(payload: dict = Body(...), role: str = Depends(require_admin)):
        payload = check(payload)
        codes = [str(c).strip() for c in payload.get("org_codes") or [] if str(c).strip()]
        if not payload.get("project_name") or not payload.get("sent_at") or not codes:
            raise HTTPException(400, "Нужны проект, дата отправки и хотя бы один комитет")
        ids = []
        for code in codes:
            due = payload.get("due") or (plus_days(payload["sent_at"], term_of(code)) if term_of(code) else None)
            ids.append(store.add({**payload, "org_code": code, "stage": payload.get("stage") or None, "due": due}))
        return {"ids": ids}

    @api.patch("/api/admin/sendings/{sid}")
    def update(sid: int, payload: dict = Body(...), role: str = Depends(require_admin)):
        if not store.get(sid):
            raise HTTPException(404, "Отправка не найдена")
        store.update(sid, check(payload))
        return next((s for s in computed() if s["id"] == sid), None)

    @api.delete("/api/admin/sendings/{sid}")
    def delete(sid: int, role: str = Depends(require_admin)):
        store.delete(sid)
        return {"ok": True}

    @api.get("/api/admin/sendings/{sid}/candidates")
    def candidates(sid: int, role: str = Depends(require_admin)):
        """Documents of the same committee and project — to link an answer by hand."""
        s = store.get(sid)
        if not s:
            raise HTTPException(404, "Отправка не найдена")
        name = " ".join(s["project_name"].lower().split())
        out = [{"id": d["id"], "file_name": d["file_name"], "decision_label": d.get("decision_label"),
                "stage": d.get("stage"), "date": d.get("doc_date") or (d.get("mtime") or "")[:10]}
               for d in state.store.documents()
               if d.get("org_code") == s["org_code"] and " ".join((d["project_name"] or "").lower().split()) == name]
        return {"candidates": sorted(out, key=lambda d: d["date"] or "", reverse=True)}

    state.sendings = store
    state.sendings_computed = computed   # remarks (app/remarks.py): which round a letter answers
    state.make_sendings = make_for_release
    return store
