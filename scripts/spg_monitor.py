#!/usr/bin/env python3
"""Simon limit watch. Zone change plus one weekly summary. Not a buy order."""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

KST = timezone(timedelta(hours=9))
CFG_PATH = ROOT / "configs" / "spg.json"
STATE_PATH = ROOT / "state" / "spg.json"
UA = {"User-Agent": "cwally-alerts spg research monitor research@example.com"}


def load_json(path: Path, fallback: dict) -> dict:
    if not path.exists():
        return dict(fallback)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return dict(fallback)
    out = dict(fallback)
    out.update(data)
    return out


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=25) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def price_of(url: str) -> float | None:
    data = json.loads(fetch(url))
    result = (data.get("chart") or {}).get("result") or []
    if not result:
        return None
    raw = (result[0].get("meta") or {}).get("regularMarketPrice")
    try:
        return float(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def as_yield(raw: float | None) -> float | None:
    if raw is None:
        return None
    return raw / 10.0 if raw > 20 else raw


def price_zone(px: float, cfg: dict) -> str:
    if px <= float(cfg["limit_usd"]):
        return "limit"
    if px <= float(cfg["upper_usd"]):
        return "upper"
    return "rich"


def yield_band(y: float, cfg: dict) -> str:
    if y <= float(cfg["reopen_yield"]):
        return "reopen"
    if y >= float(cfg["stress_yield"]):
        return "stress"
    return "base"


def iso_week(now: datetime) -> str:
    year, week, _ = now.isocalendar()
    return f"{year}-W{week:02d}"


def lines_for(stamp: str, status: str, action: str, later: str, trim: str, avoid: str) -> list[str]:
    return [
        f"{stamp} | SPG | 지정가 점검",
        f"이게 뭐 알림: {status.split(' | ', 1)[0] if False else '사이먼 지정가 150달러와 10년물 6% 조건을 보는 알림입니다. 매수 지시가 아닙니다.'}",
        f"지금 결과/상태: {status}",
        f"지금 할 일: {action}",
        f"나중에: {later}",
        f"익절/줄일 때: {trim}",
        f"하지 말 것: {avoid}",
    ]


def action_for(zone: str, band: str, cfg: dict) -> tuple[str, str, str, str]:
    limit = int(cfg["limit_usd"])
    upper = int(cfg["upper_usd"])
    if zone == "limit" and band == "stress":
        title = f"[SPG] {limit}달러 이하, 10년물 6% | 지정가 구간"
        action = f"{limit}달러 이하이고 10년물이 6% 이상입니다. 지정가 구간입니다. 신규 검토만 하세요. 자동 주문은 아닙니다."
    elif zone == "limit":
        title = f"[SPG] {limit}달러 이하 | 지정가 구간"
        action = f"{limit}달러 이하입니다. 지정가 체결 구간입니다. 신규 매수를 검토하세요. 자동 주문은 아닙니다."
    elif band == "stress":
        title = "[SPG] 10년물 6% 이상 | 지정가 150 유지"
        action = f"지정가 {limit}달러를 유지하세요. {upper}달러로 올리지 마세요."
    elif band == "reopen":
        title = "[SPG] 10년물 4.5% 이하 | 단독 논리 재검토"
        action = "사이먼만 고르던 논리가 약해집니다. 벤타스와 에퀴닉스를 다시 비교하세요. 지정가를 올리지 마세요."
    elif zone == "upper":
        title = f"[SPG] {limit}~{upper}달러 | 아직 대기"
        action = f"{limit}달러 지정가를 유지하세요. 동일점포가 확인되기 전에는 {upper}달러로 올리지 마세요."
    else:
        title = f"[SPG] 지정가 위 | {limit}달러 대기"
        action = f"{limit}달러까지 기다리세요. 지금 가격은 6% 국채를 전제로 한 신규 매수가 아닙니다."
    later = (
        f"동일점포 NOI가 두 분기 연속 연 {cfg['upgrade_noi_pct']}% 이상으로 공시에 나오면 지정가 상향을 다시 보세요. "
        "이 알림은 그 숫자를 자동 판정하지 않습니다."
    )
    trim = "이 알림만으로 보유분을 팔지 마세요. 배당이 연 9달러에서 줄거나 2026년 부동산 FFO 가이던스가 내려가면 논리가 약해집니다."
    avoid = "200달러대 추격, 신용, 미수. 국내 NOI 8.5%를 동일점포로 단정."
    return title, action, later, trim if False else trim, avoid


def evaluate(cfg: dict, state: dict, px: float, y10: float, week: str) -> dict:
    zone = price_zone(px, cfg)
    band = yield_band(y10, cfg)
    seeded = state.get("price_zone") is None or state.get("yield_band") is None
    zone_changed = (not seeded) and zone != state.get("price_zone")
    band_changed = (not seeded) and band != state.get("yield_band")
    weekly_due = week not in (state.get("weekly_sent") or {})
    return {
        "zone": zone,
        "band": band,
        "seeded": seeded,
        "zone_changed": zone_changed,
        "band_changed": band_changed,
        "weekly_due": weekly_due,
    }


def status_line(px: float, y10: float, zone: str, band: str, cfg: dict) -> str:
    gap = px - float(cfg["limit_usd"])
    names = {"limit": "지정가 구간", "upper": "150~160 대기", "rich": "신규 매수 아님"}
    bands = {"reopen": "4.5% 이하", "base": "4.5~6% 미만", "stress": "6% 이상"}
    return (
        f"종가 {px:.2f}달러, 지정가까지 {gap:.2f}달러. "
        f"10년물 {y10:.2f}% ({bands[band]}). 가격 구간은 {names[zone]}. "
        f"2026년 부동산 FFO 가이던스 {cfg['ffo_2026']}달러는 고정 기준값입니다."
    )


def build(cfg: dict, stamp: str, px: float, y10: float, res: dict, kind: str) -> tuple[str, list[str]]:
    title, action, later, trim, avoid = action_for(res["zone"], res["band"], cfg)
    if kind == "weekly" and not (res["zone_changed"] or res["band_changed"]):
        title = "[SPG] 주간 점검 | 지정가 150 대기" if res["zone"] != "limit" else "[SPG] 주간 점검 | 지정가 구간"
        action = (
            f"지정가 {int(cfg['limit_usd'])}달러를 유지하세요."
            if res["zone"] != "limit"
            else "지정가 구간입니다. 신규 매수를 검토하세요. 자동 주문은 아닙니다."
        )
    status = status_line(px, y10, res["zone"], res["band"], cfg)
    if kind == "weekly":
        status += f" 6% 재조달 이자를 상쇄하려면 연 {cfg['noi_offset_pct']}% NOI 성장이면 됩니다."
    return title, lines_for(stamp, status, action, later, trim, avoid)


def apply_state(state: dict, res: dict, px: float, y10: float, week: str, weekly: bool, now: datetime) -> None:
    state["updated_at"] = now.isoformat()
    state["price_zone"] = res["zone"]
    state["yield_band"] = res["band"]
    state["last_price"] = round(px, 4)
    state["last_yield"] = round(y10, 4)
    state["fetch_fail_date"] = None
    if weekly:
        sent = dict(state.get("weekly_sent") or {})
        sent[week] = now.date().isoformat()
        state["weekly_sent"] = dict(sorted(sent.items())[-12:])


def send(cfg: dict, title: str, body_lines: list[str], dry: bool, ops: bool = False) -> None:
    text = "\n".join(body_lines)
    if dry:
        print("DRY", title)
        print(text)
        return
    from ntfy_util import publish

    topic = "cwally-ops" if ops else cfg["ntfy_topic"]
    publish(topic, title, text, 4 if ops else 3, ["warning"] if ops else ["chart_with_upwards_trend"], cfg.get("ntfy_server"))


def run(mode: str, now: datetime | None = None, px: float | None = None, y10: float | None = None) -> int:
    dry = mode == "dry" or os.environ.get("SPG_DRY") == "1"
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    state = load_json(STATE_PATH, {
        "updated_at": None,
        "price_zone": None,
        "yield_band": None,
        "weekly_sent": {},
        "fetch_fail_date": None,
        "last_price": None,
        "last_yield": None,
    })
    now = now or datetime.now(KST)
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    ends = datetime.strptime(cfg["ends_kst"], "%Y-%m-%d %H:%M").replace(tzinfo=KST)
    if now > ends and mode not in {"dry", "self-test"}:
        print(f"after {cfg['ends_kst']}; silent")
        return 0
    week = iso_week(now)
    err = None
    if px is None or y10 is None:
        try:
            px = price_of(cfg["sources"]["spg"])
            y10 = as_yield(price_of(cfg["sources"]["tnx"]))
        except Exception as exc:
            err = str(exc)
            px = y10 = None
    print(f"spg={px} y10={y10} err={err}")
    if px is None or y10 is None:
        today = now.date().isoformat()
        if state.get("fetch_fail_date") != today:
            send(
                cfg,
                "[SPG] 시세 조회 실패 | 조건 미확인",
                lines_for(
                    stamp,
                    f"사이먼 종가 또는 10년물을 못 읽었습니다. {err or '빈 응답'}. 조건 미충족이 아닙니다.",
                    "증권 앱에서 사이먼 종가와 미국 10년물 수익률을 직접 보세요.",
                    "다음 평일 09:00 KST에 재시도합니다.",
                    "이번 알림으로 비중을 바꾸지 마세요.",
                    "조회 실패를 매수 신호로 취급.",
                ),
                dry,
                ops=True,
            )
            state["fetch_fail_date"] = today
            if not dry:
                save_state(state)
        return 0
    res = evaluate(cfg, state, px, y10, week)
    if res["seeded"]:
        print(f"seed zone={res['zone']} band={res['band']}; no first alert")
        apply_state(state, res, px, y10, week, weekly=False, now=now)
        if not dry:
            save_state(state)
        return 0
    if not res["zone_changed"] and not res["band_changed"] and not res["weekly_due"]:
        print("unchanged; silent")
        apply_state(state, res, px, y10, week, weekly=False, now=now)
        if not dry:
            save_state(state)
        return 0
    kind = "event" if res["zone_changed"] or res["band_changed"] else "weekly"
    title, body_lines = build(cfg, stamp, px, y10, res, kind)
    send(cfg, title, body_lines, dry, ops=False)
    apply_state(state, res, px, y10, week, weekly=res["weekly_due"] or kind == "weekly", now=now)
    if not dry:
        save_state(state)
    return 0


def self_test() -> int:
    cfg = {
        "limit_usd": 150,
        "upper_usd": 160,
        "reopen_yield": 4.5,
        "stress_yield": 6.0,
        "ffo_2026": 13.25,
        "noi_offset_pct": 2.5,
        "upgrade_noi_pct": 4.5,
        "dividend_usd": 9.0,
    }
    state = {"price_zone": "rich", "yield_band": "base", "weekly_sent": {"2026-W41": "2026-10-07"}}
    quiet = evaluate(cfg, state, 201.74, 5.27, "2026-W41")
    assert not quiet["zone_changed"] and not quiet["band_changed"] and not quiet["weekly_due"]
    hit = evaluate(cfg, state, 149.5, 5.27, "2026-W41")
    assert hit["zone_changed"] and hit["zone"] == "limit"
    stress = evaluate(cfg, state, 201.74, 6.05, "2026-W41")
    assert stress["band_changed"] and stress["band"] == "stress"
    week = evaluate(cfg, state, 201.74, 5.27, "2026-W42")
    assert week["weekly_due"] and not week["zone_changed"]
    upper = evaluate(cfg, state, 155, 5.27, "2026-W41")
    assert upper["zone"] == "upper" and upper["zone_changed"]
    seed = evaluate(cfg, {"price_zone": None, "yield_band": None, "weekly_sent": {}}, 201.74, 5.27, "2026-W41")
    assert seed["seeded"]
    title, body = build(cfg, "2026-10-08 09:00 KST", 201.74, 5.27, week, "weekly")
    text = "\n".join(body)
    for label in ("이게 뭐 알림", "지금 결과/상태", "지금 할 일", "나중에", "익절/줄일 때", "하지 말 것"):
        assert label in text, label
    assert "꺼지는 때" not in text
    assert title.startswith("[SPG]")
    assert abs(as_yield(52.7) - 5.27) < 1e-9
    assert as_yield(5.27) == 5.27
    print("self-test ok")
    return 0


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    if mode == "self-test":
        return self_test()
    return run(mode)


if __name__ == "__main__":
    raise SystemExit(main())
