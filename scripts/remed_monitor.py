#!/usr/bin/env python3
"""REMED 리메드(302550) 신저가 이후 진입 감시. 보유 없음, 진입 여부 판단용.

KRX 장 마감 뒤(15:45 KST) 당일 완료 종가로 구간을 판정하고, 구간이 바뀔 때만 cwally-port로 알린다.
  구간 0: 종가 < 기준일(2026-10-15) 저가          -> 하락 지속, 진입 보류
  구간 1: 기준일 저가 <= 종가 < 2,270원(10/14 저점) -> 저점권, 관망
  구간 2: 2,270원 <= 종가 (2,600원 미만이거나 거래량 부족) -> 구 저점 회복, 실적 전 소액만
  구간 3: 종가 >= 2,600원 이고 거래량 >= 직전 20거래일 평균 -> 추세 회복
아래로 내려가는 판정은 경계선 -1%(recover_buffer_pct)에서 한다(하루 흔들림 방지).
3분기 실적: 네이버 분기 실적 칸(q3_gate.period_key)이 컨센서스(isConsensus=Y)에서 실제치로 바뀌면 영업이익을
  9억 이상 유지 / 5~8억 둔화 / 5억 미만 악화로 한 번 판정해 알린다(정수 억 단위라 반올림 한계 있음).
공시: CB/정정/유상증자/실적 등 주요 공시가 새로 올라오면 알린다(공매도 과열, 투자주의 등 시장조치는 제외).
첫 실행은 기준일 종가로 구간을 정하고 공시 목록을 조용히 기록한다.
modes: auto(기본) | dry(발송 없음, state 안 씀) | install(설치 확인 1회, state 안 씀)
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
import naver_api  # noqa: E402
import kr_calendar  # noqa: E402

KST = timezone(timedelta(hours=9))
NAME = "remed"
CFG_PATH = ROOT / "configs" / f"{NAME}.json"
STATE_PATH = ROOT / "state" / f"{NAME}.json"
CHART = "https://fchart.stock.naver.com/sise.nhn?symbol={code}&timeframe=day&count=40&requestType=0"
CLOSE_READY = "15:40"  # KRX 정규장 15:30 마감 + 종가 반영 여유


def load_state() -> dict:
    base = {"updated_at": None, "zone": None, "last_day": None, "last_close": None,
            "seen_notices": [], "fetch_fail_date": None}
    if STATE_PATH.exists():
        try:
            base.update(json.loads(STATE_PATH.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass
    return base


def daily_bars(code: str) -> list[dict]:
    req = urllib.request.Request(CHART.format(code=code), headers=naver_api.UA)
    with urllib.request.urlopen(req, timeout=20) as resp:
        text = resp.read().decode("euc-kr", errors="ignore")
    out = []
    for m in re.finditer(r'data="(\d{8})\|(\d+)\|(\d+)\|(\d+)\|(\d+)\|(\d+)"', text):
        d, o, h, lo, c, v = m.groups()
        out.append({"day": d, "open": float(o), "high": float(h), "low": float(lo), "close": float(c), "volume": float(v)})
    out.sort(key=lambda r: r["day"])
    return out


def raw_zone(close: float, vol_ok: bool, low_line: float, reclaim: float, trend: float) -> int:
    if close < low_line:
        return 0
    if close < reclaim:
        return 1
    if close >= trend and vol_ok:
        return 3
    return 2


def next_zone(prev: int, close: float, vol_ok: bool, lines: dict, buf: float) -> int:
    raw = raw_zone(close, vol_ok, lines["low"], lines["reclaim"], lines["trend"])
    if raw >= prev:
        return raw
    bound = {3: lines["trend"], 2: lines["reclaim"], 1: lines["low"]}
    z = prev
    while z > raw and close < bound[z] * (1 - buf / 100):
        z -= 1
    return z


def body(stamp: str, status: str, action: str, later: str, trim: str, avoid: str) -> str:
    return "\n".join([
        f"{stamp} | REMED | 리메드(302550) 진입 감시",
        "이게 뭐 알림: 10/14 52주 신저가와 100억 CB 공시 뒤 리메드를 새로 살지 보는 감시. 종가 구간이 바뀌거나 주요 공시가 나올 때만 옵니다. 보유 없음 기준.",
        f"지금 결과/상태: {status}",
        f"지금 할 일: {action}",
        f"나중에: {later}",
        f"익절/줄일 때: {trim}",
        f"하지 말 것: {avoid}",
    ])


LATER = "11월 중순 3분기 실적에서 분기 영업이익 9억 원 수준(2분기)이 유지되는지 보세요. CB 338만 주(11.18%)는 2027-10-27부터 전환 가능."
TRIM = "아직 보유 없음. 들어갔다면 2,270원 아래 다시 마감 시 줄이고, 2,800~2,954원(CB 전환가대)에서는 일부 익절을 검토."
AVOID = "하루 급등 추격, 공시 직후 한 번에 다 사기, 신용/미수, 테마 기사만 보고 매수."

ZONE_TXT = {0: "기준일 저가 아래", 1: "저점권(기준일 저가~2,270원)", 2: "2,270원 위", 3: "2,600원 위(거래량 동반)"}


def zone_alert(prev: int, z: int, lines: dict) -> tuple[str, str, int]:
    low = f"{lines['low']:,.0f}원"
    if z == 0:
        return (f"[REMED] 302550 {low} 아래 마감 | 하락 지속, 진입 보류",
                f"신규 진입 보류. 바닥 확인 전이라 사지 마세요. 다음 기준은 {low} 위 회복입니다.", 4)
    if z == 1 and prev == 0:
        return (f"[REMED] 302550 {low} 위 회복 | 아직 관망",
                "회복만으로 사지 마세요. 2,270원 위 마감과 거래량 감소를 먼저 기다리세요.", 3)
    if z == 1:
        return ("[REMED] 302550 2,270원 아래 다시 마감 | 진입 보류",
                "구 저점 회복이 실패했습니다. 진입했다면 비중을 줄이고, 아니라면 계속 관망하세요.", 4)
    if z == 2 and prev < 2:
        return ("[REMED] 302550 2,270원 위 마감 | 실적 전 소액 분할만 검토",
                "들어간다면 목표 비중의 1/3 이하로만. 나머지는 3분기 실적 확인 뒤에 판단하세요.", 4)
    if z == 2:
        return ("[REMED] 302550 2,600원 아래 다시 마감 | 추가 진입 보류",
                "추세 회복이 꺾였습니다. 새로 더 사지 말고 2,270원 지지 여부를 보세요.", 3)
    return ("[REMED] 302550 2,600원 회복(거래량 동반) | 추세 전환, 추격 금지",
            "추세가 돌아선 신호입니다. 다음 날 바로 추격하지 말고 눌림(2,600원 부근)과 실적을 보고 판단하세요.", 4)


QUARTER = "https://m.stock.naver.com/api/stock/{code}/finance/quarter"


def quarter_op(code: str, key: str) -> tuple[float | None, str]:
    """(영업이익 억원, 상태). 상태: actual | pending | missing."""
    raw = naver_api.get_json(QUARTER.format(code=code))
    fin = (raw or {}).get("financeInfo") or {}
    col = next((t for t in fin.get("trTitleList") or [] if t.get("key") == key), None)
    if col is None:
        return None, "missing"
    if str(col.get("isConsensus") or "").upper() == "Y":
        return None, "pending"
    row = next((r for r in fin.get("rowList") or [] if r.get("title") == "영업이익"), None)
    val = naver_api.num(((row or {}).get("columns") or {}).get(key, {}).get("value")) if row else None
    return (val, "actual") if val is not None else (None, "pending")


def q3_grade(op: float, gate: dict) -> tuple[str, str, int]:
    keep, slow = float(gate.get("maintain", 9)), float(gate.get("slow_min", 5))
    if op >= keep:
        return ("유지", "실적 조건은 통과. 진입은 종가 구간(2,270원 위 마감)과 함께 볼 때만 목표 비중 1/3씩 분할.", 4)
    if op >= slow:
        return ("둔화", "2분기 9억보다 둔화. 진입을 서두르지 말고 4분기 흐름이나 2,600원 회복(거래량 동반)을 기다리세요.", 3)
    return ("악화", "실적 개선 근거가 약해졌습니다. 신규 진입 보류, 기준선을 다시 잡습니다.", 4)


def notice_alerts(cfg: dict, items: list[dict], seen: set[str]) -> list[dict]:
    inc = cfg.get("notice_keywords") or []
    exc = cfg.get("notice_exclude") or []
    out = []
    for it in items:
        if it["id"] in seen:
            continue
        t = it["title"]
        if any(x in t for x in exc):
            continue
        if any(k in t for k in inc):
            out.append(it)
    return out


def split_backlog(found: list[dict], now: datetime, max_age_days: int) -> tuple[list[dict], list[dict]]:
    """새 공시를 (최근, 오래된 것)으로 나눈다. 날짜를 못 읽으면 최근으로 본다."""
    cutoff = (now - timedelta(days=max_age_days)).strftime("%Y-%m-%d")
    fresh, stale = [], []
    for it in found:
        day = str(it.get("datetime") or "")[:10]
        (stale if re.fullmatch(r"\d{4}-\d{2}-\d{2}", day) and day < cutoff else fresh).append(it)
    return fresh, stale


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    today = now.strftime("%Y%m%d")
    topic = resolve_topic("port", cfg.get("ntfy_topic"))
    server = resolve_server(cfg.get("ntfy_server"))
    code = cfg["code"]
    reclaim = float(cfg["levels"]["reclaim"])
    trend = float(cfg["levels"]["trend"])
    buf = float(cfg.get("recover_buffer_pct") or 1)
    vol_mult = float(cfg.get("trend_volume_mult") or 1)
    base_day = str(cfg["baseline_date"]).replace("-", "")

    try:
        bars = daily_bars(code)
        err = None if bars else "차트 비어 있음"
    except Exception as exc:  # noqa: BLE001
        bars, err = [], str(exc)
    base = next((b for b in bars if b["day"] == base_day), None)
    if base is not None:
        low_line = float(cfg["levels"].get("low") or base["low"])
        low_line = min(low_line, reclaim * 0.97)
    else:
        low_line = float(cfg["levels"].get("low") or 0) or None
    lines = {"low": low_line, "reclaim": reclaim, "trend": trend}

    if mode == "install":
        if base is not None:
            bz = raw_zone(base["close"], False, low_line, reclaim, trend)
            st = (f"기준일 {base_day} 종가 {base['close']:,.0f}원, 저가 {base['low']:,.0f}원, 거래량 {base['volume']:,.0f}주. "
                  f"기준선: 하락선 {low_line:,.0f}원(기준일 저가), 회복선 2,270원, 추세선 2,600원(거래량 20일 평균 이상). "
                  f"현재 구간 {ZONE_TXT[bz]}. 평일 15:45 KST 종가로 판정.")
        else:
            st = f"기준일 {base_day} 종가 조회 실패({err}). 첫 정규 실행에서 다시 읽습니다."
        publish(topic, "[REMED] 302550 설치 확인 | 매매 신호 아님",
                body(stamp, st, "설치 확인만 하세요. 이 알림으로 사거나 팔지 마세요.",
                     "구간이 바뀌거나 CB 정정, 유상증자, 실적 같은 주요 공시가 나올 때만 옵니다. " + LATER,
                     TRIM, "설치 확인을 매수 신호로 읽기."),
                3, ["information_source"], server)
        print("install confirmation sent")
        return 0

    if now.year != int(cfg.get("year_guard") or 2026):
        print("year guard; silent")
        return 0
    end = cfg.get("ends_kst")
    if end and now > datetime.strptime(end, "%Y-%m-%d %H:%M").replace(tzinfo=KST) + timedelta(minutes=59):
        print(f"ended at {end}; silent")
        return 0
    if not kr_calendar.is_trading_day(now.date()):
        print("KRX holiday; silent")
        return 0
    if now.strftime("%H:%M") < CLOSE_READY:
        print("before close; silent")
        return 0

    dry = mode == "dry"
    state = load_state()
    alerts: list[tuple[str, str, int]] = []
    bar = bars[-1] if bars else None

    if bar is None or bar["day"] != today or low_line is None:
        why = err or ("당일 봉 없음" if low_line is not None else "기준일 저가 없음")
        if state.get("fetch_fail_date") != today:
            alerts.append(("[REMED] 302550 종가 조회 실패 | 조건 미확인",
                           body(stamp, f"당일 종가 조회 실패: {why}. 구간 판정 안 함.", "증권 앱에서 리메드 종가를 직접 보세요.",
                                "다음 거래일 15:45에 다시 봅니다.", "이번 알림으로 사거나 팔지 마세요.", "조회 실패를 신호로 읽기."), 3))
            state["fetch_fail_date"] = today
    else:
        prior = [b for b in bars if b["day"] < today][-20:]
        avg20 = sum(b["volume"] for b in prior) / len(prior) if prior else 0
        vol_ok = bool(avg20) and bar["volume"] >= avg20 * vol_mult
        close = bar["close"]
        if state.get("zone") is None:
            seed_close = base["close"] if base is not None else close
            state["zone"] = raw_zone(seed_close, False, low_line, reclaim, trend)
            print(f"seed zone {state['zone']} from close {seed_close}")
        prev = int(state["zone"])
        z = next_zone(prev, close, vol_ok, lines, buf)
        print(f"day={bar['day']} close={close} vol={bar['volume']} avg20={avg20:.0f} zone {prev}->{z}")
        if z != prev:
            title, action, pri = zone_alert(prev, z, lines)
            status = (f"{bar['day'][4:6]}/{bar['day'][6:]} 종가 {close:,.0f}원, 거래량 {bar['volume']:,.0f}주(20일 평균 {avg20:,.0f}주). "
                      f"구간 {ZONE_TXT[prev]} -> {ZONE_TXT[z]}. 하락선 {low_line:,.0f}원 / 회복선 2,270원 / 추세선 2,600원.")
            alerts.append((title, body(stamp, status, action, LATER, TRIM, AVOID), pri))
            state["zone"] = z
        state["last_day"], state["last_close"] = bar["day"], close

    try:
        items = naver_api.notices(code, 15)
    except Exception as exc:  # noqa: BLE001
        items = None
        print(f"notice fetch failed: {exc}")
    if items is not None:
        seen = set(state.get("seen_notices") or [])
        if not seen:
            print(f"seed {len(items)} notices silently")
        else:
            fresh, stale = split_backlog(notice_alerts(cfg, items, seen), now,
                                         int(cfg.get("notice_max_age_days") or 3))
            if stale:
                # 쉬는 기간(10/19~11/10 등)에 쌓인 공시는 한 번에 묶어 1건만 보낸다(재개 첫날 폭주 방지).
                rows = "; ".join(f"{it['datetime'][:10]} {it['title']}" for it in stale)
                status = f"알림을 쉬는 동안 나온 주요 공시 {len(stale)}건: {rows}. 원문: {stale[0]['url']}"
                alerts.append((f"[REMED] 302550 쉬는 동안 나온 공시 {len(stale)}건 | 원문 확인 전 진입 보류",
                               body(stamp, status, "목록의 공시 원문을 먼저 읽으세요. CB 정정이면 인수자, 전환가 2,954원, 리픽싱 조건이 바뀌었는지 봅니다.",
                                    LATER, TRIM, AVOID), 3))
            for it in fresh:
                t = it["title"]
                cb_fix = "정정" in t and "전환사채" in t
                title = "[REMED] 302550 CB 정정공시 | 인수자, 조건 확인 전 진입 보류" if cb_fix else "[REMED] 302550 새 주요 공시 | 내용 확인 전 진입 보류"
                status = f"{it['datetime'][:16].replace('T', ' ')} 공시: {t}. 원문: {it['url']}"
                action = ("인수자 변경(메드테크1호 조합 예정)과 전환가 2,954원, 리픽싱 없음 조건이 바뀌었는지 원문을 보세요."
                          if cb_fix else "공시 원문을 먼저 읽으세요. 희석(CB, 유상증자)이나 실적 변화면 기준선을 다시 봅니다.")
                alerts.append((title, body(stamp, status, action, LATER, TRIM, AVOID), 4))
        state["seen_notices"] = sorted(seen | {it["id"] for it in items})[-60:]

    gate = cfg.get("q3_gate") or {}
    if gate and not state.get("q3_done"):
        try:
            op, st = quarter_op(code, str(gate.get("period_key") or "202609"))
        except Exception as exc:  # noqa: BLE001
            op, st = None, f"error {exc}"
        print(f"q3 {gate.get('period_key')} op={op} status={st}")
        if st == "actual" and op is not None:
            grade, action, pri = q3_grade(op, gate)
            status = (f"3분기 영업이익 {op:,.0f}억 원(네이버 분기 실적, 정수 억 단위). 직전 2분기 9억, 1분기 7억. "
                      f"판정 {grade}: 9억 이상 유지 / 5~8억 둔화 / 5억 미만 악화.")
            alerts.append((f"[REMED] 302550 3분기 영업이익 {op:,.0f}억 | {grade}",
                           body(stamp, status, action, "분기보고서 원문(DART)에서 일회성 손익과 순현금을 확인하세요. " + LATER, TRIM, AVOID), pri))
            state["q3_done"] = {"op": op, "grade": grade, "at": stamp}

    for title, msg, pri in alerts:
        print(title)
        print(msg)
        if not dry:
            publish(topic, title, msg, pri, ["chart_with_downwards_trend"], server)
    if not alerts:
        print("no alert")
    if dry:
        print("DRY RUN: no publish, state not written")
        return 0
    state["updated_at"] = now.isoformat()
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
