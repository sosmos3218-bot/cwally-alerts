#!/usr/bin/env python3
"""245710 ACE 베트남VN30(합성): ISA 지정가 분할 조건 알림 (change-only).

afternoon 슬롯(평일 15:20 KST). 245710 실시간 가격과 실시간 추정 NAV(iNAV), VN지수 직전 완료 종가를 본다.
- 1차 후보: 가격 <= 26,000원 AND iNAV 대비 할인 >= 2% AND 중단 상태 아님
- 2차 후보: 가격 <= 24,700원 AND iNAV 대비 할인 >= 2% AND 중단 상태 아님
- 중단: VN지수 완료 종가 < 1,650 (회복 시 해제 알림)
- 3차 추세 확인: VN지수 완료 종가 3거래일 연속 >= 1,780
modes: auto(기본) | dry(발송 없음, state 안 씀) | install(설치 확인 1회, state 안 씀)
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
NAME = "245710"
CFG_PATH = ROOT / "configs" / f"{NAME}.json"
STATE_PATH = ROOT / "state" / f"{NAME}.json"
UA = {"User-Agent": "Mozilla/5.0 (cwally-alerts; +mailto:sosmos3218@gmail.com)"}


def load_state() -> dict:
    base = {
        "updated_at": None,
        "stop": None,
        "buy1_armed": True,
        "buy2_armed": True,
        "confirm_armed": True,
        "last_price": None,
        "last_inav": None,
        "last_vn_day": None,
        "last_vn_close": None,
        "fetch_fail_date": None,
    }
    if STATE_PATH.exists():
        try:
            base.update(json.loads(STATE_PATH.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass
    return base


def etf_quote(code: str) -> tuple[float | None, float | None, str | None, str | None]:
    """(price, inav, traded_at, err)"""
    try:
        b = naver_api.get_json(f"https://m.stock.naver.com/api/stock/{code}/basic")
        i = naver_api.get_json(f"https://m.stock.naver.com/api/stock/{code}/integration")
    except Exception as exc:  # noqa: BLE001
        return None, None, None, str(exc)
    price = naver_api.num(b.get("closePrice"))
    inav = None
    for t in i.get("totalInfos") or []:
        if str(t.get("code")).lower() == "nav":
            inav = naver_api.num(t.get("value"))
    if inav is None:
        inav = naver_api.num((i.get("etfKeyIndicator") or {}).get("nav"))
    if not price or not inav:
        return price, inav, b.get("localTradedAt"), "가격 또는 iNAV 없음"
    return price, inav, b.get("localTradedAt"), None


def vn_closes_naver() -> list[tuple[str, float]]:
    raw = naver_api.get_json("https://api.stock.naver.com/index/.VNI/price?page=1&pageSize=10")
    out = []
    for r in raw if isinstance(raw, list) else []:
        day = str(r.get("localTradedAt") or "")[:10].replace("-", "")
        c = naver_api.num(r.get("closePrice"))
        if day and c:
            out.append((day, c))
    return out


def vn_closes_yahoo() -> list[tuple[str, float]]:
    req = urllib.request.Request("https://query1.finance.yahoo.com/v8/finance/chart/%5EVNINDEX.VN?range=1mo&interval=1d", headers=UA)
    with urllib.request.urlopen(req, timeout=20) as resp:
        d = json.loads(resp.read().decode())["chart"]["result"][0]
    ict = timezone(timedelta(hours=7))
    out = []
    for t, c in zip(d.get("timestamp") or [], d["indicators"]["quote"][0].get("close") or []):
        if c:
            out.append((datetime.fromtimestamp(t, ict).strftime("%Y%m%d"), float(c)))
    return out


def vn_completed(today: str) -> tuple[list[tuple[str, float]], str | None, str]:
    """직전 완료 종가 목록(최신순). 오늘(베트남 장중일 수 있음) 행은 뺀다."""
    errs = []
    for label, fn in (("naver", vn_closes_naver), ("yahoo", vn_closes_yahoo)):
        try:
            rows = sorted({d: c for d, c in fn() if d < today}.items(), reverse=True)
            if len(rows) >= 3:
                return rows, None, label
            errs.append(f"{label}: 완료 종가 {len(rows)}개")
        except Exception as exc:  # noqa: BLE001
            errs.append(f"{label}: {exc}")
    return [], "; ".join(errs), "-"


def body(stamp: str, status: str, action: str, later: str, trim: str, avoid: str) -> str:
    return "\n".join(
        [
            f"{stamp} | 245710 | ACE 베트남VN30(합성)",
            "이게 뭐 알림: ISA 1,000만원 3분할 지정가 조건 알림. 1차 26,000원, 2차 24,700원 이하(각각 iNAV 할인 2% 이상), VN지수 1,650 미만이면 중단, VN지수 1,780 이상 3거래일이면 3차. 자동 주문 아님.",
            f"지금 결과/상태: {status}",
            f"지금 할 일: {action}",
            f"나중에: {later}",
            f"익절/줄일 때: {trim}",
            f"하지 말 것: {avoid}",
        ]
    )


TRIM = "베트남 합계가 전체 금융자산 5%를 넘으면 더 사지 않음. 52주 저점 25,825원 이탈이 이틀 넘게 이어지면 비중 축소 검토."
AVOID = "시장가 주문, 장 시작 직후나 15:20 이후 주문, 한 번에 1,000만원 다 사기, 할인 5% 넘을 때 운용사 공지 확인 없이 매수."


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    today = now.strftime("%Y%m%d")
    topic = resolve_topic("port", cfg.get("ntfy_topic"))
    server = resolve_server(cfg.get("ntfy_server"))
    b1, b2, st, cf = cfg["buy1"], cfg["buy2"], cfg["stop"], cfg["confirm"]

    end = cfg.get("ends_kst")
    if mode != "install" and end and now > datetime.strptime(end, "%Y-%m-%d %H:%M").replace(tzinfo=KST):
        print(f"ended at {end}; silent")
        return 0

    price, inav, traded_at, qerr = etf_quote(cfg["code"])
    vn, verr, vsrc = vn_completed(today)
    print(f"price={price} inav={inav} at={traded_at} qerr={qerr} vn={vn[:4]} vsrc={vsrc} verr={verr}")

    disc = (1 - price / inav) * 100 if price and inav else None
    vn_last = vn[0] if vn else None
    is_stop = (vn_last[1] < st["vn_close_below"]) if vn_last else None
    streak = vn[: cf["days"]]
    is_confirm = len(streak) == cf["days"] and all(c >= cf["vn_close_min"] for _, c in streak)
    is_buy1 = bool(price and disc is not None and price <= b1["price_max"] and disc >= b1["discount_min_pct"] and is_stop is False)
    is_buy2 = bool(price and disc is not None and price <= b2["price_max"] and disc >= b2["discount_min_pct"] and is_stop is False)

    q_txt = f"가격 {price:,.0f}원, iNAV {inav:,.0f}원, 할인 {disc:.1f}%" if disc is not None else f"가격/iNAV 조회 실패({qerr})"
    v_txt = (f"VN지수 직전 종가 {vn_last[1]:,.2f}({vn_last[0][4:6]}/{vn_last[0][6:]}), 최근 3일 " + " / ".join(f"{c:,.0f}" for _, c in streak)) if vn_last else f"VN지수 조회 실패({verr})"
    status = f"{q_txt}. {v_txt}."
    if disc is not None and disc > cfg["discount_warn_pct"]:
        status += f" 할인 {disc:.1f}%로 {cfg['discount_warn_pct']:.0f}% 초과: 운용사 공지(설정 중단, 스왑 한도) 먼저 확인."

    if mode == "install":
        flags = []
        flags.append("중단 상태" if is_stop else "중단 아님")
        flags.append("1차 조건 충족" if is_buy1 else "1차 조건 미충족")
        flags.append("2차 조건 충족" if is_buy2 else "2차 조건 미충족")
        flags.append("3차 추세 확인됨" if is_confirm else "3차 추세 미확인")
        publish(
            topic,
            "[245710] 설치 확인 | 매매 신호 아님",
            body(stamp, f"평일 15:20 KST afternoon 슬롯에 붙였습니다. {status} 지금 판정: {', '.join(flags)}.",
                 "설치 확인만 하세요. 이 알림으로 사지 마세요.",
                 "1차, 2차 조건이 새로 켜질 때, VN지수 1,650 이탈/회복 때, 1,780 3거래일 확인 때만 옵니다.",
                 TRIM, "설치 확인을 매수 신호로 읽기."),
            3, ["information_source"], server,
        )
        print("install confirmation sent")
        return 0

    dry = mode == "dry"
    state = load_state()
    first = state.get("updated_at") is None
    alerts: list[tuple[str, str, int]] = []

    if price is None or inav is None or not vn:
        if state.get("fetch_fail_date") != today:
            alerts.append((
                "[245710] 조회 실패 | 조건 미확인",
                body(stamp, status, "증권 앱에서 245710 가격, 추정NAV, VN지수를 직접 보세요.",
                     "다음 평일 15:20에 다시 봅니다.", "이번 알림으로 비중을 바꾸지 마세요.", "조회 실패를 조건 충족이나 정상으로 읽기."),
                3,
            ))
            state["fetch_fail_date"] = today
    else:
        prev_stop = state.get("stop")
        if not first and prev_stop is not None and is_stop != prev_stop:
            if is_stop:
                alerts.append((
                    "[245710] VN지수 1,650 이탈 | 남은 분할 중단",
                    body(stamp, status, "남은 회차 주문을 모두 취소하고 멈춤. 이미 산 물량은 유지.",
                         "VN지수 종가가 1,650 위로 돌아오면 해제 알림이 옵니다. 그때 조건 다시 확인.", TRIM,
                         "물타기, 미체결 지정가 방치."),
                    4,
                ))
            else:
                alerts.append((
                    "[245710] VN지수 1,650 회복 | 분할 중단 해제",
                    body(stamp, status, "중단만 풀림. 1차, 2차는 가격과 할인 조건을 다시 만족할 때만.",
                         "1차, 2차 조건이 켜지면 따로 알림.", TRIM, "해제 알림을 매수 신호로 읽기."),
                    3,
                ))
        state["stop"] = is_stop

        fire1 = is_buy1 and state.get("buy1_armed", True)
        fire2 = is_buy2 and state.get("buy2_armed", True)
        order_tip = "가격은 그때 HTS 추정NAV x 0.98 이하. 주문은 다음 거래일 KST 11:15~13:30(호치민 오전장)에, 그때 조건을 한 번 더 확인하고."
        if fire2:
            both = fire1
            amt = b1["amount_manwon"] + b2["amount_manwon"] if both else b2["amount_manwon"]
            title = (f"[245710] 1, 2차 조건 동시 충족 | {amt}만원 지정가 검토" if both
                     else f"[245710] 2차 조건 충족 | {amt}만원 지정가 검토")
            act = (f"ISA에서 1차 {b1['amount_manwon']}만원과 2차 {b2['amount_manwon']}만원을 지정가로 나눠 주문. 2차는 {b2['price_max']:,}원 이하. " if both
                   else f"ISA에서 2차 {b2['amount_manwon']}만원을 {b2['price_max']:,}원 이하 지정가로 주문. 1차를 아직 안 샀다면 1차부터. ") + order_tip
            alerts.append((title, body(stamp, status, act,
                                       "3차는 VN지수 1,780 이상 3거래일 확인 뒤(가격 28,500원 이하). 2차 뒤에는 가격만 보고 더 사지 않음.", TRIM, AVOID), 4))
            state["buy2_armed"] = False
            if both:
                state["buy1_armed"] = False
        elif fire1:
            alerts.append((
                f"[245710] 1차 조건 충족 | {b1['amount_manwon']}만원 지정가 검토",
                body(stamp, status,
                     f"ISA에서 {b1['amount_manwon']}만원을 지정가로 주문. {b1['price_max']:,}원 이하. " + order_tip,
                     f"2차는 {b2['price_max']:,}원 이하(할인 2% 이상). 3차는 VN지수 1,780 이상 3거래일 확인 뒤(가격 28,500원 이하).", TRIM, AVOID),
                4,
            ))
            state["buy1_armed"] = False
        if not state.get("buy1_armed", True) and not fire1 and price > b1["rearm_price"]:
            state["buy1_armed"] = True
        if not state.get("buy2_armed", True) and not fire2 and price > b2["rearm_price"]:
            state["buy2_armed"] = True

        if is_confirm and state.get("confirm_armed", True) and not first:
            cap_ok = price <= cf["price_cap"]
            alerts.append((
                f"[245710] VN지수 1,780 3일 유지 | 3차 {cf['amount_manwon']}만원 검토",
                body(stamp, status,
                     (f"추세 회복 확인. 가격 {cf['price_cap']:,}원 이하에서 {cf['amount_manwon']}만원 지정가 검토. 할인 2% 이상일 때만." if cap_ok
                      else f"추세는 확인됐지만 가격이 상한 {cf['price_cap']:,}원 위. 3차는 보류."),
                     "VN지수 종가가 1,750 아래로 내려가면 이 조건은 다시 무장됩니다.", TRIM, AVOID),
                4,
            ))
            state["confirm_armed"] = False
        elif is_confirm and first:
            state["confirm_armed"] = False
        elif not state.get("confirm_armed", True) and vn_last[1] < cf["rearm_below"]:
            state["confirm_armed"] = True

        state.update(last_price=price, last_inav=inav, last_vn_day=vn_last[0], last_vn_close=vn_last[1])

    for title, msg, pri in alerts:
        print(title)
        print(msg)
        if not dry:
            publish(topic, title, msg, pri, ["chart_with_downwards_trend"], server)
    if not alerts:
        print(f"no alert (stop={is_stop} buy1={is_buy1} buy2={is_buy2} confirm={is_confirm} first={first})")
    if dry:
        print("DRY RUN: no publish, state not written")
        return 0
    state["updated_at"] = now.isoformat()
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
