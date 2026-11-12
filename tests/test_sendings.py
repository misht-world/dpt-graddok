"""Sendings: automatic answer, rounds, manual overrides, release → sendings."""

from app.sendings import SendingStore, compute, from_release


def doc(i, org, kind, when, stage="2", name="Далта", label=None):
    return {"id": i, "org_code": org, "decision_kind": kind, "decision_label": label or kind, "mtime": when + "T10:00:00",
            "doc_date": None, "project_type": "ДПТ", "project_name": name, "stage": stage, "file_name": f"{i}.pdf"}


def sending(i, org, sent, **kw):
    return {"id": i, "project_type": "ДПТ", "project_name": "Далта", "stage": "2", "org_code": org,
            "sent_at": sent, "due": kw.pop("due", None), "status": None, "answer_mode": None,
            "answer_doc_id": None, "answer_date": None, **kw}


def test_first_answer_after_sending_and_rounds():
    docs = [doc(1, "КТ", "positive", "2026-01-10"),        # before the sending: not an answer
            doc(2, "КТ", "neutral", "2026-03-01"),         # remarks → refused, round 1
            doc(3, "КТ", "positive", "2026-04-20"),        # round 2
            doc(4, "КТ", "info", "2026-03-02"),            # an application is not an answer
            doc(5, "КТ", "positive", "2026-03-05", stage="1")]
    out = compute([sending(1, "КТ", "2026-02-16"), sending(2, "КТ", "2026-04-01")], docs, today="2026-05-01")
    r1, r2 = out
    assert (r1["round"], r1["state"], r1["answer"]["id"], r1["days"]) == (1, "refused", 2, 13)
    assert (r2["round"], r2["state"], r2["answer"]["id"], r2["days"]) == (2, "approved", 3, 19)


def test_waiting_overdue_and_manual():
    s = sending(1, "КЭИО", "2026-10-12", due="2026-10-30")
    r = compute([s], [], today="2026-11-10")[0]
    assert r["state"] == "waiting" and r["overdue"] and r["days"] == 29
    r = compute([{**s, "status": "approved", "answer_date": "2026-11-01"}], [], today="2026-11-10")[0]
    assert r["state"] == "approved" and r["days"] == 20 and not r["overdue"]
    docs = [doc(7, "КЭИО", "positive", "2026-11-03", name="Далта")]
    assert compute([{**s, "answer_mode": "none"}], docs, today="2026-11-10")[0]["answer"] is None
    assert compute([s], docs, today="2026-11-10")[0]["answer"]["id"] == 7


def test_from_release_uses_application_and_default_due():
    rel = {"id": 1, "released_at": "2026-11-05T13:10", "project_type": "ДПТ", "project_name": "Далта", "stage": "2",
           "addressees": ["КГА", "КТ"],
           "applications": [{"number": "176699650", "due": "2026-12-03", "filed": "2026-11-05", "committees": ["КГА"]},
                            {"number": "1", "due": "2026-05-01", "filed": "2026-04-01", "committees": ["КТ"]}]}
    out = from_release(rel, stale_before="2026-11-02")
    assert [(s["org_code"], s["due"], s["application_no"]) for s in out] == [
        ("КГА", "2026-12-03", "176699650"), ("КТ", "2026-12-05", None)]


def test_store_roundtrip(tmp_path):
    from app.releases import ReleaseStore
    ReleaseStore(tmp_path / "r.db")
    st = SendingStore(tmp_path / "r.db")
    sid = st.add({"project_name": "Далта", "org_code": "КТ", "sent_at": "2026-02-16"})
    assert st.get(sid)["due"] is None            # the caller decides the term (per organization)
    st.update(sid, {"status": "withdrawn", "note": ""})
    assert st.get(sid)["status"] == "withdrawn" and st.get(sid)["note"] is None


def test_unanswered_round_ends_at_resend():
    out = compute([sending(1, "КЭИО", "2026-04-14", due="2026-05-05"), sending(2, "КЭИО", "2026-10-12")],
                  [doc(9, "КЭИО", "positive", "2026-11-03")], today="2026-11-10")
    assert (out[0]["state"], out[0]["days"], out[0]["overdue"]) == ("unanswered", 181, False)
    assert (out[1]["state"], out[1]["days"]) == ("approved", 22)


def test_from_release_term_per_organization():
    rel = {"id": 1, "released_at": "2026-11-05T10:00", "project_type": "АГПТ", "project_name": "Далта",
           "addressees": ["Заказчик", "КС"], "applications": []}
    out = from_release(rel, term=lambda code: None if code == "Заказчик" else 30)
    assert [(s["org_code"], s["due"]) for s in out] == [("Заказчик", None), ("КС", "2026-12-05")]
