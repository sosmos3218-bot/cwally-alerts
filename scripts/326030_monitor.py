#!/usr/bin/env python3
"""[326030] SK바이오팜 watch alert on the k203780 19:00 KST slot (approved in chat 2026-11-03, t37u).

Three parts, one run, one state file (state/326030.json), topic cwally-port, failures to cwally-ops:
  lines    configs/326030.json "lines" through the 005930 engine (scripts/005930_monitor.py): KRX regular close
           (Yahoo 326030.KS) <= 68,000 / >= 83,400 / >= 91,600, each fires once, re-arms after the close moves more
           than 1% back. Watch-only lines (holding unknown). Hold on missing data: no close, no judgment, the session
           stays unprocessed and the next run catches it up; one 조회 실패 notice a day to cwally-ops.
  notices  "notice_watch" through the same engine: new Naver notice titles with 투자판단 관련 주요경영사항, Opakalim,
           오파칼림, Clinical Hold, 임상, 라이선스 since 2026-11-03, one alert per filing number ("결과 공시 나옴").
  earn_q3  this file: 2026 Q3 연결재무제표기준영업(잠정)실적 notice (정정 skipped, on/after earn_q3.since), parsed for
           revenue and OP; fallback Naver finance/quarter 202609 only when isConsensus is not Y. Verdict by OP vs the
           frozen consensus 950억: >= 998 상회, <= 902 하회, else 부합 (revenue vs 2,546억, +-3% band, not decisive).
           YoY vs Q3 2025 1,917억 and the discovery-brief +15% (2,205억) exit line, Japan milestone warning, Xcopri US
           sales best effort from the skbp.com press release (else 수동 확인). Sent once in session days 11/5..11/12;
           nothing by 11/12 -> one 원문 없음 notice (catch-up 3 days). Feed failure -> hold, ops notice.
Ends 2027-01-31 23:59 KST (configs ends_kst, extended 2026-11-04 for the PGTC sNDA January checks; earn_q3 stays 2026-only by its dates): after that the run is silent and reads nothing.
Crash guard: any exception in auto/force sends one ops notice and exits 0 so the rest of the 19:00 bundle runs.
Modes: auto (slot) | force (ignore holiday) | dry (live data, print, no ntfy, no state) | selftest (offline) |
       install (one install-confirmation send to cwally-port, no state).
"""
from __future__ import annotations

import copy
import html
import importlib.util
import re
import sys
import traceback
import urllib.request
from datetime import date, datetime
from pathlib import Path

_spec = importlib.util.spec_from_file_location("krplan_005930", Path(__file__).resolve().parent / "005930_monitor.py")
engine = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(engine)

sys.path.insert(0, str(Path(__file__).resolve().parent))
import naver_api  # noqa: E402
from kr_calendar import is_trading_day, session_day  # noqa: E402
from ntfy_util import publish, resolve_server, resolve_topic  # noqa: E402

NAME = "326030"
KST = engine.KST
OPS = "cwally-ops"
UA = {"User-Agent": "Mozilla/5.0 cwally-alerts 326030"}
UNIT_TO_EOK = {"백만원": 100.0, "억원": 1.0, "천원": 100000.0, "조원": 0.0001}


# ---------- fetch (all injectable through io for the offline selftest) ----------
def http_text(url: str) -> str:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def live_io() -> dict:
    return {
        "collect": engine.collect,                                       # (cfg, now) -> (data, fails), Yahoo regular closes
        "notice_rows": None,                                             # engine default (titles only)
        "ref": None,                                                     # engine default NXT reference line
        "notices": lambda cfg: naver_api.notices(cfg["code"], 60),       # titles + contents
        "quarter": lambda cfg: naver_api.get_json(cfg["sources"]["naver_quarter"].format(code=cfg["code"])),
        "http": http_text,
    }


# ---------- Q3 result ----------
def _tokens(seg: str) -> list:
    return re.findall(r"-?[\d,]+(?:\.\d+)?|-", seg)


def _num(tok) -> float | None:
    if tok is None or tok == "-":
        return None
    try:
        return float(str(tok).replace(",", ""))
    except ValueError:
        return None


def parse_q3(text: str, period_end: str) -> dict | None:
    """{rev, op, rev_yoy_base, op_yoy_base} in 억원 from a 연결 영업(잠정)실적 notice body, else None."""
    t = re.sub(r"\s+", " ", text or "")
    m = re.search(r"당기실적\s*(\d{4}-\d{2}-\d{2})\s*~\s*(\d{4}-\d{2}-\d{2})", t)
    if not m or m.group(2) != period_end:
        return None
    um = re.search(r"단위\s*[:：]\s*(조원|억원|백만원|천원)", t)
    if not um:
        return None
    div = UNIT_TO_EOK[um.group(1)]

    def row(name):
        rm = re.search(name + r"\s*당해실적\s*(.*?)누계실적", t)
        if not rm:
            return None, None
        tk = _tokens(rm.group(1))
        cur = _num(tk[0]) if tk else None
        yoy = _num(tk[4]) if len(tk) > 4 else None
        return (None if cur is None else round(cur / div)), (None if yoy is None else round(yoy / div))

    rev, rev_y = row("매출액")
    op, op_y = row("영업이익")
    if rev is None or op is None:
        return None
    return {"rev": rev, "op": op, "rev_yoy_base": rev_y, "op_yoy_base": op_y}


def find_q3(cfg: dict, io: dict) -> tuple[dict | None, str, list[str], list[str]]:
    """(result, source, fails, notes). fails = notice feed failure or an unparsable result notice (hold + ops)."""
    e = cfg["earn_q3"]
    fails, notes = [], []
    try:
        for r in io["notices"](cfg):
            title = str(r.get("title") or "")
            if e["title_key"] not in title or "정정" in title or str(r.get("datetime") or "")[:10] < e["since"]:
                continue
            got = parse_q3(r.get("text") or "", e["period_end"])
            if got:
                got.update(title=engine.scrub(title), datetime=str(r.get("datetime") or "")[:16].replace("T", " "),
                           link=cfg["sources"]["notice_page"].format(code=cfg["code"]))
                return got, "notice", fails, notes
            fails.append(f"{cfg['code']} 3분기 실적 공시 파싱 실패 ({engine.scrub(title)[:40]}; 판단 보류, 다음 실행에서 다시 확인)")
            return None, "none", fails, notes
    except Exception as exc:
        fails.append(f"{cfg['code']} 공시 피드 (네이버 {str(exc)[:60]}; 실적 판단 보류, 다음 실행에서 다시 확인)")
    try:
        key = e["period_end"][:4] + e["period_end"][5:7]
        d = io["quarter"](cfg)["financeInfo"]
        cons = {t["key"]: t.get("isConsensus") == "Y" for t in d["trTitleList"]}
        if key in cons and not cons[key]:
            vals = {}
            for row in d["rowList"]:
                if row["title"] in ("매출액", "영업이익"):
                    vals[row["title"]] = _num((row["columns"].get(key) or {}).get("value"))
            if vals.get("매출액") is not None and vals.get("영업이익") is not None:
                return ({"rev": round(vals["매출액"]), "op": round(vals["영업이익"]), "rev_yoy_base": None, "op_yoy_base": None,
                         "title": "네이버 분기 재무(공시 원문 아님)", "datetime": None,
                         "link": cfg["sources"]["notice_page"].format(code=cfg["code"])}, "naver_quarter", fails, notes)
    except Exception as exc:
        notes.append(f"quarter fallback skipped: {exc}")
    return None, "none", fails, notes


def xcopri_us(cfg: dict, io: dict) -> tuple[int | None, str]:
    """Best effort: Xcopri US sales (억원) from the skbp.com Q3 press release. (value, url or reason)."""
    since = cfg["earn_q3"]["since"].replace("-", ".")
    try:
        page = io["http"](cfg["sources"]["skbp_list"])
        for m in re.finditer(r'data-seq="(\d+)"(.*?)</li>', page, re.S):
            text = html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", m.group(2))))
            dm = re.search(r"(\d{4}\.\d{2}\.\d{2})", text)
            if not dm or dm.group(1) < since or "3분기" not in text or not any(k in text for k in ("매출", "영업이익", "실적")):
                continue
            url = cfg["sources"]["skbp_view"].format(seq=m.group(1))
            body = html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", io["http"](url)))).replace("\xa0", " ")
            vm = re.search(r"XCOPRI\s*®?\s*US\s*매출\s*([\d,]+)", body) or re.search(r"미국 매출은\s*([\d,]+)\s*억원", body)
            if vm:
                return int(vm.group(1).replace(",", "")), url
            return None, "보도자료에서 숫자를 못 찾음"
        return None, "3분기 보도자료 아직 없음"
    except Exception as exc:
        return None, f"보도자료 조회 실패 {str(exc)[:40]}"


def pct(a: float, b: float) -> float:
    return (a / b - 1.0) * 100.0


def verdict(e: dict, op: int) -> str:
    return "상회" if op >= e["op_beat"] else "하회" if op <= e["op_miss"] else "부합"


def build_q3(cfg: dict, res: dict, src: str, xc: tuple, stamp: str, close: float | None = None) -> tuple[str, str, int]:
    e = cfg["earn_q3"]
    c = e["consensus"]
    v = verdict(e, res["op"])
    dop, drev = pct(res["op"], c["op"]), pct(res["rev"], c["rev"])
    band = e["rev_band_pct"]
    rv = "부합 범위" if abs(drev) <= band else ("위" if drev > 0 else "아래")
    yoy = pct(res["rev"], e["yoy_base_rev"])
    opm = res["op"] / res["rev"] * 100 if res["rev"] else 0.0
    refs = ", ".join(f"{r['name']} 매출 {r['rev']:,}억, 영업이익 {r['op']:,}억" for r in e["refs"])
    exit_ok = res["rev"] >= e["exit_rev"]
    exit_line = (f"발굴 브리프 제외 기준(매출 +{e['exit_growth_pct']:.0f}%, {e['exit_rev']:,}억): "
                 + ("통과, 보유 논리 유지 조건 충족." if exit_ok else "미달, 보유 논리 재점검 조건에 해당."))
    xv, xinfo = xc
    xline = (f"Xcopri 미국 매출 {xv:,}억(회사 보도자료, 2분기 {e['q2_xcopri_eok']:,}억 대비 {pct(xv, e['q2_xcopri_eok']):+.1f}%): {xinfo}"
             if xv else f"Xcopri 미국 매출: 자동 확인 안 됨({xinfo}), 수동 확인. 보도자료 {cfg['sources']['skbp_list']}")
    srcl = (f"출처: {res['title']} ({res['datetime']}), 공시 목록 {res['link']}" if src == "notice"
            else f"출처: {res['title']}, 공시 원문 확인 필요 {res['link']}")
    title = f"{cfg['title_prefix']} 3분기 영업익 {res['op']:,}억 컨센 대비 {dop:+.0f}% | {v}, 마일스톤 포함"
    todo = {
        "상회": "마일스톤을 빼고도 영업이익이 컨센서스 위인지, Xcopri 미국 매출 성장이 이어졌는지 컨퍼런스콜 자료로 확인하세요.",
        "부합": "Xcopri 미국 매출 성장과 비용(오파칼림 관련 지급수수료) 흐름을 컨퍼런스콜 자료로 확인하세요.",
        "하회": "마일스톤이 들어 있는데도 하회라 비용 증가나 Xcopri 둔화가 원인인지 먼저 확인하세요.",
    }[v]
    msg = "\n".join([
        engine.head(cfg, stamp),
        f"이게 뭐 알림: {cfg['short']} {e['period']} 잠정실적을 발표 전 고정한 컨센서스와 비교한 1회 알림입니다. 판정은 영업이익 기준입니다. 매수 지시가 아닙니다.",
        f"지금 결과/상태: 판정 {v}. 영업이익 {res['op']:,}억(컨센서스 {c['op']:,}억 대비 {dop:+.1f}%, 상회 {e['op_beat']:,}억 이상, 하회 {e['op_miss']:,}억 이하), "
        f"매출 {res['rev']:,}억(컨센서스 {c['rev']:,}억 대비 {drev:+.1f}%, +-{band:.0f}% 기준 {rv}), 영업이익률 {opm:.1f}%. "
        f"매출 전년 동기({e['yoy_base_rev']:,}억) 대비 {yoy:+.1f}%. {exit_line} {xline}. 참고 추정: {refs}. 컨센서스 출처: {c['source']}. "
        f"주의: {e['milestone_note']} {srcl} {pos_line(cfg, close)}",
        f"지금 할 일: {todo} 중기 보유 계획이라 실적 하루 반응으로 매매하지 않습니다.",
        "나중에: 오파칼림 부분 임상 보류 해제, 거래 종결, RISE3 톱라인 공시는 이 알림의 공시 감시로 따로 옵니다. 종가 선 손절선 68,000원, 83,400원, 91,600원, 평단 106,683원 감시는 계속합니다.",
        "익절/줄일 때: 실적만으로 줄이지 않습니다. 하회이면서 매출 성장이 +15% 미만이면 보유 논리를 재점검하고, 손절선 68,000원 종가 이탈 때 15주(절반) 매도 검토. 최대 비중 3%, 보류 해제 전 물타기 금지.",
        "하지 말 것: 잠정치를 확정치로 보기, 실적 하루 반응으로 매매, 마일스톤이 들어간 상회만 보고 추격 매수, 보류 해제 전 물타기, 신용 및 미수. 투자 권유 아님.",
    ])
    return title, msg, 3 if v == "부합" else 4


def build_q3_empty(cfg: dict, stamp: str) -> tuple[str, str, int]:
    e = cfg["earn_q3"]
    title = f"{cfg['title_prefix']} 3분기 실적 원문 없음 | 직접 확인"
    msg = "\n".join([
        engine.head(cfg, stamp),
        f"이게 뭐 알림: {cfg['short']} {e['period']} 잠정실적 판정 창({e['start']}~{e['end']})이 끝났는데 실적 공시를 자동으로 찾지 못했습니다. 매매 신호가 아닙니다.",
        f"지금 결과/상태: 네이버 공시 피드에 {e['title_key']} 공시가 없고 네이버 분기 재무도 아직 추정치입니다. 공시 목록: {cfg['sources']['notice_page'].format(code=cfg['code'])}",
        f"지금 할 일: DART와 회사 IR 자료에서 3분기 실적을 직접 확인하세요. 비교 기준은 영업이익 {e['consensus']['op']:,}억, 매출 {e['consensus']['rev']:,}억입니다.",
        "나중에: 이 실적 판정은 더 오지 않습니다. 종가 선과 공시 감시는 계속합니다.",
        "익절/줄일 때: 실적 미확인 상태로 비중을 바꾸지 마세요. 보유 30주, 평단 106,683원, 최대 비중 3%.",
        "하지 말 것: 추정치를 실적으로 읽기, 신용 및 미수. 투자 권유 아님.",
    ])
    return title, msg, 3


def earn_step(cfg: dict, st: dict, now: datetime, io: dict, fails: list[str], close: float | None = None) -> list[tuple]:
    e = cfg.get("earn_q3")
    if not e:
        return []
    es = st.setdefault("earn_q3", {"sent": None, "empty_sent": None, "result": None})
    today = session_day(now).isoformat()
    if es.get("sent") or es.get("empty_sent") or today < e["start"]:
        return []
    if (date.fromisoformat(today) - date.fromisoformat(e["end"])).days > int(e.get("catch_up_days", 3)):
        return []
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    res, src, f, notes = find_q3(cfg, io)
    for n in notes:
        print(n)
    if res:
        xc = xcopri_us(cfg, io)
        t, m, p = build_q3(cfg, res, src, xc, stamp, close if close is not None else st.get("last_close"))
        es.update(sent=stamp, result={"rev": res["rev"], "op": res["op"], "src": src, "verdict": verdict(e, res["op"]), "xcopri": xc[0]})
        return [(t, m, p, ["clipboard"])]
    if f:
        fails.extend(f)  # hold: no 원문 없음 on a failed or unreadable feed
        return []
    if today >= e["end"]:
        es["empty_sent"] = stamp
        return [build_q3_empty(cfg, stamp) + (["clipboard"],)]
    return []


# ---------- run ----------
def seed_state(cfg: dict) -> dict:
    st = engine.default_state(cfg)
    st.update(seeded=True, last_day=cfg["seed"]["day"], last_close=float(cfg["seed"]["close"]))
    for ln in cfg["lines"]:
        st["lines"][ln["key"]]["armed"] = not engine.hit(ln, st["last_close"])
    st["earn_q3"] = {"sent": None, "empty_sent": None, "result": None}
    st["seed_note"] = f"config seed {cfg['seed']['day']} close {cfg['seed']['close']:,}"
    return st


def ended(cfg: dict, now: datetime) -> bool:
    end = datetime.strptime(cfg["ends_kst"], "%Y-%m-%d %H:%M").replace(tzinfo=KST)
    return now > end or now.year != int(cfg.get("year_guard") or now.year)


def plan(cfg: dict, st: dict, now: datetime, io: dict) -> list[tuple]:
    """Mutates st; returns [(title, msg, pri, tags)] for lines, notices, Q3 result and the daily fail notice."""
    data, fails = io["collect"](cfg, now)
    print(f"data src={data.get('src')} rows={(data.get('rows') or [])[-3:]} fails={fails}")
    fails = list(fails)
    rows = data.get("rows") or []
    sends = earn_step(cfg, st, now, io, fails, rows[-1]["close"] if rows else None)
    sends = [(t, engine.with_footer(cfg, m), p, tg) for t, m, p, tg in sends]
    for t, m, _, _ in sends:
        engine.clean(t, m)
    kw = {"notice_fn": io.get("notice_rows")}
    if io.get("ref"):
        kw["ref_fn"] = io["ref"]
    out = [hold_adjust(cfg, *x) for x in engine.plan_sends(cfg, st, data, fails, now, **kw)]
    return sends + pgtc_note(cfg, st, out, now)


def pgtc_note(cfg: dict, st: dict, sends: list[tuple], now: datetime) -> list[tuple]:
    """Remember an acceptance-like notice (접수, PDUFA, 심사, 거절) seen by the notice watch; the PGTC date checks say so."""
    words = cfg.get("pgtc_accept_words") or []
    for t, m, _p, _tg in sends:
        if not t.startswith(cfg["title_prefix"] + " 결과 공시 나옴"):
            continue
        line = next((x for x in m.split("\n") if x.startswith("지금 결과/상태:")), "")
        head = line.split(". 공시 목록:")[0].replace("지금 결과/상태: ", "").replace(f"새 공시({cfg['code']}): ", "")
        if any(w in head for w in words):
            st.setdefault("pgtc_seen", []).append({"day": session_day(now).isoformat(), "title": head[:120]})
    seen = st.get("pgtc_seen") or []
    titles = {tc["title"] for tc in cfg.get("time_checks") or []}
    out = []
    for t, m, p, tg in sends:
        if seen and t.split(" | ")[0][len(cfg["title_prefix"]) + 1:] in titles:
            note = " 공시 감시가 이미 본 관련 공시: " + ", ".join(f"{x['title']}({x['day']})" for x in seen[-3:]) + ". 이미 알림을 받았다면 내용만 다시 확인하세요."
            m = "\n".join(x + note if x.startswith("지금 결과/상태:") else x for x in m.split("\n"))
        out.append((t, m, p, tg))
    return out


NO_BUY = ("추가 매수를 검토", "매수 검토", "분할 매수", "추가 매수 조건 점검")


def hold_adjust(cfg: dict, t: str, m: str, p: int, tg) -> tuple:
    """Held position: per-line priority (stop p4, others p3) and a stop message with no buy wording."""
    by = {ln["title"]: ln for ln in cfg["lines"]}
    top = next((ln for tt, ln in by.items() if t.endswith("| " + tt)), None)
    if not top:
        return t, m, p, tg
    p = int(top.get("priority", p))
    if top["key"] == "stop":
        if cfg.get("lines_trim_stop"):
            m = m.replace(cfg["lines_trim"], cfg["lines_trim_stop"])
        for ln in cfg["lines"]:
            if ln["key"] != "stop" and ln["action"] in m:
                m = m.replace(ln["action"], "")
        m = "\n".join(" ".join(x.split()) if x.startswith("지금 할 일:") else x for x in m.split("\n"))
        bad = [w for w in NO_BUY if w in m or w in t]
        if bad:
            raise ValueError(f"buy wording in stop message: {bad}")
    return t, m, p, tg


def pos_line(cfg: dict, close: float | None) -> str:
    ps = cfg["position"]
    if close is None:
        return f"보유 {ps['shares']}주, 평단 {ps['avg']:,}원."
    return f"보유 {ps['shares']}주, 평단 {ps['avg']:,}원, 최근 종가 {close:,.0f}원 기준 평단 대비 {pct(close, ps['avg']):+.1f}%(평가손익 약 {(close - ps['avg']) * ps['shares'] / 10000:+,.0f}만원)."


def install_msg(cfg: dict, data: dict, fails: list[str], stamp: str) -> tuple[str, str]:
    rows = data.get("rows") or []
    px = f"최근 종가 {engine.fmt(rows[-1]['close'])}({rows[-1]['day']}, {data.get('src')}). " if rows else "시세 없음. "
    lines = ", ".join(f"{ln['title']} {engine.fmt(ln['level'])} {({'le': '이하', 'ge': '이상', 'lt': '미만'})[ln['op']]}" for ln in cfg["lines"])
    e = cfg["earn_q3"]
    w = cfg["notice_watch"]
    title = f"{cfg['title_prefix']} 설치 확인 | 매매 신호 아님"
    msg = "\n".join([
        engine.head(cfg, stamp),
        f"이게 뭐 알림: {cfg['short']} 보유 알림(보유 30주, 평단 106,683원) 갱신 설치 확인입니다. KRX 거래일 19:00에 새 조건이 생길 때만 옵니다. 끝: {cfg['ends_kst']} KST.",
        f"지금 결과/상태: {px}" + (" 조회 실패: " + ", ".join(fails) + "." if fails else "")
        + f" 감시 1) 3분기 실적 {e['start']}~{e['end']} 1번: 영업이익 컨센서스 {e['consensus']['op']:,}억(상회 {e['op_beat']:,} 이상, 하회 {e['op_miss']:,} 이하), "
        f"매출 {e['consensus']['rev']:,}억(+-{e['rev_band_pct']:.0f}%), 전년 동기 {e['yoy_base_rev']:,}억 대비 +15%({e['exit_rev']:,}억) 기준, 일본 마일스톤 포함 경고, Xcopri 미국 매출. "
        f"2) 공시 감시: {', '.join(w['keywords'])} 제목 새 공시마다 1번(결과 공시 나옴). 3) 종가 선: {lines}, 선마다 1회, 1% 반대로 돌아와야 재무장. "
        + pos_line(cfg, rows[-1]["close"] if rows else None),
        "지금 할 일: 없음. 참고: 52주 고가 144,100원, 증권사 목표가 120,000~160,000원(평균 약 14.5만~15.2만원).",
        f"나중에: {e['ir_note']}. 결과 판정은 그날 19:00에 옵니다.",
        "익절/줄일 때: 손절선 68,000원 종가 이탈 때 15주(절반) 매도 검토. 최대 비중 3%(약 600만원), 넘으면 넘은 만큼만 줄이기. 임상 보류 해제 전 물타기 금지.",
        "하지 말 것: 이 설치 확인을 매수나 매도 신호로 읽기. 투자 권유 아님.",
    ])
    msg = engine.with_footer(cfg, msg)
    engine.clean(title, msg)
    return title, msg


def execute(mode: str, cfg: dict, st: dict | None, now: datetime, io: dict, pub, save) -> int:
    """pub(topic, title, msg, pri, tags, server); save(st). st None = load failed or absent -> config seed."""
    dry = mode == "dry"
    topic = resolve_topic("port", cfg.get("ntfy_topic"))
    server = resolve_server(cfg.get("ntfy_server"))
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    if ended(cfg, now):
        print(f"{stamp} {NAME} ended ({cfg['ends_kst']} KST) or outside year guard; silent")
        return 0
    if mode == "install":
        data, fails = io["collect"](cfg, now)
        t, m = install_msg(cfg, data, fails, stamp)
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
        st = None
        p = engine.state_path(NAME)
        if p.exists() and mode != "install":
            st = engine.load_state(cfg)
            if "low68" in st["lines"]:  # 2026-11-04 low68 -> stop (held position), keep armed state
                st["lines"]["stop"] = st["lines"].pop("low68")
            st.setdefault("earn_q3", {"sent": None, "empty_sent": None, "result": None})
    except Exception as exc:  # config or state unreadable: crash guard
        if mode == "dry":
            raise
        traceback.print_exc()
        publish(OPS, f"[{NAME}] 실행 오류 | 조회 실패, 판단 보류", f"{now:%Y-%m-%d %H:%M KST} | {NAME} | 설정 또는 상태 파일을 못 읽음: {type(exc).__name__}", 3, ["warning"], "https://ntfy.sh")
        return 0
    if mode == "dry":
        print("DRY RUN")
        if st:
            st = copy.deepcopy(st)
    return execute(mode, cfg, st, now, live_io(), publish, lambda s: engine.save_state(cfg, s))


# ---------- offline selftest ----------
Q3_NOTICE = ("연결재무제표 기준 영업(잠정)실적(공정공시) 실적기간 당기실적 2026-07-01 ~ 2026-09-30 전기실적 2026-04-01 ~ 2026-06-30 "
             "전년동기실적 2025-07-01 ~ 2025-09-30 1. 연결실적내용 단위 : 백만원, % 구분 당기실적 전기실적 전기대비 전년동기실적 전년동기대비 "
             "매출액 당해실적 {rev} 247,367 3.0 - 191,700 33.0 - 누계실적 735,000 - - - 512,354 43.5 - "
             "영업이익 당해실적 {op} 97,064 -2.1 - 70,100 35.5 - 누계실적 281,832 - - - 157,655 78.8 - "
             "법인세비용차감전계속사업이익 당해실적 93,360 99,978 -6.6 - 34,676 169.2 - 누계실적 193,338")
SKBP_LIST = ('<ul class="list-news"><li><a href="javascript:;" data-seq="812"><span class="tit">SK바이오팜, 3분기 매출 2,600억, 영업이익 1,000억</span>'
             '<span class="cate">보도자료</span><span class="date">2026.11.05</span></a></li>'
             '<li><a href="javascript:;" data-seq="807"><span class="tit">SK바이오팜, 美서 세노바메이트 경험 공유</span><span class="date">2026.10.12</span></a></li></ul>')
SKBP_VIEW = "<table><tr><td>XCOPRI &reg; &nbsp;US&nbsp; 매출</td><td>2,390</td><td>+6.5%</td></tr></table>"


def selftest() -> int:
    from unittest import mock

    cfg = engine.load_cfg(NAME)
    n = {"ok": 0, "fail": 0}

    def check(label, got, want):
        good = got == want
        n["ok" if good else "fail"] += 1
        print(("ok  " if good else "FAIL"), label, "" if good else f"got={got!r} want={want!r}")

    def at(d: str, hm: str = "19:00") -> datetime:
        return datetime.strptime(f"{d} {hm}", "%Y-%m-%d %H:%M").replace(tzinfo=KST)

    sent, saved = [], []
    state_file = engine.state_path(NAME)
    before = (lambda s: (s.st_size, s.st_mtime_ns))(state_file.stat()) if state_file.exists() else None
    fx = {"rows": [], "fails": [], "watch": [], "notices": [], "quarter": None, "http": {}}

    def collect(c, now):
        if isinstance(fx["rows"], Exception):
            return {"rows": [], "src": None}, [f"{c['code']} 정규장 종가 (Yahoo {fx['rows']}; 판단 보류, 다음 실행에서 따라잡음)"]
        return {"rows": copy.deepcopy(fx["rows"]), "src": "Yahoo 정규장"}, list(fx["fails"])

    def watch_rows(c, code):
        if isinstance(fx["watch"], Exception):
            raise fx["watch"]
        return copy.deepcopy(fx["watch"])

    def notices(c):
        if isinstance(fx["notices"], Exception):
            raise fx["notices"]
        return copy.deepcopy(fx["notices"])

    def quarter(c):
        if fx["quarter"] is None:
            raise OSError("quarter fixture missing")
        return copy.deepcopy(fx["quarter"])

    def http(url):
        for k, v in fx["http"].items():
            if k in url:
                if isinstance(v, Exception):
                    raise v
                return v
        raise OSError(f"no fixture {url}")

    io = {"collect": collect, "notice_rows": watch_rows, "ref": lambda c, rows, now: f"{c['short']} NXT 정보 없음",
          "notices": notices, "quarter": quarter, "http": http}

    def pub(topic, title, msg, pri=3, tags=None, server=None):
        from ntfy_util import route_topic
        sent.append((route_topic(topic, title), title, pri, msg))

    def save(s):
        saved.append(copy.deepcopy(s))

    def blocked(*a, **k):
        raise OSError("selftest: network disabled")

    def qtr(cons: str, rev="2,546", op="950"):
        return {"financeInfo": {"trTitleList": [{"key": "202606", "isConsensus": "N"}, {"key": "202609", "isConsensus": cons}],
                                "rowList": [{"title": "매출액", "columns": {"202609": {"value": rev}}},
                                            {"title": "영업이익", "columns": {"202609": {"value": op}}}]}}

    def q3(rev: str, op: str, title="에스케이바이오팜 주식회사 연결재무제표기준영업(잠정)실적(공정공시)", dt="2026-11-05T00:50:00"):
        return {"id": "q3", "title": title, "datetime": dt, "text": Q3_NOTICE.format(rev=rev, op=op), "url": "x"}

    def run(mode, when, st):
        sent.clear()
        return execute(mode, cfg, st, when, io, pub, save)

    def texts():
        return [s[1] + "\n" + s[3] for s in sent]

    patches = [mock.patch("urllib.request.urlopen", blocked), mock.patch("socket.socket.connect", blocked),
               mock.patch("socket.create_connection", blocked)]
    keep = {k: getattr(engine, k) for k in ("get_json", "yahoo_rows", "naver_rows")}
    for p in patches:
        p.start()
    engine.get_json = engine.yahoo_rows = engine.naver_rows = blocked
    all_text: list[str] = []
    try:
        # --- parser on the real 2Q26 notice layout (백만원), numbers for 3Q
        r = parse_q3(Q3_NOTICE.format(rev="260,000", op="100,000"), "2026-09-30")
        check("parse 3Q notice 백만원 -> 억원 (rev, op, yoy bases)", r, {"rev": 2600, "op": 1000, "rev_yoy_base": 1917, "op_yoy_base": 701})
        check("2Q period is not 3Q", parse_q3(Q3_NOTICE.format(rev="1", op="1").replace("2026-07-01 ~ 2026-09-30", "2026-04-01 ~ 2026-06-30"), "2026-09-30"), None)
        check("no unit -> None", parse_q3(Q3_NOTICE.format(rev="1", op="1").replace("단위 : 백만원,", ""), "2026-09-30"), None)
        check("no 영업이익 row -> None", parse_q3(Q3_NOTICE.format(rev="1", op="1").replace("영업이익 당해실적", "기타"), "2026-09-30"), None)

        # --- seed: config seed 11/2 close 77,500, all lines armed, same close next day silent
        st = seed_state(cfg)
        check("seed: all four lines armed at 77,500", [st["lines"][k]["armed"] for k in ("stop", "hold83", "deal91", "avg106")], [True, True, True, True])
        fx.update(rows=[{"day": "2026-11-03", "close": 77500.0}], watch=[], notices=[], quarter=qtr("Y"))
        run("auto", at("2026-11-03"), None)
        check("first run from config seed, flat close -> silent", sent, [])
        check("state saved via save() only, last_day 11/3", saved[-1]["last_day"] if saved else None, "2026-11-03")

        # --- lines: fire, once, re-arm after 1%
        st = seed_state(cfg)
        days = iter(["2026-11-03", "2026-11-04", "2026-11-05", "2026-11-06", "2026-11-09", "2026-11-10", "2026-11-11",
                     "2026-11-12", "2026-11-13", "2026-11-16", "2026-11-17", "2026-11-18", "2026-11-19", "2026-11-20",
                     "2026-11-23", "2026-11-24", "2026-11-25", "2026-11-26", "2026-11-27", "2026-11-30"])
        st["earn_q3"]["empty_sent"] = "x"  # earnings checked separately below

        def feed(close):
            d = next(days)
            fx["rows"] = [{"day": d, "close": float(close)}]
            run("auto", at(d), st)
            return [s[1] for s in sent]

        t = feed(68000)
        check("close 68,000 (<=) fires low68 once", t, ["[326030] 종가 68,000원 이하 | 손절선 이탈 (10/15 저점)"])
        check("again 67,000 silent (fired once)", feed(67000), [])
        check("68,600 (< 1% above) no re-arm", feed(68600), [])
        check("68,000 still silent", feed(68000), [])
        check("68,700 (> 1% above) re-arms, silent", feed(68700), [])
        check("67,900 fires low68 again after re-arm", feed(67900), ["[326030] 종가 68,000원 이하 | 손절선 이탈 (10/15 저점)"])
        check("83,400 (>=) fires hold83", feed(83400), ["[326030] 종가 83,400원 이상 | 보류 전 종가 회복, 추가 매수 조건 점검"])
        check("82,600 (< 1% below) no re-arm, 84,000 silent", (feed(82600), feed(84000)), ([], []))
        check("82,500 (> 1% below) re-arms, 83,500 fires hold83 again", (feed(82500), feed(83500)), ([], ["[326030] 종가 83,400원 이상 | 보류 전 종가 회복, 추가 매수 조건 점검"]))
        check("91,600 fires deal91 (hold83 already fired)", feed(91600), ["[326030] 종가 91,600원 이상 | 라이선스 발표일 종가 회복"])
        check("92,000 silent", feed(92000), [])
        feed(106683)
        check("106,683 fires avg106 평단 회복 p3", [(s[1], s[2]) for s in sent], [("[326030] 종가 106,683원 이상 | 평단 회복", 3)])
        check("105,700 (< 1% below) no re-arm, 107,000 silent", (feed(105700), feed(107000)), ([], []))
        check("105,600 re-arms, 106,700 fires avg106 again", (feed(105600), feed(106700)), ([], ["[326030] 종가 106,683원 이상 | 평단 회복"]))
        all_text += texts()
        st2 = seed_state(cfg)
        st2["earn_q3"]["empty_sent"] = "x"
        fx["rows"] = [{"day": "2026-11-04", "close": 67000.0}]
        run("auto", at("2026-11-04", "14:00"), st2)
        check("intraday row (14:00) not judged", sent, [])
        run("auto", at("2026-11-04"), st2)
        msg = sent[0][3] if sent else ""
        check("stop message: p4, half-sell action, weight rule, footer, ladder, no buy wording",
              (sent[0][2] if sent else None, "15주(절반) 매도를 검토" in msg, "최대 비중 3%" in msg, "손절선 아래에서는 추가 매수 없음" in msg,
               msg.rstrip().endswith("보유 30주, 평단 106,683원, 비중 약 1.2%(총자산 약 2억원 자가 신고), 최대 3%. 투자 권유 아님."),
               "보류 전 종가선 83,400원" in msg, any(w in msg for w in NO_BUY), "미확인" in msg), (4, True, True, True, True, True, False, False))
        run("auto", at("2026-11-04", "22:00"), st2)
        check("same session twice -> silent", sent, [])

        # --- missing data: hold, ops notice once a day, catch-up in order
        st3 = seed_state(cfg)
        st3["earn_q3"]["empty_sent"] = "x"
        fx["rows"] = OSError("HTTP 503")
        run("auto", at("2026-11-04"), st3)
        check("Yahoo down -> one 조회 실패 to cwally-ops, no line judged", [(s[0], s[1]) for s in sent], [("cwally-ops", "[326030] 조회 실패 | 조건 판단 보류")])
        check("hold: last_day stays at seed", st3["last_day"], "2026-11-02")
        all_text += texts()
        run("auto", at("2026-11-04", "21:00"), st3)
        check("second failure same day -> no second notice", sent, [])
        fx["rows"] = [{"day": "2026-11-04", "close": 67500.0}, {"day": "2026-11-05", "close": 84000.0}]
        run("auto", at("2026-11-05"), st3)
        check("next run catches up 11/4 and 11/5 in order, one message with both hits",
              (len(sent), "손절선(10/15 저점 이탈, 68,000원 이하" in (sent[0][3] if sent else ""), "임상 보류 공시 전 종가선(83,400원 이상" in (sent[0][3] if sent else "")), (1, True, True))
        check("... combined stop message keeps p4 and drops the hold83 buy-condition action", (sent[0][2] if sent else None, "추가 매수를 검토" in (sent[0][3] if sent else "x")), (4, False))
        check("... both lines recorded (stop on 11/4, hold83 on 11/5)", (st3["lines"]["stop"]["fired_day"], st3["lines"]["hold83"]["fired_day"]), ("2026-11-04", "2026-11-05"))

        # --- notice watch
        st4 = seed_state(cfg)
        st4["earn_q3"]["empty_sent"] = "x"
        fx["rows"] = [{"day": "2026-11-04", "close": 77000.0}]
        hold = {"no": "93001", "code": "326030", "datetime": "2026-11-04 07:10",
                "title": "에스케이바이오팜 주식회사 투자판단 관련 주요경영사항(뇌전증 신약 후보 물질 Opakalim의 미국 FDA 임상 2/3상 Partial Clinical Hold 해제)"}
        fx["watch"] = [hold, {"no": "93002", "code": "326030", "datetime": "2026-11-04 07:00", "title": "에스케이바이오팜 주식회사 단일판매, 공급계약체결"},
                       {"no": "92000", "code": "326030", "datetime": "2026-09-10 06:52", "title": "에스케이바이오팜 주식회사 투자판단 관련 주요경영사항(Opakalim Partial Clinical Hold 통보 수령)"}]
        run("auto", at("2026-11-04"), st4)
        check("new 투자판단 filing -> one 결과 공시 나옴 (old 9/10 filing and 공급계약 ignored)",
              [s[1].startswith("[326030] 결과 공시 나옴 | 투자판단 관련 주요경영사항") for s in sent], [True])
        body = sent[0][3] if sent else ""
        check("notice message has headline, link and leaves judgment to the user",
              ("Partial Clinical Hold 해제" in body, "https://m.stock.naver.com/domestic/stock/326030/notice" in body, "판정하지 않습니다" in body), (True, True, True))
        all_text += texts()
        fx["rows"] = [{"day": "2026-11-05", "close": 77000.0}]
        run("auto", at("2026-11-05"), st4)
        check("same filing number next day -> silent", sent, [])
        fx["watch"].append({"no": "93100", "code": "326030", "datetime": "2026-11-06 16:00", "title": "에스케이바이오팜 주식회사 투자판단 관련 주요경영사항(Opakalim RISE3 임상 3상 톱라인 결과)"})
        fx["rows"] = [{"day": "2026-11-06", "close": 77000.0}]
        run("auto", at("2026-11-06"), st4)
        check("a second new filing (RISE3) -> its own alert", len(sent), 1)
        fx["watch"] = OSError("HTTP 500")
        fx["rows"] = [{"day": "2026-11-09", "close": 77000.0}]
        run("auto", at("2026-11-09"), st4)
        check("notice feed down -> 조회 실패 to cwally-ops", [s[0] for s in sent], ["cwally-ops"])
        fx["watch"] = []

        # --- Q3 verdicts
        fx.update(http={"list.do": SKBP_LIST, "boardSeq=812": SKBP_VIEW}, quarter=qtr("Y"))

        def q3_run(rev, op, when="2026-11-05", st=None, **kw):
            st = st or seed_state(cfg)
            fx["rows"] = [{"day": when, "close": 80000.0}]
            fx["notices"] = [q3(rev, op, **kw)]
            run("auto", at(when), st)
            return st

        st5 = q3_run("260,000", "100,000")
        check("OP 1,000억 -> 상회 p4, title prefix", [(s[1], s[2]) for s in sent], [("[326030] 3분기 영업익 1,000억 컨센 대비 +5% | 상회, 마일스톤 포함", 4)])
        body = sent[0][3] if sent else ""
        check("상회 body: rev vs consensus band, YoY, +15% exit line, milestone, Xcopri parsed",
              ("매출 2,600억(컨센서스 2,546억 대비 +2.1%, +-3% 기준 부합 범위)" in body, "전년 동기(1,917억) 대비 +35.6%" in body,
               "제외 기준(매출 +15%, 2,205억): 통과" in body, "일본 세노바메이트 허가(9/16) 마일스톤" in body, "Xcopri 미국 매출 2,390억" in body),
              (True, True, True, True, True))
        all_text += texts()
        fx["rows"] = [{"day": "2026-11-06", "close": 80000.0}]
        run("auto", at("2026-11-06"), st5)
        check("result sends once (next run silent)", sent, [])
        q3_run("254,600", "99,800")
        check("OP 998 boundary -> 상회", [s[1].split(" | ")[1][:2] for s in sent], ["상회"])
        q3_run("254,600", "95,000")
        check("OP 950 -> 부합 p3", [(s[1].split(" | ")[1][:2], s[2]) for s in sent], [("부합", 3)])
        all_text += texts()
        q3_run("254,600", "90,300")
        check("OP 903 -> 부합", [s[1].split(" | ")[1][:2] for s in sent], ["부합"])
        q3_run("200,000", "90,200")
        check("OP 902 boundary -> 하회 p4", [(s[1].split(" | ")[1][:2], s[2]) for s in sent], [("하회", 4)])
        body = sent[0][3] if sent else ""
        check("rev 2,000억: 컨센 아래, +15% exit 미달", ("-21.4%, +-3% 기준 아래" in body, "미달, 보유 논리 재점검 조건에 해당" in body), (True, True))
        check("verdict body: 보유 30주, 평단, % vs avg at the last close", ("보유 30주, 평단 106,683원, 최근 종가 80,000원 기준 평단 대비 -25.0%" in body), True)
        all_text += texts()
        q3_run("270,000", "95,000")
        check("rev +6% is 위 but OP decides (부합)", ("+-3% 기준 위" in (sent[0][3] if sent else ""), sent[0][1].split(" | ")[1][:2] if sent else None), (True, "부합"))
        fx["http"] = {"list.do": OSError("HTTP 500")}
        q3_run("260,000", "100,000")
        check("Xcopri page down -> 수동 확인 line, result still sent", ("Xcopri 미국 매출: 자동 확인 안 됨" in (sent[0][3] if sent else ""), len(sent)), (True, 1))
        all_text += texts()
        fx["http"] = {"list.do": SKBP_LIST, "boardSeq=812": SKBP_VIEW}
        q3_run("260,000", "100,000", title="[정정] 에스케이바이오팜 주식회사 연결재무제표기준영업(잠정)실적(공정공시)")
        check("정정 notice skipped, quarter still consensus -> silent", sent, [])
        q3_run("260,000", "100,000", dt="2026-08-05T06:50:30")
        check("notice before since (old filing) -> silent", sent, [])
        q3_run("260,000", "100,000", when="2026-11-04")
        check("before window (11/4) -> no earnings send", sent, [])
        fx.update(notices=[], quarter=qtr("N", "2,580", "960"))
        st6 = seed_state(cfg)
        fx["rows"] = [{"day": "2026-11-06", "close": 80000.0}]
        run("auto", at("2026-11-06"), st6)
        check("no notice, quarter non-consensus -> fallback verdict 부합 with 원문 확인 필요",
              (sent[0][1] if sent else None, "공시 원문 확인 필요" in (sent[0][3] if sent else "")), ("[326030] 3분기 영업익 960억 컨센 대비 +1% | 부합, 마일스톤 포함", True))

        # --- missing Q3: silent until 11/12, then 원문 없음 once; feed failure holds
        fx.update(notices=[], quarter=qtr("Y"))
        st7 = seed_state(cfg)
        for d in ("2026-11-05", "2026-11-11"):
            fx["rows"] = [{"day": d, "close": 80000.0}]
            run("auto", at(d), st7)
        check("no result 11/5..11/11 -> silent", sent, [])
        fx["rows"] = [{"day": "2026-11-12", "close": 80000.0}]
        run("auto", at("2026-11-12"), st7)
        check("11/12 still nothing -> one 원문 없음 p3 to cwally-port", [(s[0], s[1], s[2]) for s in sent], [("cwally-port", "[326030] 3분기 실적 원문 없음 | 직접 확인", 3)])
        all_text += texts()
        fx["rows"] = [{"day": "2026-11-13", "close": 80000.0}]
        run("auto", at("2026-11-13"), st7)
        check("원문 없음 not repeated", sent, [])
        fx["notices"] = OSError("HTTP 500")
        st8 = seed_state(cfg)
        fx["rows"] = [{"day": "2026-11-12", "close": 80000.0}]
        run("auto", at("2026-11-12"), st8)
        check("feed down on 11/12 -> hold (no 원문 없음), ops notice", ([s[0] for s in sent], st8["earn_q3"]["empty_sent"]), (["cwally-ops"], None))
        fx["notices"] = [q3("260,000", "100,000", dt="2026-11-05T00:50:00")]
        fx["rows"] = [{"day": "2026-11-13", "close": 80000.0}]
        run("auto", at("2026-11-13"), st8)
        check("feed back on 11/13 (catch-up) -> verdict sent", [s[1].startswith("[326030] 3분기 영업익 1,000억") for s in sent], [True])
        st9 = seed_state(cfg)
        fx["notices"] = []
        fx["rows"] = [{"day": "2026-11-18", "close": 80000.0}]
        run("auto", at("2026-11-18"), st9)
        check("past end + 3 days -> earnings part silent", sent, [])
        fx["notices"] = [q3("260,000", "70,000")]
        fx["notices"][0]["text"] = fx["notices"][0]["text"].replace("단위 : 백만원,", "")
        st10 = seed_state(cfg)
        fx["rows"] = [{"day": "2026-11-05", "close": 80000.0}]
        run("auto", at("2026-11-05"), st10)
        check("unparsable result notice -> hold + 파싱 실패 to ops", ([s[0] for s in sent], st10["earn_q3"]["sent"]), (["cwally-ops"], None))

        # --- end date, holiday, crash guard, dry, install
        fx.update(notices=[], quarter=qtr("Y"), rows=[{"day": "2026-12-31", "close": 60000.0}])
        st11 = seed_state(cfg)
        run("auto", at("2027-02-01", "00:30"), st11)
        check("after ends_kst 2027-01-31 23:59 -> silent, nothing judged", (sent, st11["last_day"]), ([], "2026-11-02"))
        fx["rows"] = [{"day": "2026-12-30", "close": 60000.0}]
        run("auto", at("2026-12-30"), st11)
        check("12/30 19:00 (last KRX session) still runs (fires stop)", [s[1].startswith("[326030] 종가 68,000원 이하") for s in sent], [True])
        fx["rows"] = [{"day": "2026-12-30", "close": 60000.0}]
        run("auto", at("2026-12-31"), st11)
        check("12/31 KRX year-end holiday -> silent", sent, [])
        import ntfy_util
        gate = lambda when: ntfy_util.ends_skip_reason("cwally-port", "[326030] x", config_name=NAME, config=cfg, now=at(*when.split(" ")))
        check("publish end gate: 2027-01-31 23:59 open, 2027-02-01 01:00 blocked", (gate("2027-01-31 23:59"), gate("2027-02-01 01:00") is not None), (None, True))

        # --- PGTC sNDA date checks (2027-01-05, 2027-01-20), notice keywords, 2027 runs
        stp = seed_state(cfg)
        stp["last_day"], stp["last_close"] = "2026-12-30", 79000.0
        fx.update(notices=[], watch=[], rows=[{"day": "2027-01-04", "close": 79000.0}])
        run("auto", at("2027-01-04"), stp)
        check("2027-01-04 runs (no year guard), no date check yet, earn_q3 silent", sent, [])
        fx["rows"] = [{"day": "2027-01-05", "close": 79000.0}]
        run("auto", at("2027-01-05"), stp)
        check("2027-01-05 -> one p3 PGTC 접수 점검", [(s[1], s[2]) for s in sent], [("[326030] SK바이오팜 PGTC sNDA FDA 접수 여부 점검 | 날짜 점검", 3)])
        body = sent[0][3] if sent else ""
        check("1/5 body: 60-day ~1/2, Refuse-to-File, PDUFA, Day-74 1/16, position, stop, add rule, no buy advice",
              all(k in body for k in ("약 2027-01-02", "Refuse-to-File", "PDUFA", "약 2027-01-16", "보유 30주, 평단 106,683원", "68,000원", "83,400원 이상일 때만", "178330274"))
              and not any(w in body for w in NO_BUY), True)
        all_text += texts()
        fx["rows"] = [{"day": "2027-01-06", "close": 79000.0}]
        run("auto", at("2027-01-06"), stp)
        check("1/5 check sends once", sent, [])
        fx["watch"] = [{"no": "190001", "code": "326030", "datetime": "2027-01-08 07:00",
                        "title": "에스케이바이오팜 주식회사 투자판단 관련 주요경영사항(세노바메이트 PGTC 적응증 미국 FDA 허가 신청 접수)"},
                       {"no": "190002", "code": "326030", "datetime": "2027-01-08 07:00", "title": "에스케이바이오팜 주식회사 기업설명회(IR) 개최(안내공시)"}]
        fx["rows"] = [{"day": "2027-01-08", "close": 79000.0}]
        run("auto", at("2027-01-08"), stp)
        check("acceptance notice caught once (IR 안내 ignored)", [s[1][:30] for s in sent], ["[326030] 결과 공시 나옴 | 투자판단 관련 주요"])
        check("acceptance remembered for the Day-74 check", len(stp.get("pgtc_seen") or []), 1)
        fx["watch"] = []
        fx["rows"] = [{"day": "2027-01-20", "close": 79000.0}]
        run("auto", at("2027-01-20"), stp)
        check("2027-01-20 -> one p3 Day-74 check that mentions the seen notice",
              ([(s[1], s[2]) for s in sent], "공시 감시가 이미 본 관련 공시" in (sent[0][3] if sent else "")),
              ([("[326030] SK바이오팜 PGTC sNDA Day-74 확인 (PDUFA 날짜 공개 여부) | 날짜 점검", 3)], True))
        all_text += texts()
        stq = seed_state(cfg)
        stq["last_day"] = "2027-01-15"
        fx["rows"] = [{"day": "2027-01-29", "close": 79000.0}]
        run("auto", at("2027-01-29"), stq)
        check("stale: first run 1/29 -> both date checks skipped (catch-up 5 days)", sent, [])
        kw = cfg["notice_watch"]["keywords"]
        old = ["기업설명회(IR) 개최(안내공시)", "단일판매ㆍ공급계약체결", "정기주주총회결과", "감사보고서 제출", "주주명부폐쇄기간 또는 기준일 설정", "단기차입금증가결정",
               "주식선물 2단계 가격제한폭 확대요건 도달(상승)", "유상증자결정(종속회사의 주요경영사항)", "타법인주식및출자증권취득결정", "지속가능경영보고서 등 관련사항(자율공시)"]
        check("new keywords hit no routine title from the 2024-2026 feed", [t for t in old if any(k in t for k in kw)], [])
        fx["rows"] = [{"day": "2026-11-06", "close": 60000.0}]
        st12 = seed_state(cfg)
        run("auto", at("2026-11-07"), st12)
        check("Saturday -> holiday silent", (sent, st12["last_day"]), ([], "2026-11-02"))
        boom = dict(io)
        boom["collect"] = lambda c, now: (_ for _ in ()).throw(RuntimeError("boom"))
        sent.clear()
        rc = execute("auto", cfg, seed_state(cfg), at("2026-11-04"), boom, pub, save)
        check("crash -> rc 0 and one 실행 오류 notice to cwally-ops", (rc, [(s[0], s[1]) for s in sent]), (0, [("cwally-ops", "[326030] 실행 오류 | 조회 실패, 판단 보류")]))
        all_text += texts()
        saved.clear()
        fx["rows"] = [{"day": "2026-11-04", "close": 60000.0}]
        sent.clear()
        execute("dry", cfg, seed_state(cfg), at("2026-11-04"), io, pub, save)
        check("dry: no publish, no save", (sent, saved), ([], []))
        fx["rows"] = [{"day": "2026-11-03", "close": 81300.0}]
        sent.clear()
        execute("install", cfg, None, at("2026-11-03", "12:30"), io, pub, save)
        check("install: one send to cwally-port, [326030] 설치 확인, lists the three parts",
              ([(s[0], s[1]) for s in sent], all(k in (sent[0][3] if sent else "") for k in ("3분기 실적", "공시 감시", "종가 선", "81,300원"))),
              ([("cwally-port", "[326030] 설치 확인 | 매매 신호 아님")], True))
        check("install wrote no state", saved, [])
        check("install body: held position, no 미확인", ("보유 30주, 평단 106,683원" in (sent[0][3] if sent else ""), "미확인" in (sent[0][3] if sent else "")), (True, False))
        all_text += texts()

        # --- text rules over every message sent above
        check("every title starts with [326030]", all(t.startswith("[326030] ") for t in all_text), True)
        bad = sorted({ch for t in all_text for ch in ("\u00b7", "\u2014") if ch in t})
        check("no middle dot or em-dash in any message", bad, [])
        check(f"messages checked: {len(all_text)} (lines, ops fail, notice, 3 verdicts, Xcopri fail, 원문 없음, crash, install)", len(all_text) >= 9, True)
    finally:
        for k, v in keep.items():
            setattr(engine, k, v)
        for p in reversed(patches):
            p.stop()
    after = (lambda s: (s.st_size, s.st_mtime_ns))(state_file.stat()) if state_file.exists() else None
    check("state/326030.json untouched (size, mtime)", after == before, True)
    print(f"SELFTEST {'OK' if not n['fail'] else 'FAIL'}: {n['ok']} passed, {n['fail']} failed (fixtures only, no network, no publish, no state)")
    return 1 if n["fail"] else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
