#!/usr/bin/env python3
"""SAMG엔터 419530: conditional growth thesis check (2026-10-07 analysis).

Slot: existing weekday 09:00 morning bundle. No new cron.
Sends to cwally-port only when a condition changes, plus one weekly summary on the
first successful run of each ISO week. Lookup problems go to cwally-ops and block any
trading line for that run.

Conditions (configs/samg26.json):
  price band   : close < 20,000 / 20,000~21,000 (review) / > 21,000, on the last completed KRX session
  earnings gate: FY2026 consensus EPS >= 2,048 and OP >= 233억 (Naver FnGuide, isConsensus=Y)
  estimate move: EPS or OP moves >= 5% vs the last confirmed reference
  new actuals  : Naver quarter column turns from consensus to actual
  disclosures  : new earnings / capital / supply-contract notices (KRX notice list)
CFO is never auto-extracted. A new report only produces "CFO 수동 확인 필요".
Price alone never produces a buy line. Excluded source: samg.net PR/news pages.

Modes: auto (default), dry (no state, no send), self-test (offline asserts),
install-test (one cwally-port install confirmation, explicitly not a trading signal).
"""
from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from kr_calendar import is_trading_day, prev_trading_day  # noqa: E402
from naver_api import get_json, notices, stock_trend  # noqa: E402
from ntfy_util import publish, resolve_server  # noqa: E402

KST = timezone(timedelta(hours=9))
CFG_PATH = ROOT / "configs" / "samg26.json"
STATE_PATH = ROOT / "state" / "samg26.json"
CLOSE_READY = (20, 30)  # KRX trades until 20:00 since 2026-10; a same-day row counts only after 20:30 KST
EARN_WORDS = ("손익구조", "잠정", "결산실적", "기업설명회", "영업실적")
CAPITAL_WORDS = ("유상증자", "전환사채", "신주인수권", "교환사채", "자기주식")
US_WORDS = ("공급계약", "Target", "타겟", "TARGET")


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


def fetch_close(code: str, session: date) -> float | None:
    key = session.strftime("%Y%m%d")
    for r in stock_trend(code, 10):
        if r.get("day") == key:
            return r.get("close")
    return None


def fetch_latest_close(code: str, before: date) -> tuple[date, float] | None:
    key = before.strftime("%Y%m%d")
    rows = [r for r in stock_trend(code, 10) if r.get("day") and r["day"] <= key and r.get("close") is not None]
    if not rows:
        return None
    r = max(rows, key=lambda x: x["day"])
    return datetime.strptime(r["day"], "%Y%m%d").date(), r["close"]


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


def fetch_consensus(code: str, year: int) -> dict | None:
    cols, vals = finance(code, "annual")
    key = f"{year}12"
    if (key, True) not in cols:
        return None
    v = vals.get(key) or {}
    eps, op = v.get("EPS"), v.get("영업이익")
    if eps is None or op is None:
        return None
    return {"eps": eps, "op": op, "sales": v.get("매출액")}


def fetch_latest_actual(code: str) -> dict | None:
    cols, vals = finance(code, "quarter")
    actual = [k for k, is_cons in cols if not is_cons]
    if not actual:
        return None
    k = max(actual)
    v = vals.get(k) or {}
    nxt = [c for c, is_cons in cols if is_cons and c > k]
    nv = vals.get(min(nxt)) if nxt else None
    return {
        "key": k,
        "sales": v.get("매출액"),
        "op": v.get("영업이익"),
        "next_key": min(nxt) if nxt else None,
        "next_op": (nv or {}).get("영업이익"),
    }


def classify_notice(title: str, text: str) -> str | None:
    if any(w in title for w in US_WORDS) or any(w in text for w in ("Target", "타겟")):
        return "us"
    if any(w in title for w in EARN_WORDS):
        return "earnings"
    if any(w in title for w in CAPITAL_WORDS):
        return "capital"
    return None


# ---------- judgment (pure) ----------

def band_of(close: float, cfg: dict) -> str:
    lo, hi = cfg["review_price"]["lower"], cfg["review_price"]["upper"]
    if close < lo:
        return "below_review"
    if close <= hi:
        return "review"
    return "above_review"


BAND_KO = {"below_review": "20,000원 미만", "review": "20,000~21,000원 검토구간", "above_review": "21,000원 초과"}


def gate_of(cons: dict, cfg: dict) -> str:
    g = cfg["rules"]["unchanged_earnings_gate"]
    ok = cons["eps"] >= g["min_fy2026_eps_krw"] and cons["op"] >= g["min_fy2026_op_eok"]
    return "maintained" if ok else "broken"


def pct(new: float, old: float) -> float:
    return (new / old - 1) * 100 if old else 0.0


def action_line(band: str, gate: str) -> tuple[str, str]:
    """(title action, 지금 할 일)."""
    if gate == "broken":
        return ("실적 전망 하향 | 매수 검토 보류",
                "실적 유지 조건이 깨졌습니다. 가격이 싸 보여도 매수 검토를 멈추고 하향 이유부터 확인하세요.")
    if band == "review":
        return ("검토구간 진입 | 분할 검토 가능",
                "가격과 실적 조건이 함께 맞습니다. 한 번에 사지 말고 소액 분할만 검토하세요.")
    if band == "below_review":
        return ("20,000원 하회 | 원인 확인 전 보류",
                "검토구간 아래로 내려갔습니다. 실적 전망은 유지 중이지만 하락 원인을 확인한 뒤 판단하세요.")
    return ("검토가 위 | 추격매수 금지", "아직 검토가(20,000~21,000원)보다 높습니다. 관망하세요.")


def evaluate(cfg: dict, state: dict, obs: dict) -> dict:
    """obs: session(date), close, cons{eps,op,sales}, actual{...}|None, new_notices[list]|None, iso_week."""
    events: list[str] = []
    band = band_of(obs["close"], cfg)
    gate = gate_of(obs["cons"], cfg)
    ref = state.get("consensus_reference") or {}
    if state.get("price_band") and band != state["price_band"]:
        events.append(f"가격 구간 변경: {BAND_KO.get(state['price_band'], state['price_band'])} -> {BAND_KO[band]}")
    if state.get("earnings_gate") and gate != state["earnings_gate"]:
        events.append("실적 유지 조건: " + ("유지 -> 깨짐" if gate == "broken" else "깨짐 -> 회복"))
    lim = float(cfg["rules"]["estimate_change_pct"])
    est_moved = False
    eps_ref = ref.get("eps_krw", ref.get("eps"))
    op_ref = ref.get("op_eok", ref.get("op"))
    if eps_ref and op_ref:
        de, do = pct(obs["cons"]["eps"], eps_ref), pct(obs["cons"]["op"], op_ref)
        if abs(de) >= lim or abs(do) >= lim:
            est_moved = True
            events.append(f"2026 전망 변경: EPS {eps_ref:,.0f} -> {obs['cons']['eps']:,.0f}원 ({de:+.1f}%), "
                          f"영업이익 {op_ref:,.0f} -> {obs['cons']['op']:,.0f}억 ({do:+.1f}%)")
    act = obs.get("actual")
    if act and state.get("latest_actual_key") and act["key"] > state["latest_actual_key"]:
        op_txt = f"{act['op']:,.0f}억" if act.get("op") is not None else "숫자 미확인"
        sales_txt = f"{act['sales']:,.0f}억" if act.get("sales") is not None else "숫자 미확인"
        events.append(f"새 분기 실적 반영({act['key']}): 매출 {sales_txt}, 영업이익 {op_txt}. "
                      "영업현금흐름은 자동 확인하지 않습니다. DART 보고서로 수동 확인 필요")
    for n in obs.get("new_notices") or []:
        if n["kind"] == "us":
            events.append(f"미국/공급 관련 공시: {n['title']} ({n['datetime'][:10]}). 수량과 금액은 원문 확인 필요, 검증된 재주문으로 보지 마세요")
        elif n["kind"] == "earnings":
            events.append(f"실적 관련 공시: {n['title']} ({n['datetime'][:10]}). 영업현금흐름 수동 확인 필요")
        else:
            events.append(f"자본 변동 공시: {n['title']} ({n['datetime'][:10]}). 희석 여부 확인 필요")
    weekly_due = not (state.get("weekly_sent") or {}).get(obs["iso_week"])
    return {"band": band, "gate": gate, "events": events, "weekly_due": weekly_due, "est_moved": est_moved}


def build_message(cfg: dict, state: dict, obs: dict, res: dict, kind: str) -> tuple[str, list[str]]:
    b = cfg["baseline"]
    cons = obs["cons"]
    fpe = obs["close"] / cons["eps"] if cons["eps"] > 0 else None
    action, todo = action_line(res["band"], res["gate"])
    if kind == "install":
        title = f"{cfg['title_prefix']} 419530 설치 확인 | 매매 신호 아님"
        todo = ("가격 구간 판단은 다음 거래일 09:00 첫 정기 실행부터 시작합니다."
                if obs.get("stale") else todo)
    elif res["events"]:
        title = f"{cfg['title_prefix']} 419530 {action}"
    else:
        title = f"{cfg['title_prefix']} 419530 주간 점검 | {action.split(' | ')[-1]}"
    act = obs.get("actual") or {}
    cfo = state.get("latest_cfo_verified") or {}
    price_line = (f"직전 반영 종가 {obs['close']:,.0f}원 ({obs['session']:%Y-%m-%d}). 오늘 종가는 아직 데이터에 없어 구간 판단은 하지 않았습니다"
                  if obs.get("stale") else
                  f"종가 {obs['close']:,.0f}원 ({obs['session']:%Y-%m-%d} 완료 세션), 구간: {BAND_KO[res['band']]}")
    status = [
        price_line,
        f"2026 전망(Naver/FnGuide): EPS {cons['eps']:,.0f}원, 영업이익 {cons['op']:,.0f}억, 예상 PER "
        + (f"{fpe:.1f}배" if fpe else "계산 불가") + (" (직전 반영 종가 기준)" if obs.get("stale") else ""),
        "실적 유지 조건(EPS 2,048원, 영업이익 233억 이상): " + ("유지" if res["gate"] == "maintained" else "깨짐"),
        f"최근 확정 분기: {act.get('key', '미확인')} 영업이익 "
        + (f"{act['op']:,.0f}억" if act.get("op") is not None else "미확인")
        + (f" | 다음 분기({act['next_key']}) 컨센 영업이익 {act['next_op']:,.0f}억" if act.get("next_op") is not None else ""),
        f"영업현금흐름: {cfo.get('period', '미확인')} {cfo.get('value_eok', 0):+,.1f}억 ({cfo.get('report_date', '')} 보고서 기준, 최신값 아닐 수 있음)",
    ]
    lines = [
        "이게 뭐 알림: SAMG엔터 419530 조건부 성장 논리 점검. 2026-10-07 분석 기준. "
        + ("설치가 끝났는지 확인하는 1회 알림입니다." if kind == "install" else
           ("조건이 바뀌어서 보냅니다." if res["events"] else "매주 첫 거래일 정기 요약입니다.")),
        "지금 결과/상태:",
        *[f"- {s}" for s in status],
    ]
    if res["events"] and kind != "install":
        lines.append("바뀐 점:")
        lines += [f"- {e}" for e in res["events"]]
    lines += [
        "지금 할 일: " + ("없음. 이 알림은 설치 확인용이며 매매 신호가 아닙니다. " if kind == "install" else "") + todo,
        f"나중에: 하반기 영업이익이 약 {b['h2_requirement']['op_eok']:.0f}억(전년 대비 +{b['h2_requirement']['yoy_pct']:.0f}%) 나와야 2026 전망이 맞습니다. "
        "3분기 실적, 영업현금흐름 플러스 전환, 미국 Target 재주문 여부를 확인하세요.",
        "익절/줄일 때: 실적 유지 조건이 깨지거나, 해외 매출이 늘어도 이익률이 회복되지 않을 때 비중을 줄이세요.",
        "하지 말 것: 급등 추격, 가격만 보고 매수, 기사 제목만으로 재주문 확정 판단.",
        f"출처: {cfg['sources']['notice_page']} | 기준 보고서 {cfg['sources']['baseline_dart']}",
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
    title = f"{cfg['title_prefix']} 419530 SAMG엔터 {key} 조회 실패 | 판단 유보"
    lines = [
        "이게 뭐 알림: SAMG엔터 419530 정기 점검의 데이터 문제 안내.",
        f"지금 결과/상태: {detail}. {now:%Y-%m-%d %H:%M} KST. 이번 실행의 매매 판단은 막았습니다.",
        "지금 할 일: 없음. 다음 거래일에 자동 재시도합니다.",
        "나중에: 이틀 이상 계속되면 Naver API 변경 여부를 확인하세요.",
        "익절/줄일 때: 이 알림만으로 판단하지 마세요.",
        "하지 말 것: 누락된 숫자를 0이나 예전 값으로 보고 매매.",
    ]
    if send(cfg, title, lines, dry, ops=True, priority=2) and not dry:
        ops_daily[key] = day


# ---------- run ----------

def observe(cfg: dict, state: dict, now: datetime, install: bool = False) -> tuple[dict | None, str | None]:
    session = expected_session(now)
    stale = False
    try:
        close = fetch_close(cfg["ticker"], session)
        if close is None and install:
            got = fetch_latest_close(cfg["ticker"], now.date())
            if got:
                session, close = got
        if install:
            stale = session != now.date()  # install message never judges a price band on an older close
    except Exception as exc:
        return None, f"종가|{type(exc).__name__}"
    if close is None:
        return None, f"종가|{session:%Y-%m-%d} 완료 세션 종가가 아직 없습니다"
    try:
        cons = fetch_consensus(cfg["ticker"], int(cfg["baseline_year"]))
    except Exception as exc:
        return None, f"컨센서스|{type(exc).__name__}"
    if cons is None:
        return None, "컨센서스|2026.12 컨센서스 EPS 또는 영업이익이 없습니다"
    try:
        actual = fetch_latest_actual(cfg["ticker"])
    except Exception:
        actual = None
    new_notices, all_ids = None, None
    try:
        rows = notices(cfg["ticker"], 50)
        seen = set(state.get("seen_notice_ids") or [])
        all_ids = [r["id"] for r in rows]
        if state.get("notices_seeded"):
            new_notices = []
            for r in rows:
                if r["id"] in seen:
                    continue
                k = classify_notice(r["title"], r.get("text", ""))
                if k:
                    new_notices.append({"id": r["id"], "title": r["title"].replace("(주)에스에이엠지엔터테인먼트", "").strip(),
                                        "datetime": r["datetime"], "kind": k})
    except Exception as exc:
        print(f"notice fetch failed: {type(exc).__name__}")
    iso = now.isocalendar()
    return {"session": session, "close": close, "cons": cons, "actual": actual, "stale": stale,
            "new_notices": new_notices, "all_notice_ids": all_ids,
            "iso_week": f"{iso[0]}-W{iso[1]:02d}"}, None


def apply_state(state: dict, obs: dict, res: dict, weekly: bool) -> None:
    state["price_band"] = res["band"]
    state["earnings_gate"] = res["gate"]
    if res["est_moved"] or not state.get("consensus_reference"):
        state["consensus_reference"] = {"year": 2026, "eps_krw": obs["cons"]["eps"], "op_eok": obs["cons"]["op"],
                                        "sales_eok": obs["cons"].get("sales"), "as_of": obs["session"].isoformat(),
                                        "source": "Naver FnGuide 컨센서스"}
    if obs.get("actual"):
        state["latest_actual_key"] = max(state.get("latest_actual_key") or "", obs["actual"]["key"])
    if obs.get("all_notice_ids") is not None:
        state["seen_notice_ids"] = list(dict.fromkeys((obs["all_notice_ids"] or []) + (state.get("seen_notice_ids") or [])))[:300]
        state["notices_seeded"] = True
    if weekly:
        state.setdefault("weekly_sent", {})[obs["iso_week"]] = obs["session"].isoformat()
        state["weekly_sent"] = dict(sorted(state["weekly_sent"].items())[-12:])
    state["last_observation"] = {"session": obs["session"].isoformat(), "close": obs["close"],
                                 "eps": obs["cons"]["eps"], "op": obs["cons"]["op"]}


def run(mode: str, now: datetime | None = None) -> int:
    now = (now or datetime.now(KST)).astimezone(KST)
    cfg = read_json(CFG_PATH)
    state = read_json(STATE_PATH)
    dry = mode == "dry"
    install = mode == "install-test"
    if now.year != int(cfg.get("baseline_year", 0)):
        if not state.get("year_guard_sent") and not dry:
            problem(cfg, state, now, "기준연도", "2026 기준값이라 새 연도 판단을 멈췄습니다. config 갱신 필요", dry)
            state["year_guard_sent"] = True
            save_state(state, now)
        print("year guard: silent")
        return 0
    if not install and not is_trading_day(now.date()):
        print("not a KRX trading day; silent")
        return 0
    obs, err = observe(cfg, state, now, install)
    if err:
        key, detail = err.split("|", 1)
        problem(cfg, state, now, key, detail, dry)
        if not dry:
            save_state(state, now)
        return 0
    res = evaluate(cfg, state, obs)
    if install:
        title, lines = build_message(cfg, state, obs, res, "install")
        ok = send(cfg, title, lines, False)
        if ok:
            # install only seeds the notice list; band, gate and weekly start with the first slot run
            state["install_test_sent"] = now.isoformat(timespec="seconds")
            if obs.get("all_notice_ids") is not None:
                state["seen_notice_ids"] = list(dict.fromkeys(obs["all_notice_ids"]))[:300]
                state["notices_seeded"] = True
            save_state(state, now)
        return 0 if ok else 1
    if not res["events"] and not res["weekly_due"]:
        print(f"no change; silent (close={obs['close']}, band={res['band']}, gate={res['gate']})")
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
    base = {"price_band": "above_review", "earnings_gate": "maintained", "latest_actual_key": "202606",
            "consensus_reference": {"eps_krw": 2048, "op_eok": 233}, "weekly_sent": {"2026-W41": "2026-10-07"}}
    def obs(close=23000, eps=2048, op=233, akey="202606", notes=None, week="2026-W41"):
        return {"session": date(2026, 10, 7), "close": close, "cons": {"eps": eps, "op": op, "sales": 1645},
                "actual": {"key": akey, "sales": 375, "op": 21, "next_key": "202609", "next_op": 28},
                "new_notices": notes or [], "iso_week": week}
    r = evaluate(cfg, base, obs())
    assert not r["events"] and not r["weekly_due"], "unchanged must be silent"
    r = evaluate(cfg, base, obs(close=20500))
    assert r["band"] == "review" and r["events"], "review band entry"
    assert action_line(r["band"], r["gate"])[0].startswith("검토구간"), "review action"
    assert band_of(20000, cfg) == "review" and band_of(21000, cfg) == "review" and band_of(19990, cfg) == "below_review"
    r = evaluate(cfg, base, obs(close=20500, eps=1900))
    assert r["gate"] == "broken" and "보류" in action_line(r["band"], r["gate"])[0], "gate blocks review buy"
    r = evaluate(cfg, base, obs(eps=2160))
    assert r["est_moved"], ">=5% estimate move"
    r = evaluate(cfg, base, obs(eps=2100))
    assert not r["est_moved"] and not r["events"], "<5% silent"
    r = evaluate(cfg, base, obs(akey="202609"))
    assert any("새 분기" in e and "수동 확인" in e for e in r["events"]), "new actual asks manual CFO"
    r = evaluate(cfg, base, obs(notes=[{"id": "1", "title": "단일판매ㆍ공급계약체결", "datetime": "2026-10-20", "kind": "us"}]))
    assert any("검증된 재주문으로 보지" in e for e in r["events"]), "US notice not treated as verified"
    r = evaluate(cfg, base, obs(week="2026-W42"))
    assert r["weekly_due"] and not r["events"], "weekly summary due"
    assert classify_notice("(주)에스에이엠지엔터테인먼트 기업설명회(IR) 개최", "") == "earnings"
    assert classify_notice("주식매수선택권행사", "") is None
    assert expected_session(datetime(2026, 10, 8, 9, 0, tzinfo=KST)) == date(2026, 10, 7)
    assert expected_session(datetime(2026, 10, 13, 9, 0, tzinfo=KST)) == date(2026, 10, 12), "skips 10-09 holiday"
    assert expected_session(datetime(2026, 10, 7, 16, 0, tzinfo=KST)) == date(2026, 10, 6), "same day only after 20:30"
    assert expected_session(datetime(2026, 10, 7, 21, 0, tzinfo=KST)) == date(2026, 10, 7)
    assert not is_trading_day(date(2026, 10, 9))
    t, lines = build_message(cfg, {"latest_cfo_verified": {"period": "2026H1", "value_eok": -5.1, "report_date": "2026-08-14"}},
                             obs(close=20500), evaluate(cfg, base, obs(close=20500)), "event")
    body = "\n".join([t, *lines])
    for head in ("이게 뭐 알림", "지금 결과/상태", "지금 할 일", "나중에", "익절/줄일 때", "하지 말 것"):
        assert head in body, head
    assert "\u00b7" not in body and "\u2014" not in body, "no middle dot or em dash"
    assert t.startswith("[SAMG]")
    print("self-test ok")
    return 0


if __name__ == "__main__":
    m = sys.argv[1] if len(sys.argv) > 1 else "auto"
    if m == "self-test":
        sys.exit(self_test())
    if m not in {"auto", "dry", "install-test"}:
        print(f"unknown mode: {m}")
        sys.exit(2)
    sys.exit(run(m))
