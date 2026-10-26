#!/usr/bin/env python3
"""COIN(Coinbase) 관망 판단 변경 조건 알림 (change-only).

morning 슬롯(평일 09:00 KST). 미국 직전 완료 일봉 종가만 본다(진행 중인 봉은 제외).
- 관심 구간: COIN 완료 종가 <= 160달러. 재무장: 종가 > 170달러
- 관심 구간 이탈: 완료 종가 < 150달러. 재무장: 종가 > 158달러
- 기준 시나리오 상단: 완료 종가 >= 220달러. 재무장: 종가 < 205달러
- 실적(미국 10/29 장 마감 후): 10/28 첫 실행에서 D-1 안내 1회, 10/30 첫 실행부터 SEC 8-K(Item 2.02) 첨부 덱의
  "Financial Recap" 표에서 거래 매출(Transaction, net), 구독 및 서비스 매출, 조정 EBITDA, 주식보상비용을 읽어
  직전 분기와 대조한 결과 1회. 못 읽으면 수동 확인 안내를 1회 보내고, 창(post_window_days) 안에서 다음 실행 때 다시 읽는다.
modes: auto(기본) | dry(발송 없음, state 안 씀) | install(설치 확인 1회, state 안 씀)
       | earnings-dry [8-K 접수번호] (실적 판독만 출력, 발송과 state 없음. 번호를 주면 그 8-K로 시험)
"""
from __future__ import annotations

import html
import json
import re
import sys
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic  # noqa: E402

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore[assignment]

KST = timezone(timedelta(hours=9))
NAME = "coin"
CFG_PATH = ROOT / "configs" / f"{NAME}.json"
STATE_PATH = ROOT / "state" / f"{NAME}.json"
UA = {"User-Agent": "Mozilla/5.0 (cwally-alerts; +mailto:sosmos3218@gmail.com)"}
SEC_UA = {"User-Agent": "cwally-alerts sosmos3218@gmail.com"}


def ny_now() -> datetime:
    if ZoneInfo is not None:
        try:
            return datetime.now(ZoneInfo("America/New_York"))
        except Exception:  # noqa: BLE001
            pass
    off = -5 if datetime.now(timezone.utc).month in (11, 12, 1, 2, 3) else -4
    return datetime.now(timezone(timedelta(hours=off)))


def load_state() -> dict:
    base = {
        "updated_at": None,
        "buy_armed": True,
        "break_armed": True,
        "top_armed": True,
        "pre_sent": False,
        "post_sent": False,
        "post_parsed": False,
        "last_bar_date": None,
        "last_close": None,
        "fetch_fail_date": None,
    }
    if STATE_PATH.exists():
        try:
            base.update(json.loads(STATE_PATH.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass
    return base


def fetch_bars(symbol: str, now_ny: datetime) -> list[tuple[date, float]]:
    """완료 일봉 (미국 동부 날짜, 종가) 오름차순. 진행 중인 오늘 봉은 뺀다."""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=1y&interval=1d"
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=20) as resp:
        res = json.loads(resp.read().decode())["chart"]["result"][0]
    tz = now_ny.tzinfo
    out: dict[date, float] = {}
    for t, c in zip(res.get("timestamp") or [], res["indicators"]["quote"][0].get("close") or []):
        if c is None:
            continue
        out[datetime.fromtimestamp(t, tz).date()] = float(c)
    # 직전 거래일 일봉 close가 한동안 null로 오므로, 장 마감 뒤 찍힌 regularMarketPrice로 채운다
    meta = res.get("meta") or {}
    rmt, rmp = meta.get("regularMarketTime"), meta.get("regularMarketPrice")
    if rmt and rmp:
        mt = datetime.fromtimestamp(rmt, tz)
        if mt.date() not in out and mt.hour * 60 + mt.minute >= 16 * 60:
            out[mt.date()] = float(rmp)
    today = now_ny.date()
    after_close = now_ny.hour * 60 + now_ny.minute >= 16 * 60 + 10
    return sorted((d, c) for d, c in out.items() if d < today or (d == today and after_close))


def body(stamp: str, status: str, action: str, later: str, trim: str, avoid: str) -> str:
    return "\n".join(
        [
            f"{stamp} | COIN | Coinbase Global",
            "이게 뭐 알림: COIN 관망 판단 점검 알림(10/22 분석 기준). 관심 구간 160달러 이하, 150달러 이탈, 기준 시나리오 상단 220달러가 새로 켜질 때와 10/29 실적 전후 확인 시점에만 옴. 자동 주문 아님.",
            f"지금 결과/상태: {status}",
            f"지금 할 일: {action}",
            f"나중에: {later}",
            f"익절/줄일 때: {trim}",
            f"하지 말 것: {avoid}",
        ]
    )


TRIM = "종가 220달러 이상인데 거래 매출과 서비스 매출이 함께 늘지 않으면 비중을 덜어낼 기준부터 정하기. 150달러 아래 이탈이면 신규 매수는 멈추고 보유분만 점검."
AVOID = "실적 전에 예정 금액 전액 투입, 장중 급등 추격, 150달러 이탈 직후 물타기, 비트코인 가격만 보고 매수, 이 알림만 보고 주문."


def pct(a: float, b: float) -> float:
    return (a / b - 1) * 100


NUM = r"\(?-?[\d,]+\.\d\)?"


def edgar_get(url: str) -> bytes:
    req = urllib.request.Request(url, headers=SEC_UA)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def html_to_text(raw: str) -> str:
    t = re.sub(r"<script.*?</script>|<style.*?</style>", "", raw, flags=re.S)
    t = re.sub(r"</(p|div|tr|li|h\d|table)>", "\n", t)
    t = html.unescape(re.sub(r"<[^>]+>", " ", t))
    return re.sub(r"\s+", " ", t)


def _row(section: str, label: str) -> list[float]:
    """표의 한 행에서 라벨 뒤에 이어지는 소수 한 자리 숫자들을 모두 읽는다. 괄호는 음수."""
    m = re.search(label + r"\d?\s+((?:" + NUM + r"\s+)+)", section)
    if not m:
        raise ValueError(f"행 없음: {label}")
    vals = [-float(x.strip("()").replace(",", "")) if x.startswith("(") else float(x.replace(",", ""))
            for x in m.group(1).split()]
    if len(vals) < 2:
        raise ValueError(f"행 숫자 부족: {label}")
    return vals


def parse_deck(text: str) -> dict[str, tuple[float, float]]:
    """덱 본문에서 (최신 분기, 직전 분기) 값을 읽는다. 단위는 백만 달러."""
    i = text.find("Financial Recap")
    if i < 0:
        raise ValueError("Financial Recap 표 없음")
    j = text.find("Income Statement", i)
    recap = text[i:j if j > i else i + 4000]
    k = text.find("Reconciliation of Adjusted EBITDA")
    if k < 0:
        raise ValueError("Adjusted EBITDA 조정표 없음")
    recon = text[k:k + 6000]
    out = {}
    for key, section, label in (
        ("trading", recap, "Transaction, net"),
        ("subs", recap, "Subscription and services"),
        ("ebitda", recap, "Adjusted EBITDA"),
        ("sbc", recon, "Stock-based compensation expense"),
    ):
        v = _row(section, label)
        out[key] = (v[-1], v[-2])
    return out


def fetch_earnings(cfg: dict, acc_override: str | None = None) -> tuple[str, str, dict[str, tuple[float, float]]]:
    """SEC 8-K(Item 2.02)를 찾아 덱을 읽는다. (접수번호, 제출일, 값). 실패하면 예외."""
    ern, ed = cfg["earnings"], cfg["edgar"]
    cik = ed["cik"]
    if acc_override:
        acc, fdate = acc_override, "?"
    else:
        sub = json.loads(edgar_get(f"https://data.sec.gov/submissions/CIK{cik}.json"))["filings"]["recent"]
        lo = date.fromisoformat(ern["date_us"])
        hi = lo + timedelta(days=int(ed["search_days"]))
        hits = [(d, a) for f, d, a, it in zip(sub["form"], sub["filingDate"], sub["accessionNumber"], sub["items"])
                if f == "8-K" and ed["item"] in it.split(",") and lo <= date.fromisoformat(d) <= hi]
        if not hits:
            raise LookupError(f"{lo} 이후 {ed['search_days']}일 안에 8-K Item {ed['item']} 아직 없음")
        fdate, acc = max(hits)
    base_url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc.replace('-', '')}"
    items = json.loads(edgar_get(f"{base_url}/index.json"))["directory"]["item"]
    cands = sorted(
        (i for i in items if i["name"].lower().endswith(".htm") and not re.match(r"R\d+\.htm$", i["name"])
         and "index" not in i["name"].lower()),
        key=lambda i: -int(i.get("size") or 0),
    )[:3]
    last_err = "첨부 htm 없음"
    for it in cands:
        try:
            return acc, fdate, parse_deck(html_to_text(edgar_get(f"{base_url}/{it['name']}").decode("utf-8", "replace")))
        except ValueError as exc:
            last_err = f"{it['name']}: {exc}"
    raise ValueError(last_err)


def check_layout(parsed: dict[str, tuple[float, float]], base: dict, tol: float = 0.15) -> None:
    """표의 직전 분기 열이 설정의 2분기 기준값과 같은지 본다. 열이 밀리거나 행이 바뀌면 판독을 믿지 않는다."""
    pairs = {"trading": "trading_rev_musd", "subs": "subs_services_rev_musd", "ebitda": "adj_ebitda_musd", "sbc": "sbc_musd"}
    bad = [f"{k} 직전분기 {parsed[k][1]} != 기준 {base[b]}" for k, b in pairs.items() if abs(parsed[k][1] - base[b]) > tol]
    if bad:
        raise ValueError("표 구조 변경 의심: " + "; ".join(bad))


def earnings_alert(parsed: dict[str, tuple[float, float]], acc: str, fdate: str, status: str, stamp: str, earn_us: date) -> tuple[str, str, int, list[str]]:
    t, s_, e, b = (parsed[k] for k in ("trading", "subs", "ebitda", "sbc"))
    gap_cur, gap_prev = e[0] - b[0], e[1] - b[1]
    ok = {"거래 매출": t[0] > t[1], "서비스 매출": s_[0] > s_[1], "조정 EBITDA와 주식보상비용 차이": gap_cur > gap_prev}
    n = sum(ok.values())
    miss = [k for k, v in ok.items() if not v]

    def line(name: str, cur: float, prev: float) -> str:
        return f"{name} ${cur:,.1f}백만(직전 ${prev:,.1f}, {pct(cur, prev):+.1f}%)"

    nums = (f"{line('거래 매출', *t)}. {line('구독 및 서비스 매출', *s_)}. {line('조정 EBITDA', *e)}. {line('주식보상비용', *b)}. "
            f"조정 EBITDA에서 주식보상비용을 뺀 차이 {gap_cur:+,.1f}(직전 {gap_prev:+,.1f}, 구조 비교용이고 회계상 이익이 아님). "
            f"출처 SEC 8-K {acc}" + (f"({fdate} 제출)." if fdate != "?" else "."))
    if n == 3:
        title = "[COIN] 실적 숫자 3개 개선 | 가이던스 확인 뒤 분할 검토"
        action = ("남은 4번(다음 분기 전망, 비용 가이던스)을 보도자료와 콘퍼런스콜 요약에서 직접 확인. 같이 좋아졌으면 분석의 "
                  "'관망에서 분할 매수' 조건이 맞습니다. 미리 정한 한도 안에서 분할로만 하세요.")
    else:
        title = (f"[COIN] 실적 숫자 {n}/3 개선 | 관망 유지" if n else "[COIN] 실적 숫자 개선 없음 | 관망 유지")
        action = (f"관망 유지. 개선되지 않은 항목({', '.join(miss)})이 다음 분기에 돌아오는지 보세요. "
                  "4번(다음 분기 전망, 비용 가이던스)은 직접 확인.")
    msg = body(stamp, f"{earn_us:%m/%d} 미국 장 마감 후 3분기 실적을 자동 판독했습니다. {nums} {status}", action,
               "숫자 3개는 직전 분기와 단순 비교입니다. 점유율, USDC 잔액, 신사업은 이 판독에 없습니다. 가격 조건 알림은 계속 옵니다.",
               TRIM, AVOID)
    return title, msg, 4, ["bar_chart"]


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    now_ny = ny_now()
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    today_kst = now.date()
    topic = resolve_topic("port", cfg.get("ntfy_topic"))
    server = resolve_server(cfg.get("ntfy_server"))
    buy, brk, top, ern = cfg["buy_zone"], cfg["break_zone"], cfg["base_top"], cfg["earnings"]
    base = ern["q2_baseline"]

    end = cfg.get("ends_kst")
    if mode not in ("install", "earnings-dry") and end and now > datetime.strptime(end, "%Y-%m-%d %H:%M").replace(tzinfo=KST):
        print(f"ended at {end}; silent")
        return 0

    rows: list[tuple[date, float]] = []
    err = ""
    try:
        rows = fetch_bars(cfg["symbol"], now_ny)
        if len(rows) < 3:
            raise RuntimeError(f"COIN 완료 일봉 {len(rows)}개")
        if (now_ny.date() - rows[-1][0]).days > int(cfg["stale_days"]):
            raise RuntimeError(f"COIN 마지막 봉 {rows[-1][0]} 너무 오래됨")
    except Exception as exc:  # noqa: BLE001
        err = str(exc)
        rows = []

    last_d = rows[-1][0] if rows else None
    close = rows[-1][1] if rows else None
    prev = rows[-2][1] if len(rows) >= 2 else None
    hi52 = max(c for _, c in rows) if len(rows) >= 150 else None
    lo52 = min(c for _, c in rows) if len(rows) >= 150 else None
    if close is not None:
        status = f"COIN 종가 ${close:,.2f}({last_d:%m/%d} 미국)"
        if prev:
            status += f", 전일 대비 {pct(close, prev):+.2f}%"
        if hi52 and lo52:
            status += f", 종가 기준 52주 ${lo52:,.2f}~${hi52:,.2f}(고점 대비 {pct(close, hi52):+.1f}%)"
        status += f". 관심 구간(${brk['close_max']:.0f}~{buy['close_max']:.0f})까지 {pct(buy['close_max'], close):+.1f}%."
    else:
        status = "COIN 조회 실패"

    is_buy = bool(close is not None and close <= buy["close_max"])
    is_break = bool(close is not None and close < brk["close_max"])
    is_top = bool(close is not None and close >= top["close_min"])

    if mode == "install":
        flags = [
            "관심 구간 충족" if is_buy else "관심 구간 미충족",
            "150달러 이탈" if is_break else "150달러 이탈 아님",
            "220달러 도달" if is_top else "220달러 미도달",
        ]
        publish(
            topic,
            "[COIN] 설치 확인 | 매매 신호 아님",
            body(stamp, f"평일 09:00 KST morning 슬롯에 붙였습니다. {status} 지금 판정: {', '.join(flags)}.",
                 "설치 확인만 하세요. 이 알림으로 사거나 팔지 마세요.",
                 f"가격 조건은 새로 켜질 때만 옵니다. 실적 안내는 {ern['pre_date_kst'][5:]} 1회, {ern['post_date_kst'][5:]} 1회. 조회 실패는 cwally-ops로 갑니다.",
                 TRIM, "설치 확인을 매수나 매도 신호로 읽기."),
            3, ["information_source"], server,
        )
        print("install confirmation sent")
        return 0

    if mode == "earnings-dry":
        acc_arg = sys.argv[2] if len(sys.argv) > 2 else None
        acc, fdate, parsed = fetch_earnings(cfg, acc_arg)
        if not acc_arg:  # 번호를 준 시험 실행은 다른 분기 덱이라 기준값 대조를 건너뛴다
            check_layout(parsed, base)
        title, msg, _, _ = earnings_alert(parsed, acc, fdate, status, stamp, date.fromisoformat(ern["date_us"]))
        print(parsed)
        print(title)
        print(msg)
        print("DRY RUN: no publish, state not written")
        return 0

    dry = mode == "dry"
    state = load_state()
    alerts: list[tuple[str, str, int, list[str]]] = []
    print(f"close={close} prev={prev} hi52={hi52} lo52={lo52} buy={is_buy} break={is_break} top={is_top} err={err}")

    pre_d = date.fromisoformat(ern["pre_date_kst"])
    post_d = date.fromisoformat(ern["post_date_kst"])
    post_end = post_d + timedelta(days=int(ern["post_window_days"]))
    earn_us = date.fromisoformat(ern["date_us"])

    # 실적 안내는 가격 조회와 무관하게 날짜로 판단한다
    if not state.get("pre_sent") and pre_d <= today_kst < post_d:
        alerts.append((
            "[COIN] 내일 실적 | 전액 투입 보류",
            body(stamp, f"미국 {earn_us:%m/%d} 장 마감 후({ern['when']}) 3분기 실적이 나옵니다. {status}",
                 "예정 금액의 20~25%만 선행 진입하기로 했다면 그 선까지만. 나머지는 실적을 본 뒤 결정. 아직 안 샀다면 실적 뒤에 보셔도 됩니다.",
                 f"{post_d:%m/%d} 오전 알림에 확인 4항목(거래 매출, 서비스 매출, 조정 EBITDA와 주식보상비용, 다음 분기 전망)이 옵니다.",
                 TRIM, AVOID),
            3, ["calendar"],
        ))
        state["pre_sent"] = True
    if not state.get("post_parsed") and post_d <= today_kst < post_end:
        parse_err = ""
        try:
            acc, fdate, parsed = fetch_earnings(cfg)
            check_layout(parsed, base)
            alerts.append(earnings_alert(parsed, acc, fdate, status, stamp, earn_us))
            state["post_parsed"] = True
            state["post_sent"] = True
        except Exception as exc:  # noqa: BLE001
            parse_err = f"{type(exc).__name__}: {exc}"
            print(f"earnings parse failed: {parse_err}")
    else:
        parse_err = ""
    if not state.get("post_sent") and parse_err:
        alerts.append((
            "[COIN] 실적 나옴 | 4항목 직접 확인",
            body(stamp, f"{earn_us:%m/%d} 미국 장 마감 후 3분기 실적이 나왔습니다. 자동 판독은 못 했습니다({parse_err}). {status}",
                 (f"회사 IR({ern['ir_url']}) 보도자료에서 4가지를 2분기와 비교. "
                  f"1) 거래 매출 ${base['trading_rev_musd']}백만 달러보다 늘었나, "
                  f"2) 구독 및 서비스 매출 ${base['subs_services_rev_musd']}백만 달러보다 늘었나(비중이 아니라 금액), "
                  f"3) 조정 EBITDA ${base['adj_ebitda_musd']}백만 달러와 주식보상비용 ${base['sbc_musd']}백만 달러 구조가 나아졌나, "
                  "4) 다음 분기 전망과 비용 가이던스가 같이 좋아졌나."),
                 "1)과 2)가 함께 늘고 3)이 나아지면 관망에서 분할 매수로 바꾸는 조건입니다. 반대로 USDC 잔액과 점유율은 느는데 이익이 계속 줄면 비중을 더 낮게 보세요. 다음 실행 때 자동 판독을 다시 시도합니다.",
                 TRIM, AVOID),
            4, ["bar_chart"],
        ))
        state["post_sent"] = True

    if close is None:
        if state.get("fetch_fail_date") != today_kst.strftime("%Y%m%d"):
            alerts.append((
                "[COIN] 조회 실패 | 조건 미확인",
                body(stamp, f"{status}. {err}", "증권사 앱에서 COIN 종가를 직접 보세요.",
                     "다음 평일 09:00에 다시 봅니다.", "이번 알림으로 비중을 바꾸지 마세요.", "조회 실패를 조건 충족이나 정상으로 읽기."),
                3, ["warning"],
            ))
            state["fetch_fail_date"] = today_kst.strftime("%Y%m%d")
    else:
        if is_break and state.get("break_armed", True):
            alerts.append((
                "[COIN] 150달러 이탈 | 신규 매수 보류",
                body(stamp, status + f" 종가가 {brk['close_max']:.0f}달러(관심 구간 아래)로 내려왔습니다. 분석의 약세 시나리오(110~140달러) 쪽입니다.",
                     "신규 매수는 멈추세요. 실적 전망이 훼손됐는지(거래 매출, 이익률, 가이던스 뉴스)부터 확인.",
                     f"종가가 {brk['rearm_close']:.0f}달러 위로 돌아오면 이 조건이 다시 무장됩니다. 10/29 실적이 남아 있으면 그 결과를 먼저 보세요.",
                     TRIM, AVOID),
                4, ["chart_with_downwards_trend"],
            ))
            state["break_armed"] = False
        elif is_buy and state.get("buy_armed", True) and not is_break:
            alerts.append((
                "[COIN] 관심 구간 진입 | 분할 검토",
                body(stamp, status + f" 종가가 {buy['close_max']:.0f}달러(관심 구간 150~160) 이하로 들어왔습니다.",
                     "실적 전망이 그대로인지 먼저 확인. 이상 없으면 미리 정한 한도 안에서 분할로만 검토. 미국 정규장 지정가, 개장 직후 30분은 피하기.",
                     f"종가가 {buy['rearm_close']:.0f}달러 위로 올라가면 이 조건이 다시 무장됩니다. 150달러 아래로 내려가면 이탈 알림이 옵니다.",
                     TRIM, AVOID),
                4, ["chart_with_upwards_trend"],
            ))
            state["buy_armed"] = False
        if is_top and state.get("top_armed", True):
            alerts.append((
                "[COIN] 220달러 도달 | 추격 말고 점검",
                body(stamp, status + f" 종가가 {top['close_min']:.0f}달러(기준 시나리오 180~220 상단)에 닿았습니다. 더 오르려면 이익 회복 확인이 필요합니다.",
                     "추격 매수 대신 거래 매출, 서비스 매출, 조정 EBITDA가 같이 회복됐는지 확인. 정한 비중을 넘었으면 덜어낼 기준부터 정하기.",
                     f"종가가 {top['rearm_close']:.0f}달러 아래로 내려가면 이 조건이 다시 무장됩니다.",
                     TRIM, AVOID),
                4, ["trophy"],
            ))
            state["top_armed"] = False

        if not state.get("buy_armed", True) and not is_buy and close > buy["rearm_close"]:
            state["buy_armed"] = True
        if not state.get("break_armed", True) and not is_break and close > brk["rearm_close"]:
            state["break_armed"] = True
        if not state.get("top_armed", True) and not is_top and close < top["rearm_close"]:
            state["top_armed"] = True
        state.update(last_bar_date=last_d.isoformat(), last_close=close, fetch_fail_date=None)

    for title, msg, pri, tags in alerts:
        print(title)
        print(msg)
        if not dry:
            publish(topic, title, msg, pri, tags, server)
    if not alerts:
        print(f"no alert (buy={is_buy} break={is_break} top={is_top} pre_sent={state.get('pre_sent')} post_sent={state.get('post_sent')})")
    if dry:
        print("DRY RUN: no publish, state not written")
        return 0
    state["updated_at"] = now.isoformat()
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
