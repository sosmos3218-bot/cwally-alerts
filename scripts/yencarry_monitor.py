#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
CFG_PATH = ROOT / "configs" / "yencarry.json"
STATE_PATH = ROOT / "state" / "yencarry.json"
UA = {"User-Agent": "Mozilla/5.0 cwally-alerts"}
DRY = False
# Hysteresis (approved in chat 2026-10-30, owner note pt24u): a level fires once when it is crossed and re-arms only after
# the quote moves back past the line by a buffer (원달러 10원, 달러/엔 0.5엔, 일본 30년 0.05%p). Level messages go out
# only when a NEW level enters the active set; a level leaving the set is silent. 30-day backtest (09:00/15:20 runs,
# Yahoo hourly): old rule 12 level sends (archive: 22 incl. the pre-10/19 quote-key bug) -> 2.
LEVEL_DEFS = [  # key, quote, op, config level key, label
    ("jpy152", "usdjpy", "le", "usdjpy_low", "달러/엔 {q} → 152 이탈"),
    ("jpy160", "usdjpy", "ge", "usdjpy_high", "달러/엔 {q} → 160 돌파"),
    ("jpy158", "usdjpy", "ge", "usdjpy_watch", "달러/엔 {q} → 158 돌파"),
    ("krw1340", "usdkrw", "le", "usdkrw_low", "원달러 {q}원 → 1,340 이탈"),
    ("krw1400", "usdkrw", "ge", "usdkrw_high", "원달러 {q}원 → 1,400 돌파"),
    ("jgb45", "jgb30", "ge", "jgb30_danger", "일본 30년 {q}% → 4.5 돌파"),
    ("jgb43", "jgb30", "ge", "jgb30_warn", "일본 30년 {q}% → 4.3 돌파"),
    ("jgb425", "jgb30", "ge", "jgb30_watch", "일본 30년 {q}% → 4.25 돌파"),
]
DEFAULT_BUF = {"usdkrw": 10.0, "usdjpy": 0.5, "jgb30": 0.05}
DEFAULT_LV = {"usdjpy_watch": 158, "jgb30_watch": 4.25}


def level_step(cfg: dict, active: dict | None, q: dict) -> tuple[dict, list[str], list[str]]:
    """-> (new active {key: True}, display keys for every active level, newly entered keys).
    active None = first run: init from quotes, nothing counts as new. Missing quote keeps the level as it was."""
    buf = dict(DEFAULT_BUF, **(cfg.get("hysteresis") or {}))
    lv = dict(DEFAULT_LV, **cfg["levels"])
    first = active is None
    cur = dict(active or {})
    for key, qn, op, lk, _ in LEVEL_DEFS:
        v = q.get(qn)
        if v is None:
            continue
        line, b = float(lv[lk]), float(buf[qn])
        inside = v <= line if op == "le" else v >= line
        out = v >= line + b if op == "le" else v <= line - b
        if inside:
            cur[key] = True
        elif out:
            cur.pop(key, None)
    new = [] if first else [k for k in cur if k not in (active or {})]
    nd = {"usdjpy": 1, "usdkrw": 0, "jgb30": 2}
    shown = []
    for key, qn, op, lk, label in LEVEL_DEFS:
        if key not in cur:
            continue
        if key == "jpy158" and "jpy160" in cur:
            continue  # 160 supersedes the 158 watch (old elif)
        if key in ("jgb43", "jgb425") and "jgb45" in cur or key == "jgb425" and "jgb43" in cur:
            continue
        shown.append(label.format(q=fmt(q.get(qn), nd[qn])))
    return cur, shown, new


def fetch(url: str, timeout: int = 20) -> str:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def yahoo(ticker: str) -> float | None:
    url = "https://query1.finance.yahoo.com/v8/finance/chart/" + ticker + "?interval=1d&range=5d"
    try:
        payload = json.loads(fetch(url))
        meta = payload["chart"]["result"][0].get("meta", {})
        price = meta.get("regularMarketPrice") or meta.get("previousClose")
        return float(price) if price is not None else None
    except Exception as exc:
        print(f"yahoo fail {ticker}: {exc}", file=sys.stderr)
        return None


def jgb30() -> float | None:
    urls = [
        "https://www.mof.go.jp/jgbs/reference/interest_rate/jgbcm.csv",
        "https://www.mof.go.jp/english/policy/jgbs/reference/interest_rate/historical/jgbcme.csv",
    ]
    for url in urls:
        try:
            text = fetch(url)
        except Exception as exc:
            print(f"jgb fail {url}: {exc}", file=sys.stderr)
            continue
        rows = [ln.strip() for ln in text.splitlines() if ln.strip()]
        if len(rows) < 2:
            continue
        header = [c.strip() for c in rows[0].split(",")]
        idx = None
        for i, name in enumerate(header):
            if "30" in name and ("년" in name or "年" in name or "Y" in name.upper() or name.endswith("30")):
                idx = i
        if idx is None and len(header) >= 12:
            idx = -2 if "40" in header[-1] else -1
        for line in reversed(rows[1:]):
            cols = [c.strip() for c in line.split(",")]
            if idx is None or idx >= len(cols):
                continue
            raw = cols[idx].replace("%", "")
            try:
                val = float(raw)
            except ValueError:
                continue
            if 0.1 < val < 10:
                return val
    return None


def fed_stance(day: datetime) -> str:
    ymd = day.strftime("%Y%m%d")
    url = f"https://www.federalreserve.gov/newsevents/pressreleases/monetary{ymd}a.htm"
    try:
        html = fetch(url)
    except Exception as exc:
        print(f"fed fetch fail: {exc}", file=sys.stderr)
        return "문장 없음"
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text).lower()
    if "raise the target range" in text or "decided to raise" in text:
        return "금리 인상"
    if "lower the target range" in text or "decided to lower" in text:
        return "금리 인하"
    if "maintain the target range" in text or "decided to maintain" in text:
        return "금리 동결"
    return "문장 확인 실패"


def boj_stance(day: datetime) -> str:
    ymd = day.strftime("%y%m%d")
    pages = [
        "https://www.boj.or.jp/mopo/mpmdeci/mpr_2026/index.htm",
        "https://www.boj.or.jp/en/mopo/mpmdeci/mpr_2026/index.htm",
    ]
    blob = ""
    for url in pages:
        try:
            blob += " " + fetch(url)
        except Exception as exc:
            print(f"boj fetch fail {url}: {exc}", file=sys.stderr)
    if not blob.strip():
        return "공표문 아직 없음"
    text = re.sub(r"<[^>]+>", " ", blob)
    text = re.sub(r"\s+", " ", text)
    day_ko = f"{day.month}月{day.day}日"
    if day_ko not in text and ymd not in text:
        return "공표문 아직 없음"
    if "金融市場調節方針の変更" in text:
        return "금리 변경(인상 가능)"
    if "当面の金融政策運営" in text:
        return "금리 동결 가능"
    return "공표문 확인. 인상, 동결은 본문 확인"


def ueda_summary(day: datetime) -> list[str]:
    ymd = day.strftime("%Y-%m-%d")
    feeds = [
        "https://news.google.com/rss/search?q=%E6%A4%8D%E7%94%B0+%E7%B7%8F%E8%A3%81+%E8%A8%98%E8%80%85%E4%BC%9A%E8%A6%8B&hl=ja&gl=JP&ceid=JP:ja",
        "https://news.google.com/rss/search?q=Ueda+BOJ+press+conference&hl=en-US&gl=US&ceid=US:en",
        f"https://www.reuters.com/world/asia-pacific/boj-governor-uedas-comments-news-conference-{ymd}/",
        f"https://www.reuters.com/business/finance/boj-governor-uedas-comments-news-conference-{ymd}/",
        f"https://www.reuters.com/world/japan/boj-governor-uedas-comments-news-conference-{ymd}/",
        "https://www.boj.or.jp/about/press/kaiken_2026/index.htm",
    ]
    blob = ""
    for url in feeds:
        try:
            blob += "\n" + fetch(url, timeout=12)
        except Exception as exc:
            print(f"ueda fetch fail {url}: {exc}", file=sys.stderr)
    if not blob.strip():
        return ["속보 없음. 공식 요지는 보통 다음날."]
    day_marks = (
        ymd,
        f"{day.month}月{day.day}日",
        f"{day.month}/{day.day}",
        day.strftime("%b %d"),
        day.strftime("%B %d"),
    )
    todayish = any(m.lower() in blob.lower() for m in day_marks)
    low = re.sub(r"<[^>]+>", " ", blob)
    low = re.sub(r"\s+", " ", low)
    if not todayish:
        return ["오늘자 회견 속보 아직 없음. 공식 요지는 보통 다음날."]
    hits: list[str] = []
    rules = [
        (r"利上げ|raise (the )?(policy )?rate|further hike|additional hike", "추가 인상 여지 언급"),
        (r"現状維持|keep(ing)? rates|held (steady|rates)|unchanged", "금리 동결, 현상유지 톤"),
        (r"behind the curve|後れを取|遅れをと", "늦으면 안 된다(매파) 언급"),
        (r"円安|weak(er)? yen|yen weak", "엔약세→수입물가 전가 주시"),
        (r"長期金利|long-term (interest )?rates|JGB", "장기금리 급등 시 대응 언급"),
        (r"賃金|wage", "임금, 물가 선순환 확인"),
        (r"中東|Middle East|oil|原油", "중동, 유가 리스크 언급"),
        (r"data(-| )dependent|毎回の会合|each meeting", "데이터 보고 매 회의 결정"),
    ]
    for pat, line in rules:
        if re.search(pat, low, flags=re.I) and line not in hits:
            hits.append(line)
        if len(hits) >= 3:
            break
    if not hits:
        titles = re.findall(r"<title>([^<]{12,160})</title>", blob, flags=re.I)
        for title in titles:
            title = re.sub(r"&amp;", "&", title).strip()
            if "Google" in title or title == "Reuters":
                continue
            hits.append(title[:70])
            if len(hits) >= 2:
                break
    if not hits:
        return ["회견 중, 속보 부족. 공식 요지는 보통 다음날."]
    return hits[:3]


def fmt(v: float | None, nd: int = 1) -> str:
    return f"{v:,.{nd}f}" if v is not None else "없음"


def breaks(cfg: dict, usdjpy: float | None, usdkrw: float | None, j30: float | None) -> list[str]:
    lv = cfg["levels"]
    out: list[str] = []
    if usdjpy is not None and usdjpy <= lv["usdjpy_low"]:
        out.append(f"달러/엔 {fmt(usdjpy)} → 152 이탈")
    if usdjpy is not None and usdjpy >= lv["usdjpy_high"]:
        out.append(f"달러/엔 {fmt(usdjpy)} → 160 돌파")
    elif usdjpy is not None and usdjpy >= lv.get("usdjpy_watch", 158):
        out.append(f"달러/엔 {fmt(usdjpy)} → 158 돌파")
    if usdkrw is not None and usdkrw <= lv["usdkrw_low"]:
        out.append(f"원달러 {fmt(usdkrw, 0)}원 → 1,340 이탈")
    if usdkrw is not None and usdkrw >= lv["usdkrw_high"]:
        out.append(f"원달러 {fmt(usdkrw, 0)}원 → 1,400 돌파")
    if j30 is not None and j30 >= lv["jgb30_danger"]:
        out.append(f"일본 30년 {fmt(j30, 2)}% → 4.5 돌파")
    elif j30 is not None and j30 >= lv["jgb30_warn"]:
        out.append(f"일본 30년 {fmt(j30, 2)}% → 4.3 돌파")
    elif j30 is not None and j30 >= lv.get("jgb30_watch", 4.25):
        out.append(f"일본 30년 {fmt(j30, 2)}% → 4.25 돌파")
    return out


def action_line(keys: list[str]) -> str:
    if not keys:
        return "레벨 안 깨졌으니 기존 북 유지. 추격, 레버리지 금지."
    bits = []
    blob = " ".join(keys)
    if "160 돌파" in blob:
        bits.append("엔 롱 접기")
    if "152 이탈" in blob:
        bits.append("엔은 확인만, 원 약세(리스크오프) 대비")
    if "1,340 이탈" in blob:
        bits.append("달러 보유 줄이기")
    if "1,400 돌파" in blob:
        bits.append("달러 일부 사기")
    if "158 돌파" in blob:
        bits.append("엔 약세 경계 구간, 160 근처 개입 위험. 엔 추격 금지")
    if "4.25 돌파" in blob:
        bits.append("일본 초장기 사상 최고권. 확인만, 미국 초장기 판단은 4.3 알림 때")
    if "4.5 돌파" in blob:
        bits.append("미국 초장기 추가 축소")
    elif "4.3 돌파" in blob:
        bits.append("미국 초장기 비중 줄이기")
    return ". ".join(bits) + "."


def event_key(keys: list[str], extra: str = "") -> str:
    return "|".join(keys + ([extra] if extra else []))


def level_sig(key: str | None) -> str:
    """Event key without the live quote: "원달러 1,323원 → 1,340 이탈" -> "원달러 → 1,340 이탈".
    The raw key changed with every quote, so the same broken level was re-sent on every run. The "fed-<date>" tag
    added by the FOMC message is ignored, so the level message is not re-sent right after a FOMC message."""
    return "|".join(re.sub(r"\s+[-+]?[\d,.]+(?:원|%)?\s*→", " →", k).strip() for k in (key or "").split("|")
                    if k and not re.match(r"(fed|boj)[-_]", k))


def send(cfg: dict, title: str, lines: list[str], priority: int = 4) -> None:
    if DRY:
        print(f"DRY RUN would send (p{priority}): {title}\n" + "\n".join(lines))
        return
    topic = resolve_topic("port", cfg.get("ntfy_topic"))
    server = resolve_server(cfg.get("ntfy_server"))
    publish(topic, title, "\n".join(lines), priority, ["jp", "chart_with_upwards_trend"], server)


def quotes() -> tuple[float | None, float | None, float | None]:
    return yahoo("USDJPY=X"), yahoo("USDKRW=X"), jgb30()


def load_state() -> dict:
    state = {
        "briefing_sent": False,
        "fed_sent_date": None,
        "boj_sent_date": None,
        "boj_press_sent_date": None,
        "weekly_sent_week": None,
        "last_event_key": None,
        "last_quotes": {},
        "updated_at": None,
    }
    if STATE_PATH.exists():
        try:
            state.update(json.loads(STATE_PATH.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass
    return state


def save_state(state: dict, now: datetime, quotes_d: dict) -> None:
    if DRY:
        print("DRY: state not saved")
        return
    state["updated_at"] = now.isoformat()
    state["last_quotes"] = quotes_d
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def status_line(usdjpy: float | None, usdkrw: float | None, j30: float | None, keys: list[str]) -> str:
    base = f"달러/엔 {fmt(usdjpy)} | 원달러 {fmt(usdkrw, 0)}원 | 일본 30년 {fmt(j30, 2)}%"
    if keys:
        return base + " | 깨진 것: " + ", ".join(keys)
    return base + " | 깨진 레벨 없음"


def emit_fed(cfg: dict, now: datetime, usdjpy, usdkrw, j30, keys: list[str]) -> None:
    us_day = (now - timedelta(hours=13)).date()
    stance = fed_stance(datetime(us_day.year, us_day.month, us_day.day, tzinfo=timezone.utc))
    act = action_line(keys)
    title = f"[YENCARRY] {act}"
    lines = [
        f"{now.strftime('%Y-%m-%d %H:%M KST')} | YENCARRY | 연준 후속",
        "이게 뭐 알림: 연준 발표 뒤, 정해둔 레벨이 깨졌는지 보고 북을 바꿀지 결정하는 알림입니다.",
        f"지금 결과/상태: 연준 {stance}. {status_line(usdjpy, usdkrw, j30, keys)}",
        f"지금 할 일: {act}",
        "나중에: 일한 9/18은 레벨과 상관없이 반드시 보냅니다. 주간 점검은 월요일 09:00.",
        "익절/줄일 때: 엔 롱은 160 재돌파면 접기. 원달러 1,340 아래면 달러 보유 축소.",
        "하지 말 것: 연준 한 방으로 원, 코스피, 미국채가 같이 무너진다고 몰빵.",
    ]
    send(cfg, title, lines, 5 if keys else 4)


def emit_boj(cfg: dict, now: datetime, usdjpy, usdkrw, j30, keys: list[str], stance: str) -> None:
    act = action_line(keys)
    title = f"[YENCARRY] 일한 {stance} | {act}"
    lines = [
        f"{now.strftime('%Y-%m-%d %H:%M KST')} | YENCARRY | 일한 결과",
        "이게 뭐 알림: 일본은행 결정. 레벨이 안 깨져도 결과는 반드시 보냅니다.",
        f"지금 결과/상태: 일한 {stance}. {status_line(usdjpy, usdkrw, j30, keys)}",
        f"지금 할 일: {act}",
        "나중에: 다음 일한 10/30, 12/18도 공표문+회견 두 장입니다.",
        "익절/줄일 때: 160 재돌파면 엔 롱 접기. 1,340 아래면 달러 줄이기.",
        "하지 말 것: 공표문 없이 추격. 엔 강세=원 강세로 같이 베팅.",
    ]
    send(cfg, title, lines, 5)


def emit_boj_press(cfg: dict, now: datetime, usdjpy, usdkrw, j30, keys: list[str], stance: str) -> None:
    act = action_line(keys)
    title = f"[YENCARRY] 일한 회견 후 | {act}"
    lines = [
        f"{now.strftime('%Y-%m-%d %H:%M KST')} | YENCARRY | 일한 기자회견 후",
        "이게 뭐 알림: 우에다 기자회견(15:30) 뒤 레벨과 발언 요약을 봅니다. 공표문 알림과 별도입니다.",
        f"지금 결과/상태: 일한 {stance}. {status_line(usdjpy, usdkrw, j30, keys)}",
        "우에다 요약: " + " / ".join(ueda_summary(now)),
        f"지금 할 일: {act}",
        "나중에: 같은 날 회견 알림은 한 번만. 공식 전문은 보통 다음날. 다음 일한 10/30, 12/18도 두 장.",
        "익절/줄일 때: 160 재돌파면 엔 롱 접기. 1,340 아래면 달러 줄이기.",
        "하지 말 것: 회견 톤만 듣고 추격. 엔 강세=원 강세로 같이 베팅.",
    ]
    send(cfg, title, lines, 5)


def emit_event(cfg: dict, now: datetime, usdjpy, usdkrw, j30, keys: list[str], why: str) -> None:
    act = action_line(keys)
    title = f"[YENCARRY] {act}"
    lines = [
        f"{now.strftime('%Y-%m-%d %H:%M KST')} | YENCARRY | 이벤트 {why}",
        "이게 뭐 알림: 정해둔 레벨이 깨졌습니다.",
        f"지금 결과/상태: {status_line(usdjpy, usdkrw, j30, keys)}",
        f"지금 할 일: {act}",
        "나중에: 같은 레벨은 버퍼(원달러 10원, 달러/엔 0.5엔, 일본 30년 0.05%p)만큼 되돌아간 뒤 다시 깨질 때만 또 보냅니다. 주간 점검은 월요일 09:00.",
        "익절/줄일 때: 160 재돌파면 엔 롱 접기. 1,340 아래면 달러 줄이기.",
        "하지 말 것: 깨진 레벨 없이 추격. 엔 강세=원 강세로 같이 베팅.",
    ]
    send(cfg, title, lines, 5)


def emit_weekly(cfg: dict, now: datetime, usdjpy, usdkrw, j30, keys: list[str]) -> None:
    act = action_line(keys)
    title = f"[YENCARRY] 주간 점검 | {act}"
    lines = [
        f"{now.strftime('%Y-%m-%d %H:%M KST')} | YENCARRY | 주간",
        "이게 뭐 알림: 엔캐리 레벨을 한 주에 한 번 보는 점검입니다.",
        f"지금 결과/상태: {status_line(usdjpy, usdkrw, j30, keys)}",
        f"지금 할 일: {act}",
        "나중에: 152/160, 1,340/1,400, 일본 30년 4.3 깨지면 그때 다시 보냅니다.",
        "익절/줄일 때: 160 재돌파면 엔 롱 접기. 1,340 아래면 달러 줄이기.",
        "하지 말 것: 매주 점검 대신 매일 추격.",
    ]
    send(cfg, title, lines, 3 if not keys else 4)


def main() -> int:
    global DRY
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    if mode == "selftest":
        return selftest()
    DRY = (sys.argv[2] if len(sys.argv) > 2 else "auto").lower() == "dry"
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    # Year guard only for the dated one-off modes (fed / boj_press): their GitHub crons have no year.
    # Weekly check and level watch are recurring and stay on every year.
    if mode in {"fed", "boj_press"} and now.year != int(cfg.get("year_guard") or 2026):
        print(f"yencarry {mode} year-guard silent")
        return 0
    state = load_state()
    usdjpy, usdkrw, j30 = quotes()
    q = {"usdjpy": usdjpy, "usdkrw": usdkrw, "jgb30": j30}
    prev_active = state.get("active_levels")
    active, keys, new_levels = level_step(cfg, prev_active, q)
    state["active_levels"] = active
    print(f"{now} | {mode}{' DRY' if DRY else ''} | {q} | active={sorted(active)} new={new_levels} | {keys}")

    today = now.strftime("%Y-%m-%d")
    week = now.strftime("%G-W%V")
    fomc = set(cfg.get("fomc_dates") or [])
    boj = set(cfg.get("boj_dates") or [])
    us_cal = (now - timedelta(hours=13)).strftime("%Y-%m-%d")
    is_fed_window = us_cal in fomc or today in fomc
    is_boj = today in boj
    boj_after = 12
    raw_after = str(cfg.get("boj_send_after_kst") or "12:00")
    try:
        boj_after = int(raw_after.split(":")[0])
    except ValueError:
        boj_after = 12

    if mode == "fed" or (mode == "auto" and is_fed_window and now.hour >= 3):
        if state.get("fed_sent_date") == us_cal:
            print("fed already sent")
        else:
            emit_fed(cfg, now, usdjpy, usdkrw, j30, keys)
            state["fed_sent_date"] = us_cal
            state["last_event_key"] = event_key(keys, "fed-" + us_cal)
            save_state(state, now, q)
            return 0

    if mode == "boj_press" or (is_boj and cfg.get("boj_press_force", True) and now.hour >= 16):
        if state.get("boj_press_sent_date") == today:
            print("boj press already sent")
        else:
            stance = boj_stance(now)
            emit_boj_press(cfg, now, usdjpy, usdkrw, j30, keys, stance)
            state["boj_press_sent_date"] = today
            save_state(state, now, q)
            return 0

    if is_boj and cfg.get("boj_force", True) and mode != "boj_press":
        if now.hour < boj_after and mode != "fed":
            print("boj morning; wait for statement")
        elif state.get("boj_sent_date") == today:
            print("boj already sent")
        else:
            stance = boj_stance(now)
            emit_boj(cfg, now, usdjpy, usdkrw, j30, keys, stance)
            state["boj_sent_date"] = today
            save_state(state, now, q)
            return 0

    if new_levels:
        emit_event(cfg, now, usdjpy, usdkrw, j30, keys, "레벨 탈출")
        state["last_event_key"] = event_key(keys)
        save_state(state, now, q)
        return 0
    if keys:
        # level(s) still active (inside the zone or within the re-arm buffer): no repeat; the Monday weekly still reports it
        print("no new level (hysteresis); silent")

    if mode in {"weekly", "auto"} and now.weekday() == 0:
        if state.get("weekly_sent_week") == week:
            print("weekly already sent")
            save_state(state, now, q)
            return 0
        emit_weekly(cfg, now, usdjpy, usdkrw, j30, keys)
        state["weekly_sent_week"] = week
        save_state(state, now, q)
        return 0

    print("silent")
    save_state(state, now, q)
    return 0


def selftest() -> int:
    global DRY
    DRY = True
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    fails = 0

    def check(label, got, want):
        nonlocal fails
        ok = got == want
        fails += not ok
        print(("ok  " if ok else "FAIL"), label, "" if ok else f"got {got!r} want {want!r}")
    Q = lambda k, j=157.0, g=3.9: {"usdkrw": k, "usdjpy": j, "jgb30": g}  # noqa: E731
    a, keys, new = level_step(cfg, None, Q(1335))
    check("first run init, nothing new", (sorted(a), new), (["krw1340"], []))
    a, keys, new = level_step(cfg, {}, Q(1340))
    check("1,340 entry fires (<=)", new, ["krw1340"])
    walk = [(1341, []), (1337, []), (1349.9, []), (1338, []), (1350, []), (1352, []), (1339, ["krw1340"]), (1345, []), (1336, [])]
    act = {"krw1340": True}
    for i, (k, want) in enumerate(walk):
        act, keys, new = level_step(cfg, act, Q(k))
        check(f"krw walk{i} {k}", new, want)
    check("status keeps the level while inside the buffer", any("1,340 이탈" in x for x in level_step(cfg, {"krw1340": True}, Q(1345))[1]), True)
    act = {}
    seq = [(157.9, []), (158.0, ["jpy158"]), (157.7, []), (158.2, []), (157.5, []), (158.0, ["jpy158"])]
    for i, (j, want) in enumerate(seq):
        act, _, new = level_step(cfg, act, Q(1360, j))
        check(f"jpy walk{i} {j}", new, want)
    act, keys, new = level_step(cfg, {"jpy158": True}, Q(1360, 160.1))
    check("160 entry, 158 hidden", (new, [x for x in keys if "158" in x]), (["jpy160"], []))
    act, keys, new = level_step(cfg, {"krw1340": True}, Q(None))
    check("missing quote keeps level", sorted(act), ["krw1340"])
    act, keys, new = level_step(cfg, {"krw1340": True}, Q(1395))
    check("leaving is silent", (new, sorted(act)), ([], []))
    check("1,400 entry", level_step(cfg, {}, Q(1400))[2], ["krw1400"])
    check("1,400 exit needs 1,390", sorted(level_step(cfg, {"krw1400": True}, Q(1391))[0]), ["krw1400"])
    print("selftest", "PASS" if not fails else f"FAIL {fails}")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
