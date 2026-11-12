"""Organizations per project: custom organizations, requirements keyed by folder."""

import shutil
import sqlite3
from pathlib import Path

import pytest

from app.config import ConfigError, add_committee, load_config
from app.parsing import build_org_matchers, match_org
from app.store import Store, project_folder

ROOT = Path(__file__).resolve().parent.parent


def test_add_custom_organization_is_recognised(tmp_path, monkeypatch):
    shutil.copy(ROOT / "committees.yaml", tmp_path / "committees.yaml")
    shutil.copy(ROOT / "config.example.yaml", tmp_path / "config.yaml")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("COMMITTEES_FILE", raising=False)
    cfg = load_config(tmp_path / "config.yaml")
    known = {x.lower() for c in cfg.committees for x in c.all_codes}
    add_committee(tmp_path / "committees.yaml", "РЖД", "ОАО «Российские железные дороги»", ["ОАО РЖД"], known)
    cfg = load_config(tmp_path / "config.yaml")
    rzd = next(c for c in cfg.committees if c.code == "РЖД")
    assert rzd.custom and not rzd.active and rzd.aliases == ["ОАО РЖД"]
    assert match_org("Согласование РЖД 2 этап", build_org_matchers(cfg)).code == "РЖД"
    with pytest.raises(ConfigError):
        add_committee(tmp_path / "committees.yaml", "кт", "дубль", [], known)


def test_requirements_by_folder_and_old_keys(tmp_path):
    db = tmp_path / "i.db"
    st = Store(db)
    st.set_requirements("6:ДПТ - Далта", ["КТ", "КБ"])
    assert st.requirements() == {"ДПТ - Далта": ["КТ", "КБ"]}
    st.close()
    c = sqlite3.connect(db)
    c.execute("INSERT INTO project_requirements VALUES('7:ППТЛ - Магистраль 1','КТ','x')")
    c.commit(); c.close()
    st = Store(db)                                   # old «N:» keys are migrated on start
    assert st.requirements()["ППТЛ - Магистраль 1"] == ["КТ"]
    assert project_folder("12:АГПТ - X") == "АГПТ - X" and project_folder("ДПТ - Далта") == "ДПТ - Далта"
