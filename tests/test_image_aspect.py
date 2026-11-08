"""Мастер «Картинка»: кнопка горизонтального 16:9 и точный aspect_ratio в запросе xAI.

Без платных вызовов: мастер мокает XAIClient.image, отдельный тест — httpx.MockTransport.
"""
from __future__ import annotations

import base64
import json
import tempfile
import unittest

import httpx

import bot
from tests.test_video_i2v import TOKEN, UID, Harness

JPEG = base64.b64encode(b"\xff\xd8\xff\xd9").decode()


class ImageAspectTests(unittest.IsolatedAsyncioTestCase):
    async def _session_allows_photo(self, h: Harness):
        orig = h.session.make_request

        async def make_request(bot_, method, timeout=None):
            if type(method).__name__ == "SendPhoto":
                h.session.calls.append(method)
                h.session.texts.append(method.caption or "")
                return h.session._msg(bot_, method.chat_id, method.caption or "")
            return await orig(bot_, method, timeout)

        h.session.make_request = make_request

    async def _choose(self, h: Harness, label: str, prompt: str = "рыжий кот на луне") -> tuple[str, list]:
        calls: list[tuple] = []

        async def fake_image(prompt_text, aspect_ratio=None, resolution=None):
            calls.append((prompt_text, aspect_ratio, resolution))
            return b"\xff\xd8\xff\xd9", "image/jpeg", bot.Cost(0.04, True)

        h.xai.image = fake_image
        await self._session_allows_photo(h)
        await h.message(1, text=bot.MENU_IMG)
        await h.message(2, text=prompt)
        data = h.button(label)
        await h.click(3, data)
        return data, calls

    async def test_horizontal_button_passes_exact_16_9(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            async with Harness(tmp) as h:
                data, calls = await self._choose(h, "Горизонтальное 16:9")
                self.assertTrue(data.endswith(":16:9"))
                self.assertEqual(data.split(":", 3)[3], "16:9")
                self.assertEqual(calls, [("рыжий кот на луне", "16:9", None)])
                self.assertIn("рыжий кот на луне", h.joined())

    async def test_existing_formats_stay_exact(self) -> None:
        for label, aspect in (("1:1", "1:1"), ("9:16", "9:16")):
            with self.subTest(aspect=aspect):
                with tempfile.TemporaryDirectory() as tmp:
                    async with Harness(tmp) as h:
                        data, calls = await self._choose(h, label, prompt=f"кадр {aspect}")
                        self.assertEqual(data.split(":", 3)[3], aspect)
                        self.assertEqual(calls, [(f"кадр {aspect}", aspect, None)])

    async def test_unknown_aspect_is_not_sent_as_square(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            async with Harness(tmp) as h:
                calls: list[tuple] = []

                async def fake_image(*args, **kwargs):
                    calls.append((args, kwargs))
                    return b"\xff\xd8", "image/jpeg", bot.Cost(0, True)

                h.xai.image = fake_image
                await h.message(1, text=bot.MENU_IMG)
                await h.message(2, text="тест")
                tok = h.button("1:1").split(":")[1]
                await h.click(3, f"wz:{tok}:a:4:3")
                self.assertEqual(calls, [])
                self.assertIn("Не понял выбор", h.joined())

    async def test_image_api_posts_aspect_ratio_16_9(self) -> None:
        seen: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(json.loads(request.content.decode()))
            return httpx.Response(200, json={
                "data": [{"b64_json": JPEG, "mime_type": "image/jpeg"}],
                "usage": {"cost_in_usd_ticks": 400_000_000},
            })

        cfg = bot.Config(
            telegram_token=TOKEN, xai_api_key="xai-test-not-a-secret",
            allowed_user_ids=frozenset({UID}), admin_user_ids=frozenset({UID}),
            image_model="grok-imagine-image-2.0",
        )
        client = bot.XAIClient(cfg)
        await client.http.aclose()
        client.http = httpx.AsyncClient(
            base_url=cfg.base_url,
            headers={"Authorization": f"Bearer {cfg.xai_api_key}"},
            transport=httpx.MockTransport(handler),
            timeout=httpx.Timeout(5.0),
        )
        try:
            raw, mime, cost = await client.image("горизонтальный закат", "16:9")
            self.assertEqual(mime, "image/jpeg")
            self.assertTrue(raw.startswith(b"\xff\xd8"))
            self.assertEqual(seen[0]["aspect_ratio"], "16:9")
            self.assertEqual(seen[0]["prompt"], "горизонтальный закат")
            self.assertEqual(seen[0]["model"], "grok-imagine-image-2.0")
            self.assertAlmostEqual(cost.usd, 0.04, places=6)
            seen.clear()
            await client.image("квадрат", "1:1")
            await client.image("сторис", "9:16")
            self.assertEqual([b["aspect_ratio"] for b in seen], ["1:1", "9:16"])
        finally:
            await client.close()


if __name__ == "__main__":
    unittest.main()
