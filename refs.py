"""Референсы пользователя: приём фото, уменьшение, описание стиля (vision), флаги режима."""
from __future__ import annotations

import base64
import io
import os
from dataclasses import dataclass

from PIL import Image, ImageOps

REF_MAX_SIDE = 2048     # храним не больше 2048 px по длинной стороне (хватает на 1080×1920 без апскейла)
EDIT_MAX_SIDE = 1536    # в /images/edits отправляем не больше 1536 px (размер запроса)
VISION_MAX_SIDE = 768   # для описания стиля хватает
OVERLAY_VISION_SIDE = 512  # overlay: Grok смотрит фото в detail=low — 512 px достаточно
API_MAX_REFS = 5        # /v1/images/edits принимает до 5 исходных картинок
OVERLAY_MAX_REFS = 10   # overlay: до 10 фото (= до 10 слайдов, лимит альбома Telegram)
MAX_DECODE_PIXELS = 60_000_000  # не-JPEG крупнее ~60 Мп не декодируем (RAM 256 МБ)

MODE_EDIT, MODE_STYLE, MODE_BG, MODE_OVERLAY = "edit", "style", "bg", "overlay"
MODES = (MODE_EDIT, MODE_STYLE, MODE_BG, MODE_OVERLAY)
MODE_WORDS = {
    "bg": MODE_BG, "фон": MODE_BG, "-bg": MODE_BG, "--bg": MODE_BG, "asis": MODE_BG,
    "style": MODE_STYLE, "стиль": MODE_STYLE, "-style": MODE_STYLE, "--style": MODE_STYLE,
    "edit": MODE_EDIT, "ref": MODE_EDIT, "реф": MODE_EDIT,
    "overlay": MODE_OVERLAY, "-overlay": MODE_OVERLAY, "--overlay": MODE_OVERLAY, "наложение": MODE_OVERLAY,
    "поверх": MODE_OVERLAY, "оверлей": MODE_OVERLAY,
}
NOREF_WORDS = {"noref", "безреф", "без-референса", "-noref", "--noref"}
MODE_TITLES = {
    MODE_EDIT: "референсы → в генерацию картинок (стиль/персонаж/товар)",
    MODE_STYLE: "только стиль с референсов (описание Grok → в промпт)",
    MODE_BG: "фото как фон слайда (без генерации картинок)",
    MODE_OVERLAY: "текст поверх ваших фото без изменений (1 фото = 1 слайд, без генерации)",
}


def split_flags(raw: str) -> tuple[str, str | None, bool]:
    """Убирает из аргументов флаги режима референсов -> (аргументы, режим|None, noref)."""
    mode, noref, rest = None, False, []
    for w in (raw or "").split():
        lw = w.lower().strip(",.")
        if lw in MODE_WORDS:
            mode = MODE_WORDS[lw]
        elif lw in NOREF_WORDS:
            noref = True
        else:
            rest.append(w)
    return " ".join(rest), mode, noref


class StillError(ValueError):
    """Кадр для image-to-video не принят. code: empty | too_big | format | pixels | unreadable."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


STILL_FORMATS = frozenset({"JPEG", "PNG", "WEBP"})  # Videos API: image.url — JPEG, PNG или WebP
STILL_MAX_BYTES = 20 * 1024 * 1024  # Bot API отдаёт файл до 20 МБ
VIDEO_STILL_MAX_SIDE = 1536  # длинная сторона data URI в image-to-video (меньше JSON и RAM)


def save_video_still(data: bytes, path: str, max_bytes: int = STILL_MAX_BYTES) -> tuple[int, int]:
    """Проверяет байты кадра (JPEG/PNG/WebP) и сохраняет их как референс: EXIF-поворот, лимит стороны."""
    if not data:
        raise StillError("empty")
    if len(data) > max_bytes:
        raise StillError("too_big")
    try:
        with Image.open(io.BytesIO(data)) as im:
            fmt = (im.format or "").upper()
    except Exception as e:
        raise StillError("unreadable") from e
    if fmt not in STILL_FORMATS:
        raise StillError("format")
    try:
        return save_ref(data, path, REF_MAX_SIDE)
    except ValueError as e:
        raise StillError("pixels") from e
    except Exception as e:
        raise StillError("unreadable") from e


def video_still_data_uri(path: str, max_side: int = VIDEO_STILL_MAX_SIDE) -> str:
    """Локальный файл → data:image/jpeg;base64,... для поля image.url. Не URL Telegram."""
    uri = data_uri(path, max_side)
    if (not uri.startswith("data:image/jpeg;base64,") or "api.telegram.org" in uri or "/file/bot" in uri):
        raise StillError("format")
    return uri


def save_ref(data: bytes, path: str, max_side: int = REF_MAX_SIDE) -> tuple[int, int]:
    """Открывает фото (JPEG/PNG/WebP), поворачивает по EXIF, уменьшает и сохраняет JPEG."""
    im = Image.open(io.BytesIO(data))
    if im.format == "JPEG":
        im.draft("RGB", (max_side, max_side))  # декодирование сразу в уменьшенном масштабе — экономит RAM
    elif im.width * im.height > MAX_DECODE_PIXELS:
        raise ValueError(f"слишком большое изображение ({im.width}×{im.height})")
    im = ImageOps.exif_transpose(im)
    if im.mode in ("RGBA", "LA", "P"):
        im = im.convert("RGBA")
        bg = Image.new("RGB", im.size, (255, 255, 255))
        bg.paste(im, mask=im.split()[3])
        im = bg
    elif im.mode != "RGB":
        im = im.convert("RGB")
    im.thumbnail((max_side, max_side), Image.LANCZOS)
    im.save(path, "JPEG", quality=95)  # overlay выводит фото как есть — храним почти без потерь
    size = im.size
    im.close()
    return size


def data_uri(path: str, max_side: int | None = None) -> str:
    if max_side is None:
        with open(path, "rb") as f:
            raw = f.read()
    else:
        with Image.open(path) as im:
            if max(im.size) <= max_side and im.format == "JPEG":
                with open(path, "rb") as f:
                    return "data:image/jpeg;base64," + base64.b64encode(f.read()).decode()
            im.draft("RGB", (max_side, max_side))
            im = im.convert("RGB")
            im.thumbnail((max_side, max_side))
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=85)
            raw = buf.getvalue()
    return "data:image/jpeg;base64," + base64.b64encode(raw).decode()


@dataclass
class RefStyle:
    subject: str = ""   # что на фото (англ.) — для промптов
    style: str = ""     # визуальный стиль (англ.)
    palette: str = ""
    summary_ru: str = ""

    def prompt_suffix(self) -> str:
        parts = []
        if self.style:
            parts.append(f"Visual style matching the user's reference photos: {self.style}")
        if self.palette:
            parts.append(f"Color palette: {self.palette}")
        return ". ".join(parts)

    def plan_note(self) -> str:
        if not (self.summary_ru or self.subject):
            return ""
        return ("\n\nПользователь прислал референсные фото. Что на них: "
                f"{self.summary_ru or self.subject}. Визуальный стиль: {self.style or '—'}; палитра: {self.palette or '—'}. "
                "Сделай style_prompt в этом же стиле, а image_prompt — с тем же объектом/персонажем, если он важен для темы.")


def vision_schema() -> dict:
    props = {
        "subject": {"type": "string", "description": "Main subject/product/character, English, 1 sentence"},
        "style": {"type": "string", "description": "Photographic/visual style, lighting, composition, mood; English, <=40 words"},
        "palette": {"type": "string", "description": "3-6 main colors, English"},
        "summary_ru": {"type": "string", "description": "Кратко по-русски, что на фото и какое настроение, <=25 слов"},
    }
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


def vision_messages(paths: list[str], detail: str = "low") -> list[dict]:
    content: list[dict] = []
    for p in paths:
        content.append({"type": "input_image", "image_url": data_uri(p, VISION_MAX_SIDE), "detail": detail})
    content.append({"type": "input_text", "text": (
        "These are reference photos for social media content. Describe what they share: the main subject "
        "(product/person/pet/character), visual style (lighting, lens, composition, texture, mood) and color palette, "
        "so another image model can reproduce the look. Reply only with JSON by the schema.")})
    return [{"role": "user", "content": content}]


def normalize_style(data: dict) -> RefStyle:
    g = lambda k, n: " ".join(str(data.get(k) or "").split())[:n]
    return RefStyle(g("subject", 300), g("style", 400), g("palette", 200), g("summary_ru", 300))


def edit_prompt(base_prompt: str, n_refs: int, keep_subject: bool = True) -> str:
    refs = "the reference image" if n_refs == 1 else f"the {n_refs} reference images"
    keep = (f"Keep the same main subject/product/character as in {refs} (shape, colors, details) "
            if keep_subject else "")
    return (f"Create a NEW image (not a copy) using {refs} as visual reference: match their style, lighting and "
            f"color palette. {keep}New scene: {base_prompt}")


def contact_sheet(paths: list[str], cell: int = 360) -> bytes:
    cols = min(5, max(1, len(paths)))
    rows = (len(paths) + cols - 1) // cols or 1
    sheet = Image.new("RGB", (cell * cols + 8 * (cols + 1), cell * rows + 8 * (rows + 1)), (24, 24, 28))
    for i, p in enumerate(paths):
        with Image.open(p) as im:
            im.draft("RGB", (cell, cell))
            im = ImageOps.fit(im.convert("RGB"), (cell, cell))
            r, c = divmod(i, cols)
            sheet.paste(im, (8 + c * (cell + 8), 8 + r * (cell + 8)))
    buf = io.BytesIO()
    sheet.save(buf, "JPEG", quality=85)
    return buf.getvalue()


def list_refs(d: str) -> list[str]:
    if not os.path.isdir(d):
        return []
    # порядок загрузки: время записи, затем имя (ref_01…ref_10 / ref_<time_ns>)
    return sorted((os.path.join(d, f) for f in os.listdir(d) if f.endswith(".jpg")),
                  key=lambda p: (os.path.getmtime(p), os.path.basename(p)))


def pack_for_api(paths: list[str], outdir: str, limit: int = API_MAX_REFS) -> list[str]:
    """xAI /images/edits принимает до 5 картинок. Если фото больше — первые limit-1 идут как есть,
    остальные склеиваются в одну коллаж-картинку (последний слот)."""
    if len(paths) <= limit:
        return list(paths)
    head, rest = list(paths[:limit - 1]), paths[limit - 1:]
    os.makedirs(outdir, exist_ok=True)
    out = os.path.join(outdir, "collage.jpg")
    with open(out, "wb") as f:
        f.write(contact_sheet(rest, cell=512))
    return head + [out]
