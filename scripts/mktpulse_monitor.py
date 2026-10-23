#!/usr/bin/env python3
"""[MARKET] 장중 시장 점검: 반도체, 전력기기 수급 + 주요 종목 가격 + 메타 뮤즈 코멘트.

Trigger (approved in chat 2026-10-22 09:43 KST): a recurring cron-job.org job (weekdays 10, 11, 13,
15 Asia/Seoul) dispatches port-alerts.yml with {"job":"oneshot"}; scripts/oneshot_pick.py picks the
due `oneshot_kst` entry of configs/mktpulse.json and runs this script with `auto`. There is no
GitHub schedule cron (2-8 h late, useless intraday).

modes:
  auto     send once per slot. Silent (exit 0) on a non-trading day, after 15:30 KST, outside the
           year guard / ends_kst, or more than stale_min (45) minutes after the nearest slot.
  dry      fetch and print the message it would send. Never calls publish(), never writes state.
  force    send now even off-slot (manual retry). Writes state.
  install  one install-confirmation send `[MARKET] 설치 확인 | 매매 신호 아님`. Never writes state.
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
    if now.year != int(cfg["year_guard"]):
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
                if key in checked:
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
        checked[key] = now.strftime("%Y-%m-%d")
        mm = muse_re.search(blob)
        if mm and meta_re.search(blob):
            i = mm.start() - len(title) - 1  # offset inside text; <0 = the hit is in the title
            snippet = text[max(0, i - 40): i + 70].strip() if i >= 0 else ""
            hits.append({"key": key, "kind": "리포트", "title": title, "who": str(r.get("brokerName") or ""),
                         "date": str(r.get("writeDate") or "")[5:].replace("-", "/"), "snippet": sanitize(snippet),
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
                        "title": title, "who": source, "date": when.strftime("%m/%d %H:%M") if when else "", "snippet": ""})
    return out


# ---------------------------------------------------------------- message
def fmt_flow(f: dict) -> str:
    return ", ".join(f"{k} {signed(v, '억')}" for k, v in f.items())


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
    prev = (state.get("last_flow") or {})
    for mk, label in (("KOSPI", "코스피"), ("KOSDAQ", "코스닥")):
        f = flows.get(mk)
        if f is None:
            lines.append(f"- {label} 수급: 아직 없음(장 초반 또는 조회 실패)")
            continue
        extra = ""
        p = prev.get(mk) or {}
        if p.get("day") == today and p.get("foreign") is not None:
            extra = f" (직전 {p.get('slot', '')[11:]} 대비 외국인 {signed(f['외국인'] - p['foreign'], '억')})"
        lines.append(f"- {label} 수급(잠정, 억원): {fmt_flow(f).replace('억', '')}{extra}")

    secs = []
    for s in cfg["sectors"]:
        try:
            g = sector(int(s["no"]))
        except Exception as exc:
            g = None
            log.append(f"업종 {s['no']} {type(exc).__name__}")
        secs.append((s["name"], g))
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

    provisional_seen = []
    for bname, members in cfg["baskets"].items():
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

    muse_items: list[dict] = []
    try:
        muse_items += research_hits(cfg, state, now, log)
    except Exception as exc:
        log.append(f"리포트 목록 {type(exc).__name__}")
    try:
        muse_items += news_hits(cfg, now, log)
    except Exception as exc:
        log.append(f"뉴스 {type(exc).__name__}")
    seen = state.get("seen") or {}
    new = [i for i in muse_items if i["key"] not in seen and i.get("guid", "") not in seen]
    new.sort(key=lambda i: (i["kind"] != "리포트", i["date"]), reverse=False)
    shown = new[: int(cfg["muse"]["max_items"])]
    if not shown:
        lines.append("- 뮤즈 코멘트: 새 코멘트 없음")
    else:
        lines.append(f"- 뮤즈 새 코멘트 {len(new)}건:")
        for i in shown:
            tail = f" ({i['snippet']})" if i.get("snippet") else ""
            lines.append(f"  [{i['kind']} {i['who']} {i['date']}] {i['title']}{tail}")
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
    title = f"{PREFIX} {hh}시 " + (", ".join(tparts) if tparts else "시장 점검") + " | 확인만"

    nxt = next_slot(cfg, slot, now)
    later = (f"다음 점검 {nxt[5:].replace('-', '/')} KST. " if nxt else "이번이 마지막 점검입니다. ") + \
        "확정 수급은 장 마감 뒤 15:40 [FLOW] 알림에서 봅니다."
    body = "\n".join([
        "이게 뭐 알림: 장중 반도체, 전력기기 수급과 주요 종목 가격, 메타 뮤즈 관련 새 증권사 코멘트 점검입니다. 매매 신호가 아닙니다.",
        f"지금 결과/상태 ({stamp} KST 조회, 장중 수급은 잠정치):",
        *lines,
        "지금 할 일: 다음 점검에서 외국인 방향이 이어지는지만 보세요. 뮤즈 코멘트는 원문을 열어 메모리 수요 언급인지 확인하세요.",
        f"나중에: {later}",
        "익절/줄일 때: 이 알림만으로 정하지 않습니다. 보유 종목은 각 종목 알림 기준을 따르세요.",
        "하지 말 것: 장중 잠정 수급으로 추격매수. 업종 등락을 종목 수급으로 읽기. 뉴스 제목만 보고 매매. 투자 권유 아님.",
    ])
    snap = {mk: {"day": today, "slot": slot or now.strftime("%Y-%m-%d %H:%M"), "foreign": (f or {}).get("외국인")}
            for mk, f in flows.items() if f}
    print(f"provisional per-stock rows today: {provisional_seen or 'none'}")
    if log:
        print("fetch notes:", "; ".join(log))
    return sanitize_lines(title), sanitize_lines(body), snap, new


def sanitize_lines(text: str) -> str:
    return "\n".join(re.sub(r"\s*[·ㆍ•‧∙]\s*", ", ", re.sub(r"\s*[—―–]\s*", " ", ln)).replace("…", "...") for ln in text.split("\n"))


# ---------------------------------------------------------------- main
def install(cfg: dict) -> int:
    body = "\n".join([
        "이게 뭐 알림: [MARKET] 장중 시장 점검 알림 설치 확인입니다. 매매 신호가 아닙니다.",
        "지금 결과/상태: 거래일 10:00, 11:00, 13:00, 15:00 KST에 코스피, 코스닥 잠정 수급, 반도체와 전기장비 업종 등락,"
        " 삼성전자, SK하이닉스, LS ELECTRIC 등 바스켓 가격과 수급, 메타 뮤즈 관련 새 증권사 코멘트를 보냅니다.",
        "지금 할 일: 설치 확인만 하세요.",
        "나중에: 정시 실행은 cron-job.org 반복 잡이 맡습니다. 조회 실패 안내는 cwally-ops로 갑니다.",
        "익절/줄일 때: 이 알림으로 정하지 않습니다.",
        "하지 말 것: 설치 확인을 매수 신호로 읽기.",
    ])
    publish(resolve_topic("port", cfg.get("ntfy_topic")), f"{PREFIX} 설치 확인 | 매매 신호 아님", body, 3,
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
    check("slot on time", current_slot(cfg, t("2026-10-23 10:05"))[0], "2026-10-23 10:00")
    check("slot 45 min edge", current_slot(cfg, t("2026-10-23 10:45"))[0], "2026-10-23 10:00")
    check("slot stale 46 min", current_slot(cfg, t("2026-10-23 10:46"))[0], None)
    check("after close", current_slot(cfg, t("2026-10-23 15:31"))[0], None)
    check("weekend", current_slot(cfg, t("2026-10-24 10:05"))[0], None)
    check("after ends", current_slot(cfg, t("2026-11-06 10:05"))[0], None)
    check("year guard", current_slot(cfg, t("2027-10-22 10:05"))[0], None)
    m = cfg["muse"]
    block = re.compile(m["block_re"])
    check("spam blocked", bool(block.search("베트맨 토토 앱 설정 변경 전 확인할 적용 시점")), True)
    ok_title = "메타 뮤즈 열풍에 D램 수요 늘어난다, 증권가 전망"
    check("filter keeps analyst/memory title", bool(re.search(m["muse_re"], ok_title) and re.search(m["meta_re"], ok_title, re.I)
                                                    and re.search(m["topic_re"], ok_title, re.I)), True)
    check("filter drops plain app news", bool(re.search(m["topic_re"], "메타 뮤즈 공유 링크 노출 논란", re.I)), False)
    check("Muse word boundary", bool(re.search(m["muse_re"], "Museum opening")), False)
    check("norm_title strips source", norm_title("메타 뮤즈 D램 - 한국경제") == norm_title("메타 뮤즈 D램"), True)
    print("SELF-TEST " + ("PASS" if not fails else f"FAIL ({fails})"))
    return 1 if fails else 0


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").strip().lower()
    if mode == "self-test":
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
    title, body, snap, new = build(cfg, state, now, slot, mode)
    if dry:
        print("DRY RUN (would send)", title, body, "dry-run; no ntfy, state untouched", sep="\n")
        return 0
    publish(resolve_topic("port", cfg.get("ntfy_topic")), title, body, 3, ["chart_with_upwards_trend"],
            cfg.get("ntfy_server"), config_name="mktpulse")
    stamp = now.strftime("%Y-%m-%d")
    seen = state.get("seen") or {}
    for i in new:  # mark only after a real send
        seen[i["key"]] = stamp
        if i.get("guid"):
            seen[i["guid"]] = stamp
    state["seen"] = seen
    state.setdefault("slots_sent", {})[slot or now.strftime("%Y-%m-%d %H:%M")] = stamp
    state["last_flow"] = {**(state.get("last_flow") or {}), **snap}
    save_state(state, now, int(cfg["muse"]["keep_days"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
