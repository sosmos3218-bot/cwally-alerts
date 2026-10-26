#!/usr/bin/env python3
"""Watch new LG Energy Solution exchange disclosures in the existing afternoon slot."""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from naver_api import notices  # noqa: E402
from ntfy_util import publish, resolve_server, resolve_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
CFG = ROOT / "configs" / "lges.json"
STATE = ROOT / "state" / "lges.json"


def category(item: dict) -> str | None:
    title = item.get("title", "").replace(" ", "")
    body = item.get("text", "")
    if "단일판매" in title and "공급계약" in title:
        return "공급계약 정정" if "정정" in title else "공급계약"
    if "계약해지" in title or "공급계약해지" in title:
        return "계약 해지"
    if "연결재무제표기준영업(잠정)실적" in title:
        if re.search(r"당기실적\s*2026-07-01\s*[~∼-]\s*2026-09-30|당기실적\s*\('?26\.3Q", body):
            return "3분기 실적 정정" if "정정" in title else None  # Existing lgesq3 one-shot owns the initial Q3 result.
        return "실적 공시"
    if ("풍문또는보도에대한해명" in title or "투자판단관련주요경영사항" in title) and re.search(
        r"ESS|에너지저장|얼티엄|GM|세액공제|AMPC|생산중단|가동", body, re.I
    ):
        return "사업 관련 해명"
    return None


def body_lines(cfg: dict, item: dict, label: str, now: datetime) -> list[str]:
    body = item.get("text", "")
    tags = []
    for word in ("ESS", "에너지저장", "GM", "얼티엄", "AMPC", "세액공제", "현금흐름"):
        if word.lower() in body.lower():
            tags.append(word)
    if "정정" in item.get("title", ""):
        tags.append("정정")
    clue = ", ".join(dict.fromkeys(tags)) or "수치 별도 확인"
    return [
        f"이게 뭐 알림: LG에너지솔루션 373220의 새 {label}을 확인했습니다.",
        (f"지금 결과/상태: {item['title']}. 관련 키워드: {clue}. 공시일 {item.get('datetime', '')[:10]}, "
         f"조회 {now:%Y-%m-%d %H:%M} KST. 공시 번호 {item['id']}. 원문 확인: {cfg['original_source']}"),
        "지금 할 일: 계약 규모와 정정 여부를 공시 원문에서 확인하세요. 확인 전 신규 매수 판단을 보류하세요.",
        "나중에: 연간 ESS 신규 수주 90GWh, AMPC 제외 손익, GM 생산 회복, 2027년 현금흐름을 별도 공식 자료로 확인하세요.",
        "익절/줄일 때: AMPC 제외 손실 확대, 수주 취소, 재가동 지연이 공식 확인되면 비중을 재평가하세요.",
        "하지 말 것: 계약금액을 GWh로 임의 환산하거나 이 공시 하나만으로 매수·매도 신호를 만들지 마세요.",
    ]


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    if mode not in {"auto", "dry"}:
        raise SystemExit(f"unknown mode: {mode}")
    cfg = json.loads(CFG.read_text(encoding="utf-8"))
    if not cfg.get("disclosure_alerts", True):
        # 2026-10-26: 373220 disclosures moved to [LGES] lgeswatch_monitor.py (KIND). No fetch, no ntfy, no state.
        print(f"lges disabled (disclosure_alerts=false): {cfg.get('disclosure_alerts_note', '')}")
        return 0
    state = json.loads(STATE.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    try:
        rows = notices(cfg["ticker"], 30)
        if not rows:
            raise ValueError("empty notice list")
    except Exception as exc:
        if state.get("last_fail_day") == now.strftime("%Y-%m-%d"):
            print("fetch failure already reported today")
            return 0
        title = "[LG] LG엔솔 공시 조회 실패 | 판단 유보"
        lines = [
            "이게 뭐 알림: LG에너지솔루션 신규 공시 확인.",
            f"지금 결과/상태: 조회 실패({type(exc).__name__}). {now:%Y-%m-%d %H:%M} KST.",
            "지금 할 일: 공시 원문을 확인하기 전 매매 판단을 보류하세요.",
            "나중에: 다음 평일 15:20 슬롯에서 다시 확인합니다.",
            "익절/줄일 때: 조회 실패만으로 비중을 바꾸지 마세요.",
            "하지 말 것: 빈 응답을 조건 미충족으로 취급하지 마세요.",
        ]
        if mode == "dry":
            print(title, *lines, "dry-run; no ntfy", sep="\n")
            return 0
        publish(resolve_topic("port"), title, "\n".join(lines), 4, ["warning"], resolve_server(cfg["ntfy_server"]))
        state["last_fail_day"] = now.strftime("%Y-%m-%d")
        state["updated_at"] = now.isoformat(timespec="seconds")
        STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return 0

    seen = set(state.get("seen_ids") or [])
    fresh = [
        item for item in reversed(rows)
        if item.get("datetime", "")[:10] >= cfg["start_date"] and item["id"] not in seen
    ]
    print(f"new notices={len(fresh)}, relevant={sum(category(item) is not None for item in fresh)}")
    if mode == "dry":
        for item in fresh:
            label = category(item)
            if label:
                print(f"[LG] LG엔솔 {label} | 원문 확인", *body_lines(cfg, item, label, now), sep="\n")
        print("dry-run; no ntfy, no state write")
        return 0
    for item in fresh:
        label = category(item)
        if label:
            publish(
                resolve_topic("port", cfg.get("ntfy_topic")),
                f"[LG] LG엔솔 {label} | 원문 확인",
                "\n".join(body_lines(cfg, item, label, now)),
                4, ["mag"], resolve_server(cfg["ntfy_server"]),
            )
        seen.add(item["id"])  # Only after successful send for a relevant item.
        state["seen_ids"] = sorted(seen)[-80:]
        state["updated_at"] = now.isoformat(timespec="seconds")
        STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
