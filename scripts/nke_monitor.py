#!/usr/bin/env python3
"""NKE watch: price zones, dividend cut, earnings flip/invalidate. Silent unless change."""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
CFG_PATH = ROOT / "configs" / "nke.json"
STATE_PATH = ROOT / "state" / "nke.json"
UA = {"User-Agent": "cwally-alerts nke research monitor research@example.com"}


def load_json(path: Path, fallback: dict) -> dict:
    if not path.exists():
        return dict(fallback)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return dict(fallback)
    out = dict(fallback)
    out.update(data)
    return out


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=25) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def quote(url: str) -> tuple[float | None, float | None, str | None]:
    raw = fetch(url)
    data = json.loads(raw)
    result = (data.get("chart") or {}).get("result") or []
    if not result:
        return None, None, "quote empty"
    meta = result[0].get("meta") or {}
    price = meta.get("regularMarketPrice")
    div = meta.get("trailingAnnualDividendRate")
    if div is None:
        div = meta.get("dividendRate")
    try:
        price_f = float(price) if price is not None else None
    except (TypeError, ValueError):
        price_f = None
    try:
        div_f = float(div) if div is not None else None
    except (TypeError, ValueError):
        div_f = None
    return price_f, div_f, None


def visible(html: str) -> str:
    text = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", html)
    text = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text)


def latest_sec_date(url: str) -> str | None:
    raw = fetch(url)
    data = json.loads(raw)
    recent = ((data.get("filings") or {}).get("recent") or {})
    forms = recent.get("form") or []
    dates = recent.get("filingDate") or []
    for form, day in zip(forms, dates):
        if form in {"8-K", "10-Q", "10-K"}:
            return day
    return None


def parse_earnings(blob: str) -> dict:
    china = None
    m = re.search(
        r"greater china[^.]{0,80}?(?:down|declin\w+|decreas\w+)[^.]{0,40}?(\d{1,2}(?:\.\d+)?)\s*percent",
        blob,
        re.I,
    )
    if m:
        china = -float(m.group(1))
    else:
        m = re.search(
            r"greater china[^.]{0,80}?(?:up|increas\w+|grew)[^.]{0,40}?(\d{1,2}(?:\.\d+)?)\s*percent",
            blob,
            re.I,
        )
        if m:
            china = float(m.group(1))
    gm = None
    m = re.search(r"gross margin[^.]{0,60}?(\d{2}(?:\.\d+)?)\s*percent", blob, re.I)
    if m:
        gm = float(m.group(1))
    fy28_growth = None
    if re.search(r"fiscal 2028[^.]{0,120}?(increas|grow|growth|up)", blob, re.I):
        fy28_growth = True
    if re.search(r"fiscal 2028[^.]{0,120}?(declin|down|decreas)", blob, re.I):
        fy28_growth = False
    eps_hi = None
    m = re.search(
        r"adjusted diluted earnings per share[^.]{0,80}?\$(\d+\.\d+)\s*(?:to|–|-)\s*\$(\d+\.\d+)",
        blob,
        re.I,
    )
    if m:
        eps_hi = float(m.group(2))
    na = None
    m = re.search(
        r"north america[^.]{0,80}?(?:up|increas\w+|grew)[^.]{0,30}?(\d{1,2}(?:\.\d+)?)\s*percent",
        blob,
        re.I,
    )
    if m:
        na = float(m.group(1))
    return {
        "china_yoy": china,
        "gm": gm,
        "fy28_growth": fy28_growth,
        "eps_hi": eps_hi,
        "na_yoy": na,
    }


def zone_of(price: float, watch: float, buy: float) -> str:
    if price <= buy:
        return "buy"
    if price <= watch:
        return "watch"
    return "above"


def body(stamp: str, status: str, action: str, later: str, trim: str, avoid: str) -> str:
    return "\n".join(
        [
            f"{stamp} | NKE | 변경조건 점검",
            "이게 뭐 알림: 나이키 관망 판단의 반대 근거와 변경 조건만 보는 알림입니다. 매수 지시가 아닙니다.",
            f"지금 결과/상태: {status}",
            f"지금 할 일: {action}",
            f"나중에: {later}",
            f"익절/줄일 때: {trim}",
            f"하지 말 것: {avoid}",
        ]
    )


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    dry = mode == "dry" or os.environ.get("NKE_DRY") == "1"
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    state = load_json(
        STATE_PATH,
        {
            "updated_at": None,
            "last_price": None,
            "zone": "above",
            "last_weekly": None,
            "div_alerted": False,
            "last_filing": cfg.get("last_known_filing"),
            "earnings_fingerprint": None,
            "fetch_fail_date": None,
        },
    )
    now = datetime.now(KST)
    today = now.date()
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    if today.isoformat() > cfg["valid_until"] and mode not in {"force", "dry"}:
        print(f"after {cfg['valid_until']}; silent")
        return 0

    th = cfg["thresholds"]
    base = cfg["baseline"]
    price, div, qerr = None, None, None
    try:
        price, div, qerr = quote(cfg["sources"]["quote"])
    except Exception as exc:
        qerr = str(exc)
    print(f"price={price} div={div} qerr={qerr}")

    alerts: list[tuple[str, str, int]] = []
    if price is None:
        if state.get("fetch_fail_date") != today.isoformat():
            alerts.append(
                (
                    "[NKE] 주가 조회 실패 | 조건 미확인",
                    body(
                        stamp,
                        f"주가 조회 실패. {qerr or '빈 응답'}. 조건 미충족이 아닙니다.",
                        "야후 시세나 증권 앱에서 NKE 종가를 직접 보세요. 30달러, 32달러를 이 알림으로 판정하지 마세요.",
                        "다음 평일 09:00 KST에 재시도합니다.",
                        "이번 알림으로 줄이거나 사지 마세요.",
                        "조회 실패를 30달러 이탈이나 정상으로 취급.",
                    ),
                    4,
                )
            )
            state["fetch_fail_date"] = today.isoformat()
    else:
        zone = zone_of(price, th["watch_price"], th["buy_price"])
        prev = state.get("zone") or "above"
        changed = zone != prev
        weekly = today.weekday() == 0 and state.get("last_weekly") != today.isoformat() and zone != "above"
        if changed or weekly or mode == "force":
            if zone == "buy":
                title = "[NKE] 30달러 이하 | 분할 관심"
                action = "이미 둔 관심 구간입니다. 새 돈은 한 번에 넣지 말고, 실적 2개 조건은 아직 가격과 별개입니다."
            elif zone == "watch":
                title = "[NKE] 32달러 이하 | 매수는 보류"
                action = "배당 수익률만 5% 근처로 붙는 구간입니다. 추격 금지, 30달러 재확인 전 신규 매수 보류."
            else:
                title = "[NKE] 32달러 위 | 관심 해제"
                action = "가격 조건은 다시 꺼졌습니다. 관망 유지."
            why = "구간 변경" if changed else "월요일 재알림"
            alerts.append(
                (
                    title,
                    body(
                        stamp,
                        f"NKE {price:.2f}달러. 기준일 {base['price_date']} {base['price']}달러. 구간 {prev} -> {zone}. {why}. 30달러 이하는 분할 관심, 32달러 이하는 보류.",
                        action,
                        "매수 전환은 실적에서 중국 한 자릿수 감소, FY2028 매출 증가 가이던스, 매출총이익률 44% 중 2개입니다.",
                        "32달러 위로 복귀하면 가격 관심은 해제. 보유 물량 손절 신호는 아닙니다.",
                        "32달러 터치를 매수 신호로 쓰기. 신용, 미수, 추격.",
                    ),
                    4 if zone == "buy" else 3,
                )
            )
            state["zone"] = zone
            state["last_price"] = price
            if weekly:
                state["last_weekly"] = today.isoformat()
        else:
            state["last_price"] = price
            state["zone"] = zone
            print(f"price zone unchanged ({zone}); silent")

        if div is None:
            print("dividend unavailable; not treated as cut")
        elif div + 1e-9 < th["div_cut_below"]:
            if not state.get("div_alerted") or mode == "force":
                alerts.append(
                    (
                        "[NKE] 배당 축소 | 30달러 지지 약화",
                        body(
                            stamp,
                            f"연간 배당 {div:.2f}달러, 기준 {base['annual_dividend']}달러 미만. 축소 쪽입니다.",
                            "30달러도 지지로 보지 마세요. 공시에서 배당 결정을 확인하세요.",
                            "배당이 1.64달러로 복귀하기 전 분할 관심을 보류.",
                            "배당 축소가 확인되면 보유 비중 재검토.",
                            "시세 배당률만 보고 삭감이라고 단정. 조회 공백을 삭감으로 읽기.",
                        ),
                        5,
                    )
                )
                state["div_alerted"] = True
        else:
            if state.get("div_alerted"):
                alerts.append(
                    (
                        "[NKE] 배당 기준 복귀 | 축소 경보 해제",
                        body(
                            stamp,
                            f"연간 배당 {div:.2f}달러로 기준 {base['annual_dividend']}달러 이상.",
                            "배당 축소 경보만 해제. 매수 전환은 아닙니다.",
                            "가격 구간과 실적 2개 조건은 그대로 보세요.",
                            "이번 알림으로 비중을 늘리지 마세요.",
                            "배당 복귀를 실적 회복으로 단정.",
                        ),
                        3,
                    )
                )
            state["div_alerted"] = False

    filing = None
    ferr = None
    try:
        filing = latest_sec_date(cfg["sources"]["sec"])
    except Exception as exc:
        ferr = str(exc)
        print(f"sec fail: {exc}")
    last_filing = state.get("last_filing") or cfg.get("last_known_filing")
    if filing and last_filing and filing > last_filing:
        ir_text = ""
        try:
            ir_text = visible(fetch(cfg["sources"]["ir"]))
        except Exception as exc:
            print(f"ir fail: {exc}")
        parsed = parse_earnings(ir_text) if ir_text else {}
        fingerprint = json.dumps(parsed, sort_keys=True)
        if fingerprint != state.get("earnings_fingerprint"):
            signals = []
            missing = []
            china = parsed.get("china_yoy")
            gm = parsed.get("gm")
            fy28 = parsed.get("fy28_growth")
            if china is None:
                missing.append("중국")
            elif china > th["china_single_digit_floor"]:
                signals.append(f"중국 {china:.1f}% (한 자릿수보다 양호)")
            if fy28 is True:
                signals.append("FY2028 매출 증가 문구")
            elif fy28 is None:
                missing.append("FY2028")
            if gm is None:
                missing.append("매출총이익률")
            elif gm >= th["gm_buy"]:
                signals.append(f"매출총이익률 {gm:.1f}%")
            invalidate = []
            if china is not None and china <= th["china_break"]:
                invalidate.append(f"중국 {china:.1f}%")
            eps_hi = parsed.get("eps_hi")
            if eps_hi is not None and eps_hi < base["adj_eps_fy27_lo"]:
                invalidate.append(f"조정 EPS 상단 {eps_hi:.2f}")
            na = parsed.get("na_yoy")
            na_txt = f"북미 {na:.1f}%" if na is not None else "북미 확인 불가"
            if invalidate:
                alerts.append(
                    (
                        "[NKE] 실적 | 30달러 지지 약화",
                        body(
                            stamp,
                            f"새 공시 {filing}. 약화: {', '.join(invalidate)}. 파싱 실패: {', '.join(missing) or '없음'}. {na_txt}.",
                            "30달러 분할 관심을 끄세요. 원문에서 중국과 EPS를 확인하세요.",
                            "다음 분기 공시 전엔 이 판정을 유지.",
                            "약화 조건이 원문과 맞으면 보유 재검토.",
                            "파싱 실패 항목을 충족으로 세기. 주가 반응으로 조건 충족 단정.",
                        ),
                        5,
                    )
                )
            elif len(signals) >= int(cfg["buy_flip_need"]):
                alerts.append(
                    (
                        "[NKE] 실적 | 매수 전환 조건",
                        body(
                            stamp,
                            f"새 공시 {filing}. 충족 {len(signals)}/{cfg['buy_flip_need']}: {', '.join(signals)}. {na_txt}.",
                            "가격이 32달러 이하일 때만 분할 관심을 다시 보세요. 한 번에 넣지 마세요.",
                            "미파싱 항목은 IR 원문으로 채우기 전 충족으로 세지 마세요.",
                            "32달러 위면 조건이 켜져도 추격하지 마세요.",
                            "2개 문구를 실적 확정으로 단정. 신용, 미수.",
                        ),
                        4,
                    )
                )
            else:
                alerts.append(
                    (
                        "[NKE] 실적 | 전환 조건 미충족",
                        body(
                            stamp,
                            f"새 공시 {filing}. 충족 {len(signals)}/3. 확인 불가: {', '.join(missing) or '없음'}. {na_txt}.",
                            "관망 유지. 미파싱은 미충족이 아니라 확인 불가입니다.",
                            "IR 원문에서 중국, FY2028, 매출총이익률을 직접 대조.",
                            "이번 알림으로 사지 마세요.",
                            "확인 불가를 조건 충족이나 정상으로 취급.",
                        ),
                        3,
                    )
                )
            state["earnings_fingerprint"] = fingerprint
            state["last_filing"] = filing
            state["last_parsed"] = parsed
    elif ferr and state.get("fetch_fail_date") != today.isoformat() and today.weekday() == 0:
        alerts.append(
            (
                "[NKE] 공시 조회 실패 | 실적 조건 미확인",
                body(
                    stamp,
                    f"SEC 조회 실패. {ferr}. 실적 조건은 미확인입니다.",
                    "investors.nike.com 최신 실적을 직접 보세요.",
                    "다음 월요일에 재시도합니다.",
                    "이번 알림으로 비중을 바꾸지 마세요.",
                    "조회 실패를 조건 미충족으로 두기.",
                ),
                4,
            )
        )
        state["fetch_fail_date"] = today.isoformat()

    topic = resolve_topic("port", cfg.get("ntfy_topic"))
    server = resolve_server(cfg.get("ntfy_server"))
    for title, msg, pri in alerts:
        print(title)
        print(msg)
        print(f"topic={topic} send={not dry}")
        if not dry:
            publish(topic, title, msg, pri, ["chart_with_upwards_trend"], server)
    if not alerts:
        print("no alert")

    state["updated_at"] = now.isoformat()
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
