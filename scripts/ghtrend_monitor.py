#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
CFG_PATH = ROOT / "configs" / "ghtrend.json"
STATE_PATH = ROOT / "state" / "ghtrend.json"
UA = {"User-Agent": "Mozilla/5.0 cwally-alerts"}
TRENDING_URL = "https://github.com/trending"


def load_json(path: Path, fallback: dict) -> dict:
    if not path.exists():
        return dict(fallback)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        fallback.update(data)
        return fallback
    except json.JSONDecodeError:
        return dict(fallback)


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def fetch_trending(top_n: int) -> list[dict]:
    req = urllib.request.Request(TRENDING_URL, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as resp:
        html = resp.read().decode("utf-8", errors="replace")
    # Extract owner/repo from article or Box-row links
    pattern = re.compile(r'href="/([^/]+/[^/"]+)"[^>]*class="[^"]*text-bold[^"]*"')
    matches = pattern.findall(html)
    seen = []
    for m in matches:
        if m.count("/") == 1 and m not in seen:
            seen.append(m)
        if len(seen) >= top_n:
            break
    if len(seen) < top_n:
        # fallback: any /owner/repo in trending context
        alt = re.findall(r'href="/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)"', html)
        for m in alt:
            if m not in seen and not m.startswith("topics/") and "/" in m:
                seen.append(m)
            if len(seen) >= top_n:
                break
    return [{"full_name": name, "url": f"https://github.com/{name}"} for name in seen[:top_n]]


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    week = now.strftime("%G-W%V")
    state = load_json(STATE_PATH, {"last_week": None, "last_repos": []})
    top_n = int(cfg.get("top_n") or 10)

    sunday = now.weekday() == 6
    should = mode in {"weekly", "force"} or (mode == "auto" and sunday)
    if mode == "dry":
        print("DRY RUN: would fetch trending and print title, no ntfy, no state")
        return 0
    if not should:
        print("ghtrend silent; wait sunday 21:00 KST")
        state["updated_at"] = now.isoformat()
        state["last_skip_reason"] = "not_sunday"
        save_state(state)
        return 0
    if mode != "force" and state.get("last_week") == week:
        print("ghtrend weekly already sent")
        state["updated_at"] = now.isoformat()
        state["last_skip_reason"] = "dup_week"
        save_state(state)
        return 0

    try:
        repos = fetch_trending(top_n)
    except Exception as exc:
        print(f"trending fetch fail: {exc}", file=sys.stderr)
        publish(
            resolve_topic("port", cfg.get("ntfy_topic")),
            "[GHTREND] 조회 실패 | 재시도",
            "\n".join([
                f"{now.strftime('%Y-%m-%d %H:%M KST')} | GHTREND",
                "이게 뭐 알림: GitHub Trending 주간 상위.",
                f"지금 결과: 조회 실패 ({exc}).",
                "지금 할 일: 다음 주 일요일 재시도.",
                "나중에: 페이지 구조 변경 확인.",
                "익절/줄일 때: 없음.",
                "하지 말 것: 실패를 신호로 보지 말 것.",
            ]),
            4,
            ["warning"],
            resolve_server(cfg.get("ntfy_server")),
        )
        state.update({"updated_at": now.isoformat(), "last_week": week, "last_skip_reason": "fetch_fail"})
        save_state(state)
        return 1

    if not repos:
        print("no repos parsed")
        return 1

    prev = set(state.get("last_repos") or [])
    lines = []
    for i, r in enumerate(repos, 1):
        name = r["full_name"]
        mark = " NEW" if name not in prev else ""
        lines.append(f"{i}. {name}{mark}")

    title = f"[GHTREND] 상위 {len(repos)}개 | 신규 {sum(1 for r in repos if r['full_name'] not in prev)}개"
    body = "\n".join([
        f"{now.strftime('%Y-%m-%d %H:%M KST')} | GHTREND 주간",
        "이게 뭐 알림: GitHub Trending 전체 상위 10개. 투자권유 아님.",
        "지금 결과:\n" + "\n".join(lines),
        "지금 할 일: 관심 있는 저장소만 확인. NEW는 지난주 없던 것.",
        "나중에: 다음 주 동일 시각 비교.",
        "익절/줄일 때: 없음.",
        "하지 말 것: 트렌딩만 보고 즉시 보안 파업 금지.",
    ])
    publish(
        resolve_topic("port", cfg.get("ntfy_topic")),
        title,
        body,
        3,
        ["chart_with_upwards_trend"],
        resolve_server(cfg.get("ntfy_server")),
    )
    state.update({
        "updated_at": now.strftime("%Y-%m-%d %H:%M KST"),
        "last_week": week,
        "last_repos": [r["full_name"] for r in repos],
        "last_skip_reason": None,
    })
    save_state(state)
    print(title)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
