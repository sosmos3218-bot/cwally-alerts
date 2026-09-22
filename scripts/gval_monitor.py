#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
CFG_PATH = ROOT / "configs" / "gval.json"
STATE_PATH = ROOT / "state" / "gval.json"
UA = {"User-Agent": "Mozilla/5.0 cwally-alerts"}


def fetch_text(url: str) -> str:
    import urllib.request

    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=25) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def load_state() -> dict:
    state = {
        "weekly_sent_week": None,
        "fail_sent_week": None,
        "last_pick": None,
        "updated_at": None,
    }
    if STATE_PATH.exists():
        try:
            state.update(json.loads(STATE_PATH.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass
    return state


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def money(n: float | None) -> str:
    if n is None:
        return "확인불가"
    return f"${n:g}"


def blended(row: dict | None) -> float | None:
    if not row or row.get("in") is None or row.get("out") is None:
        return None
    return (float(row["in"]) * 3 + float(row["out"])) / 4.0


def parse_openai(text: str) -> dict:
    out: dict[str, dict] = {}
    for key, pat in (
        ("astra", r"gpt-6-astra"),
        ("sol", r"gpt-5\.6-sol"),
    ):
        row = re.search(pat + r"[^\n]*", text, re.I)
        if not row:
            continue
        nums = [float(x) for x in re.findall(r"\$([0-9]+(?:\.[0-9]+)?)", row.group(0))]
        if len(nums) >= 4:
            out[key] = {"in": nums[0], "cache": nums[1], "out": nums[3], "src": "live"}
        elif len(nums) >= 2:
            out[key] = {"in": nums[0], "cache": None, "out": nums[1], "src": "live"}
    return out


def parse_xai(text: str) -> dict:
    m = re.search(
        r"grok-4\.7\s*\(<\s*200k[^|]{0,40}\|[^\n]*?\$([0-9]+(?:\.[0-9]+)?)\s*\|\s*\$([0-9]+(?:\.[0-9]+)?)\s*\|\s*\$([0-9]+(?:\.[0-9]+)?)",
        text,
        re.I,
    )
    if not m:
        m = re.search(
            r"grok-4\.7[^\n]{0,80}\$([0-9]+(?:\.[0-9]+)?)[^\n]{0,40}\$([0-9]+(?:\.[0-9]+)?)[^\n]{0,40}\$([0-9]+(?:\.[0-9]+)?)",
            text,
            re.I,
        )
    if not m:
        return {}
    return {"grok47": {"in": float(m.group(1)), "cache": float(m.group(2)), "out": float(m.group(3)), "src": "live"}}


def merge_price(live: dict | None, base: dict) -> dict:
    row = dict(base)
    row["src"] = "baseline"
    if live and live.get("in") is not None and live.get("out") is not None:
        row["in"] = float(live["in"])
        row["out"] = float(live["out"])
        if live.get("cache") is not None:
            row["cache"] = float(live["cache"])
        row["src"] = "live"
    return row


def decide(astra: dict, sol: dict, grok: dict) -> dict:
    cand = [
        ("Grok 4.7 medium", grok, "api_daily"),
        ("GPT-5.6 Sol medium", sol, "api_daily"),
    ]
    ranked = [(name, row) for name, row, _ in cand if blended(row) is not None]
    ranked.sort(key=lambda x: blended(x[1]) or 1e9)
    daily_name, daily_row = ranked[0] if ranked else ("확인불가", {})
    hard_name = "GPT-6 Astra medium"
    chat_name = "Plus Work/Codex Astra medium"
    return {
        "pick_id": f"{daily_name}|{hard_name}|{chat_name}",
        "daily": daily_name,
        "daily_row": daily_row,
        "hard": hard_name,
        "chat": chat_name,
        "daily_blend": blended(daily_row),
        "astra_blend": blended(astra),
        "sol_blend": blended(sol),
        "grok_blend": blended(grok),
    }


def copy_for(kind: str, cfg: dict, dec: dict, astra: dict, sol: dict, grok: dict, notes: list[str]) -> tuple[str, list[str], int]:
    if kind == "FETCH_FAIL":
        return (
            "[GVAL] 가격 조회 실패 | 세팅 유지",
            [
                f"이게 뭐 알림: {cfg['explain']}",
                "지금 결과: 공식 가격 페이지를 못 읽었습니다. 실패를 정상으로 보지 마세요.",
                "지금 할 일: 이번 주 세팅을 바꾸지 마세요. OpenAI/xAI 가격 페이지를 직접 확인하세요.",
                "나중에: 다음 월요일 09:00 KST에 다시 읽습니다.",
                "익절/줄일 때: 해당 없음.",
                "하지 말 것: 조회 실패를 가격 인하로 보기. Astra xhigh/max를 기본값으로 올리기.",
            ],
            4,
        )
    changed = "변경" if dec.get("changed") else "유지"
    title = f"[GVAL] 일상 {dec['daily']} | {changed}"
    body = [
        f"이게 뭐 알림: {cfg['explain']}",
        (
            "지금 결과: "
            f"일상 {dec['daily']} (혼합 {money(dec['daily_blend'])}/1M) | "
            f"난제 {dec['hard']} (혼합 {money(dec['astra_blend'])}/1M) | "
            f"구독 {dec['chat']}."
        ),
        (
            "단가: "
            f"Astra {money(astra.get('in'))}/{money(astra.get('out'))} ({astra.get('src')}) | "
            f"Sol {money(sol.get('in'))}/{money(sol.get('out'))} ({sol.get('src')}) | "
            f"Grok4.7 {money(grok.get('in'))}/{money(grok.get('out'))} ({grok.get('src')}). "
            "혼합은 입력3:출력1."
        ),
        "지금 할 일: 일반 코딩/조사는 일상 세팅. 컴퓨터 사용·장시간 에이전트만 Astra medium. ChatGPT 채팅창 최강이 필요하면 Pro이지 Plus medium이 아닙니다.",
        "나중에: 가격이 바뀌거나 Sol 프로모(2026-11-21)가 끝나면 다음 주 알림이 바뀝니다. 월요일 09:00만 옵니다.",
        "익절/줄일 때: 해당 없음. 구독 업그레이드는 한도가 반복해서 모자랄 때만.",
        "하지 말 것: Astra xhigh/max를 기본값으로 두기. 입력 27.2만 토큰 넘긴 채 Astra 상시 사용. 조회 실패를 무시.",
    ]
    if notes:
        body.append("메모: " + " / ".join(notes))
    return title, body, 3


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    dry = mode in {"test", "dry", "dry-run"}
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    week = now.strftime("%G-W%V")
    state = load_state()
    notes: list[str] = []

    if mode not in {"force", "test", "dry", "dry-run"}:
        if now.weekday() != int(cfg.get("weekday_kst", 0)):
            print("gval silent; wait monday 09:00 KST")
            return 0
        if state.get("weekly_sent_week") == week:
            print("gval weekly already sent")
            return 0

    astra = merge_price(None, cfg["baseline"]["astra"])
    sol = merge_price(None, cfg["baseline"]["sol"])
    grok = merge_price(None, cfg["baseline"]["grok47"])
    kind = "OK"
    live_ok = 0
    try:
        oai = parse_openai(fetch_text(cfg["sources"]["openai_pricing"]))
        if "astra" in oai:
            astra = merge_price(oai["astra"], cfg["baseline"]["astra"])
            live_ok += 1
        else:
            notes.append("OpenAI Astra 파싱 실패, 기준가 사용")
        if "sol" in oai:
            sol = merge_price(oai["sol"], cfg["baseline"]["sol"])
            live_ok += 1
        else:
            notes.append("OpenAI Sol 파싱 실패, 기준가 사용")
    except Exception as exc:
        notes.append(f"OpenAI 조회 실패: {exc}")
    try:
        xai = parse_xai(fetch_text(cfg["sources"]["xai_pricing"]))
        if "grok47" in xai:
            grok = merge_price(xai["grok47"], cfg["baseline"]["grok47"])
            live_ok += 1
        else:
            notes.append("xAI Grok 파싱 실패, 기준가 사용")
    except Exception as exc:
        notes.append(f"xAI 조회 실패: {exc}")

    if live_ok == 0:
        kind = "FETCH_FAIL"

    dec = decide(astra, sol, grok)
    dec["changed"] = bool(state.get("last_pick") and state.get("last_pick") != dec["pick_id"])
    title, body, pri = copy_for(kind, cfg, dec, astra, sol, grok, notes)
    lines = [f"{now.strftime('%Y-%m-%d %H:%M KST')} | GVAL | 주간 가성비 세팅", *body]
    msg = "\n".join(lines)
    print(msg)
    print(f"kind={kind} pick={dec.get('pick_id')} dry={dry} live_ok={live_ok}")

    send = True
    if kind == "FETCH_FAIL" and state.get("fail_sent_week") == week and mode != "force":
        send = False
        print("gval fail already sent this week")

    if dry:
        print("dry-run; no ntfy")
    elif send:
        publish(
            resolve_topic("port", cfg.get("ntfy_topic")),
            title,
            msg,
            pri,
            ["chart_with_upwards_trend"],
            resolve_server(cfg.get("ntfy_server")),
        )
        if kind == "FETCH_FAIL":
            state["fail_sent_week"] = week
        else:
            state["weekly_sent_week"] = week
            state["fail_sent_week"] = None
            state["last_pick"] = dec["pick_id"]

    state["updated_at"] = now.isoformat()
    state["last_snapshot"] = {
        "kind": kind,
        "pick": dec.get("pick_id"),
        "astra": astra,
        "sol": sol,
        "grok47": grok,
        "live_ok": live_ok,
    }
    if not dry:
        save_state(state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
