#!/usr/bin/env python3
"""[SDI-ESS] 삼성SDI(006400) 단일판매 공급계약 체결 공시 감시 (2026-10-29 ~ 2027-03-31 19:00).

morning 09:00 / afternoon 15:20 / k203780 19:00 슬롯. KIND 상세검색(실패하면 네이버 KRX 공시 피드)에서
'단일판매ㆍ공급계약체결'(자율공시 포함)과 그 정정을 공시마다 1번(p4). 계약상대방, 계약금액, 최근 매출액 대비 비율,
계약기간, 링크, 계약내용에 ESS 나 배터리 언급 여부를 붙임. 1조원 이상이면 제목에 '조 단위 수주'.
유보된 항목은 유보라고만 적고 추정하지 않음(언론 추정치 안 씀). 첫 실행은 이미 있는 공시를 seen 으로만 기록.
읽기 실패는 cwally-ops 하루 1번.
modes: auto | dry(발송 없음, state 안 씀) | install(설치 확인 1회, state 안 씀) | selftest ; --as-of "YYYY-MM-DD HH:MM" (dry 전용)
"""
from __future__ import annotations

import json
import re
import sys
import traceback
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import secdiv_monitor as sd  # noqa: E402  (읽기 전용 재사용: http, text_of, kind_body, KIND, KIND_VIEW)
from kr_calendar import KST  # noqa: E402
from ntfy_util import publish, resolve_server, resolve_topic, route_topic  # noqa: E402

TAG = "[SDI-ESS]"
NAME = "sdiess"
CFG_PATH = ROOT / "configs" / f"{NAME}.json"
STATE_PATH = ROOT / "state" / f"{NAME}.json"
NAVER_NOTICE = "https://finance.naver.com/item/news_notice.naver?code=006400"
DOT = "[\u318d\u00b7\u2027\u30fb]"
TITLE_RE = re.compile(r"단일판매\s*" + DOT + r"?\s*공급계약\s*체결")
ESS_RE = re.compile(r"ESS|BESS|에너지\s*저장|Energy\s*Storage", re.I)
BAT_RE = re.compile(r"배터리|이차전지|2차전지|전지|Battery", re.I)


def is_target(title: str) -> bool:
    return bool(TITLE_RE.search(title or "")) and "해지" not in (title or "")


def is_correction(title: str) -> bool:
    return "정정" in (title or "")


def show_title(t: str) -> str:
    return re.sub(r"\s*" + DOT + r"\s*", " ", t or "").strip()


def norm_title(t: str) -> str:
    return re.sub(r"\s+|" + DOT, "", t or "")


# ---------- 목록 ----------
def kind_list(cfg: dict, since: str, until: str) -> list[dict]:
    form = {
        "method": "searchDetailsSub", "currentPageSize": "100", "pageIndex": "1", "orderMode": "1", "orderStat": "D",
        "forward": "details_sub", "searchCodeType": "", "repIsuSrtCd": "A" + cfg["code"], "allRepIsuSrtCd": "",
        "oldSearchCorpName": "", "disclosureType": "", "disTypevalue": "", "reportNm": "", "reportCd": "",
        "searchCorpName": cfg["code"], "business": "", "marketType": "", "settlementMonth": "", "securities": "",
        "submitOblgNm": "", "enterprise": "", "fromDate": since, "toDate": until, "reportNmTemp": "", "bfrDsclsType": "on",
    }
    page = sd.http(sd.KIND + "/disclosure/details.do", data=form,
                   headers={"X-Requested-With": "XMLHttpRequest", "Referer": sd.KIND + "/disclosure/details.do?method=searchDetailsMain"})
    out, rows = [], 0
    for row in re.findall(r"(?s)<tr[^>]*>(.*?)</tr>", page):
        acpt = re.search(r"openDisclsViewer\('(\d{14})'", row)
        if not acpt:
            continue
        rows += 1
        t = sd.text_of(row)
        dm = re.search(r"(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})", t)
        title = t[dm.end():].strip() if dm else t
        title = re.sub(r"\s*(공시차트|주가차트).*$", "", title).strip()
        title = re.sub(r"^" + cfg["corp_name"] + r"\s+", "", title)
        title = re.sub(r"\s+\S*(" + cfg["corp_name"] + r"|시장본부)$", "", title).strip()
        out.append({"id": "kind:" + acpt.group(1), "acptno": acpt.group(1), "title": title,
                    "filed": f"{dm.group(1)} {dm.group(2)}" if dm else "", "src": "KIND"})
    if rows == 0 and "조회된 결과값이 없습니다" not in page and "<table" not in page:
        raise RuntimeError("KIND 응답 형식 이상")
    return sorted(out, key=lambda r: r["acptno"])


def naver_list(cfg: dict, since: str, until: str) -> list[dict]:
    raw = json.loads(sd.http(f"https://stock.naver.com/api/domestic/detail/notice?itemCode={cfg['code']}&page=1&pageSize=100"))
    out = []
    for r in raw if isinstance(raw, list) else []:
        title = re.sub(r"^삼성SDI\(주\)\s*", "", str(r.get("title") or "").strip())
        day = str(r.get("datetime") or "")[:10]
        if since <= day <= until:
            out.append({"id": "naver:" + str(r.get("no") or r.get("bizdateSeq")), "acptno": None, "title": title,
                        "filed": day, "src": "네이버 KRX 공시 피드", "body": sd.text_of(str(r.get("contents") or ""))})
    return sorted(out, key=lambda r: r["id"])


def fetch_filings(cfg: dict, since: str, until: str) -> tuple[list[dict], list[str]]:
    notes: list[str] = []
    try:
        reps = [r for r in kind_list(cfg, since, until) if is_target(r["title"])]
        nav = None
        for r in reps:
            try:
                r["body"] = sd.kind_body(r["acptno"])
            except Exception as exc:  # noqa: BLE001
                notes.append(f"KIND 본문 {r['acptno']}: {exc}")
                try:
                    nav = nav if nav is not None else naver_list(cfg, since, until)
                    m = [n for n in nav if n["filed"] == r["filed"][:10] and norm_title(n["title"]) == norm_title(r["title"])]
                    r["body"] = m[-1]["body"] if m else ""
                except Exception as exc2:  # noqa: BLE001
                    notes.append(f"네이버 본문: {exc2}")
                    r["body"] = ""
        return reps, notes
    except Exception as exc:  # noqa: BLE001
        notes.append(f"KIND 목록: {exc}")
    return [r for r in naver_list(cfg, since, until) if is_target(r["title"])], notes


# ---------- 본문 ----------
def _n(tok: str | None) -> float | None:
    if tok is None or tok.strip() in {"-", ""}:
        return None
    try:
        return float(tok.replace(",", ""))
    except ValueError:
        return None


def _g(pat: str, s: str) -> str | None:
    m = re.search(pat, s)
    return m.group(1).strip() if m else None


def parse(text: str) -> dict | None:
    t = re.sub(r"\s+", " ", text or "")
    anchors = [m.start() for m in re.finditer(r"단일판매\s*" + DOT + r"?\s*공급계약\s*체결\s*1\.\s*판매", t)]
    if not anchors:
        return None
    s = t[anchors[-1]:]
    amt_raw = _g(r"계약금액\s*\(원\)\s*(-|[\d,]+)", s)
    out = {
        "name": _g(r"체결계약명\s*(.+?)\s*2\.\s*계약내역", s),
        "amount": _n(amt_raw), "amount_raw": amt_raw,
        "sales": _n(_g(r"최근\s*매출액\s*\(원\)\s*(-|[\d,]+)", s)),
        "ratio": _n(_g(r"매출액\s*대비\s*\(%\)\s*(-|[\d.]+)", s)),
        "party": _g(r"3\.\s*계약상대(?:방)?\s*(.+?)\s*-\s*회사와(?:의)?\s*관계", s),
        "region": _g(r"4\.\s*판매\s*" + DOT + r"?\s*공급지역\s*(.+?)\s*5\.\s*계약기간", s),
        "start": _g(r"계약기간\s*시작일\s*(\S+)", s), "end": _g(r"계약기간\s*시작일\s*\S+\s*종료일\s*(\S+)", s),
        "hold_reason": _g(r"유보사유\s*(.+?)\s*-?\s*유보기한", s), "hold_until": _g(r"유보기한\s*(\S+)", s),
        "etc": _g(r"9\.\s*기타\s*투자판단(?:과|에)\s*(?:관련한|참고할)\s*(?:중요)?사항\s*(.+?)(?:※\s*관련공시|$)", s),
        "corr_reason": _g(r"정정사유\s*(.+?)\s*4\.\s*정정사항", t),
    }
    if out["amount"] is None and out["party"] is None and out["name"] is None:
        return None
    return out


def held(v: str | None) -> bool:
    return v is None or v.strip() in {"-", "", "비공개", "공시유보", "유보"}


def ess_flags(p: dict | None) -> tuple[bool, bool]:
    if not p:
        return False, False
    txt = " ".join(str(p.get(k) or "") for k in ("name", "etc", "region"))
    return bool(ESS_RE.search(txt)), bool(BAT_RE.search(txt))


def won(v: float | None) -> str:
    if v is None:
        return "?"
    return f"{v / 1e12:,.2f}조원" if v >= 1e12 else f"{v / 1e8:,.0f}억원"


def detail(p: dict | None) -> tuple[str, list[str]]:
    if not p:
        return "본문 숫자를 자동으로 못 읽음. 링크에서 계약상대방, 금액, 기간을 직접 확인하세요. 추정하지 않음.", []
    withheld, parts = [], []
    hr = p.get("hold_reason")
    hold_note = f" (유보사유 {hr}, 유보기한 {p.get('hold_until') or '?'})" if hr and hr != "-" else ""
    if held(p.get("party")):
        withheld.append("계약상대방")
        parts.append(f"계약상대방: 유보{hold_note}, 추정하지 않음")
    else:
        parts.append(f"계약상대방: {p['party']}")
    if p.get("amount") is None:
        withheld.append("계약금액")
        parts.append(f"계약금액: 유보{hold_note}, 추정하지 않음")
    else:
        parts.append(f"계약금액: {won(p['amount'])}({p['amount']:,.0f}원)")
    parts.append(f"최근 매출액 대비: {p['ratio']}%" if p.get("ratio") is not None else "최근 매출액 대비: 유보 또는 미기재")
    if p.get("sales"):
        parts.append(f"최근 매출액 {won(p['sales'])}")
    parts.append(f"계약기간: {p.get('start') or '?'} ~ {p.get('end') or '?'}")
    if p.get("name"):
        parts.append(f"계약명: {p['name'][:60]}")
    if p.get("region") and not held(p.get("region")):
        parts.append(f"공급지역: {p['region'][:30]}")
    ess, bat = ess_flags(p)
    parts.append("계약내용 언급: " + ("ESS 있음" if ess else "ESS 없음") + ", " + ("배터리 있음" if bat else "배터리 없음"))
    if p.get("corr_reason"):
        parts.append(f"정정사유: {p['corr_reason'][:80]}")
    return ". ".join(parts) + ".", withheld


def link_of(r: dict) -> str:
    return sd.KIND_VIEW.format(acpt=r["acptno"]) if r.get("acptno") else f"{NAVER_NOTICE} (네이버 공시 목록, KIND 목록을 못 읽음)"


def body(stamp: str, hdr: str, what: str, status: str, action: str, later: str, trim: str, avoid: str) -> str:
    return "\n".join([f"{stamp} | 006400 | 삼성SDI {hdr}", f"이게 뭐 알림: {what}", f"지금 결과/상태: {status}",
                      f"지금 할 일: {action}", f"나중에: {later}", f"익절/줄일 때: {trim}", f"하지 말 것: {avoid}"])


WHAT = "삼성SDI(006400) 단일판매 공급계약 체결 공시(자율공시, 정정 포함)가 나올 때마다 1번 보내는 알림. 매매 지시 아님."


def clean(s: str) -> str:
    return re.sub(r"\s*" + DOT + r"\s*", ", ", s).replace("\u2014", ", ")


def message(cfg: dict, r: dict, p: dict | None, stamp: str) -> tuple[str, str, int]:
    corr = is_correction(r["title"])
    big = bool(p and p.get("amount") and p["amount"] >= cfg["big_order_krw"])
    ess, bat = ess_flags(p)
    line, withheld = detail(p)
    tags = []
    if big:
        tags.append("조 단위 수주")
    if ess:
        tags.append("ESS")
    elif bat:
        tags.append("배터리")
    if withheld:
        tags.append(" ".join(withheld) + " 유보")
    title = f"{TAG} 006400 {'정정 ' if corr else ''}공급계약 체결" + (f", {', '.join(tags)}" if tags else "")
    st = f"{r['filed']} 접수 '{show_title(r['title'])}' ({r['src']}). {line} 링크: {link_of(r)}"
    if big:
        act = "조 단위 수주. 사용자 상향 조건 충족이니 [SDI-PX] 가격선과 [SDI] 4분기 실적 점검을 같이 보고 2차 분할매수 계획 재검토."
    elif corr:
        act = "정정 사유와 바뀐 항목(금액, 상대방, 기간)을 원문 정정 표에서 확인하세요."
    else:
        act = "원문에서 계약 규모와 기간을 확인. 1조원 미만이면 계획은 그대로 두고 기록만."
    later = "같은 계약의 정정(유보 해제 포함)이나 새 공급계약 공시가 나오면 1번씩 더 보냅니다."
    trim = "수주 공시만으로 비중을 늘리지 말고 실적 확인 뒤 지정가로."
    avoid = "언론 추정치나 유보된 금액을 짐작해서 판단하기."
    return title, clean(body(stamp, "공급계약 공시", WHAT, st, act, later, trim, avoid)), cfg["priority"]


# ---------- 실행 ----------
def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(st: dict) -> None:
    STATE_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def check_text(t: str, m: str) -> None:
    assert not re.search("[\u00b7\u318d\u2014]", t + m), t


def ops_msg(stamp: str, status: str) -> tuple[str, str]:
    return (f"{TAG} 006400 공시 조회 실패 | 원문 확인 필요",
            body(stamp, "공시 읽기 실패", "삼성SDI 공급계약 공시 감시의 데이터 읽기 실패 안내. 하루 1번. 매매 신호 아님.", status,
                 "KIND 나 DART 에서 삼성SDI 공시를 직접 보세요.", "다음 슬롯에서 다시 읽고, 못 본 공시는 그때 보냅니다.",
                 "이 안내로 비중을 바꾸지 않기.", "읽기 실패를 공시 없음으로 읽기."))


def install_message(stamp: str) -> tuple[str, str]:
    return (f"{TAG} 006400 설치 확인 | 매매 신호 아님",
            body(stamp, "설치 확인", "삼성SDI 공급계약 공시 감시 설치 확인용 테스트 발송입니다. 매매 신호가 아닙니다.",
                 "평일 09:00, 15:20, 19:00 KST 에 KIND 공시를 보고 단일판매 공급계약 체결(자율공시, 정정 포함)이 나오면 공시마다 1번 보냅니다. "
                 "계약상대방, 금액, 매출액 대비 비율, 기간, 링크, ESS 나 배터리 언급 여부를 붙이고, 1조원 이상이면 제목에 조 단위 수주.",
                 "알림이 도착했는지 확인만 하세요.", "유보된 항목은 유보라고만 적고 추정하지 않습니다. 2027-03-31 19:00 에 끝납니다.",
                 "해당 없음.", "이 메시지를 매매 신호로 해석하기."))


def step(cfg: dict, st: dict, now: datetime, stamp: str, errs: list[str], fetch=None) -> list[tuple[str, str, int]]:
    today = now.strftime("%Y-%m-%d")
    if not (cfg["window_start"] <= today <= cfg["window_end"]):
        print(f"{today} 감시 기간 밖({cfg['window_start']} ~ {cfg['window_end']}): 조용히 종료")
        return []
    try:
        reps, notes = (fetch or fetch_filings)(cfg, cfg["window_start"], today)
    except Exception as exc:  # noqa: BLE001
        errs.append(f"KIND, 네이버 공시 피드 모두 조회 실패: {str(exc)[:120]}")
        return []
    for n in notes:
        print("note:", n)
    seen, nav_keys = set(st.get("seen") or []), set(st.get("naver_keys") or [])
    first = not st.get("seeded_at")
    out, sent = [], st.setdefault("sent", [])
    for r in reps:
        key = f"{r['filed'][:10]}|{norm_title(r['title'])}"
        if r["id"] in seen or key in nav_keys:
            continue
        seen.add(r["id"])
        if not r.get("acptno"):
            nav_keys.add(key)
        if first:
            print(f"seed (no send): {r['id']} {r['filed']} {r['title']!r}")
            continue
        p = parse(r.get("body") or "")
        print(f"filing {r['id']} {r['filed']} {r['title']!r} parsed={p}")
        out.append((*message(cfg, r, p, stamp), r.get("acptno") or r["id"]))
        sent.append({"id": r["id"], "filed": r["filed"], "title": show_title(r["title"]), "amount": (p or {}).get("amount"), "at": stamp})
    if first:
        st["seeded_at"] = stamp
        print(f"first run: {len(seen)} existing filing(s) recorded as seen, no send")
    st["seen"], st["naver_keys"] = sorted(seen), sorted(nav_keys)
    if not out and not first:
        print("새 공급계약 공시 없음")
    return out


def run(mode: str, now: datetime, cfg: dict, pub=publish) -> int:
    dry = mode == "dry"
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    topic, server = resolve_topic("port", cfg.get("ntfy_topic")), resolve_server(cfg.get("ntfy_server"))
    print(f"{stamp} {NAME} mode={mode}")
    if mode == "install":
        t, m = install_message(stamp)
        check_text(t, m)
        print(f"{t}\n{m}")
        pub(topic, t, m, 3, ["white_check_mark"], server, config_name=NAME)
        print("install confirmation sent (state untouched)")
        return 0
    st = load_state()
    errs: list[str] = []
    out = step(cfg, st, now, stamp, errs)
    ops, today = None, now.strftime("%Y-%m-%d")
    if errs and st.get("ops_day") != today:
        ops = ops_msg(stamp, " / ".join(errs) + ". state 는 그대로.")
        st["ops_day"] = today
    for t, m, pri, _key in out:
        check_text(t, m)
        print(f"TITLE: {t}\nPRIORITY: {pri} TOPIC: {route_topic(topic, t)}\n{m}\n")
    if ops:
        print(f"OPS TITLE: {ops[0]} -> {route_topic(topic, ops[0])}\n{ops[1]}\n")
    if dry:
        print("DRY RUN: no publish, state not written")
        return 0
    for t, m, pri, key in out:
        pub(topic, t, m, pri, ["battery"], server, config_name=NAME, dedup_key=str(key) if key else None)
    if ops:
        try:
            pub(resolve_topic("ops"), ops[0], ops[1], 3, ["warning"], server, config_name=NAME)
        except Exception as exc:  # noqa: BLE001
            print(f"ops notice failed: {exc}")
    st["updated_at"] = stamp
    save_state(st)
    return 0


SAMPLE = ("단일판매ㆍ공급계약 체결 정정신고(보고) 정정일자 2026-10-01 1. 정정관련 공시서류 단일판매·공급계약 체결 3. 정정사유 유보기간 만료에 따른 공시유보 해제 "
          "4. 정정사항 정정항목 정정전 정정후 ... - 단일판매ㆍ공급계약 체결 1. 판매ㆍ공급계약 구분 기타 판매ㆍ공급계약 - 체결계약명 ESS 배터리 공급계약 "
          "2. 계약내역 계약금액(원) 1,281,310,030,251 최근매출액(원) 33,745,469,740,463 매출액대비(%) 3.8 대규모법인여부 해당 3. 계약상대 Hanwha Q CELLS USA Corp "
          "- 회사와의 관계 - 4. 판매ㆍ공급지역 미국 5. 계약기간 시작일 2024-05-16 종료일 2026-10-01 6. 주요 계약조건 계약금ㆍ선급금 유무 무 "
          "7. 계약(수주)일자 2024-05-16 8. 공시유보 관련내용 유보사유 - 유보기한 - 9. 기타 투자판단과 관련한 중요사항 가. 총 4.8GWh 규모의 ESS(Energy Storage System) 배터리 공급 ※ 관련공시 -")
SAMPLE_HELD = ("단일판매ㆍ공급계약체결(자율공시) 단일판매ㆍ공급계약 체결 1. 판매ㆍ공급계약 구분 기타 판매ㆍ공급계약 - 체결계약명 전기차용 배터리 공급계약 "
               "2. 계약내역 계약금액(원) - 최근매출액(원) 16,592,000,000,000 매출액대비(%) - 대규모법인여부 해당 3. 계약상대 - - 회사와의 관계 - "
               "4. 판매ㆍ공급지역 북미 5. 계약기간 시작일 2027-01-01 종료일 2031-12-31 6. 주요 계약조건 7. 계약(수주)일자 2026-11-20 "
               "8. 공시유보 관련내용 유보사유 경영상 비밀유지 유보기한 2031-12-31 9. 기타 투자판단과 관련한 중요사항 가. 계약상대방과의 비밀유지 ※ 관련공시 -")


def selftest() -> int:
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    for t in ("단일판매ㆍ공급계약체결", "단일판매ㆍ공급계약체결(자율공시)", "[정정] 단일판매ㆍ공급계약체결", "단일판매·공급계약 체결"):
        assert is_target(t), t
    for t in ("단일판매ㆍ공급계약해지", "기타 경영사항(자율공시)", "연결재무제표기준영업(잠정)실적(공정공시)"):
        assert not is_target(t), t
    p = parse(SAMPLE)
    assert p["amount"] == 1281310030251 and p["ratio"] == 3.8 and p["party"] == "Hanwha Q CELLS USA Corp" and p["end"] == "2026-10-01", p
    assert ess_flags(p) == (True, True)
    rep = {"id": "kind:20261120000001", "acptno": "20261120000001", "title": "[정정] 단일판매ㆍ공급계약체결", "filed": "2026-11-20 16:00", "src": "KIND", "body": SAMPLE}
    t, m, pri = message(cfg, rep, p, "T")
    assert "조 단위 수주" in t and "ESS" in t and pri == 4 and "1.28조원" in m and "ESS 있음" in m and "정정사유" in m, (t, m)
    h = parse(SAMPLE_HELD)
    assert h["amount"] is None and held(h["party"]) and h["hold_reason"] == "경영상 비밀유지", h
    t2, m2, _ = message(cfg, {**rep, "title": "단일판매ㆍ공급계약체결(자율공시)", "body": SAMPLE_HELD}, h, "T")
    assert "계약상대방 계약금액 유보" in t2 and "조 단위" not in t2 and "추정하지 않음" in m2 and "배터리 있음" in m2 and "ESS 없음" in m2, (t2, m2)
    now = datetime(2026, 11, 20, 19, 0, tzinfo=KST)
    st: dict = {}
    assert step(cfg, st, now, "T", [], fetch=lambda c, a, b: ([rep], [])) == [] and st["seeded_at"]
    rep2 = {**rep, "id": "kind:20261121000002", "acptno": "20261121000002", "title": "단일판매ㆍ공급계약체결", "body": SAMPLE_HELD}
    out = step(cfg, st, now, "T", [], fetch=lambda c, a, b: ([rep, rep2], []))
    assert len(out) == 1 and "유보" in out[0][0], out
    assert step(cfg, st, now, "T", [], fetch=lambda c, a, b: ([rep, rep2], [])) == []
    nob = message(cfg, {**rep, "body": ""}, None, "T")
    assert "직접 확인" in nob[1]
    assert step(cfg, {"seeded_at": "x"}, datetime(2027, 4, 1, 9, 0, tzinfo=KST), "T", [], fetch=lambda c, a, b: ([rep], [])) == []
    for tt, mm, _ in (message(cfg, rep, p, "T"), (t2, m2, 0), nob, (*ops_msg("T", "x"), 0), (*install_message("T"), 0)):
        check_text(tt, mm)
    print("selftest ok")
    return 0


def main() -> int:
    args = sys.argv[1:]
    mode = (args[0] if args and not args[0].startswith("--") else "auto").lower()
    if mode == "selftest":
        return selftest()
    if mode not in {"auto", "dry", "install"}:
        print(f"usage: {NAME}_monitor.py <auto|dry|install|selftest> [--as-of 'YYYY-MM-DD HH:MM']")
        return 2
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
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
            t, m = ops_msg(now.strftime("%Y-%m-%d %H:%M KST"), f"스크립트 오류 {type(exc).__name__}: {str(exc)[:200]}. state 는 그대로.")
            publish(resolve_topic("ops"), t, m, 3, ["warning"], resolve_server(cfg.get("ntfy_server")), config_name=NAME)
        except Exception:  # noqa: BLE001
            return 1
        return 0


if __name__ == "__main__":
    sys.exit(main())
