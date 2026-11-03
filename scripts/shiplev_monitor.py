#!/usr/bin/env python3
"""[SHIPLEV] 466920 20일선 3거래일 회복 + 클락슨 신조선가지수 3개월 연속 하락 (사용자 승인 2026-11-03).

조건 1 (ma): 466920 확정 종가 > 20일선(그날 포함 20거래일 단순평균, 같으면 위 아님) 3거래일 연속이면 p3 1번
  '[SHIPLEV] 466920 20일선 위 3일 | 2배 단기 검토'. 종가 < 20일선 2거래일 연속이면 재무장.
  15:20 은 장 마감 전이라 오늘 행은 항상 뺀다. Daum KRX 일별 우선, 실패 시 Naver siseJson.
  state px.last_date 보다 새 날짜만 날짜순 판정. state 가 비면 이력을 조용히 재생해 seed(첫 실행 몰아 보내기 없음).
조건 2 (nbi): 직전 달 클락슨 신조선가지수가 state 에 없을 때만 네이버 뉴스 검색으로 'N월 말 ... 신조선가지수 ... 186.59'
  꼴을 파싱. 새 달 값이 들어온 날만 판정, 전월보다 낮은 달이 3개월 연속이면 그 달 키로 p3 1번.
  파싱 실패는 새 달로 보지 않음. 15일 이후에도 직전 달 값이 없으면 cwally-ops 하루 1번.
수집 실패(시세, 검색)는 cwally-ops 하루 1번(종류별).
modes: auto | dry(발송 없음, state 안 씀) | selftest
"""
from __future__ import annotations

import collections
import copy
import html
import json
import math
import re
import sys
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import ENDS_PREFIX, ends_line, publish, resolve_server, resolve_topic, route_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
CFG_PATH = ROOT / "configs" / "shiplev.json"
STATE_PATH = ROOT / "state" / "shiplev.json"
UA = {"User-Agent": "Mozilla/5.0 (cwally-alerts)"}
BANNED = ("\u00b7", "\u3186", "\u318d", "\u2014", "매수", "신호")
TITLE_MA = "[SHIPLEV] 466920 20일선 위 3일 | 2배 단기 검토"
TITLE_NBI = "[SHIPLEV] 466920 신조선가 3개월 연속 하락 | 조선 비중 점검"


# ---------------------------------------------------------------- data
def _get(url: str, headers: dict | None = None, timeout: int = 20) -> bytes:
    h = dict(UA)
    h.update(headers or {})
    with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=timeout) as r:
        return r.read()


def naver_sise(code: str, days: int = 90) -> list[tuple[str, float]]:
    end = datetime.now(KST).date()
    start = end - timedelta(days=days)
    url = (f"https://api.finance.naver.com/siseJson.naver?symbol={code}&requestType=1"
           f"&startTime={start:%Y%m%d}&endTime={end:%Y%m%d}&timeframe=day")
    txt = _get(url).decode("utf-8", "ignore")
    out = []
    for m in re.finditer(r'\[\s*"(\d{8})"\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)', txt):
        d = m.group(1)
        out.append((f"{d[:4]}-{d[4:6]}-{d[6:]}", float(m.group(5))))
    if not out:
        raise RuntimeError(f"{code} Naver siseJson 0건")
    return sorted(set(out))


def closes(code: str, n: int = 60) -> tuple[list[tuple[str, float]], str]:
    """Oldest-first (day, close). Today's row is NOT removed here."""
    try:
        import krx_close
        bars = krx_close._daum_bars(code, n)  # noqa: SLF001
        rows = [(b.day, b.close) for b in bars]
        if len(rows) < 25:
            raise RuntimeError(f"Daum {len(rows)}건")
        return rows[-n:], "daum"
    except Exception as exc:  # noqa: BLE001
        print(f"daum {code} failed: {type(exc).__name__}: {exc}; naver siseJson fallback")
        return naver_sise(code)[-n:], "naver"


def fx_latest(today: str) -> tuple[str, float] | None:
    try:
        url = "https://m.stock.naver.com/front-api/marketIndex/prices?category=exchange&reutersCode=FX_USDKRW&page=1&pageSize=5"
        rows = json.loads(_get(url).decode("utf-8", "ignore"))["result"]
        for r in rows:
            d = str(r.get("localTradedAt"))[:10]
            if d < today:
                return d, float(str(r["closePrice"]).replace(",", ""))
    except Exception as exc:  # noqa: BLE001
        print(f"fx fail: {exc}")
    return None


# ---------------------------------------------------------------- condition 1
def ma_series(rows: list[tuple[str, float]], n: int = 20) -> list[dict]:
    out = []
    for i in range(n - 1, len(rows)):
        win = [c for _, c in rows[i - n + 1:i + 1]]
        out.append({"day": rows[i][0], "close": rows[i][1], "ma": sum(win) / n})
    return out


def vol_ann(rows: list[tuple[str, float]], n: int = 20) -> float | None:
    c = [x for _, x in rows]
    if len(c) < n + 1:
        return None
    r = [c[i] / c[i - 1] - 1 for i in range(len(c) - n, len(c))]
    m = sum(r) / n
    return math.sqrt(sum((x - m) ** 2 for x in r) / (n - 1)) * math.sqrt(252)


def judge_ma(cfg: dict, px: dict, series: list[dict]) -> list[dict]:
    """Walk new days in order. Returns rows on which the 3-day recovery fired."""
    need, rearm = cfg["ma"]["above_days"], cfg["ma"]["rearm_below_days"]
    px.setdefault("armed", True)
    px.setdefault("above_streak", 0)
    px.setdefault("below_streak", 0)
    fired = []
    for row in series:
        if row["day"] <= px.get("last_date", ""):
            continue
        c, ma = row["close"], row["ma"]
        if c > ma:
            px["above_streak"] += 1
            px["below_streak"] = 0
            if px["above_streak"] >= need and px["armed"]:
                px["armed"] = False
                px["last_fire_date"] = row["day"]
                fired.append(row)
        elif c < ma:
            px["below_streak"] += 1
            px["above_streak"] = 0
            if px["below_streak"] >= rearm and not px["armed"]:
                px["armed"] = True
        else:  # equal: not above, not below
            px["above_streak"] = px["below_streak"] = 0
        px["last_date"], px["last_close"], px["last_ma20"] = row["day"], c, round(ma, 2)
    return fired


# ---------------------------------------------------------------- condition 2
_V = r"(1\d{2}\.\d{2})"
_NAME = r"클락슨\s*(?:리서치\s*)?(?:의\s*)?신조선\s?가\s?지수\s*(?:\([^)]{0,40}\))?"
PA = re.compile(r"(?:(20\d{2})년\s*)?(\d{1,2})월\s*말\s*(?:기준\s*)?(?:,\s*)?" + _NAME + r"\s*(?:는|은|가|이)?\s*" + _V)
PB = re.compile(_NAME + r"\s*(?:는|은|가|이)?\s*(?:지난\s*)?(?:(20\d{2})년\s*)?(\d{1,2})월\s*말\s*(?:기준\s*)?(?:으로\s*)?" + _V)
PREV = re.compile(r"^[^.]{0,30}?전월\s*(?:\(?\d{1,2}월\s*말?\)?\s*)?(?:의\s*)?" + _V)


def _ym(year: str | None, mon: int, today: date) -> str:
    y = int(year) if year else (today.year if mon <= today.month else today.year - 1)
    return f"{y:04d}-{mon:02d}"


def parse_nbi(text: str, today: date) -> list[tuple[str, float]]:
    out = []
    for pat in (PA, PB):
        for m in pat.finditer(text):
            y, mon, v = m.group(1), int(m.group(2)), float(m.group(3))
            if not 1 <= mon <= 12 or not 100 < v < 300:
                continue
            ym = _ym(y, mon, today)
            if ym > today.strftime("%Y-%m"):
                continue
            out.append((ym, v))
            p = PREV.match(text[m.end():m.end() + 60])
            if p:
                yy, mm = int(ym[:4]), int(ym[5:])
                yy, mm = (yy - 1, 12) if mm == 1 else (yy, mm - 1)
                out.append((f"{yy:04d}-{mm:02d}", float(p.group(1))))
    return out


def search_items(query: str) -> list[tuple[str, str]]:
    url = "https://search.naver.com/search.naver?where=news&sort=1&query=" + urllib.parse.quote(query)
    t = _get(url).decode("utf-8", "ignore")
    items = []
    for m in re.finditer(r"<a\b([^>]*)>(.*?)</a>", t, flags=re.S):
        txt = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", m.group(2))))
        h = re.search(r'href="([^"]+)"', m.group(1))
        if h and "신조선" in txt:
            items.append((html.unescape(h.group(1)), txt))
    return items


def collect_nbi(items: list[tuple[str, str]], today: date) -> dict[str, dict]:
    """month -> {value, sources}. Majority value per month; a tie between different values is dropped."""
    votes: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    srcs: dict[tuple[str, float], list[str]] = collections.defaultdict(list)
    for href, txt in items:
        for ym, v in set(parse_nbi(txt, today)):
            votes[ym][v] += 1
            if href not in srcs[(ym, v)]:
                srcs[(ym, v)].append(href)
    out = {}
    for ym, cnt in votes.items():
        top = cnt.most_common()
        if len(top) > 1 and top[0][1] == top[1][1]:
            print(f"nbi {ym} conflict {top}; skipped")
            continue
        v = top[0][0]
        out[ym] = {"value": v, "sources": srcs[(ym, v)][:3]}
    return out


def prev_month(today: date) -> str:
    first = today.replace(day=1)
    return (first - timedelta(days=1)).strftime("%Y-%m")


def month_minus(ym: str, k: int) -> str:
    y, m = int(ym[:4]), int(ym[5:])
    m -= k
    while m < 1:
        m += 12
        y -= 1
    return f"{y:04d}-{m:02d}"


def down_streak(months: dict, ym: str) -> int:
    n, cur = 0, ym
    while True:
        p = month_minus(cur, 1)
        if cur in months and p in months and months[cur]["value"] < months[p]["value"]:
            n += 1
            cur = p
        else:
            return n


def judge_nbi(cfg: dict, nb: dict, found: dict, today: date, stamp: str) -> list[str]:
    """Merge months newer than the latest stored month. Returns month keys to alert."""
    months = nb.setdefault("months", {})
    latest = max(months) if months else ""
    fire = []
    for ym in sorted(found):
        if ym <= latest:
            continue
        prev = months.get(month_minus(ym, 1))
        if prev and abs(found[ym]["value"] / prev["value"] - 1) > 0.10:
            print(f"nbi {ym} {found[ym]['value']} jump vs {prev['value']}; skipped")
            continue
        months[ym] = {"value": found[ym]["value"], "sources": found[ym]["sources"], "added": stamp}
        latest = ym
        if down_streak(months, ym) >= cfg["nbi"]["down_months"] and ym not in nb.setdefault("sent", []):
            nb["sent"].append(ym)
            fire.append(ym)
    return fire


# ---------------------------------------------------------------- messages
def finish(lines: list[str], cfg: dict) -> str:
    end = ends_line("shiplev") or f"{ENDS_PREFIX} 종료일 없음"
    return "\n".join(lines + [end, cfg["footer"]])


def check_text(title: str, msg: str) -> None:
    for b in BANNED:
        assert b not in title + msg, f"banned {b!r}"
    assert "466920" in title


def msg_ma(cfg: dict, row: dict, last3: list[dict], lev: tuple[str, float] | None, vol: float | None,
           fx: tuple[str, float] | None, stamp: str) -> tuple[str, str, int]:
    three = ", ".join(f"{r['day'][5:]} {r['close']:,.0f}/{r['ma']:,.0f}" for r in last3)
    levtxt = f"0080Y0 {lev[0]} 종가 {lev[1]:,.0f}원" if lev else "0080Y0 종가 미표시"
    if vol is not None:
        decay = (vol ** 2 / 2) * 100 + cfg["vol"]["fee_half_year_pct"]
        voltxt = f"466920 20일 연율 변동성 {vol * 100:.1f}%. 이 변동성이면 2배가 6개월 횡보할 때 감쇠 약 {decay:.1f}% (sigma^2/2 + 0.25%)."
    else:
        voltxt = "466920 20일 변동성 미표시."
    fxtxt = f"원/달러 {fx[0]} 종가 {fx[1]:,.1f}원 (정보용, 판정 없음)." if fx else "원/달러 미표시 (정보용)."
    lines = [f"{stamp} | SHIPLEV | 466920 20일선 회복",
             "이게 뭐 알림: 466920 확정 종가가 20일선 위에서 3거래일 연속 마감. 0080Y0(2배)를 짧게 쓸지 다시 보는 시점 안내. 매매 지시 아님.",
             f"지금 결과/상태: {row['day']} 종가 {row['close']:,.0f}원, 20일선 {row['ma']:,.0f}원 ({(row['close'] / row['ma'] - 1) * 100:+.1f}%). 3일 종가/20일선: {three}. {levtxt}.",
             voltxt, fxtxt,
             "지금 할 일: 2배는 짧은 기간, 작은 비중으로만 검토. 들어간다면 466920 20일선 재이탈을 나가는 기준으로 미리 정하기.",
             "나중에: 종가가 20일선 아래로 2거래일 연속 마감하면 다시 무장하고, 그 뒤 3일 회복에 다시 알림.",
             "익절/줄일 때: 466920 이 20일선 아래로 다시 마감하면 2배 비중부터 줄이기.",
             "하지 말 것: 2배 장기 보유, 신용/미수, 하루 반등만 보고 추격."]
    msg = finish(lines, cfg)
    check_text(TITLE_MA, msg)
    return TITLE_MA, msg, cfg["ma"]["priority"]


def msg_nbi(cfg: dict, nb: dict, ym: str, stamp: str) -> tuple[str, str, int]:
    months = nb["months"]
    seq = [month_minus(ym, k) for k in (3, 2, 1, 0)]
    path = " -> ".join(f"{m[2:].replace('-', '/')} {months[m]['value']:.2f}" for m in seq)
    src = ", ".join(months[ym]["sources"][:2])
    lines = [f"{stamp} | SHIPLEV | 클락슨 신조선가지수 3개월 연속 하락",
             "이게 뭐 알림: 클락슨 신조선가지수가 전월보다 낮은 달이 3개월 연속. 조선 업황(선가) 둔화 확인 안내. 매매 지시 아님.",
             f"지금 결과/상태: {path}. 3개월 변화 {months[ym]['value'] - months[seq[0]]['value']:+.2f}p. 출처: {src}",
             "지금 할 일: 466920, 0080Y0, 조선 개별주 합산 비중을 점검하고 2배 비중은 늘리지 않기.",
             "나중에: 다음 달 값이 또 낮으면 그 달에 다시 1번. 전월보다 오르면 연속 카운트가 끊김.",
             "익절/줄일 때: 선가 하락과 466920 20일선 아래가 겹치면 2배부터 줄이기.",
             "하지 말 것: 한 달 지수만으로 전량 정리, 기사 인용 값을 확정 원자료로 읽기."]
    msg = finish(lines, cfg)
    check_text(TITLE_NBI, msg)
    return TITLE_NBI, msg, cfg["nbi"]["priority"]


def msg_ops(kind: str, detail: str, stamp: str, cfg: dict) -> tuple[str, str, int]:
    titles = {"price": "[SHIPLEV] 466920 시세 조회 실패 | 20일선 판정 보류",
              "search": "[SHIPLEV] 466920 신조선가 수집 실패 | 기사 직접 확인",
              "missing": "[SHIPLEV] 466920 신조선가 직전 달 값 미게시 | 기사 직접 확인"}
    t = titles[kind]
    lines = [f"{stamp} | SHIPLEV | 운영 안내", "이게 뭐 알림: [SHIPLEV] 데이터 읽기 안내. 매매 지시 아님.",
             f"지금 결과/상태: {detail}", "지금 할 일: 필요하면 원문을 직접 확인하세요.",
             "나중에: 다음 평일 15:20 KST 에 다시 읽고 밀린 날짜와 달도 순서대로 판정합니다.",
             "하지 말 것: 읽기 실패를 조건 미충족으로 읽기."]
    msg = finish(lines, cfg)
    check_text(t, msg)
    assert route_topic("cwally-port", t) == "cwally-ops", t
    return t, msg, 3


# ---------------------------------------------------------------- run
def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def run(mode: str, now: datetime, cfg: dict, st: dict | None = None, pub=publish, closes_fn=None,
        fx_fn=None, search_fn=None, write: bool = True) -> list[tuple[str, str, int]]:
    dry = mode == "dry"
    stamp, today = now.strftime("%Y-%m-%d %H:%M KST"), now.date().isoformat()
    st = load_state() if st is None else st
    px, nb, ops = st.setdefault("px", {}), st.setdefault("nbi", {}), st.setdefault("ops", {})
    out: list[tuple[str, str, int]] = []
    closes_fn, fx_fn, search_fn = closes_fn or closes, fx_fn or fx_latest, search_fn or search_items

    # condition 1
    try:
        rows, src = closes_fn(cfg["etf"]["code"])
        rows = [r for r in rows if r[0] < today]  # 15:20 = before close; never use today's row
        if len(rows) < cfg["ma"]["days"] + 1:
            raise RuntimeError(f"확정 종가 {len(rows)}건 (20일선 계산 부족)")
        series = ma_series(rows, cfg["ma"]["days"])
        first = "last_date" not in px
        fired = judge_ma(cfg, px, series)
        px["source"] = src
        if first:
            print(f"seed: replayed {len(series)} days silently, fired_in_replay={[r['day'] for r in fired]}")
            fired = []
        if fired:
            try:
                lrows, _ = closes_fn(cfg["lev"]["code"])
                lrows = [r for r in lrows if r[0] < today]
                lev = lrows[-1] if lrows else None
            except Exception:  # noqa: BLE001
                lev = None
            vol = vol_ann(rows, cfg["vol"]["days"])
            fx = fx_fn(today)
            for row in fired:
                idx = [s["day"] for s in series].index(row["day"])
                out.append(msg_ma(cfg, row, series[idx - 2:idx + 1], lev, vol, fx, stamp))
        print(f"px: last={px.get('last_date')} close={px.get('last_close')} ma20={px.get('last_ma20')} "
              f"above={px.get('above_streak')} below={px.get('below_streak')} armed={px.get('armed')} src={src}")
    except Exception as exc:  # noqa: BLE001
        print(f"price fail: {exc}")
        if ops.get("price") != today:
            ops["price"] = today
            out.append(msg_ops("price", f"Daum, Naver 시세 {type(exc).__name__} {str(exc)[:120]}. 오늘은 20일선을 판정하지 않았습니다.", stamp, cfg))

    # condition 2
    want = prev_month(now.date())
    if want not in nb.get("months", {}):
        try:
            found = collect_nbi(search_fn(cfg["nbi"]["query"]), now.date())
            for ym in judge_nbi(cfg, nb, found, now.date(), stamp):
                out.append(msg_nbi(cfg, nb, ym, stamp))
            print(f"nbi: found={ {k: v['value'] for k, v in sorted(found.items())} }")
        except Exception as exc:  # noqa: BLE001
            print(f"nbi search fail: {exc}")
            if ops.get("search") != today:
                ops["search"] = today
                out.append(msg_ops("search", f"네이버 뉴스 검색 {type(exc).__name__} {str(exc)[:120]}. 새 달 값으로 보지 않았습니다.", stamp, cfg))
        if want not in nb.get("months", {}) and now.day >= cfg["nbi"]["missing_after_day"] and ops.get("missing") != today:
            ops["missing"] = today
            out.append(msg_ops("missing", f"{want} 클락슨 신조선가지수 값을 기사에서 찾지 못했습니다(15일 이후). 판정 보류.", stamp, cfg))
    else:
        print(f"nbi: {want} already stored ({nb['months'][want]['value']}); no search")

    topic, server = resolve_topic("port", cfg.get("ntfy_topic")), resolve_server(cfg.get("ntfy_server"))
    for t, m, p in out:
        print(f"TITLE: {t}\nPRIORITY: {p} TOPIC: {route_topic(topic, t)}\n{m}\n")
    if not out:
        print("보낼 것 없음")
    if dry:
        print("DRY RUN: no publish, state not written")
        return out
    for t, m, p in out:
        pub(topic, t, m, p, ["ship"], server, config_name="shiplev")
    if write:
        STATE_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return out


# ---------------------------------------------------------------- selftest
def selftest() -> int:
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    ok = fail = 0

    def check(label, got, want):
        nonlocal ok, fail
        if got == want:
            ok += 1
        else:
            fail += 1
            print(f"FAIL {label}: got {got!r} want {want!r}")

    def days(n, start=date(2026, 9, 1)):
        out, d = [], start
        while len(out) < n:
            if d.weekday() < 5:
                out.append(d.isoformat())
            d += timedelta(days=1)
        return out

    def mk(vals):
        return list(zip(days(len(vals)), vals))

    # ma_series / boundaries
    base = [100.0] * 20
    s = ma_series(mk(base + [100.0]))
    check("ma equal value", s[-1]["ma"], 100.0)
    px = {"last_date": s[-2]["day"], "armed": True, "above_streak": 2, "below_streak": 0}
    check("equal not above: no fire", judge_ma(cfg, px, s), [])
    check("equal resets above", px["above_streak"], 0)
    check("equal resets below", px["below_streak"], 0)

    # fire on the 3rd day above
    vals = [100.0] * 20 + [101.0, 102.0, 103.0, 104.0]
    s = ma_series(mk(vals))
    px = {"last_date": s[0]["day"], "armed": True, "above_streak": 0, "below_streak": 0}
    f = judge_ma(cfg, px, s[:3])
    check("day1 above no fire", f, [])
    check("streak 2", px["above_streak"], 2)
    f = judge_ma(cfg, px, s[:4])
    check("day3 fires", [r["day"] for r in f], [s[3]["day"]])
    check("disarmed", px["armed"], False)
    f = judge_ma(cfg, px, s)
    check("day4 no repeat", f, [])
    check("same day not rejudged", judge_ma(cfg, px, s), [])

    # rearm after 2 days below, not 1
    vals2 = vals + [90.0, 103.0, 90.0, 89.0]
    s2 = ma_series(mk(vals2))
    judge_ma(cfg, px, s2[:-3])
    check("1 below no rearm", px["armed"], False)
    judge_ma(cfg, px, s2[:-2])
    check("above breaks below streak", (px["armed"], px["below_streak"]), (False, 0))
    judge_ma(cfg, px, s2)
    check("2 below rearm", px["armed"], True)
    check("below streak 2", px["below_streak"], 2)
    vals3 = vals2 + [120.0, 121.0, 122.0]
    s3 = ma_series(mk(vals3))
    f = judge_ma(cfg, px, s3)
    check("fires again after rearm", len(f), 1)

    # just above ma counts (strict >)
    vals4 = [100.0] * 19 + [100.0, 100.01, 100.02, 100.03]
    s4 = ma_series(mk(vals4))
    px4 = {"last_date": s4[0]["day"], "armed": True, "above_streak": 0, "below_streak": 0}
    check("tiny above counts", len(judge_ma(cfg, px4, s4)), 1)

    # vol / decay
    check("vol flat", vol_ann(mk([100.0] * 25)), 0.0)
    check("vol short", vol_ann(mk([100.0] * 10)), None)

    # run(): today's row excluded, seed silent, fire message, dry no state
    now = datetime(2026, 11, 10, 15, 20, tzinfo=KST)
    hist = [("2026-10-%02d" % d, 100.0) for d in range(1, 29)]
    up = hist + [("2026-11-03", 110.0), ("2026-11-04", 111.0), ("2026-11-05", 112.0), ("2026-11-10", 50.0)]
    sent = []
    fake_pub = lambda *a, **k: sent.append(a[1])  # noqa: E731
    nbfull = {"months": {"2026-10": {"value": 186.0, "sources": ["x"]}}}
    st = {"px": {}, "nbi": copy.deepcopy(nbfull)}
    o = run("auto", now, cfg, st, fake_pub, lambda c: (up, "t"), lambda d: None, lambda q: [], write=False)
    check("seed silent", o, [])
    check("seed recorded last_date excludes today", st["px"]["last_date"], "2026-11-05")
    check("seed replay disarmed", st["px"]["armed"], False)
    st = {"px": {"last_date": "2026-10-28", "armed": True, "above_streak": 0, "below_streak": 0}, "nbi": copy.deepcopy(nbfull)}
    o = run("auto", now, cfg, st, fake_pub, lambda c: (up, "t"), lambda d: ("2026-11-07", 1346.5), lambda q: [], write=False)
    check("run fires once", [t for t, _, _ in o], [TITLE_MA])
    body = o[0][1] if o else ""
    check("body has 0080Y0", "0080Y0" in body, True)
    check("body has decay", "감쇠" in body and "sigma^2/2" in body, True)
    check("body fx line", "1,346.5" in body, True)
    check("footer last", body.rstrip().split("\n")[-1], cfg["footer"])
    check("ends line present", ENDS_PREFIX in body, True)
    check("today row unused", "50" not in body.split("3일 종가/20일선:")[1].split(".")[0], True)
    check("published", sent[-1:], [TITLE_MA])
    n_sent = len(sent)
    st2 = {"px": {"last_date": "2026-10-28", "armed": True, "above_streak": 0, "below_streak": 0}, "nbi": copy.deepcopy(nbfull)}
    o = run("dry", now, cfg, st2, fake_pub, lambda c: (up, "t"), lambda d: None, lambda q: [], write=False)
    check("dry builds msg", len(o), 1)
    check("dry no publish", len(sent), n_sent)

    # price failure -> ops once per day
    def boom(c):
        raise RuntimeError("x")
    st = {"px": {}, "nbi": copy.deepcopy(nbfull)}
    o = run("dry", now, cfg, st, fake_pub, boom, lambda d: None, lambda q: [], write=False)
    check("price fail ops", [route_topic("cwally-port", t) for t, _, _ in o], ["cwally-ops"])
    o = run("dry", now, cfg, st, fake_pub, boom, lambda d: None, lambda q: [], write=False)
    check("price fail once a day", o, [])

    # nbi parsing
    td = date(2026, 11, 3)
    check("parse yna", parse_nbi("9월말 클락슨 신조선가지수는 186.59로 전월보다 0.25포인트 상승", td), [("2026-09", 186.59)])
    check("parse with prev", parse_nbi("9월 말 클락슨 신조선가지수(Newbuilding Price Index)는 186.59로 전월 186.34보다 0.25포인트", td),
          [("2026-09", 186.59), ("2026-08", 186.34)])
    check("parse PB", parse_nbi("클락슨 신조선가지수는 2026년 8월 말 186.34포인트를 기록", td), [("2026-08", 186.34)])
    check("parse no 말 ignored", parse_nbi("2021년 9월 151.40과 비교하면", td), [])
    check("parse rounded ignored", parse_nbi("클락슨 신조선가 지수도 9월 186.6포인트로", td), [])
    check("parse jan -> prev year", parse_nbi("12월 말 클락슨 신조선가지수는 185.00", date(2027, 1, 6)), [("2026-12", 185.0)])
    check("parse garbage", parse_nbi("신조선가지수 발표 지연", td), [])
    items = [("u1", "9월 말 클락슨 신조선가지수는 186.59로"), ("u2", "9월 말 클락슨 신조선가지수는 186.59로"),
             ("u3", "9월 말 클락슨 신조선가지수는 186.95로")]
    check("majority", collect_nbi(items, td)["2026-09"]["value"], 186.59)
    check("tie dropped", collect_nbi(items[1:], td), {})

    # nbi judging: 3 consecutive declines, month dedupe
    def mon(vals):
        return {k: {"value": v, "sources": ["s"]} for k, v in vals.items()}
    nb = {"months": mon({"2026-09": 186.59, "2026-10": 186.0, "2026-11": 185.5})}
    check("2 declines no fire", judge_nbi(cfg, nb, mon({"2026-11": 185.5}), td, "t"), [])
    f = judge_nbi(cfg, nb, mon({"2026-12": 185.0}), date(2027, 1, 6), "t")
    check("3rd decline fires", f, ["2026-12"])
    check("month dedupe", judge_nbi(cfg, nb, mon({"2026-12": 185.0}), date(2027, 1, 7), "t"), [])
    nb["sent"] = []
    nb["months"].pop("2026-12")
    check("dedupe by sent key", judge_nbi(cfg, {"months": dict(nb["months"]), "sent": ["2026-12"]}, mon({"2026-12": 185.0}), date(2027, 1, 7), "t"), [])
    nb2 = {"months": mon({"2026-08": 186.34, "2026-09": 186.59, "2026-10": 186.0})}
    check("equal month breaks", judge_nbi(cfg, {"months": mon({"2026-09": 186.0, "2026-10": 185.0, "2026-11": 185.0})}, mon({"2026-12": 184.0}), date(2027, 1, 6), "t"), [])
    check("rise no fire", judge_nbi(cfg, nb2, mon({"2026-11": 186.5}), td, "t"), [])
    check("gap month no fire", judge_nbi(cfg, {"months": mon({"2026-07": 187, "2026-08": 186.5, "2026-09": 186.0})}, mon({"2026-11": 185.0}), td, "t"), [])
    check("jump >10% rejected", judge_nbi(cfg, {"months": mon({"2026-09": 186.59})}, mon({"2026-10": 120.0}), td, "t"), [])
    m = msg_nbi(cfg, {"months": mon({"2026-09": 186.59, "2026-10": 186.0, "2026-11": 185.5, "2026-12": 185.0})}, "2026-12", "t")
    check("nbi title", m[0], TITLE_NBI)
    check("nbi footer last", m[1].split("\n")[-1], cfg["footer"])

    # run(): parse failure is not a new month; ops after 15th once a day; search error ops
    st = {"px": {"last_date": "2026-11-09", "armed": True, "above_streak": 0, "below_streak": 0},
          "nbi": {"months": mon({"2026-09": 186.59})}}
    early = datetime(2026, 11, 3, 15, 20, tzinfo=KST)
    o = run("dry", early, cfg, st, fake_pub, lambda c: (up, "t"), lambda d: None, lambda q: [("u", "신조선가지수 미상")], write=False)
    check("parse fail early: silent", o, [])
    check("parse fail: no new month", sorted(st["nbi"]["months"]), ["2026-09"])
    late = datetime(2026, 11, 16, 15, 20, tzinfo=KST)
    o = run("dry", late, cfg, st, fake_pub, lambda c: (up, "t"), lambda d: None, lambda q: [], write=False)
    check("missing after 15th ops", [route_topic("cwally-port", t) for t, _, _ in o], ["cwally-ops"])
    o = run("dry", late, cfg, st, fake_pub, lambda c: (up, "t"), lambda d: None, lambda q: [], write=False)
    check("missing once a day", o, [])

    def sboom(q):
        raise RuntimeError("blocked")
    st = {"px": {"last_date": "2026-11-09", "armed": True}, "nbi": {"months": mon({"2026-09": 186.59})}}
    o = run("dry", early, cfg, st, fake_pub, lambda c: (up, "t"), lambda d: None, sboom, write=False)
    check("search fail ops", [t for t, _, _ in o], ["[SHIPLEV] 466920 신조선가 수집 실패 | 기사 직접 확인"])
    st = {"px": {"last_date": "2026-11-09", "armed": True}, "nbi": {"months": mon({"2026-09": 186.59, "2026-10": 186.0})}}
    o = run("dry", early, cfg, st, fake_pub, lambda c: (up, "t"), lambda d: None, sboom, write=False)
    check("no search when month stored", o, [])
    st = {"px": {"last_date": "2026-11-09", "armed": True},
          "nbi": {"months": mon({"2026-07": 186.5, "2026-08": 186.3, "2026-09": 186.1})}}
    o = run("dry", early, cfg, st, fake_pub, lambda c: (up, "t"), lambda d: None,
            lambda q: [("https://yna/x", "10월 말 클락슨 신조선가지수는 185.90으로")], write=False)
    check("run nbi fire", [t for t, _, _ in o], [TITLE_NBI])
    check("source stored", st["nbi"]["months"]["2026-10"]["sources"], ["https://yna/x"])

    # text rules on every title
    for t in (TITLE_MA, TITLE_NBI) + tuple(msg_ops(k, "d", "t", cfg)[0] for k in ("price", "search", "missing")):
        check(f"title rules {t}", all(b not in t for b in BANNED) and "466920" in t, True)
    check("config no ends", cfg.get("ends_kst"), None)
    print(f"selftest: {ok} passed, {fail} failed")
    return 0 if fail == 0 else 1


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    if mode == "selftest":
        return selftest()
    if mode not in {"auto", "dry"}:
        print("usage: shiplev_monitor.py <auto|dry|selftest>")
        return 2
    run(mode, datetime.now(KST), json.loads(CFG_PATH.read_text(encoding="utf-8")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
