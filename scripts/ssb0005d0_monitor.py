#!/usr/bin/env python3
"""0005D0 condition watch. Weekday 15:20 KST only. Silent unless a condition changes."""
from __future__ import annotations

import json
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parents[1]
CFG_PATH = ROOT / "configs" / "ssb0005d0.json"
STATE_PATH = ROOT / "state" / "ssb0005d0.json"
UA = {"User-Agent": "Mozilla/5.0 cwally-alerts"}
SKIP = ("MOU", "업무협약", "맞손", "공동개발", "목표주가", "투자의견", "리포트")


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


def num(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).replace(",", "").strip()
    if not text or text in {"-", "N/A"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def compact(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def quote(symbol: str) -> dict | None:
    url = f"https://m.stock.naver.com/api/stock/{symbol}/basic"
    payload = json.loads(http_get(url))
    price = num(payload.get("closePrice") or payload.get("nowPrice"))
    value = num(payload.get("accumulatedTradingValue") or payload.get("accTradePrice"))
    volume = num(payload.get("accumulatedTradingVolume") or payload.get("accTradeVolume"))
    if price is None:
        return None
    return {"price": price, "value": value, "volume": volume}


def history(symbol: str) -> list[dict]:
    url = (
        "https://api.stock.naver.com/chart/domestic/item/"
        + symbol
        + "/day?startDateTime=20260801&endDateTime=20261231"
    )
    raw = json.loads(http_get(url))
    rows = raw if isinstance(raw, list) else raw.get("result") or raw.get("priceInfos") or []
    out = []
    for row in rows:
        price = num(row.get("closePrice") or row.get("close"))
        value = num(row.get("accumulatedTradingValue") or row.get("tradingValue"))
        day = str(row.get("localDate") or row.get("date") or "")[:10]
        if price is not None:
            out.append({"date": day, "price": price, "value": value})
    return out


def notices(code: str) -> list[dict]:
    url = f"https://stock.naver.com/api/domestic/detail/notice?itemCode={code}&startIdx=0&pageSize=15"
    raw = json.loads(http_get(url))
    rows = raw if isinstance(raw, list) else raw.get("result") or raw.get("list") or []
    items = []
    for row in rows:
        title = (row.get("title") or "").strip()
        date = (row.get("datetime") or row.get("date") or "")[:10]
        nid = str(row.get("no") or f"{code}:{date}:{title}")
        if title:
            items.append({"id": f"{code}:{nid}", "code": code, "date": date, "title": title})
    return items


def classify(title: str, role: str, rules: dict) -> str | None:
    text = compact(title)
    if any(compact(k) in text for k in SKIP):
        return None
    kinds = []
    if role == "delay_or_sample":
        if any(compact(k) in text for k in rules.get("down_delay") or []):
            kinds.append("DELAY")
        if any(compact(k) in text for k in rules.get("up_sample") or []):
            kinds.append("SAMPLE")
    if role == "earnings" and any(compact(k) in text for k in rules.get("down_earn") or []):
        kinds.append("EARN")
    if not kinds:
        return None
    return "+".join(dict.fromkeys(kinds))


def publish(topic: str, title: str, message: str, priority: int) -> None:
    if topic not in {"cwally-port", "cwally-ops"}:
        topic = "cwally-port"
    payload = json.dumps(
        {
            "topic": topic,
            "title": title,
            "message": message,
            "priority": priority,
            "tags": ["chart_with_upwards_trend"],
        }
    ).encode()
    req = urllib.request.Request(
        "https://ntfy.sh",
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        print(resp.read().decode())


def zone_of(price: float | None, value: float | None, avg_value: float | None, cfg: dict) -> str:
    if price is None:
        return "NONE"
    below = price < float(cfg["price_break"])
    weak = False
    if value is not None and avg_value and avg_value > 0:
        weak = value < float(cfg["value_weak_ratio"]) * avg_value
    if below and weak:
        return "TREND"
    if below and avg_value is None:
        return "BELOW_NOV"
    if below:
        return "BELOW"
    return "WATCH"


def write_state(prev: dict, **updates) -> None:
    prev.update(updates)
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(prev, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    dry = "--dry-run" in sys.argv
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    today = now.strftime("%Y-%m-%d")
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    if now.year != int(cfg.get("year_guard") or 2026) or today > cfg.get("valid_until", "2026-12-31"):
        print("year guard or expired; silent")
        return 0

    prev = load_json(
        STATE_PATH,
        {
            "updated_at": None,
            "price": None,
            "zone": None,
            "seen_ids": [],
            "baseline_done": False,
            "last_fail_date": None,
            "last_action": None,
        },
    )
    price_err = None
    quote_row = None
    rows: list[dict] = []
    try:
        quote_row = quote(cfg["code"])
        rows = history(cfg["code"])
    except Exception as exc:
        price_err = str(exc)
        print(f"price fetch failed: {exc}", file=sys.stderr)

    notice_err = None
    found: list[dict] = []
    try:
        names = {item["code"]: item["name"] for item in cfg["watch"]}
        roles = {item["code"]: item["role"] for item in cfg["watch"]}
        for item in cfg["watch"]:
            for row in notices(item["code"]):
                row["name"] = names[item["code"]]
                row["kind"] = classify(row["title"], roles[item["code"]], cfg.get("rules") or {})
                found.append(row)
    except Exception as exc:
        notice_err = str(exc)
        print(f"notice fetch failed: {exc}", file=sys.stderr)

    if quote_row is None and notice_err:
        if prev.get("last_fail_date") != today and not dry:
            publish(
                "cwally-ops",
                "[0005D0] 조회 실패 | 재실행 필요",
                "\n".join([
                    f"{stamp} | 0005D0 조건 감시",
                    "이게 뭐 알림: 시세와 공시 조회가 같이 실패했습니다. 조건 미충족이 아닙니다.",
                    f"지금 상태: 시세 오류 {price_err or '없음'} | 공시 오류 {notice_err}",
                    "지금 할 일: 다음 평일 15:20 재실행을 기다립니다.",
                    "나중에: 한 쪽만 되면 그 값으로 조건을 다시 봅니다.",
                    "익절/줄일 때: 해당 없음.",
                    "하지 말 것: 실패를 관망으로 보지 말 것.",
                ]),
                4,
            )
        write_state(prev, updated_at=stamp, last_fail_date=today, last_action="DATA_FAIL", price_err=price_err, notice_err=notice_err)
        return 1

    price = quote_row["price"] if quote_row else None
    value = quote_row.get("value") if quote_row else None
    prior = [row for row in rows if row.get("date") and row["date"] < today and row.get("value")]
    lookback = int(cfg.get("value_lookback") or 20)
    sample = prior[-lookback:]
    avg_value = sum(row["value"] for row in sample) / len(sample) if sample else None
    zone = zone_of(price, value, avg_value, cfg)
    prev_zone = prev.get("zone")
    zone_hit = zone in {"TREND", "BELOW", "BELOW_NOV"} and prev_zone != zone
    seen = list(prev.get("seen_ids") or [])
    baseline = bool(prev.get("baseline_done"))
    fresh = [row for row in found if row.get("kind") and row["id"] not in seen]
    if not baseline:
        fresh = []
    seen_ids = (seen + [row["id"] for row in found])[-200:]

    if not (zone_hit or fresh):
        print(f"no 0005D0 event; silent zone={zone} price={price} baseline={baseline or True}")
        write_state(
            prev,
            updated_at=stamp,
            price=price,
            value=value,
            avg_value=avg_value,
            zone=zone if price is not None else prev_zone,
            seen_ids=seen_ids,
            baseline_done=True,
            last_fail_date=prev.get("last_fail_date"),
            last_action="WATCH",
            price_err=price_err,
            notice_err=notice_err,
        )
        return 0

    px = f"{price:,.0f}원" if price is not None else "시세 없음"
    avg_txt = f"{avg_value:,.0f}원" if avg_value else "확인 불가"
    val_txt = f"{value:,.0f}원" if value is not None else "확인 불가"
    delay = [row for row in fresh if "DELAY" in (row.get("kind") or "")]
    earn = [row for row in fresh if "EARN" in (row.get("kind") or "")]
    if zone == "TREND":
        action = "15,000원 미만이고 당일 대금이 20일 평균의 50% 미만. 추세 훼손으로 보고 추가매수 보류."
        title = "[0005D0] 1.5만 이탈+대금 위축 | 추가매수 보류"
    elif zone in {"BELOW", "BELOW_NOV"}:
        action = "15,000원 미만. 대금 위축은 함께 확인되지 않아 추세 훼손으로 보지 않음. 추가매수 보류."
        title = "[0005D0] 1.5만 미만 | 추가매수 보류"
    elif delay:
        action = "양산·일정 지연 키워드 공시. 원문 확인 전 비중 상향 금지."
        title = "[0005D0] 일정 지연 공시 | 비중 상향 보류"
    elif earn:
        action = "소재 잠정실적 공시. 하향 여부는 원문 확인 전 판단 금지. 비중 줄일 점검."
        title = "[0005D0] 소재 실적 공시 | 원문 확인"
    else:
        action = "샘플·양산 확정 키워드 공시. 원문에 고객 채택이 있으면 판단을 다시 엽니다."
        title = "[0005D0] 샘플 공시 | 원문 확인"
    lines = [
        f"{stamp} | 0005D0 조건 감시",
        "이게 뭐 알림: 전고체 ETF 판단 변경 조건입니다. 매수 신호가 아닙니다.",
        f"지금 상태: {px} | 당일 대금 {val_txt} | 20일 평균 대금 {avg_txt} | 구간 {zone}",
        f"지금 할 일: {action}",
        "나중에: 삼성SDI·LG에너지솔루션 고객 샘플 채택 또는 양산 일정 확정 공시가 있으면 판단을 다시 엽니다.",
        "익절/줄일 때: 15,000원 미만과 대금 위축이 같이 나오면 추가매수 보류. 소재 실적 하향 공시면 비중 줄일 점검.",
        "하지 말 것: 키워드를 확정 일정으로 보기. 신용·미수. 15:20 가격을 확정 종가로 보기. MOU와 목표주가는 조건이 아님.",
    ]
    if fresh:
        lines.append("")
        lines.append("새 공시:")
        for row in fresh[:8]:
            lines.append(f"- [{row.get('kind')}] {row.get('name')} {row.get('date')} {row.get('title')}")
    if price_err or notice_err:
        lines.append(f"조회 부분 실패: 시세 {price_err or '없음'} | 공시 {notice_err or '없음'}")
    msg = "\n".join(lines)
    print(msg)
    if not dry:
        publish("cwally-port", title, msg, 4 if zone == "TREND" or delay else 3)
    write_state(
        prev,
        updated_at=stamp,
        price=price,
        value=value,
        avg_value=avg_value,
        zone=zone if price is not None else prev_zone,
        seen_ids=seen_ids,
        baseline_done=True,
        last_fail_date=prev.get("last_fail_date"),
        last_action=title,
        price_err=price_err,
        notice_err=notice_err,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
