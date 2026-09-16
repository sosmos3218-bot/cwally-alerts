#!/usr/bin/env python3
from __future__ import annotations
import json, os, sys, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parents[1]
CFG_PATH = ROOT / "configs" / "ustsy.json"
STATE_PATH = ROOT / "state" / "ustsy.json"
ACTION_ZONES = {"TREND_1300", "LOW_1330", "BASE", "MEAN_1450", "HIGH_1520"}
ACTIONS = {
    "TREND_1300": "476760 <=20% 재검토. 평균회귀 소신 혜손",
    "LOW_1330": "476760 35-40% 확대 (484790에서 이전). 이탈분 절반",
    "BASE": "70:30 유지. 손대지 말 것",
    "MEAN_1450": "476760 25% 축소. 이탈분 절반",
    "HIGH_1520": "476760 20% 축소. 이상 확대 금지",
    "TRANSIT": "밴드 사이. 대기",
    "DATA_FAIL": "원달러 조회 실패",
}


def yahoo_fx(ticker: str):
    url = "https://query1.finance.yahoo.com/v8/finance/chart/" + urllib.parse.quote(ticker) + "?interval=1d&range=5d"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 cwally-alerts"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read().decode())
        meta = payload["chart"]["result"][0].get("meta", {})
        price = meta.get("regularMarketPrice") or meta.get("previousClose")
        return float(price) if price is not None else None
    except Exception as exc:
        print(exc, file=sys.stderr)
        return None


def zone(fx: float | None) -> str:
    if fx is None:
        return "DATA_FAIL"
    if fx <= 1300:
        return "TREND_1300"
    if fx <= 1330:
        return "LOW_1330"
    if 1360 <= fx <= 1430:
        return "BASE"
    if 1450 <= fx <= 1510:
        return "MEAN_1450"
    if fx >= 1520:
        return "HIGH_1520"
    return "TRANSIT"


def ntfy_publish(server: str, topic: str, title: str, message: str, priority: int, tags: str) -> None:
    payload = json.dumps({
        "topic": topic,
        "title": title,
        "message": message,
        "priority": priority,
        "tags": tags.split(","),
    }).encode()
    req = urllib.request.Request(
        server.rstrip("/"),
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        print(resp.read().decode())


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "event").lower()
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    topic = os.environ.get("NTFY_TOPIC") or cfg.get("ntfy_topic") or "cwally-port"
    server = os.environ.get("NTFY_SERVER") or cfg.get("ntfy_server") or "https://ntfy.sh"
    now = datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")
    prev = json.loads(STATE_PATH.read_text(encoding="utf-8")) if STATE_PATH.exists() else {}
    fx = yahoo_fx(cfg.get("fx_ticker") or "USDKRW=X")
    z = zone(fx)
    prev_z = prev.get("zone")
    STATE_PATH.write_text(
        json.dumps(
            {"updated_at": now, "last_mode": mode, "fx": fx, "zone": z if z != "DATA_FAIL" else prev_z},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    px = f"{fx:,.1f}" if fx is not None else "N/A"
    print(f"{now} | {mode} | USD/KRW {px} | {prev_z} -> {z}")

    if mode != "event":
        print(f"ustsy {mode} ignored; event only")
        return 0
    if z == "DATA_FAIL":
        print("ustsy fx fail; silent")
        return 0
    if z not in ACTION_ZONES:
        print("ustsy transit; silent")
        return 0
    if prev_z == z:
        print("ustsy same band; silent")
        return 0
    if not prev_z:
        print("ustsy baseline set; silent")
        return 0

    title = f"{cfg.get('title_prefix', '[USTSY]')} {prev_z}->{z}"
    lines = [
        f"{now} | event | USD/KRW {px}",
        f"{prev_z} -> {z}",
        ACTIONS[z],
        "",
        "기본 484790 70 / 476760 30",
        "조정은 이탈분 절반. 금리로 비중 갈이지 말 것.",
    ]
    pri = 5 if z == "TREND_1300" else 4
    ntfy_publish(server, topic, title, "\n".join(lines), pri, "warning,us")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
