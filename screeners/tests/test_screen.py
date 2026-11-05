"""픽스처와 가짜 OpenDART 행으로 조정 순현금, 트루엔, 기요하라를 본다."""

from __future__ import annotations

import io
import json
import zipfile
from datetime import date

from screeners.cli import EXPECTED, main, selftest
from screeners.evaluate import evaluate_company
from screeners.money import EOK, parse_krw, redact
from screeners.net_cash import compute_scope
from screeners.opendart import OpenDart, parse_corp_zip, probe_order
from screeners.prices import naver_quote, parse_market_text
from screeners.rows import sum_tag
from screeners.run import load_config, load_fixture, screen_fixtures
from screeners.universe import keep_listing_row


def _eok(value: float) -> int:
    return int(round(value * EOK))


def test_selftest_and_expected_passes():
    assert selftest() == 0
    for kind, table in EXPECTED.items():
        rows = {row["code"]: row for row in screen_fixtures(kind)}
        for code, want in table.items():
            assert rows[code]["passes"] is want


def test_hecto_adjusted_is_not_a_net_cash_bargain():
    row = evaluate_company(load_fixture("214180"), load_config("truen"))
    net = row["net_cash"]
    assert net["naive_krw"] == _eok(4790)
    assert net["adjusted_krw"] == _eok(686)
    assert net["lease_liability_krw"] == _eok(76)
    assert net["naive_krw"] >= row["market_cap_krw"]
    assert net["adjusted_krw"] < row["market_cap_krw"]
    assert net["screen_blocked"] is True
    deposits = net["cfs"]["components"]["deposits"]
    assert deposits == _eok(2907)
    assert net["cfs"]["components"]["settlement"] == _eok(1197)
    ids = [item["id"] for item in net["adjustments"]]
    assert ids.count("customer_deposits") == 1
    assert "예수금" in net["adjustments"][0]["reason"]
    assert "단기미수금" in net["adjustments"][1]["reason"]
    assert row["passes"] is False


def test_daewon_still_passes_and_keeps_naive():
    row = evaluate_company(load_fixture("005710"), load_config("kiyohara"))
    assert row["passes"] is True
    assert row["net_cash"]["naive_krw"] == row["net_cash"]["adjusted_krw"] == _eok(4469)
    assert row["net_cash"]["ratio"] > 1
    assert any(flag["id"] == "op_income_yoy_down" for flag in row["flags"])


def test_snt_flags_exchangeable_and_nci():
    row = evaluate_company(load_fixture("036530"), load_config("truen"))
    assert row["passes"] is False
    net = row["net_cash"]
    assert net["naive_krw"] - net["adjusted_krw"] == _eok(1026)
    exch = next(item for item in net["adjustments"] if item["id"] == "exchangeable_bond")
    assert exch["effect_krw"] == -_eok(1026)
    lease = next(item for item in net["adjustments"] if item["id"] == "lease_liability")
    assert lease["effect_krw"] == 0
    assert lease["noted_krw"] == _eok(100)
    assert any(flag["id"] == "nci" for flag in row["flags"])
    assert "비지배지분" in " ".join(row["fail_reasons"])
    kiyo = evaluate_company(load_fixture("036530"), load_config("kiyohara"))
    assert any("소형주" in reason for reason in kiyo["fail_reasons"])


def test_parent_statement_is_what_the_screen_uses():
    row = evaluate_company(load_fixture("900530"), load_config("kiyohara"))
    assert row["passes"] is True
    assert row["net_cash"]["screen_scope"] == "OFS"
    assert row["net_cash"]["screen_krw"] == _eok(2000)
    assert row["net_cash"]["naive_krw"] == _eok(10000)
    assert any(flag["id"] == "nci" for flag in row["flags"])
    assert any(flag["id"] == "exchangeable_bond" for flag in row["flags"])


def test_truen_model_passes_with_value_trap_flags_only():
    row = evaluate_company(load_fixture("417790"), load_config("truen"))
    assert row["passes"] is True
    assert row["per"] == 6.2
    assert row["pbr"] == 0.62
    assert row["net_cash"]["naive_krw"] == row["net_cash"]["adjusted_krw"] == _eok(708)
    ids = {flag["id"] for flag in row["flags"]}
    assert "revenue_yoy_down" in ids
    assert "op_income_yoy_down" in ids
    assert "financial_income" not in ids
    strict = load_config("kiyohara")
    missed = evaluate_company(load_fixture("417790"), strict)
    assert missed["passes"] is False
    assert any("기준 1.00" in reason for reason in missed["fail_reasons"])


def test_dongyang_financial_income_does_not_alone_fail_when_ratio_holds():
    payload = load_fixture("079960")
    flagged = evaluate_company(payload, load_config("truen"))
    assert any(flag["id"] == "financial_income" for flag in flagged["flags"])
    assert "240억" in next(flag["text"] for flag in flagged["flags"] if flag["id"] == "financial_income")
    # 순현금만 충분하면 금융수익 표시가 있어도 통과한다.
    payload = json.loads(json.dumps(payload))
    payload["market_cap_krw"] = _eok(40)
    for statement in payload["statements"]:
        for line in statement["rows"]:
            if line["account_nm"] == "현금및현금성자산" and line["sj_div"] == "BS":
                line["thstrm_amount"] = str(_eok(100))
            if line["account_nm"] == "단기차입금":
                line["thstrm_amount"] = "0"
            if "지배기업소유주지분" in line["account_nm"] or line["account_nm"] == "자본총계":
                line["thstrm_amount"] = str(_eok(80))
    # 반기 자본만 바꿔도 최신 재무상태를 쓴다. 순이익은 TTM 이라 PER 이 시총에 비해 매우 낮다.
    row = evaluate_company(payload, load_config("truen"))
    assert any(flag["id"] == "financial_income" for flag in row["flags"])
    assert row["passes"] is True


def test_exchangeable_inside_bond_is_not_subtracted_twice():
    rows = [
        {"fs_div": "CFS", "sj_div": "BS", "account_nm": "현금및현금성자산", "account_detail": "-", "thstrm_amount": str(_eok(1000)), "currency": "KRW", "ord": "1"},
        {"fs_div": "CFS", "sj_div": "BS", "account_nm": "사채", "account_detail": "-", "thstrm_amount": str(_eok(500)), "currency": "KRW", "ord": "2"},
        {"fs_div": "CFS", "sj_div": "BS", "account_nm": "사채", "account_detail": "교환사채", "thstrm_amount": str(_eok(200)), "currency": "KRW", "ord": "3"},
    ]
    scope = compute_scope(rows, "CFS")
    assert scope.naive_krw == _eok(500)
    assert scope.adjusted_krw == _eok(500)
    item = scope.adjustments[0]
    assert item.id == "exchangeable_bond"
    assert item.effect_krw == 0
    assert item.noted_krw == _eok(200)
    assert "한 번 더 빼지 않음" in item.reason


def test_operating_income_synonym_is_not_double_counted():
    rows = [
        {"fs_div": "OFS", "sj_div": "IS", "account_nm": "영업이익", "account_detail": "-", "thstrm_amount": "100", "currency": "KRW", "ord": "1"},
        {"fs_div": "OFS", "sj_div": "IS", "account_nm": "영업이익(손실)", "account_detail": "-", "thstrm_amount": "100", "currency": "KRW", "ord": "2"},
    ]
    assert sum_tag(rows, "op_income", "end", "OFS") == 100


def test_ytd_alone_does_not_invent_per():
    payload = {
        "code": "000001",
        "name": "누적만",
        "market": "KOSDAQ",
        "market_cap_krw": _eok(100),
        "statements": [
            {
                "year": 2026,
                "reprt_code": "11012",
                "rows": [
                    {"fs_div": "OFS", "sj_div": "BS", "account_nm": "현금및현금성자산", "account_detail": "-", "thstrm_amount": str(_eok(100)), "currency": "KRW", "ord": "1"},
                    {"fs_div": "OFS", "sj_div": "BS", "account_nm": "자본총계", "account_detail": "-", "thstrm_amount": str(_eok(200)), "currency": "KRW", "ord": "2"},
                    {"fs_div": "OFS", "sj_div": "IS", "account_nm": "매출액", "account_detail": "-", "thstrm_amount": "1", "thstrm_add_amount": str(_eok(50)), "frmtrm_add_amount": str(_eok(40)), "currency": "KRW", "ord": "1"},
                    {"fs_div": "OFS", "sj_div": "IS", "account_nm": "영업이익", "account_detail": "-", "thstrm_amount": "1", "thstrm_add_amount": str(_eok(10)), "frmtrm_add_amount": str(_eok(8)), "currency": "KRW", "ord": "2"},
                    {"fs_div": "OFS", "sj_div": "IS", "account_nm": "당기순이익", "account_detail": "-", "thstrm_amount": "1", "thstrm_add_amount": str(_eok(8)), "frmtrm_add_amount": str(_eok(6)), "currency": "KRW", "ord": "3"},
                ],
            }
        ],
        "dividend_rows": [],
        "shareholders": [],
        "disclosures": [],
    }
    row = evaluate_company(payload, load_config("truen"))
    assert row["per"] is None
    assert any("연환산하지 않음" in reason for reason in row["fail_reasons"])


def test_bank_name_is_excluded_and_low_stake_is_only_a_flag():
    payload = load_fixture("005710")
    bank = json.loads(json.dumps(payload))
    bank["name"] = "예시은행"
    assert any("금융업" in reason for reason in evaluate_company(bank, load_config("truen"))["fail_reasons"])
    low = json.loads(json.dumps(payload))
    low["shareholders"] = [{"nm": "소액", "relate": "본인", "stock_knd": "보통주", "trmend_posesn_stock_qota_rt": "3.0"}]
    row = evaluate_company(low, load_config("truen"))
    assert row["passes"] is True
    assert any(flag["id"] == "low_largest_shareholder" for flag in row["flags"])


def test_universe_drops_preferred_spac_etf():
    markets = {"KOSPI", "KOSDAQ", "KOSDAQ GLOBAL"}
    assert keep_listing_row({"code": "005930", "name": "삼성전자", "market": "KOSPI"}, markets)
    assert not keep_listing_row({"code": "005935", "name": "삼성전자우", "market": "KOSPI"}, markets)
    assert not keep_listing_row({"code": "000001", "name": "케이비스팩", "market": "KOSDAQ"}, markets)
    assert not keep_listing_row({"code": "069500", "name": "KODEX 200 ETF", "market": "KOSPI"}, markets)
    assert not keep_listing_row({"code": "123456", "name": "테스트", "market": "KONEX"}, markets)


def test_naver_quote_reads_exchange_name():
    bodies = {
        "integration": {
            "stockName": "대원산업",
            "totalInfos": [{"code": "marketValue", "value": "2,114억"}, {"code": "lastClosePrice", "value": "10,450"}],
        },
        "basic": {"stockName": "대원산업", "closePrice": "10,450", "stockExchangeType": {"nameEng": "KOSDAQ"}},
    }

    class Response:
        def __init__(self, payload):
            self._raw = json.dumps(payload).encode()

        def read(self):
            return self._raw

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def opener(req, timeout=20):
        url = req.full_url
        key = "basic" if url.endswith("/basic") else "integration"
        return Response(bodies[key])

    quote = naver_quote("005710", opener=opener)
    assert quote["market"] == "KOSDAQ"
    assert quote["market_cap_krw"] == 2114 * EOK
    assert quote["name"] == "대원산업"


def test_parse_helpers_and_probe_month():
    assert parse_krw("(1,234)") == -1234
    assert parse_krw("△80") == -80
    assert parse_krw("-") is None
    assert parse_market_text("1,564억") == _eok(1564)
    assert parse_market_text("1.12조") == _eok(11200)
    assert probe_order(date(2026, 11, 5))[0] == (2026, "11014")
    assert redact("crtfc_key=SECRET&x=1", "SECRET") == "crtfc_key=***&x=1"


def test_dart_client_uses_fixture_bytes_and_hides_key():
    xml = """<?xml version="1.0" encoding="UTF-8"?>
    <result><list><corp_code>001</corp_code><stock_code>417790</stock_code></list></result>
    """.encode()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("CORPCODE.xml", xml)
    codes = parse_corp_zip(buf.getvalue())
    assert codes["417790"] == "001"

    def transport(url, params):
        if "SECRET" in json.dumps(params):
            assert params["crtfc_key"] == "SECRET"
        if url.endswith("corpCode.xml"):
            return buf.getvalue()
        if "fnltt" in url:
            return json.dumps(
                {"status": "000", "list": [{"account_nm": "현금및현금성자산", "thstrm_amount": "10", "fs_div": "OFS", "sj_div": "BS"}]}
            ).encode()
        raise RuntimeError(f"boom crtfc_key={params['crtfc_key']}")

    dart = OpenDart("SECRET", cache_dir=_tmp(), transport=transport, sleep_s=0)
    assert dart.corp_code("417790") == "001"
    rows = dart.accounts("001", 2026, "11012")
    assert rows[0]["account_nm"] == "현금및현금성자산"
    try:
        dart.disclosures("001", "20240101", "20241105")
    except Exception as exc:
        text = str(exc)
    else:
        raise AssertionError("should fail")
    assert "SECRET" not in text
    assert "crtfc_key=***" in text or "***" in text


def test_cli_demo_writes_output_without_network(tmp_path):
    assert main(["truen", "--demo", "--out", str(tmp_path)]) == 0
    text = (tmp_path / "truen.md").read_text(encoding="utf-8")
    assert "417790" in text
    assert "통과" in text
    saved = json.loads((tmp_path / "truen.json").read_text(encoding="utf-8"))
    assert saved["passes"] >= 1


def _tmp():
    import tempfile

    return tempfile.mkdtemp()
