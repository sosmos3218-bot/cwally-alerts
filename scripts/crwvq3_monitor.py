#!/usr/bin/env python3
"""CoreWeave Q3 2026 backlog/RPO + OCF/debt/power/maturity + HBM 2027 LTA news one-shot. Existing dispatcher slots only.

Silent until the Q3 earnings 8-K (item 2.02) shows up on SEC EDGAR, then sends one summary.
The last slot (window_end) sends one summary even if results are still missing.
"""
from __future__ import annotations

import json
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
CFG_PATH = ROOT / "configs" / "crwvq3.json"
STATE_PATH = ROOT / "state" / "crwvq3.json"
SEC_UA = {"User-Agent": "cwally-alerts research sosmos3218@gmail.com", "Accept": "text/html,application/json"}
WEB_UA = {"User-Agent": "Mozilla/5.0 cwally-alerts"}
FILED_FROM = "2026-11-01"
DRY = False


def load_json(path: Path, fallback: dict) -> dict:
    if not path.exists():
        return dict(fallback)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return dict(fallback)
    out = dict(fallback)
    out.update(data)
    return out


def save_state(state: dict, now: datetime) -> None:
    if DRY:
        return
    state["updated_at"] = now.isoformat()
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def emit(cfg: dict, title: str, lines: list[str], priority: int = 4) -> None:
    print(title)
    print("\n".join(lines))
    if DRY:
        print("DRY RUN: not sent")
        return
    topic = resolve_topic("port", cfg.get("ntfy_topic"))
    server = resolve_server(cfg.get("ntfy_server"))
    publish(topic, title, "\n".join(lines), priority, ["bar_chart"], server)


def fetch(url: str, headers: dict) -> str:
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def strip_html(html: str) -> str:
    text = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", html)
    text = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", unescape(text))


def sec_docs(cik: str, filed_from: str) -> dict:
    """Return {'pr': url|None, 'tenq': url|None} for Q3 2026 filings filed on/after filed_from."""
    data = json.loads(fetch(f"https://data.sec.gov/submissions/CIK{int(cik):010d}.json", SEC_UA))
    r = data.get("filings", {}).get("recent", {})
    out = {"pr": None, "tenq": None, "pr_day": None}
    for form, day, acc, doc, items in zip(r.get("form", []), r.get("filingDate", []), r.get("accessionNumber", []), r.get("primaryDocument", []), r.get("items", [])):
        if day < filed_from:
            continue
        base = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc.replace('-', '')}"
        if form == "10-Q" and not out["tenq"]:
            out["tenq"] = f"{base}/{doc}"
        if form in {"8-K", "8-K/A"} and "2.02" in str(items) and not out["pr"]:
            idx = json.loads(fetch(f"{base}/index.json", SEC_UA))
            names = [i.get("name", "") for i in idx.get("directory", {}).get("item", [])]
            ex = [n for n in names if re.search(r"press|ex99|ex-99|earnings", n, re.I) and n.endswith(".htm")]
            out["pr"] = f"{base}/{ex[0] if ex else doc}"
            out["pr_day"] = day
    return out


def money_b(raw: str, unit: str) -> float:
    v = float(raw.replace(",", ""))
    return v / 1000 if unit.lower().startswith("m") else v


def parse_pr(text: str) -> dict:
    q3 = bool(re.search(r"third quarter (of )?2026|Q3 2026", text, re.I))
    m = re.search(r"Revenue backlog\s*\d?\s*(?:was|of|totaled|reached)\s*(?:approximately\s*)?\$\s*([0-9.,]+)\s*(billion|million)", text, re.I)
    rev = re.search(r"Revenue \$\s*([0-9,]+)\s", text)
    return {
        "q3": q3,
        "backlog_b": money_b(m.group(1), m.group(2)) if m else None,
        "revenue_m": float(rev.group(1).replace(",", "")) if rev else None,
    }


def parse_rpo(text: str) -> dict:
    m = re.search(
        r"had \$\s*([0-9.,]+)\s*(billion|million) of unsatisfied RPO, of which\s*([0-9.]+)\s*%\s*was expected to be recognized over the (?:initial|next)\s*([0-9]+)\s*months(.{0,220})",
        text,
        re.I,
    )
    if not m:
        return {}
    tail = m.group(5)
    m2 = re.search(r"([0-9.]+)\s*%\s*between months\s*([0-9]+)\s*and\s*([0-9]+)", tail, re.I)
    return {
        "rpo_b": money_b(m.group(1), m.group(2)),
        "near_pct": float(m.group(3)),
        "near_months": int(m.group(4)),
        "mid_pct": float(m2.group(1)) if m2 else None,
        "mid_span": f"{m2.group(2)}~{m2.group(3)}" if m2 else None,
    }


def parse_ocf_capex(text: str) -> dict:
    """Rough extract of operating cash flow and capital expenditures mentions."""
    ocf = None
    capex = None
    m_ocf = re.search(r"(?:operating cash flow|cash (?:from|provided by) operating activities)\s*(?:of|was|totaled)?\s*\$?\s*([0-9.,]+)\s*(billion|million)", text, re.I)
    if m_ocf:
        ocf = money_b(m_ocf.group(1), m_ocf.group(2))
    m_capex = re.search(r"(?:capital expenditures|purchases of property|capex)\s*(?:of|was|totaled)?\s*\$?\s*([0-9.,]+)\s*(billion|million)", text, re.I)
    if m_capex:
        capex = money_b(m_capex.group(1), m_capex.group(2))
    return {"ocf_b": ocf, "capex_b": capex}


def parse_debt_power(text: str) -> dict:
    """Extract mentions of new debt terms, power capacity, maturity notes."""
    debt_mentions = []
    for m in re.finditer(r"(?:issued|raised|priced|closed)\s*\$?\s*([0-9.,]+)\s*(billion|million)\s*(?:of\s*)?(?:convertible|senior|notes|bonds|loan|facility)", text, re.I):
        debt_mentions.append(m.group(0)[:120])
    power_live = None
    m_p = re.search(r"(?:live|active|operational)\s*(?:power|capacity|MW|GW)\s*(?:of|was|reached)?\s*([0-9.,]+)\s*(MW|GW)", text, re.I)
    if m_p:
        power_live = float(m_p.group(1))
        if m_p.group(2).upper() == "GW":
            power_live *= 1000
    maturity = []
    for m in re.finditer(r"(?:maturity|due|repayment|principal).{0,40}(2027|2030|2031|2032).{0,60}", text, re.I):
        maturity.append(m.group(0)[:100])
    return {
        "debt_mentions": debt_mentions[:3],
        "power_live_mw": power_live,
        "maturity_notes": maturity[:2],
    }


def simple_stress(ocf_b: float | None, near_rpo_b: float | None, debt_2027_b: float = 6.2) -> str:
    """Very rough stress note if numbers available. Not a full model."""
    if ocf_b is None and near_rpo_b is None:
        return "스트레스 계산 불가 (OCF/근기 RPO 미확인)"
    # Placeholder: assume near-term RPO converts over 2 years roughly evenly, ignore delays.
    annual_from_rpo = (near_rpo_b / 2) if near_rpo_b else 0
    cover = "커버 가능 추정" if (ocf_b or 0) + annual_from_rpo * 0.3 > debt_2027_b * 0.4 else "커버 타이트 또는 미확인"
    return f"간단 스트레스(2027 상환 ~{debt_2027_b}B$ 기준): {cover}. 실제는 지연·금리·선급 조건에 따라 크게 다름."


def hbm_lines(*texts: str) -> list[str]:
    out = []
    for t in texts:
        for s in re.split(r"(?<=[.!?])\s+", t or ""):
            if re.search(r"\bHBM\d?\w*\b|high[- ]bandwidth memory", s, re.I) and len(s) < 600:
                s = s.strip()[:160]
                if s not in out:
                    out.append(s)
    return out[:2]


def lta_news(cfg: dict, now: datetime) -> tuple[list[str], str]:
    picked, err = [], ""
    cutoff = now - timedelta(days=30)
    for url in cfg.get("news_rss") or []:
        try:
            root = ET.fromstring(fetch(url, WEB_UA))
        except Exception as exc:
            err = str(exc)
            continue
        for node in root.findall(".//item"):
            title = (node.findtext("title") or "").strip()
            try:
                pub = parsedate_to_datetime(node.findtext("pubDate") or "")
            except Exception:
                pub = None
            if pub and pub < cutoff:
                continue
            low = title.lower()
            if "hbm" not in low:
                continue
            if not re.search(r"삼성|하이닉스|마이크론|samsung|hynix|micron", low):
                continue
            if not re.search(r"lta|장기|long-term|2027|가격|물량|price|volume|contract|계약", low):
                continue
            day = pub.astimezone(KST).strftime("%m/%d") if pub else "날짜?"
            line = f"{day} {title[:110]}"
            if all(title[:40] not in p for p in picked):
                picked.append(line)
            if len(picked) >= 4:
                return picked, err
    return picked, err


def main() -> int:
    global DRY
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    DRY = mode == "dry"
    if DRY:
        print("DRY RUN: no ntfy, no state write")
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    today = now.strftime("%Y-%m-%d")
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    state = load_json(STATE_PATH, {"result_notified": False, "empty_notified": False, "fail_notified": False, "waited_for_10q": None, "updated_at": None})
    in_year = now.year == int(cfg["year_guard"])
    in_window = cfg["window_start"] <= today <= cfg["window_end"]
    last = today >= cfg["window_end"]
    print(f"{stamp} mode={mode} window={in_window} last={last}")
    if mode not in {"force", "dry"} and (not in_year or not in_window):
        print("outside 2026 window; silent")
        return 0
    if mode != "force" and (state.get("result_notified") or state.get("empty_notified")):
        print("already sent once; silent")
        return 0

    b = cfg["baselines"]
    try:
        docs = sec_docs(cfg["sec_cik"], FILED_FROM)
    except Exception as exc:
        print(f"sec fail: {exc}")
        if mode != "dry" and not state.get("fail_notified") and (last or mode == "force"):
            emit(cfg, "[CRWVQ3] 코어위브 3Q SEC 조회 실패 | 판정 유보", [
                f"{stamp} | CRWVQ3 | {cfg['event_label']}",
                "이게 뭐 알림: 코어위브 3분기 백로그/RPO·OCF·부채·전력·만기 요약입니다.",
                f"지금 상태: SEC 조회 실패. {exc}. 숫자를 추정하지 않음.",
                "지금 할 일: 코어위브 IR에서 실적 보도자료를 직접 확인하세요.",
                "나중에: 이 알림은 끝납니다.",
                "익절/줄일 때: 실패 알림으로 포지션을 바꾸지 마세요.",
                "하지 말 것: 조회 실패를 호재나 악재로 읽기.",
            ])
            state["fail_notified"] = True
        save_state(state, now)
        return 0

    print("sec docs", docs)
    pr = {}
    pr_text = tenq_text = ""
    if docs["pr"]:
        pr_text = strip_html(fetch(docs["pr"], SEC_UA))
        pr = parse_pr(pr_text)
    published = bool(pr.get("q3"))
    rpo = {}
    cash = {}
    debtp = {}
    if docs["tenq"]:
        try:
            tenq_text = strip_html(fetch(docs["tenq"], SEC_UA))
            rpo = parse_rpo(tenq_text)
            cash = parse_ocf_capex(tenq_text)
            debtp = parse_debt_power(tenq_text)
        except Exception as exc:
            print(f"10-Q fetch fail: {exc}")
    if not rpo and pr_text:
        rpo = parse_rpo(pr_text)
    if not cash and pr_text:
        cash = parse_ocf_capex(pr_text)
    if not debtp and pr_text:
        debtp = parse_debt_power(pr_text)
    print("parsed pr", pr, "rpo", rpo, "cash", cash, "debtp", debtp)

    if not published and not last and mode not in {"force", "dry"}:
        print("Q3 results not filed yet; silent")
        save_state(state, now)
        return 0
    if published and not rpo and not last and mode not in {"force", "dry"} and state.get("waited_for_10q") is None:
        state["waited_for_10q"] = today
        print("press release found but RPO conversion (10-Q) missing; wait one slot")
        save_state(state, now)
        return 0

    news, news_err = lta_news(cfg, now)
    hbm = hbm_lines(pr_text, tenq_text)

    if published:
        bk = pr.get("backlog_b")
        bk_txt = f"백로그 {bk:,.1f}B$ (2Q {b['q2_backlog_b']:.0f}B$, {bk - b['q2_backlog_b']:+.1f})" if bk else "백로그 숫자 미확인"
        if rpo:
            rpo_txt = f"RPO {rpo['rpo_b']:,.1f}B$ (2Q {b['q2_rpo_b']}B$). {rpo['near_months']}개월 내 매출 전환 {rpo['near_pct']:.0f}% (2Q 24개월 {b['q2_rpo_24m_pct']}%)"
            if rpo.get("mid_pct") is not None:
                rpo_txt += f", {rpo['mid_span']}개월 {rpo['mid_pct']:.0f}%"
        else:
            rpo_txt = "RPO/전환 일정 미확인 (10-Q 미제출 또는 문구 변경)"
        rev_txt = f"매출 {pr['revenue_m']:,.0f}M$" if pr.get("revenue_m") else "매출 미확인"
        ocf_txt = f"OCF {cash.get('ocf_b'):.1f}B$ / Capex {cash.get('capex_b'):.1f}B$" if cash.get("ocf_b") or cash.get("capex_b") else "OCF/캐펙스 미확인"
        debt_txt = "; ".join(debtp.get("debt_mentions") or []) or "신규 부채 조건 언급 없음"
        power_txt = f"가동 전력 약 {debtp.get('power_live_mw'):.0f}MW" if debtp.get("power_live_mw") else "전력 가동률 미확인"
        mat_txt = "; ".join(debtp.get("maturity_notes") or []) or "만기 스케줄 언급 없음"
        near_rpo = (rpo.get("rpo_b") * rpo.get("near_pct") / 100) if rpo and rpo.get("rpo_b") and rpo.get("near_pct") else None
        stress = simple_stress(cash.get("ocf_b"), near_rpo)
        near = rpo.get("near_pct") if rpo else None
        verdict = "전환 빨라짐" if near is not None and near > b["q2_rpo_24m_pct"] + 2 else ("전환 느려짐" if near is not None and near < b["q2_rpo_24m_pct"] - 2 else "전환 속도 비슷/미확인")
        title = f"[CRWVQ3] CRWV 코어위브 3Q 백로그 {bk:,.0f}B$ | {verdict} | 매매 신호 아님" if bk else f"[CRWVQ3] CRWV 코어위브 3Q 발표 | 백로그 미확인 | 매매 신호 아님"
        status = f"{docs.get('pr_day')} 공시. {rev_txt}. {bk_txt}. {rpo_txt}. {ocf_txt}. {power_txt}."
        extra = f"부채: {debt_txt}. 만기: {mat_txt}. {stress}"
    else:
        title = "[CRWVQ3] CRWV 코어위브 3Q 미발표 | HBM LTA 뉴스만 | 매매 신호 아님"
        status = "11/20 07:25까지 SEC에 3분기 실적 8-K 없음. 백로그/RPO/OCF/전력 판정 못 함."
        extra = ""
    lines = [
        f"{stamp} | CRWVQ3 | {cfg['event_label']}",
        "이게 뭐 알림: 코어위브 3분기 백로그·RPO 전환 속도, OCF/캐펙스, 신규 부채·전력·만기 언급, HBM 2027 LTA 뉴스 요약입니다.",
        f"지금 결과/상태: {status}",
        f"추가 파싱: {extra}" if extra else "추가 파싱: 공시 전이라 없음",
        "HBM 언급: " + (" / ".join(hbm) if hbm else "보도자료·10-Q에 HBM 직접 언급 없음"),
        "HBM 2027 LTA 뉴스: " + ("; ".join(news) if news else ("뉴스 조회 실패: " + news_err if news_err else "최근 30일 해당 제목 없음")),
        "지금 할 일: 원문(보도자료·10-Q·콜) 확인 전 매매 없음. 채권 수익률은 [CRWVBOND] 참고.",
        "나중에: 이 알림은 1회로 끝납니다. 상세 스트레스는 원문 숫자로 별도 계산.",
        "익절/줄일 때: 기존 종목별 규칙을 따르세요.",
        "하지 말 것: 백로그·파싱 숫자만 보고 추격매수. 신용/미수.",
    ]
    emit(cfg, title, lines, 4 if published else 3)
    if mode != "dry":
        state["result_notified" if published else "empty_notified"] = True
        state["last_sent"] = {"at": stamp, "published": published}
    save_state(state, now)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
