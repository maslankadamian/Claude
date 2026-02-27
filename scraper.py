"""
Moduł scrapingu danych z TGE (Towarowa Giełda Energii).
Używa Selenium do obsługi JavaScript i obejścia zabezpieczeń 403.
"""
import logging
import time
from datetime import datetime
from typing import Optional

import pandas as pd
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager

logger = logging.getLogger(__name__)


def _build_driver(config: dict) -> webdriver.Chrome:
    """Tworzy i konfiguruje instancję ChromeDriver."""
    scraping_cfg = config.get("scraping", {})
    options = Options()

    if scraping_cfg.get("headless", True):
        options.add_argument("--headless=new")

    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)

    user_agent = scraping_cfg.get(
        "user_agent",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    )
    options.add_argument(f"--user-agent={user_agent}")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--lang=pl-PL")

    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=options)

    # Ukryj flagę webdriver
    driver.execute_cdp_cmd(
        "Page.addScriptToEvaluateOnNewDocument",
        {
            "source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        },
    )
    return driver


def _extract_tables_from_html(
    html: str, url: str, fetch_time: datetime, date_column: str
) -> list[pd.DataFrame]:
    """
    Parsuje HTML i zwraca listę DataFrame-ów – po jednym na każdą tabelę.
    Do każdego rekordu dodaje kolumnę z datą pobrania i URL źródła.
    """
    soup = BeautifulSoup(html, "lxml")
    tables = soup.find_all("table")
    result = []

    for idx, table in enumerate(tables):
        try:
            dfs = pd.read_html(str(table))
            if not dfs:
                continue
            df = dfs[0]
            if df.empty or df.shape[1] < 1:
                continue

            # Spłaszcz wielopoziomowe nagłówki
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = [
                    " | ".join(str(c) for c in col).strip() for col in df.columns
                ]

            df.columns = [str(c).strip() for c in df.columns]

            # Metadane
            df[date_column] = fetch_time.strftime("%Y-%m-%d %H:%M:%S")
            df["Zrodlo_URL"] = url
            df["Numer_Tabeli"] = idx + 1

            # Usuń całkowicie puste wiersze
            df.dropna(how="all", inplace=True)

            if not df.empty:
                result.append(df)
                logger.debug(
                    "Pobrano tabelę %d z %s (%d wierszy, %d kolumn)",
                    idx + 1, url, len(df), df.shape[1],
                )
        except Exception as exc:
            logger.warning("Nie można sparsować tabeli %d z %s: %s", idx + 1, url, exc)

    return result


def scrape_url(
    driver: webdriver.Chrome,
    url: str,
    config: dict,
    fetch_time: datetime,
) -> list[pd.DataFrame]:
    """
    Otwiera URL w Selenium, czeka na załadowanie JS i ekstrahuje tabele.
    Zwraca listę DataFrame-ów.
    """
    scraping_cfg = config.get("scraping", {})
    page_load_timeout = scraping_cfg.get("page_load_timeout", 30)
    wait_for_js = scraping_cfg.get("wait_for_js", 5)
    retry_count = scraping_cfg.get("retry_count", 3)
    date_column = config.get("data", {}).get("date_column", "Data_Pobrania")

    for attempt in range(1, retry_count + 1):
        try:
            logger.info("Pobieranie %s (próba %d/%d)", url, attempt, retry_count)
            driver.set_page_load_timeout(page_load_timeout)
            driver.get(url)

            # Czekaj na załadowanie dynamicznych tabel
            try:
                WebDriverWait(driver, wait_for_js).until(
                    EC.presence_of_element_located((By.TAG_NAME, "table"))
                )
            except Exception:
                logger.debug("Brak tabeli w ciągu %ds, próbuję mimo to...", wait_for_js)

            # Dodatkowe oczekiwanie na wykonanie JS
            time.sleep(2)

            # Przewiń stronę w dół, aby aktywować lazy-load
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(1)
            driver.execute_script("window.scrollTo(0, 0);")
            time.sleep(1)

            html = driver.page_source
            tables = _extract_tables_from_html(html, url, fetch_time, date_column)

            if tables:
                logger.info(
                    "Pobrano %d tabeli(e) z %s", len(tables), url
                )
                return tables
            else:
                logger.warning("Nie znaleziono tabel na %s", url)
                return []

        except Exception as exc:
            logger.error("Błąd przy pobieraniu %s (próba %d): %s", url, attempt, exc)
            if attempt < retry_count:
                wait = 2 ** attempt
                logger.info("Czekam %ds przed kolejną próbą...", wait)
                time.sleep(wait)

    logger.error("Wszystkie próby pobrania %s nieudane.", url)
    return []


def scrape_all(config: dict) -> dict[str, list[pd.DataFrame]]:
    """
    Główna funkcja scrapingu.
    Pobiera dane ze wszystkich URL-i z konfiguracji.
    Zwraca słownik {url: [DataFrame, ...]}
    """
    urls = config.get("scraping", {}).get("urls", [])
    fetch_time = datetime.now()
    results: dict[str, list[pd.DataFrame]] = {}

    if not urls:
        logger.error("Brak URL-i w konfiguracji (scraping.urls).")
        return results

    driver = None
    try:
        driver = _build_driver(config)

        for url in urls:
            tables = scrape_url(driver, url, config, fetch_time)
            results[url] = tables

    finally:
        if driver:
            driver.quit()
            logger.debug("ChromeDriver zamknięty.")

    total_tables = sum(len(v) for v in results.values())
    logger.info(
        "Scraping zakończony. Pobrano łącznie %d tabeli(e) z %d stron.",
        total_tables, len(urls),
    )
    return results
