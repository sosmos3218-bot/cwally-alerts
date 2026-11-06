#!/usr/bin/env python3
"""1810.HK Xiaomi official earnings / EV disclosure review. Not a trade signal.

Reads the public HKEX title search for stockId 1000195151 only.
Sends one cwally-port note per new results, board-date, or EV-delivery filing.
First run records what is already listed and does not send.
Daily buyback returns and monthly share-movement returns are ignored.
"""
from __future__ import annotations

import hashlib
import html
import io
import json
import re
import sys
import traceback
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from kr_calendar import KST  # noqa: E402
from ntfy_util import publish, resolve_server, resolve_topic, route_topic  # noqa: E402

NAME = "xmi1810"
CFG_PATH = ROOT / "configs" / f"{NAME}.json"
STATE_PATH = ROOT / "state" / f"{NAME}.json"
UA = "cwally-alerts xmi1810 (HKEX title search; github.com/sosmos3218-bot/cwally-alerts)"
HOST = "https://www1.hkexnews.hk"

TITLE = {
    "results": "[1810] 1810.HK 실적 공시 게시 | 사람 검토",
    "schedule": "[1810] 1810.HK 실적 이사회 일정 | 발표로 단정 금지",
    "schedule_change": "[1810] 1810.HK 실적 일정 변경 | 사람 검토",
    "schedule_cancel": "[1810] 1810.HK 실적 일정 취소 | 사람 검토",
    "ev": "[1810] 1810.HK EV 인도 공시 | 사람 검토",
}
OPS_FETCH = "[1810] 1810.HK 공시 조회 실패 | 사람 검토 보류"
OPS_PARSE = "[1810] 1810.HK 공시 파싱 실패 | 사람 검토 보류"

MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}
ROW_RE = re.compile(
    r'<td[^>]*class="[^"]*release-time[^"]*"[^>]*>.*?(\d{2}/\d{2}/\d{4}\s+\d{2}:\d{2}).*?'
    r'<td[^>]*class="[^"]*stock-short-code[^"]*"[^>]*>(.*?)</td>.*?'
    r'<td[^>]*class="[^"]*stock-short-name[^"]*"[^>]*>(.*?)</td>.*?'
    r'<div class="headline">(.*?)</div>.*?'
    r'<a href="([^"]+\.pdf)"[^>]*>\s*([^<]+?)\s*</a>',
    re.S | re.I,
)
EXCLUDE_RE = re.compile(
    r"next day disclosure|share buy-?back|share repurchase|repurchase program|"
    r"monthly return|movements in securities|share scheme|grant of awards|grant of options|"
    r"company secretar|poll results|notice of the annual general meeting|notice of agm|"
    r"form of proxy|articles of association|memorandum and articles|notification letter|"
    r"constitutional document",
    re.I,
)
RESULTS_RE = re.compile(
    r"\[(interim|quarterly|final|annual) results\]|results announcement|profit warning|positive profit alert",
    re.I,
)
HELD_RE = re.compile(r"will be held on\s+([A-Za-z]+)\s+(\d{1,2}),\s*(\d{4})", re.I)
CANCEL_RE = re.compile(r"\bcancell?ed\b|\bcancellation\b|will not be held|取消舉行|取消举行|取消召開", re.I)
POSTPONE_RE = re.compile(r"\bpostpon|\badjourn|延期|延遲|延后", re.I)
FORBID_RE = re.compile("[\u00b7\u318d\u2014\u2013]")


def load_cfg() -> dict:
    return json.loads(CFG_PATH.read_text(encoding="utf-8"))


def load_state(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    data.setdefault("seen", [])
    data.setdefault("fingerprints", [])
    data.setdefault("seeded", False)
    return data


def save_state(path: Path, st: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def textify(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = html.unescape(s).replace("\xa0", " ")
    return re.sub(r"\s+", " ", s).strip()


def clean(s: str) -> str:
    return FORBID_RE.sub(", ", s or "")


def check_text(title: str, body: str) -> None:
    bad = FORBID_RE.search((title or "") + (body or ""))
    if bad:
        raise AssertionError(f"forbidden char {bad.group(0)!r} in {title}")


def issuer_ok(code_text: str, name_text: str) -> bool:
    if "XIAOMI" not in textify(name_text).upper():
        return False
    tokens = set(re.findall(r"\d{4,5}", code_text or ""))
    bare = {t.lstrip("0") or "0" for t in tokens}
    return "1810" in bare or "01810" in tokens or "81810" in tokens


def classify(headline: str, doc: str) -> str | None:
    blob = f"{headline} {doc}"
    low = blob.lower()
    if RESULTS_RE.search(low):
        return "results"
    if "date of board meeting" in low:
        if CANCEL_RE.search(blob):
            return "schedule_cancel"
        if POSTPONE_RE.search(blob):
            return "schedule_change"
        return "schedule"
    if EXCLUDE_RE.search(blob):
        return None
    return "ev" if is_ev_title(doc) else None


def is_ev_title(doc: str) -> bool:
    t = (doc or "").lower()
    if "monthly return" in t or "buy" in t or "repurchase" in t:
        return False
    if re.search(r"deliver", t) and re.search(r"vehicle|electric|\bev\b", t):
        return True
    return bool("交付" in (doc or "") and re.search(r"车|車|电动|電動", doc or ""))


def parse_release(raw: str) -> datetime | None:
    """HKEX prints release time as DD/MM/YYYY HH:MM."""
    m = re.search(r"(\d{2})/(\d{2})/(\d{4})\s+(\d{2}):(\d{2})", raw or "")
    if not m:
        return None
    day, month, year, hour, minute = (int(x) for x in m.groups())
    try:
        return datetime(year, month, day, hour, minute)
    except ValueError:
        return None


def news_id_of(href: str) -> str:
    m = re.search(r"/(\d{10,})(?:_[A-Za-z0-9]+)?\.pdf$", href or "")
    return m.group(1) if m else ""


def abs_url(href: str) -> str:
    href = (href or "").split("?")[0]
    if href.startswith("http://") or href.startswith("https://"):
        return href
    if not href.startswith("/"):
        href = "/" + href
    return HOST + href


def parse_listing(page: str, stock_id: str) -> dict:
    found = re.search(r'id="stockId"[^>]*value="(\d+)"', page or "")
    if not found or found.group(1) != str(stock_id):
        return {"error": "stockId", "rows": []}
    marks = re.findall(r'class="[^"]*release-time', page)
    raw = ROW_RE.findall(page)
    if marks and len(raw) != len(marks):
        return {"error": "parse", "rows": []}
    if not raw:
        if re.search(r"no match|no record|0 record|no result", page, re.I):
            return {"error": "", "rows": []}
        return {"error": "parse", "rows": []}
    rows = []
    for rel, code, name, headline, href, doc in raw:
        if not issuer_ok(code, name):
            continue
        when = parse_release(textify(rel))
        if when is None:
            continue
        href = html.unescape(href)
        nid = news_id_of(href)
        title = textify(doc)
        head = textify(headline)
        kind = classify(head, title)
        url = abs_url(href)
        path = urllib.request.urlparse(url).path
        fp = hashlib.sha1(f"{nid}|{title.lower()}|{path}".encode()).hexdigest()
        rows.append({
            "id": f"hkex:{nid}" if nid else f"fp:{fp}",
            "fp": fp,
            "pub": when.strftime("%Y-%m-%d %H:%M"),
            "pub_date": when.date().isoformat(),
            "headline": head,
            "title": title,
            "url": url,
            "kind": kind,
        })
    if not rows:
        return {"error": "issuer", "rows": []}
    rows.sort(key=lambda r: (r["pub"], r["id"]))
    return {"error": "", "rows": rows}


def extract_board_date(text: str) -> str | None:
    """Only the English sentence 'will be held on Month D, YYYY'. Anything else is unextracted."""
    found = HELD_RE.findall(text or "")
    if len(found) != 1:
        return None
    month, day, year = found[0]
    mon = MONTHS.get(month.lower())
    if not mon:
        return None
    try:
        return datetime(int(year), mon, int(day)).strftime("%Y-%m-%d")
    except ValueError:
        return None


def refine_schedule(kind: str, text: str, prev: str | None) -> tuple[str, str | None]:
    if not str(kind).startswith("schedule"):
        return kind, None
    day = extract_board_date(text or "")
    if CANCEL_RE.search(text or ""):
        return "schedule_cancel", day
    if POSTPONE_RE.search(text or ""):
        return "schedule_change", day
    if day and prev and day != prev and kind == "schedule":
        return "schedule_change", day
    return kind, day


def pdf_to_text(data: bytes) -> str:
    if not data or not data.startswith(b"%PDF"):
        return ""
    try:
        from pypdf import PdfReader
    except ImportError:
        return ""
    try:
        reader = PdfReader(io.BytesIO(data))
        return "\n".join((p.extract_text() or "") for p in reader.pages[:2])
    except Exception:  # noqa: BLE001
        return ""


def try_extract(text: str) -> dict:
    """Optional single-hit extract. Zero or two hits = that field stays unextracted. Never guesses."""
    if not text:
        return {}
    specs = {
        "phone_ship_m": r"smartphone shipments reached ([0-9]+(?:\.[0-9]+)?) million",
        "ev_units": r"total deliveries of ([0-9,]+) vehicles",
        "seg_gpm": r"smart EV, AI and other new initiatives segment was ([0-9]+(?:\.[0-9]+)?)%",
        "seg_op": r"(loss|income) from operations[^.]{0,160}?RMB([0-9]+(?:\.[0-9]+)?) billion",
    }
    out = {}
    for key, pat in specs.items():
        hits = re.findall(pat, text, re.I)
        if len(hits) == 1:
            out[key] = hits[0]
    return out


def figure_line(text: str) -> str:
    got = try_extract(text)
    if not got:
        return "이번 문서 숫자: 미추출. 링크 원문을 볼 것. 추정하지 않음."
    bits = []
    if "phone_ship_m" in got:
        bits.append(f"스마트폰 출하 문장 1회: {got['phone_ship_m']} million")
    if "ev_units" in got:
        bits.append(f"인도 문장 1회: {got['ev_units']}대")
    if "seg_gpm" in got:
        bits.append(f"부문 매출총이익률 문장 1회: {got['seg_gpm']}%")
    if "seg_op" in got:
        side, amt = got["seg_op"]
        bits.append(f"부문 영업손익 문장 1회: {side} RMB{amt} billion")
    return "일부만 추출, 나머지 미추출. " + ". ".join(bits) + ". 추정으로 빈칸을 채우지 말 것."


CHECKLIST = (
    "사람 체크: 1) 스마트폰 출하량, ASP, 매출총이익률. "
    "2) EV 인도 대수와 그 기간. "
    "3) Smart EV, AI 및 기타 신규 사업 부문의 매출총이익률과 영업손익"
    "(income from operations, 매출총이익에서 영업비용을 뺀 값). "
    "순수 EV 손익, 전사 매출총이익, 조정순이익, IFRS 순이익과 섞지 말 것. "
    "4) 2026년 연간 EV 인도 목표 550,000대(2026-03-24 연간실적 공시의 목표 문장)와 비교는 "
    "이번 문서에 같은 정의의 공식 누적이 있을 때만. 없으면 진행률을 만들지 말 것."
)
HIST = (
    "과거 공식 문장(이번 분기 숫자가 아님): 2025년 인도 411,082대, 2026년 목표 550,000대(2026-03-24). "
    "2026-08-18 공시: 2분기 인도 104,199대, 1분기 인도 80,856대(두 문장을 합쳐 상반기 합계로 쓰지 말 것). "
    "2분기 스마트폰 출하 3,120만대, ASP 1,351위안, 스마트폰 매출총이익률 8.5%. "
    "같은 공시의 그 부문 2분기 매출총이익률 19.2%, 영업손실 26억위안. "
    "2025년 그 부문 영업이익은 +9억위안(적자 확정 아님). "
    "2026-08-18 공시에 Xiaomi SkyNomad 시리즈 이름이 있음. "
    "자사주 한도는 200억 홍콩달러(2026-05-26)이고 매수 완료가 아님."
)
WHAT = "1810.HK 샤오미 Class B 의 HKEX 공식 실적 공시, 이사회 일정, EV 인도 공시를 사람 검토용으로 1번 알림. 매수/매도 신호 아님."


def body(what_status: str, action: str, later: str) -> str:
    lines = [
        "이게 뭐 알림: " + WHAT,
        "지금 결과/상태: " + what_status,
        "지금 할 일: " + action,
        "나중에: " + later,
        "익절/줄일 때: 이 알림으로 익절이나 비중을 바꾸지 말 것. 보유 수량이 입력돼 있지 않음.",
        "하지 말 것: 이사회 예정일만 보고 실적이 나왔다고 단정하기. "
        "한도를 20억 홍콩달러로 기억하거나 매수가 끝났다고 단정하기. "
        "확인하지 않은 월간 인도, 주가, PER, 목표주가로 판단하기. "
        "부문 영업손실을 순수 EV 손실로 읽기. 언론 기사를 이 공시 대신 쓰기.",
        CHECKLIST,
        HIST,
    ]
    return "\n".join(lines)


def message(row: dict, kind: str, board_day: str | None, prev: str | None, pdf_text: str) -> tuple[str, str, int]:
    title = TITLE[kind]
    pub = row["pub"]
    link = f"제목: {row['title']}. 링크: {row['url']}"
    figures = figure_line(pdf_text if kind in {"results", "ev"} else "")
    if kind == "results":
        status = (
            f"HKEX 게시 시각 {pub} (거래소 표기, 일/월/연). 실적 공시 문서가 게시됨. "
            f"게시 시각은 실적 기간이 아님. {figures} {link}"
        )
        action = "원문 링크를 열고 아래 체크를 직접 할 것. 매수나 매도 지시가 아님."
        later = "같은 뉴스아이디는 다시 안 보냄. 정정이나 다음 분기 공시가 새 아이디로 나오면 1번 더 보냄."
    elif kind == "ev":
        status = (
            f"HKEX 게시 시각 {pub}. EV 인도 제목의 공시가 게시됨. "
            "월간 주식변동 보고서(Monthly Return)는 인도 실적이 아니라 이 알림에서 제외함. "
            f"{figures} {link}"
        )
        action = "원문에서 인도 대수와 기간을 읽을 것. 숫자가 안 보이면 미추출로 두고 추정하지 말 것."
        later = "같은 문서 주소는 다시 안 보냄. 월간 언론 인도는 이 알림이 보지 않음."
    else:
        shown = board_day or "미추출"
        prev_s = prev or "없음"
        status = (
            f"HKEX 게시 시각 {pub}. 문서 종류: 이사회 일정. 게시 시각은 실적 발표 시각이 아님. "
            f"원문 이사회 예정일: {shown}. 직전 저장 예정일: {prev_s}. "
            "예정일이 있어도 그 날 실적이 공개됐다고 단정하지 않음. "
            f"이번 문서 숫자: 미추출. {link}"
        )
        if kind == "schedule_cancel":
            action = "일정이 취소된 공시인지 원문 문장으로 확인할 것. 발표 완료로 읽지 말 것."
        elif kind == "schedule_change":
            action = "이전 예정일과 다른지 원문으로 확인할 것. 날짜를 못 읽었으면 변경으로 단정하지 말 것."
        else:
            action = "이사회 날짜만 적힌 일정 공시. 실적 숫자가 나온 것으로 보지 말 것."
        later = "같은 뉴스아이디는 다시 안 보냄. 날짜가 바뀌거나 취소 공시가 새로 나오면 1번 더 보냄."
    return title, clean(body(status, action, later)), 3


def ops_body(status: str) -> str:
    return clean(body(
        status,
        "HKEX 제목 검색에서 1810 샤오미 공시를 직접 볼 것. 이 안내를 실적이나 매매 신호로 읽지 말 것.",
        "다음 슬롯에서 다시 읽음. 실패한 목록의 공시는 성공한 다음 실행에서 판단함.",
    ))


def _clear_stale_board(cfg: dict, st: dict, rows: list[dict]) -> None:
    known = cfg.get("reference_board") or {}
    known_id = "hkex:" + str(known.get("news_id") or "")
    known_day = str(known.get("announcement_date") or "")
    newer = [
        r for r in rows
        if str(r.get("kind") or "").startswith("schedule")
        and r.get("pub_date", "") > known_day
        and r.get("id") != known_id
    ]
    if newer:
        st["last_board_date"] = None
        st["last_board_date_source"] = "cleared: newer schedule on first seed, date not read"


def stale_feed(rows: list[dict], today, max_age: int) -> bool:
    dates = []
    for r in rows:
        try:
            dates.append(datetime.strptime(r["pub_date"], "%Y-%m-%d").date())
        except (KeyError, ValueError):
            continue
    if not dates:
        return False
    return (today - max(dates)).days > max_age


def _decide(cfg: dict, st: dict, now: datetime, parsed: dict, pdf_text_fn) -> tuple[list[tuple], str | None]:
    today = now.date()
    err = parsed.get("error") or ""
    rows = parsed.get("rows") or []
    if err == "parse":
        return [], OPS_PARSE
    if err:
        return [], OPS_FETCH
    if stale_feed(rows, today, int(cfg.get("stale_days") or 40)):
        return [], OPS_FETCH
    relevant = [r for r in rows if r.get("kind")]
    seen = set(st.get("seen") or [])
    fps = set(st.get("fingerprints") or [])
    if not st.get("seeded"):
        _clear_stale_board(cfg, st, relevant)
        for r in relevant:
            seen.add(r["id"])
            fps.add(r["fp"])
        st["seen"] = sorted(seen)
        st["fingerprints"] = sorted(fps)
        st["seeded"] = True
        st["seeded_at"] = now.strftime("%Y-%m-%d %H:%M KST")
        print(f"first seed: {len(relevant)} relevant filing(s), no send")
        return [], None
    out = []
    for r in relevant:
        if r["id"] in seen or r["fp"] in fps:
            continue
        kind = r["kind"]
        pdf_text = ""
        board_day = None
        baseline = st.get("last_board_date") or None
        if str(kind).startswith("schedule"):
            pdf_text = pdf_text_fn(r["url"]) or ""
            kind, board_day = refine_schedule(kind, pdf_text, baseline)
            if board_day:
                st["last_board_date"] = board_day
                st["last_board_date_source"] = r["id"]
        title, msg, pri = message(r, kind, board_day, baseline, pdf_text)
        check_text(title, msg)
        out.append((title, msg, pri, r["id"]))
        seen.add(r["id"])
        fps.add(r["fp"])
        print(f"new {r['id']} {kind} {r['pub']} {r['title']}")
    st["seen"] = sorted(seen)
    st["fingerprints"] = sorted(fps)
    if not out:
        print("새 실적/EV/일정 공시 없음")
    return out, None


def fetch_html(url: str, timeout: int = 25) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read(2_000_001)
    if len(data) > 2_000_000:
        raise RuntimeError("page too large")
    return data.decode("utf-8", "replace")


def fetch_pdf(url: str, limit: int, timeout: int = 20) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/pdf"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read(limit + 1)


def ended(cfg: dict, now: datetime) -> bool:
    end = datetime.strptime(cfg["ends_kst"], "%Y-%m-%d %H:%M").replace(tzinfo=KST)
    return now > end


def run(mode: str, now: datetime, cfg: dict, *, pub=publish, fetch=fetch_html, fetch_pdf_fn=fetch_pdf,
        state_path: Path | None = None) -> int:
    path = state_path or STATE_PATH
    dry = mode == "dry"
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    print(f"{stamp} {NAME} mode={mode}")
    if ended(cfg, now):
        print(f"ended {cfg['ends_kst']}: silent")
        return 0
    before = path.read_bytes() if path.is_file() else None
    st = load_state(path)
    try:
        page = fetch(cfg["search_url"])
        parsed = parse_listing(page, cfg["hkex_stock_id"])
    except Exception as exc:  # noqa: BLE001
        parsed = {"error": "fetch", "rows": [], "detail": str(exc)[:160]}
        print(f"fetch error: {exc}")

    def pdf_text_fn(url: str) -> str:
        try:
            data = fetch_pdf_fn(url, int(cfg.get("pdf_max_bytes") or 600000))
        except Exception as exc:  # noqa: BLE001
            print(f"pdf skip {url}: {exc}")
            return ""
        if not data or len(data) > int(cfg.get("pdf_max_bytes") or 600000):
            return ""
        return pdf_to_text(data)

    out, ops_title = _decide(cfg, st, now, parsed, pdf_text_fn)
    today = now.strftime("%Y-%m-%d")
    ops = None
    if ops_title and st.get("ops_day") != today:
        detail = parsed.get("detail") or parsed.get("error") or "stale or empty feed"
        if stale_feed(parsed.get("rows") or [], now.date(), int(cfg.get("stale_days") or 40)):
            detail = f"목록 최신 게시가 {cfg.get('stale_days')}일보다 오래됨. 실적 알림으로 쓰지 않음."
        ops = (ops_title, ops_body(f"{detail}. state 의 공시 목록은 성공한 읽기만 갱신함."))
        st["ops_day"] = today
    elif ops_title:
        print("ops already sent today")
    topic = resolve_topic("port", cfg.get("ntfy_topic"))
    server = resolve_server(cfg.get("ntfy_server"))
    for title, msg, pri, key in out:
        print(f"TITLE: {title}\nPRIORITY: {pri} TOPIC: {route_topic(topic, title)} DEDUP: {key}\n{msg}\n")
    if ops:
        print(f"OPS TITLE: {ops[0]} -> {route_topic(topic, ops[0])}\n{ops[1]}\n")
    if dry:
        print("DRY RUN: no publish, state not written")
        if before is None:
            if path.is_file() and state_path is not None and path != STATE_PATH:
                path.unlink()
        elif path.is_file() and path.read_bytes() != before and state_path is not None:
            path.write_bytes(before)
        return 0
    for title, msg, pri, key in out:
        pub(topic, title, msg, pri, ["memo"], server, config_name=NAME, dedup_key=key)
    if ops:
        try:
            pub(resolve_topic("ops"), ops[0], ops[1], 3, ["warning"], server, config_name=NAME)
        except Exception as exc:  # noqa: BLE001
            print(f"ops notice failed: {exc}")
    if out or ops or st.get("seeded"):
        st["updated_at"] = stamp
        save_state(path, st)
    return 0


def _row(nid: str, pub: str, headline: str, doc: str, kind: str | None = None) -> dict:
    href = f"/listedco/listconews/sehk/2026/1106/{nid}.pdf"
    title = textify(doc)
    head = textify(headline)
    fp = hashlib.sha1(f"{nid}|{title.lower()}|{href}".encode()).hexdigest()
    return {
        "id": f"hkex:{nid}",
        "fp": fp,
        "pub": pub,
        "pub_date": pub[:10],
        "headline": head,
        "title": title,
        "url": HOST + href,
        "kind": classify(head, title) if kind is None else kind,
    }


def _page(rows_html: str, stock_id: str = "1000195151") -> str:
    return (
        f'<html><input id="stockId" type="hidden" value="{stock_id}" />'
        f"<table>{rows_html}</table></html>"
    )


def _tr(when: str, code: str, name: str, headline: str, href: str, doc: str) -> str:
    return (
        "<tr>"
        f'<td class="release-time"><span class="mobile-list-heading">Release Time: </span>{when}</td>'
        f'<td class="stock-short-code"><span class="mobile-list-heading">Stock Code: </span>{code}</td>'
        f'<td class="stock-short-name"><span class="mobile-list-heading">Stock Short Name: </span>{name}</td>'
        f'<td><div class="headline">{headline}</div>'
        f'<div class="doc-link"><a href="{href}">{doc}</a></div></td>'
        "</tr>"
    )


def selftest() -> int:
    cfg = load_cfg()
    assert cfg["ticker"] == "1810.HK"
    assert issuer_ok("01810 81810", "XIAOMI-W XIAOMI-WR")
    assert issuer_ok("1810", "XIAOMI-W")
    assert not issuer_ok("00700", "TENCENT")
    assert not issuer_ok("01810", "TENCENT")
    assert classify("Announcements and Notices - [Quarterly Results]", "RESULTS ANNOUNCEMENT FOR THE THREE MONTHS ENDED MARCH 31, 2026") == "results"
    assert classify("Announcements and Notices - [Interim Results]", "RESULTS ANNOUNCEMENT FOR THE THREE AND SIX MONTHS ENDED JUNE 30, 2026") == "results"
    assert classify("Announcements and Notices - [Date of Board Meeting]", "DATE OF BOARD MEETING") == "schedule"
    assert classify("Announcements and Notices - [Date of Board Meeting]", "POSTPONEMENT OF DATE OF BOARD MEETING") == "schedule_change"
    assert classify("[Date of Board Meeting]", "CANCELLATION OF BOARD MEETING") == "schedule_cancel"
    assert classify("Next Day Disclosure Returns - [Share Buyback]", "Next Day Disclosure Return") is None
    assert classify("Announcements and Notices - [Other - Miscellaneous]", "VOLUNTARY ANNOUNCEMENT - HK$20 BILLION ON-MARKET SHARE REPURCHASE PROGRAM") is None
    assert classify("Monthly Returns", "Monthly Return of Equity Issuer on Movements in Securities for the month ended 31 October 2026") is None
    assert classify("Financial Statements - [Interim/Half-Year Report]", "2026 Interim Report") is None
    assert classify("Announcements and Notices - [Results of AGM]", "POLL RESULTS OF THE ANNUAL GENERAL MEETING HELD ON JUNE 2, 2026") is None
    assert classify("Announcements and Notices - [Other]", "VOLUNTARY ANNOUNCEMENT VEHICLE DELIVERY UPDATE") == "ev"
    assert classify("Announcements and Notices - [Profit Warning]", "PROFIT WARNING") == "results"
    # Date format is day/month/year. 05/11/2026 is 5 November, not 11 May.
    assert parse_release("05/11/2026 16:31") == datetime(2026, 11, 5, 16, 31)
    assert parse_release("23/10/2026 19:01") == datetime(2026, 10, 23, 19, 1)
    held = "a meeting of the Board of the Company will be held on November 24, 2026 (Tuesday)"
    assert extract_board_date(held) == "2026-11-24"
    assert extract_board_date("will be held on November 24, 2026 and will be held on December 2, 2026") is None
    assert refine_schedule("schedule", held, "2026-11-24") == ("schedule", "2026-11-24")
    changed = refine_schedule("schedule", "will be held on December 2, 2026", "2026-11-24")
    assert changed == ("schedule_change", "2026-12-02"), changed
    assert refine_schedule("schedule", "The meeting is cancelled and will not be held.", "2026-11-24")[0] == "schedule_cancel"
    assert refine_schedule("schedule", "date to be confirmed", "2026-11-24") == ("schedule", None)
    assert try_extract("noise 50000 deliveries and price 30.4 PE 21") == {}
    one = try_extract("In the second quarter of 2026, we achieved total deliveries of 104,199 vehicles, up 28.2%.")
    assert one == {"ev_units": "104,199"}, one
    two = try_extract("total deliveries of 104,199 vehicles. total deliveries of 80,856 vehicles.")
    assert "ev_units" not in two
    assert figure_line("garbage") .startswith("이번 문서 숫자: 미추출")
    assert "30.4" not in figure_line("price 30.4 PE 21 target 34")
    assert "336" not in figure_line("Jan-Oct 336000")

    xiaomi_results = _tr(
        "18/08/2026 17:25", "01810<br/>81810", "XIAOMI-W<br/>XIAOMI-WR",
        "Announcements and Notices - [Interim Results]",
        "/listedco/listconews/sehk/2026/0818/2026081801014.pdf",
        "RESULTS ANNOUNCEMENT FOR THE THREE AND SIX MONTHS ENDED JUNE 30, 2026",
    )
    tencent = _tr(
        "18/08/2026 17:25", "00700", "TENCENT",
        "Announcements and Notices - [Interim Results]",
        "/listedco/listconews/sehk/2026/0818/2026081899999.pdf",
        "RESULTS ANNOUNCEMENT",
    )
    buy = _tr(
        "05/11/2026 16:31", "01810 81810", "XIAOMI-W",
        "Next Day Disclosure Returns - [Share Buyback]",
        "/listedco/listconews/sehk/2026/1105/2026110500999.pdf",
        "Next Day Disclosure Return",
    )
    parsed = parse_listing(_page(tencent + xiaomi_results + buy), "1000195151")
    assert parsed["error"] == ""
    assert [r["id"] for r in parsed["rows"]] == ["hkex:2026081801014", "hkex:2026110500999"]
    assert [r["kind"] for r in parsed["rows"]] == ["results", None]
    assert parse_listing(_page(tencent), "1000195151")["error"] == "issuer"
    assert parse_listing(_page(xiaomi_results), "999")["error"] == "stockId"
    assert parse_listing("<html>no record found</html>", "1000195151")["error"] == "stockId"
    broken = _page('<td class="release-time">05/11/2026 16:31</td>')
    assert parse_listing(broken, "1000195151")["error"] == "parse"

    now = datetime(2026, 11, 6, 9, 0, tzinfo=KST)
    board = _row("2026102301169", "2026-10-23 19:01", "[Date of Board Meeting]", "DATE OF BOARD MEETING")
    results = _row("2026081801014", "2026-08-18 17:25", "[Interim Results]", "RESULTS ANNOUNCEMENT FOR THE THREE AND SIX MONTHS")
    fresh_buy = _row("2026110500999", "2026-11-05 16:31", "[Share Buyback]", "Next Day Disclosure Return")
    assert fresh_buy["kind"] is None and results["kind"] == "results" and board["kind"] == "schedule"
    st: dict = {"seeded": False, "seen": [], "fingerprints": [], "last_board_date": "2026-11-24"}
    listing = {"error": "", "rows": [results, board, fresh_buy]}
    out, ops = _decide(cfg, st, now, listing, lambda url: "")
    assert out == [] and ops is None and st["seeded"] and "hkex:2026081801014" in st["seen"]
    assert "hkex:2026110500999" not in st["seen"]  # buyback is not relevant, so not seeded as a send key
    # canonical dedup: same id again does not send; a new results id sends once
    again = _row("2026112400001", "2026-11-24 18:05", "[Quarterly Results]", "RESULTS ANNOUNCEMENT FOR THE THREE AND NINE MONTHS")
    out, ops = _decide(cfg, st, now, {"error": "", "rows": [results, board, fresh_buy, again]}, lambda url: "")
    assert ops is None and len(out) == 1 and out[0][0] == TITLE["results"] and out[0][3] == "hkex:2026112400001"
    assert "실적 공시 문서가 게시됨" in out[0][1] and "미추출" in out[0][1] and "1810.HK" in out[0][0]
    assert again["url"] in out[0][1] and "스마트폰" in out[0][1] and "영업손익" in out[0][1]
    assert "30.4" not in out[0][1] and "꺼지는 때" not in out[0][1]
    out2, _ = _decide(cfg, st, now, {"error": "", "rows": [results, again]}, lambda url: "")
    assert out2 == []  # same canonical id

    # fingerprint dedup when the news id matches an already stored fp
    dup = dict(again)
    dup["id"] = "hkex:other"
    dup["fp"] = again["fp"]
    assert _decide(cfg, st, now, {"error": "", "rows": [dup, fresh_buy]}, lambda url: "")[0] == []

    # schedule vs publication, change, cancel, unextracted date
    st2 = {"seeded": True, "seen": [], "fingerprints": [], "last_board_date": "2026-11-24"}
    new_board = _row("2026112000002", "2026-11-20 19:00", "[Date of Board Meeting]", "DATE OF BOARD MEETING")
    texts = {new_board["url"]: "will be held on December 2, 2026"}
    out, ops = _decide(cfg, st2, now, {"error": "", "rows": [new_board, fresh_buy]}, lambda url: texts.get(url, ""))
    assert ops is None and out[0][0] == TITLE["schedule_change"]
    assert "2026-12-02" in out[0][1] and "2026-11-24" in out[0][1]
    assert "실적 발표 시각이 아님" in out[0][1] and "실적 공시 문서가 게시됨" not in out[0][1]
    same = _row("2026112100003", "2026-11-21 19:00", "[Date of Board Meeting]", "DATE OF BOARD MEETING")
    out, _ = _decide(cfg, st2, now, {"error": "", "rows": [same]}, lambda url: "will be held on December 2, 2026")
    assert out[0][0] == TITLE["schedule"]
    blank = _row("2026112200004", "2026-11-22 19:00", "[Date of Board Meeting]", "DATE OF BOARD MEETING")
    out, _ = _decide(cfg, st2, now, {"error": "", "rows": [blank]}, lambda url: "")
    assert out[0][0] == TITLE["schedule"] and "원문 이사회 예정일: 미추출" in out[0][1]
    assert "일정 변경" not in out[0][0]
    cancel = _row("2026112300005", "2026-11-23 19:00", "[Date of Board Meeting]", "DATE OF BOARD MEETING")
    out, _ = _decide(cfg, st2, now, {"error": "", "rows": [cancel]}, lambda url: "The board meeting will not be held and is cancelled.")
    assert out[0][0] == TITLE["schedule_cancel"]

    # parser failure still has link and checklist, and does not invent user figures
    bad_text = "October deliveries 50000, price 30.4, PE 21/27, TP 34, SOTP 32-38, cumulative 336000"
    line = figure_line(bad_text)
    msg = message(again, "results", None, None, bad_text)[1]
    assert "미추출" in line and "미추출" in msg and again["url"] in msg
    for banned in ("30.4", "336000", "50000", "SOTP"):
        assert banned not in msg, banned
    assert "550,000" in msg  # official FY2025 target reference, not the unverified October figure

    # stale / empty / fetch failure do not become a financial alert
    old = _row("2026032400608", "2026-03-24 18:00", "[Final Results]", "ANNUAL RESULTS ANNOUNCEMENT")
    st3 = {"seeded": True, "seen": [], "fingerprints": [], "last_board_date": "2026-11-24", "ops_day": ""}
    out, ops = _decide(cfg, st3, now, {"error": "", "rows": [old]}, lambda url: "")
    assert out == [] and ops == OPS_FETCH and "hkex:2026032400608" not in st3.get("seen", [])
    st4 = {"seeded": False, "seen": [], "fingerprints": [], "last_board_date": "2026-11-24"}
    out, ops = _decide(cfg, st4, now, {"error": "", "rows": [fresh_buy]}, lambda url: "")
    assert out == [] and ops is None and st4["seeded"] is True and st4["seen"] == []
    st5 = {"seeded": False, "seen": [], "fingerprints": []}
    out, ops = _decide(cfg, st5, now, {"error": "fetch", "rows": []}, lambda url: "")
    assert out == [] and ops == OPS_FETCH and st5["seeded"] is False
    out, ops = _decide(cfg, st5, now, {"error": "parse", "rows": []}, lambda url: "")
    assert out == [] and ops == OPS_PARSE

    # newer schedule already on the first page clears the preset date instead of calling it a change later
    newer = _row("2026110100009", "2026-11-01 19:00", "[Date of Board Meeting]", "DATE OF BOARD MEETING")
    st6 = {"seeded": False, "seen": [], "fingerprints": [], "last_board_date": "2026-11-24"}
    _decide(cfg, st6, now, {"error": "", "rows": [newer, fresh_buy]}, lambda url: "ignored on seed")
    assert st6["last_board_date"] is None and "hkex:2026110100009" in st6["seen"]

    # dry writes nothing and does not publish
    import tempfile
    calls = []

    def spy(*a, **k):
        calls.append(a[1] if len(a) > 1 else k)

    tmp = Path(tempfile.mkdtemp()) / "xmi1810.json"
    save_state(tmp, {"seeded": True, "seen": [], "fingerprints": [], "last_board_date": "2026-11-24", "ops_day": ""})
    raw = tmp.read_bytes()
    rc = run("dry", now, cfg, pub=spy, fetch=lambda url: _page(xiaomi_results + buy), fetch_pdf_fn=lambda url, limit: b"", state_path=tmp)
    assert rc == 0 and calls == [] and tmp.read_bytes() == raw

    # past end date does not fetch
    fetched = []
    rc = run("dry", datetime(2027, 11, 25, 0, 0, tzinfo=KST), cfg, pub=spy,
             fetch=lambda url: fetched.append(url) or "", state_path=tmp)
    assert rc == 0 and fetched == [] and calls == []

    for title in TITLE.values():
        assert title.startswith("[1810] ") and "1810.HK" in title
        check_text(title, "")
    check_text(OPS_FETCH, ops_body("x"))
    check_text(OPS_PARSE, ops_body("x"))
    from ntfy_util import append_ends
    ended_line = append_ends("본문", NAME)
    assert "2027-11-24 23:59" in ended_line and "꺼지는 때" in ended_line
    print("selftest ok")
    return 0


def main() -> int:
    args = sys.argv[1:]
    mode = (args[0] if args and not args[0].startswith("--") else "auto").lower()
    if mode == "selftest":
        return selftest()
    if mode not in {"auto", "dry"}:
        print(f"usage: {NAME}_monitor.py <auto|dry|selftest>")
        return 2
    cfg = load_cfg()
    now = datetime.now(KST)
    try:
        return run(mode, now, cfg)
    except Exception as exc:  # noqa: BLE001
        print(traceback.format_exc(), file=sys.stderr)
        if mode != "auto":
            return 1
        try:
            stamp = now.strftime("%Y-%m-%d %H:%M KST")
            publish(
                resolve_topic("ops"), OPS_FETCH, ops_body(f"{stamp} 스크립트 오류 {type(exc).__name__}: {str(exc)[:180]}"),
                3, ["warning"], resolve_server(cfg.get("ntfy_server")), config_name=NAME,
            )
        except Exception:  # noqa: BLE001
            return 1
        return 0


if __name__ == "__main__":
    sys.exit(main())
