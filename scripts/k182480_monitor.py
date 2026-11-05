#!/usr/bin/env python3
"""[182480] TIGER 미국MSCI리츠(합성 H) 금리, 분배금 조건 알림 (2026-11-05 요청 pt61u).

평일 09:00 KST morning 묶음(rblx 다음). 판정 기본값은 '보통(분산/인컴용 소액)'. 아래가 바뀔 때만 보냄:
  1) 미 10년물(FRED DGS10) 4.30% 이하 2회 연속 -> 금리 하향(4.50% 위 재무장)
  2) 미 10년물 5.30% 이상 -> 금리 경계(5.10% 아래 재무장)
  3) 새 분배금(기준일)이 보이면 1번: 12개월 합, 같은 달 전년 대비, 분배율, 헤지 뒤 10년물 대비 차이
  4) 순자산 800억 미만(900억 이상 재무장), 괴리율 -2.0% 이하(-1.0% 위 재무장)
  5) 종합 판정(매력/보통/편입 보류) 변경 p4
소스가 실패하면 그 규칙만 이번 실행에서 건너뜀. 같은 소스가 2일 연속 실패하면 cwally-ops 1번(회복 뒤 다시 무장).
modes: auto | dry(발송 없음, state 안 씀) | selftest
"""
from __future__ import annotations

import json
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic, route_topic  # noqa: E402
from kr_calendar import KST  # noqa: E402

NAME = "k182480"
CFG_PATH = ROOT / "configs" / f"{NAME}.json"
STATE_PATH = ROOT / "state" / f"{NAME}.json"
FX = Path(__file__).resolve().parent / "fixtures" / NAME
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
TAG = "[182480]"


def default_state() -> dict:
    return {"updated_at": None, "y10_last_date": None, "rate_low_armed": True, "rate_high_armed": True,
            "last_dist_date": None, "ttm": None, "dist_class": "보통", "verdict": "보통",
            "aum_armed": True, "dev_armed": True, "fail": {}}


def load_state() -> dict:
    st = default_state()
    if STATE_PATH.exists():
        try:
            st.update(json.loads(STATE_PATH.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass
    return st


# ---------------- 데이터 ----------------
def http(url: str, timeout: int = 25) -> str:
    import urllib.request
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def fred(series: str, start: date) -> list[tuple[str, float]]:
    txt = http(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}&cosd={start.isoformat()}")
    rows = [ln.split(",") for ln in txt.strip().splitlines()]
    if not rows or series not in rows[0][-1]:
        raise RuntimeError("FRED csv 형식 다름")
    out = []
    for r in rows[1:]:
        try:
            out.append((r[0], float(r[1])))
        except (ValueError, IndexError):
            continue
    if not out:
        raise RuntimeError(f"FRED {series} 값 없음")
    return out


def y10_series(today: date) -> list[tuple[str, float]]:
    try:
        return fred("DGS10", today - timedelta(days=21))
    except Exception as exc:  # noqa: BLE001
        print(f"FRED DGS10 fail, Treasury fallback: {exc}", file=sys.stderr)
        from usbond_monitor import treasury_10y
        return sorted(treasury_10y(today - timedelta(days=21), today).items())


def hedge_cost(today: date, fallback: float) -> tuple[float, str]:
    try:
        us3m = fred("DGS3MO", today - timedelta(days=21))[-1][1]
        from krrate_monitor import ecos_daily
        cd = sorted(ecos_daily("010502000", today - timedelta(days=21), today).items())[-1][1]
        return us3m - cd, f"미 3개월 {us3m:.2f}% - CD91 {cd:.2f}%"
    except Exception as exc:  # noqa: BLE001
        print(f"hedge cost fallback: {exc}", file=sys.stderr)
        return fallback, f"추정 상수 {fallback:.2f}%p"


def parse_dists(html: str) -> list[tuple[str, float]]:
    """[(기준일 YYYY-MM-DD, 원)] 오래된 순. etf-lab(텍스트 행) 또는 etfro(data-sort 표) 모두."""
    rows = set()
    for a, _b, amt in re.findall(r"(20\d\d[.-]\d\d[.-]\d\d)\D{0,200}?(20\d\d[.-]\d\d[.-]\d\d)\D{0,200}?([\d,]+)\s*원", html):
        rows.add((a.replace(".", "-"), float(amt.replace(",", ""))))
    for a, _b, amt in re.findall(r'data-sort="(\d{8})">[^<]*</td>\s*<td data-sort="(\d{8})">[^<]*</td>\s*<td class="num" data-sort="([\d.]+)"', html):
        rows.add((f"{a[:4]}-{a[4:6]}-{a[6:]}", float(amt)))
    by = {}
    for d, v in rows:
        by[d] = v
    return sorted(by.items())


def dists() -> list[tuple[str, float]]:
    errs = []
    for url in ("https://etf-lab.kr/182480", "https://etfro.com/etf/182480"):
        try:
            r = parse_dists(http(url))
            if len(r) >= 13:
                return r
            errs.append(f"{url} 행 {len(r)}개")
        except Exception as exc:  # noqa: BLE001
            errs.append(f"{url} {exc}")
    raise RuntimeError(" / ".join(errs))


def etf_side(today: date) -> dict:
    d = json.loads(http("https://m.stock.naver.com/api/stock/182480/etfAnalysis"))
    aum = re.sub(r"[^\d.]", "", str(d.get("totalNav") or ""))
    dev = d.get("deviationRate")
    if not aum or dev is None:
        raise RuntimeError("순자산/괴리율 없음")
    sign = -1 if d.get("deviationSign") == "-" else 1
    return {"aum_eok": float(aum), "dev_pct": sign * float(dev), "ref": d.get("returnPerformanceReferenceDate")}


def prior_close(today: date) -> tuple[str, float]:
    import krx_close
    bars = [b for b in krx_close.regular_bars("182480", 8) if b.day < today.isoformat()]
    if not bars:
        raise RuntimeError("전 거래일 종가 없음")
    return bars[-1].day, bars[-1].close


# ---------------- 계산 ----------------
def ttm_of(rows: list[tuple[str, float]], end_idx: int) -> float | None:
    if end_idx < 11:
        return None
    return sum(v for _, v in rows[end_idx - 11: end_idx + 1])


def prev_same_month(rows: list[tuple[str, float]], idx: int) -> float | None:
    d = rows[idx][0]
    ym = f"{int(d[:4]) - 1}-{d[5:7]}"
    prev = [x for x in rows if x[0][:7] == ym]
    return prev[-1][1] if prev else None


def same_month_yoy(rows: list[tuple[str, float]], idx: int) -> float | None:
    base = prev_same_month(rows, idx)
    if not base or base <= 0:
        return None
    return (rows[idx][1] / base - 1) * 100


def classify(ttm: float, yoy: float | None, r: dict) -> str:
    if ttm < r["ttm_warn"] or (yoy is not None and yoy < r["same_month_yoy_warn_pct"]):
        return "경계"
    if ttm >= r["ttm_good"]:
        return "양호"
    return "보통"


def evaluate(cfg: dict, st: dict, obs: dict) -> list[tuple[str, str, int]]:
    """obs: y10 [(date,v)] | None, hedge (pp, note), dists [(date,v)] | None, price (date,v) | None, etf {aum,dev} | None."""
    r = cfg["rules"]
    out: list[tuple[str, str, int]] = []
    y10 = obs.get("y10")
    hedge, hnote = obs.get("hedge") or (r["hedge_cost_fallback_pp"], "추정 상수")
    price = obs.get("price")
    lines_ctx = []
    if y10:
        lines_ctx.append(f"미 10년물 {y10[-1][1]:.2f}%({y10[-1][0]})")
    rate_low2 = None
    if y10:
        last2 = [v for _, v in y10[-r["rate_low_consec"]:]]
        rate_low2 = len(last2) == r["rate_low_consec"] and all(v <= r["rate_low"] for v in last2)
        cur = y10[-1][1]
        if rate_low2 and st["rate_low_armed"]:
            st["rate_low_armed"] = False
            out.append((f"{TAG} 미 10년물 {cur:.2f}% | 금리 하향, 매력 상향 검토",
                        f"미 10년물이 {r['rate_low']:.2f}% 이하로 {r['rate_low_consec']}번 연속({', '.join(f'{d} {v:.2f}%' for d, v in y10[-2:])}). "
                        f"리츠 할인율 부담이 줄어드는 쪽. 분배금 흐름이 경계가 아니면 판정을 한 단계 올려 볼 구간.", 3))
        elif cur > r["rate_low_rearm"]:
            st["rate_low_armed"] = True
        if cur >= r["rate_high"] and st["rate_high_armed"]:
            st["rate_high_armed"] = False
            out.append((f"{TAG} 미 10년물 {cur:.2f}% | 금리 경계, 편입 보류",
                        f"미 10년물 {cur:.2f}%({y10[-1][0]}), {r['rate_high']:.2f}% 이상. 2026년 고점 5.31%(10/5) 근처. 신규 편입은 금리가 {r['rate_high_rearm']:.2f}% 아래로 내려올 때까지 보류.", 4))
        elif cur < r["rate_high_rearm"]:
            st["rate_high_armed"] = True
        st["y10_last_date"] = y10[-1][0]
    rows = obs.get("dists")
    if rows:
        idx = len(rows) - 1
        nd, nv = rows[idx]
        if st.get("last_dist_date") is None or nd > st["last_dist_date"]:
            ttm = ttm_of(rows, idx)
            if ttm is not None:
                prev_ttm = st.get("ttm") or ttm_of(rows, idx - 1)
                base = prev_same_month(rows, idx)
                yoy = same_month_yoy(rows, idx)
                min_base = r.get("yoy_min_base_krw", 30)
                yoy_used = yoy if base is not None and base >= min_base else None
                cls = classify(ttm, yoy_used, r)
                yld = (ttm / price[1] * 100) if price else None
                ctx = ""
                if yld is not None and y10:
                    hedged = y10[-1][1] - hedge
                    ctx = (f" 분배율 {yld:.2f}%(12개월 합 / {price[0]} 종가 {price[1]:,.0f}원), 헤지 뒤 미 10년물 {hedged:.2f}%"
                           f"({y10[-1][1]:.2f}% - 헤지 비용 {hedge:.2f}%p, {hnote}), 차이 {yld - hedged:+.2f}%p.")
                elif yld is not None:
                    ctx = f" 분배율 {yld:.2f}%."
                if yoy is None:
                    yoy_txt = "같은 달 전년 값 없음"
                elif yoy_used is None:
                    yoy_txt = f"전년 동월 {base:.0f}원({min_base:.0f}원 미만), 전년 비교 제외(지금 {yoy:+.0f}%)"
                else:
                    yoy_txt = f"같은 달 전년 대비 {yoy:+.0f}%(전년 {base:.0f}원)"
                head = {"경계": "분배 경계", "양호": "분배 양호", "보통": "월간 분배 업데이트"}[cls]
                out.append((f"{TAG} {nd[5:7]}월 분배금 {nv:.0f}원 | {head}",
                            f"기준일 {nd} 분배금 {nv:.0f}원, {yoy_txt}. 12개월 합 {ttm:.0f}원(직전 {prev_ttm:.0f}원, "
                            f"{ttm - prev_ttm:+.0f}원). 기준 420원 미만 또는 같은 달 -20% 미만(전년 동월 30원 이상일 때만) 경계, 460원 이상 양호.{ctx}",
                            4 if cls == "경계" else 3 if cls == "양호" else 2))
                st.update(last_dist_date=nd, ttm=ttm, dist_class=cls)
    etf = obs.get("etf")
    if etf:
        if etf["aum_eok"] < r["aum_warn_eok"] and st["aum_armed"]:
            st["aum_armed"] = False
            out.append((f"{TAG} 순자산 {etf['aum_eok']:,.0f}억 | 자금 유출 경계",
                        f"순자산 {etf['aum_eok']:,.0f}억, {r['aum_warn_eok']}억 아래. 연초 이후 순유출이 이어지는 중이면 유동성과 상장 유지 위험을 같이 보세요.", 4))
        elif etf["aum_eok"] >= r["aum_rearm_eok"]:
            st["aum_armed"] = True
        if etf["dev_pct"] <= r["dev_warn_pct"] and st["dev_armed"]:
            st["dev_armed"] = False
            out.append((f"{TAG} 괴리율 {etf['dev_pct']:+.2f}% | 할인 거래 주의",
                        f"전 거래일 마감 괴리율 {etf['dev_pct']:+.2f}%, {r['dev_warn_pct']}% 이하. 시장가 매매는 피하고 NAV 근처 지정가로.", 3))
        elif etf["dev_pct"] > r["dev_rearm_pct"]:
            st["dev_armed"] = True
    if y10:
        # 히스테리시스: 금리 하향/경계 상태는 재무장선(4.50/5.10)을 넘을 때까지 유지해 판정이 매일 뒤집히지 않게 함
        low_on = not st["rate_low_armed"]
        high_on = not st["rate_high_armed"]
        if low_on and st["dist_class"] != "경계":
            v = "매력"
        elif high_on or st["dist_class"] == "경계":
            v = "편입 보류"
        else:
            v = "보통"
        if v != st["verdict"]:
            out.append((f"{TAG} 종합 판정 변경 | {st['verdict']} -> {v}",
                        f"판정 {st['verdict']}에서 {v}로. 근거: " + ", ".join(lines_ctx + [f"분배 흐름 {st['dist_class']}"]) +
                        ". 매력 = 10년물 4.30% 이하 2회 + 분배 경계 아님, 편입 보류 = 10년물 5.30% 이상 또는 분배 경계.", 4))
            st["verdict"] = v
    return out


def body(stamp: str, text: str, verdict: str) -> str:
    return "\n".join([
        f"{stamp} | 182480 | TIGER 미국MSCI리츠(합성 H)",
        "이게 뭐 알림: 182480 의 금리, 분배금, 순자산, 괴리율 조건이 바뀔 때만 오는 알림. 기본 판정은 보통(분산/인컴용 소액). 매매 지시 아님.",
        f"지금 결과/상태: {text}",
        f"현재 종합 판정: {verdict}.",
        "하지 말 것: 하루 금리 움직임으로 크게 들어가기, 분배금 한 달 값만 보고 판단, 시장가 주문.",
    ])


# ---------------- 실행 ----------------
def gather(cfg: dict, today: date) -> tuple[dict, dict]:
    obs: dict = {}
    errs: dict = {}
    for key, fn in (("y10", lambda: y10_series(today)), ("dists", dists), ("price", lambda: prior_close(today)),
                    ("etf", lambda: etf_side(today))):
        try:
            obs[key] = fn()
        except Exception as exc:  # noqa: BLE001
            obs[key] = None
            errs[key] = str(exc)[:200]
    obs["hedge"] = hedge_cost(today, cfg["rules"]["hedge_cost_fallback_pp"])
    return obs, errs


def fail_alerts(cfg: dict, st: dict, errs: dict, today: str) -> list[tuple[str, str, int]]:
    out = []
    names = {"y10": "미 10년물", "dists": "분배금 이력", "price": "종가", "etf": "순자산/괴리율"}
    fails = st.setdefault("fail", {})
    for key in names:
        f = fails.get(key) or {"days": 0, "last": None, "notified": False}
        if key in errs:
            if f["last"] != today:
                f["days"] += 1
                f["last"] = today
            if f["days"] >= cfg["rules"]["fail_ops_after_days"] and not f["notified"]:
                f["notified"] = True
                out.append((f"{TAG} {names[key]} 조회 실패 | {f['days']}일 연속, 그 규칙 건너뜀",
                            f"{names[key]} 조회가 {f['days']}일 연속 실패({errs[key]}). 그 규칙은 회복될 때까지 판단 안 함. 조건 미충족이 아님.", 3))
        else:
            f = {"days": 0, "last": None, "notified": False}
        fails[key] = f
    return out


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    if mode == "selftest":
        return selftest()
    if mode not in {"auto", "dry"}:
        print(f"unknown mode {mode}")
        return 2
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    if now > datetime.strptime(cfg["ends_kst"], "%Y-%m-%d %H:%M").replace(tzinfo=KST):
        print("ended")
        return 0
    dry = mode == "dry"
    topic = resolve_topic("port", cfg.get("ntfy_topic"))
    server = resolve_server(cfg.get("ntfy_server"))
    st = load_state()
    obs, errs = gather(cfg, now.date())
    if errs:
        print("errors: " + json.dumps(errs, ensure_ascii=False), file=sys.stderr)
    print("obs: " + json.dumps({"y10": (obs["y10"] or [])[-3:], "dist_last": (obs["dists"] or [])[-2:], "price": obs["price"],
                                "etf": obs["etf"], "hedge": obs["hedge"]}, ensure_ascii=False))
    alerts = evaluate(cfg, st, obs) + fail_alerts(cfg, st, errs, now.date().isoformat())
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    for title, text, pri in alerts:
        print(f"TITLE: {title}\nPRIORITY: {pri} TOPIC: {route_topic(topic, title)}\n{body(stamp, text, st['verdict'])}\n")
        if not dry:
            publish(topic, title, body(stamp, text, st["verdict"]), pri, ["office"], server)
    if not alerts:
        print("no alert")
    if dry:
        print("DRY RUN: no publish, state not written")
        return 0
    st["updated_at"] = now.isoformat()
    STATE_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


# ---------------- selftest ----------------
def selftest() -> int:
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    fails: list[str] = []

    def check(name, got, want):
        ok = got == want
        print(("ok   " if ok else "FAIL ") + f"{name}: got={got!r} want={want!r}")
        if not ok:
            fails.append(name)
    lab = parse_dists((FX / "etflab.html").read_text(encoding="utf-8"))
    ro = parse_dists((FX / "etfro.html").read_text(encoding="utf-8"))
    check("etf-lab fixture rows / last / 12m 439", (len(lab), lab[-1], ttm_of(lab, len(lab) - 1)), (86, ("2026-10-30", 41.0), 439.0))
    check("etfro fixture (data-sort table) matches", (len(ro) >= 40, ro[-12:] == lab[-12:]), (True, True))
    check("annual sums 2024 535, 2025 447", (sum(v for d, v in lab if d[:4] == "2024"), sum(v for d, v in lab if d[:4] == "2025")), (535.0, 447.0))
    check("same-month yoy Oct 41 vs 45", round(same_month_yoy(lab, len(lab) - 1), 1), -8.9)
    r = cfg["rules"]
    check("classify", (classify(439, -8.9, r), classify(419, 0, r), classify(440, -21, r), classify(460, 0, r)), ("보통", "경계", "경계", "양호"))

    def init():
        s = default_state()
        s.update(last_dist_date="2026-10-30", ttm=439.0, dist_class="보통", verdict="보통")
        return s
    y = lambda *v: [(f"2026-11-{i + 1:02d}", x) for i, x in enumerate(v)]  # noqa: E731
    etf = {"aum_eok": 1083.0, "dev_pct": -0.44}
    base = {"y10": y(4.95, 4.90), "dists": lab, "price": ("2026-11-04", 13100.0), "etf": etf, "hedge": (0.87, "t")}
    st = init()
    check("initial state, current values: silent", evaluate(cfg, st, dict(base)), [])
    t = lambda st, **kw: [a[0] for a in evaluate(cfg, st, dict(base, **kw))]  # noqa: E731
    st = init()
    check("4.30 once only: no notice", t(st, y10=y(4.40, 4.30)), [])
    check("4.30 x2: rate low + verdict 매력", t(st, y10=y(4.30, 4.25)),
          [f"{TAG} 미 10년물 4.25% | 금리 하향, 매력 상향 검토", f"{TAG} 종합 판정 변경 | 보통 -> 매력"])
    check("stays low: silent", t(st, y10=y(4.25, 4.20)), [])
    check("4.45 not re-armed (needs >4.50), verdict stays 매력", t(st, y10=y(4.20, 4.45)), [])
    check("still disarmed after 4.45", st["rate_low_armed"], False)
    check(">4.50 re-arms, verdict back 보통", t(st, y10=y(4.45, 4.55)), [f"{TAG} 종합 판정 변경 | 매력 -> 보통"])
    check("re-armed >4.50 then fires again", t(st, y10=y(4.30, 4.29))[0], f"{TAG} 미 10년물 4.29% | 금리 하향, 매력 상향 검토")
    st = init()
    check("5.30: rate high + 편입 보류", t(st, y10=y(5.2, 5.30)), [f"{TAG} 미 10년물 5.30% | 금리 경계, 편입 보류", f"{TAG} 종합 판정 변경 | 보통 -> 편입 보류"])
    check("5.20 no re-arm, still 보류 silent", (t(st, y10=y(5.3, 5.20)), st["rate_high_armed"]), ([], False))
    check("<5.10 re-armed, verdict back 보통", (t(st, y10=y(5.2, 5.05)), st["rate_high_armed"]), ([f"{TAG} 종합 판정 변경 | 편입 보류 -> 보통"], True))
    nov = lab + [("2026-11-30", 20.0)]
    st = init()
    a = evaluate(cfg, st, dict(base, dists=nov))
    check("new Nov 20원: monthly update p2, ttm 439", ([x[0] for x in a], a[0][2], st["ttm"]), ([f"{TAG} 11월 분배금 20원 | 월간 분배 업데이트"], 2, 439.0))
    check("body has yield and hedged spread", ("분배율 3.35%" in a[0][1], "헤지 뒤 미 10년물 4.03%" in a[0][1], "차이 -0.68%p" in a[0][1]), (True, True, True))
    check("same distribution not repeated", evaluate(cfg, st, dict(base, dists=nov)), [])
    st = init()
    a = [x[0] for x in evaluate(cfg, st, dict(base, dists=lab + [("2026-11-30", 0.0)]))]
    check("Nov 0원 -> ttm 419 <420 -> 경계 + 편입 보류 (YoY skipped, base 20)", a, [f"{TAG} 11월 분배금 0원 | 분배 경계", f"{TAG} 종합 판정 변경 | 보통 -> 편입 보류"])
    def one(rows):
        s2 = init()
        s2["last_dist_date"] = rows[-2][0]
        a2 = evaluate(cfg, s2, dict(base, dists=rows))
        return a2[0][0], a2[0][1]
    t15, b15 = one(lab + [("2026-11-30", 15.0)])
    check("Nov base 20 now 15 (-25%): YoY skipped, ttm 434 -> not 경계", (t15, "전년 동월 20원(30원 미만), 전년 비교 제외" in b15),
          (f"{TAG} 11월 분배금 15원 | 월간 분배 업데이트", True))
    syn = [(f"2025-{m:02d}-28", 40.0) for m in range(1, 13)] + [(f"2026-{m:02d}-28", 40.0) for m in range(1, 12)]
    rows45 = [x if x[0] != "2025-11-28" else ("2025-11-28", 45.0) for x in syn]
    rows45[-1] = ("2026-11-28", 34.0)
    t34, b34 = one(rows45)
    check("base 45 now 34 (-24%), ttm 474 -> 경계 by YoY", (t34, "같은 달 전년 대비 -24%(전년 45원)" in b34), (f"{TAG} 11월 분배금 34원 | 분배 경계", True))
    rows30 = [x if x[0] != "2025-11-28" else ("2025-11-28", 30.0) for x in syn]
    rows30[-1] = ("2026-11-28", 23.0)
    check("base exactly 30 applies: 23 (-23%) -> 경계", one(rows30)[0], f"{TAG} 11월 분배금 23원 | 분배 경계")
    rows29 = [x if x[0] != "2025-11-28" else ("2025-11-28", 29.0) for x in syn]
    rows29[-1] = ("2026-11-28", 22.0)
    check("base 29 skipped: 22 (-24%), ttm 462 -> 양호", one(rows29)[0], f"{TAG} 11월 분배금 22원 | 분배 양호")
    st = init()
    a = [x[0] for x in evaluate(cfg, st, dict(base, dists=lab + [("2026-11-30", 60.0)], y10=y(4.3, 4.2)))]
    check("Nov 60원 -> ttm 479 양호, with rates low -> 매력", a,
          [f"{TAG} 미 10년물 4.20% | 금리 하향, 매력 상향 검토", f"{TAG} 11월 분배금 60원 | 분배 양호", f"{TAG} 종합 판정 변경 | 보통 -> 매력"])
    st = init()
    st["dist_class"] = "경계"
    check("rates low but dist 경계 -> 편입 보류 not 매력", t(st, y10=y(4.3, 4.2))[-1], f"{TAG} 종합 판정 변경 | 보통 -> 편입 보류")
    st = init()
    check("AUM 799 once, 850 no re-arm, 900 re-arm, 790 again",
          (t(st, etf={"aum_eok": 799, "dev_pct": 0}), t(st, etf={"aum_eok": 850, "dev_pct": 0}), t(st, etf={"aum_eok": 900, "dev_pct": 0}),
           t(st, etf={"aum_eok": 790, "dev_pct": 0})),
          ([f"{TAG} 순자산 799억 | 자금 유출 경계"], [], [], [f"{TAG} 순자산 790억 | 자금 유출 경계"]))
    st = init()
    check("deviation -2.0 once, -1.5 silent, -0.9 re-arm",
          (t(st, etf={"aum_eok": 1000, "dev_pct": -2.0}), t(st, etf={"aum_eok": 1000, "dev_pct": -1.5}), t(st, etf={"aum_eok": 1000, "dev_pct": -0.9}),
           t(st, etf={"aum_eok": 1000, "dev_pct": -2.5})),
          ([f"{TAG} 괴리율 -2.00% | 할인 거래 주의"], [], [], [f"{TAG} 괴리율 -2.50% | 할인 거래 주의"]))
    st = init()
    check("all sources down: silent, verdict kept", (evaluate(cfg, st, {"y10": None, "dists": None, "price": None, "etf": None, "hedge": None}), st["verdict"]),
          ([], "보통"))
    st = init()
    f1 = fail_alerts(cfg, st, {"dists": "x"}, "2026-11-06")
    f1b = fail_alerts(cfg, st, {"dists": "x"}, "2026-11-06")
    f2 = fail_alerts(cfg, st, {"dists": "x"}, "2026-11-09")
    f3 = fail_alerts(cfg, st, {"dists": "x"}, "2026-11-10")
    fail_alerts(cfg, st, {}, "2026-11-11")
    f4 = fail_alerts(cfg, st, {"dists": "x"}, "2026-11-12")
    check("ops only on 2nd failing day, once; reset on recovery", (f1, f1b, [x[0] for x in f2], f3, f4),
          ([], [], [f"{TAG} 분배금 이력 조회 실패 | 2일 연속, 그 규칙 건너뜀"], [], []))
    check("routing: notices port, failure ops", (route_topic("cwally-port", f"{TAG} 11월 분배금 1원 | 분배 경계"), route_topic("cwally-port", f2[0][0])),
          ("cwally-port", "cwally-ops"))
    st = init()
    allt = []
    for kw in ({"y10": y(4.3, 4.2), "dists": lab + [("2026-11-30", 0.0)]}, {"y10": y(5.2, 5.4), "etf": {"aum_eok": 700, "dev_pct": -3}}):
        for ti, tx, _ in evaluate(cfg, init(), dict(base, **kw)):
            allt.append(ti + body("s", tx, "보통"))
    text = " ".join(allt) + json.dumps(cfg, ensure_ascii=False)
    check("titles start [182480], no middle dot or em-dash", (all(a.startswith(TAG) for a in allt), "\u00b7" in text, "\u2014" in text), (True, False, False))
    check("ends 2027-10-31 23:59", cfg["ends_kst"], "2027-10-31 23:59")
    print(f"SELFTEST {'PASS' if not fails else 'FAIL'}: {len(fails)} failed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
