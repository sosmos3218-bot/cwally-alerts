"""네이버 모바일 수급. 실패하면 숫자를 만들지 않고 확인 불가로 둡니다.

https://m.stock.naver.com/api/stock/{ticker}/trend
외국인 foreignerPureBuyQuant, 기관 organPureBuyQuant.
"""

from __future__ import annotations

import time
from datetime import date, datetime

FLOW_URL = "https://m.stock.naver.com/api/stock/{ticker}/trend?pageSize=20"


def parse_signed_int(raw) -> int | None:
    if raw is None:
        return None
    text = str(raw).strip().replace(",", "").replace("+", "").replace(" ", "")
    if text in {"", "-", "null"}:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def summarize_trend(rows, target: date) -> dict:
    """기준일 이전(포함) 최근 5일, 10일 순매수 합. 재료가 없으면 값은 None."""
    empty = {
        "foreign_net_5d": None,
        "foreign_net_10d": None,
        "institution_net_5d": None,
        "institution_net_10d": None,
        "flow_note": "수급 확인 불가",
        "ok": False,
    }
    if not isinstance(rows, list):
        return empty
    parsed = []
    for row in rows:
        if not isinstance(row, dict) or not row.get("bizdate"):
            continue
        try:
            day = datetime.strptime(str(row["bizdate"]), "%Y%m%d").date()
        except ValueError:
            continue
        if day > target:
            continue
        parsed.append(
            (
                day,
                parse_signed_int(row.get("foreignerPureBuyQuant")),
                parse_signed_int(row.get("organPureBuyQuant")),
            )
        )
    parsed.sort(key=lambda item: item[0], reverse=True)
    if not parsed:
        return empty

    def total(n: int, idx: int) -> int | None:
        chunk = parsed[:n]
        values = [item[idx] for item in chunk]
        if not values or any(value is None for value in values):
            return None
        return int(sum(values))

    latest = parsed[0][0].isoformat()
    used = min(10, len(parsed))
    return {
        "foreign_net_5d": total(5, 1),
        "foreign_net_10d": total(10, 1),
        "institution_net_5d": total(5, 2),
        "institution_net_10d": total(10, 2),
        "flow_note": f"수급 최근 {used}거래일, 마지막 기준 {latest}",
        "ok": True,
    }


def _http_get(url: str) -> bytes:
    import requests

    response = requests.get(
        url,
        headers={"User-Agent": "Mozilla/5.0", "Referer": "https://m.stock.naver.com/"},
        timeout=15,
    )
    if response.status_code != 200:
        raise RuntimeError(f"HTTP {response.status_code}")
    return response.content


class FlowClient:
    def __init__(self, transport=None, pause: float = 0.12, sleep=time.sleep):
        self.transport = transport or _http_get
        self.pause = pause
        self.sleep = sleep
        self._cache: dict[str, dict] = {}
        self._calls = 0

    def lookup(self, ticker: str, target: date) -> dict:
        key = str(ticker).zfill(6) if str(ticker).isdigit() else str(ticker)
        if key in self._cache:
            return self._cache[key]
        if self._calls:
            self.sleep(self.pause)
        self._calls += 1
        try:
            import json

            raw = self.transport(FLOW_URL.format(ticker=key))
            rows = json.loads(raw.decode("utf-8"))
            result = summarize_trend(rows, target)
        except Exception:
            result = summarize_trend(None, target)
        self._cache[key] = result
        return result
