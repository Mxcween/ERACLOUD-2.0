"""Колір з назви: ходові пропускаємо, яскраві відсіюємо, без кольору не чіпаємо."""
from __future__ import annotations

import pytest

from vintsniper.engine.colours import colour_rejection, colours_in


@pytest.mark.parametrize("title", [
    "Nike Hoodie Black - M",
    "Bluza niebieska Nike",
    "Pullover weiß Stone Island",
    "Kurtka fioletowa Carhartt",
    "Pullover rot Carhartt",        # червоний ходовий
    "Top rosa Nike",                # рожевий ходовий
    "Jacket Black/Orange",          # ходовий колір є в назві - пропускаємо
    "Stone Island Compass",         # кольору нема - пропускаємо
])
def test_allowed_colours_pass(title):
    assert colour_rejection(title) is None


@pytest.mark.parametrize("title, colour", [
    ("Nike Hoodie Orange - M", "orange"),
    ("Kurtka zielona The North Face", "green"),
    ("Hoodie gelb Nike", "yellow"),
    ("Pulli grau Nike", "grey"),
])
def test_loud_colours_are_rejected(title, colour):
    assert colour in colours_in(title)
    assert colour_rejection(title) is not None


def test_allowed_list_is_configurable(monkeypatch):
    monkeypatch.setenv("ALLOWED_COLOURS", "orange")
    assert colour_rejection("Hoodie Orange") is None
    assert colour_rejection("Hoodie Black") is not None
