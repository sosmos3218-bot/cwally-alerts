#!/usr/bin/env python3
"""CASH30 one-shot (2026-11-06 08:40 KST): where the remaining 30% cash goes.

Choices: keep waiting for NEE at 73-75 / NEE first tranche now / move to DUK / SO (capped) + DUK / stay cash.
Inputs:
  state/neeq3.json  call_stance, score   (10/21 NEE Q3 alert)
  state/soq3.json   score, guide, equity (11/5-11/6 SO Q3 alert)
  configs/cash30.json inputs.rate_targets (10/22 scheduled check: 12m targets at 10y 4.5/5.1/5.5)
                      inputs.so_verdict    (11/6 07:10 scheduled check: 실행/분할 일부만/보류)
  live: NEE/SO/DUK price, US 10y (^TNX)
Missing inputs fall back to rules and the alert says so.

Modes: auto | force | dry (print only, no ntfy, no state).
Local test: CASH30_TEST='{"px":{"NEE":76,"SO":90,"DUK":118},"ty":4.9}' python scripts/cash30_monitor.py dry
Not investment advice.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_topic

KST = timezone(timedelta(hours=9))
CFG = ROOT / "configs" / "cash30.json"
STATE = ROOT / "state" / "cash30.json"
UA = {"User-Agent": "Mozilla/5.0 cwally-alerts", "Accept-Encoding": "identity"}
SYMS = ("NEE", "SO", "DUK")


def fetch_json(url: str) -> dict:
    with urlopen(Request(url, headers=UA), timeout=25) as resp:
        return json.loads(resp.read().decode("utf-8", errors="ignore"))


def last_price(url: str) -> float | None:
    try:
        res = ((fetch_json(url).get("chart") or {}).get("result") or [None])[0]
        p = (res or {}).get("meta", {}).get("regularMarketPrice")
        return float(p) if p else None
    except Exception:
        return None


def read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def target_at(table: dict, ty: float) -> float | None:
    pts = sorted((float(k), float(v)) for k, v in (table or {}).items() if v is not None)
    if not pts:
        return None
    if ty <= pts[0][0]:
        return pts[0][1]
    if ty >= pts[-1][0]:
        return pts[-1][1]
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x0 <= ty <= x1:
            return y0 + (y1 - y0) * (ty - x0) / (x1 - x0)
    return None


def exp_ret(target: float | None, price: float | None, div: float) -> float | None:
    if target is None or not price:
        return None
    return (target / price - 1 + div / price) * 100


def decide(cfg: dict, nee_st: dict, so_st: dict, px: dict, ty: float | None) -> dict:
    b, inp = cfg["baseline"], cfg["inputs"]
    lo, hi = b["nee_zone"]
    divs = b["dividends"]
    tg = {s: target_at((inp.get("rate_targets") or {}).get(s), ty) if ty is not None else None for s in SYMS}
    er = {s: exp_ret(tg[s], px.get(s), divs[s]) for s in SYMS}
    has_tg = all(er[s] is not None for s in SYMS)
    notes = []

    # NEE status
    stance, nscore = nee_st.get("call_stance"), str(nee_st.get("score") or "")
    nee_out = []
    if stance in {"철회 언급", "요금동결 수용/추가 양보"}:
        nee_out.append(f"NEE 콜 판정 {stance}")
    if nscore.startswith("주의"):
        nee_out.append("NEE 실적 판정 주의")
    if px.get("NEE") and px["NEE"] < lo:
        nee_out.append(f"NEE {px['NEE']:.2f}, {lo} 이탈")
    if not nee_st.get("notified"):
        notes.append("NEE 실적 알림 기록 없음")
    in_zone = bool(px.get("NEE")) and lo <= px["NEE"] <= hi

    # SO status
    verdict = inp.get("so_verdict")
    sscore = str(so_st.get("score") or "")
    so_allowed = verdict != "보류" and not sscore.startswith("주의")
    if verdict is None:
        notes.append("SO 검증 결론 미기록, 알림 판정만 사용")
    if not has_tg:
        notes.append("금리 시나리오 목표가 미기록, 규칙만 사용")

    def redirect(why: str) -> dict:
        so_pick = False
        if so_allowed:
            if has_tg:
                so_pick = er["SO"] > er["DUK"]
            else:
                so_pick = ty is not None and ty <= 4.8
        if so_pick:
            cap = b["so_extra_cap_pp"]
            return {"key": "so_duk", "title": f"SO {cap} + DUK {30 - cap} | 2회 분할",
                    "todo": f"30% 중 SO {cap}%p, DUK {30 - cap}%p. 오늘 밤 절반, 1주 뒤 절반. SO 비중 상한 때문에 SO는 {cap}%p까지만.", "why": why}
        return {"key": "duk", "title": "DUK로 전환 | 30%p 2회 분할",
                "todo": "30%를 DUK로. 오늘 밤 15%p, 1주 뒤 15%p." + ("" if so_allowed else " SO는 추가 매수 보류 판정이라 제외."), "why": why}

    if ty is not None and ty >= b["rate_stress"]:
        res = {"key": "cash", "title": f"현금 유지 | 10년물 {ty:.2f}%", "todo": "30%는 파킹 유지. 10년물이 5.5% 아래로 내려오면 재판정.",
               "why": f"10년물 {ty:.2f}%로 유틸리티 밸류에이션 압박 구간({b['rate_stress']}% 이상)"}
    elif nee_out:
        res = redirect("NEE 대기 해제: " + ", ".join(nee_out))
    elif in_zone:
        if has_tg and er["NEE"] + b["edge_pp"] < max(er["SO"], er["DUK"]):
            res = redirect(f"NEE가 {lo}-{hi} 구간이지만 기대수익이 대안보다 {b['edge_pp']:.0f}%p 넘게 낮음")
        else:
            res = {"key": "nee_buy", "title": "NEE 1차 분할 | 10%p 매수", "todo": "30% 중 10%p만 NEE 매수. 나머지 20%p는 합병 결정 전까지 대기.",
                   "why": f"NEE {px['NEE']:.2f}, {lo}-{hi} 구간 진입, 콜 판정 {stance or '미확인'}"}
    else:
        if has_tg:
            er_zone = exp_ret(tg["NEE"], hi, divs["NEE"])
            best = max(er["SO"], er["DUK"])
            if er_zone is not None and er_zone >= best + b["edge_pp"]:
                res = {"key": "nee_wait", "title": "NEE 대기 유지 | 현금 30% 그대로", "todo": f"NEE {hi}달러 이하 지정가 대기. 그 전엔 파킹.",
                       "why": f"{hi}달러에 사면 기대수익 {er_zone:.1f}%로 대안 최고 {best:.1f}%보다 {b['edge_pp']:.0f}%p 이상 높음"}
            else:
                res = redirect(f"{hi}달러에 사도 기대수익 {er_zone:.1f}%로 대안 최고 {best:.1f}%와 차이 {b['edge_pp']:.0f}%p 미만")
        else:
            res = {"key": "nee_wait", "title": "NEE 대기 유지 | 현금 30% 그대로", "todo": f"NEE {hi}달러 이하 지정가 대기. 그 전엔 파킹.",
                   "why": f"NEE 대기 해제 조건(요금동결 수용, 철회, 주의, {lo} 이탈) 없음"}
    res.update({"er": er, "tg": tg, "notes": notes, "stance": stance, "so_verdict": verdict, "so_score": sscore})
    return res


def body(cfg: dict, now: datetime, res: dict, px: dict, ty: float | None) -> str:
    def f(v, fmt="{:.2f}"):
        return fmt.format(v) if v is not None else "-"
    rows = ", ".join(f"{s} {f(px.get(s))}(기대 {f(res['er'][s], '{:.1f}')}%)" for s in SYMS)
    status = (f"{res['title']}. 이유: {res['why']}. 10년물 {f(ty)}%. {rows}. "
              f"NEE 콜 판정 {res['stance'] or '-'}, SO 검증 {res['so_verdict'] or '-'}, SO 알림 {res['so_score'] or '-'}."
              + (" 참고: " + "; ".join(res["notes"]) + "." if res["notes"] else ""))
    return "\n".join([
        now.strftime("%Y-%m-%d %H:%M KST") + " | CASH30 | 30% 현금 배분",
        "이게 뭐 알림: " + cfg["explain"],
        "지금 상태: " + status,
        "지금 할 일: " + res["todo"],
        "나중에: NEE 합병 SCC 결정, 10년물 5.5% 돌파 시 재판정. 이 알림은 한 번만 옵니다.",
        "익절/줄일 때: 새로 산 종목이 매수가 -6% 종가 이탈하거나 가이던스를 내리면 그 몫부터 줄임.",
        "하지 말 것: 30%를 한 번에 투입, SO 합산 70% 초과, 신용/미수. 투자 권유 아님.",
    ])


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    dry = mode == "dry"
    cfg = json.loads(CFG.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    st = read_json(STATE)
    start = datetime.fromisoformat(cfg["window_start_kst"]).replace(tzinfo=KST)
    end = datetime.fromisoformat(cfg["window_end_kst"]).replace(tzinfo=KST)
    in_win = start <= now <= end and now.year == int(cfg["event_year"])
    print(f"{now.isoformat()} mode={mode} window={in_win}")
    if dry:
        print("DRY RUN (no ntfy, no state write)")
    if mode == "auto" and (not in_win or st.get("notified")):
        print("outside window or already sent; silent")
        return 0

    test = os.environ.get("CASH30_TEST") if dry else None
    if test:
        t = json.loads(test)
        px, ty = t.get("px", {}), t.get("ty")
    else:
        px = {s: last_price(cfg["sources"]["quote"].format(sym=s)) for s in SYMS}
        ty = last_price(cfg["sources"]["rate"])
        if ty is not None and ty > 20:  # ^TNX quoted x10 on some feeds
            ty /= 10
    nee_st, so_st = read_json(ROOT / "state" / "neeq3.json"), read_json(ROOT / "state" / "soq3.json")
    print("px:", px, "ty:", ty, "| nee:", nee_st.get("call_stance"), nee_st.get("score"), "| so:", so_st.get("score"), "| inputs:", json.dumps(cfg["inputs"].get("rate_targets"), ensure_ascii=False), cfg["inputs"].get("so_verdict"))

    if not any(px.values()):
        title = "[CASH30] 시세 조회 실패 | 판정 보류"  # route_topic -> cwally-ops
        msg = "\n".join([now.strftime("%Y-%m-%d %H:%M KST") + " | CASH30", "이게 뭐 알림: " + cfg["explain"],
                         "지금 상태: 세 종목 시세 조회 실패.", "지금 할 일: 이 알림으로 배분 결정 금지. 시세 직접 확인.",
                         "나중에: 수동 재실행은 job=cash30.", "익절/줄일 때: 해당 없음.", "하지 말 것: 실패를 신호로 해석."])
        if dry:
            print("DRY RUN would send:", title)
        else:
            publish(resolve_topic("port"), title, msg, 3)
        return 0

    res = decide(cfg, nee_st, so_st, px, ty)
    title = f"[CASH30] {res['title']}"
    msg = body(cfg, now, res, px, ty)
    if dry:
        print(f"DRY RUN would send: {title}\n{msg}")
        return 0
    publish(resolve_topic("port"), title, msg, 4 if res["key"] != "nee_wait" else 3)
    STATE.write_text(json.dumps({"notified": True, "choice": res["key"], "updated_at": now.isoformat()}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
