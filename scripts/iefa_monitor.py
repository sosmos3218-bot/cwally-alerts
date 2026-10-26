#!/usr/bin/env python3
"""[IEFA] iShares Core MSCI EAFE ETF new-buy watch (approved in chat 2026-10-26). Not held; reference only.

  1. 200일선 회복: band BELOW and close > SMA200 x 1.005 -> push, band ABOVE (priority 3).
  2. 200일선 이탈: band ABOVE and close < SMA200 x 0.995 -> push, band BELOW (priority 3).
     Inside the +-0.5% band nothing changes. SMA200 is recomputed for every bar from the 200
     confirmed closes ending at that bar (incomplete current-day US bar excluded).
  3. PER 17배 이하: iShares page P/E <= 17.0 -> push once, then disarmed until a P/E >= 17.5.
     Judged once per new as-of date (state pe_last_asof).
Bars after state last_date are judged in date order (catch-up); everything that fires in one run
goes out as one combined push. Price and P/E fetch failures do not block each other; each failure
sends at most one ops notice per KST day ('조회 실패' titles route to cwally-ops).

Runs on the existing port-alerts morning 09:00 KST slot. No new cron.
Modes: auto | dry (print only, no ntfy, no state) | selftest.
"""
from __future__ import annotations

import html
import json
import re
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
CFG = ROOT / "configs" / "iefa.json"
STATE = ROOT / "state" / "iefa.json"
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) cwally-alerts", "Accept": "*/*"}
MONTHS = {m: i + 1 for i, m in enumerate("Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split())}


def load_cfg() -> dict:
    return json.loads(CFG.read_text(encoding="utf-8"))


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def http_get(url: str, timeout: int = 25) -> str:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


# ---------- price ----------

def confirmed_bars(res: dict, now_ny: datetime) -> list[tuple[date, float]]:
    """(US/Eastern date, close) ascending. Today's bar only after 16:10 ET (else it is still forming)."""
    out: dict[date, float] = {}
    closes = ((res.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
    for t, c in zip(res.get("timestamp") or [], closes):
        if c is not None:
            out[datetime.fromtimestamp(t, NY).date()] = float(c)
    today = now_ny.date()
    after_close = now_ny.hour * 60 + now_ny.minute >= 16 * 60 + 10
    return sorted((d, c) for d, c in out.items() if d < today or (d == today and after_close))


def fetch_bars(cfg: dict, now_ny: datetime) -> tuple[list[tuple[date, float]], str]:
    errs = []
    for key in ("bars", "bars_fallback"):
        try:
            res = json.loads(http_get(cfg["sources"][key]))["chart"]["result"][0]
            bars = confirmed_bars(res, now_ny)
            if bars:
                return bars, "Yahoo " + ("query1" if key == "bars" else "query2")
            errs.append(f"{key}: no bars")
        except Exception as exc:  # noqa: BLE001
            errs.append(f"{key}: {exc}")
    raise RuntimeError("; ".join(errs))


def sma(closes: list[float], n: int) -> float | None:
    return sum(closes[-n:]) / n if len(closes) >= n else None


def context(bars: list[tuple[date, float]], i: int, cfg: dict) -> dict:
    """Indicators for bar i using only closes up to and including bar i."""
    closes = [c for _, c in bars[: i + 1]]
    win = bars[max(0, i + 1 - int(cfg.get("w52_len", 252))): i + 1]
    hd, hc = max(win, key=lambda x: x[1])
    return {"day": str(bars[i][0]), "close": bars[i][1], "sma200": sma(closes, int(cfg.get("sma_len", 200))),
            "sma50": sma(closes, int(cfg.get("sma_short_len", 50))), "hi": hc, "hi_day": str(hd)}


def band_step(band: str, close: float, s200: float, pct: float) -> tuple[str, str | None]:
    up, dn = round(s200 * (1 + pct / 100), 6), round(s200 * (1 - pct / 100), 6)
    if band == "BELOW" and close > up:
        return "ABOVE", "recover"
    if band == "ABOVE" and close < dn:
        return "BELOW", "break"
    return band, None


def judge_bars(bars, band: str, last_date: str | None, cfg: dict) -> tuple[str, list[dict], dict | None]:
    """Walk bars newer than last_date in order. Returns (band, events, ctx of the last judged bar)."""
    pct = float(cfg.get("band_pct", 0.5))
    events, last, short = [], None, 0
    for i, (d, _) in enumerate(bars):
        if last_date and str(d) <= last_date:
            continue
        ctx = context(bars, i, cfg)
        if ctx["sma200"] is None:
            short += 1
            continue
        band, ev = band_step(band, ctx["close"], ctx["sma200"], pct)
        print(f"bar {d} close {ctx['close']:.2f} sma200 {ctx['sma200']:.2f} gap {(ctx['close'] / ctx['sma200'] - 1) * 100:+.2f}% -> band {band}{' FIRE ' + ev if ev else ''}")
        if ev:
            events.append(dict(ctx, key=ev))
        last = ctx
    if short:
        print(f"{short} bar(s) with fewer than {cfg.get('sma_len', 200)} closes skipped")
    return band, events, last


# ---------- P/E ----------

def parse_pe(page: str) -> tuple[float, str]:
    s = html.unescape(page)
    m = re.search(r'"priceEarnings"\s*:\s*\{(.{0,3000}?)"fullName"\s*:\s*"fundamentalsAndRisk\.priceEarnings"', s, re.S)
    if m:
        blk = m.group(1)
        v = re.search(r'"value"\s*:\s*([0-9]+(?:\.[0-9]+)?)', blk)
        a = re.search(r'"asOfDate"\s*:\s*"?(\d{8})', blk)
        if v and a:
            ad = a.group(1)
            return float(v.group(1)), f"{ad[:4]}-{ad[4:6]}-{ad[6:]}"
    t = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s))
    m = re.search(r"P/E Ratio\s+([0-9]+(?:\.[0-9]+)?)\s+as of\s+([A-Z][a-z]{2})\s+(\d{1,2}),\s+(\d{4})", t)
    if m:
        return float(m.group(1)), f"{m.group(4)}-{MONTHS[m.group(2)]:02d}-{int(m.group(3)):02d}"
    raise ValueError("P/E Ratio not found on iShares page")


def fetch_pe(cfg: dict) -> tuple[float, str]:
    return parse_pe(http_get(cfg["sources"]["pe"], timeout=30))


def pe_step(armed: bool, last_asof: str | None, pe: float, asof: str, fire_le: float, rearm_ge: float) -> tuple[bool, bool, bool]:
    """Returns (armed, fired, judged). A same or older as-of date is not judged again."""
    if last_asof and asof <= last_asof:
        return armed, False, False
    if not armed and pe >= rearm_ge:
        return True, False, True
    if armed and pe <= fire_le:
        return False, True, True
    return armed, False, True


# ---------- message ----------

def message(cfg: dict, events: list[dict], pe_fired: bool, ctx: dict | None, pe: tuple[float, str] | None, band: str, now: datetime) -> tuple[str, str, int]:
    L = cfg["lines"]
    keys = [e["key"] for e in events] + (["pe"] if pe_fired else [])
    title = "[IEFA] " + " + ".join(f"{L[k]['label']} | {L[k]['action']}" for k in keys)
    prio = max(int(L[k]["priority"]) for k in keys)
    pct = float(cfg.get("band_pct", 0.5))
    parts = []
    for e in events:
        verb = "위로 회복" if e["key"] == "recover" else "아래로 이탈"
        edge = e["sma200"] * (1 + pct / 100 if e["key"] == "recover" else 1 - pct / 100)
        parts.append(f"{e['day']} 종가 {e['close']:.2f}달러, 200일선 {e['sma200']:.2f} {verb}(기준 {edge:.2f}).")
    if pe_fired and pe:
        parts.append(f"PER {pe[0]:.2f}배({pe[1]} 기준)로 17배 이하.")
    if ctx:
        g = (ctx["close"] / ctx["sma200"] - 1) * 100
        s50 = f", 50일선 {ctx['sma50']:.2f}({(ctx['close'] / ctx['sma50'] - 1) * 100:+.2f}%)" if ctx.get("sma50") else ""
        parts.append(f"최근 종가 {ctx['close']:.2f}달러({ctx['day']} 미 동부), 200일선 {ctx['sma200']:.2f} 대비 {g:+.2f}%{s50}, "
                     f"52주 종가 고점 {ctx['hi']:.2f}({ctx['hi_day']}) 대비 {(ctx['close'] / ctx['hi'] - 1) * 100:+.1f}%.")
    else:
        parts.append("가격: 시세 조회 실패, 200일선 판정은 다음 실행에서.")
    parts.append(f"PER {pe[0]:.2f}배({pe[1]} 기준)." if pe and not pe_fired else ("" if pe else "PER: 조회 실패."))
    parts.append(f"현재 상태 200일선 {'위' if band == 'ABOVE' else '아래'}(미보유).")
    todo = []
    if "recover" in keys and band == "ABOVE":
        todo.append("신규 매수 검토 구간. 산다면 계획 금액의 일부만 나눠서, 다음 종가도 200일선 위에 머무는지 확인.")
    if pe_fired and band == "ABOVE":
        todo.append("PER 도 싸진 편. 200일선 위라 가격 흐름과 같이 보고 분할 검토.")
    elif pe_fired:
        todo.append("PER 은 싸진 편이지만 가격이 200일선 아래라 회복 알림 전에는 새로 사지 말고 보류.")
    elif band == "BELOW":
        todo.append("200일선 아래라 새로 사지 말고 보류.")
    up = dn = None
    if ctx:
        up, dn = ctx["sma200"] * (1 + pct / 100), ctx["sma200"] * (1 - pct / 100)
    nxt = (f"종가가 {dn:.2f} 아래면 200일선 이탈 알림." if band == "ABOVE" else f"종가가 {up:.2f} 위면 200일선 회복 알림.") if ctx else "다음 09:00 실행에서 다시 판정."
    lines = [
        f"{now:%Y-%m-%d %H:%M} KST | IEFA | {cfg.get('display', 'IEFA')}",
        "이게 뭐 알림: " + cfg["explain"],
        "지금 결과/상태: " + " ".join(p for p in parts if p),
        "지금 할 일: " + " ".join(todo),
        f"나중에: {nxt} 200일선은 매일 새로 계산되어 기준값이 조금씩 바뀜. PER 은 {cfg['pe_rearm_ge']}배 이상인 새 기준일에서 다시 무장.",
        "익절/줄일 때: 미보유라 해당 없음. 사게 되면 200일선 이탈 알림을 비중 재검토 신호로 봄.",
        "하지 말 것: 장중 가격으로 미리 매수, 한 번에 계획 금액 전부, 신용/미수, 이 알림만 보고 주문. 투자 권유 아님.",
    ]
    return title, "\n".join(lines), prio


# ---------- run ----------

def run(cfg: dict, st: dict, now: datetime, get_bars, get_pe, send, dry: bool) -> dict:
    """One pass. Returns the new state (caller saves it unless dry). send(topic_kind, title, body, prio)."""
    st = json.loads(json.dumps(st))
    today = str(now.date())
    if now.year != int(cfg.get("year_guard", now.year)) or today > cfg.get("expire_date", "9999-12-31"):
        print(f"iefa outside year_guard/expire ({cfg.get('year_guard')}, {cfg.get('expire_date')}); silent")
        return st
    fails = []
    bars = None
    try:
        bars, src = get_bars(cfg, now.astimezone(NY))
        print(f"price source: {src}, {len(bars)} bars, last {bars[-1]}")
    except Exception as exc:  # noqa: BLE001
        print(f"bars fetch failed: {exc}")
        fails.append(("price", str(exc)[:200]))
    pe = None
    try:
        pe = get_pe(cfg)
        print(f"P/E {pe[0]} as of {pe[1]}")
    except Exception as exc:  # noqa: BLE001
        print(f"P/E fetch failed: {exc}")
        fails.append(("pe", str(exc)[:200]))

    band = st.get("band") or "BELOW"
    events, ctx = [], None
    if bars:
        if (now.astimezone(NY).date() - bars[-1][0]).days > int(cfg.get("stale_days", 6)):
            print(f"stale last bar {bars[-1][0]}; skip price lines")
        elif not st.get("initialized"):
            ctx = context(bars, len(bars) - 1, cfg)
            if ctx["sma200"]:
                band = "ABOVE" if ctx["close"] >= ctx["sma200"] else "BELOW"
                st.update({"initialized": True, "band": band, "last_date": ctx["day"], "last_close": round(ctx["close"], 4),
                           "last_sma200": round(ctx["sma200"], 4), "init_note": f"initialized from {ctx['day']}, no send"})
                print(f"init band {band} from {ctx['day']} (no send)")
        else:
            band, events, ctx = judge_bars(bars, band, st.get("last_date"), cfg)
            if ctx:
                st.update({"band": band, "last_date": ctx["day"], "last_close": round(ctx["close"], 4), "last_sma200": round(ctx["sma200"], 4)})
            else:
                ctx = context(bars, len(bars) - 1, cfg)
                if ctx["sma200"] is None:
                    ctx = None
                    print(f"no judgeable bar after {st.get('last_date')}; band {band}")
                else:
                    print(f"no new bar after {st.get('last_date')}; band {band}; last close {ctx['close']:.2f} sma200 {ctx['sma200']:.2f}")
        st["fail_day"] = None

    pe_fired = False
    if pe:
        armed, pe_fired, judged = pe_step(bool(st.get("pe_armed", True)), st.get("pe_last_asof"), pe[0], pe[1],
                                          float(cfg["pe_fire_le"]), float(cfg["pe_rearm_ge"]))
        print(f"P/E judged={judged} fired={pe_fired} armed={armed}" + ("" if judged else f" (as-of {pe[1]} already judged)"))
        if judged:
            st.update({"pe_armed": armed, "pe_last_asof": pe[1], "pe_last_value": round(pe[0], 4)})
        st["pe_fail_day"] = None

    if events or pe_fired:
        title, body, prio = message(cfg, events, pe_fired, ctx, pe, band, now)
        if dry:
            print(f"DRY RUN would send (p{prio}): {title}\n{body}")
        else:
            send("port", title, body, prio)
        st["last_sent"] = {"at": now.isoformat(timespec="seconds"), "title": title}
    else:
        print("no crossing; silent")

    new_fails = [(k, e) for k, e in fails if st.get("fail_day" if k == "price" else "pe_fail_day") != today]
    if new_fails:
        kinds = {k for k, _ in new_fails}
        title = "[IEFA] 시세 조회 실패 | 판정 보류" if "price" in kinds else "[IEFA] PER 조회 실패 | PER 판정 보류"
        what = {"price": "야후 일봉(query1/query2)", "pe": "아이셰어즈 PER"}
        body = " / ".join(f"{what[k]} 조회 실패: {e}" for k, e in new_fails)
        body += ". 다른 쪽 판정은 그대로 진행. 다음 09:00 실행에서 다시 봅니다. 매매 신호 아님."
        if dry:
            print(f"DRY RUN would send (ops): {title}\n{body}")
        else:
            send("ops", title, body, 3)
        for k in kinds:
            st["fail_day" if k == "price" else "pe_fail_day"] = today
    return st


def main_send(kind: str, title: str, body: str, prio: int) -> None:
    publish(resolve_topic(kind), title, body, prio, tags=["warning"] if kind == "ops" else ["chart_with_upwards_trend"], config_name="iefa")


# ---------- selftest ----------

def selftest() -> int:
    cfg = load_cfg()
    fails = 0

    def check(label, got, want):
        nonlocal fails
        ok = got == want
        fails += not ok
        print(("ok  " if ok else "FAIL"), label, got, "" if ok else f"want {want}")

    end = date(2026, 10, 26)

    def mk(closes):
        n = len(closes)
        return [(end - timedelta(days=n - 1 - i), float(c)) for i, c in enumerate(closes)]

    hist = [100.0] * 199
    # SMA(199x100 + c) = (19900 + c)/200; recovery needs c > 1.005 * that, i.e. c > ~100.50
    def walk(label, band, tail, want_events, want_band):
        b = mk(hist + tail)
        nb, ev, _ = judge_bars(b, band, str(b[198][0]), cfg)
        check(label, ([(e["day"], e["key"]) for e in ev], nb), ([(str(b[199 + i][0]), k) for i, k in want_events], want_band))

    walk("seed BELOW, close inside band no fire", "BELOW", [100.4], [], "BELOW")
    walk("recovery fire", "BELOW", [101.0], [(0, "recover")], "ABOVE")
    walk("band no fire (ABOVE, -0.4%)", "ABOVE", [100, 99.6], [], "ABOVE")
    walk("breakdown fire", "ABOVE", [100, 99.0], [(1, "break")], "BELOW")
    walk("recover, band, break, re-recover (catch-up in order)", "BELOW", [101, 100.6, 99.0, 99.5, 101.5], [(0, "recover"), (2, "break"), (4, "recover")], "ABOVE")
    walk("same close twice fires once", "BELOW", [101, 101], [(0, "recover")], "ABOVE")
    # dynamic SMA: the same close 101 fires against a 100 history but not against a rising 100.8 history
    nb, ev, _ = judge_bars(mk([100.8] * 199 + [101.0]), "BELOW", None, cfg)
    check("dynamic SMA: 101 vs sma ~100.80 no fire", (ev, nb), ([], "BELOW"))
    b = mk([90.0] * 100 + [100.0] * 100 + [101.0])
    c1, c2 = context(b, 199, cfg)["sma200"], context(b, 200, cfg)["sma200"]
    check("dynamic SMA recomputed per bar (95.00 -> 95.055)", (round(c1, 3), round(c2, 3)), (95.0, 95.055))
    check("short history skipped", judge_bars(mk([100] * 150 + [120]), "BELOW", None, cfg)[1], [])
    # seed: real 10/23 numbers
    check("seed 95.68 vs 96.21 BELOW no fire", band_step("BELOW", 95.68, 96.21, 0.5), ("BELOW", None))
    check("edge exactly x1.005 no fire (strict >)", band_step("BELOW", 100.5, 100.0, 0.5), ("BELOW", None))

    # P/E
    check("P/E 18.42 armed no fire", pe_step(True, "2026-10-22", 18.42, "2026-10-23", 17, 17.5), (True, False, True))
    check("P/E 17.0 fires", pe_step(True, "2026-10-23", 17.0, "2026-10-26", 17, 17.5), (False, True, True))
    check("P/E 16.5 disarmed no fire", pe_step(False, "2026-10-26", 16.5, "2026-10-27", 17, 17.5), (False, False, True))
    check("P/E 17.4 no rearm", pe_step(False, "2026-10-27", 17.4, "2026-10-28", 17, 17.5), (False, False, True))
    check("P/E 17.5 rearms silently", pe_step(False, "2026-10-28", 17.5, "2026-10-29", 17, 17.5), (True, False, True))
    check("P/E fires again after rearm", pe_step(True, "2026-10-29", 16.9, "2026-10-30", 17, 17.5), (False, True, True))
    check("P/E same as-of not re-judged", pe_step(True, "2026-10-30", 16.0, "2026-10-30", 17, 17.5), (True, False, False))
    page_json = '...&quot;priceEarnings&quot;:{&quot;name&quot;:&quot;priceEarnings&quot;,&quot;asOfDate&quot;:20261023,&quot;formattedValue&quot;:&quot;18.42&quot;,&quot;value&quot;:18.42054,&quot;fullName&quot;:&quot;fundamentalsAndRisk.priceEarnings&quot;}'
    check("parse P/E embedded JSON", parse_pe(page_json), (18.42054, "2026-10-23"))
    check("parse P/E page text", parse_pe("<span>P/E Ratio</span> <span>18.42</span> <span>as of Oct 23, 2026</span>"), (18.42, "2026-10-23"))
    try:
        parse_pe("<html>nothing</html>")
        check("parse P/E missing raises", False, True)
    except ValueError:
        check("parse P/E missing raises", True, True)

    # full runs with fakes
    now = datetime(2026, 10, 27, 9, 0, tzinfo=KST)
    seed = load_state()
    bars_ok = mk(hist + [101.0])
    last = str(bars_ok[-1][0])
    st0 = dict(seed, last_date=str(bars_ok[-2][0]), band="BELOW")
    check("seed file: BELOW, P/E armed, 10/23", (seed.get("band"), seed.get("pe_armed"), seed.get("last_date"), seed.get("pe_last_asof")), ("BELOW", True, "2026-10-23", "2026-10-23"))
    sent = []

    def snd(kind, title, body, prio):
        sent.append((kind, title, prio, body))

    def gb(_c, _n):
        return bars_ok, "fake"

    def gb_seen(_c, _n):
        return [b for b in mk(hist + [100.0] * 5) if b[0] <= date(2026, 10, 23)], "fake"

    def gp(_c):
        return 16.8, "2026-10-26"

    def gp_same(_c):
        return 18.42, "2026-10-23"

    def boom(*_a):
        raise RuntimeError("HTTP 503")

    sent.clear()
    s = run(cfg, seed, now, gb_seen, gp_same, snd, False)
    check("seed state + same as-of: silent", sent, [])
    sent.clear()
    s = run(cfg, st0, now, gb, gp, snd, False)
    check("combined push: one send", [(k, t, p) for k, t, p, _ in sent], [("port", "[IEFA] 200일선 회복 | 매수 검토 + PER 17배 이하 | 저평가 구간", 3)])
    check("combined state", (s["band"], s["last_date"], s["pe_armed"], s["pe_last_asof"]), ("ABOVE", last, False, "2026-10-26"))
    body = sent[0][3] if sent else ""
    for frag in ("종가 101.00", "200일선", "50일선", "PER 16.80배(2026-10-26 기준)", "52주 종가 고점"):
        check(f"body has {frag}", frag in body, True)
    sent.clear()
    s2 = run(cfg, s, now, gb, gp, snd, False)
    check("same bar + same as-of re-run: silent, state same", (sent, s2["band"], s2["pe_armed"]), ([], "ABOVE", False))
    sent.clear()
    s3 = run(cfg, st0, now, boom, gp, snd, False)
    check("price fail: P/E still fires + one ops", [(k, t) for k, t, _, _ in sent],
          [("port", "[IEFA] PER 17배 이하 | 저평가 구간"), ("ops", "[IEFA] 시세 조회 실패 | 판정 보류")])
    check("price fail keeps last_date", s3["last_date"], st0["last_date"])
    sent.clear()
    run(cfg, s3, now + timedelta(hours=1), boom, gp, snd, False)
    check("price fail same day: no second ops", sent, [])
    sent.clear()
    run(cfg, s3, now + timedelta(days=1), boom, gp, snd, False)
    check("price fail next day: ops again", [(k, t) for k, t, _, _ in sent], [("ops", "[IEFA] 시세 조회 실패 | 판정 보류")])
    sent.clear()
    s4 = run(cfg, st0, now, gb, boom, snd, False)
    check("P/E fail: price line still fires + PER ops", [(k, t) for k, t, _, _ in sent],
          [("port", "[IEFA] 200일선 회복 | 매수 검토"), ("ops", "[IEFA] PER 조회 실패 | PER 판정 보류")])
    check("P/E fail keeps P/E state", (s4["pe_armed"], s4["pe_last_asof"]), (st0["pe_armed"], st0["pe_last_asof"]))
    sent.clear()
    run(cfg, st0, now, boom, boom, snd, False)
    check("both fail: one ops message", [(k, t) for k, t, _, _ in sent], [("ops", "[IEFA] 시세 조회 실패 | 판정 보류")])
    sent.clear()
    run(cfg, st0, now, gb, gp, snd, True)
    check("dry never sends", sent, [])
    sent.clear()
    run(cfg, st0, datetime(2027, 1, 4, 9, 0, tzinfo=KST), gb, gp, snd, False)
    check("after expire: silent", sent, [])
    # incomplete current-day bar excluded
    ny = datetime(2026, 10, 26, 11, 0, tzinfo=NY)
    res = {"timestamp": [int(datetime(2026, 10, 23, 9, 30, tzinfo=NY).timestamp()), int(datetime(2026, 10, 26, 9, 30, tzinfo=NY).timestamp())],
           "indicators": {"quote": [{"close": [95.68, 96.9]}]}}
    check("intraday bar excluded", [str(d) for d, _ in confirmed_bars(res, ny)], ["2026-10-23"])
    check("bar kept after 16:10 ET", [str(d) for d, _ in confirmed_bars(res, ny.replace(hour=16, minute=10))], ["2026-10-23", "2026-10-26"])
    t, b_, _ = message(cfg, [dict(context(mk(hist + [99.0]), 199, cfg), key="break")], True, context(mk(hist + [99.0]), 199, cfg), (16.8, "2026-10-26"), "BELOW", now)
    check("break + P/E: no buy advice", "보류" in b_.split("\n")[3] and "분할 검토" not in b_.split("\n")[3], True)
    for lab, txt in (("combined", body), ("break", t + b_)):
        check(f"no middle dot / em-dash ({lab})", any(ch in txt for ch in "\u00b7\u2014"), False)
    print(t)
    print("selftest", "PASS" if not fails else f"FAIL {fails}")
    return 1 if fails else 0


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    if mode == "selftest":
        return selftest()
    if mode not in {"auto", "dry"}:
        print(f"unknown mode {mode}")
        return 2
    dry = mode == "dry"
    now = datetime.now(KST)
    print(f"{now.isoformat(timespec='seconds')} iefa mode={mode}")
    if dry:
        print("DRY RUN (no ntfy, no state write)")
    st = run(load_cfg(), load_state(), now, fetch_bars, fetch_pe, main_send, dry)
    if not dry:
        st["updated_at"] = now.isoformat(timespec="seconds")
        STATE.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
