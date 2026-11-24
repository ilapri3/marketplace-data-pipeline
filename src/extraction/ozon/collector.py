import json
import re
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit, parse_qsl, urlencode

import pandas as pd
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright


#запуск хрома
# /Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome \
# --remote-debugging-port=9222 \
# --user-data-dir="$HOME/chrome-ozon-debug"

# Настройки сбора
SEARCH_QUERY = "беспроводные наушники"
MAX_PAGES = 10

CDP_URL = "http://127.0.0.1:9222"
BASE_URL = "https://www.ozon.ru"

OUTPUT_FILE = Path(__file__).resolve().parents[3] / "data" / "raw" / "ozon" / "ozon_products.csv"

# Столбцы итогового CSV
COLUMNS = [
    "sku",
    "name",
    "price",
    "original_price",
    "discount_percent",
    "rating",
    "review_count",
    "product_url",
    "marketplace",
    "search_query",
    "collection_id",
    "collected_at",
    "page_number",
    "position",
    "source_url",
]


def get_number(value):
    """Извлекает целое число из строки."""
    if value is None:
        return None

    digits = re.sub(r"\D", "", str(value))

    if not digits:
        return None

    return int(digits)


def get_rating(value):
    """Извлекает рейтинг с дробной частью."""
    if value is None:
        return None

    match = re.search(r"\d+[,.]?\d*", str(value))

    if not match:
        return None

    return float(match.group(0).replace(",", "."))



def extract_product(item):
    """Извлекает данные одного товара из структуры Ozon."""

    sku = item.get("sku") or item.get("id")

    name = None
    price = None
    original_price = None
    discount_percent = None
    rating = None
    review_count = None

    main_state = item.get("mainState", [])

    for block in main_state:

        # Название товара
        if block.get("type") == "textDS" and block.get("id") == "name":
            text_data = block.get("textDS", {})
            name = text_data.get("text")

        # Цена, старая цена и скидка
        if block.get("type") == "priceV2":
            price_data = block.get("priceV2", {})

            for price_item in price_data.get("price", []):

                text_style = price_item.get("textStyle")
                text = price_item.get("text")

                if text_style == "PRICE":
                    price = get_number(text)

                elif text_style == "ORIGINAL_PRICE":
                    original_price = get_number(text)

            discount_percent = get_number(
                price_data.get("discount")
            )

        # Рейтинг и количество отзывов
        if block.get("type") == "labelListV2":
            label_data = block.get("labelListV2", {})

            labels = [
                x.get("text", {}).get("text", "")
                for x in label_data.get("items", [])
                if x.get("type") == "text"
            ]

            label_json = json.dumps(
                label_data,
                ensure_ascii=False
            ).lower()

            if "ic_s_star" in label_json and labels:
                rating = get_rating(labels[0])

                if len(labels) > 1:
                    review_count = get_number(labels[1])

    # Ссылка на товар
    product_url = urljoin(
        BASE_URL,
        item.get("action", {}).get("link", "")
    )

    return {
        "sku": sku,
        "name": name,
        "price": price,
        "original_price": original_price,
        "discount_percent": discount_percent,
        "rating": rating,
        "review_count": review_count,
        "product_url": product_url,
    }



def build_first_page_url(current_url):
    """
    Формирует URL первой страницы на основе открытой выдачи.
    Удаляет параметры, относящиеся к пагинации.
    """

    parsed = urlsplit(current_url)

    params = parse_qsl(
        parsed.query,
        keep_blank_values=True
    )

    pagination_params = {
        "page",
        "search_page_state",
    }

    params = [
        (key, value)
        for key, value in params
        if key.lower() not in pagination_params
    ]

    first_page_url = urlunsplit((
        parsed.scheme,
        parsed.netloc,
        parsed.path,
        urlencode(params, doseq=True),
        parsed.fragment
    ))

    return first_page_url


def get_page_number(grid_data, current_url):
    """Определяет номер текущей страницы."""

    page_number = grid_data.get("page")

    if page_number is not None:
        return int(page_number)

    # Если номер отсутствует в data-state,
    # пробуем определить его по URL.
    query_params = dict(
        parse_qsl(urlsplit(current_url).query)
    )

    return int(query_params.get("page", 1))


def check_antibot(page):
    """Проверяет, не появилась ли страница антибот-защиты."""

    page_text = page.locator("body").inner_text().lower()

    blocked_phrases = [
        "antibot challenge",
        "доступ ограничен",
    ]

    for phrase in blocked_phrases:
        if phrase in page_text:
            raise RuntimeError(
                "Ozon показал страницу проверки доступа. "
                "Сбор остановлен."
            )


def read_page(page):
    """Читает товары и ссылку на следующую страницу."""

    # Ждём появления блока с товарами
    page.locator(
        '[id^="state-tileGridDesktop"]'
    ).first.wait_for(
        state="attached",
        timeout=30000
    )

    check_antibot(page)

    # Получаем HTML страницы
    html = page.content()
    soup = BeautifulSoup(html, "html.parser")

    # Находим блок с товарами
    tile = soup.find(
        id=re.compile(r"^state-tileGridDesktop")
    )

    if not tile:
        raise RuntimeError(
            "Не удалось найти блок с товарами."
        )

    raw_data = tile.get("data-state")

    if not raw_data:
        raise RuntimeError(
            "У блока с товарами отсутствует data-state."
        )

    grid_data = json.loads(raw_data)

    items = grid_data.get("items", [])

    page_number = get_page_number(
        grid_data,
        page.url
    )

    # Получаем URL следующей страницы
    next_page = None

    paginator = soup.find(
        id=re.compile(r"^state-infiniteVirtualPaginator")
    )

    if paginator:
        paginator_data = json.loads(
            paginator.get("data-state", "{}")
        )

        next_page = paginator_data.get("nextPage")

    # Резервный способ получения nextPage
    if not next_page:
        next_page = page.evaluate(
            "() => window.__NUXT__?.state?.nextPage || null"
        )

    return items, page_number, next_page


def save_products(products):
    """Сохраняет все собранные товары в CSV."""

    df = pd.DataFrame(
        products,
        columns=COLUMNS
    )

    df.to_csv(
        OUTPUT_FILE,
        index=False,
        encoding="utf-8-sig"
    )

    print(f"Промежуточный CSV сохранён: {OUTPUT_FILE}")


def main():

    # Идентификаторы одного запуска сбора
    collection_id = str(uuid.uuid4())
    collected_at = datetime.now().isoformat(timespec="seconds")

    products = []
    seen_skus = set()

    with sync_playwright() as p:

        # Подключаемся к уже запущенному Chrome
        browser = p.chromium.connect_over_cdp(CDP_URL)

        # Ищем открытую вкладку Ozon
        ozon_pages = [
            page
            for context in browser.contexts
            for page in context.pages
            if "ozon.ru" in page.url
        ]

        if not ozon_pages:
            raise RuntimeError(
                "Не найдена открытая вкладка Ozon. "
                "Откройте страницу с результатами поиска."
            )

        # Предпочитаем вкладку с каталогом
        page = next(
            (
                tab for tab in ozon_pages
                if "/category/" in tab.url
            ),
            ozon_pages[0]
        )

        print("Подключение к Ozon выполнено.")
        print("Исходная страница:", page.url)

        # Формируем URL первой страницы
        first_page_url = build_first_page_url(page.url)

        print("Переходим на первую страницу:")
        print(first_page_url)

        page.goto(
            first_page_url,
            wait_until="domcontentloaded"
        )

        # Небольшое ожидание загрузки динамических данных
        page.wait_for_timeout(1500)

        # Проверяем, что действительно открылась первая страница
        _, initial_page_number, _ = read_page(page)

        if initial_page_number != 1:
            raise RuntimeError(
                f"Не удалось начать сбор с первой страницы. "
                f"Текущая страница: {initial_page_number}. "
                "Сбор остановлен."
            )

        print("Первая страница успешно открыта.")

        # Последовательно обрабатываем страницы
        for page_index in range(MAX_PAGES):

            items, page_number, next_page = read_page(page)

            expected_page = page_index + 1

            if page_number != expected_page:
                raise RuntimeError(
                    f"Ожидалась страница {expected_page}, "
                    f"но открыта страница {page_number}. "
                    "Сбор остановлен."
                )

            print()
            print(f"Обрабатываем страницу {page_number}")

            new_products = 0

            # position начинается с 1 на каждой странице
            for position, item in enumerate(items, start=1):

                product = extract_product(item)

                sku = product["sku"]

                # Пропускаем товары без SKU
                if sku is None:
                    continue

                # Убираем повторяющиеся товары
                if sku in seen_skus:
                    continue

                seen_skus.add(sku)

                product.update({
                    "marketplace": "Ozon",
                    "search_query": SEARCH_QUERY,
                    "collection_id": collection_id,
                    "collected_at": collected_at,
                    "page_number": page_number,
                    "position": position,
                    "source_url": page.url,
                })

                products.append(product)
                new_products += 1

            print("Новых товаров:", new_products)
            print("Всего собрано:", len(products))

            # Сохраняем CSV после каждой страницы
            save_products(products)

            # Если следующей страницы нет — завершаем сбор
            if not next_page:
                print("Следующая страница отсутствует.")
                break

            # Переходим на следующую страницу
            next_url = urljoin(BASE_URL, next_page)

            print("Переход на следующую страницу:")
            print(next_url)

            page.goto(
                next_url,
                wait_until="domcontentloaded"
            )

            page.wait_for_timeout(1500)

        print()
        print("СБОР ЗАВЕРШЁН")
        print("Всего товаров:", len(products))
        print("Файл:", OUTPUT_FILE)


if __name__ == "__main__":
    main()
