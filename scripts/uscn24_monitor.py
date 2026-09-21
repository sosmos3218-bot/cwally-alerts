#!/usr/bin/env python3
from __future__ import annotations

import http.client
import json
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from html import unescape
from pathlib import Path
from urllib.parse import urljoin

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
CFG_PATH = ROOT / "configs" / "uscn24.json"
STATE_PATH = ROOT / "state" / "uscn24.json"
UA = {"User-Agent": "Mozilla/5.0 cwally-alerts"}

LABEL = {
    "ai_dialogue": "AI대화",
    "trade_truce": "무역휴전",
    "rare_earth": "희토류",
    "chip_controls": "칩수출통제",
}
SUMMIT_RE = re.compile(
    r"xi|jinping|state visit|summit|china|chinese president|习近平|中美|特朗普",
    re.I,
)
DATE_RE = re.compile(r"2026-09-2[4-7]|September 2[4-7],\s*2026|Sept\.?\s*2[4-7],?\s*2026|2026年9月2[4-7]日")

AGREE = {
    "ai_dialogue": re.compile(
        r"(agreed to |will )?(launch|establish|start|create).{0,80}(ai|artificial intelligence).{0,40}(dialogue|mechanism|working group|notification)|"
        r"(ai|artificial intelligence).{0,40}(dialogue|mechanism).{0,40}(agreed|launched|established)|"
        r"启动.{0,20}(人工智能|AI).{0,20}(对话|机制)|同意.{0,20}(人工智能|AI).{0,20}(对话|机制)",
        re.I,
    ),
    "trade_truce": re.compile(
        r"(agreed to |will )?(extend|maintain|renew).{0,60}(tariff|truce|suspension|pause)|"
        r"(tariff|truce).{0,40}(extended|maintained).{0,20}(november|11)|"
        r"延长.{0,20}(关税|休战|暂停)|同意.{0,20}(关税|暂停)",
        re.I,
    ),
    "rare_earth": re.compile(
        r"(agreed|will (ensure|resume|issue|maintain)).{0,80}(rare[- ]earth|critical mineral|magnet)|"
        r"(rare[- ]earth|critical mineral).{0,50}(license|quota|supply|export).{0,20}(agree|ensure|resume)|"
        r"(同意|达成).{0,20}(稀土|关键矿产)|保障.{0,10}(稀土|关键矿产)",
        re.I,
    ),
    "chip_controls": re.compile(
        r"(agreed|will).{0,80}(ease|relax|lift|allow|license).{0,40}(export control|chip|semiconductor|h200)|"
        r"(export control|chip|h200).{0,40}(eased|relaxed|allowed|licensed)|"
        r"(同意|放宽|允许).{0,20}(出口管制|芯片|半导体|H200)",
        re.I,
    ),
}
HINT = {
    "ai_dialogue": re.compile(r"\bai\b|artificial intelligence|인공지능|人工智能", re.I),
    "trade_truce": re.compile(r"tariff|truce|trade pause|关税|休战|贸易", re.I),
    "rare_earth": re.compile(r"rare[- ]earth|critical mineral|magnet|稀土|关键矿产", re.I),
    "chip_controls": re.compile(
        r"export control|semiconductor|\bchips?\b|h200|nvidia|出口管制|芯片|半导体",
        re.I,
    ),
}


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            raw = resp.read()
    except http.client.IncompleteRead as exc:
        raw = exc.partial or b""
        if not raw:
            raise
    return raw.decode("utf-8", errors="ignore")


def strip_html(html: str) -> str:
    text = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", html)
    text = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = unescape(text)
    return re.sub(r"\s+", " ", text)


def links_from(html: str, base: str) -> list[str]:
    out: list[str] = []
    for href in re.findall(r'href=["\']([^"\']+)["\']', html, flags=re.I):
        if href.startswith("#") or href.startswith("mailto:"):
            continue
        abs_url = urljoin(base, href)
        if abs_url not in out:
            out.append(abs_url)
    return out


def summit_doc(url: str, html: str) -> bool:
    blob = url + " " + strip_html(html)[:4000]
    return bool(SUMMIT_RE.search(blob) and DATE_RE.search(blob))


def grade_text(text: str) -> dict[str, str]:
    grades = {}
    for key in LABEL:
        if AGREE[key].search(text):
            grades[key] = "합의"
        elif HINT[key].search(text):
            grades[key] = "시사"
        else:
            grades[key] = "미언급"
    return grades


def merge_grades(parts: list[dict[str, str]]) -> dict[str, str]:
    rank = {"합의": 2, "시사": 1, "미언급": 0}
    out = {k: "미언급" for k in LABEL}
    for part in parts:
        for key, val in part.items():
            if rank.get(val, 0) > rank.get(out[key], 0):
                out[key] = val
    return out


def load_state() -> dict:
    state = {
        "final_notified": False,
        "fail_notified": False,
        "empty_notified": False,
        "last_key": None,
        "updated_at": None,
        "wh_ok": None,
        "xh_ok": None,
        "docs": [],
        "grades": {},
    }
    if STATE_PATH.exists():
        try:
            state.update(json.loads(STATE_PATH.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass
    return state


def save_state(state: dict, now: datetime) -> None:
    state["updated_at"] = now.isoformat()
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def collect(cfg: dict) -> tuple[list[dict], bool, bool]:
    docs: list[dict] = []
    wh_ok = False
    xh_ok = False
    seen: set[str] = set()
    for kind in ("whitehouse", "xinhua"):
        ok = False
        for list_url in cfg["sources"][kind]:
            try:
                listing = fetch(list_url)
                ok = True
            except Exception as exc:
                print(f"list fail {list_url}: {exc}")
                continue
            cands = [list_url]
            for href in links_from(listing, list_url):
                if kind == "whitehouse" and "whitehouse.gov" not in href:
                    continue
                if kind == "xinhua" and "news.cn" not in href and "xinhuanet.com" not in href:
                    continue
                if SUMMIT_RE.search(href) or re.search(r"2026/09/2[4-7]|2026092[4-7]", href):
                    cands.append(href)
            for href in cands[:18]:
                if href in seen:
                    continue
                seen.add(href)
                try:
                    html = listing if href == list_url else fetch(href)
                except Exception as exc:
                    print(f"doc fail {href}: {exc}")
                    continue
                if not summit_doc(href, html):
                    continue
                text = strip_html(html)
                docs.append({"kind": kind, "url": href, "text": text[:12000]})
                print(f"doc {kind} {href}")
        if kind == "whitehouse":
            wh_ok = ok
        else:
            xh_ok = ok
    return docs, wh_ok, xh_ok


def table_line(grades: dict[str, str]) -> str:
    return " / ".join(f"{LABEL[k]} {grades[k]}" for k in LABEL)


def kr_lines(cfg: dict, grades: dict[str, str], texts: str) -> list[str]:
    out = []
    for key in cfg.get("kr_direct") or []:
        g = grades.get(key, "미언급")
        if g == "미언급":
            continue
        if key == "rare_earth":
            out.append(f"희토류 {g}: 자석, 전기차, 전자 중간재 공급 일정 확인.")
        elif key == "chip_controls":
            out.append(f"칩수출통제 {g}: 삼성, 하이닉스, 장비 수출입 문장만 공식문 기준으로 보세요.")
        elif key == "trade_truce":
            out.append(f"무역휴전 {g}: 11월 10일 전 관세 재점화 여부만 후속.")
    if HINT["chip_controls"].search(texts) and grades.get("ai_dialogue") != "미언급":
        out.append(f"AI대화 {grades['ai_dialogue']}: 칩, 연산 문장이 있어 공급망에 간접 연결.")
    if not out:
        out.append("한국 공급망에 직접 닿는 공식 문장 없음.")
    return out


def action_title(grades: dict[str, str], kr: list[str], kind: str) -> str:
    if kind == "fail":
        return "[USCN24] 조회 실패 | 재시도"
    if kind == "empty":
        return "[USCN24] 공식문 없음 | 대기"
    bits = []
    for key in ("rare_earth", "chip_controls", "trade_truce"):
        if grades.get(key) in {"합의", "시사"}:
            bits.append(f"{LABEL[key]} {grades[key]}")
    if not bits:
        return "[USCN24] 공급망 항목 미언급 | 대기"
    return "[USCN24] " + " | ".join(bits[:2]) + ("" if any(grades.get(k) == "합의" for k in ("rare_earth", "chip_controls", "trade_truce")) else " | 추격 금지")


def emit(cfg: dict, title: str, lines: list[str], priority: int) -> None:
    topic = resolve_topic("port", cfg.get("ntfy_topic"))
    server = resolve_server(cfg.get("ntfy_server"))
    publish(topic, title, "\n".join(lines), priority, ["cn", "us"], server)


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    state = load_state()
    start = datetime.fromisoformat(cfg["window_start_kst"]).replace(tzinfo=KST)
    expire = datetime.fromisoformat(cfg["expire_date"] + "T23:59:59").replace(tzinfo=KST)
    in_window = start <= now <= expire and now.year == int(cfg.get("event_year", 2026))
    print(f"{now.isoformat()} mode={mode} window={in_window}")

    if mode not in {"force", "dry"} and not in_window:
        print("outside 2026 window; silent")
        save_state(state, now)
        return 0
    if state.get("final_notified") and mode != "force":
        print("already sent final; skip")
        return 0

    docs, wh_ok, xh_ok = collect(cfg)
    state["wh_ok"] = wh_ok
    state["xh_ok"] = xh_ok
    state["docs"] = [{"kind": d["kind"], "url": d["url"]} for d in docs]

    if not wh_ok and not xh_ok:
        print("both sources failed")
        if mode != "dry" and not state.get("fail_notified"):
            emit(
                cfg,
                action_title({}, [], "fail"),
                [
                    f"{now.strftime('%Y-%m-%d %H:%M KST')} | USCN24 | 미중 정상회담",
                    f"이게 뭐 알림: {cfg['explain']}",
                    "지금 상태: 백악관, 신화사 목록 조회 실패. 미언급으로 처리하지 않음.",
                    "지금 할 일: 다음 기존 슬롯까지 기다림. 지금은 추측으로 포지션 바꾸지 마세요.",
                    "나중에: 공식문이 생기면 표로 한 번만 보냅니다.",
                    "익절/줄일 때: 이번 실패 알림으로는 매매하지 마세요.",
                    "하지 말 것: 통신사 속보를 공식 합의로 단정.",
                ],
                4,
            )
            state["fail_notified"] = True
        save_state(state, now)
        return 0

    grades = merge_grades([grade_text(d["text"]) for d in docs]) if docs else {k: "미언급" for k in LABEL}
    blob = " ".join(d["text"] for d in docs)
    kr = kr_lines(cfg, grades, blob) if docs else ["공식 정상회담 발표문을 아직 못 찾음."]
    state["grades"] = grades
    key = "|".join(f"{k}:{grades[k]}" for k in LABEL) + "|" + str(bool(docs))

    last_day = now.date() == expire.date()
    send = False
    if docs and (mode in {"force", "dry"} or not state.get("final_notified")):
        send = True
    elif not docs and last_day and not state.get("empty_notified"):
        send = True
    elif not docs and mode == "force":
        send = True

    if mode == "dry":
        print(table_line(grades))
        print(kr)
        print(f"wh_ok={wh_ok} xh_ok={xh_ok} docs={len(docs)} send_would={send}")
        print("dry-run; no ntfy")
        save_state(state, now)
        return 0

    if not send:
        print("condition not met or waiting for official text; silent")
        save_state(state, now)
        return 0
    if state.get("last_key") == key and state.get("final_notified"):
        print("same key; silent")
        save_state(state, now)
        return 0

    srcs = [d["url"] for d in docs[:4]] if docs else ["백악관/신화사 목록은 열렸으나 24~27일 정상회담 공식문 없음"]
    title = action_title(grades, kr, "final" if docs else "empty")
    lines = [
        f"{now.strftime('%Y-%m-%d %H:%M KST')} | USCN24 | 미중 정상회담",
        f"이게 뭐 알림: {cfg['explain']}",
        f"지금 상태: {table_line(grades) if docs else '공식문 없음. 네 항목을 미언급으로 확정하지 않음.'}",
        "한국 공급망: " + " ".join(kr),
        "지금 할 일: " + (kr[0] if docs else "공식문 나올 때까지 대기."),
        "나중에: 같은 판정은 다시 안 보냅니다. 만료 2026-09-27.",
        "익절/줄일 때: 희토류, 칩이 합의여도 추격 금지. 문장에 없는 완화는 없다고 봅니다.",
        "하지 말 것: 로이터/SCMP를 공식 합의로 대체. 조회 실패를 미언급으로 처리.",
        "출처: " + " | ".join(srcs),
        f"조회: 백악관 {'OK' if wh_ok else '실패'} / 신화사 {'OK' if xh_ok else '실패'}",
    ]
    emit(cfg, title, lines, 5 if docs and any(v == "합의" for v in grades.values()) else 4)
    if docs:
        state["final_notified"] = True
    else:
        state["empty_notified"] = True
    state["last_key"] = key
    save_state(state, now)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
