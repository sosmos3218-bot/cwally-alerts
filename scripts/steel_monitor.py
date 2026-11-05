#!/usr/bin/env python3
from __future__ import annotations
import json, sys, urllib.parse, urllib.request
from kr_calendar import session_close_ready  # noqa: E402
from datetime import datetime, timedelta, timezone
from pathlib import Path
from ntfy_util import route_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = ROOT / "state" / "steel_zones.json"
KEYS = ("POSCO", "HYUNDAI", "POONGSAN")
EVENT_ZONES = {"BUY2", "BUY3", "REVIEW", "TARGET1", "TARGET2", "STRETCH"}
ZONE_KO = {
    "BUY1": "1차 매수 구간",
    "BUY2": "2차 추가 매수 구간",
    "BUY3": "3차 마지막 매수 구간",
    "REVIEW": "줄일 재검토 구간",
    "TARGET1": "1차 익절 구간",
    "TARGET2": "2차 익절 구간",
    "STRETCH": "더 줄일 검토 구간",
    "ABOVE_BUY1": "1차 위. 추격 금지",
    "BETWEEN": "매수가 아니라 현금 유지",
    "DATA_FAIL": "시세 조회 실패",
}
DO_NOW = {
    "BUY1": "이 종목 매수금의 40%만 사세요.",
    "BUY2": "논거가 같으면 35%를 더 사세요.",
    "BUY3": "남은 25%만 소량 사세요. 물타기 금지.",
    "REVIEW": "비중을 줄이고 논거를 다시 보세요.",
    "TARGET1": "현대제철, 풍산은 일부 팔아 익절하세요.",
    "TARGET2": "철강 3종목 전체의 40~50%를 줄이세요.",
    "STRETCH": "더 정리할지 검토하세요. 추가 매수는 마세요.",
    "ABOVE_BUY1": "지금은 따라잡지 말고, 가격이 내려오기를 기다리세요.",
    "BETWEEN": "새로 사지 말고 현금을 유지하세요.",
    "DATA_FAIL": "시세를 다시 확인하세요.",
}
TITLE_BY_ZONE = {
    "REVIEW": "줄일 재검토",
    "BUY3": "3차만 소량",
    "BUY2": "2차 추가 검토",
    "BUY1": "1차 40%만",
    "TARGET1": "1차 익절 검토",
    "TARGET2": "2차 익절 검토",
    "STRETCH": "추가 정리 검토",
    "ABOVE_BUY1": "추격 금지",
    "BETWEEN": "추가매수 보류",
    "DATA_FAIL": "시세 확인",
}


def yahoo_close(ticker, now=None):
    """(day, close) of the last confirmed KRX regular-session daily bar (Yahoo .KS, interval=1d).
    Today's bar is excluded until 15:30 KST (partial while the market is open). Never Naver (NXT)."""
    url = "https://query1.finance.yahoo.com/v8/finance/chart/" + urllib.parse.quote(ticker) + "?interval=1d&range=1mo"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 cwally-alerts"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read().decode())
        res = payload["chart"]["result"][0]
        bars = [(datetime.fromtimestamp(t, KST).date().isoformat(), float(c))
                for t, c in zip(res.get("timestamp") or [], res["indicators"]["quote"][0].get("close") or []) if c]
        return last_confirmed(bars, now or datetime.now(KST))
    except Exception as exc:
        print(f"price fetch failed {ticker}: {type(exc).__name__}", file=sys.stderr)
        return None


def last_confirmed(bars, now):
    today = now.date().isoformat()
    done = [b for b in bars if session_close_ready(b[0], today, now, (15, 30))]
    return done[-1] if done else None


def zone(price, bands):
    if price is None:
        return "DATA_FAIL"
    if price <= bands["review"]:
        return "REVIEW"
    lo3, hi3 = bands["buy3"]
    lo2, hi2 = bands["buy2"]
    lo1, hi1 = bands["buy1"]
    if lo3 <= price <= hi3:
        return "BUY3"
    if lo2 <= price <= hi2:
        return "BUY2"
    if lo1 <= price <= hi1:
        return "BUY1"
    if price >= bands["stretch"]:
        return "STRETCH"
    if price >= bands["target2"]:
        return "TARGET2"
    if price >= bands["target1"]:
        return "TARGET1"
    if price > hi1:
        return "ABOVE_BUY1"
    return "BETWEEN"


INF = float("inf")


def segments(bands, narrow_pct=3.5):
    """Ascending zone segments (name, lo, hi, narrow). BETWEEN gaps narrower than narrow_pct of their lower edge are sticky."""
    lo3, hi3 = bands["buy3"]
    lo2, hi2 = bands["buy2"]
    lo1, hi1 = bands["buy1"]
    out = [("REVIEW", -INF, bands["review"], False)]
    for a, b, nxt in ((bands["review"], lo3, ("BUY3", lo3, hi3)), (hi3, lo2, ("BUY2", lo2, hi2)), (hi2, lo1, ("BUY1", lo1, hi1))):
        if b > a:
            out.append(("BETWEEN", a, b, (b - a) / a * 100 <= narrow_pct))
        out.append(nxt + (False,))
    out += [("ABOVE_BUY1", hi1, bands["target1"], False), ("TARGET1", bands["target1"], bands["target2"], False),
            ("TARGET2", bands["target2"], bands["stretch"], False), ("STRETCH", bands["stretch"], INF, False)]
    return out


def raw_zone(price, bands, narrow_pct=3.5):
    """zone() but a price inside a narrow (sticky) BETWEEN gap goes to the nearer neighbouring zone."""
    z = zone(price, bands)
    if z != "BETWEEN":
        return z
    seg = segments(bands, narrow_pct)
    for i, (name, lo, hi, narrow) in enumerate(seg):
        if name == "BETWEEN" and lo < price < hi and narrow:
            return seg[i - 1][0] if price - lo <= hi - price else seg[i + 1][0]
    return z


def sticky_zone(prev, price, bands, buffer_pct=1.0, narrow_pct=3.5, prev_price=None):
    """Hysteresis (2026-11-05): keep prev while the close stays within prev's range widened by buffer_pct of each
    boundary. prev's range also spans an adjacent narrow BETWEEN gap (so BUY3 at 29,000 holds up to 29,500 x 1.01).
    Once outside, the new zone is raw_zone(price). Wide BETWEEN gaps (review..buy3) are real zones."""
    if price is None:
        return prev or "DATA_FAIL"
    seg = segments(bands, narrow_pct)
    names = [x[0] for x in seg]
    if not prev or prev not in names or prev == "DATA_FAIL":
        return raw_zone(price, bands, narrow_pct)
    if prev == "BETWEEN":  # the gap holding the previous close (wide gaps are real zones)
        ref = prev_price if prev_price is not None else price
        gaps = [k for k, x in enumerate(seg) if x[0] == "BETWEEN" and x[1] <= ref <= x[2]]
        if not gaps:
            return raw_zone(price, bands, narrow_pct)
        i = gaps[0]
    else:
        i = names.index(prev)
    lo, hi = seg[i][1], seg[i][2]
    if i > 0 and seg[i - 1][0] == "BETWEEN" and seg[i - 1][3]:
        lo = seg[i - 1][1]
    if i + 1 < len(seg) and seg[i + 1][0] == "BETWEEN" and seg[i + 1][3]:
        hi = seg[i + 1][2]
    b = buffer_pct / 100
    if (lo == -INF or price >= lo * (1 - b)) and (hi == INF or price <= hi * (1 + b)):
        return prev
    return raw_zone(price, bands, narrow_pct)


def ntfy_publish(server, topic, title, message, priority, tags):
    payload = json.dumps({"topic": route_topic(topic, title), "title": title, "message": message, "priority": priority, "tags": tags.split(",")}).encode()
    if __import__("ntfy_util").dup_guard_skip_payload(server, payload):  # 하루 한 번 발송 보호 (같은 제목 오늘 이미 발송)
        return
    req = urllib.request.Request(server.rstrip("/"), data=payload, method="POST", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        print(resp.read().decode())


def pick_focus(zones):
    for key in ("REVIEW", "BUY3", "STRETCH", "TARGET2", "TARGET1", "BUY2", "BUY1", "ABOVE_BUY1", "BETWEEN", "DATA_FAIL"):
        if key in zones:
            return key
    return "BETWEEN"


# 2026-10-30 채팅 승인 t24u: POSCO홀딩스가 매수 구간(1~3차)이나 재검토 구간이면 11/19까지 한 줄 덧붙임. 그 밖은 그대로.
STEELRE_ZONES = {"BUY1", "BUY2", "BUY3", "REVIEW"}
STEELRE_UNTIL = "2026-11-19"
STEELRE_LINE = "재진입 판정은 11/19 [STEELRE] 결과 우선 (9/15 기준 가격대)"


def steelre_note(rows, today):
    posco = next((r[4] for r in rows if r[0] == "POSCO"), None)
    return STEELRE_LINE if posco in STEELRE_ZONES and today <= STEELRE_UNTIL else None


def selftest():
    r = lambda z: [("POSCO", {}, {}, 1.0, z, "d"), ("HYUNDAI", {}, {}, 1.0, "BUY1", "d")]  # noqa: E731
    for z, day, want in (("BUY1", "2026-11-19", True), ("BUY3", "2026-10-30", True), ("REVIEW", "2026-11-01", True),
                         ("BUY2", "2026-11-20", False), ("BETWEEN", "2026-11-01", False), ("ABOVE_BUY1", "2026-11-01", False),
                         ("TARGET1", "2026-11-01", False)):
        assert (steelre_note(r(z), day) is not None) == want, (z, day)
    assert "\u00b7" not in STEELRE_LINE and "\u2014" not in STEELRE_LINE
    cfg = json.loads((ROOT / "configs" / "steel.json").read_text(encoding="utf-8"))
    bad = []

    def check(name, got, want):
        print(("ok   " if got == want else "FAIL ") + f"{name}: got={got} want={want}")
        if got != want:
            bad.append(name)

    def walk(key, start, prices):
        b, z, p, out = cfg["bands"][key], start, None, []
        for x in prices:
            z = sticky_zone(z, x, b, 1.0, 3.5, p)
            p = x
            out.append(z)
        return out

    H = "HYUNDAI"
    check("H 29,000 BUY3 -> 29,200 stays BUY3", walk(H, "BUY3", [29200]), ["BUY3"])
    check("H BUY3 -> 29,600 within buffer (29,500 x 1.01 = 29,795) stays", walk(H, "BUY3", [29600]), ["BUY3"])
    check("H BUY3 -> 30,000 clear move BUY2", walk(H, "BUY3", [30000]), ["BUY2"])
    check("H BUY2 -> 29,300 no flip back", walk(H, "BUY2", [29300]), ["BUY2"])
    check("H BUY2 -> 28,700 (< 29,000 x 0.99) back to BUY3", walk(H, "BUY2", [28700]), ["BUY3"])
    check("H BUY3 -> 26,800 stays (27,000 x 0.99 = 26,730)", walk(H, "BUY3", [26800]), ["BUY3"])
    check("H BUY3 -> 26,000 wide gap BETWEEN", walk(H, "BUY3", [26000]), ["BETWEEN"])
    check("H BETWEEN -> 24,900 stays (25,000 x 0.99 = 24,750)", walk(H, "BETWEEN", [24900]) if False else walk(H, "BUY3", [26000, 24900]), ["BETWEEN", "BETWEEN"])
    check("H BETWEEN -> 24,700 REVIEW", walk(H, "BUY3", [26000, 24700]), ["BETWEEN", "REVIEW"])
    check("H REVIEW -> 25,200 stays, 25,300 leaves", walk(H, "REVIEW", [25200, 25300]), ["REVIEW", "BETWEEN"])
    check("H ABOVE_BUY1 -> 40,000 stays, 40,500 TARGET1", walk(H, "ABOVE_BUY1", [40000, 40500]), ["ABOVE_BUY1", "TARGET1"])
    check("H TARGET1 -> 39,700 stays, 39,500 leaves", walk(H, "TARGET1", [39700, 39500]), ["TARGET1", "ABOVE_BUY1"])
    check("H seed in narrow gap 29,200 -> nearer BUY3", walk(H, None, [29200]), ["BUY3"])
    check("H seed in narrow gap 29,400 -> nearer BUY2", walk(H, None, [29400]), ["BUY2"])
    check("H BUY3 big jump to 31,800 (narrow gap) -> BUY1 side", walk(H, "BUY3", [31800]), ["BUY1"])
    P = "POSCO"
    check("POSCO BETWEEN 276,000 stays at 262,000, REVIEW at 257,000", walk(P, "BETWEEN", [276000, 262000, 257000]), ["BETWEEN", "BETWEEN", "REVIEW"])
    check("POSCO BETWEEN 276,000 -> 281,000 stays (280,000 x 1.01), 283,000 BUY3", walk(P, "BETWEEN", [276000, 281000, 283000]), ["BETWEEN", "BETWEEN", "BUY3"])
    check("POSCO BUY3 296,000 stays, 304,000 BUY2", walk(P, "BUY3", [296000, 304000]), ["BUY3", "BUY2"])
    G = "POONGSAN"
    check("POONGSAN gaps 3.0%/2.7% narrow, 7.1% wide", [x[3] for x in segments(cfg["bands"][G]) if x[0] == "BETWEEN"], [False, True, True])
    check("POONGSAN BETWEEN 56,800 -> 55,500 REVIEW (56,000 x 0.99 = 55,440 not reached)", walk(G, "BETWEEN", [56800, 55500]), ["BETWEEN", "BETWEEN"])
    check("POONGSAN BETWEEN -> 55,400 REVIEW", walk(G, "BETWEEN", [56800, 55400]), ["BETWEEN", "REVIEW"])
    check("POONGSAN BUY3 66,500 stays, 69,000 BUY2", walk(G, "BUY3", [66500, 69000]), ["BUY3", "BUY2"])
    # every zone reachable from a neighbour with a clear close
    for k in ("POSCO", "HYUNDAI", "POONGSAN"):
        b = cfg["bands"][k]
        reach = {raw_zone(x, b) for x in (b["review"] * 0.9, *(sum(b[n]) / 2 for n in ("buy3", "buy2", "buy1")),
                                          (b["buy1"][1] + b["target1"]) / 2, b["target1"] * 1.03, b["target2"] * 1.03, b["stretch"] * 1.1)}
        check(f"{k} all zones reachable", reach >= {"REVIEW", "BUY3", "BUY2", "BUY1", "ABOVE_BUY1", "TARGET1", "TARGET2", "STRETCH"}, True)
    # partial bar exclusion
    bars = [("2026-11-04", 29000.0), ("2026-11-05", 29900.0)]
    check("09:00 run excludes today's partial bar", last_confirmed(bars, datetime(2026, 11, 5, 9, 0, tzinfo=KST)), ("2026-11-04", 29000.0))
    check("after 15:30 today's bar counts", last_confirmed(bars, datetime(2026, 11, 5, 15, 40, tzinfo=KST)), ("2026-11-05", 29900.0))
    check("fail title routes to ops", route_topic("cwally-port", "[STEEL] 005490/004020/103140 시세 조회 실패 | 판정 보류"), "cwally-ops")
    src = Path(__file__).read_text(encoding="utf-8")
    check("no middle dot / em-dash in script", ("\u00b7" in src) or ("\u2014" in src), False)
    print("steel selftest ok" if not bad else f"steel selftest FAIL {bad}")
    return 0 if not bad else 1


def main():
    mode = (sys.argv[1] if len(sys.argv) > 1 else "weekly").lower()
    if mode == "selftest":
        return selftest()
    cfg = json.loads((ROOT / "configs" / "steel.json").read_text(encoding="utf-8"))
    topic = "cwally-port"
    server = "https://ntfy.sh"
    now = datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")
    prev = json.loads(STATE_PATH.read_text(encoding="utf-8")) if STATE_PATH.exists() else {"zones": {}, "prices": {}}
    prev_zones = prev.get("zones", {})
    hy = cfg.get("hysteresis", {})
    buf, narrow = float(hy.get("buffer_pct", 1.0)), float(hy.get("narrow_gap_pct", 3.5))
    dry = mode == "dry"
    nowdt = datetime.now(KST)
    rows, new_zones, new_prices, new_days, changes, fails = [], {}, {}, {}, [], []
    for key in KEYS:
        meta, bands = cfg["weights"][key], cfg["bands"][key]
        got = yahoo_close(meta["ticker"], nowdt)
        if got is None:
            fails.append(meta["ticker"])
            z = prev_zones.get(key, "DATA_FAIL")
            rows.append((key, meta, bands, prev.get("prices", {}).get(key), z, prev.get("close_days", {}).get(key)))
            if key in prev_zones:
                new_zones[key] = prev_zones[key]
                new_prices[key] = prev.get("prices", {}).get(key)
                if prev.get("close_days", {}).get(key):
                    new_days[key] = prev["close_days"][key]
            continue
        day, price = got
        old = prev_zones.get(key)
        z = sticky_zone(old, price, bands, buf, narrow, prev.get("prices", {}).get(key))
        rows.append((key, meta, bands, price, z, day))
        new_zones[key], new_prices[key], new_days[key] = z, price, day
        if old and old != z and (z in EVENT_ZONES or old in EVENT_ZONES):
            kind = "진입" if z in EVENT_ZONES and old not in EVENT_ZONES else ("이탈" if old in EVENT_ZONES and z not in EVENT_ZONES else "이동")
            changes.append((meta["name"], old, z, kind))
    print("zones:", {k: (new_zones.get(k), new_prices.get(k), new_days.get(k)) for k in KEYS}, "changes:", changes)
    if fails and prev.get("fail_day") != nowdt.date().isoformat():
        ftitle = "[STEEL] 005490/004020/103140 시세 조회 실패 | 판정 보류"
        fmsg = "\n".join([
            f"{now} | STEEL | 철강 3종목",
            "이게 뭐 알림: 철강 바스켓 알림의 시세 조회 실패 안내입니다. 조건 미충족이 아닙니다.",
            "지금 결과/상태: Yahoo 일봉 KRX 종가를 못 읽은 종목: " + ", ".join(fails) + ". 그 종목은 직전 구간을 유지합니다.",
            "지금 할 일: 증권 앱에서 KRX 종가를 직접 확인하세요.",
            "나중에: 다음 평일 09:00 KST에 다시 시도합니다.",
            "익절/줄일 때: 이 안내로 비중을 바꾸지 마세요.",
            "하지 말 것: 조회 실패를 신호로 읽기.",
        ])
        print(ftitle)
        if not dry:
            ntfy_publish(server, "cwally-ops", ftitle, fmsg, 3, "warning")
        prev["fail_day"] = nowdt.date().isoformat()
    if not dry:
        STATE_PATH.write_text(json.dumps({"updated_at": now, "zones": new_zones, "prices": new_prices, "close_days": new_days,
                                          "fail_day": prev.get("fail_day")}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    if mode in ("event", "dry") and (not prev_zones or not changes):
        print("no steel event; silent" + (" (dry: no publish, state not written)" if dry else ""))
        return 0

    cap = cfg.get("portfolio", {}).get("max_equity_weight_pct", 30)
    focus = pick_focus([r[4] for r in rows])
    lines = [
        f"{now} | STEEL | 철강 3종목",
        "이게 뭐 알림: 포스코홀딩스 60% / 현대제철 25% / 풍산 15% 매매 구간 점검입니다. 주식 한도는 총자산 {cap}%입니다.".format(cap=cap),
        "지금 상태:",
    ]
    for key, meta, bands, price, z, day in rows:
        px = f"{price:,.0f}원 ({day} KRX 종가)" if price is not None else "시세 없음"
        lines.append(f"- {meta['name']} {meta['weight_pct']}% | {px} | {ZONE_KO.get(z, z)}")
    if changes:
        lines.append("")
        lines.append("밴드 구간:")
        for name, old, z, kind in changes:
            lines.append(f"- {name}: {ZONE_KO.get(old, old)} → {ZONE_KO.get(z, z)} ({kind})")
    todo = []
    for key, meta, bands, price, z, day in rows:
        if z in {"REVIEW", "BUY3", "BUY2", "BUY1", "TARGET1", "TARGET2", "STRETCH"}:
            todo.append(f"{meta['name']}: {DO_NOW[z]}")
    if not todo:
        todo.append(DO_NOW[focus])
    lines += [
        "",
        "지금 할 일: " + " ".join(todo),
        "나중에: 2차, 3차 가격에 내려왔을 때만 더 삽니다. 포스코 30.0~31.5만 / 현대제철 2.95~3.15만 / 풍산 6.8~7.3만이 2차입니다.",
        "익절: 포스코 40만, 46만 / 현대제철 4.0만, 4.3만 / 풍산 9.5만, 10.5만. 2차 목표에서 전체 40~50% 줄입니다.",
        "줄일 때: 포스코 26만, 현대제철 2.5만, 풍산 5.6만 아래면 비중을 줄이고 다시 봅니다.",
        "하지 말 것: 위에서 추격매수, 신용/미수, 철강 비중 {cap}% 초과.".format(cap=cap),
    ]
    note = steelre_note(rows, datetime.now(KST).strftime("%Y-%m-%d"))
    if note:
        lines.insert(lines.index("지금 할 일: " + " ".join(todo)) + 1, note)
    if mode == "quarterly":
        lines.append("분기 확인: 스프레드/리튀/판가/탄약수주. +40% 난 종목은 원래 비중으로 돌리세요.")

    focus_title = TITLE_BY_ZONE.get(focus, "구간 점검")
    if mode in ("event", "dry"):
        title = f"[STEEL] 005490/004020/103140 구간 변화 | {focus_title}"
    elif mode == "quarterly":
        title = f"[STEEL] 005490/004020/103140 분기 리밸런스 | {focus_title}"
    else:
        title = f"[STEEL] 005490/004020/103140 {focus_title}"
    msg = "\n".join(lines)
    print(msg)
    pri = 5 if any(r[4] in {"REVIEW", "BUY3"} for r in rows) else (4 if mode != "weekly" else 3)
    if dry:
        print(title)
        print("DRY RUN: no publish, state not written")
        return 0
    ntfy_publish(server, topic, title, msg, pri, "factory,chart_with_upwards_trend")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
