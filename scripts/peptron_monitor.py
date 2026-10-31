#!/usr/bin/env python3
"""펩트론 087010: 릴리 SmartDepot 평가 종료 이후 공시 점검 (2026-10-07 분석).

Slot: existing weekday 09:00 morning bundle. No new cron.
Sends to cwally-port only when something new appears, never a weekly summary.
Lookup problems go to cwally-ops (once per day per kind) and never become a trade line.

What it watches (configs/peptron.json):
  lilly      : 릴리/SmartDepot/기술이전/기술평가 관련 새 거래소 공시
  financing  : 유상증자, 전환사채, 교환사채, 차입 등 자금조달 공시
  trial      : PT403 임상 승인 공시와 그 밖의 임상시험 공시
  capex      : 신규시설투자 공시와 정정
  quarter    : Naver finance/quarter 의 확정 분기 열이 늘어남 (새 분기 보고서 신호)
Naver 거래소 공시 목록에는 DART 정기보고서가 없다. 현금과 capex 잔여는 자동 추출하지 않고
"수동 확인 필요"로만 알린다.

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
from kr_calendar import is_trading_day  # noqa: E402
from naver_api import get_json, notices, num, stock_trend  # noqa: E402
from ntfy_util import publish, resolve_server  # noqa: E402

KST = timezone(timedelta(hours=9))
CFG_PATH = ROOT / "configs" / "peptron.json"
STATE_PATH = ROOT / "state" / "peptron.json"

PRIORITY = ["lilly", "financing", "trial_approval", "quarter", "capex", "trial", "major"]
HEAD = {
    "lilly": "릴리/기술이전 공시 | 원문 확인 먼저",
    "financing": "자금조달 공시 | 희석 확인 전 보류",
    "trial_approval": "임상 승인 공시 | 일정 확인",
    "quarter": "새 분기 실적 반영 | 현금 소진 점검",
    "capex": "시설투자 공시 | 금액과 기간 확인",
    "trial": "임상 관련 공시 | 원문 확인",
    "major": "새 주요 공시 | 원문 확인",
}
TODO = {
    "lilly": "원문에서 본계약, 연장, 종료 중 무엇인지 먼저 확인하세요. 확인 전에는 매수하지 마세요.",
    "financing": "희석 규모와 자금 용도를 원문에서 확인하세요. 확인 전에는 추가 매수를 보류하세요.",
    "trial_approval": "임상 시작 일정과 기간을 확인하세요. 1상은 안전성 확인용이라 가치 반영은 제한적입니다.",
    "quarter": "새 분기보고서에서 현금성 자산, 채무상품, 차입금, 시설투자 잔여를 직접 확인하세요. 이 알림은 숫자를 자동으로 읽지 않습니다.",
    "capex": "투자금액과 투자기간이 얼마나 바뀌었는지 확인하세요. 기간이 늘면 현금 부담이 뒤로 밀립니다.",
    "trial": "임상 일정이나 결과 변경인지 원문에서 확인하세요.",
    "major": "무슨 공시인지 원문에서 확인하세요. 제목만으로 판단하지 마세요.",
}


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

def clean_title(t: str) -> str:
    return t.replace("(주)펩트론", "").strip()


def classify(title: str, text: str, rules: dict) -> str | None:
    """Return a kind or None. Exchange noise (선물 가격제한폭, 투자주의 ...) is ignored."""
    if any(w in title for w in rules["title_exclude"]):
        return None
    if any(w in title for w in rules["lilly_words"]) or any(w in text for w in ("릴리", "Lilly", "SmartDepot", "스마트데포")):
        return "lilly"
    if any(w in title for w in rules["financing_words"]):
        return "financing"
    if "임상" in title:
        if "승인" in title and "승인신청" not in title.replace(" ", ""):
            return "trial_approval"
        return "trial"
    if any(w in title for w in rules["capex_words"]):
        return "capex"
    if rules["major_title"] in title:
        return "major"
    return None


def fetch_latest_actual(code: str) -> dict | None:
    raw = get_json(f"https://m.stock.naver.com/api/stock/{code}/finance/quarter")
    info = (raw or {}).get("financeInfo") or {}
    cols = [(str(t.get("key")), t.get("isConsensus") == "Y") for t in info.get("trTitleList") or []]
    actual = [k for k, is_cons in cols if not is_cons]
    if not actual:
        return None
    key = max(actual)
    op = None
    for row in info.get("rowList") or []:
        if row.get("title") == "영업이익":
            op = num(((row.get("columns") or {}).get(key) or {}).get("value"))
    return {"key": key, "op": op}


def fetch_close(code: str) -> tuple[str, float] | None:
    rows = [r for r in stock_trend(code, 5) if r.get("day") and r.get("close") is not None]
    if not rows:
        return None
    r = max(rows, key=lambda x: x["day"])
    return r["day"], r["close"]


# ---------- judgment (pure) ----------

def evaluate(cfg: dict, state: dict, rows: list[dict], actual: dict | None) -> dict:
    """rows: [{id,title,datetime,text}]. Returns events [{kind,text}] and ids to remember."""
    rules = cfg["rules"]
    seen = set(state.get("seen_notice_ids") or [])
    seeded = bool(state.get("notices_seeded"))
    events: list[dict] = []
    if seeded:
        for r in rows:
            if r["id"] in seen:
                continue
            kind = classify(r["title"], r.get("text", ""), rules)
            if kind:
                events.append({"kind": kind, "text": f"{clean_title(r['title'])} ({r['datetime'][:10]})"})
    if actual and state.get("latest_actual_key") and actual["key"] > state["latest_actual_key"]:
        op = f"{actual['op']:,.0f}억" if actual.get("op") is not None else "숫자 미확인"
        events.append({"kind": "quarter", "text": f"확정 분기 열 추가: {actual['key']} 영업이익 {op}"})
    return {"events": events, "ids": [r["id"] for r in rows]}


def top_kind(events: list[dict]) -> str:
    return min((e["kind"] for e in events), key=PRIORITY.index)


def build_message(cfg: dict, events: list[dict], close: tuple[str, float] | None, install: bool) -> tuple[str, list[str]]:
    b = cfg["baseline"]["h1_2026_report"]
    px = f"직전 종가 {close[1]:,.0f}원 ({close[0][:4]}-{close[0][4:6]}-{close[0][6:]})" if close else "종가 조회 안 됨"
    base = (f"기준값(2026-06-30 반기보고서): 현금성 {b['strict_liquid_eok']:.0f}억, 채무상품 {b['fvpl_debt_eok']:.0f}억, "
            f"차입금 {b['borrowings_eok']:.0f}억, 교환사채 {b['eb_face_eok']:.0f}억(조기상환 청구 {b['eb_put_from']}부터), "
            f"시설투자 잔여 {b['capex_remaining_eok']:.0f}억(종료 {b['ohsong2_end']})")
    if install:
        title = f"{cfg['title_prefix']} 087010 설치 확인 | 매매 신호 아님"
        intro = "이게 뭐 알림: 펩트론 087010 공시 점검이 설치됐는지 확인하는 1회 알림입니다. 매매 신호가 아닙니다."
        status = ["지금 결과/상태: 새 공시를 보면 이 형식으로 알립니다. 아직 감지된 변화는 없습니다.", f"- {px}"]
        todo = "지금 할 일: 없음. 평일 09:00에 새 공시가 있을 때만 알림이 옵니다."
    else:
        kind = top_kind(events)
        title = f"{cfg['title_prefix']} 087010 {HEAD[kind]}"
        intro = "이게 뭐 알림: 펩트론 087010 릴리 평가 종료 이후 공시 점검입니다. 새 공시나 분기 변화가 있어 보냅니다."
        shown = [f"- {e['text']}" for e in events[:5]]
        if len(events) > 5:
            shown.append(f"- 외 {len(events) - 5}건은 공시 목록에서 확인")
        status = ["지금 결과/상태:", *shown, f"- {px}", f"- {base}"]
        todo = f"지금 할 일: {TODO[kind]}"
    lines = [
        intro,
        *status,
        todo,
        "나중에: 3분기 보고서(11월 중순), PT403 임상 승인, 릴리 후속 협의 공시, 2027-08-05 교환사채 조기상환 청구 전 자금조달 방향을 확인하세요.",
        "익절/줄일 때: 릴리 협력 종료가 명시되거나 유상증자, 사채 발행이 나오면 비중을 다시 따지세요. 미리 정한 손실 한도를 지키세요.",
        "하지 말 것: 급락만 보고 추가 매수, 제목만 보고 계약 확정 판단, 한 번에 큰 금액 진입.",
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
    title = f"{cfg['title_prefix']} 087010 펩트론 {key} 조회 실패 | 판단 유보"
    lines = [
        "이게 뭐 알림: 펩트론 087010 공시 점검의 데이터 문제 안내.",
        f"지금 결과/상태: {detail}. {now:%Y-%m-%d %H:%M} KST. 이번 실행의 공시 확인은 건너뛰었습니다.",
        "지금 할 일: 없음. 다음 거래일에 자동 재시도합니다.",
        "나중에: 이틀 이상 계속되면 Naver 공시 API 변경 여부를 확인하세요.",
        "익절/줄일 때: 이 알림만으로 판단하지 마세요.",
        "하지 말 것: 조회가 안 된 것을 공시가 없다는 뜻으로 읽기.",
    ]
    if send(cfg, title, lines, dry, ops=True, priority=2) and not dry:
        ops_daily[key] = day


# ---------- run ----------

def run(mode: str, now: datetime | None = None) -> int:
    now = (now or datetime.now(KST)).astimezone(KST)
    cfg = read_json(CFG_PATH)
    state = read_json(STATE_PATH)
    dry = mode == "dry"
    install = mode == "install-test"
    until = date.fromisoformat(cfg.get("active_until", "2027-06-30"))
    if now.date() > until:
        if not state.get("expired_notice_sent") and not dry:
            problem(cfg, state, now, "점검기간", f"설정 점검기간({until})이 끝나 판단을 멈췄습니다. config 갱신 필요", dry)
            state["expired_notice_sent"] = True
            save_state(state, now)
        print("past active_until: silent")
        return 0
    if not install and not is_trading_day(now.date()):
        print("not a KRX trading day; silent")
        return 0
    try:
        rows = notices(cfg["ticker"], 100)
    except Exception as exc:
        problem(cfg, state, now, "공시목록", type(exc).__name__, dry)
        if not dry:
            save_state(state, now)
        return 0
    if not rows:
        problem(cfg, state, now, "공시목록", "공시 목록이 비어 있습니다", dry)
        if not dry:
            save_state(state, now)
        return 0
    try:
        actual = fetch_latest_actual(cfg["ticker"])
    except Exception as exc:
        print(f"quarter fetch failed: {type(exc).__name__}")
        actual = None
    try:
        close = fetch_close(cfg["ticker"])
    except Exception:
        close = None
    if install:
        title, lines = build_message(cfg, [], close, True)
        ok = send(cfg, title, lines, False)
        if ok:
            # install only seeds the notice list and the actual-quarter key
            state["install_test_sent"] = now.isoformat(timespec="seconds")
            state["seen_notice_ids"] = list(dict.fromkeys(r["id"] for r in rows))[:300]
            state["notices_seeded"] = True
            if actual:
                state["latest_actual_key"] = actual["key"]
            save_state(state, now)
        return 0 if ok else 1
    res = evaluate(cfg, state, rows, actual)
    if not state.get("notices_seeded"):
        print("first run: seeding notice ids, silent")
        if not dry:
            state["seen_notice_ids"] = list(dict.fromkeys(res["ids"]))[:300]
            state["notices_seeded"] = True
            if actual:
                state["latest_actual_key"] = actual["key"]
            save_state(state, now)
        return 0
    if not res["events"]:
        print("no change; silent")
        if not dry:
            state["seen_notice_ids"] = list(dict.fromkeys(res["ids"] + (state.get("seen_notice_ids") or [])))[:300]
            if actual and not state.get("latest_actual_key"):
                state["latest_actual_key"] = actual["key"]
            state["last_run"] = now.isoformat(timespec="seconds")
            save_state(state, now)
        return 0
    title, lines = build_message(cfg, res["events"], close, False)
    pri = 4 if top_kind(res["events"]) in ("lilly", "financing") else 3
    if send(cfg, title, lines, dry, priority=pri) and not dry:
        state["seen_notice_ids"] = list(dict.fromkeys(res["ids"] + (state.get("seen_notice_ids") or [])))[:300]
        if actual:
            state["latest_actual_key"] = max(state.get("latest_actual_key") or "", actual["key"])
        state["last_alert"] = {"at": now.isoformat(timespec="seconds"), "kinds": sorted({e["kind"] for e in res["events"]})}
        save_state(state, now)
    return 0


# ---------- self-test ----------

def self_test() -> int:
    cfg = read_json(CFG_PATH)
    rules = cfg["rules"]
    c = lambda t, x="": classify(t, x, rules)  # noqa: E731
    assert c("(주)펩트론 주식선물 2단계 가격제한폭 확대요건 도달(상승)") is None, "exchange noise"
    assert c("(주)펩트론 [투자주의]15일간 상승종목의 당일 소수계좌 매수관여 과다종목") is None
    assert c("(주)펩트론 투자판단 관련 주요경영사항(플랫폼 기술 평가 계약 체결)") == "lilly"
    assert c("(주)펩트론 투자판단 관련 주요경영사항(기술평가 계약 종료)") == "lilly"
    assert c("(주)펩트론 투자판단 관련 주요경영사항(기타)", "상대방 일라이 릴리") == "lilly"
    assert c("(주)펩트론 투자판단 관련 주요경영사항(임상시험 계획 승인신청)(PT403의 제1상 임상시험계획 승인 신청)") == "trial"
    assert c("(주)펩트론 투자판단 관련 주요경영사항(임상시험계획 승인)(PT403 제1상)") == "trial_approval"
    assert c("(주)펩트론 (정정)신규시설투자등") == "capex"
    assert c("(주)펩트론 주요사항보고서(유상증자결정)") == "financing"
    assert c("(주)펩트론 교환사채권 발행결정") == "financing"
    assert c("(주)펩트론 투자판단 관련 주요경영사항(기타)") == "major"
    assert c("(주)펩트론 주주총회소집공고") is None
    rows = [
        {"id": "1", "title": "(주)펩트론 (정정)신규시설투자등", "datetime": "2026-09-03T06:50:13", "text": ""},
        {"id": "2", "title": "(주)펩트론 주식선물 2단계 가격제한폭 확대요건 도달(상승)", "datetime": "2026-10-01T06:50:18", "text": ""},
    ]
    st = {"seen_notice_ids": ["1", "2"], "notices_seeded": True, "latest_actual_key": "202606"}
    assert not evaluate(cfg, st, rows, {"key": "202606", "op": -70})["events"], "unchanged is silent"
    rows2 = [{"id": "3", "title": "(주)펩트론 투자판단 관련 주요경영사항(플랫폼 기술 평가 계약 연장)", "datetime": "2026-10-20T16:00:00", "text": ""}, *rows]
    r = evaluate(cfg, st, rows2, {"key": "202606", "op": -70})
    assert [e["kind"] for e in r["events"]] == ["lilly"], r
    r = evaluate(cfg, st, rows, {"key": "202609", "op": -80})
    assert [e["kind"] for e in r["events"]] == ["quarter"] and "숫자" not in r["events"][0]["text"]
    r = evaluate(cfg, {"seen_notice_ids": [], "notices_seeded": False}, rows2, None)
    assert not r["events"], "unseeded state never alerts on the backlog"
    assert top_kind([{"kind": "capex"}, {"kind": "financing"}, {"kind": "lilly"}]) == "lilly"
    t, lines = build_message(cfg, [{"kind": "lilly", "text": "x (2026-10-20)"}], ("20261007", 96900.0), False)
    body = "\n".join([t, *lines])
    for head in ("이게 뭐 알림", "지금 결과/상태", "지금 할 일", "나중에", "익절/줄일 때", "하지 말 것"):
        assert head in body, head
    assert "\u00b7" not in body and "\u2014" not in body, "no middle dot or em dash"
    assert t.startswith("[PEPT]")
    ti, li = build_message(cfg, [], None, True)
    assert ti == "[PEPT] 087010 설치 확인 | 매매 신호 아님" and "매매 신호가 아닙니다" in "\n".join(li)
    assert not is_trading_day(date(2026, 10, 9))
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
