#!/usr/bin/env python3
"""0005D0 condition watch + 1% checklist (d0chk folded in 2026-10-26). Weekday 15:20 KST only.

watch(): zone and filing changes, silent otherwise. checklist(): the old d0chk three one-time items (price <= 15,300
with volume, 247540 Q3 earnings filing, 006400 US LFP start), same rules and state values, config "checklist",
state "checklist". The old 09:00 check is dropped (15:20 only)."""
from __future__ import annotations

import json
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
import ntfy_util  # noqa: E402

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


def notices(code: str, size: int = 15) -> list[dict]:
    url = f"https://stock.naver.com/api/domestic/detail/notice?itemCode={code}&startIdx=0&pageSize={size}"
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


def publish(topic: str, title: str, message: str, priority: int) -> bool:
    # ntfy_util.publish: same topic routing, tags, server and end line; skips after ends_kst + grace.
    ntfy_util.publish(topic, title, message, priority, ["chart_with_upwards_trend"], "https://ntfy.sh", config_name="ssb0005d0")
    return bool(ntfy_util.LAST_PUBLISH.get("sent"))


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
    if "--dry-run" in sys.argv:  # 2026-10-26: a dry run never writes state
        print("DRY RUN: state not written")
        return
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(prev, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def watch() -> tuple[int, str | None]:
    dry = "--dry-run" in sys.argv
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    today = now.strftime("%Y-%m-%d")
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    if now.year != int(cfg.get("year_guard") or 2026) or today > cfg.get("valid_until", "2026-12-31"):
        print("year guard or expired; silent")
        return 0, None

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
        return 1, prev.get("zone")

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
        return 0, (zone if price is not None else prev_zone)

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
        action = "양산, 일정 지연 키워드 공시. 원문 확인 전 비중 상향 금지."
        title = "[0005D0] 일정 지연 공시 | 비중 상향 보류"
    elif earn:
        action = "소재 잠정실적 공시. 하향 여부는 원문 확인 전 판단 금지. 비중 줄일 점검."
        title = "[0005D0] 소재 실적 공시 | 원문 확인"
    else:
        action = "샘플, 양산 확정 키워드 공시. 원문에 고객 채택이 있으면 판단을 다시 엽니다."
        title = "[0005D0] 샘플 공시 | 원문 확인"
    lines = [
        f"{stamp} | 0005D0 조건 감시",
        "이게 뭐 알림: 전고체 ETF 판단 변경 조건입니다. 매수 신호가 아닙니다.",
        f"지금 상태: {px} | 당일 대금 {val_txt} | 20일 평균 대금 {avg_txt} | 구간 {zone}",
        f"지금 할 일: {action}",
        "나중에: 삼성SDI, LG에너지솔루션 고객 샘플 채택 또는 양산 일정 확정 공시가 있으면 판단을 다시 엽니다.",
        "익절/줄일 때: 15,000원 미만과 대금 위축이 같이 나오면 추가매수 보류. 소재 실적 하향 공시면 비중 줄일 점검.",
        "하지 말 것: 키워드를 확정 일정으로 보기. 신용, 미수. 15:20 가격을 확정 종가로 보기. MOU와 목표주가는 조건이 아님.",
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
    # publish skipped (alert ended) -> keep zone and the fresh notice ids unrecorded (dry run writes no state)
    ok = dry or publish("cwally-port", title, msg, 4 if zone == "TREND" or delay else 3)
    fresh_ids = {row["id"] for row in fresh}
    write_state(
        prev,
        updated_at=stamp,
        price=price,
        value=value,
        avg_value=avg_value,
        zone=(zone if price is not None else prev_zone) if ok else prev_zone,
        seen_ids=seen_ids if ok else (seen + [row["id"] for row in found if row["id"] not in fresh_ids])[-200:],
        baseline_done=True,
        last_fail_date=prev.get("last_fail_date"),
        last_action=title,
        price_err=price_err,
        notice_err=notice_err,
    )
    return 0, (zone if price is not None else prev_zone)



# ---------- checklist (folded in from d0chk on 2026-10-26; config "checklist", state "checklist") ----------
def ck_compact(text: str) -> str:
    return compact(text).upper()


def ck_publish(topic: str, title: str, message: str, priority: int) -> bool:
    ntfy_util.publish(topic, title, message, priority, ["chart_with_upwards_trend"], "https://ntfy.sh", config_name="ssb0005d0")
    return bool(ntfy_util.LAST_PUBLISH.get("sent"))


def ck_write(whole: dict, prev: dict, **updates) -> None:
    prev.update(updates)
    if "--dry-run" in sys.argv:  # 2026-10-26: a dry run never writes state (price_sent etc. stay as they were)
        print("DRY RUN: checklist state not written")
        return
    cur = load_json(STATE_PATH, {})  # the watch stage may have written the file in this run
    cur["checklist"] = prev
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(cur, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def ck_excluded(title: str, terms: list[str]) -> bool:
    text = ck_compact(title)
    return any(ck_compact(term) in text for term in terms)


def ck_quote(code: str, today: str) -> dict | None:
    """Price and today's volume. Naver `basic` no longer returns volume (2026-10), so the polling endpoint
    (accumulatedTradingVolumeRaw) is read first; `basic` stays the fallback. traded_today is used only when
    no volume field exists at all: localTradedAt is today and trading is not halted."""
    try:
        raw = json.loads(http_get(f"https://polling.finance.naver.com/api/realtime/domestic/stock/{code}"))
        row = (raw.get("datas") or [None])[0] or {}
        price = num(row.get("closePriceRaw") or row.get("closePrice"))
        if price is not None:
            vol = num(row.get("accumulatedTradingVolumeRaw") or row.get("accumulatedTradingVolume"))
            traded = str(row.get("localTradedAt") or "")[:10] == today and (row.get("tradeStopType") or {}).get("name", "TRADING") == "TRADING"
            return {"price": price, "volume": vol, "traded_today": traded, "src": "polling"}
    except Exception as exc:
        print(f"polling quote failed, basic fallback: {exc}", file=sys.stderr)
    row = quote(code)
    if row is None:
        return None
    payload = json.loads(http_get(f"https://m.stock.naver.com/api/stock/{code}/basic"))
    traded = str(payload.get("localTradedAt") or "")[:10] == today and (payload.get("tradeStopType") or {}).get("name", "TRADING") == "TRADING"
    return {"price": row["price"], "volume": row.get("volume"), "traded_today": traded, "src": "basic"}


def is_q3_earn(title: str, date: str = "", earn_from: str = "2026-10-01") -> bool:
    """Q3 provisional earnings filing. The standard KRX title "연결재무제표 기준 영업(잠정)실적(공정공시)" names no quarter
    (2026-10-26 fix: parentheses are ignored), so a title without a quarter token counts only when filed on or after
    earn_from; the Q2 filing (2026-07-31, same title) and older ones never match. Another quarter named -> no."""
    text = ck_compact(title).replace("(", "").replace(")", "")
    if "잠정실적" not in text and "영업실적" not in text:
        return False
    if "예고" in text:
        return False
    if any(token in text for token in ("3분기", "3Q", "2026.07", "2026.08", "2026.09")):
        return True
    if any(token in text for token in ("1분기", "2분기", "4분기", "반기", "1Q", "2Q", "4Q")):
        return False
    return bool(date) and date[:10] >= earn_from


def is_lfp_start(title: str) -> bool:
    text = ck_compact(title)
    if "LFP" not in text:
        return False
    start = any(token in text for token in ("양산개시", "생산개시", "본격생산", "양산시작", "양산돌입"))
    place = any(token in text for token in ("미국", "인디애나", "STARPLUS", "스텔란티스", "SPE", "합작"))
    return start and place


def ck_body(stamp: str, what: str, status: str, now_do: str, later: str, cut: str, dont: str) -> str:
    return "\n".join([
        f"{stamp} | 0005D0 체크리스트",
        f"이게 뭐 알림: {what}",
        f"지금 결과/상태: {status}",
        f"지금 할 일: {now_do}",
        f"나중에: {later}",
        f"익절/줄일 때: {cut}",
        f"하지 말 것: {dont}",
    ])


def checklist(zone: str | None, dry: bool) -> int:
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))["checklist"]
    now = datetime.now(KST)
    today = now.strftime("%Y-%m-%d")
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    if now.year != int(cfg.get("year_guard") or 2026) or today > cfg.get("valid_until", "2026-12-31"):
        print("year guard or expired; silent")
        return 0
    if now.weekday() >= 5:
        print("weekend; silent")
        return 0

    whole = load_json(STATE_PATH, {})
    prev = {"price_sent": False, "earn_sent": False, "lfp_sent": False, "last_fail_date": None}
    prev.update(whole.get("checklist") or {})
    if prev.get("price_sent") and prev.get("earn_sent") and prev.get("lfp_sent"):
        print("all three already sent; silent")
        return 0

    price_err = None
    quote_row = None
    try:
        quote_row = ck_quote(cfg["code"], today)
    except Exception as exc:
        price_err = str(exc)
        print(f"price fetch failed: {exc}", file=sys.stderr)

    notice_err = None
    earn_rows: list[dict] = []
    lfp_rows: list[dict] = []
    try:
        terms = cfg.get("exclude_terms") or []
        for row in notices(cfg["earn_code"], 20):
            if ck_excluded(row["title"], terms):
                continue
            if is_q3_earn(row["title"], row.get("date") or "", cfg.get("earn_from", "2026-10-01")):
                earn_rows.append(row)
        for row in notices(cfg["lfp_code"], 20):
            if ck_excluded(row["title"], terms):
                continue
            if is_lfp_start(row["title"]):
                lfp_rows.append(row)
    except Exception as exc:
        notice_err = str(exc)
        print(f"notice fetch failed: {exc}", file=sys.stderr)

    if quote_row is None and notice_err:
        if prev.get("last_fail_date") != today and not dry:
            ck_publish(
                "cwally-ops",
                "[0005D0] 조회 실패 | 재실행 필요",
                ck_body(
                    stamp,
                    "체크리스트 조회가 시세와 공시 둘 다 실패했습니다. 조건 미충족이 아닙니다.",
                    f"시세 오류 {price_err} | 공시 오류 {notice_err}",
                    "다음 평일 15:20 재실행을 기다립니다.",
                    "한 쪽만 되면 그 조건만 다시 봅니다.",
                    "해당 없음.",
                    "실패를 관망으로 보지 말 것.",
                ),
                4,
            )
        ck_write(whole, prev, updated_at=stamp, last_fail_date=today, last_action="DATA_FAIL", price_err=price_err, notice_err=notice_err)
        return 1

    sent = []
    price = quote_row["price"] if quote_row else None
    volume = quote_row["volume"] if quote_row else None
    # traded today: volume > 0, or (no volume field at all) a quote stamped today while trading
    traded = bool(quote_row) and ((volume is not None and volume > 0) or (volume is None and quote_row.get("traded_today")))
    if (
        not prev.get("price_sent")
        and today <= cfg.get("price_until", "2026-12-31")
        and price is not None
        and traded
        and price <= float(cfg["price_trigger"])
    ):
        title = "[0005D0] 15300 도달 | 주식 1% 매수"
        msg = ck_body(
            stamp,
            "0005D0 정규장 1차 매수 조건입니다. 전고체 뉴스가 아닙니다.",
            f"체결가 {price:,.0f}원, " + (f"당일 거래량 {volume:,.0f}주" if volume else "당일 체결 확인") + ". 기준 15,300원 이하.",
            "주식 포트폴리오의 1%만 매수. 갭으로 더 낮으면 그 가격에 1%.",
            "2차 1%는 에코프로비엠 3분기 영업이익 180억 원 이상과 삼성SDI 미국 각형 LFP 10월 양산 개시가 둘 다 확인되고 가격이 15,800원 이하일 때만.",
            "14,500원이 거래량과 깨이면 2차를 열지 말 것.",
            "17,000원 추격. 신용, 미수. 전고체 MOU나 목표주가로 2차를 대체.",
        )
        if zone in {"TREND", "BELOW", "BELOW_NOV"}:  # watch says below 15,000: the buy line must not stand alone
            title = "[0005D0] 15300 도달 | 1.5만 미만이라 1% 매수 보류"
            msg = msg.replace("지금 할 일: 주식 포트폴리오의 1%만 매수. 갭으로 더 낮으면 그 가격에 1%.",
                              "지금 할 일: 15,000원 미만이라 같은 종목 조건 감시가 추가매수 보류 구간입니다. 1% 매수는 보류하고 15,000원 위 회복을 먼저 확인.")
        print(msg)
        skipped = not dry and not ck_publish("cwally-port", title, msg, 4)  # ended -> do not record as sent
        if not skipped:
            prev["price_sent"] = True
            prev["price_sent_at"] = stamp
            sent.append("price")

    if not prev.get("earn_sent") and today <= cfg.get("earn_until", "2026-11-15") and earn_rows:
        row = earn_rows[0]
        title = "[0005D0] 에코프로 3분기 공시 | 180억 확인"
        msg = ck_body(
            stamp,
            "에코프로비엠 3분기 잠정실적 공시입니다. 2차 1% 조건 A입니다.",
            f"{row['date']} {row['title']}",
            "연결 영업이익이 180억 원 이상이면 조건 A 통과. 미만이면 2차 취소, 1차만 유지.",
            "조건 B인 삼성SDI 미국 각형 LFP 10월 양산 개시가 같이 통과되고 가격이 15,800원 이하일 때만 추가 1%.",
            "180억 원 미만이면 추가 없음.",
            "공시 제목만 보고 통과 처리. MOU와 목표주가는 조건이 아님.",
        )
        print(msg)
        skipped = not dry and not ck_publish("cwally-port", title, msg, 4)  # ended -> do not record as sent
        if not skipped:
            prev["earn_sent"] = True
            prev["earn_sent_at"] = stamp
            prev["earn_title"] = row["title"]
            sent.append("earn")

    if not prev.get("lfp_sent") and today <= cfg.get("lfp_until", "2026-10-31") and lfp_rows:
        row = lfp_rows[0]
        title = "[0005D0] SDI LFP 양산 | 조건 B 통과"
        msg = ck_body(
            stamp,
            "삼성SDI 미국 각형 LFP 양산 개시 발표입니다. 2차 1% 조건 B입니다.",
            f"{row['date']} {row['title']}",
            "회사 보도자료 또는 공시에 개시일이 있으면 조건 B 통과. 증권사 추정은 통과가 아님.",
            "조건 A인 에코프로비엠 3분기 영업이익 180억 원 이상과 가격 15,800원 이하가 같이 있을 때만 추가 1%.",
            "10월 31일까지 회사 확인이 없으면 2차 취소.",
            "UL 화재 시험, 원통형 LFP 공개, 전고체 MOU를 양산 개시로 보지 말 것.",
        )
        print(msg)
        skipped = not dry and not ck_publish("cwally-port", title, msg, 4)  # ended -> do not record as sent
        if not skipped:
            prev["lfp_sent"] = True
            prev["lfp_sent_at"] = stamp
            prev["lfp_title"] = row["title"]
            sent.append("lfp")

    ck_write(
        whole,
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
        print(f"no checklist hit; silent price={price} volume={volume} traded={traded} earn={len(earn_rows)} lfp={len(lfp_rows)}")
    return 0



def main() -> int:
    """Condition watch first, then the 1% checklist (one script, one 15:20 run)."""
    rc, zone = watch()
    rc2 = checklist(zone, "--dry-run" in sys.argv)
    return rc or rc2


if __name__ == "__main__":
    raise SystemExit(main())
