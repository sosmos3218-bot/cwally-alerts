"""수량. 위험 예산, 비중 상한, 총 위험, 현금을 모두 적용하고 나머지는 버립니다."""

from __future__ import annotations

from decimal import Decimal, ROUND_DOWN

from trend_monitor.ticks import round_to_tick


def floor_div(numer, denom) -> int:
    if denom is None:
        return 0
    d = Decimal(str(denom))
    if d <= 0:
        return 0
    q = (Decimal(str(numer)) / d).to_integral_value(rounding=ROUND_DOWN)
    return max(0, int(q))


def size_order(
    *,
    entry: float,
    stop: float,
    account: float,
    risk_pct: float,
    max_weight_pct: float,
    max_stop_distance_pct: float,
    cash: float,
    risk_room: float,
) -> tuple[int, str]:
    """수량을 돌려줍니다. 0이면 이유 문자열을 함께 줍니다."""
    entry_px = round_to_tick(entry)
    stop_px = int(stop)
    if entry_px <= stop_px:
        return 0, "손절이 진입가 이상이라 수량을 계산하지 않습니다."
    distance = (entry_px - stop_px) / entry_px * 100.0
    if distance > max_stop_distance_pct:
        return 0, f"손절 거리 {distance:.2f}% 가 상한 {max_stop_distance_pct:g}% 를 넘습니다."
    per = entry_px - stop_px
    risk_budget = Decimal(str(account)) * Decimal(str(risk_pct)) / Decimal(100)
    qty_risk = floor_div(risk_budget, per)
    qty_total = floor_div(risk_room, per)
    weight_budget = Decimal(str(account)) * Decimal(str(max_weight_pct)) / Decimal(100)
    qty_weight = floor_div(weight_budget, entry_px)
    qty_cash = floor_div(cash, entry_px)
    qty = min(qty_risk, qty_total, qty_weight, qty_cash)
    if qty < 1:
        return 0, (
            f"수량 0 (위험 {qty_risk}, 총위험 여유 {qty_total}, 비중 {qty_weight}, 현금 {qty_cash})."
        )
    caps = []
    if qty == qty_weight and qty_weight <= qty_risk:
        caps.append("비중 상한")
    if qty == qty_total and qty_total <= qty_risk:
        caps.append("총 위험 상한")
    if qty == qty_cash and qty_cash <= qty_risk:
        caps.append("현금")
    note = f"수량 {qty}"
    if caps:
        note += " (" + ", ".join(caps) + ")"
    return qty, note
