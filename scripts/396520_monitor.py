#!/usr/bin/env python3
"""396520 TIGER China Semiconductor FactSet: drawdown from 2026-10-12 base 11345. Morning slot."""
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
CFG_PATH = ROOT / "configs" / "396520.json"
STATE_PATH = ROOT / "state" / "396520.json"
UA = {"User-Agent": "cwally-alerts 396520 research monitor"}


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


def body(stamp: str, status: str, action: str, later: str, trim: str, avoid: str) -> str:
    return "\n".join(
        [
            f"{stamp} | 396520 | 중국 반도체 테마 점검",
            "이게 뭐 알림: TIGER 차이나반도체FACTSET 기준가 대비 낙폭 점검. 1~3년 테마 보유용. 매매 신호 아님.",
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

    if mode == "install":
        publish(
            topic,
            "[396520] 설치 확인 | 매매 신호 아님",
            body(
                stamp,
                "평일 09:00 KST morning 슬롯에 붙였습니다. 기준가 2026-10-12 11,345원. -30% 약 7,941원, -40% 약 6,807원.",
                "설치 확인만 하세요. 사지 마세요.",
                "평일 09:00에 낙폭 점검. 지정학 뉴스는 별도 확인.",
                "이번 알림으로 비중을 바꾸지 마세요.",
                "설치 확인을 매수나 매도 신호로 읽기.",
            ),
            3,
            ["information_source"],
            server,
        )
        print("install confirmation sent")
        return 0

    dry = mode == "dry" or os.environ.get("CHINA_SEMI_DRY") == "1"
    state = load_json(
        STATE_PATH,
        {
            "updated_at": None,
            "last_price": None,
            "zone": "normal",
            "last_alert_date": None,
            "sent_dates": [],
            "fetch_fail_date": None,
        },
    )
    today = now.date().isoformat()

    base_price = float(cfg["baseline"]["price"])
    levels = cfg["levels"]
    price, qerr = None, None
    try:
        price, qerr = quote(cfg["sources"]["quote"])
    except Exception as exc:
        qerr = str(exc)
    print(f"price={price} qerr={qerr}")

    alerts = []
    if price is None:
        if state.get("fetch_fail_date") != today:
            alerts.append(
                (
                    "[396520] 주가 조회 실패 | 조건 미확인",
                    body(
                        stamp,
                        f"주가 조회 실패. {qerr or '빈 응답'}. 낙폭 미확인입니다.",
                        "증권 앱에서 396520 전일 종가를 직접 확인하세요.",
                        "다음 평일 09:00 KST에 재시도합니다.",
                        "이번 알림으로 줄이거나 사지 마세요.",
                        "조회 실패를 낙폭이나 정상으로 취급.",
                    ),
                    4,
                )
            )
            state["fetch_fail_date"] = today
    else:
        pct = (price / base_price - 1) * 100
        zone = "normal"
        if price <= levels["loss40"]:
            zone = "loss40"
        elif price <= levels["loss30"]:
            zone = "loss30"
        prev = state.get("zone") or "normal"
        changed = zone != prev
        if changed or mode == "force":
            if zone == "loss40":
                title = "[396520] 기준 대비 -40% | 테마 재점검"
                action = "비중 재검토. 지정학·자립 진전 확인. 추가 금지."
            elif zone == "loss30":
                title = "[396520] 기준 대비 -30% | 점검"
                action = "낙폭 허용 구간 도달. 테마 유지 여부 재확인. 추가 신중."
            else:
                title = "[396520] 기준 대비 정상 | 보유 유지"
                action = "현재 구간 유지. 추가 없음."
            alerts.append(
                (
                    title,
                    body(
                        stamp,
                        f"현재 {price:.0f}원. 기준 11,345원 대비 {pct:+.1f}%. 구간 {prev} -> {zone}.",
                        action,
                        "1~3년 테마. 지정학 뉴스·자립 진전 지속 확인.",
                        "-30% 이하 추가 신중, -40% 이하 비중 재검토. 매도 지시 아님.",
                        "낙폭 터치를 즉시 매도나 추가매수 신호로 쓰기.",
                    ),
                    5 if zone == "loss40" else 4 if zone == "loss30" else 3,
                )
            )
            state["zone"] = zone
            state["last_price"] = price
            state["last_alert_date"] = today
        else:
            state["last_price"] = price
            print(f"zone unchanged ({zone}); silent")

    for title, msg, pri in alerts:
        print(title)
        print(msg)
        if not dry:
            publish(topic, title, msg, pri, ["chart_with_downwards_trend"], server)

    if not alerts:
        print("no alert")

    state["updated_at"] = now.isoformat()
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
