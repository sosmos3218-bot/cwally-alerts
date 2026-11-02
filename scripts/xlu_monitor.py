#!/usr/bin/env python3
"""[XLU] SPDR 유틸리티 ETF 판단(긍정 / 중립 / 부정). 평일 09:00 KST morning 묶음 (xbi, hwm 옆).

2026-11-02 채팅 승인. 보유 없음, 가격 매수 구간 없음(configs/xlu.json price_bands 는 나중에 채울 자리).
판단은 미 10년물 전일 종가(Yahoo ^TNX, 실패 시 FRED DGS10)와 FOMC 결과로만 정한다:
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
판단이 바뀔 때만 p4. 안내와 조회 실패는 p3 (실패는 cwally-ops, 하루 1번). 10년물 데이터 날짜로 중복 방지.
첫 실행은 오늘 판단을 조용히 저장. 2027-02-26 23:59 KST 이후 침묵.
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


def yahoo_series(sym: str, ten: bool = False) -> list[tuple[str, float]]:
    raw = json.loads(fetch(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?interval=1d&range=1mo"))
    r = raw["chart"]["result"][0]
    out = {}
    for ts, c in zip(r.get("timestamp") or [], (r["indicators"]["quote"][0].get("close") or [])):
        if c is None:
            continue
        c = float(c)
        if ten and c > 20:  # ^TNX x10 on some feeds (same guard as ratesbell_monitor)
            c /= 10
        out[datetime.fromtimestamp(int(ts), timezone.utc).astimezone(ET).strftime("%Y-%m-%d")] = c
    return sorted(out.items())


def fred_series(sid: str, start: str) -> list[tuple[str, float]]:
    txt = fetch(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}&cosd={start}")
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
    errs = []
    today_et = now.astimezone(ET).strftime("%Y-%m-%d")
    try:
        s = [x for x in yahoo_series("%5ETNX", ten=True) if x[0] < today_et]  # 미국 전일 종가까지만
        if len(s) >= 3:
            return s, "Yahoo ^TNX", errs
        errs.append("Yahoo ^TNX 3일 미만")
    except Exception as exc:  # noqa: BLE001
        errs.append(f"Yahoo ^TNX: {exc}")
    try:
        s = fred_series("DGS10", (now - timedelta(days=40)).strftime("%Y-%m-%d"))
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
            print("DRY RUN: no publish, state not written")
            return 0
        if st.get("ops_day") != today:
            publish(route_topic("cwally-ops", ops[0]), ops[0], ops[1], 3, ["warning"], resolve_server(cfg.get("ntfy_server")), config_name="xlu")
            st["ops_day"] = today
            save_state(st, now)
        if not series:
            return 0
    try:
        xs = yahoo_series("XLU")
        today_et = now.astimezone(ET).strftime("%Y-%m-%d")
        xs = [x for x in xs if x[0] < today_et]
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
    if dry:
        print("DRY RUN: no publish, state not written")
        return 0
    if new == st:
        return 0
    if st.get("zone") is None:
        new["seeded_at"] = now.strftime("%Y-%m-%d %H:%M KST")
    save_state(new, now)
    if ev:
        publish(cfg["ntfy_topic"], t, m, pri, ["electric_plug"], resolve_server(cfg.get("ntfy_server")), config_name="xlu")
    for bt, bm, bp in band_out:
        publish(cfg["ntfy_topic"], bt, bm, bp, ["electric_plug"], resolve_server(cfg.get("ntfy_server")), config_name="xlu")
    return 0


def preview(now: datetime | None = None) -> None:
    """dry 에서 판단이 바뀌면 보낼 제목/본문 예시(지금 숫자로)."""
    cfg = load_cfg()
    now = now or datetime.now(KST)
    series, src10, _ = ten_series(now)
    if not series:
        return
    dec, src, _ = fomc_decisions(cfg, now)
    try:
        xs = [x for x in yahoo_series("XLU") if x[0] < now.astimezone(ET).strftime("%Y-%m-%d")]
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
