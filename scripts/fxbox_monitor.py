#!/usr/bin/env python3
"""Post-close check of the 1,338-1,348 USD/KRW box. One next-session center."""
from __future__ import annotations

import json
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from kr_calendar import is_trading_day, session_day  # noqa: E402
from ntfy_util import publish, resolve_server, resolve_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
CFG_PATH = ROOT / "configs" / "fxbox.json"
STATE_PATH = ROOT / "state" / "fxbox.json"
UA = {"User-Agent": "Mozilla/5.0 cwally-alerts"}


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


def save(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def fetch(url: str, timeout: int = 20) -> str:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def num(text) -> float | None:
    if text is None:
        return None
    cleaned = str(text).replace(",", "").replace("+", "").strip()
    if not cleaned or cleaned == "-":
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def next_trading_day(d):
    d += timedelta(days=1)
    while not is_trading_day(d):
        d += timedelta(days=1)
    return d


def yahoo_last(url: str) -> float | None:
    data = json.loads(fetch(url))
    meta = ((data.get("chart") or {}).get("result") or [{}])[0].get("meta") or {}
    return num(meta.get("regularMarketPrice"))


def nego_line(url: str, day: str) -> str:
    xml = fetch(url, timeout=20)
    root = ET.fromstring(xml)
    titles = []
    for node in root.findall(".//item"):
        title = (node.findtext("title") or "").strip()
        if title:
            titles.append(title)
    if not titles:
        return "네고 보도 없음"
    return " / ".join(titles[:2])[:180]


def center_for(close: float, dxy: float | None, ust: float | None, box: dict, change: dict) -> tuple[int, str]:
    low = float(box.get("low") or 1338)
    high = float(box.get("high") or 1348)
    base = int(box.get("center") or 1342)
    if low <= close <= high:
        if abs(close - base) <= 4:
            return base, "박스 안. 중심 유지"
        nxt = int(round(min(high, max(low, close))))
        return nxt, "박스 안이지만 종가 쪽으로 중심 이동"
    if close < low:
        if dxy is not None and ust is not None and dxy < float(change.get("dxy_break") or 101) and ust < float(change.get("ust_break") or 5.15):
            return 1336, "하단 이탈, 달러·금리 동반 하락. 종가 이탈 시나리오"
        return 1340, "하단 장중 이탈. 달러·금리 조건 미충족이라 다음 중심은 1,340"
    if dxy is not None and ust is not None and (dxy >= float(change.get("dxy_up") or 102.5) or ust >= float(change.get("ust_up") or 5.35)):
        return 1352, "상단 이탈, 달러 또는 금리 재상승. 다음 중심 1,352"
    return 1346, "상단 장중 이탈. 변경 조건 미충족이라 다음 중심은 1,346"


def fail_once(state, now, cfg, title, note, dry) -> None:
    day = session_day(now).isoformat()
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    body = "\n".join([
        f"{stamp} | FXBOX",
        "이게 뭐 알림: 1,338~1,348원 박스 사후검증 중 기준가 조회가 실패했습니다. 매매 신호가 아닙니다.",
        f"지금 상태: {note}",
        "지금 할 일: 실패를 박스 적중으로 보지 마세요. 서울 15:30 기준가를 직접 확인하세요.",
        "나중에: 다음 평일 15:40 슬롯에서 다시 봅니다.",
        "익절/줄일 때: 이 실패로 환전하지 마세요.",
        "하지 말 것: 조회 실패를 환율 방향으로 해석. 추격 환전.",
    ])
    print(body)
    if not dry and state.get("last_fail_day") != day:
        publish(resolve_topic("port", cfg.get("ntfy_topic")), title, body, 4, ["warning"], resolve_server(cfg.get("ntfy_server")))
        state["last_fail_day"] = day
    state["updated_at"] = stamp
    save(state)


def main() -> int:
    dry = "dry" in [a.lower() for a in sys.argv[1:]]
    cfg = load_json(CFG_PATH, {})
    now = datetime.now(KST)
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    state = load_json(STATE_PATH, {"updated_at": None, "last_sent_day": None, "last_fail_day": None, "last": None})
    if now.year != int(cfg.get("year_guard") or 2026):
        print("fxbox year-guard silent")
        return 0
    day = session_day(now)
    start = datetime.strptime(cfg.get("window_start") or "2026-10-08", "%Y-%m-%d").date()
    end = datetime.strptime((cfg.get("ends_kst") or "2026-10-15 15:40")[:10], "%Y-%m-%d").date()
    if day < start or day > end:
        print(f"fxbox outside window {day}")
        return 0
    if not is_trading_day(day):
        print(f"fxbox holiday silent {day}")
        return 0
    if state.get("last_sent_day") == day.isoformat():
        print("fxbox already sent")
        return 0

    sources = cfg.get("sources") or {}
    try:
        rows = json.loads(fetch(sources["usdkrw"]))
        bar = rows[0] if rows else {}
        close = num(bar.get("closePrice"))
        bar_day = str(bar.get("localTradedAt") or "")[:10]
    except Exception as exc:
        fail_once(state, now, cfg, "[FXBOX] 기준가 조회 실패 | 판정 보류", f"네이버 원/달러 조회 실패: {exc}", dry)
        return 0
    if close is None or bar_day != day.isoformat():
        fail_once(state, now, cfg, "[FXBOX] 기준가 조회 실패 | 판정 보류", f"마감 종가 없음. 받은 날 {bar_day or '-'} 종가 {close}", dry)
        return 0

    dxy = ust = foreign = None
    notes = []
    try:
        dxy = yahoo_last(sources["dxy"])
    except Exception as exc:
        notes.append(f"달러인덱스 미확인({exc.__class__.__name__})")
    try:
        ust = yahoo_last(sources["ust10"])
    except Exception as exc:
        notes.append(f"미 10년물 미확인({exc.__class__.__name__})")
    try:
        flow = json.loads(fetch(sources["kospi_flow"]))
        foreign = num(flow.get("foreignValue"))
        flow_day = str(flow.get("bizdate") or "")
        if flow_day != day.strftime("%Y%m%d"):
            notes.append(f"외국인 현물 기준일 {flow_day} ≠ {day.isoformat()}")
            foreign = None
    except Exception as exc:
        notes.append(f"외국인 현물 미확인({exc.__class__.__name__})")
    try:
        nego = nego_line(sources["nego_rss"], day.isoformat())
    except Exception as exc:
        nego = f"네고 보도 조회 실패({exc.__class__.__name__})"
    notes.append("달러선물 외국인 순매매는 안정 엔드포인트 없음, 미확인")

    box = cfg.get("box") or {}
    nxt, why = center_for(close, dxy, ust, box, cfg.get("change") or {})
    hit = float(box.get("low") or 1338) <= close <= float(box.get("high") or 1348)
    verdict = "적중" if hit else "이탈"
    nxt_day = next_trading_day(day)
    dxy_s = f"{dxy:.2f}" if dxy is not None else "미확인"
    ust_s = f"{ust:.2f}%" if ust is not None else "미확인"
    fr_s = f"{foreign:+,.0f}억원" if foreign is not None else "미확인"
    extra = (" " + " ".join(notes)) if notes else ""
    title = f"[FXBOX] 박스 {verdict} | 다음 중심 {nxt:,}원"
    body = "\n".join([
        f"{stamp} | FXBOX",
        "이게 뭐 알림: 1,338~1,348원 박스 전망의 마감 사후검증입니다. 매매 신호가 아닙니다.",
        f"지금 상태: {day.isoformat()} 네이버 원/달러 종가 {close:,.1f}원, 박스 {verdict}. 달러인덱스 {dxy_s}, 미 10년물 {ust_s}, 외국인 코스피 {fr_s}. 네고: {nego}.{extra}",
        f"지금 할 일: {nxt_day.isoformat()} 중심 환율은 {nxt:,}원 하나만 보세요. 이유: {why}.",
        "나중에: 다음 평일 15:40에 같은 박스를 다시 검증합니다. 10월 15일이 마지막입니다.",
        "익절/줄일 때: 이 알림은 환전 주문이 아닙니다. 기존 환전 규칙을 따르세요.",
        "하지 말 것: 네이버 종가를 서울 15:30 고시와 같다고 단정. 네고 기사 제목을 체결량으로 해석. 추격 환전.",
    ])
    print(body)
    if not dry:
        publish(resolve_topic("port", cfg.get("ntfy_topic")), title, body, 3, ["chart_with_upwards_trend"], resolve_server(cfg.get("ntfy_server")))
        state["last_sent_day"] = day.isoformat()
    state["updated_at"] = stamp
    state["last"] = {"day": day.isoformat(), "close": close, "hit": hit, "next_center": nxt, "next_day": nxt_day.isoformat()}
    save(state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
