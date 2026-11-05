"""시세와 시총. FinanceDataReader 상장 목록이 기본이고, 종목 지정 시 네이버로 보완한다."""

from __future__ import annotations

import json
import re
from urllib.request import Request, urlopen

from screeners.money import EOK

_VALUE_RE = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*(조|억)")


def parse_market_text(text: str | None) -> int | None:
    if not text:
        return None
    match = _VALUE_RE.search(str(text).replace(",", ""))
    if not match:
        return None
    number = float(match.group(1))
    if match.group(2) == "조":
        return int(round(number * 10_000 * EOK))
    return int(round(number * EOK))


def quotes_from_listing(frame) -> list[dict]:
    rows = []
    for _, item in frame.iterrows():
        code = str(item.get("Code") or "").strip()
        if code.endswith(".0"):
            code = code[:-2]
        if code.isdigit():
            code = code.zfill(6)
        marcap = item.get("Marcap")
        try:
            marcap_krw = int(float(marcap))
        except (TypeError, ValueError):
            marcap_krw = None
        close = item.get("Close")
        try:
            close_krw = int(float(close))
        except (TypeError, ValueError):
            close_krw = None
        rows.append(
            {
                "code": code,
                "name": str(item.get("Name") or ""),
                "market": str(item.get("Market") or ""),
                "market_cap_krw": marcap_krw,
                "close": close_krw,
            }
        )
    return rows


def load_listing_quotes():
    try:
        import FinanceDataReader as fdr
    except ImportError as exc:
        raise RuntimeError("FinanceDataReader 가 없습니다. trend_monitor/requirements.txt 를 설치하세요.") from exc
    frame = fdr.StockListing("KRX")
    if frame is None or len(frame) == 0:
        raise RuntimeError("KRX 상장 목록이 비어 있습니다.")
    return quotes_from_listing(frame)


def naver_quote(code: str, opener=None) -> dict:
    """네이버에서 이름, 시장, 시총을 읽는다. 실패하면 예외."""
    open_fn = opener or urlopen
    payload = _naver_json(f"https://m.stock.naver.com/api/stock/{code}/integration", open_fn)
    basic = _naver_json(f"https://m.stock.naver.com/api/stock/{code}/basic", open_fn)
    infos = {row.get("code"): row.get("value") for row in payload.get("totalInfos") or [] if isinstance(row, dict)}
    marcap = parse_market_text(infos.get("marketValue"))
    if marcap is None:
        raise RuntimeError(f"{code} 네이버 시총을 읽지 못했습니다.")
    exchange = basic.get("stockExchangeType") or {}
    market = str(exchange.get("nameEng") or basic.get("stockExchangeName") or "")
    return {
        "code": code,
        "name": str(payload.get("stockName") or basic.get("stockName") or code),
        "market": _market_name(market),
        "market_cap_krw": marcap,
        "close": _close(basic.get("closePrice") or infos.get("lastClosePrice")),
    }


def _naver_json(url: str, open_fn) -> dict:
    req = Request(url, headers={"User-Agent": "cwally-alerts-screeners"})
    with open_fn(req, timeout=20) as response:
        raw = response.read()
    text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
    return json.loads(text)


def _market_name(text: str) -> str:
    upper = text.upper()
    if "GLOBAL" in upper and "KOSDAQ" in upper:
        return "KOSDAQ GLOBAL"
    if "KOSDAQ" in upper:
        return "KOSDAQ"
    if "KOSPI" in upper:
        return "KOSPI"
    return ""


def _close(text) -> int | None:
    if text is None:
        return None
    digits = str(text).replace(",", "").strip()
    if not digits.isdigit():
        return None
    return int(digits)
