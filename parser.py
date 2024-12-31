import requests
from bs4 import BeautifulSoup
from typing import List


def find_name_and_price(base_url: str, start_path: str) -> List[tuple]:
    """
    Парсит данные с сайта и возвращает список кортежей с названиями и ценами товаров.

    Args:
        base_url (str): Основной URL сайта.
        start_path (str): Путь до категории товаров.

    Returns:
        List[tuple]: Список кортежей с названиями и ценами товаров.
    """
    url = base_url + start_path
    result = []
    while url:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, "lxml")
        articles = soup.find_all("article", class_="l-product")
        for article in articles:
            name = article.find("span", itemprop="name")
            price = article.find("span", itemprop="price")

            if name and price:
                result.append((name.text.strip(), int(price.text.strip())))

        next_page = soup.find("a", id="navigation_2_next_page")
        url = base_url + next_page["href"] if next_page else None
    return result
