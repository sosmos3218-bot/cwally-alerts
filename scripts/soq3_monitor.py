#!/usr/bin/env python3
"""SO (Southern Company) Q3 2026 one-shot, same two-phase shape as neeq3.

  1) numbers (11/5 21:40 KST, retry 23:40): adjusted EPS ("Excluding the items ... earned") vs the
     company's own Q3 estimate 1.65, reported EPS, year-over-year, price vs the 89.30 first entry.
  2) call    (11/6 06:20 KST, after the 1pm ET call and the US close): 2026 guidance top end
     reaffirmed or not, large-load contracted GW vs 17, equity-need wording, closing reaction ->
     whether the planned extra 20% buy is justified. Release text + same-day headlines, not a transcript.

Modes: auto | force (ignore window/state, sends) | dry (print only, no ntfy, no state).
Local parser test: SOQ3_TEST_TEXT=<file> [SOQ3_PHASE=call] python scripts/soq3_monitor.py dry
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
EST = timezone(timedelta(hours=-5))  # US standard time from 2026-11-01
CFG = ROOT / "configs" / "soq3.json"
STATE = ROOT / "state" / "soq3.json"
UA = {"User-Agent": "cwally-alerts sosmos3218@gmail.com", "Accept-Encoding": "identity"}
CIK_INT = 92122


# ---------- fetch ----------

def fetch(url: str) -> str:
    with urlopen(Request(url, headers=UA), timeout=25) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def textify(html: str) -> str:
    t = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", html)
    t = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", t)
    t = re.sub(r"<[^>]+>", " ", t)
    t = unescape(t).replace("\xa0", " ").replace("’", "'").replace("“", '"').replace("”", '"')
    return re.sub(r"\s+", " ", t)


def sec_release(cfg: dict) -> tuple[str, str]:
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
        ex = [n for n in names if re.search(r"ex-?99", n, re.I) and n.lower().endswith((".htm", ".html"))]
        ex = ex or [cfg.get("sec_exhibit_guess", "ex99-pressreleaseq32026.htm")]
        for name in ex:
            try:
                body = textify(fetch(f"{base}/{name}"))
            except Exception:
                continue
            if re.search(r"third.quarter", body, re.I) and re.search(r"per share", body, re.I):
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
            if pub < since or not title or title in seen or not re.search(r"southern", title, re.I):
                continue
            seen.add(title)
            out.append({"title": title, "pub": pub.astimezone(KST)})
    out.sort(key=lambda h: h["pub"], reverse=True)
    return out


def get_price(cfg: dict) -> tuple[float | None, float | None, str]:
    try:
        data = json.loads(fetch(cfg["sources"]["quote"]))
        res = ((data.get("chart") or {}).get("result") or [None])[0]
        if not res:
            return None, None, "시세 없음"
        last = (res.get("meta") or {}).get("regularMarketPrice")
        ts = res.get("timestamp") or []
        closes = ((res.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
        day = cfg["release_at_kst"][:10]  # 11/5 21:30 KST = 11/5 07:30 EST, same date
        ref = None
        for t, c in zip(ts, closes):
            if c is not None and datetime.fromtimestamp(t, EST).strftime("%Y-%m-%d") < day:
                ref = c
        return (float(last) if last else None), ref, ""
    except Exception as exc:
        return None, None, f"시세 실패: {exc}"


# ---------- parse ----------

def sentences(text: str) -> list[str]:
    return re.split(r"(?<=[A-Za-z0-9%)\]])\.\s+(?=[A-Z(\"])", text)


def num(pattern: str, text: str) -> float | None:
    m = re.search(pattern, text, re.I)
    return float(m.group(1).replace(",", "")) if m else None


def parse(text: str) -> dict:
    reported = num(r"reported third.quarter earnings of \$[\d.,]+ (?:billion|million),? or \$(\d+\.\d+) per share", text)
    adj = num(r"Excluding the items[^$]{0,200}?earned \$[\d.,]+ (?:billion|million),? or \$(\d+\.\d+) per share", text)
    if adj is None:
        adj = num(r"adjusted (?:earnings|EPS)[^$.]{0,80}\$(\d+\.\d+) per share", text)
    return {
        "published": bool(reported or adj) and bool(re.search(r"third.quarter", text, re.I)),
        "reported_eps": reported,
        "adj_eps": adj,
    }


def call_signals(release: str, heads: list[dict], b: dict) -> dict:
    corpus = [(h["title"], f"헤드라인 {h['pub']:%m/%d %H:%M}") for h in heads]
    corpus += [(s, "보도자료") for s in sentences(release)
               if re.search(r"guidance|outlook|top end|upper end|gigawatt|\bGW\b|large.load|data center|equity", s, re.I)]
    sig = {"guide": "미확인", "gw": None, "equity": "미확인", "evidence": []}

    def ev(src: str, text: str) -> None:
        line = f"{src}: {text[:90]}"
        if len(sig["evidence"]) < 3 and line not in sig["evidence"]:
            sig["evidence"].append(line)

    for text, src in corpus:
        if re.search(r"(lower(s|ed)?|cut(s)?|trim(s|med)?|reduc(es|ed))[^.]{0,40}(guidance|outlook|forecast)|lower end|bottom end|low end", text, re.I):
            if sig["guide"] != "하향/하단":
                sig["guide"] = "하향/하단"
                ev(src, text)
        elif sig["guide"] == "미확인" and re.search(r"(rais(es|ed)|lift(s|ed)|boost(s|ed)|increas(es|ed))[^.]{0,40}(guidance|outlook|forecast)", text, re.I):
            sig["guide"] = "상향"
            ev(src, text)
        elif sig["guide"] == "미확인" and re.search(r"top end|upper end|high end|top of", text, re.I):
            sig["guide"] = "상단 재확인"
            ev(src, text)
        elif sig["guide"] == "미확인" and re.search(r"(reaffirm|maintain|on track|on target)[^.]{0,40}(guidance|outlook|20\d\d)", text, re.I):
            sig["guide"] = "유지"
            ev(src, text)
        for gw in re.findall(r"(\d{1,3}(?:\.\d)?)\s?(?:GW|gigawatts?)\b", text, re.I):
            g = float(gw)
            if re.search(r"contract|signed|agreement|commit", text, re.I) and g < b["pipeline_gw"] and (sig["gw"] is None or g > sig["gw"]):
                sig["gw"] = g
                ev(src, text)
        if re.search(r"equity", text, re.I):
            if re.search(r"(more|additional|increas\w*|rais\w*|higher|new)[^.]{0,30}equity|equity[^.]{0,30}(increas|higher|offering)", text, re.I):
                sig["equity"] = "증가"
                ev(src, text)
            elif sig["equity"] == "미확인" and re.search(r"no (new|additional) equity|equity[^.]{0,30}(unchanged|reduc|lower|complete)", text, re.I):
                sig["equity"] = "유지/감소"
                ev(src, text)
    return sig


# ---------- decide + copy ----------

def judge_numbers(b: dict, p: dict, last: float | None, ref: float | None) -> tuple[str, str, int]:
    reasons, level = [], 0  # 0 계획대로, 2 보류
    eps = p["adj_eps"]
    if eps is not None:
        diff = eps - b["company_q3_eps"]
        tag = "상회" if diff > 0.02 else ("미달" if diff < -0.05 else "부합")
        reasons.append(f"조정 EPS {eps} {tag}(회사 추정 {b['company_q3_eps']}, 전년 {b['prior_q3_adj_eps']})")
        if tag == "미달":
            level = 2
    if last is not None:
        if ref:
            reasons.append(f"현재 {last:.2f}, 전일 대비 {(last / ref - 1) * 100:+.1f}%")
        if last < b["stop_ref"]:
            reasons.append(f"{b['stop_ref']} 아래")
            level = 2
    action = ["1차 보유 유지 | 추가는 06:20 판정 뒤", "", "주의 | 추가 매수 보류"][level]
    return action, ". ".join(reasons) or "판정 근거 부족", level


def judge_call(b: dict, p: dict, sig: dict, last: float | None, ref: float | None) -> tuple[str, str, int]:
    reasons, level = [], 1  # 0 추가 20% 검토, 1 추가 보류(보유 유지), 2 주의
    eps = p["adj_eps"]
    if eps is not None and eps < b["company_q3_eps"] - 0.05:
        reasons.append(f"Q3 EPS {eps} 미달")
        level = 2
    g = sig["guide"]
    reasons.append(f"2026 가이던스 {g}")
    if g == "하향/하단":
        level = 2
    if sig["equity"] == "증가":
        reasons.append("증자 필요 증가")
        level = 2
    if sig["gw"] is not None:
        reasons.append(f"대형부하 계약 {sig['gw']:g}GW (기준 {b['contracted_gw']}GW)")
    good = g in {"상단 재확인", "상향"} and sig["equity"] != "증가" and (sig["gw"] is None or sig["gw"] >= b["contracted_gw"])
    if level < 2 and good:
        level = 0
    if last is not None:
        chase = b["entry_ref"] * (1 + b["chase_limit_pct"] / 100)
        if ref:
            reasons.append(f"종가 {last:.2f}, 전일 대비 {(last / ref - 1) * 100:+.1f}%")
        if level == 0 and last > chase:
            reasons.append(f"1차 기준가 {b['entry_ref']}의 +{b['chase_limit_pct']:.0f}%({chase:.2f}) 초과라 추격 금지")
            level = 1
        if last < b["stop_ref"]:
            reasons.append(f"{b['stop_ref']} 아래")
            level = 2
    action = ["추가 20% 검토", "보유 유지 | 추가 보류", "주의 | 추가 매수 보류"][level]
    return action, ". ".join(reasons), level


def body(cfg: dict, now: datetime, head: str, status: str, todo: str, later: str) -> str:
    b = cfg["baseline"]
    return "\n".join([
        now.strftime("%Y-%m-%d %H:%M KST") + f" | SO | {head}",
        "이게 뭐 알림: " + cfg["explain"],
        "지금 상태: " + status,
        "지금 할 일: " + todo,
        "나중에: " + later,
        f"익절/줄일 때: 2026 가이던스 하향, 증자 확대, 또는 종가 {b['stop_ref']} 이탈 시 1차 물량 재평가.",
        "하지 말 것: 실적 당일 갭 추격, 계획(50%+20%) 초과 매수, 헤드라인 1건만 보고 단정, 신용/미수. 투자 권유 아님.",
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
    b = cfg["baseline"]
    now = datetime.now(KST)
    st = load_state()
    start = datetime.fromisoformat(cfg["window_start_kst"]).replace(tzinfo=KST)
    end = datetime.fromisoformat(cfg["window_end_kst"]).replace(tzinfo=KST)
    call_at = datetime.fromisoformat(cfg["call_followup_kst"]).replace(tzinfo=KST)
    in_win = start <= now <= end and now.year == int(cfg["event_year"])
    phase_call = now >= call_at or (mode in {"force", "dry"} and os.environ.get("SOQ3_PHASE") == "call")
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

    test = os.environ.get("SOQ3_TEST_TEXT") if dry else None
    err = []
    if test:
        release, src = Path(test).read_text(encoding="utf-8"), "테스트 텍스트"
    else:
        try:
            release, src = sec_release(cfg)
        except Exception as exc:
            release, src = "", ""
            err.append(f"sec: {exc}")
    p = parse(release)
    last, ref, pnote = (None, None, "테스트") if test else get_price(cfg)
    heads = [] if test else headlines(cfg, start - timedelta(hours=12))
    print("parse:", json.dumps(p, ensure_ascii=False))
    print("src:", src or "-", "| price:", last, "ref:", ref, pnote, "| heads:", len(heads), "| err:", err)

    if not phase_call:
        if not p["published"]:
            if not release and not heads and pnote.startswith("시세 실패") and not st.get("fail_notified"):
                msg = body(cfg, now, "Q3 조회", "자료 조회 실패. " + "; ".join(err + [pnote]),
                           "IR 페이지 직접 확인. 이 알림으로 매매 금지.", "23:40, 다음 날 06:20 자동 재확인.")
                send(dry, "[SO] 조회 실패 | 판정 보류", msg, 3)  # route_topic -> cwally-ops
                st["fail_notified"] = True
            else:
                print("release not parsed yet; retry at next one-shot")
            save_state(st, now, dry)
            return 0
        action, why, level = judge_numbers(b, p, last, ref)
        status = f"{action}. {why}. 보고 EPS {p['reported_eps'] if p['reported_eps'] is not None else '미기재'}. 출처 {src}."
        todo = "추가 매수 보류. 06:20 콜 판정 확인." if level == 2 else "1차 보유 유지. 추가 20%는 06:20 콜 판정 뒤 결정."
        send(dry, f"[SO] {action}", body(cfg, now, "Q3 숫자 판정", status, todo, "11/6 06:20에 콜 이후 가이던스, 대형부하, 증자 판정이 한 번 더 옵니다."), 4 if level else 3)
        st.update({"notified": True, "score": action})
        save_state(st, now, dry)
        return 0

    sig = call_signals(release, heads, b)
    for e in sig["evidence"]:
        print("  evidence:", e)
    action, why, level = judge_call(b, p, sig, last, ref)
    nums = "" if st.get("notified") else f"조정 EPS {p['adj_eps'] if p['adj_eps'] is not None else '미확인'}(회사 추정 {b['company_q3_eps']}). "
    ev = " / ".join(sig["evidence"]) or "근거 헤드라인 없음"
    status = f"{action}. {why}. 증자 {sig['equity']}. {nums}근거: {ev}. 헤드라인과 보도자료 기준이며 콜 전문 검증은 아님."
    todo = {
        0: "추가 20%를 오늘 밤 장중 분할로 검토. 갭 상승 출발이면 첫 30분은 기다림.",
        1: "1차 보유 유지. 추가 20%는 가이던스 상단과 증자 계획이 숫자로 확인될 때까지 보류.",
        2: "추가 매수 보류. 1차 물량은 손절 참고선과 가이던스 변화로 재평가.",
    }[level]
    if sig["guide"] == "미확인":
        todo += " 가이던스 문구를 못 찾았으니 콜 요약을 직접 확인."
    send(dry, f"[SO] 콜 판정 | {action}", body(cfg, now, "콜 이후 판정", status, todo, "일회 알림 종료. 다음 분기 실적 전 재평가."), 4 if level != 1 else 3)
    st.update({"notified": True, "call_notified": True, "score": action, "guide": sig["guide"], "gw": sig["gw"], "equity": sig["equity"]})
    save_state(st, now, dry)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
