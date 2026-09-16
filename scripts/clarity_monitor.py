#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parents[1]
STATE_PATH = ROOT / "state" / "clarity.json"
CFG_PATH = ROOT / "configs" / "clarity.json"

SOURCES = [
    "https://www.congress.gov/bill/119th-congress/house-bill/3633/all-actions",
    "https://www.congress.gov/search?q=%7B%22source%22:%22legislation%22,%22search%22:%22Clarity%20Act%20cloture%22%7D",
    "https://www.theblock.co/latest",
    "https://www.reuters.com/world/us/",
    "https://apnews.com/hub/congress",
]
PASS_RE = re.compile(
    r"cloture (was )?invoked|invoked cloture|cleared (the )?60|advanced to debate|"
    r"motion to proceed.{0,40}agreed|vote succeeded|passed cloture",
    re.I,
)
FAIL_RE = re.compile(
    r"cloture (vote )?fail|failed to invoke|fell short|rejected cloture|"
    r"did not (get|reach) 60|motion to proceed.{0,40}rejected",
    re.I,
)
UA = {"User-Agent": "Mozilla/5.0 cwally-alerts"}
KO = {"pass": "가결", "fail": "부결", "pending": "아직 결과 없음"}


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=25) as resp:
        raw = resp.read()
    return raw.decode("utf-8", errors="ignore")


def classify(text: str) -> tuple[str, str]:
    window = re.sub(r"<[^>]+>", " ", text)
    window = re.sub(r"\s+", " ", window)
    hits = []
    for m in re.finditer(r".{0,80}(clarity|cloture|h\.?r\.?\s*3633).{0,80}", window, re.I):
        hits.append(m.group(0))
    blob = " ".join(hits) or window[:4000]
    if PASS_RE.search(blob) and not FAIL_RE.search(blob):
        return "pass", blob[:280]
    if FAIL_RE.search(blob) and not PASS_RE.search(blob):
        return "fail", blob[:280]
    if PASS_RE.search(blob) and FAIL_RE.search(blob):
        return "pending", blob[:280]
    return "pending", (hits[0][:280] if hits else "")


def playbook(cfg: dict, result: str) -> list[str]:
    coin = cfg.get("coin", {})
    rule = (coin.get("rules") or {}).get(result) or {}
    cap = coin.get("target_pct_of_assets", 5)
    explain = cfg.get("explain") or "COIN 매매 가이드 알림입니다."
    return [
        f"이게 뭔 알림: {explain}",
        f"지금 결과: {rule.get('result_plain') or KO.get(result, result)}",
        "",
        f"지금 할 일: {rule.get('do_now')}",
        f"나중에: {rule.get('later')}",
        f"익절: {rule.get('sell')}",
        f"줄일 때: {rule.get('cut')}",
        f"하지 말 것: {rule.get('dont')}",
        "",
        f"종목 COIN | 총자산 한도 {cap}% | 1차 {rule.get('now_pct_of_target')}% / 2차 ${rule.get('t2_price')}({rule.get('t2_pct_of_target')}%) / 3차 ${rule.get('t3_price')}({rule.get('t3_pct_of_target')}%)",
    ]


def ntfy(server: str, topic: str, title: str, message: str, priority: int) -> None:
    payload = json.dumps(
        {
            "topic": topic,
            "title": title,
            "message": message,
            "priority": priority,
            "tags": ["classical_building", "chart_with_upwards_trend"],
        }
    ).encode()
    req = urllib.request.Request(
        server.rstrip("/"),
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        print(resp.read().decode())


def main() -> int:
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    topic = os.environ.get("NTFY_TOPIC") or cfg.get("ntfy_topic") or "cwally-port"
    server = os.environ.get("NTFY_SERVER") or cfg.get("ntfy_server") or "https://ntfy.sh"
    state = {
        "result": "pending",
        "pending_notified": False,
        "final_notified": False,
        "updated_at": None,
        "source": None,
        "snippet": None,
    }
    if STATE_PATH.exists():
        try:
            state.update(json.loads(STATE_PATH.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass
    if state.get("final_notified") and state.get("result") in {"pass", "fail"}:
        print("already sent final; skip")
        return 0

    result, snippet, source = "pending", "", ""
    for url in SOURCES:
        try:
            text = fetch(url)
        except Exception as exc:
            print(f"fetch fail {url}: {exc}")
            continue
        guess, snip = classify(text)
        print(f"{url} -> {guess}")
        if guess in {"pass", "fail"}:
            result, snippet, source = guess, snip, url
            break
        if snip and not snippet:
            snippet, source = snip, url

    ko = KO[result]
    lines = [
        f"{now.strftime('%Y-%m-%d %H:%M KST')} | CLARITY | COIN",
        f"결과: {ko}",
        "",
        *playbook(cfg, result),
    ]
    if source:
        lines.append("")
        lines.append(f"출처: {source}")
    msg = "\n".join(lines)
    print(msg)

    send = False
    if result in {"pass", "fail"} and not state.get("final_notified"):
        send = True
        state["final_notified"] = True
        state["pending_notified"] = True
    elif result == "pending" and not state.get("pending_notified"):
        send = True
        state["pending_notified"] = True
    if send:
        rule = ((cfg.get("coin") or {}).get("rules") or {}).get(result) or {}
        title = f"[CLARITY] {rule.get('title') or ko}"
        ntfy(server, topic, title, msg, 5 if result in {"pass", "fail"} else 3)
    else:
        print("no ntfy; state unchanged for notify")

    state.update(
        {
            "result": result,
            "updated_at": now.isoformat(),
            "source": source or state.get("source"),
            "snippet": snippet or state.get("snippet"),
        }
    )
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
