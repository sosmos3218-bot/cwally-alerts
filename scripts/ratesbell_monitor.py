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
CFG_PATH = ROOT / "configs" / "ratesbell.json"
STATE_PATH = ROOT / "state" / "ratesbell.json"
UA = {"User-Agent": "Mozilla/5.0 cwally-alerts ratesbell"}
MONTHS = {
    "January": 1, "February": 2, "March": 3, "April": 4, "May": 5, "June": 6,
    "July": 7, "August": 8, "September": 9, "October": 10, "November": 11, "December": 12,
}


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


def save_state(state: dict, now: datetime) -> None:
    state["updated_at"] = now.isoformat()
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=25) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def yahoo_tnx() -> tuple[float | None, str | None]:
    url = "https://query1.finance.yahoo.com/v8/finance/chart/%5ETNX?interval=1d&range=10d"
    raw = json.loads(fetch(url))
    result = raw["chart"]["result"][0]
    meta = result.get("meta", {})
    price = meta.get("regularMarketPrice")
    ts = meta.get("regularMarketTime")
    if price is None:
        return None, None
    price = float(price)
    if price > 20:  # ^TNX quoted x10 on some feeds (retired cash30 used the same guard)
        price /= 10
    day = datetime.fromtimestamp(int(ts), timezone.utc).strftime("%Y-%m-%d") if ts else None
    return float(price), day


def freddie_weeks() -> list[tuple[str, float]]:
    html = fetch("https://www.freddiemac.com/pmms/archive")
    pairs = re.findall(
        r"<strong[^>]*>\s*([A-Za-z]+ \d{2}, \d{4})\s*</strong>[\s\S]{0,900}?"
        r"<td class=\"text-center\">\s*([0-9]+\.[0-9]+)%</td>",
        html,
    )
    out = []
    for label, rate in pairs:
        month, day, year = label.replace(",", "").split()
        iso = f"{int(year):04d}-{MONTHS[month]:02d}-{int(day):02d}"
        out.append((iso, float(rate)))
    out.sort()
    return out[-8:]


def status_of(ten: float | None, mtg: float | None, streak: int, cfg: dict) -> str:
    if ten is None or mtg is None:
        return "FAIL"
    hi = float(cfg["ten_escalate"])
    lo = float(cfg["ten_relief"])
    mtg_hi = float(cfg["mtg_escalate"])
    need = int(cfg["mtg_weeks"])
    if ten >= hi or (mtg >= mtg_hi and streak >= need):
        return "ESCALATE"
    if ten < lo and mtg < mtg_hi:
        return "RELIEF"
    return "WATCH"


def band_of(close: float, cfg: dict) -> str:
    """5.15/4.85 band: ABOVE (> up), BELOW (< down), INSIDE (down <= close <= up)."""
    if close > float(cfg["ten_break_up"]):
        return "ABOVE"
    if close < float(cfg["ten_break_down"]):
        return "BELOW"
    return "INSIDE"


def band_cross(prev: str | None, close: float | None, cfg: dict) -> tuple[str | None, str | None]:
    """Return (new_band, crossing). crossing is UP, DOWN or None. Pushes only on a change of band
    into ABOVE or BELOW. prev None means first run: init, no push. Re-arm needs the buffer:
    ABOVE goes back to INSIDE only at close <= up - buffer, BELOW only at close >= down + buffer;
    inside the buffer the previous band is kept. Direct jumps across still fire."""
    if close is None:
        return prev, None
    close = round(float(close), 4)
    buf = float(cfg.get("ten_break_buffer", 0.0))
    new = band_of(close, cfg)
    if new == "INSIDE":
        if prev == "ABOVE" and close > round(float(cfg["ten_break_up"]) - buf, 4):
            new = "ABOVE"
        elif prev == "BELOW" and close < round(float(cfg["ten_break_down"]) + buf, 4):
            new = "BELOW"
    if prev is None or new == prev:
        return new, None
    if new == "ABOVE":
        return new, "UP"
    if new == "BELOW":
        return new, "DOWN"
    return new, None


def break_text(cross: str, cfg: dict, ten: float, ten_day: str | None):
    up = float(cfg["ten_break_up"])
    down = float(cfg["ten_break_down"])
    buf = float(cfg.get("ten_break_buffer", 0.0))
    day = ten_day or "날짜없음"
    if cross == "UP":
        title = f"[RATES] ^TNX 10년물 종가 {up:.2f}% 상향 돌파 | 추격 금지"
        lines = [
            f"레벨 돌파: 10년물 종가 {up:.2f}% 상향 돌파 (전 미국장 종가 {ten:.2f}%, {day}).",
            f"의미: {up:.2f}%는 10월 하순 박스 상단입니다. FOMC 뒤 재돌파면 '금리 정점 통과' 논리가 약해지고 한국 반도체 비중 확대 근거도 약해집니다.",
            "할 일: 반도체 추가 매수 보류, 비중 점검. 하루 종가로 전량 매도하지 마세요.",
            f"재무장: 종가가 {up - buf:.2f}% 이하로 내려오면 조용히 다시 감시합니다. {up - buf:.2f}~{up:.2f}% 사이에서는 다시 보내지 않습니다.",
        ]
        return title, lines, 4
    title = f"[RATES] ^TNX 10년물 종가 {down:.2f}% 하향 이탈 | 추가 하락"
    lines = [
        f"레벨 이탈: 10년물 종가 {down:.2f}% 하향 이탈 (전 미국장 종가 {ten:.2f}%, {day}).",
        f"의미: 10월 하순 박스({down:.2f}~{up:.2f}%) 아래로 추가 하락입니다. '금리 정점 통과' 논리에는 우호적입니다.",
        "할 일: 하루 수치로 전량 전환하지 마세요. 레버리지 복구는 금지.",
        f"재무장: 종가가 {down + buf:.2f}% 이상으로 올라오면 조용히 다시 감시합니다. {down:.2f}~{down + buf:.2f}% 사이에서는 다시 보내지 않습니다.",
    ]
    return title, lines, 3


def body(stamp: str, cfg: dict, ten, ten_day, mtg, mtg_day, streak: int, status: str, kind: str):
    ten_txt = f"{ten:.2f}%" if ten is not None else "없음"
    mtg_txt = f"{mtg:.2f}%" if mtg is not None else "없음"
    common = [
        f"{stamp} | FLOW | 미국 10년물, 30년 모기지",
        "이게 뭐 알림: 국채금리와 모기지 금리가 2008년식 위기 진입 조건에 닿는지 보는 알림입니다. 위기 확정이 아닙니다.",
    ]
    if kind == "FAIL" or status == "FAIL":
        title = "[RATES] ^TNX 금리 조회 실패 | 직접 확인"
        lines = common + [
            "지금 결과/상태: 10년물 또는 모기지 조회 실패. 실패를 관망으로 처리하지 않습니다.",
            "지금 할 일: 야후 ^TNX와 프레디맥 PMMS를 직접 확인하세요.",
            "나중에: 다음 평일 09:00 KST에 재시도합니다.",
            "익절/줄일 때: 이번 알림은 매매 신호가 아닙니다.",
            "하지 말 것: 조회 실패를 금리 하락이나 매수 신호로 보기. 신용, 미수.",
        ]
        return title, lines, 4
    if status == "ESCALATE":
        title = f"[RATES] ^TNX 10년 {ten_txt} | 위기경계, 추격 금지"
        action = "변경조건 충족. 신규 위험자산 추격 금지. 국채 듀레이션은 분할만 검토."
        pri = 5
    elif status == "RELIEF":
        title = f"[RATES] ^TNX 10년 {ten_txt} | 5% 아래 되돌림"
        action = "10년물이 5% 아래로 되밀렸습니다. 위기경계 해제 후보. 하루 수치로 전량 전환하지 마세요."
        pri = 4
    else:
        title = f"[RATES] ^TNX 10년 {ten_txt} | 관망, 추격 금지"
        action = "5.00~5.50% 관망 구간. 모기지 7.50% 2주 연속 전에는 위기 진입으로 보지 않습니다."
        pri = 3
    lines = common + [
        (
            f"지금 결과/상태: 10년물 {ten_txt} ({ten_day or '날짜없음'}). "
            f"30년 모기지 {mtg_txt} ({mtg_day or '날짜없음'}). "
            f"7.50% 이상 연속 {streak}주. 판정 {status}."
        ),
        f"지금 할 일: {action}",
        "나중에: 10년 종가 5.50% 안착, 또는 모기지 7.50%가 2주 연속이면 경계로 바꿉니다. 10년 5% 아래면 되돌림.",
        "익절/줄일 때: 경계에서는 위험자산 추격을 줄입니다. 되돌림이어도 레버리지 복구는 금지.",
        "하지 말 것: 이 알림만으로 2008년 재현이라 단정. 신용, 미수, 조회 실패를 정상으로 보기.",
    ]
    return title, lines, pri


def selftest() -> int:
    """Offline check of the 5.15/4.85 band: fake closes, no network, no state file, no ntfy."""
    global yahoo_tnx, freddie_weeks, publish, save_state, load_json
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    ok = True

    # 1) pure band logic
    cases = [
        (None, 5.00, "INSIDE", None), ("INSIDE", 5.15, "INSIDE", None), ("INSIDE", 5.18, "ABOVE", "UP"),
        ("ABOVE", 5.30, "ABOVE", None), ("ABOVE", 5.10, "INSIDE", None), ("INSIDE", 4.85, "INSIDE", None),
        ("INSIDE", 4.80, "BELOW", "DOWN"), ("BELOW", 4.70, "BELOW", None), ("BELOW", 5.20, "ABOVE", "UP"),
        ("ABOVE", 4.80, "BELOW", "DOWN"), (None, 5.20, "ABOVE", None), ("INSIDE", None, "INSIDE", None),
        # 0.03%p re-arm buffer
        ("ABOVE", 5.15, "ABOVE", None), ("ABOVE", 5.13, "ABOVE", None), ("ABOVE", 5.1201, "ABOVE", None),
        ("ABOVE", 5.12, "INSIDE", None), ("ABOVE", 5.119999771118164, "INSIDE", None),
        ("BELOW", 4.85, "BELOW", None), ("BELOW", 4.87, "BELOW", None), ("BELOW", 4.8799, "BELOW", None),
        ("BELOW", 4.88, "INSIDE", None), ("BELOW", 4.880000114, "INSIDE", None),
        ("BELOW", 5.14, "INSIDE", None), ("ABOVE", 4.86, "INSIDE", None),
    ]
    for prev, close, want_band, want_cross in cases:
        got = band_cross(prev, close, cfg)
        good = got == (want_band, want_cross)
        ok &= good
        print(f"{'OK ' if good else 'BAD'} band_cross({prev}, {close}) -> {got}")

    # 2) end-to-end through main() with fake closes in auto mode
    mem = {"state": {"dgs10": 4.988, "dgs10_date": "2026-10-23", "mortgage": 7.32,
                     "mortgage_date": "2026-10-22", "status": "RELIEF", "mtg_streak": 0,
                     "weekly_sent_date": "2026-10-26", "ten_band": "INSIDE",
                     "ten_band_date": "2026-10-23", "ten_band_close": 4.988}}
    sent = []
    seq = [
        ("2026-10-26", 5.00, None), ("2026-10-27", 5.18, "UP"), ("2026-10-28", 5.25, None),
        ("2026-10-29", 5.10, None), ("2026-10-30", 5.12, None), ("2026-11-02", 4.80, "DOWN"),
        ("2026-11-03", 4.75, None), ("2026-11-03", 4.75, None), ("2026-11-04", 4.90, None),
        ("2026-11-05", 5.16, "UP"),
        # buffer walk, up side: 5.13 keeps ABOVE, 5.16 no re-fire, 5.12 re-arms, 5.16 fires again
        ("2026-11-06", 5.13, None), ("2026-11-09", 5.16, None), ("2026-11-10", 5.12, None),
        ("2026-11-11", 5.16, "UP"), ("2026-11-12", 5.12, None),
        # down side: 4.80 fires, 4.87 keeps BELOW, 4.84 no re-fire, 4.88 re-arms, 4.84 fires again
        ("2026-11-13", 4.80, "DOWN"), ("2026-11-16", 4.87, None), ("2026-11-17", 4.84, None),
        ("2026-11-18", 4.88, None), ("2026-11-19", 4.84, "DOWN"),
        # direct jump across from BELOW still fires
        ("2026-11-20", 5.20, "UP"),
    ]
    cur = {}
    yahoo_tnx = lambda: (cur["ten"], cur["day"])
    freddie_weeks = lambda: [("2026-10-22", 7.32)]
    publish = lambda topic, title, msg, pri, tags, server: sent.append((title, msg, pri))
    save_state = lambda state, now: mem.__setitem__("state", dict(state))
    load_json = lambda path, fallback: {**fallback, **mem["state"]}
    argv = sys.argv
    sys.argv = [argv[0], "auto"]
    try:
        for day, ten, want in seq:
            cur.update(ten=ten, day=day)
            before = len(sent)
            main()
            new = sent[before:]
            breaks = [t for t, _, _ in new if "돌파" in t or "이탈" in t]
            got = None
            if breaks:
                got = "UP" if "상향" in breaks[0] else "DOWN"
            good = got == want and len(breaks) <= 1
            ok &= good
            print(f"{'OK ' if good else 'BAD'} {day} close={ten:.2f} band={mem['state'].get('ten_band')} "
                  f"pushes={len(new)} break={got} want={want} titles={[t for t, _, _ in new]}")
    finally:
        sys.argv = argv
    for title, msg, _ in sent:
        for ch in ("\u00b7", "\u2014"):
            if ch in title or ch in msg:
                ok = False
                print(f"BAD forbidden char in: {title}")
    print("SELFTEST", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    dry = mode in {"test", "dry", "dry-run"}
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    state = load_json(STATE_PATH, {
        "dgs10": None,
        "dgs10_date": None,
        "mortgage": None,
        "mortgage_date": None,
        "status": None,
        "mtg_streak": 0,
        "last_kind": None,
        "fail_sent_date": None,
        "weekly_sent_date": None,
        "ten_band": None,
        "ten_band_date": None,
        "ten_band_close": None,
        "updated_at": None,
    })
    now = datetime.now(KST)
    today = now.strftime("%Y-%m-%d")
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    year_ok = now.year == int(cfg["year_guard"])
    valid = today <= cfg["valid_until"]
    print(f"{stamp} mode={mode} year_ok={year_ok} valid={valid}")
    if mode not in {"force", "dry", "test", "dry-run"} and (not year_ok or not valid):
        print("outside window; silent")
        return 0

    ten = ten_day = None
    weeks = []
    fails = []
    try:
        ten, ten_day = yahoo_tnx()
    except Exception as exc:
        fails.append(f"tnx {exc}")
        print(f"tnx fail: {exc}", file=sys.stderr)
    try:
        weeks = freddie_weeks()
    except Exception as exc:
        fails.append(f"pmms {exc}")
        print(f"pmms fail: {exc}", file=sys.stderr)

    mtg = mtg_day = None
    streak = int(state.get("mtg_streak") or 0)
    if weeks:
        mtg_day, mtg = weeks[-1]
        prev_date = state.get("mortgage_date")
        if mtg_day != prev_date:
            streak = streak + 1 if mtg >= float(cfg["mtg_escalate"]) else 0
        elif mtg < float(cfg["mtg_escalate"]):
            streak = 0
    status = status_of(ten, mtg, streak, cfg)
    print(f"ten={ten} {ten_day} mtg={mtg} {mtg_day} streak={streak} status={status} fails={fails}")

    new_band, cross = band_cross(state.get("ten_band"), ten, cfg)
    print(f"band prev={state.get('ten_band')} new={new_band} cross={cross}")
    band_upd = {}
    if ten is not None:
        band_upd = {"ten_band": new_band, "ten_band_date": ten_day, "ten_band_close": ten}

    kind = None
    monday = now.weekday() == 0
    if status == "FAIL":
        if state.get("fail_sent_date") != today or mode in {"force", "dry", "test"}:
            kind = "FAIL"
    elif status != state.get("status") or mode in {"force", "dry", "test"}:
        kind = "CHANGE"
    elif monday and state.get("weekly_sent_date") != today:
        kind = "WEEKLY"
    elif cross:
        kind = "BREAK"

    if kind is None:
        print("no ratesbell trigger; silent")
        state.update({
            "dgs10": ten, "dgs10_date": ten_day, "mortgage": mtg,
            "mortgage_date": mtg_day, "status": status, "mtg_streak": streak,
        })
        state.update(band_upd)
        if not dry:
            save_state(state, now)
        return 0

    if kind == "BREAK":
        title, lines, pri = break_text(cross, cfg, ten, ten_day)
        lines = [
            f"{stamp} | FLOW | 미국 10년물 종가 레벨",
            "이게 뭐 알림: 2026-10-24 점검에서 정한 10년물 종가 레벨을 넘을 때만 1번 보내는 알림입니다.",
        ] + lines
    else:
        title, lines, pri = body(stamp, cfg, ten, ten_day, mtg, mtg_day, streak, status, kind)
        if cross:
            # Same push carries the crossing so one crossing never produces two pushes.
            b_title, b_lines, b_pri = break_text(cross, cfg, ten, ten_day)
            if cross == "UP":
                title += f" | {float(cfg['ten_break_up']):.2f}% 상향 돌파"
            else:
                title += f" | {float(cfg['ten_break_down']):.2f}% 하향 이탈"
            lines = lines + b_lines
            pri = max(pri, b_pri)
    msg = "\n".join(lines)
    print(title)
    print(msg)
    print(f"kind={kind} dry={dry}")
    if dry:
        print("dry-run; no ntfy")
    else:
        publish(
            resolve_topic("port", cfg.get("ntfy_topic")),
            title,
            msg,
            pri,
            ["chart_with_upwards_trend"],
            resolve_server(cfg.get("ntfy_server")),
        )
        if kind == "FAIL":
            state["fail_sent_date"] = today
        if kind == "WEEKLY":
            state["weekly_sent_date"] = today
        if monday and kind == "CHANGE":
            state["weekly_sent_date"] = today
    state.update({
        "dgs10": ten, "dgs10_date": ten_day, "mortgage": mtg,
        "mortgage_date": mtg_day, "status": status, "mtg_streak": streak,
        "last_kind": kind,
    })
    state.update(band_upd)
    if not dry:
        save_state(state, now)
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1].lower() == "selftest":
        raise SystemExit(selftest())
    raise SystemExit(main())
