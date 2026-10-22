"""Поиск в вебе и X для обычного чата: серверные инструменты xAI (web_search, x_search) в Responses API,
разбор цитат и компактный список источников для Telegram (HTML)."""
from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

TOOL_TYPES = {"web": "web_search", "x": "x_search"}
FORCE_HINT = ("Перед ответом обязательно найди актуальную информацию с помощью поиска в интернете и/или в X "
              "и опирайся на найденные источники.")
INLINE_CIT = re.compile(r"\[\[(\d{1,3})\]\]\((https?://[^\s)]+)\)")
IMAGE_MD = re.compile(r"!\[[^\]]*\]\((https?://[^\s)]+)\)")


def tools_payload(spec: str = "web,x") -> list[dict]:
    out = []
    for p in (spec or "").replace(" ", "").lower().split(","):
        if p in TOOL_TYPES and {"type": TOOL_TYPES[p]} not in out:
            out.append({"type": TOOL_TYPES[p]})
    return out


@dataclass
class ChatResult:
    text: str
    cost: object = None
    sources: list[tuple[int, str]] = field(default_factory=list)  # (номер, url)
    web_calls: int = 0
    x_calls: int = 0
    x_posts: int = 0
    x_users: int = 0
    searched: bool = False
    search_unavailable: bool = False

    def usage_note(self) -> str:
        parts = []
        if self.web_calls:
            parts.append(f"веб ×{self.web_calls}")
        if self.x_calls or self.x_posts:
            x = f"X ×{self.x_calls}" if self.x_calls else "X"
            extra = []
            if self.x_posts:
                extra.append(f"постов {self.x_posts}")
            if self.x_users:
                extra.append(f"профилей {self.x_users}")
            parts.append(x + (f" ({', '.join(extra)})" if extra else ""))
        return ("🔎 Поиск: " + ", ".join(parts)) if parts else ""


def extract(data: dict, max_sources: int = 5) -> ChatResult:
    """Текст ответа + источники + счётчики поиска из ответа /v1/responses."""
    texts: list[str] = []
    ann: list[tuple[int, str]] = []
    web = xc = 0
    for item in data.get("output") or []:
        t = item.get("type")
        if t == "message":
            for c in item.get("content") or []:
                if c.get("type") == "output_text":
                    texts.append(c.get("text") or "")
                    for a in c.get("annotations") or []:
                        if a.get("type") == "url_citation" and a.get("url"):
                            title = str(a.get("title") or "")
                            ann.append((int(title) if title.isdigit() else 0, a["url"]))
        elif t == "web_search_call":
            web += 1
        elif t == "x_search_call" or (t == "custom_tool_call" and (str(item.get("call_id") or "").startswith("xs_")
                                                                    or str(item.get("name") or "").startswith("x_"))):
            xc += 1
    text = "".join(texts) or data.get("output_text") or ""
    # номера из [[N]](url) в тексте — главный источник нумерации
    numbered: dict[int, str] = {}
    for n, url in INLINE_CIT.findall(text):
        numbered.setdefault(int(n), url)
    for n, url in ann:
        if n and n not in numbered:
            numbered[n] = url
    seen = set(numbered.values())
    extra = [u for _, u in ann if u not in seen] + [u for u in (data.get("citations") or []) if isinstance(u, str)]
    sources = sorted(numbered.items())
    nxt = (max(numbered) if numbered else 0) + 1
    for u in extra:
        if u not in seen and len(sources) < max_sources:
            seen.add(u)
            sources.append((nxt, u))
            nxt += 1
    usage = data.get("usage") or {}
    det = usage.get("server_side_tool_usage_details") or {}
    ssu = usage.get("server_side_tool_usage") or data.get("server_side_tool_usage") or {}
    web = web or int(ssu.get("SERVER_SIDE_TOOL_WEB_SEARCH") or 0)
    xc = xc or int(ssu.get("SERVER_SIDE_TOOL_X_SEARCH") or 0)
    res = ChatResult(text=text, sources=sources[:max_sources], web_calls=web, x_calls=xc,
                     x_posts=int(det.get("x_posts_fetched") or 0), x_users=int(det.get("x_users_fetched") or 0))
    res.searched = bool(web or xc or res.x_posts or sources)
    return res


def clean_text(text: str) -> str:
    """[[1]](url) → [1]; картинки-markdown убираем (в Telegram это просто текст)."""
    text = INLINE_CIT.sub(lambda m: f"[{m.group(1)}]", text)
    text = IMAGE_MD.sub("", text)
    return re.sub(r"[ \t]+\n", "\n", text).strip()


def label(url: str) -> str:
    try:
        u = urlparse(url)
    except ValueError:
        return url[:40]
    host = (u.hostname or "").lower().removeprefix("www.").removeprefix("mobile.")
    path = [p for p in (u.path or "").split("/") if p]
    if host in ("x.com", "twitter.com"):
        if "status" in path:
            if path and path[0] not in ("i", "status"):
                return f"X\u00a0@{path[0]}"
            return "X\u00a0пост"
        if path and path[0] not in ("i", "search", "hashtag"):
            return f"X\u00a0@{path[0]}"
        return "X\u00a0профиль" if "user" in path else "X"
    return host or url[:40]


def sources_text(sources: list[tuple[int, str]]) -> str:
    """Видимый текст (для подсчёта длины — лимит Telegram считается после разбора разметки)."""
    if not sources:
        return ""
    return "🔗 Источники: " + " · ".join(f"[{n}]\u00a0{label(u)}" for n, u in sources)


def sources_html(sources: list[tuple[int, str]]) -> str:
    if not sources:
        return ""
    links = " · ".join(f'<a href="{html.escape(u, quote=True)}">[{n}]\u00a0{html.escape(label(u))}</a>' for n, u in sources)
    return "🔗 Источники: " + links
