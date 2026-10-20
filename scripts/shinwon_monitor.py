#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
import ntfy_util  # noqa: E402

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parents[1]
CFG_PATH = ROOT / "configs" / "shinwon.json"
STATE_PATH = ROOT / "state" / "shinwon.json"
UA = {
    "User-Agent": "Mozilla/5.0 cwally-alerts",
    "Referer": "https://stock.naver.com/domestic/stock/009270/notice",
}

EVENT_ZONES = {"STOP898", "STOP1000", "SELL1", "SELL2", "TIME"}
ZONE_KO = {
    "STOP898": "52주 저점 898원 탈출. 전량",
    "STOP1000": "종가 1,000원 이하. 전량",
    "SELL1": "1,200~1,230원. 95주 1차 매도",
    "SELL2": "1,270원 이상. 잔량 매도",
    "TIME": "11/13 시간손절. 1,200 미도달 전량",
    "WATCH": "규칙 구간 밖. 물타기 금지",
    "ENDED": "8주 창 종료",
    "DATA_FAIL": "시세 조회 실패",
}
DO_NOW = {
    "STOP898": "190주 전량 매도. 재진입 금지.",
    "STOP1000": "종가 1,000원 이하. 190주 전량. 11/13을 기다리지 마세요.",
    "SELL1": "95주 지정가 매도. 나머지는 1,270원대.",
    "SELL2": "남은 95주 전량 매도.",
    "TIME": "11/13까지 1,200원을 못 갔습니다. 190주 전량.",
    "WATCH": "조건 주문 없이 관찰. 물타기 하지 말 것.",
    "ENDED": "시간창 끝. 새 공시만 참고.",
    "DATA_FAIL": "네이버·야후에서 현재가를 다시 확인하세요.",
}
TITLE_BY_ZONE = {
    "STOP898": "898 탈출 전량",
    "STOP1000": "1,000 종가 전량",
    "SELL1": "1,200대 95주 매도",
    "SELL2": "1,270이상 잔량 매도",
    "TIME": "11/13 전량",
    "WATCH": "관찰 유지",
    "ENDED": "창 종료",
    "DATA_FAIL": "시세 확인",
}
DISC_RULES = [
    ("EARN", ("분기보고서", "반기보고서", "사업보고서", "잠정", "손익구조", "매출액또는손익"), "3Q 영업이익률 2% 미만이면 전량. 패션 적자도 같이 보세요."),
    ("CB", ("전환사채", "전환청구", "전환권", "사채상환", "전환가액"), "CB 잔여 전환·상환이면 오버행. 1,200 추격 금지."),
    ("TS", ("자기주식", "주식소각", "자사주"), "자사주 소각이면 1차 95주만 매도. 2차는 소각 다음날까지 보류."),
]


def load_json(path: Path, fallback: dict) -> dict:
    if not path.exists():
        return dict(fallback)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        fallback.update(data)
        return fallback
    except json.JSONDecodeError:
        return fallback


def http_get(url: str, timeout: int = 20) -> str:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def yahoo_price(ticker: str):
    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        + urllib.parse.quote(ticker)
        + "?interval=1d&range=5d"
    )
    payload = json.loads(http_get(url))
    meta = payload["chart"]["result"][0].get("meta", {})
    price = meta.get("regularMarketPrice") or meta.get("previousClose")
    return float(price) if price is not None else None


def zone(price, cfg, today: str) -> str:
    end = cfg["window_end"]
    bands = cfg["bands"]
    if today > end:
        return "ENDED"
    if price is None:
        return "DATA_FAIL"
    if price <= bands["stop_hard"]:
        return "STOP898"
    if price <= bands["stop_close"]:
        return "STOP1000"
    if today >= end and price < bands["sell1_lo"]:
        return "TIME"
    if price >= bands["sell2_lo"]:
        return "SELL2"
    if bands["sell1_lo"] <= price <= bands["sell1_hi"]:
        return "SELL1"
    return "WATCH"


def classify_title(title: str) -> str | None:
    t = re.sub(r"\s+", "", title)
    for kind, keys, _hint in DISC_RULES:
        if any(k.replace(" ", "") in t for k in keys):
            return kind
    return None


def parse_naver_notice(code: str) -> list[dict]:
    url = f"https://stock.naver.com/api/domestic/detail/notice?itemCode={code}&startIdx=0&pageSize=20"
    raw = json.loads(http_get(url))
    rows = raw if isinstance(raw, list) else raw.get("result") or raw.get("list") or []
    items = []
    for row in rows:
        title = (row.get("title") or "").strip()
        date = (row.get("datetime") or "")[:10]
        nid = str(row.get("no") or f"{date}:{title}")
        if title:
            items.append({"id": nid, "date": date, "title": title, "src": "naver"})
    return items[:20]


def collect_disclosures(code: str) -> list[dict]:
    found: dict[str, dict] = {}
    try:
        for it in parse_naver_notice(code):
            it["kind"] = classify_title(it["title"])
            if it["kind"]:
                found[it["id"]] = it
    except Exception as exc:
        print(f"naver notice failed: {exc}", file=sys.stderr)
    return list(found.values())


def hint_for(kind: str) -> str:
    for k, _keys, hint in DISC_RULES:
        if k == kind:
            return hint
    return "공시 원문을 열어 확인하세요."


def publish(title: str, message: str, priority: int, tags: str) -> None:
    # ntfy_util.publish: same topic routing, tags, server and end line; skips after ends_kst + grace.
    ntfy_util.publish("cwally-port", title, message, priority, tags.split(","), "https://ntfy.sh", config_name="shinwon")


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "event").lower()
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    today = now.strftime("%Y-%m-%d")
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    prev = load_json(
        STATE_PATH,
        {"updated_at": None, "zone": None, "price": None, "seen_ids": [], "time_stop_sent": False},
    )

    price = None
    try:
        price = yahoo_price(cfg["ticker"])
    except Exception as exc:
        print(f"price fetch failed: {exc}", file=sys.stderr)

    z = zone(price, cfg, today)
    if z == "DATA_FAIL" and prev.get("zone") not in (None, "DATA_FAIL"):
        z = prev["zone"]
        price = prev.get("price")

    discs = collect_disclosures(cfg["code"])
    print("tracked discs: " + ", ".join(f"{d.get('kind')}:{d.get('date')}:{d.get('title')}" for d in discs[:8]) or "none")
    seen = list(prev.get("seen_ids") or [])
    cutoff = cfg.get("as_of") or today
    new_discs = [d for d in discs if d["id"] not in seen and d.get("date", "") >= cutoff]
    seen_ids = (seen + [d["id"] for d in discs])[-80:]

    zone_changed = bool(prev.get("zone")) and prev.get("zone") != z and (
        z in EVENT_ZONES or prev.get("zone") in EVENT_ZONES
    )
    time_fire = z == "TIME" and not prev.get("time_stop_sent")
    force = mode in {"force", "weekly"}

    send = bool(force or zone_changed or new_discs or time_fire or mode != "event")
    if not send:
        print("no shinwon event; silent")

    px = f"{price:,.0f}원" if price is not None else "시세 없음"
    cost = cfg["cost"]
    shares = cfg["shares"]
    pnl = f"{(price / cost - 1) * 100:+.1f}%" if price else "없음"
    value = f"{int(price * shares):,}원" if price else "없음"
    action = DO_NOW.get(z, "")
    if any(d.get("kind") == "EARN" for d in new_discs):
        action = "실적 공시 확인. 영업이익률 2% 미만이거나 패션 적자가 안 줄었으면 11/13 전에 전량. " + action
    if any(d.get("kind") == "CB" for d in new_discs):
        action = "CB 공시 확인. 오버행이면 1,200 추격 금지. " + action
    if any(d.get("kind") == "TS" for d in new_discs):
        action = "자사주 소각이면 95주만 팔고 잔량은 다음날. " + action

    lines = [
        f"{stamp} | SHINWON | {cfg['display']}({cfg['code']})",
        "이게 뭐 알림: 신원 190주 감시. 평단 1,451원. 8주 창은 11/13까지.",
        f"지금 상태: {px} | 평단 대비 {pnl} | 평가 {value} | {ZONE_KO.get(z, z)}",
        f"지금 할 일: {action}",
        "나중에: 3Q 영업이익률·패션 적자·CB 잔여·자사주 소각만 보세요.",
        "익절/줄일 때: 1,200~1,230원 95주, 1,270원 이상 잔량. 본전 1,451은 목표 아님.",
        "하지 말 것: 물타기, 1,000원 종가 무시, 대북 테마 추격.",
    ]
    if new_discs:
        lines.append("")
        lines.append("새 공시:")
        for d in new_discs[:6]:
            lines.append(f"- [{d.get('kind')}] {d['date']} {d['title']}")
            lines.append(f"  → {hint_for(d.get('kind') or '')}")

    STATE_PATH.write_text(
        json.dumps(
            {
                "updated_at": stamp,
                "zone": z,
                "price": price,
                "seen_ids": seen_ids,
                "time_stop_sent": bool(prev.get("time_stop_sent") or z == "TIME"),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    title_bit = TITLE_BY_ZONE.get(z, z)
    if new_discs:
        kinds = "/".join(sorted({d.get("kind") or "DISC" for d in new_discs}))
        title = f"[SHINWON] 공시 {kinds} | {title_bit}"
    elif time_fire:
        title = "[SHINWON] 11/13 전량"
    elif zone_changed:
        title = f"[SHINWON] 구간 변화 | {title_bit}"
    else:
        title = f"[SHINWON] {title_bit}"
    msg = "\n".join(lines)
    print(msg)
    if send:
        hot = z in {"STOP898", "STOP1000", "TIME"} or any(d.get("kind") in {"EARN", "CB", "TS"} for d in new_discs)
        pri = 5 if hot else (4 if z in {"SELL1", "SELL2"} or zone_changed else 3)
        publish(title, msg, pri, "warning,chart_with_downwards_trend")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
