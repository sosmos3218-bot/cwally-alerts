#!/usr/bin/env python3
"""[FIG-EARN] Figma Q3 2026 earnings reminder + checklist (approved in chat 2026-10-26).

  - 2026-11-04 (KST), morning 09:00 slot: reminder '내일 아침(미국 11/4 장 마감 후) FIG 3분기 실적 발표'
  - 2026-11-05 (KST) 08:00 one-shot (configs/figearn.json oneshot_kst, picked by oneshot_pick.py):
    checklist. The 11/5 09:00 morning run is the catch-up if 08:00 was missed.

Year guard (event_year) + date guard: silent on any other day. Each message is sent once
(state/figearn.json reminder_sent / checklist_sent), so the morning catch-up and late runs never repeat.
Modes: auto | force (send today's message even if already sent; still date-guarded) | dry (print only,
no ntfy, no state) | selftest. Numbers are not scored here (auto summary is [FIG-Q3] 09:05). Not investment advice.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
CFG = ROOT / "configs" / "figearn.json"
STATE = ROOT / "state" / "figearn.json"


def load_cfg() -> dict:
    return json.loads(CFG.read_text(encoding="utf-8"))


def load_state() -> dict:
    base = {"reminder_sent": None, "checklist_sent": None, "updated_at": None}
    if STATE.exists():
        try:
            base.update(json.loads(STATE.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass
    return base


def save_state(st: dict) -> None:
    st["updated_at"] = datetime.now(KST).isoformat(timespec="seconds")
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def due(cfg: dict, st: dict, now: datetime, force: bool = False) -> str | None:
    """Which message is due now: 'reminder' | 'checklist' | None (pure)."""
    if now.year != int(cfg["event_year"]):
        return None
    today = now.strftime("%Y-%m-%d")
    if today == cfg["reminder_date"] and (force or not st.get("reminder_sent")):
        return "reminder"
    if today == cfg["checklist_date"] and now.strftime("%H:%M") >= cfg.get("checklist_not_before", "08:00") and (force or not st.get("checklist_sent")):
        return "checklist"
    return None


def build(cfg: dict, kind: str, now: datetime) -> tuple[str, str, int]:
    stamp = f"{now:%Y-%m-%d %H:%M} KST | FIG | 피그마"
    if kind == "reminder":
        title = "[FIG-EARN] 내일 아침 3분기 실적 발표 | 실적 전 일괄 매수 금지"
        lines = [
            stamp,
            "이게 뭐 알림: " + cfg["explain"],
            "지금 결과/상태: 내일 아침(미국 11/4 장 마감 후) FIG 3분기 실적 발표.",
            "지금 할 일: 실적 전에는 새로 크게 사지 않기. 내일 08:00 KST 체크리스트로 숫자 확인.",
            "나중에: 내일 볼 것: " + ", ".join(cfg["checklist"]) + ".",
            "익절/줄일 때: 실적 숫자 확인 전에는 판단하지 않기.",
            "하지 말 것: 실적 전 일괄 매수, 신용/미수, 장전 시간외 가격 추격.",
        ]
        return title, "\n".join(lines), 3
    title = "[FIG-EARN] 3분기 실적 확인 | 체크리스트 6개"
    items = "; ".join(f"{i}) {x}" for i, x in enumerate(cfg["checklist"], 1))
    lines = [
        stamp,
        "이게 뭐 알림: " + cfg["explain"],
        "지금 결과/상태: 3분기 실적 확인: " + items + ".",
        f"지금 할 일: IR 보도자료에서 위 6개를 직접 확인. {cfg['sources']['ir']}",
        "나중에: 09:05 KST [FIG-Q3] 자동 요약과 대조. NDR 125% 미만이면 매수 논리 재검토.",
        "익절/줄일 때: 연간 가이던스 하향이나 NDR 125% 미만이면 추가 매수 멈추고 비중 재점검.",
        "하지 말 것: 헤드라인 숫자 하나만 보고 주문, 장전 시간외 가격 추격, 신용/미수.",
    ]
    return title, "\n".join(lines), 4


def selftest() -> int:
    cfg = load_cfg()
    fails = 0

    def check(label, got, want):
        nonlocal fails
        ok = got == want
        fails += not ok
        print(("ok  " if ok else "FAIL"), label, got, "" if ok else f"want {want}")

    E = {"reminder_sent": None, "checklist_sent": None}
    at = lambda s: datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=KST)  # noqa: E731
    check("10/26 silent", due(cfg, E, at("2026-10-26 09:00")), None)
    check("11/04 09:00 reminder", due(cfg, E, at("2026-11-04 09:00")), "reminder")
    check("11/04 late run still reminder", due(cfg, E, at("2026-11-04 13:00")), "reminder")
    check("11/04 already sent", due(cfg, {"reminder_sent": "x"}, at("2026-11-04 13:00")), None)
    check("11/05 07:59 not yet", due(cfg, E, at("2026-11-05 07:59")), None)
    check("11/05 08:00 checklist", due(cfg, E, at("2026-11-05 08:00")), "checklist")
    check("11/05 09:00 catch-up", due(cfg, {"reminder_sent": "x"}, at("2026-11-05 09:00")), "checklist")
    check("11/05 already sent", due(cfg, {"reminder_sent": "x", "checklist_sent": "y"}, at("2026-11-05 09:00")), None)
    check("11/06 silent", due(cfg, E, at("2026-11-06 09:00")), None)
    check("2027-11-04 year guard", due(cfg, E, at("2027-11-04 09:00")), None)
    for k in ("reminder", "checklist"):
        t, b, _ = build(cfg, k, at("2026-11-05 08:00"))
        check(f"{k} no middle dot / em-dash", any(ch in t + b for ch in "\u00b7\u2014"), False)
        print(t)
    print("selftest", "PASS" if not fails else f"FAIL {fails}")
    return 1 if fails else 0


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    if mode == "selftest":
        return selftest()
    dry = mode == "dry"
    cfg = load_cfg()
    now = datetime.now(KST)
    print(f"{now.isoformat(timespec='seconds')} figearn mode={mode}")
    if dry:
        print("DRY RUN (no ntfy, no state write)")
    st = load_state()
    kind = due(cfg, st, now, force=mode == "force")
    if not kind:
        print("not a FIG earnings date or already sent; silent")
        return 0
    title, msg, prio = build(cfg, kind, now)
    if dry:
        print(f"DRY RUN would send (p{prio}): {title}\n{msg}")
        return 0
    publish(resolve_topic("port"), title, msg, prio, tags=["calendar"])
    st[f"{kind}_sent"] = now.isoformat(timespec="seconds")
    save_state(st)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
