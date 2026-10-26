#!/usr/bin/env python3
"""487230 change-condition monitor. Silent unless a watched condition turns on or off."""
from __future__ import annotations

import json
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from kr_calendar import is_trading_day, session_day  # noqa: E402
from ntfy_util import publish, resolve_server, resolve_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
CFG_PATH = ROOT / "configs" / "a487230.json"
STATE_PATH = ROOT / "state" / "a487230.json"
UA = {"User-Agent": "cwally-alerts a487230 monitor"}


def load_json(path: Path, fallback: dict) -> dict:
    if not path.exists():
        return dict(fallback)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return dict(fallback)
    out = dict(fallback)
    out.update(data)
    return out


def get_json(url: str):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8", errors="ignore"))


def num(text):
    if text is None:
        return None
    raw = str(text).replace(",", "").replace("%", "").replace("+", "").strip()
    if not raw or raw in {"-", "N/A"}:
        return None
    mult = 1.0
    if raw.endswith("조"):
        mult, raw = 1e12, raw[:-1]
    elif raw.endswith("억"):
        mult, raw = 1e8, raw[:-1]
    elif raw.endswith("백만"):
        mult, raw = 1e6, raw[:-2]
    elif raw.endswith("만"):
        mult, raw = 1e4, raw[:-1]
    try:
        return float(raw) * mult
    except ValueError:
        return None


def info_map(rows):
    out = {}
    for row in rows or []:
        code = row.get("code")
        if code:
            out[code] = row.get("value")
    return out


def yahoo_last(symbol: str, template: str):
    data = get_json(template.format(symbol=symbol))
    result = (data.get("chart") or {}).get("result") or []
    if not result:
        return None, None
    meta = result[0].get("meta") or {}
    last = meta.get("regularMarketPrice")
    prev = meta.get("chartPreviousClose") or meta.get("previousClose")
    try:
        last_f = float(last)
    except (TypeError, ValueError):
        return None, None
    try:
        prev_f = float(prev)
        chg = (last_f / prev_f - 1.0) * 100.0
    except (TypeError, ValueError, ZeroDivisionError):
        chg = None
    return last_f, chg


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    dry = mode in {"dry", "test", "dry-run"}
    force = mode == "force"
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    state = load_json(STATE_PATH, {"updated_at": None, "last_title": None, "active": {}, "fx_base": None, "last_fail_date": None})
    now = datetime.now(KST)
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    day = session_day(now)
    if now.year != int(cfg.get("year_guard") or 2026) or day.isoformat() > cfg.get("valid_until", "2026-12-30"):
        print(f"outside window {day}; silent")
        return 0
    if not is_trading_day(day) and not force and not dry:
        print(f"{day} holiday; silent")
        return 0
    levels = cfg["levels"]
    price = rate = value = None
    qerr = None
    try:
        basic = get_json(cfg["sources"]["basic"])
        integ = get_json(cfg["sources"]["integration"])
        price = num(basic.get("closePrice"))
        rate = num(basic.get("fluctuationsRatio"))
        if basic.get("compareToPreviousPrice", {}).get("name") == "FALLING" and rate is not None:
            rate = -abs(rate)
        infos = info_map(integ.get("totalInfos") or [])
        value = num(infos.get("accumulatedTradingValue"))
    except Exception as exc:
        qerr = str(exc)
    print(f"price={price} rate={rate} value={value} qerr={qerr}")
    topic = resolve_topic("port", cfg.get("ntfy_topic"))
    server = resolve_server(cfg.get("ntfy_server"))
    if price is None:
        fail_day = day.isoformat()
        if state.get("last_fail_date") != fail_day or force:
            title = "[487230] 조회 실패 | 조건 판단 보류"
            msg = "\n".join([
                f"{stamp} | 487230 | KODEX 미국AI전력핵심인프라",
                "이게 뭐 알림: 변경 조건 점검이 시세를 못 읽었습니다. 매매 신호가 아닙니다.",
                f"지금 결과/상태: 가격 조회 실패. {qerr or '원인 미상'}",
                "지금 할 일: 다음 평일 15:20 재시도를 기다리세요. 급하면 네이버에서 487230만 확인하세요.",
                "나중에: 시세가 붙으면 2만원, 대금, 상위 종목, 환율만 다시 봅니다.",
                "익절/줄일 때: 이 실패 알림만으로 줄이지 마세요.",
                "하지 말 것: 조회 실패를 매수나 매도 신호로 읽기.",
            ])
            print(title)
            if not dry:
                publish(topic, title, msg, 3, ["warning"], server)
                state["last_fail_date"] = fail_day
                state["last_title"] = title
        state["updated_at"] = now.isoformat()
        if dry:
            print("dry: no state write")
        else:
            STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return 0
    flags = {}
    if price < float(levels["settle"]):
        flags["settle_break"] = f"가격 {price:,.0f}원, 2만원 안착 이탈"
    if rate is not None and rate <= float(levels["etf_drop_pct"]):
        flags["etf_drop"] = f"ETF 일간 {rate:+.2f}%"
    if value is not None and value < float(levels["value_weak_krw"]):
        flags["value_weak"] = f"15:20 대금 {value/1e8:.0f}억 원, 80억 미만"
    hold_bits = []
    for row in cfg["holdings"]:
        try:
            last, chg = yahoo_last(row["symbol"], cfg["sources"]["yahoo"])
        except Exception as exc:
            print(f"hold {row['symbol']} err {exc}")
            continue
        print(f"hold {row['symbol']} last={last} chg={chg}")
        if chg is not None and chg <= float(levels["holding_drop_pct"]):
            key = f"hold:{row['symbol']}"
            flags[key] = f"{row['name']} {chg:+.1f}% (비중 {row['weight']}%)"
            hold_bits.append(flags[key])
    fx_now = None
    fx_note = "환율 조회 없음"
    try:
        fx_now, _fx_chg = yahoo_last("KRW=X", cfg["sources"]["yahoo"])
    except Exception as exc:
        print(f"fx err {exc}")
    if fx_now and not state.get("fx_base"):
        state["fx_base"] = fx_now
        fx_note = f"원/달러 {fx_now:,.1f}원을 기준으로 저장. 오늘은 환율 알림 없음."
    elif fx_now and state.get("fx_base"):
        base = float(state["fx_base"])
        move = (fx_now / base - 1.0) * 100.0
        fx_note = f"원/달러 {fx_now:,.1f}원, 기준 {base:,.1f}원 대비 {move:+.2f}%"
        if move <= -float(levels["fx_move_pct"]):
            flags["fx_strong"] = f"원화 강세 {move:+.2f}%, 원화 수익에 불리"
        elif move >= float(levels["fx_move_pct"]):
            flags["fx_weak"] = f"원화 약세 {move:+.2f}%, 원화 수익에 우호"
    prev = state.get("active") or {}
    turned_on = [k for k in flags if k not in prev]
    turned_off = [k for k in prev if k not in flags]
    print(f"on={turned_on} off={turned_off} flags={list(flags)}")
    state["active"] = flags
    state["updated_at"] = now.isoformat()
    if not turned_on and not turned_off and not force:
        print("no condition change; silent")
        if dry:
            print("dry: no state write")
        else:
            STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return 0
    if "settle_break" in turned_on or "value_weak" in turned_on:
        title = "[487230] 2만원 안착 약화 | 반등 해석 보류"
    elif any(k.startswith("hold:") for k in turned_on) or "etf_drop" in turned_on:
        title = "[487230] 상위 종목 급락 | 낙폭 확대 주의"
    elif "fx_strong" in turned_on:
        title = "[487230] 원화 강세 | 원화 수익 불리"
    elif "fx_weak" in turned_on:
        title = "[487230] 원화 약세 | 분할 여지만 확인"
    elif turned_off and not turned_on:
        title = "[487230] 조건 해제 | 아직 매수 신호 아님"
    else:
        title = "[487230] 변경 조건 발생 | 추격 금지"
    ref = cfg["ref"]
    now_bits = [
        f"가격 {price:,.0f}원" + (f" ({rate:+.2f}%)" if rate is not None else ""),
        f"대금 {value/1e8:.0f}억 원" if value is not None else "대금 미확인",
        fx_note,
    ]
    if hold_bits:
        now_bits.append("급락 종목: " + ", ".join(hold_bits))
    action = "2만원 아래로 밀리거나 대금이 80억 미만이면 분할 검토를 멈추세요."
    if "settle_break" in flags or "value_weak" in flags:
        action = "안착이 깨졌습니다. 추가 매수를 보류하세요."
    elif any(k.startswith("hold:") for k in flags):
        action = "상위 보유 급락입니다. ETF 낙폭이 커질 수 있어 추격하지 마세요."
    elif "fx_weak" in flags and not any(k in flags for k in ("settle_break", "value_weak", "etf_drop")):
        action = "환율만 우호입니다. 가격 안착이 유지될 때만 기존 분할 계획을 유지하세요."
    msg = "\n".join([
        f"{stamp} | 487230 | {cfg['display']} 변경 조건",
        "이게 뭐 알림: 2026-10-07 분석의 변경 조건만 보는 알림입니다. 매수 지시가 아닙니다.",
        "지금 결과/상태: " + ". ".join(now_bits) + f". 기준가 {ref['price']:,}원({ref['price_time']}).",
        "지금 할 일: " + action,
        "나중에: 상위 4종목의 데이터센터 수주나 가이던스는 이 알림이 확인하지 않습니다. 공시로 따로 보세요. 유효기간은 2026-12-30까지입니다.",
        "익절/줄일 때: 2만원 이탈이 다음날도 이어지거나 블룸에너지, GE버노바, 버티브, 콴타가 동시에 급락하면 위성 비중을 줄이세요.",
        "하지 말 것: 조건 해제를 매수 신호로 읽기. 신용, 미수, 장마감 전 추격.",
    ])
    print(title)
    print(msg)
    print(f"topic={topic} send={not dry}")
    if not dry:
        publish(topic, title, msg, 4 if turned_on else 3, ["warning"], server)
        state["last_title"] = title
    if dry:
        print("dry: no state write")
    else:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
