"""Разовый интерактивный логин в кабинет Prom.ua.

Запусти НА СВОЁМ КОМПЬЮТЕРЕ (не на сервере без экрана):
    python save_session.py

Откроется окно браузера. Залогинься вручную (включая 2FA/SMS),
дойди до страницы кошелька и нажми Enter в терминале.
Скрипт сохранит сессию в state.json — скопируй этот файл на VPS рядом с ботом.
"""
import asyncio

from playwright.async_api import async_playwright

import config


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto("https://my.prom.ua/", wait_until="domcontentloaded")
        print("\n=== Залогинься в открывшемся браузере ===")
        print("Пройди вход (логин, пароль, 2FA/SMS при наличии),")
        print(f"открой страницу кошелька: {config.PROM_BALANCE_URL}")
        input("Когда увидишь баланс — вернись сюда и нажми Enter... ")
        await context.storage_state(path=config.SESSION_FILE)
        print(f"\nСессия сохранена в {config.SESSION_FILE}")
        print("Скопируй этот файл на сервер рядом с ботом.")
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
