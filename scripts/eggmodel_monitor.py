#!/usr/bin/env python3
"""[KOSPI] 달걀모형 단계. 판정이 바뀔 때만 cwally-port. 2027-01-14 최종 요약 1번.

목요일 19:00 k203780. 그 주 성공이 없으면 금요일 09:00 morning, 그다음 금요일 19:00.
새 cron 없음. aptscn 의 주간 엑셀 받기를 재사용한다. aptscn 상태와 판정은 건드리지 않는다.
숫자가 없으면 판정을 바꾸지 않는다. 조회가 실패하면 cwally-ops 하루 한 번.

modes: auto | dry (출력만, 발송 없음, state 안 씀) | selftest (네트워크 없음)
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import append_ends, parse_ends_kst, publish, resolve_server  # noqa: E402

NAME = "eggmodel"
CFG_PATH = ROOT / "configs" / f"{NAME}.json"
STATE_PATH = ROOT / "state" / f"{NAME}.json"
KST = timezone(timedelta(hours=9))
TAG = "[KOSPI]"
HOLD = "4단계 유지"
REVALUE = "4단계 내 조정 재평가"
STAGE1 = "1단계 쪽 이동"
RESTAGE = "2단계와 3단계 재논의"
LABEL = {"revalue": REVALUE, "stage1": STAGE1, "restage": RESTAGE}
DISTRICT_KEYS = ("yeongtong", "suji", "giheung", "dongtan")
BANNED = ("\u00b7", "\u2014", "\u2013")
UNAVAIL = "unavailable"


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


def as_float(v) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(str(v).replace(",", "").replace("%", ""))
    except (TypeError, ValueError):
        return None


def pct(v) -> str:
    return "없음" if v is None else f"{float(v):+.2f}%"


def num(v, nd: int = 2) -> str:
    return "없음" if v is None else f"{float(v):,.{nd}f}"


def check_copy(title: str, body: str) -> None:
    blob = title + "\n" + body
    bad = [c for c in BANNED if c in blob]
    if bad:
        raise RuntimeError("banned punctuation in ntfy copy")


def past_end(cfg: dict, now: datetime) -> bool:
    try:
        base, date_only = parse_ends_kst(cfg.get("ends_kst"))
    except (TypeError, ValueError):
        return False
    if base is None:
        return False
    if date_only:
        return now >= base + timedelta(days=1)
    try:
        grace = int(cfg.get("ends_grace_min", 59))
    except (TypeError, ValueError):
        grace = 59
    return now > base + timedelta(minutes=max(grace, 0))


def should_run(now: datetime, job: str, state: dict, mode: str) -> bool:
    if mode == "dry" or job == NAME:
        return True
    if state.get("final_sent"):
        return False
    iso = iso_week(now.date())
    wd = now.weekday()
    done = state.get("last_ok_iso") == iso
    today = now.date().isoformat()
    if job == "k203780" and wd == 3:
        return not (done and state.get("last_fetch_day") == today)
    if job == "k203780" and wd == 4:
        return not done
    if job == "morning" and wd == 4:
        return not done
    if job in ("k203780", "morning"):
        return False
    if wd == 3 and now.hour >= 16:
        return not (done and state.get("last_fetch_day") == today)
    if wd == 4 and now.hour < 12:
        return not done
    if wd == 4 and now.hour >= 16:
        return not done
    return False


# ---------------- REB weekly sheet (download reused from aptscn) ----------------
def nearest_parent(labels: list[str], i: int) -> str:
    j = i
    while j >= 0:
        if j < len(labels) and labels[j]:
            return labels[j]
        j -= 1
    return ""


def find_col(main: list[str], sub: list[str], names: list[str], parent: str | None) -> tuple[int, str]:
    cands = []
    n = max(len(main), len(sub))
    for i in range(n):
        top = main[i] if i < len(main) else ""
        bot = sub[i] if i < len(sub) else ""
        par = nearest_parent(main, i)
        for rank, name in enumerate(names):
            if bot == name:
                penalty = 0 if (not parent or par == parent) else 1
                cands.append((0, rank, penalty, i, name))
            elif top == name and bot == "":
                penalty = 0 if (not parent or top == parent or par == parent) else 1
                cands.append((1, rank, penalty, i, name))
    if not cands:
        raise RuntimeError(f"column missing {names[0]}")
    cands.sort()
    _row, _rank, _pen, idx, name = cands[0]
    return idx, name


def find_seoul(main: list[str], districts: list[str], skip: list[str]) -> tuple[int, list[tuple[str, int]]]:
    allow_skip = set(skip)
    wanted = list(districts)
    hits = []
    for i, lab in enumerate(main):
        if lab != "서울":
            continue
        found: list[tuple[str, int]] = []
        for j in range(i + 1, len(main)):
            name = main[j]
            if name == "" or name in allow_skip:
                continue
            if name in wanted:
                found.append((name, j))
                continue
            break
        hits.append((len(found), i, found))
    if not hits:
        raise RuntimeError("서울 column missing")
    hits.sort(key=lambda item: item[0], reverse=True)
    _count, idx, found = hits[0]
    have = {name for name, _col in found}
    missing = [name for name in wanted if name not in have]
    if missing:
        raise RuntimeError("서울 구 column missing " + ",".join(missing))
    return idx, found


def sheet_table(path: Path, sheet: str) -> tuple[list, list, list]:
    from openpyxl import load_workbook

    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        ws = wb[sheet]
        rows = ws.iter_rows(values_only=True)
        header = None
        sub = None
        for _ in range(6):
            row = next(rows)
            labels = [norm_cell(c) for c in row]
            if "서울" in labels and "경기" in labels:
                header = list(row)
                sub = list(next(rows))
                break
        if header is None or sub is None:
            raise RuntimeError(f"{sheet} header missing")
        data = []
        for row in rows:
            dt = row[0] if row else None
            if hasattr(dt, "strftime"):
                data.append(row)
    finally:
        wb.close()
    if len(data) < 1:
        raise RuntimeError(f"{sheet} rows 0")
    return header, sub, data


def parse_sale(path: Path, cfg: dict) -> tuple[list[dict], dict]:
    src = cfg["sources"]
    header, sub, data = sheet_table(path, src["sale_sheet"])
    main = [norm_cell(c) for c in header]
    sub_l = [norm_cell(c) for c in sub]
    cols: dict[str, dict] = {}
    for key, spec in (cfg.get("districts") or {}).items():
        idx, name = find_col(main, sub_l, list(spec.get("names") or []), spec.get("parent"))
        cols[key] = {"col": idx, "name": name, "label": spec.get("label") or name}
    seoul_i, gus = find_seoul(main, list(cfg.get("seoul_districts") or []), list(cfg.get("seoul_skip") or []))
    weeks = []
    for row in data:
        rec: dict = {"date": row[0].strftime("%Y-%m-%d"), "source": "reb"}
        missing = False
        for key, spec in cols.items():
            val = as_float(row[spec["col"]] if spec["col"] < len(row) else None)
            if val is None:
                missing = True
                break
            rec[key] = val
        if missing:
            continue
        rec["dongtan_name"] = cols["dongtan"]["name"]
        rec["seoul"] = as_float(row[seoul_i] if seoul_i < len(row) else None)
        down = 0
        down_missing = False
        for _name, col in gus:
            val = as_float(row[col] if col < len(row) else None)
            if val is None:
                down_missing = True
                break
            if val < 0:
                down += 1
        rec["seoul_down"] = None if down_missing else down
        weeks.append(rec)
    if not weeks:
        raise RuntimeError("sale weeks empty")
    weeks.sort(key=lambda r: r["date"])
    meta = {"cols": cols, "seoul_col": seoul_i, "seoul_gu": len(gus)}
    return weeks, meta


def fetch_reb(cfg: dict) -> tuple[list[dict], dict]:
    import aptscn_monitor

    path, _posted = aptscn_monitor.download_weekly(cfg)
    return parse_sale(path, cfg)


def fetch_kospi(cfg: dict) -> tuple[list[tuple[str, float]], str]:
    """Naver index daily closes, oldest first. pageSize above 60 returns HTTP 400, so page."""
    import naver_api

    src = cfg.get("sources") or {}
    size = int(src.get("kospi_page_size") or 40)
    pages = int(src.get("kospi_pages") or 2)
    out: dict[str, float] = {}
    for page in range(1, pages + 1):
        try:
            raw = naver_api.get_json(
                f"https://m.stock.naver.com/api/index/KOSPI/price?pageSize={size}&page={page}"
            )
        except Exception as exc:
            if page == 1:
                raise
            print(f"kospi page {page} skip: {exc}")
            break
        if not isinstance(raw, list) or not raw:
            if page == 1:
                raise RuntimeError("kospi empty")
            break
        for row in raw:
            if not isinstance(row, dict):
                continue
            day = str(row.get("localTradedAt") or "")[:10]
            close = naver_api.num(row.get("closePrice"))
            if day and close is not None:
                out[day] = float(close)
    rows = sorted(out.items())
    if len(rows) < 22:
        raise RuntimeError(f"kospi bars {len(rows)}")
    return rows, "naver"


def fetch_ktb(cfg: dict) -> dict:
    import naver_api

    src = cfg.get("sources") or {}
    k3, d3 = naver_api.bond_close(src.get("ktb3_code") or "IRR_GOVT03Y")
    k10, d10 = naver_api.bond_close(src.get("ktb10_code") or "IRR_GOVT10Y")
    if k3 is None and k10 is None:
        raise RuntimeError("ktb empty")
    return {"ktb3": k3, "ktb3_date": d3, "ktb10": k10, "ktb10_date": d10, "source": "naver"}


# ---------------- rules ----------------
def consecutive_tail(weeks: list[dict]) -> list[dict]:
    ordered = sorted((w for w in weeks if w.get("date")), key=lambda w: w["date"])
    if not ordered:
        return []
    tail = [ordered[-1]]
    for older in reversed(ordered[:-1]):
        newer = date.fromisoformat(tail[0]["date"])
        prev = date.fromisoformat(older["date"])
        if newer - prev != timedelta(days=7):
            break
        tail.insert(0, older)
    return tail


def week_counts(week: dict, below_line: float) -> tuple[int, int] | None:
    vals = []
    for key in DISTRICT_KEYS:
        raw = week.get(key)
        if raw is None:
            return None
        vals.append(float(raw))
    below = sum(v < below_line for v in vals)
    neg = sum(v < 0 for v in vals)
    return below, neg


def housing_unavailable() -> dict:
    return {
        "slowdown": UNAVAIL, "warning": UNAVAIL, "exit": UNAVAIL,
        "below": None, "neg": None, "streak_slow": 0, "streak_neg": 0,
    }


def housing_rules(weeks: list[dict], rules: dict) -> dict:
    tail = consecutive_tail(weeks)
    if not tail:
        return housing_unavailable()
    line = float(rules["slow_below"])
    latest = week_counts(tail[-1], line)
    if latest is None:
        return housing_unavailable()
    below, neg = latest
    slow_need = int(rules["slow_min_districts"])
    exit_need = int(rules["exit_min_districts"])

    def streak(pred) -> int:
        n = 0
        for week in reversed(tail):
            counts = week_counts(week, line)
            if counts is None or not pred(counts):
                break
            n += 1
        return n

    streak_slow = streak(lambda c: c[0] >= slow_need)
    streak_neg = streak(lambda c: c[1] >= exit_need)
    return {
        "slowdown": streak_slow >= int(rules["slow_weeks"]),
        "warning": neg >= 1,
        "exit": streak_neg >= int(rules["exit_weeks"]),
        "below": below,
        "neg": neg,
        "streak_slow": streak_slow,
        "streak_neg": streak_neg,
    }


def kospi_unavailable() -> dict:
    return {
        "trend": UNAVAIL, "strong": UNAVAIL, "failure": UNAVAIL, "above3": UNAVAIL,
        "close": None, "date": None, "sma": None, "pct": None, "min_close": None, "min_date": None,
    }


def kospi_rules(closes: list[tuple[str, float]], rules: dict, remembered: dict | None = None) -> dict:
    out = kospi_unavailable()
    if not closes:
        return out
    low = float(rules["kospi_low"])
    since = str(rules["kospi_low_date"])
    ma_n = int(rules["ma_days"])
    ma_k = int(rules["ma_streak"])
    close_d, close = closes[-1]
    out["close"] = close
    out["date"] = close_d
    out["failure"] = close < low
    out["pct"] = (close / low - 1.0) * 100.0
    out["strong"] = out["pct"] >= float(rules["strong_pct"])
    window = [(d, c) for d, c in closes if d >= since]
    if window:
        dmin, cmin = min(window, key=lambda item: item[1])
        if closes[0][0] > since and remembered and remembered.get("low_close") is not None:
            if float(remembered["low_close"]) < cmin:
                dmin = str(remembered.get("low_date") or since)
                cmin = float(remembered["low_close"])
        out["min_close"] = cmin
        out["min_date"] = dmin
    elif remembered and remembered.get("low_close") is not None:
        out["min_close"] = float(remembered["low_close"])
        out["min_date"] = remembered.get("low_date")
    if len(closes) >= ma_n:
        out["sma"] = sum(c for _d, c in closes[-ma_n:]) / ma_n
    if len(closes) >= ma_n + ma_k - 1:
        flags = []
        for i in range(len(closes) - ma_k, len(closes)):
            chunk = [c for _d, c in closes[i - ma_n + 1:i + 1]]
            flags.append(closes[i][1] > (sum(chunk) / ma_n))
        out["above3"] = all(flags)
    pct_hit = out["pct"] >= float(rules["rebound_pct"])
    if out["above3"] == UNAVAIL and not pct_hit:
        out["trend"] = UNAVAIL
    else:
        out["trend"] = bool(out["above3"] is True or pct_hit)
    return out


def _fade_streak(checks: list[dict], line: float) -> int:
    ordered = sorted(
        (c for c in checks if c.get("ktb3") is not None and c.get("date")),
        key=lambda c: c["date"],
    )
    streak = 0
    prev = None
    for row in reversed(ordered):
        if float(row["ktb3"]) >= line:
            break
        if prev is not None:
            gap = (date.fromisoformat(prev) - date.fromisoformat(row["date"])).days
            if gap < 5 or gap > 10:
                break
        streak += 1
        prev = row["date"]
    return streak


def ktb_rules(current: float | None, checks: list[dict], rules: dict) -> dict:
    if current is None:
        return {"fade": UNAVAIL, "top": UNAVAIL, "streak": 0}
    line = float(rules["ktb3_fade"])
    top = float(current) >= float(rules["ktb3_top"])
    streak = _fade_streak(checks, line)
    return {"fade": streak >= int(rules["ktb3_fade_weeks"]), "top": top, "streak": streak}


def _tri_and(a, b):
    if a is False or b is False:
        return False
    if a == UNAVAIL or b == UNAVAIL:
        return UNAVAIL
    return True


def _not_flag(flag):
    if flag == UNAVAIL:
        return UNAVAIL
    return not flag


def decide(prev: str, housing: dict, kospi: dict, ktb: dict) -> tuple[str, str, dict]:
    flags = {
        "revalue": _tri_and(kospi.get("trend"), _not_flag(housing.get("slowdown"))),
        "stage1": _tri_and(housing.get("exit"), kospi.get("failure")),
        "restage": _tri_and(ktb.get("fade"), housing.get("slowdown")),
    }
    confirmed = [key for key, val in flags.items() if val is True]
    unknown = [key for key, val in flags.items() if val == UNAVAIL]
    if len(confirmed) >= 2:
        return prev or HOLD, "mixed", flags
    if len(confirmed) == 1 and not unknown:
        return LABEL[confirmed[0]], "rule", flags
    if unknown:
        return prev or HOLD, "missing", flags
    return HOLD, "default", flags


def select_action(prev: str, judgment: str, why: str, *, final_due: bool, last_chance: bool, data_complete: bool) -> str:
    changed = judgment != prev and why in {"default", "rule"}
    if final_due and (data_complete or last_chance):
        return "final"
    if changed:
        return "send"
    return "quiet"


def upsert_week(weeks: list[dict], week: dict) -> list[dict]:
    out = [row for row in weeks if row.get("date") != week.get("date")]
    out.append(week)
    out.sort(key=lambda row: row.get("date") or "")
    return out[-8:]


def upsert_ktb(checks: list[dict], rec: dict) -> list[dict]:
    out = [row for row in checks if row.get("week") != rec.get("week")]
    out.append(rec)
    out.sort(key=lambda row: row.get("date") or row.get("week") or "")
    return out[-8:]


# ---------------- copy ----------------
RULE_KO = {
    "slowdown": "주택 둔화(4곳 중 3곳 이상이 +0.20% 미만, 2주 연속)",
    "warning": "주택 경고(한 곳이라도 마이너스)",
    "exit": "하락 확인(4곳 중 3곳 이상이 마이너스, 3주 연속)",
    "trend": "KOSPI 추세 반등(20일선 위 3거래일 또는 저점 대비 +10%)",
    "strong": "KOSPI 강한 반등(저점 대비 +20%)",
    "failure": "KOSPI 반등 실패(종가가 기준 저점 아래)",
    "fade": "국고 3년 인상 기대 약화(3.25% 미만 2주)",
    "top": "국고 3년 3.75% 이상",
}
DO = {
    HOLD: "비중을 바꾸지 말 것. 다음 목요일에 판정이 같은지만 본다.",
    REVALUE: "4단계 안에서 조정 가능성을 다시 적는다. 주문은 직접 판단.",
    STAGE1: "주택 하락 확인과 KOSPI 저점 이탈이 같이 나왔다. 추가 매수는 보류하고 직접 점검.",
    RESTAGE: "금리 인상 기대가 약해지고 주택이 둔화됐다. 2단계와 3단계 중 어디인지 직접 다시 적는다.",
}


def fired_text(housing: dict, kospi: dict, ktb: dict) -> str:
    flags = {
        "slowdown": housing.get("slowdown"),
        "warning": housing.get("warning"),
        "exit": housing.get("exit"),
        "trend": kospi.get("trend"),
        "strong": kospi.get("strong"),
        "failure": kospi.get("failure"),
        "fade": ktb.get("fade"),
        "top": ktb.get("top"),
    }
    on = [RULE_KO[key] for key, val in flags.items() if val is True]
    missing = [RULE_KO[key] for key, val in flags.items() if val == UNAVAIL]
    text = "켜진 규칙: " + ("없음" if not on else "; ".join(on))
    if missing:
        text += ". 못 읽은 규칙: " + "; ".join(missing)
    return text


def title_for(prev: str, judgment: str, final: bool) -> str:
    if final and prev == judgment:
        return f"{TAG} 달걀모형 최종 요약 | {judgment}"
    base = f"{TAG} 달걀모형 판정 변경: {prev} -> {judgment}"
    if final:
        return base + " | 최종 요약"
    return base


def build_body(prev: str, judgment: str, why: str, housing: dict, kospi: dict, ktb: dict, week: dict | None, ktb_raw: dict | None, final: bool) -> str:
    why_ko = {
        "default": "특수 조건이 없어 4단계 유지",
        "rule": "조건 하나가 맞음",
        "mixed": "신호가 겹쳐 직전 판정을 유지",
        "missing": "숫자가 빠져 직전 판정을 유지",
    }.get(why, why)
    if week:
        areas = " ".join(
            f"{ {'yeongtong': '영통', 'suji': '수지', 'giheung': '기흥', 'dongtan': week.get('dongtan_name') or '동탄'}[key] } {pct(week.get(key))}"
            for key in DISTRICT_KEYS
        )
        seoul = f"서울 {pct(week.get('seoul'))}, 서울 하락 구 {week.get('seoul_down') if week.get('seoul_down') is not None else '없음'}곳 ({week.get('date')})"
    else:
        areas = "영통 없음, 수지 없음, 기흥 없음, 동탄 없음"
        seoul = "서울 없음"
    if kospi.get("close") is None:
        kospi_txt = "KOSPI 없음"
    else:
        extra = ""
        if kospi.get("min_close") is not None:
            extra = f", 이후 최저 {num(kospi.get('min_close'))} ({kospi.get('min_date') or '날짜 없음'})"
        kospi_txt = (
            f"KOSPI 종가 {num(kospi.get('close'))}, 20일선 {num(kospi.get('sma'))}, "
            f"기준 저점 대비 {pct(kospi.get('pct'))}{extra} ({kospi.get('date')})"
        )
    if not ktb_raw or ktb_raw.get("ktb3") is None:
        ktb_txt = "국고 3년 없음"
        if ktb_raw and ktb_raw.get("ktb10") is not None:
            ktb_txt += f", 10년 {num(ktb_raw.get('ktb10'), 3)}%"
    else:
        ktb_txt = (
            f"국고 3년 {num(ktb_raw.get('ktb3'), 3)}%, 10년 {num(ktb_raw.get('ktb10'), 3)}% "
            f"({ktb_raw.get('ktb3_date') or ktb_raw.get('date') or '날짜 없음'})"
        )
    strengthen = ""
    if judgment == STAGE1 and ktb.get("top") is True:
        strengthen = " 국고 3년 3.75% 이상이 1단계 쪽을 보강한다."
    elif judgment == STAGE1 and ktb.get("top") == UNAVAIL:
        strengthen = " 국고 3년 보강 여부는 없다."
    later = "판정이 같으면 알림은 없다. 다음 확인은 목요일 19:00. 그 주 숫자가 없으면 금요일 09:00과 금요일 19:00에 한 번 더 본다. 2027-01-14 금통위 실행에서 최종 요약을 1번 보내고 끝난다."
    if final:
        later = "이번이 마지막 발송이다. " + later
    lines = [
        "이게 뭐 알림: KOSPI 달걀모형으로 경기 남부(광교에서 동탄) 단계가 바뀌었는지 보는 주간 점검. 매매 신호 아님.",
        f"지금 결과/상태: 이전 {prev} -> 지금 {judgment}. {why_ko}. {fired_text(housing, kospi, ktb)}. "
        f"{areas}. {seoul}. {kospi_txt}. {ktb_txt}.{strengthen}",
        f"지금 할 일: {DO.get(judgment, DO[HOLD])}",
        f"나중에: {later}",
        "익절/줄일 때: 이 알림은 매도 시점이 아니다. 보유를 이 문장만 보고 줄이지 말 것.",
        "하지 말 것: 단계 이름만 보고 매수하거나 매도하지 말 것. 못 읽은 숫자를 채워 넣지 말 것. 추격, 신용, 미수 금지.",
    ]
    return "\n".join(lines)


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


def scrub(text: str) -> str:
    for ch in BANNED:
        text = text.replace(ch, " ")
    return text


def flag_txt(val) -> str:
    if val == UNAVAIL:
        return UNAVAIL
    return "yes" if val else "no"


# ---------------- run ----------------
def evaluate(state: dict, rules: dict, weeks: list[dict] | None, closes, ktb: dict | None) -> dict:
    housing_ok = bool(weeks)
    housing = housing_rules(weeks, rules) if housing_ok else housing_unavailable()
    kospi = kospi_rules(closes or [], rules, state.get("kospi") if isinstance(state.get("kospi"), dict) else None)
    current = None if not ktb else ktb.get("ktb3")
    checks = list(state.get("ktb_checks") or [])
    if current is not None and ktb is not None:
        checks = upsert_ktb(checks, {
            "week": ktb["week"],
            "date": ktb.get("ktb3_date") or ktb.get("date"),
            "ktb3": current,
            "ktb10": ktb.get("ktb10"),
        })
    ktb_flag = ktb_rules(current, checks, rules)
    prev = state.get("judgment") or HOLD
    judgment, why, flags = decide(prev, housing, kospi, ktb_flag)
    return {
        "prev": prev,
        "judgment": judgment,
        "why": why,
        "flags": flags,
        "housing": housing,
        "kospi": kospi,
        "ktb": ktb_flag,
        "ktb_checks": checks,
        "week": weeks[-1] if weeks else None,
    }


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    if mode == "selftest":
        return selftest()
    if mode not in {"auto", "dry"}:
        print("usage: eggmodel_monitor.py auto|dry|selftest", file=sys.stderr)
        return 2
    cfg = load_json(CFG_PATH, {})
    state = load_json(STATE_PATH, {})
    now = datetime.now(KST)
    job = os.environ.get("PICK_JOB") or ""
    dry = mode == "dry"
    if past_end(cfg, now):
        print(f"eggmodel no-op: ended {cfg.get('ends_kst')}")
        return 0
    if state.get("final_sent") and not dry:
        print("eggmodel no-op: final summary already sent")
        return 0
    if not should_run(now, job, state, mode):
        print(f"eggmodel skip job={job or '-'} {now:%Y-%m-%d %H:%M} dow={now.weekday()}")
        return 0
    rules = cfg.get("rules") or {}
    today = now.date().isoformat()
    monday = week_monday(now.date()).isoformat()
    errors: list[str] = []
    reb_weeks = None
    meta = {}
    try:
        reb_weeks, meta = fetch_reb(cfg)
    except Exception as exc:
        errors.append(f"reb {type(exc).__name__}: {exc}")
    closes = None
    kospi_src = UNAVAIL
    try:
        closes, kospi_src = fetch_kospi(cfg)
    except Exception as exc:
        errors.append(f"kospi {type(exc).__name__}: {exc}")
    ktb_raw = None
    try:
        ktb_raw = fetch_ktb(cfg)
    except Exception as exc:
        errors.append(f"ktb {type(exc).__name__}: {exc}")

    fresh = None
    stale = False
    if reb_weeks:
        latest = reb_weeks[-1]
        if latest["date"] < monday:
            stale = True
            errors.append(f"reb stale week {latest['date']} monday {monday}")
        else:
            fresh = latest
    merged = list(state.get("weeks") or [])
    if fresh:
        merged = upsert_week(merged, fresh)
    ktb_for_eval = None
    if ktb_raw and ktb_raw.get("ktb3") is not None:
        ktb_for_eval = dict(ktb_raw)
        ktb_for_eval["week"] = iso_week(now.date())
        ktb_for_eval["date"] = ktb_raw.get("ktb3_date") or today
    elif ktb_raw:
        errors.append("ktb3 unavailable")

    use_weeks = merged if fresh else None
    result = evaluate(state, rules, use_weeks, closes, ktb_for_eval)
    final_on = date.fromisoformat(str(rules.get("final_on") or "2027-01-14"))
    final_due = now.date() >= final_on
    last_chance = now.date() > final_on
    data_complete = bool(fresh) and closes is not None and ktb_for_eval is not None and result["kospi"].get("close") is not None
    action = select_action(
        result["prev"], result["judgment"], result["why"],
        final_due=final_due, last_chance=last_chance, data_complete=data_complete,
    )
    final = action == "final"
    notify = action in {"send", "final"}
    title = title_for(result["prev"], result["judgment"], final)
    body = build_body(
        result["prev"], result["judgment"], result["why"],
        result["housing"], result["kospi"], result["ktb"], result["week"], ktb_raw, final,
    )
    check_copy(title, body)
    cols = (meta.get("cols") or {})
    col_txt = " ".join(f"{key}={spec.get('col')}:{spec.get('name')}" for key, spec in cols.items())
    print(f"sources reb={'ok' if fresh else ('stale' if stale else UNAVAIL)} kospi={kospi_src} ktb={ktb_raw.get('source') if ktb_raw else UNAVAIL}")
    if col_txt:
        print(f"cols {col_txt} seoul_gu={meta.get('seoul_gu')}")
    week = result["week"] or {}
    print(
        f"week={week.get('date') or '-'} yeongtong={pct(week.get('yeongtong'))} suji={pct(week.get('suji'))} "
        f"giheung={pct(week.get('giheung'))} dongtan={pct(week.get('dongtan'))} "
        f"seoul={pct(week.get('seoul'))} seoul_down={week.get('seoul_down')}"
    )
    k = result["kospi"]
    print(f"kospi close={num(k.get('close'))} sma={num(k.get('sma'))} pct={pct(k.get('pct'))} above3={flag_txt(k.get('above3'))} min={num(k.get('min_close'))}")
    print(f"ktb3={num((ktb_raw or {}).get('ktb3'), 3)} ktb10={num((ktb_raw or {}).get('ktb10'), 3)} date={(ktb_raw or {}).get('ktb3_date')}")
    h, t = result["housing"], result["ktb"]
    print(
        "rules "
        f"slowdown={flag_txt(h.get('slowdown'))} warning={flag_txt(h.get('warning'))} exit={flag_txt(h.get('exit'))} "
        f"trend={flag_txt(k.get('trend'))} strong={flag_txt(k.get('strong'))} failure={flag_txt(k.get('failure'))} "
        f"fade={flag_txt(t.get('fade'))} top={flag_txt(t.get('top'))}"
    )
    print(f"judgment={result['judgment']} prev={result['prev']} why={result['why']} action={action} final_due={final_due} complete={data_complete}")
    if errors:
        kind = "주간 미게시" if stale and not any(e.startswith("reb ") and "stale" not in e for e in errors) and fresh is None and reb_weeks else "수집 실패"
        if stale and reb_weeks and not any(e.startswith("reb ") and "stale" not in e for e in errors):
            kind = "주간 미게시"
        if any(e.startswith("reb ") and "stale" not in e for e in errors):
            kind = "수집 실패"
        ops_notify(
            state, today,
            f"[OPS] {TAG[1:-1]} 달걀모형 {kind}",
            "\n".join([
                "이게 뭐 알림: KOSPI 달걀모형 주간 숫자 일부를 못 읽음. 매매 신호 아님.",
                f"지금 결과/상태: 판정은 바꾸지 않음. 직전 {state.get('judgment') or '없음'}. " + " | ".join(scrub(e) for e in errors),
                "지금 할 일: 부동산원 시계열, KOSPI 종가, 국고 3년과 10년을 직접 확인.",
                "나중에: 금요일에 한 번 더 보고, 그 다음엔 다음 목요일.",
                "익절/줄일 때: 없음.",
                "하지 말 것: 이 안내를 단계 변경으로 읽지 말 것.",
            ]),
            dry,
        )
    if dry:
        print("DRY RUN")
        print("ntfy=no state=no")
        if notify:
            print("would_send")
            print(title)
            print(append_ends(body, NAME))
        else:
            print("no notification (unchanged)")
        return 0
    if fresh:
        state["weeks"] = merged
    if closes:
        state["kospi"] = {
            "date": result["kospi"].get("date"),
            "close": result["kospi"].get("close"),
            "sma": result["kospi"].get("sma"),
            "pct": result["kospi"].get("pct"),
            "low_date": result["kospi"].get("min_date") or (state.get("kospi") or {}).get("low_date"),
            "low_close": result["kospi"].get("min_close") if result["kospi"].get("min_close") is not None else (state.get("kospi") or {}).get("low_close"),
            "source": kospi_src,
        }
    if ktb_for_eval:
        state["ktb_checks"] = result["ktb_checks"]
    if result["why"] != "missing":
        state["judgment"] = result["judgment"]
    state["last_rules"] = {
        "why": result["why"],
        "flags": result["flags"],
        "housing": {key: result["housing"].get(key) for key in ("slowdown", "warning", "exit", "below", "neg", "streak_slow", "streak_neg")},
        "kospi": {key: result["kospi"].get(key) for key in ("trend", "strong", "failure", "above3", "close", "date", "sma", "pct")},
        "ktb": result["ktb"],
    }
    state["sources"] = {
        "reb": "ok" if fresh else ("stale" if stale else UNAVAIL),
        "kospi": kospi_src,
        "ktb": (ktb_raw or {}).get("source") or UNAVAIL,
        "ktb3": "ok" if ktb_raw and ktb_raw.get("ktb3") is not None else UNAVAIL,
        "ktb10": "ok" if ktb_raw and ktb_raw.get("ktb10") is not None else UNAVAIL,
    }
    state["updated_at"] = now.strftime("%Y-%m-%d %H:%M KST")
    if data_complete:
        state["last_ok_iso"] = iso_week(now.date())
        state["last_fetch_day"] = today
    if notify:
        pri = int((cfg.get("priority") or {}).get(result["judgment"]) or 3)
        publish("cwally-port", title, body, priority=pri, tags=["house"], server=resolve_server(), config_name=NAME)
        state["last_sent_judgment"] = result["judgment"]
        state["last_sent_at"] = now.isoformat()
        if final:
            state["final_sent"] = True
            state["last_ok_iso"] = iso_week(now.date())
            state["last_fetch_day"] = today
    save_json(STATE_PATH, state)
    print(f"saved action={action}")
    return 0


def selftest() -> int:
    rules = {
        "slow_below": 0.20, "slow_min_districts": 3, "slow_weeks": 2,
        "exit_weeks": 3, "exit_min_districts": 3,
        "kospi_low": 4697.51, "kospi_low_date": "2026-11-25",
        "rebound_pct": 10.0, "strong_pct": 20.0, "ma_days": 20, "ma_streak": 3,
        "ktb3_fade": 3.25, "ktb3_fade_weeks": 2, "ktb3_top": 3.75,
        "final_on": "2027-01-14",
    }
    assert iso_week(date(2026, 11, 27)) == "2026-W48"
    base = {"date": "2026-11-23", "yeongtong": 0.42, "suji": 0.35, "giheung": 0.52, "dongtan": 0.50, "seoul": 0.01, "seoul_down": 9}
    calm = housing_rules([base], rules)
    assert calm["slowdown"] is False and calm["warning"] is False and calm["exit"] is False, calm

    slow_weeks = [
        {"date": "2026-11-16", "yeongtong": 0.10, "suji": 0.10, "giheung": 0.10, "dongtan": 0.40},
        {"date": "2026-11-23", "yeongtong": 0.05, "suji": 0.19, "giheung": 0.00, "dongtan": 0.30},
    ]
    slow = housing_rules(slow_weeks, rules)
    assert slow["slowdown"] is True and slow["warning"] is False and slow["exit"] is False, slow
    gapped = [dict(slow_weeks[0], date="2026-11-09"), slow_weeks[1]]
    assert housing_rules(gapped, rules)["slowdown"] is False

    exit_weeks = []
    for i, day in enumerate(("2026-11-09", "2026-11-16", "2026-11-23")):
        exit_weeks.append({"date": day, "yeongtong": -0.10 - i * 0.01, "suji": -0.05, "giheung": -0.02, "dongtan": 0.10})
    fallen = housing_rules(exit_weeks, rules)
    assert fallen["exit"] is True and fallen["slowdown"] is True and fallen["warning"] is True, fallen

    start = date(2026, 10, 1)
    flat = [((start + timedelta(days=i)).isoformat(), 4800.0) for i in range(25)]
    quiet_k = kospi_rules(flat, rules)
    assert quiet_k["trend"] is False and quiet_k["failure"] is False and quiet_k["strong"] is False, quiet_k
    climb = [((start + timedelta(days=i)).isoformat(), 4500.0) for i in range(22)]
    for j, px in enumerate((5000.0, 5000.0, 5000.0)):
        climb.append(((start + timedelta(days=22 + j)).isoformat(), px))
    up = kospi_rules(climb, rules)
    assert up["trend"] is True and up["strong"] is False and up["failure"] is False, up
    hot = list(climb)
    hot[-1] = (hot[-1][0], 5700.0)
    assert kospi_rules(hot, rules)["strong"] is True
    broken = list(flat)
    broken[-1] = (broken[-1][0], 4600.0)
    bad = kospi_rules(broken, rules)
    assert bad["failure"] is True and bad["trend"] is False, bad

    none_k = ktb_rules(None, [], rules)
    assert none_k["fade"] == UNAVAIL and none_k["top"] == UNAVAIL
    one = ktb_rules(3.10, [{"week": "2026-W48", "date": "2026-11-27", "ktb3": 3.10}], rules)
    assert one["fade"] is False and one["top"] is False, one
    two = [
        {"week": "2026-W47", "date": "2026-11-20", "ktb3": 3.10},
        {"week": "2026-W48", "date": "2026-11-27", "ktb3": 3.20},
    ]
    faded = ktb_rules(3.20, two, rules)
    assert faded["fade"] is True and faded["top"] is False, faded
    topped = ktb_rules(3.80, [{"week": "2026-W48", "date": "2026-11-27", "ktb3": 3.80}], rules)
    assert topped["top"] is True and topped["fade"] is False

    hold_j, why, _flags = decide(HOLD, calm, quiet_k, topped)
    assert (hold_j, why) == (HOLD, "default"), (hold_j, why)
    re_j, re_why, _flags = decide(HOLD, calm, up, one)
    assert (re_j, re_why) == (REVALUE, "rule"), (re_j, re_why)
    s_j, s_why, _flags = decide(HOLD, fallen, bad, topped)
    assert (s_j, s_why) == (STAGE1, "rule"), (s_j, s_why)
    r_j, r_why, _flags = decide(HOLD, slow, quiet_k, faded)
    assert (r_j, r_why) == (RESTAGE, "rule"), (r_j, r_why)
    mixed_j, mixed_why, _flags = decide(REVALUE, fallen, bad, faded)
    assert mixed_j == REVALUE and mixed_why == "mixed", (mixed_j, mixed_why)
    miss_j, miss_why, _flags = decide(HOLD, housing_unavailable(), up, one)
    assert miss_j == HOLD and miss_why == "missing", (miss_j, miss_why)
    back_j, back_why, _flags = decide(REVALUE, calm, quiet_k, one)
    assert (back_j, back_why) == (HOLD, "default"), (back_j, back_why)

    assert select_action(HOLD, HOLD, "default", final_due=False, last_chance=False, data_complete=True) == "quiet"
    assert select_action(HOLD, REVALUE, "rule", final_due=False, last_chance=False, data_complete=True) == "send"
    assert select_action(HOLD, HOLD, "missing", final_due=False, last_chance=False, data_complete=False) == "quiet"
    assert select_action(HOLD, HOLD, "default", final_due=True, last_chance=False, data_complete=True) == "final"
    assert select_action(HOLD, HOLD, "missing", final_due=True, last_chance=False, data_complete=False) == "quiet"
    assert select_action(HOLD, HOLD, "missing", final_due=True, last_chance=True, data_complete=False) == "final"

    title = title_for(HOLD, REVALUE, False)
    body = build_body(HOLD, REVALUE, "rule", calm, up, one, base, {"ktb3": 3.10, "ktb10": 3.80, "ktb3_date": "2026-11-27"}, False)
    check_copy(title, body)
    assert title.startswith("[KOSPI] ") and "KOSPI" in title and "->" in title
    assert "\u00b7" not in title and "\u00b7" not in body
    assert "2단계와 3단계" in title_for(HOLD, RESTAGE, False)
    for label in ("이게 뭐 알림", "지금 결과/상태", "지금 할 일", "나중에", "익절/줄일 때", "하지 말 것"):
        assert label in body, label
    assert "영통" in body and "수지" in body and "기흥" in body and "동탄" in body
    final_title = title_for(HOLD, HOLD, True)
    assert final_title == "[KOSPI] 달걀모형 최종 요약 | 4단계 유지"

    from openpyxl import Workbook

    book = Workbook()
    ws = book.active
    ws.title = "매매변동률"
    ws.append(["1.주간"])
    main = [None] * 30
    sub = [None] * 30
    main[0] = "지역날짜"
    main[12] = "서울"
    main[13] = "도심권"
    main[14] = "종로"
    main[15] = "중"
    main[16] = "용산"
    main[17] = "경기"
    main[18] = "용인"
    sub[19] = "기흥"
    sub[20] = "수지"
    main[21] = "수원"
    sub[22] = "영통"
    main[23] = "화성"
    main[24] = "화성"
    sub[25] = "동탄"
    ws.append(main)
    ws.append(sub)
    ws.append(["English"])
    day = datetime(2026, 11, 23)
    row = [None] * 30
    row[0] = day
    row[12] = 0.01
    row[14] = -0.03
    row[15] = 0.02
    row[16] = -0.01
    row[19] = 0.52
    row[20] = 0.35
    row[22] = 0.42
    row[24] = 9.99
    row[25] = 0.50
    ws.append(row)
    dest = Path(__import__("tempfile").gettempdir()) / "eggmodel_selftest.xlsx"
    book.save(dest)
    fake = {
        "sources": {"sale_sheet": "매매변동률"},
        "districts": load_json(CFG_PATH, {}).get("districts"),
        "seoul_districts": ["종로", "중", "용산"],
        "seoul_skip": ["도심권"],
    }
    parsed, info = parse_sale(dest, fake)
    assert parsed[-1]["date"] == "2026-11-23", parsed[-1]
    assert abs(parsed[-1]["yeongtong"] - 0.42) < 1e-9
    assert abs(parsed[-1]["dongtan"] - 0.50) < 1e-9, parsed[-1]["dongtan"]
    assert parsed[-1]["dongtan_name"] == "동탄"
    assert parsed[-1]["seoul_down"] == 2, parsed[-1]["seoul_down"]
    assert info["seoul_gu"] == 3
    assert info["cols"]["giheung"]["col"] == 19
    print("selftest ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
