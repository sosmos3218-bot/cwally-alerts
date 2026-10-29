#!/usr/bin/env python3
"""[PLTR25] 팔란티어(PLTR) 보통주 신규 매수 판단. 평일 09:00 KST morning 묶음. 보유 없음(미국 직접 투자).

가격 구간(매일 Yahoo 일봉으로 다시 계산, 고정 숫자 없음):
  추세 이탈  종가 < 200일선                                  (2일 연속, p4, 신규 매수 중단)
  2차 매수   200일선 ~ 200일선 x1.05                          (p4, 2차 분할 매수 검토)
  1차 매수   200일선 x1.05 ~ 50일선 x1.02                     (p4, 1차 분할 매수 검토)
  관망       그 사이                                           (p3)
  추격 금지  종가 >= min(직전 120거래일 최고 종가, 50일선 x1.15) (2일 연속, p3)
더 싼 매수 구간으로 내려갈 때는 1일, 추격 금지와 추세 이탈 진입과 위로 벗어날 때는 2일 연속.
위로 벗어날 때 2% 여유(추세 이탈 해제는 200일선 x1.03), 추격 금지 해제는 기준선 x0.97 아래.
구간은 이력 250거래일을 매번 다시 재생해 정한다(빠진 실행에 강함). 알림은 구간이 바뀔 때만.
주간 점검(ISO 주 첫 실행, p2): 가격, 이동평균, 구간 경계, 최근 범위, PLTR 대 QQQ 1년 달러 수익률, 밸류에이션(Yahoo).
실적 판정은 [PLTR] 알림이 한다. 여기서는 참조 한 줄만. 조회 실패는 cwally-ops 하루 1번.
modes: auto | dry (ntfy 없음, state 안 씀) | selftest (가짜 값, 네트워크 없음)
"""
from __future__ import annotations

import bisect
import http.cookiejar
import json
import sys
import traceback
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic, route_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
ET = ZoneInfo("America/New_York")
CFG_PATH = ROOT / "configs" / "pltr25.json"
STATE_PATH = ROOT / "state" / "pltr25.json"
UA = {"User-Agent": "Mozilla/5.0"}
BANNED = ("\u00b7", "\u2014")
FAIL_TITLE = "[PLTR25] 주가 조회 실패 | 매매 신호 아님"
BREAK, B2, B1, WAIT, CHASE = "추세 이탈", "2차 매수", "1차 매수", "관망", "추격 금지"
ORDER = [BREAK, B2, B1, WAIT, CHASE]

DEFAULT_STATE = {
    "updated_at": None,
    "last_date": None,
    "close": None,
    "ma50": None,
    "ma200": None,
    "levels": None,
    "range": None,
    "zone": None,
    "zone_sent": None,
    "pending": None,
    "valuation": None,
    "last_weekly": None,
    "fetch_fail_date": None,
}


def fetch(url: str, opener: urllib.request.OpenerDirector | None = None) -> str:
    req = urllib.request.Request(url, headers=UA)
    client = opener or urllib.request.build_opener()
    with client.open(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def parse_yahoo(text: str, now_ts: float | None = None) -> list[tuple[date, float]]:
    res = json.loads(text)["chart"]["result"][0]
    ts = res.get("timestamp") or []
    closes = res["indicators"]["quote"][0]["close"]
    reg = ((res.get("meta") or {}).get("currentTradingPeriod") or {}).get("regular") or {}
    out = [(datetime.fromtimestamp(t, ET).date(), float(c)) for t, c in zip(ts, closes) if c is not None]
    if out and ts and reg.get("start") and reg.get("end"):
        now_ts = now_ts if now_ts is not None else datetime.now(timezone.utc).timestamp()
        if ts[-1] >= reg["start"] and now_ts < reg["end"]:
            out = out[:-1]  # 장중 막대는 종가가 아니다
    return sorted(dict(out).items())


def valuation() -> dict | None:
    jar = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    try:
        fetch("https://fc.yahoo.com", op)
    except Exception:
        pass
    crumb = fetch("https://query2.finance.yahoo.com/v1/test/getcrumb", op).strip()
    if not crumb or crumb.startswith("<") or crumb.startswith("{"):
        return None
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    d = json.loads(fetch(f"{cfg['sources']['valuation']}&crumb={urllib.parse.quote(crumb)}", op))
    return parse_valuation(d)


def parse_valuation(d: dict) -> dict | None:
    res = ((d.get("quoteSummary") or {}).get("result") or [None])[0] or {}
    sd, ks = res.get("summaryDetail") or {}, res.get("defaultKeyStatistics") or {}
    def raw(*xs):
        for x in xs:
            v = (x or {}).get("raw") if isinstance(x, dict) else None
            if isinstance(v, (int, float)):
                return float(v)
        return None
    out = {"fwd_pe": raw(sd.get("forwardPE"), ks.get("forwardPE")),
           "trail_pe": raw(sd.get("trailingPE")),
           "ps": raw(sd.get("priceToSalesTrailing12Months"))}
    return out if any(v is not None for v in out.values()) else None


# ---------------- zones ----------------
def levels_at(closes: list[float], i: int, z: dict) -> dict | None:
    f, s, rd = z["ma_fast"], z["ma_slow"], z["range_days"]
    if i + 1 < s or i < 1:
        return None
    ma50 = sum(closes[i - f + 1:i + 1]) / f
    ma200 = sum(closes[i - s + 1:i + 1]) / s
    prior = closes[max(0, i - rd):i]
    hi = max(prior)
    return {"ma50": ma50, "ma200": ma200, "hi": hi,
            "brk": ma200, "b2": ma200 * z["b2_ma200_mult"], "b1": ma50 * z["b1_ma50_mult"],
            "chase": min(hi, ma50 * z["chase_ma50_mult"])}


def raw_zone(c: float, lv: dict) -> str:
    if c < lv["brk"]:
        return BREAK
    if c <= lv["b2"]:
        return B2
    if c <= lv["b1"]:
        return B1
    if c >= lv["chase"]:
        return CHASE
    return WAIT


def up_exit(zone: str, lv: dict, z: dict) -> float:
    h = 1 + z["hyst"]
    return {BREAK: lv["brk"] * z["break_exit_mult"], B2: lv["b2"] * h, B1: lv["b1"] * h, WAIT: lv["chase"]}.get(zone, float("inf"))


def step_zone(cur: str | None, pend: dict | None, c: float, lv: dict, z: dict) -> tuple[str, dict | None]:
    r = raw_zone(c, lv)
    if cur is None:
        return r, None
    ic, ir = ORDER.index(cur), ORDER.index(r)
    if ir > ic and c < up_exit(cur, lv, z):
        r = cur
    if cur == CHASE and ir < ic and c >= lv["chase"] * z["chase_exit_mult"]:
        r = cur
    if r == cur:
        return cur, None
    need = 1 if (ORDER.index(r) < ic and r in (B1, B2)) else z["confirm_days"]
    n = pend["n"] + 1 if pend and pend.get("zone") == r else 1
    if n >= need:
        return r, None
    if r == BREAK and ic > ORDER.index(B2):
        return B2, {"zone": r, "n": n}  # 200일선 아래 첫날: 2차 구간을 지나쳤으니 2차로, 이탈은 다음 날 확인
    return cur, {"zone": r, "n": n}


def replay(bars: list[tuple[date, float]], z: dict) -> dict:
    closes = [c for _, c in bars]
    start = max(z["ma_slow"] - 1, len(bars) - z["replay_bars"])
    cur, pend, lv = None, None, None
    for i in range(start, len(bars)):
        lv = levels_at(closes, i, z)
        if lv is None:
            continue
        cur, pend = step_zone(cur, pend, closes[i], lv, z)
    if lv is None:
        raise ValueError("bars too short for 200-day MA")
    rd = z["range_days"]
    win = bars[max(0, len(bars) - rd):]
    hi = max(win, key=lambda x: x[1])
    lo = min(win, key=lambda x: x[1])
    return {"zone": cur, "pending": pend, "lv": lv, "date": bars[-1][0], "close": closes[-1],
            "range": {"hi": [hi[0].isoformat(), round(hi[1], 2)], "lo": [lo[0].isoformat(), round(lo[1], 2)]}}


def one_year(bars: list[tuple[date, float]] | None) -> float | None:
    if not bars:
        return None
    days = [d for d, _ in bars]
    i = bisect.bisect_right(days, bars[-1][0] - timedelta(days=365)) - 1
    return None if i < 0 else (bars[-1][1] / bars[i][1] - 1) * 100


# ---------------- copy ----------------
def body(stamp: str, status: str, action: str, later: str, trim: str, avoid: str) -> str:
    return "\n".join([
        f"{stamp} | PLTR 신규 매수 판단",
        "이게 뭐 알림: 팔란티어(PLTR) 보통주를 새로 살지 보는 가격 구간 알림입니다. 50일선, 200일선, 최근 고점으로 매일 다시 계산합니다. 보유 없음. 매수 지시가 아닙니다.",
        f"지금 결과/상태: {status}",
        f"지금 할 일: {action}",
        f"나중에: {later}",
        f"익절/줄일 때: {trim}",
        f"하지 말 것: {avoid}",
    ])


def level_line(r: dict) -> str:
    lv = r["lv"]
    return (f"종가 ${r['close']:.2f} (미국 {r['date'].isoformat()}). 50일선 ${lv['ma50']:.2f}, 200일선 ${lv['ma200']:.2f}. "
            f"경계: 추격 금지 ${lv['chase']:.2f} 이상, 1차 ${lv['b2']:.2f}~${lv['b1']:.2f}, 2차 ${lv['brk']:.2f}~${lv['b2']:.2f}, 추세 이탈 ${lv['brk']:.2f} 아래(2일). "
            f"최근 120거래일 고점 ${r['range']['hi'][1]:.2f} ({r['range']['hi'][0]}), 저점 ${r['range']['lo'][1]:.2f} ({r['range']['lo'][0]}).")


def earnings_line(cfg: dict, today: date) -> str:
    e = cfg.get("earnings_ref") or {}
    if not e or today.isoformat() > e.get("show_until", ""):
        return ""
    return f" 실적: 미국 {e['us']}(한국 {e['kst']}). 숫자 판정은 {e['alert']} 알림이 따로 보냅니다."


ZONE_COPY = {
    B1: ("1차 분할 매수 검토", 4, "1차 분할 매수 검토. 계획 물량의 3분의 1 이하, 지정가로.",
         "200일선 근처(2차 구간)까지 내려오면 2차 알림이 옵니다.", "한 번에 다 사기. 실적 발표 직전에 1차 물량 전부 넣기."),
    B2: ("2차 분할 매수 검토", 4, "2차 분할 매수 검토. 1차를 샀다면 같은 크기까지.",
         "200일선 아래로 2일 마감하면 추세 이탈 알림이 옵니다. 그때는 신규 매수 중단.", "200일선이 깨지는 중에 물타기."),
    BREAK: ("추세 이탈, 신규 매수 중단", 4, "신규 매수 중단. 관망.",
            "200일선 x1.03 위로 2일 마감하면 해제 알림이 옵니다.", "싸 보인다는 이유로 바닥 잡기."),
    WAIT: ("관망", 3, "관망. 1차 구간까지 기다리세요.",
           "50일선 x1.02 아래로 오면 1차 알림, 최근 고점 위로 2일 마감하면 추격 금지 알림.", "구간 밖에서 서둘러 사기."),
    CHASE: ("추격 금지", 3, "추격 금지. 신규 매수 보류.",
            "기준선 x0.97 아래로 2일 마감하면 관망으로 돌아갑니다.", "신고가 돌파만 보고 따라 사기."),
}


def title_for(zone: str, r: dict) -> str:
    tag = {B1: "50일선 근처", B2: "200일선 근처", BREAK: "200일선 아래 2일", WAIT: "구간 밖", CHASE: "최근 고점 위"}[zone]
    return f"[PLTR25] ${r['close']:.2f} {tag} | {ZONE_COPY[zone][0]}"


# ---------------- evaluate ----------------
def evaluate(cfg: dict, state: dict, data: dict, now: datetime) -> list[tuple[str, str, str, int]]:
    z = cfg["zones"]
    today = now.date()
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    alerts: list[tuple[str, str, str, int]] = []
    r = None
    err = None
    if data.get("pltr"):
        try:
            r = replay(data["pltr"], z)
        except Exception as exc:
            err = f"구간 계산 실패: {exc}"
    else:
        err = "Yahoo PLTR 일봉 조회 실패"
    if err:
        if state.get("fetch_fail_date") != today.isoformat():
            alerts.append(("ops", FAIL_TITLE, body(
                stamp, f"{err}. 구간은 이번에 바꾸지 않았습니다(마지막 {state.get('zone') or '없음'}).",
                "이 알림으로 사거나 미루지 마세요. 증권 앱에서 PLTR 종가를 직접 보세요.",
                "다음 평일 09:00 KST에 다시 계산합니다. 구간은 일봉 이력으로 다시 재생하므로 빠진 날이 있어도 맞춰집니다.",
                "보유 없음, 해당 없음.", "조회 실패를 구간 변경으로 읽기."), 3))
            state["fetch_fail_date"] = today.isoformat()
        return alerts

    lv = r["lv"]
    state.update({"last_date": r["date"].isoformat(), "close": round(r["close"], 2), "ma50": round(lv["ma50"], 2),
                  "ma200": round(lv["ma200"], 2), "zone": r["zone"], "pending": r["pending"], "range": r["range"],
                  "levels": {k: round(lv[k], 2) for k in ("chase", "b1", "b2", "brk")}})
    if data.get("valuation"):
        state["valuation"] = dict(data["valuation"], date=today.isoformat())
    el = earnings_line(cfg, today)
    prev = state.get("zone_sent")
    if prev is None:
        state["zone_sent"] = r["zone"]  # 첫 실행은 조용히 기준만 잡는다
    elif prev != r["zone"]:
        _, pri, act, later, avoid = ZONE_COPY[r["zone"]]
        alerts.append(("port", title_for(r["zone"], r), body(
            stamp, f"구간 {prev}에서 {r['zone']}(으)로. {level_line(r)}{el}", act, later, "보유 없음, 해당 없음.", avoid), pri))
        state["zone_sent"] = r["zone"]

    wk = today.strftime("%G-W%V")
    if state.get("last_weekly") != wk:
        pl, ql = one_year(data.get("pltr")), one_year(data.get("qqq"))
        pq = (f"PLTR 1년 {pl:+.1f}%, QQQ 1년 {ql:+.1f}%, 차이 {pl - ql:+.1f}%p (미국 {r['date'].isoformat()} 종가, 달러 기준)."
              if pl is not None and ql is not None else "PLTR 대 QQQ 1년: QQQ 조회 실패.")
        v = data.get("valuation")
        if v:
            parts = []
            if v.get("fwd_pe") is not None:
                parts.append(f"선행 PER {v['fwd_pe']:.1f}배")
            if v.get("trail_pe") is not None:
                parts.append(f"후행 PER {v['trail_pe']:.1f}배")
            if v.get("ps") is not None:
                parts.append(f"PSR {v['ps']:.1f}배")
            vl = " 밸류에이션(Yahoo): " + ", ".join(parts) + "."
        else:
            vl = " 밸류에이션: Yahoo 조회 실패, 이번 주는 생략."
        pend = f" 확인 중: {r['pending']['zone']} {r['pending']['n']}일째." if r["pending"] else ""
        _, _, act, later, avoid = ZONE_COPY[r["zone"]]
        alerts.append(("port", f"[PLTR25] 주간 점검 | ${r['close']:.2f}, {r['zone']}", body(
            stamp, f"구간 {r['zone']}.{pend} {level_line(r)} {pq}{vl}{el}", act, later, "보유 없음, 해당 없음.",
            "주간 점검을 매수 신호로 읽기. " + avoid), 2))
        state["last_weekly"] = wk
    return alerts


def banned_in(text: str) -> bool:
    return any(b in text for b in BANNED)


def load_state() -> dict:
    st = dict(DEFAULT_STATE)
    if STATE_PATH.exists():
        try:
            st.update({k: v for k, v in json.loads(STATE_PATH.read_text(encoding="utf-8")).items() if k in DEFAULT_STATE})
        except json.JSONDecodeError:
            pass
    return st


def gather(cfg: dict) -> dict:
    data: dict = {}
    for key in ("pltr", "qqq"):
        try:
            data[key] = parse_yahoo(fetch(cfg["sources"][key]))
        except Exception as exc:
            print(f"yahoo {key} fail: {exc}")
            data[key] = None
    try:
        data["valuation"] = valuation()
    except Exception as exc:
        print(f"valuation fail (omitted): {exc}")
        data["valuation"] = None
    return data


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    if mode == "selftest":
        return selftest(cfg)
    dry = mode != "auto"
    now = datetime.now(KST)
    state = load_state()
    data = gather(cfg)
    alerts = evaluate(cfg, state, data, now)
    print(f"close={state.get('close')} ({state.get('last_date')}) ma50={state.get('ma50')} ma200={state.get('ma200')} "
          f"levels={state.get('levels')} zone={state.get('zone')} pending={state.get('pending')} valuation={state.get('valuation')}")
    port = resolve_topic("port", cfg.get("ntfy_topic"))
    server = resolve_server(cfg.get("ntfy_server"))
    for kind, title, msg, pri in alerts:
        topic = route_topic("cwally-ops" if kind == "ops" else port, title)
        if banned_in(title + msg):
            print("WARN: banned char in copy")
        print(f"--- {title} (p{pri}, topic={topic}, send={not dry})")
        print(msg)
        if not dry:
            publish(topic, title, msg, pri, ["chart_with_upwards_trend"], server)
    if not alerts:
        print("no alert")
    if dry:
        print("DRY RUN: no ntfy, no state write")
        return 0
    state["updated_at"] = now.isoformat()
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


# ---------------- selftest ----------------
def _bars(closes: list[float], start: date = date(2025, 6, 2)) -> list[tuple[date, float]]:
    out, d = [], start
    for c in closes:
        while d.weekday() >= 5:
            d += timedelta(days=1)
        out.append((d, c))
        d += timedelta(days=1)
    return out


def selftest(cfg: dict) -> int:
    ok, fails = 0, []
    copies = [CFG_PATH.read_text(encoding="utf-8")]
    z = cfg["zones"]

    def check(label, got, want=True):
        nonlocal ok
        if got == want:
            ok += 1
        else:
            fails.append(f"{label}: got {got!r} want {want!r}")

    def run(st, data, now):
        al = evaluate(cfg, st, data, now)
        copies.extend(a[1] + a[2] for a in al)
        return al

    # 기본 이력: 완만한 상승, 210 급등, 160 되돌림, 196 회복 (실제 PLTR 모양과 비슷)
    base = ([100 + i * (50 / 199) for i in range(200)] + [150 + (i + 1) * 3 for i in range(20)]
            + [210 - (i + 1) * 1.25 for i in range(40)] + [160 + (i + 1) * 0.9 for i in range(40)])

    def lv_last(closes):
        return levels_at(closes, len(closes) - 1, z)

    lv = lv_last(base)
    check("levels ordered brk<b2<b1<chase", lv["brk"] < lv["b2"] < lv["b1"] < lv["chase"])
    check("chase = min(prior high, ma50x1.15)", round(lv["chase"], 6), round(min(max(base[-121:-1]), lv["ma50"] * 1.15), 6))
    # raw zone 전부
    check("raw BREAK", raw_zone(lv["brk"] - 0.01, lv), BREAK)
    check("raw B2", raw_zone(lv["brk"] + 0.01, lv), B2)
    check("raw B1", raw_zone(lv["b2"] + 0.01, lv), B1)
    check("raw WAIT", raw_zone(lv["b1"] + 0.01, lv), WAIT)
    check("raw CHASE", raw_zone(lv["chase"], lv), CHASE)

    # step: 아래 매수 구간은 1일, 추세 이탈과 추격 금지는 2일
    check("WAIT -> B1 one close", step_zone(WAIT, None, lv["b1"] - 0.1, lv, z), (B1, None))
    check("B1 -> B2 one close", step_zone(B1, None, lv["b2"] - 0.1, lv, z), (B2, None))
    s1 = step_zone(B2, None, lv["brk"] - 0.1, lv, z)
    check("B2 -> BREAK day1 pending", s1, (B2, {"zone": BREAK, "n": 1}))
    check("B2 -> BREAK day2 switch", step_zone(B2, s1[1], lv["brk"] - 0.1, lv, z), (BREAK, None))
    s2 = step_zone(WAIT, None, lv["chase"] + 1, lv, z)
    check("WAIT -> CHASE day1 pending", s2, (WAIT, {"zone": CHASE, "n": 1}))
    check("WAIT -> CHASE day2 switch", step_zone(WAIT, s2[1], lv["chase"] + 1, lv, z), (CHASE, None))
    check("pending reset by a non-confirming day", step_zone(WAIT, s2[1], lv["b1"] + 1, lv, z), (WAIT, None))
    # 히스테리시스
    check("B1 stays just above b1 (inside 2%)", step_zone(B1, None, lv["b1"] * 1.01, lv, z), (B1, None))
    s3 = step_zone(B1, None, lv["b1"] * 1.03, lv, z)
    check("B1 exit above +2% needs 2 days (day1)", s3, (B1, {"zone": WAIT, "n": 1}))
    check("B1 exit day2", step_zone(B1, s3[1], lv["b1"] * 1.03, lv, z), (WAIT, None))
    check("BREAK stays below ma200x1.03", step_zone(BREAK, None, lv["brk"] * 1.02, lv, z), (BREAK, None))
    s4 = step_zone(BREAK, None, lv["brk"] * 1.035, lv, z)
    check("BREAK exit day1 pending", s4[0], BREAK)
    check("BREAK exit day2 -> B2", step_zone(BREAK, s4[1], lv["brk"] * 1.035, lv, z)[0], B2)
    check("CHASE holds above chase x0.97", step_zone(CHASE, None, lv["chase"] * 0.98, lv, z), (CHASE, None))
    s5 = step_zone(CHASE, None, lv["chase"] * 0.96, lv, z)
    check("CHASE exit day1 pending", s5, (CHASE, {"zone": WAIT, "n": 1}))
    check("CHASE exit day2", step_zone(CHASE, s5[1], lv["chase"] * 0.96, lv, z), (WAIT, None))
    check("CHASE gap down to B1 in 1 day", step_zone(CHASE, None, lv["b1"] * 0.99, lv, z), (B1, None))

    # replay 와 evaluate
    now = datetime(2026, 10, 28, 9, 0, tzinfo=KST)  # W44
    st = dict(DEFAULT_STATE, last_weekly="2026-W44")
    lv0 = lv_last(base)
    up = base[:-1] + [(lv0["b1"] * 1.03 + lv0["chase"] * 0.97) / 2]   # 관망권 한가운데
    b_wait = _bars(up)
    rw = replay(b_wait, z)
    check("replay current zone WAIT", rw["zone"], WAIT)
    al = run(st, {"pltr": b_wait, "qqq": None}, now)
    check("first run seeds silently", (al, st["zone_sent"]), ([], WAIT))
    al = run(st, {"pltr": b_wait, "qqq": None}, now)
    check("idempotent: same data no send", al, [])
    lvw = rw["lv"]
    b_b1 = b_wait + [(b_wait[-1][0] + timedelta(days=1), lvw["b1"] * 0.97)]
    al = run(st, {"pltr": b_b1, "qqq": None}, now + timedelta(days=1))
    check("B1 alert p4 send-on-change", [(a[1].endswith("1차 분할 매수 검토"), a[3]) for a in al], [(True, 4)])
    al = run(st, {"pltr": b_b1, "qqq": None}, now + timedelta(days=1))
    check("B1 not repeated", al, [])
    # 추세 이탈: 1일은 침묵, 2일째 p4
    lb = levels_at([c for _, c in b_b1], len(b_b1) - 1, z)
    d0 = b_b1[-1][0]
    b_brk1 = b_b1 + [(d0 + timedelta(days=1), lb["brk"] * 0.95)]
    al = run(st, {"pltr": b_brk1, "qqq": None}, now + timedelta(days=2))
    check("one close below 200 -> B2 only (1 day down), no break yet", [a[1].split("| ")[1] for a in al], ["2차 분할 매수 검토"])
    b_brk2 = b_brk1 + [(d0 + timedelta(days=4), lb["brk"] * 0.94)]
    al = run(st, {"pltr": b_brk2, "qqq": None}, now + timedelta(days=3))
    check("second close below 200 -> BREAK p4", [(a[1].split("| ")[1], a[3]) for a in al], [("추세 이탈, 신규 매수 중단", 4)])
    # 빠진 실행: 상태가 B1 이어도 이력 재생으로 바로 BREAK 하나만
    st2 = dict(DEFAULT_STATE, last_weekly="2026-W44", zone_sent=B1)
    al = run(st2, {"pltr": b_brk2, "qqq": None}, now + timedelta(days=3))
    check("missed run: one alert for final zone", [a[1].split("| ")[1] for a in al], ["추세 이탈, 신규 매수 중단"])
    # 추격 금지: 최근 고점 위 2일
    hi_b = b_wait + [(b_wait[-1][0] + timedelta(days=1), max(base) * 1.03), (b_wait[-1][0] + timedelta(days=2), max(base) * 1.04)]
    st3 = dict(DEFAULT_STATE, last_weekly="2026-W44", zone_sent=WAIT)
    al = run(st3, {"pltr": hi_b[:-1], "qqq": None}, now)
    check("chase day1 silent", al, [])
    al = run(st3, {"pltr": hi_b, "qqq": None}, now)
    check("chase day2 p3", [(a[1].endswith("추격 금지"), a[3]) for a in al], [(True, 3)])
    check("title has price and no-chase", al[0][1].startswith("[PLTR25] $") if al else False)
    # 관망으로 복귀 p3
    st4 = dict(DEFAULT_STATE, last_weekly="2026-W44", zone_sent=B1)
    al = run(st4, {"pltr": b_wait, "qqq": None}, now)
    check("back to WAIT p3", [(a[1].endswith("관망"), a[3]) for a in al], [(True, 3)])

    # 주간 점검: ISO 주 1번, PLTR/QQQ 줄, 밸류에이션 줄 또는 생략 문구
    qb = [(d, 400 + i * 0.5) for i, (d, _) in enumerate(b_wait)]
    wnow = datetime(2026, 11, 2, 9, 0, tzinfo=KST)  # 월요일 W45
    st5 = dict(DEFAULT_STATE, last_weekly="2026-W44", zone_sent=WAIT)
    al = run(st5, {"pltr": b_wait, "qqq": qb, "valuation": {"fwd_pe": 84.2, "trail_pe": 169.7, "ps": 76.8}}, wnow)
    wk = [a for a in al if "주간 점검" in a[1]]
    check("weekly once p2", [(a[0], a[3]) for a in wk], [("port", 2)])
    import re as _re
    check("weekly PLTR/QQQ 1Y line", bool(wk and _re.search(r"PLTR 1년 [+-]\d+\.\d%, QQQ 1년 [+-]\d+\.\d%, 차이 [+-]\d+\.\d%p", wk[0][2])))
    check("weekly valuation line", bool(wk and "선행 PER 84.2배" in wk[0][2] and "PSR 76.8배" in wk[0][2]))
    check("weekly earnings reference before 11/13", bool(wk and "[PLTR] 알림" in wk[0][2]))
    al = run(st5, {"pltr": b_wait, "qqq": qb, "valuation": None}, wnow + timedelta(days=1))
    check("weekly not repeated same ISO week", [a for a in al if "주간 점검" in a[1]], [])
    st6 = dict(DEFAULT_STATE, last_weekly="2026-W45", zone_sent=WAIT)
    al = run(st6, {"pltr": b_wait, "qqq": None, "valuation": None}, datetime(2026, 11, 17, 9, 0, tzinfo=KST))  # 화요일 W47
    wk = [a for a in al if "주간 점검" in a[1]]
    check("weekly on first run of week (Tue)", len(wk), 1)
    check("valuation omitted message", bool(wk and "밸류에이션: Yahoo 조회 실패, 이번 주는 생략" in wk[0][2]))
    check("QQQ fail line", bool(wk and "QQQ 조회 실패" in wk[0][2]))
    check("no earnings line after 11/13", bool(wk and "[PLTR] 알림" not in wk[0][2]))

    # 조회 실패: ops 하루 1번, 구간 유지
    st7 = dict(DEFAULT_STATE, last_weekly="2026-W44", zone_sent=B1, zone=B1)
    al = run(st7, {"pltr": None, "qqq": None}, now)
    check("fetch fail -> ops", [(a[0], a[1]) for a in al], [("ops", FAIL_TITLE)])
    check("fail title routes cwally-ops", route_topic("cwally-port", FAIL_TITLE), "cwally-ops")
    check("zone kept on failure", (st7["zone"], st7["zone_sent"]), (B1, B1))
    check("fail not repeated same day", run(st7, {"pltr": None, "qqq": None}, now), [])
    check("short history -> ops fail", [a[0] for a in run(dict(DEFAULT_STATE), {"pltr": _bars(base[:100])}, now)], ["ops"])

    # 파서
    t0 = int(datetime(2026, 10, 27, 13, 30, tzinfo=timezone.utc).timestamp())
    t1 = int(datetime(2026, 10, 28, 13, 30, tzinfo=timezone.utc).timestamp())
    yj = json.dumps({"chart": {"result": [{"meta": {"currentTradingPeriod": {"regular": {"start": t1, "end": t1 + 23400}}},
                                           "timestamp": [t0, t1], "indicators": {"quote": [{"close": [198.73, 196.0]}]}}]}})
    check("yahoo drops intraday bar", [d.isoformat() for d, _ in parse_yahoo(yj, now_ts=t1 + 3600)], ["2026-10-27"])
    check("yahoo keeps closed bar", len(parse_yahoo(yj, now_ts=t1 + 30000)), 2)
    qs = {"quoteSummary": {"result": [{"summaryDetail": {"forwardPE": {"raw": 84.2}, "trailingPE": {"raw": 169.7},
                                                        "priceToSalesTrailing12Months": {"raw": 76.8}}, "defaultKeyStatistics": {}}]}}
    check("parse valuation", parse_valuation(qs), {"fwd_pe": 84.2, "trail_pe": 169.7, "ps": 76.8})
    check("parse valuation empty", parse_valuation({"quoteSummary": {"result": [{}]}}), None)

    # 금지 문자, 본문 순서
    copies.extend(f"{k}{v}" for k, v in ZONE_COPY.items())
    check("no middle dot / em-dash", [c[:40] for c in copies if banned_in(c)], [])
    check("body six-item order", [ln.split(":")[0] for ln in body("s", "a", "b", "c", "d", "e").splitlines()[1:]],
          ["이게 뭐 알림", "지금 결과/상태", "지금 할 일", "나중에", "익절/줄일 때", "하지 말 것"])

    for f in fails:
        print("FAIL", f)
    print(f"selftest {ok}/{ok + len(fails)}")
    return 0 if not fails else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        sys.exit(1)
