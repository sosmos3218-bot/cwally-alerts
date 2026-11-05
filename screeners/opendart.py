"""OpenDART 조회.

환경변수 OPENDART_API_KEY 만 읽는다. 키 값은 로그, 예외, 캐시에 남기지 않는다.
"""

from __future__ import annotations

import io
import json
import os
import time
import zipfile
from pathlib import Path
from xml.etree import ElementTree

from screeners.money import redact

CORP_URL = "https://opendart.fss.or.kr/api/corpCode.xml"
ACCT_URL = "https://opendart.fss.or.kr/api/fnlttSinglAcntAll.json"
HOLDER_URL = "https://opendart.fss.or.kr/api/hyslrSttus.json"
DIVIDEND_URL = "https://opendart.fss.or.kr/api/alotMatter.json"
LIST_URL = "https://opendart.fss.or.kr/api/list.json"

# 010 미등록 키, 011 정지된 키, 012 거부된 IP, 020 일일 한도.
# 이 상태만 전체를 멈춘다. 013 은 데이터 없음, 100 같은 건 그 종목만 건너뛴다.
SYSTEMIC_STATUS = {"010", "011", "012", "020"}
DEFAULT_CALL_BUDGET = 8000


class DartError(RuntimeError):
    pass


class DartFatal(DartError):
    """키 거부나 일일 한도. 다음 종목으로 가면 안 된다."""


class DartBudget(DartError):
    """우리가 정한 호출 상한. 이미 본 결과는 남기고 나머지를 건너뛴다."""


def api_key_from_env() -> str:
    return (os.environ.get("OPENDART_API_KEY") or "").strip()


def probe_order(asof) -> list[tuple[int, str]]:
    """최신 보고서부터. 11월이면 3분기를 먼저 보고, 없으면 반기."""
    year = asof.year
    month = asof.month
    order: list[tuple[int, str]] = []
    if month >= 11:
        order += [(year, "11014"), (year, "11012"), (year, "11013")]
    elif month >= 8:
        order += [(year, "11012"), (year, "11013"), (year, "11014")]
    elif month >= 5:
        order += [(year, "11013"), (year, "11012")]
    order += [(year - 1, "11011"), (year - 1, "11014"), (year - 1, "11012"), (year - 1, "11013")]
    seen = set()
    out = []
    for item in order:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def parse_corp_zip(content: bytes) -> dict[str, str]:
    if content[:2] != b"PK":
        raise DartError("OpenDART 고유번호 응답이 zip 이 아닙니다.")
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        names = [name for name in zf.namelist() if name.lower().endswith(".xml")]
        if not names:
            raise DartError("OpenDART 고유번호 zip 에 xml 이 없습니다.")
        xml_bytes = zf.read(names[0])
    root = ElementTree.fromstring(xml_bytes)
    out: dict[str, str] = {}
    for item in root.iter("list"):
        stock = (item.findtext("stock_code") or "").strip()
        corp = (item.findtext("corp_code") or "").strip()
        if stock.isdigit() and corp:
            out[stock.zfill(6)] = corp
    if not out:
        raise DartError("OpenDART 고유번호 목록이 비었습니다.")
    return out


class OpenDart:
    def __init__(self, api_key: str, cache_dir: Path, transport=None, sleep_s: float = 0.25, max_calls: int = DEFAULT_CALL_BUDGET):
        self.api_key = api_key
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.transport = transport or _http_get
        self.sleep_s = sleep_s
        self.max_calls = max(0, int(max_calls))
        self.calls = 0
        self.cache_hits = 0
        self._codes: dict[str, str] | None = None

    def corp_code(self, ticker: str) -> str | None:
        codes = self._load_codes()
        return codes.get(str(ticker).zfill(6))

    def accounts(self, corp_code: str, year: int, reprt: str, fs_div: str) -> list[dict]:
        """연결(CFS) 또는 별도(OFS). fnlttSinglAcntAll 은 fs_div 가 없으면 상태 100 이다."""
        scope = str(fs_div or "").upper()
        if scope not in {"CFS", "OFS"}:
            raise DartError("fs_div 는 CFS 또는 OFS 여야 합니다.")
        return self._rows(
            ACCT_URL,
            corp_code,
            year,
            reprt,
            f"acct-{scope}",
            extra={"fs_div": scope},
        )

    def shareholders(self, corp_code: str, year: int, reprt: str) -> list[dict]:
        return self._rows(HOLDER_URL, corp_code, year, reprt, "holder")

    def dividends(self, corp_code: str, year: int, reprt: str) -> list[dict]:
        return self._rows(DIVIDEND_URL, corp_code, year, reprt, "div")

    def disclosures(self, corp_code: str, bgn: str, end: str) -> list[dict]:
        cache = self.cache_dir / f"list-{corp_code}-{bgn}-{end}.json"
        if cache.is_file():
            cached = json.loads(cache.read_text(encoding="utf-8"))
            return list(cached.get("list") or [])
        body = self._call(LIST_URL, {"corp_code": corp_code, "bgn_de": bgn, "end_de": end, "page_count": "100"})
        rows = list(body.get("list") or [])
        cache.write_text(json.dumps({"list": rows}, ensure_ascii=False), encoding="utf-8")
        return rows

    def _rows(self, url: str, corp_code: str, year: int, reprt: str, kind: str, extra: dict | None = None) -> list[dict]:
        cache = self.cache_dir / f"{kind}-{corp_code}-{year}-{reprt}.json"
        if cache.is_file():
            self.cache_hits += 1
            cached = json.loads(cache.read_text(encoding="utf-8"))
            return list(cached.get("list") or [])
        params = {"corp_code": corp_code, "bsns_year": str(year), "reprt_code": reprt}
        if extra:
            params.update(extra)
        body = self._call(url, params)
        status = str(body.get("status") or "")
        if status == "013":
            rows: list[dict] = []
        else:
            rows = list(body.get("list") or [])
        cache.write_text(json.dumps({"status": status, "list": rows}, ensure_ascii=False), encoding="utf-8")
        return rows

    def _load_codes(self) -> dict[str, str]:
        if self._codes is not None:
            return self._codes
        cache = self.cache_dir / "corp_codes.json"
        if cache.is_file():
            self._codes = json.loads(cache.read_text(encoding="utf-8"))
            return self._codes
        time.sleep(self.sleep_s)
        try:
            content = self.transport(CORP_URL, {"crtfc_key": self.api_key})
        except DartError:
            raise
        except Exception as exc:
            raise DartError("OpenDART 고유번호 조회 실패. " + redact(str(exc), self.api_key)) from exc
        self._codes = parse_corp_zip(content)
        cache.write_text(json.dumps(self._codes, ensure_ascii=False), encoding="utf-8")
        return self._codes

    def _call(self, url: str, params: dict) -> dict:
        if self.calls >= self.max_calls:
            raise DartBudget(
                f"OpenDART 호출 상한 {self.max_calls}회. 일일 한도(키당 약 2만, 추세 모니터와 공유)를 남기려고 멈춤"
            )
        self.calls += 1
        time.sleep(self.sleep_s)
        query = {"crtfc_key": self.api_key, **params}
        try:
            content = self.transport(url, query)
        except DartError:
            raise
        except Exception as exc:
            raise DartError("OpenDART 조회 실패. " + redact(str(exc), self.api_key)) from exc
        if isinstance(content, dict):
            body = content
        else:
            text = content.decode("utf-8", "replace").strip() if isinstance(content, bytes) else str(content)
            if not text.startswith("{") and not text.startswith("["):
                raise DartError("OpenDART 응답이 JSON 이 아닙니다.")
            body = json.loads(text)
        status = str(body.get("status") or "")
        message = redact(str(body.get("message") or ""), self.api_key)
        if status in SYSTEMIC_STATUS:
            raise DartFatal(f"OpenDART 상태 {status}: {message}".strip())
        if status not in {"", "000", "013"}:
            raise DartError(f"OpenDART 상태 {status}: {message}".strip())
        return body


def _http_get(url: str, params: dict) -> bytes:
    import requests

    try:
        response = requests.get(
            url,
            params=params,
            timeout=30,
            headers={"User-Agent": "cwally-alerts-screeners"},
        )
    except Exception as exc:
        raise DartError(redact(str(exc), params.get("crtfc_key"))) from exc
    if response.status_code != 200:
        raise DartError(redact(f"HTTP {response.status_code}", params.get("crtfc_key")))
    return response.content
