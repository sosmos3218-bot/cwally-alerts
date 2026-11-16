#!/usr/bin/env python3
"""[402340] SK스퀘어 하이닉스 지분 기준 단순 할인율 D 분할 매수 검토 알림.

D = 1 - (402340 KRX 종가 x SK스퀘어 유통주식) / (000660 KRX 종가 x 하이닉스 보유 주식).
선 (configs/k402340.json lines): D >= 47% p4 1차 (D <= 44% 뒤 재무장), D >= 50% p5 2차 (D <= 47% 뒤 재무장),
D <= 30% p3 할인 축소 비중 점검 (D >= 33% 뒤 재무장). 선마다 1번.
종가: scripts/krx_close.py regular_bars (Daum KRX 정규장 종가, NXT 미포함). 15:40 이후 확정 종가만,
두 종목 같은 날짜만, 데이터 날짜(price.last_date)로 1번 판정. 첫 실행(state 에 last_date 없음)은 상태만 기록.
ends_kst(2027-02-28 23:59) 뒤에는 조용히 종료.
modes: auto | dry(발송 없음, state 안 씀) | install(설치 확인 1회, state 안 씀) | selftest ; --as-of "YYYY-MM-DD HH:MM" (dry 전용)
"""
from __future__ import annotations

import json
import re
import sys
import traceback
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic, route_topic  # noqa: E402
import krx_close  # noqa: E402
from kr_calendar import KST, is_trading_day, session_day, session_close_ready  # noqa: E402

TAG = "[402340]"
NAME = "k402340"
CFG_PATH = ROOT / "configs" / f"{NAME}.json"
STATE_PATH = ROOT / "state" / f"{NAME}.json"
CLOSE_HM = (15, 40)
N_BARS = 10


def krx_rows(code: str) -> list[dict]:
    bars = krx_close.regular_bars(code, N_BARS)
    return [{"day": b.day, "close": float(b.close), "src": b.source} for b in bars]


def confirmed(rows: list[dict], today: str, now: datetime) -> list[dict]:
    return [r for r in rows if session_close_ready(r["day"], today, now, CLOSE_HM)]


def disc(cfg: dict, sq: float, hx: float) -> float:
    s = cfg["shares"]
    return 1 - (sq * s["sq_shares_float"]) / (hx * s["hynix_shares_held"])


def disc_company(cfg: dict, sq: float, hx: float) -> float:
    s = cfg["shares"]
    nav = (hx * s["hynix_shares_held"] + cfg["company_nav_ref"]["other_nav_krw"]) / s["sq_shares_issued"]
    return 1 - sq / nav


def join_rows(sq_rows: list[dict], hx_rows: list[dict]) -> list[dict]:
    """같은 날짜에 두 종가가 다 있는 날만, 오래된 것부터."""
    hx = {r["day"]: r for r in hx_rows}
    out = []
    for r in sq_rows:
        h = hx.get(r["day"])
        if h:
            out.append({"day": r["day"], "sq": r["close"], "hx": h["close"], "src": f"{r.get('src')}/{h.get('src')}"})
    return sorted(out, key=lambda x: x["day"])


def hit(op: str, v: float, lvl: float) -> bool:
    return {"le": v <= lvl, "ge": v >= lvl}[op]


def seed(cfg: dict, rows: list[dict], stamp: str) -> dict:
    row = rows[-1]
    d = disc(cfg, row["sq"], row["hx"])
    return {
        "last_date": row["day"],
        "last_sq": row["sq"],
        "last_hx": row["hx"],
        "last_d": round(d, 5),
        "armed": {ln["key"]: not hit(ln["op"], d, ln["level"]) for ln in cfg["lines"]},
        "seed_note": f"첫 실행 {stamp}: 마지막 KRX 확정 종가 {row['day']} 402340 {row['sq']:,.0f}원 / 000660 {row['hx']:,.0f}원, D {d * 100:.2f}%",
    }


def evaluate(cfg: dict, ps: dict, rows: list[dict], today: str) -> list[dict]:
    out = []
    prev = None
    for r in rows:
        if r["day"] <= ps["last_date"]:
            prev = r
            continue
        if r["day"] < cfg["window_start"] or r["day"] > today:
            prev = r
            continue
        d = disc(cfg, r["sq"], r["hx"])
        for ln in cfg["lines"]:
            k = ln["key"]
            if hit(ln["op"], d, ln["level"]) and ps["armed"].get(k, True):
                ps["armed"][k] = False
                out.append({**ln, "day": r["day"], "sq": r["sq"], "hx": r["hx"], "d": d,
                            "prev": prev, "prev_d": ps.get("last_d")})
            elif hit(ln["rearm_op"], d, ln["rearm"]) and not ps["armed"].get(k, True):
                ps["armed"][k] = True
        ps["last_date"], ps["last_sq"], ps["last_hx"], ps["last_d"] = r["day"], r["sq"], r["hx"], round(d, 5)
        prev = r
    return out


def pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def chg(a: float, b: float | None) -> str:
    return "" if not b else f"({(a / b - 1) * 100:+.1f}% 전일)"


def body(stamp: str, hdr: str, what: str, status: str, action: str, later: str, trim: str, avoid: str) -> str:
    return "\n".join([
        f"{stamp} | 402340 SK스퀘어 / 000660 SK하이닉스 | {hdr}",
        f"이게 뭐 알림: {what}",
        f"지금 결과/상태: {status}",
        f"지금 할 일: {action}",
        f"나중에: {later}",
        f"익절/줄일 때: {trim}",
        f"하지 말 것: {avoid}",
    ])


WHAT = (
    "SK스퀘어(402340) 주가가 들고 있는 SK하이닉스(000660) 지분 가치보다 얼마나 싼지(단순 할인율 D) 보는 "
    "KRX 정규장 종가 기준 분할 매수 검토 신호(1차 47%, 2차 50%, 축소 30%). 매매 지시 아님, 자동 주문 아님."
)
AVOID = (
    "매매 지시로 보기, 시장가로 한 번에 사기, 신용/미수 쓰기, NXT 시간외 가격으로 판단하기, "
    "할인율만 보고 하이닉스 하락 위험을 잊기."
)


def messages(cfg: dict, ev: list[dict], stamp: str) -> list[tuple[str, str, int]]:
    s = cfg["shares"]
    b = cfg["baseline"]
    out = []
    for e in ev:
        sq, hx, d = e["sq"], e["hx"], e["d"]
        p = e.get("prev") or {}
        held = hx * s["hynix_shares_held"]
        mcap = sq * s["sq_shares_float"]
        dco = disc_company(cfg, sq, hx)
        st = (
            f"{e['day']} KRX 종가 402340 {sq:,.0f}원{chg(sq, p.get('sq'))}, 000660 {hx:,.0f}원{chg(hx, p.get('hx'))}. "
            f"하이닉스 지분 {s['hynix_shares_held']:,}주 가치 {held / 1e12:,.1f}조원, 스퀘어 시총(자사주 뺌) {mcap / 1e12:,.1f}조원, "
            f"D {pct(d)}" + (f"(전일 {pct(e['prev_d'])})" if e.get("prev_d") is not None else "") + ". "
            f"회사 방식(비상장 자회사와 순현금 4.81조원 포함) 할인율 약 {pct(dco)}. "
            f"{e['tier_ko']} 선 {pct(e['level'])}: {e['why']} 참고 1년: {b['stats_short']}."
        )
        if e["key"] == "narrow":
            title = f"{TAG} SK스퀘어 000660 {e['head']}"
            act = (
                "할인이 회사 목표 수준까지 좁혀졌습니다. 매수 신호가 아니라 비중 점검용입니다. "
                "계획 비중을 넘겼으면 일부를 지정가로 나눠 줄일지 검토만 하세요."
            )
            later = f"D {pct(e['rearm'])} 이상으로 닫힌 뒤 다시 {pct(e['level'])} 이하가 되면 다시 알림."
            trim = "줄인다면 한 번에 다 팔지 말고 2~3번 나눠서. 하이닉스 전망이 그대로면 일부만."
            hdr = "할인 축소 비중 점검"
        else:
            title = f"{TAG} SK스퀘어 000660 {e['head']}"
            act = (
                f"{e['tier_ko']} 분할 매수 검토 신호입니다(매매 지시 아님). 사기로 하면 계획 몫만, "
                "지정가로 2~3번 나눠서. 신용/미수 없이 현금으로만. "
                "하이닉스 급등으로 D가 커진 건지, 스퀘어만 빠져서 커진 건지 위 두 종가 등락을 먼저 보세요."
            )
            later = (
                f"D {pct(e['rearm'])} 이하로 닫힌 뒤 다시 {pct(e['level'])} 이상이면 다시 알림. "
                "분기 실적과 2026 재무제표 확정 뒤 주주환원 규모 공시에서 할인율이 바뀔 수 있음."
            )
            trim = (
                "더 넓어지면 2차(D 50%)에서 따로 알림. 한 번에 다 채우지 않기."
                if e["key"] == "t1" else
                "1년 범위 위쪽 끝. 하이닉스 자체가 빠지는 중이면 스퀘어도 같이 빠짐. 남은 몫만, 비중 계획 넘기지 않기."
            )
            hdr = f"{e['tier_ko']} 분할 매수 검토"
        st += " 할인율이 같아도 하이닉스 가격이 내리면 스퀘어도 내림. 하이닉스 자체 판단은 [SAMNIX]/[000660] 알림에서."
        out.append((title, body(stamp, hdr, WHAT, st, act, later, trim, AVOID), e["priority"]))
    return out


def ops_msg(stamp: str, status: str) -> tuple[str, str]:
    return (
        f"{TAG} SK스퀘어 000660 KRX 종가 조회 실패 | 판정 건너뜀",
        body(stamp, "KRX 종가 읽기 실패",
             "402340 할인율 분할 매수 검토 알림의 데이터 읽기 실패 안내. 하루 1번. 매매 신호 아님.",
             status, "증권 앱에서 402340 과 000660 의 KRX 정규장 종가를 직접 보세요.",
             "다음 19:00 슬롯에서 다시 읽고, 빠진 날짜는 KRX 종가로 이어서 판정합니다.",
             "이 안내로 비중을 바꾸지 않기.", "NXT 시간외 가격이나 네이버 일봉으로 대신 판정하기."),
    )


def install_message(stamp: str) -> tuple[str, str]:
    return (
        f"{TAG} SK스퀘어 000660 할인율 알림 설치 확인 | 매매 신호 아님",
        body(stamp, "설치 확인",
             "402340 SK스퀘어 하이닉스 지분 할인율 분할 매수 검토 알림 설치 확인용 테스트 발송입니다. 매매 신호가 아닙니다.",
             "평일 19:00 KST 에 KRX 정규장 확정 종가로 D 47% 이상(1차, p4), 50% 이상(2차, p5), 30% 이하(축소, p3)를 선마다 1번 봅니다.",
             "알림이 도착했는지 확인만 하세요.",
             "재무장: 44% 이하, 47% 이하, 33% 이상 종가 뒤. 2027-02-28 23:59 에 끝납니다.",
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
    assert t.startswith(TAG) and "402340" in t and "000660" in t, t


def run(mode: str, now: datetime, cfg: dict, pub=publish, rows_fn=None, state: dict | None = None) -> int:
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
    if not is_trading_day(sday):
        print(f"{today} KRX 휴장: 조용히 종료")
        return 0
    st = load_state() if state is None else state
    out, err = [], None
    fetch = rows_fn or krx_rows
    try:
        sq_rows = confirmed(fetch(cfg["code"]), today, now)
        hx_rows = confirmed(fetch(cfg["hynix_code"]), today, now)
        rows = join_rows(sq_rows, hx_rows)
        late = session_close_ready(today, today, now, CLOSE_HM)
        if late and (not rows or rows[-1]["day"] != today):
            miss = [c for c, rr in ((cfg["code"], sq_rows), (cfg["hynix_code"], hx_rows)) if not rr or rr[-1]["day"] != today]
            err = f"{today} KRX 종가가 아직 없음({', '.join(miss) or '날짜 불일치'}). 오늘 판정 건너뜀"
        if rows:
            r = rows[-1]
            print(f"latest KRX close {r['day']} 402340 {r['sq']:,.0f} 000660 {r['hx']:,.0f} src={r['src']} "
                  f"D={disc(cfg, r['sq'], r['hx']) * 100:.2f}% D_company~{disc_company(cfg, r['sq'], r['hx']) * 100:.2f}% rows={len(rows)}")
        if not (st.get("price") or {}).get("last_date"):
            if rows and not err:
                st["price"] = seed(cfg, rows, stamp)
                print(f"first run: state only, no alert: {st['price']}")
        elif rows:
            use = [x for x in rows if x["day"] != today] if err else rows
            out = messages(cfg, evaluate(cfg, st["price"], use, today), stamp)
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
        pub(topic, t, m, pri, ["chart_with_downwards_trend" if "축소" not in t else "scales"], server, config_name=NAME)
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
    s = cfg["shares"]
    ratio = s["hynix_shares_held"] / s["sq_shares_float"]  # sq price at D=0 per 1 won of hynix

    def sq_for(d: float, hx: float = 1_000_000.0) -> float:
        return round(hx * ratio * (1 - d))

    # formula check against the verified 11/5 closes (D 42.27%) and the company page 11/4 (43.2%)
    assert abs(disc(cfg, 809000, 1264000) - 0.4227) < 0.0005, disc(cfg, 809000, 1264000)
    assert abs(disc_company(cfg, 899000, 1396000) - 0.432) < 0.001, disc_company(cfg, 899000, 1396000)
    # install seed: 11/5 D 42.27%, all armed; a normal 11/6 sends nothing. The live state file moves on every
    # KRX day, so the selftest uses the seed values from configs (baseline) instead of reading state/k402340.json.
    st0 = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    assert "last_date" in st0.get("price", {}), st0
    b = cfg["baseline"]
    p0 = {"last_date": b["day"], "last_sq": float(b["sq_close"]), "last_hx": float(b["hx_close"]), "last_d": b["d"], "armed": {"t1": True, "t2": True, "narrow": True}}
    assert p0["last_date"] == "2026-11-05" and all(p0["armed"].values()), p0
    rows0 = [{"day": "2026-11-05", "sq": 809000.0, "hx": 1264000.0}, {"day": "2026-11-06", "sq": 820000.0, "hx": 1270000.0}]
    assert evaluate(cfg, p0, rows0, "2026-11-06") == [] and p0["last_date"] == "2026-11-06", p0
    # scenario
    mk = lambda ds: [{"day": f"2026-12-{i + 1:02d}", "sq": float(sq_for(d)), "hx": 1_000_000.0} for i, d in enumerate(ds)]  # noqa: E731
    ps = {"last_date": "2026-11-30", "last_d": 0.42, "armed": {"t1": True, "t2": True, "narrow": True}}
    seq = mk([0.469, 0.471, 0.48, 0.505, 0.49, 0.445, 0.471, 0.469, 0.501, 0.35, 0.299, 0.31, 0.331, 0.30])
    ev = evaluate(cfg, ps, seq, "2026-12-31")
    got = [(e["key"], e["day"]) for e in ev]
    # 12/01 .469 silent; 12/02 .471 t1; 12/03 .48 nothing; 12/04 .505 t2; 12/05 .49 nothing; 12/06 .445 nothing
    # (t1 rearm needs <=.44); 12/07 .471 nothing; 12/08 .469 rearms t2 (<=.47); 12/09 .501 t2 again;
    # 12/10 .35 rearms t1 and t2; 12/11 .299 narrow; 12/12 .31 nothing; 12/13 .331 rearm narrow; 12/14 .30 narrow
    expect = [("t1", "2026-12-02"), ("t2", "2026-12-04"), ("t2", "2026-12-09"), ("narrow", "2026-12-11"), ("narrow", "2026-12-14")]
    assert got == expect, got
    assert ps["armed"] == {"t1": True, "t2": True, "narrow": False}, ps
    # seed inside a line starts that line disarmed
    s2 = seed(cfg, [{"day": "2026-11-25", "sq": float(sq_for(0.48)), "hx": 1_000_000.0}], "T")
    assert s2["armed"] == {"t1": False, "t2": True, "narrow": True}, s2
    # join only same-day pairs
    j = join_rows([{"day": "a", "close": 1.0}, {"day": "b", "close": 2.0}], [{"day": "b", "close": 3.0}])
    assert [x["day"] for x in j] == ["b"], j
    msgs = messages(cfg, ev, "T")
    assert msgs[0][0] == "[402340] SK스퀘어 000660 할인율 47% 돌파 1차 분할 매수 검토" and msgs[0][2] == 4, msgs[0]
    assert msgs[1][0] == "[402340] SK스퀘어 000660 할인율 50% 돌파 2차 분할 매수 검토" and msgs[1][2] == 5, msgs[1]
    assert msgs[3][0] == "[402340] SK스퀘어 000660 할인율 30% 이하 축소 비중 점검" and msgs[3][2] == 3, msgs[3]
    for t, m, _ in msgs:
        check_text(t, m)
        assert "매매 지시 아님" in m and "지정가" in m and "신용/미수" in m and "SAMNIX" in m, m
        assert route_topic("cwally-port", t) == "cwally-port", t
    for t, m in (ops_msg("T", "x"), install_message("T")):
        check_text(t, m)
    assert route_topic("cwally-port", ops_msg("T", "x")[0]) == "cwally-ops"
    # dry path with fake rows: no publish, no state write; auto with in-memory state seeds silently
    def fake(code: str) -> list[dict]:
        return [{"day": "2026-11-05", "close": 809000.0 if code == "402340" else 1264000.0, "src": "t"}]
    before = STATE_PATH.read_text(encoding="utf-8")
    assert run("dry", datetime(2026, 11, 6, 15, 0, tzinfo=KST), cfg, pub=nopub, rows_fn=fake) == 0
    mem: dict = {}
    assert run("auto", datetime(2026, 11, 6, 15, 0, tzinfo=KST), cfg, pub=nopub, rows_fn=fake, state=mem) == 0
    assert mem["price"]["last_date"] == "2026-11-05" and all(mem["price"]["armed"].values()), mem
    # after 15:40 with today's row missing -> one ops notice (captured, not sent to port)
    sent: list = []
    mem2 = json.loads(json.dumps(mem))
    run("auto", datetime(2026, 11, 6, 19, 0, tzinfo=KST), cfg, pub=lambda *a, **k: sent.append((a[0], a[1])), rows_fn=fake, state=mem2)
    assert len(sent) == 1 and "조회 실패" in sent[0][1] and mem2["ops_day"] == "2026-11-06", sent
    # second run same day: no repeat
    run("auto", datetime(2026, 11, 6, 19, 30, tzinfo=KST), cfg, pub=lambda *a, **k: sent.append((a[0], a[1])), rows_fn=fake, state=mem2)
    assert len(sent) == 1, sent
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
