#!/usr/bin/env python3
"""RBLX watch: price zone, Q3 bookings guide, new filing. Silent unless change."""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
CFG_PATH = ROOT / "configs" / "rblx.json"
STATE_PATH = ROOT / "state" / "rblx.json"
UA = {"User-Agent": "cwally-alerts rblx research monitor research@example.com"}
WINDOW = {"2026-10-29", "2026-10-30"}


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


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=25) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def quote(url: str) -> tuple[float | None, str | None]:
    data = json.loads(fetch(url))
    result = (data.get("chart") or {}).get("result") or []
    if not result:
        return None, "quote empty"
    price = (result[0].get("meta") or {}).get("regularMarketPrice")
    try:
        return (float(price) if price is not None else None), None
    except (TypeError, ValueError):
        return None, "price parse"


def latest_sec_date(url: str) -> str | None:
    data = json.loads(fetch(url))
    recent = ((data.get("filings") or {}).get("recent") or {})
    forms = recent.get("form") or []
    dates = recent.get("filingDate") or []
    for form, day in zip(forms, dates):
        if form in {"8-K", "10-Q", "10-K"}:
            return day
    return None


def visible(html: str) -> str:
    text = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", html)
    text = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text)


def parse_bookings(blob: str) -> float | None:
    m = re.search(
        r"bookings[^.]{0,80}?\$\s*([0-9]+(?:\.[0-9]+)?)\s*(billion|million)",
        blob,
        re.I,
    )
    if not m:
        return None
    val = float(m.group(1))
    return val * (1000 if m.group(2).lower() == "billion" else 1)


def zone_of(price: float, low: float, high: float) -> str:
    if price <= low:
        return "below_38"
    if price > high:
        return "above_55"
    return "hold"


def body(stamp: str, status: str, action: str, later: str, trim: str, avoid: str) -> str:
    return "\n".join(
        [
            f"{stamp} | RBLX | 관망 조건 점검",
            "이게 뭐 알림: 보유 중인 로블록스 가격 구간과 Q3 bookings 조건만 보는 알림입니다. 추가매수 지시가 아닙니다.",
            f"지금 결과/상태: {status}",
            f"지금 할 일: {action}",
            f"나중에: {later}",
            f"익절/줄일 때: {trim}",
            f"하지 말 것: {avoid}",
        ]
    )


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    topic = resolve_topic("port", cfg.get("ntfy_topic"))
    server = resolve_server(cfg.get("ntfy_server"))
    if mode == "install":
        publish(
            topic,
            "[RBLX] 설치 확인 | 매매 신호 아님",
            body(
                stamp,
                "평일 09:00 KST morning 슬롯에 붙였습니다. 기준가 2026-10-06 45.56달러. Q3 bookings 가이던스 15.76~16.53억 달러.",
                "설치 확인만 하세요. 사지 마세요.",
                "월요 상태, 38/55달러 구간 변경, 10/29·10/30, 새 8-K/10-Q만 보냅니다.",
                "이번 알림으로 비중을 바꾸지 마세요.",
                "설치 확인을 매수나 매도 신호로 읽기.",
            ),
            3,
            ["information_source"],
            server,
        )
        print("install confirmation sent")
        return 0

    dry = mode == "dry" or os.environ.get("RBLX_DRY") == "1"
    state = load_json(
        STATE_PATH,
        {
            "updated_at": None,
            "last_price": None,
            "zone": "hold",
            "last_weekly": None,
            "last_filing": cfg["baseline"]["last_known_filing"],
            "earnings_fingerprint": None,
            "sent_dates": [],
            "fetch_fail_date": None,
        },
    )
    today = now.date()
    if today.isoformat() > cfg["valid_until"] and mode not in {"force", "dry"}:
        print(f"after {cfg['valid_until']}; silent")
        return 0

    base = cfg["baseline"]
    levels = cfg["levels"]
    price, qerr = None, None
    try:
        price, qerr = quote(cfg["sources"]["quote"])
    except Exception as exc:
        qerr = str(exc)
    print(f"price={price} qerr={qerr}")

    alerts: list[tuple[str, str, int]] = []
    if price is None:
        if state.get("fetch_fail_date") != today.isoformat():
            alerts.append(
                (
                    "[RBLX] 주가 조회 실패 | 조건 미확인",
                    body(
                        stamp,
                        f"주가 조회 실패. {qerr or '빈 응답'}. 조건 미충족이 아닙니다.",
                        "증권 앱에서 RBLX 전일 종가를 직접 보세요. 38달러, 55달러를 이 알림으로 판정하지 마세요.",
                        "다음 평일 09:00 KST에 재시도합니다.",
                        "이번 알림으로 줄이거나 사지 마세요.",
                        "조회 실패를 38달러 이탈이나 정상으로 취급.",
                    ),
                    4,
                )
            )
            state["fetch_fail_date"] = today.isoformat()
    else:
        zone = zone_of(price, levels["jefferies"], levels["hold_ceiling"])
        prev = state.get("zone") or "hold"
        changed = zone != prev
        weekly = today.weekday() == 0 and state.get("last_weekly") != today.isoformat()
        window = today.isoformat() in WINDOW and today.isoformat() not in (state.get("sent_dates") or [])
        if changed or weekly or window or mode == "force":
            if zone == "below_38":
                title = "[RBLX] 38달러 이하 | 보유 재검토"
                action = "보유는 유지하되 추가하지 마세요. 38달러 아래는 약세 목표가 안이라 비중 재검토 구간입니다."
            elif zone == "above_55":
                title = "[RBLX] 55달러 위 | 추가 금지"
                action = "보유는 유지하세요. 55달러 위 추가는 금지입니다."
            else:
                title = "[RBLX] 38~55달러 | 보유 유지"
                action = "보유 유지, 추가 없음. 10/29 실적 전 비중을 바꾸지 마세요."
            why = "구간 변경" if changed else "월요일 상태" if weekly else "실적 창"
            alerts.append(
                (
                    title,
                    body(
                        stamp,
                        f"RBLX {price:.2f}달러. 기준일 {base['price_date']} {base['price']}달러. 구간 {prev} -> {zone}. {why}. Q3 bookings 가이드 15.76~16.53억 달러.",
                        action,
                        "보유 유지. 상향은 bookings 16.53억 초과와 미국/캐나다 시간당 수익화 반등. 하향은 15.76억 미만이면 비중 재검토.",
                        "55달러 위 추가 금지. 38달러 아래는 보유 재검토이지 추가매수 아님.",
                        "목표가 터치를 추가매수 신호로 쓰기. 신용, 미수, 추격.",
                    ),
                    4 if zone != "hold" else 3,
                )
            )
            state["zone"] = zone
            state["last_price"] = price
            if weekly:
                state["last_weekly"] = today.isoformat()
            if window:
                sent = list(state.get("sent_dates") or [])
                sent.append(today.isoformat())
                state["sent_dates"] = sent
        else:
            state["last_price"] = price
            state["zone"] = zone
            print(f"price zone unchanged ({zone}); silent")

    filing = None
    ferr = None
    try:
        filing = latest_sec_date(cfg["sources"]["sec"])
    except Exception as exc:
        ferr = str(exc)
        print(f"sec fail: {exc}")
    last_filing = state.get("last_filing") or base["last_known_filing"]
    if filing and last_filing and filing > last_filing:
        ir_text = ""
        try:
            ir_text = visible(fetch(cfg["sources"]["ir"]))
        except Exception as exc:
            print(f"ir fail: {exc}")
        bookings = parse_bookings(ir_text) if ir_text else None
        fingerprint = json.dumps({"filing": filing, "bookings_m": bookings}, sort_keys=True)
        if fingerprint != state.get("earnings_fingerprint"):
            lo = base["q3_bookings_lo_m"]
            hi = base["q3_bookings_hi_m"]
            if bookings is None:
                title = "[RBLX] 새 공시 | bookings 확인 불가"
                status = f"새 공시 {filing}. IR에서 bookings를 읽지 못했습니다. 미확인이며 미충족이 아닙니다."
                action = "ir.roblox.com 원문에서 bookings와 미국/캐나다 수익화를 직접 보세요."
                pri = 4
            elif bookings < lo:
                title = "[RBLX] 실적 | 가이드 하단 미달"
                status = f"새 공시 {filing}. 파싱 bookings {bookings:.0f}백만 달러, 가이드 하단 {lo} 미만."
                action = "비중 축소 쪽으로 보세요. 파싱값과 원문이 다르면 원문을 우선하세요."
                pri = 5
            elif bookings > hi:
                title = "[RBLX] 실적 | 가이드 상단 초과"
                status = f"새 공시 {filing}. 파싱 bookings {bookings:.0f}백만 달러, 가이드 상단 {hi} 초과. 미국/캐나다 수익화는 이 알림이 확인하지 못했습니다."
                action = "관망을 유지하고 원문에서 미국/캐나다 시간당 수익화 반등을 확인하세요. 그 전 매수 전환 아님."
                pri = 4
            else:
                title = "[RBLX] 실적 | 가이드 안"
                status = f"새 공시 {filing}. 파싱 bookings {bookings:.0f}백만 달러, 가이드 {lo}~{hi} 안."
                action = "관망 유지. 가이드 안은 상향 조건이 아닙니다."
                pri = 3
            alerts.append(
                (
                    title,
                    body(
                        stamp,
                        status,
                        action,
                        "상향은 상단 초과와 미국/캐나다 수익화 반등이 같이 있을 때입니다.",
                        "파싱 실패나 가이드 안을 매수 전환으로 쓰지 마세요.",
                        "확인 불가를 충족으로 세기. 주가 반응으로 조건 충족 단정.",
                    ),
                    pri,
                )
            )
            state["earnings_fingerprint"] = fingerprint
            state["last_filing"] = filing
            state["last_bookings_m"] = bookings
    elif ferr and state.get("fetch_fail_date") != today.isoformat() and today.weekday() == 0:
        alerts.append(
            (
                "[RBLX] 공시 조회 실패 | 실적 조건 미확인",
                body(
                    stamp,
                    f"SEC 조회 실패. {ferr}. 실적 조건은 미확인입니다.",
                    "ir.roblox.com 최신 실적을 직접 보세요.",
                    "다음 월요일에 재시도합니다.",
                    "이번 알림으로 비중을 바꾸지 마세요.",
                    "조회 실패를 조건 미충족으로 두기.",
                ),
                4,
            )
        )
        state["fetch_fail_date"] = today.isoformat()

    for title, msg, pri in alerts:
        print(title)
        print(msg)
        print(f"topic={topic} send={not dry}")
        if not dry:
            publish(topic, title, msg, pri, ["chart_with_upwards_trend"], server)
    if not alerts:
        print("no alert")

    state["updated_at"] = now.isoformat()
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
