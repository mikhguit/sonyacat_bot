"""Стиль текста на картинках: шрифты (OFL, с кириллицей), пресеты, эффекты, разбор стиля с референса (vision-JSON),
хранилище «мой стиль» и локальный рендер надписи поверх фото.

Не зависит от Telegram и сети — всё рисуется Pillow локально, поэтому тестируется офлайн.
Поле стиля со значением None означает «как обычно для этого режима» (карусель / мем / надпись), поэтому
стиль «по умолчанию» (None) рисуется ровно так же, как до появления этого модуля.
"""
from __future__ import annotations

import io
import json
import logging
import os
import re
from dataclasses import dataclass, field, fields

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

import insta

log = logging.getLogger("grok-bot.textstyle")

FONT_DIR = insta.FONT_DIR
FILE_NAME = "text_styles.json"
SAMPLE_TEXT = "Съешь ещё этих мягких булок"


# ---------------------------------------------------------------------------
# Шрифты (все — SIL OFL, лицензии лежат рядом: fonts/OFL-*.txt)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class FontSpec:
    key: str
    label: str           # название кнопки
    category: str        # sans | condensed | serif | script | retro_script | handwritten | cursive | display | rounded
    bold_file: str
    reg_file: str | None = None   # файл «обычного» начертания (если нет — то же, что bold)
    bold_w: int | None = None     # ось Weight для вариативных шрифтов
    reg_w: int | None = None
    body: str | None = None       # ключ шрифта для мелкого текста (None — тот же шрифт)
    hint: str = ""


FONTS: dict[str, FontSpec] = {f.key: f for f in (
    FontSpec("montserrat", "Montserrat", "sans", "Montserrat-ExtraBold.ttf", "Montserrat-Regular.ttf",
             hint="современный гротеск"),
    FontSpec("oswald", "Oswald", "condensed", "Oswald-Bold.ttf", body="montserrat", hint="узкий плакатный"),
    FontSpec("playfair", "Playfair Display", "serif", "PlayfairDisplay-VF.ttf", "PlayfairDisplay-VF.ttf", 800, 500,
             hint="элегантная антиква"),
    FontSpec("lobster", "Lobster", "script", "Lobster-Regular.ttf", body="montserrat", hint="жирный каллиграфический"),
    FontSpec("pacifico", "Pacifico", "retro_script", "Pacifico-Regular.ttf", body="montserrat", hint="ретро-вывеска"),
    FontSpec("caveat", "Caveat", "handwritten", "Caveat-VF.ttf", "Caveat-VF.ttf", 700, 500, hint="живой почерк"),
    FontSpec("marck", "Marck Script", "cursive", "MarckScript-Regular.ttf", body="montserrat", hint="перо, прописи"),
    FontSpec("badscript", "Bad Script", "cursive", "BadScript-Regular.ttf", body="montserrat", hint="тонкий рукописный"),
    FontSpec("russo", "Russo One", "display", "RussoOne-Regular.ttf", body="montserrat", hint="технологичный, широкий"),
    FontSpec("comfortaa", "Comfortaa", "rounded", "Comfortaa-VF.ttf", "Comfortaa-VF.ttf", 700, 400,
             hint="округлый, мягкий"),
)}
DEFAULT_FONT = "montserrat"
FONT_KEYS = tuple(FONTS)

# категория из vision-JSON → шрифт (болд, не болд)
CATEGORY_FONT = {
    "sans": ("montserrat", "montserrat"),
    "condensed": ("oswald", "oswald"),
    "serif": ("playfair", "playfair"),
    "script": ("lobster", "marck"),
    "retro_script": ("pacifico", "pacifico"),
    "handwritten": ("caveat", "caveat"),
    "cursive": ("marck", "badscript"),
    "display": ("russo", "russo"),
    "rounded": ("comfortaa", "comfortaa"),
}

_font_cache: dict[tuple, ImageFont.FreeTypeFont] = {}


def _load_ttf(file: str, size: int, wght: int | None) -> ImageFont.FreeTypeFont:
    key = (file, size, wght)
    f = _font_cache.get(key)
    if f is None:
        # BASIC — одинаково везде, не зависит от наличия libraqm на хостинге
        f = ImageFont.truetype(os.path.join(FONT_DIR, file), size, layout_engine=ImageFont.Layout.BASIC)
        if wght is not None:
            try:
                f.set_variation_by_axes([wght])
            except (OSError, AttributeError, ValueError) as e:  # нет поддержки вариаций — берём вес по умолчанию
                log.warning("variation %s: %s", file, e)
        if len(_font_cache) > 400:
            _font_cache.clear()
        _font_cache[key] = f
    return f


class StyledFont:
    """Шрифт с трекингом (межбуквенным интервалом), рисуем побуквенно — работает и без Raqm."""

    def __init__(self, base: ImageFont.FreeTypeFont, spacing: float):
        self.base, self.spacing, self.size = base, spacing, base.size

    def getlength(self, text: str) -> float:
        return self.base.getlength(text) + self.spacing * max(0, len(text) - 1)

    def getmetrics(self):
        return self.base.getmetrics()


@dataclass(frozen=True)
class FontRef:
    """Ссылка на шрифт стиля — insta.fit_block/_font принимают и путь, и такой объект."""
    key: str
    bold: bool = True
    spacing: int = 0   # сотые доли em

    def load(self, size: int):
        spec = FONTS.get(self.key) or FONTS[DEFAULT_FONT]
        base = _load_ttf(spec.bold_file if self.bold else (spec.reg_file or spec.bold_file), size,
                         spec.bold_w if self.bold else spec.reg_w)
        return StyledFont(base, size * self.spacing / 100.0) if self.spacing else base


def title_ref(st: "TextStyle") -> FontRef:
    return FontRef(st.font, st.bold, st.spacing)


def body_ref(st: "TextStyle") -> FontRef:
    spec = FONTS.get(st.font) or FONTS[DEFAULT_FONT]
    if spec.body:  # декоративный шрифт — мелкий текст читаемым гротеском
        return FontRef(spec.body, False, min(st.spacing, 6))
    return FontRef(st.font, False, min(st.spacing, 6))


def load_font(key: str, size: int, bold: bool = True, spacing: int = 0):
    return FontRef(key if key in FONTS else DEFAULT_FONT, bold, spacing).load(size)


_COVER_TEXT = SAMPLE_TEXT + " ЁёЪъЫыЭэЮюЯя 0123456789 .,!?«»—"


def glyphs_present(f: ImageFont.FreeTypeFont, text: str = _COVER_TEXT) -> bool:
    """Все ли символы текста нарисованы настоящими глифами (а не «.notdef»-квадратом)."""
    notdef = bytes(f.getmask("\uffff"))
    for ch in set(text):
        if ch.isspace():
            continue
        m = f.getmask(ch)
        if m.getbbox() is None or bytes(m) == notdef:
            return False
    return True


def strip_missing(f: ImageFont.FreeTypeFont, text: str) -> tuple[str, str]:
    """Убрать символы, которых нет в шрифте (эмодзи, редкие значки) — иначе вместо них рисуется квадрат.
    Возвращает (чистый текст, убранные символы)."""
    f = getattr(f, "base", f)
    notdef = bytes(f.getmask("\uffff"))
    keep, drop = [], []
    for ch in text:
        if ch.isspace() or ch == "\n":
            keep.append(ch)
            continue
        m = f.getmask(ch)
        (drop if (m.getbbox() is None or bytes(m) == notdef) else keep).append(ch)
    clean = " ".join("".join(keep).split())
    return clean, "".join(dict.fromkeys(drop))


def font_covers(key: str, text: str = _COVER_TEXT) -> bool:
    spec = FONTS[key]
    return all(glyphs_present(_load_ttf(file, 48, w), text)
               for file, w in {(spec.bold_file, spec.bold_w), (spec.reg_file or spec.bold_file, spec.reg_w)})


# ---------------------------------------------------------------------------
# Палитра, размеры, позиции
# ---------------------------------------------------------------------------
PALETTE: tuple[tuple[str, str, str, str], ...] = (
    # key, подпись, HEX, английское название (для промптов Grok)
    ("white", "⚪ Белый", "#FFFFFF", "white"),
    ("black", "⚫ Чёрный", "#111111", "black"),
    ("yellow", "🟡 Жёлтый", "#FFD54A", "bright yellow"),
    ("gold", "🥇 Золотой", "#E8C872", "luxurious gold"),
    ("orange", "🟠 Оранжевый", "#FF8A3D", "orange"),
    ("red", "🔴 Красный", "#F03E3E", "red"),
    ("pink", "🌸 Розовый", "#FF6FB5", "hot pink"),
    ("purple", "🟣 Фиолетовый", "#9B6BFF", "purple"),
    ("blue", "🔵 Синий", "#3D8BFF", "blue"),
    ("cyan", "🩵 Голубой", "#38E1FF", "cyan"),
    ("green", "🟢 Зелёный", "#3DDC84", "green"),
    ("lime", "🍏 Лаймовый", "#C6F135", "lime"),
    ("cream", "🤍 Кремовый", "#F5E6C8", "cream"),
    ("gray", "🩶 Серый", "#B8BCC4", "light gray"),
)
PALETTE_HEX = {k: h for k, _, h, _ in PALETTE}
SNAP_DISTANCE = 70   # цвет с референса ближе этого к палитре «прилипает» к ней

SIZES = {"xs": ("Мелкий", 0.62), "s": ("Поменьше", 0.82), "m": ("Обычный", 1.0), "l": ("Крупный", 1.3),
         "xl": ("Огромный", 1.65)}
POSITIONS = {"top": "сверху", "center": "по центру", "bottom": "снизу"}
CASES = {"upper": "ЗАГЛАВНЫЕ", "lower": "строчные", "title": "Каждое Слово С Заглавной"}
EFFECTS = (("shadow", "Тень"), ("stroke", "Обводка"), ("glow", "Свечение"), ("plate", "Подложка"),
           ("gradient", "Затемнение"))


def hex_to_rgb(s: str | None, default=(255, 255, 255)) -> tuple[int, int, int]:
    s = (s or "").strip().lstrip("#")
    if len(s) == 3 and re.fullmatch(r"[0-9a-fA-F]{3}", s):
        s = "".join(c * 2 for c in s)
    if re.fullmatch(r"[0-9a-fA-F]{6}", s):
        return tuple(int(s[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]
    return default


def norm_hex(s: str | None) -> str | None:
    """'#fa0' / 'FFD54A' → '#FFAA00' / '#FFD54A'; мусор → None."""
    s = (s or "").strip().lstrip("#")
    if len(s) == 3 and re.fullmatch(r"[0-9a-fA-F]{3}", s):
        s = "".join(c * 2 for c in s)
    return "#" + s.upper() if re.fullmatch(r"[0-9a-fA-F]{6}", s) else None


def color_distance(a, b) -> float:
    # «взвешенная» евклидова: глаз чувствительнее к зелёному
    return (2 * (a[0] - b[0]) ** 2 + 4 * (a[1] - b[1]) ** 2 + 3 * (a[2] - b[2]) ** 2) ** 0.5 / 3 ** 0.5


def nearest_palette(rgb) -> tuple[str, str, float]:
    best = min(PALETTE, key=lambda p: color_distance(rgb, hex_to_rgb(p[2])))
    return best[0], best[2], color_distance(rgb, hex_to_rgb(best[2]))


def color_name_ru(hexv: str | None) -> str:
    if not hexv:
        return "как обычно"
    for k, label, h, _ in PALETTE:
        if h == hexv:
            return label.split(" ", 1)[1].lower()
    return hexv


def color_name_en(hexv: str | None) -> str:
    if not hexv:
        return ""
    for _, _, h, en in PALETTE:
        if h == hexv:
            return en
    return f"color {hexv}"


def luminance(rgb) -> float:
    return (0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2]) / 255


def mix(a, b, t: float):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


# ---------------------------------------------------------------------------
# Стиль
# ---------------------------------------------------------------------------
@dataclass
class TextStyle:
    font: str = DEFAULT_FONT
    color: str | None = None          # '#RRGGBB'; None — как обычно (белый)
    size: str = "m"                   # xs | s | m | l | xl
    pos: str | None = None            # top | center | bottom; None — как обычно
    case: str | None = None           # upper | lower | title; None — как есть (у мемов — ЗАГЛАВНЫЕ)
    bold: bool = True
    spacing: int = 0                  # межбуквенный интервал, сотые доли em (0..30)
    shadow: bool | None = None        # None — как обычно для режима
    stroke: bool | None = None
    glow: bool | None = None
    plate: bool | None = None
    gradient: bool | None = None      # затемнение под текстом (режим «надпись»)

    def to_dict(self) -> dict:
        return {f.name: getattr(self, f.name) for f in fields(self) if getattr(self, f.name) is not None}

    def key(self) -> tuple:
        return tuple(getattr(self, f.name) for f in fields(self))


def from_dict(d: dict | None) -> TextStyle:
    """Безопасная сборка из словаря (JSON из хранилища / восстановления): всё неизвестное отбрасывается."""
    d = d if isinstance(d, dict) else {}
    st = TextStyle()
    if d.get("font") in FONTS:
        st.font = d["font"]
    st.color = norm_hex(d.get("color")) if isinstance(d.get("color"), str) else None
    if d.get("size") in SIZES:
        st.size = d["size"]
    if d.get("pos") in POSITIONS:
        st.pos = d["pos"]
    if d.get("case") in CASES:
        st.case = d["case"]
    st.bold = bool(d["bold"]) if isinstance(d.get("bold"), bool) else True
    try:
        st.spacing = max(0, min(30, int(d.get("spacing") or 0)))
    except (TypeError, ValueError):
        st.spacing = 0
    for k in ("shadow", "stroke", "glow", "plate", "gradient"):
        v = d.get(k)
        setattr(st, k, v if isinstance(v, bool) else None)
    return st


@dataclass
class Fx:
    shadow: bool
    stroke: bool
    glow: bool
    plate: bool
    gradient: bool
    case: str | None
    color: tuple[int, int, int]
    size_k: float


# значения «по умолчанию» для режима: slide (карусель/сторис без фото), overlay (карусель по фото),
# meme, caption (надпись на картинке)
_MODE_DEFAULTS = {
    "slide":   dict(shadow=False, stroke=False, glow=False, plate=True, gradient=False, case=None),
    "overlay": dict(shadow=True, stroke=False, glow=False, plate=False, gradient=False, case=None),
    "meme":    dict(shadow=False, stroke=True, glow=False, plate=False, gradient=False, case="upper"),
    "caption": dict(shadow=True, stroke=False, glow=False, plate=False, gradient=False, case=None),
}


def resolve(st: TextStyle, mode: str) -> Fx:
    dflt = _MODE_DEFAULTS[mode]
    g = lambda k: getattr(st, k) if getattr(st, k) is not None else dflt[k]
    return Fx(shadow=g("shadow"), stroke=g("stroke"), glow=g("glow"), plate=g("plate"), gradient=g("gradient"),
              case=st.case or dflt["case"], color=hex_to_rgb(st.color), size_k=SIZES[st.size][1])


def apply_case(text: str, case: str | None) -> str:
    if case == "upper":
        return text.upper()
    if case == "lower":
        return text.lower()
    if case == "title":
        return re.sub(r"(?<![\w'’])(\w)(\w*)", lambda m: m.group(1).upper() + m.group(2).lower(), text)
    return text


def stroke_color(fx: Fx) -> tuple[int, int, int]:
    return (0, 0, 0) if luminance(fx.color) > 0.42 else (255, 255, 255)


def plate_fill(fx: Fx) -> tuple[int, int, int, int]:
    return (10, 10, 14, 170) if luminance(fx.color) > 0.42 else (255, 255, 255, 215)


def describe(st: TextStyle, mode: str = "overlay") -> str:
    """Короткое русское описание стиля («понял стиль так: …»)."""
    fx = resolve(st, mode)
    spec = FONTS[st.font]
    parts = [f"шрифт {spec.label} ({spec.hint})" + ("" if st.bold or not spec.reg_file else ", обычное начертание"),
             f"цвет {color_name_ru(st.color) if st.color else 'белый'}" + (f" ({st.color})" if st.color and color_name_ru(st.color) != st.color else ""),
             f"размер {SIZES[st.size][0].lower()}"]
    parts.append("регистр " + CASES[st.case] if st.case else "регистр как в тексте")
    if st.pos:
        parts.append("положение " + POSITIONS[st.pos])
    if st.spacing:
        parts.append("разрежённые буквы" if st.spacing >= 8 else "слегка разрежённые буквы")
    on = [lbl.lower() for k, lbl in EFFECTS if (getattr(st, k) if getattr(st, k) is not None else getattr(fx, k))]
    parts.append("эффекты: " + (", ".join(on) if on else "нет"))
    return "; ".join(parts)


# ---------------------------------------------------------------------------
# Готовые стили
# ---------------------------------------------------------------------------
PRESETS: dict[str, tuple[str, TextStyle]] = {
    "classic": ("Классика", TextStyle("montserrat", None, shadow=True)),
    "neon": ("Неон", TextStyle("comfortaa", "#38E1FF", shadow=False, glow=True)),
    "magazine": ("Журнал", TextStyle("playfair", "#FFFFFF", shadow=True)),
    "gold": ("Золото", TextStyle("playfair", "#E8C872", case="upper", spacing=8, shadow=True)),
    "hand": ("Почерк", TextStyle("caveat", "#FFFFFF", size="l", shadow=True)),
    "retro": ("Ретро-вывеска", TextStyle("pacifico", "#FFD54A", stroke=True, shadow=True)),
    "bold": ("Плакат", TextStyle("oswald", "#FFFFFF", case="upper", stroke=True, shadow=False)),
    "tech": ("Техно", TextStyle("russo", "#C6F135", case="upper", spacing=4, shadow=True)),
    "pen": ("Перо", TextStyle("marck", "#F5E6C8", size="l", shadow=True)),
    "plate": ("На плашке", TextStyle("montserrat", "#FFFFFF", plate=True, shadow=False)),
}


def preset(key: str) -> TextStyle | None:
    p = PRESETS.get(key)
    return from_dict(p[1].to_dict()) if p else None


# ---------------------------------------------------------------------------
# Разбор стиля с референса (vision → JSON → ближайшие пресеты)
# ---------------------------------------------------------------------------
VISION_NAME = "textstyle"
_CATS = list(CATEGORY_FONT)
_WEIGHTS = ["light", "regular", "bold", "black"]
_SPACING = {"tight": 0, "normal": 0, "wide": 10}
_SIZE_WORD = {"small": "s", "medium": "m", "large": "l", "huge": "xl"}


def vision_schema() -> dict:
    props = {
        "has_text": {"type": "boolean", "description": "Is there any lettering/inscription in the image"},
        "font_category": {"type": "string", "enum": _CATS, "description": "Closest lettering class"},
        "weight": {"type": "string", "enum": _WEIGHTS},
        "color": {"type": "string", "description": "Main fill color of the letters as #RRGGBB"},
        "case": {"type": "string", "enum": ["upper", "lower", "title", "as_is"]},
        "position": {"type": "string", "enum": ["top", "center", "bottom", "unknown"]},
        "size": {"type": "string", "enum": list(_SIZE_WORD)},
        "letter_spacing": {"type": "string", "enum": list(_SPACING)},
        "stroke": {"type": "boolean", "description": "Letters have a contrasting outline"},
        "shadow": {"type": "boolean", "description": "Letters have a drop shadow"},
        "glow": {"type": "boolean", "description": "Letters glow (neon)"},
        "plate": {"type": "boolean", "description": "Text sits on a solid/semi-transparent plate or banner"},
        "description_ru": {"type": "string", "description": "По-русски, до 20 слов: как выглядит надпись"},
    }
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


def vision_messages(image_uri: str, detail: str = "high") -> list[dict]:
    text = (
        "Look ONLY at the lettering/typography in this image (ignore what the picture shows). Describe the style of "
        "the most prominent text so it can be reproduced with a limited set of fonts. Pick the closest "
        "font_category: sans (geometric/grotesque), condensed (narrow poster), serif, script (bold calligraphic), "
        "retro_script (rounded retro signage script), handwritten (casual marker/pen), cursive (thin copybook "
        "script), display (wide techno/heavy decorative), rounded (soft rounded sans). Give the main fill color as "
        "#RRGGBB, case of the text, vertical position, relative size, letter spacing, and effects (stroke, shadow, "
        "glow, plate). If there is no text, set has_text=false and give your best guess for a fitting style. "
        "Reply only with JSON by the schema.")
    return [{"role": "user", "content": [{"type": "input_image", "image_url": image_uri, "detail": detail},
                                         {"type": "input_text", "text": text}]}]


def style_from_json(data: dict) -> tuple[TextStyle, list[str]]:
    """Проверка и маппинг JSON от модели на наши пресеты. Ничего невозможного не придумываем:
    неизвестные значения отбрасываются, цвет прилипает к палитре, шрифт — к ближайшей категории."""
    notes: list[str] = []
    data = data if isinstance(data, dict) else {}
    cat = data.get("font_category")
    if cat not in CATEGORY_FONT:
        notes.append("шрифт не распознан — взят Montserrat")
        cat = "sans"
    weight = data.get("weight") if data.get("weight") in _WEIGHTS else "bold"
    bold = weight in ("bold", "black")
    font = CATEGORY_FONT[cat][0 if bold else 1]
    spec = FONTS[font]
    st = TextStyle(font=font, bold=bold or not spec.reg_file)

    raw = data.get("color")
    hx = norm_hex(raw) if isinstance(raw, str) else None
    if hx:
        key, ph, dist = nearest_palette(hex_to_rgb(hx))
        if dist <= SNAP_DISTANCE:
            hx = ph
        else:
            notes.append(f"цвет {hx} вне палитры — оставлен как есть")
        st.color = None if hx == "#FFFFFF" else hx
    else:
        notes.append("цвет не распознан — белый")

    case = data.get("case")
    st.case = case if case in CASES else None
    pos = data.get("position")
    st.pos = pos if pos in POSITIONS else None
    st.size = _SIZE_WORD.get(data.get("size"), "m")
    st.spacing = _SPACING.get(data.get("letter_spacing"), 0)
    for k in ("stroke", "shadow", "glow", "plate"):
        v = data.get(k)
        setattr(st, k, v if isinstance(v, bool) else None)
    if st.glow:
        st.shadow = False if st.shadow is None else st.shadow
    if st.plate and st.color and luminance(hex_to_rgb(st.color)) < 0.2 and st.plate:
        pass  # тёмный текст на плашке — плашка станет светлой (plate_fill), всё читаемо
    if data.get("has_text") is False:
        notes.append("на картинке нет надписи — подобран подходящий стиль")
    return st, notes


def style_summary_ru(st: TextStyle, data: dict | None = None, mode: str = "overlay") -> str:
    d = ""
    if data and isinstance(data.get("description_ru"), str) and data["description_ru"].strip():
        d = " ".join(data["description_ru"].split())[:220] + "\n"
    return d + describe(st, mode)


# ---------------------------------------------------------------------------
# «Мой стиль» — хранилище (JSON в DATA_DIR) + код для восстановления
# ---------------------------------------------------------------------------
class StyleStore:
    """Личный стиль каждого пользователя. На бесплатном bothost файл стирается при рестарте, поэтому
    /mystyle отдаёт короткий код, который можно вернуть командой /mystyle <код>."""

    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self.path = os.path.join(data_dir, FILE_NAME)
        self.persist_ok = True
        self.styles: dict[int, dict] = {}
        self._load()

    def _load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as f:
                d = json.load(f)
            self.styles = {int(k): from_dict(v).to_dict() for k, v in (d.get("styles") or {}).items()}
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError, AttributeError) as e:
            log.warning("Не удалось прочитать %s (%s) — сохранённых стилей нет", self.path, e)

    def save(self) -> bool:
        tmp = self.path + ".tmp"
        try:
            os.makedirs(self.data_dir, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"styles": {str(k): v for k, v in self.styles.items()}}, f, ensure_ascii=False, indent=1)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
            self.persist_ok = True
        except OSError as e:
            if self.persist_ok:
                log.warning("Не могу сохранить %s (%s) — стили только в памяти", self.path, e)
            self.persist_ok = False
            try:
                os.remove(tmp)
            except OSError:
                pass
        return self.persist_ok

    def get(self, uid: int) -> TextStyle | None:
        d = self.styles.get(uid)
        return from_dict(d) if d else None

    def set(self, uid: int, st: TextStyle) -> bool:
        self.styles[uid] = st.to_dict()
        return self.save()

    def reset(self, uid: int) -> bool:
        had = self.styles.pop(uid, None) is not None
        if had:
            self.save()
        return had


def export_code(st: TextStyle) -> str:
    return json.dumps(st.to_dict(), ensure_ascii=False, separators=(",", ":"))


def import_code(raw: str) -> TextStyle | None:
    raw = (raw or "").strip().strip("`")
    if not raw.startswith("{"):
        return None
    try:
        d = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(d, dict) or not d:
        return None
    return from_dict(d)


# ---------------------------------------------------------------------------
# Рисование текста с эффектами
# ---------------------------------------------------------------------------
def _raw_text(draw: ImageDraw.ImageDraw, xy, text: str, font, fill, stroke_width: int = 0, stroke_fill=None) -> None:
    if isinstance(font, StyledFont):
        x, y = xy
        for ch in text:
            draw.text((x, y), ch, font=font.base, fill=fill, stroke_width=stroke_width, stroke_fill=stroke_fill)
            x += font.base.getlength(ch) + font.spacing
    else:
        draw.text(xy, text, font=font, fill=fill, stroke_width=stroke_width, stroke_fill=stroke_fill)


def _composite(img: Image.Image, layer: Image.Image, origin: tuple[int, int]) -> None:
    """Наложить RGBA-слой на img (RGB или RGBA) в точке origin, с обрезкой по границам."""
    x, y = origin
    W, H = img.size
    lx0, ly0 = max(0, -x), max(0, -y)
    lx1, ly1 = min(layer.width, W - x), min(layer.height, H - y)
    if lx1 <= lx0 or ly1 <= ly0:
        return
    part = layer.crop((lx0, ly0, lx1, ly1))
    box = (x + lx0, y + ly0)
    if img.mode == "RGBA":
        region = img.crop(box + (box[0] + part.width, box[1] + part.height))
        region.alpha_composite(part)
        img.paste(region, box)
    else:
        img.paste(part.convert("RGB"), box, part.getchannel("A"))


def _line_box(line: str, xy, font, pad: int) -> tuple[int, int, int, int]:
    asc, desc = font.getmetrics()
    x, y = xy
    return (int(x) - pad, int(y) - pad, int(x + font.getlength(line)) + pad, int(y + asc + desc) + pad)


def draw_line(img: Image.Image, xy, line: str, font, fx: Fx, fill=None, shadow_strength: float = 1.0) -> None:
    """Одна строка текста с тенью / свечением / обводкой. xy — левый верх строки."""
    if not line:
        return
    size = font.size
    rgb = fill or fx.color
    x, y = int(xy[0]), int(xy[1])
    if fx.glow:
        pad = int(size * 0.8) + 4
        bx0, by0, bx1, by1 = _line_box(line, (x, y), font, pad)
        layer = Image.new("RGBA", (bx1 - bx0, by1 - by0), rgb + (0,))
        _raw_text(ImageDraw.Draw(layer), (x - bx0, y - by0), line, font, rgb + (255,),
                  stroke_width=max(1, size // 28), stroke_fill=rgb + (255,))
        blur = layer.filter(ImageFilter.GaussianBlur(max(2, size * 0.14)))
        alpha = blur.getchannel("A").point(lambda v: min(255, int(v * 2.4)))
        glow = Image.new("RGBA", layer.size, rgb + (0,))
        glow.putalpha(alpha)
        _composite(img, glow, (bx0, by0))
        _composite(img, glow, (bx0, by0))
        core = mix(rgb, (255, 255, 255), 0.55)
    else:
        core = rgb
    if fx.shadow:
        pad = int(size * 0.35) + 4
        dx, dy = max(2, int(size * 0.045)), max(2, int(size * 0.065))
        bx0, by0, bx1, by1 = _line_box(line, (x, y), font, pad)
        bx1 += dx
        by1 += dy
        layer = Image.new("RGBA", (bx1 - bx0, by1 - by0), (0, 0, 0, 0))
        a = int(min(255, 185 * shadow_strength))
        _raw_text(ImageDraw.Draw(layer), (x - bx0 + dx, y - by0 + dy), line, font, (0, 0, 0, a))
        layer = layer.filter(ImageFilter.GaussianBlur(max(1, size * 0.03)))
        _composite(img, layer, (bx0, by0))
    d = ImageDraw.Draw(img, "RGBA")
    if fx.stroke:
        sw = max(2, size // 14)
        _raw_text(d, (x, y), line, font, core, stroke_width=sw, stroke_fill=stroke_color(fx) + (255,))
    else:
        _raw_text(d, (x, y), line, font, core + (255,))


def _vgrad_band(w: int, h: int, alpha_max: int, edge: str) -> Image.Image:
    """Тёмная полоса, плавно исчезающая с одного края ('top' — прозрачна сверху, 'bottom' — снизу, 'both')."""
    col = Image.new("L", (1, h), 0)
    for yy in range(h):
        t = yy / max(1, h - 1)
        if edge == "top":
            k = t
        elif edge == "bottom":
            k = 1 - t
        else:
            k = 1 - abs(2 * t - 1)
        col.putpixel((0, yy), int(alpha_max * (k ** 1.4)))
    out = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    out.putalpha(col.resize((w, h)))
    return out


LAST_CAPTION: dict = {}   # геометрия последнего рендера надписи (для тестов)


def render_caption(photo: Image.Image, text: str, st: TextStyle, max_side: int = 4096) -> Image.Image:
    """Нарисовать надпись поверх фото в его исходном разрешении. Всё, что вне зоны под текстом, остаётся
    пиксель-в-пиксель как в оригинале (LAST_CAPTION['touched'] — изменённые области)."""
    img = ImageOps.exif_transpose(photo) if hasattr(photo, "getexif") else photo
    img = img.convert("RGB")
    if max(img.size) > max_side:
        img.thumbnail((max_side, max_side), Image.LANCZOS)
    W, H = img.size
    fx = resolve(st, "caption")
    text = apply_case(text.strip(), fx.case)
    margin = int(W * 0.06)
    pad = int(min(W, H) * 0.035) if fx.plate else 0
    max_w = W - 2 * margin - 2 * pad
    base = min(W, H) * 0.085 * fx.size_k
    ref = title_ref(st)
    text, dropped = strip_missing(ref.load(48), text)
    size = max(14, int(base))
    max_h = int(H * (0.5 if fx.size_k > 1.2 else 0.42))
    while True:
        font = ref.load(size)
        lines = insta.wrap_text(text, font, max_w) if text else []
        lh = int(size * 1.18)
        if size > 14 and text and insta.has_overlong_word(text, font, max_w):
            size -= 2
            continue
        if lh * len(lines) <= max_h or size <= 14:
            if lh * len(lines) > max_h:
                keep = max(1, max_h // lh)
                lines = lines[:keep]
                lines[-1] = lines[-1].rstrip(" .,;:") + "…"
            break
        size -= 2
    block_h = lh * len(lines)
    pos = st.pos or "bottom"
    if pos == "top":
        y0 = margin // 2 + pad + int(H * 0.02)
    elif pos == "center":
        y0 = (H - block_h) // 2
    else:
        y0 = H - block_h - pad - int(H * 0.04) - margin // 2
    y0 = max(pad, y0)
    touched: list[tuple[int, int, int, int]] = []
    if fx.gradient and not fx.plate:
        gh = min(H, block_h + int(H * 0.30))
        if pos == "top":
            gy0, edge = 0, "bottom"
        elif pos == "bottom":
            gy0, edge = H - gh, "top"
        else:
            gy0, edge = max(0, y0 - int(H * 0.12)), "both"
            gh = min(H - gy0, block_h + int(H * 0.24))
        band = _vgrad_band(W, gh, 190, edge)
        _composite(img, band, (0, gy0))
        touched.append((0, gy0, W, gy0 + gh))
    widths = [font.getlength(ln) for ln in lines] or [0]
    if fx.plate:
        bw = int(max(widths)) + 2 * pad
        bx0 = (W - bw) // 2
        box = (bx0, y0 - pad, bx0 + bw, y0 + block_h + pad)
        layer = Image.new("RGBA", (box[2] - box[0], box[3] - box[1]), (0, 0, 0, 0))
        ImageDraw.Draw(layer).rounded_rectangle((0, 0) + layer.size, radius=int(min(W, H) * 0.03), fill=plate_fill(fx))
        _composite(img, layer, (box[0], box[1]))
        touched.append(box)
    y = y0
    for ln, w in zip(lines, widths):
        draw_line(img, ((W - w) / 2, y + (lh - size) // 4), ln, font, fx, shadow_strength=1.2)
        y += lh
    pad_t = int(size * 0.9) + 6
    touched.append((max(0, margin - pad_t), max(0, y0 - pad_t), min(W, W - margin + pad_t), min(H, y0 + block_h + pad_t)))
    LAST_CAPTION.clear()
    LAST_CAPTION.update(size=size, lines=lines, text_box=(margin, y0, W - margin, y0 + block_h), touched=touched,
                        pos=pos, font_size=size, img_size=(W, H), dropped=dropped)
    return img


def sample_photo(size=(960, 640), seed: int = 2) -> Image.Image:
    return insta.placeholder_background(size, seed).convert("RGB")


def render_preview(st: TextStyle, text: str = SAMPLE_TEXT, size=(960, 540)) -> bytes:
    """Превью стиля на нейтральном фоне — показываем перед «Применить»."""
    img = render_caption(sample_photo(size, 1), text, TextStyle(**{**st.__dict__, "pos": st.pos or "center"}))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=88)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Описание стиля для Grok (способ «Нарисует Grok»)
# ---------------------------------------------------------------------------
AI_STYLES: dict[str, tuple[str, str]] = {
    "neon": ("💡 Неон", "glowing neon-tube lettering with a soft bright glow"),
    "hand": ("🖋 Рукописный", "elegant hand-written brush calligraphy lettering"),
    "3d": ("🧊 3D", "bold glossy 3D extruded letters with depth and soft studio lighting"),
    "gold": ("🥇 Золото", "luxurious polished gold metallic lettering with highlights"),
    "graffiti": ("🎨 Граффити", "colorful street-art graffiti lettering with outline"),
    "retro": ("📺 Ретро", "retro vintage signboard lettering with thick outline and drop shadow"),
    "minimal": ("▫️ Минимал", "clean modern minimalist sans-serif lettering, flat, perfectly legible"),
    "chalk": ("🖍 Мел", "hand-drawn chalk-style lettering with a slightly dusty texture"),
}
AI_COLORS = (("auto", "✨ Как лучше"),) + tuple((k, lbl) for k, lbl, _, _ in PALETTE if k in
                                                ("white", "black", "yellow", "gold", "red", "pink", "cyan", "green"))
AI_POS = {"top": "сверху", "center": "по центру", "bottom": "снизу", "keep": "как было"}


def ai_caption_prompt(mode: str, text: str, style_desc: str = "", color: str = "auto", pos: str = "bottom",
                      with_style_ref: bool = False) -> str:
    """Промпт для /images/edits. mode: add | replace | remove."""
    keep = ("Keep everything else in the image exactly the same: the subject, composition, colors, lighting and "
            "background must not change. Do not crop or resize.")
    if mode == "remove":
        return ("Remove all text, lettering, captions, logos and watermarks from this image and reconstruct what is "
                "behind them naturally. " + keep)
    cl = "" if color == "auto" else f" The letters are {color_name_en(PALETTE_HEX.get(color)) or color}."
    where = {"top": " Place the text near the top.", "center": " Place the text in the center.",
             "bottom": " Place the text near the bottom.", "keep": " Put it exactly where the old text was."
             if mode == "replace" else ""}.get(pos, "")
    if with_style_ref:
        look = (" Draw the letters in the same lettering style as in the SECOND image (font shape, weight, color, "
                "outline, glow) — copy only the style of its lettering, never its content.")
        if style_desc:
            look += f" Additional style notes: {style_desc}."
    else:
        look = f" Lettering style: {style_desc or 'clean legible lettering with a subtle shadow'}."
    spell = (f' The text must read EXACTLY "{text}" (Cyrillic, spelled letter by letter, no typos, no extra words).')
    if mode == "replace":
        head = ("Replace the existing text/inscription in the (first) image with the new text. Remove the old "
                "lettering completely and rebuild the background behind it.")
    else:
        head = "Add a text inscription to the (first) image."
    return f"{head}{spell}{look}{cl}{where} No other text anywhere. {keep}"


def materialize(st: TextStyle, mode: str) -> TextStyle:
    """Копия стиля, где «как обычно» заменено реальными значениями режима (для честного превью)."""
    fx = resolve(st, mode)
    d = dict(st.__dict__)
    d.update(shadow=fx.shadow, stroke=fx.stroke, glow=fx.glow, plate=fx.plate, gradient=fx.gradient,
             case=fx.case)
    return TextStyle(**d)


_FONT_SHEET: bytes | None = None


def render_font_sheet(text: str = SAMPLE_TEXT) -> bytes:
    """Картинка-образец: каждый шрифт своей фразой (для выбора кнопками)."""
    global _FONT_SHEET
    if _FONT_SHEET:
        return _FONT_SHEET
    W, rowh = 1000, 132
    img = Image.new("RGB", (W, rowh * len(FONTS) + 16), (22, 24, 32))
    d = ImageDraw.Draw(img)
    cap = load_font(DEFAULT_FONT, 22, bold=False)
    y = 12
    for key, spec in FONTS.items():
        d.text((28, y + 6), f"{spec.label} — {spec.hint}", font=cap, fill=(150, 156, 172))
        size = 54
        f = load_font(key, size, bold=True)
        while f.getlength(text) > W - 56 and size > 20:
            size -= 2
            f = load_font(key, size, bold=True)
        d.text((28, y + 46), text, font=f, fill=(245, 245, 250))
        y += rowh
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=88)
    _FONT_SHEET = buf.getvalue()
    return _FONT_SHEET
