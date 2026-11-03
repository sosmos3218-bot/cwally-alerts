#!/usr/bin/env python3
"""[MEMEXIT] 메모리 4단계 Exit 계획 점검: SK하이닉스 000660, 삼성전자 005930 (2026-11-03 사용자 계획 붙여 넣기).

KRX 거래일 19:00 KST (port-alerts k203780 슬롯에 부착, 새 cron 없음). 상태가 바뀔 때만 보내고, 같은 실행에서 걸린 것은 한 메시지로 묶음.
  1) 고점 대비 낙폭: 2026-11-02 이후 KRX 정규장 종가 최고치 대비 -20% 이하 p4 1번, 새 고점 또는 -12% 위 회복에 재무장.
  2) 외국인 월간: 네이버 종목 투자자 일별 외국인 순매수 주식 수를 달력 월로 합산, 3개월 연속 순매도 p3 1번(다음 금융으로 부호 교차 확인),
     순매수 달에 재무장. 달이 끝난 뒤 첫 실행에서 판정.
  3) 12M fwd PER: WiseReport 컨센서스 올해 E, 내년 E EPS 를 남은 일수로 가중. 8배 이상 p3 1번, 7.5배 미만 재무장.
  4) 월간 점검: 매달 첫 KRX 거래일 p3 1번(종가, 고점 대비, 지난달 외국인, PER, 지난달 뉴스 건수와 제목, 수동 분기 점검, Phase).
  5) 실적 리마인더: 분기 실적(컨콜) 다음 거래일 p3. 두 회사 날짜가 3일 안이면 하나로. 날짜는 IR 개최 안내공시로 확정.
  6) 단발: 2027-07-01 Phase 2 전환 점검, 2027-12-01 연장 점검.
첫 실행은 조용히 시드. 데이터 조회 실패는 하루 1번 cwally-ops. ends_kst 뒤 조용히 종료.
DRAM 계약가 상승률, LTA 비중, DIO, CSP CAPEX 숫자는 자동 판정하지 않음(월간 점검 수동 줄 + 실적 리마인더).
usage: memexit_monitor.py <auto|dry> [--as-of "YYYY-MM-DD HH:MM"] | selftest | confirm
"""
from __future__ import annotations

import html
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from kr_calendar import KST, is_trading_day  # noqa: E402
from ntfy_util import publish, resolve_server, resolve_topic  # noqa: E402

NAME = "memexit"
TAG = "[MEMEXIT]"
CODES = "000660 005930"
CFG_PATH = ROOT / "configs" / f"{NAME}.json"
STATE_PATH = ROOT / "state" / f"{NAME}.json"
MEMWATCH_STATE = ROOT / "state" / "memwatch.json"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
BAD_WORDS = ("분할 매수", "분할매수", "매수 기회", "저가 매수", "추가 매수", "매수하세요", "사세요", "물타기", "비중 확대", "진입", "담을",
             "매수 검토", "지금 매수")


def get(url: str, referer: str | None = None, tries: int = 3) -> bytes:
    h = {"User-Agent": UA}
    if referer:
        h["Referer"] = referer
    err = None
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=30) as r:
                return r.read()
        except Exception as exc:  # noqa: BLE001
            err = exc
            time.sleep(2 * (i + 1))
    raise err  # type: ignore[misc]


def won(v) -> str:
    return f"{v:,.0f}" if v is not None else "?"


def ymd(d: str) -> str:
    return f"{int(d[5:7])}/{int(d[8:10])}"


# ---------- 데이터 ----------
def closes(cfg: dict, code: str, now: datetime, n: int = 40) -> list[tuple[str, float]]:
    """KRX 정규장 확정 종가 [(YYYY-MM-DD, close)]. 당일 행은 15:31 이후만."""
    raw = get(cfg["sources"]["price"].format(code=code, n=n)).decode("euc-kr", "ignore")
    rows = sorted((f"{d[:4]}-{d[4:6]}-{d[6:]}", float(c)) for d, c in re.findall(r'item data="(\d{8})\|[^|]*\|[^|]*\|[^|]*\|([\d.]+)\|', raw))
    today = now.strftime("%Y-%m-%d")
    return [r for r in rows if r[0] < today or (r[0] == today and (now.hour, now.minute) >= (15, 31))]


def month_bounds(ym: str) -> tuple[date, date]:
    y, m = int(ym[:4]), int(ym[5:7])
    start = date(y, m, 1)
    nxt = date(y + (m == 12), m % 12 + 1, 1)
    return start, nxt


def trading_days_in(ym: str) -> int:
    s, e = month_bounds(ym)
    return sum(is_trading_day(s + timedelta(days=i)) for i in range((e - s).days))


def naver_month(cfg: dict, code: str, ym: str) -> dict:
    """{'shares','amt','days'} 외국인 순매수 주식 수 합, 추정금액(주식 수 x 종가), 행 수."""
    _, nxt = month_bounds(ym)
    rows = json.loads(get(cfg["sources"]["naver_trend"].format(code=code, bizdate=nxt.strftime("%Y%m%d"))))
    sh = amt = n = 0
    for r in rows:
        if not str(r.get("bizdate", "")).startswith(ym.replace("-", "")):
            continue
        v = int(str(r["foreignerPureBuyQuant"]).replace(",", "").replace("+", ""))
        c = int(str(r["closePrice"]).replace(",", ""))
        sh, amt, n = sh + v, amt + v * c, n + 1
    return {"shares": sh, "amt": amt, "days": n}


def daum_months(cfg: dict, code: str, yms: list[str]) -> dict:
    """다음 금융(KRX) 외국인 순매수 주식 수 월 합 {ym: shares}. 교차 확인용."""
    need = min(month_bounds(ym)[0] for ym in yms).isoformat()
    out: dict = {}
    for page in range(1, 9):
        data = json.loads(get(cfg["sources"]["daum_investor"].format(code=code, page=page), referer=f"https://finance.daum.net/quotes/A{code}"))["data"]
        for r in data:
            ym = r["date"][:7]
            if ym in yms:
                out[ym] = out.get(ym, 0) + int(r["foreignStraightPurchaseVolume"])
        if not data or data[-1]["date"][:10] < need:
            break
    return out


def wise_eps(cfg: dict, code: str, year: int) -> dict:
    """WiseReport 컨센서스 {'y0': year E EPS, 'y1': year+1 E EPS, 'base': 기준일}."""
    page = cfg["sources"]["wise_page"].format(code=code)
    m = re.search(r"sDT:\s*'(\d{8})'", get(page).decode("utf-8", "ignore"))
    if not m:
        raise ValueError("WiseReport 기준일(sDT) 없음")
    rows = json.loads(get(cfg["sources"]["wise_table"].format(code=code, dt=m.group(1)), referer=page))["JsonData"]
    lab = {r.get("YYMM", ""): r for r in rows}

    def eps(y):
        r = lab.get(f"{y}.12(E)") or lab.get(f"{y}.12(A)")
        try:
            return float(str(r.get("EPS")).replace(",", "")) if r else None
        except ValueError:
            return None
    y0, y1 = eps(year), eps(year + 1)
    lo = cfg["per"]["plausible_eps_min"]
    if not y0 or not y1 or y0 < lo or y1 < lo:
        raise ValueError(f"{year}E/{year + 1}E EPS 없음 또는 이상 ({y0}, {y1})")
    b = m.group(1)
    return {"y0": y0, "y1": y1, "year": year, "base": f"{b[:4]}-{b[4:6]}-{b[6:]}"}


def fwd_per(price: float, e: dict, day: str) -> tuple[float, float]:
    d = date.fromisoformat(day)
    left = (date(d.year, 12, 31) - d).days / 365.0
    left = min(max(left, 0.0), 1.0)
    feps = e["y0"] * left + e["y1"] * (1 - left)
    return price / feps, feps


def naver_per(cfg: dict, code: str) -> dict:
    d = json.loads(get(cfg["sources"]["naver_integration"].format(code=code)))
    out = {}
    for t in d.get("totalInfos") or []:
        if t.get("code") in ("per", "cnsPer"):
            out[t["code"]] = f"{t.get('value')}{(' (' + t['valueDesc'] + ')') if t.get('valueDesc') else ''}"
    return out


def ir_dates(cfg: dict, code: str) -> dict:
    """네이버 공시 피드의 기업설명회(IR) 개최 안내공시 -> {'2026Q4': 'YYYY-MM-DD'} (경영실적 발표만)."""
    rows = json.loads(get(cfg["sources"]["naver_notice"].format(code=code)))
    out = {}
    for r in rows if isinstance(rows, list) else []:
        if "기업설명회" not in str(r.get("title") or ""):
            continue
        t = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html.unescape(str(r.get("contents") or ""))))
        m = re.search(r"일시\s*(\d{4}-\d{2}-\d{2})", t)
        p = re.search(r"개최목적\s*(.{0,60})", t)
        if not m or not p or "경영실적" not in p.group(1):
            continue
        q = re.search(r"(\d{4})년\s*(?:연간\s*(?:및\s*)?)?([1-4])\s*분기", p.group(1))
        if q:
            key = f"{q.group(1)}Q{q.group(2)}"
        elif re.search(r"(\d{4})년\s*(연간|경영실적)", p.group(1)):
            key = f"{re.search(r'(\d{4})년', p.group(1)).group(1)}Q4"
        else:
            continue
        out.setdefault(key, m.group(1))  # 피드는 최신순: 정정 공시가 먼저
    return out


def gnews(cfg: dict, q: str, ym: str) -> list[tuple[str, str]]:
    s, e = month_bounds(ym)
    qq = f"{q} after:{(s - timedelta(days=1)).isoformat()} before:{e.isoformat()}"
    x = get(cfg["sources"]["gnews"].format(q=urllib.parse.quote(qq))).decode("utf-8", "ignore")
    out = []
    for it in re.findall(r"<item>(.*?)</item>", x, re.S):
        t = re.search(r"<title>(.*?)</title>", it, re.S)
        d = re.search(r"<pubDate>(.*?)</pubDate>", it)
        if t:
            title = html.unescape(re.sub(r"<!\[CDATA\[|\]\]>", "", t.group(1))).strip()
            out.append((clean(title), d.group(1)[5:16] if d else ""))
    return out


def memwatch_ref() -> str:
    try:
        s = json.loads(MEMWATCH_STATE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return ""
    parts = []
    r = (s.get("rolling") or {}).get("dram")
    if r and r.get("chg") is not None:
        parts.append(f"트렌드포스 DDR4 8Gb 계약가 {r.get('base')}~{r.get('last')} {r['chg']:+.1f}%")
    t = (s.get("tf_latest") or {}).get("dram")
    if t and t.get("lo") is not None:
        sign = "+" if (t.get("dir") or 1) > 0 else "-"
        kind = "전망" if t.get("kind") == "forecast" else "집계"
        parts.append(f"{t.get('q')} 범용 D램 계약가 {kind} {sign}{t['lo']:g}~{t['hi']:g}% (보도 {t.get('date')})")
    return ("참고 [MEMWATCH]: " + ", ".join(parts)) if parts else ""


# ---------- 판정 ----------
def step_drawdown(cfg: dict, st: dict, code: str, rows: list[tuple[str, float]]) -> dict | None:
    """새 종가 날짜만 차례로 처리. 발송할 이벤트(최대 1개) 반환. st['dd'][code] 를 고침."""
    dd = cfg["drawdown"]
    s = st.setdefault("dd", {}).setdefault(code, {"armed": True})
    last = s.get("last_date", "")
    ev = None
    for d, c in rows:
        if d < dd["start_day"] or d <= last:
            continue
        if c > s.get("peak", 0):
            s["peak"], s["peak_date"] = c, d
        pct = round((c / s["peak"] - 1) * 100, 4)
        if s["armed"] and pct <= dd["trigger_pct"]:
            s["armed"], s["fired"] = False, d
            ev = {"kind": "dd", "code": code, "day": d, "close": c, "peak": s["peak"], "peak_date": s["peak_date"], "pct": pct}
        elif not s["armed"] and pct > dd["rearm_pct"]:
            s["armed"] = True
        s["last_date"], s["close"], s["pct"] = d, c, pct
    return ev


def foreign_streak(months: dict) -> int:
    n = 0
    for ym in sorted(months, reverse=True):
        if months[ym]["shares"] < 0:
            n += 1
        else:
            break
    return n


def step_foreign(cfg: dict, st: dict, code: str, ym: str, rec: dict, daum: int | None) -> dict | None:
    f = st.setdefault("foreign", {}).setdefault(code, {"armed": True, "months": {}})
    rec = dict(rec, daum=daum)
    f["months"][ym] = rec
    n = foreign_streak(f["months"])
    agree = daum is None or (daum < 0) == (rec["shares"] < 0)
    rec["agree"] = agree
    if rec["shares"] >= 0:
        f["armed"] = True
        return None
    if f["armed"] and n >= cfg["foreign"]["months_needed"] and agree:
        f["armed"] = False
        return {"kind": "foreign", "code": code, "ym": ym, "streak": n}
    return None


def step_per(cfg: dict, st: dict, code: str, per: float, day: str) -> dict | None:
    p = st.setdefault("per", {}).setdefault(code, {"armed": True})
    prev = p.get("day")
    p.update(value=round(per, 2), day=day)
    if prev == day:
        return None
    if p["armed"] and per >= cfg["per"]["trigger"]:
        p["armed"] = False
        return {"kind": "per", "code": code, "day": day, "per": per}
    if not p["armed"] and per < cfg["per"]["rearm_below"]:
        p["armed"] = True
    return None


def next_trading_after(d: str) -> str:
    x = date.fromisoformat(d) + timedelta(days=1)
    while not is_trading_day(x):
        x += timedelta(days=1)
    return x.isoformat()


def earn_groups(cfg: dict, st: dict) -> list[dict]:
    """분기별 리마인더 묶음 [{'id','q','label','codes','dates','confirmed','due'}]."""
    conf = st.get("ir", {})
    out = []
    md = cfg["earnings"]["merge_days"]
    for q in cfg["earnings"]["quarters"]:
        ds = {}
        for code in ("000660", "005930"):
            c = conf.get(code, {}).get(q["q"])
            ds[code] = (c, True) if c else (q[code], False)
        a, b = ds["000660"][0], ds["005930"][0]
        if abs((date.fromisoformat(a) - date.fromisoformat(b)).days) <= md:
            groups = [("000660", "005930")]
        else:
            groups = [("000660",), ("005930",)]
        for g in groups:
            last = max(ds[c][0] for c in g)
            out.append({"id": f"{q['q']}:{'+'.join(g)}", "q": q["q"], "label": q["label"], "codes": g,
                        "dates": {c: ds[c][0] for c in g}, "confirmed": {c: ds[c][1] for c in g}, "due": next_trading_after(last)})
    return out


def earn_left(st: dict, g: dict) -> bool:
    """이 묶음에 아직 리마인더를 안 받은 회사가 있으면 True (묶음이 나중에 바뀌어도 회사 단위로 중복 방지)."""
    done = st.get("earn_done", {}).get(g["q"], [])
    return any(c not in done for c in g["codes"])


def mark_earn(st: dict, g: dict, note: str) -> None:
    done = st.setdefault("earn_done", {}).setdefault(g["q"], [])
    done.extend(c for c in g["codes"] if c not in done)
    st.setdefault("earn_sent", {})[g["id"]] = note


def due_earn(cfg: dict, st: dict, today: str) -> list[dict]:
    out = []
    for g in earn_groups(cfg, st):
        if not earn_left(st, g) or g["due"] > today:
            continue
        if (date.fromisoformat(today) - date.fromisoformat(g["due"])).days > 10:
            mark_earn(st, g, f"skip(stale) {today}")
            continue
        out.append(g)
    return out


def first_trading_day(ym: str) -> str:
    s, _ = month_bounds(ym)
    while not is_trading_day(s):
        s += timedelta(days=1)
    return s.isoformat()


def prev_month(ym: str) -> str:
    y, m = int(ym[:4]), int(ym[5:7])
    return f"{y - (m == 1)}-{(m - 2) % 12 + 1:02d}"


# ---------- 메시지 ----------
def stock_line(cfg: dict, st: dict, code: str) -> str:
    nm = cfg["stocks"][code]["name"]
    d = st.get("dd", {}).get(code, {})
    p = st.get("per", {}).get(code, {})
    s = f"{nm} {code} {won(d.get('close'))}원({ymd(d['last_date']) if d.get('last_date') else '?'})"
    if d.get("peak"):
        s += f", 고점 {won(d['peak'])}원({ymd(d['peak_date'])}) 대비 {d.get('pct', 0):+.1f}%"
    if p.get("value") is not None:
        s += f", 12M fwd PER {p['value']:.1f}배"
    return s


def foreign_line(cfg: dict, st: dict, code: str, ym: str | None = None) -> str:
    f = st.get("foreign", {}).get(code, {})
    ms = f.get("months", {})
    if not ms:
        return f"{cfg['stocks'][code]['short']} 외국인 월간 자료 없음"
    ym = ym or max(ms)
    r = ms.get(ym)
    if not r:
        return f"{cfg['stocks'][code]['short']} {ym} 외국인 확인 불가"
    sign = "순매수" if r["shares"] >= 0 else "순매도"
    tail = "" if r.get("agree", True) else " (다음 금융과 부호 엇갈림)"
    return (f"{cfg['stocks'][code]['short']} {ym} 외국인 {sign} {abs(r['shares']):,}주(약 {abs(r['amt']) / 1e12:.2f}조원), "
            f"연속 순매도 {foreign_streak(ms)}개월{tail}")


def later_line(cfg: dict, st: dict, today: str) -> str:
    items = []
    ym = today[:7]
    y, m = int(ym[:4]), int(ym[5:7])
    nxt = f"{y + (m == 12)}-{m % 12 + 1:02d}"
    items.append(f"월간 점검 {first_trading_day(nxt)}")
    for g in earn_groups(cfg, st):
        if g["due"] > today and earn_left(st, g):
            tag = "확정" if all(g["confirmed"].values()) else "추정"
            items.append(f"{g['label']} 실적 리마인더 {g['due']}({tag})")
            break
    for o in cfg["oneoffs"]:
        if o["date"] > today and o["key"] not in st.get("oneoff_sent", {}):
            items.append(f"{o['title']} {o['date']}")
            break
    return "나중에: " + ", ".join(items) + ". 고점 대비 -20%, 외국인 3개월 연속 순매도, PER 8배는 걸릴 때만."


def build(cfg: dict, st: dict, evs: list[dict], stamp: str, today: str, monthly: dict | None) -> tuple[str, str, int]:
    labels, pri = [], 2
    status, todo = [], []
    warn = False
    for e in evs:
        k = e["kind"]
        if k == "dd":
            nm = cfg["stocks"][e["code"]]["name"]
            labels.append(f"{e['code']} 고점 대비 {e['pct']:.0f}%")
            pri, warn = max(pri, cfg["drawdown"]["priority"]), True
            status.append(f"{nm} {e['code']} {ymd(e['day'])} 종가 {won(e['close'])}원, 고점 {won(e['peak'])}원({ymd(e['peak_date'])}) 대비 {e['pct']:.1f}%.")
            todo.append(f"{nm}: {cfg['drawdown']['label']}. 원칙은 보유의 30% 를 최소 3회로 나눠 줄이는 것, 한 번에 다 팔지 않기.")
        elif k == "foreign":
            labels.append(f"{e['code']} 외국인 {e['streak']}개월 연속 순매도")
            pri, warn = max(pri, cfg["foreign"]["priority"]), True
            status.append(foreign_line(cfg, st, e["code"], e["ym"]) + ".")
            todo.append(f"{cfg['stocks'][e['code']]['name']}: 외국인 3개월 연속 순매도는 계획상 경고. Phase 2 조건(계약가, CAPEX, DIO, CXMT)과 같이 보세요.")
        elif k == "per":
            labels.append(f"{e['code']} 12M fwd PER {e['per']:.1f}배")
            pri, warn = max(pri, cfg["per"]["priority"]), True
            status.append(f"{cfg['stocks'][e['code']]['name']} 12M fwd PER {e['per']:.1f}배({ymd(e['day'])} 종가, WiseReport 컨센서스).")
            todo.append(f"{cfg['stocks'][e['code']]['name']}: PER 8배 돌파는 계획상 50% 이하로 축소 규칙. 컨센서스 EPS 변화(하향인지)부터 확인.")
        elif k == "earn":
            g = e["g"]
            names = " ".join(f"{cfg['stocks'][c]['short']} {ymd(g['dates'][c])}({'확정' if g['confirmed'][c] else '추정'})" for c in g["codes"])
            labels.append(f"{' '.join(g['codes'])} {g['label']} 실적 점검")
            pri = max(pri, cfg["earnings"]["priority"])
            status.append(f"{g['label']} 실적 발표 {names}.")
            todo.append(f"{g['label']} 실적 자료와 컨콜에서 {cfg['earnings']['text']}. 수치가 나왔는지 먼저 확인(추정 날짜면 발표 전일 수 있음).")
        elif k == "oneoff":
            o = e["o"]
            labels.append(o["title"])
            pri = max(pri, o["priority"])
            todo.append(o["body"])
    if monthly:
        labels.append(f"월간 점검 {monthly['ym']}")
        pri = max(pri, cfg["monthly"]["priority"])
    title = f"{TAG} 000660 005930 " + " + ".join(labels)
    lines = [f"이게 뭐 알림: 메모리 4단계 Exit 계획 점검, 현재 {cfg['phase_label']}. 이번: {', '.join(labels)}.",
             f"{stamp} | {CODES} | SK하이닉스, 삼성전자"]
    st_lines = status + [stock_line(cfg, st, c) for c in cfg["stocks"]]
    if monthly:
        st_lines += monthly.get("hi_ref", [])
        st_lines += [foreign_line(cfg, st, c, monthly["fym"]) for c in cfg["stocks"]]
        st_lines.append(monthly["per_ref"])
    lines.append("지금 결과/상태: " + " / ".join(x for x in st_lines if x))
    if monthly:
        lines.append(f"뉴스 {monthly['fym']}: " + monthly["news"])
        if monthly.get("mw"):
            lines.append(monthly["mw"])
        todo.append(cfg["monthly"]["manual_line"] + ".")
        todo.append(f"현재 Phase: {cfg['phase_label']}.")
    lines.append("지금 할 일: " + " ".join(todo))
    lines.append(later_line(cfg, st, today))
    lines.append("익절/줄일 때: 고점 대비 -20% 면 30% 를 최소 3회 분할 매도, PER 8배 돌파면 50% 이하, LTA 비중 50% 미만이면 즉시 Phase 상향, "
                 "Phase 3 는 50~80%, Phase 4 는 0~30%.")
    if warn:
        lines.append("하지 말 것: 한 번에 전량 매도, 반등 기대로 원칙 미루기, 이 알림을 자동 Phase 판정으로 읽기.")
    else:
        lines.append("하지 말 것: 이 점검을 자동 Phase 판정으로 읽기, 체크리스트 트리거를 감정으로 미루기, 한 번에 전량 매도.")
    body = "\n".join(lines)
    if len(title) > 150:
        title = title[:147] + "..."
    return title, body, pri


def fit(body: str, cfg: dict, monthly: dict | None, rebuild) -> str:
    """ntfy 본문 4KB 아래로: 뉴스 제목 수를 줄여서 다시 만듦."""
    lim = cfg["monthly"]["max_bytes"]
    if len(body.encode()) <= lim or not monthly:
        return body
    for k in (1, 0):
        monthly["news"] = news_text(monthly["news_raw"], k)
        body = rebuild()
        if len(body.encode()) <= lim:
            return body
    return body


def clean(t: str) -> str:
    return re.sub(r"\s+", " ", t.replace("\u00b7", ", ").replace("\u2014", ", ").replace("\u2013", "-")).strip()


def news_text(raw: list[dict], k: int) -> str:
    parts = []
    for g in raw:
        if g.get("err"):
            parts.append(f"{g['label']} 확인 불가")
            continue
        n = len(g["items"])
        s = f"{g['label']} {n}{'+' if n >= 100 else ''}건"
        if k and g["items"]:
            s += " (" + " | ".join(clean(t)[:48] for t, _ in g["items"][:k]) + ")"
        parts.append(s)
    return "; ".join(parts) + ". 제목 키워드 집계라 원문 확인 필요."


def ops_msg(stamp: str, what: str, err: str) -> tuple[str, str, int]:
    body = "\n".join([
        "이게 뭐 알림: MEMEXIT 데이터 조회 실패 안내. 매매 신호 아님.",
        f"{stamp} | {CODES} | SK하이닉스, 삼성전자",
        f"지금 결과/상태: {what} 확인 불가 ({err[:200]}).",
        "지금 할 일: 필요하면 네이버 증권, 다음 금융, WiseReport 에서 직접 보세요.",
        "나중에: 다음 KRX 거래일 19:00 에 다시 봅니다(외국인 월 판정은 자료가 다 모일 때까지 미룸).",
        "익절/줄일 때: 이 안내로 판단을 바꾸지 않기.",
        "하지 말 것: 조회 실패를 조건 미충족으로 읽기.",
    ])
    return f"{TAG} 000660 005930 {what} 조회 실패 | 판정 보류", body, 3


def dry_seed_quiet(seeding: bool, loud: list) -> bool:
    """시드 실행에서는 종가 조회 실패만 알림(시드가 안 된 것이므로), 나머지는 다음 실행에서."""
    return seeding and not any("종가" in w for w, _ in loud)


# ---------- 실행 ----------
def run(mode: str, now: datetime, cfg: dict, io: dict | None = None) -> tuple[list, dict]:
    io = io or {}
    dry = mode == "dry"
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    today = now.strftime("%Y-%m-%d")
    ym = today[:7]
    st = json.loads(STATE_PATH.read_text(encoding="utf-8")) if STATE_PATH.exists() and "state" not in io else json.loads(json.dumps(io.get("state") or {}))
    seeding = not st.get("seeded")
    fails: list[tuple[str, str]] = []
    evs: list[dict] = []
    f_closes = io.get("closes", lambda c: closes(cfg, c, now))
    f_c52 = io.get("closes52", lambda c: closes(cfg, c, now, 270))  # 월간 참고 줄 전용(52주 종가 고점), 판정에는 안 씀
    f_eps = io.get("eps", lambda c, y: wise_eps(cfg, c, y))
    f_nm = io.get("naver_month", lambda c, m: naver_month(cfg, c, m))
    f_dm = io.get("daum_months", lambda c, ms: daum_months(cfg, c, ms))
    f_ir = io.get("ir", lambda c: ir_dates(cfg, c))
    f_news = io.get("news", lambda q, m: gnews(cfg, q, m))
    f_nper = io.get("naver_per", lambda c: naver_per(cfg, c))
    # 1) 종가, 고점 낙폭, PER
    for code in cfg["stocks"]:
        try:
            rows = f_closes(code)
            if not rows:
                raise ValueError("확정 종가 없음")
        except Exception as exc:  # noqa: BLE001
            fails.append((f"{code} 종가", str(exc)))
            continue
        e = step_drawdown(cfg, st, code, rows)
        if e:
            evs.append(e)
        d, c = rows[-1]
        try:
            try:
                eps = f_eps(code, int(d[:4]))
            except Exception as exc:  # noqa: BLE001  WiseReport 가 가끔 끊김: 10일 안의 같은 해 값이면 그대로 씀
                old = st.get("eps", {}).get(code)
                if not old or old.get("year") != int(d[:4]) or (date.fromisoformat(d) - date.fromisoformat(old["base"])).days > 10:
                    raise
                print(f"{code} WiseReport 실패, 저장값({old['base']}) 사용: {exc}")
                eps = old
            per, feps = fwd_per(c, eps, d)
            if not (0 < per < cfg["per"]["plausible_per_max"]):
                raise ValueError(f"PER 값 이상 {per}")
            st.setdefault("eps", {})[code] = dict(eps, fwd=round(feps))
            e = step_per(cfg, st, code, per, d)
            if e:
                evs.append(e)
        except Exception as exc:  # noqa: BLE001
            fails.append((f"{code} PER(WiseReport)", str(exc)))
    # 2) 외국인 월간 (달이 끝난 뒤, 자료가 다 모이면 1번)
    fym = prev_month(ym)
    todo_months = []
    if seeding:
        m = fym
        for _ in range(6):
            todo_months.insert(0, m)
            m = prev_month(m)
    elif st.get("foreign_done") != fym:
        todo_months = [fym]
    if todo_months:
        ok = True
        for code in cfg["stocks"]:
            dm: dict = {}
            try:
                dm = f_dm(code, todo_months)
            except Exception as exc:  # noqa: BLE001
                fails.append((f"{code} 다음 외국인(교차 확인)", str(exc)))
            for m in todo_months:
                try:
                    rec = f_nm(code, m)
                    need = trading_days_in(m)
                    if rec["days"] != need:
                        raise ValueError(f"{m} 행 {rec['days']}개, KRX 거래일 {need}일")
                except Exception as exc:  # noqa: BLE001
                    fails.append((f"{code} 외국인 월간", str(exc)))
                    ok = False
                    break
                e = step_foreign(cfg, st, code, m, rec, dm.get(m))
                if e and not seeding:  # 시드: 이미 충족 상태는 발송 없이 무장 해제만
                    evs.append(e)
        if ok:
            st["foreign_done"] = fym
    # 3) 실적 날짜 확정 (IR 개최 안내공시)
    for code in cfg["stocks"]:
        try:
            for q, d in f_ir(code).items():
                st.setdefault("ir", {}).setdefault(code, {})[q] = d
        except Exception as exc:  # noqa: BLE001
            fails.append((f"{code} 공시 피드", str(exc)))
    if seeding:
        for g in earn_groups(cfg, st):
            if g["due"] <= today:
                mark_earn(st, g, f"seed {today}")
        st.setdefault("oneoff_sent", {}).update({o["key"]: f"seed {today}" for o in cfg["oneoffs"] if o["date"] <= today})
        st["monthly_sent"] = ym
    else:
        for g in due_earn(cfg, st, today):
            evs.append({"kind": "earn", "g": g})
        for o in cfg["oneoffs"]:
            if o["date"] <= today and o["key"] not in st.setdefault("oneoff_sent", {}):
                evs.append({"kind": "oneoff", "o": o})
    # 4) 월간 점검
    monthly = None
    if not seeding and st.get("monthly_sent") != ym and today >= first_trading_day(ym) and is_trading_day(date.fromisoformat(today)):
        raw = []
        for g in cfg["monthly"]["news"]:
            try:
                raw.append({"label": g["label"], "items": f_news(g["q"], fym)})
            except Exception as exc:  # noqa: BLE001
                raw.append({"label": g["label"], "items": [], "err": str(exc)})
        per_ref = []
        for code in cfg["stocks"]:
            try:
                n = f_nper(code)
                per_ref.append(f"{cfg['stocks'][code]['short']} 네이버 추정PER {n.get('cnsPer', '?')}, 후행 PER {n.get('per', '?')}")
            except Exception:  # noqa: BLE001
                per_ref.append(f"{cfg['stocks'][code]['short']} 네이버 PER 확인 불가")
        hi_ref = []
        for code in cfg["stocks"]:
            nm = cfg["stocks"][code]["short"]
            try:
                rows = f_c52(code)
                cut = (date.fromisoformat(today) - timedelta(days=365)).isoformat()
                rows = [r for r in rows if r[0] >= cut]
                hd, hc = max(rows, key=lambda r: (r[1], r[0]))
                ld, lc = rows[-1]
                hi_ref.append(f"참고, 경고 기준 아님: {nm} 52주 종가 고점 {won(hc)}원({hd}) 대비 {(lc / hc - 1) * 100:+.1f}% ({ymd(ld)} 종가)")
            except Exception:  # noqa: BLE001
                hi_ref.append(f"참고, 경고 기준 아님: {nm} 52주 종가 고점 확인 불가")
        eps_ref = ", ".join(f"{cfg['stocks'][c]['short']} 12M fwd EPS {won(st.get('eps', {}).get(c, {}).get('fwd'))}원" for c in cfg["stocks"])
        monthly = {"ym": ym, "fym": fym, "news_raw": raw, "news": news_text(raw, cfg["monthly"]["news_per_group"]),
                   "hi_ref": hi_ref, "per_ref": "PER 참고(계획서 3배 주장): " + ", ".join(per_ref) + f" ({eps_ref}, WiseReport 컨센서스)", "mw": memwatch_ref()}
    out = []
    if (evs or monthly) and not seeding:
        t, b, p = build(cfg, st, evs, stamp, today, monthly)
        b = fit(b, cfg, monthly, lambda: build(cfg, st, evs, stamp, today, monthly)[1])
        out.append((t, b, p))
        for e in evs:
            if e["kind"] == "earn":
                mark_earn(st, e["g"], stamp)
            elif e["kind"] == "oneoff":
                st["oneoff_sent"][e["o"]["key"]] = stamp
        if monthly:
            st["monthly_sent"] = ym
    # 조회 실패: 종가, 외국인 월간은 바로, 나머지(PER, 공시 피드, 다음 교차 확인)는 3회 연속일 때만 cwally-ops 하루 1번
    fs = st.setdefault("fail_streak", {})
    now_keys = {w for w, _ in fails}
    for k in list(fs):
        if k not in now_keys:
            fs.pop(k)
    for k in now_keys:
        fs[k] = fs.get(k, 0) + 1
    loud = [(w, e) for w, e in fails if ("종가" in w or "외국인 월간" in w or fs[w] >= 3)]
    if loud and st.get("fail_day") != today and not dry_seed_quiet(seeding, loud):
        st["fail_day"] = today
        out.append(ops_msg(stamp, ", ".join(w for w, _ in loud)[:80], "; ".join(e for _, e in loud)))
    st["seeded"] = st.get("seeded") or stamp
    st["updated_at"] = stamp
    summary = {c: {f"{s}.{k}": st.get(s, {}).get(c, {}).get(k) for s, k in (("dd", "close"), ("dd", "peak"), ("dd", "pct"), ("dd", "armed"), ("per", "value"), ("per", "armed"))}
               for c in cfg["stocks"]}
    print(f"seeding={seeding} summary={summary} foreign_streak={ {c: foreign_streak(st.get('foreign', {}).get(c, {}).get('months', {})) for c in cfg['stocks']} } "
          f"ir={st.get('ir')} fails={fails}")
    if io.get("no_io"):
        return out, st
    for t, m, pri in out:
        print(t)
        print(m)
        print(f"({len(m.encode())} bytes, p{pri})")
        if not dry:
            publish(resolve_topic("port", cfg.get("ntfy_topic")), t, m, pri, ["warning"] if pri >= 4 else ["chart_with_upwards_trend"],
                    resolve_server(cfg.get("ntfy_server")), config_name=NAME)
            st["last_sent"] = {"at": stamp, "title": t}
    if not out:
        print("no alert")
    if dry:
        print("DRY RUN: no publish, state not written")
    else:
        STATE_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return out, st


# ---------- 시험 ----------
def selftest(cfg: dict) -> int:
    bad = []

    def check(name, got, want):
        ok = got == want
        print(("ok   " if ok else "FAIL ") + name + ("" if ok else f": got {got!r} want {want!r}"))
        if not ok:
            bad.append(name)

    # 낙폭: 고점 갱신, -20% 1번, -12% 위 회복 재무장, 새 고점 재무장
    st: dict = {}
    rows = [("2026-10-30", 300.0), ("2026-11-02", 100.0), ("2026-11-03", 90.0), ("2026-11-04", 81.0), ("2026-11-05", 80.0)]
    check("dd ignores days before start", step_drawdown(cfg, st, "X", rows[:2]) is None and st["dd"]["X"]["peak"], 100.0)
    e = step_drawdown(cfg, st, "X", rows)
    check("dd fires at -20%", (e["day"], round(e["pct"], 1)), ("2026-11-05", -20.0))
    check("dd no repeat", step_drawdown(cfg, st, "X", rows + [("2026-11-06", 75.0), ("2026-11-09", 87.0)]), None)
    check("dd still disarmed at -13%", st["dd"]["X"]["armed"], False)
    step_drawdown(cfg, st, "X", [("2026-11-10", 88.5)])
    check("dd rearm above -12%", st["dd"]["X"]["armed"], True)
    e = step_drawdown(cfg, st, "X", [("2026-11-11", 120.0), ("2026-11-12", 96.0)])
    check("dd new peak base", (st["dd"]["X"]["peak"], e and round(e["pct"])), (120.0, -20))
    check("dd skips old days", step_drawdown(cfg, st, "X", [("2026-11-12", 10.0)]), None)
    # 외국인 월간
    st = {}
    for m, v in [("2026-07", -5), ("2026-08", -3)]:
        check(f"foreign {m} no fire", step_foreign(cfg, st, "X", m, {"shares": v, "amt": v, "days": 20}, v), None)
    e = step_foreign(cfg, st, "X", "2026-09", {"shares": -1, "amt": -1, "days": 20}, -2)
    check("foreign 3rd month fires", e and e["streak"], 3)
    check("foreign 4th month no repeat", step_foreign(cfg, st, "X", "2026-10", {"shares": -1, "amt": -1, "days": 20}, -1), None)
    step_foreign(cfg, st, "X", "2026-11", {"shares": 5, "amt": 5, "days": 20}, 4)
    check("foreign rearm on net buy", st["foreign"]["X"]["armed"], True)
    for m in ("2026-12", "2027-01"):
        step_foreign(cfg, st, "X", m, {"shares": -1, "amt": -1, "days": 20}, -1)
    check("foreign source disagree blocks", step_foreign(cfg, st, "X", "2027-02", {"shares": -1, "amt": -1, "days": 20}, 3), None)
    check("foreign fires next agreeing month", (step_foreign(cfg, st, "X", "2027-03", {"shares": -1, "amt": -1, "days": 20}, -1) or {}).get("streak"), 4)
    # PER
    st = {}
    eps = {"y0": 100.0, "y1": 200.0}
    per, feps = fwd_per(1000.0, eps, "2026-11-03")
    check("fwd eps weighting", round(feps), round(100 * 58 / 365 + 200 * 307 / 365))
    check("fwd per 12/31 uses next year", round(fwd_per(1000.0, eps, "2026-12-31")[0], 2), 5.0)
    check("per below 8 none", step_per(cfg, st, "X", 7.9, "2026-11-03"), None)
    check("per >= 8 fires", (step_per(cfg, st, "X", 8.0, "2026-11-04") or {}).get("kind"), "per")
    check("per same day no rerun", step_per(cfg, st, "X", 9.0, "2026-11-04"), None)
    step_per(cfg, st, "X", 7.6, "2026-11-05")
    check("per 7.6 stays disarmed", st["per"]["X"]["armed"], False)
    step_per(cfg, st, "X", 7.4, "2026-11-06")
    check("per rearm < 7.5", st["per"]["X"]["armed"], True)
    # 실적 묶음
    st = {}
    gs = earn_groups(cfg, st)
    check("earn groups (Q4 merged, Q1 split, Q2 Q3 merged)", [g["id"] for g in gs],
          ["2026Q4:000660+005930", "2027Q1:000660", "2027Q1:005930", "2027Q2:000660+005930", "2027Q3:000660+005930"])
    check("earn due next trading day", [g["due"] for g in gs], ["2027-01-29", "2027-04-23", "2027-04-30", "2027-07-30", "2027-10-29"])
    check("earn not due on call day", due_earn(cfg, st, "2027-01-28"), [])
    d = due_earn(cfg, st, "2027-01-29")
    check("earn due", [g["id"] for g in d], ["2026Q4:000660+005930"])
    mark_earn(st, d[0], "x")
    check("earn no repeat", due_earn(cfg, st, "2027-02-01"), [])
    st = {"ir": {"000660": {"2026Q4": "2027-01-21"}}}
    gs = earn_groups(cfg, st)
    check("earn confirmed date splits group", [(g["id"], g["due"]) for g in gs[:2]], [("2026Q4:000660", "2027-01-22"), ("2026Q4:005930", "2027-01-29")])
    check("earn stale skipped", due_earn(cfg, {}, "2027-02-20")[:1], [])
    check("next trading after holiday (2027-02-05 -> 2/10)", next_trading_after("2027-02-05"), "2027-02-10")
    check("first trading day 2027-01", first_trading_day("2027-01"), "2027-01-04")
    check("first trading day 2026-12", first_trading_day("2026-12"), "2026-12-01")
    check("prev month rollover", (prev_month("2027-01"), prev_month("2026-12")), ("2026-12", "2026-11"))
    check("trading days 2026-10", trading_days_in("2026-10"), 20)
    # IR 공시 파싱
    fake = [{"title": "에스케이하이닉스(주) (정정)기업설명회(IR) 개최(안내공시)", "contents": "1. 일시 및 장소 일시 2027-01-28 09:00 3. 개최목적 2026년 4분기 경영실적 발표"},
            {"title": "삼성전자(주) 기업설명회(IR) 개최(안내공시)", "contents": "일시 2027-01-29 10:00 개최목적 2026년 연간 및 4분기 경영실적 발표"},
            {"title": "x 기업설명회(IR) 개최(안내공시)", "contents": "일시 2026-11-20 개최목적 국내 NDR"}]
    orig = globals()["get"]
    globals()["get"] = lambda url, referer=None, tries=3: json.dumps(fake).encode()
    try:
        check("ir parse", ir_dates(cfg, "000660"), {"2026Q4": "2027-01-28"} | {"2026Q4": "2027-01-28"})
        fake.pop(0)
        check("ir parse 연간 및 4분기", ir_dates(cfg, "005930"), {"2026Q4": "2027-01-29"})
    finally:
        globals()["get"] = orig
    # 전체 흐름 (가짜 입력): 시드 조용, 월간 첫 거래일 1번, 같은 날 단발 묶음, 경고 문구
    days = [("2026-11-02", 100.0), ("2026-11-03", 95.0)]
    io = {"no_io": True, "state": {},
          "closes": lambda c: days, "eps": lambda c, y: {"y0": 30.0, "y1": 40.0, "year": y, "base": "2026-11-02"},
          "naver_month": lambda c, m: {"shares": -10, "amt": -1000, "days": trading_days_in(m)},
          "daum_months": lambda c, ms: {m: -9 for m in ms}, "ir": lambda c: {},
          "closes52": lambda c: [("2025-11-01", 999.0), ("2026-06-22", 200.0), ("2026-12-01", 70.0)],
          "news": lambda q, m: [("CXMT 증설 기사 제목 예시 \u00b7 끝", "01 Nov 2026")] * 3, "naver_per": lambda c: {"per": "9.9배", "cnsPer": "4.8배"}}
    out, st = run("dry", datetime(2026, 11, 3, 19, 0, tzinfo=KST), cfg, io)
    check("seed silent", out, [])
    check("seed foreign streak 6 disarmed", (foreign_streak(st["foreign"]["000660"]["months"]), st["foreign"]["000660"]["armed"]), (6, False))
    check("seed monthly marked", st["monthly_sent"], "2026-11")
    io["state"] = st
    out, st = run("dry", datetime(2026, 11, 4, 19, 0, tzinfo=KST), cfg, io)
    check("calm day silent", out, [])
    days.append(("2026-12-01", 70.0))
    io["state"] = st
    out, st = run("dry", datetime(2026, 12, 1, 19, 0, tzinfo=KST), cfg, io)
    check("12/1 one batched message", len(out), 1)
    t, b, p = out[0]
    check("12/1 title has dd + monthly, p4", ("고점 대비 -30%" in t, "월간 점검 2026-12" in t, p, t.startswith("[MEMEXIT] 000660 005930 ")), (True, True, 4, True))
    check("body six parts", all(k in b for k in ("이게 뭐 알림:", "지금 결과/상태:", "지금 할 일:", "나중에:", "익절/줄일 때:", "하지 말 것:")), True)
    check("no middle dot or em-dash", ("\u00b7" in b + t) or ("\u2014" in b + t), False)
    check("no buy wording in warning", [w for w in BAD_WORDS if w in b], [])
    check("monthly mentions phase + manual", ("Phase 1 (자동 판정 아님)" in b, "분기 점검(수동)" in b, "LTA 비중" in b), (True, True, True))
    check("under 4KB", len(b.encode()) <= cfg["monthly"]["max_bytes"], True)
    check("52w high reference line (older than 365d ignored)", "참고, 경고 기준 아님: 하이닉스 52주 종가 고점 200원(2026-06-22) 대비 -65.0% (12/1 종가)" in b, True)
    check("dd basis unchanged (peak 100 since 11/2)", st["dd"]["000660"]["peak"], 100.0)
    io["state"] = st
    out, st = run("dry", datetime(2026, 12, 2, 19, 0, tzinfo=KST), cfg, io)
    check("12/2 silent (no repeats)", out, [])
    io["state"] = json.loads(json.dumps(st))
    io["state"]["monthly_sent"] = "2027-06"
    out, _ = run("dry", datetime(2027, 7, 1, 19, 0, tzinfo=KST), cfg, io)
    check("2027-07-01 phase2 + monthly merged", len(out) == 1 and "Phase 2 전환 점검" in out[0][0] and "월간 점검 2027-07" in out[0][0], True)
    check("phase2 body lists triggers", "CXMT 월 50K" in out[0][1] and "2분기 연속" in out[0][1], True)
    io["state"] = json.loads(json.dumps(st))
    io["state"]["monthly_sent"] = "2027-01"
    out, _ = run("dry", datetime(2027, 1, 29, 19, 0, tzinfo=KST), cfg, io)
    check("2027-01-29 earnings reminder", len(out) == 1 and "2026년 4분기 실적 점검" in out[0][0] and cfg["earnings"]["text"] in out[0][1], True)
    # WiseReport 끊김: 저장 EPS 사용, 3회 연속 실패 전엔 ops 없음
    io2 = dict(io, state=json.loads(json.dumps(st)))
    io2["eps"] = lambda c, y: (_ for _ in ()).throw(OSError("ssl eof"))
    io2["state"]["eps"] = {c: {"y0": 30.0, "y1": 40.0, "year": 2026, "base": "2026-12-01"} for c in cfg["stocks"]}
    out, s2 = run("dry", datetime(2026, 12, 3, 19, 0, tzinfo=KST), cfg, io2)
    check("eps cache used, no ops", (out, s2["per"]["000660"]["day"]), ([], "2026-12-01"))
    io2["state"] = json.loads(json.dumps(st))
    io2["state"].pop("eps", None)
    r = []
    for d in (3, 4, 7):
        out, io2["state"] = run("dry", datetime(2026, 12, d, 19, 0, tzinfo=KST), cfg, io2)
        r.append(len(out))
    check("soft fail ops only on 3rd straight run", r, [0, 0, 1])
    print("selftest " + ("PASS" if not bad else f"FAIL ({len(bad)})"))
    return 0 if not bad else 1


def confirm(cfg: dict) -> int:
    st = json.loads(STATE_PATH.read_text(encoding="utf-8")) if STATE_PATH.exists() else {}
    now = datetime.now(KST)
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    fs = {c: foreign_streak(st.get("foreign", {}).get(c, {}).get("months", {})) for c in cfg["stocks"]}
    body = "\n".join([
        "이게 뭐 알림: MEMEXIT(메모리 4단계 Exit 계획) 알림 설치 확인입니다. 매매 신호 아님.",
        f"{stamp} | {CODES} | SK하이닉스, 삼성전자",
        "지금 결과/상태: " + " / ".join(stock_line(cfg, st, c) for c in cfg["stocks"])
        + f" / 외국인 연속 순매도 하이닉스 {fs['000660']}개월, 삼성 {fs['005930']}개월(이미 3개월 넘어 시드, 순매수 달 뒤 다시 3개월이면 알림).",
        "지금 할 일: 없음. KRX 거래일 19:00 에 고점 대비 -20%(p4), 외국인 3개월 연속 순매도(p3), 12M fwd PER 8배(p3)를 상태가 바뀔 때만 봅니다.",
        "나중에: 매달 첫 거래일 월간 점검(다음 2026-12-01), 실적 다음 날 리마인더(다음 2027-01-29 추정), 2027-07-01 Phase 2 전환 점검, "
        "2027-12-01 연장 점검. DRAM 계약가 상승률, LTA 비중, DIO, CSP CAPEX 는 자동 판정 안 함(월간 점검 수동 줄).",
        "익절/줄일 때: 해당 없음.",
        "하지 말 것: 이 설치 확인을 매매 신호로 읽기.",
    ])
    title = f"{TAG} 000660 005930 메모리 Exit 계획 설치 확인 | 매매 신호 아님"
    print(title)
    print(body)
    publish(resolve_topic("port", cfg.get("ntfy_topic")), title, body, 3, ["white_check_mark"], resolve_server(cfg.get("ntfy_server")),
            config_name=NAME)
    return 0


def main() -> int:
    args = sys.argv[1:]
    mode = (args[0] if args else "auto").lower()
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    if mode == "selftest":
        return selftest(cfg)
    if mode == "confirm":
        return confirm(cfg)
    if mode not in {"auto", "dry"}:
        print(__doc__)
        return 2
    now = datetime.now(KST)
    if "--as-of" in args:
        now = datetime.strptime(args[args.index("--as-of") + 1], "%Y-%m-%d %H:%M").replace(tzinfo=KST)
    ends = cfg.get("ends_kst")
    if ends and now > datetime.strptime(ends, "%Y-%m-%d %H:%M").replace(tzinfo=KST):
        print(f"{NAME} ended {ends} KST; silent")
        return 0
    try:
        run(mode, now, cfg)
    except Exception as exc:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        if mode != "dry":
            t, b, p = ops_msg(now.strftime("%Y-%m-%d %H:%M KST"), "스크립트", repr(exc))
            publish(resolve_topic("ops"), t, b, p, ["warning"], resolve_server(cfg.get("ntfy_server")), config_name=NAME)
    return 0


if __name__ == "__main__":
    sys.exit(main())
