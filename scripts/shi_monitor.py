#!/usr/bin/env python3
"""[SHI] 삼성중공업(010140) 종가 선 알림 (사용자 승인 2026-10-30).

low  : 확정 종가 < 14,500  -> p4 '[SHI] 14,500 이탈 | 비중 축소 검토'. 종가가 14,500 x 1.03 = 14,935 를 넘어야 재무장.
high : 확정 종가 > 18,000  -> p3 '[SHI] 18,000 돌파 | 부분 차익 검토'. 종가가 18,000 x 0.97 = 17,460 아래로 와야 재무장.
평일 15:20 KST afternoon 묶음. 장 마감 전이라 확정 종가(직전 거래일까지, 15:35 이후 수동 실행이면 당일 포함)만 날짜순 판정,
state last_date 로 같은 날짜를 두 번 보지 않음. 시세 읽기 실패: 판정 없이 cwally-ops 하루 1번.
modes: auto | dry(발송 없음, state 안 씀)
"""
from __future__ import annotations

import json
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic, route_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
CFG_PATH = ROOT / "configs" / "shi.json"
STATE_PATH = ROOT / "state" / "shi.json"
UA = {"User-Agent": "Mozilla/5.0 cwally-alerts"}


def get(url: str):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=25) as r:
        return json.loads(r.read().decode("utf-8", "ignore"))


def num(v) -> float | None:
    try:
        return float(str(v).replace(",", "").replace("+", "").replace("%", ""))
    except (TypeError, ValueError):
        return None


def price_rows(cfg: dict) -> list[dict]:
    out = []
    for r in get(cfg["sources"]["price"]):
        c = num(r.get("closePrice"))
        if c:
            out.append({"day": str(r.get("localTradedAt", ""))[:10], "close": c, "chg": num(r.get("compareToPreviousClosePrice")),
                        "pct": num(r.get("fluctuationsRatio"))})
    if not out:
        raise RuntimeError("시세 0건")
    return sorted(out, key=lambda x: x["day"])


def foreign_by_day(cfg: dict) -> dict:
    try:
        return {f"{r['bizdate'][:4]}-{r['bizdate'][4:6]}-{r['bizdate'][6:]}": num(r.get("foreignerPureBuyQuant")) for r in get(cfg["sources"]["trend"])}
    except Exception:  # noqa: BLE001
        return {}


def judge(cfg: dict, st: dict, rows: list[dict], now: datetime) -> list[tuple[str, dict]]:
    today = now.date().isoformat()
    after_close = (now.hour, now.minute) >= (15, 35)
    lo, hi = cfg["lines"]["low"]["level"], cfg["lines"]["high"]["level"]
    r = cfg["rearm_pct"] / 100
    arm = st.setdefault("armed", {"low": True, "high": True})
    fired = []
    for row in rows:
        d = row["day"]
        if d <= st.get("last_date", "") or d > today or (d == today and not after_close):
            continue
        c = row["close"]
        if c < lo and arm["low"]:
            arm["low"] = False
            fired.append(("low", row))
        elif c > lo * (1 + r) and not arm["low"]:
            arm["low"] = True
        if c > hi and arm["high"]:
            arm["high"] = False
            fired.append(("high", row))
        elif c < hi * (1 - r) and not arm["high"]:
            arm["high"] = True
        st["last_date"], st["last_close"] = d, c
    return fired


def message(cfg: dict, side: str, row: dict, frn: float | None, stamp: str) -> tuple[str, str, int]:
    ln = cfg["lines"][side]
    lo, hi = cfg["lines"]["low"]["level"], cfg["lines"]["high"]["level"]
    c = row["close"]
    chg = f"{row['chg']:+,.0f}원 ({row['pct']:+.2f}%)" if row.get("chg") is not None and row.get("pct") is not None else "전일 대비 미표시"
    ftxt = f"외국인 순매수 {frn:+,.0f}주" if frn is not None else "외국인 순매수 미표시"
    if side == "low":
        other = f"18,000원까지 {(hi / c - 1) * 100:+.1f}%"
        status = f"{row['day']} 종가 {c:,.0f}원, 전일 대비 {chg}, 14,500원 아래 마감. {ftxt}. {other}."
        act, later = "비중 축소를 검토하세요. 반등 기대만으로 버티지 말고 남길 수량을 정하기.", f"종가가 {lo * 1.03:,.0f}원(14,500 x 1.03)을 넘은 뒤 다시 14,500 아래로 오면 다시 알림."
        trim, avoid = "손절선 이탈이 이어지면 남은 비중도 다시 점검.", "물타기, 장중 반등만 보고 손절 철회."
    else:
        other = f"14,500원까지 {(lo / c - 1) * 100:+.1f}%"
        status = f"{row['day']} 종가 {c:,.0f}원, 전일 대비 {chg}, 18,000원 위 마감. {ftxt}. {other}."
        act, later = "부분 차익을 검토하세요. 단기 목표 18,000~20,000원 구간.", f"종가가 {hi * 0.97:,.0f}원(18,000 x 0.97) 아래로 내려간 뒤 다시 18,000 위로 오면 다시 알림."
        trim, avoid = "20,000원 근처에서 추가 차익 검토.", "돌파 직후 추격 매수로 비중 늘리기."
    msg = "\n".join([f"{stamp} | SHI | 삼성중공업 010140 종가 선",
                     "이게 뭐 알림: 삼성중공업 손절선 14,500원과 단기 목표 18,000원 종가 알림. 매매 지시 아님.",
                     f"지금 결과/상태: {status}", f"지금 할 일: {act}", f"나중에: {later}", f"익절/줄일 때: {trim}", f"하지 말 것: {avoid}"])
    assert "\u00b7" not in ln["title"] + msg and "\u2014" not in ln["title"] + msg
    assert "확인 불가" not in ln["title"] and "조회 실패" not in ln["title"]
    return ln["title"], msg, ln["priority"]


def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def run(mode: str, now: datetime, cfg: dict, pub=publish, rows_fn=None, frn_fn=None) -> int:
    dry = mode == "dry"
    stamp, today = now.strftime("%Y-%m-%d %H:%M KST"), now.date().isoformat()
    st = load_state()
    topic, server = resolve_topic("port", cfg.get("ntfy_topic")), resolve_server(cfg.get("ntfy_server"))
    out = []
    try:
        rows = (rows_fn or price_rows)(cfg)
    except Exception as exc:  # noqa: BLE001
        rows = None
        print(f"price fail: {exc}")
        if st.get("fail_date") != today:
            out.append(("[SHI] 시세 조회 실패 | 종가 판정 보류", "\n".join([
                f"{stamp} | SHI | 데이터 읽기 실패", "이게 뭐 알림: 삼성중공업 종가 선 알림의 시세 읽기 실패 안내. 매매 신호 아님.",
                f"지금 결과/상태: 네이버 시세 {type(exc).__name__} {str(exc)[:120]}. 오늘은 판정하지 않았습니다.",
                "지금 할 일: 증권 앱에서 010140 종가를 직접 보세요.", "나중에: 다음 평일 15:20 KST 에 다시 읽고 밀린 날짜도 순서대로 판정합니다.",
                "익절/줄일 때: 이 안내로 비중을 바꾸지 마세요.", "하지 말 것: 읽기 실패를 선 미이탈로 읽기."]), 3))
            st["fail_date"] = today
    if rows is not None:
        fired = judge(cfg, st, rows, now)
        print(f"state: last_date={st.get('last_date')} close={st.get('last_close')} armed={st.get('armed')}")
        if fired:
            frn = (frn_fn or foreign_by_day)(cfg)
            for side, row in fired:
                out.append(message(cfg, side, row, frn.get(row["day"]), stamp))
    for t, m, p in out:
        print(f"TITLE: {t}\nPRIORITY: {p} TOPIC: {route_topic(topic, t)}\n{m}\n")
    if not out:
        print("보낼 것 없음")
    if dry:
        print("DRY RUN: no publish, state not written")
        return 0
    for t, m, p in out:
        pub(topic, t, m, p, ["ship"], server, config_name="shi")
    STATE_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    if mode not in {"auto", "dry"}:
        print("usage: shi_monitor.py <auto|dry>")
        return 2
    return run(mode, datetime.now(KST), json.loads(CFG_PATH.read_text(encoding="utf-8")))


if __name__ == "__main__":
    sys.exit(main())
