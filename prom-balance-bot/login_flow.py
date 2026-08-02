"""Одноразовый вход в кабинет Prom.ua прямо из облака через Telegram.

Идея: бот сам открывает страницу входа в облаке (headless-браузер),
вводит логин/пароль из переменных окружения, а когда Prom запрашивает
SMS/2FA-код — просит тебя прислать код в чат. Ты пишешь код сообщением,
бот его подставляет и сохраняет сессию (state.json) на постоянный том.
После этого мониторинг работает сам, повторный вход нужен редко.

Если поле входа не найдено, бот присылает список всех полей страницы —
по нему легко дописать точные селекторы.
"""
import asyncio
import logging

from playwright.async_api import async_playwright

import config

log = logging.getLogger("login")

# флаги, без которых Chromium не стартует/падает в контейнере (Railway)
LAUNCH_ARGS = [
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
]

# кандидаты селекторов (первый подходящий — используется)
# форма Prom — в iframe connect-rid.prom.ua, вход по телефону (autocomplete=tel)
LOGIN_SELECTORS = [
    "input[autocomplete='tel']",
    "input[inputmode='tel']",
    "input[type='tel']",
    "input[name*='phone']",
    "input[name='email']",
    "input[type='email']",
    "input[autocomplete='username']",
    "input[name='login']",
    "input[inputmode='email']",
    "input[placeholder*='телефон' i]",
    "input[placeholder*='mail' i]",
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
    "input[inputmode='numeric']",
]
SUBMIT_SELECTORS = [
    "button[type='submit']",
    "button:has-text('Увійти')",
    "button:has-text('Войти')",
    "button:has-text('Вхід')",
    "button:has-text('Продовжити')",
    "button:has-text('Продолжить')",
    "button:has-text('Далі')",
    "button:has-text('Далее')",
]
# текст кнопки/ссылки, открывающей форму входа
OPENERS = [
    "text=Вхід", "text=Войти", "text=Увійти",
    "a:has-text('Вхід')", "a:has-text('Войти')",
    "button:has-text('Вхід')", "button:has-text('Войти')",
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
        if self.awaiting_otp:
            self._otp_future.set_result(code.strip())
            return True
        return False

    def _frames(self, page):
        """Основная страница + все iframe (форма входа бывает в iframe)."""
        return [page] + [f for f in page.frames if f != page.main_frame]

    async def _first(self, page, selectors, timeout=8000):
        """Первый видимый элемент из кандидатов; сканируем все фреймы по кругу."""
        rounds = max(int(timeout / 1000), 3)
        for _ in range(rounds):
            for scope in self._frames(page):
                for sel in selectors:
                    try:
                        loc = scope.locator(sel).first
                        if await loc.count() and await loc.is_visible():
                            return loc
                    except Exception:  # noqa: BLE001
                        continue
            await page.wait_for_timeout(1000)
        return None

    async def run(self, send) -> bool:
        if self._running:
            await send("Вход уже выполняется, подожди…")
            return False
        if not config.PROM_LOGIN or not config.PROM_PASSWORD:
            await send("Не заданы PROM_LOGIN / PROM_PASSWORD в переменных окружения.")
            return False

        self._running = True
        try:
            return await self._run(send)
        except Exception as e:  # noqa: BLE001
            log.exception("Ошибка входа")
            await send(f"⚠️ Не удалось войти автоматически: {e}")
            return False
        finally:
            self._running = False
            self._otp_future = None

    async def _dump_fields(self, page, send):
        """Прислать поля ВСЕХ фреймов (форма входа бывает в cross-origin iframe)."""
        lines = [f"URL: {page.url}"]
        frames = page.frames
        lines.append(f"Фреймов: {len(frames)}")
        for i, fr in enumerate(frames):
            url = ""
            try:
                url = fr.url
            except Exception:  # noqa: BLE001
                url = "?"
            # только фреймы, где есть поля ввода — чтобы не засорять
            try:
                inputs = await fr.locator("input").all()
            except Exception as e:  # noqa: BLE001
                lines.append(f"[frame {i}] {url} — ошибка: {e}")
                continue
            if not inputs:
                continue
            lines.append(f"[frame {i}] {url} — input'ов: {len(inputs)}")
            for el in inputs[:15]:
                try:
                    a = await el.evaluate(
                        "e => ({ty:e.type||'', n:e.name||'', id:e.id||'', "
                        "ph:e.placeholder||'', ac:e.getAttribute('autocomplete')||'', "
                        "im:e.getAttribute('inputmode')||''})"
                    )
                    lines.append(
                        f"  • input ty={a['ty']} name={a['n']} id={a['id']} "
                        f"ph='{a['ph']}' ac={a['ac']} im={a['im']}"
                    )
                except Exception:  # noqa: BLE001
                    continue
        if len(lines) <= 2:
            lines.append("(полей ввода не найдено ни в одном фрейме)")
        await send("\n".join(lines)[:3900])

    async def _run(self, send) -> bool:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=LAUNCH_ARGS)
            context = await browser.new_context(
                user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/124.0 Safari/537.36"),
                locale="uk-UA",
            )
            page = await context.new_page()
            try:
                await send("Открываю страницу входа Prom…")
                await page.goto(config.PROM_LOGIN_URL, wait_until="domcontentloaded",
                                timeout=45000)
                await page.wait_for_timeout(2500)

                # шаг 1: открыть меню кабинета ("Кабінет")
                for opener in ["button:has-text('Кабінет')",
                               "button:has-text('Кабинет')", "text=Кабінет"]:
                    try:
                        await page.locator(opener).first.click(timeout=2500)
                        await page.wait_for_timeout(2500)
                        break
                    except Exception:  # noqa: BLE001
                        continue

                # шаг 2: нажать "Вхід/Войти", если появилось (в сайдбаре/меню)
                for opener in OPENERS:
                    try:
                        await page.locator(opener).first.click(timeout=2000)
                        await page.wait_for_timeout(3500)  # ждём загрузку формы/iframe
                        break
                    except Exception:  # noqa: BLE001
                        continue

                login_el = await self._first(page, LOGIN_SELECTORS, timeout=12000)
                if not login_el:
                    await send("Не нашёл поле логина. Присылаю поля всех фреймов — "
                               "по ним подстрою точные селекторы:")
                    await self._dump_fields(page, send)
                    return False
                await login_el.fill(config.PROM_LOGIN)

                pass_el = await self._first(page, PASSWORD_SELECTORS, timeout=4000)
                if pass_el:
                    await pass_el.fill(config.PROM_PASSWORD)
                await self._click_submit(page)

                if not pass_el:  # пароль на втором шаге
                    pass_el = await self._first(page, PASSWORD_SELECTORS, timeout=10000)
                    if pass_el:
                        await pass_el.fill(config.PROM_PASSWORD)
                        await self._click_submit(page)

                otp_el = await self._first(page, OTP_SELECTORS, timeout=8000)
                if otp_el:
                    code = await self._ask_otp(send)
                    if code is None:
                        await send("Код не получен вовремя, вход отменён.")
                        return False
                    await otp_el.fill(code)
                    await self._click_submit(page)
                    await page.wait_for_load_state("networkidle", timeout=30000)

                await page.goto(config.PROM_BALANCE_URL, wait_until="networkidle",
                                timeout=45000)
                if "login" in page.url.lower() or "auth" in page.url.lower():
                    await send("Похоже, вход не прошёл (нас вернуло на страницу входа).")
                    await self._dump_fields(page, send)
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
                await page.wait_for_timeout(1500)
                return
            except Exception:  # noqa: BLE001
                pass
        try:
            await page.keyboard.press("Enter")
            await page.wait_for_timeout(1500)
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
