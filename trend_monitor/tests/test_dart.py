import io
import json
import zipfile
from datetime import date
from decimal import Decimal

from trend_monitor.dart import (
    MISSING_KEY_MESSAGE,
    DartClient,
    RateLimiter,
    isolate_quarter,
    parse_amount,
    parse_corp_zip,
    quarters_to_try,
    yoy_pct,
)


def test_parse_amount_signs():
    assert parse_amount("1,234") == Decimal("1234")
    assert parse_amount("(1,234)") == Decimal("-1234")
    assert parse_amount("-50") == Decimal("-50")
    assert parse_amount("△80") == Decimal("-80")
    assert parse_amount("") is None
    assert parse_amount("-") is None


def test_yoy_turnaround_and_zero_base():
    assert yoy_pct(Decimal("150"), Decimal("100")) == Decimal("50.00")
    # 적자 -100 에서 흑자 50: (150)/100 = 150%
    assert yoy_pct(Decimal("50"), Decimal("-100")) == Decimal("150.00")
    # 적자 축소 -100 -> -40
    assert yoy_pct(Decimal("-40"), Decimal("-100")) == Decimal("60.00")
    # 적자 확대
    assert yoy_pct(Decimal("-200"), Decimal("-100")) == Decimal("-100.00")
    assert yoy_pct(Decimal("10"), Decimal("0")) is None
    assert yoy_pct(None, Decimal("10")) is None


def test_isolate_quarter_from_cumulative():
    bag = {"11013": Decimal("80"), "11012": Decimal("100"), "11014": Decimal("130"), "11011": Decimal("150")}
    assert isolate_quarter(1, bag) == Decimal("80")
    assert isolate_quarter(2, bag) == Decimal("20")
    assert isolate_quarter(3, bag) == Decimal("30")
    assert isolate_quarter(4, bag) == Decimal("20")
    assert isolate_quarter(2, {"11012": Decimal("100")}) is None


def test_quarters_on_november_second():
    assert quarters_to_try(date(2026, 11, 2))[0] == (2026, 3)
    assert quarters_to_try(date(2026, 9, 15))[0] == (2026, 2)


def test_corp_zip_and_cached_statement():
    xml = """<?xml version="1.0" encoding="UTF-8"?>
    <result>
      <list>
        <corp_code>00126380</corp_code>
        <corp_name>데모전자</corp_name>
        <stock_code>900001</stock_code>
        <modify_date>20260101</modify_date>
      </list>
      <list>
        <corp_code>00000000</corp_code>
        <corp_name>비상장</corp_name>
        <stock_code></stock_code>
        <modify_date>20260101</modify_date>
      </list>
    </result>
    """.encode()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("CORPCODE.xml", xml)
    codes = parse_corp_zip(buf.getvalue())
    assert codes == {"900001": "00126380"}

    calls = []

    def transport(url, params):
        calls.append((url, dict(params)))
        assert "crtfc_key" in params
        if url.endswith("corpCode.xml"):
            return buf.getvalue()
        year = params["bsns_year"]
        # 2026 Q3 누적 130, 2026 반기 100, 2025 Q3 누적 80, 2025 반기 60
        # 분기 EPS 2026Q3 = 30, 2025Q3 = 20, 전년비 50%
        table = {
            ("2026", "11014"): "130",
            ("2026", "11012"): "100",
            ("2025", "11014"): "80",
            ("2025", "11012"): "60",
        }
        amount = table.get((year, params["reprt_code"]))
        if amount is None:
            return json.dumps({"status": "013", "message": "조회된 데이타가 없습니다."}).encode()
        body = {
            "status": "000",
            "list": [
                {
                    "account_nm": "희석주당순이익",
                    "fs_div": "CFS",
                    "sj_div": "IS",
                    "thstrm_amount": "999",
                },
                {
                    "account_nm": "기본주당순이익",
                    "fs_div": "OFS",
                    "sj_div": "IS",
                    "thstrm_amount": "1",
                },
                {
                    "account_nm": "기본주당순이익",
                    "fs_div": "CFS",
                    "sj_div": "IS",
                    "thstrm_amount": amount,
                },
            ],
        }
        return json.dumps(body).encode()

    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        client = DartClient("secret-key", Path(tmp), min_interval=0, transport=transport)
        pct, note = client.eps_yoy("900001", date(2026, 11, 2))
        assert pct == 50.0
        assert "2026Q3" in note
        first = len(calls)
        pct2, _ = client.eps_yoy("900001", date(2026, 11, 2))
        assert pct2 == 50.0
        # 고유번호와 재무 응답은 캐시되므로 두 번째는 네트워크가 없습니다.
        assert len(calls) == first
        assert all(item[1].get("crtfc_key") != "printed" for item in calls)


def test_rejected_key_does_not_echo_secret():
    def transport(url, params):
        return json.dumps({"status": "010", "message": "등록되지 않은 키입니다. " + params["crtfc_key"]}).encode()

    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        client = DartClient("super-secret", Path(tmp), min_interval=0, transport=transport)
        pct, note = client.eps_yoy("900001", date(2026, 11, 2))
        assert pct is None
        assert "super-secret" not in note
        assert "거부" in note or "unknown" in note


def test_rate_limiter_uses_injected_clock():
    slept = []
    now = {"t": 0.0}

    def clock():
        return now["t"]

    def sleep(sec):
        slept.append(sec)
        now["t"] += sec

    limiter = RateLimiter(0.25, clock=clock, sleep=sleep)
    limiter.wait()
    limiter.wait()
    assert slept == [0.25]


def test_missing_key_message_is_stable():
    assert "OPENDART_API_KEY" in MISSING_KEY_MESSAGE
    assert "unknown" in MISSING_KEY_MESSAGE
