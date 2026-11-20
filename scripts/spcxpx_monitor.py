#!/usr/bin/env python3
"""[SPCX-PX] SpaceX (Nasdaq: SPCX) 관심 종목 종가 선 + SEC 공시 감시 (2026-11-20 pt87u, 미보유).

평일 09:00 KST (port-alerts morning 슬롯에 부착, 새 cron 없음). Yahoo SPCX 일봉, 뉴욕 16:00 이후 끝난 날만.
선(상태가 바뀔 때만 1번, 재무장 완충):
  dn135 : 종가 <= 135 (공모가 1차 관심선) -> p3 공모가 관심 구간 점검 (139 이상 재무장)
  dn120 : 종가 <= 120 (2차 관심선)       -> p3 2차 관심 구간 점검 (124 이상 재무장)
          같은 날 135와 120이 같이 걸리면 한 건으로 합침
  up165 : 종가 >= 165 (추격 보류 구간)   -> p3 추격 보류 구간 (160 이하 재무장)
SEC (CIK 0001181412): 8-K/8-K/A, S-3, S-3ASR, 424B5.
  Anthropic/Customer B: Item 1.02 또는 해지/취소 문구는 p4(매수 문구 없음), 그 외 계약 갱신은 p3
  증자/자금조달: S-3/S-3ASR/424B5, Item 3.02, 또는 관련 키워드 -> p4(매수 문구 없음)
  테슬라 합병만: [TSLA-SPCX] state/tslaspcx.json seen 에 있으면 생략, 없으면 tsla_pending.
                1일 뒤 또는 tslaspcx 조회 실패 시에도 아직 없으면 p4 대체 발송
  Anthropic/증자와 합병이 겹치면 위험 공시는 그대로 발송(합병만 생략하지 않음)
첫 실행은 조용히 시드. 조회 실패 3회 연속은 cwally-ops 하루 1번. ends_kst 뒤 조용히 종료.
usage: spcxpx_monitor.py <auto|dry> | selftest | confirm
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
from ntfy_util import publish, resolve_server, resolve_topic, route_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
NY = ZoneInfo("America/New_York")
NAME = "spcxpx"
TAG = "[SPCX-PX]"
TICKER = "SPCX"
CFG_PATH = ROOT / "configs" / f"{NAME}.json"
STATE_PATH = ROOT / "state" / f"{NAME}.json"
TSLA_STATE_PATH = ROOT / "state" / "tslaspcx.json"
YAHOO_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
BUY_WORDS = ("매수 검토", "분할 매수", "매수 기회", "저가 매수", "사세요", "담을", "물타기", "비중 확대", "지금 매수")
DOTS = "\u00b7\u30fb\u2027\u2014"
OPS = {"le": lambda v, x: v <= x, "lt": lambda v, x: v < x, "ge": lambda v, x: v >= x, "gt": lambda v, x: v > x}


def load_cfg() -> dict:
    return json.loads(CFG_PATH.read_text(encoding="utf-8"))


def load_json(path: Path, fallback: dict) -> dict:
    if not path.exists():
        return dict(fallback)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return dict(fallback)
    return data if isinstance(data, dict) else dict(fallback)


def usd(v) -> str:
    return f"${v:,.2f}" if v is not None else "?"


def get_bytes(url: str, ua: str, limit: int | None = None) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept-Encoding": "identity"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = resp.read(limit + 1) if limit else resp.read()
    if limit and len(data) > limit:
        data = data[:limit]
    return data


def yahoo_rows(cfg: dict, now: datetime) -> list[tuple[str, float]]:
    """Completed US/Eastern daily closes. If today's bar close is empty after 16:00, use regularMarketPrice (figpx)."""
    raw = get_bytes(cfg["sources"]["price"], YAHOO_UA)
    res = json.loads(raw.decode("utf-8", errors="ignore"))["chart"]["result"][0]
    out: dict[date, float] = {}
    for t, c in zip(res.get("timestamp") or [], (res.get("indicators") or {}).get("quote", [{}])[0].get("close") or []):
        if c is None:
            continue
        out[datetime.fromtimestamp(t, NY).date()] = float(c)
    meta = res.get("meta") or {}
    rmt, rmp = meta.get("regularMarketTime"), meta.get("regularMarketPrice")
    if rmt and rmp is not None:
        mt = datetime.fromtimestamp(rmt, NY)
        if mt.date() not in out and mt.hour * 60 + mt.minute >= 16 * 60:
            out[mt.date()] = float(rmp)
    now_ny = now.astimezone(NY)
    hh, mm = (int(x) for x in str(cfg.get("close_hm") or "16:00").split(":"))
    after_close = now_ny.hour * 60 + now_ny.minute >= hh * 60 + mm + 10
    today = now_ny.date()
    return sorted((d.isoformat(), c) for d, c in out.items() if d < today or (d == today and after_close))


def judge(cfg: dict, armed: dict, p: float) -> list[dict]:
    fired = []
    for ln in cfg["lines"]:
        k = ln["key"]
        hit = OPS[ln["op"]](p, ln["level"])
        if k not in armed:
            armed[k] = not hit
        elif armed[k] and hit:
            armed[k] = False
            fired.append(ln)
        elif not armed[k] and OPS[ln["rearm_op"]](p, ln["rearm"]):
            armed[k] = True
    return fired


def line_text(ln: dict) -> tuple[str, str]:
    k = ln["key"]
    if k == "dn135":
        return ("공모가($135) 관심 구간입니다. 가이던스, 수주, 증자/희석, Anthropic(Customer B) 계약 유지를 직접 확인하세요. 미보유, 매수 계획 없음.",
                "하루 종가를 바닥으로 읽기, 계획 없이 한 번에 사기, 추격.")
    if k == "dn120":
        return ("2차 관심 구간($120)입니다. 공모가 대비 낙폭, 계약/증자 공시, 논리 유지 여부를 다시 확인하세요. 미보유, 매수 계획 없음.",
                "낙폭을 즉시 매수 신호로 읽기, 계획 없이 한 번에 사기, 추격.")
    return ("추격 보류 구간($165)입니다. 추가 추격은 하지 마세요. 목표가와 비중 계획을 먼저 점검하세요.",
            "목표가 도달을 추가 상승 신호로 읽기, 추격.")


def price_message(cfg: dict, fired: list[dict], day: str, p: float, stamp: str) -> tuple[str, str, int]:
    keys = {x["key"] for x in fired}
    shown = list(fired)
    if "dn135" in keys and "dn120" in keys:
        order = {"dn135": 0, "dn120": 1, "up165": 2}
        shown = sorted(fired, key=lambda x: order.get(x["key"], 9))
    title = f"{TAG} {TICKER} 종가 {usd(p)} | " + " + ".join(x["title"] for x in shown)
    todo = " ".join(line_text(x)[0] for x in shown)
    dont = " ".join(line_text(x)[1] for x in shown)
    body = "\n".join([
        f"이게 뭐 알림: SpaceX(SPCX) 관심 종목(미보유) 미국 정규장 종가가 미리 정한 선에 닿았다는 알림. 점검 시점. 매매 지시 아님.",
        f"{stamp} | SPCX | Space Exploration Technologies",
        f"지금 결과/상태: {day} 종가 {usd(p)}(Yahoo, 뉴욕 정규장). 걸린 선: {', '.join(x['title'] for x in shown)}.",
        f"지금 할 일: {todo}",
        "나중에: 같은 선은 재무장 뒤에만 다시 옵니다($135 선은 $139 이상, $120 선은 $124 이상, $165 선은 $160 이하). "
        f"SEC 공시(Anthropic 계약, 증자, 테슬라 합병)는 같은 알림이 따로 봅니다. 종료 {cfg['ends_kst'][:10]}.",
        "익절/줄일 때: 미보유, 해당 없음.",
        f"하지 말 것: {dont}",
    ])
    return title, body, max(x["priority"] for x in shown)


def sec_get(cfg: dict, url: str, limit: int | None = None) -> str:
    cap = limit if limit is not None else int(cfg["sec"].get("doc_limit_bytes") or 400000)
    return get_bytes(url, cfg["sec"]["user_agent"], cap).decode("utf-8", errors="ignore")


def sec_rows(subs: dict) -> list[dict]:
    r = (subs.get("filings") or {}).get("recent") or {}
    keys = ("form", "filingDate", "accessionNumber", "items", "primaryDocument")
    cols = [r.get(k) or [] for k in keys]
    n = min(len(c) for c in cols) if cols else 0
    return [{"form": cols[0][i], "date": cols[1][i], "acc": cols[2][i], "items": cols[3][i] or "",
             "doc": cols[4][i] or ""} for i in range(n)]


def filing_dir(cfg: dict, f: dict) -> str:
    return f"https://www.sec.gov/Archives/edgar/data/{int(cfg['sec']['cik'])}/{f['acc'].replace('-', '')}"


def index_url(cfg: dict, f: dict) -> str:
    return f"{filing_dir(cfg, f)}/{f['acc']}-index.htm"


def parse_items(raw: str) -> list[str]:
    return [x.strip() for x in str(raw or "").split(",") if x.strip()]


def plain(doc_html: str) -> str:
    t = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", doc_html or "", flags=re.S | re.I)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", t))).strip()


def has_any(text: str, words: list[str]) -> bool:
    low = (text or "").lower()
    return any(w.lower() in low for w in words)


def classify(cfg: dict, f: dict, fetch) -> dict | None:
    """One filing -> hit dict or None. fetch(url) -> text. Text fetch errors propagate for retry."""
    sec = cfg["sec"]
    form = f["form"].upper().strip()
    items = parse_items(f["items"])
    base = {"form": f["form"], "date": f["date"], "acc": f["acc"], "items": f["items"], "url": index_url(cfg, f)}
    offer_forms = {x.upper() for x in sec["forms_offer"]}
    forms_8k = {x.upper() for x in sec["forms_8k"]}
    cats: list[str] = []
    labels: list[str] = []
    details: list[str] = []
    prio = 3
    warn = False
    text = ""
    need_text = form in forms_8k
    if need_text and f.get("doc"):
        text = plain(fetch(f"{filing_dir(cfg, f)}/{f['doc'].split('/')[-1]}"))
    blob = " ".join([form, " ".join(items), text])

    if form in offer_forms:
        cats.append("capital")
        labels.append(f"{f['form']} 증자/자금조달")
        details.append(f"{f['form']} 증권 신고 또는 발행 설명서. 신주/ATM/전환사채 등 희석 가능.")
        prio, warn = 4, True
    if form in forms_8k:
        anth_term = any(i in set(sec["anthropic_items_term"]) for i in items)
        anth_kw = has_any(blob, sec["anthropic_keywords"])
        anth_update = any(i in set(sec["anthropic_items_update"]) for i in items) and anth_kw
        if anth_term or anth_update or (anth_kw and has_any(blob, sec["term_keywords"])):
            term = anth_term or has_any(blob, sec["term_keywords"])
            cats.append("anthropic")
            if term:
                labels.append("8-K Anthropic 계약 종료")
                details.append("8-K Item 1.02 또는 계약 해지/취소 문구. Anthropic(Customer B) 계약 유지 여부를 원문에서 확인.")
                prio, warn = 4, True
            else:
                labels.append("8-K Anthropic 계약 갱신")
                details.append("8-K Item 1.01/8.01 등에서 Anthropic 또는 Customer B, compute 계약 관련 갱신. 해지 아님.")
                prio = max(prio, 3)
        cap_item = any(i in set(sec["capital_items"]) for i in items)
        cap_kw = has_any(blob, sec["capital_keywords"])
        if cap_item or cap_kw:
            if "capital" not in cats:
                cats.append("capital")
                labels.append("8-K 증자/자금조달")
                details.append("8-K Item 3.02 또는 증자/자금조달 키워드(공모, ATM, 전환사채, 크레딧 등). 희석 가능성을 원문에서 확인.")
            prio, warn = 4, True
        tesla = has_any(blob, sec["tesla_names"]) and has_any(blob, sec["tesla_terms"])
        if tesla:
            cats.append("tesla")
            labels.append("8-K 테슬라 합병")
            details.append("8-K 본문에 tesla/tsla 와 합병 관련 용어가 같이 있음. 확정 여부는 원문과 [TSLA-SPCX]를 확인.")
            if "anthropic" not in cats and "capital" not in cats:
                prio, warn = 4, True
    if not cats:
        return None
    return {**base, "cats": cats, "prio": prio, "warn": warn,
            "label": labels[0] + (f" 외 {len(labels) - 1}건" if len(labels) > 1 else ""),
            "detail": " ".join(details), "labels": labels}


def load_tsla_state(io: dict) -> dict:
    if "tsla_state" in io:
        return io["tsla_state"] or {}
    return load_json(TSLA_STATE_PATH, {})


def tslaspcx_seen(tsla: dict) -> set[str]:
    return {str(x) for x in (tsla.get("seen") or [])}


def tslaspcx_fetch_failed(tsla: dict, since_day: str) -> bool:
    fr = tsla.get("fail_runs") or {}
    fn = tsla.get("fail_notice") or {}
    for k in ("sec_tesla", "sec_spacex", "sec_docs"):
        if int(fr.get(k) or 0) >= 3:
            return True
        d = fn.get(k)
        if d and str(d)[:10] >= since_day:
            return True
    return False


def pending_should_fallback(item: dict, today: str, tsla: dict, lag_days: int) -> str | None:
    acc = item["acc"]
    if acc in tslaspcx_seen(tsla):
        return "resolved"
    filed = str(item.get("filed") or item.get("date") or today)[:10]
    if tslaspcx_fetch_failed(tsla, filed):
        return "tslaspcx_ops_fail"
    if date.fromisoformat(today) >= date.fromisoformat(filed) + timedelta(days=lag_days):
        return "tslaspcx_lag"
    return None


def sec_eval(cfg: dict, st: dict, subs: dict, today: date, fetch, tsla: dict) -> tuple[list[dict], list[dict], list[str], list[str]]:
    """-> (send_hits, fallback_hits, accessions to mark seen, log)."""
    rows = sec_rows(subs)
    seen = set(st.get("sec_seen") or [])
    log: list[str] = []
    if "sec_seen" not in st:
        log.append(f"seed {len(rows)} accession numbers silently")
        return [], [], [r["acc"] for r in rows], log
    new = [r for r in rows if r["acc"] not in seen]
    hits, mark = [], []
    pending = list(st.get("tsla_pending") or [])
    pending_acs = {p["acc"] for p in pending}
    for r in sorted(new, key=lambda x: (x["date"], x["acc"])):
        try:
            h = classify(cfg, r, fetch)
        except Exception as exc:  # noqa: BLE001
            log.append(f"detail fetch failed, retry next run: {r['form']} {r['acc']} {exc}")
            continue
        mark.append(r["acc"])
        if not h:
            log.append(f"{r['date']} {r['form']} [{r['items']}] {r['acc']} -> ignore")
            continue
        cats = set(h["cats"])
        risk = bool(cats & {"anthropic", "capital"})
        if cats == {"tesla"} or (cats == set(h["cats"]) and "tesla" in cats and not risk):
            if r["acc"] in tslaspcx_seen(tsla):
                log.append(f"{r['date']} {r['form']} {r['acc']} -> tesla already in [TSLA-SPCX] seen, skip")
            elif r["acc"] not in pending_acs:
                pending.append({"acc": r["acc"], "filed": r["date"], "date": r["date"], "form": r["form"],
                                "url": h["url"], "label": h["label"], "detail": h["detail"], "items": r["items"]})
                pending_acs.add(r["acc"])
                log.append(f"{r['date']} {r['form']} {r['acc']} -> tesla pending (not yet in [TSLA-SPCX])")
            continue
        log.append(f"{r['date']} {r['form']} [{r['items']}] {r['acc']} -> p{h['prio']} {h['label']}")
        hits.append(h)
    lag = int(cfg["sec"].get("lag_days") or 1)
    today_s = today.isoformat()
    still, fallback = [], []
    for item in pending:
        reason = pending_should_fallback(item, today_s, tsla, lag)
        if reason == "resolved":
            log.append(f"pending {item['acc']} resolved by [TSLA-SPCX], drop")
            continue
        if reason in {"tslaspcx_lag", "tslaspcx_ops_fail"}:
            fb = {
                "form": item.get("form") or "8-K",
                "date": item.get("date") or item.get("filed"),
                "acc": item["acc"],
                "items": item.get("items") or "",
                "url": item.get("url") or "",
                "cats": ["tesla"],
                "prio": 4,
                "warn": True,
                "label": "8-K TSLA-SPCX 미처리 대체 발송",
                "detail": item.get("detail") or "테슬라 합병 관련 8-K. [TSLA-SPCX]가 아직 처리하지 않아 대체 발송.",
                "fallback": reason,
                "labels": ["8-K TSLA-SPCX 미처리 대체 발송"],
            }
            fallback.append(fb)
            log.append(f"pending {item['acc']} fallback {reason}")
            continue
        still.append(item)
    st["tsla_pending"] = still[-50:]
    return hits, fallback, mark, log


def sec_message(cfg: dict, hits: list[dict], stamp: str, *, fallback: bool = False) -> tuple[str, str, int]:
    hits = sorted(hits, key=lambda h: (-h["prio"], h["date"]))
    top, prio = hits[0], hits[0]["prio"]
    more = f" 외 {len(hits) - 1}건" if len(hits) > 1 else ""
    warn = any(h.get("warn") for h in hits) or fallback
    if warn:
        act = "원문 확인, 추가 보류"
        todo = ("EDGAR 원문을 먼저 읽으세요. 관망 유지, 추가 보류. 계약 해지, 희석(신주, 전환사채), "
                "합병 조건이 관심 논리에 주는 영향부터 확인. 매수 문구 아님.")
    else:
        act = "원문 확인"
        todo = "EDGAR 원문에서 계약 상대, 금액, 기간, 해지 조항이 있는지 확인하세요. 공시 한 건으로 계획을 바꾸지 마세요."
    state = " ".join(
        f"{i}) p{h['prio']} {h['detail']} 서류 {h['form']}, 항목 {h['items'] or '없음'}, 제출일 {h['date']}, EDGAR {h['url']}"
        + (f" ({'TSLA-SPCX 조회 실패' if h.get('fallback')=='tslaspcx_ops_fail' else '1일 대기 후 미처리'})"
           if h.get("fallback") else "")
        for i, h in enumerate(hits, 1)
    )
    what = "테슬라 합병 공시를 [TSLA-SPCX]가 처리하지 못해 대체 발송하는" if fallback else "Anthropic/Customer B 계약, 증자/자금조달, 테슬라 합병 관련"
    body = "\n".join([
        f"이게 뭐 알림: SpaceX(SPCX) 관심 종목(미보유) SEC 공시 중 {what} 서류를 골라 보내는 [SPCX-PX] 알림입니다. 매매 지시 아님.",
        f"{stamp} | SPCX | Space Exploration Technologies",
        f"지금 결과/상태: 새 공시 {len(hits)}건. {state}",
        "지금 할 일: " + todo,
        "나중에: 같은 접수번호는 다시 오지 않습니다. 테슬라 합병만 해당하고 [TSLA-SPCX]가 이미 본 건은 생략합니다. "
        f"종가 선($135, $120, $165)은 상태가 바뀔 때만 옵니다. 종료 {cfg['ends_kst'][:10]}.",
        "익절/줄일 때: 미보유, 해당 없음.",
        "하지 말 것: 공시 제목만 보고 바로 주문, 장 시작 직후 추격, 신용/미수. 투자 권유 아님.",
    ])
    return f"{TAG} {TICKER} {top['label']}{more} | {act}", body, prio


def fail_message(stamp: str, loud: list[tuple[str, str]]) -> tuple[str, str, int]:
    body = "\n".join([
        "이게 뭐 알림: SPCX 관심 알림의 데이터 조회 실패 안내(3회 연속). 매매 신호 아님.",
        f"{stamp} | SPCX | Space Exploration Technologies",
        f"지금 결과/상태: {', '.join(w for w, _ in loud)} 확인 불가 ({'; '.join(m for _, m in loud)}).",
        "지금 할 일: 필요하면 Yahoo, SEC EDGAR 에서 직접 보세요.",
        "나중에: 다음 평일 09:00 에 다시 봅니다.",
        "익절/줄일 때: 이 안내로 판단을 바꾸지 않기.",
        "하지 말 것: 조회 실패를 조건 미충족으로 읽기.",
    ])
    return f"{TAG} {TICKER} {', '.join(w for w, _ in loud)} 조회 실패 | 판정 보류", body, 3


def confirm_message(cfg: dict, stamp: str) -> tuple[str, str, int]:
    body = "\n".join([
        "이게 뭐 알림: SpaceX(SPCX) 관심 종목 종가 선과 SEC 공시 감시 알림 설치 확인입니다. 매매 신호 아님.",
        f"{stamp} | SPCX | Space Exploration Technologies",
        "지금 결과/상태: 설치 확인만 합니다. 종가/공시 판정은 다음 평일 09:00 부터.",
        "지금 할 일: 없음. 평일 09:00 KST 에 전일 미국 종가로 $135 이하(공모가 관심 구간 점검), "
        "$120 이하(2차 관심 구간 점검), $165 이상(추격 보류 구간)과 Anthropic 계약, 증자, 테슬라 합병 공시를 상태가 바뀔 때만 봅니다.",
        f"나중에: 테슬라 합병은 [TSLA-SPCX](매일 07:30)가 먼저 보고, 여기선 중복을 줄입니다. 종료 {cfg['ends_kst'][:10]}.",
        "익절/줄일 때: 미보유, 해당 없음.",
        "하지 말 것: 이 설치 확인을 매매 신호로 읽기.",
    ])
    return f"{TAG} {TICKER} 관심 종목 알림 설치 확인 | 매매 신호 아님", body, 3


def run(mode: str, now: datetime, cfg: dict, io: dict | None = None) -> tuple[list, dict]:
    io = io or {}
    dry = mode == "dry"
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    today = stamp[:10]
    st = io["state"] if "state" in io else load_json(STATE_PATH, {})
    out, fails = [], []
    try:
        rows = io["rows"]() if "rows" in io else yahoo_rows(cfg, now)
        if not rows:
            raise ValueError("확정 종가 없음")
        day, p = rows[-1]
        armed = st.setdefault("armed", {})
        last = st.get("last_date")
        fired: list[dict] = []
        for d, c in rows:
            if (last is None and d == day) or (last is not None and d > last):
                for f in judge(cfg, armed, c):
                    fired = [x for x in fired if x["key"] != f["key"]] + [f]
        if fired and last is not None:
            out.append(price_message(cfg, fired, day, p, stamp))
        st.update(last_date=day, last_close=[day, p])
        print(f"close {day} {usd(p)} armed={armed}")
    except Exception as exc:  # noqa: BLE001
        fails.append(("SPCX 종가", str(exc)[:150]))
        print(f"price fetch failed: {exc}")

    tsla = load_tsla_state(io)
    try:
        if "subs" in io:
            subs = io["subs"]() if callable(io["subs"]) else io["subs"]
        else:
            subs = json.loads(sec_get(cfg, cfg["sec"]["submissions"], limit=2_000_000))
        fetch = io["fetch"] if "fetch" in io else (lambda u: sec_get(cfg, u))
        hits, fallback, mark, log = sec_eval(cfg, st, subs, date.fromisoformat(today), fetch, tsla)
        for line in log:
            print(line)
        seen = set(st.get("sec_seen") or [])
        seen.update(mark)
        st["sec_seen"] = sorted(seen)
        if hits:
            out.append(sec_message(cfg, hits, stamp))
        if fallback:
            out.append(sec_message(cfg, fallback, stamp, fallback=True))
    except Exception as exc:  # noqa: BLE001
        fails.append(("SEC 공시", str(exc)[:150]))
        print(f"sec submissions fetch failed: {exc}")

    fs = st.setdefault("fail_streak", {})
    keys = {w for w, _ in fails}
    for k in list(fs):
        if k not in keys:
            fs.pop(k)
    for k in keys:
        fs[k] = fs.get(k, 0) + 1
    loud = [(w, m) for w, m in fails if fs[w] >= 3]
    if loud and st.get("fail_day") != today:
        st["fail_day"] = today
        out.append(fail_message(stamp, loud))
    st["updated_at"] = stamp
    if "state" in io:
        return out, st
    for t, m, pri in out:
        print(t)
        print(m)
        if not dry:
            publish(
                resolve_topic("port", cfg.get("ntfy_topic")),
                t, m, pri,
                ["warning"] if pri >= 4 else ["chart_with_upwards_trend"],
                resolve_server(cfg.get("ntfy_server")),
                config_name=NAME,
            )
    if not out:
        print("no alert")
    if dry:
        print("DRY RUN: no publish, state not written")
    else:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return out, st


def _check(fails: list, label, got, want=True):
    if want is True and not isinstance(got, bool):
        ok = bool(got)
        want_r = True
    else:
        ok = got == want
        want_r = want
    fails.append(0 if ok else 1)
    print(("ok  " if ok else "FAIL"), label, "" if ok else f"got {got!r} want {want_r!r}")
    return ok


def selftest() -> int:
    fails: list[int] = []
    cfg = load_cfg()
    stamp = "2026-11-20 09:00 KST"

    def chk(label, got, want=True):
        return _check(fails, label, got, want)

    # --- price lines ---
    a = {"dn135": True, "dn120": True, "up165": True}
    chk("140 silent", [x["key"] for x in judge(cfg, a, 140.0)], [])
    chk("135 fires dn135", [x["key"] for x in judge(cfg, a, 135.0)], ["dn135"])
    chk("136 no rearm yet", [x["key"] for x in judge(cfg, a, 136.0)], [])
    chk("139 rearms", (judge(cfg, a, 139.0), a["dn135"]), ([], True))
    chk("134 fires again", [x["key"] for x in judge(cfg, a, 134.0)], ["dn135"])

    a2 = {"dn135": True, "dn120": True, "up165": True}
    got = [x["key"] for x in judge(cfg, a2, 119.0)]
    chk("120 merges with 135 keys", got, ["dn135", "dn120"])
    t, b, p = price_message(cfg, [x for x in cfg["lines"] if x["key"] in {"dn135", "dn120"}], "2026-12-01", 119.0, stamp)
    chk("merged title", t, "[SPCX-PX] SPCX 종가 $119.00 | 공모가 관심 구간 점검 + 2차 관심 구간 점검")
    chk("merged p3", p, 3)
    chk("124 rearms dn120 only", (judge(cfg, a2, 124.0), a2["dn120"], a2["dn135"]), ([], True, False))

    a3 = {"dn135": True, "dn120": True, "up165": True}
    chk("164 silent", [x["key"] for x in judge(cfg, a3, 164.0)], [])
    chk("165 fires up165", [x["key"] for x in judge(cfg, a3, 165.0)], ["up165"])
    chk("161 no rearm", [x["key"] for x in judge(cfg, a3, 161.0)], [])
    chk("160 rearms", (judge(cfg, a3, 160.0), a3["up165"]), ([], True))
    t165, b165, p165 = price_message(cfg, [x for x in cfg["lines"] if x["key"] == "up165"], "2026-12-02", 166.0, stamp)
    chk("165 title", t165, "[SPCX-PX] SPCX 종가 $166.00 | 추격 보류 구간")
    chk("165 p3", p165, 3)

    # --- SEC classify ---
    def fetch_map(docs: dict):
        def _f(url: str) -> str:
            for k, v in docs.items():
                if k in url:
                    return v
            raise FileNotFoundError(url)
        return _f

    def filing(form, date_, acc, items="", doc="x.htm"):
        return {"form": form, "date": date_, "acc": acc, "items": items, "doc": doc}

    term_html = "<html><body>Item 1.02 Termination of a Material Definitive Agreement. SpaceX and Anthropic (Customer B) ended the compute agreement on 90-day notice.</body></html>"
    h = classify(cfg, filing("8-K", "2026-11-20", "0001181412-26-000101", "1.02", "term.htm"), fetch_map({"term.htm": term_html}))
    chk("1.02 is anthropic p4", (h["cats"], h["prio"], h["warn"], "종료" in h["label"]), (["anthropic"], 4, True, True))
    t1, b1, p1 = sec_message(cfg, [h], stamp)
    chk("1.02 title has SPCX and 종료", "[SPCX-PX]" in t1 and "SPCX" in t1 and "종료" in t1, True)
    chk("1.02 no buy wording", [w for w in BUY_WORDS if w in t1 + b1], [])
    chk("1.02 p4", p1, 4)

    upd_html = "<html><body>Item 1.01 Entry into a Material Definitive Agreement. Amendment of the Anthropic Customer B compute agreement, extending the term and fee schedule.</body></html>"
    h2 = classify(cfg, filing("8-K", "2026-11-20", "0001181412-26-000102", "1.01", "upd.htm"), fetch_map({"upd.htm": upd_html}))
    chk("1.01 is anthropic p3", (h2["cats"], h2["prio"], h2["warn"], "갱신" in h2["label"]), (["anthropic"], 3, False, True))
    t2, b2, p2 = sec_message(cfg, [h2], stamp)
    chk("1.01 p3 title", p2 == 3 and "갱신" in t2, True)

    offer_html = "<html><body>Item 3.02 Unregistered Sales of Equity Securities. Underwriting agreement for a follow-on equity offering of common stock.</body></html>"
    h3 = classify(cfg, filing("8-K", "2026-11-20", "0001181412-26-000103", "3.02", "off.htm"), fetch_map({"off.htm": offer_html}))
    chk("8-K 3.02 capital p4", (h3["cats"], h3["prio"], h3["warn"]), (["capital"], 4, True))
    t3, b3, p3 = sec_message(cfg, [h3], stamp)
    chk("capital 8-K no buy wording", [w for w in BUY_WORDS if w in t3 + b3], [])

    h4 = classify(cfg, filing("S-3", "2026-11-20", "0001181412-26-000104", "", "s3.htm"), fetch_map({"s3.htm": "shelf"}))
    chk("S-3 capital p4", (h4["cats"], h4["prio"], h4["warn"], "증자" in h4["label"]), (["capital"], 4, True, True))
    t4, b4, _ = sec_message(cfg, [h4], stamp)
    chk("S-3 no buy wording", [w for w in BUY_WORDS if w in t4 + b4], [])

    merge_html = "<html><body>Item 8.01 Other Events. Tesla (TSLA) and SpaceX signed a definitive agreement for a merger. Special committee reviewed the exchange ratio.</body></html>"
    hm = classify(cfg, filing("8-K", "2026-11-20", "0001181412-26-000201", "8.01", "m.htm"), fetch_map({"m.htm": merge_html}))
    chk("tesla merger classified", "tesla" in hm["cats"] and "anthropic" not in hm["cats"] and "capital" not in hm["cats"], True)

    both_html = "<html><body>Item 1.02 Termination of the Anthropic Customer B compute agreement. The Tesla merger special committee also met.</body></html>"
    hb = classify(cfg, filing("8-K", "2026-11-20", "0001181412-26-000202", "1.02,8.01", "both.htm"), fetch_map({"both.htm": both_html}))
    chk("anthropic+tesla keeps risk", set(hb["cats"]) >= {"anthropic", "tesla"} and hb["prio"] == 4, True)

    def subs_of(*rows):
        return {"filings": {"recent": {
            "form": [r["form"] for r in rows],
            "filingDate": [r["date"] for r in rows],
            "accessionNumber": [r["acc"] for r in rows],
            "items": [r["items"] for r in rows],
            "primaryDocument": [r["doc"] for r in rows],
        }}}

    docs = {"term.htm": term_html, "upd.htm": upd_html, "off.htm": offer_html, "s3.htm": "shelf",
            "m.htm": merge_html, "both.htm": both_html}
    fetch = fetch_map(docs)
    today = date(2026, 11, 20)

    # tesla already in tslaspcx seen -> skip
    st = {"sec_seen": [], "tsla_pending": []}
    hits, fb, mark, log = sec_eval(cfg, st, subs_of(filing("8-K", "2026-11-20", "0001181412-26-000201", "8.01", "m.htm")),
                                   today, fetch, {"seen": ["0001181412-26-000201"]})
    chk("tesla in tslaspcx seen: no send, no pending", (hits, fb, st["tsla_pending"], "0001181412-26-000201" in mark), ([], [], [], True))

    # tesla not in seen -> pending, no send
    st = {"sec_seen": [], "tsla_pending": []}
    hits, fb, mark, log = sec_eval(cfg, st, subs_of(filing("8-K", "2026-11-20", "0001181412-26-000201", "8.01", "m.htm")),
                                   today, fetch, {"seen": []})
    chk("tesla pending no send", (hits, fb, [p["acc"] for p in st["tsla_pending"]]), ([], [], ["0001181412-26-000201"]))

    # same day again: still pending
    hits, fb, mark, log = sec_eval(cfg, st, subs_of(filing("8-K", "2026-11-20", "0001181412-26-000201", "8.01", "m.htm")),
                                   today, fetch, {"seen": []})
    chk("same-day pending stays, acc now seen so no dup append", (hits, fb, len(st["tsla_pending"])), ([], [], 1))

    # 1 day later, still missing from tslaspcx -> fallback
    hits, fb, mark, log = sec_eval(cfg, st, {"filings": {"recent": {"form": [], "filingDate": [], "accessionNumber": [], "items": [], "primaryDocument": []}}},
                                   date(2026, 11, 21), fetch, {"seen": []})
    chk("1-day fallback fires", (hits, [x["label"] for x in fb], st["tsla_pending"]), ([], ["8-K TSLA-SPCX 미처리 대체 발송"], []))
    tf, bf, pf = sec_message(cfg, fb, stamp, fallback=True)
    chk("fallback title", "TSLA-SPCX 미처리 대체 발송" in tf and "[SPCX-PX]" in tf and "SPCX" in tf, True)
    chk("fallback p4 no buy wording", (pf, [w for w in BUY_WORDS if w in tf + bf]), (4, []))

    # ops fail fallback without waiting a day
    st = {"sec_seen": ["0001181412-26-000201"], "tsla_pending": [{"acc": "0001181412-26-000201", "filed": "2026-11-20",
          "date": "2026-11-20", "form": "8-K", "url": "u", "label": "8-K 테슬라 합병", "detail": "d", "items": "8.01"}]}
    hits, fb, mark, log = sec_eval(cfg, st, {"filings": {"recent": {"form": [], "filingDate": [], "accessionNumber": [], "items": [], "primaryDocument": []}}},
                                   today, fetch, {"seen": [], "fail_runs": {"sec_spacex": 3}, "fail_notice": {"sec_spacex": "2026-11-20"}})
    chk("ops-fail fallback same day", (not hits and len(fb) == 1 and not st["tsla_pending"]), True)

    # tslaspcx later processed -> drop pending silently
    st = {"sec_seen": ["0001181412-26-000201"], "tsla_pending": [{"acc": "0001181412-26-000201", "filed": "2026-11-20",
          "date": "2026-11-20", "form": "8-K", "url": "u", "label": "8-K 테슬라 합병", "detail": "d", "items": "8.01"}]}
    hits, fb, mark, log = sec_eval(cfg, st, {"filings": {"recent": {"form": [], "filingDate": [], "accessionNumber": [], "items": [], "primaryDocument": []}}},
                                   date(2026, 11, 22), fetch, {"seen": ["0001181412-26-000201"]})
    chk("processed pending dropped, no fallback", (hits, fb, st["tsla_pending"]), ([], [], []))

    # anthropic + tesla still sends anthropic
    st = {"sec_seen": [], "tsla_pending": []}
    hits, fb, mark, log = sec_eval(cfg, st, subs_of(filing("8-K", "2026-11-20", "0001181412-26-000202", "1.02,8.01", "both.htm")),
                                   today, fetch, {"seen": []})
    chk("risk+tesla sends, not pending", (len(hits) == 1 and "anthropic" in hits[0]["cats"] and not fb and not st["tsla_pending"]), True)

    # first-run seed silent
    st = {}
    hits, fb, mark, log = sec_eval(cfg, st, subs_of(filing("8-K", "2026-11-19", "old-1", "1.02", "term.htm")),
                                   today, fetch, {"seen": []})
    chk("seed silent", (hits, fb, mark), ([], [], ["old-1"]))

    # --- run() price+ops ---
    lines_by_key = {x["key"]: x for x in cfg["lines"]}
    io = {
        "state": {"armed": {"dn135": True, "dn120": True, "up165": True}, "last_date": "2026-11-18",
                  "last_close": ["2026-11-18", 143.5], "sec_seen": ["old"], "tsla_pending": []},
        "rows": lambda: [("2026-11-18", 143.5), ("2026-11-19", 134.0)],
        "subs": {"filings": {"recent": {"form": [], "filingDate": [], "accessionNumber": [], "items": [], "primaryDocument": []}}},
        "fetch": fetch,
        "tsla_state": {"seen": []},
    }
    out, st = run("dry", datetime(2026, 11, 20, 9, 0, tzinfo=KST), cfg, io)
    chk("run drop to 134 sends dn135 only", [o[0] for o in out],
        ["[SPCX-PX] SPCX 종가 $134.00 | 공모가 관심 구간 점검"])
    chk("ticker in run title", all("SPCX" in o[0] and o[0].startswith("[SPCX-PX]") for o in out), True)

    # fetch fail 3 times -> ops routing
    bad = {
        "state": {"armed": {"dn135": True, "dn120": True, "up165": True}, "last_date": "2026-11-18", "sec_seen": ["x"],
                  "fail_streak": {"SPCX 종가": 2, "SEC 공시": 2}},
        "rows": lambda: (_ for _ in ()).throw(OSError("yahoo down")),
        "subs": lambda: (_ for _ in ()).throw(OSError("edgar down")),
        "fetch": fetch,
        "tsla_state": {},
    }
    out, st = run("dry", datetime(2026, 11, 20, 9, 0, tzinfo=KST), cfg, bad)
    chk("3rd fail sends ops notice", len(out) == 1 and "조회 실패" in out[0][0] and "SPCX" in out[0][0], True)
    chk("ops title routes to cwally-ops", route_topic("cwally-port", out[0][0]), "cwally-ops")
    out2, st2 = run("dry", datetime(2026, 11, 20, 10, 0, tzinfo=KST), cfg, {**bad, "state": st})
    chk("ops notice once per day", out2, [])

    # no middle dot / em-dash in generated texts
    samples = [t, b, t1, b1, t2, b2, t3, b3, t4, b4, t165, b165, tf, bf, out[0][0] + out[0][1]]
    ct, cb, _ = confirm_message(cfg, stamp)
    samples.extend([ct, cb])
    chk("no middle dot / em-dash", any(ch in "".join(samples) for ch in DOTS), False)
    chk("confirm title format", ct, "[SPCX-PX] SPCX 관심 종목 알림 설치 확인 | 매매 신호 아님")
    chk("config has no dots", any(ch in json.dumps(cfg, ensure_ascii=False) for ch in DOTS), False)

    print("selftest", "PASS" if not sum(fails) else f"FAIL {sum(fails)}")
    return 1 if sum(fails) else 0


def confirm(cfg: dict) -> int:
    stamp = datetime.now(KST).strftime("%Y-%m-%d %H:%M KST")
    title, body, pri = confirm_message(cfg, stamp)
    print(title)
    print(body)
    publish(resolve_topic("port", cfg.get("ntfy_topic")), title, body, pri, ["white_check_mark"],
            resolve_server(cfg.get("ntfy_server")), config_name=NAME)
    return 0


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    if mode == "selftest":
        return selftest()
    cfg = load_cfg()
    if mode == "confirm":
        return confirm(cfg)
    if mode not in {"auto", "dry"}:
        print("usage: spcxpx_monitor.py <auto|dry> | selftest | confirm", file=sys.stderr)
        return 2
    run(mode, datetime.now(KST), cfg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
