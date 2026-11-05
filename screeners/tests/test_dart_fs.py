"""fnlttSinglAcntAll 의 fs_div 와 종목별 오류 처리. 응답은 모두 가짜다."""

from __future__ import annotations

import io
import json
import zipfile
from datetime import date

import pytest

from screeners.opendart import DartBudget, DartError, DartFatal, OpenDart
from screeners.run import discover_report, prefilter_reason, run_live, statements_for_company


def _zip(pairs: list[tuple[str, str]]) -> bytes:
    rows = "".join(
        f"<list><corp_code>{corp}</corp_code><stock_code>{code}</stock_code></list>"
        for code, corp in pairs
    )
    xml = f'<?xml version="1.0" encoding="UTF-8"?><result>{rows}</result>'.encode()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("CORPCODE.xml", xml)
    return buf.getvalue()


def _cash(scope: str) -> dict:
    return {
        "fs_div": scope,
        "sj_div": "BS",
        "account_nm": "현금및현금성자산",
        "account_detail": "-",
        "thstrm_amount": "100000000000",
        "currency": "KRW",
        "ord": "1",
    }


def test_accounts_send_fs_div_and_ofs_fills_missing_cfs(tmp_path):
    """상태 100 은 fs_div 를 빠뜨리면 난다. CFS 가 013 이면 OFS 만 남긴다."""
    calls = []

    def transport(url, params):
        calls.append((url, dict(params)))
        if url.endswith("corpCode.xml"):
            return _zip([("005930", "SAM"), ("214180", "HEC")])
        if "fnltt" in url:
            assert params.get("fs_div") in {"CFS", "OFS"}
            if params["fs_div"] == "CFS":
                return json.dumps({"status": "013", "message": "조회된 데이타가 없습니다."}).encode()
            return json.dumps({"status": "000", "list": [_cash("OFS")]}).encode()
        raise AssertionError(url)

    dart = OpenDart("SECRET", tmp_path, transport=transport, sleep_s=0)
    found = discover_report(dart, date(2026, 11, 5))
    assert found == (2026, "11014")
    fnltt = [params for url, params in calls if "fnltt" in url]
    assert fnltt
    assert all("fs_div" in params for params in fnltt)
    statements = statements_for_company(dart, "HEC", date(2026, 11, 5), found)
    scopes = {row["fs_div"] for row in statements[0]["rows"]}
    assert scopes == {"OFS"}
    assert all(params.get("fs_div") for params in fnltt)


def test_company_status_100_skips_and_bad_key_aborts(tmp_path):
    class Fake:
        def __init__(self, fatal: bool = False):
            self.fatal = fatal
            self.calls = 0
            self.cache_hits = 0
            self.seen: list[str] = []
            self.asked: list[str] = []

        def corp_code(self, ticker: str):
            self.asked.append(ticker)
            return {"005930": "SAM", "111111": "BAD", "222222": "OK"}.get(ticker)

        def accounts(self, corp, year, reprt, fs_div):
            self.calls += 1
            self.seen.append(str(corp))
            assert fs_div in {"CFS", "OFS"}
            if corp == "BAD":
                raise DartError("OpenDART 상태 100: 필수값(fs_div)이 누락되었습니다.")
            if self.fatal and corp == "OK":
                raise DartFatal("OpenDART 상태 010: 등록되지 않은 키")
            return [_cash(fs_div)]

        def dividends(self, *args):
            return []

        def shareholders(self, *args):
            return []

        def disclosures(self, *args):
            return []

    listing = [
        {"code": "111111", "name": "오류종목", "market": "KOSPI", "market_cap_krw": 50_000_000_000, "close": 1000},
        {"code": "222222", "name": "정상종목", "market": "KOSPI", "market_cap_krw": 50_000_000_000, "close": 1000},
        {"code": "333333", "name": "비싼종목", "market": "KOSPI", "market_cap_krw": 50_000_000_000, "close": 1000},
    ]

    def ratios(code):
        if code == "333333":
            return 5.0, 3.0
        return 5.0, 0.5

    dart = Fake()
    rows = run_live(
        "truen",
        None,
        None,
        True,
        tmp_path,
        date(2026, 11, 5),
        None,
        listing=listing,
        dart=dart,
        ratio_fetch=ratios,
        ratio_cache=tmp_path / "ratios.json",
    )
    assert [row["code"] for row in rows] == ["222222"]
    saved = json.loads((tmp_path / "truen.json").read_text(encoding="utf-8"))
    assert saved["checked"] == 1
    assert saved["skipped"] == 2
    reasons = {item["code"]: item["reason"] for item in saved["skipped_rows"]}
    assert "100" in reasons["111111"]
    assert reasons["333333"] == "PBR이 기준 밖"
    assert "333333" not in dart.asked
    assert saved["dart_calls"] == dart.calls
    assert "SECRET" not in json.dumps(saved["skipped_rows"], ensure_ascii=False)

    fatal = Fake(fatal=True)
    with pytest.raises(SystemExit) as caught:
        run_live(
            "truen",
            None,
            None,
            True,
            tmp_path,
            date(2026, 11, 5),
            None,
            listing=[listing[1]],
            dart=fatal,
            ratio_fetch=lambda code: (5.0, 0.5),
            ratio_cache=tmp_path / "ratios2.json",
        )
    assert "010" in str(caught.value)
    assert "SECRET" not in str(caught.value)


def test_call_budget_stops_without_dropping_finished_rows(tmp_path):
    class Tiny:
        def __init__(self):
            self.calls = 0
            self.cache_hits = 0
            self.max_calls = 1

        def corp_code(self, ticker: str):
            return "SAM" if ticker == "005930" else "C" + ticker

        def accounts(self, corp, year, reprt, fs_div):
            if self.calls >= self.max_calls:
                raise DartBudget("OpenDART 호출 상한 1회")
            self.calls += 1
            return [_cash(fs_div)]

    listing = [
        {"code": "222222", "name": "정상종목", "market": "KOSPI", "market_cap_krw": 50_000_000_000, "close": 1000},
    ]
    dart = Tiny()
    rows = run_live(
        "kiyohara",
        None,
        None,
        True,
        tmp_path,
        date(2026, 11, 5),
        None,
        listing=listing,
        dart=dart,
        ratio_fetch=lambda code: (4.0, 0.4),
        ratio_cache=tmp_path / "ratios.json",
    )
    assert rows == []
    saved = json.loads((tmp_path / "kiyohara.json").read_text(encoding="utf-8"))
    assert saved["checked"] == 0
    assert saved["skipped"] == 1
    assert saved["skipped_rows"][0]["reason"] == "OpenDART 호출 상한"
    assert "호출 상한" in saved["budget_note"]


def test_prefilter_requires_listing_ratios():
    config = {"per_max": 10, "pbr_max": 1, "mcap_min_krw": 0, "mcap_max_krw": None, "exclude_financial_keywords": ["은행"]}
    assert prefilter_reason({"name": "예시", "market_cap_krw": 100, "per": None, "pbr": None}, config) == "PER 또는 PBR 없음"
    assert prefilter_reason({"name": "예시", "market_cap_krw": 100, "per": 11, "pbr": 0.4}, config) == "PER이 기준 밖"
    assert prefilter_reason({"name": "예시", "market_cap_krw": 100, "per": 4, "pbr": 0.4}, config) is None
    assert prefilter_reason({"name": "예시은행", "market_cap_krw": 100, "per": 4, "pbr": 0.4}, config) == "금융업 제외"


def test_plain_name_is_one_dart_call_and_cap_skips_the_rest(tmp_path):
    class One:
        def __init__(self):
            self.calls = 0
            self.cache_hits = 0
            self.by_corp: dict[str, int] = {}

        def corp_code(self, ticker: str):
            return "SAM" if ticker == "005930" else "C" + ticker

        def accounts(self, corp, year, reprt, fs_div):
            self.calls += 1
            self.by_corp[corp] = self.by_corp.get(corp, 0) + 1
            assert fs_div == "CFS"
            return [_cash("CFS")]

    listing = [
        {"code": f"{i:06d}", "name": f"종목{i}", "market": "KOSPI", "market_cap_krw": 50_000_000_000 + i, "close": 1000}
        for i in range(1, 5)
    ]
    dart = One()
    run_live(
        "kiyohara",
        None,
        2,
        True,
        tmp_path,
        date(2026, 11, 5),
        None,
        listing=listing,
        dart=dart,
        ratio_fetch=lambda code: (4.0, 0.4),
        ratio_cache=tmp_path / "ratios.json",
    )
    saved = json.loads((tmp_path / "kiyohara.json").read_text(encoding="utf-8"))
    assert saved["checked"] == 2
    assert sum(1 for row in saved["skipped_rows"] if row["reason"] == "조회 상한") == 2
    company_calls = {corp: n for corp, n in dart.by_corp.items() if corp != "SAM"}
    assert company_calls
    assert all(n == 1 for n in company_calls.values())
    assert saved["elapsed_sec"] is not None


def test_cache_hit_does_not_count_as_call(tmp_path):
    def transport(url, params):
        if "fnltt" in url:
            assert params["fs_div"] == "CFS"
            return json.dumps({"status": "000", "list": [_cash("CFS")]}).encode()
        raise AssertionError(url)

    dart = OpenDart("SECRET", tmp_path, transport=transport, sleep_s=0, max_calls=2)
    assert dart.accounts("1", 2026, "11012", "CFS")
    assert dart.calls == 1
    assert dart.accounts("1", 2026, "11012", "CFS")
    assert dart.calls == 1
    assert dart.cache_hits == 1
    with pytest.raises(DartBudget):
        dart.accounts("2", 2026, "11012", "CFS")
        dart.accounts("3", 2026, "11012", "CFS")
