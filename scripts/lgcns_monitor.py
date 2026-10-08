#!/usr/bin/env python3
"""LG CNS 064400 price-zone watch. Earnings stay on lgcnsq3.

Slot: existing weekday 15:20 KST afternoon. Sends only on zone change,
Monday weekly, data failure, or install confirmation. Silent otherwise.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from naver_api import get_json, num  # noqa: E402
from ntfy_util import publish, resolve_server, resolve_topic, route_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
CFG_PATH = ROOT / "configs" / "lgcns.json"
STATE_PATH = ROOT / "state" / "lgcns.json"


def load_cfg() -> dict:
    return json.loads(CFG_PATH.read_text(encoding="utf-8"))


def load_state() -> dict:
    base = {"zone": None, "price": None, "last_alert_date": None, "updated_at": None}
    if STATE_PATH.exists():
        try:
            data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                base.update({k: data.get(k) for k in base})
        except json.JSONDecodeError:
            pass
    return base


def save_state(state: dict, now: datetime) -> None:
    state["updated_at"] = now.isoformat(timespec="seconds")
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(STATE_PATH)


def last_close(code: str) -> float | None:
    rows = get_json(f"https://m.stock.naver.com/api/stock/{code}/trend?pageSize=5")
    if not isinstance(rows, list):
        return None
    for row in rows:
        price = num((row or {}).get("closePrice"))
        if price:
            return price
    return None


def zone_of(price: float, zones: dict) -> str:
    if price < float(zones["deep"]):
        return "DEEP"
    if price < float(zones["split"]):
        return "SPLIT"
    if price < float(zones["nonew"]):
        return "NONEW"
    return "STOP"


COPY = {
    "DEEP": (
        "저가 분할",
        "52주 저점 근처입니다. 사려면 나눠서만. 실적 확인 전 몰아서 사지 마세요.",
        "3분기 영업이익률 8% 미만이면 이 구간도 보류로 바꿉니다.",
        "저가라고 비중을 한 번에 늘리지 마세요.",
    ),
    "SPLIT": (
        "분할 관찰",
        "현재 판단 구간입니다. 사려면 나눠서만. 발표 전 추격 금지.",
        "3분기 잠정은 lgcnsq3 알림을 기다리세요. 영업이익률 9% 회복과 대외 GPU 계약이 있어야 상향입니다.",
        "컨센서스 괴리만 보고 몰아 사지 마세요.",
    ),
    "NONEW": (
        "신규 보류",
        "컨센서스 하단 8만~9만에 들어왔습니다. 실적 상향 전 신규 매수 보류.",
        "목표가 괴리가 10% 안으로 줄면 추가매수 근거가 사라집니다.",
        "급등 추격, 신용/미수 금지.",
    ),
    "STOP": (
        "추가매수 금지",
        "9만 이상입니다. 실적 상향 없이 괴리가 줄었습니다. 보유만, 신규 금지.",
        "3분기 상향 조건(영업이익 컨센서스 초과, 이익률 9%)이 확인되기 전엔 늘리지 마세요.",
        "고점 추격 금지.",
    ),
}


def body(cfg: dict, price: float, z: str, peer: float | None, now: datetime, weekly: bool) -> tuple[str, list[str]]:
    label, todo, later, dont = COPY[z]
    man = price / 10000
    title = f"[LGCNS] {label} | {man:.1f}만"
    peer_txt = f"삼성SDS {peer:,.0f}원." if peer else "삼성SDS 시세 없음."
    kind = "월요일 주간 요약." if weekly else "가격 구간이 바뀌었습니다."
    lines = [
        f"이게 뭐 알림: LG씨엔에스 064400 가격 구간 알림. {kind} 투자 권유 아님.",
        f"지금 결과/상태: {now:%Y-%m-%d %H:%M} KST 종가 {price:,.0f}원, 구간 {label}. {peer_txt} 기준일 {cfg['as_of']}. 배수는 이 알림이 다시 계산하지 않습니다.",
        f"지금 할 일: {todo}",
        f"나중에: {later}",
        "익절/줄일 때: 9만 접근 또는 3분기 하향 판정이 겹치면 신규를 줄이세요. 보유 축소는 실적 알림을 본 뒤.",
        f"하지 말 것: {dont} GPU 계약은 공시로만 확인.",
    ]
    return title, lines


def send(cfg: dict, title: str, lines: list[str], priority: int = 3) -> None:
    topic = route_topic(resolve_topic("port", cfg.get("ntfy_topic")), title)
    publish(topic, title, "\n".join(lines), priority, ["chart_with_upwards_trend"], resolve_server(cfg.get("ntfy_server")))


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "slot").lower()
    if mode not in {"slot", "dry", "install", "force"}:
        raise SystemExit(f"unknown mode: {mode}")
    cfg = load_cfg()
    now = datetime.now(KST)
    if mode == "install":
        title = "[LGCNS] 설치 확인 | 매매 신호 아님"
        lines = [
            "이게 뭐 알림: LG씨엔에스 064400 가격 구간 알림 설치 확인. 매매 신호 아님.",
            "지금 결과/상태: 평일 15:20 기존 슬롯에 연결. 6만 미만 저가 분할, 6~8만 분할 관찰, 8~9만 신규 보류, 9만 이상 추가매수 금지.",
            "지금 할 일: 없음. 구간이 바뀔 때와 월요일에만 옵니다.",
            "나중에: 3분기 실적 판정은 기존 [LG] lgcnsq3 알림이 합니다.",
            "익절/줄일 때: 이 설치 알림으로 비중을 바꾸지 마세요.",
            "하지 말 것: 설치 확인을 매수 신호로 읽기.",
        ]
        if mode == "install":
            send(cfg, title, lines, 3)
            print("install confirmation sent")
            return 0
    dry = mode == "dry"
    if now.year != int(cfg["year_guard"]) and not dry:
        print("year-guard; silent")
        return 0
    state = load_state()
    try:
        price = last_close(cfg["ticker"])
    except Exception as exc:
        price = None
        err = type(exc).__name__
    else:
        err = "빈 응답"
    if not price:
        title = "[LGCNS] 시세 조회 실패 | 판단 유보"
        lines = [
            "이게 뭐 알림: LG씨엔에스 가격 구간 알림의 운영 안내.",
            f"지금 결과/상태: 시세 조회 실패({err}). {now:%Y-%m-%d %H:%M} KST.",
            "지금 할 일: 비중을 바꾸지 마세요.",
            "나중에: 다음 평일 15:20에 다시 봅니다.",
            "익절/줄일 때: 조회 실패만으로 줄이지 마세요.",
            "하지 말 것: 없는 가격으로 구간을 단정.",
        ]
        if dry:
            print(title, *lines, sep="\n")
            return 0
        if state.get("last_alert_date") != now.strftime("%Y-%m-%d"):
            send(cfg, title, lines, 3)
            state["last_alert_date"] = now.strftime("%Y-%m-%d")
            save_state(state, now)
        return 0
    z = zone_of(price, cfg["zones"])
    today = now.strftime("%Y-%m-%d")
    weekly = now.weekday() == 0
    changed = state.get("zone") not in (None, z)
    already = state.get("last_alert_date") == today
    send_now = mode == "force" or ((changed or weekly) and not already)
    peer = None
    if send_now or dry:
        try:
            peer = last_close(cfg["peer"])
        except Exception:
            peer = None
    title, lines = body(cfg, price, z, peer, now, weekly and not changed)
    if dry:
        print(title, *lines, sep="\n")
        print(f"would_send={send_now} zone={z}")
        return 0
    if send_now:
        send(cfg, title, lines, 4 if changed else 3)
        state["last_alert_date"] = today
    else:
        print(f"no zone change; silent zone={z} price={price}")
    state["zone"] = z
    state["price"] = price
    save_state(state, now)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
