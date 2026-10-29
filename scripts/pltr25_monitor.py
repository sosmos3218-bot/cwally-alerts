#!/usr/bin/env python3
"""[PLTR25] RISE 팔란티어고정테크100(0047R0) 매수 판단 감시. 평일 09:00 KST morning 묶음. 보유 없음.

두 가지를 본다.
1) 지수 안 팔란티어 비중. KEDI 방법론: 5 미국 영업일 연속 30% 초과면 T 종가 뒤 재산정, T+1 종가에 25%로 수행.
   매월 마지막 미국 영업일 D 확정, D+1 종가 수행(정기). 비중은 매 실행마다 Yahoo 일봉으로 다시 계산한다
   (빠진 실행, 미국 휴장일에 강함). 1순위: FunETF 공시 비중(미국 전일 종가 반영)을 기준점으로 PLTR/QQQ 상대 수익률로 굴림.
   2순위: 마지막 리셋 25%에서 순수 추정. 리셋 완료 확인: FunETF 25.5% 이하 또는 네이버 PLTR 보유 주식 수 8% 이상 감소.
2) 1년 수익률 차이 0047R0 - 133690 (네이버 일별 종가, 가격 기준). 구간은 2일 연속 확인, 이력 전체를 매번 다시 재생.
   매수 검토 >= +3.0 (해제 < +1.0), 보류 <= -2.0 (해제 >= 0), 그 밖 관망.

알림은 바뀔 때만(state 서명), 주간 점검은 ISO 주 첫 실행 1번. 조회 실패는 cwally-ops 하루 1번.
modes: auto | dry (ntfy 없음, state 안 씀) | selftest (가짜 값, 네트워크 없음)
"""
from __future__ import annotations

import bisect
import json
import re
import sys
import traceback
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
WEB_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
BANNED = ("\u00b7", "\u2014")
FAIL_TITLE = "[PLTR25] 데이터 조회 실패 | 매매 신호 아님"
UNCONF_TITLE = "[PLTR25] 리셋 확인 불가 | 매매 신호 아님"
Z_BUY, Z_WAIT, Z_HOLD = "매수 검토", "관망", "보류"

DEFAULT_STATE = {
    "updated_at": None,
    "last_us_date": None,
    "last_reset": None,
    "anchor": None,
    "weight_model": None,
    "weight_funetf": None,
    "streak30": 0,
    "pre_armed": True,
    "trigger_sent_for": None,
    "reset_pending": None,
    "pltr_shares_cu": None,
    "gap": None,
    "last_weekly": None,
    "fetch_fail_date": None,
    "unconfirmed_sent_for": None,
}


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers=WEB_UA)
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read()
    for enc in ("utf-8", "euc-kr"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="ignore")


# ---------------- parsers ----------------
def parse_yahoo(text: str, now_ts: float | None = None) -> list[tuple[date, float]]:
    res = json.loads(text)["chart"]["result"][0]
    ts = res.get("timestamp") or []
    closes = res["indicators"]["quote"][0]["close"]
    reg = ((res.get("meta") or {}).get("currentTradingPeriod") or {}).get("regular") or {}
    out: list[tuple[date, float]] = []
    for t, c in zip(ts, closes):
        if c is None:
            continue
        out.append((datetime.fromtimestamp(t, ET).date(), float(c)))
    # 장중 막대(아직 종가 아님)는 버린다
    if out and reg.get("start") and reg.get("end") and ts:
        now_ts = now_ts if now_ts is not None else datetime.now(timezone.utc).timestamp()
        if ts[-1] >= reg["start"] and now_ts < reg["end"]:
            out = out[:-1]
    dedup: dict[date, float] = {}
    for d, c in out:
        dedup[d] = c
    return sorted(dedup.items())


def parse_funetf(html: str) -> dict | None:
    m = re.search(r'"value":"PLTR","additionalProperty":\[(.*?)\]\}', html, re.S)
    if not m:
        return None
    blk = m.group(1)
    w = re.search(r'etf:holding\.weight","value":([0-9.]+)', blk)
    s = re.search(r'etf:holding\.shares","value":([0-9.]+)', blk)
    d = re.search(r"(\d{4})\.(\d{2})\.(\d{2}) 기준", html)
    if not w or not d:
        return None
    return {
        "kr_date": f"{d.group(1)}-{d.group(2)}-{d.group(3)}",
        "weight": float(w.group(1)),
        "shares": float(s.group(1)) if s else None,
    }


def parse_naver_analysis(text: str) -> dict:
    d = json.loads(text)
    shares = None
    for it in d.get("etfTop10MajorConstituentAssets") or []:
        if "PALANTIR" in (it.get("itemName") or "").upper():
            try:
                shares = float(str(it.get("stockCount")).replace(",", ""))
            except ValueError:
                shares = None
    nav_y1 = next((x.get("value") for x in d.get("navPerformanceList") or [] if x.get("periodTypeCode") == "Y1"), None)
    px_y1 = next((x.get("value") for x in d.get("returnPerformanceList") or [] if x.get("periodTypeCode") == "Y1"), None)
    return {"shares": shares, "nav_y1": nav_y1, "px_y1": px_y1, "ref": d.get("navPerformanceReferenceDate")}


def parse_fchart(text: str, today: date | None = None, now_kst: datetime | None = None) -> dict[date, float]:
    out: dict[date, float] = {}
    for m in re.finditer(r'data="(\d{8})\|[^|]*\|[^|]*\|[^|]*\|([0-9.]+)\|', text):
        out[datetime.strptime(m.group(1), "%Y%m%d").date()] = float(m.group(2))
    # 오늘 장 마감(15:40 KST) 전이면 오늘 막대는 종가가 아니다
    if today and now_kst and (now_kst.hour, now_kst.minute) < (15, 40):
        out.pop(today, None)
    return out


# ---------------- weight model ----------------
def first_us_day_index(days: list[date]) -> list[int]:
    return [i for i in range(1, len(days)) if days[i].month != days[i - 1].month]


def replay_weights(pltr: list[tuple[date, float]], qqq: list[tuple[date, float]], cfg: dict,
                   funetf_anchor: tuple[date, float] | None = None) -> dict:
    """마지막 정기 리셋부터 매일 지수 비중을 다시 계산한다. 반환: days/weights/streak/events/last_reset/src."""
    m = cfg["method"]
    tgt = m["target_weight"] / 100.0
    trig = m["trigger_pct"] / 100.0
    k = m.get("pltr_in_qqq", 0.02)
    P = dict(pltr)
    Q = dict(qqq)
    days = sorted(set(P) & set(Q))
    if len(days) < 3:
        raise ValueError("bars too short")
    starts = first_us_day_index(days)
    si = starts[-1] if starts else 0
    seg = days[si:]
    anchor_d, anchor_w, kind = seg[0], tgt, "monthly"
    last_reset = {"date": seg[0].isoformat(), "kind": "monthly"}
    weights: list[float] = []
    streak = 0
    events: list[dict] = []
    reset_next = False
    src = "drift"
    for d in seg:
        if reset_next:
            anchor_d, anchor_w = d, tgt
            last_reset = {"date": d.isoformat(), "kind": "adhoc"}
            reset_next = False
            streak = 0
        rp = P[d] / P[anchor_d]
        rr = (Q[d] / Q[anchor_d] - k * rp) / (1 - k)
        w = anchor_w * rp / (anchor_w * rp + (1 - anchor_w) * rr)
        if funetf_anchor and funetf_anchor[0] == d and d >= anchor_d:
            anchor_d, anchor_w, w = d, funetf_anchor[1] / 100.0, funetf_anchor[1] / 100.0
            src = "funetf"
        if d == anchor_d and last_reset["date"] == d.isoformat():
            w = anchor_w
        weights.append(w)
        streak = streak + 1 if w > trig else 0
        if streak == m["trigger_days"]:
            events.append({"T": d.isoformat()})
            reset_next = True
    if src == "funetf" and anchor_d.isoformat() < last_reset["date"]:
        src = "drift"
    return {"days": seg, "weights": weights, "streak": streak, "events": events,
            "last_reset": last_reset, "src": src, "pending_exec": reset_next}


def need_rel(w: float, trig: float = 0.30) -> float:
    return ((trig / (1 - trig)) / (w / (1 - w)) - 1) * 100


def one_year(bars: list[tuple[date, float]]) -> float | None:
    if not bars:
        return None
    days = [d for d, _ in bars]
    t, c = bars[-1]
    i = bisect.bisect_right(days, t - timedelta(days=365)) - 1
    if i < 0:
        return None
    return (c / bars[i][1] - 1) * 100


# ---------------- gap ----------------
def gap_series(a: dict[date, float], b: dict[date, float]) -> list[tuple[date, float]]:
    ds = sorted(set(a) & set(b))
    if not ds:
        return []
    out = []
    for d in ds:
        base_t = d - timedelta(days=365)
        if base_t < ds[0]:
            continue
        i = bisect.bisect_right(ds, base_t) - 1
        bd = ds[i]
        out.append((d, ((a[d] / a[bd]) - (b[d] / b[bd])) * 100))
    return out


def replay_zone(series: list[tuple[date, float]], lv: dict) -> str:
    z = Z_WAIT
    n = lv["gap_confirm_days"]
    c_buy = c_hold = c_lt1 = c_ge0 = 0
    start = date.fromisoformat(lv["gap_replay_start"])
    for d, g in series:
        if d < start:
            continue
        c_buy = c_buy + 1 if g >= lv["gap_buy"] else 0
        c_hold = c_hold + 1 if g <= lv["gap_hold"] else 0
        c_lt1 = c_lt1 + 1 if g < lv["gap_buy_exit"] else 0
        c_ge0 = c_ge0 + 1 if g >= lv["gap_hold_exit"] else 0
        if z == Z_WAIT:
            if c_buy >= n:
                z = Z_BUY
            elif c_hold >= n:
                z = Z_HOLD
        elif z == Z_BUY:
            if c_hold >= n:
                z = Z_HOLD
            elif c_lt1 >= n:
                z = Z_WAIT
        elif z == Z_HOLD:
            if c_buy >= n:
                z = Z_BUY
            elif c_ge0 >= n:
                z = Z_WAIT
    return z


# ---------------- copy ----------------
def body(stamp: str, status: str, action: str, later: str, trim: str, avoid: str) -> str:
    return "\n".join([
        f"{stamp} | 0047R0 매수 판단",
        "이게 뭐 알림: RISE 팔란티어고정테크100(0047R0) 매수 검토용. 지수 안 팔란티어 비중(30% 5일 연속이면 25%로 줄임)과 0047R0 대 TIGER 미국나스닥100(133690) 1년 수익률 차이를 봅니다. 보유 없음. 매수 지시가 아닙니다.",
        f"지금 결과/상태: {status}",
        f"지금 할 일: {action}",
        f"나중에: {later}",
        f"익절/줄일 때: {trim}",
        f"하지 말 것: {avoid}",
    ])


def pct(x: float | None, d: int = 1, sign: bool = False) -> str:
    if x is None:
        return "확인 안 됨"
    return f"{x:+.{d}f}" if sign else f"{x:.{d}f}"


def weight_line(ctx: dict) -> str:
    w = ctx["w"] * 100
    fe = ctx.get("funetf")
    fe_txt = f"펀드 공시 {fe['weight']:.2f}% (미국 {fe['us_date']} 종가)" if fe else "펀드 공시 조회 실패, 순수 추정"
    line = f"추정 비중 {w:.2f}% (미국 {ctx['us_date']} 종가, {'공시 기준점' if ctx['src'] == 'funetf' else '순수 추정'}), {fe_txt}. 30% 초과 {ctx['streak']}일째."
    lv = ctx["lv"]
    if lv["band_lo"] <= w <= lv["band_hi"]:
        line += " 추정 오차 ±0.2%p 안쪽이라 30% 초과 여부가 바뀔 수 있습니다."
    return line


def gap_line(ctx: dict) -> str:
    g = ctx.get("gap")
    if not g:
        return "1년 차이: 한국 종가 조회 실패."
    s = f"1년 차이 {g['value']:+.2f}%p (0047R0 {g['ret_a']:+.2f}%, 133690 {g['ret_b']:+.2f}%, 한국 {g['krx_date']} 종가), 구간 {g['zone']}."
    if g.get("nav_value") is not None:
        s += f" 순자산 기준 {g['nav_value']:+.2f}%p (네이버 {g.get('nav_ref')})."
    return s


def zone_action(z: str) -> str:
    if z == Z_BUY:
        return "팔란티어 비중 효과가 1년 기준으로 확인됐습니다. 매수 검토, 나눠서."
    if z == Z_HOLD:
        return "팔란티어 비중이 1년 기준으로 돈이 안 됐습니다. 매수 보류."
    return "효과가 뚜렷하지 않습니다. 관망."


# ---------------- evaluate ----------------
def evaluate(cfg: dict, state: dict, data: dict, now: datetime) -> list[tuple[str, str, str, int]]:
    """state 를 고치고 (topic_kind, title, body, priority) 목록을 돌려준다."""
    lv = cfg["levels"]
    trig = cfg["method"]["trigger_pct"]
    today = now.date()
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    alerts: list[tuple[str, str, str, int]] = []
    errors: list[str] = list(data.get("errors") or [])
    ctx: dict = {"lv": lv}

    # --- weight ---
    pltr, qqq = data.get("pltr"), data.get("qqq")
    rw = None
    if pltr and qqq:
        fe = data.get("funetf")
        fe_anchor = None
        if fe:
            kr = date.fromisoformat(fe["kr_date"])
            prior = [d for d, _ in pltr if d < kr]
            if prior:
                fe = dict(fe, us_date=prior[-1].isoformat())
                fe_anchor = (prior[-1], fe["weight"])
            else:
                fe = None
        try:
            rw = replay_weights(pltr, qqq, cfg, fe_anchor)
        except Exception as exc:
            errors.append(f"비중 계산: {exc}")
        if rw:
            ctx.update(w=rw["weights"][-1], us_date=rw["days"][-1].isoformat(), streak=rw["streak"],
                       src=rw["src"], funetf=fe, last_reset=rw["last_reset"])
    else:
        errors.append("Yahoo 일봉(PLTR, QQQ) 조회 실패")

    # --- gap ---
    ga, gb = data.get("closes_a"), data.get("closes_b")
    if ga and gb:
        ser = gap_series(ga, gb)
        if ser:
            ds = sorted(set(ga) & set(gb))
            d_last = ser[-1][0]
            i = bisect.bisect_right(ds, d_last - timedelta(days=365)) - 1
            bd = ds[i]
            zone = replay_zone(ser, lv)
            nav = data.get("nav") or {}
            nav_val = None
            if nav.get("a") is not None and nav.get("b") is not None:
                nav_val = float(nav["a"]) - float(nav["b"])
            recent = [g for d, g in ser if d >= d_last - timedelta(days=28)]
            ctx["gap"] = {"krx_date": d_last.isoformat(), "value": round(ser[-1][1], 2),
                          "ret_a": round((ga[d_last] / ga[bd] - 1) * 100, 2), "ret_b": round((gb[d_last] / gb[bd] - 1) * 100, 2),
                          "zone": zone, "nav_value": round(nav_val, 2) if nav_val is not None else None,
                          "nav_ref": nav.get("ref"), "range4w": [round(min(recent), 2), round(max(recent), 2)]}
        else:
            errors.append("1년 차이 계산 불가(이력 부족)")
    else:
        errors.append("네이버 일별 종가(0047R0, 133690) 조회 실패")

    # --- fetch failure (ops, 하루 1번) ---
    if errors and state.get("fetch_fail_date") != today.isoformat():
        alerts.append(("ops", FAIL_TITLE, body(
            stamp,
            "조회 실패: " + "; ".join(errors) + ". 30% 초과 일수와 1년 차이 구간은 이번에 바꾸지 않았습니다.",
            "이 알림으로 사거나 미루지 마세요. 필요하면 FunETF와 네이버에서 직접 보세요.",
            "다음 평일 09:00 KST에 다시 계산합니다. 일수는 일봉으로 다시 세므로 빠진 날이 있어도 맞춰집니다.",
            "보유 없음, 해당 없음.",
            "조회 실패를 리셋이나 구간 변경으로 읽기.",
        ), 3))
        state["fetch_fail_date"] = today.isoformat()

    # --- weight alerts ---
    if rw:
        w = ctx["w"] * 100
        streak = rw["streak"]
        state["weight_model"] = round(w, 2)
        state["streak30"] = streak
        state["last_us_date"] = ctx["us_date"]
        if ctx.get("funetf"):
            state["weight_funetf"] = {"us_date": ctx["funetf"]["us_date"], "kr_date": ctx["funetf"]["kr_date"], "value": ctx["funetf"]["weight"]}
        prev_reset = state.get("last_reset")
        state["last_reset"] = rw["last_reset"]
        if rw["src"] == "funetf" and ctx.get("funetf"):
            state["anchor"] = {"us_date": ctx["funetf"]["us_date"], "weight": ctx["funetf"]["weight"], "src": "funetf"}
        else:
            state["anchor"] = {"us_date": rw["last_reset"]["date"], "weight": cfg["method"]["target_weight"], "src": "reset"}
        if prev_reset and prev_reset.get("date") != rw["last_reset"]["date"]:
            state["pre_armed"] = True  # 새 리셋 = 다시 무장
        wl = weight_line(ctx)
        gl = gap_line(ctx)
        # trigger (p4)
        for ev in rw["events"]:
            if state.get("trigger_sent_for") == ev["T"]:
                continue
            late = ev["T"] != ctx["us_date"]
            title = ("[PLTR25] 30% 5일 연속 | 리셋 실행일 지남, 확인 중" if late
                     else "[PLTR25] 30% 5일 연속 | 오늘 밤 25%로 리셋 예정, 매수 관망")
            alerts.append(("port", title, body(
                stamp,
                f"미국 {ev['T']} 종가로 팔란티어 비중이 5영업일 연속 30%를 넘었습니다(추정). {wl} {gl}",
                "매수 관망. 오늘 사면 지금은 팔란티어 약 30%, 리셋 뒤 25%를 들게 됩니다." if not late else "매수 관망. 리셋 완료 알림을 기다리세요.",
                "다음 미국 영업일 종가에 25%로 줄어듭니다. 리셋 완료 알림 뒤 1년 차이로 다시 판단하세요.",
                "보유 없음, 해당 없음.",
                "리셋 직전 비중이 높다는 이유만으로 추격 매수.",
            ), 4))
            state["trigger_sent_for"] = ev["T"]
            state["reset_pending"] = {"T": ev["T"], "shares_ref": state.get("pltr_shares_cu"), "runs": 0}
            state["pre_armed"] = False
        # pre-alert (p3) with hysteresis
        pre_cond = w >= lv["pre_pct"] or streak >= lv["pre_streak_days"]
        if not rw["events"] or all(state.get("trigger_sent_for") == e["T"] for e in rw["events"]):
            if state.get("pre_armed", True) and pre_cond and streak < cfg["method"]["trigger_days"]:
                left = cfg["method"]["trigger_days"] - streak
                alerts.append(("port", f"[PLTR25] 팔란티어 비중 {w:.1f}% | 리셋 근접, 매수 관망", body(
                    stamp,
                    f"{wl} {gl}",
                    f"매수 관망. 30% 초과가 {left}일 더 이어지면 25%로 줄어듭니다." if streak else "매수 관망. 30%를 5일 연속 넘으면 25%로 줄어듭니다.",
                    "리셋 완료 알림이나 28% 아래로 내려온 뒤 1년 수익률 차이로 다시 판단하세요.",
                    "보유 없음, 해당 없음.",
                    "비중이 높다는 이유만으로 추격 매수.",
                ), 3))
                state["pre_armed"] = False
            elif not state.get("pre_armed", True) and w < lv["pre_rearm_pct"] and streak == 0:
                state["pre_armed"] = True
        # reset confirmation (p3)
        rp = state.get("reset_pending")
        if rp:
            fe = ctx.get("funetf")
            nshares = data.get("naver_shares")
            ok_fe = bool(fe and fe["us_date"] > rp["T"] and fe["weight"] <= lv["reset_confirm_funetf_pct"])
            ok_nv = bool(nshares and rp.get("shares_ref") and nshares <= rp["shares_ref"] * (1 - lv["reset_confirm_share_drop"]))
            if ok_fe or ok_nv:
                how = (f"펀드 공시 {fe['weight']:.2f}%" if ok_fe else "") + (" , " if ok_fe and ok_nv else "") + \
                      (f"네이버 PLTR 주식 수 {rp['shares_ref']:g}에서 {nshares:g}" if ok_nv else "")
                g = ctx.get("gap")
                gtxt = f"1년 차이 {g['value']:+.1f}%p, {g['zone']}" if g else "1년 차이 확인 안 됨"
                alerts.append(("port", f"[PLTR25] 리셋 완료, 팔란티어 25% | {gtxt}", body(
                    stamp,
                    f"수시 리밸런싱 확인({how.strip(' ,')}). 미국 {rp['T']} 판정분. {wl} {gl}",
                    zone_action(g["zone"]) if g else "1년 차이를 확인한 뒤 판단하세요.",
                    "다음 리셋은 매월 첫 미국 영업일 종가 또는 다시 30% 5일 연속일 때입니다.",
                    "보유 없음, 해당 없음.",
                    "리셋만 보고 바로 매수. 판단은 1년 차이 구간으로.",
                ), 3))
                state["reset_pending"] = None
                state["pre_armed"] = True
            else:
                rp["runs"] = int(rp.get("runs", 0)) + 1
                if rp["runs"] >= 3 and state.get("unconfirmed_sent_for") != rp["T"]:
                    alerts.append(("ops", UNCONF_TITLE, body(
                        stamp,
                        f"미국 {rp['T']} 30% 5일 연속 추정 뒤 3번 실행했지만 펀드 공시 25.5% 이하나 네이버 주식 수 8% 감소가 안 보입니다. {wl}",
                        "FunETF 구성종목에서 팔란티어 비중을 직접 보세요.",
                        "확인되면 리셋 완료 알림이 옵니다.",
                        "보유 없음, 해당 없음.",
                        "확인 불가를 리셋 완료나 미실시로 읽기.",
                    ), 3))
                    state["unconfirmed_sent_for"] = rp["T"]
    if data.get("naver_shares"):
        state["pltr_shares_cu"] = data["naver_shares"]

    # --- gap zone change (send on change) ---
    g = ctx.get("gap")
    if g:
        prev = state.get("gap") or {}
        prev_zone = prev.get("zone_sent")
        g = dict(g, zone_sent=prev_zone if prev_zone else g["zone"])
        if prev_zone and prev_zone != g["zone"]:
            if g["zone"] == Z_BUY:
                title, pri = f"[PLTR25] 1년 차이 {g['value']:+.1f}%p | 팔란티어 비중 효과 확인, 매수 검토", 4
                act, avoid = zone_action(Z_BUY), "한 번에 다 사기. 리셋 직전(비중 29% 이상)이면 리셋 뒤로 미루기."
            elif g["zone"] == Z_HOLD:
                title, pri = f"[PLTR25] 1년 차이 {g['value']:+.1f}%p | 효과 없음, 매수 보류", 3
                act, avoid = zone_action(Z_HOLD), "팔란티어 단기 급등만 보고 사기."
            else:
                title, pri = f"[PLTR25] 1년 차이 {g['value']:+.1f}%p | 관망으로 변경", 3
                act, avoid = zone_action(Z_WAIT), "구간이 바뀐 첫날 바로 사기."
            alerts.append(("port", title, body(
                stamp,
                f"{gap_line(ctx)} 이전 구간 {prev_zone}. 2일 연속 확인. " + (weight_line(ctx) if rw else ""),
                act,
                "매수 검토는 +3.0%p 이상 2일, 해제는 +1.0%p 미만 2일. 보류는 -2.0%p 이하 2일, 해제는 0 이상 2일.",
                "보유 없음, 해당 없음.",
                avoid,
            ), pri))
            g["zone_sent"] = g["zone"]
        state["gap"] = g

    # --- weekly (ISO 주 첫 실행, p2) ---
    wk = today.strftime("%G-W%V")
    if state.get("last_weekly") != wk and (rw or g):
        pl = one_year(pltr or [])
        ql = one_year(qqq or [])
        pq = (f"PLTR 1년 {pl:+.1f}%, QQQ 1년 {ql:+.1f}%, 차이 {pl - ql:+.1f}%p (미국 {ctx.get('us_date')} 종가, 달러 기준)."
              if pl is not None and ql is not None else "PLTR 대 QQQ 1년: 조회 실패.")
        wtxt = f"비중 {ctx['w'] * 100:.1f}%" if rw else "비중 확인 안 됨"
        gtxt = f"1년 차이 {g['value']:+.1f}%p, {g['zone']}" if g else "1년 차이 확인 안 됨"
        dist = (f" 30%까지 팔란티어가 나머지보다 약 {need_rel(ctx['w']):.0f}% 더 올라야 합니다. 마지막 리셋 {ctx['last_reset']['date']} ({'정기' if ctx['last_reset']['kind'] == 'monthly' else '수시'})."
                if rw else "")
        rng = f" 최근 4주 1년 차이 범위 {g['range4w'][0]:+.1f}~{g['range4w'][1]:+.1f}%p." if g else ""
        alerts.append(("port", f"[PLTR25] 주간 점검 | {wtxt}, {gtxt}", body(
            stamp,
            (weight_line(ctx) if rw else "비중 계산 실패.") + dist + " " + gap_line(ctx) + rng + " " + pq,
            zone_action(g["zone"]) if g else "1년 차이 확인 뒤 판단하세요.",
            "비중 29% 이상이면 리셋 근접 알림, 1년 차이 구간이 바뀌면 바로 알림이 옵니다.",
            "보유 없음, 해당 없음.",
            "주간 점검을 매수 신호로 읽기.",
        ), 2))
        state["last_weekly"] = wk
    return alerts


def banned_in(text: str) -> bool:
    return any(b in text for b in BANNED)


# ---------------- io ----------------
def load_state() -> dict:
    st = dict(DEFAULT_STATE)
    if STATE_PATH.exists():
        try:
            st.update(json.loads(STATE_PATH.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass
    return st


def gather(cfg: dict, now: datetime) -> dict:
    src = cfg["sources"]
    data: dict = {"errors": []}
    try:
        data["pltr"] = parse_yahoo(fetch(src["pltr"]))
        data["qqq"] = parse_yahoo(fetch(src["qqq"]))
    except Exception as exc:
        print(f"yahoo fail: {exc}")
        data["pltr"] = data["qqq"] = None
    try:
        data["funetf"] = parse_funetf(fetch(src["funetf"]))
        if not data["funetf"]:
            print("funetf: PLTR weight not found, fallback drift")
    except Exception as exc:
        print(f"funetf fail (fallback drift): {exc}")
        data["funetf"] = None
    nav = {}
    for key, code in (("a", cfg["codes"]["fund"]), ("b", cfg["codes"]["compare"])):
        try:
            na = parse_naver_analysis(fetch(src["naver_analysis"].format(code=code)))
            nav[key] = na["nav_y1"]
            nav["ref"] = na["ref"]
            if key == "a":
                data["naver_shares"] = na["shares"]
        except Exception as exc:
            print(f"naver analysis {code} fail: {exc}")
    data["nav"] = nav
    try:
        data["closes_a"] = parse_fchart(fetch(src["naver_daily"].format(code=cfg["codes"]["fund"])), now.date(), now)
        data["closes_b"] = parse_fchart(fetch(src["naver_daily"].format(code=cfg["codes"]["compare"])), now.date(), now)
    except Exception as exc:
        print(f"naver daily fail: {exc}")
        data["closes_a"] = data["closes_b"] = None
    return data


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    if mode == "selftest":
        return selftest(cfg)
    dry = mode != "auto"
    now = datetime.now(KST)
    state = load_state()
    data = gather(cfg, now)
    alerts = evaluate(cfg, state, data, now)
    print(f"weight_model={state.get('weight_model')} funetf={state.get('weight_funetf')} streak={state.get('streak30')} "
          f"last_reset={state.get('last_reset')} pre_armed={state.get('pre_armed')} gap={state.get('gap')}")
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
def _bars(start: date, closes: list[float], skip: set[date] | None = None) -> list[tuple[date, float]]:
    out, d = [], start
    for c in closes:
        while d.weekday() >= 5 or (skip and d in skip):
            d += timedelta(days=1)
        out.append((d, c))
        d += timedelta(days=1)
    return out


def selftest(cfg: dict) -> int:
    ok = 0
    fails: list[str] = []
    copies: list[str] = [CFG_PATH.read_text(encoding="utf-8")]

    def check(label, got, want=True):
        nonlocal ok
        if got == want:
            ok += 1
        else:
            fails.append(f"{label}: got {got!r} want {want!r}")

    def run(state, data, now):
        al = evaluate(cfg, state, data, now)
        for a in al:
            copies.append(a[1] + a[2])
        return al

    def titles(al):
        return [a[1] for a in al]

    lv = cfg["levels"]
    # 기본 gap 데이터 (관망)
    def closes(n=400, start=date(2025, 5, 13), fa=1.0, fb=1.0):
        a, b, d = {}, {}, start
        i = 0
        while len(a) < n:
            if d.weekday() < 5:
                a[d] = 10000 * (1 + 0.001 * i * fa)
                b[d] = 10000 * (1 + 0.001 * i * fb)
                i += 1
            d += timedelta(days=1)
        return a, b

    ga, gb = closes()
    base_gap = {"closes_a": ga, "closes_b": gb, "nav": {"a": 7.45, "b": 7.14, "ref": "2026.10.28"}}

    # 미국 일봉: 9월 말 + 10월. PLTR 이 10월 둘째 주부터 크게 오름.
    q = [100.0] * 30
    p_flat = [100.0] * 30
    oct1 = date(2026, 10, 1)
    sept = _bars(date(2026, 9, 21), [100.0] * 8)  # 9/21~9/30
    def mk(pc, qc=None, skip=None):
        pb = sept + _bars(oct1, pc, skip)
        qb = sept + _bars(oct1, qc or [100.0] * len(pc), skip)
        return pb, qb

    # 1) 30% 초과 비중이 되는 PLTR 배수: w=0.25r/(0.25r+0.75*(1-0.02r)/0.98)
    hi = [100.0, 100, 100, 135, 136, 137, 138, 139]  # 10/1..: 4~8번째 막대 >30%, 8번째가 T
    pb, qb = mk(hi)
    rw = replay_weights(pb, qb, cfg)
    check("reset date = first US day of Oct", rw["last_reset"]["date"], "2026-10-01")
    check("weight at reset day 25%", round(rw["weights"][0], 4), 0.25)
    check("1.35x above 30%", rw["weights"][3] > 0.30)
    check("trigger on 5th day above", [e["T"] for e in rw["events"]], [pb[-1][0].isoformat()])
    check("pending exec after trigger on last bar", rw["pending_exec"], True)
    # 다음 날 막대가 생기면 리셋 25%
    pb2, qb2 = mk(hi + [140.0])
    rw2 = replay_weights(pb2, qb2, cfg)
    check("adhoc reset executes T+1 at 25%", (rw2["last_reset"]["kind"], round(rw2["weights"][-1], 4)), ("adhoc", 0.25))
    check("streak 0 after reset", rw2["streak"], 0)

    # 2) 휴장일(미국 10/12 없음)이 있어도 막대 기준으로 연속 일수 셈
    hol = {date(2026, 10, 12)}
    pb3, qb3 = mk(hi, skip=hol)
    rw3 = replay_weights(pb3, qb3, cfg)
    check("holiday: bars skip 10/12", date(2026, 10, 12) in [d for d, _ in pb3], False)
    check("holiday: still 5-bar streak trigger", len(rw3["events"]), 1)

    # 3) 월말 4일 초과 뒤 새 달 첫 영업일 정기 리셋이면 트리거 없음
    sep_hi = _bars(date(2026, 9, 1), [100.0, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 100, 135, 136, 137, 138])
    oct_b = _bars(oct1, [138.0, 138, 138])
    rw4 = replay_weights(sep_hi + oct_b, [(d, 100.0) for d, _ in sep_hi + oct_b], cfg)
    check("monthly reset breaks streak", (rw4["events"], rw4["last_reset"]["date"], rw4["streak"]), ([], "2026-10-01", 0))

    # 4) evaluate: 빠진 실행 (2일째 상태에서 바로 5일째 데이터) -> 트리거 1번
    now = datetime(2026, 10, 14, 9, 0, tzinfo=KST)  # 수요일, W42
    st = dict(DEFAULT_STATE, last_weekly="2026-W42")
    pb5, qb5 = mk(hi[:6])  # 4번째, 5번째, 6번째 막대: 초과 3일
    al = run(st, dict(base_gap, pltr=pb5, qqq=qb5, funetf=None, naver_shares=765.0), now)
    check("pre-alert at 3 days above (p3)", [(t.startswith("[PLTR25] 팔란티어 비중"), p) for _, t, _, p in al], [(True, 3)])
    check("pre disarmed", st["pre_armed"], False)
    al = run(st, dict(base_gap, pltr=pb5, qqq=qb5, funetf=None, naver_shares=765.0), now)
    check("idempotent: same data no send", al, [])
    al = run(st, dict(base_gap, pltr=pb, qqq=qb, funetf=None, naver_shares=765.0), now + timedelta(days=2))
    check("missed run: trigger once (p4)", [(t, p) for _, t, _, p in al], [("[PLTR25] 30% 5일 연속 | 오늘 밤 25%로 리셋 예정, 매수 관망", 4)])
    check("pending set with shares ref", (st["reset_pending"]["T"], st["reset_pending"]["shares_ref"]), (pb[-1][0].isoformat(), 765.0))
    al = run(st, dict(base_gap, pltr=pb, qqq=qb, funetf=None, naver_shares=765.0), now + timedelta(days=2))
    check("trigger not repeated", al, [])
    # 5) 리셋 확인: 네이버 주식 수 5% 감소는 아님, 9% 감소면 완료
    al = run(st, dict(base_gap, pltr=pb2, qqq=qb2, funetf=None, naver_shares=765.0 * 0.95), now + timedelta(days=3))
    check("share drop 5% not confirmed", any("리셋 완료" in t for t in titles(al)), False)
    al = run(st, dict(base_gap, pltr=pb2, qqq=qb2, funetf=None, naver_shares=765.0 * 0.91), now + timedelta(days=3))
    check("share drop 9% confirms reset (p3)", [(t.startswith("[PLTR25] 리셋 완료"), p) for _, t, _, p in al], [(True, 3)])
    check("pending cleared, re-armed", (st["reset_pending"], st["pre_armed"]), (None, True))
    # FunETF 25.3% (T 다음 미국 날짜) 로도 확인
    st2 = dict(DEFAULT_STATE, last_weekly="2026-W42", trigger_sent_for=pb[-1][0].isoformat(),
               reset_pending={"T": pb[-1][0].isoformat(), "shares_ref": None, "runs": 0}, pre_armed=False)
    kr_after = (pb2[-1][0] + timedelta(days=1)).isoformat()
    al = run(st2, dict(base_gap, pltr=pb2, qqq=qb2, funetf={"kr_date": kr_after, "weight": 25.3, "shares": 640.0}, naver_shares=None), now + timedelta(days=3))
    check("funetf 25.3% after T confirms", any(t.startswith("[PLTR25] 리셋 완료") for t in titles(al)), True)
    # FunETF 가 아직 T 날짜(25.4%여도 T 이전 자료)면 확인 아님
    st3 = dict(DEFAULT_STATE, last_weekly="2026-W42", trigger_sent_for=pb[-1][0].isoformat(),
               reset_pending={"T": pb[-1][0].isoformat(), "shares_ref": None, "runs": 2}, pre_armed=False)
    kr_T = (pb[-1][0] + timedelta(days=1)).isoformat()
    al = run(st3, dict(base_gap, pltr=pb, qqq=qb, funetf={"kr_date": kr_T, "weight": 25.4, "shares": 760.0}, naver_shares=None), now + timedelta(days=3))
    check("stale funetf not confirm; 3rd run -> ops unconfirmed", [(k, t) for k, t, _, _ in al], [("ops", UNCONF_TITLE)])
    check("unconfirmed routes ops", route_topic("cwally-port", UNCONF_TITLE), "cwally-ops")

    # 6) 사전 알림 히스테리시스: 29.1 보냄, 28.5 침묵, 27.9 재무장, 29.0 다시
    def wbars(target):
        # 10/1 리셋 뒤 하루: 목표 비중이 되는 PLTR 배수 찾기
        lo_, hi_ = 1.0, 2.0
        for _ in range(60):
            mid = (lo_ + hi_) / 2
            rr = (1 - 0.02 * mid) / 0.98
            w = 0.25 * mid / (0.25 * mid + 0.75 * rr)
            lo_, hi_ = (mid, hi_) if w < target else (lo_, mid)
        return mk([100.0, 100 * hi_])
    st = dict(DEFAULT_STATE, last_weekly="2026-W42")
    seq = []
    for tw in (0.291, 0.285, 0.279, 0.2905):
        b_ = wbars(tw)
        al = run(st, dict(base_gap, pltr=b_[0], qqq=b_[1], funetf=None, naver_shares=765.0), now)
        seq.append(len([1 for t in titles(al) if "팔란티어 비중" in t]))
    check("pre hysteresis 29.1/28.5/27.9/29.05", seq, [1, 0, 0, 1])
    b_ = wbars(0.2995)
    ctxb = {"w": 0.2995, "us_date": "x", "streak": 0, "src": "drift", "funetf": None, "lv": lv}
    check("band note at 29.95%", "±0.2%p" in weight_line(ctxb), True)
    ctxb["w"] = 0.293
    check("no band note at 29.3%", "±0.2%p" in weight_line(ctxb), False)

    # 7) FunETF 기준점: 공시 26.55% (한국 10/28 = 미국 10/27 막대) 그날 비중이 공시값
    pb6, qb6 = mk([100.0, 101, 102, 103, 104, 105, 106, 107, 108, 109, 110, 111, 112, 113, 114, 115, 116, 117, 118, 119])
    kr = (pb6[-1][0] + timedelta(days=1)).isoformat()
    rw6 = replay_weights(pb6, qb6, cfg, (pb6[-1][0], 26.55))
    check("funetf anchor sets weight", (rw6["src"], round(rw6["weights"][-1] * 100, 2)), ("funetf", 26.55))
    st = dict(DEFAULT_STATE, last_weekly="2026-W42")
    run(st, dict(base_gap, pltr=pb6, qqq=qb6, funetf={"kr_date": kr, "weight": 26.55, "shares": 764.73}, naver_shares=765.0), now)
    check("funetf us_date = prior US bar", st["weight_funetf"]["us_date"], pb6[-1][0].isoformat())
    rw7 = replay_weights(pb6, qb6, cfg, None)
    check("fallback pure drift src", rw7["src"], "drift")

    # 8) gap 구간: 2일 확인 + 히스테리시스
    def zseq(vals):
        ser = [(date(2026, 6, 1) + timedelta(days=i), v) for i, v in enumerate(vals)]
        return replay_zone(ser, lv)
    check("single day +3.5 stays 관망", zseq([0.5, 3.5]), Z_WAIT)
    check("two days >= +3 -> 매수 검토", zseq([0.5, 3.1, 3.0]), Z_BUY)
    check("매수 검토 holds at +1.5", zseq([3.1, 3.2, 1.5, 1.2]), Z_BUY)
    check("one day < +1 holds", zseq([3.1, 3.2, 0.9, 1.1]), Z_BUY)
    check("two days < +1 -> 관망", zseq([3.1, 3.2, 0.9, 0.8]), Z_WAIT)
    check("two days <= -2 -> 보류", zseq([0.0, -2.0, -2.5]), Z_HOLD)
    check("보류 holds at -0.5", zseq([-2.1, -2.2, -0.5, -0.3]), Z_HOLD)
    check("two days >= 0 exits 보류", zseq([-2.1, -2.2, 0.0, 0.2]), Z_WAIT)
    check("보류 to 매수 검토 directly", zseq([-2.1, -2.2, 3.0, 3.3]), Z_BUY)
    check("before replay start ignored", replay_zone([(date(2026, 5, 1), 5.0), (date(2026, 5, 2), 5.0)], lv), Z_WAIT)
    # gap 변경 알림 (send on change)
    a1, b1 = closes(fa=1.0, fb=0.5)  # 0047R0 가 앞섬 -> 매수 검토
    st = dict(DEFAULT_STATE, last_weekly="2026-W42", gap={"zone_sent": Z_WAIT})
    pbf, qbf = mk([100.0, 100])
    al = run(st, dict(pltr=pbf, qqq=qbf, funetf=None, naver_shares=765.0, closes_a=a1, closes_b=b1, nav={}), now)
    check("gap change to 매수 검토 p4", [(t.endswith("매수 검토"), p) for _, t, _, p in al], [(True, 4)])
    al = run(st, dict(pltr=pbf, qqq=qbf, funetf=None, naver_shares=765.0, closes_a=a1, closes_b=b1, nav={}), now)
    check("gap no repeat", al, [])
    a2, b2 = closes(fa=0.5, fb=1.0)
    al = run(st, dict(pltr=pbf, qqq=qbf, funetf=None, naver_shares=765.0, closes_a=a2, closes_b=b2, nav={}), now)
    check("gap change to 보류 p3", [(t.endswith("매수 보류"), p) for _, t, _, p in al], [(True, 3)])
    st = dict(DEFAULT_STATE, last_weekly="2026-W42")
    al = run(st, dict(pltr=pbf, qqq=qbf, funetf=None, naver_shares=765.0, closes_a=a2, closes_b=b2, nav={}), now)
    check("first run seeds zone silently", (al, st["gap"]["zone_sent"]), ([], Z_HOLD))

    # 9) 주간 점검: ISO 주 1번, PLTR/QQQ 줄 포함
    py = _bars(date(2025, 10, 1), [100.0 + i * 0.2 for i in range(280)])
    qy = [(d, 100.0 + i * 0.05) for i, (d, _) in enumerate(py)]
    wnow = datetime(2026, 11, 2, 9, 0, tzinfo=KST)  # 월요일 W45
    st = dict(DEFAULT_STATE, last_weekly="2026-W44")
    al = run(st, dict(base_gap, pltr=py, qqq=qy, funetf=None, naver_shares=765.0), wnow)
    wk = [a for a in al if "주간 점검" in a[1]]
    check("weekly sends once in new ISO week (p2)", [(a[0], a[3]) for a in wk], [("port", 2)])
    check("weekly has PLTR/QQQ 1Y line", bool(wk and re.search(r"PLTR 1년 [+-]\d+\.\d%, QQQ 1년 [+-]\d+\.\d%, 차이 [+-]\d+\.\d%p", wk[0][2])), True)
    pl, ql = one_year(py), one_year(qy)
    check("1Y calc sane", pl is not None and ql is not None and pl > ql, True)
    al = run(st, dict(base_gap, pltr=py, qqq=qy, funetf=None, naver_shares=765.0), wnow + timedelta(days=1))
    check("weekly not repeated same week", [a for a in al if "주간 점검" in a[1]], [])
    st["last_weekly"] = "2026-W44"
    al = run(st, dict(base_gap, pltr=py, qqq=qy, funetf=None, naver_shares=765.0), datetime(2026, 11, 3, 9, 0, tzinfo=KST))
    check("weekly on first run of week even if Tuesday", len([a for a in al if "주간 점검" in a[1]]), 1)

    # 10) 조회 실패: ops 로, 하루 1번, 상태값 유지
    st = dict(DEFAULT_STATE, last_weekly="2026-W42", streak30=3, weight_model=29.5)
    al = run(st, dict(pltr=None, qqq=None, funetf=None, closes_a=None, closes_b=None), now)
    check("fetch fail -> ops once", [(k, t) for k, t, _, _ in al], [("ops", FAIL_TITLE)])
    check("fail title routes ops", route_topic("cwally-port", FAIL_TITLE), "cwally-ops")
    check("streak kept on failure", (st["streak30"], st["weight_model"]), (3, 29.5))
    al = run(st, dict(pltr=None, qqq=None, funetf=None, closes_a=None, closes_b=None), now)
    check("fetch fail not repeated same day", al, [])

    # 11) 파서
    fx = ('<meta content="(2026.10.28 기준)">{"@type":"PropertyValue","name":"팔란티어","propertyID":"etf:holding","value":"PLTR",'
          '"additionalProperty":[{"propertyID":"etf:holding.rank","value":1},{"propertyID":"etf:holding.weight","value":26.55,"unitCode":"P1"},'
          '{"propertyID":"etf:holding.shares","value":764.73,"unitText":"주"}]},')
    check("parse funetf", parse_funetf(fx), {"kr_date": "2026-10-28", "weight": 26.55, "shares": 764.73})
    check("parse funetf missing", parse_funetf("<html></html>"), None)
    nv = json.dumps({"etfTop10MajorConstituentAssets": [{"itemName": "PALANTIR TECHNOLOGIES INC-A", "stockCount": "765"}],
                     "navPerformanceList": [{"periodTypeCode": "Y1", "value": 7.45}], "navPerformanceReferenceDate": "2026.10.28"})
    check("parse naver shares/nav", (parse_naver_analysis(nv)["shares"], parse_naver_analysis(nv)["nav_y1"]), (765.0, 7.45))
    fc = '<item data="20261028|1|1|1|15190|100" /><item data="20261029|1|1|1|15000|100" />'
    pc = parse_fchart(fc, date(2026, 10, 29), datetime(2026, 10, 29, 9, 0, tzinfo=KST))
    check("fchart drops today's bar before 15:40", sorted(pc), [date(2026, 10, 28)])
    t0 = int(datetime(2026, 10, 27, 13, 30, tzinfo=timezone.utc).timestamp())
    t1 = int(datetime(2026, 10, 28, 13, 30, tzinfo=timezone.utc).timestamp())
    yj = json.dumps({"chart": {"result": [{"meta": {"currentTradingPeriod": {"regular": {"start": t1, "end": t1 + 23400}}},
                                           "timestamp": [t0, t1], "indicators": {"quote": [{"close": [198.73, 196.0]}]}}]}})
    check("yahoo drops intraday bar", [d.isoformat() for d, _ in parse_yahoo(yj, now_ts=t1 + 3600)], ["2026-10-27"])
    check("yahoo keeps closed bar", len(parse_yahoo(yj, now_ts=t1 + 30000)), 2)

    # 12) 금지 문자
    check("no middle dot / em-dash in copy", [c[:40] for c in copies if banned_in(c)], [])
    check("body six-item order", [ln.split(":")[0] for ln in body("s", "a", "b", "c", "d", "e").splitlines()[1:]],
          ["이게 뭐 알림", "지금 결과/상태", "지금 할 일", "나중에", "익절/줄일 때", "하지 말 것"])

    total = ok + len(fails)
    for f in fails:
        print("FAIL", f)
    print(f"selftest {ok}/{total}")
    return 0 if not fails else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        sys.exit(1)
