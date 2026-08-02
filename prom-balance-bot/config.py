"""Загрузка конфигурации из переменных окружения (.env)."""
import os
from dotenv import load_dotenv

load_dotenv()

# Каталог для сохранения состояния (сессия кабинета + БД).
# На Railway подключается том и монтируется, напр., в /data.
# Если тома нет — используем текущую папку.
DATA_DIR = os.getenv("DATA_DIR", "/data" if os.path.isdir("/data") else ".").strip()
os.makedirs(DATA_DIR, exist_ok=True)


def _get(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _int(name: str, default: int) -> int:
    try:
        return int(float(_get(name, str(default))))
    except (ValueError, TypeError):
        return default


def _float(name: str, default: float) -> float:
    raw = _get(name, str(default)).replace(",", ".")
    try:
        return float(raw)
    except (ValueError, TypeError):
        return default


# Telegram
TELEGRAM_TOKEN = _get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = _get("TELEGRAM_CHAT_ID")

# Пороги/расписание
LOW_THRESHOLD = _float("LOW_THRESHOLD", 200)
CHECK_INTERVAL_MIN = _int("CHECK_INTERVAL_MIN", 20)
REPEAT_ALERT_HOURS = _float("REPEAT_ALERT_HOURS", 6)

# Источник баланса
BALANCE_SOURCE = _get("BALANCE_SOURCE", "both").lower()  # cabinet | api | both

# Кабинет (Playwright) — рабочие значения зашиты по умолчанию
PROM_BALANCE_URL = _get("PROM_BALANCE_URL", "https://my.prom.ua/cms/invoice")
PROM_BALANCE_REGEX = _get(
    "PROM_BALANCE_REGEX", r"Баланс[:\s]*([-−]?\d[\d\s ]*[.,]?\d*)\s*₴"
)
PROM_LOGIN = _get("PROM_LOGIN")
PROM_PASSWORD = _get("PROM_PASSWORD")
PROM_LOGIN_URL = _get("PROM_LOGIN_URL", "https://my.prom.ua/")
SESSION_FILE = _get("SESSION_FILE", os.path.join(DATA_DIR, "state.json"))

# Публичный API (оценка)
PROM_API_TOKEN = _get("PROM_API_TOKEN")
PROM_API_BASE = _get("PROM_API_BASE", "https://my.prom.ua/api/v1")
API_ESTIMATE_START_BALANCE = _float("API_ESTIMATE_START_BALANCE", 0)
API_COMMISSION_PERCENT = _float("API_COMMISSION_PERCENT", 0)
API_DELIVERY_FEE = _float("API_DELIVERY_FEE", 30)

# Пополнение (на этой же странице кабинета "Поповнення балансу")
PROM_TOPUP_URL = _get("PROM_TOPUP_URL", "https://my.prom.ua/cms/invoice")

# Файл состояния
DB_FILE = _get("DB_FILE", os.path.join(DATA_DIR, "state.db"))


def validate() -> list[str]:
    """Вернёт список проблем конфигурации (пустой = всё ок)."""
    problems = []
    if not TELEGRAM_TOKEN:
        problems.append("TELEGRAM_TOKEN не задан")
    if not TELEGRAM_CHAT_ID:
        problems.append("TELEGRAM_CHAT_ID не задан")
    if BALANCE_SOURCE not in ("cabinet", "api", "both"):
        problems.append("BALANCE_SOURCE должен быть cabinet | api | both")
    if BALANCE_SOURCE == "api" and not PROM_API_TOKEN:
        problems.append("Для режима api нужен PROM_API_TOKEN")
    return problems
