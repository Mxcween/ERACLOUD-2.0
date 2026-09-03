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
