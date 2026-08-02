"""Единая точка получения баланса с учётом выбранного источника."""
import logging

import config
from prom_client import PromAPI
from prom_scraper import get_balance_from_cabinet

log = logging.getLogger("balance")


class BalanceResult:
    def __init__(self, value: float, source: str, exact: bool):
        self.value = value
        self.source = source   # "cabinet" | "api"
        self.exact = exact     # True для кабинета, False для оценки

    def __repr__(self):
        kind = "точно" if self.exact else "оценка"
        return f"{self.value:.2f} грн ({self.source}, {kind})"


async def get_balance() -> BalanceResult:
    """Вернуть баланс согласно BALANCE_SOURCE.

    both: сначала кабинет (точно), при ошибке — оценка по API.
    """
    src = config.BALANCE_SOURCE
    errors = []

    if src in ("cabinet", "both"):
        try:
            value = await get_balance_from_cabinet()
            return BalanceResult(value, "cabinet", exact=True)
        except Exception as e:  # noqa: BLE001
            errors.append(f"кабинет: {e}")
            log.warning("Скрапер кабинета не сработал: %s", e)
            if src == "cabinet":
                raise

    if src in ("api", "both") and config.PROM_API_TOKEN:
        try:
            value = await PromAPI().estimate_balance()
            return BalanceResult(value, "api", exact=False)
        except Exception as e:  # noqa: BLE001
            errors.append(f"api: {e}")
            log.warning("Оценка по API не сработала: %s", e)

    raise RuntimeError("Не удалось получить баланс. " + "; ".join(errors))
