#!/usr/bin/env python3
"""[NIX1102] 2026-11-02 장 마감 후 SK하이닉스, 삼성전자 점검 1회.

점검 3가지: 일봉 지지(10/30 저가 기준), 외국인 확정 순매수 주식 수, SK하이닉스 ADR 괴리.
판정: 1차 분할 진입 검토 / 관망 / 진입 보류 (규칙은 configs/nix1102.json rules).

modes:
  auto      one-shot 실행. 자료가 다 있으면 1번 발송, 아니면 final 시각 전까지 대기.
  force     final 처럼 있는 만큼으로 판정 (수동 재시도용, 이미 보냈으면 침묵).
  dry       실제 조회 + 판정 출력. ntfy 없음, state 쓰기 없음.
  selftest  가짜 자료로 판정과 문구 점검. 네트워크, ntfy, state 없음.
"""
from __future__ import annotations

import json
import statistics
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server  # noqa: E402

KST = timezone(timedelta(hours=9))
CFG_PATH = ROOT / "configs" / "nix1102.json"
STATE_PATH = ROOT / "state" / "nix1102.json"
YAHOO_UA = {"User-Agent": "Mozilla/5.0 cwally-alerts"}


# ---------------- 공통 ----------------
def load_cfg() -> dict:
    return json.loads(CFG_PATH.read_text(encoding="utf-8"))


def load_state() -> dict:
    st = {"sent": None, "attempts": [], "ops_sent": None}
    if STATE_PATH.exists():
        try:
            st.update(json.loads(STATE_PATH.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass
    return st


def save_state(st: dict) -> None:
    STATE_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def norm_day(raw) -> str:
    digits = "".join(ch for ch in str(raw or "") if ch.isdigit())[:8]
    return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}" if len(digits) == 8 else ""


def won(v: float | None) -> str:
    return "미확인" if v is None else f"{v:,.0f}원"


def shares(v: float | None) -> str:
    if v is None:
        return "미확인"
    sign = "+" if v > 0 else ""
    return f"{sign}{v:,.0f}주"


def eok(v: float | None) -> str:
    if v is None:
        return "미확인"
    sign = "+" if v > 0 else ""
    return f"{sign}{v / 1e8:,.0f}억원"


def pct(v: float | None, digits: int = 2) -> str:
    if v is None:
        return "미확인"
    sign = "+" if v > 0 else ""
    return f"{sign}{v:.{digits}f}%"


# ---------------- 조회 ----------------
DAUM_DAYS = "https://finance.daum.net/api/quote/A{code}/days?symbolCode=A{code}&page={page}&perPage=100&pagination=true"
KRX_CLOSE_HM = (15, 40)


def daum_daily(code: str, now: datetime, min_rows: int = 61, max_pages: int = 3) -> dict[str, dict]:
    """{YYYY-MM-DD: {open, high, low, close}} Daum KRX 정규장 일별 시세 (NXT 제외, 네이버 일별은 NXT 섞임이라 안 씀).
    오늘 행은 KST 15:40 이후에만 확정으로 받는다."""
    today = now.strftime("%Y-%m-%d")
    after_close = (now.hour, now.minute) >= KRX_CLOSE_HM
    headers = {"User-Agent": "Mozilla/5.0", "Referer": f"https://finance.daum.net/quotes/A{code}"}
    out: dict[str, dict] = {}
    for page in range(1, max_pages + 1):
        req = urllib.request.Request(DAUM_DAYS.format(code=code, page=page), headers=headers)
        with urllib.request.urlopen(req, timeout=25) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
        rows = raw.get("data") or []
        for r in rows:
            if str(r.get("symbolCode")) != "A" + code:
                raise ValueError("종목 코드 불일치")
            d = str(r.get("date") or "")[:10]
            if len(d) != 10 or d > today or (d == today and not after_close):
                continue
            row = {"open": r.get("openingPrice"), "high": r.get("highPrice"), "low": r.get("lowPrice"), "close": r.get("tradePrice")}
            if all(row[k] for k in row):
                out[d] = {k: float(v) for k, v in row.items()}
        if len(out) >= min_rows or not rows:
            break
    if not out:
        raise ValueError("KRX 일별 시세 없음")
    return dict(sorted(out.items()))


def naver_foreign(code: str) -> dict[str, float | None]:
    """{YYYY-MM-DD: 외국인 순매수 주식 수}."""
    import naver_api

    out: dict[str, float | None] = {}
    for r in naver_api.stock_trend(code, 10):
        d = norm_day(r.get("day"))
        if d:
            out[d] = r.get("foreign")
    return out


def yahoo_closes(ticker: str) -> list[tuple[str, float]]:
    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        + urllib.parse.quote(ticker)
        + "?interval=1d&range=3mo"
    )
    req = urllib.request.Request(url, headers=YAHOO_UA)
    with urllib.request.urlopen(req, timeout=20) as resp:
        payload = json.loads(resp.read().decode())
    result = payload["chart"]["result"][0]
    offset = int((result.get("meta") or {}).get("gmtoffset") or 0)
    closes = result["indicators"]["quote"][0]["close"]
    bars = []
    for ts, close in zip(result.get("timestamp") or [], closes):
        if close is None:
            continue
        day = datetime.fromtimestamp(int(ts) + offset, tz=timezone.utc).strftime("%Y-%m-%d")
        bars.append((day, float(close)))
    if not bars:
        raise RuntimeError(f"no closes {ticker}")
    return bars


def fetch_all(cfg: dict, now: datetime) -> dict:
    data: dict = {"stocks": {}, "adr": None, "fx": None, "errors": []}
    for s in cfg["stocks"]:
        code = s["code"]
        item: dict = {"daily": {}, "foreign": {}}
        try:
            item["daily"] = daum_daily(code, now)
        except Exception as exc:  # noqa: BLE001
            data["errors"].append(f"{s['name']} 일별 시세: {exc}")
        try:
            item["foreign"] = naver_foreign(code)
        except Exception as exc:  # noqa: BLE001
            data["errors"].append(f"{s['name']} 투자자 동향: {exc}")
        data["stocks"][code] = item
    try:
        data["adr"] = yahoo_closes(cfg["adr"]["ticker"])
    except Exception as exc:  # noqa: BLE001
        data["errors"].append(f"ADR {cfg['adr']['ticker']}: {exc}")
    try:
        data["fx"] = yahoo_closes(cfg["adr"]["fx_ticker"])
    except Exception as exc:  # noqa: BLE001
        data["errors"].append(f"원달러 {cfg['adr']['fx_ticker']}: {exc}")
    return data


# ---------------- 판정 ----------------
def sma(closes: list[float], n: int) -> float | None:
    return sum(closes[-n:]) / n if len(closes) >= n else None


def support_state(t: dict, ref_low: float) -> str:
    if t["close"] < ref_low:
        return "지지 이탈"
    mid = (t["high"] + t["low"]) / 2
    return "지지 확인" if t["close"] >= mid else "지지 시험 중"


def foreign_state(f_t: float | None, f_r: float | None, ratio: float) -> tuple[str, bool]:
    if f_t is None:
        return "외국인 미확인", False
    if f_t >= 0:
        return "외국인 순매수", True
    if f_r is not None and f_r < 0 and abs(f_t) <= ratio * abs(f_r):
        return f"외국인 순매도 축소(10/30의 {abs(f_t) / abs(f_r) * 100:.0f}%)", True
    if f_r is not None and f_r < 0:
        return f"외국인 순매도 지속(10/30의 {abs(f_t) / abs(f_r) * 100:.0f}%)", False
    return "외국인 순매도 전환", False


def latest_before(bars: list[tuple[str, float]], day: str, inclusive: bool) -> tuple[str, float] | None:
    pick = None
    for d, v in bars:
        if d < day or (inclusive and d == day):
            pick = (d, v)
    return pick


def adr_gap(cfg: dict, daily: dict[str, dict], adr: list | None, fx: list | None, target: str) -> dict:
    out = {"dev_pct": None, "adr_day": None, "adr_ret_pct": None, "krx_ret_pct": None, "fx_ret_pct": None, "points": 0, "why": ""}
    if not adr or not fx:
        out["why"] = "ADR 또는 원달러 자료 없음"
        return out
    days = [d for d in daily if d <= target]
    if target not in daily:
        out["why"] = "11/2 국내 종가 없음"
        return out
    ratios: dict[str, float] = {}
    for d in days[-(int(cfg["adr"]["median_days"]) + 1):]:
        a = latest_before(adr, d, inclusive=False)
        f = latest_before(fx, d, inclusive=True)
        if a and f and a[1] > 0 and f[1] > 0:
            ratios[d] = daily[d]["close"] / (a[1] * f[1])
    hist = [v for d, v in ratios.items() if d < target]
    out["points"] = len(hist)
    if target not in ratios or len(hist) < int(cfg["adr"]["min_points"]):
        out["why"] = f"비교 가능한 날 {len(hist)}일, 기준 미달"
        return out
    med = statistics.median(hist)
    out["dev_pct"] = (ratios[target] / med - 1) * 100
    a_last = latest_before(adr, target, inclusive=False)
    a_prev = latest_before(adr, a_last[0], inclusive=False) if a_last else None
    if a_last and a_prev:
        out["adr_day"] = a_last[0]
        out["adr_ret_pct"] = (a_last[1] / a_prev[1] - 1) * 100
    prev_days = [d for d in days if d < target]
    if prev_days:
        out["krx_ret_pct"] = (daily[target]["close"] / daily[prev_days[-1]]["close"] - 1) * 100
        f_t = latest_before(fx, target, inclusive=True)
        f_p = latest_before(fx, prev_days[-1], inclusive=True)
        if f_t and f_p:
            out["fx_ret_pct"] = (f_t[1] / f_p[1] - 1) * 100
    return out


def judge_stock(cfg: dict, s: dict, item: dict, data: dict) -> dict:
    target, ref = cfg["target_day"], cfg["ref_day"]
    daily, foreign = item["daily"], item["foreign"]
    res: dict = {"code": s["code"], "name": s["name"], "short": s["short"], "missing": [], "next_support": s["next_support"]}
    t, r = daily.get(target), daily.get(ref)
    res["t"], res["r"] = t, r
    if not t:
        res["missing"].append("11/2 일봉")
    if not r:
        res["missing"].append("10/30 일봉")
    closes = [v["close"] for d, v in daily.items() if d <= target]
    res["sma20"], res["sma60"] = sma(closes, 20), sma(closes, 60)
    res["support"] = support_state(t, r["low"]) if t and r else "판정 불가"
    f_t, f_r = foreign.get(target), foreign.get(ref)
    res["f_t"], res["f_r"] = f_t, f_r
    if f_t is None:
        res["missing"].append("11/2 외국인 수급")
    res["foreign_txt"], foreign_ok = foreign_state(f_t, f_r, float(cfg["rules"]["foreign_shrink_ratio"]))
    res["f_amt"] = f_t * t["close"] if (f_t is not None and t) else None
    adr_ok = True
    res["adr"] = None
    if s.get("adr"):
        res["adr"] = adr_gap(cfg, daily, data.get("adr"), data.get("fx"), target)
        dev = res["adr"]["dev_pct"]
        if dev is None:
            res["missing"].append("ADR 괴리")
            adr_ok = False
        else:
            adr_ok = dev <= float(cfg["adr"]["premium_max_pct"])
    if res["support"] == "지지 이탈":
        verdict = "진입 보류"
    elif res["support"] == "지지 확인" and foreign_ok and adr_ok and not res["missing"]:
        verdict = "1차 분할 진입 검토"
    else:
        verdict = "관망"
    res["verdict"] = verdict
    res["adr_ok"] = adr_ok
    res["foreign_ok"] = foreign_ok
    return res


# ---------------- 문구 ----------------
def stock_lines(res: dict, cfg: dict) -> list[str]:
    t, r = res["t"], res["r"]
    out = [f"■ {res['name']}: {res['verdict']}"]
    if t and r:
        chg = (t["close"] / r["close"] - 1) * 100
        out.append(
            f"- 일봉: 종가 {won(t['close'])} ({pct(chg)}), 고가 {won(t['high'])}, 저가 {won(t['low'])}. "
            f"기준 저가(10/30) {won(r['low'])} -> {res['support']}"
        )
        ma = []
        if res["sma20"]:
            ma.append(f"20일선 {won(res['sma20'])} {'위' if t['close'] >= res['sma20'] else '아래'}")
        if res["sma60"]:
            ma.append(f"60일선 {won(res['sma60'])} {'위' if t['close'] >= res['sma60'] else '아래'}")
        if ma:
            out.append("- 이동평균: " + ", ".join(ma))
    else:
        out.append(f"- 일봉: {', '.join(m for m in res['missing'] if '일봉' in m)} 없음")
    out.append(
        f"- 외국인: 11/2 {shares(res['f_t'])} (종가 환산 {eok(res['f_amt'])}), 10/30 {shares(res['f_r'])} -> {res['foreign_txt']}"
    )
    a = res.get("adr")
    if a is not None:
        if a["dev_pct"] is None:
            out.append(f"- ADR 괴리: 미확인 ({a['why']})")
        else:
            side = "국내가 ADR보다 비싸게 마감" if a["dev_pct"] > 0 else "국내가 ADR보다 싸게 마감"
            out.append(
                f"- ADR 괴리: 20일 중앙값 대비 {pct(a['dev_pct'])} ({side}, 기준 +{cfg['adr']['premium_max_pct']:.1f}% 이하"
                f" {'통과' if res['adr_ok'] else '초과'}). ADR {a['adr_day'] or '-'} 등락 {pct(a['adr_ret_pct'])}, "
                f"국내 11/2 {pct(a['krx_ret_pct'])}, 원달러 {pct(a['fx_ret_pct'])}"
            )
    if res["missing"]:
        out.append("- 미확인 항목: " + ", ".join(res["missing"]) + " (추정 안 함)")
    return out


def build_message(cfg: dict, results: list[dict], now: datetime) -> tuple[str, str, int]:
    verdicts = {r["code"]: r["verdict"] for r in results}
    title = cfg["title_prefix"] + " 000660/005930 " + " | ".join(f"{r['short']}({r['code']}) {r['verdict']}" for r in results)
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    do_now = []
    later = []
    cut = []
    for r in results:
        if r["verdict"] == "1차 분할 진입 검토":
            do_now.append(f"{r['short']}: 계획 비중의 1/3까지만 1차 분할 진입 검토.")
            cut.append(f"{r['short']}: 10/30 저가 {won(r['r']['low'])} 종가 이탈 시 1차분 정리 검토, 추가 진입 중단.")
        elif r["verdict"] == "진입 보류":
            do_now.append(f"{r['short']}: 진입 보류. 다음 지지 {won(r['next_support'])} 종가 확인까지 대기.")
            cut.append(f"{r['short']}: 보유분이 있으면 {won(r['next_support'])} 종가 이탈 시 비중 축소 검토.")
        else:
            why = []
            if r["support"] != "지지 확인":
                why.append(r["support"])
            if not r["foreign_ok"]:
                why.append(r["foreign_txt"])
            if r.get("adr") is not None and not r["adr_ok"] and r["adr"]["dev_pct"] is not None:
                why.append("ADR 괴리 조건 미충족")
            if r["missing"]:
                why.append("미확인 항목 있음")
            do_now.append(f"{r['short']}: 관망({', '.join(why) or '조건 일부 미충족'}). 신규 진입 없음.")
            cut.append(f"{r['short']}: 10/30 저가 종가 이탈 시 진입 보류로 전환.")
    later.append("2차 1/3: [SAMNIX] 외국인 3일 연속 순매수 전환 알림 또는 종가 20일선 회복 중 먼저 오는 것.")
    later.append("3차 1/3: 10/30 시가 회복 또는 1월 실적 확인.")
    body = "\n".join(
        [
            f"{stamp} | NIX1102 | 11/2 장 마감 후 점검",
            "이게 뭐 알림: 인텔 쇼크 다음 거래일(11/2) SK하이닉스, 삼성전자의 일봉 지지(10/30 저가 기준), 외국인 수급, 하이닉스 ADR 괴리를 보고 1차 분할 진입 조건 충족 여부를 1번 알립니다. 매수 지시가 아닙니다.",
            "지금 결과:",
            *[line for r in results for line in stock_lines(r, cfg)],
            "지금 할 일: " + " ".join(do_now),
            "나중에: " + " ".join(later),
            "익절/줄일 때: " + " ".join(cut),
            "하지 말 것: 한 번에 전량 진입, 신용과 미수, 미확인 항목을 추정해서 조건 충족으로 보기, 장중 갭하락만 보고 추격 매도.",
        ]
    )
    prio = 4 if any(v in ("1차 분할 진입 검토", "진입 보류") for v in verdicts.values()) else 3
    return title, body, prio


# ---------------- 실행 ----------------
def run(mode: str, now: datetime | None = None, data: dict | None = None) -> int:
    cfg = load_cfg()
    now = now or datetime.now(KST)
    if now.year != int(cfg["year_guard"]):
        print("nix1102 year-guard silent")
        return 0
    ends = datetime.strptime(cfg["ends_kst"], "%Y-%m-%d %H:%M").replace(tzinfo=KST)
    if now > ends and mode not in ("dry", "selftest"):
        print("nix1102 window ended, silent")
        return 0
    st = load_state()
    if st.get("sent") and mode not in ("dry", "selftest"):
        print(f"nix1102 already sent at {st['sent'].get('at')}, silent")
        return 0
    final_from = datetime.strptime(cfg["final_from_kst"], "%Y-%m-%d %H:%M").replace(tzinfo=KST)
    final = mode == "force" or now >= final_from
    data = data if data is not None else fetch_all(cfg, now)
    for e in data["errors"]:
        print("fetch:", e)
    results = [judge_stock(cfg, s, data["stocks"].get(s["code"], {"daily": {}, "foreign": {}}), data) for s in cfg["stocks"]]
    title, body, prio = build_message(cfg, results, now)
    missing = [f"{r['name']} {m}" for r in results for m in r["missing"]]
    no_candle = [r["name"] for r in results if not r["t"] or not r["r"]]
    stamp = now.strftime("%Y-%m-%d %H:%M")

    if mode in ("dry", "selftest"):
        print("DRY RUN" if mode == "dry" else "SELFTEST")
        print("final" if final else "not final", "| missing:", missing or "-")
        print("TITLE:", title)
        print(body)
        return 0

    topic = cfg["ntfy_topic"]
    server = resolve_server(cfg.get("ntfy_server"))
    if missing and not final:
        st.setdefault("attempts", []).append({"at": stamp, "missing": missing})
        save_state(st)
        print("nix1102 data incomplete, wait for final run:", missing)
        return 0
    if no_candle:
        if not st.get("ops_sent"):
            ops_title = f"{cfg['title_prefix']} 000660/005930 11/2 일봉 조회 실패 | 판정 없음"
            ops_body = "\n".join(
                [
                    f"{now.strftime('%Y-%m-%d %H:%M KST')} | NIX1102",
                    "이게 뭐 알림: 11/2 장 마감 후 점검에 필요한 일봉을 받지 못했습니다. 매매 신호가 아닙니다.",
                    "지금 상태: KRX 일봉 없음 " + ", ".join(no_candle) + (". 오류: " + " / ".join(data["errors"]) if data["errors"] else ""),
                    "지금 할 일: HTS에서 직접 11/2 종가와 10/30 저가를 비교하세요.",
                    "나중에: 필요하면 job=nix1102 를 oneoff_mode=auto 로 다시 실행.",
                    "익절/줄일 때: 해당 없음.",
                    "하지 말 것: 이 알림을 지지 이탈로 읽기.",
                ]
            )
            publish("cwally-ops", ops_title, ops_body, 3, ["warning"], server)
            st["ops_sent"] = {"at": stamp}
        st.setdefault("attempts", []).append({"at": stamp, "missing": missing})
        save_state(st)
        return 0
    publish(topic, title, body, prio, ["bar_chart"], server)
    st["sent"] = {"at": stamp, "title": title, "verdicts": {r["code"]: r["verdict"] for r in results}}
    st.setdefault("attempts", []).append({"at": stamp, "missing": missing})
    save_state(st)
    return 0


def _fake(target_close: float, low: float, high: float, f_t, f_r, base: float) -> dict:
    daily = {}
    start = datetime(2026, 9, 1)
    d = start
    i = 0
    while d.strftime("%Y-%m-%d") < "2026-10-30":
        if d.weekday() < 5:
            p = base * (1 + 0.002 * i)
            daily[d.strftime("%Y-%m-%d")] = {"open": p, "high": p * 1.01, "low": p * 0.99, "close": p}
            i += 1
        d += timedelta(days=1)
    daily["2026-10-30"] = {"open": base * 1.05, "high": base * 1.06, "low": base * 0.95, "close": base * 0.97}
    daily["2026-11-02"] = {"open": base * 0.93, "high": high, "low": low, "close": target_close}
    return {"daily": daily, "foreign": {"2026-10-30": f_r, "2026-11-02": f_t}}


def selftest() -> int:
    base = 1_450_000
    adr, fx = [], []
    d = datetime(2026, 9, 1)
    while d.strftime("%Y-%m-%d") <= "2026-11-02":
        if d.weekday() < 5:
            day = d.strftime("%Y-%m-%d")
            fx.append((day, 1400.0))
            adr.append((day, 100.0))
        d += timedelta(days=1)
    cases = {
        "support_ok": (_fake(base * 0.965, base * 0.94, base * 0.97, -1000, -5000, base), _fake(226000, 221000, 227000, 100, -900, 230000)),
        "break": (_fake(base * 0.90, base * 0.88, base * 0.95, -6000, -5000, base), _fake(219000, 218000, 226000, -1500, -900, 230000)),
        "no_foreign": (_fake(base * 0.965, base * 0.94, base * 0.97, None, -5000, base), _fake(226000, 221000, 227000, None, -900, 230000)),
    }
    expect = {
        "support_ok": ("1차 분할 진입 검토", "1차 분할 진입 검토"),
        "break": ("진입 보류", "관망"),
        "no_foreign": ("관망", "관망"),
    }
    cfg = load_cfg()
    now = datetime(2026, 11, 2, 18, 20, tzinfo=KST)
    for name, (hy, sam) in cases.items():
        print(f"===== case {name}")
        data = {"stocks": {"000660": hy, "005930": sam}, "adr": adr, "fx": fx, "errors": []}
        run("selftest", now, data)
        results = [judge_stock(cfg, s, data["stocks"][s["code"]], data) for s in cfg["stocks"]]
        got = tuple(r["verdict"] for r in results)
        assert got == expect[name], f"{name}: {got} != {expect[name]}"
        title, body, _ = build_message(cfg, results, now)
        assert "(000660)" in title and "(005930)" in title, title
        assert "\u00b7" not in title + body and "\u2014" not in title + body, "middle dot or em-dash"
    # 15:40 gate: today's row before close is not a bar
    assert "2026-11-02" not in _gate_rows(datetime(2026, 11, 2, 15, 39, tzinfo=KST))
    assert "2026-11-02" in _gate_rows(datetime(2026, 11, 2, 15, 40, tzinfo=KST))
    print("SELFTEST OK")
    return 0


def _gate_rows(now: datetime) -> dict:
    fake = {"data": [
        {"symbolCode": "A000660", "date": "2026-11-02 00:00:00", "openingPrice": 1, "highPrice": 2, "lowPrice": 1, "tradePrice": 2},
        {"symbolCode": "A000660", "date": "2026-10-30 00:00:00", "openingPrice": 1, "highPrice": 2, "lowPrice": 1, "tradePrice": 2},
    ]}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps(fake).encode()

    orig = urllib.request.urlopen
    urllib.request.urlopen = lambda *a, **k: _Resp()
    try:
        return daum_daily("000660", now, min_rows=1)
    finally:
        urllib.request.urlopen = orig


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "auto"
    if mode == "selftest":
        return selftest()
    if mode not in ("auto", "force", "dry"):
        print(f"unknown mode {mode}")
        return 2
    return run(mode)


if __name__ == "__main__":
    raise SystemExit(main())
