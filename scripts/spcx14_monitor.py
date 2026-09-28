#!/usr/bin/env python3
from __future__ import annotations

import json
import math
import re
import sys
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
CFG_PATH = ROOT / "configs" / "spcx14.json"
STATE_PATH = ROOT / "state" / "spcx14.json"
UA = {"User-Agent": "Mozilla/5.0 cwally-alerts"}

ORBIT_OK = re.compile(
    r"orbital insertion|reached orbit|achieved orbit|in orbit|six orbits|completed .+ orbit",
    re.I,
)
DEPLOY_OK = re.compile(
    r"deploy(?:ed|ment) .+starlink|starlink v3.{0,40}deploy|26 starlink.{0,30}deploy|payload deploy(?:ed|ment) complete",
    re.I,
)
REENTRY_OK = re.compile(
    r"splashdown|controlled reentry|successful reentry|ship .{0,20}splash",
    re.I,
)
SCRUB = re.compile(r"scrub(?:bed)?|hold|recycle|no earlier than|net |delayed|weather hold", re.I)
FAIL = re.compile(
    r"lost the ship|rapid unscheduled|exploded|disintegrated|failed to reach orbit|anomaly|vehicle lost",
    re.I,
)
PLANNED = re.compile(r"preparing to launch|pending regulatory|targets the launch|planned", re.I)


def fetch_text(url: str) -> str:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=25) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def visible(text: str) -> str:
    text = re.sub(r"<script[\s\S]*?</script>", " ", text, flags=re.I)
    text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text)


def yahoo_last(ticker: str) -> float:
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=5d"
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=20) as resp:
        payload = json.loads(resp.read().decode())
    result = payload["chart"]["result"][0]
    meta = result.get("meta") or {}
    px = meta.get("regularMarketPrice")
    if px:
        return float(px)
    closes = [x for x in result["indicators"]["quote"][0]["close"] if x is not None]
    if not closes:
        raise RuntimeError(f"no price for {ticker}")
    return float(closes[-1])


def classify(blob: str) -> tuple[str, dict]:
    flags = {
        "orbit": bool(ORBIT_OK.search(blob)),
        "deploy": bool(DEPLOY_OK.search(blob)),
        "reentry": bool(REENTRY_OK.search(blob)),
        "fail": bool(FAIL.search(blob)),
        "scrub": bool(SCRUB.search(blob)) and not ORBIT_OK.search(blob),
        "planned": bool(PLANNED.search(blob)),
    }
    if flags["fail"] and not flags["reentry"]:
        if flags["orbit"] or flags["deploy"]:
            return "partial", flags
        return "fail", flags
    if flags["orbit"] and flags["deploy"] and flags["reentry"]:
        return "success", flags
    if flags["orbit"] or flags["deploy"] or flags["reentry"]:
        return "partial", flags
    if flags["scrub"]:
        return "scrub", flags
    if flags["planned"]:
        return "pending", flags
    return "unknown", flags


def shares_for(budget_krw: float, px: float, fx: float) -> tuple[float, int, float, float]:
    usd = budget_krw / fx
    raw = usd / px
    whole = math.floor(raw)
    spent_usd = whole * px
    spent_krw = spent_usd * fx
    return raw, whole, spent_usd, spent_krw


def won(n: float) -> str:
    return f"{n:,.0f}원"


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    today = now.date()
    state = {
        "updated_at": None,
        "phase": "idle",
        "results_notified": False,
        "miss_notified": False,
        "outcome": None,
        "price": None,
        "fx": None,
        "source": None,
        "flags": None,
    }
    if STATE_PATH.exists():
        try:
            state.update(json.loads(STATE_PATH.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass

    start = date.fromisoformat(cfg["window_start"])
    end = date.fromisoformat(cfg["window_end"])
    year_ok = today.year == int(cfg["oneshot_year"])
    in_window = year_ok and start <= today <= end
    if mode not in {"force", "event"} and not in_window:
        print(f"outside window {start}~{end}; silent")
        return 0
    if state.get("results_notified") and mode != "force":
        print("already sent; skip")
        return 0

    kind, flags, source, snippet = "unknown", {}, "", ""
    fails = 0
    for url in cfg["sources"]:
        try:
            raw = fetch_text(url)
        except Exception as exc:
            print(f"fetch fail {url}: {exc}")
            fails += 1
            continue
        guess, fl = classify(visible(raw))
        print(f"{url} -> {guess} {fl}")
        if guess in {"success", "partial", "fail"}:
            kind, flags, source, snippet = guess, fl, url, visible(raw)[:240]
            if guess == "success":
                break
        elif guess == "scrub" and kind in {"unknown", "pending"}:
            kind, flags, source, snippet = guess, fl, url, visible(raw)[:240]
        elif kind == "unknown":
            kind, flags, source, snippet = guess, fl, url, visible(raw)[:240]

    price = fx = None
    price_err = fx_err = ""
    try:
        price = yahoo_last(cfg["ticker"])
    except Exception as exc:
        price_err = str(exc)
        print(f"price fail: {exc}")
    try:
        fx = yahoo_last(cfg["fx_ticker"])
    except Exception as exc:
        fx_err = str(exc)
        print(f"fx fail: {exc}")

    ready = kind in {"success", "partial", "fail", "scrub"} and price and fx
    miss = (
        (not ready)
        and today >= end
        and year_ok
        and not state.get("miss_notified")
        and not state.get("results_notified")
    )
    if not ready and not miss:
        print(f"not ready kind={kind} price={price} fx={fx}; silent")
        state["updated_at"] = now.isoformat()
        state["phase"] = kind
        state["outcome"] = kind
        state["source"] = source
        STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return 0

    assets = float(cfg["assets_krw"])
    if price and fx:
        raw1, sh1, usd1, krw1 = shares_for(assets * cfg["tranche1_pct"], price, fx)
        raw2, sh2, usd2, krw2 = shares_for(assets * cfg["tranche2_pct"], price, fx)
        raw7, sh7, usd7, krw7 = shares_for(assets * cfg["hardcap_pct"], price, fx)
        chg = (price / cfg["ref_close"] - 1.0) * 100.0
        p20 = price * (1 + cfg["add2_dd"])  # from NEW close if 1차 at this close
        p25 = price * (1 + cfg["review_dd"])
        p35 = price * (1 + cfg["add3_dd"])
        p40 = price * (1 + cfg["cut_dd"])
        p30 = price * (1 + cfg["trim_up"])
    else:
        raw1 = sh1 = usd1 = krw1 = 0
        raw2 = sh2 = usd2 = krw2 = 0
        raw7 = sh7 = usd7 = krw7 = 0
        chg = 0
        p20 = p25 = p35 = p40 = p30 = 0

    if kind == "success":
        outcome_ko = "궤도+V3배치+재진입 문구가 보임. 성공으로 분류."
        t1 = "1차(2.5%) 가능. 발사를 더 기다리지 말 것."
        t2 = "2차는 불가. 성공은 추가매수 신호가 아님. 평균단가 -20~-25%만 대기."
        action = "1차만 검토 | 2차 보류"
    elif kind == "partial":
        bits = []
        bits.append("궤도 있음" if flags.get("orbit") else "궤도 미확인")
        bits.append("V3 배치 있음" if flags.get("deploy") else "V3 배치 미확인")
        bits.append("재진입 있음" if flags.get("reentry") else "재진입 미확인")
        outcome_ko = "부분성공. " + ", ".join(bits)
        t1 = "1차 가능. 다만 갭이 크면 체결을 서두르지 말 것."
        t2 = "2차는 불가. 부분실패만으로 물타기 금지."
        action = "1차 신중 | 2차 보류"
    elif kind == "fail":
        outcome_ko = "실패·이상 문구. 회사 존폐 이벤트는 아님."
        t1 = "1차는 가격이 기준가 근처면 가능. 실패 자체를 매수 신호로 쓰지 말 것."
        t2 = "2차는 주가가 평균단가 -20~-25%이고 스타링크 테제가 살아 있을 때만."
        action = "추격 금지 | 2차 조건부"
    elif kind == "scrub":
        outcome_ko = "연기·홀드. 결과 없음."
        t1 = "1차 가능하나 발사 당일 갭을 피할 것."
        t2 = "2차 불가."
        action = "결과 전 | 2차 보류"
    else:
        outcome_ko = f"결과 미확인. price_err={price_err} fx_err={fx_err}"
        t1 = "표 갱신 보류. 직접 확인."
        t2 = "2차 불가."
        action = "미확인 | 직접 확인"

    two_ok = bool(price) and price <= cfg["ref_close"] * (1 + cfg["add2_dd"])
    if two_ok and kind in {"fail", "partial"}:
        t2 = f"가격이 기준가 대비 -20% 이하({price:.2f}). 1차를 이미 채웠고 테제가 살았으면 2차 검토. 아니면 1차부터."

    lines = [
        f"{now.strftime('%Y-%m-%d %H:%M KST')} | SPCX | 스타십 14호기 후 비중표",
        "이게 뭐 알림: 스타십 14호기 결과와 종가·환율로 2억 원 기준 1차/2차/하드캡 표를 갱신한 알림입니다.",
        f"지금 결과/상태: {outcome_ko}",
        f"종가 {price if price else '없음'}달러 (기준 148.68달러 대비 {chg:+.1f}%) | 환율 {fx if fx else '없음'}원",
        f"궤도 {flags.get('orbit')} | V3배치 {flags.get('deploy')} | 재진입 {flags.get('reentry')}",
        f"1차 2.5% {won(assets*cfg['tranche1_pct'])} → {raw1:.1f}주, 내림 {sh1}주 ({won(krw1)})",
        f"2차 누적 5% {won(assets*cfg['tranche2_pct'])} → 내림 {sh2}주 ({won(krw2)})",
        f"하드캡 7% {won(assets*cfg['hardcap_pct'])} → 내림 {sh7}주 ({won(krw7)})",
        f"점검가(이 종가에 1차 시): -20% {p20:.2f} | -25% {p25:.2f} | -35% {p35:.2f} | -40% {p40:.2f} | +30% {p30:.2f}",
        f"지금 할 일: {t1}",
        f"2차 대기: {t2}",
        "나중에: 11월 Q3 실적과 10/9·10/24 락업 때만 표를 다시 보세요.",
        "익절/줄일 때: +30%이고 비중이 7%면 5%로. -40%면 절반.",
        "하지 말 것: 레버리지, 성공 추격으로 5% 초과, 실패만으로 물타기, 신용/미수.",
    ]
    if source:
        lines.append(f"출처: {source}")
    msg = "\n".join(lines)
    title = f"[PORT] SPCX {action}"
    print(msg)
    topic = resolve_topic("port", cfg.get("ntfy_topic"))
    server = resolve_server(cfg.get("ntfy_server"))
    publish(topic, title, msg, 4, ["rocket", "chart_with_upwards_trend"], server)

    state.update(
        {
            "updated_at": now.isoformat(),
            "phase": kind,
            "results_notified": True if ready else state.get("results_notified"),
            "miss_notified": True if miss else state.get("miss_notified"),
            "outcome": kind,
            "price": price,
            "fx": fx,
            "source": source,
            "flags": flags,
            "snippet": snippet,
        }
    )
    if miss:
        state["miss_notified"] = True
        state["results_notified"] = True
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
