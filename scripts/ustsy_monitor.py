#!/usr/bin/env python3
from __future__ import annotations
import json, sys, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from ntfy_util import route_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parents[1]
CFG_PATH = ROOT / "configs" / "ustsy.json"
STATE_PATH = ROOT / "state" / "ustsy.json"
ACTION_ZONES = {"TREND_1300", "LOW_1330", "BASE", "MEAN_1450", "HIGH_1520"}
ZONE_KO = {
    "TREND_1300": "원달러 1,300 아래. 헤지 실패 조짐",
    "LOW_1330": "원달러 1,330 아래. 비헤지 확대",
    "BASE": "원달러 1,360~1,430. 기본 비중",
    "MEAN_1450": "원달러 1,450~1,510. 비헤지 축소",
    "HIGH_1520": "원달러 1,520 위. 비헤지 더 축소",
    "TRANSIT": "밴드 사이. 대기",
    "DATA_FAIL": "원달러 조회 실패",
}
DO_NOW = {
    "TREND_1300": "비헤지(476760) 비중을 20% 이하로 줄이고, 평균회귀 소신을 다시 보세요.",
    "LOW_1330": "헤지(484790)에서 일부를 비헤지(476760) 35~40%로 옮기세요. 옮길 때는 이탈분의 절반만.",
    "BASE": "70:30을 그대로 두세요. 오늘 손대지 말세요.",
    "MEAN_1450": "비헤지(476760)를 25%로 줄이세요. 옮길 때는 이탈분의 절반만.",
    "HIGH_1520": "비헤지(476760)를 20%로 줄이세요. 이 이상 늘리지 말세요.",
    "TRANSIT": "밴드 사이입니다. 기다리세요.",
    "DATA_FAIL": "원달러를 다시 확인하세요.",
}
TITLE_BY_ZONE = {
    "TREND_1300": "헤지 실패 조짐 | 비헤지 20%이하",
    "LOW_1330": "비헤지 35~40%로",
    "BASE": "70:30 유지",
    "MEAN_1450": "비헤지 25%로",
    "HIGH_1520": "비헤지 20%로",
    "TRANSIT": "대기",
    "DATA_FAIL": "환율 확인",
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
        "topic": route_topic(topic, title),
        "title": title,
        "message": message,
        "priority": priority,
        "tags": tags.split(","),
    }).encode()
    req = urllib.request.Request(server.rstrip("/"), data=payload, method="POST", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        print(resp.read().decode())


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "event").lower()
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    topic = "cwally-port"
    server = "https://ntfy.sh"
    now = datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")
    prev = json.loads(STATE_PATH.read_text(encoding="utf-8")) if STATE_PATH.exists() else {}
    fx = yahoo_fx(cfg.get("fx_ticker") or "USDKRW=X")
    z = zone(fx)
    prev_z = prev.get("zone")
    STATE_PATH.write_text(
        json.dumps({"updated_at": now, "last_mode": mode, "fx": fx, "zone": z if z != "DATA_FAIL" else prev_z}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    px = f"{fx:,.1f}" if fx is not None else "없음"
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

    title = f"[USTSY] {TITLE_BY_ZONE.get(z, z)}"
    lines = [
        f"{now} | USTSY | 원달러 {px}",
        "이게 뭐 알림: 미국 30년국채 헤지(484790) 70% / 비헤지(476760) 30% 비중을 원달러로 조정할지 보는 알림입니다.",
        f"지금 상태: {ZONE_KO.get(prev_z, prev_z)} → {ZONE_KO.get(z, z)}",
        f"지금 할 일: {DO_NOW.get(z, '')}",
        "나중에: 다음 밴드로 이동할 때만 다시 조정합니다. 이율로 비중을 자주 갈이지 말세요.",
        "익절/줄일 때: 이 알림은 국채 ETF 매도 신호가 아닙니다. 헤지·비헤지 비중만 바꿉니다.",
        "하지 말 것: 이율만 보고 비중을 자주 바꾸기. 조정은 이탈분의 절반만. 엔화채·스트립채는 이 알림 범위 밖.",
    ]
    pri = 5 if z == "TREND_1300" else 4
    ntfy_publish(server, topic, title, "\n".join(lines), pri, "warning,us")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
