#!/usr/bin/env python3
"""133690 change-condition watch. Silent unless zone change or Monday."""
from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
CFG_PATH = ROOT / "configs" / "133690.json"
STATE_PATH = ROOT / "state" / "133690.json"
UA = {"User-Agent": "cwally-alerts 133690 research monitor research@example.com"}


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


def fetch(url: str, opener: urllib.request.OpenerDirector | None = None) -> str:
    req = urllib.request.Request(url, headers=UA)
    client = opener or urllib.request.build_opener()
    with client.open(req, timeout=25) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def yahoo_opener() -> urllib.request.OpenerDirector:
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())
    try:
        fetch("https://fc.yahoo.com", opener)
    except Exception:
        pass
    return opener


def quote(url: str) -> tuple[float | None, str | None]:
    data = json.loads(fetch(url))
    result = (data.get("chart") or {}).get("result") or []
    if not result:
        return None, "quote empty"
    price = (result[0].get("meta") or {}).get("regularMarketPrice")
    try:
        return (float(price) if price is not None else None), None
    except (TypeError, ValueError):
        return None, "price parse"


def forward_pe(url: str) -> tuple[float | None, str | None]:
    opener = yahoo_opener()
    crumb = fetch("https://query1.finance.yahoo.com/v1/test/getcrumb", opener).strip()
    if not crumb or crumb.startswith("<"):
        return None, "crumb empty"
    data = json.loads(fetch(f"{url}&crumb={urllib.parse.quote(crumb)}", opener))
    result = (data.get("quoteSummary") or {}).get("result") or []
    if not result:
        return None, "pe empty"
    stats = result[0].get("defaultKeyStatistics") or {}
    raw = (stats.get("forwardPE") or {}).get("raw")
    try:
        if raw is None:
            return None, "forwardPE empty"
        return float(raw), None
    except (TypeError, ValueError):
        return None, "pe parse"


def zone_of(price: float | None, pe: float | None, up_px: float, low_px: float, pe_cut: float) -> str:
    flags = []
    if pe is not None and pe <= pe_cut:
        flags.append("up_pe")
    if price is not None and price <= up_px:
        flags.append("up_price")
    if price is not None and price <= low_px:
        flags.append("down_low")
    return "+".join(flags) if flags else "mid"


def body(stamp: str, status: str, action: str, later: str, trim: str, avoid: str) -> str:
    return "\n".join(
        [
            f"{stamp} | 133690 | 변경조건 점검",
            "이게 뭐 알림: TIGER 미국나스닥100 변경조건만 보는 알림입니다. 매수나 매도 지시가 아닙니다.",
            f"지금 결과/상태: {status}",
            f"지금 할 일: {action}",
            f"나중에: {later}",
            f"익절/줄일 때: {trim}",
            f"하지 말 것: {avoid}",
        ]
    )


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    topic = resolve_topic("port", cfg.get("ntfy_topic"))
    server = resolve_server(cfg.get("ntfy_server"))
    levels = cfg["levels"]
    base = cfg["baseline"]
    if mode == "install":
        publish(
            topic,
            "[133690] 설치 확인 | 매매 신호 아님",
            body(
                stamp,
                f"평일 09:00 KST morning 슬롯에 붙였습니다. 기준가 {base['price_date']} {base['price']:,.0f}원. 상향 가격 {levels['upgrade_price']:,.0f}원, 저점대 {levels['low_zone']:,.0f}원, QQQ 선행 PER {levels['forward_pe_upgrade']:.0f}배 이하.",
                "설치 확인만 하세요. 사지 마세요.",
                "월요일 상태, 구간 변경만 보냅니다. 실적 추정 상승과 AI 자본지출은 확인하지 않습니다.",
                "이번 알림으로 비중을 바꾸지 마세요.",
                "설치 확인을 매수나 매도 신호로 읽기.",
            ),
            3,
            ["information_source"],
            server,
        )
        print("install confirmation sent")
        return 0

    dry = mode == "dry" or os.environ.get("NDQ133690_DRY") == "1"
    state = load_json(
        STATE_PATH,
        {
            "updated_at": None,
            "last_price": None,
            "last_pe": None,
            "zone": None,
            "last_weekly": None,
            "fetch_fail_date": None,
        },
    )
    today = now.date()
    if today.isoformat() > cfg["valid_until"] and mode not in {"force", "dry"}:
        print(f"after {cfg['valid_until']}; silent")
        return 0

    price, qerr = None, None
    pe, perr = None, None
    try:
        price, qerr = quote(cfg["sources"]["quote"])
    except Exception as exc:
        qerr = str(exc)
    try:
        pe, perr = forward_pe(cfg["sources"]["pe"])
    except Exception as exc:
        perr = str(exc)
    print(f"price={price} qerr={qerr} pe={pe} perr={perr}")

    alerts: list[tuple[str, str, int]] = []
    if price is None and pe is None:
        if state.get("fetch_fail_date") != today.isoformat():
            alerts.append(
                (
                    "[133690] 시세 조회 실패 | 조건 미확인",
                    body(
                        stamp,
                        f"가격과 선행 PER을 읽지 못했습니다. {qerr or perr or '빈 응답'}. 조건 미충족이 아닙니다.",
                        "증권 앱에서 133690 전일 종가를 직접 보세요. 이 알림으로 구간을 판정하지 마세요.",
                        "다음 평일 09:00 KST에 재시도합니다.",
                        "이번 알림으로 줄이거나 사지 마세요.",
                        "조회 실패를 저점 이탈이나 정상으로 취급.",
                    ),
                    4,
                )
            )
            state["fetch_fail_date"] = today.isoformat()
    else:
        zone = zone_of(
            price,
            pe,
            levels["upgrade_price"],
            levels["low_zone"],
            levels["forward_pe_upgrade"],
        )
        prev = state.get("zone") or "mid"
        changed = zone != prev and state.get("zone") is not None
        first = state.get("zone") is None
        weekly = today.weekday() == 0 and state.get("last_weekly") != today.isoformat()
        if changed or weekly or mode == "force" or (first and mode == "force"):
            px = f"{price:,.0f}원" if price is not None else "가격 미확인"
            pe_txt = f"QQQ 선행 PER {pe:.2f}" if pe is not None else "선행 PER 미확인"
            gap = None if price is None else (price / base["price"] - 1) * 100
            gap_txt = "기준가 대비 미확인" if gap is None else f"기준가 대비 {gap:+.1f}%"
            if "down_low" in zone:
                title = "[133690] 52주 저가 이탈 | 하향 가격조건"
                action = "가격 조건만 충족입니다. AI 자본지출과 반도체 이익은 직접 확인하세요. 매도 지시가 아닙니다."
                pri = 5
            elif "up_pe" in zone and "up_price" in zone:
                title = "[133690] PER 20 이하와 10% 조정 | 상향 후보"
                action = "두 가격성 조건이 같이 왔습니다. 실적 추정 상승은 미확인이라 추가 전에 원자료를 보세요."
                pri = 4
            elif "up_pe" in zone:
                title = "[133690] 선행 PER 20 이하 | 상향 후보"
                action = "밸류 조건만 충족입니다. 가격이 기준가 근처면 일괄매수로 읽지 마세요."
                pri = 4
            elif "up_price" in zone:
                title = "[133690] 기준가 -10% | 가격조건만"
                action = "10% 조정 조건만 충족입니다. 실적 추정 상승은 이 알림이 확인하지 않았습니다."
                pri = 4
            else:
                title = "[133690] 변경조건 밖 | 관망"
                action = "상향·하향 가격조건이 아닙니다. 비중을 바꾸지 마세요."
                pri = 3
            why = "구간 변경" if changed else "월요일 상태" if weekly else "수동 확인"
            alerts.append(
                (
                    title,
                    body(
                        stamp,
                        f"133690 {px}. {gap_txt}. {pe_txt}. 구간 {prev} -> {zone}. {why}. 기준 {base['price_date']} {base['price']:,.0f}원.",
                        action,
                        "상향은 선행 PER 20 이하, 또는 166,757원 이하와 실적 추정 상승. 하향 가격조건은 153,650원 이하.",
                        "166,757원 아래도 실적 추정 확인 전 추격 금지. 153,650원 이탈은 재검토이지 전량매도 지시 아님.",
                        "PER 대용을 지수 공식 선행 PER로 단정. 조회 실패를 조건 충족으로 세기. 신용, 미수.",
                    ),
                    pri,
                )
            )
            state["zone"] = zone
            state["last_price"] = price
            state["last_pe"] = pe
            if weekly:
                state["last_weekly"] = today.isoformat()
        else:
            state["last_price"] = price
            state["last_pe"] = pe
            if state.get("zone") is None:
                state["zone"] = zone
            print(f"zone unchanged ({zone}); silent")

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
