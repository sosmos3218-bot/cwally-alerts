#!/usr/bin/env python3
"""[487240] KODEX AI전력핵심설비 관심 종목 확정 종가 선 알림.

평일 19:00 KST (k203780 슬롯, sntholdings 바로 뒤). 정규장 종가+거래량 scripts/krx_close.py
(Daum days, 실패 시 네이버 15:30 분봉). 당일 행은 15:40 이후만 확정. 네이버 일봉은 안 씀.
2026-11-06 t119u: 15:40 k487240 과 19:00 kr487240 을 이 하나로 합침. ends 2027-05-31.

선(상태가 바뀔 때만 1번):
  l25000  : 종가 <= 25,000 -> p3 1차 매수 검토 (25,800 이상 재무장)
  l22000  : 종가 <= 22,000 -> p3 2차 매수 검토 (22,700 이상)
  l20700  : 종가 <  20,700 -> p4 추가 매수 중단선 (21,400 이상). 경고, 매수 권유 없음.
  ma20    : 종가가 그날 20일선 위 2거래일 연속 -> p3 3차 매수 검토. 20일선 아래 종가 뒤 재무장.
  support : 종가 >= 19,000 이 3거래일 연속이고 그날 거래량 < 직전 20일 평균 -> p3 지지 확인.
            종가 < 19,000 뒤 재무장.
  low     : 종가 < 19,000 -> p4 하단 이탈. 종가 > 19,800 뒤 재무장.
  recovery: 종가 > 22,680 (2026-11-05 종가) -> p3 1번만. 재무장 안 함.
같은 실행에서 걸린 선은 한 건으로 묶음. 20,700 과 19,000 이 같이 걸리면 p4 한 통.
22,000 매수와 22,680 회복이 같이 걸리면 한 통(회복이 사다리 2차를 참고).
첫 실행은 조용히 시드. 종가 조회 실패는 하루 1번 cwally-ops.
usage: k487240_monitor.py <auto|dry> | selftest | confirm
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic  # noqa: E402
from kr_calendar import session_close_ready  # noqa: E402

KST = timezone(timedelta(hours=9))
NAME = "k487240"
CFG_PATH = ROOT / "configs" / f"{NAME}.json"
STATE_PATH = ROOT / "state" / f"{NAME}.json"
TAG = "[487240]"
CLOSE_HM = (15, 40)
BUY_WORDS = ("매수 검토", "분할 매수", "분할매수", "매수 기회", "저가 매수", "사세요", "담을", "물타기 하", "비중 확대", "지금 매수", "1차 금액", "2차 금액", "3차 금액")
OPS = {"le": lambda v, x: v <= x, "lt": lambda v, x: v < x, "ge": lambda v, x: v >= x, "gt": lambda v, x: v > x}


def won(v) -> str:
    return f"{v:,.0f}" if v is not None else "?"


def fetch_rows(cfg: dict) -> list[dict]:
    import krx_close
    rows = [{"day": b.day, "close": float(b.close), "volume": b.volume} for b in krx_close.regular_bars(cfg["code"], 260)]
    if not rows:
        raise RuntimeError("시세 0건")
    return rows


def confirmed(rows: list[dict], now: datetime) -> list[dict]:
    today = now.strftime("%Y-%m-%d")
    return [r for r in rows if session_close_ready(r["day"], today, now, CLOSE_HM)]


def ma_at(closes: list[float], i: int, n: int) -> float | None:
    return sum(closes[i - n + 1:i + 1]) / n if i + 1 >= n else None


def avg_vol(rows: list[dict], i: int, n: int) -> float | None:
    prior = rows[max(0, i - n):i]
    vols = [r.get("volume") for r in prior]
    if len(prior) < n or any(v is None for v in vols):
        return None
    return sum(vols) / n


def step_lines(cfg: dict, armed: dict, closes: list[float], i: int) -> list[dict]:
    """Judge ladder + ma20 at day i. New keys seed silently."""
    p = closes[i]
    fired = []
    for ln in cfg["lines"]:
        k = ln["key"]
        hit = OPS[ln["op"]](p, ln["level"])
        if k not in armed:
            armed[k] = not hit
        elif armed[k] and hit:
            armed[k] = False
            fired.append(dict(ln))
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


def step_struct(cfg: dict, st: dict, rows: list[dict], i: int) -> list[dict]:
    """Judge support / low / recovery at day i. Mutates st struct fields."""
    s, lo, rc = cfg["support"], cfg["low"], cfg["recovery"]
    row = rows[i]
    p, vol = row["close"], row.get("volume")
    av = avg_vol(rows, i, s["vol_days"])
    fired = []
    if p >= s["level"]:
        st["above_streak"] = int(st.get("above_streak") or 0) + 1
    else:
        st["above_streak"] = 0
        st["support_armed"] = True
    if (
        st.get("support_armed", True)
        and st["above_streak"] >= s["days"]
        and av is not None and vol is not None and vol < av
    ):
        st["support_armed"] = False
        fired.append({"key": "support", "title": s["title"], "priority": s["priority"], "warn": False,
                      "level": s["level"], "volume": vol, "avg": av, "streak": st["above_streak"]})
    if p < lo["level"] and st.get("low_armed", True):
        st["low_armed"] = False
        fired.append({"key": "low", "title": lo["title"], "priority": lo["priority"], "warn": True,
                      "level": lo["level"], "volume": vol, "avg": av})
    elif p > lo["rearm_above"] and not st.get("low_armed", True):
        st["low_armed"] = True
    if p > rc["level"] and not st.get("recovery_sent"):
        st["recovery_sent"] = row["day"]
        fired.append({"key": "recovery", "title": rc["title"], "priority": rc["priority"], "warn": False,
                      "level": rc["level"], "volume": vol, "avg": av})
    return fired


def checklist(cfg: dict, key: str, seen: set | None = None) -> str:
    items = [x for x in cfg["checklists"].get(key, []) if seen is None or x not in seen]
    if seen is not None:
        seen.update(items)
    return ", ".join(f"({i}) {x}" for i, x in enumerate(items, 1))


def vol_bit(x: dict) -> str:
    vol, av = x.get("volume"), x.get("avg")
    if vol is None or av is None or av == 0:
        return ""
    return f" 거래량 {vol:,.0f}주, 직전 20거래일 평균 {av:,.0f}주의 {vol / av:.2f}배."


def message(cfg: dict, fired: list[dict], day: str, p: float, ma20: float | None, ma200: float | None, stamp: str) -> tuple[str, str, int]:
    by = {x["key"]: x for x in fired}
    # Same-day dedupe: one p4 for 20,700 + 19,000; one p3 for 22,000 buy + 22,680 recovery
    if "l20700" in by and "low" in by:
        fired = [x for x in fired if x["key"] not in ("l20700", "low")]
        fired.append({
            "key": "floor_combo", "title": "추가 매수 중단선 + 19,000원 하단 이탈",
            "priority": 4, "warn": True, "level": by["low"]["level"],
            "volume": by["low"].get("volume"), "avg": by["low"].get("avg"),
            "also_20700": True,
        })
    if "l22000" in by and "recovery" in by:
        fired = [x for x in fired if x["key"] not in ("l22000", "recovery")]
        fired.append({
            "key": "buy_recovery", "title": "2차 매수 검토 + 22,680원 회복",
            "priority": 3, "warn": False, "level": by["recovery"]["level"],
            "volume": by["recovery"].get("volume"), "avg": by["recovery"].get("avg"),
            "combo_l22000": True,
        })
    fired = sorted(fired, key=lambda x: -x["priority"])
    warn = any(x.get("warn") for x in fired)
    shown = [x for x in fired if x.get("warn")] if warn else fired
    title = f"{TAG} 487240 종가 {won(p)} | " + " + ".join(x["title"] for x in shown)
    g20 = f"{(p / ma20 - 1) * 100:+.1f}%" if ma20 else "?"
    g200 = f"{(p / ma200 - 1) * 100:+.1f}%" if ma200 else "?"
    ctx = cfg.get("context") or ""
    status = (f"{day} 확정 종가 {won(p)}원(정규장 krx_close). 20일선 {won(ma20)}원({g20}), 200일선 {won(ma200)}원({g200}, 재평가 가격). "
              f"52주 저점 21,799원(장중). 걸린 선: {', '.join(x['title'] for x in shown)}.")
    for x in shown:
        if x["key"] in ("support", "low", "floor_combo", "recovery", "buy_recovery"):
            status += vol_bit(x)
    if ctx and any(x["key"] in ("support", "low", "floor_combo", "recovery", "buy_recovery") for x in shown):
        status += " " + ctx
    plan = cfg["plan"]
    if warn:
        if any(x["key"] == "floor_combo" for x in shown):
            status += " 20,700원 추가 매수 중단선과 19,000원 하단 이탈이 같은 날 걸려 한 통으로 보냅니다."
        elif len(fired) > 1:
            status += f" 같은 날 위쪽 선({', '.join(won(x['level']) for x in fired if not x.get('warn') and x.get('level'))})도 지났지만 중단/이탈 아래라 보류로 봅니다."
        action = ("추가 매수 중단. 52주 저점(21,799원) 아래 또는 19,000원 아래로 밀렸다는 뜻이라 AI 전력 설비 논거(상위 편입 LS ELECTRIC, 효성중공업, "
                  "HD현대일렉트릭 수주와 실적)를 다시 점검하세요. 이미 산 분량이 있으면 계좌 비중만 확인.")
        later = (f"20,700 선은 21,400원 이상, 19,000 선은 19,800원 초과 종가로 회복해야 다시 무장됩니다. "
                 f"그 전까지 다른 선 알림이 와도 이 중단/이탈 기준이 우선. 종료 {cfg['ends_kst'][:10]}.")
        trim = f"산 분량이 있으면 비중이 계획 상한 {plan['max_weight_pct']}% 를 넘는지만 보고, 이 알림 하나로 전량 매도하지 않기."
        avoid = "물타기, 신용, 미수, 저점 이탈을 바닥 신호로 읽기."
    else:
        todo = []
        seen: set = set()
        for x in shown:
            if x["key"] == "buy_recovery":
                todo.append(
                    f"2차 매수 검토 + 22,680원 회복: {checklist(cfg, 'l22000', seen)}. "
                    "같은 날 11/5 종가(22,680원)도 넘었으니 2차 사다리와 회복을 한 번에 점검."
                )
            elif x["key"] == "support":
                todo.append("19,000원 지지 확인: 3거래일 연속 지지와 거래량 평균 미만. 분할 매수 계획을 다시 점검. 한 번에 다 사지 말고 지정가로.")
            elif x["key"] == "recovery":
                todo.append("22,680원 회복: 11/5 종가 위로 회복. 보유/분할 매수 계획을 다시 점검(사다리 2차 22,000원 선과 구분).")
            elif x["key"] in cfg.get("checklists", {}) or x["key"] == "ma20":
                todo.append(f"{x['title']}: {checklist(cfg, x['key'], seen)}.")
            else:
                todo.append(f"{x['title']}.")
        action = " ".join(todo) + f" 합계 {plan['weight_pct']}(약 600만~1,000만원), 최대 {plan['max_weight_pct']}%, {plan['account']}. 지정가로 나눠서."
        later = (f"같은 선은 재무장 뒤에만 다시 옵니다(25,000->25,800, 22,000->22,700, 20일선은 아래 종가 뒤, "
                 f"19,000 지지는 19,000 미만 뒤, 19,000 이탈은 19,800 초과 뒤). 22,680 회복은 1번만. "
                 f"20,700 미만 또는 19,000 미만이면 p4. 종료 {cfg['ends_kst'][:10]}.")
        trim = f"아직 보유 없음. 산 뒤에는 200일선({won(ma200)}원) 근처가 재평가 가격, 비중이 {plan['max_weight_pct']}% 를 넘으면 넘은 만큼 줄이기."
        avoid = "한 번에 다 사기, 신용, 미수, 하루 반등을 추세 전환으로 확정하기."
    body = "\n".join([
        f"이게 뭐 알림: KODEX AI전력핵심설비(487240) 관심 종목(미보유) 확정 종가가 미리 정한 선에 닿았다는 알림. {'경고.' if warn else '3회 분할 계획과 지지/회복 점검.'} 매매 지시 아님.",
        f"{stamp} | 487240 | KODEX AI전력핵심설비",
        f"지금 결과/상태: {status}",
        f"지금 할 일: {action}",
        f"나중에: {later}",
        f"익절/줄일 때: {trim}",
        f"하지 말 것: {avoid}",
    ])
    return title, body, max(x["priority"] for x in fired)


def due_reminders(cfg: dict, st: dict, today: str) -> list[dict]:
    sent = st.setdefault("reminders_sent", {})
    out = []
    for r in cfg.get("reminders") or []:
        if r["key"] in sent or today < r["date"]:
            continue
        late = (datetime.fromisoformat(today) - datetime.fromisoformat(r["date"])).days
        if late > int(r.get("stale_days", 7)):
            sent[r["key"]] = f"skip(stale) {today}"
            continue
        out.append(r)
    return out


def reminder_message(cfg: dict, r: dict, st: dict, stamp: str) -> tuple[str, str, int]:
    lc = st.get("last_close") or ["?", None]
    ma20 = st.get("ma20")
    body = "\n".join([
        f"이게 뭐 알림: {r['what']} 매매 지시 아님.",
        f"{stamp} | 487240 298040 | KODEX AI전력핵심설비, 효성중공업",
        f"지금 결과/상태: 487240 최근 확정 종가 {lc[0]} {won(lc[1])}원, 20일선 {won(ma20)}원. 확인할 곳: {r['where']}",
        f"지금 할 일: {r['todo']}",
        f"나중에: 가격 선(25,000, 22,000, 20,700, 20일선 2일, 19,000 지지/이탈, 22,680 회복)은 그대로 상태가 바뀔 때만 옵니다. {r['rule']}",
        "익절/줄일 때: 아직 보유 없음, 해당 없음.",
        f"하지 말 것: {r['dont']}",
    ])
    return f"{TAG} 487240 {r['title']}", body, int(r.get("priority", 3))


def ensure_struct(st: dict) -> None:
    st.setdefault("support_armed", True)
    st.setdefault("above_streak", 0)
    st.setdefault("low_armed", True)
    st.setdefault("recovery_sent", None)


def run(mode: str, now: datetime, cfg: dict, rows_fn=None, state: dict | None = None) -> tuple[list, dict]:
    dry = mode == "dry"
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    st = state if state is not None else (json.loads(STATE_PATH.read_text(encoding="utf-8")) if STATE_PATH.exists() else {})
    ensure_struct(st)
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
                f"지금 결과/상태: 정규장 종가(krx_close) 확인 불가 ({err}).",
                "지금 할 일: 필요하면 Daum 금융이나 네이버 증권에서 487240 종가를 직접 보세요.",
                "나중에: 다음 평일 19:00 에 다시 봅니다.",
                "익절/줄일 때: 이 안내로 판단을 바꾸지 않기.",
                "하지 말 것: 조회 실패를 조건 미충족으로 읽기.",
            ])
            out.append((f"{TAG} 487240 종가 조회 실패 | 판정 보류", body, 3))
    else:
        days = [r["day"] for r in rows]
        closes = [r["close"] for r in rows]
        armed = st.setdefault("armed", {})
        last = st.get("last_date")
        new = [i for i, d in enumerate(days) if last is None and i == len(days) - 1 or last is not None and d > last]
        fired: list[dict] = []
        for i in new:
            for f in step_lines(cfg, armed, closes, i) + step_struct(cfg, st, rows, i):
                fired = [x for x in fired if x["key"] != f["key"]] + [f]
        day, p = rows[-1]["day"], rows[-1]["close"]
        n = len(closes) - 1
        ma20, ma200 = ma_at(closes, n, cfg["ma"]["days"]), ma_at(closes, n, cfg["ma"]["reval_days"])
        if fired and last is not None:
            out.append(message(cfg, fired, day, p, ma20, ma200, stamp))
        elif fired and last is None:
            # first run / seed day: apply structure flags from step but do not send
            pass
        st.update(
            last_date=day, last_close=[day, p], last_volume=rows[-1].get("volume"),
            ma20=round(ma20, 2) if ma20 else None, ma200=round(ma200, 2) if ma200 else None,
        )
        print(f"close {day} {won(p)} vol {rows[-1].get('volume')} ma20 {won(ma20)} ma200 {won(ma200)} "
              f"new_days={len(new)} armed={armed} support_armed={st.get('support_armed')} "
              f"above_streak={st.get('above_streak')} low_armed={st.get('low_armed')} recovery_sent={st.get('recovery_sent')}")
    for r in due_reminders(cfg, st, stamp[:10]):
        out.append(reminder_message(cfg, r, st, stamp))
        st["reminders_sent"][r["key"]] = stamp
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


def _rows(pairs: list) -> list[dict]:
    """pairs: (day, close) or (day, close, volume)."""
    out = []
    for p in pairs:
        if len(p) == 2:
            out.append({"day": p[0], "close": float(p[1]), "volume": 1000.0})
        else:
            out.append({"day": p[0], "close": float(p[1]), "volume": float(p[2])})
    return out


def selftest(cfg: dict) -> int:
    fails = 0

    def check(label, got, want):
        nonlocal fails
        ok = got == want
        fails += not ok
        print(("ok  " if ok else "FAIL"), label, "" if ok else f"got {got!r} want {want!r}")

    base_closes = [30000.0] * 20
    # seed at 25,075
    armed: dict = {}
    check("seed silent", step_lines(cfg, armed, base_closes + [25075.0], 20), [])
    check("seed armed all", armed, {"l25000": True, "l22000": True, "l20700": True, "ma20": True})
    a2: dict = {}
    step_lines(cfg, a2, base_closes + [24900.0], 20)
    check("seed already <=25,000 -> l25000 disarmed (no fire on seed)", a2["l25000"], False)
    seq = base_closes + [25075.0, 25000.0, 24500.0, 25700.0, 24900.0, 25800.0, 24990.0]
    got = []
    a = dict(armed)
    for i in range(21, len(seq)):
        got.append([x["key"] for x in step_lines(cfg, a, seq, i)])
    check("l25000 fires at 25,000, no refire until >=25,800", got, [["l25000"], [], [], [], [], ["l25000"]])
    a = {"l25000": False, "l22000": True, "l20700": True, "ma20": True}
    seq = base_closes + [22000.0, 20700.0, 20699.0, 21399.0, 20000.0, 21400.0, 20600.0]
    got = [[x["key"] for x in step_lines(cfg, a, seq, i)] for i in range(20, len(seq))]
    check("22,000 and <20,700 (strict) with re-arm 21,400", got, [["l22000"], [], ["l20700"], [], [], [], ["l20700"]])
    a = {"l25000": False, "l22000": False, "l20700": True, "ma20": True}
    seq = [26000.0] * 20 + [27000.0, 26900.0, 27500.0, 25100.0, 27000.0, 27100.0]
    got = [[x["key"] for x in step_lines(cfg, a, seq, i)] for i in range(20, len(seq))]
    check("ma20 needs 2 days above, re-arm after a close below", got, [[], ["ma20"], [], [], [], ["ma20"]])
    # structure: support needs 3 days >=19k + vol < avg; low; recovery once
    base_r = [{"day": f"2026-09-{d:02d}", "close": 21000.0, "volume": 1000.0} for d in range(1, 21)]
    st = {"support_armed": True, "above_streak": 0, "low_armed": True, "recovery_sent": None}
    rows = base_r + [
        {"day": "2026-11-09", "close": 20000.0, "volume": 2000.0},
        {"day": "2026-11-10", "close": 20100.0, "volume": 2000.0},
        {"day": "2026-11-11", "close": 20200.0, "volume": 500.0},
    ]
    got = []
    for i in range(20, len(rows)):
        got.append([x["key"] for x in step_struct(cfg, st, rows, i)])
    check("support fires on 3rd day when vol < avg", got, [[], [], ["support"]])
    st2 = {"support_armed": True, "above_streak": 3, "low_armed": True, "recovery_sent": None}
    rows2 = base_r + [
        {"day": "2026-11-09", "close": 18500.0, "volume": 500.0},
        {"day": "2026-11-10", "close": 19800.0, "volume": 500.0},
        {"day": "2026-11-11", "close": 19801.0, "volume": 500.0},
        {"day": "2026-11-12", "close": 18500.0, "volume": 500.0},
    ]
    got = [[x["key"] for x in step_struct(cfg, st2, rows2, i)] for i in range(20, len(rows2))]
    check("low fires, re-arm only above 19800, fires again", got, [["low"], [], [], ["low"]])
    st3 = {"support_armed": True, "above_streak": 0, "low_armed": True, "recovery_sent": None}
    rows3 = base_r + [
        {"day": "2026-11-09", "close": 22680.0, "volume": 2000.0},
        {"day": "2026-11-10", "close": 22700.0, "volume": 2000.0},
        {"day": "2026-11-11", "close": 23000.0, "volume": 2000.0},
    ]
    got = [[x["key"] for x in step_struct(cfg, st3, rows3, i)] for i in range(20, len(rows3))]
    check("recovery once above 22680", got, [[], ["recovery"], []])  # vol above avg so support does not also fire
    # messages
    l = {x["key"]: x for x in cfg["lines"]}
    t1, b1, p1 = message(cfg, [l["l25000"]], "2026-11-04", 24950, 28600, 36300, "s")
    t2, b2, p2 = message(cfg, [l["l20700"]], "2026-12-01", 20600, 24000, 35000, "s")
    t3, b3, p3 = message(cfg, [l["l22000"], l["l20700"], l["l25000"]], "2026-12-01", 20500, 24000, 35000, "s")
    t4, b4, p4 = message(cfg, [{"key": "ma20", "title": cfg["ma"]["title"], "priority": 3, "warn": False}], "2027-01-05", 29000, 28000, 34000, "s")
    check("l25000 title/pri", (t1, p1), ("[487240] 487240 종가 24,950 | 1차 매수 검토", 3))
    check("l25000 checklist", all(k in b1 for k in ("중간선거", "Eaton", "산일전기", "200만~330만원", "일반계좌")), True)
    for k in ("l22000", "ma20"):
        _t, _b, _p = message(cfg, [l[k] if k in l else {"key": "ma20", "title": cfg["ma"]["title"], "priority": 3, "warn": False}], "2026-11-20", 21900.0 if k == "l22000" else 30000.0, 29000.0, 36000.0, "stamp")
        check(f"{k} funding rule first", ("(1) 자금 원칙: 하이닉스와 삼성 합산 43%" in _b, "MEMEXIT 매도 자금이 있을 때만" in _b), (True, True))
    _t, _b, _p = message(cfg, [l["l25000"], l["l22000"]], "2026-11-20", 21900.0, 29000.0, 36000.0, "stamp")
    check("funding rule once when two lines fire", _b.count("자금 원칙:"), 1)
    check("stop title/pri", (t2, p2), ("[487240] 487240 종가 20,600 | 추가 매수 중단선 (52주 저점 -5%)", 4))
    check("combo with stop shows only warning", (t3, p3), ("[487240] 487240 종가 20,500 | 추가 매수 중단선 (52주 저점 -5%)", 4))
    check("no buy wording in warning", [w for w in BUY_WORDS if w in b2 + b3], [])
    # same-day floor combo and buy+recovery combo
    tc, bc, pc = message(cfg, [l["l20700"], {"key": "low", "title": cfg["low"]["title"], "priority": 4, "warn": True, "level": 19000, "volume": 500, "avg": 1000}], "2026-11-20", 18500, 24000, 35000, "s")
    check("floor combo one p4", (tc, pc, "한 통" in bc, "19,000" in tc and "중단선" in tc), ("[487240] 487240 종가 18,500 | 추가 매수 중단선 + 19,000원 하단 이탈", 4, True, True))
    tr, br, pr = message(cfg, [l["l22000"], {"key": "recovery", "title": cfg["recovery"]["title"], "priority": 3, "warn": False, "level": 22680, "volume": 500, "avg": 1000}], "2026-11-20", 22700, 24000, 35000, "s")
    check("buy+recovery one message", (tr, pr, "2차 매수" in tr and "22,680" in tr, "한 번에 점검" in br), ("[487240] 487240 종가 22,700 | 2차 매수 검토 + 22,680원 회복", 3, True, True))
    allt = t1 + b1 + t2 + b2 + t3 + b3 + t4 + b4 + tc + bc + tr + br
    check("no dot/dash", any(ch in allt for ch in "\u00b7\u2014"), False)
    check("six parts", [x.split(":")[0] for x in b1.split("\n")[2:]], ["지금 결과/상태", "지금 할 일", "나중에", "익절/줄일 때", "하지 말 것"])
    check("krx_close in status", "krx_close" in b1, True)
    # run: seed silent, next day fires, before 15:40 ignored, same day silent
    rows = _rows([(f"2026-10-{i + 1:02d}", 30000.0) for i in range(20)] + [("2026-11-02", 26480.0), ("2026-11-03", 25075.0)])
    out, st = run("dry", datetime(2026, 11, 3, 19, 0, tzinfo=KST), cfg, lambda: rows, {})
    check("run seed silent", (out, st["armed"]["l25000"]), ([], True))
    rows2 = rows + _rows([("2026-11-04", 24800.0)])
    out, st = run("dry", datetime(2026, 11, 4, 15, 30, tzinfo=KST), cfg, lambda: rows2, st)
    check("today before 15:40 ignored", out, [])
    out, st = run("dry", datetime(2026, 11, 4, 19, 0, tzinfo=KST), cfg, lambda: rows2, st)
    check("next day fires l25000", [o[0] for o in out], ["[487240] 487240 종가 24,800 | 1차 매수 검토"])
    out, st = run("dry", datetime(2026, 11, 4, 19, 5, tzinfo=KST), cfg, lambda: rows2, st)
    check("same day re-run silent", out, [])
    # reminders
    r0 = cfg["reminders"][0]
    check("customs reminder date", r0["date"], "2026-11-16")
    rs: dict = {}
    check("not due before date", due_reminders(cfg, rs, "2026-11-13"), [])
    check("due on date", [r["key"] for r in due_reminders(cfg, rs, "2026-11-16")], ["customs_oct_2026"])
    rs["reminders_sent"]["customs_oct_2026"] = "x"
    check("no repeat", due_reminders(cfg, rs, "2026-11-17"), [])
    rs2: dict = {}
    check("stale skip", (due_reminders(cfg, rs2, "2026-11-30"), "customs_oct_2026" in rs2["reminders_sent"]), ([], True))
    trm, brm, prm = reminder_message(cfg, r0, {"last_close": ["2026-11-16", 24000.0], "ma20": 26000.0}, "2026-11-16 19:00 KST")
    check("reminder title tickers + p3", ("487240" in trm and "298040" in trm, prm), (True, 3))
    check("reminder body HS codes, site, funding rule", all(k in brm for k in ("HS 8504", "8535", "8537", "tradedata.go.kr", "MEMEXIT", "2차 대체 트리거")), True)
    out, st = run("dry", datetime(2026, 11, 16, 19, 0, tzinfo=KST), cfg, lambda: rows2, st)
    check("run sends reminder once on 11/16", [o[0] for o in out], [trm])
    out, st = run("dry", datetime(2026, 11, 17, 19, 0, tzinfo=KST), cfg, lambda: rows2, st)
    check("run no repeat 11/17", out, [])
    print("selftest", "PASS" if not fails else f"FAIL {fails}")
    return 1 if fails else 0


def confirm(cfg: dict) -> int:
    st = json.loads(STATE_PATH.read_text(encoding="utf-8")) if STATE_PATH.exists() else {}
    lc = st.get("last_close") or ["기록 없음", 0]
    body = "\n".join([
        "이게 뭐 알림: KODEX AI전력핵심설비(487240) 관심 종목 확정 종가 선 알림 설치 확인입니다. 매매 신호 아님.",
        f"{datetime.now(KST).strftime('%Y-%m-%d %H:%M KST')} | 487240 | KODEX AI전력핵심설비",
        f"지금 결과/상태: 최근 확정 종가 {lc[0]} {won(lc[1])}원, 20일선 {won(st.get('ma20'))}원, 200일선 {won(st.get('ma200'))}원.",
        "지금 할 일: 없음. 평일 19:00 KST 정규장 종가(krx_close)로 25,000 이하(p3), 22,000 이하(p3), 20,700 미만 중단선(p4), "
        "20일선 위 2일 연속(p3), 19,000 지지/이탈, 22,680 회복을 상태가 바뀔 때만 봅니다.",
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
