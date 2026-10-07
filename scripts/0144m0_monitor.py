#!/usr/bin/env python3
"""0144M0 one-shot verdict. Silent outside 2026-10-07 and in later years."""
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
CFG_PATH = ROOT / "configs" / "0144m0.json"
STATE_PATH = ROOT / "state" / "0144m0.json"
UA = {"User-Agent": "cwally-alerts 0144m0 research monitor"}
FIRE_DAY = "2026-10-07"


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


def quote(url: str) -> tuple[float | None, float | None]:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read().decode("utf-8", errors="ignore"))
    price = data.get("closePrice") or data.get("now") or data.get("tradePrice")
    rate = data.get("fluctuationsRatio") or data.get("changeRate")
    try:
        price_f = float(str(price).replace(",", "")) if price is not None else None
    except (TypeError, ValueError):
        price_f = None
    try:
        rate_f = float(str(rate).replace(",", "").replace("%", "")) if rate is not None else None
    except (TypeError, ValueError):
        rate_f = None
    return price_f, rate_f


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    dry = mode == "dry" or os.environ.get("UAM0144_DRY") == "1"
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    state = load_json(STATE_PATH, {"sent": False, "updated_at": None, "last_title": None})
    now = datetime.now(KST)
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    if now.year != int(cfg.get("year_guard") or 2026):
        print(f"year {now.year}; silent")
        return 0
    if now.date().isoformat() != FIRE_DAY and mode not in {"force", "dry"}:
        print(f"not {FIRE_DAY}; silent")
        return 0
    if state.get("sent") and mode not in {"force", "dry"}:
        print("already sent; silent")
        return 0

    base = cfg["baseline"]
    live, rate = None, None
    qerr = None
    try:
        live, rate = quote(cfg["sources"]["quote"])
    except Exception as exc:
        qerr = str(exc)
    print(f"live={live} rate={rate} qerr={qerr}")

    if live is None:
        px = f"기준 {base['price_date']} 종가 {base['close']:,}원({base['day_change_pct']:+.2f}%). 발송 시점 재조회는 붙지 않음."
    else:
        chg = f", 등락 {rate:+.2f}%" if rate is not None else ""
        px = f"발송 시점 {live:,.0f}원{chg}. 기준 {base['price_date']} 종가 {base['close']:,}원."

    title = "[0144M0] 매력 낮음 | 핵심배분 금지"
    msg = "\n".join(
        [
            f"{stamp} | 0144M0 | KODEX 미국드론UAM TOP10 분석 전달",
            "이게 뭐 알림: 2026-10-07 분석의 결론만 보내는 일회 알림입니다. 매수 지시가 아닙니다.",
            (
                f"지금 결과/상태: 투자매력 낮음. {px} 52주 고가 {base['high_52w']:,}원 대비 약 -50%. "
                f"순자산 약 178억 원, 총보수 약 {base['expense_pct']}%. "
                "JOBY 23.8%, ACHR 15.6%, BETA 11.4%로 개발사 비중이 절반 이상. "
                "JOBY·ACHR는 2026-10-06 기준 1년 약 -69%, -63%이고 분기 현금 소진은 약 2억 달러로 전해짐. FAA 상업 운항 일자는 미확정."
            ),
            "지금 할 일: 핵심 배분에서 빼세요. 이미 가진 물량이 있으면 총금융자산의 아주 작은 한도만 남기세요.",
            "나중에: JOBY 또는 ACHR의 FAA 형식증명 핵심 단계와 상업 운항 일정이 문서로 확정되거나, 대규모 희석 없이 현금 런웨이가 2년 이상으로 재확인되면 다시 보세요. 순자산 500억 원 이상도 조건입니다.",
            "익절/줄일 때: 인증 지연이나 추가 희석 공시가 나오면 위성 비중도 줄이세요. 52주 저점 근접은 매수 신호가 아닙니다.",
            "하지 말 것: 고점 대비 하락만 보고 추격 매수. 신용, 미수. 이 알림을 매수 권유로 읽기.",
        ]
    )
    topic = resolve_topic("port", cfg.get("ntfy_topic"))
    server = resolve_server(cfg.get("ntfy_server"))
    print(title)
    print(msg)
    print(f"topic={topic} send={not dry}")
    if not dry:
        publish(topic, title, msg, 3, ["warning"], server)
        state["sent"] = True
        state["last_title"] = title
    state["updated_at"] = now.isoformat()
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
