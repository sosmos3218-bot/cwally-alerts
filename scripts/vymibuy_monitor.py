#!/usr/bin/env python3
"""[VYMI] 분할 매수 가격선, 환율 조건 알림 (2026-11-05 요청 pt64u). 미보유, 목표 총자산 5%(약 1,000만원), 상한 7%.

평일 09:00 KST morning 묶음(k182480 다음). 미국 전 거래일 종가 기준, 바뀔 때만 보냄:
  1) 1차 $99.5 / 2차 $96.0 / 3차 $91.0(40/30/30, config 고정값) 종가 이하 -> 매수 문구(환율에 따라 매수 검토 / 환전 분할 / 대기), 주수와 원화 금액
     1차 $102.5, 2차 $99.0 이상에서 재무장, 3차는 1번만. 급락 점검 중이면 3차 보류
  2) 추격 금지: 종가 $108 이상(새 고점), $105 미만 재무장
  3) 급락 점검: 종가 $83.5 이하 2회 연속 -> 재점검, $87.5 이상 2회 연속이면 해제
  4) 환율: 1,320 이하 환전 유리(1,340 위 재무장), 1,450 이상 환율 경계(1,420 아래 재무장)
  5) 새 배당락일 1번: 최근 4회 합 vs 1년 전 4회 합, -10% 넘게 줄면 경계
소스가 실패하면 그 규칙만 건너뜀. 2일 연속 실패면 cwally-ops 1번(회복 뒤 재무장).
기존 scripts/vymi_monitor.py(분기 점검)와 별개. 제목은 모두 "[VYMI] 매수선" 으로 시작해 "[VYMI] 분기 점검" 과 구분.
modes: auto | dry(발송 없음, state 안 씀) | selftest
"""
from __future__ import annotations

import json
import math
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic, route_topic  # noqa: E402
from kr_calendar import KST  # noqa: E402

NAME = "vymibuy"
CFG_PATH = ROOT / "configs" / f"{NAME}.json"
STATE_PATH = ROOT / "state" / f"{NAME}.json"
FX = Path(__file__).resolve().parent / "fixtures" / NAME
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
TAG = "[VYMI]"


def default_state() -> dict:
    return {"updated_at": None, "last_bar_date": None, "armed": {"t1": True, "t2": True, "t3": True},
            "fired": {"t1": False, "t2": False, "t3": False}, "chase_armed": True, "crash_active": False,
            "fx_good_armed": True, "fx_warn_armed": True, "last_div_date": None, "ttm": None, "fail": {}}


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


def parse_yahoo(txt: str) -> tuple[list[tuple[str, float]], list[tuple[str, float]]]:
    """(bars [(YYYY-MM-DD, close)], divs [(ex-date, amount)]) 오래된 순. 날짜는 UTC(미국 장 시작 시각)."""
    res = json.loads(txt)["chart"]["result"][0]
    bars = []
    for t, c in zip(res["timestamp"], res["indicators"]["quote"][0]["close"]):
        if c is not None:
            bars.append((datetime.fromtimestamp(t, timezone.utc).date().isoformat(), float(c)))
    divs = sorted((datetime.fromtimestamp(int(v["date"]), timezone.utc).date().isoformat(), float(v["amount"]))
                  for v in ((res.get("events") or {}).get("dividends") or {}).values())
    if len(bars) < 30:
        raise RuntimeError(f"Yahoo 행 {len(bars)}개")
    return bars, divs


def yahoo(symbol: str, rng: str) -> tuple[list, list]:
    errs = []
    for host in ("query1", "query2"):
        try:
            return parse_yahoo(http(f"https://{host}.finance.yahoo.com/v8/finance/chart/{symbol}?range={rng}&interval=1d&events=div"))
        except Exception as exc:  # noqa: BLE001
            errs.append(f"{host} {exc}")
    raise RuntimeError(" / ".join(errs))


def parse_naver_fx(txt: str, today: str) -> tuple[str, float]:
    rows = [(r["localTradedAt"], float(r["closePrice"].replace(",", ""))) for r in json.loads(txt)["result"]]
    rows = sorted(r for r in rows if r[0] < today)
    if not rows:
        raise RuntimeError("네이버 전 영업일 환율 없음")
    return rows[-1]


def parse_fred(txt: str, today: str) -> tuple[str, float]:
    out = []
    for ln in txt.strip().splitlines()[1:]:
        p = ln.split(",")
        try:
            out.append((p[0], float(p[1])))
        except (ValueError, IndexError):
            continue
    out = [r for r in out if r[0] < today]
    if not out:
        raise RuntimeError("FRED DEXKOUS 값 없음")
    return out[-1]


def usdkrw(today: date) -> tuple[str, float, str]:
    t = today.isoformat()
    errs = []
    try:
        d, v = parse_naver_fx(http("https://m.stock.naver.com/front-api/marketIndex/prices?category=exchange&reutersCode=FX_USDKRW&page=1"), t)
        return d, v, "서울 종가(네이버)"
    except Exception as exc:  # noqa: BLE001
        errs.append(f"naver {exc}")
    try:
        bars, _ = yahoo("KRW=X", "1mo")
        bars = [b for b in bars if b[0] < t]
        return bars[-1][0], bars[-1][1], "Yahoo KRW=X"
    except Exception as exc:  # noqa: BLE001
        errs.append(f"yahoo {exc}")
    try:
        from fred_fetch import fetch_fred_csv
        d, v = parse_fred(fetch_fred_csv("DEXKOUS", (today - timedelta(days=30)).isoformat()), t)
        return d, v, "FRED DEXKOUS"
    except Exception as exc:  # noqa: BLE001
        errs.append(f"fred {exc}")
    raise RuntimeError(" / ".join(errs))


# ---------------- 계산 ----------------
def sma(closes: list[float], n: int) -> float | None:
    return sum(closes[-n:]) / n if len(closes) >= n else None


def fx_phrase(fx: float | None, r: dict) -> str:
    if fx is None:
        return "매수 검토(환율 확인 못 함, 직접 확인)"
    if fx <= r["fx_ok_max"]:
        return "매수 검토"
    if fx < r["fx_split_max"]:
        return "매수 검토, 환전 분할"
    return "대기(환율 고점권)"


def tranche_cost(tr: dict, price: float, fx: float | None, r: dict) -> str:
    krw = r["target_krw"] * tr["pct"] / 100
    if not fx:
        return f"배정 {krw / 1e4:,.0f}만원(환율 없음, 주수 계산 안 함)"
    sh = math.floor(krw / (price * fx))
    return f"배정 {krw / 1e4:,.0f}만원 = 약 {sh}주 x ${price:.2f} x {fx:,.1f}원 = {sh * price * fx / 1e4:,.0f}만원(수수료 별도)"


def ttm_at(divs: list, idx: int) -> float | None:
    return sum(v for _, v in divs[idx - 3: idx + 1]) if idx >= 3 else None


def evaluate(cfg: dict, st: dict, obs: dict) -> list[tuple[str, str, int]]:
    """obs: bars [(date, close)] | None, divs [(date, amt)] | None, fx (date, v, src) | None."""
    r = cfg["rules"]
    out: list[tuple[str, str, int]] = []
    fxo = obs.get("fx")
    fx = fxo[1] if fxo else None
    fx_txt = f"USD/KRW {fx:,.1f}({fxo[0]}, {fxo[2]})" if fxo else "USD/KRW 확인 못 함"
    bars = obs.get("bars")
    if bars and (st.get("last_bar_date") is None or bars[-1][0] > st["last_bar_date"]):
        d, c = bars[-1]
        closes = [x for _, x in bars]
        m50, m200 = sma(closes, 50), sma(closes, 200)
        hi = max(r["high_ref"], max(closes[-252:]))
        ctx = (f"종가 ${c:.2f}({d}), 종가 고점 ${hi:.2f} 대비 {(c / hi - 1) * 100:+.1f}%"
               + (f", 50DMA ${m50:.2f}" if m50 else "") + (f", 200DMA ${m200:.2f}" if m200 else "") + f", {fx_txt}.")
        crash_n = r["crash_consec"]
        last_n = closes[-crash_n:]
        if not st["crash_active"] and len(last_n) == crash_n and all(x <= r["crash_line"] for x in last_n):
            st["crash_active"] = True
            out.append((f"{TAG} 매수선 종가 ${c:.2f} | 급락 재점검, 3차 보류",
                        f"종가가 ${r['crash_line']:.1f}(고점 -22%) 이하로 {crash_n}번 연속. {ctx} 지수 전체 급락인지 VYMI만의 문제(배당 삭감, 특정 국가)인지 확인 전까지 3차 보류. "
                        f"${r['crash_clear']:.1f} 이상 {r['crash_clear_consec']}번 연속이면 해제.", 4))
        elif st["crash_active"]:
            cl = closes[-r["crash_clear_consec"]:]
            if len(cl) == r["crash_clear_consec"] and all(x >= r["crash_clear"] for x in cl):
                st["crash_active"] = False
                out.append((f"{TAG} 매수선 종가 ${c:.2f} | 급락 재점검 해제",
                            f"${r['crash_clear']:.1f} 이상 {r['crash_clear_consec']}번 연속. {ctx} 3차 보류 풀림.", 3))
        for tr in r["tranches"]:
            k = tr["key"]
            if c <= tr["line"] and st["armed"][k]:
                if k == "t3" and st["crash_active"]:
                    continue
                st["armed"][k] = False
                st["fired"][k] = True
                ph = fx_phrase(fx, r)
                rearm = f"${tr['rearm']:.1f} 이상에서 다시 무장" if tr["rearm"] else "1번만"
                out.append((f"{TAG} 매수선 {tr['label']} ${tr['line']:.1f} | 종가 ${c:.2f}, {ph}",
                            f"{tr['label']}({tr['pct']}%) 선 ${tr['line']:.1f} 이하. {ctx} {tranche_cost(tr, c, fx, r)}. "
                            f"환율 기준 1,400 이하 매수 검토, 1,400~1,450 환전 분할, 1,450 이상 대기. 일반 해외주식 계좌, 지정가. {rearm}.",
                            3 if ph.startswith("대기") else 4))
            elif tr["rearm"] and not st["armed"][k] and c >= tr["rearm"]:
                st["armed"][k] = True
        if c >= r["chase_line"] and st["chase_armed"]:
            st["chase_armed"] = False
            out.append((f"{TAG} 매수선 종가 ${c:.2f} | 새 고점, 추격 금지",
                        f"종가 ${r['chase_line']:.0f} 이상(새 고점권). {ctx} 매수선까지 기다리기. ${r['chase_rearm']:.0f} 미만이면 재무장.", 3))
        elif c < r["chase_rearm"]:
            st["chase_armed"] = True
        st["last_bar_date"] = d
    if fx is not None:
        if fx <= r["fx_good"] and st["fx_good_armed"]:
            st["fx_good_armed"] = False
            out.append((f"{TAG} 매수선 USD/KRW {fx:,.1f} | 환전 유리, 달러 환전 검토",
                        f"{fx_txt}, {r['fx_good']:,} 이하(52주 저점 약 1,323.5 근처). VYMI 매수선 전이라도 배정 금액 일부 미리 환전 검토. {r['fx_good_rearm']:,} 위에서 재무장.", 3))
        elif fx > r["fx_good_rearm"]:
            st["fx_good_armed"] = True
        if fx >= r["fx_warn"] and st["fx_warn_armed"]:
            st["fx_warn_armed"] = False
            out.append((f"{TAG} 매수선 USD/KRW {fx:,.1f} | 환율 경계",
                        f"{fx_txt}, {r['fx_warn']:,} 이상(2026 고점 약 1,550~1,559). 매수선이 와도 대기 문구. {r['fx_warn_rearm']:,} 아래에서 재무장.", 3))
        elif fx < r["fx_warn_rearm"]:
            st["fx_warn_armed"] = True
    divs = obs.get("divs")
    if divs and (st.get("last_div_date") is None or divs[-1][0] > st["last_div_date"]):
        idx = len(divs) - 1
        ttm, prev = ttm_at(divs, idx), ttm_at(divs, idx - 4)
        if ttm is not None:
            yoy = (ttm / prev - 1) * 100 if prev else None
            warn = yoy is not None and yoy < r["ttm_yoy_warn_pct"]
            px = bars[-1][1] if bars else None
            yld = f", 분배율 {ttm / px * 100:.2f}%(종가 ${px:.2f})" if px else ""
            yt = f"1년 전 4회 합 ${prev:.3f} 대비 {yoy:+.1f}%" if yoy is not None else "1년 전 비교 값 없음"
            out.append((f"{TAG} 매수선 분배 ${divs[-1][1]:.3f}(배당락 {divs[-1][0]}) | {'분배 경계' if warn else '분배 업데이트'}",
                        f"최근 4회 합 ${ttm:.3f}, {yt}{yld}. 기준 4회 합이 1년 전보다 {abs(r['ttm_yoy_warn_pct']):.0f}% 넘게 줄면 경계"
                        + (", 분할 매수 속도 늦추고 배당 삭감 원인 확인." if warn else "."), 4 if warn else 2))
            st.update(last_div_date=divs[-1][0], ttm=round(ttm, 4))
    return out


def body(stamp: str, text: str) -> str:
    return "\n".join([
        f"{stamp} | VYMI | 미국 상장 해외 고배당 ETF",
        "이게 뭐 알림: VYMI 미보유, 목표 총자산 5%(약 1,000만원, 상한 7%) 3회 분할(40/30/30) 매수선, 환율 조건이 바뀔 때만 오는 알림. 매매 지시 아님.",
        f"지금 결과/상태: {text}",
        "하지 말 것: 한 번에 전액 매수, 상한 7% 넘기기, ISA/연금 계좌로 시도, 시장가 주문, 환율 고점권 일괄 환전.",
    ])


# ---------------- 실행 ----------------
def gather(today: date) -> tuple[dict, dict]:
    obs: dict = {"bars": None, "divs": None, "fx": None}
    errs: dict = {}
    try:
        bars, divs = yahoo("VYMI", "2y")
        obs["bars"] = [b for b in bars if b[0] < today.isoformat()]
        obs["divs"] = divs
    except Exception as exc:  # noqa: BLE001
        errs["price"] = str(exc)[:200]
    try:
        obs["fx"] = usdkrw(today)
    except Exception as exc:  # noqa: BLE001
        errs["fx"] = str(exc)[:200]
    return obs, errs


def fail_alerts(cfg: dict, st: dict, errs: dict, today: str) -> list[tuple[str, str, int]]:
    out = []
    names = {"price": "VYMI 가격", "fx": "USD/KRW 환율"}
    fails = st.setdefault("fail", {})
    for key in names:
        f = fails.get(key) or {"days": 0, "last": None, "notified": False}
        if key in errs:
            if f["last"] != today:
                f["days"] += 1
                f["last"] = today
            if f["days"] >= cfg["rules"]["fail_ops_after_days"] and not f["notified"]:
                f["notified"] = True
                out.append((f"{TAG} 매수선 {names[key]} 조회 실패 | {f['days']}일 연속, 그 규칙 건너뜀",
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
    obs, errs = gather(now.date())
    if errs:
        print("errors: " + json.dumps(errs, ensure_ascii=False), file=sys.stderr)
    closes = [x for _, x in obs["bars"] or []]
    print("obs: " + json.dumps({"bar": (obs["bars"] or [None])[-1], "sma50": sma(closes, 50), "sma200": sma(closes, 200),
                                "fx": obs["fx"], "div_last": (obs["divs"] or [None])[-1]}, ensure_ascii=False))
    alerts = evaluate(cfg, st, obs) + fail_alerts(cfg, st, errs, now.date().isoformat())
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    for title, text, pri in alerts:
        print(f"TITLE: {title}\nPRIORITY: {pri} TOPIC: {route_topic(topic, title)}\n{body(stamp, text)}\n")
        if not dry:
            publish(topic, title, body(stamp, text), pri, ["office"], server)
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
    r = cfg["rules"]
    fails: list[str] = []

    def check(name, got, want):
        ok = got == want
        print(("ok   " if ok else "FAIL ") + f"{name}: got={got!r} want={want!r}")
        if not ok:
            fails.append(name)
    bars, divs = parse_yahoo((FX / "yahoo_vymi.json").read_text(encoding="utf-8"))
    closes = [x for _, x in bars]
    check("fixture last bar 11/4 103.32", (bars[-1][0], round(bars[-1][1], 2)), ("2026-11-04", 103.32))
    check("50DMA 102.88, 200DMA 99.93, 252 closing high 107.13, low 84.63",
          (round(sma(closes, 50), 2), round(sma(closes, 200), 2), round(max(closes[-252:]), 2), round(min(closes[-252:]), 2)),
          (102.88, 99.93, 107.13, 84.63))
    check("TTM 3.721, prior 3.341, yield 3.60%", (round(ttm_at(divs, len(divs) - 1), 3), round(ttm_at(divs, len(divs) - 5), 3),
                                                  round(ttm_at(divs, len(divs) - 1) / 103.32 * 100, 2)), (3.721, 3.341, 3.6))
    check("fixed lines + re-arm from config", [(x["line"], x["rearm"]) for x in r["tranches"]] + [r["crash_line"]],
          [(99.5, 102.5), (96.0, 99.0), (91.0, None), 83.5])
    check("lines are pullbacks below 50DMA/200DMA", (99.5 < sma(closes, 200) < sma(closes, 50), round((99.5 / r["high_ref"] - 1) * 100, 1)), (True, -7.1))
    check("naver fx prior Seoul close (today 11/05 excluded)", parse_naver_fx((FX / "naver_usdkrw.json").read_text(encoding="utf-8"), "2026-11-05"),
          ("2026-11-04", 1342.5))
    kb, _ = parse_yahoo((FX / "yahoo_krw.json").read_text(encoding="utf-8"))
    check("yahoo KRW=X fallback parses", round(kb[-1][1]), 1341)
    check("FRED DEXKOUS fallback parses", parse_fred((FX / "fred_dexkous.csv").read_text(encoding="utf-8"), "2026-11-05")[0][:7], "2026-10")
    check("fx phrase 1400 / 1420 / 1450", (fx_phrase(1400, r), fx_phrase(1420, r), fx_phrase(1450, r)),
          ("매수 검토", "매수 검토, 환전 분할", "대기(환율 고점권)"))
    check("tranche cost 1차 at 99.5 x 1342.5", tranche_cost(r["tranches"][0], 99.5, 1342.5, r),
          "배정 400만원 = 약 29주 x $99.50 x 1,342.5원 = 387만원(수수료 별도)")
    state_file = json.loads(STATE_PATH.read_text(encoding="utf-8"))

    def init():
        s = default_state()
        s.update(json.loads(json.dumps(state_file)))
        return s
    fx0 = ("2026-11-04", 1342.5, "t")
    base = {"bars": bars, "divs": divs, "fx": fx0}
    st = init()
    check("initial state with current values: silent", evaluate(cfg, st, dict(base)), [])
    bdate = [date(2026, 11, 5)]

    def bar(c):
        bdate[0] += timedelta(days=1)
        return bars + [(bdate[0].isoformat(), c)]

    def t(st, c, fx=1342.5, **kw):
        nonlocal_bars = bar(c) if c is not None else bars
        return [a[0] for a in evaluate(cfg, st, dict(base, bars=nonlocal_bars, fx=(bdate[0].isoformat(), fx, "t") if fx else None, **kw))]
    st = init()
    check("99.6 nothing (was a 1차 hit under old formula 102.5)", (t(st, 102.0), t(st, 99.6)), ([], []))
    check("99.5 -> 1차 매수 검토", t(st, 99.5), [f"{TAG} 매수선 1차 $99.5 | 종가 $99.50, 매수 검토"])
    check("same bar again: silent", evaluate(cfg, st, dict(base, bars=bars + [(bdate[0].isoformat(), 99.5)])), [])
    check("98 still disarmed", t(st, 98.0), [])
    check("102.4 no re-arm", (t(st, 102.4), st["armed"]["t1"]), ([], False))
    check("102.5 re-arms", (t(st, 102.5), st["armed"]["t1"]), ([], True))
    check("fires again at 99", t(st, 99.0), [f"{TAG} 매수선 1차 $99.5 | 종가 $99.00, 매수 검토"])
    st = init()
    check("1420 -> 환전 분할 wording", t(st, 99.4, fx=1420), [f"{TAG} 매수선 1차 $99.5 | 종가 $99.40, 매수 검토, 환전 분할"])
    st = init()
    a = t(st, 95.5, fx=1460)
    check("gap to 95.5 with fx 1460: 1차+2차 대기, fx 경계", a,
          [f"{TAG} 매수선 1차 $99.5 | 종가 $95.50, 대기(환율 고점권)", f"{TAG} 매수선 2차 $96.0 | 종가 $95.50, 대기(환율 고점권)",
           f"{TAG} 매수선 USD/KRW 1,460.0 | 환율 경계"])
    check("2차 re-arm at 99.0", (t(st, 98.9, fx=1440), st["armed"]["t2"], t(st, 99.0, fx=1430), st["armed"]["t2"]),
          ([], False, [], True))
    check("fx warn re-arm only < 1420", (t(st, None, fx=1425), st["fx_warn_armed"], t(st, None, fx=1419), st["fx_warn_armed"]), ([], False, [], True))
    st = init()
    a = t(st, 91.0)
    check("91 -> 1차 2차 3차", a, [f"{TAG} 매수선 {n} ${p} | 종가 $91.00, 매수 검토" for n, p in (("1차", "99.5"), ("2차", "96.0"), ("3차", "91.0"))])
    check("3차 once: 102.5 then 90 -> only 1차, 2차", (t(st, 102.5), t(st, 90.0)),
          ([], [f"{TAG} 매수선 1차 $99.5 | 종가 $90.00, 매수 검토", f"{TAG} 매수선 2차 $96.0 | 종가 $90.00, 매수 검토"]))
    st = init()
    st["armed"].update(t1=False, t2=False)
    check("crash 1 close 83.5: no 재점검, 3차 fires (above crash rule needs 2)", t(st, 83.5)[-1], f"{TAG} 매수선 3차 $91.0 | 종가 $83.50, 매수 검토")
    st = init()
    st["armed"].update(t1=False, t2=False)
    seq = bars + [("2026-11-06", 83.4)]
    evaluate(cfg, st, dict(base, bars=seq[:-1] + [("2026-11-05", 103.0)]))
    st["armed"].update(t1=False, t2=False, t3=True)
    st["last_bar_date"] = "2026-11-05"
    seq2 = bars + [("2026-11-05", 83.4), ("2026-11-06", 83.0)]
    check("2 closes <= 83.5 -> 재점검, 3차 paused", [a[0] for a in evaluate(cfg, st, dict(base, bars=seq2))], [f"{TAG} 매수선 종가 $83.00 | 급락 재점검, 3차 보류"])
    check("3차 still armed while paused", (st["armed"]["t3"], st["crash_active"]), (True, True))
    seq3 = seq2 + [("2026-11-09", 88.0)]
    check("one close 88 not clear", [a[0] for a in evaluate(cfg, st, dict(base, bars=seq3))], [])
    seq4 = seq3 + [("2026-11-10", 89.0)]
    check("2 closes >= 87.5 clear -> 3차 then fires (89 <= 91)", [a[0] for a in evaluate(cfg, st, dict(base, bars=seq4))],
          [f"{TAG} 매수선 종가 $89.00 | 급락 재점검 해제", f"{TAG} 매수선 3차 $91.0 | 종가 $89.00, 매수 검토"])
    st = init()
    check("chase 108 once, 106 no re-arm, 104.9 re-arm, 108.2 again", (t(st, 108.0), t(st, 106.0), t(st, 104.9), t(st, 108.2)),
          ([f"{TAG} 매수선 종가 $108.00 | 새 고점, 추격 금지"], [], [], [f"{TAG} 매수선 종가 $108.20 | 새 고점, 추격 금지"]))
    st = init()
    check("fx 1320 good once, 1335 no re-arm, 1341 re-arm, 1318 again",
          (t(st, None, fx=1320), t(st, None, fx=1335), t(st, None, fx=1341), t(st, None, fx=1318)),
          ([f"{TAG} 매수선 USD/KRW 1,320.0 | 환전 유리, 달러 환전 검토"], [], [], [f"{TAG} 매수선 USD/KRW 1,318.0 | 환전 유리, 달러 환전 검토"]))
    st = init()
    nd = divs + [("2026-12-18", 0.95)]
    a = evaluate(cfg, st, dict(base, divs=nd))
    check("new ex-date 12/18 0.95: TTM 3.732 vs 3.315 update p2", ([x[0] for x in a], a[0][2], "+12.6%" in a[0][1], "분배율 3.61%" in a[0][1]),
          ([f"{TAG} 매수선 분배 $0.950(배당락 2026-12-18) | 분배 업데이트"], 2, True, True))
    check("same ex-date not repeated", evaluate(cfg, st, dict(base, divs=nd)), [])
    st = init()
    a = evaluate(cfg, st, dict(base, divs=divs + [("2026-12-18", 0.15)]))
    check("12/18 0.15: TTM 2.932 vs 3.315 (-11.6%) -> 분배 경계 p4", ([x[0] for x in a], a[0][2]),
          ([f"{TAG} 매수선 분배 $0.150(배당락 2026-12-18) | 분배 경계"], 4))
    st = init()
    a = evaluate(cfg, st, dict(base, divs=divs + [("2026-12-18", 0.25)]))
    check("12/18 0.25: -8.5% -> not 경계", a[0][0], f"{TAG} 매수선 분배 $0.250(배당락 2026-12-18) | 분배 업데이트")
    st = init()
    check("fx missing: tranche still fires with fallback wording", t(st, 99.0, fx=None), [f"{TAG} 매수선 1차 $99.5 | 종가 $99.00, 매수 검토(환율 확인 못 함, 직접 확인)"])
    st = init()
    check("all sources down: silent, state kept", (evaluate(cfg, st, {"bars": None, "divs": None, "fx": None}), st["last_bar_date"]), ([], state_file["last_bar_date"]))
    st = init()
    f1 = fail_alerts(cfg, st, {"fx": "x"}, "2026-11-06")
    f1b = fail_alerts(cfg, st, {"fx": "x"}, "2026-11-06")
    f2 = fail_alerts(cfg, st, {"fx": "x"}, "2026-11-09")
    f3 = fail_alerts(cfg, st, {"fx": "x"}, "2026-11-10")
    fail_alerts(cfg, st, {}, "2026-11-11")
    f4 = fail_alerts(cfg, st, {"fx": "x"}, "2026-11-12")
    check("ops only on 2nd failing day, once; reset on recovery", (f1, f1b, [x[0] for x in f2], f3, f4),
          ([], [], [f"{TAG} 매수선 USD/KRW 환율 조회 실패 | 2일 연속, 그 규칙 건너뜀"], [], []))
    check("routing: notices port, failure ops", (route_topic("cwally-port", f"{TAG} 매수선 1차 $99.5 | 종가 $99.00, 매수 검토"), route_topic("cwally-port", f2[0][0])),
          ("cwally-port", "cwally-ops"))
    allt = []
    for c, fx in ((91.0, 1460), (108.0, 1310), (99.0, 1420)):
        s = init()
        for ti, tx, _ in t(s, c, fx=fx) and evaluate(cfg, init(), dict(base, bars=bar(c), fx=("d", fx, "t"))):
            allt.append(ti + body("s", tx))
    text = " ".join(allt) + json.dumps(cfg, ensure_ascii=False) + Path(__file__).read_text(encoding="utf-8")
    check("titles start [VYMI] 매수선, distinct from 분기 점검, no middle dot or em-dash", (len(allt) > 5, all(a.startswith(TAG + " 매수선 ") and "분기 점검" not in a.split("\n")[0] for a in allt), "\u00b7" in text, "\u2014" in text),
          (True, True, False, False))
    check("ends 2027-10-31 23:59", cfg["ends_kst"], "2027-10-31 23:59")
    print(f"SELFTEST {'PASS' if not fails else 'FAIL'}: {len(fails)} failed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
