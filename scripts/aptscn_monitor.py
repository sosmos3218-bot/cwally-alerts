#!/usr/bin/env python3
"""[KOSPI] 아파트 시장 시나리오. 판정이 바뀔 때만 cwally-port.

목요일 19:00 k203780 슬롯에서 한 번. 그 주 성공 기록이 없으면 금요일 09:00 morning 이 따라잡음.
새 cron 없음. 조회가 실패하면 시나리오 변경으로 보내지 않고 cwally-ops 하루 한 번.

modes: auto | dry (출력만, 발송 없음, state 안 씀) | selftest (네트워크 없음)
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from html import unescape
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import append_ends, publish, resolve_server  # noqa: E402

NAME = "aptscn"
CFG_PATH = ROOT / "configs" / f"{NAME}.json"
STATE_PATH = ROOT / "state" / f"{NAME}.json"
KST = timezone(timedelta(hours=9))
TAG = "[KOSPI]"
SCENARIO = "완만한 둔화"
RISE = "상승 지속"
FALL = "본격 하락"
BANNED = ("\u00b7", "\u2014")
UA = {"User-Agent": "Mozilla/5.0 (cwally-alerts; +mailto:sosmos3218@gmail.com)"}
DEC_KO = {"hold": "동결", "hike": "인상", "cut": "인하"}


def load_json(path: Path, default: dict) -> dict:
    if not path.exists():
        return dict(default)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return dict(default)
    return raw if isinstance(raw, dict) else dict(default)


def save_json(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def iso_week(d: date) -> str:
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def week_monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def norm_cell(v) -> str:
    if v is None:
        return ""
    return re.sub(r"\s+", "", str(v))


def pct(v: float | None) -> str:
    return "값 없음" if v is None else f"{v:+.2f}%"


def jo(v: float | None) -> str:
    return "값 없음" if v is None else f"{v:+.1f}조"


def comma(v: int | None) -> str:
    return "값 없음" if v is None else f"{int(v):,}건"


def check_copy(title: str, body: str) -> None:
    blob = title + "\n" + body
    bad = [c for c in BANNED if c in blob]
    if bad:
        raise RuntimeError("banned punctuation in ntfy copy")


def http_bytes(url: str, data: dict | None = None, timeout: int = 30) -> bytes:
    body = None
    headers = dict(UA)
    if data is not None:
        body = urllib.parse.urlencode(data).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def http_text(url: str, data: dict | None = None, timeout: int = 30) -> str:
    return http_bytes(url, data=data, timeout=timeout).decode("utf-8", "replace")


def http_json(url: str, data: dict | None = None, timeout: int = 40):
    return json.loads(http_text(url, data=data, timeout=timeout))


def month_shift(ym: str, delta: int) -> str:
    y, m = int(ym[:4]), int(ym[4:6])
    idx = y * 12 + (m - 1) + delta
    return f"{idx // 12}{(idx % 12) + 1:02d}"


def should_run(now: datetime, job: str, state: dict, mode: str) -> bool:
    if mode == "dry" or job == NAME:
        return True
    iso = iso_week(now.date())
    wd = now.weekday()
    if job == "k203780" and wd == 3:
        return not (state.get("last_ok_iso") == iso and state.get("last_fetch_day") == now.date().isoformat())
    if job == "morning" and wd == 4:
        return state.get("last_ok_iso") != iso
    if job in ("k203780", "morning"):
        return False
    if wd == 3 and now.hour >= 16:
        return not (state.get("last_ok_iso") == iso and state.get("last_fetch_day") == now.date().isoformat())
    if wd == 4 and now.hour < 12:
        return state.get("last_ok_iso") != iso
    return False


# ---------------- weekly REB workbook ----------------
def download_weekly(cfg: dict) -> tuple[Path, str]:
    src = cfg["sources"]
    listing = http_json(src["reb_list"], {"page": 1, "rows": 20, "listSubCd": src["reb_list_sub"]})
    item = next(x for x in listing["data"] if src["reb_title_key"] in (x.get("bbsTit") or ""))
    seq = int(item["seq"])
    detail = http_json(src["reb_detail"], {"bbsCd": src["reb_bbs"], "seq": seq})["data"]
    xfiles = [f for f in (detail.get("files") or []) if str(f.get("fileExt", "")).lower() == "xlsx"]
    if not xfiles:
        raise RuntimeError("xlsx missing")
    xfile = max(xfiles, key=lambda f: int(f.get("fileSize") or 0))
    file_seq = int(xfile["fileSeq"])
    dest = Path(tempfile.gettempdir()) / f"aptscn_{file_seq}.xlsx"
    size = int(xfile.get("fileSize") or 0)
    if not dest.exists() or (size and dest.stat().st_size != size):
        raw = http_bytes(
            src["reb_file"],
            {"bbsCd": src["reb_bbs"], "seq": str(seq), "fileSeq": str(file_seq)},
            timeout=120,
        )
        if not raw.startswith(b"PK"):
            raise RuntimeError("xlsx is not a zip")
        dest.write_bytes(raw)
    return dest, str(detail.get("userDttm") or "")


def sheet_series(path: Path, sheet: str, regions: dict[str, str]) -> tuple[dict[str, int], list[dict]]:
    from openpyxl import load_workbook

    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        ws = wb[sheet]
        rows = ws.iter_rows(values_only=True)
        header = None
        for _ in range(6):
            row = next(rows)
            labels = [norm_cell(c) for c in row]
            if "서울" in labels and "경기" in labels:
                header = labels
                break
        if header is None:
            raise RuntimeError(f"{sheet} header missing")
        idx: dict[str, int] = {}
        for label, key in regions.items():
            hits = [i for i, lab in enumerate(header) if lab == label]
            if len(hits) != 1:
                raise RuntimeError(f"{sheet} {label} cols {hits}")
            idx[key] = hits[0]
        out = []
        for row in rows:
            dt = row[0] if row else None
            if not hasattr(dt, "strftime"):
                continue
            rec = {"date": dt.strftime("%Y-%m-%d")}
            missing = False
            for key, i in idx.items():
                val = row[i] if i < len(row) else None
                if val is None:
                    missing = True
                    break
                rec[key] = float(val)
            if not missing:
                out.append(rec)
    finally:
        wb.close()
    if len(out) < 4:
        raise RuntimeError(f"{sheet} rows {len(out)}")
    return idx, out


def build_weeks(path: Path, cfg: dict) -> list[dict]:
    src = cfg["sources"]
    regions = src["regions"]
    _, sale = sheet_series(path, src["sale_sheet"], regions)
    _, jeonse = sheet_series(path, src["jeonse_sheet"], regions)
    by_date = {row["date"]: row for row in jeonse}
    weeks = []
    for row in sale:
        j = by_date.get(row["date"])
        if not j:
            continue
        g3 = [row[k] for k in ("seocho", "gangnam", "songpa")]
        weeks.append({
            "date": row["date"],
            "seoul": row["seoul"],
            "gyeonggi": row["gyeonggi"],
            "jeonse": j["seoul"],
            "gangnam3": sum(g3) / 3.0,
        })
    weeks.sort(key=lambda r: r["date"])
    if len(weeks) < 4:
        raise RuntimeError("joined weeks short")
    return weeks


# ---------------- monthly ----------------
def latest_decision(changes: list[tuple[str, float]], meetings: list[str], today: date, hour: int, confirm_hour: int) -> dict | None:
    """Last confirmed 금통위 decision. A later rate-table move wins if the calendar is missing it."""
    ch = sorted((d, float(r)) for d, r in changes)
    if not ch:
        return None
    today_s = today.isoformat()
    confirmed = [m for m in sorted(set(meetings)) if m < today_s or (m == today_s and hour >= confirm_hour)]
    moves = [(d, r) for d, r in ch if d <= today_s]
    if not moves:
        return None
    move_d, move_r = moves[-1]
    prev_r = None
    for d, r in moves:
        if d < move_d:
            prev_r = r
    meeting = confirmed[-1] if confirmed else None
    if meeting is None or move_d > meeting:
        kind = "hold" if prev_r is None or abs(move_r - prev_r) < 0.001 else ("hike" if move_r > prev_r else "cut")
        return {"date": move_d, "rate": move_r, "decision": kind}
    at = [r for d, r in moves if d == meeting]
    before = [r for d, r in moves if d < meeting]
    if at and before:
        kind = "hike" if at[-1] > before[-1] + 0.001 else ("cut" if at[-1] < before[-1] - 0.001 else "hold")
        rate = at[-1]
    elif at and not before:
        kind, rate = "hold", at[-1]
    else:
        kind, rate = "hold", before[-1] if before else move_r
    return {"date": meeting, "rate": rate, "decision": kind}


def fetch_decision(cfg: dict, now: datetime) -> dict:
    import bok_mpc

    src = cfg["sources"]
    html = bok_mpc.fetch(src["bok_base_rate_table"], 25)
    changes = bok_mpc.parse_base_table(html)
    if not changes:
        raise RuntimeError("base rate table empty")
    meetings = set(src.get("meetings_seed") or [])
    for year in (now.year - 1, now.year, now.year + 1):
        try:
            page = bok_mpc.fetch(src["bok_meeting_page"].format(year=year), 25)
        except Exception:
            continue
        meetings |= {f"{year}-{mo}-{d}" for mo, d in re.findall(r'<th scope="row">\s*(\d{2})월\s*(\d{2})일', page)}
    got = latest_decision(changes, sorted(meetings), now.date(), now.hour, int(cfg["rules"]["meeting_confirm_hour"]))
    if not got or got.get("rate") is None:
        raise RuntimeError("decision empty")
    got["source"] = "한은 기준금리 표 + 금통위 일정"
    got["link"] = src["bok_base_rate_table"]
    return got


def fetch_mortgage(cfg: dict, now: datetime) -> dict:
    end = f"{now.year}{now.month:02d}"
    start = month_shift(end, -7)
    key = os.environ.get("ECOS_API_KEY") or "sample"
    url = (
        "https://ecos.bok.or.kr/api/StatisticSearch/"
        f"{urllib.parse.quote(key)}/json/kr/1/10/121Y006/M/{start}/{end}/BECBLA0302"
    )
    payload = json.loads(http_text(url, timeout=25))
    rows = ((payload.get("StatisticSearch") or {}).get("row")) or []
    if not rows:
        res = payload.get("RESULT") or {}
        raise RuntimeError(f"ECOS mortgage {res.get('CODE')}")
    series = {}
    for row in rows:
        val = row.get("DATA_VALUE")
        if val in (None, ""):
            continue
        series[str(row["TIME"])] = float(val)
    if not series:
        raise RuntimeError("ECOS mortgage empty")
    latest = max(series)
    prior = month_shift(latest, -int(cfg["rules"]["mortgage_lookback_months"]))
    delta = None if prior not in series else series[latest] - series[prior]
    return {
        "month": f"{latest[:4]}-{latest[4:]}",
        "rate": series[latest],
        "prior_month": f"{prior[:4]}-{prior[4:]}",
        "prior_rate": series.get(prior),
        "delta": delta,
        "source": "ECOS 121Y006 주택담보대출 신규취급액",
        "link": "https://ecos.bok.or.kr/",
    }


def _pdf_text(raw: bytes) -> str:
    if not raw.startswith(b"%PDF"):
        raise RuntimeError("not a pdf")
    from pypdf import PdfReader
    from io import BytesIO

    reader = PdfReader(BytesIO(raw))
    return "\n".join((page.extract_text() or "") for page in reader.pages)


def parse_loan_text(text: str, year: int) -> dict:
    compact = re.sub(r"\s+", "", text)
    m = re.search(
        r"가계대출\((?:\d{2}\.)?(\d{1,2})월([+-]?\d+(?:\.\d+)?)조원[\u2192>\-]+(\d{1,2})월([+-]?\d+(?:\.\d+)?)조원\)",
        compact,
    )
    if not m:
        raise RuntimeError("loan sentence missing")
    month = int(m.group(3))
    change = float(m.group(4))
    return {"month": f"{year}-{month:02d}", "change": change, "prev_change": float(m.group(2))}


def fetch_loans(cfg: dict) -> dict:
    html = http_text(cfg["sources"]["bok_news_list"], timeout=30)
    found = []
    for m in re.finditer(r'<a[^>]+href="([^"]*nttId=\d+[^"]*)"[^>]*>(.*?)</a>', html, re.S):
        title = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", unescape(m.group(2)))).strip()
        title = re.sub(r"^.*-->\s*", "", title)
        hit = re.search(r"(\d{4})년\s*(\d{1,2})월\s*금융시장 동향", title)
        if not hit or "국제" in title:
            continue
        found.append((int(hit.group(1)), int(hit.group(2)), unescape(m.group(1)), title))
    if not found:
        raise RuntimeError("loan release missing")
    year, month, href, title = sorted(found)[-1]
    if href.startswith("/"):
        view = "https://www.bok.or.kr" + href
    elif href.startswith("http"):
        view = href
    else:
        view = "https://www.bok.or.kr/" + href
    page = http_text(view, timeout=30)
    pdf_href = re.search(r'href="(/fileSrc/[^"]+\.pdf)"', page)
    if not pdf_href:
        raise RuntimeError("loan pdf missing")
    raw = http_bytes("https://www.bok.or.kr" + pdf_href.group(1), timeout=60)
    parsed = parse_loan_text(_pdf_text(raw), year)
    if parsed["month"] != f"{year}-{month:02d}":
        raise RuntimeError(f"loan month {parsed['month']} != {year}-{month:02d}")
    parsed["source"] = "한은 금융시장 동향"
    parsed["link"] = view.split("&pageUnit=")[0]
    parsed["title"] = title
    return parsed


def parse_housing_title(title: str) -> tuple[int, int] | None:
    t = title.replace("’", "'").replace("‘", "'")
    m = re.search(r"(20\d{2}|\d{2})년\s*(\d{1,2})월", t)
    if not m:
        return None
    year = int(m.group(1))
    if year < 100:
        year += 2000
    return year, int(m.group(2))


def parse_tx_text(text: str, year: int, month: int) -> dict:
    apt = re.search(r"서울\s*지역\s*아파트\s*매매거래는\s*([\d,]+)\s*건", text)
    if not apt:
        raise RuntimeError("seoul apt volume missing")
    sec = re.search(r"주택 매매거래량\(신고일 기준, 전체주택\)(.*)", text, re.S)
    if not sec:
        raise RuntimeError("gyeonggi table missing")
    block = re.split(r"참고\s*\d+", sec.group(1), maxsplit=1)[0]
    gg = re.search(r"경\s*기\s+([\d,]+)", block)
    if not gg:
        raise RuntimeError("gyeonggi volume missing")
    return {
        "month": f"{year}-{month:02d}",
        "seoul_apt": int(apt.group(1).replace(",", "")),
        "gyeonggi_housing": int(gg.group(1).replace(",", "")),
    }


def fetch_tx(cfg: dict) -> dict:
    html = http_text(cfg["sources"]["kdi_list"], timeout=30)
    items = []
    for m in re.finditer(r"<p>([^<]*주택통계[^<]*)</p>", html):
        title = unescape(m.group(1)).strip()
        ym = parse_housing_title(title)
        if not ym:
            continue
        rest = html[m.end():m.end() + 1800]
        dl = re.search(r"callDownload\.do\?num=(\d+)", rest)
        if not dl:
            continue
        items.append({"title": title, "year": ym[0], "month": ym[1], "num": dl.group(1)})
    if not items:
        raise RuntimeError("kdi housing list empty")
    item = sorted(items, key=lambda r: (r["year"], r["month"]))[-1]
    raw = http_bytes(f"https://eiec.kdi.re.kr/policy/callDownload.do?num={item['num']}&filenum=1", timeout=60)
    parsed = parse_tx_text(_pdf_text(raw), item["year"], item["month"])
    parsed["source"] = "KDI 재게시 국토부 주택통계"
    parsed["link"] = f"https://eiec.kdi.re.kr/policy/materialView.do?num={item['num']}"
    parsed["title"] = item["title"]
    return parsed


def with_baseline(live: dict, baseline: dict) -> dict:
    out = dict(live)
    for key in ("decision", "mortgage", "loans", "tx"):
        if out.get(key):
            continue
        seed = baseline.get(key)
        if isinstance(seed, dict):
            row = dict(seed)
            row["source"] = "설치 기준"
            out[key] = row
    return out


# ---------------- judge ----------------
def both_neg_streak(weeks: list[dict]) -> int:
    n = 0
    for row in reversed(weeks):
        if row["seoul"] < 0 and row["gyeonggi"] < 0:
            n += 1
        else:
            break
    return n


def judge(weeks: list[dict], monthly: dict, rules: dict) -> dict:
    last = weeks[-4:]
    if len(last) < 4:
        raise RuntimeError("need 4 weeks")
    cur = last[-1]
    seoul_4w = sum(row["seoul"] for row in last) / 4.0
    decision = monthly.get("decision") or {}
    loans = monthly.get("loans") or {}
    mortgage = monthly.get("mortgage") or {}
    tx = monthly.get("tx") or {}
    loan_change = loans.get("change")
    loans_growing = None if loan_change is None else loan_change > float(rules["loan_flat_max"])
    loans_stopped = None if loan_change is None else loan_change <= float(rules["loan_flat_max"])
    delta = mortgage.get("delta")
    rate_cut = decision.get("decision") == "cut" or (
        delta is not None and delta <= -float(rules["mortgage_cut_pp"])
    )
    rate_hike = decision.get("decision") == "hike"
    streak = both_neg_streak(weeks)
    fall_a = streak >= int(rules["fall_negative_weeks"])
    fall_b = cur["jeonse"] <= 0 and (rate_hike or loans_stopped is True)
    rise = (
        seoul_4w >= float(rules["seoul_sale_4w_min"])
        and cur["gyeonggi"] > 0
        and cur["jeonse"] > 0
        and (loans_growing is True or rate_cut)
    )
    if fall_a or fall_b:
        scenario = FALL
    elif rise:
        scenario = RISE
    else:
        scenario = SCENARIO
    facts = {
        "scenario": scenario,
        "week": cur["date"],
        "week_from": last[0]["date"],
        "seoul": cur["seoul"],
        "gyeonggi": cur["gyeonggi"],
        "jeonse": cur["jeonse"],
        "seoul_4w": seoul_4w,
        "gangnam3": cur.get("gangnam3"),
        "streak": streak,
        "fall_a": fall_a,
        "fall_b": fall_b,
        "rise": rise,
        "decision": decision.get("decision"),
        "rate": decision.get("rate"),
        "decision_date": decision.get("date"),
        "decision_source": decision.get("source"),
        "decision_link": decision.get("link"),
        "loans": loan_change,
        "loans_month": loans.get("month"),
        "loans_source": loans.get("source"),
        "loans_link": loans.get("link"),
        "mortgage": mortgage.get("rate"),
        "mortgage_month": mortgage.get("month"),
        "mortgage_delta": delta,
        "mortgage_source": mortgage.get("source"),
        "mortgage_link": mortgage.get("link"),
        "tx_seoul": tx.get("seoul_apt"),
        "tx_gg": tx.get("gyeonggi_housing"),
        "tx_month": tx.get("month"),
        "tx_source": tx.get("source"),
        "tx_link": tx.get("link"),
    }
    return facts


def reason_lines(facts: dict, rules: dict) -> list[str]:
    """The 3 or 4 readings that decided the label."""
    week = facts["week"]
    rate_txt = "값 없음" if facts.get("rate") is None else f"{facts['rate']:.2f}%"
    lines = []
    if facts["scenario"] == FALL and facts["fall_a"]:
        lines.append(
            f"서울 매매 {pct(facts['seoul'])}, 경기 매매 {pct(facts['gyeonggi'])} ({week}), "
            f"둘 다 마이너스 {facts['streak']}주 연속 (기준 {int(rules['fall_negative_weeks'])}주)"
        )
        lines.append(f"서울 전세 {pct(facts['jeonse'])} ({week})")
    elif facts["scenario"] == FALL and facts["fall_b"]:
        lines.append(f"서울 전세 {pct(facts['jeonse'])} ({week}), 상승 멈춤")
        lines.append(
            f"기준금리 {rate_txt} {DEC_KO.get(facts['decision'], '값 없음')} ({facts['decision_date']}), "
            f"은행 가계대출 {facts['loans_month']} {jo(facts['loans'])}"
        )
    elif facts["scenario"] == RISE:
        lines.append(
            f"서울 매매 4주 평균 {pct(facts['seoul_4w'])} ({facts['week_from']}~{week}), "
            f"기준 {float(rules['seoul_sale_4w_min']):.2f}% 이상"
        )
        lines.append(f"경기 매매 {pct(facts['gyeonggi'])}, 서울 전세 {pct(facts['jeonse'])} ({week})")
        lines.append(
            f"은행 가계대출 {facts['loans_month']} {jo(facts['loans'])}, "
            f"기준금리 {facts['decision_date']} {DEC_KO.get(facts['decision'], '값 없음')}"
        )
    else:
        lines.append(
            f"서울 매매 4주 평균 {pct(facts['seoul_4w'])} ({facts['week_from']}~{week}), "
            f"기준 {float(rules['seoul_sale_4w_min']):.2f}% 미만"
        )
        lines.append(
            f"서울 매매 {pct(facts['seoul'])}, 경기 매매 {pct(facts['gyeonggi'])} ({week}), "
            f"동시 하락 {facts['streak']}주"
        )
        lines.append(f"서울 전세 {pct(facts['jeonse'])} ({week})")
        lines.append(
            f"은행 가계대출 {facts['loans_month']} {jo(facts['loans'])}, "
            f"기준금리 {rate_txt} {DEC_KO.get(facts['decision'], '값 없음')} ({facts['decision_date']})"
        )
    return lines[:4]


def build_body(prev: str, facts: dict, rules: dict, press: str) -> str:
    scenario = facts["scenario"]
    reasons = " ".join(reason_lines(facts, rules))
    rate = "값 없음" if facts.get("rate") is None else f"{facts['rate']:.2f}%"
    delta = "값 없음" if facts.get("mortgage_delta") is None else f"{facts['mortgage_delta']:+.2f}%p"
    mortgage_txt = "값 없음" if facts.get("mortgage") is None else f"{facts['mortgage']:.2f}%"
    ref = (
        f"참고, 판정 제외: 강남3구 매매(서초, 강남, 송파 평균) {pct(facts.get('gangnam3'))} ({facts['week']}). "
        f"서울 아파트 매매 {facts.get('tx_month')} {comma(facts.get('tx_seoul'))}. "
        f"경기 주택 매매 {facts.get('tx_month')} {comma(facts.get('tx_gg'))}."
    )
    links = " ".join(
        bit for bit in (
            f"주간 시계열 {press}",
            f"보도 목록 https://www.reb.or.kr/reb/na/ntt/selectNttList.do?bbsId=1154&mi=9565",
            f"기준금리 {facts.get('decision_link') or ''}".strip(),
            f"가계대출 {facts.get('loans_link') or ''}".strip(),
            f"주담대 {facts.get('mortgage_source') or ''}".strip(),
            f"거래량 {facts.get('tx_link') or ''}".strip(),
        ) if bit
    )
    do = {
        RISE: "추격하지 말 것. 대출이 계속 늘거나 금리가 내려가는지만 다음 달에 확인.",
        FALL: "주택 관련 추가 매수는 보류. 보유분만 직접 점검.",
        SCENARIO: "비중을 급히 바꾸지 말 것. 다음 주 서울, 경기 매매만 확인.",
    }[scenario]
    lines = [
        "이게 뭐 알림: KOSPI 아파트 시장 시나리오가 직전과 달라졌는지 보는 주간 점검. 매매 신호 아님.",
        f"지금 결과/상태: 이전 {prev} -> 지금 {scenario}. {reasons} 신규 주담대 {facts.get('mortgage_month')} "
        f"{mortgage_txt} (3개월 전과 {delta}). "
        f"기준금리 {rate}. {ref}",
        f"지금 할 일: {do}",
        "나중에: 다음 목요일 19:00에 다시 본다. 시나리오가 같으면 알림은 없다. 목요일 숫자가 없으면 금요일 09:00에 한 번 더 본다.",
        "익절/줄일 때: 이 알림은 매도 시점이 아니다. 본격 하락으로 바뀔 때만 주택 관련 비중을 직접 점검.",
        "하지 말 것: 시나리오 이름만 보고 매수하거나 매도하지 말 것. 못 읽은 숫자를 채워 넣지 말 것.",
        f"출처: {links}",
    ]
    return "\n".join(lines)


def title_for(prev: str, scenario: str) -> str:
    return f"{TAG} 아파트 시나리오 변경: {prev} -> {scenario}"


def outcome(prev: str | None, scenario: str, weekly_ok: bool) -> str:
    if not weekly_ok:
        return "ops"
    if not prev:
        return "seed"
    if prev != scenario:
        return "send"
    return "quiet"


def ops_notify(state: dict, today: str, title: str, body: str, dry: bool) -> None:
    check_copy(title, body)
    print(title)
    print(body)
    if state.get("ops_day") == today:
        print("ops already sent today")
        return
    state["ops_day"] = today
    if dry:
        print("DRY RUN ops not sent")
        return
    publish("cwally-ops", title, body, priority=3, tags=["warning"], server=resolve_server(), config_name=NAME)


def snapshot(facts: dict) -> dict:
    keep = (
        "week", "seoul", "gyeonggi", "jeonse", "seoul_4w", "gangnam3", "streak",
        "decision", "rate", "decision_date", "loans", "loans_month", "mortgage",
        "mortgage_month", "mortgage_delta", "tx_seoul", "tx_gg", "tx_month",
    )
    return {k: facts.get(k) for k in keep}


# ---------------- run ----------------
def collect(cfg: dict, now: datetime, state: dict) -> tuple[dict | None, dict, list[str], str]:
    errors: list[str] = []
    path, posted = download_weekly(cfg)
    weeks = build_weeks(path, cfg)
    monthly: dict = {}
    for key, fn in (
        ("decision", lambda: fetch_decision(cfg, now)),
        ("mortgage", lambda: fetch_mortgage(cfg, now)),
        ("loans", lambda: fetch_loans(cfg)),
        ("tx", lambda: fetch_tx(cfg)),
    ):
        try:
            monthly[key] = fn()
        except Exception as exc:
            errors.append(f"{key} {type(exc).__name__}: {exc}")
            prev = (state.get("monthly") or {}).get(key)
            if isinstance(prev, dict):
                monthly[key] = prev
    monthly = with_baseline(monthly, cfg.get("baseline") or {})
    facts = judge(weeks, monthly, cfg["rules"])
    facts["posted"] = posted
    return facts, monthly, errors, weeks[-1]["date"]


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    if mode == "selftest":
        return selftest()
    if mode not in {"auto", "dry"}:
        print("usage: aptscn_monitor.py auto|dry|selftest", file=sys.stderr)
        return 2
    cfg = load_json(CFG_PATH, {})
    state = load_json(STATE_PATH, {})
    now = datetime.now(KST)
    job = os.environ.get("PICK_JOB") or ""
    dry = mode == "dry"
    if not should_run(now, job, state, mode):
        print(f"aptscn skip job={job or '-'} {now:%Y-%m-%d %H:%M} dow={now.weekday()}")
        return 0
    today = now.date().isoformat()
    monday = week_monday(now.date()).isoformat()
    try:
        facts, monthly, errors, week = collect(cfg, now, state)
    except Exception as exc:
        ops_notify(
            state, today,
            f"[OPS] {TAG[1:-1]} 아파트 시나리오 수집 실패",
            "\n".join([
                "이게 뭐 알림: KOSPI 아파트 시나리오 주간 숫자 읽기 실패. 매매 신호 아님.",
                f"지금 결과/상태: 시나리오는 바꾸지 않음. 직전 판정 {state.get('scenario') or '없음'}. 이유 {type(exc).__name__}: {exc}",
                "지금 할 일: 부동산원 시계열을 직접 확인.",
                "나중에: 금요일 09:00에 한 번 더 보고, 그 다음엔 다음 목요일.",
                "익절/줄일 때: 없음.",
                "하지 말 것: 이 안내를 하락이나 상승으로 읽지 말 것.",
            ]),
            dry,
        )
        if not dry:
            save_json(STATE_PATH, state)
        return 0
    assert facts is not None
    if week < monday:
        ops_notify(
            state, today,
            f"[OPS] KOSPI 아파트 시나리오 주간 미게시",
            "\n".join([
                "이게 뭐 알림: KOSPI 아파트 시나리오 이번 주 조사치가 아직 없음. 매매 신호 아님.",
                f"지금 결과/상태: 시계열 마지막 주 {week}, 이번 주 월요일 {monday}. 시나리오는 바꾸지 않음.",
                "지금 할 일: 목요일 보도가 늦으면 금요일에 다시 본다.",
                "나중에: 다음 실행.",
                "익절/줄일 때: 없음.",
                "하지 말 것: 지난 주를 이번 주 변경으로 보지 말 것.",
            ]),
            dry,
        )
        if not dry:
            save_json(STATE_PATH, state)
        return 0
    if errors:
        ops_notify(
            state, today,
            "[OPS] KOSPI 아파트 시나리오 월간 수집 실패",
            "\n".join([
                "이게 뭐 알림: KOSPI 아파트 시나리오의 월간 숫자 일부를 못 읽음. 주간 판정은 직전 월간 값으로 계속.",
                "지금 결과/상태: " + " | ".join(errors),
                "지금 할 일: 한은, KDI 원문을 직접 확인.",
                "나중에: 다음 주 실행에서 다시 읽음.",
                "익절/줄일 때: 없음.",
                "하지 말 것: 못 읽은 월간 숫자를 대출 중단이나 금리 인상으로 보지 말 것.",
            ]),
            dry,
        )
    prev = state.get("scenario") or ""
    action = outcome(prev, facts["scenario"], True)
    title = title_for(prev or "없음", facts["scenario"])
    body = build_body(prev or "없음", facts, cfg["rules"], cfg["sources"]["reb_press_list"])
    check_copy(title, body)
    print(f"week={facts['week']} seoul={pct(facts['seoul'])} gyeonggi={pct(facts['gyeonggi'])} "
          f"jeonse={pct(facts['jeonse'])} seoul_4w={pct(facts['seoul_4w'])} gangnam3={pct(facts['gangnam3'])}")
    print(f"decision={facts.get('decision')} {facts.get('rate')} {facts.get('decision_date')} "
          f"mortgage={facts.get('mortgage')} {facts.get('mortgage_month')} loans={facts.get('loans')} {facts.get('loans_month')} "
          f"tx={facts.get('tx_seoul')}/{facts.get('tx_gg')} {facts.get('tx_month')}")
    print(f"scenario={facts['scenario']} prev={prev or '-'} action={action}")
    if dry:
        print("DRY RUN")
        print("ntfy=no state=no")
        if action == "send":
            print("would_send")
            print(title)
            print(append_ends(body, NAME))
        else:
            print("no notification (unchanged)")
        return 0
    if action == "send":
        pri = int((cfg.get("priority") or {}).get(facts["scenario"]) or 3)
        publish("cwally-port", title, body, priority=pri, tags=["house"], server=resolve_server(), config_name=NAME)
        state["last_sent_scenario"] = facts["scenario"]
        state["last_sent_at"] = now.isoformat()
    state["scenario"] = facts["scenario"]
    state["last_week"] = facts["week"]
    state["last_ok_iso"] = iso_week(now.date())
    state["last_fetch_day"] = today
    state["updated_at"] = now.strftime("%Y-%m-%d %H:%M KST")
    state["monthly"] = monthly
    state["last_values"] = snapshot(facts)
    save_json(STATE_PATH, state)
    print(f"saved action={action}")
    return 0


def selftest() -> int:
    rules = {
        "seoul_sale_4w_min": 0.15,
        "fall_negative_weeks": 3,
        "loan_flat_max": 0.0,
        "mortgage_cut_pp": 0.10,
        "mortgage_lookback_months": 3,
    }
    base_weeks = [
        {"date": "2026-10-19", "seoul": 0.12901158340086205, "gyeonggi": 0.21, "jeonse": 0.16, "gangnam3": -0.25},
        {"date": "2026-10-26", "seoul": 0.0903468223817594, "gyeonggi": 0.20, "jeonse": 0.17, "gangnam3": -0.32},
        {"date": "2026-11-02", "seoul": 0.07059682583461235, "gyeonggi": 0.20, "jeonse": 0.17, "gangnam3": -0.31},
        {"date": "2026-11-09", "seoul": 0.07172598058482471, "gyeonggi": 0.21317881047726583, "jeonse": 0.1661946934966796, "gangnam3": -0.2866},
    ]
    monthly = {
        "decision": {"date": "2026-10-22", "rate": 3.0, "decision": "hold", "source": "test", "link": "https://example.test/rate"},
        "loans": {"month": "2026-10", "change": 5.0, "source": "test", "link": "https://example.test/loan"},
        "mortgage": {"month": "2026-09", "rate": 4.88, "delta": 0.52, "source": "test", "link": "https://example.test/m"},
        "tx": {"month": "2026-09", "seoul_apt": 3915, "gyeonggi_housing": 14850, "source": "test", "link": "https://example.test/tx"},
    }
    calm = judge(base_weeks, monthly, rules)
    assert calm["scenario"] == SCENARIO, calm["scenario"]
    assert abs(calm["seoul_4w"] - 0.09042030305051463) < 1e-9
    assert outcome(SCENARIO, calm["scenario"], True) == "quiet"
    assert outcome(SCENARIO, FALL, True) == "send"
    assert outcome("", calm["scenario"], True) == "seed"
    assert outcome(SCENARIO, RISE, False) == "ops"

    hot = [dict(row, seoul=0.20, gyeonggi=0.10, jeonse=0.05) for row in base_weeks]
    assert judge(hot, monthly, rules)["scenario"] == RISE
    down = []
    for i, row in enumerate(base_weeks):
        down.append(dict(row, seoul=-0.04 - i * 0.01, gyeonggi=-0.02))
    assert judge(down, monthly, rules)["scenario"] == FALL
    stall = [dict(row) for row in base_weeks]
    stall[-1] = dict(stall[-1], jeonse=-0.01)
    hiked = dict(monthly)
    hiked["decision"] = dict(monthly["decision"], decision="hike", rate=3.25)
    assert judge(stall, hiked, rules)["scenario"] == FALL
    assert judge(stall, monthly, rules)["scenario"] == SCENARIO

    changes = [("2026-07-16", 2.75), ("2026-08-27", 3.0)]
    meetings = ["2026-08-27", "2026-10-22", "2026-11-26"]
    hold = latest_decision(changes, meetings, date(2026, 11, 12), 19, 18)
    assert hold == {"date": "2026-10-22", "rate": 3.0, "decision": "hold"}, hold
    hike = latest_decision(changes + [("2026-11-26", 3.25)], meetings, date(2026, 11, 26), 19, 18)
    assert hike["decision"] == "hike" and hike["rate"] == 3.25
    missed = latest_decision(changes + [("2027-03-01", 3.25)], meetings, date(2027, 3, 2), 19, 18)
    assert missed["decision"] == "hike" and missed["date"] == "2027-03-01"

    loan = parse_loan_text("가계대출(26.9월 +0.5조원\u2192 10월 +5.0조원)", 2026)
    assert loan["month"] == "2026-10" and loan["change"] == 5.0
    tx_text = (
        "서울 지역 아파트 매매거래는 3,915건으로 전월 대비 감소\n"
        "참고 7 주택 매매거래량(신고일 기준, 전체주택)\n"
        "서 울 7,318 7,884\n경 기 14,850 14,926 17,003\n"
        "참고 8 다음"
    )
    tx = parse_tx_text(tx_text, 2026, 9)
    assert tx["seoul_apt"] == 3915 and tx["gyeonggi_housing"] == 14850
    assert parse_housing_title("2026년 9월 주택통계") == (2026, 9)
    assert parse_housing_title("‘26년 8월 주택통계 및 확정") == (2026, 8)

    from openpyxl import Workbook

    book = Workbook()
    ws = book.active
    ws.title = "매매변동률"
    ws2 = book.create_sheet("전세변동률")
    header = [None, "서울", "경기", "서초", "강남", "송파"]
    for sheet in (ws, ws2):
        sheet.append(["title"])
        sheet.append(header)
    for i, row in enumerate(base_weeks):
        dt = datetime(2026, 10, 19) + timedelta(days=7 * i)
        ws.append([dt, row["seoul"], row["gyeonggi"], -0.3, -0.2, -0.1])
        ws2.append([dt, row["jeonse"], 0.1, 0.0, 0.0, 0.0])
    dest = Path(tempfile.gettempdir()) / "aptscn_selftest.xlsx"
    book.save(dest)
    regions = {"서울": "seoul", "경기": "gyeonggi", "서초": "seocho", "강남": "gangnam", "송파": "songpa"}
    fake_cfg = {"sources": {"sale_sheet": "매매변동률", "jeonse_sheet": "전세변동률", "regions": regions}}
    parsed = build_weeks(dest, fake_cfg)
    assert parsed[-1]["date"] == "2026-11-09"
    assert abs(parsed[-1]["seoul"] - base_weeks[-1]["seoul"]) < 1e-9
    assert abs(parsed[-1]["gangnam3"] - (-0.2)) < 1e-9

    body = build_body(SCENARIO, calm, rules, "https://www.reb.or.kr/reb/na/ntt/selectNttList.do?bbsId=1154&mi=9565")
    title = title_for(SCENARIO, FALL)
    check_copy(title, body)
    assert title.startswith("[KOSPI] ")
    assert "KOSPI" in body.splitlines()[0]
    for label in ("이게 뭐 알림", "지금 결과/상태", "지금 할 일", "나중에", "익절/줄일 때", "하지 말 것"):
        assert label in body
    assert "->" in title and "\u00b7" not in title
    print("selftest ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
