#!/usr/bin/env python3
"""[XBI] SPDR S&P 바이오텍 ETF 분할 매수 구간 점검. 평일 09:00 KST morning 묶음 (lrcx 옆).

가격: 미국 전일 종가(Yahoo 일봉). 데이터 거래일(state.last_date)로 중복 방지.
구간(configs/xbi.json zones): 들어오면 1번 알림(p3), 경계 밖으로 rearm_pct(4%) 넘게 벗어난 뒤 다시 들어오면 다시 알림.
추격 금지선(nochase) 이상이면 신규 매수 보류 알림. 조회 실패는 cwally-ops 하루 1번.
modes: auto | dry(발송 없음, state 안 씀) | selftest(가짜 값, 네트워크 없음)
"""
from __future__ import annotations

import json
import sys
import traceback
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic, route_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
ET = ZoneInfo("America/New_York")
CFG_PATH = ROOT / "configs" / "xbi.json"
STATE_PATH = ROOT / "state" / "xbi.json"
WEB_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
NA = "확인 불가"


def fetch(url: str, headers: dict) -> str:
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="ignore")


# ---------- 구간 판정 (lrcx 와 같은 재진입 규칙) ----------
def in_zone(p: float, z: dict) -> bool:
    return p >= z["lo"] and (z["hi"] is None or p <= z["hi"])


def far_out(p: float, z: dict, pct: float) -> bool:
    k = pct / 100
    return p < z["lo"] * (1 - k) or (z["hi"] is not None and p > z["hi"] * (1 + k))


def judge_zones(zones: list[dict], zst: dict, p: float, pct: float) -> list[dict]:
    fired = []
    for z in zones:
        s = zst.setdefault(z["key"], {"armed": True})
        if in_zone(p, z):
            if s["armed"]:
                s["armed"] = False
                fired.append(z)
        elif not s["armed"] and far_out(p, z, pct):
            s["armed"] = True
    return fired


def seed_zones(zones: list[dict], p: float) -> dict:
    return {z["key"]: {"armed": not in_zone(p, z)} for z in zones}


def zone_where(zones: list[dict], p: float) -> str:
    for z in zones:
        if in_zone(p, z):
            return z["key"]
    return "구간 밖"


# ---------- 본문 ----------
def body(stamp: str, what: str, status: str, action: str, later: str, trim: str, avoid: str) -> str:
    return "\n".join([f"{stamp} | XBI | 바이오텍 ETF 분할 매수 점검", f"이게 뭐 알림: {what}", f"지금 결과/상태: {status}",
                      f"지금 할 일: {action}", f"나중에: {later}", f"익절/줄일 때: {trim}", f"하지 말 것: {avoid}"])


def zone_table(cfg: dict) -> str:
    zs = {z["key"]: z for z in cfg["zones"]}
    return (f"추격 금지 ${zs['nochase']['lo']:g} 이상, 1차 {zs['z1']['lo']:g}~{zs['z1']['hi']:g}({zs['z1']['budget_manwon']}만원), "
            f"2차 {zs['z2']['lo']:g}~{zs['z2']['hi']:g}({zs['z2']['budget_manwon']}만원), 3차 {zs['z3']['lo']:g}~{zs['z3']['hi']:g}({zs['z3']['budget_manwon']}만원)")


def zone_msg(cfg: dict, z: dict, day: str, p: float, stamp: str) -> tuple[str, str, int]:
    dd = (p / cfg["high_52w"] - 1) * 100
    k = z["key"]
    if k == "nochase":
        title = f"[XBI] 추격 금지선 돌파 | 종가 ${p:.2f} > ${z['lo']:g}, 신규 매수 보류"
        action = "신규 매수 보류. 이미 산 물량은 이 알림으로 정하지 않기."
    else:
        n = {"z1": "1", "z2": "2", "z3": "3"}[k]
        title = f"[XBI] {n}차 분할 매수 구간 진입 | 종가 ${p:.2f} ({z['lo']:g}~{z['hi']:g}, 고점 대비 {dd:.1f}%)"
        action = f"계획한 {n}차 {z['budget_manwon']}만원만 검토. 한 번에 다 사지 않기. 해외 계좌 기준 (ISA 는 XBI 불가)."
    status = f"{day} 미국 종가 ${p:.2f}, 52주 고점 ${cfg['high_52w']:g} 대비 {dd:.1f}%. 구간: {zone_table(cfg)}."
    msg = body(stamp, "XBI 종가가 미리 정한 분할 매수 구간이나 추격 금지선에 들어왔다는 알림. 매매 지시가 아닙니다.", status, action,
               f"같은 구간은 종가가 경계 밖으로 {cfg['rearm_pct']:g}% 넘게 벗어났다가 다시 들어올 때만 다시 옵니다. 미 10년물 5.2% 초과나 추가 인상 신호가 나오면 구간을 낮추는 검토.",
               "해외 양도세 22%(연 250만원 공제)와 환율을 같이 보기. 구간 알림만으로 비중을 줄이지 않기.",
               "추격 금지선 위에서 신규 매수, 금리 급등 중 남은 금액 한꺼번에 넣기.")
    return title, msg, z["priority"]


def ops_msg(stamp: str, title_tail: str, status: str) -> tuple[str, str, int]:
    return (f"[XBI] {title_tail}", body(stamp, "XBI 알림 데이터 수집 안내. 매매 신호 아님.", status, "필요하면 시세를 직접 보세요.",
                                         "다음 평일 09:00 에 다시 봅니다.", "이 안내로 비중을 바꾸지 않기.", "조회 실패를 조건 미충족으로 읽기."), 3)


# ---------- 가격 ----------
def last_close(url: str, now: datetime) -> tuple[str, float] | None:
    d = json.loads(fetch(url, WEB_UA))
    res = ((d.get("chart") or {}).get("result") or [None])[0] or {}
    ts = res.get("timestamp") or []
    closes = (((res.get("indicators") or {}).get("quote") or [{}])[0]).get("close") or []
    et_now = now.astimezone(ET)
    best = None
    for t, c in zip(ts, closes):
        if c is None:
            continue
        day = datetime.fromtimestamp(t, ET).date()
        if day < et_now.date() or (day == et_now.date() and (et_now.hour, et_now.minute) >= (16, 15)):
            best = (day.isoformat(), round(float(c), 2))
    return best


def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def run(mode: str, now: datetime, cfg: dict) -> int:
    dry = mode == "dry"
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    st = load_state()
    out: list[tuple[str, str, int]] = []
    try:
        lc = last_close(cfg["sources"]["quote"], now)
    except Exception as exc:  # noqa: BLE001
        lc, qerr = None, str(exc)[:150]
    else:
        qerr = None if lc else "종가 없음"
    if lc:
        day, p = lc
        if "zones" not in st:
            st["zones"] = seed_zones(cfg["zones"], p)
            st["last_date"] = day
            print(f"seeded zones at {day} ${p}: {st['zones']}")
        if day != st.get("last_date"):
            for z in judge_zones(cfg["zones"], st["zones"], p, cfg["rearm_pct"]):
                out.append(zone_msg(cfg, z, day, p, stamp))
            st["last_date"] = day
        st["last_close"] = [day, p]
        print(f"close {day} ${p:.2f} -> {zone_where(cfg['zones'], p)}, armed={ {k: v['armed'] for k, v in st['zones'].items()} }")
    elif st.get("quote_fail") != stamp[:10]:
        out.append(ops_msg(stamp, "종가 조회 실패 | 구간 판정 못 함", f"Yahoo 일봉 {NA} ({qerr})."))
        st["quote_fail"] = stamp[:10]
    topic, server = resolve_topic("port", cfg.get("ntfy_topic")), resolve_server(cfg.get("ntfy_server"))
    for t, m, p in out:
        assert "\u00b7" not in t + m and "\u2014" not in t + m, t
        print(f"TITLE: {t}\nPRIORITY: {p} TOPIC: {route_topic(topic, t)}\n{m}\n")
        if not dry:
            publish(topic, t, m, p, ["dna"], server)
    if not out:
        print("no alert")
    if dry:
        print("DRY RUN: no publish, state not written")
        return 0
    st["updated_at"] = now.isoformat()
    STATE_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


# ---------- selftest ----------
def selftest(cfg: dict) -> int:
    zones, pct = cfg["zones"], cfg["rearm_pct"]
    stamp = "2026-11-03 09:00 KST"
    zst = seed_zones(zones, 157.51)
    assert all(v["armed"] for v in zst.values())
    # z1 147~151: 재무장은 151*1.04=157.04 초과 또는 147*0.96=141.12 미만
    seq = [(150.0, ["z1"]), (148.0, []), (154.0, []), (149.0, []), (157.5, []), (150.5, ["z1"]), (144.0, ["z2"]),
           (139.0, []), (136.0, ["z3"]), (125.0, []), (133.0, ["z3"]), (143.0, ["z2"]), (152.0, []), (166.0, ["nochase"]),
           (170.0, []), (160.0, []), (165.5, []), (158.0, []), (165.0, ["nochase"]), (150.0, ["z1"]), (144.5, ["z2"])]
    for p, want in seq:
        got = [z["key"] for z in judge_zones(zones, zst, p, pct)]
        assert got == want, (p, got, want, zst)
    print("zone seq ok (재진입은 4% 넘게 벗어난 뒤만)")
    for p in (166.2, 149.4, 142.1, 134.0):
        z = next(z for z in zones if in_zone(p, z))
        t, m, pr = zone_msg(cfg, z, "2026-11-02", p, stamp)
        assert "\u00b7" not in t + m and "\u2014" not in t + m and route_topic("cwally-port", t) == "cwally-port", t
        assert "확인 불가" not in t and "조회 실패" not in t and pr == 3, t
        print(f"-- sample\nTITLE: {t}\nPRIORITY: {pr}\n{m}\n")
    t, _, _ = ops_msg(stamp, "종가 조회 실패 | 구간 판정 못 함", "x")
    assert route_topic("cwally-port", t) == "cwally-ops", t
    print("SELFTEST OK: fixtures only, no network, no publish, no state")
    return 0


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    if mode == "selftest":
        return selftest(cfg)
    if mode not in {"auto", "dry"}:
        print(f"unknown mode {mode}")
        return 2
    now = datetime.now(KST)
    try:
        return run(mode, now, cfg)
    except Exception as exc:  # noqa: BLE001
        print(traceback.format_exc(), file=sys.stderr)
        if mode == "dry":
            return 1
        try:
            t, m, p = ops_msg(now.strftime("%Y-%m-%d %H:%M KST"), "스크립트 오류 | 이번 실행 판정 못 함", f"{type(exc).__name__}: {str(exc)[:200]}")
            publish(resolve_topic("ops"), t, m, p, ["warning"], resolve_server(cfg.get("ntfy_server")))
        except Exception:  # noqa: BLE001
            return 1
        return 0


if __name__ == "__main__":
    sys.exit(main())
