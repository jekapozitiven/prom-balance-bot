"""Telegram-бот: мониторинг баланса Prom.ua и уведомления.

Команды:
  /start      — приветствие и chat_id
  /balance    — проверить баланс сейчас
  /status     — текущее состояние и настройки
  /threshold N — задать порог "мало денег" (грн)
  /topup      — кнопка со ссылкой на пополнение
"""
import logging
import os
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import config
import storage
from balance import get_balance
from login_flow import manager as login_manager

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
log = logging.getLogger("bot")


def _level(value: float, threshold: float) -> str:
    if value < 0:
        return "negative"
    if value < threshold:
        return "low"
    return "ok"


def _threshold() -> float:
    return storage.get_float("low_threshold", config.LOW_THRESHOLD)


def _topup_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("💳 Пополнить баланс", url=config.PROM_TOPUP_URL)]]
    )


def _is_owner(update: Update) -> bool:
    return str(update.effective_chat.id) == str(config.TELEGRAM_CHAT_ID)


# ---------- Команды ----------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await update.message.reply_text(
        f"Привет! Я слежу за балансом Prom.ua.\n"
        f"Твой chat_id: <code>{chat_id}</code>\n\n"
        f"Команды:\n"
        f"/login — войти в кабинет (для точного баланса)\n"
        f"/balance — проверить баланс\n"
        f"/status — состояние и настройки\n"
        f"/threshold 200 — порог предупреждения\n"
        f"/topup — ссылка на пополнение",
        parse_mode=ParseMode.HTML,
    )


async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update):
        return
    msg = await update.message.reply_text("Проверяю баланс…")
    try:
        res = await get_balance()
    except Exception as e:  # noqa: BLE001
        await msg.edit_text(f"⚠️ Не удалось получить баланс:\n{e}")
        return
    kind = "точно" if res.exact else "оценка"
    th = _threshold()
    emoji = "🟢" if res.value >= th else ("🔴" if res.value < 0 else "🟡")
    await msg.edit_text(
        f"{emoji} Баланс: <b>{res.value:.2f} грн</b>\n"
        f"Источник: {res.source} ({kind}) · порог {th:.0f} грн",
        parse_mode=ParseMode.HTML,
        reply_markup=_topup_markup(),
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update):
        return
    last = storage.get("last_balance", "—")
    last_ts = storage.get("last_check_ts")
    when = (
        time.strftime("%d.%m %H:%M", time.localtime(float(last_ts)))
        if last_ts
        else "—"
    )
    await update.message.reply_text(
        f"Последний баланс: {last} грн (в {when})\n"
        f"Порог: {_threshold():.0f} грн\n"
        f"Проверка каждые {config.CHECK_INTERVAL_MIN} мин\n"
        f"Источник: {config.BALANCE_SOURCE}\n"
        f"Повтор алерта: {config.REPEAT_ALERT_HOURS} ч"
    )


async def cmd_threshold(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update):
        return
    if not context.args:
        await update.message.reply_text(
            f"Текущий порог: {_threshold():.0f} грн\nЗадать: /threshold 300"
        )
        return
    try:
        val = float(context.args[0].replace(",", "."))
    except ValueError:
        await update.message.reply_text("Нужно число, например: /threshold 300")
        return
    storage.set("low_threshold", val)
    await update.message.reply_text(f"Порог обновлён: {val:.0f} грн")


async def cmd_topup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update):
        return
    await update.message.reply_text(
        "Пополнить баланс:", reply_markup=_topup_markup()
    )


async def cmd_login(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_owner(update):
        return
    chat_id = update.effective_chat.id

    async def send(text: str):
        await context.bot.send_message(chat_id, text)

    # запускаем вход в фоне, чтобы бот продолжал принимать сообщение с кодом
    context.application.create_task(login_manager.run(send))


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ловим SMS-код, когда идёт процесс входа."""
    if not _is_owner(update):
        return
    text = (update.message.text or "").strip()
    if login_manager.awaiting_otp and text.replace(" ", "").isdigit():
        if login_manager.submit_otp(text):
            await update.message.reply_text("Код принят, продолжаю вход…")


# ---------- Периодическая проверка ----------

async def check_job(context: ContextTypes.DEFAULT_TYPE):
    try:
        res = await get_balance()
    except Exception as e:  # noqa: BLE001
        log.warning("Проверка не удалась: %s", e)
        # уведомим об ошибке источника не чаще раза в REPEAT_ALERT_HOURS
        last_err = storage.get_float("last_error_ts", 0)
        if time.time() - last_err > max(config.REPEAT_ALERT_HOURS, 1) * 3600:
            storage.set("last_error_ts", time.time())
            await context.bot.send_message(
                config.TELEGRAM_CHAT_ID,
                f"⚠️ Не могу проверить баланс:\n{e}",
            )
        return

    th = _threshold()
    level = _level(res.value, th)
    prev = storage.get("alert_level", "ok")
    last_alert_ts = storage.get_float("last_alert_ts", 0)
    now = time.time()

    storage.set("last_balance", f"{res.value:.2f}")
    storage.set("last_check_ts", now)

    rank = {"ok": 0, "low": 1, "negative": 2}
    kind = "точно" if res.exact else "оценка"
    src_note = f"\nИсточник: {res.source} ({kind})"

    should_alert = False
    text = ""

    if rank[level] > rank[prev]:  # стало хуже
        should_alert = True
        if level == "negative":
            text = (
                f"🔴 <b>Баланс ушёл в минус: {res.value:.2f} грн</b>\n"
                f"Магазин могут заблокировать — пополни срочно.{src_note}"
            )
        else:
            text = (
                f"🟡 <b>Мало денег: {res.value:.2f} грн</b>\n"
                f"Ниже порога {th:.0f} грн.{src_note}"
            )
    elif level != "ok" and (now - last_alert_ts) > config.REPEAT_ALERT_HOURS * 3600 > 0:
        should_alert = True
        emoji = "🔴" if level == "negative" else "🟡"
        text = (
            f"{emoji} Напоминание: баланс всё ещё {res.value:.2f} грн."
            f"{src_note}"
        )
    elif level == "ok" and prev != "ok":  # восстановился
        should_alert = True
        text = f"🟢 Баланс восстановлен: {res.value:.2f} грн.{src_note}"

    storage.set("alert_level", level)
    if should_alert:
        storage.set("last_alert_ts", now)
        await context.bot.send_message(
            config.TELEGRAM_CHAT_ID,
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=_topup_markup() if level != "ok" else None,
        )


async def on_startup(app: Application):
    has_session = os.path.exists(config.SESSION_FILE)
    hint = (
        "\nСессия кабинета найдена — читаю точный баланс."
        if has_session
        else "\n⚠️ Входа в кабинет ещё нет. Отправь /login для точного баланса "
        "(пока работает оценка по API, если задан токен)."
    )
    await app.bot.send_message(
        config.TELEGRAM_CHAT_ID,
        "🤖 Бот запущен, слежу за балансом Prom.ua." + hint,
    )


def main():
    problems = config.validate()
    if problems:
        raise SystemExit("Ошибки конфигурации:\n- " + "\n- ".join(problems))

    storage.init()
    app = Application.builder().token(config.TELEGRAM_TOKEN).post_init(on_startup).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("login", cmd_login))
    app.add_handler(CommandHandler("balance", cmd_balance))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("threshold", cmd_threshold))
    app.add_handler(CommandHandler("topup", cmd_topup))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    app.job_queue.run_repeating(
        check_job,
        interval=config.CHECK_INTERVAL_MIN * 60,
        first=15,
        name="balance_check",
    )

    log.info("Бот запущен. Проверка каждые %s мин.", config.CHECK_INTERVAL_MIN)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
