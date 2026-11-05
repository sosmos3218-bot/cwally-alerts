#!/usr/bin/env python3
"""[458750] TIGER 미국배당다우존스타겟커버드콜1호 분배금/환율/밴드/일일요약 (2026-11-05 요청 pt7u).

KRX 거래일 19:00 k203780 슬롯(장 마감 후). 판정:
  1) 월 분배금 금액이 state 직전 기록과 달라지면 1번
  2) 원달러(USDKRW) 1,250원 이하 교차 하향 1번 -> 1,265원 위 재무장. 제목 '458750 / USDKRW'
  3) 종가 14,020원 초과 또는 10,735원 미만 교차 1번, 1% 버퍼 재무장
  4) 거래일 일일 요약 1번: 종가, 전일 대비 %, NAV/괴리(가능하면), 원달러
소스가 실패하면 그 규칙만 이번 실행에서 건너뜀. 같은 소스가 2일 연속 실패하면 cwally-ops 1번.
modes: auto | dry(발송 없음, state 안 씀) | install(설치 확인 1회, state 안 씀) | selftest
"""
from __future__ import annotations

import json
import re
import sys
import urllib.parse
import urllib.request
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic, route_topic  # noqa: E402
from kr_calendar import KST, is_trading_day, session_day  # noqa: E402

NAME = "k458750"
CFG_PATH = ROOT / "configs" / f"{NAME}.json"
STATE_PATH = ROOT / "state" / f"{NAME}.json"
FX = Path(__file__).resolve().parent / "fixtures" / NAME
UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}
TAG = "[458750]"
CODE = "458750"
ISIN = "KR7458750007"
NAME_KR = "TIGER 미국배당다우존스타겟커버드콜1호"
BANNED = ("\u00b7", "\u2014")


def default_state() -> dict:
    return {
        "updated_at": None,
        "fx_low_armed": True,
        "fx_last": None,
        "price_high_armed": True,
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


def http(url: str, timeout: int = 25) -> str:
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def http_json(url: str, timeout: int = 25) -> dict:
    return json.loads(http(url, timeout))


# ---------------- 데이터 ----------------
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
    import usdiv_monitor as ud

    form = ud.funetf_form(ISIN)
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


def yahoo_usdkrw() -> float:
    errs = []
    for host in ("query1", "query2"):
        url = f"https://{host}.finance.yahoo.com/v8/finance/chart/" + urllib.parse.quote("USDKRW=X") + "?interval=1d&range=5d"
        try:
            meta = http_json(url)["chart"]["result"][0].get("meta") or {}
            price = meta.get("regularMarketPrice") or meta.get("previousClose")
            if price is None:
                errs.append(f"{host}: empty")
                continue
            return float(price)
        except Exception as exc:  # noqa: BLE001
            errs.append(f"{host}: {exc}")
    raise RuntimeError("; ".join(errs))


def _num(v) -> float | None:
    if v is None:
        return None
    try:
        return float(str(v).replace(",", "").replace("%", "").strip())
    except (TypeError, ValueError):
        return None


def fetch_nav(close_day: str, close: float) -> dict | None:
    """Return {nav, premium_pct, source, note} or None."""
    # 1) Mirae official table for that day
    try:
        from k203780_monitor import mirae_nav

        rows = mirae_nav(ISIN, date.fromisoformat(close_day))
        hit = rows.get(close_day)
        if hit and hit.get("nav"):
            nav = float(hit["nav"])
            prem = (close / nav - 1.0) * 100.0
            return {
                "nav": nav,
                "premium_pct": prem,
                "source": "미래에셋 기준가격",
                "note": f"기준일 {close_day}",
            }
    except Exception as exc:  # noqa: BLE001
        print(f"mirae_nav fail: {exc}", file=sys.stderr)

    # 2) Naver etfKeyIndicator (date often unclear for overseas ETFs)
    try:
        k = (http_json(f"https://m.stock.naver.com/api/stock/{CODE}/integration").get("etfKeyIndicator") or {})
        nav = _num(k.get("nav"))
        dev = _num(k.get("deviationRate"))
        if nav and nav > 0:
            prem = dev if dev is not None else (close / nav - 1.0) * 100.0
            return {
                "nav": nav,
                "premium_pct": prem,
                "source": "네이버 etfKeyIndicator",
                "note": "날짜 미확인(해외 기초자산 ETF는 전일 NAV일 수 있음)",
            }
    except Exception as exc:  # noqa: BLE001
        print(f"naver nav fail: {exc}", file=sys.stderr)
    return None


# ---------------- 본문 ----------------
def body(stamp: str, text: str, action: str, later: str = "", avoid: str = "") -> str:
    lines = [
        f"{stamp} | {CODE} | {NAME_KR}",
        f"이게 뭐 알림: {CODE}({NAME_KR}) 월 분배금 변경, 원달러 1,250원 이하, 종가 14,020/10,735 밴드, 장 마감 후 일일 요약. 매매 지시 아님.",
        f"지금 결과/상태: {text}",
        f"지금 할 일: {action}",
        f"나중에: {later or '같은 조건은 재무장 선을 넘었다가 다시 와야 알림이 옵니다.'}",
        "익절/줄일 때: 계획한 비중이 있으면 그 안에서만. 없으면 새로 비중을 정한 뒤.",
        f"하지 말 것: {avoid or '조회 실패를 신호로 읽기, 하루 환율 움직임으로 크게 들어가기, 분배금 한 달 값만 보고 판단.'}",
    ]
    return "\n".join(lines)


# ---------------- 판정 ----------------
def evaluate(cfg: dict, st: dict, obs: dict, today: str) -> list[tuple[str, str, int]]:
    r = cfg["rules"]
    out: list[tuple[str, str, int]] = []
    fx = obs.get("fx")
    bars = obs.get("bars")
    rows = obs.get("dists")
    nav = obs.get("nav")

    if fx is not None:
        if fx <= r["fx_low"] and st.get("fx_low_armed", True):
            st["fx_low_armed"] = False
            out.append(
                (
                    f"{TAG} 458750 / USDKRW {fx:,.2f}원 | 원달러 1,250원 이하",
                    f"원달러 {fx:,.2f}원, {r['fx_low']:,.0f}원 이하로 내려옴(환노출 ETF 환율 리스크). "
                    f"{r['fx_low_rearm']:,.0f}원 위로 올라가야 다시 무장.",
                    3,
                )
            )
        elif fx > r["fx_low_rearm"]:
            st["fx_low_armed"] = True
        st["fx_last"] = fx

    if bars:
        day, close = bars[-1]
        prev = bars[-2][1] if len(bars) >= 2 else None
        if close > r["price_high"] and st.get("price_high_armed", True):
            st["price_high_armed"] = False
            out.append(
                (
                    f"{TAG} 종가 {close:,.0f}원 | 14,020원 돌파(52주 고)",
                    f"{day} 종가 {close:,.0f}원, 52주 고점선 {r['price_high']:,}원 위. "
                    f"재무장선 {r['price_high_rearm']:,}원(x0.99) 아래로 내려와야 다시 무장.",
                    4,
                )
            )
        elif close < r["price_high_rearm"]:
            st["price_high_armed"] = True

        if close < r["price_low"] and st.get("price_low_armed", True):
            st["price_low_armed"] = False
            out.append(
                (
                    f"{TAG} 종가 {close:,.0f}원 | 10,735원 하회(52주 저)",
                    f"{day} 종가 {close:,.0f}원, 52주 저점선 {r['price_low']:,}원 아래. "
                    f"재무장선 {r['price_low_rearm']:,}원(x1.01) 위로 올라가야 다시 무장.",
                    4,
                )
            )
        elif close > r["price_low_rearm"]:
            st["price_low_armed"] = True
        st["price_last_date"] = day

        if day == today and st.get("last_summary_date") != today:
            chg = ((close / prev - 1) * 100) if prev else None
            chg_txt = f"{chg:+.2f}%" if chg is not None else "비교 불가"
            fx_txt = f"{fx:,.2f}원" if fx is not None else "확인 불가"
            if nav:
                nav_txt = (
                    f"NAV {nav['nav']:,.2f}원, 괴리 {nav['premium_pct']:+.2f}% "
                    f"({nav['source']}, {nav['note']})"
                )
            else:
                nav_txt = "NAV/괴리 확인 불가(미래에셋/네이버 모두 실패 또는 미갱신, 요약에서 생략)"
            out.append(
                (
                    f"{TAG} 일일 요약 | 종가 {close:,.0f}원",
                    f"{day} 종가 {close:,.0f}원, 전일 대비 {chg_txt}, {nav_txt}, 원달러 {fx_txt}. "
                    f"14,020까지 {close - r['price_high']:+,.0f}원, 10,735까지 {close - r['price_low']:+,.0f}원.",
                    2,
                )
            )
            st["last_summary_date"] = today

    if rows:
        nd, nv = rows[-1]
        last_d = st.get("last_dist_date")
        last_a = st.get("last_dist_amt")
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
            st["last_dist_date"], st["last_dist_amt"] = nd, nv
            print(f"dist same amount {nv:.0f} on {nd}; date advanced, no alert")

    return out


def fail_alerts(cfg: dict, st: dict, errs: dict, today: str) -> list[tuple[str, str, int]]:
    out = []
    names = {"fx": "원달러", "dists": "분배금 이력", "bars": "종가", "nav": "NAV"}
    fails = st.setdefault("fail", {})
    # NAV failure alone should not ops-spam (optional for summary); still track but only ops for critical
    critical = ("fx", "dists", "bars")
    for key in names:
        f = fails.get(key) or {"days": 0, "last": None, "notified": False}
        if key in errs:
            if f["last"] != today:
                f["days"] += 1
                f["last"] = today
            if key in critical and f["days"] >= cfg["rules"]["fail_ops_after_days"] and not f["notified"]:
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
    for key, fn in (("fx", yahoo_usdkrw), ("dists", dists), ("bars", lambda: price_bars(today))):
        try:
            obs[key] = fn()
        except Exception as exc:  # noqa: BLE001
            obs[key] = None
            errs[key] = str(exc)[:200]
    # NAV optional: only when we have a close bar
    if obs.get("bars"):
        day, close = obs["bars"][-1]
        try:
            nav = fetch_nav(day, close)
            if nav is None:
                errs["nav"] = "NAV 소스 없음"
                obs["nav"] = None
            else:
                obs["nav"] = nav
        except Exception as exc:  # noqa: BLE001
            obs["nav"] = None
            errs["nav"] = str(exc)[:200]
    else:
        obs["nav"] = None
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
                "분배금 변경, 원달러 1,250 이하, 종가 14,020 돌파/10,735 하회 시에만 별도 알림.",
                "일일 요약만 보고 바로 매수/매도.",
            )
        elif "USDKRW" in title:
            msg = body(
                stamp,
                text,
                "환노출 비중과 헤지 여부를 다시 보세요. 한 번에 크게 넣지 마세요.",
                "1,265원 위로 올라가면 다시 무장됩니다.",
            )
        elif "14,020" in title or "돌파" in title:
            msg = body(
                stamp,
                text,
                "돌파 지속 여부와 거래량을 확인. 추격 매수는 계획 비중 안에서만.",
                "종가가 13,880원 아래로 내려온 뒤 다시 14,020원 위로 오면 또 알림.",
            )
        elif "10,735" in title or "하회" in title:
            msg = body(
                stamp,
                text,
                "보유 중이면 비중과 손절 기준을 오늘 다시 정하기. 신규는 바닥 확인부터.",
                "종가가 10,842원 위로 올라온 뒤 다시 10,735원 아래로 오면 또 알림.",
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
        b = (obs.get("bars") or [None])[-1]
        fx = obs.get("fx")
        d = (obs.get("dists") or [None])[-1]
        status = (f"종가 {b[1]:,.0f}원({b[0]})" if b else "종가 확인 불가")
        status += f", 원달러 {fx:,.2f}원" if fx is not None else ", 원달러 확인 불가"
        if d:
            status += f", 최근 분배금 {d[1]:.0f}원({d[0]})"
        if obs.get("nav"):
            n = obs["nav"]
            status += f", NAV {n['nav']:,.2f}원 괴리 {n['premium_pct']:+.2f}%"
        if errs:
            status += f". 조회 실패: {json.dumps(errs, ensure_ascii=False)}"
        title = f"{TAG} 설치 확인 | 매매 신호 아님"
        msg = body(
            stamp,
            status + ". 지금 켜진 이벤트 조건 없음(설치 확인).",
            "없음. KRX 거래일 19:00에 조건이 새로 켜질 때만 옵니다.",
            "조건: 월 분배금 변경, 원달러 1,250 이하, 종가 14,020 돌파/10,735 하회, 일일 요약.",
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
                "fx": obs.get("fx"),
                "dist_last": (obs.get("dists") or [])[-2:],
                "bars": (obs.get("bars") or [])[-3:],
                "nav": obs.get("nav"),
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

    lab = parse_dists((FX / "etfro.html").read_text(encoding="utf-8"))
    check("etfro fixture last 64", lab[-1], ("2026-10-14", 64.0))
    check("fixture has Oct/Sep/Aug", [v for d, v in lab if d >= "2026-08-01"], [60.0, 65.0, 64.0])

    def init():
        s = default_state()
        s.update(
            last_dist_date="2026-10-14",
            last_dist_amt=64.0,
            last_summary_date="2026-11-04",
            price_last_date="2026-11-04",
            fx_last=1340.0,
        )
        return s

    bars = [("2026-11-04", 13030.0), ("2026-11-05", 13315.0)]
    base = {"fx": 1340.78, "dists": lab, "bars": bars, "nav": None}

    st = init()
    check(
        "current mid-band silent for old summary day",
        evaluate(cfg, st, dict(base, bars=[("2026-11-04", 13030.0)]), "2026-11-05"),
        [],
    )

    st = init()
    a = evaluate(cfg, st, dict(base, fx=1249.5, bars=[("2026-11-04", 13030.0)]), "2026-11-05")
    check(
        "fx low fires with ticker title",
        [x[0] for x in a],
        [f"{TAG} 458750 / USDKRW 1,249.50원 | 원달러 1,250원 이하"],
    )
    check("fx disarmed", st["fx_low_armed"], False)
    check(
        "1260 no rearm",
        (evaluate(cfg, st, dict(base, fx=1260.0, bars=[("2026-11-04", 13030.0)]), "2026-11-06"), st["fx_low_armed"]),
        ([], False),
    )
    evaluate(cfg, st, dict(base, fx=1266.0, bars=[("2026-11-04", 13030.0)]), "2026-11-07")
    check("1266 rearms", st["fx_low_armed"], True)
    a2 = evaluate(cfg, st, dict(base, fx=1248.0, bars=[("2026-11-04", 13030.0)]), "2026-11-10")
    check("fires again after rearm", [x[0] for x in a2][0].startswith(f"{TAG} 458750 / USDKRW"), True)

    st = init()
    hi = [("2026-11-04", 13900.0), ("2026-11-05", 14050.0)]
    a = evaluate(cfg, st, dict(base, bars=hi, fx=None), "2026-11-05")
    titles = [x[0] for x in a]
    check("high + daily", titles, [
        f"{TAG} 종가 14,050원 | 14,020원 돌파(52주 고)",
        f"{TAG} 일일 요약 | 종가 14,050원",
    ])
    check("high disarmed", st["price_high_armed"], False)
    st["last_summary_date"] = "2026-11-05"
    evaluate(cfg, st, dict(base, bars=[("2026-11-06", 13900.0)], fx=None), "2026-11-06")
    check("13900 no high rearm", st["price_high_armed"], False)
    evaluate(cfg, st, dict(base, bars=[("2026-11-07", 13850.0)], fx=None), "2026-11-07")
    check("13850 rearms high", st["price_high_armed"], True)

    st = init()
    low = [("2026-11-04", 11000.0), ("2026-11-05", 10700.0)]
    a = evaluate(cfg, st, dict(base, bars=low, fx=None), "2026-11-05")
    check("low fires", any("10,735원 하회" in t for t in [x[0] for x in a]), True)
    check("low disarmed", st["price_low_armed"], False)
    st["last_summary_date"] = "2026-11-05"
    evaluate(cfg, st, dict(base, bars=[("2026-11-06", 10800.0)], fx=None), "2026-11-06")
    check("10800 no low rearm", st["price_low_armed"], False)
    evaluate(cfg, st, dict(base, bars=[("2026-11-07", 10850.0)], fx=None), "2026-11-07")
    check("10850 rearms low", st["price_low_armed"], True)

    st = init()
    nov = lab + [("2026-11-14", 60.0)]
    a = evaluate(cfg, st, dict(base, dists=nov, bars=[("2026-11-04", 13030.0)], fx=None), "2026-11-05")
    check("dist change 64->60", [x[0] for x in a], [f"{TAG} 월 분배금 60원 | 직전 64원에서 변경"])
    check("same dist not repeated", evaluate(cfg, st, dict(base, dists=nov, bars=[("2026-11-04", 13030.0)], fx=None), "2026-11-05"), [])

    st = init()
    same = lab + [("2026-11-14", 64.0)]
    a = evaluate(cfg, st, dict(base, dists=same, bars=[("2026-11-04", 13030.0)], fx=None), "2026-11-05")
    check("same amount new date silent", (a, st["last_dist_date"]), ([], "2026-11-14"))

    st = init()
    nav = {"nav": 13280.0, "premium_pct": 0.26, "source": "테스트", "note": "기준일 2026-11-05"}
    a = evaluate(cfg, st, dict(base, nav=nav), "2026-11-05")
    check("daily once", [x[0] for x in a], [f"{TAG} 일일 요약 | 종가 13,315원"])
    check("nav in body", "NAV 13,280.00원" in a[0][1], True)
    check("summary not repeated", evaluate(cfg, st, dict(base, nav=nav), "2026-11-05"), [])

    st = init()
    f1 = fail_alerts(cfg, st, {"dists": "x"}, "2026-11-06")
    f2 = fail_alerts(cfg, st, {"dists": "x"}, "2026-11-09")
    check("ops on 2nd failing day", [x[0] for x in f2], [f"{TAG} 분배금 이력 조회 실패 | 2일 연속, 그 규칙 건너뜀"])
    check("ops silent day1", f1, [])
    # nav fail alone no ops
    st = init()
    n1 = fail_alerts(cfg, st, {"nav": "x"}, "2026-11-06")
    n2 = fail_alerts(cfg, st, {"nav": "x"}, "2026-11-09")
    check("nav fail no ops", n1 + n2, [])

    msgs = []
    for kw in (
        {"fx": 1240.0, "bars": hi, "dists": nov, "nav": None},
        {"fx": None, "bars": low, "dists": None, "nav": None},
    ):
        for ti, tx, _ in evaluate(cfg, init(), dict(base, **kw), "2026-11-05"):
            msgs.append(ti + body("s", tx, "a"))
    text = " ".join(msgs) + json.dumps(cfg, ensure_ascii=False)
    check(
        "titles start [458750], USDKRW ticker, no banned chars",
        (
            all(m.startswith(TAG) for m in msgs),
            any("458750 / USDKRW" in m for m in msgs),
            "\u00b7" in text,
            "\u2014" in text,
        ),
        (True, True, False, False),
    )
    check("ends 2027-11-05 23:59", cfg["ends_kst"], "2027-11-05 23:59")
    check("isin", cfg["isin"], "KR7458750007")
    check("routing failure -> ops", route_topic("cwally-port", f2[0][0]), "cwally-ops")
    print(f"SELFTEST {'PASS' if not fails else 'FAIL'}: {len(fails)} failed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
