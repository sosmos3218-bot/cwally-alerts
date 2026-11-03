#!/usr/bin/env python3
"""[GRAB] 그랩 홀딩스(NASDAQ: GRAB) 적정가/최악 시나리오 가격선 + 인도네시아 배달 수수료 상한 뉴스. 평일 09:00 KST morning 묶음 (ftnt 뒤).

가격: 미국 전일 정규장 완료 일봉(Yahoo chart, 종가/고가/저가). 데이터 거래일(state.last_date)로 중복 방지.
  up415: 종가 또는 고가 >= 4.15 이면 1번(p4), 종가 < 4.03 에서 재무장.
  dn320: 종가 또는 저가 <= 3.20 이면 1번(p4), 종가 > 3.30 에서 재무장.
뉴스: Google News RSS(인도네시아어/영어). 제목이 규제 단어 AND 범위 단어(배달/물품/후속 규정)일 때만. 배달 + 수수료 단어면 p4,
  아니면 p3. 실행마다 새 기사 한 묶음. 링크와 정규화 제목으로 중복 방지. 첫 실행은 시드(발송 없음).
조회 실패는 cwally-ops 하루 1번(제목에 조회 실패). modes: auto | dry(발송 없음, state 안 씀) | selftest(네트워크 없음)
"""
from __future__ import annotations

import hashlib
import html
import json
import re
import sys
import traceback
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic, route_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
ET = ZoneInfo("America/New_York")
CFG_PATH = ROOT / "configs" / "grab.json"
STATE_PATH = ROOT / "state" / "grab.json"
WEB_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
TAGS = ["motor_scooter"]
BAD = ("\u00b7", "\u2014", "\u318d")


def fetch(url: str, headers: dict | None = None) -> str:
    req = urllib.request.Request(url, headers=headers or WEB_UA)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def clean(t: str) -> str:
    return t.replace("\u00b7", "/").replace("\u318d", "/").replace("\u2014", "-")


# ---------- 가격 ----------
def bars_from_chart(d: dict, now: datetime) -> list[dict]:
    """완료된 정규장 일봉만 (미국 동부 16:15 이후 당일 포함), 오름차순."""
    res = ((d.get("chart") or {}).get("result") or [None])[0] or {}
    q = (((res.get("indicators") or {}).get("quote") or [{}])[0])
    et_now = now.astimezone(ET)
    out = []
    for t, c, h, lo in zip(res.get("timestamp") or [], q.get("close") or [], q.get("high") or [], q.get("low") or []):
        if c is None:
            continue
        day = datetime.fromtimestamp(t, ET).date()
        if day < et_now.date() or (day == et_now.date() and (et_now.hour, et_now.minute) >= (16, 15)):
            c = round(float(c), 2)
            out.append({"date": day.isoformat(), "close": c, "high": round(float(h), 2) if h is not None else c,
                        "low": round(float(lo), 2) if lo is not None else c})
    return out


def inside(line: dict, bar: dict) -> bool:
    if line["side"] == "ge":
        return max(bar["close"], bar["high"]) >= line["price"]
    return min(bar["close"], bar["low"]) <= line["price"]


def rearm(line: dict, bar: dict) -> bool:
    if line["side"] == "ge":
        return bar["close"] < line["rearm_close_below"]
    return bar["close"] > line["rearm_close_above"]


def judge(lines: list[dict], armed: dict, bar: dict) -> list[str]:
    fired = []
    for ln in lines:
        k = ln["key"]
        a = armed.get(k, True)
        if a and inside(ln, bar):
            armed[k] = False
            fired.append(k)
        elif not a and rearm(ln, bar):
            armed[k] = True
        else:
            armed[k] = a
    return fired


def body(stamp: str, what: str, status: str, action: str, later: str, trim: str, avoid: str) -> str:
    return "\n".join([f"{stamp} | GRAB | 그랩 홀딩스", f"이게 뭐 알림: {what}", f"지금 결과/상태: {status}",
                      f"지금 할 일: {action}", f"나중에: {later}", f"익절/줄일 때: {trim}", f"하지 말 것: {avoid}"])


def price_msg(cfg: dict, key: str, bar: dict, prev: float | None, stamp: str) -> tuple[str, str, int]:
    ln = next(x for x in cfg["lines"] if x["key"] == key)
    th = cfg["thesis"]
    c = bar["close"]
    chg = f", 전일 대비 {(c / prev - 1) * 100:+.1f}%" if prev else ""
    fv = (th["fair_value"] / c - 1) * 100
    wc = (c / th["worst_case"] - 1) * 100
    if key == "up415":
        title = f"[GRAB] GRAB 종가 ${c:.2f}(고가 ${bar['high']:.2f}) 적정가 $4.15 도달 | 추격 금지, 일부 익절 검토"
        action = "보유분이 있으면 일부 익절을 검토. 새로 사지는 말고, 4.15 위에서 근거(실적 상향, 규제 완화)가 새로 나왔는지 확인."
        later = f"종가가 ${ln['rearm_close_below']:.2f} 아래로 내려가면 다시 무장합니다."
        trim = "적정가 근처라 한 번에 다 팔기보다 나눠서."
        avoid = "적정가 돌파를 새 매수 신호로 읽기, 신용/미수."
    else:
        title = f"[GRAB] GRAB 종가 ${c:.2f}(저가 ${bar['low']:.2f}) 최악 시나리오 $3.20 이하 | 인도네시아 규제 확인"
        action = ("인도네시아 8% 수수료 상한이 음식/물품 배달까지 확대됐는지(Komdigi, Perpres 후속 규정) 먼저 확인. "
                  "확대가 없으면 최악 가정보다 싼 구간이라 분할로만 검토, 확대가 확정이면 적정가 자체를 낮춰 다시 계산.")
        later = f"종가가 ${ln['rearm_close_above']:.2f} 를 넘으면 다시 무장합니다."
        trim = "배달 수수료 상한 확대가 확정되면 비중 축소 검토."
        avoid = "규제 확인 없이 물타기, 한 번에 매수, 이 알림만 보고 주문."
    status = (f"{ln['label']}. {bar['date']} 미국 종가 ${c:.2f}{chg}, 고가 ${bar['high']:.2f}, 저가 ${bar['low']:.2f}. "
              f"적정가 ${th['fair_value']:.2f} 까지 {fv:+.1f}%, 최악 ${th['worst_case']:.2f} 대비 {wc:+.1f}%. {cfg['sources']['quote_page']}")
    what = "GRAB 가격이 분석의 적정가(4.15달러) 또는 최악 시나리오(3.20달러)에 닿았다는 알림. 매매 지시 아님, 투자 권유 아님."
    return title, body(stamp, what, status, action, later, trim, avoid), int(ln["priority"])


# ---------- 뉴스 ----------
def norm_title(t: str) -> str:
    t = re.sub(r"\s+-\s+[^-]{2,60}$", "", t.strip())  # ' - 출처' 꼬리
    return re.sub(r"[^0-9a-z가-힣]+", "", t.lower())


def news_key(title: str) -> str:
    return hashlib.sha1(norm_title(title).encode()).hexdigest()[:16]


def classify_news(cfg: dict, title: str) -> int | None:
    """None = 무시, 4 = 배달 수수료 상한, 3 = 후속 규정/관련."""
    n = cfg["news"]
    if not (re.search(n["rule_re"], title, re.I) and re.search(n["scope_re"], title, re.I)):
        return None
    return 4 if re.search(n["delivery_re"], title, re.I) and re.search(n["cap_re"], title, re.I) else 3


def parse_rss(rss: str) -> list[dict]:
    out = []
    for item in re.findall(r"(?s)<item>(.*?)</item>", rss):
        t = re.search(r"(?s)<title>(.*?)</title>", item)
        lk = re.search(r"(?s)<link>(.*?)</link>", item)
        pd = re.search(r"(?s)<pubDate>(.*?)</pubDate>", item)
        if not t:
            continue
        title = clean(html.unescape(re.sub(r"<!\[CDATA\[|\]\]>", "", t.group(1))).strip())
        try:
            when = parsedate_to_datetime(pd.group(1)).astimezone(KST) if pd else None
        except Exception:  # noqa: BLE001
            when = None
        out.append({"title": title, "link": html.unescape(lk.group(1).strip()) if lk else "", "when": when})
    return out


def fetch_news(cfg: dict) -> list[dict]:
    items, errs = [], []
    for q in cfg["news"]["queries"]:
        url = "https://news.google.com/rss/search?" + urllib.parse.urlencode(
            {"q": q["q"] + " when:7d", "hl": q["hl"], "gl": q["gl"], "ceid": q["ceid"]})
        try:
            items += parse_rss(fetch(url))
        except Exception as exc:  # noqa: BLE001
            errs.append(f"{q['q']}: {exc.__class__.__name__}")
    if errs and len(errs) == len(cfg["news"]["queries"]):
        raise RuntimeError("; ".join(errs)[:200])
    return items


def pick_news(cfg: dict, items: list[dict], seen: set[str], now: datetime) -> list[dict]:
    cutoff = now - timedelta(hours=int(cfg["news"]["lookback_hours"]))
    out, keys = [], set()
    for it in items:
        if it["when"] is None or it["when"] < cutoff or it["when"] > now + timedelta(hours=1):
            continue
        pr = classify_news(cfg, it["title"])
        if pr is None:
            continue
        k = news_key(it["title"])
        if k in seen or k in keys or (it["link"] and it["link"] in seen):
            continue
        keys.add(k)
        out.append({**it, "prio": pr, "key": k})
    out.sort(key=lambda x: (-x["prio"], -x["when"].timestamp()))
    return out


def news_msg(cfg: dict, picks: list[dict], stamp: str) -> tuple[str, str, int]:
    top = max(p["prio"] for p in picks)
    n = len(picks)
    if top >= 4:
        title = f"[GRAB] GRAB 인도네시아 배달 수수료 상한 뉴스 {n}건 | 규제 범위 확인"
    else:
        title = f"[GRAB] GRAB 인도네시아 ojol 후속 규정 뉴스 {n}건 | 참고"
    shown = picks[: int(cfg["news"]["max_items"])]
    lines = [f"{p['when']:%m/%d %H:%M} {p['title'][:120]} {p['link']}" for p in shown]
    more = f" 외 {n - len(shown)}건" if n > len(shown) else ""
    status = "새 기사: " + " / ".join(lines) + more
    what = "인도네시아 8% 수수료 상한(ojol)이 음식/물품 배달로 확대되는지(Komdigi, Perpres 후속 규정) 관련 새 기사 알림. GRAB 최악 시나리오의 핵심 위험."
    action = ("기사 원문에서 배달(GrabFood, GoFood, 물품) 포함 여부, 시행 날짜, 확정/검토 단계를 확인." if top >= 4
              else "후속 규정 일정과 배달 포함 여부만 확인. 아직 매매 판단 근거 아님.")
    return title, body(stamp, what, status, action, "같은 기사(링크, 제목)는 다시 보내지 않습니다. 평일 09:00 에 새 기사만 봅니다.",
                       "배달 확대가 확정되면 적정가 4.15 가정을 낮추고 비중 축소 검토.",
                       "제목만 보고 주문, 검토/제안 단계 기사를 확정으로 읽기."), top


def ops_msg(stamp: str, tail: str, status: str) -> tuple[str, str, int]:
    return (f"[GRAB] GRAB {tail}", body(stamp, "GRAB 알림 데이터 수집 안내. 매매 신호 아님.", status, "필요하면 시세나 뉴스를 직접 보세요.",
                                       "다음 평일 09:00 에 다시 봅니다.", "이 안내로 비중을 바꾸지 않기.", "조회 실패를 조건 미충족으로 읽기."), 3)


# ---------- 실행 ----------
def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def step_price(cfg: dict, st: dict, bars: list[dict], stamp: str) -> list[tuple[str, str, int]]:
    out = []
    if not bars:
        return out
    armed = st.setdefault("armed", {ln["key"]: True for ln in cfg["lines"]})
    last = st.get("last_date")
    prev = (st.get("last_bar") or {}).get("close")
    for b in bars:
        if last and b["date"] <= last:
            prev = b["close"]
            continue
        fired = judge(cfg["lines"], armed, b)
        print(f"bar {b['date']} c={b['close']} h={b['high']} l={b['low']} -> fired={fired} armed={armed}")
        out += [price_msg(cfg, k, b, prev, stamp) for k in fired]
        prev = b["close"]
        st["last_date"], st["last_bar"] = b["date"], b
    return out


def step_news(cfg: dict, st: dict, items: list[dict], now: datetime, stamp: str) -> list[tuple[str, str, int]]:
    ns = st.setdefault("news", {})
    seen = set(ns.get("seen") or [])
    picks = pick_news(cfg, items, seen, now)
    if not ns.get("seeded"):
        ns["seeded"] = stamp
        ns["seed_titles"] = [p["title"][:100] for p in picks]
        ns["seen"] = sorted(seen | {p["key"] for p in picks} | {p["link"] for p in picks if p["link"]})[-600:]
        print(f"news seeded (no send): {len(picks)}")
        return []
    if not picks:
        print("news: no new matching item")
        return []
    ns["seen"] = (list(ns.get("seen") or []) + [p["key"] for p in picks] + [p["link"] for p in picks if p["link"]])[-600:]
    return [news_msg(cfg, picks, stamp)]


def run(mode: str, now: datetime, cfg: dict) -> int:
    dry = mode == "dry"
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    day = stamp[:10]
    st = load_state()
    out: list[tuple[str, str, int]] = []
    try:
        bars = bars_from_chart(json.loads(fetch(cfg["sources"]["quote"])), now)
        if not bars:
            raise RuntimeError("완료 일봉 없음")
        out += step_price(cfg, st, bars, stamp)
    except Exception as exc:  # noqa: BLE001
        print(f"quote fail: {exc}")
        if st.get("quote_fail") != day:
            out.append(ops_msg(stamp, "시세 조회 실패 | 가격선 판정 못 함", f"Yahoo 일봉 조회 실패 ({str(exc)[:150]})."))
            st["quote_fail"] = day
    try:
        out += step_news(cfg, st, fetch_news(cfg), now, stamp)
    except Exception as exc:  # noqa: BLE001
        print(f"news fail: {exc}")
        if st.get("news_fail") != day:
            out.append(ops_msg(stamp, "뉴스 조회 실패 | 규제 뉴스 판정 못 함", f"Google News RSS 조회 실패 ({str(exc)[:150]})."))
            st["news_fail"] = day
    topic, server = resolve_topic("port", cfg.get("ntfy_topic")), resolve_server(cfg.get("ntfy_server"))
    for t, m, p in out:
        assert not any(ch in t + m for ch in BAD), t
        assert t.startswith("[GRAB] GRAB"), t
        print(f"TITLE: {t}\nPRIORITY: {p} TOPIC: {route_topic(topic, t)}\n{m}\n")
        if not dry:
            publish(topic, t, m, p, TAGS, server)
    if not out:
        print("no alert")
    lb = st.get("last_bar") or {}
    print(f"last bar {lb.get('date')} close {lb.get('close')} armed={st.get('armed')}")
    if dry:
        print("DRY RUN: no publish, state not written")
        return 0
    st["updated_at"] = now.isoformat(timespec="seconds")
    STATE_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


# ---------- selftest ----------
def selftest(cfg: dict) -> int:
    stamp = "2026-11-04 09:00 KST"
    L = cfg["lines"]
    b = lambda d, c, h=None, lo=None: {"date": d, "close": c, "high": h if h is not None else c, "low": lo if lo is not None else c}  # noqa: E731
    armed = {"up415": True, "dn320": True}
    seq = [
        (b("d1", 3.10, 3.25, 3.05), ["dn320"]),   # 저가/종가 3.20 이하 -> 1번
        (b("d2", 3.15, 3.22, 3.00), []),          # 그대로 안쪽 -> 재발송 없음
        (b("d3", 3.29, 3.40, 3.21), []),          # 3.30 이하 종가 -> 아직 재무장 안 됨
        (b("d4", 3.31, 3.40, 3.25), []),          # 종가 3.31 > 3.30 -> 재무장 (저가 3.25 는 3.20 위)
        (b("d5", 3.35, 3.40, 3.20), ["dn320"]),   # 저가 정확히 3.20 (<=) -> 다시 1번
        (b("d6", 4.00, 4.15, 3.90), ["up415"]),   # 고가 정확히 4.15 (>=) -> 1번
        (b("d7", 4.20, 4.30, 4.10), []),          # 안쪽 유지
        (b("d8", 4.05, 4.10, 4.00), []),          # 종가 4.05 >= 4.03 -> 재무장 안 됨
        (b("d9", 4.02, 4.10, 3.98), []),          # 종가 4.02 < 4.03 -> 재무장
        (b("d10", 4.16, 4.16, 4.05), ["up415"]),  # 종가 4.16 -> 다시 1번
        (b("d11", 3.80, 4.149, 3.70), []),        # 고가 4.149 -> 미달
    ]
    for bar, want in seq:
        got = judge(L, armed, bar)
        assert got == want, (bar, got, want, armed)
    print(f"price seq ok ({len(seq)} bars): 종가/고가/저가 기준 1번 발송, 3% 재무장 확인")
    # step_price: dedupe by date, seed armed -> 11/3 bar judged
    st = {"armed": {"up415": True, "dn320": True}, "last_date": "2026-11-02", "last_bar": b("2026-11-02", 2.86, 2.93, 2.75)}
    bars = [b("2026-11-02", 2.86, 2.93, 2.75), b("2026-11-03", 3.12, 3.30, 3.05)]
    o = step_price(cfg, st, bars, stamp)
    assert [x[0][:40] for x in o] and len(o) == 1 and "최악 시나리오 $3.20 이하" in o[0][0] and st["last_date"] == "2026-11-03", (o, st)
    assert step_price(cfg, st, bars, stamp) == [], "same bar twice must not resend"
    t, m, p = o[0]
    assert p == 4 and "전일 대비 +9.1%" in m and "finance.yahoo.com/quote/GRAB" in m and "저가 $3.05" in t, (t, m)
    print(f"TITLE: {t} (p{p})")
    t2, m2, p2 = price_msg(cfg, "up415", b("x", 4.18, 4.25, 4.0), 4.0, stamp)
    assert t2.startswith("[GRAB] GRAB 종가 $4.18(고가 $4.25) 적정가 $4.15 도달") and p2 == 4, t2
    print(f"TITLE: {t2} (p{p2})")
    # news
    now = datetime(2026, 11, 4, 9, 0, tzinfo=KST)
    w = now - timedelta(hours=5)
    rss = "".join(f"<item><title>{html.escape(tt)}</title><link>{lk}</link><pubDate>{w.strftime('%a, %d %b %Y %H:%M:%S +0900')}</pubDate></item>"
                  for tt, lk in [
                      ("Komdigi Siapkan Aturan Komisi 8% untuk GoFood dan GrabFood - Kompas", "https://n/1"),
                      ("Aturan Turunan Perpres Ojol Bakal Terbit Sebelum Desember 2026 - Liputan6.com", "https://n/2"),
                      ("Komdigi Siapkan Aturan Komisi 8% untuk GoFood dan GrabFood - Detik", "https://n/3"),   # 같은 제목, 다른 출처
                      ("Segini Penghasilan Ojol Kerja Seharian, Makin Lama Makin Kecil - CNBC Indonesia", "https://n/4"),  # 소음
                      ("Komisi Ojol Turun: Solusi atau Ancaman bagi Industri? - KONTAN", "https://n/5"),  # 규제 단어만, 범위 없음
                      ("Grab Boosts Sales, Earnings Forecasts on Rising Ride Demand - Bloomberg", "https://n/6"),
                      ("Kerja sama antara Grab dan Pemda soal komisi - X", "https://n/7"),  # 'antara' 는 배달 아님
                  ])
    items = parse_rss(rss)
    picks = pick_news(cfg, items, set(), now)
    assert [p["link"] for p in picks] == ["https://n/1", "https://n/2"] and [p["prio"] for p in picks] == [4, 3], picks
    st = {}
    assert step_news(cfg, st, items, now, stamp) == [] and st["news"]["seeded"], "first run seeds"
    assert step_news(cfg, st, items, now, stamp) == [], "seeded items not resent"
    st2 = {"news": {"seeded": "x", "seen": []}}
    o = step_news(cfg, st2, items, now, stamp)
    assert len(o) == 1 and o[0][2] == 4 and o[0][0] == "[GRAB] GRAB 인도네시아 배달 수수료 상한 뉴스 2건 | 규제 범위 확인", o
    assert "https://n/1" in o[0][1] and step_news(cfg, st2, items, now, stamp) == [], "dedupe by key/link"
    print(f"TITLE: {o[0][0]} (p{o[0][2]})")
    old = parse_rss(rss.replace(w.strftime('%a, %d %b %Y'), (now - timedelta(days=6)).strftime('%a, %d %b %Y')))
    assert pick_news(cfg, old, set(), now) == [], "older than lookback ignored"
    o3 = news_msg(cfg, [picks[1]], stamp)
    assert o3[0] == "[GRAB] GRAB 인도네시아 ojol 후속 규정 뉴스 1건 | 참고" and o3[2] == 3, o3
    print(f"TITLE: {o3[0]} (p{o3[2]})")
    for t, m, _ in (o[0], o3, price_msg(cfg, "dn320", b("x", 3.1, 3.2, 3.0), 3.3, stamp), price_msg(cfg, "up415", b("x", 4.2), 4.0, stamp)):
        assert not any(ch in t + m for ch in BAD) and route_topic("cwally-port", t) == "cwally-port" and t.startswith("[GRAB] GRAB"), t
    for tail in ("시세 조회 실패 | 가격선 판정 못 함", "뉴스 조회 실패 | 규제 뉴스 판정 못 함"):
        t, m, _ = ops_msg(stamp, tail, "x")
        assert route_topic("cwally-port", t) == "cwally-ops", t
    assert route_topic(resolve_topic("ops"), ops_msg(stamp, "스크립트 오류 | 이번 실행 판정 못 함", "x")[0]) == "cwally-ops"
    print("ops notices route to cwally-ops")
    print("SELFTEST OK: fixtures only, no network, no publish, no state")
    return 0


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    if mode == "selftest":
        return selftest(cfg)
    if mode not in {"auto", "dry"}:
        print(f"unknown mode {mode}")
        return 2
    now = datetime.now(KST)
    try:
        return run(mode, now, cfg)
    except Exception as exc:  # noqa: BLE001
        print(traceback.format_exc(), file=sys.stderr)
        if mode == "dry":
            return 1
        try:
            st = load_state()
            if st.get("crash_day") == now.strftime("%Y-%m-%d"):
                return 0
            t, m, p = ops_msg(now.strftime("%Y-%m-%d %H:%M KST"), "스크립트 오류 | 이번 실행 판정 못 함", f"{type(exc).__name__}: {str(exc)[:200]}")
            publish(resolve_topic("ops"), t, m, p, ["warning"], resolve_server(cfg.get("ntfy_server")))
            st["crash_day"] = now.strftime("%Y-%m-%d")
            STATE_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        except Exception:  # noqa: BLE001
            return 1
        return 0


if __name__ == "__main__":
    sys.exit(main())
