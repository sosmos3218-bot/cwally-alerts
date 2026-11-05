#!/usr/bin/env python3
"""[079960] 동양이엔피 disclosure watch (2026-11-05 ~ 2027-02-28).

morning 09:00 / afternoon 15:20 / k203780 19:00. Naver notice feed (DART/KIND mirror, no key).
Fires once per filing id for: (a) earnings (잠정실적/분기보고서/30%변동) with parsed sales/OP/margin
+ re-rate/downgrade checklist; (b) supply contracts; (c) buyback/cancellation; (d) dividend;
(e) affiliate stake / loan / capex (cash deployment). First run seeds seen, no send.
Review prompt only. Modes: auto | dry | install | selftest ; --as-of for dry.
"""
from __future__ import annotations

import copy
import json
import re
import sys
import traceback
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import naver_api  # noqa: E402
from kr_calendar import KST, is_trading_day, session_day  # noqa: E402
from ntfy_util import publish, resolve_server, resolve_topic, route_topic  # noqa: E402

TAG = "[079960]"
NAME = "k079960"
CFG_PATH = ROOT / "configs" / f"{NAME}.json"
STATE_PATH = ROOT / "state" / f"{NAME}.json"
UA = {"User-Agent": "Mozilla/5.0 cwally-alerts k079960"}
FORBIDDEN = ("\u00b7", "\u2014", "\u318d")


def scrub(s: str) -> str:
    for a, b in (("\u00b7", ", "), ("\u318d", ", "), ("\u2014", " - "), ("\u2027", ", "), ("\u30fb", ", ")):
        s = s.replace(a, b)
    return re.sub(r"\s+", " ", s).strip()


def check_text(t: str, m: str) -> None:
    for ch in FORBIDDEN:
        if ch in t or ch in m:
            raise ValueError(f"forbidden U+{ord(ch):04X} in {t}")


def load_cfg() -> dict:
    return json.loads(CFG_PATH.read_text(encoding="utf-8"))


def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(st: dict) -> None:
    STATE_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def body(stamp: str, hdr: str, what: str, status: str, action: str, later: str, trim: str, avoid: str) -> str:
    return "\n".join([
        f"{stamp} | 079960 | 동양이엔피 {hdr}",
        f"이게 뭐 알림: {what}",
        f"지금 결과/상태: {status}",
        f"지금 할 일: {action}",
        f"나중에: {later}",
        f"익절/줄일 때: {trim}",
        f"하지 말 것: {avoid}",
    ])


def match_rule(cfg: dict, title: str) -> dict | None:
    t = scrub(title)
    for rule in cfg["rules"]:
        if any(x in t for x in rule.get("exclude_any") or []):
            continue
        if any(k in t for k in rule["any"]):
            return rule
    return None


def notices_live(cfg: dict) -> list[dict]:
    rows = naver_api.notices(cfg["code"], 80)
    out = []
    for r in rows:
        title = scrub(str(r.get("title") or ""))
        no = str(r.get("id") or r.get("no") or "")
        day = str(r.get("datetime") or "")[:10]
        if not no or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
            continue
        out.append({
            "id": no,
            "title": title,
            "datetime": str(r.get("datetime") or "")[:16].replace("T", " "),
            "day": day,
            "text": str(r.get("text") or ""),
            "url": str(r.get("url") or cfg["sources"]["notice_page"]),
        })
    return sorted(out, key=lambda x: x["datetime"])


def _num_tok(tok: str) -> float | None:
    tok = tok.replace(",", "").strip()
    if not tok or tok == "-":
        return None
    try:
        return float(tok)
    except ValueError:
        return None


def parse_earn(text: str) -> dict:
    """Best-effort sales/OP in 억원 from a 잠정실적-style body. Never invents."""
    t = text or ""
    unit = 1.0
    if "단위 : 백만원" in t or "단위: 백만원" in t or "단위 :백만원" in t:
        unit = 0.01
    elif "단위 : 천원" in t or "단위: 천원" in t:
        unit = 0.0001
    out: dict = {"sales": None, "op": None, "margin": None, "sales_yoy": None, "op_yoy": None, "unit_note": f"scale×{unit}"}

    def row(label: str) -> list[float]:
        # "매출액 당해실적 12,345 ..." style
        m = re.search(label + r"[^\d\-]*((-?[\d,]+(?:\.\d+)?|-)\s+){1,6}", t)
        if not m:
            return []
        return [v for v in (_num_tok(x) for x in re.findall(r"-?[\d,]+(?:\.\d+)?|-", m.group(0)[len(label):])) if v is not None]

    sales_row = row("매출액")
    op_row = row("영업이익")
    if sales_row:
        out["sales"] = sales_row[0] * unit
        if len(sales_row) >= 4 and sales_row[3]:
            # often: 당기, 전기, 전기대비%, 전년동기, 전년동기대비%
            pass
        # try percent tokens after sales
        m = re.search(r"매출액[\s\S]{0,120}?((-?\d+(?:\.\d+)?)\s*%)", t)
        if m:
            out["sales_yoy"] = float(m.group(2))
    if op_row:
        out["op"] = op_row[0] * unit
    # margin line
    m = re.search(r"영업이익률[^\d\-]*(-?\d+(?:\.\d+)?)\s*%", t)
    if m:
        out["margin"] = float(m.group(1))
    elif out["sales"] and out["op"] is not None and out["sales"]:
        out["margin"] = out["op"] / out["sales"] * 100.0
    # YoY % near 전년동기대비 after 영업이익
    m = re.search(r"영업이익[\s\S]{0,200}?전년동기대비[^\d\-]*(-?\d+(?:\.\d+)?)\s*%", t)
    if m:
        out["op_yoy"] = float(m.group(1))
    m = re.search(r"매출액[\s\S]{0,200}?전년동기대비[^\d\-]*(-?\d+(?:\.\d+)?)\s*%", t)
    if m:
        out["sales_yoy"] = float(m.group(1))
    return out


def quarter_ref(cfg: dict) -> str:
    try:
        req = urllib.request.Request(cfg["sources"]["naver_quarter"], headers=UA)
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = json.loads(resp.read().decode())
        fi = raw.get("financeInfo") or {}
        cols = {}
        for row in fi.get("rowList") or []:
            title = row.get("title") or ""
            c = (row.get("columns") or {}).get("202609") or {}
            if title in {"매출액", "영업이익"} and c.get("value") not in (None, ""):
                cols[title] = str(c.get("value"))
                cols[title + "_cons"] = c.get("isConsensus") == "Y" or any(
                    x.get("key") == "202609" and x.get("isConsensus") == "Y" for x in (fi.get("trTitleList") or [])
                )
        if not cols:
            return "네이버 분기 재무 2026.09 수치 없음"
        note = "컨센서스" if cols.get("매출액_cons") else "확정/잠정 열"
        return f"네이버 2026.09 분기({note}): 매출 {cols.get('매출액', '?')}, 영업이익 {cols.get('영업이익', '?')} (단위는 화면 기준, 억 환산은 원문 확인)"
    except Exception as exc:  # noqa: BLE001
        return f"네이버 분기 재무 조회 실패({type(exc).__name__})"


def new_biz_hits(text: str) -> list[str]:
    keys = ("전장", "ESS", "에너지저장", "전력변환", "충전기", "전기차", "EV", "태양광", "인버터", "SMPS")
    t = text or ""
    return [k for k in keys if k.casefold() in t.casefold()]


def earn_checklist(cfg: dict, parsed: dict, text: str) -> tuple[str, str]:
    th = cfg["thesis"]
    bits = []
    if parsed.get("sales") is not None:
        bits.append(f"매출 {parsed['sales']:,.0f}억")
    if parsed.get("op") is not None:
        bits.append(f"영업이익 {parsed['op']:,.0f}억")
    if parsed.get("margin") is not None:
        bits.append(f"영업이익률 {parsed['margin']:.1f}%")
    if parsed.get("sales_yoy") is not None:
        bits.append(f"매출 전년동기 {parsed['sales_yoy']:+.1f}%")
    if parsed.get("op_yoy") is not None:
        bits.append(f"영업이익 전년동기 {parsed['op_yoy']:+.1f}%")
    nums = ", ".join(bits) if bits else "숫자 자동 추출 실패(원문 확인)"
    biz = new_biz_hits(text)
    biz_s = ", ".join(biz) if biz else "신규사업 키워드 없음(제목/본문 기준)"
    sales = parsed.get("sales")
    margin = parsed.get("margin")
    sales_yoy = parsed.get("sales_yoy")
    # soft labels for review only
    rerate_ok = []
    down_ok = []
    if margin is not None and margin >= 5.0:
        rerate_ok.append("마진 5%대 이상(회복 후보, 직전 분기와 비교 필요)")
    if sales_yoy is not None and sales_yoy > 0:
        rerate_ok.append("매출 전년동기 증가")
    if biz:
        rerate_ok.append(f"신규사업 언급: {biz_s}")
    if sales_yoy is not None and sales_yoy < -5:
        down_ok.append("매출 전년동기 5% 이상 감소")
    if sales is not None and sales < th["ttm_sales_eok"] * 0.2:
        # quarterly vs TTM rough: TTM/4 ~ 1300
        pass
    if sales is not None and th["ttm_sales_eok"] and sales < 1100:
        down_ok.append("분기 매출이 낮은 편(TTM 약 5,200억 대비 분기 약세 가능, 원문 확인)")
    label = "재평가 후보 일부" if rerate_ok and not down_ok else ("하향 경계" if down_ok and not rerate_ok else "혼합/확인 필요")
    status = (
        f"추출 {nums}. 점검 라벨: {label}. "
        f"재평가 힌트: {'; '.join(rerate_ok) or '없음'}. "
        f"하향 힌트: {'; '.join(down_ok) or '없음'}. "
        f"신규사업 키워드: {biz_s}. "
        f"기준 메모: {th['ttm_eps_note']}. TTM 매출 약 {th['ttm_sales_eok']:,}억(감소 추세)."
    )
    action = (
        "원문에서 영업이익률 추이, 전장/ESS/충전기 수주/가이던스, 주주환원 언급을 확인하세요. "
        f"재평가 조건: {', '.join(th['rerate'])}. "
        f"하향 조건: {', '.join(th['downgrade'])}. "
        "이 알림만으로 매수/매도하지 마세요."
    )
    return status, action


def message_for(cfg: dict, rule: dict, row: dict, stamp: str, qref: str) -> tuple[str, str, int]:
    th = cfg["thesis"]
    link = cfg["sources"]["notice_page"]
    short = row["title"]
    for p in ("동양이엔피(주) ", "동양이엔피 주식회사 ", "동양이엔피 "):
        short = short.replace(p, "")
    short = short[:50]
    if rule["key"] == "earn":
        parsed = parse_earn(row.get("text") or "")
        st, act = earn_checklist(cfg, parsed, row.get("text") or "")
        title = f"{TAG} 동양이엔피 3Q 실적 공시 | 재평가 조건 점검" if (
            "분기보고서" in row["title"] or "잠정" in row["title"] or "실적" in row["title"]
        ) else f"{TAG} 동양이엔피 실적 관련 공시 | 재평가 조건 점검"
        # if outside q3 window still say 실적
        qw = cfg.get("q3_window") or {}
        if row["day"] < qw.get("start", "2026-11-10") or row["day"] > qw.get("end", "2026-11-20"):
            title = f"{TAG} 동양이엔피 실적 공시 | 재평가 조건 점검"
        if "30" in row["title"] and "손익" in row["title"]:
            title = f"{TAG} 동양이엔피 손익 변동 공시 | 재평가 조건 점검"
        what = "동양이엔피(079960) 실적/손익 공시 점검입니다. 극저평가 순현금 테제와 본업 둔화 리스크를 같이 봅니다. 자동 매수 신호 아님."
        status = (
            f"{row['datetime']} 공시: {row['title']}. {st} 참고 분기 데이터: {qref}. 목록: {link}"
        )
        later = "같은 공시 번호는 다시 안 보냅니다. 수주/자사주/배당/타법인 취득 공시는 따로 옵니다."
        trim = f"보유 기록이 저장소에 없습니다({cfg['position']['note']}). 비중은 본업 가시성과 현금 사용을 보고 스스로 정하세요."
        avoid = "잠정치를 확정으로 보기, 저PER만 보고 추격 매수, 공시 제목만으로 신규사업 성공 단정. 투자 권유 아님."
        return title, body(stamp, rule["label"], what, status, act, later, trim, avoid), int(rule["priority"])

    # non-earn
    biz = new_biz_hits(row.get("text") or "" + " " + row["title"])
    title = f"{TAG} 동양이엔피 {rule['label']} | 공시 점검"
    if rule["key"] == "contract":
        title = f"{TAG} 동양이엔피 수주/공급계약 공시 | 신규사업 가시성 점검"
    elif rule["key"] == "buyback":
        title = f"{TAG} 동양이엔피 자사주/소각 공시 | 주주환원 점검"
    elif rule["key"] == "dividend":
        title = f"{TAG} 동양이엔피 배당 공시 | 주주환원 점검"
    elif rule["key"] == "invest":
        title = f"{TAG} 동양이엔피 투자/현금투입 공시 | 현금 사용 점검"
    what = (
        f"동양이엔피(079960) {rule['label']} 공시 알림입니다. "
        f"재평가 조건({', '.join(th['rerate'])}) / 하향 조건({', '.join(th['downgrade'])}) 점검용. 자동 매수 신호 아님."
    )
    status = (
        f"{row['datetime']} 공시: {row['title']}. "
        f"본문 키워드: {', '.join(biz) if biz else '전장/ESS/충전기 등 특정 키워드 없음'}. "
        f"참고: {th['buyback_note']}. 목록: {link}"
    )
    if rule["key"] == "contract":
        act = "계약상대방, 금액, 기간, 전장/ESS/충전기/인버터 관련 여부를 원문에서 확인하세요. 수주가 본업 회복인지 신규사업인지 구분하세요."
    elif rule["key"] == "invest":
        act = "투입 규모와 대상(해외법인 대여, 타법인 지분 등)을 확인하세요. 본업 둔화 속에 현금이 과도하게 묶이면 하향 조건에 가깝습니다."
    elif rule["key"] in {"buyback", "dividend"}:
        act = "환원 규모와 지속 가능성을 확인하세요. 기업가치제고계획의 추가 환원 의지와 맞춰 보세요."
    else:
        act = "원문을 열고 테제 변경 조건에 해당하는지 표시만 하세요."
    later = "같은 공시 번호는 다시 안 보냅니다. 11/13 전후 실적 공시는 별도 체크리스트로 옵니다."
    trim = f"{cfg['position']['note']} 공시만으로 비중을 한꺼번에 바꾸지 마세요."
    avoid = "제목만 보고 추격 매매, 수주 금액을 추정해서 쓰기, 투자 권유로 해석하기."
    return title, body(stamp, rule["label"], what, status, act, later, trim, avoid), int(rule["priority"])


def ops_msg(stamp: str, status: str) -> tuple[str, str]:
    return (
        f"{TAG} 동양이엔피 공시 조회 실패 | 판정 건너뜀",
        body(
            stamp, "공시 읽기 실패",
            "동양이엔피 공시 감시의 데이터 읽기 실패 안내. 하루 1번. 매매 신호 아님.",
            status,
            "증권 앱이나 DART에서 079960 공시를 직접 보세요.",
            "다음 슬롯에서 다시 읽고, 못 본 공시는 그때 보냅니다.",
            "이 안내로 비중을 바꾸지 않기.",
            "조회 실패를 악재/호재로 해석하기.",
        ),
    )


def install_message(cfg: dict, stamp: str) -> tuple[str, str]:
    return (
        f"{TAG} 동양이엔피 설치 확인 | 매매 신호 아님",
        body(
            stamp, "설치 확인",
            "동양이엔피(079960) 공시 감시 알림 설치 확인입니다. 매매 신호가 아닙니다.",
            "평일 09:00, 15:20, 19:00 KST 에 네이버 공시를 봅니다. "
            "실적(잠정/분기/30%변동), 공급계약, 자사주/소각, 배당, 타법인취득/금전대여 등이 나오면 1번씩 "
            "재평가/하향 조건 점검을 보냅니다. 3분기 실적 예상일 2026-11-13 전후를 포함합니다. "
            f"{cfg['position']['note']}",
            "알림이 도착했는지 확인만 하세요.",
            f"끝: {cfg['ends_kst']} KST.",
            "해당 없음.",
            "이 메시지를 매매 신호로 해석하기.",
        ),
    )


def step(cfg: dict, st: dict, now: datetime, rows_fn=None, qref_fn=None) -> tuple[list[tuple[str, str, int]], str | None]:
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    today = session_day(now).isoformat()
    if today > cfg["window_end"]:
        return [], None
    if today < cfg["window_start"]:
        return [], None
    err = None
    try:
        rows = (rows_fn or notices_live)(cfg)
    except Exception as exc:  # noqa: BLE001
        return [], f"네이버 공시 피드 실패: {str(exc)[:160]}"
    seen = set(st.get("seen") or [])
    first = not st.get("seeded")
    out: list[tuple[str, str, int]] = []
    qref = "분기 참고 생략"
    # only fetch quarter ref when we will actually send an earn alert (or install handled elsewhere)
    pending = []
    for r in rows:
        if r["day"] < cfg["window_start"] or r["day"] > today:
            continue
        if r["id"] in seen:
            continue
        rule = match_rule(cfg, r["title"])
        if not rule:
            continue
        pending.append((rule, r))
    if first:
        for rule, r in pending:
            seen.add(r["id"])
            print(f"seed (no send): {r['id']} {r['datetime']} {r['title'][:60]}")
        st["seeded"] = True
        st["seeded_at"] = stamp
        st["seen"] = sorted(seen)
        return [], None
    need_q = any(rule["key"] == "earn" for rule, _ in pending)
    if need_q:
        qref = (qref_fn or quarter_ref)(cfg)
    for rule, r in pending:
        seen.add(r["id"])
        t, m, p = message_for(cfg, rule, r, stamp, qref)
        check_text(t, m)
        out.append((t, m, p))
        print(f"fire {rule['key']}: {t}")
    st["seen"] = sorted(seen)
    return out, err


def run(mode: str, now: datetime, cfg: dict, pub=publish, rows_fn=None, qref_fn=None) -> int:
    dry = mode == "dry"
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    topic = resolve_topic("port", cfg.get("ntfy_topic"))
    server = resolve_server(cfg.get("ntfy_server"))
    print(f"{stamp} {NAME} mode={mode}")
    if mode == "install":
        t, m = install_message(cfg, stamp)
        check_text(t, m)
        print(f"{t}\n{m}")
        pub(topic, t, m, 3, ["white_check_mark"], server, config_name=NAME)
        print("install confirmation sent (state untouched)")
        return 0
    if not is_trading_day(session_day(now)) and mode != "force":
        # allow dry on holiday for tests; auto skips
        if mode == "auto":
            print(f"{session_day(now)} KRX holiday; silent")
            return 0
    st = load_state()
    out, err = [], None
    try:
        out, err = step(cfg, st, now, rows_fn=rows_fn, qref_fn=qref_fn)
    except Exception as exc:  # noqa: BLE001
        err = f"스크립트 오류 {type(exc).__name__}: {str(exc)[:160]}"
        traceback.print_exc()
    ops = None
    today = session_day(now).isoformat()
    if err:
        print("ERR:", err)
        if st.get("ops_day") != today:
            ops = ops_msg(stamp, err)
            st["ops_day"] = today
    for t, m, pri in out:
        print(f"TITLE: {t}\nPRIORITY: {pri} TOPIC: {route_topic(topic, t)}\n{m}\n")
    if ops:
        check_text(ops[0], ops[1])
        print(f"OPS: {ops[0]}\n{ops[1]}\n")
    if not out and not ops:
        print("보낼 것 없음")
    if dry:
        print("DRY RUN: no publish, state not written")
        return 0
    for t, m, pri in out:
        pub(topic, t, m, pri, ["page_facing_up"], server, config_name=NAME)
    if ops:
        try:
            pub(resolve_topic("ops"), ops[0], ops[1], 3, ["warning"], server, config_name=NAME)
        except Exception as exc:  # noqa: BLE001
            print(f"ops failed: {exc}")
    st["updated_at"] = stamp
    save_state(st)
    return 0


def selftest() -> int:
    cfg = load_cfg()
    assert cfg["code"] == "079960"
    # match rules
    assert match_rule(cfg, "동양이엔피(주) 연결재무제표기준영업(잠정)실적(공정공시)")["key"] == "earn"
    assert match_rule(cfg, "동양이엔피(주) 분기보고서 (2026.09)")["key"] == "earn"
    assert match_rule(cfg, "동양이엔피(주) 단일판매, 공급계약체결")["key"] == "contract"
    assert match_rule(cfg, "동양이엔피(주) 공급계약해지") is None
    assert match_rule(cfg, "동양이엔피(주) 주식소각 결정")["key"] == "buyback"
    assert match_rule(cfg, "동양이엔피(주) 현금, 현물배당결정")["key"] == "dividend"
    assert match_rule(cfg, "동양이엔피(주) 타법인주식및출자증권취득결정")["key"] == "invest"
    # parse
    sample = (
        "연결재무제표 기준 영업(잠정)실적(공정공시) 단위 : 백만원 "
        "매출액 당해실적 130,000 140,000 -7.1 - 150,000 -13.3 - "
        "영업이익 당해실적 6,500 5,000 30.0 - 4,000 62.5 - 영업이익률 5.0%"
    )
    p = parse_earn(sample)
    assert p["sales"] == 1300 and p["op"] == 65 and abs(p["margin"] - 5.0) < 0.01, p
    # messages + forbidden
    row = {
        "id": "E1", "title": "동양이엔피(주) 연결재무제표기준영업(잠정)실적(공정공시)",
        "datetime": "2026-11-13 15:30", "day": "2026-11-13", "text": sample + " ESS 전력변환 수주", "url": "u",
    }
    t, m, pri = message_for(cfg, match_rule(cfg, row["title"]), row, "T", "qref")
    check_text(t, m)
    assert "079960" in t and "재평가" in t and pri == 4
    assert "영업이익률 5.0%" in m and "ESS" in m
    # step seed then fire
    st: dict = {}
    rows = [
        {"id": "OLD", "title": "동양이엔피(주) 주식소각 결정", "datetime": "2026-11-01 09:00", "day": "2026-11-01", "text": "", "url": "u"},
        {"id": "N1", "title": "동양이엔피(주) 단일판매, 공급계약체결", "datetime": "2026-11-12 09:00", "day": "2026-11-12", "text": "전장 부품 공급", "url": "u"},
        {"id": "N2", "title": row["title"], "datetime": row["datetime"], "day": row["day"], "text": row["text"], "url": "u"},
    ]
    now = datetime(2026, 11, 12, 19, 0, tzinfo=KST)
    out, err = step(cfg, st, now, rows_fn=lambda _c: rows, qref_fn=lambda _c: "qref")
    assert err is None and out == [] and st.get("seeded") and "N1" in st["seen"] and "OLD" not in st["seen"]
    # OLD before window_start should not seed; N1 on 11-12 seeds
    out, err = step(cfg, st, datetime(2026, 11, 13, 19, 0, tzinfo=KST), rows_fn=lambda _c: rows, qref_fn=lambda _c: "qref")
    assert err is None and len(out) == 1 and "3Q 실적" in out[0][0], [x[0] for x in out]
    out2, _ = step(cfg, st, datetime(2026, 11, 14, 19, 0, tzinfo=KST), rows_fn=lambda _c: rows, qref_fn=lambda _c: "qref")
    assert out2 == []
    # dry path
    rc = run(
        "dry",
        datetime(2026, 11, 13, 19, 0, tzinfo=KST),
        cfg,
        pub=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no pub")),
        rows_fn=lambda _c: rows,
        qref_fn=lambda _c: "qref",
    )
    assert rc == 0
    for t, m in (ops_msg("T", "x"), install_message(cfg, "T")):
        check_text(t, m)
        assert "079960" in t
    print("selftest ok")
    return 0


def main() -> int:
    args = sys.argv[1:]
    mode = (args[0] if args and not args[0].startswith("--") else "auto").lower()
    if mode == "selftest":
        return selftest()
    if mode not in {"auto", "dry", "install", "force"}:
        print(f"usage: {NAME}_monitor.py <auto|dry|install|force|selftest> [--as-of 'YYYY-MM-DD HH:MM']")
        return 2
    cfg = load_cfg()
    now = datetime.now(KST)
    if "--as-of" in args:
        if mode != "dry":
            print("--as-of 는 dry 에서만")
            return 2
        now = datetime.strptime(args[args.index("--as-of") + 1], "%Y-%m-%d %H:%M").replace(tzinfo=KST)
    try:
        return run(mode, now, cfg)
    except Exception as exc:  # noqa: BLE001
        print(traceback.format_exc(), file=sys.stderr)
        if mode != "auto":
            return 1
        try:
            t, m = ops_msg(now.strftime("%Y-%m-%d %H:%M KST"), f"스크립트 오류 {type(exc).__name__}: {str(exc)[:200]}")
            publish(resolve_topic("ops"), t, m, 3, ["warning"], resolve_server(cfg.get("ntfy_server")), config_name=NAME)
        except Exception:  # noqa: BLE001
            return 1
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
