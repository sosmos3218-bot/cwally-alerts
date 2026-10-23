#!/usr/bin/env python3
"""[KR-HC] 헬스케어 ETF 3종 + 셀트리온/삼성바이오로직스 주간 비교 요약 (주 1번).

- 언제: 기존 k203780 슬롯(평일 19:00)에 부착. 그날(session_day)이 그 주(월~금) 마지막 KRX 거래일이고
  장 마감(15:40) 뒤일 때 그 주를 보낸다. 같은 주는 한 번만(state last_sent_week = 그 주 월요일).
  빠진 주는 다음 실행(19:00 또는 morning 09:00 슬롯)에서 한 번 늦게 보낸다. 마지막 거래일 뒤 catchup_days 안까지만.
- 무엇:
  1) 143860 / 227540 / 248270 주간 등락 (그 주 마지막 거래일 종가 / 직전 주 마지막 거래일 종가)과
     괴리율(참고 지표). NAV 는 미래에셋 기준가격 표(날짜 있음) -> 네이버 nav(203780 과 같은 날짜 확인).
  2) 원/달러 주간 변화 (하나은행 고시 매매기준율, 네이버). 248270 은 환헤지 없음.
  3) 셀트리온 068270 / 삼성바이오로직스 207940 주간 등락.
  실패 항목은 확인 불가. 매매 신호 아님.
modes: auto(기본) | dry(발송 없음, state 안 씀). dry 에서만 --as-of "YYYY-MM-DD HH:MM" (KST) 로 실행 시각을 바꿔 볼 수 있다.
"""
from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic, route_topic  # noqa: E402
from kr_calendar import KST, is_trading_day, session_day  # noqa: E402
from k203780_monitor import NA, get, mirae_nav, naver_nav, num, usdkrw  # noqa: E402  (읽기 전용 재사용)

NAME = "krhc"
CFG_PATH = ROOT / "configs" / f"{NAME}.json"
STATE_PATH = ROOT / "state" / f"{NAME}.json"


def load_state() -> dict:
    base = {"updated_at": None, "last_sent_week": None, "sent": {}, "fail_notice": {}}
    if STATE_PATH.exists():
        try:
            base.update(json.loads(STATE_PATH.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass
    return base


# ---------------- 주 계산 ----------------
def week_last_trading_day(monday: date) -> date | None:
    for i in range(4, -1, -1):
        d = monday + timedelta(days=i)
        if is_trading_day(d):
            return d
    return None


def last_trading_before(d: date) -> date:
    d -= timedelta(days=1)
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d


def target_week(now: datetime, close_hm: str) -> tuple[date, date] | None:
    """(그 주 월요일, 그 주 마지막 거래일). 이번 주가 끝났으면 이번 주, 아니면 직전 주."""
    sess = session_day(now)
    mon = sess - timedelta(days=sess.weekday())
    hh, mm = (int(x) for x in close_hm.split(":"))
    for _ in range(3):
        last = week_last_trading_day(mon)
        if last is not None:
            done = sess > last or (sess == last and (now.date() > sess or (now.hour, now.minute) >= (hh, mm)))
            if done:
                return mon, last
        mon -= timedelta(days=7)
    return None


# ---------------- 데이터 ----------------
def daily_closes(code: str) -> dict[str, float]:
    raw = json.loads(get(f"https://m.stock.naver.com/api/stock/{code}/price?pageSize=20&page=1"))
    out: dict[str, float] = {}
    for r in raw if isinstance(raw, list) else []:
        d = str(r.get("localTradedAt") or "")[:10]
        c = num(r.get("closePrice"))
        if len(d) == 10 and c:
            out[d] = c
    return out


def weekly(code: str, last: date, prev_last: date) -> tuple[dict, dict[str, float]]:
    closes = daily_closes(code)
    a, b = closes.get(last.isoformat()), closes.get(prev_last.isoformat())
    if a is None:
        raise RuntimeError(f"{last:%m/%d} 종가 없음")
    if b is None:
        raise RuntimeError(f"{prev_last:%m/%d} 종가 없음")
    return {"close": a, "prev": b, "pct": (a / b - 1) * 100}, closes


def etf_nav(etf: dict, last: date, closes: dict[str, float]) -> tuple[float, date, str]:
    """(nav, nav 날짜, 출처). 마지막 거래일 NAV 우선, 없으면 직전 거래일 NAV (날짜 표시)."""
    errs = []
    try:
        rows = mirae_nav(etf["isin"], last)
        for d in (last, last_trading_before(last)):
            r = rows.get(d.isoformat())
            if r and r.get("nav"):
                return r["nav"], d, "미래에셋"
        errs.append("미래에셋 표에 그날 NAV 없음")
    except Exception as exc:  # noqa: BLE001
        errs.append(f"미래에셋 {exc}")
    d1 = last_trading_before(last)
    d2 = last_trading_before(d1)
    for d, prev in ((last, d1), (d1, d2)):
        c, pc = closes.get(d.isoformat()), closes.get(prev.isoformat())
        if c is None:
            continue
        try:
            return naver_nav(etf["code"], c, pc, None), d, "네이버"
        except Exception as exc:  # noqa: BLE001
            errs.append(f"네이버({d:%m/%d}) {exc}")
    raise RuntimeError(" / ".join(errs))


def fx_on_or_before(rows: dict[str, dict], d: date) -> tuple[str, float] | None:
    keys = sorted(k for k in rows if k <= d.isoformat())
    return (keys[-1], rows[keys[-1]]["px"]) if keys else None


def pct_txt(p: float) -> str:
    return f"{p:+.2f}%"


# ---------------- 메시지 ----------------
def build(cfg: dict, mon: date, last: date, now: datetime, late: bool) -> tuple[str, str, int, list[str], int, list[str]]:
    prev_last = last_trading_before(mon)
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    parts: list[str] = []
    ok = 0
    errors: list[str] = []
    rank: list[tuple[float, str]] = []
    for i, e in enumerate(cfg["etfs"], 1):
        line = f"{i}) {e['code']} {e['name']}: "
        try:
            w, closes = weekly(e["code"], last, prev_last)
            ok += 1
            rank.append((w["pct"], e["code"]))
            line += f"{w['close']:,.0f}원, 주간 {pct_txt(w['pct'])} (직전 주 {w['prev']:,.0f}원)"
            try:
                nav, nd, src = etf_nav(e, last, closes)
                prem = (closes[nd.isoformat()] / nav - 1) * 100
                when = f"{nd:%m/%d}" + ("" if nd == last else f" 기준, {last:%m/%d} NAV 아직 없음")
                line += f", 괴리율 {prem:+.2f}% (NAV {nav:,.2f}, {when}, {src}, 참고 지표)"
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{e['code']} NAV: {exc}")
                line += f", 괴리율 {NA}"
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{e['code']} 시세: {exc}")
            line += f"주간 등락 {NA}, 괴리율 {NA}"
        parts.append(line)
    if len(rank) >= 2:
        parts.append("ETF 주간 등락 순: " + " > ".join(f"{c} {pct_txt(p)}" for p, c in sorted(rank, reverse=True)))
    n = len(cfg["etfs"]) + 1
    fx_note = next((e.get("fx_note") for e in cfg["etfs"] if e.get("fx_note")), "")
    try:
        rows = usdkrw()
        a, b = fx_on_or_before(rows, last), fx_on_or_before(rows, prev_last)
        if not a or not b or a[0] != last.isoformat():
            raise RuntimeError(f"{last:%m/%d} 고시값 없음")
        ok += 1
        live = " (조회 시점 최신 고시)" if a[0] == now.date().isoformat() else ""
        parts.append(f"{n}) 원/달러(하나은행 매매기준율): {b[1]:,.1f} → {a[1]:,.1f}{live}, 주간 {pct_txt((a[1] / b[1] - 1) * 100)}. "
                     f"248270 은 {fx_note}.")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"원/달러: {exc}")
        parts.append(f"{n}) 원/달러(하나은행 매매기준율): 주간 변화 {NA}. 248270 은 {fx_note}.")
    for j, s in enumerate(cfg["stocks"], n + 1):
        try:
            w, _ = weekly(s["code"], last, prev_last)
            ok += 1
            parts.append(f"{j}) {s['name']} {s['code']}: {w['close']:,.0f}원, 주간 {pct_txt(w['pct'])}")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{s['code']}: {exc}")
            parts.append(f"{j}) {s['name']} {s['code']}: 주간 등락 {NA}")
    wk = f"{mon:%m/%d}~{last:%m/%d}"
    title = f"[KR-HC] {wk} 주간 비교{' (늦은 발송)' if late else ''} | 매매 신호 아님"
    status = (f"기준 {prev_last:%m/%d} 종가 → {last:%m/%d} 종가. " + " / ".join(parts))
    msg = "\n".join([
        f"{stamp} | KR-HC | 헬스케어 주간 비교",
        "이게 뭐 알림: TIGER 헬스케어 ETF 3종과 원/달러, 셀트리온, 삼성바이오로직스의 한 주 등락 비교. 그 주 마지막 거래일 19:00 무렵 주 1번. 매매 신호 아님.",
        f"지금 결과/상태: {status}",
        f"지금 할 일: 순위만 훑어보기. 순위 판단은 본인 기준({cfg['user_view']}) 그대로.",
        "나중에: 다음 주 마지막 거래일 19:00 무렵 다시 옵니다. 빠진 주는 다음 실행 때 한 번 늦게 옵니다.",
        "익절/줄일 때: 이 요약만으로 정하지 않기. 미리 정한 비중 기준대로.",
        "하지 말 것: 한 주 등락으로 갈아타기, 괴리율 하나만 보고 사거나 팔기, 확인 불가 항목을 0이나 정상으로 읽기.",
    ])
    return title, msg, 3, ["bar_chart"], ok, errors


def main() -> int:
    args = sys.argv[1:]
    mode = (args[0] if args and not args[0].startswith("--") else "auto").lower()
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    if "--as-of" in args:
        if mode != "dry":
            print("--as-of 는 dry 에서만")
            return 2
        now = datetime.strptime(args[args.index("--as-of") + 1], "%Y-%m-%d %H:%M").replace(tzinfo=KST)
    if mode not in {"auto", "dry"}:
        print(f"unknown mode {mode}")
        return 2
    dry = mode == "dry"
    topic = resolve_topic("port", cfg.get("ntfy_topic"))
    server = resolve_server(cfg.get("ntfy_server"))
    state = load_state()
    tw = target_week(now, cfg.get("market_close_kst", "15:40"))
    print(f"now={now:%Y-%m-%d %H:%M} session={session_day(now)} target={tw} last_sent_week={state.get('last_sent_week')}")
    if tw is None:
        print("no target week")
        return 0
    mon, last = tw
    key = mon.isoformat()
    if state.get("last_sent_week") and state["last_sent_week"] >= key:
        print("already sent for this week")
        return 0
    if (session_day(now) - last).days > int(cfg.get("catchup_days", 7)):
        print("target week too old for catch-up, skip")
        return 0
    late = session_day(now) != last
    title, msg, pri, tags, ok, errors = build(cfg, mon, last, now, late)
    if errors:
        print("errors: " + " | ".join(errors), file=sys.stderr)
    if ok == 0:
        today = now.strftime("%Y-%m-%d")
        title = "[KR-HC] 주간 비교 조회 실패 | 주간 요약 못 보냄"
        msg = "\n".join([
            f"{now:%Y-%m-%d %H:%M KST} | KR-HC | 헬스케어 주간 비교",
            "이게 뭐 알림: 헬스케어 주간 비교 요약의 데이터 조회 실패 안내.",
            f"지금 결과/상태: {mon:%m/%d}~{last:%m/%d} 주 시세와 원/달러 모두 {NA} ({' | '.join(errors)[:300]}).",
            "지금 할 일: 필요하면 증권사 앱에서 직접 보세요.",
            "나중에: 다음 실행 때 다시 시도합니다(마지막 거래일 뒤 7일 안).",
            "익절/줄일 때: 이번 안내로 비중을 바꾸지 마세요.",
            "하지 말 것: 조회 실패를 변동 없음으로 읽기.",
        ])
        fn = state.setdefault("fail_notice", {})
        print(f"TITLE: {title}\nTOPIC: {route_topic(topic, title)}\n{msg}")
        if dry:
            print("DRY RUN: no publish, state not written")
            return 0
        if fn.get("all") != today:
            publish(topic, title, msg, 3, ["warning"], server)
            fn["all"] = today
        state["updated_at"] = now.isoformat()
        STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return 0
    print(f"TITLE: {title}\nPRIORITY: {pri} TOPIC: {route_topic(topic, title)}\n{msg}\n")
    if dry:
        print("DRY RUN: no publish, state not written")
        return 0
    publish(topic, title, msg, pri, tags, server)
    state["last_sent_week"] = key
    state.setdefault("sent", {})[key] = {"last_trading_day": last.isoformat(), "sent_at": now.isoformat(), "late": late,
                                         "items_ok": ok, "errors": errors[:6]}
    state["sent"] = dict(sorted(state["sent"].items())[-12:])
    state["updated_at"] = now.isoformat()
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
