#!/usr/bin/env python3
"""[WBTN4Q] WBTN 4Q26 실적 발표 다음 미국 정규장 종가 기준 일회 판정.

modes: auto (morning 기본) | dry (발송/상태 저장 없이 출력) | force (창/중복 무시하고 채점 발송)
       install (설치 확인 1회 발송, 상태 파일을 건드리지 않음)
       selftest (네트워크 없이 파서/판정/제목 검사)

보도자료: ir.webtoon.com 뉴스, GlobeNewswire RSS, SEC 8-K Item 2.02(보조).
제목에 Fourth Quarter 와 2026 이 있고 실적 결과(Reports, to Report 제외)인 건만 채점 대상으로 본다.
발표 다음 미국 정규장이 끝난 뒤 한 번만 보내고 state.fired 로 잠근다.
2027-03-15 이후 또는 event_year 가 아니면 침묵(크론에 연도가 없어도 2028에 다시 안 울림).
"""
from __future__ import annotations

import json
import re
import sys
from datetime import date, datetime, timedelta, timezone
from html import unescape
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import LAST_PUBLISH, publish, resolve_server, resolve_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
CFG = ROOT / "configs" / "wbtn4q.json"
STATE = ROOT / "state" / "wbtn4q.json"
CIK_NUM = "1997859"
SEC_UA = {"User-Agent": "cwally-alerts sosmos3218@gmail.com", "Accept-Encoding": "identity"}
WEB_UA = {
    "User-Agent": "Mozilla/5.0 (cwally-alerts)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "identity",
}
MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"
MONTH_NUM = {m: i + 1 for i, m in enumerate(MONTHS.split("|"))}
ABBR = {m[:3]: i + 1 for i, m in enumerate(MONTHS.split("|"))}


def fetch(url: str, ua: dict) -> str:
    with urlopen(Request(url, headers=ua), timeout=25) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def textify(html: str) -> str:
    t = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
    t = re.sub(r"<[^>]+>", " ", t)
    return re.sub(r"\s+", " ", unescape(t))


def abs_url(href: str, base: str = "https://ir.webtoon.com") -> str:
    if href.startswith("http"):
        return href
    if href.startswith("/"):
        return base + href
    return base + "/" + href


def parse_mdy(text: str) -> date | None:
    m = re.search(rf"({MONTHS})\s+(\d{{1,2}}),\s+(20\d{{2}})", text, re.I)
    if not m:
        m = re.search(r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+(\d{1,2}),\s+(20\d{2})", text, re.I)
        if not m:
            return None
        mon = ABBR.get(m.group(1)[:3].title())
        return date(int(m.group(3)), mon, int(m.group(2))) if mon else None
    mon = MONTH_NUM[m.group(1).capitalize()]
    return date(int(m.group(3)), mon, int(m.group(2)))


def is_date_notice(title: str) -> bool:
    t = re.sub(r"\s+", " ", title)
    return bool(re.search(r"\bto\s+Report\b", t, re.I) and re.search(r"Fourth\s+Quarter", t, re.I) and "2026" in t)


def is_results_title(title: str) -> bool:
    """실적 결과 보도자료. 'to Report ... on DATE' 공지는 제외."""
    t = re.sub(r"\s+", " ", title or "")
    if is_date_notice(t):
        return False
    if not re.search(r"Fourth\s+Quarter", t, re.I) or "2026" not in t:
        return False
    return bool(re.search(r"\bReports?\b", t, re.I) or re.search(r"Financial\s+Results", t, re.I))


def fmt_pct(v: float | None) -> str:
    if v is None:
        return "확인 불가"
    sign = "+" if v > 0 else ""
    return f"{sign}{v:.1f}%"


def fmt_px(v: float | None) -> str:
    return f"{v:.2f}달러" if v is not None else "확인 불가"



def read_pct(s: str | None) -> float | None:
    """ '(11.4)', '(11.4%)', '(11.4)%' 는 음수, '-11.4' 도 음수."""
    if not s:
        return None
    s = s.strip()
    neg = "(" in s or s.lstrip().startswith("-")
    s = s.replace("(", "").replace(")", "").replace("%", "").replace(",", "").strip()
    try:
        v = float(s)
    except ValueError:
        return None
    return -abs(v) if neg else v


def parse_results(text: str) -> dict:
    """Q4 실적 본문에서 전사 고정환율 성장과 일본 고정환율 성장을 읽는다. 가이던스 문구는 버린다."""
    # 가이던스('in the range of')와 'We define' 정의 문단은 패턴이 달라 쓰지 않는다. 표는 Outlook 뒤에 올 수 있어 자르지 않는다.
    body = text
    cc = None
    m = re.search(r"Revenue Growth on a Constant Currency Basis of (\(?-?[\d.]+\)?)\s*%", body, re.I)
    if m:
        cc = read_pct(m.group(1))
    if cc is None:
        m = re.search(
            r"(?<!Korea )(?<!Japan )(?<!World )Revenue on a Constant Currency Basis\s*\$?\s*[\d,]+\s*\$?\s*[\d,]+\s*(\(?-?[\d.]+\)?)\s*%",
            body,
            re.I,
        )
        if m:
            cc = read_pct(m.group(1))
    jp = None
    m = re.search(
        r"Japan Revenue on a Constant Currency Basis\s*\$?\s*[\d,]+\s*\$?\s*[\d,]+\s*(\(?-?[\d.]+\)?)\s*%",
        body,
        re.I,
    )
    if m:
        jp = read_pct(m.group(1))
    return {"cc_growth": cc, "jp_cc_growth": jp, "parsed": cc is not None or jp is not None}


def ir_listing(cfg: dict) -> tuple[list[dict], str]:
    try:
        raw = fetch(cfg["sources"]["ir_news"], WEB_UA)
    except Exception as exc:
        return [], f"ir: {exc}"
    items = []
    for m in re.finditer(r'data-title="([^"]+)"', raw):
        title = unescape(m.group(1))
        chunk = raw[m.start() : m.start() + 2500]
        href_m = re.search(r'href="([^"]+news-release-details[^"]+)"', chunk)
        href = abs_url(href_m.group(1)) if href_m else ""
        # listing date: first Month DD, YYYY that is not only inside the title
        after = chunk[len(m.group(0)) :]
        d = parse_mdy(after) or parse_mdy(chunk)
        items.append({"title": title, "url": href, "date": d.isoformat() if d else None, "src": "ir"})
    return items, ""


def gn_listing(cfg: dict) -> tuple[list[dict], str]:
    errs = []
    items = []
    for url in cfg["sources"].get("globenewswire_rss") or []:
        try:
            raw = fetch(url, WEB_UA)
        except Exception as exc:
            errs.append(f"gn: {exc}")
            continue
        for block in re.findall(r"<item\b[^>]*>.*?</item>", raw, re.I | re.S):
            tm = re.search(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", block, re.I | re.S)
            lm = re.search(r"<link>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</link>", block, re.I | re.S)
            dm = re.search(r"<pubDate>(.*?)</pubDate>", block, re.I | re.S)
            title = unescape(re.sub(r"\s+", " ", (tm.group(1) if tm else "").strip()))
            link = (lm.group(1) if lm else "").strip()
            pub = None
            if dm:
                # 'Tue, 09 Nov 2026 16:05:00 GMT'
                p = re.search(r"(\d{1,2})\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(20\d{2})", dm.group(1))
                if p:
                    pub = date(int(p.group(3)), ABBR[p.group(2).title()], int(p.group(1))).isoformat()
            items.append({"title": title, "url": link, "date": pub, "src": "gn"})
        if items:
            break
    return items, "; ".join(errs)


def sec_results(cfg: dict) -> tuple[dict | None, str]:
    """보조: 2027-01-15 이후 8-K Item 2.02 EX-99 에 Fourth Quarter 2026 이 있으면 결과로 본다."""
    try:
        data = json.loads(fetch(cfg["sources"]["sec"], SEC_UA))
    except Exception as exc:
        return None, f"sec: {exc}"
    rec = data.get("filings", {}).get("recent", {})
    rows = zip(
        rec.get("form", []),
        rec.get("filingDate", []),
        rec.get("accessionNumber", []),
        rec.get("primaryDocument", []),
        rec.get("items", []),
    )
    for form, filed, acc, doc, items in rows:
        if filed < "2027-01-15":
            break
        if form != "8-K" or "2.02" not in (items or ""):
            continue
        base = f"https://www.sec.gov/Archives/edgar/data/{CIK_NUM}/{acc.replace('-', '')}"
        try:
            idx = json.loads(fetch(base + "/index.json", SEC_UA))
            names = [i.get("name", "") for i in idx.get("directory", {}).get("item", [])]
            ex = [n for n in names if re.search(r"(ex-?99|exx?99|991)", n, re.I) and re.search(r"\.(htm|html|txt)$", n, re.I)]
            url = base + "/" + (ex[0] if ex else doc)
            text = textify(fetch(url, SEC_UA))
        except Exception as exc:
            return None, f"sec 8-K: {exc}"
        if re.search(r"Fourth\s+Quarter", text, re.I) and "2026" in text and re.search(r"December 31, 2026|fourth quarter ended", text, re.I):
            return {"title": "SEC 8-K Exhibit (Fourth Quarter 2026)", "url": url, "date": filed, "src": "sec", "text": text}, ""
    return None, ""


def fetch_body(url: str) -> tuple[str, str]:
    try:
        raw = fetch(url, WEB_UA if "sec.gov" not in url else SEC_UA)
        return textify(raw), ""
    except Exception as exc:
        return "", str(exc)


def yahoo_closes(cfg: dict) -> tuple[list[tuple[date, float]], str]:
    try:
        data = json.loads(fetch(cfg["sources"]["quote"], WEB_UA))
        res = data["chart"]["result"][0]
        ts = res.get("timestamp") or []
        qs = (res.get("indicators", {}).get("quote") or [{}])[0]
        closes = qs.get("close") or []
        off = int((res.get("meta") or {}).get("gmtoffset") or 0)
        out: list[tuple[date, float]] = []
        for t, c in zip(ts, closes):
            if c is None:
                continue
            d = datetime.fromtimestamp(int(t) + off, timezone.utc).date()
            out.append((d, float(c)))
        return out, ""
    except Exception as exc:
        return [], f"yahoo: {exc}"


def session_after(release: date, bars: list[tuple[date, float]]) -> tuple[date, float] | None:
    """발표일 다음 미국 정규장(발표일보다 큰 첫 봉)의 종가."""
    later = [(d, c) for d, c in bars if d > release]
    return later[0] if later else None


def judge(cc: float | None, jp: float | None, close: float | None, cfg: dict) -> dict:
    th = cfg["thresholds"]
    cc_fire = cc is not None and cc < th["cc_growth_min"]
    jp_fire = jp is not None and jp <= th["japan_cc_max_decline"]
    px_fire = close is not None and close <= th["close_usd"]
    jp_improving = jp is not None and jp > th["japan_cc_max_decline"]
    fires = []
    if cc_fire:
        fires.append(f"고정환율 성장 {fmt_pct(cc)} (기준 9%)")
    if jp_fire:
        fires.append(f"일본 고정환율 {fmt_pct(jp)} (기준 -11.4% 이하)")
    if px_fire:
        fires.append(f"종가 {fmt_px(close)} (기준 8.50달러 이하)")
    if fires:
        verdict = "축소 25~50% 검토"
        why = "조건 충족: " + ", ".join(fires) + "."
        pri = 4
    elif cc is None and jp is None:
        verdict = "확인 불가"
        why = "보도자료에서 고정환율 성장과 일본 고정환율 숫자를 읽지 못했습니다. 원문 링크와 종가만 참고하세요."
        pri = 3
    elif cc is not None and jp_improving and close is not None and not cc_fire and not px_fire:
        verdict = "보유 유지"
        why = f"세 기준을 모두 통과했고 일본 감소폭이 -11.4%보다 줄었습니다. 고정환율 {fmt_pct(cc)}, 일본 {fmt_pct(jp)}, 종가 {fmt_px(close)}."
        pri = 3
    else:
        verdict = "보유, 0~20% 소폭 축소 선택"
        why = "당장 25~50% 축소 조건은 안 걸렸지만 일부 숫자가 비었거나 일본 개선을 단정할 수 없습니다."
        pri = 3
    return {"verdict": verdict, "why": why, "priority": pri, "fires": fires, "jp_improving": jp_improving}


def load_state() -> dict:
    if STATE.is_file():
        try:
            return json.loads(STATE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {"fired": False, "release_url": None, "release_date": None, "judge_session": None, "verdict": None}


def save_state(st: dict, now: datetime) -> None:
    st["updated_at"] = now.isoformat()
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def emit(cfg: dict, title: str, lines: list[str], pri: int) -> None:
    publish(resolve_topic("port", cfg.get("ntfy_topic")), title[:180], "\n".join(lines), pri, server=resolve_server(cfg.get("ntfy_server")), config_name="wbtn4q", config=cfg)


def head(now: datetime, kind: str) -> str:
    return f"{now.strftime('%Y-%m-%d %H:%M KST')} {kind}"


def six(kind: str, what: str, status: str, todo: str, later: str, cut: str, dont: str, now: datetime) -> list[str]:
    return [
        head(now, kind),
        "이게 뭐 알림: " + what,
        "지금 결과/상태: " + status,
        "지금 할 일: " + todo,
        "나중에: " + later,
        "익절/줄일 때: " + cut,
        "하지 말 것: " + dont,
    ]


def score_lines(cfg: dict, now: datetime, j: dict, cc, jp, close, close_day, release: dict, url: str) -> list[str]:
    status = (
        f"{j['why']} 고정환율 매출 성장 {fmt_pct(cc)} (기준 9% 이상), "
        f"일본 고정환율 {fmt_pct(jp)} (나쁨 = -11.4% 이하), "
        f"종가 {fmt_px(close)}"
        + (f" ({close_day})" if close_day else "")
        + f". 보도자료 날짜 {release.get('date') or '확인 불가'}."
    )
    if url:
        status += f" 원문 {url}"
    todo = {
        "축소 25~50% 검토": "보유 비중 25~50% 축소 검토. 이 알림만으로 전량 매도하지 마세요.",
        "보유 유지": "지금은 비중을 유지. 추가 매수나 축소는 이 알림만으로 결정하지 마세요.",
        "보유, 0~20% 소폭 축소 선택": "보유를 기본으로 하되 0~20% 소폭 축소는 선택. 급하게 크게 줄이지 마세요.",
        "확인 불가": "원문과 종가를 직접 확인. 숫자를 못 읽었다고 자동 매도하지 마세요.",
    }[j["verdict"]]
    return six(
        j["verdict"],
        cfg["explain"],
        status,
        todo,
        "이 일회 판정은 다시 보내지 않습니다. 이후 판단은 직접 하시면 됩니다.",
        "이 알림의 결론이 나와도 전량 청산 신호는 아닙니다.",
        "투자 자문이 아닙니다. 이 알림만으로 매수 또는 매도하지 마세요.",
        now,
    )


# ---------- selftest fixtures (오프라인) ----------
Q3_TITLE = "WEBTOON Entertainment Inc. Reports Third Quarter 2026 Financial Results"
Q4_TITLE = "WEBTOON Entertainment Inc. Reports Fourth Quarter 2026 Financial Results"
Q4_NOTICE = "WEBTOON Entertainment Inc. to Report Fourth Quarter 2026 Financial Results on March 2, 2027"
Q4_FIXTURE = """
WEBTOON Entertainment Inc. Reports Fourth Quarter 2026 Financial Results
LOS ANGELES, March 02, 2027 (GLOBE NEWSWIRE)
Fourth Quarter Revenue Growth on a Constant Currency Basis of 10.2%
Revenue on a Constant Currency Basis $ 400,000 $ 363,000 10.2%
Japan Revenue $ 160,000 $ 179,624 (10.9%)
Japan Revenue on a Constant Currency Basis $ 165,000 $ 179,624 (8.1%)
Fourth Quarter 2027 Outlook For the first quarter 2027, the Company expects:
Revenue growth on a constant currency basis in the range of 9.0%-12.0%.
"""
Q4_MISS = """
WEBTOON Entertainment Inc. Reports Fourth Quarter 2026 Financial Results
Revenue Growth on a Constant Currency Basis of 7.0%
Japan Revenue on a Constant Currency Basis $ 150,000 $ 179,624 (16.5%)
"""


def selftest() -> int:
    fails = []

    def check(name, got, want):
        if got != want:
            fails.append(f"{name}: got {got!r} want {want!r}")

    check("q3 title not results", is_results_title(Q3_TITLE), False)
    check("q4 title is results", is_results_title(Q4_TITLE), True)
    check("date notice excluded", is_results_title(Q4_NOTICE), False)
    check("date notice flag", is_date_notice(Q4_NOTICE), True)
    p = parse_results(Q4_FIXTURE)
    check("cc 10.2 not outlook 9-12", p["cc_growth"], 10.2)
    check("japan -8.1", p["jp_cc_growth"], -8.1)
    # Q3 excerpt should parse japan -11.4 if we feed the table line, but title path ignores Q3
    q3 = parse_results(
        "Revenue Growth on a Constant Currency Basis of 0.2% "
        "Japan Revenue on a Constant Currency Basis $ 159,077 $ 179,624 (11.4%) "
        "Fourth Quarter 2026 Outlook For the fourth quarter 2026, the Company expects: "
        "Revenue growth on a constant currency basis in the range of 9.0%-12.0%."
    )
    check("q3 cc 0.2", q3["cc_growth"], 0.2)
    check("q3 japan -11.4", q3["jp_cc_growth"], -11.4)
    cfg = json.loads(CFG.read_text(encoding="utf-8"))
    hold = judge(10.2, -8.1, 9.00, cfg)
    check("all clear hold", hold["verdict"], "보유 유지")
    cut = judge(7.0, -8.1, 9.00, cfg)
    check("cc miss cut", cut["verdict"], "축소 25~50% 검토")
    cut2 = judge(10.2, -11.4, 9.00, cfg)
    check("japan equal cut", cut2["verdict"], "축소 25~50% 검토")
    cut3 = judge(10.2, -8.1, 8.50, cfg)
    check("price equal cut", cut3["verdict"], "축소 25~50% 검토")
    unk = judge(None, None, 9.10, cfg)
    check("parse fail", unk["verdict"], "확인 불가")
    mid = judge(10.2, None, 9.10, cfg)
    check("partial otherwise", mid["verdict"], "보유, 0~20% 소폭 축소 선택")
    miss = parse_results(Q4_MISS)
    check("miss cc", miss["cc_growth"], 7.0)
    check("miss jp", miss["jp_cc_growth"], -16.5)
    # next session
    bars = [(date(2027, 3, 1), 9.1), (date(2027, 3, 2), 8.8), (date(2027, 3, 3), 8.4)]
    check("session after 3/2", session_after(date(2027, 3, 2), bars), (date(2027, 3, 3), 8.4))
    check("session not yet", session_after(date(2027, 3, 3), bars), None)
    titles = [
        "[WBTN4Q] WBTN 축소 25~50% 검토 | 4분기 실적 다음 날",
        "[WBTN4Q] WBTN 보유 유지 | 4분기 실적 다음 날",
        "[WBTN4Q] WBTN 보유, 0~20% 소폭 축소 선택 | 4분기 실적 다음 날",
        "[WBTN4Q] WBTN 확인 불가 | 4분기 실적 다음 날",
        "[WBTN4Q] WBTN 설치 확인 | 매매 신호 아님",
        "[WBTN4Q] WBTN 조회 실패 | 판단 보류",
    ]
    for t in titles:
        check("ticker in " + t, "WBTN" in t and t.startswith("[WBTN4Q]"), True)
        if "—" in t or "·" in t:
            fails.append("bad punct " + t)
    if fails:
        print("SELFTEST FAIL")
        for f in fails:
            print(" ", f)
        return 1
    print("selftest ok")
    return 0


def install(cfg: dict, now: datetime) -> int:
    lines = six(
        "설치 확인",
        cfg["explain"],
        "설치 확인용 발송입니다. 매매 신호가 아닙니다. 회사는 아직 4분기 발표일을 공지하지 않았습니다. 창은 2027-02-01부터 2027-03-15까지, 평일 09:00 KST morning 슬롯에서만 찾습니다.",
        "없음. 이 설치 확인으로 비중을 바꾸지 마세요.",
        "보도자료(제목 Fourth Quarter 와 2026, 실적 결과)가 나온 뒤 그다음 미국 정규장 종가로 한 번만 판정합니다. 2027-03-15 이후 또는 이미 보낸 뒤에는 조용합니다.",
        "이 설치 확인으로 익절하거나 줄이지 마세요.",
        "이 알림을 매수 또는 매도 신호로 읽기. 투자 자문이 아닙니다.",
        now,
    )
    emit(cfg, "[WBTN4Q] WBTN 설치 확인 | 매매 신호 아님", lines, 2)
    print("install sent", LAST_PUBLISH)
    return 0


def in_window(cfg: dict, now: datetime) -> bool:
    start = datetime.fromisoformat(cfg["window_start_kst"]).replace(tzinfo=KST)
    end = datetime.fromisoformat(cfg["ends_kst"]).replace(tzinfo=KST)
    return start <= now <= end and now.year == int(cfg["event_year"])


def title_for(verdict: str) -> str:
    return f"[WBTN4Q] WBTN {verdict} | 4분기 실적 다음 날"


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    cfg = json.loads(CFG.read_text(encoding="utf-8"))
    now = datetime.now(KST)

    if mode == "selftest":
        return selftest()
    if mode == "install":
        return install(cfg, now)

    st = load_state()
    window = in_window(cfg, now)
    print(f"{now.isoformat()} mode={mode} window={window} fired={st.get('fired')}")
    if mode != "force" and not window:
        print("outside 2027-02-01..2027-03-15 window or wrong year; silent")
        return 0
    if st.get("fired") and mode != "force":
        print("already fired; silent")
        return 0

    errors = []
    ir_items, ir_err = ir_listing(cfg)
    if ir_err:
        errors.append(ir_err)
    gn_items, gn_err = gn_listing(cfg)
    if gn_err:
        errors.append(gn_err)
    items = ir_items + gn_items
    # remember date-notice if we see one (정보만, 발송 없음)
    notice = next((it for it in items if is_date_notice(it["title"])), None)
    results = [it for it in items if is_results_title(it["title"])]
    sec_hit, sec_err = (None, "")
    if not results:
        sec_hit, sec_err = sec_results(cfg)
        if sec_err:
            errors.append(sec_err)
        if sec_hit:
            results = [sec_hit]

    if not results:
        if errors and mode != "dry":
            day = now.strftime("%Y-%m-%d")
            if st.get("ops_day") != day:
                emit(
                    cfg,
                    "[WBTN4Q] WBTN 조회 실패 | 판단 보류",
                    six(
                        "조회 실패",
                        cfg["explain"],
                        "조회 실패. " + "; ".join(errors) + ". 실패를 실적 결과로 채점하지 않음."
                        + (f" 발표일 공지 후보: {notice['title']}" if notice else ""),
                        "없음. 다음에 다시 찾습니다.",
                        "보도자료가 나오면 그다음 정규장 종가로 한 번 판정합니다.",
                        "조회 실패로 비중 조정 금지.",
                        "조회 실패를 실적 미스로 읽기.",
                        now,
                    ),
                    3,
                )
                if mode != "dry":
                    st["ops_day"] = day
                    save_state(st, now)
        else:
            print("no Q4 results release yet", notice, errors)
        return 0

    rel = results[0]
    url = rel.get("url") or st.get("release_url")
    text = rel.get("text") or ""
    if url and not text:
        text, berr = fetch_body(url)
        if berr:
            errors.append(berr)
    pub = rel.get("date")
    if not pub:
        # try dateline in body
        d = parse_mdy(text[:800]) if text else None
        pub = d.isoformat() if d else None
    if not pub:
        pub = now.date().isoformat()

    bars, yerr = yahoo_closes(cfg)
    if yerr:
        errors.append(yerr)
    sess = session_after(date.fromisoformat(pub), bars) if pub else None
    if sess is None:
        print("release found, waiting for next US regular session close", pub, url, errors)
        if mode != "dry":
            st.update(release_url=url, release_date=pub, announced=notice["title"] if notice else st.get("announced"))
            save_state(st, now)
        return 0

    close_day, close = sess
    parsed = parse_results(text) if text else {"cc_growth": None, "jp_cc_growth": None}
    j = judge(parsed.get("cc_growth"), parsed.get("jp_cc_growth"), close, cfg)
    title = title_for(j["verdict"])
    lines = score_lines(cfg, now, j, parsed.get("cc_growth"), parsed.get("jp_cc_growth"), close, close_day.isoformat(), rel, url or "")
    print(title)
    print("\n".join(lines))
    if mode == "dry":
        print("DRY RUN (no ntfy, no state write)")
        return 0
    emit(cfg, title, lines, j["priority"])
    st.update(
        fired=True,
        release_url=url,
        release_date=pub,
        judge_session=close_day.isoformat(),
        close=close,
        verdict=j["verdict"],
        parsed=parsed,
    )
    save_state(st, now)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
