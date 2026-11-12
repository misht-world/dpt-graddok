"""Backup: DB snapshots and the blob mirror."""

import sqlite3
from datetime import datetime

from app.backup import Backup, mirror_blobs


def test_backup_snapshot_and_mirror(tmp_path):
    data, target = tmp_path / "data", tmp_path / "pc"
    data.mkdir(); target.mkdir()
    c = sqlite3.connect(data / "index.db")
    c.execute("CREATE TABLE t(x)"); c.execute("INSERT INTO t VALUES (1)"); c.commit(); c.close()
    (data / "blobs" / "ab").mkdir(parents=True)
    (data / "blobs" / "ab" / "abc").write_bytes(b"file")
    meta = {}
    b = Backup(data, target, meta.get, meta.__setitem__)
    assert not b.run()["ok"]          # same disk as the data: not a real backup
    b.allow_same_device = True        # in the test both folders are on one disk
    r = b.run()
    assert r["ok"], r
    day = datetime.now().date().isoformat()
    c = sqlite3.connect(target / "db" / day / "index.db")
    assert c.execute("SELECT x FROM t").fetchone() == (1,)
    c.close()
    assert (target / "blobs" / "ab" / "abc").read_bytes() == b"file"
    assert (data / "backups" / day / "index.db").is_file()
    assert not b.due()                                   # done for today

    (data / "blobs" / "ab" / "abc").unlink()             # deleted on the Pi → deleted in the copy
    assert mirror_blobs(data / "blobs", target / "blobs")[2] == 1


def test_backup_pc_off(tmp_path):
    data = tmp_path / "data"; data.mkdir()
    meta = {}
    r = Backup(data, tmp_path / "missing", meta.get, meta.__setitem__).run()
    assert not r["ok"] and "недоступна" in r["error"] and not meta.get("backup_last_ok")
