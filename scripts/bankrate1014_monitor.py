#!/usr/bin/env python3
"""[BANKRATE] 10/13 미국 대형은행 실적 + 10년물 5.31% 재돌파 -> 시나리오 A~D 판정 일회 알림.

modes: auto (기본, 창 안에서만 발송) | dry (발송과 state 저장 없음) | force (창 무시, 이미 보냈어도 발송)
dry/force 에서만 쓰는 선택 인자: python scripts/bankrate1014_monitor.py dry [US_DATE] [SEC_FILING_DATE]
  예) dry 2026-10-06 2026-07-14  (과거 날짜로 판정 로직 시험)
"""
from __future__ import annotations

import json
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from html import unescape
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
CFG = ROOT / "configs" / "bankrate1014.json"
STATE = ROOT / "state" / "bankrate1014.json"
YUA = {"User-Agent": "Mozilla/5.0 cwally-alerts"}
SUA = {"User-Agent": "cwally-alerts sosmos3218@gmail.com", "Accept-Encoding": "identity"}


def fetch(url: str, headers: dict, timeout: int = 30) -> str:
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def textify(html: str) -> str:
    t = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", html)
    t = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", t)
    t = re.sub(r"<[^>]+>", " ", t)
    return re.sub(r"\s+", " ", unescape(t))


def load_state() -> dict:
    st = {"final_notified": False, "fail_notified": False, "last_phase": None, "updated_at": None, "result": {}}
    if STATE.exists():
        try:
            st.update(json.loads(STATE.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass
    return st


def save_state(st: dict, now: datetime) -> None:
    st["updated_at"] = now.isoformat()
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# ---------- 시세 ----------

def bars(ticker: str) -> tuple[list[dict], float | None]:
    """일봉 목록(거래소 현지 날짜 기준)과 현재가. 종가가 비어 있는 봉은 버린다."""
    url = "https://query1.finance.yahoo.com/v8/finance/chart/" + urllib.parse.quote(ticker) + "?interval=1d&range=1mo"
    payload = json.loads(fetch(url, YUA))
    res = payload["chart"]["result"][0]
    off = int(res["meta"].get("gmtoffset") or 0)
    q = res["indicators"]["quote"][0]
    out = []
    for i, ts in enumerate(res.get("timestamp") or []):
        close = q["close"][i]
        if close is None:
            continue
        day = datetime.fromtimestamp(ts + off, tz=timezone.utc).strftime("%Y-%m-%d")
        out.append({"date": day, "close": float(close), "high": float(q["high"][i]) if q["high"][i] is not None else None})
    # 같은 날짜가 두 번 오면(장중 임시 봉) 마지막 값만 쓴다
    dedup: dict[str, dict] = {}
    for b in out:
        dedup[b["date"]] = b
    price = res["meta"].get("regularMarketPrice")
    return [dedup[k] for k in sorted(dedup)], (float(price) if price is not None else None)


def day_move(blist: list[dict], date: str):
    """date 봉의 종가, 전일 종가, 등락률(%), 고가. date 봉이 없으면 None."""
    for i, b in enumerate(blist):
        if b["date"] == date and i > 0:
            prev = blist[i - 1]["close"]
            return {"close": b["close"], "prev": prev, "pct": (b["close"] / prev - 1) * 100, "high": b["high"], "prev_date": blist[i - 1]["date"]}
    return None


def norm_yield(v):
    if v is None:
        return None
    return v / 10.0 if v > 20 else v


# ---------- JPM 조정 EPS (SEC 8-K item 2.02) ----------

def jpm_eps(cik: str, filing_date: str) -> dict | None:
    sub = json.loads(fetch(f"https://data.sec.gov/submissions/CIK{cik}.json", SUA, 60))
    rec = sub["filings"]["recent"]
    acc = None
    for i, form in enumerate(rec["form"]):
        if form == "8-K" and rec["filingDate"][i] == filing_date and "2.02" in (rec["items"][i] or ""):
            acc = (rec["accessionNumber"][i], rec["primaryDocument"][i])
            break
    if not acc:
        return None
    nodash = acc[0].replace("-", "")
    base = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{nodash}/"
    idx = json.loads(fetch(base + "index.json", SUA, 30))
    names = [x["name"] for x in idx["directory"]["item"] if x["name"].lower().endswith(".htm")]
    cands = [n for n in names if re.search(r"99|exhibit|narrative|ex9", n, re.I) and not re.match(r"R\d+\.htm", n)]
    cands.sort(key=lambda n: (0 if re.search(r"narrative|991", n, re.I) else 1, n))
    for name in cands:
        text = textify(fetch(base + name, SUA, 40))
        head = text[:2500]
        vals = re.findall(r"\(\s*\$\s*([0-9]+\.[0-9]{2})\s+PER\s+SHARE\s*\)", head, re.I)
        if not vals:
            continue
        reported = float(vals[0])
        adjusted = float(vals[1]) if len(vals) > 1 and re.search(r"EXCLUDING\s+SIGNIFICANT\s+ITEMS", head, re.I) else reported
        return {"reported": reported, "adjusted": adjusted, "doc": name, "accession": acc[0]}
    return None


# ---------- 입찰 상태 (ustsy10 기록 재사용) ----------

def auction_lines(cfg: dict) -> tuple[list[str], bool]:
    a = cfg["auction"]
    try:
        parsed = json.loads((ROOT / a["state_file"]).read_text(encoding="utf-8")).get("parsed") or {}
    except Exception:
        return ["입찰: 기록 읽기 실패(확인 불가)"], False
    lines, weak_any = [], False
    for tenor, label in (("10y", "10년물"), ("30y", "30년물")):
        row = parsed.get(tenor) or {}
        hy, btc, ind = row.get("high_yield"), row.get("btc"), row.get("indirect")
        if hy is None and btc is None:
            lines.append(f"{label} 입찰: 결과 미기록")
            continue
        avg = a["avg"][tenor]
        weak = (btc is not None and btc <= avg["btc"] - a["weak_btc_below_avg"]) or (
            ind is not None and ind <= avg["indirect"] - a["weak_indirect_below_avg_pp"]
        )
        weak_any = weak_any or weak
        lines.append(
            f"{label} 입찰 낙찰금리 {hy if hy is not None else '확인 불가'}%, 응찰률 {btc if btc is not None else '확인 불가'}배(평균 {avg['btc']}), "
            f"간접 {('%.1f' % ind) if ind is not None else '확인 불가'}%(평균 {avg['indirect']}) -> {'약함' if weak else '무난'}"
        )
    return lines, weak_any


# ---------- 판정 ----------

def classify(cfg: dict, tnx: dict, jpm_adj, banks: dict, mkt_pct: float) -> dict:
    rules = cfg["rules"]
    trig = rules["yield_trigger"]
    close = tnx["close"]
    breakout = close >= trig
    touch_only = (not breakout) and tnx.get("high") is not None and tnx["high"] >= trig

    cons = cfg["consensus"]["JPM_adj_eps"]
    eps_score = 0
    if jpm_adj is not None:
        eps_score = 1 if jpm_adj >= cons else -1
    rets = [v["pct"] for v in banks.values() if v]
    avg = sum(rets) / len(rets)
    excess = avg - mkt_pct
    thr = rules["excess_ret_pp"]
    px_score = 1 if excess >= thr else (-1 if excess <= -thr else 0)
    total = eps_score + px_score
    bank = "good" if total >= 1 else ("bad" if total <= -1 else "mixed")
    if bank == "good":
        code = "B" if breakout else "A"
    elif bank == "bad":
        code = "D" if breakout else "C"
    else:
        code = "BD" if breakout else "AC"
    return {
        "code": code,
        "bank": bank,
        "breakout": breakout,
        "touch_only": touch_only,
        "eps_score": eps_score,
        "px_score": px_score,
        "avg_bank_pct": avg,
        "excess_pp": excess,
    }


def fmt_pct(v) -> str:
    return f"{v:+.2f}%" if isinstance(v, (int, float)) else "확인 불가"


def emit(cfg: dict, title: str, lines: list[str], pri: int) -> None:
    publish(resolve_topic("port", cfg.get("ntfy_topic")), title[:180], "\n".join(lines), pri, ["chart_with_upwards_trend"], resolve_server(cfg.get("ntfy_server")))


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    cfg = json.loads(CFG.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    st = load_state()
    us_date = sys.argv[2] if len(sys.argv) > 2 and mode in {"dry", "force"} else cfg["event_us_date"]
    sec_date = sys.argv[3] if len(sys.argv) > 3 and mode in {"dry", "force"} else us_date
    start = datetime.strptime(cfg["window_start_kst"], "%Y-%m-%d %H:%M").replace(tzinfo=KST)
    end = datetime.strptime(cfg["window_end_kst"], "%Y-%m-%d %H:%M").replace(tzinfo=KST)
    in_win = start <= now <= end and now.year == int(cfg["year_guard"])
    print(f"{now.isoformat()} mode={mode} us_date={us_date} window={in_win}")
    if mode not in {"dry", "force"} and not in_win:
        print("outside 2026 window; silent")
        save_state(st, now)
        return 0
    if st.get("final_notified") and mode != "force":
        print("already sent; skip")
        return 0

    ban = cfg["banks"]
    tick = cfg["tickers"]
    errs: list[str] = []
    data: dict = {}
    try:
        tb, _ = bars(tick["tnx"])
        data["tnx"] = day_move(tb, us_date)
        if data["tnx"]:
            data["tnx"]["close"] = norm_yield(data["tnx"]["close"])
            data["tnx"]["high"] = norm_yield(data["tnx"]["high"])
            data["tnx"]["prev"] = norm_yield(data["tnx"]["prev"])
            data["tnx"]["bp"] = (data["tnx"]["close"] - data["tnx"]["prev"]) * 100
    except Exception as exc:
        errs.append(f"10년물 {exc}")
    banks: dict = {}
    for sym in [ban["lead"], *ban["peers"]]:
        try:
            b, _ = bars(sym)
            banks[sym] = day_move(b, us_date)
        except Exception as exc:
            errs.append(f"{sym} {exc}")
            banks[sym] = None
    try:
        gb, _ = bars(ban["market_ticker"])
        data["mkt"] = day_move(gb, us_date)
        xb, _ = bars(ban["sector_ticker"])
        data["xlf"] = day_move(xb, us_date)
    except Exception as exc:
        errs.append(f"S&P500/XLF {exc}")
    try:
        sb, _ = bars(tick["sox"])
        data["sox"] = day_move(sb, us_date)
    except Exception as exc:
        errs.append(f"SOX {exc}")
    kospi = krw = None
    try:
        kb, _ = bars(tick["kospi"])
        kospi = kb[-1] if kb else None
    except Exception as exc:
        errs.append(f"KOSPI {exc}")
    try:
        _, krw = bars(tick["usdkrw"])
    except Exception as exc:
        errs.append(f"원/달러 {exc}")
    eps = None
    try:
        eps = jpm_eps(cfg["consensus"]["sec_cik"], sec_date)
    except Exception as exc:
        errs.append(f"JPM 8-K {exc}")

    tnx = data.get("tnx")
    mkt = data.get("mkt")
    have_banks = [k for k, v in banks.items() if v]
    critical_ok = bool(tnx) and bool(mkt) and ban["lead"] in have_banks and len(have_banks) >= 2
    if not critical_ok:
        msg = "필수 데이터 부족: " + ("10년물 봉 없음 " if not tnx else "") + ("S&P500 봉 없음 " if not mkt else "") + f"은행 시세 {len(have_banks)}/3. " + "; ".join(errs)
        print(msg)
        if mode == "dry":
            return 0
        if not st.get("fail_notified") or mode == "force":
            emit(cfg, "[BANKRATE] 조회 실패 | 시나리오로 읽지 마", [
                now.strftime("%Y-%m-%d %H:%M KST") + " | BANKRATE | 은행 실적과 10년물",
                "이게 뭐 알림: " + cfg["explain"],
                "지금 상태: 조회 실패. " + msg + ". 시나리오 판정을 하지 않았습니다.",
                "지금 할 일: 10/13 10년물 종가와 JPM, WFC, 씨티 실적을 직접 확인. 이 실패로 매매하지 마세요.",
                "나중에: 같은 알림은 다시 오지 않습니다. 같은 밤 21:30 KST CPI 알림은 별도입니다.",
                "익절/줄일 때: 실패 알림으로 비중 조정 금지.",
                "하지 말 것: 조회 실패를 은행 부진이나 금리 안정으로 읽기.",
            ], 4)
            st["fail_notified"] = True
            st["last_phase"] = "fail"
            save_state(st, now)
        return 0

    jpm_adj = eps["adjusted"] if eps else None
    res = classify(cfg, tnx, jpm_adj, {k: banks[k] for k in have_banks}, mkt["pct"])
    sc = cfg["scenarios"][res["code"]]
    base = cfg["baseline"]
    auction, auction_weak = auction_lines(cfg)

    cons = cfg["consensus"]
    eps_txt = (
        f"JPM 조정 EPS {jpm_adj:.2f}달러(컨센서스 {cons['JPM_adj_eps']:.2f}, {'상회' if jpm_adj >= cons['JPM_adj_eps'] else '하회'}), 보고 EPS {eps['reported']:.2f}"
        if eps
        else "JPM EPS 확인 불가(8-K 파싱 실패, 주가 반응만으로 판정)"
    )
    bank_txt = ", ".join(f"{k} {fmt_pct(banks[k]['pct'])}" for k in have_banks)
    missing = [k for k in [ban["lead"], *ban["peers"]] if k not in have_banks]
    if missing:
        bank_txt += " (" + ", ".join(missing) + " 시세 없음)"
    yld = (
        f"10/13 종가 {tnx['close']:.3f}% ({tnx['bp']:+.0f}bp), 장중 고가 {tnx['high']:.3f}%" if tnx.get("high") is not None else f"10/13 종가 {tnx['close']:.3f}%"
    )
    trig = cfg["rules"]["yield_trigger"]
    if res["breakout"]:
        yld += f" -> {trig}% 재돌파"
    elif res["touch_only"]:
        yld += f" -> 장중만 {trig}% 터치, 종가는 아래"
    else:
        yld += f" -> {trig}% 아래"

    sox = data.get("sox")
    kospi_txt = ""
    if kospi:
        kospi_txt = f"KOSPI 직전 마감({kospi['date']}) {kospi['close']:,.2f}(10/7 {base['kospi']:,.2f} 대비 {(kospi['close'] / base['kospi'] - 1) * 100:+.2f}%)"
    krw_txt = ""
    if krw:
        krw_txt = f"원/달러 {krw:,.1f}원(10/7 15:30 {base['usdkrw']:,.1f}원 대비 {krw - base['usdkrw']:+.1f}원, 참고 시세)"
        if krw >= base["watch_usdkrw_up"]:
            krw_txt += f" 이미 {base['watch_usdkrw_up']:,}원 위"
        elif krw <= base["watch_usdkrw_down"]:
            krw_txt += f" 이미 {base['watch_usdkrw_down']:,}원 아래"

    bank_label = {"good": "호조", "bad": "부진", "mixed": "엇갈림"}[res["bank"]]
    status = (
        f"판정 {res['code']}. 은행 {bank_label}. {eps_txt}. "
        f"주가 반응 {bank_txt}, 평균 {fmt_pct(res['avg_bank_pct'])}, S&P500 {fmt_pct(mkt['pct'])} 대비 {res['excess_pp']:+.2f}%p. "
        f"10년물 {yld}. "
        + " ".join(x + "." for x in auction)
        + (f" 반도체지수(SOX) {fmt_pct(sox['pct'])}." if sox else "")
        + (" " + kospi_txt + "." if kospi_txt else "")
        + (" " + krw_txt + "." if krw_txt else "")
    )
    caution = ""
    if auction_weak and not res["breakout"]:
        caution = " 입찰이 약했으니 금리 재상승 위험은 한 단계 높게 보기."
    title = f"[BANKRATE] {sc['title']}"
    lines = [
        now.strftime("%Y-%m-%d %H:%M KST") + " | BANKRATE | 10/13 은행 실적과 10년물",
        "이게 뭐 알림: " + cfg["explain"],
        "지금 상태: " + status,
        "지금 할 일: " + sc["semis"] + " " + sc["krw"] + caution,
        "나중에: 오늘 21:30 KST 미국 9월 CPI(CPI 알림 별도). 이 판정은 CPI 전 잠정이라 CPI 근원이 0.3% 이상이면 B, D 쪽으로 한 칸 이동해서 읽기. 미확인: GS, MS, BAC 실적일은 이 판정에서 제외.",
        f"익절/줄일 때: 오늘 외국인 순매도 {base['watch_foreign_sell_tn']:.0f}조원 이상(10/7은 약 {base['foreign_net_sell_krw_tn_1007']}조원)이거나 원/달러가 {base['watch_usdkrw_up']:,}원 위로 안착하면 반도체 비중 점검.",
        "하지 말 것: " + sc["dont"] + " 은행 한 곳 실적으로 시나리오를 확정하기.",
        "출처: Yahoo Finance 일봉(^TNX, JPM, WFC, C, ^GSPC, ^SOX, ^KS11, KRW=X), SEC EDGAR JPM 8-K, state/ustsy10.json",
    ]
    if mode == "dry":
        print(title)
        print("\n".join(lines))
        print("errors:", errs)
        print("result:", json.dumps(res, ensure_ascii=False))
        return 0
    emit(cfg, title, lines, 4)
    st["final_notified"] = True
    st["last_phase"] = "final"
    st["result"] = {**res, "us_date": us_date, "jpm_adj_eps": jpm_adj, "tnx_close": tnx["close"], "sent_at": now.isoformat()}
    save_state(st, now)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
