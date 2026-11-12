"""Telegram messages about changes in the catalog (copied from dpt-doc-change).

In ГРАДДОК the message goes right after «Актуализировать»: the copies are on the server
at once, so the link works. If sending fails (no internet), the summary waits in the
index and goes with the next «Актуализировать».
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime

from .config import Config
from .store import Store

log = logging.getLogger("notify")

API = "https://api.telegram.org/bot{token}/{method}"
TIMEOUT = 30
# Телеграм не принимает сообщения длиннее 4096 символов.
MAX_TEXT = 3900
MAX_LISTED = 40
PENDING_KEY = "telegram_pending"


class TelegramError(Exception):
    pass


# Как называем состояние документа в сообщении.
STATUS_LABEL = {"new": "новый", "changed": "обновлён", "removed": "удалён"}
STATUS_ORDER = ("new", "changed", "removed")


@dataclass
class Pending:
    """Что накопилось с прошлой отправки.

    Один список на все состояния: и новые, и обновлённые, и удалённые.
    Раньше обновлённые считались числом — из-за этого сообщение про
    обновление приходило без перечня файлов.
    """

    items: list[dict] = field(default_factory=list)
    since: str | None = None

    def to_json(self) -> str:
        return json.dumps({"items": self.items, "since": self.since}, ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str | None) -> "Pending":
        if not raw:
            return cls()
        try:
            data = json.loads(raw)
        except ValueError:
            return cls()
        items = list(data.get("items") or [])
        # Сводка, сохранённая старой версией: там были только новые.
        for old in data.get("new") or []:
            items.append({**old, "status": "new"})
        return cls(items=items, since=data.get("since"))

    @property
    def empty(self) -> bool:
        return not self.items

    def counts(self) -> dict[str, int]:
        out = {k: 0 for k in STATUS_ORDER}
        for item in self.items:
            out[item.get("status", "new")] = out.get(item.get("status", "new"), 0) + 1
        return out


def _row_brief(row: dict, status: str) -> dict:
    """Оставляем только то, что нужно для сообщения."""
    return {
        "project": (row.get("project_type") + " · " if row.get("project_type") else "")
        + (row.get("project_name") or ""),
        "stage": row.get("stage") or "",
        "file": row.get("file_name") or "",
        "status": status,
    }


def merge_item(items: list[dict], brief: dict) -> None:
    """Складывает несколько «Актуализировать» в одну запись на файл.

    Правила: появился и тут же исчез — в сообщение не попадает вовсе,
    адресаты его и не видели. Новый, который потом переписали, остаётся
    новым: для получателя это по-прежнему первое появление.
    """
    key = (brief["project"], brief["stage"], brief["file"])
    for i, existing in enumerate(items):
        if (existing["project"], existing["stage"], existing["file"]) != key:
            continue
        was, now = existing.get("status", "new"), brief["status"]
        if now == "removed" and was == "new":
            items.pop(i)
        elif was == "new" and now == "changed":
            pass  # остаётся новым
        else:
            existing["status"] = now
        return
    items.append(brief)


def build_message(pending: Pending, link: str) -> str:
    """Собирает текст: файлы по проектам, у каждого — что с ним произошло."""
    counts = pending.counts()
    only_new = counts["new"] == len(pending.items)
    lines = [
        "ГРАДДОК — " + ("новые письма" if only_new else "изменения в письмах"),
        "",
    ]

    groups: dict[str, list[dict]] = {}
    for item in pending.items[:MAX_LISTED]:
        title = item["project"] + (f", {item['stage']} этап" if item["stage"] else "")
        groups.setdefault(title, []).append(item)

    for title, items in groups.items():
        lines.append(title)
        for item in items:
            label = STATUS_LABEL.get(item.get("status", "new"), "")
            lines.append(f"• {item['file']}" + (f" — {label}" if label else ""))
        lines.append("")

    hidden = len(pending.items) - MAX_LISTED
    if hidden > 0:
        lines.append(f"…и ещё {hidden} документ(ов)")
        lines.append("")

    totals = []
    for key, word in (("new", "новых"), ("changed", "обновлено"), ("removed", "удалено")):
        if counts[key]:
            totals.append(f"{word}: {counts[key]}")
    if totals:
        lines.append(", ".join(totals).capitalize())

    if link:
        lines.append("")
        lines.append(link)

    nl = chr(10)
    text = nl.join(lines)
    if len(text) > MAX_TEXT:
        text = text[:MAX_TEXT].rsplit(nl, 1)[0] + nl + "…" + nl + nl + link
    return text


class TelegramNotifier:
    def __init__(self, config: Config, store: Store):
        self.config = config
        self.store = store

    # ------------------------------------------------------------- настройки

    @property
    def settings(self) -> dict:
        return self.config.telegram or {}

    @property
    def enabled(self) -> bool:
        return bool(self.settings.get("enabled"))

    @property
    def token(self) -> str:
        return str(self.settings.get("bot_token", "")).strip()

    @property
    def chat_ids(self) -> list[str]:
        raw = self.settings.get("chat_ids") or []
        if isinstance(raw, (str, int)):
            raw = [raw]
        return [str(x).strip() for x in raw if str(x).strip()]

    @property
    def link(self) -> str:
        return str(self.settings.get("link", "")).strip()

    def check(self) -> dict:
        problems = []
        if not self.token:
            problems.append("не задан telegram.bot_token")
        if not self.chat_ids:
            problems.append("не задан telegram.chat_ids")
        if not self.link:
            problems.append("не задан telegram.link")
        return {
            "enabled": self.enabled,
            "ready": self.enabled and not problems,
            "problems": problems,
            "chats": len(self.chat_ids),
            "link": self.link,
        }

    # ---------------------------------------------------------------- запрос

    def _call(self, method: str, payload: dict) -> dict:
        req = urllib.request.Request(
            API.format(token=self.token, method=method),
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            try:
                data = json.loads(exc.read().decode())
            except Exception:
                raise TelegramError(f"Телеграм ответил HTTP {exc.code}") from exc
            raise TelegramError(_explain(data)) from exc
        except urllib.error.URLError as exc:
            raise TelegramError(f"Нет связи с Телеграм: {exc.reason}") from exc

    def send(self, text: str) -> dict:
        """Шлёт текст во все указанные чаты. Ошибка одного не рвёт остальные."""
        if not self.token:
            raise TelegramError("Не задан telegram.bot_token")
        if not self.chat_ids:
            raise TelegramError("Не задан telegram.chat_ids")

        sent, failed = [], []
        for chat_id in self.chat_ids:
            try:
                self._call("sendMessage", {
                    "chat_id": chat_id,
                    "text": text,
                    "disable_web_page_preview": False,
                })
                sent.append(chat_id)
            except TelegramError as exc:
                log.warning("чат %s: %s", chat_id, exc)
                failed.append({"chat_id": chat_id, "error": str(exc)})
        if not sent and failed:
            raise TelegramError(failed[0]["error"])
        return {"sent": sent, "failed": failed}

    def bot_name(self) -> str:
        return str(self._call("getMe", {}).get("result", {}).get("username") or "")

    def discover_chats(self) -> list[dict]:
        """Ищет чаты, где боту уже писали — чтобы не искать chat_id вручную."""
        data = self._call("getUpdates", {"limit": 100})
        seen: dict[str, dict] = {}
        for upd in data.get("result", []):
            msg = upd.get("message") or upd.get("channel_post") or {}
            chat = msg.get("chat") or {}
            if not chat.get("id"):
                continue
            title = chat.get("title") or " ".join(
                x for x in (chat.get("first_name"), chat.get("last_name")) if x
            ) or chat.get("username") or ""
            seen[str(chat["id"])] = {
                "chat_id": str(chat["id"]),
                "type": chat.get("type"),
                "title": title,
            }
        return list(seen.values())

    # ---------------------------------------------------- накопленная сводка

    def pending(self) -> Pending:
        return Pending.from_json(self.store.get_meta(PENDING_KEY))

    def remember(
        self,
        new_rows: list[dict],
        changed_rows: list[dict] | None = None,
        removed_rows: list[dict] | None = None,
    ) -> Pending:
        """Adds changes to the summary that waits to be sent (merged per file)."""
        pending = self.pending()
        for rows, status in (
            (new_rows or [], "new"),
            (changed_rows or [], "changed"),
            (removed_rows or [], "removed"),
        ):
            for row in rows:
                merge_item(pending.items, _row_brief(row, status))
        if pending.since is None:
            pending.since = datetime.now().astimezone().isoformat(timespec="seconds")
        self.store.set_meta(PENDING_KEY, pending.to_json())
        return pending

    def forget(self, rows: list[dict]) -> None:
        """Вычёркивает документы из накопленной сводки.

        Нужна, когда документ удалили из каталога: сообщать согласующим о
        файле, которого они уже не увидят, незачем.
        """
        pending = self.pending()
        if pending.empty:
            return
        keys = set()
        for row in rows:
            brief = _row_brief(row, "new")
            keys.add((brief["project"], brief["stage"], brief["file"]))
        kept = [
            item for item in pending.items
            if (item["project"], item["stage"], item["file"]) not in keys
        ]
        if len(kept) == len(pending.items):
            return
        pending.items = kept
        self.store.set_meta(PENDING_KEY, pending.to_json())

    def clear(self) -> None:
        self.store.set_meta(PENDING_KEY, Pending().to_json())

    def send_pending(self) -> dict | None:
        """Отправляет накопленное. None — если отправлять нечего или выключено."""
        if not self.enabled:
            return None
        pending = self.pending()
        if pending.empty:
            return None
        if not self.check()["ready"]:
            log.warning("уведомление не отправлено: %s", "; ".join(self.check()["problems"]))
            return None

        text = build_message(pending, self.link)
        result = self.send(text)
        self.clear()
        counts = pending.counts()
        result["text"] = text
        result["total"] = len(pending.items)
        result.update(counts)
        return result


def _explain(data: dict) -> str:
    desc = str(data.get("description") or "")
    low = desc.lower()
    if "chat not found" in low:
        return "Чат не найден: проверьте telegram.chat_ids, бот должен состоять в чате"
    if "bot was blocked" in low:
        return "Бот заблокирован в этом чате"
    if "unauthorized" in low:
        return "Неверный telegram.bot_token"
    if "bot is not a member" in low or "not enough rights" in low:
        return "Бот не состоит в этом чате или ему не хватает прав"
    return desc or "Телеграм отклонил запрос"
