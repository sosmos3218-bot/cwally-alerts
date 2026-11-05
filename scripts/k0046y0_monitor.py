#!/usr/bin/env python3
"""[0046Y0] ACE 미국배당퀄리티 KRX 확정 종가 분할 매수 검토 알림.

선 (configs/k0046y0.json lines): 종가 <= 11,900 p4 (12,260 이상 종가 뒤 재무장),
종가 <= 11,200 p5 (11,540 이상 종가 뒤 재무장). 선마다 1번.
20일선 회복 (recovery): 1차나 2차가 울린 뒤에만 무장, 그 뒤 처음 종가 > SMA20 이면 p3 1번.
종가: scripts/krx_close.py regular_bars (Daum KRX 정규장 종가, NXT 미포함). 15:40 이후 확정 종가만,
데이터 날짜(price.last_date)로 1번 판정. 본문에 DGRW 등락과 USD/KRW 참고줄(Yahoo, 실패해도 판정 진행).
첫 실행(state 에 last_date 없음)은 상태만 기록. ends_kst(2027-02-28 23:59) 뒤에는 조용히 종료.
modes: auto | dry(발송 없음, state 안 씀) | install(설치 확인 1회, state 안 씀) | selftest ; --as-of "YYYY-MM-DD HH:MM" (dry 전용)
"""
from __future__ import annotations

import json
import re
import sys
import traceback
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic, route_topic  # noqa: E402
import krx_close  # noqa: E402
from kr_calendar import KST, is_trading_day, session_day, session_close_ready  # noqa: E402

TAG = "[0046Y0]"
NAME = "k0046y0"
CFG_PATH = ROOT / "configs" / f"{NAME}.json"
STATE_PATH = ROOT / "state" / f"{NAME}.json"
CLOSE_HM = (15, 40)
N_BARS = 45


def krx_rows(code: str) -> list[dict]:
    bars = krx_close.regular_bars(code, N_BARS)
    return [{"day": b.day, "close": float(b.close), "src": b.source} for b in bars]


def yahoo_meta(ticker: str) -> dict[str, Any] | None:
    url = "https://query1.finance.yahoo.com/v8/finance/chart/" + urllib.parse.quote(ticker) + "?interval=1d&range=5d"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 cwally-alerts"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode())["chart"]["result"][0].get("meta") or {}
    except Exception as exc:  # noqa: BLE001
        print(f"yahoo fail {ticker}: {exc}", file=sys.stderr)
        return None


def ctx_line(cfg: dict) -> str:
    parts: list[str] = []
    ref = cfg.get("ref_ticker", "DGRW")
    m = yahoo_meta(ref)
    if m:
        px = m.get("regularMarketPrice") or m.get("previousClose")
        prev = m.get("chartPreviousClose") or m.get("previousClose")
        if px is not None and prev:
            parts.append(f"{ref} {float(px):,.2f} USD ({(float(px) / float(prev) - 1) * 100:+.1f}% vs 전일)")
        elif px is not None:
            parts.append(f"{ref} {float(px):,.2f} USD")
    fx = yahoo_meta(cfg.get("fx_ticker", "USDKRW=X"))
    if fx:
        px = fx.get("regularMarketPrice") or fx.get("previousClose")
        if px is not None:
            parts.append(f"원/달러 {float(px):,.1f}")
    if not parts:
        return f"{ref}, 원/달러 참고값 못 읽음(가격선 판정과 무관)"
    return " / ".join(parts) + " (환노출 상품이라 원/달러가 내리면 같은 지수라도 원화 가격이 내려감)"


def confirmed(rows: list[dict], today: str, now: datetime) -> list[dict]:
    return [r for r in rows if session_close_ready(r["day"], today, now, CLOSE_HM)]


def hit(op: str, c: float, lvl: float) -> bool:
    return {"le": c <= lvl, "ge": c >= lvl, "gt": c > lvl, "lt": c < lvl}[op]


def sma(rows: list[dict], i: int, n: int) -> float | None:
    if i + 1 < n:
        return None
    return sum(r["close"] for r in rows[i - n + 1:i + 1]) / n


def seed(cfg: dict, rows: list[dict], stamp: str) -> dict:
    row = rows[-1]
    ma = sma(rows, len(rows) - 1, cfg["recovery"]["ma"])
    return {
        "last_date": row["day"],
        "last_close": row["close"],
        "armed": {ln["key"]: not hit(ln["op"], row["close"], ln["level"]) for ln in cfg["lines"]},
        "rec_armed": False,
        "cycle_from": None,
        "last_ma20": round(ma, 1) if ma else None,
        "seed_note": f"첫 실행 {stamp}: 마지막 KRX 확정 종가 {row['day']} {row['close']:,.0f}원",
    }


def evaluate(cfg: dict, ps: dict, rows: list[dict], today: str) -> list[dict]:
    """rows 는 오래된 것부터, 20일선 계산용으로 last_date 이전 행도 포함."""
    n = cfg["recovery"]["ma"]
    out = []
    for i, r in enumerate(rows):
        d, c = r["day"], r["close"]
        if d <= ps["last_date"] or d < cfg["window_start"] or d > today:
            continue
        ma = sma(rows, i, n)
        prev = rows[i - 1]["close"] if i else None
        # 20일선 회복 먼저: 같은 날 선이 새로 울리면 그날이 아니라 다음 날부터 회복을 봄
        if ps.get("rec_armed") and ma is not None and c > ma:
            ps["rec_armed"] = False
            out.append({"key": "rec", "day": d, "close": c, "prev": prev, "ma": ma, "priority": cfg["recovery"]["priority"],
                        "head": cfg["recovery"]["head"], "cycle_from": ps.get("cycle_from")})
        for ln in cfg["lines"]:
            k = ln["key"]
            if hit(ln["op"], c, ln["level"]) and ps["armed"].get(k, True):
                ps["armed"][k] = False
                ps["rec_armed"] = True
                ps["cycle_from"] = f"{d} {ln['tier_ko']}"
                out.append({**ln, "day": d, "close": c, "prev": prev, "ma": ma})
            elif hit(ln["rearm_op"], c, ln["rearm"]) and not ps["armed"].get(k, True):
                ps["armed"][k] = True
        ps["last_date"], ps["last_close"] = d, c
        ps["last_ma20"] = round(ma, 1) if ma is not None else ps.get("last_ma20")
    return out


def judge_text(cfg: dict, c: float, ma: float | None) -> str:
    inside = [ln["head"] for ln in cfg["lines"] if hit(ln["op"], c, ln["level"])]
    ma_s = f", 20일선 {ma:,.1f}원 {'위' if c > ma else '아래'}" if ma else ""
    return ("선 안쪽: " + " / ".join(inside) if inside else "11,900원 초과, 두 선 밖") + ma_s


def body(stamp: str, hdr: str, what: str, status: str, action: str, later: str, trim: str, avoid: str) -> str:
    return "\n".join([
        f"{stamp} | 0046Y0 ACE 미국배당퀄리티 | {hdr}",
        f"이게 뭐 알림: {what}",
        f"지금 결과/상태: {status}",
        f"지금 할 일: {action}",
        f"나중에: {later}",
        f"익절/줄일 때: {trim}",
        f"하지 말 것: {avoid}",
    ])


WHAT = (
    "ACE 미국배당퀄리티(0046Y0, 환노출, 월배당) KRX 정규장 종가 기준 분할 매수 검토 신호"
    "(1차 11,900원, 2차 11,200원, 그 뒤 20일선 회복). 매매 지시 아님, 자동 주문 아님."
)
AVOID = "매매 지시로 보기, 시장가 주문(거래량 적음), 한 번에 다 사기, 신용/미수 쓰기, NXT 시간외 가격으로 판단하기."


def messages(cfg: dict, ev: list[dict], stamp: str, ctx: str) -> list[tuple[str, str, int]]:
    b = cfg["baseline"]
    out = []
    for e in ev:
        c = e["close"]
        base = (
            f"{e['day']} KRX 종가 {c:,.0f}원" + (f"(전일 {e['prev']:,.0f}원)" if e.get("prev") else "")
            + (f", 20일선 {e['ma']:,.1f}원 대비 {(c / e['ma'] - 1) * 100:+.1f}%" if e.get("ma") else "")
            + f". 3개월 최저 종가 {b['low_3m']:,}원({b['low_3m_day']}) 대비 {(c / b['low_3m'] - 1) * 100:+.1f}%,"
            f" 52주 최저 종가 {b['low_52w']:,}원({b['low_52w_day']}) 대비 {(c / b['low_52w'] - 1) * 100:+.1f}%."
        )
        if e["key"] == "rec":
            title = f"{TAG} ACE 미국배당퀄리티 20일선 위 종가 회복 {c:,.0f}원"
            st = base + f" 사이클 시작: {e.get('cycle_from') or '기록 없음'} 선 이탈. 참고: {ctx}."
            act = "하락이 멈췄는지 보는 검토 신호입니다. 남은 분할 몫을 지정가로 나눠 넣을지 검토만 하세요."
            later = "이 회복 알림은 사이클마다 1번. 다음은 1차나 2차 선이 다시 울린 뒤에만 옵니다."
            trim = "장기 위성 비중이라 이 알림으로 줄이지 않음. 계획 비중을 넘겼으면 추가 매수만 멈춤."
            hdr = "20일선 회복"
        else:
            title = f"{TAG} ACE 미국배당퀄리티 {e['head']}"
            st = base + f" {e['tier_ko']} 선 {e['level']:,}원: {e['why']} 참고: {ctx}."
            act = (
                f"{e['tier_ko']} 분할 매수 검토 신호입니다(매매 지시 아님). 사기로 하면 계획 몫만, "
                "지정가로 2~3번 나눠서. 신용/미수 없이 현금으로만."
            )
            later = (
                f"{e['rearm']:,}원 이상 종가가 나온 뒤 다시 {e['level']:,}원 이하로 오면 다시 알림. "
                "이번 하락 뒤 처음 20일선 위로 닫으면 회복 알림 1번(p3)."
            )
            trim = (
                "더 내려가면 2차(11,200원)에서 따로 알림. 한 번에 다 채우지 않기."
                if e["key"] == "t1" else
                "52주 저점도 깬 자리. 지수(DGRW)가 빠진 건지 원/달러가 빠진 건지 본 뒤 남은 몫만. 비중 계획 넘기지 않기."
            )
            hdr = f"{e['tier_ko']} 분할 매수 검토"
        out.append((title, body(stamp, hdr, WHAT, st, act, later, trim, AVOID), e["priority"]))
    return out


def ops_msg(stamp: str, status: str) -> tuple[str, str]:
    return (
        f"{TAG} ACE 미국배당퀄리티 KRX 종가 조회 실패 | 판정 건너뜀",
        body(stamp, "KRX 종가 읽기 실패",
             "0046Y0 분할 매수 검토 알림의 데이터 읽기 실패 안내. 하루 1번. 매매 신호 아님.",
             status, "증권 앱에서 0046Y0 KRX 정규장 종가를 직접 보세요.",
             "다음 19:00 슬롯에서 다시 읽고, 빠진 날짜는 KRX 종가로 이어서 판정합니다.",
             "이 안내로 비중을 바꾸지 않기.", "NXT 시간외 가격이나 네이버 일봉으로 대신 판정하기."),
    )


def install_message(stamp: str) -> tuple[str, str]:
    return (
        f"{TAG} ACE 미국배당퀄리티 설치 확인 | 매매 신호 아님",
        body(stamp, "설치 확인",
             "0046Y0 ACE 미국배당퀄리티 KRX 종가 분할 매수 검토 알림 설치 확인용 테스트 발송입니다. 매매 신호가 아닙니다.",
             "평일 19:00 KST 에 KRX 정규장 확정 종가로 11,900원 이하(1차, p4), 11,200원 이하(2차, p5)를 선마다 1번 보고, "
             "선이 울린 뒤 처음 20일선 위로 닫으면 p3 1번 보냅니다.",
             "알림이 도착했는지 확인만 하세요.",
             "재무장: 12,260원 이상, 11,540원 이상 종가 뒤. 2027-02-28 23:59 에 끝납니다.",
             "해당 없음.", "이 메시지를 매매 신호로 해석하기."),
    )


def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(st: dict) -> None:
    STATE_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def check_text(t: str, m: str) -> None:
    assert not re.search("[\u00b7\u318d\u2014]", t + m), t
    assert t.startswith(TAG) and "0046Y0" in t, t


def run(mode: str, now: datetime, cfg: dict, pub=publish, rows_fn=None, ctx_fn=None, state: dict | None = None) -> int:
    dry = mode == "dry"
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    topic, server = resolve_topic("port", cfg.get("ntfy_topic")), resolve_server(cfg.get("ntfy_server"))
    print(f"{stamp} {NAME} mode={mode}")
    if mode == "install":
        t, m = install_message(stamp)
        check_text(t, m)
        print(f"{t}\n{m}")
        pub(topic, t, m, 3, ["white_check_mark"], server, config_name=NAME)
        print("install confirmation sent (state untouched)")
        return 0
    sday = session_day(now)
    today = sday.isoformat()
    if today > cfg["window_end"]:
        print(f"{today} 감시 끝({cfg['window_end']}): 조용히 종료")
        return 0
    if not is_trading_day(sday) or today < cfg["window_start"]:
        print(f"{today} KRX 휴장 또는 시작 전: 조용히 종료")
        return 0
    st = load_state() if state is None else state
    out, err = [], None
    ctx = "참고값 생략"
    try:
        rows = confirmed((rows_fn or krx_rows)(cfg["code"]), today, now)
        if session_close_ready(today, today, now, CLOSE_HM) and (not rows or rows[-1]["day"] != today):
            err = f"{today} KRX 종가가 아직 없음(마지막 {rows[-1]['day'] if rows else '없음'}). 오늘 판정 건너뜀"
        if rows:
            ma = sma(rows, len(rows) - 1, cfg["recovery"]["ma"])
            print(f"latest KRX close {rows[-1]['day']} {rows[-1]['close']:,.0f} src={rows[-1].get('src')} "
                  f"MA20={'n/a' if ma is None else f'{ma:,.1f}'} rows={len(rows)}: {judge_text(cfg, rows[-1]['close'], ma)}")
        ctx = (ctx_fn or ctx_line)(cfg)
        print(f"ctx: {ctx}")
        if not (st.get("price") or {}).get("last_date"):
            if rows and not err:
                st["price"] = seed(cfg, rows, stamp)
                print(f"first run: state only, no alert: {st['price']}")
        elif rows:
            use = [r for r in rows if r["day"] != today] if err else rows
            out = messages(cfg, evaluate(cfg, st["price"], use, today), stamp, ctx)
            print(f"price: {st['price']}")
    except Exception as exc:  # noqa: BLE001
        err = f"KRX 정규장 일별 조회 실패: {str(exc)[:160]}. 판정 건너뜀"
    ops = None
    if err:
        print("ERR:", err)
        if st.get("ops_day") != today:
            ops = ops_msg(stamp, err + ". 네이버 일봉(NXT 섞임)으로 대신 판정하지 않음. state 는 그대로.")
            st["ops_day"] = today
    for t, m, pri in out:
        check_text(t, m)
        print(f"TITLE: {t}\nPRIORITY: {pri} TOPIC: {route_topic(topic, t)}\n{m}\n")
    if ops:
        print(f"OPS TITLE: {ops[0]} -> {route_topic(topic, ops[0])}\n{ops[1]}\n")
    if not out and not ops:
        print("보낼 것 없음")
    if dry:
        print("DRY RUN: no publish, state not written")
        return 0
    for t, m, pri in out:
        pub(topic, t, m, pri, ["chart_with_downwards_trend" if "이탈" in t else "chart_with_upwards_trend"], server, config_name=NAME)
    if ops:
        try:
            pub(resolve_topic("ops"), ops[0], ops[1], 3, ["warning"], server, config_name=NAME)
        except Exception as exc:  # noqa: BLE001
            print(f"ops notice failed: {exc}")
    st["updated_at"] = stamp
    if state is None:
        save_state(st)
    return 0


def selftest() -> int:
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    nopub = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no pub"))  # noqa: E731
    # 20 days of history at 12,100 then a scenario
    hist = [{"day": f"2026-11-{i + 6:02d}", "close": 12100.0} for i in range(20)]
    ps = seed(cfg, hist, "T")
    assert ps["armed"] == {"t1": True, "t2": True} and ps["rec_armed"] is False, ps
    assert abs(ps["last_ma20"] - 12100) < 0.01
    # spurious first-run guard: committed state (11/4 12,095) + a normal 11/5 close sends nothing
    st0 = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    p0 = json.loads(json.dumps(st0["price"]))
    rows0 = [{"day": f"2026-10-{i + 7:02d}", "close": 12100.0} for i in range(19)] + [
        {"day": "2026-11-04", "close": 12095.0}, {"day": "2026-11-05", "close": 12370.0}]
    assert evaluate(cfg, p0, rows0, "2026-11-05") == [] and p0["last_date"] == "2026-11-05", p0
    # 12,370 above MA20 but recovery not armed (no line fired yet) -> silent
    mk = lambda start, cs: [{"day": f"2026-12-{start + i:02d}", "close": float(c)} for i, c in enumerate(cs)]  # noqa: E731
    seq = mk(2, [11901, 11900, 11950, 12000, 12200, 11200, 11800, 12260, 11900, 11540, 11200])
    ev = evaluate(cfg, ps, hist + seq, "2026-12-31")
    got = [(e["key"], e["day"]) for e in ev]
    # 12/02 11901 silent; 12/03 11900 t1 (rec armed); 12/04 11950 < MA; 12/05 12000 < MA;
    # 12/06 12200 > MA20 -> rec; 12/07 11200 t2 (t1 still disarmed, rec re-armed);
    # 12/08 11800 < MA; 12/09 12260 rearms t1 and > MA -> rec;
    # 12/10 11900 t1 again; 12/11 11540 rearms t2; 12/12 11200 t2 again
    expect = [("t1", "2026-12-03"), ("rec", "2026-12-06"), ("t2", "2026-12-07"), ("rec", "2026-12-09"),
              ("t1", "2026-12-10"), ("t2", "2026-12-12")]
    assert got == expect, got
    assert ps["rec_armed"] is True and ps["armed"] == {"t1": False, "t2": False}, ps
    # seed inside a line starts that line disarmed
    s2 = seed(cfg, hist[:-1] + [{"day": "2026-11-25", "close": 11850.0}], "T")
    assert s2["armed"] == {"t1": False, "t2": True}, s2
    msgs = messages(cfg, ev, "T", "DGRW 85.00 USD (+0.3% vs 전일) / 원/달러 1,380.0")
    assert msgs[0][0] == "[0046Y0] ACE 미국배당퀄리티 1차 기준선 11,900원 종가 이탈" and msgs[0][2] == 4, msgs[0]
    assert msgs[1][0].startswith("[0046Y0] ACE 미국배당퀄리티 20일선 위 종가 회복") and msgs[1][2] == 3
    assert msgs[2][0] == "[0046Y0] ACE 미국배당퀄리티 2차 기준선 11,200원 종가 이탈" and msgs[2][2] == 5
    for t, m, _ in msgs:
        check_text(t, m)
        assert "매매 지시 아님" in m and "지정가" in m and "신용/미수" in m, m
        assert route_topic("cwally-port", t) == "cwally-port", t
    for t, m in (ops_msg("T", "x"), install_message("T")):
        check_text(t, m)
    assert route_topic("cwally-port", ops_msg("T", "x")[0]) == "cwally-ops"
    # dry path with fake rows: no publish, no state write; auto with in-memory state seeds silently
    fake = lambda _c: [dict(r, day=f"2026-10-{i + 7:02d}") for i, r in enumerate(hist)] + [{"day": "2026-11-04", "close": 12095.0}]  # noqa: E731
    before = STATE_PATH.read_text(encoding="utf-8")
    assert run("dry", datetime(2026, 11, 5, 15, 0, tzinfo=KST), cfg, pub=nopub, rows_fn=fake, ctx_fn=lambda _c: "ctx") == 0
    mem: dict = {}
    assert run("auto", datetime(2026, 11, 5, 15, 0, tzinfo=KST), cfg, pub=nopub, rows_fn=fake, ctx_fn=lambda _c: "ctx", state=mem) == 0
    assert mem["price"]["last_date"] == "2026-11-04", mem
    # after 15:40 with today's row missing -> one ops notice (captured, not sent)
    sent: list = []
    mem2 = json.loads(json.dumps(mem))
    run("auto", datetime(2026, 11, 5, 19, 0, tzinfo=KST), cfg, pub=lambda *a, **k: sent.append(a[1]), rows_fn=fake,
        ctx_fn=lambda _c: "ctx", state=mem2)
    assert len(sent) == 1 and "조회 실패" in sent[0] and mem2["ops_day"] == "2026-11-05", sent
    assert STATE_PATH.read_text(encoding="utf-8") == before
    print("selftest ok")
    return 0


def main() -> int:
    args = sys.argv[1:]
    mode = (args[0] if args and not args[0].startswith("--") else "auto").lower()
    if mode == "selftest":
        return selftest()
    if mode not in {"auto", "dry", "install"}:
        print(f"usage: {NAME}_monitor.py <auto|dry|install|selftest> [--as-of 'YYYY-MM-DD HH:MM']")
        return 2
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    if "--as-of" in args:
        if mode != "dry":
            print("--as-of 는 dry 에서만")
            return 2
        now = datetime.strptime(args[args.index("--as-of") + 1], "%Y-%m-%d %H:%M").replace(tzinfo=KST)
    try:
        return run(mode, now, cfg)
    except Exception as exc:  # noqa: BLE001
        print(traceback.format_exc(), file=sys.stderr)
        if mode != "auto":
            return 1
        try:
            t, m = ops_msg(now.strftime("%Y-%m-%d %H:%M KST"), f"스크립트 오류 {type(exc).__name__}: {str(exc)[:200]}. state 는 그대로.")
            publish(resolve_topic("ops"), t, m, 3, ["warning"], resolve_server(cfg.get("ntfy_server")), config_name=NAME)
        except Exception:  # noqa: BLE001
            return 1
        return 0


if __name__ == "__main__":
    sys.exit(main())
