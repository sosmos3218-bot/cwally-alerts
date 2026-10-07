#!/usr/bin/env python3
"""LG생활건강우 051905: 관망 조건 점검 (2026-10-07 분석 기준).

Slot: existing weekday 09:00 morning bundle. No new cron.
Sends to cwally-port only when a condition changes, plus one weekly summary on the first
successful run of each ISO week. Lookup problems go to cwally-ops and block any trading
line for that run.

Conditions (configs/lghpref.json, from the 2026-10-07 analysis):
  price zone : preferred close <= 90,000원 or discount to 051900 >= 67%   (info only, never a buy signal alone)
  liquidity  : median of last 20 sessions' trade value (close x volume) >= 10억  -> liq_ok
               average volume of last 5 sessions < 1,000주                     -> liq_dry (avoid)
  3Q results : Naver quarter column 202609 turns from consensus to actual.
               OP >= 1,057억 ok, OP < 800억 shock, otherwise mid.
  dividend   : FY2026 consensus DPS (common) < 2,100원 -> div_risk (avoid)
  opinion    : avoid if q3 shock / liq_dry / div_risk; split candidate only if q3 ok AND liq_ok; else watch.
  notices    : new KRX earnings / dividend / buyback notices for 051900 (subsidiary items excluded)
  estimate   : 3Q consensus OP moves >= 5% while results are pending

Modes: auto (default), dry (no state, no send), self-test (offline asserts).
There is deliberately no install-test mode: a live test send needs chat approval.
"""
from __future__ import annotations

import json
import statistics
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from kr_calendar import is_trading_day, prev_trading_day  # noqa: E402
from naver_api import get_json, notices, stock_trend  # noqa: E402
from ntfy_util import publish, resolve_server  # noqa: E402

KST = timezone(timedelta(hours=9))
CFG_PATH = ROOT / "configs" / "lghpref.json"
STATE_PATH = ROOT / "state" / "lghpref.json"
CLOSE_READY = (20, 30)  # KRX trades until 20:00 since 2026-10; a same-day row counts only after 20:30 KST
EARN_WORDS = ("잠정", "결산실적공시", "영업실적", "손익구조")
DIV_WORDS = ("배당",)
BUYBACK_WORDS = ("자기주식", "소각")
OPINION_KO = {"watch": "관망", "split": "분할 검토 후보", "avoid": "회피"}
OPINION_TITLE = {
    "watch": "관망 유지 | 신규매수 보류",
    "split": "분할 검토 후보 | 소액 지정가만",
    "avoid": "회피 조건 충족 | 신규매수 금지",
}
Q3_KO = {"pending": "발표 전", "ok": "양호", "mid": "중간", "shock": "쇼크"}


# ---------- state ----------

def read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state: dict, now: datetime) -> None:
    state["updated_at"] = now.isoformat(timespec="seconds")
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(STATE_PATH)


# ---------- data ----------

def num(raw) -> float | None:
    if raw is None:
        return None
    s = str(raw).replace(",", "").strip()
    if s in {"", "-", "N/A"}:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def expected_session(now: datetime) -> date:
    d = now.date()
    if is_trading_day(d) and (now.hour, now.minute) >= CLOSE_READY:
        return d
    return prev_trading_day(d)


def fetch_rows(code: str, session: date, size: int = 30) -> list[dict]:
    """Rows newest first, only sessions <= `session`, close and volume both numeric."""
    key = session.strftime("%Y%m%d")
    rows = [r for r in stock_trend(code, size)
            if r.get("day") and r["day"] <= key and r.get("close") is not None and r.get("volume") is not None]
    return sorted(rows, key=lambda r: r["day"], reverse=True)


def finance(code: str, period: str) -> tuple[list[tuple[str, bool]], dict]:
    raw = get_json(f"https://m.stock.naver.com/api/stock/{code}/finance/{period}")
    info = (raw or {}).get("financeInfo") or {}
    cols = [(str(t.get("key")), t.get("isConsensus") == "Y") for t in info.get("trTitleList") or []]
    vals: dict = {}
    for row in info.get("rowList") or []:
        title = row.get("title")
        for k, cell in (row.get("columns") or {}).items():
            vals.setdefault(k, {})[title] = num((cell or {}).get("value"))
    return cols, vals


def fetch_dps(code: str, year: int) -> float | None:
    cols, vals = finance(code, "annual")
    key = f"{year}12"
    if (key, True) not in cols:
        return None
    return (vals.get(key) or {}).get("주당배당금")


def fetch_q3(code: str, key: str) -> dict | None:
    """{'actual': bool, 'op': float|None, 'revenue': float|None}. None if the column is missing."""
    cols, vals = finance(code, "quarter")
    flags = dict(cols)
    if key not in flags:
        return None
    v = vals.get(key) or {}
    return {"actual": not flags[key], "op": v.get("영업이익"), "revenue": v.get("매출액")}


def classify_notice(title: str) -> str | None:
    if "종속회사" in title or "배당락" in title:
        return None
    if any(w in title for w in EARN_WORDS):
        return "earnings"
    if any(w in title for w in DIV_WORDS):
        return "dividend"
    if any(w in title for w in BUYBACK_WORDS):
        return "buyback"
    return None


# ---------- judgment (pure) ----------

def metrics(pref_rows: list[dict], common_close: float, rules: dict) -> dict | None:
    win = pref_rows[: int(rules["liq_window_sessions"])]
    dry_win = pref_rows[: int(rules["liq_dry_window_sessions"])]
    if len(win) < 15 or len(dry_win) < int(rules["liq_dry_window_sessions"]):
        return None
    pref_close = pref_rows[0]["close"]
    values = [r["close"] * r["volume"] / 1e8 for r in win]
    return {
        "pref_close": pref_close,
        "common_close": common_close,
        "discount_pct": (1 - pref_close / common_close) * 100,
        "liq_median_value_eok": statistics.median(values),
        "avg5_volume": sum(r["volume"] for r in dry_win) / len(dry_win),
        "last_volume": pref_rows[0]["volume"],
        "sessions": len(win),
    }


def q3_state_of(op: float | None, rules: dict) -> str:
    if op is None:
        return "pending"
    if op >= rules["q3_ok_op_eok"]:
        return "ok"
    if op < rules["q3_shock_op_eok"]:
        return "shock"
    return "mid"


def flags_of(cfg: dict, m: dict, q3_op: float | None, dps: float) -> dict:
    r = cfg["rules"]
    q3 = q3_state_of(q3_op, r)
    f = {
        "price_zone": m["pref_close"] <= r["price_zone_pref_krw"] or m["discount_pct"] >= r["discount_zone_pct"],
        "liq_ok": m["liq_median_value_eok"] >= r["liq_median_value_eok"],
        "liq_dry": m["avg5_volume"] < r["liq_dry_avg_volume"],
        "q3_state": q3,
        "div_risk": dps < r["dps_cut_below_krw"],
    }
    avoid = f["liq_dry"] or f["div_risk"] or q3 == "shock"
    split = q3 == "ok" and f["liq_ok"]
    f["opinion"] = "avoid" if avoid else "split" if split else "watch"
    return f


def pct(new: float, old: float) -> float:
    return (new / old - 1) * 100 if old else 0.0


def evaluate(cfg: dict, state: dict, obs: dict) -> dict:
    """obs: m (metrics), dps, q3_op, q3_cons_op, new_notices, iso_week."""
    f = flags_of(cfg, obs["m"], obs.get("q3_op"), obs["dps"])
    events: list[str] = []
    r = cfg["rules"]
    prev = state
    if prev.get("opinion") and f["opinion"] != prev["opinion"]:
        events.append(f"의견 변경: {OPINION_KO[prev['opinion']]} -> {OPINION_KO[f['opinion']]}")
    if prev.get("price_zone") is not None and f["price_zone"] != prev["price_zone"]:
        events.append("가격 구간: " + (f"진입 (우선주 {r['price_zone_pref_krw']:,}원 이하 또는 할인율 {r['discount_zone_pct']}% 이상). 가격만으로는 분할 신호가 아님"
                                       if f["price_zone"] else "이탈 (90,000원 위, 할인율 67% 미만)"))
    if prev.get("liq_ok") is not None and f["liq_ok"] != prev["liq_ok"]:
        events.append("유동성 조건(20세션 거래대금 중앙값 10억 이상): " + ("충족" if f["liq_ok"] else "미충족으로 되돌아감"))
    if prev.get("liq_dry") is not None and f["liq_dry"] != prev["liq_dry"]:
        events.append("거래 고갈(5세션 평균 1,000주 미만): " + ("발생. 매도가 어려울 수 있음" if f["liq_dry"] else "해소"))
    if prev.get("div_risk") is not None and f["div_risk"] != prev["div_risk"]:
        events.append("배당 위험(2026 DPS 컨센서스 2,100원 미만): " + ("발생" if f["div_risk"] else "해소"))
    if prev.get("q3_state") and f["q3_state"] != prev["q3_state"]:
        op = obs.get("q3_op")
        events.append(f"3분기 실적 반영: 영업이익 {op:,.0f}억 ({Q3_KO[f['q3_state']]}). "
                      "2분기에는 미국 관세 환급 일회성이 있었으니 3분기 일회성 여부를 원문으로 확인하세요"
                      if op is not None else f"3분기 실적 상태: {Q3_KO[f['q3_state']]}")
    est_moved = False
    ref = (prev.get("consensus_reference") or {}).get("q3_op_eok")
    cons = obs.get("q3_cons_op")
    if f["q3_state"] == "pending" and ref and cons is not None:
        d = pct(cons, ref)
        if abs(d) >= float(r["estimate_change_pct"]):
            est_moved = True
            events.append(f"3분기 영업이익 컨센서스 변경: {ref:,.0f} -> {cons:,.0f}억 ({d:+.1f}%)")
    for n in obs.get("new_notices") or []:
        label = {"earnings": "실적 관련 공시", "dividend": "배당 관련 공시", "buyback": "자사주 관련 공시"}[n["kind"]]
        tail = "숫자는 원문 확인. 확정 영업이익은 Naver 반영 후 자동 판정" if n["kind"] == "earnings" else "금액과 우선주 포함 여부는 원문 확인"
        events.append(f"{label}: {n['title']} ({n['datetime'][:10]}). {tail}")
    weekly_due = not (prev.get("weekly_sent") or {}).get(obs["iso_week"])
    return {"flags": f, "events": events, "weekly_due": weekly_due, "est_moved": est_moved}


def todo_line(opinion: str) -> str:
    if opinion == "avoid":
        return "신규 매수를 하지 마세요. 보유 중이면 비중 축소를 검토하고 원인(실적, 배당, 거래 고갈)을 먼저 확인하세요."
    if opinion == "split":
        return "한 번에 사지 말고 소액 분할만 검토하세요. 호가가 얇으니 지정가로, 보통주 가격과 함께 보세요."
    return "신규 매수는 보류하세요. 이미 보유 중이면 배당과 3분기 실적 발표를 기다리며 유지 여부만 점검하세요."


def build_message(cfg: dict, state: dict, obs: dict, res: dict, kind: str) -> tuple[str, list[str]]:
    r = cfg["rules"]
    m, f = obs["m"], res["flags"]
    title = (f"{cfg['title_prefix']} {OPINION_TITLE[f['opinion']]}" if kind == "event"
             else f"{cfg['title_prefix']} 주간 점검 | {OPINION_KO[f['opinion']]}")
    q3_txt = ("확정 영업이익 " + f"{obs['q3_op']:,.0f}억 ({Q3_KO[f['q3_state']]}, 기준 {r['q3_ok_op_eok']:,}억 이상 양호, {r['q3_shock_op_eok']:,}억 미만 쇼크)"
              if obs.get("q3_op") is not None else
              "아직 발표 전. 컨센서스 영업이익 "
              + (f"{obs['q3_cons_op']:,.0f}억" if obs.get("q3_cons_op") is not None else "미확인")
              + f" (양호 {r['q3_ok_op_eok']:,}억 이상, 쇼크 {r['q3_shock_op_eok']:,}억 미만)")
    status = [
        f"우선주 {m['pref_close']:,.0f}원, 보통주 {m['common_close']:,.0f}원 ({obs['session']:%Y-%m-%d} 완료 세션). 할인율 {m['discount_pct']:.1f}%"
        f" ({'가격 구간 안' if f['price_zone'] else '가격 구간 밖'}: {r['price_zone_pref_krw']:,}원 이하 또는 {r['discount_zone_pct']}% 이상)",
        f"유동성: 최근 {m['sessions']}세션 거래대금 중앙값 {m['liq_median_value_eok']:.1f}억 (기준 {r['liq_median_value_eok']}억, "
        + ("충족" if f["liq_ok"] else "미충족") + f"), 5세션 평균 거래량 {m['avg5_volume']:,.0f}주 (고갈 기준 {r['liq_dry_avg_volume']:,}주 미만). 거래대금은 종가 x 거래량 추정",
        f"3분기: {q3_txt}",
        f"2026 배당: 보통주 DPS 컨센서스 {obs['dps']:,.0f}원, 보통주 DPS를 우선주 종가로 나눈 값 {obs['dps'] / m['pref_close'] * 100:.1f}% (추정, 위험 기준 {r['dps_cut_below_krw']:,}원 미만)",
        f"현재 의견: {OPINION_KO[f['opinion']]}",
    ]
    lines = [
        "이게 뭐 알림: LG생활건강우 051905 관망 조건 점검. 2026-10-07 분석(의견 관망) 기준. "
        + ("조건이 바뀌어서 보냅니다." if kind == "event" else "매주 첫 거래일 정기 요약입니다."),
        "지금 결과/상태:",
        *[f"- {s}" for s in status],
    ]
    if res["events"]:
        lines.append("바뀐 점:")
        lines += [f"- {e}" for e in res["events"]]
    lines += [
        "지금 할 일: " + todo_line(f["opinion"]),
        "나중에: 3분기 실적 발표일은 공시 전이라 날짜를 확정하지 않았습니다. 실적 공시 예고가 뜨면 알립니다. "
        "분할 검토는 3분기 영업이익 1,057억 이상과 유동성 조건이 함께 맞을 때만 나옵니다.",
        "익절/줄일 때: 보유 중이면 3분기 영업이익 800억 미만, 배당 컷 신호, 거래 고갈 중 하나가 나오면 비중 축소를 검토하세요.",
        "하지 말 것: 할인율이 크다는 이유만으로 매수, 호가가 얇은데 시장가 주문, 보통주 PER과 PBR을 우선주 가치로 착각.",
        f"출처: {cfg['sources']['notice_page']} (Naver FnGuide 컨센서스, KRX 공시)",
    ]
    return title, lines


# ---------- sending ----------

def send(cfg: dict, title: str, lines: list[str], dry: bool, ops: bool = False, priority: int = 3) -> bool:
    topic = "cwally-ops" if ops else "cwally-port"
    if dry:
        print(f"[dry] topic={topic}", title, *lines, "dry-run; no ntfy", sep="\n")
        return True
    try:
        publish(topic, title, "\n".join(lines), priority, ["warning"] if ops else ["chart_with_upwards_trend"],
                resolve_server(cfg.get("ntfy_server")))
        return True
    except Exception as exc:  # keep state unchanged so the next run retries
        print(f"publish failed: {type(exc).__name__}")
        return False


def problem(cfg: dict, state: dict, now: datetime, key: str, detail: str, dry: bool) -> None:
    day = now.strftime("%Y-%m-%d")
    ops_daily = state.setdefault("ops_daily", {})
    if ops_daily.get(key) == day and not dry:
        print(f"ops already sent today: {key}")
        return
    title = f"{cfg['title_prefix']} LG생활건강우 {key} 조회 실패 | 판단 유보"
    lines = [
        "이게 뭐 알림: LG생활건강우 051905 정기 점검의 데이터 문제 안내.",
        f"지금 결과/상태: {detail}. {now:%Y-%m-%d %H:%M} KST. 이번 실행의 의견 판단은 막았습니다.",
        "지금 할 일: 없음. 다음 거래일에 자동 재시도합니다.",
        "나중에: 이틀 이상 계속되면 Naver API 변경 여부를 확인하세요.",
        "익절/줄일 때: 이 알림만으로 판단하지 마세요.",
        "하지 말 것: 누락된 숫자를 0이나 예전 값으로 보고 매매.",
    ]
    if send(cfg, title, lines, dry, ops=True, priority=2) and not dry:
        ops_daily[key] = day


# ---------- run ----------

def observe(cfg: dict, state: dict, now: datetime) -> tuple[dict | None, str | None]:
    session = expected_session(now)
    key = session.strftime("%Y%m%d")
    try:
        pref = fetch_rows(cfg["ticker"], session)
        common = fetch_rows(cfg["ticker_common"], session, 5)
    except Exception as exc:
        return None, f"시세|{type(exc).__name__}"
    if not pref or pref[0]["day"] != key:
        return None, f"시세|우선주 {session:%Y-%m-%d} 완료 세션 종가가 아직 없습니다"
    if not common or common[0]["day"] != key:
        return None, f"시세|보통주 {session:%Y-%m-%d} 완료 세션 종가가 아직 없습니다"
    m = metrics(pref, common[0]["close"], cfg["rules"])
    if m is None:
        return None, "시세|유동성 계산에 필요한 세션 수가 부족합니다"
    try:
        dps = fetch_dps(cfg["ticker_common"], int(cfg["baseline_year"]))
    except Exception as exc:
        return None, f"컨센서스|{type(exc).__name__}"
    if dps is None:
        return None, "컨센서스|2026.12 주당배당금 컨센서스가 없습니다"
    try:
        q3 = fetch_q3(cfg["ticker_common"], str(cfg["rules"].get("q3_key", "202609")))
    except Exception as exc:
        return None, f"실적|{type(exc).__name__}"
    if q3 is None:
        return None, "실적|3분기 열이 Naver 분기 실적표에 없습니다"
    q3_op = q3["op"] if q3["actual"] and q3["op"] is not None else None
    q3_cons_op = None if q3["actual"] else q3["op"]
    new_notices, all_ids = None, None
    try:
        rows = notices(cfg["ticker_common"], 50)
        seen = set(state.get("seen_notice_ids") or [])
        all_ids = [r["id"] for r in rows]
        if state.get("notices_seeded"):
            new_notices = []
            for r in rows:
                if r["id"] in seen:
                    continue
                k = classify_notice(r["title"])
                if k:
                    new_notices.append({"id": r["id"], "title": r["title"].replace("(주)LG생활건강", "").strip(),
                                        "datetime": r["datetime"], "kind": k})
    except Exception as exc:
        print(f"notice fetch failed: {type(exc).__name__}")
    iso = now.isocalendar()
    return {"session": session, "m": m, "dps": dps, "q3_op": q3_op, "q3_cons_op": q3_cons_op,
            "new_notices": new_notices, "all_notice_ids": all_ids,
            "iso_week": f"{iso[0]}-W{iso[1]:02d}"}, None


def apply_state(state: dict, obs: dict, res: dict, weekly: bool) -> None:
    f = res["flags"]
    for k in ("opinion", "price_zone", "liq_ok", "liq_dry", "q3_state", "div_risk"):
        state[k] = f[k]
    if obs.get("q3_cons_op") is not None and (res["est_moved"] or not (state.get("consensus_reference") or {}).get("q3_op_eok")):
        state["consensus_reference"] = {"q3_op_eok": obs["q3_cons_op"], "as_of": obs["session"].isoformat(),
                                        "source": "Naver FnGuide 컨센서스"}
    if obs.get("all_notice_ids") is not None:
        state["seen_notice_ids"] = list(dict.fromkeys((obs["all_notice_ids"] or []) + (state.get("seen_notice_ids") or [])))[:300]
        state["notices_seeded"] = True
    if weekly:
        state.setdefault("weekly_sent", {})[obs["iso_week"]] = obs["session"].isoformat()
        state["weekly_sent"] = dict(sorted(state["weekly_sent"].items())[-12:])
    m = obs["m"]
    state["last_observation"] = {"session": obs["session"].isoformat(), "pref_close": m["pref_close"],
                                 "common_close": m["common_close"], "discount_pct": round(m["discount_pct"], 2),
                                 "liq_median_value_eok": round(m["liq_median_value_eok"], 2),
                                 "avg5_volume": round(m["avg5_volume"]), "dps": obs["dps"]}


def run(mode: str, now: datetime | None = None) -> int:
    now = (now or datetime.now(KST)).astimezone(KST)
    cfg = read_json(CFG_PATH)
    state = read_json(STATE_PATH)
    dry = mode == "dry"
    if now.year != int(cfg.get("baseline_year", 0)):
        if not state.get("year_guard_sent") and not dry:
            problem(cfg, state, now, "기준연도", "2026 기준값이라 새 연도 판단을 멈췄습니다. config 갱신 필요", dry)
            state["year_guard_sent"] = True
            save_state(state, now)
        print("year guard: silent")
        return 0
    if not is_trading_day(now.date()):
        print("not a KRX trading day; silent")
        return 0
    obs, err = observe(cfg, state, now)
    if err:
        key, detail = err.split("|", 1)
        problem(cfg, state, now, key, detail, dry)
        if not dry:
            save_state(state, now)
        return 0
    res = evaluate(cfg, state, obs)
    f = res["flags"]
    if not res["events"] and not res["weekly_due"]:
        print(f"no change; silent (opinion={f['opinion']}, q3={f['q3_state']}, price_zone={f['price_zone']}, liq_ok={f['liq_ok']})")
        if not dry:
            apply_state(state, obs, res, weekly=False)
            save_state(state, now)
        return 0
    title, lines = build_message(cfg, state, obs, res, "event" if res["events"] else "weekly")
    pri = 4 if res["events"] else 3
    if send(cfg, title, lines, dry, priority=pri) and not dry:
        apply_state(state, obs, res, weekly=True)
        save_state(state, now)
    return 0


# ---------- self-test ----------

def self_test() -> int:
    cfg = read_json(CFG_PATH)
    r = cfg["rules"]

    def rows(vols, close=93000):
        d = date(2026, 10, 6)
        out = []
        for v in vols:
            out.append({"day": d.strftime("%Y%m%d"), "close": close, "volume": v})
            d = prev_trading_day(d)
        return out

    base_vols = [5709, 7417, 2410, 14776, 24968, 17690, 7581, 17681, 15024, 44836,
                 6570, 7800, 13050, 7378, 4377, 5086, 6866, 2488, 8127, 6513]
    m = metrics(rows(base_vols), 267000, r)
    assert m and abs(m["discount_pct"] - 65.17) < 0.05, "discount from 10-06 closes"
    assert 6.5 < m["liq_median_value_eok"] < 8.5, "median trade value about 7억, below the 10억 gate"
    assert m["avg5_volume"] > 1000

    def obs(m=m, q3_op=None, cons=1057.0, dps=3019.0, notes=None, week="2026-W41"):
        return {"session": date(2026, 10, 6), "m": m, "dps": dps, "q3_op": q3_op, "q3_cons_op": cons,
                "new_notices": notes or [], "iso_week": week}

    base = {"opinion": "watch", "price_zone": False, "liq_ok": False, "liq_dry": False, "q3_state": "pending",
            "div_risk": False, "consensus_reference": {"q3_op_eok": 1057.0}, "weekly_sent": {"2026-W41": "2026-10-06"}}
    res = evaluate(cfg, base, obs())
    assert not res["events"] and not res["weekly_due"], "unchanged must be silent"
    assert res["flags"]["opinion"] == "watch"

    # first run: nothing stored, weekly summary only
    res = evaluate(cfg, {}, obs())
    assert not res["events"] and res["weekly_due"], "first run is a weekly summary"

    # price zone alone is not a split signal
    cheap = metrics(rows(base_vols, close=89000), 267000, r)
    res = evaluate(cfg, base, obs(m=cheap))
    assert res["flags"]["price_zone"] and res["flags"]["opinion"] == "watch" and any("가격 구간" in e for e in res["events"])
    zone67 = metrics(rows(base_vols, close=88000), 267000, r)
    assert flags_of(cfg, zone67, None, 3019)["price_zone"]

    # q3 ok but illiquid -> still watch; q3 ok and liquid -> split
    res = evaluate(cfg, base, obs(q3_op=1100))
    assert res["flags"]["q3_state"] == "ok" and res["flags"]["opinion"] == "watch"
    liquid = dict(m, liq_median_value_eok=12.0)
    res = evaluate(cfg, base, obs(m=liquid, q3_op=1100))
    assert res["flags"]["opinion"] == "split" and any("의견 변경" in e for e in res["events"])
    assert any("관세 환급" in e for e in res["events"]), "q3 event reminds about the 2Q one-off"
    assert q3_state_of(1056.9, r) == "mid" and q3_state_of(799.9, r) == "shock" and q3_state_of(1057, r) == "ok"

    # avoid triggers
    res = evaluate(cfg, base, obs(q3_op=700))
    assert res["flags"]["opinion"] == "avoid"
    dry = dict(m, avg5_volume=900)
    assert evaluate(cfg, base, obs(m=dry))["flags"]["opinion"] == "avoid"
    assert evaluate(cfg, base, obs(dps=2000))["flags"]["opinion"] == "avoid"
    assert evaluate(cfg, dict(base, opinion="avoid"), obs(dps=3019))["flags"]["opinion"] == "watch"

    # estimate move and notices
    res = evaluate(cfg, base, obs(cons=1000.0))
    assert res["est_moved"] and any("컨센서스 변경" in e for e in res["events"])
    res = evaluate(cfg, base, obs(cons=1040.0))
    assert not res["est_moved"], "<5% silent"
    assert classify_notice("(주)LG생활건강 연결재무제표기준영업(잠정)실적(공정공시)") == "earnings"
    assert classify_notice("(주)LG생활건강 결산실적공시 예고(안내공시)") == "earnings"
    assert classify_notice("(주)LG생활건강 현금ㆍ현물 배당 결정") == "dividend"
    assert classify_notice("(주)LG생활건강 중간(분기)배당락 기준가격 안내") is None
    assert classify_notice("(주)LG생활건강 유상증자결정(종속회사의 주요경영사항)") is None
    assert classify_notice("(주)LG생활건강 주식선물ㆍ주식옵션 2단계 가격제한폭 확대요건 도달(상승)") is None
    res = evaluate(cfg, base, obs(notes=[{"id": "1", "title": "연결재무제표기준영업(잠정)실적(공정공시)", "datetime": "2026-10-29T16:30:00", "kind": "earnings"}]))
    assert any("실적 관련 공시" in e for e in res["events"])
    assert evaluate(cfg, base, obs(week="2026-W42"))["weekly_due"]

    # calendar and session
    assert expected_session(datetime(2026, 10, 8, 9, 0, tzinfo=KST)) == date(2026, 10, 7)
    assert expected_session(datetime(2026, 10, 13, 9, 0, tzinfo=KST)) == date(2026, 10, 12), "skips 10-09 holiday"
    assert expected_session(datetime(2026, 10, 7, 16, 0, tzinfo=KST)) == date(2026, 10, 6), "same day only after 20:30"

    # message format
    title, lines = build_message(cfg, base, obs(m=liquid, q3_op=1100), evaluate(cfg, base, obs(m=liquid, q3_op=1100)), "event")
    body = "\n".join([title, *lines])
    for head in ("이게 뭐 알림", "지금 결과/상태", "지금 할 일", "나중에", "익절/줄일 때", "하지 말 것"):
        assert head in body, head
    assert "꺼지는 때" not in body, "ntfy_util appends the end line"
    assert "\u00b7" not in body and "\u2014" not in body, "no middle dot or em dash"
    assert title.startswith("[051905]")
    t2, l2 = build_message(cfg, base, obs(), evaluate(cfg, {}, obs()), "weekly")
    assert t2 == "[051905] 주간 점검 | 관망"
    assert "\u00b7" not in "\n".join(l2) and "\u2014" not in "\n".join(l2)
    print("self-test ok")
    return 0


if __name__ == "__main__":
    mode_arg = sys.argv[1] if len(sys.argv) > 1 else "auto"
    if mode_arg == "self-test":
        sys.exit(self_test())
    if mode_arg not in {"auto", "dry"}:
        print(f"unknown mode: {mode_arg}")
        sys.exit(2)
    sys.exit(run(mode_arg))
