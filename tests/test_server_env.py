"""Server adaptations for running behind tsdproxy on the Pi."""

from types import SimpleNamespace

from app.server import browse_roots, client_ip


def _req(headers, host="172.18.0.5"):
    return SimpleNamespace(headers=headers, client=SimpleNamespace(host=host))


def test_client_ip_prefers_forwarded_for():
    assert client_ip(_req({"x-forwarded-for": "100.64.1.2, 172.18.0.2"})) == "100.64.1.2"


def test_client_ip_falls_back_to_peer():
    assert client_ip(_req({})) == "172.18.0.5"


def test_browse_roots_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("BROWSE_ROOTS", str(tmp_path))
    assert browse_roots() == [str(tmp_path)]


def test_browse_roots_skips_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("BROWSE_ROOTS", str(tmp_path / "nope"))
    assert str(tmp_path / "nope") not in browse_roots()
