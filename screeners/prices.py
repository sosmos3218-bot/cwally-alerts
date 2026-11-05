"""시세와 시총. FinanceDataReader 상장 목록이 기본이고, 종목 지정 시 네이버로 보완한다."""

from __future__ import annotations

import json
import re
from urllib.request import Request, urlopen

from screeners.money import EOK, parse_ratio

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


def _ratios_fresh(saved: str, today: str, days: int = 7) -> bool:
    """같은 주 안쪽의 PER, PBR 캐시를 다시 쓴다. 날짜가 깨지면 버린다."""
    from datetime import date, timedelta

    try:
        left = date.fromisoformat(saved)
        right = date.fromisoformat(today)
    except ValueError:
        return False
    return abs(right - left) <= timedelta(days=days)


def ratios_from_infos(infos: dict) -> tuple[float | None, float | None]:
    """네이버 totalInfos 의 per, pbr. 없거나 N/A 면 None."""
    return parse_ratio(infos.get("per")), parse_ratio(infos.get("pbr"))


def fetch_naver_ratios(code: str, opener=None) -> tuple[float | None, float | None]:
    open_fn = opener or urlopen
    payload = _naver_json(f"https://m.stock.naver.com/api/stock/{code}/integration", open_fn)
    infos = {row.get("code"): row.get("value") for row in payload.get("totalInfos") or [] if isinstance(row, dict)}
    return ratios_from_infos(infos)


def lookup_ratios(codes: list[str], fetch, cache_path=None, today: str | None = None, workers: int = 8) -> dict[str, dict]:
    """일주일 안의 PER, PBR 캐시를 먼저 쓴다. 한 종목 실패는 없음으로 둔다."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from pathlib import Path

    cached: dict = {}
    path = Path(cache_path) if cache_path else None
    if path and path.is_file() and today:
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            saved = {}
        if _ratios_fresh(str(saved.get("date") or ""), today):
            cached = dict(saved.get("items") or {})
    out: dict[str, dict] = {}
    missing = []
    for code in codes:
        hit = cached.get(code)
        if isinstance(hit, dict) and ("per" in hit or "pbr" in hit):
            out[code] = {"per": hit.get("per"), "pbr": hit.get("pbr")}
        else:
            missing.append(code)

    def one(code: str) -> tuple[str, float | None, float | None]:
        try:
            per, pbr = fetch(code)
        except Exception:
            return code, None, None
        return code, per, pbr

    if missing and workers > 1 and len(missing) >= 4:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(one, code) for code in missing]
            for future in as_completed(futures):
                code, per, pbr = future.result()
                out[code] = {"per": per, "pbr": pbr}
    else:
        for code in missing:
            code, per, pbr = one(code)
            out[code] = {"per": per, "pbr": pbr}
    if path and today:
        path.parent.mkdir(parents=True, exist_ok=True)
        merged = dict(cached)
        merged.update(out)
        path.write_text(
            json.dumps({"date": today, "items": merged}, ensure_ascii=False),
            encoding="utf-8",
        )
    return out


def _close(text) -> int | None:
    if text is None:
        return None
    digits = str(text).replace(",", "").strip()
    if not digits.isdigit():
        return None
    return int(digits)
