#!/usr/bin/env python3
"""[487240] KODEX AI전력핵심설비 관심 종목 확정 종가 선 알림 (2026-11-03 사용자 요청, 미보유, 3회 분할 계획).

평일 15:40 KST (0090b0 슬롯에 부착). 네이버 fchart 일봉 정규장 종가(당일 행은 15:31 이후). 데이터 거래일로 중복 방지.
선(모두 상태가 바뀔 때만 1번, 재무장 완충):
  l25000 : 종가 <= 25,000 -> p3 1차 매수 검토 (25,800 이상에서 재무장)
  l22000 : 종가 <= 22,000 -> p3 2차 매수 검토 (22,700 이상)
  l20700 : 종가 <  20,700 -> p4 추가 매수 중단선 (21,400 이상). 경고라 매수 권유 문구 없음.
  ma20   : 종가가 그날 20일선 위 2거래일 연속 -> p3 3차 매수 검토. 20일선 아래 종가 뒤 재무장.
같은 실행에서 걸린 선은 한 건으로 묶음(중단선이 같이 걸리면 매수 검토 문구는 빼고 경고만). 첫 실행은 조용히 시드.
종가 조회 실패는 하루 1번 cwally-ops. ends_kst 뒤 조용히 종료.
usage: k487240_monitor.py <auto|dry> | selftest | confirm
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
NAME = "k487240"
CFG_PATH = ROOT / "configs" / f"{NAME}.json"
STATE_PATH = ROOT / "state" / f"{NAME}.json"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
TAG = "[487240]"
BUY_WORDS = ("매수 검토", "분할 매수", "분할매수", "매수 기회", "저가 매수", "사세요", "담을", "물타기 하", "비중 확대", "지금 매수", "1차 금액", "2차 금액", "3차 금액")


def won(v) -> str:
    return f"{v:,.0f}" if v is not None else "?"


def fetch_rows(cfg: dict) -> list[tuple[str, float]]:
    with urllib.request.urlopen(urllib.request.Request(cfg["sources"]["price"], headers=UA), timeout=30) as r:
        raw = r.read().decode("euc-kr", "ignore")
    return sorted((f"{d[:4]}-{d[4:6]}-{d[6:]}", float(c)) for d, c in re.findall(r'item data="(\d{8})\|[^|]*\|[^|]*\|[^|]*\|([\d.]+)\|', raw))


def confirmed(rows: list[tuple[str, float]], now: datetime) -> list[tuple[str, float]]:
    today = now.strftime("%Y-%m-%d")
    return [r for r in rows if r[0] < today or (r[0] == today and (now.hour, now.minute) >= (15, 31))]


OPS = {"le": lambda v, x: v <= x, "lt": lambda v, x: v < x, "ge": lambda v, x: v >= x, "gt": lambda v, x: v > x}


def ma_at(closes: list[float], i: int, n: int) -> float | None:
    return sum(closes[i - n + 1:i + 1]) / n if i + 1 >= n else None


def step(cfg: dict, armed: dict, closes: list[float], i: int) -> list[dict]:
    """Judge day i. New keys seed silently (armed only when not already hit). Returns fired line dicts."""
    p = closes[i]
    fired = []
    for ln in cfg["lines"]:
        k = ln["key"]
        hit = OPS[ln["op"]](p, ln["level"])
        if k not in armed:
            armed[k] = not hit
        elif armed[k] and hit:
            armed[k] = False
            fired.append(ln)
        elif not armed[k] and OPS[ln["rearm_op"]](p, ln["rearm"]):
            armed[k] = True
    m = cfg["ma"]
    n, need = m["days"], m["consecutive"]
    above = [closes[j] > ma_at(closes, j, n) for j in range(i - need + 1, i + 1) if j >= 0 and ma_at(closes, j, n) is not None]
    hit = len(above) == need and all(above)
    below = ma_at(closes, i, n) is not None and p < ma_at(closes, i, n)
    k = m["key"]
    if k not in armed:
        armed[k] = not hit
    elif armed[k] and hit:
        armed[k] = False
        fired.append({"key": k, "title": m["title"], "priority": m["priority"], "warn": False, "level": ma_at(closes, i, n)})
    elif not armed[k] and below:
        armed[k] = True
    return fired


def checklist(cfg: dict, key: str) -> str:
    return ", ".join(f"({i}) {x}" for i, x in enumerate(cfg["checklists"].get(key, []), 1))


def message(cfg: dict, fired: list[dict], day: str, p: float, ma20: float | None, ma200: float | None, stamp: str) -> tuple[str, str, int]:
    fired = sorted(fired, key=lambda x: -x["priority"])
    warn = any(x["warn"] for x in fired)
    shown = [x for x in fired if x["warn"]] if warn else fired
    title = f"{TAG} 487240 종가 {won(p)} | " + " + ".join(x["title"] for x in shown)
    g20 = f"{(p / ma20 - 1) * 100:+.1f}%" if ma20 else "?"
    g200 = f"{(p / ma200 - 1) * 100:+.1f}%" if ma200 else "?"
    status = (f"{day} 확정 종가 {won(p)}원(네이버 일봉). 20일선 {won(ma20)}원({g20}), 200일선 {won(ma200)}원({g200}, 재평가 가격). "
              f"52주 저점 21,799원(장중). 걸린 선: {', '.join(x['title'] for x in shown)}.")
    plan = cfg["plan"]
    if warn:
        if len(fired) > 1:
            status += f" 같은 날 위쪽 선({', '.join(won(x['level']) for x in fired if not x['warn'] and x.get('level'))})도 지났지만 중단선 아래라 보류로 봅니다."
        action = ("추가 매수 중단. 52주 저점(21,799원) 아래 5% 까지 밀렸다는 뜻이라 AI 전력 설비 논거(상위 편입 LS ELECTRIC, 효성중공업, "
                  "HD현대일렉트릭 수주와 실적)를 다시 점검하세요. 이미 산 분량이 있으면 계좌 비중만 확인.")
        later = f"21,400원 이상 종가로 회복해야 이 선이 다시 무장됩니다. 그 전까지 다른 선 알림이 와도 이 중단선 기준이 우선. 종료 {cfg['ends_kst'][:10]}."
        trim = f"산 분량이 있으면 비중이 계획 상한 {plan['max_weight_pct']}% 를 넘는지만 보고, 이 알림 하나로 전량 매도하지 않기."
        avoid = "물타기, 신용, 미수, 저점 이탈을 바닥 신호로 읽기."
    else:
        todo = []
        for x in fired:
            todo.append(f"{x['title']}: {checklist(cfg, x['key'])}.")
        action = " ".join(todo) + f" 합계 {plan['weight_pct']}(약 600만~1,000만원), 최대 {plan['max_weight_pct']}%, {plan['account']}. 지정가로 나눠서."
        later = (f"같은 선은 재무장 뒤에만 다시 옵니다(25,000 선은 25,800 이상, 22,000 선은 22,700 이상, 20일선은 20일선 아래 종가 뒤). "
                 f"20,700 미만 종가면 추가 매수 중단선 p4. 종료 {cfg['ends_kst'][:10]}.")
        trim = f"아직 보유 없음. 산 뒤에는 200일선({won(ma200)}원) 근처가 재평가 가격, 비중이 {plan['max_weight_pct']}% 를 넘으면 넘은 만큼 줄이기."
        avoid = "한 번에 다 사기, 신용, 미수, 하루 반등을 추세 전환으로 확정하기."
    body = "\n".join([
        f"이게 뭐 알림: KODEX AI전력핵심설비(487240) 관심 종목(미보유) 확정 종가가 미리 정한 선에 닿았다는 알림. {'경고.' if warn else '3회 분할 계획의 검토 시점.'} 매매 지시 아님.",
        f"{stamp} | 487240 | KODEX AI전력핵심설비",
        f"지금 결과/상태: {status}",
        f"지금 할 일: {action}",
        f"나중에: {later}",
        f"익절/줄일 때: {trim}",
        f"하지 말 것: {avoid}",
    ])
    return title, body, max(x["priority"] for x in fired)


def run(mode: str, now: datetime, cfg: dict, rows_fn=None, state: dict | None = None) -> tuple[list, dict]:
    dry = mode == "dry"
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    st = state if state is not None else (json.loads(STATE_PATH.read_text(encoding="utf-8")) if STATE_PATH.exists() else {})
    out = []
    try:
        rows = confirmed((rows_fn or (lambda: fetch_rows(cfg)))(), now)
        err = None if len(rows) >= cfg["ma"]["days"] + 1 else f"확정 종가 {len(rows)}개뿐"
    except Exception as exc:  # noqa: BLE001
        rows, err = [], str(exc)[:150]
    if err:
        print(f"fetch fail: {err}")
        if st.get("fail_day") != stamp[:10]:
            st["fail_day"] = stamp[:10]
            body = "\n".join([
                "이게 뭐 알림: 487240 알림의 종가 조회 실패 안내. 매매 신호 아님.",
                f"{stamp} | 487240 | KODEX AI전력핵심설비",
                f"지금 결과/상태: 네이버 일봉 종가 확인 불가 ({err}).",
                "지금 할 일: 필요하면 네이버 증권에서 487240 종가를 직접 보세요.",
                "나중에: 다음 평일 15:40 에 다시 봅니다.",
                "익절/줄일 때: 이 안내로 판단을 바꾸지 않기.",
                "하지 말 것: 조회 실패를 조건 미충족으로 읽기.",
            ])
            out.append((f"{TAG} 487240 종가 조회 실패 | 판정 보류", body, 3))
    else:
        days = [d for d, _ in rows]
        closes = [c for _, c in rows]
        armed = st.setdefault("armed", {})
        last = st.get("last_date")
        new = [i for i, d in enumerate(days) if last is None and i == len(days) - 1 or last is not None and d > last]
        fired: list[dict] = []
        for i in new:
            for f in step(cfg, armed, closes, i):
                fired = [x for x in fired if x["key"] != f["key"]] + [f]
        day, p = rows[-1]
        n = len(closes) - 1
        ma20, ma200 = ma_at(closes, n, cfg["ma"]["days"]), ma_at(closes, n, cfg["ma"]["reval_days"])
        if fired and last is not None:
            out.append(message(cfg, fired, day, p, ma20, ma200, stamp))
        st.update(last_date=day, last_close=[day, p], ma20=round(ma20, 2) if ma20 else None, ma200=round(ma200, 2) if ma200 else None)
        print(f"close {day} {won(p)} ma20 {won(ma20)} ma200 {won(ma200)} new_days={len(new)} armed={armed}")
    st["updated_at"] = stamp
    if state is not None:
        return out, st
    for t, m, pri in out:
        print(t)
        print(m)
        if not dry:
            publish(resolve_topic("port", cfg.get("ntfy_topic")), t, m, pri, ["warning"] if pri >= 4 else ["chart_with_upwards_trend"],
                    resolve_server(cfg.get("ntfy_server")), config_name=NAME)
            st["last_sent"] = {"at": stamp, "title": t}
    if not out:
        print("no alert")
    if dry:
        print("DRY RUN: no publish, state not written")
    else:
        STATE_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return out, st


def selftest(cfg: dict) -> int:
    fails = 0

    def check(label, got, want):
        nonlocal fails
        ok = got == want
        fails += not ok
        print(("ok  " if ok else "FAIL"), label, "" if ok else f"got {got!r} want {want!r}")

    base = [30000.0] * 20  # MA20 30,000
    # seed at 25,075 (below MA20, above 25,000): silent, all armed
    armed: dict = {}
    check("seed silent", step(cfg, armed, base + [25075.0], 20), [])
    check("seed armed all", armed, {"l25000": True, "l22000": True, "l20700": True, "ma20": True})
    a2: dict = {}
    step(cfg, a2, base + [24900.0], 20)
    check("seed already <=25,000 -> l25000 disarmed (no fire on seed)", a2["l25000"], False)
    # walk for l25000 with buffer
    seq = base + [25075.0, 25000.0, 24500.0, 25700.0, 24900.0, 25800.0, 24990.0]
    got = []
    a = dict(armed)
    for i in range(21, len(seq)):
        got.append([x["key"] for x in step(cfg, a, seq, i)])
    check("l25000 fires at 25,000, no refire until >=25,800", got, [["l25000"], [], [], [], [], ["l25000"]])
    a = {"l25000": False, "l22000": True, "l20700": True, "ma20": True}
    seq = base + [22000.0, 20700.0, 20699.0, 21399.0, 20000.0, 21400.0, 20600.0]
    got = [[x["key"] for x in step(cfg, a, seq, i)] for i in range(20, len(seq))]
    check("22,000 and <20,700 (strict) with re-arm 21,400", got, [["l22000"], [], ["l20700"], [], [], [], ["l20700"]])
    # MA20 two consecutive closes above
    a = {"l25000": False, "l22000": False, "l20700": True, "ma20": True}
    seq = [26000.0] * 20 + [27000.0, 26900.0, 27500.0, 25100.0, 27000.0, 27100.0]
    got = [[x["key"] for x in step(cfg, a, seq, i)] for i in range(20, len(seq))]
    check("ma20 needs 2 days above, re-arm after a close below", got, [[], ["ma20"], [], [], [], ["ma20"]])
    # messages
    l = {x["key"]: x for x in cfg["lines"]}
    t1, b1, p1 = message(cfg, [l["l25000"]], "2026-11-04", 24950, 28600, 36300, "s")
    t2, b2, p2 = message(cfg, [l["l20700"]], "2026-12-01", 20600, 24000, 35000, "s")
    t3, b3, p3 = message(cfg, [l["l22000"], l["l20700"], l["l25000"]], "2026-12-01", 20500, 24000, 35000, "s")
    t4, b4, p4 = message(cfg, [{"key": "ma20", "title": cfg["ma"]["title"], "priority": 3, "warn": False}], "2027-01-05", 29000, 28000, 34000, "s")
    check("l25000 title/pri", (t1, p1), ("[487240] 487240 종가 24,950 | 1차 매수 검토", 3))
    check("l25000 checklist", all(k in b1 for k in ("중간선거", "Eaton", "산일전기", "200만~330만원", "일반계좌")), True)
    check("l22000 alt trigger", "효성중공업 4분기 이연매출" in message(cfg, [l["l22000"]], "d", 21900, 24000, 35000, "s")[1], True)
    check("ma20 mentions 200일선 재평가", "200일선" in b4 and "재평가" in b4 and t4.endswith("3차 매수 검토 (20일선 회복)"), True)
    check("stop title/pri", (t2, p2), ("[487240] 487240 종가 20,600 | 추가 매수 중단선 (52주 저점 -5%)", 4))
    check("combo with stop shows only warning", (t3, p3), ("[487240] 487240 종가 20,500 | 추가 매수 중단선 (52주 저점 -5%)", 4))
    check("no buy wording in warning", [w for w in BUY_WORDS if w in b2 + b3], [])
    allt = t1 + b1 + t2 + b2 + t3 + b3 + t4 + b4
    check("no dot/dash", any(ch in allt for ch in "\u00b7\u2014"), False)
    check("six parts", [x.split(":")[0] for x in b1.split("\n")[2:]], ["지금 결과/상태", "지금 할 일", "나중에", "익절/줄일 때", "하지 말 것"])
    check("first line", all(b.startswith("이게 뭐 알림:") for b in (b1, b2, b3, b4)), True)
    # run: seed silent, then a new day fires once, re-run same day silent, today's bar before 15:31 ignored
    rows = [(f"2026-10-{i + 1:02d}", 30000.0) for i in range(20)] + [("2026-11-02", 26480.0), ("2026-11-03", 25075.0)]
    out, st = run("dry", datetime(2026, 11, 3, 15, 40, tzinfo=KST), cfg, lambda: rows, {})
    check("run seed silent", (out, st["armed"]["l25000"]), ([], True))
    rows2 = rows + [("2026-11-04", 24800.0)]
    out, st = run("dry", datetime(2026, 11, 4, 15, 20, tzinfo=KST), cfg, lambda: rows2, st)
    check("today before 15:31 ignored", out, [])
    out, st = run("dry", datetime(2026, 11, 4, 15, 40, tzinfo=KST), cfg, lambda: rows2, st)
    check("next day fires l25000", [o[0] for o in out], ["[487240] 487240 종가 24,800 | 1차 매수 검토"])
    out, st = run("dry", datetime(2026, 11, 4, 15, 45, tzinfo=KST), cfg, lambda: rows2, st)
    check("same day re-run silent", out, [])
    print("selftest", "PASS" if not fails else f"FAIL {fails}")
    return 1 if fails else 0


def confirm(cfg: dict) -> int:
    st = json.loads(STATE_PATH.read_text(encoding="utf-8")) if STATE_PATH.exists() else {}
    lc = st.get("last_close") or ["기록 없음", 0]
    body = "\n".join([
        "이게 뭐 알림: KODEX AI전력핵심설비(487240) 관심 종목 확정 종가 선 알림 설치 확인입니다. 매매 신호 아님.",
        f"{datetime.now(KST).strftime('%Y-%m-%d %H:%M KST')} | 487240 | KODEX AI전력핵심설비",
        f"지금 결과/상태: 최근 확정 종가 {lc[0]} {won(lc[1])}원, 20일선 {won(st.get('ma20'))}원, 200일선 {won(st.get('ma200'))}원. 모든 선 무장, 지금은 발송 대상 없음.",
        "지금 할 일: 없음. 평일 15:40 KST 확정 종가로 25,000 이하(p3), 22,000 이하(p3), 20,700 미만 추가 매수 중단선(p4), "
        "20일선 위 2일 연속(p3)을 상태가 바뀔 때만 봅니다.",
        f"나중에: 3회 분할, 합계 3~5%(약 600만~1,000만원), 최대 7%, 일반계좌. 종료 {cfg['ends_kst'][:10]}.",
        "익절/줄일 때: 해당 없음(미보유).",
        "하지 말 것: 이 설치 확인을 매매 신호로 읽기.",
    ])
    title = f"{TAG} 487240 KODEX AI전력핵심설비 설치 확인 | 매매 신호 아님"
    print(title)
    print(body)
    publish(resolve_topic("port", cfg.get("ntfy_topic")), title, body, 3, ["white_check_mark"],
            resolve_server(cfg.get("ntfy_server")), config_name=NAME)
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
        print(f"{NAME} ended {ends} KST; silent")
        return 0
    if now.weekday() >= 5 and mode == "auto":
        print("weekend; silent")
        return 0
    run(mode, now, cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
