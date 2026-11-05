#!/usr/bin/env python3
"""[FIG-PX] Figma (FIG, NYSE) close-based zones (approved in chat 2026-10-26; zones retuned 2026-11-05 after Q3 2026: bear 16.60->17.10, base 28.40->29.10, buy 22.00 kept).

  - close <= 22.00 -> '매수 검토 구간 진입'      (priority 4)
  - close <= 17.10 -> '약세 시나리오 가치 도달'  (priority 5)
  - close >= 29.10 -> '기본 시나리오 가치 도달'  (priority 3)

Each zone fires once when a completed close ENTERS it. It re-arms (quietly) only after a close
leaves the zone (le: close above the line, ge: close below the line). No buffer.
The first run (no state) only records the current close, it never sends.
Dedupe by the bar's own US/Eastern date (state last_date). Same pattern as pcorpx_monitor.py.

Runs on the existing morning 09:00 KST slot (after the US close). No new cron.
Modes: auto | force (ignore date dedupe, still respects arm state) | dry (print only, no ntfy, no state)
       | confirm (install-confirmation send, no state) | selftest.
Not investment advice.
"""
from __future__ import annotations

import html
import json
import re
import sys
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
NY = ZoneInfo("America/New_York")
CFG = ROOT / "configs" / "figpx.json"
STATE = ROOT / "state" / "figpx.json"
UA = {"User-Agent": "Mozilla/5.0 cwally-alerts"}


def load_cfg() -> dict:
    return json.loads(CFG.read_text(encoding="utf-8"))


def load_state() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {}


def save_state(st: dict, dry: bool) -> None:
    if dry:
        return
    st["updated_at"] = datetime.now(KST).isoformat(timespec="seconds")
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def fetch_bars(cfg: dict, now_ny: datetime) -> list[tuple[date, float]]:
    """Completed daily bars (US/Eastern date, close), ascending. Today's live bar is excluded."""
    req = urllib.request.Request(cfg["sources"]["bars"], headers=UA)
    with urllib.request.urlopen(req, timeout=20) as resp:
        res = json.loads(resp.read().decode())["chart"]["result"][0]
    out: dict[date, float] = {}
    for t, c in zip(res.get("timestamp") or [], res["indicators"]["quote"][0].get("close") or []):
        if c is not None:
            out[datetime.fromtimestamp(t, NY).date()] = float(c)
    meta = res.get("meta") or {}
    rmt, rmp = meta.get("regularMarketTime"), meta.get("regularMarketPrice")
    if rmt and rmp:
        mt = datetime.fromtimestamp(rmt, NY)
        if mt.date() not in out and mt.hour * 60 + mt.minute >= 16 * 60:
            out[mt.date()] = float(rmp)
    today = now_ny.date()
    after_close = now_ny.hour * 60 + now_ny.minute >= 16 * 60 + 10
    return sorted((d, c) for d, c in out.items() if d < today or (d == today and after_close))


# ---------- core (pure) ----------

def hit(level: dict, close: float) -> bool:
    p = float(level["price"])
    if level["op"] == "le":
        return close <= p
    if level["op"] == "ge":
        return close >= p
    return close < p  # "lt"


def step(levels: list[dict], armed: dict | None, close: float) -> tuple[dict, list[str]]:
    """One completed close. armed[key] True = watching (outside the zone). Returns (new armed, fired keys).
    armed None = first run: init from this close, nothing fires."""
    close = round(float(close), 4)
    if armed is None:
        return {lv["key"]: not hit(lv, close) for lv in levels}, []
    new, fired = dict(armed), []
    for lv in levels:
        k = lv["key"]
        inside = hit(lv, close)
        if new.get(k, True):
            if inside:
                new[k] = False
                fired.append(k)
        elif not inside:
            new[k] = True  # left the zone: quiet re-arm
    return new, fired


def lines_text(cfg: dict) -> str:
    return "매수 검토 22달러 이하, 약세 시나리오 17.1달러 이하, 기본 시나리오 가치 29.1달러 이상"


STOP_TODO = ("추가매수 보류, NDR(순달러유지율), 매출총이익률, SBC(주식보상비용)부터 재확인해 투자 논리 점검. "
             "약세 시나리오 값($17.1)까지 내려왔습니다. 오늘 일괄 매수 금지.")
BUY_WORDS = ("분할매수", "나눠서", "추가 매수 전에", "분할매수 검토")


def message(cfg: dict, fired: list[str], close: float, day: str, prev_close: float | None) -> tuple[str, str, int]:
    lv = {x["key"]: x for x in cfg["levels"]}
    top = max((lv[k] for k in fired), key=lambda x: x["priority"])
    labels = " + ".join(lv[k]["label"] for k in fired)
    if "bear171" in fired:
        act = "매수 논리 재점검"
        todo = STOP_TODO
    elif "buy22" in fired:
        act = "분할매수 검토"
        todo = "$20~22 분할매수 검토 구간 (사업 전망 유지 전제). 실적 악화로 인한 하락이면 기준 재계산 필요. 계획 금액을 나눠서만 검토."
    else:
        act = "비중 점검, 추격 금지"
        todo = "기본 시나리오 12개월 가치 $29.1에 도달했습니다. 추가 매수보다 비중과 목표 재점검."
    title = f"[FIG-PX] 종가 {close:.2f}달러 {top['label']} | {act}"
    chg = f", 전 거래일 대비 {(close / prev_close - 1) * 100:+.1f}%" if prev_close else ""
    notes = " ".join(lv[k]["note"] for k in fired if not ("bear171" in fired and k == "buy22"))  # no buy note next to the bear line
    lines = [
        f"{datetime.now(KST):%Y-%m-%d %H:%M} KST | FIG | 피그마",
        "이게 뭐 알림: " + cfg["explain"],
        f"지금 결과/상태: {labels}. 미국 종가 {close:.2f}달러({day} 미 동부{chg}). {notes}",
        "지금 할 일: " + todo,
        f"나중에: 같은 구간은 종가가 구간을 벗어난 뒤 다시 들어올 때만 또 울립니다. 선: {lines_text(cfg)}.",
        "익절/줄일 때: 29.1달러 이상은 기본 시나리오 가치 도달이라 추가 매수보다 일부 정리와 비중 점검이 먼저입니다.",
        "하지 말 것: 장중 급락 추격 물타기, 계획 금액 초과, 신용/미수, 이 알림만 보고 주문. 투자 권유 아님.",
    ]
    return title, "\n".join(lines), int(top["priority"])


def confirm_message(cfg: dict, close: float | None, day: str | None) -> tuple[str, str]:
    px = f"{close:.2f}달러({day} 미 동부 종가)" if close is not None else "조회 실패(구간 미확인)"
    title = "[FIG-PX] 설치 확인 | 매매 신호 아님"
    lines = [
        "[테스트] FIG 알림 설정 완료",
        "이게 뭐 알림: " + cfg["explain"],
        f"지금 결과/상태: FIG 현재 종가 {px}. 선 3개: 22달러 이하 매수 검토(우선순위 4), 17.1달러 이하 약세 시나리오(5), 29.1달러 이상 기본 시나리오 가치(3).",
        "지금 할 일: 없음. 평일 09:00 KST morning 에서 전일 미국 종가로 판정하고, 구간에 새로 들어갈 때만 보냅니다. 실적 전날(11/4 09:00)과 체크리스트(11/5 08:00)는 [FIG-EARN] 으로 옵니다.",
        "나중에: 같은 구간은 종가가 구간을 벗어난 뒤 다시 들어올 때만 또 울립니다. "
        f"{cfg['sec']['start_date_kst'][5:].replace('-', '/')}부터 SEC 공시 감시 추가: 중요 8-K(재작성, 감사인 변경, 상장 통지, CEO/CFO/CTO 변동 등)와 S-1/S-3/424B, 내부자 장내 매수는 우선순위 4, "
        f"8-K 8.01, 13D, 신고 1건 {cfg['sec']['insider_sale_usd'] / 1e6:,.0f}백만 달러 초과 내부자 매도와 CEO의 10b5-1 계획 밖 매도는 3, 10-K 는 2. Form 144, S-8, 일반 Form 4 는 보내지 않습니다.",
        "익절/줄일 때: 29.1달러 이상 알림이 오면 비중 점검.",
        "하지 말 것: 이 설치 확인을 매수/매도 신호로 읽기.",
    ]
    return title, "\n".join(lines)


# ---------- SEC filing whitelist (approved in chat 2026-10-29, from 2026-11-06) ----------
# p4: 8-K items 1.01 1.02 2.01 2.05 2.06 3.01 4.01 4.02 5.01, 8-K 5.02 about the CEO/CFO/CTO
#     (5.02 that cannot be tied to them -> p3), S-1 / S-3 / 424B*, Form 4 open-market purchase (P)
# p3: 8-K 8.01 or any other non-ignored item, Schedule 13D(/A), Form 4 open-market sales (S) over
#     sec.insider_sale_usd per filing, CEO sale (S) not under a 10b5-1 plan
# p2: 10-K (short notice)
# ignored: 144, other Form 4, S-8, 13G, proxy, 10-Q, 8-K with only 5.07 / 9.01 / 2.02 / 7.01
# Dedupe: state sec_seen (accession numbers). First run seeds every listed accession silently.
# Before sec.start_date_kst new accessions are recorded silently (no fetch, no send).

ITEM_KO = {
    "1.01": "중요 계약 체결", "1.02": "중요 계약 종료", "2.01": "인수 또는 자산 처분 완료",
    "2.05": "구조조정 비용", "2.06": "중대한 자산 손상", "3.01": "상장 유지 기준 미달 또는 상장폐지 통지",
    "4.01": "회계감사인 변경", "4.02": "과거 재무제표 신뢰 불가(재작성)", "5.01": "회사 지배권 변경",
    "5.02": "임원 또는 이사 변동", "8.01": "기타 중요 사항",
}
WARN_ITEMS = {"1.02", "2.05", "2.06", "3.01", "4.01", "4.02", "5.01", "5.02"}
OFFICER_RE = re.compile(r"Chief\s+(Executive|Financial|Technology)\s+Officer|\b(CEO|CFO|CTO)\b")
SEC_BAD_WORDS = ("분할매수", "매수 검토", "매수 기회", "매수 구간", "사세요", "추가 매수 전에", "나눠서")


def sec_get(cfg: dict, url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": cfg["sec"]["user_agent"], "Accept-Encoding": "identity"})
    with urllib.request.urlopen(req, timeout=25) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def sec_rows(subs: dict) -> list[dict]:
    r = (subs.get("filings") or {}).get("recent") or {}
    keys = ("form", "filingDate", "accessionNumber", "items", "primaryDocument")
    cols = [r.get(k) or [] for k in keys]
    n = min(len(c) for c in cols) if cols else 0
    return [{"form": cols[0][i], "date": cols[1][i], "acc": cols[2][i], "items": cols[3][i] or "",
             "doc": cols[4][i] or ""} for i in range(n)]


def cik_int(cfg: dict) -> int:
    return int(cfg["sec"]["cik"])


def filing_dir(cfg: dict, f: dict) -> str:
    return f"https://www.sec.gov/Archives/edgar/data/{cik_int(cfg)}/{f['acc'].replace('-', '')}"


def index_url(cfg: dict, f: dict) -> str:
    return f"{filing_dir(cfg, f)}/{f['acc']}-index.htm"


def parse_items(raw: str) -> list[str]:
    return [x.strip() for x in str(raw or "").split(",") if x.strip()]


def item_502_text(doc_html: str) -> str:
    t = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", doc_html)))
    i = t.find("Item 5.02")
    if i < 0:
        return ""
    seg = t[i + len("Item 5.02"):]
    ends = [j for j in (seg.find("Item "), seg.find("SIGNATURE")) if j > 0]
    return seg[:min(ends)] if ends else seg


def parse_form4(xml: str) -> dict:
    """Owner, CEO flag, open-market purchases (P) and sales (S) with 10b5-1 plan flags per transaction."""
    def tag(name: str, src: str) -> str:
        m = re.search(rf"<{name}>\s*(?:<value>)?\s*([^<]*)", src)
        return html.unescape(m.group(1).strip()) if m else ""
    owner = tag("rptOwnerName", xml)
    title = tag("officerTitle", xml)
    notes = {fid: txt for fid, txt in re.findall(r'<footnote id="(F\d+)">(.*?)</footnote>', xml, re.S)}
    any_fn_plan = any("10b5-1" in t for t in notes.values())
    box = tag("aff10b5One", xml).lower() in {"1", "true"}
    buys, sells = [], []
    for t in re.findall(r"<nonDerivativeTransaction>(.*?)</nonDerivativeTransaction>", xml, re.S):
        code = tag("transactionCode", t)
        try:
            sh = float(tag("transactionShares", t) or 0)
            px = float(tag("transactionPricePerShare", t) or 0)
        except ValueError:
            sh, px = 0.0, 0.0
        refs = re.findall(r'<footnoteId id="(F\d+)"', t)
        plan = any("10b5-1" in notes.get(r, "") for r in refs) or (box and not any_fn_plan)
        row = {"shares": sh, "price": px, "value": sh * px, "plan": plan}
        if code == "P":
            buys.append(row)
        elif code == "S":
            sells.append(row)
    is_ceo = bool(re.search(r"Chief\s+Executive|\bCEO\b", title))
    return {"owner": owner, "title": title, "is_ceo": is_ceo, "buys": buys, "sells": sells}


def usd(v: float) -> str:
    return f"{v / 1e6:,.1f}백만 달러"


def classify(cfg: dict, f: dict, fetch) -> dict | None:
    """One filing -> hit dict {prio, kind, warn, label, detail} or None (ignored).
    fetch(url) -> text; may raise (5.02 -> p3, Form 4 -> re-raise so the accession is retried)."""
    sec = cfg["sec"]
    form = f["form"].upper().strip()
    base = {"form": f["form"], "date": f["date"], "acc": f["acc"], "items": f["items"], "url": index_url(cfg, f)}
    if form in {"8-K", "8-K/A"}:
        items = parse_items(f["items"])
        rest = [i for i in items if i not in set(sec["ignore_8k_items"])]
        if not rest:
            return None
        p4 = [i for i in rest if i in set(sec["p4_8k_items"])]
        prio, warn, extra = 3, False, ""
        if p4:
            prio = 4
        if "5.02" in rest:
            try:
                officer = bool(OFFICER_RE.search(item_502_text(fetch(f"{filing_dir(cfg, f)}/{f['doc']}"))))
                extra = " CEO/CFO/CTO 관련 내용 있음." if officer else " CEO/CFO/CTO 언급 없음(이사 또는 다른 임원 변동)."
            except Exception as exc:  # noqa: BLE001
                officer = False
                extra = f" 원문 조회 실패({exc.__class__.__name__}), 임원 범위 미확인이라 우선순위 3."
            if officer:
                prio = 4
        warn = any(i in WARN_ITEMS for i in rest)
        names = ", ".join(f"{i} {ITEM_KO.get(i, '기타 항목')}" for i in rest)
        return {**base, "prio": prio, "kind": "8k", "warn": warn,
                "label": f"8-K {names.split(', ')[0]}" + (f" 외 {len(rest) - 1}건" if len(rest) > 1 else ""),
                "detail": f"8-K 항목 {names}.{extra}"}
    if any(form.startswith(p) for p in sec["p4_forms_prefix"]):
        return {**base, "prio": 4, "kind": "offer", "warn": True, "label": f"{f['form']} 증권 발행 서류",
                "detail": f"{f['form']} 증권 신고 또는 발행 설명서. 신주 발행(희석)이나 대주주 매출일 수 있음."}
    if form in {x.upper() for x in sec["p3_forms"]}:
        return {**base, "prio": 3, "kind": "13d", "warn": True, "label": f"{f['form']} 대량 보유 신고",
                "detail": f"{f['form']} 5% 이상 보유자의 적극적 보유 목적 신고(지분 변동, 경영 참여 의사)."}
    if form in {x.upper() for x in sec["p2_forms"]}:
        return {**base, "prio": 2, "kind": "10k", "warn": False, "label": f"{f['form']} 연차보고서 제출", "detail": f"{f['form']} 연차보고서."}
    if form in {"4", "4/A"}:
        doc = f["doc"].split("/")[-1]
        info = parse_form4(fetch(f"{filing_dir(cfg, f)}/{doc}"))  # errors propagate: retry next run
        who = info["owner"] + (f"({info['title']})" if info["title"] else "")
        buy_v = sum(r["value"] for r in info["buys"])
        sell_v = sum(r["value"] for r in info["sells"])
        ceo_np = sum(r["value"] for r in info["sells"] if not r["plan"]) if info["is_ceo"] else 0.0
        if info["buys"]:
            return {**base, "prio": 4, "kind": "f4buy", "warn": False, "label": "Form 4 내부자 장내 매수",
                    "detail": f"Form 4 {who} 장내 매수(P) {len(info['buys'])}건, 합계 {usd(buy_v)}."}
        thr = float(sec["insider_sale_usd"])
        if sell_v > thr or ceo_np > 0:
            why = []
            if sell_v > thr:
                why.append(f"신고 1건 매도 합계 {usd(sell_v)}, 기준 {usd(thr)} 초과")
            if ceo_np > 0:
                why.append(f"CEO 매도 중 10b5-1 계획 밖 {usd(ceo_np)}")
            return {**base, "prio": 3, "kind": "f4sell", "warn": True, "label": "Form 4 대규모 내부자 매도" if sell_v > thr else "Form 4 CEO 계획 밖 매도",
                    "detail": f"Form 4 {who} 장내 매도(S) {len(info['sells'])}건. " + ", ".join(why) + "."}
        return None
    return None


def sec_eval(cfg: dict, st: dict, subs: dict, today: date, fetch) -> tuple[list[dict], list[str], list[str]]:
    """-> (hits, accessions to mark seen, log lines). Pure apart from fetch(url)."""
    rows = sec_rows(subs)
    seen = set(st.get("sec_seen") or [])
    log: list[str] = []
    if "sec_seen" not in st:
        log.append(f"seed {len(rows)} accession numbers silently")
        return [], [r["acc"] for r in rows], log
    new = [r for r in rows if r["acc"] not in seen]
    start = date.fromisoformat(cfg["sec"]["start_date_kst"])
    if today < start:
        if new:
            log.append(f"before {start}: record {len(new)} new accession(s) silently: " + ", ".join(f"{r['form']} {r['acc']}" for r in new))
        return [], [r["acc"] for r in new], log
    hits, mark = [], []
    for r in sorted(new, key=lambda x: (x["date"], x["acc"])):
        try:
            h = classify(cfg, r, fetch)
        except Exception as exc:  # noqa: BLE001
            log.append(f"detail fetch failed, retry next run: {r['form']} {r['acc']} {exc}")
            continue
        mark.append(r["acc"])
        log.append(f"{r['date']} {r['form']} [{r['items']}] {r['acc']} -> " + (f"p{h['prio']} {h['label']}" if h else "ignore"))
        if h:
            hits.append(h)
    return hits, mark, log


def sec_message(cfg: dict, hits: list[dict]) -> tuple[str, str, int]:
    hits = sorted(hits, key=lambda h: (-h["prio"], h["date"]))
    top = hits[0]
    prio = top["prio"]
    more = f" 외 {len(hits) - 1}건" if len(hits) > 1 else ""
    warn = any(h["warn"] for h in hits)
    stamp = f"{datetime.now(KST):%Y-%m-%d %H:%M} KST | FIG | 피그마 SEC 공시"
    if prio == 2 and all(h["prio"] == 2 for h in hits):
        title = f"[FIG-PX] SEC {top['label']}{more} | 참고만"
        lines = [
            stamp,
            "이게 뭐 알림: 피그마 SEC 공시 중 고른 종류만 보내는 [FIG-PX] 공시 감시. 10-K 는 참고용 짧은 알림입니다.",
            "지금 결과/상태: " + " ".join(f"{h['form']} 제출일 {h['date']}, EDGAR {h['url']}" for h in hits),
            "지금 할 일: 없음. 시간 날 때 위험 요인과 가이던스 부분만 훑어보세요.",
            "나중에: 같은 접수번호는 다시 오지 않습니다.",
            "익절/줄일 때: 가격 구간 알림 기준 그대로.",
            "하지 말 것: 이 알림만 보고 주문. 투자 권유 아님.",
        ]
        return title, "\n".join(lines), 2
    if warn:
        act = "원문 확인, 추가매수 보류"
        todo = "EDGAR 원문을 먼저 읽으세요. 추가매수 보류, 내용이 실적과 투자 논리(NDR, 매출총이익률, 희석)에 주는 영향부터 확인."
    elif top["kind"] == "f4buy":
        act = "원문 확인, 추격 금지"
        todo = "EDGAR 원문에서 누가 얼마에 샀는지 확인만 하세요. 내부자 매수 한 건으로 계획을 바꾸지 마세요."
    else:
        act = "원문 확인"
        todo = "EDGAR 원문을 먼저 읽고 계약 상대, 금액, 실적 영향이 있는지 확인하세요."
    title = f"[FIG-PX] SEC {top['label']}{more} | {act}"
    state = " ".join(f"{i}) p{h['prio']} {h['detail']} 서류 {h['form']}, 항목 {h['items'] or '없음'}, 제출일 {h['date']}, EDGAR {h['url']}"
                     for i, h in enumerate(hits, 1))
    lines = [
        stamp,
        "이게 뭐 알림: 피그마(FIG) SEC 공시 중 판단에 중요한 종류(중요 8-K, 증권 발행, 13D, 대규모 내부자 거래, 10-K)만 골라 보내는 [FIG-PX] 공시 감시입니다. 투자 권유 아님.",
        f"지금 결과/상태: 새 공시 {len(hits)}건. {state}",
        "지금 할 일: " + todo,
        "나중에: 같은 접수번호는 다시 오지 않습니다. 종가 구간 알림(22, 17.1, 29.1달러)은 따로 계속 봅니다.",
        "익절/줄일 때: 재작성, 감사인 변경, 상장 관련 통지, 대규모 내부자 매도가 겹치고 종가가 17.1달러 아래면 비중 축소 검토.",
        "하지 말 것: 공시 제목만 보고 바로 주문, 장 시작 직후 추격, 신용/미수. 투자 권유 아님.",
    ]
    return title, "\n".join(lines), prio


def sec_part(cfg: dict, st: dict, now: datetime, dry: bool) -> None:
    if not cfg.get("sec"):
        return
    try:
        subs = json.loads(sec_get(cfg, cfg["sec"]["submissions"]))
    except Exception as exc:  # noqa: BLE001
        print(f"sec submissions fetch failed: {exc}")
        today = now.date().isoformat()
        if st.get("sec_fail_date") != today and now.date() >= date.fromisoformat(cfg["sec"]["start_date_kst"]):
            title, msg = "[FIG-PX] SEC 조회 실패 | 매매 신호 아님", f"Figma EDGAR submissions 조회 실패: {exc}. 다음 09:00 실행에서 다시 봅니다."
            if dry:
                print(f"DRY RUN would send (ops): {title}\n{msg}")
            else:
                publish(resolve_topic("ops"), title, msg, 3)
                st["sec_fail_date"] = today
        return
    hits, mark, log = sec_eval(cfg, st, subs, now.date(), lambda u: sec_get(cfg, u))
    for ln in log:
        print("  sec", ln)
    if hits:
        title, msg, prio = sec_message(cfg, hits)
        if dry:
            print(f"DRY RUN would send (p{prio}): {title}\n{msg}")
        else:
            publish(resolve_topic("port"), title, msg, prio, tags=["page_facing_up"])
    elif not log:
        print("  sec: no new filings")
    st["sec_seen"] = sorted(set(st.get("sec_seen") or []) | set(mark))
    st["sec_checked"] = now.isoformat(timespec="seconds")


F4_TMPL = """<ownershipDocument><aff10b5One>{box}</aff10b5One><reportingOwner><reportingOwnerId><rptOwnerName>{name}</rptOwnerName></reportingOwnerId>
<reportingOwnerRelationship><isOfficer>1</isOfficer><officerTitle>{title}</officerTitle></reportingOwnerRelationship></reportingOwner>
<nonDerivativeTable>{tx}</nonDerivativeTable><footnotes>{fn}</footnotes></ownershipDocument>"""
TX_TMPL = """<nonDerivativeTransaction><transactionCoding><transactionCode>{code}</transactionCode></transactionCoding>
<transactionAmounts><transactionShares><value>{sh}</value></transactionShares><transactionPricePerShare><value>{px}</value>{fref}</transactionPricePerShare></transactionAmounts></nonDerivativeTransaction>"""


def f4(name: str, title: str, txs: list[tuple], box: str = "0", plan_fn: bool = False) -> str:
    tx = "".join(TX_TMPL.format(code=c, sh=sh, px=px, fref='<footnoteId id="F1"/>' if ref else "") for c, sh, px, ref in txs)
    fn = '<footnote id="F1">Sold pursuant to a Rule 10b5-1 trading plan adopted on May 1, 2026.</footnote>' if plan_fn else ""
    return F4_TMPL.format(box=box, name=name, title=title, tx=tx, fn=fn)


def sec_selftest(cfg: dict, check) -> int:
    before = 0
    errs = [0]

    def chk(label, got, want):
        ok = got == want
        errs[0] += not ok
        check("sec " + label, got, want)

    doc_ceo = "<p>Item 5.02 Departure of Directors or Certain Officers. On Nov 9, 2026 our Chief Financial Officer resigned.</p><p>Item 9.01</p>"
    doc_dir = "<p>Item 5.02 Departure ... Mike Krieger, a member of the Board, resigned.</p><p>SIGNATURES By /s/ General Counsel</p>"
    docs = {
        "a502ceo.htm": doc_ceo, "a502dir.htm": doc_dir,
        "f4buy.xml": f4("Reed Andrew", "", [("P", 10000, 22.5, False)]),
        "f4big.xml": f4("Mulligan Brendan", "General Counsel", [("S", 600000, 24.0, True), ("S", 300000, 24.5, True)], box="1", plan_fn=True),
        "f4small.xml": f4("Herb Tyler", "Chief Accounting Officer", [("S", 2000, 24.0, True)], box="1", plan_fn=True),
        "f4ceoplan.xml": f4("Field Dylan", "President &amp; CEO", [("S", 100000, 24.0, True)], box="1", plan_fn=True),
        "f4ceonp.xml": f4("Field Dylan", "President &amp; CEO", [("S", 50000, 24.0, False)], box="0"),
        "f4tax.xml": f4("Melwani Praveer", "CFO and Treasurer", [("F", 5000, 24.0, False)]),
        "f4mixed.xml": f4("Field Dylan", "President &amp; CEO", [("S", 1000, 24.0, True), ("S", 2000, 24.0, False)], box="1", plan_fn=True),
    }

    def fetch(url: str) -> str:
        name = url.rsplit("/", 1)[-1]
        if name == "boom.xml" or name == "boom.htm":
            raise OSError("fixture network down")
        return docs[name]

    def F(form, acc, items="", doc="x.htm", d="2026-11-09"):
        return {"form": form, "date": d, "acc": acc, "items": items, "doc": doc}

    cases = [
        (F("8-K", "a1", "4.02,9.01"), 4), (F("8-K", "a2", "1.01,9.01"), 4), (F("8-K", "a3", "3.01"), 4),
        (F("8-K", "a4", "4.01,9.01"), 4), (F("8-K", "a5", "2.05"), 4), (F("8-K", "a6", "5.01"), 4),
        (F("8-K", "a7", "5.02", "a502ceo.htm"), 4), (F("8-K", "a8", "5.02,9.01", "a502dir.htm"), 3),
        (F("8-K", "a9", "5.02", "boom.htm"), 3), (F("8-K", "a10", "8.01,9.01"), 3), (F("8-K", "a11", "3.02"), 3),
        (F("8-K", "i1", "2.02,9.01"), None), (F("8-K", "i2", "5.07"), None), (F("8-K", "i3", "7.01,9.01"), None),
        (F("8-K/A", "a12", "2.01,9.01"), 4),
        (F("S-1", "o1"), 4), (F("S-3ASR", "o2"), 4), (F("424B4", "o3"), 4), (F("424B7", "o4"), 4),
        (F("SCHEDULE 13D", "d1"), 3), (F("SCHEDULE 13D/A", "d2"), 3), (F("SC 13D/A", "d3"), 3),
        (F("10-K", "k1"), 2),
        (F("144", "i4"), None), (F("S-8", "i5"), None), (F("SCHEDULE 13G/A", "i6"), None), (F("10-Q", "i7"), None),
        (F("DEF 14A", "i8"), None), (F("3", "i9"), None),
        (F("4", "f1", doc="xslF345X05/f4buy.xml"), 4), (F("4", "f2", doc="xslF345X05/f4big.xml"), 3),
        (F("4", "f3", doc="f4small.xml"), None), (F("4", "f4", doc="f4ceoplan.xml"), None),
        (F("4", "f5", doc="f4ceonp.xml"), 3), (F("4", "f6", doc="f4tax.xml"), None), (F("4", "f7", doc="f4mixed.xml"), 3),
    ]
    for f, want in cases:
        h = classify(cfg, f, fetch)
        chk(f"{f['form']} [{f['items']}] {f['doc']}", h["prio"] if h else None, want)
    # threshold edge: exactly at the limit does not fire, one dollar more does
    thr = float(cfg["sec"]["insider_sale_usd"])
    docs["edge0.xml"] = f4("X Y", "Director", [("S", thr / 20, 20, True)], box="1", plan_fn=True)
    docs["edge1.xml"] = f4("X Y", "Director", [("S", thr / 20 + 1, 20, True)], box="1", plan_fn=True)
    chk("sale == threshold ignored", classify(cfg, F("4", "e0", doc="edge0.xml"), fetch), None)
    chk("sale > threshold p3", (classify(cfg, F("4", "e1", doc="edge1.xml"), fetch) or {}).get("prio"), 3)

    subs = {"filings": {"recent": {
        "form": ["8-K", "4", "144", "10-K"], "filingDate": ["2026-11-09"] * 4,
        "accessionNumber": ["n1", "n2", "n3", "n4"], "items": ["4.02,9.01", "", "", ""],
        "primaryDocument": ["x.htm", "boom.xml", "x.txt", "k.htm"]}}}
    # first run: seed everything, nothing sent
    h, mark, _ = sec_eval(cfg, {}, subs, date(2026, 11, 9), fetch)
    chk("seed: no hits", h, [])
    chk("seed: all marked", sorted(mark), ["n1", "n2", "n3", "n4"])
    # before start: nothing sent even for a p4 filing, recorded silently
    for d in (date(2026, 10, 29), date(2026, 11, 4), date(2026, 11, 5)):
        h, mark, _ = sec_eval(cfg, {"sec_seen": []}, subs, d, fetch)
        chk(f"before start {d}: no hits", h, [])
        chk(f"before start {d}: marked", sorted(mark), ["n1", "n2", "n3", "n4"])
        before += bool(h)
    # from start: p4 + p2 sent, 144 ignored and marked, Form 4 fetch failure left for retry
    h, mark, log = sec_eval(cfg, {"sec_seen": []}, subs, date(2026, 11, 6), fetch)
    chk("start day: hit prios", sorted(x["prio"] for x in h), [2, 4])
    chk("start day: failed Form 4 not marked", sorted(mark), ["n1", "n3", "n4"])
    h2, mark2, _ = sec_eval(cfg, {"sec_seen": ["n1", "n3", "n4"]}, subs, date(2026, 11, 6), fetch)
    chk("dedupe by accession", (h2, mark2), ([], []))
    # messages
    t, body, p = sec_message(cfg, h)
    chk("combined prio is max", p, 4)
    chk("combined title", t.startswith("[FIG-PX] SEC 8-K 4.02") and t.endswith("| 원문 확인, 추가매수 보류"), True)
    print(p, t)
    for key in ("form 8-K", "4.02,9.01", "2026-11-09", "https://www.sec.gov/Archives/edgar/data/1579878/n1/n1-index.htm"):
        chk(f"body has {key.split()[-1]}", key.replace("form ", "") in body, True)
    chk("six parts", [ln.split(":")[0] for ln in body.split("\n")[1:]],
        ["이게 뭐 알림", "지금 결과/상태", "지금 할 일", "나중에", "익절/줄일 때", "하지 말 것"])
    allmsgs = []
    for f, want in cases:
        hh = classify(cfg, f, fetch)
        if hh:
            allmsgs.append(sec_message(cfg, [hh]))
    allmsgs.append(sec_message(cfg, h))
    for t, b, p in allmsgs:
        chk(f"no dot/dash {t[:30]}", any(ch in t + b for ch in "\u00b7\u2014"), False)
        chk(f"no buy wording {t[:30]}", [w for w in SEC_BAD_WORDS if w in t + b], [])
        chk(f"prefix {t[:20]}", t.startswith("[FIG-PX] "), True)
    t, b, p = sec_message(cfg, [classify(cfg, F("10-K", "k1"), fetch)])
    chk("10-K p2 short", (p, t), (2, "[FIG-PX] SEC 10-K 연차보고서 제출 | 참고만"))
    warn = [sec_message(cfg, [classify(cfg, F(*a), fetch)]) for a in
            (("8-K", "w1", "4.02"), ("8-K", "w2", "4.01"), ("8-K", "w3", "3.01"), ("4", "w4", "", "f4big.xml"))]
    for t, b, p in warn:
        chk(f"warning hold-off {t[:28]}", "추가매수 보류" in t and "추가매수 보류" in b, True)
    chk("nothing sends before 11/06", before, 0)
    return errs[0]


# ---------- selftest ----------

def selftest() -> int:
    cfg = load_cfg()
    L = cfg["levels"]
    fails = 0

    def check(label, got, want):
        nonlocal fails
        ok = got == want
        fails += not ok
        print(("ok  " if ok else "FAIL"), label, got, "" if ok else f"want {want}")

    A = {"buy22": True, "bear171": True, "base291": True}
    check("init 24.64", step(L, None, 24.64), (A, []))
    check("init 21 no send", step(L, None, 21.0), ({"buy22": False, "bear171": True, "base291": True}, []))
    check("init 30 no send", step(L, None, 30.0)[1], [])
    check("22.01 nothing", step(L, A, 22.01)[1], [])
    check("22.00 buy (<=)", step(L, A, 22.0)[1], ["buy22"])
    check("17.10 gap buy+bear", step(L, A, 17.1)[1], ["buy22", "bear171"])
    check("29.09 nothing", step(L, A, 29.09)[1], [])
    check("29.10 base (>=)", step(L, A, 29.1)[1], ["base291"])
    walk = [(21.5, ["buy22"]), (21.0, []), (22.0, []), (22.01, []), (21.9, ["buy22"]),
            (17.0, ["bear171"]), (16.5, []), (17.11, []), (17.1, ["bear171"]), (25.0, []),
            (29.2, ["base291"]), (29.5, []), (29.09, []), (29.1, ["base291"]), (20.0, ["buy22"])]
    st = dict(A)
    for i, (c, want) in enumerate(walk):
        st, f = step(L, st, c)
        check(f"walk{i} {c}", f, want)
    for fired in (["buy22"], ["buy22", "bear171"], ["base291"]):
        t, body, p = message(cfg, fired, 16.5 if "bear171" in fired else 21.0, "2026-11-05", 23.0)
        check(f"no middle dot / em-dash {fired}", any(ch in t + body for ch in "\u00b7\u2014"), False)
        print(p, t)
    t, body = confirm_message(cfg, 24.64, "2026-10-23")
    check("confirm no middle dot / em-dash", any(ch in t + body for ch in "\u00b7\u2014"), False)
    for fired in (["bear171"], ["buy22", "bear171"]):
        body = message(cfg, fired, 16.5, "2026-11-05", 23.0)[1]
        todo = next(x for x in body.split("\n") if x.startswith("지금 할 일:"))
        state_ln = next(x for x in body.split("\n") if x.startswith("지금 결과/상태:"))
        check(f"bear {fired}: hold-off todo", todo, "지금 할 일: " + STOP_TODO)
        check(f"bear {fired}: no buy advice in todo/status", [w for w in BUY_WORDS if w in todo + state_ln], [])
    body = message(cfg, ["buy22"], 21.0, "2026-11-05", 23.0)[1]
    check("pure buy22 todo unchanged", "지금 할 일: $20~22 분할매수 검토 구간 (사업 전망 유지 전제)." in body, True)
    check("priorities", message(cfg, ["buy22", "bear171"], 16.5, "d", None)[2], 5)
    sec_selftest(cfg, check)  # counts through check()
    print("selftest", "PASS" if not fails else f"FAIL {fails}")
    return 1 if fails else 0


# ---------- main ----------

def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    if mode == "selftest":
        return selftest()
    dry = mode == "dry"
    cfg = load_cfg()
    now = datetime.now(KST)
    print(f"{now.isoformat(timespec='seconds')} figpx mode={mode}")
    if dry:
        print("DRY RUN (no ntfy, no state write)")
    if mode == "confirm":  # install-confirmation send (AGENTS.md): no state
        try:
            bars = fetch_bars(cfg, now.astimezone(NY))
            d, c = bars[-1]
            title, msg = confirm_message(cfg, c, str(d))
        except Exception as exc:
            print(f"bars fetch failed: {exc}")
            title, msg = confirm_message(cfg, None, None)
        print(title)
        print(msg)
        publish(resolve_topic("port"), title, msg, 3, tags=["white_check_mark"])
        return 0
    vu = cfg.get("valid_until")
    if vu and now.date() > date.fromisoformat(vu):
        print(f"expired (valid_until {vu}); silent")
        return 0
    st = load_state()
    price_part(cfg, st, now, mode, dry)
    sec_part(cfg, st, now, dry)
    save_state(st, dry)
    return 0


def price_part(cfg: dict, st: dict, now: datetime, mode: str, dry: bool) -> None:
    try:
        bars = fetch_bars(cfg, now.astimezone(NY))
    except Exception as exc:
        print(f"bars fetch failed: {exc}")
        if not st.get("fail_notified"):
            title = "[FIG-PX] 시세 조회 실패 | 매매 신호 아님"
            msg = f"FIG 일봉 조회 실패: {exc}. 다음 09:00 실행에서 다시 봅니다."
            if dry:
                print(f"DRY RUN would send (ops): {title}\n{msg}")
            else:
                publish(resolve_topic("ops"), title, msg, 3)
                st["fail_notified"] = True
        return
    if not bars:
        print("no completed bars")
        return
    st["fail_notified"] = False
    L = cfg["levels"]
    last_d, last_c = bars[-1]
    if (now.date() - last_d).days > int(cfg.get("stale_days", 6)):
        print(f"stale bar {last_d}; skip")
        return
    armed = st.get("armed") if st.get("initialized") else None
    if armed is None:
        armed, _ = step(L, None, last_c)
        st.update({"initialized": True, "armed": armed, "last_date": str(last_d), "last_close": round(last_c, 4),
                   "init_note": f"initialized from {last_d} close {last_c:.2f}, no send"})
        print(f"init from {last_d} close {last_c:.2f}: {armed} (no send)")
        return
    prev_date = st.get("last_date")
    new_bars = [(d, c) for d, c in bars if prev_date is None or str(d) > prev_date]
    if mode == "force" and not new_bars:
        new_bars = bars[-1:]
    if not new_bars:
        print(f"no new bar after {prev_date}; armed={armed}; last close {last_c:.2f} ({last_d})")
        return
    fired_all: list[str] = []
    prev_close = st.get("last_close")
    for d, c in new_bars:
        armed, fired = step(L, armed, c)
        print(f"bar {d} close {c:.2f} -> fired={fired} armed={armed}")
        fired_all += [k for k in fired if k not in fired_all]
    d, c = new_bars[-1]
    if len(new_bars) > 1:
        prev_close = new_bars[-2][1]
    for lv in L:
        print(f"  line {lv['price']:.2f} ({lv['label']}): {'armed' if armed[lv['key']] else 'inside zone'}, distance {(c / lv['price'] - 1) * 100:+.1f}%")
    if fired_all:
        title, msg, prio = message(cfg, fired_all, c, str(d), prev_close)
        if dry:
            print(f"DRY RUN would send (p{prio}): {title}\n{msg}")
        else:
            tag = "chart_with_upwards_trend" if fired_all == ["base291"] else "chart_with_downwards_trend"
            publish(resolve_topic("port"), title, msg, prio, tags=[tag])
    else:
        print("no zone entry; silent")
    st.update({"armed": armed, "last_date": str(d), "last_close": round(c, 4)})


if __name__ == "__main__":
    raise SystemExit(main())
