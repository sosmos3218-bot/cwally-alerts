#!/usr/bin/env python3
"""MERAS 메르 A/S 종합 알림: 미국 10년물 + 삼성전자/SK하이닉스 + 한국카본.

morning 슬롯(평일 09:00 KST).
2026-10-30 (approved in chat, owner note pt24u): 거의 매일 오던 발송을 줄임. 비교 기준을 '직전 실행'에서 '직전 발송 때 값(anchor)'으로
바꾸고 문턱을 올림: 종목 종가가 anchor 대비 ±10% 이상(config thresholds.stock_pct) 또는 미국10Y ±0.20%p 이상(thresholds.us10y_pp).
주간 첫 영업일 발송은 월 첫 실행 1번(heartbeat)으로 바꿈. 발송하면 anchor 를 그때 값으로 다시 잡음. 조회 실패는 cwally-ops 에 하루 1번만.
30일 백테스트(10/01~10/30): 기존 규칙 11건 -> 새 규칙 4건(+월 첫 실행 1건).
modes: auto(기본) | dry(발송 없음, state 안 씀) | install(설치 확인 1회) | selftest
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
    import krx_close
    iso = f"{today[:4]}-{today[4:6]}-{today[6:]}" if len(str(today)) == 8 else str(today)[:10]
    try:
        bars = [b for b in krx_close.regular_bars(code, 8) if b.day < iso]
    except Exception as exc:
        return None, None, str(exc)
    if not bars:
        return None, None, "완료 종가 없음"
    b = bars[-1]
    return b.day.replace("-", ""), float(b.close), None

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
AVOID = "단기 변동을 매매 신호로 해석, 신용/미수, 한 종목 몰빵."

LABELS = {"005930": "삼성", "000660": "하이닉스", "017960": "카본"}


def anchors(state: dict) -> tuple[float | None, dict]:
    """Values at the last send. Older state (before 2026-10-30) only has last_* = values at the last run, which was a send."""
    return state.get("anchor_us10y", state.get("last_us10y")), dict(state.get("anchor_closes") or state.get("last_closes") or {})


def evaluate(state: dict, cur: dict, days: dict, now: datetime, cfg: dict) -> tuple[list[str], str]:
    th = cfg.get("thresholds") or {}
    stock_pct, us_pp = float(th.get("stock_pct", 10.0)), float(th.get("us10y_pp", 0.20))
    a_us, a_cl = anchors(state)
    reasons, parts = [], []
    us = cur.get("us10y")
    if us is not None:
        d = (us - a_us) if a_us is not None else 0.0
        parts.append(f"미국10Y {us:.2f}% (직전 발송 {a_us:.2f}% 대비 {d:+.2f}%p)" if a_us is not None else f"미국10Y {us:.2f}%")
        if a_us is None or abs(d) >= us_pp - 1e-9:
            reasons.append(f"미국10Y {d:+.2f}%p(기준 ±{us_pp:.2f}%p)" if a_us is not None else "기준값 없음")
    else:
        parts.append("미국10Y 조회실패")
    for code, label in LABELS.items():
        c = cur.get(code)
        if c is None:
            parts.append(f"{label} 조회실패")
            continue
        prev = a_cl.get(code)
        pct = (c / prev - 1) * 100 if prev else 0.0
        parts.append(f"{label} {c:,.0f}원({days.get(code) or '-'}) 직전 발송 대비 {pct:+.1f}%")
        if prev is None or abs(pct) >= stock_pct - 1e-9:
            reasons.append(f"{label} {pct:+.1f}%(기준 ±{stock_pct:.0f}%)" if prev else f"{label} 기준값 없음")
    month = now.strftime("%Y-%m")
    if state.get("last_month", month) != month:
        reasons.append("월 첫 점검")
    return reasons, " | ".join(parts)


def rearm(state: dict, cur: dict, now: datetime) -> None:
    """After a send: anchors = current values (failed items keep their old anchor)."""
    a_us, a_cl = anchors(state)
    state["anchor_us10y"] = cur["us10y"] if cur.get("us10y") is not None else a_us
    state["anchor_closes"] = {k: (cur[k] if cur.get(k) is not None else a_cl.get(k)) for k in LABELS}
    state["anchor_at"] = now.isoformat()
    state["last_month"] = now.strftime("%Y-%m")
    state["last_us10y"] = state["anchor_us10y"]
    state["last_closes"] = state["anchor_closes"]


def selftest() -> int:
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    fails = 0

    def check(label, got, want):
        nonlocal fails
        ok = got == want
        fails += not ok
        print(("ok  " if ok else "FAIL"), label, "" if ok else f"got {got!r} want {want!r}")
    at = lambda s: datetime.strptime(s, "%Y-%m-%d").replace(hour=9, tzinfo=KST)  # noqa: E731
    st = {"last_us10y": 4.99, "last_closes": {"005930": 230500.0, "000660": 1516000.0, "017960": 15440.0}, "last_week": "2026-W43"}
    base = {"us10y": 4.99, "005930": 230500.0, "000660": 1516000.0, "017960": 15440.0}
    D = {k: "20261030" for k in LABELS}
    check("migrated anchors, no move", evaluate(st, base, D, at("2026-10-30"), cfg)[0], [])
    check("+9.9% no send", evaluate(st, dict(base, **{"005930": 230500 * 1.099}), D, at("2026-10-30"), cfg)[0], [])
    check("+10% send", len(evaluate(st, dict(base, **{"005930": 230500 * 1.10}), D, at("2026-10-30"), cfg)[0]), 1)
    check("-10% send", len(evaluate(st, dict(base, **{"017960": 15440 * 0.90}), D, at("2026-10-30"), cfg)[0]), 1)
    check("10Y +0.19 no", evaluate(st, dict(base, us10y=5.18), D, at("2026-10-30"), cfg)[0], [])
    check("10Y +0.20 send", len(evaluate(st, dict(base, us10y=5.19), D, at("2026-10-30"), cfg)[0]), 1)
    # cumulative drift: three daily +4% moves never fire under the old daily rule but fire vs anchor on day 3
    s2 = json.loads(json.dumps(st))
    c = dict(base)
    fired = []
    for i, day in enumerate(("2026-11-02", "2026-11-03", "2026-11-04")):
        c = dict(c, **{"000660": c["000660"] * 1.04})
        r, _ = evaluate(dict(s2, last_month="2026-11"), c, D, at(day), cfg)
        fired.append(bool(r))
    check("cumulative drift fires on day 3 (+12.5%)", fired, [False, False, True])
    # month heartbeat once
    r, _ = evaluate(dict(st, last_month="2026-10"), base, D, at("2026-11-02"), cfg)
    check("month first run heartbeat", r, ["월 첫 점검"])
    s3 = json.loads(json.dumps(st))
    rearm(s3, dict(base, **{"005930": 260000.0}), at("2026-11-02"))
    check("rearm anchor + month", (s3["anchor_closes"]["005930"], s3["last_month"]), (260000.0, "2026-11"))
    check("after rearm quiet", evaluate(s3, dict(base, **{"005930": 260000.0}), D, at("2026-11-03"), cfg)[0], [])
    rearm(s3, dict(base, us10y=None), at("2026-11-03"))
    check("failed item keeps anchor", s3["anchor_us10y"], 4.99)
    b = body("2026-10-30 09:00 KST", "x", "y", LATER, TRIM, AVOID)
    check("no dot/dash", any(ch in b for ch in "\u00b7\u2014"), False)
    print("selftest", "PASS" if not fails else f"FAIL {fails}")
    return 1 if fails else 0


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    if mode == "selftest":
        return selftest()
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    today = now.strftime("%Y%m%d")
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
        publish(topic, "[MERAS] 005930/000660/017960 설치 확인 | 매매 신호 아님",
                body(stamp, status, "설치 확인만. 이 알림으로 사거나 팔지 마세요.",
                     "직전 발송 대비 종목 ±10% 또는 미국10Y ±0.20%p, 그리고 월 첫 실행에 발송.", TRIM, "설치 확인을 매매 신호로 읽기."),
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

    cur = {"us10y": us, "005930": sam_c, "000660": hy_c, "017960": ca_c}
    days = {"005930": sam_d, "000660": hy_d, "017960": ca_d}
    failed = [k for k, v in cur.items() if v is None]
    if failed:
        print(f"fetch failed: {failed} us_err={us_err} {sam_e} {hy_e} {ca_e}")
        if state.get("fetch_fail_date") != today:
            msg = f"MERAS 조회 실패: {', '.join(failed)}. 실패 항목은 이번 판정에서 빼고 다음 09:00 에 다시 봅니다."
            if dry:
                print("DRY RUN would send (ops): [MERAS] 005930/000660/017960 조회 실패 | 매매 신호 아님")
            else:
                publish(resolve_topic("ops"), "[MERAS] 005930/000660/017960 조회 실패 | 매매 신호 아님", msg, 3, ["warning"], server)
                state["fetch_fail_date"] = today
    reasons, status = evaluate(state, cur, days, now, cfg)
    if not reasons and mode != "force":
        print(f"no notable change vs anchor; skip | {status}")
        if not dry:
            state["updated_at"] = now.isoformat()
            STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return 0
    rearm(state, cur, now)
    status = f"{status} | 발송 이유: {', '.join(reasons) or '수동 force'}"

    title = "[MERAS] 005930/000660/017960 메르 A/S 종합 점검 | 변동 확인"
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
