#!/usr/bin/env python3
"""[068270] 셀트리온 2026년 4분기 연결 잠정실적 영업이익 6,000억원 1회 판정 (2026-11-20 pt44u).

Attached to existing weekday slots 09:00 / 15:20 / 19:00 (configs/ctq4.json).
When 연결재무제표기준영업(잠정)실적(공정공시) for 2026-10-01~2026-12-31 or (2026.4Q)/(26.4Q)
appears in the Naver notice feed, compare consolidated 영업이익 당해실적 to 6,000억원
with exact unit conversion (no floor/round), send once, then end permanently.
No 정정 re-alert. Outlook, 15% annual-change, and separate-company 영업(잠정)실적 are ignored.
No filing by 2027-03-10 19:00 KST -> one 미게시 notice (cwally-ops).
Parse/fetch failure -> [OPS] [068270] ... 판정 유보 once per day, notified stays false.

Modes: auto | dry [--now "YYYY-MM-DD HH:MM"] (print only, no ntfy, no state) | selftest.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from naver_api import notices  # noqa: E402
from ntfy_util import publish, resolve_server, resolve_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
CFG_PATH = ROOT / "configs" / "ctq4.json"
STATE_PATH = ROOT / "state" / "ctq4.json"
THRESHOLD = Decimal("6000")
CORRECTION_NOTE = "이후 정정 공시는 다시 보지 않음"
UNIT_RE = re.compile(r"단위\s*[:：]\s*(백만원|억원|천원|원)")
OP_RE = re.compile(
    r"영업이익\s+당해실적\s+([△(]?\s*[\d,]+(?:\.\d+)?\)?|[-]?\s*[\d,]+(?:\.\d+)?)",
    re.S,
)
ISO_DANGI_RE = re.compile(
    r"당기실적[^\d누전]{0,40}2026-10-01\s*[~\-]\s*2026-12-31"
)
MARK_DANGI_RE = re.compile(
    r"당기실적[\s\S]{0,220}\(?'?(?:2026|26)\s*\.\s*4\s*Q"
)


def read_state() -> dict:
    if not STATE_PATH.exists():
        return {"notified": False, "empty_notified": False, "last_key": None}
    return json.loads(STATE_PATH.read_text(encoding="utf-8"))


def save_state(state: dict, now: datetime) -> None:
    state["updated_at"] = now.isoformat(timespec="seconds")
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def send(cfg: dict, kind: str, title: str, lines: list[str], prio: int) -> None:
    topic = resolve_topic(kind, cfg.get("ntfy_topic") if kind == "port" else cfg.get("ntfy_ops_topic"))
    publish(topic, title, "\n".join(lines), prio,
            ["warning"] if kind == "ops" else ["chart_with_upwards_trend"],
            resolve_server(cfg.get("ntfy_server")))


def in_window(cfg: dict, now: datetime) -> bool:
    if now.year != int(cfg["year_guard"]):
        return False
    day = now.strftime("%Y-%m-%d")
    if not (cfg["window_start"] <= day <= cfg["window_end"]):
        return False
    raw = cfg.get("ends_kst") or ""
    try:
        end = datetime.strptime(raw.replace("T", " ").split(" KST")[0].strip(), "%Y-%m-%d %H:%M").replace(tzinfo=KST)
    except ValueError:
        return True
    return now <= end


def compact(text: str) -> str:
    text = re.sub(r"\s*\|\s*", " ", text)
    text = re.sub(r"[ \t]+", " ", text)
    return text


def is_consol_prelim(title: str) -> bool:
    if re.search(r"전망|손익구조|장래사업|경영계획", title):
        return False
    if not re.search(r"연결재무제표\s*기준", title):
        return False
    if not re.search(r"잠정", title):
        return False
    if not re.search(r"실적", title):
        return False
    if not re.search(r"공정공시", title):
        return False
    return True


def is_target_period(title: str, text: str) -> bool:
    blob = compact(title + "\n" + text)
    if ISO_DANGI_RE.search(blob):
        return True
    if MARK_DANGI_RE.search(blob):
        return True
    return False


def detect_unit(text: str) -> str | None:
    blob = compact(text)
    # Prefer the 연결실적내용 / 실적내용 table unit, not a later footnote.
    section = re.search(r"(연결실적내용|실적내용)[\s\S]{0,400}?단위\s*[:：]\s*(백만원|억원|천원|원)", blob)
    if section:
        return section.group(2)
    hit = UNIT_RE.search(blob)
    return hit.group(1) if hit else None


def parse_number(raw: str) -> Decimal:
    s = raw.strip().replace(",", "").replace(" ", "")
    sign = Decimal(1)
    if s.startswith("△"):
        sign = Decimal(-1)
        s = s[1:]
    if s.startswith("(") and s.endswith(")"):
        sign = Decimal(-1)
        s = s[1:-1]
    if s.startswith("-"):
        sign = Decimal(-1)
        s = s[1:]
    return sign * Decimal(s)


def to_eok(amount: Decimal, unit: str) -> Decimal:
    if unit == "억원":
        return amount
    if unit == "백만원":
        return amount / Decimal(100)
    if unit == "천원":
        return amount / Decimal(10000)
    if unit == "원":
        return amount / Decimal(100000000)
    raise ValueError(f"unknown unit {unit}")


def extract_op_current(text: str) -> tuple[Decimal, str, Decimal]:
    """Return (raw_amount, unit, eok) for 영업이익 당해실적. Raises on failure."""
    blob = compact(text)
    unit = detect_unit(blob)
    if not unit:
        raise ValueError("unit not found")
    hit = OP_RE.search(blob)
    if not hit:
        raise ValueError("영업이익 당해실적 not found")
    raw = parse_number(hit.group(1))
    return raw, unit, to_eok(raw, unit)


def format_raw(amount: Decimal, unit: str) -> str:
    if amount == amount.to_integral_value():
        body = f"{int(amount):,}"
    else:
        body = f"{amount:,}"
    return f"{body}{unit}"


def format_eok(eok: Decimal) -> str:
    q = eok.normalize()
    if q == q.to_integral_value():
        return f"{int(q):,}억원"
    text = f"{q:,.10f}".rstrip("0").rstrip(".")
    return f"{text}억원"


def source_link(item: dict, cfg: dict) -> str:
    blob = " ".join([
        str(item.get("url") or ""),
        str(item.get("title") or ""),
        str(item.get("text") or ""),
    ])
    m = re.search(r"rcpNo=(\d{14})", blob)
    dart_search = cfg.get("dart_search") or "https://dart.fss.or.kr/dsab007/main.do"
    naver = item.get("url") or f"https://m.stock.naver.com/domestic/stock/{cfg['ticker']}/notice"
    if m:
        return f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={m.group(1)}"
    return f"{naver} | DART 검색 {dart_search}"


def filing_key(item: dict) -> str:
    return str(item.get("id") or "") + "|" + str(item.get("datetime") or "") + "|" + str(item.get("title") or "")


def is_q4_prelim(item: dict) -> bool:
    title, text = item.get("title") or "", item.get("text") or ""
    return is_consol_prelim(title) and is_target_period(title, text)


def verdict(eok: Decimal, threshold: Decimal) -> str:
    return "달성" if eok >= threshold else "미달"


def result_title(cfg: dict, label: str) -> str:
    return f"{cfg['title_prefix']} 셀트리온 4Q 잠정 영업이익 6,000억 {label}"


def defer_title(cfg: dict) -> str:
    return f"[OPS] {cfg['title_prefix']} 셀트리온 4Q 잠정 판정 유보"


def empty_title(cfg: dict) -> str:
    return f"{cfg['title_prefix']} 셀트리온 4Q 잠정 미게시"


def result_lines(cfg: dict, item: dict, raw: Decimal, unit: str, eok: Decimal, label: str, now: datetime) -> list[str]:
    link = source_link(item, cfg)
    return [
        "이게 뭐 알림: 셀트리온(068270) 2026년 4분기 연결 잠정실적 영업이익이 회사 목표 6,000억원 이상인지 한 번 판정합니다. 투자 권유가 아닙니다.",
        (
            f"지금 결과/상태: {label}. 연결 영업이익 당해실적 원문 {format_raw(raw, unit)} "
            f"(환산 {format_eok(eok)}), 기준 6,000억원 이상. 누계실적은 쓰지 않음. "
            f"공시 {item.get('title', '')} ({(item.get('datetime') or '')[:19]}). 원문 {link}. "
            f"{CORRECTION_NOTE}. 조회 {now:%Y-%m-%d %H:%M} KST."
        ),
        "지금 할 일: 원문을 확인하고 보유 판단을 직접 하세요.",
        "하지 말 것: 잠정 숫자를 확정으로 보기, 연간 누계나 별도 실적, 전망 공시를 이 목표에 쓰기. 투자 권유 아님.",
    ]


def defer_lines(cfg: dict, now: datetime, detail: str, item: dict | None) -> list[str]:
    link = source_link(item, cfg) if item else (
        f"https://m.stock.naver.com/domestic/stock/{cfg['ticker']}/notice | DART 검색 {cfg.get('dart_search')}"
    )
    return [
        f"{now:%Y-%m-%d %H:%M} KST | OPS | ctq4 4분기 잠정",
        "이게 뭐 알림: 셀트리온(068270) 4분기 잠정실적 판정을 이번 실행에서는 내리지 못했습니다. 투자 판단 알림이 아닙니다. 숫자는 추정하지 않습니다.",
        f"지금 상태: {detail}. notified는 그대로 false이며 다음 실행에서 다시 봅니다. 원문 {link}. {CORRECTION_NOTE}.",
        "지금 할 일: 원문을 직접 확인하세요.",
    ]


def empty_lines(cfg: dict, now: datetime) -> list[str]:
    return [
        "이게 뭐 알림: 셀트리온(068270) 2026년 4분기 연결 잠정실적 감시 창이 끝났습니다. 매매 신호가 아닙니다.",
        (
            f"지금 결과/상태: {cfg['window_start']}~{cfg['window_end']} 동안 2026년 4분기 "
            f"연결재무제표기준영업(잠정)실적 공시를 찾지 못했습니다. 조회 {now:%Y-%m-%d %H:%M} KST. "
            f"https://m.stock.naver.com/domestic/stock/{cfg['ticker']}/notice | DART 검색 {cfg.get('dart_search')}."
        ),
        "지금 할 일: 공시 일정을 직접 확인하세요.",
    ]


def run(
    cfg: dict,
    state: dict,
    now: datetime,
    mode: str,
    rows: list[dict] | None = None,
    fetch_exc: Exception | None = None,
    emit=None,
    persist=None,
) -> int:
    dry = mode == "dry"
    day = now.strftime("%Y-%m-%d")
    threshold = Decimal(str(cfg.get("threshold_eok") or "6000"))

    def out(kind: str, title: str, lines: list[str], prio: int) -> None:
        if emit is not None:
            emit(kind, title, lines, prio)
            return
        if dry:
            print(title, *lines, "dry-run; no ntfy", sep="\n")
            return
        send(cfg, kind, title, lines, prio)

    def store() -> None:
        if persist is not None:
            persist(state)
            return
        if not dry:
            save_state(state, now)

    if state.get("notified") or state.get("empty_notified"):
        print("ctq4 already finished; silent")
        return 0

    if fetch_exc is not None:
        rows_err: Exception | None = fetch_exc
    elif rows is None:
        try:
            rows = notices(cfg["ticker"], int(cfg.get("notice_size") or 80))
            rows_err = None
        except Exception as exc:  # noqa: BLE001
            rows_err = exc
    else:
        rows_err = None

    if rows_err is not None:
        print(f"::warning::notice fetch failed: {type(rows_err).__name__}", file=sys.stderr)
        if state.get("ops_fetch_day") != day:
            out("ops", defer_title(cfg), defer_lines(cfg, now, f"공시 목록 조회 실패({type(rows_err).__name__})", None), 3)
            if not dry:
                state["ops_fetch_day"] = day
                store()
        return 0

    hits = [r for r in (rows or []) if is_q4_prelim(r)]
    hits.sort(key=lambda r: (r.get("datetime") or "", r.get("id") or ""))
    if not hits:
        last = day >= cfg["window_end"] and now.hour >= int(cfg.get("empty_after_hour") or 19)
        if last and not state.get("empty_notified"):
            out("ops", empty_title(cfg), empty_lines(cfg, now), 3)
            if not dry:
                state["empty_notified"] = True
                store()
        else:
            print("4Q prelim not posted yet; silent")
        return 0

    item = hits[0]
    try:
        raw, unit, eok = extract_op_current(item.get("text") or "")
    except (ValueError, InvalidOperation) as exc:
        key = filing_key(item)
        already = state.get("ops_parse_day") == day and state.get("ops_parse_key") == key
        if not already:
            out("ops", defer_title(cfg), defer_lines(cfg, now, f"공시는 찾았지만 연결 영업이익 당해실적을 읽지 못함({exc})", item), 3)
            if not dry:
                state["ops_parse_day"] = day
                state["ops_parse_key"] = key
                store()
        else:
            print("parse defer already sent today; silent")
        return 0

    label = verdict(eok, threshold)
    title = result_title(cfg, label)
    lines = result_lines(cfg, item, raw, unit, eok, label, now)
    out("port", title, lines, 4)
    if not dry:
        state.update({
            "notified": True,
            "last_key": filing_key(item),
            "verdict": label,
            "sent_at": now.isoformat(timespec="seconds"),
            "parsed": {
                "raw": format_raw(raw, unit),
                "eok": str(eok),
                "unit": unit,
            },
        })
        store()
    return 0


def main() -> int:
    args = sys.argv[1:]
    mode = (args[0] if args else "auto").lower()
    if mode == "selftest":
        return selftest()
    if mode not in {"auto", "dry"}:
        raise SystemExit(f"unknown mode: {mode} (auto|dry|selftest)")
    now = datetime.now(KST)
    if "--now" in args:
        if mode != "dry":
            raise SystemExit("--now is for dry only")
        now = datetime.strptime(args[args.index("--now") + 1], "%Y-%m-%d %H:%M").replace(tzinfo=KST)
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    state = read_state()
    if mode == "dry":
        print(f"DRY RUN (no ntfy, no state write) now={now:%Y-%m-%d %H:%M} KST")
    if not in_window(cfg, now):
        if mode == "dry":
            try:
                rows = notices(cfg["ticker"], int(cfg.get("notice_size") or 80))
                hits = [r for r in rows if is_q4_prelim(r)]
                print(
                    f"outside window {cfg['window_start']}~{cfg['window_end']}; "
                    f"dry fetch notices={len(rows)} target_hits={len(hits)}; silent"
                )
                for h in hits[:3]:
                    print("  hit", (h.get("datetime") or "")[:19], h.get("title"))
            except Exception as exc:  # noqa: BLE001
                print(f"outside window; dry fetch failed ({type(exc).__name__}); silent")
        else:
            print(f"outside window {cfg['window_start']}~{cfg['window_end']}; silent")
        return 0
    return run(cfg, state, now, mode)


# ---------------- selftest ----------------
FY2025_Q4 = (
    "연결재무제표 기준 영업(잠정)실적(공정공시) 실적기간 당기실적 2025-10-01 ~ 2025-12-31 "
    "전기실적 2025-07-01 ~ 2025-09-30 전년동기실적 2024-10-01 ~ 2024-12-31 "
    "당기누계실적 2025-01-01 ~ 2025-12-31 "
    "※ 동 정보는 잠정치로서 향후 확정치와는 다를 수 있음. "
    "1. 연결실적내용 단위 : 백만원, % 구분 당기실적 전기실적 (25.4Q) (25.3Q) "
    "매출액 당해실적 1,330,157 1,028,969 29.27 누계실적 4,162,495 - - "
    "영업이익 당해실적 475,172 301,412 57.65 누계실적 1,168,471 - - "
    "당기순이익 당해실적 527,928 331,923 59.05 누계실적 1,031,484 - -"
)
FY2024_Q4_CONSOL = (
    "※ 동 정보는 잠정치로서 향후 확정치와는 다를 수 있음. "
    "1. 연결실적내용 단위 : 백만원, % "
    "구분 당기실적 전기실적 전년동기실적 (2024.4Q) (2024.3Q) (2023.4Q) "
    "매출액 당해실적 1,063,649 881,933 20.60% 누계실적 3,557,304 "
    "영업이익 당해실적 196,404 207,686 -5.43% 누계실적 492,016 "
    "당기순이익 당해실적 235,569 84,094 180.13% 누계실적 418,883"
)
FY2024_Q4_SEP = (
    "※ 동 정보는 잠정치로서 향후 확정치와는 다를 수 있음. "
    "1. 실적내용 단위 : 백만원, % "
    "구분 당기실적 전기실적 (2024.4Q) (2024.3Q) "
    "매출액 당해실적 1,057,839 누계실적 3,709,214 "
    "영업이익 당해실적 407,521 누계실적 1,211,014 "
    "상기 영업실적은 (주)셀트리온 별도재무제표 기준입니다."
)
FY2025_15PCT = (
    "매출액 또는 손익구조 30%(대규모법인 15%)이상 변경 "
    "1. 재무제표의 종류 연결 2. 결산기간 시작일 2025-01-01 종료일 2025-12-31 "
    "3. 매출액 또는 손익구조 변동내용(단위:천원) "
    "매출액 4,162,495,109 영업이익 1,168,471,346 당기순이익 1,031,483,789"
)
FY2025_OUTLOOK = (
    "※ 동 정보는 예측정보로서 향후 실제 결과와는 다를 수 있음 "
    "1. 연결 영업실적 전망 내용 구분(단위 : 억원) 대상기간 시작일 2025-10-01 종료일 2025-12-31 "
    "매출액 12,839 영업이익 4,722 EBITDA 5,389"
)
FY2026_Q3 = (
    "연결재무제표 기준 영업(잠정)실적(공정공시) 실적기간 당기실적 2026-07-01 ~ 2026-09-30 "
    "1. 연결실적내용 단위 : 억원, % 구분 당기실적 ('26.3Q) "
    "매출액 당해실적 15,000 누계실적 - 영업이익 당해실적 5,100 누계실적 -"
)
FY2026_Q3_CORR = (
    "(정정) 연결재무제표 기준 영업(잠정)실적(공정공시) 실적기간 당기실적 2026-07-01 ~ 2026-09-30 "
    "1. 연결실적내용 단위 : 억원, % 구분 당기실적 ('26.3Q) "
    "매출액 당해실적 15,755 누계실적 - 영업이익 당해실적 5,253 누계실적 -"
)


def fixture_q4(op: str, unit: str = "백만원", header: str = "iso") -> str:
    if header == "iso":
        period = "실적기간 당기실적 2026-10-01 ~ 2026-12-31 전기실적 2026-07-01 ~ 2026-09-30 "
        mark = "('26.4Q)"
    elif header == "mark2026":
        period = ""
        mark = "(2026.4Q)"
    else:
        period = ""
        mark = "(26.4Q)"
    return (
        f"연결재무제표 기준 영업(잠정)실적(공정공시) {period}"
        f"1. 연결실적내용 단위 : {unit}, % 구분 당기실적 전기실적 {mark} "
        f"매출액 당해실적 1,000,000 누계실적 3,000,000 "
        f"영업이익 당해실적 {op} 1,000 누계실적 9,999,999 "
        f"당기순이익 당해실적 1 누계실적 2"
    )


def selftest() -> int:
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    fails = 0
    inside = datetime(2027, 2, 5, 9, 0, tzinfo=KST)

    def check(name, got, want):
        nonlocal fails
        ok = got == want
        print(("ok  " if ok else "FAIL"), name, got, "" if ok else f"want {want}")
        fails += not ok

    def item(text, title="연결재무제표기준영업(잠정)실적(공정공시)", dt="2027-02-05T06:50:03", url="", fid="fx"):
        return {
            "id": fid,
            "title": title,
            "text": text,
            "datetime": dt,
            "url": url or "https://m.stock.naver.com/domestic/stock/068270/notice",
        }

    def play(rows, now=None, state=None, mode="auto", fetch_exc=None):
        st = {"notified": False, "empty_notified": False, "ops_parse_day": None, "ops_parse_key": None, "ops_fetch_day": None}
        if state is not None:
            st.update(state)
        sent: list = []

        def emit(kind, title, lines, prio):
            sent.append((kind, title, lines, prio))

        written = {}

        def persist(s):
            written.clear()
            written.update(s)

        rc = run(cfg, st, now or inside, mode, rows=rows, fetch_exc=fetch_exc, emit=emit, persist=persist)
        if written:
            st.update(written)
        return sent, st, rc

    # filters
    check("title 연결잠정", is_consol_prelim("연결재무제표기준영업(잠정)실적(공정공시)"), True)
    check("title 정정 연결잠정", is_consol_prelim("(정정)연결재무제표기준영업(잠정)실적(공정공시)"), True)
    check("title 전망 제외", is_consol_prelim("연결재무제표기준영업실적등에대한전망(공정공시)"), False)
    check("title 15% 제외", is_consol_prelim("매출액또는손익구조30%(대규모법인은15%)이상변경"), False)
    check("title 별도 제외", is_consol_prelim("영업(잠정)실적(공정공시)"), False)
    check("title 장래사업 제외", is_consol_prelim("장래사업ㆍ경영계획(공정공시)"), False)

    check("period iso 당기", is_target_period("", "당기실적 2026-10-01 ~ 2026-12-31"), True)
    check("period (2026.4Q)", is_target_period("", "구분 당기실적 전기실적 (2026.4Q) (2026.3Q)"), True)
    check("period (26.4Q)", is_target_period("", "당기실적 (26.4Q)"), True)
    check("period 25.4Q 제외", is_target_period("", FY2025_Q4), False)
    check("period 3Q 제외", is_target_period("", FY2026_Q3), False)

    raw, unit, eok = extract_op_current(FY2025_Q4)
    check("2025.4Q raw", (raw, unit, eok), (Decimal("475172"), "백만원", Decimal("4751.72")))
    check("599999 미달 비교", verdict(to_eok(Decimal("599999"), "백만원"), THRESHOLD), "미달")
    check("600000 달성 비교", verdict(to_eok(Decimal("600000"), "백만원"), THRESHOLD), "달성")
    check("599900 미달 비교", verdict(to_eok(Decimal("599900"), "백만원"), THRESHOLD), "미달")
    check("6000억 달성 비교", verdict(Decimal("6000"), THRESHOLD), "달성")

    # 2026-02-05 25.4Q must not fire inside the 2026-Q4 window
    sent, st, _ = play([item(FY2025_Q4, dt="2026-02-05T06:50:03")])
    check("2026-02-05 25.4Q 미발화", (sent, st.get("notified")), ([], False))

    # 2025-02-25 consol (2024.4Q) silent; separate title excluded even with a 2026.4Q body
    sent, st, _ = play([item(FY2024_Q4_CONSOL, dt="2025-02-25T15:38:00")])
    check("2025-02-25 연결 미발화", (sent, st.get("notified")), ([], False))
    trap = item(fixture_q4("600,000"), title="영업(잠정)실적(공정공시)", fid="sep")
    sent, st, _ = play([trap])
    check("2025-02-25 별도 제목 제외", (sent, st.get("notified")), ([], False))

    sent, st, _ = play([item(FY2025_15PCT, title="매출액또는손익구조30%(대규모법인은15%)이상변경")])
    check("15%변경 미발화", sent, [])
    sent, st, _ = play([item(FY2025_OUTLOOK, title="연결재무제표기준영업실적등에대한전망(공정공시)")])
    check("전망 미발화", sent, [])
    sent, st, _ = play([
        item(FY2026_Q3, dt="2026-10-12T06:50:01"),
        item(FY2026_Q3_CORR, title="(정정)연결재무제표기준영업(잠정)실적(공정공시)", dt="2026-10-27T06:50:12"),
    ])
    check("2026 3Q 미발화", sent, [])

    sent, st, _ = play([item(fixture_q4("600,000"))])
    check("600,000백만원 제목", sent[0][1] if sent else None, result_title(cfg, "달성"))
    check("600,000백만원 본문 원문", any("600,000백만원" in ln for ln in sent[0][2]) if sent else False, True)
    check("600,000백만원 환산", any("6,000억원" in ln for ln in sent[0][2]) if sent else False, True)
    check("600,000백만원 정정문구", any(CORRECTION_NOTE in ln for ln in sent[0][2]) if sent else False, True)
    check("600,000백만원 notified", st.get("notified") and st.get("verdict") == "달성", True)

    sent, st, _ = play([item(fixture_q4("599,900"))])
    check("599,900백만원 제목", sent[0][1] if sent else None, result_title(cfg, "미달"))
    check("599,900백만원 환산", any("5,999억원" in ln for ln in sent[0][2]) if sent else False, True)
    check("599,900백만원 notified", st.get("verdict") == "미달", True)

    sent, st, _ = play([item(fixture_q4("599,999"))])
    check("599,999백만원 미달", sent[0][1] if sent else None, result_title(cfg, "미달"))
    check("599,999백만원 환산 정확", any("5,999.99억원" in ln for ln in sent[0][2]) if sent else False, True)

    sent, st, _ = play([item(fixture_q4("6,000", unit="억원"))])
    check("6,000억원 달성", sent[0][1] if sent else None, result_title(cfg, "달성"))
    check("6,000억원 원문", any("6,000억원" in ln for ln in sent[0][2]) if sent else False, True)

    sent, st, _ = play([item(fixture_q4("6,000", unit="억원", header="mark2026"))])
    check("(2026.4Q) 헤더만 달성", sent[0][1] if sent else None, result_title(cfg, "달성"))
    sent, st, _ = play([item(fixture_q4("6,000", unit="억원", header="mark26"))])
    check("(26.4Q) 헤더만 달성", sent[0][1] if sent else None, result_title(cfg, "달성"))

    # parse failure fallback, once per day, notified stays false
    bad = item("연결실적내용 단위 : 백만원, % 당기실적 2026-10-01 ~ 2026-12-31 매출액 당해실적 1 누계실적 2", fid="bad")
    sent, st, _ = play([bad])
    check("파싱 실패 제목", sent[0][1] if sent else None, defer_title(cfg))
    check("파싱 실패 추정 금지", (not sent) or all("영업이익 당해실적 원문" not in ln for ln in sent[0][2]), True)
    check("파싱 실패 링크", (not sent) or any("dart.fss.or.kr" in ln or "stock.naver.com" in ln for ln in sent[0][2]), True)
    check("파싱 실패 notified false", st.get("notified") is False, True)
    sent2, st2, _ = play([bad], state=st)
    check("파싱 실패 같은 공시 하루 1회", sent2, [])
    next_day = datetime(2027, 2, 6, 9, 0, tzinfo=KST)
    sent3, _, _ = play([bad], now=next_day, state=st2)
    check("파싱 실패 다음날 재시도", bool(sent3) and sent3[0][1] == defer_title(cfg), True)

    rcp_item = item(
        fixture_q4("600,000") + " https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20270205800031",
        url="https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20270205800031",
    )
    sent, _, _ = play([rcp_item])
    check("DART 원문 링크", any("rcpNo=20270205800031" in ln for ln in sent[0][2]) if sent else False, True)

    # notified then no resend, including 정정
    first = item(fixture_q4("5,900", unit="억원"), dt="2027-02-05T06:50:03", fid="orig")
    sent, st, _ = play([first])
    check("첫 판정 미달 notified", st.get("notified") and st.get("verdict") == "미달", True)
    corr = item(fixture_q4("6,100", unit="억원"), title="(정정)연결재무제표기준영업(잠정)실적(공정공시)", dt="2027-02-20T06:50:03", fid="corr")
    sent2, st2, _ = play([first, corr], state=st)
    check("정정 재알림 없음", sent2, [])
    check("정정 후에도 notified 유지", st2.get("notified") is True and st2.get("verdict") == "미달", True)

    # if both appear on the first run, judge the earlier (원공시) only
    sent, st, _ = play([corr, first])
    check("동시 발견 시 원공시 기준", sent[0][1] if sent else None, result_title(cfg, "미달"))
    check("동시 발견 1통", len(sent), 1)

    # dry does not persist
    sent, st, _ = play([item(fixture_q4("600,000"))], mode="dry")
    check("dry 제목은 나오지만 notified 유지 false", (bool(sent) and st.get("notified") is False), True)

    # window-end empty once
    end = datetime(2027, 3, 10, 19, 0, tzinfo=KST)
    sent, st, _ = play([], now=end)
    check("창 종료 미게시 제목", sent[0][1] if sent else None, empty_title(cfg))
    check("미게시에 068270", "[068270]" in (sent[0][1] if sent else ""), True)
    check("empty_notified", st.get("empty_notified") is True, True)
    sent2, _, _ = play([], now=datetime(2027, 3, 10, 19, 5, tzinfo=KST), state=st)
    check("미게시 1회만", sent2, [])
    sent, _, _ = play([], now=datetime(2027, 3, 10, 15, 20, tzinfo=KST))
    check("마감일 15:20은 미게시 아님", sent, [])

    # outside window stays silent even with a matching filing
    for label, ts in (
        ("창 이전", datetime(2027, 1, 19, 9, 0, tzinfo=KST)),
        ("창 이후", datetime(2027, 3, 11, 9, 0, tzinfo=KST)),
        ("연도 가드", datetime(2026, 11, 20, 15, 0, tzinfo=KST)),
    ):
        if in_window(cfg, ts):
            check(f"{label} in_window", True, False)
            continue
        # gated behaviour is in main(); run() itself is only used inside the window.
        check(f"{label} in_window false", in_window(cfg, ts), False)

    # fetch fail once per day
    sent, st, _ = play(None, fetch_exc=RuntimeError("x"))
    check("조회 실패 제목", sent[0][1] if sent else None, defer_title(cfg))
    sent2, _, _ = play(None, fetch_exc=RuntimeError("x"), state=st)
    check("조회 실패 하루 1회", sent2, [])

    # config guards
    check("title_prefix", cfg["title_prefix"], "[068270]")
    check("topics", (cfg["ntfy_topic"], cfg["ntfy_ops_topic"]), ("cwally-port", "cwally-ops"))
    check("window", (cfg["window_start"], cfg["window_end"], cfg["year_guard"], cfg["ends_kst"]),
          ("2027-01-20", "2027-03-10", 2027, "2027-03-10 23:59"))
    check("in_window 시작일 09:00", in_window(cfg, datetime(2027, 1, 20, 9, 0, tzinfo=KST)), True)
    check("in_window 종료 23:59", in_window(cfg, datetime(2027, 3, 10, 23, 59, tzinfo=KST)), True)
    check("in_window 종료 다음 분", in_window(cfg, datetime(2027, 3, 10, 23, 59, tzinfo=KST) + timedelta(minutes=1)), False)

    print("ctq4 selftest", "PASS" if not fails else f"FAIL {fails}")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
