#!/usr/bin/env python3
"""[XBI] SPDR S&P 바이오텍 ETF 분할 매수 구간 점검. 평일 09:00 KST morning 묶음 (lrcx 옆).

가격: 미국 전일 종가(Yahoo 일봉). 데이터 거래일(state.last_date)로 중복 방지.
구간(configs/xbi.json zones): 들어오면 1번 알림(p3), 경계 밖으로 rearm_pct(4%) 넘게 벗어난 뒤 다시 들어오면 다시 알림.
추격 금지선(nochase) 이상이면 신규 매수 보류 알림. 조회 실패는 cwally-ops 하루 1번.
점검 선(configs lines, 2026-11-04 추가, 모두 p3, 상태가 바뀔 때만): XBI 종가 >= 169.89 (52주 고점, 164.80 미만 재무장),
XBI 종가 <= 125 (3차 구간 이탈, 128.75 이상 재무장, 매수 문구 없음), ^TNX 종가 5거래일 연속 <= 4.50 (4.64 이상 재무장),
^TNX 종가 >= 5.20 (5.04 미만 재무장). 169.89 와 추격 금지 165 가 같은 날이면 한 건. state["lines"] 가 없으면 조용히 시드.
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
import ntfy_util  # noqa: E402
from ntfy_util import publish, record_fired, resolve_server, resolve_topic, route_topic  # noqa: E402

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


def tnx_text(rows: list | None) -> str:
    """1차 body: latest completed ^TNX close, change vs 5 sessions ago, position vs the plan lines 4.50 / 5.20."""
    if not rows:
        return "10년물 조회 실패."
    d, v = rows[-1]
    ch = f", 5거래일 전({rows[-6][0]} {rows[-6][1]:.2f}%) 대비 {(v - rows[-6][1]) * 100:+.0f}bp" if len(rows) >= 6 else ""
    if v <= 4.5:
        pos = "4.50% 이하(긍정 조건 쪽, 5거래일 연속이면 판단 긍정 전환 조건 점검)"
    elif v >= 5.2:
        pos = "5.20% 이상(주의, 구간 한 단계 낮추는 검토 조건)"
    else:
        pos = f"4.50%(긍정 조건)과 5.20%(주의, 구간 한 단계 낮추는 검토) 사이, 4.50%까지 {(v - 4.5) * 100:.0f}bp, 5.20%까지 {(5.2 - v) * 100:.0f}bp"
    return f"10년물(^TNX) {d} {v:.2f}%{ch}. 위치: {pos}."


def fomc_text(cfg: dict, now: datetime) -> str:
    f = cfg.get("fomc") or {}
    if not f:
        return "FOMC 정보 없음."
    m = f["meeting"][5:].replace("-", "/").lstrip("0")
    if now >= datetime.strptime(f["next_decision_kst"], "%Y-%m-%d %H:%M").replace(tzinfo=KST):
        return f"FOMC 정보 갱신 필요 (마지막 반영 {m})."
    return f"최근 FOMC({f['meeting']}): {f['decision']}, 표결 {f['vote']}, 반대 {f['dissent']}. 다음 회의 {f['next_meeting']}."


def zone_msg(cfg: dict, z: dict, day: str, p: float, stamp: str, extra: str = "") -> tuple[str, str, int]:
    dd = (p / cfg["high_52w"] - 1) * 100
    k = z["key"]
    if k == "nochase":
        title = f"[XBI] 추격 금지선 돌파 | 종가 ${p:.2f} > ${z['lo']:g}, 신규 매수 보류"
        action = "신규 매수 보류. 이미 산 물량은 이 알림으로 정하지 않기."
    else:
        n = {"z1": "1", "z2": "2", "z3": "3"}[k]
        title = f"[XBI] {n}차 분할 매수 구간 진입 | 종가 ${p:.2f} ({z['lo']:g}~{z['hi']:g}, 고점 대비 {dd:.1f}%)"
        action = f"계획한 {n}차 {z['budget_manwon']}만원만 검토. 한 번에 다 사지 않기. 해외 계좌 기준 (ISA 는 XBI 불가)."
    status = f"{day} 미국 종가 ${p:.2f}, 52주 고점 ${cfg['high_52w']:g} 대비 {dd:.1f}%. 구간: {zone_table(cfg)}." + (f" {extra}" if extra else "")
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


OPS = {"le": lambda v, x: v <= x, "lt": lambda v, x: v < x, "ge": lambda v, x: v >= x}
CT = ZoneInfo("America/Chicago")


def tnx_rows(url: str, now: datetime) -> list[tuple[str, float]]:
    """^TNX completed daily closes (Chicago 15:00 or later for today's bar)."""
    d = json.loads(fetch(url, WEB_UA))
    res = d["chart"]["result"][0]
    loc = now.astimezone(CT)
    out = {}
    for t, c in zip(res.get("timestamp") or [], res["indicators"]["quote"][0].get("close") or []):
        if c is None:
            continue
        day = datetime.fromtimestamp(t, CT).date()
        if day < loc.date() or (day == loc.date() and loc.hour >= 15):
            out[day.isoformat()] = round(float(c), 3)
    return sorted(out.items())


def line_hit(ln: dict, closes: list[float]) -> bool:
    n = int(ln.get("days", 1))
    return len(closes) >= n and all(OPS[ln["op"]](c, ln["level"]) for c in closes[-n:])


def judge_line(ln: dict, lst: dict, closes: list[float]) -> bool:
    s = lst.setdefault(ln["key"], {"armed": not line_hit(ln, closes)})
    if s["armed"] and line_hit(ln, closes):
        s["armed"] = False
        return True
    if not s["armed"] and OPS[ln["rearm_op"]](closes[-1], ln["rearm"]):
        s["armed"] = True
    return False


LINE_TEXT = {
    "hi52": ("8/25 52주 고점 $169.89 를 종가로 넘었습니다. 추격 금지 유지, 신규 매수 보류. 재평가 점검: 대형 M&A, FDA 승인 흐름, 10년물 수준, 랠리 뒤 밸류에이션.",
             "고점 돌파만 보고 추격, 남은 금액 한꺼번에 넣기."),
    "below3": ("3차 구간(131~137) 아래로 내려왔습니다. 추가 매수 보류, 원인 확인 먼저: 10년물 급등, 임상 실패 집중, 정책(약가, MFN) 충격 중 무엇인지. 계획 자체를 다시 볼지 판단.",
               "원인 확인 없이 남은 금액 넣기, 하락을 싸졌다는 신호로 읽기, 신용 및 미수."),
    "tnx_low5": ("분석의 판단 긍정 전환 조건(10년물 4.5% 이하 안정)에 해당. 점검: XBI 반응, Fed 다음 회의 전망, 바이오 M&A 흐름. 구간 조정은 사용자 승인 뒤.",
                 "금리 하나만 보고 구간을 바로 바꾸기."),
    "tnx_hi": ("10년물이 5.20% 이상. 계획의 변경 조건(5.2% 초과면 구간 한 단계 낮추는 검토)에 해당. 점검: Fed 인상 가능성, XBI 낙폭. 구간 조정은 사용자 승인 뒤.",
               "금리 급등 중 남은 금액 한꺼번에 넣기."),
}


def line_msg(cfg: dict, fired: list[dict], vals: dict, stamp: str, nochase: bool = False) -> tuple[str, str, int]:
    fmtv = {"xbi": lambda v: f"${v:.2f}", "tnx": lambda v: f"{v:.2f}%"}
    head = " + ".join(ln["title"] for ln in fired)
    tail = f"종가 ${vals['xbi'][1]:.2f}" if any(ln["series"] == "xbi" for ln in fired) and vals.get("xbi") else f"10년물 {vals['tnx'][1]:.2f}%"
    title = f"[XBI] {head} | {tail}"
    hits = " ".join(f"{ln['title']}: {vals[ln['series']][0]} {fmtv[ln['series']](vals[ln['series']][1])}"
                    f"{' (5거래일 연속)' if ln.get('days') else ''}, 기준 {fmtv[ln['series']](ln['level'])}." for ln in fired)
    now = ", ".join(f"{'XBI' if k == 'xbi' else '10년물'} {v[0]} {fmtv[k](v[1])}" for k, v in vals.items() if v)
    nc = " 같은 날 추격 금지선($165 이상)에도 들어와 한 건으로 합쳤습니다." if nochase else ""
    msg = body(stamp, "XBI 분할 매수 계획에 붙인 점검 선 알림(상태가 바뀔 때만). 매매 지시가 아닙니다.",
               f"{hits} 최근 값: {now}.{nc} 구간: {zone_table(cfg)}.",
               " ".join(LINE_TEXT[ln["key"]][0] for ln in fired),
               "같은 선은 재무장 뒤에만 다시 옵니다(169.89 선은 $164.80 미만, 125 선은 $128.75 이상, 4.50% 선은 4.64% 이상, 5.20% 선은 5.04% 미만).",
               "점검 선 알림만으로 비중을 줄이지 않기. 해외 양도세 22%(연 250만원 공제)와 환율을 같이 보기.",
               " ".join(LINE_TEXT[ln["key"]][1] for ln in fired))
    return title, msg, max(ln["priority"] for ln in fired)


FIRED_MAX = 60  # state/xbi.json fired 최근 건수 (기록은 ntfy_util.record_fired, 실제 발송 성공 때만)


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
    meta: dict[int, tuple[str, str]] = {}  # out index -> (판정 종가 날짜, 조건 키); ops 안내는 없음
    pend_lines: list[dict] = []
    tnx_res: list | None = None
    tnx_err = ""
    if cfg.get("lines") or cfg.get("fomc"):
        try:
            tnx_res = tnx_rows(cfg["sources"]["tnx"], now) or None
            if not tnx_res:
                tnx_err = "확정 종가 없음"
        except Exception as exc:  # noqa: BLE001
            tnx_err = str(exc)[:150]
    nochase_merged = False
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
        lst = st.setdefault("lines", {})
        xl = [ln for ln in cfg.get("lines") or [] if ln["series"] == "xbi"]
        for ln in xl:
            if ln["key"] not in lst:  # migrate old state: seed silently at the last close
                lst[ln["key"]] = {"armed": not line_hit(ln, [p])}
                print(f"seeded line {ln['key']} at {day} ${p}: {lst[ln['key']]}")
        if day != st.get("last_date"):
            zf = judge_zones(cfg["zones"], st["zones"], p, cfg["rearm_pct"])
            fired = [ln for ln in xl if judge_line(ln, lst, [p])]
            merge = any(ln["key"] == "hi52" for ln in fired) and any(z["key"] == "nochase" for z in zf)
            for z in zf:
                if not (merge and z["key"] == "nochase"):
                    extra = f"{tnx_text(tnx_res)} {fomc_text(cfg, now)}" if z["key"] == "z1" else ""
                    meta[len(out)] = (day, z["key"])
                    out.append(zone_msg(cfg, z, day, p, stamp, extra))
            pend_lines.extend(fired)
            nochase_merged = merge
            st["last_date"] = day
        st["last_close"] = [day, p]
        print(f"close {day} ${p:.2f} -> {zone_where(cfg['zones'], p)}, armed={ {k: v['armed'] for k, v in st['zones'].items()} }")
    elif st.get("quote_fail") != stamp[:10]:
        out.append(ops_msg(stamp, "종가 조회 실패 | 구간 판정 못 함", f"Yahoo 일봉 {NA} ({qerr})."))
        st["quote_fail"] = stamp[:10]
    tl = [ln for ln in cfg.get("lines") or [] if ln["series"] == "tnx"]
    if tl:
        rows = tnx_res
        if not rows:
            if st.get("tnx_fail") != stamp[:10]:
                out.append(ops_msg(stamp, "10년물 조회 실패 | 금리 선 판정 못 함", f"Yahoo ^TNX {NA} ({tnx_err})."))
                st["tnx_fail"] = stamp[:10]
        else:
            lst = st.setdefault("lines", {})
            closes = [c for _, c in rows]
            seed = any(ln["key"] not in lst for ln in tl) or not st.get("tnx_last_date")
            if seed:
                for ln in tl:
                    lst[ln["key"]] = {"armed": not line_hit(ln, closes)}
                print(f"seeded 10Y lines at {rows[-1]}: { {ln['key']: lst[ln['key']] for ln in tl} }")
            else:
                for i, (d, _) in enumerate(rows):
                    if d > st["tnx_last_date"]:
                        for ln in tl:
                            if judge_line(ln, lst, closes[: i + 1]) and ln not in pend_lines:
                                pend_lines.append(ln)
            st["tnx_last_date"] = rows[-1][0]
            st["tnx_last"] = list(rows[-1])
            print(f"10Y last {rows[-5:]}")
    if pend_lines:
        src = st.get("last_close") if any(ln["series"] == "xbi" for ln in pend_lines) and st.get("last_close") else st.get("tnx_last")
        meta[len(out)] = ((src or [stamp[:10]])[0], "+".join(ln["key"] for ln in pend_lines))
        out.append(line_msg(cfg, pend_lines, {"xbi": st.get("last_close"), "tnx": st.get("tnx_last")}, stamp, nochase_merged))
    topic, server = resolve_topic("port", cfg.get("ntfy_topic")), resolve_server(cfg.get("ntfy_server"))
    for i, (t, m, p) in enumerate(out):
        assert "\u00b7" not in t + m and "\u2014" not in t + m, t
        print(f"TITLE: {t}\nPRIORITY: {p} TOPIC: {route_topic(topic, t)}\n{m}\n")
        if not dry:
            publish(topic, t, m, p, ["dna"], server)
            if i in meta:
                record_fired(st, meta[i][0], meta[i][1], p, t, now, cap=FIRED_MAX)
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
    # --- 2026-11-04 점검 선: migrate old state, XBI and 10Y lines, merge with nochase
    import tempfile
    g = globals()
    keep = {k: g[k] for k in ("STATE_PATH", "last_close", "tnx_rows", "publish")}
    sent: list = []
    fx: dict = {}
    try:
        g["STATE_PATH"] = Path(tempfile.mkdtemp()) / "xbi.json"
        def fake_pub(topic, t, m, p, tags=None, server=None, **k):
            sent.append((route_topic(topic, t), t, m, p))
            ntfy_util.LAST_PUBLISH.clear()
            ntfy_util.LAST_PUBLISH.update({"sent": True, "reason": "", "title": t})
        g["publish"] = fake_pub
        g["last_close"] = lambda url, now: fx["x"]
        g["tnx_rows"] = lambda url, now: fx["t"]() if callable(fx["t"]) else fx["t"]
        old = {"zones": {"nochase": {"armed": True}, "z1": {"armed": True}, "z2": {"armed": True}, "z3": {"armed": True}},
               "last_date": "2026-11-03", "last_close": ["2026-11-03", 154.97]}
        g["STATE_PATH"].write_text(json.dumps(old), encoding="utf-8")
        T = [("2026-10-28", 5.045), ("2026-10-29", 4.989), ("2026-10-30", 4.929), ("2026-11-02", 4.894), ("2026-11-03", 4.952)]
        now = datetime(2026, 11, 4, 9, 0, tzinfo=KST)

        def go(x, t, when=None):
            sent.clear()
            fx["x"], fx["t"] = x, t
            run("auto", when or now, cfg)
            return [(s[1], s[3]) for s in sent]
        assert go(("2026-11-03", 154.97), list(T)) == [], sent
        st = json.loads(g["STATE_PATH"].read_text(encoding="utf-8"))
        assert st["zones"] == old["zones"] and all(st["lines"][k]["armed"] for k in ("hi52", "below3", "tnx_low5", "tnx_hi")), st
        print("ok old state migrated: zones kept, 4 lines seeded armed, silent")
        r = go(("2026-11-04", 170.5), T + [("2026-11-04", 4.9)])
        assert len(r) == 1 and r[0][0] == "[XBI] 52주 고점 돌파, 재평가 점검 (추격 금지 유지) | 종가 $170.50" and r[0][1] == 3, r
        m = sent[0][2]
        assert "한 건으로 합쳤습니다" in m and "추격 금지 유지, 신규 매수 보류" in m, m
        print("ok 169.89 + nochase same day -> one merged message")
        assert go(("2026-11-05", 171.0), T + [("2026-11-04", 4.9), ("2026-11-05", 4.9)]) == []
        assert go(("2026-11-06", 164.7), T + [("2026-11-04", 4.9)]) == []
        r = go(("2026-11-09", 170.0), T + [("2026-11-04", 4.9)])
        assert [x[0][:20] for x in r] == ["[XBI] 52주 고점 돌파, 재평가"], r
        print("ok hi52 re-arm below 164.80 (nochase still fired: no second nochase)")
        r = go(("2026-11-10", 124.9), T + [("2026-11-04", 4.9)])
        titles = [x[0] for x in r]
        assert "[XBI] 3차 구간 이탈, 원인 점검 (추가 매수 보류) | 종가 $124.90" in titles, titles
        m = next(s[2] for s in sent if s[1].startswith("[XBI] 3차 구간 이탈"))
        assert "추가 매수 보류" in m and not any(w in m for w in ("만원만 검토", "매수 기회", "저가 매수", "분할 매수 구간 진입")), m
        assert go(("2026-11-11", 128.7), T + [("2026-11-04", 4.9)]) == [] and go(("2026-11-12", 124.0), T + [("2026-11-04", 4.9)]) == []
        go(("2026-11-13", 128.75), T + [("2026-11-04", 4.9)])
        r = go(("2026-11-16", 125.0), T + [("2026-11-04", 4.9)])
        assert [x[0].startswith("[XBI] 3차 구간 이탈") for x in r] == [True], r
        print("ok below3 fires, no buy wording, re-arm only at >= 128.75")
        base = T + [("2026-11-04", 4.9)]
        low = [("2026-11-%02d" % d, v) for d, v in ((17, 4.5), (18, 4.49), (19, 4.48), (20, 4.47))]
        assert go(("2026-11-16", 125.0), base + low) == []
        r = go(("2026-11-16", 125.0), base + low + [("2026-11-23", 4.46)])
        assert [x[0] for x in r] == ["[XBI] 10년물 4.50% 이하 5거래일, 판단 긍정 전환 조건 점검 | 10년물 4.46%"], r
        assert go(("2026-11-16", 125.0), base + low + [("2026-11-23", 4.46), ("2026-11-24", 4.63)]) == []
        r = go(("2026-11-16", 125.0), base + low + [("2026-11-23", 4.46), ("2026-11-24", 4.63), ("2026-11-25", 5.21)])
        assert [x[0] for x in r] == ["[XBI] 10년물 5.20% 이상, 금리 부담 점검 | 10년물 5.21%"], r
        assert go(("2026-11-16", 125.0), base + low + [("2026-11-23", 4.46), ("2026-11-24", 4.63), ("2026-11-25", 5.21), ("2026-11-26", 5.05), ("2026-11-27", 5.3)]) == []
        print("ok 10Y 5-session low and 5.20 high with re-arm buffers")
        r = go(("2026-11-30", 150.0), lambda: (_ for _ in ()).throw(OSError("HTTP 500")))
        assert [(s[0], s[1]) for s in sent][-1] == ("cwally-ops", "[XBI] 10년물 조회 실패 | 금리 선 판정 못 함") and any("1차" in t for t, _ in r), sent
        print("ok 10Y fetch fail -> one ops notice, XBI zones still judged")
        for _, t, m, _ in sent:
            assert t.startswith("[XBI]") and "\u00b7" not in t + m and "\u2014" not in t + m
        # --- 1차 body: 10Y and FOMC (normal, fetch fail, stale)
        T6 = [("2026-10-27", 4.993)] + T
        go(("2026-11-29", 157.5), T6)  # z1 fired 11/30 above: re-arm first
        r = go(("2026-12-01", 149.0), T6)
        m = next(s[2] for s in sent if s[1].startswith("[XBI] 1차"))
        assert "10년물(^TNX) 2026-11-03 4.95%, 5거래일 전(2026-10-27 4.99%) 대비 -4bp" in m, m
        assert "4.50%(긍정 조건)과 5.20%(주의, 구간 한 단계 낮추는 검토) 사이" in m and "표결 10 대 2, 반대 Cook, Hammack 2명 25bp 인상 주장" in m, m
        assert "계획한 1차 300만원만 검토" in m, m
        print("ok 1차 body: 10Y level, 5-session change, position, FOMC dissent")
        go(("2026-12-02", 157.5), T6)
        r = go(("2026-12-03", 149.5), lambda: (_ for _ in ()).throw(OSError("HTTP 500")))
        m = next(s[2] for s in sent if s[1].startswith("[XBI] 1차"))
        assert "10년물 조회 실패." in m, m  # ops notice already sent once today in the earlier fail case
        print("ok 10Y fetch fail -> 1차 still sent with '10년물 조회 실패'")
        go(("2026-12-04", 157.5), T6)
        go(("2026-12-10", 149.5), T6, datetime(2026, 12, 10, 9, 0, tzinfo=KST))
        m = next(s[2] for s in sent if s[1].startswith("[XBI] 1차"))
        assert "FOMC 정보 갱신 필요 (마지막 반영 10/28)." in m and "Cook" not in m, m
        print("ok stale FOMC after 12/10 04:00 KST -> 갱신 필요, no stale dissent")
        go(("2026-12-11", 157.5), T6)
        go(("2026-12-14", 143.0), T6)
        m = next(s[2] for s in sent if s[1].startswith("[XBI] 2차"))
        assert "Cook" not in m and "10년물(^TNX)" not in m, m
        print("ok 2차 body unchanged")
        fh = json.loads(g["STATE_PATH"].read_text(encoding="utf-8")).get("fired") or []
        sig = [x for x in sent if x[0] == "cwally-port"]
        assert fh and fh[-1]["title"] == sig[-1][1] and fh[-1]["date"] == "2026-12-14" and fh[-1]["key"] == "z2", fh[-3:]
        assert not any("조회 실패" in x["title"] for x in fh), fh
        assert {"z1", "z2"} <= {x["key"] for x in fh} and any(k in x["key"] for x in fh for k in ("hi52", "below3", "tnx_low5", "tnx_hi")), fh
        print(f"ok fired history in auto sim: {len(fh)} sends recorded, ops notices not recorded, keys {sorted({x['key'] for x in fh})}")
    finally:
        g.update(keep)
    fst: dict = {}
    ntfy_util.LAST_PUBLISH.clear()
    ntfy_util.LAST_PUBLISH.update({"sent": False, "reason": "ends_kst passed", "title": "[XBI] t"})
    assert not record_fired(fst, "2026-11-04", "z1", 3, "[XBI] t", cap=FIRED_MAX) and "fired" not in fst
    ntfy_util.LAST_PUBLISH.update({"sent": True, "reason": ""})
    assert record_fired(fst, "2026-11-04", "z1", 3, "[XBI] t", datetime(2026, 11, 5, 9, tzinfo=KST), cap=FIRED_MAX)
    assert fst["fired"][0] == {"date": "2026-11-04", "key": "z1", "priority": 3, "title": "[XBI] t", "sent_at": "2026-11-05T09:00:00+09:00"}
    for k in range(70):
        record_fired(fst, f"d{k}", "hi52+tnx_hi", 3, "[XBI] x", cap=FIRED_MAX)
    assert len(fst["fired"]) == FIRED_MAX and fst["fired"][-1]["key"] == "hi52+tnx_hi"
    ntfy_util.LAST_PUBLISH.clear()
    print("fired history ok (최근 60건)")
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
