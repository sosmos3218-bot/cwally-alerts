#!/usr/bin/env python3
"""473640 HANARO 글로벌금채굴기업: 1차 검토 조건 알림 (change-only).

afternoon 슬롯(평일 15:20 KST). 실시간 가격과 실시간 추정 NAV(iNAV)를 본다.
- 1차 검토: 가격 <= 27,000원 AND 괴리율(가격/iNAV - 1) <= 0%
- 재무장: 울린 뒤 가격 > 28,000원
- 보류(무효 조건): 금 선물 완료 종가 2거래일 연속 < 4,000달러, 또는 순자산 < 400억 AND 당일 거래대금 < 1억
modes: auto(기본) | dry(발송 없음, state 안 씀) | install(설치 확인 1회, state 안 씀)
"""
from __future__ import annotations

import json
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic  # noqa: E402
import naver_api  # noqa: E402

KST = timezone(timedelta(hours=9))
NAME = "473640"
CFG_PATH = ROOT / "configs" / f"{NAME}.json"
STATE_PATH = ROOT / "state" / f"{NAME}.json"
UA = {"User-Agent": "Mozilla/5.0 (cwally-alerts; +mailto:sosmos3218@gmail.com)"}


def load_state() -> dict:
    base = {
        "updated_at": None,
        "buy1_armed": True,
        "hold": None,
        "last_price": None,
        "last_inav": None,
        "last_premium_pct": None,
        "last_gold": None,
        "fetch_fail_date": None,
    }
    if STATE_PATH.exists():
        try:
            base.update(json.loads(STATE_PATH.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass
    return base


def eok(value) -> float | None:
    """'641억' / '1.15억' / '1조 2,345억' -> 억원 float."""
    if value is None:
        return None
    s = str(value).replace(",", "").replace(" ", "")
    m = re.fullmatch(r"(?:(\d+(?:\.\d+)?)조)?(?:(\d+(?:\.\d+)?)억)?", s)
    if m and (m.group(1) or m.group(2)):
        return float(m.group(1) or 0) * 10000 + float(m.group(2) or 0)
    return naver_api.num(s)


def etf_quote(code: str) -> dict:
    out = {"price": None, "inav": None, "aum": None, "value": None, "at": None, "err": None, "inav_src": None}
    try:
        b = naver_api.get_json(f"https://m.stock.naver.com/api/stock/{code}/basic")
        i = naver_api.get_json(f"https://m.stock.naver.com/api/stock/{code}/integration")
    except Exception as exc:  # noqa: BLE001
        out["err"] = str(exc)
        return out
    out["price"] = naver_api.num(b.get("closePrice"))
    out["at"] = b.get("localTradedAt")
    for t in i.get("totalInfos") or []:
        c = str(t.get("code"))
        if c.lower() == "nav":
            out["inav"] = naver_api.num(t.get("value"))
            out["inav_src"] = "naver"
        elif c == "accumulatedTradingValue":
            out["value"] = eok(t.get("value"))
    out["aum"] = eok((i.get("etfKeyIndicator") or {}).get("totalNav"))
    if out["inav"] is None:
        try:
            z = naver_api.get_json(f"https://etf.zeroin.co.kr/api/quote/{code}")
            if str(z.get("as_of")) == datetime.now(KST).strftime("%Y%m%d"):
                out["inav"] = naver_api.num(z.get("nav"))
                out["inav_src"] = "zeroin"
        except Exception:  # noqa: BLE001
            pass
    if not out["price"] or not out["inav"]:
        out["err"] = "가격 또는 iNAV 없음"
    return out


def gold_closes() -> tuple[list[tuple[str, float]], str | None]:
    """Yahoo GC=F 완료 종가(최신순).

    Yahoo는 일봉을 뉴욕 자정 타임스탬프로 주고, 진행 중인 실시간 값은 자정이 아닌 타임스탬프로 덧붙인다.
    자정 정렬 봉만 쓰고, 날짜 D 봉은 뉴욕 D 17:00(선물 일일 마감) 이후에만 완료로 본다.
    """
    try:
        req = urllib.request.Request("https://query1.finance.yahoo.com/v8/finance/chart/GC%3DF?range=1mo&interval=1d", headers=UA)
        with urllib.request.urlopen(req, timeout=20) as resp:
            d = json.loads(resp.read().decode())["chart"]["result"][0]
    except Exception as exc:  # noqa: BLE001
        return [], f"yahoo: {exc}"
    off = int((d.get("meta") or {}).get("gmtoffset") or -14400)
    ny = timezone(timedelta(seconds=off))
    now_ny = datetime.now(ny)
    rows = {}
    for t, c in zip(d.get("timestamp") or [], d["indicators"]["quote"][0].get("close") or []):
        if not c or t % 86400 not in (14400, 18000):
            continue  # 실시간 포인트 또는 비정렬 값 (뉴욕 자정 = UTC 04:00 EDT / 05:00 EST)
        bar = datetime.fromtimestamp(t, timezone(timedelta(seconds=-(t % 86400)))).replace(tzinfo=ny)
        if now_ny < bar.replace(hour=17, minute=0):
            continue  # 아직 끝나지 않은 세션
        rows[bar.strftime("%Y%m%d")] = float(c)
    out = sorted(rows.items(), reverse=True)
    return out, (None if len(out) >= 2 else f"yahoo: 완료 종가 {len(out)}개")


def body(stamp: str, status: str, action: str, later: str, trim: str, avoid: str) -> str:
    return "\n".join(
        [
            f"{stamp} | 473640 | HANARO 글로벌금채굴기업",
            "이게 뭐 알림: 1차 검토 조건 알림. 가격 27,000원 이하이면서 괴리율 0% 이하(iNAV보다 비싸지 않음)일 때만 옵니다. 금 2거래일 4,000달러 미만이나 순자산 400억 미만+거래대금 1억 미만이면 보류. 자동 주문 아님.",
            f"지금 결과/상태: {status}",
            f"지금 할 일: {action}",
            f"나중에: {later}",
            f"익절/줄일 때: {trim}",
            f"하지 말 것: {avoid}",
        ]
    )


TRIM = "금광 ETF는 위성 비중으로만(예: 위험자산의 5% 이내). 금이 2거래일 4,000달러 미만이면 추가 매수 중단."
AVOID = "시장가 주문, 장 시작 동시호가(08:30~09:00)나 마감 동시호가(15:20~15:30) 주문, iNAV보다 비싼 지정가, 한 번에 전액 매수, 금 헤지용으로 착각."
ORDER_TIP = "다음 거래일 10:00~15:00 사이에 HTS의 추정NAV(iNAV) 이하로만 지정가. 주문 직전 가격 27,000원 이하와 괴리율 0% 이하를 한 번 더 확인."


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    today = now.strftime("%Y%m%d")
    topic = resolve_topic("port", cfg.get("ntfy_topic"))
    server = resolve_server(cfg.get("ntfy_server"))
    b1, hd = cfg["buy1"], cfg["hold"]

    end = cfg.get("ends_kst")
    if mode != "install" and end and now > datetime.strptime(end, "%Y-%m-%d %H:%M").replace(tzinfo=KST):
        print(f"ended at {end}; silent")
        return 0

    q = etf_quote(cfg["code"])
    gold, gerr = gold_closes()
    price, inav = q["price"], q["inav"]
    print(f"quote={q} gold={gold[:3]} gerr={gerr}")

    prem = (price / inav - 1) * 100 if price and inav else None
    is_buy1 = bool(prem is not None and price <= b1["price_max"] and prem <= b1["premium_max_pct"])
    g_last = gold[: hd["gold_days"]]
    gold_hold = len(g_last) == hd["gold_days"] and all(c < hd["gold_close_below"] for _, c in g_last)
    liq_hold = bool(q["aum"] is not None and q["value"] is not None and q["aum"] < hd["aum_below_eok"] and q["value"] < hd["value_below_eok"])
    is_hold = gold_hold or liq_hold
    reasons = []
    if gold_hold:
        reasons.append(f"금 완료 종가 {hd['gold_days']}거래일 연속 {hd['gold_close_below']:,}달러 미만")
    if liq_hold:
        reasons.append(f"순자산 {q['aum']:,.0f}억, 거래대금 {q['value']:.2f}억")

    q_txt = (f"가격 {price:,.0f}원, iNAV {inav:,.0f}원, 괴리율 {prem:+.2f}%" if prem is not None else f"가격/iNAV 조회 실패({q['err']})")
    if q["aum"] is not None:
        q_txt += f", 순자산 {q['aum']:,.0f}억"
    if q["value"] is not None:
        q_txt += f", 오늘 거래대금 {q['value']:.2f}억"
    g_txt = ("금 완료 종가 " + " / ".join(f"{d[4:6]}/{d[6:]} {c:,.0f}달러" for d, c in g_last)) if g_last else f"금 조회 실패({gerr})"
    gap_txt = f" 1차 가격까지 {(b1['price_max'] / price - 1) * 100:+.1f}%." if price else ""
    status = f"{q_txt}. {g_txt}.{gap_txt}"

    if mode == "install":
        flags = ["1차 조건 충족" if is_buy1 else "1차 조건 미충족", ("보류 켜짐(" + ", ".join(reasons) + ")") if is_hold else "보류 아님"]
        publish(
            topic,
            "[473640] 설치 확인 | 매매 신호 아님",
            body(stamp, f"평일 15:20 KST afternoon 슬롯에 붙였습니다. {status} 지금 판정: {', '.join(flags)}.",
                 "설치 확인만 하세요. 이 알림으로 사지 마세요.",
                 "1차 조건이 새로 켜질 때, 보류 조건이 켜지거나 풀릴 때만 옵니다. 울린 뒤에는 28,000원 위로 갔다 와야 다시 울립니다.",
                 TRIM, "설치 확인을 매수 신호로 읽기."),
            3, ["information_source"], server,
        )
        print("install confirmation sent")
        return 0

    dry = mode == "dry"
    state = load_state()
    first = state.get("updated_at") is None
    alerts: list[tuple[str, str, int]] = []

    if price is None or inav is None:
        if state.get("fetch_fail_date") != today:
            alerts.append((
                "[473640] 조회 실패 | 조건 미확인",
                body(stamp, status, "증권 앱에서 473640 가격과 추정NAV를 직접 보세요.",
                     "다음 평일 15:20에 다시 봅니다.", "이번 알림으로 비중을 바꾸지 마세요.", "조회 실패를 조건 충족이나 정상으로 읽기."),
                3,
            ))
            state["fetch_fail_date"] = today
    else:
        prev_hold = state.get("hold")
        if not first and prev_hold is not None and is_hold != prev_hold and not (is_buy1 and state.get("buy1_armed", True)):
            if is_hold:
                alerts.append((
                    "[473640] 보류 조건 켜짐 | 1차 검토 멈춤",
                    body(stamp, status + " 보류 이유: " + ", ".join(reasons) + ".",
                         "가격이 27,000원 아래로 와도 새로 사지 마세요. 걸어 둔 지정가가 있으면 취소.",
                         "보류가 풀리면 따로 알림이 옵니다. 그때 가격과 괴리율을 다시 확인.", TRIM, "보류 중 물타기, 미체결 지정가 방치."),
                    4,
                ))
            else:
                alerts.append((
                    "[473640] 보류 해제 | 1차 조건은 따로 확인",
                    body(stamp, status, "보류만 풀렸습니다. 가격 27,000원 이하와 괴리율 0% 이하가 함께 맞아야 1차 검토입니다.",
                         "1차 조건이 켜지면 따로 알림이 옵니다.", TRIM, "해제 알림을 매수 신호로 읽기."),
                    3,
                ))
        state["hold"] = is_hold

        if is_buy1 and state.get("buy1_armed", True):
            if is_hold:
                alerts.append((
                    "[473640] 가격, 괴리율 충족 | 보류 조건 때문에 검토 보류",
                    body(stamp, status + " 보류 이유: " + ", ".join(reasons) + ".",
                         "가격 조건은 맞았지만 무효 조건이 켜져 있어 사지 않습니다.",
                         "보류가 풀린 뒤에도 27,000원 이하, 괴리율 0% 이하면 그때 검토.", TRIM, AVOID),
                    4,
                ))
            else:
                alerts.append((
                    "[473640] 1차 조건 충족 | 지정가 1차 검토",
                    body(stamp, status, "1차 분할만 검토. " + ORDER_TIP,
                         "2차는 52주 저점 부근(약 25,000원)에서 따로 판단. 1차 뒤 가격만 보고 바로 더 사지 않음.", TRIM, AVOID),
                    4,
                ))
            state["buy1_armed"] = False
        elif not state.get("buy1_armed", True) and price > b1["rearm_price"]:
            state["buy1_armed"] = True

        state.update(last_price=price, last_inav=inav, last_premium_pct=round(prem, 3), last_gold=(g_last[0] if g_last else None))

    for title, msg, pri in alerts:
        print(title)
        print(msg)
        if not dry:
            publish(topic, title, msg, pri, ["chart_with_downwards_trend"], server)
    if not alerts:
        print(f"no alert (buy1={is_buy1} hold={is_hold} armed={state.get('buy1_armed')} first={first})")
    if dry:
        print("DRY RUN: no publish, state not written")
        return 0
    state["updated_at"] = now.isoformat()
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
