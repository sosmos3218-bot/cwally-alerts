#!/usr/bin/env python3
"""[SEMIUS] 삼성전자(005930), SK하이닉스(000660) 수요 리스크 감시 (사용자 승인 설계 2026-11-05, pt58u).

1) 미국 빅테크 2027 capex 하향 (AMZN, MSFT, GOOGL, META). 출처는 SEC EDGAR 공식 공시뿐:
   submissions JSON -> 처음 보는 8-K(2.02/7.01/8.01, Ex.99 보도자료 포함), 10-Q, 10-K.
   (a) 2027 capex 숫자 가이던스 중간값이 저장된 직전 2027 가이던스(또는 같은 문장의 prior outlook)보다 낮으면 p4.
       직전값이 null 이면 저장만. (b) 2027 capex 가 2026 대비 줄어든다고 명시한 문장이면 p4.
   'growth will moderate/slow' 같은 증가율 둔화는 하향 아님: 보내지 않고 state log 에만 남김.
   접수번호마다 1번만 판정, 회사별 공시 1건당 p4 1번.
2) 텍사스 데이터센터 모라토리엄 법적 효력.
   (a) Texas Legislature Online 90R 접수 법안 보고서(하원, 상원)에서 caption 에 data center/large load 가 있는 법안의
       History 페이지를 읽어 caption+subjects 에 moratorium/pause/신규 건설, 허가 금지가 있으면 추적.
       접수만으로는 발송 없음. 위원회 통과 p3 1번, 주지사 서명/Effective/서명 없이 성립 p4 1번.
   (b) 주지사 행정명령(gov.texas.gov executive-order 카테고리 + 뉴스 RSS) 중 데이터센터 모라토리엄/중단 -> p4 1번.
   PUCT 규칙 채택은 안정적으로 읽을 목록이 없어 넣지 않음.
일정: configs/semius.json phases. 2027-01-11 까지 ISO 주 1번(첫 morning 실행), 2027-01-12 ~ 06-02 평일 매일.
조회/파싱 실패는 earnings_parse.safe_parse 로 잡고 cwally-ops 에 하루 1번 묶어서 1통. 실패한 공시나 법안으로는 절대 p4 를 보내지 않음.
modes: auto | dry (발송 없음, state 안 씀, 날짜 거름 무시) | selftest (네트워크 없음) | seed (지금 접수번호와 기준선 저장, 발송 없음)
"""
from __future__ import annotations

import copy
import html
import json
import re
import sys
import time
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic, route_topic  # noqa: E402
from earnings_parse import safe_parse  # noqa: E402  ops once-a-day parse failure helper
import capex_monitor as cx  # noqa: E402  shared SEC EDGAR fetch + sentence/amount helpers

KST = timezone(timedelta(hours=9))
NAME = "semius"
CFG_PATH = ROOT / "configs" / f"{NAME}.json"
STATE_PATH = ROOT / "state" / f"{NAME}.json"
TICKERS = "005930 000660"
HEAD = "[SEMIUS] 005930 000660"  # literal tickers so title_ticker_check sees them (same text as before)
OPS_TITLE = "[OPS] SEMIUS 파싱 실패 [005930 000660] | 판정 유보"
BANNED = ("\u00b7", "\u318d", "\u3186", "\u2027", "\u2219", "\u2014", "\u2013", "ㅉ")
UA_WEB = {"User-Agent": "Mozilla/5.0 (cwally-alerts semius)"}


# ================================================================ text helpers
def check_text(title: str, msg: str) -> None:
    assert "005930" in title and "000660" in title, title
    assert "매수" not in title, title
    for b in BANNED:
        assert b not in title + msg, f"banned {b!r}"


def q(s: str, cap: int = 300) -> str:
    s = cx.clean(re.sub(r"\s+", " ", s)).strip()
    for b in BANNED:
        s = s.replace(b, " ")
    return s if len(s) <= cap else s[:cap] + "..."


def finish(lines: list[str], cfg: dict) -> str:
    return "\n".join(lines + [cfg["footer"]])


# ================================================================ capex classifier
CAPEX_RE = re.compile(r"(?i)\b(?:cash\s+)?(?:capital\s+expenditures?|capex|capital\s+spending)\b")
FWD_RE = re.compile(r"(?i)\b(expect\w*|anticipat\w*|will|plan\w*|outlook|guid\w*|forecast\w*|intend\w*|project\w*|should|to be)\b")
NEG_RE = re.compile(r"(?i)\b(not|no|never|won't|isn't|aren't|nor|rather than)\b")
GROWTH_RE = re.compile(r"(?i)\b(growth|grow\w*|rate|pace|increase\w*|year[- ]over[- ]year)\b")
_ADV = r"(?:\w+ly\s+)?"
_CAP = r"(?:cash\s+)?(?:capital\s+expenditures?|capex|capital\s+spending)"
CUT_RES = [
    re.compile(r"(?i)" + _CAP + r"[^.;]{0,100}?\b(?:to\s+|will\s+|would\s+|should\s+)?" + _ADV +
               r"(?:decreas\w*|declin\w*|be\s+" + _ADV + r"(?:lower|down|reduced|less|smaller)|fall|drop|come\s+down|be\s+cut|shrink)\b"),
    re.compile(r"(?i)\b(?:reduce|reducing|lower|lowering|cut|cutting|decrease|decreasing|scale\s+back|pull\s+back\s+on)\s+(?:our\s+)?(?:\w+\s+){0,3}?" + _CAP),
    re.compile(r"(?i)\b(?:lower|reduced|decreased|smaller)\s+(?:\w+\s+){0,2}?" + _CAP),
    re.compile(r"(?i)" + _CAP + r"[^.;]{0,120}?\b(?:below|lower\s+than|less\s+than)\s+(?:\w+\s+){0,3}?(?:in\s+)?2026\b"),
]
GROWTH_PREFIX_RE = re.compile(r"(?i)\b(?:growth|rate|pace)\b(?:\s+\w+){0,3}\s*$")
MODERATE_RE = re.compile(r"(?i)\bmoderat\w*|\bslow\w*|\bdecelerat\w*|\bless\s+(?:rapid|steep)|\bsmaller\s+(?:increase|growth)|\blower\s+(?:growth|rate)")
INCREASE_RE = re.compile(r"(?i)\b(increas\w*|grow\w*|higher|rise|rising|expand\w*|significantly\s+larger|larger)\b")


def year_ref(s: str, filed: str, ticker: str) -> bool:
    if re.search(r"(?i)\b2027\b|\bFY\s?'?27\b|\bfiscal\s+(?:year\s+)?2027\b", s):
        return True
    if ticker == "MSFT":  # fiscal year Jul-Jun: relative words are ambiguous
        return False
    if filed[:4] == "2026" and re.search(r"(?i)\bnext\s+year\b", s):
        return True
    if filed[:4] == "2027" and re.search(r"(?i)\bthis\s+year\b", s):
        return True
    return False


def classify(s: str, filed: str = "2026-11-05", ticker: str = "AMZN") -> str | None:
    """None (not about 2027 capex guidance) | cut | moderate | increase | other."""
    if not CAPEX_RE.search(s) or not year_ref(s, filed, ticker) or not FWD_RE.search(s):
        return None
    for rx in CUT_RES:
        m = rx.search(s)
        if not m:
            continue
        span = m.group(0)
        if NEG_RE.search(s[max(0, m.start() - 30):m.end()]):
            return "other"
        if GROWTH_RE.search(re.sub(r"(?i)" + _CAP, "", span)) or GROWTH_PREFIX_RE.search(s[max(0, m.start() - 50):m.start()]):
            return "moderate"
        return "cut"
    if MODERATE_RE.search(s):
        return "moderate"
    if INCREASE_RE.search(s):
        return "increase"
    return "other"


YEAR_TOK = re.compile(r"(?i)\b(20\d\d)\b|\bFY\s?'?(\d\d)\b")


def numeric_2027(s: str) -> dict | None:
    """2027 capex numeric guidance in one sentence -> {lo, hi, mid, prior_mid} (billions)."""
    if not CAPEX_RE.search(s) or not FWD_RE.search(s) or not re.search(r"(?i)billion", s):
        return None
    prior = None
    main = s
    pm = cx.PRIOR_RE.search(s)
    if pm:
        prior = cx.amount(pm.group(0))
        main = s[: pm.start()] + s[pm.end():]
    spans = sorted([m for m in cx.RANGE_RE.finditer(main)] +
                   [m for m in cx.SINGLE_RE.finditer(main) if not any(r.start() <= m.start() < r.end() for r in cx.RANGE_RE.finditer(main))],
                   key=lambda m: m.start())
    for m in spans:
        before = list(YEAR_TOK.finditer(main[: m.start()]))
        after = list(YEAR_TOK.finditer(main[m.end():]))
        tok = before[-1] if before else (after[0] if after else None)
        if tok is None or (tok.group(1) or "20" + tok.group(2)) != "2027":
            continue
        a = cx.amount(main[m.start():m.end()])
        if a:
            break
    else:
        return None
    return {"lo": a[0], "hi": a[1], "mid": a[2], "prior_mid": prior[2] if prior else None}


def judge_filing(texts: list[tuple[str, str]], filed: str, ticker: str, base: dict | None) -> dict:
    """Pure decision for one filing. base = stored 2027 numeric guidance or None."""
    out = {"cut_text": [], "moderate": [], "increase": [], "numeric": None, "numeric_cut": False, "send": False}
    for _doc, text in texts:
        for s in cx.sentences(text):
            c = classify(s, filed, ticker)
            if c == "cut":
                out["cut_text"].append(s)
            elif c == "moderate":
                out["moderate"].append(s)
            elif c == "increase":
                out["increase"].append(s)
            if out["numeric"] is None:
                n = numeric_2027(s)
                if n:
                    n["text"] = s
                    out["numeric"] = n
    n = out["numeric"]
    if n:
        prev = (base or {}).get("mid")
        if prev is not None and n["mid"] < prev - 1e-9:
            out["numeric_cut"] = True
        if n.get("prior_mid") is not None and n["mid"] < n["prior_mid"] - 1e-9:
            out["numeric_cut"] = True
    out["send"] = bool(out["cut_text"]) or out["numeric_cut"]
    return out


# ================================================================ SEC
def sec_cfg(cfg: dict) -> dict:
    c = cfg["capex"]
    return {"sec": {"user_agent": c["user_agent"], "max_docs_per_filing": c["max_docs_per_filing"],
                    "max_doc_bytes": c["max_doc_bytes"]}}


def sec_list(comp: dict, cfg: dict) -> list[dict]:
    rows = cx.sec_recent(comp["cik"], cfg["capex"]["user_agent"], cfg["capex"]["forms"])
    if not rows:
        raise RuntimeError(f"{comp['ticker']} submissions 0건")
    return rows


def relevant(f: dict, cfg: dict) -> bool:
    if f["form"].startswith("8-K"):
        items = str(f.get("items") or "")
        return any(i in items for i in cfg["capex"]["eight_k_items"])
    return True


def read_filing(comp: dict, f: dict, cfg: dict) -> list[tuple[str, str]]:
    texts = cx.filing_texts(comp["cik"], f, sec_cfg(cfg))
    if sum(len(t) for _, t in texts) < 500:
        raise RuntimeError(f"{comp['ticker']} {f['acc']} 본문 너무 짧음")
    return texts


def filing_link(comp: dict, f: dict) -> str:
    return cx.filing_url(comp["cik"], f["acc"], f.get("primary") or "")


# ================================================================ Texas Legislature Online
DC_RE = re.compile(r"(?i)\bdata\s*cent(?:er|re)s?\b|\blarge\s+(?:electrical\s+)?loads?\b")
MOR_RE = re.compile(
    r"(?i)\bmoratori\w*|\bpaus\w*|"
    r"\b(?:prohibit\w*|ban|bans|banning|suspen\w*|halt\w*|freez\w*)\b[^.;]{0,80}?\b(?:construct\w*|permit\w*|new|siting|develop\w*|interconnect\w*|locat\w*)|"
    r"\b(?:construct\w*|permit\w*|new)\b[^.;]{0,40}?\b(?:prohibit\w*|suspen\w*|halt\w*)")
LAW_RE = re.compile(r"(?i)signed by the governor|^effective\b|filed without the governor'?s signature|see remarks for effective date|became law")
VETO_RE = re.compile(r"(?i)vetoed by the governor")
COMM_RE = re.compile(r"(?i)reported favorably|reported from committee|committee report sent to calendars|comte report filed|committee report filed|reported engrossed")


def _txt(s: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s))).strip()


def _get(url: str, timeout: int = 60) -> str:
    """https first (2 tries); on a refused/reset connection one plain-http try (TX state sites refuse some cloud IPs)."""
    last: Exception | None = None
    tries = [url, url, url.replace("https://", "http://", 1)] if url.startswith("https://") else [url, url]
    for i, u in enumerate(tries):
        if i == 2 and not re.search(r"(?i)refused|reset|unreachable|timed out", str(last)):
            break
        try:
            with urllib.request.urlopen(urllib.request.Request(u, headers=UA_WEB), timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as exc:  # noqa: BLE001
            last = exc
            if i == 0:
                time.sleep(3)
    raise last  # type: ignore[misc]


def parse_report(t: str) -> list[dict]:
    if not re.search(r"Filed (House|Senate) Bills", t):
        raise RuntimeError("TLO 보고서 제목 없음")
    if re.search(r"There are no (house|senate) bills filed", t, re.I):
        return []
    m = re.search(r"Number of Bills:\s*([\d,]+)", t)
    if not m:
        raise RuntimeError("TLO 보고서 건수 없음")
    want = int(m.group(1).replace(",", ""))
    rows = []
    for chunk in t.split('<div class="row">')[1:]:
        b = re.search(r"Bill=([A-Z]+)(\d+)", chunk)
        cap = re.search(r"<b>Caption</b>:\s*</div>\s*<div[^>]*>(.*?)</div>", chunk, re.S)
        la = re.search(r"<b>Last Action:</b></div>\s*<div[^>]*>(.*?)</div>", chunk, re.S)
        if not b:
            continue
        rows.append({"bill": f"{b.group(1)} {b.group(2)}", "key": f"{b.group(1)}{b.group(2)}",
                     "caption": _txt(cap.group(1)) if cap else "", "last_action": _txt(la.group(1)) if la else ""})
    if len(rows) < want * 0.95 or (want and not any(r["caption"] for r in rows)):
        raise RuntimeError(f"TLO 보고서 행 {len(rows)} / {want}")
    return rows


def parse_history(t: str) -> dict:
    cap = re.search(r'<span id="lblCaptionText">(.*?)</span>', t, re.S)
    if not cap:
        raise RuntimeError("History lblCaptionText 없음")
    subj = re.search(r'<span id="lblSubjects">(.*?)</span>', t, re.S)
    last = re.search(r'<span id="lblLastAction">(.*?)</span>', t, re.S)
    comm = [_txt(x) for x in re.findall(r'<span id="lblComm\dCommitteeStatusValue">(.*?)</span>', t, re.S)]
    acts = [_txt(x) for x in re.findall(r'<td data-label="Action Description">(.*?)</td>', t, re.S)]
    if not acts and not last:
        raise RuntimeError("History 동작 표 없음")
    return {"caption": _txt(cap.group(1)), "subjects": _txt((subj.group(1) if subj else "").replace("<br/>", "; ")),
            "last_action": _txt(last.group(1)) if last else "", "committee": comm, "actions": acts}


def bill_status(h: dict) -> str:
    acts = h.get("actions") or []
    if any(LAW_RE.search(a) for a in acts):
        return "law"
    if any(VETO_RE.search(a) for a in acts):
        return "vetoed"
    if any(c.lower() == "out of committee" for c in h.get("committee") or []) or any(COMM_RE.search(a) for a in acts):
        return "committee"
    return "filed"


def bill_matches(caption: str, subjects: str = "") -> bool:
    blob = f"{caption} {subjects}"
    return bool(DC_RE.search(blob) and MOR_RE.search(blob))


def bill_transition(rec: dict, status: str) -> str | None:
    """Mutates rec. Returns 'committee' (p3), 'law' (p4) or None. Each once; law skips a later committee send."""
    sent = rec.setdefault("sent", [])
    rec["status"] = status
    if status == "law" and "law" not in sent:
        sent.append("law")
        if "committee" not in sent:
            sent.append("committee")
        return "law"
    if status == "committee" and "committee" not in sent and "law" not in sent:
        sent.append("committee")
        return "committee"
    return None


# ================================================================ governor
def parse_eo_category(t: str) -> list[tuple[str, str]]:
    if "There are no posts" in t:
        return []
    out = []
    for m in re.finditer(r'<a[^>]+href="(https://gov\.texas\.gov/news/post/[^"#]+)"[^>]*>(.*?)</a>', t, re.S):
        if m.group(1) not in [u for u, _ in out]:
            out.append((m.group(1), _txt(m.group(2))))
    if not out:
        raise RuntimeError("executive-order 카테고리: 글 목록도 'no posts' 도 없음")
    return out


def parse_rss(t: str) -> list[dict]:
    if "<rss" not in t:
        raise RuntimeError("RSS 아님")
    items = []
    for it in re.findall(r"<item>(.*?)</item>", t, re.S):
        g = lambda tag: re.search(rf"<{tag}>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</{tag}>", it, re.S)  # noqa: E731
        ti, li, de = g("title"), g("link"), g("description")
        items.append({"title": _txt(ti.group(1)) if ti else "", "link": (li.group(1).strip() if li else ""),
                      "text": _txt(de.group(1)) if de else ""})
    if not items:
        raise RuntimeError("RSS item 0건")
    return items


def eo_matches(text: str, need_eo_word: bool) -> bool:
    if need_eo_word and not re.search(r"(?i)executive\s+order", text):
        return False
    return bool(re.search(r"(?i)\bdata\s*cent(?:er|re)s?\b", text) and
                re.search(r"(?i)\bmoratori\w*|\bpaus\w*|\bsuspen\w*|\bhalt\w*|\bprohibit\w*", text))


# ================================================================ messages
def msg_capex(cfg: dict, comp: dict, f: dict, j: dict, base: dict | None, stamp: str) -> tuple[str, str, int]:
    tk = comp["ticker"]
    title = f"{HEAD} {tk} 2027 capex 하향 공시 | 메모리 수요 점검"
    link = filing_link(comp, f)
    why = []
    if j["numeric_cut"]:
        n = j["numeric"]
        prev = (base or {}).get("mid")
        ref = f"직전 저장값 {prev:g}" if prev is not None and n["mid"] < prev else f"문장 안 직전 전망 {n['prior_mid']:g}"
        why.append(f"2027 capex 숫자 가이던스 중간값 {n['mid']:g}십억달러 ({n['lo']:g}~{n['hi']:g}), {ref}십억달러보다 낮음.")
    if j["cut_text"]:
        why.append("2027 capex 가 2026 보다 줄어든다는 문장이 있음.")
    quote = q((j["cut_text"] or [j["numeric"]["text"] if j["numeric"] else ""])[0])
    lines = [f"{stamp} | SEMIUS | {comp['name']} 2027 capex",
             f"이게 뭐 알림: {comp['name']}({tk}) 공식 SEC 공시에서 2027년 설비투자(capex) 하향이 나왔습니다. 삼성전자(005930), SK하이닉스(000660) 메모리 수요 쪽 모니터링 신호입니다.",
             f"지금 결과/상태: {f['form']} {f['filed']} 접수번호 {f['acc']}. " + " ".join(why),
             f"원문 인용: \"{quote}\"",
             f"출처: {link}",
             "지금 할 일: 원문 문장과 실적 콜 설명을 직접 확인하고 반도체 비중 계획을 점검하세요.",
             "하지 말 것: 한 회사 공시 한 줄만으로 전량 정리, 증가율 둔화를 하향으로 읽기."]
    msg = finish(lines, cfg)
    check_text(title, msg)
    return title, msg, cfg["capex"]["priority"]


def msg_bill(cfg: dict, b: dict, kind: str, stamp: str) -> tuple[str, str, int]:
    tx = cfg["texas"]
    if kind == "law":
        title = f"{HEAD} 텍사스 데이터센터 유예 {b['bill']} 법 성립 | 수요 점검"
        what = "주지사 서명 또는 효력 발생(서명 없이 성립 포함)으로 법적 효력이 생겼습니다."
        prio = tx["p_law"]
    else:
        title = f"{HEAD} 텍사스 데이터센터 유예 {b['bill']} 위원회 통과 | 진행 확인"
        what = "위원회를 통과했습니다. 아직 법이 아닙니다(본회의, 반대 원, 주지사 단계 남음)."
        prio = tx["p_committee"]
    lines = [f"{stamp} | SEMIUS | 텍사스 {b['bill']}",
             f"이게 뭐 알림: 텍사스 90회 의회 데이터센터 모라토리엄(신규 건설, 허가 중단) 법안 {b['bill']} 이 {what} 미국 데이터센터 투자와 메모리 수요 쪽 모니터링 신호입니다.",
             f"지금 결과/상태: 마지막 동작 '{q(b.get('last_action', ''), 160)}'. 제목: \"{q(b.get('caption', ''), 260)}\"",
             f"출처: {tx['history_url'].format(sess=tx['leg_session'], bill=b['key'])}",
             "지금 할 일: 법안 원문의 대상(규모, 지역, 기간)과 효력일을 직접 확인하세요.",
             "하지 말 것: 위원회 통과를 법 성립으로 읽기."]
    msg = finish(lines, cfg)
    check_text(title, msg)
    return title, msg, prio


def msg_eo(cfg: dict, link: str, ttl: str, snippet: str, stamp: str) -> tuple[str, str, int]:
    title = f"{HEAD} 텍사스 주지사 데이터센터 유예 행정명령 | 수요 점검"
    lines = [f"{stamp} | SEMIUS | 텍사스 주지사 행정명령",
             "이게 뭐 알림: 텍사스 주지사 행정명령에 데이터센터 모라토리엄(중단) 내용이 나왔습니다. 미국 데이터센터 투자와 메모리 수요 쪽 모니터링 신호입니다.",
             f"지금 결과/상태: \"{q(ttl, 160)}\" {q(snippet, 220)}",
             f"출처: {link}",
             "지금 할 일: 행정명령 원문의 대상과 기간을 직접 확인하세요.",
             "하지 말 것: 보도 제목만 보고 범위를 넓게 읽기."]
    msg = finish(lines, cfg)
    check_text(title, msg)
    return title, msg, cfg["texas"]["governor_eo"]["priority"]


def msg_ops(cfg: dict, fails: list[str], stamp: str) -> tuple[str, str, int]:
    lines = [f"{stamp} | SEMIUS | 운영 안내",
             "이게 뭐 알림: [SEMIUS] 출처 읽기 또는 구조 해석에 실패했습니다. 투자 판단 알림이 아니며, 실패한 출처로는 판정하지 않았습니다.",
             "지금 결과/상태: " + " / ".join(q(x, 160) for x in fails[:6]),
             "출처: https://data.sec.gov/submissions/ , https://capitol.texas.gov/Reports/ , https://gov.texas.gov/news",
             "지금 할 일: 같은 실패가 며칠 이어지면 출처 페이지 구조를 확인하세요. 다음 실행에서 다시 읽습니다.",
             "하지 말 것: 읽기 실패를 조건 미충족으로 읽기."]
    msg = finish(lines, cfg)
    check_text(OPS_TITLE, msg)
    assert route_topic("cwally-port", OPS_TITLE) == "cwally-ops"
    return OPS_TITLE, msg, 3


# ================================================================ state / schedule
def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(st: dict, now: datetime) -> None:
    st["updated_at"] = now.isoformat(timespec="seconds")
    STATE_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def phase(cfg: dict, d: date) -> str | None:
    p, s = cfg["phases"], d.isoformat()
    if p["weekly_from"] <= s <= p["weekly_to"]:
        return "weekly"
    if p["daily_from"] <= s <= p["daily_to"]:
        return "daily"
    return None


def iso_week(d: date) -> str:
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def due(cfg: dict, st: dict, now: datetime) -> tuple[bool, str]:
    ph = phase(cfg, now.date())
    if ph is None:
        return False, "기간 밖"
    if ph == "weekly" and st.get("week") == iso_week(now.date()):
        return False, f"주간 구간, {st['week']} 이미 판정"
    if ph == "daily" and now.weekday() >= 5:
        return False, "주말"
    return True, ph


# ================================================================ run
class Sources:
    """Network access, swappable in selftest."""

    def __init__(self, cfg: dict):
        self.cfg = cfg

    def sec_list(self, comp):
        return sec_list(comp, self.cfg)

    def sec_read(self, comp, f):
        return read_filing(comp, f, self.cfg)

    def report(self, url):
        return parse_report(_get(url, 120))

    def history(self, key):
        tx = self.cfg["texas"]
        return parse_history(_get(tx["history_url"].format(sess=tx["leg_session"], bill=key)))

    def eo_category(self):
        return parse_eo_category(_get(self.cfg["texas"]["governor_eo"]["category_url"]))

    def eo_post(self, url):
        t = _get(url)
        i, j = t.find("<main"), t.find("</main>")
        return _txt(re.sub(r"<(script|style).*?</\1>", " ", t[i:j] if i >= 0 < j else t, flags=re.S))

    def rss(self):
        return parse_rss(_get(self.cfg["texas"]["governor_eo"]["rss_url"]))


def run(mode: str, now: datetime, cfg: dict, st: dict | None = None, src: Sources | None = None,
        pub=publish, write: bool = True) -> list[tuple[str, str, int]]:
    dry = mode == "dry"
    st = load_state() if st is None else st
    src = src or Sources(cfg)
    stamp, day = now.strftime("%Y-%m-%d %H:%M KST"), now.date().isoformat()
    out: list[tuple[str, str, int]] = []
    fails: list[str] = []
    report = {"filings": {}, "bills": [], "dc_bills": [], "eo": []}

    def guard(fn, label):
        return safe_parse(fn, state={}, day=day, ops_notify=lambda d: fails.append(f"{label}: {d}"), dry=True, label=label)

    if mode == "auto":
        ok, why = due(cfg, st, now)
        if not ok:
            print(f"semius: skip ({why})")
            return out
        print(f"semius: run ({why})")
    else:
        print(f"semius: {mode} (schedule check would say: {due(cfg, st, now)})")

    if not st.get("seeded") and not dry:
        print("semius: state not seeded; seeding silently first")
        seed(now, cfg, st, src, write=False)

    # ---------------- 1) capex
    cap = st.setdefault("capex", {})
    seen = cap.setdefault("processed", {})
    base = cap.setdefault("baseline", {})
    sent = st.setdefault("sent", [])
    log = st.setdefault("log", [])
    for comp in cfg["capex"]["companies"]:
        tk = comp["ticker"]
        rows = guard(lambda c=comp: src.sec_list(c), f"SEC {tk} submissions")
        if rows is None:
            continue
        latest = [r for r in rows if relevant(r, cfg)][:3]
        report["filings"][tk] = [f"{r['form']} {r['filed']} {r['acc']}" for r in latest]
        new = [r for r in rows if r["acc"] not in seen.get(tk, []) and r["filed"] >= cfg["capex"]["min_filed"] and relevant(r, cfg)]
        for f in sorted(new, key=lambda r: r["filed"]):
            texts = guard(lambda c=comp, ff=f: src.sec_read(c, ff), f"SEC {tk} {f['acc']}")
            if texts is None:
                continue  # not processed -> retried next run; never sends on a failure
            j = judge_filing(texts, f["filed"], tk, base.get(tk))
            prev = copy.deepcopy(base.get(tk))
            if j["numeric"]:
                n = j["numeric"]
                base[tk] = {"mid": n["mid"], "lo": n["lo"], "hi": n["hi"], "text": q(n["text"], 400),
                            "src": filing_link(comp, f), "acc": f["acc"], "filed": f["filed"]}
            for kind in ("moderate", "increase"):
                for s in j[kind][:2]:
                    log.append({"day": day, "ticker": tk, "acc": f["acc"], "kind": kind, "text": q(s, 300)})
                    print(f"capex {tk} {f['acc']} {kind} (no send): {q(s, 200)}")
            key = f"capex:{tk}:{f['acc']}"
            print(f"capex {tk} {f['form']} {f['filed']} {f['acc']}: cut_text={len(j['cut_text'])} numeric={j['numeric'] and j['numeric']['mid']} "
                  f"numeric_cut={j['numeric_cut']} send={j['send']}")
            if j["send"] and key not in sent:
                out.append(msg_capex(cfg, comp, f, j, prev, stamp))
                sent.append(key)
            seen.setdefault(tk, []).insert(0, f["acc"])
            seen[tk] = seen[tk][:80]
    st["log"] = log[-60:]

    # ---------------- 2a) Texas bills
    tx = cfg["texas"]
    bills = st.setdefault("bills", {})
    rows_all: list[dict] = []
    report_ok = True
    for url in tx["reports"]:
        r = guard(lambda u=url: src.report(u), f"TLO {url.rsplit('=', 1)[-1]}")
        if r is None:
            report_ok = False
            continue
        rows_all.extend(r)
    report["filed_total"] = len(rows_all)
    cands = [r for r in rows_all if DC_RE.search(r["caption"])]
    cands.sort(key=lambda r: (not MOR_RE.search(r["caption"]), r["key"]))
    keys = {r["key"] for r in cands}
    if report_ok:
        for k in bills:  # keep watching tracked bills even if a caption changes
            if k not in keys:
                cands.append({"key": k, "bill": bills[k].get("bill", k), "caption": bills[k].get("caption", ""), "last_action": ""})
    for r in cands[: tx["max_history_fetch"]]:
        h = guard(lambda k=r["key"]: src.history(k), f"TLO History {r['bill']}")
        if h is None:
            continue
        status = bill_status(h)
        report["dc_bills"].append(f"{r['bill']} [{status}] {h['caption'][:120]}")
        if not bill_matches(h["caption"], h["subjects"]):
            continue
        rec = bills.setdefault(r["key"], {"bill": r["bill"], "key": r["key"], "first_seen": day})
        rec.update({"caption": h["caption"], "last_action": h["last_action"]})
        kind = bill_transition(rec, status)
        report["bills"].append(f"{r['bill']} {status} -> {kind or '발송 없음'}")
        if kind:
            out.append(msg_bill(cfg, rec, kind, stamp))

    # ---------------- 2b) governor executive orders
    eo_seen = st.setdefault("eo_seen", [])
    posts = guard(src.eo_category, "gov.texas.gov executive-order")
    for link, ttl in posts or []:
        if link in eo_seen:
            continue
        body = guard(lambda u=link: src.eo_post(u), f"EO {link[-60:]}")
        if body is None:
            continue
        eo_seen.append(link)
        report["eo"].append(ttl)
        if eo_matches(ttl + " " + body, need_eo_word=False) and f"eo:{link}" not in sent:
            sent.append(f"eo:{link}")
            out.append(msg_eo(cfg, link, ttl, body[:300], stamp))
    items = guard(src.rss, "gov.texas.gov RSS")
    for it in items or []:
        if it["link"] in eo_seen:
            continue
        if eo_matches(it["title"] + " " + it["text"], need_eo_word=True) and f"eo:{it['link']}" not in sent:
            eo_seen.append(it["link"])
            sent.append(f"eo:{it['link']}")
            out.append(msg_eo(cfg, it["link"], it["title"], it["text"][:300], stamp))
    st["eo_seen"] = eo_seen[-200:]
    st["sent"] = sent[-300:]

    # ---------------- ops (one per day, never a p4)
    if fails:
        print("FAILURES:\n  " + "\n  ".join(fails))
        if st.get("ops_parse_day") != day:
            out.append(msg_ops(cfg, fails, stamp))
            if not dry:
                st["ops_parse_day"] = day
        else:
            print("ops notice already sent today")
    if mode == "auto" and phase(cfg, now.date()) == "weekly":
        # week closes even on a failure: unprocessed filings are retried next week, and the ops notice stays weekly
        # in the weekly phase (GitHub-hosted runners got 'connection refused' from capitol.texas.gov / gov.texas.gov on 2026-11-05)
        st["week"] = iso_week(now.date())
    if mode == "auto" and phase(cfg, now.date()) == "daily":
        st["last_daily"] = day

    print("---- summary")
    for tk, fl in report["filings"].items():
        print(f"latest {tk}: {fl}  baseline2027_mid={(base.get(tk) or {}).get('mid')}")
    print(f"TLO 90R filed bills: {report.get('filed_total')} (data center captions {len(cands)}): {report['dc_bills'] or '없음'}")
    print(f"moratorium bills: {report['bills'] or '없음'}")
    print(f"EO category posts newly checked: {report['eo'] or '없음'}; RSS items read: {len(items or [])}")
    topic, server = resolve_topic("port", cfg.get("ntfy_topic")), resolve_server(cfg.get("ntfy_server"))
    for t, m, p in out:
        print(f"TITLE: {t}\nPRIORITY: {p} TOPIC: {route_topic(topic, t)}\n{m}\n")
    if not out:
        print("보낼 것 없음")
    if dry:
        print("DRY RUN: no publish, state not written")
        return out
    for t, m, p in out:
        pub(resolve_topic("ops") if t.startswith("[OPS]") else topic, t, m, p,
            ["warning"] if t.startswith("[OPS]") else ["chart_with_downwards_trend"], server, config_name=NAME)
    if write:
        save_state(st, now)
    return out


def seed(now: datetime, cfg: dict, st: dict, src: Sources | None = None, write: bool = True) -> dict:
    """Mark every current filing accession processed; keep (or create) the 2027 baseline. No sends."""
    src = src or Sources(cfg)
    cap = st.setdefault("capex", {})
    seen = cap.setdefault("processed", {})
    cap.setdefault("baseline", {})
    for comp in cfg["capex"]["companies"]:
        rows = src.sec_list(comp)
        seen[comp["ticker"]] = [r["acc"] for r in rows][:80]
        cap["baseline"].setdefault(comp["ticker"], None)
    st["seeded"] = now.isoformat(timespec="seconds")
    if write:
        save_state(st, now)
    return st


# ================================================================ selftest
def selftest() -> int:
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    ok = fail = 0

    def check(label, got, want):
        nonlocal ok, fail
        if got == want:
            ok += 1
        else:
            fail += 1
            print(f"FAIL {label}: got {got!r} want {want!r}")

    # --- classifier: cut / not cut
    C = classify
    check("cut decrease", C("We expect 2027 capital expenditures to decrease compared to 2026."), "cut")
    check("cut lower than 2026", C("We anticipate capital expenditures in 2027 will be lower than 2026 levels."), "cut")
    check("cut reduce", C("We plan to reduce our capital expenditures in 2027 as data center build-outs complete."), "cut")
    check("cut modestly lower", C("We expect cash capital expenditures to be modestly lower in 2027."), "cut")
    check("cut decline", C("We expect capex to decline in 2027."), "cut")
    check("cut next year (2026 filing)", C("We expect capital expenditures to be down next year.", "2026-11-05", "META"), "cut")
    check("cut fiscal 2027 MSFT", C("We expect fiscal year 2027 capital expenditures to decline.", "2026-10-28", "MSFT"), "cut")
    check("moderate FP", C("We expect capital expenditures growth to moderate in 2027."), "moderate")
    check("moderate slower", C("We expect capital expenditures will continue to grow in 2027, but at a slower pace."), "moderate")
    check("growth rate lower", C("We expect the growth rate of capital expenditures in FY27 to be lower than FY26.", "2026-10-28", "MSFT"), "moderate")
    check("capex growth will decline", C("We expect capex growth will decline in 2027."), "moderate")
    check("increase AMZN Q3 10-Q", C("We expect cash capital expenditures to meaningfully increase in 2027, primarily driven by investments in technology infrastructure."), "increase")
    check("significantly larger", C("We expect capital expenditures dollar growth to be significantly larger in 2027."), "increase")
    check("negated", C("We do not expect capital expenditures to decline in 2027."), "other")
    check("no 2027", C("Capital expenditures decreased in the third quarter compared to the prior year."), None)
    check("past tense no fwd", C("Capital expenditures in 2027 decreased."), None)
    check("MSFT next year ignored", C("We expect capital expenditures to be lower next year.", "2026-10-28", "MSFT"), None)
    check("this year in 2027 filing", C("We expect capital expenditures this year to be lower than 2026.", "2027-02-05", "AMZN"), "cut")
    check("this year in 2026 filing", C("We expect capital expenditures this year to be lower.", "2026-11-05", "AMZN"), None)
    check("2026 only", C("We continue to expect 2026 cash capital expenditures to be approximately $220 billion."), None)

    # --- numeric extraction + comparison
    n = numeric_2027("We anticipate 2027 capital expenditures to be in the range of $150-170 billion.")
    check("numeric range", (n["lo"], n["hi"], n["mid"]), (150.0, 170.0, 160.0))
    check("numeric 2026 ignored", numeric_2027("We continue to expect 2026 cash capital expenditures to be approximately $220 billion."), None)
    check("numeric mixed years", numeric_2027("After $220 billion in 2026, we expect 2027 capital expenditures of approximately $200 billion.")["mid"], 200.0)
    n2 = numeric_2027("We anticipate 2027 capital expenditures to be in the range of $140-150 billion, lowered from our prior outlook of $150-170 billion.")
    check("numeric prior", (n2["mid"], n2["prior_mid"]), (145.0, 160.0))
    T = lambda s: [("ex99.htm", s)]  # noqa: E731
    j = judge_filing(T("We anticipate 2027 capital expenditures to be in the range of $150-170 billion."), "2027-01-29", "META", None)
    check("numeric first, null base: no send", (j["send"], j["numeric"]["mid"]), (False, 160.0))
    j = judge_filing(T("We anticipate 2027 capital expenditures to be in the range of $140-150 billion."), "2027-04-29", "META", {"mid": 160.0})
    check("numeric lower than base: send", (j["numeric_cut"], j["send"]), (True, True))
    j = judge_filing(T("We anticipate 2027 capital expenditures to be in the range of $160-180 billion."), "2027-04-29", "META", {"mid": 160.0})
    check("numeric higher: no send", j["send"], False)
    j = judge_filing(T("We anticipate 2027 capital expenditures to be in the range of $150-170 billion."), "2027-04-29", "META", {"mid": 160.0})
    check("numeric equal: no send", j["send"], False)
    j = judge_filing(T("We anticipate 2027 capital expenditures to be in the range of $140-150 billion, lowered from our prior outlook of $150-170 billion."), "2027-04-29", "META", None)
    check("numeric prior outlook, null base: send", j["send"], True)
    j = judge_filing(T("We expect capital expenditures growth to moderate in 2027."), "2027-01-29", "META", None)
    check("moderate: no send, logged", (j["send"], len(j["moderate"])), (False, 1))

    # --- Texas bill status transitions
    H = lambda acts, comm=None: {"actions": acts, "committee": comm or []}  # noqa: E731
    check("status filed", bill_status(H(["Filed"])), "filed")
    check("status referred", bill_status(H(["Referred to State Affairs", "Read first time"])), "filed")
    check("status committee", bill_status(H(["Reported favorably as substituted", "Referred to State Affairs"])), "committee")
    check("status out of committee", bill_status(H(["Considered in public hearing"], ["Out of committee"])), "committee")
    check("status law signed", bill_status(H(["Signed by the Governor", "Sent to the Governor"])), "law")
    check("status law effective", bill_status(H(["Effective on 9/1/27", "Signed by the Governor"])), "law")
    check("status without signature", bill_status(H(["Filed without the Governor's signature"])), "law")
    check("status vetoed", bill_status(H(["Vetoed by the Governor", "Sent to the Governor"], ["Out of committee"])), "vetoed")
    rec = {}
    check("transition filed", bill_transition(rec, "filed"), None)
    check("transition committee", bill_transition(rec, "committee"), "committee")
    check("transition committee dedup", bill_transition(rec, "committee"), None)
    check("transition law", bill_transition(rec, "law"), "law")
    check("transition law dedup", bill_transition(rec, "law"), None)
    rec2 = {}
    check("jump to law", bill_transition(rec2, "law"), "law")
    check("no committee after law", bill_transition(rec2, "committee"), None)
    check("match moratorium", bill_matches("Relating to a moratorium on the construction of new data centers."), True)
    check("match prohibit permits", bill_matches("Relating to prohibiting the issuance of permits for new data center facilities."), True)
    check("match large load pause", bill_matches("Relating to a temporary pause on interconnection of large loads."), True)
    check("no match tax", bill_matches("Relating to an exemption from ad valorem taxation of real property used as a data center."), False)
    check("no match moratorium other", bill_matches("Relating to a moratorium on new groundwater permits."), False)

    # --- TLO / governor parsers (real 2026-11-05 shapes)
    check("report empty 90R", parse_report("<h2>Filed House Bills</h2> There are no house bills filed for this legislative session."), [])
    rep = ('<h2>Filed Senate Bills</h2>Number of Bills: 1<br><div class="bill-search-results"><div class="row">'
           '<div class="bill-search-result-label"><a href="http://capitol.texas.gov/BillLookup/History.aspx?LegSess=90R&amp;Bill=SB12">SB 12 </a></div>'
           '<div class="bill-search-result-label"><b>Last Action:</b></div>\n<div class="bill-search-result-data">11/09/2026 S Filed</div>'
           '<div class="bill-search-result-label"><b>Caption</b>:\n\t\t</div>\n<div class="bill-search-result-data">Relating to a moratorium on new data centers.</div></div>')
    check("report row", parse_report(rep)[0]["bill"], "SB 12")
    for bad in ("<html>maintenance</html>", "<h2>Filed House Bills</h2>Number of Bills: 50"):
        try:
            parse_report(bad)
            check(f"report bad raises {bad[:20]}", False, True)
        except RuntimeError:
            check(f"report bad raises {bad[:20]}", True, True)
    hist = ('<span id="lblCaptionText">Relating to a moratorium on new data centers.</span><span id="lblSubjects">Utilities--Electric (I0831)<br/></span>'
            '<span id="lblLastAction">03/01/2027 S Reported favorably</span><span id="lblComm1CommitteeStatusValue">Out of committee</span>'
            '<td data-label="Action Description">&nbsp;Reported favorably&nbsp;</td>')
    h = parse_history(hist)
    check("history parse", (bill_status(h), bill_matches(h["caption"], h["subjects"])), ("committee", True))
    check("eo no posts", parse_eo_category("<main>News There are no posts.</main>"), [])
    check("eo post link", parse_eo_category('<a href="https://gov.texas.gov/news/post/x">Executive Order GA-60</a>')[0][0], "https://gov.texas.gov/news/post/x")
    check("eo match", eo_matches("Governor Abbott issues Executive Order GA-60 directing a pause on new data center permits", True), True)
    check("eo no eo word", eo_matches("Governor Abbott celebrates a new data center", True), False)

    # --- run(): fake sources; dedup; ops once a day; no p4 on failure
    sent: list = []

    def fake_pub(topic, title, msg, prio, tags, server, config_name=None):
        sent.append((route_topic(topic, title), title, prio))

    class Fake(Sources):
        def __init__(self, cfg, filings=None, texts=None, report_rows=None, hist=None, fail=()):
            super().__init__(cfg)
            self.filings, self.texts, self.rows, self.hist, self.fail = filings or {}, texts or {}, report_rows or [], hist or {}, set(fail)

        def sec_list(self, comp):
            if "sec" in self.fail:
                raise RuntimeError("403")
            return self.filings.get(comp["ticker"], [{"acc": "old-1", "form": "10-Q", "filed": "2026-10-30", "primary": "x.htm", "items": ""}])

        def sec_read(self, comp, f):
            if "doc" in self.fail:
                raise RuntimeError("timeout")
            return self.texts[f["acc"]]

        def report(self, url):
            if "tlo" in self.fail:
                raise RuntimeError("layout")
            return self.rows if "house" in url else []

        def history(self, key):
            return self.hist[key]

        def eo_category(self):
            return []

        def rss(self):
            return [{"title": "x", "link": "https://gov.texas.gov/news/post/y", "text": "proclamation"}]

    now = datetime(2027, 1, 29, 9, 0, tzinfo=KST)
    st = {"seeded": "t", "capex": {"processed": {"META": ["old-1"]}, "baseline": {"META": None}}}
    f_new = {"acc": "0001628280-27-000001", "form": "8-K", "filed": "2027-01-28", "primary": "m.htm", "items": "2.02,9.01"}
    fk = Fake(cfg, filings={"META": [f_new]}, texts={f_new["acc"]: T("We expect capital expenditures in 2027 to decrease compared to 2026.")})
    o = run("auto", now, cfg, st, fk, fake_pub, write=False)
    check("capex cut p4", [(t.split(" | ")[0], p) for t, _, p in o], [(f"{HEAD} META 2027 capex 하향 공시", 4)])
    check("routed port", sent[-1][0], "cwally-port")
    o = run("auto", now + timedelta(days=3), cfg, st, fk, fake_pub, write=False)
    check("accession dedup", o, [])
    # moderate filing -> no send, logged
    f_mod = dict(f_new, acc="0001628280-27-000002")
    st2 = {"seeded": "t", "capex": {"processed": {}, "baseline": {}}}
    o = run("auto", now, cfg, st2, Fake(cfg, filings={"META": [f_mod]}, texts={f_mod["acc"]: T("We expect capital expenditures growth to moderate in 2027.")}), fake_pub, write=False)
    check("moderate no send", o, [])
    check("moderate logged", [x["kind"] for x in st2["log"]], ["moderate"])
    # doc read failure -> ops once a day, not processed, no p4
    st3 = {"seeded": "t", "capex": {"processed": {}, "baseline": {}}}
    fk3 = Fake(cfg, filings={"META": [f_new]}, texts={}, fail={"doc"})
    o = run("auto", now, cfg, st3, fk3, fake_pub, write=False)
    check("doc fail -> ops only", [(route_topic("cwally-port", t), p) for t, _, p in o], [("cwally-ops", 3)])
    check("doc fail not processed", f_new["acc"] in st3["capex"]["processed"].get("META", []), False)
    o = run("auto", now + timedelta(hours=2), cfg, st3, fk3, fake_pub, write=False)
    check("ops once a day", o, [])
    fk3.fail = set()
    fk3.texts = {f_new["acc"]: T("We expect capital expenditures in 2027 to decrease compared to 2026.")}
    o = run("auto", now + timedelta(days=3), cfg, st3, fk3, fake_pub, write=False)
    check("retry after failure sends once", [p for _, _, p in o], [4])
    # TLO structure failure -> ops, never p4
    st4 = {"seeded": "t", "capex": {"processed": {}, "baseline": {}}}
    o = run("auto", now, cfg, st4, Fake(cfg, fail={"tlo"}), fake_pub, write=False)
    check("tlo fail ops", [t for t, _, _ in o], [OPS_TITLE])
    # bills: committee p3 then law p4, each once; filing alone nothing
    rows = [{"bill": "HB 7", "key": "HB7", "caption": "Relating to a moratorium on new data centers.", "last_action": ""}]
    hs = {"HB7": {"caption": rows[0]["caption"], "subjects": "", "last_action": "Filed", "committee": [], "actions": ["Filed"]}}
    st5 = {"seeded": "t", "capex": {"processed": {}, "baseline": {}}}
    fk5 = Fake(cfg, report_rows=rows, hist=hs)
    check("bill filed silent", run("auto", now, cfg, st5, fk5, fake_pub, write=False), [])
    hs["HB7"]["actions"] = ["Reported favorably w/o amendment(s)", "Filed"]
    check("bill committee p3", [p for _, _, p in run("auto", now, cfg, st5, fk5, fake_pub, write=False)], [3])
    check("bill committee dedup", run("auto", now, cfg, st5, fk5, fake_pub, write=False), [])
    hs["HB7"]["actions"] = ["Effective on 9/1/27", "Signed by the Governor"]
    o = run("auto", now, cfg, st5, fk5, fake_pub, write=False)
    check("bill law p4", [(p, "법 성립" in t) for t, _, p in o], [(4, True)])
    check("bill law dedup", run("auto", now, cfg, st5, fk5, fake_pub, write=False), [])
    # schedule gating
    stw = {"seeded": "t", "week": "2026-W46"}
    check("weekly first run", due(cfg, {}, datetime(2026, 11, 9, 9, 0, tzinfo=KST)), (True, "weekly"))
    check("weekly second run skip", due(cfg, stw, datetime(2026, 11, 10, 9, 0, tzinfo=KST))[0], False)
    check("daily phase", due(cfg, stw, datetime(2027, 1, 12, 9, 0, tzinfo=KST)), (True, "daily"))
    check("after 06-02 off", due(cfg, {}, datetime(2027, 6, 3, 9, 0, tzinfo=KST))[0], False)
    check("ends_kst", cfg["ends_kst"], "2027-06-05 23:59")
    stf = {"seeded": "t", "capex": {"processed": {}, "baseline": {}}}
    o = run("auto", datetime(2026, 11, 9, 9, 0, tzinfo=KST), cfg, stf, Fake(cfg, fail={"tlo"}), fake_pub, write=False)
    check("weekly fail: ops once, week closed", ([t for t, _, _ in o], stf.get("week")), ([OPS_TITLE], "2026-W46"))
    check("weekly fail: no rerun same week", run("auto", datetime(2026, 11, 10, 9, 0, tzinfo=KST), cfg, stf, Fake(cfg, fail={"tlo"}), fake_pub, write=False), [])
    # dry: no publish, no state change
    n_before = len(sent)
    st6 = {"seeded": "t", "capex": {"processed": {}, "baseline": {}}}
    snap = json.dumps(st6, sort_keys=True)
    o = run("dry", now, cfg, st6, Fake(cfg, filings={"META": [f_new]}, texts={f_new["acc"]: T("We expect capital expenditures in 2027 to decrease compared to 2026.")}), fake_pub, write=False)
    check("dry builds msg", len(o), 1)
    check("dry no publish", len(sent), n_before)
    check("dry no ops day", "ops_parse_day" in st6, False)
    del snap

    # --- title rules on every title
    comp = cfg["capex"]["companies"][3]
    jj = judge_filing(T("We anticipate 2027 capital expenditures to be in the range of $140-150 billion, lowered from our prior outlook of $150-170 billion \u2014 a cut."), "2027-04-29", "META", None)
    titles = [msg_capex(cfg, comp, f_new, jj, None, "t"),
              msg_bill(cfg, {"bill": "HB 7", "key": "HB7", "caption": "Relating to a moratorium \u00b7 data centers", "last_action": "x"}, "committee", "t"),
              msg_bill(cfg, {"bill": "HB 7", "key": "HB7", "caption": "c", "last_action": "x"}, "law", "t"),
              msg_eo(cfg, "https://gov.texas.gov/news/post/x", "EO", "data center pause", "t"),
              msg_ops(cfg, ["SEC META: HTTPError 403"], "t")]
    for t, m, _ in titles:
        good = "005930" in t and "000660" in t and "매수" not in t and all(b not in t + m for b in BANNED) and "출처:" in m and "모니터링 신호" in m + cfg["footer"]
        check(f"title rules {t}", good, True)
    check("ops title routes ops", route_topic("cwally-port", OPS_TITLE), "cwally-ops")
    check("ops title tickers", "[005930 000660]" in OPS_TITLE, True)
    print(f"selftest: {ok} passed, {fail} failed")
    return 0 if fail == 0 else 1


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    if mode == "selftest":
        return selftest()
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    if mode == "seed":
        st = load_state()
        seed(now, cfg, st)
        print(json.dumps(st.get("capex", {}).get("baseline"), ensure_ascii=False, indent=1))
        return 0
    if mode not in {"auto", "dry"}:
        print("usage: semius_monitor.py <auto|dry|selftest|seed>")
        return 2
    run(mode, now, cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
