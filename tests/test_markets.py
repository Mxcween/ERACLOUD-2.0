"""Набір ринків задається оточенням, а не редагуванням конфігу.

Потрібно це для того, щоб один репозиторій обслуговував кілька розгортань:
у головного бота MARKETS порожній (працює PL+DE з config.yaml), у бота для
Іспанії MARKETS=ES. Код і конфіг спільні, а ринки, бот у Telegram, база й
бюджет трафіку - у кожного свої.
"""
from __future__ import annotations

import pytest

from vintsniper.settings import CONFIG_DIR, load_settings


def codes(monkeypatch, value: str | None) -> list[str]:
    if value is None:
        monkeypatch.delenv("MARKETS", raising=False)
    else:
        monkeypatch.setenv("MARKETS", value)
    return [m.code for m in load_settings().enabled_markets]


class TestSelection:
    def test_without_the_variable_the_config_decides(self, monkeypatch):
        assert codes(monkeypatch, None) == ["PL", "DE"]

    def test_an_empty_variable_changes_nothing(self, monkeypatch):
        """Render уміє віддавати змінну як порожній рядок - це не вибір."""
        assert codes(monkeypatch, "") == ["PL", "DE"]

    def test_one_market_turns_the_others_off(self, monkeypatch):
        assert codes(monkeypatch, "ES") == ["ES"]

    def test_it_also_turns_markets_ON(self, monkeypatch):
        """Іспанія в конфізі позначена enabled: false, і змінна це перебиває.

        Інакше вмикати її довелось би правкою в спільному файлі, тобто
        розгортання для Іспанії вимагало б власної гілки.
        """
        assert "ES" in codes(monkeypatch, "pl,es")

    def test_case_and_spaces_do_not_matter(self, monkeypatch):
        assert codes(monkeypatch, " es , PL ") == ["PL", "ES"]


class TestRefusals:
    def test_an_unknown_market_is_refused_loudly(self, monkeypatch):
        """Мовчки проігнорувати означає запустити бота не на тому ринку і
        дізнатись про це з тишини в Telegram."""
        monkeypatch.setenv("MARKETS", "ES,FR")
        with pytest.raises(ValueError, match="FR"):
            load_settings()

    def test_the_error_lists_what_is_available(self, monkeypatch):
        monkeypatch.setenv("MARKETS", "XX")
        with pytest.raises(ValueError, match="ES"):
            load_settings()


class TestSpain:
    """Іспанія має бути повністю описана в конфізі, а не наполовину."""

    def test_spain_is_configured_but_off_by_default(self, monkeypatch):
        monkeypatch.delenv("MARKETS", raising=False)
        es = next(m for m in load_settings().markets if m.code == "ES")
        assert es.enabled is False, "головний бот не має платити за третій ринок"
        assert es.host == "www.vinted.es"
        assert es.currency == "EUR"
        assert es.shipping_eur > 0, "без доставки дешеві категорії оцінюються брехливо"
        assert es.locale.startswith("es")


class TestSpanishStringsAreKnown:
    """Рядки, зняті з живої сторінки vinted.es, мусять упізнаватись.

    Усі пʼять станів і "Talla única" взяті з реального розбору 96 лотів у
    категорії футболок. Якщо тест упаде - значить Vinted змінив
    формулювання, і іспанський ринок осліп саме так, як колись осліп
    польський: 126 лотів за 25 хвилин у відсів з поміткою "невідомий стан".
    """

    @pytest.mark.parametrize(
        "title",
        ["Nuevo con etiquetas", "Nuevo sin etiquetas", "Muy bueno",
         "Bueno", "Satisfactorio"],
    )
    def test_every_spanish_condition_resolves(self, title):
        from vintsniper.engine.conditions import StatusMap
        m = StatusMap("ES", {})
        assert m.status_id(title) is not None, f"стан {title!r} не впізнається"

    def test_the_spanish_one_size_label_is_known(self):
        import yaml
        raw = yaml.safe_load((CONFIG_DIR / "categories.yaml").read_text())
        labels = raw["sizes"]["one_size_labels"]
        assert "Talla única" in labels
