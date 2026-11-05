#!/usr/bin/env python3
"""[475380] RISE 글로벌리얼티인컴 금리/저점/분배금/일일요약 알림 (2026-11-05 요청 pt1u).

KRX 거래일 19:00 k203780 슬롯(장 마감 후). 판정:
  1) 미 10년물(US10Y) 4.50% 이하 교차 하향 1번 -> 4.60% 위 재무장. 제목 '475380 / US10Y'
  2) 475380 확정 종가 9,320원 미만 1번 -> 9,413원(1% 버퍼) 위 재무장
  3) 월 분배금 금액이 state 직전 기록과 달라지면 1번
  4) 거래일 일일 요약 1번: 종가, 전일 대비 %, 9,320 대비 거리, 최신 US10Y
소스가 실패하면 그 규칙만 이번 실행에서 건너뜀. 같은 소스가 2일 연속 실패하면 cwally-ops 1번.
modes: auto | dry(발송 없음, state 안 씀) | install(설치 확인 1회, state 안 씀) | selftest
"""
from __future__ import annotations

import json
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic, route_topic  # noqa: E402
from kr_calendar import KST, is_trading_day, session_day  # noqa: E402

NAME = "k475380"
CFG_PATH = ROOT / "configs" / f"{NAME}.json"
STATE_PATH = ROOT / "state" / f"{NAME}.json"
FX = Path(__file__).resolve().parent / "fixtures" / NAME
UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}
TAG = "[475380]"
CODE = "475380"
NAME_KR = "RISE 글로벌리얼티인컴"


def default_state() -> dict:
    return {
        "updated_at": None,
        "rate_low_armed": True,
        "y10_last_date": None,
        "price_low_armed": True,
        "price_last_date": None,
        "last_dist_date": None,
        "last_dist_amt": None,
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


# ---------------- 데이터 ----------------
def http(url: str, timeout: int = 25) -> str:
    import urllib.request

    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def fred(series: str, start: date) -> list[tuple[str, float]]:
    txt = http(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}&cosd={start.isoformat()}")
    rows = [ln.split(",") for ln in txt.strip().splitlines()]
    if not rows or series not in rows[0][-1]:
        raise RuntimeError("FRED csv 형식 다름")
    out = []
    for r in rows[1:]:
        try:
            out.append((r[0], float(r[1])))
        except (ValueError, IndexError):
            continue
    if not out:
        raise RuntimeError(f"FRED {series} 값 없음")
    return out


def y10_series(today: date) -> list[tuple[str, float]]:
    try:
        return fred("DGS10", today - timedelta(days=21))
    except Exception as exc:  # noqa: BLE001
        print(f"FRED DGS10 fail, Treasury fallback: {exc}", file=sys.stderr)
        from usbond_monitor import treasury_10y

        return sorted(treasury_10y(today - timedelta(days=21), today).items())


def parse_dists(html: str) -> list[tuple[str, float]]:
    """[(기준일 YYYY-MM-DD, 원)] 오래된 순. etf-lab(텍스트 행) 또는 etfro(data-sort 표)."""
    rows = set()
    for a, _b, amt in re.findall(
        r"(20\d\d[.-]\d\d[.-]\d\d)\D{0,200}?(20\d\d[.-]\d\d[.-]\d\d)\D{0,200}?([\d,]+)\s*원", html
    ):
        rows.add((a.replace(".", "-"), float(amt.replace(",", ""))))
    for a, _b, amt in re.findall(
        r'data-sort="(\d{8})">[^<]*</td>\s*<td data-sort="(\d{8})">[^<]*</td>\s*<td class="num" data-sort="([\d.]+)"',
        html,
    ):
        rows.add((f"{a[:4]}-{a[4:6]}-{a[6:]}", float(amt)))
    by: dict[str, float] = {}
    for d, v in rows:
        by[d] = v
    return sorted(by.items())


def funetf_dists() -> list[tuple[str, float]]:
    """금융투자협회 funetf etfdividend (ISIN KR7475380002). 날짜는 분배락일."""
    import usdiv_monitor as ud

    form = ud.funetf_form("KR7475380002")
    raw = ud.funetf_divs(form)
    if len(raw) < 6:
        raise RuntimeError(f"funetf 행 {len(raw)}개")
    return sorted(raw.items())


def dists() -> list[tuple[str, float]]:
    errs = []
    try:
        return funetf_dists()
    except Exception as exc:  # noqa: BLE001
        errs.append(f"funetf {exc}")
    urls = (f"https://etfro.com/etf/{CODE}", f"https://etf-lab.jp/{CODE}")
    for attempt in range(2):
        for url in urls:
            try:
                r = parse_dists(http(url, timeout=35))
                if len(r) >= 6:
                    return r
                errs.append(f"{url} 행 {len(r)}개")
            except Exception as exc:  # noqa: BLE001
                errs.append(f"{url} try{attempt + 1} {exc}")
        if attempt < 1:
            import time
            time.sleep(1.0)
    raise RuntimeError(" / ".join(errs[-8:]))


def price_bars(today: date) -> list[tuple[str, float]]:
    import krx_close

    bars = [b for b in krx_close.regular_bars(CODE, 30) if b.day <= today.isoformat()]
    if not bars:
        raise RuntimeError("종가 없음")
    return [(b.day, float(b.close)) for b in bars]


# ---------------- 본문 ----------------
def body(stamp: str, text: str, action: str, later: str = "", avoid: str = "") -> str:
    lines = [
        f"{stamp} | {CODE} | {NAME_KR}",
        f"이게 뭐 알림: {CODE}({NAME_KR}) 금리 4.50% 이하, 종가 9,320원 미만, 월 분배금 변경, 장 마감 후 일일 요약. 매매 지시 아님.",
        f"지금 결과/상태: {text}",
        f"지금 할 일: {action}",
        f"나중에: {later or '같은 조건은 재무장 선을 넘었다가 다시 와야 알림이 옵니다.'}",
        "익절/줄일 때: 계획한 비중이 있으면 그 안에서만. 없으면 새로 비중을 정한 뒤.",
        f"하지 말 것: {avoid or '조회 실패를 신호로 읽기, 하루 금리 움직임으로 크게 들어가기, 분배금 한 달 값만 보고 판단.'}",
    ]
    return "\n".join(lines)


# ---------------- 판정 ----------------
def evaluate(cfg: dict, st: dict, obs: dict, today: str) -> list[tuple[str, str, int]]:
    r = cfg["rules"]
    out: list[tuple[str, str, int]] = []
    y10 = obs.get("y10")
    bars = obs.get("bars")
    rows = obs.get("dists")

    if y10:
        cur_d, cur = y10[-1]
        if cur <= r["rate_low"] and st.get("rate_low_armed", True):
            st["rate_low_armed"] = False
            out.append(
                (
                    f"{TAG} 475380 / US10Y {cur:.2f}% | 미 10년물 4.50% 이하",
                    f"미 10년물 {cur:.2f}%({cur_d}), {r['rate_low']:.2f}% 이하로 내려옴. "
                    f"글로벌 리츠 할인율 부담이 줄어드는 쪽. {r['rate_low_rearm']:.2f}% 위로 올라가야 다시 무장.",
                    3,
                )
            )
        elif cur > r["rate_low_rearm"]:
            st["rate_low_armed"] = True
        st["y10_last_date"] = cur_d

    if bars:
        day, close = bars[-1]
        prev = bars[-2][1] if len(bars) >= 2 else None
        if close < r["price_low"] and st.get("price_low_armed", True):
            st["price_low_armed"] = False
            out.append(
                (
                    f"{TAG} 종가 {close:,.0f}원 | 9,320원 하회",
                    f"{day} 종가 {close:,.0f}원, 52주 저점 참고선 {r['price_low']:,}원 아래. "
                    f"{r['price_low_rearm']:,}원(참고선 x 1.01) 위로 올라가야 다시 무장.",
                    4,
                )
            )
        elif close > r["price_low_rearm"]:
            st["price_low_armed"] = True
        st["price_last_date"] = day

        # 일일 요약: 당일 확정 종가가 있고 아직 안 보낸 날
        if day == today and st.get("last_summary_date") != today:
            chg = ((close / prev - 1) * 100) if prev else None
            dist_pct = (close / r["price_low"] - 1) * 100
            chg_txt = f"{chg:+.2f}%" if chg is not None else "비교 불가"
            y_txt = f"{y10[-1][1]:.2f}%({y10[-1][0]})" if y10 else "확인 불가"
            out.append(
                (
                    f"{TAG} 일일 요약 | 종가 {close:,.0f}원",
                    f"{day} 종가 {close:,.0f}원, 전일 대비 {chg_txt}, "
                    f"9,320원 대비 {dist_pct:+.2f}%({close - r['price_low']:+,.0f}원), 미 10년물 {y_txt}.",
                    2,
                )
            )
            st["last_summary_date"] = today

    if rows:
        nd, nv = rows[-1]
        last_d = st.get("last_dist_date")
        last_a = st.get("last_dist_amt")
        # 새 기준일이고 금액이 직전 기록과 다르면 알림. 첫 시드(last_d None)는 금액만 저장.
        if last_d is None:
            st["last_dist_date"], st["last_dist_amt"] = nd, nv
        elif nd > last_d and (last_a is None or float(nv) != float(last_a)):
            out.append(
                (
                    f"{TAG} 월 분배금 {nv:.0f}원 | 직전 {last_a:.0f}원에서 변경",
                    f"기준일 {nd} 분배금 {nv:.0f}원. 직전 기록은 기준일 {last_d} {last_a:.0f}원. "
                    f"차이 {nv - float(last_a):+.0f}원.",
                    3,
                )
            )
            st["last_dist_date"], st["last_dist_amt"] = nd, nv
        elif nd > last_d:
            # 금액 동일: 조용히 기준일만 갱신
            st["last_dist_date"], st["last_dist_amt"] = nd, nv
            print(f"dist same amount {nv:.0f} on {nd}; date advanced, no alert")

    return out


def fail_alerts(cfg: dict, st: dict, errs: dict, today: str) -> list[tuple[str, str, int]]:
    out = []
    names = {"y10": "미 10년물", "dists": "분배금 이력", "bars": "종가"}
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
    for key, fn in (("y10", lambda: y10_series(today)), ("dists", dists), ("bars", lambda: price_bars(today))):
        try:
            obs[key] = fn()
        except Exception as exc:  # noqa: BLE001
            obs[key] = None
            errs[key] = str(exc)[:200]
    return obs, errs


def send_actions(alerts: list[tuple[str, str, int]], stamp: str) -> list[tuple[str, str, int]]:
    """Attach six-item body for event titles that only have status text."""
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
                "금리 4.50% 이하, 종가 9,320 미만, 분배금 변경 시에만 별도 알림.",
                "일일 요약만 보고 바로 매수/매도.",
            )
        elif "US10Y" in title:
            msg = body(
                stamp,
                text,
                "보유/편입 계획이 있으면 금리 구간을 다시 보세요. 한 번에 크게 넣지 마세요.",
                "4.60% 위로 올라가면 다시 무장됩니다.",
            )
        elif "9,320" in title or "하회" in title:
            msg = body(
                stamp,
                text,
                "보유 중이면 비중과 손절 기준을 오늘 다시 정하기. 신규는 바닥 확인부터.",
                "9,413원 위 종가가 나온 뒤 다시 9,320원 아래로 오면 다시 알림.",
            )
        elif "분배금" in title:
            msg = body(
                stamp,
                text,
                "분배금 흐름이 계획과 맞는지 확인. 한 달 값만으로 비중을 바꾸지 마세요.",
                "다음 달 금액이 또 바뀌면 다시 알림.",
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
        y = (obs.get("y10") or [None])[-1]
        b = (obs.get("bars") or [None])[-1]
        d = (obs.get("dists") or [None])[-1]
        status = (f"종가 {b[1]:,.0f}원({b[0]})" if b else "종가 확인 불가")
        status += f", 미 10년물 {y[1]:.2f}%({y[0]})" if y else ", 미 10년물 확인 불가"
        if d:
            status += f", 최근 분배금 {d[1]:.0f}원({d[0]})"
        if errs:
            status += f". 조회 실패: {json.dumps(errs, ensure_ascii=False)}"
        title = f"{TAG} 설치 확인 | 매매 신호 아님"
        msg = body(
            stamp,
            status + ". 지금 켜진 이벤트 조건 없음(설치 확인).",
            "없음. KRX 거래일 19:00에 조건이 새로 켜질 때만 옵니다.",
            "조건: US10Y 4.50% 이하, 종가 9,320 미만, 분배금 변경, 일일 요약.",
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
                "y10": (obs.get("y10") or [])[-3:],
                "dist_last": (obs.get("dists") or [])[-2:],
                "bars": (obs.get("bars") or [])[-3:],
            },
            ensure_ascii=False,
        )
    )

    raw = evaluate(cfg, st, obs, today) + fail_alerts(cfg, st, errs, today)
    alerts = send_actions(raw, stamp)
    for title, msg, pri in alerts:
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

    lab = parse_dists((FX / "etfro.html").read_text(encoding="utf-8"))
    check("etfro fixture rows / last 32", (len(lab), lab[-1]), (32, ("2026-10-30", 32.0)))
    check("Aug/Sep/Oct amounts", [v for d, v in lab if d >= "2026-08-01"], [32.0, 32.0, 32.0])

    def init():
        s = default_state()
        s.update(
            last_dist_date="2026-10-30",
            last_dist_amt=32.0,
            last_summary_date="2026-11-04",
            y10_last_date="2026-11-03",
            price_last_date="2026-11-04",
        )
        return s

    y = lambda *vals: [(f"2026-11-{i + 1:02d}", x) for i, x in enumerate(vals)]  # noqa: E731
    bars = [("2026-11-04", 9765.0), ("2026-11-05", 10045.0)]
    base = {"y10": y(4.90, 4.90), "dists": lab, "bars": bars}

    st = init()
    check("current values silent (no rate/price/dist/summary for old day)", evaluate(cfg, st, dict(base, bars=[("2026-11-04", 9765.0)]), "2026-11-05"), [])

    st = init()
    a = evaluate(cfg, st, dict(base, y10=y(4.90, 4.50), bars=[("2026-11-04", 9765.0)]), "2026-11-05")
    check(
        "4.50 rate low fires with both tickers",
        [x[0] for x in a],
        [f"{TAG} 475380 / US10Y 4.50% | 미 10년물 4.50% 이하"],
    )
    check("disarmed after fire", st["rate_low_armed"], False)
    check("4.55 no rearm", (evaluate(cfg, st, dict(base, y10=y(4.50, 4.55)), "2026-11-06"), st["rate_low_armed"]), ([], False))
    check(
        ">4.60 rearms then fires again",
        (
            evaluate(cfg, st, dict(base, y10=y(4.55, 4.61)), "2026-11-07"),
            st["rate_low_armed"],
            [x[0] for x in evaluate(cfg, st, dict(base, y10=y(4.61, 4.49)), "2026-11-10")],
        ),
        ([], True, [f"{TAG} 475380 / US10Y 4.49% | 미 10년물 4.50% 이하"]),
    )

    st = init()
    low_bars = [("2026-11-04", 9765.0), ("2026-11-05", 9300.0)]
    a = evaluate(cfg, st, dict(base, bars=low_bars, y10=None), "2026-11-05")
    titles = [x[0] for x in a]
    check("price low + daily summary both", titles, [
        f"{TAG} 종가 9,300원 | 9,320원 하회",
        f"{TAG} 일일 요약 | 종가 9,300원",
    ])
    check("price disarmed", st["price_low_armed"], False)
    # rearm needs >9413; 9400 silent stay disarmed; 9500 rearms
    st["last_summary_date"] = "2026-11-05"
    a9400 = evaluate(cfg, st, dict(base, bars=[("2026-11-06", 9400.0)], y10=None), "2026-11-06")
    check("9400 no rearm", ([x[0] for x in a9400], st["price_low_armed"]), ([f"{TAG} 일일 요약 | 종가 9,400원"], False))
    st["last_summary_date"] = "2026-11-06"
    evaluate(cfg, st, dict(base, bars=[("2026-11-07", 9500.0)], y10=None), "2026-11-07")
    check("after 9500 armed", st["price_low_armed"], True)

    st = init()
    nov = lab + [("2026-11-28", 30.0)]
    a = evaluate(cfg, st, dict(base, dists=nov, bars=[("2026-11-04", 9765.0)], y10=None), "2026-11-05")
    check("dist amount change 32->30", [x[0] for x in a], [f"{TAG} 월 분배금 30원 | 직전 32원에서 변경"])
    check("same dist not repeated", evaluate(cfg, st, dict(base, dists=nov, bars=[("2026-11-04", 9765.0)], y10=None), "2026-11-05"), [])

    st = init()
    same = lab + [("2026-11-28", 32.0)]
    a = evaluate(cfg, st, dict(base, dists=same, bars=[("2026-11-04", 9765.0)], y10=None), "2026-11-05")
    check("same amount new date: silent, date advanced", (a, st["last_dist_date"]), ([], "2026-11-28"))

    st = init()
    a = evaluate(cfg, st, dict(base, y10=None), "2026-11-05")
    check("daily summary once", [x[0] for x in a], [f"{TAG} 일일 요약 | 종가 10,045원"])
    check("summary not repeated same day", evaluate(cfg, st, dict(base, y10=None), "2026-11-05"), [])

    st = init()
    f1 = fail_alerts(cfg, st, {"dists": "x"}, "2026-11-06")
    f2 = fail_alerts(cfg, st, {"dists": "x"}, "2026-11-09")
    check("ops on 2nd failing day", [x[0] for x in f2], [f"{TAG} 분배금 이력 조회 실패 | 2일 연속, 그 규칙 건너뜀"])
    check("ops silent day1", f1, [])

    # formatting: no middle dot / em-dash; titles have ticker
    st = init()
    msgs = []
    for kw in (
        {"y10": y(4.9, 4.4), "bars": low_bars, "dists": nov},
        {"y10": None, "bars": bars, "dists": None},
    ):
        for ti, tx, _ in evaluate(cfg, init(), dict(base, **kw), "2026-11-05"):
            msgs.append(ti + body("s", tx, "a"))
    text = " ".join(msgs) + json.dumps(cfg, ensure_ascii=False)
    check(
        "titles start [475380], ticker in US10Y title, no middle dot or em-dash",
        (
            all(m.startswith(TAG) for m in msgs),
            any("475380 / US10Y" in m for m in msgs),
            "\u00b7" in text,
            "\u2014" in text,
        ),
        (True, True, False, False),
    )
    check("ends 2027-11-05 23:59", cfg["ends_kst"], "2027-11-05 23:59")
    check("routing failure -> ops", route_topic("cwally-port", f2[0][0]), "cwally-ops")
    print(f"SELFTEST {'PASS' if not fails else 'FAIL'}: {len(fails)} failed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
