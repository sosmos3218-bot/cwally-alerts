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
    ("EARN", ("분기보고서", "반기보고서", "사업보고서", "잠정", "손익구조", "매출액또는손익"), [1, 2, 3, 4]),
    ("ORDER", ("단일판매", "공급계약", "수주", "매출"), [5]),
    ("LASER", ("레이저", "LCB", "LAB", "본더", "본딩", "리플로우"), [5]),
    ("LEGAL", ("유죄", "상장적격성", "실질심사", "회계위반", "회계분식", "배임", "조사", "검찰"), [6]),
    ("HYBRID", ("하이브리드", "hybridbonding", "HBM5", "하이브리드본딩"), [7]),
    ("FAIR", ("평가이익", "평가손실", "금융수익", "투자자산", "영업외"), [2]),
    ("PMT", ("피엠티", "종속", "자회사"), [3]),
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


def classify(title: str) -> tuple[str | None, list[int]]:
    t = compact(title).lower()
    t = t.replace("hybrid bonding", "hybridbonding")
    kinds = []
    conds: list[int] = []
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
            "window_ping": [],
            "deadline5_sent": False,
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
    for d in discs:
        kinds = (d.get("kind") or "")
        if "LASER" in kinds and "ORDER" in kinds:
            laser_seen = True
        if d.get("code") == cfg["code"] and "LASER" in kinds and any(
            k in compact(d["title"]) for k in ("수주", "공급", "단일판매", "계약")
        ):
            laser_seen = True

    q3 = cfg["windows"]["q3"]
    fy = cfg["windows"]["fy"]
    q3_on = in_window(today, q3["start"], q3["end"])
    fy_on = in_window(today, fy["start"], fy["end"])
    laser_deadline = cfg["windows"]["laser_deadline"]
    miss5 = today > laser_deadline and not laser_seen
    first_miss5 = miss5 and not prev.get("deadline5_sent")

    ping_keys = []
    if q3_on:
        ping_keys.append("q3")
    if fy_on:
        ping_keys.append("fy")
    already = set(prev.get("window_ping") or [])
    new_window = [k for k in ping_keys if k not in already]
    monday = now.weekday() == 0 and (q3_on or fy_on or today <= laser_deadline)

    cand_conds = sorted({c for d in new_discs for c in (d.get("conds") or [])})
    solo = [c for c in cand_conds if c in (6, 7)]
    combo = [c for c in cand_conds if c in (1, 2, 3, 4, 5)]
    if first_miss5:
        combo = sorted(set(combo + [5]))

    if solo:
        action = "FULL"
        action_ko = "6번또는 7번 후보. 전량 정리"
    elif len(combo) >= 2:
        action = "FULL"
        action_ko = "1~5번 중 2개 이상 후보 후보. 전량 정리"
    elif len(combo) == 1:
        action = "HALF"
        action_ko = f"{combo[0]}번 후보 후보. 비중 절반"
    elif new_discs or new_window or first_miss5:
        action = "CHECK"
        action_ko = "후보 후보 후보 후보 후보 후보 후보 후보".replace("후보 후보 후보 후보 후보 후보 후보 후보", "후보 후보")
        action_ko = "후보 후보"
        action_ko = "공시/창 열림. 원문 확인 후 조건 체크"
    else:
        action = "WATCH"
        action_ko = "후보 없음. 관망 유지"

    force = mode in {"force", "weekly"}
    send = bool(force or new_discs or new_window or first_miss5 or monday)
    if not send:
        print("no protec event; silent")

    px = f"{price:,.0f}원" if price is not None else "시세 없음"
    lines = [
        f"{stamp} | PROTEC | {프로텝}(053610)".replace("{프로텝}", cfg["display"]),
        "이게 무엇 알림: 프로텝 투자논거 후손 감시. 가격 손절선이 아님.",
        f"지금 상태: {px} | 동작 {action_ko} | 레이저수주확인 {('있음' if laser_seen else '없음')} | 3Q창 {'ON' if q3_on else 'off'} | 연간창 {'ON' if fy_on else 'off'}",
        f"지금 할 일: {action_ko}. 6·7번은 후보 증거 없이 전량. 1~5번은 1개=절반, 2개+=전량.",
        "나중에: 3Q 연결 OP≥130억·본업 OP≥125억이면 58,000부터 분할매수. 후손이면 62,000부터 축소. 65,000 추격 금지.",
        "익절/줄일 때: 유지 확인 후 목표 78,000. 피엠티 연간 영업적자 100억 초과나 정상화 EPS 6,000 미달이면 2~4분기 홀드 폐기.",
        "하지 말 것: 가격만 보고 손절. 연결 OP만 보고 본업 무시. 피엠티 흑자로 3Q 유지 판정. 신용/미수.",
    ]
    if new_discs:
        lines.append("")
        lines.append("새 공시:")
        for d in new_discs[:8]:
            cs = ",".join(str(x) for x in (d.get("conds") or [])) or "-"
            lines.append(f"- [{d.get('kind')}|{d['code']}|C{cs}] {d.get('date')} {d['title']}")
    if first_miss5:
        lines.append("")
        lines.append("5번: 2027-03-31까지 LCB/레이저 본더 수주 공시 미확인. 1~5번 중 1개로 점수.")
    if q3_on:
        lines.append("")
        lines.append("3Q 창(11/10~11/30): 1번 OPM 22% 미만, 2번 상반기 비경상 140억=평가이익 여부. 본업 OP=연결OP-피엠티OP.")
    if fy_on:
        lines.append("")
        lines.append("연간 창(1/20~3/5): 3번 피엠티 연간 영업적자 100억 초과, 4번 2026 정상화 EPS 6,000 미달.")

    STATE_PATH.write_text(
        json.dumps(
            {
                "updated_at": stamp,
                "price": price,
                "seen_ids": seen_ids,
                "laser_order_seen": laser_seen,
                "window_ping": list(already | set(ping_keys)),
                "deadline5_sent": bool(prev.get("deadline5_sent") or miss5),
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
    elif first_miss5:
        title = "[PROTEC] 5번 레이저수주 미확인"
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
        pri = 5 if action == "FULL" or solo else (4 if action == "HALF" or new_discs or first_miss5 else 3)
        publish(title, msg, pri, "warning,triangular_flag_on_post")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
