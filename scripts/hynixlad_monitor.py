#!/usr/bin/env python3
"""HYNIXLAD SK하이닉스 분할매수 사다리 소진(145만 원 아래)과 7월 저점(132.2만 원) 이탈 알림.

configs/port.json dca.hynix 사다리의 정지 신호. 매수 신호 아님. morning 슬롯(평일 09:00 KST).
직전 거래일 완료 종가만 쓴다. 구간이 바뀔 때만 알리고, 위로 회복은 경계선 +3%에서 판정한다.
modes: auto(기본) | dry(발송 없음, state 안 씀) | install(설치 확인 1회, state 안 씀)
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic  # noqa: E402
import naver_api  # noqa: E402

KST = timezone(timedelta(hours=9))
NAME = "hynixlad"
CFG_PATH = ROOT / "configs" / f"{NAME}.json"
STATE_PATH = ROOT / "state" / f"{NAME}.json"


def load_state() -> dict:
    base = {"updated_at": None, "last_day": None, "last_close": None, "zone": 0, "fetch_fail_date": None}
    if STATE_PATH.exists():
        try:
            base.update(json.loads(STATE_PATH.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass
    return base


def last_close(code: str, today: str) -> tuple[str | None, float | None, str | None]:
    try:
        rows = naver_api.stock_trend(code, 10)
    except Exception as exc:  # noqa: BLE001
        return None, None, str(exc)
    done = [r for r in rows if r.get("day") and r["day"] < today and r.get("close")]
    if not done:
        return None, None, "완료 종가 없음"
    top = max(done, key=lambda r: r["day"])
    return top["day"], float(top["close"]), None


def next_zone(prev: int, close: float, exhaust: float, july: float, buf: float) -> int:
    raw = 2 if close < july else 1 if close < exhaust else 0
    if raw >= prev:
        return raw
    z = prev
    bound = {2: july, 1: exhaust}
    while z > 0 and close >= bound[z] * (1 + buf / 100):
        z -= 1
    return z


def body(stamp: str, status: str, action: str, later: str, trim: str, avoid: str) -> str:
    return "\n".join(
        [
            f"{stamp} | HYNIXLAD | SK하이닉스 사다리 점검",
            "이게 뭐 알림: port.json 하이닉스 분할매수 사다리(1차 180만~190만, 2차 160만~170만, 3차 145만~155만)가 끝났는지와 7월 저점 이탈을 알리는 정지 신호. 매수 신호 아님.",
            f"지금 결과/상태: {status}",
            f"지금 할 일: {action}",
            f"나중에: {later}",
            f"익절/줄일 때: {trim}",
            f"하지 말 것: {avoid}",
        ]
    )


LATER = "10/27~28 하이닉스 3분기 실적과 외국인 수급(HBMCHK)을 본 뒤 사다리를 다시 짭니다."
TRIM = "1차 매수가 -20% 조각만 절반 감액(port.json 규칙). 주식 슬리브 19% 목표 비중을 넘기지 않음."
AVOID = "4차를 새로 만들어 물타기, 신용 미수, 하루 급락에 전량 매도."


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    today = now.strftime("%Y%m%d")
    topic = resolve_topic("port", cfg.get("ntfy_topic"))
    server = resolve_server(cfg.get("ntfy_server"))
    ex = float(cfg["levels"]["exhaust"])
    jl = float(cfg["levels"]["july_low"])
    buf = float(cfg.get("recover_buffer_pct") or 3)

    if mode == "install":
        day, close, err = last_close(cfg["code"], today)
        now_txt = f"직전 종가 {close:,.0f}원({day})." if close else f"종가 조회 실패({err})."
        publish(
            topic,
            "[HYNIXLAD] 설치 확인 | 매매 신호 아님",
            body(
                stamp,
                f"평일 09:00 KST morning 슬롯에 붙였습니다. 경계선 145만 원(사다리 소진), 132.2만 원(7월 저점). {now_txt}",
                "설치 확인만 하세요. 이 알림으로 사거나 팔지 마세요.",
                "구간이 바뀔 때만 옵니다. 위로 회복은 경계선 +3%에서 판정.",
                TRIM,
                "설치 확인을 매매 신호로 읽기.",
            ),
            3,
            ["information_source"],
            server,
        )
        print("install confirmation sent")
        return 0

    end = cfg.get("ends_kst")
    if end and now > datetime.strptime(end, "%Y-%m-%d %H:%M").replace(tzinfo=KST) + timedelta(minutes=59):
        print(f"ended at {end}; silent")
        return 0

    dry = mode == "dry"
    state = load_state()
    day, close, err = last_close(cfg["code"], today)
    print(f"day={day} close={close} err={err}")
    alerts: list[tuple[str, str, int]] = []

    if close is None:
        if state.get("fetch_fail_date") != today:
            alerts.append((
                "[HYNIXLAD] 종가 조회 실패 | 조건 미확인",
                body(stamp, f"종가 조회 실패: {err}. 구간 판정 안 함.", "증권 앱에서 SK하이닉스 전일 종가를 직접 보세요.",
                     "다음 평일 09:00에 다시 봅니다.", "이번 알림으로 비중을 바꾸지 마세요.", "조회 실패를 정상이나 이탈로 읽기."),
                3,
            ))
            state["fetch_fail_date"] = today
    else:
        prev = int(state.get("zone") or 0)
        z = next_zone(prev, close, ex, jl, buf)
        print(f"zone {prev}->{z}")
        status = f"직전 종가 {close:,.0f}원({day}). 145만 원 대비 {(close / ex - 1) * 100:+.1f}%, 7월 저점 대비 {(close / jl - 1) * 100:+.1f}%."
        if z != prev:
            if z == 2:
                title, action, pri = "[HYNIXLAD] 7월 저점 132.2만 이탈 | 추가 금지, 위험 재점검", "신규 추가 금지. 보유 비중을 유지할지 실적과 외국인 수급으로 다시 판단하세요.", 5
            elif z == 1 and prev == 0:
                title, action, pri = "[HYNIXLAD] 145만 원 아래 | 사다리 소진, 추가 보류", "1~3차를 다 채웠다면 더 사지 마세요. 3차를 못 채웠다면 실적 확인 뒤에만 채우세요.", 4
            elif z == 1:
                title, action, pri = "[HYNIXLAD] 7월 저점 위 회복 | 추가는 계속 보류", "회복만으로 추가하지 마세요. 사다리는 실적 뒤 다시 짭니다.", 3
            else:
                title, action, pri = "[HYNIXLAD] 145만 원 위 회복 | 추격 금지", "기존 보유 유지. 회복을 따라 추가 매수하지 마세요.", 3
            alerts.append((title, body(stamp, status, action, LATER, TRIM, AVOID), pri))
            state["zone"] = z
        state["last_day"], state["last_close"] = day, close

    for title, msg, pri in alerts:
        print(title)
        print(msg)
        if not dry:
            publish(topic, title, msg, pri, ["chart_with_downwards_trend"], server)
    if not alerts:
        print("no alert")
    if dry:
        print("DRY RUN: no publish, state not written")
        return 0
    state["updated_at"] = now.isoformat()
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
