"""Share links: one object for people without an account, inside the program for those with one, 30 days."""

import sqlite3
import time
from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from test_projects import client  # noqa: F401  (fixture: admin signed in, a folder with a release)


def register(client):
    folder = client.work / "АЕ" / "АГПТ - Далта"
    client.post("/api/admin/projects", json={"project_type": "АГПТ", "project_name": "Далта", "pc_path": str(folder)})
    rel = folder / "04_Выпуск" / "АГПТ Далта - Выпуск"
    pv = client.get("/api/admin/releases/preview", params={"path": str(rel)}).json()
    body = {k: pv[k] for k in ("title", "released_at", "project_type", "project_name", "project_folder", "stage", "label")}
    job = client.post("/api/admin/releases", json={**body, "path": str(rel), "addressees": ["КЭИО"]}).json()
    for _ in range(100):
        if client.get(f"/api/admin/releases/jobs/{job['id']}").json()["state"] != "copying":
            break
        time.sleep(0.05)
    return job["release_id"]


def test_share_links(client):  # noqa: F811
    rid = register(client)
    files = client.get(f"/api/releases/{rid}").json()["files"]
    fid = files[0]["id"]

    s = client.post("/api/admin/shares", json={"kind": "rfile", "id": fid, "release_id": rid}).json()
    assert s["state"] == "active" and len(s["token"]) >= 20
    again = client.post("/api/admin/shares", json={"kind": "rfile", "id": fid, "release_id": rid}).json()
    assert again["token"] == s["token"]          # one object — one active link
    whole = client.post("/api/admin/shares", json={"kind": "release", "id": rid}).json()

    # signed in: straight into the program
    r = client.get(s["url"], follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/#release={rid}&file={fid}"

    with TestClient(client.app) as guest:
        assert guest.get("/api/releases").status_code == 401           # nothing else without an account
        assert guest.get(s["url"]).status_code == 200                  # the share page
        data = guest.get("/api/s/" + s["token"]).json()
        assert data["kind"] == "rfile" and [f["id"] for f in data["files"]] == [fid]
        assert guest.get(f"{s['url']}/file/{fid}?dl=1").content == b"%PDF album"
        assert guest.get(f"{s['url']}/zip").status_code == 404          # a file link gives no archive
        assert guest.get(f"/s/{whole['token']}/zip").status_code == 200
        assert guest.get(f"/s/{whole['token']}/file/{fid}").status_code == 200
        assert guest.get(f"/s/{whole['token']}/file/999999").status_code == 404
        assert guest.get("/api/s/not-a-real-token-at-all").status_code == 404

        listing = client.get("/api/admin/shares").json()["shares"]
        assert next(x for x in listing if x["token"] == s["token"])["hits"] == 1
        assert "created_by" not in listing[0]

        client.post(f"/api/admin/shares/{s['token']}/revoke")
        r = guest.get("/api/s/" + s["token"])
        assert r.status_code == 410 and "отключена" in r.json()["detail"]
        assert guest.get(f"{s['url']}/file/{fid}").status_code == 410

        # 30 days later
        past = (datetime.now().astimezone() - timedelta(days=1)).isoformat(timespec="seconds")
        conn = sqlite3.connect(client.data_dir / "index.db")
        conn.execute("UPDATE shares SET expires_at=? WHERE token=?", (past, whole["token"]))
        conn.commit()
        conn.close()
        r = guest.get("/api/s/" + whole["token"])
        assert r.status_code == 410 and "истёк" in r.json()["detail"]
