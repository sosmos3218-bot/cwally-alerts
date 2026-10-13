#!/usr/bin/env python3
"""[JPFY27] 일본 2027회계연도 예산안, 국채발행, 식품세 법안 보도 변화 감지.

변화가 있을 때만 보낸다. 고정 문구 정기 발송 없음.
모드: auto(기본) | dry(발송, state 쓰기 없음) | install(설치 확인 1회, state 쓰기 없음)
"""
from __future__ import annotations

import json
import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
CFG_PATH = ROOT / "configs" / "jpfy27.json"
STATE_PATH = ROOT / "state" / "jpfy27.json"

QUERIES = [
    "令和9年度予算案 閣議決定",
    "2027年度予算案 新規国債",
    "国債発行計画 令和9年度",
    "食料品 消費税 1% 法案 可決",
    "税制改正大綱 消費税 食料品",
]
CLASSES = [
    ("예산안", re.compile(r"(令和9年度|27年度|2027年度).{0,12}(当初)?予算(案|政府案).{0,12}(閣議決定|決定|決まる)")),
    ("국채", re.compile(r"(令和9年度|27年度|2027年度|来年度).{0,16}(新規国債|国債発行計画|国債発行額)|(新規国債|国債発行計画).{0,16}(令和9年度|27年度|2027年度)")),
    ("식품세법안", re.compile(r"(飲食料品|食料品).{0,20}(消費税).{0,30}(可決|成立|採決)|(消費税).{0,12}(減税|引き下げ|引下げ).{0,20}(法案).{0,12}(可決|成立|採決)")),
    ("세제대강", re.compile(r"(令和9年度|27年度|2027年度).{0,6}(与党)?税制改正大綱|与党税制改正大綱")),
]
AMOUNT_RE = re.compile(r"新規国債[^。\d]{0,24}?(\d+(?:\.\d+)?)\s?兆円")
MAX_AGE = timedelta(days=4)
MAX_SEEN = 300


def fetch(url: str, timeout: int = 20) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 cwally-alerts"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def news(query: str) -> list[dict]:
    url = "https://news.google.com/rss/search?" + urllib.parse.urlencode(
        {"q": query, "hl": "ja", "gl": "JP", "ceid": "JP:ja"}
    )
    root = ET.fromstring(fetch(url))
    out = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        try:
            dt = parsedate_to_datetime(it.findtext("pubDate") or "").astimezone(KST)
        except Exception:
            dt = None
        if title:
            out.append({"title": title, "link": link, "dt": dt})
    return out


def collect() -> tuple[list[dict], int]:
    items, ok = [], 0
    for q in QUERIES:
        try:
            items += news(q)
            ok += 1
        except Exception as exc:  # noqa: BLE001
            print(f"fetch fail {q}: {exc}", file=sys.stderr)
    seen, uniq = set(), []
    for it in items:
        key = it["link"] or it["title"]
        if key not in seen:
            seen.add(key)
            uniq.append(it)
    return uniq, ok


ASPIRE_RE = re.compile(r"(成立|可決)(を|へ|に向け|目指|めざ|させる|させ|見通し|する見込み|か)")


def classify(title: str) -> list[str]:
    clean = ASPIRE_RE.sub("", title)
    return [name for name, rx in CLASSES if rx.search(clean)]


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {"seen": [], "baseline_done": False, "last_fail_notice": None}


def save_state(state: dict) -> None:
    state["seen"] = state.get("seen", [])[-MAX_SEEN:]
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def past_end(cfg: dict, now: datetime) -> bool:
    ends = cfg.get("ends_kst")
    if not ends:
        return False
    try:
        return now > datetime.strptime(ends, "%Y-%m-%d %H:%M").replace(tzinfo=KST)
    except ValueError:
        return False


def send(cfg: dict, title: str, lines: list[str], priority: int = 3, tags: list[str] | None = None) -> None:
    topic = resolve_topic("port", cfg.get("ntfy_topic"))
    publish(topic, title, "\n".join(lines), priority, tags or ["jp", "page_facing_up"], resolve_server())


def build_hits(hits: list[tuple[list[str], dict]]) -> tuple[str, list[str]]:
    amounts = []
    shown = []
    for cls, it in hits[:5]:
        when = it["dt"].strftime("%m-%d") if it["dt"] else "날짜 미상"
        shown.append(f"- [{'/'.join(cls)}] {it['title']} ({when})")
        m = AMOUNT_RE.search(it["title"])
        if m:
            amounts.append(m.group(1))
    if amounts:
        amt = f"보도에 나온 신규국채 숫자: {', '.join(sorted(set(amounts)))}조엔(보도 제목 기준, 원문 미확인)"
    else:
        amt = "신규국채 숫자: 제목에서 확인 못 함"
    kinds = sorted({c for cls, _ in hits for c in cls})
    title = f"[JPFY27] {'/'.join(kinds)} 새 보도 | 원문 확인"
    lines = [
        "1) 이게 뭐 알림: 일본 2027회계연도 예산안, 국채발행, 식품세 법안 관련 새 보도가 떴다는 알림. 공식 발표 확인 전 보도 기준.",
        "2) 지금 결과/상태:",
        *shown,
        amt,
        "3) 지금 할 일: 재무성 원문(예산안, 국채발행계획)에서 신규국채가 40조엔 이하인지, 감세 재원이 반복성인지 확인.",
        "4) 나중에: 12월 중순 세제개정대강, 12월 말 예산안과 국채발행계획(연례 관행 기준, 날짜 미확인).",
        "5) 익절/줄일 때: 이 알림만으로 줄이지 않음. yencarry의 일본 30년 4.25/4.3 또는 달러엔 158/160 알림이 같이 오면 초장기 비중 점검.",
        "6) 하지 말 것: 헤드라인 한 줄로 매매하지 않기. 보도는 원문 확인 전까지 확정 사실 아님.",
    ]
    return title, lines


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    now = datetime.now(KST)
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))

    if mode == "install":
        send(cfg, "[JPFY27] 설치 확인 | 매매 신호 아님", [
            "1) 이게 뭐 알림: 예산안, 국채발행, 식품세 법안 새 보도 변화 감지 알림 설치 확인.",
            "2) 지금 결과/상태: 설치만 됨. 변화가 있을 때만 옴. 고정 문구 정기 발송은 없앴음.",
            "3) 지금 할 일: 없음.",
            "4) 나중에: 평일 09:00 점검에서 새 보도가 있을 때만 알림.",
            "5) 익절/줄일 때: 해당 없음.",
            "6) 하지 말 것: 이 메시지를 매매 신호로 읽지 않기.",
        ])
        print("install sent")
        return 0

    if past_end(cfg, now):
        print("past ends_kst; silent")
        return 0
    if now.weekday() >= 5 and mode == "auto":
        print("weekend; skip")
        return 0

    dry = mode == "dry"
    state = load_state()
    items, ok = collect()
    print(f"items={len(items)} feeds_ok={ok}/{len(QUERIES)} mode={mode}")

    if ok == 0:
        last = state.get("last_fail_notice")
        if dry or last != now.strftime("%Y-%m-%d"):
            title = "[JPFY27] 조회 실패 | 뉴스 피드 전부 실패"
            lines = [
                "1) 이게 뭐 알림: 예산안 보도 확인 실패 알림 (매매 신호 아님).",
                "2) 지금 결과/상태: 뉴스 피드 5개 모두 응답 없음.",
                "3) 지금 할 일: 없음. 다음 평일에 자동 재시도.",
                "4) 나중에: 연속 실패 시 소스 점검.",
                "5) 익절/줄일 때: 해당 없음.",
                "6) 하지 말 것: 실패를 신호로 읽지 않기.",
            ]
            if dry:
                print("DRY RUN would send:", title)
            else:
                send(cfg, title, lines, 2, ["warning"])
                state["last_fail_notice"] = now.strftime("%Y-%m-%d")
                save_state(state)
        return 0

    seen = set(state.get("seen", []))
    fresh = []
    for it in items:
        key = it["link"] or it["title"]
        if key in seen:
            continue
        cls = classify(it["title"])
        recent = it["dt"] is None or now - it["dt"] <= MAX_AGE
        fresh.append((key, cls, it, recent))

    if not state.get("baseline_done") and not dry:
        state["seen"] = list(seen | {k for k, *_ in fresh})
        state["baseline_done"] = True
        save_state(state)
        print(f"baseline recorded: {len(fresh)} items; silent")
        return 0

    hits = [(cls, it) for _, cls, it, recent in fresh if cls and recent]
    print(f"new={len(fresh)} hits={len(hits)}")
    if not hits:
        if not dry:
            state["seen"] = list(seen | {k for k, *_ in fresh})
            save_state(state)
        print("silent")
        return 0

    title, lines = build_hits(hits)
    if dry:
        print("DRY RUN would send:", title)
        print("\n".join(lines))
        return 0
    send(cfg, title, lines)
    state["seen"] = list(seen | {k for k, *_ in fresh})
    state["last_sent"] = now.isoformat()
    save_state(state)
    print("sent")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
