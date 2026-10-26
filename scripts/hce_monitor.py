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
CFG_PATH = ROOT / "configs" / "hce.json"
STATE_PATH = ROOT / "state" / "hce.json"
EVENT_ZONES = {"KILL", "REDUCE"}
NOTICE_CUTOFF = "2026-09-01"
ZONE_KO = {
    "KILL": "폐기선 11.4만 이하",
    "REDUCE": "축소선 11.4만~12.2만",
    "WAIT": "12.2만 위. 선취 금지",
    "HOLD": "유지 확인 가격",
    "PENDING": "가격 미확정",
    "DATA_FAIL": "시세 조회 실패",
}
DO_NOW = {
    "KILL": "3~6개월 스윙을 버리세요. 선취하지 마세요.",
    "REDUCE": "축소로 봅니다. 11.4만을 다시 깨면 폐기입니다.",
    "WAIT": "실적 익일 종가까지 선취하지 마세요.",
    "HOLD": "유지입니다. 13.0만~13.7만 회복을 봅니다.",
    "PENDING": "익일 종가로 유지/축소를 갈라세요.",
    "DATA_FAIL": "시세를 다시 확인하세요.",
}
TITLE_BY_ZONE = {
    "KILL": "11.4만 이탈 폐기",
    "REDUCE": "12.2만 이하 축소",
    "WAIT": "선취 금지 | 익일 확인",
    "HOLD": "유지",
    "PENDING": "미확정",
    "DATA_FAIL": "시세 확인",
}


def print_window(print_d):
    if not print_d:
        return None, None, None
    d0 = datetime.strptime(print_d, "%Y-%m-%d")
    prenight = (d0 + timedelta(days=-1)).strftime("%Y-%m-%d")
    nxt = (d0 + timedelta(days=1)).strftime("%Y-%m-%d")
    return prenight, print_d, nxt


def http_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 cwally-alerts"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode())


def http_text(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 cwally-alerts"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.read().decode("utf-8", "ignore")


def fetch_3q_notice():
    """3Q earnings IR/잠정실적 notice via stock.naver.com notice API.

    Legacy finance.naver news_notice is 410 and m.stock front-api/disclosure is 404.
    Only notices whose body is about 3Q results count; roadshow/Corporate Day IR does not.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import naver_api

    try:
        items = naver_api.notices("267270", 30)
    except Exception as exc:
        print(f"notice api fail: {exc}", file=sys.stderr)
        return None
    for it in items:
        title, text = it["title"], it["text"]
        day = it["datetime"][:10]
        if day < NOTICE_CUTOFF:
            continue
        if "영업(잠정)" in title or "잠정실적" in title:
            if "2026-07-01" not in text:
                continue
            return {"title": title.strip(), "date": day, "kind": "print"}
        if "기업설명회" in title or "IR" in title:
            if not any(k in text for k in ("3분기", "3Q", "'26년 3분기", "26년 3분기")):
                continue
            m = __import__("re").search(r"일시\s*(2026-\d{2}-\d{2})", text)
            return {"title": title.strip(), "date": m.group(1) if m else day, "kind": "ir"}
    return None


def yahoo_price(ticker: str):
    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        + urllib.parse.quote(ticker)
        + "?interval=1d&range=5d"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 cwally-alerts"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read().decode())
        meta = payload["chart"]["result"][0].get("meta", {})
        price = meta.get("regularMarketPrice") or meta.get("previousClose")
        return float(price) if price is not None else None
    except Exception as exc:
        print(f"price fetch failed {ticker}: {exc}", file=sys.stderr)
        return None


def zone(price, bands):
    if price is None:
        return "DATA_FAIL"
    if price <= bands["kill"]:
        return "KILL"
    if price <= bands["reduce_hi"]:
        return "REDUCE"
    return "WAIT"


def active_events(cfg, today):
    return [ev for ev in cfg.get("events", []) if ev["start"] <= today <= ev["end"]]


def upcoming_events(cfg, today, days=21):
    lim = (datetime.strptime(today, "%Y-%m-%d") + timedelta(days=days)).strftime("%Y-%m-%d")
    return [ev for ev in cfg.get("events", []) if today < ev["start"] <= lim]


def ntfy_publish(server, topic, title, message, priority, tags):
    # ntfy_util.publish: same routing/payload, appends the "꺼지는 때" end line; skips after ends_kst + grace.
    ntfy_util.publish(topic, title, message, priority, tags.split(","), server, config_name="hce")


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "daily").lower()
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    topic = "cwally-port"
    server = "https://ntfy.sh"
    now = datetime.now(KST)
    today = now.strftime("%Y-%m-%d")
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    if now.year != int(cfg.get("year_guard") or 2026):
        # 2026 3Q 스윙 카드. GitHub one-shot cron(10/27~10/29)에는 연도가 없어 내년에도 울리므로 연도 밖이면 침묵.
        print("year guard; silent")
        return 0
    confirmed = bool(cfg.get("print_date_confirmed"))
    print_d = (cfg.get("print_date") or "").strip() or None
    notice = fetch_3q_notice()
    new_confirm = False
    if notice and not confirmed:
        print_d = notice["date"]
        confirmed = True
        # announce once: the config flag is never written back, so before 2026-10-26 every run re-announced the
        # same notice (p5 "발표일 확정" daily). state/hce.json keeps the confirmed date.
        try:
            st = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            st = {}
        new_confirm = not (st.get("print_date_confirmed") and st.get("print_date") == notice["date"])
        print(f"3Q notice found: {notice} new_confirm={new_confirm}")
    prenight_d, print_d, next_d = print_window(print_d)
    oneshot_dates = {d for d in (prenight_d, print_d, next_d) if d}

    if print_d and today in oneshot_dates and today[:4] != print_d[:4]:
        print("oneshot year-guard; silent")
        return 0

    prev = {"zone": None, "price": None, "last_event_ids": [], "updated_at": None}
    if STATE_PATH.exists():
        try:
            prev.update(json.loads(STATE_PATH.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass

    price = yahoo_price(cfg["ticker"])
    z = zone(price, cfg["bands"])
    if z == "DATA_FAIL" and prev.get("zone"):
        z = prev["zone"]
        price = prev.get("price")

    live = active_events(cfg, today)
    soon = upcoming_events(cfg, today)
    live_ids = [e["id"] for e in live]
    new_live = [e for e in live if e["id"] not in (prev.get("last_event_ids") or [])]
    zone_changed = prev.get("zone") and prev.get("zone") != z and (
        z in EVENT_ZONES or prev.get("zone") in EVENT_ZONES
    )
    oneshot = today in oneshot_dates
    prenight = bool(prenight_d) and today == prenight_d
    print_day = bool(print_d) and today == print_d
    next_day = bool(next_d) and today == next_d

    send = True
    if mode == "event" and not (zone_changed or new_live or oneshot or new_confirm):
        print("no hce event; silent")
        send = False

    px = f"{price:,.0f}원" if price is not None else "시세 없음"
    cap = cfg["max_equity_weight_pct"]
    date_note = (
        f"발표일 {print_d} 확정"
        if confirmed
        else f"발표일 {print_d or '미설정'} 미확정"
    )
    lines = [
        f"{stamp} | HCE | {cfg['display']}",
        f"이게 뭐 알림: HD현대건설기계({cfg['code']}) 3~6개월 스윙 카드입니다. {date_note}.",
        f"지금 상태: {px} | {ZONE_KO.get(z, z)}",
        f"지금 할 일: {DO_NOW.get(z, '')}",
        "나중에: 실적은 익일 종가로 확인. OP 1,500억 미만 폐기, 1,700억 축소, 1,701~1,899억 미확정, 1,900억 이상 유지 대기.",
        "익절/줄일 때: 종가 114,000원 이탈 또는 OP 1,500억 미만이면 폐기. 114,000~122,000원은 축소.",
        f"하지 말 것: 선취, 추격, 신용/미수, 총자산 {cap}% 초과. 2Q 10.2% 재현을 기대하지 말 것.",
    ]
    if new_confirm and notice:
        lines.append("")
        lines.append(f"공시 확인: {notice['title']} / {notice['date']}")
        lines.append("oneshot cron은 전날 21:00, 당일 15:40, 익일 15:40 KST로 옷겨야 합니다.")
    if prenight:
        lines.append("")
        lines.append("발표 전날 밤 확인:")
        for item in cfg.get("pre_night", []):
            lines.append(f"- {item}")
        lines.append("하나라도 깨지면 선취하지 말고 1단계부터 보세요. " + date_note + ".")
    if live:
        lines.append("")
        lines.append("지금 진행 중인 일정:")
        for e in live:
            lines.append(f"- {e['label']} {e['start']}~{e['end']}: {e['check']}")
    if soon:
        lines.append("")
        lines.append("21일 안 일정:")
        for e in soon:
            lines.append(f"- {e['label']} {e['start']}")

    STATE_PATH.write_text(
        json.dumps(
            {
                "updated_at": stamp,
                "zone": z,
                "price": price,
                "last_event_ids": live_ids,
                "print_date": print_d,
                "print_date_confirmed": confirmed,
                "notice": notice,
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
        title = f"[HCE] {TITLE_BY_ZONE.get(z, z)}"
        if new_confirm:
            title = f"[HCE] 발표일 확정 | {print_d}"
        elif prenight:
            title = "[HCE] 발표 전날 | 선취 금지"
        elif print_day:
            title = f"[HCE] 당일 종가 | {TITLE_BY_ZONE.get(z, z)}"
        elif next_day:
            title = f"[HCE] 익일 종가 | {TITLE_BY_ZONE.get(z, z)}"
        elif zone_changed:
            title = f"[HCE] 구간 변화 | {TITLE_BY_ZONE.get(z, z)}"
        elif new_live:
            title = f"[HCE] 일정 창 | {TITLE_BY_ZONE.get(z, z)}"
        elif mode != "event" and now.weekday() == 0:
            title = f"[HCE] 주간 체크 | {TITLE_BY_ZONE.get(z, z)}"
        pri = 5 if z == "KILL" or prenight or new_confirm else (4 if z == "REDUCE" or zone_changed or oneshot else 3)
        ntfy_publish(server, topic, title, msg, pri, "tractor,chart_with_upwards_trend")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
