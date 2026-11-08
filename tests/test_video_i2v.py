"""Моки image-to-video: запрос xAI, фото/файл Telegram, мастер, отмена, успех и ошибки.

Платный вызов api.x.ai здесь не делается — httpx.MockTransport и подмена Bot.download.
"""
from __future__ import annotations

import base64
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

import httpx
from PIL import Image
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import TelegramMethod
from aiogram.methods.base import TelegramType
from aiogram.types import Message, Update

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import bot  # noqa: E402
import refs  # noqa: E402

TOKEN = "123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"
UID = 42
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64


def jpeg_bytes(w: int = 40, h: int = 24, color: tuple[int, int, int] = (20, 80, 140)) -> bytes:
    im = Image.new("RGB", (w, h), color)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=90)
    return buf.getvalue()


def png_bytes(w: int = 30, h: int = 18, color: tuple[int, int, int, int] = (200, 10, 10, 255)) -> bytes:
    im = Image.new("RGBA", (w, h), color)
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def gif_bytes() -> bytes:
    im = Image.new("RGB", (8, 8), (1, 2, 3))
    buf = io.BytesIO()
    im.save(buf, "GIF")
    return buf.getvalue()


def decode_data_uri(uri: str) -> bytes:
    head, _, b64 = uri.partition(",")
    if not head.startswith("data:image/jpeg;base64"):
        raise AssertionError(head)
    return base64.b64decode(b64)


class FakeSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[TelegramMethod] = []
        self.texts: list[str] = []
        self.fail_video = False
        self._mid = 100

    async def close(self) -> None:
        return None

    def _msg(self, bot_: Bot, chat_id: int, text: str) -> Message:
        self._mid += 1
        return Message.model_validate(
            {"message_id": self._mid, "date": 1_700_000_000, "chat": {"id": chat_id, "type": "private"},
             "text": text or ""},
            context={"bot": bot_},
        )

    async def make_request(self, bot_: Bot, method: TelegramMethod[TelegramType],
                           timeout: int | None = None) -> TelegramType:
        self.calls.append(method)
        name = type(method).__name__
        if name == "SendMessage":
            self.texts.append(method.text or "")
            return self._msg(bot_, method.chat_id, method.text or "")
        if name in ("EditMessageText", "EditMessageReplyMarkup", "DeleteMessage", "SendChatAction",
                    "AnswerCallbackQuery"):
            if name == "EditMessageText":
                self.texts.append(method.text or "")
            return True
        if name == "SendVideo":
            path = getattr(method.video, "path", None)
            if path:
                with open(path, "rb") as f:
                    data = f.read(32)
                if not data.startswith(b"\x00\x00\x00\x18ftyp"):
                    raise AssertionError(data[:16])
            if self.fail_video:
                self.fail_video = False
                raise TelegramBadRequest(method=method, message="Bad Request: failed to send video")
            return self._msg(bot_, method.chat_id, getattr(method, "caption", "") or "")
        if name == "SendDocument":
            path = method.document.path
            with open(path, "rb") as f:
                data = f.read(32)
            if not data.startswith(b"\x00\x00\x00\x18ftyp"):
                raise AssertionError(data[:16])
            return self._msg(bot_, method.chat_id, getattr(method, "caption", "") or "")
        raise AssertionError(f"unexpected method {name}")

    async def stream_content(self, url: str, headers=None, timeout: int = 30, chunk_size: int = 65536,
                             raise_for_status: bool = True):
        yield b""


class ApiScript:
    """Сценарий POST /videos/generations и GET /videos/{id} без сети."""

    def __init__(self, mode: str = "ok") -> None:
        self.mode = mode
        self.posts: list[dict] = []
        self.gets = 0
        self.downloads = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            self.posts.append(json.loads(request.content.decode()))
            if self.mode == "start_timeout":
                raise httpx.ReadTimeout("slow")
            if self.mode == "start_400":
                return httpx.Response(400, json={"error": {"message": "bad image"}})
            return httpx.Response(200, json={"request_id": "req-video-1"})
        self.gets += 1
        if self.mode == "poll_retry":
            if self.gets == 1:
                return httpx.Response(500, text="upstream reset")
            if self.gets == 2:
                # Промежуточный usage нельзя прибавлять к финальному.
                return httpx.Response(200, json={
                    "status": "pending", "progress": 40,
                    "usage": {"cost_in_usd_ticks": 9_000_000_000_000},
                })
        if self.mode == "timeout":
            return httpx.Response(200, json={
                "status": "pending", "progress": 5,
                "usage": {"cost_in_usd_ticks": 5_000_000_000_000},
            })
        if self.mode == "failed":
            return httpx.Response(200, json={
                "status": "failed",
                "error": {"code": "invalid_argument", "message": "модель отклонила кадр"},
                "usage": {"cost_in_usd_ticks": 500_000_000},
            })
        if self.mode == "moderation":
            return httpx.Response(200, json={
                "status": "done",
                "video": {"url": "", "duration": 5, "respect_moderation": False},
                "usage": {"cost_in_usd_ticks": 1_000_000_000},
            })
        if self.mode == "expired":
            return httpx.Response(200, json={"status": "expired", "error": {"message": "expired"}})
        return httpx.Response(200, json={
            "status": "done", "progress": 100,
            "video": {"url": "https://vidgen.x.ai/clip.mp4", "duration": 5, "respect_moderation": True},
            "usage": {"cost_in_usd_ticks": 1_500_000_000},
        })

    def download(self, request: httpx.Request) -> httpx.Response:
        self.downloads += 1
        if "vidgen.x.ai" not in str(request.url):
            return httpx.Response(404)
        return httpx.Response(200, content=MP4)


class Harness:
    def __init__(self, tmp: str, *, mode: str = "ok", timeout: float = 5.0, proxy: str | None = None,
                 fail_video: bool = False, balance: float | None = None) -> None:
        self.tmp = tmp
        self.script = ApiScript(mode)
        self.cfg = bot.Config(
            telegram_token=TOKEN,
            xai_api_key="xai-test-not-a-secret",
            allowed_user_ids=frozenset({UID}),
            admin_user_ids=frozenset({UID}),
            xai_proxy=proxy,
            data_dir=os.path.join(tmp, "data"),
            work_dir=os.path.join(tmp, "work"),
            video_poll_sec=0.01,
            video_timeout_sec=timeout,
            video_duration=6,
            video_resolution="720p",
            show_cost=True,
            start_balance=balance,
            warn_thresholds=(),
            album_wait_sec=0.02,
            access_requests=False,
        )
        self.session = FakeSession()
        self.session.fail_video = fail_video
        self.bot = Bot(token=TOKEN, session=self.session)
        self.xai = bot.XAIClient(self.cfg)
        self.files = {
            "ph-1": jpeg_bytes(),
            "ph-2": jpeg_bytes(color=(9, 9, 9)),
            "doc-png": png_bytes(),
            "doc-gif": gif_bytes(),
            "reply-ph": jpeg_bytes(16, 16, (4, 5, 6)),
        }
        self.downloaded: list[str] = []

        async def fake_download(file, destination=None, **kwargs):
            fid = file if isinstance(file, str) else getattr(file, "file_id", "")
            self.downloaded.append(fid)
            return io.BytesIO(self.files[fid])

        self.bot.download = fake_download  # type: ignore[method-assign]
        self.dp = None
        self.tracker = None

    async def __aenter__(self):
        await self.xai.http.aclose()
        self.xai.http = httpx.AsyncClient(
            base_url=self.cfg.base_url,
            headers={"Authorization": f"Bearer {self.cfg.xai_api_key}"},
            transport=httpx.MockTransport(self.script.handler),
            timeout=httpx.Timeout(5.0),
        )
        self.xai.dl_transport = httpx.MockTransport(self.script.download)
        self.tracker = bot.BalanceTracker(self.cfg)
        self.dp = bot.build_dispatcher(self.cfg, self.xai, self.tracker)
        return self

    async def __aexit__(self, *exc):
        await self.xai.close()
        if self.tracker:
            await self.tracker.close()
        await self.bot.session.close()

    def _user(self, uid: int = UID) -> dict:
        return {"id": uid, "is_bot": False, "first_name": "Тест"}

    def _chat(self) -> dict:
        return {"id": UID, "type": "private"}

    async def message(self, mid: int, uid: int = UID, **fields) -> None:
        msg = {"message_id": mid, "date": 1_700_000_000, "chat": self._chat(), "from": self._user(uid), **fields}
        await self.dp.feed_update(self.bot, Update.model_validate({"update_id": mid, "message": msg}))

    async def click(self, mid: int, data: str, uid: int = UID) -> None:
        upd = {
            "update_id": mid,
            "callback_query": {
                "id": f"cb{mid}",
                "from": self._user(uid),
                "chat_instance": "ci",
                "data": data,
                "message": {"message_id": 1, "date": 1_700_000_000, "chat": self._chat(),
                            "from": {"id": 1, "is_bot": True, "first_name": "bot"}, "text": "шаг"},
            },
        }
        await self.dp.feed_update(self.bot, Update.model_validate(upd))

    def button(self, contains: str) -> str:
        for method in reversed(self.session.calls):
            markup = getattr(method, "reply_markup", None)
            rows = getattr(markup, "inline_keyboard", None) or []
            for row in rows:
                for btn in row:
                    if contains in (btn.text or "") or contains in (btn.callback_data or ""):
                        return btn.callback_data
        raise AssertionError(f"нет кнопки {contains!r}; тексты: {self.session.texts[-4:]}")

    def joined(self) -> str:
        return "\n".join(self.session.texts)

    async def open_video(self, seconds: str = "5 сек") -> None:
        await self.message(1, text=bot.MENU_VIDEO)
        await self.click(2, self.button(seconds))

    async def choose_photo_mode(self) -> None:
        await self.open_video()
        await self.click(3, self.button("По фото"))


class StillUnitTests(unittest.TestCase):
    def test_save_and_data_uri_roundtrip(self) -> None:
        raw = jpeg_bytes(80, 40, (30, 60, 90))
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "still.jpg")
            refs.save_video_still(raw, path)
            uri = refs.video_still_data_uri(path)
            self.assertNotIn("api.telegram.org", uri)
            self.assertNotIn("/file/bot", uri)
            back = Image.open(io.BytesIO(decode_data_uri(uri)))
            self.assertEqual(back.format, "JPEG")
            self.assertLessEqual(max(back.size), refs.VIDEO_STILL_MAX_SIDE)
            px = back.getpixel((0, 0))
            self.assertLess(abs(px[0] - 30), 12)

    def test_rejects_empty_gif_and_huge(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "x.jpg")
            with self.assertRaises(refs.StillError) as empty:
                refs.save_video_still(b"", path)
            self.assertEqual(empty.exception.code, "empty")
            with self.assertRaises(refs.StillError) as gif:
                refs.save_video_still(gif_bytes(), path)
            self.assertEqual(gif.exception.code, "format")
            self.assertIn("JPG", bot.still_error_text(gif.exception))
            with self.assertRaises(refs.StillError) as big:
                refs.save_video_still(b"x" * (refs.STILL_MAX_BYTES + 1), path)
            self.assertEqual(big.exception.code, "too_big")
            with self.assertRaises(refs.StillError) as junk:
                refs.save_video_still(b"this is not an image", path)
            self.assertEqual(junk.exception.code, "unreadable")

    def test_png_accepted_and_huge_pixels_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "x.jpg")
            refs.save_video_still(png_bytes(), path)
            self.assertTrue(path.endswith(".jpg"))
            old = refs.MAX_DECODE_PIXELS
            refs.MAX_DECODE_PIXELS = 10
            try:
                big = png_bytes(20, 20)
                with self.assertRaises(refs.StillError) as err:
                    refs.save_video_still(big, path)
                self.assertEqual(err.exception.code, "pixels")
            finally:
                refs.MAX_DECODE_PIXELS = old


class VideoClientTests(unittest.IsolatedAsyncioTestCase):
    async def _client(self, handler, proxy: str | None = None) -> bot.XAIClient:
        cfg = bot.Config(
            telegram_token=TOKEN, xai_api_key="xai-test-not-a-secret",
            allowed_user_ids=frozenset({UID}), admin_user_ids=frozenset({UID}),
            xai_proxy=proxy, video_resolution="720p", video_model="grok-imagine-video-1.5-lite",
        )
        client = bot.XAIClient(cfg)
        await client.http.aclose()
        client.http = httpx.AsyncClient(
            base_url=cfg.base_url,
            headers={"Authorization": f"Bearer {cfg.xai_api_key}"},
            transport=httpx.MockTransport(handler),
            timeout=httpx.Timeout(5.0),
        )
        return client

    async def test_image_payload_and_text_payload(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            if request.method == "GET":
                if request.url.path.endswith("/videos/req"):
                    return httpx.Response(202, json={"progress": 15})
                return httpx.Response(200, json={"status": "done"})
            return httpx.Response(200, json={"request_id": "req"})

        client = await self._client(handler)
        try:
            uri = "data:image/jpeg;base64," + base64.b64encode(jpeg_bytes()).decode()
            rid = await client.video_start("камера приближается", 12, "9:16", "720p", image_url=uri)
            self.assertEqual(rid, "req")
            body = json.loads(seen[0].content.decode())
            self.assertEqual(body["model"], "grok-imagine-video-1.5-lite")
            self.assertEqual(body["duration"], 12)
            self.assertEqual(body["resolution"], "720p")
            self.assertEqual(body["prompt"], "камера приближается")
            self.assertEqual(body["image"], {"url": uri})
            self.assertNotIn("aspect_ratio", body)
            self.assertNotIn("api.telegram.org", body["image"]["url"])
            self.assertTrue(seen[0].headers["authorization"].startswith("Bearer "))
            seen.clear()
            await client.video_start("ocean", 6, "9:16", "480p")
            text_body = json.loads(seen[0].content.decode())
            self.assertEqual(text_body["aspect_ratio"], "9:16")
            self.assertNotIn("image", text_body)
            self.assertIn("ocean", text_body["prompt"])
            pending = await client.video_get("req")
            self.assertEqual(pending["status"], "pending")
            self.assertEqual(pending["progress"], 15)
        finally:
            await client.close()

    async def test_refuses_telegram_and_http_urls_without_request(self) -> None:
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            return httpx.Response(200, json={"request_id": "nope"})

        client = await self._client(handler)
        try:
            for url in (
                "https://api.telegram.org/file/bot123:SECRET/photos/file_1.jpg",
                "https://example.com/public.jpg",
                "data:image/gif;base64,AAAA",
            ):
                with self.assertRaises(bot.XAIError):
                    await client.video_start("motion", 4, image_url=url)
            self.assertEqual(seen, [])
        finally:
            await client.close()

    async def test_proxy_passed_to_api_and_download_clients(self) -> None:
        proxies: list[str | None] = []
        real = bot.httpx.AsyncClient
        posts: list[dict] = []

        def api(request: httpx.Request) -> httpx.Response:
            if request.method == "POST":
                posts.append(json.loads(request.content.decode()))
                return httpx.Response(200, json={"request_id": "req"})
            return httpx.Response(200, json={"status": "pending"})

        def factory(*args, **kwargs):
            proxies.append(kwargs.get("proxy"))
            kwargs = dict(kwargs)
            kwargs.pop("proxy", None)
            if kwargs.get("transport") is None:
                kwargs["transport"] = httpx.MockTransport(api)
            return real(*args, **kwargs)

        bot.httpx.AsyncClient = factory
        path = ""
        try:
            cfg = bot.Config(
                telegram_token=TOKEN, xai_api_key="xai-test-not-a-secret",
                allowed_user_ids=frozenset({UID}), admin_user_ids=frozenset({UID}),
                xai_proxy="http://user:pass@127.0.0.1:9",
            )
            client = bot.XAIClient(cfg)
            uri = "data:image/png;base64," + base64.b64encode(png_bytes()).decode()
            await client.video_start("pan", 3, image_url=uri)
            with tempfile.TemporaryDirectory() as tmp:
                path = os.path.join(tmp, "v.mp4")

                def dl(request: httpx.Request) -> httpx.Response:
                    return httpx.Response(200, content=MP4)

                client.dl_transport = httpx.MockTransport(dl)
                n = await client.download("https://vidgen.x.ai/clip.mp4", path)
            self.assertGreater(n, 0)
            self.assertTrue(path.endswith(".mp4"))
            self.assertEqual(posts[0]["image"]["url"][:22], "data:image/png;base64,")
            self.assertGreaterEqual(len(proxies), 2)
            self.assertTrue(all(p == "http://user:pass@127.0.0.1:9" for p in proxies))
            await client.close()
        finally:
            bot.httpx.AsyncClient = real


class WizardTests(unittest.IsolatedAsyncioTestCase):
    async def test_photo_wizard_sends_data_uri_and_video(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            async with Harness(tmp) as h:
                await h.choose_photo_mode()
                await h.message(10, photo=[{
                    "file_id": "ph-1", "file_unique_id": "u1", "width": 40, "height": 24, "file_size": 800,
                }])
                self.assertIn("Как оживить", h.joined())
                self.assertEqual(h.script.posts, [])
                await h.message(11, text="камера медленно приближается, лёгкий ветер")
                self.assertEqual(len(h.script.posts), 1)
                body = h.script.posts[0]
                self.assertNotIn("aspect_ratio", body)
                self.assertEqual(body["duration"], 5)
                self.assertEqual(body["resolution"], "720p")
                self.assertIn("лёгкий ветер", body["prompt"])
                self.assertNotIn("9:16", body["prompt"])
                raw = decode_data_uri(body["image"]["url"])
                self.assertTrue(raw.startswith(b"\xff\xd8"))
                self.assertEqual(h.downloaded, ["ph-1"])
                self.assertNotIn("api.telegram.org", body["image"]["url"])
                self.assertEqual(h.script.downloads, 1)
                self.assertAlmostEqual(h.tracker.state.spent_total, 0.15, places=6)
                self.assertTrue(any(type(c).__name__ == "SendVideo" for c in h.session.calls))
                self.assertIn("по вашему фото", h.joined())
                self.assertIn("$0.1500", h.joined())

    async def test_document_png_and_caption_launches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            async with Harness(tmp) as h:
                await h.choose_photo_mode()
                await h.message(10, caption="человек улыбается и машет рукой", document={
                    "file_id": "doc-png", "file_unique_id": "d1", "file_name": "shot.png",
                    "mime_type": "image/png", "file_size": 500,
                })
                self.assertEqual(len(h.script.posts), 1)
                body = h.script.posts[0]
                self.assertIn("машет", body["prompt"])
                self.assertTrue(body["image"]["url"].startswith("data:image/jpeg;base64,"))
                self.assertEqual(h.downloaded, ["doc-png"])
                self.assertAlmostEqual(h.tracker.state.spent_total, 0.15, places=6)

    async def test_text_to_video_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            async with Harness(tmp) as h:
                await h.open_video("8 сек")
                await h.click(4, h.button("По описанию"))
                await h.message(5, text="чашка кофе на подоконнике")
                body = h.script.posts[0]
                self.assertEqual(body["aspect_ratio"], "9:16")
                self.assertNotIn("image", body)
                self.assertIn("9:16", body["prompt"])
                self.assertIn("чашка кофе", body["prompt"])
                self.assertEqual(body["duration"], 8)
                self.assertTrue(any(type(c).__name__ == "SendVideo" for c in h.session.calls))

    async def test_command_photo_caption_and_plain_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            async with Harness(tmp) as h:
                await h.message(1, caption="/storyvideo 4 камера едет вперёд", photo=[{
                    "file_id": "ph-1", "file_unique_id": "u1", "width": 40, "height": 24, "file_size": 100,
                }])
                body = h.script.posts[0]
                self.assertEqual(body["duration"], 4)
                self.assertIn("едет вперёд", body["prompt"])
                self.assertNotIn("aspect_ratio", body)
                self.assertTrue(body["image"]["url"].startswith("data:image/"))
                await h.message(2, text="/storyvideo 3 море и закат")
                second = h.script.posts[1]
                self.assertEqual(second["aspect_ratio"], "9:16")
                self.assertNotIn("image", second)
                self.assertIn("море", second["prompt"])

    async def test_reply_to_photo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            async with Harness(tmp) as h:
                await h.message(3, text="/storyvideo 5 камера отъезжает", reply_to_message={
                    "message_id": 2, "date": 1_700_000_000, "chat": h._chat(), "from": h._user(),
                    "photo": [{"file_id": "reply-ph", "file_unique_id": "r", "width": 16, "height": 16,
                               "file_size": 50}],
                })
                self.assertEqual(h.downloaded, ["reply-ph"])
                self.assertIn("отъезжает", h.script.posts[0]["prompt"])
                self.assertIn("image", h.script.posts[0])

    async def test_cancel_then_photo_is_just_a_reference(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            async with Harness(tmp) as h:
                await h.choose_photo_mode()
                await h.click(6, h.button("Отмена"))
                self.assertIn("Отменено", h.joined())
                await h.message(7, photo=[{
                    "file_id": "ph-1", "file_unique_id": "u1", "width": 40, "height": 24, "file_size": 100,
                }])
                self.assertEqual(h.script.posts, [])
                self.assertIn("Фото сохранены", h.joined())

    async def test_text_instead_of_photo_and_bad_formats(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            async with Harness(tmp) as h:
                await h.choose_photo_mode()
                await h.message(8, text="просто текст")
                self.assertIn("нужно фото", h.joined())
                self.assertEqual(h.script.posts, [])
                await h.message(9, document={
                    "file_id": "doc-gif", "file_unique_id": "g", "file_name": "a.gif",
                    "mime_type": "image/gif", "file_size": 40,
                })
                self.assertIn("JPG, PNG или WebP", h.joined())
                self.assertNotIn("doc-gif", h.downloaded)
                await h.message(12, document={
                    "file_id": "doc-gif", "file_unique_id": "g2", "file_name": "fake.jpg",
                    "mime_type": "image/jpeg", "file_size": 40,
                })
                self.assertIn("doc-gif", h.downloaded)
                self.assertIn("формат не подходит", h.joined())
                await h.message(13, photo=[{
                    "file_id": "ph-1", "file_unique_id": "big", "width": 4000, "height": 3000,
                    "file_size": 21 * 1024 * 1024,
                }])
                self.assertIn("20 МБ", h.joined())
                self.assertEqual(h.downloaded.count("ph-1"), 0)
                self.assertEqual(h.script.posts, [])

    async def test_saved_reference_button(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            async with Harness(tmp) as h:
                await h.message(1, photo=[{
                    "file_id": "ph-1", "file_unique_id": "u1", "width": 40, "height": 24, "file_size": 100,
                }])
                self.assertIn("Фото сохранены", h.joined())
                await h.message(2, text=bot.MENU_VIDEO)
                await h.click(3, h.button("5 сек"))
                await h.click(4, h.button("По фото"))
                await h.click(5, h.button("сохранённое"))
                self.assertIn("Как оживить", h.joined())
                await h.message(6, text="лёгкий поворот головы")
                self.assertEqual(len(h.script.posts), 1)
                self.assertIn("поворот", h.script.posts[0]["prompt"])
                self.assertIn("image", h.script.posts[0])

    async def test_album_uses_first_frame_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            async with Harness(tmp) as h:
                await h.choose_photo_mode()
                photo = lambda fid: [{
                    "file_id": fid, "file_unique_id": fid, "width": 40, "height": 24, "file_size": 80,
                }]
                await h.message(20, media_group_id="alb", photo=photo("ph-1"), caption="камера вверх")
                await h.message(21, media_group_id="alb", photo=photo("ph-2"))
                import asyncio
                await asyncio.sleep(0.12)
                self.assertEqual(len(h.script.posts), 1)
                self.assertIn("только первое", h.joined())
                self.assertEqual(h.downloaded, ["ph-1"])
                self.assertIn("вверх", h.script.posts[0]["prompt"])

    async def test_poll_retry_charges_final_usage_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            async with Harness(tmp, mode="poll_retry") as h:
                await h.choose_photo_mode()
                await h.message(10, caption="кивок", photo=[{
                    "file_id": "ph-1", "file_unique_id": "u", "width": 10, "height": 10, "file_size": 20,
                }])
                self.assertEqual(len(h.script.posts), 1)
                self.assertGreaterEqual(h.script.gets, 3)
                self.assertIn("Оживляю фото", h.joined())
                self.assertAlmostEqual(h.tracker.state.spent_total, 0.15, places=6)
                self.assertEqual(h.script.downloads, 1)

    async def test_failures_do_not_double_charge_or_resubmit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            async with Harness(tmp, mode="failed") as h:
                await h._run_photo_job()
                self.assertEqual(len(h.script.posts), 1)
                self.assertAlmostEqual(h.tracker.state.spent_total, 0.05, places=6)
                self.assertIn("не получилось", h.joined())
                self.assertEqual(h.script.downloads, 0)
                self.assertFalse(any(type(c).__name__ == "SendVideo" for c in h.session.calls))
        with tempfile.TemporaryDirectory() as tmp:
            async with Harness(tmp, mode="moderation") as h:
                await h._run_photo_job()
                self.assertAlmostEqual(h.tracker.state.spent_total, 0.10, places=6)
                self.assertIn("модерац", h.joined())
                self.assertEqual(h.script.downloads, 0)
                self.assertEqual(len(h.script.posts), 1)
        with tempfile.TemporaryDirectory() as tmp:
            async with Harness(tmp, mode="expired") as h:
                await h._run_photo_job()
                self.assertEqual(h.tracker.state.spent_total, 0)
                self.assertIn("expired", h.joined())
                self.assertEqual(len(h.script.posts), 1)
        with tempfile.TemporaryDirectory() as tmp:
            async with Harness(tmp, mode="timeout", timeout=0.05) as h:
                await h._run_photo_job()
                self.assertEqual(len(h.script.posts), 1)
                self.assertEqual(h.tracker.state.spent_total, 0)
                self.assertIn("дважды", h.joined())
                self.assertGreaterEqual(h.script.gets, 1)
        with tempfile.TemporaryDirectory() as tmp:
            async with Harness(tmp, mode="start_timeout") as h:
                await h._run_photo_job()
                self.assertEqual(len(h.script.posts), 1)
                self.assertEqual(h.tracker.state.spent_total, 0)
                self.assertIn("Повторно запрос не отправляю", h.joined())
                self.assertEqual(h.script.downloads, 0)
        with tempfile.TemporaryDirectory() as tmp:
            async with Harness(tmp, mode="start_400") as h:
                await h._run_photo_job()
                self.assertEqual(len(h.script.posts), 1)
                self.assertEqual(h.tracker.state.spent_total, 0)
                self.assertIn("ошибку запроса", h.joined())

    async def test_low_balance_confirm_keeps_reference_image(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            async with Harness(tmp, balance=0.01) as h:
                await h.choose_photo_mode()
                await h.message(10, caption="кивок", photo=[{
                    "file_id": "ph-1", "file_unique_id": "u", "width": 20, "height": 20, "file_size": 30,
                }])
                self.assertEqual(h.script.posts, [])
                self.assertIn("больше остатка", h.joined())
                await h.click(11, h.button("Всё равно"))
                self.assertEqual(len(h.script.posts), 1)
                self.assertIn("кивок", h.script.posts[0]["prompt"])
                self.assertTrue(h.script.posts[0]["image"]["url"].startswith("data:image/jpeg;base64,"))
                self.assertAlmostEqual(h.tracker.state.spent_total, 0.15, places=6)

    async def test_send_video_falls_back_to_document(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            async with Harness(tmp, fail_video=True) as h:
                await h._run_photo_job()
                names = [type(c).__name__ for c in h.session.calls]
                self.assertIn("SendVideo", names)
                self.assertIn("SendDocument", names)
                self.assertEqual(h.script.downloads, 1)
                self.assertAlmostEqual(h.tracker.state.spent_total, 0.15, places=6)

    async def test_access_control_and_admin_users(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            async with Harness(tmp) as h:
                await h.message(1, uid=99, text=bot.MENU_VIDEO)
                await h.message(2, uid=99, text="/storyvideo 5 море")
                await h.click(3, "wz:abcdef:v:photo", uid=99)
                self.assertEqual(h.script.posts, [])
                self.assertIn("нет доступа", h.joined().lower())
                await h.message(4, text="/users")
                self.assertIn("Доступ к боту", h.joined())
                self.assertIn("Администраторы", h.joined())

async def _run_photo_job(self: Harness) -> None:
    await self.choose_photo_mode()
    await self.message(10, caption="движение камеры", photo=[{
        "file_id": "ph-1", "file_unique_id": "u", "width": 20, "height": 20, "file_size": 30,
    }])


Harness._run_photo_job = _run_photo_job  # type: ignore[attr-defined]


if __name__ == "__main__":
    unittest.main()
