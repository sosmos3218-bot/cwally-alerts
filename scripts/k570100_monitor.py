#!/usr/bin/env python3
"""[570100] 한투 일본종합상사TOP5 ETN 진입/닛케이/엔원/일일요약 (2026-11-06 요청 pt13u).

KRX 거래일 19:00 k203780 슬롯(장 마감 후). 판정:
  1) 종가 19,000원 이하 교차 1번 -> 19,400원 위 재무장
  2) 닛케이225(^N225) 완료 일봉 종가 55,000 미만 1번 -> 56,000 위 재무장. 제목 '570100/N225'
  3) 100엔당 원(Yahoo JPYKRW=X*100)이 약 20거래일 전 대비 +5% 초과 1번
     -> +3% 미만으로 돌아오면 재무장. 제목 '570100 / JPYKRW'
  4) 거래일 일일 요약 1번: 종가, 전일 대비 %, 닛케이, 100엔당 원, 19,000까지 거리
소스가 실패하면 그 규칙만 이번 실행에서 건너뜀. 같은 소스가 2일 연속 실패하면 cwally-ops 1번.
modes: auto | dry(발송 없음, state 안 씀) | install(설치 확인 1회, state 안 씀) | selftest
"""
from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic, route_topic  # noqa: E402
from kr_calendar import KST, is_trading_day, session_day  # noqa: E402

NAME = "k570100"
CFG_PATH = ROOT / "configs" / f"{NAME}.json"
STATE_PATH = ROOT / "state" / f"{NAME}.json"
UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}
TAG = "[570100]"
CODE = "570100"
NAME_KR = "한투 일본종합상사TOP5 ETN"
BANNED = ("\u00b7", "\u2014")


def default_state() -> dict:
    return {
        "updated_at": None,
        "price_low_armed": True,
        "price_last_date": None,
        "n225_low_armed": True,
        "n225_last": None,
        "jpy_up_armed": True,
        "jpy100_last": None,
        "jpy100_base": None,
        "jpy_chg_pct_last": None,
        "last_summary_date": None,
        "fail": {},
    }


def load_state() -> dict:
    st = default_state()
    if STATE_PATH.exists():
        try:
            st.update(json.loads(STATE_PATH.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass
    return st


def save_state(st: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def http_json(url: str, timeout: int = 25) -> dict:
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


# ---------------- 데이터 ----------------
def yahoo_daily_closes(symbol: str, range_: str = "3mo") -> list[tuple[str, float]]:
    """[(YYYY-MM-DD KST date of bar, close)] oldest first. Latest completed daily close last."""
    errs: list[str] = []
    enc = urllib.parse.quote(symbol, safe="")
    for host in ("query1", "query2"):
        url = f"https://{host}.finance.yahoo.com/v8/finance/chart/{enc}?interval=1d&range={range_}"
        try:
            res = http_json(url)["chart"]["result"][0]
            ts = res.get("timestamp") or []
            closes = (res.get("indicators") or {}).get("quote", [{}])[0].get("close") or []
            out: list[tuple[str, float]] = []
            for t, c in zip(ts, closes):
                if c is None:
                    continue
                day = datetime.fromtimestamp(int(t), tz=timezone.utc).astimezone(KST).date().isoformat()
                out.append((day, float(c)))
            if len(out) < 5:
                errs.append(f"{host}: short {len(out)}")
                continue
            # de-dupe by day keeping last
            by: dict[str, float] = {}
            for d, v in out:
                by[d] = v
            return [(d, by[d]) for d in sorted(by)]
        except Exception as exc:  # noqa: BLE001
            errs.append(f"{host}: {exc}")
    raise RuntimeError("; ".join(errs) or f"{symbol} empty")


def n225_series() -> list[tuple[str, float]]:
    return yahoo_daily_closes("^N225", "3mo")


def jpy100_series() -> list[tuple[str, float]]:
    """100엔당 원 = Yahoo JPYKRW=X (1엔당 원) * 100."""
    raw = yahoo_daily_closes("JPYKRW=X", "3mo")
    return [(d, v * 100.0) for d, v in raw]


def price_bars(today: date) -> list[tuple[str, float]]:
    """ETN 570100: Daum 미지원. krx_close가 Naver minute 15:30 폴백."""
    import krx_close

    bars = [b for b in krx_close.regular_bars(CODE, 15) if b.day <= today.isoformat()]
    if not bars:
        raise RuntimeError("종가 없음(Daum 미지원 ETN, minute 15:30도 실패)")
    return [(b.day, float(b.close)) for b in bars]


# ---------------- 본문 ----------------
def body(stamp: str, text: str, action: str, later: str = "", avoid: str = "") -> str:
    lines = [
        f"{stamp} | {CODE} | {NAME_KR}",
        f"이게 뭐 알림: {CODE}({NAME_KR}) 종가 19,000원 이하, 닛케이 55,000 하회, "
        f"100엔당 원 20거래일 대비 +5% 초과, 장 마감 후 일일 요약. 매매 지시 아님.",
        f"지금 결과/상태: {text}",
        f"지금 할 일: {action}",
        f"나중에: {later or '같은 조건은 재무장 선을 넘었다가 다시 와야 알림이 옵니다.'}",
        "익절/줄일 때: 계획한 비중이 있으면 그 안에서만. 없으면 새로 비중을 정한 뒤.",
        f"하지 말 것: {avoid or '조회 실패를 신호로 읽기, 하루 환율/닛케이 움직임으로 크게 들어가기, 유동성 낮은 ETN 대량 추격.'}",
    ]
    return "\n".join(lines)


# ---------------- 판정 ----------------
def evaluate(cfg: dict, st: dict, obs: dict, today: str) -> list[tuple[str, str, int]]:
    r = cfg["rules"]
    out: list[tuple[str, str, int]] = []
    bars = obs.get("bars")
    n225 = obs.get("n225")
    jpy = obs.get("jpy100")

    if n225:
        nd, nv = n225[-1]
        if nv < r["n225_low"] and st.get("n225_low_armed", True):
            st["n225_low_armed"] = False
            out.append(
                (
                    f"{TAG} 570100/N225 {nv:,.0f} | 닛케이 55,000 하회",
                    f"닛케이225 종가 {nv:,.2f}({nd}), {r['n225_low']:,} 아래. "
                    f"일본 증시 안정(55k 지지) 조건이 깨진 쪽. "
                    f"{r['n225_low_rearm']:,} 위로 올라가야 다시 무장.",
                    4,
                )
            )
        elif nv > r["n225_low_rearm"]:
            st["n225_low_armed"] = True
        st["n225_last"] = {"date": nd, "close": nv}

    if jpy and len(jpy) > r["jpy_lookback_bars"]:
        cur_d, cur = jpy[-1]
        base_d, base = jpy[-(r["jpy_lookback_bars"] + 1)]
        chg = (cur / base - 1.0) * 100.0
        st["jpy100_last"] = {"date": cur_d, "value": cur}
        st["jpy100_base"] = {"date": base_d, "value": base}
        st["jpy_chg_pct_last"] = chg
        if chg > r["jpy_up_pct"] and st.get("jpy_up_armed", True):
            st["jpy_up_armed"] = False
            out.append(
                (
                    f"{TAG} 570100 / JPYKRW {cur:,.1f}원 | 100엔당 +{chg:.1f}% (20거래일)",
                    f"100엔당 {cur:,.1f}원({cur_d}), {r['jpy_lookback_bars']}거래일 전 "
                    f"{base:,.1f}원({base_d}) 대비 {chg:+.2f}%. "
                    f"엔화 강세 구간(+{r['jpy_up_pct']:.0f}% 초과). 환노출 ETN 매력 하락 점검. "
                    f"+{r['jpy_up_rearm_pct']:.0f}% 미만으로 돌아와야 다시 무장.",
                    3,
                )
            )
        elif chg < r["jpy_up_rearm_pct"]:
            st["jpy_up_armed"] = True

    if bars:
        day, close = bars[-1]
        prev = bars[-2][1] if len(bars) >= 2 else None
        if close <= r["price_low"] and st.get("price_low_armed", True):
            st["price_low_armed"] = False
            out.append(
                (
                    f"{TAG} 종가 {close:,.0f}원 | 19,000원 이하 진입 구간",
                    f"{day} 종가 {close:,.0f}원, 진입 참고선 {r['price_low']:,}원 이하. "
                    f"재무장선 {r['price_low_rearm']:,}원 위로 올라가야 다시 무장. "
                    f"유동성 낮은 ETN이라 분할만.",
                    4,
                )
            )
        elif close > r["price_low_rearm"]:
            st["price_low_armed"] = True
        st["price_last_date"] = day

        if day == today and st.get("last_summary_date") != today:
            chg = ((close / prev - 1) * 100) if prev else None
            chg_txt = f"{chg:+.2f}%" if chg is not None else "비교 불가"
            if n225:
                n_txt = f"{n225[-1][1]:,.0f}({n225[-1][0]})"
            else:
                n_txt = "확인 불가"
            if jpy:
                j_txt = f"{jpy[-1][1]:,.1f}원"
                if st.get("jpy_chg_pct_last") is not None:
                    j_txt += f"(20거래일 {st['jpy_chg_pct_last']:+.2f}%)"
            else:
                j_txt = "확인 불가"
            out.append(
                (
                    f"{TAG} 일일 요약 | 종가 {close:,.0f}원",
                    f"{day} 종가 {close:,.0f}원, 전일 대비 {chg_txt}, "
                    f"닛케이 {n_txt}, 100엔당 {j_txt}, "
                    f"19,000까지 {close - r['price_low']:+,.0f}원.",
                    2,
                )
            )
            st["last_summary_date"] = today

    return out


def fail_alerts(cfg: dict, st: dict, errs: dict, today: str) -> list[tuple[str, str, int]]:
    out = []
    names = {"bars": "종가", "n225": "닛케이225", "jpy100": "엔원(100엔당)"}
    fails = st.setdefault("fail", {})
    for key in names:
        f = fails.get(key) or {"days": 0, "last": None, "notified": False}
        if key in errs:
            if f["last"] != today:
                f["days"] += 1
                f["last"] = today
            if f["days"] >= cfg["rules"]["fail_ops_after_days"] and not f["notified"]:
                f["notified"] = True
                out.append(
                    (
                        f"{TAG} {names[key]} 조회 실패 | {f['days']}일 연속, 그 규칙 건너뜀",
                        f"{names[key]} 조회가 {f['days']}일 연속 실패({errs[key]}). "
                        f"그 규칙은 회복될 때까지 판단 안 함. 조건 미충족이 아님.",
                        3,
                    )
                )
        else:
            f = {"days": 0, "last": None, "notified": False}
        fails[key] = f
    return out


def gather(today: date) -> tuple[dict, dict]:
    obs: dict = {}
    errs: dict = {}
    for key, fn in (
        ("bars", lambda: price_bars(today)),
        ("n225", n225_series),
        ("jpy100", jpy100_series),
    ):
        try:
            obs[key] = fn()
        except Exception as exc:  # noqa: BLE001
            obs[key] = None
            errs[key] = str(exc)[:200]
    return obs, errs


def send_actions(alerts: list[tuple[str, str, int]], stamp: str) -> list[tuple[str, str, int]]:
    built = []
    for title, text, pri in alerts:
        if "조회 실패" in title:
            msg = body(
                stamp,
                text,
                "다음 거래일 19:00 재시도를 기다리세요.",
                "읽힌 항목은 정상 판단했습니다.",
                "조회 실패를 조건 충족으로 읽기.",
            )
        elif "일일 요약" in title:
            msg = body(
                stamp,
                text,
                "참고만. 오늘 새로 켠 이벤트 조건이 없으면 추가 조치는 없습니다.",
                "19,000 이하, 닛케이 55,000 하회, 100엔당 +5%(20거래일) 시에만 별도 알림.",
                "일일 요약만 보고 바로 매수/매도.",
            )
        elif "N225" in title or "닛케이" in title:
            msg = body(
                stamp,
                text,
                "닛케이 안정(55k 지지)이 깨졌는지 확인. 포지션 크기를 제한하세요.",
                "56,000 위로 올라가면 다시 무장됩니다.",
            )
        elif "JPYKRW" in title or "100엔당" in title:
            msg = body(
                stamp,
                text,
                "환노출 비중과 헤지 여부를 다시 보세요. 한 번에 크게 넣지 마세요.",
                "+3% 미만으로 돌아오면 다시 무장됩니다.",
            )
        elif "19,000" in title or "진입" in title:
            msg = body(
                stamp,
                text,
                "분할 매수만 검토. 유동성 낮아 슬리피지 주의. 닛케이/원자재/환율도 같이 보세요.",
                "종가가 19,400원 위로 올라온 뒤 다시 19,000원 이하로 오면 또 알림.",
            )
        else:
            msg = body(stamp, text, "상태를 확인하세요.")
        built.append((title, msg, pri))
    return built


# ---------------- 실행 ----------------
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
        obs, errs = gather(now.date())
        b = (obs.get("bars") or [None])[-1]
        n = (obs.get("n225") or [None])[-1]
        j = (obs.get("jpy100") or [None])[-1]
        status = f"종가 {b[1]:,.0f}원({b[0]})" if b else "종가 확인 불가"
        status += f", 닛케이 {n[1]:,.0f}({n[0]})" if n else ", 닛케이 확인 불가"
        status += f", 100엔당 {j[1]:,.1f}원({j[0]})" if j else ", 100엔당 확인 불가"
        if errs:
            status += f". 조회 실패: {json.dumps(errs, ensure_ascii=False)}"
        title = f"{TAG} 설치 확인 | 매매 신호 아님"
        msg = body(
            stamp,
            status + ". 지금 켜진 이벤트 조건 없음(설치 확인).",
            "없음. KRX 거래일 19:00에 조건이 새로 켜질 때만 옵니다.",
            "조건: 종가 19,000 이하, 닛케이 55,000 하회, 100엔당 +5%(20거래일), 일일 요약.",
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
    obs, errs = gather(now.date())
    if errs:
        print("errors: " + json.dumps(errs, ensure_ascii=False), file=sys.stderr)
    print(
        "obs: "
        + json.dumps(
            {
                "bars": (obs.get("bars") or [])[-3:],
                "n225": (obs.get("n225") or [])[-3:],
                "jpy100": (obs.get("jpy100") or [])[-3:],
            },
            ensure_ascii=False,
        )
    )

    raw = evaluate(cfg, st, obs, today) + fail_alerts(cfg, st, errs, today)
    alerts = send_actions(raw, stamp)
    for title, msg, pri in alerts:
        blob = title + msg
        for ch in BANNED:
            if ch in blob:
                raise RuntimeError(f"banned char {ch!r} in alert")
        print(f"TITLE: {title}\nPRIORITY: {pri} TOPIC: {route_topic(topic, title)}\n{msg}\n")
        if not dry:
            publish(topic, title, msg, pri, ["office"], server)
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

    def init():
        s = default_state()
        s.update(last_summary_date="2026-11-05", price_last_date="2026-11-05")
        return s

    # 21 bars so lookback 20 works: index -(20+1) = -21
    def mk_jpy(last: float, base: float, n: int = 25) -> list[tuple[str, float]]:
        # oldest ... base at -21, ... last at -1
        out = []
        for i in range(n):
            day = f"2026-10-{i + 1:02d}" if i < 20 else f"2026-11-{i - 19:02d}"
            if i == n - 21:
                v = base
            elif i == n - 1:
                v = last
            else:
                v = base
            out.append((day, v))
        return out

    bars = [("2026-11-05", 22000.0), ("2026-11-06", 20840.0)]
    n225_ok = [("2026-11-05", 61078.0), ("2026-11-06", 57526.0)]
    jpy_flat = mk_jpy(860.0, 850.0)
    base = {"bars": bars, "n225": n225_ok, "jpy100": jpy_flat}

    st = init()
    check(
        "mid silent when summary already sent for old day",
        evaluate(cfg, st, dict(base, bars=[("2026-11-05", 22000.0)]), "2026-11-06"),
        [],
    )

    # entry zone
    st = init()
    low = [("2026-11-05", 19500.0), ("2026-11-06", 18950.0)]
    a = evaluate(cfg, st, dict(base, bars=low, n225=None, jpy100=None), "2026-11-06")
    titles = [x[0] for x in a]
    check(
        "entry + daily",
        titles,
        [
            f"{TAG} 종가 18,950원 | 19,000원 이하 진입 구간",
            f"{TAG} 일일 요약 | 종가 18,950원",
        ],
    )
    check("price disarmed", st["price_low_armed"], False)
    st["last_summary_date"] = "2026-11-06"
    evaluate(cfg, st, dict(base, bars=[("2026-11-07", 19300.0)], n225=None, jpy100=None), "2026-11-07")
    check("19300 no rearm", st["price_low_armed"], False)
    evaluate(cfg, st, dict(base, bars=[("2026-11-08", 19450.0)], n225=None, jpy100=None), "2026-11-08")
    check("19450 rearms", st["price_low_armed"], True)

    # nikkei
    st = init()
    a = evaluate(
        cfg,
        st,
        dict(base, bars=[("2026-11-05", 22000.0)], n225=[("2026-11-06", 54900.0)], jpy100=None),
        "2026-11-06",
    )
    check(
        "n225 fires with ticker",
        [x[0] for x in a],
        [f"{TAG} 570100/N225 54,900 | 닛케이 55,000 하회"],
    )
    check("n225 disarmed", st["n225_low_armed"], False)
    evaluate(cfg, st, dict(base, bars=[("2026-11-05", 22000.0)], n225=[("2026-11-07", 55500.0)], jpy100=None), "2026-11-07")
    check("55500 no n225 rearm", st["n225_low_armed"], False)
    evaluate(cfg, st, dict(base, bars=[("2026-11-05", 22000.0)], n225=[("2026-11-08", 56050.0)], jpy100=None), "2026-11-08")
    check("56050 rearms n225", st["n225_low_armed"], True)
    a2 = evaluate(
        cfg,
        st,
        dict(base, bars=[("2026-11-05", 22000.0)], n225=[("2026-11-10", 54800.0)], jpy100=None),
        "2026-11-10",
    )
    check("fires again after n225 rearm", a2[0][0].startswith(f"{TAG} 570100/N225"), True)

    # jpy +5%
    st = init()
    j_up = mk_jpy(900.0, 850.0)  # +5.88%
    a = evaluate(cfg, st, dict(base, bars=[("2026-11-05", 22000.0)], n225=None, jpy100=j_up), "2026-11-06")
    check("jpy up fires", any("JPYKRW" in t and "+5." in t for t in [x[0] for x in a]), True)
    check("jpy disarmed", st["jpy_up_armed"], False)
    # still +4% -> no rearm
    j_mid = mk_jpy(884.0, 850.0)  # +4%
    evaluate(cfg, st, dict(base, bars=[("2026-11-05", 22000.0)], n225=None, jpy100=j_mid), "2026-11-07")
    check("4pct no jpy rearm", st["jpy_up_armed"], False)
    j_ok = mk_jpy(870.0, 850.0)  # +2.35%
    evaluate(cfg, st, dict(base, bars=[("2026-11-05", 22000.0)], n225=None, jpy100=j_ok), "2026-11-08")
    check("below 3pct rearms jpy", st["jpy_up_armed"], True)

    # daily once
    st = init()
    a = evaluate(cfg, st, dict(base, n225=n225_ok, jpy100=jpy_flat), "2026-11-06")
    check("daily once", [x[0] for x in a], [f"{TAG} 일일 요약 | 종가 20,840원"])
    check("n225 in body", "닛케이 57,526" in a[0][1], True)
    check("jpy in body", "100엔당 860.0원" in a[0][1], True)
    check("summary not repeated", evaluate(cfg, st, dict(base), "2026-11-06"), [])

    # fail ops
    st = init()
    f1 = fail_alerts(cfg, st, {"n225": "x"}, "2026-11-06")
    f2 = fail_alerts(cfg, st, {"n225": "x"}, "2026-11-09")
    check("ops silent day1", f1, [])
    check(
        "ops on 2nd failing day",
        [x[0] for x in f2],
        [f"{TAG} 닛케이225 조회 실패 | 2일 연속, 그 규칙 건너뜀"],
    )

    # formatting
    msgs = []
    for kw in (
        {"bars": low, "n225": [("2026-11-06", 54000.0)], "jpy100": j_up},
        {"bars": bars, "n225": None, "jpy100": None},
    ):
        for ti, tx, _ in evaluate(cfg, init(), dict(base, **kw), "2026-11-06"):
            msgs.append(ti + body("s", tx, "a"))
    text = " ".join(msgs) + json.dumps(cfg, ensure_ascii=False)
    check(
        "titles start [570100], N225/JPYKRW tickers, no banned chars",
        (
            all(m.startswith(TAG) for m in msgs),
            any("570100/N225" in m for m in msgs),
            any("570100 / JPYKRW" in m for m in msgs),
            "\u00b7" in text,
            "\u2014" in text,
        ),
        (True, True, True, False, False),
    )
    check("ends 2027-11-06 23:59", cfg["ends_kst"], "2027-11-06 23:59")
    check("code 570100", cfg["code"], "570100")
    check("routing failure -> ops", route_topic("cwally-port", f2[0][0]), "cwally-ops")
    print(f"SELFTEST {'PASS' if not fails else 'FAIL'}: {len(fails)} failed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
