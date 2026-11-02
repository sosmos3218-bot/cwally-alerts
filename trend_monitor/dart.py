"""OpenDART 분기 EPS 전년비.

환경변수 OPENDART_API_KEY 만 읽습니다. 키 값은 로그에 남기지 않습니다.
corpCode.xml(zip) 으로 종목코드 -> 고유번호를 찾고,
fnlttSinglAcnt 로 분기 보고서를 읽습니다.
2, 3, 4분기는 누적 기본주당순이익에서 직전 누적을 빼 분기를 나눕니다.
전년 값이 음수이거나 흑자전환이어도 (이번-직전)/절대값 으로 계산합니다.
전년 값이 0이면 계산하지 않고 unknown 입니다.
"""

from __future__ import annotations

import io
import json
import os
import re
import time
import zipfile
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from xml.etree import ElementTree

from trend_monitor.errors import TrendDataError

CORP_URL = "https://opendart.fss.or.kr/api/corpCode.xml"
ACCT_URL = "https://opendart.fss.or.kr/api/fnlttSinglAcnt.json"
ACCT_ALL_URL = "https://opendart.fss.or.kr/api/fnlttSinglAcntAll.json"

# 1분기, 반기, 3분기, 사업보고서
REPRT = {1: "11013", 2: "11012", 3: "11014", 4: "11011"}
_NEED = {1: ("11013",), 2: ("11012", "11013"), 3: ("11014", "11012"), 4: ("11011", "11014")}


def redact(text: str, key: str | None) -> str:
    if not text:
        return ""
    out = text
    if key:
        out = out.replace(key, "***")
    out = re.sub(r"crtfc_key=[^&\s]+", "crtfc_key=***", out)
    return out


class RateLimiter:
    def __init__(self, interval: float, clock=time.monotonic, sleep=time.sleep):
        self.interval = max(0.0, float(interval))
        self.clock = clock
        self.sleep = sleep
        self._next = 0.0

    def wait(self) -> None:
        now = self.clock()
        if now < self._next:
            self.sleep(self._next - now)
            now = self.clock()
        self._next = now + self.interval


def parse_amount(raw) -> Decimal | None:
    """쉼표, 괄호 음수, 삼각형 기호를 읽습니다. 빈 값은 None."""
    if raw is None:
        return None
    text = str(raw).strip()
    if text == "" or text in {"-", "nan", "NaN"}:
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
    return -value if neg else value


def is_basic_eps(name: str | None) -> bool:
    compact = re.sub(r"\s+", "", name or "")
    if "희석" in compact:
        return False
    if "기본" in compact and "주당" in compact:
        return True
    return compact.startswith("주당순이익") or compact.startswith("주당순손익")


def yoy_pct(current: Decimal | None, prior: Decimal | None) -> Decimal | None:
    """(이번 - 직전) / abs(직전) * 100.

    직전이 양수면 보통의 전년비와 같습니다.
    직전이 음수면 적자 축소와 흑자전환이 양수가 됩니다.
    직전이 0이면 None.
    """
    if current is None or prior is None or prior == 0:
        return None
    pct = (current - prior) / abs(prior) * Decimal(100)
    return pct.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def isolate_quarter(quarter: int, cumulative: dict[str, Decimal | None]) -> Decimal | None:
    """누적 기본주당순이익에서 해당 분기만 꺼냅니다. 재료가 빠지면 None."""
    if quarter not in _NEED:
        raise TrendDataError(f"분기 번호가 1에서 4가 아닙니다: {quarter}")
    if quarter == 1:
        return cumulative.get("11013")
    left, right = _NEED[quarter]
    a = cumulative.get(left)
    b = cumulative.get(right)
    if a is None or b is None:
        return None
    return a - b


def quarter_end(year: int, quarter: int) -> date:
    month_day = {1: (3, 31), 2: (6, 30), 3: (9, 30), 4: (12, 31)}[quarter]
    return date(year, month_day[0], month_day[1])


def quarters_to_try(trade_date: date, lookback: int = 4) -> list[tuple[int, int]]:
    """분기가 끝난 뒤의 날짜만, 최근 것부터."""
    year = trade_date.year
    quarter = (trade_date.month - 1) // 3 + 1
    out: list[tuple[int, int]] = []
    guard = 0
    while len(out) < lookback and guard < 12:
        guard += 1
        if trade_date > quarter_end(year, quarter):
            out.append((year, quarter))
        quarter -= 1
        if quarter == 0:
            quarter = 4
            year -= 1
    return out


def pick_eps_amount(rows: list[dict] | None) -> Decimal | None:
    if not rows:
        return None
    chosen = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if not is_basic_eps(row.get("account_nm")):
            continue
        chosen.append(row)
    if not chosen:
        return None
    cfs = [row for row in chosen if str(row.get("fs_div") or "").upper() == "CFS"]
    row = (cfs or chosen)[0]
    return parse_amount(row.get("thstrm_amount"))


def parse_corp_zip(content: bytes) -> dict[str, str]:
    """종목코드 6자리 -> 고유번호. 상장 코드가 없는 회사는 빠집니다."""
    if content[:2] != b"PK":
        raise TrendDataError("OpenDART 고유번호 응답이 zip 이 아닙니다.")
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        names = [n for n in zf.namelist() if n.lower().endswith(".xml")]
        if not names:
            raise TrendDataError("OpenDART 고유번호 zip 에 xml 이 없습니다.")
        xml_bytes = zf.read(names[0])
    root = ElementTree.fromstring(xml_bytes)
    out: dict[str, str] = {}
    for item in root.iter("list"):
        stock = (item.findtext("stock_code") or "").strip()
        corp = (item.findtext("corp_code") or "").strip()
        if stock.isdigit() and corp:
            out[stock.zfill(6)] = corp
    if not out:
        raise TrendDataError("OpenDART 고유번호 목록이 비었습니다.")
    return out


def _loads_json(content: bytes) -> dict | None:
    text = content.decode("utf-8", "replace").strip()
    if not text.startswith("{") and not text.startswith("["):
        return None
    data = json.loads(text)
    return data if isinstance(data, dict) else {"list": data}


class DartClient:
    def __init__(
        self,
        api_key: str,
        cache_dir: Path,
        min_interval: float = 0.25,
        transport=None,
        limiter: RateLimiter | None = None,
    ):
        self.api_key = api_key
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.transport = transport or _http_get
        self.limiter = limiter or RateLimiter(min_interval)
        self.auth_failed = False
        self.auth_message = ""
        self._codes: dict[str, str] | None = None

    def corp_code(self, ticker: str) -> str | None:
        codes = self._load_codes()
        if codes is None:
            return None
        return codes.get(str(ticker).zfill(6))

    def statement(self, corp_code: str, year: int, reprt_code: str) -> list[dict] | None:
        """보고서 행. 데이터 없음은 빈 목록, 인증 실패는 None."""
        if self.auth_failed:
            return None
        cache = self.cache_dir / f"acct-{corp_code}-{year}-{reprt_code}.json"
        if cache.is_file():
            cached = json.loads(cache.read_text(encoding="utf-8"))
            if cached.get("status") == "000":
                return list(cached.get("list") or [])
            if cached.get("status") == "013":
                return []
        found: list[dict] | None = None
        last_status = ""
        for url in (ACCT_URL, ACCT_ALL_URL):
            body = self._call(url, {"corp_code": corp_code, "bsns_year": str(year), "reprt_code": reprt_code})
            if body is None:
                return None
            status = str(body.get("status") or "")
            last_status = status
            rows = list(body.get("list") or [])
            if status == "000" and pick_eps_amount(rows) is not None:
                cache.write_text(json.dumps({"status": "000", "list": rows}, ensure_ascii=False), encoding="utf-8")
                return rows
            if status == "000" and found is None:
                found = rows
        if found is not None:
            cache.write_text(json.dumps({"status": "000", "list": found}, ensure_ascii=False), encoding="utf-8")
            return found
        if last_status == "013":
            cache.write_text(json.dumps({"status": "013", "list": []}, ensure_ascii=False), encoding="utf-8")
        return []

    def quarter_eps(self, corp_code: str, year: int, quarter: int) -> Decimal | None:
        bag: dict[str, Decimal | None] = {}
        for code in _NEED[quarter]:
            rows = self.statement(corp_code, year, code)
            if rows is None:
                return None
            bag[code] = pick_eps_amount(rows)
        return isolate_quarter(quarter, bag)

    def eps_yoy(self, ticker: str, trade_date: date) -> tuple[float | None, str]:
        if self.auth_failed:
            return None, self.auth_message or "OpenDART 인증 실패. EPS unknown."
        corp = self.corp_code(ticker)
        if self.auth_failed:
            return None, self.auth_message or "OpenDART 인증 실패. EPS unknown."
        if not corp:
            return None, "OpenDART 고유번호 없음. EPS unknown."
        saw_partial = False
        for year, quarter in quarters_to_try(trade_date):
            current = self.quarter_eps(corp, year, quarter)
            prior = self.quarter_eps(corp, year - 1, quarter)
            if self.auth_failed:
                return None, self.auth_message or "OpenDART 인증 실패. EPS unknown."
            if current is None and prior is None:
                continue
            if current is None or prior is None:
                saw_partial = True
                continue
            pct = yoy_pct(current, prior)
            label = f"{year}Q{quarter}"
            if pct is None:
                return None, f"{label} 직전 EPS가 0이라 전년비를 계산하지 않습니다. EPS unknown."
            return float(pct), (
                f"{label} 기본주당순이익 {current} 대 전년 {prior}, 전년비 {pct}%"
                " (2분기 이후는 누적 EPS 차이, 직전 음수는 절대값 기준)."
            )
        if saw_partial:
            return None, "분기 EPS 재료가 일부만 있어 전년비를 만들지 않습니다. EPS unknown."
        return None, "최근 분기 실적이 없습니다. EPS unknown."

    def _load_codes(self) -> dict[str, str] | None:
        if self._codes is not None:
            return self._codes
        if self.auth_failed:
            return None
        cache = self.cache_dir / "corp_codes.json"
        if cache.is_file():
            self._codes = json.loads(cache.read_text(encoding="utf-8"))
            return self._codes
        self.limiter.wait()
        try:
            content = self.transport(CORP_URL, {"crtfc_key": self.api_key})
        except Exception as exc:
            self.auth_message = "OpenDART 고유번호 조회 실패. EPS unknown. " + redact(str(exc), self.api_key)
            self.auth_failed = True
            return None
        if content[:2] != b"PK":
            meta = _loads_json(content) or {}
            status = str(meta.get("status") or "")
            message = redact(str(meta.get("message") or "고유번호 응답이 zip 이 아닙니다."), self.api_key)
            self.auth_message = f"OpenDART 고유번호 상태 {status or '없음'}: {message} EPS unknown."
            if status in {"010", "011", "012", "020", "100", "800", "900", "901"} or status == "":
                self.auth_failed = True
            return None
        try:
            self._codes = parse_corp_zip(content)
        except TrendDataError as exc:
            self.auth_message = redact(str(exc), self.api_key)
            self.auth_failed = True
            return None
        cache.write_text(json.dumps(self._codes, ensure_ascii=False), encoding="utf-8")
        return self._codes

    def _call(self, url: str, params: dict) -> dict | None:
        if self.auth_failed:
            return None
        self.limiter.wait()
        query = {"crtfc_key": self.api_key, **params}
        try:
            content = self.transport(url, query)
        except Exception as exc:
            self.auth_message = "OpenDART 재무 조회 실패. EPS unknown. " + redact(str(exc), self.api_key)
            return None
        meta = _loads_json(content)
        if meta is None:
            return {"status": "", "message": "json 아님"}
        status = str(meta.get("status") or "")
        if status in {"010", "011", "012"}:
            self.auth_failed = True
            self.auth_message = "OpenDART 키가 거부되었습니다. EPS unknown."
            return None
        if status == "020":
            # 한도. 한 번만 더 쉬고 재시도하지 않고 이 건은 실패로 둡니다.
            self.auth_message = "OpenDART 조회 한도에 걸렸습니다. EPS unknown."
            return None
        return meta


def _http_get(url: str, params: dict) -> bytes:
    import requests

    try:
        response = requests.get(url, params=params, timeout=30)
    except Exception as exc:
        raise TrendDataError(redact(str(exc), params.get("crtfc_key"))) from exc
    if response.status_code != 200:
        raise TrendDataError(redact(f"HTTP {response.status_code}", params.get("crtfc_key")))
    return response.content


def api_key_from_env() -> str:
    return (os.environ.get("OPENDART_API_KEY") or "").strip()


MISSING_KEY_MESSAGE = (
    "OPENDART_API_KEY 가 없습니다. EPS 전년비는 unknown 으로 두고 가격 조건만 계산합니다. "
    "키는 저장소 시크릿 OPENDART_API_KEY 로 넣으세요. README 를 보세요."
)
KEY_PRESENT_MESSAGE = "OPENDART_API_KEY 가 설정되어 있습니다. EPS 전년비를 조회합니다. 키 값은 출력하지 않습니다."
