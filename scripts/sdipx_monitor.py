#!/usr/bin/env python3
"""[SDI-PX] 삼성SDI(006400) KRX 확정 종가 가격선. 기존 평일 19:00 k203780 슬롯.

선 (configs/sdipx.json lines): 종가 <= 500,000 p3 (515,000 초과 종가 뒤 재무장),
<= 470,000 p4 (484,100 초과 뒤 재무장), >= 650,000 p3 (630,500 미만 뒤 재무장). 선마다 1번.
종가: Daum KRX 일별만(NXT 미포함). 네이버 일별은 쓰지 않음. 15:40 이후 확정 종가만, 데이터 날짜로 1번 판정.
KRX 거래일 19:00 에 오늘 KRX 종가를 못 읽으면 판정을 건너뛰고 cwally-ops 하루 1번.
첫 실행은 상태만 기록. ends_kst(2027-03-31 19:00) 뒤에는 조용히 종료.
modes: auto | dry(발송 없음, state 안 씀) | install(설치 확인 1회, state 안 씀) | selftest ; --as-of "YYYY-MM-DD HH:MM" (dry 전용)
"""
from __future__ import annotations

import json
import re
import sys
import traceback
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from kr_calendar import KST, is_trading_day, session_day, session_close_ready  # noqa: E402
from ntfy_util import publish, resolve_server, resolve_topic, route_topic  # noqa: E402

TAG = "[SDI-PX]"
NAME = "sdipx"
CFG_PATH = ROOT / "configs" / f"{NAME}.json"
STATE_PATH = ROOT / "state" / f"{NAME}.json"
CLOSE_HM = (15, 40)
DAUM = "https://finance.daum.net/api/quote/A{code}/days?symbolCode=A{code}&page=1&perPage=10&pagination=true"


def daum_rows(code: str) -> list[dict]:
    req = urllib.request.Request(DAUM.format(code=code), headers={"User-Agent": "Mozilla/5.0", "Referer": f"https://finance.daum.net/quotes/A{code}"})
    with urllib.request.urlopen(req, timeout=25) as resp:
        raw = json.loads(resp.read().decode("utf-8"))
    out = []
    for r in raw.get("data") or []:
        if str(r.get("symbolCode")) != "A" + code:
            raise ValueError("종목 코드 불일치")
        day, px = str(r.get("date") or "")[:10], r.get("tradePrice")
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", day) and px:
            out.append({"day": day, "close": float(px), "prev": float(r["prevClosingPrice"]) if r.get("prevClosingPrice") else None})
    if not out:
        raise ValueError("KRX 일별 시세 없음")
    return sorted(out, key=lambda x: x["day"])


def confirmed(rows: list[dict], today: str, now: datetime) -> list[dict]:
    return [r for r in rows if session_close_ready(r["day"], today, now, CLOSE_HM)]


def hit(op: str, c: float, lvl: float) -> bool:
    return {"le": c <= lvl, "ge": c >= lvl, "gt": c > lvl, "lt": c < lvl}[op]


def seed(cfg: dict, row: dict, stamp: str) -> dict:
    return {"last_date": row["day"], "last_close": row["close"],
            "armed": {ln["key"]: not hit(ln["op"], row["close"], ln["level"]) for ln in cfg["lines"]},
            "seed_note": f"첫 실행 {stamp}: 마지막 KRX 확정 종가 {row['day']} {row['close']:,.0f}원"}


def evaluate(cfg: dict, ps: dict, rows: list[dict], today: str) -> list[dict]:
    out = []
    for r in rows:
        d, c = r["day"], r["close"]
        if d <= ps["last_date"] or d < cfg["window_start"] or d > today:
            continue
        for ln in cfg["lines"]:
            k = ln["key"]
            if hit(ln["op"], c, ln["level"]) and ps["armed"].get(k, True):
                ps["armed"][k] = False
                out.append({**ln, "day": d, "close": c, "prev": r.get("prev")})
            elif hit(ln["rearm_op"], c, ln["rearm"]) and not ps["armed"].get(k, True):
                ps["armed"][k] = True
        ps["last_date"], ps["last_close"] = d, c
    return out


def judge_text(cfg: dict, c: float) -> str:
    inside = [ln["head"] for ln in cfg["lines"] if hit(ln["op"], c, ln["level"])]
    return "선 안쪽: " + " / ".join(inside) if inside else "500,000원 초과 650,000원 미만, 세 선 사이라 알림 없음"


def body(stamp: str, hdr: str, what: str, status: str, action: str, later: str, trim: str, avoid: str) -> str:
    return "\n".join([f"{stamp} | 006400 | 삼성SDI {hdr}", f"이게 뭐 알림: {what}", f"지금 결과/상태: {status}",
                      f"지금 할 일: {action}", f"나중에: {later}", f"익절/줄일 때: {trim}", f"하지 말 것: {avoid}"])


WHAT = "삼성SDI(006400) KRX 확정 종가 가격선 알림(500,000 / 470,000 / 650,000원). 자동 매수 신호 아님, 지정가 주문만."
AMPC = "2차 매수는 기존 [SDI] 알림의 4분기 AMPC 제외 흑자 확인도 함께 필요"


def messages(cfg: dict, ev: list[dict], stamp: str) -> list[tuple[str, str, int]]:
    b = cfg["baseline"]
    out = []
    for e in ev:
        dist = (e["close"] / b["close"] - 1) * 100
        rearm = f"{e['rearm']:,}원 {'넘는' if e['rearm_op'] == 'gt' else '밑'} 종가가 나온 뒤 다시 들어오면 다시 알림."
        st = (f"{e['day']} KRX 종가 {e['close']:,.0f}원"
              + (f"(전일 {e['prev']:,.0f}원)" if e.get("prev") else "")
              + f", 기준 {b['day']} 종가 {b['close']:,}원 대비 {dist:+.1f}%. 선 {e['level']:,}원.")
        if e["key"] == "h650":
            per = e["close"] / cfg["eps_est"]
            st += f" 지금 종가 기준 PER 약 {per:.0f}배(추정 EPS {cfg['eps_est']:,}원)."
            act = "추정치(EPS) 상향이 없었다면 추격 매수 금지. 보유분은 비중 점검."
            trim = "과열 구간이면 늘린 몫부터 지정가로 나눠 줄이기 검토."
            avoid = "65만원 돌파만 보고 따라 사기, 시장가 주문."
        else:
            act = (f"2차 분할매수 검토 가능. {AMPC}. 지정가로 나눠서." if e["key"] == "l500"
                   else f"2차 구간 하단. 펀더멘털 변화(실적, 수주, 정책) 확인 뒤 2차 집행 검토. {AMPC}. 지정가로만.")
            trim = "더 내려가면 470,000원 선에서 따로 알림. 계획 비중 넘기지 않기." if e["key"] == "l500" else "하단 이탈이 이어지면 계획 자체를 다시 점검."
            avoid = "자동 매수 신호로 보기, 시장가 주문, 한 번에 다 사기."
        out.append((f"{TAG} 006400 {e['head']}", body(stamp, "KRX 종가 가격선", WHAT, st, act, rearm + (f" {AMPC}." if e["key"] == "h650" else ""), trim, avoid), e["priority"]))
    return out


def ops_msg(stamp: str, status: str) -> tuple[str, str]:
    return (f"{TAG} 006400 KRX 종가 조회 실패 | 판정 건너뜀",
            body(stamp, "KRX 종가 읽기 실패", "삼성SDI 가격선 알림의 데이터 읽기 실패 안내. 하루 1번. 매매 신호 아님.", status,
                 "증권 앱에서 삼성SDI KRX 종가를 직접 보세요.", "다음 19:00 슬롯에서 다시 읽고, 빠진 날짜는 KRX 종가로 이어서 판정합니다.",
                 "이 안내로 비중을 바꾸지 않기.", "NXT 시간외 가격이나 네이버 일별 값으로 대신 판정하기."))


def install_message(stamp: str) -> tuple[str, str]:
    return (f"{TAG} 006400 설치 확인 | 매매 신호 아님",
            body(stamp, "설치 확인", "삼성SDI(006400) KRX 종가 가격선 알림 설치 확인용 테스트 발송입니다. 매매 신호가 아닙니다.",
                 "평일 19:00 KST 에 Daum KRX 확정 종가로 500,000원 이하(2차 분할매수 구간), 470,000원 이하(2차 구간 하단), "
                 "650,000원 이상(과열 점검)을 선마다 1번 봅니다. 기준은 10/29 KRX 종가 538,000원.",
                 "알림이 도착했는지 확인만 하세요.",
                 "재무장: 515,000원 초과, 484,100원 초과, 630,500원 미만 종가 뒤. 2027-03-31 19:00 에 끝납니다.",
                 "해당 없음.", "이 메시지를 매매 신호로 해석하기."))


def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(st: dict) -> None:
    STATE_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def check_text(t: str, m: str) -> None:
    assert not re.search("[\u00b7\u318d\u2014]", t + m), t


def run(mode: str, now: datetime, cfg: dict, pub=publish, rows_fn=None) -> int:
    dry = mode == "dry"
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    topic, server = resolve_topic("port", cfg.get("ntfy_topic")), resolve_server(cfg.get("ntfy_server"))
    print(f"{stamp} {NAME} mode={mode}")
    if mode == "install":
        t, m = install_message(stamp)
        check_text(t, m)
        print(f"{t}\n{m}")
        pub(topic, t, m, 3, ["white_check_mark"], server, config_name=NAME)
        print("install confirmation sent (state untouched)")
        return 0
    sday = session_day(now)
    today = sday.isoformat()
    if today > cfg["window_end"]:
        print(f"{today} 감시 끝({cfg['window_end']} 19:00 슬롯이 마지막): 조용히 종료")
        return 0
    if not is_trading_day(sday) or today < cfg["window_start"]:
        print(f"{today} KRX 휴장 또는 시작 전: 조용히 종료")
        return 0
    st = load_state()
    out, err = [], None
    try:
        rows = confirmed((rows_fn or daum_rows)(cfg["code"]), today, now)
        if session_close_ready(today, today, now, CLOSE_HM) and (not rows or rows[-1]["day"] != today):
            err = f"{today} KRX 종가가 Daum KRX 일별에 아직 없음(마지막 {rows[-1]['day'] if rows else '없음'}). 오늘 판정 건너뜀"
        if rows:
            print(f"latest KRX close {rows[-1]['day']} {rows[-1]['close']:,.0f} (prev {rows[-1].get('prev')}): {judge_text(cfg, rows[-1]['close'])}")
        if not (st.get("price") or {}).get("last_date"):
            if rows and not err:
                st["price"] = seed(cfg, rows[-1], stamp)
                print(f"first run: state only, no alert: {st['price']}")
        elif rows:
            use = [r for r in rows if r["day"] != today] if err else rows
            out = messages(cfg, evaluate(cfg, st["price"], use, today), stamp)
            print(f"price: {st['price']}")
    except Exception as exc:  # noqa: BLE001
        err = f"Daum KRX 일별 조회 실패: {str(exc)[:160]}. 판정 건너뜀"
    ops = None
    if err:
        print("ERR:", err)
        if st.get("ops_day") != today:
            ops = ops_msg(stamp, err + ". 네이버(NXT 섞임)로 대신 판정하지 않음. state 는 그대로.")
            st["ops_day"] = today
    for t, m, pri in out:
        check_text(t, m)
        print(f"TITLE: {t}\nPRIORITY: {pri} TOPIC: {route_topic(topic, t)}\n{m}\n")
    if ops:
        print(f"OPS TITLE: {ops[0]} -> {route_topic(topic, ops[0])}\n{ops[1]}\n")
    if not out and not ops:
        print("보낼 것 없음")
    if dry:
        print("DRY RUN: no publish, state not written")
        return 0
    for t, m, pri in out:
        pub(topic, t, m, pri, ["chart_with_upwards_trend"], server, config_name=NAME)
    if ops:
        try:
            pub(resolve_topic("ops"), ops[0], ops[1], 3, ["warning"], server, config_name=NAME)
        except Exception as exc:  # noqa: BLE001
            print(f"ops notice failed: {exc}")
    st["updated_at"] = stamp
    save_state(st)
    return 0


def selftest() -> int:
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    ps = seed(cfg, {"day": "2026-10-29", "close": 538000.0}, "T")
    assert all(ps["armed"].values()) and "세 선 사이" in judge_text(cfg, 538000)
    mk = lambda cs: [{"day": f"2026-11-{i + 2:02d}", "close": float(c), "prev": None} for i, c in enumerate(cs)]  # noqa: E731
    ev = evaluate(cfg, ps, mk([500001, 500000, 490000, 470000, 484100, 484101, 469000, 515000, 515001, 500000, 650000, 640000, 630500, 630499, 650000]), "2026-12-31")
    got = [(e["key"], e["day"]) for e in ev]
    assert got == [("l500", "2026-11-03"), ("l470", "2026-11-05"), ("l470", "2026-11-08"), ("l500", "2026-11-11"),
                   ("h650", "2026-11-12"), ("h650", "2026-11-16")], got
    s2 = seed(cfg, {"day": "2026-10-29", "close": 480000.0}, "T")
    assert s2["armed"] == {"l500": False, "l470": True, "h650": True}, s2
    msgs = messages(cfg, ev, "T")
    assert "AMPC" in msgs[0][1] and "-7.1%" in msgs[0][1] and "PER 약 54배" in msgs[4][1], msgs[4][1]
    for t, m, _ in msgs:
        check_text(t, m)
    for t, m in (ops_msg("T", "x"), install_message("T")):
        check_text(t, m)
    # post-midnight backup: session_day still yesterday, wall clock < close must still count that close
    late = datetime(2026, 11, 5, 2, 46, tzinfo=KST)
    rows = [{"day": "2026-11-04", "close": 500000.0}, {"day": "2026-11-05", "close": 510000.0}]
    assert [r["day"] for r in confirmed(rows, "2026-11-04", late)] == ["2026-11-04"]
    intra = datetime(2026, 11, 5, 14, 0, tzinfo=KST)
    assert [r["day"] for r in confirmed(rows, "2026-11-05", intra)] == ["2026-11-04"]
    after = datetime(2026, 11, 5, 15, 40, tzinfo=KST)
    assert [r["day"] for r in confirmed(rows, "2026-11-05", after)] == ["2026-11-04", "2026-11-05"]
    print("selftest ok")
    return 0


def main() -> int:
    args = sys.argv[1:]
    mode = (args[0] if args and not args[0].startswith("--") else "auto").lower()
    if mode == "selftest":
        return selftest()
    if mode not in {"auto", "dry", "install"}:
        print(f"usage: {NAME}_monitor.py <auto|dry|install|selftest> [--as-of 'YYYY-MM-DD HH:MM']")
        return 2
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    if "--as-of" in args:
        if mode != "dry":
            print("--as-of 는 dry 에서만")
            return 2
        now = datetime.strptime(args[args.index("--as-of") + 1], "%Y-%m-%d %H:%M").replace(tzinfo=KST)
    try:
        return run(mode, now, cfg)
    except Exception as exc:  # noqa: BLE001
        print(traceback.format_exc(), file=sys.stderr)
        if mode != "auto":
            return 1
        try:
            t, m = ops_msg(now.strftime("%Y-%m-%d %H:%M KST"), f"스크립트 오류 {type(exc).__name__}: {str(exc)[:200]}. state 는 그대로.")
            publish(resolve_topic("ops"), t, m, 3, ["warning"], resolve_server(cfg.get("ntfy_server")), config_name=NAME)
        except Exception:  # noqa: BLE001
            return 1
        return 0


if __name__ == "__main__":
    sys.exit(main())
