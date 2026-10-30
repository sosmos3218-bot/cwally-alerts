#!/usr/bin/env python3
"""[IREN] IREN news + schedule watch (watchlist, user does not hold IREN).

Cadence (approved in chat 2026-10-30, owner note pt22u): weekly news digest instead of near-daily p2 sends.
  - Every weekday 09:00 KST morning run: fetch Yahoo RSS headlines, keep the keyword hits (configs/iren.json
    keywords) that were not seen before (dedupe by URL in seen_ids) and store them in state pending_news. Silent.
  - Monday 09:00 KST run (or, if that run was missed, the next run whose last digest is 7+ days old):
    ONE p2 digest "[IREN] 주간 뉴스 요약 (MM/DD~MM/DD)", newest first, max 10 items + "외 N건", each with its link.
    Zero items -> nothing sent (the digest clock still advances). Items older than 7 days at digest time are dropped.
  - Immediate (p3): a schedule window from configs/iren.json events that newly starts (h24 Horizon, midterm,
    btc mining exit, grace, sweetwater). It carries no news (news only goes into the digest).
  - SEC filings are not read here ([IREN-PX] whitelist); earnings are [IREN-EARN].
Modes: argv[1] event | weekly (weekly = digest due even if not Monday), argv[2] auto | dry (no ntfy, no state) | selftest.
Not investment advice.
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
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parents[1]
CFG_PATH = ROOT / "configs" / "iren.json"
STATE_PATH = ROOT / "state" / "iren.json"
UA = "cwally-alerts/iren (alerts@local)"
TAG_KO = {
    "REVIEW": "$30 아래. 재검토",
    "JPM": "JPM 목표 $65 도달",
    "CONSENSUS": "콘센서스 $80 도달",
    "WATCH": "관찰 구간",
}
DO_NOW = {
    "REVIEW": "보유 없음, 관망 유지, 추가매수 보류. Horizon 2~4 인수와 채굴 종료 일정을 먼저 확인하세요.",
    "JPM": "JPM 목표가 근처입니다. 계약 인수 공시가 없으면 추격 매수 보류.",
    "CONSENSUS": "컨센서스 목표가 근처입니다. 추격 매수 보류, 근거 재확인.",
    "WATCH": "새 뉴스와 일정만 보고, 오늘 새로 사지 마세요.",
}
TITLE_BY_TAG = {
    "REVIEW": "$30 하회 재검토",
    "JPM": "$65 근처 추격 금지",
    "CONSENSUS": "$80 근처 추격 금지",
    "WATCH": "관찰 유지",
}


def yahoo_price(ticker: str):
    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        + urllib.parse.quote(ticker)
        + "?interval=1d&range=5d"
    )
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read().decode())
        meta = payload["chart"]["result"][0].get("meta", {})
        price = meta.get("regularMarketPrice") or meta.get("previousClose")
        return float(price) if price is not None else None
    except Exception as exc:
        print(f"price fetch failed: {exc}", file=sys.stderr)
        return None


def fetch_sec(cik: str):
    url = (
        "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK="
        + cik
        + "&type=&dateb=&owner=include&count=15&output=atom"
    )
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/atom+xml"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            xml = resp.read().decode("utf-8", errors="ignore")
    except Exception as exc:
        print(f"sec fetch failed: {exc}", file=sys.stderr)
        return []
    items = []
    for block in re.findall(r"<entry>(.*?)</entry>", xml, re.S | re.I):
        tm = re.search(r"<title[^>]*>(.*?)</title>", block, re.S | re.I)
        title = unescape(re.sub(r"<[^>]+>", " ", tm.group(1) if tm else "")).strip()
        href = ""
        hm = re.search(r"<link[^>]*href=\"([^\"]+)\"", block, re.I)
        if hm:
            href = unescape(hm.group(1))
        if title:
            items.append({"id": href or title, "title": re.sub(r"\s+", " ", title), "url": href, "src": "sec"})
    return items[:15]


def fetch_yahoo_news(ticker: str):
    url = "https://feeds.finance.yahoo.com/rss/2.0/headline?s=" + urllib.parse.quote(ticker) + "&region=US&lang=en-US"
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            xml = resp.read().decode("utf-8", errors="ignore")
    except Exception as exc:
        print(f"yahoo news failed: {exc}", file=sys.stderr)
        return []
    items = []
    for block in re.findall(r"<item>(.*?)</item>", xml, re.S | re.I):
        tm = re.search(r"<title[^>]*>(.*?)</title>", block, re.S | re.I)
        lm = re.search(r"<link[^>]*>(.*?)</link>", block, re.S | re.I)
        title = unescape(re.sub(r"<!\[CDATA\[|\]\]>", "", tm.group(1) if tm else "")).strip()
        href = unescape(re.sub(r"<[^>]+>", "", lm.group(1) if lm else "")).strip()
        if title:
            items.append({"id": href or title, "title": re.sub(r"\s+", " ", title), "url": href, "src": "yahoo"})
    return items[:15]


def hot_hit(title: str, keywords):
    t = title.lower()
    return any(k.lower() in t for k in keywords)


def active_events(cfg, today):
    return [ev for ev in cfg.get("events", []) if ev["start"] <= today <= ev["end"]]


def upcoming_events(cfg, today, days=21):
    lim = (datetime.strptime(today, "%Y-%m-%d") + timedelta(days=days)).strftime("%Y-%m-%d")
    return [ev for ev in cfg.get("events", []) if today < ev["start"] <= lim]



DIGEST_CAP = 10
DIGEST_MAX_AGE_DAYS = 7
BANNED = ("\u00b7", "\u2014")
BUY_WORDS = ("분할매수", "매수 검토", "매수 기회", "매수 구간", "저가 매수", "사세요", "진입 검토", "담을", "나눠서")
SIX = ["이게 뭐 알림", "지금 결과/상태", "지금 할 일", "나중에", "익절/줄일 때", "하지 말 것"]


def price_tag(cfg, price):
    lv = cfg["levels"]
    if price is not None and price <= lv["review"]:
        return "REVIEW"
    if price is not None and price >= lv["consensus"]:
        return "CONSENSUS"
    if price is not None and price >= lv["jpm"]:
        return "JPM"
    return "WATCH"


def parse_stamp(s):
    if not s:
        return None
    try:
        return datetime.strptime(s[:16], "%Y-%m-%d %H:%M").replace(tzinfo=KST)
    except ValueError:
        return None


def digest_due(st, now, force=False):
    """Monday run, or catch-up when the last digest (sent or empty) is 7+ days old. Missing clock -> not due (seed)."""
    last = parse_stamp(st.get("last_digest_at"))
    if last is None:
        return False
    if last.date() == now.date():
        return False
    return force or now.weekday() == 0 or (now - last) >= timedelta(days=7)


def collect(st, news, keywords, now):
    """Add new keyword-hit headlines to pending_news, mark every fetched item seen. Returns number added."""
    seen = list(st.get("seen_ids") or [])
    seen_set = set(seen)
    pending = list(st.get("pending_news") or [])
    added = 0
    for it in news:
        if it["id"] in seen_set:
            continue
        seen.append(it["id"])
        seen_set.add(it["id"])
        if hot_hit(it["title"], keywords):
            pending.append({"title": it["title"], "url": it.get("url", ""), "seen_at": now.strftime("%Y-%m-%d %H:%M KST")})
            added += 1
    st["seen_ids"] = seen[-200:]
    st["pending_news"] = pending
    return added


def status_line(cfg, price, tag):
    px = f"${price:,.2f}" if price is not None else "시세 없음"
    return f"{px} | {TAG_KO.get(tag, tag)}. 계약 ARR $40억 / 가동 $10억 / 갭 $30억."


def digest_message(cfg, items, since, now, price, tag, soon):
    items = sorted(items, key=lambda x: x["seen_at"], reverse=True)
    shown, more = items[:DIGEST_CAP], len(items) - DIGEST_CAP
    news = " ".join(f"{i}) {it['title']} ({it['seen_at'][5:10].replace('-', '/')}) {it['url']}" for i, it in enumerate(shown, 1))
    if more > 0:
        news += f" 외 {more}건."
    span = f"{since:%m/%d}~{now:%m/%d}"
    upcoming = (" 21일 안 일정: " + ", ".join(f"{e['label']} {e['start']}" for e in soon) + ".") if soon else ""
    lines = [
        f"{now:%Y-%m-%d %H:%M} KST | IREN | {cfg['display']} | 보유 없음, 관심 종목",
        "이게 뭐 알림: IREN 키워드 뉴스(Horizon, Microsoft, NVIDIA, ARR, ERCOT, 채굴 종료 등)를 매일 모아 월요일 09:00 에 1번 보내는 주간 요약입니다. SEC 공시는 [IREN-PX], 실적은 [IREN-EARN] 이 따로 보냅니다.",
        f"지금 결과/상태: {status_line(cfg, price, tag)} 지난 요약 뒤 새 뉴스 {len(items)}건(최신순). {news}",
        "지금 할 일: 관망 유지. " + DO_NOW.get(tag, "") + " 헤드라인보다 회사 공시와 보도자료로 사실 확인.",
        "나중에: MS Horizon 2~4 인도(올 4분기 목표, 유예는 27년 2분기 초), NVDA $34억은 27년 램프." + upcoming,
        "익절/줄일 때: 보유 없음이라 해당 없음. 경고 신호는 $30 하회, Horizon 미인수, ERCOT/스위트워터 지연, 채굴 종료 후 현금흐름 공백.",
        "하지 말 것: 뉴스 헤드라인만 보고 추격 매수, 신용/미수, 이 알림만 보고 주문. 투자 권유 아님.",
    ]
    return f"[IREN] 주간 뉴스 요약 ({span})", "\n".join(lines), 2


def event_message(cfg, new_live, now, price, tag):
    ev = " ".join(f"{e['label']} {e['start']}~{e['end']}: {e['check']}" for e in new_live)
    lines = [
        f"{now:%Y-%m-%d %H:%M} KST | IREN | {cfg['display']} | 보유 없음, 관심 종목",
        "이게 뭐 알림: IREN 핵심 일정 구간이 새로 시작될 때 바로 1번 오는 알림입니다. 뉴스는 월요일 주간 요약으로 모아 보냅니다.",
        f"지금 결과/상태: {status_line(cfg, price, tag)} 새로 시작한 일정: {ev}",
        "지금 할 일: 관망 유지. " + DO_NOW.get(tag, "") + " 이 기간에는 위 확인 항목이 회사 공시로 나오는지 봅니다.",
        "나중에: 관련 SEC 공시는 [IREN-PX], 실적은 [IREN-EARN] 으로 옵니다.",
        "익절/줄일 때: 보유 없음이라 해당 없음.",
        "하지 말 것: 일정 시작만 보고 미리 주문, 신용/미수. 투자 권유 아님.",
    ]
    names = ", ".join(e["label"] for e in new_live)
    return f"[IREN] 일정 시작: {names} | 관망, 확인 항목 점검", "\n".join(lines), 3


def run(cfg, st, now, mode, dry, price, news, send):
    """Pure-ish core: mutates st, calls send(title, body, prio, tags). Returns list of sent titles."""
    today = now.strftime("%Y-%m-%d")
    sent = []
    tag = price_tag(cfg, price)
    added = collect(st, news, cfg["keywords"], now)
    print(f"news: {len(news)} fetched, {added} new keyword hits queued, pending {len(st['pending_news'])}")
    live = active_events(cfg, today)
    new_live = [e for e in live if e["id"] not in (st.get("last_event_ids") or [])]
    if new_live:
        t, b, p = event_message(cfg, new_live, now, price, tag)
        send(t, b, p, ["calendar", "zap"])
        sent.append(t)
    st["last_event_ids"] = [e["id"] for e in live]
    if st.get("last_digest_at") is None:
        st["last_digest_at"] = now.strftime("%Y-%m-%d %H:%M KST")
        st["pending_news"] = []
        print("digest clock seeded; current headlines marked seen, nothing queued")
    elif digest_due(st, now, force=(mode == "weekly")):
        since = parse_stamp(st["last_digest_at"])
        cutoff = now - timedelta(days=DIGEST_MAX_AGE_DAYS)
        items = [it for it in st["pending_news"] if (parse_stamp(it["seen_at"]) or now) >= cutoff]
        if items:
            t, b, p = digest_message(cfg, items, since, now, price, tag, upcoming_events(cfg, today))
            send(t, b, p, ["newspaper"])
            sent.append(t)
        else:
            print("digest due but no new items; silent")
        st["pending_news"] = []
        st["last_digest_at"] = now.strftime("%Y-%m-%d %H:%M KST")
        st["last_digest_items"] = len(items)
    else:
        print(f"digest not due (last {st.get('last_digest_at')}); silent")
    st.update({"updated_at": now.strftime("%Y-%m-%d %H:%M KST"), "price": price, "tag": tag})
    return sent


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "event").lower()
    if mode == "selftest":
        return selftest()
    dry = (sys.argv[2] if len(sys.argv) > 2 else "auto").lower() == "dry"
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    print(f"{now.isoformat(timespec='seconds')} iren mode={mode}" + (" DRY RUN (no ntfy, no state write)" if dry else ""))
    st = {}
    if STATE_PATH.exists():
        try:
            st = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            st = {}

    def send(title, body, prio, tags):
        if dry:
            print(f"DRY RUN would send (p{prio}): {title}\n{body}")
        else:
            publish(resolve_topic("port"), title, body, prio, tags=tags, config_name="iren")

    run(cfg, st, now, mode, dry, yahoo_price(cfg["ticker"]), fetch_yahoo_news(cfg["ticker"]), send)
    if dry:
        print("dry: state not saved")
    else:
        STATE_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


def selftest() -> int:
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    cfg = dict(cfg, events=[{"id": "ev1", "label": "테스트 일정", "start": "2026-11-11", "end": "2026-11-20", "check": "확인 항목."}])
    fails = 0

    def check(label, got, want):
        nonlocal fails
        ok = got == want
        fails += not ok
        print(("ok  " if ok else "FAIL"), label, "" if ok else f"got {got!r} want {want!r}")

    def at(s):
        return datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=KST)

    def N(i, hot=True):
        return {"id": f"u{i}", "title": (f"IREN Horizon update {i}" if hot else f"Market wrap {i}"), "url": f"https://x/{i}", "src": "yahoo"}

    log = []

    def send(t, b, p, tags):
        log.append((t, b, p))

    st = {"seen_ids": ["u0"], "last_event_ids": []}
    # seed run (Fri 10/30): clock set, existing headlines marked seen, no send
    run(cfg, st, at("2026-10-30 09:00"), "event", False, 26.25, [N(0), N(1), N(2, False)], send)
    check("seed: silent", log, [])
    check("seed: nothing queued", st["pending_news"], [])
    check("seed: headlines seen", {"u1", "u2"} <= set(st["seen_ids"]), True)
    # daily runs silent, items queued, non-keyword ignored, dedupe
    run(cfg, st, at("2026-10-31 09:00"), "event", False, 26.0, [N(1), N(3), N(4, False)], send)
    run(cfg, st, at("2026-11-01 09:00"), "event", False, 26.0, [N(3), N(5)], send)
    check("daily runs silent", log, [])
    check("queued u3,u5 only", [x["url"] for x in st["pending_news"]], ["https://x/3", "https://x/5"])
    # Monday: one digest
    run(cfg, st, at("2026-11-02 09:00"), "weekly", False, 25.0, [N(5), N(6)], send)
    check("Monday sends one", len(log), 1)
    t, b, p = log[0]
    check("digest title", t, "[IREN] 주간 뉴스 요약 (10/30~11/02)")
    check("digest p2", p, 2)
    check("newest first", b.index("update 6") < b.index("update 3"), True)
    check("links present", all(f"https://x/{i}" in b for i in (3, 5, 6)), True)
    check("queue cleared", st["pending_news"], [])
    check("six parts", [x.split(":")[0] for x in b.split("\n")[1:7]], SIX)
    check("no dot/dash", any(c in t + b for c in BANNED), False)
    check("no buy wording", [w for w in BUY_WORDS if w in t + b], [])
    check("watchlist wording", "보유 없음" in b and "관망" in b, True)
    # same Monday rerun: no second digest
    run(cfg, st, at("2026-11-02 09:30"), "weekly", False, 25.0, [N(7)], send)
    check("same-day rerun silent", len(log), 1)
    # missed Monday 11/09: Tue 11/10 catch-up (7+ days)
    for d, n in (("2026-11-03", 8), ("2026-11-04", 9), ("2026-11-05", 10), ("2026-11-06", 11)):
        run(cfg, st, at(f"{d} 09:00"), "event", False, 25.0, [N(n)], send)
    check("weekdays silent", len(log), 1)
    run(cfg, st, at("2026-11-10 09:00"), "event", False, 25.0, [N(12)], send)
    check("catch-up after missed Monday", (len(log), log[-1][0]), (2, "[IREN] 주간 뉴스 요약 (11/02~11/10)"))
    # 11/11 schedule window opens: immediate p3, no news in it
    run(cfg, st, at("2026-11-11 09:00"), "event", False, 25.0, [N(13)], send)
    check("schedule start immediate p3", (len(log), log[-1][2], log[-1][0].startswith("[IREN] 일정 시작: 테스트 일정")), (3, 3, True))
    check("event msg has no headlines", "update 13" in log[-1][1], False)
    check("event msg six parts / clean", ([x.split(":")[0] for x in log[-1][1].split("\n")[1:7]], any(c in log[-1][1] for c in BANNED),
                                          [w for w in BUY_WORDS if w in log[-1][1]]), (SIX, False, []))
    run(cfg, st, at("2026-11-12 09:00"), "event", False, 25.0, [], send)
    check("schedule once", len(log), 3)
    # Monday 11/16: digest has u13 only (u12 went in catch-up)
    run(cfg, st, at("2026-11-16 09:00"), "weekly", False, 25.0, [], send)
    check("next Monday digest", (len(log), "update 13" in log[-1][1], "update 12" in log[-1][1]), (4, True, False))
    # empty week: Monday silent, clock advances, Tuesday not treated as missed
    run(cfg, st, at("2026-11-23 09:00"), "weekly", False, 25.0, [N(13)], send)
    check("empty week silent", len(log), 4)
    check("clock advanced on empty week", st["last_digest_at"], "2026-11-23 09:00 KST")
    run(cfg, st, at("2026-11-24 09:00"), "event", False, 25.0, [N(14)], send)
    check("Tuesday after empty Monday silent", len(log), 4)
    # cap 10 + 외 N건, stale (>7d) dropped
    st["pending_news"] = [{"title": f"IREN item {i}", "url": f"https://y/{i}", "seen_at": f"2026-11-{24 + i % 5:02d} 09:00 KST"} for i in range(13)]
    st["pending_news"].append({"title": "IREN old", "url": "https://y/old", "seen_at": "2026-11-20 09:00 KST"})
    run(cfg, st, at("2026-11-30 09:00"), "weekly", False, 25.0, [], send)
    t, b, p = log[-1]
    check("cap 10 + 외 3건", (b.count("https://y/"), "외 3건" in b), (10, True))
    check("stale >7d dropped", "IREN old" in b, False)
    # tag wording for every price tag stays clean
    for px in (25.0, 50.0, 70.0, 85.0):
        tg = price_tag(cfg, px)
        t, b, p = digest_message(cfg, [{"title": "IREN x", "url": "u", "seen_at": "2026-11-02 09:00 KST"}], at("2026-10-30 09:00"), at("2026-11-02 09:00"), px, tg, [])
        check(f"tag {tg} clean", (any(c in t + b for c in BANNED), [w for w in BUY_WORDS if w in t + b]), (False, []))
    print("selftest", "PASS" if not fails else f"FAIL {fails}")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
