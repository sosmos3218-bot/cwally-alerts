#!/usr/bin/env python3
"""[KR-RATE] 국고채 금리 조건 알림 (148070 국고10년 ETF 판단 보조). 매매 신호 아님, 자동 주문 아님.

기존 k203780 19:00 슬롯(cron-job.org 정시 dispatch + GitHub cron 백업)에 붙어 KRX 거래일에만 판정한다.
1) 국고채 10년 종가 >= yield10.trigger_pct -> '148070 금리 피크아웃 확인 후 검토(상승 중에는 추가매수 보류)' 1회.
   종가 < rearm_below_pct 면 다시 무장. 10년 종가 >= hold_ceiling_pct(4.50%) 이거나 [MACRO-IR] 148070 <= 104,000원 경고가
   켜져 있으면 추가매수 보류 문구만 쓴다(2026-10-26 사용자 요청, [MACRO-IR] 와 충돌 방지).
2) 금통위 결정일: 기준금리 결정(동결/인상/인하, 새 수준)과 국고3년/10년 종가, 하루 변화만 요약 (판단 없음).
   근원물가(농산물및석유류제외 전년동월비): 새 달 수치가 처음 보이는 실행에서 1회, 2.5% 미만이면 강조.
3) 신용 스프레드(회사채 AA- 3년 - 국고채 3년)가 20거래일 전보다 widen_trigger_bp 넘게 확대 -> 1회.
   확대폭 < rearm_below_bp 면 다시 무장. 일별 값은 state hist 에 저장.
데이터: 한국은행 ECOS (KOFIA 최종호가, 기존 ECOS_API_KEY secret, 없으면 sample 키), 네이버 시장지표(대체, 출처 표시),
한국은행 금통위 일정/보도자료/기준금리 표(scripts/bok_mpc.py 함수 재사용, 읽기 전용. 2026-10-26 ktb148070_monitor 에서 이동).
값의 날짜가 판정 거래일과 같을 때만 쓴다. 없으면 '확인 불가', 조회 실패는 cwally-ops.
금통위 요약 보강(2026-10-24 승인, 본문만): 결정과 새 기준금리(ECOS 722Y001 교차확인), 소수의견 수와 방향(결정문, 없으면 기사 제목),
연속 인상 여부(기준금리 변경 이력으로 계산), 다음 회의 신호 문구(결정문 인용), 판단 변경 조건(재동결 + 근원 CPI 둔화 또는 성장 둔화).
modes: auto | dry(발송 없음, state 안 씀, --as-of "YYYY-MM-DD HH:MM", --mpc-date YYYY-MM-DD 가능) | selftest(가짜 값으로 조건 검사, 네트워크 없음)
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic, route_topic  # noqa: E402
from kr_calendar import KST, is_trading_day, prev_trading_day, session_day  # noqa: E402
import bok_mpc as ktb  # noqa: E402  (읽기 전용: 한은 결정문/기준금리 표/회의 페이지/뉴스, scripts/bok_mpc.py)

NAME = "krrate"
CFG_PATH = ROOT / "configs" / f"{NAME}.json"
STATE_PATH = ROOT / "state" / f"{NAME}.json"
NA = "확인 불가"
UA = {"User-Agent": "Mozilla/5.0 (cwally-alerts; +mailto:sosmos3218@gmail.com)"}
DEC_KO = {"hold": "동결", "cut": "인하", "hike": "인상"}
ECOS_ITEMS = {"y3": "010200000", "y10": "010210000", "aa": "010300000"}


# ---------------- io ----------------
def load_state() -> dict:
    base = {"updated_at": None, "seeded": False, "y10": {"armed": True, "last_day": None, "fired_day": None},
            "spread": {"armed": True, "last_day": None, "fired_day": None, "seeded": False},
            "cpi_last_month": None, "mpc_done": [], "hist": {}, "fail_notice": {}}
    if STATE_PATH.exists():
        try:
            base.update(json.loads(STATE_PATH.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass
    return base


def get(url: str, timeout: int = 25) -> str:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def num(v) -> float | None:
    try:
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


# ---------------- 데이터 ----------------
def ecos(stat: str, cycle: str, start: str, end: str, item: str) -> dict[str, float]:
    """{TIME: value}. 키가 없으면 sample 키로 10행씩 나눠 받는다."""
    key = os.environ.get("ECOS_API_KEY") or ""
    step = 100 if key else 10
    key = key or "sample"
    out: dict[str, float] = {}
    i = 1
    while i <= 400:
        url = (f"https://ecos.bok.or.kr/api/StatisticSearch/{urllib.parse.quote(key)}/json/kr/{i}/{i + step - 1}/"
               f"{stat}/{cycle}/{start}/{end}/{item}")
        d = json.loads(get(url))
        if "StatisticSearch" not in d:
            res = d.get("RESULT") or {}
            if res.get("CODE") == "INFO-200" and i > 1:  # 더 없음
                break
            if res.get("CODE") == "INFO-200":
                return out
            raise RuntimeError(f"ECOS {stat}/{item} {res.get('CODE')} {str(res.get('MESSAGE'))[:60]}")
        rows = d["StatisticSearch"].get("row") or []
        for r in rows:
            v = num(r.get("DATA_VALUE"))
            if v is not None:
                out[str(r["TIME"])] = v
        total = int(d["StatisticSearch"].get("list_total_count") or 0)
        i += step
        if i > total:
            break
    return out


def ecos_daily(item: str, start: date, end: date) -> dict[str, float]:
    raw = ecos("817Y002", "D", start.strftime("%Y%m%d"), end.strftime("%Y%m%d"), item)
    return {f"{k[:4]}-{k[4:6]}-{k[6:8]}": v for k, v in raw.items()}


def naver_bond(code: str) -> dict[str, float]:
    raw = json.loads(get("https://m.stock.naver.com/front-api/marketIndex/prices?category=bond&reutersCode="
                         f"{urllib.parse.quote(code)}&page=1&pageSize=30"))
    out = {}
    for r in (raw or {}).get("result") or []:
        d = str(r.get("localTradedAt") or "")[:10]
        v = num(r.get("closePrice"))
        if len(d) == 10 and v is not None:
            out[d] = v
    return out


def naver_aa() -> dict[str, float]:
    raw = json.loads(get("https://m.stock.naver.com/front-api/marketIndex/prices?category=domesticInterest"
                         "&reutersCode=KFIA103009&page=1&pageSize=30"))
    out = {}
    for r in (raw or {}).get("result") or []:
        d = str(r.get("localTradedAt") or "")[:10]
        v = num(r.get("closePrice"))
        if len(d) == 10 and v is not None:
            out[d] = v
    return out


def cpi_yoy(end_month: str) -> dict:
    """{'month': 'YYYY-MM', 'qb': yoy, 'qb_prev': yoy 전월, 'db': yoy} 최신 달 기준."""
    y, m = int(end_month[:4]), int(end_month[5:7])
    start = f"{y - 2}{m:02d}"
    qb = ecos("901Y010", "M", start, f"{y}{m:02d}", "QB")
    db = ecos("901Y010", "M", start, f"{y}{m:02d}", "DB")
    if not qb:
        raise RuntimeError("ECOS 901Y010/QB 비어 있음")
    last = max(qb)

    def yoy(series: dict, t: str) -> float | None:
        p = f"{int(t[:4]) - 1}{t[4:]}"
        return round((series[t] / series[p] - 1) * 100, 1) if t in series and p in series and series[p] else None

    lm = int(last[4:])
    prev_t = f"{int(last[:4]) - (1 if lm == 1 else 0)}{(12 if lm == 1 else lm - 1):02d}"
    return {"month": f"{last[:4]}-{last[4:]}", "qb": yoy(qb, last), "qb_prev": yoy(qb, prev_t), "db": yoy(db, last)}


def mpc_schedule(cfg: dict, year: int) -> tuple[list[str], list[str]]:
    """(날짜 목록, 메모). config 목록 + 한은 페이지(올해, 내년)."""
    days = set(cfg["mpc"]["schedule"])
    notes = []
    for y in (year, year + 1):
        try:
            html = get(cfg["sources"]["bok_meeting_page"].format(year=y))
            found = {f"{y}-{mo}-{d}" for mo, d in re.findall(r'<th scope="row">\s*(\d{2})월\s*(\d{2})일', html)}
            days |= found
            if not found:
                notes.append(f"{y}년 일정 한은 페이지에 아직 없음")
        except Exception as exc:  # noqa: BLE001
            notes.append(f"{y}년 한은 일정 페이지 조회 실패({type(exc).__name__})")
    return sorted(days), notes


def mpc_decision(cfg: dict, d: str) -> dict:
    """bok_mpc 의 결정 판정 함수(원래 ktb148070)를 이 회의 날짜로 다시 쓴다."""
    y, mo, dd = d.split("-")
    md = datetime.strptime(d, "%Y-%m-%d")
    kcfg = {
        "meeting_date": d,
        "current_base_rate": None,
        "rules": cfg["mpc"]["rules"],
        "sources": {
            "bok_news_list": cfg["sources"]["bok_news_list"],
            "bok_release_title_re": rf"통화정책방향\s*\(\s*{y}\s*\.\s*0?{int(mo)}\s*\.\s*0?{int(dd)}\s*\)",
            "bok_base_rate_table": cfg["sources"]["bok_base_rate_table"],
            "bok_meeting_page": cfg["sources"]["bok_meeting_page"].format(year=y),
            "news_rss_query": f"한국은행 기준금리 금통위 after:{(md - timedelta(days=1)):%Y-%m-%d} before:{(md + timedelta(days=2)):%Y-%m-%d}",
        },
    }
    table = ktb.bok_base_table(kcfg)
    ref = table.get("latest_rate") if table.get("status") == "ok" else None
    kcfg["current_base_rate"] = ref if ref is not None else 0.0
    rel = ktb.bok_release(kcfg)
    mtg = ktb.bok_meeting(kcfg)
    news = ktb.news_source(kcfg, kcfg["current_base_rate"])
    dec = ktb.resolve_decision(kcfg, rel, table, mtg, news)
    try:  # 네이버 기준금리(발표 시각 포함) 교차확인
        rows = json.loads(get("https://stock.naver.com/api/securityService/marketindex/standardInterest"))
        r = next(x for x in rows if x.get("reutersCode") == "KROCRT=ECIX")
        dec["naver"] = (str(r.get("localTradedAt"))[:10], num(r.get("closePrice")))
    except Exception:  # noqa: BLE001
        dec["naver"] = None
    return dec



# ---------------- 금통위 요약 보강 (2026-10-24 12:44 KST 승인: 본문만) ----------------
MPC_MAX_BYTES = 3900
DISSENT_ALL_RE = re.compile(r"금융통화위원\s*(\d)\s*명\s*(?:모두|전원)\s*찬성|만장일치")
DISSENT_SEG_RE = re.compile(r"금번\s*기준금리\s*\S+\s*결정에\s*대해서?는?.{0,600}?(?:다\s*\.|$)")
DISSENT_NAME_RE = re.compile(r"([가-힣]{2,4})\s*위원(?:은|이|과|,)")
DISSENT_RATE_RE = re.compile(r"([가-힣]{2,4})\s*위원은\s*기준금리를\s*(\d\.\d{1,2})\s*%\s*로")
GUIDE_KEYS = ("추가 인상", "추가 인하", "인상의 시기", "인하의 시기", "긴축 기조", "완화 기조", "긴축기조", "완화기조",
              "파급영향", "파급 영향", "향후 통화정책", "금리인상", "금리인하", "인상 여부", "인하 여부")
EN_NUM = {"one": 1, "two": 2, "three": 3, "four": 4, "a": 1, "single": 1}


def ko_clean(s: str) -> str:
    """원문 인용용: 가운뎃점류와 줄표를 쉼표/하이픈으로, ' .' 공백 정리."""
    s = re.sub(r"\s*[\u00b7\u2027\u30fb\u2219]\s*", ", ", s)
    s = s.replace("\u2014", "-").replace("\u2013", "-")
    s = re.sub(r"\s+([.,)])", r"\1", s)
    s = re.sub(r"([(])\s+", r"\1", s)
    return re.sub(r"\s+", " ", s).strip()


def release_text(url: str | None) -> str | None:
    if not url:
        return None
    t = ktb.text_of(ktb.fetch(url, 25))
    i = t.find("금융통화위원회는")
    j = t.find("목록 연관자료")
    return re.sub(r"\s+", " ", t[i if i >= 0 else 0: j if j > 0 else len(t)])


def dissent_from_text(t: str, rate: float, prev: float | None = None) -> dict | None:
    """결정문의 '금번 기준금리 ... 결정에 대해서는 ...' 문단. {'count', 'dir', 'text'} 또는 None(문단 없음)."""
    if not t:
        return None
    m = DISSENT_ALL_RE.search(t)
    seg_m = DISSENT_SEG_RE.search(t)
    seg = seg_m.group(0) if seg_m else ""
    if m and (not seg or m.start() >= (seg_m.start() if seg_m else 0)) and not DISSENT_RATE_RE.search(seg):
        n = m.group(1) if m.lastindex else None
        return {"count": 0, "dir": None, "text": f"소수의견 0명(금통위원 {n}명 모두 찬성)" if n else "소수의견 0명(만장일치)"}
    if not seg:
        return None
    hits = DISSENT_RATE_RE.findall(seg)
    if hits:
        dirs = []
        base = prev if prev is not None else rate  # 직전 금리 대비: 같으면 동결 주장
        for _, r in hits:
            r = float(r)
            dirs.append("인상" if r > base else ("인하" if r < base else "동결"))
        names = ", ".join(f"{n} 위원 {float(r):.2f}%" for n, r in hits)
        d = sorted(set(dirs))
        return {"count": len(hits), "dir": "/".join(d), "text": f"소수의견 {len(hits)}명({'/'.join(d)} 주장: {names})"}
    names = [n for n in DISSENT_NAME_RE.findall(seg) if n not in ("금융통화", "통화")]
    if names and "소수의견" in seg:
        return {"count": len(set(names)), "dir": None, "text": f"소수의견 {len(set(names))}명(방향은 결정문에 없음)"}
    return None


def dissent_from_titles(items: list[tuple[str, str]]) -> dict | None:
    """기사 제목 [(제목, 언론사)] 에서 소수의견 수와 방향."""
    for title, src in items:
        t = title
        if "만장일치" in t or re.search(r"\bunanimous", t, re.I):
            return {"count": 0, "dir": None, "text": f"소수의견 0명(만장일치, 기사: {src})"}
        m = re.search(r"(인상|인하|동결)\s*소수의견\s*(\d)\s*명|소수의견\s*(\d)\s*명[^,.]{0,12}?(인상|인하|동결)?", t)
        if m:
            n = int(m.group(2) or m.group(3))
            dr = m.group(1) or m.group(4)
            return {"count": n, "dir": dr, "text": f"소수의견 {n}명" + (f"({dr} 주장)" if dr else "(방향은 기사 제목에 없음)") + f", 기사: {src}"}
        m = re.search(r"(\d)\s*명\s*(?:이|은)?\s*[\"“']?\s*(인상|인하|동결)\s*(?:주장|소수의견|의견)", t)
        if m:
            return {"count": int(m.group(1)), "dir": m.group(2), "text": f"소수의견 {m.group(1)}명({m.group(2)} 주장), 기사: {src}"}
        m = re.search(r"\b(one|two|three|four|a|single)\s+(?:board member\s+)?dissent", t, re.I)
        if m:
            n = EN_NUM[m.group(1).lower()]
            dr = "인하" if re.search(r"\bcut", t, re.I) else ("인상" if re.search(r"\bhike|raise", t, re.I) else None)
            return {"count": n, "dir": dr, "text": f"소수의견 {n}명" + (f"({dr} 주장)" if dr else "(방향 확인 안 됨)") + f", 기사: {src}"}
    return None


def news_titles(query: str, lang: str = "ko") -> list[tuple[str, str]]:
    gl = "hl=ko&gl=KR&ceid=KR:ko" if lang == "ko" else "hl=en-US&gl=US&ceid=US:en"
    xml = ktb.fetch(f"https://news.google.com/rss/search?q={urllib.parse.quote(query)}&{gl}", 20)
    out = []
    for it in ktb.parse_rss(xml):
        t = it.get("title") or ""
        src = (it.get("source") or (t.rsplit(" - ", 1)[1] if " - " in t else "")).strip() or "언론사 미상"
        out.append((re.sub(r"\s+-\s+[^-]+$", "", t), src))
    return out


def guidance_quote(t: str | None, limit: int = 200) -> str | None:
    if not t:
        return None
    sents = [x.strip() for x in re.split(r"(?<=다)\s*\.\s*", t) if x.strip()]
    picks = [x for x in sents if any(k in x for k in GUIDE_KEYS) and "통화정책" in x and "앞으로" not in x[:5] or
             ("향후 통화정책" in x)]
    picks += [x for x in sents if x not in picks and any(k in x for k in GUIDE_KEYS)]
    if not picks:
        return None
    q = ko_clean(picks[0]) + "."
    if len(q) < limit and len(picks) > 1:
        extra = ko_clean(picks[1]) + "."
        if len(q) + 1 + len(extra) <= limit:
            q = q + " " + extra
    return q if len(q) <= limit else q[: limit - 3].rstrip() + "..."


def meeting_dates(cfg: dict, years: list[int]) -> list[str]:
    days = set(cfg["mpc"]["schedule"])
    for y in years:
        try:
            html = get(cfg["sources"]["bok_meeting_page"].format(year=y))
            days |= {f"{y}-{mo}-{d}" for mo, d in re.findall(r'<th scope="row">\s*(\d{2})월\s*(\d{2})일', html)}
        except Exception:  # noqa: BLE001
            pass
    return sorted(days)


def meeting_path(meetings: list[str], changes: list[tuple[str, float]], upto: str) -> list[tuple[str, str, float | None]]:
    """회의별 (날짜, hike|cut|hold, 결정 뒤 금리). changes = 기준금리 변경 [(날짜, 새 금리)] (아무 순서)."""
    ch = sorted(changes)
    out = []
    for d in [m for m in sorted(meetings) if m < upto]:
        before = [r for x, r in ch if x < d]
        at = [r for x, r in ch if x == d]
        prev = before[-1] if before else None
        if at and prev is not None:
            out.append((d, "hike" if at[0] > prev else ("cut" if at[0] < prev else "hold"), at[0]))
        else:
            out.append((d, "hold", prev))
    return out


def path_text(past: list[tuple[str, str, float | None]], cur: str, d: str) -> str:
    """연속 인상 여부 등 결정 흐름 한 줄 (과거 회의 + 이번 결정)."""
    seq = past + [(d, cur, None)]
    yr = d[:4]
    n_hike = sum(1 for x, k, _ in seq if x.startswith(yr) and k == "hike")
    n_cut = sum(1 for x, k, _ in seq if x.startswith(yr) and k == "cut")
    run = 0
    for _, k, _ in reversed(seq):
        if k != cur:
            break
        run += 1
    prior = seq[: len(seq) - run]
    last_move = next(((x, k, r) for x, k, r in reversed(prior if cur == "hold" else past) if k != "hold"), None)
    if cur in ("hike", "cut"):
        word = DEC_KO[cur]
        if run >= 2:
            s = f"{run}회 연속 {word}"
        else:
            holds = 0
            for _, k, _ in reversed(past):
                if k != "hold":
                    break
                holds += 1
            before = past[len(past) - holds - 1][1] if len(past) > holds else None
            if holds and before == cur:
                s = f"동결 {holds}회 후 재{word}"
            elif holds and before:
                s = f"동결 {holds}회 후 {word} 전환({DEC_KO[before]} 뒤)"
            elif before and before != cur:
                s = f"{DEC_KO[before]} 직후 {word} 전환"
            elif holds:
                s = f"동결 {holds}회 후 {word}"
            else:
                s = f"{word}"
        cnt = n_hike if cur == "hike" else n_cut
        return f"{s}, 올해 {cnt}번째 {word}"
    s = f"{run}회 연속 동결 (재동결)" if run >= 2 else (f"첫 동결(직전 회의 {DEC_KO[prior[-1][1]]})" if prior else "동결")
    if last_move:
        s += f", 마지막 변경 {last_move[0]} {DEC_KO[last_move[1]]}" + (f"({last_move[2]:.2f}%)" if last_move[2] is not None else "")
    return s + f", 올해 인상 {n_hike}회, 인하 {n_cut}회"


def gdp_qq() -> dict:
    """한은 실질 GDP 계절조정 전기비 (ECOS 200Y102/10111) 최근 2분기."""
    y = date.today().year
    raw = ecos("200Y102", "Q", f"{y - 2}Q1", f"{y}Q4", "10111")
    ks = sorted(raw)
    if not ks:
        raise RuntimeError("ECOS 200Y102 비어 있음")
    return {"q": ks[-1], "v": raw[ks[-1]], "q_prev": ks[-2] if len(ks) > 1 else None, "v_prev": raw[ks[-2]] if len(ks) > 1 else None}


def judge_line(cur: str, path: list[tuple[str, str, float | None]], cpi: dict | None, gdp: dict | None) -> str:
    """판단 변경 조건 = 재동결 + (근원 CPI 둔화 또는 성장 둔화)."""
    prev = path[-1][1] if path else None
    re_hold = cur == "hold" and prev == "hold"
    rh = "예" if re_hold else ("아니오(첫 동결)" if cur == "hold" else f"아니오({DEC_KO.get(cur, cur)})")
    if cpi and cpi.get("qb") is not None and cpi.get("qb_prev") is not None:
        cd = cpi["qb"] < cpi["qb_prev"]
        y, m = cpi["month"].split("-")
        ct = f"근원 CPI(농산물및석유류제외) {int(m)}월 {cpi['qb']:.1f}% (전월 {cpi['qb_prev']:.1f}%) {'둔화' if cd else '둔화 아님'}"
    else:
        cd, ct = None, f"근원 CPI {NA}"
    if gdp and gdp.get("v") is not None and gdp.get("v_prev") is not None:
        gd = gdp["v"] < gdp["v_prev"]
        gt = f"실질 GDP 전기비 {gdp['q']} {gdp['v']:.1f}% ({gdp['q_prev']} {gdp['v_prev']:.1f}%) {'둔화' if gd else '둔화 아님'}"
    else:
        gd, gt = None, f"실질 GDP 전기비 {NA}"
    if not re_hold:
        verdict = "미충족(재동결 아님)"
    elif cd or gd:
        verdict = "충족: 채권 ETF 판단 변경 검토"
    elif cd is None and gd is None:
        verdict = "판정 보류(물가, 성장 둘 다 확인 불가)"
    elif cd is None or gd is None:
        verdict = "판정 보류(하나 확인 불가, 나머지는 둔화 아님)"
    else:
        verdict = "미충족(물가, 성장 둔화 없음)"
    return f"판단 변경 조건(재동결 + 근원 CPI 둔화 또는 성장 둔화): 재동결 {rh}, {ct}, {gt} -> {verdict}"


def ecos_base_rate(start: date, end: date) -> dict[str, float]:
    raw = ecos("722Y001", "D", start.strftime("%Y%m%d"), end.strftime("%Y%m%d"), "0101000")
    return {f"{k[:4]}-{k[4:6]}-{k[6:8]}": v for k, v in raw.items()}


def changes_from_daily(series: dict[str, float]) -> list[tuple[str, float]]:
    out, prev = [], None
    for d in sorted(series):
        if prev is not None and series[d] != prev:
            out.append((d, series[d]))
        if prev is None:
            out.append((d, series[d]))  # 시작 수준(변경일 아님, 이전 금리 기준용)
        prev = series[d]
    return out


def mpc_extras(cfg: dict, d: str, dec: dict) -> dict:
    """결정 확인(ECOS), 소수의견, 결정 흐름, 다음 회의 신호, 판단 변경 조건. 실패한 항목은 '확인 불가'."""
    ex: dict = {"src_ok": [], "src_fail": []}
    md = date.fromisoformat(d)
    # 1) ECOS 기준금리 (결정일 값과 전날 값)
    eser: dict[str, float] = {}
    try:
        eser = ecos_base_rate(md - timedelta(days=380), md)
        ex["src_ok"].append("ECOS 722Y001")
    except Exception as exc:  # noqa: BLE001
        ex["src_fail"].append(f"ECOS 722Y001 {type(exc).__name__}")
    if d in eser:
        before = [eser[k] for k in sorted(eser) if k < d]
        p = before[-1] if before else None
        er = eser[d]
        edec = "hold" if p is None or er == p else ("hike" if er > p else "cut")
        same = abs(er - dec["rate"]) < 0.001 and edec == dec["decision"]
        ex["ecos"] = f"ECOS 기준금리 {d} {er:.2f}% {'일치' if same else '불일치, 원문 확인'}"
    elif eser:
        ex["ecos"] = f"ECOS 기준금리 {d} 값 아직 없음"
    else:
        ex["ecos"] = f"ECOS 기준금리 {NA}"
    # 2) 소수의견: 결정문 -> 기사(연합뉴스 우선)
    rtext = None
    try:
        rtext = release_text(dec.get("url"))
        if rtext:
            ex["src_ok"].append("한은 통화정책방향 결정문")
    except Exception as exc:  # noqa: BLE001
        ex["src_fail"].append(f"한은 결정문 {type(exc).__name__}")
    dis = dissent_from_text(rtext or "", dec["rate"], dec.get("prev"))
    if dis:
        dis["text"] += ", 출처 한은 통화정책방향 결정문"
    else:
        items: list[tuple[str, str]] = []
        for q, lang in ((f"한국은행 기준금리 소수의견 after:{(md - timedelta(days=1)):%Y-%m-%d} before:{(md + timedelta(days=2)):%Y-%m-%d}", "ko"),
                        (f"Bank of Korea rate dissent Yonhap after:{(md - timedelta(days=1)):%Y-%m-%d} before:{(md + timedelta(days=2)):%Y-%m-%d}", "en")):
            try:
                items += news_titles(q, lang)
            except Exception as exc:  # noqa: BLE001
                ex["src_fail"].append(f"뉴스 RSS({lang}) {type(exc).__name__}")
        items.sort(key=lambda x: 0 if re.search(r"연합|Yonhap", x[1], re.I) else 1)
        dis = dissent_from_titles(items)
        if items:
            ex["src_ok"].append(f"뉴스 RSS {len(items)}건")
    ex["dissent"] = dis["text"] if dis else f"소수의견 {NA}(결정문과 기사 제목에 없음, 총재 기자간담회 기사 확인)"
    ex["dissent_n"] = dis["count"] if dis else None
    # 3) 결정 흐름: 한은 기준금리 표(변경 이력) -> ECOS 일별
    changes: list[tuple[str, float]] = []
    try:
        changes = ktb.parse_base_table(ktb.fetch(cfg["sources"]["bok_base_rate_table"], 25))
        ex["src_ok"].append("한은 기준금리 표")
        hsrc = "한은 기준금리 변경 이력"
    except Exception as exc:  # noqa: BLE001
        ex["src_fail"].append(f"한은 기준금리 표 {type(exc).__name__}")
        changes = changes_from_daily(eser) if eser else []
        hsrc = "ECOS 기준금리 일별"
    meets = meeting_dates(cfg, [md.year - 1, md.year])
    past = meeting_path(meets, changes, d) if changes else []
    ex["past"] = past
    ex["path"] = (path_text(past, dec["decision"], d) + f" (출처 {hsrc})") if past else f"결정 흐름 {NA}"
    # 4) 다음 회의 신호
    gq = guidance_quote(rtext)
    ex["guidance"] = f"\"{gq}\" (결정문)" if gq else NA
    # 5) 판단 변경 조건
    cpi = gdp = None
    try:
        cpi = cpi_yoy(md.strftime("%Y-%m"))
        ex["src_ok"].append("ECOS 901Y010")
    except Exception as exc:  # noqa: BLE001
        ex["src_fail"].append(f"ECOS 901Y010 {type(exc).__name__}")
    try:
        gdp = gdp_qq()
        ex["src_ok"].append("ECOS 200Y102")
    except Exception as exc:  # noqa: BLE001
        ex["src_fail"].append(f"ECOS 200Y102 {type(exc).__name__}")
    ex["judge"] = judge_line(dec["decision"], past, cpi, gdp) if past else judge_line(dec["decision"], [], cpi, gdp).replace("재동결 아니오", f"재동결 {NA}")
    return ex


# ---------------- 판정 (순수 함수, selftest 대상) ----------------
def eval_y10(st: dict, v: float, cfg: dict) -> str | None:
    """'fire' | 'rearm' | None. st 를 바꾼다."""
    g = cfg["yield10"]
    if st["armed"] and v >= g["trigger_pct"]:
        st["armed"] = False
        return "fire"
    if not st["armed"] and v < g["rearm_below_pct"]:
        st["armed"] = True
        return "rearm"
    return None


def spread_widen(hist: dict, d: str, lookback: int) -> tuple[float, float, str, float] | None:
    """(오늘 bp, 확대 bp, 기준일, 기준일 bp). hist = {date: {'y3','aa'}} ECOS 관측일만."""
    days = sorted(k for k, v in hist.items() if v.get("y3") is not None and v.get("aa") is not None)
    if d not in days:
        return None
    i = days.index(d)
    if i < lookback:
        return None
    b = days[i - lookback]
    sp = (hist[d]["aa"] - hist[d]["y3"]) * 100
    bsp = (hist[b]["aa"] - hist[b]["y3"]) * 100
    return sp, sp - bsp, b, bsp


def eval_spread(st: dict, widen: float, cfg: dict) -> str | None:
    g = cfg["spread"]
    if st["armed"] and widen > g["widen_trigger_bp"]:
        st["armed"] = False
        return "fire"
    if not st["armed"] and widen < g["rearm_below_bp"]:
        st["armed"] = True
        return "rearm"
    return None


# ---------------- 메시지 ----------------
def body(stamp: str, what: str, status: str, action: str, later: str, trim: str, avoid: str) -> str:
    return "\n".join([f"{stamp} | KR-RATE | 국고채 금리 점검", f"이게 뭐 알림: {what}", f"지금 결과/상태: {status}",
                      f"지금 할 일: {action}", f"나중에: {later}", f"익절/줄일 때: {trim}", f"하지 말 것: {avoid}"])


def fmt(v: float | None, n: int = 3) -> str:
    return f"{v:.{n}f}%" if v is not None else NA


def macroir_ktb_hold(cfg: dict, d: str | None = None) -> str | None:
    """[MACRO-IR] 148070 <= 104,000원 경고가 켜져 있으면 이유 문자열. 읽기 실패는 None(금리 상한 판단은 따로 계속)."""
    g = cfg["yield10"]
    lvl = float(g.get("macroir_ktb_level", 104000))
    try:
        ms = json.loads((ROOT / g.get("macroir_state", "state/macroir.json")).read_text(encoding="utf-8"))
        px = (ms.get("last_values") or {}).get("ktb148070")
        if (ms.get("armed") or {}).get("ktb104k") is False or (px is not None and float(px) <= lvl):
            return f"[MACRO-IR] 148070 {lvl:,.0f}원 이하 경고 켜짐"
    except Exception as exc:  # noqa: BLE001
        print(f"macroir state read skipped: {exc}", file=sys.stderr)
    if d:
        try:
            rows = json.loads(get("https://m.stock.naver.com/api/stock/148070/price?pageSize=5&page=1"))
            for r in rows:
                if str(r.get("localTradedAt") or "")[:10] == d:
                    c = num(r.get("closePrice"))
                    if c is not None and c <= lvl:
                        return f"148070 {d} 종가 {c:,.0f}원, [MACRO-IR] {lvl:,.0f}원 경고 구간"
                    break
        except Exception as exc:  # noqa: BLE001
            print(f"148070 close check skipped: {exc}", file=sys.stderr)
    return None


def y10_hold_reason(cfg: dict, v: float, macro: str | None) -> str | None:
    ceil = cfg["yield10"].get("hold_ceiling_pct")
    if ceil is not None and v >= float(ceil):
        return f"10년 종가가 {float(ceil):.2f}% 이상(148070 약 104,000원 수준, [MACRO-IR] 경고와 같은 구간)"
    return macro


def y10_msg(cfg, stamp, d, v, src, prev, y3, nxt, hold: str | None = None):
    g = cfg["yield10"]
    bp = (v - prev[1]) * 100 if prev and prev[1] is not None else None
    chg = f", 직전 거래일 {fmt(prev[1])} 대비 {bp:+.1f}bp" if bp is not None else ""
    what = (f"국고채 10년 종가가 {g['trigger_pct']:.2f}% 이상이 되면 1번 오는 알림(148070 국고10년 ETF 판단 보조). "
            "148070 가격 경고(104,000원 이하)는 [MACRO-IR]가 따로 봅니다. 매매 신호 아님.")
    status = f"{d} 국고채 10년 종가 {v:.3f}% ({src}){chg}. 기준 {g['trigger_pct']:.2f}% 이상. 국고3년 {fmt(y3)}. 다음 금통위 {nxt}."
    later = f"10년 종가가 {g['rearm_below_pct']:.2f}% 아래로 내려갔다가 다시 {g['trigger_pct']:.2f}% 이상이 되면 다시 옵니다."
    if hold:
        title = f"[KR-RATE] 국고10년 {v:.3f}% ({d[5:7]}/{d[8:]} 종가) | 148070 추가매수 보류"
        status += f" 보류 이유: {hold}."
        action = "장기금리 급등 구간. 148070 추가매수 보류, 금리 피크아웃 확인 후 재판단(금리 고점 통과 신호: 2~3거래일 연속 하락 등)."
        trim = "보유분은 목표 비중 안에서 유지. 경고가 풀리기 전에는 비중을 늘리지 않기."
        avoid = "물타기, 금리 고점 단정, 신용/미수, 확인 불가 값을 0이나 정상으로 읽기."
        return title, body(stamp, what, status, action, later, trim, avoid), 4, ["warning"]
    title = f"[KR-RATE] 국고10년 {v:.3f}% ({d[5:7]}/{d[8:]} 종가) | 148070 금리 피크아웃 확인 후 검토"
    action = "148070 금리 피크아웃 확인 후 검토(금리 고점 통과 신호: 2~3거래일 연속 하락 등). 상승 중에는 추가매수 보류."
    if bp is not None and bp > 0:
        action += f" 오늘은 직전 대비 {bp:+.1f}bp 상승 중이라 보류 구간입니다."
    trim = f"보유분은 목표 비중 안에서 유지. 10년 종가 {float(g.get('hold_ceiling_pct', 4.5)):.2f}% 이상이면 추가매수 보류 알림으로 바뀝니다."
    avoid = "상승 중 추격 편입, 한 번에 전액 편입, 금리 고점 단정, 확인 불가 값을 0이나 정상으로 읽기."
    return title, body(stamp, what, status, action, later, trim, avoid), 4, ["rotating_light"]


def spread_msg(cfg, stamp, d, aa, y3, sp, w, b, bsp):
    g = cfg["spread"]
    title = f"[KR-RATE] 148070 신용 스프레드 {w:+.0f}bp 확대 (20거래일) | 회사채 위험 점검"
    msg = body(stamp, f"회사채 AA- 3년과 국고채 3년 금리 차이가 약 1개월(20거래일) 전보다 {g['widen_trigger_bp']}bp 넘게 벌어지면 1번 오는 알림. 매매 신호 아님.",
               f"{d} 회사채 AA- 3년 {aa:.3f}% - 국고채 3년 {y3:.3f}% = {sp:.0f}bp. {b} {bsp:.0f}bp 대비 {w:+.0f}bp "
               f"(기준 {g['widen_trigger_bp']}bp 초과). 출처 KOFIA 최종호가(한국은행 ECOS).",
               "회사채나 크레딧 상품 비중이 있으면 점검하고, 신용 경색 관련 뉴스를 확인. 국고채(148070)와 머니마켓 ETF 는 신용 위험과 따로 봅니다.",
               f"확대폭이 {g['rearm_below_bp']}bp 미만으로 줄면 다시 무장하고, 다시 벌어지면 또 옵니다.",
               "회사채형 상품이 있으면 비중 축소 검토.",
               "스프레드 하나로 국고채까지 팔기, 확인 불가 값을 정상으로 읽기.")
    return title, msg, 4, ["rotating_light"]


def mpc_msg(stamp, d, dec, y, nxt, ex: dict | None = None):
    dk = DEC_KO.get(dec["decision"], dec["decision"])
    prev = f"{dec['prev']:.2f}% → " if dec.get("prev") is not None else ""
    ylines = []
    for k, nm in (("y3", "국고3년"), ("y10", "국고10년")):
        v, p, src = y.get(k, (None, None, None))
        if v is None:
            ylines.append(f"{nm} 종가 {NA}")
        else:
            ch = f", 전일 대비 {(v - p) * 100:+.1f}bp" if p is not None else ", 전일 대비 확인 불가"
            ylines.append(f"{nm} 종가 {v:.3f}%{ch} ({src})")
    nv = dec.get("naver")
    cross = ""
    if nv and nv[1] is not None:
        cross = f" 네이버 기준금리 {nv[1]:.2f}% ({nv[0]} 발표) {'일치' if abs(nv[1] - dec['rate']) < 0.001 and nv[0] == d else '불일치, 원문 확인'}."
    y10 = y.get("y10", (None,))[0]
    title = f"[KR-RATE] 148070 {d[5:7]}/{d[8:]} 금통위 기준금리 {dk} {dec['rate']:.2f}%" + (f" | 국고10년 {y10:.3f}%" if y10 is not None else "")
    def build(guide: str) -> str:
        if ex is None:
            status = (f"기준금리 {prev}{dec['rate']:.2f}% ({dk}), 출처 {dec.get('source')}. {dec.get('dissent') or ''}.{cross} "
                      + " / ".join(ylines) + f". 다음 금통위 {nxt}.")
        else:
            status = (f"결정: {dk}, 기준금리 {prev}{dec['rate']:.2f}% (출처 {dec.get('source')}; {ex['ecos']}).{cross} "
                      f"소수의견 수: {ex['dissent'].removeprefix('소수의견 ')}. 연속 인상 여부: {ex['path']}. 다음 회의 신호 문구: {guide}. "
                      + " / ".join(ylines) + f". 다음 금통위 {nxt}. {ex['judge']}.")
        what = ("금통위(한국은행 금융통화위원회) 기준금리 결정일 저녁 사실 요약. 판단 없음. 매매 신호 아님." if ex is None else
                "금통위(한국은행 금융통화위원회) 기준금리 결정일 저녁 사실 요약과 채권 ETF 판단 변경 조건 점검. 매매 신호 아님.")
        return body(stamp, what,
                    status,
                    "결정과 금리 숫자만 확인. 148070 판단(10년 종가 기준, 금리 피크아웃 확인 후 검토)은 따로 알림이 옵니다.",
                    f"다음 금통위({nxt}) 저녁에 다시 옵니다.",
                    "이 요약만으로 비중을 바꾸지 않기.",
                    "결정 하나로 금리 방향 단정하기(인상 종료 같은 판단 포함), 확인 불가 값을 정상으로 읽기.")

    guide = ex["guidance"] if ex else ""
    msg = build(guide)
    # ntfy 끝 줄("꺼지는 때: ...") 여유 150바이트를 두고 3,900바이트 아래로: 인용문부터 줄인다
    q = re.match(r'^"(.*)" \(결정문\)$', guide)
    inner = q.group(1) if q else None
    while ex is not None and len(msg.encode("utf-8")) > MPC_MAX_BYTES - 150 and inner and len(inner) > 30:
        inner = inner[: int(len(inner) * 0.8)].rstrip() + "..."
        msg = build(f'"{inner}" (결정문)')
    return title, msg, 3, ["bank"]


def cpi_msg(cfg, stamp, c):
    g = cfg["cpi"]
    emph = c["qb"] is not None and c["qb"] < g["emphasis_below_pct"]
    y, m = c["month"].split("-")
    title = f"[KR-RATE] 148070 {y}년 {int(m)}월 근원물가 {c['qb']:.1f}% (농산물 및 석유류 제외)" + (f" | {g['emphasis_below_pct']}% 미만" if emph else "")
    prev = f" (전월 {c['qb_prev']:.1f}%)" if c.get("qb_prev") is not None else ""
    db = f"{c['db']:.1f}%" if c.get("db") is not None else NA
    msg = body(stamp, "근원물가(농산물및석유류제외지수 전년동월비) 새 달 수치가 나오면 1번 오는 알림. 2.5% 미만이면 강조. 매매 신호 아님.",
               f"{y}년 {int(m)}월 농산물및석유류제외 전년동월비 {c['qb']:.1f}%{prev}, 기준 {g['emphasis_below_pct']}% "
               f"{'미만' if emph else '이상'}. 참고 식료품및에너지제외 {db}. 출처 국가데이터처 소비자물가동향 지수(한국은행 ECOS 901Y010, 지수로 계산).",
               "물가 흐름만 확인. 금리 판단은 국고10년 조건 알림과 금통위 요약으로.",
               "다음 달 소비자물가동향(보통 매월 초) 뒤 첫 19:00 실행에서 다시 옵니다.",
               "이 숫자만으로 비중을 바꾸지 않기.",
               "한 달 수치로 금리 인하나 인상을 단정하기.")
    return title, msg, (4 if emph else 3), (["rotating_light"] if emph else ["chart_with_downwards_trend"])


# ---------------- main ----------------
def trading_days_between(a: str | None, b: date, cap: int) -> list[str]:
    out, d = [], b
    while len(out) < cap and (a is None or d.isoformat() > a):
        if is_trading_day(d):
            out.append(d.isoformat())
        d -= timedelta(days=1)
    return sorted(out)


def main() -> int:
    args = sys.argv[1:]
    mode = (args[0] if args and not args[0].startswith("--") else "auto").lower()
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    if mode == "selftest":
        return selftest(cfg)
    now = datetime.now(KST)
    if "--as-of" in args:
        if mode != "dry":
            print("--as-of 는 dry 에서만")
            return 2
        now = datetime.strptime(args[args.index("--as-of") + 1], "%Y-%m-%d %H:%M").replace(tzinfo=KST)
    if mode not in {"auto", "dry"}:
        print(f"unknown mode {mode}")
        return 2
    mpc_override = None
    if "--mpc-date" in args:  # dry 전용: 지난 금통위 날짜로 결정일 요약을 다시 만들어 봄 (mpc_done 무시)
        if mode != "dry":
            print("--mpc-date 는 dry 에서만")
            return 2
        mpc_override = args[args.index("--mpc-date") + 1]
        if mpc_override == "last":  # 오늘 이전 마지막 금통위, 그날 19:00 으로
            mpc_override = max(x for x in cfg["mpc"]["schedule"] if x <= datetime.now(KST).strftime("%Y-%m-%d"))
            now = datetime.strptime(mpc_override + " 19:00", "%Y-%m-%d %H:%M").replace(tzinfo=KST)
    dry = mode == "dry"
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    today = now.strftime("%Y-%m-%d")
    topic = resolve_topic("port", cfg.get("ntfy_topic"))
    server = resolve_server(cfg.get("ntfy_server"))
    state = load_state()
    sess = session_day(now)
    print(f"now={now:%Y-%m-%d %H:%M} session={sess} trading={is_trading_day(sess)} ecos_key={'yes' if os.environ.get('ECOS_API_KEY') else 'no(sample)'}")
    if not is_trading_day(sess):
        print("not a KRX trading day: skip")
        return 0
    late = now.date() > sess or now.hour >= 18
    out: list[tuple[str, str, int, list[str]]] = []
    errors: list[str] = []

    def ops_once(key: str, title: str, msg: str) -> None:
        fn = state.setdefault("fail_notice", {})
        if fn.get(key) != today:
            out.append((title, msg, 3, ["warning"]))
            fn[key] = today

    def ops_body(what: str, status: str) -> str:
        return body(stamp, what, status, "필요하면 한국은행 ECOS 나 증권사 앱에서 직접 보세요.", "다음 19:00 실행 때 다시 봅니다.",
                    "이번 안내로 비중을 바꾸지 마세요.", "조회 실패를 조건 미충족으로 읽기.")

    # --- 데이터 ---
    start = sess - timedelta(days=70)
    ec: dict[str, dict[str, float]] = {}
    for k, item in ECOS_ITEMS.items():
        try:
            ec[k] = ecos_daily(item, start, sess)
        except Exception as exc:  # noqa: BLE001
            ec[k] = {}
            errors.append(f"ECOS {k}: {type(exc).__name__} {str(exc)[:80]}")
    nv: dict[str, dict[str, float]] = {}
    for k, code in (("y10", "KR10YT=RR"), ("y3", "KR3YT=RR")):
        try:
            nv[k] = naver_bond(code)
        except Exception as exc:  # noqa: BLE001
            nv[k] = {}
            errors.append(f"네이버 {code}: {type(exc).__name__}")
    try:
        nv["aa"] = naver_aa()
    except Exception as exc:  # noqa: BLE001
        nv["aa"] = {}
        errors.append(f"네이버 AA-: {type(exc).__name__}")
    hist = state.setdefault("hist", {})
    for k in ECOS_ITEMS:
        for d, v in ec[k].items():
            hist.setdefault(d, {})[k] = v
    keep = int(cfg["spread"]["history_keep"])
    state["hist"] = hist = dict(sorted(hist.items())[-keep:])
    if errors:
        print("errors: " + " | ".join(errors), file=sys.stderr)

    def close(k: str, d: str) -> tuple[float | None, str | None]:
        if d in ec.get(k, {}):
            return ec[k][d], "KOFIA 최종호가(한국은행 ECOS)"
        if d in nv.get(k, {}):
            return nv[k][d], "로이터 시장 종가(네이버), ECOS 미반영"
        return None, None

    sched, sched_notes = mpc_schedule(cfg, sess.year)
    nxt_list = [d for d in sched if d > sess.isoformat()]
    nxt = nxt_list[0] if nxt_list else f"미정({', '.join(sched_notes) or '일정 없음'})"
    y10s, y3s = close("y10", sess.isoformat()), close("y3", sess.isoformat())
    print(f"y10 {sess}: {y10s} | y3: {y3s} | ECOS last y10={max(ec['y10']) if ec['y10'] else None} "
          f"aa={max(ec['aa']) if ec['aa'] else None} | naver last y10={max(nv['y10']) if nv['y10'] else None} "
          f"aa={max(nv['aa']) if nv['aa'] else None} | next MPC {nxt} {sched_notes}")

    seeding = not state.get("seeded")
    cap = int(cfg["catchup_trading_days"])

    # --- 1) 국고10년 ---
    st = state["y10"]
    for d in trading_days_between(st.get("last_day"), sess, cap):
        v, src = close("y10", d)
        if v is None:
            if d == sess.isoformat() and late:
                ops_once("y10", "[KR-RATE] 148070 국고10년 조회 실패 | 조건 판정 못 함",
                         ops_body("국고채 10년 금리 조건 알림의 데이터 조회 실패 안내.",
                                  f"{d} 국고채 10년 종가 {NA} (ECOS, 네이버 모두 그날 값 없음. {' | '.join(errors)[:200]})."))
            break
        res = eval_y10(st, v, cfg)
        print(f"y10 {d} {v} {src} -> {res} armed={st['armed']}")
        if res == "fire":
            st["fired_day"] = d
            if not seeding:
                pd = prev_trading_day(date.fromisoformat(d)).isoformat()
                hold = y10_hold_reason(cfg, v, macroir_ktb_hold(cfg, d))
                out.append(y10_msg(cfg, stamp, d, v, src, (pd, close("y10", pd)[0]), close("y3", d)[0], nxt, hold))
        st["last_day"] = d

    # --- 3) 신용 스프레드 (ECOS 만, 같은 출처) ---
    sst = state["spread"]
    lb = int(cfg["spread"]["lookback_obs"])
    now_sp = None
    sdays = trading_days_between(sst.get("last_day"), sess, cap)
    if not sst.get("seeded"):  # 첫 판정은 계산되는 가장 최근 하루만 (지난 날짜로 울리지 않게)
        sdays = [d for d in sdays if spread_widen(hist, d, lb) is not None][-1:]
    for d in sdays:
        r = spread_widen(hist, d, lb)
        if r is None:
            break
        sp, w, b, bsp = r
        now_sp = (d, sp, w, b, bsp)
        if not sst.get("seeded"):
            sst["armed"] = w <= cfg["spread"]["widen_trigger_bp"]
            sst["seeded"] = True
            res = "seed"
        else:
            res = eval_spread(sst, w, cfg)
        print(f"spread {d} {sp:.1f}bp widen {w:+.1f}bp vs {b} -> {res} armed={sst['armed']}")
        if res == "fire":
            sst["fired_day"] = d
            if not seeding:
                out.append(spread_msg(cfg, stamp, d, hist[d]["aa"], hist[d]["y3"], sp, w, b, bsp))
        aa_n = nv.get("aa", {}).get(d)
        if aa_n is not None and abs(aa_n - hist[d]["aa"]) > 0.0015:
            print(f"note: AA- ECOS {hist[d]['aa']} vs 네이버 {aa_n} ({d})")
        sst["last_day"] = d
    if sst.get("last_day"):
        lag = len(trading_days_between(sst["last_day"], sess, 10)) - 1
        if lag >= 2 and late:
            ops_once("spread", "[KR-RATE] 148070 신용 스프레드 조회 실패 | 판정 밀림",
                     ops_body("회사채 신용 스프레드 알림의 데이터 지연 안내.",
                              f"ECOS 회사채 AA-/국고3년 값이 {sst['last_day']} 이후 {lag}거래일째 없음 ({' | '.join(errors)[:200]})."))
    elif late and not hist:
        ops_once("spread", "[KR-RATE] 148070 신용 스프레드 조회 실패 | 판정 못 함",
                 ops_body("회사채 신용 스프레드 알림의 데이터 조회 실패 안내.", f"ECOS 시장금리 {NA} ({' | '.join(errors)[:200]})."))

    # --- 2a) 금통위 결정일 ---
    done = set(state.get("mpc_done") or [])
    cands = [x for x in sched if x <= sess.isoformat() and x not in done]
    if mpc_override:  # dry 전용: 지난 회의 하나를 요약 경로 그대로 다시 만들어 본다
        cands = [mpc_override]
    for d in cands:
        age = (sess - date.fromisoformat(d)).days
        if not mpc_override and (seeding or age > int(cfg["mpc"]["retry_days"])):
            done.add(d)
            continue
        if not is_trading_day(date.fromisoformat(d)):
            continue
        dec = mpc_decision(cfg, d)
        print(f"mpc {d}: {dec}")
        if dec.get("status") != "ok":
            ops_once(f"mpc_{d}", "[KR-RATE] 148070 금통위 결정 확인 불가 | 요약 못 보냄",
                     ops_body("금통위 결정일 요약의 데이터 확인 실패 안내.",
                              f"{d} 기준금리 결정 {NA} ({dec.get('why')}). 다음 실행 때 다시 봅니다({cfg['mpc']['retry_days']}일 안)."))
            continue
        pd = prev_trading_day(date.fromisoformat(d)).isoformat()
        y = {}
        for k in ("y3", "y10"):
            v, src = close(k, d)
            y[k] = (v, close(k, pd)[0], src)
        try:
            ex = mpc_extras(cfg, d, dec)
            print(f"mpc extras ok={ex['src_ok']} fail={ex['src_fail']}")
        except Exception as exc:  # noqa: BLE001  보강 실패는 기존 요약으로
            print(f"mpc extras failed: {type(exc).__name__} {exc}", file=sys.stderr)
            ex = None
        out.append(mpc_msg(stamp, d, dec, y, nxt, ex))
        done.add(d)
    state["mpc_done"] = sorted(done)[-24:]

    # --- 2b) 근원물가 ---
    try:
        c = cpi_yoy(sess.strftime("%Y-%m"))
        print(f"cpi {c} last_reported={state.get('cpi_last_month')}")
        if c["qb"] is None:
            raise RuntimeError(f"{c['month']} 전년동월비 계산 못 함")
        if state.get("cpi_last_month") is None or c["month"] > state["cpi_last_month"]:
            if not seeding and state.get("cpi_last_month") is not None:
                out.append(cpi_msg(cfg, stamp, c))
            state["cpi_last_month"] = c["month"]
    except Exception as exc:  # noqa: BLE001
        print(f"cpi failed: {exc}", file=sys.stderr)
        if late:
            ops_once("cpi", "[KR-RATE] 148070 근원물가 조회 실패 | 새 달 수치 판정 못 함",
                     ops_body("근원물가 알림의 데이터 조회 실패 안내.", f"ECOS 소비자물가지수(특수분류) {NA} ({str(exc)[:200]})."))

    if now_sp:
        print(f"spread now {now_sp[0]} {now_sp[1]:.1f}bp, 20obs widen {now_sp[2]:+.1f}bp vs {now_sp[3]} ({now_sp[4]:.1f}bp)")
    if seeding:
        state["seeded"] = True
        print("SEED RUN: conditions evaluated, nothing sent")
    for title, msg, pri, tags in out:
        print(f"TITLE: {title}\nPRIORITY: {pri} TOPIC: {route_topic(topic, title)}\n{msg}\n")
        if not dry:
            publish(topic, title, msg, pri, tags, server)
    if not out:
        print("no alert")
    if dry:
        print("DRY RUN: no publish, state not written")
        return 0
    state["updated_at"] = now.isoformat()
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


def mpc_selftest(cfg: dict) -> list:
    """금통위 요약 보강: 가짜 인상(11/26 3.25%)과 가짜 동결(11/26 3.00%)."""
    meets = ["2025-10-23", "2025-11-27", "2026-01-15", "2026-02-26", "2026-04-10", "2026-05-28", "2026-07-16", "2026-08-27",
             "2026-10-22", "2026-11-26"]
    changes = [("2026-08-27", 3.0), ("2026-07-16", 2.75), ("2025-05-29", 2.5), ("2025-02-25", 2.75)]
    past = meeting_path(meets, changes, "2026-11-26")
    assert [k for _, k, _ in past[-3:]] == ["hike", "hike", "hold"], past
    assert path_text(past, "hike", "2026-11-26") == "동결 1회 후 재인상, 올해 3번째 인상", path_text(past, "hike", "2026-11-26")
    hold_t = path_text(past, "hold", "2026-11-26")
    assert hold_t.startswith("2회 연속 동결 (재동결), 마지막 변경 2026-08-27 인상(3.00%)") and "올해 인상 2회" in hold_t, hold_t
    assert path_text(past[:-1], "hike", "2026-10-22") == "3회 연속 인상, 올해 3번째 인상"
    p2 = meeting_path(meets[:5], [("2025-02-25", 2.75), ("2025-05-29", 2.5)], "2026-02-26")
    assert path_text(p2, "cut", "2026-02-26").startswith("동결 ") and "인하" in path_text(p2, "cut", "2026-02-26")
    real = ("금융통화위원회는 다음 통화정책방향 결정시까지 한국은행 기준금리를 현재의 3.00% 수준에서 유지하여 통화정책을 운용하기로 하였다 . "
            "기준금리를 현 수준에서 유지하면서 대내외 위험요인의 전개상황과 두 차례 인상의 파급영향을 점검할 필요가 있다고 판단하였다 . "
            "따라서 향후 통화정책은 물가 · 경기 흐름 및 금융안정 상황을 면밀히 점검하면서 추가 인상의 시기와 속도 등을 결정해 나갈 것이다 . "
            "□ 금번 기준금리 동결 결정에 대해서는 금융통화위원 7 명 모두 찬성하였다 .")
    d0 = dissent_from_text(real, 3.0)
    assert d0 and d0["count"] == 0 and "7명 모두 찬성" in d0["text"], d0
    hk = ("금융통화위원회는 한국은행 기준금리를 현재의 3.00%에서 3.25%로 상향 조정하여 통화정책을 운용하기로 하였다. "
          "□ 금번 기준금리 인상 결정에 대해서는 홍길동 위원은 기준금리를 3.00%로 유지하자는 소수의견을 개진하였다.")
    d1 = dissent_from_text(hk, 3.25, 3.0)
    assert d1 and d1["count"] == 1 and d1["dir"] == "동결", d1
    assert dissent_from_text("금융통화위원회는 기준금리를 3.00% 수준에서 유지하기로 하였다.", 3.0) is None
    t1 = dissent_from_titles([("한은 기준금리 3.25%로 인상…동결 소수의견 1명", "연합뉴스")])
    assert t1 and t1["count"] == 1 and t1["dir"] == "동결", t1
    t2 = dissent_from_titles([("Bank of Korea raises rate to 3.25% with one dissent", "Yonhap News Agency")])
    assert t2 and t2["count"] == 1, t2
    assert dissent_from_titles([("한은 기준금리 동결", "연합뉴스")]) is None
    gq = guidance_quote(real)
    assert gq and gq.startswith("따라서 향후 통화정책은 물가, 경기 흐름") and len(gq) <= 200 and "\u00b7" not in gq, gq
    assert guidance_quote("국내경제는 성장세를 이어갔다.") is None
    cpi = {"month": "2026-10", "qb": 2.5, "qb_prev": 2.7, "db": 2.6}
    gdp = {"q": "2026Q3", "v": 1.1, "q_prev": "2026Q2", "v_prev": 1.3}
    j_hold = judge_line("hold", past, cpi, gdp)
    assert "재동결 예" in j_hold and "충족: 채권 ETF 판단 변경 검토" in j_hold, j_hold
    j_hike = judge_line("hike", past, cpi, gdp)
    assert "미충족(재동결 아님)" in j_hike, j_hike
    j_na = judge_line("hold", past, None, None)
    assert "판정 보류" in j_na and NA in j_na
    j_no = judge_line("hold", past, {"month": "2026-10", "qb": 2.8, "qb_prev": 2.7}, {"q": "2026Q3", "v": 1.5, "q_prev": "2026Q2", "v_prev": 1.3})
    assert "미충족(물가, 성장 둔화 없음)" in j_no
    y = {"y3": (3.40, 3.32, "KOFIA 최종호가(한국은행 ECOS)"), "y10": (4.10, 4.05, "KOFIA 최종호가(한국은행 ECOS)")}
    base = {"src_ok": [], "src_fail": [], "ecos": "ECOS 기준금리 2026-11-26 값 아직 없음"}
    ex_hike = dict(base, dissent=d1["text"] + ", 출처 한은 통화정책방향 결정문", dissent_n=1, past=past,
                   path=path_text(past, "hike", "2026-11-26") + " (출처 한은 기준금리 변경 이력)",
                   guidance=f'"{guidance_quote(real)}" (결정문)', judge=j_hike)
    ex_hold = dict(base, dissent=d0["text"] + ", 출처 한은 통화정책방향 결정문", dissent_n=0, past=past,
                   path=hold_t + " (출처 한은 기준금리 변경 이력)", guidance=f'"{"가" * 1200}" (결정문)', judge=j_hold)
    out = []
    for dec, ex in ((({"decision": "hike", "rate": 3.25, "prev": 3.0, "source": "한은 통화정책방향 보도자료",
                       "naver": ("2026-11-26", 3.25)}), ex_hike),
                    (({"decision": "hold", "rate": 3.0, "prev": 3.0, "source": "한은 통화정책방향 보도자료", "naver": None}), ex_hold)):
        t, m, p, tg = mpc_msg("2026-11-26 19:00 KST", "2026-11-26", dec, y, "2027 일정 미발표", ex)
        assert len(m.encode("utf-8")) <= MPC_MAX_BYTES - 150, len(m.encode("utf-8"))
        for need in ("결정:", "소수의견 수:", "연속 인상 여부:", "다음 회의 신호 문구:", "판단 변경 조건"):
            assert need in m, need
        out.append((t, m, p, tg))
    assert "동결 1회 후 재인상, 올해 3번째 인상" in out[0][1] and "1명(동결 주장" in out[0][1]
    assert "재동결 예" in out[1][1] and "..." in out[1][1]
    print(f"mpc extras ok: hike {len(out[0][1].encode())}B, hold {len(out[1][1].encode())}B")
    return out


def selftest(cfg: dict) -> int:
    """가짜 값으로 조건과 문구만 검사 (네트워크, ntfy, state 없음)."""
    stamp = "2026-11-02 19:00 KST"
    st = {"armed": True}
    seq = [(4.30, None), (4.36, "fire"), (4.40, None), (4.31, None), (4.29, "rearm"), (4.34, None), (4.35, "fire")]
    for v, want in seq:
        got = eval_y10(st, v, cfg)
        assert got == want, (v, got, want)
    print("y10 ok:", seq)
    hist = {}
    d0 = date(2026, 9, 1)
    days = []
    d = d0
    while len(days) < 26:
        if is_trading_day(d):
            days.append(d.isoformat())
        d += timedelta(days=1)
    for i, dd in enumerate(days):
        hist[dd] = {"y3": 3.80, "aa": 4.45 + (0.25 if i >= 21 else 0.0) + (-0.20 if i >= 24 else 0.0)}
    sst = {"armed": True}
    res = []
    for dd in days[20:]:
        sp, w, b, bsp = spread_widen(hist, dd, 20)
        res.append((dd, round(w, 1), eval_spread(sst, w, cfg)))
    assert [r[2] for r in res] == [None, "fire", None, None, "rearm", None], res
    print("spread ok:", res)
    assert spread_widen(hist, days[5], 20) is None
    c = {"month": "2026-10", "qb": 2.4, "qb_prev": 2.7, "db": 2.6}
    t, m, p, tg = cpi_msg(cfg, stamp, c)
    assert p == 4 and tg == ["rotating_light"] and "2.5% 미만" in t
    t2, _, p2, _ = cpi_msg(cfg, stamp, {"month": "2026-10", "qb": 2.5, "qb_prev": 2.7, "db": 2.6})
    assert p2 == 3 and "미만" not in t2
    samples = [y10_msg(cfg, stamp, "2026-11-02", 4.361, "KOFIA 최종호가(한국은행 ECOS)", ("2026-10-30", 4.312), 3.951, "2026-11-26"),
               spread_msg(cfg, stamp, "2026-11-02", 4.75, 3.80, 95.0, 25.0, "2026-10-02", 70.0),
               mpc_msg(stamp, "2026-11-26", {"decision": "hold", "rate": 3.0, "prev": 3.0, "source": "한은 통화정책방향 보도자료",
                                            "dissent": "만장일치", "naver": ("2026-11-26", 3.0)},
                       {"y3": (3.85, 3.87, "KOFIA 최종호가(한국은행 ECOS)"), "y10": (4.20, None, "로이터 시장 종가(네이버), ECOS 미반영")}, "2027 일정 미발표"),
               (t, m, p, tg)]
    samples += mpc_selftest(cfg)

    # 148070 문구: 매수 권유 없음, 피크아웃 문구, [MACRO-IR] 경고와 충돌 방지 (2026-10-26)
    def act(m):
        return next(x for x in m.split("\n") if x.startswith("지금 할 일:"))

    def buy_words(text):
        t = text.replace("추가매수 보류", "")
        return [w for w in ("매수", "분할 진입", "비중 확대", "투입", "시작 검토") if w in t]

    src = "KOFIA 최종호가(한국은행 ECOS)"
    t1, m1, p1, _ = y10_msg(cfg, stamp, "2026-11-02", 4.35, src, ("2026-10-30", 4.31), 3.95, "2026-11-26",
                            y10_hold_reason(cfg, 4.35, None))
    assert buy_words(t1 + m1) == [], buy_words(t1 + m1)
    assert "148070 금리 피크아웃 확인 후 검토(금리 고점 통과 신호: 2~3거래일 연속 하락 등). 상승 중에는 추가매수 보류." in act(m1), act(m1)
    assert "+4.0bp 상승 중이라 보류" in act(m1) and "피크아웃 확인 후 검토" in t1 and p1 == 4
    print("y10 4.35% rising: peak-out wording, no buy wording ok")
    t2, m2, p2, _ = y10_msg(cfg, stamp, "2026-11-02", 4.60, src, ("2026-10-30", 4.40), 3.95, "2026-11-26",
                            y10_hold_reason(cfg, 4.60, None))
    assert buy_words(t2 + m2) == [] and "추가매수 보류" in t2 and "피크아웃 확인 후 검토" not in act(m2) and "4.50% 이상" in m2 and p2 == 4
    t3, m3, _, _ = y10_msg(cfg, stamp, "2026-11-02", 4.50, src, ("2026-10-30", 4.40), 3.95, "2026-11-26",
                           y10_hold_reason(cfg, 4.50, None))
    assert "추가매수 보류" in t3 and buy_words(t3 + m3) == []
    print("y10 4.60% / 4.50% ceiling: only 추가매수 보류 ok")
    import tempfile
    from unittest import mock
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "state").mkdir()
        cases = [({"armed": {"ktb104k": False}, "last_values": {"ktb148070": 104500}}, True),
                 ({"armed": {"ktb104k": True}, "last_values": {"ktb148070": 103900}}, True),
                 ({"armed": {"ktb104k": True}, "last_values": {"ktb148070": 107420}}, False)]
        for ms, want in cases:
            (root / "state" / "macroir.json").write_text(json.dumps(ms), encoding="utf-8")
            with mock.patch(f"{__name__}.ROOT", root):
                got = macroir_ktb_hold(cfg)
            assert bool(got) == want, (ms, got)
        with mock.patch(f"{__name__}.ROOT", root / "missing"):
            assert macroir_ktb_hold(cfg) is None
        (root / "state" / "macroir.json").write_text(json.dumps(cases[0][0]), encoding="utf-8")
        with mock.patch(f"{__name__}.ROOT", root):
            r = y10_hold_reason(cfg, 4.36, macroir_ktb_hold(cfg))
        t4, m4, _, _ = y10_msg(cfg, stamp, "2026-11-02", 4.36, src, ("2026-10-30", 4.30), 3.95, "2026-11-26", r)
        assert r and "추가매수 보류" in t4 and buy_words(t4 + m4) == [] and "[MACRO-IR]" in m4
        # Naver same-day close check (mocked): 148070 <= 104,000 -> hold even when the macroir state is clean
        (root / "state" / "macroir.json").write_text(json.dumps(cases[2][0]), encoding="utf-8")
        fake = json.dumps([{"localTradedAt": "2026-11-02", "closePrice": "103,950"}])
        with mock.patch(f"{__name__}.ROOT", root), mock.patch(f"{__name__}.get", return_value=fake):
            assert macroir_ktb_hold(cfg, "2026-11-02")
        fake_ok = json.dumps([{"localTradedAt": "2026-11-02", "closePrice": "107,000"}])
        with mock.patch(f"{__name__}.ROOT", root), mock.patch(f"{__name__}.get", return_value=fake_ok):
            assert macroir_ktb_hold(cfg, "2026-11-02") is None
    print("macroir zone active -> only 추가매수 보류 ok; missing state ignored ok")
    samples += [(t1, m1, p1, []), (t2, m2, p2, []), (t4, m4, 4, [])]
    for t, m, p, tg in samples:
        assert "\u00b7" not in t + m and "\u2014" not in t + m
        assert route_topic("cwally-port", t) == "cwally-port", t
        print(f"-- sample (가짜 숫자)\nTITLE: {t}\nPRIORITY: {p} TAGS: {tg}\n{m}\n")
    assert route_topic("cwally-port", "[KR-RATE] 148070 국고10년 조회 실패 | 조건 판정 못 함") == "cwally-ops"
    print("SELFTEST OK: no network, no publish, no state")
    return 0


if __name__ == "__main__":
    sys.exit(main())
