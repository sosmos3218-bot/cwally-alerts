#!/usr/bin/env python3
"""[VYMI] 분기 점검 (2026-10-19 chat 승인, 기존 morning 09:00 KST 슬롯).

1/4/7/10월 15일 이후 첫 평일에 분기 1회만 보낸다. 그달 안에서 아직 안 보냈으면
다음 평일에 따라잡기 발송. 상태는 발송했을 때만 state/vymi.json 에 쓴다.

modes:
  auto   스케줄 판단 후 발송 (morning 슬롯)
  dry    지표 계산 후 제목/본문만 출력. ntfy 없음, state 쓰기 없음. VYMI_NOW 로 날짜 흉내 가능.
  force  스케줄 무시하고 발송, 발송 분기 기록 (수동용, 워크플로에 연결 안 함)
  sim    python scripts/vymi_monitor.py sim YYYY-MM-DD [sent_quarter]
         스케줄 판단만 출력. 네트워크, ntfy, state 쓰기 모두 없음 (테스트용).
"""
from __future__ import annotations

import csv
import io
import json
import os
import sys
import time
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import append_ends, publish, resolve_server, resolve_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
CFG_PATH = ROOT / "configs" / "vymi.json"
STATE_PATH = ROOT / "state" / "vymi.json"
UA = {"User-Agent": "Mozilla/5.0 cwally-alerts"}
NA = "확인 불가"


# ---------- schedule ----------

def quarter_key(d: date) -> str:
    return f"{d.year}Q{(d.month - 1) // 3 + 1}"


def decide(today: date, sent_quarter: str | None, cfg: dict) -> tuple[bool, str, str]:
    """(send, quarter, reason). 순수 함수: 네트워크/파일 접근 없음."""
    q = quarter_key(today)
    months = cfg.get("quarter_months") or [1, 4, 7, 10]
    day = int(cfg.get("quarter_day") or 15)
    first = cfg.get("first_quarter")
    if first and q < first:
        return False, q, f"before first_quarter {first}"
    if today.month not in months:
        return False, q, "not a quarter-start month"
    if today.day < day:
        return False, q, f"before day {day}"
    if today.weekday() >= 5:
        return False, q, "weekend"
    if sent_quarter == q:
        return False, q, f"already sent {q}"
    return True, q, "first weekday on/after 15th" if _is_first_weekday(today, day) else "catch-up (not sent yet this quarter)"


def _is_first_weekday(today: date, day: int) -> bool:
    d = today.replace(day=day)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d == today


def next_send_date(after: date, cfg: dict) -> date:
    months = cfg.get("quarter_months") or [1, 4, 7, 10]
    day = int(cfg.get("quarter_day") or 15)
    y, m = after.year, after.month
    for _ in range(16):
        m += 1
        if m > 12:
            y, m = y + 1, 1
        if m in months:
            d = date(y, m, day)
            while d.weekday() >= 5:
                d += timedelta(days=1)
            return d
    return after


# ---------- io ----------

def load_json(path: Path, fallback: dict) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            out = dict(fallback)
            out.update(data)
            return out
    except Exception:
        pass
    return dict(fallback)


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def http_get(url: str, headers: dict | None = None, timeout: int = 25) -> bytes:
    last = None
    for attempt in range(2):
        try:
            req = urllib.request.Request(url, headers=headers or UA)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except Exception as exc:  # retry once
            last = exc
            time.sleep(1.5)
    raise last  # type: ignore[misc]


# ---------- prices ----------

def yahoo_series(symbol: str, rng: str = "2y") -> dict:
    """{'dates': [date], 'close': [float], 'adj': [float], 'divs': [(date, amount)], 'source': str}"""
    errs = []
    for host in ("query1", "query2"):
        url = f"https://{host}.finance.yahoo.com/v8/finance/chart/{symbol}?range={rng}&interval=1d&events=div"
        try:
            data = json.loads(http_get(url).decode())
            res = data["chart"]["result"][0]
            ts = res["timestamp"]
            q = res["indicators"]["quote"][0]["close"]
            adj = (res["indicators"].get("adjclose") or [{}])[0].get("adjclose") or q
            dates, close, adjc = [], [], []
            for t, c, a in zip(ts, q, adj):
                if c is None:
                    continue
                dates.append(datetime.fromtimestamp(t, KST).date())
                close.append(float(c))
                adjc.append(float(a if a is not None else c))
            divs = []
            for v in ((res.get("events") or {}).get("dividends") or {}).values():
                try:
                    divs.append((datetime.fromtimestamp(int(v["date"]), timezone.utc).date(), float(v["amount"])))
                except Exception:
                    continue
            divs.sort()
            if len(close) < 30:
                raise ValueError(f"too few rows {len(close)}")
            return {"dates": dates, "close": close, "adj": adjc, "divs": divs, "source": f"Yahoo {host}"}
        except Exception as exc:
            errs.append(f"{host}: {exc}")
    # stooq fallback: 종가만 (배당 없음)
    try:
        raw = http_get(f"https://stooq.com/q/d/l/?s={symbol.lower()}.us&i=d").decode("utf-8", "replace")
        rows = list(csv.DictReader(io.StringIO(raw)))
        dates, close = [], []
        for r in rows:
            try:
                dates.append(date.fromisoformat(r["Date"]))
                close.append(float(r["Close"]))
            except Exception:
                continue
        if len(close) < 30:
            raise ValueError("stooq rows missing (blocked or empty)")
        return {"dates": dates, "close": close, "adj": close, "divs": [], "source": "stooq (배당 없음)"}
    except Exception as exc:
        errs.append(f"stooq: {exc}")
    raise RuntimeError("; ".join(errs))


def value_on_or_before(s: dict, d: date, key: str = "adj") -> float | None:
    val = None
    for dd, v in zip(s["dates"], s[key]):
        if dd <= d:
            val = v
        else:
            break
    return val


def ret_pct(s: dict, days: int) -> float | None:
    end = s["dates"][-1]
    base = value_on_or_before(s, end - timedelta(days=days))
    if not base:
        return None
    return (s["adj"][-1] / base - 1) * 100


def ttm_div(divs: list, end: date) -> float:
    return sum(a for d, a in divs if end - timedelta(days=365) < d <= end)


# ---------- SEC provisions ----------

def provision_change(facts: dict, concepts: list[str], today: date, stale_days: int) -> dict:
    best = None
    for spec in concepts:
        sign = -1 if spec.startswith("-") else 1
        tax, name = spec.lstrip("-").split(":", 1)
        node = (facts.get(tax) or {}).get(name)
        if not node:
            continue
        for unit, arr in node.get("units", {}).items():
            rows = {}
            for a in arr:
                if not a.get("start") or not a.get("end"):
                    continue
                try:
                    s, e = date.fromisoformat(a["start"]), date.fromisoformat(a["end"])
                except ValueError:
                    continue
                rows[(s, e)] = sign * float(a["val"])
            if not rows:
                continue
            latest_end = max(e for _, e in rows)
            if best is None or latest_end > best["latest_end"]:
                best = {"latest_end": latest_end, "rows": rows, "unit": unit, "concept": name}
    if not best:
        return {"ok": False, "why": "공시 항목 없음"}
    if (today - best["latest_end"]).days > stale_days:
        return {"ok": False, "why": f"최신 공시 {best['latest_end']}, 오래됨"}
    rows = best["rows"]
    end = best["latest_end"]
    cands = sorted(((e - s).days, s, e) for (s, e) in rows if e == end)
    for dur, s, e in cands:  # 짧은 기간(분기)부터
        for (ps, pe), pv in rows.items():
            if abs((pe - (e - timedelta(days=365))).days) <= 20 and abs((pe - ps).days - dur) <= 20:
                cur = rows[(s, e)]
                chg = (cur / pv - 1) * 100 if pv else None
                label = "분기" if dur < 120 else ("반기" if dur < 220 else ("9개월" if dur < 300 else "연간"))
                return {"ok": True, "end": e, "label": label, "cur": cur, "prev": pv, "chg": chg, "unit": best["unit"], "concept": best["concept"]}
    return {"ok": False, "why": f"전년 같은 기간 비교값 없음 (최신 {end})"}


def fmt_money(v: float, unit: str) -> str:
    a = abs(v)
    if unit == "JPY":
        return f"{v/1e12:,.2f}조엔" if a >= 1e12 else f"{v/1e8:,.0f}억엔"
    return f"{v/1e9:,.2f}B {unit}" if a >= 1e9 else f"{v/1e6:,.0f}M {unit}"


def fetch_weights(cfg: dict) -> tuple[dict, str | None]:
    try:
        data = json.loads(http_get(cfg["holdings_source"], {"User-Agent": "Mozilla/5.0"}).decode())
        ents = data["fund"]["entity"]
        asof = (data.get("asOfDate") or "")[:10] or None
        return {e.get("isin"): float(e.get("percentWeight")) for e in ents if e.get("percentWeight") and e.get("isin")}, asof
    except Exception as exc:
        print(f"vanguard holdings fetch fail: {exc}", file=sys.stderr)
        return {}, None


# ---------- build ----------

def pct(v: float | None, signed: bool = True) -> str:
    if v is None:
        return NA
    return f"{v:+.1f}%" if signed else f"{v:.1f}%"


def build(cfg: dict, now: datetime, quarter: str) -> dict:
    today = now.date()
    rules = cfg.get("rules") or {}
    srcs: list[str] = []
    warn: list[str] = []
    got: dict[str, str] = {}

    # 1) drawdown + 2) dividend
    vymi = None
    try:
        vymi = yahoo_series(cfg.get("ticker", "VYMI"), "10y")
        srcs.append(f"가격={vymi['source']}")
    except Exception as exc:
        print(f"VYMI price fail: {exc}", file=sys.stderr)

    if vymi:
        last_d, last = vymi["dates"][-1], vymi["close"][-1]
        win = [c for d, c in zip(vymi["dates"], vymi["close"]) if d > last_d - timedelta(days=365)]
        h52 = max(win)
        hall = max(vymi["close"])
        dd52 = (last / h52 - 1) * 100
        ddall = (last / hall - 1) * 100
        line1 = f"1) 가격: {last_d} 종가 ${last:.2f}, 52주 고점 ${h52:.2f} 대비 {dd52:+.1f}%, 10년 고점 ${hall:.2f} 대비 {ddall:+.1f}%."
        got["drawdown"] = vymi["source"]
        if -dd52 >= float(rules.get("drawdown_warn_pct", 15)):
            warn.append("가격 급락")
    else:
        line1 = f"1) 가격: {NA} (가격 조회 실패)."
        dd52 = None

    div_yoy = None
    if vymi and vymi["divs"]:
        divs = vymi["divs"]
        ld, la = divs[-1]
        prev = divs[-2][1] if len(divs) >= 2 else None
        yago = None
        for d, a in divs[:-1]:
            if abs((d - (ld - timedelta(days=365))).days) <= 45:
                yago = a
        qoq = (la / prev - 1) * 100 if prev else None
        div_yoy = (la / yago - 1) * 100 if yago else None
        end = vymi["dates"][-1]
        y_now = ttm_div(divs, end) / vymi["close"][-1] * 100
        qago = end - timedelta(days=91)
        px_qago = value_on_or_before(vymi, qago, "close")
        y_qago = ttm_div(divs, qago) / px_qago * 100 if px_qago else None
        line2 = (
            f"2) 배당: 최근 분배금 ${la:.3f} ({ld}), 직전 분기 대비 {pct(qoq)}, 1년 전 같은 분기 대비 {pct(div_yoy)}. "
            f"12개월 배당수익률 {y_now:.2f}% (한 분기 전 {f'{y_qago:.2f}%' if y_qago else NA})."
        )
        got["dividend"] = vymi["source"]
        if div_yoy is not None and div_yoy <= float(rules.get("dividend_yoy_warn_pct", -10)):
            warn.append("배당 감소")
    else:
        line2 = f"2) 배당: {NA} (배당 기록 조회 실패)."

    # 3) provisions
    weights, w_asof = fetch_weights(cfg)
    if weights:
        srcs.append(f"보유비중=Vanguard {w_asof or ''}".strip())
    ua = {"User-Agent": cfg.get("sec_user_agent") or "cwally-alerts sosmos3218@gmail.com", "Accept-Encoding": "identity"}
    stale = int(cfg.get("provision_stale_days") or 400)
    up_th = float(rules.get("provision_up_pct", 20))
    parts, ups, downs, okn = [], 0, 0, 0
    for h in cfg.get("financial_holdings") or []:
        w = weights.get(h.get("isin")) or h.get("weight_pct")
        nm = f"{h['name']}({w:.2f}%)" if w else h["name"]
        try:
            facts = json.loads(http_get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{h['cik']}.json", ua, 40).decode()).get("facts") or {}
            r = provision_change(facts, h.get("concepts") or [], today, stale)
        except Exception as exc:
            print(f"SEC {h['name']} fail: {exc}", file=sys.stderr)
            r = {"ok": False, "why": "SEC 조회 실패"}
        time.sleep(0.2)
        if r["ok"]:
            okn += 1
            if r["chg"] is not None and r["chg"] >= up_th:
                ups += 1
            elif r["chg"] is not None and r["chg"] <= -up_th:
                downs += 1
            parts.append(f"{nm} {r['label']}({r['end']}) {fmt_money(r['cur'], r['unit'])}, 전년 대비 {pct(r['chg'])}")
        else:
            parts.append(f"{nm} {NA} ({r['why']})")
    if okn:
        got["provisions"] = f"SEC EDGAR XBRL ({okn}/{len(parts)}곳)"
        srcs.append("충당금=SEC EDGAR XBRL")
    if okn and ups * 2 >= okn and ups > downs:
        prov_dir = "늘어나는 쪽"
        warn.append("충당금 증가")
    elif okn and downs * 2 >= okn and downs > ups:
        prov_dir = "줄어드는 쪽"
    elif okn:
        prov_dir = "섞여 있음"
    else:
        prov_dir = None
    line3 = "3) 금융 보유주 대손충당금(전년 같은 기간 대비, 20% 넘게 변하면 의미 있음): " + " / ".join(parts) + "."
    if prov_dir:
        line3 += f" 방향: {prov_dir} (늘어남 {ups}, 줄어듦 {downs}, 확인 {okn}곳)."

    # 4) relative performance
    bm = cfg.get("benchmarks") or {}
    rets: dict[str, tuple[float | None, float | None]] = {}
    if vymi:
        rets["VYMI"] = (ret_pct(vymi, 91), ret_pct(vymi, 365))
    for key in ("financials", "broad_ex_us"):
        sym = bm.get(key)
        if not sym:
            continue
        try:
            s = yahoo_series(sym, "2y")
            rets[sym] = (ret_pct(s, 91), ret_pct(s, 365))
        except Exception as exc:
            print(f"{sym} fail: {exc}", file=sys.stderr)
            rets[sym] = (None, None)
    fin, broad = bm.get("financials", "IXG"), bm.get("broad_ex_us", "VXUS")

    def spread(a: str, b: str, i: int) -> float | None:
        x, y = rets.get(a, (None, None))[i], rets.get(b, (None, None))[i]
        return None if x is None or y is None else x - y

    def pp(v: float | None) -> str:
        return NA if v is None else f"{v:+.1f}%p"

    ret_txt = ", ".join(f"{k} 3개월 {pct(v[0])}/12개월 {pct(v[1])}" for k, v in rets.items())
    line4 = (
        f"4) 상대 성과(배당 포함): {ret_txt}. VYMI-{broad} 3개월 {pp(spread('VYMI', broad, 0))}, 12개월 {pp(spread('VYMI', broad, 1))}; "
        f"금융({fin})-{broad} 3개월 {pp(spread(fin, broad, 0))}, 12개월 {pp(spread(fin, broad, 1))}."
    )
    if any(v[0] is not None for v in rets.values()):
        got["relative"] = "Yahoo"
    rel3 = spread("VYMI", broad, 0)
    if rel3 is not None and rel3 <= float(rules.get("relative_3m_warn_pp", -5)):
        warn.append("상대 약세")

    # dividend sustainability comment (rule-based)
    if div_yoy is None and dd52 is None:
        comment = "배당 지속성: 데이터 부족으로 판단 보류."
    elif not warn:
        comment = "배당 지속성: 분배금 흐름, 가격, 은행 충당금 모두 큰 경고 없음. 배당 유지 쪽."
    elif len(warn) == 1:
        comment = f"배당 지속성: 경고 1개({warn[0]}). 당장 배당이 꺾인다고 보기는 이르다. 다음 분기에 같은 경고가 이어지는지 확인."
    else:
        comment = f"배당 지속성: 경고 {len(warn)}개({', '.join(warn)}). 배당 감소 위험이 커지는 조합. 비중 점검 필요."

    if vymi is None:
        verdict, todo, prio = "가격 데이터 없음, 판단 보류", "판단 보류. 다음 평일 재시도 결과를 기다릴 것.", 3
    elif not warn:
        verdict, todo, prio = "배당 흐름 유지, 보유 유지", "보유 유지. 정해 둔 적립식 계획대로.", 3
    elif len(warn) == 1:
        verdict, todo, prio = f"경고 1개({warn[0]}), 추가매수 속도 점검", "보유 유지. 추가매수는 계획 범위 안에서만, 속도를 높이지 말 것.", 3
    else:
        verdict, todo, prio = f"경고 {len(warn)}개, 비중 점검", "신규 추가매수 멈추고 전체 포트에서 VYMI 비중이 계획보다 큰지 확인.", 4

    nxt = next_send_date(today, cfg)
    title = f"{cfg.get('title_prefix', '[VYMI]')} 분기 점검 {quarter} | {verdict}"
    body = "\n".join([
        f"{now.strftime('%Y-%m-%d %H:%M KST')} | VYMI 분기 점검 {quarter}",
        f"이게 뭐 알림: {cfg.get('display', 'VYMI')} 분기 1회 점검. 가격 하락폭, 배당, 금융주 대손충당금, 상대 성과 4가지 요약. 투자권유 아님, 매매 신호 아님.",
        "지금 결과:",
        line1,
        line2,
        line3,
        line4,
        comment,
        f"지금 할 일: {todo}",
        f"나중에: 다음 점검 {nxt} 09:00 KST 전후. 분배금 발표(3/6/9/12월 하순) 뒤 숫자가 바뀜.",
        "익절/줄일 때: 배당이 2분기 연속 전년 대비 10% 넘게 줄고 충당금이 함께 늘 때 비중 축소 검토.",
        "하지 말 것: 한 분기 숫자만 보고 전량 매도 또는 몰빵 추가매수 하지 말 것. 신용/미수 금지.",
        f"출처: {', '.join(srcs) if srcs else '없음'}.",
    ])
    return {"title": title, "body": body, "priority": prio, "price_ok": vymi is not None, "got": got, "warn": warn}


def main() -> int:
    args = sys.argv[1:]
    mode = (args[0] if args else "auto").lower()
    if mode not in {"auto", "dry", "force", "sim"}:
        print(f"unknown mode {mode}")
        return 2
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    state = load_json(STATE_PATH, {"sent_quarter": None, "sent_at": None})

    if mode == "sim":  # 스케줄 판단만. 네트워크/ntfy/state 없음.
        d = date.fromisoformat(args[1])
        sent = args[2] if len(args) > 2 else None
        send, q, why = decide(d, sent, cfg)
        print(f"SIM {d} ({d.strftime('%a')}) sent_quarter={sent} -> {'SEND' if send else 'silent'} {q} ({why})")
        return 0

    now = datetime.now(KST)
    if mode == "dry" and os.environ.get("VYMI_NOW"):
        now = datetime.fromisoformat(os.environ["VYMI_NOW"]).replace(tzinfo=KST)
    send, quarter, why = decide(now.date(), state.get("sent_quarter"), cfg)

    if mode == "auto" and not send:
        print(f"vymi silent; {why} (next {next_send_date(now.date(), cfg)} 09:00 KST)")
        return 0

    out = build(cfg, now, quarter)

    if mode == "dry":
        print("DRY RUN (ntfy 없음, state 쓰기 없음)")
        print(f"schedule: {now.date()} -> {'SEND' if send else 'silent'} {quarter} ({why})")
        print(f"priority: {out['priority']}  data: {out['got']}  warn: {out['warn']}")
        print(out["title"])
        print(append_ends(out["body"], "vymi"))
        return 0

    topic = resolve_topic("port", cfg.get("ntfy_topic"))
    server = resolve_server(cfg.get("ntfy_server"))
    if not out["price_ok"]:
        # 가격 전부 실패: 매매 신호가 아니므로 cwally-ops (route_topic 이 '조회 실패' 제목을 ops로 보냄).
        # 분기당 1번만 알리고, 발송 분기는 기록하지 않아 다음 평일에 다시 시도한다.
        if state.get("fail_notice_quarter") != quarter:
            publish(topic, f"[VYMI] 가격 조회 실패 | 분기 점검 {quarter} 다음 평일 재시도", out["body"], 3, ["warning"], server)
            state["fail_notice_quarter"] = quarter
            save_state(state)
        print("vymi price fetch failed; will retry next weekday")
        return 1

    publish(topic, out["title"], out["body"], out["priority"], ["moneybag"], server)
    state.update({"sent_quarter": quarter, "sent_at": now.strftime("%Y-%m-%d %H:%M KST"), "last_title": out["title"]})
    save_state(state)
    print(out["title"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
