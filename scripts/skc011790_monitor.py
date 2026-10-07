#!/usr/bin/env python3
"""SKC(011790) condition watch. Weekday 15:20 KST only. Silent unless a condition changes."""
from __future__ import annotations

import json
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from ntfy_util import append_ends, route_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parents[1]
CFG_PATH = ROOT / "configs" / "skc011790.json"
STATE_PATH = ROOT / "state" / "skc011790.json"
UA = {"User-Agent": "Mozilla/5.0 cwally-alerts"}

RULES = [
    ("PREVIEW", ("결산실적공시예고", "결산실적공시 예고", "결산실적 예고")),
    ("EARN", ("잠정실적", "손익구조")),
    ("CERT", ("고객인증", "인증통과", "공급계약", "단일판매", "양산", "수주계약", "앱솔릭스납품")),
    ("RAISE", ("유상증자", "채무보증", "자금보충", "출자증권취득")),
    ("SALE", ("지분매각", "영업양도", "매각결정")),
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


def compact(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def classify(title: str) -> str | None:
    t = compact(title)
    kinds = []
    for kind, keys in RULES:
        if any(compact(k) in t for k in keys):
            kinds.append(kind)
    if not kinds:
        return None
    return "+".join(dict.fromkeys(kinds))


def extract_dates(text: str, year: int) -> list[str]:
    found = []
    for y, m, d in re.findall(r"(20\d{2})\s*[.에/-]\s*(\d{1,2})\s*[.월/-]\s*(\d{1,2})", text or ""):
        found.append(f"{int(y):04d}-{int(m):02d}-{int(d):02d}")
    for m, d in re.findall(r"(\d{1,2})\s*월\s*(\d{1,2})\s*일", text or ""):
        found.append(f"{year:04d}-{int(m):02d}-{int(d):02d}")
    out = []
    for item in found:
        try:
            datetime.strptime(item, "%Y-%m-%d")
        except ValueError:
            continue
        if item not in out:
            out.append(item)
    return out


def parse_notices(code: str) -> list[dict]:
    url = f"https://stock.naver.com/api/domestic/detail/notice?itemCode={code}&startIdx=0&pageSize=20"
    raw = json.loads(http_get(url))
    rows = raw if isinstance(raw, list) else raw.get("result") or raw.get("list") or []
    items = []
    for row in rows:
        title = (row.get("title") or "").strip()
        date = (row.get("datetime") or row.get("date") or "")[:10]
        body = row.get("contents") or row.get("content") or row.get("body") or ""
        nid = str(row.get("no") or f"{code}:{date}:{title}")
        if title:
            items.append({
                "id": f"{code}:{nid}",
                "date": date,
                "title": title,
                "body": str(body),
                "kind": classify(title),
            })
    return items[:20]


def publish(topic: str, title: str, message: str, priority: int) -> None:
    message = append_ends(message)  # AGENTS.md "Alert end line"
    if topic not in {"cwally-port", "cwally-ops"}:
        topic = "cwally-port"
    payload = json.dumps(
        {
            "topic": route_topic(topic, title),
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


def zone_of(price: float | None, cfg: dict) -> str:
    if price is None:
        return "NONE"
    if price <= cfg["consensus_low"]:
        return "LOW"
    if price <= cfg["gap15_price"]:
        return "GAP"
    if price >= cfg["consensus_high"]:
        return "HIGH"
    return "MID"


def checklist(cfg: dict) -> list[str]:
    e = cfg["earnings"]
    return [
        f"매출: 3분기 콘센서스 약 {e['revenue_consensus_bn']}억. 2분기 {e['q2_revenue_bn']}억. 미달이면 추격 금지.",
        f"영업이익: 2분기 {e['q2_op_bn']}억. 적자 축소만으로는 판단 유지. 2분기 연속 흑자라야 상향 조건.",
        f"동박 가동률: 손익분기 약 {e['foil_breakeven_util_pct']}%. {e['foil_note']}",
        "앱솔릭스 고객 인증: 공급계약·납품·매출 인식 문구가 있는지. 출자 공시만으로는 조건 미충족.",
        "순차입: 실적 공시의 순차입·순부채. 10/1 기준 EV가 시총보다 약 3.4조 커서 부채 부담이 남아 있음.",
    ]


def confirmed_preview_date(item: dict, today: str, year: int) -> str | None:
    if "PREVIEW" not in (item.get("kind") or ""):
        return None
    dates = extract_dates(f"{item.get('title') or ''} {item.get('body') or ''}", year)
    future = [d for d in dates if today <= d <= f"{year}-12-31"]
    return future[0] if future else None


def update_check_date(cfg: dict, date: str) -> bool:
    earnings = cfg.setdefault("earnings", {})
    if earnings.get("checklist_dates") == [date]:
        return False
    earnings["checklist_dates"] = [date]
    # config is read-only; the confirmed date is persisted in state/skc011790.json (checklist_dates)
    return True


def main() -> int:
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    today = now.strftime("%Y-%m-%d")
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    if now.year != int(cfg.get("year_guard") or 2026) or today > cfg.get("valid_until", "2026-12-31"):
        print("year guard or expired; silent")
        return 0

    prev = load_json(
        STATE_PATH,
        {"updated_at": None, "price": None, "zone": None, "seen_ids": [], "checklist_sent": [], "last_fail_date": None, "last_action": None},
    )
    if prev.get("checklist_dates"):
        cfg.setdefault("earnings", {})["checklist_dates"] = list(prev["checklist_dates"])
    price = None
    price_err = None
    try:
        price = yahoo_price(cfg["ticker"])
    except Exception as exc:
        price_err = str(exc)
        print(f"price fetch failed: {exc}", file=sys.stderr)
    notice_err = None
    notices: list[dict] = []
    try:
        notices = parse_notices(cfg["code"])
    except Exception as exc:
        notice_err = str(exc)
        print(f"notice fetch failed: {exc}", file=sys.stderr)

    if price is None and notice_err:
        if prev.get("last_fail_date") != today:
            publish(
                "cwally-ops",
                "[SKC] 조회 실패 | 재실행 필요",
                "\n".join([
                    f"{stamp} | SKC 011790 조건 감시",
                    "이게 뭐 알림: 시세와 공시 조회가 같이 실패했습니다. 조건 미충족이 아닙니다.",
                    f"지금 상태: 시세 오류 {price_err or '없음'} | 공시 오류 {notice_err}",
                    "지금 할 일: 다음 15:20 재실행을 기다립니다. 수동 확인은 네이버 종목공시.",
                    "나중에: 한 쪽만 되면 그 값으로 조건을 다시 봅니다.",
                    "익절/줄일 때: 해당 없음.",
                    "하지 말 것: 실패를 관망으로 보지 말 것.",
                ]),
                4,
            )
        prev["last_fail_date"] = today
        prev["updated_at"] = stamp
        STATE_PATH.write_text(json.dumps(prev, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return 1

    zone = zone_of(price, cfg)
    prev_zone = prev.get("zone")
    zone_changed = prev_zone not in (None, zone) and zone in {"LOW", "GAP", "HIGH"}
    seen = list(prev.get("seen_ids") or [])
    cutoff = cfg.get("as_of") or today
    new_discs = [d for d in notices if d.get("kind") and d["id"] not in seen and d.get("date", "") >= cutoff]
    seen_ids = (seen + [d["id"] for d in notices])[-120:]
    sent = set(prev.get("checklist_sent") or [])
    due = today in (cfg.get("earnings") or {}).get("checklist_dates", []) and today not in sent
    preview_new = [d for d in new_discs if "PREVIEW" in (d.get("kind") or "")]
    earn_new = [d for d in new_discs if "EARN" in (d.get("kind") or "")]
    date_updated = None
    for item in preview_new:
        found = confirmed_preview_date(item, today, now.year)
        item["scheduled"] = found
        if found and update_check_date(cfg, found):
            date_updated = found
            break
    for item in earn_new:
        dates = extract_dates(f"{item.get('title') or ''} {item.get('body') or ''}", now.year)
        item["scheduled"] = dates[0] if dates else None

    if not (zone_changed or new_discs or due or preview_new or earn_new):
        print(f"no skc event; silent zone={zone} price={price}")
        STATE_PATH.write_text(
            json.dumps(
                {
                    "updated_at": stamp,
                    "price": price,
                    "zone": zone if price is not None else prev_zone,
                    "seen_ids": seen_ids,
                    "checklist_sent": list(sent),
                    "last_fail_date": prev.get("last_fail_date"),
                    "last_action": "WATCH",
                    "official_date": (cfg.get("earnings") or {}).get("official_date"),
                    "checklist_dates": (cfg.get("earnings") or {}).get("checklist_dates"),
                    "price_err": price_err,
                    "notice_err": notice_err,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        return 0

    px = f"{price:,.0f}원" if price is not None else "시세 없음"
    if preview_new:
        sched = preview_new[0].get("scheduled") or "확인 불가"
        action = f"예고 공시. 예정 실적일 {sched}. " + (
            f"체크 날짜를 {date_updated}로 바꿈." if date_updated else "날짜가 없어 체크 날짜는 안 바꿈."
        )
        title = "[SKC] 실적 예고 | 날짜 확인"
    elif earn_new:
        action = "잠정실적 공시. 원문 확인 전 비중 변경 금지."
        title = "[SKC] 잠정실적 | 원문 확인"
    elif zone == "LOW":
        action = "하단 6.9만 이하. 매도의견 구간. 비중 확대 아님. 공시 확인."
        title = "[SKC] 6.9만 이하 | 추가매수 보류"
    elif zone == "GAP":
        action = "컨센서스 10만 대비 15% 아래. 목표가 상향이 아님. 인증 전 관망."
        title = "[SKC] 목표 대비 15% 아래 | 관망"
    elif zone == "HIGH":
        action = "컨센서스 상단 13만. 줄일 구간 점검. 추격 금지."
        title = "[SKC] 13만 상단 | 줄일 점검"
    elif due:
        action = "실적 체크리스트. 원문 확인 전 비중 변경 금지."
        title = "[SKC] 3분기 체크 | 비중 보류"
    else:
        action = "조건 공시. 원문 확인 전 매수 금지."
        title = "[SKC] 조건 공시 | 원문 확인"
    lines = [
        f"{stamp} | SKC 011790 조건 감시",
        "이게 뭐 알림: 실적 예고·잠정실적과 판단 변경 조건을 보는 알림입니다.",
        f"지금 상태: {px} | 구간 {zone} | {action}",
        f"지금 할 일: {action}",
        "나중에: 고객 인증·공급계약 또는 매출 인식, 동박 손익분기, 영업이익 2분기 연속 흑자면 판단을 다시 엽니다.",
        "익절/줄일 때: 13만 원 도달 시 비중 점검. 인증 없이 오른 구간은 추격하지 말 것.",
        "하지 말 것: 추정일을 확정일로 보기. 신용·미수. 잠정실적 접수일을 예정일로 바꾸기.",
        "",
        "체크리스트:",
    ]
    lines.extend(f"- {row}" for row in checklist(cfg))
    if preview_new or earn_new:
        lines.append("")
        lines.append("실적 공시:")
        for d in (preview_new + earn_new)[:6]:
            lines.append(f"- 제목: {d['title']}")
            lines.append(f"  접수일: {d.get('date') or '없음'}")
            lines.append(f"  예정 실적일: {d.get('scheduled') or '확인 불가'}")
    if new_discs:
        lines.append("")
        lines.append("새 공시:")
        for d in new_discs[:8]:
            lines.append(f"- [{d.get('kind')}] {d.get('date')} {d['title']}")
    if price_err or notice_err:
        lines.append(f"조회 부분 실패: 시세 {price_err or '없음'} | 공시 {notice_err or '없음'}")
    msg = "\n".join(lines)
    print(msg)
    publish("cwally-port", title, msg, 4 if preview_new or earn_new or zone in {"LOW", "HIGH"} else 3)
    if due:
        sent.add(today)
    STATE_PATH.write_text(
        json.dumps(
            {
                "updated_at": stamp,
                "price": price,
                "zone": zone if price is not None else prev_zone,
                "seen_ids": seen_ids,
                "checklist_sent": sorted(sent),
                "last_fail_date": prev.get("last_fail_date"),
                "last_action": title,
                "official_date": date_updated or prev.get("official_date"),
                "checklist_dates": (cfg.get("earnings") or {}).get("checklist_dates"),
                "price_err": price_err,
                "notice_err": notice_err,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
