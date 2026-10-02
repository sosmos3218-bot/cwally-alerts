#!/usr/bin/env python3
"""KOSPI thesis invalidation alert. Silent unless a change condition hits or a fetch fails."""
from __future__ import annotations

import json
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
CFG_PATH = ROOT / "configs" / "kosbrk.json"
STATE_PATH = ROOT / "state" / "kosbrk.json"
UA = {"User-Agent": "Mozilla/5.0 cwally-alerts"}
GUIDE_NEED = (
    "가이던스 하향",
    "가이던스 하회",
    "실적 가이던스 하향",
    "전망치 하향",
    "guidance cut",
    "lowers outlook",
    "lowered its outlook",
    "cuts guidance",
)
GUIDE_NAME = ("삼성전자", "sk하이닉스", "sk 하이닉스", "마이크론", "micron")
GUIDE_NEG = ("소식통", "검토", "전망", "예상", "sources said", "알려졌다")


def load_json(path: Path, fallback: dict) -> dict:
    if not path.exists():
        return dict(fallback)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        out = dict(fallback)
        out.update(data)
        return out
    except json.JSONDecodeError:
        return dict(fallback)


def fetch(url: str, timeout: int = 20) -> str:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    return raw.decode("utf-8", errors="ignore")


def num(text: str) -> float:
    cleaned = str(text).replace(",", "").replace("+", "").strip()
    if not cleaned or cleaned == "-":
        return 0.0
    return float(cleaned)


def save(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def fail_once(state: dict, now: datetime, cfg: dict, title: str, note: str, dry: bool) -> None:
    day = now.strftime("%Y-%m-%d")
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    body = "\n".join(
        [
            f"{stamp} | KOSBRK | 사용자 Cwally",
            "이게 뭐 알림: 코스피 대기 논리 변경 조건 점검 중 조회가 실패했습니다.",
            f"지금 상태: {note}",
            "지금 할 일: 실패를 조건 미충족으로 보지 마세요. 네이버 코스피 종가·외국인 수급을 직접 확인하세요.",
            "나중에: 다음 평일 15:40 KST 슬롯에서 다시 봅니다.",
            "익절/줄일 때: 이 실패로 사거나 팔지 마세요.",
            "하지 말 것: 조회 실패를 저점 이탈로 해석. 신용/미수.",
        ]
    )
    print(body)
    if not dry and state.get("last_fail_day") != day:
        publish(resolve_topic("port", cfg.get("ntfy_topic")), title, body, 4, ["warning"], resolve_server(cfg.get("ntfy_server")))
        state["last_fail_day"] = day
    state["updated_at"] = stamp
    save(state)


def latest_closed(price_rows: list[dict], basic: dict) -> dict | None:
    status = str((basic or {}).get("marketStatus") or "").upper()
    today = datetime.now(KST).strftime("%Y-%m-%d")
    rows = list(price_rows or [])
    if status == "CLOSE" and rows:
        return rows[0]
    for row in rows:
        if str(row.get("localTradedAt") or "")[:10] < today:
            return row
    return None


def guide_hits(items: list[dict], seen: set[str]) -> list[dict]:
    picked = []
    for it in items:
        link = it.get("link") or ""
        if not link or link in seen:
            continue
        text = it.get("text") or ""
        if not any(k in text for k in GUIDE_NAME):
            continue
        if not any(k in text for k in GUIDE_NEED):
            continue
        if any(k in text for k in GUIDE_NEG) and not any(k in text for k in ("공시", "실적발표", "ir", "official")):
            continue
        picked.append(it)
    return picked[:3]


def rss_items(url: str) -> list[dict]:
    xml = fetch(url, timeout=20)
    root = ET.fromstring(xml)
    items = []
    for node in root.findall(".//item"):
        title = (node.findtext("title") or "").strip()
        link = (node.findtext("link") or "").strip()
        desc = re.sub(r"<[^>]+>", " ", node.findtext("description") or "")
        items.append({"title": title, "link": link, "text": f"{title} {desc}".lower()})
    return items


def main() -> int:
    dry = "dry" in [a.lower() for a in sys.argv[1:]]
    cfg = load_json(CFG_PATH, {})
    now = datetime.now(KST)
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    state = load_json(
        STATE_PATH,
        {"updated_at": None, "fired": {}, "flow_days": [], "seen_links": [], "last_sent": None, "last_fail_day": None},
    )
    fired = dict(state.get("fired") or {})
    for key in ("close_break", "intraday_break", "flow_expand"):
        fired.setdefault(key, False)

    if now.year != int(cfg.get("year_guard") or 2026):
        print("kosbrk year-guard silent")
        return 0
    expire = datetime.strptime(cfg.get("expire_date") or "2026-12-31", "%Y-%m-%d").date()
    if now.date() > expire:
        print("kosbrk expired silent")
        return 0
    if now.weekday() >= 5:
        print("kosbrk weekend silent")
        return 0

    try:
        basic = json.loads(fetch(cfg["sources"]["basic"]))
        price_rows = json.loads(fetch(cfg["sources"]["price"]))
    except Exception as exc:
        fail_once(state, now, cfg, "[KOSBRK] 시세 조회 실패 | 판정 보류", f"코스피 시세 조회 실패: {exc}", dry)
        return 0

    bar = latest_closed(price_rows, basic)
    if not bar:
        print("kosbrk no closed bar; silent")
        state["updated_at"] = stamp
        save(state)
        return 0

    day = str(bar.get("localTradedAt") or "")[:10]
    close = num(bar.get("closePrice"))
    low = num(bar.get("lowPrice"))
    levels = cfg.get("levels") or {}
    close_line = float(levels.get("close_break") or 5663.24)
    low_line = float(levels.get("intraday_break") or 5262.77)

    if close > close_line:
        fired["close_break"] = False
    if low > low_line:
        fired["intraday_break"] = False

    flow_note = "수급 미확인"
    flow_hit = False
    flow_detail = ""
    try:
        flow = json.loads(fetch(cfg["sources"]["flow"]))
        flow_day = f"{flow.get('bizdate','')[:4]}-{flow.get('bizdate','')[4:6]}-{flow.get('bizdate','')[6:8]}"
        foreign = num(flow.get("foreignValue"))
        days = [d for d in (state.get("flow_days") or []) if d.get("day") != flow_day]
        days.append({"day": flow_day, "foreign": foreign})
        days = sorted(days, key=lambda d: d["day"])[-8:]
        state["flow_days"] = days
        if flow_day == day and len(days) >= 2:
            prev = days[-2]["foreign"]
            rule = cfg.get("flow") or {}
            expanding = foreign <= float(rule.get("expand_day_max") or -5000) and foreign < prev
            streak = days[-3:]
            streak_hit = (
                len(streak) == 3
                and all(d["foreign"] < 0 for d in streak)
                and sum(d["foreign"] for d in streak) <= float(rule.get("streak_sum_max") or -10000)
            )
            flow_hit = expanding or streak_hit
            flow_detail = f"{flow_day} 외국인 {foreign:+,.0f}억, 전일 {prev:+,.0f}억"
            if foreign >= 0:
                fired["flow_expand"] = False
        elif flow_day != day:
            flow_note = f"수급 기준일 {flow_day} ≠ 종가일 {day}"
        else:
            flow_note = "완료 수급 2일 미만. 재확대 판정 보류"
        flow_note = flow_detail or flow_note
    except Exception as exc:
        fail_once(state, now, cfg, "[KOSBRK] 수급 조회 실패 | 판정 보류", f"외국인 수급 조회 실패: {exc}", dry)
        return 0

    guide = []
    guide_note = "가이던스 하향 키워드 없음"
    try:
        items = []
        for url in (cfg.get("sources") or {}).get("rss") or []:
            items.extend(rss_items(url))
        seen = set(state.get("seen_links") or [])
        guide = guide_hits(items, seen)
        if guide:
            guide_note = " / ".join(it["title"][:80] for it in guide)
    except Exception as exc:
        guide_note = f"가이던스 RSS 조회 실패: {exc}"
        print(guide_note, file=sys.stderr)

    new_hits = []
    if close <= close_line and not fired.get("close_break"):
        fired["close_break"] = True
        new_hits.append("close")
    if low <= low_line and not fired.get("intraday_break"):
        fired["intraday_break"] = True
        new_hits.append("low")
    if flow_hit and not fired.get("flow_expand"):
        fired["flow_expand"] = True
        new_hits.append("flow")
    if guide:
        new_hits.append("guide")
        state["seen_links"] = list(dict.fromkeys(list(state.get("seen_links") or []) + [it["link"] for it in guide]))[-40:]

    state["fired"] = fired
    state["updated_at"] = stamp
    state["last_bar"] = {"day": day, "close": close, "low": low}
    print(stamp, "close", close, "low", low, "flow", flow_note, "new", new_hits)

    if not new_hits:
        save(state)
        print("kosbrk silent; no new change-condition")
        return 0

    labels = {
        "close": f"종가 {close:,.2f} <= {close_line:,.2f}",
        "low": f"장중저가 {low:,.2f} <= {low_line:,.2f}",
        "flow": "외국인 순매도 재확대",
        "guide": "반도체 가이던스 하향 키워드",
    }
    title = "[KOSBRK] " + " / ".join(labels[k] for k in new_hits) + " | 대기 논리 재검토"
    body = "\n".join(
        [
            f"{stamp} | KOSBRK | 사용자 Cwally",
            "이게 뭐 알림: 문남중 코스피 대기 주장의 변경 조건입니다. 매도 신호가 아닙니다.",
            f"지금 상태: {day} 종가 {close:,.2f} / 저가 {low:,.2f}. 수급 {flow_note}. 가이던스 {guide_note}.",
            "지금 할 일: 연말 전고점 회복 대기를 다시 보세요. 새 매수는 보류하세요.",
            "나중에: 종가가 5,663 위로 회복하거나 외국인이 다시 순매수면 같은 조건은 한 번 리셋됩니다. 2026-12-31 이후 침묵.",
            "익절/줄일 때: 이 알림은 줄이라는 주문이 아닙니다. 기존 [개별 종목] 규칙을 따르세요.",
            "하지 말 것: 조건 충족을 추격 매도로 해석. 신용/미수. 소식통 기사를 공식 가이던스로 취급.",
        ]
    )
    print(body)
    if not dry:
        publish(resolve_topic("port", cfg.get("ntfy_topic")), title, body, 4, ["warning"], resolve_server(cfg.get("ntfy_server")))
        state["last_sent"] = {"at": stamp, "hits": new_hits}
    save(state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
