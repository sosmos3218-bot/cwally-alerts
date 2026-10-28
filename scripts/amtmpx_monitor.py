#!/usr/bin/env python3
"""[AMTM-PX] Amentum (AMTM, NYSE) close-based zones (approved in chat 2026-10-28).

  be2399   close >= 23.99  본전 회복         p3 info
  ma200    close >= 24.67  200일선 회복       p3 info
  low20    close <= 20.00                     p4  (before the Q4 8-K: 추가매수 보류; after: conditional limit add wording)
  stop1772 close <= 17.72  52주 저점 이탈     p5 warning (no buy wording; overrides low20 wording)

Each zone fires once when a completed close ENTERS it and re-arms quietly when a close leaves it (figpx pattern).
Same-run hits are combined, highest priority wins. Earnings state comes from state/amtmearn.json (found_accession).
Runs on the existing morning 09:00 KST slot (after the US close). No new cron.
Modes: auto | dry (print only, no ntfy, never writes state) | confirm (install-confirmation send, no state) | selftest.
Not investment advice.
"""
from __future__ import annotations

import json
import sys
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
NY = ZoneInfo("America/New_York")
CFG = ROOT / "configs" / "amtmpx.json"
STATE = ROOT / "state" / "amtmpx.json"
EARN_STATE = ROOT / "state" / "amtmearn.json"
UA = {"User-Agent": "Mozilla/5.0 cwally-alerts"}
NAME = "[AMTM-PX]"
BUY_WORDS = ("매수", "추가 검토", "지정가", "분할", "비중 확대", "투입", "담기")
ALLOWED_HOLD = ("추가매수 보류",)
BANNED_CHARS = ("\u00b7", "\u2014")
WARN_TODO = "추가매수 보류, 투자 논리(FY27 매출, FCF 성장, 레버리지) 재점검, 정리 여부 검토."
DRY = False


def load_cfg() -> dict:
    return json.loads(CFG.read_text(encoding="utf-8"))


def load_json(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except json.JSONDecodeError:
        return {}


def save_state(st: dict, dry: bool) -> None:
    if dry or DRY:
        print("dry: state not saved")
        return
    st["updated_at"] = datetime.now(KST).isoformat(timespec="seconds")
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def send(kind: str, title: str, body: str, prio: int, tags: list[str], dry: bool) -> None:
    if dry or DRY:
        print(f"DRY RUN would send ({kind}, p{prio}): {title}\n{body}")
        return
    publish(resolve_topic(kind), title, body, prio, tags=tags, config_name="amtmpx")


def fetch_bars(cfg: dict, now_ny: datetime) -> list[tuple[date, float]]:
    """Completed daily bars (US/Eastern date, close), ascending. Today's live bar is excluded."""
    req = urllib.request.Request(cfg["sources"]["bars"], headers=UA)
    with urllib.request.urlopen(req, timeout=20) as resp:
        res = json.loads(resp.read().decode())["chart"]["result"][0]
    out: dict[date, float] = {}
    for t, c in zip(res.get("timestamp") or [], res["indicators"]["quote"][0].get("close") or []):
        if c is not None:
            out[datetime.fromtimestamp(t, NY).date()] = float(c)
    meta = res.get("meta") or {}
    rmt, rmp = meta.get("regularMarketTime"), meta.get("regularMarketPrice")
    if rmt and rmp:
        mt = datetime.fromtimestamp(rmt, NY)
        if mt.date() not in out and mt.hour * 60 + mt.minute >= 16 * 60:
            out[mt.date()] = float(rmp)
    today = now_ny.date()
    after_close = now_ny.hour * 60 + now_ny.minute >= 16 * 60 + 10
    return sorted((d, c) for d, c in out.items() if d < today or (d == today and after_close))


# ---------- core (pure) ----------

def hit(lv: dict, close: float) -> bool:
    return close >= lv["price"] if lv["op"] == "ge" else close <= lv["price"]


def step(levels: list[dict], armed: dict | None, close: float) -> tuple[dict, list[str]]:
    """armed None = first run: lines already inside their zone start disarmed, nothing fires."""
    if armed is None:
        return {lv["key"]: not hit(lv, close) for lv in levels}, []
    new, fired = dict(armed), []
    for lv in levels:
        k = lv["key"]
        if hit(lv, close):
            if new.get(k, True):
                fired.append(k)
            new[k] = False
        else:
            new[k] = True
    return new, fired


def earnings_found(est: dict | None = None) -> str | None:
    est = load_json(EARN_STATE) if est is None else est
    return est.get("found_accession")


def pnl_text(cfg: dict, close: float) -> str:
    pos = cfg["position"]
    n, avg = int(pos["shares"]), float(pos["avg_cost"])
    usd = (close - avg) * n
    pct = (close / avg - 1) * 100
    return f"보유 {n}주 평단 {avg:.4f}달러 기준 평가손익 {usd:+,.2f}달러({pct:+.1f}%)"


def message(cfg: dict, fired: list[str], close: float, day: str, prev_close: float | None, post: bool) -> tuple[str, str, int]:
    L = {lv["key"]: lv for lv in cfg["levels"]}
    fired = [lv["key"] for lv in cfg["levels"] if lv["key"] in fired]
    prio = max(int(L[k]["priority"]) for k in fired)
    warn = "stop1772" in fired
    todos: list[str] = []
    if warn:
        todos.append("52주 저점 이탈. " + WARN_TODO)
        act = "추가매수 보류, 논리 재점검"
    elif "low20" in fired:
        if post:
            todos.append("20달러 이하. 실적 체크리스트에서 시나리오 1 또는 2 확인된 경우에만 지정가 추가 검토. "
                         "시나리오 3(FY27 매출 감소 또는 FCF 성장 철회)이면 추가하지 않고 정리 여부 검토.")
            act = "시나리오 확인된 경우만 지정가 검토"
        else:
            todos.append("20달러 이하. 실적 발표 전, 추가매수 보류. 11/24 가이던스 확인 후 재판단.")
            act = "실적 전 추가매수 보류"
    else:
        if "ma200" in fired:
            todos.append("200일선(24.67달러) 회복. 추세 회복 신호지만 하루 종가로 단정하지 않기.")
        if "be2399" in fired:
            todos.append("본전(평단 23.99달러) 회복.")
        todos.append("보유 유지, 실적 전에는 추가매수 보류." if not post else "보유 유지. 비중 조정은 실적 체크리스트 결과 기준으로만.")
        act = "보유 유지"
    labels = ", ".join(L[k]["label"] for k in fired)
    title = f"{NAME} 종가 {close:.2f}달러 {L[fired[-1] if warn or 'low20' in fired else fired[0]]['label'].split('(')[0]} | {act}"
    chg = f", 전 거래일 대비 {(close / prev_close - 1) * 100:+.1f}%" if prev_close else ""
    earn = "실적 8-K 감지됨" if post else "실적(FY26 4분기, 11/24 콜 전 발표) 확인 전"
    if warn:
        trim = "정리 여부는 투자 논리 재점검 결과로 결정. 같은 해 다른 해외주식 이익이 있으면 손실 상계 고려."
        dont = "물타기, 장중 급락 추격, 신용/미수, 이 알림만 보고 주문. 투자 권유 아님."
    elif "low20" in fired:
        trim = "시나리오 3이면 정리 검토(같은 해 해외주식 이익과 손실 상계 고려)."
        dont = ("체크리스트 확인 전 추가" if post else "실적 확인 전 추가") + ", 장중 급락 추격, 신용/미수, 이 알림만 보고 주문. 투자 권유 아님."
    else:
        trim = "본전 회복만으로 서둘러 팔지 않기. 정리는 실적 시나리오 3일 때 검토."
        dont = "추격 매수, 실적 전 비중 늘리기, 신용/미수, 이 알림만 보고 주문. 투자 권유 아님."
    lines = [
        f"{datetime.now(KST):%Y-%m-%d %H:%M} KST | AMTM | 아멘텀",
        "이게 뭐 알림: " + cfg["explain"] + " " + cfg["source_note"],
        f"지금 결과/상태: 새로 도달: {labels}. 미국 종가 {close:.2f}달러({day} 미 동부{chg}). {pnl_text(cfg, close)}. {earn}.",
        "지금 할 일: " + " ".join(todos),
        "나중에: 같은 구간은 종가가 구간을 벗어난 뒤 다시 들어올 때만 또 울립니다(평일 09:00 점검). 선: 23.99, 24.67 이상, 20.00, 17.72 이하.",
        "익절/줄일 때: " + trim,
        "하지 말 것: " + dont,
    ]
    return title, "\n".join(lines), prio


def confirm_message(cfg: dict, close: float | None, day: str | None, armed: dict) -> tuple[str, str]:
    px = f"{close:.2f}달러({day} 미 동부 종가), {pnl_text(cfg, close)}" if close is not None else "조회 실패(구간 미확인)"
    inside = [lv["label"] for lv in cfg["levels"] if armed.get(lv["key"]) is False]
    lines = [
        "[테스트] AMTM 알림 설정 완료",
        "이게 뭐 알림: " + cfg["explain"] + " " + cfg["source_note"],
        f"지금 결과/상태: AMTM 현재 {px}. 이미 구간 안인 선: {', '.join(inside) if inside else '없음(모두 대기)'}.",
        "지금 할 일: 없음. 평일 09:00 KST 에 전일 미국 종가로 판정. 실적 사전 알림(11/23 09:00)과 8-K 감지(11/24~12/5, 09:00과 19:00)는 [AMTM-EARN] 으로 옵니다.",
        "나중에: 같은 구간은 종가가 구간을 벗어난 뒤 다시 들어올 때만 또 울립니다.",
        "익절/줄일 때: 해당 없음.",
        "하지 말 것: 이 설치 확인을 매수/매도 신호로 읽기.",
    ]
    return f"{NAME} 설치 확인 | 매매 신호 아님", "\n".join(lines)


# ---------- run ----------

def run(cfg: dict, dry: bool, now: datetime, bars_fn=fetch_bars) -> int:
    st = load_json(STATE)
    try:
        bars = bars_fn(cfg, now.astimezone(NY))
    except Exception as exc:
        print(f"bars fetch failed: {exc}")
        today = now.strftime("%Y-%m-%d")
        if st.get("fail_day") != today:
            send("ops", f"{NAME} 시세 조회 실패 | 매매 신호 아님", f"AMTM 일봉 조회 실패: {exc}. 다음 09:00 실행에서 다시 봅니다.", 3, ["warning"], dry)
            st["fail_day"] = today
            save_state(st, dry)
        return 0
    if not bars:
        print("no completed bars")
        return 0
    L = cfg["levels"]
    last_d, last_c = bars[-1]
    if (now.date() - last_d).days > int(cfg.get("stale_days", 6)):
        print(f"stale bar {last_d}; skip")
        return 0
    armed = st.get("armed") if st.get("initialized") else None
    if armed is None:
        armed, _ = step(L, None, last_c)
        st.update({"initialized": True, "armed": armed, "last_date": str(last_d), "last_close": round(last_c, 4),
                   "init_note": f"initialized from {last_d} close {last_c:.2f}, no send"})
        print(f"init from {last_d} close {last_c:.2f}: {armed} (no send)")
        save_state(st, dry)
        return 0
    prev_date = st.get("last_date")
    new_bars = [(d, c) for d, c in bars if prev_date is None or str(d) > prev_date]
    if not new_bars:
        print(f"no new bar after {prev_date}; armed={armed}")
        return 0
    fired_all: list[str] = []
    prev_close = st.get("last_close")
    for d, c in new_bars:
        armed, fired = step(L, armed, c)
        print(f"bar {d} close {c:.2f} -> fired={fired} armed={armed}")
        fired_all += [k for k in fired if k not in fired_all]
    d, c = new_bars[-1]
    if len(new_bars) > 1:
        prev_close = new_bars[-2][1]
    if fired_all:
        title, msg, prio = message(cfg, fired_all, c, str(d), prev_close, bool(earnings_found()))
        tag = "warning" if prio >= 4 else "chart_with_upwards_trend"
        send("port", title, msg, prio, [tag], dry)
        st["last_sent"] = {"day": str(d), "fired": fired_all, "title": title}
    else:
        print("no zone entry; silent")
    st.update({"armed": armed, "last_date": str(d), "last_close": round(c, 4)})
    save_state(st, dry)
    return 0


def main(argv: list[str]) -> int:
    global DRY
    mode = (argv[1] if len(argv) > 1 else "auto").lower()
    if mode == "selftest":
        return selftest()
    cfg = load_cfg()
    now = datetime.now(KST)
    print(f"{now.isoformat(timespec='seconds')} amtmpx mode={mode}")
    if mode == "confirm":
        try:
            d, c = fetch_bars(cfg, now.astimezone(NY))[-1]
            title, msg = confirm_message(cfg, c, str(d), load_json(STATE).get("armed") or {})
        except Exception as exc:
            print(f"bars fetch failed: {exc}")
            title, msg = confirm_message(cfg, None, None, {})
        print(title)
        print(msg)
        publish(resolve_topic("port"), title, msg, 3, tags=["white_check_mark"], config_name="amtmpx")
        return 0
    dry = mode != "auto"
    DRY = dry
    if dry:
        print("DRY RUN (no ntfy, no state write)")
    if now.date() > date.fromisoformat(cfg["valid_until"]):
        print(f"expired (valid_until {cfg['valid_until']}); silent")
        return 0
    return run(cfg, dry, now)


# ---------- selftest ----------

def selftest() -> int:
    import io
    import tempfile
    from contextlib import redirect_stdout
    from unittest import mock

    global STATE, EARN_STATE, DRY
    cfg = load_cfg()
    L = cfg["levels"]
    fails = 0

    def check(label, got, want):
        nonlocal fails
        ok = got == want
        fails += not ok
        print(("ok  " if ok else "FAIL"), label, "" if ok else f"got {got!r} want {want!r}")

    A = {"be2399": True, "ma200": True, "low20": True, "stop1772": True}
    check("init 21.75 all armed", step(L, None, 21.75), (A, []))
    check("init 19.5 low20 disarmed, no send", step(L, None, 19.5), ({**A, "low20": False}, []))
    check("init 25 be+ma disarmed, no send", step(L, None, 25.0), ({**A, "be2399": False, "ma200": False}, []))
    check("23.98 nothing", step(L, A, 23.98)[1], [])
    check("23.99 breakeven (>=)", step(L, A, 23.99)[1], ["be2399"])
    check("24.67 both up lines", step(L, A, 24.67)[1], ["be2399", "ma200"])
    check("20.01 nothing", step(L, A, 20.01)[1], [])
    check("20.00 low20 (<=)", step(L, A, 20.0)[1], ["low20"])
    check("17.72 gap low20+stop", step(L, A, 17.72)[1], ["low20", "stop1772"])
    walk = [(19.8, ["low20"]), (19.0, []), (20.5, []), (19.9, ["low20"]), (17.5, ["stop1772"]), (17.0, []),
            (18.0, []), (17.7, ["stop1772"]), (24.0, ["be2399"]), (24.7, ["ma200"]), (24.5, []), (24.8, ["ma200"])]
    st = dict(A)
    for i, (c, want) in enumerate(walk):
        st, f = step(L, st, c)
        check(f"walk{i} {c}", f, want)

    def todo(b):
        return next(x for x in b.split("\n") if x.startswith("지금 할 일:"))

    def buy(text):
        for ok in ALLOWED_HOLD:
            text = text.replace(ok, "")
        return [w for w in BUY_WORDS if w in text]

    out = {}
    for name, fired, close, post in [("be", ["be2399"], 23.99, False), ("ma", ["be2399", "ma200"], 24.7, False),
                                     ("low_pre", ["low20"], 19.9, False), ("low_post", ["low20"], 19.9, True),
                                     ("stop", ["stop1772"], 17.5, False), ("both", ["low20", "stop1772"], 17.6, False),
                                     ("both_post", ["low20", "stop1772"], 17.6, True)]:
        t, b, p = message(cfg, fired, close, "2026-11-27", 21.0, post)
        out[name] = (t, b, p)
        check(f"{name} no middle dot / em-dash", any(ch in t + b for ch in BANNED_CHARS), False)
        check(f"{name} six-part order", [x.split(":")[0] for x in b.split("\n")[1:7]],
              ["이게 뭐 알림", "지금 결과/상태", "지금 할 일", "나중에", "익절/줄일 때", "하지 말 것"])
        check(f"{name} P/L line", "보유 34주 평단 23.9888달러 기준 평가손익" in b, True)
        check(f"{name} source note", "붙여넣은 외부 분석(2026-10-28)" in b and "투자 권유 아님" in b, True)
    check("P/L math 17.50", "-220.62달러(-27.0%)" in out["stop"][1], True)
    check("P/L math 23.99 ~ 0", "+0.04달러(+0.0%)" in out["be"][1], True)
    check("low_pre wording", "실적 발표 전, 추가매수 보류. 11/24 가이던스 확인 후 재판단." in todo(out["low_pre"][1]), True)
    check("low_pre no buy wording", buy(out["low_pre"][0] + out["low_pre"][1]), [])
    check("low_post conditional wording", "시나리오 1 또는 2 확인된 경우에만 지정가 추가 검토" in todo(out["low_post"][1]), True)
    for n in ("stop", "both", "both_post"):
        check(f"{n} warning: no buy wording anywhere", buy(out[n][0] + "\n" + out[n][1]), [])
        check(f"{n} warning todo", WARN_TODO in todo(out[n][1]), True)
        check(f"{n} priority 5", out[n][2], 5)
    check("be priority 3", out["be"][2], 3)
    check("low priority 4", out["low_pre"][2], 4)
    check("earnings_found reads accession", (earnings_found({"found_accession": "x"}), earnings_found({})), ("x", None))
    t, b = confirm_message(cfg, 21.75, "2026-10-27", A)
    check("confirm title", t, "[AMTM-PX] 설치 확인 | 매매 신호 아님")
    check("confirm no middle dot / em-dash", any(ch in t + b for ch in BANNED_CHARS), False)

    old = (STATE, EARN_STATE)
    sent = []
    try:
        with tempfile.TemporaryDirectory() as td:
            STATE, EARN_STATE = Path(td) / "amtmpx.json", Path(td) / "amtmearn.json"
            STATE.write_text(json.dumps({"initialized": True, "armed": A, "last_date": "2026-10-27", "last_close": 21.75}), encoding="utf-8")
            before = STATE.read_bytes()
            bars = lambda c, n: [(date(2026, 10, 27), 21.75), (date(2026, 10, 28), 17.5)]  # noqa: E731
            now = datetime(2026, 10, 29, 9, 0, tzinfo=KST)
            with mock.patch(f"{__name__}.publish", side_effect=lambda *a, **k: sent.append(a[1])), redirect_stdout(io.StringIO()):
                DRY = True
                run(cfg, True, now, bars_fn=bars)
                DRY = False
                check("dry: state unchanged", STATE.read_bytes() == before, True)
                check("dry: nothing published", sent, [])
                run(cfg, False, now, bars_fn=bars)
                run(cfg, False, now, bars_fn=bars)
            check("auto: one send, dedupe by bar date", len(sent), 1)
            check("auto: warning title", "추가매수 보류" in sent[0], True)
    finally:
        STATE, EARN_STATE = old
        DRY = False
    print("\n=== sample 17.72 warning (with 20 line, pre-earnings) ===")
    print(out["both"][2], out["both"][0])
    print(out["both"][1])
    print("\n=== sample 20 post-earnings ===")
    print(out["low_post"][2], out["low_post"][0])
    print(out["low_post"][1])
    print("selftest", "PASS" if not fails else f"FAIL {fails}")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
