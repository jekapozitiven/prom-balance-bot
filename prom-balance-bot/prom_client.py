"""Публичный API Prom.ua — оценка баланса по новым заказам.

ВАЖНО: у публичного API нет прямого метода "баланс кошелька".
Здесь мы оцениваем баланс: стартовое значение минус приблизительное
списание по каждому новому заказу (комиссия % + фикс за доставку).
Это резервный/оценочный источник. Точное значение даёт скрапер кабинета.
"""
import httpx

import config
import storage


class PromAPI:
    def __init__(self, token: str | None = None, base: str | None = None):
        self.token = token or config.PROM_API_TOKEN
        self.base = (base or config.PROM_API_BASE).rstrip("/")

    def _headers(self):
        return {"Authorization": f"Bearer {self.token}"}

    async def fetch_orders(self, limit: int = 100) -> list[dict]:
        """Получить последние заказы."""
        url = f"{self.base}/orders/list"
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(
                url, headers=self._headers(), params={"limit": limit}
            )
            r.raise_for_status()
            data = r.json()
        return data.get("orders", []) if isinstance(data, dict) else []

    async def estimate_balance(self) -> float:
        """Оценка баланса: старт − списания по новым (ещё не учтённым) заказам."""
        current = storage.get_float(
            "api_estimate_balance", config.API_ESTIMATE_START_BALANCE
        )
        # первый запуск: зафиксировать стартовое значение
        if storage.get("api_estimate_initialized") is None:
            current = config.API_ESTIMATE_START_BALANCE
            storage.set("api_estimate_initialized", "1")

        orders = await self.fetch_orders(limit=100)
        for o in orders:
            oid = str(o.get("id") or o.get("order_id") or "")
            if not oid or storage.is_order_seen(oid):
                continue
            amount = _order_amount(o)
            deduction = (
                amount * config.API_COMMISSION_PERCENT / 100.0
                + config.API_DELIVERY_FEE
            )
            current -= deduction
            storage.mark_order_seen(oid)

        storage.set("api_estimate_balance", current)
        return round(current, 2)


def _order_amount(order: dict) -> float:
    """Сумма заказа из разных возможных полей API."""
    for key in ("price", "amount_total", "total_price", "full_price"):
        v = order.get(key)
        if v is not None:
            try:
                return float(str(v).replace(",", "."))
            except (ValueError, TypeError):
                pass
    return 0.0
