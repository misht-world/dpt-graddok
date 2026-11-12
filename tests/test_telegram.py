"""Telegram message after «Актуализировать»: new letters with the link; nothing to say — no message."""

import time
from pathlib import Path

from app.config import load_config
from app.server import App

ROOT = Path(__file__).resolve().parent.parent


def test_message_after_publish(tmp_path, monkeypatch):
    import shutil
    shutil.copy(ROOT / "config.example.yaml", tmp_path / "config.yaml")
    shutil.copy(ROOT / "committees.yaml", tmp_path / "committees.yaml")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("CONFIG_FILE", raising=False)
    monkeypatch.delenv("COMMITTEES_FILE", raising=False)
    cfg = load_config(tmp_path / "config.yaml")
    assert not cfg.telegram["enabled"]
    cfg.telegram.update(enabled=True, bot_token="t", chat_ids=["1"], link="https://graddok.example.ts.net")
    app = App(cfg)
    sent = []
    monkeypatch.setattr(app.telegram, "_call", lambda method, payload: sent.append(payload["text"]) or {"ok": True})

    block = tmp_path / "work" / "ДПТ - Далта" / "08_Согласования" / "02_Согласования ИОГВ"
    block.mkdir(parents=True)
    (block / "Замечания КТ.pdf").write_bytes(b"%PDF remarks")
    app.store.add_source(str(tmp_path / "work"))
    app.publish("first")
    for _ in range(50):
        if sent:
            break
        time.sleep(0.05)
    assert len(sent) == 1
    assert "ГРАДДОК — новые письма" in sent[0] and "Замечания КТ.pdf" in sent[0]
    assert sent[0].rstrip().endswith("https://graddok.example.ts.net")
    assert app.telegram.pending().empty
