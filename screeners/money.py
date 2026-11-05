"""금액 읽기와 억 단위 표시. 키나 비밀은 다루지 않는다."""

from __future__ import annotations

import re
from decimal import Decimal, ROUND_HALF_UP

EOK = 100_000_000


def parse_krw(raw) -> int | None:
    """쉼표, 괄호 음수, 삼각형 기호를 원 단위 정수로 읽는다. 빈 값은 None."""
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, Decimal)):
        return int(raw)
    if isinstance(raw, float):
        if raw != raw:  # NaN
            return None
        return int(Decimal(str(raw)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    text = str(raw).strip()
    if text == "" or text in {"-", "nan", "NaN", "N/A", "null"}:
        return None
    neg = False
    if text.startswith("(") and text.endswith(")"):
        neg = True
        text = text[1:-1]
    text = text.replace(",", "").replace(" ", "")
    if text[:1] in {"-", "△", "▲"}:
        neg = True
        text = text[1:]
    if text == "":
        return None
    try:
        value = Decimal(text)
    except Exception:
        return None
    won = int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    return -won if neg else won


def parse_ratio(raw) -> float | None:
    """지분율, PER 처럼 퍼센트 기호가 있을 수 있는 수를 읽는다."""
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        if isinstance(raw, float) and raw != raw:
            return None
        return float(raw)
    text = str(raw).strip().replace(",", "").replace("%", "").replace("배", "")
    if text in {"", "-", "N/A", "nan"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def fmt_eok(krw: int | None) -> str:
    """화면용. 1조 이상이면 조, 그 외에는 억. 소수 한 자리까지 반올림하되 .0 은 뺀다."""
    if krw is None:
        return "없음"
    eok = krw / EOK
    sign = "-" if eok < 0 else ""
    eok = abs(eok)
    if eok >= 10_000:
        text = f"{eok / 10_000:.2f}".rstrip("0").rstrip(".")
        return f"{sign}{text}조"
    if eok >= 100:
        return f"{sign}{eok:,.0f}억"
    text = f"{eok:,.1f}".rstrip("0").rstrip(".")
    return f"{sign}{text}억"


def redact(text: str, key: str | None) -> str:
    if not text:
        return ""
    out = text
    if key:
        out = out.replace(key, "***")
    out = re.sub(r"crtfc_key=[^&\s]+", "crtfc_key=***", out)
    return out
