"""Розбір сторінки каталогу Vinted.

17 вересня 2026 Vinted вимкнув /api/v2/catalog/items: 404 на всіх ринках
одночасно (PL, DE, FR, UK, com). Сайт переїхав на серверний рендеринг, і в
його бандлах не лишилось жодного шляху /api/ - публічного REST більше немає.

Але дані нікуди не поділись. Сторінка каталогу віддає ті самі 96 лотів, і
все потрібне лежить у розмітці:

    data-testid="product-item-id-10034840261"    -> id лота
    <img src="https://images1.vinted.net/...">   -> фото
    <a href="/items/10034840261-...">            -> посилання
    title="Kurtka Nike, Marka: Nike, Stan: Dobry, Rozmiar: L, 94.99 zł, 102.64 zł"

Останній рядок - підпис для читалок екрана, і в ньому є назва, бренд, стан,
розмір і обидві ціни. Фільтри в URL (catalog[], brand_ids[], status_ids[],
order, page) працюють так само, як працювали в API, тож звужувати видачу ми
й далі можемо на боці Vinted.

Чого в розмітці НЕМА, на відміну від старого API: id продавця, кількість
вподобань і час завантаження фото. Тому ліміт "той самий продавець" і
фільтр за віком лота на цьому джерелі не працюють - див. коментарі в
runner.py біля відповідних місць.
"""
from __future__ import annotations

import html as html_module
import logging
import re
from typing import Iterable

from ..models import Listing

log = logging.getLogger(__name__)

# Один лот: id у data-testid, далі все інше до наступного такого ж блоку
_ITEM_BLOCK = re.compile(
    r'data-testid="product-item-id-(?P<id>\d+)"(?P<body>.*?)'
    r'(?=data-testid="product-item-id-\d+"|\Z)',
    re.S,
)
_LABEL = re.compile(r'title="(?P<label>[^"]{10,400})"')
_PHOTO = re.compile(r'<img[^>]+src="(?P<url>https://images\d*\.vinted\.net/[^"]+)"')
_HREF = re.compile(r'href="(?P<path>/items/\d+[^"?#]*)')
# Ціна: число з пробілами-роздільниками тисяч і будь-якою валютою поруч
_PRICE = re.compile(
    r"(?P<amount>\d[\d  ]*(?:[.,]\d{1,2})?)\s*"
    r"(?P<currency>zł|€|£|Kč|kr|Ft|lei|лв)",
)
# Мітка виду "Marka: ", "Zustand: ", "Größe: " на початку або після коми
_FIELD = re.compile(r"(?:\A|,\s*)(?P<name>[^,:]{2,25}):\s*")


def _to_float(raw: str) -> float | None:
    cleaned = raw.replace(" ", "").replace(" ", "")
    # "1.234,56" -> кома десяткова; "1,234.56" -> крапка десяткова
    if "," in cleaned and "." in cleaned:
        cleaned = (
            cleaned.replace(".", "").replace(",", ".")
            if cleaned.rindex(",") > cleaned.rindex(".")
            else cleaned.replace(",", "")
        )
    else:
        cleaned = cleaned.replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


def split_label(label: str) -> tuple[str, dict[str, str], list[float]]:
    """Розбирає підпис лота на назву, пари "мітка: значення" і ціни.

    Мітки локалізовані ("Marka" / "Marke" / "Marque"), тому назви міток ми
    не знаємо і знати не хочемо. А ось значення можуть містити кому: на
    німецькому ринку стан пишеться "Neu, mit Etikett". Тому спершу зрізаємо
    з хвоста ціни, а решту ріжемо саме по мітках, а не по комах.
    """
    text = html_module.unescape(label).strip()

    prices: list[float] = []
    tail = len(text)
    for match in reversed(list(_PRICE.finditer(text))):
        if match.end() < tail - 2:  # ціни йдуть суцільним хвостом
            break
        value = _to_float(match.group("amount"))
        if value is None:
            break
        prices.insert(0, value)
        tail = match.start()
        while tail > 0 and text[tail - 1] in ", ":
            tail -= 1
    head = text[:tail]

    fields: dict[str, str] = {}
    marks = list(_FIELD.finditer(head))
    title = head[: marks[0].start()].rstrip(", ") if marks else head
    for i, mark in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(head)
        fields[mark.group("name").strip()] = head[mark.end(): end].strip().rstrip(",")
    return title, fields, prices


def _classify(
    fields: dict[str, str],
    known_conditions: Iterable[str],
    is_known_brand,
) -> tuple[str, str, str]:
    """Хто з полів бренд, хто стан, хто розмір.

    За назвою мітки визначати не можна - вона іншою мовою на кожному ринку.
    Зате порядок сталий: бренд, стан, розмір. А бренд ми впізнаємо точно, за
    реєстром, тому далі все стає на місця саме від нього.

    Спиратись на список відомих станів як на головний спосіб не вийшло:
    німецький ринок віддає "Neu, mit Etikett" і просто "Neu", яких у
    запасному словнику не було, і стан у чверті лотів губився. Тепер список
    станів - лише запасний варіант, коли бренду в реєстрі немає.
    """
    values = [v.strip() for v in fields.values() if v and v.strip()]
    if not values:
        return "", "", ""

    for i, value in enumerate(values):
        if is_known_brand(value):
            rest = values[i + 1:]
            return value, (rest[0] if rest else ""), (rest[1] if len(rest) > 1 else "")

    # Бренду не знаємо: тоді хапаємось за стан, якщо він упізнається.
    conditions = {c.casefold() for c in known_conditions}
    for i, value in enumerate(values):
        if value.casefold() in conditions:
            before = values[:i]
            after = values[i + 1:]
            return (before[-1] if before else ""), value, (after[0] if after else "")

    # І нарешті просто за порядком: бренд, стан, розмір.
    padded = (values + ["", "", ""])[:3]
    return padded[0], padded[1], padded[2]


def parse_catalog(
    page: str,
    *,
    market_code: str,
    base_url: str,
    catalog_id: int,
    server_ts: int,
    currency: str,
    known_conditions: Iterable[str],
    is_known_brand,
) -> list[Listing]:
    """Усі лоти зі сторінки каталогу."""
    listings: list[Listing] = []
    seen: set[int] = set()
    for block in _ITEM_BLOCK.finditer(page):
        try:
            item_id = int(block.group("id"))
        except (TypeError, ValueError):
            continue
        if item_id in seen:
            # Один лот дає кілька елементів з тим самим data-testid
            # (картинка, посилання, кнопка "в улюблене")
            continue
        body = block.group("body")
        label_match = _LABEL.search(body)
        if label_match is None:
            continue
        seen.add(item_id)

        title, fields, prices = split_label(label_match.group("label"))
        if not prices:
            continue
        brand, condition, size = _classify(fields, known_conditions, is_known_brand)

        photo = _PHOTO.search(body)
        href = _HREF.search(body)
        path = href.group("path") if href else f"/items/{item_id}"

        listings.append(
            Listing(
                item_id=item_id,
                market=market_code,
                catalog_id=catalog_id,
                title=title,
                brand_title=brand,
                size_title=size,
                status_title=condition,
                status_id=None,
                price=min(prices),
                total_price=max(prices),
                currency=currency,
                url=f"{base_url}{path}",
                photo_url=photo.group("url") if photo else None,
                # Розмітка їх не несе: продавця не видно взагалі, вподобання
                # тільки як стан кнопки, а часу завантаження немає.
                seller_id=None,
                seller_login=None,
                seller_is_business=False,
                favourite_count=0,
                view_count=0,
                uploaded_ts=None,
                seen_ts=server_ts,
            )
        )
    return listings
