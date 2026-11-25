#!/usr/bin/env python3
"""[018290] 브이티 2026년 4분기 연결 영업이익 170억·이익률 9% 1회 판정 (2026-11-25 pt48u).

Attached to existing weekday slots 09:00 / 15:20 / 19:00 (configs/vtq4.json).
Prefer 연결재무제표기준영업(잠정)실적(공정공시) for 2026-10-01~2026-12-31 or (2026.4Q)/(26.4Q).
If that quarterly prelim never appears, use the first FY2026 연결 손익구조 15%/30% 변동
and derive 4Q = annual - 9M (9M from 분기보고서 2026.09 rcpNo=20261113000876, 원 단위).
Compare 4Q consolidated 영업이익 to 17,000백만원 exactly and margin = OP/매출 < 9%.
Send once, then end permanently. No 정정 re-alert.
No filing by 2027-03-31 19:00 KST -> one 미게시 notice (cwally-ops).
Parse/fetch failure -> [OPS] [018290] ... 판정 유보 once per day, notified stays false.

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
from earnings_parse import filing_period  # noqa: E402
from naver_api import notices  # noqa: E402
from ntfy_util import publish, resolve_server, resolve_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
CFG_PATH = ROOT / "configs" / "vtq4.json"
STATE_PATH = ROOT / "state" / "vtq4.json"
THRESHOLD_WON = Decimal("17000000000")
MARGIN_LIMIT = Decimal("9") / Decimal("100")
CORRECTION_NOTE = "이후 정정 공시는 다시 보지 않음"
UNIT_RE = re.compile(r"단위\s*[:：]\s*(백만원|억원|천원|원)")
NUM_RE = r"([△(]\s*[\d,]+(?:\.\d+)?\)?|[-]?\s*[\d,]+(?:\.\d+)?)"
OP_CURRENT_RE = re.compile(rf"영업이익\s+당해실적\s+{NUM_RE}", re.S)
SALES_CURRENT_RE = re.compile(rf"매출액\s+당해실적\s+{NUM_RE}", re.S)
PRIOR_OP_RE = re.compile(
    rf"영업이익\s+당해실적\s+{NUM_RE}\s+{NUM_RE}\s+{NUM_RE}",
    re.S,
)
PRIOR_SALES_RE = re.compile(
    rf"매출액\s+당해실적\s+{NUM_RE}\s+{NUM_RE}\s+{NUM_RE}",
    re.S,
)
ISO_DANGI_RE = re.compile(
    r"당기실적[^\d누전]{0,40}2026-10-01\s*[~\-]\s*2026-12-31"
)
MARK_DANGI_RE = re.compile(
    r"당기실적[\s\S]{0,220}\(?'?(?:2026|26)\s*\.\s*4\s*Q"
)
STRUCT_SALES_RE = re.compile(rf"매출액\s+{NUM_RE}\s+{NUM_RE}")
STRUCT_OP_RE = re.compile(rf"영업이익\s+{NUM_RE}\s+{NUM_RE}")


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
    if filing_period(blob) == "2026년 4분기":
        return True
    if ISO_DANGI_RE.search(blob):
        return True
    if MARK_DANGI_RE.search(blob):
        return True
    return False


def is_q4_prelim(item: dict) -> bool:
    title, text = item.get("title") or "", item.get("text") or ""
    return is_consol_prelim(title) and is_target_period(title, text)


def is_struct_title(title: str) -> bool:
    if re.search(r"전망|장래사업|경영계획", title):
        return False
    if not re.search(r"매출액.{0,12}손익구조", title):
        return False
    if not re.search(r"변동|변경", title):
        return False
    return True


def is_fy2026_consol_change(item: dict) -> bool:
    title, text = item.get("title") or "", item.get("text") or ""
    if not is_struct_title(title):
        return False
    blob = compact(title + "\n" + text)
    if not re.search(r"재무제표의\s*종류\s*연결", blob):
        return False
    if re.search(r"재무제표의\s*종류\s*별도", blob):
        return False
    if not re.search(r"시작일\s+2026-01-01\b", blob):
        return False
    if not re.search(r"종료일\s+2026-12-31\b", blob):
        return False
    return True


def detect_unit(text: str, prefer_struct: bool = False) -> str | None:
    blob = compact(text)
    if prefer_struct:
        section = re.search(
            r"손익구조\s*변동내용\s*\(\s*단위\s*[:：]\s*(백만원|억원|천원|원)",
            blob,
        )
        if section:
            return section.group(1)
        section = re.search(r"변동내용\s*\(\s*단위\s*[:：]\s*(백만원|억원|천원|원)", blob)
        if section:
            return section.group(1)
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


def to_won(amount: Decimal, unit: str) -> Decimal:
    # Historical 손익구조 정정 머리글이 천원인데 숫자는 원인 경우가 있어, 자리수로 오표기를 보정한다.
    if unit == "원":
        return amount
    if unit == "천원":
        if abs(amount) >= Decimal("10000000000"):
            return amount
        return amount * Decimal(1000)
    if unit == "백만원":
        if abs(amount) >= Decimal("1000000000"):
            return amount
        return amount * Decimal(1000000)
    if unit == "억원":
        return amount * Decimal(100000000)
    raise ValueError(f"unknown unit {unit}")


def to_eok(won: Decimal) -> Decimal:
    return won / Decimal(100000000)


def cfg_won(cfg: dict, group: str, key: str) -> Decimal:
    return Decimal(str(cfg[group][key]))


def extract_prelim_quarter(text: str) -> dict:
    """당해실적 매출액·영업이익을 원으로. Raises on failure."""
    blob = compact(text)
    unit = detect_unit(blob, prefer_struct=False)
    if not unit:
        raise ValueError("unit not found")
    sales_hit = SALES_CURRENT_RE.search(blob)
    op_hit = OP_CURRENT_RE.search(blob)
    if not sales_hit:
        raise ValueError("매출액 당해실적 not found")
    if not op_hit:
        raise ValueError("영업이익 당해실적 not found")
    sales = to_won(parse_number(sales_hit.group(1)), unit)
    op = to_won(parse_number(op_hit.group(1)), unit)
    prior_sales = prior_op = None
    ps = PRIOR_SALES_RE.search(blob)
    po = PRIOR_OP_RE.search(blob)
    # typical order: 당해 전기 전년동기. 전년동기는 세 번째 숫자.
    if ps:
        try:
            prior_sales = to_won(parse_number(ps.group(3)), unit)
        except (InvalidOperation, ValueError, IndexError):
            prior_sales = None
    if po:
        try:
            prior_op = to_won(parse_number(po.group(3)), unit)
        except (InvalidOperation, ValueError, IndexError):
            prior_op = None
    return {
        "sales_won": sales,
        "op_won": op,
        "prior_sales_won": prior_sales,
        "prior_op_won": prior_op,
        "unit": unit,
        "source": "prelim",
    }


def extract_struct_annual(text: str) -> dict:
    blob = compact(text)
    unit = detect_unit(blob, prefer_struct=True)
    if not unit:
        raise ValueError("unit not found")
    sales_hit = STRUCT_SALES_RE.search(blob)
    op_hit = STRUCT_OP_RE.search(blob)
    if not sales_hit:
        raise ValueError("손익구조 매출액 not found")
    if not op_hit:
        raise ValueError("손익구조 영업이익 not found")
    return {
        "sales_won": to_won(parse_number(sales_hit.group(1)), unit),
        "op_won": to_won(parse_number(op_hit.group(1)), unit),
        "prior_sales_won": to_won(parse_number(sales_hit.group(2)), unit),
        "prior_op_won": to_won(parse_number(op_hit.group(2)), unit),
        "unit": unit,
    }


def derive_quarter(annual: dict, cfg: dict) -> dict:
    sales = annual["sales_won"] - cfg_won(cfg, "nine_m_26", "sales_won")
    op = annual["op_won"] - cfg_won(cfg, "nine_m_26", "op_won")
    prior_sales = prior_op = None
    if annual.get("prior_sales_won") is not None:
        prior_sales = annual["prior_sales_won"] - cfg_won(cfg, "nine_m_25", "sales_won")
    if annual.get("prior_op_won") is not None:
        prior_op = annual["prior_op_won"] - cfg_won(cfg, "nine_m_25", "op_won")
    return {
        "sales_won": sales,
        "op_won": op,
        "prior_sales_won": prior_sales,
        "prior_op_won": prior_op,
        "unit": annual["unit"],
        "source": "derived",
        "annual_sales_won": annual["sales_won"],
        "annual_op_won": annual["op_won"],
    }


def parse_target(item: dict, cfg: dict, kind: str) -> dict:
    text = item.get("text") or ""
    if kind == "prelim":
        return extract_prelim_quarter(text)
    annual = extract_struct_annual(text)
    return derive_quarter(annual, cfg)


def judge(op_won: Decimal, sales_won: Decimal) -> tuple[str, bool, Decimal]:
    if sales_won <= 0:
        raise ValueError("매출액이 없거나 0 이하라 이익률을 계산할 수 없음")
    op_label = "달성" if op_won >= THRESHOLD_WON else "미달"
    margin = op_won / sales_won
    warn = margin < MARGIN_LIMIT
    return op_label, warn, margin


def format_won(amount: Decimal) -> str:
    if amount == amount.to_integral_value():
        return f"{int(amount):,}원"
    text = f"{amount:,.10f}".rstrip("0").rstrip(".")
    return f"{text}원"


def format_eok_short(eok: Decimal) -> str:
    q = eok.normalize()
    if q == q.to_integral_value():
        return f"{int(q):,}억"
    text = f"{q:,.10f}".rstrip("0").rstrip(".")
    return f"{text}억"


def format_margin_pct(margin: Decimal) -> str:
    pct = margin * Decimal(100)
    if pct == pct.to_integral_value():
        return f"{int(pct)}.0%"
    text = f"{pct:.6f}".rstrip("0").rstrip(".")
    # 반올림 표시가 9.0%로 가려지지 않게, 경계 근처는 그대로 둔다.
    return f"{text}%"


def yoy_text(curr: Decimal | None, prior: Decimal | None, label: str) -> str:
    if curr is None or prior is None:
        return f"{label} 전년동기 확인 불가"
    if prior == 0:
        return f"{label} 전년동기 {format_won(prior)} (증감률 확인 불가)"
    chg = (curr - prior) / abs(prior) * Decimal(100)
    sign = "+" if chg >= 0 else ""
    return f"{label} 전년동기 {format_won(prior)} 대비 {sign}{format_margin_pct(chg / Decimal(100)).rstrip('%')}%"


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


def result_title(cfg: dict, op_won: Decimal, op_label: str, margin: Decimal, warn: bool) -> str:
    parts = [
        f"{cfg['title_prefix']} 브이티 4Q 잠정 영업이익 {format_eok_short(to_eok(op_won))}",
        f"170억 {op_label}",
        f"이익률 {format_margin_pct(margin)}",
    ]
    if warn:
        parts.append("9% 미만 경고")
    return " | ".join(parts)


def defer_title(cfg: dict) -> str:
    return f"[OPS] {cfg['title_prefix']} 브이티 4Q 잠정 판정 유보"


def empty_title(cfg: dict) -> str:
    return f"{cfg['title_prefix']} 브이티 4Q 잠정 미게시"


def result_lines(cfg: dict, item: dict, parsed: dict, op_label: str, warn: bool, margin: Decimal, now: datetime) -> list[str]:
    link = source_link(item, cfg)
    sales, op = parsed["sales_won"], parsed["op_won"]
    if parsed.get("source") == "derived":
        method = (
            f"연결 손익구조변동 연간(매출 {format_won(parsed['annual_sales_won'])}, "
            f"영업이익 {format_won(parsed['annual_op_won'])})에서 "
            f"3분기 누적(매출 {format_won(cfg_won(cfg, 'nine_m_26', 'sales_won'))}, "
            f"영업이익 {format_won(cfg_won(cfg, 'nine_m_26', 'op_won'))}, "
            f"rcpNo=20261113000876)을 빼 4분기를 계산함."
        )
    else:
        method = "연결재무제표기준영업(잠정)실적 당해실적(4분기 단일 분기)을 그대로 씀."
    warn_txt = "이익률이 9% 미만이라 경고." if warn else "이익률은 9% 미만이 아님."
    return [
        "이게 뭐 알림: 브이티(018290) 2026년 4분기 연결 영업이익이 17,000백만원 이상인지, 영업이익률이 9% 미만인지를 한 번 판정합니다. 투자 권유가 아닙니다.",
        (
            f"지금 결과/상태: 영업이익 {op_label}, {warn_txt} "
            f"4분기 연결 매출 {format_won(sales)} ({format_eok_short(to_eok(sales))}), "
            f"영업이익 {format_won(op)} ({format_eok_short(to_eok(op))}), "
            f"이익률 {format_margin_pct(margin)}. "
            f"{yoy_text(sales, parsed.get('prior_sales_won'), '매출')}. "
            f"{yoy_text(op, parsed.get('prior_op_won'), '영업이익')}. "
            f"{method} 기준 영업이익 17,000백만원 이상, 이익률 9% 미만. 누계실적은 쓰지 않음. "
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
        f"{now:%Y-%m-%d %H:%M} KST | OPS | vtq4 4분기 잠정",
        "이게 뭐 알림: 브이티(018290) 4분기 잠정실적 판정을 이번 실행에서는 내리지 못했습니다. 투자 판단 알림이 아닙니다. 숫자는 추정하지 않습니다.",
        f"지금 상태: {detail}. notified는 그대로 false이며 다음 실행에서 다시 봅니다. 원문 {link}. {CORRECTION_NOTE}.",
        "지금 할 일: 원문을 직접 확인하세요.",
    ]


def empty_lines(cfg: dict, now: datetime) -> list[str]:
    return [
        "이게 뭐 알림: 브이티(018290) 2026년 4분기 연결 실적 감시 창이 끝났습니다. 매매 신호가 아닙니다.",
        (
            f"지금 결과/상태: {cfg['window_start']}~{cfg['window_end']} 동안 2026년 4분기 "
            f"연결재무제표기준영업(잠정)실적 또는 FY2026 연결 손익구조변동 공시를 찾지 못했습니다. "
            f"조회 {now:%Y-%m-%d %H:%M} KST. "
            f"https://m.stock.naver.com/domestic/stock/{cfg['ticker']}/notice | DART 검색 {cfg.get('dart_search')}."
        ),
        "지금 할 일: 공시 일정을 직접 확인하세요.",
    ]


def pick_hits(rows: list[dict]) -> tuple[list[dict], str | None]:
    prelim = [r for r in rows if is_q4_prelim(r)]
    prelim.sort(key=lambda r: (r.get("datetime") or "", r.get("id") or ""))
    if prelim:
        return prelim, "prelim"
    struct = [r for r in rows if is_fy2026_consol_change(r)]
    struct.sort(key=lambda r: (r.get("datetime") or "", r.get("id") or ""))
    if struct:
        return struct, "derived"
    return [], None


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
        print("vtq4 already finished; silent")
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

    hits, kind = pick_hits(rows or [])
    if not hits:
        last = day >= cfg["window_end"] and now.hour >= int(cfg.get("empty_after_hour") or 19)
        if last and not state.get("empty_notified"):
            out("ops", empty_title(cfg), empty_lines(cfg, now), 3)
            if not dry:
                state["empty_notified"] = True
                store()
        else:
            print("4Q filing not posted yet; silent")
        return 0

    item = hits[0]
    try:
        parsed = parse_target(item, cfg, kind or "derived")
        op_label, warn, margin = judge(parsed["op_won"], parsed["sales_won"])
    except (ValueError, InvalidOperation, KeyError, TypeError) as exc:
        key = filing_key(item)
        already = state.get("ops_parse_day") == day and state.get("ops_parse_key") == key
        if not already:
            out("ops", defer_title(cfg), defer_lines(cfg, now, f"공시는 찾았지만 연결 4분기 매출·영업이익을 읽지 못함({exc})", item), 3)
            if not dry:
                state["ops_parse_day"] = day
                state["ops_parse_key"] = key
                store()
        else:
            print("parse defer already sent today; silent")
        return 0

    title = result_title(cfg, parsed["op_won"], op_label, margin, warn)
    lines = result_lines(cfg, item, parsed, op_label, warn, margin, now)
    out("port", title, lines, 4)
    if not dry:
        state.update({
            "notified": True,
            "last_key": filing_key(item),
            "verdict": op_label,
            "margin_warn": warn,
            "source": parsed.get("source"),
            "sent_at": now.isoformat(timespec="seconds"),
            "parsed": {
                "sales_won": str(parsed["sales_won"]),
                "op_won": str(parsed["op_won"]),
                "margin": str(margin),
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
                hits, kind = pick_hits(rows)
                print(
                    f"outside window {cfg['window_start']}~{cfg['window_end']}; "
                    f"dry fetch notices={len(rows)} target_hits={len(hits)} kind={kind}; silent"
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
FY2025_STRUCT = (
    "매출액 또는 손익구조 30%(대규모법인은 15%)이상 변동 "
    "1. 재무제표의 종류 연결 2. 결산기간 당해사업연도 직전사업연도 "
    "- 시작일 2025-01-01 2024-01-01 - 종료일 2025-12-31 2024-12-31 "
    "3. 매출액 또는 손익구조변동내용(단위: 원) 당해사업연도 직전사업연도 증감금액 증감비율(%) 흑자적자전환여부 "
    "- 매출액 437,584,522,154 431,696,809,961 5,887,712,193 1.36 - "
    "- 영업이익 82,975,356,320 110,898,660,064 -27,923,303,744 -25.17 - "
    "- 법인세비용차감전계속사업이익 79,693,511,740 134,088,523,249 -54,395,011,509 -40.56 - "
    "- 당기순이익 63,241,728,434 103,998,705,371 -40,756,976,937 -39.18 -"
)
FY2025_STRUCT_REAL_TAIL = ""


def fixture_prelim(sales: str, op: str, unit: str = "백만원", header: str = "iso", prior_sales: str | None = None, prior_op: str | None = None) -> str:
    if header == "iso":
        period = "실적기간 당기실적 2026-10-01 ~ 2026-12-31 전기실적 2026-07-01 ~ 2026-09-30 "
        mark = "('26.4Q)"
    elif header == "mark2026":
        period = ""
        mark = "(2026.4Q)"
    else:
        period = ""
        mark = "(26.4Q)"
    p_sales = prior_sales or "100,000"
    p_op = prior_op or "10,000"
    y_sales = "90,000"
    y_op = "9,000"
    return (
        f"연결재무제표 기준 영업(잠정)실적(공정공시) {period}"
        f"1. 연결실적내용 단위 : {unit}, % 구분 당기실적 전기실적 전년동기실적 {mark} "
        f"매출액 당해실적 {sales} {p_sales} {y_sales} 누계실적 9,999,999 "
        f"영업이익 당해실적 {op} {p_op} {y_op} 누계실적 9,999,999 "
        f"당기순이익 당해실적 1 2 3 누계실적 4"
    )


def fixture_struct(sales: int | str, op: int | str, prior_sales: int | str = 437183656431, prior_op: int | str = 82869214759, year: int = 2026, unit: str = "원") -> str:
    prev = year - 1
    def fmt(v):
        if isinstance(v, str):
            return v
        return f"{v:,}"
    return (
        f"매출액 또는 손익구조 30%(대규모법인은 15%)이상 변동 "
        f"1. 재무제표의 종류 연결 2. 결산기간 당해사업연도 직전사업연도 "
        f"- 시작일 {year}-01-01 {prev}-01-01 - 종료일 {year}-12-31 {prev}-12-31 "
        f"3. 매출액 또는 손익구조변동내용(단위: {unit}) 당해사업연도 직전사업연도 증감금액 증감비율(%) 흑자적자전환여부 "
        f"- 매출액 {fmt(sales)} {fmt(prior_sales)} 1 1.00 - "
        f"- 영업이익 {fmt(op)} {fmt(prior_op)} 1 1.00 - "
        f"- 당기순이익 1 2 3 1.00 -"
    )


def selftest() -> int:
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    fails = 0
    inside = datetime(2027, 2, 26, 9, 0, tzinfo=KST)
    nine_s = Decimal(cfg["nine_m_26"]["sales_won"])
    nine_o = Decimal(cfg["nine_m_26"]["op_won"])
    nine_s25 = Decimal(cfg["nine_m_25"]["sales_won"])
    nine_o25 = Decimal(cfg["nine_m_25"]["op_won"])

    def check(name, got, want):
        nonlocal fails
        ok = got == want
        print(("ok  " if ok else "FAIL"), name, got, "" if ok else f"want {want}")
        fails += not ok

    def item(text, title="연결재무제표기준영업(잠정)실적(공정공시)", dt="2027-02-26T06:53:06", url="", fid="fx"):
        return {
            "id": fid,
            "title": title,
            "text": text,
            "datetime": dt,
            "url": url or "https://m.stock.naver.com/domestic/stock/018290/notice",
        }

    def struct_item(text, title="매출액 또는 손익구조 30%(대규모법인은 15%)이상 변동", dt="2027-02-26T06:53:06", fid="st", url=""):
        return item(text, title=title, dt=dt, fid=fid, url=url)

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

    # title filters
    check("title 연결잠정", is_consol_prelim("연결재무제표기준영업(잠정)실적(공정공시)"), True)
    check("title 정정 연결잠정", is_consol_prelim("(정정)연결재무제표기준영업(잠정)실적(공정공시)"), True)
    check("title 전망 제외", is_consol_prelim("연결재무제표기준영업실적등에대한전망(공정공시)"), False)
    check("title 별도 제외", is_consol_prelim("영업(잠정)실적(공정공시)"), False)
    check("period iso", is_target_period("", "당기실적 2026-10-01 ~ 2026-12-31"), True)
    check("period filing_period iso", filing_period("당기실적 2026-10-01 ~ 2026-12-31"), "2026년 4분기")
    check("period (2026.4Q)", is_target_period("", "구분 당기실적 전기실적 (2026.4Q) (2026.3Q)"), True)
    check("period (26.4Q)", is_target_period("", "당기실적 (26.4Q)"), True)
    check("period '26.4Q filing_period", filing_period("당기실적 ('26.4Q) 매출액 1"), "2026년 4분기")
    check("period 25.4Q 제외", is_target_period("", "당기실적 2025-10-01 ~ 2025-12-31 (25.4Q)"), False)

    # official 9M pins
    check("9M26 매출 원문", nine_s, Decimal("383748392567"))
    check("9M26 영업이익 원문", nine_o, Decimal("42865992932"))
    check("9M25 매출 원문", nine_s25, Decimal("322189642232"))
    check("9M25 영업이익 원문", nine_o25, Decimal("70208100568"))

    # boundaries in 백만원 (prelim)
    def t_prelim(sales_m, op_m, **kw):
        return item(fixture_prelim(f"{sales_m:,}", f"{op_m:,}"), **kw)

    sent, st, _ = play([t_prelim(200000, 17000)])
    check("17000백만 달성", "170억 달성" in (sent[0][1] if sent else ""), True)
    check("17000/200000 경고", "9% 미만 경고" in (sent[0][1] if sent else ""), True)
    check("17000 제목 티커", (sent[0][1] if sent else "").startswith("[018290]"), True)
    check("17000 notified", st.get("notified") is True and st.get("verdict") == "달성", True)

    sent, st, _ = play([t_prelim(200000, 16999)])
    check("16999백만 미달", "170억 미달" in (sent[0][1] if sent else ""), True)
    check("16999 notified 미달", st.get("verdict") == "미달", True)

    # margin 9.0% vs just under 9%
    # 18,000 / 200,000 = 9.0% exact, OP 18,000백만 >= 17,000
    sent, st, _ = play([t_prelim(200000, 18000)])
    check("이익률 9.0% 경고 없음", "9% 미만 경고" not in (sent[0][1] if sent else "x"), True)
    check("이익률 9.0% 제목", "이익률 9.0%" in (sent[0][1] if sent else ""), True)
    check("이익률 9.0% 달성", "170억 달성" in (sent[0][1] if sent else ""), True)

    # 17,000 / 188,889 = 8.9999417...% < 9%
    sent, st, _ = play([t_prelim(188889, 17000)])
    check("이익률 8.99대 경고", "9% 미만 경고" in (sent[0][1] if sent else ""), True)
    check("이익률 8.99대 달성", "170억 달성" in (sent[0][1] if sent else ""), True)

    # 17,000 / 188,888 = 9.0000423...% >= 9%
    sent, st, _ = play([t_prelim(188888, 17000)])
    check("이익률 9.0000대 경고 없음", "9% 미만 경고" not in (sent[0][1] if sent else "x"), True)

    # historical FY2025 손익구조 must not fire
    sent, st, _ = play([struct_item(FY2025_STRUCT)])
    check("2025 손익구조 미발화", (sent, st.get("notified")), ([], False))

    # FY2026 손익구조 derive: annual = 9M26 + quarter
    q_sales = Decimal(188889000000)
    q_op = Decimal(17000000000)
    annual_sales = int(nine_s + q_sales)
    annual_op = int(nine_o + q_op)
    prior_sales = int(nine_s25 + Decimal(100000000000))
    prior_op = int(nine_o25 + Decimal(12000000000))
    derived = struct_item(fixture_struct(annual_sales, annual_op, prior_sales, prior_op))
    sent, st, _ = play([derived])
    check("손익구조 도출 달성", "170억 달성" in (sent[0][1] if sent else ""), True)
    check("손익구조 도출 경고", "9% 미만 경고" in (sent[0][1] if sent else ""), True)
    check("손익구조 도출 본문 계산", any("빼 4분기를 계산함" in ln for ln in sent[0][2]) if sent else False, True)
    check("손익구조 도출 YoY 영업", any("전년동기 12,000,000,000원" in ln for ln in sent[0][2]) if sent else False, True)
    check("손익구조 본문 매출", any("188,889,000,000원" in ln for ln in sent[0][2]) if sent else False, True)
    check("손익구조 source derived", st.get("source") == "derived", True)

    # 16,999 million derived miss
    miss_annual_op = int(nine_o + Decimal(16999000000))
    sent, st, _ = play([struct_item(fixture_struct(annual_sales, miss_annual_op, prior_sales, prior_op))])
    check("손익구조 16999 미달", "170억 미달" in (sent[0][1] if sent else ""), True)

    # prefer quarterly prelim over 손익구조 on the same run
    prelim_miss = t_prelim(200000, 16999, fid="pre", dt="2027-02-26T07:00:00")
    sent, st, _ = play([derived, prelim_miss])
    check("잠정 우선 미달", "170억 미달" in (sent[0][1] if sent else ""), True)
    check("잠정 우선 source", st.get("source") == "prelim", True)

    # 별도 / 전망 / 3Q 제외
    sent, _, _ = play([item(fixture_prelim("200,000", "17,000"), title="영업(잠정)실적(공정공시)")])
    check("별도 제목 제외", sent, [])
    sent, _, _ = play([item(fixture_prelim("200,000", "17,000"), title="연결재무제표기준영업실적등에대한전망(공정공시)")])
    check("전망 제외", sent, [])
    q3 = item(
        "연결재무제표 기준 영업(잠정)실적(공정공시) 실적기간 당기실적 2026-07-01 ~ 2026-09-30 "
        "1. 연결실적내용 단위 : 백만원, % 구분 당기실적 ('26.3Q) "
        "매출액 당해실적 137,789 누계실적 383,748 영업이익 당해실적 14,854 누계실적 42,866",
        dt="2026-11-13T15:36:00",
    )
    sent, _, _ = play([q3])
    check("3Q 잠정 미발화", sent, [])
    sep_struct = struct_item(fixture_struct(annual_sales, annual_op).replace("종류 연결", "종류 별도"))
    sent, _, _ = play([sep_struct])
    check("별도 손익구조 제외", sent, [])

    # parse failure once per day
    bad = item("연결실적내용 단위 : 백만원, % 당기실적 2026-10-01 ~ 2026-12-31 매출액 당해실적 1 누계실적 2", fid="bad")
    sent, st, _ = play([bad])
    check("파싱 실패 제목", sent[0][1] if sent else None, defer_title(cfg))
    check("파싱 실패 추정 금지", (not sent) or all("지금 결과/상태: 영업이익" not in ln for ln in sent[0][2]), True)
    check("파싱 실패 링크", (not sent) or any("dart.fss.or.kr" in ln or "stock.naver.com" in ln for ln in sent[0][2]), True)
    check("파싱 실패 notified false", st.get("notified") is False, True)
    sent2, st2, _ = play([bad], state=st)
    check("파싱 실패 같은 공시 하루 1회", sent2, [])
    next_day = datetime(2027, 2, 27, 9, 0, tzinfo=KST)
    sent3, _, _ = play([bad], now=next_day, state=st2)
    check("파싱 실패 다음날 재시도", bool(sent3) and sent3[0][1] == defer_title(cfg), True)

    # zero revenue defers
    zero = item(fixture_prelim("0", "17,000"), fid="zero")
    sent, st, _ = play([zero])
    check("매출 0 유보", sent[0][1] if sent else None, defer_title(cfg))
    check("매출 0 notified false", st.get("notified") is False, True)

    rcp_item = t_prelim(200000, 17000, url="https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20270226901115")
    rcp_item["text"] += " https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20270226901115"
    sent, _, _ = play([rcp_item])
    check("DART 원문 링크", any("rcpNo=20270226901115" in ln for ln in sent[0][2]) if sent else False, True)
    check("정정문구", any(CORRECTION_NOTE in ln for ln in sent[0][2]) if sent else False, True)

    # notified then no resend, including 정정
    first = t_prelim(200000, 16999, dt="2027-02-26T06:53:06", fid="orig")
    sent, st, _ = play([first])
    check("첫 판정 미달 notified", st.get("notified") and st.get("verdict") == "미달", True)
    corr = t_prelim(200000, 18000, title="(정정)연결재무제표기준영업(잠정)실적(공정공시)", dt="2027-03-12T06:53:15", fid="corr")
    sent2, st2, _ = play([first, corr], state=st)
    check("정정 재알림 없음", sent2, [])
    check("정정 후에도 notified 유지", st2.get("notified") is True and st2.get("verdict") == "미달", True)

    sent, st, _ = play([corr, first])
    check("동시 발견 시 원공시 기준", "170억 미달" in (sent[0][1] if sent else ""), True)
    check("동시 발견 1통", len(sent), 1)

    sent, st, _ = play([t_prelim(200000, 17000)], mode="dry")
    check("dry 제목은 나오지만 notified 유지 false", (bool(sent) and st.get("notified") is False), True)

    # window-end empty once (03-31, later than ctq4)
    end = datetime(2027, 3, 31, 19, 0, tzinfo=KST)
    sent, st, _ = play([], now=end)
    check("창 종료 미게시 제목", sent[0][1] if sent else None, empty_title(cfg))
    check("미게시에 018290", "[018290]" in (sent[0][1] if sent else ""), True)
    check("empty_notified", st.get("empty_notified") is True, True)
    sent2, _, _ = play([], now=datetime(2027, 3, 31, 19, 5, tzinfo=KST), state=st)
    check("미게시 1회만", sent2, [])
    sent, _, _ = play([], now=datetime(2027, 3, 31, 15, 20, tzinfo=KST))
    check("마감일 15:20은 미게시 아님", sent, [])

    for label, ts in (
        ("창 이전", datetime(2027, 1, 19, 9, 0, tzinfo=KST)),
        ("창 이후", datetime(2027, 4, 1, 9, 0, tzinfo=KST)),
        ("연도 가드", datetime(2026, 11, 25, 15, 0, tzinfo=KST)),
    ):
        check(f"{label} in_window false", in_window(cfg, ts), False)

    sent, st, _ = play(None, fetch_exc=RuntimeError("x"))
    check("조회 실패 제목", sent[0][1] if sent else None, defer_title(cfg))
    sent2, _, _ = play(None, fetch_exc=RuntimeError("x"), state=st)
    check("조회 실패 하루 1회", sent2, [])

    # header-only prelim periods
    sent, _, _ = play([item(fixture_prelim("200,000", "18,000", unit="백만원", header="mark2026"))])
    check("(2026.4Q) 헤더만 달성", "170억 달성" in (sent[0][1] if sent else ""), True)
    sent, _, _ = play([item(fixture_prelim("200,000", "18,000", unit="백만원", header="mark26"))])
    check("(26.4Q) 헤더만 달성", "170억 달성" in (sent[0][1] if sent else ""), True)

    # judge helper boundaries
    check("judge 17000 vs 16999", judge(Decimal("17000000000"), Decimal("200000000000"))[0], "달성")
    check("judge 16999", judge(Decimal("16999000000"), Decimal("200000000000"))[0], "미달")
    check("judge margin 9.0", judge(Decimal("18000000000"), Decimal("200000000000"))[1], False)
    check("judge margin 8.5", judge(Decimal("17000000000"), Decimal("200000000000"))[1], True)
    check("judge margin 188889", judge(Decimal("17000000000"), Decimal("188889000000"))[1], True)
    check("judge margin 188888", judge(Decimal("17000000000"), Decimal("188888000000"))[1], False)

    # config guards
    check("title_prefix", cfg["title_prefix"], "[018290]")
    check("topics", (cfg["ntfy_topic"], cfg["ntfy_ops_topic"]), ("cwally-port", "cwally-ops"))
    check("corp_code", cfg["corp_code"], "00150165")
    check("window", (cfg["window_start"], cfg["window_end"], cfg["year_guard"], cfg["ends_kst"]),
          ("2027-01-20", "2027-03-31", 2027, "2027-03-31 23:59"))
    check("in_window 시작일 09:00", in_window(cfg, datetime(2027, 1, 20, 9, 0, tzinfo=KST)), True)
    check("in_window 종료 23:59", in_window(cfg, datetime(2027, 3, 31, 23, 59, tzinfo=KST)), True)
    check("in_window 종료 다음 분", in_window(cfg, datetime(2027, 3, 31, 23, 59, tzinfo=KST) + timedelta(minutes=1)), False)

    # titles never miss the ticker
    check("defer ticker", defer_title(cfg), "[OPS] [018290] 브이티 4Q 잠정 판정 유보")
    check("empty ticker", empty_title(cfg), "[018290] 브이티 4Q 잠정 미게시")

    print("vtq4 selftest", "PASS" if not fails else f"FAIL {fails}")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
