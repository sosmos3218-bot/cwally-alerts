#!/usr/bin/env python3
"""MERAS 메르 A/S 종합 알림: 미국 10년물 + 삼성전자/SK하이닉스 + 한국카본.

morning 슬롯(평일 09:00 KST). 직전 대비 유의 변동 또는 주간 첫 영업일 발송.
modes: auto(기본) | dry(발송 없음, state 안 씀) | install(설치 확인 1회)
"""
from __future__ import annotations

import json
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic  # noqa: E402
import naver_api  # noqa: E402

KST = timezone(timedelta(hours=9))
NAME = "meras"
CFG_PATH = ROOT / "configs" / f"{NAME}.json"
STATE_PATH = ROOT / "state" / f"{NAME}.json"

UA = {"User-Agent": "Mozilla/5.0 (cwally-alerts)"}

def load_state() -> dict:
    base = {"updated_at": None, "last_us10y": None, "last_closes": {}, "last_week": None, "fetch_fail_date": None}
    if STATE_PATH.exists():
        try:
            base.update(json.loads(STATE_PATH.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass
    return base

def fetch_us10y() -> tuple[float | None, str | None]:
    try:
        url = "https://query1.finance.yahoo.com/v8/finance/chart/%5ETNX?interval=1d&range=5d"
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
        result = data["chart"]["result"][0]
        closes = result["indicators"]["quote"][0]["close"]
        ts = result["timestamp"]
        for i in range(len(closes)-1, -1, -1):
            if closes[i] is not None:
                return float(closes[i]), datetime.fromtimestamp(ts[i], KST).strftime("%Y-%m-%d")
        return None, "no close"
    except Exception as e:
        return None, str(e)

def last_close(code: str, today: str) -> tuple[str | None, float | None, str | None]:
    try:
        rows = naver_api.stock_trend(code, 5)
    except Exception as exc:
        return None, None, str(exc)
    done = [r for r in rows if r.get("day") and r["day"] < today and r.get("close")]
    if not done:
        return None, None, "완료 종가 없음"
    top = max(done, key=lambda r: r["day"])
    return top["day"], float(top["close"]), None

def body(stamp: str, status: str, action: str, later: str, trim: str, avoid: str) -> str:
    return "\n".join([
        f"{stamp} | MERAS | 메르 A/S 종합 점검",
        f"이게 뭐 알림: 메르 블로그 '불장에 취하지 마라 A/S' 핵심 3항목(미국 10년물 5.3% 재매수 근처, 삼성전자+SK하이닉스 AI 공급자, 한국카본 조선주) 주기 요약. 매수 신호 아님.",
        f"지금 결과/상태: {status}",
        f"지금 할 일: {action}",
        f"나중에: {later}",
        f"익절/줄일 때: {trim}",
        f"하지 말 것: {avoid}",
    ])

LATER = "국민연금 매도 지속 여부, AI 실적, 조선 수주 흐름 계속 관찰."
TRIM = "고점 대비 큰 폭 하락 시 비중 점검. 추격 매수 금지."
AVOID = "단기 변동을 매매 신호로 해석, 신용/미수, 몱벵."

def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    today = now.strftime("%Y%m%d")
    week = now.strftime("%Y-W%W")
    topic = resolve_topic("port", cfg.get("ntfy_topic"))
    server = resolve_server(cfg.get("ntfy_server"))

    if mode == "install":
        us, us_err = fetch_us10y()
        sam_d, sam_c, sam_e = last_close("005930", today)
        hy_d, hy_c, hy_e = last_close("000660", today)
        ca_d, ca_c, ca_e = last_close("017960", today)
        status = f"미국10Y {us:.2f}% " if us else f"미국10Y 조회실패({us_err}) "
        status += f"삼성 {sam_c:,.0f}원({sam_d}) " if sam_c else "삼성 실패 "
        status += f"하이닉스 {hy_c:,.0f}원 " if hy_c else "하이닉스 실패 "
        status += f"카본 {ca_c:,.0f}원" if ca_c else "카본 실패"
        publish(topic, "[MERAS] 설치 확인 | 매매 신호 아님",
                body(stamp, status, "설치 확인만. 이 알림으로 사거나 팔지 마세요.",
                     "유의 변동 시 또는 주간 첫 영업일 발송.", TRIM, "설치 확인을 매매 신호로 읽기."),
                3, ["information_source"], server)
        print("install confirmation sent")
        return 0

    end = cfg.get("ends_kst")
    if end and now > datetime.strptime(end, "%Y-%m-%d %H:%M").replace(tzinfo=KST) + timedelta(minutes=59):
        print(f"ended at {end}; silent")
        return 0

    dry = mode == "dry"
    state = load_state()
    us, us_err = fetch_us10y()
    sam_d, sam_c, sam_e = last_close("005930", today)
    hy_d, hy_c, hy_e = last_close("000660", today)
    ca_d, ca_c, ca_e = last_close("017960", today)
    print(f"us={us} sam={sam_c} hy={hy_c} ca={ca_c}")

    notable = False
    status_parts = []
    if us is not None:
        prev = state.get("last_us10y")
        delta = (us - prev) if prev else 0
        status_parts.append(f"미국10Y {us:.2f}% (직전 {prev or '-'} 대비 {delta:+.2f})")
        if prev is None or abs(delta) >= 0.10:
            notable = True
        state["last_us10y"] = us
    else:
        status_parts.append(f"미국10Y 조회실패({us_err})")
        notable = True

    for code, label, close, day in [("005930", "삼성", sam_c, sam_d), ("000660", "하이닉스", hy_c, hy_d), ("017960", "카본", ca_c, ca_d)]:
        if close is not None:
            prev = (state.get("last_closes") or {}).get(code)
            pct = ((close / prev - 1) * 100) if prev else 0
            status_parts.append(f"{label} {close:,.0f}원({day}) {pct:+.1f}%")
            if prev is None or abs(pct) >= 5.0:
                notable = True
            state.setdefault("last_closes", {})[code] = close
        else:
            status_parts.append(f"{label} 조회실패")
            notable = True

    status = " | ".join(status_parts)
    weekly_first = state.get("last_week") != week
    if weekly_first:
        notable = True
        state["last_week"] = week

    if not notable and mode != "force":
        print("no notable change; skip")
        if not dry:
            state["updated_at"] = now.isoformat()
            STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return 0

    title = "[MERAS] 메르 A/S 종합 점검 | 변동 확인"
    action = "현재 수준만 확인. 추가 매수나 전량 매도는 별도 판단."
    msg = body(stamp, status, action, LATER, TRIM, AVOID)
    print(title)
    print(msg)
    if not dry:
        publish(topic, title, msg, 3, ["chart_with_upwards_trend"], server)
        state["updated_at"] = now.isoformat()
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    else:
        print("DRY RUN: no publish, state not written")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
