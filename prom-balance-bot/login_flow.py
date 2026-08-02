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
LOGIN_SELECTORS = [
    "input[name='email']",
    "input[type='email']",
    "input[autocomplete='username']",
    "input[name='login']",
    "input[name*='phone']",
    "input[type='tel']",
    "input[inputmode='email']",
    "input[placeholder*='mail' i]",
    "input[placeholder*='телефон' i]",
    "input[placeholder*='ошта' i]",
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
        """Первый видимый элемент из кандидатов, ищем и во фреймах."""
        deadline_each = max(timeout // max(len(selectors), 1), 1200)
        for scope in self._frames(page):
            for sel in selectors:
                try:
                    el = scope.locator(sel).first
                    await el.wait_for(state="visible", timeout=deadline_each)
                    return el
                except Exception:  # noqa: BLE001
                    continue
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
        """Прислать список полей/кнопок и адрес — для настройки селекторов."""
        try:
            data = await page.evaluate(
                """() => {
                    const grab = (root) => Array.from(root.querySelectorAll('input,button'))
                      .map(e => ({
                        t: e.tagName,
                        type: e.type || '',
                        name: e.name || '',
                        id: e.id || '',
                        ph: e.placeholder || '',
                        ac: e.getAttribute('autocomplete') || '',
                        txt: (e.innerText || e.value || '').slice(0, 25)
                      }));
                    let out = grab(document);
                    for (const f of document.querySelectorAll('iframe')) {
                      try { out = out.concat(grab(f.contentDocument)); } catch(e) {}
                    }
                    return out;
                }"""
            )
        except Exception as e:  # noqa: BLE001
            data = f"(не удалось прочитать: {e})"
        lines = [f"URL: {page.url}", "Поля на странице:"]
        if isinstance(data, list):
            for d in data[:40]:
                lines.append(
                    f"• {d['t']} type={d['type']} name={d['name']} id={d['id']} "
                    f"ph='{d['ph']}' ac={d['ac']} txt='{d['txt']}'"
                )
            if not data:
                lines.append("(полей не найдено — возможно, форма ещё грузится)")
        else:
            lines.append(str(data))
        msg = "\n".join(lines)
        await send(msg[:3900])

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

                # шаг 2: нажать "Вхід/Войти", если появилось
                for opener in OPENERS:
                    try:
                        await page.locator(opener).first.click(timeout=2000)
                        await page.wait_for_timeout(2000)
                        break
                    except Exception:  # noqa: BLE001
                        continue

                login_el = await self._first(page, LOGIN_SELECTORS, timeout=12000)
                if not login_el:
                    await send("Не нашёл поле логина на странице входа. "
                               "Присылаю, что реально на странице — по этому "
                               "подстроим селекторы:")
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
