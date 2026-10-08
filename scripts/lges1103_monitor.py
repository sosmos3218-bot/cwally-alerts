#!/usr/bin/env python3
"""LG Energy Solution 2026-11-03 confirmed Q3 print. One-shot, year-guarded."""
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
CFG_PATH = ROOT / "configs" / "lges1103.json"
STATE_PATH = ROOT / "state" / "lges1103.json"
DISCLOSURES = "https://www.lgensol.com/kr/investors/electronic-disclosure"


def read_state() -> dict:
    if not STATE_PATH.exists():
        return {"result_notified": False, "empty_notified": False, "last_problem_key": None}
    return json.loads(STATE_PATH.read_text(encoding="utf-8"))


def save_state(state: dict, now: datetime) -> None:
    state["updated_at"] = now.isoformat(timespec="seconds")
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def send(cfg: dict, title: str, lines: list[str]) -> None:
    publish(
        resolve_topic("port", cfg.get("ntfy_topic")), title, "\n".join(lines), 4,
        ["chart_with_upwards_trend"], resolve_server(cfg.get("ntfy_server")),
    )


def eok(text: str, pattern: str) -> int | None:
    hit = re.search(pattern, text, re.I)
    if not hit:
        return None
    raw = int(hit.group(1).replace(",", ""))
    unit = hit.group(2)
    if unit.startswith("백만"):
        raw = raw // 100
    if not -200000 < raw < 300000:
        return None
    return raw


def grab(text: str, label: str) -> int | None:
    return eok(text, label + r"[^0-9\-]{0,24}(-?[\d,]+)\s*(억원|백만원)")


def is_final(item: dict) -> bool:
    title = item.get("title", "")
    body = item.get("text", "")
    blob = title + " " + body
    if not re.search(r"엘지에너지솔루션|LG에너지솔루션|에너지솔루션", blob):
        return False
    if not re.search(r"2026[\.\-/ ]*3분기|2026-07-01|2026\.07\.01|'?26\.3Q|3분기", blob):
        return False
    return bool(re.search(r"실적|기업설명회|컨퍼런스|영업이익", title + body[:400]))


def parse(item: dict) -> dict:
    text = item.get("title", "") + "\n" + item.get("text", "")
    op = grab(text, r"영업이익")
    sales = grab(text, r"매출액")
    net = grab(text, r"당기순이익|순이익")
    ampc = grab(text, r"AMPC|세액공제")
    comp = grab(text, r"보상금|위약금|최소구매")
    ev = grab(text, r"전기차|EV")
    ess = grab(text, r"ESS|에너지저장")
    ocf = grab(text, r"영업활동현금흐름|영업현금흐름")
    ex = None
    if op is not None and ampc is not None:
        ex = op - ampc
    return {
        "sales": sales, "op": op, "net": net, "ampc": ampc, "comp": comp,
        "ev": ev, "ess": ess, "ocf": ocf, "ex": ex,
    }


def fmt(v: int | None) -> str:
    return "확인 불가" if v is None else f"{v:+,}억원"


def verdict(p: dict, prelim: dict) -> tuple[str, str, str]:
    ex, comp, op = p["ex"], p["comp"], p["op"]
    if ex is None or op is None:
        return (
            "판정 유보",
            "보상금, 사업부 손익, 순이익, 현금흐름을 확정 숫자로 분리하지 못했습니다.",
            "유지, 상향, 철회 중 하나를 고르지 마세요. 공시 원문 확인 전까지 비중을 바꾸지 마세요.",
        )
    base_op = int(prelim["op_eok"])
    if ex < 0 or op < int(base_op * 0.7):
        return (
            "철회",
            "AMPC 제외 흑자 또는 잠정 영업이익 유지 조건이 깨졌습니다.",
            "조건부 분할매수를 철회하세요. 신규 매수를 멈추고 보유 비중을 재평가하세요.",
        )
    if comp is not None and (ex - comp) >= 2000 and op >= int(base_op * 0.9):
        return (
            "상향",
            "보상금을 빼도 AMPC 제외 영업이익이 2,000억원 이상이고 잠정 영업이익의 90% 이상입니다.",
            "조건부 분할매수를 상향하세요. 그래도 한 번에 비중을 채우지 마세요.",
        )
    missing = []
    if comp is None:
        missing.append("보상금 금액")
    if p["ev"] is None or p["ess"] is None:
        missing.append("사업부 손익")
    if p["net"] is None:
        missing.append("순이익")
    if p["ocf"] is None:
        missing.append("현금흐름")
    gap = ", ".join(missing) if missing else "보상금 차감 후 2,000억 미만"
    return (
        "유지",
        f"AMPC 제외 흑자는 남았지만 {gap} 때문에 상향 조건은 아닙니다.",
        "조건부 분할매수를 유지하세요. 확인되지 않은 2027년 이익을 선반영하지 마세요.",
    )


def problem(cfg: dict, state: dict, now: datetime, kind: str, detail: str, dry: bool) -> None:
    key = f"{kind}:{detail}"
    if state.get("last_problem_key") == key:
        print(f"problem already reported: {kind}")
        return
    title = f"[LGESF] LG엔솔 확정 {kind} | 판정 유보"
    lines = [
        "이게 뭐 알림: LG에너지솔루션 2026년 3분기 확정 실적과 컨퍼런스콜 판정.",
        f"지금 결과/상태: {kind}. {detail}. {now:%Y-%m-%d %H:%M} KST. 공시: {DISCLOSURES}",
        "지금 할 일: 유지, 상향, 철회를 고르지 마세요. 원문 확인 전까지 매매를 보류하세요.",
        "나중에: 같은 날 16:32 KST 원샷이 남아 있으면 그때 다시 확인합니다.",
        "익절/줄일 때: 자료 누락만으로 보유 비중을 바꾸지 마세요.",
        "하지 말 것: 잠정 숫자나 증권사 추정을 확정 실적으로 쓰지 마세요.",
    ]
    if dry:
        print(title, *lines, sep="\n")
        return
    send(cfg, title, lines)
    state["last_problem_key"] = key
    save_state(state, now)


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    if mode not in {"auto", "dry", "force"}:
        raise SystemExit(f"unknown mode: {mode}")
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    state = read_state()
    now = datetime.now(KST)
    day = now.strftime("%Y-%m-%d")
    if now.year != int(cfg["year_guard"]) or not cfg["window_start"] <= day <= cfg["window_end"]:
        print("outside 2026 one-shot window; silent")
        return 0
    if state.get("result_notified") and mode != "force":
        print("result already notified; silent")
        return 0
    try:
        rows = notices(cfg["ticker"], 30)
    except Exception as exc:
        problem(cfg, state, now, "조회 실패", type(exc).__name__, mode == "dry")
        return 0
    matches = [r for r in rows if is_final(r) and "20261103" in r.get("datetime", "").replace("-", "")[:8] or (
        is_final(r) and r.get("datetime", "")[:10] in {"2026-11-03", "2026.11.03", "20261103"}
    )]
    if not matches:
        matches = [r for r in rows if is_final(r) and "잠정" not in r.get("title", "")]
    if not matches:
        last = now.hour > 16 or (now.hour == 16 and now.minute >= 30)
        if last and not state.get("empty_notified"):
            problem(cfg, state, now, "미게시", "11월 3일 확정 실적 공시를 찾지 못했습니다", mode == "dry")
            if mode != "dry":
                state["empty_notified"] = True
                save_state(state, now)
        else:
            print("final disclosure not posted; silent")
        return 0
    item = matches[0]
    p = parse(item)
    label, why, action = verdict(p, cfg["prelim"])
    title = f"[LGESF] LG엔솔 3Q 확정 {label}"
    lines = [
        "이게 뭐 알림: 373220 2026년 3분기 확정 실적에서 보상금, 사업부 손익, 순이익, 현금흐름을 분리합니다.",
        (
            f"지금 결과/상태: 판정 {label}. {why} "
            f"매출 {fmt(p['sales'])}, 영업이익 {fmt(p['op'])}, AMPC {fmt(p['ampc'])}, "
            f"AMPC 제외 {fmt(p['ex'])}, 보상금 {fmt(p['comp'])}, EV {fmt(p['ev'])}, ESS {fmt(p['ess'])}, "
            f"순이익 {fmt(p['net'])}, 영업현금흐름 {fmt(p['ocf'])}. "
            f"잠정은 매출 96,434억원, 영업이익 7,560억원, AMPC 제외 3,391억원. "
            f"공시 {item.get('id')}. 조회 {now:%Y-%m-%d %H:%M} KST. {DISCLOSURES}"
        ),
        f"지금 할 일: {action}",
        "나중에: 4분기 AMPC 제외 흑자와 ESS 누적 수주 60GWh는 이 알림 범위 밖입니다.",
        "익절/줄일 때: 철회면 신규 매수를 멈추고, 유지·상향이어도 한 번에 채우지 마세요.",
        "하지 말 것: 확인 불가 칸을 추정으로 채우거나 잠정 7,560억원을 확정 이익으로 바꾸지 마세요.",
    ]
    if mode == "dry":
        print(title, *lines, "dry-run; no ntfy", sep="\n")
        return 0
    send(cfg, title, lines)
    state["result_notified"] = True
    state["last_key"] = item.get("id")
    state["verdict"] = label
    state["last_problem_key"] = None
    save_state(state, now)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
