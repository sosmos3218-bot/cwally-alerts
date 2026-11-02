#!/usr/bin/env python3
"""[227540] TIGER 200 헬스케어 확정 종가 선 알림 (2026-11-02 승인, 셀트리온 개별주 대체 보유).

평일 15:40 KST (0090b0 슬롯에 부착). 네이버 수정주가 일봉(fchart requestType=0) 확정 종가, 데이터 거래일로 중복 방지.
선(모두 상태가 바뀔 때만 1번):
  low   : 종가 <= 16,900원(1년 저점) -> p4 경고. 16,900 + 3% 위 종가로 회복해야 다시 무장.
  ma200 : 종가 >= 200일 이동평균(매번 계산) -> p3 추세 회복(회복 첫날). 200일선 -3% 아래 종가로 내려가야 다시 무장.
  avg10, avg20 : config avg_price 가 있을 때만. 평균가 대비 -10%, -20% 이하 -> p4. 선 + 3% 위 회복 뒤 다시 무장.
같은 날 여러 선이 걸리면 한 건으로 묶음. 첫 실행(또는 새 선이 처음 켜질 때)은 조용히 시드.
종가 조회 실패는 하루 1번 cwally-ops (제목에 '조회 실패'). ends_kst 뒤에는 조회 없이 조용히 끝남.
usage: k227540_monitor.py <auto|dry> | selftest | confirm
"""
from __future__ import annotations

import json
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
CFG_PATH = ROOT / "configs" / "k227540.json"
STATE_PATH = ROOT / "state" / "k227540.json"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
TAG = "[227540]"
BAD_WORDS = ("분할매수", "분할 매수", "매수 기회", "매수 구간", "저가 매수", "사세요", "진입 검토", "담을", "물타기 하세요",
             "비중 확대", "지금 매수", "매수 검토", "추가 매수 하")


def won(v: float) -> str:
    return f"{v:,.0f}"


def fetch_rows(code: str, count: int) -> list[tuple[str, float]]:
    url = f"https://fchart.stock.naver.com/sise.nhn?symbol={code}&timeframe=day&count={count}&requestType=0"
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as resp:
        raw = resp.read().decode("euc-kr", errors="ignore")
    rows = [(f"{d[:4]}-{d[4:6]}-{d[6:]}", float(c))
            for d, c in re.findall(r'item data="(\d{8})\|[^|]*\|[^|]*\|[^|]*\|([\d.]+)\|', raw)]
    return sorted(rows)


def confirmed(rows: list[tuple[str, float]], now: datetime) -> list[tuple[str, float]]:
    today = now.strftime("%Y-%m-%d")
    return [r for r in rows if r[0] < today or (r[0] == today and (now.hour, now.minute) >= (15, 31))]


def lines_for(cfg: dict, ma: float | None) -> list[dict]:
    """Each line: key, kind (down: fire when close <= level, up: fire when close >= level), level, rearm level, priority."""
    out = []
    lo = float(cfg["low_line"])
    out.append({"key": "low", "kind": "down", "level": lo, "rearm": lo * (1 + cfg["low_rearm_pct"] / 100),
                "pri": int(cfg["low_priority"])})
    if ma:
        out.append({"key": "ma200", "kind": "up", "level": ma, "rearm": ma * (1 - cfg["ma_rearm_pct"] / 100),
                    "pri": int(cfg["ma_priority"])})
    avg = cfg.get("avg_price")
    if avg:
        for pct in cfg.get("avg_lines_pct") or []:
            lv = float(avg) * (1 + float(pct) / 100)
            out.append({"key": f"avg{abs(int(pct))}", "kind": "down", "level": lv, "pct": int(pct),
                        "rearm": lv * (1 + cfg["avg_rearm_pct"] / 100), "pri": int(cfg["avg_priority"])})
    return out


def hit(ln: dict, p: float) -> bool:
    return p <= ln["level"] if ln["kind"] == "down" else p >= ln["level"]


def rearmed(ln: dict, p: float) -> bool:
    return p >= ln["rearm"] if ln["kind"] == "down" else p < ln["rearm"]


def judge(lines: list[dict], armed: dict, p: float) -> list[dict]:
    """New line keys seed silently (armed only when not already past the line). Fires once, re-arms past the buffer."""
    fired = []
    for ln in lines:
        k = ln["key"]
        if k not in armed:
            armed[k] = not hit(ln, p)
            continue
        if armed[k] and hit(ln, p):
            armed[k] = False
            fired.append(ln)
        elif not armed[k] and rearmed(ln, p):
            armed[k] = True
    return fired


def line_title(ln: dict, p: float) -> str:
    if ln["key"] == "low":
        return f"1년 저점 {won(ln['level'])} 이탈 | 추가 매수 보류, 비중 점검"
    if ln["key"] == "ma200":
        return f"200일선 {won(ln['level'])} 회복 | 추격 매수 보류, 근거 재확인"
    return f"평균가 대비 {ln['pct']}% ({won(ln['level'])}) 이탈 | 추가 매수 보류, 비중 점검"


def message(cfg: dict, fired: list[dict], day: str, p: float, ma: float | None, stamp: str) -> tuple[str, str, int]:
    fired = sorted(fired, key=lambda x: -x["pri"])
    title = f"{TAG} 종가 {won(p)} | {line_title(fired[0], p)}" + (f" 외 {len(fired) - 1}건" if len(fired) > 1 else "")
    keys = {x["key"] for x in fired}
    down = bool(keys - {"ma200"})
    gap = f" (이격 {(p / ma - 1) * 100:+.1f}%)" if ma else ""
    avg = cfg.get("avg_price")
    what = "+".join(line_title(x, p).split(" | ")[0] for x in fired)
    status = (f"{day} 확정 종가 {won(p)}원 (수정주가). 걸린 선: {what}. 200일선 {won(ma) if ma else '계산 불가'}원{gap}, "
              f"1년 저점 {won(cfg['low_line'])}원, 평균가 {won(avg) + '원' if avg else '미입력(평균가 경고선 꺼짐)'}.")
    if down:
        action = "추가 매수 보류. 포트폴리오에서 헬스케어 비중이 계획보다 커졌는지만 점검하세요. 상위 편입 종목(셀트리온, 삼성바이오로직스 등) 실적과 공시를 확인하세요."
        trim = "줄일지는 미리 정한 비중 기준으로만 정하세요. 이 알림 하나로 전량 매도하지 않기."
        avoid = "물타기, 신용, 미수, 저점 이탈을 바닥 신호로 읽기."
    else:
        action = "추격 매수 보류, 근거 재확인. 거래량이 붙었는지, 상위 편입 종목 실적, 원달러 환율, 하루 회복인지 며칠 이어지는지 보세요."
        trim = "회복으로 헬스케어 비중이 계획을 넘었으면 넘은 만큼만 줄일지 검토하세요."
        avoid = "추격 매수, 신용, 미수, 하루 회복을 추세 전환으로 확정하기."
    later = (f"같은 선은 다시 무장된 뒤에만 다시 옵니다(저점선은 {won(float(cfg['low_line']) * (1 + cfg['low_rearm_pct'] / 100))}원 위 회복, "
             f"200일선은 200일선 -{cfg['ma_rearm_pct']:g}% 아래로 내려간 뒤). 종료 {cfg['ends_kst'][:10]}.")
    body = "\n".join([
        "이게 뭐 알림: TIGER 200 헬스케어(227540) 확정 종가가 미리 정한 선을 넘었다는 알림입니다. 셀트리온 개별주 대신 보유하는 ETF. 매매 지시가 아닙니다.",
        f"{stamp} | 227540 | TIGER 200 헬스케어",
        f"지금 결과/상태: {status}",
        f"지금 할 일: {action}",
        f"나중에: {later}",
        f"익절/줄일 때: {trim}",
        f"하지 말 것: {avoid}",
    ])
    return title, body, max(x["pri"] for x in fired)


def run(mode: str, now: datetime, cfg: dict) -> int:
    dry = mode == "dry"
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    st = json.loads(STATE_PATH.read_text(encoding="utf-8")) if STATE_PATH.exists() else {}
    if now.weekday() >= 5 and mode == "auto":
        print("weekend; silent")
        return 0
    try:
        rows = confirmed(fetch_rows(cfg["code"], 260), now)
        err = None if len(rows) >= 1 else "확정 종가 없음"
    except Exception as exc:  # noqa: BLE001
        rows, err = [], str(exc)[:150]
    out = []
    if err:
        print(f"fetch fail: {err}")
        if st.get("fail_day") != stamp[:10]:
            st["fail_day"] = stamp[:10]
            body = "\n".join([
                "이게 뭐 알림: 227540 알림의 종가 조회 실패 안내. 매매 신호 아님.",
                f"{stamp} | 227540 | TIGER 200 헬스케어",
                f"지금 결과/상태: 네이버 수정주가 일봉 확인 불가 ({err}).",
                "지금 할 일: 필요하면 네이버 증권에서 227540 종가를 직접 보세요.",
                "나중에: 다음 평일 15:40 에 다시 봅니다.",
                "익절/줄일 때: 이 안내로 비중을 바꾸지 않기.",
                "하지 말 것: 조회 실패를 조건 미충족으로 읽기.",
            ])
            out.append((f"{TAG} 227540 종가 조회 실패 | 판정 보류", body, 3))
    else:
        day, p = rows[-1]
        closes = [c for _, c in rows]
        ma = sum(closes[-cfg["ma_days"]:]) / cfg["ma_days"] if len(closes) >= cfg["ma_days"] else None
        lines = lines_for(cfg, ma)
        armed = st.setdefault("armed", {})
        print(f"close {day} {won(p)} ma{cfg['ma_days']} {won(ma) if ma else None} armed(before)={dict(armed)} last={st.get('last_date')}")
        if day != st.get("last_date"):
            fired = judge(lines, armed, p)
            if fired:
                out.append(message(cfg, fired, day, p, ma, stamp))
            st["last_date"] = day
        st["last_close"] = [day, p]
        st["ma200"] = round(ma, 2) if ma else None
        st["levels"] = {ln["key"]: round(ln["level"], 2) for ln in lines}
        print(f"armed(after)={armed}")
    st["updated_at"] = stamp
    for t, m, pri in out:
        print(t)
        print(m)
        if not dry:
            publish(resolve_topic("port", cfg.get("ntfy_topic")), t, m, pri,
                    ["warning"] if pri >= 4 else ["chart_with_upwards_trend"], resolve_server(cfg.get("ntfy_server")),
                    config_name="k227540")
            st["last_sent"] = {"at": stamp, "title": t}
    if not out:
        print("no alert")
    if dry:
        print("DRY RUN: no publish, state not written")
    else:
        STATE_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


def selftest(cfg: dict) -> int:
    fails = 0

    def check(label, got, want):
        nonlocal fails
        ok = got == want
        fails += not ok
        print(("ok  " if ok else "FAIL"), label, "" if ok else f"got {got!r} want {want!r}")
    ma = 20240.0
    ln = lines_for(cfg, ma)
    check("default lines (avg off)", [x["key"] for x in ln], ["low", "ma200"])
    armed: dict = {}
    check("seed today 18,130 silent", judge(ln, armed, 18130), [])
    check("seed armed both", armed, {"low": True, "ma200": True})
    # 17,406 < 16,900*1.03 = 17,407 stays disarmed; 17,407 re-arms. 19,633 is above 20,240*0.97 = 19,632.8, 19,632 re-arms.
    walk = [17000, 16900, 16500, 17300, 17406, 17407, 16890, 17410, 16800, 20000, 20240, 20500, 19700, 19633, 19632, 20300]
    armed = {"low": True, "ma200": True}
    got = []
    for p in walk:
        got.append([x["key"] for x in judge(ln, armed, p)])
    check("walk fires", got, [[], ["low"], [], [], [], [], ["low"], [], ["low"], [], ["ma200"], [], [], [], [], ["ma200"]])
    check("ma re-arm only below -3% (19,632.8)", (rearmed(ln[1], 19633), rearmed(ln[1], 19632)), (False, True))
    check("low re-arm at +3% (17,407)", (rearmed(ln[0], 17406), rearmed(ln[0], 17407)), (False, True))
    acfg = {**cfg, "avg_price": 20000}
    la = lines_for(acfg, ma)
    check("avg lines on", [(x["key"], round(x["level"])) for x in la], [("low", 16900), ("ma200", 20240), ("avg10", 18000), ("avg20", 16000)])
    a2 = {"low": True, "ma200": True}
    check("new avg lines seed silently", judge(la, a2, 18130), [])
    check("avg10 fires", [x["key"] for x in judge(la, a2, 17900)], ["avg10"])
    a3 = {"low": True, "ma200": True, "avg10": True, "avg20": True}
    f = judge(la, a3, 15900)
    t, b, pri = message(acfg, f, "2026-12-01", 15900, ma, "2026-12-01 15:40 KST")
    check("combo one message, p4 title", (t, pri), ("[227540] 종가 15,900 | 1년 저점 16,900 이탈 | 추가 매수 보류, 비중 점검 외 2건", 4))
    t1, b1, p1 = message(cfg, [ln[0]], "2026-12-01", 16850, ma, "s")
    t2, b2, p2 = message(cfg, [ln[1]], "2026-12-01", 20300, ma, "s")
    check("low title", (t1, p1), ("[227540] 종가 16,850 | 1년 저점 16,900 이탈 | 추가 매수 보류, 비중 점검", 4))
    check("ma title", (t2, p2), ("[227540] 종가 20,300 | 200일선 20,240 회복 | 추격 매수 보류, 근거 재확인", 3))
    allt = t + b + t1 + b1 + t2 + b2
    check("first line is 이게 뭐 알림", all(x.split("\n")[0].startswith("이게 뭐 알림:") for x in (b, b1, b2)), True)
    check("six parts", [x.split(":")[0] for x in b1.split("\n")[2:]], ["지금 결과/상태", "지금 할 일", "나중에", "익절/줄일 때", "하지 말 것"])
    check("no dot/dash", any(ch in allt for ch in "\u00b7\u2014"), False)
    check("no buy wording", [w for w in BAD_WORDS if w in allt], [])
    check("ticker in titles", all("227540" in x for x in (t, t1, t2)), True)
    now = datetime(2026, 11, 2, 15, 0, tzinfo=KST)
    check("today's bar not confirmed before 15:31", confirmed([("2026-10-30", 1.0), ("2026-11-02", 2.0)], now)[-1][0], "2026-10-30")
    print("selftest", "PASS" if not fails else f"FAIL {fails}")
    return 1 if fails else 0


def confirm(cfg: dict) -> int:
    st = json.loads(STATE_PATH.read_text(encoding="utf-8")) if STATE_PATH.exists() else {}
    lc = st.get("last_close") or ["기록 없음", 0]
    body = "\n".join([
        "이게 뭐 알림: TIGER 200 헬스케어(227540) 확정 종가 선 알림 설치 확인입니다. 매매 신호 아님.",
        f"{datetime.now(KST).strftime('%Y-%m-%d %H:%M KST')} | 227540 | TIGER 200 헬스케어",
        f"지금 결과/상태: 최근 확정 종가 {lc[0]} {won(lc[1])}원, 200일선 {won(st['ma200']) if st.get('ma200') else '계산 전'}원. 두 선 사이라 지금은 발송 대상 없음.",
        f"지금 할 일: 없음. 평일 15:40 KST 확정 종가로 판정하고 상태가 바뀔 때만 보냅니다. 1년 저점 {won(cfg['low_line'])}원 이탈 p4, 200일선 회복 p3.",
        f"나중에: 평균가를 알려 주면 평균가 대비 -10%, -20% 경고선을 켭니다. 종료 {cfg['ends_kst'][:10]}. 셀트리온 개별주 알림은 2026-11-02 종료.",
        "익절/줄일 때: 해당 없음.",
        "하지 말 것: 이 설치 확인을 매수/매도 신호로 읽기.",
    ])
    title = f"{TAG} TIGER 200 헬스케어 설치 확인 | 매매 신호 아님"
    print(title)
    print(body)
    publish(resolve_topic("port", cfg.get("ntfy_topic")), title, body, 3, ["white_check_mark"],
            resolve_server(cfg.get("ntfy_server")), config_name="k227540")
    return 0


def main() -> int:
    args = [a.lower() for a in sys.argv[1:]]
    mode = args[0] if args else "auto"
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    if mode == "selftest":
        return selftest(cfg)
    if mode == "confirm":
        return confirm(cfg)
    if mode not in {"auto", "dry"}:
        print(__doc__)
        return 2
    now = datetime.now(KST)
    ends = cfg.get("ends_kst")
    if ends and now > datetime.strptime(ends, "%Y-%m-%d %H:%M").replace(tzinfo=KST):
        print(f"k227540 ended {ends} KST; silent")
        return 0
    return run(mode, now, cfg)


if __name__ == "__main__":
    sys.exit(main())
