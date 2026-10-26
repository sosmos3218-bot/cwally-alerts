#!/usr/bin/env python3
"""[LGES] LG에너지솔루션 (KRX 373220) watch (approved in chat 2026-10-26).

1. Price zone: completed KRX close <= 330,000 KRW -> '매수 매력 구간 진입' (priority 4).
   Fires once when the close ENTERS the zone, quietly re-arms when a close is back above 330,000.
   First run (no state) only records the current close. Dedupe by trading date (state last_date).
   Same pattern as figpx_monitor.py. Prices: Naver mobile daily prices (same source as lgesfor/k079900).
2. Filing watch: KIND company disclosure list (kind.krx.co.kr, no API key; same request as k079900).
   Sends titles matching configs filings.rules (조회공시/풍문/해명/불성실공시, 영업(잠정)실적, 횡령/배임,
   단일판매/공급계약, 주요사항보고서, 분기/반기/사업보고서). Dedupe by KIND receipt number (seen_acptno).
   First run seeds the current list without sending.
3. Q4 2026 checklist: a new 영업(잠정)실적 filing inside q4.window (2027-01-01..02-28) also sends the
   checklist once (q4_checklist_sent).

Runs on the existing k203780 19:00 KST slot (KRX trading days). No new cron.
Modes: auto | force (re-evaluate the last bar even if seen) | dry (print only, no ntfy, no state)
       | confirm (install-confirmation send, no state) | selftest.
Not investment advice.
"""
from __future__ import annotations

import html
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
CFG = ROOT / "configs" / "lgeswatch.json"
STATE = ROOT / "state" / "lgeswatch.json"
UA = "Mozilla/5.0 (X11; Linux x86_64) cwally-alerts"
KIND = "https://kind.krx.co.kr"


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


def http(url: str, data: dict | None = None, headers: dict | None = None, tries: int = 3, timeout: int = 30) -> str:
    h = {"User-Agent": UA, "Accept-Language": "ko-KR,ko;q=0.9"}
    h.update(headers or {})
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    if body is not None:
        h["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
    err: Exception | None = None
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data=body, headers=h), timeout=timeout) as resp:
                raw = resp.read()
                ctype = (resp.headers.get("Content-Type") or "").lower()
            if "euc-kr" in ctype:
                return raw.decode("euc-kr", errors="replace")
            try:
                return raw.decode("utf-8")
            except UnicodeDecodeError:
                return raw.decode("euc-kr", errors="replace")
        except Exception as exc:  # noqa: BLE001
            err = exc
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"{url.split('?')[0]}: {err}")


def text_of(page: str) -> str:
    t = re.sub(r"(?s)<(script|style)[^>]*>.*?</\1>", " ", page)
    t = re.sub(r"<[^>]+>", " ", t)
    return re.sub(r"\s+", " ", html.unescape(t)).strip()


def clean(t: str) -> str:
    """KIND titles use the Hangul dot (U+318D) and sometimes U+00B7; the copy rule bans middle dots."""
    return t.replace("\u318d", "/").replace("\u00b7", "/").replace("\u2014", "-")


# ---------- data ----------

def fetch_bars(cfg: dict, now: datetime) -> list[tuple[str, float]]:
    """Completed daily closes (KST date, close), ascending. Today's bar only after market_close_kst."""
    raw = json.loads(http(cfg["sources"]["bars"]))
    out: dict[str, float] = {}
    for r in raw if isinstance(raw, list) else []:
        d = str(r.get("localTradedAt") or "")[:10]
        c = str(r.get("closePrice") or "").replace(",", "")
        if len(d) == 10 and c.replace(".", "").isdigit():
            out[d] = float(c)
    if not out:
        raise RuntimeError("시세 0건")
    hh, mm = map(int, cfg.get("market_close_kst", "15:40").split(":"))
    today = now.strftime("%Y-%m-%d")
    after = (now.hour, now.minute) >= (hh, mm)
    return sorted((d, c) for d, c in out.items() if d < today or (d == today and after))


def kind_list(cfg: dict, since: str, until: str) -> list[dict]:
    form = {
        "method": "searchDetailsSub", "currentPageSize": "100", "pageIndex": "1", "orderMode": "1", "orderStat": "D",
        "forward": "details_sub", "searchCodeType": "", "repIsuSrtCd": "A" + cfg["code"], "allRepIsuSrtCd": "",
        "oldSearchCorpName": "", "disclosureType": "", "disTypevalue": "", "reportNm": "", "reportCd": "",
        "searchCorpName": cfg["code"], "business": "", "marketType": "", "settlementMonth": "", "securities": "",
        "submitOblgNm": "", "enterprise": "", "fromDate": since, "toDate": until, "reportNmTemp": "", "bfrDsclsType": "on",
    }
    page = http(KIND + "/disclosure/details.do", data=form,
                headers={"X-Requested-With": "XMLHttpRequest", "Referer": KIND + "/disclosure/details.do?method=searchDetailsMain"})
    out, rows = [], 0
    for row in re.findall(r"(?s)<tr[^>]*>(.*?)</tr>", page):
        acpt = re.search(r"openDisclsViewer\('(\d{14})'", row)
        if not acpt:
            continue
        rows += 1
        cells = [text_of(td) for td in re.findall(r"(?s)<td[^>]*>(.*?)</td>", row)]
        # cells: no, "YYYY-MM-DD HH:MM", company, title, submitter, charts
        if len(cells) < 5 or cfg["name_kr"] not in cells[2] or not re.match(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", cells[1]):
            continue
        out.append({"acptno": acpt.group(1), "filed": cells[1][:16], "title": cells[3], "submitter": cells[4]})
    if rows == 0 and "조회된 결과값이 없습니다" not in page and "<table" not in page:
        raise RuntimeError("KIND 응답 형식 이상")
    return out


# ---------- core (pure) ----------

def hit(level: dict, close: float) -> bool:
    return close <= float(level["price"]) if level["op"] == "le" else close < float(level["price"])


def step(level: dict, armed: bool | None, close: float) -> tuple[bool, bool]:
    """(new armed, fired). armed None = first run: init only."""
    inside = hit(level, round(float(close), 2))
    if armed is None:
        return (not inside), False
    if armed and inside:
        return False, True
    if not armed and not inside:
        return True, False
    return armed, False


def classify(cfg: dict, title: str) -> dict | None:
    for r in cfg["filings"]["rules"]:
        if re.search(r["re"], title):
            return r
    return None


def in_q4(cfg: dict, filed: str) -> bool:
    q = cfg["q4"]
    return q["window_start"] <= filed[:10] <= q["window_end"]


def won(v: float) -> str:
    return f"{v:,.0f}원"


def price_msg(cfg: dict, close: float, day: str, prev: float | None) -> tuple[str, str, int]:
    lv = cfg["price"]
    ref = cfg["reference"]
    chg = f", 전 거래일 대비 {(close / prev - 1) * 100:+.1f}%" if prev else ""
    title = f"[LGES] 종가 {won(close)} {lv['label']} | 논리 확인 뒤 분할"
    lines = [
        f"{datetime.now(KST):%Y-%m-%d %H:%M} KST | 373220 | LG에너지솔루션",
        "이게 뭐 알림: " + cfg["explain"],
        f"지금 결과/상태: {lv['label']}. KRX 종가 {won(close)}({day}{chg}). 기준선 {won(lv['price'])} 이하. 52주 최고 {won(ref['high_52w'])}.",
        "지금 할 일: " + lv["note"] + " 논리가 그대로면 계획 금액을 나눠서만 검토.",
        "나중에: 종가가 330,000원 위로 올라간 뒤 다시 들어올 때만 또 울립니다. 4분기 잠정실적(2027년 1월 초)에서 AMPC 제외 영업이익 3,000억원 이상인지 확인.",
        "익절/줄일 때: AMPC 요건 강화/FEOC 규정, ESS 수주나 마진 미달이면 비중 축소 검토.",
        "하지 말 것: 실적 악화로 빠진 하락을 싼 가격으로 읽기, 일괄 매수, 신용/미수, 이 알림만 보고 주문. 투자 권유 아님.",
    ]
    return title, "\n".join(lines), int(lv["priority"])


def filing_msg(cfg: dict, f: dict, rule: dict) -> tuple[str, str, int]:
    t = clean(f["title"])
    link = cfg["sources"]["kind_view"].format(acpt=f["acptno"])
    extra = {
        "inquiry": "조회공시는 답변 내용(확정/미확정/사실무근)과 다음 공시 기한을 확인.",
        "prelim": "AMPC 제외 영업이익, ESS 매출과 수주, AMPC 비중을 확인.",
        "embezzle": "금액, 자기자본 대비 비율, 거래정지/불성실공시 여부를 확인.",
        "contract": "계약 금액, 매출액 대비 비율, 상대방(ESS/EV), 기간을 확인.",
        "major": "유상증자, 사채 발행, 자산 양수도 같은 결정인지 확인.",
        "periodic": "AMPC 제외 이익, ESS 수주 잔고, 우발채무 주석을 확인.",
    }.get(rule["key"], "원문을 확인.")
    title = f"[LGES] 새 공시 {rule['label']} | 원문 확인"
    lines = [
        f"{datetime.now(KST):%Y-%m-%d %H:%M} KST | 373220 | LG에너지솔루션",
        "이게 뭐 알림: " + cfg["explain"],
        f"지금 결과/상태: {f['filed']} 공시 '{t}' (KIND 접수번호 {f['acptno']}). {link}",
        "지금 할 일: " + extra,
        "나중에: 같은 공시는 다시 보내지 않습니다. 정정 공시는 새 접수번호라 1번 더 옵니다.",
        "익절/줄일 때: 횡령/배임 금액이 크거나 거래정지, 불성실공시법인 지정이 나오면 비중 축소 검토.",
        "하지 말 것: 제목만 보고 주문, 장중 급변동 추격. 투자 권유 아님.",
    ]
    return title, "\n".join(lines), int(rule["priority"])


def q4_msg(cfg: dict, f: dict) -> tuple[str, str, int]:
    link = cfg["sources"]["kind_view"].format(acpt=f["acptno"])
    title = "[LGES] 4분기 잠정실적 확인 | 체크리스트 4개"
    lines = [
        f"{datetime.now(KST):%Y-%m-%d %H:%M} KST | 373220 | LG에너지솔루션",
        "이게 뭐 알림: " + cfg["explain"],
        f"지금 결과/상태: {f['filed']} '{clean(f['title'])}' 공시. {cfg['q4']['checklist']}",
        f"지금 할 일: 원문에서 위 4개를 직접 확인. {link}",
        "나중에: 사업보고서(3월)에서 연간 ESS 수주와 AMPC 금액을 다시 대조.",
        "익절/줄일 때: AMPC 제외 영업이익 3,000억원 미달이나 FEOC/AMPC 불리한 변화면 비중 축소 검토.",
        "하지 말 것: 헤드라인 영업이익(AMPC 포함)만 보고 판단, 실적 당일 추격. 투자 권유 아님.",
    ]
    return title, "\n".join(lines), 4


def confirm_msg(cfg: dict, close: float | None, day: str | None) -> tuple[str, str]:
    px = f"{won(close)}({day} KRX 종가)" if close is not None else "조회 실패(구간 미확인)"
    lines = [
        "[테스트] LGES 알림 설정 완료",
        "이게 뭐 알림: " + cfg["explain"],
        f"지금 결과/상태: 373220 최근 종가 {px}. 기준선: 종가 330,000원 이하 매수 매력 구간(우선순위 4). 공시 감시: KIND 목록, 키 없이 동작.",
        "지금 할 일: 없음. KRX 거래일 19:00 KST 에 판정하고, 구간에 새로 들어가거나 새 중요 공시가 있을 때만 보냅니다. 4분기 잠정실적 공시가 나오면 체크리스트를 1번 보냅니다.",
        "나중에: 같은 구간은 종가가 330,000원 위로 올라간 뒤 다시 들어올 때만 또 울립니다.",
        "익절/줄일 때: AMPC 요건 강화/FEOC 규정, ESS 수주나 마진 미달이면 비중 축소 검토.",
        "하지 말 것: 이 설치 확인을 매수/매도 신호로 읽기.",
    ]
    return "[LGES] 설치 확인 | 매매 신호 아님", "\n".join(lines)


# ---------- selftest ----------

def selftest() -> int:
    cfg = load_cfg()
    lv = cfg["price"]
    fails = 0

    def check(label, got, want):
        nonlocal fails
        ok = got == want
        fails += not ok
        print(("ok  " if ok else "FAIL"), label, got, "" if ok else f"want {want}")

    check("init 374000", step(lv, None, 374000), (True, False))
    check("init 320000 no send", step(lv, None, 320000), (False, False))
    check("330001 nothing", step(lv, True, 330001), (True, False))
    check("330000 fires (<=)", step(lv, True, 330000), (False, True))
    walk = [(329000, True), (320000, False), (330000, False), (330500, False), (329999, True), (400000, False), (300000, True)]
    a = True
    for i, (c, want) in enumerate(walk):
        a, f = step(lv, a, c)
        check(f"walk{i} {c}", f, want)
    cases = {
        "조회공시 요구(풍문 또는 보도)": "inquiry", "조회공시요구(풍문또는보도)에대한답변(미확정)": "inquiry",
        "풍문 또는 보도에 대한 해명(미확정)": "inquiry", "연결재무제표기준영업(잠정)실적(공정공시)": "prelim",
        "횡령\u318d배임혐의발생": "embezzle", "[정정] 단일판매\u318d공급계약체결": "contract",
        "주요사항보고서(유상증자결정)": "major", "반기보고서(일반법인) (2026.06)": "periodic",
        "임원\u318d주요주주특정증권등소유상황보고서": None, "기업설명회(IR) 개최(안내공시)": None,
        "주식선물\u318d주식옵션 2단계 가격제한폭 확대요건 도달(상승)": None,
    }
    for t, want in cases.items():
        r = classify(cfg, t)
        check(f"classify {t[:20]}", r["key"] if r else None, want)
    check("q4 window in", in_q4(cfg, "2027-01-08 08:30"), True)
    check("q4 window out", in_q4(cfg, "2026-10-08 08:34"), False)
    f = {"acptno": "20270108000055", "filed": "2027-01-08 08:30", "title": "연결재무제표기준영업(잠정)실적(공정공시)"}
    msgs = [price_msg(cfg, 329000, "2026-11-02", 335000), filing_msg(cfg, {**f, "title": "횡령\u318d배임혐의발생"}, classify(cfg, "횡령배임")),
            q4_msg(cfg, f), confirm_msg(cfg, 374000, "2026-10-26") + (3,)]
    for t, b, p in msgs:
        check(f"no middle dot / em-dash: {t[:30]}", any(ch in t + b for ch in "\u00b7\u2014\u318d"), False)
        print(p, t)
    print("selftest", "PASS" if not fails else f"FAIL {fails}")
    return 1 if fails else 0


# ---------- main ----------

def send(dry: bool, topic: str, title: str, msg: str, prio: int, tags: list[str]) -> None:
    print(title)
    if dry:
        print(f"DRY RUN would send (p{prio}):\n{msg}")
    else:
        publish(topic, title, msg, prio, tags=tags)


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    if mode == "selftest":
        return selftest()
    dry = mode == "dry"
    cfg = load_cfg()
    now = datetime.now(KST)
    print(f"{now.isoformat(timespec='seconds')} lgeswatch mode={mode}")
    if dry:
        print("DRY RUN (no ntfy, no state write)")
    if mode == "confirm":
        try:
            d, c = fetch_bars(cfg, now)[-1]
            title, msg = confirm_msg(cfg, c, d)
        except Exception as exc:
            print(f"bars fetch failed: {exc}")
            title, msg = confirm_msg(cfg, None, None)
        print(msg)
        publish(resolve_topic("port"), title, msg, 3, tags=["white_check_mark"])
        return 0
    vu = cfg.get("valid_until")
    if vu and now.date() > date.fromisoformat(vu):
        print(f"expired (valid_until {vu}); silent")
        return 0
    st = load_state()
    port, ops = resolve_topic("port"), resolve_topic("ops")

    # 1. price zone
    lv = cfg["price"]
    try:
        bars = fetch_bars(cfg, now)
        st["price_fail_notified"] = False
    except Exception as exc:
        bars = []
        print(f"bars fetch failed: {exc}")
        if not st.get("price_fail_notified"):
            send(dry, ops, "[LGES] 시세 조회 실패 | 매매 신호 아님", f"373220 일별 시세 조회 실패: {exc}. 다음 19:00 실행에서 다시 봅니다.", 3, ["warning"])
            st["price_fail_notified"] = True
    if bars:
        last_d, last_c = bars[-1]
        if (now.date() - date.fromisoformat(last_d)).days > int(cfg.get("stale_days", 6)):
            print(f"stale bar {last_d}; price skip")
        elif not st.get("initialized"):
            armed, _ = step(lv, None, last_c)
            st.update({"initialized": True, "armed": armed, "last_date": last_d, "last_close": last_c,
                       "init_note": f"initialized from {last_d} close {last_c:,.0f}, no send"})
            print(f"init from {last_d} close {last_c:,.0f}: armed={armed} (no send)")
        else:
            prev_date = st.get("last_date")
            new = [(d, c) for d, c in bars if prev_date is None or d > prev_date]
            if mode == "force" and not new:
                new = bars[-1:]
            armed, fired_any, prev_close = st.get("armed", True), False, st.get("last_close")
            for d, c in new:
                armed, fired = step(lv, armed, c)
                print(f"bar {d} close {c:,.0f} -> fired={fired} armed={armed}")
                fired_any = fired_any or fired
            if new:
                d, c = new[-1]
                if len(new) > 1:
                    prev_close = new[-2][1]
                print(f"  line {lv['price']:,.0f}: {'armed' if armed else 'inside zone'}, distance {(c / lv['price'] - 1) * 100:+.1f}%")
                if fired_any:
                    t, m, p = price_msg(cfg, c, d, prev_close)
                    send(dry, port, t, m, p, ["chart_with_downwards_trend"])
                else:
                    print("no zone entry; silent")
                st.update({"armed": armed, "last_date": d, "last_close": c})
            else:
                print(f"no new bar after {prev_date}; armed={armed}")

    # 2. filings (+ 3. Q4 checklist)
    since = (now - timedelta(days=int(cfg["filings"]["lookback_days"]))).strftime("%Y-%m-%d")
    try:
        fl = kind_list(cfg, since, now.strftime("%Y-%m-%d"))
        st["kind_fail_notified"] = False
    except Exception as exc:
        fl = None
        print(f"KIND fetch failed: {exc}")
        if not st.get("kind_fail_notified"):
            send(dry, ops, "[LGES] 공시 조회 실패 | 매매 신호 아님", f"KIND 공시 목록 조회 실패: {exc}. 다음 19:00 실행에서 다시 봅니다.", 3, ["warning"])
            st["kind_fail_notified"] = True
    if fl is not None:
        print("filings:", [(f["acptno"], clean(f["title"])) for f in fl])
        seen = set(st.get("seen_acptno") or [])
        if not st.get("filings_seeded"):
            st["seen_acptno"] = sorted(seen | {f["acptno"] for f in fl})
            st["filings_seeded"] = True
            print(f"filings seeded (no alert): {len(fl)}")
        else:
            for f in sorted((x for x in fl if x["acptno"] not in seen), key=lambda x: x["acptno"]):
                rule = classify(cfg, f["title"])
                if rule:
                    t, m, p = filing_msg(cfg, f, rule)
                    send(dry, port, t, m, p, ["page_facing_up"])
                    if rule["key"] == "prelim" and in_q4(cfg, f["filed"]) and not st.get("q4_checklist_sent"):
                        t, m, p = q4_msg(cfg, f)
                        send(dry, port, t, m, p, ["clipboard"])
                        st["q4_checklist_sent"] = f["acptno"]
                else:
                    print(f"skip (not watched): {f['acptno']} {clean(f['title'])}")
                seen.add(f["acptno"])
            st["seen_acptno"] = sorted(seen)[-300:]
    save_state(st, dry)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
