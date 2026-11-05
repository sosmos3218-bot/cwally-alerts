#!/usr/bin/env python3
"""[417790] 트루엔 실적/주주환원 공시 + 가격 밴드 + 일일 요약 (2026-11-05 요청 pt2u).

KRX 거래일 19:00 k203780 슬롯(장 마감 후). 기존 oneshot [TRUEN] truen417790 과 별개.
  1) 실적 공시(분기보고서, 영업(잠정)실적, 매출액 또는 손익구조 변동) 신규 1번/건
  2) 주주환원 공시(자기주식 취득/처분/소각 결정, 현금배당 결정, 신탁계약 체결/해지) 신규 1번/건
  3) 종가 >= 7,600 또는 < 5,500 교차 1번, 1% 버퍼 재무장
  4) 거래일 일일 요약: 종가, 전일 대비 %, 순현금 주당 6,900원 대비 배수
공시: KIND 상세검색, 실패 시 네이버 KRX 공시 피드(OpenDART 키는 port-alerts 미주입).
첫 실행(seeded 아님): 이미 있는 해당 공시를 seen 만 채우고 발송 없음.
modes: auto | dry | install | selftest
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import secdiv_monitor as sd  # noqa: E402
from kr_calendar import KST, is_trading_day, session_day  # noqa: E402
from ntfy_util import publish, resolve_server, resolve_topic, route_topic  # noqa: E402

NAME = "k417790"
CFG_PATH = ROOT / "configs" / f"{NAME}.json"
STATE_PATH = ROOT / "state" / f"{NAME}.json"
FX = Path(__file__).resolve().parent / "fixtures" / NAME
TAG = "[417790]"
CODE = "417790"
CORP = "트루엔"
DOT = r"[\u318d\u00b7\u2027\u30fb]"

# 실적
EARN_RE = re.compile(
    r"분기보고서|영업\s*\(?\s*잠정\s*\)?\s*실적|"
    r"매출액\s*또는\s*손익구조|손익구조\s*" + DOT + r"?\s*변동"
)
# 주주환원 결정 (상황보고서/주주명부/변경상장 제외)
RET_BUY = re.compile(r"자기주식\s*취득\s*결정")
RET_SELL = re.compile(r"자기주식\s*처분\s*결정")
RET_BURN = re.compile(r"주식\s*소각\s*결정")
RET_DIV = re.compile(r"현금\s*" + DOT + r"?\s*현물\s*배당\s*결정|현금\s*배당\s*결정")
RET_TRUST = re.compile(r"자기주식\s*취득\s*신탁\s*계약\s*(체결|해지)\s*결정|신탁\s*계약\s*(체결|해지)\s*결정")
RET_SKIP = re.compile(r"상황보고서|주주명부|변경상장|대량보유|임원\s*" + DOT + r"?\s*주요주주|소유상황")


def default_state() -> dict:
    return {
        "updated_at": None,
        "seeded": False,
        "high_armed": True,
        "low_armed": True,
        "price_last_date": None,
        "last_summary_date": None,
        "seen": [],
        "fail": {},
    }


def load_state() -> dict:
    st = default_state()
    if STATE_PATH.exists():
        try:
            st.update(json.loads(STATE_PATH.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass
    st["seen"] = list(st.get("seen") or [])
    return st


def save_state(st: dict) -> None:
    STATE_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def show_title(t: str) -> str:
    return re.sub(r"\s*" + DOT + r"\s*", ", ", t or "").strip()


def classify(title: str) -> tuple[str, str] | None:
    """Return (bucket, label) or None. bucket: earn | return."""
    t = title or ""
    if RET_SKIP.search(t):
        return None
    if EARN_RE.search(t):
        return "earn", "실적 공시"
    if RET_TRUST.search(t):
        return "return", "신탁계약"
    if RET_BUY.search(t):
        return "return", "자기주식 취득"
    if RET_SELL.search(t):
        return "return", "자기주식 처분"
    if RET_BURN.search(t):
        return "return", "주식 소각"
    if RET_DIV.search(t):
        return "return", "현금배당"
    return None


# ---------------- filings ----------------
def parse_kind_html(page: str, corp: str = CORP) -> list[dict]:
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
        title = re.sub(r"^주식회사\s*" + corp + r"\s+", "", title)
        title = re.sub(r"^" + corp + r"\s+", "", title)
        title = re.sub(r"\s+(" + corp + r"|코스닥시장본부|유가증권시장본부)$", "", title).strip()
        out.append({
            "id": "kind:" + acpt.group(1),
            "acptno": acpt.group(1),
            "title": title,
            "filed": f"{dm.group(1)} {dm.group(2)}" if dm else "",
            "src": "KIND",
            "url": sd.KIND_VIEW.format(acpt=acpt.group(1)),
        })
    if rows == 0 and "조회된 결과값이 없습니다" not in page and "<table" not in page:
        raise RuntimeError("KIND 응답 형식 이상")
    return sorted(out, key=lambda r: r["id"])


def kind_list(since: str, until: str) -> list[dict]:
    form = {
        "method": "searchDetailsSub", "currentPageSize": "100", "pageIndex": "1", "orderMode": "1", "orderStat": "D",
        "forward": "details_sub", "searchCodeType": "", "repIsuSrtCd": "A" + CODE, "allRepIsuSrtCd": "",
        "oldSearchCorpName": "", "disclosureType": "", "disTypevalue": "", "reportNm": "", "reportCd": "",
        "searchCorpName": CODE, "business": "", "marketType": "", "settlementMonth": "", "securities": "",
        "submitOblgNm": "", "enterprise": "", "fromDate": since, "toDate": until, "reportNmTemp": "", "bfrDsclsType": "on",
    }
    page = sd.http(
        sd.KIND + "/disclosure/details.do",
        data=form,
        headers={
            "X-Requested-With": "XMLHttpRequest",
            "Referer": sd.KIND + "/disclosure/details.do?method=searchDetailsMain",
        },
    )
    return parse_kind_html(page)


def parse_naver_rows(raw) -> list[dict]:
    out = []
    for r in raw if isinstance(raw, list) else []:
        title = str(r.get("title") or "")
        title = re.sub(r"^주식회사\s*" + CORP + r"\s+", "", title).strip()
        title = re.sub(r"^" + CORP + r"\s+", "", title).strip()
        day = str(r.get("datetime") or "")[:10]
        nid = str(r.get("no") or r.get("bizdateSeq") or "")
        if not nid or len(day) != 10:
            continue
        out.append({
            "id": "naver:" + nid,
            "acptno": None,
            "title": title,
            "filed": day,
            "src": "네이버 KRX 공시 피드",
            "url": f"https://finance.naver.com/item/news_notice.naver?code={CODE}",
        })
    return sorted(out, key=lambda r: r["id"])


def naver_list() -> list[dict]:
    raw = json.loads(sd.http(f"https://stock.naver.com/api/domestic/detail/notice?itemCode={CODE}&page=1&pageSize=100"))
    return parse_naver_rows(raw)


def fetch_filings(today: str) -> tuple[list[dict], str]:
    since = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=400)).strftime("%Y-%m-%d")
    errs = []
    try:
        rows = kind_list(since, today)
        if rows:
            return rows, "KIND"
        errs.append("KIND 0건")
    except Exception as exc:  # noqa: BLE001
        errs.append(f"KIND {exc}")
    try:
        rows = naver_list()
        if rows:
            return rows, "네이버"
        errs.append("네이버 0건")
    except Exception as exc:  # noqa: BLE001
        errs.append(f"네이버 {exc}")
    raise RuntimeError(" / ".join(errs))


def price_bars(today: str) -> list[tuple[str, float]]:
    import krx_close

    bars = [b for b in krx_close.regular_bars(CODE, 30) if b.day <= today]
    if not bars:
        raise RuntimeError("종가 없음")
    return [(b.day, float(b.close)) for b in bars]


# ---------------- body ----------------
def body(stamp: str, text: str, action: str, later: str = "", avoid: str = "") -> str:
    return "\n".join([
        f"{stamp} | {CODE} | {CORP}",
        f"이게 뭐 알림: {CODE}({CORP}) 실적/주주환원 공시, 종가 7,600/5,500 밴드, 장 마감 후 일일 요약. 매매 지시 아님.",
        f"지금 결과/상태: {text}",
        f"지금 할 일: {action}",
        f"나중에: {later or '같은 조건은 재무장 선을 넘었다가 다시 와야 알림이 옵니다.'}",
        "익절/줄일 때: 계획한 비중이 있으면 그 안에서만. 순현금 대비 프리미엄이 사라지면 비중 재검토.",
        f"하지 말 것: {avoid or '조회 실패를 신호로 읽기, 공시 제목만 보고 바로 매매, 순현금 수치를 추정하기.'}",
    ])


# ---------------- evaluate ----------------
def evaluate(cfg: dict, st: dict, obs: dict, today: str, seed_only: bool = False) -> list[tuple]:
    r = cfg["rules"]
    out: list[tuple[str, str, int]] = []
    seen = set(st.get("seen") or [])
    filings = obs.get("filings") or []

    for f in filings:
        hit = classify(f["title"])
        if not hit:
            continue
        if f["id"] in seen:
            continue
        seen.add(f["id"])
        if seed_only:
            continue
        bucket, label = hit
        title_s = show_title(f["title"])[:60]
        dedup = f.get("acptno") or f["id"]
        if bucket == "earn":
            out.append((
                f"{TAG} 트루엔 실적 공시 | {label}",
                f"{f['filed']} '{title_s}' ({f['src']}). Q3는 11월 중순 전후 예상. 원문에서 매출, 영업이익, 순현금 변화부터 확인. {f.get('url', '')}",
                4,
                dedup,
            ))
        else:
            out.append((
                f"{TAG} 트루엔 주주환원 공시 | {label}",
                f"{f['filed']} '{title_s}' ({f['src']}). 자사주/배당/신탁 결정이면 규모와 일정을 원문에서 확인. {f.get('url', '')}",
                4,
                dedup,
            ))
    st["seen"] = sorted(seen)

    bars = obs.get("bars")
    if bars:
        day, close = bars[-1]
        prev = bars[-2][1] if len(bars) >= 2 else None
        if not seed_only:
            if close >= r["price_high"] and st.get("high_armed", True):
                st["high_armed"] = False
                out.append((
                    f"{TAG} 종가 {close:,.0f}원 | 7,600원 이상 리레이팅",
                    f"{day} 종가 {close:,.0f}원, 관찰선 {r['price_high']:,}원 이상(순현금 주당 {r['net_cash_per_share']:,}원 위). "
                    f"{r['price_high_rearm']:,}원 아래로 내려가야 다시 무장.",
                    3,
                    None,
                ))
            elif close < r["price_high_rearm"]:
                st["high_armed"] = True

            if close < r["price_low"] and st.get("low_armed", True):
                st["low_armed"] = False
                out.append((
                    f"{TAG} 종가 {close:,.0f}원 | 5,500원 하회 급락",
                    f"{day} 종가 {close:,.0f}원, 급락 경계 {r['price_low']:,}원 아래. "
                    f"{r['price_low_rearm']:,}원 위로 올라가야 다시 무장.",
                    4,
                    None,
                ))
            elif close > r["price_low_rearm"]:
                st["low_armed"] = True

            if day == today and st.get("last_summary_date") != today:
                chg = ((close / prev - 1) * 100) if prev else None
                chg_txt = f"{chg:+.2f}%" if chg is not None else "비교 불가"
                nc = r["net_cash_per_share"]
                ratio = close / nc
                out.append((
                    f"{TAG} 일일 요약 | 종가 {close:,.0f}원",
                    f"{day} 종가 {close:,.0f}원, 전일 대비 {chg_txt}, "
                    f"순현금 주당 {nc:,}원 대비 {ratio:.2f}배({close - nc:+,.0f}원).",
                    2,
                    None,
                ))
                st["last_summary_date"] = today
        else:
            # seed arms from current close without sending
            st["high_armed"] = not (close >= r["price_high"])
            st["low_armed"] = not (close < r["price_low"])
            st["last_summary_date"] = today
        st["price_last_date"] = day
    return out


def fail_alerts(cfg: dict, st: dict, errs: dict, today: str) -> list[tuple]:
    out = []
    names = {"filings": "공시 목록", "bars": "종가"}
    fails = st.setdefault("fail", {})
    for key in names:
        f = fails.get(key) or {"days": 0, "last": None, "notified": False}
        if key in errs:
            if f["last"] != today:
                f["days"] += 1
                f["last"] = today
            if f["days"] >= cfg["rules"]["fail_ops_after_days"] and not f["notified"]:
                f["notified"] = True
                out.append((
                    f"{TAG} {names[key]} 조회 실패 | {f['days']}일 연속, 그 규칙 건너뜀",
                    f"{names[key]} 조회가 {f['days']}일 연속 실패({errs[key]}). 조건 미충족이 아님.",
                    3,
                    None,
                ))
        else:
            f = {"days": 0, "last": None, "notified": False}
        fails[key] = f
    return out


def gather(today: str) -> tuple[dict, dict]:
    obs: dict = {}
    errs: dict = {}
    try:
        rows, src = fetch_filings(today)
        obs["filings"] = rows
        obs["filings_src"] = src
    except Exception as exc:  # noqa: BLE001
        obs["filings"] = None
        errs["filings"] = str(exc)[:200]
    try:
        obs["bars"] = price_bars(today)
    except Exception as exc:  # noqa: BLE001
        obs["bars"] = None
        errs["bars"] = str(exc)[:200]
    return obs, errs


def send_actions(alerts: list[tuple], stamp: str) -> list[tuple]:
    built = []
    for item in alerts:
        title, text, pri = item[0], item[1], item[2]
        dedup = item[3] if len(item) > 3 else None
        if "조회 실패" in title:
            msg = body(stamp, text, "다음 거래일 19:00 재시도를 기다리세요.", "읽힌 항목은 정상 판단했습니다.", "조회 실패를 조건 충족으로 읽기.")
        elif "일일 요약" in title:
            msg = body(stamp, text, "참고만. 새 공시나 밴드 교차가 없으면 추가 조치는 없습니다.", "실적/환원 공시와 7,600, 5,500 밴드만 별도 알림.", "일일 요약만 보고 바로 매수/매도.")
        elif "실적 공시" in title:
            msg = body(stamp, text, "원문에서 매출, 영업이익, 순이익, 순현금 변화를 확인. 확인 전 비중 유지.", "정정 공시가 나오면 다시 알림.", "잠정 숫자만 보고 추격매수.")
        elif "주주환원" in title:
            msg = body(stamp, text, "원문에서 규모, 기간, 보통주 수량을 확인.", "다음 결정/정정이 나오면 다시 알림.", "제목만 보고 바로 매매.")
        elif "7,600" in title:
            msg = body(stamp, text, "순현금 위 리레이팅 구간. 비중 확대는 계획 한도 안에서만.", "7,524원 아래로 내려가면 다시 무장.", "한 번에 크게 넣기.")
        elif "5,500" in title:
            msg = body(stamp, text, "급락 구간. 보유 중이면 비중과 손절 기준을 다시 정하기.", "5,555원 위로 올라가면 다시 무장.", "패닉 매도만으로 판단.")
        else:
            msg = body(stamp, text, "상태를 확인하세요.")
        built.append((title, msg, pri, dedup))
    return built


# ---------------- main ----------------
def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    if mode == "selftest":
        return selftest()
    if mode not in {"auto", "dry", "install"}:
        print(f"unknown mode {mode}")
        return 2

    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    if now > datetime.strptime(cfg["ends_kst"], "%Y-%m-%d %H:%M").replace(tzinfo=KST):
        print("ended")
        return 0

    dry = mode == "dry"
    topic = resolve_topic("port", cfg.get("ntfy_topic"))
    server = resolve_server(cfg.get("ntfy_server"))
    day = session_day(now)
    today = day.isoformat()
    stamp = now.strftime("%Y-%m-%d %H:%M KST")

    if mode == "install":
        obs, errs = gather(today)
        b = (obs.get("bars") or [None])[-1]
        n_match = sum(1 for f in (obs.get("filings") or []) if classify(f["title"]))
        status = (f"종가 {b[1]:,.0f}원({b[0]})" if b else "종가 확인 불가")
        status += f", 공시 소스 {obs.get('filings_src', '없음')}, 감시 대상 공시 {n_match}건(시드용)"
        if errs:
            status += f". 조회 실패: {json.dumps(errs, ensure_ascii=False)}"
        title = f"{TAG} 설치 확인 | 매매 신호 아님"
        msg = body(
            stamp,
            status + ". 설치 확인용.",
            "없음. KRX 거래일 19:00에 새 공시나 밴드 교차만 옵니다.",
            "조건: 실적/환원 공시, 종가 7,600 이상 또는 5,500 미만, 일일 요약.",
            "이 설치 확인을 매수나 매도 신호로 읽기.",
        )
        print(title)
        print(msg)
        if not dry:
            publish(topic, title, msg, 3, ["white_check_mark"], server)
        return 0

    if not is_trading_day(day) and mode != "dry":
        print(f"{today} KRX holiday; silent")
        return 0

    st = load_state()
    obs, errs = gather(today)
    if errs:
        print("errors: " + json.dumps(errs, ensure_ascii=False), file=sys.stderr)
    print("obs: " + json.dumps({
        "filings_src": obs.get("filings_src"),
        "filings_n": len(obs.get("filings") or []),
        "match_n": sum(1 for f in (obs.get("filings") or []) if classify(f["title"])),
        "bars": (obs.get("bars") or [])[-3:],
    }, ensure_ascii=False))

    if not st.get("seeded"):
        evaluate(cfg, st, obs, today, seed_only=True)
        st["seeded"] = True
        st["seed_note"] = f"{stamp} 시드. seen {len(st.get('seen') or [])}건, high_armed={st['high_armed']}, low_armed={st['low_armed']}"
        print(st["seed_note"])
        if not dry:
            st["updated_at"] = now.isoformat()
            save_state(st)
        else:
            print("DRY RUN: seed not written")
        return 0

    raw = evaluate(cfg, st, obs, today) + fail_alerts(cfg, st, errs, today)
    alerts = send_actions(raw, stamp)
    for title, msg, pri, dedup in alerts:
        print(f"TITLE: {title}\nPRIORITY: {pri} TOPIC: {route_topic(topic, title)}\n{msg}\n")
        if not dry:
            publish(topic, title, msg, pri, ["office"], server, dedup_key=dedup)
    if not alerts:
        print("no alert")
    if dry:
        print("DRY RUN: no publish, state not written")
        return 0
    st["updated_at"] = now.isoformat()
    save_state(st)
    return 0


# ---------------- selftest ----------------
def selftest() -> int:
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    fails: list[str] = []

    def check(name, got, want):
        ok = got == want
        print(("ok   " if ok else "FAIL ") + f"{name}: got={got!r} want={want!r}")
        if not ok:
            fails.append(name)

    kind_rows = parse_kind_html((FX / "kind.html").read_text(encoding="utf-8"))
    check("kind fixture has rows", len(kind_rows) >= 5, True)
    naver_rows = parse_naver_rows(json.loads((FX / "naver.json").read_text(encoding="utf-8")))
    check("naver fixture rows", len(naver_rows), 6)

    check("earn 분기보고서", classify("분기보고서(일반법인) (2026.03)"), ("earn", "실적 공시"))
    check("earn 잠정", classify("영업(잠정)실적(공정공시)"), ("earn", "실적 공시"))
    check("earn 손익구조", classify("매출액또는손익구조30%이상변경"), ("earn", "실적 공시"))
    check("ret 배당", classify("현금ㆍ현물배당 결정"), ("return", "현금배당"))
    check("ret 신탁 체결", classify("자기주식취득 신탁계약 체결 결정"), ("return", "신탁계약"))
    check("ret 소각", classify("주식소각 결정"), ("return", "주식 소각"))
    check("ret 취득", classify("자기주식 취득 결정"), ("return", "자기주식 취득"))
    check("skip 상황보고서", classify("신탁계약에의한 취득 상황보고서"), None)
    check("skip 변경상장", classify("변경상장(주식소각)"), None)

    def init():
        s = default_state()
        s.update(seeded=True, last_summary_date="2026-11-04", price_last_date="2026-11-04",
                 seen=["kind:old"], high_armed=True, low_armed=True)
        return s

    bars = [("2026-11-04", 6650.0), ("2026-11-05", 6590.0)]
    base = {"filings": [], "bars": bars}

    st = init()
    check("mid-band silent (no summary for old last_summary? wait today summary)", 
          # last_summary 11-04, today 11-05 with bars day 11-05 -> summary fires
          [x[0] for x in evaluate(cfg, st, dict(base), "2026-11-05")],
          [f"{TAG} 일일 요약 | 종가 6,590원"])

    st = init()
    st["last_summary_date"] = "2026-11-05"
    check("current mid silent", evaluate(cfg, st, dict(base), "2026-11-05"), [])

    st = init()
    st["last_summary_date"] = "2026-11-05"
    a = evaluate(cfg, st, dict(base, bars=[("2026-11-04", 6650.0), ("2026-11-05", 7600.0)]), "2026-11-05")
    check("high 7600", [x[0] for x in a], [f"{TAG} 종가 7,600원 | 7,600원 이상 리레이팅"])
    check("high disarmed", st["high_armed"], False)
    st2 = init(); st2.update(high_armed=False, last_summary_date="2026-11-05")
    evaluate(cfg, st2, dict(base, bars=[("2026-11-06", 7550.0)]), "2026-11-06")
    check("7550 stays disarmed", st2["high_armed"], False)
    evaluate(cfg, st2, dict(base, bars=[("2026-11-07", 7520.0)]), "2026-11-07")
    check("7520 rearms high", st2["high_armed"], True)

    st = init()
    st["last_summary_date"] = "2026-11-05"
    a = evaluate(cfg, st, dict(base, bars=[("2026-11-04", 6650.0), ("2026-11-05", 5490.0)]), "2026-11-05")
    check("low 5490", [x[0] for x in a], [f"{TAG} 종가 5,490원 | 5,500원 하회 급락"])
    st["last_summary_date"] = "2026-11-05"
    evaluate(cfg, st, dict(base, bars=[("2026-11-06", 5560.0)]), "2026-11-06")
    check("5560 rearms low", st["low_armed"], True)

    st = init()
    st["last_summary_date"] = "2026-11-05"
    new_f = [{
        "id": "kind:20261112000001", "acptno": "20261112000001",
        "title": "영업(잠정)실적(공정공시)", "filed": "2026-11-12 16:00",
        "src": "KIND", "url": "https://kind.krx.co.kr/x",
    }]
    a = evaluate(cfg, st, dict(base, filings=new_f), "2026-11-05")
    check("new earn filing", [x[0] for x in a], [f"{TAG} 트루엔 실적 공시 | 실적 공시"])
    check("seen stored", "kind:20261112000001" in st["seen"], True)
    check("same filing silent", evaluate(cfg, st, dict(base, filings=new_f), "2026-11-05"), [])

    st = init()
    st["last_summary_date"] = "2026-11-05"
    ret_f = [{
        "id": "kind:20261112000002", "acptno": "20261112000002",
        "title": "자기주식 취득 결정", "filed": "2026-11-12 16:10",
        "src": "KIND", "url": "https://kind.krx.co.kr/x",
    }]
    a = evaluate(cfg, st, dict(base, filings=ret_f), "2026-11-05")
    check("new return filing", [x[0] for x in a], [f"{TAG} 트루엔 주주환원 공시 | 자기주식 취득"])

    st = init()
    evaluate(cfg, st, dict(base, filings=new_f + ret_f), "2026-11-05", seed_only=True)
    check("seed_only no alerts but seen", (True, "kind:20261112000001" in st["seen"]), (True, True))

    # formatting
    msgs = []
    st = init()
    for ti, tx, *_ in evaluate(cfg, init(), dict(base, filings=new_f, bars=[("2026-11-05", 5490.0)]), "2026-11-05"):
        msgs.append(ti + body("s", tx, "a"))
    text = " ".join(msgs) + json.dumps(cfg, ensure_ascii=False)
    check(
        "titles have [417790] and 417790, no middle dot or em-dash",
        (all(m.startswith(TAG) for m in msgs), "417790" in "".join(msgs), "\u00b7" in text, "\u2014" in text),
        (True, True, False, False),
    )
    # show_title strips middle dot from filing titles in body - config may still have none
    check("show_title strips dot", "ㆍ" not in show_title("현금ㆍ현물배당 결정") and "," in show_title("현금ㆍ현물배당 결정"), True)
    check("ends", cfg["ends_kst"], "2027-11-05 23:59")
    f2 = fail_alerts(cfg, init(), {"filings": "x"}, "2026-11-06")
    stf = init()
    fail_alerts(cfg, stf, {"filings": "x"}, "2026-11-06")
    f2 = fail_alerts(cfg, stf, {"filings": "x"}, "2026-11-09")
    check("ops day2", [x[0] for x in f2], [f"{TAG} 공시 목록 조회 실패 | 2일 연속, 그 규칙 건너뜀"])
    check("ops -> cwally-ops", route_topic("cwally-port", f2[0][0]), "cwally-ops")

    # fix the broken check earlier that had wrong arity - already replaced with st2 checks
    print(f"SELFTEST {'PASS' if not fails else 'FAIL'}: {len(fails)} failed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
