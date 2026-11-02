#!/usr/bin/env python3
"""KOSPI upside level break alert. Silent unless a new level is broken or fetch fails.

Market-state zones (approved in chat 2026-10-30, owner note pt29u; replaces the paused external Grok automation
"코스피 8단계 ntfy 라인 체크" upside lines). Config `zones`, state `zones`:
  - z6700 재탈환: armed and a KRX close >= 6,700 -> one p3 message; re-arms only after a close < 6,600.
  - z7000 안착:  armed and the two latest KRX closes both >= 7,000 -> one p3 message; re-arms only after a close < 6,900.
  - both on the same run -> ONE combined message. First run seeds both armed (close 5,937.66 on 10/29, far below).
Sends go through kospi_spool like the 5750/6000 levels, so on the flow 15:40 slot they appear inside [KOSPI].
Modes: auto (default) | dry (no ntfy, no state) | selftest | confirm (install-confirmation send "[KOSPI] 설치 확인").
"""
from __future__ import annotations

import json
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import resolve_server, resolve_topic  # noqa: E402
from kospi_spool import publish  # noqa: E402  [KOSPI] ladder spool (2026-10-26)

KST = timezone(timedelta(hours=9))
CFG_PATH = ROOT / "configs" / "kosup.json"
STATE_PATH = ROOT / "state" / "kosup.json"
UA = {"User-Agent": "Mozilla/5.0 cwally-alerts"}


def load_json(path: Path, fallback: dict) -> dict:
    if not path.exists():
        return dict(fallback)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        out = dict(fallback)
        out.update(data)
        return out
    except json.JSONDecodeError:
        return dict(fallback)


def fetch(url: str, timeout: int = 20) -> str:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def num(text: str) -> float:
    cleaned = str(text).replace(",", "").replace("+", "").strip()
    return float(cleaned) if cleaned and cleaned != "-" else 0.0


def save(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def fail_once(state: dict, now: datetime, cfg: dict, title: str, note: str, dry: bool) -> None:
    day = now.strftime("%Y-%m-%d")
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    body = "\n".join([
        f"{stamp} | KOSUP",
        "이게 뭐 알림: 코스피 상방 레벨 점검 중 시세 조회가 실패했습니다.",
        f"지금 상태: {note}",
        "지금 할 일: 네이버 코스피 종가를 직접 확인하세요.",
        "나중에: 다음 평일 15:40 KST에 다시 봅니다.",
        "익절/줄일 때: 이 실패로 매매하지 마세요.",
        "하지 말 것: 조회 실패를 레벨 돌파로 해석.",
    ])
    print(body)
    if not dry and state.get("last_fail_day") != day:
        publish(resolve_topic("port", cfg.get("ntfy_topic")), title, body, 4, ["warning"], resolve_server(cfg.get("ntfy_server")))
        state["last_fail_day"] = day
    state["updated_at"] = stamp
    save(state)


def latest_closed(price_rows: list, basic: dict) -> dict | None:
    status = str((basic or {}).get("marketStatus") or "").upper()
    today = datetime.now(KST).strftime("%Y-%m-%d")
    rows = list(price_rows or [])
    if status == "CLOSE" and rows:
        return rows[0]
    for row in rows:
        if str(row.get("localTradedAt") or "")[:10] < today:
            return row
    return None


ZONE_BAD_WORDS = ("분할매수", "매수 검토", "매수 기회", "매수 구간", "저가 매수", "사세요", "진입 검토", "담을", "나눠서", "비중 확대", "지금 매수")
ZONE_DEFAULT = {"z6700": {"label": "6,700 재탈환", "line": 6700.0, "rearm_below": 6600.0, "days": 1, "priority": 3},
                "z7000": {"label": "7,000 안착", "line": 7000.0, "rearm_below": 6900.0, "days": 2, "priority": 3}}


def zone_step(zcfg: dict, zst: dict | None, bars: list[tuple[str, float]]) -> tuple[dict, list[str]]:
    """bars: completed (day, close), any order, latest included. zst None = first run: seed armed, nothing fires
    unless the condition already holds (it does not on 2026-10-30). Returns (new zone state, fired keys)."""
    zst = json.loads(json.dumps(zst)) if zst else {"armed": {k: True for k in zcfg}, "closes": []}
    hist = {d: c for d, c in zst.get("closes") or []}
    for d, c in bars:
        hist[d] = float(c)
    closes = sorted(hist.items())[-5:]
    zst["closes"] = [[d, c] for d, c in closes]
    if not closes:
        return zst, []
    last = closes[-1][1]
    fired = []
    for key, z in zcfg.items():
        armed = zst["armed"].get(key, True)
        recent = [c for _, c in closes[-int(z["days"]):]]
        hit = len(recent) == int(z["days"]) and all(c >= float(z["line"]) for c in recent)
        if armed and hit:
            zst["armed"][key] = False
            fired.append(key)
        elif not armed and last < float(z["rearm_below"]):
            zst["armed"][key] = True
    return zst, fired


def zone_message(zcfg: dict, fired: list[str], closes: list, stamp: str) -> tuple[str, str]:
    day, close = closes[-1]
    labels = " + ".join(zcfg[k]["label"] for k in fired)
    two = ", ".join(f"{d} {c:,.2f}" for d, c in closes[-2:])
    title = f"[KOSUP] KOSPI 종가 {close:,.2f} | {labels} | 추격 매수 보류, 근거 재확인"
    rearm = ", ".join(f"{zcfg[k]['label']}은 종가 {zcfg[k]['rearm_below']:,.0f} 아래로 내려간 뒤 재무장" for k in zcfg)
    body = "\n".join([
        f"{stamp} | KOSUP | 코스피 시장 상태 신호",
        "이게 뭐 알림: 코스피 확정 종가 기준 시장 상태 신호입니다(6,700 재탈환은 종가 1일, 7,000 안착은 종가 2일 연속). "
        "상태가 바뀔 때 1번만 옵니다. 매매 지시가 아닙니다.",
        f"지금 결과/상태: {labels}. 최근 종가 {two}.",
        "지금 할 일: 추격 매수 보류, 근거 재확인. 1) 외국인 수급(순매수가 이어지는지) 2) 원달러 환율(원화 강세 동반인지) "
        "3) 반도체 대형주 비중(삼성전자, SK하이닉스 쏠림으로만 오른 것인지) 4) 기존 하락 경보 해제 여부(하방 레벨, 5500 시나리오, 대기 논리 조건이 정리됐는지).",
        f"나중에: {rearm}. 같은 상태가 이어지는 동안에는 다시 보내지 않습니다.",
        "익절/줄일 때: 이미 가진 주식 비중이 계획보다 커졌다면 리밸런싱 기준만 점검하세요.",
        "하지 말 것: 레벨 회복만 보고 추격 매수, 신용/미수, 이 알림만 보고 주문.",
    ])
    return title, body


def closed_bars(price_rows: list, basic: dict) -> list[tuple[str, float]]:
    status = str((basic or {}).get("marketStatus") or "").upper()
    today = datetime.now(KST).strftime("%Y-%m-%d")
    out = []
    for row in price_rows or []:
        d = str(row.get("localTradedAt") or "")[:10]
        if d and (d < today or status == "CLOSE"):
            out.append((d, num(row.get("closePrice"))))
    return out


def selftest() -> int:
    zc = ZONE_DEFAULT
    fails = 0

    def check(label, got, want):
        nonlocal fails
        ok = got == want
        fails += not ok
        print(("ok  " if ok else "FAIL"), label, "" if ok else f"got {got!r} want {want!r}")
    st, f = zone_step(zc, None, [("2026-10-28", 6012.44), ("2026-10-29", 5937.66)])
    check("seed armed, silent", (st["armed"], f), ({"z6700": True, "z7000": True}, []))
    seq = [("11-02", 6650, []), ("11-03", 6700, ["z6700"]), ("11-04", 6800, []), ("11-05", 6650, []),
           ("11-06", 6600, []), ("11-09", 6720, []), ("11-10", 6599, []), ("11-11", 6700, ["z6700"]),
           ("11-12", 7000, []), ("11-13", 6990, []), ("11-16", 7010, []), ("11-17", 7020, ["z7000"]),
           ("11-18", 7100, []), ("11-19", 6950, []), ("11-20", 7050, []), ("11-23", 7060, []),
           ("11-24", 6899, []), ("11-25", 7001, []), ("11-26", 7002, ["z7000"])]
    for d, c, want in seq:
        st, f = zone_step(zc, st, [("2026-" + d, c)])
        check(f"walk {d} {c}", f, want)
    st, f = zone_step(zc, st, [("2026-11-26", 7002)])
    check("same day rerun no repeat", f, [])
    st, f = zone_step(zc, {"armed": {"z6700": True, "z7000": True}, "closes": [["2026-12-01", 6500]]},
                      [("2026-12-02", 7050), ("2026-12-03", 7080)])
    check("same-day combo", f, ["z6700", "z7000"])
    t, b = zone_message(zc, f, st["closes"], "2026-12-03 15:40 KST")
    check("combo one message title", t, "[KOSUP] KOSPI 종가 7,080.00 | 6,700 재탈환 + 7,000 안착 | 추격 매수 보류, 근거 재확인")
    check("six parts", [x.split(":")[0] for x in b.split("\n")[1:7]], ["이게 뭐 알림", "지금 결과/상태", "지금 할 일", "나중에", "익절/줄일 때", "하지 말 것"])
    check("no dot/dash", any(ch in t + b for ch in "\u00b7\u2014"), False)
    check("no buy wording", [w for w in ZONE_BAD_WORDS if w in t + b], [])
    check("checklist", all(x in b for x in ("외국인 수급", "환율", "반도체 대형주", "하락 경보 해제")), True)
    st2, f2 = zone_step(zc, {"armed": {"z6700": True, "z7000": True}, "closes": []}, [("2026-12-02", 7050)])
    check("7000 needs two closes", f2, ["z6700"])
    print("selftest", "PASS" if not fails else f"FAIL {fails}")
    return 1 if fails else 0


def confirm(cfg: dict) -> int:
    import ntfy_util
    st = load_json(STATE_PATH, {})
    z = (st.get("zones") or {}).get("closes") or []
    last = f"{z[-1][0]} 종가 {z[-1][1]:,.2f}" if z else "기록 없음"
    body = "\n".join([
        "[테스트] 코스피 6,700 재탈환, 7,000 안착 신호 설치 완료",
        "이게 뭐 알림: [KOSPI] 15:40 사다리에 시장 상태 신호 2개를 더했습니다. 멈춘 외부 Grok 8단계 라인 체크의 상방 신호를 대신합니다.",
        f"지금 결과/상태: 최근 {last}. 6,700 재탈환(종가 1일, 우선순위 3), 7,000 안착(종가 2일 연속, 3) 모두 무장.",
        "지금 할 일: 없음. 평일 15:40 KST 확정 종가로 판정하고 상태가 바뀔 때만 보냅니다. 둘이 같은 날 맞으면 한 건으로 옵니다.",
        "나중에: 6,700 은 종가 6,600 아래, 7,000 은 종가 6,900 아래로 내려간 뒤에만 다시 무장. 2026-12-31 까지(상방 레벨과 같은 종료일).",
        "익절/줄일 때: 해당 없음.",
        "하지 말 것: 이 설치 확인을 매수/매도 신호로 읽기.",
    ])
    title = "[KOSPI] 설치 확인 | 매매 신호 아님"
    print(title)
    print(body)
    ntfy_util.publish(resolve_topic("port", cfg.get("ntfy_topic")), title, body, 3, ["white_check_mark"],
                      resolve_server(cfg.get("ntfy_server")), config_name="kospi")
    return 0


def main() -> int:
    args = [a.lower() for a in sys.argv[1:]]
    if "selftest" in args:
        return selftest()
    if "confirm" in args:
        return confirm(load_json(CFG_PATH, {}))
    dry = "dry" in args
    global save
    if dry:
        def save(state: dict) -> None:  # noqa: F811  dry never writes state
            print("dry: state not saved")
    cfg = load_json(CFG_PATH, {})
    now = datetime.now(KST)
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    state = load_json(STATE_PATH, {"updated_at": None, "fired": {}, "last_sent": None, "last_fail_day": None})
    levels = sorted([float(x) for x in (cfg.get("levels") or [5750, 6000])])
    fired = dict(state.get("fired") or {})
    for lv in levels:
        fired.setdefault(str(int(lv)), False)

    if now.year != int(cfg.get("year_guard") or 2026):
        print("kosup year-guard silent")
        return 0
    expire = datetime.strptime(cfg.get("expire_date") or "2026-12-31", "%Y-%m-%d").date()
    if now.date() > expire:
        print("kosup expired silent")
        return 0
    if now.weekday() >= 5:
        print("kosup weekend silent")
        return 0

    try:
        basic = json.loads(fetch(cfg["sources"]["basic"]))
        price_rows = json.loads(fetch(cfg["sources"]["price"]))
    except Exception as exc:
        fail_once(state, now, cfg, "[KOSUP] KOSPI 시세 조회 실패 | 판정 보류", f"코스피 시세 조회 실패: {exc}", dry)
        return 0

    bar = latest_closed(price_rows, basic)
    if not bar:
        print("kosup no closed bar; silent")
        state["updated_at"] = stamp
        save(state)
        return 0

    day = str(bar.get("localTradedAt") or "")[:10]
    close = num(bar.get("closePrice"))

    # Reset if below a level
    for lv in levels:
        key = str(int(lv))
        if close < lv:
            fired[key] = False

    new_hits = []
    for lv in levels:
        key = str(int(lv))
        if close >= lv and not fired.get(key):
            fired[key] = True
            new_hits.append(lv)

    state["fired"] = fired
    state["updated_at"] = stamp
    state["last_bar"] = {"day": day, "close": close}
    print(stamp, "close", close, "new", new_hits)

    zcfg = cfg.get("zones") or ZONE_DEFAULT
    zst, zfired = zone_step(zcfg, state.get("zones"), closed_bars(price_rows, basic))
    print("zones", zst["armed"], "fired", zfired, "closes", zst["closes"][-2:])
    if zfired:
        zt, zb = zone_message(zcfg, zfired, zst["closes"], stamp)
        print(zt)
        print(zb)
        if not dry:
            publish(resolve_topic("port", cfg.get("ntfy_topic")), zt, zb, max(int(zcfg[k]["priority"]) for k in zfired),
                    ["chart_with_upwards_trend"], resolve_server(cfg.get("ntfy_server")))
            zst["last_sent"] = {"at": stamp, "fired": zfired}
    state["zones"] = zst

    if not new_hits:
        save(state)
        print("kosup silent; no new level break")
        return 0

    broken = ", ".join(f"{int(lv)}" for lv in new_hits)
    title = f"[KOSUP] KOSPI 종가 {close:,.2f} | {broken} 돌파 | 상방 확인"
    body = "\n".join([
        f"{stamp} | KOSUP",
        "이게 뭐 알림: 코스피 상방 레벨 돌파입니다. 매수 신호가 아닙니다.",
        f"지금 상태: {day} 종가 {close:,.2f}. 돌파 레벨: {broken}.",
        "지금 할 일: 해당 저항 안착과 외국인 수급을 점검하세요. 하방 시나리오 유지.",
        "나중에: 종가가 해당 레벨 아래로 내려오면 같은 레벨은 리셋됩니다.",
        "익절/줄일 때: 이 알림은 매수라는 주문이 아닙니다.",
        "하지 말 것: 레벨 돌파를 즉시 추격 매수로 해석. 신용/미수.",
    ])
    print(body)
    if not dry:
        publish(resolve_topic("port", cfg.get("ntfy_topic")), title, body, 4, ["warning"], resolve_server(cfg.get("ntfy_server")))
        state["last_sent"] = {"at": stamp, "hits": new_hits}
    save(state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
