from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN


def load_money_fund_income(conn, symbol: str, sessions: list[str]) -> dict[str, list[tuple[str, float]]]:
    """Associate holiday distributions with the first following CN session."""
    result: dict[str, list[tuple[str, float]]] = {}
    for day, income in conn.execute(
        """SELECT income_date, income_per_100 FROM money_fund_income
           WHERE symbol=? AND income_date BETWEEN ? AND ? ORDER BY income_date""",
        (symbol, sessions[0], sessions[-1]),
    ):
        session = sessions[bisect_left(sessions, day)]
        result.setdefault(session, []).append((day, income))
    return result


@dataclass
class MoneyFundIncome:
    accrued_cents: int = 0
    payable_cents: int = 0

    @property
    def receivable_cny(self) -> float:
        return (self.accrued_cents + self.payable_cents) / 100

    def settle(self) -> float:
        paid = self.payable_cents
        self.payable_cents = 0
        return paid / 100

    def record(self, quantity: float, income_per_100: float) -> float:
        # One fund share trades around CNY 100. The feed quotes income
        # per hundred shares, so the cents calculation cancels that divisor.
        cents = int((Decimal(str(quantity)) * Decimal(str(income_per_100))).to_integral_value(rounding=ROUND_DOWN))
        self.accrued_cents += cents
        return cents / 100

    def reinvest(self) -> int:
        shares = max(self.accrued_cents, 0) // 10_000
        self.accrued_cents -= shares * 10_000
        return shares

    def schedule_payment(self) -> None:
        self.payable_cents += self.accrued_cents
        self.accrued_cents = 0
