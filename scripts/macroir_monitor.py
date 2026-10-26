#!/usr/bin/env python3
"""[MACRO-IR] US-Iran war macro scenario lines (approved in chat 2026-10-26).

Thresholds come from a pasted external analysis dated 2026-10-26. They are not verified forecasts.

Price lines (slot `price`, existing k203780 19:00 KST slot, after the KRX close):
  combo130  Brent >= 130 AND USD/KRW >= 1,380  p5 warning  (scenario 3 confirmation)
  brent125  Brent >= 125                       p4 warning
  fx1360    USD/KRW >= 1,360                   p3 info     (standalone off: [USTSY] already pings 1,360)
  deal90    Brent <= 90                        p4 deal
  deal95    Brent <= 95                        p3 deal early notice
  kospi5300 KOSPI <= 5,300                     p3          (standalone off: [KOSDOWN] already pings 5,300)
  ktb104k   148070 <= 104,000 KRW              p4 warning  (long-rate spike)
Each line fires once when its value ENTERS the zone and re-arms quietly when the value leaves it (figpx/lgeswatch pattern).
Lines firing on the same run go into one message with the highest priority. If any warning is active
(Brent >= 125, combo, or 148070 <= 104,000) the whole message carries no buy wording.

Event checklists (slot `event`, existing morning 09:00 KST slot): 2026-11-03 pre-election, 2026-11-04 post-election.

Usage: macroir_monitor.py price|event [auto|dry] | confirm | selftest
  dry = print only: no ntfy, NEVER writes state (guarded twice: save_state refuses while DRY is set).
"""
from __future__ import annotations

import json
import sys
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
NY = ZoneInfo("America/New_York")
CFG = ROOT / "configs" / "macroir.json"
STATE = ROOT / "state" / "macroir.json"
UA = {"User-Agent": "Mozilla/5.0 cwally-alerts"}
NAME = "[MACRO-IR]"
SOURCE_NOTE = "기준값 출처: 붙여넣은 외부 분석(2026-10-26). 검증된 전망 아님."
WARNING_KEYS = ("combo130", "brent125", "ktb104k")
# buy-recommendation wording that must never appear in a warning message (selftest checks this)
BUY_WORDS = ("매수", "분할 진입", "비중 확대", "투입", "공격적")
ALLOWED_HOLD = ("추가매수 보류",)
BANNED_CHARS = ("\u00b7", "\u2014")  # middle dot, em-dash

DRY = False


def now_kst() -> datetime:
    return datetime.now(KST)


def load_cfg() -> dict:
    return json.loads(CFG.read_text(encoding="utf-8"))


def load_state() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {}


def save_state(st: dict, dry: bool) -> None:
    if dry or DRY:
        print("dry: state not saved")
        return
    st["updated_at"] = now_kst().isoformat(timespec="seconds")
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def send(topic_kind: str, title: str, body: str, prio: int, tags: list[str], dry: bool) -> None:
    if dry or DRY:
        print(f"DRY RUN would send ({topic_kind}, p{prio}): {title}\n{body}")
        return
    publish(resolve_topic(topic_kind), title, body, prio, tags=tags, config_name="macroir")


# ---------- fetch ----------

def get_json(url: str):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8", errors="ignore"))


def num(v) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(str(v).replace(",", "").replace("+", ""))
    except ValueError:
        return None


def yahoo_completed_close(url: str, now: datetime) -> tuple[float, str]:
    """Last completed US/Eastern daily close (live bar excluded)."""
    res = get_json(url)["chart"]["result"][0]
    out: dict[date, float] = {}
    for t, c in zip(res.get("timestamp") or [], res["indicators"]["quote"][0].get("close") or []):
        if c is not None:
            out[datetime.fromtimestamp(t, NY).date()] = float(c)
    now_ny = now.astimezone(NY)
    after = now_ny.hour * 60 + now_ny.minute >= 17 * 60 + 15
    done = sorted((d, c) for d, c in out.items() if d < now_ny.date() or (d == now_ny.date() and after))
    if not done:
        raise RuntimeError("no completed bar")
    d, c = done[-1]
    return c, str(d)


def yahoo_live(url: str) -> tuple[float, str]:
    meta = get_json(url)["chart"]["result"][0]["meta"]
    px, t = meta.get("regularMarketPrice"), meta.get("regularMarketTime")
    if not px:
        raise RuntimeError("no regularMarketPrice")
    return float(px), datetime.fromtimestamp(t, KST).strftime("%Y-%m-%d %H:%M") if t else "?"


def naver_close(url: str, now: datetime, close_hm: str = "15:40") -> tuple[float, str]:
    rows = get_json(url)
    if isinstance(rows, dict):
        rows = rows.get("result") or []
    today = now.strftime("%Y-%m-%d")
    hh, mm = (int(x) for x in close_hm.split(":"))
    closed = now.hour * 60 + now.minute >= hh * 60 + mm
    for r in rows:
        d = str(r.get("localTradedAt") or "")[:10]
        c = num(r.get("closePrice"))
        if c is None or not d:
            continue
        if d == today and not closed:
            continue
        return c, d
    raise RuntimeError("no closed row")


def fetch_all(cfg: dict, now: datetime) -> tuple[dict, dict, dict]:
    """values {series: float}, dates {series: str}, errors {series: str}"""
    S = cfg["sources"]
    close_hm = cfg.get("market_close_kst", "15:40")
    vals: dict = {}
    dates: dict = {}
    errs: dict = {}

    def take(key, fn):
        try:
            v, d = fn()
            vals[key], dates[key] = round(float(v), 4), d
        except Exception as exc:
            errs[key] = f"{type(exc).__name__}: {exc}"[:160]

    take("brent", lambda: yahoo_completed_close(S["brent"], now))
    take("wti", lambda: yahoo_completed_close(S["wti"], now))
    take("usdkrw", lambda: yahoo_live(S["usdkrw"]))

    def kospi():
        try:
            return naver_close(S["kospi"], now, close_hm)
        except Exception:
            return _ks11(S["kospi_fallback"], now, close_hm)

    take("kospi", kospi)
    take("ktb148070", lambda: naver_close(S["ktb148070"], now, close_hm))
    take("ktb365780", lambda: naver_close(S["ktb365780"], now, close_hm))
    return vals, dates, errs


def _ks11(url: str, now: datetime, close_hm: str) -> tuple[float, str]:
    res = get_json(url)["chart"]["result"][0]
    today = now.date()
    hh, mm = (int(x) for x in close_hm.split(":"))
    closed = now.hour * 60 + now.minute >= hh * 60 + mm
    rows = []
    for t, c in zip(res.get("timestamp") or [], res["indicators"]["quote"][0].get("close") or []):
        if c is not None:
            rows.append((datetime.fromtimestamp(t, KST).date(), float(c)))
    rows = [(d, c) for d, c in rows if d < today or (d == today and closed)]
    if not rows:
        raise RuntimeError("no ^KS11 bar")
    d, c = rows[-1]
    return c, str(d)


# ---------- core (pure) ----------

def in_zone(line: dict, vals: dict) -> bool | None:
    """True/False, or None when a needed value is missing (line is then not judged)."""
    op = line["op"]
    if op == "ge_and":
        xs = [vals.get(s) for s in line["series"]]
        if any(x is None for x in xs):
            return None
        return all(x >= lv for x, lv in zip(xs, line["level"]))
    x = vals.get(line["series"])
    if x is None:
        return None
    return x >= line["level"] if op == "ge" else x <= line["level"]


def step(lines: list[dict], armed: dict | None, vals: dict) -> tuple[dict, list[str]]:
    """armed None = first run: arm lines outside their zone, disarm lines already inside, fire nothing."""
    new = dict(armed or {})
    fired: list[str] = []
    for ln in lines:
        k = ln["key"]
        z = in_zone(ln, vals)
        if z is None:
            new.setdefault(k, True)
            continue
        if armed is None or k not in (armed or {}):
            new[k] = not z
            continue
        if z and armed.get(k, True):
            fired.append(k)
            new[k] = False
        elif not z:
            new[k] = True
    return new, fired


def warning_active(vals: dict, fired: list[str]) -> list[str]:
    """Which warning conditions are on right now (fired today or still in zone)."""
    on = [k for k in WARNING_KEYS if k in fired]
    b, fx, kt = vals.get("brent"), vals.get("usdkrw"), vals.get("ktb148070")
    if b is not None and b >= 125 and "brent125" not in on:
        on.append("brent125")
    if b is not None and fx is not None and b >= 130 and fx >= 1380 and "combo130" not in on:
        on.append("combo130")
    if kt is not None and kt <= 104000 and "ktb104k" not in on:
        on.append("ktb104k")
    return on


def fmt(v, kind: str) -> str:
    if v is None:
        return "확인 불가"
    if kind == "usd":
        return f"{v:,.2f}달러"
    if kind == "krw":
        return f"{v:,.0f}원"
    if kind == "fx":
        return f"{v:,.2f}원"
    return f"{v:,.2f}"


def values_text(vals: dict, dates: dict) -> str:
    def d(k, what):
        return f"({what} {dates[k]})" if k in dates else ""
    return (f"브렌트 {fmt(vals.get('brent'), 'usd')}{d('brent', '미국 종가')}, WTI {fmt(vals.get('wti'), 'usd')}{d('wti', '미국 종가')}, "
            f"원/달러 {fmt(vals.get('usdkrw'), 'fx')}{d('usdkrw', 'KST')}, 코스피 {fmt(vals.get('kospi'), 'idx')}{d('kospi', '종가')}, "
            f"148070 {fmt(vals.get('ktb148070'), 'krw')}{d('ktb148070', '종가')}, 365780 {fmt(vals.get('ktb365780'), 'krw')}{d('ktb365780', '종가')}")


def build_message(cfg: dict, fired: list[str], vals: dict, dates: dict, armed: dict) -> tuple[str, str, int]:
    L = {ln["key"]: ln for ln in cfg["lines"]}
    fired = [k for k in [ln["key"] for ln in cfg["lines"]] if k in fired]  # config order
    prio = max(int(L[k]["priority"]) for k in fired)
    warn = warning_active(vals, fired)
    hold_reason = ""
    if "combo130" in warn or "brent125" in warn:
        hold_reason = "격화 경고(브렌트 125달러 이상)가 켜져 있음"
    elif "ktb104k" in warn:
        hold_reason = "장기금리 급등 경고(148070 104,000원 이하)가 켜져 있음"

    todos: list[str] = []
    if "combo130" in fired:
        todos.append("판단 변경 조건 도달. 주식 비중 축소 점검, 단기채와 에너지 헤지 전환 검토, 국고채 10년물 추가매수 보류(금리 피크아웃 확인 전).")
    if "brent125" in fired:
        if "combo130" in fired:
            todos.append("현금 버퍼(파킹형 ETF) 20% 점검.")
        else:
            todos.append("격화 초기 경고. 추가매수 보류, 현금 버퍼(파킹형 ETF) 20% 점검.")
    if "ktb104k" in fired:
        todos.append("장기금리 급등 구간. 148070 추가매수 보류, 금리 피크아웃 확인 후 재판단. 국고10년 금리 수준 판단은 [KR-RATE]가 따로 봅니다(4.50% 이상이면 보류 문구만).")
    if "deal90" in fired:
        if warn:
            todos.append(f"판단 변경 조건 도달(타결 시나리오). 국고채 10년물 일부 익절 검토. {hold_reason}어서 추가매수 보류, 경고 해제 확인 후 재판단.")
        else:
            todos.append("판단 변경 조건 도달(타결 시나리오). 국고채 10년물 일부 익절 검토, 반도체 및 지수 ETF 비중 확대 검토.")
    if "deal95" in fired:
        todos.append("타결 신호 확인: 호르무즈 통항 보장, 해상봉쇄 해제 서명 여부. 아직 판단 변경은 아님(기준은 90달러 이하).")
    if "fx1360" in fired and "combo130" not in fired:
        todos.append("환노출 ETF 쿠션 작동 확인, 1,380원 돌파 여부 주시.")
    if "kospi5300" in fired:
        if warn:
            if "combo130" in warn or "brent125" in warn:
                todos.append("코스피 5,300 이하지만 격화 경고(브렌트 125달러 이상)가 켜져 있음. 추가매수 보류, 공습 전개 확인 후 재판단.")
            else:
                todos.append(f"코스피 5,300 이하지만 {hold_reason}. 추가매수 보류, 금리 피크아웃 확인 후 재판단.")
        else:
            todos.append("코스피 5,300 이하. 분석은 5,200~5,300을 역발상 구간으로 봄. ISA 현금 비중 분할 투입 검토(한 번에 다 넣지 않기).")

    labels = ", ".join(L[k]["label"] for k in fired)
    inside = [L[k]["label"] for k in L if k not in fired and armed.get(k) is False]
    if "combo130" in fired:
        act = "판단 변경(격화), 추가매수 보류"
        head = f"브렌트 {fmt(vals.get('brent'), 'usd')} + 원/달러 {fmt(vals.get('usdkrw'), 'fx')}"
    elif warn:
        act = "추가매수 보류"
        head = L[fired[0]]["label"].split("(")[0]
        if any(k in fired for k in WARNING_KEYS):
            head = next(L[k]["label"].split("(")[0] for k in fired if k in WARNING_KEYS)
    elif "deal90" in fired:
        act = "판단 변경(타결), 익절과 비중 점검"
        head = f"브렌트 {fmt(vals.get('brent'), 'usd')}"
    elif "deal95" in fired:
        act = "타결 신호 확인"
        head = f"브렌트 {fmt(vals.get('brent'), 'usd')}"
    else:
        act = "점검"
        head = L[fired[0]]["label"]
    title = f"{NAME} {head} | {act}"

    if warn:
        trim = "주식 비중 축소는 계획한 비율 안에서만, 한 번에 다 정리하지 않기." if ("combo130" in warn or "brent125" in warn) else "국고채 10년물은 보유분 유지, 추가 편입 없이 금리 흐름 확인."
        dont = "물타기, 공습 뉴스 직후 장중 추격, 신용/미수, 이 알림만 보고 주문. 투자 권유 아님."
    elif "deal90" in fired:
        trim = "국고채 10년물(148070, 365780)은 일부만 익절 검토, 한 번에 전량 정리하지 않기."
        dont = "타결 뉴스 직후 장중 추격, 한 번에 전액 투입, 신용/미수, 이 알림만 보고 주문. 투자 권유 아님."
    else:
        trim = "해당 없음. 비중 계획은 그대로."
        dont = "한 번에 전액 투입, 신용/미수, 이 알림만 보고 주문. 투자 권유 아님."
    folded = [L[k] for k in L if not L[k].get("standalone", True)]
    later = "같은 선은 값이 구간을 벗어난 뒤 다시 들어올 때만 또 울립니다(평일 19:00 점검)."
    if folded:
        later += " 코스피 5,300 이탈은 [KOSDOWN], 원/달러 1,360원은 [USTSY]가 따로 알리고, 여기서는 다른 선과 같이 울릴 때만 함께 적습니다."
    lines = [
        f"{now_kst():%Y-%m-%d %H:%M} KST | MACRO-IR | 미-이란 거시 시나리오 감시선",
        "이게 뭐 알림: " + cfg["explain"] + " " + SOURCE_NOTE,
        f"지금 결과/상태: 새로 도달: {labels}." + (f" 이미 구간 안: {', '.join(inside)}." if inside else "") + f" 현재 값: {values_text(vals, dates)}.",
        "지금 할 일: " + " ".join(todos),
        "나중에: " + later,
        "익절/줄일 때: " + trim,
        "하지 말 것: " + dont,
    ]
    return title, "\n".join(lines), prio


# ---------- events ----------

def scenario_match(cfg: dict, vals: dict) -> str:
    out = []
    for sc in cfg["events"]["scenarios"]:
        hits, n = 0, 0
        for key, series in (("brent", "brent"), ("usdkrw", "usdkrw"), ("kospi", "kospi")):
            lo, hi = sc[key]
            v = vals.get(series)
            if v is None:
                continue
            n += 1
            hits += (lo is None or v >= lo) and (hi is None or v <= hi)
        out.append(f"{sc['name']} {hits}/{n}")
    return ", ".join(out)


def scenario_text(cfg: dict) -> str:
    parts = []
    for i, sc in enumerate(cfg["events"]["scenarios"], 1):
        parts.append(f"{i}) {sc['name']}(분석 확률 {sc['prob']}): 브렌트 {sc['brent'][0]}~{sc['brent'][1]}달러, "
                     f"원/달러 {sc['usdkrw'][0]:,}~{sc['usdkrw'][1]:,}원, 코스피 {sc['kospi_text']}")
    return "; ".join(parts)


def event_message(cfg: dict, kind: str, vals: dict, dates: dict) -> tuple[str, str, int]:
    warn = warning_active(vals, [])
    if kind == "pre":
        title = f"{NAME} 미 중간선거 당일 점검 | 국고채 추격 매수 중단"
        todo = ("체크리스트 3개. 1) 국고채 10년물(148070, 365780) 추격 매수 중단, 보유분만 유지. "
                "2) ISA 안 해외 ETF 환노출(언헤지) 비중 확인. 3) 현금 버퍼(파킹형 ETF) 비율 확인.")
        later = "내일(11/4) 09:00 에 선거 뒤 3개 시나리오 대조표가 옵니다. 가격선은 평일 19:00 에 계속 봅니다."
    else:
        title = f"{NAME} 선거 다음 날 시나리오 대조 | 기준선 확인"
        todo = (f"아래 3개 시나리오 기준과 현재 값을 비교하세요. 현재 값 일치 수: {scenario_match(cfg, vals)}. "
                f"{scenario_text(cfg)}. 미국 동부 기준 11/3 저녁이라 개표 결과가 확정 전일 수 있음. 결과 확정 뉴스 확인 뒤 판단.")
        later = "판단 변경 조건(브렌트 130달러 + 원/달러 1,380원, 브렌트 90달러 이하)은 평일 19:00 가격선이 따로 알립니다."
    if warn:
        todo += " 경고선이 켜져 있어 추가매수 보류."
    lines = [
        f"{now_kst():%Y-%m-%d %H:%M} KST | MACRO-IR | 미-이란 거시 시나리오 이벤트",
        "이게 뭐 알림: 11/3 미국 중간선거 전후 점검 알림(1회). " + SOURCE_NOTE,
        f"지금 결과/상태: 현재 값: {values_text(vals, dates)}.",
        "지금 할 일: " + todo,
        "나중에: " + later,
        "익절/줄일 때: 이 알림은 점검용이며 줄이라는 주문이 아닙니다.",
        "하지 말 것: 선거 뉴스 직후 장중 추격, 신용/미수, 이 알림만 보고 주문. 투자 권유 아님.",
    ]
    return title, "\n".join(lines), 3


def confirm_message(cfg: dict, vals: dict, dates: dict, armed: dict) -> tuple[str, str]:
    title = f"{NAME} 설치 확인 | 매매 신호 아님"
    inside = [ln["label"] for ln in cfg["lines"] if armed.get(ln["key"]) is False]
    lines = [
        "[테스트] MACRO-IR 알림 설정 완료",
        "이게 뭐 알림: " + cfg["explain"] + " " + SOURCE_NOTE,
        f"지금 결과/상태: 현재 값: {values_text(vals, dates)}. 이미 구간 안인 선: {', '.join(inside) if inside else '없음(모두 대기)'}.",
        "지금 할 일: 없음. 평일 19:00 KST 에 브렌트 130+원/달러 1,380(p5), 브렌트 125(p4), 브렌트 90(p4)과 95(p3), 148070 104,000원(p4)을 보고 구간에 새로 들어갈 때만 보냅니다. 11/3, 11/4 09:00 에 선거 체크리스트 1회씩.",
        "나중에: 코스피 5,300 은 [KOSDOWN], 원/달러 1,360 은 [USTSY]가 이미 알려서 여기서는 다른 선과 같이 울릴 때만 함께 적습니다.",
        "익절/줄일 때: 해당 없음.",
        "하지 말 것: 이 설치 확인을 매수/매도 신호로 읽기.",
    ]
    return title, "\n".join(lines)


# ---------- run ----------

def fail_notice(st: dict, errs: dict, today: str, dry: bool) -> None:
    if not errs or st.get("fail_day") == today:
        return
    title = f"{NAME} 시세 조회 실패 | 매매 신호 아님"
    body = "조회 실패: " + "; ".join(f"{k} {v}" for k, v in errs.items()) + ". 이 값이 필요한 선은 이번에 판정하지 않았습니다(실패를 미충족으로 보지 않음). 다음 실행에서 다시 봅니다."
    send("ops", title, body, 3, ["warning"], dry)
    st["fail_day"] = today


def run_price(cfg: dict, dry: bool, now: datetime, fetch=fetch_all) -> int:
    st = load_state()
    vals, dates, errs = fetch(cfg, now)
    print("values", vals, dates, "errors", errs)
    today = now.strftime("%Y-%m-%d")
    fail_notice(st, errs, today, dry)
    armed_prev = st.get("armed") if st.get("initialized") else None
    armed, fired = step(cfg["lines"], armed_prev, vals)
    if armed_prev is None:
        st.update({"initialized": True, "init_note": f"initialized {today} from current values, no send"})
        print("first run: initialized, no send", armed)
    standalone = [k for k in fired if next(ln for ln in cfg["lines"] if ln["key"] == k).get("standalone", True)]
    if standalone:
        title, body, prio = build_message(cfg, fired, vals, dates, armed)
        tag = "warning" if warning_active(vals, fired) else "chart_with_upwards_trend"
        send("port", title, body, prio, [tag], dry)
        st["last_sent"] = {"day": today, "fired": fired, "title": title}
    elif fired:
        print(f"only folded lines fired {fired}; silent (covered by [KOSDOWN]/[USTSY])")
    else:
        print("no zone entry; silent")
    st.update({"armed": armed, "last_values": vals, "last_dates": dates, "last_run": today})
    save_state(st, dry)
    return 0


def run_event(cfg: dict, dry: bool, now: datetime, fetch=fetch_all) -> int:
    st = load_state()
    today = now.date()
    ev = cfg["events"]
    due = None
    for kind in ("pre", "post"):
        e = ev[kind]
        if date.fromisoformat(e["date"]) <= today <= date.fromisoformat(e["catchup_until"]) and not st.get(f"{kind}_sent"):
            due = kind
    if not due:
        print("no event due today; silent")
        return 0
    vals, dates, errs = fetch(cfg, now)
    title, body, prio = event_message(cfg, due, vals, dates)
    send("port", title, body, prio, ["calendar"], dry)
    st[f"{due}_sent"] = now.strftime("%Y-%m-%d %H:%M KST")
    save_state(st, dry)
    return 0


def main(argv: list[str]) -> int:
    global DRY
    slot = (argv[1] if len(argv) > 1 else "price").lower()
    if slot == "selftest":
        return selftest()
    mode = (argv[2] if len(argv) > 2 else "auto").lower()
    dry = mode != "auto"
    DRY = dry
    cfg = load_cfg()
    now = now_kst()
    print(f"{now.isoformat(timespec='seconds')} macroir slot={slot} mode={mode}")
    if dry:
        print("DRY RUN (no ntfy, no state write)")
    if slot == "confirm":  # install-confirmation send (AGENTS.md test title), never touches state
        vals, dates, errs = fetch_all(cfg, now)
        title, body = confirm_message(cfg, vals, dates, load_state().get("armed") or {})
        print(title)
        print(body)
        DRY = False
        if mode == "dry":
            print("dry confirm: not sent")
            return 0
        publish(resolve_topic("port"), title, body, 3, tags=["white_check_mark"], config_name="macroir")
        return 0
    if now.date() > date.fromisoformat(cfg["valid_until"]):
        print(f"expired (valid_until {cfg['valid_until']}); silent")
        return 0
    if now.year not in cfg.get("year_guard", [now.year]):
        print("year guard; silent")
        return 0
    if slot == "price":
        return run_price(cfg, dry, now)
    if slot == "event":
        return run_event(cfg, dry, now)
    print(f"unknown slot {slot}")
    return 2


# ---------- selftest ----------

def selftest() -> int:
    import io
    import tempfile
    from contextlib import redirect_stdout
    from unittest import mock

    global STATE, DRY
    cfg = load_cfg()
    L = cfg["lines"]
    fails = 0

    def check(label, got, want):
        nonlocal fails
        ok = got == want
        fails += not ok
        print(("ok  " if ok else "FAIL"), label, "" if ok else f"got {got!r} want {want!r}")

    base = {"brent": 109.63, "wti": 100.81, "usdkrw": 1323.9, "kospi": 5469.47, "ktb148070": 107420.0, "ktb365780": 84740.0}
    dates = {k: "2026-10-26" for k in base}
    A, _ = step(L, None, base)
    check("init from install values: all armed", all(A.values()), True)
    check("init never fires", step(L, None, {**base, "brent": 131, "usdkrw": 1390})[1], [])
    init_in = step(L, None, {**base, "brent": 126})[0]
    check("init already in zone -> disarmed (no immediate fire)", (init_in["brent125"], step(L, init_in, {**base, "brent": 127})[1]), (False, []))

    def fire(v):
        return step(L, A, {**base, **v})[1]

    check("1 combo 130/1380", fire({"brent": 130, "usdkrw": 1380}), ["combo130", "brent125", "fx1360"])
    check("1 combo needs both (fx 1379)", "combo130" in fire({"brent": 131, "usdkrw": 1379}), False)
    check("2 brent 125", fire({"brent": 125}), ["brent125"])
    check("2 brent 124.99 nothing", fire({"brent": 124.99}), [])
    check("3 fx 1360", fire({"usdkrw": 1360}), ["fx1360"])
    check("4 deal 90 (with 95)", fire({"brent": 90}), ["deal90", "deal95"])
    check("4 deal 95 only", fire({"brent": 94}), ["deal95"])
    check("5 kospi 5300", fire({"kospi": 5300}), ["kospi5300"])
    check("6 148070 104000", fire({"ktb148070": 104000}), ["ktb104k"])
    check("missing value not judged", step(L, A, {**base, "brent": None})[1], [])
    # once on entry, re-arm on exit
    st = dict(A)
    walk = [({"brent": 126}, ["brent125"]), ({"brent": 128}, []), ({"brent": 124}, []), ({"brent": 125.5}, ["brent125"]),
            ({"brent": 131, "usdkrw": 1381}, ["combo130", "fx1360"]), ({"brent": 132, "usdkrw": 1390}, []),
            ({"brent": 132, "usdkrw": 1370}, []), ({"brent": 133, "usdkrw": 1385}, ["combo130"])]
    for i, (v, want) in enumerate(walk):
        st, f = step(L, st, {**base, **v})
        check(f"walk{i} {v}", f, want)

    def todo_of(body):
        return next(x for x in body.split("\n") if x.startswith("지금 할 일:"))

    def no_buy(text):
        t = text
        for ok in ALLOWED_HOLD:
            t = t.replace(ok, "")
        return [w for w in BUY_WORDS if w in t]

    msgs = {}
    cases = {
        "combo": ({"brent": 131.2, "usdkrw": 1385.5}, ["combo130", "brent125", "fx1360"]),
        "brent125": ({"brent": 126.0}, ["brent125"]),
        "ktb": ({"ktb148070": 103800}, ["ktb104k"]),
        "deal90": ({"brent": 89.5}, ["deal90", "deal95"]),
        "deal95": ({"brent": 94.0}, ["deal95"]),
        "kospi_normal": ({"brent": 94.0, "kospi": 5280}, ["deal95", "kospi5300"]),
        "kospi_override": ({"brent": 126.0, "kospi": 5280}, ["brent125", "kospi5300"]),
        "kospi_override_ktb": ({"ktb148070": 103500, "kospi": 5250}, ["kospi5300", "ktb104k"]),
        "deal90_with_ktb": ({"brent": 89.0, "ktb148070": 103000}, ["deal90", "deal95", "ktb104k"]),
        "kospi_while_brent_already_125": ({"brent": 127.0, "kospi": 5290, "ktb148070": 103900}, None),
    }
    for name, (v, want) in cases.items():
        vals = {**base, **v}
        start = A if name != "kospi_while_brent_already_125" else step(L, A, {**base, "brent": 127.0})[0]
        armed, f = step(L, start, vals)
        if want is not None:
            check(f"{name} fired", f, want)
        t, b, p = build_message(cfg, f, vals, dates, armed)
        msgs[name] = (t, b, p)
        check(f"{name} no middle dot / em-dash", any(c in t + b for c in BANNED_CHARS), False)
        check(f"{name} six-part order", [ln.split(":")[0] for ln in b.split("\n")[1:7]], ["이게 뭐 알림", "지금 결과/상태", "지금 할 일", "나중에", "익절/줄일 때", "하지 말 것"])
        check(f"{name} source note", SOURCE_NOTE in b, True)
        if warning_active(vals, f):
            check(f"{name} warning: no buy wording anywhere", no_buy(t + "\n" + b), [])
            check(f"{name} warning: 추가매수 보류 present", "추가매수 보류" in t + b, True)
    check("combo priority 5", msgs["combo"][2], 5)
    check("brent125 priority 4", msgs["brent125"][2], 4)
    check("ktb priority 4", msgs["ktb"][2], 4)
    check("deal90 priority 4", msgs["deal90"][2], 4)
    check("deal95 priority 3", msgs["deal95"][2], 3)
    check("kospi override priority 4 (highest)", msgs["kospi_override"][2], 4)
    check("kospi normal has ISA cash wording", "ISA 현금 비중 분할 투입 검토" in todo_of(msgs["kospi_normal"][1]), True)
    check("kospi override replaces wording", ("ISA 현금" in msgs["kospi_override"][1], "추가매수 보류, 공습 전개 확인 후 재판단" in msgs["kospi_override"][1]), (False, True))
    check("kospi override when brent125 already active (not fired today)", "추가매수 보류, 공습 전개 확인 후 재판단" in msgs["kospi_while_brent_already_125"][1], True)
    check("deal90 normal has 비중 확대", "반도체 및 지수 ETF 비중 확대 검토" in msgs["deal90"][1], True)
    check("deal90 + ktb warning strips 비중 확대", "비중 확대" in msgs["deal90_with_ktb"][1], False)
    check("combo body wording", "판단 변경 조건 도달. 주식 비중 축소 점검, 단기채와 에너지 헤지 전환 검토, 국고채 10년물 추가매수 보류(금리 피크아웃 확인 전)." in msgs["combo"][1], True)
    # folded-only lines stay silent; events
    for kind in ("pre", "post"):
        t, b, p = event_message(cfg, kind, base, dates)
        msgs[f"event_{kind}"] = (t, b, p)
        check(f"event {kind} no middle dot / em-dash", any(c in t + b for c in BANNED_CHARS), False)
    check("post has 3 scenarios", all(s in msgs["event_post"][1] for s in ("타결(", "교착(", "격화(")), True)
    t, b = confirm_message(cfg, base, dates, A)
    check("confirm title", t, "[MACRO-IR] 설치 확인 | 매매 신호 아님")
    check("confirm no middle dot / em-dash", any(c in t + b for c in BANNED_CHARS), False)

    # dry run must never write state and never publish (past bug)
    old_state = STATE
    sent = []
    try:
        with tempfile.TemporaryDirectory() as td:
            STATE = Path(td) / "macroir.json"
            seed = {"initialized": True, "armed": A}
            STATE.write_text(json.dumps(seed), encoding="utf-8")
            before = STATE.read_bytes()
            fake = lambda c, n: ({**base, "brent": 131, "usdkrw": 1390, "kospi": 5200}, dates, {"wti": "x"})  # noqa: E731
            now = datetime(2026, 11, 4, 19, 0, tzinfo=KST)
            with mock.patch(f"{__name__}.publish", side_effect=lambda *a, **k: sent.append(a)), redirect_stdout(io.StringIO()):
                DRY = True
                run_price(cfg, True, now, fetch=fake)
                run_event(cfg, True, datetime(2026, 11, 3, 9, 0, tzinfo=KST), fetch=fake)
                run_event(cfg, True, datetime(2026, 11, 4, 9, 0, tzinfo=KST), fetch=fake)
                DRY = False
            check("dry: state file unchanged", STATE.read_bytes() == before, True)
            check("dry: nothing published", sent, [])
            # auto path writes state and dedupes (mocked publish)
            with mock.patch(f"{__name__}.publish", side_effect=lambda *a, **k: sent.append(a[1])), redirect_stdout(io.StringIO()):
                run_price(cfg, False, now, fetch=fake)
                run_price(cfg, False, now, fetch=fake)
                run_event(cfg, False, datetime(2026, 11, 3, 9, 0, tzinfo=KST), fetch=fake)
                run_event(cfg, False, datetime(2026, 11, 3, 9, 5, tzinfo=KST), fetch=fake)
                run_event(cfg, False, datetime(2026, 11, 4, 9, 0, tzinfo=KST), fetch=fake)
                run_event(cfg, False, datetime(2026, 11, 5, 9, 0, tzinfo=KST), fetch=fake)
                run_event(cfg, False, datetime(2026, 11, 2, 9, 0, tzinfo=KST), fetch=fake)
            port = [s for s in sent if "조회 실패" not in s]
            check("auto: price once + pre once + post once", len(port), 3)
            check("auto: ops fail notice once per day", sum("조회 실패" in s for s in sent), 1)
            check("auto: state written", json.loads(STATE.read_text())["armed"]["combo130"], False)
    finally:
        STATE, DRY = old_state, False
    print("\n=== sample p5 combo ===")
    print(msgs["combo"][2], msgs["combo"][0])
    print(msgs["combo"][1])
    print("\n=== sample KOSPI override ===")
    print(msgs["kospi_override"][2], msgs["kospi_override"][0])
    print(msgs["kospi_override"][1])
    print("\n=== sample KOSPI normal ===")
    print(msgs["kospi_normal"][2], msgs["kospi_normal"][0])
    print(msgs["kospi_normal"][1])
    print("\n=== event post ===")
    print(msgs["event_post"][0])
    print(msgs["event_post"][1])
    print("selftest", "PASS" if not fails else f"FAIL {fails}")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
