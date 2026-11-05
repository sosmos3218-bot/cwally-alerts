#!/usr/bin/env python3
"""[0163Y0] KoAct 코스닥액티브 종가 감시. 19:00 k203780 체인 같은 단계([0219B0] 가격 감시와 같은 시간).

1) 이탈: 확정 종가 < 6,440원(52주 저점) 이면 p4 1번. 6,440 x 1.03 = 6,633.2원 넘는 종가 뒤에 다시 무장.
2) 20일선 회복: 오늘 종가 > SMA20 이고 직전 종가 <= 직전 SMA20 이면 p3 1번(종가, SMA20, 거래량 / 20일 평균 거래량 비율,
   1.5 이상이면 거래 증가 확인). 종가가 SMA20 아래로 내려가면 다시 무장.
첫 실행(state 에 last_date 없음): 마지막 확정 종가로 상태만 기록, 발송 없음.
데이터: 네이버 일별 시세(키 없음). 데이터 날짜로 중복 방지. 읽기 실패는 cwally-ops 하루 1번.
modes: auto | dry(발송 없음, state 안 씀) | install(설치 확인 1회, state 안 씀) | selftest ; --as-of "YYYY-MM-DD HH:MM" (dry 전용)
"""
from __future__ import annotations

import json
import sys
import traceback
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import k079900_monitor as k79  # noqa: E402  (읽기 전용 재사용: http, num, KST)
from kr_calendar import is_trading_day, session_day, session_close_ready  # noqa: E402
from ntfy_util import publish, resolve_server, resolve_topic, route_topic  # noqa: E402

KST = k79.KST
TAG = "[0163Y0]"
NAME = "k0163y0"
CFG_PATH = ROOT / "configs" / f"{NAME}.json"
STATE_PATH = ROOT / "state" / f"{NAME}.json"
CLOSE_HM = (15, 40)


def fetch_rows(cfg: dict) -> list[dict]:
    raw = json.loads(k79.http(cfg["price_source"]))
    rows = []
    for r in raw if isinstance(raw, list) else []:
        d = str(r.get("localTradedAt") or "")[:10]
        c, v = k79.num(r.get("closePrice")), k79.num(r.get("accumulatedTradingVolume"))
        if len(d) == 10 and c:
            rows.append({"day": d, "close": c, "vol": v or 0.0})
    rows.sort(key=lambda x: x["day"])
    if len(rows) < 22:
        raise RuntimeError(f"시세 {len(rows)}건(22건 미만)")
    return rows


def confirmed(rows: list[dict], today: str, now: datetime) -> list[dict]:
    return [r for r in rows if session_close_ready(r["day"], today, now, CLOSE_HM)]


def metrics(rows: list[dict], i: int, n: int) -> dict | None:
    """rows[i] 기준 SMA(n), 직전 SMA, 거래량 비율. 데이터가 모자라면 None."""
    if i < n:
        return None
    win, prev = rows[i - n + 1:i + 1], rows[i - n:i]
    sma, psma = sum(r["close"] for r in win) / n, sum(r["close"] for r in prev) / n
    avgv = sum(r["vol"] for r in win) / n
    return {"day": rows[i]["day"], "close": rows[i]["close"], "sma": sma, "prev_close": rows[i - 1]["close"], "prev_sma": psma,
            "vol": rows[i]["vol"], "avg_vol": avgv, "vol_ratio": (rows[i]["vol"] / avgv) if avgv else None}


def seed_state(cfg: dict, rows: list[dict], stamp: str) -> dict:
    i = len(rows) - 1
    m = metrics(rows, i, cfg["ma"]["window"])
    c = rows[i]["close"]
    ps = {"last_date": rows[i]["day"], "last_close": c,
          "break_armed": not c < cfg["break"]["level"],
          "ma_armed": not (m and m["close"] > m["sma"]),
          "above_sma": bool(m and m["close"] > m["sma"]),
          "last_sma20": round(m["sma"], 2) if m else None,
          "seed_note": f"첫 실행 {stamp}: 마지막 확정 종가 {rows[i]['day']} {c:,.0f}원, SMA20 {m['sma']:,.1f}원" if m else f"첫 실행 {stamp}"}
    return ps


def evaluate(cfg: dict, ps: dict, rows: list[dict], today: str) -> list[dict]:
    b, ma = cfg["break"], cfg["ma"]
    lo, r = b["level"], b["rearm_pct"] / 100
    out = []
    for i, row in enumerate(rows):
        d = row["day"]
        if d <= ps["last_date"] or d < cfg["daily_window"]["start"] or d > today:
            continue
        c = row["close"]
        if c < lo and ps.get("break_armed", True):
            ps["break_armed"] = False
            out.append({"kind": "break", "day": d, "close": c})
        elif c > lo * (1 + r) and not ps.get("break_armed", True):
            ps["break_armed"] = True
        m = metrics(rows, i, ma["window"])
        if m:
            above = m["close"] > m["sma"]
            if above and m["prev_close"] <= m["prev_sma"] and ps.get("ma_armed", True):
                ps["ma_armed"] = False
                out.append({"kind": "ma", **m})
            elif m["close"] < m["sma"]:
                ps["ma_armed"] = True
            ps["above_sma"], ps["last_sma20"] = above, round(m["sma"], 2)
        ps["last_date"], ps["last_close"] = d, c
    return out


def body(stamp: str, hdr: str, what: str, status: str, action: str, later: str, trim: str, avoid: str) -> str:
    return "\n".join([f"{stamp} | 0163Y0 | {hdr}", f"이게 뭐 알림: {what}", f"지금 결과/상태: {status}", f"지금 할 일: {action}",
                      f"나중에: {later}", f"익절/줄일 때: {trim}", f"하지 말 것: {avoid}"])


WHAT = "KoAct 코스닥액티브(0163Y0) 확정 종가 기준 52주 저점 6,440원 이탈과 20일 이동평균 회복 감시. 매매 지시 아님."


def messages(cfg: dict, ev: list[dict], stamp: str) -> list[tuple[str, str, int]]:
    b, ma, out = cfg["break"], cfg["ma"], []
    rearm = b["level"] * (1 + b["rearm_pct"] / 100)
    for e in ev:
        if e["kind"] == "break":
            out.append((f"{TAG} 종가 6,440 이탈, 52주 저점 깨짐", body(
                stamp, "52주 저점 이탈", WHAT,
                f"{e['day']} 종가 {e['close']:,.0f}원, 52주 저점 {b['level']:,}원 아래로 내려옴(52주 고점 14,800원).",
                "보유 중이면 비중과 손절 기준을 오늘 다시 정하기. 새로 사려던 계획은 멈추고 바닥 확인부터.",
                f"{b['level']:,} x 1.03 = {rearm:,.0f}원 넘는 종가가 나온 뒤 다시 {b['level']:,}원 아래로 오면 다시 알림.",
                "반등이 약하면 나눠서 줄이기. 한 번에 다 팔 필요는 없음.",
                "장중 가격만 보고 판단하기, 물타기로 평단 낮추기. 이 알림은 확정 종가 기준."), b["priority"]))
        else:
            ok = e["vol_ratio"] is not None and e["vol_ratio"] >= ma["vol_ratio_ok"]
            vr = f"{e['vol_ratio']:.2f}배" if e["vol_ratio"] is not None else "계산 불가"
            vol = (f"거래량 {e['vol']:,.0f}주, 20일 평균 {e['avg_vol']:,.0f}주의 {vr}"
                   f" ({'거래 증가 확인' if ok else '거래 증가 미확인'}, 기준 {ma['vol_ratio_ok']}배)")
            out.append((f"{TAG} 20일선 회복, 추세 전환 확인 시작", body(
                stamp, "20일 이동평균 회복", WHAT,
                f"{e['day']} 종가 {e['close']:,.0f}원이 20일 이동평균 {e['sma']:,.0f}원 위로 올라옴"
                f"(직전 {e['prev_close']:,.0f}원, 그날 20일선 {e['prev_sma']:,.0f}원). {vol}.",
                ("거래가 같이 늘어 신호가 비교적 강함. 분할 매수를 검토할 수 있음, 계획 비중 안에서." if ok else
                 "거래가 안 늘어 신호가 약함. 하루이틀 20일선 위에서 버티는지 보고 정하기."),
                "종가가 다시 20일선 아래로 내려가면 무장이 풀렸다가, 그 뒤 다시 위로 올라오면 다시 알림.",
                "20일선 위에서 급등하면 추격하지 말고, 다시 20일선 아래로 마감하면 늘린 몫부터 줄이기.",
                "장중 돌파만 보고 사기. 이 알림은 확정 종가 기준."), ma["priority"]))
    return out


def install_message(cfg: dict, now: datetime, ps: dict | None) -> tuple[str, str]:
    cur = ""
    if ps and ps.get("last_sma20"):
        side = "위" if ps.get("above_sma") else "아래"
        cur = f" 지금 기록: {ps['last_date']} 종가 {ps['last_close']:,.0f}원, SMA20 {ps['last_sma20']:,.0f}원 {side}."
    return f"{TAG} 설치 확인 | 매매 신호 아님", body(
        now.strftime("%Y-%m-%d %H:%M KST"), "설치 확인",
        "KoAct 코스닥액티브(0163Y0) 종가 감시 설치 확인용 테스트 발송입니다. 매매 신호가 아닙니다.",
        "KRX 거래일 19:00 KST 에 네이버 확정 종가로 두 가지를 봅니다. 1) 종가 6,440원(52주 저점) 미만이면 1번. "
        "2) 종가가 20일 이동평균을 아래에서 위로 넘으면 1번(거래량 / 20일 평균 비율, 1.5배 이상이면 거래 증가 확인)." + cur,
        "알림이 도착했는지 확인만 하세요.",
        "6,440 알림은 6,633원 넘는 종가 뒤에, 20일선 알림은 종가가 20일선 아래로 내려간 뒤에 다시 켜집니다.",
        "해당 없음.", "이 메시지를 매매 신호로 해석하기.")


def ops_msg(stamp: str, status: str) -> tuple[str, str]:
    return (f"{TAG} 시세 조회 실패 | 원문 확인 필요", body(
        stamp, "데이터 읽기 실패", "0163Y0 종가 감시의 데이터 읽기 실패 안내. 하루 1번. 매매 신호 아님.", status,
        "네이버 증권에서 0163Y0 시세를 직접 보세요.", "다음 거래일 19:00 에 다시 읽고, 데이터 날짜로 이어서 판정합니다.",
        "이 안내로 비중을 바꾸지 않기.", "읽기 실패를 가격 이탈로 읽기."))


def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(st: dict) -> None:
    STATE_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def check_text(title: str, msg: str) -> None:
    assert "\u00b7" not in title + msg and "\u318d" not in title + msg and "\u2014" not in title + msg, title


def run(mode: str, now: datetime, cfg: dict, pub=publish, rows_fn=None) -> int:
    dry = mode == "dry"
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    sday = session_day(now)
    today = sday.isoformat()
    st = load_state()
    topic, server = resolve_topic("port", cfg.get("ntfy_topic")), resolve_server(cfg.get("ntfy_server"))
    print(f"{stamp} {NAME} mode={mode}")
    if mode == "install":
        t, m = install_message(cfg, now, st.get("price"))
        check_text(t, m)
        print(f"{t}\n{m}")
        pub(topic, t, m, 3, ["white_check_mark"], server, config_name=NAME)
        print("install confirmation sent (state untouched)")
        return 0
    d0, d1 = cfg["daily_window"]["start"], cfg["daily_window"]["end"]
    if not (d0 <= today <= d1):
        print(f"{today} 감시 기간 밖({d0} ~ {d1}): 조용히 종료")
        return 0
    if not is_trading_day(sday):
        print(f"{today} KRX 휴장: 조용히 종료")
        return 0
    out, ops = [], None
    try:
        rows = confirmed((rows_fn or fetch_rows)(cfg), today, now)
        if not (st.get("price") or {}).get("last_date"):
            st["price"] = seed_state(cfg, rows, stamp)
            print(f"first run: state only, no alert: {st['price']}")
        else:
            out = messages(cfg, evaluate(cfg, st["price"], rows, today), stamp)
        print(f"price: {st['price']}")
    except Exception as exc:  # noqa: BLE001
        if st.get("ops_day") != today:
            ops = ops_msg(stamp, f"0163Y0 네이버 시세 읽기 실패: {str(exc)[:160]}. state 는 그대로.")
            st["ops_day"] = today
    for title, msg, pri in out:
        check_text(title, msg)
        print(f"TITLE: {title}\nPRIORITY: {pri} TOPIC: {route_topic(topic, title)}\n{msg}\n")
    if ops:
        print(f"OPS TITLE: {ops[0]} -> {route_topic(topic, ops[0])}\n{ops[1]}\n")
    if not out and not ops:
        print("보낼 것 없음")
    if dry:
        print("DRY RUN: no publish, state not written")
        return 0
    for title, msg, pri in out:
        pub(topic, title, msg, pri, ["chart_with_upwards_trend"], server, config_name=NAME)
    if ops:
        try:
            pub(resolve_topic("ops"), ops[0], ops[1], 3, ["warning"], server, config_name=NAME)
        except Exception as exc:  # noqa: BLE001
            print(f"ops notice failed: {exc}")
    save_state(st)
    return 0


def selftest() -> int:
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    def mk(closes, vols=None, start=1):
        return [{"day": f"2026-11-{start + i:02d}" if start + i <= 30 else f"2026-12-{start + i - 30:02d}", "close": float(c),
                 "vol": float((vols or [100] * len(closes))[i])} for i, c in enumerate(closes)]
    # 20일선 아래에서 위로: 21일 동안 10,000 -> 9,000 하락 뒤 반등
    closes = [10000 - 50 * i for i in range(25)] + [9800]
    rows = mk(closes, [100] * 25 + [200])
    ps = {"last_date": rows[-2]["day"], "break_armed": True, "ma_armed": True}
    ev = evaluate(cfg, ps, rows, "2026-12-31")
    assert [e["kind"] for e in ev] == ["ma"] and ev[0]["vol_ratio"] >= 1.5, ev
    msgs = messages(cfg, ev, "T")
    assert "거래 증가 확인" in msgs[0][1] and "미확인" not in msgs[0][1], msgs
    assert not ps["ma_armed"]
    # 다음날도 위: 다시 안 보냄. 아래로 내려가면 재무장
    rows2 = rows + mk([9900, 8000], start=27)
    ev2 = evaluate(cfg, ps, rows2, "2026-12-31")
    assert ev2 == [] and ps["ma_armed"], (ev2, ps)
    # 이탈 6,440 / 재무장 6,633.2 초과
    ps3 = {"last_date": "2026-10-01", "break_armed": True, "ma_armed": True}
    r3 = mk([7000] * 21 + [6439, 6500, 6633, 6634, 6400])
    k = [e["kind"] for e in evaluate(cfg, ps3, r3, "2026-12-31") if e["kind"] == "break"]
    assert k == ["break", "break"], k
    # 첫 실행은 보내지 않음
    s = seed_state(cfg, rows, "T")
    assert s["above_sma"] and not s["ma_armed"], s
    for t, m, _ in msgs + messages(cfg, [{"kind": "break", "day": "d", "close": 6400}], "T"):
        check_text(t, m)
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
