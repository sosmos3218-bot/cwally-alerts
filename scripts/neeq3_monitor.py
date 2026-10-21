#!/usr/bin/env python3
"""NEE Q3 2026 one-shot (2026-10-21 KST).

Two phases, each sent at most once:
  1) numbers  (20:40, retry 22:40): adjusted EPS vs consensus, 2026/2027 guidance vs baseline,
     price vs the 73-75 USD buy zone, Virginia rate-freeze wording in the press release.
  2) call     (23:40, after the 9am ET webcast): management stance on the Virginia governor's
     six-year rate-freeze demand + 2027 guidance, from the press release and same-day headlines.
     Headline-based, not a full transcript; the alert says so.

Modes: auto (scheduled) | force (ignore window/state, sends) | dry (print only, no ntfy, no state).
Local parser test: NEEQ3_TEST_TEXT=<file> python scripts/neeq3_monitor.py dry
Not investment advice.
"""
from __future__ import annotations

import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from pathlib import Path
from urllib.parse import quote_plus
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_topic

KST = timezone(timedelta(hours=9))
CFG = ROOT / "configs" / "neeq3.json"
STATE = ROOT / "state" / "neeq3.json"
UA = {"User-Agent": "cwally-alerts sosmos3218@gmail.com", "Accept-Encoding": "identity"}
CIK_INT = 753308


# ---------- fetch ----------

def fetch(url: str) -> str:
    req = Request(url, headers=UA)
    with urlopen(req, timeout=25) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def textify(html: str) -> str:
    t = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", html)
    t = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", t)
    t = re.sub(r"<[^>]+>", " ", t)
    t = unescape(t).replace("\xa0", " ").replace("’", "'")
    return re.sub(r"\s+", " ", t)


def sec_release(cfg: dict) -> tuple[str, str]:
    """Press release (EX-99.1) of the first 8-K filed on/after release day."""
    data = json.loads(fetch(cfg["sources"]["sec"]))
    recent = data.get("filings", {}).get("recent", {})
    day = cfg["release_at_kst"][:10]
    for form, date, acc in zip(recent.get("form", []), recent.get("filingDate", []), recent.get("accessionNumber", [])):
        if form != "8-K" or date < day:
            continue
        base = f"https://www.sec.gov/Archives/edgar/data/{CIK_INT}/{acc.replace('-', '')}"
        names = []
        try:
            idx = json.loads(fetch(base + "/index.json"))
            names = [i.get("name", "") for i in idx.get("directory", {}).get("item", [])]
        except Exception:
            pass
        ex = [n for n in names if re.search(r"ex(hibit)?-?99", n, re.I) and n.lower().endswith((".htm", ".html"))]
        ex = ex or [cfg.get("sec_exhibit_guess", "neeq32026exhibit99.htm")]
        for name in ex:
            try:
                body = textify(fetch(f"{base}/{name}"))
            except Exception:
                continue
            if re.search(r"third.quarter", body, re.I):
                return body, f"SEC 8-K {date} {name}"
    return "", ""


def headlines(cfg: dict, since: datetime) -> list[dict]:
    out, seen = [], set()
    for q in cfg.get("news_queries", []):
        url = f"https://news.google.com/rss/search?q={quote_plus(q)}&hl=en-US&gl=US&ceid=US:en"
        try:
            root = ET.fromstring(fetch(url))
        except Exception:
            continue
        for item in root.iter("item"):
            title = (item.findtext("title") or "").strip()
            try:
                pub = parsedate_to_datetime(item.findtext("pubDate") or "")
            except Exception:
                continue
            if pub.tzinfo is None:
                pub = pub.replace(tzinfo=timezone.utc)
            if pub < since or not title or title in seen:
                continue
            seen.add(title)
            out.append({"title": title, "pub": pub.astimezone(KST)})
    out.sort(key=lambda h: h["pub"], reverse=True)
    return out


def get_price(cfg: dict) -> tuple[float | None, float | None, str]:
    """(last, reference close before the release day session, note)."""
    try:
        data = json.loads(fetch(cfg["sources"]["quote"]))
        res = ((data.get("chart") or {}).get("result") or [None])[0]
        if not res:
            return None, None, "시세 없음"
        last = (res.get("meta") or {}).get("regularMarketPrice")
        ts = res.get("timestamp") or []
        closes = ((res.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
        et = timezone(timedelta(hours=-4))
        day = cfg["release_at_kst"][:10]  # same calendar date in ET (BMO 07:30 ET)
        ref = None
        for t, c in zip(ts, closes):
            if c is not None and datetime.fromtimestamp(t, et).strftime("%Y-%m-%d") < day:
                ref = c
        return (float(last) if last else None), ref, ""
    except Exception as exc:
        return None, None, f"시세 실패: {exc}"


# ---------- parse ----------

def sentences(text: str) -> list[str]:
    return re.split(r"(?<=[A-Za-z0-9%)\]])\.\s+(?=[A-Z(])", text)


# Forward-looking/cautionary boilerplate lists "termination of the merger agreement", "lower" outlook etc.
# as risks; it is never management's stance or a guidance change (10/21 false "철회 언급").
BOILER_CUT = r"(Cautionary Statements? (and|Regarding)|forward-looking statements? (are|include|involve|within the meaning))"
BOILER_SENT = r"(forward.looking|risks? and uncertaint|could cause|may cause|cautionary|risk factors|no assurance|could give rise|subject to conditions that are not anticipated)"


def drop_boilerplate(text: str) -> str:
    m = re.search(BOILER_CUT, text, re.I)
    if m and m.start() > len(text) * 0.3:
        text = text[:m.start()]
    return " ".join(s + "." for s in sentences(text) if not re.search(BOILER_SENT, s, re.I))


def num(pattern: str, text: str) -> float | None:
    m = re.search(pattern, text, re.I)
    return float(m.group(1).replace(",", "")) if m else None


def parse_guidance(text: str) -> dict[int, tuple[float, float]]:
    out: dict[int, tuple[float, float]] = {}
    for s in sentences(text):
        if not re.search(r"adjusted earnings per share", s, re.I) or not re.search(r"range", s, re.I):
            continue
        # year sits before the first $ range ("continues to expect 2026 adjusted earnings per share
        # to be in the range of $3.92 to $4.02"); splitting on "expect" dropped it (10/21 "2026 미기재").
        head = s.split("$", 1)[0]
        years = [int(y) for y in re.findall(r"\b(20[2-4]\d)\b", head)]
        ranges = [(float(a), float(b)) for a, b in re.findall(r"\$(\d+\.\d+) to \$(\d+\.\d+)", s)]
        for y, r in zip(years, ranges):
            out.setdefault(y, r)
    return out


def parse(text: str) -> dict:
    text = drop_boilerplate(text)
    adj = num(r"adjusted basis,[^$]{0,120}third.quarter earnings were \$[\d.,]+ (?:billion|million),? or \$(\d+\.\d+) per share", text)
    if adj is None:
        adj = num(r"third.quarter[^.$]{0,60}adjusted earnings[^$]{0,40}\$[\d.,]+ (?:billion|million),? or \$(\d+\.\d+) per share", text)
    gaap = num(r"GAAP basis of \$[\d.,]+ (?:billion|million),? or \$(\d+\.\d+) per share", text)
    growth = []
    for s in sentences(text):
        if re.search(r"per share", s, re.I) and re.search(r"grow|growth|compound", s, re.I) and not re.search(r"dividend|regulatory capital", s, re.I):
            growth += [int(p) for p in re.findall(r"(\d{1,2})%", s)]
    return {
        "published": bool(adj or gaap) and bool(re.search(r"third.quarter", text, re.I)),
        "adj_eps": adj,
        "gaap_eps": gaap,
        "guidance": parse_guidance(text),
        "guide_cut": bool(re.search(r"(lower(ed|s)?|reduc(ed|es)|revis(ed|es) down)[^.]{0,60}(guidance|expectations|outlook|range)", text, re.I)),
        "growth_pcts": sorted(set(growth)),
    }


STANCE_RULES = [
    ("철회 언급", r"(terminat(es|ed|ing)|walk(s|ed|ing)? away|abandon(s|ed|ing)|call(s|ed|ing)? off|scrap(s|ped|ping)?|withdr[ae]w(s|n|ing)?)[^.]{0,40}(merger|deal|combination|application|agreement|bid)"),
    ("요금동결 수용/추가 양보", r"(accept|agree|offer|sweeten|enhanc|additional|increas|new)\w* [^.]{0,60}(freeze|credit|commitment|concession)|(freeze|credit|concession)[^.]{0,40}(accept|agree|offer)"),
    ("반박/거부", r"(reject|oppos|disagree|push(es|ed)? back|unreasonable|not (be )?(feasible|workable|acceptable))[^.]{0,60}(freeze|governor|spanberger|staff|condition)|(freeze|condition)[^.]{0,60}(reject|unworkable|unreasonable)"),
    ("검토/협상 중", r"compromise|(review|evaluat|consider|discuss|work(ing)? with|engag|negotiat|settle|confident)[^.]{0,60}(freeze|governor|spanberger|scc|commission|condition|virginia|approval)"),
]


# Headlines whose subject is the governor, SCC staff, lawmakers etc. describe THEIR position
# ("Spanberger will oppose ... if there's no rate freeze"), not management's; they count as a mention only.
THIRD_PARTY = r"^\W*(va\.?\s+|virginia\s+)?(gov\b|gov\.|governor|spanberger|scc|state corporation|staff|lawmakers?|legislators?|regulators?|attorney general|consumer|critics?|opponents?|advocates?|commentary|opinion)"


def stance(release: str, heads: list[dict]) -> tuple[str, list[str]]:
    corpus = [(h["title"], f"헤드라인 {h['pub']:%H:%M}") for h in heads]
    for s in sentences(drop_boilerplate(release)):
        if re.search(r"virginia|dominion|spanberger|rate freeze|scc|commission", s, re.I):
            corpus.append((s, "보도자료"))
    hits: list[tuple[int, str, str]] = []
    for text, src in corpus:
        if not re.search(r"freeze|spanberger|governor|virginia|dominion|scc|merger", text, re.I):
            continue
        if src != "보도자료" and re.search(THIRD_PARTY, text, re.I):
            continue
        for rank, (label, pat) in enumerate(STANCE_RULES):
            if re.search(pat, text, re.I):
                hits.append((rank, label, f"{src}: {text[:90]}"))
                break
    if not hits:
        mention = any(re.search(r"freeze|spanberger", t, re.I) for t, _ in corpus)
        return ("언급만 있음, 입장 불명" if mention else "입장 미발견"), []
    hits.sort(key=lambda h: h[0])
    return hits[0][1], [h[2] for h in hits[:2]]


# ---------- decide + copy ----------

def judge(cfg: dict, p: dict, last: float | None, ref: float | None, merger: str) -> tuple[str, str, int]:
    b = cfg["baseline"]
    reasons, level = [], 0  # 0 관망, 1 구간 접근, 2 주의
    g26 = p["guidance"].get(2026)
    eps = p["adj_eps"]
    if eps is not None:
        if eps < b["consensus_eps_low"] - 0.02:
            reasons.append(f"EPS {eps} 미스(컨센 {b['consensus_eps_low']}-{b['consensus_eps_high']})")
            level = 2
        elif eps > b["consensus_eps_high"]:
            reasons.append(f"EPS {eps} 상회")
        else:
            reasons.append(f"EPS {eps} 부합")
    if p["guide_cut"] or (g26 and (g26[0] < b["guidance_low"] or g26[1] < b["guidance_high"])):
        reasons.append("2026 가이던스 하향")
        level = 2
    if merger == "철회 언급":
        reasons.append("합병 철회 언급")
    elif merger.startswith("요금동결 수용"):
        # 6년 동결/크레딧 양보는 Dominion 수익 회수와 합병 EPS 기여를 직접 깎는다.
        # 73-75 구간은 양보 전 가치 기준이라, 양보 규모가 숫자로 나오기 전엔 구간 도달도 매수 근거가 아니다.
        reasons.append("요금동결 양보, 73-75 구간이어도 매수 보류")
        level = 2
    if last is not None:
        lo, hi = b["buy_zone"]
        if last < lo:
            reasons.append(f"{last:.2f}달러, {lo} 이탈")
            level = 2
        elif last <= hi and level < 2:
            reasons.append(f"{last:.2f}달러, {lo}-{hi} 구간 진입")
            level = 1
        if ref:
            reasons.append(f"전일 대비 {(last / ref - 1) * 100:+.1f}%")
    action = ["관망 유지 | 신규 매수 보류", "73-75 구간 | 1차 분할만 검토", "주의 | 신규 매수 보류"][level]
    return action, ". ".join(reasons) or "판정 근거 부족", level


def g_text(g: dict, year: int, base: str) -> str:
    r = g.get(year)
    return f"{year} {r[0]:.2f}-{r[1]:.2f} (기준 {base})" if r else f"{year} 미기재 (기준 {base})"


def body(cfg: dict, now: datetime, head: str, status: str, todo: str, later: str) -> str:
    b = cfg["baseline"]
    return "\n".join([
        now.strftime("%Y-%m-%d %H:%M KST") + f" | NEE | {head}",
        "이게 뭐 알림: " + cfg["explain"],
        "지금 상태: " + status,
        "지금 할 일: " + todo,
        "나중에: " + later,
        f"익절/줄일 때: 미보유 기준. 매수했다면 {b['buy_zone'][0]}달러 종가 이탈 또는 2026 가이던스 하향 시 비중 축소.",
        "하지 말 것: 실적 당일 추격, 한 번에 전액, 헤드라인 1건만 보고 단정, 신용/미수. 투자 권유 아님.",
    ])


# ---------- main ----------

def load_state() -> dict:
    st = {"notified": False, "call_notified": False, "fail_notified": False}
    if STATE.exists():
        try:
            st.update(json.loads(STATE.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass
    return st


def save_state(st: dict, now: datetime, dry: bool) -> None:
    if dry:
        return
    st["updated_at"] = now.isoformat()
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def send(dry: bool, title: str, msg: str, prio: int) -> None:
    if dry:
        print(f"DRY RUN would send: {title}\n{msg}\n")
        return
    publish(resolve_topic("port"), title, msg, prio)


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    dry = mode == "dry"
    cfg = json.loads(CFG.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    st = load_state()
    start = datetime.fromisoformat(cfg["window_start_kst"]).replace(tzinfo=KST)
    end = datetime.fromisoformat(cfg["expire_date"] + "T23:59:59").replace(tzinfo=KST)
    call_at = datetime.fromisoformat(cfg["call_followup_kst"]).replace(tzinfo=KST)
    in_win = start <= now <= end and now.year == int(cfg["event_year"])
    phase_call = now >= call_at or (mode in {"force", "dry"} and os.environ.get("NEEQ3_PHASE") == "call")
    print(f"{now.isoformat()} mode={mode} window={in_win} phase={'call' if phase_call else 'numbers'}")
    if dry:
        print("DRY RUN (no ntfy, no state write)")
    if mode == "auto" and not in_win:
        print("outside window; silent")
        return 0
    if mode == "auto" and st.get("call_notified"):
        print("all phases sent; skip")
        return 0
    if mode == "auto" and st.get("notified") and not phase_call:
        print("numbers already sent; waiting for call phase")
        return 0

    test = os.environ.get("NEEQ3_TEST_TEXT") if dry else None
    err = []
    if test:
        release, src = Path(test).read_text(encoding="utf-8"), "테스트 텍스트"
    else:
        try:
            release, src = sec_release(cfg)
        except Exception as exc:
            release, src = "", ""
            err.append(f"sec: {exc}")
    p = parse(release) if release else parse("")
    last, ref, pnote = get_price(cfg) if not test else (None, None, "테스트")
    heads = [] if test else headlines(cfg, start - timedelta(hours=12))
    merger, evidence = stance(release, heads)
    action, why, level = judge(cfg, p, last, ref, merger)
    g = p["guidance"]
    b = cfg["baseline"]
    print("parse:", json.dumps({**p, "guidance": {str(k): v for k, v in g.items()}}, ensure_ascii=False))
    print("src:", src or "-", "| price:", last, "ref:", ref, pnote, "| merger:", merger, "| heads:", len(heads), "| err:", err)
    for e in evidence:
        print("  evidence:", e)

    if not p["published"] and not phase_call:
        if not release and not heads and pnote.startswith("시세 실패") and not st.get("fail_notified"):
            msg = body(cfg, now, "Q3 조회", "자료 조회 실패. " + "; ".join(err + [pnote]),
                       "IR 페이지 직접 확인. 이 알림으로 매매 금지.", "22:40, 23:40 자동 재확인.")
            send(dry, "[NEE] 조회 실패 | 판정 보류", msg, 3)  # route_topic sends this to cwally-ops
            st["fail_notified"] = True
        else:
            print("release not parsed yet; retry at next one-shot")
        save_state(st, now, dry)
        return 0

    g26_base = "{}-{}".format(b["guidance_low"], b["guidance_high"])
    g27_base = "추정 {:.2f}, 연 {:.0f}% 경로".format(b["implied_2027_eps"], b["long_term_cagr"])
    eps_txt = p["adj_eps"] if p["adj_eps"] is not None else "미확인"
    growth_txt = ",".join(str(x) + "%" for x in p["growth_pcts"]) or "미확인"
    nums = (f"조정 EPS {eps_txt} (컨센 {b['consensus_eps_low']}-{b['consensus_eps_high']}). "
            f"가이던스 {g_text(g, 2026, g26_base)}, {g_text(g, 2027, g27_base)}. "
            f"성장률 문구 {growth_txt}.")

    if not phase_call:
        status = f"{action}. {why}. {nums} 버지니아 요금동결 대응(보도자료): {merger}. 출처 {src}."
        todo = {0: "관망. 23:40 콜 이후 요금동결 대응 알림 확인 뒤 결정.",
                1: "1차 분할(계획 금액의 1/3)만 검토. 나머지는 23:40 알림 뒤.",
                2: "신규 매수 보류. SO/DUK 대안 유지."}[level]
        send(dry, f"[NEE] {action}", body(cfg, now, "Q3 숫자 판정", status, todo, "23:40에 콜 이후 요금동결 대응 판정이 한 번 더 옵니다."), 4 if level else 3)
        st.update({"notified": True, "score": action})
        save_state(st, now, dry)
        return 0

    # call phase (23:40): stance on the 6-year freeze + 2027 guidance
    g27 = g.get(2027)
    g27_note = ("2027 가이던스 " + (f"{g27[0]:.2f}-{g27[1]:.2f}" if g27 else "보도자료 미기재")
                + (f", 중간값이 추정 경로({b['implied_2027_eps']:.2f}) {'이상' if g27 and sum(g27) / 2 >= b['implied_2027_eps'] else '미만'}" if g27 else ""))
    ev = " / ".join(evidence) if evidence else "근거 헤드라인 없음"
    status = (f"요금동결 대응: {merger}. {g27_note}. {action}. {why}. 근거: {ev}. "
              + ("" if st.get("notified") else nums + " ")
              + "헤드라인과 보도자료 기준이며 어닝콜 전문 검증은 아님.")
    todo = {
        "철회 언급": "합병 철회 시나리오. 내일 장중 주가 반응 확인 전 신규 매수 금지.",
        "요금동결 수용/추가 양보": "73-75 구간이어도 매수 보류. 양보 규모와 2027 EPS 영향이 숫자로 나온 뒤 재판정.",
        "반박/거부": "규제 장기화 가능성. 관망 유지, 73-75 구간에서만 분할.",
        "검토/협상 중": "결론 미정. 관망 유지, SCC 청문 일정 확인.",
    }.get(merger, "입장 확인 불가. 관망 유지. 콜 다시보기나 전문으로 직접 확인.")
    if level == 2 and "보류" not in todo:
        todo = "신규 매수 보류. " + todo
    title = f"[NEE] 콜 판정 {merger.split('/')[0].split(',')[0]} | {action.split('|')[-1].strip()}"
    send(dry, title, body(cfg, now, "콜 이후 판정", status, todo, "일회 알림 종료. SCC 청문과 합병 결정은 별도 확인."), 4 if merger in {"철회 언급", "요금동결 수용/추가 양보"} or level == 2 else 3)
    st.update({"notified": True, "call_notified": True, "call_stance": merger, "score": action})
    save_state(st, now, dry)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
