#!/usr/bin/env python3
"""FIG periodic watch: price zones and Monday status. Q3 scoring separate if needed."""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
CFG_PATH = ROOT / "configs" / "fig.json"
STATE_PATH = ROOT / "state" / "fig.json"
UA = {"User-Agent": "cwally-alerts fig research monitor research@example.com"}


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


def quote(url: str) -> float | None:
    data = json.loads(fetch(url))
    result = (data.get("chart") or {}).get("result") or []
    if not result:
        return None
    price = (result[0].get("meta") or {}).get("regularMarketPrice")
    try:
        return float(price) if price is not None else None
    except (TypeError, ValueError):
        return None


def latest_sec_date(url: str) -> str | None:
    data = json.loads(fetch(url))
    recent = ((data.get("filings") or {}).get("recent") or {})
    forms = recent.get("form") or []
    dates = recent.get("filingDate") or []
    for form, day in zip(forms, dates):
        if form in {"8-K", "10-Q", "10-K"}:
            return day
    return None


def zone_of(price: float, split: float, chase: float) -> str:
    if price <= split:
        return "split"
    if price >= chase:
        return "chase"
    return "fair"


def body(stamp: str, status: str, action: str, later: str, trim: str, avoid: str) -> str:
    return "\n".join(
        [
            f"{stamp} | FIG | 변경조건 점검",
            "이게 뭐 알림: 피그마 가격 구간과 변경 조건만 보는 주기 알림입니다. 매수 지시가 아닙니다. Q3 실적 채점은 별도.",
            f"지금 결과/상태: {status}",
            f"지금 할 일: {action}",
            f"나중에: {later}",
            f"익절/줄일 때: {trim}",
            f"하지 말 것: {avoid}",
        ]
    )


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    dry = mode == "dry" or os.environ.get("FIG_DRY") == "1"
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    today = now.date()
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    topic = resolve_topic("port", cfg.get("ntfy_topic"))
    server = resolve_server(cfg.get("ntfy_server"))
    base = cfg["baseline"]
    levels = cfg["levels"]

    if mode == "confirm":
        msg = body(
            stamp,
            f"설치 확인. 기준일 {base['price_date']} 종가 {base['price']}달러. 분할 관심 {levels['split']}달러 이하, 추격 금지 {levels['chase']}달러 이상. 선행 PER 약 {base['fwd_pe']}배는 프리미엄.",
            "평일 09:00 KST morning에서만 돕니다. 구간이 바뀌거나 월요일이면 보냅니다. 그 외는 침묵입니다.",
            "상향 조건은 Q3 매출 380M+ 비트와 가이던스 상향, NDR 유지. 하향은 미스와 가이던스 하향. 실적은 11/4 창.",
            "27달러 이상은 추격 금지. 보유분은 이 알림이 모릅니다.",
            "이 설치 확인을 매수·매도 신호로 읽기. 조회 실패를 조건 충족으로 세기.",
        )
        title = "[FIG] 설치 확인 | 매매 신호 아님"
        print(title)
        print(msg)
        if not dry:
            publish(topic, title, msg, 3, ["white_check_mark"], server)
        return 0

    state = load_json(
        STATE_PATH,
        {
            "updated_at": None,
            "last_price": None,
            "zone": "fair",
            "last_weekly": None,
            "last_filing": base.get("last_known_filing"),
            "filing_alerted": None,
            "fetch_fail_date": None,
        },
    )
    if today.isoformat() > cfg["valid_until"] and mode not in {"force", "dry"}:
        print(f"after {cfg['valid_until']}; silent")
        return 0

    price = None
    qerr = None
    try:
        price = quote(cfg["sources"]["quote"])
    except Exception as exc:
        qerr = str(exc)
    print(f"price={price} qerr={qerr}")

    alerts: list[tuple[str, str, int]] = []
    if price is None:
        if state.get("fetch_fail_date") != today.isoformat() or mode == "force":
            alerts.append(
                (
                    "[FIG] 주가 조회 실패 | 구간 미확인",
                    body(
                        stamp,
                        f"주가 조회 실패. {qerr or '빈 응답'}. 20달러 이탈이 아닙니다.",
                        "증권 앱에서 FIG 종가를 직접 보세요. 이 알림으로 구간을 판정하지 마세요.",
                        "다음 평일 09:00 KST에 재시도합니다.",
                        "이번 알림으로 사지 말고 줄이지 마세요.",
                        "조회 실패를 20달러 이탈이나 정상으로 취급.",
                    ),
                    4,
                )
            )
            state["fetch_fail_date"] = today.isoformat()
    else:
        zone = zone_of(price, levels["split"], levels["chase"])
        prev = state.get("zone") or "fair"
        changed = zone != prev
        weekly = today.weekday() == 0 and state.get("last_weekly") != today.isoformat()
        if changed or weekly or mode == "force":
            if zone == "split":
                title = "[FIG] 20달러 이하 | 분할만, 일괄 금지"
                action = "장기 관심 구간에 가깝습니다. 새 돈은 나누고, 11/4 실적 전 한 번에 넣지 마세요."
            elif zone == "chase":
                title = "[FIG] 27달러 이상 | 추격 금지"
                action = "단기 저항 구간에 가깝습니다. 신규 추격 금지. 보유분은 이 알림이 모릅니다."
            else:
                title = "[FIG] 20~27달러 | 프리미엄 유지, 일괄 금지"
                action = "저평가가 아닙니다. 관망이거나 이미 둔 분할만. 일괄 매수 금지."
            why = "구간 변경" if changed else "월요일 상태"
            if changed and weekly:
                why = "구간 변경, 월요일 상태"
            alerts.append(
                (
                    title,
                    body(
                        stamp,
                        f"FIG {price:.2f}달러. 기준일 {base['price_date']} {base['price']}달러, 52주 {base['low_52w']}~{base['high_52w']}. 구간 {prev} -> {zone}. {why}. 선행 PER 약 {base['fwd_pe']}배.",
                        action,
                        "상향은 Q3 380M+ 비트와 가이던스 상향. 하향은 미스와 가이던스 하향. 실적은 11/4 창.",
                        "27달러 이상이면 추격하지 마세요. 20달러 복귀는 손절 신호가 아닙니다.",
                        "목표가 30달러를 안전마진으로 쓰기. 신용, 미수, 실적 전 일괄 매수.",
                    ),
                    4 if zone != "fair" else 3,
                )
            )
            state["zone"] = zone
            state["last_price"] = price
            if weekly or mode == "force":
                state["last_weekly"] = today.isoformat()
        else:
            state["last_price"] = price
            state["zone"] = zone
            print(f"price zone unchanged ({zone}); silent")

    filing = None
    ferr = None
    try:
        filing = latest_sec_date(cfg["sources"]["sec"])
    except Exception as exc:
        ferr = str(exc)
        print(f"sec fail: {exc}")
    last_filing = state.get("last_filing") or base.get("last_known_filing")
    if filing and last_filing and filing > last_filing and state.get("filing_alerted") != filing:
        alerts.append(
            (
                "[FIG] 새 공시 | 실적 점수는 원문",
                body(
                    stamp,
                    f"SEC 8-K/10-Q/10-K {filing}. 이전 {last_filing}. 매출, NDR, AI 기여는 이 알림이 채점하지 않습니다.",
                    "IR 원문에서 Q3 매출과 가이던스를 보세요. 11/4 창.",
                    "상향은 380M+ 비트와 가이던스 상향. 하향은 미스와 하향.",
                    "공시 제목만 보고 비중을 바꾸지 마세요.",
                    "이 알림의 공시 감지를 실적 확정으로 읽기.",
                ),
                4,
            )
        )
        state["last_filing"] = filing
        state["filing_alerted"] = filing
    elif ferr and state.get("fetch_fail_date") != today.isoformat() and today.weekday() == 0:
        alerts.append(
            (
                "[FIG] 공시 조회 실패 | 변경조건 미확인",
                body(
                    stamp,
                    f"SEC 조회 실패. {ferr}. 변경 조건은 미확인입니다.",
                    "investor.figma.com 최신 실적을 직접 보세요.",
                    "다음 월요일에 재시도합니다.",
                    "이번 알림으로 비중을 바꾸지 마세요.",
                    "조회 실패를 가이던스 유지로 읽기.",
                ),
                4,
            )
        )
        state["fetch_fail_date"] = today.isoformat()

    for title, msg, pri in alerts:
        print(title)
        print(msg)
        print(f"topic={topic} send={not dry}")
        if not dry:
            publish(topic, title, msg, pri, ["chart_with_upwards_trend"], server)
    if not alerts:
        print("no alert")

    state["updated_at"] = now.isoformat()
    if mode != "dry":
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
