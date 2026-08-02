"""Точный баланс из кабинета my.prom.ua через Playwright.

Рекомендуемый путь: один раз залогиниться интерактивно через save_session.py,
он сохранит cookies в state.json. Дальше бот работает headless, переиспользуя сессию.
Это корректно работает и с 2FA.
"""
import os
import re

from playwright.async_api import async_playwright

import config


def _parse_balance(text: str) -> float | None:
    """Найти число баланса в тексте страницы по регэкспу из конфига."""
    m = re.search(config.PROM_BALANCE_REGEX, text)
    if not m:
        return None
    raw = m.group(1)
    raw = (
        raw.replace(" ", "")
        .replace(" ", "")
        .replace(" ", "")
        .replace("−", "-")
        .replace(",", ".")
    )
    try:
        return float(raw)
    except ValueError:
        return None


async def get_balance_from_cabinet() -> float:
    """Открыть страницу кошелька и вытащить баланс. Бросает исключение при неудаче."""
    if not os.path.exists(config.SESSION_FILE):
        raise RuntimeError(
            f"Нет сохранённой сессии ({config.SESSION_FILE}). "
            f"Запусти: python save_session.py"
        )

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(storage_state=config.SESSION_FILE)
        page = await context.new_page()
        try:
            await page.goto(config.PROM_BALANCE_URL, wait_until="networkidle", timeout=45000)
            # если сессия протухла и нас редиректнуло на логин
            if "login" in page.url.lower() or "auth" in page.url.lower():
                raise RuntimeError(
                    "Сессия истекла — перелогинься через save_session.py"
                )
            body = await page.inner_text("body")
            balance = _parse_balance(body)
            if balance is None:
                raise RuntimeError(
                    "Не удалось найти баланс на странице. "
                    "Проверь PROM_BALANCE_URL и PROM_BALANCE_REGEX."
                )
            # переписываем сессию свежими куками — так вход "1 раз" живёт долго
            try:
                await context.storage_state(path=config.SESSION_FILE)
            except Exception:  # noqa: BLE001
                pass
            return balance
        finally:
            await context.close()
            await browser.close()
