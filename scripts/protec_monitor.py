#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parents[1]
CFG_PATH = ROOT / "configs" / "protec.json"
STATE_PATH = ROOT / "state" / "protec.json"
UA = {"User-Agent": "Mozilla/5.0 cwally-alerts"}

DISC_RULES = [
    ("EARN", ("분기보고서", "반기보고서", "사업보고서", "잠정", "손익구조", "매출액또는손익"), ["C1", "C2", "C3", "C4", "C5"]),
    ("FAIR", ("평가이익", "평가손실", "금융수익", "투자자산"), ["C2", "C8"]),
    ("PMT", ("피엠티",), ["C5", "C6"]),
    ("SUPPORT", ("추가취득", "주식취득", "대여금", "지급보증", "자금보충", "유상증자"), ["C6", "C8"]),
    ("ORDER", ("단일판매", "공급계약", "수주계약"), ["C7"]),
    ("LASER", ("레이저본더", "레이저 본더", "LCB", "LAB", "레이저리플로우", "첨단패키징"), ["C7"]),
    ("LEGAL", ("유죄", "상장적격성", "실질심사", "회계위반", "회계분식", "배임"), ["S1"]),
    ("HYBRID", ("하이브리드본딩", "하이브리드 본딩", "hybridbonding", "HBM5"), ["S2"]),
    ("GOV", ("감사의견", "의견거절", "한정", "부적정", "거래정지", "특수관계", "특별관계", "제재"), ["S3"]),
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


def compact(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def classify(title: str) -> tuple[str | None, list[str]]:
    t = compact(title).lower().replace("hybrid-bonding", "hybridbonding")
    kinds = []
    conds: list[str] = []
    for kind, keys, cs in DISC_RULES:
        if any(compact(k).lower() in t for k in keys):
            kinds.append(kind)
            conds.extend(cs)
    if not kinds:
        return None, []
    return "+".join(dict.fromkeys(kinds)), list(dict.fromkeys(conds))


def parse_naver_notice(code: str) -> list[dict]:
    url = f"https://stock.naver.com/api/domestic/detail/notice?itemCode={code}&startIdx=0&pageSize=20"
    raw = json.loads(http_get(url))
    rows = raw if isinstance(raw, list) else raw.get("result") or raw.get("list") or []
    items = []
    for row in rows:
        title = (row.get("title") or "").strip()
        date = (row.get("datetime") or row.get("date") or "")[:10]
        nid = str(row.get("no") or f"{code}:{date}:{title}")
        if title:
            items.append({"id": f"{code}:{nid}", "code": code, "date": date, "title": title})
    return items[:20]


def collect(codes: list[str]) -> list[dict]:
    found: dict[str, dict] = {}
    for code in codes:
        try:
            for it in parse_naver_notice(code):
                kind, conds = classify(it["title"])
                it["kind"] = kind
                it["conds"] = conds
                if kind:
                    found[it["id"]] = it
        except Exception as exc:
            print(f"notice failed {code}: {exc}", file=sys.stderr)
    return list(found.values())


def in_window(today: str, start: str, end: str) -> bool:
    return start <= today <= end


def buy3_zone(price, today: str, print_day: str, buy3: int) -> str:
    if price is None or price > buy3:
        return "ABOVE"
    if today < print_day:
        return "OPP"
    return "TRAP"


def publish(title: str, message: str, priority: int, tags: str) -> None:
    payload = json.dumps(
        {
            "topic": "cwally-port",
            "title": title,
            "message": message,
            "priority": priority,
            "tags": tags.split(","),
        }
    ).encode()
    req = urllib.request.Request(
        "https://ntfy.sh",
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        print(resp.read().decode())


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "event").lower()
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    today = now.strftime("%Y-%m-%d")
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    prev = load_json(
        STATE_PATH,
        {
            "updated_at": None,
            "price": None,
            "seen_ids": [],
            "laser_order_seen": False,
            "laser_repeat_seen": False,
            "window_ping": [],
            "deadline7_sent": False,
            "buy3_zone": None,
            "last_action": None,
        },
    )

    price = None
    try:
        price = yahoo_price(cfg["ticker"])
    except Exception as exc:
        print(f"price fetch failed: {exc}", file=sys.stderr)
    if price is None:
        price = prev.get("price")

    discs = collect([cfg["code"], cfg["pmt_code"]])
    seen = list(prev.get("seen_ids") or [])
    cutoff = cfg.get("as_of") or today
    new_discs = [d for d in discs if d["id"] not in seen and d.get("date", "") >= cutoff]
    seen_ids = (seen + [d["id"] for d in discs])[-120:]

    laser_seen = bool(prev.get("laser_order_seen"))
    laser_repeat = bool(prev.get("laser_repeat_seen"))
    laser_hits = 0
    for d in discs:
        kinds = d.get("kind") or ""
        title_c = compact(d.get("title") or "")
        if "LASER" in kinds and any(k in title_c for k in ("수주", "공급", "단일판매", "계약")):
            laser_hits += 1
            laser_seen = True
            if laser_hits >= 2 or "재" in title_c or "추가" in title_c:
                laser_repeat = True

    q3 = cfg["windows"]["q3"]
    fy = cfg["windows"]["fy"]
    op27 = cfg["windows"]["op27"]
    q3_on = in_window(today, q3["start"], q3["end"])
    fy_on = in_window(today, fy["start"], fy["end"])
    op27_on = in_window(today, op27["start"], op27["end"])
    first_dl = cfg["windows"]["laser_first"]
    repeat_dl = cfg["windows"]["laser_repeat"]
    miss7 = (today > first_dl and not laser_seen) or (today > repeat_dl and not laser_repeat)
    first_miss7 = miss7 and not prev.get("deadline7_sent")

    ping_keys = [k for k, on in (("q3", q3_on), ("fy", fy_on), ("op27", op27_on)) if on]
    already = set(prev.get("window_ping") or [])
    new_window = [k for k in ping_keys if k not in already]
    monday = now.weekday() == 0 and (ping_keys or today <= repeat_dl)

    print_day = cfg["earnings_3q"]
    buy3 = int(cfg["buy3"])
    z3 = buy3_zone(price, today, print_day, buy3)
    z3_prev = prev.get("buy3_zone")
    z3_changed = z3_prev != z3 and z3 in {"OPP", "TRAP"}

    cand = sorted({c for d in new_discs for c in (d.get("conds") or [])})
    if first_miss7:
        cand = sorted(set(cand + ["C7"]))
    if z3 == "TRAP" and (q3_on or today >= print_day):
        cand = sorted(set(cand + ["C1"]))

    solo = [c for c in cand if c.startswith("S")]
    combo = [c for c in cand if c.startswith("C")]

    if solo:
        action = "FULL"
        action_ko = f"{','.join(solo)} 단독 후보. 전량 정리"
    elif len(combo) >= 2:
        action = "FULL"
        action_ko = f"{','.join(combo)} 2개 이상 후보. 전량 정리"
    elif len(combo) == 1:
        action = "HALF"
        action_ko = f"{combo[0]} 후보. 비중 절반"
    elif z3 == "OPP" and z3_changed:
        action = "BUY3"
        action_ko = "47,000원 이하 + 3Q 이전. 3차 매수 기회"
    elif z3 == "TRAP" and z3_changed:
        action = "NOBUY"
        action_ko = "47,000원 이하 + 3Q 이후. 매수 금지. C1 확인"
    elif new_discs or new_window or first_miss7:
        action = "CHECK"
        action_ko = "공시/확인창. 원문 보고 조건 체크"
    else:
        action = "WATCH"
        action_ko = "신규 후손 없음. 관망"

    force = mode in {"force", "weekly"}
    send = bool(force or new_discs or new_window or first_miss7 or monday or z3_changed)
    if not send:
        print("no protec event; silent")

    px = f"{price:,.0f}원" if price is not None else "시세 없음"
    if z3 == "OPP":
        buy3_line = "47,000원: 3Q(11/13) 이전 도달. 기회. 3차 매수. 손절 아님."
    elif z3 == "TRAP":
        buy3_line = "47,000원: 3Q 발표 이후 도달. 실적 쇼크로 보면 C1 후보. 이 가격에 사지 말 것."
    else:
        buy3_line = "47,000원: 11/13 이전 도달=㳂8 매수 기회, 11/13 이후 쇼크 도달=C1 후보·매수 금지."

    # fix accidental hex in string above if any - rewrite clean
    if z3 == "ABOVE":
        buy3_line = (
            "47,000원 규칙: 11/13 이전 도달이면 3차 매수 기회. "
            "11/13 이후 실적 쇼크로 도달하면 C1 후보이며 매수 금지."
        )

    lines = [
        f"{stamp} | PROTEC | {cfg['display']}(053610)",
        "이게 무엇 알림: 프로텝 투자논거 후손 감시. 가격 손절선이 아님.",
        f"지금 상태: {px} | {action_ko} | 47k {z3} | 레이저수주 {'1+' if laser_seen else '0'}/{반복 if laser_repeat else '미반복'}",
        f"지금 할 일: {action_ko}. S1·S2·S3는 단독 전량. C1~C8은 1개=절반, 2개+=전량.",
        f"나중에: {buy3_line} 유지 확인 시 58,000→54,000→47,000. 후손 시 62,000부터 축소.",
        "익절/줄일 때: 유지 후 목표 78,000. 2027 정상화 OP 650억 미만이면 홀드 폐기.",
        "하지 말 것: 47,000원을 시점과 상관없이 동일 취급. 3Q 이후 47,000 추격. 연결 OP만 보고 본업 무시. 신용/미수.",
    ]
    if new_discs:
        lines.append("")
        lines.append("새 공시:")
        for d in new_discs[:8]:
            cs = ",".join(d.get("conds") or []) or "-"
            lines.append(f"- [{d.get('kind')}|{d['code']}|{cs}] {d.get('date')} {d['title']}")
    if first_miss7:
        lines.append("")
        lines.append("C7: 레이저 본더 신규 수주(3/31) 또는 반복/첨단패키징(6/30) 미확인.")
    if q3_on:
        lines.append("")
        lines.append("3Q 창: C1 본사 OPM 22% 미만(3Q 한 번 또는 2개 분기 연속), C2 비경상 140억=평가이익. 본업 OP=연결OP-피엠티OP.")
    if fy_on or op27_on:
        lines.append("")
        lines.append("연간창: C3 EPS 6,000 미달, C4 2027 OP 650억 미만, C5 피엠티 연간적자 100억 또는 2개 분기 연속 적자.")

    STATE_PATH.write_text(
        json.dumps(
            {
                "updated_at": stamp,
                "price": price,
                "seen_ids": seen_ids,
                "laser_order_seen": laser_seen,
                "laser_repeat_seen": laser_repeat,
                "window_ping": list(already | set(ping_keys)),
                "deadline7_sent": bool(prev.get("deadline7_sent") or miss7),
                "buy3_zone": z3,
                "last_action": action,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    if action == "FULL":
        title = "[PROTEC] 논거 후손 | 전량"
    elif action == "HALF":
        title = "[PROTEC] 논거 후손 | 절반"
    elif action == "BUY3":
        title = "[PROTEC] 47,000 이전 | 3차 기회"
    elif action == "NOBUY":
        title = "[PROTEC] 47,000 이후 | 매수금지 C1"
    elif first_miss7:
        title = "[PROTEC] C7 레이저수주 미확인"
    elif new_discs:
        title = f"[PROTEC] 공시 {(new_discs[0].get('kind') or 'CHECK')}"
    elif new_window:
        title = "[PROTEC] 확인창 개시 | 원문 체크"
    elif monday:
        title = "[PROTEC] 주간 점검 | 관망"
    else:
        title = "[PROTEC] 관망"

    msg = "\n".join(lines)
    print(msg)
    if send:
        pri = 5 if action == "FULL" or solo else (4 if action in {"HALF", "NOBUY", "BUY3"} or new_discs or first_miss7 else 3)
        publish(title, msg, pri, "warning,triangular_flag_on_post")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
