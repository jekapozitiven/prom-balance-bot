"""Одноразовый вход в кабинет Prom.ua прямо из облака через Telegram.

Идея: бот сам открывает страницу входа в облаке (headless-браузер),
вводит логин/пароль из переменных окружения, а когда Prom запрашивает
SMS/2FA-код — просит тебя прислать код в чат. Ты пишешь код сообщением,
бот его подставляет и сохраняет сессию (state.json) на постоянный том.
После этого мониторинг работает сам, повторный вход нужен редко.

Селекторы Prom могут меняться — поэтому используем несколько кандидатов
и мягко деградируем. Если автоматический вход не удался, бот честно сообщит
об этом, и можно временно работать в режиме оценки по API (BALANCE_SOURCE=api).
"""
import asyncio
import logging

from playwright.async_api import async_playwright

import config

log = logging.getLogger("login")

# кандидаты селекторов (первый подходящий — используется)
LOGIN_SELECTORS = [
    "input[name='email']",
    "input[type='email']",
    "input[autocomplete='username']",
    "input[type='tel']",
    "input[name='login']",
]
PASSWORD_SELECTORS = [
    "input[type='password']",
    "input[name='password']",
    "input[autocomplete='current-password']",
]
OTP_SELECTORS = [
    "input[autocomplete='one-time-code']",
    "input[name*='code']",
    "input[name*='otp']",
    "input[id*='code']",
]
SUBMIT_SELECTORS = [
    "button[type='submit']",
    "button:has-text('Увійти')",
    "button:has-text('Войти')",
    "button:has-text('Продовжити')",
    "button:has-text('Далі')",
]


class LoginManager:
    """Держит одно активное состояние входа и мост для OTP-кода."""

    def __init__(self):
        self._otp_future: asyncio.Future | None = None
        self._running = False

    @property
    def running(self) -> bool:
        return self._running

    @property
    def awaiting_otp(self) -> bool:
        return self._otp_future is not None and not self._otp_future.done()

    def submit_otp(self, code: str) -> bool:
        """Передать код из чата в ожидающий процесс входа."""
        if self.awaiting_otp:
            self._otp_future.set_result(code.strip())
            return True
        return False

    async def _first(self, page, selectors, timeout=8000):
        """Найти первый существующий элемент из списка кандидатов."""
        for sel in selectors:
            try:
                el = page.locator(sel).first
                await el.wait_for(state="visible", timeout=timeout)
                return el
            except Exception:  # noqa: BLE001
                continue
        return None

    async def run(self, send) -> bool:
        """Выполнить вход. send — async-функция для сообщений пользователю.

        Возвращает True при успехе.
        """
        if self._running:
            await send("Вход уже выполняется, подожди…")
            return False
        if not config.PROM_LOGIN or not config.PROM_PASSWORD:
            await send(
                "Не заданы PROM_LOGIN / PROM_PASSWORD в переменных окружения."
            )
            return False

        self._running = True
        try:
            return await self._run(send)
        except Exception as e:  # noqa: BLE001
            log.exception("Ошибка входа")
            await send(f"⚠️ Не удалось войти автоматически: {e}\n"
                       f"Можно временно перейти на оценку по API (BALANCE_SOURCE=api).")
            return False
        finally:
            self._running = False
            self._otp_future = None

    async def _run(self, send) -> bool:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context()
            page = await context.new_page()
            try:
                await send("Открываю страницу входа Prom…")
                await page.goto(config.PROM_LOGIN_URL, wait_until="domcontentloaded",
                                timeout=45000)

                # некоторые страницы прячут форму за кнопкой "Вхід"
                for opener in ["text=Вхід", "text=Войти", "text=Увійти"]:
                    try:
                        await page.locator(opener).first.click(timeout=2500)
                        break
                    except Exception:  # noqa: BLE001
                        continue

                login_el = await self._first(page, LOGIN_SELECTORS)
                if not login_el:
                    await send("Не нашёл поле логина на странице входа.")
                    return False
                await login_el.fill(config.PROM_LOGIN)

                pass_el = await self._first(page, PASSWORD_SELECTORS, timeout=4000)
                if pass_el:
                    await pass_el.fill(config.PROM_PASSWORD)
                await self._click_submit(page)

                # иногда пароль на втором шаге
                if not pass_el:
                    pass_el = await self._first(page, PASSWORD_SELECTORS, timeout=8000)
                    if pass_el:
                        await pass_el.fill(config.PROM_PASSWORD)
                        await self._click_submit(page)

                # ждём либо OTP, либо успешный вход
                otp_el = await self._first(page, OTP_SELECTORS, timeout=8000)
                if otp_el:
                    code = await self._ask_otp(send)
                    if code is None:
                        await send("Код не получен вовремя, вход отменён.")
                        return False
                    await otp_el.fill(code)
                    await self._click_submit(page)
                    await page.wait_for_load_state("networkidle", timeout=30000)

                # проверяем, что вошли: открываем кошелёк
                await page.goto(config.PROM_BALANCE_URL, wait_until="networkidle",
                                timeout=45000)
                if "login" in page.url.lower() or "auth" in page.url.lower():
                    await send("Похоже, вход не прошёл (нас вернуло на страницу входа).")
                    return False

                await context.storage_state(path=config.SESSION_FILE)
                await send("✅ Вход выполнен, сессия сохранена. Мониторинг активен.")
                return True
            finally:
                await context.close()
                await browser.close()

    async def _click_submit(self, page):
        el = await self._first(page, SUBMIT_SELECTORS, timeout=4000)
        if el:
            try:
                await el.click()
                return
            except Exception:  # noqa: BLE001
                pass
        try:
            await page.keyboard.press("Enter")
        except Exception:  # noqa: BLE001
            pass

    async def _ask_otp(self, send) -> str | None:
        loop = asyncio.get_event_loop()
        self._otp_future = loop.create_future()
        await send("🔐 Prom запросил код из SMS. Пришли его сюда сообщением "
                   "(только цифры), в течение 3 минут.")
        try:
            return await asyncio.wait_for(self._otp_future, timeout=180)
        except asyncio.TimeoutError:
            return None


manager = LoginManager()
