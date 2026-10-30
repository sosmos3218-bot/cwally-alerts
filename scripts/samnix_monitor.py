#!/usr/bin/env python3
"""SAMNIX: confirmed two-stock flows in shares, for reference only.

Plus (2026-10-24, approved card t25s2) a per-stock foreign net-buy streak flip alert:
foreign net buy > 0 on 3 consecutive trading days right after a net-sell day -> one alert (priority 4).
One net-sell day re-arms.
First-tranche marker (2026-10-30, pre-approved 10/26): on a flip, the same trading day KOSPI close (Naver index price)
>= configs first_tranche.kospi_level adds "1차 조건 충족" to the title (p4); below it a body line "코스피 5,500 미만, 1차 조건 미충족".
Toggle: first_tranche.enabled. Flip detection, dedupe and state are unchanged. Judged by data trading date (state["flip"][code]["last_date"]), confirmed rows only
(<= the preceding trading day at 15:20), so a date is never judged twice. Modes: auto | dry | test | selftest.

Weekly digest (approved in chat 2026-10-30, owner note pt24u): the daily 15:20 reference message is no longer sent.
Each weekday 15:20 run records the preceding day's confirmed totals in state week_rows silently. The Friday 15:20 run
(or, if Friday was missed, the next run whose last digest is 7+ days old) sends ONE p3 "[SAMNIX] 주간 수급 요약
(MM/DD~MM/DD)". Zero rows -> nothing sent (the digest clock still advances). The p4 foreign 3-day flip stays immediate.
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


def first_tranche_line(ft: dict | None, kospi: dict | None, day: str) -> tuple[bool, str]:
    """(1차 조건 충족 여부, 본문 줄). ft = configs/samnix.json first_tranche, kospi = {"day","close"} or None."""
    if not ft or not ft.get("enabled"):
        return False, ""
    level = float(ft.get("kospi_level", 5500))
    lv = f"{level:,.0f}"
    if not kospi or kospi.get("close") is None or kospi.get("day") != day:
        return False, (f"1차 조건: 전환 마지막 날({ymd(day)}) 코스피 종가가 아직 조회되지 않아 1차 조건 충족 여부는 표시하지 않습니다. "
                       f"[KOSPI] 15:40 묶음의 5,500 상태로 직접 확인하세요.")
    close = kospi["close"]
    if close >= level:
        return True, (f"1차 조건 판정: {ft.get('marker', '1차 조건 충족')}. {ymd(day)} 코스피 종가 {close:,.2f} (코스피 {lv} 이상) + 외국인 {FLIP_RUN}일 연속 순매수 전환. "
                      "분할매수 1차 검토 조건일 뿐 주문 지시가 아닙니다.")
    return False, f"코스피 {lv} 미만, 1차 조건 미충족 ({ymd(day)} 코스피 종가 {close:,.2f})."


def kospi_close(day: str) -> dict | None:
    """Naver 지수 일별 종가 (samnix 와 같은 데이터 거래일). 없거나 실패면 None (추정 안 함)."""
    import naver_api
    try:
        raw = naver_api.get_json("https://m.stock.naver.com/api/index/KOSPI/price?pageSize=10&page=1")
    except Exception as exc:  # noqa: BLE001
        print(f"kospi close 조회 실패: {exc}")
        return None
    for r in raw if isinstance(raw, list) else []:
        if str(r.get("localTradedAt", "")).replace("-", "") == day:
            return {"day": day, "close": naver_api.num(r.get("closePrice"))}
    return None


def flip_message(now: datetime, name: str, ev: dict, ft: dict | None = None, kospi: dict | None = None) -> tuple[str, str, int]:
    run = ev["days"]
    met, ft_line = first_tranche_line(ft, kospi, run[-1]["day"])
    if met:
        title = f"[SAMNIX] {ft.get('marker', '1차 조건 충족')} 외국인 순매수 전환 {name} {FLIP_RUN}일 연속"
    else:
        title = f"[SAMNIX] 참고용 외국인 순매수 전환 {name} {FLIP_RUN}일 연속"
    prio = int(ft.get("priority", 4)) if met else 4
    days = ", ".join(f"{ymd(r['day'])} 외국인 {fmt_opt(r.get('foreign'))} / 기관 {fmt_opt(r.get('organ'))}" for r in run)
    lines = [
        f"{now.strftime('%Y-%m-%d %H:%M KST')} | SAMNIX | {name} 외국인 순매수 전환 (확정 수급, 주식 수)",
        f"이게 뭐 알림: {name} 외국인이 순매도 구간 뒤 {FLIP_RUN}거래일 연속 순매수로 돌아섰다는 참고용 알림. 매매 신호가 아닙니다.",
        f"지금 상태: {days}. 앞선 외국인 연속 순매도 {ev['sell_len']} ({ymd(ev['sell_from'])}~{ymd(ev['sell_to'])}). 출처 네이버 증권 종목별 투자자 확정 수급.",
        "판단 조건: 10/23 반등을 솔리다임 IPO 철회 안도 반등으로만 보는 판단을 바꿀 두 조건 중 하나(외국인 3일 연속 순매수 전환)입니다. "
        "다른 하나(실적 발표 뒤 코스피 5,500 유지)는 [KOS5500] 알림이 따로 봅니다. 두 조건이 함께 맞을 때 판단 변경을 검토하세요.",
        "지금 할 일: 외국인 전환 사실만 확인. 이 알림 하나로 추격 매수하지 마세요.",
        "나중에: 외국인이 다시 순매도하는 날 이 조건이 다시 무장되고, 다음 전환 때 다시 옵니다. 일일 수급은 금요일 15:20 주간 요약으로 옵니다.",
        "익절/줄일 때: 이 알림만으로 매매하지 말고 개별 종목 조건을 따르세요.",
        "하지 말 것: 3일 순매수만 보고 반등 지속을 단정하기.",
    ]
    if ft_line:
        lines.insert(4, ft_line)  # 판단 조건 다음, 지금 할 일 앞
    return title, "\n".join(lines), prio


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
            ft = cfg.get("first_tranche")
            kospi = kospi_close(ev["days"][-1]["day"]) if ft and ft.get("enabled") else None
            title, message, prio = flip_message(now, name, ev, ft, kospi)
            assert route_topic("cwally-port", title) == "cwally-port", title
            print(title, "\n", message)
            if mode == "auto":
                ntfy(cfg.get("ntfy_server") or "https://ntfy.sh", cfg.get("ntfy_topic") or "cwally-port", title, message, prio)
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
    when = datetime(2026, 10, 27, 15, 20, tzinfo=KST)
    ev0 = flip_events(mk([-5] * 18 + [3, 2, 1]), {"armed": True})[0]
    t, m, p = flip_message(when, "SK하이닉스", ev0)
    assert t == "[SAMNIX] 참고용 외국인 순매수 전환 SK하이닉스 3일 연속" and p == 4 and "1차 조건" not in m
    assert "\u00b7" not in t + m and "\u2014" not in t + m and route_topic("cwally-port", t) == "cwally-port"
    assert "확인 불가" not in t and "조회 실패" not in t
    print(f"-- sample (가짜 숫자)\nTITLE: {t}\nPRIORITY: {p}\n{m}\n")
    first_tranche_selftest(when, ev0)
    weekly_selftest()
    print("SELFTEST OK: no network, no publish, no state")
    return 0


def first_tranche_selftest(when: datetime, ev: dict) -> None:
    """1차 조건 표시 (2026-10-30, 사용자 사전 승인 10/26). 가짜 숫자, 네트워크 없음."""
    ft = json.loads(CFG_PATH.read_text(encoding="utf-8")).get("first_tranche") or {}
    assert ft.get("kospi_level") == 5500 and ft.get("marker") == "1차 조건 충족", ft
    ft = dict(ft, enabled=True)
    day = ev["days"][-1]["day"]
    seen = []
    for label, kospi in (("코스피 5,500 이상", {"day": day, "close": 5612.34}),
                         ("코스피 5,500 미만", {"day": day, "close": 5480.10}),
                         ("종가 미조회", None)):
        t, m, p = flip_message(when, "SK하이닉스", ev, ft, kospi)
        assert "확인 불가" not in t and "조회 실패" not in t and route_topic("cwally-port", t) == "cwally-port", t
        assert "\u00b7" not in t + m and "\u2014" not in t + m
        seen.append((t, p, m))
        print(f"-- first_tranche fixture: {label}\nTITLE: {t}\nPRIORITY: {p}\n{m}\n")
    assert seen[0][0] == "[SAMNIX] 1차 조건 충족 외국인 순매수 전환 SK하이닉스 3일 연속" and seen[0][1] == 4
    assert seen[1][0] == "[SAMNIX] 참고용 외국인 순매수 전환 SK하이닉스 3일 연속" and "코스피 5,500 미만, 1차 조건 미충족" in seen[1][2]
    assert seen[2][0] == seen[1][0] and "1차 조건 충족 여부는 표시하지 않습니다" in seen[2][2]
    # 다른 날짜 종가는 쓰지 않음
    t, m, _ = flip_message(when, "SK하이닉스", ev, ft, {"day": "20261001", "close": 6000.0})
    assert t == seen[1][0] and "아직 조회되지 않아" in m
    # 토글 false 면 기존 메시지 그대로
    assert flip_message(when, "SK하이닉스", ev, dict(ft, enabled=False), {"day": day, "close": 6000.0})[0] == seen[1][0]
    print("first_tranche selftest OK")


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
        if mode in ("auto", "dry"):  # the weekly digest still goes out on its day with the rows already queued
            work = json.loads(json.dumps(state))
            sends = [] if work.get("day") == send_day else weekly_step(work, now, None, send_day)
            for t, m in sends:
                print(t, "\n", m)
            if mode == "auto":
                for t, m in sends:
                    ntfy(server, topic, t, m)
                work["last_skip"] = {"day": send_day, "reason": reason, "at": now.isoformat()}
                save_state(work)
        return 0

    # Naver reports net shares, not KRW. Convert by each stock's close before aggregation.
    foreign = sum(r["foreign"] * r["close"] for r in flows.values())
    organ = sum(r["organ"] * r["close"] for r in flows.values())
    row = {"flow_day": flow_day, "foreign": round(foreign), "organ": round(organ),
           "stocks": {n: {"foreign": r["foreign"], "organ": r["organ"], "close": r["close"]} for n, r in flows.items()}}
    print(f"{ymd(flow_day)} 외국인 합 {fmt_krw(foreign)} / 기관 합 {fmt_krw(organ)} ({signal_of(foreign, organ)[0]})")
    if mode == "test":
        t, m = digest_message(now, [row], now - timedelta(days=7))
        t = t.replace("[SAMNIX] ", "[SAMNIX] 테스트 ", 1)
        print(t, "\n", m)
        ntfy(server, topic, t, m)
        print("TEST: state 변경 없음")
        return 0
    work = json.loads(json.dumps(state))
    sends = weekly_step(work, now, row, send_day)
    for t, m in sends:
        print(t, "\n", m)
    if mode == "dry":
        print("DRY: ntfy/state 변경 없음")
        return 0
    for t, m in sends:
        ntfy(server, topic, t, m)
    work.pop("last_skip", None)
    save_state(work)
    return 0


def signal_of(foreign: float, organ: float) -> tuple[str, str]:
    if foreign > 0 and organ > 0:
        return "외국인+기관 순매수", "우호"
    if foreign < 0:
        return "외국인 순매도", "신중"
    return "수급 혼조", "중립"


def parse_iso(s):
    try:
        return datetime.fromisoformat(s) if s else None
    except ValueError:
        return None


def digest_due(state: dict, now: datetime) -> bool:
    last = parse_iso(state.get("last_digest_at"))
    if last is None or last.date() == now.date():
        return False
    return now.weekday() == 4 or (now - last) >= timedelta(days=7)


def weekly_step(state: dict, now: datetime, row: dict | None, send_day: str) -> list[tuple[str, str]]:
    """Record one confirmed day (dedupe by flow_day), then send the weekly digest when due. Mutates state."""
    rows = state.setdefault("week_rows", [])
    if row and row["flow_day"] > state.get("digest_after", "") and row["flow_day"] not in {r["flow_day"] for r in rows}:
        rows.append(row)
    state.update({"day": send_day, "flow_day": (row or {}).get("flow_day", state.get("flow_day")), "updated_at": now.isoformat()})
    if state.get("last_digest_at") is None:
        state["last_digest_at"] = now.isoformat()
        state["week_rows"] = []  # the seeding day was already sent as a daily message
        state["digest_after"] = (row or {}).get("flow_day", "")
        print("digest clock seeded")
        return []
    if not digest_due(state, now):
        print(f"weekly digest not due (last {state.get('last_digest_at')}); {len(rows)} day(s) queued; silent")
        return []
    since = parse_iso(state["last_digest_at"])
    out = []
    if rows:
        out.append(digest_message(now, sorted(rows, key=lambda r: r["flow_day"]), since))
    else:
        print("weekly digest due but no rows; silent")
    state["week_rows"] = []
    state["last_digest_at"] = now.isoformat()
    return out


def digest_message(now: datetime, rows: list[dict], since: datetime) -> tuple[str, str]:
    f_sum = sum(r["foreign"] for r in rows)
    o_sum = sum(r["organ"] for r in rows)
    days = " ".join(f"{ymd(r['flow_day'])[5:]} 외국인 {fmt_krw(r['foreign'])}, 기관 {fmt_krw(r['organ'])} ({signal_of(r['foreign'], r['organ'])[0]})." for r in rows)
    per = []
    for name in CODES.values():
        fs = sum((r["stocks"].get(name) or {}).get("foreign", 0) for r in rows)
        os_ = sum((r["stocks"].get(name) or {}).get("organ", 0) for r in rows)
        per.append(f"{name} 외국인 {fmt_shares(fs)}, 기관 {fmt_shares(os_)}")
    sig, tone = signal_of(f_sum, o_sum)
    span = f"{ymd(rows[0]['flow_day'])[5:].replace('-', '/')}~{ymd(rows[-1]['flow_day'])[5:].replace('-', '/')}"
    title = f"[SAMNIX] 주간 수급 요약 ({span}) | 참고용 {sig}"
    lines = [
        f"{now.strftime('%Y-%m-%d %H:%M KST')} | SAMNIX | 삼성전자, SK하이닉스 주간 확정 수급 (주식 수 × 해당일 종가, 추정금액)",
        "이게 뭐 알림: 매일 15:20 에 모은 전 거래일 확정 수급을 금요일 15:20 에 1번 묶어 보내는 참고용 요약. 매매 신호가 아닙니다.",
        f"지금 결과/상태: {len(rows)}거래일 합계 외국인 {fmt_krw(f_sum)}, 기관 {fmt_krw(o_sum)}, 주간 방향 {sig}({tone}). 일별: {days} 종목별 주식 수 합계: {', '.join(per)}.",
        "지금 할 일: 주간 방향만 확인. 다음 주 개별 종목 조건 알림과 함께 보세요.",
        "나중에: 외국인 순매도 뒤 3거래일 연속 순매수 전환(우선순위 4)은 요약을 기다리지 않고 바로 옵니다. 다음 요약은 다음 금요일 15:20.",
        "익절/줄일 때: 이 요약만으로 매매하지 말고 개별 종목 조건을 따르세요.",
        "하지 말 것: 지난주 수급 합계만 보고 추격 매수, 신용/미수.",
    ]
    return title, "\n".join(lines)


def weekly_selftest() -> None:
    def R(d, f, o):
        return {"flow_day": d, "foreign": f * 1e8, "organ": o * 1e8,
                "stocks": {"삼성전자": {"foreign": f * 100, "organ": o * 100, "close": 1}, "SK하이닉스": {"foreign": f, "organ": o, "close": 1}}}

    def at(s):
        return datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=KST)
    st = {}
    assert weekly_step(st, at("2026-10-30 15:20"), R("20261029", 1, 1), "20261030") == []  # seed
    assert weekly_step(st, at("2026-10-30 15:40"), R("20261029", 1, 1), "20261030") == [] and st["week_rows"] == []  # rerun after seed
    for d, fd in (("2026-11-02", "20261030"), ("2026-11-03", "20261102"), ("2026-11-04", "20261103"), ("2026-11-05", "20261104")):
        assert weekly_step(st, at(f"{d} 15:20"), R(fd, 2, -1), d.replace("-", "")) == [], d  # daily silent
    assert weekly_step(st, at("2026-11-05 15:25"), R("20261104", 2, -1), "20261105") == []  # same flow_day dedupe
    assert len(st["week_rows"]) == 4
    out = weekly_step(st, at("2026-11-06 15:20"), R("20261105", 3, 9), "20261106")
    assert len(out) == 1 and out[0][0] == "[SAMNIX] 주간 수급 요약 (10/30~11/05) | 참고용 외국인+기관 순매수", out[0][0]
    t, m = out[0]
    assert "\u00b7" not in t + m and "\u2014" not in t + m
    assert [x.split(":")[0] for x in m.split("\n")[1:7]] == ["이게 뭐 알림", "지금 결과/상태", "지금 할 일", "나중에", "익절/줄일 때", "하지 말 것"]
    assert "5거래일 합계 외국인 +11억원" in m, m
    assert weekly_step(st, at("2026-11-06 15:30"), None, "20261106") == []  # same Friday rerun
    # missed Friday 11/13 -> Monday 11/16 catch-up (7+ days)
    for d, fd in (("2026-11-09", "20261106"), ("2026-11-10", "20261109"), ("2026-11-11", "20261110"), ("2026-11-12", "20261111")):
        assert weekly_step(st, at(f"{d} 15:20"), R(fd, -1, -1), d.replace("-", "")) == []
    out = weekly_step(st, at("2026-11-16 15:20"), R("20261113", -1, 1), "20261116")
    assert len(out) == 1 and "11/06~11/13" in out[0][0] and "외국인 순매도" in out[0][0], out
    assert weekly_step(st, at("2026-11-17 15:20"), None, "20261117") == []  # not due next day
    # empty week: Friday silent, clock advances, Monday not a catch-up
    st["week_rows"] = []
    assert weekly_step(st, at("2026-11-20 15:20"), None, "20261120") == []
    assert st["last_digest_at"].startswith("2026-11-20")
    assert weekly_step(st, at("2026-11-23 15:20"), None, "20261123") == []
    print("weekly selftest OK")

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "auto"))
