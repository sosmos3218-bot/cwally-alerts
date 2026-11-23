#!/usr/bin/env python3
"""[MARKET] 장중 시장 점검: 반도체, 전력기기 수급 + 주요 종목 가격 + 메타 뮤즈 코멘트.

Trigger (approved in chat 2026-10-22 09:43 KST; reduced to once a day at 11:00 KST in chat 2026-10-26):
a recurring cron-job.org job (weekdays 11 Asia/Seoul) dispatches port-alerts.yml with {"job":"oneshot"}; scripts/oneshot_pick.py picks the
due `oneshot_kst` entry of configs/mktpulse.json and runs this script with `auto`. There is no
GitHub schedule cron (2-8 h late, useless intraday).

modes:
  auto     send once per slot. Silent (exit 0) on a non-trading day, after 15:30 KST, outside the
           year guard / ends_kst, or more than stale_min (45) minutes after the nearest slot.
  dry      fetch and print the message it would send. Never calls publish(), never writes state.
  force    send now even off-slot (manual retry). Writes state.
  install  one install-confirmation send `[MARKET] KOSPI/KOSDAQ 설치 확인 | 매매 신호 아님`. Never writes state.
  self-test  offline checks of the filters, sanitizer and slot guard (no network, no send).
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from kr_calendar import is_trading_day  # noqa: E402
from ntfy_util import publish, resolve_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parents[1]
CFG_PATH = ROOT / "configs" / "mktpulse.json"
STATE_PATH = ROOT / "state" / "mktpulse.json"
SKIPS_KEEP = 30
PREFIX = "[MARKET]"
UA = {"User-Agent": "Mozilla/5.0 (cwally-alerts; +mailto:sosmos3218@gmail.com)"}


# ---------------------------------------------------------------- helpers
def sanitize(text: str) -> str:
    """No middle dot / em-dash in Korean copy (AGENTS.md copy style, user rule)."""
    text = unescape(text or "")
    text = re.sub(r"\s*[·ㆍ•‧∙]\s*", ", ", text)
    text = re.sub(r"\s*[—―–]\s*", " ", text)
    text = text.replace("…", "...")
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"[ \t]+", " ", text).strip()


def num(v) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(str(v).replace(",", "").replace("+", "").replace("%", "").strip())
    except ValueError:
        return None


def get(url: str, timeout: int = 15) -> bytes:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def get_json(url: str, timeout: int = 15):
    return json.loads(get(url, timeout).decode("utf-8", errors="ignore"))


def signed(v: float, unit: str = "", digits: int = 0) -> str:
    s = f"{v:+,.{digits}f}"
    return s + unit


def eok_from_won(won: float) -> str:
    if abs(won) >= 1e12:
        return f"{won / 1e12:+.2f}조"
    return f"{won / 1e8:+,.0f}억"


def norm_title(title: str) -> str:
    t = re.sub(r"\s+-\s+[^-]{1,40}$", "", title)  # Google News " - 매체" suffix
    t = re.sub(r"[^0-9A-Za-z가-힣]", "", t).lower()
    return hashlib.sha1(t.encode()).hexdigest()[:16]


# ---------------------------------------------------------------- config / state
def load_cfg() -> dict:
    return json.loads(CFG_PATH.read_text(encoding="utf-8"))


def load_state() -> dict:
    base = {"seen": {}, "research_checked": {}, "slots_sent": {}, "last_flow": {}}
    try:
        raw = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            base.update(raw)
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return base


def prune(state: dict, now: datetime, keep_days: int) -> None:
    cutoff = (now - timedelta(days=keep_days)).strftime("%Y-%m-%d")
    for k in ("seen", "research_checked", "slots_sent"):
        state[k] = {key: d for key, d in (state.get(k) or {}).items() if str(d)[:10] >= cutoff}


def save_state(state: dict, now: datetime, keep_days: int) -> None:
    prune(state, now, keep_days)
    state["updated_at"] = now.isoformat(timespec="seconds")
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")


# ---------------------------------------------------------------- slot guard
def current_slot(cfg: dict, now: datetime) -> tuple[str | None, str]:
    """(slot 'YYYY-MM-DD HH:MM' or None, reason)."""
    years = cfg.get("year_guard") or [now.year]
    if isinstance(years, int):
        years = [years]
    if now.year not in years:
        return None, f"year guard {cfg['year_guard']} (now {now.year})"
    ends = cfg.get("ends_kst")
    if ends and now > datetime.strptime(ends, "%Y-%m-%d %H:%M").replace(tzinfo=KST):
        return None, f"after ends_kst {ends}"
    if not is_trading_day(now.date()):
        return None, f"{now.date()} is not a KRX trading day"
    if now.strftime("%H:%M") >= cfg.get("market_close_kst", "15:30"):
        return None, "after 15:30 KST market close"
    stale = timedelta(minutes=int(cfg.get("stale_min", 45)))
    best = None
    for raw in cfg.get("oneshot_kst") or []:
        at = datetime.strptime(raw, "%Y-%m-%d %H:%M").replace(tzinfo=KST)
        if at <= now <= at + stale:
            best = raw
    if not best:
        return None, f"no slot within {stale.seconds // 60} min before {now:%H:%M}"
    return best, "ok"


def next_slot(cfg: dict, slot: str | None, now: datetime) -> str | None:
    ref = slot or now.strftime("%Y-%m-%d %H:%M")
    later = [s for s in cfg.get("oneshot_kst") or [] if s > ref]
    return later[0] if later else None


# ---------------------------------------------------------------- data
def market_flow(market: str, day: str) -> dict | None:
    raw = get_json(f"https://m.stock.naver.com/api/index/{market}/trend?bizdate={day}")
    if not isinstance(raw, dict) or str(raw.get("bizdate")) != day:
        return None
    vals = {"외국인": num(raw.get("foreignValue")), "기관": num(raw.get("institutionalValue")),
            "개인": num(raw.get("personalValue"))}
    if any(v is None for v in vals.values()):
        return None
    return vals  # 억원


def sector(no: int) -> dict | None:
    raw = get_json(f"https://m.stock.naver.com/api/stocks/industry/{no}?page=1&pageSize=1")
    g = (raw or {}).get("groupInfo") or {}
    if num(g.get("changeRate")) is None:
        return None
    return {"name": g.get("name"), "chg": num(g.get("changeRate")), "up": g.get("riseCount"),
            "down": g.get("fallCount"), "flat": g.get("steadyCount")}


def prices(codes: list[str]) -> dict:
    raw = get_json("https://polling.finance.naver.com/api/realtime/domestic/stock/" + ",".join(codes))
    out = {}
    for d in (raw or {}).get("datas") or []:
        out[str(d.get("itemCode"))] = {
            "name": d.get("stockName"), "price": num(d.get("closePrice")),
            "diff": num(d.get("compareToPreviousClosePrice")), "pct": num(d.get("fluctuationsRatio")),
            "at": str(d.get("localTradedAt") or "")[:16], "status": d.get("marketStatus"),
        }
    return out


def stock_flow(code: str, today: str, live_price: float | None) -> dict | None:
    rows = get_json(f"https://m.stock.naver.com/api/stock/{code}/trend?pageSize=3")
    rows = [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []
    if not rows:
        return None
    r = rows[0]
    day = str(r.get("bizdate") or "")
    provisional = day == today
    close = live_price if (provisional and live_price) else num(r.get("closePrice"))
    fq, oq = num(r.get("foreignerPureBuyQuant")), num(r.get("organPureBuyQuant"))
    if fq is None or oq is None or not close:
        return None
    three_f = sum((num(x.get("foreignerPureBuyQuant")) or 0) * (num(x.get("closePrice")) or 0) for x in rows[:3])
    three_o = sum((num(x.get("organPureBuyQuant")) or 0) * (num(x.get("closePrice")) or 0) for x in rows[:3])
    return {"day": day, "provisional": provisional, "f_won": fq * close, "o_won": oq * close,
            "f3_won": three_f, "o3_won": three_o, "days3": len(rows[:3])}


def research_hits(cfg: dict, state: dict, now: datetime, log: list[str]) -> list[dict]:
    m = cfg["muse"]
    muse_re, meta_re = re.compile(m["muse_re"]), re.compile(m["meta_re"], re.I)
    since = (now - timedelta(days=int(m["research_days"]))).strftime("%Y-%m-%d")
    checked = state.get("research_checked") or {}
    seen = state.get("seen") or {}
    cands = []
    for kind in ("industry", "company"):
        for page in range(1, int(m["research_pages"]) + 1):
            rows = get_json(f"https://m.stock.naver.com/api/research/{kind}?page={page}&pageSize=50")
            for r in rows if isinstance(rows, list) else []:
                wd = str(r.get("writeDate") or "")
                if wd < since:
                    continue
                if kind == "industry" and r.get("category") not in m["research_categories"] and not muse_re.search(str(r.get("title"))):
                    continue
                key = f"r:{kind}:{r.get('researchId')}"
                if key in checked or key in seen:
                    continue
                cands.append((kind, key, r))
    hits = []
    for kind, key, r in cands:
        try:
            d = (get_json(f"https://m.stock.naver.com/api/research/{kind}/{r['researchId']}") or {}).get("researchContent") or {}
        except Exception as exc:  # one report failing must not drop the rest
            log.append(f"research {r.get('researchId')} {type(exc).__name__}")
            continue
        text = sanitize(re.sub(r"<[^>]+>", " ", str(d.get("content") or "")))
        title = sanitize(str(r.get("title") or ""))
        blob = title + " " + text
        mm = muse_re.search(blob)
        # Only non-hits go to research_checked. A hit stays unchecked so that, if the max_items cap
        # leaves it unshown, the next run returns it again; once shown it is in `seen` (skipped above).
        # It drops out by itself when its writeDate leaves research_days.
        if not (mm and meta_re.search(blob)):
            checked[key] = now.strftime("%Y-%m-%d")
        if mm and meta_re.search(blob):
            i = mm.start() - len(title) - 1  # offset inside text; <0 = the hit is in the title
            snippet = text[max(0, i - 40): i + 70].strip() if i >= 0 else ""
            hits.append({"key": key, "kind": "리포트", "title": title, "who": str(r.get("brokerName") or ""),
                         "date": str(r.get("writeDate") or "")[5:].replace("-", "/"), "sort_at": str(r.get("writeDate") or "")[:10],
                         "snippet": sanitize(snippet),
                         "url": f"https://m.stock.naver.com/research/{kind}/{r['researchId']}"})
        time.sleep(0.1)
    state["research_checked"] = checked
    return hits


def news_hits(cfg: dict, now: datetime, log: list[str]) -> list[dict]:
    m = cfg["muse"]
    muse_re, meta_re = re.compile(m["muse_re"]), re.compile(m["meta_re"], re.I)
    topic_re, block_re = re.compile(m["topic_re"], re.I), re.compile(m["block_re"])
    out, seen_local = [], set()
    for q in m["news_queries"]:
        url = "https://news.google.com/rss/search?" + urllib.parse.urlencode(
            {"q": q["q"], "hl": q["hl"], "gl": q["gl"], "ceid": q["ceid"]})
        try:
            xml = get(url).decode("utf-8", errors="ignore")
        except Exception as exc:
            log.append(f"news {q['hl']} {type(exc).__name__}")
            continue
        for it in re.findall(r"<item>(.*?)</item>", xml, re.S):
            t = re.search(r"<title>(.*?)</title>", it, re.S)
            src = re.search(r"<source[^>]*>(.*?)</source>", it, re.S)
            pub = re.search(r"<pubDate>(.*?)</pubDate>", it)
            guid = re.search(r"<guid[^>]*>(.*?)</guid>", it, re.S)
            if not t:
                continue
            title = sanitize(t.group(1))
            source = sanitize(src.group(1)) if src else ""
            if source and title.endswith(" - " + source):
                title = title[: -len(source) - 3].strip()
            if source in m["block_sources"] or block_re.search(title):
                continue
            if not (muse_re.search(title) and meta_re.search(title + " " + source) and topic_re.search(title)):
                continue
            try:
                when = parsedate_to_datetime(pub.group(1)).astimezone(KST) if pub else None
            except (TypeError, ValueError):
                when = None
            if when and when < now - timedelta(hours=48):
                continue
            h = norm_title(title)
            if h in seen_local:
                continue
            seen_local.add(h)
            out.append({"key": f"n:{h}", "guid": f"g:{(guid.group(1) if guid else h)[:80]}", "kind": "뉴스",
                        "title": title, "who": source, "date": when.strftime("%m/%d %H:%M") if when else "",
                        "sort_at": when.strftime("%Y-%m-%d %H:%M") if when else "", "snippet": ""})
    return out


# ---------------------------------------------------------------- 2026-11-18 additions
# 2026-11-18 유용성 검토(10/26~11/17 실제 발송 20건): 전력기기 바스켓 수급(전일) 줄은 잡음 11, 쓸모 5 이고
# 쓸모 있음 사례 5건 모두 전기장비 업종 줄도 같은 방향이라 유일 신호였던 적이 없어 뺐다. config 플래그가 없어 코드에서 끈다.
FLOW_BASKETS_OFF = {"전력기기"}
NIX_CFG_PATH = ROOT / "configs" / "000660.json"
NIXEPS_STATE_PATH = ROOT / "state" / "nixeps.json"


def load_ladder(log: list[str]) -> list[tuple[str, float]]:
    """[000660] 매수 사다리(lines 중 key buy*, op le)를 config 에서 읽는다. 하드코딩 없음."""
    try:
        raw = json.loads(NIX_CFG_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        log.append(f"000660 config {type(exc).__name__}")
        return []
    out = []
    for ln in raw.get("lines") or []:
        if str(ln.get("key", "")).startswith("buy") and ln.get("op") == "le" and num(ln.get("level")):
            out.append((str(ln["key"]), float(num(ln["level"]))))
    return sorted(out, key=lambda kv: -kv[1])


def ladder_line(ladder: list[tuple[str, float]], price: float | None, log: list[str]) -> str:
    head = "- 000660 매수 사다리 거리(장중 기준이며 [000660]은 KRX 종가에 발화): "
    if not ladder:
        return head + "확인 불가(configs/000660.json 사다리 없음)"
    if not price:
        log.append("000660 장중가 없음")
        return head + "확인 불가(장중가 조회 실패)"
    parts = []
    for i, (_key, lvl) in enumerate(ladder, 1):
        d = (price / lvl - 1) * 100
        parts.append(f"{i}차 {lvl:,.0f}원 " + (f"장중 도달(선 대비 {d:+.1f}%)" if price <= lvl else f"선 대비 {d:+.1f}%"))
    return head + f"현재 {price:,.0f}원 기준 남은 거리, " + ", ".join(parts)


def load_nixeps_active(log: list[str]) -> bool | None:
    try:
        st = json.loads(NIXEPS_STATE_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        log.append(f"nixeps state {type(exc).__name__}")
        return None
    return bool((st.get("episode") or {}).get("active"))


def supply_light(kospi_foreign: float | None, stock_foreign: dict[str, float | None], pcts: dict[str, float | None],
                 scores: dict) -> tuple[float | None, str]:
    """수급 등 장중 재계산. 외국인 방향(코스피 잠정 + 종목 잠정이 있으면 종목) 과 두 종목 장중 등락률 평균."""
    dirs = [v for v in [kospi_foreign, *stock_foreign.values()] if v is not None]
    pv = [v for v in pcts.values() if v is not None]
    if not dirs or len(pv) < len(pcts):
        return None, "확인 불가"
    sell = sum(1 for v in dirs if v < 0) > len(dirs) / 2
    buy = sum(1 for v in dirs if v > 0) > len(dirs) / 2
    avg = sum(pv) / len(pv)
    if sell and avg < 0:
        return float(scores.get("warn", 1.0)), "경고(외국인 순매도, 주가 하락)"
    if buy and avg >= 0:
        return float(scores.get("ok", 0.0)), "양호(외국인 순매수, 주가 상승)"
    return float(scores.get("mixed", 0.5)), "혼조"


def semi_light_line(cfg: dict, flows: dict, px: dict, stock_rows: dict, nix_active: bool | None, log: list[str]) -> str:
    sl = cfg.get("semi_light") or {}
    codes = sl.get("codes") or ["005930", "000660"]
    kf = (flows.get("KOSPI") or {}).get("외국인")
    if kf is None:
        log.append("경고등: 코스피 잠정 외국인 없음")
    sf = {c: (stock_rows[c]["f_won"] if c in stock_rows and stock_rows[c].get("provisional") else None) for c in codes}
    if all(v is None for v in sf.values()):
        log.append("경고등 종목별 장중 외국인(네이버 trend 당일 행 없음, 코스피 잠정으로 대신)")
    pcts = {c: (px.get(c) or {}).get("pct") for c in codes}
    s_score, s_why = supply_light(kf, sf, pcts, sl.get("supply_scores") or {})
    base = sl.get("baseline") or {}
    others = []
    total = s_score
    for key, label in (("earnings", "실적"), ("valuation", "밸류"), ("narrative", "내러티브")):
        v = num(base.get(key))
        others.append(f"{label} {v:g}" if v is not None else f"{label} 확인 불가")
        total = None if (v is None or total is None) else total + v
    pct_txt = ", ".join(f"{c} {signed(v, '%', 2)}" if v is not None else f"{c} 확인 불가" for c, v in pcts.items())
    s_txt = f"수급 {s_score:g}({s_why}; 코스피 외국인 {signed(kf, '억') if kf is not None else '확인 불가'}, {pct_txt})" \
        if s_score is not None else f"수급 확인 불가({pct_txt})"
    bdate = f"기준값 {base['as_of']} 판정" if base.get("as_of") else "기준값: 판정 기록 없음"
    warn, hard = sl.get("warn_at", 2.0), sl.get("hard_at", 2.5)
    tot_txt = f"합계 {total:.1f} (기준 {float(warn):.1f}, {float(hard):.1f})" if total is not None else f"합계 확인 불가 (기준 {float(warn):.1f}, {float(hard):.1f})"
    if nix_active or (total is not None and total >= float(warn)):
        verdict = "SK하이닉스 신규 진입 동결 조건 충족" + (" ([NIX-EPS] 에피소드 활성)" if nix_active else "")
    elif total is None:
        verdict = "점수 조건 판단 불가, [NIX-EPS] 에피소드 " + ("없음" if nix_active is False else "확인 불가")
    else:
        verdict = "동결 조건 아님"
    return f"- 반도체 경고등 장중 점수 (005930, 000660): {s_txt}, {', '.join(others)} ({bdate}). {tot_txt}. {verdict}"


# ---------------------------------------------------------------- message
def fmt_flow(f: dict) -> str:
    return ", ".join(f"{k} {signed(v, '억')}" for k, v in f.items())


def pick_muse(items: list[dict], seen: dict, max_items: int) -> tuple[list[dict], list[dict]]:
    """(new, shown): unseen items, reports first, newest first within kind; shown = first max_items."""
    new = [i for i in items if i["key"] not in seen and i.get("guid", "") not in seen]
    new.sort(key=lambda i: i.get("sort_at") or "", reverse=True)  # sort_at = YYYY-MM-DD[ HH:MM]; empty last
    new.sort(key=lambda i: i["kind"] != "리포트")  # stable: keeps newest-first inside each kind
    return new, new[: max(0, int(max_items))]


def mark_seen(seen: dict, shown: list[dict], stamp: str) -> dict:
    """Mark only the items that were in the message; the rest stay unseen for the next slot."""
    for i in shown:
        seen[i["key"]] = stamp
        if i.get("guid"):
            seen[i["guid"]] = stamp
    return seen


def carry_line(left: int, slot: str | None, nxt: str | None) -> str:
    if left <= 0:
        return ""
    if not nxt:
        return f"  외 {left}건은 이번이 마지막 점검이라 따로 보여 드리지 않습니다. 원문 검색으로 확인하세요."
    if slot and nxt[:10] == slot[:10]:
        return f"  외 {left}건은 다음 점검에서 이어서 보여 드립니다."
    return f"  외 {left}건은 다음 거래일 점검({nxt[5:].replace('-', '/')} KST)에서 이어서 보여 드립니다."


def build(cfg: dict, state: dict, now: datetime, slot: str | None, mode: str) -> tuple[str, str, dict, list[dict]]:
    today = now.strftime("%Y%m%d")
    log: list[str] = []
    stamp = now.strftime("%m/%d %H:%M")
    lines: list[str] = []
    flows = {}
    for mk in cfg["markets"]:
        try:
            flows[mk] = market_flow(mk, today)
        except Exception as exc:
            flows[mk] = None
            log.append(f"{mk} 수급 {type(exc).__name__}")
    flow_lines = []
    for mk, label in (("KOSPI", "코스피"), ("KOSDAQ", "코스닥")):
        f = flows.get(mk)
        if f is None:
            flow_lines.append(f"- {label} 수급: 아직 없음(장 초반 또는 조회 실패)")
            continue
        # 하루 1회(11:00)라 같은 날 직전 점검이 없다. '직전 대비' 줄은 2026-10-26 에 뺐다.
        flow_lines.append(f"- {label} 수급(잠정, 억원): {fmt_flow(f).replace('억', '')}")

    secs = []
    for s in cfg["sectors"]:
        try:
            g = sector(int(s["no"]))
        except Exception as exc:
            g = None
            log.append(f"업종 {s['no']} {type(exc).__name__}")
        secs.append((s["name"], g))
    # 2026-11-18 유용성 검토: 가격 계열(쓸모 있음 76%)을 수급 계열(54%)보다 먼저 보여 준다.
    lines.append("- 업종: " + ", ".join(
        f"{n} {signed(g['chg'], '%', 2)} (상승 {g['up']}, 하락 {g['down']})" if g else f"{n} 조회 실패" for n, g in secs))

    codes = [x["code"] for b in cfg["baskets"].values() for x in b]
    try:
        px = prices(codes)
    except Exception as exc:
        px = {}
        log.append(f"현재가 {type(exc).__name__}")
    for bname, members in cfg["baskets"].items():
        parts = []
        for x in members:
            p = px.get(x["code"])
            parts.append(f"{x['name']} {p['price']:,.0f}원 {signed(p['pct'], '%', 2)}" if p and p["price"] else f"{x['name']} 조회 실패")
        lines.append(f"- {bname} 가격: " + ", ".join(parts))
    items_on = cfg.get("items") or {}
    if items_on.get("nix_ladder", True):
        lines.append(ladder_line(load_ladder(log), (px.get("000660") or {}).get("price"), log))

    lines += flow_lines
    provisional_seen = []
    stock_rows: dict[str, dict] = {}
    for bname, members in cfg["baskets"].items():
        if bname in FLOW_BASKETS_OFF:
            continue
        tot_f = tot_o = tot_f3 = tot_o3 = 0.0
        days, got, per = set(), 0, []
        for x in members:
            try:
                fl = stock_flow(x["code"], today, (px.get(x["code"]) or {}).get("price"))
            except Exception as exc:
                fl = None
                log.append(f"{x['code']} 수급 {type(exc).__name__}")
            print(f"trend {x['code']}: latest={fl and fl['day']} provisional_today={bool(fl and fl['provisional'])}")
            if not fl:
                continue
            stock_rows[x["code"]] = fl
            got += 1
            days.add(fl["day"] + ("(잠정)" if fl["provisional"] else ""))
            if fl["provisional"]:
                provisional_seen.append(x["code"])
            tot_f += fl["f_won"]; tot_o += fl["o_won"]; tot_f3 += fl["f3_won"]; tot_o3 += fl["o3_won"]
            per.append(f"{x['name']} 외국인 {eok_from_won(fl['f_won'])}")
        if not got:
            lines.append(f"- {bname} 바스켓 수급: 조회 실패")
            continue
        dlabel = ", ".join(sorted(f"{d[4:6]}/{d[6:8]}{d[8:]}" for d in days))
        lines.append(f"- {bname} 바스켓 수급({dlabel} 기준, 추정): 외국인 {eok_from_won(tot_f)}, 기관 {eok_from_won(tot_o)}"
                     f" (3일 합 외국인 {eok_from_won(tot_f3)}, 기관 {eok_from_won(tot_o3)}; {', '.join(per)})")
    if items_on.get("semi_light", True):
        lines.append(semi_light_line(cfg, flows, px, stock_rows, load_nixeps_active(log), log))

    muse_items: list[dict] = []
    try:
        muse_items += research_hits(cfg, state, now, log)
    except Exception as exc:
        log.append(f"리포트 목록 {type(exc).__name__}")
    try:
        muse_items += news_hits(cfg, now, log)
    except Exception as exc:
        log.append(f"뉴스 {type(exc).__name__}")
    new, shown = pick_muse(muse_items, state.get("seen") or {}, int(cfg["muse"]["max_items"]))
    nxt = next_slot(cfg, slot, now)
    if not shown:
        lines.append("- 뮤즈 코멘트: 새 코멘트 없음")
    else:
        lines.append(f"- 뮤즈 새 코멘트 {len(new)}건:")
        for i in shown:
            tail = f" ({i['snippet']})" if i.get("snippet") else ""
            lines.append(f"  [{i['kind']} {i['who']} {i['date']}] {i['title']}{tail}")
        if len(new) > len(shown):
            lines.append(carry_line(len(new) - len(shown), slot, nxt))
    if log:
        lines.append("- 일부 조회 실패: " + "; ".join(log[:4]))

    hh = (slot or now.strftime("%Y-%m-%d %H:%M"))[11:13].lstrip("0")
    semi = next((g for n, g in secs if n == "반도체"), None)
    elec = next((g for n, g in secs if n == "전기장비"), None)
    kf = (flows.get("KOSPI") or {}).get("외국인")
    tparts = []
    if semi:
        tparts.append(f"반도체 {signed(semi['chg'], '%', 2)}")
    if elec:
        tparts.append(f"전기장비 {signed(elec['chg'], '%', 2)}")
    if kf is not None:
        tparts.append(f"외국인 코스피 {signed(kf, '억')}")
    if shown:
        tparts.append(f"뮤즈 새 코멘트 {len(new)}건")
    title = f"{PREFIX} KOSPI/KOSDAQ {hh}시 " + (", ".join(tparts) if tparts else "시장 점검") + " | 확인만"

    later = (f"다음 점검 {nxt[5:].replace('-', '/')} KST. " if nxt else "이번이 마지막 점검입니다. ") + \
        "확정 수급은 장 마감 뒤 15:40 [FLOW] 알림에서 봅니다."
    body = "\n".join([
        "이게 뭐 알림: 장중 반도체, 전력기기 수급과 주요 종목 가격, 메타 뮤즈 관련 새 증권사 코멘트 점검입니다. 매매 신호가 아닙니다.",
        f"지금 결과/상태 ({stamp} KST 조회, 장중 수급은 잠정치):",
        *lines,
        "지금 할 일: 장 마감 뒤 15:40 [FLOW] 확정 수급에서 외국인 방향이 이어지는지만 보세요. 뮤즈 코멘트는 원문을 열어 메모리 수요 언급인지 확인하세요.",
        f"나중에: {later}",
        "익절/줄일 때: 이 알림만으로 정하지 않습니다. 보유 종목은 각 종목 알림 기준을 따르세요.",
        "하지 말 것: 장중 잠정 수급으로 추격매수. 업종 등락을 종목 수급으로 읽기. 뉴스 제목만 보고 매매. 투자 권유 아님.",
    ])
    snap = {mk: {"day": today, "slot": slot or now.strftime("%Y-%m-%d %H:%M"), "foreign": (f or {}).get("외국인")}
            for mk, f in flows.items() if f}
    print(f"provisional per-stock rows today: {provisional_seen or 'none'}")
    if log:
        print("fetch notes:", "; ".join(log))
    return sanitize_lines(title), sanitize_lines(body), snap, shown


def sanitize_lines(text: str) -> str:
    return "\n".join(re.sub(r"\s*[·ㆍ•‧∙]\s*", ", ", re.sub(r"\s*[—―–]\s*", " ", ln)).replace("…", "...") for ln in text.split("\n"))


# ---------------------------------------------------------------- main
def install(cfg: dict) -> int:
    body = "\n".join([
        "이게 뭐 알림: [MARKET] 장중 시장 점검 알림 설치 확인입니다. 매매 신호가 아닙니다.",
        "지금 결과/상태: 거래일 하루 1회 11:00 KST에 코스피, 코스닥 잠정 수급, 반도체와 전기장비 업종 등락,"
        " 삼성전자, SK하이닉스, LS ELECTRIC 등 바스켓 가격과 수급, 메타 뮤즈 관련 새 증권사 코멘트를 보냅니다.",
        "지금 할 일: 설치 확인만 하세요.",
        "나중에: 정시 실행은 cron-job.org 반복 잡이 맡습니다. 조회 실패 안내는 cwally-ops로 갑니다.",
        "익절/줄일 때: 이 알림으로 정하지 않습니다.",
        "하지 말 것: 설치 확인을 매수 신호로 읽기.",
    ])
    publish(resolve_topic("port", cfg.get("ntfy_topic")), f"{PREFIX} KOSPI/KOSDAQ 설치 확인 | 매매 신호 아님", body, 3,
            ["information_source"], cfg.get("ntfy_server"), config_name="mktpulse")
    print("install confirmation sent (state untouched)")
    return 0


def self_test() -> int:
    cfg = load_cfg()
    fails = 0

    def check(label, got, want):
        nonlocal fails
        ok = got == want
        fails += not ok
        print(("PASS " if ok else "FAIL ") + f"{label}: got={got!r} want={want!r}")

    check("sanitize middle dot", sanitize("삼성·SK 반등"), "삼성, SK 반등")
    check("sanitize em-dash", sanitize("메모리—수요"), "메모리 수요")
    t = lambda s: datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=KST)  # noqa: E731
    check("slot on time", current_slot(cfg, t("2026-10-23 11:05"))[0], "2026-10-23 11:00")
    check("slot 45 min edge", current_slot(cfg, t("2026-10-23 11:45"))[0], "2026-10-23 11:00")
    check("slot stale 46 min", current_slot(cfg, t("2026-10-23 11:46"))[0], None)
    check("after close", current_slot(cfg, t("2026-10-23 15:31"))[0], None)
    check("weekend", current_slot(cfg, t("2026-10-24 11:05"))[0], None)
    check("after ends", current_slot(cfg, t("2027-06-30 11:50"))[0], None)
    check("year guard", current_slot(cfg, t("2028-06-30 11:05"))[0], None)
    check("2027 still in year_guard before ends", current_slot(cfg, t("2027-06-30 11:05"))[0], "2027-06-30 11:00")
    check("13:00 slot removed", current_slot(cfg, t("2026-10-27 13:05"))[0], None)
    check("10:00 slot removed", current_slot(cfg, t("2026-10-27 10:05"))[0], None)
    check("11/5 11:00 still a slot", current_slot(cfg, t("2026-11-05 11:05"))[0], "2026-11-05 11:00")
    check("11/19 11:00 still a slot", current_slot(cfg, t("2026-11-19 11:05"))[0], "2026-11-19 11:00")
    check("12/3 11:00 still a slot", current_slot(cfg, t("2026-12-03 11:05"))[0], "2026-12-03 11:00")
    check("last slot 2027-06-30 11:00", current_slot(cfg, t("2027-06-30 11:05"))[0], "2027-06-30 11:00")
    check("next after 10/30 is 11/2 11:00", next_slot(cfg, "2026-10-30 11:00", t("2026-10-30 11:05")), "2026-11-02 11:00")
    check("next after 11/5 is 11/6", next_slot(cfg, "2026-11-05 11:00", t("2026-11-05 11:05")), "2026-11-06 11:00")
    check("next after 11/19 is 11/20", next_slot(cfg, "2026-11-19 11:00", t("2026-11-19 11:05")), "2026-11-20 11:00")
    check("next after 12/3 is 12/4", next_slot(cfg, "2026-12-03 11:00", t("2026-12-03 11:05")), "2026-12-04 11:00")
    check("no next after 2027-06-30", next_slot(cfg, "2027-06-30 11:00", t("2027-06-30 11:05")), None)
    m = cfg["muse"]
    block = re.compile(m["block_re"])
    check("spam blocked", bool(block.search("베트맨 토토 앱 설정 변경 전 확인할 적용 시점")), True)
    ok_title = "메타 뮤즈 열풍에 D램 수요 늘어난다, 증권가 전망"
    check("filter keeps analyst/memory title", bool(re.search(m["muse_re"], ok_title) and re.search(m["meta_re"], ok_title, re.I)
                                                    and re.search(m["topic_re"], ok_title, re.I)), True)
    check("filter drops plain app news", bool(re.search(m["topic_re"], "메타 뮤즈 공유 링크 노출 논란", re.I)), False)
    check("Muse word boundary", bool(re.search(m["muse_re"], "Museum opening")), False)
    check("norm_title strips source", norm_title("메타 뮤즈 D램 - 한국경제") == norm_title("메타 뮤즈 D램"), True)
    # 2026-11-18: 000660 사다리 거리, 반도체 경고등 장중 점수, 전력기기 바스켓 수급 제거
    lad = load_ladder([])
    check("ladder read from 000660 config (3 buy lines, high first)", [k for k, _ in lad], ["buy1", "buy2", "buy3"])
    ll = ladder_line([("buy1", 1270000.0), ("buy2", 1160000.0), ("buy3", 1050000.0)], 1127000.0, [])
    check("ladder pct distance", "1차 1,270,000원 장중 도달(선 대비 -11.3%)" in ll and "3차 1,050,000원 선 대비 +7.3%" in ll, True)
    check("ladder notes close-based [000660]", "[000660]은 KRX 종가에 발화" in ll, True)
    check("ladder touched intraday", "장중 도달" in ladder_line([("buy1", 1270000.0)], 1200000.0, []), True)
    check("ladder no price -> 확인 불가", "확인 불가" in ladder_line([("buy1", 1270000.0)], None, []), True)
    sc = {"ok": 0.0, "mixed": 0.5, "warn": 1.0}
    check("supply warn", supply_light(-5000.0, {}, {"005930": -1.0, "000660": -2.0}, sc)[0], 1.0)
    check("supply ok", supply_light(1300.0, {}, {"005930": 0.2, "000660": 2.6}, sc)[0], 0.0)
    check("supply mixed", supply_light(1300.0, {}, {"005930": -1.0, "000660": -2.0}, sc)[0], 0.5)
    check("supply no price -> None", supply_light(1300.0, {}, {"005930": None, "000660": 1.0}, sc)[0], None)
    lcfg = {"semi_light": {"baseline": {"as_of": "2026-11-06", "earnings": 1.0, "valuation": 0.5, "narrative": 0.0}}}
    pxs = {"005930": {"pct": -1.0}, "000660": {"pct": -2.0}}
    line = semi_light_line(lcfg, {"KOSPI": {"외국인": -5000.0}}, pxs, {}, False, [])
    check("light total 2.5 -> freeze", "합계 2.5 (기준 2.0, 2.5)" in line and "신규 진입 동결 조건 충족" in line, True)
    line = semi_light_line({"semi_light": {}}, {"KOSPI": {"외국인": 1300.0}}, {"005930": {"pct": 0.2}, "000660": {"pct": 2.6}}, {}, False, [])
    check("light no baseline -> 확인 불가, no freeze", "합계 확인 불가" in line and "동결 조건 충족" not in line and "실적 확인 불가" in line, True)
    line = semi_light_line({"semi_light": {}}, {"KOSPI": {"외국인": 1300.0}}, {"005930": {"pct": 0.2}, "000660": {"pct": 2.6}}, {}, True, [])
    check("light NIX-EPS active -> freeze", "신규 진입 동결 조건 충족 ([NIX-EPS] 에피소드 활성)" in line, True)
    check("power basket flow line off", "전력기기" in FLOW_BASKETS_OFF and "반도체" not in FLOW_BASKETS_OFF, True)
    check("new item flags default on", (cfg.get("items") or {}).get("semi_light") is True and (cfg.get("items") or {}).get("nix_ladder") is True, True)
    # muse cap / order / carryover
    check("muse max_items is 3", int(m["max_items"]), 3)
    items = [
        {"key": "n:a", "guid": "g:a", "kind": "뉴스", "date": "10/26 09:00", "sort_at": "2026-10-26 09:00"},
        {"key": "r:old", "kind": "리포트", "date": "10/23", "sort_at": "2026-10-23"},
        {"key": "n:b", "guid": "g:b", "kind": "뉴스", "date": "10/25 22:00", "sort_at": "2026-10-25 22:00"},
        {"key": "r:new", "kind": "리포트", "date": "10/26", "sort_at": "2026-10-26"},
        {"key": "n:c", "guid": "g:c", "kind": "뉴스", "date": "", "sort_at": ""},
        {"key": "n:x", "guid": "g:x", "kind": "뉴스", "date": "10/26 10:00", "sort_at": "2026-10-26 10:00"},
    ]
    new, shown = pick_muse(items, {"g:x": "2026-10-26"}, m["max_items"])
    check("order reports first, newest first", [i["key"] for i in new], ["r:new", "r:old", "n:a", "n:b", "n:c"])
    check("cap 3", [i["key"] for i in shown], ["r:new", "r:old", "n:a"])
    seen = mark_seen({"g:x": "2026-10-26"}, shown, "2026-10-26")
    check("carryover marks only shown", sorted(seen), ["g:a", "g:x", "n:a", "r:new", "r:old"])
    new2, shown2 = pick_muse(items, seen, m["max_items"])
    check("carried items show next run", [i["key"] for i in shown2], ["n:b", "n:c"])
    check("carry line same day", carry_line(2, "2026-10-26 11:00", "2026-10-26 13:00"), "  외 2건은 다음 점검에서 이어서 보여 드립니다.")
    check("carry line next trading day", carry_line(2, "2026-10-30 11:00", "2026-11-02 11:00"),
          "  외 2건은 다음 거래일 점검(11/02 11:00 KST)에서 이어서 보여 드립니다.")
    check("carry line last slot", "마지막" in carry_line(1, "2026-11-19 11:00", None), True)
    check("carry line none", carry_line(0, "2026-10-26 11:00", None), "")
    # research_checked must not suppress an unshown hit
    global get_json
    real_get_json = get_json
    fake = {
        "https://m.stock.naver.com/api/research/industry?page=1&pageSize=50": [
            {"researchId": 1, "writeDate": "2026-10-26", "category": "반도체", "title": "메모리 업황", "brokerName": "A증권"},
            {"researchId": 2, "writeDate": "2026-10-26", "category": "반도체", "title": "파운드리", "brokerName": "B증권"}],
        "https://m.stock.naver.com/api/research/industry/1": {"researchContent": {"content": "메타 뮤즈 수요로 D램 증가"}},
        "https://m.stock.naver.com/api/research/industry/2": {"researchContent": {"content": "뮤즈 언급 없음"}},
    }
    get_json = lambda url, timeout=15: fake.get(url, [])  # noqa: E731
    try:
        rcfg = {"muse": {**m, "research_pages": 1}}
        st = {"seen": {}, "research_checked": {}}
        now_t = t("2026-10-26 11:00")
        h1 = [i["key"] for i in research_hits(rcfg, st, now_t, [])]
        check("research hit found", h1, ["r:industry:1"])
        check("non-hit checked, hit not", sorted(st["research_checked"]), ["r:industry:2"])
        h2 = [i["key"] for i in research_hits(rcfg, st, now_t, [])]
        check("unshown research hit returns next run", h2, ["r:industry:1"])
        st["seen"]["r:industry:1"] = "2026-10-26"
        check("shown research hit skipped", research_hits(rcfg, st, now_t, []), [])
        check("research hit leaves research_days window", research_hits(rcfg, {"seen": {}, "research_checked": {}}, t("2026-10-30 11:00"), []), [])
    finally:
        get_json = real_get_json
    print("SELF-TEST " + ("PASS" if not fails else f"FAIL ({fails})"))
    return 1 if fails else 0


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").strip().lower()
    if mode in {"self-test", "selftest"}:
        return self_test()
    if mode not in {"auto", "dry", "force", "install"}:
        print(f"unknown mode {mode!r}; use auto | dry | force | install | self-test")
        return 2
    cfg = load_cfg()
    if mode == "install":
        return install(cfg)
    now = datetime.now(KST)
    dry = mode == "dry"
    if dry:
        print("DRY RUN: no ntfy, no state write")
    slot, why = current_slot(cfg, now)
    print(f"now={now:%Y-%m-%d %H:%M} KST slot={slot} ({why})")
    state = load_state()
    if mode == "auto":
        if not slot:
            # still silent on ntfy; leave a visible notice and a bounded skip record (last SKIPS_KEEP)
            print(f"::notice::mktpulse skip: {why}")
            skips = dict(state.get("skips") or {})
            skips[now.strftime("%Y-%m-%d %H:%M:%S")] = why
            state["skips"] = dict(sorted(skips.items())[-SKIPS_KEEP:])
            save_state(state, now, int(cfg["muse"]["keep_days"]))
            return 0
        if slot in (state.get("slots_sent") or {}):
            print(f"slot {slot} already sent; skip")
            return 0
    elif dry and not slot:
        print("outside slot; dry run continues for wiring check")
    title, body, snap, shown = build(cfg, state, now, slot, mode)
    if dry:
        print("DRY RUN (would send)", title, body, "dry-run; no ntfy, state untouched", sep="\n")
        return 0
    publish(resolve_topic("port", cfg.get("ntfy_topic")), title, body, 3, ["chart_with_upwards_trend"],
            cfg.get("ntfy_server"), config_name="mktpulse")
    stamp = now.strftime("%Y-%m-%d")
    # mark only after a real send, and only the shown items (the rest carry to the next slot)
    state["seen"] = mark_seen(state.get("seen") or {}, shown, stamp)
    state.setdefault("slots_sent", {})[slot or now.strftime("%Y-%m-%d %H:%M")] = stamp
    state["last_flow"] = {**(state.get("last_flow") or {}), **snap}
    save_state(state, now, int(cfg["muse"]["keep_days"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
