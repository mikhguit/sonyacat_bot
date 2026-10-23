"""Instagram-контент: план от Grok (JSON) + рендер слайдов/сторис через Pillow.

Не зависит от Telegram и сети — всё рендерится локально, поэтому легко тестируется.
"""
from __future__ import annotations

import json
import math
import os
import random
import re
from dataclasses import dataclass, field

from PIL import Image, ImageDraw, ImageFilter, ImageFont

FONT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")
FONT_TITLE = os.path.join(FONT_DIR, "Montserrat-ExtraBold.ttf")
FONT_SEMI = os.path.join(FONT_DIR, "Montserrat-SemiBold.ttf")
FONT_BODY = os.path.join(FONT_DIR, "Montserrat-Regular.ttf")

CAROUSEL_SIZE = (1080, 1080)
STORY_SIZE = (1080, 1920)
STORY_SAFE_TOP = 250      # верх: аватар, имя, полоски прогресса
STORY_SAFE_BOTTOM = 340   # низ: поле ответа, кнопки

CAROUSEL_MIN, CAROUSEL_MAX, CAROUSEL_DEFAULT = 3, 10, 7
STORY_MIN, STORY_MAX, STORY_DEFAULT = 1, 10, 3


# ---------------------------------------------------------------------------
# План контента
# ---------------------------------------------------------------------------
@dataclass
class Slide:
    title: str
    text: str = ""
    image_prompt: str = ""


@dataclass
class Plan:
    kind: str  # "carousel" | "story"
    slides: list[Slide]
    caption: str = ""
    hashtags: list[str] = field(default_factory=list)
    style_prompt: str = ""


def plan_schema() -> dict:
    slide = {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Заголовок слайда, до 7 слов"},
            "text": {"type": "string", "description": "Текст слайда, до 35 слов (может быть пустым)"},
            "image_prompt": {"type": "string", "description": "Описание фоновой сцены на английском, без текста"},
        },
        "required": ["title", "text", "image_prompt"],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {
            "slides": {"type": "array", "items": slide},
            "caption": {"type": "string"},
            "hashtags": {"type": "array", "items": {"type": "string"}},
            "style_prompt": {"type": "string"},
        },
        "required": ["slides", "caption", "hashtags", "style_prompt"],
        "additionalProperties": False,
    }


def plan_messages(kind: str, topic: str, n: int, brand_name: str = "", brand_style: str = "",
                  ref_note: str = "") -> list[dict]:
    if kind == "carousel":
        structure = (
            f"Ровно {n} слайдов для Instagram-карусели. Слайд 1 — цепляющий хук (заголовок-обещание, "
            f"короткий подзаголовок). Слайды 2–{n - 1} — полезный контент, по одной мысли на слайд. "
            f"Слайд {n} — призыв к действию (сохранить, подписаться, написать в директ)."
        )
        limits = "Заголовок — до 7 слов, текст — до 35 слов."
    else:
        structure = (
            f"Ровно {n} кадров для Instagram Stories (вертикально 9:16). "
            + ("Один кадр: хук + суть + призыв." if n == 1 else
               f"Кадр 1 — хук, далее суть, кадр {n} — призыв к действию (ответить, перейти, написать).")
        )
        limits = "Заголовок — до 6 слов, текст — до 20 слов: сторис читают за 3–5 секунд."
    brand = ""
    if brand_name:
        brand += f" Бренд/аккаунт: {brand_name} — упомяни его в призыве к действию."
    if brand_style:
        brand += f" Фирменный стиль: {brand_style}."
    system = (
        "Ты — SMM-копирайтер и арт-директор. Пишешь живо, конкретно, без воды и канцелярита, по-русски. "
        "Никаких эмодзи в title и text слайдов (их нельзя отрисовать шрифтом). "
        "Отвечай только JSON по схеме."
    )
    user = (
        f"Тема: {topic}\n\n{structure} {limits}{brand}\n\n"
        "caption — подпись к посту по-русски (до 1500 символов, абзацы, можно эмодзи, в конце вопрос к аудитории). "
        "hashtags — 10–15 релевантных хэштегов по-русски и по-английски, с символом #. "
        "style_prompt — ОДИН общий визуальный стиль для всех фонов на английском (палитра, свет, техника, "
        "настроение), чтобы серия выглядела единой. "
        "image_prompt каждого слайда — сцена/объект для фона на английском, без надписей и букв, "
        "с пустым пространством под текст.\n\n"
        'Формат ответа: {"slides":[{"title":"...","text":"...","image_prompt":"..."}],'
        '"caption":"...","hashtags":["#..."],"style_prompt":"..."}'
        + ref_note
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def parse_json_loose(text: str) -> dict:
    text = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if m:
        text = m.group(1).strip()
    try:
        return json.loads(text)
    except ValueError:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start:end + 1])
        raise


_EMOJI = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0000FE00-\U0000FE0F\U0000200D\U00002B00-\U00002BFF"
    "\U0001F1E6-\U0001F1FF\U000E0000-\U000E007F]+"
)


def clean_slide_text(s: str) -> str:
    s = _EMOJI.sub("", s or "")
    s = s.replace("**", "").replace("__", "")
    s = re.sub(r"[ \t]+", " ", s)
    return s.strip()


def normalize_plan(data: dict, kind: str, n: int, topic: str) -> Plan:
    raw = data.get("slides") or data.get("frames") or []
    slides: list[Slide] = []
    for item in raw[:n]:
        if isinstance(item, dict):
            slides.append(Slide(
                title=clean_slide_text(str(item.get("title") or "")),
                text=clean_slide_text(str(item.get("text") or item.get("body") or "")),
                image_prompt=str(item.get("image_prompt") or "").strip(),
            ))
    while len(slides) < n:  # модель вернула меньше — добиваем, чтобы число слайдов совпало
        slides.append(Slide(title=clean_slide_text(topic)[:60] if not slides else "Сохраните, чтобы не потерять",
                            text="", image_prompt="abstract soft background"))
    for s in slides:
        if not s.title and s.text:
            s.title, s.text = s.text, ""
    tags = []
    for t in data.get("hashtags") or []:
        t = re.sub(r"\s+", "", str(t))
        if not t:
            continue
        tags.append(t if t.startswith("#") else "#" + t)
    return Plan(kind=kind, slides=slides, caption=str(data.get("caption") or "").strip(),
                hashtags=tags[:30], style_prompt=str(data.get("style_prompt") or "").strip())


def image_prompt_for(plan: Plan, slide: Slide, brand_style: str = "") -> str:
    parts = [plan.style_prompt, slide.image_prompt, brand_style,
             "Background image for an Instagram post. Absolutely no text, no letters, no words, no logos, "
             "no watermark. Leave calm negative space for a text overlay."]
    return ". ".join(p.strip().rstrip(".") for p in parts if p and p.strip())


def caption_text(plan: Plan) -> str:
    cap = plan.caption
    if plan.hashtags:
        cap = (cap + "\n\n" + " ".join(plan.hashtags)).strip()
    return cap[:2200]  # лимит подписи Instagram


# ---------------------------------------------------------------------------
# Рендер
# ---------------------------------------------------------------------------
def hex_color(s: str, default=(255, 213, 74)) -> tuple[int, int, int]:
    s = (s or "").strip().lstrip("#")
    if re.fullmatch(r"[0-9a-fA-F]{6}", s):
        return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]
    return default


def fit_cover(img: Image.Image, size: tuple[int, int]) -> Image.Image:
    """Обрезка по центру до нужных пропорций + ресайз (как CSS object-fit: cover)."""
    tw, th = size
    w, h = img.size
    scale = max(tw / w, th / h)
    nw, nh = max(tw, round(w * scale)), max(th, round(h * scale))
    img = img.convert("RGB").resize((nw, nh), Image.LANCZOS)
    left, top = (nw - tw) // 2, (nh - th) // 2
    return img.crop((left, top, left + tw, top + th))


def placeholder_background(size: tuple[int, int], seed: int = 0) -> Image.Image:
    """Цветной градиент с мягкими пятнами — фон-заглушка (превью и запасной вариант при ошибке API)."""
    rnd = random.Random(seed)
    w, h = size
    palettes = [((32, 58, 96), (233, 120, 80)), ((20, 30, 48), (36, 59, 85)), ((67, 40, 116), (220, 90, 140)),
                ((15, 76, 92), (230, 200, 120)), ((40, 40, 40), (120, 140, 160))]
    c1, c2 = palettes[seed % len(palettes)]
    small = Image.new("RGB", (64, 64))
    px = small.load()
    for y in range(64):
        for x in range(64):
            t = (x + y) / 126
            px[x, y] = tuple(int(c1[i] + (c2[i] - c1[i]) * t) for i in range(3))
    img = small.resize(size, Image.BICUBIC)
    blobs = Image.new("RGBA", size, (0, 0, 0, 0))
    d = ImageDraw.Draw(blobs)
    for _ in range(6):
        r = rnd.randint(w // 6, w // 2)
        cx, cy = rnd.randint(0, w), rnd.randint(0, h)
        col = tuple(min(255, c + rnd.randint(-30, 60)) for c in (c2 if rnd.random() < .5 else c1))
        d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=col + (110,))
    blobs = blobs.filter(ImageFilter.GaussianBlur(w // 10))
    img.paste(blobs, (0, 0), blobs)
    return img


def _font(path: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(path, size)


NBSP = "\u00a0"
_SHORT_WORD = re.compile(r"(?<![\w-])([A-Za-zА-Яа-яЁё]{1,2}|не|ни|за|до|по|на|от|из|со|во|об|без|для|при|про)\s+", re.I)


def typograph(text: str) -> str:
    """Неразрывные пробелы: короткие слова/предлоги не висят в конце строки, «°C», «%», «—» не отрываются."""
    text = _SHORT_WORD.sub(lambda m: m.group(1) + NBSP, text)
    text = re.sub(r"(\d)\s+(°|%|₽|\$|€|руб|мин|сек|ч\b|г\b|кг|мл|л\b)", lambda m: m.group(1) + NBSP + m.group(2), text)
    text = re.sub(r"\s+(—|–)\s", lambda m: NBSP + m.group(1) + " ", text)
    return text


def wrap_text(text: str, font: ImageFont.FreeTypeFont, max_w: int) -> list[str]:
    lines: list[str] = []
    for para in typograph(text).split("\n"):
        words = [w for w in para.split(" ") if w]
        if not words:
            lines.append("")
            continue
        cur = ""
        for word in words:
            while font.getlength(word) > max_w and len(word) > 1:  # очень длинное слово — режем
                cut = len(word)
                while cut > 1 and font.getlength(word[:cut] + "-") > max_w:
                    cut -= 1
                if cur:
                    lines.append(cur)
                    cur = ""
                lines.append(word[:cut] + "-")
                word = word[cut:]
            cand = f"{cur} {word}".strip()
            if font.getlength(cand) <= max_w:
                cur = cand
            else:
                lines.append(cur)
                cur = word
        if cur:
            lines.append(cur)
    return lines


@dataclass
class TextBlock:
    lines: list[str]
    font: ImageFont.FreeTypeFont
    line_h: int

    @property
    def height(self) -> int:
        return self.line_h * len(self.lines) if self.lines else 0


def fit_block(text: str, path: str, max_w: int, max_h: int, start: int, minimum: int,
              spacing: float = 1.22) -> TextBlock:
    size = start
    while True:
        font = _font(path, size)
        lines = wrap_text(text, font, max_w) if text else []
        lh = int(size * spacing)
        if lh * len(lines) <= max_h or size <= minimum:
            if lh * len(lines) > max_h:  # даже минимальный размер не влез — обрезаем с многоточием
                keep = max(1, max_h // lh)
                lines = lines[:keep]
                lines[-1] = lines[-1].rstrip(" .,;:") + "…"
            return TextBlock(lines, font, lh)
        size -= 2


def _vertical_gradient(size, top_alpha: int, bottom_alpha: int, start_frac: float) -> Image.Image:
    w, h = size
    col = Image.new("L", (1, h))
    start = int(h * start_frac)
    for y in range(h):
        if y < start:
            a = top_alpha
        else:
            t = (y - start) / max(1, h - start)
            a = int(top_alpha + (bottom_alpha - top_alpha) * (t ** 0.9))
        col.putpixel((0, y), a)
    alpha = col.resize((w, h))
    over = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    over.putalpha(alpha)
    return over


def _pill(draw: ImageDraw.ImageDraw, xy_right_top: tuple[int, int], text: str, font, fg=(255, 255, 255),
          bg=(0, 0, 0, 150), pad=(22, 12)) -> None:
    x_r, y = xy_right_top
    tw = int(font.getlength(text))
    asc, desc = font.getmetrics()
    th = asc + desc
    box = (x_r - tw - 2 * pad[0], y, x_r, y + th + 2 * pad[1])
    draw.rounded_rectangle(box, radius=(th + 2 * pad[1]) // 2, fill=bg)
    draw.text((box[0] + pad[0], y + pad[1]), text, font=font, fill=fg)


LAST_LAYOUT: dict = {}  # геометрия последнего рендера (для тестов)


def render_slide(bg: Image.Image | None, slide: Slide, idx: int, total: int, kind: str,
                 brand_name: str = "", accent: tuple[int, int, int] = (255, 213, 74)) -> Image.Image:
    """idx — с 0. Возвращает RGB-картинку 1080x1080 (карусель) или 1080x1920 (сторис)."""
    size = CAROUSEL_SIZE if kind == "carousel" else STORY_SIZE
    W, H = size
    base = fit_cover(bg, size) if bg is not None else placeholder_background(size, idx)
    img = base.convert("RGBA")
    is_first, is_last = idx == 0, idx == total - 1 and total > 1

    if kind == "carousel":
        margin = 80
        area_top, area_bottom = 150, H - 110  # сверху — счётчик, снизу — подпись бренда
        img.alpha_composite(_vertical_gradient(size, 40, 225, 0.30))
    else:
        margin = 90
        area_top, area_bottom = STORY_SAFE_TOP + 90, H - STORY_SAFE_BOTTOM - 40
        # затемняем сверху и снизу (там интерфейс Instagram) + усиливаем низ под текст
        img.alpha_composite(_vertical_gradient(size, 60, 230, 0.35))

    max_w = W - 2 * margin - 2 * 36  # 36 — внутренний отступ плашки
    title_start = 100 if (kind == "carousel" and is_first) else (92 if kind == "story" else 72)
    body_start = 44 if kind == "carousel" else 50
    avail = area_bottom - area_top
    title = fit_block(slide.title, FONT_TITLE, max_w, int(avail * 0.55), title_start, 40, 1.12)
    body_text = slide.text
    body = fit_block(body_text, FONT_BODY, max_w, avail - title.height - 120, body_start, 28, 1.32)

    extra = []  # (текст, шрифт, цвет) — бренд на последнем слайде
    if is_last and brand_name:
        bf = _font(FONT_TITLE, 46 if kind == "carousel" else 54)
        extra.append((brand_name, bf, accent + (255,)))

    gap = 26 if body.lines else 0
    bar_h, bar_gap = 10, 28
    extra_h = sum(int(f.size * 1.4) for _, f, _ in extra) + (24 if extra else 0)
    block_h = bar_h + bar_gap + title.height + gap + body.height + extra_h
    pad = 36
    if kind == "story":
        y0 = area_bottom - block_h - pad   # текст в нижней части безопасной зоны
    elif is_first:
        y0 = area_bottom - block_h - pad - 40
    else:
        y0 = area_bottom - block_h - pad
    y0 = max(area_top + pad, y0)

    LAST_LAYOUT.update(plate=(margin, y0 - pad, W - margin, y0 + block_h + pad), title_size=title.font.size,
                       body_size=body.font.size, lines=len(title.lines) + len(body.lines))
    plate = Image.new("RGBA", size, (0, 0, 0, 0))
    pd = ImageDraw.Draw(plate)
    pd.rounded_rectangle((margin, y0 - pad, W - margin, y0 + block_h + pad), radius=36, fill=(10, 10, 14, 150))
    img.alpha_composite(plate)

    d = ImageDraw.Draw(img)
    x = margin + pad
    y = y0
    d.rounded_rectangle((x, y, x + 120, y + bar_h), radius=5, fill=accent + (255,))
    y += bar_h + bar_gap
    for line in title.lines:
        d.text((x, y), line, font=title.font, fill=(255, 255, 255, 255))
        y += title.line_h
    y += gap
    for line in body.lines:
        d.text((x, y), line, font=body.font, fill=(232, 232, 236, 255))
        y += body.line_h
    if extra:
        y += 24
        for t, f, col in extra:
            d.text((x, y), t, font=f, fill=col)
            y += int(f.size * 1.4)

    # счётчик слайдов
    cnt_font = _font(FONT_SEMI, 34 if kind == "carousel" else 38)
    cnt_y = 60 if kind == "carousel" else STORY_SAFE_TOP + 20
    if total > 1:
        _pill(d, (W - margin, cnt_y), f"{idx + 1}/{total}", cnt_font)
    # подсказка листать на первом слайде карусели
    if kind == "carousel" and is_first and total > 1:
        f = _font(FONT_SEMI, 32)
        _pill(d, (W - margin, H - 95), "Листайте  →", f, fg=(20, 20, 20), bg=accent + (235,))
    # небольшая подпись бренда (кроме последнего, где он крупно)
    if brand_name and not is_last:
        f = _font(FONT_SEMI, 30 if kind == "carousel" else 34)
        by = (H - 85) if kind == "carousel" else (STORY_SAFE_TOP + 28)
        d.text((margin + (0 if kind == "carousel" else 0), by), brand_name, font=f, fill=(255, 255, 255, 220))
    out = img.convert("RGB")
    img.close()
    base.close()
    return out


def contact_sheet(paths: list[str], cols: int, thumb_w: int, out_path: str, gap: int = 16) -> None:
    """Склеивает превью в одну картинку (для README/демо)."""
    ims = []
    for p in paths:
        im = Image.open(p)
        r = thumb_w / im.width
        ims.append(im.resize((thumb_w, round(im.height * r)), Image.LANCZOS))
        im.close()
    th = max(i.height for i in ims)
    rows = math.ceil(len(ims) / cols)
    sheet = Image.new("RGB", (cols * thumb_w + (cols + 1) * gap, rows * th + (rows + 1) * gap), (245, 245, 247))
    for k, im in enumerate(ims):
        r, c = divmod(k, cols)
        sheet.paste(im, (gap + c * (thumb_w + gap), gap + r * (th + gap)))
        im.close()
    sheet.save(out_path, quality=90)


# ---------------------------------------------------------------------------
# Overlay: текст поверх фото пользователя (фото не перерисовывается)
# ---------------------------------------------------------------------------
OVERLAY_POSITIONS = ("bottom", "top", "center", "auto")
OVERLAY_STYLES = ("gradient", "plate")


def overlay_schema() -> dict:
    slide = {
        "type": "object",
        "properties": {
            "photo": {"type": "string", "description": "Что на этом фото, 3–8 слов (для сверки)"},
            "title": {"type": "string", "description": "Заголовок слайда, до 6 слов"},
            "text": {"type": "string", "description": "Короткий текст, до 20 слов (может быть пустым)"},
        },
        "required": ["photo", "title", "text"],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {
            "slides": {"type": "array", "items": slide},
            "caption": {"type": "string"},
            "hashtags": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["slides", "caption", "hashtags"],
        "additionalProperties": False,
    }


def overlay_messages(kind: str, topic: str, n: int, images: list[str] | None = None, brand_name: str = "",
                     brand_style: str = "", detail: str = "low") -> list[dict]:
    """Один запрос: тексты для n слайдов, слайд i — к фото i. images — data URI (или None, если vision выключен)."""
    what = "слайдов Instagram-карусели" if kind == "carousel" else "кадров Instagram Stories (9:16)"
    if n == 1:
        structure = "Один слайд: хук + суть + призыв."
    else:
        structure = (f"Слайд 1 — цепляющий хук, далее по одной мысли на слайд, слайд {n} — призыв к действию "
                     "(сохранить, подписаться, написать).")
    brand = f" Бренд/аккаунт: {brand_name} — упомяни его в призыве к действию." if brand_name else ""
    if brand_style:
        brand += f" Тон бренда: {brand_style}."
    if images:
        photo_rule = ("Ниже фото пользователя по порядку (Фото 1…Фото N). Текст слайда i пишется поверх фото i — "
                      "он должен соответствовать тому, что на этом фото, и теме. В поле photo кратко опиши фото i.")
    else:
        photo_rule = ("Тексты лягут поверх фото пользователя (по одному на слайд), но фото ты не видишь — "
                      "пиши по теме, нейтрально к содержимому кадра; в поле photo пиши «—».")
    system = ("Ты — SMM-копирайтер. Пишешь живо, конкретно, без воды, по-русски. Текст накладывается поверх "
              "фотографии, поэтому он короткий. Никаких эмодзи в title и text. Отвечай только JSON по схеме.")
    user = (
        f"Тема: {topic or 'не задана — придумай её по содержимому фото'}\n\n"
        f"Ровно {n} {what}. {structure} Заголовок — до 6 слов, текст — до 20 слов.{brand}\n\n"
        f"{photo_rule}\n\n"
        "caption — подпись к посту по-русски (до 1200 символов, можно эмодзи, в конце вопрос к аудитории). "
        "hashtags — 10–15 релевантных хэштегов с #.\n"
        'Формат: {"slides":[{"photo":"...","title":"...","text":"..."}],"caption":"...","hashtags":["#..."]}'
    )
    content: list[dict] = [{"type": "input_text", "text": user}]
    for i, uri in enumerate(images or []):
        content.append({"type": "input_text", "text": f"Фото {i + 1}:"})
        content.append({"type": "input_image", "image_url": uri, "detail": detail})
    return [{"role": "system", "content": system}, {"role": "user", "content": content if images else user}]


def open_photo(path: str, size: tuple[int, int]) -> Image.Image:
    """Открыть фото с учётом EXIF-поворота, JPEG декодируется сразу в нужном масштабе (RAM)."""
    from PIL import ImageOps
    im = Image.open(path)
    if im.format == "JPEG":
        im.draft("RGB", (size[0], size[1]))  # draft не уменьшит ниже запрошенного
    out = ImageOps.exif_transpose(im)
    if out is not im:
        im.close()
    if out.mode != "RGB":
        rgb = out.convert("RGB")
        out.close()
        out = rgb
    return out


def _busy_score(img: Image.Image, box: tuple[int, int, int, int]) -> float:
    """Насколько «занята» зона (средняя яркость границ) — для OVERLAY_TEXT_POS=auto."""
    from PIL import ImageStat
    reg = img.crop(box).convert("L")
    reg.thumbnail((270, 270))
    return ImageStat.Stat(reg.filter(ImageFilter.FIND_EDGES)).mean[0]


def render_overlay(photo: Image.Image, slide: Slide, idx: int, total: int, kind: str, brand_name: str = "",
                   accent: tuple[int, int, int] = (255, 213, 74), pos: str = "bottom",
                   style: str = "gradient") -> Image.Image:
    """Фото → cover-crop до 1080×1080 / 1080×1920, затемнение ТОЛЬКО под текстом, текст, счётчик, бренд.
    Пиксели вне LAST_LAYOUT['touched'] совпадают с fit_cover(photo)."""
    size = CAROUSEL_SIZE if kind == "carousel" else STORY_SIZE
    W, H = size
    img = fit_cover(photo, size)
    is_first, is_last = idx == 0, idx == total - 1 and total > 1
    if kind == "carousel":
        margin, area_top, area_bottom = 70, 140, H - 120
    else:
        margin, area_top, area_bottom = 80, STORY_SAFE_TOP + 100, H - STORY_SAFE_BOTTOM - 30
    if pos not in OVERLAY_POSITIONS:
        pos = "bottom"
    if pos == "auto":
        band = (area_bottom - area_top) // 3
        top_s = _busy_score(img, (0, area_top, W, area_top + band))
        bot_s = _busy_score(img, (0, area_bottom - band, W, area_bottom))
        pos = "top" if top_s < bot_s * 0.8 else "bottom"
    style = style if style in OVERLAY_STYLES else "gradient"
    pad = 36 if style == "plate" else 0
    max_w = W - 2 * margin - 2 * pad
    avail = (area_bottom - area_top) // (2 if kind == "carousel" else 3) + (60 if kind == "carousel" else 120)
    title = fit_block(slide.title, FONT_TITLE, max_w, int(avail * 0.6), 80 if is_first else 64, 38, 1.12)
    body = fit_block(slide.text, FONT_BODY, max_w, max(60, avail - title.height - 60),
                     42 if kind == "carousel" else 46, 28, 1.3)
    extra = []
    if is_last and brand_name:
        extra.append((brand_name, _font(FONT_TITLE, 44 if kind == "carousel" else 52), accent))
    gap = 22 if body.lines else 0
    bar_h, bar_gap = 10, 24
    extra_h = sum(int(f.size * 1.4) for _, f, _ in extra) + (20 if extra else 0)
    block_h = bar_h + bar_gap + title.height + gap + body.height + extra_h
    if pos == "top":
        y0 = area_top + pad
    elif pos == "center":
        y0 = (H - block_h) // 2
    else:
        y0 = area_bottom - block_h - pad
    y0 = max(area_top + pad, y0)
    text_box = (margin, y0, W - margin, y0 + block_h)
    touched: list[tuple[int, int, int, int]] = []

    if style == "plate":
        box = (margin, y0 - pad, W - margin, y0 + block_h + pad)
        ImageDraw.Draw(img, "RGBA").rounded_rectangle(box, radius=32, fill=(10, 10, 14, 165))
        touched.append(box)
    else:
        # полоса затемнения: плавный вход (fade) → плато под текстом; вне полосы фото не трогаем
        fade, mx = (130 if kind == "carousel" else 180), 175
        t0, t1 = y0 - 40, y0 + block_h + 40
        if pos == "bottom":
            b0, b1 = max(0, t0 - fade), H
        elif pos == "top":
            b0, b1 = 0, min(H, t1 + fade)
        else:
            b0, b1 = max(0, t0 - fade), min(H, t1 + fade)
        col = Image.new("L", (1, b1 - b0), 0)
        for y in range(b0, b1):
            if y < t0 and pos != "top":
                a = mx * ((y - b0) / max(1, t0 - b0)) ** 1.6
            elif y > t1 and pos != "bottom":
                a = mx * ((b1 - y) / max(1, b1 - t1)) ** 1.6
            else:
                a = mx
            col.putpixel((0, y - b0), int(a))
        mask = col.resize((W, b1 - b0))
        region = img.crop((0, b0, W, b1))
        dark = Image.new("RGB", region.size, (0, 0, 0))
        img.paste(Image.composite(dark, region, mask), (0, b0))
        for x in (mask, region, dark, col):
            x.close()
        touched.append((0, b0, W, b1))

    d = ImageDraw.Draw(img, "RGBA")
    shadow = (0, 0, 0, 110)
    x, y = margin + pad, y0
    d.rounded_rectangle((x, y, x + 110, y + bar_h), radius=5, fill=accent)
    y += bar_h + bar_gap
    for line in title.lines:
        d.text((x + 2, y + 3), line, font=title.font, fill=shadow)
        d.text((x, y), line, font=title.font, fill=(255, 255, 255))
        y += title.line_h
    y += gap
    for line in body.lines:
        d.text((x + 2, y + 2), line, font=body.font, fill=shadow)
        d.text((x, y), line, font=body.font, fill=(236, 236, 240))
        y += body.line_h
    if extra:
        y += 20
        for t, f, colr in extra:
            d.text((x, y), t, font=f, fill=colr)
            y += int(f.size * 1.4)
    touched.append(text_box)

    def pill(right: int, top: int, text: str, font, fg=(255, 255, 255), bg=(0, 0, 0, 150), padx=(22, 12)):
        tw = int(font.getlength(text))
        asc, desc = font.getmetrics()
        th = asc + desc
        box = (right - tw - 2 * padx[0], top, right, top + th + 2 * padx[1])
        d.rounded_rectangle(box, radius=(th + 2 * padx[1]) // 2, fill=bg)
        d.text((box[0] + padx[0], top + padx[1]), text, font=font, fill=fg)
        touched.append(box)

    cnt_y = 50 if kind == "carousel" else STORY_SAFE_TOP + 20
    if total > 1:
        pill(W - margin, cnt_y, f"{idx + 1}/{total}", _font(FONT_SEMI, 32 if kind == "carousel" else 36))
    if kind == "carousel" and is_first and total > 1:
        f = _font(FONT_SEMI, 30)
        fy = H - 92 if pos != "bottom" or style == "plate" else H - 80
        pill(W - margin, fy, "Листайте  →", f, fg=(20, 20, 20), bg=accent + (235,))
    if brand_name and not is_last:
        f = _font(FONT_SEMI, 28 if kind == "carousel" else 32)
        tw = int(f.getlength(brand_name))
        by = 50 if kind == "carousel" else STORY_SAFE_TOP + 20
        pill(margin + tw + 44, by, brand_name, f)
    LAST_LAYOUT.clear()
    LAST_LAYOUT.update(mode="overlay", pos=pos, style=style, text_box=text_box, touched=touched,
                       title_size=title.font.size, body_size=body.font.size,
                       lines=len(title.lines) + len(body.lines))
    return img
