#!/usr/bin/env python3
"""[XLU] SPDR 유틸리티 ETF 판단(긍정 / 중립 / 부정). 평일 09:00 KST morning 묶음 (xbi, hwm 옆).

2026-11-02 채팅 승인. 보유 없음, 가격 매수 구간 없음(configs/xlu.json price_bands 는 나중에 채울 자리).
판단은 미 10년물 완료 종가(Yahoo ^TNX, 실패 시 FRED DGS10)와 FOMC 결과로만 정한다.
  완료 봉: 미국 동부 16:10 이후면 그날 봉을 넣고, 그 전이면 직전 평일까지. 토, 일 봉은 넣지 않는다.
  휴장일은 봉이 없으므로 그 전 평일이 마지막이다. close 가 비고 regularMarketTime 이 16:00 ET 이후일 때만 regularMarketPrice 로 채운다.
  2026-11-21 채팅 승인. 이미 저장한 데이터 날짜는 다시 보내지 않는다.
  긍정  10년물 종가 4.50% 미만 3거래일 연속 + 12월 FOMC 동결(인상 없음)
        12월 FOMC 전이면 중립 유지 + "10년물 4.5% 하회, 12월 FOMC 동결 확인 대기" 안내 1번 (p3)
  부정  10년물 종가 5.30% 초과, 또는 FOMC 인상이 새로 확인됨 (12월, 그 뒤 1월)
  중립  그 밖
  여유  부정에서 벗어나려면 종가 5.20% 미만, 긍정에서 벗어나려면 종가 4.60% 이상 또는 그 뒤 인상
FOMC 결과: 성명서 원문 'decided to raise / maintain / lower', 못 읽으면 FRED DFEDTARU.
분할 지정가 가격대(configs/xlu.json price_bands, 2026-11-02 승인, 목표 비중 5%):
  1차 41.70~42.00 (2%, FOMC 전, 2026-12-09 까지) / 2차 42.40~43.00 (2%, 12/10부터 12월 동결 또는 인하일 때)
  3차 39.50~40.00 (1%, 12/10부터) / 재검토 종가 39.00 미만 / 추격 금지 종가 45.50 이상
  들어오면 1번, 경계 밖 0.3% 넘게 벗어나면 이탈 1번 (XLU 데이터 날짜로 중복 방지). filled=true 면 그 가격대 끝.
판단이 바뀔 때만 p4. 안내와 조회 실패는 p3 (실패는 cwally-ops, 하루 1번). 10년물과 XLU 데이터 날짜로 중복 방지.
첫 실행은 오늘 판단을 조용히 저장. 2027-02-26 23:59 KST 이후 침묵.
상대 매력(rotation, 2026-11-20): XLP / XLU / XLY. 기존 판단, 가격대, ends_kst 는 그대로.
  XLU 상향: XLU/XLP 가 20일선과 60일선 위 + 20거래일 상대 성과 +2%p, 그리고 금리 완화 또는 NEE/CEG 주도.
  XLY 상향: XLY/XLP 같은 조건 + AMZN/TSLA 강세. 금리 완화와 소매판매 반등이 같이 있으면 매크로 확인, 아니면 가격 선행.
  XLP 우위: FRED 실질 소매판매 신규 월이 둔화이고 XLP 가 XLU, XLY 보다 강할 때 그 월 1번.
  state 에 rotation 이 없으면 첫 실행은 조용히 시드. 조회가 실패해도 기존 판단은 그대로 가고, cwally-ops 는 하루 1번.
modes: auto | dry (발송 없음, state 안 씀) | selftest (가짜 값, 네트워크 없음)
"""
from __future__ import annotations

import json
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from html import unescape
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, route_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
ET = ZoneInfo("America/New_York")
CFG_PATH = ROOT / "configs" / "xlu.json"
STATE_PATH = ROOT / "state" / "xlu.json"
UA = {"User-Agent": "Mozilla/5.0 (cwally-alerts; +mailto:sosmos3218@gmail.com)"}
POS, NEU, NEG = "긍정", "중립", "부정"
END_LINE = "꺼지는 때: 2027-02-26 이후 자동 발송 없음"
DISCLAIMER = "참고용, 투자 권유 아님"
ENDS_AT = datetime(2027, 2, 26, 23, 59, tzinfo=KST)
PRE_NOTE = "10년물 4.5% 하회, 12월 FOMC 동결 확인 대기"
BANNED_RE = re.compile("[\u00b7\u318d\u2014]")
DEC_KO = {"hike": "인상", "hold": "동결", "cut": "인하"}


# ---------- io ----------

def load_cfg() -> dict:
    return json.loads(CFG_PATH.read_text(encoding="utf-8"))


def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(st: dict, now: datetime) -> None:
    st["updated_at"] = now.strftime("%Y-%m-%d %H:%M KST")
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def fetch(url: str, timeout: int = 25) -> str:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def keep_session(day: str, now: datetime) -> bool:
    """Completed US session. Today only at or after 16:10 ET. Saturday and Sunday never count."""
    d = datetime.strptime(day, "%Y-%m-%d").date()
    if d.weekday() >= 5:
        return False
    today = now.astimezone(ET).date()
    if d < today:
        return True
    if d > today:
        return False
    et = now.astimezone(ET)
    return (et.hour, et.minute) >= (16, 10)


def yahoo_series(sym: str, now: datetime, ten: bool = False) -> list[tuple[str, float]]:
    """Yahoo 1mo daily closes, completed-session filter, null-close fill via bars_from_chart."""
    raw = json.loads(fetch(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?interval=1d&range=1mo"))
    return bars_from_chart(raw, now, ten=ten)


def fred_series(sid: str, start: str) -> list[tuple[str, float]]:
    from fred_fetch import fetch_fred_csv
    txt = fetch_fred_csv(sid, start)
    out = []
    for line in txt.splitlines()[1:]:
        parts = line.strip().split(",")
        if len(parts) == 2 and parts[1] not in ("", "."):
            try:
                out.append((parts[0], float(parts[1])))
            except ValueError:
                pass
    return out


def ten_series(now: datetime) -> tuple[list[tuple[str, float]], str, list[str]]:
    """Completed 10Y closes. Yahoo ^TNX first, else FRED DGS10 with the same session dates.

    ibb_monitor imports this. A date already stored in state is not judged again.
    """
    errs = []
    try:
        s = yahoo_series("%5ETNX", now, ten=True)
        if len(s) >= 3:
            return s, "Yahoo ^TNX", errs
        errs.append("Yahoo ^TNX 3일 미만")
    except Exception as exc:  # noqa: BLE001
        errs.append(f"Yahoo ^TNX: {exc}")
    try:
        s = [x for x in fred_series("DGS10", (now - timedelta(days=40)).strftime("%Y-%m-%d")) if keep_session(x[0], now)]
        if len(s) >= 3:
            return s, "FRED DGS10", errs
        errs.append("FRED DGS10 3일 미만")
    except Exception as exc:  # noqa: BLE001
        errs.append(f"FRED DGS10: {exc}")
    return [], "", errs


def statement_decision(url: str) -> str | None:
    t = re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", fetch(url))))
    m = re.search(r"decided to (raise|maintain|lower) the target range", t)
    return {"raise": "hike", "maintain": "hold", "lower": "cut"}[m.group(1)] if m else None


def fomc_decisions(cfg: dict, now: datetime) -> tuple[dict, dict, list[str]]:
    """{key: hike|hold|cut|None(아직)|"unknown"}, {key: 출처}, errors."""
    dec, src, errs = {}, {}, []
    fred = None
    prev_upper = cfg["fomc"]["upper_before"]
    for m in cfg["fomc"]["meetings"]:
        known = datetime.strptime(m["known_kst"], "%Y-%m-%d %H:%M").replace(tzinfo=KST)
        if now < known:
            dec[m["key"]] = None
            continue
        d = None
        try:
            d = statement_decision(m["statement"])
            if d:
                src[m["key"]] = "성명서 원문"
        except Exception as exc:  # noqa: BLE001
            errs.append(f"성명서 {m['key']}: {exc}")
        if d is None:
            try:
                fred = fred if fred is not None else fred_series("DFEDTARU", "2026-11-01")
                after = [v for day, v in fred if day > m["decision_et"]]
                if after:
                    d = "hike" if after[0] > prev_upper else ("cut" if after[0] < prev_upper else "hold")
                    src[m["key"]] = "FRED DFEDTARU"
            except Exception as exc:  # noqa: BLE001
                errs.append(f"FRED DFEDTARU: {exc}")
        dec[m["key"]] = d or "unknown"
        if d == "hike":
            prev_upper += 0.25
        elif d == "cut":
            prev_upper -= 0.25
    return dec, src, errs


# ---------- judgement (pure) ----------

def step_zone(prev: str | None, closes: list[float], dec_dec: str | None, any_hike: bool, new_hike: bool, r: dict) -> str:
    c = closes[-1]
    below = len(closes) >= r["pos_days"] and all(x < r["pos_below"] for x in closes[-r["pos_days"]:])
    if c > r["neg_above"] or new_hike:
        return NEG
    if prev == NEG and c >= r["neg_exit_below"]:
        return NEG
    if prev == POS and c < r["pos_exit_at_or_above"] and not any_hike:
        return POS
    if below and dec_dec in ("hold", "cut") and not any_hike:
        return POS
    return NEU


def below_now(closes: list[float], r: dict) -> bool:
    return len(closes) >= r["pos_days"] and all(x < r["pos_below"] for x in closes[-r["pos_days"]:])


def advance(st: dict, series: list[tuple[str, float]], dec: dict, r: dict) -> tuple[dict, dict | None]:
    """새 데이터 날짜를 차례로 반영. 반환 (새 state, 이벤트 None | {kind: change|note, prev, zone, why})."""
    st = dict(st)
    seen = dict(st.get("fomc_seen") or {})
    new_hikes = [k for k, v in dec.items() if v == "hike" and seen.get(k) != "hike"]
    any_hike = any(v == "hike" for v in dec.values())
    dec_dec = dec.get("2026-12")
    last = st.get("last_date") or ""
    new_days = [i for i, (d, _) in enumerate(series) if d > last]
    if not new_days and not new_hikes:
        return st, None
    idxs = new_days or [len(series) - 1]
    prev0 = st.get("zone")
    zone = prev0
    for n, i in enumerate(idxs):
        closes = [c for _, c in series[: i + 1]]
        zone = step_zone(zone, closes, dec_dec, any_hike, bool(new_hikes) and n == len(idxs) - 1, r)
    closes = [c for _, c in series]
    st.update(zone=zone, last_date=series[-1][0], last_ten=series[-1][1])
    for k, v in dec.items():
        if v in ("hike", "hold", "cut"):
            seen[k] = v
    st["fomc_seen"] = seen
    if prev0 is None:  # install: 조용히 저장
        st["note_sent"] = bool(zone == NEU and below_now(closes, r) and dec_dec is None)
        return st, None
    if zone != prev0:
        c = closes[-1]
        if zone == NEG:
            why = (f"{'12월' if '2026-12' in new_hikes else '1월'} FOMC 인상") if new_hikes else "10년물 5.3% 초과"
        elif zone == POS:
            why = "10년물 4.5% 하회, 12월 동결"
        elif prev0 == NEG:
            why = "10년물 5.2% 아래 복귀"
        elif prev0 == POS and any_hike:
            why = "FOMC 인상"
        else:
            why = "10년물 4.6% 이상 복귀" if c >= r["pos_exit_at_or_above"] else "조건 해제"
        if zone != NEU or not below_now(closes, r):
            st["note_sent"] = st.get("note_sent", False) and below_now(closes, r)
        return st, {"kind": "change", "prev": prev0, "zone": zone, "why": why, "new_hikes": new_hikes}
    if zone == NEU and below_now(closes, r) and dec_dec is None and not st.get("note_sent"):
        st["note_sent"] = True
        return st, {"kind": "note", "prev": zone, "zone": zone, "why": PRE_NOTE, "new_hikes": []}
    if not below_now(closes, r):
        st["note_sent"] = False  # 다시 4.5% 위로 가면 다음 하회 때 안내 1번 더
    return st, None


# ---------- price bands (pure) ----------

def band_items(cfg: dict) -> list[dict]:
    lv = cfg["price_levels"]
    items = [dict(b, kind="band") for b in cfg.get("price_bands") or []]
    items.append({"key": "review", "kind": "review", "line": lv["review_below"]})
    items.append({"key": "nochase", "kind": "nochase", "line": lv["nochase_at"]})
    return items


def band_active(b: dict, today: str, dec_dec: str | None) -> bool:
    if b["kind"] != "band":
        return True
    if b.get("filled"):
        return False
    if b.get("active_until_kst") and today > b["active_until_kst"]:
        return False
    if b.get("active_from_kst") and today < b["active_from_kst"]:
        return False
    if b.get("needs_fomc") and dec_dec not in b["needs_fomc"]:
        return False
    return True


def inside(b: dict, c: float) -> bool:
    if b["kind"] == "review":
        return c < b["line"]
    if b["kind"] == "nochase":
        return c >= b["line"]
    return b["lo"] <= c <= b["hi"]


def far_out(b: dict, c: float, h: float) -> bool:
    k = h / 100
    if b["kind"] == "review":
        return c >= b["line"] * (1 + k)
    if b["kind"] == "nochase":
        return c < b["line"] * (1 - k)
    return c < b["lo"] * (1 - k) or c > b["hi"] * (1 + k)


def advance_bands(bst: dict | None, day: str, close: float, today: str, dec_dec: str | None, cfg: dict) -> tuple[dict, list[dict]]:
    """가격대 진입/이탈. bst None 이면 조용히 시작(설치). 같은 데이터 날짜는 건너뜀."""
    seed = bst is None
    bst = {"date": None, "in": {}} if seed else {"date": bst.get("date"), "in": dict(bst.get("in") or {})}
    if bst["date"] and day <= bst["date"]:
        return bst, []
    h = cfg["price_levels"]["hysteresis_pct"]
    events = []
    for b in band_items(cfg):
        was = bool(bst["in"].get(b["key"]))
        if not band_active(b, today, dec_dec):
            if was:
                bst["in"][b["key"]] = False  # 기간 끝, 체결 완료: 조용히 끔
            continue
        if not was and inside(b, close):
            bst["in"][b["key"]] = True
            if not seed:
                events.append({"kind": "band", "item": b, "dir": "in"})
        elif was and far_out(b, close, h):
            bst["in"][b["key"]] = False
            if not seed:
                events.append({"kind": "band", "item": b, "dir": "out"})
    bst["date"] = day
    return bst, events


def band_table(cfg: dict, today: str, dec_dec: str | None) -> str:
    parts = []
    for b in cfg["price_bands"]:
        if b.get("filled"):
            st = "체결 완료"
        elif band_active(dict(b, kind="band"), today, dec_dec):
            st = "유효"
        elif b.get("active_until_kst") and today > b["active_until_kst"]:
            st = "기간 끝"
        else:
            st = "대기"
        parts.append(f"{b['label']} {b['lo']:.2f}~{b['hi']:.2f} ({b['pct']:g}%, {b['shares']}주, {b['phase']}, {st})")
    lv = cfg["price_levels"]
    return "가격대: " + ", ".join(parts) + f". 재검토 종가 {lv['review_below']:.2f} 미만, 추격 금지 {lv['nochase_at']:.2f} 이상"


def band_message(cfg: dict, ev: dict, zone: str, xlu: tuple[float, str], today: str, dec_dec: str | None,
                 now: datetime) -> tuple[str, str, int]:
    b, d = ev["item"], ev["dir"]
    px, pday = xlu
    lv = cfg["price_levels"]
    neg = zone == NEG
    if b["kind"] == "band":
        rng = f"{b['lo']:.2f}~{b['hi']:.2f}"
        if d == "in":
            title, pri = f"[XLU] {b['label']} 분할 가격대 진입 ({rng}, 자산 {b['pct']:g}%)", 4
            what = f"종가 {px:.2f}달러가 {b['label']} 가격대 {rng} 안 ({b['phase']})"
            todo = ("분할 일시 중지. 판단이 부정이라 지정가를 넣지 말고 기다림." if neg else
                    f"{b['label']} 지정가 {rng}달러에 {b['shares']}주(약 {b['budget_krw'] // 10000}만원, 자산 {b['pct']:g}%)만 넣기.")
        else:
            title, pri = f"[XLU] {b['label']} 분할 가격대 이탈 ({rng})", 3
            what = f"종가 {px:.2f}달러가 {b['label']} 가격대 {rng} 밖으로 0.3% 넘게 벗어남"
            todo = f"{b['label']} 미체결 지정가는 그대로 두거나 취소, 따라가지 않기."
    elif b["kind"] == "review":
        if d == "in":
            title, pri = f"[XLU] 종가 {lv['review_below']:.2f} 미만 | 분할 중단, 판단 재검토", 4
            what = f"종가 {px:.2f}달러, 재검토선 {lv['review_below']:.2f} 아래 (52주 장중 저점 39.03 이탈)"
            todo = "분할 중단, 판단 재검토. 남은 지정가 취소 검토."
        else:
            title, pri = f"[XLU] 종가 {lv['review_below']:.2f} 위 회복 | 재검토선 해제", 3
            what = f"종가 {px:.2f}달러, 재검토선 위로 0.3% 넘게 회복"
            todo = "재검토 결론 전까지 분할 재개 보류."
    else:
        if d == "in":
            title, pri = f"[XLU] 종가 {lv['nochase_at']:.2f} 이상 | 추격 금지", 3
            what = f"종가 {px:.2f}달러, 추격 금지선 {lv['nochase_at']:.2f} 이상"
            todo = "추격 금지. 새로 담지 말고 가격대로 내려오기를 기다림."
        else:
            title, pri = f"[XLU] 종가 {lv['nochase_at']:.2f} 아래 | 추격 금지 해제", 3
            what = f"종가 {px:.2f}달러, 추격 금지선 아래로 0.3% 넘게 내려옴"
            todo = "추격 금지 해제. 가격대에 들어올 때까지 기다림."
    lines = [f"{now.strftime('%Y-%m-%d %H:%M')} KST | XLU | 유틸리티 ETF 분할 가격대",
             "이게 뭐 알림: 목표 비중 총자산 5%를 12월 FOMC 앞뒤로 나눠 담는 지정가 가격대 점검. 가격대 진입과 이탈 때만 보냄.",
             f"지금 결과/상태: {what}.",
             f"- XLU 종가 {px:.2f}달러 ({pday[5:].replace('-', '/')}), 52주 {cfg['baseline']['low_52w']:.2f}~{cfg['baseline']['high_52w']:.2f}",
             "- " + band_table(cfg, today, dec_dec),
             f"- 판단 {zone}" + (": 2차 보류, 분할 일시 중지" if neg else ""),
             "- " + cfg["position"]["tax_note"]]
    if b["kind"] == "band" and d == "in":
        lines.append(f"- 체결 뒤 configs/xlu.json price_bands 의 {b['key']} filled 를 true 로, position.shares 를 체결 수량으로 고치면 이 알림은 완료로 꺼집니다.")
    lines += [f"지금 할 일: {todo}", END_LINE, DISCLAIMER]
    return title, "\n".join(lines), pri


# ---------- message ----------

def fomc_line(cfg: dict, dec: dict, src: dict) -> str:
    base = "FOMC: 9/16 인상(3.75~4.00%), 10/28 동결."
    d = dec.get("2026-12")
    if d is None:
        return base + " 다음 12/8~9 (결과 12/10 04:00 KST), 결과 대기."
    if d == "unknown":
        return base + " 12월 결과 확인 불가 (성명서, FRED 모두 못 읽음)."
    line = base + f" 12월 {DEC_KO[d]} ({src.get('2026-12', '')})."
    j = dec.get("2027-01")
    if j in DEC_KO:
        line += f" 1월 {DEC_KO[j]} ({src.get('2027-01', '')})."
    elif j is None:
        line += " 다음 1/26~27 (결과 1/28 04:00 KST)."
    return line


TODO = {POS: "신규 편입 검토 가능. 목표 비중과 가격 구간을 먼저 정하고 분할로만.",
        NEU: "기다림. 새로 담지 말고 10년물과 12월 FOMC 결과 확인.",
        NEG: "신규 편입 보류. 10년물이 5.2% 아래로 돌아올 때까지 기다림."}


def message(cfg: dict, ev: dict, series: list[tuple[str, float]], src10: str, xlu: tuple[float | None, str | None],
            dec: dict, src: dict, now: datetime) -> tuple[str, str, int]:
    r = cfg["rules"]
    d10, ten = series[-1]
    if ev["kind"] == "change":
        title = f"[XLU] 판단 {ev['prev']} -> {ev['zone']} | {ev['why']}"
        pri = 4
    else:
        title = f"[XLU] 판단 중립 유지 | {PRE_NOTE}"
        pri = 3
    last3 = ", ".join(f"{d[5:].replace('-', '/')} {c:.2f}%" for d, c in series[-3:])
    px, pday = xlu
    lines = [f"{now.strftime('%Y-%m-%d %H:%M')} KST | XLU | 유틸리티 ETF 금리 판단",
             "이게 뭐 알림: 미 10년물과 FOMC로 XLU 판단(긍정, 중립, 부정)이 바뀔 때만 보내는 알림. 보유 없음, 가격 구간 없음.",
             f"지금 결과/상태: 판단 {ev['prev']} -> {ev['zone']}. {ev['why']}." if ev["kind"] == "change"
             else f"지금 결과/상태: 판단 중립 유지. {PRE_NOTE}.",
             (f"- XLU 종가 {px:.2f}달러 ({pday[5:].replace('-', '/')}), 52주 {cfg['baseline']['low_52w']:.2f}~{cfg['baseline']['high_52w']:.2f}"
              if px else "- XLU 종가: 확인 불가"),
             f"- 10년물 {ten:.2f}% ({d10[5:].replace('-', '/')}, {src10}), 4.5% 선 대비 {ten - r['pos_below']:+.2f}%p, 5.3% 선 대비 {ten - r['neg_above']:+.2f}%p. 최근 3일 {last3}"]
    if px:
        y = cfg["dividend_usd"] / px * 100
        lines.append(f"- 배당수익률 {y:.2f}% (연 {cfg['dividend_usd']:.2f}달러) - 10년물 {ten:.2f}% = {y - ten:+.2f}%p")
    else:
        lines.append("- 배당수익률 - 10년물: 확인 불가 (XLU 종가 없음)")
    lines.append("- " + fomc_line(cfg, dec, src))
    for k in ev.get("new_hikes") or []:
        m = next(x for x in cfg["fomc"]["meetings"] if x["key"] == k)
        lines.append(f"- 추가 인상 시사 여부는 성명서/회견 원문 확인: {m['statement']}")
    lines.append("- " + cfg["position"]["tax_note"])
    lines.append(f"지금 할 일: {TODO[ev['zone']]}")
    lines.append("판단 규칙: 긍정 = 10년물 4.5% 미만 3일 연속 + 12월 동결, 부정 = 10년물 5.3% 초과 또는 인상, 부정 해제 5.2% 미만, 긍정 해제 4.6% 이상 또는 인상.")
    lines += [END_LINE, DISCLAIMER]
    return title, "\n".join(lines), pri


def check_text(t: str, m: str) -> None:
    assert not BANNED_RE.search(t + m), "banned char"
    assert "XLU" in t and "매수" not in t and "신호" not in t, t


def ops_message(now: datetime, what: str, errs: list[str]) -> tuple[str, str]:
    t = f"[XLU] {what} | 판단 보류"
    m = "\n".join([f"{now.strftime('%Y-%m-%d %H:%M')} KST | XLU",
                   "이게 뭐 알림: XLU 판단에 쓰는 데이터를 못 읽음. 판단 바뀜 아님.",
                   "지금 결과/상태: " + "; ".join(errs)[:400],
                   "지금 할 일: 야후 ^TNX, FRED DGS10, 연준 성명서를 직접 확인.", END_LINE, DISCLAIMER])
    return t, m


# ---------- rotation: XLP / XLU / XLY (pure rules + isolated fetch) ----------

def _sma(vals: list[float], n: int) -> float | None:
    if n <= 0 or len(vals) < n:
        return None
    return sum(vals[-n:]) / n


def _ret(vals: list[float], n: int) -> float | None:
    if len(vals) < n + 1:
        return None
    base = vals[-1 - n]
    if base <= 0:
        return None
    return (vals[-1] / base - 1.0) * 100.0


def expected_us_day(now: datetime) -> str:
    """Last completed US regular session. Before 16:10 ET, the session is the prior weekday."""
    et = now.astimezone(ET)
    d = et.date()
    if (et.hour, et.minute) < (16, 10):
        d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d.isoformat()


def days_between(earlier: str, later: str) -> int:
    a = datetime.strptime(earlier, "%Y-%m-%d").date()
    b = datetime.strptime(later, "%Y-%m-%d").date()
    return (b - a).days


def _meta_close(meta: dict, day: str, ten: bool) -> float | None:
    """Use regularMarketPrice only when the candle close is missing and the session stamp is 16:00 ET or later."""
    rmt, px = meta.get("regularMarketTime"), meta.get("regularMarketPrice")
    if rmt is None or px is None:
        return None
    try:
        px = float(px)
    except (TypeError, ValueError):
        return None
    if ten and px > 20:
        px /= 10.0
    if not px > 0:
        return None
    stamp = datetime.fromtimestamp(int(rmt), timezone.utc).astimezone(ET)
    if stamp.strftime("%Y-%m-%d") != day or (stamp.hour, stamp.minute) < (16, 0):
        return None
    return px


def bars_from_chart(payload: dict, now: datetime, ten: bool = False) -> list[tuple[str, float]]:
    """Completed daily closes. Drop a null close and the in-progress ET session (before 16:10).

    If the newest candle close is empty but regularMarketTime is 16:00 ET or later that day,
    use regularMarketPrice. That is the value Yahoo writes into the candle once it fills
    (fulldayPrice can differ and is not used).
    """
    res = payload["chart"]["result"][0]
    quote = ((res.get("indicators") or {}).get("quote") or [{}])[0]
    meta = res.get("meta") or {}
    out: dict[str, float] = {}
    last_day = None
    for ts, close in zip(res.get("timestamp") or [], quote.get("close") or []):
        if ts is None:
            continue
        day = datetime.fromtimestamp(int(ts), timezone.utc).astimezone(ET).strftime("%Y-%m-%d")
        last_day = day
        if close is None:
            continue
        px = float(close)
        if ten and px > 20:
            px /= 10.0
        if not px > 0:
            continue
        out[day] = px
    if last_day and last_day not in out:
        filled = _meta_close(meta, last_day, ten)
        if filled is not None:
            out[last_day] = filled
    return [(d, out[d]) for d in sorted(out) if keep_session(d, now)]


def fetch_completed(sym: str, now: datetime, span: str, ten: bool = False) -> list[tuple[str, float]]:
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{quote(sym, safe='')}?interval=1d&range={span}"
    rows = bars_from_chart(json.loads(fetch(url)), now, ten=ten)
    if not rows:
        raise RuntimeError(f"{sym} 완료 일봉 없음")
    return rows


def align_prices(series: dict[str, list[tuple[str, float]]], min_bars: int) -> tuple[list[str], dict[str, list[float]]]:
    sets = [set(d for d, _ in rows) for rows in series.values()]
    days = sorted(set.intersection(*sets)) if sets else []
    if len(days) < min_bars:
        raise RuntimeError(f"공통 거래일 {len(days)}개")
    out = {}
    for sym, rows in series.items():
        mp = dict(rows)
        out[sym] = [mp[d] for d in days]
    return days, out


def cover_days(rows: list[tuple[str, float]] | None, days: list[str]) -> list[float] | None:
    """Prices on days, or None when the symbol is missing any of those sessions."""
    if not rows:
        return None
    mp = dict(rows)
    if any(d not in mp for d in days):
        return None
    return [mp[d] for d in days]


def rs_view(num: list[float], den: list[float], rc: dict) -> dict:
    fast, slow = int(rc["ma_fast"]), int(rc["ma_slow"])
    if len(num) < slow or len(num) != len(den):
        raise RuntimeError("상대강도 표본 부족")
    if any(b <= 0 for b in den):
        raise RuntimeError("비율 계산 불가")
    ratio = [a / b for a, b in zip(num, den)]
    ma20, ma60 = _sma(ratio, fast), _sma(ratio, slow)
    rn, rd = _ret(num, fast), _ret(den, fast)
    if ma20 is None or ma60 is None or rn is None or rd is None:
        raise RuntimeError("상대강도 계산 불가")
    excess = rn - rd
    last = ratio[-1]
    above = last > ma20 and last > ma60
    below = last < ma20 and last < ma60
    return {
        "enter": bool(above and excess >= float(rc["rs_enter_pct"])),
        "exit": bool(below and excess <= float(rc["rs_exit_pct"])),
        "ratio": round(last, 6),
        "ma20": round(ma20, 6),
        "ma60": round(ma60, 6),
        "excess": round(excess, 4),
    }


def lead_view(names: list[list[float]], base: list[float], rc: dict) -> dict:
    fast = int(rc["ma_fast"])
    details = []
    for px in names:
        ma, r, rb = _sma(px, fast), _ret(px, fast), _ret(base, fast)
        if ma is None or r is None or rb is None:
            raise RuntimeError("주도주 표본 부족")
        details.append({"above": px[-1] > ma, "excess": round(r - rb, 4)})
    enter = all(d["above"] and d["excess"] >= float(rc["leader_enter_pct"]) for d in details)
    exit_ = any((not d["above"]) or d["excess"] <= float(rc["leader_exit_pct"]) for d in details)
    return {
        "enter": bool(enter), "exit": bool(exit_),
        "nee_excess": details[0]["excess"], "ceg_excess": details[1]["excess"],
        "nee_above": details[0]["above"], "ceg_above": details[1]["above"],
    }


def stock_view(names: list[list[float]], rc: dict) -> dict:
    fast = int(rc["ma_fast"])
    details = []
    for px in names:
        ma, r = _sma(px, fast), _ret(px, fast)
        if ma is None or r is None:
            raise RuntimeError("개별주 표본 부족")
        details.append({"above": px[-1] > ma, "ret": round(r, 4)})
    enter = all(d["above"] and d["ret"] >= float(rc["stock_enter_pct"]) for d in details)
    exit_ = any((not d["above"]) or d["ret"] <= float(rc["stock_exit_pct"]) for d in details)
    return {
        "enter": bool(enter), "exit": bool(exit_),
        "amzn_ret": details[0]["ret"], "tsla_ret": details[1]["ret"],
        "amzn_above": details[0]["above"], "tsla_above": details[1]["above"],
    }


def rate_view(tens, zone, fomc_on: bool, rc: dict) -> dict:
    n = int(rc["rate_ma"])
    closes = [c for _, c in tens] if tens and isinstance(tens[0], (tuple, list)) else list(tens or [])
    if len(closes) < n + 1:
        raise RuntimeError("10년물 표본 부족")
    ma = _sma(closes, n)
    if ma is None:
        raise RuntimeError("10년물 평균 불가")
    drop = closes[-1 - n] - closes[-1]
    last = closes[-1]
    path = last <= ma and drop >= float(rc["rate_drop_enter"])
    path_off = last > ma or drop < float(rc["rate_drop_exit"])
    enter = zone == POS or bool(fomc_on) or path
    exit_ = zone != POS and not fomc_on and path_off
    return {
        "enter": bool(enter), "exit": bool(exit_), "path": bool(path),
        "last": round(float(last), 4), "ma": round(float(ma), 4), "drop": round(float(drop), 4),
        "zone": zone, "fomc_cut": bool(fomc_on),
    }


def monthly_mom(rows: list[tuple[str, float]]) -> list[dict]:
    out = []
    for i in range(1, len(rows)):
        prev, cur = rows[i - 1], rows[i]
        if prev[1] <= 0:
            continue
        out.append({"month": cur[0][:7], "mom": (cur[1] / prev[1] - 1.0) * 100.0, "date": cur[0], "level": cur[1]})
    return out


def retail_regime(real_rows: list[tuple[str, float]], nom_rows: list[tuple[str, float]], rules: dict) -> dict | None:
    real = {r["month"]: r for r in monthly_mom(real_rows)}
    nom = {r["month"]: r for r in monthly_mom(nom_rows)}
    common = sorted(set(real) & set(nom))
    if not common:
        return None
    month = common[-1]
    rm, nm = real[month]["mom"], nom[month]["mom"]
    if rm <= float(rules["slow_real_mom_max"]) or (rm <= 0 and nm <= 0):
        regime = "slowdown"
    elif rm >= float(rules["rebound_real_mom_min"]) and nm >= float(rules["rebound_nominal_mom_min"]):
        regime = "rebound"
    else:
        regime = "mixed"
    return {
        "month": month, "regime": regime,
        "real_mom": round(rm, 4), "nominal_mom": round(nm, 4),
        "real_date": real[month]["date"], "nominal_date": nom[month]["date"],
        "real": real[month]["level"], "nominal": nom[month]["level"],
    }


def xlp_leads_view(xlu: list[float], xlp: list[float], xly: list[float], rc: dict) -> bool:
    fast = int(rc["ma_fast"])
    ru, rp, ry = _ret(xlu, fast), _ret(xlp, fast), _ret(xly, fast)
    if None in (ru, rp, ry) or any(v <= 0 for v in xlu[-fast:] + xly[-fast:]):
        raise RuntimeError("XLP 비교 표본 부족")
    ratio_u = [a / b for a, b in zip(xlp, xlu)]
    ratio_y = [a / b for a, b in zip(xlp, xly)]
    mu, my = _sma(ratio_u, fast), _sma(ratio_y, fast)
    if mu is None or my is None:
        raise RuntimeError("XLP 비교 평균 불가")
    return bool(rp > ru and rp > ry and ratio_u[-1] >= mu and ratio_y[-1] >= my)


def cut_active(obj: dict | None) -> tuple[bool, str | None]:
    sig = (obj or {}).get("signal") or {}
    klass = sig.get("class")
    if sig.get("active") and klass in ("인하 사이클 진입", "인하 신호 강화"):
        return True, klass
    return False, None


def read_fomc_cut() -> tuple[bool, str | None]:
    path = ROOT / "state" / "fomccut.json"
    try:
        return cut_active(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, TypeError):
        return False, None


def fomc_phrase(flag: bool, klass: str | None) -> str:
    if not flag:
        return "FOMC 인하 사이클 표시 없음"
    if klass == "인하 사이클 진입":
        return "FOMC 인하 사이클 진입"
    return "FOMC 인하 사이클 강화"


def step_flag(prev: bool | None, enter: bool, exit_: bool) -> bool:
    if prev is None:
        return bool(enter)
    if enter and not prev:
        return True
    if exit_ and prev:
        return False
    return bool(prev)


def xlu_label(rs_on: bool, rate_on: bool, power_on: bool) -> str:
    if rs_on and rate_on and power_on:
        return "both"
    if rs_on and rate_on:
        return "rate"
    if rs_on and power_on:
        return "power"
    return "off"


def xly_label(rs_on: bool, stocks_on: bool, rate_on: bool, rebound: bool) -> str:
    if not (rs_on and stocks_on):
        return "off"
    if rate_on and rebound:
        return "full"
    return "price"


def _r2(v) -> float | None:
    return None if v is None else round(float(v), 2)


def advance_rotation(prev: dict | None, snap: dict, rc: dict) -> tuple[dict, list[dict]]:
    """State transition only. prev None seeds the current picture and returns no events."""
    seeded = prev is None
    prev = prev or {}
    rs_u = step_flag(None if seeded else bool(prev.get("xlu_rs")), snap["xlu_rs"]["enter"], snap["xlu_rs"]["exit"])
    rs_y = step_flag(None if seeded else bool(prev.get("xly_rs")), snap["xly_rs"]["enter"], snap["xly_rs"]["exit"])
    power = step_flag(None if seeded else bool(prev.get("power")), snap["power"]["enter"], snap["power"]["exit"])
    rate = step_flag(None if seeded else bool(prev.get("rate_ease")), snap["rate"]["enter"], snap["rate"]["exit"])
    stocks = step_flag(None if seeded else bool(prev.get("stocks")), snap["stocks"]["enter"], snap["stocks"]["exit"])
    retail = snap["retail"]
    rebound = bool(retail and retail["regime"] == "rebound")
    label_u = xlu_label(rs_u, rate, power)
    label_y = xly_label(rs_y, stocks, rate, rebound)
    events: list[dict] = []
    if seeded:
        slow_month = retail["month"] if retail else None
    else:
        slow_month = prev.get("xlp_slow_month")
        prev_u = prev.get("xlu_label") or "off"
        prev_y = prev.get("xly_label") or "off"
        if label_u != prev_u:
            events.append({"kind": "xlu", "prev": prev_u, "label": label_u, "priority": _xlu_priority(prev_u, label_u)})
        if label_y != prev_y:
            events.append({"kind": "xly", "prev": prev_y, "label": label_y, "priority": _xly_priority(prev_y, label_y)})
        if retail and retail["regime"] == "slowdown" and snap["xlp_leads"] and slow_month != retail["month"]:
            events.append({"kind": "xlp", "month": retail["month"], "priority": 4})
            slow_month = retail["month"]
    rot = {
        "asof": snap["asof"],
        "xlu_rs": rs_u, "xly_rs": rs_y, "power": power, "rate_ease": rate, "stocks": stocks,
        "xlu_label": label_u, "xly_label": label_y,
        "retail_month": retail["month"] if retail else None,
        "retail_regime": retail["regime"] if retail else None,
        "xlp_slow_month": slow_month,
        "px": {k: (None if v is None else round(float(v), 4)) for k, v in snap["px"].items()},
        "ret20": {k: _r2(v) for k, v in snap["ret20"].items()},
        "xlu_xlp": snap["xlu_rs"]["ratio"], "xly_xlp": snap["xly_rs"]["ratio"],
        "xlu_excess": snap["xlu_rs"]["excess"], "xly_excess": snap["xly_rs"]["excess"],
        "ten": snap["rate"]["last"], "ten_drop20": snap["rate"]["drop"],
        "real_mom": retail["real_mom"] if retail else None,
        "nominal_mom": retail["nominal_mom"] if retail else None,
        "ten_src": snap.get("ten_src"),
        "power_fresh": bool(snap["power"].get("fresh", True)),
        "stocks_fresh": bool(snap["stocks"].get("fresh", True)),
    }
    return rot, events


def _xlu_priority(prev: str, label: str) -> int:
    if label == "off":
        return 3
    if prev == "off" or label == "both":
        return 4
    return 3


def _xly_priority(prev: str, label: str) -> int:
    if label == "full" and prev != "full":
        return 4
    return 3


def _frozen_power() -> dict:
    return {"enter": False, "exit": False, "nee_excess": None, "ceg_excess": None,
            "nee_above": False, "ceg_above": False, "fresh": False}


def _frozen_stocks() -> dict:
    return {"enter": False, "exit": False, "amzn_ret": None, "tsla_ret": None,
            "amzn_above": False, "tsla_above": False, "fresh": False}


def make_snap(px: dict[str, list[float] | None], ten, ten_src: str, retail, zone, fomc_on: bool, fomc_class, rc: dict, asof: str) -> dict:
    fast = int(rc["ma_fast"])
    if px.get("NEE") is not None and px.get("CEG") is not None:
        power = lead_view([px["NEE"], px["CEG"]], px["XLU"], rc)
        power["fresh"] = True
    else:
        power = _frozen_power()
    if px.get("AMZN") is not None and px.get("TSLA") is not None:
        stocks = stock_view([px["AMZN"], px["TSLA"]], rc)
        stocks["fresh"] = True
    else:
        stocks = _frozen_stocks()
    closes, rets = {}, {}
    for sym, series in px.items():
        if not series:
            closes[sym] = None
            rets[sym] = None
            continue
        closes[sym] = round(float(series[-1]), 4)
        rets[sym] = round(float(_ret(series, fast)), 4)
    return {
        "asof": asof, "ten_src": ten_src, "fomc_class": fomc_class,
        "xlu_rs": rs_view(px["XLU"], px["XLP"], rc),
        "xly_rs": rs_view(px["XLY"], px["XLP"], rc),
        "power": power, "stocks": stocks,
        "rate": rate_view(ten, zone, fomc_on, rc),
        "retail": retail,
        "xlp_leads": xlp_leads_view(px["XLU"], px["XLP"], px["XLY"], rc),
        "px": closes, "ret20": rets,
    }


def build_snapshot(cfg: dict, series_10y, zone, now: datetime) -> dict:
    rc = cfg["rotation"]
    got: dict[str, list[tuple[str, float]]] = {}
    errs: list[str] = []
    for sym in rc["symbols"]:
        try:
            got[sym] = fetch_completed(sym, now, "2y")
        except Exception as exc:  # noqa: BLE001
            errs.append(f"{sym}: {exc}")
    ten, ten_src = None, ""
    try:
        ten = fetch_completed("^TNX", now, "1y", ten=True)
        ten_src = "Yahoo ^TNX"
    except Exception as exc:  # noqa: BLE001
        if series_10y and len(series_10y) >= int(rc["rate_ma"]) + 1:
            ten, ten_src = list(series_10y), "기존 10년물"
        else:
            errs.append(f"^TNX: {exc}")
    retail = None
    try:
        real = fred_series(rc["retail"]["real"], "2024-01-01")
        nom = fred_series(rc["retail"]["nominal"], "2024-01-01")
        retail = retail_regime(real, nom, rc["retail"])
        if retail is None:
            raise RuntimeError("공통 월 부족")
    except Exception as exc:  # noqa: BLE001
        errs.append(f"FRED 소매판매: {exc}")
    if errs or ten is None:
        raise RuntimeError("; ".join(errs)[:500] or "상대매력 데이터 없음")
    # XLU, XLP, XLY set the session. A missing latest print on NEE/CEG/AMZN/TSLA/SPY
    # does not fail the run and does not move that condition (enter and exit stay off).
    days, core = align_prices({s: got[s] for s in ("XLU", "XLP", "XLY")}, int(rc["min_bars"]))
    px = {s: core[s] for s in ("XLU", "XLP", "XLY")}
    for sym in ("SPY", "NEE", "CEG", "AMZN", "TSLA"):
        px[sym] = cover_days(got.get(sym), days)
    last = days[-1]
    expect = expected_us_day(now)
    gap = days_between(last, expect)
    if gap > int(rc["stale_days"]) or gap < 0:
        raise RuntimeError(f"완료 일봉 {last}, 기대 세션 {expect}")
    fomc_on, fomc_class = read_fomc_cut()
    return make_snap(px, ten, ten_src, retail, zone, fomc_on, fomc_class, rc, last)


XLU_TITLES = {
    ("off", "rate"): "[XLU] XLP XLY 상대 매력 상향 | XLU XLP 금리 확인",
    ("off", "power"): "[XLU] XLP XLY 상대 매력 상향 | XLU XLP NEE CEG 전력 확인",
    ("off", "both"): "[XLU] XLP XLY 상대 매력 상향 | XLU XLP NEE CEG 금리와 전력 확인",
    ("rate", "both"): "[XLU] XLP XLY 확인 보강 | XLU XLP NEE CEG 금리와 전력",
    ("power", "both"): "[XLU] XLP XLY 확인 보강 | XLU XLP NEE CEG 금리와 전력",
    ("both", "rate"): "[XLU] XLP XLY 확인 축소 | XLU XLP 금리만 유지",
    ("both", "power"): "[XLU] XLP XLY 확인 축소 | XLU XLP NEE CEG 전력만 유지",
    ("rate", "power"): "[XLU] XLP XLY 확인 전환 | XLU XLP NEE CEG 전력 확인",
    ("power", "rate"): "[XLU] XLP XLY 확인 전환 | XLU XLP 금리 확인",
    ("rate", "off"): "[XLU] XLP XLY 상대 매력 해제 | XLU XLP",
    ("power", "off"): "[XLU] XLP XLY 상대 매력 해제 | XLU XLP",
    ("both", "off"): "[XLU] XLP XLY 상대 매력 해제 | XLU XLP",
}
XLY_TITLES = {
    ("off", "price"): "[XLU] XLP XLY 가격 선행 | XLY XLP AMZN TSLA 매크로 미확인",
    ("off", "full"): "[XLU] XLP XLY 상대 매력 상향 | XLY XLP AMZN TSLA 매크로 확인",
    ("price", "full"): "[XLU] XLP XLY 상대 매력 상향 | XLY XLP AMZN TSLA 매크로 확인",
    ("full", "price"): "[XLU] XLP XLY 매크로 확인 해제 | XLY XLP AMZN TSLA",
    ("price", "off"): "[XLU] XLP XLY 상대 매력 해제 | XLY XLP AMZN TSLA",
    ("full", "off"): "[XLU] XLP XLY 상대 매력 해제 | XLY XLP AMZN TSLA",
}


def _fmt_pct(v) -> str:
    return "없음" if v is None else f"{v:+.2f}%"


def _fmt_pp(v) -> str:
    return "없음" if v is None else f"{v:+.2f}%p"


def _px_line(snap: dict) -> str:
    px, r20, day = snap["px"], snap["ret20"], snap["asof"][5:].replace("-", "/")
    return (f"종가 XLU {px['XLU']:.2f}, XLP {px['XLP']:.2f}, XLY {px['XLY']:.2f} ({day}). "
            f"20거래일 XLU {_fmt_pct(r20['XLU'])}, XLP {_fmt_pct(r20['XLP'])}, XLY {_fmt_pct(r20['XLY'])}, SPY {_fmt_pct(r20.get('SPY'))}.")


def _rate_line(snap: dict) -> str:
    rt = snap["rate"]
    bits = [f"10년물 {rt['last']:.2f}% (20일 {rt['ma']:.2f}%, 20거래일 변화 {-rt['drop']:+.2f}%p, {snap.get('ten_src') or '10년물'})."]
    if rt["zone"] == POS:
        bits.append("기존 판단 긍정.")
    elif rt["zone"]:
        bits.append(f"기존 판단 {rt['zone']}.")
    bits.append(fomc_phrase(rt["fomc_cut"], snap.get("fomc_class")) + ".")
    if rt["path"]:
        bits.append("10년물 경로 완화.")
    return " ".join(bits)


def rotation_message(cfg: dict, ev: dict, snap: dict, rot: dict, now: datetime) -> tuple[str, str, int]:
    kind, pri = ev["kind"], int(ev["priority"])
    head = f"{now.strftime('%Y-%m-%d %H:%M')} KST | XLU XLP XLY | 섹터 상대 매력"
    what = "이게 뭐 알림: XLP, XLU, XLY 상대 매력이 바뀔 때만 보내는 점검. 매일 반복하지 않음. 주문 지시가 아님."
    nums = _px_line(snap)
    rs_u, rs_y = snap["xlu_rs"], snap["xly_rs"]
    pw, stc = snap["power"], snap["stocks"]
    retail = snap["retail"] or {}
    if kind == "xlu":
        title = XLU_TITLES[(ev["prev"], ev["label"])]
        path = {"rate": "금리 확인", "power": "전력주 확인", "both": "금리와 전력주 확인", "off": "확인 해제"}[ev["label"]]
        if pw.get("fresh", True):
            power_txt = f"NEE 상대 {_fmt_pp(pw['nee_excess'])}, CEG 상대 {_fmt_pp(pw['ceg_excess'])}."
        else:
            power_txt = "NEE 또는 CEG 완료 종가 없음, 전력 상태는 바꾸지 않음."
        status = (f"XLU 상대 매력 {ev['prev']} -> {ev['label']} ({path}). "
                  f"XLU/XLP {rs_u['ratio']:.4f} (20일 {rs_u['ma20']:.4f}, 60일 {rs_u['ma60']:.4f}), 20거래일 상대 {rs_u['excess']:+.2f}%p. "
                  f"{power_txt} {_rate_line(snap)} {nums}")
        if ev["label"] == "off":
            todo = "XLU 를 XLP 보다 앞에 두지 않음. 기존 가격대와 긍정, 중립, 부정 판단을 그대로 봄."
        elif ev["label"] == "power":
            todo = "XLU 검토 순서를 올려 볼 자리. 다만 금리 완화는 아직 없고, NEE 와 CEG 주가 주도만 켜짐. 실적 반영은 이 알림이 확인하지 않음."
        elif ev["label"] == "rate":
            todo = "XLU 검토 순서를 XLP 보다 한 단계 올려 볼 자리. 금리 또는 인하 쪽 확인이 켜짐. NEE, CEG 주가 주도는 아직 없음."
        else:
            todo = "XLU 검토 순서를 XLP 보다 올려 볼 자리. 금리 쪽과 NEE, CEG 주가 주도 가 같이 켜짐. 실적 반영은 이 알림이 확인하지 않음."
        later = "XLU/XLP 가 20일선과 60일선 아래로 내려오고 20거래일 상대 성과가 0%p 이하이면 해제 1번. 금리만 꺼지거나 전력주만 꺼지면 축소 1번."
    elif kind == "xly":
        title = XLY_TITLES[(ev["prev"], ev["label"])]
        macro = "매크로 확인" if ev["label"] == "full" else ("가격만 선행, 매크로 미확인" if ev["label"] == "price" else "해제")
        if stc.get("fresh", True):
            stock_txt = f"AMZN 20거래일 {_fmt_pct(stc['amzn_ret'])}, TSLA 20거래일 {_fmt_pct(stc['tsla_ret'])}."
        else:
            stock_txt = "AMZN 또는 TSLA 완료 종가 없음, 개별주 상태는 바꾸지 않음."
        status = (f"XLY 상대 매력 {ev['prev']} -> {ev['label']} ({macro}). "
                  f"XLY/XLP {rs_y['ratio']:.4f} (20일 {rs_y['ma20']:.4f}, 60일 {rs_y['ma60']:.4f}), 20거래일 상대 {rs_y['excess']:+.2f}%p. "
                  f"{stock_txt} {_rate_line(snap)} "
                  f"소매 {retail.get('month') or '없음'} 실질 {retail.get('real_mom')}%, 명목 {retail.get('nominal_mom')}%, 판정 {retail.get('regime') or '없음'}. {nums}")
        if ev["label"] == "full":
            todo = "XLY 검토 순서를 올려 볼 자리. 가격과 AMZN, TSLA, 금리 완화, 소매 반등이 같이 켜짐."
        elif ev["label"] == "price":
            todo = "가격은 먼저 돌아섰으나 매크로 확인은 없음. XLY 순서를 올리지 말고 관망."
        else:
            todo = "XLY 상대 매력 상향은 해제. XLP 우위를 유지하고 XLY 는 후순위."
        later = "AMZN 또는 TSLA 가 20일선 아래이거나 20거래일 성과가 0% 이하, 또는 XLY/XLP 상대 강도가 꺼지면 해제. 소매 반등과 금리 완화가 나중에 켜지면 매크로 확인 1번."
    else:
        title = "[XLU] XLP XLY 소비 둔화 확인 | XLP XLU XLY 우위 유지"
        status = (f"FRED {retail.get('month')} 실질 소매판매 전월비 {retail.get('real_mom'):+.2f}% ({retail.get('real_date')}), "
                  f"명목 전월비 {retail.get('nominal_mom'):+.2f}% ({retail.get('nominal_date')}). 둔화 확인. "
                  f"XLP 20거래일 성과가 XLU, XLY 이상이고 XLP 비율이 20일선 위. {nums}")
        todo = "XLP 우위를 유지. XLU 나 XLY 순서를 이 알림으로 올리지 않음."
        later = "다음 소매판매 월이 새로 나오고 둔화가 이어지며 XLP 우위가 유지될 때만 다시 1번. 같은 월은 반복하지 않음."
    trim = f"이 알림에는 익절 가격이 없음. 기존 재검토 {cfg['price_levels']['review_below']:.2f}달러, 추격 금지 {cfg['price_levels']['nochase_at']:.2f}달러를 그대로 봄."
    avoid = "이 알림만 보고 비중을 한 번에 바꾸기. 장중 가격을 따라가기. 금리나 소매 숫자 없이 순서만 올리기."
    lines = [head, what, f"지금 결과/상태: {status}", f"지금 할 일: {todo}", f"나중에: {later}",
             f"익절/줄일 때: {trim}", f"하지 말 것: {avoid}", END_LINE, DISCLAIMER]
    return title, "\n".join(lines), pri


def rotation_ops_message(now: datetime, errs: list[str]) -> tuple[str, str]:
    t = "[XLU] XLP XLY 상대매력 조회 실패"
    m = "\n".join([
        f"{now.strftime('%Y-%m-%d %H:%M')} KST | XLU XLP XLY",
        "이게 뭐 알림: XLP, XLU, XLY 상대 매력에 쓰는 시세를 못 읽음. 기존 10년물 판단과 가격대는 그대로 진행.",
        "지금 결과/상태: " + "; ".join(errs)[:400],
        "지금 할 일: 야후 XLU, XLP, XLY 와 FRED RRSFS, RSAFS 를 직접 확인.",
        "나중에: 다음 실행에서 다시 시도. 하루 1번만 이 안내를 보냄.",
        "익절/줄일 때: 해당 없음.",
        "하지 말 것: 이 조회 실패를 상대 매력 전환으로 읽기.",
        END_LINE, DISCLAIMER,
    ])
    return t, m


def rotation_step(base: dict, cfg: dict, series, zone, now: datetime, fetch=None):
    """Returns (rotation dict or None, messages, ops pair or None). Never raises."""
    try:
        snap = fetch() if fetch is not None else build_snapshot(cfg, series, zone, now)
        prev = base.get("rotation") if isinstance(base.get("rotation"), dict) else None
        rot, events = advance_rotation(prev, snap, cfg["rotation"])
        msgs = []
        for ev in events:
            t, m, p = rotation_message(cfg, ev, snap, rot, now)
            check_text(t, m)
            msgs.append((t, m, p))
        return rot, msgs, None
    except Exception as exc:  # noqa: BLE001
        t, m = rotation_ops_message(now, [str(exc)[:300]])
        try:
            check_text(t, m)
        except Exception:  # noqa: BLE001
            t, m = rotation_ops_message(now, ["상대매력 계산 실패"])
        return None, [], (t, m)


def attach_rotation(container: dict, cfg: dict, series, zone, now: datetime):
    """Print the rotation result and store it on container. Does not publish or set the ops day."""
    missing = not isinstance(container.get("rotation"), dict)
    rot, msgs, ops = rotation_step(container, cfg, series, zone, now)
    if rot is not None:
        evs = ",".join(t for t, _, _ in msgs) or "-"
        tail = " silent seed (no alert)" if missing else ""
        print(f"rotation: asof={rot['asof']} XLU={rot['px'].get('XLU')} XLP={rot['px'].get('XLP')} XLY={rot['px'].get('XLY')} "
              f"xlu_label={rot['xlu_label']} xly_label={rot['xly_label']} power_fresh={rot.get('power_fresh')} "
              f"stocks_fresh={rot.get('stocks_fresh')} retail={rot['retail_regime']} {rot['retail_month']} "
              f"real_mom={rot['real_mom']} nom_mom={rot['nominal_mom']} events={evs}{tail}")
        container["rotation"] = rot
    else:
        print(f"rotation: fetch failed ({ops[0] if ops else 'unknown'})")
    for t, m, p in msgs:
        print(f"rotation priority={p}\n{t}\n{m}")
    if ops:
        print(f"rotation ops: {ops[0]}\n{ops[1]}")
    return msgs, ops


def mark_rotation_ops(container: dict, base: dict, ops, now: datetime) -> bool:
    if not ops or base.get("rotation_ops_day") == now.strftime("%Y-%m-%d"):
        return False
    container["rotation_ops_day"] = now.strftime("%Y-%m-%d")
    return True


def publish_rotation(cfg: dict, msgs, ops, send_ops: bool) -> None:
    server = resolve_server(cfg.get("ntfy_server"))
    topic = cfg["ntfy_topic"]
    if send_ops and ops:
        publish(route_topic(topic, ops[0]), ops[0], ops[1], 3, ["warning"], server, config_name="xlu")
    for t, m, p in msgs:
        publish(route_topic(topic, t), t, m, p, ["electric_plug"], server, config_name="xlu")


def _finish_rotation_early(st: dict, cfg: dict, series, zone, now: datetime) -> None:
    """10년물이 없어 본 판단이 여기서 끝나는 날. 상대 매력만 따로 처리하고 기존 조기 종료는 유지."""
    work = dict(st)
    msgs, ops = attach_rotation(work, cfg, series, zone, now)
    send = mark_rotation_ops(work, st, ops, now)
    if work != st:
        save_state(work, now)
    publish_rotation(cfg, msgs, ops, send)


def rotation_example(cfg: dict, now: datetime) -> tuple[str, str, int]:
    """Network-free sample of an XLU upgrade message. Not a live signal."""
    rc = cfg["rotation"]
    snap = {
        "asof": "2026-11-19", "ten_src": "Yahoo ^TNX", "fomc_class": None,
        "px": {"XLU": 43.01, "XLP": 89.86, "XLY": 115.88, "SPY": 715.87, "NEE": 83.79, "CEG": 252.03, "AMZN": 257.37, "TSLA": 374.41},
        "ret20": {"XLU": 3.2, "XLP": 0.4, "XLY": -1.1, "SPY": -2.0, "NEE": 6.0, "CEG": 5.5, "AMZN": 4.0, "TSLA": 4.2},
        "xlu_rs": {"enter": True, "exit": False, "ratio": 0.49, "ma20": 0.47, "ma60": 0.46, "excess": 2.8},
        "xly_rs": {"enter": False, "exit": True, "ratio": 1.2, "ma20": 1.3, "ma60": 1.25, "excess": -1.5},
        "power": {"enter": False, "exit": True, "nee_excess": -1.5, "ceg_excess": -2.0, "nee_above": False, "ceg_above": False, "fresh": True},
        "stocks": {"enter": False, "exit": True, "amzn_ret": 4.0, "tsla_ret": 4.2, "amzn_above": True, "tsla_above": True},
        "rate": {"enter": True, "exit": False, "path": True, "last": 4.40, "ma": 4.70, "drop": 0.30, "zone": NEU, "fomc_cut": False},
        "retail": {"month": "2026-10", "regime": "mixed", "real_mom": -0.14, "nominal_mom": 0.21, "real_date": "2026-10-01", "nominal_date": "2026-10-01"},
        "xlp_leads": False,
    }
    rot, events = advance_rotation({"xlu_rs": False, "xly_rs": False, "power": False, "rate_ease": False, "stocks": False,
                                    "xlu_label": "off", "xly_label": "off", "xlp_slow_month": "2026-10"}, snap, rc)
    ev = next(e for e in events if e["kind"] == "xlu")
    return rotation_message(cfg, ev, snap, rot, now)


# ---------- run ----------

def run(mode: str, now: datetime | None = None) -> int:
    cfg = load_cfg()
    now = now or datetime.now(KST)
    dry = mode == "dry"
    if now > ENDS_AT:
        print("xlu: ended (after 2027-02-26 23:59 KST); silent")
        return 0
    st = load_state()
    series, src10, errs = ten_series(now)
    dec, src, ferrs = fomc_decisions(cfg, now)
    today = now.strftime("%Y-%m-%d")
    ops = None
    if not series:
        ops = ops_message(now, "10년물 조회 실패", errs)
    elif dec.get("2026-12") == "unknown":
        ops = ops_message(now, "FOMC 결과 확인 불가", ferrs)
    if ops:
        check_text(*ops)
        print(f"ops: {ops[0]}\n{ops[1]}")
        if dry:
            attach_rotation(dict(st), cfg, series, st.get("zone"), now)
            print("DRY RUN: no publish, state not written")
            return 0
        if st.get("ops_day") != today:
            publish(route_topic("cwally-ops", ops[0]), ops[0], ops[1], 3, ["warning"], resolve_server(cfg.get("ntfy_server")), config_name="xlu")
            st["ops_day"] = today
            save_state(st, now)
        if not series:
            _finish_rotation_early(st, cfg, series, st.get("zone"), now)
            return 0
    try:
        xs = yahoo_series("XLU", now)
        xlu = (xs[-1][1], xs[-1][0]) if xs else (None, None)
    except Exception as exc:  # noqa: BLE001
        print(f"XLU quote failed: {exc}")
        xlu = (None, None)
    new, ev = advance(st, series, dec, cfg["rules"])
    band_out: list[tuple[str, str, int]] = []
    if xlu[0] is not None:
        bst, bevs = advance_bands(st.get("bands"), xlu[1], xlu[0], today, dec.get("2026-12"), cfg)
        new["bands"] = bst
        if st.get("bands") is None:
            print(f"bands: seed silently at {xlu[0]:.2f} ({xlu[1]}), in={bst['in']}")
        for bev in bevs:
            bt, bm, bp = band_message(cfg, bev, new.get("zone"), xlu, today, dec.get("2026-12"), now)
            check_text(bt, bm)
            print(f"band priority={bp}\n{bt}\n{bm}")
            band_out.append((bt, bm, bp))
    print(f"xlu: 10y {series[-1][1]:.3f}% ({series[-1][0]}, {src10}) xlu={xlu} fomc={dec} zone {st.get('zone')} -> {new.get('zone')} event={ev and ev['kind']}")
    if ev:
        t, m, pri = message(cfg, ev, series, src10, xlu, dec, src, now)
        check_text(t, m)
        print(f"priority={pri}\n{t}\n{m}")
    elif st.get("zone") is None:
        print("install: seed silently (no send)")
    rot_msgs, rot_ops = attach_rotation(new, cfg, series, new.get("zone"), now)
    if dry:
        print("DRY RUN: no publish, state not written")
        return 0
    send_rot_ops = mark_rotation_ops(new, st, rot_ops, now)
    if new == st:
        return 0
    if st.get("zone") is None:
        new["seeded_at"] = now.strftime("%Y-%m-%d %H:%M KST")
    save_state(new, now)
    if ev:
        publish(cfg["ntfy_topic"], t, m, pri, ["electric_plug"], resolve_server(cfg.get("ntfy_server")), config_name="xlu")
    for bt, bm, bp in band_out:
        publish(cfg["ntfy_topic"], bt, bm, bp, ["electric_plug"], resolve_server(cfg.get("ntfy_server")), config_name="xlu")
    publish_rotation(cfg, rot_msgs, rot_ops, send_rot_ops)
    return 0


def preview(now: datetime | None = None) -> None:
    """dry 에서 판단이 바뀌면 보낼 제목/본문 예시(지금 숫자로)."""
    cfg = load_cfg()
    now = now or datetime.now(KST)
    try:
        rt, rm, rp = rotation_example(cfg, now)
        check_text(rt, rm)
        print(f"--- rotation preview (example only, 샘플 숫자) priority={rp}\n{rt}\n{rm}")
    except Exception as exc:  # noqa: BLE001
        print(f"rotation preview failed: {exc}")
    series, src10, _ = ten_series(now)
    if not series:
        return
    dec, src, _ = fomc_decisions(cfg, now)
    try:
        xs = yahoo_series("XLU", now)
        xlu = (xs[-1][1], xs[-1][0])
    except Exception:  # noqa: BLE001
        xlu = (None, None)
    ev = {"kind": "change", "prev": NEU, "zone": NEG, "why": "10년물 5.3% 초과", "new_hikes": []}
    t, m, p = message(cfg, ev, series, src10, xlu, dec, src, now)
    check_text(t, m)
    print(f"--- preview (example only, 지금 숫자로 만든 변경 메시지 모양) priority={p}\n{t}\n{m}")
    b1 = dict(cfg["price_bands"][0], kind="band")
    bt, bm, bp = band_message(cfg, {"kind": "band", "item": b1, "dir": "in"}, NEU, (41.85, "2026-11-19"), now.strftime("%Y-%m-%d"), dec.get("2026-12"), now)
    check_text(bt, bm)
    print(f"--- band preview (example only, 1차 가격대에 41.85로 들어왔다고 가정) priority={bp}\n{bt}\n{bm}")


# ---------- selftest ----------

def selftest() -> int:
    cfg = load_cfg()
    r = cfg["rules"]
    n = 0

    def check(lab, got, want):
        nonlocal n
        assert got == want, f"{lab}: {got!r} != {want!r}"
        n += 1

    def ser(vals, start=1):
        return [(f"2026-11-{start + i:02d}", v) for i, v in enumerate(vals)]

    pend = {"2026-12": None, "2027-01": None}
    held = {"2026-12": "hold", "2027-01": None}
    hiked = {"2026-12": "hike", "2027-01": None}
    # install: 4.98 -> 중립, silent
    st, ev = advance({}, ser([5.05, 4.98, 4.93]), pend, r)
    check("install zone", (st["zone"], ev), (NEU, None))
    # dedupe: same data date -> nothing
    st2, ev = advance(st, ser([5.05, 4.98, 4.93]), pend, r)
    check("dedupe", (ev, st2 == st), (None, True))
    # 중립 -> 부정 above 5.30
    s0 = {"zone": NEU, "last_date": "2026-11-01"}
    st, ev = advance(s0, ser([5.2, 5.31]), pend, r)
    check("neu->neg", (st["zone"], ev["kind"], ev["why"]), (NEG, "change", "10년물 5.3% 초과"))
    # exactly 5.30 is not above
    st, ev = advance(s0, ser([5.2, 5.30]), pend, r)
    check("5.30 stays neu", (st["zone"], ev), (NEU, None))
    # hysteresis: 부정 stays at 5.25 and 5.20, leaves at 5.19
    sn = {"zone": NEG, "last_date": "2026-11-01"}
    check("neg 5.25", advance(sn, ser([5.4, 5.25]), pend, r)[0]["zone"], NEG)
    check("neg 5.20", advance(sn, ser([5.4, 5.20]), pend, r)[0]["zone"], NEG)
    st, ev = advance(sn, ser([5.4, 5.19]), pend, r)
    check("neg->neu 5.19", (st["zone"], ev["why"]), (NEU, "10년물 5.2% 아래 복귀"))
    # pre-FOMC: 3 days below 4.5 -> 중립 + note once
    st, ev = advance(s0, ser([4.49, 4.48, 4.40], 2), pend, r)
    check("pre note", (st["zone"], ev["kind"], ev["why"]), (NEU, "note", PRE_NOTE))
    st2, ev2 = advance(st, ser([4.49, 4.48, 4.40, 4.39], 2), pend, r)
    check("pre note once", ev2, None)
    # 2 days below only -> nothing
    check("2 days", advance(s0, ser([4.6, 4.48, 4.40], 2), pend, r)[1], None)
    # hold in December + 3 days below -> 긍정
    st, ev = advance(s0, ser([4.49, 4.48, 4.40], 2), held, r)
    check("neu->pos hold", (st["zone"], ev["kind"], ev["why"]), (POS, "change", "10년물 4.5% 하회, 12월 동결"))
    # cut counts as no hike
    check("cut ok", advance(s0, ser([4.49, 4.48, 4.40], 2), {"2026-12": "cut"}, r)[0]["zone"], POS)
    # hysteresis: 긍정 stays at 4.55, leaves at 4.60
    sp = {"zone": POS, "last_date": "2026-11-01", "fomc_seen": {"2026-12": "hold"}}
    check("pos 4.55", advance(sp, ser([4.4, 4.55]), held, r)[0]["zone"], POS)
    st, ev = advance(sp, ser([4.4, 4.60]), held, r)
    check("pos->neu 4.60", (st["zone"], ev["why"]), (NEU, "10년물 4.6% 이상 복귀"))
    # December hike -> 부정 with statement link line
    st, ev = advance({"zone": NEU, "last_date": "2026-12-08"}, [("2026-12-09", 4.9)], hiked, r)
    check("hike neg", (st["zone"], ev["why"], ev["new_hikes"]), (NEG, "12월 FOMC 인상", ["2026-12"]))
    t, m, p = message(cfg, ev, [("2026-12-08", 4.95), ("2026-12-09", 4.9)], "Yahoo ^TNX", (43.0, "2026-12-09"), hiked, {"2026-12": "성명서 원문"},
                      datetime(2026, 12, 10, 9, 0, tzinfo=KST))
    check_text(t, m)
    check("hike title", t, "[XLU] 판단 중립 -> 부정 | 12월 FOMC 인상")
    assert "추가 인상 시사 여부는 성명서/회견 원문 확인: https://www.federalreserve.gov/newsevents/pressreleases/monetary20261209a.htm" in m
    check("hike pri", p, 4)
    # hike already seen: next day 4.9 -> leaves 부정 (5.2 미만), cannot go 긍정
    st2, ev2 = advance(st, [("2026-12-09", 4.9), ("2026-12-10", 4.4), ("2026-12-11", 4.4), ("2026-12-14", 4.4)], hiked, r)
    check("after hike no pos", st2["zone"], NEU)
    # 긍정 then later (January) hike -> 부정
    sp2 = {"zone": POS, "last_date": "2027-01-27", "fomc_seen": {"2026-12": "hold"}}
    st, ev = advance(sp2, [("2027-01-27", 4.3)], {"2026-12": "hold", "2027-01": "hike"}, r)
    check("jan hike", (st["zone"], ev["why"]), (NEG, "1월 FOMC 인상"))
    # 부정 -> 긍정 cannot skip hysteresis (5.25 stays)
    check("neg no jump", advance(dict(sn, last_date="2026-11-02"), ser([4.4, 4.4, 5.25]), held, r)[0]["zone"], NEG)
    # missed runs are replayed day by day: a 4.4 day in between leaves 부정
    check("replay", advance(sn, ser([5.4, 4.4, 5.25]), held, r)[0]["zone"], NEU)
    # message shape on change and note
    for ev in ({"kind": "change", "prev": NEU, "zone": NEG, "why": "10년물 5.3% 초과", "new_hikes": []},
               {"kind": "change", "prev": NEU, "zone": POS, "why": "10년물 4.5% 하회, 12월 동결", "new_hikes": []},
               {"kind": "note", "prev": NEU, "zone": NEU, "why": PRE_NOTE, "new_hikes": []}):
        t, m, p = message(cfg, ev, ser([4.5, 4.4, 4.3]), "Yahoo ^TNX", (43.43, "2026-10-30"), pend, {}, datetime(2026, 11, 3, 9, tzinfo=KST))
        check_text(t, m)
        ls = m.splitlines()
        check("end lines", ls[-2:], [END_LINE, DISCLAIMER])
        check("pri", p, 4 if ev["kind"] == "change" else 3)
        check("topic", route_topic("cwally-port", t), "cwally-port")
        assert sum(1 for x in ls if x.startswith("지금 할 일: ")) == 1 and "배당수익률 2.79%" in m and "4.5% 선 대비" in m
    check("change title", message(cfg, {"kind": "change", "prev": NEU, "zone": POS, "why": "10년물 4.5% 하회, 12월 동결", "new_hikes": []},
                                  ser([4.4, 4.4, 4.4]), "x", (43.43, "2026-10-30"), held, {}, datetime(2026, 12, 15, 9, tzinfo=KST))[0],
          "[XLU] 판단 중립 -> 긍정 | 10년물 4.5% 하회, 12월 동결")
    t, m = ops_message(datetime(2026, 11, 3, 9, tzinfo=KST), "10년물 조회 실패", ["x"])
    check_text(t, m)
    check("ops topic", route_topic("cwally-port", t), "cwally-ops")
    # ---- price bands ----
    pre, post = "2026-11-20", "2026-12-15"
    # install seed: 43.43 is in no band, silent
    bs, bev = advance_bands(None, "2026-10-30", 43.43, "2026-11-02", None, cfg)
    check("band seed", (bev, any(bs["in"].values())), ([], False))
    # dedupe same data date
    check("band dedupe", advance_bands(bs, "2026-10-30", 41.8, pre, None, cfg)[1], [])
    # t1 entry before FOMC
    b1, bev = advance_bands(bs, "2026-11-19", 41.80, pre, None, cfg)
    check("t1 in", [(e["item"]["key"], e["dir"]) for e in bev], [("t1", "in")])
    # hysteresis: 42.10 (within 0.3% of 42.00) stays, 42.20 exits
    b2, bev = advance_bands(b1, "2026-11-20", 42.10, pre, None, cfg)
    check("t1 hyst stay", bev, [])
    b3, bev = advance_bands(b2, "2026-11-23", 42.20, pre, None, cfg)
    check("t1 out", [(e["item"]["key"], e["dir"]) for e in bev], [("t1", "out")])
    # t2/t3 not active before FOMC: 42.70 and 39.80 silent
    check("t2 wait", advance_bands(bs, "2026-11-19", 42.70, pre, None, cfg)[1], [])
    check("t3 wait", advance_bands(bs, "2026-11-19", 39.80, pre, None, cfg)[1], [])
    # t1 expires silently after 12/9 (while inside)
    b4, bev = advance_bands(b1, "2026-12-10", 41.85, "2026-12-10", "hold", cfg)
    check("t1 expiry silent", (bev, b4["in"]["t1"]), ([], False))
    # t2 after hold, not after hike
    check("t2 hold", [e["item"]["key"] for e in advance_bands(bs, "2026-12-14", 42.70, post, "hold", cfg)[1]], ["t2"])
    check("t2 cut", [e["item"]["key"] for e in advance_bands(bs, "2026-12-14", 42.70, post, "cut", cfg)[1]], ["t2"])
    check("t2 hike none", advance_bands(bs, "2026-12-14", 42.70, post, "hike", cfg)[1], [])
    check("t2 unknown none", advance_bands(bs, "2026-12-14", 42.70, post, "unknown", cfg)[1], [])
    # t3 after hike
    check("t3 hike", [e["item"]["key"] for e in advance_bands(bs, "2026-12-14", 39.60, post, "hike", cfg)[1]], ["t3"])
    # review below 39.00, exit needs >= 39.117
    r1, bev = advance_bands(bs, "2026-12-14", 38.90, post, "hike", cfg)
    check("review in", [(e["item"]["key"], e["dir"]) for e in bev], [("review", "in")])
    check("review hyst", advance_bands(r1, "2026-12-15", 39.10, post, "hike", cfg)[1], [])
    check("review out", [(e["item"]["key"], e["dir"]) for e in advance_bands(r1, "2026-12-15", 39.20, post, "hike", cfg)[1]], [("review", "out")])
    # nochase
    n1, bev = advance_bands(bs, "2026-11-19", 45.50, pre, None, cfg)
    check("nochase in", [(e["item"]["key"], e["dir"]) for e in bev], [("nochase", "in")])
    check("nochase hyst", advance_bands(n1, "2026-11-20", 45.40, pre, None, cfg)[1], [])
    check("nochase out", [e["dir"] for e in advance_bands(n1, "2026-11-20", 45.30, pre, None, cfg)[1]], ["out"])
    # filled flag turns a band off silently
    import copy
    cf = copy.deepcopy(cfg)
    cf["price_bands"][0]["filled"] = True
    check("filled none", advance_bands(bs, "2026-11-19", 41.80, pre, None, cf)[1], [])
    check("filled off", advance_bands(b1, "2026-11-20", 41.80, pre, None, cf)[0]["in"]["t1"], False)
    # messages
    ev_in = advance_bands(bs, "2026-11-19", 41.80, pre, None, cfg)[1][0]
    t, m, p = band_message(cfg, ev_in, NEU, (41.80, "2026-11-19"), pre, None, datetime(2026, 11, 20, 9, tzinfo=KST))
    check_text(t, m)
    check("band title", t, "[XLU] 1차 분할 가격대 진입 (41.70~42.00, 자산 2%)")
    check("band pri", p, 4)
    ls = m.splitlines()
    check("band end", ls[-2:], [END_LINE, DISCLAIMER])
    assert sum(1 for x in ls if x.startswith("지금 할 일: ")) == 1 and "71주" in m and "filled 를 true 로" in m and "원천징수 15%" in m and "22% 양도세" in m
    t, m, p = band_message(cfg, ev_in, NEG, (41.80, "2026-11-19"), pre, None, datetime(2026, 11, 20, 9, tzinfo=KST))
    check_text(t, m)
    assert "2차 보류, 분할 일시 중지" in m and "지금 할 일: 분할 일시 중지" in m
    for e in (advance_bands(b1, "2026-11-23", 42.20, pre, None, cfg)[1] + advance_bands(bs, "2026-12-14", 38.90, post, "hike", cfg)[1]
              + advance_bands(r1, "2026-12-15", 39.20, post, "hike", cfg)[1] + advance_bands(bs, "2026-11-19", 45.60, pre, None, cfg)[1]
              + advance_bands(n1, "2026-11-20", 45.30, pre, None, cfg)[1]):
        t, m, p = band_message(cfg, e, NEU, (40.0, "2026-12-14"), post, "hike", datetime(2026, 12, 15, 9, tzinfo=KST))
        check_text(t, m)
        check("topic band", route_topic("cwally-port", t), "cwally-port")
        assert m.splitlines()[-2:] == [END_LINE, DISCLAIMER]
    check("review title", band_message(cfg, advance_bands(bs, "2026-12-14", 38.90, post, "hike", cfg)[1][0], NEU, (38.9, "2026-12-14"), post, "hike",
                                       datetime(2026, 12, 15, 9, tzinfo=KST))[0], "[XLU] 종가 39.00 미만 | 분할 중단, 판단 재검토")
    # expiry: run returns before any network
    import contextlib
    import io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = run("auto", datetime(2027, 2, 27, 0, 0, tzinfo=KST))
    check("expiry", (rc, "ended" in buf.getvalue()), (0, True))
    check("not expired yet", datetime(2027, 2, 26, 23, 59, tzinfo=KST) > ENDS_AT, False)
    # fomc pending before known time (no network)
    check("pending", fomc_decisions(cfg, datetime(2026, 12, 10, 3, 59, tzinfo=KST))[0], {"2026-12": None, "2027-01": None})
    # ---- rotation (fixtures only) ----
    rc = cfg["rotation"]
    check("rot thresholds", (rc["rs_enter_pct"], rc["rs_exit_pct"], rc["leader_enter_pct"], rc["leader_exit_pct"],
                             rc["stock_enter_pct"], rc["stock_exit_pct"], rc["rate_drop_enter"], rc["rate_drop_exit"],
                             rc["retail"]["slow_real_mom_max"], rc["retail"]["rebound_real_mom_min"], rc["retail"]["rebound_nominal_mom_min"]),
          (2.0, 0.0, 1.0, -1.0, 3.0, 0.0, 0.25, 0.10, -0.20, 0.30, 0.20))
    assert not BANNED_RE.search(json.dumps(rc, ensure_ascii=False)), "rotation config banned char"
    check("step on", step_flag(False, True, False), True)
    check("step stay", step_flag(True, False, False), True)
    check("step off", step_flag(True, False, True), False)
    check("step seed", step_flag(None, True, False), True)
    # 2026-10 FRED print is mixed, not a slowdown and not a rebound
    oct_real = [("2026-09-01", 222016.0), ("2026-10-01", 221711.0)]
    oct_nom = [("2026-09-01", 746637.0), ("2026-10-01", 748220.0)]
    oct = retail_regime(oct_real, oct_nom, rc["retail"])
    check("oct regime", (oct["month"], oct["regime"]), ("2026-10", "mixed"))
    check("oct not slow", oct["real_mom"] <= rc["retail"]["slow_real_mom_max"], False)
    slow = retail_regime([("2026-10-01", 100.0), ("2026-11-01", 99.7)], [("2026-10-01", 100.0), ("2026-11-01", 100.5)], rc["retail"])
    check("real drop slow", slow["regime"], "slowdown")
    both_down = retail_regime([("2026-10-01", 100.0), ("2026-11-01", 99.9)], [("2026-10-01", 100.0), ("2026-11-01", 99.8)], rc["retail"])
    check("both nonpositive slow", both_down["regime"], "slowdown")
    reb = retail_regime([("2026-10-01", 100.0), ("2026-11-01", 100.4)], [("2026-10-01", 100.0), ("2026-11-01", 100.3)], rc["retail"])
    check("rebound", reb["regime"], "rebound")
    mixed = retail_regime([("2026-10-01", 100.0), ("2026-11-01", 99.95)], [("2026-10-01", 100.0), ("2026-11-01", 100.1)], rc["retail"])
    check("tiny dip mixed", mixed["regime"], "mixed")
    check("retail no overlap", retail_regime([("2026-10-01", 100.0), ("2026-11-01", 99.0)], [("2026-09-01", 100.0), ("2026-10-01", 99.0)], rc["retail"]), None)

    def flat(v, n=80):
        return [float(v)] * n

    def rise(start, n=80, steps=20, end=None):
        end = start if end is None else end
        head = [float(start)] * (n - steps)
        tail = [start + (end - start) * i / (steps - 1) for i in range(steps)]
        return head + tail

    xlp_flat = flat(100)
    check("rs enter", rs_view(rise(40, end=50), xlp_flat, rc)["enter"], True)
    check("rs exit", rs_view(rise(50, end=42), xlp_flat, rc)["exit"], True)
    check("rs flat not enter", rs_view(flat(40), xlp_flat, rc)["enter"], False)
    check("power enter", lead_view([rise(100, end=120), rise(100, end=120)], flat(40), rc)["enter"], True)
    check("power flat exit", lead_view([flat(100), flat(100)], flat(40), rc)["exit"], True)
    check("stocks enter", stock_view([rise(100, end=120), rise(100, end=120)], rc)["enter"], True)
    check("stocks flat exit", stock_view([flat(100), flat(100)], rc)["exit"], True)
    check("rate path on", rate_view([5.4] * 20 + [5.1], NEU, False, rc)["enter"], True)
    hyst = rate_view([5.2] * 20 + [5.05], NEU, False, rc)
    check("rate hyst band", (hyst["enter"], hyst["exit"]), (False, False))
    check("rate path off", rate_view([5.0] * 20 + [5.2], NEU, False, rc)["exit"], True)
    check("rate zone pos", rate_view([5.2] * 21, POS, False, rc)["enter"], True)
    check("rate fomc", rate_view([5.2] * 21, NEU, True, rc)["enter"], True)
    check("xlp leads", xlp_leads_view(flat(40), rise(80, end=100), flat(100), rc), True)
    check("xlp tie no", xlp_leads_view(flat(40), flat(80), flat(100), rc), False)
    check("cut on", cut_active({"signal": {"active": True, "class": "인하 사이클 진입"}}), (True, "인하 사이클 진입"))
    check("cut off", cut_active({"signal": {"active": False, "class": "인하 사이클 진입"}}), (False, None))
    check("cut missing", cut_active({}), (False, None))
    check("phrase no raw", "신호" in fomc_phrase(True, "인하 신호 강화"), False)

    def neutral_snap():
        return {
            "asof": "2026-11-19", "ten_src": "Yahoo ^TNX", "fomc_class": None,
            "px": {"XLU": 43.0, "XLP": 90.0, "XLY": 116.0, "SPY": 716.0, "NEE": 84.0, "CEG": 252.0, "AMZN": 257.0, "TSLA": 374.0},
            "ret20": {"XLU": 0.0, "XLP": 0.0, "XLY": 0.0, "SPY": 0.0, "NEE": 0.0, "CEG": 0.0, "AMZN": 0.0, "TSLA": 0.0},
            "xlu_rs": {"enter": False, "exit": True, "ratio": 0.47, "ma20": 0.48, "ma60": 0.49, "excess": -1.0},
            "xly_rs": {"enter": False, "exit": True, "ratio": 1.2, "ma20": 1.3, "ma60": 1.25, "excess": -1.0},
            "power": {"enter": False, "exit": True, "nee_excess": -2.0, "ceg_excess": -3.0, "nee_above": False, "ceg_above": False},
            "stocks": {"enter": False, "exit": True, "amzn_ret": -1.0, "tsla_ret": -2.0, "amzn_above": False, "tsla_above": False},
            "rate": {"enter": False, "exit": True, "path": False, "last": 5.05, "ma": 5.0, "drop": -0.05, "zone": NEU, "fomc_cut": False},
            "retail": {"month": "2026-10", "regime": "mixed", "real_mom": -0.14, "nominal_mom": 0.21, "real_date": "2026-10-01", "nominal_date": "2026-10-01"},
            "xlp_leads": False,
        }

    gap = neutral_snap()
    gap["px"] = {**gap["px"], "CEG": None}
    gap["ret20"] = {**gap["ret20"], "CEG": None}
    gap_rot, gap_ev = advance_rotation(None, gap, rc)
    check("none close seed", (gap_ev, gap_rot["px"]["CEG"]), ([], None))
    seeded, evs = advance_rotation(None, neutral_snap(), rc)
    check("seed silent", (evs, seeded["xlu_label"], seeded["xly_label"], seeded["xlp_slow_month"]), ([], "off", "off", "2026-10"))
    hot = neutral_snap()
    hot["xlu_rs"] = {**hot["xlu_rs"], "enter": True, "exit": False, "excess": 3.0}
    hot["rate"] = {**hot["rate"], "enter": True, "exit": False, "path": True, "drop": 0.30}
    hot["power"] = {**hot["power"], "enter": True, "exit": False}
    hot["xly_rs"] = {**hot["xly_rs"], "enter": True, "exit": False, "excess": 3.0}
    hot["stocks"] = {**hot["stocks"], "enter": True, "exit": False, "amzn_ret": 4.0, "tsla_ret": 4.0, "amzn_above": True, "tsla_above": True}
    hot["retail"] = {**hot["retail"], "month": "2026-11", "regime": "slowdown", "real_mom": -0.4, "nominal_mom": 0.1}
    hot["xlp_leads"] = True
    seeded_hot, evs = advance_rotation(None, hot, rc)
    check("seed hot silent", (evs, seeded_hot["xlu_label"], seeded_hot["xly_label"], seeded_hot["xlp_slow_month"]), ([], "both", "price", "2026-11"))
    again, evs = advance_rotation(seeded, neutral_snap(), rc)
    check("seed next quiet", evs, [])
    # rate enter/exit both false so the rate flag sticks, rs sticks, label stays, no event.
    stick = neutral_snap()
    stick["xlu_rs"] = {**stick["xlu_rs"], "enter": False, "exit": False, "excess": 1.0}
    stick["rate"] = {**stick["rate"], "enter": False, "exit": False, "drop": 0.15}
    base_on = {"xlu_rs": True, "xly_rs": False, "power": False, "rate_ease": True, "stocks": False,
               "xlu_label": "rate", "xly_label": "off", "xlp_slow_month": "2026-10"}
    stuck, evs = advance_rotation(base_on, stick, rc)
    check("hyst no event", (stuck["xlu_label"], stuck["xlu_rs"], stuck["rate_ease"], evs), ("rate", True, True, []))
    up, evs = advance_rotation(seeded, hot, rc)
    check("xlu both alert", ("xlu", "both", 4) in [(e["kind"], e.get("label"), e["priority"]) for e in evs], True)
    check("xly price alert", ("xly", "price", 3) in [(e["kind"], e.get("label"), e["priority"]) for e in evs], True)
    check("xlp once setup", ("xlp", None, 4) in [(e["kind"], e.get("label"), e["priority"]) for e in evs], True)
    check("xlp month marked", up["xlp_slow_month"], "2026-11")
    again, evs = advance_rotation(up, hot, rc)
    check("no repeat", evs, [])
    # XLP slowdown suppressed when XLP is not leading, then fires once when it leads
    pend = dict(seeded)
    pend_snap = neutral_snap()
    pend_snap["retail"] = {**pend_snap["retail"], "month": "2026-11", "regime": "slowdown", "real_mom": -0.4}
    pend_snap["xlp_leads"] = False
    held, evs = advance_rotation(pend, pend_snap, rc)
    check("xlp waits", (evs, held["xlp_slow_month"]), ([], "2026-10"))
    pend_snap["xlp_leads"] = True
    fired, evs = advance_rotation(held, pend_snap, rc)
    check("xlp fires later", ([e["kind"] for e in evs], fired["xlp_slow_month"]), (["xlp"], "2026-11"))
    quiet, evs = advance_rotation(fired, pend_snap, rc)
    check("xlp month dedupe", evs, [])
    # macro confirmation upgrades price -> full
    full_snap = neutral_snap()
    full_snap["xly_rs"] = {**full_snap["xly_rs"], "enter": True, "exit": False}
    full_snap["stocks"] = {**full_snap["stocks"], "enter": True, "exit": False}
    full_snap["rate"] = {**full_snap["rate"], "enter": True, "exit": False, "path": True}
    full_snap["retail"] = {**full_snap["retail"], "regime": "rebound", "real_mom": 0.4, "nominal_mom": 0.3}
    price_state = {"xlu_rs": False, "xly_rs": True, "power": False, "rate_ease": False, "stocks": True,
                   "xlu_label": "off", "xly_label": "price", "xlp_slow_month": "2026-10"}
    full, evs = advance_rotation(price_state, full_snap, rc)
    check("xly full", (full["xly_label"], [(e["kind"], e["label"], e["priority"]) for e in evs if e["kind"] == "xly"]), ("full", [("xly", "full", 4)]))
    # clear XLU
    off_snap = neutral_snap()
    cleared, evs = advance_rotation(up, off_snap, rc)
    check("xlu clear", (cleared["xlu_label"], any(e["kind"] == "xlu" and e["label"] == "off" for e in evs)), ("off", True))
    # backward compatible state: no rotation key, existing judgement untouched
    disk = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    disk.pop("rotation", None)
    disk.pop("rotation_ops_day", None)
    st_disk, ev_disk = advance(disk, [(disk["last_date"], disk["last_ten"])], {"2026-12": None, "2027-01": None}, r)
    check("disk compat", (st_disk["zone"], st_disk["last_date"], "rotation" in st_disk), (disk["zone"], disk["last_date"], False))
    merged = dict(disk)
    merged["rotation"] = seeded
    check("merge keeps zone", (merged["zone"], merged["last_date"], merged["bands"]), (disk["zone"], disk["last_date"], disk["bands"]))
    # completed bar: null close dropped, in-progress session dropped, after 16:10 kept
    def bar_payload(pairs):
        ts, cs = [], []
        for day, close in pairs:
            dt = datetime(int(day[:4]), int(day[5:7]), int(day[8:10]), 14, 30, tzinfo=timezone.utc)
            ts.append(int(dt.timestamp()))
            cs.append(close)
        return {"chart": {"result": [{"timestamp": ts, "indicators": {"quote": [{"close": cs}]}}]}}
    before = datetime(2026, 11, 19, 15, 0, tzinfo=ET)
    after = datetime(2026, 11, 19, 16, 30, tzinfo=ET)
    payload = bar_payload([("2026-11-18", 43.55), ("2026-11-19", None)])
    check("null close dropped", bars_from_chart(payload, after), [("2026-11-18", 43.55)])
    closed = dict(payload)
    closed["chart"]["result"][0]["meta"] = {
        "regularMarketPrice": 252.03,
        "regularMarketTime": int(datetime(2026, 11, 19, 21, 0, tzinfo=timezone.utc).timestamp()),
    }
    check("meta fills 16:00", bars_from_chart(closed, after)[-1], ("2026-11-19", 252.03))
    early_meta = dict(payload)
    early_meta["chart"]["result"][0]["meta"] = {
        "regularMarketPrice": 252.03,
        "regularMarketTime": int(datetime(2026, 11, 19, 20, 0, tzinfo=timezone.utc).timestamp()),
    }
    check("meta ignores 15:00", bars_from_chart(early_meta, after), [("2026-11-18", 43.55)])
    filled = bar_payload([("2026-11-18", 43.55), ("2026-11-19", 43.01)])
    check("intraday excluded", bars_from_chart(filled, before), [("2026-11-18", 43.55)])
    check("after close kept", bars_from_chart(filled, after)[-1], ("2026-11-19", 43.01))
    # 10Y and price-band session filter (2026-11-21). 2026-11-19 is Thursday, 11-20 Friday, 11-21 Saturday.
    thu_before = datetime(2026, 11, 19, 16, 9, tzinfo=ET)
    thu_at = datetime(2026, 11, 19, 16, 10, tzinfo=ET)
    check("10y before 16:10", keep_session("2026-11-19", thu_before), False)
    check("10y at 16:10", keep_session("2026-11-19", thu_at), True)
    check("10y prior day", keep_session("2026-11-18", thu_before), True)
    check("chart before 16:10", bars_from_chart(filled, thu_before), [("2026-11-18", 43.55)])
    check("chart at 16:10", bars_from_chart(filled, thu_at)[-1], ("2026-11-19", 43.01))
    sat = datetime(2026, 11, 21, 16, 30, tzinfo=ET)
    sun = datetime(2026, 11, 22, 18, 0, tzinfo=ET)
    check("saturday bar dropped", keep_session("2026-11-21", sat), False)
    check("friday kept on saturday", keep_session("2026-11-20", sat), True)
    check("saturday dropped on sunday", keep_session("2026-11-21", sun), False)
    mon_am = datetime(2026, 11, 23, 10, 0, tzinfo=ET)
    holiday = [("2026-11-19", 4.9), ("2026-11-20", 5.0)]
    check("holiday monday keeps friday", [d for d, _ in holiday if keep_session(d, mon_am)][-1], "2026-11-20")
    fred_rows = holiday + [("2026-11-21", 5.2), ("2026-11-23", 5.3)]
    check("fred dating morning", [d for d, _ in fred_rows if keep_session(d, mon_am)], ["2026-11-19", "2026-11-20"])
    check("fred dating after close", [d for d, _ in fred_rows if keep_session(d, datetime(2026, 11, 23, 16, 10, tzinfo=ET))],
          ["2026-11-19", "2026-11-20", "2026-11-23"])
    # already stored date is not sent again; the next session is judged once
    seen = {"zone": NEU, "last_date": "2026-11-18", "fomc_seen": {}}
    nxt = [("2026-11-18", 5.06), ("2026-11-19", 5.31)]
    st_new, ev_new = advance(seen, nxt, pend, r)
    check("next date once", (st_new["last_date"], ev_new["kind"], ev_new["why"]), ("2026-11-19", "change", "10년물 5.3% 초과"))
    check("same date silent", advance(st_new, nxt, pend, r)[1], None)
    check("stored date not resent", advance({"zone": NEU, "last_date": "2026-11-19", "fomc_seen": {}}, nxt, pend, r)[1], None)
    band_seen = {"date": "2026-11-18", "in": {}}
    check("band stored date", advance_bands(band_seen, "2026-11-18", 41.80, "2026-11-20", None, cfg)[1], [])
    band_new, band_ev = advance_bands(band_seen, "2026-11-19", 41.80, "2026-11-20", None, cfg)
    check("band next date", [e["item"]["key"] for e in band_ev], ["t1"])
    check("band next date once", advance_bands(band_new, "2026-11-19", 41.80, "2026-11-20", None, cfg)[1], [])
    morning = datetime(2026, 11, 20, 9, 6, tzinfo=KST)
    check("morning expects 11-19", expected_us_day(morning), "2026-11-19")
    # align drops a symbol that has not printed the newest day
    days, aligned = align_prices({"XLU": [("2026-11-18", 1.0), ("2026-11-19", 1.1)], "XLP": [("2026-11-18", 2.0)]}, 1)
    check("align lag", (days, aligned["XLU"]), (["2026-11-18"], [1.0]))
    check("cover miss", cover_days([("2026-11-18", 1.0)], ["2026-11-18", "2026-11-19"]), None)
    check("cover hit", cover_days([("2026-11-18", 1.0), ("2026-11-19", 1.1)], ["2026-11-18", "2026-11-19"]), [1.0, 1.1])
    stale_snap = neutral_snap()
    stale_snap["xlu_rs"] = {**stale_snap["xlu_rs"], "enter": False, "exit": False}
    stale_snap["power"] = {"enter": False, "exit": False, "nee_excess": None, "ceg_excess": None, "nee_above": False, "ceg_above": False, "fresh": False}
    stale_prev = {"xlu_rs": True, "xly_rs": False, "power": True, "rate_ease": False, "stocks": False,
                  "xlu_label": "power", "xly_label": "off", "xlp_slow_month": "2026-10"}
    stale_rot, stale_ev = advance_rotation(stale_prev, stale_snap, rc)
    check("stale power sticks", (stale_rot["power"], stale_rot["xlu_label"], stale_ev), (True, "power", []))
    stale_t, stale_m, _ = rotation_message(cfg, {"kind": "xlu", "prev": "off", "label": "power", "priority": 4}, stale_snap, stale_rot,
                                          datetime(2026, 11, 20, 9, 6, tzinfo=KST))
    check_text(stale_t, stale_m)
    check("stale body", "전력 상태는 바꾸지 않음" in stale_m, True)
    # fetch failure is non-blocking: base state unchanged, ops title, no port send
    base_state = {"zone": NEU, "last_date": "2026-11-18", "last_ten": 5.066, "bands": {"date": "2026-11-18", "in": {}}}
    snapshot = json.dumps(base_state)

    def boom():
        raise RuntimeError("Yahoo XLP: timed out")

    rot_fail, msgs_fail, ops_fail = rotation_step(base_state, cfg, [], NEU, morning, fetch=boom)
    check("fail isolated", (rot_fail, msgs_fail, json.dumps(base_state) == snapshot, ops_fail is not None), (None, [], True, True))
    check_text(*ops_fail)
    check("fail topic", route_topic(cfg["ntfy_topic"], ops_fail[0]), "cwally-ops")
    check("fail title", ops_fail[0], "[XLU] XLP XLY 상대매력 조회 실패")
    # wiring: missing rotation key seeds and does not alert, even if the picture is already hot
    rot_seed, msgs_seed, ops_seed = rotation_step({"zone": NEU}, cfg, [], NEU, morning, fetch=lambda: hot)
    check("wire seed silent", (msgs_seed, ops_seed, rot_seed["xlu_label"], rot_seed["xlp_slow_month"]), ([], None, "both", "2026-11"))
    # an existing rotation block does alert on the transition
    held_state = {"zone": NEU, "rotation": seeded}
    rot_ok, msgs_ok, ops_ok = rotation_step(held_state, cfg, [], NEU, morning, fetch=lambda: hot)
    check("wire no ops", (ops_ok, held_state["rotation"]["xlu_label"]), (None, "off"))
    check("wire count", len(msgs_ok), 3)
    from title_ticker_check import has_ticker
    bodies = []
    for t, m, p in msgs_ok:
        check_text(t, m)
        bodies.append(t + "\n" + m)
        check("ticker fn", has_ticker(t), True)
        check("names xlp xly", ("XLP" in t and "XLY" in t), True)
        check("topic port", route_topic("cwally-port", t), "cwally-port")
        ls = m.splitlines()
        check("six items", [x.split(":")[0] for x in ls if x.startswith(("이게 뭐", "지금 결과", "지금 할 일", "나중에", "익절", "하지 말 것"))],
              ["이게 뭐 알림", "지금 결과/상태", "지금 할 일", "나중에", "익절/줄일 때", "하지 말 것"])
        check("ends", ls[-2:], [END_LINE, DISCLAIMER])
    assert any("금리와 전력" in t for t, _, _ in msgs_ok)
    assert any("매크로 미확인" in t for t, _, _ in msgs_ok)
    assert any("소비 둔화" in t for t, _, _ in msgs_ok)
    ex_t, ex_m, ex_p = rotation_example(cfg, morning)
    check_text(ex_t, ex_m)
    check("example title", ex_t, "[XLU] XLP XLY 상대 매력 상향 | XLU XLP 금리 확인")
    check("example pri", ex_p, 4)
    check("example ticker", has_ticker(ex_t), True)
    assert not BANNED_RE.search(ex_t + ex_m + "".join(bodies))
    # labels
    check("label both", xlu_label(True, True, True), "both")
    check("label rs only", xlu_label(True, False, False), "off")
    check("label price", xly_label(True, True, False, False), "price")
    check("label full", xly_label(True, True, True, True), "full")
    check("label no stock", xly_label(True, False, True, True), "off")
    print(f"SELFTEST OK: {n} checks (fixtures only, no network, no publish, no state)")
    return 0


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    if mode == "selftest":
        return selftest()
    if mode not in ("auto", "dry"):
        print(f"unknown mode {mode}")
        return 2
    rc = run(mode)
    if mode == "dry":
        preview()
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
