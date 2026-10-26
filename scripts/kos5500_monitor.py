#!/usr/bin/env python3
"""Date-strict KOSPI scenario observations. Naver publication != KRX final.

afterclose: daily 15:40 slot; previous: next 09:00 slot; review: 2026-10-20 once.
dry and install never write state. Only install may publish an installation receipt.
"""
from __future__ import annotations

import json
import math
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import kr_calendar
from naver_api import get_json
from ntfy_util import resolve_server
from kospi_spool import publish  # [KOSPI] ladder spool (2026-10-26)

KST = timezone(timedelta(hours=9))
CFG_PATH = ROOT / "configs/kos5500.json"
STATE_PATH = ROOT / "state/kos5500.json"
STOCKS = {"005930": "삼성전자", "000660": "SK하이닉스"}


class DataUnavailable(ValueError):
    pass


def number(value) -> float:
    if isinstance(value, bool) or value is None or str(value).strip() in {"", "-"}:
        raise DataUnavailable("숫자 누락")
    try:
        result = float(str(value).replace(",", "").replace("+", ""))
    except (ValueError, TypeError) as exc:
        raise DataUnavailable("숫자 형식 변경") from exc
    if not math.isfinite(result):
        raise DataUnavailable("유효하지 않은 숫자")
    return result


def money(value) -> int:
    number(value)
    return int(Decimal(str(value).replace(",", "").replace("+", "")) * 100_000_000)


def price_rows(raw, days: list[str]) -> dict:
    if not isinstance(raw, list):
        raise DataUnavailable("코스피 일봉 형식 변경")
    out = {}
    for row in raw:
        if not isinstance(row, dict) or row.get("localTradedAt") not in days:
            continue
        day = row["localTradedAt"]
        if day in out:
            raise DataUnavailable("동일 날짜 일봉 중복")
        bar = {key: number(row.get(field)) for key, field in {
            "close": "closePrice", "low": "lowPrice", "high": "highPrice"
        }.items()}
        if not (0 < bar["low"] <= bar["close"] <= bar["high"]):
            raise DataUnavailable("일봉 가격 범위 불일치")
        out[day] = bar
    if any(day not in out for day in days[:2]):
        raise DataUnavailable("대상일 또는 전 거래일 종가 미게시. 이전 날짜 대체 금지")
    return out


def market_flow(raw, day: str) -> dict:
    if not isinstance(raw, dict) or str(raw.get("bizdate")) != day.replace("-", ""):
        raise DataUnavailable(f"{day} 코스피 수급 미게시 또는 날짜 불일치")
    return {"foreign": money(raw.get("foreignValue")),
            "institution": money(raw.get("institutionalValue")),
            "individual": money(raw.get("personalValue"))}


def stock_flows(raw, days: list[str], code: str) -> dict:
    if not isinstance(raw, list):
        raise DataUnavailable(f"{STOCKS[code]} 수급 형식 변경")
    out = {}
    for row in raw:
        if not isinstance(row, dict):
            continue
        raw_day = str(row.get("bizdate") or "")
        day = raw_day[:4] + "-" + raw_day[4:6] + "-" + raw_day[6:8]
        if day not in days:
            continue
        if str(row.get("itemCode", code)) != code or day in out:
            raise DataUnavailable("종목 또는 날짜 중복 불일치")
        out[day] = number(row.get("foreignerPureBuyQuant"))
    return out


def days_before(target: date, count: int = 4) -> list[str]:
    out = [target.isoformat()]
    for _ in range(count - 1):
        target = kr_calendar.prev_trading_day(target)
        out.append(target.isoformat())
    return out


def target_for(mode: str, now: datetime, cfg: dict) -> date:
    if mode == "review":
        return date.fromisoformat(cfg["review_day"])
    session = kr_calendar.session_day(now)
    if mode == "previous" or (mode in {"auto", "dry"} and "06:00" <= now.strftime("%H:%M") < "15:40"):
        return kr_calendar.prev_trading_day(session)
    return session


def collect(cfg: dict, target: date) -> dict:
    days = days_before(target)
    sources = cfg["sources"]
    bars = price_rows(get_json(sources["price"]), days)
    flows, stocks, gaps = {}, {}, []
    for day in days:
        try:
            flows[day] = market_flow(get_json(sources["flow"].format(day=day.replace("-", ""))), day)
        except Exception as exc:
            gaps.append(f"{day} 시장 수급 {type(exc).__name__}")
    for code in STOCKS:
        try:
            stocks[code] = stock_flows(get_json(sources["stock"].format(code=code)), days, code)
        except Exception as exc:
            stocks[code] = {}
            gaps.append(f"{STOCKS[code]} 수급 {type(exc).__name__}")
    return {"days": days, "bars": bars, "flows": flows, "stocks": stocks, "gaps": gaps,
            "source": "네이버 일별 게시값", "krx_final_confirmed": False}


def judge(data: dict, cfg: dict, offset: int = 0) -> dict:
    days = data["days"][offset:]
    day = days[0]
    bar = data["bars"][day]
    prev = data["bars"].get(days[1]) if len(days) > 1 else None
    support, recovery = cfg["support"], cfg["recovery"]
    above = bar["close"] >= support
    broken_twice = not above and prev is not None and prev["close"] < support
    two_above = bar["close"] > recovery and prev is not None and prev["close"] > recovery
    f3 = [data["flows"].get(d, {}).get("foreign") for d in days[:3]]
    cumulative = sum(f3) if len(f3) == 3 and all(v is not None for v in f3) else None
    semi = [data["stocks"].get(code, {}).get(day) for code in STOCKS]
    semi_buy = all(x > 0 for x in semi) if all(x is not None for x in semi) else None
    strengthened = bool(two_above and cumulative is not None and cumulative > 0 and semi_buy is True)
    if broken_twice:
        label = "5500 재이탈 지속"
    elif not above:
        label = "5500 이탈 첫날"
    elif strengthened:
        label = "회복 조건 충족(게시값 기준)"
    elif bar["close"] > recovery:
        label = "5800 회복 관찰"
    else:
        label = "5500 유지"
    return {"day": day, "label": label, "close": bar["close"], "low": bar["low"],
            "support": above, "below_twice": broken_twice, "above_twice": two_above,
            "low_rising": bar["low"] > prev["low"] if prev else None,
            "foreign_3d": cumulative, "semi_buy": semi_buy, "recovery_published": strengthened,
            "foreign": data["flows"].get(day, {}).get("foreign"),
            "institution": data["flows"].get(day, {}).get("institution"),
            "krx_final_confirmed": False}


def eok(value) -> str:
    return "미게시" if value is None else f"{value / 100_000_000:+,.0f}억원"


def status(value) -> str:
    return "미확인" if value is None else ("충족" if value else "미충족")


def make_report(data: dict, cfg: dict, now: datetime, review: bool) -> tuple[str, str, dict]:
    current, previous = judge(data, cfg), judge(data, cfg, 1)
    delta = current["close"] - previous["close"]
    rate = (current["close"] / previous["close"] - 1) * 100
    foreign_delta = (current["foreign"] - previous["foreign"]
                     if current["foreign"] is not None and previous["foreign"] is not None else None)
    action = "추가 매수 보류" if not current["support"] else "추격 매수 보류, 조건 관찰"
    if current["recovery_published"]:
        action = "분할 진입 조건 검토. KRX 최종 수급은 별도 확인"
    incomplete = current["foreign"] is None or current["institution"] is None
    prefix = "수급 미게시 | 가격만 관찰" if incomplete else current["label"]
    title = f"[KOS5500] {prefix}" + (" | 10/20 원샷" if review else "")
    lines = [
        "이게 뭐 알림: " + cfg["explain"],
        f"지금 상태: {current['day']} 종가 {current['close']:,.2f}, 전 거래일 대비 {delta:+,.2f}p ({rate:+.2f}%).",
        f"전일 대비 판정: {previous['day']} {previous['label']} → {current['day']} {current['label']}.",
        f"① 5500 유지: 종가 {status(current['support'])}, 저점 상승 {status(current['low_rising'])}.",
        f"② 재이탈: 당일 5500 미만 {status(not current['support'])}, 2거래일 연속 미회복 {status(current['below_twice'])}.",
        f"③ 회복: 5800 초과 종가 2일 {status(current['above_twice'])}, 외국인 3일 누적 {eok(current['foreign_3d'])}, 삼성/하이닉스 외국인 동반 매수 {status(current['semi_buy'])}.",
        f"시장 수급: 외국인 {eok(current['foreign'])}, 기관 {eok(current['institution'])}. 외국인 순매수액 전 거래일 대비 변화 {eok(foreign_delta)}.",
        f"데이터: 네이버 일별 게시값, KRX 최종 미확인. 조회 {now:%Y-%m-%d %H:%M KST}. 종목 수급은 주식 수 기준.",
        f"지금 할 일: {action}. 5668.05는 중간 관찰선입니다.",
        "나중에: 다음 거래일 09:00 수정분 점검, 15:40 당일 판정. 원샷 누락은 다음 아침 보충.",
        "익절/줄일 때: 5500 아래 종가가 다음 거래일에도 회복되지 않으면 기존 손실 한도를 재점검합니다. 자동 매도 지시는 아닙니다.",
        "하지 말 것: 게시값을 KRX 확정치로 단정하거나 하루 반등만으로 신용/레버리지 추격 매수.",
        "출처: " + cfg["sources"]["public_index"],
    ]
    if data["gaps"]:
        lines.append("미확인: " + "; ".join(data["gaps"]))
    return title, "\n".join(lines), current


def signature(report: dict) -> tuple:
    def sign(x):
        return None if x is None else (1 if x > 0 else -1 if x < 0 else 0)
    return (report["label"], report["support"], report["below_twice"], report["above_twice"],
            report["low_rising"], report["semi_buy"], sign(report["foreign_3d"]),
            sign(report["foreign"]), sign(report["institution"]))


def should_send(state: dict, report: dict, cfg: dict, mode: str, review: bool) -> bool:
    if review and not state.get("review_notified"):
        return True
    old = state.get("history", {}).get(report["day"])
    if old is None:
        return True
    if signature(old) != signature(report):
        return True
    a, b = old.get("foreign"), report["foreign"]
    return a is not None and b is not None and abs(a - b) >= cfg["correction_threshold_eok"] * 100_000_000


def save(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp = STATE_PATH.with_suffix(".tmp")
    temp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(STATE_PATH)


def ops(state: dict, cfg: dict, now: datetime, key: str, note: str, dry: bool) -> None:
    title = "[KOS5500] 조회 실패 | 판정 보류"
    body = "\n".join([
        "이게 뭐 알림: 코스피 시나리오 점검 자료가 준비되지 않았습니다.",
        "지금 상태: " + note,
        "지금 할 일: 네이버 일별 자료와 KRX 원문을 확인하세요. 조회 실패는 가격 이탈 신호가 아닙니다.",
        "나중에: 다음 기존 슬롯에서 재확인합니다. 10월 20일 원샷이 누락되면 다음 아침에 보충합니다.",
        "익절/줄일 때: 이 실패만으로 매매하지 않습니다.",
        "하지 말 것: 없는 수급을 0 또는 이전 날짜 값으로 대체.",
    ])
    print(title, body, sep="\n")
    if dry:
        return
    token = now.date().isoformat() + ":" + key
    if token in state.setdefault("errors", {}):
        return
    publish("cwally-port", title, body, 3, ["warning"], resolve_server(cfg["ntfy_server"]))
    state["errors"] = {k: v for k, v in state["errors"].items() if k[:10] >= (now.date() - timedelta(days=7)).isoformat()}
    state["errors"][token] = now.isoformat()
    save(state)


def main(argv=None, now=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    mode = argv[0].lower() if argv else "auto"
    if mode not in {"auto", "afterclose", "previous", "review", "dry", "install"}:
        raise ValueError("unsupported mode")
    dry = mode == "dry"
    now = (now or datetime.now(KST)).astimezone(KST)
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    if mode == "install":
        title = "[KOS5500] 설치 확인 | 매매 신호 아님"
        body = "\n".join([
            "이게 뭐 알림: 코스피 5500 시나리오 알림 설치 확인입니다.",
            "지금 상태: 설치 경로 확인용이며 10월 20일 시장 판정 결과가 아닙니다.",
            "지금 할 일: 이 알림의 수신 여부만 확인하세요.",
            "나중에: 10월 20일 20:40 원샷, 평일 15:40 판정과 다음 09:00 수정 점검 예정입니다.",
            "익절/줄일 때: 이 알림으로 매매하지 않습니다.",
            "하지 말 것: 설치 확인을 매수/매도 신호로 해석.",
        ])
        publish("cwally-port", title, body, 3, ["white_check_mark"], resolve_server(cfg["ntfy_server"]))
        return 0
    if dry:
        print("DRY RUN: no ntfy, no state writes")
    state = json.loads(STATE_PATH.read_text(encoding="utf-8")) if STATE_PATH.exists() else {"history": {}, "errors": {}, "review_notified": False}
    if mode == "review" and now.year != cfg["event_year"]:
        print("one-shot year guard; silent")
        return 0
    if mode == "review" and state.get("review_notified"):
        print("review already sent; silent")
        return 0
    if now.year != 2026:
        ops(state, cfg, now, "calendar", "2027년 이후 거래일 달력 미검증. 갱신 전 판정 보류", dry)
        return 0
    if not kr_calendar.is_trading_day(kr_calendar.session_day(now)):
        print("KRX holiday/weekend; silent")
        return 0
    if mode != "review" and not dry and now.date().isoformat() < cfg["start_day"]:
        print("before monitor start; silent")
        return 0
    target = target_for(mode, now, cfg)
    ready = datetime.combine(target, datetime.strptime("15:40", "%H:%M").time(), tzinfo=KST)
    if now < ready:
        print("target close not available yet; silent")
        return 0
    review_target = date.fromisoformat(cfg["review_day"])
    catch_up = mode == "previous" and not state.get("review_notified") and target >= review_target
    targets = [review_target, target] if catch_up and target != review_target else [target]
    for selected in targets:
        review = selected == review_target and not state.get("review_notified") and (mode == "review" or catch_up)
        try:
            data = collect(cfg, selected)
            title, body, report = make_report(data, cfg, now, review)
        except Exception as exc:
            ops(state, cfg, now, selected.isoformat(), f"{selected}: {type(exc).__name__}. 날짜가 맞는 종가를 확보하지 못했습니다.", dry)
            continue
        if report["foreign"] is None or report["institution"] is None:
            ops(state, cfg, now, selected.isoformat() + ":flow", body, dry)
            continue
        print(title, body, sep="\n")
        if dry:
            continue
        if not should_send(state, report, cfg, mode, review):
            print("unchanged/already reported; silent")
            continue
        # A send failure must propagate. Never mark a report sent before a successful publish.
        publish("cwally-port", title, body, 4 if not report["support"] else 3,
                ["chart_with_upwards_trend"], resolve_server(cfg["ntfy_server"]))
        state.setdefault("history", {})[report["day"]] = report
        keep = sorted(state["history"])[-30:]
        state["history"] = {d: state["history"][d] for d in keep}
        if not state.get("last_day") or report["day"] >= state["last_day"]:
            state["last_day"], state["last_report"] = report["day"], report
        if review:
            state["review_notified"] = True
        state["updated_at"] = now.isoformat()
        save(state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
