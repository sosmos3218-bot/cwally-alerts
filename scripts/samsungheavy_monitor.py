#!/usr/bin/env python3
"""[010140] 삼성중공업 종가 감시. 19:00 k203780 체인 같은 단계([089860] 롯데렌탈 감시와 같은 시간).

1) z2: 확정 종가 <= 15,200원 이면 p3 1번(2차 분할 매수 검토 구간). 종가가 15,800원 넘게 회복한 뒤에야 다시 무장.
2) stop: 확정 종가 < 14,500원 이 2거래일 연속이면 p4 1번(손절 기준 도달). 종가가 14,500원 이상으로 돌아온 뒤에야 다시 무장.
3) trend: 확정 종가 > 16,500원 이고 그날 거래량 >= 직전 20거래일 평균 거래량 x 1.5 이면 p3 1번만(다시 무장 안 함, config trend.enabled).
판정 날짜: 정규장 확정 종가(scripts/krx_close.py, Daum days, 실패 시 네이버 15:30 분봉). 당일 행은 15:40 KST 이후에만 확정으로 본다.
  (AGENTS.md 2026-11-03 규칙: 네이버 일봉 closePrice 는 시간외가 섞여 종가로 쓰지 않음.)
  state price.last_date 보다 새 날짜만 날짜순으로 판정하고, 판정한 날짜를 last_date 에 남긴다.
  19:00 에 당일(KRX 거래일) 행이 아직 없으면 그날은 판정하지 않고 price.pending 에 남긴 뒤 다음 실행에서 이어서 판정.
  기대 날짜보다 stale_trading_days(2) 거래일 이상 밀리면 cwally-ops 에 하루 1번 안내.
첫 실행(state 에 last_date 없음): 마지막 확정 종가로 상태만 기록, 발송 없음(이미 구간 안이면 그 조건은 발송한 것으로 처리).
읽기 실패는 cwally-ops 하루 1번(제목에 조회 실패). 감시 기간(daily_window) 밖, KRX 휴장일은 조용히 끝남.
modes: auto | dry(발송 없음, state 안 씀) | selftest ; --as-of "YYYY-MM-DD HH:MM" (dry 전용)
"""
from __future__ import annotations

import json
import sys
import traceback
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import k079900_monitor as k79  # noqa: E402  (읽기 전용 재사용: KST)
from kr_calendar import is_trading_day, prev_trading_day, session_day, session_close_ready  # noqa: E402
from ntfy_util import publish, resolve_server, resolve_topic, route_topic  # noqa: E402

KST = k79.KST
TAG = "[010140]"
NAME = "samsungheavy"
CFG_PATH = ROOT / "configs" / f"{NAME}.json"
STATE_PATH = ROOT / "state" / f"{NAME}.json"
CLOSE_HM = (15, 40)


def fetch_rows(cfg: dict) -> list[dict]:
    import krx_close
    rows = [{"day": b.day, "close": b.close, "volume": b.volume} for b in krx_close.regular_bars(cfg["code"], 45)]
    if not rows:
        raise RuntimeError("시세 0건")
    return rows


def confirmed(rows: list[dict], today: str, now: datetime) -> list[dict]:
    return [r for r in rows if session_close_ready(r["day"], today, now, CLOSE_HM)]


def expected_day(sday: date, now: datetime) -> str:
    """이번 실행에서 있어야 할 마지막 확정 거래일."""
    late = (now.hour, now.minute) >= CLOSE_HM or now.date() > sday
    if is_trading_day(sday) and late:
        return sday.isoformat()
    return prev_trading_day(sday).isoformat()


def lag_days(last: str, expect: str) -> int:
    """last 다음부터 expect 까지 KRX 거래일 수(0 이면 최신)."""
    if last >= expect:
        return 0
    n, d = 0, date.fromisoformat(expect)
    lo = date.fromisoformat(last)
    while d > lo:
        if is_trading_day(d):
            n += 1
        d = date.fromordinal(d.toordinal() - 1)
    return n


def avg_vol(rows: list[dict], i: int, n: int) -> float | None:
    """rows[i] 직전 n 거래일 평균 거래량. 이력이 n 개 안 되거나 거래량이 비면 None."""
    prior = rows[max(0, i - n):i]
    vols = [r.get("volume") for r in prior]
    if len(prior) < n or any(v is None for v in vols):
        return None
    return sum(vols) / n


def trend_hit(cfg: dict, rows: list[dict], i: int) -> tuple[bool, float | None]:
    t = cfg["trend"]
    r = rows[i]
    av = avg_vol(rows, i, t["vol_days"])
    if av is None or r.get("volume") is None or av <= 0:
        return False, av
    return (r["close"] > t["level"] and r["volume"] >= t["vol_mult"] * av), av


def seed_state(cfg: dict, rows: list[dict], stamp: str) -> dict:
    i = len(rows) - 1
    r = rows[i]
    z, s = cfg["z2"], cfg["stop"]
    streak = 0
    for x in reversed(rows):
        if x["close"] < s["level"]:
            streak += 1
        else:
            break
    hit, av = trend_hit(cfg, rows, i)
    return {"last_date": r["day"], "last_close": r["close"], "last_volume": r.get("volume"),
            "z2_armed": not r["close"] <= z["level"],
            "stop_armed": streak < s["days"],
            "below_streak": streak,
            "trend_sent": r["day"] if hit else None,
            "pending": None,
            "seed_note": f"첫 실행 {stamp}: 마지막 확정 종가 {r['day']} {r['close']:,.0f}원, 거래량 {r.get('volume') or 0:,.0f}주"
                         + (f", 직전 20거래일 평균 {av:,.0f}주" if av else "")}


def evaluate(cfg: dict, ps: dict, rows: list[dict], today: str) -> list[dict]:
    z, s, t = cfg["z2"], cfg["stop"], cfg["trend"]
    out = []
    for i, row in enumerate(rows):
        d, c = row["day"], row["close"]
        if d <= ps["last_date"] or d < cfg["daily_window"]["start"] or d > today:
            continue
        if c <= z["level"] and ps.get("z2_armed", True):
            ps["z2_armed"] = False
            out.append({"kind": "z2", "day": d, "close": c})
        elif c > z["rearm_above"] and not ps.get("z2_armed", True):
            ps["z2_armed"] = True
        if c < s["level"]:
            ps["below_streak"] = int(ps.get("below_streak") or 0) + 1
            if ps["below_streak"] >= s["days"] and ps.get("stop_armed", True):
                ps["stop_armed"] = False
                out.append({"kind": "stop", "day": d, "close": c, "prev_close": ps.get("last_close")})
        else:
            ps["below_streak"] = 0
            ps["stop_armed"] = True
        if t.get("enabled") and not ps.get("trend_sent"):
            hit, av = trend_hit(cfg, rows, i)
            if hit:
                ps["trend_sent"] = d
                out.append({"kind": "trend", "day": d, "close": c, "volume": row["volume"], "avg": av})
            elif av is None and c > t["level"]:
                print(f"{d} trend: 직전 {t['vol_days']}거래일 거래량 이력 부족, 판정 보류")
        ps["last_date"], ps["last_close"], ps["last_volume"] = d, c, row.get("volume")
    return out


def body(stamp: str, hdr: str, what: str, status: str, action: str, later: str, trim: str, avoid: str) -> str:
    return "\n".join([f"{stamp} | 010140 삼성중공업 | {hdr}", f"이게 뭐 알림: {what}", f"지금 결과/상태: {status}",
                      f"지금 할 일: {action}", f"나중에: {later}", f"익절/줄일 때: {trim}", f"하지 말 것: {avoid}"])


def dist(c: float, lv: float) -> str:
    return f"{(c / lv - 1) * 100:+.1f}% ({c - lv:+,.0f}원)"


def messages(cfg: dict, ev: list[dict], stamp: str) -> list[tuple[str, str, int]]:
    z, s, t, out = cfg["z2"], cfg["stop"], cfg["trend"], []
    for e in ev:
        c = e["close"]
        if e["kind"] == "z2":
            out.append((f"{TAG} 삼성중공업 | 종가 {c:,.0f}원, 15,200원 이하 2차 분할 매수 검토 구간", body(
                stamp, "15,200원 2차 분할 매수 검토", "삼성중공업(010140, KOSPI) 정규장 확정 종가가 15,200원 이하로 내려오면 오는 알림. 매매 지시 아님.",
                f"{e['day']} 종가 {c:,.0f}원 (판정 거래일 {e['day']}). 15,200원 대비 {dist(c, z['level'])}, 손절 기준 14,500원 대비 {dist(c, s['level'])}.",
                "2차 분할 매수 검토. 한 번에 다 사지 말고 나눠서 지정가로, 14,500원 손절 기준을 같이 정해 두기.",
                f"이 알림은 한 번만 옵니다. 종가가 {z['rearm_above']:,}원 넘게 회복한 뒤 다시 15,200원 이하로 오면 다시 알림. 감시는 2027-02-26 까지.",
                "종가 14,500원 미만 2일 연속이면 손절 기준 알림이 따로 옵니다.",
                "장중 가격만 보고 사기, 신용 및 미수, 손절 기준 없이 물타기. 투자 권유 아님."), z["priority"]))
        elif e["kind"] == "stop":
            pc = e.get("prev_close")
            prev = f" 직전 거래일 종가 {pc:,.0f}원." if pc else ""
            out.append((f"{TAG} 삼성중공업 | 종가 {c:,.0f}원, 14,500원 2일 연속 하회 손절 기준 도달", body(
                stamp, "14,500원 2일 연속 하회", "삼성중공업(010140) 정규장 확정 종가가 14,500원 미만으로 2거래일 연속 마감하면 오는 알림. 매매 지시 아님.",
                f"{e['day']} 종가 {c:,.0f}원 (판정 거래일 {e['day']}).{prev} 14,500원 대비 {dist(c, s['level'])}.",
                "손절 기준 도달. 계획대로 정리 또는 비중 축소를 검토하세요.",
                "이 알림은 한 번만 옵니다. 종가가 14,500원 이상으로 돌아온 뒤 다시 2일 연속 하회하면 다시 알림. 감시는 2027-02-26 까지.",
                "손절 기준이라 추가 매수보다 줄이는 쪽 먼저.",
                "기준을 미루며 버티기, 손절 구간에서 물타기. 투자 권유 아님."), s["priority"]))
        else:
            out.append((f"{TAG} 삼성중공업 | 종가 {c:,.0f}원, 16,500원 거래량 동반 돌파 추세 전환 확인", body(
                stamp, "16,500원 거래량 동반 돌파", "삼성중공업(010140) 종가가 16,500원을 넘고 거래량이 직전 20거래일 평균의 1.5배 이상인 날 1번만 오는 알림. 매매 지시 아님.",
                f"{e['day']} 종가 {c:,.0f}원 (판정 거래일 {e['day']}), 16,500원 대비 {dist(c, t['level'])}. "
                f"거래량 {e['volume']:,.0f}주, 직전 20거래일 평균 {e['avg']:,.0f}주의 {e['volume'] / e['avg']:.2f}배.",
                "추세 전환 확인. 보유 계획과 분할 매수 계획을 다시 점검하세요.",
                "이 알림은 다시 오지 않습니다. 15,200원, 14,500원 감시는 2027-02-26 까지 계속.",
                "오른 김에 일부 차익을 둘지 계획대로 판단.", "돌파 다음 날 추격 매수, 신용 및 미수. 투자 권유 아님."), t["priority"]))
    return out


def ops_msg(stamp: str, status: str) -> tuple[str, str]:
    return (f"{TAG} 삼성중공업 시세 조회 실패 | 원문 확인 필요", body(
        stamp, "데이터 읽기 실패", "010140 종가 감시의 데이터 읽기 실패 안내. 하루 1번. 매매 신호 아님.", status,
        "Daum 금융이나 네이버 증권에서 010140 일별 시세를 직접 보세요.", "다음 거래일 19:00 에 다시 읽고, 데이터 날짜로 이어서 판정합니다.",
        "이 안내로 계획을 바꾸지 않기.", "읽기 실패를 가격 신호로 읽기."))


def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(st: dict) -> None:
    STATE_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def check_text(title: str, msg: str) -> None:
    assert "\u00b7" not in title + msg and "\u318d" not in title + msg and "\u2014" not in title + msg, title
    assert title.startswith(TAG), title


def run(mode: str, now: datetime, cfg: dict, pub=publish, rows_fn=None, st: dict | None = None, save=True) -> int:
    dry = mode == "dry"
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    sday = session_day(now)
    today = sday.isoformat()
    st = load_state() if st is None else st
    topic, server = resolve_topic("port", cfg.get("ntfy_topic")), resolve_server(cfg.get("ntfy_server"))
    print(f"{stamp} {NAME} mode={mode} session={today}")
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
        if not rows:
            raise RuntimeError("확정 종가 0건")
        if not (st.get("price") or {}).get("last_date"):
            st["price"] = seed_state(cfg, rows, stamp)
            st["price"]["last_judged_at"] = stamp
            print(f"first run: state only, no alert: {st['price']}")
        else:
            ps = st["price"]
            before = ps["last_date"]
            ev = evaluate(cfg, ps, rows, today)
            out = messages(cfg, ev, stamp)
            expect = expected_day(sday, now)
            ps["pending"] = expect if ps["last_date"] < expect else None
            ps["last_judged_at"] = stamp
            judged = [r["day"] for r in rows if before < r["day"] <= today]
            print(f"judged trading dates: {judged or '없음(새 확정 종가 없음)'}; expected {expect}; latest row {rows[-1]['day']}")
            lag = lag_days(ps["last_date"], expect)
            if lag >= cfg.get("stale_trading_days", 2) and st.get("ops_day") != today:
                ops = ops_msg(stamp, f"010140 정규장 일별 시세가 {expect} 까지 와야 하는데 마지막 행이 {rows[-1]['day']} ({lag}거래일 지연). 다음 실행에서 이어서 판정.")
                st["ops_day"] = today
        print(f"price: {st['price']}")
    except Exception as exc:  # noqa: BLE001
        if st.get("ops_day") != today:
            ops = ops_msg(stamp, f"010140 시세 읽기 실패: {str(exc)[:160]}. state 는 그대로.")
            st["ops_day"] = today
    for title, msg, pri in out:
        check_text(title, msg)
        assert route_topic(topic, title) == topic, title
        print(f"TITLE: {title}\nPRIORITY: {pri} TOPIC: {route_topic(topic, title)}\n{msg}\n")
    if ops:
        check_text(*ops)
        print(f"OPS TITLE: {ops[0]} -> {route_topic(resolve_topic('ops'), ops[0])}\n{ops[1]}\n")
    if not out and not ops:
        print("보낼 것 없음")
    if dry:
        print("DRY RUN: no publish, state not written")
        return 0
    for title, msg, pri in out:
        pub(topic, title, msg, pri, ["moneybag"], server, config_name=NAME)
    if ops:
        try:
            pub(resolve_topic("ops"), ops[0], ops[1], 3, ["warning"], server, config_name=NAME)
        except Exception as exc:  # noqa: BLE001
            print(f"ops notice failed: {exc}")
    if save:
        save_state(st)
    return 0


def selftest() -> int:
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    base = [(f"2026-10-{d:02d}", 17000, 1000) for d in range(1, 31)]  # 거래량 평균 1,000 이력(날짜 형식만 맞춤)

    def mk(trip):
        return [{"day": d, "close": float(c), "volume": float(v)} for d, c, v in base + trip]
    st0 = lambda: {"last_date": "2026-11-03", "last_close": 17280.0, "z2_armed": True, "stop_armed": True, "below_streak": 0, "trend_sent": None}  # noqa: E731
    # z2: 15,200 이하 1번, 15,800 이하 회복은 재무장 안 함, 15,800 초과 뒤 재발송
    ps = st0()
    ev = evaluate(cfg, ps, mk([("2026-11-03", 17280, 1000), ("2026-11-04", 15200, 1000), ("2026-11-05", 15100, 1000),
                               ("2026-11-06", 15800, 1000), ("2026-11-09", 15000, 1000), ("2026-11-10", 15810, 1000),
                               ("2026-11-11", 15200, 1000)]), "2026-11-11")
    assert [(e["kind"], e["day"]) for e in ev] == [("z2", "2026-11-04"), ("z2", "2026-11-11")], ev
    assert ps["last_date"] == "2026-11-11" and not ps["z2_armed"], ps
    # stop: 14,500 미만 1일은 안 보냄, 2일 연속이면 p4 1번, 3일째 반복 안 함, 14,500 이상 뒤 재무장
    ps = st0()
    ev = evaluate(cfg, ps, mk([("2026-11-04", 14400, 1000), ("2026-11-05", 14500, 1000), ("2026-11-06", 14400, 1000),
                               ("2026-11-09", 14300, 1000), ("2026-11-10", 14200, 1000), ("2026-11-11", 14500, 1000),
                               ("2026-11-12", 14400, 1000), ("2026-11-13", 14400, 1000)]), "2026-11-13")
    assert [(e["kind"], e["day"]) for e in ev if e["kind"] == "stop"] == [("stop", "2026-11-09"), ("stop", "2026-11-13")], ev
    assert ev[1]["prev_close"] == 14400.0
    # trend: 16,500 초과 + 거래량 1.5배 이상만(11/5 1.49배 안 됨, 11/6 avg 1,024.5 의 1.503배), 1번만, 16,500 정확히는 안 됨, enabled=false 면 끔
    ps = st0()
    ev = evaluate(cfg, ps, mk([("2026-11-04", 16500, 1000), ("2026-11-05", 16600, 1490), ("2026-11-06", 16600, 1540),
                               ("2026-11-09", 18000, 9000)]), "2026-11-09")
    assert [(e["kind"], e["day"]) for e in ev] == [("trend", "2026-11-06")], ev
    assert ps["trend_sent"] == "2026-11-06"
    off = json.loads(json.dumps(cfg))
    off["trend"]["enabled"] = False
    assert evaluate(off, st0(), mk([("2026-11-06", 17000, 9000)]), "2026-11-06") == []
    # 거래량 이력 20일 미만이면 판정 보류
    short = [{"day": "2026-11-0%d" % i, "close": 17000.0, "volume": 1000.0} for i in range(3, 6)] + [{"day": "2026-11-06", "close": 17000.0, "volume": 9000.0}]
    assert evaluate(cfg, st0(), short, "2026-11-06") == []
    # seed: 이미 구간 안이면 발송한 것으로
    sd = seed_state(cfg, mk([("2026-11-02", 14400, 1000), ("2026-11-03", 14300, 1000)]), "T")
    assert sd["z2_armed"] is False and sd["stop_armed"] is False and sd["below_streak"] == 2 and sd["trend_sent"] is None, sd
    sd = seed_state(cfg, mk([("2026-11-03", 17280, 3000)]), "T")
    assert sd["z2_armed"] and sd["stop_armed"] and sd["trend_sent"] == "2026-11-03", sd
    sd = seed_state(cfg, mk([("2026-11-03", 17280, 1090)]), "T")
    assert sd["z2_armed"] and sd["stop_armed"] and sd["trend_sent"] is None and sd["below_streak"] == 0, sd
    # seed 날짜 이하, 감시 시작 전은 판정 안 함
    assert evaluate(cfg, st0(), mk([("2026-11-03", 14000, 9000)]), "2026-11-04") == []
    # 확정 종가: 15:40 전 당일 행 제외, 19:00 포함
    r = mk([("2026-11-03", 17280, 1000), ("2026-11-04", 15000, 1000)])
    assert confirmed(r, "2026-11-04", datetime(2026, 11, 4, 15, 30, tzinfo=KST))[-1]["day"] == "2026-11-03"
    assert confirmed(r, "2026-11-04", datetime(2026, 11, 4, 19, 0, tzinfo=KST))[-1]["day"] == "2026-11-04"
    assert expected_day(date(2026, 11, 4), datetime(2026, 11, 4, 19, 0, tzinfo=KST)) == "2026-11-04"
    assert expected_day(date(2026, 11, 4), datetime(2026, 11, 4, 10, 0, tzinfo=KST)) == "2026-11-03"
    assert lag_days("2026-11-03", "2026-11-04") == 1 and lag_days("2026-11-04", "2026-11-04") == 0
    assert lag_days("2026-11-02", "2026-11-04") == 2
    # run(): 19:00 당일 행 없으면 pending, 발송 없음; 다음 날 이어서 판정; 실패는 ops 1번, 같은 날 두 번 안 보냄
    sent = []
    pub = lambda t, ti, m, p, tags, srv, config_name=None: sent.append((t, ti, p))  # noqa: E731
    st = {"price": st0()}
    run("auto", datetime(2026, 11, 4, 19, 0, tzinfo=KST), cfg, pub, lambda c: mk([("2026-11-03", 17280, 1000)]), st, save=False)
    assert sent == [] and st["price"]["pending"] == "2026-11-04" and st["price"]["last_judged_at"].startswith("2026-11-04"), st
    run("auto", datetime(2026, 11, 5, 19, 0, tzinfo=KST), cfg, pub,
        lambda c: mk([("2026-11-03", 17280, 1000), ("2026-11-04", 15100, 1000), ("2026-11-05", 15000, 1000)]), st, save=False)
    assert [(s[0], s[2]) for s in sent] == [("cwally-port", 3)] and "15,100원" in sent[0][1] and st["price"]["last_date"] == "2026-11-05", sent
    assert st["price"]["pending"] is None
    # 2거래일 이상 지연: ops 1번
    st2 = {"price": st0()}
    run("auto", datetime(2026, 11, 5, 19, 0, tzinfo=KST), cfg, pub, lambda c: mk([("2026-11-03", 17280, 1000)]), st2, save=False)
    assert sent[-1][0] == "cwally-ops" and "조회 실패" in sent[-1][1], sent
    n = len(sent)
    run("auto", datetime(2026, 11, 5, 20, 0, tzinfo=KST), cfg, pub, lambda c: mk([("2026-11-03", 17280, 1000)]), st2, save=False)
    assert len(sent) == n

    def boom(c):
        raise RuntimeError("x")
    run("auto", datetime(2026, 11, 6, 19, 0, tzinfo=KST), cfg, pub, boom, st, save=False)
    run("auto", datetime(2026, 11, 6, 20, 0, tzinfo=KST), cfg, pub, boom, st, save=False)
    assert [s[0] for s in sent[n:]] == ["cwally-ops"] and "조회 실패" in sent[n][1], sent
    # 기간 밖, 휴장: 조용히
    n = len(sent)
    run("auto", datetime(2027, 3, 2, 19, 0, tzinfo=KST), cfg, pub, boom, st, save=False)
    run("auto", datetime(2026, 11, 7, 19, 0, tzinfo=KST), cfg, pub, boom, st, save=False)
    assert len(sent) == n
    # dry: 발송 없음
    run("dry", datetime(2026, 11, 10, 19, 0, tzinfo=KST), cfg, pub, boom, {"price": st0()}, save=False)
    assert len(sent) == n
    # 문구 규칙, 제목 형식
    msgs = messages(cfg, [{"kind": "z2", "day": "2026-11-04", "close": 15100.0},
                          {"kind": "stop", "day": "2026-11-05", "close": 14400.0, "prev_close": 14450.0},
                          {"kind": "trend", "day": "2026-11-06", "close": 16600.0, "volume": 1500.0, "avg": 1000.0}], "T")
    assert msgs[0][0] == "[010140] 삼성중공업 | 종가 15,100원, 15,200원 이하 2차 분할 매수 검토 구간", msgs[0][0]
    assert msgs[1][0] == "[010140] 삼성중공업 | 종가 14,400원, 14,500원 2일 연속 하회 손절 기준 도달", msgs[1][0]
    assert msgs[2][0] == "[010140] 삼성중공업 | 종가 16,600원, 16,500원 거래량 동반 돌파 추세 전환 확인", msgs[2][0]
    assert [m[2] for m in msgs] == [3, 4, 3] and "1.50배" in msgs[2][1]
    o = ops_msg("T", "x")
    assert o[0] == "[010140] 삼성중공업 시세 조회 실패 | 원문 확인 필요", o[0]
    for t, m, _ in msgs + [(*o, 3)]:
        check_text(t, m)
        assert ("조회 실패" in t) == (t == o[0]), t
        assert route_topic("cwally-port", t) == ("cwally-ops" if "조회 실패" in t else "cwally-port"), t
    print("selftest ok")
    return 0


def main() -> int:
    args = sys.argv[1:]
    mode = (args[0] if args and not args[0].startswith("--") else "auto").lower()
    if mode == "selftest":
        return selftest()
    if mode not in {"auto", "dry"}:
        print(f"usage: {NAME}_monitor.py <auto|dry|selftest> [--as-of 'YYYY-MM-DD HH:MM']")
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
