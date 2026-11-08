#!/usr/bin/env python3
"""LG CNS vs Samsung SDS price-switch watch. No new cron.

Sends when the 2026-10-08 switch flips, or on Monday. Silent otherwise.
3Q26 earnings were checked on 10/30 (lgcnsq3 retired 2026-10-31). Price zones moved from lgcns to the 064400 engine extra_lines (2026-10-26).
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic, route_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
CFG_PATH = ROOT / "configs" / "cnsds.json"
STATE_PATH = ROOT / "state" / "cnsds.json"


def load_cfg() -> dict:
    return json.loads(CFG_PATH.read_text(encoding="utf-8"))


def load_state() -> dict:
    base = {"verdict": None, "cns": None, "sds": None, "last_alert_date": None, "updated_at": None}
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
    """Latest completed KRX regular-session close via krx_close (Daum, else Naver 15:30 bar).

    Naver trend closePrice mixes NXT and after-hours prints, so it is not used (2026-11-08).
    """
    import krx_close

    bars = krx_close.completed(krx_close.regular_bars(code, 5), datetime.now(KST))
    return bars[-1].close if bars else None


def verdict(cns: float, sds: float, cfg: dict) -> str:
    sds_hit = sds <= float(cfg["sds_cut"])
    cns_hit = cns >= float(cfg["cns_cut"])
    if sds_hit and cns_hit:
        return "BOTH"
    if sds_hit:
        return "SDS"
    if cns_hit:
        return "SHRINK"
    return "CNS"


COPY = {
    "CNS": (
        "가격 할인 유지",
        "LG CNS를 가격 기준으로 둡니다. 사업 질 우위가 확인된 것은 아닙니다.",
        "삼성SDS가 17.5만원 이하이거나 LG CNS가 8만원 이상이면 다시 봅니다.",
        "34.9%를 확정 수익률로 읽지 마세요. GPU 5000원 하한은 전력비가 빠진 가정입니다.",
    ),
    "SDS": (
        "삼성SDS로 전환",
        "삼성SDS가 17.5만원 이하입니다. 상대 선택은 삼성SDS로 바꿉니다.",
        "LG CNS 3분기 실적은 기존 알림을 기다리세요.",
        "동일 주가 비교만으로 실제 보유를 바꾸지 마세요.",
    ),
    "SHRINK": (
        "LG CNS 여력 축소",
        "LG CNS가 8만원 이상입니다. 기준 목표가까지 여력이 줄었습니다. 신규 보류.",
        "실적 상향 전 추가매수 금지.",
        "컨센서스 괴리만 보고 추격하지 마세요.",
    ),
    "BOTH": (
        "양쪽 조건 충족",
        "삼성SDS는 싸고 LG CNS 여력은 줄었습니다. 신규는 삼성SDS 쪽만 검토.",
        "가격이 다시 선을 벗어나면 재평가합니다.",
        "한 알림으로 양쪽을 동시에 사지 마세요.",
    ),
}


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
        send(cfg, "[CNSDS] 064400/018260 설치 확인 | 매매 신호 아님", [
            "이게 뭐 알림: LG씨엔에스와 삼성SDS 가격 전환 알림 설치 확인. 매매 신호 아님.",
            "지금 결과/상태: 평일 15:20 기존 슬롯. LG CNS 8만원 미만이고 삼성SDS 17.5만원 초과면 가격 할인 유지.",
            "지금 할 일: 없음. 선이 바뀔 때와 월요일에만 옵니다.",
            "나중에: 가격 구간과 계획 선은 [064400].",
            "익절/줄일 때: 이 설치 알림으로 비중을 바꾸지 마세요.",
            "하지 말 것: 설치 확인을 매수 신호로 읽기. GPU 5000원 하한을 확정으로 읽기.",
        ], 3)
        print("install confirmation sent")
        return 0
    dry = mode == "dry"
    if now.year != int(cfg["year_guard"]) and not dry:
        print("year-guard; silent")
        return 0
    state = load_state()
    try:
        cns = last_close(cfg["cns"])
        sds = last_close(cfg["sds"])
        err = "빈 응답"
    except Exception as exc:
        cns = sds = None
        err = type(exc).__name__
    if not cns or not sds:
        title = "[CNSDS] 064400/018260 시세 조회 실패 | 판단 유보"
        lines = [
            "이게 뭐 알림: LG씨엔에스 대 삼성SDS 전환 알림의 운영 안내.",
            f"지금 결과/상태: 시세 조회 실패({err}). {now:%Y-%m-%d %H:%M} KST.",
            "지금 할 일: 비중을 바꾸지 마세요.",
            "나중에: 다음 평일 15:20에 다시 봅니다.",
            "익절/줄일 때: 조회 실패만으로 줄이지 마세요.",
            "하지 말 것: 없는 가격으로 전환을 단정.",
        ]
        if dry:
            print(title, *lines, sep="\n")
            return 0
        if state.get("last_alert_date") != now.strftime("%Y-%m-%d"):
            send(cfg, title, lines, 3)
            state["last_alert_date"] = now.strftime("%Y-%m-%d")
            save_state(state, now)
        return 0
    v = verdict(cns, sds, cfg)
    label, todo, later, dont = COPY[v]
    today = now.strftime("%Y-%m-%d")
    changed = state.get("verdict") not in (None, v)
    weekly = now.weekday() == 0
    send_now = mode == "force" or ((changed or weekly) and state.get("last_alert_date") != today)
    title = f"[CNSDS] 064400/018260 {label} | CNS {cns/10000:.1f}만 SDS {sds/10000:.1f}만"
    lines = [
        f"이게 뭐 알림: LG씨엔에스와 삼성SDS 가격 전환 알림. {'조건이 바뀌었습니다.' if changed else '월요일 요약.'} 투자 권유 아님.",
        f"지금 결과/상태: {now:%Y-%m-%d %H:%M} KST LG CNS {cns:,.0f}원, 삼성SDS {sds:,.0f}원. 선은 8만원과 17.5만원. 기준일 {cfg['as_of']}.",
        f"지금 할 일: {todo}",
        f"나중에: {later}",
        "익절/줄일 때: 8만원 접근 또는 3분기 하향이 겹치면 LG CNS 신규를 줄이세요.",
        f"하지 말 것: {dont}",
    ]
    if dry:
        print(title, *lines, sep="\n")
        print(f"would_send={send_now} verdict={v}")
        return 0
    if send_now:
        send(cfg, title, lines, 4 if changed else 3)
        state["last_alert_date"] = today
    else:
        print(f"no switch; silent verdict={v} cns={cns} sds={sds}")
    state["verdict"] = v
    state["cns"] = cns
    state["sds"] = sds
    save_state(state, now)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
