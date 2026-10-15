#!/usr/bin/env python3
"""XLP 지정가 분할 조건 알림 (change-only).

morning 슬롯(평일 09:00 KST). 미국 직전 완료 일봉 종가만 본다(진행 중인 봉은 제외).
- 1차: XLP 완료 종가 <= 78달러 AND 급등일(직전 거래일 +2% 이상)이 아님
- 2차: XLP 완료 종가 <= 75달러 AND SPY 완료 종가가 최근 52주 종가 고점 대비 10% 이상 하락 AND 급등일 아님
- 재무장: 1차는 종가 > 80달러, 2차는 종가 > 77.5달러가 되면 다시 켜짐
modes: auto(기본) | dry(발송 없음, state 안 씀) | install(설치 확인 1회, state 안 씀)
"""
from __future__ import annotations

import json
import sys
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic  # noqa: E402

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore[assignment]

KST = timezone(timedelta(hours=9))
NAME = "xlp"
CFG_PATH = ROOT / "configs" / f"{NAME}.json"
STATE_PATH = ROOT / "state" / f"{NAME}.json"
UA = {"User-Agent": "Mozilla/5.0 (cwally-alerts; +mailto:sosmos3218@gmail.com)"}


def ny_now() -> datetime:
    if ZoneInfo is not None:
        try:
            return datetime.now(ZoneInfo("America/New_York"))
        except Exception:  # noqa: BLE001
            pass
    off = -5 if datetime.now(timezone.utc).month in (11, 12, 1, 2, 3) else -4
    return datetime.now(timezone(timedelta(hours=off)))


def load_state() -> dict:
    base = {
        "updated_at": None,
        "buy1_armed": True,
        "buy2_armed": True,
        "last_bar_date": None,
        "last_close": None,
        "last_spy_drawdown_pct": None,
        "fetch_fail_date": None,
    }
    if STATE_PATH.exists():
        try:
            base.update(json.loads(STATE_PATH.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass
    return base


def fetch_bars(symbol: str, now_ny: datetime) -> list[tuple[date, float]]:
    """완료 일봉 (미국 동부 날짜, 종가) 오름차순. 진행 중인 오늘 봉은 뺀다."""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=1y&interval=1d"
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=20) as resp:
        res = json.loads(resp.read().decode())["chart"]["result"][0]
    tz = now_ny.tzinfo
    out: dict[date, float] = {}
    for t, c in zip(res.get("timestamp") or [], res["indicators"]["quote"][0].get("close") or []):
        if c is None:
            continue
        d = datetime.fromtimestamp(t, tz).date()
        out[d] = float(c)
    today = now_ny.date()
    after_close = now_ny.hour * 60 + now_ny.minute >= 16 * 60 + 10
    rows = sorted((d, c) for d, c in out.items() if d < today or (d == today and after_close))
    return rows


def body(stamp: str, status: str, action: str, later: str, trim: str, avoid: str) -> str:
    return "\n".join(
        [
            f"{stamp} | XLP | Consumer Staples Select Sector SPDR",
            "이게 뭐 알림: XLP 지정가 분할 조건 알림. 1차는 완료 종가 78달러 이하, 2차는 75달러 이하이면서 SPY가 52주 종가 고점 대비 10% 이상 하락. 하루 +2% 이상 급등한 날은 보류. 자동 주문 아님.",
            f"지금 결과/상태: {status}",
            f"지금 할 일: {action}",
            f"나중에: {later}",
            f"익절/줄일 때: {trim}",
            f"하지 말 것: {avoid}",
        ]
    )


TRIM = "XLP 합계가 금융자산 5%를 넘으면 더 사지 않음. 종가가 86달러(10/14 종가) 위로 돌아오고 향후 1년 PER이 20배 근처면 신규 추가는 멈추고 보유분만 유지."
AVOID = "시장가 주문, 급등 직후 추격 매수, 1차와 2차를 한 번에 다 사기, 원/달러 환율 확인 없이 주문, 한국 ISA에서 XLP를 찾기(불가)."
ORDER_TIP = "미국 정규장에 지정가로 주문. 개장 직후 30분은 피하고, 주문 직전에 직전 종가와 원/달러를 한 번 더 확인."


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    now_ny = ny_now()
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    today_kst = now.strftime("%Y%m%d")
    topic = resolve_topic("port", cfg.get("ntfy_topic"))
    server = resolve_server(cfg.get("ntfy_server"))
    b1, b2 = cfg["buy1"], cfg["buy2"]
    surge_pct = float(cfg["surge_day_pct"])

    end = cfg.get("ends_kst")
    if mode != "install" and end and now > datetime.strptime(end, "%Y-%m-%d %H:%M").replace(tzinfo=KST):
        print(f"ended at {end}; silent")
        return 0

    errs: list[str] = []
    xlp: list[tuple[date, float]] = []
    spy: list[tuple[date, float]] = []
    for sym, store in (("XLP", "xlp"), ("SPY", "spy")):
        try:
            rows = fetch_bars(sym, now_ny)
            if len(rows) < 3:
                raise RuntimeError(f"{sym} 완료 일봉 {len(rows)}개")
            if (now_ny.date() - rows[-1][0]).days > int(cfg["stale_days"]):
                raise RuntimeError(f"{sym} 마지막 봉 {rows[-1][0]} 너무 오래됨")
            if store == "xlp":
                xlp = rows
            else:
                spy = rows
        except Exception as exc:  # noqa: BLE001
            errs.append(f"{sym}: {exc}")

    last_d = xlp[-1][0] if xlp else None
    close = xlp[-1][1] if xlp else None
    prev = xlp[-2][1] if len(xlp) >= 2 else None
    day_pct = (close / prev - 1) * 100 if close and prev else None
    surge = bool(day_pct is not None and day_pct >= surge_pct)

    spy_close = spy[-1][1] if spy else None
    spy_dd = None
    spy_hi = None
    if len(spy) >= 150:
        spy_hi = max(c for _, c in spy)
        spy_dd = (1 - spy_close / spy_hi) * 100
    elif spy:
        errs.append(f"SPY: 52주 고점 계산용 일봉 {len(spy)}개 부족")

    hi52 = max(c for _, c in xlp) if len(xlp) >= 150 else None
    lo52 = min(c for _, c in xlp) if len(xlp) >= 150 else None

    is_b1 = bool(close is not None and close <= b1["close_max"] and not surge)
    spy_gate = bool(spy_dd is not None and spy_dd >= b2["spy_drawdown_min_pct"])
    is_b2 = bool(close is not None and close <= b2["close_max"] and spy_gate and not surge)

    if close is not None:
        x_txt = f"XLP 종가 ${close:,.2f}({last_d:%m/%d} 미국)"
        if day_pct is not None:
            x_txt += f", 전일 대비 {day_pct:+.2f}%"
        if hi52 and lo52:
            x_txt += f", 종가 기준 52주 ${lo52:,.2f}~${hi52:,.2f}"
    else:
        x_txt = "XLP 조회 실패"
    s_txt = (f"SPY ${spy_close:,.2f}, 52주 종가 고점 ${spy_hi:,.2f} 대비 -{spy_dd:.1f}%" if spy_dd is not None
             else "SPY 낙폭 계산 불가")
    status = f"{x_txt}. {s_txt}."
    if surge:
        status += f" 직전 거래일 +{surge_pct:.0f}% 이상 급등일이라 조건 판정 보류."
    if errs and close is not None:
        status += " 일부 조회 실패: " + "; ".join(errs) + "."

    if mode == "install":
        flags = [
            "1차 조건 충족" if is_b1 else "1차 조건 미충족",
            "2차 조건 충족" if is_b2 else "2차 조건 미충족",
            "SPY 10% 낙폭 충족" if spy_gate else "SPY 10% 낙폭 미충족",
        ]
        publish(
            topic,
            "[XLP] 설치 확인 | 매매 신호 아님",
            body(stamp, f"평일 09:00 KST morning 슬롯에 붙였습니다. {status} 지금 판정: {', '.join(flags)}.",
                 "설치 확인만 하세요. 이 알림으로 사지 마세요.",
                 "1차(78달러 이하), 2차(75달러 이하 + SPY 10% 낙폭) 조건이 새로 켜질 때만 옵니다. 조회 실패는 cwally-ops로 갑니다.",
                 TRIM, "설치 확인을 매수 신호로 읽기."),
            3, ["information_source"], server,
        )
        print("install confirmation sent")
        return 0

    dry = mode == "dry"
    state = load_state()
    first = state.get("updated_at") is None
    alerts: list[tuple[str, str, int]] = []
    print(f"close={close} day_pct={day_pct} surge={surge} spy_close={spy_close} spy_hi={spy_hi} spy_dd={spy_dd} errs={errs}")

    if close is None:
        if state.get("fetch_fail_date") != today_kst:
            alerts.append((
                "[XLP] 조회 실패 | 조건 미확인",
                body(stamp, status + " 조건을 확인하지 못했습니다.", "증권사 앱에서 XLP, SPY 종가를 직접 보세요.",
                     "다음 평일 09:00에 다시 봅니다.", "이번 알림으로 비중을 바꾸지 마세요.", "조회 실패를 조건 충족이나 정상으로 읽기."),
                3,
            ))
            state["fetch_fail_date"] = today_kst
    else:
        fire1 = is_b1 and state.get("buy1_armed", True)
        fire2 = is_b2 and state.get("buy2_armed", True)
        spy_note = (f"SPY 낙폭 -{spy_dd:.1f}%(2차 조건은 -{b2['spy_drawdown_min_pct']:.0f}% 이상)" if spy_dd is not None
                    else "SPY 낙폭 확인 불가")
        if fire2:
            both = fire1
            title = ("[XLP] 1, 2차 조건 동시 충족 | 지정가 검토" if both else "[XLP] 2차 조건 충족 | 지정가 검토")
            act = ((f"미국 계좌에서 1차(금융자산 {b1['alloc_note']})와 2차를 나눠 지정가 주문. 1차 {b1['close_max']:.0f}달러 이하, 2차 {b2['close_max']:.0f}달러 이하. " if both
                    else f"미국 계좌에서 2차를 {b2['close_max']:.0f}달러 이하 지정가로 주문(XLP 금융자산 {b2['alloc_note']}). 1차를 아직 안 샀다면 1차부터. ") + ORDER_TIP)
            alerts.append((title, body(stamp, status, act, "종가가 77.5달러 위로 올라가면 2차 조건이 다시 무장됩니다. 같은 구간에서 반복 알림은 없습니다.", TRIM, AVOID), 4))
            state["buy2_armed"] = False
            if both:
                state["buy1_armed"] = False
        elif fire1:
            alerts.append((
                "[XLP] 1차 조건 충족 | 지정가 검토",
                body(stamp, status,
                     f"미국 계좌에서 {b1['close_max']:.0f}달러 이하 지정가로 1차 주문(금융자산 {b1['alloc_note']}). " + ORDER_TIP,
                     f"2차는 종가 {b2['close_max']:.0f}달러 이하이면서 SPY가 52주 종가 고점 대비 -{b2['spy_drawdown_min_pct']:.0f}% 이상일 때. 지금 {spy_note}.",
                     TRIM, AVOID),
                4,
            ))
            state["buy1_armed"] = False
        if not state.get("buy1_armed", True) and not fire1 and close > b1["rearm_close"]:
            state["buy1_armed"] = True
        if not state.get("buy2_armed", True) and not fire2 and close > b2["rearm_close"]:
            state["buy2_armed"] = True
        state.update(last_bar_date=last_d.isoformat(), last_close=close,
                     last_spy_drawdown_pct=(round(spy_dd, 2) if spy_dd is not None else None))

    for title, msg, pri in alerts:
        print(title)
        print(msg)
        if not dry:
            publish(topic, title, msg, pri, ["chart_with_downwards_trend"], server)
    if not alerts:
        print(f"no alert (b1={is_b1} b2={is_b2} surge={surge} first={first})")
    if dry:
        print("DRY RUN: no publish, state not written")
        return 0
    state["updated_at"] = now.isoformat()
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
