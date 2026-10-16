#!/usr/bin/env python3
"""CoreWeave Q3 2026 backlog/RPO + HBM 2027 LTA news one-shot. Existing dispatcher slots only.

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
                "이게 뭐 알림: 코어위브 3분기 백로그/RPO와 HBM 2027 장기계약 뉴스 요약입니다.",
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
    if docs["tenq"]:
        try:
            tenq_text = strip_html(fetch(docs["tenq"], SEC_UA))
            rpo = parse_rpo(tenq_text)
        except Exception as exc:
            print(f"10-Q fetch fail: {exc}")
    if not rpo and pr_text:
        rpo = parse_rpo(pr_text)
    print("parsed pr", pr, "rpo", rpo)

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
        near = rpo.get("near_pct") if rpo else None
        verdict = "전환 빨라짐" if near is not None and near > b["q2_rpo_24m_pct"] + 2 else ("전환 느려짐" if near is not None and near < b["q2_rpo_24m_pct"] - 2 else "전환 속도 비슷/미확인")
        title = f"[CRWVQ3] 코어위브 3Q 백로그 {bk:,.0f}B$ | {verdict} | 매매 신호 아님" if bk else f"[CRWVQ3] 코어위브 3Q 발표 | 백로그 미확인 | 매매 신호 아님"
        status = f"{docs.get('pr_day')} 공시. {rev_txt}. {bk_txt}. {rpo_txt}."
    else:
        title = "[CRWVQ3] 코어위브 3Q 미발표 | HBM LTA 뉴스만 | 매매 신호 아님"
        status = "11/20 07:25까지 SEC에 3분기 실적 8-K 없음. 백로그/RPO 판정 못 함."
    lines = [
        f"{stamp} | CRWVQ3 | {cfg['event_label']}",
        "이게 뭐 알림: 코어위브 3분기 백로그·RPO·매출 전환 속도, HBM 언급, 삼성·하이닉스·마이크론 2027 HBM 장기계약(LTA) 뉴스 요약입니다.",
        f"지금 결과/상태: {status}",
        "HBM 언급: " + (" / ".join(hbm) if hbm else "보도자료·10-Q에 HBM 직접 언급 없음"),
        "HBM 2027 LTA 뉴스: " + ("; ".join(news) if news else ("뉴스 조회 실패: " + news_err if news_err else "최근 30일 해당 제목 없음")),
        "지금 할 일: 메모리 보유 비중 판단에 참고만. 원문(보도자료·콜) 확인 전 매매 없음.",
        "나중에: 이 알림은 1회로 끝납니다. LTA 가격·물량 확정 기사는 원문으로 확인.",
        "익절/줄일 때: 기존 종목별 규칙을 따르세요.",
        "하지 말 것: 백로그 숫자만 보고 추격매수. 기사 제목을 공식 계약으로 취급. 신용/미수.",
    ]
    emit(cfg, title, lines, 4 if published else 3)
    if mode != "dry":
        state["result_notified" if published else "empty_notified"] = True
        state["last_sent"] = {"at": stamp, "published": published}
    save_state(state, now)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
