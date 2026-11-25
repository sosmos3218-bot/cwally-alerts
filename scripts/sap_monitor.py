#!/usr/bin/env python3
"""[SAP] SAP SE ADR (NYSE: SAP) 가격 구간 + 공식 분기 실적 점검. 매매 지시 아님.

평일 09:00 KST (port-alerts morning, 새 cron 없음).
완료 봉: 뉴욕 16:10 이후면 그날 봉, 그 전이면 직전 평일. 토, 일 봉은 넣지 않는다.
구간 진입만 1번. 경계 밖으로 1.5% 넘게 나가야 재무장. 데이터 날짜로 중복 방지.
  2차 175~195 (195 포함), 1차 195 초과~207, 추격 금지 235 이상.
  207 초과 235 미만, 175 미만은 알림 구간이 아님.
SEC 6-K (CIK 0001000184) 새 분기 실적마다 1번. 첫 auto 는 기존 접수번호를 시드만 하고 안 보냄.
Q4/FY2026 일정은 SAP IR events 의 2027-01-28 06:00 CET (2026-11-25 확인, 공식).
일정이 없어서 추정으로 돌릴 때는 2027-01-25~01-31 를 이 주석에만 적고, 알림 문장에는 추정이라고 쓰지 않는다.
이번 실행은 공식 일정이 있으므로 그 창은 쓰지 않는다.
조회 실패는 cwally-ops 하루 1번. ends_kst 이후 침묵.
usage: sap_monitor.py <auto|dry> | selftest
"""
from __future__ import annotations

import json
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from html import unescape
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
NY = ZoneInfo("America/New_York")
NAME = "sap"
TAG = "[SAP]"
CFG_PATH = ROOT / "configs" / f"{NAME}.json"
STATE_PATH = ROOT / "state" / f"{NAME}.json"
YAHOO_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
BANNED_RE = re.compile("[\u00b7\u318d\u2014\u2013]")
QWORD = {"first": 1, "second": 2, "third": 3, "fourth": 4}
RESULTS_RE = re.compile(r"announced today its financial results|Quarterly Statement Q[1-4]\s+20\d\d", re.I)
QUARTER_ENDED_RE = re.compile(
    r"financial results for the (first|second|third|fourth) quarter ended [A-Za-z]+ \d{1,2}, (\d{4})",
    re.I,
)
QUARTER_LABEL_RE = re.compile(r"Quarterly Statement Q([1-4])\s+(20\d\d)", re.I)
CCB_RE = re.compile(
    r"current cloud backlog of EUR\s*[\d.]+\s*billion, up\s*[\d.]+%\s*and up\s*([\d.]+)%\s*at constant currencies",
    re.I,
)
ERP_RE = re.compile(
    r"Cloud ERP Suite revenue up\s+([\d.]+)%\s+and up\s+([\d.]+)%\s+at constant currencies",
    re.I,
)
GM_RE = re.compile(
    r"Cloud gross margin \(in %\)\s+([\d.]+)\s+([\d.]+)\s+-?[\d.]+pp\s+([\d.]+)\s+([\d.]+)",
    re.I,
)
SUPPORT_RE = re.compile(
    r"Software support revenue\s+[\d,]+\s+[\d,]+\s+(-?\d+)\s+[\d,]+\s+[\d,]+\s+(-?\d+)\s+(-?\d+)",
    re.I,
)
FCF_RE = re.compile(r"approximately EUR\s*([\d.]+)\s*billion free cash flow", re.I)
FCF_FY_RE = re.compile(
    r"(?:full[- ]year|fiscal year).{0,80}free cash flow.{0,40}EUR\s*([\d.]+)\s*billion",
    re.I,
)
KEPT_RE = re.compile(r"to accelerate in 20\d\d", re.I)
WITHDRAW_RE = re.compile(
    r"no longer expect.{0,140}accelerat"
    r"|will not accelerate in 20\d\d"
    r"|do not expect.{0,80}accelerat.{0,40}20\d\d"
    r"|withdraw(?:n|s|ing)? (?:its |the )?(?:20\d\d |full[- ]year )?guidance"
    r"|lowered .{0,60}20\d\d",
    re.I,
)


def load_cfg() -> dict:
    return json.loads(CFG_PATH.read_text(encoding="utf-8"))


def load_state(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("armed", {})
    data.setdefault("seen_acc", [])
    data.setdefault("ccb", [])
    data.setdefault("results_sent", {})
    return data


def save_state(path: Path, st: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def check_text(title: str, body: str) -> None:
    bad = BANNED_RE.search((title or "") + "\n" + (body or ""))
    if bad:
        raise AssertionError(f"forbidden char {bad.group(0)!r} in {title}")


def usd(v) -> str:
    return f"${v:,.2f}" if v is not None else "?"


def get_bytes(url: str, ua: str, limit: int | None = None) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept-Encoding": "identity"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = resp.read(limit + 1) if limit else resp.read()
    if limit and len(data) > limit:
        data = data[:limit]
    return data


def norm(text: str) -> str:
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", text or "", flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = unescape(text).replace("\xa0", " ")
    text = text.replace("€", "EUR").replace("\u20ac", "EUR")
    text = re.sub(r"[\u2010-\u2015\u2212]", "-", text)
    return re.sub(r"\s+", " ", text).strip()


def inside(p: float, zone: dict) -> bool:
    lo = float(zone["lo"])
    hi = zone.get("hi")
    lo_ok = p >= lo if zone.get("lo_inclusive", True) else p > lo
    if hi is None:
        return lo_ok
    hi = float(hi)
    hi_ok = p <= hi if zone.get("hi_inclusive", True) else p < hi
    return lo_ok and hi_ok


def outside_buffer(p: float, zone: dict, h: float) -> bool:
    """True when price has left the zone by more than h (1.5% = 0.015)."""
    lo = float(zone["lo"])
    hi = zone.get("hi")
    if hi is None:
        return p < lo * (1 - h)
    return p > float(hi) * (1 + h) or p < lo * (1 - h)


def judge(zones: list[dict], armed: dict, p: float, h: float) -> list[dict]:
    fired = []
    for zone in zones:
        key = zone["key"]
        hit = inside(p, zone)
        if key not in armed:
            armed[key] = not hit
            continue
        if armed[key] and hit:
            armed[key] = False
            fired.append(zone)
        elif (not armed[key]) and outside_buffer(p, zone, h):
            armed[key] = True
    return fired


def current_zone(zones: list[dict], p: float) -> str:
    hits = [z for z in zones if inside(p, z)]
    if not hits:
        return "none"
    return max(hits, key=lambda z: int(z["priority"]))["key"]


def bars_from_chart(raw: dict, now: datetime, close_hm: str = "16:10") -> list[tuple[str, float]]:
    res = raw["chart"]["result"][0]
    out: dict = {}
    quote = (res.get("indicators") or {}).get("quote") or [{}]
    for t, c in zip(res.get("timestamp") or [], quote[0].get("close") or []):
        if c is None:
            continue
        dt = datetime.fromtimestamp(int(t), NY)
        if dt.weekday() >= 5:
            continue
        out[dt.date()] = float(c)
    meta = res.get("meta") or {}
    rmt, rmp = meta.get("regularMarketTime"), meta.get("regularMarketPrice")
    if rmt and rmp is not None:
        mt = datetime.fromtimestamp(int(rmt), NY)
        if mt.weekday() < 5 and mt.date() not in out and (mt.hour, mt.minute) >= (16, 0):
            out[mt.date()] = float(rmp)
    hh, mm = (int(x) for x in str(close_hm or "16:10").split(":"))
    now_ny = now.astimezone(NY)
    today = now_ny.date()
    after = (now_ny.hour, now_ny.minute) >= (hh, mm)
    rows = []
    for day, close in sorted(out.items()):
        if day.weekday() >= 5:
            continue
        if day < today or (day == today and after):
            rows.append((day.isoformat(), close))
    return rows


def yahoo_rows(cfg: dict, now: datetime) -> list[tuple[str, float]]:
    raw = json.loads(get_bytes(cfg["sources"]["price"], YAHOO_UA).decode("utf-8", "replace"))
    return bars_from_chart(raw, now, cfg.get("close_hm") or "16:10")


def buffer_line(cfg: dict) -> str:
    h = float(cfg["hysteresis_pct"]) / 100.0
    bits = []
    for zone in cfg["zones"]:
        lo = float(zone["lo"])
        hi = zone.get("hi")
        if hi is None:
            bits.append(f"{zone['label']} ${lo * (1 - h):,.2f} 미만")
        else:
            hi = float(hi)
            bits.append(f"{zone['label']} ${lo * (1 - h):,.2f} 미만 또는 ${hi * (1 + h):,.2f} 초과")
    return ", ".join(bits)


def price_message(cfg: dict, zone: dict, fire_day: str, fire_px: float, latest_day: str, latest_px: float, stamp: str) -> tuple[str, str, int]:
    title = f"{TAG} SAP 종가 {usd(fire_px)} | {zone['title']}"
    extra = ""
    if latest_day != fire_day:
        extra = f" 그 뒤 최근 완료 종가는 {latest_day} {usd(latest_px)} ({current_zone(cfg['zones'], latest_px)})."
    q4 = cfg["q4_results"]
    body = "\n".join([
        "이게 뭐 알림: SAP SE ADR(NYSE: SAP) 미국 정규장 종가가 미리 정한 구간에 들어왔다는 알림. 점검 시점. 매매 지시 아님.",
        f"지금 결과/상태: {fire_day} 종가 {usd(fire_px)} (Yahoo, 뉴욕 정규장 완료 봉). 구간 {zone['label']}. "
        f"1차는 $195 초과~$207, 2차는 $175~$195 ($195 는 2차), 추격 금지는 $235 이상. {stamp}.{extra}",
        f"지금 할 일: {zone['todo']} 미보유.",
        "나중에: 같은 구간은 경계 밖으로 1.5% 넘게 나간 뒤에만 다시 무장합니다 "
        f"({buffer_line(cfg)}). 분기 실적은 SEC 6-K 로 따로 봅니다. "
        f"Q4/FY2026 공식 일정은 {q4['date']} {q4['time_note']}",
        "익절/줄일 때: 미보유, 해당 없음.",
        f"하지 말 것: {zone['dont']}",
    ])
    check_text(title, body)
    return title, body, int(zone["priority"])


def apply_price(cfg: dict, st: dict, rows: list[tuple[str, float]], stamp: str) -> tuple[str, str, int] | None:
    if not rows:
        raise ValueError("확정 종가 없음")
    day, px = rows[-1]
    prev = st.get("last_date")
    if prev == day:
        print(f"dedup {day} {usd(px)} zone={current_zone(cfg['zones'], px)}")
        return None
    armed = st.setdefault("armed", {})
    h = float(cfg["hysteresis_pct"]) / 100.0
    last_fire = None
    for d, c in rows:
        if prev is not None and d <= prev:
            continue
        got = judge(cfg["zones"], armed, c, h)
        if prev is not None and got:
            last_fire = (d, c, got)
    st["last_date"] = day
    st["last_close"] = [day, round(px, 4)]
    st["price_seeded"] = True
    print(f"close {day} {usd(px)} zone={current_zone(cfg['zones'], px)} armed={{{', '.join(f'{k}:{armed.get(k)}' for k in (z['key'] for z in cfg['zones']))}}}")
    if prev is None:
        print("seed silent")
        return None
    if not last_fire:
        return None
    fire_day, fire_px, got = last_fire
    zone = max(got, key=lambda z: int(z["priority"]))
    if fire_day != day and not inside(px, zone):
        return None
    return price_message(cfg, zone, fire_day, fire_px, day, px, stamp)


def quarter_of(text: str) -> str | None:
    m = QUARTER_ENDED_RE.search(text)
    if m:
        return f"{m.group(2)}Q{QWORD[m.group(1).lower()]}"
    m = QUARTER_LABEL_RE.search(text)
    if m:
        return f"{m.group(2)}Q{m.group(1)}"
    return None


def is_results(text: str) -> bool:
    return bool(RESULTS_RE.search(text or ""))


def _num(pat: re.Pattern, text: str, group: int = 1) -> float | None:
    m = pat.search(text)
    if not m:
        return None
    try:
        return float(m.group(group))
    except ValueError:
        return None


def parse_results(raw: str) -> dict:
    text = norm(raw)
    if WITHDRAW_RE.search(text):
        accel = "withdrawn"
    elif KEPT_RE.search(text):
        accel = "kept"
    else:
        accel = None
    fcf = _num(FCF_RE, text)
    fcf_fy = _num(FCF_FY_RE, text)
    return {
        "quarter": quarter_of(text),
        "ccb_cc": _num(CCB_RE, text),
        "cloud_erp_cc": _num(ERP_RE, text, 2),
        "cloud_gm": _num(GM_RE, text, 3),
        "support_cc": _num(SUPPORT_RE, text, 3),
        "fcf_bn": fcf if fcf is not None else fcf_fy,
        "accel_2027": accel,
        "results": is_results(text),
    }


def qkey(q: str) -> int:
    return int(q[:4]) * 4 + int(q[-1])


def update_ccb(series: list[dict], quarter: str | None, cc: float | None, below: float, need: int) -> tuple[list[dict], bool, int]:
    if quarter is None or cc is None:
        streak = _ccb_streak(series, below)
        return series, streak >= need and len(series) >= need, streak
    series = [x for x in series if x.get("q") != quarter]
    series.append({"q": quarter, "cc": cc})
    series.sort(key=lambda x: x["q"])
    streak = _ccb_streak(series, below)
    return series, streak >= need, streak


def _ccb_streak(series: list[dict], below: float) -> int:
    streak = 0
    prev = None
    for row in series:
        cc = row.get("cc")
        q = row.get("q")
        if cc is None or not q:
            streak = 0
            prev = None
            continue
        if prev is not None and qkey(q) != qkey(prev) + 1:
            streak = 0
        if cc < below:
            streak += 1
        else:
            streak = 0
        prev = q
    return streak


def _fmt(v, suffix="") -> str:
    if v is None:
        return "자동 판정 불가"
    if isinstance(v, float) and v.is_integer():
        return f"{int(v)}{suffix}"
    return f"{v}{suffix}"


def filing_breaks(cfg: dict, parsed: dict, ccb_break: bool) -> list[str]:
    t = cfg["thesis"]
    out = []
    if ccb_break:
        out.append("CCB 성장 2분기 연속 20% 미만")
    erp = parsed.get("cloud_erp_cc")
    if erp is not None and erp <= float(t["cloud_erp_cc_max"]):
        out.append("클라우드 ERP 성장 저조")
    if parsed.get("accel_2027") == "withdrawn":
        out.append("2027 가속 가이던스 철회")
    gm = parsed.get("cloud_gm")
    if gm is not None and gm < float(t["cloud_gm_below"]):
        out.append("클라우드 매출총이익률 72% 미만")
    fcf = parsed.get("fcf_bn")
    if fcf is not None and fcf < float(t["fy_fcf_below_bn"]):
        out.append("연간 FCF EUR 10B 미만")
    sup = parsed.get("support_cc")
    if sup is not None and sup <= float(t["support_cc_at_or_below"]):
        out.append("서포트 매출 감소 6% 이상")
    return out


def filing_message(cfg: dict, row: dict, parsed: dict, breaks: list[str], streak: int, link: str, stamp: str) -> tuple[str, str, int]:
    q = parsed.get("quarter") or "분기 자동 판정 불가"
    if breaks:
        title = f"{TAG} SAP 논리 이탈 | " + ", ".join(breaks[:3])
        pri = 4
    else:
        title = f"{TAG} SAP 분기 실적 게시 | 조건 확인"
        pri = 3
    accel = parsed.get("accel_2027")
    accel_txt = {"kept": "가속 문구 유지", "withdrawn": "철회 또는 하향 문구"}.get(accel, "자동 판정 불가")
    ccb = parsed.get("ccb_cc")
    ccb_txt = "자동 판정 불가" if ccb is None else f"{_fmt(ccb, '%')}, 연속 {streak}분기 (20% 미만 기준)"
    lines = [
        "이게 뭐 알림: SAP 공식 분기 실적(SEC 6-K)이 새로 게시됐다는 점검 알림. 매매 지시 아님.",
        f"지금 결과/상태: {row.get('date')} {row.get('form')} {q}. 접수번호 {row.get('acc')}. {stamp}. 링크: {link}",
        "지금 할 일: 원문에서 6개 조건을 확인하세요. "
        f"(1) CCB 고정환율 성장 {ccb_txt}. "
        f"(2) 클라우드 ERP 스위트 고정환율 {_fmt(parsed.get('cloud_erp_cc'), '%')} (23% 이하면 낮은 20%대). "
        f"(3) 2027 매출 성장 가속 {accel_txt}. "
        f"(4) 비IFRS 클라우드 매출총이익률 {_fmt(parsed.get('cloud_gm'), '%')} (72% 미만이면 이탈). "
        f"(5) 연간 FCF {_fmt(parsed.get('fcf_bn'), 'B EUR')} (10 미만이면 이탈, 약 10은 이탈 아님). "
        f"(6) 서포트 매출 고정환율 {_fmt(parsed.get('support_cc'), '%')} (-6% 이하면 이탈).",
        "나중에: 같은 접수번호는 다시 보내지 않습니다. CCB 는 연속 2분기 20% 미만일 때만 논리 이탈입니다. "
        "못 읽은 항목은 자동 판정 불가이니 IR 원문을 직접 보세요. "
        f"IR: {cfg['sec']['ir_results']}",
        "익절/줄일 때: 미보유, 해당 없음. 논리 이탈이어도 자동 매도 지시가 아닙니다.",
        "하지 말 것: 헤드라인 한 줄로 매수, 숫자를 못 읽은 항목을 통과로 보기, 추격.",
    ]
    body = "\n".join(lines)
    check_text(title, body)
    return title, body, pri


def exhibit_names(html: str, primary: str) -> list[str]:
    found = []
    for href in re.findall(r'href="([^"]+\.htm)"', html or "", flags=re.I):
        name = href.split("?")[0].split("/")[-1]
        if not name or name == primary or name in found:
            continue
        found.append(name)
    return found[:4]


def filing_text(cfg: dict, row: dict) -> tuple[str, str]:
    cik = int(cfg["sec"]["cik"])
    accn = row["acc"].replace("-", "")
    base = f"https://www.sec.gov/Archives/edgar/data/{cik}/{accn}"
    limit = int(cfg["sec"].get("doc_limit_bytes") or 400000)
    ua = cfg["sec"]["user_agent"]
    primary = get_bytes(f"{base}/{row['doc']}", ua, limit).decode("utf-8", "replace")
    parts = [primary]
    link = f"{base}/{row['acc']}-index.html"
    for name in exhibit_names(primary, row.get("doc") or ""):
        parts.append(get_bytes(f"{base}/{name}", ua, limit).decode("utf-8", "replace"))
        if "ex99" in name.lower():
            link = f"{base}/{name}"
    return "\n".join(parts), link


def sec_rows(cfg: dict) -> list[dict]:
    ua = cfg["sec"]["user_agent"]
    data = json.loads(get_bytes(cfg["sec"]["submissions"], ua).decode("utf-8", "replace"))
    recent = (data.get("filings") or {}).get("recent") or {}
    forms = recent.get("form") or []
    dates = recent.get("filingDate") or []
    accs = recent.get("accessionNumber") or []
    docs = recent.get("primaryDocument") or []
    want = set(cfg["sec"].get("forms") or ["6-K", "6-K/A"])
    out = []
    n = min(len(forms), len(dates), len(accs), len(docs))
    for i in range(n):
        if forms[i] not in want:
            continue
        out.append({"form": forms[i], "date": dates[i], "acc": accs[i], "doc": docs[i]})
    return out


def apply_filings(cfg: dict, st: dict, rows: list[dict], text_of, stamp: str) -> list[tuple[str, str, int]]:
    seen = set(st.get("seen_acc") or [])
    if not st.get("filings_seeded"):
        for row in rows:
            seen.add(row["acc"])
        st["seen_acc"] = sorted(seen)
        st["filings_seeded"] = True
        print(f"filings seed {len(seen)} silent")
        return []
    alerts = []
    fresh = []
    queued = set()
    for row in rows:
        if row["acc"] in seen or row["acc"] in queued:
            continue
        queued.add(row["acc"])
        fresh.append(row)
    for row in sorted(fresh, key=lambda r: (r.get("date") or "", r["acc"])):
        text, link = text_of(row)
        plain = norm(text)
        if not is_results(plain):
            seen.add(row["acc"])
            print(f"filing skip {row['acc']} not results")
            continue
        parsed = parse_results(plain)
        series, ccb_break, streak = update_ccb(
            list(st.get("ccb") or []),
            parsed.get("quarter"),
            parsed.get("ccb_cc"),
            float(cfg["thesis"]["ccb_cc_below"]),
            int(cfg["thesis"]["ccb_break_quarters"]),
        )
        st["ccb"] = series
        breaks = filing_breaks(cfg, parsed, ccb_break)
        alerts.append(filing_message(cfg, row, parsed, breaks, streak, link, stamp))
        seen.add(row["acc"])
        if parsed.get("quarter"):
            st.setdefault("results_sent", {})[row["acc"]] = parsed["quarter"]
        print(f"filing {row['acc']} {parsed.get('quarter')} breaks={breaks}")
    st["seen_acc"] = sorted(seen)
    return alerts


def quarter_seen(st: dict, quarter: str) -> bool:
    return quarter in (st.get("results_sent") or {}).values()


def reminder_message(cfg: dict, stamp: str) -> tuple[str, str, int]:
    q4 = cfg["q4_results"]
    title = f"{TAG} SAP 2026Q4 실적 리마인더 | 1월 28일"
    body = "\n".join([
        "이게 뭐 알림: SAP 2026년 4분기와 연간 실적 발표 전 리마인더. 매매 신호 아님.",
        f"지금 결과/상태: SAP IR 일정에 {q4['date']} {q4['time_note']} 공시로 적혀 있음. {stamp}. 출처: {q4['source']}",
        "지금 할 일: 발표 후 6-K 또는 IR 원문에서 CCB 고정환율 성장, 클라우드 ERP 성장, 2027 가속 가이던스, "
        "클라우드 매출총이익률, 연간 FCF, 서포트 매출을 확인하세요. 새 6-K 가 보이면 다음 실행에서 조건 알림이 따로 갑니다.",
        "나중에: 이 리마인더는 한 번만 갑니다. 실적 6-K 를 이미 읽었으면 이 안내를 보내지 않습니다.",
        "익절/줄일 때: 미보유, 해당 없음.",
        "하지 말 것: 발표 전에 숫자를 예상해서 매수, 이 리마인더를 실적 판정으로 읽기.",
    ])
    check_text(title, body)
    return title, body, 3


def missing_message(cfg: dict, stamp: str) -> tuple[str, str, int]:
    q4 = cfg["q4_results"]
    title = f"{TAG} SAP 2026Q4 실적 공시 아직 없음 | IR 직접 확인"
    body = "\n".join([
        "이게 뭐 알림: 예정일이 지났는데 SAP 2026Q4 실적 6-K 를 아직 못 찾았다는 안내. 매매 신호 아님.",
        f"지금 결과/상태: 공식 일정 {q4['date']} 이후 {q4['missing_after']} 까지 SEC 6-K 실적 문구를 찾지 못함. {stamp}.",
        f"지금 할 일: IR 일정과 최근 실적 페이지를 직접 보세요. {q4['source']}",
        "나중에: 이 안내는 한 번만 갑니다. 그 뒤 6-K 가 나오면 조건 알림이 따로 갑니다.",
        "익절/줄일 때: 미보유, 해당 없음.",
        "하지 말 것: 공시가 없다는 안내를 실적 악화로 읽기.",
    ])
    check_text(title, body)
    return title, body, 3


def maybe_calendar(cfg: dict, st: dict, today: str, sec_ok: bool, stamp: str) -> list[tuple[str, str, int]]:
    if not sec_ok:
        return []
    q4 = cfg["q4_results"]
    quarter = q4["quarter"]
    out = []
    if not st.get("q4_reminder_sent") and not quarter_seen(st, quarter):
        if q4["date"] <= today <= q4["catchup_through"]:
            out.append(reminder_message(cfg, stamp))
            st["q4_reminder_sent"] = today
    if not st.get("q4_missing_sent") and not quarter_seen(st, quarter) and today >= q4["missing_after"]:
        out.append(missing_message(cfg, stamp))
        st["q4_missing_sent"] = today
    return out


def ops_message(fails: list[tuple[str, str]], stamp: str) -> tuple[str, str, int]:
    what = ", ".join(name for name, _ in fails)
    detail = "; ".join(f"{name}: {msg}" for name, msg in fails)
    title = f"{TAG} SAP 조회 실패 | 판정 보류"
    body = "\n".join([
        "이게 뭐 알림: SAP 알림의 데이터 조회 실패 안내. 매매 신호 아님.",
        f"지금 결과/상태: {what} 조회가 실패해서 이번 판정을 건너뜀 ({detail}). {stamp}.",
        "지금 할 일: 필요하면 Yahoo 와 SEC EDGAR 에서 SAP 를 직접 보세요.",
        "나중에: 같은 날 다시 실패해도 이 안내는 하루 1번입니다. 다음 평일 09:00 에 다시 봅니다.",
        "익절/줄일 때: 이 안내로 판단을 바꾸지 않기.",
        "하지 말 것: 조회 실패를 구간 진입이나 논리 이탈로 읽기.",
    ])
    check_text(title, body)
    return title, body, 3


def run(mode: str, now: datetime, cfg: dict, io: dict | None = None) -> tuple[list, dict]:
    io = io or {}
    dry = mode == "dry"
    testing = "state" in io
    stamp = now.astimezone(KST).strftime("%Y-%m-%d %H:%M KST")
    today = stamp[:10]
    st = io["state"] if testing else load_state(STATE_PATH)
    out: list[tuple[str, str, int]] = []
    fails: list[tuple[str, str]] = []
    try:
        rows = io["rows"]() if "rows" in io else yahoo_rows(cfg, now)
        msg = apply_price(cfg, st, rows, stamp)
        if msg:
            out.append(msg)
    except Exception as exc:  # noqa: BLE001
        fails.append(("시세", str(exc)[:150]))
    sec_ok = False
    try:
        filings = io["filings"]() if "filings" in io else sec_rows(cfg)
        text_of = io.get("text_of") or (lambda row: filing_text(cfg, row))
        out.extend(apply_filings(cfg, st, filings, text_of, stamp))
        sec_ok = True
    except Exception as exc:  # noqa: BLE001
        fails.append(("SEC 6-K", str(exc)[:150]))
    out.extend(maybe_calendar(cfg, st, today, sec_ok, stamp))
    if fails and st.get("ops_day") != today:
        st["ops_day"] = today
        out.append(ops_message(fails, stamp))
        print("ops", [name for name, _ in fails])
    elif fails:
        print("ops suppressed", today, [name for name, _ in fails])
    st["updated_at"] = stamp
    if testing:
        return out, st
    for title, body, pri in out:
        print(title)
        print(body)
        if not dry:
            publish(
                resolve_topic("port", cfg.get("ntfy_topic")),
                title,
                body,
                pri,
                ["warning"] if pri >= 4 else ["chart_with_upwards_trend"],
                resolve_server(cfg.get("ntfy_server")),
                config_name=NAME,
            )
    if not out:
        print("no alert")
    if dry:
        print("DRY RUN: no publish, state not written")
    else:
        save_state(STATE_PATH, st)
    return out, st


def selftest(cfg: dict) -> int:
    fails = 0
    n = 0

    def check(label, got, want):
        nonlocal fails, n
        n += 1
        ok = got == want
        fails += not ok
        print(("ok  " if ok else "FAIL"), label, "" if ok else f"got {got!r} want {want!r}")

    h = float(cfg["hysteresis_pct"]) / 100.0
    zones = cfg["zones"]
    armed: dict = {}
    check("bands seed 208.75 outside, all armed", (judge(zones, armed, 208.75, h), dict(armed)), ([], {"t2": True, "t1": True, "nochase": True}))
    check("band 195 is t2 not t1", ([z["key"] for z in judge(zones, {"t2": True, "t1": True, "nochase": True}, 195.0, h)], inside(195.0, zones[0]), inside(195.0, zones[1])), (["t2"], True, False))
    check("band 200 is t1", [z["key"] for z in judge(zones, {"t2": True, "t1": True, "nochase": True}, 200.0, h)], ["t1"])
    check("band 207 is t1", inside(207.0, zones[1]) and not inside(207.0, zones[0]), True)
    check("band 207.01 is none", current_zone(zones, 207.01), "none")
    check("band 235 is nochase", [z["key"] for z in judge(zones, {"t2": True, "t1": True, "nochase": True}, 235.0, h)], ["nochase"])
    check("band 174.99 is none", current_zone(zones, 174.99), "none")

    a = {"t2": True, "t1": True, "nochase": True}
    walk = [200.0, 206.0, 209.0, 210.10, 210.11, 200.0, 194.0, 198.0, 192.0, 236.0, 230.0, 235.0]
    got = []
    for p in walk:
        got.append([z["key"] for z in judge(zones, a, p, h)])
    check(
        "hysteresis walk",
        got,
        [["t1"], [], [], [], [], ["t1"], ["t2"], [], ["t2"], ["nochase"], [], ["nochase"]],
    )
    def ny_ts(y, m, d, hh, mm):
        return int(datetime(y, m, d, hh, mm, tzinfo=NY).timestamp())

    chart = {
        "chart": {
            "result": [{
                "timestamp": [ny_ts(2026, 11, 24, 16, 0), ny_ts(2026, 11, 25, 16, 0), ny_ts(2026, 11, 28, 16, 0)],
                "indicators": {"quote": [{"close": [208.75, 200.0, 190.0]}]},
                "meta": {},
            }]
        }
    }
    before = bars_from_chart(chart, datetime(2026, 11, 25, 16, 9, tzinfo=NY), "16:10")
    after = bars_from_chart(chart, datetime(2026, 11, 25, 16, 10, tzinfo=NY), "16:10")
    check("completed-session before 16:10 drops today", [d for d, _ in before], ["2026-11-24"])
    check("completed-session at 16:10 keeps today", [d for d, _ in after], ["2026-11-24", "2026-11-25"])
    check("completed-session drops Saturday", "2026-11-28" in [d for d, _ in after], False)

    hist = [
        ("2026-11-05", 243.53),
        ("2026-11-17", 222.51),
        ("2026-11-23", 203.40),
        ("2026-11-24", 208.75),
    ]
    io = {"state": {}, "rows": lambda: list(hist), "filings": lambda: [{"form": "6-K", "date": "2026-10-26", "acc": "0001104659-26-118617", "doc": "a.htm"}], "text_of": lambda row: ("not results", "http://example")}
    out, st = run("dry", datetime(2026, 11, 25, 9, 0, tzinfo=KST), cfg, io)
    check("seed silent", out, [])
    check("seed t1 disarmed after recent inside", st["armed"].get("t1"), False)
    check("seed t2 armed", st["armed"].get("t2"), True)
    check("seed nochase rearmed after 222.51", st["armed"].get("nochase"), True)
    check("seed last_date", st.get("last_date"), "2026-11-24")
    check("filing seed marks acc", "0001104659-26-118617" in st["seen_acc"], True)

    hist.append(("2026-11-25", 206.0))
    io["state"] = st
    out, st = run("dry", datetime(2026, 11, 26, 9, 0, tzinfo=KST), cfg, io)
    check("hysteresis no reentry while inside buffer", out, [])
    hist.append(("2026-11-26", 211.0))
    out, st = run("dry", datetime(2026, 11, 27, 9, 0, tzinfo=KST), cfg, io)
    check("rearm above 210.105 silent", out, [])
    hist.append(("2026-11-27", 200.0))
    out, st = run("dry", datetime(2026, 11, 28, 9, 0, tzinfo=KST), cfg, io)
    check("zone entry after rearm", [o[0] for o in out], ["[SAP] SAP 종가 $200.00 | 1차 구간 진입"])
    check("title ticker", out[0][0].startswith("[SAP]") and "SAP" in out[0][0], True)
    check("six parts", [x.split(":")[0] for x in out[0][1].split("\n")], ["이게 뭐 알림", "지금 결과/상태", "지금 할 일", "나중에", "익절/줄일 때", "하지 말 것"])
    check("no dot/dash", BANNED_RE.search(out[0][0] + out[0][1]) is None, True)
    out, st = run("dry", datetime(2026, 11, 28, 9, 5, tzinfo=KST), cfg, io)
    check("dedup same data date", out, [])

    q3 = (
        "Quarterly Statement Q3 2026. SAP SE announced today its financial results for the third quarter ended September 30, 2026. "
        "Current cloud backlog of EUR 24.1 billion, up 28% and up 26% at constant currencies. "
        "Cloud ERP Suite revenue up 29% and up 28% at constant currencies. "
        "Cloud gross margin (in %) 73.9 74.6 -0.7pp 74.3 75.1 -0.8pp -0.7pp. "
        "Software support revenue 2,434 2,565 -5 2,434 2,565 -5 -6. "
        "Approximately EUR 10 billion free cash flow at actual currencies. "
        "Constant currencies total revenue growth in 2026 to remain at similar levels and to accelerate in 2027."
    )
    parsed = parse_results(q3)
    check("parse q3", (parsed["quarter"], parsed["ccb_cc"], parsed["cloud_erp_cc"], parsed["cloud_gm"], parsed["support_cc"], parsed["fcf_bn"], parsed["accel_2027"]), ("2026Q3", 26.0, 28.0, 74.3, -6.0, 10.0, "kept"))
    series, brk, streak = update_ccb([], "2026Q3", 26.0, 20.0, 2)
    check("ccb one good quarter is not a break", (brk, streak), (False, 0))
    breaks = filing_breaks(cfg, parsed, False)
    check("q3 support -6 is a break flag", breaks, ["서포트 매출 감소 6% 이상"])

    low = dict(parsed, support_cc=-4.0, cloud_erp_cc=22.0, cloud_gm=71.5, fcf_bn=9.4, accel_2027="withdrawn", ccb_cc=18.0)
    series, brk, streak = update_ccb([{"q": "2026Q3", "cc": 19.0}], "2026Q4", 18.0, 20.0, 2)
    check("ccb two consecutive below 20", (brk, streak, [x["q"] for x in series]), (True, 2, ["2026Q3", "2026Q4"]))
    check("all hard flags", filing_breaks(cfg, low, True), [
        "CCB 성장 2분기 연속 20% 미만",
        "클라우드 ERP 성장 저조",
        "2027 가속 가이던스 철회",
        "클라우드 매출총이익률 72% 미만",
        "연간 FCF EUR 10B 미만",
    ])
    bare = "SAP SE announced today its financial results for the first quarter ended March 31, 2027."
    bare_p = parse_results(bare)
    check("unparsed stays unlabeled", (bare_p["quarter"], bare_p["ccb_cc"], bare_p["accel_2027"], filing_breaks(cfg, bare_p, False)), ("2027Q1", None, None, []))
    title, body, pri = filing_message(cfg, {"date": "2027-04-22", "form": "6-K", "acc": "acc-1"}, bare_p, [], 0, "https://www.sec.gov/Archives/example", "s")
    check("checklist title", (title, pri, "자동 판정 불가" in body, "SAP" in title), ("[SAP] SAP 분기 실적 게시 | 조건 확인", 3, True, True))

    filings = [
        {"form": "6-K", "date": "2026-10-26", "acc": "old", "doc": "old.htm"},
    ]
    texts = {
        "old": ("share repurchase only", "http://old"),
        "new1": (q3, "https://www.sec.gov/Archives/new1"),
    }
    io2 = {
        "state": {"last_date": "2026-11-24", "armed": {"t1": True, "t2": True, "nochase": True}, "seen_acc": [], "ccb": [], "results_sent": {}, "filings_seeded": False},
        "rows": lambda: [("2026-11-24", 208.75)],
        "filings": lambda: list(filings),
        "text_of": lambda row: texts[row["acc"]],
    }
    out, st = run("dry", datetime(2026, 11, 25, 9, 0, tzinfo=KST), cfg, io2)
    check("filing seed does not send old", out, [])
    filings.append({"form": "6-K", "date": "2027-01-28", "acc": "new1", "doc": "new.htm"})
    st["filings_seeded"] = True
    st["seen_acc"] = ["old"]
    io2["state"] = st
    out, st = run("dry", datetime(2026, 12, 1, 9, 0, tzinfo=KST), cfg, io2)
    check("new results filing sends once", [o[0] for o in out], ["[SAP] SAP 논리 이탈 | 서포트 매출 감소 6% 이상"])
    out, st = run("dry", datetime(2026, 12, 2, 9, 0, tzinfo=KST), cfg, io2)
    check("filing dedup", [o[0] for o in out if "실적" in o[0] or "논리" in o[0]], [])

    io3 = {
        "state": {"last_date": "2027-01-27", "armed": {"t1": True, "t2": True, "nochase": True}, "seen_acc": ["old"], "ccb": [], "results_sent": {}, "filings_seeded": True},
        "rows": lambda: [("2027-01-27", 210.0)],
        "filings": lambda: [{"form": "6-K", "date": "2026-10-26", "acc": "old", "doc": "x"}],
        "text_of": lambda row: ("nope", "http://x"),
    }
    out, st = run("dry", datetime(2027, 1, 28, 9, 0, tzinfo=KST), cfg, io3)
    check("q4 reminder on official date", [o[0] for o in out], ["[SAP] SAP 2026Q4 실적 리마인더 | 1월 28일"])
    check("reminder has no estimate wording", "추정" not in out[0][1], True)
    io3["state"] = st
    out, st = run("dry", datetime(2027, 1, 29, 9, 0, tzinfo=KST), cfg, io3)
    check("reminder once", out, [])
    io3["state"] = st
    out, st = run("dry", datetime(2027, 2, 6, 9, 0, tzinfo=KST), cfg, io3)
    check("missing after window", [o[0] for o in out], ["[SAP] SAP 2026Q4 실적 공시 아직 없음 | IR 직접 확인"])

    bad = {"state": {}, "rows": lambda: (_ for _ in ()).throw(OSError("yahoo down")), "filings": lambda: (_ for _ in ()).throw(OSError("sec down"))}
    out, st = run("dry", datetime(2026, 11, 25, 9, 0, tzinfo=KST), cfg, bad)
    check("ops on first failure", [o[0] for o in out], ["[SAP] SAP 조회 실패 | 판정 보류"])
    check("ops title ticker and route word", ("[SAP]" in out[0][0] and "조회 실패" in out[0][0]), True)
    bad["state"] = st
    out, st = run("dry", datetime(2026, 11, 25, 9, 5, tzinfo=KST), cfg, bad)
    check("ops once per KST day", out, [])
    bad["state"] = st
    out, st = run("dry", datetime(2026, 11, 26, 9, 0, tzinfo=KST), cfg, bad)
    check("ops next KST day", len(out), 1)

    print("selftest", "PASS" if not fails else f"FAIL {fails}", n)
    return 1 if fails else 0


def main() -> int:
    args = [a.lower() for a in sys.argv[1:]]
    mode = args[0] if args else "auto"
    cfg = load_cfg()
    if mode == "selftest":
        return selftest(cfg)
    if mode not in {"auto", "dry"}:
        print(__doc__)
        return 2
    now = datetime.now(KST)
    ends = cfg.get("ends_kst")
    if ends and now > datetime.strptime(ends, "%Y-%m-%d %H:%M").replace(tzinfo=KST):
        print(f"{NAME} ended {ends} KST; silent")
        return 0
    run(mode, now, cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
