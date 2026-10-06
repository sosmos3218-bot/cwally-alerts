#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from ntfy_util import route_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parents[1]
CFG_PATH = ROOT / "configs" / "0220wl.json"
STATE_PATH = ROOT / "state" / "0220wl.json"
EVENT_ZONES = {"STOP20", "HOLD10", "TRIM12", "TRIM20", "TRIM35"}
ZONE_KO = {
    "TRIM35": "+35% 익절 (7,783원)",
    "TRIM20": "+20% 익절 (6,918원)",
    "TRIM12": "+12% 익절 (6,457원)",
    "HOLD10": "-10% 구간 (5,189원). 추가매수 없음",
    "STOP20": "-20% 검토 (4,612원)",
    "WATCH": "규칙 구간 밖. 87주 유지",
    "DATA_FAIL": "시세 조회 실패",
    "QUALITY2": "품질악화 2개",
    "EXIT": "품질악화 2개 + 4,612원 폐기",
}
DO_NOW = {
    "TRIM35": "87주 중 상당 부분을 팔고 전환·배당 대기 분만 남기세요.",
    "TRIM20": "87주 중 절반을 줄이세요.",
    "TRIM12": "약 30주를 지정가로 팔아 1차 익절하세요.",
    "HOLD10": "추가매수 없이 분할배정분을 유지하세요.",
    "STOP20": "신규매수 정지. 87주 중 43주 수동 매도를 검토하세요.",
    "WATCH": "87주 위성으로 그대로 두세요.",
    "DATA_FAIL": "시세를 다시 확인하세요.",
    "QUALITY2": "87주 중 43주 축소를 검토하세요.",
    "EXIT": "87주 잔량을 폐기하세요. 30일 재매수 금지.",
}
TITLE_BY_ZONE = {
    "TRIM35": "+35% 전환대기분만",
    "TRIM20": "+20% 절반 축소",
    "TRIM12": "+12% 30주 익절",
    "HOLD10": "-10% 추가매수 금지",
    "STOP20": "-20% 43주 매도 검토",
    "WATCH": "87주 유지",
    "DATA_FAIL": "시세 확인",
    "QUALITY2": "품질악화 | 43주 검토",
    "EXIT": "폐기 | 30일 재매수 금지",
}


def yahoo_chart(ticker: str):
    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        + urllib.parse.quote(ticker)
        + "?interval=1d&range=2mo"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 cwally-alerts"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        payload = json.loads(resp.read().decode())
    result = payload["chart"]["result"][0]
    meta = result.get("meta", {})
    price = meta.get("regularMarketPrice") or meta.get("chartPreviousClose") or meta.get("previousClose")
    quote = (result.get("indicators", {}).get("quote", [{}]) or [{}])[0]
    closes = [x for x in (quote.get("close") or []) if x is not None]
    volumes = [x for x in (quote.get("volume") or []) if x is not None]
    ret20 = None
    if len(closes) > 20:
        ret20 = closes[-1] / closes[-21] - 1
    elif len(closes) > 5:
        ret20 = closes[-1] / closes[0] - 1
    avg5 = sum(volumes[-5:]) / 5 if len(volumes) >= 5 else None
    avg20 = sum(volumes[-20:]) / min(20, len(volumes)) if volumes else None
    return (float(price) if price is not None else None), ret20, avg5, avg20


def zone(price, bands):
    if price is None:
        return "DATA_FAIL"
    if price <= bands["stop20"]:
        return "STOP20"
    if price <= bands["hold10"]:
        return "HOLD10"
    if price >= bands["trim35"]:
        return "TRIM35"
    if price >= bands["trim20"]:
        return "TRIM20"
    if price >= bands["trim12"]:
        return "TRIM12"
    return "WATCH"


def ntfy_publish(server, topic, title, message, priority, tags):
    payload = json.dumps(
        {"topic": route_topic(topic, title), "title": title, "message": message, "priority": priority, "tags": tags.split(",")}
    ).encode()
    req = urllib.request.Request(server.rstrip("/"), data=payload, method="POST", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        print(resp.read().decode())


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "event").lower()
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    topic = "cwally-port"
    server = "https://ntfy.sh"
    now = datetime.now(KST)
    today = now.strftime("%Y-%m-%d")
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    prev = {
        "zone": None,
        "price": None,
        "common_price": None,
        "ratio": None,
        "quality_flags": [],
        "last_event_ids": [],
        "updated_at": None,
    }
    if STATE_PATH.exists():
        try:
            prev.update(json.loads(STATE_PATH.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass

    price = ret20 = avg5 = avg20 = None
    common = bench20 = None
    try:
        price, ret20, avg5, avg20 = yahoo_chart(cfg["ticker"])
    except Exception as exc:
        print(f"price fetch failed: {exc}", file=sys.stderr)
    try:
        common, bench20, _, _ = yahoo_chart(cfg["bench_ticker"])
    except Exception as exc:
        print(f"bench fetch failed: {exc}", file=sys.stderr)

    z = zone(price, cfg["bands"])
    if z == "DATA_FAIL" and prev.get("zone"):
        z = prev["zone"]
        price = prev.get("price")
        common = prev.get("common_price") if common is None else common

    ratio = (price / common) if price and common else None
    lag = (ret20 - bench20) if ret20 is not None and bench20 is not None else None
    vol_ratio = (avg5 / avg20) if avg5 and avg20 else None
    qcfg = cfg["quality"]
    flags = []
    if ratio is not None and ratio < qcfg["gap_ratio_min"]:
        flags.append("GAP")
    if lag is not None and lag <= qcfg["rel_lag"]:
        flags.append("LAG")
    if vol_ratio is not None and vol_ratio <= qcfg["vol_dry"]:
        flags.append("VOL")

    if z == "STOP20" and len(flags) >= qcfg["hits_exit"]:
        action_key = "EXIT"
    elif len(flags) >= qcfg["hits_exit"]:
        action_key = "QUALITY2"
    else:
        action_key = z

    live = [ev for ev in cfg.get("events", []) if ev["start"] <= today <= ev["end"]]
    live_ids = [e["id"] for e in live]
    new_live = [e for e in live if e["id"] not in (prev.get("last_event_ids") or [])]
    zone_changed = bool(prev.get("zone")) and prev.get("zone") != z and (
        z in EVENT_ZONES or prev.get("zone") in EVENT_ZONES
    )
    prev_flags = prev.get("quality_flags") or []
    quality_new = bool(set(flags) - set(prev_flags)) or (
        len(flags) >= qcfg["hits_exit"] and len(prev_flags) < qcfg["hits_exit"]
    )

    send = False
    title = f"[0220WL] {TITLE_BY_ZONE.get(action_key, action_key)}"
    if mode == "event":
        if zone_changed or new_live or quality_new or action_key in {"QUALITY2", "EXIT"}:
            send = True
            if action_key == "EXIT":
                title = "[0220WL] 폐기점검 | 30일 재매수 금지"
            elif new_live:
                title = f"[0220WL] 시간점검 | {TITLE_BY_ZONE.get(action_key, action_key)}"
            elif quality_new or action_key == "QUALITY2":
                title = "[0220WL] 품질악화 | 43주 검토"
            else:
                title = f"[0220WL] 매도점검 | {TITLE_BY_ZONE.get(action_key, action_key)}"
        else:
            print("no 0220wl event; silent")
    else:
        send = True
        title = f"[0220WL] 점검 | {TITLE_BY_ZONE.get(action_key, action_key)}"

    px = f"{price:,.0f}원" if price is not None else "시세 없음"
    cx = f"{common:,.0f}원" if common is not None else "없음"
    cost = cfg["cost"]
    shares = cfg.get("shares_phase1", 0)
    pnl = f"{(price / cost - 1) * 100:+.1f}%" if price else "없음"
    ratio_s = f"{ratio:.3f}" if ratio is not None else "없음"
    qtxt = ",".join(flags) if flags else "없음"
    lines = [
        f"{stamp} | 0220WL | {cfg['display']}",
        f"이게 뭐 알림: 한화M&S 3우B {shares}주 매도 검토입니다. 원가 {cost:,}원은 분할배정분이며 신규매수 단가가 아닙니다.",
        f"지금 상태: 우선 {px} / 보통 {cx} / 비율 {ratio_s} / 원가 대비 {pnl} / {ZONE_KO.get(action_key, action_key)} / 품질 {qtxt}",
        f"지금 할 일: {DO_NOW.get(action_key, '')}",
        "나중에: 2026-10 배당·전환조건, 2026-12 상대성과, 2027-06 12개월, 2028-06 교체 판단.",
        "익절: +12% / +20% / +35% 구간에서 순차 축소.",
        "줄일 때: 품질악화(고려 비율<0.32, 상대-8%p, 거래량 35%) 2개면 43주. 같이 4,612원이면 잔량 폐기.",
        "하지 말 것: 평가손익만 보고 추가 매수. 손절 후 30일 재매수. 이율로 비중 갈이지 말 것.",
    ]
    if ret20 is not None and bench20 is not None and lag is not None:
        lines.append(f"우선 20일 {ret20 * 100:+.1f}% | 보통 20일 {bench20 * 100:+.1f}% | 상대 {lag * 100:+.1f}%p")
    if vol_ratio is not None:
        lines.append(f"거래량 5일/20일 {vol_ratio:.2f}")
    if live:
        lines.append("")
        lines.append("시간 점검:")
        for e in live:
            lines.append(f"- {e['label']} {e['start']}~{e['end']}: {e['check']}")

    STATE_PATH.write_text(
        json.dumps(
            {
                "updated_at": stamp,
                "zone": z,
                "price": price,
                "common_price": common,
                "ratio": ratio,
                "quality_flags": flags,
                "last_event_ids": live_ids,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    msg = "\n".join(lines)
    print(msg)
    if send:
        pri = 5 if action_key in {"STOP20", "EXIT"} or new_live else (
            4 if z in EVENT_ZONES or zone_changed or quality_new else 3
        )
        ntfy_publish(server, topic, title, msg, pri, "warning,chart_with_downwards_trend")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
