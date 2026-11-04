#!/usr/bin/env python3
"""[LGESF] LG에너지솔루션(373220) 2026년 4분기 잠정실적 일회 판정 (2026-11-04 t49u).

Window 2027-01-05 ~ 01-15 KST (configs/lgesq4.json oneshot_kst). When the DART
연결재무제표기준영업(잠정)실적(공정공시) for '26.4Q shows up in the Naver notice feed, parse it with
scripts/earnings_parse.py, compare to 3분기 (매출 96,434억, 영업이익 7,560억, AMPC 4,169억, AMPC 제외 3,391억)
and to the company's 4분기 guidance (매출 약 +10% QoQ, 수익성 일부 둔화), send one p4 message. Once only.
No filing by the last run (01-15 16:47) -> one 미게시 notice (routed to cwally-ops by the title).
Parse error -> one cwally-ops notice per day (earnings_parse.safe_parse). No consensus numbers.

Modes: auto | force | dry [--now "YYYY-MM-DD HH:MM"] (print only, no ntfy, no state) | selftest.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from naver_api import notices  # noqa: E402
from ntfy_util import publish, resolve_server, resolve_topic  # noqa: E402
from earnings_parse import parse_results, safe_parse  # noqa: E402  shared parser

KST = timezone(timedelta(hours=9))
CFG_PATH = ROOT / "configs" / "lgesq4.json"
STATE_PATH = ROOT / "state" / "lgesq4.json"
TITLE_LABEL = {"매수 검토": "검토 가능"}  # never 매수 in a title


def read_state() -> dict:
    if not STATE_PATH.exists():
        return {"notified": False, "empty_notified": False, "last_key": None}
    return json.loads(STATE_PATH.read_text(encoding="utf-8"))


def save_state(state: dict, now: datetime) -> None:
    state["updated_at"] = now.isoformat(timespec="seconds")
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def send(cfg: dict, kind: str, title: str, lines: list[str], prio: int) -> None:
    publish(resolve_topic(kind, cfg.get("ntfy_topic") if kind == "port" else None), title, "\n".join(lines), prio,
            ["warning"] if kind == "ops" else ["chart_with_upwards_trend"], resolve_server(cfg.get("ntfy_server")))


def is_q4_prelim(item: dict, cfg: dict) -> bool:
    title, body = item.get("title", ""), item.get("text", "")
    if not re.search(r"영업\s*\(?잠정\)?\s*실적", title):
        return False
    if item.get("datetime", "") and item["datetime"] < cfg["period"]["min_stamp"]:
        return False
    blob = title + " " + body
    return any(m in blob for m in cfg["period"]["marks"])


def parse(item: dict) -> dict:
    return parse_results(item.get("title", "") + "\n" + item.get("text", ""))


def pct(a: int | None, b: int | None) -> float | None:
    if a is None or not b:
        return None
    return (a / b - 1) * 100


def verdict(p: dict, cfg: dict) -> tuple[str, str, str]:
    th = cfg["thresholds"]
    op, ex, sales = p.get("op"), p.get("ex"), p.get("sales")
    sq = pct(sales, cfg["q3"]["sales_eok"])
    if op is None or ex is None:
        return ("관망", "영업이익이나 AMPC 제외 영업이익을 공시에서 분리하지 못했습니다(판정 유보)",
                "사지 말고 관망하세요. 공시 원문과 1월 말 확정 실적을 확인하세요.")
    if ex < 0 or op < 0:
        return ("보류", "AMPC 제외 영업이익" + (" 적자" if ex < 0 else "은 흑자지만 영업이익 적자"),
                "신규 매수 보류. AMPC 제외 이익이 다시 흑자로 확인되기 전에는 사지 마세요.")
    if ex >= th["ex_review_eok"] and sq is not None and sq >= th["sales_qoq_review_pct"]:
        return ("검토 가능", f"AMPC 제외 영업이익 {ex:,}억원(2,000억원 이상)이고 매출이 전분기 대비 {sq:+.1f}%(+5% 이상)",
                "신규 매수 검토 가능. 계획 금액의 일부만 나눠서 들어가고, 1월 말 확정 실적과 실적콜 전까지 추가는 보류하세요.")
    gap = []
    if ex < th["ex_review_eok"]:
        gap.append(f"AMPC 제외 영업이익 {ex:,}억원으로 2,000억원 미만")
    if sq is None:
        gap.append("매출 확인 불가")
    elif sq < th["sales_qoq_review_pct"]:
        gap.append(f"매출 전분기 대비 {sq:+.1f}%로 +5% 미만")
    return ("관망", "AMPC 제외 흑자는 유지했지만 " + ", ".join(gap),
            "아직 사지 말고 관망하세요. 1월 말 확정 실적과 실적콜을 기다리세요.")


def eok_txt(v: int | None) -> str:
    return "확인 불가" if v is None else f"{v:,}억원"


def qoq_txt(v: int | None, base: int) -> str:
    q = pct(v, base)
    return "" if q is None else f"(3분기 {base:,}억원 대비 {q:+.1f}%)"


def message(cfg: dict, p: dict, item: dict, now: datetime) -> tuple[str, list[str]]:
    q3 = cfg["q3"]
    label, why, action = verdict(p, cfg)
    sq = pct(p.get("sales"), q3["sales_eok"])
    m = p.get("ex_margin")  # only from the filing text, never computed
    sales_vs = "" if sq is None else (" 가이던스 약 +10%와 비슷" if 5 <= sq <= 15 else (" 가이던스 약 +10%보다 낮음" if sq < 5 else " 가이던스 약 +10%보다 높음"))
    if m is None:
        prof = "AMPC 제외 이익률 확인 불가"
    else:
        prof = f"AMPC 제외 이익률 {q3['ex_margin_pct']}% -> {m:.1f}%" + (
            " (둔화 예상과 일치)" if 0 <= m < q3["ex_margin_pct"] else " (예상보다 나쁨, 적자)" if m < 0 else " (둔화 예상보다 좋음)")
    net = "" if p.get("net") is None else (f", 당기순손실 {-p['net']:,}억원" if p["net"] < 0 else f", 당기순이익 {p['net']:,}억원")
    title = f"[LGESF] 373220 LG엔솔 4Q 잠정 {TITLE_LABEL.get(label, label)}"
    lines = [
        "이게 뭐 알림: LG에너지솔루션(373220) 2026년 4분기 잠정실적을 3분기와 회사 4분기 가이던스(매출 약 +10%, 수익성 일부 둔화)에 대 보고 신규 매수 여부를 봅니다. 미보유, 참고용, 투자 권유 아님.",
        (f"지금 결과/상태: 판정 {label}. 이유: {why}. "
         f"매출 {eok_txt(p.get('sales'))}{qoq_txt(p.get('sales'), q3['sales_eok'])}{sales_vs}, "
         f"영업이익 {eok_txt(p.get('op'))}{qoq_txt(p.get('op'), q3['op_eok'])}, "
         f"AMPC {eok_txt(p.get('ampc'))}{qoq_txt(p.get('ampc'), q3['ampc_eok'])}, "
         f"AMPC 제외 영업이익 {eok_txt(p.get('ex'))}{qoq_txt(p.get('ex'), q3['ex_ampc_eok'])}, {prof}{net}. "
         f"컨센서스는 쓰지 않음. 공시 {item.get('title', '')} ({item.get('datetime', '')[:16]}), {item.get('url', '')} , {cfg['disclosures']}. "
         f"조회 {now:%Y-%m-%d %H:%M} KST."),
        f"지금 할 일: {action}",
        "나중에: 1월 말 확정 실적과 실적콜에서 보조금 제외 손익, 일회성, 2027년 가이던스를 다시 확인하세요. 가격 구간은 [LGES] 알림이 따로 봅니다.",
        "익절/줄일 때: 미보유라 해당 없음. 사게 되면 AMPC 제외 적자 전환이 확인될 때 비중 재검토.",
        "하지 말 것: 잠정 숫자를 확정으로 보기, AMPC가 낀 영업이익을 본업 이익으로 보기, 한 번에 다 사기. 투자 권유 아님.",
    ]
    return title, lines


def run(cfg: dict, state: dict, now: datetime, mode: str) -> int:
    dry = mode == "dry"
    day = now.strftime("%Y-%m-%d")
    if state.get("notified") and mode != "force":
        print("4Q prelim already notified; silent")
        return 0
    try:
        rows = notices(cfg["ticker"], 30)
    except Exception as exc:  # noqa: BLE001
        print(f"::warning::notice fetch failed: {type(exc).__name__}", file=sys.stderr)
        if state.get("ops_fetch_day") != day:
            title = "[LGESF] 373220 LG엔솔 4Q 잠정 조회 실패 | 판정 유보"
            lines = [f"{now:%Y-%m-%d %H:%M} KST 공시 목록 조회 실패({type(exc).__name__}). 다음 실행에서 다시 봅니다. 매매 신호가 아닙니다."]
            if dry:
                print(title, *lines, "dry-run; no ntfy", sep="\n")
            else:
                send(cfg, "ops", title, lines, 3)
                state["ops_fetch_day"] = day
                save_state(state, now)
        return 0
    hits = [r for r in rows if is_q4_prelim(r, cfg)]
    if not hits:
        last = day >= cfg["window_end"] and now.hour >= 16
        if last and not state.get("empty_notified"):
            title = "[LGESF] 373220 LG엔솔 4Q 잠정 미게시 | 판정 유보"
            lines = [
                "이게 뭐 알림: LG에너지솔루션(373220) 4분기 잠정실적 감시 창이 끝났습니다. 매매 신호가 아닙니다.",
                f"지금 결과/상태: {cfg['window_start']}~{cfg['window_end']} 동안 '26.4Q 잠정실적 공시를 찾지 못했습니다. 조회 {now:%Y-%m-%d %H:%M} KST. {cfg['disclosures']}",
                "지금 할 일: 공시 일정을 직접 확인하세요. 필요하면 창을 늘리는 설정 변경을 요청하세요.",
            ]
            if dry:
                print(title, *lines, "dry-run; no ntfy", sep="\n")
            else:
                send(cfg, "port", title, lines, 3)  # 미게시 in the title -> cwally-ops (ntfy_util.route_topic)
                state["empty_notified"] = True
                save_state(state, now)
        else:
            print("4Q prelim not posted yet; silent")
        return 0
    item = hits[0]

    def ops(detail: str) -> None:
        title = f"[OPS] LG엔솔 4Q 잠정 파싱 실패 [{cfg['ticker']}] | 판정 유보"
        lines = [
            f"{now:%Y-%m-%d %H:%M} KST | OPS | lgesq4 4분기 잠정",
            "이게 뭐 알림: 4분기 잠정실적 공시는 찾았지만 숫자 읽기(parse)에서 오류가 났습니다. 투자 판단 알림이 아닙니다.",
            f"지금 상태: 공시 {item.get('id')}, 오류 {detail}. 이번 실행은 판정 없이 끝났고 다음 실행에서 다시 읽습니다.",
            f"지금 할 일: 로그의 ::error:: 줄과 공시 원문을 확인하세요. {cfg['disclosures']}",
        ]
        if dry:
            print(title, *lines, "dry-run; no ntfy", sep="\n")
        else:
            send(cfg, "ops", title, lines, 3)

    before = state.get("ops_parse_day")
    p = safe_parse(lambda: parse(item), state=state, day=day, ops_notify=ops, dry=dry, label="lgesq4 parse")
    if p is None:
        if not dry and state.get("ops_parse_day") != before:
            save_state(state, now)
        return 0
    title, lines = message(cfg, p, item, now)
    if dry:
        print(title, *lines, "dry-run; no ntfy", sep="\n")
        return 0
    send(cfg, "port", title, lines, 4)
    state.update({"notified": True, "last_key": item.get("id"), "verdict": verdict(p, cfg)[0],
                  "sent_at": now.isoformat(timespec="seconds"),
                  "parsed": {k: p.get(k) for k in ("sales", "op", "ampc", "ex", "ex_margin", "net")}})
    save_state(state, now)
    return 0


# ---------------- selftest ----------------
# FIXTURE (not real 4Q26 numbers): text in the real DART 공정공시 format of the 2026-01-09 4Q25 and
# 2026-10-08/11-03 3Q26 filings, with made-up '26.4Q figures to exercise each verdict.
def fixture(sales: str, op: str, ex: str, margin: str) -> str:
    return (
        "연결재무제표 기준 영업(잠정)실적(공정공시) 실적기간 당기실적 2026-10-01 ~ 2026-12-31 전기실적 2026-07-01 ~ 2026-09-30 "
        "1. 연결실적내용 단위 : 억원, % 구분 당기실적 전기실적 ('26.4Q) ('26.3Q) "
        f"매출액 당해실적 {sales} 96,434 - - - 누계실적 - - - 영업이익 당해실적 {op} 7,560 - - - "
        "법인세비용차감전계속사업이익 당해실적 - - - - 당기순이익 당해실적 - - - - "
        f"4. 기타 투자판단과 관련한 중요사항 나. 상기 1. 연결실적내용 중, '26.4Q 영업이익 당기실적' 금액인 {op}억원은 미국 IRA Tax Credit "
        "(Inflation Reduction Act) 첨단 제조 생산 세액공제(Advanced Manufacturing Production Credit) 제도(45X) 등에 따른 "
        f"2026년 4분기 예상 세액 공제 금액을 포함한 수치이며, 이를 제외한 2026년 4분기 영업이익은 {ex}억원, 영업이익률은 {margin}%임"
    )


def selftest() -> int:
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    fails = 0

    def check(name, got, want):
        nonlocal fails
        ok = got == want
        print(("ok  " if ok else "FAIL"), name, got, "" if ok else f"want {want}")
        fails += not ok

    def item(text, title="연결재무제표기준영업(잠정)실적(공정공시)", dt="2027-01-08T13:05:00"):
        return {"id": "fx", "title": title, "text": text, "datetime": dt, "url": "https://m.stock.naver.com/domestic/stock/373220/notice"}

    cases = [
        ("FIXTURE 검토 가능", fixture("106,100", "6,300", "2,350", "+2.3"), "검토 가능", {"sales": 106100, "op": 6300, "ex": 2350, "ampc": 3950}),
        ("FIXTURE 관망 (ex 2,000 미만)", fixture("105,000", "5,200", "1,100", "+1.1"), "관망", {"ex": 1100, "ampc": 4100}),
        ("FIXTURE 관망 (매출 +5% 미만)", fixture("98,000", "6,500", "2,500", "+2.6"), "관망", {"sales": 98000}),
        ("FIXTURE 보류 (ex 적자)", fixture("101,000", "2,900", "-1,300", "-1.3"), "보류", {"ex": -1300, "ampc": 4200, "ex_margin": -1.3}),
        ("FIXTURE 보류 (op 적자)", fixture("101,000", "-500", "-4,600", "-4.6"), "보류", {"op": -500}),
    ]
    for name, text, want, nums in cases:
        p = parse(item(text))
        check(f"{name} verdict", verdict(p, cfg)[0], want)
        for k, v in nums.items():
            check(f"{name} {k}", p[k], v)
        t, lines = message(cfg, p, item(text), datetime(2027, 1, 8, 13, 47, tzinfo=KST))
        check(f"{name} title", (t.startswith("[LGESF] 373220 "), "매수" in t, "신호" in t), (True, False, False))
        body = "\n".join([t] + lines)
        check(f"{name} no middle dot/em-dash", any(c in body for c in "·ㆍ—"), False)
    check("title 검토 가능", message(cfg, parse(item(cases[0][1])), item(cases[0][1]), datetime(2027, 1, 8, tzinfo=KST))[0],
          "[LGESF] 373220 LG엔솔 4Q 잠정 검토 가능")
    check("unparsed -> 관망", verdict({"op": None, "ex": None, "sales": None}, cfg)[0], "관망")
    # matching: Q3 정정 (real 11/3 title) and the 10/8 Q3 prelim must not match; Q4 must
    q3_corr = item("정정사항 '26.3Q 2026-07-01 ~ 2026-09-30", "(정정)연결재무제표기준영업(잠정)실적(공정공시)", "2026-11-03T16:00:00")
    check("Q3 정정 not matched", is_q4_prelim(q3_corr, cfg), False)
    check("Q4 matched", is_q4_prelim(item(cases[0][1]), cfg), True)
    check("Q4 IR 안내 not matched", is_q4_prelim(item("2026년 4분기 실적설명회", "기업설명회(IR)개최(안내공시)"), cfg), False)
    check("old stamp not matched", is_q4_prelim(item(cases[0][1], dt="2026-12-30T09:00:00"), cfg), False)
    # run() paths with fake notices (dry: print only)
    import io, contextlib
    g = globals()
    real_notices, real_send, real_save = g["notices"], g["send"], g["save_state"]
    sent: list = []
    g["send"] = lambda *a, **k: sent.append(a[2])
    g["save_state"] = lambda *a, **k: None
    try:
        def out_of(rows, at, mode="dry", st=None):
            g["notices"] = (lambda *_a: rows) if not isinstance(rows, Exception) else (lambda *_a: (_ for _ in ()).throw(rows))
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
                run(cfg, st if st is not None else {}, at, mode)
            return buf.getvalue()
        at = datetime(2027, 1, 8, 13, 47, tzinfo=KST)
        check("dry found", out_of([item(cases[0][1])], at).splitlines()[0], "[LGESF] 373220 LG엔솔 4Q 잠정 검토 가능")
        check("dry none mid-window silent", out_of([q3_corr], at).strip(), "4Q prelim not posted yet; silent")
        check("dry none last run", out_of([], datetime(2027, 1, 15, 16, 47, tzinfo=KST)).splitlines()[0], "[LGESF] 373220 LG엔솔 4Q 잠정 미게시 | 판정 유보")
        check("dry fetch fail", out_of(RuntimeError("x"), at).splitlines()[0], "[LGESF] 373220 LG엔솔 4Q 잠정 조회 실패 | 판정 유보")
        check("dry sends nothing", sent, [])
        st = {}
        out_of([item(cases[0][1])], at, "auto", st)
        check("auto sends once", (sent, st.get("notified"), st.get("verdict")), (["[LGESF] 373220 LG엔솔 4Q 잠정 검토 가능"], True, "검토 가능"))
        out_of([item(cases[0][1])], at, "auto", st)
        check("auto no resend", len(sent), 1)
        st2: dict = {}
        out_of([], datetime(2027, 1, 15, 16, 47, tzinfo=KST), "auto", st2)
        out_of([], datetime(2027, 1, 15, 16, 47, tzinfo=KST), "auto", st2)
        check("미게시 once", (sent[1:], st2.get("empty_notified")), (["[LGESF] 373220 LG엔솔 4Q 잠정 미게시 | 판정 유보"], True))
        g["parse"] = lambda _i: (_ for _ in ()).throw(ValueError("bad"))
        st3: dict = {}
        out_of([item(cases[0][1])], at, "auto", st3)
        out_of([item(cases[0][1])], at, "auto", st3)
        check("parse fail ops once/day", (sent[2:], st3.get("ops_parse_day"), st3.get("notified")),
              (["[OPS] LG엔솔 4Q 잠정 파싱 실패 [373220] | 판정 유보"], "2027-01-08", None))
    finally:
        g["notices"], g["send"], g["save_state"] = real_notices, real_send, real_save
        g["parse"] = parse_ref
    from ntfy_util import route_topic
    check("미게시 routes to ops", route_topic("cwally-port", "[LGESF] 373220 LG엔솔 4Q 잠정 미게시 | 판정 유보"), "cwally-ops")
    check("result routes to port", route_topic("cwally-port", "[LGESF] 373220 LG엔솔 4Q 잠정 관망"), "cwally-port")
    check("oneshot count", len(cfg["oneshot_kst"]), len(cfg["cron_utc"]))
    print("lgesq4 selftest", "PASS" if not fails else f"FAIL {fails}")
    return 1 if fails else 0


parse_ref = parse


def main() -> int:
    args = sys.argv[1:]
    mode = (args[0] if args else "auto").lower()
    if mode == "selftest":
        return selftest()
    if mode not in {"auto", "dry", "force"}:
        raise SystemExit(f"unknown mode: {mode}")
    now = datetime.now(KST)
    if "--now" in args:
        if mode != "dry":
            raise SystemExit("--now is for dry only")
        now = datetime.strptime(args[args.index("--now") + 1], "%Y-%m-%d %H:%M").replace(tzinfo=KST)
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    state = read_state()
    day = now.strftime("%Y-%m-%d")
    if mode == "dry":
        print(f"DRY RUN (no ntfy, no state write) now={now:%Y-%m-%d %H:%M} KST")
    if now.year != int(cfg["year_guard"]) or not cfg["window_start"] <= day <= cfg["window_end"]:
        print("outside 2027-01 4Q prelim window; silent")
        return 0
    return run(cfg, state, now, mode)


if __name__ == "__main__":
    raise SystemExit(main())
