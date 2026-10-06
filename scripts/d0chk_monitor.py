#!/usr/bin/env python3
"""0005D0 checklist. Existing 09:00 and 15:20 KST slots. Each hit once."""
from __future__ import annotations

import json
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parents[1]
CFG_PATH = ROOT / "configs" / "d0chk.json"
STATE_PATH = ROOT / "state" / "d0chk.json"
UA = {"User-Agent": "Mozilla/5.0 cwally-alerts"}


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
    return re.sub(r"\s+", "", text or "").upper()


def excluded(title: str, terms: list[str]) -> bool:
    text = compact(title)
    return any(compact(term) in text for term in terms)


def quote(symbol: str) -> dict | None:
    payload = json.loads(http_get(f"https://m.stock.naver.com/api/stock/{symbol}/basic"))
    price = num(payload.get("closePrice") or payload.get("nowPrice"))
    volume = num(payload.get("accumulatedTradingVolume") or payload.get("accTradeVolume"))
    if price is None:
        return None
    return {"price": price, "volume": volume or 0}


def notices(code: str) -> list[dict]:
    url = f"https://stock.naver.com/api/domestic/detail/notice?itemCode={code}&startIdx=0&pageSize=20"
    raw = json.loads(http_get(url))
    rows = raw if isinstance(raw, list) else raw.get("result") or raw.get("list") or []
    items = []
    for row in rows:
        title = (row.get("title") or "").strip()
        date = (row.get("datetime") or row.get("date") or "")[:10]
        nid = str(row.get("no") or f"{code}:{date}:{title}")
        if title:
            items.append({"id": f"{code}:{nid}", "date": date, "title": title})
    return items


def is_q3_earn(title: str) -> bool:
    text = compact(title)
    if "잠정실적" not in text and "영업실적" not in text:
        return False
    return any(token in text for token in ("3분기", "7월", "8월", "9월", "2026.07", "2026.08", "2026.09"))


def is_lfp_start(title: str) -> bool:
    text = compact(title)
    if "LFP" not in text:
        return False
    start = any(token in text for token in ("양산개시", "생산개시", "본격생산", "양산시작", "양산돌입"))
    place = any(token in text for token in ("미국", "인디애나", "STARPLUS", "스텔란티스", "SPE", "합작"))
    return start and place


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


def write_state(prev: dict, **updates) -> None:
    prev.update(updates)
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(prev, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def body(stamp: str, what: str, status: str, now_do: str, later: str, cut: str, dont: str) -> str:
    return "\n".join([
        f"{stamp} | 0005D0 체크리스트",
        f"이게 뭐 알림: {what}",
        f"지금 결과/상태: {status}",
        f"지금 할 일: {now_do}",
        f"나중에: {later}",
        f"익절/줄일 때: {cut}",
        f"하지 말 것: {dont}",
    ])


def main() -> int:
    dry = "--dry-run" in sys.argv
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    today = now.strftime("%Y-%m-%d")
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    if now.year != int(cfg.get("year_guard") or 2026) or today > cfg.get("valid_until", "2026-12-31"):
        print("year guard or expired; silent")
        return 0
    if now.weekday() >= 5:
        print("weekend; silent")
        return 0

    prev = load_json(
        STATE_PATH,
        {
            "price_sent": False,
            "earn_sent": False,
            "lfp_sent": False,
            "last_fail_date": None,
        },
    )
    if prev.get("price_sent") and prev.get("earn_sent") and prev.get("lfp_sent"):
        print("all three already sent; silent")
        return 0

    price_err = None
    quote_row = None
    try:
        quote_row = quote(cfg["code"])
    except Exception as exc:
        price_err = str(exc)
        print(f"price fetch failed: {exc}", file=sys.stderr)

    notice_err = None
    earn_rows: list[dict] = []
    lfp_rows: list[dict] = []
    try:
        terms = cfg.get("exclude_terms") or []
        for row in notices(cfg["earn_code"]):
            if excluded(row["title"], terms):
                continue
            if is_q3_earn(row["title"]):
                earn_rows.append(row)
        for row in notices(cfg["lfp_code"]):
            if excluded(row["title"], terms):
                continue
            if is_lfp_start(row["title"]):
                lfp_rows.append(row)
    except Exception as exc:
        notice_err = str(exc)
        print(f"notice fetch failed: {exc}", file=sys.stderr)

    if quote_row is None and notice_err:
        if prev.get("last_fail_date") != today and not dry:
            publish(
                "cwally-ops",
                "[0005D0] 조회 실패 | 재실행 필요",
                body(
                    stamp,
                    "체크리스트 조회가 시세와 공시 둘 다 실패했습니다. 조건 미충족이 아닙니다.",
                    f"시세 오류 {price_err} | 공시 오류 {notice_err}",
                    "다음 평일 09:00 또는 15:20 재실행을 기다립니다.",
                    "한 쪽만 되면 그 조건만 다시 봅니다.",
                    "해당 없음.",
                    "실패를 관망으로 보지 말 것.",
                ),
                4,
            )
        write_state(prev, updated_at=stamp, last_fail_date=today, last_action="DATA_FAIL", price_err=price_err, notice_err=notice_err)
        return 1

    sent = []
    price = quote_row["price"] if quote_row else None
    volume = quote_row["volume"] if quote_row else 0
    if (
        not prev.get("price_sent")
        and today <= cfg.get("price_until", "2026-12-31")
        and price is not None
        and volume > 0
        and price <= float(cfg["price_trigger"])
    ):
        title = "[0005D0] 15300 도달 | 주식 1% 매수"
        msg = body(
            stamp,
            "0005D0 정규장 1차 매수 조건입니다. 전고체 뉴스가 아닙니다.",
            f"체결가 {price:,.0f}원, 당일 거래량 있음. 기준 15,300원 이하.",
            "주식 포트폴리오의 1%만 매수. 갭으로 더 낮으면 그 가격에 1%.",
            "2차 1%는 에코프로비엠 3분기 영업이익 180억 원 이상과 삼성SDI 미국 각형 LFP 10월 양산 개시가 둘 다 확인되고 가격이 15,800원 이하일 때만.",
            "14,500원이 거래량과 깨이면 2차를 열지 말 것.",
            "17,000원 추격. 신용·미수. 전고체 MOU나 목표주가로 2차를 대체.",
        )
        print(msg)
        if not dry:
            publish("cwally-port", title, msg, 4)
        prev["price_sent"] = True
        prev["price_sent_at"] = stamp
        sent.append("price")

    if not prev.get("earn_sent") and today <= cfg.get("earn_until", "2026-11-15") and earn_rows:
        row = earn_rows[0]
        title = "[0005D0] 에코프로 3분기 공시 | 180억 확인"
        msg = body(
            stamp,
            "에코프로비엠 3분기 잠정실적 공시입니다. 2차 1% 조건 A입니다.",
            f"{row['date']} {row['title']}",
            "연결 영업이익이 180억 원 이상이면 조건 A 통과. 미만이면 2차 취소, 1차만 유지.",
            "조건 B인 삼성SDI 미국 각형 LFP 10월 양산 개시가 같이 통과되고 가격이 15,800원 이하일 때만 추가 1%.",
            "180억 원 미만이면 추가 없음.",
            "공시 제목만 보고 통과 처리. MOU와 목표주가는 조건이 아님.",
        )
        print(msg)
        if not dry:
            publish("cwally-port", title, msg, 4)
        prev["earn_sent"] = True
        prev["earn_sent_at"] = stamp
        prev["earn_title"] = row["title"]
        sent.append("earn")

    if not prev.get("lfp_sent") and today <= cfg.get("lfp_until", "2026-10-31") and lfp_rows:
        row = lfp_rows[0]
        title = "[0005D0] SDI LFP 양산 | 조건 B 통과"
        msg = body(
            stamp,
            "삼성SDI 미국 각형 LFP 양산 개시 발표입니다. 2차 1% 조건 B입니다.",
            f"{row['date']} {row['title']}",
            "회사 보도자료 또는 공시에 개시일이 있으면 조건 B 통과. 증권사 추정은 통과가 아님.",
            "조건 A인 에코프로비엠 3분기 영업이익 180억 원 이상과 가격 15,800원 이하가 같이 있을 때만 추가 1%.",
            "10월 31일까지 회사 확인이 없으면 2차 취소.",
            "UL 화재 시험, 원통형 LFP 공개, 전고체 MOU를 양산 개시로 보지 말 것.",
        )
        print(msg)
        if not dry:
            publish("cwally-port", title, msg, 4)
        prev["lfp_sent"] = True
        prev["lfp_sent_at"] = stamp
        prev["lfp_title"] = row["title"]
        sent.append("lfp")

    write_state(
        prev,
        updated_at=stamp,
        last_price=price,
        last_volume=volume,
        last_fail_date=prev.get("last_fail_date"),
        last_action=",".join(sent) if sent else "WATCH",
        price_err=price_err,
        notice_err=notice_err,
    )
    if not sent:
        print(f"no checklist hit; silent price={price} volume={volume} earn={len(earn_rows)} lfp={len(lfp_rows)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
