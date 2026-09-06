"""Студійний бот: розбір рядка в опис і читання відповіді моделі."""
import base64

import pytest

from studio import caption
from studio.imagegen import _extract_image, _refusal_reason
from studio.styles import KEEP_THE_GARMENT, STYLES, style_prompt


class TestCaption:
    def test_full_line(self):
        out = caption.build("Ralph Lauren / світшот / M / 1450 / 9 / 48 56 68 64")
        assert out.startswith("Ralph Lauren · світшот · M · 1450 ₴")
        assert "Стан 9/10." in out
        assert "плечі 48, груди 56, довжина 68, рукав 64" in out.lower()
        assert "#ralphlauren" in out

    def test_price_keeps_a_currency_the_seller_typed(self):
        assert "3200 грн" in caption.build("Carhartt / куртка / L / 3200 грн")

    def test_bare_number_becomes_hryvnia(self):
        assert "890 ₴" in caption.build("Nike / худі / S / 890")

    def test_newlines_work_like_slashes(self):
        out = caption.build("Fila\nвітрівка\nXL\n700")
        assert "Fila · вітрівка · XL · 700 ₴" in out

    def test_missing_tail_fields_are_fine(self):
        out = caption.build("Levi's / джинси")
        assert out.startswith("Levi's · джинси")
        assert "Пиши «+» у дірект" in out

    def test_empty_input_has_no_caption(self):
        assert caption.build("") is None
        assert caption.build("   ") is None

    def test_condition_text_passes_through(self):
        assert "Як нова." in caption.build("Puma / кофта / M / 600 / як нова")

    def test_call_to_action_and_tags_always_present(self):
        out = caption.build("Adidas / штани / L / 900")
        assert out.rstrip().endswith("#оригінал")
        assert "Пиши «+» у дірект" in out


class TestStyles:
    @pytest.mark.parametrize("key", list(STYLES))
    def test_every_style_forbids_touching_the_garment(self, key):
        """Головне правило: річ на фото не можна ні чистити, ні перемальовувати."""
        prompt = style_prompt(key)
        assert prompt.startswith(KEEP_THE_GARMENT)
        assert "must stay exactly as they are" in prompt

    def test_every_style_asks_for_the_post_ratio(self):
        for key in STYLES:
            assert "vertical 4:5" in style_prompt(key)

    def test_unknown_style_falls_back_instead_of_failing(self):
        assert style_prompt("не існує") == style_prompt("light")


class TestModelResponse:
    def test_reads_the_image_out_of_a_reply(self):
        raw = b"\x89PNG fake"
        data = {"candidates": [{"content": {"parts": [
            {"text": "here you go"},
            {"inlineData": {"mime_type": "image/png", "data": base64.b64encode(raw).decode()}},
        ]}}]}
        assert _extract_image(data) == raw

    def test_snake_case_key_also_works(self):
        raw = b"jpegbytes"
        data = {"candidates": [{"content": {"parts": [
            {"inline_data": {"data": base64.b64encode(raw).decode()}}
        ]}}]}
        assert _extract_image(data) == raw

    def test_no_image_returns_none(self):
        assert _extract_image({"candidates": [{"content": {"parts": [{"text": "no"}]}}]}) is None

    def test_refusal_is_reported_in_words(self):
        data = {"candidates": [{"finishReason": "SAFETY", "content": {"parts": []}}]}
        assert "відмовилась" in _refusal_reason(data)

    def test_model_explanation_is_passed_through(self):
        data = {"candidates": [{"content": {"parts": [{"text": "I cannot edit this"}]}}]}
        assert _refusal_reason(data) == "I cannot edit this"


class TestModelFallback:
    """Квота в Gemini рахується окремо для кожної моделі, тож 429 на одній
    не означає, що не спрацює сусідня."""

    @pytest.mark.asyncio
    async def test_moves_past_a_model_that_is_out_of_quota(self, monkeypatch):
        from studio.imagegen import ImageGenerator, _TryNextModel
        from studio.settings import StudioSettings

        gen = ImageGenerator(StudioSettings(bot_token="t", api_key="k",
                                            models=["a", "b", "c"]))
        tried: list[str] = []

        async def fake_pick():
            return "a"

        async def fake_one_shot(image, mime, style, model):
            tried.append(model)
            if model != "c":
                raise _TryNextModel(f"{model}: 429 quota")
            return b"png"

        monkeypatch.setattr(gen, "pick_model", fake_pick)
        monkeypatch.setattr(gen, "_one_shot", fake_one_shot)
        assert await gen.transform(b"in", "image/jpeg", "light") == b"png"
        assert tried == ["a", "b", "c"]
        assert gen.model == "c"
        await gen.close()

    @pytest.mark.asyncio
    async def test_all_out_of_quota_explains_billing(self, monkeypatch):
        from studio.imagegen import ImageGenError, ImageGenerator, _TryNextModel
        from studio.settings import StudioSettings

        gen = ImageGenerator(StudioSettings(bot_token="t", api_key="k", models=["a", "b"]))

        async def fake_pick():
            return "a"

        async def always_fail(image, mime, style, model):
            raise _TryNextModel(f"{model}: 429 quota")

        monkeypatch.setattr(gen, "pick_model", fake_pick)
        monkeypatch.setattr(gen, "_one_shot", always_fail)
        with pytest.raises(ImageGenError) as err:
            await gen.transform(b"in", "image/jpeg", "light")
        assert "білінг" in str(err.value)
        await gen.close()
