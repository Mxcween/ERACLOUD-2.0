"""Тихі години."""
from __future__ import annotations

from typing import Iterable, Sequence


def in_quiet_hours(windows: Iterable[Sequence[int]] | None, hour: int) -> bool:
    """Чи потрапляє година у якесь із вікон тиші.

    Вікно [1, 8] це з 01:00 до 08:00. Вікно [22, 6] перетинає північ і означає
    з 22:00 до 06:00.
    """
    if not windows:
        return False
    for window in windows:
        try:
            start, end = int(window[0]), int(window[1])
        except (TypeError, ValueError, IndexError, KeyError):
            continue
        if start == end:
            continue
        if start < end:
            if start <= hour < end:
                return True
        elif hour >= start or hour < end:
            return True
    return False
