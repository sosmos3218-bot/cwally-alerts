#!/usr/bin/env python3
"""[SHINWON] 신원(009270) 190주 감시. 평일 09:00 morning, 15:20 afternoon 슬롯 (새 cron 없음).
가격 구간: Yahoo 현재가로 898원 이하(STOP898), 1,200~1,230원(SELL1), 1,270원 이상(SELL2), 11/13 시간손절(TIME).
1,000원 손절(STOP1000)은 2026-11-02 승인(t34u)부터 KRX 정규장 확정 종가 < 1,000원 (Daum KRX 일별, 실패 시 네이버 fchart).
  오늘 행은 15:40 KST 뒤에만 확정 종가로 씀(09:00 은 전날 종가, 15:20 은 장중이라 제외). 종가 >= 1,000원이면 해제(여유폭 없음).
동전주 카운트: 2026-07-01 이후 Daum KRX 종가 < 1,000원 연속 거래일 수. 10, 20일 p3, 25, 30일 p4 1번씩(연속 구간별),
  종가 >= 1,000원이면 0으로 리셋. 카운트는 Daum 이력으로만 계산(네이버는 NXT 포함 통합시세라 카운트에 안 씀).
알림 끝: 2026-11-20 23:59 KST (3Q 분기보고서 공시 감지 포함). 그 뒤 조용히 종료, state 안 씀.
modes: event | force | weekly | dry (발송 없음, state 안 씀) | selftest (가짜 값, 네트워크 없음)
"""
from __future__ import annotations

import json
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
import ntfy_util  # noqa: E402

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parents[1]
CFG_PATH = ROOT / "configs" / "shinwon.json"
STATE_PATH = ROOT / "state" / "shinwon.json"
UA = {
    "User-Agent": "Mozilla/5.0 cwally-alerts",
    "Referer": "https://stock.naver.com/domestic/stock/009270/notice",
}

EVENT_ZONES = {"STOP898", "STOP1000", "SELL1", "SELL2", "TIME"}
ZONE_KO = {
    "STOP898": "52주 저점 898원 탈출. 전량",
    "STOP1000": "KRX 종가 1,000원 미만. 전량",
    "SELL1": "1,200~1,230원. 95주 1차 매도",
    "SELL2": "1,270원 이상. 잔량 매도",
    "TIME": "11/13 시간손절. 1,200 미도달 전량",
    "WATCH": "규칙 구간 밖. 물타기 금지",
    "ENDED": "알림 창 종료",
    "DATA_FAIL": "시세 조회 실패",
}
DO_NOW = {
    "STOP898": "190주 전량 매도. 재진입 금지.",
    "STOP1000": "KRX 종가 1,000원 미만. 190주 전량. 11/13을 기다리지 마세요.",
    "SELL1": "95주 지정가 매도. 나머지는 1,270원대.",
    "SELL2": "남은 95주 전량 매도.",
    "TIME": "11/13까지 1,200원을 못 갔습니다. 190주 전량.",
    "WATCH": "조건 주문 없이 관찰. 물타기 하지 말 것.",
    "ENDED": "시간창 끝. 새 공시만 참고.",
    "DATA_FAIL": "네이버나 야후에서 현재가를 다시 확인하세요.",
}
TITLE_BY_ZONE = {
    "STOP898": "898 탈출 전량",
    "STOP1000": "1,000 종가 전량",
    "SELL1": "1,200대 95주 매도",
    "SELL2": "1,270이상 잔량 매도",
    "TIME": "11/13 전량",
    "WATCH": "관찰 유지",
    "ENDED": "창 종료",
    "DATA_FAIL": "시세 확인",
}
DISC_RULES = [
    ("EARN", ("분기보고서", "반기보고서", "사업보고서", "잠정", "손익구조", "매출액또는손익"), "3Q 영업이익률 2% 미만이면 전량. 패션 적자도 같이 보세요."),
    ("CB", ("전환사채", "전환청구", "전환권", "사채상환", "전환가액"), "CB 잔여 전환이나 상환이면 오버행. 1,200 추격 금지."),
    ("TS", ("자기주식", "주식소각", "자사주"), "자사주 소각이면 1차 95주만 매도. 2차는 소각 다음날까지 보류."),
]


def load_json(path: Path, fallback: dict) -> dict:
    if not path.exists():
        return dict(fallback)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        fallback.update(data)
        return fallback
    except json.JSONDecodeError:
        return fallback


def http_get(url: str, timeout: int = 20) -> str:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def yahoo_price(ticker: str):
    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        + urllib.parse.quote(ticker)
        + "?interval=1d&range=5d"
    )
    payload = json.loads(http_get(url))
    meta = payload["chart"]["result"][0].get("meta", {})
    price = meta.get("regularMarketPrice") or meta.get("previousClose")
    return float(price) if price is not None else None


def zone(price, cfg, today: str, close_below: bool = False) -> str:
    """price = Yahoo 현재가(898, 매도 구간용). close_below = 최신 확정 KRX 종가 < 1,000."""
    end = cfg["window_end"]
    bands = cfg["bands"]
    if today > end:
        return "ENDED"
    if price is None and not close_below:
        return "DATA_FAIL"
    if price is not None and price <= bands["stop_hard"]:
        return "STOP898"
    if close_below:
        return "STOP1000"
    if price is None:
        return "DATA_FAIL"
    if today >= cfg.get("time_stop_date", end) and price < bands["sell1_lo"]:
        return "TIME"
    if price >= bands["sell2_lo"]:
        return "SELL2"
    if bands["sell1_lo"] <= price <= bands["sell1_hi"]:
        return "SELL1"
    return "WATCH"


# ---------------- KRX 확정 종가, 동전주 카운트 (2026-11-02 승인 t34u) ----------------
CLOSE_HM = (15, 40)
COIN_FROM = "2026-07-01"
COIN_LEVEL = 1000.0
MILESTONES = {10: 3, 20: 3, 25: 4, 30: 4}
END_LINE = "꺼지는 때: 2026-11-20 23:59 KST 이후 자동 발송 없음"
DAUM = "https://finance.daum.net/api/quote/A{code}/days?symbolCode=A{code}&page={page}&perPage=100&pagination=true"


def daum_closes(code: str) -> dict:
    out = {}
    for page in (1, 2):
        req = urllib.request.Request(DAUM.format(code=code, page=page),
                                     headers={"User-Agent": "Mozilla/5.0", "Referer": f"https://finance.daum.net/quotes/A{code}"})
        with urllib.request.urlopen(req, timeout=25) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
        rows = raw.get("data") or []
        for r in rows:
            if str(r.get("symbolCode")) != "A" + code:
                raise ValueError("종목 코드 불일치")
            day, px = str(r.get("date") or "")[:10], r.get("tradePrice")
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", day) and px:
                out[day] = float(px)
        if not rows or min(out) < COIN_FROM:
            break
    if not out:
        raise ValueError("Daum KRX 일별 시세 없음")
    return dict(sorted(out.items()))


def naver_closes(code: str) -> dict:
    txt = http_get(f"https://fchart.stock.naver.com/sise.nhn?symbol={code}&timeframe=day&count=10&requestType=0")
    out = {}
    for d, c in re.findall(r'data="(\d{8})\|[^|]*\|[^|]*\|[^|]*\|([\d.]+)\|', txt):
        out[f"{d[:4]}-{d[4:6]}-{d[6:]}"] = float(c)
    if not out:
        raise ValueError("네이버 일별 시세 없음")
    return dict(sorted(out.items()))


def confirmed(closes: dict, now: datetime) -> dict:
    today = now.strftime("%Y-%m-%d")
    late = (now.hour, now.minute) >= CLOSE_HM
    return {d: c for d, c in closes.items() if d < today or (d == today and late)}


def coin_stats(closes: dict) -> dict:
    """closes: 확정 KRX 종가 {date: close}. 7/1 이후 연속 미만 일수와 기록."""
    ds = [d for d in closes if d >= COIN_FROM]
    streak, start, mx, mx_span, total, cur_start = 0, None, 0, None, 0, None
    for d in ds:
        if closes[d] < COIN_LEVEL:
            total += 1
            if streak == 0:
                cur_start = d
            streak += 1
            if streak > mx:
                mx, mx_span = streak, (cur_start, d)
        else:
            streak, cur_start = 0, None
    return {"streak": streak, "streak_start": cur_start, "max": mx, "max_span": mx_span, "total": total,
            "last_date": ds[-1] if ds else None, "last_close": closes[ds[-1]] if ds else None}


def md(d: str | None) -> str:
    return f"{int(d[5:7])}/{int(d[8:10])}" if d else "-"


def coin_line(cs: dict | None, src: str) -> str:
    if not cs or not cs.get("last_date"):
        return "동전주 카운트: KRX 확정 종가 조회 실패, 카운트 보류(다음 실행에서 이력으로 따라잡음)"
    span = f", 최장 {cs['max']}일 {md(cs['max_span'][0])}~{md(cs['max_span'][1])}" if cs.get("max_span") else ""
    return (f"동전주 카운트: {md(cs['last_date'])} {src} 종가 {cs['last_close']:,.0f}원, 1,000원 미만 연속 {cs['streak']}거래일 "
            f"(30일이면 관리종목 요건. 7/1 이후 미만 {cs['total']}일{span})")


def coin_milestone(cs: dict, coin_prev: dict) -> tuple[int | None, dict]:
    """(보낼 이정표 또는 None, 새 coin state). 연속 구간(streak_start)별로 이정표 1번씩."""
    sent = list(coin_prev.get("sent") or []) if coin_prev.get("streak_start") == cs["streak_start"] else []
    hit = [m for m in sorted(MILESTONES) if cs["streak"] >= m and m not in sent]
    new = {"streak": cs["streak"], "streak_start": cs["streak_start"], "last_date": cs["last_date"],
           "last_close": cs["last_close"], "max": cs["max"], "max_span": cs["max_span"], "total": cs["total"],
           "sent": sorted(set(sent) | set(hit))}
    return (max(hit) if hit else None), new


def milestone_msg(m: int, cs: dict, stamp: str) -> tuple[str, str, int]:
    title = f"[SHINWON] 009270 동전주 종가 1,000원 미만 {cs['streak']}거래일 연속 | 관리종목 요건 30일"
    if m >= 30:
        now_line = "30거래일 연속 충족: 관리종목 지정 요건에 걸릴 수 있음. KRX 공시와 거래소 안내 원문 확인 권장."
        todo = "지금 할 일: 190주 처리 계획 확인. 관리종목 지정 공시가 나오는지 KRX KIND 원문 확인."
    else:
        now_line = f"30거래일 연속이면 관리종목 지정 요건. 남은 거래일 {30 - cs['streak']}일."
        todo = "지금 할 일: 190주 손절 규칙(KRX 종가 1,000원 미만 전량) 이미 걸린 상태인지 확인."
    lines = [f"{stamp} | SHINWON | 신원(009270) 동전주 카운트",
             "이게 뭐 알림: 2026-07-01 시행 동전주 규정. 종가 1,000원 미만 30거래일 연속이면 관리종목, 그 뒤 90거래일 안에 1,000원 이상 45거래일 연속 못 지키면 상장폐지.",
             f"지금 상태: {coin_line(cs, 'KRX')}. {now_line}",
             todo,
             "나중에: KRX 종가가 1,000원 이상이면 카운트 0으로 리셋, 이정표 10, 20, 25, 30일에 1번씩.",
             END_LINE]
    return title, "\n".join(lines), MILESTONES[m]


def classify_title(title: str) -> str | None:
    t = re.sub(r"\s+", "", title)
    for kind, keys, _hint in DISC_RULES:
        if any(k.replace(" ", "") in t for k in keys):
            return kind
    return None


def parse_naver_notice(code: str) -> list[dict]:
    url = f"https://stock.naver.com/api/domestic/detail/notice?itemCode={code}&startIdx=0&pageSize=20"
    raw = json.loads(http_get(url))
    rows = raw if isinstance(raw, list) else raw.get("result") or raw.get("list") or []
    items = []
    for row in rows:
        title = (row.get("title") or "").strip()
        date = (row.get("datetime") or "")[:10]
        nid = str(row.get("no") or f"{date}:{title}")
        if title:
            items.append({"id": nid, "date": date, "title": title, "src": "naver"})
    return items[:20]


def collect_disclosures(code: str) -> list[dict]:
    found: dict[str, dict] = {}
    try:
        for it in parse_naver_notice(code):
            it["kind"] = classify_title(it["title"])
            if it["kind"]:
                found[it["id"]] = it
    except Exception as exc:
        print(f"naver notice failed: {exc}", file=sys.stderr)
    return list(found.values())


def hint_for(kind: str) -> str:
    for k, _keys, hint in DISC_RULES:
        if k == kind:
            return hint
    return "공시 원문을 열어 확인하세요."


def publish(title: str, message: str, priority: int, tags: str) -> bool:
    # ntfy_util.publish: same topic routing, tags, server and end line; skips after ends_kst + grace.
    ntfy_util.publish("cwally-port", title, message, priority, tags.split(","), "https://ntfy.sh", config_name="shinwon")
    return bool(ntfy_util.LAST_PUBLISH.get("sent"))


def ended(now: datetime, cfg: dict) -> bool:
    return now.strftime("%Y-%m-%d %H:%M") > cfg["ends_kst"]


def run(mode: str, now: datetime, cfg: dict, prev: dict, price_fn, daum_fn, naver_fn, discs_fn, pub=None, write=None) -> dict:
    today = now.strftime("%Y-%m-%d")
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    dry = mode in {"dry", "selftest"}
    if ended(now, cfg):
        print(f"shinwon ended ({cfg['ends_kst']} KST): silent, no fetch, no state")
        return {"sent": [], "state": prev, "ended": True}

    price = None
    try:
        price = price_fn(cfg["ticker"])
    except Exception as exc:
        print(f"price fetch failed: {exc}", file=sys.stderr)

    cs, close_src, close_last = None, None, None
    try:
        conf = confirmed(daum_fn(cfg["code"]), now)
        cs = coin_stats(conf)
        close_src = "Daum KRX"
        close_last = (cs["last_date"], cs["last_close"])
    except Exception as exc:
        print(f"daum close failed: {exc}", file=sys.stderr)
        try:
            conf = confirmed(naver_fn(cfg["code"]), now)
            d = max(conf)
            close_src, close_last = "네이버 통합시세(NXT 포함 가능)", (d, conf[d])
        except Exception as exc2:
            print(f"naver close failed: {exc2}", file=sys.stderr)
    prev_close_below = prev.get("zone") == "STOP1000"
    close_below = (close_last[1] < COIN_LEVEL) if close_last else prev_close_below

    z = zone(price, cfg, today, close_below)
    if z == "DATA_FAIL" and prev.get("zone") not in (None, "DATA_FAIL"):
        z = prev["zone"]
        price = prev.get("price")

    discs = discs_fn(cfg["code"])
    print("tracked discs: " + (", ".join(f"{d.get('kind')}:{d.get('date')}:{d.get('title')}" for d in discs[:8]) or "none"))
    seen = list(dict.fromkeys(prev.get("seen_ids") or []))
    cutoff = cfg.get("as_of") or today
    new_discs = [d for d in discs if d["id"] not in seen and d.get("date", "") >= cutoff]
    seen_ids = list(dict.fromkeys(seen + [d["id"] for d in discs]))[-80:]

    zone_changed = bool(prev.get("zone")) and prev.get("zone") != z and (
        z in EVENT_ZONES or prev.get("zone") in EVENT_ZONES
    )
    time_fire = z == "TIME" and not prev.get("time_stop_sent")
    force = mode in {"force", "weekly"}

    coin_prev = prev.get("coin") or {}
    milestone, coin_new = (None, coin_prev)
    if cs:
        if not coin_prev:  # 첫 실행: 이미 지난 이정표는 보낸 것으로 기록만
            coin_new = coin_milestone(cs, {})[1]
            print(f"coin seed: streak {cs['streak']}, sent {coin_new['sent']} (silent)")
        else:
            milestone, coin_new = coin_milestone(cs, coin_prev)

    send = bool(force or zone_changed or new_discs or time_fire or mode not in {"event", "dry", "selftest"})
    if not send:
        print("no shinwon zone/disclosure event")

    px = f"{price:,.0f}원" if price is not None else "시세 없음"
    cost = cfg["cost"]
    shares = cfg["shares"]
    pnl = f"{(price / cost - 1) * 100:+.1f}%" if price else "없음"
    value = f"{int(price * shares):,}원" if price else "없음"
    action = DO_NOW.get(z, "")
    if any(d.get("kind") == "EARN" for d in new_discs):
        action = "실적 공시 확인. 영업이익률 2% 미만이거나 패션 적자가 안 줄었으면 11/13 전에 전량. " + action
    if any(d.get("kind") == "CB" for d in new_discs):
        action = "CB 공시 확인. 오버행이면 1,200 추격 금지. " + action
    if any(d.get("kind") == "TS" for d in new_discs):
        action = "자사주 소각이면 95주만 팔고 잔량은 다음날. " + action
    close_txt = (f"확정 종가 {md(close_last[0])} {close_last[1]:,.0f}원({close_src})" if close_last else "확정 종가 조회 실패")

    lines = [
        f"{stamp} | SHINWON | {cfg['display']}({cfg['code']})",
        "이게 뭐 알림: 신원 190주 감시. 평단 1,451원. 시간손절 11/13, 알림은 11/20까지(3Q 분기보고서 포함).",
        f"지금 상태: {px} | 평단 대비 {pnl} | 평가 {value} | {ZONE_KO.get(z, z)}",
        f"- {close_txt}. 1,000원 손절은 확정 종가 1,000원 미만일 때만.",
        f"- {coin_line(cs, 'KRX')}",
        f"지금 할 일: {action}",
        "나중에: 3Q 영업이익률, 패션 적자, CB 잔여, 자사주 소각만 보세요.",
        "익절/줄일 때: 1,200~1,230원 95주, 1,270원 이상 잔량. 본전 1,451은 목표 아님.",
        "하지 말 것: 물타기, 1,000원 종가 무시, 대북 테마 추격.",
    ]
    if new_discs:
        lines.append("")
        lines.append("새 공시:")
        for d in new_discs[:6]:
            lines.append(f"- [{d.get('kind')}] {d['date']} {d['title']}")
            lines.append(f"  → {hint_for(d.get('kind') or '')}")
    lines.append(END_LINE)

    title_bit = TITLE_BY_ZONE.get(z, z)
    if new_discs:
        kinds = "/".join(sorted({d.get("kind") or "DISC" for d in new_discs}))
        title = f"[SHINWON] 009270 공시 {kinds} | {title_bit}"
    elif time_fire:
        title = "[SHINWON] 009270 11/13 전량"
    elif zone_changed:
        title = f"[SHINWON] 009270 구간 변화 | {title_bit}"
    else:
        title = f"[SHINWON] 009270 {title_bit}"
    msg = "\n".join(lines)
    print(msg)
    out = []
    if send:
        hot = z in {"STOP898", "STOP1000", "TIME"} or any(d.get("kind") in {"EARN", "CB", "TS"} for d in new_discs)
        pri = 5 if hot else (4 if z in {"SELL1", "SELL2"} or zone_changed else 3)
        out.append(("main", title, msg, pri))
    if milestone:
        mt, mm, mp = milestone_msg(milestone, cs, stamp)
        print(f"{mt}\n{mm}")
        out.append(("coin", mt, mm, mp))
    for _, t, m, _ in out:
        assert not re.search("[\u00b7\u318d\u2014]", t + m), t
    if dry:
        for _, t, _, p in out:
            print(f"WOULD SEND p{p}: {t}")
        print("DRY RUN: no publish, state not written")
        return {"sent": [], "would": out, "zone": z, "coin": coin_new, "state": prev}
    sent = {}
    for kind, t, m, p in out:
        sent[kind] = (pub or publish)(t, m, p, "warning,chart_with_downwards_trend")
    ok = sent.get("main", False) or not send
    coin_ok = sent.get("coin", False) or not milestone
    new_ids = {d["id"] for d in new_discs}
    st = {
        "updated_at": stamp,
        "zone": z if ok else prev.get("zone"),
        "price": price,
        "close": {"date": close_last[0], "close": close_last[1], "src": close_src} if close_last else prev.get("close"),
        "coin": coin_new if coin_ok else dict(coin_new, sent=[s for s in coin_new.get("sent", []) if s != milestone]),
        "seen_ids": seen_ids if ok else list(dict.fromkeys(seen + [d["id"] for d in discs if d["id"] not in new_ids]))[-80:],
        "time_stop_sent": bool(prev.get("time_stop_sent") or (z == "TIME" and ok)),
    }
    (write or save_state)(st)
    return {"sent": [k for k, v in sent.items() if v], "zone": z, "coin": coin_new, "state": st}


def save_state(st: dict) -> None:
    STATE_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def selftest() -> int:
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    n = 0

    def ck(c, m=""):
        nonlocal n
        assert c, m
        n += 1
    T = lambda d, h=9, mi=0: datetime.fromisoformat(d).replace(hour=h, minute=mi, tzinfo=KST)  # noqa: E731
    sent, saved = [], []
    pub = lambda t, m, p, tags: sent.append((t, m, p)) or True  # noqa: E731
    wr = lambda s: saved.append(s)  # noqa: E731
    nodisc = lambda code: []  # noqa: E731
    nonav = lambda code: (_ for _ in ()).throw(RuntimeError("x"))  # noqa: E731
    px = lambda v: (lambda t: v)  # noqa: E731

    def days(start: str, n_: int) -> list[str]:
        out, d = [], datetime.fromisoformat(start)
        while len(out) < n_:
            if d.weekday() < 5:
                out.append(d.strftime("%Y-%m-%d"))
            d += timedelta(days=1)
        return out
    base = {"zone": "WATCH", "price": 1050.0, "seen_ids": [], "time_stop_sent": False,
            "coin": {"streak": 0, "streak_start": None, "sent": []}}
    # 1) 종가 999 미만 -> STOP1000, 1000 정확히는 아님(미만만)
    ck(zone(1050.0, cfg, "2026-11-03", close_below=True) == "STOP1000")
    ck(zone(1050.0, cfg, "2026-11-03", close_below=False) == "WATCH")
    ck(zone(890.0, cfg, "2026-11-03", close_below=True) == "STOP898", "898 우선")
    ck(zone(990.0, cfg, "2026-11-03", close_below=False) == "WATCH", "장중 990 은 손절 아님(종가 기준)")
    ds = days("2026-10-26", 6)  # 10/26..11/2
    r = run("event", T("2026-11-03"), cfg, base, px(1010.0), lambda c: dict(zip(ds, [1005, 1001, 1002, 1003, 1004, 1000.0])), nonav, nodisc, pub, wr)
    ck(r["zone"] == "WATCH" and not sent, "종가 1,000 정확히는 손절 아님")
    r = run("event", T("2026-11-03"), cfg, base, px(1010.0), lambda c: dict(zip(ds, [1005, 1001, 1002, 1003, 1004, 999.0])), nonav, nodisc, pub, wr)
    ck(r["zone"] == "STOP1000" and len(sent) == 1 and "구간 변화 | 1,000 종가 전량" in sent[0][0] and sent[0][2] == 5, sent)
    ck("1,000원 미만 연속 1거래일" in sent[0][1] and sent[0][1].rstrip().endswith(END_LINE), sent[0][1])
    st = r["state"]
    sent.clear()
    r = run("event", T("2026-11-03", 15, 20), cfg, st, px(1010.0), lambda c: dict(zip(ds, [1005, 1001, 1002, 1003, 1004, 999.0])), nonav, nodisc, pub, wr)
    ck(r["zone"] == "STOP1000" and not sent, "같은 종가 재실행 무발송")
    # 2) 장중 값은 종가 아님: 15:20 의 오늘 행은 제외, 15:40 뒤에는 포함
    ds2 = days("2026-10-27", 6)  # ..11/3
    c2 = dict(zip(ds2, [1005, 1001, 1002, 1003, 1004, 950.0]))
    ck(confirmed(c2, T("2026-11-03", 15, 20)) == {d: c2[d] for d in ds2[:-1]})
    ck(confirmed(c2, T("2026-11-03", 15, 45)) == c2)
    r = run("dry", T("2026-11-03", 15, 20), cfg, base, px(950.0), lambda c: c2, nonav, nodisc, pub, wr)
    ck(r["zone"] == "WATCH" and r["coin"]["streak"] == 0, "15:20 장중 950 은 카운트 안 함")
    # 3) 해제: 종가 >= 1,000 이면 STOP1000 -> WATCH
    sent.clear()
    r = run("event", T("2026-11-04"), cfg, st, px(1010.0), lambda c: dict(zip(days("2026-10-27", 6), [1005, 1001, 1002, 1003, 999, 1000.0])), nonav, nodisc, pub, wr)
    ck(r["zone"] == "WATCH" and len(sent) == 1 and "관찰 유지" in sent[0][0], sent)
    # 4) 동전주 이정표: 9일 없음, 10일 p3, 같은 구간 재실행 없음, 20 p3, 25 p4, 30 p4, 한꺼번에 넘으면 큰 것 1번
    dd = days("2026-07-01", 40)

    def hist(k):  # 마지막 k 거래일 미만
        return {d: (990.0 if i >= len(dd[:-0 or None]) - k else 1100.0) for i, d in enumerate(dd)}
    now = T("2026-09-01")  # dd 마지막 날(8/25) 뒤
    stc = dict(base, zone="STOP1000")
    for k, want in ((9, None), (10, (10, 3))):
        sent.clear()
        r = run("event", now, cfg, stc, px(990.0), lambda c, k=k: hist(k), nonav, nodisc, pub, wr)
        got = [(int(re.search(r"(\d+)거래일 연속", t).group(1)), p) for t, _, p in sent if "동전주" in t]
        ck(got == ([] if want is None else [want]), (k, got))
        stc = r["state"]
    sent.clear()
    r = run("event", now, cfg, stc, px(990.0), lambda c: hist(10), nonav, nodisc, pub, wr)
    ck(not sent, "같은 10일 재실행 무발송")
    for k, p in ((20, 3), (25, 4), (30, 4)):
        sent.clear()
        r = run("event", now, cfg, r["state"], px(990.0), lambda c, k=k: hist(k), nonav, nodisc, pub, wr)
        ck(len(sent) == 1 and f"{k}거래일 연속" in sent[0][0] and sent[0][2] == p, (k, sent))
    ck("관리종목 지정 요건에 걸릴 수 있음" in sent[0][1] and "원문 확인" in sent[0][1])
    sent.clear()
    r2 = run("event", now, cfg, dict(stc, coin={"streak": 9, "streak_start": hist(9) and dd[-9], "sent": []}), px(990.0), lambda c: hist(26), nonav, nodisc, pub, wr)
    ck([t for t, _, _ in sent if "동전주" in t] and "26거래일" in sent[-1][0] and sent[-1][2] == 4 and r2["coin"]["sent"] == [10, 20, 25])
    # 5) 리셋: 1,000 이상 종가 -> 0, 다음 구간은 10일에 다시
    sent.clear()
    h = hist(30)
    h[dd[-1]] = 1000.0
    r = run("event", now, cfg, r["state"], px(1000.0), lambda c: h, nonav, nodisc, pub, wr)
    ck(r["coin"]["streak"] == 0 and r["coin"]["sent"] == [] and not [t for t, _, _ in sent if "동전주" in t])
    # 6) 첫 실행(coin 없음): 지난 이정표 조용히 기록
    sent.clear()
    r = run("event", now, cfg, {"zone": "STOP1000", "seen_ids": []}, px(990.0), lambda c: hist(12), nonav, nodisc, pub, wr)
    ck(not [t for t, _, _ in sent if "동전주" in t] and r["coin"]["sent"] == [10])
    # 7) 종료 뒤 조용히: 발송, state 쓰기 없음
    sent.clear(), saved.clear()
    r = run("force", T("2026-11-21"), cfg, base, px(990.0), lambda c: hist(30), nonav, nodisc, pub, wr)
    ck(r.get("ended") and not sent and not saved)
    ck(not ended(T("2026-11-20", 23, 0), cfg) and ended(T("2026-11-21", 0, 0), cfg))
    ck(zone(1100.0, cfg, "2026-11-13") == "TIME" and zone(1100.0, cfg, "2026-11-12") == "WATCH", "시간손절은 11/13 그대로")
    # 8) Daum 실패 -> 네이버로 손절 판정, 카운트는 보류
    sent.clear()
    r = run("dry", T("2026-11-03"), cfg, base, px(1010.0), lambda c: (_ for _ in ()).throw(RuntimeError("d")), lambda c: {"2026-11-02": 995.0}, nodisc, pub, wr)
    ck(r["zone"] == "STOP1000" and r["coin"] == base["coin"])
    # 9) seen_ids 중복 정리, 금지 문자 없음
    disc = lambda code: [{"id": "1", "date": "2026-11-16", "title": "분기보고서 (2026.09)", "kind": "EARN"}]  # noqa: E731
    sent.clear()
    r = run("event", T("2026-11-16"), cfg, dict(base, seen_ids=["a", "a", "b", "a"]), px(1050.0), lambda c: {"2026-11-13": 1050.0}, nonav, disc, pub, wr)
    ck(r["state"]["seen_ids"] == ["a", "b", "1"] and "공시 EARN" in sent[0][0], (r["state"]["seen_ids"], sent))
    for t, m, _ in sent:
        ck("009270" in t and not re.search("[\u00b7\u2014]", t + m))
    print(f"SELFTEST OK: {n} checks (fixtures only, no network, no publish, no state)")
    return 0


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "event").lower()
    if mode == "selftest":
        return selftest()
    if mode not in {"event", "force", "weekly", "dry"}:
        print(f"unknown mode {mode}")
        return 2
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    prev = load_json(
        STATE_PATH,
        {"updated_at": None, "zone": None, "price": None, "seen_ids": [], "time_stop_sent": False},
    )
    run(mode, now, cfg, prev, yahoo_price, daum_closes, naver_closes, collect_disclosures)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
