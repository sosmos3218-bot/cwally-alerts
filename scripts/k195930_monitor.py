#!/usr/bin/env python3
"""[195930] 458730/475350 주간 상관 0.60 감시 (2026-11-20 승인 pt74u).

KRX 거래일 15:40 KST (0090b0 슬롯에 부착, [055550] 바로 뒤). 새 cron 없음.
완결된 주(그 주 마지막 거래일 종가, 금요일이 휴장이면 목요일)만 판정.
기본 창은 26주 총수익 주간 상관. 52주는 본문 참고.
트리거: corr(195930, 458730) 또는 corr(195930, 475350) 또는
corr(195930, 미국 6:1 합산) 중 하나라도 0.60 이상이면 p3 1건.
재무장: 세 값 모두 0.50 이하.
1월 첫 점검 주에 연 1회 리밸런싱 안내 p4 1건.
판정은 Daum KRX 확정 종가 + FunETF 분배락일 분배금(총수익). 네이버 일봉 안 씀.
첫 실행은 상태만 기록. 만료일 없음(수동 해제 전까지).
modes: auto | dry(발송 없음, state 안 씀) | selftest
"""
from __future__ import annotations

import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from kr_calendar import KST, is_trading_day, session_close_ready, session_day  # noqa: E402
from krx_close import completed, regular_bars  # noqa: E402
from ntfy_util import publish, resolve_server, resolve_topic, route_topic  # noqa: E402

TAG = "[195930]"
NAME = "k195930"
CFG_PATH = ROOT / "configs" / f"{NAME}.json"
STATE_PATH = ROOT / "state" / f"{NAME}.json"
CLOSE_HM = (15, 40)
FUNETF = "https://www.funetf.co.kr"
UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
}
CODES_US = ("458730", "475350")
CODE_EU = "195930"
ALL_CODES = ("458730", "475350", "195930")
BLEND_W = (6 / 7, 1 / 7)
FORBIDDEN = re.compile("[\u00b7\u318d\u2014]")


def check_text(t: str, m: str) -> None:
    if FORBIDDEN.search(t + m):
        raise AssertionError(f"forbidden char in {t}")


def friday_of(d: date) -> date:
    return d + timedelta(days=4 - d.weekday())


def last_session_of_week(friday: date) -> date:
    d = friday
    for _ in range(12):
        if is_trading_day(d):
            return d
        d -= timedelta(days=1)
    raise ValueError(f"주말 세션 없음: {friday}")


def week_complete(friday: date, today: date, now: datetime) -> bool:
    last = last_session_of_week(friday)
    if today < last:
        return False
    if today == last:
        return session_close_ready(last.isoformat(), today.isoformat(), now, CLOSE_HM)
    return True


def latest_complete_friday(today: date, now: datetime) -> date:
    fr = friday_of(today)
    if not week_complete(fr, today, now):
        fr -= timedelta(days=7)
    return fr


def pearson(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 3 or n != len(ys):
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = sum((x - mx) ** 2 for x in xs)
    dy = sum((y - my) ** 2 for y in ys)
    if dx <= 0 or dy <= 0:
        return None
    return num / ((dx ** 0.5) * (dy ** 0.5))


def rolling_corr(a: list[float], b: list[float], window: int) -> list[float | None]:
    out: list[float | None] = []
    for i in range(len(a)):
        if i + 1 < window:
            out.append(None)
        else:
            out.append(pearson(a[i + 1 - window : i + 1], b[i + 1 - window : i + 1]))
    return out


def _http(url: str, headers: dict | None = None, timeout: int = 25) -> str:
    last: Exception | None = None
    for i in range(3):
        try:
            h = dict(UA)
            if headers:
                h.update(headers)
            req = urllib.request.Request(url, headers=h)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8", "replace")
        except Exception as exc:  # noqa: BLE001
            last = exc
            if i < 2:
                time.sleep(0.4 * (i + 1))
    raise last  # type: ignore[misc]


def funetf_divs(isin: str) -> dict[str, float]:
    pg = _http(f"{FUNETF}/product/etf/view/{isin}")
    form = re.search(r'(?s)<form name="frm" id="frm">(.*?)</form>', pg)
    if not form:
        raise RuntimeError("funetf 상품 페이지 형식 이상")
    fields = dict(re.findall(r'name="([^"]+)"[^>]*value="([^"]*)"', form.group(1)))
    if not fields.get("fundCd") or fields.get("itemId") != isin:
        raise RuntimeError("funetf fundCd 없음")
    q = urllib.parse.urlencode(fields)
    raw = json.loads(_http(
        f"{FUNETF}/api/public/product/view/etfdividend?{q}",
        {"X-Requested-With": "XMLHttpRequest", "Referer": f"{FUNETF}/product/etf/view/{isin}"},
    ))
    out: dict[str, float] = {}
    for r in raw or []:
        g = str(r.get("gijunYmd") or "")
        try:
            a = float(r.get("divAmt"))
        except (TypeError, ValueError):
            continue
        if re.fullmatch(r"\d{8}", g):
            out[f"{g[:4]}-{g[4:6]}-{g[6:]}"] = a
    return out


def merge_divs(cached: dict, live: dict | None) -> dict[str, float]:
    out = {str(k): float(v) for k, v in (cached or {}).items()}
    if live:
        out.update({str(k): float(v) for k, v in live.items()})
    return out


def closes_from_bars(bars) -> list[tuple[str, float]]:
    return [(b.day, float(b.close)) for b in bars]


def align_closes(by_code: dict[str, list[tuple[str, float]]]) -> list[str]:
    sets = [set(d for d, _ in rows) for rows in by_code.values()]
    common = sets[0]
    for s in sets[1:]:
        common &= s
    return sorted(common)


def tr_index(days: list[str], close: dict[str, float], divs: dict[str, float]) -> dict[str, float]:
    out: dict[str, float] = {}
    prev_px = None
    level = 1.0
    for d in days:
        px = close[d]
        if prev_px is None:
            out[d] = 1.0
        else:
            level *= (px + divs.get(d, 0.0)) / prev_px
            out[d] = level
        prev_px = px
    return out


def week_end_series(days: list[str], level: dict[str, float]) -> list[tuple[str, str, float]]:
    """(friday_label, last_session, index_level) oldest first, one row per week."""
    buckets: dict[str, tuple[str, float]] = {}
    for d in days:
        fr = friday_of(date.fromisoformat(d)).isoformat()
        buckets[fr] = (d, level[d])
    out = []
    for fr in sorted(buckets):
        sess, lv = buckets[fr]
        out.append((fr, sess, lv))
    return out


def weekly_returns(rows: list[tuple[str, str, float]]) -> list[tuple[str, str, float]]:
    out = []
    for i in range(1, len(rows)):
        fr, sess, lv = rows[i]
        prev = rows[i - 1][2]
        if prev == 0:
            continue
        out.append((fr, sess, lv / prev - 1.0))
    return out


def pair_corrs(weekly: dict[str, list[tuple[str, str, float]]], window: int) -> list[dict]:
    """Align by friday label; return list of {friday, session, c458, c475, cblend}."""
    by_fr = {}
    for code, rows in weekly.items():
        for fr, sess, ret in rows:
            by_fr.setdefault(fr, {})[code] = (sess, ret)
    frs = sorted(fr for fr, m in by_fr.items() if all(c in m for c in ALL_CODES))
    r458 = [by_fr[fr]["458730"][1] for fr in frs]
    r475 = [by_fr[fr]["475350"][1] for fr in frs]
    r195 = [by_fr[fr]["195930"][1] for fr in frs]
    rbl = [BLEND_W[0] * a + BLEND_W[1] * b for a, b in zip(r458, r475)]
    c458 = rolling_corr(r195, r458, window)
    c475 = rolling_corr(r195, r475, window)
    cbl = rolling_corr(r195, rbl, window)
    out = []
    for i, fr in enumerate(frs):
        if c458[i] is None or c475[i] is None or cbl[i] is None:
            continue
        out.append({
            "friday": fr,
            "session": by_fr[fr]["195930"][0],
            "c458": c458[i],
            "c475": c475[i],
            "cblend": cbl[i],
        })
    return out


def fmt_corr(x: float | None) -> str:
    if x is None:
        return "없음"
    return f"{x:.3f}"


def triggered(row: dict, thresh: float) -> list[str]:
    hits = []
    if row["c458"] >= thresh:
        hits.append("458730")
    if row["c475"] >= thresh:
        hits.append("475350")
    if row["cblend"] >= thresh:
        hits.append("USblend")
    return hits


def all_rearmed(row: dict, rearm: float) -> bool:
    return row["c458"] <= rearm and row["c475"] <= rearm and row["cblend"] <= rearm


def seed_state(row26: dict, row52: dict | None, stamp: str, reb_year: int) -> dict:
    return {
        "last_week": row26["friday"],
        "last_session": row26["session"],
        "corr26": {"458730": row26["c458"], "475350": row26["c475"], "USblend": row26["cblend"]},
        "corr52": None if row52 is None else {
            "458730": row52["c458"], "475350": row52["c475"], "USblend": row52["cblend"],
        },
        "armed": not bool(triggered(row26, 0.60)),
        "reb_year_sent": reb_year,
        "seed_note": (
            f"첫 실행 {stamp}: 완결 주 {row26['friday']}(세션 {row26['session']}) "
            f"26주 총수익 상관 458730 {fmt_corr(row26['c458'])}, "
            f"475350 {fmt_corr(row26['c475'])}, 미국 6:1 합산 {fmt_corr(row26['cblend'])}"
        ),
    }


def evaluate(rows26: list[dict], ps: dict, fire: float, rearm: float) -> list[dict]:
    out = []
    last = ps.get("last_week") or ""
    for row in rows26:
        if row["friday"] <= last:
            continue
        hits = triggered(row, fire)
        if hits and ps.get("armed", True):
            ps["armed"] = False
            out.append({**row, "hits": hits, "kind": "corr"})
        elif (not ps.get("armed", True)) and all_rearmed(row, rearm):
            ps["armed"] = True
        ps["last_week"] = row["friday"]
        ps["last_session"] = row["session"]
        ps["corr26"] = {"458730": row["c458"], "475350": row["c475"], "USblend": row["cblend"]}
    return out


def body(stamp: str, what: str, status: str, action: str, later: str, trim: str, avoid: str) -> str:
    return "\n".join([
        f"{stamp} | 195930 | TIGER 유로스탁스50(합성 H) vs 458730/475350",
        f"이게 뭐 알림: {what}",
        f"지금 결과/상태: {status}",
        f"지금 할 일: {action}",
        f"나중에: {later}",
        f"익절/줄일 때: {trim}",
        f"하지 말 것: {avoid}",
    ])


WHAT_CORR = (
    "195930과 미국 ETF(458730, 475350, 미국 6:1 합산)의 26주 총수익 주간 상관이 "
    "0.60 이상에 닿았다는 알림. 매매 지시 아님. 투자 참고용."
)
WHAT_REB = (
    "60/10/30 포트폴리오의 연 1회 리밸런싱 점검 안내. 매매 지시 아님. 투자 참고용."
)
WHAT_OPS = "195930 주간 상관 알림의 데이터 읽기 실패 안내. 하루 1번. 매매 신호 아님."


def corr_message(cfg: dict, ev: dict, row52: dict | None, stamp: str) -> tuple[str, str, int]:
    hits = ", ".join(ev["hits"])
    st = (
        f"기준주 {ev['friday']} (마지막 거래일 {ev['session']}). "
        f"26주 총수익 주간 상관: 458730 {fmt_corr(ev['c458'])}, "
        f"475350 {fmt_corr(ev['c475'])}, 미국 6:1 합산 {fmt_corr(ev['cblend'])}. "
        f"0.60 돌파: {hits}."
    )
    if row52:
        st += (
            f" 참고 52주: 458730 {fmt_corr(row52['c458'])}, "
            f"475350 {fmt_corr(row52['c475'])}, 미국 6:1 합산 {fmt_corr(row52['cblend'])}."
        )
    st += f" {cfg.get('context', '')}"
    title = f"{TAG} 미국 ETF(458730, 475350) 주간 상관 0.60 도달"
    return (
        title,
        body(
            stamp,
            WHAT_CORR,
            st,
            "195930 비중 30%에서 20%로 축소 검토",
            f"26주 상관이 458730, 475350, 미국 6:1 합산 모두 {cfg['rearm']:.2f} 이하로 내려가면 다시 무장합니다.",
            "비중을 줄일 때는 195930 20일 평균 거래대금의 10%를 넘는 시장가 주문을 피하기.",
            "자동 매수/매도 신호로 보기, 상관 한 번으로 전량 매도하기. 투자 권유 아님.",
        ),
        int(cfg.get("priority", 3)),
    )


def reb_message(cfg: dict, row26: dict, stamp: str, year: int) -> tuple[str, str, int]:
    st = (
        f"{year}년 연 1회 리밸런싱 점검 주 {row26['friday']} (세션 {row26['session']}). "
        f"목표 비중 458730 60%, 475350 10%, 195930 30%. "
        f"지금 26주 상관: 458730 {fmt_corr(row26['c458'])}, "
        f"475350 {fmt_corr(row26['c475'])}, 미국 6:1 합산 {fmt_corr(row26['cblend'])}. "
        f"{cfg.get('rebalance_note', '')}"
    )
    title = f"{TAG} 458730/475350 연 1회 리밸런싱 점검"
    return (
        title,
        body(
            stamp,
            WHAT_REB,
            st,
            "첫 거래일 종가 기준으로 458730 60%, 475350 10%, 195930 30%에 맞춰 리밸런싱을 점검하세요.",
            "다음 안내는 내년 1월입니다. 상관 0.60 알림은 따로 옵니다.",
            "거래대금이 얇은 195930, 475350은 여러 날에 나눠 조정.",
            "한 번에 시장가로 크게 주문하기. 투자 권유 아님.",
        ),
        int(cfg.get("rebalance_priority", 4)),
    )


def ops_msg(stamp: str, status: str) -> tuple[str, str]:
    return (
        f"{TAG} 458730/475350 주간 상관 조회 실패 | 판정 건너뜀",
        body(
            stamp,
            WHAT_OPS,
            status,
            "증권 앱에서 195930, 458730, 475350 종가를 직접 보세요.",
            "다음 15:40 슬롯에서 다시 읽고, 빠진 주는 이어서 판정합니다.",
            "이 안내로 비중을 바꾸지 않기.",
            "네이버 일별(NXT 섞임)로 대신 판정하기.",
        ),
    )


def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(st: dict) -> None:
    STATE_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def fetch_closes(n: int, now: datetime, bars_fn=None) -> dict[str, list[tuple[str, float]]]:
    fn = bars_fn or (lambda code: regular_bars(code, n, compare=False))
    out = {}
    for code in ALL_CODES:
        bars = completed(fn(code), now, CLOSE_HM)
        if not bars:
            raise RuntimeError(f"{code} 확정 종가 없음")
        out[code] = closes_from_bars(bars)
    return out


def fetch_divs(cfg: dict) -> tuple[dict[str, dict[str, float]], str | None]:
    cached = cfg.get("div_cache") or {}
    live_err = []
    live: dict[str, dict[str, float]] = {}
    for code in ALL_CODES:
        isin = cfg["etfs"][code]["isin"]
        try:
            live[code] = funetf_divs(isin)
        except Exception as exc:  # noqa: BLE001
            live_err.append(f"{code}: {type(exc).__name__}: {str(exc)[:80]}")
    merged = {c: merge_divs(cached.get(c) or {}, live.get(c)) for c in ALL_CODES}
    warn = None
    if live_err:
        warn = "FunETF 분배금 조회 실패, 캐시로 계산: " + "; ".join(live_err)
    if any(not merged[c] and c == "458730" for c in ALL_CODES):
        # 458730 is monthly; empty after a failed live+empty cache is a real problem
        if not (cached.get("458730") or live.get("458730")):
            raise RuntimeError("458730 분배금 없음")
    return merged, warn


def build_rows(closes: dict[str, list[tuple[str, float]]], divs: dict[str, dict[str, float]],
               start: str, last_friday: str) -> tuple[list[dict], list[dict]]:
    last_sess = last_session_of_week(date.fromisoformat(last_friday)).isoformat()
    days = [d for d in align_closes(closes) if start <= d <= last_sess]
    if len(days) < 30:
        raise RuntimeError(f"공통 종가 부족 {len(days)}일")
    weekly = {}
    for code in ALL_CODES:
        cmap = dict(closes[code])
        level = tr_index(days, cmap, divs.get(code) or {})
        weekly[code] = weekly_returns(week_end_series(days, level))
    rows26 = pair_corrs(weekly, 26)
    rows52 = pair_corrs(weekly, 52)
    # drop any week after last_friday (should not happen)
    rows26 = [r for r in rows26 if r["friday"] <= last_friday]
    rows52 = [r for r in rows52 if r["friday"] <= last_friday]
    return rows26, rows52


def row_as_of(rows: list[dict], friday: str) -> dict | None:
    for r in reversed(rows):
        if r["friday"] == friday:
            return r
    return rows[-1] if rows else None


def run(mode: str, now: datetime, cfg: dict, pub=publish, bars_fn=None,
        divs_fn=None) -> int:
    dry = mode == "dry"
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    topic, server = resolve_topic("port", cfg.get("ntfy_topic")), resolve_server(cfg.get("ntfy_server"))
    print(f"{stamp} {NAME} mode={mode}")
    ends = cfg.get("ends_kst")
    if ends:
        end_dt = datetime.strptime(ends, "%Y-%m-%d %H:%M").replace(tzinfo=KST)
        if now > end_dt:
            print(f"{NAME} ended; silent")
            return 0
    sday = session_day(now)
    today = sday
    start = cfg.get("window_start", "2024-02-27")
    fire = float(cfg.get("fire", 0.60))
    rearm = float(cfg.get("rearm", 0.50))
    n_bars = int(cfg.get("bar_count", 800))

    st = load_state()
    latest_fr = latest_complete_friday(today, now)
    last_week = st.get("last_week")
    need_fetch = True
    if last_week and latest_fr.isoformat() <= last_week and mode != "dry":
        print(f"{today.isoformat()} 새 완결 주 없음(마지막 {last_week}, 최신 완결 {latest_fr}): 조용히 종료")
        need_fetch = False
        return 0
    if not need_fetch:
        return 0

    out: list[tuple[str, str, int]] = []
    err = None
    ops = None
    try:
        closes = fetch_closes(n_bars, now, bars_fn=bars_fn)
        if divs_fn:
            divs, warn = divs_fn()
        else:
            divs, warn = fetch_divs(cfg)
        if warn:
            print("WARN:", warn)
        rows26, rows52 = build_rows(closes, divs, start, latest_fr.isoformat())
        if not rows26:
            raise RuntimeError("26주 상관 계산 불가(주간 수익률 부족)")
        last26 = rows26[-1]
        last52 = row_as_of(rows52, last26["friday"])
        print(
            f"latest complete week {last26['friday']} session {last26['session']} "
            f"26w 458730={fmt_corr(last26['c458'])} 475350={fmt_corr(last26['c475'])} "
            f"USblend={fmt_corr(last26['cblend'])}"
            + ("" if not last52 else
               f" 52w 458730={fmt_corr(last52['c458'])} 475350={fmt_corr(last52['c475'])} "
               f"USblend={fmt_corr(last52['cblend'])}")
        )
        if not st.get("last_week"):
            st = seed_state(last26, last52, stamp, today.year)
            print(f"first run: state only, no alert: {st['seed_note']}")
        else:
            ev = evaluate(rows26, st, fire, rearm)
            if last52:
                st["corr52"] = {
                    "458730": last52["c458"], "475350": last52["c475"], "USblend": last52["cblend"],
                }
            for e in ev:
                r52 = row_as_of(rows52, e["friday"])
                out.append(corr_message(cfg, e, r52, stamp))
            # 연 1회 리밸런싱 안내: 이번 처리에 1월 세션이 새로 들어왔고 그 해에 아직 안 보냄
            sent = int(st.get("reb_year_sent") or 0)
            new_weeks = [r for r in rows26 if r["friday"] > (last_week or "")]
            for r in new_weeks:
                y = date.fromisoformat(r["session"]).year
                if date.fromisoformat(r["session"]).month == 1 and y > sent:
                    out.append(reb_message(cfg, r, stamp, y))
                    st["reb_year_sent"] = y
                    break
    except Exception as exc:  # noqa: BLE001
        err = f"주간 상관 데이터 조회/계산 실패: {str(exc)[:200]}. 판정 건너뜀"

    if err:
        print("ERR:", err)
        if st.get("ops_day") != today.isoformat():
            ops = ops_msg(stamp, err + ". 네이버(NXT 섞임)로 대신 판정하지 않음. state 의 주차는 그대로.")
            st["ops_day"] = today.isoformat()

    for t, m, pri in out:
        check_text(t, m)
        print(f"TITLE: {t}\nPRIORITY: {pri} TOPIC: {route_topic(topic, t)}\n{m}\n")
    if ops:
        check_text(ops[0], ops[1])
        print(f"OPS TITLE: {ops[0]} -> {route_topic(topic, ops[0])}\n{ops[1]}\n")
    if not out and not ops and not err:
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
        # ops only: do not advance last_week; already not updated on exception
        # but ops_day should persist
        st["updated_at"] = stamp
        save_state(st)
        return 0
    st["updated_at"] = stamp
    save_state(st)
    return 0


def selftest() -> int:
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    fails = 0

    def check(label, ok):
        nonlocal fails
        fails += not ok
        print(("ok  " if ok else "FAIL"), label)

    # pearson sanity
    check("pearson perfect", abs(pearson([1, 2, 3, 4], [2, 4, 6, 8]) - 1.0) < 1e-12)
    check("pearson none on constant", pearson([1, 1, 1], [1, 2, 3]) is None)

    # week helpers: 2026-05-01 Friday holiday, last session 2026-04-30
    check("friday_of Thu", friday_of(date(2026, 4, 30)) == date(2026, 5, 1))
    check("last session before Fri holiday", last_session_of_week(date(2026, 5, 1)) == date(2026, 4, 30))
    thu_close = datetime(2026, 4, 30, 15, 40, tzinfo=KST)
    thu_early = datetime(2026, 4, 30, 14, 0, tzinfo=KST)
    fri_hol = datetime(2026, 5, 1, 15, 40, tzinfo=KST)
    check("Thu early week incomplete", not week_complete(date(2026, 5, 1), date(2026, 4, 30), thu_early))
    check("Thu close completes holiday week", week_complete(date(2026, 5, 1), date(2026, 4, 30), thu_close))
    check("Fri holiday still complete", week_complete(date(2026, 5, 1), date(2026, 5, 1), fri_hol))
    # normal Friday
    fri = datetime(2026, 11, 20, 15, 40, tzinfo=KST)
    fri_am = datetime(2026, 11, 20, 10, 22, tzinfo=KST)
    check("Fri morning incomplete", not week_complete(date(2026, 11, 20), date(2026, 11, 20), fri_am))
    check("Fri 15:40 complete", week_complete(date(2026, 11, 20), date(2026, 11, 20), fri))
    check("Thu before normal Fri incomplete", not week_complete(date(2026, 11, 20), date(2026, 11, 19), thu_close.replace(year=2026, month=11, day=19)))

    # synthetic weekly corrs: build evaluate walk
    def row(fr, c458, c475, cbl, sess=None):
        return {"friday": fr, "session": sess or fr, "c458": c458, "c475": c475, "cblend": cbl}

    seed_row = row("2026-10-02", 0.20, 0.45, 0.25)
    ps = seed_state(seed_row, row("2026-10-02", 0.10, 0.30, 0.15), "T", 2026)
    check("seed below 0.60 is armed", ps["armed"] is True)
    walk = [
        row("2026-10-09", 0.20, 0.599, 0.25),   # no fire (below 0.60)
        row("2026-10-16", 0.20, 0.600, 0.25),   # fire 475350
        row("2026-10-23", 0.20, 0.70, 0.25),    # still high, silent
        row("2026-10-30", 0.20, 0.55, 0.25),    # between, no rearm
        row("2026-11-06", 0.20, 0.50, 0.25),    # rearm (all <= 0.50)
        row("2026-11-13", 0.20, 0.61, 0.25),    # fire again
        row("2026-11-20", 0.60, 0.40, 0.58),    # if were armed would fire 458730; still disarmed
    ]
    ev = evaluate(walk, ps, 0.60, 0.50)
    got = [(e["friday"], e["hits"]) for e in ev]
    want = [
        ("2026-10-16", ["475350"]),
        ("2026-11-13", ["475350"]),
    ]
    check(f"hysteresis walk {got}", got == want)
    check("ends disarmed after last fire", ps["armed"] is False)
    check("last_week advanced", ps["last_week"] == "2026-11-20")

    # 0.599 does not fire; 0.600 does; blend-only fire
    ps2 = seed_state(row("2026-09-04", 0.1, 0.1, 0.1), None, "T", 2026)
    ev2 = evaluate([
        row("2026-09-11", 0.1, 0.1, 0.5999),
        row("2026-09-18", 0.1, 0.1, 0.60),
    ], ps2, 0.60, 0.50)
    check("blend 0.60 fires, 0.5999 does not", [e["hits"] for e in ev2] == [["USblend"]])

    # same week not reprocessed
    ev3 = evaluate([row("2026-09-18", 0.9, 0.9, 0.9)], ps2, 0.60, 0.50)
    check("same week skipped", ev3 == [])

    # messages
    ev_hit = {**row("2026-10-16", 0.192, 0.600, 0.257, "2026-10-16"), "hits": ["475350"], "kind": "corr"}
    r52 = row("2026-10-16", 0.092, 0.298, 0.125)
    t, m, pri = corr_message(cfg, ev_hit, r52, "2026-10-16 15:40 KST")
    check("title has three tickers", "195930" in t and "458730" in t and "475350" in t)
    check("title wording", t == "[195930] 미국 ETF(458730, 475350) 주간 상관 0.60 도달")
    check("priority p3", pri == 3)
    check("body has 26w numbers", "0.192" in m and "0.600" in m and "0.257" in m)
    check("body has 52w ref", "52주" in m and "0.092" in m)
    check("body has action", "195930 비중 30%에서 20%로 축소 검토" in m)
    check("body has 6:1", "미국 6:1 합산" in m)
    check("body has week date", "2026-10-16" in m)
    check_text(t, m)
    check("corr msg no forbidden", True)

    rt, rm, rp = reb_message(cfg, ev_hit, "2027-01-08 15:40 KST", 2027)
    check("reb title has tickers", "195930" in rt and "458730" in rt and "475350" in rt)
    check("reb priority p4", rp == 4)
    check("reb body 60/10/30", "60%" in rm and "10%" in rm and "30%" in rm)
    check_text(rt, rm)

    ot, om = ops_msg("T", "x")
    check("ops title has tickers", "195930" in ot and "458730" in ot and "475350" in ot)
    check_text(ot, om)

    # seed already >= 0.60 starts disarmed
    s3 = seed_state(row("2026-10-02", 0.1, 0.70, 0.2), None, "T", 2026)
    check("seed above 0.60 disarmed", s3["armed"] is False)

    # dry run does not publish or write: isolated with fixtures
    published = []

    def fake_pub(*a, **k):
        published.append((a, k))

    class Bar:
        def __init__(self, day, close):
            self.day = day
            self.close = close

    # 80 weeks of loosely related returns so 26w corr is computable, ends 2026-11-13 below 0.60
    # Build daily closes Mon-Fri from 2024-02-27 for ~30 months.
    def synth_bars(code, start="2024-02-27", end="2026-11-13"):
        d = date.fromisoformat(start)
        end_d = date.fromisoformat(end)
        px = 10000.0
        seed = sum(ord(ch) for ch in code)
        out = []
        i = 0
        while d <= end_d:
            if d.weekday() < 5:
                # deterministic walk
                step = ((seed * 17 + i * 13) % 21 - 10) / 1000.0
                if code == "195930":
                    step += ((i % 7) - 3) / 2000.0
                px *= 1.0 + step
                out.append(Bar(d.isoformat(), round(px, 2)))
                i += 1
            d += timedelta(days=1)
        return out

    rc = run(
        "dry",
        datetime(2026, 11, 13, 15, 40, tzinfo=KST),
        cfg,
        pub=fake_pub,
        bars_fn=synth_bars,
        divs_fn=lambda: ({c: {} for c in ALL_CODES}, None),
    )
    check("dry returns 0", rc == 0)
    check("dry publishes nothing", published == [])
    # state file should remain as on disk (dry must not write). We do not assert contents here.

    # holiday week alignment in week_end_series: Thu 04-30 is last of week labeled 05-01
    days = ["2026-04-27", "2026-04-28", "2026-04-29", "2026-04-30"]
    lv = {d: float(i + 1) for i, d in enumerate(days)}
    rows = week_end_series(days, lv)
    check("holiday week labels Friday", rows[-1][0] == "2026-05-01" and rows[-1][1] == "2026-04-30")

    # config / Korean text hygiene
    raw = CFG_PATH.read_text(encoding="utf-8") + (STATE_PATH.read_text(encoding="utf-8") if STATE_PATH.exists() else "")
    check("config+state no forbidden", not FORBIDDEN.search(raw))
    for key in ("fire", "rearm", "window"):
        check(f"cfg has {key}", key in cfg or key == "window")
    check("fire is 0.60", float(cfg["fire"]) == 0.60)
    check("rearm is 0.50", float(cfg["rearm"]) == 0.50)
    check("window 26", int(cfg["window"]) == 26)

    n = 36  # printed roughly; actual count from fails
    print("selftest", "PASS" if not fails else f"FAIL {fails}", f"fails={fails}")
    return 1 if fails else 0


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    if mode == "selftest":
        return selftest()
    if mode not in {"auto", "dry"}:
        print(__doc__)
        return 2
    now = datetime.now(KST)
    return run(mode, now, cfg)


if __name__ == "__main__":
    raise SystemExit(main())
