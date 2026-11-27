#!/usr/bin/env python3
"""[0220WL] 한화머시너리앤서비스홀딩스 3우B close-based alert on the k203780 19:00 KST slot (approved in chat 2026-10-28).

Same engine as scripts/005930_monitor.py (loaded below), own configs/0220wl_close.json and state/0220wl_close.json.
Named 0220wl_close (not 0220WL) so it cannot collide with the older 15:20 alert scripts/0220wl_monitor.py /
configs/0220wl.json on case-insensitive filesystems. Same [0220WL] title prefix.

Uses the optional engine keys added 2026-10-28 (absent for 005930/000660/064400, whose selftests are unchanged):
  lines + ladder_all/lines_what/lines_trim/lines_dont/yield/suppress  pref close <= 2,040 (yield 5%) and < 2,000 (재점검)
  pair + ratio_lines   same-session discount (0220W0 - 0220WL) / 0220W0: < 65.0% 괴리 축소, > 75.0% 괴리 확대 (2026-11-04; was <= 60 / >= 75)
  notice_events        10/29 한화비전 3Q filing; 11/2~11/4 tender offer result (disabled 2026-11-04, enabled=false)
  notice_watch         0220W0/0220WL filings with 배당/소각/자기주식/주주환원 in the title, one alert per filing
Added 2026-10-29 (chat approval; the 15:20 0220wl alert retired from the workflow):
  footer_note          holding note line appended to every message
  time_checks          6개월 상대성과 점검 (end 2027-01-09), 12개월 인컴 유지 점검 (end 2027-07-10): dated reminders,
                       no filing lookup, once on/after the end day (catch-up 3 days), dedupe st["time_done"]
Added 2026-11-27 (chat approval, 0220WL 관망 analysis check; no new cron):
  lines buy1           close <= 2,000 (p4, 매수 상향 조건 1, not a buy order), re-arm only at close >= 2,100 (engine opt-in "rearm_at");
                       suppresses review and yield5 so one message goes out
  notice_watch classes first matching class sets title/priority/text: 자회사 (p3), 유상증자 (p4 warning), 무배당 (p4 warning),
                       배당결정 (p4, 3우B DPS read from DART document.xml by dart_dividend_detail below), 소각/자기주식 (p3)
  year_guard, ends_kst [2026, 2027], 2027-07-13 23:59 (engine checks both only for configs that carry year_guard;
                       2027-07-10 12개월 점검은 토요일이라 실제 발송은 07-12 또는 07-13 catch-up, 만료를 그날까지 연장)
Modes: auto | force | dry | selftest (own offline checks, below) | install (one install-confirmation send, no state)
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
import urllib.request
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

_spec = importlib.util.spec_from_file_location("krplan_005930", Path(__file__).resolve().parent / "005930_monitor.py")
engine = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(engine)

NAME = "0220wl_close"
KST = engine.KST
DART_CORP = "02042949"  # 한화머시너리앤서비스홀딩스 (DART corp_code)
DPS_FULL = 102  # 3우B 최저 우선배당 연 102원 (액면 1,000원의 10.2%)
DPS_PRORATA = round(DPS_FULL * 153 / 365, 2)  # 2026년 배당기산일 8/1 일할: 42.76원
DART_BASE = "https://opendart.fss.or.kr/api"


def parse_pref3_dps(text: str) -> tuple[int | None, str]:
    """3우B (제3우선주) 1주당 배당금 from a DART 현금ㆍ현물배당결정 document text. (value, how); value None = not found."""
    m = re.search(r"제3우선주\s*\([^)]*\)\s*우선주\s+(-|[\d,]+)", text)
    how = "제3우선주 표"
    if not m:
        m = re.search(r"1주당 배당금\(원\)\s*보통주식\s*(?:-|[\d,]+)\s*종류주식\s*(-|[\d,]+)", text)
        how = "종류주식 칸"
    if not m:
        return None, ""
    v = m.group(1)
    return (0 if v == "-" else int(v.replace(",", ""))), how


def dividend_detail_from_text(text: str) -> dict:
    """Engine NOTICE_DETAIL result for a dividend decision text: status, title tail, class switch to 무배당 when DPS is 0."""
    dps, how = parse_pref3_dps(text)
    if dps is None:
        return {"status": "3우B 1주당 배당금을 원문에서 자동으로 못 읽었습니다. DART 원문의 제3우선주 1주당 배당금을 직접 확인하세요."}
    kind = (re.search(r"배당구분\s*(\S+)", text) or [None, "구분 미확인"])[1]
    base = (re.search(r"배당기준일\s*(\d{4}-\d{2}-\d{2})", text) or [None, "미확인"])[1]
    pay = (re.search(r"배당금지급 예정일자\s*(\d{4}-\d{2}-\d{2})", text) or [None, "미정"])[1]
    meta = f"{kind}, 기준일 {base}, 지급 예정 {pay}, 출처 {how}"
    if dps == 0:
        return {"class": "nodiv", "tail": "3우B 배당금 0원", "status": f"3우B 1주당 배당금 0원({meta})."}
    if dps >= DPS_FULL:
        verdict = "연 102원 이상 확인, 매수 상향 조건 충족. 가격 조건(종가 2,000원 이하)은 따로 봅니다."
        tag = "연 102원 이상 확인, 매수 상향 조건 충족"
    elif dps >= int(DPS_PRORATA):
        verdict = (f"2026 일할 배당 정상 이행, 2027년 연 102원 지급 확인 전까지 2,000원 이하 소량 분할만. "
                   f"2026년은 8/1 기산 일할 약 {DPS_PRORATA}원 수준입니다.")
        tag = "2026 일할 배당 정상 이행, 2027년 연 102원 지급 확인 전까지 2,000원 이하 소량 분할만"
    else:
        verdict = f"2026년 일할 약 {DPS_PRORATA}원에도 못 미칩니다. 누적적이라 부족분이 이월되는지 원문에서 확인하세요."
        tag = "일할 미달"
    if kind != "결산배당":
        verdict += " 결산배당이 아니면 한 해 합계가 아닙니다."
    return {"tail": f"3우B {dps:,}원 ({tag})", "status": f"3우B 1주당 배당금 {dps:,}원({meta}). {verdict}"}


def pick_dividend_filing(rows: list[dict]) -> dict | None:
    """Latest 현금ㆍ현물배당결정 (기재정정 allowed) from a DART list; 주주명부폐쇄(기준일)결정 and the like are not it."""
    hits = sorted((r for r in rows if "배당결정" in r["report_nm"].replace(" ", "")), key=lambda r: r["rcept_no"])
    return hits[-1] if hits else None


def dart_dividend_detail(cfg: dict, f: dict, cls: dict) -> dict:
    """NOTICE_DETAIL hook: look up the filing on DART (OPENDART_API_KEY), read the 3우B DPS. Any failure -> manual-check text.

    Error text never includes the request URL (it carries the key)."""
    fallback = {"status": "3우B 1주당 배당금은 자동 확인에 실패했습니다. DART 원문의 제3우선주 1주당 배당금을 직접 확인하세요."}
    key = os.environ.get("OPENDART_API_KEY", "")
    if not key:
        return {"status": "3우B 1주당 배당금은 DART 키가 없어 자동 확인을 못 했습니다. DART 원문의 제3우선주 1주당 배당금을 직접 확인하세요."}
    try:
        day = datetime.fromisoformat(f["datetime"][:10])
        lst = json.loads(urllib.request.urlopen(
            f"{DART_BASE}/list.json?crtfc_key={key}&corp_code={DART_CORP}&bgn_de={(day - timedelta(days=3)):%Y%m%d}"
            f"&end_de={(day + timedelta(days=1)):%Y%m%d}&page_count=100", timeout=20).read())
        hit = pick_dividend_filing(lst.get("list") or [])
        if not hit:
            return fallback
        raw = urllib.request.urlopen(f"{DART_BASE}/document.xml?crtfc_key={key}&rcept_no={hit['rcept_no']}", timeout=40).read()
        z = zipfile.ZipFile(io.BytesIO(raw))
        text = " ".join(re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", z.read(n).decode("utf8", "ignore")))) for n in z.namelist())
        return dividend_detail_from_text(text)
    except Exception as exc:  # noqa: BLE001
        print(f"dart_dividend_detail failed: {type(exc).__name__}", file=sys.stderr)
        return fallback


def at(d: str, h: int = 19, m: int = 0) -> datetime:
    return datetime.fromisoformat(d).replace(hour=h, minute=m, tzinfo=KST)


def install_msg(cfg: dict, st: dict, data: dict, fails: list[str], stamp: str) -> tuple[str, str]:
    fmt = engine.fmt
    pl = engine.pair_last(data)
    if pl:
        d, m, p, v = pl
        px = f"최근 KRX 종가({d}) 우선주 {fmt(m)}, 보통주 {fmt(p)}, 괴리율 {v:.2f}%, 최저 배당수익률 {cfg['yield']['dps'] / m * 100:.2f}%."
    else:
        px = f"시드: {st['last_day']} 우선주 {fmt(st['last_close'])}, 괴리율 {st['ratio']['last']:.2f}%."
    title = f"{cfg['title_prefix']} 설치 확인 | 매매 신호 아님"
    msg = "\n".join([
        engine.head(cfg, stamp),
        "이게 뭐 알림: 한화머시너리앤서비스홀딩스 3우B 종가 기준 알림 설치 확인입니다. KRX 거래일 19:00에 조건을 새로 넘을 때만 옵니다. 15:20 기존 [0220WL] 알림과는 별개입니다.",
        f"지금 결과/상태: {px} 판단은 Yahoo KRX 정규장 종가만 씁니다(NXT 가격은 참고 한 줄만). 종가가 없으면 판단 보류, 운영 알림 하루 1회."
        + (" 조회 실패: " + ", ".join(fails) + "." if fails else ""),
        "지금 할 일: 없음. 선: 1) 종가 2,040원 이하 = 최저 배당수익률 5% 돌파, 공격안 검토(최저 배당 연 102원, 액면 1,000원의 10.2%, 누적적 비참가적, 수익률 = 102 / 종가). "
        "2) 종가 2,000원 미만 = 재점검 신호(추가 매수 중단선, 손절 지시 아님). 선마다 1회, 종가가 1% 넘게 반대로 돌아와야 재무장. "
        "3) 종가 2,000원 이하 = 매수 상향 조건 1(가격 조건, 배당 102원 기준 수익률 5.10% 이상). 2027년 연 102원 지급 확인 전까지 소량 분할만, 투자 논리 재점검. 2,000원 이하가 되면 이 선 하나만 오고, 종가 2,100원 이상으로 올라가야 재무장. "
        "공시: 결산배당 결정(3우B 주당 배당금 보고), 유상증자 또는 무배당(경고), 소각 또는 자기주식(참고).",
        "나중에: 괴리율 = (보통주 종가 - 우선주 종가) / 보통주 종가, 같은 날 KRX 종가 둘 다 있을 때만 계산. 60.0% 이하 할인 해소, 75.0% 이상 괴리 확대(11/2 공개매수 종료 뒤 보통주 하락 감시), 1%p 넘게 반대로 와야 재무장. "
        "이벤트: 10/29 한화비전 3분기 잠정실적(비전 실적이 NAV 최대 자산 6,600억대에 주는 영향, 주가 반응, 괴리율 변화), 11/2~11/4 공개매수 결과(응모율과 안분 비율, 보통주 가격 반응, 괴리율, 11/4 결제. 결과보고서는 개인 공개매수자가 DART에 내는 서류라 회사 공시 목록에 안 뜨면 11/4에 공시 확인 필요로 옴), "
        "배당, 소각, 자기주식, 주주환원 공시는 건마다 1회. 공시가 늦으면 3일 안에서 따라잡음.",
        "익절/줄일 때: 괴리율 60% 이하 할인 해소 알림 때 비중 점검.",
        "하지 말 것: 이 설치 확인을 매매 신호로 읽기. 미확인: 잔여재산 분배 순위. 첫해 배당은 일할 계산(2026년 약 42.76원 수준)이 3분기보고서로 확인됨.",
    ])
    msg = engine.with_footer(cfg, msg)
    engine.clean(title, msg)
    return title, msg


def run(argv: list[str]) -> int:
    mode = (argv[1] if len(argv) > 1 else "auto").lower()
    if mode == "selftest":
        return selftest()
    if mode == "install":
        cfg = engine.load_cfg(NAME)
        now = datetime.now(KST)
        stamp = now.strftime("%Y-%m-%d %H:%M KST")
        st = engine.load_state(cfg)
        data, fails = engine.collect(cfg, now)
        title, msg = install_msg(cfg, st, data, fails, stamp)
        print(title)
        print(msg)
        engine.publish(engine.resolve_topic("port", cfg.get("ntfy_topic")), title, msg, 3, ["white_check_mark"], engine.resolve_server(cfg.get("ntfy_server")))
        return 0
    engine.NOTICE_DETAIL = dart_dividend_detail
    return engine.run(NAME, argv)


# ---------- offline self-test ----------
DIV_DOC = ("현금ㆍ현물배당 결정 1. 배당구분 {kind} 2. 배당종류 현금배당 3. 1주당 배당금(원) 보통주식 {dps} 종류주식 {dps} 4. 시가배당율(%) 보통주식 1.0 종류주식 - "
           "5. 배당금총액(원) 1 6. 배당기준일 2026-12-31 7. 배당금지급 예정일자 - 8. 주주총회 개최여부 개최 "
           "【종류주식에 대한 배당 관련 사항】 종류주식명 종류주식구분 1주당 배당금(원) 시가배당율(%) 배당금총액(원) 제3우선주 (한화머시너리앤서비스홀딩스3우B) 우선주 {dps} 2.3 1")
# real DART layout, Hanwha Corp 2026-02-25 (rcept 20260225801273), used only to pin the table parser
REAL_HANWHA_SNIPPET = ("종류주식명 종류주식구분 1주당 배당금(원) 시가배당율(%) 배당금총액(원) 제1우선주 (한화우) 우선주 1,150 - 228,887,950 "
                       "제3우선주 (한화3우B) 우선주 1,150 2.3 22,315,107,150")


def selftest() -> int:
    from ntfy_util import route_topic

    cfg = engine.load_cfg(NAME)
    ok = True
    pfx = cfg["title_prefix"]

    def check(label, got, want):
        nonlocal ok
        good = got == want
        ok &= good
        print(f"{'ok  ' if good else 'FAIL'} {label}: got={got} want={want}")

    none_ref = lambda c, rows, now: f"{c['short']} NXT 정보 없음"  # noqa: E731
    no_notice = lambda c, code: []  # noqa: E731

    def sends(st, data, now, fails=None, notice_fn=no_notice):
        return engine.plan_sends(cfg, st, data, list(fails or []), now, lambda c, d: None, none_ref, notice_fn)

    def rows(*pairs):
        return [{"day": d, "close": float(c)} for d, c in pairs]

    # The committed-state check (seeded, cursors set) was dropped 2026-11-02 (selftest audit): the selftest no longer
    # reads state/0220wl_close.json at all.
    # The checks below are written against the config seed day (2026-10-27). The committed state moves on with every
    # 19:00 run, so use a fresh state pinned to the config seed (same fix as the engine selftest, 2026-10-26).
    seed = engine.default_state(cfg)
    seed.update(seeded=True, last_day=cfg["seed"]["day"], last_close=float(cfg["seed"]["close"]))
    seed["ratio"].update(last_day=cfg["seed"]["day"], last=round(engine.discount(cfg["seed"]["close"], cfg["seed"]["pair_close"]), 4))
    check("seed: last_day 10-27, all armed", (seed["last_day"], all(v["armed"] for v in seed["lines"].values()),
                                             all(v["armed"] for v in seed["ratio"]["lines"].values())), ("2026-10-27", True, True))
    check("seed discount 71.43%", round(engine.discount(2200, 7700), 2), 71.43)
    st = copy.deepcopy(seed)
    check("seed closes re-run silent", sends(st, {"rows": rows(("2026-10-27", 2200)), "pair_rows": rows(("2026-10-27", 7700))}, at("2026-10-27")), [])
    st = copy.deepcopy(seed)
    t = sends(st, {"rows": rows(("2026-10-27", 2200), ("2026-10-28", 2230)), "pair_rows": rows(("2026-10-27", 7700), ("2026-10-28", 7790))}, at("2026-10-28"))
    check("10-28 judged (2,230 / 7,790 = 71.37%): silent, cursors move", (t, st["last_day"], st["ratio"]["last_day"]), ([], "2026-10-28", "2026-10-28"))

    # ---- price lines
    st = copy.deepcopy(seed)
    days = iter(f"2026-11-{i:02d}" for i in range(2, 31))

    def feed(close, pair=None):
        d = next(days)
        r = [{"day": d, "close": float(close)}]
        ev = engine.eval_lines(cfg, st, r, d, at(d))
        if pair:
            ev += [e for e in engine.eval_ratio(cfg, st, r, [{"day": d, "close": float(pair)}], d, at(d)) if e["kind"] != "pair_line"]
        return sorted(e["key"] for e in ev)
    check("2,041 silent", feed(2041), [])
    check("2,040 yield5 fires (le)", feed(2040), ["yield5"])
    check("2,030 no repeat", feed(2030), [])
    check("2,060 (< 1% above) no re-arm", feed(2060), [])
    check("2,061 re-arms (silent)", feed(2061), [])
    check("2,040 fires again", feed(2040), ["yield5"])
    check("2,001 silent", feed(2001), [])
    check("2,000 buy1 fires (le), review silent (strict <)", feed(2000), ["buy1"])
    check("1,999 review fires (deeper line)", feed(1999), ["review"])
    check("2,020 no re-arm (review needs > 2,020)", feed(2020), [])
    check("1,990 silent while disarmed", feed(1990), [])
    check("2,021 re-arms review, then 1,995 fires", (feed(2021), feed(1995))[1], ["review"])
    check("buy1 rearm_at 2,100: 2,099 stays disarmed", (feed(2099), st["lines"]["buy1"]["armed"]), ([], False))
    check("buy1 2,100 re-arms (silent)", (feed(2100), st["lines"]["buy1"]["armed"]), ([], True))
    check("buy1 fires again at 2,000 (yield5 re-armed at 2,099 fires too)", feed(2000), ["buy1", "yield5"])
    st = copy.deepcopy(seed)
    ev = engine.eval_lines(cfg, st, rows(("2026-10-28", 1990)), "2026-10-28", at("2026-10-28"))
    title, msg, pri = engine.build_lines(cfg, ev, {"rows": rows(("2026-10-28", 1990)), "src": "Yahoo 정규장"}, "2026-10-28 19:00 KST")
    check("gap through all three lines: fires all", sorted(e["key"] for e in ev), ["buy1", "review", "yield5"])
    check("gap: title is buy1 (매수 상향 조건 1)", title, f"{pfx} 종가 2,000원 이하 | 매수 상향 조건 1 충족, 배당 확정 전")
    check("gap pri 4", pri, 4)
    todo = next(x for x in msg.split("\n") if x.startswith("지금 할 일:"))
    check("gap: 할 일 is buy1 only (no 공격안, no 재점검 신호, 소량 분할 문구)", ("공격안" in todo, "재점검 신호입니다" in todo, "소량 분할만" in todo and "투자 논리 재점검" in todo), (False, False, True))
    check("gap: yield wording 5.10% on 102원", "최저 배당수익률 5.05%(연 102원 / 종가 2,020원" in msg or "최저 배당수익률 5.13%(연 102원 / 종가 1,990원" in msg, True)
    st = copy.deepcopy(seed)
    ev = engine.eval_lines(cfg, st, rows(("2026-10-28", 2000)), "2026-10-28", at("2026-10-28"))
    title, msg, pri = engine.build_lines(cfg, ev, {"rows": rows(("2026-10-28", 2000)), "src": "Yahoo 정규장"}, "2026-10-28 19:00 KST")
    check("2,000 exactly: buy1 + yield5, title buy1", (sorted(e["key"] for e in ev), title), (["buy1", "yield5"], f"{pfx} 종가 2,000원 이하 | 매수 상향 조건 1 충족, 배당 확정 전"))
    check("2,000: 5.10% on 102원", "최저 배당수익률 5.10%(연 102원 / 종가 2,000원" in msg, True)
    st = copy.deepcopy(seed)
    ev = engine.eval_lines(cfg, st, rows(("2026-10-28", 2040)), "2026-10-28", at("2026-10-28"))
    title, msg, pri = engine.build_lines(cfg, ev, {"rows": rows(("2026-10-28", 2040)), "pair_rows": rows(("2026-10-28", 7000)), "src": "Yahoo 정규장"}, "2026-10-28 19:00 KST")
    check("yield5 title", title, f"{pfx} 종가 2,040원 이하 | 최저 배당수익률 5% 돌파, 공격안 검토")
    check("yield shown = 102 / close", "최저 배당수익률 5.00%(연 102원 / 종가 2,040원" in msg, True)
    check("yield5 message shows discount", "괴리율 70.86%" in msg, True)
    check("yield5 pri", pri, 4)

    # ---- ratio lines
    st = copy.deepcopy(seed)
    days = iter(f"2026-11-{i:02d}" for i in range(2, 31))
    pref = 2200.0
    pc = lambda v: pref / (1 - v / 100)  # noqa: E731  common close giving discount v
    # 2026-11-04 thresholds: > 75.0% 괴리 확대, < 65.0% 괴리 축소, re-arm back inside 66~74%
    check("ratio 70% inside band silent", feed(pref, pc(70.0)), [])
    check("ratio 75.0% exactly silent (gt)", feed(pref, pc(75.0)), [])
    check("ratio 75.5% widen fires", feed(pref, pc(75.5)), ["widen"])
    check("ratio 77% stays above, no repeat", feed(pref, pc(77.0)), [])
    check("ratio 74.5% no re-arm yet", feed(pref, pc(74.5)), [])
    check("ratio 70% back inside re-arms (silent)", (feed(pref, pc(70.0)), st["ratio"]["lines"]["widen"]["armed"]), ([], True))
    check("ratio 76% widen again after re-arm", feed(pref, pc(76.0)), ["widen"])
    check("ratio 70% re-arm", feed(pref, pc(70.0)), [])
    check("ratio 65.0% exactly silent (lt)", feed(pref, pc(65.0)), [])
    check("ratio 64.5% resolve (괴리 축소) fires", feed(pref, pc(64.5)), ["resolve"])
    check("ratio 63% stays below, no repeat", feed(pref, pc(63.0)), [])
    check("ratio 65.8% no re-arm yet", feed(pref, pc(65.8)), [])
    check("ratio 66.5% re-arms (silent)", (feed(pref, pc(66.5)), st["ratio"]["lines"]["resolve"]["armed"]), ([], True))
    check("ratio 64% resolve again", feed(pref, pc(64.0)), ["resolve"])
    # pair line: common 0220W0 close > 8,000 (tender price), re-arm at <= 7,800 (2026-11-04)
    st = copy.deepcopy(seed)
    days = iter(f"2026-12-{i:02d}" for i in range(1, 31))
    def pfeed(common: float) -> list:
        d = next(days)
        return [e["key"] for e in engine.eval_ratio(cfg, st, rows((d, 2180)), rows((d, common)), d, at(d)) if e["kind"] == "pair_line"]
    check("pair 7,560 no fire", pfeed(7560), [])
    check("pair 8,000 exactly no fire", pfeed(8000), [])
    check("pair 8,050 fires", pfeed(8050), ["tender8000"])
    check("pair 8,200 no repeat", pfeed(8200), [])
    check("pair 7,900 not re-armed", (pfeed(7900), st["ratio"]["pair_lines"]["tender8000"]["armed"]), ([], False))
    check("pair 7,800 re-armed", (pfeed(7800), st["ratio"]["pair_lines"]["tender8000"]["armed"]), ([], True))
    check("pair 8,100 fires again", pfeed(8100), ["tender8000"])
    st = copy.deepcopy(seed)
    s = sends(st, {"rows": rows(("2026-11-04", 2180)), "pair_rows": rows(("2026-11-04", 8050)), "src": "Yahoo 정규장"}, at("2026-11-04"))
    check("pair line message title", [x[0] for x in s], ["[0220WL] 한화M&S 보통주 공개매수가 8,000원 회복 (0220W0/0220WL)"])
    keys = ["이게 뭐 알림", "지금 결과/상태", "지금 할 일", "나중에", "익절/줄일 때", "하지 말 것"]
    pos = [s[0][1].index(k + ":") for k in keys]
    check("pair line six-item order", pos == sorted(pos), True)
    check("pair line body", all(k in s[0][1] for k in ("보통주(0220W0) 종가 8,050원", "대비 +0.6", "우선주 종가 2,180원", "괴리율 72.92%", "공개매수가보다 높게")), True)
    check("pair line no middle dot / em-dash", ("\u00b7" in s[0][1] + s[0][0]) or ("\u2014" in s[0][1] + s[0][0]), False)
    # same-day closes only; hold when either is missing
    st = copy.deepcopy(seed)
    ev = engine.eval_ratio(cfg, st, rows(("2026-10-28", 1900)), rows(("2026-10-27", 7700)), "2026-10-28", at("2026-10-28"))
    check("pair missing that day: hold (no ratio from stale pair)", (ev, st["ratio"]["last_day"]), ([], "2026-10-27"))
    ev = engine.eval_ratio(cfg, st, rows(("2026-10-28", 1900), ("2026-10-29", 2000)), rows(("2026-10-28", 7700), ("2026-10-29", 8000)), "2026-10-29", at("2026-10-29"))
    check("next run catches up in order", [(e["day"], e["key"]) for e in ev], [("2026-10-28", "widen")])
    check("ratio cursor", st["ratio"]["last_day"], "2026-10-29")
    st = copy.deepcopy(seed)
    ev = engine.eval_ratio(cfg, st, rows(("2026-10-27", 2200)), rows(("2026-10-27", 7700), ("2026-10-28", 9000)), "2026-10-28", at("2026-10-28"))
    check("main missing that day: nothing judged", (ev, st["ratio"]["last_day"]), ([], "2026-10-27"))
    st = copy.deepcopy(seed)
    check("intraday not judged", engine.eval_ratio(cfg, st, rows(("2026-10-28", 1000)), rows(("2026-10-28", 9000)), "2026-10-28", at("2026-10-28", 14)), [])
    st = copy.deepcopy(seed)
    s = sends(st, {"rows": rows(("2026-10-28", 1900)), "pair_rows": rows(("2026-10-28", 7700)), "src": "Yahoo 정규장"}, at("2026-10-28"))
    check("price + ratio same day: two messages", [x[0] for x in s],
          [f"{pfx} 종가 2,000원 이하 | 매수 상향 조건 1 충족, 배당 확정 전", "[0220WL] 한화M&S 3우B 괴리율 75% 돌파 (0220WL/0220W0)"])
    check("ratio message body", "괴리율 75.32%(기준 75.0%, 차이 +0.32%p)" in s[1][1], True)
    check("ratio message prior-day gap", "직전 거래일 괴리율 71.43%" in s[1][1], True)

    # ---- hold on Yahoo failure; NXT never judged
    g = engine.__dict__
    orig = {k: g[k] for k in ("yahoo_rows", "naver_rows", "get_json")}
    try:
        def down(*a, **k):
            raise RuntimeError("HTTP 503")
        g["yahoo_rows"] = down
        g["naver_rows"] = lambda c: rows(("2026-10-28", 1500))
        d1, f1 = engine.collect(cfg, at("2026-10-28"))
        check("yahoo down: no rows, two fails", (d1["rows"], d1["pair_rows"], len(f1)), ([], [], 2))
        st = copy.deepcopy(seed)
        s = sends(st, d1, at("2026-10-28"), f1)
        check("yahoo down: only the fail notice", [x[0] for x in s], [f"{pfx} 조회 실패 | 조건 판단 보류"])
        check("fail notice -> cwally-ops", route_topic("cwally-port", s[0][0]), "cwally-ops")
        check("yahoo down: cursors unchanged", (st["last_day"], st["ratio"]["last_day"]), ("2026-10-27", "2026-10-27"))
        check("fail notice once a day", sends(st, d1, at("2026-10-28", 22), f1), [])
        g["yahoo_rows"] = lambda c, sym, regular_only=False: rows(("2026-10-27", 2200 if sym == "0220WL.KS" else 7700))
        d2, f2 = engine.collect(cfg, at("2026-10-28"))
        check("session close missing for both -> two hold notes", sum("2026-10-28 정규장 종가 없음" in f for f in f2), 2)
        g["yahoo_rows"] = lambda c, sym, regular_only=False: (rows(("2026-10-27", 2200), ("2026-10-28", 2230)) if sym == "0220WL.KS" else rows(("2026-10-27", 7700)))
        d3, f3 = engine.collect(cfg, at("2026-10-28"))
        st = copy.deepcopy(seed)
        s = sends(st, d3, at("2026-10-28"), f3)
        check("pair missing only: price judged, ratio held, ops notice", ([x[0] for x in s], st["last_day"], st["ratio"]["last_day"]),
              ([f"{pfx} 조회 실패 | 조건 판단 보류"], "2026-10-28", "2026-10-27"))
        st = copy.deepcopy(seed)
        low = lambda c, rows_, now: engine.nxt_line(c["short"], 2200, 1500, now)  # noqa: E731
        check("NXT far below lines never fires", engine.plan_sends(cfg, st, {"rows": rows(("2026-10-28", 2200)), "pair_rows": rows(("2026-10-28", 7700))}, [], at("2026-10-28"), None, low, no_notice), [])
    finally:
        g.update(orig)

    # ---- dated notice events
    vis = {"no": "9001", "code": "489790", "title": "한화비전(주) 연결재무제표기준영업(잠정)실적(공정공시)", "datetime": "2026-10-29 06:50"}
    vis_pre = {"no": "9000", "code": "489790", "title": "한화비전(주) 결산실적공시 예고(안내공시)", "datetime": "2026-10-28 06:50"}
    tnd = {"no": "9100", "code": "0220W0", "title": "한화머시너리앤서비스홀딩스 주식회사 최대주주등소유주식변동신고서", "datetime": "2026-11-04 17:00"}
    feed_map = {}
    nf = lambda c, code: list(feed_map.get(code, []))  # noqa: E731
    base = {"rows": rows(("2026-10-27", 2200)), "pair_rows": rows(("2026-10-27", 7700))}
    st = copy.deepcopy(seed)
    feed_map.clear()
    feed_map["489790"] = [vis_pre]
    check("10-28: no event yet", sends(st, base, at("2026-10-28"), notice_fn=nf), [])
    feed_map["489790"] = [vis, vis_pre]
    s = sends(st, base, at("2026-10-29"), notice_fn=nf)
    check("10-29 vision filing found", [x[0] for x in s], [f"{pfx} 한화비전 3분기 실적 체크 | 공시 확인됨"])
    check("vision checklist", all(k in s[0][1] for k in ("6,600억대", "주가 반응", "괴리율 변화")), True)
    check("vision second run silent", sends(st, base, at("2026-10-29", 23), notice_fn=nf), [])
    st = copy.deepcopy(seed)
    feed_map["489790"] = [vis_pre]
    check("10-29 preview only -> 확인 필요 (예고 excluded)", [x[0] for x in sends(st, base, at("2026-10-29"), notice_fn=nf)], [f"{pfx} 한화비전 3분기 실적 체크 | 공시 확인 필요"])
    st = copy.deepcopy(seed)
    feed_map["489790"] = [vis]
    check("catch-up 11-01 (3 days)", [x[0] for x in sends(st, base, at("2026-11-01"), notice_fn=nf)][:1], [f"{pfx} 한화비전 3분기 실적 체크 | 공시 확인됨"])
    st = copy.deepcopy(seed)
    st["notice_done"]["tender-result-2026-11"] = "x"
    check("too late 11-02 silent", [x[0] for x in sends(st, base, at("2026-11-02"), notice_fn=nf) if "한화비전" in x[0]], [])

    def boom(c, code):
        raise RuntimeError("timeout")
    st = copy.deepcopy(seed)
    s = sends(st, base, at("2026-10-29"), notice_fn=boom)
    check("feed down on 10-29 -> 확인 필요 + ops notice", [x[0] for x in s], [f"{pfx} 한화비전 3분기 실적 체크 | 공시 확인 필요", f"{pfx} 조회 실패 | 조건 판단 보류"])
    check("확인 필요 stays on cwally-port", route_topic("cwally-port", s[0][0]), "cwally-port")
    st = copy.deepcopy(seed)
    st["notice_done"]["vision3q-2026"] = "x"
    feed_map.clear()
    # tender-result check disabled 2026-11-04 (enabled=false): never emits, even with the filing present
    check("tender disabled in config", next(e for e in cfg["notice_events"] if e["id"] == "tender-result-2026-11").get("enabled"), False)
    for d in ("2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05"):
        check(f"{d} tender check silent (not found)", sends(st, base, at(d), notice_fn=nf), [])
    st = copy.deepcopy(seed)
    st["notice_done"]["vision3q-2026"] = "x"
    feed_map["0220W0"] = [tnd]
    check("11-04 tender silent even with filing", sends(st, base, at("2026-11-04", 19), notice_fn=nf), [])
    check("tender not marked done (just skipped)", "tender-result-2026-11" in st["notice_done"], False)
    feed_map.clear()
    st = copy.deepcopy(seed)
    st["notice_done"]["vision3q-2026"] = "x"
    s = sends(st, {"rows": rows(("2026-11-04", 2180)), "pair_rows": rows(("2026-11-04", 9000)), "src": "Yahoo 정규장"}, at("2026-11-04"), notice_fn=nf)
    check("gap > 75% still fires on 11-04 with tender off", [x[0] for x in s if "괴리율" in x[0]], ["[0220WL] 한화M&S 3우B 괴리율 75% 돌파 (0220WL/0220W0)"])

    # ---- notice watch (classes, 2026-11-27)
    old = {"no": "8000", "code": "0220W0", "title": "한화머시너리앤서비스홀딩스 주식회사 현금ㆍ현물배당결정", "datetime": "2026-10-08 17:00"}
    new = {"no": "8001", "code": "0220W0", "title": "한화머시너리앤서비스홀딩스 주식회사 주식소각결정", "datetime": "2026-11-10 16:00"}
    new2 = {"no": "8002", "code": "0220WL", "title": "한화머시너리앤서비스홀딩스 주식회사 현금ㆍ현물배당결정", "datetime": "2026-11-11 16:00"}
    other = {"no": "8003", "code": "0220W0", "title": "한화머시너리앤서비스홀딩스 주식회사 임원ㆍ주요주주특정증권등소유상황보고서", "datetime": "2026-11-11 16:00"}
    docs = {}
    saved_hook = engine.NOTICE_DETAIL
    engine.NOTICE_DETAIL = lambda c, f, cl: dividend_detail_from_text(docs.get(f["no"], ""))
    try:
        st = copy.deepcopy(seed)
        st["notice_done"] = {"vision3q-2026": "x", "tender-result-2026-11": "x"}
        feed_map.clear()
        feed_map["0220W0"] = [old, new, other]
        s = sends(st, base, at("2026-11-10"), notice_fn=nf)
        check("watch: new 소각 filing once (p3 payout class); old and unrelated ignored", [(x[0], x[2]) for x in s], [(f"{pfx} 소각 또는 자사주 공시 | 주식소각결정", 3)])
        check("watch: second run silent", sends(st, base, at("2026-11-10", 23), notice_fn=nf), [])
        feed_map["0220WL"] = [new2]
        docs["8002"] = DIV_DOC.format(dps="102", kind="결산배당")
        s = sends(st, base, at("2026-11-11"), notice_fn=nf)
        check("watch: 배당결정 DPS 102 -> p4, 연 102원 이상 확인 문구", ([x[0] for x in s], s[0][2], "연 102원 이상 확인, 매수 상향 조건 충족" in s[0][0] and "연 102원 이상 확인, 매수 상향 조건 충족" in s[0][1]),
              ([f"{pfx} 결산배당 결정 공시 | 3우B 102원 (연 102원 이상 확인, 매수 상향 조건 충족)"], 4, True))
        check("watch: seen ids", st["notice_seen"], ["8001", "8002"])

        def one(no, title, doc=None, code="0220WL"):
            f = {"no": no, "code": code, "title": "한화머시너리앤서비스홀딩스 주식회사 " + title, "datetime": "2026-11-12 16:00"}
            if doc is not None:
                docs[no] = doc
            return engine.build_notice_watch(cfg, {"filing": f}, base, "2026-11-12 19:00 KST")
        t, m, p = one("9001", "현금ㆍ현물배당결정", DIV_DOC.format(dps="43", kind="결산배당"))
        PRORATA_PHRASE = "2026 일할 배당 정상 이행, 2027년 연 102원 지급 확인 전까지 2,000원 이하 소량 분할만"
        check("watch: DPS 43 = 일할 정상 이행 문구 in title and body", (t, p, PRORATA_PHRASE in t and PRORATA_PHRASE in m and "42.76원" in m),
              (f"{pfx} 결산배당 결정 공시 | 3우B 43원 ({PRORATA_PHRASE})", 4, True))
        t, m, p = one("9002", "현금ㆍ현물배당결정", DIV_DOC.format(dps="20", kind="결산배당"))
        check("watch: DPS 20 = 일할 미달", (t, "이월되는지" in m), (f"{pfx} 결산배당 결정 공시 | 3우B 20원 (일할 미달)", True))
        t, m, p = one("9003", "현금ㆍ현물배당결정", DIV_DOC.format(dps="-", kind="결산배당"))
        check("watch: DPS 0 switches to 무배당 warning p4", (t, p), (f"{pfx} 무배당 경고 | 3우B 배당금 0원", 4))
        check("watch: 무배당 warning has no 매수 wording", "매수" in t + m, False)
        t, m, p = one("9004", "현금ㆍ현물배당결정", "문서 형식이 다름")
        check("watch: unparsable doc -> manual check text, class stays 배당", (t.startswith(f"{pfx} 결산배당 결정 공시 | "), "직접 확인하세요" in m, p), (True, True, 4))
        t, m, p = one("9005", "유상증자결정")
        check("watch: 유상증자 warning p4, no 매수 wording", (t, p, "매수" in t + m), (f"{pfx} 유상증자 결정 경고 | 유상증자결정", 4, False))
        t, m, p = one("9006", "무배당결정")
        check("watch: 무배당 title warning p4, no 매수 wording", (t, p, "매수" in t + m), (f"{pfx} 무배당 경고 | 무배당결정", 4, False))
        t, m, p = one("9007", "자기주식취득결정")
        check("watch: 자기주식 p3", (t, p), (f"{pfx} 소각 또는 자사주 공시 | 자기주식취득결정", 3))
        t, m, p = one("9008", "현금ㆍ현물배당결정(자회사의 주요경영사항)", code="0220W0")
        check("watch: 자회사 배당결정 is p3 sub class, not the parent DPS", (t.startswith(f"{pfx} 자회사 공시, 모회사 영향 확인 | "), p, "3우B 1주당" in m), (True, 3, False))
        watch_all = [one("9010", "현금ㆍ현물배당결정", DIV_DOC.format(dps="102", kind="결산배당")), one("9011", "유상증자결정"), one("9012", "주식소각결정"),
                     one("9013", "현금ㆍ현물배당결정", DIV_DOC.format(dps="-", kind="결산배당")), one("9014", "현금ㆍ현물배당결정(자회사의 주요경영사항)", code="0220W0")]
        check("watch: no forbidden chars in any class message", any(ch in t + m for t, m, _ in watch_all for ch in engine.FORBIDDEN), False)
        check("watch: classes keep six-item order", [t for t, m, _ in watch_all if [x.split(":")[0] + ":" for x in m.split("\n")[1:7]] != ["이게 뭐 알림:", "지금 결과/상태:", "지금 할 일:", "나중에:", "익절/줄일 때:", "하지 말 것:"]], [])
    finally:
        engine.NOTICE_DETAIL = saved_hook
    check("parse: real Hanwha layout (제3우선주 표) 1,150", parse_pref3_dps(REAL_HANWHA_SNIPPET), (1150, "제3우선주 표"))
    check("parse: item 3 fallback (종류주식 칸)", parse_pref3_dps("3. 1주당 배당금(원) 보통주식 100 종류주식 150 - 차등배당"), (150, "종류주식 칸"))
    check("parse: dash = 0", parse_pref3_dps("3. 1주당 배당금(원) 보통주식 - 종류주식 -"), (0, "종류주식 칸"))
    lst = [{"rcept_no": "20260225801281", "report_nm": "현금ㆍ현물배당을위한주주명부폐쇄(기준일)결정"}, {"rcept_no": "20260225801273", "report_nm": "현금ㆍ현물배당결정              "},
           {"rcept_no": "20260226801327", "report_nm": "중대재해발생(종속회사의주요경영사항)"}]
    check("dart pick: 배당결정 only, not 주주명부폐쇄", (pick_dividend_filing(lst) or {}).get("rcept_no"), "20260225801273")
    check("dart pick: 기재정정 is later and wins", pick_dividend_filing(lst + [{"rcept_no": "20260227800001", "report_nm": "[기재정정]현금ㆍ현물배당결정"}])["rcept_no"], "20260227800001")
    check("dart pick: none", pick_dividend_filing(lst[:1]), None)
    check("config: year_guard, ends_kst, notice priority", (cfg["year_guard"], cfg["ends_kst"], cfg["notice_watch"]["priority"]), ([2026, 2027], "2027-07-13 23:59", 3))
    check("config: buy1 line", {k: next(l for l in cfg["lines"] if l["key"] == "buy1")[k] for k in ("op", "level", "rearm_at")}, {"op": "le", "level": 2000, "rearm_at": 2100})

    # ---- time checks (dated reminders, no filing lookup), footer note
    foot = cfg["footer_note"]
    tcs = {t["id"]: t for t in cfg["time_checks"]}
    st = copy.deepcopy(seed)
    st["notice_done"] = {"vision3q-2026": "x", "tender-result-2026-11": "x"}
    tc_titles = lambda s: [x[0] for x in s if "날짜 점검" in x[0]]  # noqa: E731
    check("time: window start 12-21 silent", tc_titles(sends(st, base, at("2026-12-21"))), [])
    check("time: inside window 01-08 silent", tc_titles(sends(st, base, at("2027-01-08"))), [])
    s = sends(st, base, at("2027-01-09"))
    check("time: 6m fires on end day 01-09", tc_titles(s), [f"{pfx} 6개월 상대성과 점검 | 날짜 점검"])
    check("time: 6m checklist", all(k in s[0][1] for k in ("1) 보통주 대비 괴리율 변화와 상대수익.", "2) 자회사 실적(비전, 로보틱스 vs 유통).", "3) 동반 부진이면 87주 중 43주 매도 검토.")), True)
    check("time: no 공시 확인 필요 wording", any("공시 확인 필요" in t + m for t, m, *_ in s), False)
    check("time: message ends with footer", s[0][1].split("\n")[-1], foot)
    check("time: dedupe id stored", st["time_done"], {"rel-6m-2027-01": "2027-01-09"})
    check("time: second run same day silent", tc_titles(sends(st, base, at("2027-01-09", 23))), [])
    check("time: next days silent", tc_titles(sends(st, base, at("2027-01-11"))), [])
    check("time: 12m window start 06-22 silent", tc_titles(sends(st, base, at("2027-06-22"))), [])
    s = sends(st, base, at("2027-07-10"))
    check("time: 12m fires on end day 07-10", tc_titles(s), [f"{pfx} 12개월 인컴 유지 점검 | 날짜 점검"])
    check("time: 12m checklist", all(k in s[0][1] for k in ("누적 우선배당 102원이 실제로 지급됐는지", "보통주 대비 크게 밀리지 않았는지", "아니면 43주 매도 검토")), True)
    check("time: 12m once", tc_titles(sends(st, base, at("2027-07-12"))), [])
    # 2027-07-10 is Saturday: auto 19:00 slot and is_trading_day guard stay silent that day.
    # catch_up 3 days -> first weekday send is Mon 07-12 (or Tue 07-13). ends_kst 2027-07-13 23:59 covers that.
    st = copy.deepcopy(seed)
    st["notice_done"] = {"vision3q-2026": "x", "tender-result-2026-11": "x"}
    check("time: 12m end day Sat, first weekday Mon 07-12 catches up", tc_titles(sends(st, base, at("2027-07-12"))), [f"{pfx} 12개월 인컴 유지 점검 | 날짜 점검"])
    from ntfy_util import ends_skip_reason
    twelve_title = f"{pfx} 12개월 인컴 유지 점검 | 날짜 점검"
    def engine_open(when):
        stamp = when.strftime("%Y-%m-%d %H:%M KST")
        ends = cfg["ends_kst"]
        return when.year in cfg["year_guard"] and not (ends and stamp[:len(ends)] > ends)
    for d, want in (("2027-07-10", True), ("2027-07-12", True), ("2027-07-13", True), ("2027-07-14", False)):
        now = at(d)
        skip = ends_skip_reason("cwally-port", twelve_title, config_name="0220wl_close", config=cfg, now=now)
        check(f"12m ends: engine year_guard/ends open at {d} 19:00", engine_open(now), want)
        check(f"12m ends: ntfy ends_skip_reason is None at {d} 19:00", skip is None, want)
    st = copy.deepcopy(seed)
    st["notice_done"] = {"vision3q-2026": "x", "tender-result-2026-11": "x"}
    check("time: end day Sat, first run Mon 01-11 catches up", tc_titles(sends(st, base, at("2027-01-11"))), [f"{pfx} 6개월 상대성과 점검 | 날짜 점검"])
    st = copy.deepcopy(seed)
    st["notice_done"] = {"vision3q-2026": "x", "tender-result-2026-11": "x"}
    check("time: catch-up limit 3 days (01-13 silent)", tc_titles(sends(st, base, at("2027-01-13"))), [])
    st = copy.deepcopy(seed)
    s = sends(st, {"rows": rows(("2026-10-28", 1900)), "pair_rows": rows(("2026-10-28", 7700)), "src": "Yahoo 정규장"}, at("2026-10-28"), ["x"])
    check("footer on every sent message (price, ratio, fail)", [m.split("\n")[-1] == foot for _, m, *_ in s], [True, True, True])
    check("footer after the 참고 line", s[0][1].split("\n")[-2].startswith("참고: "), True)
    check("footer has no middle dot / em-dash", any(ch in foot for ch in engine.FORBIDDEN), False)
    check("install message ends with footer", install_msg(cfg, copy.deepcopy(seed), base, [], "x")[1].split("\n")[-1], foot)
    st = copy.deepcopy(seed)
    st["notice_done"] = {"vision3q-2026": "x", "tender-result-2026-11": "x"}
    tc_sample = sends(st, {"rows": rows(("2027-01-08", 2150)), "pair_rows": rows(("2027-01-08", 7400)), "src": "Yahoo 정규장"}, at("2027-01-11"))
    tc_sample = [x for x in tc_sample if "날짜 점검" in x[0]][0]

    # ---- messages
    stamp = "2026-10-28 19:00 KST"
    d = {"rows": rows(("2026-10-28", 1990)), "pair_rows": rows(("2026-10-28", 8000)), "src": "Yahoo 정규장"}
    msgs = []
    for ln in cfg["lines"]:
        msgs.append(engine.build_lines(cfg, [{"kind": "line", "key": ln["key"], "line": ln, "day": "2026-10-28", "close": 1990.0}], d, stamp))
    for ln in cfg["ratio_lines"]:
        msgs.append(engine.build_ratio(cfg, [{"kind": "ratio", "key": ln["key"], "line": ln, "day": "2026-10-28", "close": 1990.0, "pair": 8000.0, "v": ln["level"]}], d, stamp))
    for ev in cfg["notice_events"]:
        msgs.append(engine.build_notice_event(cfg, {"ev": ev, "filing": None, "ffail": "x"}, d, stamp))
    for tc in cfg["time_checks"]:
        msgs.append(engine.build_time_check(cfg, {"tc": tc}, d, stamp))
    msgs.append(engine.build_notice_watch(cfg, {"filing": new}, d, stamp))
    msgs.append(engine.build_fail(cfg, ["x"], stamp))
    msgs.append(install_msg(cfg, copy.deepcopy(seed), base, [], stamp) + (3,))
    order = ["이게 뭐 알림:", "지금 결과/상태:", "지금 할 일:", "나중에:", "익절/줄일 때:", "하지 말 것:"]
    check("no middle dot / em-dash", [t for t, m, _ in msgs if any(ch in t + m for ch in engine.FORBIDDEN)], [])
    check("six-item body order", [t for t, m, _ in msgs if [x.split(":")[0] + ":" for x in m.split("\n")[1:7]] != order], [])
    check("titles have prefix", [t for t, *_ in msgs if not t.startswith(pfx + " ")], [])
    check("signals stay on cwally-port", [t for t, *_ in msgs[:-2] if route_topic("cwally-port", t) != "cwally-port"], [])
    inst = msgs[-1][1]
    check("install mentions unconfirmed items", ("잔여재산 분배 순위" in inst, "일할" in inst), (True, True))
    for t, m, _ in msgs:
        print("SAMPLE:", t)
    print("SAMPLE BODY:", msgs[2][1].replace("\n", " / "))
    print("SAMPLE TIME CHECK:", tc_sample[0])
    print(tc_sample[1])
    print("SELFTEST", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(run(sys.argv))
