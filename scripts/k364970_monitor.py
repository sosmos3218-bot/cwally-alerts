#!/usr/bin/env python3
"""[364970] TIGER 바이오TOP10 소액 구간, 6,075원 이탈, 상위 3종목 공시.

평일 15:40 KST (0090b0 슬롯, 새 cron 없음). 정규장 종가는 scripts/krx_close.py.
첫 auto 실행은 종가와 이미 나온 공시를 시드만 하고 보내지 않는다.
modes: auto | dry(발송 없음, state 안 씀) | selftest(네트워크 없음)
수동 job=k364970 은 oneoff_mode=auto 가 아니면 selftest + dry.
"""
from __future__ import annotations

import copy
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import earnings_parse  # noqa: E402
from kr_calendar import KST, is_trading_day, session_day  # noqa: E402
from ntfy_util import publish, resolve_server, resolve_topic, route_topic  # noqa: E402

TAG = "[364970]"
NAME = "k364970"
CFG_PATH = ROOT / "configs" / f"{NAME}.json"
STATE_PATH = ROOT / "state" / f"{NAME}.json"
CLOSE_HM = (15, 40)
UA = {"User-Agent": "Mozilla/5.0 (cwally-alerts; +https://github.com/sosmos3218-bot/cwally-alerts)"}
DART_SEARCH = "https://dart.fss.or.kr/dsab007/detailSearch.ax"
DART_VIEW = "https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcp}"
NAVER_NOTICE = "https://stock.naver.com/api/domestic/detail/notice?itemCode={code}&page=1&pageSize=80"
FORBIDDEN = re.compile(r"[\u00b7\u318d\u2014\u2013\u2015]")
PILLAR_KEYS = (
    "celltrion_op_ok",
    "celltrion_ni_recover",
    "samsung_deal_closed",
    "samsung_major_order",
    "samsung_flow_stable",
    "alteogen_cash_in",
    "alteogen_keytruda_growth",
)


def clean(text: str) -> str:
    out = text or ""
    for ch in ("\u00b7", "\u318d", "\u2014", "\u2013", "\u2015", "\u30fb", "ㆍ"):
        out = out.replace(ch, " ")
    return re.sub(r"\s+", " ", out).strip()


def norm_title(text: str) -> str:
    s = clean(text)
    s = re.sub(r"\(주\)|주식회사", "", s)
    return re.sub(r"[^0-9A-Za-z가-힣]", "", s).lower()


def won(v: float) -> str:
    return f"{v:,.0f}원"


def num(v) -> float | None:
    if v is None or v is False:
        return None
    try:
        return float(str(v).replace(",", "").replace("%", ""))
    except (TypeError, ValueError):
        return None


def http(url: str, data: bytes | None = None, headers: dict | None = None, timeout: int = 25) -> str:
    h = dict(UA)
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    for enc in ("utf-8", "euc-kr", "cp949"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def check_text(title: str, message: str) -> None:
    if FORBIDDEN.search(title + message):
        raise AssertionError(f"forbidden char in {title}")
    if not title.startswith(TAG):
        raise AssertionError(f"title missing ticker: {title}")


def load_state() -> dict:
    try:
        raw = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def save_state(st: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def pillars_of(st: dict) -> dict:
    p = st.setdefault("pillars", {})
    for key in PILLAR_KEYS:
        p.setdefault(key, False)
    return p


def pillar_score(p: dict) -> tuple[int, list[bool]]:
    bits = [
        bool(p.get("celltrion_op_ok") and p.get("celltrion_ni_recover")),
        bool(p.get("samsung_flow_stable") and (p.get("samsung_deal_closed") or p.get("samsung_major_order"))),
        bool(p.get("alteogen_cash_in") and p.get("alteogen_keytruda_growth")),
    ]
    return sum(bits), bits


def yn(ok: bool) -> str:
    return "확인" if ok else "미확인"


def pillar_line(p: dict) -> str:
    n, _bits = pillar_score(p)
    cell = f"셀트리온 영업이익 {yn(p.get('celltrion_op_ok'))}, 순이익 회복 {yn(p.get('celltrion_ni_recover'))}"
    sam = (
        f"삼성바이오로직스 인수 종결 {yn(p.get('samsung_deal_closed'))}, "
        f"주요 수주 {yn(p.get('samsung_major_order'))}, 수급 안정 {yn(p.get('samsung_flow_stable'))}"
    )
    alt = f"알테오젠 입금 {yn(p.get('alteogen_cash_in'))}, 키트루다 SC 판매 성장 {yn(p.get('alteogen_keytruda_growth'))}"
    return f"투자매력 상향은 3개 중 2개. 지금 {n}/3. {cell}. {sam}. {alt}."


def six(stamp: str, what: str, status: str, action: str, later: str, trim: str, avoid: str) -> str:
    return "\n".join([
        f"{stamp} | 364970 | TIGER 바이오TOP10",
        f"이게 뭐 알림: {what}",
        f"지금 결과/상태: {status}",
        f"지금 할 일: {action}",
        f"나중에: {later}",
        f"익절/줄일 때: {trim}",
        f"하지 말 것: {avoid}",
    ])


def in_band(cfg: dict, close: float) -> bool:
    z = cfg["zone"]
    return z["low"] <= close <= z["high"]


def add_trading_days(day: str, n: int) -> str:
    d = date.fromisoformat(day)
    left = int(n)
    while left > 0:
        d += timedelta(days=1)
        if is_trading_day(d):
            left -= 1
    return d.isoformat()


def avg_n(rows: list[dict], day: str, n: int = 20) -> float | None:
    xs = [float(r["close"]) for r in rows if r["day"] <= day]
    if len(xs) < n:
        return None
    return sum(xs[-n:]) / n


def seed_price(cfg: dict, day: str, close: float) -> dict:
    """이미 구간 안이거나 이미 6,075원 아래면 그 사실을 울리지 않고 기록만 한다."""
    level = float(cfg["break"]["level"])
    return {
        "last_date": day,
        "last_close": float(close),
        "zone_armed": not in_band(cfg, close),
        "break_armed": not (close < level),
        "break_day": None,
        "follow_pending": False,
        "follow_due": None,
    }


def evaluate_prices(cfg: dict, ps: dict, rows: list[dict]) -> list[dict]:
    z = cfg["zone"]
    br = cfg["break"]
    level = float(br["level"])
    out = []
    for r in rows:
        day, close = r["day"], float(r["close"])
        if ps.get("last_date") and day <= ps["last_date"]:
            continue
        prev = ps.get("last_close")
        if ps.get("zone_armed", True) and in_band(cfg, close):
            ps["zone_armed"] = False
            out.append({"type": "zone", "day": day, "close": close, "prev": prev})
        elif not ps.get("zone_armed", True) and (close > z["rearm_above"] or close < z["rearm_below"]):
            ps["zone_armed"] = True
        if ps.get("break_armed", True) and close < level:
            ps["break_armed"] = False
            ps["break_day"] = day
            ps["follow_pending"] = True
            ps["follow_due"] = add_trading_days(day, int(br["wait_trading_days"]))
            out.append({"type": "break", "day": day, "close": close, "prev": prev, "due": ps["follow_due"]})
        elif ps.get("follow_pending") and ps.get("break_day") and day > ps["break_day"]:
            if close >= level:
                ps["follow_pending"] = False
            elif day >= ps.get("follow_due") and close < level:
                ps["follow_pending"] = False
                out.append({"type": "follow", "day": day, "close": close, "prev": prev, "break_day": ps["break_day"]})
        if not ps.get("break_armed", True) and close >= float(br["rearm"]):
            ps["break_armed"] = True
        ps["last_date"] = day
        ps["last_close"] = close
    return out


def nav_which(nav: float, dev: float, sign: str, close: float, prev_close: float | None) -> str:
    """k203780_monitor.naver_nav_dated 와 같은 날짜 판정. today | prev | unknown."""
    signed = -abs(dev) if str(sign or "").strip() == "-" else abs(dev)
    implied = nav * (1 + signed / 100)
    tol = nav * 0.00005 + 0.6
    hit_today = abs(implied - close) <= tol
    hit_prev = prev_close is not None and abs(implied - prev_close) <= tol
    if hit_today and hit_prev:
        return "unknown"
    if hit_prev:
        return "prev"
    if hit_today:
        return "today"
    return "unknown"


def describe_nav(pack: dict | None, close: float, prev_close: float | None, premium_max: float) -> tuple[str, bool]:
    if not pack or pack.get("nav") is None or pack.get("dev") is None:
        return "NAV 확인 불가. 괴리 판단은 보류.", False
    nav = float(pack["nav"])
    which = nav_which(nav, float(pack["dev"]), str(pack.get("sign") or ""), close, prev_close)
    if which == "today":
        prem = close / nav - 1
        flag = prem > premium_max
        extra = " 프리미엄 과다, 주문 보류." if flag else ""
        return f"NAV {nav:,.2f}원(당일 확인), 괴리 {prem * 100:+.2f}%.{extra}", flag
    if which == "prev":
        sign = "-" if str(pack.get("sign") or "").strip() == "-" else "+"
        return (
            f"NAV {nav:,.2f}원은 전 거래일 값(그날 괴리 {sign}{abs(float(pack['dev'])):.2f}%). 오늘 괴리 판단은 보류.",
            False,
        )
    return f"NAV {nav:,.2f}원(날짜 확인 불가). 괴리 판단은 보류.", False


def price_messages(cfg: dict, events: list[dict], stamp: str, rows: list[dict], pack: dict | None, p: dict) -> list[tuple]:
    up = pillar_line(p)
    out = []
    z = cfg["zone"]
    for ev in events:
        nav_txt, hot = describe_nav(pack, ev["close"], ev.get("prev"), float(z["premium_max"]))
        avg = avg_n(rows, ev["day"])
        avg_txt = f" 이날까지 20거래일 평균 {won(round(avg))}." if avg is not None else ""
        if ev["type"] == "zone":
            hold = " 프리미엄 과다, 주문 보류." if hot else ""
            title = f"{TAG} 소액 분할매수 검토 구간"
            status = (
                f"{ev['day']} KRX 종가 {won(ev['close'])}. 구간 {won(z['low'])}~{won(z['high'])}. "
                f"{nav_txt}{avg_txt}{hold} {up}"
            )
            if hot:
                action = "이미 정해 둔 소액만 지정가로 나눌지 검토. 프리미엄 과다, 주문 보류."
            elif "보류" in nav_txt:
                action = "이미 정해 둔 소액만 지정가로 나눌지 검토. 오늘 괴리를 앱에서 확인한 뒤에 주문."
            else:
                action = "이미 정해 둔 소액만 지정가로 나눌지 검토. 프리미엄이 과하지 않을 때만."
            later = f"종가가 {won(z['rearm_above'])} 위로 올라가거나 {won(z['rearm_below'])} 아래로 내려간 뒤에만 이 구간 알림이 다시 켜진다."
            avoid = "장중 가격이나 시간외 가격으로 따라 사기. 상향 근거 2개가 되기 전에 비중을 늘리기."
            what = "TIGER 바이오TOP10(364970) 정규장 종가가 소액 분할매수 검토 구간에 들어왔다는 알림. 추격 매수 알림이 아님."
            pri = int(z["priority"])
        elif ev["type"] == "break":
            title = f"{TAG} 6,075 종가 이탈"
            status = (
                f"{ev['day']} KRX 종가 {won(ev['close'])}, 6,075원 미만. "
                f"2거래일 뒤({ev['due']})에도 미만이면 추가매수 금지 알림이 한 번 더 온다. {nav_txt} {up}"
            )
            action = "새로 사는 금액은 보류. 이미 가진 수량은 이 알림만으로 팔지 않기."
            later = f"2거래일 안에 종가가 6,075원 이상으로 회복하면 추가 알림은 없다. 재무장은 {won(cfg['break']['rearm'])} 이상."
            avoid = "이탈 당일을 바닥으로 보고 바로 더 사기."
            what = "TIGER 바이오TOP10(364970) 정규장 종가가 6,075원 아래로 내려왔다는 경고. 매도 지시 아님."
            pri = int(cfg["break"]["priority"])
        else:
            title = f"{TAG} 2거래일 미회복, 추가매수 금지"
            status = (
                f"이탈일 {ev['break_day']} 이후 2거래일이 지난 {ev['day']} 종가 {won(ev['close'])}, 아직 6,075원 미만. {up}"
            )
            action = "추가매수 금지. 회복 전엔 새 지정가를 넣지 않기."
            later = f"종가가 {won(cfg['break']['rearm'])} 이상으로 올라와야 이탈 알림이 다시 켜진다."
            avoid = "미회복 상태에서 분할 매수를 이어 가기."
            what = "6,075원 이탈 뒤 2거래일 동안 종가가 회복되지 않았다는 알림. 매도 지시 아님."
            pri = int(cfg["break"]["priority"])
        msg = six(
            stamp, what, status, action, later,
            "이 알림은 매도 신호가 아님. 보유 비중은 따로 정한 기준으로.",
            avoid,
        )
        out.append((title, msg, pri, None))
    return out


def is_q4_2026(text: str) -> bool:
    t = re.sub(r"\s+", "", text or "")
    if "2026" not in t:
        return False
    if re.search(r"2026[.\-/년]*10[.\-/월]*0?1", t) and re.search(r"12[.\-/월]*31|12월31|4분기", t):
        return True
    if "4분기" in t or "4/4분기" in t:
        return True
    return False


def _eok_pair(text: str) -> tuple[int | None, int | None]:
    t = re.sub(r"\s+", " ", text or "")
    m = re.search(
        r"당기순이익\s+당해실적\s+(-?[\d,]+)(?:\s+전기실적\s+-?[\d,]+)?\s+전년(?:동기)?실적\s+(-?[\d,]+)",
        t,
    )
    if not m:
        return None, None
    cur = int(m.group(1).replace(",", ""))
    prior = int(m.group(2).replace(",", ""))
    if re.search(r"단위\s*:\s*백만원", t):
        cur //= 100
        prior //= 100
    return cur, prior


def mentions_keytruda_growth(text: str) -> bool:
    if "키트루다" not in (text or ""):
        return False
    return re.search(r"판매\s*성장|매출\s*성장|판매\s*증가|매출\s*증가|판매\s*확대|매출\s*확대", text) is not None


def _has_poly(text: str) -> bool:
    return re.search(r"PolyPeptide|폴리펩타이드|폴리펩티드", text or "", re.I) is not None


def _deal_closed(text: str) -> bool:
    for m in re.finditer(r"(인수|양수|취득).{0,8}(종결|완료)|(종결|완료).{0,8}(인수|양수|취득)", text or ""):
        window = text[max(0, m.start() - 6): m.end() + 6]
        if "예정" not in window:
            return True
    return False


def _alt_bad(text: str) -> bool:
    t = text or ""
    if re.search(r"임상.{0,8}(중단|중지|철회)", t):
        return True
    if re.search(r"(계약|라이선스|라이센스).{0,8}(해지|종료|취소)", t):
        return True
    if re.search(r"(마일스톤|기술료).{0,16}(지연|연기|미수령)", t):
        return True
    if re.search(r"(지연|연기|미수령).{0,16}(마일스톤|기술료)", t):
        return True
    return False


def _stale(filed: str, today: str, days: int = 10) -> bool:
    filed = (filed or "")[:10]
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", filed) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", today or ""):
        return False
    return (date.fromisoformat(today) - date.fromisoformat(filed)).days > days


def classify(f: dict, today: str = "") -> dict | str | None:
    """관련 공시면 dict, 본문이 더 필요하면 'wait', 아니면 None."""
    title = clean(f.get("title") or "")
    body = f.get("text") or ""
    blob = title + "\n" + body
    code = f.get("code")
    if code == "068270" and "잠정" in title and "실적" in title:
        if not body.strip():
            return None if _stale(f.get("filed") or "", today) else "wait"
        if not is_q4_2026(blob):
            return None
        parsed = earnings_parse.parse_results(body)
        cur, prior = _eok_pair(body)
        net = parsed.get("net")
        if net is None:
            net = cur
        op = parsed.get("op")
        ni = net is not None and prior is not None and net > prior and net > 0
        if op is None:
            kind, pri, head = "cell_unknown", 3, "068270 4Q 잠정 자동 판정 불가"
        elif op >= 6000:
            kind, pri, head = "cell_hit", 3, "068270 4Q 영업이익 6,000억 달성 | 상향 근거"
        else:
            kind, pri, head = "cell_miss", 4, "068270 4Q 영업이익 6,000억 미달 | 관망 강화"
        op_txt = "자동 판정 불가" if op is None else f"{op:,.0f}억원"
        if net is None or prior is None:
            ni_txt = "순이익 전년 대비 자동 판정 불가"
        else:
            ni_txt = f"순이익 {net:,.0f}억원, 전년 {prior:,.0f}억원, 회복 {'예' if ni else '아니오'}"
        return {
            "kind": kind, "priority": pri, "head": head, "op_ok": op is not None and op >= 6000,
            "ni_ok": bool(ni), "detail": f"영업이익 {op_txt}. {ni_txt}.",
            "keytruda": False,
        }
    if code == "196170" and re.search(r"마일스톤|기술료|임상|해지", title):
        if not re.search(r"마일스톤|기술료|임상|해지|중단", title):
            return None
        growth = mentions_keytruda_growth(blob)
        if _alt_bad(blob) or _alt_bad(title):
            return {
                "kind": "alt_bad", "priority": 4, "head": "196170 계약 해지 또는 임상 중단",
                "op_ok": False, "ni_ok": False, "keytruda": growth,
                "detail": "해지, 중단, 지연 문구가 있다. 입금 확인으로 세지 않는다.",
            }
        pending = "예정" in blob and not re.search(r"입금\s*완료|수령\s*완료|수령하였|입금되었|입금\s*확인", blob)
        if pending or "예정" in title:
            usd = re.search(r"USD\s*([\d,]+)", blob)
            extra = f" 금액 USD {usd.group(1)}." if usd else ""
            return {
                "kind": "alt_pending", "priority": 3, "head": "196170 마일스톤 수령 예정 | 입금 미확인",
                "op_ok": False, "ni_ok": False, "keytruda": growth,
                "detail": "청구 또는 수령 예정이다. 입금 확인이 아니라 상향 근거로 세지 않는다." + extra,
            }
        if re.search(r"수령|입금", title + blob):
            return {
                "kind": "alt_in", "priority": 3, "head": "196170 마일스톤 수령 확인",
                "op_ok": False, "ni_ok": False, "keytruda": growth, "cash_in": True,
                "detail": "수령 또는 입금 확인 문구가 있다.",
            }
        return None
    if code == "207940" and re.search(r"양수|취득결정|타법인", title):
        if not body.strip() and not _has_poly(title):
            return None if _stale(f.get("filed") or "", today) else "wait"
        if not _has_poly(blob):
            return None
        closed = _deal_closed(blob)
        pct = re.search(r"(\d{2}\.\d{2})\s*%", blob)
        extra = f" 응모 지분 {pct.group(1)}%." if pct else ""
        if closed:
            return {
                "kind": "sam_closed", "priority": 3, "head": "207940 PolyPeptide 인수 종결",
                "op_ok": False, "ni_ok": False, "keytruda": False, "deal_closed": True,
                "detail": "인수 종결 또는 양수 완료 문구가 있다." + extra,
            }
        return {
            "kind": "sam_update", "priority": 3, "head": "207940 PolyPeptide 인수 진행",
            "op_ok": False, "ni_ok": False, "keytruda": False,
            "detail": "PolyPeptide 인수 공시이나 종결 문구는 없다." + extra,
        }
    if code == "207940" and "단일판매" in title and "공급계약" in title:
        cancel = "해지" in title or re.search(r"공급계약\s*해지|계약\s*해지", blob)
        if cancel:
            return {
                "kind": "sam_cancel", "priority": 4, "head": "207940 단일판매 공급계약 해지",
                "op_ok": False, "ni_ok": False, "keytruda": False,
                "detail": "공급계약 해지 문구가 있다. 주요 수주로 세지 않는다.",
            }
        amts = re.findall(r"계약금액\(원\)\s*([\d,]+)", blob)
        extra = f" 계약금액 {int(amts[-1].replace(',', '')):,}원." if amts else ""
        return {
            "kind": "sam_contract", "priority": 3, "head": "207940 단일판매 공급계약 체결",
            "op_ok": False, "ni_ok": False, "keytruda": False, "major_order": True,
            "detail": "단일판매 공급계약 체결 공시." + extra,
        }
    return None


def apply_flags(p: dict, hit: dict) -> None:
    kind = hit["kind"]
    if kind == "cell_hit":
        p["celltrion_op_ok"] = True
    elif kind == "cell_miss":
        p["celltrion_op_ok"] = False
    if kind in {"cell_hit", "cell_miss", "cell_unknown"} and hit.get("ni_ok"):
        p["celltrion_ni_recover"] = True
    elif kind == "cell_miss" and hit.get("ni_ok") is False and "회복 아니오" in hit.get("detail", ""):
        p["celltrion_ni_recover"] = False
    if hit.get("deal_closed"):
        p["samsung_deal_closed"] = True
    if hit.get("major_order"):
        p["samsung_major_order"] = True
    if hit.get("cash_in") or kind == "alt_in":
        p["alteogen_cash_in"] = True
    if hit.get("keytruda"):
        p["alteogen_keytruda_growth"] = True


def filing_message(cfg: dict, f: dict, hit: dict, stamp: str, p: dict) -> tuple[str, str, int, str]:
    title = f"{TAG} {hit['head']}"
    link = f.get("url") or "https://dart.fss.or.kr"
    listed = cfg.get("samsung_listing") or {}
    list_line = ""
    if f.get("code") == "207940" and listed:
        list_line = f" 신주 상장일 {listed.get('day')} (수급 안정은 자동 판정 없음)."
    status = f"{clean(f.get('title') or '')} ({f.get('filed') or '날짜 없음'}, {f.get('src') or 'DART'}). {hit['detail']}{list_line} {pillar_line(p)} 링크 {link}"
    msg = six(
        stamp,
        "364970 상위 보유 종목 공시 중 미리 정한 문구만 전하는 알림. 매매 지시 아님.",
        status,
        "원문 숫자를 한 번 더 보고, 상향 근거가 2개가 되기 전에는 추격 매수하지 않기.",
        "같은 접수번호는 다시 보내지 않는다. 새 공시만 온다.",
        "공시 한 건으로 보유 수량을 줄이거나 늘리지 않기.",
        "제목만 보고 입금이나 인수 종결로 단정하기. 자동 판정 불가를 달성으로 읽기.",
    )
    return title, msg, int(hit["priority"]), str(f.get("rcp") or f.get("id"))


def parse_dart_html(page: str, code: str) -> list[dict]:
    out = []
    seen = set()
    for m in re.finditer(r'rcpNo=(\d{14})"[^>]*title="([^"]+)"', page or ""):
        rcp, title = m.group(1), clean(re.sub(r"\s*공시뷰어\s*새창\s*$", "", m.group(2)))
        if not title or "기업개황" in title or rcp in seen:
            continue
        seen.add(rcp)
        out.append({
            "id": f"dart:{code}:{rcp}",
            "code": code,
            "rcp": rcp,
            "title": title,
            "filed": f"{rcp[:4]}-{rcp[4:6]}-{rcp[6:8]}",
            "text": "",
            "url": DART_VIEW.format(rcp=rcp),
            "src": "DART",
        })
    return out


def parse_naver_notices(raw, code: str) -> list[dict]:
    out = []
    for r in raw if isinstance(raw, list) else []:
        if not isinstance(r, dict):
            continue
        nid = str(r.get("no") or r.get("bizdateSeq") or "")
        title = clean(str(r.get("title") or ""))
        filed = str(r.get("datetime") or "")[:10]
        if not nid or len(filed) != 10 or not title:
            continue
        text = re.sub(r"<[^>]+>", " ", str(r.get("contents") or ""))
        text = clean(re.sub(r"\s+", " ", text))
        out.append({
            "id": f"naver:{code}:{nid}",
            "code": code,
            "rcp": None,
            "nid": nid,
            "title": title,
            "filed": filed,
            "text": text,
            "url": f"https://m.stock.naver.com/domestic/stock/{code}/notice",
            "src": "네이버",
        })
    return out


def _attach_body(dart_rows: list[dict], naver_rows: list[dict]) -> list[dict]:
    used = set()
    for d in dart_rows:
        nt = norm_title(d["title"])
        hits = []
        for n in naver_rows:
            if n["id"] in used:
                continue
            nn = norm_title(n["title"])
            if not nt or not nn:
                continue
            if nt in nn or nn in nt:
                hits.append(n)
        same = [n for n in hits if n["filed"] == d["filed"]] or hits
        if len(same) == 1 or (same and same[0]["filed"] == d["filed"]):
            pick = same[0]
            d["text"] = pick.get("text") or ""
            d["naver_id"] = pick["id"]
            if pick.get("text"):
                used.add(pick["id"])
    extra = [n for n in naver_rows if n["id"] not in used]
    return dart_rows + extra


def default_filings(cfg: dict, since: str, until: str) -> tuple[list[dict], list[str]]:
    rows: list[dict] = []
    errs: list[str] = []
    bgn = since.replace("-", "")
    end = until.replace("-", "")
    key = os.environ.get("OPENDART_API_KEY") or ""
    for co in cfg["companies"]:
        code, cik, name = co["code"], co["dart_cik"], co["name"]
        dart_rows: list[dict] | None = None
        try:
            q = urllib.parse.urlencode({
                "textCrpCik": cik, "startDate": bgn, "endDate": end,
                "maxResults": "100", "currentPage": "1",
            })
            page = http(DART_SEARCH + "?" + q, headers={"Referer": "https://dart.fss.or.kr/dsab007/main.do"})
            dart_rows = parse_dart_html(page, code)
            if dart_rows is None:
                raise RuntimeError("목록 없음")
            if not dart_rows and "rcpNo=" not in page and "없습니다" not in page and name not in page:
                raise RuntimeError("DART 목록 형식 이상")
        except Exception as exc:  # noqa: BLE001
            dart_rows = None
            dart_err = f"{code} DART 목록 실패: {exc}"
        else:
            dart_err = ""
        if key and dart_rows is not None:
            try:
                q = urllib.parse.urlencode({
                    "crtfc_key": key, "corp_code": cik, "bgn_de": bgn, "end_de": end, "page_count": "100",
                })
                raw = json.loads(http(f"https://opendart.fss.or.kr/api/list.json?{q}"))
                if str(raw.get("status")) not in {"000", "013"}:
                    raise RuntimeError(str(raw.get("message") or raw.get("status")))
                have = {r["rcp"] for r in dart_rows}
                for item in raw.get("list") or []:
                    rcp = str(item.get("rcept_no") or "")
                    if len(rcp) != 14 or rcp in have:
                        continue
                    dart_rows.append({
                        "id": f"dart:{code}:{rcp}", "code": code, "rcp": rcp,
                        "title": clean(str(item.get("report_nm") or "")),
                        "filed": f"{rcp[:4]}-{rcp[4:6]}-{rcp[6:8]}",
                        "text": "", "url": DART_VIEW.format(rcp=rcp), "src": "DART",
                    })
            except Exception as exc:  # noqa: BLE001
                print(f"{code} OpenDART 보강 실패: {exc}")
        naver_rows: list[dict] | None = None
        try:
            naver_rows = parse_naver_notices(json.loads(http(NAVER_NOTICE.format(code=code))), code)
            naver_rows = [r for r in naver_rows if since <= r["filed"] <= until]
        except Exception as exc:  # noqa: BLE001
            naver_rows = None
            naver_err = f"{code} 네이버 공시 실패: {exc}"
        else:
            naver_err = ""
        if dart_rows is None and naver_rows is None:
            errs.append(clean(f"{dart_err}; {naver_err}")[:220])
            continue
        if dart_rows is None:
            rows.extend(naver_rows or [])
            print(f"{code} DART 실패, 네이버 공시로 대체")
        else:
            rows.extend(_attach_body(dart_rows, naver_rows or []))
    return rows, errs


def default_rows(code: str, now: datetime) -> list[dict]:
    import krx_close
    bars = krx_close.regular_bars(code, 40, compare=False)
    done = krx_close.completed(bars, now, CLOSE_HM)
    return [{"day": b.day, "close": float(b.close)} for b in done]


def default_nav(code: str) -> dict:
    raw = json.loads(http(f"https://m.stock.naver.com/api/stock/{code}/integration"))
    k = raw.get("etfKeyIndicator") or {}
    inav = None
    for row in raw.get("totalInfos") or []:
        if row.get("code") == "nav":
            inav = num(row.get("value"))
    nav, dev = num(k.get("nav")), num(k.get("deviationRate"))
    if nav is None or dev is None:
        raise RuntimeError("네이버 nav 없음")
    return {"nav": nav, "dev": dev, "sign": k.get("deviationSign"), "inav": inav}


def intraday_note(code: str) -> str:
    try:
        basic = json.loads(http(f"https://m.stock.naver.com/api/stock/{code}/basic"))
    except Exception as exc:  # noqa: BLE001
        return f"장중 참고 조회 실패: {exc}"
    px = basic.get("closePrice")
    status = basic.get("marketStatus")
    if status and status != "CLOSE":
        return f"장중 참고 {px} ({basic.get('fluctuationsRatio')}%, {basic.get('localTradedAt')}). 판정에는 쓰지 않음."
    return ""


def ops_message(stamp: str, status: str) -> tuple[str, str]:
    title = f"{TAG} 시세 또는 공시 조회 실패 | 하루 1번"
    msg = six(
        stamp,
        "364970 알림이 시세나 공시를 읽지 못했다는 안내. 매매 신호 아님.",
        status,
        "증권 앱에서 364970 종가와 상위 종목 공시를 직접 보세요.",
        "다음 15:40 슬롯에서 다시 읽는다. 이 안내는 하루 1번.",
        "이 안내로 비중을 바꾸지 않기.",
        "조회 실패를 공시 없음이나 가격 안정으로 읽기.",
    )
    return title, msg


def _seen_add(st: dict, f: dict) -> None:
    seen = st.setdefault("seen", [])
    for i in [f.get("id"), f.get("naver_id")]:
        if i and i not in seen:
            seen.append(i)
    if len(seen) > 500:
        del seen[:-500]


def _seen_has(st: dict, f: dict) -> bool:
    seen = set(st.get("seen") or [])
    return bool({f.get("id"), f.get("naver_id")} & seen)


def run(mode: str, now: datetime, cfg: dict, pub=publish, rows_fn=None, nav_fn=None,
        filings_fn=None, state=None, quote: bool = False) -> int:
    dry = mode == "dry"
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    topic = resolve_topic("port", cfg.get("ntfy_topic"))
    server = resolve_server(cfg.get("ntfy_server"))
    print(f"{stamp} {NAME} mode={mode}")
    ends = datetime.strptime(cfg["ends_kst"], "%Y-%m-%d %H:%M").replace(tzinfo=KST)
    if now > ends:
        print(f"{NAME} ended; silent")
        return 0
    sday = session_day(now)
    today = sday.isoformat()
    if today > cfg["window_end"] or today < cfg["window_start"]:
        print(f"{today} 감시 창 밖: 조용히 종료")
        return 0
    st = load_state() if state is None else state
    if dry:
        st = copy.deepcopy(st)
    p = pillars_of(st)
    live = rows_fn is None and nav_fn is None and filings_fn is None
    rows_fn = rows_fn or (lambda code: default_rows(code, now))
    nav_fn = nav_fn or default_nav
    filings_fn = filings_fn or default_filings
    errs: list[str] = []
    rows: list[dict] | None = []
    if is_trading_day(sday):
        try:
            rows = rows_fn(cfg["code"])
        except Exception as exc:  # noqa: BLE001
            rows = None
            errs.append(f"종가 조회 실패: {exc}"[:200])
        if rows is not None and not rows:
            errs.append("정규장 종가 없음")
            rows = None
    else:
        print(f"{today} KRX 휴장: 종가 판정 없음")
        rows = []
    pack = None
    events: list[dict] = []
    if rows:
        last = rows[-1]
        prev = rows[-2]["close"] if len(rows) > 1 else None
        print(f"latest KRX close {last['day']} {won(last['close'])}")
        try:
            pack = nav_fn(cfg["code"])
            which = nav_which(float(pack["nav"]), float(pack["dev"]), str(pack.get("sign") or ""), float(last["close"]), prev)
            print(f"NAV {pack['nav']} sign={pack.get('sign')} dev={pack['dev']} which={which} inav={pack.get('inav')}")
        except Exception as exc:  # noqa: BLE001
            pack = None
            errs.append(f"NAV 조회 실패: {exc}"[:200])
        if live and quote:
            note = intraday_note(cfg["code"])
            if note:
                print(note)
        if not (st.get("price") or {}).get("last_date"):
            st["price"] = seed_price(cfg, last["day"], float(last["close"]))
            where = "구간 안" if in_band(cfg, float(last["close"])) else "구간 밖"
            print(f"SEED price {last['day']} {won(last['close'])} {where} zone_armed={st['price']['zone_armed']} break_armed={st['price']['break_armed']}")
        else:
            events = evaluate_prices(cfg, st["price"], rows)
    out = price_messages(cfg, events, stamp, rows or [], pack, p)
    since = (sday - timedelta(days=45)).isoformat()
    try:
        filings, ferrs = filings_fn(cfg, since, today)
    except Exception as exc:  # noqa: BLE001
        filings, ferrs = [], [f"공시 조회 실패: {exc}"[:200]]
    errs.extend(ferrs)
    relevant = 0
    if not ferrs:
        if not st.get("filings_seeded"):
            for f in filings:
                _seen_add(st, f)
                hit = classify(f, today)
                if hit in (None, "wait"):
                    continue
                relevant += 1
                apply_flags(p, hit)
                print(f"SEED filing {hit['kind']} {f.get('filed')} {clean(f.get('title') or '')[:80]}")
            st["filings_seeded"] = True
            print(f"SEED filings {len(filings)} rows, relevant {relevant}, no send")
        else:
            sent_kind = set()
            for f in filings:
                if _seen_has(st, f):
                    continue
                hit = classify(f, today)
                if hit == "wait":
                    print(f"WAIT body {f.get('id')} {clean(f.get('title') or '')[:80]}")
                    continue
                _seen_add(st, f)
                if not hit:
                    continue
                kind_key = (f.get("code"), (f.get("filed") or "")[:10], hit["kind"])
                if kind_key in sent_kind:
                    print(f"dedupe {f.get('id')} same day {hit['kind']}")
                    continue
                sent_kind.add(kind_key)
                apply_flags(p, hit)
                out.append(filing_message(cfg, f, hit, stamp, p))
                relevant += 1
    else:
        print("filings skipped:", "; ".join(ferrs)[:300])
    print("pillars", {k: bool(p.get(k)) for k in PILLAR_KEYS}, "score", f"{pillar_score(p)[0]}/3")
    ops = None
    if errs and st.get("ops_day") != today:
        ops = ops_message(stamp, " ".join(clean(e) for e in errs)[:500])
        st["ops_day"] = today
    for item in out:
        check_text(item[0], item[1])
        print(f"TITLE: {item[0]}\nPRIORITY: {item[2]} TOPIC: {route_topic(topic, item[0])}\n{item[1]}\n")
    if ops:
        check_text(ops[0], ops[1])
        print(f"OPS TITLE: {ops[0]} -> {route_topic(topic, ops[0])}\n{ops[1]}\n")
    if not out and not ops:
        print("보낼 것 없음")
    if dry:
        print("DRY RUN: no publish, state not written")
        return 0
    for title, msg, pri, key in out:
        pub(topic, title, msg, pri, ["chart_with_downwards_trend"], server, config_name=NAME, dedup_key=key)
    if ops:
        try:
            pub(resolve_topic("ops"), ops[0], ops[1], 3, ["warning"], server, config_name=NAME)
        except Exception as exc:  # noqa: BLE001
            print(f"ops notice failed: {exc}")
    st["updated_at"] = stamp
    if state is None:
        save_state(st)
    return 0


def selftest() -> int:
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    fails = 0

    def check(label, ok):
        nonlocal fails
        fails += not ok
        print(("ok  " if ok else "FAIL"), label)

    z, br = cfg["zone"], cfg["break"]
    check("rearm below is 3%", z["rearm_below"] == 6400 * 97 // 100)
    check("break rearm is 3% truncated", br["rearm"] == 6075 * 103 // 100)
    check("6075 itself is not a break", not (6075 < br["level"]))
    check("band edges", in_band(cfg, 6400) and in_band(cfg, 6500) and not in_band(cfg, 6399) and not in_band(cfg, 6501))

    ps = seed_price(cfg, "2026-11-26", 6405)
    check("seed inside band disarms zone", ps["zone_armed"] is False and ps["break_armed"] is True and ps["follow_pending"] is False)
    ev = evaluate_prices(cfg, ps, [{"day": "2026-11-27", "close": 6480}])
    check("still inside band does not resend", ev == [] and ps["zone_armed"] is False)
    ev = evaluate_prices(cfg, ps, [{"day": "2026-11-30", "close": 6600}, {"day": "2026-12-01", "close": 6601}])
    check("6600 does not rearm, 6601 does", ev == [] and ps["zone_armed"] is True)
    ev = evaluate_prices(cfg, ps, [{"day": "2026-12-02", "close": 6450}])
    check("reentry fires once", [e["type"] for e in ev] == ["zone"] and ps["zone_armed"] is False)
    ev = evaluate_prices(cfg, ps, [{"day": "2026-12-03", "close": 6300}, {"day": "2026-12-04", "close": 6208}])
    check("buffer below 6400 does not rearm", ev == [] and ps["zone_armed"] is False)
    ev = evaluate_prices(cfg, ps, [{"day": "2026-12-07", "close": 6207}])
    check("below 6208 rearms", ev == [] and ps["zone_armed"] is True)

    ps = seed_price(cfg, "2026-11-20", 6700)
    ev = evaluate_prices(cfg, ps, [
        {"day": "2026-11-23", "close": 6075},
        {"day": "2026-11-24", "close": 6074},
        {"day": "2026-11-25", "close": 6100},
    ])
    check("6075 no break, 6074 breaks, 6100 recovers with no follow",
          [e["type"] for e in ev] == ["break"] and ev[0]["due"] == "2026-11-26" and ps["follow_pending"] is False)
    check("6100 does not rearm break", ps["break_armed"] is False)
    ev = evaluate_prices(cfg, ps, [{"day": "2026-11-26", "close": 6257}])
    check("6257 rearms break", ev == [] and ps["break_armed"] is True)

    ps = seed_price(cfg, "2026-11-20", 6700)
    ev = evaluate_prices(cfg, ps, [
        {"day": "2026-11-23", "close": 6000},
        {"day": "2026-11-24", "close": 6010},
        {"day": "2026-11-25", "close": 6020},
    ])
    check("2nd session still below sends follow", [e["type"] for e in ev] == ["break", "follow"])
    check("follow due is 11-25", ev[0]["due"] == "2026-11-25" and ev[1]["day"] == "2026-11-25")
    ev = evaluate_prices(cfg, ps, [{"day": "2026-11-26", "close": 5900}])
    check("follow does not repeat", ev == [])

    ps = seed_price(cfg, "2026-11-26", 6700)
    ev = evaluate_prices(cfg, ps, [
        {"day": "2026-11-27", "close": 6000},
        {"day": "2026-11-30", "close": 6010},
        {"day": "2026-12-01", "close": 5900},
    ])
    check("friday break waits across weekend", [e["type"] for e in ev] == ["break", "follow"] and ev[1]["day"] == "2026-12-01")

    ps = seed_price(cfg, "2026-11-26", 6000)
    ev = evaluate_prices(cfg, ps, [{"day": "2026-11-27", "close": 5900}, {"day": "2026-11-30", "close": 5800}])
    check("seed already below does not start the 2-day note", ev == [] and ps["follow_pending"] is False)

    rows = [{"day": f"2026-11-{i:02d}", "close": 6500 + i} for i in range(1, 27) if i not in (7, 8, 14, 15, 21, 22)]
    # build 20 real dates ending 11-26 from the verify set
    closes = [6610, 6640, 6580, 6670, 6950, 6775, 6565, 6515, 6590, 6545, 6545, 6500, 6505, 6295, 6390, 6380, 6075, 6140, 6440, 6405]
    days = ["2026-10-30", "2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05", "2026-11-06", "2026-11-09",
            "2026-11-10", "2026-11-11", "2026-11-12", "2026-11-13", "2026-11-16", "2026-11-17", "2026-11-18",
            "2026-11-19", "2026-11-20", "2026-11-23", "2026-11-24", "2026-11-25", "2026-11-26"]
    series = [{"day": d, "close": c} for d, c in zip(days, closes)]
    check("verified 20-day average", round(avg_n(series, "2026-11-26"), 2) == 6505.75)

    pack_today = {"nav": 6427.60, "dev": 0.35, "sign": "-", "inav": 6756.2}
    check("11/26 nav matches that close", nav_which(6427.60, 0.35, "-", 6405, 6440) == "today")
    check("same nav is previous vs a later close", nav_which(6427.60, 0.35, "-", 6745, 6405) == "prev")
    hot_txt, hot = describe_nav({"nav": 6400, "dev": 0.31, "sign": "+"}, 6420, 6300, 0.002)
    check("premium over 0.20 flags", hot and "프리미엄 과다, 주문 보류" in hot_txt)
    mild_txt, mild = describe_nav({"nav": 6400, "dev": 0.16, "sign": "+"}, 6410, 6300, 0.002)
    check("0.16% premium does not flag", (not mild) and "주문 보류" not in mild_txt)

    ps = seed_price(cfg, "2026-11-26", 6700)
    ev = evaluate_prices(cfg, ps, [{"day": "2026-11-27", "close": 6450}])
    msgs = price_messages(cfg, ev, "2026-11-27 15:40 KST", series + [{"day": "2026-11-27", "close": 6450}], pack_today, pillars_of({}))
    check("zone title", msgs[0][0] == "[364970] 소액 분할매수 검토 구간")
    check("zone priority", msgs[0][2] == 3)
    check("upgrade count in zone", "지금 0/3" in msgs[0][1] and "3개 중 2개" in msgs[0][1])
    for t, m, _pri, _k in msgs:
        check_text(t, m)

    q4 = (
        "연결 영업(잠정)실적 2026.10.01 ~ 2026.12.31 단위 : 억원 "
        "영업이익 당해실적 6,100 전년실적 5,000 "
        "당기순이익 당해실적 4,200 전기실적 1,000 전년실적 3,000"
    )
    hit = classify({"code": "068270", "title": "연결재무제표기준영업(잠정)실적(공정공시)", "text": q4, "filed": "2027-01-28"}, "2027-01-28")
    check("q4 hit", isinstance(hit, dict) and hit["kind"] == "cell_hit" and hit["ni_ok"] is True)
    q3 = q4.replace("2026.10.01", "2026.07.01").replace("2026.12.31", "2026.09.30").replace("4분기", "3분기")
    check("q3 prelim ignored", classify({"code": "068270", "title": "연결재무제표기준영업(잠정)실적(공정공시)", "text": q3, "filed": "2026-10-12"}, "2026-10-12") is None)
    miss = q4.replace("6,100", "5,900").replace("4,200", "2,000").replace("전년실적 3,000", "전년실적 3,500")
    hit_m = classify({"code": "068270", "title": "영업(잠정)실적", "text": miss, "filed": "2027-01-28"}, "2027-01-28")
    check("q4 miss", isinstance(hit_m, dict) and hit_m["kind"] == "cell_miss" and hit_m["priority"] == 4 and hit_m["ni_ok"] is False)
    check("empty prelim waits", classify({"code": "068270", "title": "영업(잠정)실적", "text": "", "filed": "2027-01-28"}, "2027-01-28") == "wait")
    check("stale empty prelim dropped", classify({"code": "068270", "title": "영업(잠정)실적", "text": "", "filed": "2026-12-01"}, "2027-01-28") is None)

    pend = classify({
        "code": "196170",
        "title": "투자판단 관련 주요경영사항(ALT-B4 라이선스 계약 파트너사 MSD로부터 판매 마일스톤 수령 예정)",
        "text": "마일스톤 USD 40,000,000 을 수령 예정입니다. 키트루다 피하주사 판매 마일스톤. 30일 이내 수령할 예정입니다.",
        "filed": "2026-11-26",
    }, "2026-11-27")
    check("milestone invoice is pending not cash", isinstance(pend, dict) and pend["kind"] == "alt_pending" and not pend.get("cash_in") and pend["keytruda"] is False)
    paid = classify({
        "code": "196170",
        "title": "투자판단 관련 주요경영사항(마일스톤 기술료 수령)",
        "text": "기술료를 수령하였습니다. 키트루다 SC 매출 증가.",
        "filed": "2026-12-20",
    }, "2026-12-20")
    check("receipt sets cash and keytruda growth", isinstance(paid, dict) and paid["kind"] == "alt_in" and paid["keytruda"] is True)
    bad = classify({
        "code": "196170", "title": "투자판단 관련 주요경영사항(임상 중단)",
        "text": "임상 중단을 결정했습니다.", "filed": "2026-12-01",
    }, "2026-12-01")
    check("clinical stop", isinstance(bad, dict) and bad["kind"] == "alt_bad" and bad["priority"] == 4)
    check("price-limit title ignored", classify({
        "code": "196170", "title": "주식선물 주식옵션 2단계 가격제한폭 확대요건 도달",
        "text": "관련공시 마일스톤 수령", "filed": "2026-11-27",
    }, "2026-11-27") is None)

    deal = classify({
        "code": "207940", "title": "(정정)타법인 주식 및 출자증권 양수결정",
        "text": "PolyPeptide Group AG 최종 응모 지분 97.98% 를 기록. 양수예정일자 2026년 12월 31일. 100%를 확보할 예정입니다.",
        "filed": "2026-11-06",
    }, "2026-11-27")
    check("tender update is not closed", isinstance(deal, dict) and deal["kind"] == "sam_update" and not deal.get("deal_closed"))
    closed = classify({
        "code": "207940", "title": "타법인 주식 및 출자증권 양수결정",
        "text": "PolyPeptide 인수 종결. 양수 완료.",
        "filed": "2026-12-31",
    }, "2026-12-31")
    check("deal closed", isinstance(closed, dict) and closed.get("deal_closed") is True)
    check("unrelated acquisition ignored", classify({
        "code": "207940", "title": "타법인 주식 및 출자증권 양수결정",
        "text": "다른 회사 주식 양수.", "filed": "2026-11-06",
    }, "2026-11-27") is None)
    con = classify({
        "code": "207940", "title": "단일판매ㆍ공급계약체결",
        "text": "계약금액(원) 434,322,560,000", "filed": "2026-11-03",
    }, "2026-11-27")
    check("supply contract", isinstance(con, dict) and con["kind"] == "sam_contract" and con.get("major_order") is True and "434,322,560,000" in con["detail"])
    check("rights issue ignored", classify({
        "code": "207940", "title": "추가상장(유상증자(주주배정 후 실권주 일반공모))",
        "text": "상장일 2026년11월30일", "filed": "2026-11-25",
    }, "2026-11-27") is None)
    check("quarterly report ignored", classify({
        "code": "068270", "title": "분기보고서 (2026.09)", "text": "영업이익 5,100억", "filed": "2026-11-14",
    }, "2026-11-27") is None)

    p = pillars_of({})
    apply_flags(p, hit)
    check("pillar cell both", pillar_score(p) == (1, [True, False, False]))
    cash_only = dict(paid)
    cash_only["keytruda"] = False
    apply_flags(p, cash_only)
    check("cash alone is not the third pillar", pillar_score(p)[0] == 1 and p["alteogen_cash_in"] and not p["alteogen_keytruda_growth"])
    apply_flags(p, con)
    check("order without flow is not the second pillar", pillar_score(p)[0] == 1 and p["samsung_major_order"] and not p["samsung_flow_stable"])
    p["samsung_flow_stable"] = True
    check("flow plus order is the second pillar", pillar_score(p)[0] == 2)
    apply_flags(p, paid)
    check("keytruda plus cash is the third pillar", pillar_score(p) == (3, [True, True, True]))

    dart_page = '''
    <a href="/dsaf001/main.do?rcpNo=20261126900206" title="투자판단관련주요경영사항(마일스톤 수령 예정) 공시뷰어 새창">x</a>
    <a title="알테오젠 기업개황 새창">skip</a>
    '''
    parsed = parse_dart_html(dart_page, "196170")
    check("dart parse keeps the filing", len(parsed) == 1 and parsed[0]["rcp"] == "20261126900206")
    merged = _attach_body(parsed, [{
        "id": "naver:196170:1", "title": "알테오젠 투자판단관련주요경영사항(마일스톤 수령 예정)",
        "filed": "2026-11-26", "text": "USD 40,000,000 수령 예정", "code": "196170",
    }])
    check("naver body attached once", merged[0]["text"].startswith("USD") and len(merged) == 1)

    published = []

    def fake_pub(*a, **k):
        published.append((a, k))

    def rows_of(_code):
        return [{"day": "2026-11-26", "close": 6405.0}, {"day": "2026-11-27", "close": 6745.0}]

    def nav_of(_code):
        return {"nav": 6427.60, "dev": 0.35, "sign": "-", "inav": 6756.2}

    def files_of(_cfg, _since, _until):
        return ([{
            "id": "dart:196170:20261126900206", "code": "196170", "rcp": "20261126900206",
            "title": "마일스톤 수령 예정", "filed": "2026-11-26", "text": "USD 40,000,000 수령 예정입니다.",
            "url": "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20261126900206", "src": "DART",
        }], [])

    st: dict = {}
    now = datetime(2026, 11, 27, 15, 45, tzinfo=KST)
    rc = run("auto", now, cfg, pub=fake_pub, rows_fn=rows_of, nav_fn=nav_of, filings_fn=files_of, state=st)
    check("first auto seeds and does not send", rc == 0 and published == [] and st.get("filings_seeded") is True)
    check("seed records only the latest close", st["price"]["last_date"] == "2026-11-27" and st["price"]["last_close"] == 6745.0 and st["price"]["zone_armed"] is True)
    rc = run("auto", datetime(2026, 11, 30, 15, 45, tzinfo=KST), cfg, pub=fake_pub, rows_fn=rows_of, nav_fn=nav_of, filings_fn=files_of, state=st)
    check("second run dedupes the seeded filing and the same close", rc == 0 and published == [])

    published.clear()
    st2: dict = {"price": seed_price(cfg, "2026-11-26", 6700), "filings_seeded": True, "seen": [], "pillars": {}}
    def down_rows(_code):
        return [{"day": "2026-11-26", "close": 6700.0}, {"day": "2026-11-27", "close": 6450.0}]
    rc = run("dry", now, cfg, pub=fake_pub, rows_fn=down_rows, nav_fn=nav_of, filings_fn=files_of, state=st2)
    check("dry sends nothing and does not mark the new filing", rc == 0 and published == [] and st2.get("seen") == [])

    published.clear()
    st3: dict = {"price": seed_price(cfg, "2026-11-26", 6700), "filings_seeded": True, "seen": [], "pillars": {}}
    rc = run("auto", now, cfg, pub=fake_pub, rows_fn=down_rows, nav_fn=nav_of, filings_fn=files_of, state=st3)
    titles = [a[0][1] for a in published]
    check("auto sends zone and new filing", rc == 0 and any(t.startswith("[364970] 소액") for t in titles) and any("196170" in t for t in titles))
    rc = run("auto", datetime(2026, 11, 30, 15, 45, tzinfo=KST), cfg, pub=fake_pub, rows_fn=down_rows, nav_fn=nav_of, filings_fn=files_of, state=st3)
    check("same filing and same close do not send again", len(published) == 2)

    published.clear()
    def boom(_code):
        raise RuntimeError("down")
    st4: dict = {"price": seed_price(cfg, "2026-11-26", 6700), "filings_seeded": True, "seen": [], "pillars": {}}
    run("auto", now, cfg, pub=fake_pub, rows_fn=boom, nav_fn=nav_of, filings_fn=lambda c, s, u: ([], ["196170 DART 목록 실패"]), state=st4)
    run("auto", now, cfg, pub=fake_pub, rows_fn=boom, nav_fn=nav_of, filings_fn=lambda c, s, u: ([], ["196170 DART 목록 실패"]), state=st4)
    ops_n = [a for a in published if "조회 실패" in a[0][1]]
    published.clear()
    st5: dict = {"price": seed_price(cfg, "2026-11-05", 6700), "filings_seeded": True, "seen": [], "pillars": {}}

    def pair(_cfg, _since, _until):
        return ([
            {"id": "dart:207940:1", "code": "207940", "rcp": "1", "title": "타법인 주식 및 출자증권 양수결정",
             "filed": "2026-11-06", "text": "PolyPeptide 응모 지분 97.98%. 양수예정일자.", "url": "https://dart.fss.or.kr/a", "src": "DART"},
            {"id": "naver:207940:2", "code": "207940", "rcp": None, "title": "(정정)타법인 주식 및 출자증권 양수결정",
             "filed": "2026-11-06", "text": "PolyPeptide 응모 지분 97.98%.", "url": "https://m.stock.naver.com/x", "src": "네이버"},
        ], [])

    run("auto", now, cfg, pub=fake_pub, rows_fn=lambda c: [{"day": "2026-11-06", "close": 6700.0}],
        nav_fn=nav_of, filings_fn=pair, state=st5)
    sam_titles = [a[0][1] for a in published if "207940" in a[0][1]]
    check("same day same kind sends once", len(sam_titles) == 1)
    check("ops once per day", len(ops_n) == 1 and route_topic("cwally-port", ops_n[0][0][1]) == "cwally-ops")
    check("ops title has ticker", ops_n[0][0][1].startswith("[364970]"))

    print("selftest", "PASS" if not fails else f"FAIL {fails}")
    return 1 if fails else 0


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    if mode == "selftest":
        return selftest()
    if mode not in {"auto", "dry"}:
        print(__doc__)
        return 2
    return run(mode, datetime.now(KST), cfg, quote=True)


if __name__ == "__main__":
    sys.exit(main())
