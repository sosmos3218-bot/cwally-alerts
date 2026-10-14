#!/usr/bin/env python3
"""446770 ACE 글로벌반도체TOP4 Plus: 6/22 고점 종가 대비 -30/-35/-40% 3단계 분할 알림.

morning 슬롯(평일 09:00 KST). 직전 거래일 완료 종가만 쓴다. 단계는 처음 도달할 때 한 번만 알린다.
2, 3차는 SK하이닉스 3분기 영업이익 관문(configs/port.json 컨센서스, -15%)을 통과해야 유지된다.
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
NAME = "446770"
CFG_PATH = ROOT / "configs" / f"{NAME}.json"
PORT_CFG_PATH = ROOT / "configs" / "port.json"
STATE_PATH = ROOT / "state" / f"{NAME}.json"
TIER_LABEL = {1: "1차", 2: "2차", 3: "3차"}


def load_state() -> dict:
    base = {
        "updated_at": None,
        "last_day": None,
        "last_close": None,
        "max_tier": 0,
        "gate": "wait",
        "gate_op_trn": None,
        "gate_title": None,
        "fetch_fail_date": None,
    }
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


def check_gate(cfg: dict, state: dict) -> tuple[str, float | None, str | None, list[str]]:
    """returns (gate, op, notice_title, info_lines). 이미 keep/cancel이면 고정."""
    port = json.loads(PORT_CFG_PATH.read_text(encoding="utf-8"))
    cons = float(port.get("hynix_op_consensus_trn") or 78.8)
    miss = float(port.get("op_miss_cancel_pct") or -15)
    if state.get("gate") in ("keep", "cancel"):
        return state["gate"], state.get("gate_op_trn"), state.get("gate_title"), [f"하이닉스 3분기 관문 판정 끝: {state['gate']}."]
    try:
        from port_monitor import cfg_period_start, latest_notice_op

        if cfg_period_start() != cfg["gate"]["period_start"]:
            return "wait", None, None, ["3분기 판정 기간이 지났는데 공시를 못 찾음. 수동 확인 필요."]
        op, title = latest_notice_op("000660")
    except Exception as exc:  # noqa: BLE001
        return "wait", None, None, [f"실적 공시 조회 실패: {exc}"]
    if op is None:
        return "wait", None, None, [f"하이닉스 3분기 잠정실적 아직 없음. 컨센서스 {cons:.1f}조."]
    gap = (op / cons - 1.0) * 100.0
    gate = "cancel" if gap <= miss else "keep"
    return gate, op, title, [f"하이닉스 3분기 영업이익 {op:.1f}조, 컨센서스 {cons:.1f}조 대비 {gap:+.1f}% (기준 {miss:.0f}%)."]


def tier_actions(cfg: dict, gate: str, upto: int) -> list[str]:
    out = []
    for i, t in enumerate(cfg["tiers"], start=1):
        if i > upto:
            break
        if not t["gated"]:
            out.append(f"{TIER_LABEL[i]} {t['price']:,}원 이하: 주식 슬리브 {t['weight_pct']}% 분할 검토.")
        elif gate == "keep":
            out.append(f"{TIER_LABEL[i]} {t['price']:,}원 이하: 실적 통과, {t['weight_pct']}% 분할 검토.")
        elif gate == "cancel":
            out.append(f"{TIER_LABEL[i]} {t['price']:,}원 이하: 실적 미달로 취소. 사지 않음.")
        else:
            out.append(f"{TIER_LABEL[i]} {t['price']:,}원 이하: 하이닉스 실적 확인 전 보류.")
    return out


def body(stamp: str, status: str, action: str, later: str, trim: str, avoid: str) -> str:
    return "\n".join(
        [
            f"{stamp} | 446770 | ACE 글로벌반도체TOP4 Plus",
            "이게 뭐 알림: 6/22 고점 종가 104,849원 대비 -30%, -35%, -40% 3단계 분할 알림. 목표 비중 주식 슬리브 6%(단계마다 2%). 자동 주문 아님.",
            f"지금 결과/상태: {status}",
            f"지금 할 일: {action}",
            f"나중에: {later}",
            f"익절/줄일 때: {trim}",
            f"하지 말 것: {avoid}",
        ]
    )


TRIM = "반도체 합산(삼성전자+하이닉스+ACE)이 주식 슬리브 50%를 넘으면 ACE부터 줄임."
AVOID = "한 번에 6% 다 사기, 다음 단계 가격 전에 미리 채우기, 신용 미수."


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    today = now.strftime("%Y%m%d")
    topic = resolve_topic("port", cfg.get("ntfy_topic"))
    server = resolve_server(cfg.get("ntfy_server"))
    lv = " / ".join(f"{t['pct']}% {t['price']:,}원" for t in cfg["tiers"])

    if mode == "install":
        day, close, err = last_close(cfg["code"], today)
        now_txt = f"직전 종가 {close:,.0f}원({day}), 고점 대비 {(close / cfg['peak']['close'] - 1) * 100:+.1f}%." if close else f"종가 조회 실패({err})."
        publish(
            topic,
            "[446770] 설치 확인 | 매매 신호 아님",
            body(
                stamp,
                f"평일 09:00 KST morning 슬롯에 붙였습니다. 단계: {lv}. {now_txt}",
                "설치 확인만 하세요. 이 알림으로 사지 마세요.",
                "새 단계에 처음 닿을 때와 하이닉스 3분기 실적(10/27~28 예정) 판정 때만 옵니다.",
                TRIM,
                "설치 확인을 매수 신호로 읽기.",
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
                "[446770] 종가 조회 실패 | 조건 미확인",
                body(stamp, f"종가 조회 실패: {err}. 단계 판정 안 함.", "증권 앱에서 446770 전일 종가를 직접 보세요.",
                     "다음 평일 09:00에 다시 봅니다.", "이번 알림으로 비중을 바꾸지 마세요.", "조회 실패를 단계 도달이나 정상으로 읽기."),
                3,
            ))
            state["fetch_fail_date"] = today
    else:
        peak = float(cfg["peak"]["close"])
        dd = (close / peak - 1) * 100
        tier_now = sum(1 for t in cfg["tiers"] if close <= t["price"])
        prev_gate = state.get("gate") or "wait"
        gate, op, ntitle, ginfo = check_gate(cfg, state)
        print(f"tier_now={tier_now} max_tier={state.get('max_tier')} gate={prev_gate}->{gate} {ginfo}")
        status = f"직전 종가 {close:,.0f}원({day}), 고점 대비 {dd:+.1f}%. " + " ".join(ginfo)

        if gate != prev_gate and gate in ("keep", "cancel"):
            reached = int(state.get("max_tier") or 0)
            if gate == "keep":
                title = "[446770] 하이닉스 실적 관문 통과 | 2, 3차 유지"
                action = "2, 3차 가격대 유지. 이미 지나친 단계는 지금 가격이 그 단계 가격 이하일 때만 채움." if reached >= 2 else "2, 3차 가격대 유지. 해당 가격에 닿을 때 분할."
            else:
                title = "[446770] 하이닉스 실적 -15% 하회 | 2, 3차 취소"
                action = "2, 3차 취소. 1차만 유지하고 추가 금지."
            alerts.append((title, body(stamp, status, action, "이후에는 새 단계 도달 때만 알림.", TRIM, AVOID), 4))
            state["gate"], state["gate_op_trn"], state["gate_title"] = gate, op, ntitle

        if tier_now > int(state.get("max_tier") or 0):
            t = cfg["tiers"][tier_now - 1]
            acts = tier_actions(cfg, gate, tier_now)
            if t["gated"] and gate == "wait":
                tail = f"{TIER_LABEL[tier_now]}는 하이닉스 실적 확인 전 보류"
            elif t["gated"] and gate == "cancel":
                tail = f"{TIER_LABEL[tier_now]} 취소 상태, 추가 금지"
            else:
                tail = f"{TIER_LABEL[tier_now]} 2% 분할 검토"
            title = f"[446770] 고점 {t['pct']}% 도달 | {tail}"
            nxt = cfg["tiers"][tier_now]["price"] if tier_now < len(cfg["tiers"]) else None
            later = (f"다음 단계 {nxt:,}원. " if nxt else "마지막 단계입니다. 더 내려가도 추가 단계 없음. ") + "하이닉스 실적 10/27~28 예정."
            alerts.append((title, body(stamp, status, " ".join(acts), later, TRIM, AVOID), 4))
            state["max_tier"] = tier_now
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
