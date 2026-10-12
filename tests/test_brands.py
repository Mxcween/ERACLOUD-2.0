from pathlib import Path

from vintsniper.settings import CONFIG_DIR
from vintsniper.vinted.brands import BrandRegistry, ResolvedBrand, _normalise, pick_best_match


class TestNormalise:
    def test_strips_diacritics(self):
        assert _normalise("Stüssy") == _normalise("Stussy")
        assert _normalise("Aimé Leon Dore") == _normalise("Aime Leon Dore")

    def test_strips_punctuation(self):
        assert _normalise("C.P. Company") == "cpcompany"
        assert _normalise("Levi's") == "levis"
        assert _normalise("Arc'teryx") == "arcteryx"


class TestPickBestMatch:
    def test_prefers_exact_title(self):
        candidates = [
            {"id": 362, "title": "Carhartt", "item_count": 3_784_861},
            {"id": 872289, "title": "Carhartt WIP", "item_count": 111_931},
        ]
        assert pick_best_match("Carhartt", candidates)["id"] == 362
        assert pick_best_match("Carhartt WIP", candidates)["id"] == 872289

    def test_ignores_collaborations(self):
        candidates = [
            {"id": 7525411, "title": "Stone Island x New Balance", "item_count": 4100},
            {"id": 73306, "title": "Stone Island", "item_count": 890_200},
        ]
        assert pick_best_match("Stone Island", candidates)["id"] == 73306

    def test_returns_none_when_nothing_matches(self):
        assert pick_best_match("Nike", [{"id": 1, "title": "Zara", "item_count": 5}]) is None
        assert pick_best_match("Nike", []) is None


class TestRegistry:
    def test_lookup_by_title_and_id(self):
        registry = BrandRegistry(
            [ResolvedBrand(name="Nike", brand_id=53, vinted_title="Nike", tier="B", replica_risk="high")]
        )
        assert registry.by_title("nike").brand_id == 53
        assert registry.by_title("NIKE").brand_id == 53
        assert registry.by_id(53).name == "Nike"
        assert registry.by_title("Zara") is None

    def test_shipped_cache_covers_every_configured_brand(self, settings):
        """У репо лежить готовий кеш, бот має стартувати без resolve_brands."""
        registry = BrandRegistry.load(CONFIG_DIR / "brand_ids.json")
        assert registry is not None
        assert len(registry) == len(settings.brands)
        for brand in settings.brands:
            assert registry.by_title(brand.name) is not None, brand.name

    def test_roundtrip_through_disk(self, tmp_path: Path):
        original = BrandRegistry(
            [ResolvedBrand(name="Nike", brand_id=53, vinted_title="Nike", tier="B", replica_risk="high")]
        )
        target = tmp_path / "brand_ids.json"
        original.save(target)
        restored = BrandRegistry.load(target)
        assert restored.by_id(53).name == "Nike"

    def test_missing_cache_returns_none(self, tmp_path: Path):
        assert BrandRegistry.load(tmp_path / "nope.json") is None


class TestTheShortList:
    """Список брендів скорочено до тих, яких знає кожен.

    До цього тут було 76 назв, відібраних за виміряним попитом. Відбір був
    правильний, але на неправильне питання: він питав "чи розлітається це на
    Vinted", а власнику треба "чи впізнаю я це, не гуглячи". Половина списку
    не проходила саме цю перевірку, і алерти по ній виглядали як шум.
    """

    def _names(self):
        import json

        from vintsniper.settings import CONFIG_DIR
        return [b["name"] for b in json.loads((CONFIG_DIR / "brand_ids.json").read_text())["brands"]]

    def test_the_two_files_agree(self):
        """brands.yaml редагують руками, brand_ids.json - той, що працює.

        Розійтись вони можуть мовчки: бот прочитає лише json і спокійно
        шукатиме бренд, якого в конфізі вже немає, або навпаки пропустить
        доданий. Видно це буде тільки по дивних алертах.
        """
        from vintsniper.settings import load_settings
        yaml_names = {b.name for b in load_settings().brands}
        assert yaml_names == set(self._names())

    def test_the_brands_the_owner_asked_to_drop_are_gone(self):
        for name in ("Carhartt", "Carhartt WIP", "Timberland", "Napapijri"):
            assert name not in self._names(), f"{name} прибрано на пряме прохання"

    def test_the_big_names_are_all_there(self):
        """Саме на цих тримається обсяг: Nike і Adidas - чверть усієї стрічки."""
        for name in ("Nike", "Adidas", "Jordan", "Puma", "New Balance",
                     "The North Face", "Levi's", "Calvin Klein"):
            assert name in self._names()

    def test_the_list_stays_short(self):
        """Стеля навмисна. Список росте непомітно, по одному бренду за раз,
        і саме так він доріс до 76 назв минулого разу."""
        assert len(self._names()) <= 30, "більше тридцяти - це вже не 'самі ходові'"
