#!/usr/bin/env python3
"""FLTW(Franklin FTSE Taiwan ETF) 분할 계획 단계 조건 알림 (change-only). 2026-11-04 t53u.

morning 슬롯(평일 09:00 KST). 미국 직전 완료 일봉 종가만 본다. 일봉 조회는 coin_monitor.fetch_bars 재사용.
- 1차 매수: 종가 <= 90. 재무장 > 95
- 2차 매수: 종가 <= 82, TSMC 가이던스 하향이 없을 때만. 4분기 판정 전이면 "하향 없는지 직접 확인" 문구,
  판정이 보류(하회 또는 경계)면 "2차 보류" 문구. 재무장 > 87
- 3차 매수: TSMC 4분기 판정 양호 또는 종가 <= 75 중 먼저, 전체 기간 1회. 재점검이 켜져 있으면 가격 경로는 보류
- 재점검: 종가 <= 70 2거래일 연속 -> "매수 중단, 다시 판단" 1회. 해제 > 75
- 추격 금지: 종가 >= 110. 재무장 < 105
- TSMC 4분기 2026 실적: search_from~search_to 사이 SEC 6-K 전체 텍스트에서 4분기 매출(가이던스 52.3-54.3 대조),
  1Q27 매출 가이던스, 2027년 연간 성장 전망을 읽어 판정 1회. 못 읽으면 수동 확인 1회 뒤 재시도.
modes: auto | dry(발송과 state 없음) | selftest(오프라인) | tsmc-dry [6-K 접수번호 q4low q4high 다음분기라벨 연도] (판독만)
"""
from __future__ import annotations

import html
import json
import re
import sys
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic  # noqa: E402
from coin_monitor import fetch_bars, ny_now  # noqa: E402  (같은 Yahoo 완료 일봉 규칙)

KST = timezone(timedelta(hours=9))
NAME = "fltw"
CFG_PATH = ROOT / "configs" / f"{NAME}.json"
STATE_PATH = ROOT / "state" / f"{NAME}.json"
SEC_UA = {"User-Agent": "cwally-alerts sosmos3218@gmail.com"}

STATE_KEYS = {
    "updated_at": None, "buy1_armed": True, "buy2_armed": True, "buy3_done": False, "recheck_active": False,
    "chase_armed": True, "tsmc_verdict": None, "tsmc_done": False, "tsmc_manual_sent": False,
    "last_bar_date": None, "last_close": None, "fetch_fail_date": None,
}

INTRO = ("이게 뭐 알림: FLTW 분할 계획 단계 점검(11/4 승인). 1차 90달러, 2차 82달러(TSMC 가이던스 하향 없을 때), 3차(TSMC 4분기 양호 또는 75달러), "
         "70달러 2일 재점검, 110달러 추격 금지, TSMC 4분기 실적 판정이 새로 켜질 때만 옴. 자동 주문 아님.")
AVOID = "장중 급등 추격, 재점검 중 추가 매수, 대만 지정학 뉴스 하루만 보고 매매, 이 알림만 보고 주문, 레버리지."


def load_state() -> dict:
    base = dict(STATE_KEYS)
    if STATE_PATH.exists():
        try:
            base.update({k: v for k, v in json.loads(STATE_PATH.read_text(encoding="utf-8")).items() if k in STATE_KEYS})
        except json.JSONDecodeError:
            pass
    return base


def body(stamp: str, status: str, action: str, later: str, avoid: str = AVOID) -> str:
    return "\n".join([f"{stamp} | FLTW | Franklin FTSE Taiwan ETF", INTRO, f"지금 결과/상태: {status}",
                      f"지금 할 일: {action}", f"나중에: {later}", f"하지 말 것: {avoid}"])


def pct(a: float, b: float) -> float:
    return (a / b - 1) * 100


Alert = tuple[str, str, int, list[str]]


def buy3_msg(stamp: str, status: str, why: str) -> Alert:
    return ("[FLTW] 3차 매수 조건 | " + why,
            body(stamp, status + f" 3차 조건({why})이 먼저 맞았습니다. 3차는 전체 기간 1회만 옵니다.",
                 "계획의 3차 단계 조건입니다. 미국 정규장 지정가로, 미리 정한 계획 안에서만 검토하세요.",
                 "이후 3차 알림은 다시 오지 않습니다. 70달러 2거래일이면 재점검 알림이 옵니다."), 4, ["chart_with_upwards_trend"])


def evaluate(cfg: dict, state: dict, rows: list[tuple[date, float]], stamp: str) -> list[Alert]:
    r = cfg["rules"]
    b1, b2, b3, rc, ch = r["buy1"], r["buy2"], r["buy3"], r["recheck"], r["chase"]
    closes = [c for _, c in rows]
    last_d, close = rows[-1]
    prev = closes[-2] if len(closes) >= 2 else None
    hi52 = max(closes) if len(closes) >= 150 else None
    status = f"FLTW 종가 ${close:,.2f}({last_d:%m/%d} 미국)"
    if prev:
        status += f", 전일 대비 {pct(close, prev):+.2f}%"
    if hi52:
        status += f", 52주 종가 고점 ${hi52:,.2f} 대비 {pct(close, hi52):+.1f}%"
    v = state.get("tsmc_verdict")
    status += f". TSMC 4분기 판정: {v or '아직 없음(2027년 1월 중순 발표 예정)'}."
    out: list[Alert] = []

    n = int(rc["consec_days"])
    down_now = len(closes) >= n and all(c <= rc["close_max"] for c in closes[-n:])
    if down_now and not state["recheck_active"]:
        out.append(("[FLTW] 재점검 | 매수 중단, 다시 판단",
                    body(stamp, status + f" 종가가 {n}거래일 연속 {rc['close_max']:.0f}달러 이하입니다.",
                         "매수 중단. 계획을 다시 판단하세요(TSMC 가이던스, 대만 지정학, AI 투자 흐름).",
                         f"종가가 {rc['rearm_close']:.0f}달러 위로 올라가면 해제됩니다. 그 전에는 3차 가격 경로도 보류입니다."),
                    4, ["warning"]))
        state["recheck_active"] = True
    elif state["recheck_active"] and close > rc["rearm_close"]:
        state["recheck_active"] = False

    if close <= b1["close_max"] and state["buy1_armed"]:
        out.append(("[FLTW] 1차 매수 조건 | 90달러 이하",
                    body(stamp, status + f" 종가가 {b1['close_max']:.0f}달러 이하입니다.",
                         "계획의 1차 단계 조건입니다. 미국 정규장 지정가로, 개장 직후 30분은 피하고 미리 정한 계획 안에서만 검토하세요.",
                         f"종가가 {b1['rearm_close']:.0f}달러 위로 올라가면 다시 무장됩니다. 다음 단계는 82달러(2차)."), 4, ["chart_with_downwards_trend"]))
        state["buy1_armed"] = False
    elif not state["buy1_armed"] and close > b1["rearm_close"]:
        state["buy1_armed"] = True

    if close <= b2["close_max"] and state["buy2_armed"] and not state["recheck_active"]:
        if v and v != "양호":
            out.append(("[FLTW] 2차 보류 | 82달러, TSMC 가이던스 약화",
                        body(stamp, status + f" 종가가 {b2['close_max']:.0f}달러 이하지만 TSMC 4분기 판정이 '{v}'입니다.",
                             "2차는 보류. TSMC 가이던스가 약해졌으니 계획을 다시 보세요.",
                             f"종가가 {b2['rearm_close']:.0f}달러 위로 갔다가 다시 내려오면 다시 판정합니다."), 3, ["hourglass"]))
        else:
            check = ("TSMC 4분기 판정 양호라 가이던스 하향 없음이 확인됐습니다." if v == "양호" else
                     "아직 TSMC 4분기 자동 판정 전입니다. 주문 전에 TSMC가 가이던스를 낮추지 않았는지(최근 실적 발표, 월매출 공시) 직접 확인하세요.")
            out.append(("[FLTW] 2차 매수 조건 | 82달러 이하",
                        body(stamp, status + f" 종가가 {b2['close_max']:.0f}달러 이하입니다. {check}",
                             "TSMC 가이던스 하향이 없을 때만 계획의 2차 단계 조건입니다. 지정가로, 미리 정한 계획 안에서만 검토하세요.",
                             f"종가가 {b2['rearm_close']:.0f}달러 위로 올라가면 다시 무장됩니다. 다음은 75달러 또는 TSMC 4분기 양호(3차)."),
                        4, ["chart_with_downwards_trend"]))
        state["buy2_armed"] = False
    elif not state["buy2_armed"] and close > b2["rearm_close"]:
        state["buy2_armed"] = True

    if not state["buy3_done"] and close <= b3["close_max"] and not state["recheck_active"]:
        out.append(buy3_msg(stamp, status, f"{b3['close_max']:.0f}달러 이하"))
        state["buy3_done"] = True

    if close >= ch["close_min"] and state["chase_armed"]:
        out.append(("[FLTW] 110달러 도달 | 추격 금지",
                    body(stamp, status + f" 종가가 {ch['close_min']:.0f}달러 이상입니다(52주 고점 근처).",
                         "추격 매수 금지. 계획한 가격대(90, 82, 75)까지 기다리세요.",
                         f"종가가 {ch['rearm_close']:.0f}달러 아래로 내려가면 다시 무장됩니다."), 3, ["no_entry"]))
        state["chase_armed"] = False
    elif not state["chase_armed"] and close < ch["rearm_close"]:
        state["chase_armed"] = True

    state.update(last_bar_date=last_d.isoformat(), last_close=close, fetch_fail_date=None)
    state["_status"] = status
    return out


# ---------- TSMC 4분기 ----------

def sec_get(url: str) -> bytes:
    with urllib.request.urlopen(urllib.request.Request(url, headers=SEC_UA), timeout=40) as resp:
        return resp.read()


def to_text(raw: str) -> str:
    raw = re.sub(r"^begin \d+ .*?^end$", " ", raw, flags=re.S | re.M)  # uuencode 그림 덩어리 제거
    t = re.sub(r"<script.*?</script>|<style.*?</style>", "", raw, flags=re.S)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", t)))


ADJ = {"low": 2.0, "mid": 5.0, "high": 8.0}


def parse_tsmc(text: str, g_low: float, g_high: float, next_q: str, fy: int) -> dict:
    """6-K 텍스트에서 {'rev': 실적, 'q1': (lo, hi), 'fy': 성장%} (못 읽은 항목은 None)."""
    out: dict = {"rev": None, "q1": None, "fy": None}
    for m in re.finditer(r"Net Revenue \(US\$ billions\) ([\d.]+) ([\d.]+) ?- ?([\d.]+)", text):
        if abs(float(m.group(2)) - g_low) < 0.05 and abs(float(m.group(3)) - g_high) < 0.05:
            out["rev"] = float(m.group(1))
            break
    m = re.search(next_q + r" Guidance.{0,120}?Revenue to be between US\$([\d.]+) billion and US\$([\d.]+) billion", text)
    if m:
        out["q1"] = (float(m.group(1)), float(m.group(2)))
    m = re.search(rf"Future Outlook.{{0,160}}?{fy} revenue to (increase|grow|decrease|decline) by (?:around |close to |approximately |about |nearly )?"
                  r"(?:(low|mid|high)[- ]?(\d+)s|(\d+(?:\.\d+)?))\s*(?:%|percent)", text)
    if m:
        val = float(m.group(3)) + ADJ[m.group(2)] if m.group(2) else float(m.group(4))
        out["fy"] = -val if m.group(1) in ("decrease", "decline") else val
    return out


def judge_tsmc(p: dict, c: dict) -> dict:
    names = {"rev": "4분기 매출", "q1": "1분기 가이던스", "fy": "2027 연간 전망"}
    miss = [names[k] for k in ("rev", "q1", "fy") if p[k] is None]
    if miss:
        return {"ok": False, "why": "못 읽은 항목: " + ", ".join(miss)}
    rev = "상회" if p["rev"] > c["q4_guidance_high"] else "하회" if p["rev"] < c["q4_guidance_low"] else "부합"
    fy = "양호" if p["fy"] >= c["fy_growth_good_pct"] else "경계" if p["fy"] < c["fy_growth_warn_pct"] else "중립"
    mid = sum(p["q1"]) / 2
    q1 = "경계" if mid < p["rev"] * (1 - c["q1_drop_warn_pct"] / 100) else "양호"
    good = rev != "하회" and "경계" not in (fy, q1)
    return {"ok": True, "rev": rev, "fy": fy, "q1": q1, "mid": mid, "verdict": "양호" if good else "보류",
            "p_rev": p["rev"], "p_fy": p["fy"], "p_q1": p["q1"]}


def find_tsmc(c: dict, today: date) -> tuple[str | None, str]:
    """창 안 6-K 중 4분기 실적(가이던스 범위 일치)이 들어 있는 첫 공시의 (접수번호, 텍스트)."""
    s = json.loads(sec_get(f"https://data.sec.gov/submissions/CIK{c['cik']}.json"))["filings"]["recent"]
    lo = date.fromisoformat(c["search_from"])
    cands = sorted((d, a) for f, d, a in zip(s["form"], s["filingDate"], s["accessionNumber"]) if f == "6-K" and lo <= date.fromisoformat(d) <= today)
    for _, a in cands:
        t = to_text(sec_get(f"https://www.sec.gov/Archives/edgar/data/{int(c['cik'])}/{a.replace('-', '')}/{a}.txt").decode("utf-8", "replace"))
        if "Net Revenue (US$ billions)" in t and "Guidance" in t:
            return a, t
    return None, ""


def read_tsmc(c: dict, today: date) -> tuple[dict | None, str]:
    acc, text = find_tsmc(c, today)
    if not acc:
        return None, "TSMC 4분기 실적 6-K 아직 없음"
    return judge_tsmc(parse_tsmc(text, c["q4_guidance_low"], c["q4_guidance_high"], "1Q27", 2027), c), f"SEC 6-K {acc}"


def tsmc_msg(j: dict, src: str, stamp: str, status: str, c: dict) -> Alert:
    if j["ok"]:
        detail = (f"4분기 매출 {j['p_rev']*10:.1f}억 달러(가이던스 {c['q4_guidance_low']*10:.0f}억~{c['q4_guidance_high']*10:.0f}억 달러, {j['rev']}). "
                  f"2027년 연간 매출 성장 전망 {j['p_fy']:+.0f}%({j['fy']}, 20% 이상 양호 15% 미만 경계는 제안 기준). "
                  f"1분기 2027 매출 가이던스 중간값 {j['mid']*10:.1f}억 달러, 4분기 대비 {pct(j['mid'], j['p_rev']):+.1f}%({j['q1']}). 출처 {src}.")
        if j["verdict"] == "양호":
            return ("[FLTW] TSMC 4분기 양호 | 3차 조건 충족", body(stamp, detail + " " + status,
                    "TSMC 쪽 3차 조건이 맞았습니다. 3차가 아직이면 미리 정한 계획 안에서만 지정가로 검토하세요.",
                    "가격 조건 알림은 계속 옵니다."), 4, ["bar_chart"])
        return ("[FLTW] TSMC 4분기 약화 | 3차 보류", body(stamp, detail + " " + status,
                "3차는 보류. 82달러 2차 조건도 보류로 바뀝니다.", "다음 TSMC 실적(4월)까지 가격 조건만 계속 봅니다."), 4, ["bar_chart"])
    return ("[FLTW] TSMC 4분기 실적 나옴 | 직접 확인",
            body(stamp, f"TSMC 4분기 실적 공시는 찾았지만 자동 판정은 하지 않았습니다({j['why']}). 출처 {src}. {status}",
                 f"TSMC IR({c['ir_url']})에서 4분기 매출이 {c['q4_guidance_low']*10:.0f}억~{c['q4_guidance_high']*10:.0f}억 달러보다 낮지 않은지, "
                 "2027년 연간 성장 전망이 15% 미만이 아닌지, 1분기 가이던스 중간값이 4분기보다 10% 넘게 낮지 않은지 확인하세요. 셋 다 괜찮으면 3차 조건 충족.",
                 "다음 실행 때 자동 판독을 다시 시도합니다."), 4, ["bar_chart"])


def tsmc_step(cfg: dict, state: dict, today: date, stamp: str, status: str, reader=None) -> list[Alert]:
    c = cfg["tsmc_q4"]
    if state["tsmc_done"] or not (date.fromisoformat(c["search_from"]) <= today <= date.fromisoformat(c["search_to"])):
        return []
    try:
        j, src = (reader or read_tsmc)(c, today)
    except Exception as exc:  # noqa: BLE001
        print(f"tsmc read error: {type(exc).__name__}: {exc}")
        return []
    if j is None:
        print(src)
        return []
    if j["ok"]:
        state["tsmc_done"] = True
        state["tsmc_verdict"] = j["verdict"]
        out = [tsmc_msg(j, src, stamp, status, c)]
        if j["verdict"] == "양호" and not state["buy3_done"]:
            out.append(buy3_msg(stamp, status, "TSMC 4분기 양호"))
            state["buy3_done"] = True
        return out
    if not state["tsmc_manual_sent"]:
        state["tsmc_manual_sent"] = True
        return [tsmc_msg(j, src, stamp, status, c)]
    return []


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    if mode == "selftest":
        return selftest()
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    topic = resolve_topic("port", cfg.get("ntfy_topic"))
    server = resolve_server(cfg.get("ntfy_server"))

    if mode == "tsmc-dry":  # 예: tsmc-dry 0001046179-26-000008 32.2 33.4 1Q26 2026
        a, lo, hi, nq, fy = sys.argv[2], float(sys.argv[3]), float(sys.argv[4]), sys.argv[5], int(sys.argv[6])
        c = dict(cfg["tsmc_q4"], q4_guidance_low=lo, q4_guidance_high=hi)
        t = to_text(sec_get(f"https://www.sec.gov/Archives/edgar/data/{int(c['cik'])}/{a.replace('-', '')}/{a}.txt").decode("utf-8", "replace"))
        p = parse_tsmc(t, lo, hi, nq, fy)
        j = judge_tsmc(p, c)
        print(p)
        print(j)
        print("\n".join(tsmc_msg(j, f"SEC 6-K {a}", stamp, "(dry)", c)[:2]))
        print("DRY RUN: no publish, state not written")
        return 0

    end = cfg.get("ends_kst")
    if end and now > datetime.strptime(end, "%Y-%m-%d %H:%M").replace(tzinfo=KST):
        print(f"ended at {end}; silent")
        return 0
    rows, err = [], ""
    try:
        rows = fetch_bars(cfg["symbol"], ny_now())
        if len(rows) < 3:
            raise RuntimeError(f"FLTW 완료 일봉 {len(rows)}개")
        if (ny_now().date() - rows[-1][0]).days > int(cfg["stale_days"]):
            raise RuntimeError(f"FLTW 마지막 봉 {rows[-1][0]} 너무 오래됨")
    except Exception as exc:  # noqa: BLE001
        err, rows = str(exc), []
    dry = mode == "dry"
    state = load_state()
    alerts: list[Alert] = []
    if rows:
        alerts += evaluate(cfg, state, rows, stamp)
        status = state.pop("_status")
    else:
        status = f"FLTW 조회 실패({err})"
        if state.get("fetch_fail_date") != now.strftime("%Y%m%d"):
            alerts.append(("[FLTW] 조회 실패 | 조건 미확인", body(stamp, status, "증권사 앱에서 FLTW 종가를 직접 보세요.",
                           "다음 평일 09:00에 다시 봅니다.", "조회 실패를 조건 충족이나 정상으로 읽기."), 3, ["warning"]))
            state["fetch_fail_date"] = now.strftime("%Y%m%d")
    alerts += tsmc_step(cfg, state, now.date(), stamp, status)
    print(status)
    for title, msg, pri, tags in alerts:
        print(title)
        print(msg)
        if not dry:
            publish(topic, title, msg, pri, tags, server)
    if not alerts:
        print("no alert")
    if dry:
        print("DRY RUN: no publish, state not written")
        return 0
    state["updated_at"] = now.isoformat()
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


# ---------- 오프라인 selftest ----------

def selftest() -> int:
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    fails: list[str] = []

    def check(name, got, want):
        ok = got == want
        print(("ok   " if ok else "FAIL ") + f"{name}: got={got!r} want={want!r}")
        if not ok:
            fails.append(name)

    hist: dict = {}

    def run(path, state=None, base=100.0):
        st = dict(STATE_KEYS) if state is None else state
        closes = hist.setdefault(id(st), [base] * 60)
        out = []
        for c in path:
            closes.append(c)
            rows = [(date(2026, 8, 1) + timedelta(days=i), x) for i, x in enumerate(closes)]
            al = evaluate(cfg, st, rows, "S")
            st.pop("_status", None)
            out.append([t for t, _, _, _ in al])
        return out, st

    r = cfg["rules"]
    check("config thresholds", (r["buy1"]["close_max"], r["buy2"]["close_max"], r["buy3"]["close_max"], r["recheck"]["close_max"],
                                r["recheck"]["consec_days"], r["chase"]["close_min"], cfg["ends_kst"], cfg["tsmc_q4"]["q4_guidance_low"],
                                cfg["tsmc_q4"]["q4_guidance_high"]), (90.0, 82.0, 75.0, 70.0, 2, 110.0, "2027-03-31 23:59", 52.3, 54.3))
    check("threshold note says proposal not consensus", "컨센서스가 아님" in cfg["tsmc_q4"]["threshold_note"], True)
    t, _ = run([93.98, 95.0])
    check("93.98 then 95: silent", t, [[], []])
    t, st = run([90.0, 89.0, 95.5, 90.0])
    check("rule1 <=90 once, re-arm >95, fires again", t, [["[FLTW] 1차 매수 조건 | 90달러 이하"], [], [], ["[FLTW] 1차 매수 조건 | 90달러 이하"]])
    t, st = run([85.0, 82.0, 81.0, 88.0, 82.0])
    check("rule2 <=82 once (pre-Q4: says confirm guidance), re-arm >87", t,
          [["[FLTW] 1차 매수 조건 | 90달러 이하"], ["[FLTW] 2차 매수 조건 | 82달러 이하"], [], [], ["[FLTW] 2차 매수 조건 | 82달러 이하"]])
    al = evaluate(cfg, dict(STATE_KEYS, buy1_armed=False), [(date(2026, 11, 1), 85.0), (date(2026, 11, 2), 84.0), (date(2026, 11, 3), 81.0)], "S")
    check("rule2 pre-verdict message tells to confirm TSMC guidance not lowered", "가이던스를 낮추지 않았는지" in al[0][1], True)
    al = evaluate(cfg, dict(STATE_KEYS, buy1_armed=False, tsmc_verdict="보류"), [(date(2026, 11, 1), 85.0), (date(2026, 11, 2), 84.0), (date(2026, 11, 3), 81.0)], "S")
    check("rule2 after weak TSMC verdict: hold, no buy", [x[0] for x in al], ["[FLTW] 2차 보류 | 82달러, TSMC 가이던스 약화"])
    al = evaluate(cfg, dict(STATE_KEYS, buy1_armed=False, tsmc_verdict="양호"), [(date(2026, 11, 1), 85.0), (date(2026, 11, 2), 84.0), (date(2026, 11, 3), 81.0)], "S")
    check("rule2 after good TSMC verdict: buy, says confirmed", ([x[0] for x in al], "확인됐습니다" in al[0][1]), (["[FLTW] 2차 매수 조건 | 82달러 이하"], True))

    t, st = run([88.0, 80.0, 75.0, 74.0, 90.0, 96.0, 74.0])
    flat = [x for d in t for x in d]
    check("rule3 price path: <=75 fires once for whole window (no repeat after re-arms)",
          (t[2], sum("3차" in x for x in flat)), (["[FLTW] 3차 매수 조건 | 75달러 이하"], 1))
    c = cfg["tsmc_q4"]
    good = judge_tsmc({"rev": 55.0, "q1": (50.0, 52.0), "fy": 25.0}, c)
    weak = judge_tsmc({"rev": 52.0, "q1": (50.0, 52.0), "fy": 25.0}, c)

    def ts(st, j, day="2027-01-15"):
        return [t for t, _, _, _ in tsmc_step(cfg, st, date.fromisoformat(day), "S", "st", reader=lambda cc, d: (j, "SEC 6-K x"))]
    st = dict(STATE_KEYS)
    check("rule3 TSMC 양호 first: verdict + 3차 once", ts(st, good), ["[FLTW] TSMC 4분기 양호 | 3차 조건 충족", "[FLTW] 3차 매수 조건 | TSMC 4분기 양호"])
    t, _ = run([74.0], state=st)
    check("rule3 then price <=75 later: no second 3차", any("3차" in x for x in t[0]), False)
    st = dict(STATE_KEYS)
    run([88.0, 74.0], state=st)
    check("rule3 price first, then TSMC 양호: verdict only, no second 3차", ts(st, good), ["[FLTW] TSMC 4분기 양호 | 3차 조건 충족"])
    st = dict(STATE_KEYS)
    check("TSMC weak: 3차 보류, no 3차", (ts(st, weak), st["tsmc_verdict"]), (["[FLTW] TSMC 4분기 약화 | 3차 보류"], "보류"))

    t, st = run([72.0, 70.0, 71.0, 69.0, 70.0, 68.0, 76.0])
    flat = [x for d in t for x in d]
    check("rule4 needs 2 consecutive <=70 (70,71 no; 69,70 yes), once, clears >75",
          (["[FLTW] 재점검 | 매수 중단, 다시 판단" in d for d in t], st["recheck_active"]), ([False, False, False, False, True, False, False], False))
    st = dict(STATE_KEYS, buy1_armed=False, buy2_armed=False, recheck_active=True)
    t, st = run([74.0, 73.0], state=st, base=74.0)
    check("rule4 active (not yet cleared >75) blocks 3차 price path", (any("3차" in x for d in t for x in d), st["buy3_done"]), (False, False))
    t, st = run([80.0, 81.0, 74.0], base=80.0)
    check("gap down to <=75 before any recheck: 3차 fires (whichever first)", any("3차" in x for x in t[2]), True)
    t, _ = run([109.0, 110.0, 111.0, 104.0, 112.0])
    check("rule5 >=110 once, re-arm <105", t, [[], ["[FLTW] 110달러 도달 | 추격 금지"], [], [], ["[FLTW] 110달러 도달 | 추격 금지"]])

    # TSMC parser on real 6-K phrasing (2026-01-15 Q4 2025 and 2026-10-15 Q3 2026)
    q425 = ("Guidance Net Revenue (US$ billions) 33.73 32.2-33.4 33.10 26.88 +1.9% +25.5% Net Revenue ... 1Q26 Guidance Based on our current "
            "business outlook, management expects: ▪ Revenue to be between US$34.6 billion and US$35.8 billion And, ... Future Outlook Based on our "
            "current business outlook, management expects: ▪ 2026 revenue to increase by close to 30% in US dollar terms From 2024")
    check("parse real Q4 2025 6-K text", parse_tsmc(q425, 32.2, 33.4, "1Q26", 2026), {"rev": 33.73, "q1": (34.6, 35.8), "fy": 30.0})
    q326 = ("Net Revenue (US$ billions) 46.72 44.6-45.8 40.20 33.10 +16.2% ... 4Q26 Guidance Based on our current business outlook, management expects: "
            "▪ Revenue to be between US$52.3 billion and US$54.3 billion And ... Future Outlook Based on our current business outlook, management expects: "
            "▪ 2026 revenue to increase by around 44% in US dollar terms")
    check("parse real Q3 2026 6-K text", parse_tsmc(q326, 44.6, 45.8, "4Q26", 2026), {"rev": 46.72, "q1": (52.3, 54.3), "fy": 44.0})
    check("parse rejects wrong guidance range (not Q4)", parse_tsmc(q326, 52.3, 54.3, "1Q27", 2027)["rev"], None)
    check("parse mid-20s phrasing", parse_tsmc("Future Outlook x: ▪ 2027 revenue to increase by mid-20s percent in US dollar", 0, 0, "1Q27", 2027)["fy"], 25.0)
    j = judge_tsmc({"rev": 53.0, "q1": (46.0, 47.0), "fy": 22.0}, c)
    check("judge: Q1 mid >10% below Q4 -> 경계, 보류", (j["rev"], j["q1"], j["verdict"]), ("부합", "경계", "보류"))
    j = judge_tsmc({"rev": 53.0, "q1": (50.0, 51.0), "fy": 17.0}, c)
    check("judge: 15-20 중립 still 양호", (j["fy"], j["verdict"]), ("중립", "양호"))
    j = judge_tsmc({"rev": 55.0, "q1": (50.0, 51.0), "fy": 14.0}, c)
    check("judge: fy <15 경계 -> 보류", (j["rev"], j["fy"], j["verdict"]), ("상회", "경계", "보류"))
    bad = judge_tsmc({"rev": 53.0, "q1": None, "fy": None}, c)
    st = dict(STATE_KEYS)
    check("manual fallback once, then retry gives verdict once",
          (ts(st, bad), ts(st, bad), ts(st, good)[0], ts(st, good)),
          (["[FLTW] TSMC 4분기 실적 나옴 | 직접 확인"], [], "[FLTW] TSMC 4분기 양호 | 3차 조건 충족", []))
    check("TSMC outside window: reader not called", [t for t, _, _, _ in tsmc_step(cfg, dict(STATE_KEYS), date(2027, 1, 7), "S", "st",
                                                     reader=lambda cc, d: (_ for _ in ()).throw(AssertionError()))], [])
    check("TSMC no filing yet: silent", [t for t, _, _, _ in tsmc_step(cfg, dict(STATE_KEYS), date(2027, 1, 10), "S", "st", reader=lambda cc, d: (None, "없음"))], [])

    msgs = []
    for path in ([89.0, 81.0, 74.0, 69.0, 68.0, 111.0],):
        st = dict(STATE_KEYS)
        closes = [100.0] * 60
        for x in path:
            closes.append(x)
            msgs += evaluate(cfg, st, [(date(2026, 8, 1) + timedelta(days=i), y) for i, y in enumerate(closes)], "S")
    msgs += [tsmc_msg(good, "s", "S", "st", c), tsmc_msg(weak, "s", "S", "st", c), tsmc_msg(bad, "s", "S", "st", c)]
    text = " ".join(t + m for t, m, _, _ in msgs)
    check("all titles [FLTW], no quantity/weight words, no middle dot or em-dash",
          (all(t.startswith("[FLTW]") for t, _, _, _ in msgs), bool(re.search(r"비중|수량|\d+주\b|전액|한도", text.replace("52주", ""))),
           "\u00b7" in text, "\u2014" in text), (True, False, False, False))
    check("manual title not routed to ops", bool(re.search(r"조회\s?실패|파싱\s?실패|확인 불가|해석\s?불가", tsmc_msg(bad, "s", "S", "st", c)[0])), False)
    print(f"SELFTEST {'PASS' if not fails else 'FAIL'}: {len(fails)} failed")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
