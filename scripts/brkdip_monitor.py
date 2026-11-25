#!/usr/bin/env python3
"""[BRKDIP] 475350 RISE 버크셔포트폴리오TOP10 KRX 확정 종가 분할매수 3단계.

선 (configs/brkdip.json lines): 종가 <= 14,000 p3 (14,420 초과 뒤 재무장),
<= 13,700 p4 (14,111 초과 뒤 재무장), <= 13,540 p5 (13,946 초과 뒤 재무장). 선마다 1번.
종가: Daum KRX 일별만(NXT 미포함). 15:40 이후 확정 종가만, 데이터 날짜로 1번 판정.
본문에 BRK.B 일간 등락과 USD/KRW 를 Yahoo 로 붙임(실패해도 가격선 판정은 진행).
첫 실행은 상태만 기록. ends_kst(2027-11-05 19:00) 뒤에는 조용히 종료.
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
from kr_calendar import KST, is_trading_day, session_day, session_close_ready  # noqa: E402
from ntfy_util import publish, resolve_server, resolve_topic, route_topic  # noqa: E402

TAG = "[BRKDIP]"
NAME = "brkdip"
CFG_PATH = ROOT / "configs" / f"{NAME}.json"
STATE_PATH = ROOT / "state" / f"{NAME}.json"
CLOSE_HM = (15, 40)
DAUM = "https://finance.daum.net/api/quote/A{code}/days?symbolCode=A{code}&page=1&perPage=10&pagination=true"


def daum_rows(code: str) -> list[dict]:
    import daum_fetch
    raw = json.loads(daum_fetch.fetch_text(
        DAUM.format(code=code),
        headers={"User-Agent": "Mozilla/5.0", "Referer": f"https://finance.daum.net/quotes/A{code}"},
    ))
    out = []
    for r in raw.get("data") or []:
        if str(r.get("symbolCode")) != "A" + code:
            raise ValueError("종목 코드 불일치")
        day, px = str(r.get("date") or "")[:10], r.get("tradePrice")
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", day) and px:
            out.append(
                {
                    "day": day,
                    "close": float(px),
                    "prev": float(r["prevClosingPrice"]) if r.get("prevClosingPrice") else None,
                }
            )
    if not out:
        raise ValueError("KRX 일별 시세 없음")
    return sorted(out, key=lambda x: x["day"])


def yahoo_meta(ticker: str) -> dict[str, Any] | None:
    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        + urllib.parse.quote(ticker)
        + "?interval=1d&range=5d"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 cwally-alerts"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read().decode())
        return payload["chart"]["result"][0].get("meta") or {}
    except Exception as exc:  # noqa: BLE001
        print(f"yahoo fail {ticker}: {exc}", file=sys.stderr)
        return None


def ctx_line(cfg: dict) -> str:
    parts: list[str] = []
    brk = yahoo_meta(cfg.get("brk_ticker", "BRK-B"))
    if brk:
        px = brk.get("regularMarketPrice") or brk.get("previousClose")
        prev = brk.get("chartPreviousClose") or brk.get("previousClose")
        if px is not None and prev:
            chg = (float(px) / float(prev) - 1.0) * 100.0
            parts.append(f"BRK.B {float(px):,.2f} USD ({chg:+.1f}% vs 전일 {float(prev):,.2f})")
        elif px is not None:
            parts.append(f"BRK.B {float(px):,.2f} USD")
        lo = brk.get("fiftyTwoWeekLow")
        hi = brk.get("fiftyTwoWeekHigh")
        if lo and hi:
            parts.append(f"BRK.B 52주 {float(lo):,.2f}-{float(hi):,.2f}")
    fx = yahoo_meta(cfg.get("fx_ticker", "USDKRW=X"))
    if fx:
        px = fx.get("regularMarketPrice") or fx.get("previousClose")
        if px is not None:
            parts.append(f"USD/KRW {float(px):,.1f}")
    return " / ".join(parts) if parts else "BRK.B, USD/KRW 조회 실패(가격선 판정과 무관)"


def confirmed(rows: list[dict], today: str, now: datetime) -> list[dict]:
    return [r for r in rows if session_close_ready(r["day"], today, now, CLOSE_HM)]


def hit(op: str, c: float, lvl: float) -> bool:
    return {"le": c <= lvl, "ge": c >= lvl, "gt": c > lvl, "lt": c < lvl}[op]


def seed(cfg: dict, row: dict, stamp: str) -> dict:
    return {
        "last_date": row["day"],
        "last_close": row["close"],
        "armed": {ln["key"]: not hit(ln["op"], row["close"], ln["level"]) for ln in cfg["lines"]},
        "seed_note": f"첫 실행 {stamp}: 마지막 KRX 확정 종가 {row['day']} {row['close']:,.0f}원",
        "low_seen": min(float(cfg["baseline"].get("low_52w") or row["close"]), row["close"]),
    }


def evaluate(cfg: dict, ps: dict, rows: list[dict], today: str) -> list[dict]:
    out = []
    low_seen = float(ps.get("low_seen") or cfg["baseline"].get("low_52w") or 1e18)
    for r in rows:
        d, c = r["day"], r["close"]
        if d <= ps["last_date"] or d < cfg["window_start"] or d > today:
            continue
        # 새 52주 저점: 기준 저점보다 더 내려가면 t3 선과 같이 취급(이미 t3 무장 해제면 중복 발송 없음)
        new_low = c < low_seen
        if new_low:
            low_seen = c
        for ln in cfg["lines"]:
            k = ln["key"]
            fire = hit(ln["op"], c, ln["level"]) or (k == "t3" and new_low)
            if fire and ps["armed"].get(k, True):
                ps["armed"][k] = False
                out.append({**ln, "day": d, "close": c, "prev": r.get("prev"), "new_52w_low": bool(k == "t3" and new_low)})
            elif hit(ln["rearm_op"], c, ln["rearm"]) and not ps["armed"].get(k, True):
                ps["armed"][k] = True
        ps["last_date"], ps["last_close"] = d, c
    ps["low_seen"] = low_seen
    return out


def judge_text(cfg: dict, c: float) -> str:
    inside = [ln["head"] for ln in cfg["lines"] if hit(ln["op"], c, ln["level"])]
    return "선 안쪽: " + " / ".join(inside) if inside else "14,000원 초과, 세 선 밖이라 알림 없음"


def body(stamp: str, hdr: str, what: str, status: str, action: str, later: str, trim: str, avoid: str) -> str:
    return "\n".join(
        [
            f"{stamp} | 475350 / BRK.B | {hdr}",
            f"이게 뭐 알림: {what}",
            f"지금 결과/상태: {status}",
            f"지금 할 일: {action}",
            f"나중에: {later}",
            f"익절/줄일 때: {trim}",
            f"하지 말 것: {avoid}",
        ]
    )


WHAT = (
    "RISE 버크셔포트폴리오TOP10(475350) KRX 확정 종가 분할매수 알림"
    "(14,000 / 13,700 / 13,540원). 자동 매수 신호 아님. 투자 자문 아님."
)


def messages(cfg: dict, ev: list[dict], stamp: str, ctx: str) -> list[tuple[str, str, int]]:
    b = cfg["baseline"]
    low52 = float(b.get("low_52w") or 13540)
    out = []
    for e in ev:
        dist_low = (e["close"] / low52 - 1) * 100
        dist_base = (e["close"] / b["close"] - 1) * 100
        rearm = f"{e['rearm']:,}원 넘는 종가가 나온 뒤 같은 구간으로 다시 들어오면 다시 알림."
        st = (
            f"{e['day']} KRX 종가 {e['close']:,.0f}원"
            + (f"(전일 {e['prev']:,.0f}원)" if e.get("prev") else "")
            + f", {e['tier_ko']} 선 {e['level']:,}원."
            f" 52주 저점 {low52:,.0f}원 대비 {dist_low:+.1f}%."
            f" 기준 {b['day']} 종가 {b['close']:,}원 대비 {dist_base:+.1f}%."
        )
        if e.get("new_52w_low"):
            st += " 새 52주 저점(또는 기준 저점 하회)."
        st += f" 참고: {ctx}."
        act = (
            f"{e['tier_ko']} 분할매수 검토. 지정가로 나눠서. "
            "기존 [BRK10] 총자산 12% 한도와 환율 가이드도 함께 보세요."
        )
        if e["key"] == "t1":
            trim = "더 내려가면 2차(13,700), 3차(13,540)에서 따로 알림. 계획 비중 넘기지 않기."
        elif e["key"] == "t2":
            trim = "3차(13,540, 52주 저점권)에서 따로 알림. 한 번에 다 채우지 않기."
        else:
            trim = "52주 저점권. 펀더멘털/구성 변화 확인 뒤 남은 비중만. 전량 베팅 금지."
        avoid = "자동 매수 신호로 보기, 시장가 주문, 한 번에 다 사기, 총자산 12% 초과, 이 알림을 투자 자문으로 해석하기."
        title = f"{TAG} 475350 / BRK.B {e['head']}"
        out.append(
            (
                title,
                body(stamp, f"{e['tier_ko']} 분할매수", WHAT, st, act, rearm, trim, avoid),
                e["priority"],
            )
        )
    return out


def ops_msg(stamp: str, status: str) -> tuple[str, str]:
    return (
        f"{TAG} 475350 KRX 종가 조회 실패 | 판정 건너뜀",
        body(
            stamp,
            "KRX 종가 읽기 실패",
            "475350 분할매수 알림의 데이터 읽기 실패 안내. 하루 1번. 매매 신호 아님.",
            status,
            "증권 앱에서 475350 KRX 종가를 직접 보세요.",
            "다음 19:00 슬롯에서 다시 읽고, 빠진 날짜는 KRX 종가로 이어서 판정합니다.",
            "이 안내로 비중을 바꾸지 않기.",
            "NXT 시간외 가격이나 네이버 일별 값으로 대신 판정하기.",
        ),
    )


def install_message(stamp: str) -> tuple[str, str]:
    return (
        f"{TAG} 475350 / BRK.B 설치 확인 | 매매 신호 아님",
        body(
            stamp,
            "설치 확인",
            "475350(RISE 버크셔포트폴리오TOP10) KRX 종가 분할매수 알림 설치 확인용 테스트 발송입니다. 매매 신호가 아닙니다.",
            "평일 19:00 KST 에 Daum KRX 확정 종가로 14,000원 이하(1차), 13,700원 이하(2차), "
            "13,540원 이하(3차/52주 저점권)를 선마다 1번 봅니다. 본문에 BRK.B 등락과 USD/KRW 참고줄을 붙입니다.",
            "알림이 도착했는지 확인만 하세요.",
            "재무장: 14,420원 초과, 14,111원 초과, 13,946원 초과 종가 뒤. 2027-11-05 19:00 에 끝납니다.",
            "해당 없음.",
            "이 메시지를 매매 신호로 해석하기.",
        ),
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


def run(mode: str, now: datetime, cfg: dict, pub=publish, rows_fn=None, ctx_fn=None) -> int:
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
        print(f"{today} 감시 끝({cfg['window_end']} 19:00 슬롯이 마지막): 조용히 종료")
        return 0
    if not is_trading_day(sday) or today < cfg["window_start"]:
        print(f"{today} KRX 휴장 또는 시작 전: 조용히 종료")
        return 0
    st = load_state()
    out, err = [], None
    ctx = "BRK.B, USD/KRW 생략(dry/selftest 기본)"
    try:
        rows = confirmed((rows_fn or daum_rows)(cfg["code"]), today, now)
        if session_close_ready(today, today, now, CLOSE_HM) and (not rows or rows[-1]["day"] != today):
            err = (
                f"{today} KRX 종가가 Daum KRX 일별에 아직 없음"
                f"(마지막 {rows[-1]['day'] if rows else '없음'}). 오늘 판정 건너뜀"
            )
        if rows:
            print(
                f"latest KRX close {rows[-1]['day']} {rows[-1]['close']:,.0f} "
                f"(prev {rows[-1].get('prev')}): {judge_text(cfg, rows[-1]['close'])}"
            )
        if not dry:
            ctx = (ctx_fn or ctx_line)(cfg)
            print(f"ctx: {ctx}")
        if not (st.get("price") or {}).get("last_date"):
            if rows and not err:
                st["price"] = seed(cfg, rows[-1], stamp)
                print(f"first run: state only, no alert: {st['price']}")
        elif rows:
            use = [r for r in rows if r["day"] != today] if err else rows
            out = messages(cfg, evaluate(cfg, st["price"], use, today), stamp, ctx)
            print(f"price: {st['price']}")
    except Exception as exc:  # noqa: BLE001
        err = f"Daum KRX 일별 조회 실패: {str(exc)[:160]}. 판정 건너뜀"
    ops = None
    if err:
        print("ERR:", err)
        if st.get("ops_day") != today:
            ops = ops_msg(stamp, err + ". 네이버(NXT 섞임)로 대신 판정하지 않음. state 는 그대로.")
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
        pub(topic, t, m, pri, ["chart_with_upwards_trend"], server, config_name=NAME)
    if ops:
        try:
            pub(resolve_topic("ops"), ops[0], ops[1], 3, ["warning"], server, config_name=NAME)
        except Exception as exc:  # noqa: BLE001
            print(f"ops notice failed: {exc}")
    st["updated_at"] = stamp
    save_state(st)
    return 0


def selftest() -> int:
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    ps = seed(cfg, {"day": "2026-11-04", "close": 14305.0}, "T")
    assert all(ps["armed"].values()), ps
    assert "세 선 밖" in judge_text(cfg, 14305)
    mk = lambda cs: [  # noqa: E731
        {"day": f"2026-11-{i + 5:02d}", "close": float(c), "prev": None} for i, c in enumerate(cs)
    ]
    # 14001 silent; 14000 t1; 13700 t2; 13540 t3; rearm t1 at 14421; 14000 t1 again;
    # new 52w low 13400 while t3 disarmed: no t3; rearm t3 at 13947; 13400 t3 again
    # 14001 silent; 14000 t1; 13700 t2; 13540 t3; 14421 rearms all;
    # 14000 t1 again; 13400 t2+t3(new low); 13947 rearms t3; 13400 t3 by level
    days = [14001, 14000, 13700, 13540, 14421, 14000, 13400, 13947, 13400]
    ev = evaluate(cfg, ps, mk(days), "2026-12-31")
    got = [(e["key"], e["day"], bool(e.get("new_52w_low"))) for e in ev]
    expect = [
        ("t1", "2026-11-06", False),
        ("t2", "2026-11-07", False),
        ("t3", "2026-11-08", False),
        ("t1", "2026-11-10", False),
        ("t2", "2026-11-11", False),
        ("t3", "2026-11-11", True),
        ("t3", "2026-11-13", False),
    ]
    assert got == expect, got
    s2 = seed(cfg, {"day": "2026-11-04", "close": 13800.0}, "T")
    assert s2["armed"] == {"t1": False, "t2": True, "t3": True}, s2
    msgs = messages(cfg, ev, "T", "BRK.B 579.00 USD (+0.5% vs 전일 576.00) / USD/KRW 1,343.0")
    assert "475350 / BRK.B" in msgs[0][0] and "1차" in msgs[0][0]
    assert "52주 저점" in msgs[0][1] and "BRK.B" in msgs[0][1]
    for t, m, _ in msgs:
        check_text(t, m)
        assert "\u00b7" not in t + m and "\u2014" not in t + m
    for t, m in (ops_msg("T", "x"), install_message("T")):
        check_text(t, m)
        assert "475350" in t
    # dry path uses fake rows, no network
    fake = [{"day": "2026-11-04", "close": 14305.0, "prev": 14400.0}]
    rc = run(
        "dry",
        datetime(2026, 11, 5, 19, 0, tzinfo=KST),
        cfg,
        pub=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no pub")),
        rows_fn=lambda _c: fake,
        ctx_fn=lambda _c: "ctx",
    )
    assert rc == 0
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
            t, m = ops_msg(
                now.strftime("%Y-%m-%d %H:%M KST"),
                f"스크립트 오류 {type(exc).__name__}: {str(exc)[:200]}. state 는 그대로.",
            )
            publish(
                resolve_topic("ops"),
                t,
                m,
                3,
                ["warning"],
                resolve_server(cfg.get("ntfy_server")),
                config_name=NAME,
            )
        except Exception:  # noqa: BLE001
            return 1
        return 0


if __name__ == "__main__":
    sys.exit(main())
