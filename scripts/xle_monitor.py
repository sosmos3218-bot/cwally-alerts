#!/usr/bin/env python3
"""XLE 1차 가격 조건 알림 (change-only).

morning 슬롯(평일 09:00 KST). 미국 직전 완료 일봉 종가만 본다(진행 중인 봉은 제외).
- 1차: XLE 완료 종가 <= 63.50달러이면 한 번 울림
- 재무장: 종가 > 66달러가 되면 다시 켜짐
- 브렌트(BZ=F)는 참고값으로만 적는다. 85달러 미만 경계는 [OIL]이 따로 본다.
modes: auto(기본) | dry(발송 없음, state 안 씀; XLE_TEST_CLOSE로 종가 대체) | install(설치 확인 1회, state 안 씀)
"""
from __future__ import annotations

import json
import os
import sys
import time
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
NAME = "xle"
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
        "last_bar_date": None,
        "last_close": None,
        "fetch_fail_date": None,
    }
    if STATE_PATH.exists():
        try:
            base.update(json.loads(STATE_PATH.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass
    return base


def fetch_json(url: str) -> dict:
    last: Exception | None = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read().decode())
        except Exception as exc:  # noqa: BLE001  retry network hiccups and 429
            last = exc
            time.sleep(3 * (attempt + 1))
    raise last if last else RuntimeError("fetch failed")


def fetch_bars(symbol: str, now_ny: datetime, rng: str = "1y") -> list[tuple[date, float]]:
    """완료 일봉 (미국 동부 날짜, 종가) 오름차순. 진행 중인 오늘 봉은 뺀다."""
    sym = urllib.request.quote(symbol)
    res = fetch_json(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range={rng}&interval=1d")["chart"]["result"][0]
    tz = now_ny.tzinfo
    out: dict[date, float] = {}
    for t, c in zip(res.get("timestamp") or [], res["indicators"]["quote"][0].get("close") or []):
        if c is None:
            continue
        out[datetime.fromtimestamp(t, tz).date()] = float(c)
    today = now_ny.date()
    after_close = now_ny.hour * 60 + now_ny.minute >= 16 * 60 + 10
    return sorted((d, c) for d, c in out.items() if d < today or (d == today and after_close))


def body(stamp: str, status: str, action: str, later: str, trim: str, avoid: str) -> str:
    return "\n".join(
        [
            f"{stamp} | XLE | Energy Select Sector SPDR",
            "이게 뭐 알림: XLE(미국 에너지 ETF) 완료 종가가 1차 가격 63.50달러 이하로 새로 내려왔을 때만 울립니다. 해외주식 계좌용이며 ISA에는 못 담습니다. 자동 주문 아님.",
            f"지금 결과/상태: {status}",
            f"지금 할 일: {action}",
            f"나중에: {later}",
            f"익절/줄일 때: {trim}",
            f"하지 말 것: {avoid}",
        ]
    )


OIL_LINE = "[OIL] 브렌트 85달러 미만 알림과 함께 보세요. 그 알림이 이미 왔거나 같이 오면 유가 하락이 이어지는 중이라 1차를 더 낮게 다시 잡을지 먼저 판단하세요."
TRIM = "XLE 비중이 목표(포트폴리오 3~5%)의 1.5배를 넘으면 일부 줄이기를 검토. 올해 해외주식 양도차익이 이미 250만원을 넘어, 연내 실현 이익은 22% 과세 대상."
AVOID = "시장가 주문, 한 번에 목표 비중 전부 매수, 2차(58달러)를 공급 차질 확인 없이 자동으로 사기, 원/달러 확인 없이 주문, ISA에서 XLE 찾기(불가)."
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
    b1 = cfg["buy1"]
    level = float(b1["close_max"])
    rearm = float(b1["rearm_close"])

    end = cfg.get("ends_kst")
    if mode not in {"install", "dry"} and end and now > datetime.strptime(end, "%Y-%m-%d %H:%M").replace(tzinfo=KST):
        print(f"ended at {end}; silent")
        return 0

    errs: list[str] = []
    xle: list[tuple[date, float]] = []
    try:
        rows = fetch_bars("XLE", now_ny)
        if len(rows) < 2:
            raise RuntimeError(f"XLE 완료 일봉 {len(rows)}개")
        if (now_ny.date() - rows[-1][0]).days > int(cfg["stale_days"]):
            raise RuntimeError(f"XLE 마지막 봉 {rows[-1][0]} 너무 오래됨")
        xle = rows
    except Exception as exc:  # noqa: BLE001
        errs.append(f"XLE: {exc}")

    test = os.environ.get("XLE_TEST_CLOSE")
    if mode == "dry" and test and xle:
        xle = xle[:-1] + [(xle[-1][0], float(test))]

    brent_txt = "브렌트 참고값 조회 실패"
    try:
        br = fetch_bars(cfg["oil_link"]["brent_symbol"], now_ny, "1mo")
        if br:
            brent_txt = f"브렌트 선물 ${br[-1][1]:,.2f}({br[-1][0]:%m/%d}, 참고값, [OIL] 경계 85달러)"
    except Exception as exc:  # noqa: BLE001
        print(f"brent fetch failed: {exc}")

    last_d = xle[-1][0] if xle else None
    close = xle[-1][1] if xle else None
    prev = xle[-2][1] if len(xle) >= 2 else None
    day_pct = (close / prev - 1) * 100 if close and prev else None
    hi52 = max(c for _, c in xle) if len(xle) >= 150 else None
    is_b1 = bool(close is not None and close <= level)

    if close is not None:
        status = f"XLE 종가 ${close:,.2f}({last_d:%m/%d} 미국)"
        if day_pct is not None:
            status += f", 전일 대비 {day_pct:+.2f}%"
        if hi52:
            status += f", 52주 종가 고점 ${hi52:,.2f} 대비 {(close / hi52 - 1) * 100:+.1f}%"
        status += f". {brent_txt}."
    else:
        status = "XLE 조회 실패: " + "; ".join(errs) + "."

    if mode == "install":
        publish(
            topic,
            "[XLE] 설치 확인 | 매매 신호 아님",
            body(stamp,
                 f"평일 09:00 KST morning 슬롯에 붙였습니다. {status} 지금 판정: 1차 조건 {'충족' if is_b1 else '미충족'}(기준 {level:.2f}달러 이하).",
                 "설치 확인만 하세요. 이 알림으로 사지 마세요.",
                 f"종가가 {level:.2f}달러 이하로 새로 내려올 때만 옵니다. {rearm:.0f}달러 위로 올라갔다가 다시 내려오면 또 옵니다. " + OIL_LINE,
                 TRIM, "설치 확인을 매수 신호로 읽기."),
            3, ["information_source"], server,
        )
        print("install confirmation sent")
        return 0

    dry = mode == "dry"
    state = load_state()
    alerts: list[tuple[str, str, int]] = []
    print(f"close={close} bar={last_d} day_pct={day_pct} is_b1={is_b1} armed={state.get('buy1_armed')} errs={errs}")

    if close is None:
        if state.get("fetch_fail_date") != today_kst:
            alerts.append((
                "[XLE] 조회 실패 | 조건 미확인",
                body(stamp, status + " 조건 미충족이 아닙니다.", "증권사 앱에서 XLE 종가를 직접 보세요.",
                     "다음 평일 09:00에 다시 봅니다.", "이번 알림으로 비중을 바꾸지 마세요.", "조회 실패를 가격 미도달이나 정상으로 읽기."),
                3,
            ))
            state["fetch_fail_date"] = today_kst
    else:
        if is_b1 and state.get("buy1_armed", True):
            alerts.append((
                f"[XLE] {level:.2f}달러 이하 | 1차 지정가 검토",
                body(stamp, status,
                     f"해외주식 계좌에서 1차만 {level:.2f}달러 이하 지정가로 검토하세요(목표 {b1['alloc_note']}의 일부). " + ORDER_TIP,
                     OIL_LINE + f" 2차 58달러 부근은 공급 차질이 2027년에도 남는다는 확인이 있을 때만입니다. 종가가 {rearm:.0f}달러 위로 올라가면 이 알림이 다시 켜집니다.",
                     TRIM, AVOID),
                4,
            ))
            state["buy1_armed"] = False
        elif not state.get("buy1_armed", True) and close > rearm:
            state["buy1_armed"] = True
        state.update(last_bar_date=last_d.isoformat(), last_close=close)

    for title, msg, pri in alerts:
        print(title)
        print(msg)
        if not dry:
            publish(topic, title, msg, pri, ["chart_with_downwards_trend"], server)
    if not alerts:
        print(f"no alert (b1={is_b1} armed={state.get('buy1_armed')})")
    if dry:
        print("DRY RUN: no publish, state not written")
        return 0
    state["updated_at"] = now.isoformat()
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
