"""Скрипт калібрування переписує config/categories.yaml, тому перевіряємо,
що він міняє рівно те, що треба, і не ламає решту файлу."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def calibrate():
    sys.path.insert(0, str(ROOT / "src"))
    spec = importlib.util.spec_from_file_location("calibrate", ROOT / "scripts" / "calibrate.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def config_copy(tmp_path: Path) -> Path:
    target = tmp_path / "categories.yaml"
    target.write_text((ROOT / "config" / "categories.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    return target


class TestRewrite:
    def test_updates_only_the_named_category(self, calibrate, config_copy, settings):
        before = yaml.safe_load(config_copy.read_text(encoding="utf-8"))
        applied = calibrate._rewrite(config_copy, {"outerwear": {"S": 200, "A": 60, "B": 30}}, settings)
        after = yaml.safe_load(config_copy.read_text(encoding="utf-8"))

        assert applied == 1
        by_key = {c["key"]: c for c in after["categories"]}
        assert by_key["outerwear"]["baseline_eur"] == {"S": 200, "A": 60, "B": 30}

        untouched = {c["key"]: c for c in before["categories"]}
        for key in untouched:
            if key != "outerwear":
                assert by_key[key]["baseline_eur"] == untouched[key]["baseline_eur"]

    def test_keeps_missing_tiers_at_current_value(self, calibrate, config_copy, settings):
        current = next(c for c in settings.categories if c.key == "shoes").baseline_eur
        calibrate._rewrite(config_copy, {"shoes": {"B": 40}}, settings)
        after = yaml.safe_load(config_copy.read_text(encoding="utf-8"))
        shoes = next(c for c in after["categories"] if c["key"] == "shoes")
        assert shoes["baseline_eur"]["B"] == 40
        assert shoes["baseline_eur"]["S"] == int(current["S"])
        assert shoes["baseline_eur"]["A"] == int(current["A"])

    def test_file_stays_valid_and_comments_survive(self, calibrate, config_copy, settings):
        calibrate._rewrite(config_copy, {"jeans": {"S": 31, "A": 21, "B": 17}}, settings)
        text = config_copy.read_text(encoding="utf-8")
        assert "# ceiling_eur" in text, "коментарі мають лишитись на місці"
        parsed = yaml.safe_load(text)
        assert len(parsed["categories"]) == len(settings.categories)
        assert parsed["conditions"]["accepted_ids"] == [6, 1, 2, 3]

    def test_unknown_category_changes_nothing(self, calibrate, config_copy, settings):
        original = config_copy.read_text(encoding="utf-8")
        assert calibrate._rewrite(config_copy, {"nope": {"S": 1}}, settings) == 0
        assert config_copy.read_text(encoding="utf-8") == original
