"""Точка входа.

  python run.py            — запустить сервер
  python run.py scan       — показать, что нашлось, ничего не публикуя
  python run.py publish    — обновить индекс из командной строки
  python run.py check      — проверить, есть ли изменения с последней публикации
"""

from __future__ import annotations

import argparse
import logging
import os
import socket
import sys

from app.config import ConfigError, load_config
from app.scanner import Source, fingerprint, scan
from app.store import Store


def _setup_console() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass


def _hold() -> None:
    """Не даёт окну закрыться, когда программу запустили двойным щелчком.

    Из ZAPUSK.bat не срабатывает: там свой pause, два подряд только мешают.
    """
    if os.environ.get("DPT_FROM_BAT"):
        return
    try:
        if sys.stdin is not None and sys.stdin.isatty():
            input("  Нажмите Enter, чтобы закрыть окно...")
    except (EOFError, OSError):
        pass


def _port_taken(host: str, port: int) -> bool:
    bind_host = "" if host == "0.0.0.0" else host
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((bind_host, port))
            return False
        except OSError:
            return True


def _already_running(port: int) -> bool:
    """Отвечает ли на этом порту наш же каталог."""
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/login", timeout=3) as resp:
            return resp.status == 200 and "ГРАДДОК" in resp.read().decode(
                "utf-8", "replace"
            )
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _open_store(config):
    """Открывает индекс и при первом запуске переносит папки из config.yaml."""
    store = Store(config.data_dir / "index.db")
    if not store.sources():
        for path in config.sources:
            if path.is_dir():
                store.add_source(str(path))
    return store


def _sources(store) -> list:
    return [Source.from_row(r) for r in store.sources(enabled_only=True)]


def cmd_serve(args) -> int:
    import uvicorn

    from app.server import create_app

    config = load_config(args.config)
    host_label = "localhost" if config.host in ("0.0.0.0", "127.0.0.1") else config.host
    lines = [
        "",
        "  ГРАДДОК",
        "  адрес       : http://{}:{}/".format(host_label, config.port),
        "  панель      : http://{}:{}/admin".format(host_label, config.port),
        "  остановить  : Ctrl+C или закрыть это окно",
        "",
    ]
    if config.viewer_password in ("", "smena-parol") or config.admin_password in ("", "smena-parol-admin"):
        lines += [
            "  ВНИМАНИЕ: пароли не изменены — откройте config.yaml и задайте свои.",
            "",
        ]
    # flush: при перенаправлении вывода буфер иначе не доходит до файла
    for line in lines:
        print(line)
    sys.stdout.flush()

    if _port_taken(config.host, config.port):
        print("  Порт {} занят — запустить не получится.".format(config.port))
        print()
        if _already_running(config.port):
            print("  Каталог уже запущен. Ищите другое окно программы")
            print("  или просто откройте http://{}:{}/".format(host_label, config.port))
        else:
            print("  Его занимает другая программа. Закройте её либо укажите")
            print("  другой порт в config.yaml, раздел server.")
        print()
        _hold()
        return 3

    uvicorn.run(create_app(config), host=config.host, port=config.port, log_level="warning")
    return 0


def cmd_scan(args) -> int:
    config = load_config(args.config)
    store = _open_store(config)
    sources = _sources(store)
    store.close()

    if not sources:
        print("\nНе указано ни одной папки с проектами.")
        print("Добавьте их в панели управления или в config.yaml (раздел sources).")
        return 1

    docs, projects = scan(config, sources, with_hash=False)

    print("\nПапки с проектами:")
    for src in sources:
        print(f"  {src.path}")
    print(f"\nПроектов: {len(projects)}, документов: {len(docs)}\n")
    for p in projects:
        groups = ", ".join(p.groups) or "—"
        print(f"  {p.dir.name}  [тип: {p.type_code or '?'}, этапы: {'да' if p.has_stages else 'нет'}, группы: {groups}]")

    if docs:
        print()
        header = "{:<18} {:<5} {:<14} {:<8} {}".format("ПРОЕКТ", "ЭТАП", "РЕШЕНИЕ", "ОРГАН", "ФАЙЛ")
        print(header)
        print("-" * max(len(header), 80))
        for d in docs:
            print("{:<18} {:<5} {:<14} {:<8} {}".format(
                d["project_name"][:18], d["stage"] or "—",
                d["decision_label"][:14], (d["org_code"] or "?")[:8], d["file_name"]))

    unknown = [d for d in docs if not d["org_code"] or d["decision_id"] == "unknown"]
    if unknown:
        print(f"\nНе распознано полностью ({len(unknown)}):")
        for d in unknown:
            missing = []
            if not d["org_code"]:
                missing.append("орган")
            if d["decision_id"] == "unknown":
                missing.append("решение")
            print(f"  [{', '.join(missing)}] {d['abs_path']}")
    return 0


def cmd_publish(args) -> int:
    config = load_config(args.config)
    store = _open_store(config)
    sources = _sources(store)
    if not sources:
        store.close()
        print("Не указано ни одной папки с проектами.")
        return 1
    docs, _ = scan(config, sources)
    fp = fingerprint(config, sources)
    stats = store.publish(docs, fp, args.note or "командная строка")
    store.set_meta("published_fingerprint", fp)
    store.close()
    print(
        "Публикация #{publication_id}: всего {files_total}, новых {files_new}, "
        "изменено {files_changed}, перемещено {files_moved}, удалено {files_removed}".format(**stats)
    )
    return 0


def cmd_check(args) -> int:
    config = load_config(args.config)
    store = _open_store(config)
    current = fingerprint(config, _sources(store))
    published = store.get_meta("published_fingerprint")
    store.close()
    if current == published:
        print("Изменений нет.")
        return 0
    print("Есть изменения — требуется «Актуализировать».")
    return 1


def main(argv: list[str] | None = None) -> int:
    _setup_console()
    parser = argparse.ArgumentParser(description="ГРАДДОК ДПТ / ППТЛ")
    parser.add_argument("-c", "--config", help="путь к config.yaml")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("serve", help="запустить веб-сервер").set_defaults(func=cmd_serve)
    sub.add_parser("scan", help="показать найденное без публикации").set_defaults(func=cmd_scan)
    p_pub = sub.add_parser("publish", help="обновить индекс")
    p_pub.add_argument("--note", help="комментарий к публикации")
    p_pub.set_defaults(func=cmd_publish)
    sub.add_parser("check", help="проверить наличие изменений").set_defaults(func=cmd_check)

    args = parser.parse_args(argv)
    func = getattr(args, "func", cmd_serve)
    try:
        return func(args)
    except ConfigError as exc:
        print()
        for line in str(exc).splitlines():
            print("  " + line)
        print()
        _hold()
        return 2
    except KeyboardInterrupt:
        print("\nОстановлено.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
