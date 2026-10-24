#!/usr/bin/env python3
"""SAMNIX: confirmed two-stock flows in shares, for reference only.

Plus (2026-10-24, approved card t25s2) a per-stock foreign net-buy streak flip alert:
foreign net buy > 0 on 3 consecutive trading days right after a net-sell day -> one alert (priority 4).
One net-sell day re-arms. Judged by data trading date (state["flip"][code]["last_date"]), confirmed rows only
(<= the preceding trading day at 15:20), so a date is never judged twice. Modes: auto | dry | test | selftest.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ntfy_util import append_ends, route_topic

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = ROOT / "state" / "samnix.json"
CFG_PATH = ROOT / "configs" / "samnix.json"
CODES = {"005930": "삼성전자", "000660": "SK하이닉스"}


def fmt_shares(value: float) -> str:
    return f"{value:+,.0f}주"


def fmt_krw(won: float) -> str:
    return f"{won / 100_000_000:+,.0f}억원"


def read_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def fetch_stock_flow(code: str, day: str) -> tuple[dict | None, str]:
    import naver_api
    try:
        rows = naver_api.stock_trend(code, 10)
    except Exception as exc:
        return None, f"{code} 조회 실패: {exc}"
    row = next((r for r in rows if r.get("day") == day), None)
    if row is None:
        latest = max((str(r.get("day", "")) for r in rows), default="없음")
        return None, f"{code} 확정 수급 기준일 {day} 없음 (최신 {latest})"
    if any(row.get(k) is None for k in ("foreign", "organ", "close")):
        return None, f"{code} 외국인/기관 수급 또는 종가 누락 ({day})"
    return row, ""


FLIP_RUN = 3
NA = "확인 불가"


def fmt_opt(value) -> str:
    return NA if value is None else fmt_shares(value)


def ymd(day: str) -> str:
    return f"{day[:4]}-{day[4:6]}-{day[6:]}"


def foreign_streak(rows: list[dict]) -> str:
    """rows oldest first. Latest run of same-sign foreign net shares, as text."""
    if not rows or rows[-1].get("foreign") is None:
        return NA
    last = rows[-1]["foreign"]
    if last == 0:
        return f"{ymd(rows[-1]['day'])} 외국인 순매매 0주"
    n = 0
    for r in reversed(rows):
        v = r.get("foreign")
        if v is None or v == 0 or (v > 0) != (last > 0):
            break
        n += 1
    first = rows[-n]["day"]
    word = "순매수" if last > 0 else "순매도"
    span = ymd(first) if n == 1 else f"{ymd(first)}~{ymd(rows[-1]['day'])}"
    return f"{n}거래일 연속 {word} ({span})"


def flip_events(rows: list[dict], st: dict) -> list[dict]:
    """rows oldest first, confirmed only. Updates st (last_date, armed); returns fired events."""
    out = []
    for i, r in enumerate(rows):
        if st.get("last_date") and r["day"] <= st["last_date"]:
            continue
        v = r.get("foreign")
        if v is None:
            break  # 빈 값은 다음 실행에서 다시 판정
        st["last_date"] = r["day"]
        if v < 0:
            st["armed"] = True
            continue
        if v == 0:
            continue
        run = []
        j = i
        while j >= 0 and (rows[j].get("foreign") or 0) > 0:
            run.insert(0, rows[j])
            j -= 1
        if len(run) != FLIP_RUN or j < 0 or rows[j].get("foreign") is None or rows[j]["foreign"] >= 0:
            continue
        sell = 0
        k = j
        while k >= 0 and rows[k].get("foreign") is not None and rows[k]["foreign"] < 0:
            sell += 1
            k -= 1
        sell_txt = f"{sell}거래일" + (" 이상(조회 범위 끝)" if k < 0 else "")
        if st.get("armed", True):
            st["armed"] = False
            out.append({"days": run, "sell_len": sell_txt, "sell_from": rows[j - sell + 1]["day"], "sell_to": rows[j]["day"]})
    return out


def seed_flip(rows: list[dict], now: datetime) -> dict:
    """지난 기록으로 무장 상태만 정함. 마지막 확정일까지는 판정하지 않음."""
    st = {"armed": True}
    for r in rows:
        v = r.get("foreign")
        if v is None:
            break
        if v < 0:
            st["armed"] = True
        elif v > 0:
            n, j = 0, rows.index(r)
            while j >= 0 and (rows[j].get("foreign") or 0) > 0:
                n, j = n + 1, j - 1
            if n == FLIP_RUN and j >= 0 and (rows[j].get("foreign") or 0) < 0:
                st["armed"] = False
        st["last_date"] = r["day"]
    st["seeded"] = {"at": now.isoformat(), "upto": st.get("last_date"), "note": "seed 이전 날짜는 판정 안 함"}
    return st


def flip_message(now: datetime, name: str, ev: dict) -> tuple[str, str]:
    run = ev["days"]
    title = f"[SAMNIX] 참고용 외국인 순매수 전환 {name} {FLIP_RUN}일 연속"
    days = ", ".join(f"{ymd(r['day'])} 외국인 {fmt_opt(r.get('foreign'))} / 기관 {fmt_opt(r.get('organ'))}" for r in run)
    lines = [
        f"{now.strftime('%Y-%m-%d %H:%M KST')} | SAMNIX | {name} 외국인 순매수 전환 (확정 수급, 주식 수)",
        f"이게 뭐 알림: {name} 외국인이 순매도 구간 뒤 {FLIP_RUN}거래일 연속 순매수로 돌아섰다는 참고용 알림. 매매 신호가 아닙니다.",
        f"지금 상태: {days}. 앞선 외국인 연속 순매도 {ev['sell_len']} ({ymd(ev['sell_from'])}~{ymd(ev['sell_to'])}). 출처 네이버 증권 종목별 투자자 확정 수급.",
        "판단 조건: 10/23 반등을 솔리다임 IPO 철회 안도 반등으로만 보는 판단을 바꿀 두 조건 중 하나(외국인 3일 연속 순매수 전환)입니다. "
        "다른 하나(실적 발표 뒤 코스피 5,500 유지)는 [KOS5500] 알림이 따로 봅니다. 두 조건이 함께 맞을 때 판단 변경을 검토하세요.",
        "지금 할 일: 외국인 전환 사실만 확인. 이 알림 하나로 추격 매수하지 마세요.",
        "나중에: 외국인이 다시 순매도하는 날 이 조건이 다시 무장되고, 다음 전환 때 다시 옵니다. 평일 15:20 일일 수급 알림은 그대로 옵니다.",
        "익절/줄일 때: 이 알림만으로 매매하지 말고 개별 종목 조건을 따르세요.",
        "하지 말 것: 3일 순매수만 보고 반등 지속을 단정하기.",
    ]
    return title, "\n".join(lines)


def flip_check(mode: str, now: datetime, flow_day: str, state: dict) -> None:
    """확정 행(<= flow_day)만으로 종목별 외국인 순매수 전환을 판정. auto 만 발송/저장."""
    import naver_api
    flip = state.setdefault("flip", {})
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    for code, name in CODES.items():
        try:
            rows = sorted((r for r in naver_api.stock_trend(code, 60) if r.get("day") and r["day"] <= flow_day), key=lambda r: r["day"])
        except Exception as exc:  # noqa: BLE001
            print(f"flip {code} 조회 실패: {exc}")
            continue
        st = flip.setdefault(code, {"armed": True})
        if not st.get("last_date") and rows:  # 처음 보는 종목: 지난 기록은 판정 없이 seed
            st.update(seed_flip(rows, now))
        tail = " ".join(f"{r['day'][4:]}:{fmt_opt(r.get('foreign'))}" for r in rows[-5:])
        work = dict(st)
        events = flip_events(rows, work)
        print(f"flip {name}: 현재 외국인 {foreign_streak(rows)}, armed={work.get('armed')}, last_date={work.get('last_date')}, 새 전환 {len(events)}건 | 최근 {tail}")
        for ev in events:
            title, message = flip_message(now, name, ev)
            assert route_topic("cwally-port", title) == "cwally-port", title
            print(title, "\n", message)
            if mode == "auto":
                ntfy(cfg.get("ntfy_server") or "https://ntfy.sh", cfg.get("ntfy_topic") or "cwally-port", title, message, 4)
                work.setdefault("fired", []).append({"date": ev["days"][-1]["day"], "at": now.isoformat()})
                work["fired"] = work["fired"][-10:]
        if mode == "auto":
            flip[code] = work
    if mode == "auto":
        save_state(state)
    else:
        print(f"{mode.upper()}: 외국인 전환 판정 state 변경 없음")


def selftest() -> int:
    def mk(seq):
        return [{"day": f"202610{i + 1:02d}", "foreign": v, "organ": -v if v is not None else None, "close": 1.0} for i, v in enumerate(seq)]
    # 18일 순매도 뒤 3일 순매수 -> 1건, 4일째는 없음
    rows = mk([-5] * 18 + [3, 2, 1, 4])
    st = {"armed": True}
    ev = flip_events(rows[:20], st)
    assert ev == [] and st["last_date"] == "20261020", (ev, st)
    ev = flip_events(rows, st)
    assert len(ev) == 1 and ev[0]["sell_len"] == "18거래일 이상(조회 범위 끝)" and ev[0]["days"][-1]["day"] == "20261021", ev
    assert flip_events(rows, st) == []  # 같은 날짜 두 번 판정 안 함
    # 매수 2일 뒤 매도(재무장), 다시 3일 매수 -> 1건; 앞선 매도 1일
    rows = mk([-1, -2, 3, 4, -1, 2, 2, 2])
    st = {"armed": True}
    ev = flip_events(rows, st)
    assert len(ev) == 1 and ev[0]["sell_len"] == "1거래일" and ev[0]["days"][0]["day"] == "20261006", ev
    # 무장 해제 상태(이미 발송)에서는 매도 하루 없이 다시 안 옴, 0 은 재무장 아님
    rows = mk([-1, 1, 1, 1, 0, 1, 1, 1])
    st = {"armed": True}
    assert len(flip_events(rows, st)) == 1 and st["armed"] is False
    # 빈 값은 판정 멈춤, 다음 실행에서 이어감
    rows = mk([-3, 1, 1, None])
    st = {"armed": True}
    assert flip_events(rows, st) == [] and st["last_date"] == "20261003"
    rows[3]["foreign"] = 2
    assert len(flip_events(rows, st)) == 1
    # 시작부터 매수 3일(앞선 매도 모름) -> 발송 안 함
    assert flip_events(mk([1, 1, 1]), {"armed": True}) == []
    assert foreign_streak(mk([-1, 2, 3])) == "2거래일 연속 순매수 (2026-10-02~2026-10-03)"
    assert foreign_streak(mk([1, None])) == NA
    t, m = flip_message(datetime(2026, 10, 27, 15, 20, tzinfo=KST), "SK하이닉스", flip_events(mk([-5] * 18 + [3, 2, 1]), {"armed": True})[0])
    assert "\u00b7" not in t + m and "\u2014" not in t + m and route_topic("cwally-port", t) == "cwally-port"
    assert "확인 불가" not in t and "조회 실패" not in t
    print(f"-- sample (가짜 숫자)\nTITLE: {t}\nPRIORITY: 4\n{m}\n")
    print("SELFTEST OK: no network, no publish, no state")
    return 0


def ntfy(server: str, topic: str, title: str, message: str, priority: int = 3) -> None:
    import urllib.request
    payload = json.dumps({"topic": route_topic(topic, title), "title": title,
                          "message": append_ends(message), "priority": priority,
                          "tags": ["chart_with_upwards_trend"]}).encode()
    req = urllib.request.Request(server.rstrip("/"), data=payload, method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        print("ntfy ok", resp.read().decode())


def main(mode: str = "auto") -> int:
    if mode == "selftest":
        return selftest()
    if mode not in ("auto", "dry", "test"):
        raise ValueError(f"unsupported mode: {mode}")
    now = datetime.now(KST)
    import kr_calendar
    session = kr_calendar.session_day(now)
    if not kr_calendar.is_trading_day(session):
        print(f"{session} 휴장/주말 skip")
        if mode == "dry":  # dry 는 주말에도 외국인 전환 판정만 보여줌 (발송/state 없음)
            flip_check("dry", now, kr_calendar.prev_trading_day(session).strftime("%Y%m%d"), read_state())
        return 0
    # At 15:20 KST, use the preceding trading day's confirmed row only.
    flow_day = kr_calendar.prev_trading_day(session).strftime("%Y%m%d")
    send_day = session.strftime("%Y%m%d")
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    topic = cfg.get("ntfy_topic") or "cwally-port"
    server = cfg.get("ntfy_server") or "https://ntfy.sh"
    state = read_state()
    flip_check(mode, now, flow_day, state)

    flows = {}
    errors = []
    for code, name in CODES.items():
        row, error = fetch_stock_flow(code, flow_day)
        if error:
            errors.append(error)
        else:
            flows[name] = row
    if errors:
        reason = "; ".join(errors)
        print(f"수급 데이터 없음; quiet skip: {reason}")
        if mode == "auto":
            state["last_skip"] = {"day": send_day, "reason": reason, "at": now.isoformat()}
            save_state(state)
        return 0

    # Naver reports net shares, not KRW. Convert by each stock's close before aggregation.
    foreign = sum(r["foreign"] * r["close"] for r in flows.values())
    organ = sum(r["organ"] * r["close"] for r in flows.values())
    if foreign > 0 and organ > 0:
        signal = "외국인+기관 순매수 | 수급 우호"
        do_now = "전 거래일 수급은 우호적입니다. 당일 수급 확인 전 추격하지 마세요."
    elif foreign < 0:
        signal = "외국인 순매도 | 반등 신중"
        do_now = "전 거래일 외국인 순매도입니다. 당일 수급 확인 전 추가 매수를 보류하세요."
    else:
        signal = "수급 혼조 | 계획 유지"
        do_now = "전 거래일 수급이 혼조입니다. 기존 계획을 유지하세요."
    title = f"[SAMNIX] 참고용 {signal}"
    if mode == "test":
        title = f"[SAMNIX] 테스트 참고용 {signal}"
    label = f"{flow_day[:4]}-{flow_day[4:6]}-{flow_day[6:]} 수급 주식 수 × 해당일 종가 (추정금액)"
    lines = [
        f"{now.strftime('%Y-%m-%d %H:%M KST')} | SAMNIX | 수급 기준 {label}",
        "이게 뭐 알림: 삼성전자/하이닉스 전 거래일 확정 수급 참고용. 매매 신호가 아닙니다.",
        f"지금 상태: 외국인 합 {fmt_krw(foreign)} / 기관 합 {fmt_krw(organ)} ({label})",
        "고수 동향: 당일 상위 투자자 순매수 자료 미확보. 고정 문구로 동향을 단정하지 않습니다.",
        "수요 지표: 당일 메모리 가격/AI 수요 지표 미확보. 별도 확인이 필요합니다.",
        "평가: 전 거래일 수급 방향만 확인됨. 당일 반등 지속성은 판단 불가.",
        f"지금 할 일: {do_now}",
        "나중에: 다음 평일 15:20에 최신 확정 수급을 확인합니다.",
        "익절/줄일 때: 이 알림만으로 매매하지 말고 개별 종목 조건을 따르세요.",
        "하지 말 것: 전 거래일 추정 수급금액만 보고 당일 추격 매수.",
        "", "종목별:",
    ]
    for name, row in flows.items():
        lines.append(f"- {name} 외국인 {fmt_shares(row['foreign'])} ({fmt_krw(row['foreign'] * row['close'])}) / 기관 {fmt_shares(row['organ'])} ({fmt_krw(row['organ'] * row['close'])})")
    message = "\n".join(lines)
    print(title, "\n", message)
    if mode == "dry":
        print("DRY: ntfy/state 변경 없음")
        return 0
    if mode == "auto" and state.get("day") == send_day:
        print("already sent today; silent")
        return 0
    ntfy(server, topic, title, message)
    if mode == "test":
        print("TEST: state 변경 없음")
        return 0
    state.update({"day": send_day, "flow_day": flow_day, "updated_at": now.isoformat()})
    state.pop("last_skip", None)
    save_state(state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "auto"))
