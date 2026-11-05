#!/usr/bin/env python3
"""[FOMC-CUT] FOMC statement + SEP cut-cycle signal for 453850 / 476760.

Runs on the existing morning 09:00 KST slot the calendar day after each FOMC
decision (2pm ET ≈ 04:00 KST next day). No new cron.

Does NOT revive retired [FOMC] oneshots (fedodds CME odds, fomc1007 minutes
sentence counts). Those expired in the 2026-10-26 oneshot cleanup.

Classification: 인하 사이클 진입 / 인하 신호 강화 / 중립 / 매파 유지.
On 진입 or 인하 신호 강화: one linked alert + set state/fomccut.json signal
flag that k453850 and 476760 monitors read. Hawkish/neutral: one brief summary
if config says so. Once per meeting (state.done).

Modes: auto | dry | install | force | selftest | seed
"""
from __future__ import annotations

import csv
import difflib
import io
import json
import re
import sys
import traceback
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from kr_calendar import KST  # noqa: E402
from ntfy_util import publish, resolve_server, resolve_topic  # noqa: E402

TAG = "[FOMC-CUT]"
NAME = "fomccut"
CFG_PATH = ROOT / "configs" / f"{NAME}.json"
STATE_PATH = ROOT / "state" / f"{NAME}.json"
UA = {"User-Agent": "Mozilla/5.0 (cwally-alerts; +mailto:sosmos3218@gmail.com)"}
FORBIDDEN = ("\u00b7", "\u2014", "\u318d")
CLASSES = ("인하 사이클 진입", "인하 신호 강화", "중립", "매파 유지")
SIGNAL_CLASSES = ("인하 사이클 진입", "인하 신호 강화")


def scrub(s: str) -> str:
    for a, b in (("\u00b7", ", "), ("\u318d", ", "), ("\u2014", " - "), ("\u2027", ", "), ("\u30fb", ", ")):
        s = s.replace(a, b)
    s = re.sub(r"[^\S\n]+", " ", s)
    return "\n".join(line.strip() for line in s.strip().splitlines())


def check_text(t: str, m: str) -> None:
    for ch in FORBIDDEN:
        if ch in t or ch in m:
            raise ValueError(f"forbidden U+{ord(ch):04X}")


def get(url: str, timeout: int = 40) -> str:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def get_json(url: str):
    return json.loads(get(url))


def num(v) -> float | None:
    if v is None:
        return None
    try:
        return float(str(v).replace(",", "").replace("\u2013", "-").strip())
    except ValueError:
        return None


def load_cfg() -> dict:
    return json.loads(CFG_PATH.read_text(encoding="utf-8"))


def load_state() -> dict:
    base = {
        "seeded": False,
        "done": [],
        "signal": {"active": False, "class": None, "meeting": None, "score": None, "updated_at": None},
        "prior_statement": {},
        "prior_sep": {},
        "ops_day": None,
        "updated_at": None,
        "last": {},
    }
    if STATE_PATH.exists():
        try:
            base.update(json.loads(STATE_PATH.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass
    return base


def save_state(st: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def ended(cfg: dict, now: datetime) -> bool:
    ends = cfg.get("ends_kst")
    if not ends:
        return False
    try:
        return now >= datetime.strptime(ends, "%Y-%m-%d %H:%M").replace(tzinfo=KST)
    except ValueError:
        return False


def all_decision_dates(cfg: dict) -> list[str]:
    g = cfg["fomc"]
    out = []
    for k, v in g.items():
        if k.startswith("decision_dates_"):
            out.extend(v)
    return sorted(out)


def sep_dates(cfg: dict) -> set[str]:
    g = cfg["fomc"]
    out = set()
    for k, v in g.items():
        if k.startswith("sep_dates_"):
            out.update(v)
    return out


def fomc_due(cfg: dict, done: list[str], now: datetime) -> list[str]:
    """Meetings whose next-KST-morning window covers now (hour>=8 on send day)."""
    g = cfg["fomc"]
    catch = int(g.get("catchup_days", 4))
    today = now.date()
    out = []
    for d in all_decision_dates(cfg):
        if d in done:
            continue
        send = date.fromisoformat(d) + timedelta(days=1)
        if send <= today <= send + timedelta(days=catch) and (today > send or now.hour >= 8):
            out.append(d)
    return out


def normalize_stmt(text: str) -> str:
    text = text.replace("\u2013", "-").replace("\u2014", "-").replace("\u00a0", " ")
    text = re.sub(r"Share\s*-*>", " ", text, flags=re.I)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def main_policy_text(text: str) -> str:
    """Committee statement body without Voting for/against paragraphs."""
    m = re.search(r"\bVoting (for|against)\b", text, flags=re.I)
    return text[:m.start()].strip() if m else text


def fetch_statement(decision: str) -> str:
    ymd = decision.replace("-", "")
    url = f"https://www.federalreserve.gov/newsevents/pressreleases/monetary{ymd}a.htm"
    html = get(url)
    paras = []
    for p in re.findall(r"<p[^>]*>([\s\S]*?)</p>", html, re.I):
        t = re.sub(r"<[^>]+>", " ", p)
        t = re.sub(r"&\w+;", " ", t)
        t = re.sub(r"\s+", " ", t).strip()
        if t:
            paras.append(t)
    start = 0
    for i, t in enumerate(paras):
        if "Federal Open Market Committee approved" in t or t.startswith("The Committee decided"):
            start = i
            break
    out = []
    for t in paras[start:]:
        if t.startswith("Implementation Note") or t.startswith("For media inquiries"):
            break
        if t.startswith("For release at") and out:
            continue
        out.append(t)
        # stop after voting against paragraph when we already have the decision
        if out and t.lower().startswith("voting against"):
            break
    text = normalize_stmt(" ".join(out))
    # drop release-time / share chrome that sometimes sticks to the first paragraph
    text = re.sub(r"^For release at[^.]*(AM|PM|ET|EDT|EST)\.?\s*", "", text, flags=re.I)
    text = re.sub(r"(-+>\s*)+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    # prefer starting at the decision sentence when present
    m = re.search(r"The Committee decided\b.*", text)
    if m:
        # keep a short lead vote-count clause if present just before
        lead = text[:m.start()]
        vote = re.search(r"by a\s+[\d\s–-]+\s*vote:?\s*$", lead, flags=re.I)
        prefix = lead[vote.start():] if vote else ""
        text = (prefix + m.group(0)).strip()
    if "target range" not in text.lower() and "federal funds" not in text.lower():
        raise RuntimeError(f"성명 본문 파싱 실패 ({decision})")
    return text


def decision_action(text: str) -> str:
    t = main_policy_text(text).lower()
    if re.search(r"(raise|increase|raised|increased)\s+the\s+target\s+range", t):
        return "hike"
    if re.search(r"(reduce|lower|decrease|reduced|lowered|decreased)\s+the\s+target\s+range", t):
        return "cut"
    if "maintain the target range" in t or "decided to maintain" in t:
        return "hold"
    return "unknown"


def phrase_hits(text: str, phrases: list[str]) -> list[str]:
    t = text.lower()
    return [p for p in phrases if p.lower() in t]


def cut_dissent(text: str) -> bool:
    """True if a dissenter preferred a lower/cut path."""
    m = re.search(r"voting against.{0,400}", text, re.I)
    if not m:
        return False
    chunk = m.group(0).lower()
    return bool(re.search(r"preferr?ed to (reduce|lower|decrease|cut)", chunk))


def hike_dissent(text: str) -> bool:
    m = re.search(r"voting against.{0,400}", text, re.I)
    if not m:
        return False
    chunk = m.group(0).lower()
    return bool(re.search(r"preferr?ed to (raise|increase)", chunk))


def stmt_diff(prev: str | None, cur: str) -> list[str]:
    if not prev:
        return ["이전 성명 없음(첫 비교 또는 시드)"]
    # sentence-ish split
    def parts(s: str) -> list[str]:
        return [p.strip() for p in re.split(r"(?<=[.!?])\s+", s) if len(p.strip()) > 25]

    a, b = parts(prev), parts(cur)
    added = [x for x in b if x not in a]
    removed = [x for x in a if x not in b]
    # also phrase-level quick flags
    out = []
    for x in removed[:4]:
        out.append("삭제: " + x[:140])
    for x in added[:4]:
        out.append("추가: " + x[:140])
    if not out:
        # fallback difflib ratio note
        ratio = difflib.SequenceMatcher(None, prev, cur).ratio()
        out.append(f"문장 단위 큰 변화 없음(유사도 {ratio:.2f})")
    return out


def fetch_sep(decision: str) -> dict | None:
    ymd = decision.replace("-", "")
    url = f"https://www.federalreserve.gov/monetarypolicy/fomcprojtabl{ymd}.htm"
    try:
        html = get(url)
    except Exception as exc:
        if "404" in str(exc) or "Not Found" in str(exc):
            return None
        raise
    # header years under Median
    rows = re.findall(r"<tr[\s\S]*?</tr>", html, re.I)
    years: list[str] = []
    for r in rows:
        cells = [re.sub(r"<[^>]+>", "", c).strip() for c in re.findall(r"<t[hd][^>]*>([\s\S]*?)</t[hd]>", r, re.I)]
        if cells and all(re.fullmatch(r"\d{4}|Longer run", c or "") for c in cells if c) and any(re.fullmatch(r"\d{4}", c or "") for c in cells):
            # may be median years only in second header row
            yrs = [c for c in cells if re.fullmatch(r"\d{4}", c)]
            if len(yrs) >= 3 and not years:
                years = yrs
    medians: dict[str, float] = {}
    for r in rows:
        if "federal funds rate" not in r.lower():
            continue
        cells = [re.sub(r"<[^>]+>", "", c).strip() for c in re.findall(r"<t[hd][^>]*>([\s\S]*?)</t[hd]>", r, re.I)]
        # cells[0]=label, then median values for each year (+ longer run), then central, then range
        vals = []
        for c in cells[1:]:
            v = num(c.replace("\u2013", "-").split("-")[0] if "\u2013" in c or "–" in c else c)
            # only take simple decimals like 3.6 (median columns come first)
            if v is not None and c.replace(".", "", 1).isdigit() or (c and c[0].isdigit() and "–" not in c and "-" not in c[1:]):
                if re.fullmatch(r"\d+(\.\d+)?", c.replace(",", "")):
                    vals.append(float(c.replace(",", "")))
        if not years:
            # fallback: assume current year onward from decision
            y0 = int(decision[:4])
            years = [str(y0 + i) for i in range(len(vals) - 1)] + (["Longer run"] if vals else [])
        # map first len(years) medians; years list may exclude Longer run
        # From Fed table: Median columns = year1..yearN + Longer run
        n = min(len(vals), len(years) + 1)
        # rebuild years including longer run if needed
        ycols = years[:]
        if len(vals) == len(years) + 1:
            ycols = years + ["Longer run"]
        elif len(vals) > len(years) and "Longer run" not in ycols:
            ycols = years + ["Longer run"]
        for i, y in enumerate(ycols):
            if i < len(vals):
                medians[y] = vals[i]
        break
    if not medians:
        raise RuntimeError(f"SEP 연방기금 중앙값 파싱 실패 ({decision})")
    return {"meeting": decision, "url": url, "medians": medians, "years": years}


def fred_series(series: str, start: date) -> list[tuple[str, float]]:
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}&cosd={start.isoformat()}"
    txt = get(url, 35)
    rows = list(csv.reader(io.StringIO(txt)))
    out = []
    for r in rows[1:]:
        if len(r) >= 2:
            v = num(r[1])
            if v is not None:
                out.append((r[0], v))
    if not out:
        raise RuntimeError(f"FRED {series} 비어 있음")
    return out


def rate_change_on(decision: str) -> tuple[str, float | None, float | None]:
    """Compare DFEDTARU on decision day vs prior observation."""
    start = date.fromisoformat(decision) - timedelta(days=14)
    try:
        upper = fred_series("DFEDTARU", start)
        lower = fred_series("DFEDTARL", start)
    except Exception:
        return "unknown", None, None
    # find value on/after decision and previous
    def around(series):
        prev = None
        cur = None
        for d, v in series:
            if d < decision:
                prev = (d, v)
            elif d >= decision and cur is None:
                cur = (d, v)
        if cur is None and series:
            cur = series[-1]
        return prev, cur

    pu, cu = around(upper)
    pl, cl = around(lower)
    if not cu or not pu:
        return "unknown", None, None
    up_now, up_prev = cu[1], pu[1]
    # midpoint change
    mid_now = (cu[1] + (cl[1] if cl else cu[1])) / 2
    mid_prev = (pu[1] + (pl[1] if pl else pu[1])) / 2
    if mid_now < mid_prev - 0.05:
        return "cut", mid_now, mid_prev
    if mid_now > mid_prev + 0.05:
        return "hike", mid_now, mid_prev
    return "hold", mid_now, mid_prev


def next_year_key(decision: str, medians: dict[str, float]) -> str | None:
    y = str(int(decision[:4]) + 1)
    if y in medians:
        return y
    # else first calendar year after decision year present
    years = sorted(k for k in medians if k.isdigit())
    for yy in years:
        if int(yy) > int(decision[:4]):
            return yy
    return years[0] if years else None


def classify(cfg: dict, decision: str, cur_stmt: str, prev_stmt: str | None,
             sep: dict | None, prior_sep: dict | None,
             rate_act: str, mid_now: float | None) -> dict:
    sc = cfg["scoring"]
    bank = cfg["phrase_bank"]
    dovish = 0
    hawk = 0
    reasons: list[str] = []

    main_cur = main_policy_text(cur_stmt)
    main_prev = main_policy_text(prev_stmt) if prev_stmt else ""
    action = decision_action(cur_stmt)
    # prefer FRED if clear
    if rate_act in {"cut", "hike", "hold"}:
        action = rate_act if action == "unknown" else action
        if rate_act != action and rate_act != "unknown":
            # statement vs FRED disagreement: trust FRED for actual, note both
            reasons.append(f"성명 판정={decision_action(cur_stmt)}, FRED={rate_act} -> FRED 우선")
            action = rate_act

    if action == "cut":
        dovish += sc["actual_cut"]
        reasons.append(f"실제 인하(+{sc['actual_cut']})")
    elif action == "hike":
        hawk += sc["hawk_actual_hike"]
        reasons.append(f"실제 인상(+{sc['hawk_actual_hike']} 매파)")

    prev_firm = phrase_hits(main_prev, bank["firming"]) if main_prev else []
    cur_firm = phrase_hits(main_cur, bank["firming"])
    prev_ease = phrase_hits(main_prev, bank["easing"]) if main_prev else []
    cur_ease = phrase_hits(main_cur, bank["easing"])

    removed_firm = [p for p in prev_firm if p not in cur_firm]
    added_firm = [p for p in cur_firm if p not in prev_firm]
    added_ease = [p for p in cur_ease if p not in prev_ease]

    if removed_firm:
        dovish += sc["removed_firming"]
        reasons.append(f"매파 표현 삭제(+{sc['removed_firming']}): {', '.join(removed_firm)}")
    if added_ease:
        dovish += sc["easing_language_added"]
        reasons.append(f"완화 표현 추가(+{sc['easing_language_added']}): {', '.join(added_ease)}")
    if added_firm:
        hawk += sc["hawk_added_firming"]
        reasons.append(f"매파 표현 추가(+{sc['hawk_added_firming']} 매파): {', '.join(added_firm)}")

    if cut_dissent(cur_stmt):
        dovish += sc["cut_dissent"]
        reasons.append(f"인하 선호 반대표(+{sc['cut_dissent']})")
    if hike_dissent(cur_stmt):
        hawk += 1
        reasons.append("인상 선호 반대표(+1 매파)")

    sep_note = "SEP 없음(이번 회의)"
    if sep and sep.get("medians"):
        mid = mid_now
        if mid is None:
            # parse from statement "3-3/4 to 4" roughly
            m = re.search(r"(\d)(?:-(\d)/(\d))?\s+to\s+(\d)(?:-(\d)/(\d))?\s+percent", cur_stmt, re.I)
            if m:
                def frac(a, b, c):
                    x = float(a)
                    if b and c:
                        x += float(b) / float(c)
                    return x
                lo = frac(m.group(1), m.group(2), m.group(3))
                hi = frac(m.group(4), m.group(5), m.group(6))
                mid = (lo + hi) / 2
        ny = next_year_key(decision, sep["medians"])
        if ny and mid is not None:
            med = sep["medians"][ny]
            sep_note = f"SEP {ny} 중앙값 {med:.2f}%, 현재 구간 중점 {mid:.2f}%"
            if med < mid - 0.05:
                dovish += sc["sep_median_below_mid"]
                reasons.append(f"점도표 {ny} 중앙값 {med:.2f}% < 현재 중점 {mid:.2f}%(+{sc['sep_median_below_mid']})")
            elif med > mid + 0.25:
                hawk += sc["hawk_sep_median_higher"]
                reasons.append(f"점도표 {ny} 중앙값 {med:.2f}% >> 현재 중점(+{sc['hawk_sep_median_higher']} 매파)")
        if prior_sep and prior_sep.get("medians") and ny:
            pm = prior_sep["medians"].get(ny)
            cm = sep["medians"].get(ny)
            if pm is not None and cm is not None:
                if cm < pm - 0.05:
                    dovish += sc["sep_median_lower_vs_prior"]
                    reasons.append(f"점도표 {ny} 중앙값 직전 SEP {pm:.2f}% -> {cm:.2f}%(+{sc['sep_median_lower_vs_prior']})")
                elif cm > pm + 0.05:
                    hawk += sc["hawk_sep_median_higher"]
                    reasons.append(f"점도표 {ny} 중앙값 상승 {pm:.2f}% -> {cm:.2f}%(매파)")
                sep_note += f", 직전 SEP {pm:.2f}%"

    net = dovish - hawk
    if action == "hike" or (hawk >= 3 and hawk > dovish):
        klass = "매파 유지"
    elif dovish >= sc["entry_min"] and net >= 2:
        klass = "인하 사이클 진입"
    elif dovish >= sc["strengthen_min"] and net >= 1:
        klass = "인하 신호 강화"
    elif hawk > dovish:
        klass = "매파 유지"
    else:
        klass = "중립"

    return {
        "class": klass,
        "dovish": dovish,
        "hawk": hawk,
        "net": net,
        "action": action,
        "reasons": reasons,
        "sep_note": sep_note,
        "diff": stmt_diff(prev_stmt, cur_stmt),
        "mid": mid_now,
    }


def yahoo_tyx() -> tuple[str, float]:
    from datetime import timezone as _tz
    url = "https://query1.finance.yahoo.com/v8/finance/chart/%5ETYX?interval=1d&range=1mo"
    res = get_json(url)["chart"]["result"][0]
    ts = res.get("timestamp") or []
    closes = (res.get("indicators") or {}).get("quote", [{}])[0].get("close") or []
    last_d, last_v = None, None
    for t, c in zip(ts, closes):
        if c is None:
            continue
        last_d = datetime.fromtimestamp(int(t), tz=_tz.utc).date().isoformat()
        last_v = float(c)
    if last_d is None:
        raise RuntimeError("^TYX 없음")
    return last_d, last_v


def fred_dgs30() -> tuple[str, float]:
    start = date.today() - timedelta(days=30)
    rows = fred_series("DGS30", start)
    return rows[-1]


def us30y() -> tuple[float, str, str]:
    fred = tyx = None
    fe = te = None
    try:
        fred = fred_dgs30()
    except Exception as exc:
        fe = exc
    try:
        tyx = yahoo_tyx()
    except Exception as exc:
        te = exc
    if fred and tyx:
        if tyx[0] > fred[0]:
            return tyx[1], tyx[0], "Yahoo ^TYX"
        return fred[1], fred[0], "FRED DGS30"
    if fred:
        return fred[1], fred[0], "FRED DGS30"
    if tyx:
        return tyx[1], tyx[0], "Yahoo ^TYX"
    raise RuntimeError(f"US30Y 실패 ({fe}; {te})")


def naver_close(code: str) -> tuple[str, float]:
    raw = get_json(f"https://m.stock.naver.com/api/stock/{code}/price?pageSize=5&page=1")
    for r in raw if isinstance(raw, list) else []:
        d = str(r.get("localTradedAt") or "")[:10]
        c = num(r.get("closePrice"))
        if len(d) == 10 and c:
            return d, c
    raise RuntimeError(f"{code} 시세 없음")


def body(stamp: str, hdr: str, what: str, status: str, action: str, later: str, trim: str, avoid: str) -> str:
    return "\n".join([
        f"{stamp} | FOMC-CUT | {hdr}",
        f"이게 뭐 알림: {what}",
        f"지금 결과/상태: {status}",
        f"지금 할 일: {action}",
        f"나중에: {later}",
        f"익절/줄일 때: {trim}",
        f"하지 말 것: {avoid}",
    ])


def market_snapshot(cfg: dict) -> dict:
    out: dict = {}
    fails = []
    try:
        y, yd, ys = us30y()
        out["y30"] = y
        out["y30_day"] = yd
        out["y30_src"] = ys
    except Exception as exc:
        fails.append(f"US30Y ({exc})")
    for code in cfg["linked"]["tickers"]:
        try:
            d, c = naver_close(code)
            out[code] = {"day": d, "close": c}
        except Exception as exc:
            fails.append(f"{code} ({exc})")
    out["fails"] = fails
    return out


def distance_lines(cfg: dict, snap: dict) -> str:
    th = cfg["linked"]["thresholds"]
    y = snap.get("y30")
    bits = []
    if y is not None:
        bits.append(f"US30Y {y:.3f}%({snap.get('y30_day')}, {snap.get('y30_src')})")
        a = th["453850"]
        bits.append(f"453850선: 5.00%까지 {(y - a['y_upgrade']) * 100:+.0f}bp, 5.50%까지 {(a['y_risk'] - y) * 100:+.0f}bp")
        b = th["476760"]
        bits.append(f"476760선: 5.40%까지 {(y - b['y_raise']) * 100:+.0f}bp, 5.50%까지 {(b['y_review'] - y) * 100:+.0f}bp")
    for code in ("453850", "476760"):
        px = snap.get(code) or {}
        if px.get("close") is not None:
            if code == "453850":
                lvl = th[code]["px_low"]
                bits.append(f"453850 종가 {px['close']:,.0f}원({px['day']}), 6,685까지 {px['close'] - lvl:+,.0f}원")
            else:
                lvl = th[code]["px_add"]
                bits.append(f"476760 종가 {px['close']:,.0f}원({px['day']}), 8,300까지 {px['close'] - lvl:+,.0f}원")
    return ". ".join(bits) + ("." if bits else "")


def build_signal(cfg, stamp, decision, result, snap) -> tuple[str, str, int]:
    klass = result["class"]
    if klass == "인하 사이클 진입":
        title = "[453850 / 476760] FOMC 인하 사이클 진입 | 관망 해제 점검"
    else:
        title = "[453850 / 476760] FOMC 인하 신호 | 관망 해제 점검"
    checklist = (
        f"분류 {klass} (비둘기 {result['dovish']} / 매파 {result['hawk']}, 행동 {result['action']}). "
        f"근거: {'; '.join(result['reasons']) or '없음'}. {result['sep_note']}. "
        f"성명 변화: {' / '.join(result['diff'][:3])}."
    )
    status = (
        f"회의 {decision}. {checklist} {distance_lines(cfg, snap)} "
        f"점검표: 453850 관망해제(US30Y<5.0% 또는 FOMC 인하 신호), 476760 비중/분할 선(5.4%/5.5%/8,300) 재확인."
    )
    msg = body(
        stamp, f"{decision} {klass}",
        "FOMC 성명과 점도표(해당 시)로 인하 사이클 신호를 본 뒤 453850/476760 관망 해제를 점검하는 알림입니다. 매매 신호가 아닙니다.",
        scrub(status),
        "관망 해제 여부를 점검하세요. 금리 선(5.0%/5.4%)과 가격 선(6,685/8,300)을 같이 보고 소액 분할만 검토하세요.",
        "같은 회의는 다시 안 옵니다. 다음 FOMC 다음 morning에 다시 판정합니다. k453850/476760 본문에도 'FOMC 인하 신호 확인됨'이 붙습니다.",
        "해당 없음(미보유면). 보유 중이면 듀레이션 위험을 먼저 보세요.",
        "이 알림만으로 한 번에 사기. 조회 실패를 신호로 읽기.",
    )
    return scrub(title), scrub(msg), 4


def build_brief(cfg, stamp, decision, result, snap) -> tuple[str, str, int]:
    klass = result["class"]
    title = f"{TAG} {decision[5:7]}/{decision[8:]} 요약 | {klass}"
    status = (
        f"회의 {decision}. 분류 {klass} (비둘기 {result['dovish']} / 매파 {result['hawk']}, 행동 {result['action']}). "
        f"근거: {'; '.join(result['reasons']) or '뚜렷한 가점 없음'}. {result['sep_note']}. "
        f"성명 변화: {' / '.join(result['diff'][:2])}. {distance_lines(cfg, snap)}"
    )
    msg = body(
        stamp, f"{decision} {klass}",
        "FOMC 결정 다음 morning 요약입니다. 인하 신호가 아니라 짧은 기록용입니다. 매매 신호가 아닙니다.",
        scrub(status),
        "관망 유지. 453850/476760 금리 선이 깨질 때만 각 알림을 보세요.",
        "다음 FOMC 다음 morning에 다시 옵니다.",
        "해당 없음.",
        "이 요약을 매수 신호로 읽기.",
    )
    return scrub(title), scrub(msg), 3


def ops_msg(stamp: str, status: str) -> tuple[str, str]:
    t = f"{TAG} 조회 실패 | 판정 보류"
    m = body(stamp, "조회 실패", "FOMC 인하 신호 점검이 데이터를 못 읽었습니다.", status,
             "다음 morning 재시도를 기다리세요.", "회의당 1번 원칙은 유지됩니다.",
             "이 실패만으로 포지션을 바꾸지 마세요.", "조회 실패를 인하 신호로 읽기.")
    return scrub(t), scrub(m)


def install_message(cfg, stamp, snap) -> tuple[str, str]:
    nxt = []
    today = date.today()
    for d in all_decision_dates(cfg):
        if date.fromisoformat(d) >= today - timedelta(days=1):
            nxt.append(f"{d} -> {(date.fromisoformat(d)+timedelta(days=1)).isoformat()} 09:00")
        if len(nxt) >= 3:
            break
    t = f"{TAG} 설치 확인 | 매매 신호 아님"
    status = (
        f"{distance_lines(cfg, snap)} 다음 예정: {', '.join(nxt) or '설정 없음'}. "
        f"SEP 회의: {', '.join(sorted(sep_dates(cfg)))}."
    )
    m = body(
        stamp, "설치 확인",
        "FOMC 성명+점도표 인하 사이클 감지 알림 설치 확인입니다. 453850/476760에 연결됩니다. 매매 신호가 아닙니다.",
        scrub(status),
        "알림이 도착했는지 확인만 하세요.",
        "평일 morning 09:00에 결정일 다음 날만 판정합니다. 인하 신호 강화/진입이면 [453850 / 476760] 제목으로 1번, "
        "중립/매파면 짧은 [FOMC-CUT] 요약 1번. 폐기된 CME/의사록 일회 알림은 재도입하지 않습니다.",
        "해당 없음.",
        "이 메시지를 매매 신호로 해석하기.",
    )
    m += f"\n꺼지는 때: {cfg.get('ends_kst')} ({cfg.get('ends_note')})"
    return scrub(t), scrub(m)


def analyze_meeting(cfg: dict, st: dict, decision: str) -> tuple[dict, dict, list[str]]:
    fails: list[str] = []
    cur_stmt = None
    sep = None
    try:
        cur_stmt = fetch_statement(decision)
    except Exception as exc:
        fails.append(f"성명 ({exc})")
    is_sep = decision in sep_dates(cfg)
    if is_sep:
        try:
            sep = fetch_sep(decision)
        except Exception as exc:
            fails.append(f"SEP ({exc})")
    rate_act, mid_now, mid_prev = rate_change_on(decision)
    prev_stmt = (st.get("prior_statement") or {}).get("text")
    # if prior is same meeting somehow, ignore
    if (st.get("prior_statement") or {}).get("meeting") == decision:
        prev_stmt = None
    prior_sep = st.get("prior_sep") or None
    if not cur_stmt:
        raise RuntimeError("; ".join(fails) or "성명 없음")
    result = classify(cfg, decision, cur_stmt, prev_stmt, sep, prior_sep, rate_act, mid_now)
    result["statement"] = cur_stmt
    result["sep"] = sep
    result["rate_act"] = rate_act
    result["mid_prev"] = mid_prev
    result["is_sep"] = is_sep
    result["fails"] = fails
    return result, {"cur_stmt": cur_stmt, "sep": sep}, fails


def apply_signal_flag(st: dict, decision: str, result: dict, stamp: str) -> None:
    active = result["class"] in SIGNAL_CLASSES
    st["signal"] = {
        "active": active,
        "class": result["class"],
        "meeting": decision,
        "score": {"dovish": result["dovish"], "hawk": result["hawk"], "net": result["net"]},
        "action": result["action"],
        "updated_at": stamp,
        "reasons": result["reasons"][:6],
    }


def run(mode: str, as_of: datetime | None = None) -> int:
    dry = mode in {"dry", "test", "dry-run"}
    cfg = load_cfg()
    now = as_of or datetime.now(KST)
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    topic = resolve_topic("port", cfg.get("ntfy_topic"))
    server = resolve_server(cfg.get("ntfy_server"))
    st = load_state()

    if ended(cfg, now) and mode not in {"install", "selftest", "dry", "force"}:
        print(f"{now.date()} past ends_kst; silent")
        return 0

    if mode == "install":
        snap = market_snapshot(cfg)
        t, m = install_message(cfg, stamp, snap)
        check_text(t, m)
        print(stamp, f"{NAME} mode=install")
        print(t)
        print(m)
        if not dry:
            publish(topic, t, m, 3, ["white_check_mark"], server, config_name=NAME)
            print("install confirmation sent (state untouched)")
        return 0

    due = fomc_due(cfg, st.get("done") or [], now)
    if mode == "force" and not due:
        # force latest past decision not in done, or last calendar date <= today
        past = [d for d in all_decision_dates(cfg) if date.fromisoformat(d) <= now.date()]
        due = [past[-1]] if past else []

    if mode in {"seed"} or not st.get("seeded"):
        # seed: mark meetings whose catchup window fully expired; leave current catchup dues
        catch = int(cfg["fomc"].get("catchup_days", 4))
        past = [
            d for d in all_decision_dates(cfg)
            if date.fromisoformat(d) + timedelta(days=1 + catch) < now.date()
        ]
        st["done"] = sorted(set(st.get("done") or []) | set(past))
        # load most recent statement as prior
        if past:
            last = past[-1]
            try:
                text = fetch_statement(last)
                st["prior_statement"] = {"meeting": last, "text": text}
            except Exception as exc:
                print(f"seed statement skip: {exc}")
            if last in sep_dates(cfg):
                try:
                    st["prior_sep"] = fetch_sep(last) or st.get("prior_sep")
                except Exception as exc:
                    print(f"seed sep skip: {exc}")
            else:
                # find last SEP before now
                seps = sorted(d for d in sep_dates(cfg) if date.fromisoformat(d) < now.date())
                if seps:
                    try:
                        st["prior_sep"] = fetch_sep(seps[-1]) or {}
                    except Exception as exc:
                        print(f"seed prior sep skip: {exc}")
        st["seeded"] = True
        st["seed_note"] = f"{stamp} 시드. done={st['done'][-4:]}"
        st["updated_at"] = now.isoformat()
        print(st["seed_note"])
        if not dry:
            save_state(st)
        else:
            print("DRY RUN: no publish, state not written")
        due = fomc_due(cfg, st.get("done") or [], now)
        if mode == "seed" or not due:
            return 0

    if not due:
        print("no FOMC due; silent")
        return 0

    sends: list[tuple[str, str, int, str]] = []
    for decision in due:
        print(f"process {decision}")
        try:
            result, arts, fails = analyze_meeting(cfg, st, decision)
        except Exception as exc:
            traceback.print_exc()
            if st.get("ops_day") != now.date().isoformat():
                ot, om = ops_msg(stamp, f"회의 {decision} 실패: {exc}")
                check_text(ot, om)
                sends.append((ot, om, 3, resolve_topic("ops")))
                if not dry:
                    st["ops_day"] = now.date().isoformat()
            continue

        snap = market_snapshot(cfg)
        apply_signal_flag(st, decision, result, stamp)
        if result["class"] in SIGNAL_CLASSES:
            title, msg, pri = build_signal(cfg, stamp, decision, result, snap)
        elif cfg.get("send_brief_on_neutral_or_hawkish", True):
            title, msg, pri = build_brief(cfg, stamp, decision, result, snap)
        else:
            title = msg = None
            pri = 3
            print(f"{decision} {result['class']}; brief disabled, silent")

        if title:
            check_text(title, msg)
            sends.append((title, msg, pri, topic))

        # update priors / done
        st.setdefault("done", []).append(decision)
        st["prior_statement"] = {"meeting": decision, "text": arts["cur_stmt"]}
        if arts.get("sep"):
            st["prior_sep"] = arts["sep"]
        st["last"] = {
            "meeting": decision,
            "class": result["class"],
            "dovish": result["dovish"],
            "hawk": result["hawk"],
            "action": result["action"],
        }
        print(f"class={result['class']} dovish={result['dovish']} hawk={result['hawk']} action={result['action']}")
        print("reasons:", result["reasons"])

    st["updated_at"] = now.isoformat()
    for title, msg, pri, top in sends:
        print(title)
        print(msg)
        print(f"topic={top} send={not dry}")
        if not dry:
            tags = ["warning"] if top == resolve_topic("ops") else ["bank"]
            publish(top, title, msg, pri, tags, server, config_name=NAME)
    if dry:
        print("DRY RUN: no publish, state not written")
    else:
        save_state(st)
    return 0


def selftest() -> int:
    cfg = load_cfg()
    assert "2026-12-09" in cfg["fomc"]["decision_dates_2026"]
    assert "2026-12-09" in cfg["fomc"]["sep_dates_2026"]
    # due window
    assert fomc_due(cfg, [], datetime(2026, 12, 10, 9, 0, tzinfo=KST)) == ["2026-12-09"]
    assert fomc_due(cfg, [], datetime(2026, 12, 10, 7, 0, tzinfo=KST)) == []
    assert fomc_due(cfg, ["2026-12-09"], datetime(2026, 12, 10, 9, 0, tzinfo=KST)) == []
    assert fomc_due(cfg, [], datetime(2026, 12, 12, 9, 0, tzinfo=KST)) == ["2026-12-09"]

    # synthetic classify: cut + removed firming + sep below -> entry
    cur = (
        "The Committee decided to reduce the target range for the federal funds rate by 1/4 percentage point "
        "to 3-1/2 to 3-3/4 percent. The Committee will carefully assess incoming data. "
        "Voting against the monetary policy action was Jane Doe, who preferred to lower the target range by 1/2 percentage point."
    )
    prev = (
        "The Committee decided to maintain the target range for the federal funds rate at 3-3/4 to 4 percent. "
        "The Committee judges that additional policy firming may be appropriate."
    )
    sep = {"medians": {"2026": 3.6, "2027": 3.1, "2028": 3.0, "Longer run": 3.0}, "years": ["2026", "2027", "2028"]}
    prior_sep = {"medians": {"2026": 3.6, "2027": 3.4, "2028": 3.1, "Longer run": 3.0}, "years": ["2026", "2027", "2028"]}
    r = classify(cfg, "2026-12-09", cur, prev, sep, prior_sep, "cut", 3.875)
    assert r["class"] in SIGNAL_CLASSES, r
    assert r["dovish"] >= cfg["scoring"]["strengthen_min"], r

    # hawkish hold with hike dissent
    cur2 = (
        "The Committee decided to maintain the target range for the federal funds rate at 3-3/4 to 4 percent. "
        "Voting against the monetary policy action were A and B, who preferred to raise the target range "
        "for the federal funds rate by 1/4 percentage point at this meeting."
    )
    prev2 = "The Committee decided to raise the target range for the federal funds rate by 1/4 percentage point to 3-3/4 to 4 percent."
    r2 = classify(cfg, "2026-10-28", cur2, prev2, None, None, "hold", 3.875)
    assert r2["class"] in {"중립", "매파 유지"}, r2

    snap = {
        "y30": 5.28, "y30_day": "2026-11-04", "y30_src": "Yahoo ^TYX",
        "453850": {"day": "2026-11-05", "close": 7170},
        "476760": {"day": "2026-11-05", "close": 8500},
        "fails": [],
    }
    t, m, p = build_signal(cfg, "2026-12-10 09:00 KST", "2026-12-09", r, snap)
    check_text(t, m)
    assert "453850" in t and "476760" in t
    assert "관망" in t
    tb, mb, _ = build_brief(cfg, "T", "2026-10-28", r2, snap)
    check_text(tb, mb)
    for ch in FORBIDDEN:
        assert ch not in t and ch not in m and ch not in tb and ch not in mb

    # flag helper
    st = load_state()
    apply_signal_flag(st, "2026-12-09", r, "T")
    assert st["signal"]["active"] is True

    print("fire signal:", t)
    print("fire brief:", tb)
    print("selftest ok")
    return 0


def main() -> int:
    argv = sys.argv[1:]
    mode = (argv[0] if argv else "auto").lower()
    as_of = None
    if "--as-of" in argv:
        i = argv.index("--as-of")
        as_of = datetime.strptime(argv[i + 1], "%Y-%m-%d %H:%M").replace(tzinfo=KST)
    if mode == "selftest":
        return selftest()
    if mode not in {"auto", "dry", "install", "force", "seed"}:
        print(f"usage: {NAME}_monitor.py <auto|dry|install|force|seed|selftest> [--as-of 'YYYY-MM-DD HH:MM']")
        return 2
    try:
        if mode == "dry":
            print("DRY RUN")
        return run(mode, as_of)
    except Exception as exc:
        traceback.print_exc()
        try:
            cfg = load_cfg()
            t, m = ops_msg(datetime.now(KST).strftime("%Y-%m-%d %H:%M KST"), f"스크립트 오류 {type(exc).__name__}: {str(exc)[:200]}")
            if mode not in {"dry", "selftest"}:
                publish(resolve_topic("ops"), t, m, 3, ["warning"], resolve_server(cfg.get("ntfy_server")), config_name=NAME)
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
