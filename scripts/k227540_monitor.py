#!/usr/bin/env python3
"""Samsung Biologics 2026 Q3 earnings -> TIGER 200 Healthcare (227540) first-tranche buy verdict.

One-shot alert (configs/k227540.json oneshot_kst, 2026-10-21 ~ 10-23 KST).

1. Finds the KRX 'consolidated preliminary results' notice of 207940 for Q3 2026 and compares
   operating profit with the consensus (live Naver consensus if still posted, else config).
2. Picks the pre-announcement base close of 227540: the announcement-day close when the notice
   came after the close (>= 15:30 KST, or time unknown), else the previous trading-day close.
3. Checks the first 1-2 finalized closes after that base, plus Naver's closing premium/discount
   to NAV, and sends one verdict for the first tranche (1/3 of the plan): execute / wait / hold.

Sends at most: earnings stage (only while no post close exists yet), one wait notice, one verdict.

modes: auto (default) | dry (print only: no ntfy, no state write) | force (resend the verdict)
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from naver_api import get_json, news, notices, num  # noqa: E402
from ntfy_util import publish, resolve_server, resolve_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
CFG_PATH = ROOT / "configs" / "k227540.json"
STATE_PATH = ROOT / "state" / "k227540.json"
NOTICE_PAGE = "https://m.stock.naver.com/domestic/stock/207940/notice"
CLOSE_FINAL_HHMM = "15:45"  # a same-day close is treated as final only after this KST time

EARN_TITLE = re.compile(r"연결재무제표\s*기준\s*영업\s*\(잠정\)\s*실적")
Q3_SPAN = re.compile(
    r"2026[-.]\s*0?7[-.]\s*0?1\s*[~∼-]\s*2026[-.]\s*0?9[-.]\s*30"
    r"|'?\s*26\s*\.?\s*3\s*Q|2026\s*\.?\s*3\s*Q|3\s*Q\s*'?\s*26|2026\s*년\s*3\s*분기"
)
UNIT_EOK = {"조원": 10000.0, "억원": 1.0, "백만원": 0.01, "천원": 0.00001, "원": 0.00000001}


# ---------- state ----------

def read_state() -> dict:
    base = {
        "earn_notified": False,
        "wait_notified": False,
        "verdict_notified": False,
        "last_problem_key": None,
    }
    if STATE_PATH.exists():
        try:
            base.update(json.loads(STATE_PATH.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass
    return base


def save_state(state: dict, now: datetime) -> None:
    state["updated_at"] = now.isoformat(timespec="seconds")
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# ---------- earnings notice ----------

def notice_time(item: dict) -> tuple[str, str | None]:
    """(YYYY-MM-DD, HH:MM or None) from the Naver notice datetime."""
    raw = str(item.get("datetime") or "")
    d = re.search(r"(\d{4})[-.](\d{2})[-.](\d{2})", raw)
    t = re.search(r"[T\s](\d{2}):(\d{2})", raw)
    day = f"{d.group(1)}-{d.group(2)}-{d.group(3)}" if d else ""
    return day, (f"{t.group(1)}:{t.group(2)}" if t else None)


def find_earnings(rows: list[dict], cfg: dict) -> dict | None:
    for r in rows:  # newest first, so a correction wins
        day, _ = notice_time(r)
        if (EARN_TITLE.search(r["title"]) and day and cfg["window_start"] <= day <= cfg["window_end"]
                and Q3_SPAN.search(r["text"])):
            return r
    return None


def first_num(text: str, pattern: str) -> float | None:
    hit = re.search(pattern, text)
    return num(hit.group(1)) if hit else None


def parse_earnings(item: dict) -> dict:
    body = item["text"]
    unit = re.search(r"단위\s*[:：]?\s*(조원|억원|백만원|천원|원)", body)
    factor = UNIT_EOK[unit.group(1)] if unit else 1.0
    row = r"\s*(?:\([^)]*\))?\s+당해실적\s+(-?[\d,]+(?:\.\d+)?)"
    sales = first_num(body, r"매출액" + row)
    op = first_num(body, r"영업이익" + row)
    ni = first_num(body, r"(?:지배\s*기업\s*소유주\s*지분\s*)?당기순이익" + row)
    out = {k: (round(v * factor) if v is not None else None) for k, v in {"sales": sales, "op": op, "ni": ni}.items()}
    # sanity bands (억원): quarterly sales 0.8~3조, operating profit -0.5~1.5조
    if out["sales"] is not None and not 8_000 <= out["sales"] <= 30_000:
        out["sales"] = None
    if out["op"] is not None and not -5_000 <= out["op"] <= 15_000:
        out["op"] = None
    if out["ni"] is not None and not -5_000 <= out["ni"] <= 15_000:
        out["ni"] = None
    out["unit"] = unit.group(1) if unit else "억원(추정)"
    return out


def live_consensus(code: str) -> int | None:
    """Naver finance/quarter 2026.09 operating-profit consensus (억원), only while still a consensus."""
    try:
        d = get_json(f"https://m.stock.naver.com/api/stock/{code}/finance/quarter")["financeInfo"]
        key = next((t["key"] for t in d["trTitleList"] if str(t.get("key", "")).startswith("202609")), None)
        if not key or next(t for t in d["trTitleList"] if t["key"] == key).get("isConsensus") != "Y":
            return None
        row = next(r for r in d["rowList"] if r.get("title") == "영업이익")
        v = num(row["columns"][key].get("value"))
        return int(v) if v is not None and 2_000 <= v <= 15_000 else None
    except Exception as exc:  # optional refresh; config value is the fallback
        print(f"live consensus skipped: {type(exc).__name__}", file=sys.stderr)
        return None


def guidance_flags(cfg: dict, since: str) -> dict:
    """Keyword-only suspicion from Naver news on/after the announcement day."""
    out = {"cut": None, "up": None, "error": None}
    try:
        cut_re = re.compile(cfg["guidance_watch"]["cut_re"])
        up_re = re.compile(cfg["guidance_watch"]["up_re"])
        since_c = since.replace("-", "")
        for it in news(cfg["earn_code"], 30):
            stamp = re.sub(r"\D", "", str(it.get("datetime") or ""))[:8]  # 202610211530 / 2026.10.21 ...
            if len(stamp) == 8 and stamp < since_c:
                continue
            text = f"{it['title']} {it['body']}"
            if out["cut"] is None and cut_re.search(text):
                out["cut"] = it["title"][:60]
            if out["up"] is None and up_re.search(text):
                out["up"] = it["title"][:60]
    except Exception as exc:
        out["error"] = type(exc).__name__
        print(f"guidance scan skipped: {exc}", file=sys.stderr)
    return out


# ---------- ETF market data ----------

def etf_market(code: str) -> dict:
    basic = get_json(f"https://m.stock.naver.com/api/stock/{code}/basic")
    integ = get_json(f"https://m.stock.naver.com/api/stock/{code}/integration")
    ind = (integ or {}).get("etfKeyIndicator") or {}
    price = num(basic.get("closePrice"))
    nav = num(ind.get("nav"))
    rate = num(ind.get("deviationRate"))
    if rate is not None:  # Naver sends an unsigned rate and the sign separately
        premium = -abs(rate) if str(ind.get("deviationSign") or "").strip() == "-" else abs(rate)
    elif price and nav:
        premium = (price - nav) / nav * 100.0
    else:
        premium = None
    rows = []
    for r in get_json(f"https://m.stock.naver.com/api/stock/{code}/price?pageSize=20&page=1") or []:
        d = str(r.get("localTradedAt") or "")[:10]
        c = num(r.get("closePrice"))
        if d and c is not None:
            rows.append({"date": d, "close": c})
    rows.sort(key=lambda x: x["date"])
    return {
        "date": str(basic.get("localTradedAt") or "")[:10],
        "price": price,
        "nav": nav,
        "premium": premium,
        "status": basic.get("marketStatus"),
        "rows": rows,
    }


def split_closes(rows: list[dict], ann_day: str, ann_hhmm: str | None, cfg: dict, now: datetime):
    """(base_row, [post rows that are final]) around the announcement."""
    after_close = ann_hhmm is None or ann_hhmm >= cfg["rules"]["after_close_hhmm"]
    pre = [r for r in rows if (r["date"] <= ann_day if after_close else r["date"] < ann_day)]
    if not pre:
        return None, [], after_close
    base = pre[-1]
    today = now.strftime("%Y-%m-%d")
    final_today = now.strftime("%H:%M") >= CLOSE_FINAL_HHMM
    post = [r for r in rows if r["date"] > base["date"] and (r["date"] < today or (r["date"] == today and final_today))]
    return base, post[: int(cfg["rules"]["post_days_max"])], after_close


# ---------- copy ----------

def eok(v: int | None) -> str:
    if v is None:
        return "미공시"
    return f"{v / 10000:.2f}조원" if abs(v) >= 10000 else f"{v:,}억원"


def won(v: float | None) -> str:
    return "확인 불가" if v is None else f"{v:,.0f}원"


def pct(v: float | None) -> str:
    return "확인 불가" if v is None else f"{v:+.2f}%"


def send(cfg: dict, title: str, lines: list[str], priority: int, dry: bool) -> None:
    if dry:
        print("DRY RUN (would send)", title, *lines, "dry-run; no ntfy", sep="\n")
        return
    publish(resolve_topic("port", cfg.get("ntfy_topic")), title, "\n".join(lines), priority,
            ["pill", "chart_with_upwards_trend"], resolve_server(cfg.get("ntfy_server")))


def problem(cfg: dict, state: dict, now: datetime, kind: str, detail: str, dry: bool) -> None:
    key = f"{kind}:{detail}"
    if state.get("last_problem_key") == key:
        print(f"problem already reported: {kind}")
        return
    title = f"[227540] 삼바 3Q {kind} | 1차 판정 유보"
    lines = [
        "이게 뭐 알림: 삼성바이오로직스 3분기 실적과 227540 종가로 1차 분할매수(1/3) 실행 여부를 판정하는 일회 알림입니다.",
        f"지금 결과/상태: {kind}. {detail}. 조회 {now:%Y-%m-%d %H:%M} KST. 공시: {NOTICE_PAGE}",
        "지금 할 일: 공시 원문과 HTS 종가, 괴리율을 직접 확인하기 전에는 1차 매수를 하지 마세요.",
        "나중에: 남은 원샷 시각에 다시 확인합니다. 마지막은 10/23 15:52 KST입니다.",
        "익절/줄일 때: 이 알림은 진입만 봅니다. 보유분은 직전 스윙 저점 이탈 기준을 따르세요.",
        "하지 말 것: 조회 실패를 매수나 매도 신호로 읽지 마세요.",
    ]
    send(cfg, title, lines, 3, dry)
    if not dry:
        state["last_problem_key"] = key
        save_state(state, now)


def earn_line(e: dict, cons: int, cons_src: str, gap: float, ann_day: str, ann_hhmm: str | None) -> str:
    return (f"삼바 3분기 영업이익 {eok(e['op'])} (컨센서스 {eok(cons)}, {cons_src}, {gap:+.1f}%), "
            f"매출 {eok(e['sales'])}. 공시 {ann_day} {ann_hhmm or '시각 미표기'}.")


def guide_line(g: dict) -> str:
    if g["cut"]:
        return f"가이던스 하향 의심 기사: {g['cut']}."
    if g["up"]:
        return f"가이던스 상향 기사: {g['up']}."
    if g["error"]:
        return "가이던스 기사 조회 실패, 원문 확인 필요."
    return "가이던스 변경 기사 없음(키워드 기준)."


def price_line(m: dict, base: dict, post: list[dict], band: float) -> str:
    seq = ", ".join(f"{r['date'][5:]} {won(r['close'])}" for r in post) or "아직 없음"
    inside = m["premium"] is not None and abs(m["premium"]) <= band
    return (f"227540 기준 종가 {base['date'][5:]} {won(base['close'])}, 발표 후 종가 {seq}. "
            f"괴리율 {pct(m['premium'])} (NAV {won(m['nav'])}, {m['date'] or '날짜 미상'}, "
            f"기준 ±{band:.1f}% {'안' if inside else '밖'}).")


# ---------- main ----------

def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    if mode not in {"auto", "dry", "force"}:
        raise SystemExit(f"unknown mode: {mode}")
    dry = mode == "dry"
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    state = read_state()
    now = datetime.now(KST)
    day = now.strftime("%Y-%m-%d")
    if dry:
        print("DRY RUN: no ntfy, no state write")
    if now.year != int(cfg["year_guard"]) or not (cfg["window_start"] <= day <= cfg["window_end"]):
        if not dry:
            print("outside 2026 one-shot window; silent")
            return 0
        print("outside window; dry run continues for wiring check")
    if state.get("verdict_notified") and mode == "auto":
        print("verdict already notified; silent")
        return 0
    final = now >= datetime.strptime(cfg["final_kst"], "%Y-%m-%d %H:%M").replace(tzinfo=KST)
    rules = cfg["rules"]
    band = float(rules["premium_band_pct"])

    try:
        rows = notices(cfg["earn_code"], 30)
    except Exception as exc:
        problem(cfg, state, now, "조회 실패", f"공시 목록 {type(exc).__name__}", dry)
        return 0
    if not rows:
        problem(cfg, state, now, "조회 실패", "공시 목록이 비었습니다", dry)
        return 0
    earn = find_earnings(rows, cfg)
    print(f"notices={len(rows)} earnings={earn and earn['id']}")
    if not earn:
        if final:
            problem(cfg, state, now, "미게시", "2026년 3분기 잠정실적 공시를 찾지 못했습니다", dry)
        else:
            print("earnings not posted yet; silent")
        return 0

    e = parse_earnings(earn)
    ann_day, ann_hhmm = notice_time(earn)
    print(f"parsed earnings={e} at {ann_day} {ann_hhmm}")
    if e["op"] is None:
        problem(cfg, state, now, "숫자 미확인", f"실적 공시 {earn['id']}에서 영업이익을 읽지 못했습니다", dry)
        return 0
    live = live_consensus(cfg["earn_code"])
    cons = live or int(cfg["consensus"]["op_eok"])
    cons_src = "네이버 컨센서스" if live else f"에프앤가이드 {cfg['consensus']['as_of']}"
    gap = (e["op"] - cons) / cons * 100.0
    g = guidance_flags(cfg, ann_day)
    earn_ok = e["op"] >= cons * float(rules["op_ok_ratio"]) and not g["cut"]
    soft = e["op"] >= cons * float(rules["op_soft_ratio"])
    grade = ("부합 이상" if e["op"] >= cons else ("소폭 미달" if soft else "미달"))
    head = earn_line(e, cons, cons_src, gap, ann_day, ann_hhmm) + " " + guide_line(g)

    try:
        m = etf_market(cfg["etf_code"])
    except Exception as exc:
        problem(cfg, state, now, "조회 실패", f"227540 시세 {type(exc).__name__}", dry)
        return 0
    base, post, after_close = split_closes(m["rows"], ann_day, ann_hhmm, cfg, now)
    if base is None:
        problem(cfg, state, now, "숫자 미확인", "227540 발표 전 종가를 찾지 못했습니다", dry)
        return 0
    print(f"base={base} post={post} premium={m['premium']} after_close={after_close}")
    pline = price_line(m, base, post, band)
    above = [r for r in post if r["close"] > base["close"]]
    latest = post[-1] if post else None
    inside = m["premium"] is not None and abs(m["premium"]) <= band

    common_tail = [
        "익절/줄일 때: 1차 진입 뒤 일봉 직전 스윙 저점을 종가로 이탈하면 추가 매수를 멈추고 재판단하세요.",
        "하지 말 것: 시장가 주문, 괴리 ±0.5% 밖 체결, 한 번에 전액 투입, 253280(괴리 +5%대) 대체 매수.",
    ]

    if not earn_ok:
        why = "가이던스 하향 의심 기사" if g["cut"] else f"영업이익 {grade}"
        title = f"[227540] 삼바 3Q {why} | 1차 매수 보류"
        lines = [
            "이게 뭐 알림: 삼성바이오로직스 3분기 실적과 227540 종가로 1차 분할매수(1/3) 실행 여부를 판정합니다.",
            f"지금 결과/상태: 결론 1차 보류. {head} {pline}",
            ("지금 할 일: 1차 매수를 하지 마세요. 기사 원문과 회사 IR 자료에서 연간 매출 가이던스가 실제로 낮아졌는지 확인하세요."
             if g["cut"] else
             "지금 할 일: 1차 매수를 하지 마세요. 원문(공시, 컨콜 자료)에서 미달 이유가 파업, 환율 같은 일회성인지 확인하세요."),
            "나중에: 일회성이 확인되고 일봉 직전 스윙 고점 돌파와 20일선 안착이 나오면 2차 기준으로 다시 봅니다.",
            *common_tail,
        ]
        verdict = "hold_earn"
        pri = 4
    elif above:
        hit = above[0]
        when = "다음 거래일 장중" if not inside or hit is not latest else "다음 거래일"
        order = ("종가 괴리가 기준 안이라 다음 거래일 지정가로 1/3을 넣으세요."
                 if inside else f"종가 괴리 {pct(m['premium'])}가 기준 밖이라, {when} iNAV 대비 ±{band:.1f}% 안일 때만 지정가로 1/3을 넣으세요.")
        title = f"[227540] 삼바 3Q {grade}, 종가 기준 위 | 1차 1/3 실행"
        lines = [
            "이게 뭐 알림: 삼성바이오로직스 3분기 실적과 227540 종가로 1차 분할매수(1/3) 실행 여부를 판정합니다.",
            f"지금 결과/상태: 결론 1차 실행. {hit['date'][5:]} 종가 {won(hit['close'])}, 기준 {won(base['close'])}보다 높습니다. {head} {pline}",
            f"지금 할 일: {order}",
            "나중에: 2차 1/3은 일봉 직전 스윙 고점 돌파와 20일선 안착(또는 1차 매수가 대비 -5~7% 눌림), 3차는 주봉 추세 확인 뒤입니다.",
            *common_tail,
        ]
        verdict = "execute"
        pri = 4
    elif len(post) >= int(rules["post_days_max"]) or (final and post):
        title = f"[227540] 삼바 3Q {grade}, 종가 기준 아래 | 1차 매수 보류"
        lines = [
            "이게 뭐 알림: 삼성바이오로직스 3분기 실적과 227540 종가로 1차 분할매수(1/3) 실행 여부를 판정합니다.",
            f"지금 결과/상태: 결론 1차 보류. 실적은 통과했지만 발표 후 {len(post)}거래일 종가가 기준 {won(base['close'])} 위를 지키지 못했습니다. {head} {pline}",
            "지금 할 일: 1차 매수를 하지 마세요. 실적이 가격에 먹히지 않는 구간입니다.",
            "나중에: 일봉 직전 스윙 고점 돌파와 20일선 안착이 나오면 그때 1차(추세 확인형)로 들어갑니다.",
            *common_tail,
        ]
        verdict = "hold_price"
        pri = 3
    elif post:  # day 1 closed at or below base, day 2 still pending
        if state.get("wait_notified") and mode == "auto":
            print("wait already notified; silent")
            return 0
        title = f"[227540] 삼바 3Q {grade}, 1일차 종가 기준 이하 | 내일 종가까지 대기"
        lines = [
            "이게 뭐 알림: 삼성바이오로직스 3분기 실적과 227540 종가로 1차 분할매수(1/3) 실행 여부를 판정합니다.",
            f"지금 결과/상태: 대기. 실적은 통과, {post[0]['date'][5:]} 종가 {won(post[0]['close'])}, 기준 {won(base['close'])} 이하입니다. {head} {pline}",
            "지금 할 일: 오늘은 사지 마세요. 내일 종가가 기준 위인지 보고 15:52에 최종 판정을 보냅니다.",
            "나중에: 내일도 기준 이하면 1차 보류, 2차 추세 확인 기준으로 넘어갑니다.",
            *common_tail,
        ]
        send(cfg, title, lines, 3, dry)
        if not dry:
            state.update({"wait_notified": True, "last_problem_key": None, "earn_id": earn["id"],
                          "op_eok": e["op"], "consensus_eok": cons, "base": base, "post": post})
            save_state(state, now)
        return 0
    else:  # earnings passed, no post close yet
        if state.get("earn_notified") and mode == "auto":
            print("earnings stage already notified; silent")
            return 0
        if final:
            problem(cfg, state, now, "숫자 미확인", "발표 후 227540 종가를 찾지 못했습니다", dry)
            return 0
        nxt = "내일" if after_close else "오늘 장 마감 뒤"
        title = f"[227540] 삼바 3Q {grade} ({gap:+.0f}%) | 1차는 {nxt} 종가로 판정"
        lines = [
            "이게 뭐 알림: 삼성바이오로직스 3분기 실적과 227540 종가로 1차 분할매수(1/3) 실행 여부를 판정합니다.",
            f"지금 결과/상태: 실적 조건 통과. {head} 기준 종가 {base['date'][5:]} {won(base['close'])}.",
            f"지금 할 일: 아직 사지 마세요. {nxt} 종가가 {won(base['close'])} 위면 15:52에 1차 실행 알림을 보냅니다.",
            "나중에: 1~2거래일 안에 기준 위 종가가 없으면 1차 보류로 판정합니다.",
            *common_tail,
        ]
        send(cfg, title, lines, 3, dry)
        if not dry:
            state.update({"earn_notified": True, "last_problem_key": None, "earn_id": earn["id"],
                          "op_eok": e["op"], "consensus_eok": cons, "base": base})
            save_state(state, now)
        return 0

    send(cfg, title, lines, pri, dry)
    if not dry:
        state.update({
            "verdict_notified": True,
            "earn_notified": True,
            "last_problem_key": None,
            "earn_id": earn["id"],
            "op_eok": e["op"],
            "consensus_eok": cons,
            "base": base,
            "post": post,
            "premium": m["premium"],
            "verdict": verdict,
        })
        save_state(state, now)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
