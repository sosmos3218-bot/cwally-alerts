#!/usr/bin/env python3
"""[0085P0] ACE 미국10년국채액티브(환노출, 0085P0) 분할 매수 판단 보조 알림. 매매 신호 아님, 자동 주문 아님.

기존 평일 09:00 KST morning 슬롯에 붙는다 (새 cron 없음). 선과 재무장은 configs/k0085p0.json lines:
  fx_low      원/달러 종가 < 1,285 -> 1번 (하향). 종가 > 1,300 이면 다시 무장.
  y10_high    미국 10년 종가 > 5.30% -> 1번 (하향). 종가 < 5.20% 이면 다시 무장.
  y10_settle  미국 10년 종가 < 4.50% 3영업일 연속 -> 1번 (상향, 안착). 종가 > 4.60% 이면 다시 무장.
데이터: 10년 = 재무부 Daily Treasury Par Yield 10 Yr 우선, FRED DGS10 교차 확인과 빈 날짜 채움.
        원/달러 = 네이버 FX_USDKRW 일별 종가(오늘 KST 행 제외) 우선, ECOS 731Y001 매매기준율 교차 확인과 대체.
조회 함수는 usbond_monitor 것을 그대로 쓴다. series 별 last_date 로 중복 방지, 첫 실행은 조용히 상태만 기록.
조회 실패(전 출처 실패 또는 값이 max_age_days 보다 오래됨)와 스크립트 오류는 cwally-ops 하루 1번.
종료: ends_kst 2027-02-26 23:59 KST 뒤에는 어떤 모드든 조용히 끝(발송, 실패 안내, state 쓰기 없음).
11/26 금통위 요약(configs mpc_summary, 2026-11-02 승인): `mpc auto|dry` 를 15:20 afternoon 과 19:00 k203780 슬롯에서 부름.
  그날 10:30 뒤 결정이 확인되면 p3 요약 1번, state mpc.<날짜>.done 으로 중복 방지. 15:20 에 확인 안 되면 19:00 에 재시도,
  final_hm(18:30) 이후에도 안 되면 '확인 불가' p3 1번(cwally-ops) 뒤 끝. 결정 판정은 krrate_monitor.mpc_decision 재사용.
modes: auto | dry (조회 + 판정 출력, 발송 없음, state 안 씀) | selftest (가짜 값, 네트워크 없음, assert)
       | mpc auto | mpc dry [--as-of "YYYY-MM-DD HH:MM"] (dry 는 판정 출력과 예시 미리보기만)
"""
from __future__ import annotations

import copy
import json
import sys
import traceback
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server  # noqa: E402
from kr_calendar import KST  # noqa: E402
import usbond_monitor as ub  # noqa: E402

NAME = "k0085p0"
CFG_PATH = ROOT / "configs" / f"{NAME}.json"
STATE_PATH = ROOT / "state" / f"{NAME}.json"
TICKERS = "(0085P0, 0085N0)"
END_LINE = "꺼지는 때: 2027-02-26 이후 자동 발송 없음"
DISCLAIMER = "참고용, 투자 권유 아님"
DEC_KO = {"hold": "동결", "hike": "인상", "cut": "인하"}


def ended(now: datetime, cfg: dict) -> bool:
    e = cfg.get("ends_kst")
    return bool(e) and now >= datetime.fromisoformat(e)


def load_cfg() -> dict:
    return json.loads(CFG_PATH.read_text(encoding="utf-8"))


def load_state() -> dict:
    st = {"fx": {"last_date": None, "armed": True},
          "y10": {"last_date": None, "high_armed": True, "settle_armed": True},
          "ops_day": None, "err_day": None, "updated_at": None}
    if STATE_PATH.exists():
        try:
            for k, v in json.loads(STATE_PATH.read_text(encoding="utf-8")).items():
                if isinstance(v, dict) and isinstance(st.get(k), dict):
                    st[k].update(v)
                else:
                    st[k] = v
        except json.JSONDecodeError:
            pass
    return st


def save_state(st: dict) -> None:
    STATE_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# ---------------- 조회 ----------------
def fetch_all(now: datetime) -> dict:
    today = now.date()
    start = today - timedelta(days=40)
    data: dict = {"y10": {}, "fx": {}, "fred": {}, "ecos": {}, "errors": []}

    def tryget(label, fn):
        try:
            got = fn()
            if not got:
                raise RuntimeError("빈 응답")
            return got
        except Exception as exc:  # noqa: BLE001
            data["errors"].append(f"{label}: {type(exc).__name__} {str(exc)[:80]}")
            return {}

    tre = tryget("재무부 par yield", lambda: ub.treasury_10y(start, today))
    fred = tryget("FRED DGS10", lambda: ub.fred_dgs10(start))
    nav = tryget("네이버 FX_USDKRW", ub.naver_fx)
    ecos = tryget("ECOS 매매기준율", lambda: ub.ecos_fx(start, today))
    iso = today.isoformat()
    # 10년: 미국 날짜 < 오늘 KST (09:00 KST 엔 전 미국 영업일까지만 확정)
    for d, v in tre.items():
        if d < iso:
            data["y10"][d] = (v, "재무부")
    for d, v in fred.items():
        if d < iso:
            data["fred"][d] = v
            data["y10"].setdefault(d, (v, "FRED"))
    # 원/달러: 오늘 KST 행은 장중 값이라 제외
    for d, v in nav.items():
        if d < iso:
            data["fx"][d] = (v, "네이버 종가")
    for d, v in ecos.items():
        if d < iso:
            data["ecos"][d] = v
            data["fx"].setdefault(d, (v, "ECOS 매매기준율"))
    data["y10"] = dict(sorted(data["y10"].items()))
    data["fx"] = dict(sorted(data["fx"].items()))
    return data


# ---------------- 판정 ----------------
def new_dates(ser: dict, last: str | None, cap: int) -> list[str]:
    ds = [d for d in ser if last is None or d > last]
    return ds[-cap:]


def eval_fx(st: dict, ser: dict, cfg: dict) -> list[dict]:
    ln = cfg["lines"]["fx_low"]
    s = st["fx"]
    events = []
    if not ser:
        return events
    if s["last_date"] is None:
        d = list(ser)[-1]
        s.update({"last_date": d, "armed": not ser[d][0] < ln["level"], "seeded": d})
        return events
    for d in new_dates(ser, s["last_date"], int(cfg["catchup_dates"])):
        v = ser[d][0]
        if s["armed"] and v < ln["level"]:
            events.append({"key": "fx_low", "date": d, "value": v, "src": ser[d][1]})
            s["armed"] = False
        elif not s["armed"] and v > ln["rearm_above"]:
            s["armed"] = True
        s["last_date"] = d
    return events


def settled(ser: dict, d: str, level: float, n: int) -> bool:
    ds = [x for x in ser if x <= d][-n:]
    return len(ds) == n and all(ser[x][0] < level for x in ds)


def eval_y10(st: dict, ser: dict, cfg: dict) -> list[dict]:
    hi, se = cfg["lines"]["y10_high"], cfg["lines"]["y10_settle"]
    s = st["y10"]
    events = []
    if not ser:
        return events
    if s["last_date"] is None:
        d = list(ser)[-1]
        s.update({"last_date": d, "high_armed": not ser[d][0] > hi["level"],
                  "settle_armed": not settled(ser, d, se["level"], int(se["days"])), "seeded": d})
        return events
    for d in new_dates(ser, s["last_date"], int(cfg["catchup_dates"])):
        v = ser[d][0]
        if s["high_armed"] and v > hi["level"]:
            events.append({"key": "y10_high", "date": d, "value": v, "src": ser[d][1]})
            s["high_armed"] = False
        elif not s["high_armed"] and v < hi["rearm_below"]:
            s["high_armed"] = True
        if s["settle_armed"] and settled(ser, d, se["level"], int(se["days"])):
            ds = [x for x in ser if x <= d][-int(se["days"]):]
            events.append({"key": "y10_settle", "date": d, "value": v, "src": ser[d][1],
                           "run": [(x, ser[x][0]) for x in ds]})
            s["settle_armed"] = False
        elif not s["settle_armed"] and v > se["rearm_above"]:
            s["settle_armed"] = True
        s["last_date"] = d
    return events


def stale(ser: dict, now: datetime, max_age: int) -> bool:
    if not ser:
        return True
    return (now.date() - date.fromisoformat(list(ser)[-1])).days > max_age


# ---------------- 문구 ----------------
def md(d: str) -> str:
    return f"{int(d[5:7])}/{int(d[8:10])}"


def fx_txt(v: float) -> str:
    return f"{v:,.1f}원"


def latest(ser: dict) -> tuple[str, float, str] | None:
    if not ser:
        return None
    d = list(ser)[-1]
    return d, ser[d][0], ser[d][1]


def context_line(data: dict) -> str:
    f, y = latest(data["fx"]), latest(data["y10"])
    a = f"원/달러 {fx_txt(f[1])}({md(f[0])} {f[2]})" if f else "원/달러 미확인"
    b = f"미국 10년 {y[1]:.2f}%({md(y[0])} {y[2]})" if y else "미국 10년 미확인"
    return f"참고 현재값: {a}, {b}."


def cross_note(ev: dict, data: dict, cfg: dict) -> str:
    d = ev["date"]
    if ev["key"] == "fx_low":
        e = data["ecos"].get(d)
        return f" ECOS 매매기준율 같은 날 {fx_txt(e)}." if e is not None and ev["src"] != "ECOS 매매기준율" else ""
    f = data["fred"].get(d)
    if f is None or ev["src"] == "FRED":
        return " FRED DGS10 같은 날 값 아직 없음." if f is None else ""
    if abs(f - ev["value"]) > float(cfg["y10_cross_tol"]):
        return f" FRED DGS10 {f:.2f}% 로 차이 있음(판정은 재무부 값)."
    return f" FRED DGS10 {f:.2f}% 일치."


def body(lines: list[str]) -> str:
    return "\n".join(lines)


def build(ev: dict, data: dict, cfg: dict, now: datetime) -> tuple[str, str, int]:
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    ln = cfg["lines"][ev["key"]]
    head = f"{stamp} | 0085P0 | {ln['kind']} 트리거"
    what = "이게 뭐 알림: ACE 미국10년국채액티브 환노출(0085P0) 분할 매수 판단 보조. 미리 정한 기준선을 넘었는지만 알립니다. 매매 신호 아님."
    ctx = context_line(data)
    if ev["key"] == "fx_low":
        title = f"[0085P0] 원/달러 1,285원 하회 | 분할 매수 판단 하향 {TICKERS}"
        lines = [head, what,
                 f"지금 결과: 원/달러 {md(ev['date'])} 종가 {fx_txt(ev['value'])} ({ev['src']}), 하향 기준 {fx_txt(ln['level'])} 아래.{cross_note(ev, data, cfg)} {ctx}",
                 "지금 할 일: 0085P0 신규 분할 매수 보류. 환노출형은 원화 강세가 이어지면 금리가 내려도 원화 손익이 깎입니다. 계속 사려면 환헤지형 0085N0 와 비교.",
                 f"나중에: 원/달러 종가가 {fx_txt(ln['rearm_above'])} 위로 올라오면 다시 무장(다음 하회 때 다시 알림).",
                 "익절/줄일 때: 보유분이 있으면 원화 기준 손익을 보고 일부 줄이기 또는 0085N0 로 옮기기 검토.",
                 "하지 말 것: 하루 값만 보고 전량 매도, 환율 바닥을 추정해서 몰아서 사기.", END_LINE]
    elif ev["key"] == "y10_high":
        title = f"[0085P0] 미국 10년 5.30% 상회 | 분할 매수 판단 하향 {TICKERS}"
        lines = [head, what,
                 f"지금 결과: 미국 10년 {md(ev['date'])} 종가 {ev['value']:.2f}% ({ev['src']}), 하향 기준 {ln['level']:.2f}% 위. 10월 고점은 5.31%(10/5).{cross_note(ev, data, cfg)} {ctx}",
                 "지금 할 일: 금리 상승이 이어질 수 있는 구간. 0085P0 분할 매수는 회차를 늘리지 말고 보류 또는 계획한 소액 회차만.",
                 f"나중에: 10년 종가가 {ln['rearm_below']:.2f}% 아래로 내려오면 다시 무장. 4.50% 아래 3일 안착이면 상향 알림.",
                 "익절/줄일 때: 해당 없음(금리 상승은 가격 하락 구간). 손실 한도를 정해 두었으면 그 기준대로.",
                 "하지 말 것: 고점 추정해서 한 번에 크게 사기, 신용이나 미수로 사기.", END_LINE]
    else:
        run = ", ".join(f"{md(d)} {v:.2f}%" for d, v in ev["run"])
        title = f"[0085P0] 미국 10년 4.50% 아래 {ln['days']}일 안착 | 분할 매수 판단 상향 {TICKERS}"
        lines = [head, what,
                 f"지금 결과: 미국 10년 종가 {run} ({ev['src']}), {ln['days']}영업일 연속 {ln['level']:.2f}% 아래.{cross_note(ev, data, cfg)} {ctx}",
                 "지금 할 일: 0085P0 분할 매수 판단 상향 검토(계획 회차 진행). 단 원/달러가 1,285원 아래면 환 손실 위험이 커서 상향 보류.",
                 f"나중에: 10년 종가가 {ln['rearm_above']:.2f}% 위로 올라가면 다시 무장.",
                 "익절/줄일 때: 금리 하락으로 평가이익이 나면 계획한 익절 구간에서 일부 차익 검토.",
                 "하지 말 것: 남은 금액 한 번에 몰아서 사기, 환율 확인 없이 환노출형만 늘리기.", END_LINE]
    return title, body(lines), int(ln["priority"])


def ops_msg(now: datetime, what: str) -> tuple[str, str]:
    title = f"[0085P0] 금리/환율 조회 실패 | 판정 보류 {TICKERS}"
    b = body([f"{now.strftime('%Y-%m-%d %H:%M KST')} | 0085P0",
              "이게 뭐 알림: 0085P0 판단 보조에 쓰는 값을 받지 못했습니다. 매매 신호 아님.",
              f"지금 상태: {what}",
              "지금 할 일: 필요하면 재무부 10년 금리와 원/달러 종가를 직접 확인.",
              "나중에: 다음 평일 09:00 실행에서 다시 조회, 밀린 날짜는 따라잡아 판정.",
              "익절/줄일 때: 해당 없음.",
              "하지 말 것: 이 알림을 하향이나 상향 신호로 읽기.", END_LINE])
    return title, b


# ---------------- 실행 ----------------
def run(mode: str, now: datetime, cfg: dict, data: dict | None = None, st: dict | None = None) -> tuple[dict, list]:
    st = copy.deepcopy(st if st is not None else load_state())
    if ended(now, cfg):
        print(f"k0085p0 ended ({cfg['ends_kst']}): silent, no fetch, no state")
        return st, []
    data = data if data is not None else fetch_all(now)
    for e in data["errors"]:
        print("fetch:", e)
    events = eval_fx(st, data["fx"], cfg) + eval_y10(st, data["y10"], cfg)
    msgs = [build(ev, data, cfg, now) for ev in events]
    bad = [lab for lab, key in (("원/달러", "fx"), ("미국 10년", "y10")) if stale(data[key], now, int(cfg["max_age_days"]))]
    today = now.strftime("%Y-%m-%d")
    ops = None
    if bad and st.get("ops_day") != today:
        ops = ops_msg(now, f"{', '.join(bad)} 값 없음 또는 {cfg['max_age_days']}일 넘게 갱신 안 됨. 오류: " + (" / ".join(data["errors"]) or "없음"))
        st["ops_day"] = today
    st["updated_at"] = now.isoformat(timespec="seconds")
    if mode in ("dry", "selftest"):
        print("DRY RUN" if mode == "dry" else "SELFTEST", "| fx", latest(data["fx"]), "| y10", latest(data["y10"]))
        print("state after:", json.dumps({k: st[k] for k in ("fx", "y10")}, ensure_ascii=False))
        for t, b, p in msgs:
            print(f"WOULD SEND p{p}: {t}\n{b}")
        if ops:
            print("WOULD SEND OPS:", ops[0])
        if not msgs:
            print("no signal")
        return st, msgs
    server = resolve_server(cfg.get("ntfy_server"))
    for t, b, p in msgs:
        publish(cfg["ntfy_topic"], t, b, p, ["chart_with_downwards_trend" if p >= 4 else "chart_with_upwards_trend"], server)
    if ops:
        publish(cfg["ntfy_ops_topic"], ops[0], ops[1], 3, ["warning"], server)
    save_state(st)
    print(f"k0085p0 done: sent={len(msgs)} fx={st['fx']} y10={st['y10']}")
    return st, msgs


def _ser(vals: dict, src: str) -> dict:
    return {d: (v, src) for d, v in sorted(vals.items())}


def _days(start: str, n: int) -> list[str]:
    out, d = [], date.fromisoformat(start)
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def selftest() -> int:
    cfg = load_cfg()
    days = _days("2026-10-01", 30)
    now = datetime.fromisoformat(days[-1]).replace(hour=9, tzinfo=KST) + timedelta(days=1)

    def data(fx: list[float], y: list[float]) -> dict:
        ds = days[-len(fx):]
        return {"fx": _ser(dict(zip(ds, fx)), "네이버 종가"), "y10": _ser(dict(zip(days[-len(y):], y)), "재무부"),
                "fred": {}, "ecos": {}, "errors": []}

    # 1) 첫 실행: 조건이 거짓이면 조용히 무장
    st, msgs = run("selftest", now, cfg, data([1335.8], [4.94]), load_state_blank())
    assert not msgs and st["fx"]["armed"] and st["y10"]["high_armed"] and st["y10"]["settle_armed"]
    # 1b) 첫 실행에서 이미 참이면 발송 없이 무장 해제
    st, msgs = run("selftest", now, cfg, data([1280.0], [5.35]), load_state_blank())
    assert not msgs and not st["fx"]["armed"] and not st["y10"]["high_armed"]
    # 2) 원/달러 하회 1번, 같은 날짜 재실행 무발송, 1,300 위에서 재무장 후 다시 1번
    base = load_state_blank()
    base["fx"].update({"last_date": days[-6], "armed": True})
    base["y10"].update({"last_date": days[-1]})
    d = data([1290.0, 1284.0, 1283.0, 1295.0, 1301.0], [4.9])
    st, msgs = run("selftest", now, cfg, d, base)
    assert [m[0] for m in msgs] == [f"[0085P0] 원/달러 1,285원 하회 | 분할 매수 판단 하향 {TICKERS}"], msgs
    assert st["fx"]["armed"] and st["fx"]["last_date"] == days[-1]
    st2, msgs2 = run("selftest", now, cfg, d, st)
    assert not msgs2
    # 3) 10년 5.30% 상회 1번, 5.20 아래로 재무장
    base = load_state_blank()
    base["fx"].update({"last_date": days[-1]})
    base["y10"].update({"last_date": days[-5]})
    st, msgs = run("selftest", now, cfg, data([1330.0], [5.25, 5.31, 5.35, 5.22, 5.19]), base)
    assert len(msgs) == 1 and "5.30% 상회" in msgs[0][0] and msgs[0][2] == 4 and st["y10"]["high_armed"]
    # 4) 4.50 아래 3일 안착: 2일째까지 없음, 3일째 1번, 4.60 위에서 재무장
    base = load_state_blank()
    base["fx"].update({"last_date": days[-1]})
    base["y10"].update({"last_date": days[-6]})
    st, msgs = run("selftest", now, cfg, data([1330.0], [4.55, 4.49, 4.48, 4.47, 4.46, 4.61]), base)
    assert len(msgs) == 1 and "4.50% 아래 3일 안착" in msgs[0][0] and msgs[0][2] == 3, msgs
    assert st["y10"]["settle_armed"]
    st, msgs = run("selftest", now, cfg, data([1330.0], [4.55, 4.49, 4.48, 4.55, 4.47, 4.46]), dict(base, y10=dict(base["y10"], last_date=days[-4])))
    assert not msgs, "중간에 4.55 가 끼면 연속 아님"
    # 5) 조회 실패: ops 1번, 같은 날 두 번째는 없음
    st, msgs = run("selftest", now, cfg, {"fx": {}, "y10": {}, "fred": {}, "ecos": {}, "errors": ["x"]}, load_state_blank())
    assert st["ops_day"] == now.strftime("%Y-%m-%d")
    # 6) 문구 규칙: 티커, 금지 문자, ops 단어는 ops 제목에만
    titles = []
    for key, ev in (("fx_low", {"value": 1280.0}), ("y10_high", {"value": 5.35}), ("y10_settle", {"value": 4.4, "run": [(days[-1], 4.4)]})):
        t, b, _ = build(dict(ev, key=key, date=days[-1], src="x"), data([1280.0], [4.4]), cfg, now)
        titles.append(t)
        assert "0085P0" in t and "0085N0" in t, t
        assert "\u00b7" not in t + b and "\u2014" not in t + b
        assert "조회 실패" not in t
    ot, ob = ops_msg(now, "x")
    assert "조회 실패" in ot and "0085P0" in ot and "\u00b7" not in ot + ob and "\u2014" not in ot + ob
    n = mpc_selftest(cfg, now, days, data)
    print(f"SELFTEST OK: existing line checks + {n} ends/mpc checks")
    return 0


# ---------------- 11/26 금통위 요약 (2026-11-02 승인 pt32u, one-shot) ----------------
def mkt_snapshot(now: datetime) -> dict:
    """요약용 최신값. 원/달러는 오늘 행(장중 또는 당일 마감)도 씀. 실패 항목은 None, errors 에 남김."""
    today = now.date()
    start = today - timedelta(days=14)
    out: dict = {"fx": None, "y10": None, "etf": {}, "errors": []}
    try:
        ser = ub.naver_fx()
        d = max(ser)
        out["fx"] = (d, ser[d], "네이버 최신" if d == today.isoformat() else "네이버 종가")
    except Exception as exc:  # noqa: BLE001
        out["errors"].append(f"네이버 FX {type(exc).__name__}")
        try:
            ser = ub.ecos_fx(start, today)
            d = max(ser)
            out["fx"] = (d, ser[d], "ECOS 매매기준율")
        except Exception as exc2:  # noqa: BLE001
            out["errors"].append(f"ECOS FX {type(exc2).__name__}")
    for label, fn in (("재무부", lambda: ub.treasury_10y(start, today)), ("FRED", lambda: ub.fred_dgs10(start))):
        try:
            ser = fn()
            d = max(ser)
            out["y10"] = (d, ser[d], f"{label} 종가")
            break
        except Exception as exc:  # noqa: BLE001
            out["errors"].append(f"{label} 10년 {type(exc).__name__}")
    for code in ("0085P0", "0085N0"):
        try:
            b = json.loads(ub.get(f"https://m.stock.naver.com/api/stock/{code}/basic", 20))
            px = float(str(b["closePrice"]).replace(",", ""))
            at = str(b.get("localTradedAt") or "")
            out["etf"][code] = (px, at[:16].replace("T", " "), "장중" if b.get("marketStatus") == "OPEN" else "종가")
        except Exception as exc:  # noqa: BLE001
            out["errors"].append(f"네이버 {code} {type(exc).__name__}")
    return out


def dist_lines(mkt: dict, cfg: dict) -> list[str]:
    fl, hi, se = cfg["lines"]["fx_low"]["level"], cfg["lines"]["y10_high"]["level"], cfg["lines"]["y10_settle"]["level"]
    out = []
    if mkt.get("fx"):
        d, v, src = mkt["fx"]
        gap = (v / fl - 1) * 100
        rel = f"1,285원 하회선보다 {v - fl:,.1f}원({gap:.1f}%) 위" if v >= fl else f"1,285원 하회선 아래 {fl - v:,.1f}원({-gap:.1f}%)"
        out.append(f"- 원/달러 {fx_txt(v)} ({md(d)} {src}), {rel}")
    else:
        out.append("- 원/달러 확인 불가")
    if mkt.get("y10"):
        d, v, src = mkt["y10"]
        a = f"5.30% 상회선까지 {hi - v:.2f}%p 남음" if v <= hi else f"5.30% 상회선 위 {v - hi:.2f}%p"
        b = f"4.50% 안착선보다 {v - se:.2f}%p 위" if v >= se else f"4.50% 아래 {se - v:.2f}%p (3일 연속이면 안착 알림)"
        out.append(f"- 미국 10년 {v:.2f}% ({md(d)} {src}), {a}, {b}")
    else:
        out.append("- 미국 10년 확인 불가")
    parts = []
    for code, nm in (("0085P0", "환노출"), ("0085N0", "환헤지")):
        e = mkt.get("etf", {}).get(code)
        parts.append(f"{code}({nm}) {e[0]:,.0f}원 ({md(e[1][:10])} {e[1][11:16]} {e[2]})" if e else f"{code}({nm}) 확인 불가")
    out.append("- " + ", ".join(parts))
    return out


MEANING = {
    "hike": "의미: 인상은 한미 금리차를 줄여 원화 강세 쪽 압력. 환노출 0085P0 는 원/달러가 내리면 원화 손익이 깎이고, 환헤지 0085N0 는 환 영향이 작고 헤지 비용이 줄어드는 쪽. 미국 10년 자체에는 직접 영향 작음.",
    "hold": "의미: 동결이면 원화 쪽 추가 압력은 덜함. 0085P0 와 0085N0 차이는 계속 원/달러와 미국 10년이 정함. 다음 회의 인상 기대가 남으면 원화 강세 압력도 남음.",
    "cut": "의미: 인하는 한미 금리차를 넓혀 원화 약세 쪽. 환노출 0085P0 에 유리하고, 환헤지 0085N0 는 헤지 비용이 늘어나는 쪽.",
}
TODO = {
    "hike": "지금 할 일: 0085P0 분할 계획은 그대로 두고, 다음 회차 전에 원/달러와 1,285원 선 거리만 확인(가까우면 그 회차는 0085N0 와 비교).",
    "hold": "지금 할 일: 할 일 없음. 계획 회차 그대로, 기준선 3개는 평일 09:00 알림이 계속 봄.",
    "cut": "지금 할 일: 할 일 없음. 계획 회차 그대로, 환노출 0085P0 비중 유지.",
}


def mpc_summary_msg(cfg: dict, d: str, dec: dict, mkt: dict, now: datetime) -> tuple[str, str, int]:
    mc = cfg["mpc_summary"]
    dk = DEC_KO.get(dec["decision"], dec["decision"])
    rate = float(dec["rate"])
    prev = dec.get("prev") if dec.get("prev") is not None else mc["baseline"]["rate"]
    title = f"[0085P0] {md(d)} 금통위 결과 요약 | {rate:.2f}% {dk}"
    vote = dec.get("dissent") or "소수의견 확인 안 됨"
    src = dec.get("source") or "확인 불가"
    link = dec.get("url") or mc["release_list_url"]
    exp = "예상대로" if dec["decision"] == "hike" else "예상과 다름"
    lines = [f"{now.strftime('%Y-%m-%d %H:%M KST')} | 0085P0 | {md(d)} 금통위 결과 요약",
             "이게 뭐 알림: 이번 금통위 결과가 0085P0(환노출)와 0085N0(환헤지)에 주는 의미만 1번 정리. 결정문 문구, 국고채, 물가는 [KR-RATE] 19:00 요약 몫이라 뺌. 매매 신호 아님.",
             f"지금 결과: 기준금리 {float(prev):.2f}% -> {rate:.2f}% {dk}. {vote}. 출처: {src} {link}",
             f"- 배경: {mc['baseline']['history']}. {mc['survey']['text']}, 결과는 {exp}.",
             *dist_lines(mkt, cfg),
             MEANING.get(dec["decision"], MEANING["hold"]),
             TODO.get(dec["decision"], TODO["hold"]),
             END_LINE, DISCLAIMER]
    return title, body(lines), int(mc["priority"])


def mpc_fail_msg(cfg: dict, d: str, why: str, now: datetime) -> tuple[str, str, int]:
    title = f"[0085P0] {md(d)} 금통위 결과 확인 불가 | 요약 보류"
    b = body([f"{now.strftime('%Y-%m-%d %H:%M KST')} | 0085P0 | {md(d)} 금통위 요약",
              "이게 뭐 알림: 금통위 요약에 쓸 결정을 15:20, 19:00 슬롯 모두에서 확인하지 못했습니다. 매매 신호 아님.",
              f"지금 상태: {why}",
              "지금 할 일: 필요하면 한국은행 통화정책방향 보도자료에서 결정을 직접 확인.",
              "나중에: 이 요약은 다시 보내지 않음. 결정 자체는 [KR-RATE] 와 [BOK] 알림이 따로 다룸.",
              END_LINE, DISCLAIMER])
    return title, b, int(cfg["mpc_summary"]["priority"])


def mpc_run(mode: str, now: datetime, cfg: dict, st: dict, dec_fn, mkt_fn, pub=None, save=None) -> tuple[dict, list, str]:
    """returns (state, [(title, body, prio, topic)], why). dry: 발송 없음, state 안 씀."""
    st = copy.deepcopy(st)
    if ended(now, cfg):
        return st, [], "ended"
    mc = cfg["mpc_summary"]
    d = mc["date"]
    if now.strftime("%Y-%m-%d") != d:
        return st, [], f"not {d}"
    mark = st.setdefault("mpc", {}).setdefault(d, {})
    if mark.get("done"):
        return st, [], f"already done ({mark.get('result')})"
    if now.strftime("%H:%M") < mc["ready_hm"]:
        return st, [], f"before {mc['ready_hm']}"
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    try:
        dec = dec_fn(d)
    except Exception as exc:  # noqa: BLE001
        dec = {"status": "unknown", "why": f"{type(exc).__name__}: {str(exc)[:120]}"}
    print(f"mpc {d} decision: {dec}")
    out = []
    if dec.get("status") == "ok" and dec.get("rate") is not None and dec.get("decision") in DEC_KO:
        t, b, p = mpc_summary_msg(cfg, d, dec, mkt_fn(now), now)
        out.append((t, b, p, cfg["ntfy_topic"]))
        mark.update({"done": True, "result": "sent", "decision": dec["decision"], "rate": dec["rate"], "at": stamp})
        why = "sent"
    elif now.strftime("%H:%M") >= mc["final_hm"]:
        t, b, p = mpc_fail_msg(cfg, d, f"{dec.get('status')}: {dec.get('why', '')}", now)
        out.append((t, b, p, cfg["ntfy_ops_topic"]))
        mark.update({"done": True, "result": "unconfirmed", "at": stamp})
        why = "unconfirmed final"
    else:
        mark.setdefault("tries", []).append(f"{stamp} {dec.get('status')}")
        why = "unconfirmed, retry next slot"
    for t, b, p, tp in out:
        assert not any(ch in t + b for ch in "\u00b7\u318d\u2014") and "매수" not in t and "신호" not in t, t
        print(f"{'WOULD SEND' if mode != 'auto' else ('SEND' if pub is None else 'SELFTEST FAKE SEND (not published)')} p{p} -> {tp}: {t}\n{b}")
    if mode != "auto":
        return st, out, why
    server = resolve_server(cfg.get("ntfy_server"))
    for t, b, p, tp in out:
        (pub or publish)(tp, t, b, p, ["bank"], server, config_name=NAME)
    (save or save_state)(st)
    return st, out, why


def mpc_main(args: list[str]) -> int:
    mode = args[0] if args else "dry"
    if mode not in ("auto", "dry"):
        print(f"unknown mpc mode {mode}")
        return 2
    cfg = load_cfg()
    now = datetime.now(KST)
    if "--as-of" in args and mode == "dry":
        now = datetime.strptime(args[args.index("--as-of") + 1], "%Y-%m-%d %H:%M").replace(tzinfo=KST)
    if ended(now, cfg):
        print(f"k0085p0 mpc ended ({cfg['ends_kst']}): silent")
        return 0
    import krrate_monitor as kr  # noqa: E402  (읽기 전용: mpc_decision)
    kcfg = json.loads((ROOT / "configs" / "krrate.json").read_text(encoding="utf-8"))
    try:
        _, out, why = mpc_run(mode, now, cfg, load_state(), lambda d: kr.mpc_decision(kcfg, d), mkt_snapshot)
    except Exception:  # noqa: BLE001
        traceback.print_exc()  # 다음 슬롯이 재시도, 19:00 에도 안 되면 그때 확인 불가 1번
        why = "error"
    print(f"k0085p0 mpc {mode}: {why}")
    if mode == "dry":
        print("DRY RUN: no publish, state not written")
        ex = {"fx": ("2026-11-26", 1328.4, "네이버 최신"), "y10": ("2026-11-25", 4.93, "재무부 종가"),
              "etf": {"0085P0": (9380.0, "2026-11-26 15:19", "장중"), "0085N0": (9640.0, "2026-11-26 15:19", "장중")}}
        dec = {"status": "ok", "decision": "hike", "rate": 3.25, "prev": 3.0, "source": "한은 통화정책방향 보도자료",
               "dissent": "소수의견: 가나다 위원 3.00% 주장", "url": None}
        t, b, p = mpc_summary_msg(cfg, cfg["mpc_summary"]["date"], dec, ex, datetime(2026, 11, 26, 15, 20, tzinfo=KST))
        print(f"--- 예시 미리보기 (가짜 값, 실제 결과 아님) p{p}\n{t}\n{b}")
    return 0


def mpc_selftest(cfg: dict, now0: datetime, days: list, data) -> int:
    n = 0

    def ck(c, m=""):
        nonlocal n
        assert c, m
        n += 1
    from ntfy_util import route_topic
    T = lambda h, m=0, d=26: datetime(2026, 11, d, h, m, tzinfo=KST)  # noqa: E731
    mkt = {"fx": ("2026-11-26", 1328.4, "네이버 최신"), "y10": ("2026-11-25", 4.93, "재무부 종가"),
           "etf": {"0085P0": (9380.0, "2026-11-26 15:19", "장중"), "0085N0": (9640.0, "2026-11-26 15:19", "장중")}}
    mk = lambda now: mkt  # noqa: E731
    hike = lambda d: {"status": "ok", "decision": "hike", "rate": 3.25, "prev": 3.0, "source": "한은 통화정책방향 보도자료", "dissent": "만장일치"}  # noqa: E731
    hold = lambda d: {"status": "ok", "decision": "hold", "rate": 3.0, "prev": 3.0, "source": "한은 통화정책방향 보도자료", "dissent": "소수의견 확인 안 됨"}  # noqa: E731
    notyet = lambda d: {"status": "not_yet", "why": "아직 없음"}  # noqa: E731

    def boom(d):
        raise RuntimeError("net")
    sent, saved = [], []
    pub = lambda tp, t, b, p, tags, server, config_name=None: sent.append((route_topic(tp, t), t, b, p))  # noqa: E731
    sv = lambda s: saved.append(s)  # noqa: E731
    blank = load_state_blank()
    # 종료: 기준선 알림과 요약 모두 조용히
    ck(ended(datetime(2027, 2, 27, 9, 0, tzinfo=KST), cfg) and not ended(datetime(2027, 2, 26, 23, 0, tzinfo=KST), cfg), "ends")
    st, msgs = run("selftest", datetime(2027, 3, 2, 9, 0, tzinfo=KST), cfg,
                   {"fx": {}, "y10": {}, "fred": {}, "ecos": {}, "errors": ["x"]}, blank)
    ck(not msgs and st.get("ops_day") is None, "ended run must not ping ops")
    _, out, why = mpc_run("auto", datetime(2027, 3, 2, 15, 20, tzinfo=KST), cfg, blank, hike, mk, pub, sv)
    ck(not out and not sent and not saved and why == "ended", why)
    ck(cfg["ends_kst"] == "2027-02-26T23:59:00+09:00")
    # 끝 줄: 기존 기준선 메시지와 ops 에도
    for key in ("fx_low", "y10_high"):
        t, b, _ = build({"key": key, "date": days[-1], "value": 1.0, "src": "x"}, data([1280.0], [5.4]), cfg, now0)
        ck(b.endswith(END_LINE), key)
    ck(ops_msg(now0, "x")[1].endswith(END_LINE))
    # 다른 날, 10:30 전: 아무것도 안 함, state 안 씀
    _, out, why = mpc_run("auto", T(15, 20, 25), cfg, blank, hike, mk, pub, sv)
    ck(not out and not sent and not saved and why.startswith("not"), why)
    _, out, why = mpc_run("auto", T(9, 0), cfg, blank, hike, mk, pub, sv)
    ck(not out and not sent and not saved and why.startswith("before"), why)
    # 15:20 인상 확인: 1번, port, p3, 제목, 다시 돌려도 없음
    st, out, why = mpc_run("auto", T(15, 20), cfg, blank, hike, mk, pub, sv)
    ck(len(sent) == 1 and sent[0][1] == "[0085P0] 11/26 금통위 결과 요약 | 3.25% 인상", sent)
    ck(sent[0][0] == "cwally-port" and sent[0][3] == 3 and st["mpc"]["2026-11-26"]["done"] and len(saved) == 1)
    b = sent[0][2]
    ck(b.count("지금 할 일") == 1 and b.endswith(END_LINE + "\n" + DISCLAIMER), b)
    ck("1,285원 하회선보다 43.4원(3.4%) 위" in b and "5.30% 상회선까지 0.37%p" in b and "4.50% 안착선보다 0.43%p 위" in b, b)
    ck("0085P0(환노출) 9,380원" in b and "0085N0(환헤지) 9,640원" in b and "3.00% -> 3.25% 인상" in b and "만장일치" in b, b)
    ck("85.7%" in b and "예상대로" in b)
    ck(not any(c in sent[0][1] + b for c in "\u00b7\u318d\u2014") and "매수" not in sent[0][1] and "신호" not in sent[0][1])
    _, out, why = mpc_run("auto", T(19, 0), cfg, st, hike, mk, pub, sv)
    ck(not out and len(sent) == 1 and why.startswith("already"), why)
    # 동결 제목
    sent.clear()
    st, out, _ = mpc_run("auto", T(15, 20), cfg, blank, hold, mk, pub, sv)
    ck(sent[0][1] == "[0085P0] 11/26 금통위 결과 요약 | 3.00% 동결" and "예상과 다름" in sent[0][2], sent)
    # 15:20 미확인 -> 발송 없이 재시도 기록, 19:00 확인 -> 요약 1번
    sent.clear()
    st, out, why = mpc_run("auto", T(15, 20), cfg, blank, notyet, mk, pub, sv)
    ck(not sent and not st["mpc"]["2026-11-26"].get("done") and len(st["mpc"]["2026-11-26"]["tries"]) == 1, why)
    st, out, why = mpc_run("auto", T(19, 0), cfg, st, hike, mk, pub, sv)
    ck(len(sent) == 1 and why == "sent", why)
    # 15:20 과 19:00 모두 미확인(예외 포함) -> 19:00 에 확인 불가 1번(ops), 그 뒤 없음
    sent.clear()
    st, _, _ = mpc_run("auto", T(15, 20), cfg, blank, boom, mk, pub, sv)
    ck(not sent)
    st, out, why = mpc_run("auto", T(19, 0), cfg, st, notyet, mk, pub, sv)
    ck(len(sent) == 1 and sent[0][0] == "cwally-ops" and "확인 불가" in sent[0][1] and sent[0][3] == 3, sent)
    ck(st["mpc"]["2026-11-26"]["result"] == "unconfirmed" and sent[0][2].endswith(DISCLAIMER))
    _, out, why = mpc_run("auto", T(19, 30), cfg, st, hike, mk, pub, sv)
    ck(not out and len(sent) == 1, why)
    # dry: 발송 없음, state 안 씀
    sent.clear(), saved.clear()
    _, out, _ = mpc_run("dry", T(15, 20), cfg, blank, hike, mk, pub, sv)
    ck(len(out) == 1 and not sent and not saved)
    # 값 조회 실패여도 요약은 감(항목만 확인 불가)
    t, b, _ = mpc_summary_msg(cfg, "2026-11-26", hike("x"), {"fx": None, "y10": None, "etf": {}}, T(15, 20))
    ck("원/달러 확인 불가" in b and "미국 10년 확인 불가" in b and "0085P0(환노출) 확인 불가" in b)
    return n


def load_state_blank() -> dict:
    return {"fx": {"last_date": None, "armed": True},
            "y10": {"last_date": None, "high_armed": True, "settle_armed": True},
            "ops_day": None, "err_day": None, "updated_at": None}


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "auto"
    if mode == "selftest":
        return selftest()
    if mode == "mpc":
        return mpc_main(sys.argv[2:])
    if mode not in ("auto", "dry"):
        print(f"unknown mode {mode}")
        return 2
    cfg = load_cfg()
    now = datetime.now(KST)
    if ended(now, cfg):
        print(f"k0085p0 ended ({cfg['ends_kst']}): silent")
        return 0
    try:
        run(mode, now, cfg)
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        if mode == "auto":
            st = load_state()
            today = now.strftime("%Y-%m-%d")
            if st.get("err_day") != today:
                t = f"[0085P0] 스크립트 오류 확인 불가 | 판정 보류 {TICKERS}"
                publish(cfg["ntfy_ops_topic"], t, f"{now.strftime('%Y-%m-%d %H:%M KST')} | k0085p0_monitor.py 오류. 매매 신호 아님. Actions 로그 확인.\n{END_LINE}", 3, ["warning"], resolve_server(cfg.get("ntfy_server")))
                st["err_day"] = today
                save_state(st)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
