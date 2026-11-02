#!/usr/bin/env python3
"""[MEMWATCH] 하이퍼스케일러 AI capex 가이던스 하향 감시 (MSFT, GOOGL, META, AMZN). 매매 신호 아님, 자동 주문 아님.

[MEMWATCH] 확장 (사용자 승인 2026-10-31 t61u). 매일 07:30 KST, 기존 tslaspcx 흐름의 한 단계
(cron-job.org 8711128 dispatch {job:tslaspcx, oneoff_mode:auto} + GitHub 30 22 * * * 백업, 주말 포함).
1) SEC EDGAR submissions 에서 본 적 없는 8-K(/A) 를 받고, 주문서와 Ex.99.1(회사 IR 보도자료) 본문에서
   capex 가이던스 문장(expect/anticipate/outlook + capital expenditures + $..billion)을 찾음.
2) 회사별 기간 키(FY2026, Q4 2026, Q2 FY2027, CY2026)별 중간값을 state 에 저장하고 같은 키끼리 비교.
   중간값 5% 이상 하향 -> p4. 같은 실적 시즌(공시일 달력 분기)에 2개사 이상 하향 -> p5, 본문에
   삼성전자/SK하이닉스 '추가 매수 보류' 재적용과 1차/2차 분할매수 멈춤.
   숫자 하향 없이 속도 조절 표현만 -> p3 관찰. 공시 1건당 1번 (접수번호 seen 으로 중복 방지).
3) 상향, 유지, 5% 미만 하향, 새 기간 첫 가이던스는 저장만 (발송 없음).
조회 실패는 회사별 3번 연속이면 cwally-ops 하루 1번, 스크립트 오류도 cwally-ops 하루 1번.
2027-02-28 23:59 KST 뒤에는 판정 안 함.
modes: auto | dry(발송 없음, state 안 씀) | selftest(가짜 문장, 네트워크 없음) | seed(빌드 때 1회: 지금까지 접수번호 + 10월 가이던스 저장, 발송 없음)
"""
from __future__ import annotations

import html
import json
import re
import sys
import time
import traceback
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, route_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
NAME = "capex"
CFG_PATH = ROOT / "configs" / f"{NAME}.json"
STATE_PATH = ROOT / "state" / f"{NAME}.json"
END = datetime(2027, 2, 28, 23, 59, tzinfo=KST)
MAX_QUOTE = 320
OPS = "cwally-ops"
PORT = "cwally-port"


# ---------------- io ----------------
def load_cfg() -> dict:
    return json.loads(CFG_PATH.read_text(encoding="utf-8"))


def load_state() -> dict:
    base = {"seeded": False, "seen": {}, "guidance": {}, "cuts": {}, "fail_runs": {}, "fail_notice": {}, "sent": []}
    if STATE_PATH.exists():
        try:
            base.update(json.loads(STATE_PATH.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass
    return base


def save_state(st: dict, now: datetime) -> None:
    st["updated_at"] = now.isoformat(timespec="seconds")
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def get(url: str, ua: str, timeout: int = 30, limit: int | None = None) -> str:
    last: Exception | None = None
    for attempt in (1, 2):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept-Encoding": "identity"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read(limit) if limit else resp.read()
                return raw.decode("utf-8", errors="replace")
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt == 1:
                time.sleep(2)
    raise last  # type: ignore[misc]


def clean(s: str) -> str:
    """Korean alert text rule: no middle dot, no em-dash."""
    return s.replace("\u2014", "-").replace("\u2013", "-").replace("\u00b7", " ").replace("\u2022", " ")


def strip_html(t: str) -> str:
    t = re.sub(r"(?is)<(script|style|ix:header)\b.*?</\1>", " ", t)
    t = re.sub(r"(?s)<[^>]+>", " ", t)
    t = html.unescape(t).replace("\xa0", " ")
    return re.sub(r"\s+", " ", t)


# ---------------- SEC ----------------
def sec_recent(cik: str, ua: str, forms: list[str]) -> list[dict]:
    d = json.loads(get(f"https://data.sec.gov/submissions/CIK{cik}.json", ua))
    r = d["filings"]["recent"]
    out = []
    for i in range(len(r["accessionNumber"])):
        if r["form"][i] not in forms:
            continue
        out.append({"acc": r["accessionNumber"][i], "form": r["form"][i], "filed": r["filingDate"][i],
                    "accepted": r["acceptanceDateTime"][i], "primary": r["primaryDocument"][i],
                    "items": (r.get("items") or [""] * (i + 1))[i]})
    return out


def filing_url(cik: str, acc: str, doc: str = "") -> str:
    return f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc.replace('-', '')}/{doc}"


def filing_texts(cik: str, f: dict, cfg: dict) -> list[tuple[str, str]]:
    """주문서 + Ex.99 문서(보도자료). 그 밖의 첨부(계약서 등)는 읽지 않음."""
    ua = cfg["sec"]["user_agent"]
    names = [f["primary"]] if f.get("primary") else []
    try:
        idx = json.loads(get(filing_url(cik, f["acc"], "index.json"), ua))
        for it in idx["directory"]["item"]:
            n = it.get("name", "")
            low = n.lower()
            if n not in names and low.endswith((".htm", ".html", ".txt")) and re.search(r"ex[-_]?99|exhibit99|ex991|dex99", low):
                names.append(n)
    except Exception as exc:  # noqa: BLE001
        if not names:
            raise RuntimeError(f"index.json {type(exc).__name__}")
    out = []
    for n in names[: int(cfg["sec"]["max_docs_per_filing"])]:
        if "/" in n:
            continue
        raw = get(filing_url(cik, f["acc"], n), ua, 45, int(cfg["sec"]["max_doc_bytes"]))
        out.append((n, strip_html(raw) if n.lower().endswith((".htm", ".html")) else re.sub(r"\s+", " ", raw)))
    if not out:
        raise RuntimeError("문서 없음")
    return out


# ---------------- parsing ----------------
NUM = r"(\d{1,4}(?:\.\d+)?)"
RANGE_RE = re.compile(r"(?:between\s+)?\$\s?" + NUM + r"\s*(?:billion\s*)?(?:-|\u2013|\u2014|to|and)\s*\$?\s?" + NUM + r"\s*billion", re.I)
SINGLE_RE = re.compile(r"(approximately|about|around|roughly|over|more than|at least|in excess of|exceed\w*|above|up to)?\s*\$\s?" + NUM + r"\s*billion", re.I)
PRIOR_RE = re.compile(r"(?:,\s*)?(?:narrowed|lowered|reduced|raised|increased|updated|down|up|compared|versus|vs\.?)?\s*(?:from|with|to)?\s*(?:our|the)?\s*(?:prior|previous|earlier)\s+(?:outlook|guidance|range|expectations?|forecast)\s+(?:of\s+)?(?:\$[^.;]*?billion)", re.I)
GUIDE_RE = re.compile(r"(?i)\b(expect\w*|anticipat\w*|outlook|guidance|forecast\w*|plan\w* to|project\w*)\b")
QUARTER_RE = re.compile(r"(?i)\b(first|second|third|fourth)\s+quarter(?:\s+of)?(?:\s+fiscal(?:\s+year)?)?\s+(20\d\d)\b|\bQ([1-4])\s*(?:FY\s?)?(20\d\d)\b")
FISCAL_RE = re.compile(r"(?i)\bfiscal(?:\s+year)?\s+(20\d\d)\b|\bFY\s?(20\d\d)\b")
YEAR_RE = re.compile(r"\b(20\d\d)\b")
QN = {"first": 1, "second": 2, "third": 3, "fourth": 4}


def sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z\u2022])|\s\u2022\s|\s\u25cf\s", text)
    return [p.strip() for p in parts if 20 <= len(p.strip()) <= 1200]


def amount(s: str) -> tuple[float, float, float] | None:
    m = RANGE_RE.search(s)
    if m:
        lo, hi = float(m.group(1)), float(m.group(2))
        if lo <= hi:
            return lo, hi, round((lo + hi) / 2, 3)
    m = SINGLE_RE.search(s)
    if m:
        v = float(m.group(2))
        return v, v, v
    return None


def period_key(s: str) -> str | None:
    q = QUARTER_RE.search(s)
    if q:
        n = QN[q.group(1).lower()] if q.group(1) else int(q.group(3))
        yr = q.group(2) or q.group(4)
        return f"Q{n} FY{yr}" if re.search(r"(?i)fiscal|\bFY", q.group(0)) else f"Q{n} {yr}"
    f = FISCAL_RE.search(s)
    if f:
        return f"FY{f.group(1) or f.group(2)}"
    if re.search(r"(?i)calendar year", s):
        y = YEAR_RE.search(s)
        return f"CY{y.group(1)}" if y else None
    y = YEAR_RE.search(s)
    return f"FY{y.group(1)}" if y else None


def parse_guidance(text: str, cfg: dict) -> tuple[list[dict], list[str]]:
    """(가이던스 목록, 속도 조절 표현 문장). 한 기간 키는 첫 문장만."""
    cap = re.compile(cfg["rules"]["capex_re"])
    word = re.compile(cfg["rules"]["wording_re"])
    out, keys, wording = [], set(), []
    for s in sentences(text):
        if not cap.search(s):
            continue
        if word.search(s):
            wording.append(s)
        if not GUIDE_RE.search(s) or not re.search(r"(?i)billion", s):
            continue
        prior = None
        pm = PRIOR_RE.search(s)
        main = s
        if pm:
            prior = amount(pm.group(0))
            main = s[: pm.start()] + s[pm.end():]
        a = amount(main)
        k = period_key(main)
        if not a or not k or k in keys:
            continue
        keys.add(k)
        out.append({"key": k, "lo": a[0], "hi": a[1], "mid": a[2], "text": s[:600],
                    "prior_mid": prior[2] if prior else None})
    return out, wording


def season_of(filed: str) -> str:
    y, m = int(filed[:4]), int(filed[5:7])
    return f"{y}-Q{(m - 1) // 3 + 1}"


def evaluate(tk: str, f: dict, guides: list[dict], wording: list[str], st: dict, cfg: dict) -> dict:
    """state 갱신(guidance, cuts) + 이 공시의 이벤트. 반환 {cuts:[...], wording:[...], stored:[...]}"""
    pct_lim = float(cfg["rules"]["cut_pct"])
    g = st["guidance"].setdefault(tk, {})
    cuts, stored = [], []
    for x in guides:
        old = g.get(x["key"])
        base = old["mid"] if old else x.get("prior_mid")
        base_src = "저장값" if old else ("본문 직전 가이던스" if x.get("prior_mid") else None)
        if base:
            pct = (x["mid"] - base) / base * 100
            if pct <= -pct_lim:
                cuts.append(dict(x, base=base, base_src=base_src, pct=round(pct, 2)))
        g[x["key"]] = {"mid": x["mid"], "lo": x["lo"], "hi": x["hi"], "text": x["text"][:400],
                       "src": f"8-K {f['acc']} ({f['filed']})", "acc": f["acc"]}
        stored.append(x["key"])
    if cuts:
        season = season_of(f["filed"])
        st["cuts"].setdefault(season, {})[tk] = {"acc": f["acc"], "filed": f["filed"],
                                                "pct": min(c["pct"] for c in cuts), "key": cuts[0]["key"]}
    return {"cuts": cuts, "wording": [] if cuts else wording, "stored": stored}


# ---------------- messages ----------------
def q(s: str) -> str:
    s = clean(s.strip())
    return s if len(s) <= MAX_QUOTE else s[:MAX_QUOTE].rstrip() + "..."


def body(lines: list[str]) -> str:
    return clean("\n".join(lines))


def cut_message(now: datetime, comp: dict, f: dict, ev: dict, st: dict, cfg: dict, link: str) -> tuple[str, str, int]:
    tk = comp["ticker"]
    season = season_of(f["filed"])
    season_cuts = st["cuts"].get(season, {})
    multi = len(season_cuts) >= int(cfg["rules"]["multi_min_companies"])
    c = ev["cuts"][0]
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    detail = "; ".join(f"{x['key']} 중간값 ${x['base']:g}B -> ${x['mid']:g}B ({x['pct']:+.1f}%, 기준 {x['base_src']})" for x in ev["cuts"])
    if multi:
        tks = sorted(season_cuts, key=lambda k: season_cuts[k]["filed"])
        title = f"[MEMWATCH] {'/'.join(tks)} capex 가이던스 동시 하향 | 추가 매수 보류 재적용"
        prio = int(cfg["rules"]["p_multi"])
        others = ", ".join(f"{k} {v['pct']:+.1f}% ({v['key']}, {v['filed']})" for k, v in season_cuts.items())
        lines = [
            f"{stamp} | MEMWATCH | 하이퍼스케일러 capex 가이던스 동시 하향 ({season} 시즌)",
            f"이게 뭐 알림: 같은 실적 시즌에 {len(season_cuts)}개사가 AI capex 가이던스 중간값을 {cfg['rules']['cut_pct']:g}% 이상 내렸다는 알림. 메모리 수요 근거가 약해졌다는 신호이지 주문이 아닙니다.",
            f"지금 결과/상태: 이번 공시 {tk} {detail}. 이번 시즌 하향: {others}. 원문: \"{q(c['text'])}\" 공시: {link}",
            "지금 할 일: [MEMWATCH] 삼성전자/SK하이닉스 '추가 매수 보류' 규칙을 다시 적용합니다. 1차/2차 분할매수는 멈춥니다.",
            "나중에: 다음 실적 시즌에 가이던스가 다시 오르거나, 실적 콜에서 수요 확인 발언이 나오면 보류 해제를 검토하세요.",
            "익절/줄일 때: 비중이 계획보다 크면 개별 종목 계획(005930, 000660) 줄일 조건을 먼저 보세요.",
            "하지 말 것: 하향 직후 급락만 보고 물타기, 신용/미수.",
        ]
    else:
        title = f"[MEMWATCH] {tk} capex 가이던스 {abs(c['pct']):.1f}% 하향 | 추가 매수 보류 점검"
        prio = int(cfg["rules"]["p_cut"])
        lines = [
            f"{stamp} | MEMWATCH | {comp['name']}({tk}) capex 가이던스 하향",
            f"이게 뭐 알림: {tk} 가 AI capex 가이던스 중간값을 {cfg['rules']['cut_pct']:g}% 이상 내렸다는 알림. 한 회사만이면 관찰, 같은 시즌 2개사 이상이면 추가 매수 보류(p5)입니다.",
            f"지금 결과/상태: {detail}. 원문: \"{q(c['text'])}\" 공시: {link}",
            "지금 할 일: 삼성전자/SK하이닉스 새 분할매수는 다음 하이퍼스케일러 공시를 볼 때까지 서두르지 마세요.",
            f"나중에: 같은 시즌({season})에 다른 회사도 5% 이상 내리면 p5 로 '추가 매수 보류' 재적용 알림이 옵니다.",
            "익절/줄일 때: 이 알림만으로 팔지 말고 개별 종목 계획을 따르세요.",
            "하지 말 것: 한 회사 하향만 보고 메모리 업황 꺾임을 단정하기.",
        ]
    return title, body(lines), prio


def wording_message(now: datetime, comp: dict, f: dict, ev: dict, cfg: dict, link: str) -> tuple[str, str, int]:
    tk = comp["ticker"]
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    quotes = " / ".join(f"\"{q(s)}\"" for s in ev["wording"][:2])
    lines = [
        f"{stamp} | MEMWATCH | {comp['name']}({tk}) capex 속도 조절 표현",
        f"이게 뭐 알림: {tk} 공시에 숫자 하향 없이 투자 속도 조절(pace, moderate, slow 등) 표현이 나왔다는 관찰 알림. 매매 신호가 아닙니다.",
        f"지금 결과/상태: {quotes} 공시: {link}",
        "지금 할 일: 실적 콜에서 숫자 가이던스가 바뀌었는지 확인만.",
        "나중에: 다음 공시에서 중간값 5% 이상 하향이 나오면 p4, 같은 시즌 2개사면 p5 가 옵니다.",
        "익절/줄일 때: 이 알림만으로 매매하지 말고 개별 종목 계획을 따르세요.",
        "하지 말 것: 표현 하나로 하향을 단정하기.",
    ]
    return f"[MEMWATCH] {tk} capex 속도 조절 표현 | 관찰", body(lines), int(cfg["rules"]["p_wording"])


# ---------------- run ----------------
def send(mode: str, topic: str, title: str, msg: str, prio: int, cfg: dict, tags: list[str]) -> None:
    assert "확인 불가" not in title and "조회 실패" not in title or topic == OPS, title
    print(f"[{mode}] -> {route_topic(topic, title)} p{prio}\nTITLE: {title}\n{msg}\n")
    if mode == "auto":
        publish(topic, title, msg, prio, tags, resolve_server(cfg.get("ntfy_server")), config_name=NAME)


def run(mode: str, now: datetime, cfg: dict) -> int:
    if now > END:
        print(f"{NAME}: ended {END:%Y-%m-%d %H:%M} KST; no evaluation")
        return 0
    st = load_state()
    if not st.get("seeded"):
        print(f"{NAME}: state not seeded; run 'seed' first (no evaluation, nothing sent)")
        return 0
    ua = cfg["sec"]["user_agent"]
    today = now.strftime("%Y-%m-%d")
    for comp in cfg["companies"]:
        tk, cik = comp["ticker"], comp["cik"]
        seen = st["seen"].setdefault(tk, [])
        try:
            filings = sec_recent(cik, ua, cfg["sec"]["forms"])
            new = sorted((f for f in filings if f["acc"] not in seen), key=lambda f: f["accepted"])
            print(f"{tk}: 8-K {len(filings)}건 중 새 공시 {len(new)}건")
            for f in new:
                texts = filing_texts(cik, f, cfg)
                guides, wording = [], []
                for _, tx in texts:
                    g, w = parse_guidance(tx, cfg)
                    guides += [x for x in g if x["key"] not in {y["key"] for y in guides}]
                    wording += w
                ev = evaluate(tk, f, guides, wording, st, cfg)
                link = filing_url(cik, f["acc"], f"{f['acc']}-index.htm")
                print(f"  {f['acc']} {f['filed']} items={f['items']} 가이던스 {[(x['key'], x['mid']) for x in guides]} 하향 {len(ev['cuts'])} 표현 {len(ev['wording'])}")
                if ev["cuts"]:
                    t, m, p = cut_message(now, comp, f, ev, st, cfg, link)
                    send(mode, PORT, t, m, p, cfg, ["warning"])
                elif ev["wording"]:
                    t, m, p = wording_message(now, comp, f, ev, cfg, link)
                    send(mode, PORT, t, m, p, cfg, ["eyes"])
                if ev["cuts"] or ev["wording"]:
                    st["sent"] = (st.get("sent") or [])[-30:] + [{"at": now.isoformat(timespec="seconds"), "tk": tk, "acc": f["acc"]}]
                seen.append(f["acc"])
            st["seen"][tk] = seen[-200:]
            st["fail_runs"][tk] = 0
        except Exception as exc:  # noqa: BLE001
            st["fail_runs"][tk] = int(st["fail_runs"].get(tk, 0)) + 1
            print(f"{tk}: SEC 조회 실패 {st['fail_runs'][tk]}회 연속: {type(exc).__name__}: {exc}")
            if st["fail_runs"][tk] >= int(cfg["sec"]["ops_fail_after_runs"]) and st["fail_notice"].get(tk) != today:
                send(mode, OPS, f"[MEMWATCH] {tk} capex SEC 조회 실패 {st['fail_runs'][tk]}회 연속 | 판정 보류",
                     body([f"{now:%Y-%m-%d %H:%M KST} | MEMWATCH capex | {tk} SEC EDGAR 조회 실패",
                           f"이게 뭐 알림: 운영 알림. 매매 신호 아님. 사유: {type(exc).__name__}: {str(exc)[:200]}",
                           "지금 할 일: 없음. 다음 07:30 실행에서 같은 공시를 다시 봅니다 (본 것으로 표시 안 함)."]), 3, cfg, ["warning"])
                st["fail_notice"][tk] = today
    if mode == "auto":
        save_state(st, now)
    else:
        print("DRY: ntfy/state 변경 없음")
    return 0


def seed(now: datetime, cfg: dict) -> int:
    st = load_state()
    if st.get("seeded"):
        print("already seeded; nothing changed")
        return 0
    ua = cfg["sec"]["user_agent"]
    for comp in cfg["companies"]:
        tk = comp["ticker"]
        filings = sec_recent(comp["cik"], ua, cfg["sec"]["forms"])
        st["seen"][tk] = [f["acc"] for f in filings][:200]  # newest first
        st["guidance"][tk] = {k: dict(v) for k, v in cfg["seed"][tk].items()}
        print(f"{tk}: seen {len(st['seen'][tk])} accessions, guidance {st['guidance'][tk]}")
    st["seeded"] = True
    st["seeded_at"] = now.isoformat(timespec="seconds")
    st["seed_note"] = "2026-10-31 빌드: 지금까지의 8-K 접수번호는 판정 안 함. 가이던스는 configs/capex.json seed (10월 실적 뒤)."
    save_state(st, now)
    return 0


def selftest(cfg: dict) -> int:
    meta = ("CFO Outlook Commentary. We anticipate 2027 capital expenditures, including principal payments on finance leases, "
            "to be in the range of $120-130 billion, lowered from our prior outlook of $135-145 billion. Other text here.")
    g, w = parse_guidance(meta, cfg)
    assert g and g[0]["key"] == "FY2027" and g[0]["mid"] == 125 and g[0]["prior_mid"] == 140, g
    amzn = "Guidance. \u2022 We continue to expect 2026 cash capital expenditures to be approximately $220 billion. \u2022 This guidance assumes"
    g, _ = parse_guidance(amzn, cfg)
    assert g and g[0]["key"] == "FY2026" and g[0]["mid"] == 220, g
    goog = "We expect full year 2026 capital expenditures between $195 and $205 billion."
    assert parse_guidance(goog, cfg)[0][0]["mid"] == 200
    msq = "In the second quarter of fiscal year 2027, we expect capital expenditures to be over $50 billion."
    assert parse_guidance(msq, cfg)[0][0]["key"] == "Q2 FY2027"
    hist = "Capital expenditures were $51.4 billion in the third quarter of 2026."
    assert parse_guidance(hist, cfg)[0] == []  # 과거 실적 문장은 가이던스 아님
    soft = "We plan to moderate the pace of capital expenditures in 2027 as we digest capacity."
    g, w = parse_guidance(soft, cfg)
    assert g == [] and w, (g, w)

    now = datetime(2027, 1, 29, 7, 30, tzinfo=KST)
    st = {"guidance": {"META": {"FY2026": {"mid": 140.0}}, "AMZN": {"FY2026": {"mid": 220.0}}}, "cuts": {}}
    f1 = {"acc": "0000000000-27-000001", "filed": "2027-01-28"}
    ev = evaluate("META", f1, [{"key": "FY2026", "lo": 125, "hi": 135, "mid": 130.0, "text": "x", "prior_mid": None}], [], st, cfg)
    assert ev["cuts"] and ev["cuts"][0]["pct"] == -7.14 and st["guidance"]["META"]["FY2026"]["mid"] == 130.0
    comp = {"ticker": "META", "name": "Meta Platforms"}
    t, m, p = cut_message(now, comp, f1, ev, st, cfg, "https://example")
    assert t == "[MEMWATCH] META capex 가이던스 7.1% 하향 | 추가 매수 보류 점검" and p == 4, (t, p)
    small = evaluate("AMZN", f1, [{"key": "FY2026", "lo": 212, "hi": 212, "mid": 212.0, "text": "x", "prior_mid": None}], [], st, cfg)
    assert small["cuts"] == [] and st["guidance"]["AMZN"]["FY2026"]["mid"] == 212.0  # -3.6%: 저장만
    f2 = {"acc": "0000000000-27-000002", "filed": "2027-02-05"}
    ev2 = evaluate("AMZN", f2, [{"key": "FY2026", "lo": 190, "hi": 190, "mid": 190.0, "text": "y", "prior_mid": None}], [], st, cfg)
    t2, m2, p2 = cut_message(now, {"ticker": "AMZN", "name": "Amazon"}, f2, ev2, st, cfg, "https://example")
    assert t2 == "[MEMWATCH] META/AMZN capex 가이던스 동시 하향 | 추가 매수 보류 재적용" and p2 == 5, (t2, p2)
    assert "추가 매수 보류' 규칙을 다시 적용" in m2 and "1차/2차 분할매수는 멈춥니다" in m2
    newkey = evaluate("META", f2, [{"key": "FY2027", "lo": 150, "hi": 160, "mid": 155.0, "text": "z", "prior_mid": None}], [], st, cfg)
    assert newkey["cuts"] == []  # 새 기간 첫 가이던스: 저장만
    st2 = {"guidance": {}, "cuts": {}}
    wv = evaluate("GOOGL", f1, [], [soft], st2, cfg)
    t3, m3, p3 = wording_message(now, {"ticker": "GOOGL", "name": "Alphabet"}, f1, wv, cfg, "https://example")
    assert t3 == "[MEMWATCH] GOOGL capex 속도 조절 표현 | 관찰" and p3 == 3
    for tt, mm in ((t, m), (t2, m2), (t3, m3)):
        assert "\u00b7" not in tt + mm and "\u2014" not in tt + mm, tt
        assert "확인 불가" not in tt and "조회 실패" not in tt and route_topic(PORT, tt) == PORT, tt
        assert [x.split(":")[0] for x in mm.split("\n")[1:7]] == ["이게 뭐 알림", "지금 결과/상태", "지금 할 일", "나중에", "익절/줄일 때", "하지 말 것"], mm
    assert route_topic(PORT, "[MEMWATCH] MSFT capex SEC 조회 실패 3회 연속 | 판정 보류") == OPS
    assert season_of("2027-01-28") == season_of("2027-02-05") == "2027-Q1" and season_of("2027-04-25") == "2027-Q2"
    for tt, mm, pp in ((t, m, p), (t2, m2, p2), (t3, m3, p3)):
        print(f"-- sample (가짜 숫자)\nTITLE: {tt}\nPRIORITY: {pp}\n{mm}\n")
    print("SELFTEST OK: no network, no publish, no state")
    return 0


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "dry"
    cfg = load_cfg()
    now = datetime.now(KST)
    if mode == "selftest":
        return selftest(cfg)
    if mode == "seed":
        return seed(now, cfg)
    if mode not in ("auto", "dry"):
        raise SystemExit(f"unknown mode {mode}")
    try:
        return run(mode, now, cfg)
    except Exception as exc:  # noqa: BLE001  스크립트 오류 -> cwally-ops 하루 1번
        traceback.print_exc()
        if mode == "auto":
            st = load_state()
            today = now.strftime("%Y-%m-%d")
            if st["fail_notice"].get("_script") != today:
                publish(OPS, "[MEMWATCH] MSFT/GOOGL/META/AMZN capex 스크립트 오류 | 이번 실행 판정 못 함",
                        clean(f"{now:%Y-%m-%d %H:%M KST} | MEMWATCH capex 스크립트 오류: {type(exc).__name__}: {str(exc)[:300]}"),
                        3, ["warning"], resolve_server(cfg.get("ntfy_server")), config_name=NAME)
                st["fail_notice"]["_script"] = today
                save_state(st, now)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
