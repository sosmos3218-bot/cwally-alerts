#!/usr/bin/env python3
"""[488500] TIGER 미국S&P500동일가중 RSP/SPY 브레드스/밴드/분배금/일일요약 (2026-11-06 요청 pt14u).

KRX 거래일 19:00 k203780 슬롯(장 마감 후). 판정:
  1) Yahoo RSP/SPY 완료 일봉 종가 비율이 약 20거래일 전 대비 -3% 초과 하락 1번
     -> -1.5% 위로 돌아오면 재무장. 제목 '488500/RSP/SPY'
  2) 같은 비율 +3% 초과 상승 1번 -> +1.5% 아래로 돌아오면 재무장
  3) 종가 11,500원 이하 교차 1번(>11,700 재무장), 14,000원 이상 교차 1번(<13,800 재무장)
  4) 분기 분배금 금액이 state 직전 기록과 달라지면 1번
  5) 거래일 일일 요약 1번: 종가, 전일 대비 %, RSP/SPY 비율과 20거래일 변화, 11,500/14,000까지 거리
소스가 실패하면 그 규칙만 이번 실행에서 건너뜀. 같은 소스가 2일 연속 실패하면 cwally-ops 1번.
modes: auto | dry(발송 없음, state 안 씀) | install(설치 확인 1회, state 안 씀) | selftest
"""
from __future__ import annotations

import json
import re
import sys
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic, route_topic  # noqa: E402
from kr_calendar import KST, is_trading_day, session_day  # noqa: E402

NAME = "k488500"
CFG_PATH = ROOT / "configs" / f"{NAME}.json"
STATE_PATH = ROOT / "state" / f"{NAME}.json"
FX = Path(__file__).resolve().parent / "fixtures" / NAME
UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}
TAG = "[488500]"
CODE = "488500"
ISIN = "KR7488500000"
NAME_KR = "TIGER 미국S&P500동일가중"
BANNED = ("\u00b7", "\u2014")


def default_state() -> dict:
    return {
        "updated_at": None,
        "breadth_down_armed": True,
        "breadth_up_armed": True,
        "breadth_last": None,
        "breadth_base": None,
        "breadth_chg_pct_last": None,
        "price_low_armed": True,
        "price_high_armed": True,
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
    if len(raw) < 4:
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
                if len(r) >= 4:
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


def yahoo_daily_closes(symbol: str, range_: str = "3mo") -> list[tuple[str, float]]:
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
                day = datetime.fromtimestamp(int(t), tz=timezone.utc).date().isoformat()
                out.append((day, float(c)))
            if len(out) < 5:
                errs.append(f"{host}: short {len(out)}")
                continue
            by: dict[str, float] = {}
            for d, v in out:
                by[d] = v
            return [(d, by[d]) for d in sorted(by)]
        except Exception as exc:  # noqa: BLE001
            errs.append(f"{host}: {exc}")
    raise RuntimeError("; ".join(errs) or f"{symbol} empty")


def rsp_spy_ratio_series() -> list[tuple[str, float]]:
    """[(YYYY-MM-DD, RSP/SPY close ratio)] oldest first, common US trading days only."""
    rsp = dict(yahoo_daily_closes("RSP", "3mo"))
    spy = dict(yahoo_daily_closes("SPY", "3mo"))
    common = sorted(set(rsp) & set(spy))
    out = [(d, rsp[d] / spy[d]) for d in common if spy[d]]
    if len(out) < 25:
        raise RuntimeError(f"RSP/SPY 공통 일봉 {len(out)}개")
    return out


# ---------------- 본문 ----------------
def body(stamp: str, text: str, action: str, later: str = "", avoid: str = "") -> str:
    lines = [
        f"{stamp} | {CODE} | {NAME_KR}",
        f"이게 뭐 알림: {CODE}({NAME_KR}) RSP/SPY 브레드스 ±3%(20거래일), "
        f"종가 11,500/14,000, 분기 분배금 변경, 장 마감 후 일일 요약. 매매 지시 아님.",
        f"지금 결과/상태: {text}",
        f"지금 할 일: {action}",
        f"나중에: {later or '같은 조건은 재무장 선을 넘었다가 다시 와야 알림이 옵니다.'}",
        "익절/줄일 때: 계획한 비중이 있으면 그 안에서만. 없으면 새로 비중을 정한 뒤.",
        f"하지 말 것: {avoid or '조회 실패를 신호로 읽기, 하루 브레드스 움직임으로 코어를 교체하기, 유동성 낮은 ETF 대량 추격.'}",
    ]
    return "\n".join(lines)


# ---------------- 판정 ----------------
def evaluate(cfg: dict, st: dict, obs: dict, today: str) -> list[tuple[str, str, int]]:
    r = cfg["rules"]
    out: list[tuple[str, str, int]] = []
    bars = obs.get("bars")
    ratio = obs.get("ratio")
    rows = obs.get("dists")
    lookback = int(r["breadth_lookback_bars"])

    if ratio and len(ratio) > lookback:
        cur_d, cur = ratio[-1]
        base_d, base = ratio[-(lookback + 1)]
        chg = (cur / base - 1.0) * 100.0
        st["breadth_last"] = {"date": cur_d, "ratio": cur}
        st["breadth_base"] = {"date": base_d, "ratio": base}
        st["breadth_chg_pct_last"] = chg

        if chg < r["breadth_down_pct"] and st.get("breadth_down_armed", True):
            st["breadth_down_armed"] = False
            out.append(
                (
                    f"{TAG} 488500/RSP/SPY 비율 {cur:.4f} | 20거래일 {chg:+.1f}% 약화",
                    f"RSP/SPY 비율 {cur:.4f}({cur_d}), {lookback}거래일 전 "
                    f"{base:.4f}({base_d}) 대비 {chg:+.2f}%. "
                    f"시장 폭 약화(테크 재주도 쪽, 동일가중 매력 하락 점검). "
                    f"{r['breadth_down_rearm_pct']:+.1f}% 위로 돌아와야 다시 무장.",
                    4,
                )
            )
        elif chg > r["breadth_down_rearm_pct"]:
            st["breadth_down_armed"] = True

        if chg > r["breadth_up_pct"] and st.get("breadth_up_armed", True):
            st["breadth_up_armed"] = False
            out.append(
                (
                    f"{TAG} 488500/RSP/SPY 비율 {cur:.4f} | 20거래일 {chg:+.1f}% 강화",
                    f"RSP/SPY 비율 {cur:.4f}({cur_d}), {lookback}거래일 전 "
                    f"{base:.4f}({base_d}) 대비 {chg:+.2f}%. "
                    f"로테이션/중간주 상대 강세(동일가중 상대 매력 상승 점검). "
                    f"{r['breadth_up_rearm_pct']:+.1f}% 아래로 돌아와야 다시 무장.",
                    3,
                )
            )
        elif chg < r["breadth_up_rearm_pct"]:
            st["breadth_up_armed"] = True

    if bars:
        day, close = bars[-1]
        prev = bars[-2][1] if len(bars) >= 2 else None
        if close <= r["price_low"] and st.get("price_low_armed", True):
            st["price_low_armed"] = False
            out.append(
                (
                    f"{TAG} 종가 {close:,.0f}원 | 11,500원 이하 매수 구간",
                    f"{day} 종가 {close:,.0f}원, 매수 참고선 {r['price_low']:,}원 이하. "
                    f"재무장선 {r['price_low_rearm']:,}원 위로 올라가야 다시 무장. "
                    f"유동성 낮은 ETF라 분할만.",
                    4,
                )
            )
        elif close > r["price_low_rearm"]:
            st["price_low_armed"] = True

        if close >= r["price_high"] and st.get("price_high_armed", True):
            st["price_high_armed"] = False
            out.append(
                (
                    f"{TAG} 종가 {close:,.0f}원 | 14,000원 이상 고점 근처",
                    f"{day} 종가 {close:,.0f}원, 고점 참고선 {r['price_high']:,}원 이상. "
                    f"재무장선 {r['price_high_rearm']:,}원 아래로 내려와야 다시 무장.",
                    3,
                )
            )
        elif close < r["price_high_rearm"]:
            st["price_high_armed"] = True
        st["price_last_date"] = day

        if day == today and st.get("last_summary_date") != today:
            chg_px = ((close / prev - 1) * 100) if prev else None
            chg_txt = f"{chg_px:+.2f}%" if chg_px is not None else "비교 불가"
            if ratio and st.get("breadth_chg_pct_last") is not None and st.get("breadth_last"):
                br = st["breadth_last"]
                b_txt = f"RSP/SPY {br['ratio']:.4f}({br['date']}), 20거래일 {st['breadth_chg_pct_last']:+.2f}%"
            else:
                b_txt = "RSP/SPY 확인 불가"
            out.append(
                (
                    f"{TAG} 일일 요약 | 종가 {close:,.0f}원",
                    f"{day} 종가 {close:,.0f}원, 전일 대비 {chg_txt}, {b_txt}, "
                    f"11,500까지 {close - r['price_low']:+,.0f}원, "
                    f"14,000까지 {close - r['price_high']:+,.0f}원.",
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
                    f"{TAG} 분기 분배금 {nv:.0f}원 | 직전 {last_a:.0f}원에서 변경",
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
    names = {"bars": "종가", "ratio": "RSP/SPY 비율", "dists": "분배금 이력"}
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
        ("ratio", rsp_spy_ratio_series),
        ("dists", dists),
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
                "RSP/SPY ±3%(20거래일), 종가 11,500/14,000, 분배금 변경 시에만 별도 알림.",
                "일일 요약만 보고 바로 매수/매도.",
            )
        elif "약화" in title:
            msg = body(
                stamp,
                text,
                "테크 재주도 여부를 확인. 동일가중 비중을 늘리기 전에 시장 폭을 다시 보세요.",
                "-1.5% 위로 돌아오면 다시 무장됩니다.",
            )
        elif "강화" in title:
            msg = body(
                stamp,
                text,
                "로테이션 지속 여부를 확인. 시총가중 대비 보완 비중을 점검하세요.",
                "+1.5% 아래로 돌아오면 다시 무장됩니다.",
            )
        elif "11,500" in title or "매수 구간" in title:
            msg = body(
                stamp,
                text,
                "분할 매수만 검토. 유동성 낮아 슬리피지 주의. RSP/SPY 브레드스도 같이 보세요.",
                "종가가 11,700원 위로 올라온 뒤 다시 11,500원 이하로 오면 또 알림.",
            )
        elif "14,000" in title or "고점" in title:
            msg = body(
                stamp,
                text,
                "고점 근처 추격은 계획 비중 안에서만. 시총가중과의 상대 성과를 같이 보세요.",
                "종가가 13,800원 아래로 내려온 뒤 다시 14,000원 이상으로 오면 또 알림.",
            )
        elif "분배금" in title:
            msg = body(
                stamp,
                text,
                "분기 분배금 흐름이 계획과 맞는지 확인. 한 분기 값만으로 비중을 바꾸지 마세요.",
                "다음 분기 금액이 또 바뀌면 다시 알림.",
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
        ratio = obs.get("ratio") or []
        d = (obs.get("dists") or [None])[-1]
        status = f"종가 {b[1]:,.0f}원({b[0]})" if b else "종가 확인 불가"
        if ratio and len(ratio) > 20:
            cur_d, cur = ratio[-1]
            base_d, base = ratio[-21]
            chg = (cur / base - 1.0) * 100.0
            status += f", RSP/SPY {cur:.4f}({cur_d}) 20거래일 {chg:+.2f}%"
        else:
            status += ", RSP/SPY 확인 불가"
        if d:
            status += f", 최근 분배금 {d[1]:.0f}원({d[0]})"
        if errs:
            status += f". 조회 실패: {json.dumps(errs, ensure_ascii=False)}"
        title = f"{TAG} 설치 확인 | 매매 신호 아님"
        msg = body(
            stamp,
            status + ". 지금 켜진 이벤트 조건 없음(설치 확인).",
            "없음. KRX 거래일 19:00에 조건이 새로 켜질 때만 옵니다.",
            "조건: RSP/SPY ±3%(20거래일), 종가 11,500/14,000, 분기 분배금 변경, 일일 요약.",
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
                "ratio": (obs.get("ratio") or [])[-3:],
                "dist_last": (obs.get("dists") or [])[-2:],
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
    check("etfro fixture last 43", lab[-1], ("2026-10-29", 43.0))
    check("fixture has 6 rows", len(lab), 6)

    def init():
        s = default_state()
        s.update(
            last_dist_date="2026-10-29",
            last_dist_amt=43.0,
            last_summary_date="2026-11-05",
            price_last_date="2026-11-05",
            breadth_up_armed=True,
            breadth_down_armed=True,
        )
        return s

    def mk_ratio(last: float, base: float, n: int = 25) -> list[tuple[str, float]]:
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

    bars = [("2026-11-05", 12615.0), ("2026-11-06", 12425.0)]
    ratio_flat = mk_ratio(0.28, 0.28)
    base = {"bars": bars, "ratio": ratio_flat, "dists": lab}

    st = init()
    check(
        "mid silent when summary already sent for old day",
        evaluate(cfg, st, dict(base, bars=[("2026-11-05", 12615.0)]), "2026-11-06"),
        [],
    )

    # breadth down
    st = init()
    r_dn = mk_ratio(0.27, 0.28)  # -3.57%
    a = evaluate(cfg, st, dict(base, bars=[("2026-11-05", 12615.0)], ratio=r_dn, dists=None), "2026-11-06")
    check(
        "breadth down fires",
        any("약화" in t and "488500/RSP/SPY" in t for t in [x[0] for x in a]),
        True,
    )
    check("breadth down disarmed", st["breadth_down_armed"], False)
    r_mid = mk_ratio(0.274, 0.28)  # -2.14%
    evaluate(cfg, st, dict(base, bars=[("2026-11-05", 12615.0)], ratio=r_mid, dists=None), "2026-11-07")
    check("-2.14 no down rearm", st["breadth_down_armed"], False)
    r_ok = mk_ratio(0.276, 0.28)  # -1.43%
    evaluate(cfg, st, dict(base, bars=[("2026-11-05", 12615.0)], ratio=r_ok, dists=None), "2026-11-08")
    check("above -1.5 rearms down", st["breadth_down_armed"], True)

    # breadth up
    st = init()
    r_up = mk_ratio(0.29, 0.28)  # +3.57%
    a = evaluate(cfg, st, dict(base, bars=[("2026-11-05", 12615.0)], ratio=r_up, dists=None), "2026-11-06")
    check(
        "breadth up fires",
        any("강화" in t and "488500/RSP/SPY" in t for t in [x[0] for x in a]),
        True,
    )
    check("breadth up disarmed", st["breadth_up_armed"], False)
    r_mid2 = mk_ratio(0.286, 0.28)  # +2.14%
    evaluate(cfg, st, dict(base, bars=[("2026-11-05", 12615.0)], ratio=r_mid2, dists=None), "2026-11-07")
    check("+2.14 no up rearm", st["breadth_up_armed"], False)
    r_ok2 = mk_ratio(0.283, 0.28)  # +1.07%
    evaluate(cfg, st, dict(base, bars=[("2026-11-05", 12615.0)], ratio=r_ok2, dists=None), "2026-11-08")
    check("below +1.5 rearms up", st["breadth_up_armed"], True)

    # price low
    st = init()
    low = [("2026-11-05", 11600.0), ("2026-11-06", 11480.0)]
    a = evaluate(cfg, st, dict(base, bars=low, ratio=None, dists=None), "2026-11-06")
    titles = [x[0] for x in a]
    check(
        "low + daily",
        titles,
        [
            f"{TAG} 종가 11,480원 | 11,500원 이하 매수 구간",
            f"{TAG} 일일 요약 | 종가 11,480원",
        ],
    )
    check("low disarmed", st["price_low_armed"], False)
    st["last_summary_date"] = "2026-11-06"
    evaluate(cfg, st, dict(base, bars=[("2026-11-07", 11650.0)], ratio=None, dists=None), "2026-11-07")
    check("11650 no low rearm", st["price_low_armed"], False)
    evaluate(cfg, st, dict(base, bars=[("2026-11-08", 11750.0)], ratio=None, dists=None), "2026-11-08")
    check("11750 rearms low", st["price_low_armed"], True)

    # price high
    st = init()
    hi = [("2026-11-05", 13900.0), ("2026-11-06", 14050.0)]
    a = evaluate(cfg, st, dict(base, bars=hi, ratio=None, dists=None), "2026-11-06")
    check("high fires", any("14,000원 이상" in t for t in [x[0] for x in a]), True)
    check("high disarmed", st["price_high_armed"], False)
    st["last_summary_date"] = "2026-11-06"
    evaluate(cfg, st, dict(base, bars=[("2026-11-07", 13900.0)], ratio=None, dists=None), "2026-11-07")
    check("13900 no high rearm", st["price_high_armed"], False)
    evaluate(cfg, st, dict(base, bars=[("2026-11-08", 13750.0)], ratio=None, dists=None), "2026-11-08")
    check("13750 rearms high", st["price_high_armed"], True)

    # dist change
    st = init()
    nov = lab + [("2027-01-29", 40.0)]
    a = evaluate(cfg, st, dict(base, dists=nov, bars=[("2026-11-05", 12615.0)], ratio=None), "2026-11-06")
    check("dist change 43->40", [x[0] for x in a], [f"{TAG} 분기 분배금 40원 | 직전 43원에서 변경"])
    check(
        "same dist not repeated",
        evaluate(cfg, st, dict(base, dists=nov, bars=[("2026-11-05", 12615.0)], ratio=None), "2026-11-06"),
        [],
    )

    st = init()
    same = lab + [("2027-01-29", 43.0)]
    a = evaluate(cfg, st, dict(base, dists=same, bars=[("2026-11-05", 12615.0)], ratio=None), "2026-11-06")
    check("same amount new date silent", (a, st["last_dist_date"]), ([], "2027-01-29"))

    # daily once
    st = init()
    a = evaluate(cfg, st, dict(base, ratio=ratio_flat), "2026-11-06")
    check("daily once", [x[0] for x in a], [f"{TAG} 일일 요약 | 종가 12,425원"])
    check("ratio in body", "RSP/SPY 0.2800" in a[0][1], True)
    check("summary not repeated", evaluate(cfg, st, dict(base), "2026-11-06"), [])

    # fail ops
    st = init()
    f1 = fail_alerts(cfg, st, {"ratio": "x"}, "2026-11-06")
    f2 = fail_alerts(cfg, st, {"ratio": "x"}, "2026-11-09")
    check("ops silent day1", f1, [])
    check(
        "ops on 2nd failing day",
        [x[0] for x in f2],
        [f"{TAG} RSP/SPY 비율 조회 실패 | 2일 연속, 그 규칙 건너뜀"],
    )

    msgs = []
    for kw in (
        {"bars": low, "ratio": r_dn, "dists": nov},
        {"bars": hi, "ratio": r_up, "dists": None},
    ):
        for ti, tx, _ in evaluate(cfg, init(), dict(base, **kw), "2026-11-06"):
            msgs.append(ti + body("s", tx, "a"))
    text = " ".join(msgs) + json.dumps(cfg, ensure_ascii=False)
    check(
        "titles start [488500], RSP/SPY tickers, no banned chars",
        (
            all(m.startswith(TAG) for m in msgs),
            any("488500/RSP/SPY" in m for m in msgs),
            "\u00b7" in text,
            "\u2014" in text,
        ),
        (True, True, False, False),
    )
    check("ends 2027-11-06 23:59", cfg["ends_kst"], "2027-11-06 23:59")
    check("isin", cfg["isin"], "KR7488500000")
    check("routing failure -> ops", route_topic("cwally-port", f2[0][0]), "cwally-ops")
    print(f"SELFTEST {'PASS' if not fails else 'FAIL'}: {len(fails)} failed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
