#!/usr/bin/env python3
"""[143860] TIGER 헬스케어 ETF alert on the k203780 19:00 KST slot (approved in chat 2026-11-27, t94u; no new cron).

Same engine as scripts/005930_monitor.py (loaded below), own configs/143860.json and state/143860.json.
  lines adj10          close <= 11/27 close x 0.90 (p3), re-arm only at close >= -7% (engine opt-ins "rearm_at", line "priority")
  notice_watch         DART list.json for 셀트리온 068270, 삼성바이오로직스 207940, 알테오젠 196170 (OPENDART_API_KEY),
                       per-company title filters in configs dart_watch, dedupe by 접수번호 in st["notice_seen"], no 기재정정
    068270             연결 영업(잠정)실적 incl. 기재정정 (셀트리온 sends a rounded first estimate, then the real numbers in an
                       IR-day 기재정정, seen 2026-07 and 2026-10): period 2026-10-01~2026-12-31 -> class q4 (p4, numbers vs
                       FnGuide consensus), other period -> class q_other (p3)
    207940             단일판매ㆍ공급계약체결 -> p3
    196170             마일스톤, 라이선스, 기술이전, 기술수출 in the title -> p3
  year_guard [2026, 2027], ends_kst 2027-06-30 23:59 (engine checks both for configs that carry year_guard)
Modes: auto | force | dry | selftest (offline). Manual job=143860 is always dry (workflow guard).
Not a trading signal.
"""
from __future__ import annotations

import copy
import html
import importlib.util
import io
import json
import os
import re
import sys
import urllib.parse
import urllib.request
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path

_spec = importlib.util.spec_from_file_location("krplan_005930", Path(__file__).resolve().parent / "005930_monitor.py")
engine = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(engine)

NAME = "143860"
KST = engine.KST
DART_LIST = "https://opendart.fss.or.kr/api/list.json"
DART_DOC = "https://opendart.fss.or.kr/api/document.xml"
DART_VIEW = "https://dart.fss.or.kr/dsaf001/main.do?rcpNo={no}"
LOOKBACK_DAYS = 30


def at(d: str, h: int = 19, m: int = 0) -> datetime:
    return datetime.fromisoformat(d).replace(hour=h, minute=m, tzinfo=KST)


# ---------- DART ----------
def _key() -> str:
    k = os.environ.get("OPENDART_API_KEY", "")
    if not k:
        raise RuntimeError("OPENDART_API_KEY 없음")
    return k


def _dart_bytes(url: str, params: dict, timeout: int) -> bytes:
    """Errors never carry the URL (it holds the key): only the exception type or HTTP status."""
    try:
        return urllib.request.urlopen(url + "?" + urllib.parse.urlencode({"crtfc_key": _key(), **params}), timeout=timeout).read()
    except RuntimeError:
        raise
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code}") from None
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(type(exc).__name__) from None


def filter_dart_rows(code: str, raw: list[dict], w: dict) -> list[dict]:
    """DART list rows -> engine notice rows, only the titles configs dart_watch lets through. Titles scrubbed (ㆍ -> ', ')."""
    out = []
    for r in raw:
        nm = engine.scrub(re.sub(r"\s+", " ", str(r.get("report_nm") or "")).strip())
        if not any(engine.scrub(k) in nm for k in w["include"]) or any(engine.scrub(x) in nm for x in w.get("exclude") or []):
            continue
        d = str(r["rcept_dt"])
        out.append({"no": str(r["rcept_no"]), "code": code, "title": nm, "datetime": f"{d[:4]}-{d[4:6]}-{d[6:8]}",  # DART list has no time of day
                    "url": DART_VIEW.format(no=r["rcept_no"])})
    return out


def dart_notice_rows(cfg: dict, code: str) -> list[dict]:
    """Replaces engine.notice_rows (Naver) for this config: DART list.json per corp_code, last LOOKBACK_DAYS or since."""
    w = cfg["dart_watch"][code]
    today = datetime.now(KST).date()
    bgn = max(date.fromisoformat(cfg["notice_watch"]["since"]), today - timedelta(days=LOOKBACK_DAYS))
    if bgn > today:  # since is a future day: DART rejects bgn_de > end_de (status 100), nothing can match yet
        return []
    raw: list[dict] = []
    for pg in (1, 2, 3):
        d = json.loads(_dart_bytes(DART_LIST, {"corp_code": w["corp_code"], "bgn_de": bgn.strftime("%Y%m%d"),
                                               "end_de": today.strftime("%Y%m%d"), "page_count": "100", "page_no": str(pg)}, 20))
        if d.get("status") not in ("000", "013"):  # 013 = no data
            raise RuntimeError(f"DART status {d.get('status')}")
        raw += d.get("list") or []
        if pg >= int(d.get("total_page") or 1):
            break
    return filter_dart_rows(code, raw, w)


def dart_doc_text(rcept_no: str) -> str:
    z = zipfile.ZipFile(io.BytesIO(_dart_bytes(DART_DOC, {"rcept_no": rcept_no}, 40)))
    return " ".join(re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", z.read(n).decode("utf8", "ignore")))) for n in z.namelist())


_UNIT_EOK = {"백만원": 1 / 100, "억원": 1.0, "천원": 1 / 100000, "원": 1 / 100000000}


def _num(tok: str) -> float | None:
    t = tok.strip()
    if t in ("-", ""):
        return None
    neg = t.startswith(("△", "(", "-")) or t.endswith(")")
    v = float(re.sub(r"[^\d.]", "", t))
    return -v if neg else v


def parse_q_results(text: str) -> dict | None:
    """연결 영업(잠정)실적 text -> {start, end, rev, op, rev_prev, op_prev} in 억원 (당해실적 = single quarter), or None."""
    pm = re.search(r"당기실적\s*(\d{4}-\d{2}-\d{2})\s*~\s*(\d{4}-\d{2}-\d{2})", text)
    um = re.search(r"단위\s*[:：]\s*(백만원|억원|천원|원)", text)
    if not pm or not um:
        return None
    f = _UNIT_EOK[um.group(1)]
    out = {"start": pm.group(1), "end": pm.group(2)}
    for key, label in (("rev", "매출액"), ("op", "영업이익")):
        m = re.search(label + r"\s+당해실적\s+(.*?)\s+누계실적", text)
        if not m:
            return None
        seg = m.group(1).split()
        cur = _num(seg[0]) if seg else None
        if cur is None:
            return None
        prev = _num(seg[1]) if len(seg) > 1 else None
        out[key] = cur * f
        out[key + "_prev"] = prev * f if prev is not None else None
    return out


def q4_detail_from_text(text: str, cfg: dict, f_title: str = "") -> dict:
    """Engine NOTICE_DETAIL result for a 셀트리온 연결 잠정실적 filing."""
    q = cfg["q4"]
    cons_r, cons_o = q["consensus_revenue_eok"], q["consensus_op_eok"]
    res = parse_q_results(text)
    if res is None:
        return {"status": f"숫자를 원문에서 자동으로 못 읽었습니다. 원문의 연결 매출액과 영업이익 당해실적을 컨센서스(매출 {cons_r:,.0f}억, 영업이익 {cons_o:,.0f}억, FnGuide {q['consensus_as_of']} 기준)와 직접 비교하세요."}
    if (res["start"], res["end"]) != (q["period_start"], q["period_end"]):
        return {"class": "q_other", "status": f"실적기간 {res['start']}~{res['end']}, 영업이익 {res['op']:,.0f}억, 매출 {res['rev']:,.0f}억."}
    rev, op = res["rev"], res["op"]
    corr = "정정" in f_title
    d_r, d_o = (rev / cons_r - 1) * 100, (op / cons_o - 1) * 100
    mg, cmg = op / rev * 100, cons_o / cons_r * 100
    pm = (f", 전기 {res['op_prev'] / res['rev_prev'] * 100:.1f}%" if res.get("op_prev") is not None and res.get("rev_prev") else "")
    return {
        "tail": f"영업이익 {op:,.0f}억 (컨센서스 대비 {d_o:+.1f}%{', 정정' if corr else ''})",
        "status": (("정정 공시(확정 수치). " if corr else "첫 공시라 반올림 추정치일 수 있고, 2026년 2~3분기처럼 이후 기재정정 공시에서 확정 수치로 바뀔 수 있습니다. ")
                   + f"4Q26 연결 매출 {rev:,.0f}억(컨센서스 {cons_r:,.0f}억 대비 {d_r:+.1f}%), 영업이익 {op:,.0f}억(컨센서스 {cons_o:,.0f}억 대비 {d_o:+.1f}%), "
                   f"영업이익률 {mg:.1f}%(컨센서스 {cmg:.1f}%{pm}). 컨센서스는 FnGuide {q['consensus_as_of']} 기준 설정값이라 발표 직전 값과 다를 수 있습니다."),
    }


def notice_detail(cfg: dict, f: dict, cls: dict) -> dict:
    """Engine NOTICE_DETAIL hook. Failure -> manual-check text, class unchanged."""
    if cls.get("detail") != "dart_q4":
        return {}
    try:
        return q4_detail_from_text(dart_doc_text(f["no"]), cfg, f.get("title", ""))
    except Exception as exc:  # noqa: BLE001
        print(f"notice_detail failed: {type(exc).__name__} {str(exc)[:40]}", file=sys.stderr)
        q = cfg["q4"]
        return {"status": f"숫자를 자동으로 가져오지 못했습니다. 원문의 연결 매출액과 영업이익 당해실적을 컨센서스(매출 {q['consensus_revenue_eok']:,.0f}억, 영업이익 {q['consensus_op_eok']:,.0f}억)와 직접 비교하세요."}


def run(argv: list[str]) -> int:
    mode = (argv[1] if len(argv) > 1 else "auto").lower()
    if mode == "selftest":
        return selftest()
    engine.notice_rows = dart_notice_rows
    engine.NOTICE_DETAIL = notice_detail
    return engine.run(NAME, argv)


# ---------- offline self-test ----------
# Real DART layout, 셀트리온 2026-02-05 4Q25 filing (rcept 20260205800031), cut down. Pins the table parser.
REAL_Q = ("셀트리온/연결재무제표기준영업(잠정)실적(공정공시)/(2026.02.05)연결재무제표기준영업(잠정)실적(공정공시) 연결재무제표 기준 영업(잠정)실적(공정공시) "
          "실적기간 당기실적 2025-10-01 ~ 2025-12-31 전기실적 2025-07-01 ~ 2025-09-30 전년동기실적 2024-10-01 ~ 2024-12-31 "
          "1. 연결실적내용 단위 : 백만원, % 구분 당기실적 전기실적 전기대비 전년동기실적 전년동기대비 (25.4Q) (25.3Q) 증감율(%) 흑자적자전환여부 (24.4Q) 증감율(%) 흑자적자전환여부 "
          "매출액 당해실적 1,330,157 1,028,969 29.27 - 1,063,649 25.06 - 누계실적 4,162,495 - - - 3,557,304 17.01 - "
          "영업이익 당해실적 475,172 301,412 57.65 - 196,404 141.94 - 누계실적 1,168,471 - - - 492,016 137.49 -")
# Real DART layout, 셀트리온 2026-10-27 기재정정 (rcept 20261027800047), cut down: 정정전/정정후 table, then the final table in 억원.
REAL_CORR = ("셀트리온/연결재무제표기준영업(잠정)실적(공정공시)/(2026.10.27) 정정신고(보고) 3. 정정사유 2026년 3분기 연결재무제표 기준 영업(잠정)실적(공정공시) 내용 정정 "
             "4. 정정사항 정정항목 정정전 정정후 1. 연결실적내용 - - 당기실적(26.3Q) - - - 매출액(당해실적) 15,000 15,755 - 매출액(누계실적) 40,387 41,142 - 영업이익(당해실적) 5,100 5,253 "
             "연결재무제표 기준 영업(잠정)실적(공정공시) 실적기간 당기실적 2026-07-01 ~ 2026-09-30 전기실적 2026-04-01 ~ 2026-06-30 전년동기실적 2025-07-01 ~ 2025-09-30 "
             "1. 연결실적내용 단위 : 억원, % 구분 당기실적 전기실적 전기대비 전년동기실적 전년동기대비 (26.3Q) (26.2Q) 증감율(%) 흑자적자전환여부 (25.3Q) 증감율(%) 흑자적자전환여부 "
             "매출액 당해실적 15,755 13,937 13.04 - 10,290 53.11 - 누계실적 41,142 - - - 28,323 45.26 - 영업이익 당해실적 5,253 4,518 16.27 - 3,014 74.29 - 누계실적 12,989 - - - 6,933 87.35 -")
Q4_DOC = REAL_Q.replace("2025-10-01 ~ 2025-12-31", "2026-10-01 ~ 2026-12-31").replace("1,330,157", "1,700,000").replace("475,172", "640,000").replace("1,028,969", "1,575,548").replace("301,412", "525,283")


def selftest() -> int:
    from ntfy_util import ends_skip_reason, route_topic

    cfg = engine.load_cfg(NAME)
    ok = True
    pfx = cfg["title_prefix"]

    def check(label, got, want):
        nonlocal ok
        good = got == want
        ok &= good
        print(f"{'ok  ' if good else 'FAIL'} {label}: got={got} want={want}")

    ln = cfg["lines"][0]
    L, R, REF = ln["level"], ln["rearm_at"], cfg["ref_close"]["close"]
    check("config: line adj10 le, p3, level = round(ref x 0.90), re-arm -7%", (ln["key"], ln["op"], ln["priority"], L, R), ("adj10", "le", 3, round(REF * 0.90), round(REF * 0.93)))
    check("config: prefix, year_guard, ends_kst", (pfx, cfg["year_guard"], cfg["ends_kst"]), ("[143860]", [2026, 2027], "2027-06-30 23:59"))
    none_ref = lambda c, rows, now: f"{c['short']} NXT 정보 없음"  # noqa: E731
    no_notice = lambda c, code: []  # noqa: E731

    def sends(st, data, now, notice_fn=no_notice, fails=None):
        return engine.plan_sends(cfg, st, data, list(fails or []), now, lambda c, d: None, none_ref, notice_fn)

    def rows(*pairs):
        return [{"day": d, "close": float(c)} for d, c in pairs]

    seed = engine.default_state(cfg)
    seed.update(seeded=True, last_day=cfg["ref_close"]["day"], last_close=float(REF))
    seed["notice_done"] = {}
    # ---- price line
    st = copy.deepcopy(seed)
    days = iter(f"2026-12-{i:02d}" for i in range(1, 31))

    def feed(close):
        d = next(days)
        return sorted(e["key"] for e in engine.eval_lines(cfg, st, [{"day": d, "close": float(close)}], d, at(d)))
    check("seed: armed, last close = 11/27 ref", (seed["lines"]["adj10"]["armed"], seed["last_close"]), (True, float(REF)))
    check("level+1 silent", feed(L + 1), [])
    check("level fires (le)", feed(L), ["adj10"])
    check("lower again: no repeat", feed(L - 500), [])
    check("rearm-1 stays disarmed", (feed(R - 1), st["lines"]["adj10"]["armed"]), ([], False))
    check("rearm_at re-arms (silent)", (feed(R), st["lines"]["adj10"]["armed"]), ([], True))
    check("fires again after re-arm", feed(L - 1), ["adj10"])
    st = copy.deepcopy(seed)
    s = sends(st, {"rows": rows(("2026-12-01", L - 10)), "src": "Yahoo 정규장"}, at("2026-12-01"))
    t, m, p, tags = s[0]
    check("price send: title, p3", (t, p), (f"{pfx} 종가 {L:,}원 이하 | {ln['title']}", 3))
    check("price send: required wording", all(k in m for k in ("이익 전망 유지 확인 시 지정가로 소액 분할 검토, 시장가 금지", f"{REF:,}원 대비 -10%")), True)
    check("price send: 143860 and related tickers in title", all(x in t for x in ("143860", "068270", "207940", "196170")), True)
    check("price send: stays on cwally-port", route_topic("cwally-port", t), "cwally-port")
    check("price send: second run same day silent", sends(st, {"rows": rows(("2026-12-01", L - 10)), "src": "Yahoo 정규장"}, at("2026-12-01", 22)), [])
    # ---- DART row filter (real titles from the 2026 lists)
    dw = cfg["dart_watch"]
    raw_c = [{"rcept_no": "20261102800302", "rcept_dt": "20261102", "report_nm": "투자판단관련주요경영사항              (CTP13 SC(짐펜트라) 유럽 임상 3상)"},
             {"rcept_no": "20261027800047", "rcept_dt": "20261027", "report_nm": "[기재정정]연결재무제표기준영업(잠정)실적(공정공시)"},
             {"rcept_no": "20260205800031", "rcept_dt": "20260205", "report_nm": "연결재무제표기준영업(잠정)실적(공정공시)"},
             {"rcept_no": "20260101800001", "rcept_dt": "20260101", "report_nm": "연결재무제표기준영업실적등에대한전망(공정공시)"},
             {"rcept_no": "20260317800019", "rcept_dt": "20260317", "report_nm": "단일판매ㆍ공급계약체결"}]
    check("filter 068270: 연결 잠정실적 and its 기재정정 (real numbers come in the correction), no 전망, no other", [r["no"] for r in filter_dart_rows("068270", raw_c, dw["068270"])], ["20261027800047", "20260205800031"])
    raw_b = [{"rcept_no": "20261103800043", "rcept_dt": "20261103", "report_nm": "단일판매ㆍ공급계약체결"},
             {"rcept_no": "20261102800004", "rcept_dt": "20261102", "report_nm": "[기재정정]단일판매ㆍ공급계약체결"},
             {"rcept_no": "20261002800830", "rcept_dt": "20261002", "report_nm": "단일판매ㆍ공급계약체결(자율공시)"},
             {"rcept_no": "20261021800344", "rcept_dt": "20261021", "report_nm": "연결재무제표기준영업(잠정)실적(공정공시)"}]
    got = filter_dart_rows("207940", raw_b, dw["207940"])
    check("filter 207940: 단일판매 incl 자율공시, no 정정, no 잠정실적", [r["no"] for r in got], ["20261103800043", "20261002800830"])
    check("filter: title scrubbed (no ㆍ), link to DART viewer", (any(ch in got[0]["title"] for ch in engine.FORBIDDEN), got[0]["url"]),
          (False, "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20261103800043"))
    raw_a = [{"rcept_no": "20261126900206", "rcept_dt": "20261126", "report_nm": "투자판단관련주요경영사항              (ALTB4 라이선스 계약 파트너사 MSD로부터 판매 마일스톤 수령 예정)"},
             {"rcept_no": "20260515900004", "rcept_dt": "20260515", "report_nm": "투자판단관련주요경영사항              (아이젠피주(ALT-L9, 아일리아 바이오시밀러) 식약처 품목허가 승인)"},
             {"rcept_no": "20261103900196", "rcept_dt": "20261103", "report_nm": "연결재무제표기준영업(잠정)실적(공정공시)"}]
    check("filter 196170: 마일스톤 yes, 품목허가 no, 잠정실적 no", [r["no"] for r in filter_dart_rows("196170", raw_a, dw["196170"])], ["20261126900206"])
    # ---- parser
    res = parse_q_results(REAL_Q)
    check("parse real 4Q25 layout (백만원 -> 억원)", (res["start"], res["end"], round(res["rev"], 2), round(res["op"], 2), round(res["op_prev"], 2)),
          ("2025-10-01", "2025-12-31", 13301.57, 4751.72, 3014.12))
    check("parse real 3Q26 기재정정 doc (정정전/정정후 table first, 억원 final table)", (lambda r: (r["start"], r["end"], r["rev"], r["op"], r["rev_prev"], r["op_prev"]))(parse_q_results(REAL_CORR)),
          ("2026-07-01", "2026-09-30", 15755.0, 5253.0, 13937.0, 4518.0))
    check("parse: unparsable -> None", parse_q_results("형식이 다른 문서"), None)
    check("parse: 단위 억원 and 음수(△)", (lambda r: (r["op"], r["rev"]))(parse_q_results("당기실적 2026-10-01 ~ 2026-12-31 단위 : 억원 매출액 당해실적 100 90 11.1 - 80 25.0 - 누계실적 1 영업이익 당해실적 △5 3 적자전환 - 4 - - 누계실적 1")), (-5.0, 100.0))
    # ---- disclosure classes through plan_sends
    feed_map: dict = {}
    nf = lambda c, code: list(feed_map.get(code, []))  # noqa: E731
    docs = {"C1": Q4_DOC, "C2": REAL_Q, "C3": "형식이 다른 문서", "C4": Q4_DOC.replace("1,700,000", "1,716,000").replace("640,000", "653,000")}
    saved = engine.NOTICE_DETAIL
    engine.NOTICE_DETAIL = lambda c, f, cl: q4_detail_from_text(docs.get(f["no"], ""), cfg, f.get("title", "")) if cl.get("detail") == "dart_q4" else {}
    base = {"rows": rows(("2026-12-01", REF)), "src": "Yahoo 정규장"}
    try:
        def one(code, no, title):
            feed_map.clear()
            feed_map[code] = [{"no": no, "code": code, "title": title, "datetime": "2027-02-05", "url": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={no}"}]
            st2 = copy.deepcopy(seed)
            st2["last_day"] = "2026-12-01"
            return sends(st2, base, at("2027-02-05"), notice_fn=nf), st2
        s1, st1 = one("068270", "C1", "연결재무제표기준영업(잠정)실적(공정공시)")
        t, m, p, _ = s1[0]
        exp_op = 6400.0
        check("셀트리온 4Q26: title carries ticker, 영업이익 and consensus deviation, p4", (t, p),
              (f"{pfx} 셀트리온 068270 4분기 잠정실적 | 영업이익 6,400억 (컨센서스 대비 {(6400 / 6201.25 - 1) * 100:+.1f}%)", 4))
        check("셀트리온 4Q26: body has revenue and 영업이익 vs consensus, 조건 1, 지정가 wording", all(k in m for k in (
            "매출 17,000억(컨센서스 16,826억 대비 +1.0%)", "영업이익 6,400억(컨센서스 6,201억 대비 +3.2%)", "영업이익률 37.6%(컨센서스 36.9%, 전기 33.3%)",
            "판단 변경 조건 1을 점검하세요", "지정가로 소액 분할 진입 검토, 시장가 금지", "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=C1")), True)
        check("셀트리온 4Q26 first filing says 반올림 추정치 may change", ("첫 공시라 반올림 추정치일 수 있고" in m, "정정" in t), (True, False))
        check("셀트리온: seen = rcept no, repeat silent", (st1["notice_seen"], sends(st1, base, at("2027-02-05", 23), notice_fn=nf)), (["C1"], []))
        s1c, _ = one("068270", "C4", "[기재정정]연결재무제표기준영업(잠정)실적(공정공시)")
        check("셀트리온 4Q26 기재정정 alerts again with final numbers (tail marks 정정)", (s1c[0][0].endswith(", 정정)"), "정정 공시(확정 수치)" in s1c[0][1], "영업이익 6,530억" in s1c[0][1], s1c[0][2]), (True, True, True, 4))
        s2, _ = one("068270", "C2", "연결재무제표기준영업(잠정)실적(공정공시)")
        check("셀트리온 other quarter (4Q25 doc) -> p3 q_other", (s2[0][0].startswith(f"{pfx} 셀트리온 068270 잠정실적(4분기 아님) | "), s2[0][2]), (True, 3))
        s3, _ = one("068270", "C3", "연결재무제표기준영업(잠정)실적(공정공시)")
        check("셀트리온 unparsable doc -> still p4 with manual comparison text", (s3[0][2], "직접 비교하세요" in s3[0][1], s3[0][0].startswith(f"{pfx} 셀트리온 068270 4분기 잠정실적 | ")), (4, True, True))
        s4, _ = one("207940", "B1", "단일판매, 공급계약체결")
        check("삼성바이오로직스 단일판매 -> p3, ticker in title", (s4[0][0].startswith(f"{pfx} 삼성바이오로직스 207940 단일판매 공급계약 | "), s4[0][2]), (True, 3))
        s5, _ = one("196170", "A1", "투자판단관련주요경영사항 (ALTB4 라이선스 계약 파트너사 MSD로부터 판매 마일스톤 수령 예정)")
        check("알테오젠 마일스톤 -> p3, ticker in title", (s5[0][0].startswith(f"{pfx} 알테오젠 196170 마일스톤 기술이전 공시 | "), s5[0][2]), (True, 3))
        allm = s1 + s2 + s3 + s4 + s5 + s[:1]
        check("every title: 143860 prefix, one related ticker (or 143860 line carries all three)", all(t.startswith(pfx + " ") and any(x in t for x in ("068270", "207940", "196170")) for t, *_ in allm), True)
        check("no forbidden chars in any title or body", any(ch in t + m for t, m, *_ in allm for ch in engine.FORBIDDEN), False)
        order = ["이게 뭐 알림:", "지금 결과/상태:", "지금 할 일:", "나중에:", "익절/줄일 때:", "하지 말 것:"]
        check("six-item body order", [t for t, m, *_ in allm if [x.split(":")[0] + ":" for x in m.split("\n")[1:7]] != order], [])
        check("all routed to cwally-port", [t for t, *_ in allm if route_topic("cwally-port", t) != "cwally-port"], [])
        # since: older filings never announced
        feed_map.clear()
        feed_map["207940"] = [{"no": "B0", "code": "207940", "title": "단일판매, 공급계약체결", "datetime": "2026-11-03", "url": "x"}]
        st3 = copy.deepcopy(seed)
        check("filing before since (11/28) silent", sends(st3, base, at("2026-12-01"), notice_fn=nf), [])
        # feed down -> one ops fail notice, nothing marked seen
        def boom(c, code):
            raise RuntimeError("HTTP 500")
        st4 = copy.deepcopy(seed)
        s6 = sends(st4, base, at("2026-12-01"), notice_fn=boom)
        check("DART down: one ops fail notice naming DART, nothing seen", ([route_topic("cwally-port", x[0]) for x in s6][:1], "DART" in s6[0][1], st4["notice_seen"]), (["cwally-ops"], True, []))
    finally:
        engine.NOTICE_DETAIL = saved
    # ---- expiry and year guard (mirrors engine.run and ntfy_util)
    def open_at(when):
        stamp = when.strftime("%Y-%m-%d %H:%M KST")
        ends = cfg["ends_kst"]
        return when.year in cfg["year_guard"] and not (ends and stamp[:len(ends)] > ends)
    for d, want in (("2026-11-27", True), ("2027-06-30", True), ("2027-07-01", False), ("2028-01-03", False)):
        check(f"open at {d} 19:00 (engine guard)", open_at(at(d)), want)
        check(f"ntfy ends_skip_reason none at {d} 19:00", ends_skip_reason("cwally-port", f"{pfx} x", config_name=NAME, config=cfg, now=at(d)) is None, want)
    print("SAMPLE:", t)
    print("SAMPLE BODY:", m.replace("\n", " / "))
    print("SELFTEST", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(run(sys.argv))
