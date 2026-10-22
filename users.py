"""Пользователи, добавленные админом командой /adduser или кнопкой «✅ Пустить», и заявки на доступ.

Хранятся в DATA_DIR/users.json (атомарная запись: tmp + os.replace).
Итоговый список доступа = ALLOWED_USER_IDS ∪ ADMIN_USER_IDS ∪ пользователи из этого файла.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass

log = logging.getLogger("grok-bot.users")

FILE_NAME = "users.json"
PENDING_TTL_SEC = 24 * 3600   # заявку без ответа можно повторить через сутки
MAX_PENDING = 20              # защита админов от наплыва заявок (например, от ботов-спамеров)
MAX_USER_ID = 10 ** 15


def parse_user_id(raw: str | None) -> int | None:
    """Telegram ID пользователя: положительное целое (группы/каналы с минусом не принимаем)."""
    s = (raw or "").strip()
    if not s.isdigit():
        return None
    v = int(s)
    return v if 0 < v < MAX_USER_ID else None


def fmt_ts(ts: float | None) -> str:
    if not ts:
        return "—"
    return time.strftime("%d.%m.%Y %H:%M UTC", time.gmtime(ts))


def display_name(rec: dict) -> str:
    name = (rec.get("name") or "").strip()
    un = (rec.get("username") or "").strip()
    parts = [p for p in (name, f"@{un}" if un else "") if p]
    return " ".join(parts) or "без имени"


@dataclass
class RequestResult:
    status: str        # created | allowed | pending | cooldown | limit
    wait_sec: float = 0.0


class UserStore:
    def __init__(self, data_dir: str, cooldown_sec: float = 3600.0, clock=time.time):
        self.data_dir = data_dir
        self.path = os.path.join(data_dir, FILE_NAME)
        self.cooldown_sec = cooldown_sec
        self.clock = clock
        self.persist_ok = True
        self.users: dict[int, dict] = {}
        self.requests: dict[int, dict] = {}
        self._load()

    # --- хранение ---
    def _load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as f:
                d = json.load(f)
            self.users = {int(k): dict(v) for k, v in (d.get("users") or {}).items()}
            self.requests = {int(k): dict(v) for k, v in (d.get("requests") or {}).items()}
            log.info("Загружено пользователей из %s: %d", self.path, len(self.users))
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError, AttributeError) as e:
            log.warning("Не удалось прочитать %s (%s) — список добавленных пользователей пуст", self.path, e)

    def save(self) -> bool:
        data = {"users": {str(k): v for k, v in self.users.items()},
                "requests": {str(k): v for k, v in self.requests.items()}}
        tmp = self.path + ".tmp"
        try:
            os.makedirs(self.data_dir, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
            self.persist_ok = True
        except OSError as e:
            if self.persist_ok:
                log.warning("Не могу сохранить %s (%s) — пользователи только в памяти", self.path, e)
            self.persist_ok = False
            try:
                os.remove(tmp)
            except OSError:
                pass
        return self.persist_ok

    # --- пользователи ---
    def __contains__(self, uid: int) -> bool:
        return uid in self.users

    def add(self, uid: int, *, name: str = "", username: str = "", note: str = "", added_by: int | None = None) -> bool:
        """True — новый пользователь, False — уже был (данные обновлены)."""
        old = self.users.get(uid)
        rec = dict(old or {})
        rec.update({k: v for k, v in (("name", name), ("username", username), ("note", note)) if v})
        if not old:
            rec["added_at"] = self.clock()
            rec["added_by"] = added_by
        self.users[uid] = rec
        self.requests.pop(uid, None)
        self.save()
        return old is None

    def remove(self, uid: int) -> bool:
        if uid not in self.users:
            return False
        del self.users[uid]
        self.save()
        return True

    # --- заявки ---
    def request_state(self, uid: int) -> RequestResult | None:
        """Текущее состояние заявки: pending / cooldown или None (можно подавать)."""
        r = self.requests.get(uid)
        if not r:
            return None
        age = self.clock() - float(r.get("ts") or 0)
        if r.get("status") == "pending" and age < PENDING_TTL_SEC:
            return RequestResult("pending", PENDING_TTL_SEC - age)
        if r.get("status") == "denied" and age < self.cooldown_sec:
            return RequestResult("cooldown", self.cooldown_sec - age)
        return None

    def pending(self) -> dict[int, dict]:
        now = self.clock()
        return {u: r for u, r in self.requests.items()
                if r.get("status") == "pending" and now - float(r.get("ts") or 0) < PENDING_TTL_SEC}

    def create_request(self, uid: int, name: str = "", username: str = "") -> RequestResult:
        if uid in self.users:
            return RequestResult("allowed")
        st = self.request_state(uid)
        if st:
            return st
        if len(self.pending()) >= MAX_PENDING:
            return RequestResult("limit")
        self.requests[uid] = {"status": "pending", "ts": self.clock(), "name": name, "username": username, "msgs": []}
        self.save()
        return RequestResult("created")

    def set_request_msgs(self, uid: int, msgs: list[tuple[int, int]]) -> None:
        r = self.requests.get(uid)
        if r is not None:
            r["msgs"] = [list(x) for x in msgs]
            self.save()

    def cancel_request(self, uid: int) -> None:
        if self.requests.pop(uid, None) is not None:
            self.save()

    def get_pending(self, uid: int) -> dict | None:
        st = self.request_state(uid)
        return self.requests.get(uid) if st and st.status == "pending" else None

    def deny(self, uid: int, by: int | None = None) -> dict | None:
        r = self.get_pending(uid)
        if not r:
            return None
        r.update(status="denied", ts=self.clock(), by=by)
        self.save()
        return r

    def approve(self, uid: int, by: int | None = None) -> dict | None:
        r = self.get_pending(uid)
        if not r:
            return None
        self.add(uid, name=r.get("name", ""), username=r.get("username", ""), note="по заявке", added_by=by)
        return r
