#!/usr/bin/env python3
"""[IBB] iShares 바이오테크 ETF 분할 매수 가격선. 평일 09:00 KST morning 묶음 (xbi 뒤, hwm 앞).

2026-11-04 채팅 승인 (pt97u). 미국 전일 정규장 종가(Yahoo 일봉), 밀린 거래일은 날짜 순서대로 판정.
  buy1  종가 <= 212 p3, 종가 > 218 이면 재무장
  buy2  종가 <= 205 p3 1번
  buy3  종가 <= 201 p3 1번 ($200 근처)
  stop  종가 < 200  p4 1번 (관망 전환)
같은 실행에 여러 개가 켜지면 가장 심한 것 하나만 보내고 나머지는 본문에 적는다.
본문: 미 10년물 최근 종가(xlu_monitor.ten_series: Yahoo ^TNX, 실패 시 FRED DGS10),
  < 4.6% 매수 상향 신호, > 5.2% 관망 전환 신호, 그 밖 중립. 같은 날 XBI 가 구간 안이면 한 줄.
조회 실패는 cwally-ops 하루 1번. 2027-02-26 23:59 KST 뒤에는 아무것도 안 함.
modes: auto | dry (발송 없음, state 안 씀) | selftest (가짜 값, 네트워크 없음)
"""
from __future__ import annotations

import json
import sys
import traceback
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic, route_topic  # noqa: E402
from xlu_monitor import ten_series  # noqa: E402  (10년물 소스 재사용)

KST = timezone(timedelta(hours=9))
ET = ZoneInfo("America/New_York")
CFG_PATH = ROOT / "configs" / "ibb.json"
STATE_PATH = ROOT / "state" / "ibb.json"
XBI_CFG = ROOT / "configs" / "xbi.json"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
ENDS_AT = datetime(2027, 2, 26, 23, 59, tzinfo=KST)
SEVERITY = ["stop", "buy3", "buy2", "buy1"]  # 앞이 더 심함
FAIL_TITLE = "[IBB] iShares 바이오 시세 조회 실패 | 원문 확인 필요"
NAMES = {"buy1": "1회차 분할매수 ($212 이하)", "buy2": "2회차 분할매수 ($205 이하)",
         "buy3": "3회차 분할매수 ($200 근처, $201 이하)", "stop": "$200 이탈, 관망 전환"}


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def closes(url: str, now: datetime) -> list[tuple[str, float]]:
    """완료된 미국 정규장 일봉 종가만 (당일 16:15 ET 전 진행 봉 제외)."""
    d = json.loads(fetch(url))
    res = ((d.get("chart") or {}).get("result") or [None])[0] or {}
    et_now = now.astimezone(ET)
    out = {}
    for t, c in zip(res.get("timestamp") or [], (((res.get("indicators") or {}).get("quote") or [{}])[0]).get("close") or []):
        if c is None:
            continue
        day = datetime.fromtimestamp(t, ET).date()
        if day < et_now.date() or (day == et_now.date() and (et_now.hour, et_now.minute) >= (16, 15)):
            out[day.isoformat()] = round(float(c), 2)
    return sorted(out.items())


# ---------- 판정 ----------
def hit(cfg: dict, key: str, p: float) -> bool:
    ln = cfg["lines"][key]
    return p < ln["lt"] if "lt" in ln else p <= ln["le"]


def judge(cfg: dict, armed: dict, day: str, p: float) -> list[tuple[str, str, float]]:
    fired = []
    for key in SEVERITY:
        ln = cfg["lines"][key]
        armed.setdefault(key, True)
        if hit(cfg, key, p):
            if armed[key]:
                armed[key] = False
                fired.append((key, day, p))
        elif not ln.get("once") and not armed[key] and "rearm_gt" in ln and p > ln["rearm_gt"]:
            armed[key] = True
    return fired


def ten_label(v: float, cfg: dict) -> str:
    t = cfg["ten_year"]
    if v < t["buy_up_below"]:
        return "매수 상향 신호"
    if v > t["wait_above"]:
        return "관망 전환 신호"
    return "중립"


def ten_line(ten: tuple[str, float, str] | None, cfg: dict) -> str:
    if not ten:
        return "미 10년물: 이번 실행에서 못 읽음. 직접 확인 (판정은 종가만으로 함)."
    d, v, src = ten
    return f"미 10년물: {d} 종가 {v:.2f}% ({src}) -> {ten_label(v, cfg)} (4.6% 미만 매수 상향, 5.2% 초과 관망 전환)"


def xbi_line(day: str, xbi_close: float | None) -> str | None:
    if xbi_close is None:
        return None
    try:
        zs = json.loads(XBI_CFG.read_text(encoding="utf-8"))["zones"]
    except Exception:  # noqa: BLE001
        return None
    for z in zs:
        if xbi_close >= z["lo"] and (z["hi"] is None or xbi_close <= z["hi"]):
            nm = "추격 금지선 위" if z["key"] == "nochase" else f"{z['key'][1]}차 분할 매수 구간"
            return f"XBI 겹침: 같은 날 {day} XBI 종가 ${xbi_close:.2f}, {nm}. [XBI] 알림은 따로 옴, 바이오 합산 비중 안에서 나누기."
    return None


def message(cfg: dict, fired: list[tuple[str, str, float]], ten, xbi: str | None, stamp: str) -> tuple[str, str, int]:
    fired = sorted(fired, key=lambda f: (SEVERITY.index(f[0]), f[1]))
    key, day, p = fired[0]
    title = {"buy1": "[IBB] iShares 바이오 $212 이하 | 1회차 분할매수",
             "buy2": "[IBB] iShares 바이오 $205 이하 | 2회차 분할매수",
             "buy3": "[IBB] iShares 바이오 $200 근처 | 3회차 분할매수",
             "stop": "[IBB] iShares 바이오 $200 이탈 | 관망 전환"}[key]
    if key == "stop":
        action = "새 분할매수 멈추고 관망. 남은 지정가 주문은 취소 검토."
        later = "종가가 $200 위로 돌아오고 10년물이 안정되면 다시 판단."
    else:
        n = {"buy1": "1", "buy2": "2", "buy3": "3"}[key]
        action = f"계획한 {n}회차 지정가만 검토. 한 번에 다 사지 않기."
        later = "다음 가격선: 1회차 $212, 2회차 $205, 3회차 $201, $200 아래 종가면 관망 전환."
    others = [f"{d} ${q:.2f} {NAMES[k]}" for k, d, q in fired[1:]]
    lines = [f"{stamp} | IBB | iShares 바이오테크 ETF 분할 매수 가격선",
             "이게 뭐 알림: IBB 미국 정규장 종가가 미리 정한 가격선에 닿았다는 알림. 매매 지시가 아닙니다.",
             f"지금 결과/상태: {day} 종가 ${p:.2f}, {NAMES[key]}."]
    if others:
        lines.append("같이 켜진 조건: " + ", ".join(others) + ".")
    lines.append(ten_line(ten, cfg))
    if xbi:
        lines.append(xbi)
    lines += [f"지금 할 일: {action}",
              f"계획: {cfg['plan_note']}",
              f"나중에: {later} 1회차는 종가 $218 초과 뒤 다시 무장, 2회차 3회차 관망 전환은 1번만.",
              "익절/줄일 때: 이 알림은 매수 가격선만 봅니다. 줄이는 판단은 따로.",
              "하지 말 것: 시장가 추격, 계획 비중(2억원의 3~5%) 넘기기, 10년물 관망 전환 신호 때 남은 금액 한꺼번에 넣기."]
    return title, "\n".join(lines), cfg["lines"][key]["priority"]


def fail_msg(stamp: str, status: str) -> tuple[str, str, int]:
    return FAIL_TITLE, "\n".join([f"{stamp} | IBB | 데이터 수집 안내", "이게 뭐 알림: IBB 알림 데이터 조회 안내. 매매 신호 아님.",
                                  f"지금 결과/상태: {status}", "지금 할 일: 필요하면 IBB 시세를 직접 보세요.",
                                  "나중에: 다음 평일 09:00 에 밀린 날짜까지 다시 봅니다.", "익절/줄일 때: 이 안내로 비중을 바꾸지 않기.",
                                  "하지 말 것: 조회 실패를 조건 미충족으로 읽기."]), 3


def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def check(t: str, m: str) -> None:
    assert "\u00b7" not in t + m and "\u2014" not in t + m, t


def run(mode: str, now: datetime, cfg: dict) -> int:
    dry = mode == "dry"
    if now > ENDS_AT:
        print("ended (ends_kst passed): no-op")
        return 0
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    st = load_state()
    armed = st.setdefault("armed", {k: True for k in SEVERITY})
    out: list[tuple[str, str, int]] = []
    try:
        ser = closes(cfg["sources"]["quote"], now)
        err = None if ser else "종가 없음"
    except Exception as exc:  # noqa: BLE001
        ser, err = [], str(exc)[:150]
    if ser:
        if not st.get("last_date"):
            st["last_date"], st["last_close"] = ser[-1][0], list(ser[-1])
            for k in SEVERITY:
                armed[k] = not hit(cfg, k, ser[-1][1])
            print(f"seeded at {ser[-1]}")
        pending = [x for x in ser if x[0] > st["last_date"]]
        fired = []
        for day, p in pending:
            fired += judge(cfg, armed, day, p)
            print(f"judge {day} ${p:.2f} armed={armed}")
        if pending:
            st["last_date"], st["last_close"] = pending[-1][0], list(pending[-1])
        if fired:
            ten = None
            try:
                ts, src, errs = ten_series(now)
                if ts:
                    ten = (ts[-1][0], ts[-1][1], src)
                else:
                    print(f"10y fail: {errs}")
            except Exception as exc:  # noqa: BLE001
                print(f"10y fail: {exc}")
            top_day = sorted(fired, key=lambda f: SEVERITY.index(f[0]))[0][1]
            xl = None
            try:
                xc = dict(closes(cfg["sources"]["xbi_quote"], now)).get(top_day)
                xl = xbi_line(top_day, xc)
            except Exception as exc:  # noqa: BLE001
                print(f"xbi read fail: {exc}")
            out.append(message(cfg, fired, ten, xl, stamp))
        print(f"last {st['last_date']} pending={len(pending)} fired={[f[0] for f in fired]}")
    elif st.get("quote_fail") != stamp[:10]:
        out.append(fail_msg(stamp, f"Yahoo 일봉 못 읽음 ({err}). 이번 실행 판정 못 함."))
        st["quote_fail"] = stamp[:10]
    topic, server = resolve_topic("port", cfg.get("ntfy_topic")), resolve_server(cfg.get("ntfy_server"))
    for t, m, p in out:
        check(t, m)
        print(f"TITLE: {t}\nPRIORITY: {p} TOPIC: {route_topic(topic, t)}\n{m}\n")
        if not dry:
            publish(topic, t, m, p, ["dna"], server)
    if not out:
        print("no alert")
    if dry:
        print("DRY RUN: no publish, state not written")
        return 0
    st["updated_at"] = now.isoformat()
    STATE_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


def selftest(cfg: dict) -> int:
    stamp = "2026-11-05 09:00 KST"
    a = {k: True for k in SEVERITY}
    seq = [(215.47, []), (212.0, ["buy1"]), (211.0, []), (217.9, []), (210.0, []), (218.5, []), (211.5, ["buy1"]),
           (204.0, ["buy2"]), (205.0, []), (213.0, []), (219.0, []), (200.5, ["buy3", "buy1"]), (199.9, ["stop"]),
           (198.0, []), (230.0, []), (201.0, ["buy1"])]
    for p, want in seq:
        got = [f[0] for f in judge(cfg, a, "d", p)]
        assert sorted(got) == sorted(want), (p, got, want, a)
    print("line seq ok")
    a = {k: True for k in SEVERITY}
    f = judge(cfg, a, "2026-11-04", 199.0)
    assert [x[0] for x in f] == ["stop", "buy3", "buy2", "buy1"], f
    for tv, lab in ((4.5, "매수 상향 신호"), (4.95, "중립"), (5.3, "관망 전환 신호")):
        t, m, pr = message(cfg, f, ("2026-11-04", tv, "Yahoo ^TNX"), "XBI 겹침: 테스트", stamp)
        check(t, m)
        assert t == "[IBB] iShares 바이오 $200 이탈 | 관망 전환" and pr == 4 and lab in m and "같이 켜진 조건" in m and "XBI 겹침" in m, m
        assert route_topic("cwally-port", t) == "cwally-port" and "조회 실패" not in t
    print(f"-- sample\nTITLE: {t}\n{m}\n")
    for k, pv in (("buy1", 211.0), ("buy2", 204.0), ("buy3", 200.8)):
        t, m, pr = message(cfg, [(k, "2026-11-04", pv)], None, None, stamp)
        check(t, m)
        assert pr == 3 and "2억원의 3~5%" in m and "10년물" in m and route_topic("cwally-port", t) == "cwally-port", t
        print(f"TITLE: {t}")
    t, m, _ = fail_msg(stamp, "x")
    check(t, m)
    assert route_topic("cwally-port", t) == "cwally-ops", t
    assert xbi_line("2026-11-04", 149.0) and xbi_line("2026-11-04", 155.0) is None
    print("SELFTEST OK: fixtures only, no network, no publish, no state")
    return 0


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    if mode == "selftest":
        return selftest(cfg)
    if mode not in {"auto", "dry"}:
        print(f"unknown mode {mode}")
        return 2
    now = datetime.now(KST)
    try:
        return run(mode, now, cfg)
    except Exception as exc:  # noqa: BLE001
        print(traceback.format_exc(), file=sys.stderr)
        if mode == "dry":
            return 1
        st = load_state()
        day = now.strftime("%Y-%m-%d")
        if st.get("quote_fail") == day:
            return 0
        try:
            t, m, p = fail_msg(now.strftime("%Y-%m-%d %H:%M KST"), f"스크립트 오류 {type(exc).__name__}: {str(exc)[:200]}")
            publish(resolve_topic("ops"), t, m, p, ["warning"], resolve_server(cfg.get("ntfy_server")))
            st["quote_fail"] = day
            STATE_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        except Exception:  # noqa: BLE001
            return 1
        return 0


if __name__ == "__main__":
    sys.exit(main())
