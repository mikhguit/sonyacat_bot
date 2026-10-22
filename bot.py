"""Telegram-бот — прокси к вашему аккаунту xAI (Grok).

aiogram 3 (long polling) + httpx -> xAI REST API (https://api.x.ai/v1).

Чат:      POST /v1/responses (основной текстовый API xAI; Chat Completions у xAI — legacy).
Картинки: POST /v1/images/generations, response_format="b64_json".
Стоимость: usage.cost_in_usd_ticks в каждом ответе (1 USD = 10^10 тиков), docs.x.ai/developers/cost-tracking.
Баланс:   GET https://management-api.x.ai/v1/billing/teams/{team_id}/prepaid/balance (нужен management key)
          или локальный счётчик: START_BALANCE_USD / /setbalance минус потраченное.

Проверка конфигурации без сети:  python bot.py --check
"""
from __future__ import annotations

import asyncio
import html
import base64
import io
import json
import logging
import os
import re
import secrets
import shutil
import sys
import tempfile
import time
import zipfile
from collections import defaultdict, deque
from dataclasses import dataclass, field

import httpx
from aiogram import Bot, Dispatcher, F
from aiogram.enums import ChatAction, ChatType
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramUnauthorizedError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import LinkPreviewOptions
from aiogram.types import (BufferedInputFile, CallbackQuery, FSInputFile, InlineKeyboardButton,
                           InlineKeyboardMarkup, InputMediaPhoto, Message)
from aiogram.utils.chat_action import ChatActionSender
from aiogram.utils.token import TokenValidationError
from PIL import Image

import insta
import memes
import refs
import search

# ---------------------------------------------------------------------------
# Константы (значения по умолчанию — из docs.x.ai, 22.10.2026)
# ---------------------------------------------------------------------------
DEFAULT_BASE_URL = "https://api.x.ai/v1"
DEFAULT_MGMT_URL = "https://management-api.x.ai"
DEFAULT_CHAT_MODEL = "grok-4.7"                 # developers/models: "Chat: Grok 4.7"
DEFAULT_IMAGE_MODEL = "grok-imagine-image-2.0"  # developers/models: "Images: Grok Imagine Image 2.0"
# Цены grok-4.7 (< 200k токенов промпта), $ за 1M токенов.
DEFAULT_PRICE_INPUT = 2.00
DEFAULT_PRICE_CACHED = 0.50
DEFAULT_PRICE_OUTPUT = 6.00
# grok-imagine-image-2.0 (developers/pricing, таблица Imagine API): 1K·Low $0.04, 1.5K·Low $0.05, 2K·Low $0.06,
# Medium — на $0.02 дороже; каждая входная картинка (edits) — $0.01. Только для ОЦЕНКИ: реальная цена — из ответа.
DEFAULT_PRICE_IMAGE = 0.04          # 1K, quality low
PRICE_IMAGE_RES_ADD = {"1k": 0.0, "1.5k": 0.01, "2k": 0.02}
PRICE_IMAGE_MEDIUM_ADD = 0.02
DEFAULT_PRICE_IMAGE_INPUT = 0.01
DEFAULT_VIDEO_MODEL = "grok-imagine-video-1.5-lite"  # docs: рекомендован для простых видео
# $ за секунду по разрешению (developers/pricing): lite 480p $0.02, 720p $0.03, 1080p $0.14; 1.5 — $0.08/$0.14/$0.25
VIDEO_PRICES = {"grok-imagine-video-1.5-lite": {"480p": 0.02, "720p": 0.03, "1080p": 0.14},
                "grok-imagine-video-1.5": {"480p": 0.08, "720p": 0.14, "1080p": 0.25},
                "grok-imagine-video": {"480p": 0.05, "720p": 0.07}}
DEFAULT_PRICE_VIDEO_PER_SEC = 0.03
DEFAULT_EST_CHAT_USD = 0.03  # грубая оценка одного JSON-запроса к grok-4.7 (для предупреждения до старта)
TG_UPLOAD_LIMIT = 50 * 1024 * 1024  # лимит Bot API на отправку файла
LONG_CONTEXT_TOKENS = 200_000  # от этого порога xAI считает всё по двойной цене
TICKS_PER_USD = 10_000_000_000

DEFAULT_SYSTEM_PROMPT = (
    "Ты — Grok, полезный ассистент в Telegram. Отвечай на языке пользователя, "
    "по умолчанию по-русски. Пиши обычным текстом без Markdown-разметки."
)
TG_LIMIT = 4096
HISTORY_TURNS = 10


# ---------------------------------------------------------------------------
# Конфигурация
# ---------------------------------------------------------------------------
def release_memory() -> None:
    """Вернуть освобождённую память ОС после тяжёлого кадра (важно на тарифе 256 МБ).
    gc + malloc_trim (glibc); на других libc — просто gc."""
    import gc
    gc.collect()
    try:
        import ctypes
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except (OSError, AttributeError):
        pass


def load_dotenv(path: str = ".env") -> None:
    """Мини-загрузчик .env (без зависимостей). Не перезаписывает уже заданные переменные."""
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip().removeprefix("export ").strip(), v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            os.environ.setdefault(k, v)


class ConfigError(Exception):
    pass


def _env(name: str) -> str:
    return os.getenv(name, "").strip()


def _ids(name: str) -> frozenset[int]:
    ids: set[int] = set()
    for part in _env(name).replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.add(int(part))
        except ValueError:
            logging.getLogger("config").warning("%s: пропускаю некорректное значение %r", name, part)
    return frozenset(ids)


def _float(name: str, default: float | None) -> float | None:
    raw = _env(name).replace(",", ".").lstrip("$")
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        raise ConfigError(f"{name}: ожидается число, получено {raw!r}")


@dataclass(frozen=True)
class Config:
    telegram_token: str
    xai_api_key: str
    allowed_user_ids: frozenset[int]
    admin_user_ids: frozenset[int]
    chat_model: str = DEFAULT_CHAT_MODEL
    image_model: str = DEFAULT_IMAGE_MODEL
    xai_proxy: str | None = None
    system_prompt: str = DEFAULT_SYSTEM_PROMPT
    base_url: str = DEFAULT_BASE_URL
    # стоимость / баланс
    price_input: float = DEFAULT_PRICE_INPUT
    price_cached: float = DEFAULT_PRICE_CACHED
    price_output: float = DEFAULT_PRICE_OUTPUT
    price_image: float = DEFAULT_PRICE_IMAGE
    show_cost: bool = True
    mgmt_key: str | None = None
    team_id: str | None = None
    mgmt_url: str = DEFAULT_MGMT_URL
    start_balance: float | None = None
    data_dir: str = "data"
    warn_thresholds: tuple[float, ...] = (5.0, 1.0)
    balance_refresh_sec: float = 60.0
    # Instagram / видео
    brand_name: str = ""
    brand_style: str = ""
    brand_accent: str = "#FFD54A"
    insta_image_resolution: str = "2k"
    insta_reasoning_effort: str = "low"
    est_chat_usd: float = DEFAULT_EST_CHAT_USD
    video_model: str = DEFAULT_VIDEO_MODEL
    video_resolution: str = "720p"
    video_duration: int = 6
    price_video_per_sec: float | None = None   # None — по таблице VIDEO_PRICES
    video_timeout_sec: float = 600.0
    max_parallel_jobs: int = 1
    work_dir: str = ""
    video_poll_sec: float = 5.0
    # мемы
    meme_animals: str = ""
    meme_image_resolution: str = "1k"
    # референсы
    price_image_input: float = DEFAULT_PRICE_IMAGE_INPUT
    ref_max: int = 4
    ref_mode: str = "edit"          # режим по умолчанию, если есть референсы: edit | style | bg
    ref_edit_quality: str = "low"   # quality для /v1/images/edits: low | medium | auto
    ref_vision: bool = True         # описывать стиль референсов через Grok vision
    ref_ttl_sec: float = 3600.0
    album_wait_sec: float = 1.2      # сколько ждать остальные фото альбома
    # поиск в вебе и X (серверные инструменты xAI)
    search_mode: str = "auto"        # auto — Grok сам решает, искать ли; off — без поиска (кроме /search)
    search_max_sources: int = 5
    search_tools: str = "web,x"
    search_max_tool_calls: int | None = None

    def image_price(self, resolution: str | None, quality: str = "low") -> float:
        p = self.price_image + PRICE_IMAGE_RES_ADD.get((resolution or "1k").lower(), 0.0)
        return p + (PRICE_IMAGE_MEDIUM_ADD if quality in ("medium", "auto") else 0.0)

    def video_price(self) -> float:
        if self.price_video_per_sec is not None:
            return self.price_video_per_sec
        return VIDEO_PRICES.get(self.video_model, {}).get(self.video_resolution, DEFAULT_PRICE_VIDEO_PER_SEC)

    @classmethod
    def from_env(cls) -> "Config":
        token, key = _env("TELEGRAM_BOT_TOKEN"), _env("XAI_API_KEY")
        missing = [n for n, v in (("TELEGRAM_BOT_TOKEN", token), ("XAI_API_KEY", key)) if not v]
        if missing:
            raise ConfigError("Не заданы обязательные переменные окружения: " + ", ".join(missing))
        allowed = _ids("ALLOWED_USER_IDS")
        admins = _ids("ADMIN_USER_IDS") or allowed
        thresholds = []
        for p in (_env("BALANCE_WARN_THRESHOLDS") or "5,1").split(","):
            p = p.strip().lstrip("$")
            if p:
                try:
                    thresholds.append(float(p))
                except ValueError:
                    raise ConfigError(f"BALANCE_WARN_THRESHOLDS: некорректное число {p!r}")
        return cls(
            telegram_token=token,
            xai_api_key=key,
            allowed_user_ids=allowed | admins,  # админы всегда имеют доступ
            admin_user_ids=admins,
            chat_model=_env("XAI_CHAT_MODEL") or DEFAULT_CHAT_MODEL,
            image_model=_env("XAI_IMAGE_MODEL") or DEFAULT_IMAGE_MODEL,
            xai_proxy=_env("XAI_PROXY") or None,
            system_prompt=_env("SYSTEM_PROMPT") or DEFAULT_SYSTEM_PROMPT,
            base_url=(_env("XAI_BASE_URL") or DEFAULT_BASE_URL).rstrip("/"),
            price_input=_float("PRICE_INPUT_PER_M", DEFAULT_PRICE_INPUT),
            price_cached=_float("PRICE_CACHED_INPUT_PER_M", DEFAULT_PRICE_CACHED),
            price_output=_float("PRICE_OUTPUT_PER_M", DEFAULT_PRICE_OUTPUT),
            price_image=_float("PRICE_IMAGE", DEFAULT_PRICE_IMAGE),
            show_cost=_env("SHOW_COST").lower() not in ("0", "false", "no", "off", "нет"),
            mgmt_key=_env("XAI_MANAGEMENT_KEY") or None,
            team_id=_env("XAI_TEAM_ID") or None,
            mgmt_url=(_env("XAI_MANAGEMENT_BASE_URL") or DEFAULT_MGMT_URL).rstrip("/"),
            start_balance=_float("START_BALANCE_USD", None),
            data_dir=_env("DATA_DIR") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"),
            warn_thresholds=tuple(sorted(set(thresholds), reverse=True)),
            balance_refresh_sec=_float("BALANCE_REFRESH_SEC", 60.0),
            brand_name=_env("BRAND_NAME"),
            brand_style=_env("BRAND_STYLE"),
            brand_accent=_env("BRAND_ACCENT") or "#FFD54A",
            insta_image_resolution=_env("INSTA_IMAGE_RESOLUTION") or "2k",
            insta_reasoning_effort=_env("INSTA_REASONING_EFFORT") or "low",
            est_chat_usd=_float("EST_CHAT_USD", DEFAULT_EST_CHAT_USD),
            video_model=_env("XAI_VIDEO_MODEL") or DEFAULT_VIDEO_MODEL,
            video_resolution=_env("VIDEO_RESOLUTION") or "720p",
            video_duration=int(_float("VIDEO_DURATION", 6)),
            price_video_per_sec=_float("PRICE_VIDEO_PER_SEC", None),
            video_timeout_sec=_float("VIDEO_TIMEOUT_SEC", 600.0),
            max_parallel_jobs=max(1, int(_float("MAX_PARALLEL_JOBS", 1))),
            work_dir=_env("WORK_DIR"),
            meme_animals=_env("MEME_ANIMALS"),
            meme_image_resolution=_env("MEME_IMAGE_RESOLUTION") or "1k",
            price_image_input=_float("PRICE_IMAGE_INPUT", DEFAULT_PRICE_IMAGE_INPUT),
            ref_max=max(1, min(refs.API_MAX_REFS, int(_float("REF_MAX", 4)))),
            ref_mode=(_env("REF_MODE") or "edit").lower() if (_env("REF_MODE") or "edit").lower() in ("edit", "style", "bg") else "edit",
            ref_edit_quality=(_env("REF_EDIT_QUALITY") or "low").lower(),
            ref_vision=_env("REF_VISION").lower() not in ("0", "false", "no", "off", "нет"),
            search_mode="off" if (_env("SEARCH_MODE") or "auto").lower() in ("off", "0", "false", "no", "нет") else "auto",
            search_max_sources=max(0, min(10, int(_float("SEARCH_MAX_SOURCES", 5)))),
            search_tools=_env("SEARCH_TOOLS") or "web,x",
            search_max_tool_calls=(int(_float("SEARCH_MAX_TOOL_CALLS", 0)) or None),
        )


# ---------------------------------------------------------------------------
# Логирование без секретов
# ---------------------------------------------------------------------------
class RedactSecrets(logging.Filter):
    def __init__(self, secrets: list[str]):
        super().__init__()
        self.secrets = [s for s in secrets if s and len(s) >= 6]

    def filter(self, record: logging.LogRecord) -> bool:
        if not self.secrets:
            return True
        msg = record.getMessage()
        redacted = msg
        for s in self.secrets:
            redacted = redacted.replace(s, "***")
        if redacted != msg:
            record.msg, record.args = redacted, None
        return True


def setup_logging() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def install_redaction(cfg: Config) -> None:
    secrets = [cfg.telegram_token, cfg.xai_api_key, cfg.mgmt_key or ""]
    if cfg.xai_proxy:
        secrets.append(cfg.xai_proxy)
    f = RedactSecrets(secrets)
    for h in logging.getLogger().handlers:
        h.addFilter(f)


log = logging.getLogger("grok-bot")


# ---------------------------------------------------------------------------
# Стоимость
# ---------------------------------------------------------------------------
@dataclass
class Cost:
    usd: float
    exact: bool  # True — из usage.cost_in_usd_ticks (реально списано), False — оценка по прайсу


def cost_from_usage(usage: dict | None, cfg: Config, *, image_count: int = 0) -> Cost:
    """Берёт реальную стоимость из ответа xAI; если её нет — оценивает по прайсу из env."""
    usage = usage or {}
    ticks = usage.get("cost_in_usd_ticks")
    if isinstance(ticks, (int, float)) and not isinstance(ticks, bool):
        return Cost(ticks / TICKS_PER_USD, True)
    nano = usage.get("cost_in_nano_usd")
    if isinstance(nano, (int, float)) and not isinstance(nano, bool):
        return Cost(nano / 1e9, True)
    if image_count:
        return Cost(image_count * cfg.price_image, False)
    inp = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
    out = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
    det_in = usage.get("input_tokens_details") or usage.get("prompt_tokens_details") or {}
    det_out = usage.get("output_tokens_details") or usage.get("completion_tokens_details") or {}
    cached = int(det_in.get("cached_tokens") or 0)
    reasoning = int(det_out.get("reasoning_tokens") or 0)
    total = int(usage.get("total_tokens") or 0)
    # В Responses API output_tokens может не включать reasoning (пример в доках: 32+9+110=151).
    if total and inp + out + reasoning > total:
        reasoning = 0  # reasoning уже учтён в output_tokens
    mult = 2.0 if inp >= LONG_CONTEXT_TOKENS else 1.0
    usd = mult * (
        max(inp - cached, 0) * cfg.price_input
        + cached * cfg.price_cached
        + (out + reasoning) * cfg.price_output
    ) / 1_000_000
    return Cost(usd, False)


def fmt_usd(x: float, digits: int = 2) -> str:
    sign = "-" if x < 0 else ""
    return f"{sign}${abs(x):,.{digits}f}".replace(",", " ")


def fmt_cost(x: float) -> str:
    if 0 < x < 0.0001:
        return "<$0.0001"
    return f"${x:.4f}"


# ---------------------------------------------------------------------------
# Баланс
# ---------------------------------------------------------------------------
@dataclass
class BalanceInfo:
    usd: float | None
    source: str  # "api" | "api+local" | "local" | "unknown"
    detail: str = ""


@dataclass
class _State:
    base_balance: float | None = None   # баланс, от которого считаем (START_BALANCE_USD или /setbalance)
    spent_since_base: float = 0.0
    spent_total: float = 0.0
    warned: list[float] = field(default_factory=list)  # пороги, о которых уже предупредили
    base_set_at: str = ""


class BalanceTracker:
    """Реальный баланс через Management API (если настроен) + локальный учёт расходов.

    Между запросами к Management API (не чаще BALANCE_REFRESH_SEC) из последнего полученного
    значения вычитается реальная стоимость запросов, сделанных после него.
    """

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.lock = asyncio.Lock()
        self.path = os.path.join(cfg.data_dir, "balance.json")
        self.persist_ok = True
        self.state = self._load()
        self.api_balance: float | None = None
        self.api_fetched_at = 0.0
        self.spent_since_fetch = 0.0
        self.api_error: str | None = None
        self.team_id = cfg.team_id
        self.http: httpx.AsyncClient | None = None
        if cfg.mgmt_key:
            self.http = httpx.AsyncClient(
                base_url=cfg.mgmt_url,
                headers={"Authorization": f"Bearer {cfg.mgmt_key}"},
                timeout=httpx.Timeout(15.0),
                proxy=cfg.xai_proxy,
            )

    # --- хранение ---
    def _load(self) -> _State:
        st = _State(base_balance=self.cfg.start_balance)
        try:
            with open(self.path, encoding="utf-8") as f:
                d = json.load(f)
            st = _State(
                base_balance=d.get("base_balance"),
                spent_since_base=float(d.get("spent_since_base") or 0),
                spent_total=float(d.get("spent_total") or 0),
                warned=[float(x) for x in d.get("warned") or []],
                base_set_at=d.get("base_set_at") or "",
            )
            log.info("Загружен учёт баланса из %s", self.path)
        except FileNotFoundError:
            log.info("Файл учёта %s не найден — старт с START_BALANCE_USD=%s", self.path, self.cfg.start_balance)
        except (OSError, ValueError, TypeError) as e:
            log.warning("Не удалось прочитать %s (%s) — начинаю заново", self.path, e)
        return st

    def _save(self) -> None:
        try:
            os.makedirs(self.cfg.data_dir, exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.state.__dict__, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
            self.persist_ok = True
        except OSError as e:
            if self.persist_ok:
                log.warning("Не могу сохранить %s (%s) — учёт только в памяти", self.path, e)
            self.persist_ok = False

    async def close(self) -> None:
        if self.http:
            await self.http.aclose()

    @property
    def uses_api(self) -> bool:
        return self.http is not None

    # --- Management API ---
    async def _resolve_team(self) -> str:
        if self.team_id:
            return self.team_id
        r = await self.http.get("/auth/management-keys/validation")
        r.raise_for_status()
        d = r.json()
        tid = d.get("teamId") or (d.get("scopeId") if d.get("scope") == "SCOPE_TEAM" else None)
        if not tid:
            raise ValueError("не удалось определить team id по management key — задайте XAI_TEAM_ID")
        self.team_id = tid
        log.info("XAI_TEAM_ID определён по management key")
        return tid

    async def fetch_api_balance(self) -> float:
        """Остаток предоплаченных кредитов, USD. total.val в центах; покупки отрицательные, траты положительные."""
        tid = await self._resolve_team()
        r = await self.http.get(f"/v1/billing/teams/{tid}/prepaid/balance")
        r.raise_for_status()
        d = r.json()
        val = ((d.get("total") or {}).get("val"))
        if val is None:
            raise ValueError("в ответе нет total.val")
        return -float(val) / 100.0

    async def _refresh_if_stale(self, force: bool = False) -> None:
        if not self.uses_api:
            return
        if not force and self.api_balance is not None and \
                time.monotonic() - self.api_fetched_at < self.cfg.balance_refresh_sec:
            return
        try:
            bal = await self.fetch_api_balance()
        except (httpx.HTTPError, ValueError, KeyError) as e:
            status = getattr(getattr(e, "response", None), "status_code", None)
            self.api_error = f"HTTP {status}" if status else type(e).__name__
            log.warning("Management API: не удалось получить баланс: %s", self.api_error)
            return
        self.api_balance, self.api_fetched_at, self.spent_since_fetch = bal, time.monotonic(), 0.0
        self.api_error = None

    # --- публичное ---
    def _current(self) -> BalanceInfo:
        if self.api_balance is not None:
            src = "api+local" if self.spent_since_fetch else "api"
            return BalanceInfo(self.api_balance - self.spent_since_fetch, src)
        if self.state.base_balance is not None:
            return BalanceInfo(self.state.base_balance - self.state.spent_since_base, "local",
                               self.api_error or "")
        return BalanceInfo(None, "unknown", self.api_error or "")

    async def get(self, force_refresh: bool = False) -> tuple[BalanceInfo, list[float]]:
        """Текущий баланс + пороги, пересечённые с прошлой проверки (например, из-за трат вне бота)."""
        async with self.lock:
            await self._refresh_if_stale(force_refresh)
            info = self._current()
            crossed = self._check_thresholds(info.usd)
            if crossed:
                self._save()
            return info, crossed

    async def record_spend(self, usd: float) -> tuple[BalanceInfo, list[float]]:
        """Учитывает трату, обновляет баланс. Возвращает (баланс, пороги, которые только что пересекли)."""
        async with self.lock:
            self.state.spent_since_base += usd
            self.state.spent_total += usd
            self.spent_since_fetch += usd
            await self._refresh_if_stale()
            info = self._current()
            crossed = self._check_thresholds(info.usd)
            self._save()
            return info, crossed

    async def set_balance(self, usd: float) -> BalanceInfo:
        async with self.lock:
            self.state.base_balance = usd
            self.state.spent_since_base = 0.0
            self.state.base_set_at = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
            self._check_thresholds(usd)  # сбрасывает/ставит флаги предупреждений
            self._save()
            return self._current()

    def _check_thresholds(self, bal: float | None) -> list[float]:
        if bal is None:
            return []
        crossed = []
        warned = set(self.state.warned)
        for t in self.cfg.warn_thresholds:
            if bal < t and t not in warned:
                warned.add(t)
                crossed.append(t)
            elif bal >= t and t in warned:
                warned.discard(t)  # баланс пополнили — снова предупредим при следующем пересечении
        self.state.warned = sorted(warned, reverse=True)
        return crossed


def balance_suffix(info: BalanceInfo) -> str:
    if info.usd is None:
        return "Остаток: неизвестен (/balance)"
    approx = "" if info.source == "api" else "≈"
    return f"Остаток: {approx}{fmt_usd(info.usd)}"


def cost_line(cost: Cost, info: BalanceInfo) -> str:
    c = fmt_cost(cost.usd) if cost.exact else "≈" + fmt_cost(cost.usd)
    return f"💰 Стоимость: {c} · {balance_suffix(info)}"


# ---------------------------------------------------------------------------
# Клиент xAI
# ---------------------------------------------------------------------------
class XAIError(Exception):
    def __init__(self, kind: str, status: int | None = None, detail: str = ""):
        super().__init__(f"{kind} status={status} {detail[:300]}")
        self.kind = kind
        self.status = status
        self.detail = detail
        self.cost: Cost | None = None


REGION_MARKERS = (
    "region", "country", "territor", "not available in your", "unsupported location",
    "location is not supported", "sanction", "blocked",
)


class XAIClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.http = httpx.AsyncClient(
            base_url=cfg.base_url,
            headers={"Authorization": f"Bearer {cfg.xai_api_key}"},
            timeout=httpx.Timeout(connect=20.0, read=300.0, write=60.0, pool=20.0),
            proxy=cfg.xai_proxy,  # прокси ТОЛЬКО для xAI; Telegram ходит напрямую
            follow_redirects=True,
        )
        self.dl_transport: httpx.AsyncBaseTransport | None = None  # для тестов

    async def close(self) -> None:
        await self.http.aclose()

    @staticmethod
    def _raise_http(r: httpx.Response) -> None:
        body = r.text[:1000]
        low = body.lower()
        region_hint = any(m in low for m in REGION_MARKERS)
        if r.status_code == 401 or "api key" in low:
            kind = "auth"  # xAI на неверный ключ отвечает 400 "Incorrect API key provided"
        elif r.status_code in (403, 451) or (r.status_code == 400 and region_hint):
            kind = "region"
        elif r.status_code == 429:
            kind = "rate"
        elif r.status_code >= 500:
            kind = "server"
        elif "moderation" in low or "content policy" in low or "safety" in low:
            kind = "moderation"
        elif "credit" in low or "balance" in low or "spending limit" in low:
            kind = "credits"
        else:
            kind = "bad_request"
        raise XAIError(kind, r.status_code, body)

    async def _post(self, path: str, payload: dict) -> dict:
        try:
            r = await self.http.post(path, json=payload)
        except httpx.TimeoutException as e:
            raise XAIError("timeout", detail=type(e).__name__) from e
        except httpx.HTTPError as e:
            raise XAIError("network", detail=f"{type(e).__name__}: {e}") from e

        if r.status_code >= 400:
            self._raise_http(r)
        try:
            return r.json()
        except ValueError as e:
            raise XAIError("server", r.status_code, "invalid JSON") from e

    async def chat(self, messages: list[dict]) -> tuple[str, Cost]:
        data = await self._post(
            "/responses",
            {"model": self.cfg.chat_model, "input": messages, "store": False},
        )
        cost = cost_from_usage(data.get("usage"), self.cfg)
        text = data.get("output_text") or ""
        if not text:
            parts: list[str] = []
            for item in data.get("output") or []:
                if item.get("type") == "message":
                    for c in item.get("content") or []:
                        if c.get("type") == "output_text" and c.get("text"):
                            parts.append(c["text"])
            text = "".join(parts)
        if not text.strip():
            err = XAIError("empty", detail=str(data.get("status")))
            err.cost = cost  # запрос всё равно оплачен
            raise err
        return text, cost

    async def chat_search(self, messages: list[dict], mode: str | None) -> search.ChatResult:
        """Обычный чат с серверными инструментами поиска.
        mode: None — без поиска; "auto" — Grok сам решает; "force" — поиск обязателен (/search).
        Если xAI отклонит параметры (400), повторяем проще: без tool_choice/max_tool_calls, затем без поиска."""
        tools = search.tools_payload(self.cfg.search_tools) if mode else []
        msgs = messages
        if mode == "force":
            msgs = [*messages[:-1], {"role": "system", "content": search.FORCE_HINT}, messages[-1]]
        base = {"model": self.cfg.chat_model, "input": msgs, "store": False}
        variants: list[dict] = []
        if tools:
            full = {**base, "tools": tools}
            if mode == "force":
                full["tool_choice"] = "required"
            if self.cfg.search_max_tool_calls:
                full["max_tool_calls"] = self.cfg.search_max_tool_calls
            variants.append(full)
            if len(full) > len(base) + 1:
                variants.append({**base, "tools": tools})
        variants.append(base)
        data: dict = {}
        degraded = False
        for i, payload in enumerate(variants):
            try:
                data = await self._post("/responses", payload)
                break
            except XAIError as e:
                if e.kind == "bad_request" and i < len(variants) - 1:
                    log.warning("chat: xAI отклонил вариант запроса с поиском (%s) — пробую проще", e.status)
                    degraded = True
                    continue
                raise
        res = search.extract(data, self.cfg.search_max_sources)
        res.cost = cost_from_usage(data.get("usage"), self.cfg)
        res.search_unavailable = bool(tools) and "tools" not in payload
        if degraded:
            log.info("chat: ответ получен в упрощённом режиме (tools=%s)", "tools" in payload)
        if not res.text.strip():
            err = XAIError("empty", detail=str(data.get("status")))
            err.cost = res.cost
            raise err
        return res

    async def chat_json(self, messages: list[dict], schema: dict, name: str = "plan") -> tuple[dict, Cost]:
        """Responses API со structured outputs (text.format = json_schema). Если модель/API не примет
        схему или reasoning — повтор без них, JSON парсится из текста."""
        base = {"model": self.cfg.chat_model, "input": messages, "store": False}
        variants = [
            {**base, "text": {"format": {"type": "json_schema", "name": name, "schema": schema, "strict": True}},
             **({"reasoning": {"effort": self.cfg.insta_reasoning_effort}} if self.cfg.insta_reasoning_effort else {})},
            {**base, "text": {"format": {"type": "json_object"}}},
        ]
        total = Cost(0.0, True)
        last: Exception | None = None
        for payload in variants:
            try:
                data = await self._post("/responses", payload)
            except XAIError as e:
                if e.kind == "bad_request":
                    log.warning("chat_json: вариант запроса отклонён (%s), пробую проще", e.status)
                    last = e
                    continue
                raise
            c = cost_from_usage(data.get("usage"), self.cfg)
            total = Cost(total.usd + c.usd, total.exact and c.exact)
            text = data.get("output_text") or "".join(
                c2.get("text", "") for item in data.get("output") or [] if item.get("type") == "message"
                for c2 in item.get("content") or [] if c2.get("type") == "output_text")
            try:
                return insta.parse_json_loose(text), total
            except ValueError as e:
                last = XAIError("empty", detail=f"invalid JSON: {e}")
                last.cost = total
                continue
        if isinstance(last, XAIError):
            last.cost = total if total.usd else last.cost
            raise last
        raise XAIError("empty", detail="no JSON")

    async def image(self, prompt: str, aspect_ratio: str | None = None,
                    resolution: str | None = None) -> tuple[bytes, str, Cost]:
        payload = {"model": self.cfg.image_model, "prompt": prompt, "n": 1, "response_format": "b64_json"}
        if aspect_ratio:
            payload["aspect_ratio"] = aspect_ratio
        if resolution:
            payload["resolution"] = resolution
        data = await self._post("/images/generations", payload)
        return await self._image_result(data, Cost(self.cfg.image_price(resolution), False))

    async def image_edit(self, prompt: str, ref_paths: list[str], aspect_ratio: str | None = None,
                         resolution: str | None = None, quality: str | None = None) -> tuple[bytes, str, Cost]:
        """POST /v1/images/edits: 1 картинка — поле image, 2–5 — массив images (base64 data URI)."""
        srcs = [{"type": "image_url", "url": refs.data_uri(p)} for p in ref_paths[:refs.API_MAX_REFS]]
        payload = {"model": self.cfg.image_model, "prompt": prompt, "n": 1, "response_format": "b64_json"}
        if len(srcs) == 1:
            payload["image"] = srcs[0]
        else:
            payload["images"] = srcs
        if aspect_ratio:
            payload["aspect_ratio"] = aspect_ratio
        if resolution:
            payload["resolution"] = resolution
        if quality:
            payload["quality"] = quality
        del srcs
        data = await self._post("/images/edits", payload)
        del payload
        est = self.cfg.image_price(resolution, quality or "auto") + len(ref_paths[:refs.API_MAX_REFS]) * self.cfg.price_image_input
        return await self._image_result(data, Cost(est, False))

    async def _image_result(self, data: dict, fallback: Cost) -> tuple[bytes, str, Cost]:
        items = data.get("data") or []
        usage = data.get("usage") or {}
        cost = cost_from_usage(usage, self.cfg) if (usage.get("cost_in_usd_ticks") is not None
                                                  or usage.get("cost_in_nano_usd") is not None) else fallback
        if not items:
            raise XAIError("empty", detail="no images")
        item = items[0]
        mime = item.get("mime_type") or "image/jpeg"
        if item.get("b64_json"):
            return base64.b64decode(item["b64_json"]), mime, cost
        if item.get("url"):
            try:
                async with httpx.AsyncClient(proxy=self.cfg.xai_proxy, timeout=60.0,
                                             follow_redirects=True) as dl:
                    r = await dl.get(item["url"])
                    r.raise_for_status()
            except httpx.HTTPError as e:
                raise XAIError("network", detail=f"download: {type(e).__name__}") from e
            return r.content, r.headers.get("content-type", mime), cost
        raise XAIError("empty", detail="no b64_json/url")


    # --- видео (асинхронно: старт -> опрос) ---
    async def video_start(self, prompt: str, duration: int, aspect_ratio: str = "9:16",
                          resolution: str | None = None) -> str:
        payload = {"model": self.cfg.video_model, "prompt": prompt, "duration": duration,
                   "aspect_ratio": aspect_ratio}
        if resolution:
            payload["resolution"] = resolution
        data = await self._post("/videos/generations", payload)
        rid = data.get("request_id")
        if not rid:
            raise XAIError("empty", detail="no request_id")
        return rid

    async def video_get(self, request_id: str) -> dict:
        try:
            r = await self.http.get(f"/videos/{request_id}")
        except httpx.TimeoutException as e:
            raise XAIError("timeout", detail=type(e).__name__) from e
        except httpx.HTTPError as e:
            raise XAIError("network", detail=f"{type(e).__name__}: {e}") from e
        if r.status_code == 202:
            try:
                return {"status": "pending", **(r.json() or {})}
            except ValueError:
                return {"status": "pending"}
        if r.status_code >= 400:
            self._raise_http(r)
        return r.json()

    async def download(self, url: str, path: str, max_bytes: int = TG_UPLOAD_LIMIT) -> int:
        """Потоково качает файл на диск (не держит видео в памяти)."""
        size = 0
        try:
            async with httpx.AsyncClient(proxy=self.cfg.xai_proxy, timeout=httpx.Timeout(120.0),
                                         follow_redirects=True, transport=self.dl_transport) as dl:
                async with dl.stream("GET", url) as r:
                    r.raise_for_status()
                    with open(path, "wb") as f:
                        async for chunk in r.aiter_bytes(256 * 1024):
                            size += len(chunk)
                            if size > max_bytes:
                                raise XAIError("too_big", detail=str(size))
                            f.write(chunk)
        except httpx.HTTPError as e:
            raise XAIError("network", detail=f"download: {type(e).__name__}") from e
        return size


# ---------------------------------------------------------------------------
# Тексты
# ---------------------------------------------------------------------------
HELP = (
    "Привет! Я бот-посредник к Grok (xAI).\n\n"
    "Команды:\n"
    "/start — это сообщение\n"
    "/myid — узнать свой Telegram ID (нужен владельцу для доступа)\n"
    "/img <описание> — сгенерировать картинку\n"
    "/new — начать новый диалог (сбросить историю)\n"
    "/search <запрос> — ответ с обязательным поиском в интернете и X (обычные сообщения ищут сами, когда нужно)\n"
    "/balance — остаток на счёте xAI\n"
    "/carousel [N] <тема> — карусель для Instagram (N слайдов 3–10, по умолчанию 7)\n"
    "/story <тема> [N] — сторис 9:16 (N кадров 1–10, по умолчанию 3)\n"
    "/storyvideo [сек] <описание> — короткое вертикальное видео (1–15 сек)\n"
    "Добавьте слово zip к /carousel или /story — пришлю PNG без сжатия архивом.\n"
    "/meme [зверь] [тема] — мем с милым животным (без зверя — покажу кнопки); "
    "story — формат 9:16, число 1–5 — несколько мемов, /meme5 — сразу 5\n"
    "/memevideo [сек] <зверь> <ситуация> — короткое мем-видео 9:16\n"
    "📎 Референсы: пришлите фото (или альбом) с подписью /carousel …, /story …, /meme … или ответьте командой на фото; "
    "просто фото без подписи сохраняются как референсы (/ref — посмотреть, /ref clear — удалить). "
    "Флаги: bg — фото как фон без генерации, style — только стиль, noref — без референсов.\n\n"
    "Просто напишите сообщение — и Grok ответит. Я помню последние ~10 реплик диалога. "
    "Если нужен свежий факт, Grok сам поищет в интернете и X и покажет источники.\n"
    "После каждого ответа показываю его стоимость и остаток баланса."
)
REF_HELP = (
    "📎 Референсы — ваши фото, по которым бот делает карусели, сторис и мемы в том же стиле или с тем же "
    "товаром/персонажем/питомцем.\n\n"
    "Как добавить:\n"
    "• отправьте фото или альбом с подписью: /carousel 7 тема · /story тема 5 · /meme работа\n"
    "• или ответьте командой на сообщение с фото\n"
    "• или просто пришлите фото без подписи — они сохранятся (до {n} шт., {ttl} мин) и будут использоваться дальше\n\n"
    "Режимы (слово в команде):\n"
    "• по умолчанию — фото передаются в генерацию картинок (стиль, персонаж, товар)\n"
    "• style — только описание стиля с фото (дешевле)\n"
    "• bg — само фото становится фоном слайда/мема, без генерации (платится только текст)\n"
    "• noref — не использовать сохранённые референсы\n\n"
    "/ref clear — удалить сохранённые фото."
)
ADMIN_HELP = "\n\nДля админа: /setbalance <сумма в $> — указать текущий остаток вручную."
NO_ACCESS = (
    "⛔ У вас нет доступа к этому боту.\n"
    "Отправьте /myid, перешлите свой ID владельцу бота — он добавит вас в список разрешённых."
)


def error_text(e: XAIError) -> str:
    if e.kind == "region":
        return (
            "🌍 xAI отклонил запрос (HTTP {s}). Скорее всего, API xAI недоступен из страны, "
            "где стоит сервер бота.\n\nВладельцу: задайте переменную XAI_PROXY (HTTP/SOCKS-прокси "
            "за рубежом, например socks5://user:pass@host:1080) или разместите бота на зарубежном VPS. "
            "Если прокси уже задан — проверьте ключ и права команды в console.x.ai."
        ).format(s=e.status)
    if e.kind == "auth":
        return f"🔑 xAI не принял API-ключ (HTTP {e.status}). Владельцу: проверьте XAI_API_KEY в console.x.ai."
    if e.kind == "credits":
        return f"💳 xAI отклонил запрос (HTTP {e.status}): похоже, закончились кредиты или достигнут лимит расходов. Пополните баланс в console.x.ai."
    if e.kind == "rate":
        return "⏳ Слишком много запросов или закончился баланс/лимит xAI (HTTP 429). Попробуйте чуть позже."
    if e.kind == "moderation":
        return "🚫 Запрос отклонён модерацией xAI. Попробуйте переформулировать."
    if e.kind == "bad_request":
        return f"⚠️ xAI вернул ошибку запроса (HTTP {e.status}). Возможно, неверное имя модели в настройках или запрос отклонён."
    if e.kind == "server":
        return "🛠 На стороне xAI временная ошибка. Попробуйте ещё раз через минуту."
    if e.kind == "timeout":
        return "⌛ xAI слишком долго не отвечает. Попробуйте ещё раз."
    if e.kind == "too_big":
        return "📦 Файл получился больше 50 МБ — Telegram не даст его отправить. Попробуйте короче/ниже разрешение."
    if e.kind == "network":
        return (
            "📡 Не удалось подключиться к xAI. Возможно, сервер бота не может достучаться до api.x.ai "
            "или не работает прокси.\nВладельцу: проверьте XAI_PROXY (или задайте его, если сервер в РФ)."
        )
    return "🤷 Grok вернул пустой ответ. Попробуйте ещё раз или /new."


def split_text(text: str, limit: int = TG_LIMIT) -> list[str]:
    chunks: list[str] = []
    text = text.strip()
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = text.rfind(" ", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    if text:
        chunks.append(text)
    return chunks or ["…"]


def warning_text(threshold: float, info: BalanceInfo) -> str:
    icon = "🔴" if threshold <= 1 else "🟠"
    return (
        f"{icon} Внимание: баланс xAI опустился ниже {fmt_usd(threshold)}.\n"
        f"{balance_suffix(info)}\n"
        "Пополните кредиты: console.x.ai → Billing. После пополнения без Management API "
        "укажите новый остаток командой /setbalance <сумма>."
    )


def source_text(info: BalanceInfo, tracker: BalanceTracker) -> str:
    if info.source == "api":
        return "xAI Management API (реальные данные)"
    if info.source == "api+local":
        return "xAI Management API + реальная стоимость запросов после последнего обновления"
    if info.source == "local":
        s = "локальный счётчик: стартовый остаток минус реальная стоимость запросов"
        if tracker.uses_api:
            s += f" (Management API недоступен: {info.detail or 'ошибка'})"
        return s
    return "неизвестно"


# ---------------------------------------------------------------------------
# Бот
# ---------------------------------------------------------------------------
def build_dispatcher(cfg: Config, xai: XAIClient, tracker: BalanceTracker) -> Dispatcher:
    dp = Dispatcher()
    history: dict[int, deque] = defaultdict(lambda: deque(maxlen=HISTORY_TURNS * 2))
    locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

    def allowed(m: Message) -> bool:
        return bool(m.from_user) and m.from_user.id in cfg.allowed_user_ids

    def is_admin(m: Message) -> bool:
        return bool(m.from_user) and m.from_user.id in cfg.admin_user_ids

    async def notify_admins(bot: Bot, crossed: list[float], info: BalanceInfo) -> None:
        for t in crossed:
            log.warning("Баланс ниже %s: %s", t, info.usd)
            for uid in cfg.admin_user_ids:
                try:
                    await bot.send_message(uid, warning_text(t, info))
                except (TelegramForbiddenError, TelegramBadRequest) as e:
                    log.warning("Не удалось отправить предупреждение админу %s: %s", uid, e)

    async def account(bot: Bot, cost: Cost) -> str:
        info, crossed = await tracker.record_spend(cost.usd)
        if crossed:
            asyncio.create_task(notify_admins(bot, crossed, info))
        return cost_line(cost, info)

    # Фото обрабатываются раньше команд: подпись «/carousel …» у фото/альбома = команда с референсами.
    @dp.message(F.photo | F.document.mime_type.in_({"image/jpeg", "image/png", "image/webp"}))
    async def on_photo_first(m: Message, bot: Bot) -> None:
        await on_photo(m, bot)

    @dp.message(CommandStart())
    async def cmd_start(m: Message) -> None:
        text = HELP + (ADMIN_HELP if is_admin(m) else "")
        if not allowed(m):
            text += "\n\n" + NO_ACCESS
        await m.answer(text)

    @dp.message(Command("myid"))
    async def cmd_myid(m: Message) -> None:
        uid = m.from_user.id if m.from_user else None
        await m.answer(f"Ваш Telegram ID: {uid}\nID этого чата: {m.chat.id}")

    @dp.message(Command("new"))
    async def cmd_new(m: Message) -> None:
        if not allowed(m):
            await m.answer(NO_ACCESS)
            return
        history.pop(m.from_user.id, None)
        await m.answer("🧹 История очищена. Начинаем новый диалог!")

    @dp.message(Command("balance"))
    async def cmd_balance(m: Message, bot: Bot) -> None:
        if not allowed(m):
            await m.answer(NO_ACCESS)
            return
        info, crossed = await tracker.get(force_refresh=True)
        if crossed:
            asyncio.create_task(notify_admins(bot, crossed, info))
        st = tracker.state
        lines = [f"💳 {balance_suffix(info)}", f"Источник: {source_text(info, tracker)}"]
        if info.usd is None:
            lines.append("Остаток неизвестен. Админ может задать его: /setbalance 10, "
                         "или настроить XAI_MANAGEMENT_KEY (см. README).")
        lines.append(f"Потрачено через бота: {fmt_cost(st.spent_total)} всего"
                     + (f", {fmt_cost(st.spent_since_base)} с последней установки баланса" if st.base_balance is not None else ""))
        if cfg.warn_thresholds:
            lines.append("Предупреждения при остатке ниже: " + ", ".join(fmt_usd(t) for t in cfg.warn_thresholds))
        if not tracker.persist_ok:
            lines.append("⚠️ Учёт не сохраняется на диск (DATA_DIR недоступен) — сбросится при перезапуске.")
        await m.answer("\n".join(lines))

    @dp.message(Command("setbalance"))
    async def cmd_setbalance(m: Message, command: CommandObject) -> None:
        if not is_admin(m):
            await m.answer("⛔ Эта команда только для администратора.")
            return
        raw = (command.args or "").strip().replace(",", ".").lstrip("$")
        try:
            usd = float(raw)
            if usd < 0 or usd > 1_000_000:
                raise ValueError
        except ValueError:
            await m.answer("Использование: /setbalance 12.34 — текущий остаток на счёте xAI в долларах "
                           "(смотрите console.x.ai → Billing).")
            return
        info = await tracker.set_balance(usd)
        msg = f"✅ Остаток установлен: {fmt_usd(usd)}. Дальше бот будет вычитать стоимость каждого запроса."
        if tracker.uses_api:
            msg += ("\nℹ️ Настроен Management API — в первую очередь показывается реальный баланс из xAI, "
                    "это значение используется, если API недоступен.")
        if not tracker.persist_ok:
            msg += "\n⚠️ DATA_DIR недоступен для записи — значение сбросится при перезапуске."
        await m.answer(msg)

    @dp.message(Command("img"))
    async def cmd_img(m: Message, command: CommandObject, bot: Bot) -> None:
        if not allowed(m):
            await m.answer(NO_ACCESS)
            return
        await handle_img(m, bot, command.args or "")

    async def handle_img(m: Message, bot: Bot, raw: str, ref_paths: list[str] | None = None) -> None:
        raw, _mode, noref = refs.split_flags(raw)
        prompt = raw.strip()
        if not prompt:
            await m.answer("Напишите описание после команды, например:\n/img рыжий кот в скафандре на Луне\n\n"
                           "С фото: ответьте на фото командой /img сделай в стиле акварели — бот отредактирует его.")
            return
        if ref_paths is None:
            # для /img берём только явно приложенное фото или фото из ответа (не сохранённые /ref)
            ref_paths = []
            if photo_file(m.reply_to_message):
                try:
                    p = await download_ref(bot, m.reply_to_message, new_tmp_dir(), "ref_1.jpg")
                    ref_paths = [p] if p else []
                except Exception as e:  # noqa: BLE001
                    log.warning("не удалось скачать фото из ответа: %s", e)
        try:
            async with ChatActionSender(bot=bot, chat_id=m.chat.id, action=ChatAction.UPLOAD_PHOTO):
                if ref_paths:
                    img, mime, cost = await xai.image_edit(prompt, ref_paths, None if len(ref_paths) == 1 else "auto",
                                                           None, cfg.ref_edit_quality)
                else:
                    img, mime, cost = await xai.image(prompt)
                line = await account(bot, cost)
        except XAIError as e:
            log.error("image error user=%s: %s", m.from_user.id, e)
            await m.answer(error_text(e))
            return
        ext = {"image/png": "png", "image/webp": "webp"}.get(mime.split(";")[0], "jpg")
        tail = ("\n\n" + line) if cfg.show_cost else ""
        caption = (("✏️ " if ref_paths else "🎨 ") + prompt)[: 1024 - len(tail)] + tail
        try:
            await m.answer_photo(BufferedInputFile(img, filename=f"grok.{ext}"), caption=caption)
        except TelegramBadRequest as e:
            log.warning("send_photo failed (%s), sending as document", e)
            await m.answer_document(BufferedInputFile(img, filename=f"grok.{ext}"), caption=caption)


    # ------------------------------------------------------------------
    # Instagram: карусели, сторис, видео
    # ------------------------------------------------------------------
    work_root = cfg.work_dir or os.path.join(tempfile.gettempdir(), "grok-bot-jobs")
    jobs: dict[str, dict] = {}       # готовые наборы (для кнопки «zip»)
    pending: dict[str, dict] = {}    # ждут подтверждения (оценка > баланса)
    busy: set[int] = set()
    sem = asyncio.Semaphore(cfg.max_parallel_jobs)
    accent = insta.hex_color(cfg.brand_accent)
    JOB_TTL = 3600
    FATAL = {"region", "auth", "credits", "rate", "network", "timeout"}
    animals = memes.load_animals(cfg.meme_animals)
    meme_jobs: dict[str, dict] = {}  # готовые мемы (фон хранится для «Другая подпись»)
    meme_req: dict[str, dict] = {}   # ждут выбора зверя кнопкой
    REF_DIR = os.path.join(work_root, "refs")
    ref_tmp: dict[str, float] = {}   # временные папки: фото с подписью-командой / фото из ответа
    albums: dict[tuple, dict] = {}   # сборка альбомов (media_group_id)

    def user_ref_dir(uid: int) -> str:
        return os.path.join(REF_DIR, f"u{uid}")

    def user_refs(uid: int) -> list[str]:
        paths = refs.list_refs(user_ref_dir(uid))
        if paths and time.time() - os.path.getmtime(paths[-1]) > cfg.ref_ttl_sec:
            shutil.rmtree(user_ref_dir(uid), ignore_errors=True)
            return []
        return paths

    def new_tmp_dir() -> str:
        d = os.path.join(work_root, "t" + secrets.token_hex(5))
        os.makedirs(d, exist_ok=True)
        ref_tmp[d] = time.time()
        return d

    def photo_file(msg: Message | None) -> tuple[str, int] | None:
        """(file_id, размер) самой крупной версии фото до ~1600 px или картинки-документа."""
        if msg is None:
            return None
        if msg.photo:
            best = msg.photo[0]
            for ps in msg.photo:  # Telegram отдаёт размеры по возрастанию
                if max(ps.width, ps.height) <= 1600 or ps is msg.photo[0]:
                    best = ps
            return best.file_id, best.file_size or 0
        d = msg.document
        if d and (d.mime_type or "").lower() in ("image/jpeg", "image/png", "image/webp"):
            return d.file_id, d.file_size or 0
        return None

    async def download_ref(bot: Bot, msg: Message, dest: str, name: str) -> str | None:
        f = photo_file(msg)
        if not f:
            return None
        fid, size = f
        if size > 20 * 1024 * 1024:  # Bot API отдаёт файлы до 20 МБ
            raise ValueError("файл больше 20 МБ")
        buf = await bot.download(fid)
        path = os.path.join(dest, name)
        try:
            await asyncio.to_thread(refs.save_ref, buf.getvalue(), path)
        finally:
            buf.close()
            release_memory()
        return path

    async def resolve_refs(m: Message, bot: Bot, noref: bool) -> tuple[list[str], str]:
        """Референсы для команды: фото, на которое ответили командой, иначе сохранённые /ref."""
        if photo_file(m.reply_to_message):
            try:
                p = await download_ref(bot, m.reply_to_message, new_tmp_dir(), "ref_1.jpg")
                if p:
                    return [p], "фото из ответа"
            except Exception as e:  # noqa: BLE001 — не мешаем основной команде
                log.warning("не удалось скачать фото из ответа: %s", e)
        if not noref and m.from_user:
            ps = user_refs(m.from_user.id)
            if ps:
                return ps, "сохранённые через /ref"
        return [], ""

    def images_estimate(n: int, res: str, k: int, mode: str | None) -> float:
        if not k or not mode:
            return n * cfg.image_price(res)
        vis = 0.01 if cfg.ref_vision else 0.0
        if mode == refs.MODE_BG:
            return vis
        if mode == refs.MODE_STYLE:
            return vis + n * cfg.image_price(res)
        return vis + n * (cfg.image_price(res, cfg.ref_edit_quality) + min(k, refs.API_MAX_REFS) * cfg.price_image_input)

    def ref_line(k: int, mode: str | None, source: str = "") -> str:
        if not k or not mode:
            return ""
        return f"📎 Референсы: {k}{(' (' + source + ')') if source else ''} · режим: {refs.MODE_TITLES[mode]}"

    async def analyze_refs(paths: list[str], spend: "Spend") -> refs.RefStyle | None:
        if not paths or not cfg.ref_vision:
            return None
        try:
            data, cost = await xai.chat_json(refs.vision_messages(paths), refs.vision_schema(), name="refstyle")
            await spend.add(cost)
            return refs.normalize_style(data)
        except XAIError as e:
            await spend.add(e.cost)
            if e.kind in FATAL:
                raise
            log.warning("описание референсов не получено: %s", e)
            return None

    async def make_background(bot: Bot, chat_id: int, i: int, prompt: str, aspect: str, res: str,
                              rc: dict, spend: "Spend") -> Image.Image | None:
        """Фон кадра: фото пользователя как есть (bg), генерация по референсам (/images/edits),
        или обычная генерация (+ описание стиля). Фатальные ошибки xAI пробрасываются."""
        paths = rc.get("paths") or []
        mode = rc.get("mode") if paths else None
        if mode == refs.MODE_BG:
            with Image.open(paths[i % len(paths)]) as im:
                return im.convert("RGB")
        style = rc.get("style")
        if style and style.prompt_suffix():
            prompt = f"{prompt}. {style.prompt_suffix()}"
        use_edit = mode == refs.MODE_EDIT and not rc.get("edit_failed")
        for attempt in (1, 2):
            try:
                async with ChatActionSender.upload_photo(bot=bot, chat_id=chat_id):
                    if use_edit:
                        raw, _mime, cost = await xai.image_edit(refs.edit_prompt(prompt, len(paths)), paths, aspect,
                                                                res, cfg.ref_edit_quality)
                    else:
                        raw, _mime, cost = await xai.image(prompt, aspect, res)
                await spend.add(cost)
                bg = await load_image(raw)
                del raw
                if bg is not None:
                    return bg
            except XAIError as e:
                log.warning("image %d attempt %d (%s): %s", i + 1, attempt, "edit" if use_edit else "gen", e)
                await spend.add(e.cost)
                if e.kind in FATAL:
                    raise
                if use_edit and e.kind == "bad_request":
                    rc["edit_failed"] = True
                    use_edit = False
        return None

    def cleanup_jobs() -> None:
        now = time.time()
        for jid, j in list(jobs.items()):
            if now - j["created"] > JOB_TTL:
                shutil.rmtree(j["dir"], ignore_errors=True)
                jobs.pop(jid, None)
        for tok, p in list(pending.items()):
            if now - p["created"] > 900:
                pending.pop(tok, None)
        for jid, j in list(meme_jobs.items()):
            if now - j["created"] > JOB_TTL:
                shutil.rmtree(j["dir"], ignore_errors=True)
                meme_jobs.pop(jid, None)
        for tok, r in list(meme_req.items()):
            if now - r["created"] > 900:
                meme_req.pop(tok, None)
        for d, t in list(ref_tmp.items()):
            if now - t > cfg.ref_ttl_sec:
                shutil.rmtree(d, ignore_errors=True)
                ref_tmp.pop(d, None)
        if os.path.isdir(REF_DIR):
            for u in os.listdir(REF_DIR):
                user_refs(int(u[1:])) if u[1:].isdigit() else None  # удалит просроченные

    class Spend:
        """Суммирует реальную стоимость всех вызовов одной задачи."""
        def __init__(self, bot: Bot):
            self.bot, self.usd, self.exact, self.info = bot, 0.0, True, None
            self.parts: list[str] = []

        async def add(self, cost: Cost | None) -> None:
            if not cost:
                return
            self.usd += cost.usd
            self.exact &= cost.exact
            self.info, crossed = await tracker.record_spend(cost.usd)
            if crossed:
                asyncio.create_task(notify_admins(self.bot, crossed, self.info))

        def line(self, what: str = "") -> str:
            if self.info is None:
                self.info = tracker._current()
            c = fmt_cost(self.usd) if self.exact else "≈" + fmt_cost(self.usd)
            return f"💰 Итого: {c}{(' (' + what + ')') if what else ''} · {balance_suffix(self.info)}"

    async def status_edit(msg: Message, text: str) -> None:
        try:
            if msg.bot:
                await msg.bot.edit_message_text(text=text, chat_id=msg.chat.id, message_id=msg.message_id)
        except TelegramBadRequest:
            pass  # «message is not modified» и т.п.

    async def status_delete(msg: Message) -> None:
        try:
            if msg.bot:
                await msg.bot.delete_message(chat_id=msg.chat.id, message_id=msg.message_id)
        except TelegramBadRequest:
            pass

    def parse_args(raw: str, kind: str) -> tuple[int | None, str, bool, str | None]:
        """-> (N, тема, zip?, ошибка)."""
        words = (raw or "").split()
        as_zip = False
        rest = []
        for w in words:
            if w.lower().strip(",.") in ("zip", "png", "архив", "-zip", "--zip"):
                as_zip = True
            else:
                rest.append(w)
        n = None
        if kind == "storyvideo":
            lo, hi = 1, 15
        elif kind == "carousel":
            lo, hi = insta.CAROUSEL_MIN, insta.CAROUSEL_MAX
        else:
            lo, hi = insta.STORY_MIN, insta.STORY_MAX
        if rest and re.fullmatch(r"\d{1,2}", rest[0]):
            n = int(rest.pop(0))
        elif kind == "story" and rest and re.fullmatch(r"\d{1,2}", rest[-1]):
            n = int(rest.pop())
        if n is not None and not lo <= n <= hi:
            return None, "", as_zip, f"Число должно быть от {lo} до {hi}."
        return n, " ".join(rest).strip(), as_zip, None

    async def precheck_and_run(m: Message, bot: Bot, kind: str, params: dict, estimate: float) -> None:
        await precheck(bot, m.chat.id, m.from_user.id, kind, params, estimate)

    async def precheck(bot: Bot, chat_id: int, user_id: int, kind: str, params: dict, estimate: float) -> None:
        info, _ = await tracker.get()
        if info.usd is not None and estimate > info.usd:
            tok = secrets.token_hex(4)
            pending[tok] = {"user": user_id, "kind": kind, "params": params, "created": time.time(),
                            "chat": chat_id}
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="▶️ Всё равно запустить", callback_data=f"go:{tok}"),
                InlineKeyboardButton(text="✖️ Отмена", callback_data=f"no:{tok}"),
            ]])
            await bot.send_message(chat_id, f"⚠️ Оценка стоимости ≈{fmt_cost(estimate)} больше остатка "
                                   f"({balance_suffix(info)}). xAI может отклонить часть запросов, "
                                   "когда кредиты закончатся.", reply_markup=kb)
            return
        await start_job(bot, chat_id, user_id, kind, params)

    async def start_job(bot: Bot, chat_id: int, user_id: int, kind: str, params: dict) -> None:
        if user_id in busy:
            await bot.send_message(chat_id, "⏳ Предыдущая задача ещё выполняется — дождитесь её окончания.")
            return
        busy.add(user_id)
        try:
            if sem.locked():
                await bot.send_message(chat_id, "🕐 Сервер занят другой генерацией — ваша задача в очереди…")
            async with sem:
                cleanup_jobs()
                if kind == "storyvideo":
                    await run_video(bot, chat_id, **params)
                elif kind == "meme":
                    await run_meme(bot, chat_id, user_id, **params)
                elif kind == "meme_recap":
                    await run_recap(bot, chat_id, **params)
                elif kind == "memevideo":
                    await run_memevideo(bot, chat_id, **params)
                else:
                    await run_insta(bot, chat_id, kind, **params)
        except Exception:
            log.exception("job %s failed", kind)
            await bot.send_message(chat_id, "😵 Что-то пошло не так при генерации. Подробности в логах бота.")
        finally:
            busy.discard(user_id)
            release_memory()

    async def run_insta(bot: Bot, chat_id: int, kind: str, topic: str, n: int, as_zip: bool,
                        ref_paths: list[str] | tuple = (), ref_mode: str | None = None, ref_source: str = "") -> None:
        spend = Spend(bot)
        word = "слайд" if kind == "carousel" else "кадр"
        rc = {"paths": [p for p in ref_paths if os.path.exists(p)], "mode": ref_mode}
        if not rc["paths"]:
            rc["mode"] = None
        est = cfg.est_chat_usd + images_estimate(n, cfg.insta_image_resolution, len(rc["paths"]), rc["mode"])
        rl = ref_line(len(rc["paths"]), rc["mode"], ref_source)
        status = await bot.send_message(chat_id, (rl + "\n" if rl else "") + f"✍️ Пишу тексты и стиль… (оценка ≈{fmt_cost(est)})")
        # 0. референсы → описание стиля (vision)
        note = ""
        if rc["mode"]:
            try:
                async with ChatActionSender.typing(bot=bot, chat_id=chat_id):
                    rc["style"] = await analyze_refs(rc["paths"], spend)
            except XAIError as e:
                await status_edit(status, error_text(e) + ("\n\n" + spend.line() if spend.usd else ""))
                return
            if rc.get("style"):
                note = rc["style"].plan_note()
            if rc["mode"] == refs.MODE_BG:
                note += "\n\nФоном слайдов будут фото пользователя без изменений — тексты должны им соответствовать."
        # 1. план
        try:
            async with ChatActionSender.typing(bot=bot, chat_id=chat_id):
                data, cost = await xai.chat_json(
                    insta.plan_messages(kind, topic, n, cfg.brand_name, cfg.brand_style, note), insta.plan_schema())
            await spend.add(cost)
        except XAIError as e:
            log.error("%s plan error: %s", kind, e)
            await spend.add(e.cost)
            await status_edit(status, error_text(e) + ("\n\n" + spend.line() if spend.usd else ""))
            return
        plan = insta.normalize_plan(data, kind, n, topic)
        # 2. картинки — строго по одной (RAM)
        jid = secrets.token_hex(5)
        jdir = os.path.join(work_root, jid)
        os.makedirs(jdir, exist_ok=True)
        aspect = "1:1" if kind == "carousel" else "9:16"
        pngs, jpgs, fallbacks = [], [], []
        try:
            for i, slide in enumerate(plan.slides):
                await status_edit(status, f"{'🖼 Собираю' if rc['mode'] == refs.MODE_BG else '🎨 Генерирую'} {word} {i + 1}/{n}…")
                prompt = insta.image_prompt_for(plan, slide, cfg.brand_style)
                try:
                    bg = await make_background(bot, chat_id, i, prompt, aspect, cfg.insta_image_resolution, rc, spend)
                except XAIError as e:
                    await status_edit(status, error_text(e) + f"\n\nГотово {i} из {n}. " + spend.line())
                    shutil.rmtree(jdir, ignore_errors=True)
                    return
                if bg is None:
                    fallbacks.append(i + 1)
                out = insta.render_slide(bg, slide, i, n, kind, cfg.brand_name, accent)
                if bg is not None:
                    bg.close()
                png = os.path.join(jdir, f"{kind}_{i + 1:02d}.png")
                jpg = os.path.join(jdir, f"{kind}_{i + 1:02d}.jpg")
                out.save(png)
                out.save(jpg, quality=92)
                out.close()
                del out, bg
                release_memory()
                pngs.append(png)
                jpgs.append(jpg)
            # 3. отправка
            await status_edit(status, "📤 Отправляю…")
            if len(jpgs) == 1:
                await bot.send_photo(chat_id, FSInputFile(jpgs[0]))
            else:
                await bot.send_media_group(chat_id, [InputMediaPhoto(media=FSInputFile(p)) for p in jpgs])
            cap = insta.caption_text(plan)
            if cap:
                for chunk in split_text("📝 Подпись к посту:\n\n" + cap):
                    await bot.send_message(chat_id, chunk)
            what = "текст, фото как фон" if rc["mode"] == refs.MODE_BG else f"текст + {n} изобр."
            if rc["mode"] in (refs.MODE_EDIT, refs.MODE_STYLE):
                what += f", референсов: {len(rc['paths'])}"
            note = ""
            if rc.get("edit_failed"):
                note += "\nℹ️ xAI не принял генерацию по референсам — использовано описание их стиля."
            if fallbacks:
                note += f"\n⚠️ Для {word}ов {', '.join(map(str, fallbacks))} картинку получить не удалось — использован градиентный фон."
            jobs[jid] = {"dir": jdir, "files": pngs, "created": time.time(), "kind": kind}
            if as_zip:
                await send_zip(bot, chat_id, jid)
                await bot.send_message(chat_id, spend.line(what) + note)
            else:
                kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
                    text="📦 PNG без сжатия (zip)", callback_data=f"zip:{jid}")]])
                await bot.send_message(chat_id, spend.line(what) + note, reply_markup=kb)
            await status_delete(status)
        except BaseException:
            shutil.rmtree(jdir, ignore_errors=True)
            jobs.pop(jid, None)
            raise
        finally:
            for p in jpgs:  # JPEG нужны только для альбома
                try:
                    os.remove(p)
                except OSError:
                    pass

    async def send_zip(bot: Bot, chat_id: int, jid: str) -> bool:
        j = jobs.get(jid)
        if not j or not all(os.path.exists(p) for p in j["files"]):
            return False
        zpath = os.path.join(j["dir"], f"{j['kind']}_{jid}.zip")
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_STORED) as z:  # PNG уже сжаты
            for p in j["files"]:
                z.write(p, os.path.basename(p))
        try:
            await bot.send_document(chat_id, FSInputFile(zpath), caption="PNG в исходном качестве")
        finally:
            os.remove(zpath)
        return True

    async def run_video(bot: Bot, chat_id: int, prompt: str, seconds: int, title: str = "",
                        spend: "Spend | None" = None, status: Message | None = None) -> None:
        spend = spend or Spend(bot)
        est = seconds * cfg.video_price()
        text = f"🎬 Запускаю генерацию видео {seconds} сек, 9:16 (оценка ≈{fmt_cost(est)})…"
        if status is None:
            status = await bot.send_message(chat_id, text)
        else:
            await status_edit(status, text)
        full_prompt = f"{prompt}. Vertical 9:16 video for Instagram Stories."
        if cfg.brand_style:
            full_prompt += f" Style: {cfg.brand_style}."
        try:
            rid = await xai.video_start(full_prompt, seconds, "9:16", cfg.video_resolution)
        except XAIError as e:
            log.error("video start: %s", e)
            await status_edit(status, error_text(e) + ("\n\n" + spend.line() if spend.usd else ""))
            return
        t0, last_edit, last_prog = time.monotonic(), 0.0, None
        data: dict = {}
        while True:
            await asyncio.sleep(cfg.video_poll_sec)
            try:
                data = await xai.video_get(rid)
            except XAIError as e:
                if e.kind in ("network", "timeout", "server") and time.monotonic() - t0 < cfg.video_timeout_sec:
                    log.warning("video poll: %s — повторю", e)
                    continue
                await status_edit(status, error_text(e))
                return
            st = data.get("status")
            if st == "done":
                break
            if st in ("failed", "expired"):
                err = (data.get("error") or {}).get("message") or st
                if (data.get("usage") or {}).get("cost_in_usd_ticks"):
                    await spend.add(cost_from_usage(data.get("usage"), cfg))
                await status_edit(status, f"❌ Видео не получилось: {err[:300]}" +
                                  ("\n\n" + spend.line() if spend.usd else ""))
                return
            if time.monotonic() - t0 > cfg.video_timeout_sec:
                await status_edit(status, "⌛ Видео генерируется слишком долго — остановил ожидание. "
                                          "Стоимость могла списаться; проверьте /balance позже.")
                return
            prog = data.get("progress")
            if prog != last_prog and time.monotonic() - last_edit > 10:
                last_prog, last_edit = prog, time.monotonic()
                await status_edit(status, f"🎬 Генерирую видео… {prog if prog is not None else '…'}%")
        usage = data.get("usage")
        vid = data.get("video") or {}
        dur = int(vid.get("duration") or seconds)
        cost = cost_from_usage(usage, cfg) if (usage or {}).get("cost_in_usd_ticks") is not None \
            else Cost(dur * cfg.video_price(), False)
        await spend.add(cost)
        if vid.get("respect_moderation") is False or not vid.get("url"):
            await status_edit(status, "🚫 Видео не прошло модерацию xAI (ссылка не выдана).\n\n" + spend.line("видео"))
            return
        jdir = os.path.join(work_root, "v" + secrets.token_hex(5))
        os.makedirs(jdir, exist_ok=True)
        path = os.path.join(jdir, "story.mp4")
        try:
            await status_edit(status, "📥 Скачиваю видео…")
            await xai.download(vid["url"], path)
            await bot.send_video(chat_id, FSInputFile(path), supports_streaming=True,
                                 caption=((title or f"🎬 {prompt}")[:800] + "\n\n" + spend.line(f"видео {dur} сек"))[:1024])
            await status_delete(status)
        except XAIError as e:
            await status_edit(status, error_text(e) + "\n\n" + spend.line("видео"))
        finally:
            shutil.rmtree(jdir, ignore_errors=True)

    @dp.message(Command("carousel", "story"))
    async def cmd_insta(m: Message, command: CommandObject, bot: Bot) -> None:
        if not allowed(m):
            await m.answer(NO_ACCESS)
            return
        await handle_insta(m, bot, command.command.lower(), command.args or "")

    async def handle_insta(m: Message, bot: Bot, kind: str, raw: str, ref_paths: list[str] | None = None,
                           ref_source: str = "") -> None:
        raw, mode, noref = refs.split_flags(raw)
        n, topic, as_zip, err = parse_args(raw, kind)
        if err or not topic:
            ex = ("/carousel 7 как выбрать кофемолку\n/carousel 5 zip 5 ошибок новичка в беге"
                  if kind == "carousel" else "/story утренняя рутина за 10 минут 3\n/story анонс вебинара 1 zip")
            await m.answer((err + "\n\n" if err else "") + "Напишите тему, например:\n" + ex)
            return
        if n is None:
            n = insta.CAROUSEL_DEFAULT if kind == "carousel" else insta.STORY_DEFAULT
        if ref_paths is None:
            ref_paths, ref_source = await resolve_refs(m, bot, noref)
        if mode and not ref_paths:
            await m.answer("📎 Флаг bg/style работает с фото: пришлите фото с подписью-командой, ответьте командой на фото "
                           "или сохраните референсы (просто отправьте фото боту). /ref — подробнее.")
            return
        mode = (mode or cfg.ref_mode) if ref_paths else None
        est = cfg.est_chat_usd + images_estimate(n, cfg.insta_image_resolution, len(ref_paths), mode)
        await precheck_and_run(m, bot, kind, {"topic": topic, "n": n, "as_zip": as_zip, "ref_paths": ref_paths,
                                              "ref_mode": mode, "ref_source": ref_source}, est)

    @dp.message(Command("storyvideo"))
    async def cmd_storyvideo(m: Message, command: CommandObject, bot: Bot) -> None:
        if not allowed(m):
            await m.answer(NO_ACCESS)
            return
        n, prompt, _z, err = parse_args(command.args or "", "storyvideo")
        if err or not prompt:
            await m.answer((err + "\n\n" if err else "") + "Опишите видео, например:\n"
                           "/storyvideo 6 чашка кофе на подоконнике, пар, утренний свет, камера медленно приближается")
            return
        seconds = n or max(1, min(15, cfg.video_duration))
        await precheck_and_run(m, bot, "storyvideo", {"prompt": prompt, "seconds": seconds},
                               seconds * cfg.video_price())

    @dp.callback_query(F.data.regexp(r"^(go|no|zip):[0-9a-f]+$"))
    async def on_callback(cq: CallbackQuery, bot: Bot) -> None:
        if not cq.from_user or cq.from_user.id not in cfg.allowed_user_ids:
            await cq.answer("Нет доступа", show_alert=True)
            return
        action, tok = cq.data.split(":", 1)
        cleanup_jobs()
        if action == "zip":
            await cq.answer("Собираю архив…")
            if not await send_zip(bot, cq.message.chat.id, tok):
                await bot.send_message(cq.message.chat.id, "Файлы уже удалены (хранятся 1 час или до перезапуска). "
                                                           "Сгенерируйте заново с флагом zip.")
            return
        p = pending.get(tok)
        if not p:
            await cq.answer("Запрос устарел", show_alert=True)
            return
        if p["user"] != cq.from_user.id:
            await cq.answer("Это не ваш запрос", show_alert=True)
            return
        pending.pop(tok, None)
        try:
            await cq.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass
        if action == "no":
            await cq.answer("Отменено")
            return
        await cq.answer("Запускаю")
        await start_job(bot, p["chat"], p["user"], p["kind"], p["params"])

    # ------------------------------------------------------------------
    # Мемы с милыми животными
    # ------------------------------------------------------------------
    def animal_by_key(key: str) -> memes.Animal:
        if key == "photo":
            return memes.photo_animal()
        for a in animals:
            if a.key == key:
                return a
        return memes.random.choice(animals)

    def animal_kb(tok: str) -> InlineKeyboardMarkup:
        btns = [InlineKeyboardButton(text=a.name, callback_data=f"ma:{tok}:{i}") for i, a in enumerate(animals)]
        rows = [btns[i:i + 2] for i in range(0, len(btns), 2)]
        rows.append([InlineKeyboardButton(text="🎲 Случайный", callback_data=f"ma:{tok}:r")])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    def meme_kb(jid: str) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔄 Ещё", callback_data=f"mm:more:{jid}"),
            InlineKeyboardButton(text="✏️ Другая подпись", callback_data=f"mm:cap:{jid}"),
            InlineKeyboardButton(text="📦 PNG", callback_data=f"mm:png:{jid}"),
        ]])

    def meme_estimate(n: int, k: int = 0, mode: str | None = None) -> float:
        return cfg.est_chat_usd + images_estimate(n, cfg.meme_image_resolution, k, mode)

    async def load_image(raw: bytes) -> Image.Image | None:
        try:
            im = Image.open(io.BytesIO(raw))
            im.load()
            return im.convert("RGB") if im.mode != "RGB" else im
        except (OSError, ValueError) as e:
            log.warning("meme: не удалось открыть картинку: %s", e)
            return None

    async def send_meme(bot: Bot, chat_id: int, jid: str, caption: str) -> None:
        j = meme_jobs[jid]
        try:
            await bot.send_photo(chat_id, FSInputFile(j["png"]), caption=caption[:1024], reply_markup=meme_kb(jid))
        except TelegramBadRequest as e:
            log.warning("meme send_photo failed (%s), sending as document", e)
            await bot.send_document(chat_id, FSInputFile(j["png"]), caption=caption[:1024], reply_markup=meme_kb(jid))

    async def run_meme(bot: Bot, chat_id: int, user_id: int, animal: str, topic: str, n: int, story: bool,
                       ref_paths: list[str] | tuple = (), ref_mode: str | None = None, ref_source: str = "") -> None:
        a = animal_by_key(animal)
        spend = Spend(bot)
        rc = {"paths": [p for p in ref_paths if os.path.exists(p)], "mode": ref_mode}
        if not rc["paths"]:
            rc["mode"] = None
        rl = ref_line(len(rc["paths"]), rc["mode"], ref_source)
        status = await bot.send_message(
            chat_id, (rl + "\n" if rl else "") + f"✍️ Придумываю {'мемы' if n > 1 else 'мем'}: {a.name}"
                     f"{(' — ' + topic) if topic else ''} (оценка ≈{fmt_cost(meme_estimate(n, len(rc['paths']), rc['mode']))})")
        photo_note = ""
        if rc["mode"]:
            try:
                async with ChatActionSender.typing(bot=bot, chat_id=chat_id):
                    rc["style"] = await analyze_refs(rc["paths"], spend)
            except XAIError as e:
                await status_edit(status, error_text(e) + ("\n\n" + spend.line() if spend.usd else ""))
                return
            st = rc.get("style")
            if st:
                if a.key == "photo" and st.subject:
                    a = memes.photo_animal(st.subject)
                photo_note = f"Фото пользователя: {st.summary_ru or st.subject}."
            if rc["mode"] == refs.MODE_BG:
                photo_note += (" Картинкой будет само фото пользователя без изменений — подпись должна подходить "
                               "именно к нему; в scene просто опиши это фото.")
        try:
            async with ChatActionSender.typing(bot=bot, chat_id=chat_id):
                data, cost = await xai.chat_json(memes.meme_messages(a, topic, n, story, photo_note=photo_note),
                                                 memes.meme_schema(), name="memes")
            await spend.add(cost)
        except XAIError as e:
            log.error("meme text error: %s", e)
            await spend.add(e.cost)
            await status_edit(status, error_text(e) + ("\n\n" + spend.line() if spend.usd else ""))
            return
        items = memes.normalize_memes(data, n, a)
        if not items:
            await status_edit(status, "🤷 Grok не придумал подпись. Попробуйте ещё раз или уточните тему.\n\n"
                              + spend.line("текст"))
            return
        aspect = "9:16" if story else "1:1"
        done = 0
        for i, it in enumerate(items):
            if len(items) > 1:
                await status_edit(status, f"🎨 Рисую мем {i + 1}/{len(items)}…")
            else:
                await status_edit(status, "🎨 Рисую картинку…" if rc["mode"] != refs.MODE_BG else "🖼 Накладываю подпись…")
            prompt = memes.image_prompt(a, it.scene, story, cfg.brand_style)
            try:
                bg = await make_background(bot, chat_id, i, prompt, aspect, cfg.meme_image_resolution, rc, spend)
            except XAIError as e:
                await status_edit(status, error_text(e) + f"\n\nГотово {done} из {len(items)}. " + spend.line())
                return
            jid = secrets.token_hex(5)
            jdir = os.path.join(work_root, "m" + jid)
            os.makedirs(jdir, exist_ok=True)
            bg_path = os.path.join(jdir, "bg.jpg")
            seed = secrets.randbelow(1000)
            if bg is not None:
                bg.save(bg_path, quality=93)
            out = memes.render_meme(bg, it.top, it.bottom, story=story, watermark=cfg.brand_name, seed=seed)
            if bg is not None:
                bg.close()
            png = os.path.join(jdir, "meme_1.png")
            out.save(png)
            out.close()
            del out, bg
            release_memory()
            meme_jobs[jid] = {"dir": jdir, "png": png, "bg": bg_path if os.path.exists(bg_path) else None,
                              "seed": seed, "animal": a.key, "topic": topic, "story": story, "scene": it.scene,
                              "history": [it.label], "created": time.time(), "user": user_id, "ver": 1,
                              "ref_paths": rc["paths"], "ref_mode": rc["mode"], "ref_source": ref_source}
            done += 1
            note = "" if os.path.exists(bg_path) else "\n⚠️ Картинку получить не удалось — фон-заглушка."
            if rc.get("edit_failed") and i == 0:
                note += "\nℹ️ xAI не принял генерацию по референсу — использовано описание фото."
            if len(items) == 1:
                cap = f"{a.name}{(' · ' + topic) if topic else ''}{note}"
                if cfg.show_cost:
                    cap += "\n\n" + spend.line("текст, ваше фото" if rc["mode"] == refs.MODE_BG else
                                                ("текст + изобр. по референсу" if rc["mode"] else "текст + изобр."))
            else:
                cap = f"{a.name} · {i + 1}/{len(items)}{note}"
            await send_meme(bot, chat_id, jid, cap)
        if len(items) > 1:
            await bot.send_message(chat_id, spend.line("текст, ваше фото" if rc["mode"] == refs.MODE_BG else f"текст + {done} изобр.")
                                   + (f"\nGrok вернул {len(items)} из {n}." if len(items) < n else ""))
        await status_delete(status)

    async def run_recap(bot: Bot, chat_id: int, jid: str) -> None:
        j = meme_jobs.get(jid)
        if not j or not os.path.isdir(j["dir"]):
            await bot.send_message(chat_id, "Картинка уже удалена (хранится 1 час или до перезапуска). "
                                            "Сделайте новый мем: /meme")
            return
        a = animal_by_key(j["animal"])
        spend = Spend(bot)
        try:
            async with ChatActionSender.typing(bot=bot, chat_id=chat_id):
                data, cost = await xai.chat_json(memes.recaption_messages(a, j["topic"], j["scene"], j["history"]),
                                                 memes.meme_schema(), name="memes")
            await spend.add(cost)
        except XAIError as e:
            log.error("meme recaption error: %s", e)
            await spend.add(e.cost)
            await bot.send_message(chat_id, error_text(e) + ("\n\n" + spend.line() if spend.usd else ""))
            return
        items = memes.normalize_memes(data, 1, a)
        if not items:
            await bot.send_message(chat_id, "🤷 Не получилось придумать новую подпись. Попробуйте ещё раз.\n\n"
                                   + spend.line("текст"))
            return
        it = items[0]
        bg = None
        if j["bg"] and os.path.exists(j["bg"]):
            bg = Image.open(j["bg"])
            bg.load()
        out = memes.render_meme(bg, it.top, it.bottom, story=j["story"], watermark=cfg.brand_name, seed=j["seed"])
        if bg is not None:
            bg.close()
        j["ver"] += 1
        old = j["png"]
        j["png"] = os.path.join(j["dir"], f"meme_{j['ver']}.png")
        out.save(j["png"])
        out.close()
        try:
            os.remove(old)
        except OSError:
            pass
        j["history"].append(it.label)
        cap = f"✏️ Новая подпись · {a.name}"
        if cfg.show_cost:
            cap += "\n\n" + spend.line("только текст")
        await send_meme(bot, chat_id, jid, cap)

    async def run_memevideo(bot: Bot, chat_id: int, animal: str, situation: str, seconds: int) -> None:
        a = animal_by_key(animal)
        spend = Spend(bot)
        status = await bot.send_message(chat_id, f"✍️ Придумываю сценку: {a.name}…")
        scene, title = "", ""
        try:
            async with ChatActionSender.typing(bot=bot, chat_id=chat_id):
                data, cost = await xai.chat_json(memes.meme_messages(a, situation, 1, True, video=True),
                                                 memes.meme_schema(), name="memes")
            await spend.add(cost)
            items = memes.normalize_memes(data, 1, a)
            if items:
                scene, title = items[0].scene, "😂 " + items[0].label
        except XAIError as e:
            if e.kind in FATAL:
                await spend.add(e.cost)
                await status_edit(status, error_text(e) + ("\n\n" + spend.line() if spend.usd else ""))
                return
            log.warning("memevideo: сценарий не получен (%s), использую описание как есть", e)
            await spend.add(e.cost)
        prompt = memes.video_prompt(a, scene or situation or "doing something funny and relatable")
        await run_video(bot, chat_id, prompt, seconds, title=title or f"{a.name} {situation}".strip(),
                        spend=spend, status=status)

    @dp.message(Command("meme", "meme5"))
    async def cmd_meme(m: Message, command: CommandObject, bot: Bot) -> None:
        if not allowed(m):
            await m.answer(NO_ACCESS)
            return
        await handle_meme(m, bot, command.command.lower(), command.args or "")

    async def handle_meme(m: Message, bot: Bot, cmd: str, raw: str, ref_paths: list[str] | None = None,
                          ref_source: str = "") -> None:
        raw, mode, noref = refs.split_flags(raw)
        args = memes.parse_meme_args(raw, animals, 5 if cmd == "meme5" else 1)
        if args.error:
            await m.answer(args.error + "\n\nПример: /meme 3 капибара понедельник")
            return
        if ref_paths is None:
            ref_paths, ref_source = await resolve_refs(m, bot, noref)
        if mode and not ref_paths:
            await m.answer("📎 Флаг bg/style работает с фото: пришлите фото с подписью /meme bg …, ответьте командой "
                           "на фото или сохраните референсы. /ref — подробнее.")
            return
        mode = (mode or cfg.ref_mode) if ref_paths else None
        rp = {"ref_paths": ref_paths, "ref_mode": mode, "ref_source": ref_source}
        if args.animal is None and ref_paths:
            await precheck_and_run(m, bot, "meme", {"animal": "photo", "topic": args.topic, "n": args.n,
                                                    "story": args.story, **rp},
                                   meme_estimate(args.n, len(ref_paths), mode))
            return
        if args.animal is None:
            cleanup_jobs()
            tok = secrets.token_hex(4)
            meme_req[tok] = {"user": m.from_user.id, "chat": m.chat.id, "topic": args.topic, "n": args.n,
                             "story": args.story, "created": time.time()}
            extra = []
            if args.topic:
                extra.append(f"тема: {args.topic}")
            if args.story:
                extra.append("формат 9:16")
            if args.n > 1:
                extra.append(f"{args.n} шт.")
            await m.answer("Выберите зверя" + (f" ({', '.join(extra)})" if extra else "") + ":",
                           reply_markup=animal_kb(tok))
            return
        await precheck_and_run(m, bot, "meme", {"animal": args.animal.key, "topic": args.topic, "n": args.n,
                                                "story": args.story, **rp},
                               meme_estimate(args.n, len(ref_paths), mode))

    @dp.message(Command("memevideo"))
    async def cmd_memevideo(m: Message, command: CommandObject, bot: Bot) -> None:
        if not allowed(m):
            await m.answer(NO_ACCESS)
            return
        words = (command.args or "").split()
        seconds = max(1, min(15, cfg.video_duration))
        if words and re.fullmatch(r"\d{1,2}", words[0]):
            seconds = int(words.pop(0))
            if not 1 <= seconds <= 15:
                await m.answer("Длительность — от 1 до 15 секунд.")
                return
        a = memes.find_animal(animals, words[0]) if words else None
        if a:
            words.pop(0)
        situation = " ".join(words).strip()
        if not a and not situation:
            await m.answer("Например:\n/memevideo капибара сидит на созвоне, который мог быть письмом\n"
                           "/memevideo 8 енот опаздывает на работу\n\nЗвери: "
                           + ", ".join(x.name for x in animals))
            return
        if not a:
            a = memes.random.choice(animals)
            await m.answer(f"Зверь не указан — беру случайного: {a.name}")
        await precheck_and_run(m, bot, "memevideo", {"animal": a.key, "situation": situation, "seconds": seconds},
                               cfg.est_chat_usd + seconds * cfg.video_price())

    @dp.callback_query(F.data.regexp(r"^ma:[0-9a-f]+:(\d{1,2}|r)$"))
    async def on_animal(cq: CallbackQuery, bot: Bot) -> None:
        if not cq.from_user or cq.from_user.id not in cfg.allowed_user_ids:
            await cq.answer("Нет доступа", show_alert=True)
            return
        _, tok, choice = cq.data.split(":")
        r = meme_req.get(tok)
        if not r:
            await cq.answer("Запрос устарел — отправьте /meme ещё раз", show_alert=True)
            return
        if r["user"] != cq.from_user.id:
            await cq.answer("Это не ваш запрос", show_alert=True)
            return
        meme_req.pop(tok, None)
        if choice == "r" or int(choice) >= len(animals):
            a = memes.random.choice(animals)
        else:
            a = animals[int(choice)]
        await cq.answer(a.name)
        try:
            await cq.message.edit_text(f"Зверь: {a.name}" + (f" · тема: {r['topic']}" if r["topic"] else ""))
        except TelegramBadRequest:
            pass
        await precheck(bot, r["chat"], r["user"], "meme", {"animal": a.key, "topic": r["topic"], "n": r["n"],
                                                           "story": r["story"]}, meme_estimate(r["n"]))

    @dp.callback_query(F.data.regexp(r"^mm:(more|cap|png):[0-9a-f]+$"))
    async def on_meme_button(cq: CallbackQuery, bot: Bot) -> None:
        if not cq.from_user or cq.from_user.id not in cfg.allowed_user_ids:
            await cq.answer("Нет доступа", show_alert=True)
            return
        _, action, jid = cq.data.split(":")
        cleanup_jobs()
        j = meme_jobs.get(jid)
        chat_id = cq.message.chat.id
        uid = cq.from_user.id
        if action == "png":
            if not j or not os.path.exists(j["png"]):
                await cq.answer("Файл уже удалён (хранится 1 час)", show_alert=True)
                return
            await cq.answer("Отправляю PNG…")
            await bot.send_document(chat_id, FSInputFile(j["png"], filename=f"meme_{jid}.png"),
                                    caption="PNG без сжатия")
            return
        if not j:
            await cq.answer("Мем устарел (хранится 1 час) — отправьте /meme", show_alert=True)
            return
        if uid in busy:
            await cq.answer("⏳ Дождитесь окончания предыдущей задачи", show_alert=True)
            return
        if action == "more":
            await cq.answer("Делаю ещё один…")
            rp = [p for p in j.get("ref_paths") or [] if os.path.exists(p)]
            mode = j.get("ref_mode") if rp else None
            await precheck(bot, chat_id, uid, "meme", {"animal": j["animal"], "topic": j["topic"], "n": 1,
                                                       "story": j["story"], "ref_paths": rp, "ref_mode": mode,
                                                       "ref_source": j.get("ref_source", "")},
                           meme_estimate(1, len(rp), mode))
        else:
            await cq.answer("Придумываю другую подпись…")
            await precheck(bot, chat_id, uid, "meme_recap", {"jid": jid}, cfg.est_chat_usd)

    # ------------------------------------------------------------------
    # Референсы: фото, альбомы, /ref
    # ------------------------------------------------------------------
    PHOTO_COMMANDS = {"carousel", "story", "meme", "meme5", "img"}

    async def on_photo(m: Message, bot: Bot) -> None:
        if not allowed(m):
            await m.answer(NO_ACCESS)
            return
        if m.media_group_id:
            key = (m.chat.id, m.media_group_id)
            buf = albums.setdefault(key, {"msgs": [], "task": None})
            buf["msgs"].append(m)
            if buf["task"]:
                buf["task"].cancel()
            buf["task"] = asyncio.create_task(flush_album(key, bot))
            return
        await process_photos([m], bot)

    async def flush_album(key: tuple, bot: Bot) -> None:
        try:
            await asyncio.sleep(cfg.album_wait_sec)
        except asyncio.CancelledError:
            return
        buf = albums.pop(key, None)
        if buf:
            try:
                await process_photos(buf["msgs"], bot)
            except Exception:
                log.exception("album processing failed")

    async def process_photos(msgs: list[Message], bot: Bot) -> None:
        m = msgs[0]
        caption = next((x.caption for x in msgs if x.caption), "") or ""
        cm = re.match(r"^/([A-Za-z0-9_]+)(?:@\w+)?(?:\s+(.*))?$", caption.strip(), re.S)
        cmd = cm.group(1).lower() if cm else ""
        args = (cm.group(2) or "") if cm else ""
        if cm and cmd in PHOTO_COMMANDS:
            limit = refs.API_MAX_REFS if cmd != "img" else refs.API_MAX_REFS
            take = msgs[:min(limit, cfg.ref_max)]
            d = new_tmp_dir()
            paths = []
            for i, x in enumerate(take):
                try:
                    p = await download_ref(bot, x, d, f"ref_{i + 1}.jpg")
                    if p:
                        paths.append(p)
                except Exception as e:  # noqa: BLE001
                    log.warning("не удалось скачать фото: %s", e)
            if not paths:
                await m.answer("😕 Не удалось получить фото. Пришлите JPG/PNG до 20 МБ.")
                return
            if len(msgs) > len(take):
                await m.answer(f"ℹ️ Беру первые {len(take)} фото (лимит REF_MAX={cfg.ref_max}).")
            src = "прикреплённые фото"
            if cmd in ("carousel", "story"):
                await handle_insta(m, bot, cmd, args, paths, src)
            elif cmd in ("meme", "meme5"):
                await handle_meme(m, bot, cmd, args, paths, src)
            else:
                await handle_img(m, bot, args, paths)
            return
        if cm:
            await m.answer(f"Команда /{cmd} не работает с фото. С фото: /carousel, /story, /meme, /img.")
            return
        # Без команды — сохраняем как референсы пользователя.
        uid = m.from_user.id
        d = user_ref_dir(uid)
        os.makedirs(d, exist_ok=True)
        added = 0
        for x in msgs:
            try:
                if await download_ref(bot, x, d, f"ref_{time.time_ns()}.jpg"):
                    added += 1
            except Exception as e:  # noqa: BLE001
                log.warning("не удалось сохранить референс: %s", e)
        paths = refs.list_refs(d)
        dropped = 0
        while len(paths) > cfg.ref_max:
            os.remove(paths.pop(0))
            dropped += 1
        if not added:
            await m.answer("😕 Не удалось сохранить фото. Пришлите JPG/PNG до 20 МБ.")
            return
        await m.answer(
            f"📌 Референсы сохранены: {len(paths)} из {cfg.ref_max}"
            + (f" (старые {dropped} удалены)" if dropped else "") + ".\n"
            f"Теперь /carousel, /story и /meme будут их учитывать (режим по умолчанию: {refs.MODE_TITLES[cfg.ref_mode]}).\n"
            "Флаги в команде: bg — фото как фон, style — только стиль, noref — без референсов.\n"
            f"/ref — посмотреть, /ref clear — удалить. Хранятся {int(cfg.ref_ttl_sec // 60)} мин.")

    @dp.message(Command("ref"))
    async def cmd_ref(m: Message, command: CommandObject, bot: Bot) -> None:
        if not allowed(m):
            await m.answer(NO_ACCESS)
            return
        arg = (command.args or "").strip().lower()
        uid = m.from_user.id
        if arg in ("clear", "очистить", "удалить", "reset", "off"):
            shutil.rmtree(user_ref_dir(uid), ignore_errors=True)
            await m.answer("🧹 Референсы удалены.")
            return
        paths = user_refs(uid)
        if not paths:
            await m.answer(REF_HELP.format(n=cfg.ref_max, ttl=int(cfg.ref_ttl_sec // 60)))
            return
        sheet = await asyncio.to_thread(refs.contact_sheet, paths)
        await m.answer_photo(BufferedInputFile(sheet, filename="refs.jpg"),
                             caption=f"📎 Ваши референсы: {len(paths)} из {cfg.ref_max}. Режим по умолчанию: "
                                     f"{refs.MODE_TITLES[cfg.ref_mode]}.\n/ref clear — удалить.")

    @dp.message(F.text & ~F.text.startswith("/"), F.chat.type == ChatType.PRIVATE)
    async def on_text(m: Message, bot: Bot) -> None:
        if not allowed(m):
            await m.answer(NO_ACCESS)
            return
        await answer_chat(m, bot, m.text, "auto" if cfg.search_mode == "auto" else None)

    @dp.message(Command("search"))
    async def cmd_search(m: Message, command: CommandObject, bot: Bot) -> None:
        if not allowed(m):
            await m.answer(NO_ACCESS)
            return
        q = (command.args or "").strip()
        if not q:
            await m.answer("Напишите запрос, например:\n/search что нового у xAI на этой неделе\n\n"
                           "Обычные сообщения тоже ищут в интернете и X, когда Grok считает это нужным; "
                           "/search — поиск обязательно.")
            return
        await answer_chat(m, bot, q, "force")

    async def answer_chat(m: Message, bot: Bot, text: str, mode: str | None) -> None:
        uid = m.from_user.id
        async with locks[uid]:
            hist = history[uid]
            messages = [{"role": "system", "content": cfg.system_prompt}, *hist, {"role": "user", "content": text}]
            try:
                async with ChatActionSender.typing(bot=bot, chat_id=m.chat.id):
                    res = await xai.chat_search(messages, mode)
                    line = await account(bot, res.cost)
            except XAIError as e:
                log.error("chat error user=%s: %s", uid, e)
                err = error_text(e)
                if e.cost and e.cost.usd > 0:
                    err += "\n\n" + await account(bot, e.cost)
                await m.answer(err)
                return
            reply = search.clean_text(res.text)
            hist.append({"role": "user", "content": text})
            hist.append({"role": "assistant", "content": reply})
        await send_with_sources(m, reply, res, line)

    async def send_with_sources(m: Message, reply: str, res: search.ChatResult, line: str) -> None:
        """Ответ режется по 4096 (видимых символов); источники и стоимость — в конце, ссылки — HTML."""
        tail_vis, tail_html = [], []
        src_vis, src_html = search.sources_text(res.sources), search.sources_html(res.sources)
        if src_vis:
            tail_vis.append(src_vis)
            tail_html.append(src_html)
        if cfg.show_cost:
            note = res.usage_note()
            if mode_note := ("ℹ️ Поиск недоступен для этой модели/ключа — ответ без поиска."
                             if res.search_unavailable else ""):
                tail_vis.append(mode_note)
                tail_html.append(html.escape(mode_note))
            cl = line + (" (вкл. поиск)" if res.searched else "")
            if note:
                cl = note + "\n" + cl
            tail_vis.append(cl)
            tail_html.append(html.escape(cl))
        tv, th = "\n".join(tail_vis), "\n".join(tail_html)
        chunks = split_text(reply)
        out = [html.escape(c) for c in chunks]
        if tv:
            if len(chunks[-1]) + 2 + len(tv) <= TG_LIMIT:
                out[-1] += "\n\n" + th
            else:
                out.append(th)
        for chunk in out:
            await m.answer(chunk, parse_mode="HTML", link_preview_options=LinkPreviewOptions(is_disabled=True))

    @dp.message(F.chat.type == ChatType.PRIVATE)
    async def on_other(m: Message) -> None:
        if not allowed(m):
            await m.answer(NO_ACCESS)
            return
        await m.answer("Я понимаю текст и фото (фото = референсы, см. /ref). Команды: /start, /img, /meme, /memevideo, "
                       "/carousel, /story, /storyvideo, /ref, /search, /new, /balance, /myid")

    @dp.errors()
    async def on_error(event) -> bool:
        log.exception("Unhandled error: %s", event.exception)
        return True

    return dp


def describe(cfg: Config) -> str:
    bal_src = "Management API" if cfg.mgmt_key else (
        f"локальный счётчик (START_BALANCE_USD={cfg.start_balance})" if cfg.start_balance is not None
        else "локальный счётчик (стартовый остаток не задан — /setbalance)")
    return (f"chat={cfg.chat_model} image={cfg.image_model} base_url={cfg.base_url} "
            f"proxy={'да' if cfg.xai_proxy else 'нет'} allowed={len(cfg.allowed_user_ids)} "
            f"admins={len(cfg.admin_user_ids)} balance={bal_src} data_dir={cfg.data_dir} "
            f"warn={list(cfg.warn_thresholds)} video={cfg.video_model} {cfg.video_resolution} "
            f"brand={cfg.brand_name or '-'} meme_animals={len(memes.load_animals(cfg.meme_animals))} "
            f"search={cfg.search_mode}({cfg.search_tools}, src≤{cfg.search_max_sources}) "
            f"refs={cfg.ref_mode}/max{cfg.ref_max}/q={cfg.ref_edit_quality}/vision={'on' if cfg.ref_vision else 'off'}")


async def main() -> int:
    setup_logging()
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
    try:
        cfg = Config.from_env()
    except ConfigError as e:
        log.error("%s. См. .env.example и README.md", e)
        return 2
    install_redaction(cfg)

    try:
        bot = Bot(token=cfg.telegram_token)
    except TokenValidationError:
        log.error("TELEGRAM_BOT_TOKEN имеет неверный формат (ожидается 123456:ABC...)")
        return 2

    if "--check" in sys.argv:
        await bot.session.close()
        log.info("Конфигурация OK: %s", describe(cfg))
        return 0

    if not cfg.allowed_user_ids:
        log.warning("ALLOWED_USER_IDS пуст — бот будет отвечать всем только на /start и /myid.")
    shutil.rmtree(cfg.work_dir or os.path.join(tempfile.gettempdir(), "grok-bot-jobs"), ignore_errors=True)
    xai = XAIClient(cfg)
    tracker = BalanceTracker(cfg)
    dp = build_dispatcher(cfg, xai, tracker)
    try:
        me = await bot.get_me()
        log.info("Запущен @%s | %s", me.username, describe(cfg))
        if tracker.uses_api:
            info, _ = await tracker.get(force_refresh=True)
            log.info("Баланс при старте: %s (источник: %s)", info.usd, info.source)
        await bot.delete_webhook(drop_pending_updates=False)
        await dp.start_polling(bot)
    except TelegramUnauthorizedError:
        log.error("Telegram отклонил TELEGRAM_BOT_TOKEN (Unauthorized). Проверьте токен в @BotFather.")
        return 3
    finally:
        await xai.close()
        await tracker.close()
        await bot.session.close()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        pass
