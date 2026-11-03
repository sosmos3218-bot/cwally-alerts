#!/usr/bin/env python3
"""[000815] 삼성화재우 new-buy alert on the k203780 19:00 KST slot (approved in chat 2026-11-03, t41u).

Replaces the [000810] common-share price levels (615,000 / 580,000, configs/k000810.json price part, retired
2026-11-03) with the preferred share as the vehicle. One run, one state file (state/000815.json), topic cwally-port,
failures to cwally-ops. Parts:
  lines        configs/000815.json "lines" through the 005930 engine (scripts/005930_monitor.py): KRX regular close
               (Yahoo 000815.KS) <= 360,000 (1차, 1/3 of target) and <= 340,000 (3차 대안). Once each, re-arm after the
               close moves more than 1% back above. No 400,000 line (2차 is decided in the [000810] Q3 verdict).
  ratio        engine "pair" + "ratio_lines": same-session discount (000810 - 000815) / 000810, <= 25.0% 우선주 이점 소멸
               (추가 매수 중단), >= 48.0% 할인 확대 경고, 1%p re-arm. Hold when either close is missing.
  sec_line     this file: Samsung Electronics (Yahoo 005930.KS regular close) <= 171,750 (11/2 official 229,000 -25%)
               once, re-arm after > 1% above: '철회 조건 충족 검토' with the manual withdrawal items as reminder text.
               Own cursor st["sec"]; Yahoo failure -> hold (cursor unchanged), the daily ops notice.
  notice_watch engine: 000810 / 000815 filings since 2026-11-04 (정정 excluded), one alert per filing number.
  footer_note  holding line on every message.
Ends 2027-03-31 23:59 KST (configs ends_kst): after that the run is silent and reads nothing.
Crash guard: any exception in auto/force sends one ops notice and exits 0 so the rest of the 19:00 bundle runs.
Modes: auto (slot) | force (ignore holiday) | dry (live data, print, no ntfy, no state) | selftest (offline) |
       install (one install-confirmation send to cwally-port, no state).
"""
from __future__ import annotations

import copy
import importlib.util
import sys
import traceback
from datetime import datetime
from pathlib import Path

_spec = importlib.util.spec_from_file_location("krplan_005930", Path(__file__).resolve().parent / "005930_monitor.py")
engine = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(engine)

sys.path.insert(0, str(Path(__file__).resolve().parent))
from kr_calendar import is_trading_day, session_day  # noqa: E402
from ntfy_util import publish, resolve_server, resolve_topic  # noqa: E402

NAME = "000815"
KST = engine.KST
OPS = "cwally-ops"
fmt = engine.fmt


# ---------- data (injectable through io for the offline selftest) ----------
def sec_collect(cfg: dict, now: datetime) -> tuple[list[dict], list[str]]:
    s = cfg["sec_line"]
    try:
        rows = engine.yahoo_rows(cfg, s["yahoo_symbol"], regular_only=True)
    except Exception as exc:
        return [], [f"{s['code']} 정규장 종가 (Yahoo {str(exc)[:80]}; 철회선 판단 보류, 다음 실행에서 따라잡음)"]
    gap = engine.regular_gap(rows, now)
    return rows, ([f"{s['code']} {gap} 정규장 종가 없음 (Yahoo; 철회선 판단 보류, 다음 실행에서 따라잡음)"] if gap else [])


def live_io() -> dict:
    return {"collect": engine.collect, "sec": sec_collect, "notice_rows": None, "ref": None}


# ---------- Samsung Electronics withdrawal line ----------
def sec_eval(cfg: dict, st: dict, rows: list[dict], now: datetime) -> list[dict]:
    s = cfg["sec_line"]
    ss = st.setdefault("sec", {"last_day": cfg["seed"]["day"], "last_close": None, "armed": True, "fired_day": None})
    today = session_day(now).isoformat()
    close_hm = tuple(map(int, cfg.get("market_close_kst", "15:30").split(":")))
    ln = {"op": s["op"], "level": s["level"]}
    out = []
    for r in rows:
        if r["day"] <= (ss.get("last_day") or ""):
            continue
        if r["day"] > today or (r["day"] == today and (now.hour, now.minute) < close_hm):
            continue
        if ss.get("armed", True) and engine.hit(ln, r["close"]):
            ss.update(armed=False, fired_day=r["day"])
            out.append({"day": r["day"], "close": r["close"]})
        elif not ss.get("armed", True) and engine.rearm(ln, r["close"], float(s.get("rearm_pct", 1.0))):
            ss["armed"] = True
            print(f"{NAME} sec line re-armed on {r['day']} close {r['close']:,.0f}")
        ss["last_day"], ss["last_close"] = r["day"], r["close"]
    return out


def build_sec(cfg: dict, events: list[dict], data: dict, stamp: str) -> tuple[str, str, int]:
    s = cfg["sec_line"]
    e = events[-1]
    title = f"{cfg['title_prefix']} 삼성전자 종가 {fmt(s['level'])} 이하 | {s['title']}"
    pl = engine.pair_last(data)
    px = (f" 같은 기준 최근 KRX 종가({pl[0]}): 우선주 {fmt(pl[1])}, 보통주 {fmt(pl[2])}, 괴리율 {pl[3]:.2f}%." if pl else " 우선주, 보통주 종가 없음.")
    msg = "\n".join([
        engine.head(cfg, stamp),
        f"이게 뭐 알림: 삼성화재우 신규 매수 계획의 철회 조건 중 자동으로 볼 수 있는 하나(삼성전자 종가 {s['drop_pct']}% 넘게 하락)가 충족됐다는 알림입니다. "
        "삼성화재 보통주 시가총액의 약 3분의 2가 삼성전자 지분 가치라서 이 계획의 철회 조건입니다. 매도 지시가 아닙니다.",
        f"지금 결과/상태: {s['label']}: 종가 {fmt(e['close'])}({e['day']}), 기준 {fmt(s['level'])}, 기준 대비 {engine.dist(e['close'], s['level']):+.2f}%, "
        f"{s['base_day']} 종가 {fmt(s['base_close'])} 대비 {engine.dist(e['close'], s['base_close']):+.1f}%.{px} (Yahoo 정규장)",
        "지금 할 일: 철회 조건 충족을 검토하세요. 남은 분할 매수(2차, 3차)는 멈추고, 아래 수동 항목도 함께 확인하세요: " + ", ".join(s["manual"]) + ".",
        "나중에: 이 선은 삼성전자 종가가 173,468원(1%) 넘게 올라갔다 와야 다시 울립니다. 수동 항목(K-ICS, DPS 가이던스, 신계약 CSM)은 자동 확인이 안 됩니다.",
        "익절/줄일 때: 이미 산 분량은 수동 항목까지 보고 정하세요. 이 알림 하나로 전량 매도하지 마세요.",
        "하지 말 것: 삼성전자 급락 날 우선주 물타기, 신용 및 미수, 조회 실패를 신호로 읽기.",
    ])
    return title, msg, int(s.get("priority", 4))


# ---------- run ----------
def seed_state(cfg: dict) -> dict:
    sd = cfg["seed"]
    st = engine.default_state(cfg)
    st.update(seeded=True, last_day=sd["day"], last_close=float(sd["close"]))
    for ln in cfg["lines"]:
        st["lines"][ln["key"]]["armed"] = not engine.hit(ln, st["last_close"])
    v = engine.discount(sd["close"], sd["pair_close"])
    st["ratio"].update(last_day=sd["day"], last=round(v, 4))
    for ln in cfg["ratio_lines"]:
        st["ratio"]["lines"][ln["key"]]["armed"] = not engine.ratio_hit(ln, v)
    sl = cfg["sec_line"]
    st["sec"] = {"last_day": sd["day"], "last_close": float(sd["sec_close"]), "armed": not engine.hit({"op": sl["op"], "level": sl["level"]}, sd["sec_close"]),
                 "fired_day": None}
    st["seed_note"] = f"config seed {sd['day']}: 우선주 {fmt(sd['close'])}, 보통주 {fmt(sd['pair_close'])}(괴리율 {v:.2f}%), 삼성전자 {fmt(sd['sec_close'])}"
    return st


def ended(cfg: dict, now: datetime) -> bool:
    return now > datetime.strptime(cfg["ends_kst"], "%Y-%m-%d %H:%M").replace(tzinfo=KST)


def plan(cfg: dict, st: dict, now: datetime, io: dict) -> list[tuple]:
    """Mutates st; returns [(title, msg, pri, tags)]: SEC line, then the engine (lines, ratio, notices, fail notice)."""
    data, fails = io["collect"](cfg, now)
    srows, sfails = io["sec"](cfg, now)
    print(f"data src={data.get('src')} rows={(data.get('rows') or [])[-2:]} pair={(data.get('pair_rows') or [])[-2:]} sec={srows[-2:]} fails={fails + sfails}")
    sends = []
    ev = sec_eval(cfg, st, srows, now)
    if ev:
        t, m, p = build_sec(cfg, ev, data, now.strftime("%Y-%m-%d %H:%M KST"))
        m = engine.with_footer(cfg, m)
        engine.clean(t, m)
        sends.append((t, m, p, ["warning"]))
    kw = {"notice_fn": io.get("notice_rows")}
    if io.get("ref"):
        kw["ref_fn"] = io["ref"]
    return sends + engine.plan_sends(cfg, st, data, list(fails) + list(sfails), now, **kw)


def install_msg(cfg: dict, data: dict, srows: list[dict], fails: list[str], stamp: str) -> tuple[str, str]:
    pl = engine.pair_last(data)
    if pl:
        d, m_, p_, v = pl
        px = f"최근 KRX 종가({d}) 우선주 {fmt(m_)}, 보통주 {fmt(p_)}, 괴리율 {v:.2f}%, 후행 배당수익률 {cfg['yield']['dps'] / m_ * 100:.2f}%."
    else:
        sd = cfg["seed"]
        px = f"시드 {sd['day']} 우선주 {fmt(sd['close'])}, 보통주 {fmt(sd['pair_close'])}, 괴리율 {engine.discount(sd['close'], sd['pair_close']):.2f}%."
    sx = f" 삼성전자 {fmt(srows[-1]['close'])}({srows[-1]['day']})." if srows else ""
    s = cfg["sec_line"]
    title = f"{cfg['title_prefix']} 설치 확인 | 매매 신호 아님"
    msg = "\n".join([
        engine.head(cfg, stamp),
        f"이게 뭐 알림: 삼성화재우 신규 매수 알림 설치 확인입니다. KRX 거래일 19:00에 조건을 새로 넘을 때만 옵니다. 끝: {cfg['ends_kst']} KST. [000810] 보통주 615,000원/580,000원 가격대는 이 알림으로 옮겼습니다.",
        f"지금 결과/상태: {px}{sx} 판단은 Yahoo KRX 정규장 종가만 씁니다. 종가가 없으면 판단 보류, 운영 알림 하루 1회." + (" 조회 실패: " + ", ".join(fails) + "." if fails else ""),
        "지금 할 일: 없음. 감시 1) 우선주 종가 360,000원 이하 1차(목표의 3분의 1, 지정가), 340,000원 이하 3차 대안. 선마다 1회, 1% 넘게 위로 돌아와야 재무장. 400,000원 선은 없음. "
        "2) 괴리율((보통주 - 우선주) / 보통주) 25% 이하 우선주 이점 소멸(추가 매수 중단), 48% 이상 할인 확대 경고, 1%p 재무장. "
        f"3) 삼성전자 종가 {fmt(s['level'])} 이하(11/2 공식 종가 {fmt(s['base_close'])} 대비 -{s['drop_pct']}%) 철회 조건 충족 검토 1회. "
        "4) 삼성화재 보통주, 우선주 공시(배당 결정, 주식 소각, 자기주식, 기업가치 제고 계획, 삼성전자 지분 처분, IR 개최, 종류주식, 우선주) 건마다 1회, 11/4부터, 정정 제외.",
        "나중에: 11/12 10:00 KST 3분기 컨퍼런스콜. 실적 1회 판정(지배순익 4,600억 미만 하회, 5,200억 이상 상회)과 2차(우선주 400,000원 이하, 조건 충족 때만) 안내는 [000810] 알림이 보냅니다. "
        "수동 철회 항목: " + ", ".join(s["manual"]) + ".",
        "익절/줄일 때: 괴리율 25% 이하 알림 때 추가 매수 중단과 보유분 점검.",
        "하지 말 것: 이 설치 확인을 매수 신호로 읽기, 시장가 주문, 391,000원대 추격 매수.",
    ])
    msg = engine.with_footer(cfg, msg)
    engine.clean(title, msg)
    return title, msg


def crash_msg(cfg: dict, exc: Exception, stamp: str) -> tuple[str, str]:
    t = f"{cfg['title_prefix']} 실행 오류 | 조회 실패, 판단 보류"
    m = "\n".join([
        engine.head(cfg, stamp),
        f"이게 뭐 알림: {cfg['short']} 알림 실행 중 오류가 나서 이번 판단을 건너뜁니다. 매매 신호가 아닙니다.",
        f"지금 결과/상태: {type(exc).__name__}: {engine.scrub(str(exc))[:160]}",
        "지금 할 일: 없음. 다음 KRX 거래일 19:00에 다시 돕니다. 밀린 종가는 다음 실행에서 순서대로 판단합니다.",
        "나중에: 같은 오류가 이어지면 스크립트를 점검하세요.",
        "익절/줄일 때: 이 알림만으로 줄이지 마세요.",
        "하지 말 것: 실행 오류를 조건 충족으로 읽기.",
    ])
    engine.clean(t, m)
    return t, m


def execute(mode: str, cfg: dict, st: dict | None, now: datetime, io: dict, pub, save) -> int:
    """pub(topic, title, msg, pri, tags, server); save(st). st None = absent -> config seed."""
    dry = mode == "dry"
    topic = resolve_topic("port", cfg.get("ntfy_topic"))
    server = resolve_server(cfg.get("ntfy_server"))
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    if ended(cfg, now):
        print(f"{stamp} {NAME} ended ({cfg['ends_kst']} KST); silent")
        return 0
    if mode == "install":
        data, fails = io["collect"](cfg, now)
        srows, sfails = io["sec"](cfg, now)
        t, m = install_msg(cfg, data, srows, list(fails) + list(sfails), stamp)
        print(t)
        print(m)
        pub(topic, t, m, 3, ["white_check_mark"], server)
        return 0
    if not is_trading_day(session_day(now)) and mode not in {"force", "dry"}:
        print(f"{session_day(now)} KRX holiday; silent")
        return 0
    if not st or not st.get("seeded"):
        st = seed_state(cfg)
        print(st["seed_note"])
    try:
        sends = plan(cfg, st, now, io)
    except Exception as exc:
        if dry:
            raise
        traceback.print_exc()
        t, m = crash_msg(cfg, exc, stamp)
        pub(OPS, t, m, 3, ["warning"], server)
        return 0
    st["updated_at"] = now.isoformat()
    if not sends:
        print("no new condition; silent")
    for t, m, p, tg in sends:
        print(t)
        print(m)
        print(f"topic={topic} send={not dry}")
        if not dry:
            pub(topic, t, m, p, tg, server)
    if dry:
        print("DRY RUN: no ntfy sent, state not written")
    else:
        save(st)
    return 0


def main(argv: list[str]) -> int:
    mode = (argv[1] if len(argv) > 1 else "auto").lower()
    if mode not in {"auto", "force", "dry", "selftest", "install"}:
        print(f"unknown mode {mode}", file=sys.stderr)
        return 2
    if mode == "selftest":
        return selftest()
    now = datetime.now(KST)
    try:
        cfg = engine.load_cfg(NAME)
        st = engine.load_state(cfg) if engine.state_path(NAME).exists() and mode != "install" else None
    except Exception as exc:  # config or state unreadable: crash guard
        if mode == "dry":
            raise
        traceback.print_exc()
        publish(OPS, f"[{NAME}] 실행 오류 | 조회 실패, 판단 보류", f"{now:%Y-%m-%d %H:%M KST} | {NAME} | 설정 또는 상태 파일을 못 읽음: {type(exc).__name__}",
                3, ["warning"], "https://ntfy.sh")
        return 0
    if mode == "dry":
        print("DRY RUN")
        if st:
            st = copy.deepcopy(st)
    return execute(mode, cfg, st, now, live_io(), publish, lambda s: engine.save_state(cfg, s))


# ---------- offline selftest ----------
def selftest() -> int:
    import socket
    import urllib.request
    from unittest import mock

    from ntfy_util import route_topic

    cfg = engine.load_cfg(NAME)
    pfx = cfg["title_prefix"]
    foot = cfg["footer_note"]
    n = {"ok": 0, "fail": 0}

    def check(label, got, want):
        good = got == want
        n["ok" if good else "fail"] += 1
        print(f"{'ok  ' if good else 'FAIL'} {label}: got={got} want={want}")

    def at(d: str, h: int = 19, m: int = 0) -> datetime:
        return datetime.fromisoformat(d).replace(hour=h, minute=m, tzinfo=KST)

    def rows(*pairs):
        return [{"day": d, "close": float(c)} for d, c in pairs]

    sp = engine.state_path(NAME)
    before = sp.read_bytes() if sp.exists() else None
    world = {"rows": [], "pair": [], "sec": [], "notices": {}, "fail": None, "secfail": None}

    def collect(c, now):
        if world["fail"]:
            return {"rows": [], "pair_rows": [], "src": None}, [world["fail"]]
        return {"rows": list(world["rows"]), "pair_rows": list(world["pair"]), "src": "Yahoo 정규장"}, []

    def sec(c, now):
        return ([], [world["secfail"]]) if world["secfail"] else (list(world["sec"]), [])

    def notice_rows(c, code):
        v = world["notices"].get(code, [])
        if isinstance(v, Exception):
            raise v
        return list(v)

    io = {"collect": collect, "sec": sec, "notice_rows": notice_rows, "ref": lambda c, r, now: f"{c['short']} NXT 정보 없음"}
    sent: list = []
    saved: list = []

    def pub(topic, title, msg, pri=3, tags=None, server=None):
        sent.append((topic, title, msg, pri))

    def save(s):
        saved.append(copy.deepcopy(s))

    def blocked(*a, **k):
        raise RuntimeError("offline selftest: network blocked")

    def run(mode, when, st):
        sent.clear()
        saved.clear()
        execute(mode, cfg, st, when, io, pub, save)
        return [t for _, t, _, _ in sent]

    def reset(**kw):
        world.update(rows=[], pair=[], sec=[], notices={}, fail=None, secfail=None)
        world.update(kw)

    with mock.patch.object(urllib.request, "urlopen", blocked), mock.patch.object(socket, "create_connection", blocked), \
            mock.patch.object(engine, "get_json", blocked), mock.patch.object(engine, "yahoo_rows", blocked), \
            mock.patch.object(engine, "naver_rows", blocked):
        sd = cfg["seed"]
        seed = seed_state(cfg)
        check("seed: config seed only, cursors 11-02", (seed["last_day"], seed["ratio"]["last_day"], seed["sec"]["last_day"]), ("2026-11-02",) * 3)
        check("seed: all armed (391,500 / 39.86% / 229,000)", (all(v["armed"] for v in seed["lines"].values()),
                                                                all(v["armed"] for v in seed["ratio"]["lines"].values()), seed["sec"]["armed"]), (True, True, True))
        check("seed discount 39.86%", round(engine.discount(sd["close"], sd["pair_close"]), 2), 39.86)
        base = dict(rows=rows(("2026-11-02", 391500)), pair=rows(("2026-11-02", 651000)), sec=rows(("2026-11-02", 229000)))
        reset(**base)
        st = copy.deepcopy(seed)
        check("seed closes re-run silent", run("auto", at("2026-11-03"), st), [])
        reset(rows=rows(("2026-11-02", 391500), ("2026-11-03", 389000)), pair=rows(("2026-11-02", 651000), ("2026-11-03", 652000)),
              sec=rows(("2026-11-02", 229000), ("2026-11-03", 219750)))
        st = copy.deepcopy(seed)
        check("11-03 like today (389,000 / 652,000 = 40.34%, SEC 219,750): silent", run("auto", at("2026-11-03"), st), [])
        check("11-03: cursors move, state saved once", (st["last_day"], st["ratio"]["last_day"], st["sec"]["last_day"], len(saved)), ("2026-11-03",) * 3 + (1,))

        # ---- price lines (engine)
        st = copy.deepcopy(seed)
        days = iter(f"2026-11-{i:02d}" for i in range(4, 31))

        def feed(close):
            d = next(days)
            return sorted(e["key"] for e in engine.eval_lines(cfg, st, rows((d, close)), d, at(d)))
        check("360,001 silent", feed(360001), [])
        check("360,000 1차 fires (le)", feed(360000), ["l360"])
        check("355,000 no repeat", feed(355000), [])
        check("363,600 (= 1%) no re-arm", feed(363600), [])
        check("363,601 re-arms (silent)", feed(363601), [])
        check("359,000 1차 fires again", feed(359000), ["l360"])
        check("340,001 silent", feed(340001), [])
        check("340,000 3차 대안 fires", feed(340000), ["l340"])
        check("330,000 silent while disarmed", feed(330000), [])
        check("343,400 no re-arm", feed(343400), [])
        check("343,401 re-arms 3차, 340,000 fires again", (feed(343401), feed(340000))[1], ["l340"])
        st = copy.deepcopy(seed)
        reset(rows=rows(("2026-11-04", 339000)), pair=rows(("2026-11-04", 560000)), sec=rows(("2026-11-04", 220000)))
        s = run("auto", at("2026-11-04"), st)
        check("gap through both: one message, 3차 대안 title", s, [f"{pfx} 종가 340,000원 이하 | 3차 대안 매수 구간"])
        msg = sent[0][2]
        check("gap message names both lines", ("1차 매수선" in msg and "3차 대안 매수선" in msg), True)
        check("gap message: yield and discount shown", ("후행 배당수익률 5.75%" in msg, "괴리율 39.46%" in msg), (True, True))
        check("price message priority 4, cwally-port", (sent[0][3], sent[0][0]), (4, "cwally-port"))
        check("no 400,000 line in config", [ln["level"] for ln in cfg["lines"]], [340000, 360000])
        reset(rows=rows(("2026-11-04", 395000)), pair=rows(("2026-11-04", 650000)), sec=rows(("2026-11-04", 220000)))
        st = copy.deepcopy(seed)
        check("395,000 (under 400,000) silent", run("auto", at("2026-11-04"), st), [])

        # ---- ratio lines (engine)
        st = copy.deepcopy(seed)
        days = iter(f"2026-11-{i:02d}" for i in range(4, 31))
        pref = 390000.0
        pc = lambda v: pref / (1 - v / 100)  # noqa: E731  common close giving discount v

        def rfeed(v):
            d = next(days)
            return sorted(e["key"] for e in engine.eval_ratio(cfg, st, rows((d, pref)), [{"day": d, "close": pc(v)}], d, at(d)))
        check("ratio 25.01% silent", rfeed(25.01), [])
        check("ratio 25.0% 이점 소멸 fires (le)", rfeed(25.0), ["resolve"])
        check("ratio 24% no repeat", rfeed(24.0), [])
        check("ratio 25.9% no re-arm", rfeed(25.9), [])
        check("ratio 26.1% re-arms (silent)", rfeed(26.1), [])
        check("ratio 25.0% fires again", rfeed(25.0), ["resolve"])
        check("ratio 47.99% silent", rfeed(47.99), [])
        check("ratio 48.0% 할인 확대 fires (ge)", rfeed(48.0), ["widen"])
        check("ratio 47.1% no re-arm", rfeed(47.1), [])
        check("ratio 46.9% re-arms (silent)", rfeed(46.9), [])
        check("ratio 49% widen again", rfeed(49.0), ["widen"])
        st = copy.deepcopy(seed)
        reset(rows=rows(("2026-11-04", 390000)), pair=rows(("2026-11-04", 520000)), sec=rows(("2026-11-04", 220000)))
        s = run("auto", at("2026-11-04"), st)
        check("discount 25.0% message title", s, [f"{pfx} 괴리율 25.0% 이하 | 우선주 이점 소멸, 추가 매수 중단"])
        check("discount message: 추가 매수를 멈추세요", "추가 매수를 멈추세요" in sent[0][2], True)
        st = copy.deepcopy(seed)
        ev = engine.eval_ratio(cfg, st, rows(("2026-11-04", 390000)), rows(("2026-11-03", 520000)), "2026-11-04", at("2026-11-04"))
        check("pair missing that day: hold", (ev, st["ratio"]["last_day"]), ([], "2026-11-02"))
        check("ratio intraday not judged", engine.eval_ratio(cfg, copy.deepcopy(seed), rows(("2026-11-04", 300000)), rows(("2026-11-04", 600000)), "2026-11-04", at("2026-11-04", 14)), [])

        # ---- Samsung Electronics withdrawal line
        st = copy.deepcopy(seed)
        days = iter(f"2026-11-{i:02d}" for i in range(4, 31))

        def sfeed(c):
            d = next(days)
            return [e["day"] for e in sec_eval(cfg, st, rows((d, c)), at(d))]
        check("SEC 171,751 silent", sfeed(171751), [])
        check("SEC 171,750 fires (le)", sfeed(171750), ["2026-11-05"])
        check("SEC 160,000 no repeat", sfeed(160000), [])
        check("SEC 173,467 no re-arm (needs > 173,467.5)", sfeed(173467), [])
        check("SEC 173,468 re-arms (silent)", sfeed(173468), [])
        check("SEC 171,000 fires again", sfeed(171000), ["2026-11-09"])
        st = copy.deepcopy(seed)
        check("SEC intraday not judged", sec_eval(cfg, st, rows(("2026-11-04", 150000)), at("2026-11-04", 14)), [])
        check("SEC catch-up in order (11-04 fires, cursor 11-05)",
              ([e["day"] for e in sec_eval(cfg, st, rows(("2026-11-04", 170000), ("2026-11-05", 169000)), at("2026-11-05"))], st["sec"]["last_day"]), (["2026-11-04"], "2026-11-05"))
        st = copy.deepcopy(seed)
        reset(rows=rows(("2026-11-04", 380000)), pair=rows(("2026-11-04", 640000)), sec=rows(("2026-11-04", 171000)))
        s = run("auto", at("2026-11-04"), st)
        check("SEC message title", s, [f"{pfx} 삼성전자 종가 171,750원 이하 | 철회 조건 충족 검토"])
        m = sent[0][2]
        check("SEC message: manual items listed", all(k in m for k in ("K-ICS 250% 미만", "2026년 DPS 가이던스 22,000원 미만", "신계약 CSM 2개 분기 연속 5,500억원 미만")), True)
        check("SEC message: -25.3% vs 229,000, discount shown", ("229,000원 대비 -25.3%" in m, "괴리율 40.62%" in m), (True, True))
        check("SEC message on cwally-port", route_topic("cwally-port", s[0]), "cwally-port")
        check("SEC second run same day silent", run("auto", at("2026-11-04", 22), st), [])
        st = copy.deepcopy(seed)
        reset(rows=rows(("2026-11-04", 380000)), pair=rows(("2026-11-04", 640000)), secfail="005930 정규장 종가 (Yahoo HTTP 503)")
        s = run("auto", at("2026-11-04"), st)
        check("SEC Yahoo down: only the ops fail notice, sec cursor held, price judged", (s, st["sec"]["last_day"], st["last_day"]),
              ([f"{pfx} 조회 실패 | 조건 판단 보류"], "2026-11-02", "2026-11-04"))
        check("fail notice -> cwally-ops", route_topic("cwally-port", s[0]), "cwally-ops")
        check("fail notice once a day", run("auto", at("2026-11-04", 22), st), [])
        world.update(secfail=None, sec=rows(("2026-11-04", 171000), ("2026-11-05", 175000)))
        check("next run catches up the SEC close of 11-04", run("auto", at("2026-11-05"), st), [f"{pfx} 삼성전자 종가 171,750원 이하 | 철회 조건 충족 검토"])
        st = copy.deepcopy(seed)
        reset(fail="000815 정규장 종가 (Yahoo HTTP 503)", sec=rows(("2026-11-04", 220000)))
        s = run("auto", at("2026-11-04"), st)
        check("pref Yahoo down: hold, ops notice, cursors held", (s, st["last_day"], st["ratio"]["last_day"]), ([f"{pfx} 조회 실패 | 조건 판단 보류"], "2026-11-02", "2026-11-02"))

        # ---- notice watch (engine)
        corp = "삼성화재해상보험(주) "
        old = {"no": "1599544", "code": "000810", "title": corp + "현금, 현물 배당 결정", "datetime": "2026-02-13 17:48"}
        ir_old = {"no": "174495922", "code": "000810", "title": corp + "기업설명회(IR) 개최(안내공시)", "datetime": "2026-10-30 06:53"}
        div = {"no": "900001", "code": "000810", "title": corp + "현금, 현물 배당 결정", "datetime": "2027-01-29 16:00"}
        cor = {"no": "900002", "code": "000810", "title": corp + "(정정)현금, 현물 배당 결정", "datetime": "2027-01-29 17:00"}
        sale = {"no": "900003", "code": "000810", "title": corp + "타법인주식및출자증권처분결정(자율공시)", "datetime": "2027-01-30 06:50"}
        pref_n = {"no": "900004", "code": "000815", "title": corp + "우선주 소각 결정", "datetime": "2027-01-30 07:00"}
        other = {"no": "900005", "code": "000810", "title": corp + "주식선물, 주식옵션 2단계 가격제한폭 확대요건 도달(하락)", "datetime": "2027-01-30 07:00"}
        ir4 = {"no": "900006", "code": "000810", "title": corp + "기업설명회(IR) 개최(안내공시)", "datetime": "2027-01-15 06:50"}
        acq = {"no": "900007", "code": "000810", "title": corp + "타법인주식및출자증권취득결정", "datetime": "2027-01-30 07:00"}
        st = copy.deepcopy(seed)
        reset(**base)
        world["notices"] = {"000810": [old, ir_old, div, cor, sale, other, ir4, acq], "000815": [pref_n]}
        s = run("auto", at("2027-02-01"), st)
        check("watch: new filings once each (정정, pre-11/4, unrelated, 취득 silent)", s,
              [f"{pfx} 새 공시 | 기업설명회(IR) 개최(안내공시)", f"{pfx} 새 공시 | 현금, 현물 배당 결정",
               f"{pfx} 새 공시 | 타법인주식및출자증권처분결정(자율공시)", f"{pfx} 새 공시 | 우선주 소각 결정"])
        bodies = {t: m for _, t, m, _ in sent}
        check("watch: 배당 결정 todo names DPS 22,000", "22,000원 미만이면 철회 조건 검토" in bodies[f"{pfx} 새 공시 | 현금, 현물 배당 결정"], True)
        check("watch: 처분 todo names the SEC stake", "86,967,675주" in bodies[f"{pfx} 새 공시 | 타법인주식및출자증권처분결정(자율공시)"], True)
        check("watch: IR todo asks for the Q4 date", "실적 판정 설정에 날짜" in bodies[f"{pfx} 새 공시 | 기업설명회(IR) 개최(안내공시)"], True)
        check("watch: 우선주 todo", "괴리율 변화" in bodies[f"{pfx} 새 공시 | 우선주 소각 결정"], True)
        check("watch: seen ids", sorted(st["notice_seen"]), ["900001", "900003", "900004", "900006"])
        check("watch: repeat silent", run("auto", at("2027-02-02"), st), [])
        st = copy.deepcopy(seed)
        reset(**base)
        world["notices"] = {"000810": RuntimeError("timeout"), "000815": []}
        s = run("auto", at("2026-11-04"), st)
        check("watch: feed down -> ops notice, nothing seen", (s, st["notice_seen"]), ([f"{pfx} 조회 실패 | 조건 판단 보류"], []))

        # ---- end date, holiday, crash guard, dry, install
        reset(**base)
        st = copy.deepcopy(seed)
        reset(rows=rows(("2027-03-31", 330000)), pair=rows(("2027-03-31", 600000)), sec=rows(("2027-03-31", 220000)))
        check("2027-03-31 19:00 still runs (fires)", run("auto", at("2027-03-31"), st)[:1], [f"{pfx} 종가 340,000원 이하 | 3차 대안 매수 구간"])
        calls = {"n": 0}

        def counting(c, now):
            calls["n"] += 1
            return collect(c, now)
        io2 = dict(io, collect=counting)
        sent.clear()
        execute("auto", cfg, copy.deepcopy(seed), at("2027-04-01", 19), io2, pub, save)
        check("after end 2027-03-31 23:59: silent, reads nothing", (sent, calls["n"]), ([], 0))
        sent.clear()
        execute("auto", cfg, copy.deepcopy(seed), at("2027-04-01", 0, 30), io2, pub, save)
        check("00:30 after the end is silent too", (sent, calls["n"]), ([], 0))
        reset(rows=rows(("2026-11-06", 300000)), pair=rows(("2026-11-06", 600000)), sec=rows(("2026-11-06", 100000)))
        check("Saturday (KRX holiday): silent", run("auto", at("2026-11-07"), copy.deepcopy(seed)), [])
        check("Saturday: no state write", saved, [])

        def boom(c, now):
            raise ValueError("boom")
        sent.clear()
        saved.clear()
        execute("auto", cfg, copy.deepcopy(seed), at("2026-11-04"), dict(io, collect=boom), pub, save)
        check("crash guard: one ops notice, no state", ([(tp, t) for tp, t, _, _ in sent], saved), ([(OPS, f"{pfx} 실행 오류 | 조회 실패, 판단 보류")], []))
        reset(rows=rows(("2026-11-04", 350000)), pair=rows(("2026-11-04", 640000)), sec=rows(("2026-11-04", 170000)))
        sent.clear()
        saved.clear()
        execute("dry", cfg, copy.deepcopy(seed), at("2026-11-04"), io, pub, save)
        check("dry: nothing published, no state", (sent, saved), ([], []))
        reset(**base)
        s = run("install", at("2026-11-03", 15), None)
        check("install: one send to cwally-port", [(tp, t) for tp, t, _, _ in sent], [("cwally-port", f"{pfx} 설치 확인 | 매매 신호 아님")])
        im = sent[0][2]
        check("install lists every watch", all(k in im for k in ("360,000원 이하 1차", "340,000원 이하 3차 대안", "25% 이하", "48% 이상", "171,750원 이하", "공시", "2027-03-31 23:59")), True)
        check("install: no state", saved, [])

        # ---- every message: prefix, footer, no forbidden chars
        msgs = []
        reset(rows=rows(("2026-11-04", 335000)), pair=rows(("2026-11-04", 700000)), sec=rows(("2026-11-04", 170000)))
        world["notices"] = {"000810": [div, sale], "000815": [pref_n]}
        st = copy.deepcopy(seed)
        st["notice_done"] = {}
        sent.clear()
        execute("auto", cfg, st, at("2027-02-01"), io, pub, save)
        msgs += [(t, m) for _, t, m, _ in sent]
        reset(fail="x", sec=rows(("2026-11-04", 220000)))
        sent.clear()
        execute("auto", cfg, copy.deepcopy(seed), at("2026-11-04"), io, pub, save)
        msgs += [(t, m) for _, t, m, _ in sent]
        msgs.append(install_msg(cfg, {"rows": [], "pair_rows": []}, [], [], "x"))
        msgs.append(crash_msg(cfg, ValueError("x"), "x"))
        check("message count covers price, ratio, SEC, notices, fail, install, crash", len(msgs) >= 9, True)
        check("every title starts with [000815]", [t for t, _ in msgs if not t.startswith(pfx + " ")], [])
        check("no · — ㆍ in any title or body", [t for t, m in msgs if any(ch in t + m for ch in engine.FORBIDDEN)], [])
        check("footer on every alert message (not crash)", [t for t, m in msgs[:-1] if m.split("\n")[-1] != foot], [])
        check("footer text has no middle dot", any(ch in foot for ch in engine.FORBIDDEN), False)
        order = ["이게 뭐 알림:", "지금 결과/상태:", "지금 할 일:", "나중에:", "익절/줄일 때:", "하지 말 것:"]
        check("six-item body order", [t for t, m in msgs if [x.split(":")[0] + ":" for x in m.split("\n")[1:7]] != order], [])
        for t, _ in msgs:
            print("SAMPLE:", t)
    after = sp.read_bytes() if sp.exists() else None
    check("state/000815.json untouched", after == before, True)
    print(f"SELFTEST {'OK' if not n['fail'] else 'FAIL'}: {n['ok']} passed, {n['fail']} failed (fixtures only, no network, no publish, no state)")
    return 0 if not n["fail"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
