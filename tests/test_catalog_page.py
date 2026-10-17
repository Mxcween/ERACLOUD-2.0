"""Розбір сторінки каталогу.

17 вересня 2026 Vinted вимкнув /api/v2/catalog/items - 404 на всіх ринках
одночасно. Дані лишились у розмітці сторінки, і тепер бот читає їх звідти.
Зразки в tests/fixtures зняті з живих сторінок PL і DE того ж дня.
"""
from __future__ import annotations

import pathlib

import pytest

from vintsniper.vinted.catalog_page import parse_catalog, split_label

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
CONDITIONS_PL = ["Nowy z metką", "Nowy bez metki", "Bardzo dobry", "Dobry", "Zadowalający"]
CONDITIONS_DE = ["Neu, mit Etikett", "Neu, ohne Etikett", "Sehr gut", "Gut", "Zufriedenstellend"]
BRANDS = {"nike", "adidas", "carhartt", "stone island"}


def known_brand(value: str) -> bool:
    return value.casefold() in BRANDS


class TestSplitLabel:
    def test_polish_label(self):
        title, fields, prices = split_label(
            "Czerwona kamizelka Nike z kapturem rozmiar L, "
            "Marka: Nike, Stan: Dobry, Rozmiar: L, 94.99 zł, 102.64 zł"
        )
        assert title == "Czerwona kamizelka Nike z kapturem rozmiar L"
        assert fields == {"Marka": "Nike", "Stan": "Dobry", "Rozmiar": "L"}
        assert prices == [94.99, 102.64]

    def test_a_value_may_contain_a_comma(self):
        """Німецький "Neu, mit Etikett" ламає будь-який розбір по комах."""
        title, fields, prices = split_label(
            "Nike therma-fit sans manche adv, Marke: Nike, "
            "Zustand: Neu, mit Etikett, Größe: M, 55.00 €, 58.45 €"
        )
        assert title == "Nike therma-fit sans manche adv"
        assert fields["Zustand"] == "Neu, mit Etikett"
        assert fields["Größe"] == "M"
        assert prices == [55.00, 58.45]

    def test_html_entities_come_back_as_characters(self):
        title, fields, _ = split_label(
            "Kurtka, Marka: Arc&#x27;teryx, Stan: Dobry, Rozmiar: L, 10 zł, 11 zł"
        )
        assert fields["Marka"] == "Arc'teryx"

    def test_thousands_and_decimal_commas(self):
        _, _, prices = split_label("Kurtka, Marka: Nike, Rozmiar: L, 1 234,50 zł, 1 300,00 zł")
        assert prices == [1234.50, 1300.00]

    def test_a_label_without_prices_yields_none(self):
        _, _, prices = split_label("Kurtka Nike, Marka: Nike, Rozmiar: L")
        assert prices == []


@pytest.mark.parametrize(
    "fixture,conditions,currency",
    [("catalog_pl.html", CONDITIONS_PL, "PLN"), ("catalog_de.html", CONDITIONS_DE, "EUR")],
)
class TestRealPages:
    def _parse(self, fixture, conditions, currency):
        page = (FIXTURES / fixture).read_text(encoding="utf-8")
        return parse_catalog(
            page, market_code="PL", base_url="https://www.vinted.pl",
            catalog_id=1206, server_ts=1700000000, currency=currency,
            known_conditions=conditions, is_known_brand=known_brand,
        )

    def test_every_item_is_found_once(self, fixture, conditions, currency):
        items = self._parse(fixture, conditions, currency)
        assert len(items) == 6, f"очікували 6 лотів, вийшло {len(items)}"
        assert len({i.item_id for i in items}) == 6, "лоти не мають дублюватись"

    def test_each_item_is_complete(self, fixture, conditions, currency):
        for item in self._parse(fixture, conditions, currency):
            assert item.item_id > 0
            assert item.title, "порожня назва"
            assert item.brand_title, f"нема бренду: {item.title}"
            assert item.status_title, f"нема стану: {item.title}"
            assert item.price > 0
            assert item.total_price >= item.price, "ціна з захистом не може бути меншою"
            assert item.url.startswith("https://www.vinted.pl/items/")
            assert item.photo_url and item.photo_url.startswith("https://images")
            assert item.currency == currency

    def test_condition_is_recognised_not_guessed(self, fixture, conditions, currency):
        """Стан має збігтись зі списком, інакше лот відсіється як невідомий."""
        known = {c.casefold() for c in conditions}
        for item in self._parse(fixture, conditions, currency):
            assert item.status_title.casefold() in known, item.status_title

    def test_brand_lands_in_the_brand_field(self, fixture, conditions, currency):
        for item in self._parse(fixture, conditions, currency):
            assert item.brand_title.casefold() in BRANDS, item.brand_title


class TestMissingData:
    """Чого в розмітці немає - і що через це перестає працювати."""

    def test_seller_and_upload_time_are_absent(self):
        page = (FIXTURES / "catalog_pl.html").read_text(encoding="utf-8")
        items = parse_catalog(
            page, market_code="PL", base_url="https://www.vinted.pl",
            catalog_id=1206, server_ts=1700000000, currency="PLN",
            known_conditions=CONDITIONS_PL, is_known_brand=known_brand,
        )
        assert all(i.seller_id is None for i in items)
        assert all(i.uploaded_ts is None for i in items)

    def test_rubbish_page_yields_nothing_rather_than_raising(self):
        assert parse_catalog(
            "<html><body>нічого тут немає</body></html>",
            market_code="PL", base_url="https://www.vinted.pl", catalog_id=1206,
            server_ts=1700000000, currency="PLN",
            known_conditions=CONDITIONS_PL, is_known_brand=known_brand,
        ) == []
