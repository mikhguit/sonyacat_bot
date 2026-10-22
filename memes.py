"""Мемы с милыми животными: список зверей, ТЗ для Grok, рендер классического мема (Pillow)."""
from __future__ import annotations

import random
import re
from dataclasses import dataclass

from PIL import Image, ImageDraw, ImageFilter

import insta

FONT_MEME = insta.os.path.join(insta.FONT_DIR, "Oswald-Bold.ttf")  # Impact-подобный, OFL, с кириллицей
MEME_MAX = 5


@dataclass(frozen=True)
class Animal:
    key: str
    name: str           # для кнопки
    prompt: str         # описание для генерации картинки (англ.)
    vibe: str = ""      # характер мемного образа — подсказка для юмора
    stems: tuple[str, ...] = ()  # как пользователь может написать зверя

    def matches(self, word: str) -> bool:
        w = word.lower().strip(",.!?:;«»\"'")
        return w == self.key or any(w.startswith(s) for s in self.stems)


# Список по результатам веб-исследования (октябрь 2026) — см. README, раздел «Мемы».
DEFAULT_ANIMALS: tuple[Animal, ...] = (
    Animal("cat", "🐱 Котик", "a fluffy domestic cat with a very expressive face",
           "драматичный, осуждающий, ленивый", ("кот", "кош", "cat")),
    Animal("capybara", "🦫 Капибара", "a calm capybara",
           "невозмутимый дзен, «мне всё равно, я в тёплой воде»", ("капибар", "capy")),
    Animal("raccoon", "🦝 Енот", "a round chubby raccoon",
           "хаотичный, мелкий воришка, бежит по делам", ("енот", "енотик", "racc")),
    Animal("shiba", "🐕 Шиба-ину", "a shiba inu dog with a side-eye look",
           "классика doge, подозрительный взгляд", ("шиб", "сиба", "shiba", "doge", "доге")),
    Animal("quokka", "😊 Квокка", "a smiling quokka",
           "вечно улыбается, наивный оптимист", ("квокк", "quokk")),
    Animal("otter", "🦦 Выдра", "a cute sea otter with tiny paws",
           "маленькие лапки, «я заслужил вкусняшку»", ("выдр", "калан", "otter")),
    Animal("hamster", "🐹 Хомяк", "a tiny fluffy hamster with puffed cheeks",
           "щёки, снеки, перегруз", ("хомя", "hamst")),
    Animal("pygmy_hippo", "🦛 Бегемотик", "a baby pygmy hippo, wet and shiny",
           "капризный малыш, громко возмущается", ("бегемот", "гиппо", "hippo", "мудэнг", "mooden")),
    Animal("penguin", "🐧 Пингвин", "a small penguin walking alone",
           "уходит в закат от всех дел, экзистенциально", ("пингв", "pengu")),
    Animal("macaque", "🐒 Обезьянка", "a baby japanese macaque hugging a plush toy",
           "трогательный, обнимает игрушку, ищет поддержки", ("обезьян", "макак", "monkey")),
)


def load_animals(spec: str = "") -> list[Animal]:
    """MEME_ANIMALS: пусто — все по умолчанию; иначе ключи через запятую и/или свои
    «Название:english description» (например «cat,capybara,🦊 Лиса:a fluffy red fox»)."""
    if not spec.strip():
        return list(DEFAULT_ANIMALS)
    by_key = {a.key: a for a in DEFAULT_ANIMALS}
    out: list[Animal] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if part.lower() in by_key:
            out.append(by_key[part.lower()])
        elif ":" in part:
            name, desc = (x.strip() for x in part.split(":", 1))
            if name and desc:
                stem = re.sub(r"[^\w]", "", name.lower())[:5]
                key = "c" + str(len(out)) + "_" + re.sub(r"[^a-z0-9]", "", desc.lower())[:12]
                out.append(Animal(key[:20], name, desc, "", (stem,) if stem else ()))
    return out or list(DEFAULT_ANIMALS)


def find_animal(animals: list[Animal], word: str) -> Animal | None:
    for a in animals:
        if a.matches(word):
            return a
    return None


@dataclass
class MemeArgs:
    animal: Animal | None
    topic: str
    n: int
    story: bool
    error: str | None = None


def parse_meme_args(raw: str, animals: list[Animal], default_n: int = 1) -> MemeArgs:
    story, n, animal, rest = False, default_n, None, []
    for w in (raw or "").split():
        lw = w.lower().strip(",.")
        if lw in ("story", "сторис", "стори", "9:16", "вертикально", "-story", "--story"):
            story = True
        elif re.fullmatch(r"\d{1,2}", lw) and not rest:
            n = int(lw)
        elif animal is None and not rest and (a := find_animal(animals, lw)):
            animal = a
        elif lw in ("случайный", "random", "любой") and animal is None and not rest:
            animal = random.choice(animals)
        else:
            rest.append(w)
    if not 1 <= n <= MEME_MAX:
        return MemeArgs(animal, "", n, story, f"Можно от 1 до {MEME_MAX} мемов за раз.")
    return MemeArgs(animal, " ".join(rest).strip(), n, story)


# ---------------------------------------------------------------------------
# ТЗ для Grok
# ---------------------------------------------------------------------------
def meme_schema() -> dict:
    item = {
        "type": "object",
        "properties": {
            "top": {"type": "string", "description": "Верхний текст, до 8 слов (может быть пустым)"},
            "bottom": {"type": "string", "description": "Нижний текст / панчлайн, до 8 слов"},
            "scene": {"type": "string", "description": "Сцена для картинки на английском, без текста"},
        },
        "required": ["top", "bottom", "scene"],
        "additionalProperties": False,
    }
    return {"type": "object", "properties": {"memes": {"type": "array", "items": item}},
            "required": ["memes"], "additionalProperties": False}


_RULES = (
    "Юмор узнаваемый и добрый: работа, учёба, понедельник, дедлайны, быт, отношения с будильником. "
    "Без политики, оскорблений, мата и шуток над внешностью/группами людей. "
    "Текст короткий, разговорный, как в настоящих мемах; формат — верх/низ (setup → панчлайн) "
    "или однострочник (тогда top пустой). Без эмодзи и хэштегов. Не повторяй избитые шутки."
)


def meme_messages(animal: Animal, topic: str, n: int, story: bool, video: bool = False,
                  photo_note: str = "") -> list[dict]:
    system = "Ты — автор вирусных русскоязычных мемов для Instagram и Telegram. Отвечай только JSON по схеме."
    user = (
        f"Сделай {n} {'разных мемов' if n > 1 else 'мем'} с животным: {animal.name.split(' ', 1)[-1]} "
        f"({animal.prompt}). Мемный образ: {animal.vibe or 'милый и смешной'}.\n"
        f"Тема/ситуация: {topic or 'на твой выбор, жизненная ситуация'}.\n{_RULES}\n"
        f"scene — что делает животное и где (на английском, 1–2 предложения, смешная поза/выражение, "
        f"{'вертикальный кадр' if story else 'квадратный кадр'}), без надписей.\n"
        + ("Это сценарий для короткого видео 5–10 секунд: в scene опиши действие в динамике и камеру; "
           "top/bottom — подпись к видео.\n" if video else "")
        + (photo_note + "\n" if photo_note else "")
        + 'Формат: {"memes":[{"top":"...","bottom":"...","scene":"..."}]}'
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def photo_animal(subject: str = "") -> Animal:
    """Псевдо-«зверь» для мема по фото пользователя (питомец, персонаж, товар)."""
    return Animal("photo", "📷 С вашего фото", subject or "the pet or character from the user's reference photo",
                  "герой фото пользователя — обыграй то, что на нём видно")


def recaption_messages(animal: Animal, topic: str, scene: str, previous: list[str]) -> list[dict]:
    system = "Ты — автор вирусных русскоязычных мемов. Отвечай только JSON по схеме."
    prev = "\n".join(f"- {p}" for p in previous[-6:]) or "—"
    user = (
        f"Картинка уже есть: {scene} (животное: {animal.prompt}). Тема: {topic or 'любая жизненная'}.\n"
        f"Придумай ДРУГУЮ подпись к этой же картинке, не похожую на прошлые:\n{prev}\n{_RULES}\n"
        'scene верни ту же. Формат: {"memes":[{"top":"...","bottom":"...","scene":"..."}]}'
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


@dataclass
class MemeText:
    top: str
    bottom: str
    scene: str

    @property
    def label(self) -> str:
        return " / ".join(x for x in (self.top, self.bottom) if x)


def normalize_memes(data: dict, n: int, animal: Animal) -> list[MemeText]:
    out = []
    for m in (data.get("memes") or [])[:n]:
        if not isinstance(m, dict):
            continue
        top = insta.clean_slide_text(str(m.get("top") or ""))
        bottom = insta.clean_slide_text(str(m.get("bottom") or ""))
        if not bottom and top:
            top, bottom = "", top
        if not bottom:
            continue
        out.append(MemeText(top, bottom, str(m.get("scene") or "").strip() or f"{animal.prompt} looking funny"))
    return out


def image_prompt(animal: Animal, scene: str, story: bool, style: str = "") -> str:
    parts = [scene, f"The main character is {animal.prompt}",
             "funny expressive meme photo, photorealistic, sharp focus, natural light",
             "vertical 9:16 composition" if story else "square composition",
             "keep the top and bottom of the frame relatively plain for caption text",
             style, "absolutely no text, no letters, no captions, no watermark"]
    return ". ".join(p.strip().rstrip(".") for p in parts if p and p.strip())


# ---------------------------------------------------------------------------
# Рендер
# ---------------------------------------------------------------------------
def _fit_upper(text: str, max_w: int, max_h: int, start: int, minimum: int):
    text = text.upper()
    size = start
    while True:
        font = insta._font(FONT_MEME, size)
        sw = max(2, size // 13)
        lines = insta.wrap_text(text, font, max_w - 2 * sw) if text else []
        lh = int(size * 1.08)
        if (lh * len(lines) <= max_h and len(lines) <= 3) or size <= minimum:
            if lh * len(lines) > max_h:
                lines = lines[:max(1, max_h // lh)]
            return lines, font, lh, sw
        size -= 3


def render_meme(bg: Image.Image | None, top: str, bottom: str, story: bool = False,
                watermark: str = "", seed: int = 0) -> Image.Image:
    size = insta.STORY_SIZE if story else insta.CAROUSEL_SIZE
    W, H = size
    img = insta.fit_cover(bg, size) if bg is not None else placeholder_animal(size, seed)
    d = ImageDraw.Draw(img)
    margin = 40
    zone_top = insta.STORY_SAFE_TOP + 20 if story else 28
    zone_bottom = H - insta.STORY_SAFE_BOTTOM - 20 if story else H - 28
    max_h = int((zone_bottom - zone_top) * (0.30 if story else 0.34))
    start = 118 if not story else 124
    layout = {}
    if top:
        lines, font, lh, sw = _fit_upper(top, W - 2 * margin, max_h, start, 46)
        y = zone_top
        for ln in lines:
            w = font.getlength(ln)
            d.text(((W - w) / 2, y), ln, font=font, fill="white", stroke_width=sw, stroke_fill="black")
            y += lh
        layout["top"] = (zone_top, y)
    if bottom:
        lines, font, lh, sw = _fit_upper(bottom, W - 2 * margin, max_h, start, 46)
        y = zone_bottom - lh * len(lines) - int(lh * 0.12)
        layout["bottom"] = (y, zone_bottom)
        for ln in lines:
            w = font.getlength(ln)
            d.text(((W - w) / 2, y), ln, font=font, fill="white", stroke_width=sw, stroke_fill="black")
            y += lh
    if watermark:
        f = insta._font(insta.FONT_SEMI, 26)
        tw = f.getlength(watermark)
        wy = (H - insta.STORY_SAFE_BOTTOM + 12) if story else (H - 40)
        if not story and bottom:
            wy = layout["bottom"][0] - 36
        d.text((W - margin - tw, wy), watermark, font=f, fill=(255, 255, 255), stroke_width=2,
               stroke_fill=(0, 0, 0))
    LAST_LAYOUT.clear()
    LAST_LAYOUT.update(layout, size=size)
    return img


LAST_LAYOUT: dict = {}


def placeholder_animal(size: tuple[int, int], seed: int = 0) -> Image.Image:
    """Мультяшная мордочка на градиенте — заглушка для превью/тестов (без API)."""
    rnd = random.Random(seed)
    img = insta.placeholder_background(size, seed + 3).convert("RGB")
    W, H = size
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    cx, cy, r = W // 2, int(H * 0.52), int(min(W, H) * 0.30)
    fur = [(214, 150, 92), (160, 120, 90), (120, 120, 128), (230, 190, 140)][seed % 4]
    dark = tuple(max(0, c - 70) for c in fur)
    if seed % 2 == 0:  # кот: треугольные уши
        d.polygon([(cx - r, cy - r // 3), (cx - r // 2, cy - r - r // 2), (cx - r // 6, cy - r + 10)], fill=fur)
        d.polygon([(cx + r, cy - r // 3), (cx + r // 2, cy - r - r // 2), (cx + r // 6, cy - r + 10)], fill=fur)
    else:  # капибара/хомяк: круглые уши
        for sx in (-1, 1):
            d.ellipse((cx + sx * r * 0.7 - r * 0.22, cy - r * 1.0, cx + sx * r * 0.7 + r * 0.22, cy - r * 0.58), fill=dark)
    d.ellipse((cx - r, cy - r, cx + r, cy + r * 0.95), fill=fur)
    d.ellipse((cx - r * 0.55, cy + r * 0.05, cx + r * 0.55, cy + r * 0.75), fill=tuple(min(255, c + 40) for c in fur))
    for sx in (-1, 1):  # глаза
        ex, ey = cx + sx * r * 0.38, cy - r * 0.18
        d.ellipse((ex - r * 0.13, ey - r * 0.15, ex + r * 0.13, ey + r * 0.15), fill=(25, 20, 20))
        d.ellipse((ex - r * 0.04 + sx * 3, ey - r * 0.1, ex + r * 0.03 + sx * 3, ey - r * 0.02), fill="white")
    d.ellipse((cx - r * 0.1, cy + r * 0.12, cx + r * 0.1, cy + r * 0.25), fill=(60, 35, 35))
    d.arc((cx - r * 0.2, cy + r * 0.2, cx, cy + r * 0.42), 20, 160, fill=(60, 35, 35), width=5)
    d.arc((cx, cy + r * 0.2, cx + r * 0.2, cy + r * 0.42), 20, 160, fill=(60, 35, 35), width=5)
    shadow = layer.filter(ImageFilter.GaussianBlur(18))
    img.paste((0, 0, 0), (12, 18), shadow.split()[3].point(lambda a: a // 3))
    img.paste(layer, (0, 0), layer)
    return img


def video_prompt(animal: Animal, scene: str) -> str:
    return (f"{scene}. The main character is {animal.prompt}. Funny cute meme-style clip, expressive reaction, "
            f"photorealistic, smooth camera, no text on screen")
