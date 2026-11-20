#!/usr/bin/env python3
"""[US10Y] [USDKRW] [KOSPI] 거시 4선 감시 (2026-11-20 승인 pt77u).

한 모니터, 두 기존 슬롯(새 cron 없음):
  morning 09:00 KST: 미 10년물만 (전 미국 영업일 재무부/FRED 확정치)
  0090b0 15:40 KST: 원달러와 코스피 확정 종가
선 (configs/kmacro4.json lines):
  y10_low  미 10년 종가 < 4.80% -> p3 (5.00% 이상 재무장)
  fx_low   원달러 종가 < 1,300원 -> p3 (1,320원 이상 재무장)
  fx_high  원달러 종가 > 1,380원 -> p3 (1,360원 이하 재무장)
  kospi_up 코스피 종가 >= 4,900 -> p3 (4,900 미만 재무장)
데이터: 10년=재무부 Daily Treasury Par Yield 10 Yr 우선, FRED DGS10 교차.
        원달러=네이버 FX_USDKRW 종가 우선, ECOS 731Y001 매매기준율은 키가 있을 때만 교차.
        코스피=Daum KOSPI 확정 종가, 외국인 순매매는 네이버 trend(억원) 있으면 본문에 넣고 없으면 생략.
첫 실행은 상태만 기록. ends_kst 2027-06-30 23:59 뒤 조용히 종료.
modes: auto | dry | selftest. 선택 슬롯 인자 morning|close|all (없으면 dry는 all, auto는 시각으로 추정).
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from kr_calendar import KST, is_trading_day, session_close_ready, session_day  # noqa: E402
from ntfy_util import publish, resolve_server, resolve_topic, route_topic  # noqa: E402
import usbond_monitor as ub  # noqa: E402

NAME = "kmacro4"
TAG_Y10 = "[US10Y]"
TAG_FX = "[USDKRW]"
TAG_KOS = "[KOSPI]"
CFG_PATH = ROOT / "configs" / f"{NAME}.json"
STATE_PATH = ROOT / "state" / f"{NAME}.json"
CLOSE_HM = (15, 40)
FORBIDDEN = re.compile("[\u00b7\u318d\u2014]")
DAUM_KOSPI = "https://finance.daum.net/api/market_index/days?page=1&perPage=10&market=KOSPI&pagination=true"
NAVER_FLOW = "https://m.stock.naver.com/api/index/KOSPI/trend?bizdate={day}"
UA = {"User-Agent": "Mozilla/5.0 (cwally-alerts; +mailto:sosmos3218@gmail.com)"}


def check_text(t: str, m: str) -> None:
    if FORBIDDEN.search(t + m):
        raise AssertionError(f"forbidden char in {t}")


def num(v) -> float | None:
    if v is None:
        return None
    s = str(v).replace(",", "").replace("+", "").replace("원", "").replace("%", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def http_json(url: str, ref: str, timeout: int = 25):
    req = urllib.request.Request(url, headers={**UA, "Referer": ref})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def load_cfg() -> dict:
    return json.loads(CFG_PATH.read_text(encoding="utf-8"))


def load_state() -> dict:
    blank = {
        "y10": {"last_date": None, "armed": True},
        "fx": {"last_date": None, "low_armed": True, "high_armed": True},
        "kospi": {"last_date": None, "armed": True},
        "ops_day": None,
        "updated_at": None,
    }
    if not STATE_PATH.exists():
        return blank
    try:
        raw = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return blank
    for k, v in raw.items():
        if isinstance(v, dict) and isinstance(blank.get(k), dict):
            blank[k].update(v)
        else:
            blank[k] = v
    return blank


def save_state(st: dict) -> None:
    STATE_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def ended(now: datetime, cfg: dict) -> bool:
    e = cfg.get("ends_kst")
    return bool(e) and now >= datetime.strptime(e, "%Y-%m-%d %H:%M").replace(tzinfo=KST)


def infer_slot(now: datetime, explicit: str | None) -> str:
    if explicit in {"morning", "close", "all"}:
        return explicit
    if (now.hour, now.minute) >= CLOSE_HM:
        return "all"  # after KR close, 10Y of previous US day is also known
    return "morning"


# ---------------- fetch ----------------
def fetch_y10(now: datetime) -> tuple[dict[str, tuple[float, str]], dict[str, float], list[str]]:
    today = now.date()
    start = today - timedelta(days=40)
    errors: list[str] = []
    tre, fred = {}, {}
    try:
        tre = ub.treasury_10y(start, today)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"재무부: {type(exc).__name__} {str(exc)[:80]}")
    try:
        fred = ub.fred_dgs10(start)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"FRED DGS10: {type(exc).__name__} {str(exc)[:80]}")
    iso = today.isoformat()
    out: dict[str, tuple[float, str]] = {}
    for d, v in sorted(tre.items()):
        if d < iso:
            out[d] = (float(v), "재무부")
    for d, v in sorted(fred.items()):
        if d < iso:
            if d not in out:
                out[d] = (float(v), "FRED")
    return dict(sorted(out.items())), {d: float(v) for d, v in fred.items() if d < iso}, errors


def fetch_fx(now: datetime, include_today: bool) -> tuple[dict[str, tuple[float, str]], dict[str, float], list[str]]:
    today = now.date()
    start = today - timedelta(days=40)
    errors: list[str] = []
    nav, ecos = {}, {}
    try:
        nav = ub.naver_fx()
    except Exception as exc:  # noqa: BLE001
        errors.append(f"네이버 FX: {type(exc).__name__} {str(exc)[:80]}")
    if os.environ.get("ECOS_API_KEY"):
        try:
            ecos = ub.ecos_fx(start, today)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"ECOS: {type(exc).__name__} {str(exc)[:80]}")
    cap = today.isoformat() if include_today else (today - timedelta(days=1)).isoformat()
    # include_today still requires close ready; caller decides include_today
    out: dict[str, tuple[float, str]] = {}
    for d, v in sorted(nav.items()):
        if d <= cap:
            out[d] = (float(v), "네이버 FX_USDKRW 종가")
    # ECOS is official 매매기준율: if present, it is the judgment value; Naver kept for gap
    ecos_used = {}
    for d, v in sorted(ecos.items()):
        if d <= cap:
            ecos_used[d] = float(v)
            if d in out:
                out[d] = (float(v), "ECOS 매매기준율")
            else:
                out[d] = (float(v), "ECOS 매매기준율")
    return dict(sorted(out.items())), ecos_used, errors


def fetch_kospi(now: datetime, include_today: bool) -> tuple[dict[str, float], dict[str, float], list[str]]:
    errors: list[str] = []
    closes: dict[str, float] = {}
    try:
        raw = http_json(DAUM_KOSPI, "https://finance.daum.net/global/market/KOSPI")
        for r in raw.get("data") or []:
            day = str(r.get("date") or "")[:10]
            px = num(r.get("tradePrice"))
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", day) and px:
                closes[day] = px
    except Exception as exc:  # noqa: BLE001
        errors.append(f"Daum KOSPI: {type(exc).__name__} {str(exc)[:80]}")
    cap = now.date().isoformat() if include_today else (now.date() - timedelta(days=1)).isoformat()
    closes = {d: v for d, v in sorted(closes.items()) if d <= cap}
    foreign: dict[str, float] = {}
    for day in list(closes)[-5:]:
        try:
            fl = http_json(NAVER_FLOW.format(day=day.replace("-", "")), "https://m.stock.naver.com/")
            fv = num(fl.get("foreignValue"))
            if fv is not None:
                foreign[day] = fv
        except Exception as exc:  # noqa: BLE001
            errors.append(f"외국인 수급 {day}: {type(exc).__name__}")
    return closes, foreign, errors


# ---------------- evaluate ----------------
def new_dates(ser: list[str], last: str | None, cap: int) -> list[str]:
    ds = [d for d in ser if last is None or d > last]
    return ds[-cap:]


def seed_or_eval_y10(st: dict, ser: dict[str, tuple[float, str]], cfg: dict, fred: dict[str, float]) -> list[dict]:
    ln = cfg["lines"]["y10_low"]
    s = st["y10"]
    if not ser:
        return []
    if not s.get("last_date"):
        d = list(ser)[-1]
        v, src = ser[d]
        s.update({"last_date": d, "last": v, "src": src, "armed": not (v < ln["level"]), "seeded": d})
        return []
    events = []
    for d in new_dates(list(ser), s["last_date"], int(cfg.get("catchup_dates", 10))):
        v, src = ser[d]
        if s.get("armed", True) and v < ln["level"]:
            events.append({"key": "y10_low", "date": d, "value": v, "src": src, "fred": fred.get(d)})
            s["armed"] = False
        elif (not s.get("armed", True)) and v >= ln["rearm"]:
            s["armed"] = True
        s["last_date"], s["last"], s["src"] = d, v, src
    return events


def seed_or_eval_fx(st: dict, ser: dict[str, tuple[float, str]], cfg: dict, nav: dict[str, float] | None = None) -> list[dict]:
    low, high = cfg["lines"]["fx_low"], cfg["lines"]["fx_high"]
    s = st["fx"]
    if not ser:
        return []
    if not s.get("last_date"):
        d = list(ser)[-1]
        v, src = ser[d]
        s.update({
            "last_date": d, "last": v, "src": src,
            "low_armed": not (v < low["level"]),
            "high_armed": not (v > high["level"]),
            "seeded": d,
        })
        return []
    events = []
    for d in new_dates(list(ser), s["last_date"], int(cfg.get("catchup_dates", 10))):
        v, src = ser[d]
        if s.get("low_armed", True) and v < low["level"]:
            events.append({"key": "fx_low", "date": d, "value": v, "src": src})
            s["low_armed"] = False
        elif (not s.get("low_armed", True)) and v >= low["rearm"]:
            s["low_armed"] = True
        if s.get("high_armed", True) and v > high["level"]:
            events.append({"key": "fx_high", "date": d, "value": v, "src": src})
            s["high_armed"] = False
        elif (not s.get("high_armed", True)) and v <= high["rearm"]:
            s["high_armed"] = True
        s["last_date"], s["last"], s["src"] = d, v, src
    return events


def seed_or_eval_kospi(st: dict, ser: dict[str, float], cfg: dict, foreign: dict[str, float]) -> list[dict]:
    ln = cfg["lines"]["kospi_up"]
    s = st["kospi"]
    if not ser:
        return []
    if not s.get("last_date"):
        d = list(ser)[-1]
        v = ser[d]
        s.update({
            "last_date": d, "last": v, "src": "Daum KOSPI",
            "armed": not (v >= ln["level"]),
            "foreign_eok": foreign.get(d),
            "seeded": d,
        })
        return []
    events = []
    for d in new_dates(list(ser), s["last_date"], int(cfg.get("catchup_dates", 10))):
        v = ser[d]
        if s.get("armed", True) and v >= ln["level"]:
            events.append({"key": "kospi_up", "date": d, "value": v, "src": "Daum KOSPI", "foreign_eok": foreign.get(d)})
            s["armed"] = False
        elif (not s.get("armed", True)) and v < ln["rearm"]:
            s["armed"] = True
        s["last_date"], s["last"], s["src"] = d, v, "Daum KOSPI"
        if d in foreign:
            s["foreign_eok"] = foreign[d]
    return events


# ---------------- messages ----------------
def body(stamp: str, what: str, status: str, action: str, later: str, trim: str, avoid: str) -> str:
    return "\n".join([
        stamp,
        f"이게 뭐 알림: {what}",
        f"지금 결과/상태: {status}",
        f"지금 할 일: {action}",
        f"나중에: {later}",
        f"익절/줄일 때: {trim}",
        f"하지 말 것: {avoid}",
    ])


def fmt_fx(v: float) -> str:
    return f"{v:,.1f}원"


def fmt_y(v: float) -> str:
    return f"{v:.2f}%"


def fmt_k(v: float) -> str:
    return f"{v:,.2f}"


def fmt_flow(v: float | None) -> str | None:
    if v is None:
        return None
    return f"외국인 유가증권 순매매 {v:+,.0f}억원"


def messages(cfg: dict, ev: dict, stamp: str) -> tuple[str, str, int]:
    key = ev["key"]
    pri = int(cfg["lines"][key].get("priority", 3))
    if key == "y10_low":
        title = f"{TAG_Y10} 미 10년물 4.8% 하회"
        st = (
            f"{ev['date']} 미 10년물 {fmt_y(ev['value'])} ({ev['src']}). "
            f"11/5 저점 4.84%."
        )
        if ev.get("fred") is not None and abs(ev["fred"] - ev["value"]) > 0.02:
            st += f" FRED DGS10은 같은 날 {fmt_y(ev['fred'])}."
        st += f" {cfg.get('context', '')}"
        msg = body(
            f"{stamp} | US10Y | 미 10년물",
            "미 10년물 확정 종가가 4.80% 아래로 내려왔다는 알림. 매매 지시 아님. 투자 참고용.",
            st,
            "주식 비중 소폭 확대 검토",
            f"같은 선은 종가 {fmt_y(cfg['lines'][key]['rearm'])} 이상 뒤에만 다시 옵니다. 종료 {cfg['ends_kst'][:10]}.",
            "이 알림만으로 한 번에 크게 늘리지 않기.",
            "자동 매수 신호로 보기. 투자 권유 아님.",
        )
    elif key == "fx_low":
        title = f"{TAG_FX} 원달러 1,300원 하회"
        st = f"{ev['date']} 원달러 종가 {fmt_fx(ev['value'])} ({ev['src']}). {cfg.get('context', '')}"
        msg = body(
            f"{stamp} | USDKRW | 원달러",
            "원달러 종가가 1,300원 아래로 내려왔다는 알림. 매매 지시 아님. 투자 참고용.",
            st,
            "환노출 달러 자산 분할 추가 매수 검토",
            f"같은 선은 종가 {fmt_fx(cfg['lines'][key]['rearm'])} 이상 뒤에만 다시 옵니다. 종료 {cfg['ends_kst'][:10]}.",
            "분할로만 검토. 한 번에 몰아서 환전하지 않기.",
            "자동 매수 신호로 보기. 투자 권유 아님.",
        )
    elif key == "fx_high":
        title = f"{TAG_FX} 원달러 1,380원 상회"
        st = (
            f"{ev['date']} 원달러 종가 {fmt_fx(ev['value'])} ({ev['src']}). "
            f"현재 미국 자산은 환노출로 운용 중. {cfg.get('context', '')}"
        )
        msg = body(
            f"{stamp} | USDKRW | 원달러",
            "원달러 종가가 1,380원을 넘어 환헤지 전환을 검토할 시점이라는 알림. 매매 지시 아님. 투자 참고용.",
            st,
            "신규 달러 매수 중단, 미국 자산 환헤지 전환 검토(현재 환노출 운용 중)",
            f"같은 선은 종가 {fmt_fx(cfg['lines'][key]['rearm'])} 이하로 내려간 뒤에만 다시 옵니다. 종료 {cfg['ends_kst'][:10]}.",
            "헤지 전환은 비용과 보유 기간을 보고 결정.",
            "자동 매도 또는 자동 환헤지 주문으로 보기. 투자 권유 아님.",
        )
    else:
        title = f"{TAG_KOS} 코스피 4,900 복귀"
        fl = fmt_flow(ev.get("foreign_eok"))
        st = f"{ev['date']} 코스피 종가 {fmt_k(ev['value'])} ({ev['src']})."
        if fl:
            st += f" {fl}."
        else:
            st += " 외국인 유가증권 순매매는 이번 조회에서 확인하지 못함."
        st += f" {cfg.get('context', '')}"
        msg = body(
            f"{stamp} | KOSPI | 코스피",
            "코스피 확정 종가가 4,900 이상으로 돌아왔다는 알림. 매매 지시 아님. 투자 참고용.",
            st,
            "한국 주식 비중 복원 검토, 외국인 현물과 선물 동반 매수 확인",
            f"같은 선은 종가 {fmt_k(cfg['lines'][key]['rearm'])} 미만으로 내려간 뒤에만 다시 옵니다. 종료 {cfg['ends_kst'][:10]}.",
            "복원은 분할로. 외국인 선물만 사고 현물이 팔리면 속도를 늦추기.",
            "자동 매수 신호로 보기. 투자 권유 아님.",
        )
    return title, msg, pri


def ops_msg(stamp: str, status: str) -> tuple[str, str]:
    return (
        f"{TAG_Y10} {TAG_FX} {TAG_KOS} 거시 4선 조회 실패 | 판정 건너뜀",
        body(
            stamp,
            "거시 4선 알림의 데이터 읽기 실패 안내. 하루 1번. 매매 신호 아님.",
            status,
            "미 10년물, 원달러, 코스피 종가를 증권 앱에서 직접 보세요.",
            "다음 슬롯에서 다시 읽고, 빠진 날짜는 이어서 판정합니다.",
            "이 안내로 비중을 바꾸지 않기.",
            "장중 값이나 확인되지 않은 출처로 대신 판정하기.",
        ),
    )


# ---------------- run ----------------
def run(mode: str, now: datetime, cfg: dict, slot: str = "all", pub=publish,
        y10_fn=None, fx_fn=None, kospi_fn=None) -> int:
    dry = mode == "dry"
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    topic, server = resolve_topic("port", cfg.get("ntfy_topic")), resolve_server(cfg.get("ntfy_server"))
    print(f"{stamp} {NAME} mode={mode} slot={slot}")
    if ended(now, cfg):
        print(f"{NAME} ended; silent")
        return 0

    sday = session_day(now)
    today = sday
    include_today = session_close_ready(today.isoformat(), today.isoformat(), now, CLOSE_HM)
    want_y10 = slot in {"morning", "all"}
    want_kr = slot in {"close", "all"}
    if want_kr and slot == "close" and not is_trading_day(today) and mode != "dry":
        print(f"{today.isoformat()} KRX 휴장: 원달러/코스피 판정 생략")
        want_kr = False

    st = load_state()
    out: list[tuple[str, str, int]] = []
    errors: list[str] = []
    seeded = []

    if want_y10:
        try:
            ser, fred, err = (y10_fn or (lambda: fetch_y10(now)))()
            errors.extend(err)
            if ser:
                last_d, (last_v, last_src) = list(ser.items())[-1]
                print(f"y10 latest {last_d} {fmt_y(last_v)} ({last_src}) armed_hint={not (last_v < cfg['lines']['y10_low']['level'])}")
                before = st["y10"].get("last_date")
                ev = seed_or_eval_y10(st, ser, cfg, fred)
                if not before:
                    seeded.append(f"y10 {st['y10']}")
                for e in ev:
                    out.append(messages(cfg, e, stamp))
            elif not err:
                errors.append("미 10년물 확정치 없음")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"미 10년물 조회 실패: {str(exc)[:160]}")

    if want_kr:
        try:
            fx, ecos, err = (fx_fn or (lambda: fetch_fx(now, include_today)))()
            errors.extend(err)
            if fx:
                d, (v, src) = list(fx.items())[-1]
                print(f"fx latest {d} {fmt_fx(v)} ({src}) low_armed_hint={not (v < cfg['lines']['fx_low']['level'])} high_armed_hint={not (v > cfg['lines']['fx_high']['level'])}")
                before = st["fx"].get("last_date")
                ev = seed_or_eval_fx(st, fx, cfg)
                if not before:
                    seeded.append(f"fx {st['fx']}")
                for e in ev:
                    out.append(messages(cfg, e, stamp))
            elif not err:
                errors.append("원달러 종가 없음")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"원달러 조회 실패: {str(exc)[:160]}")
        try:
            closes, foreign, err = (kospi_fn or (lambda: fetch_kospi(now, include_today)))()
            errors.extend(err)
            if closes:
                d, v = list(closes.items())[-1]
                print(f"kospi latest {d} {fmt_k(v)} (Daum) foreign={foreign.get(d)} armed_hint={not (v >= cfg['lines']['kospi_up']['level'])}")
                before = st["kospi"].get("last_date")
                ev = seed_or_eval_kospi(st, closes, cfg, foreign)
                if not before:
                    seeded.append(f"kospi {st['kospi']}")
                for e in ev:
                    out.append(messages(cfg, e, stamp))
            elif not err:
                errors.append("코스피 종가 없음")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"코스피 조회 실패: {str(exc)[:160]}")

    if seeded:
        print("first run: state only, no alert:")
        for s in seeded:
            print(" ", s)

    ops = None
    if errors:
        print("ERR:", "; ".join(errors))
        # only ops if a needed series fully failed (no data) or a hard error
        hard = any("실패" in e or "없음" in e for e in errors)
        if hard and st.get("ops_day") != today.isoformat():
            ops = ops_msg(stamp, "; ".join(errors) + ". state 의 날짜는 그대로.")
            st["ops_day"] = today.isoformat()

    for t, m, pri in out:
        check_text(t, m)
        print(f"TITLE: {t}\nPRIORITY: {pri} TOPIC: {route_topic(topic, t)}\n{m}\n")
    if ops:
        check_text(ops[0], ops[1])
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
    cfg = load_cfg()
    fails = 0

    def check(label, ok):
        nonlocal fails
        fails += not ok
        print(("ok  " if ok else "FAIL"), label)

    def blank():
        return {"y10": {"last_date": None, "armed": True},
                "fx": {"last_date": None, "low_armed": True, "high_armed": True},
                "kospi": {"last_date": None, "armed": True}}

    # y10: 4.80 does not fire, 4.79 does, 4.99 no rearm, 5.00 rearms, fire again
    st = blank()
    ser = {"2026-11-05": (4.84, "재무부")}
    ev = seed_or_eval_y10(st, ser, cfg, {})
    check("y10 seed 4.84 armed", ev == [] and st["y10"]["armed"] is True and st["y10"]["last_date"] == "2026-11-05")
    walk = {
        "2026-11-06": (4.80, "재무부"),
        "2026-11-09": (4.79, "재무부"),
        "2026-11-10": (4.90, "재무부"),
        "2026-11-12": (4.99, "재무부"),
        "2026-11-13": (5.00, "재무부"),
        "2026-11-16": (4.70, "재무부"),
    }
    ev = seed_or_eval_y10(st, {**ser, **walk}, cfg, {"2026-11-09": 4.79})
    got = [(e["date"], e["value"]) for e in ev]
    check(f"y10 hysteresis {got}", got == [("2026-11-09", 4.79), ("2026-11-16", 4.70)])

    # seed already below 4.80 starts disarmed, no event
    st2 = blank()
    ev2 = seed_or_eval_y10(st2, {"2026-11-19": (4.70, "재무부")}, cfg, {})
    check("y10 seed inside disarmed", ev2 == [] and st2["y10"]["armed"] is False)

    # fx low <1300, rearm >=1320; high >1380, rearm <=1360; 1380 does not fire
    st = blank()
    ev = seed_or_eval_fx(st, {"2026-11-02": (1335.7, "네이버 FX_USDKRW 종가")}, cfg)
    check("fx seed mid both armed", ev == [] and st["fx"]["low_armed"] and st["fx"]["high_armed"])
    walk = {
        "2026-11-03": (1300.0, "n"),
        "2026-11-04": (1299.9, "n"),
        "2026-11-05": (1319.9, "n"),
        "2026-11-06": (1320.0, "n"),
        "2026-11-09": (1380.0, "n"),
        "2026-11-10": (1380.1, "n"),
        "2026-11-11": (1360.1, "n"),
        "2026-11-12": (1360.0, "n"),
        "2026-11-13": (1290.0, "n"),
    }
    ev = seed_or_eval_fx(st, {"2026-11-02": (1335.7, "n"), **walk}, cfg)
    keys = [(e["key"], e["date"], e["value"]) for e in ev]
    check(f"fx hysteresis {keys}", keys == [
        ("fx_low", "2026-11-04", 1299.9),
        ("fx_high", "2026-11-10", 1380.1),
        ("fx_low", "2026-11-13", 1290.0),
    ])

    # kospi >=4900 fire, <4900 rearm
    st = blank()
    ev = seed_or_eval_kospi(st, {"2026-11-20": 4869.96}, cfg, {"2026-11-20": -11266})
    check("kospi seed below armed", ev == [] and st["kospi"]["armed"] is True)
    ev = seed_or_eval_kospi(st, {"2026-11-20": 4869.96, "2026-11-23": 4900.00, "2026-11-24": 4900.00,
                                "2026-11-25": 4899.99, "2026-11-26": 4901.00}, cfg,
                            {"2026-11-23": 1200, "2026-11-26": -50})
    got = [(e["date"], e["value"], e.get("foreign_eok")) for e in ev]
    check(f"kospi hysteresis {got}", got == [("2026-11-23", 4900.00, 1200), ("2026-11-26", 4901.00, -50)])

    # same date skipped
    ev = seed_or_eval_kospi(st, {"2026-11-26": 5000.00}, cfg, {})
    check("kospi same day skipped", ev == [])

    # messages and hygiene
    evy = {"key": "y10_low", "date": "2026-11-09", "value": 4.79, "src": "재무부", "fred": 4.79}
    t, m, p = messages(cfg, evy, "2026-11-10 09:00 KST")
    check("y10 title", t == "[US10Y] 미 10년물 4.8% 하회")
    check("y10 ticker and 11/5", "US10Y" in t and "4.79%" in m and "11/5 저점 4.84%" in m)
    check("y10 action", "주식 비중 소폭 확대 검토" in m)
    check("y10 p3", p == 3)
    check_text(t, m)

    evf = {"key": "fx_low", "date": "2026-11-04", "value": 1299.9, "src": "네이버 FX_USDKRW 종가"}
    t, m, p = messages(cfg, evf, "T")
    check("fx low title", t == "[USDKRW] 원달러 1,300원 하회")
    check("fx low action", "환노출 달러 자산 분할 추가 매수 검토" in m)
    check_text(t, m)

    evh = {"key": "fx_high", "date": "2026-11-10", "value": 1380.1, "src": "네이버 FX_USDKRW 종가"}
    t, m, p = messages(cfg, evh, "T")
    check("fx high title", t == "[USDKRW] 원달러 1,380원 상회")
    check("fx high action", "신규 달러 매수 중단, 미국 자산 환헤지 전환 검토(현재 환노출 운용 중)" in m)
    check_text(t, m)

    evk = {"key": "kospi_up", "date": "2026-11-23", "value": 4900.00, "src": "Daum KOSPI", "foreign_eok": 1200}
    t, m, p = messages(cfg, evk, "T")
    check("kospi title", t == "[KOSPI] 코스피 4,900 복귀")
    check("kospi flow in body", "외국인 유가증권 순매매 +1,200억원" in m)
    check("kospi action", "한국 주식 비중 복원 검토, 외국인 현물과 선물 동반 매수 확인" in m)
    check_text(t, m)

    evk2 = {"key": "kospi_up", "date": "2026-11-23", "value": 4900.00, "src": "Daum KOSPI", "foreign_eok": None}
    t, m, p = messages(cfg, evk2, "T")
    check("kospi omits flow when missing", "확인하지 못함" in m)
    check_text(t, m)

    ot, om = ops_msg("T", "x")
    check("ops has three tickers", "US10Y" in ot and "USDKRW" in ot and "KOSPI" in ot)
    check_text(ot, om)

    # close-ready / holiday
    fri_am = datetime(2026, 11, 20, 10, 0, tzinfo=KST)
    fri_pm = datetime(2026, 11, 20, 15, 40, tzinfo=KST)
    check("before close not ready", not session_close_ready("2026-11-20", "2026-11-20", fri_am, CLOSE_HM))
    check("after close ready", session_close_ready("2026-11-20", "2026-11-20", fri_pm, CLOSE_HM))
    check("Fri holiday last session Thu", not is_trading_day(date(2026, 5, 1)) and is_trading_day(date(2026, 4, 30)))

    # dry first-run via run(), no publish
    published = []

    def fake_pub(*a, **k):
        published.append((a, k))

    y10 = lambda: ({"2026-11-19": (5.04, "재무부")}, {"2026-11-18": 5.07}, [])  # noqa: E731
    fx = lambda: ({"2026-11-20": (1335.7, "네이버 FX_USDKRW 종가")}, {}, [])  # noqa: E731
    kos = lambda: ({"2026-11-20": 4869.96}, {"2026-11-20": -11266.0}, [])  # noqa: E731
    rc = run("dry", fri_pm, cfg, "all", pub=fake_pub, y10_fn=y10, fx_fn=fx, kospi_fn=kos)
    check("dry returns 0", rc == 0)
    check("dry publishes nothing", published == [])

    # config hygiene
    raw = CFG_PATH.read_text(encoding="utf-8")
    if STATE_PATH.exists():
        raw += STATE_PATH.read_text(encoding="utf-8")
    check("config+state no forbidden", not FORBIDDEN.search(raw))
    check("ends 2027-06-30", str(cfg.get("ends_kst", "")).startswith("2027-06-30"))
    check("y10 level 4.80", float(cfg["lines"]["y10_low"]["level"]) == 4.80)
    check("fx levels", cfg["lines"]["fx_low"]["level"] == 1300 and cfg["lines"]["fx_high"]["level"] == 1380)
    check("kospi 4900", cfg["lines"]["kospi_up"]["level"] == 4900)

    print("selftest", "PASS" if not fails else f"FAIL {fails}", f"fails={fails}")
    return 1 if fails else 0


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    slot = (sys.argv[2] if len(sys.argv) > 2 else "").lower() or None
    cfg = load_cfg()
    if mode == "selftest":
        return selftest()
    if mode not in {"auto", "dry"}:
        print(__doc__)
        return 2
    now = datetime.now(KST)
    use = infer_slot(now, slot)
    return run(mode, now, cfg, use)


if __name__ == "__main__":
    raise SystemExit(main())
