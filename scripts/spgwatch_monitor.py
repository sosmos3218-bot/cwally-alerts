#!/usr/bin/env python3
"""[SPG] Simon Property Group watch (approved in chat 2026-11-05 pt4u).

Morning 09:00 KST (existing US after-close slot). No new cron. Separate from
configs/spg.json ([SPG] limit 150/160) and vtrspg ([VTRSPG]).

  a) New SEC 8-K Item 2.02 (CIK 0001063761) once per accession. Best-effort parse
     of Real Estate FFO/share, 2026 FFO guidance range, Malls & Outlets occupancy.
     Unreliable parse -> link + headline only. Also any later 2.02.
  b) Close > 238.50 or close < 205 once per crossing; 1% buffer re-arm.
  c) US10Y <= 4.50% once; re-arm above 4.60%. Title includes 'SPG / US10Y'.
  d) US trading-day after-close daily summary: close, day %, distance to 238.50
     and 205, P/FFO at guidance midpoint (13.25 until a parsed update).

Modes: auto | dry (no ntfy, no state) | install (one confirmation, no state) | selftest
Not investment advice. Korean text: no middle dot (·) or em-dash (—).
"""
from __future__ import annotations

import json
import re
import sys
import urllib.request
from datetime import date, datetime, timedelta, timezone
from html import unescape
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic, route_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
NY = ZoneInfo("America/New_York")
NAME = "spgwatch"
CFG_PATH = ROOT / "configs" / f"{NAME}.json"
STATE_PATH = ROOT / "state" / f"{NAME}.json"
FX = Path(__file__).resolve().parent / "fixtures" / NAME
TAG = "[SPG]"
TICKER = "SPG"
BANNED = ("\u00b7", "\u2014")
UA_YAHOO = {"User-Agent": "Mozilla/5.0 cwally-alerts", "Accept": "application/json"}


def default_state() -> dict:
    return {
        "updated_at": None,
        "price_high_armed": True,
        "price_low_armed": True,
        "rate_low_armed": True,
        "y10_last_date": None,
        "price_last_date": None,
        "last_summary_date": None,
        "ffo_midpoint": 13.25,
        "ffo_midpoint_source": "config",
        "seen_accessions": [],
        "fail": {},
    }


def load_cfg() -> dict:
    return json.loads(CFG_PATH.read_text(encoding="utf-8"))


def load_state() -> dict:
    st = default_state()
    if STATE_PATH.exists():
        try:
            st.update(json.loads(STATE_PATH.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass
    return st


def save_state(st: dict, dry: bool) -> None:
    if dry:
        print("dry: state not saved")
        return
    st["updated_at"] = datetime.now(KST).isoformat(timespec="seconds")
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def http_text(url: str, headers: dict | None = None, timeout: int = 25) -> str:
    req = urllib.request.Request(url, headers=headers or UA_YAHOO)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def http_json(url: str, headers: dict | None = None, timeout: int = 25) -> dict:
    return json.loads(http_text(url, headers, timeout))


# ---------- price / yield ----------

def parse_chart(res: dict, now_ny: datetime) -> list[tuple[str, float]]:
    out: dict[str, float] = {}
    quote = (res.get("indicators") or {}).get("quote") or [{}]
    closes = (quote[0] or {}).get("close") or []
    for t, c in zip(res.get("timestamp") or [], closes):
        if c is None:
            continue
        d = datetime.fromtimestamp(t, NY).date().isoformat()
        out[d] = float(c)
    meta = res.get("meta") or {}
    rmt, rmp = meta.get("regularMarketTime"), meta.get("regularMarketPrice")
    if rmt and rmp is not None:
        mt = datetime.fromtimestamp(rmt, NY)
        if mt.date().isoformat() not in out and mt.hour * 60 + mt.minute >= 16 * 60:
            out[mt.date().isoformat()] = float(rmp)
    today = now_ny.date()
    after_close = now_ny.hour * 60 + now_ny.minute >= 16 * 60 + 10
    bars = sorted(
        (d, c) for d, c in out.items() if date.fromisoformat(d) < today or (date.fromisoformat(d) == today and after_close)
    )
    return bars


def fetch_bars(cfg: dict, now_ny: datetime) -> list[tuple[str, float]]:
    errs = []
    for host in ("query1", "query2"):
        url = f"https://{host}.finance.yahoo.com/v8/finance/chart/SPG?interval=1d&range=3mo"
        try:
            bars = parse_chart(http_json(url)["chart"]["result"][0], now_ny)
            if bars:
                return bars
            errs.append(f"{host}: empty")
        except Exception as exc:  # noqa: BLE001
            errs.append(f"{host}: {exc}")
    raise RuntimeError("; ".join(errs))


def fred_dgs10(start: date) -> list[tuple[str, float]]:
    txt = http_text(
        f"https://fred.stlouisfed.org/graph/fredgraph.csv?id=DGS10&cosd={start.isoformat()}",
        timeout=20,
    )
    rows = [ln.split(",") for ln in txt.strip().splitlines()]
    if not rows or "DGS10" not in rows[0][-1]:
        raise RuntimeError("FRED csv 형식 다름")
    out = []
    for r in rows[1:]:
        try:
            out.append((r[0], float(r[1])))
        except (ValueError, IndexError):
            continue
    if not out:
        raise RuntimeError("FRED DGS10 값 없음")
    return out


def y10_series(today: date) -> list[tuple[str, float]]:
    start = today - timedelta(days=21)
    try:
        return fred_dgs10(start)
    except Exception as exc:  # noqa: BLE001
        print(f"FRED DGS10 fail, Treasury fallback: {exc}", file=sys.stderr)
        try:
            from usbond_monitor import treasury_10y

            return sorted(treasury_10y(start, today).items())
        except Exception as exc2:  # noqa: BLE001
            print(f"Treasury fail, Yahoo ^TNX fallback: {exc2}", file=sys.stderr)
            res = http_json(
                "https://query1.finance.yahoo.com/v8/finance/chart/%5ETNX?interval=1d&range=1mo"
            )["chart"]["result"][0]
            bars = parse_chart(res, datetime.now(NY))
            if not bars:
                raise RuntimeError("US10Y 소스 전부 실패") from exc2
            return bars


# ---------- EDGAR ----------

def edgar_get(cfg: dict, url: str) -> str:
    hdr = {"User-Agent": cfg["edgar"]["user_agent"], "Accept-Encoding": "identity"}
    return http_text(url, hdr, timeout=30)


def html_text(h: str) -> str:
    t = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", h or "", flags=re.S | re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    return re.sub(r"\s+", " ", unescape(t)).strip()


def recent_filings(sub: dict) -> list[dict]:
    r = sub["filings"]["recent"]
    items = r.get("items") or [""] * len(r["form"])
    return [
        {
            "form": r["form"][i],
            "date": r["filingDate"][i],
            "acc": r["accessionNumber"][i],
            "items": items[i] or "",
            "doc": r["primaryDocument"][i],
        }
        for i in range(len(r["form"]))
    ]


def pick_new_202(filings: list[dict], seen: set[str], min_date: str | None = None) -> list[dict]:
    out = []
    for f in sorted(filings, key=lambda x: (x["date"], x["acc"])):
        items = [x.strip() for x in (f["items"] or "").split(",") if x.strip()]
        if f["form"] not in ("8-K", "8-K/A") or "2.02" not in items:
            continue
        if f["acc"] in seen:
            continue
        if min_date and f["date"] < min_date:
            continue
        out.append(f)
    return out


def exhibit_name(index_json: dict) -> str | None:
    names = [i["name"] for i in index_json["directory"]["item"]]
    for n in names:
        if re.search(r"(ex|exhibit)[-_]?99[-_.]?1?", n, re.I) and n.lower().endswith((".htm", ".html", ".txt")):
            return n
    return None


def filing_urls(cfg: dict, f: dict) -> tuple[str, str, str]:
    base = f"{cfg['edgar']['archive_base']}/{f['acc'].replace('-', '')}"
    return base, f"{base}/{f['acc']}-index.html", f"{base}/{f['doc']}"


def parse_earnings_metrics(text: str) -> dict:
    """Best-effort. Returns only keys that look reliable; empty dict if nothing solid."""
    out: dict = {}
    if not text:
        return out
    # guidance range near 2026 Real Estate FFO
    m = re.search(
        r"(?:full[- ]year|full year|outlook for|guidance for)[^\.]{0,80}?"
        r"2026[^\.]{0,80}?Real Estate FFO[^\.]{0,80}?"
        r"\$\s*(\d+\.\d{2})\s*(?:to|-)\s*\$?\s*(\d+\.\d{2})",
        text,
        re.I,
    )
    if not m:
        m = re.search(
            r"Real Estate FFO per diluted share to \$\s*(\d+\.\d{2})\s*(?:to|-)\s*\$?\s*(\d+\.\d{2})",
            text,
            re.I,
        )
    if m:
        lo, hi = float(m.group(1)), float(m.group(2))
        if 5.0 < lo < hi < 30.0:
            out["guidance_low"] = lo
            out["guidance_high"] = hi
            out["guidance_mid"] = round((lo + hi) / 2, 4)

    # quarterly RE FFO per share: prefer "Basic and Diluted Real Estate FFO per Share $ X.XX"
    m = re.search(
        r"Basic and Diluted Real Estate FFO per Share\s*\$\s*(\d+\.\d{2})",
        text,
        re.I,
    )
    if not m:
        m = re.search(
            r"Real Estate FFO per share\s*\(1\)\s*\$\s*(\d+\.\d{2})",
            text,
            re.I,
        )
    if not m:
        # avoid matching the annual guidance $13.xx: require value under 8
        for cand in re.finditer(r"Real Estate FFO per share[^\d$]{0,40}\$\s*(\d+\.\d{2})", text, re.I):
            v = float(cand.group(1))
            if 1.0 <= v <= 8.0:
                m = cand
                break
    if m:
        v = float(m.group(1))
        if 1.0 <= v <= 8.0:
            out["re_ffo_ps"] = v

    # occupancy near Malls / Premium Outlets (dates contain digits, so use .{0,80}?)
    m = re.search(
        r"Malls and Premium Outlets.{0,220}?Occupancy at.{0,80}?(\d{2}\.\d)\s*%",
        text,
        re.I | re.S,
    )
    if not m:
        m = re.search(
            r"Occupancy at.{0,80}?(\d{2}\.\d)\s*%.{0,80}?(?:unchanged|Malls|Outlets)",
            text,
            re.I | re.S,
        )
    if not m:
        m = re.search(
            r"Occupancy at.{0,80}?(\d{2}\.\d)\s*%",
            text,
            re.I | re.S,
        )
    if m:
        occ = float(m.group(1))
        if 80.0 <= occ <= 100.0:
            out["occupancy"] = occ

    # headline: first ~120 chars of press-like sentence
    hm = re.search(
        r"(Simon[^\.]{0,20}Reports[^\.]{0,100})",
        text,
        re.I,
    )
    if hm:
        out["headline"] = re.sub(r"\s+", " ", hm.group(1)).strip()[:160]
    return out


def inspect_filing(cfg: dict, f: dict, get=None) -> dict:
    get = get or (lambda u: edgar_get(cfg, u))
    base, index_url, main_url = filing_urls(cfg, f)
    out: dict = {
        "index_url": index_url,
        "main_url": main_url,
        "ex_url": None,
        "metrics": {},
        "note": "",
        "headline": f"8-K Item 2.02 ({f['date']})",
    }
    texts = []
    try:
        texts.append(html_text(get(main_url)))
    except Exception as exc:  # noqa: BLE001
        out["note"] += f"8-K 본문 조회 실패({type(exc).__name__}). "
    try:
        ex = exhibit_name(json.loads(get(f"{base}/index.json")))
        if ex:
            out["ex_url"] = f"{base}/{ex}"
            texts.append(html_text(get(out["ex_url"])))
        else:
            out["note"] += "Exhibit 99.1 파일 못 찾음. "
    except Exception as exc:  # noqa: BLE001
        out["note"] += f"Exhibit 99.1 조회 실패({type(exc).__name__}). "
    blob = " ".join(texts)
    metrics = parse_earnings_metrics(blob)
    out["metrics"] = metrics
    if metrics.get("headline"):
        out["headline"] = metrics["headline"]
    # reliability: need at least one of the three target numbers
    keys = ("re_ffo_ps", "guidance_low", "occupancy")
    if not any(k in metrics for k in keys):
        out["note"] += "숫자 파싱 불안정, 링크와 헤드라인만 전달. "
        out["metrics"] = {k: metrics[k] for k in ("headline",) if k in metrics}
    return out


# ---------- messages ----------

def body(stamp: str, status: str, action: str, later: str = "", avoid: str = "") -> str:
    lines = [
        f"{stamp} | {TICKER} | Simon Property Group",
        "이게 뭐 알림: " + load_cfg().get("explain", "SPG 감시 알림."),
        f"지금 결과/상태: {status}",
        f"지금 할 일: {action}",
        f"나중에: {later or '같은 조건은 재무장 선을 넘었다가 다시 와야 알림이 옵니다.'}",
        "익절/줄일 때: 계획한 비중이 있으면 그 안에서만. 없으면 새로 비중을 정한 뒤.",
        f"하지 말 것: {avoid or '조회 실패를 신호로 읽기, 이 알림만 보고 주문, 신용/미수.'}",
    ]
    return "\n".join(lines)


def earnings_alert(cfg: dict, f: dict, info: dict, stamp: str) -> tuple[str, str, int]:
    m = info.get("metrics") or {}
    parts = []
    if "re_ffo_ps" in m:
        parts.append(f"RE FFO/주 ${m['re_ffo_ps']:.2f}")
    if "guidance_low" in m and "guidance_high" in m:
        parts.append(f"2026 FFO 가이던스 ${m['guidance_low']:.2f}-${m['guidance_high']:.2f}")
    if "occupancy" in m:
        parts.append(f"Malls&Outlets occupancy {m['occupancy']:.1f}%")
    summary = ", ".join(parts) if parts else "숫자 요약 생략(파싱 불안정)"
    title = f"{TAG} 실적 8-K Item 2.02 | {f['date']}"
    links = f"목록 {info['index_url']}"
    if info.get("ex_url"):
        links += f", Exhibit 99.1 {info['ex_url']}"
    else:
        links += f", 본문 {info['main_url']}"
    note = (info.get("note") or "").strip()
    status = (
        f"{f['date']} 8-K(접수 {f['acc']}) Item 2.02. 헤드라인: {info.get('headline', '')}. "
        f"{summary}. {links}."
        + (f" {note}" if note else "")
    )
    msg = body(
        stamp,
        status,
        "보도자료와 가이던스/점유율을 직접 대조하세요. 파싱 값은 참고용입니다.",
        "이후 새 Item 2.02 8-K가 나오면 또 1번 옵니다. 일일 요약의 P/FFO midpoint는 가이던스 파싱 성공 시 갱신됩니다.",
        "헤드라인만 보고 장전/시간외 추격, 파싱 숫자를 확정 실적으로 단정.",
    )
    return title, msg, 4


def evaluate(cfg: dict, st: dict, obs: dict, today_us: str) -> list[tuple[str, str, int]]:
    """Pure rules on price/yield/daily. Earnings handled separately. Mutates st."""
    r = cfg["rules"]
    out: list[tuple[str, str, int]] = []
    stamp_placeholder = ""  # filled by send_actions
    y10 = obs.get("y10")
    bars = obs.get("bars")
    ffo_mid = float(st.get("ffo_midpoint") or r.get("ffo_midpoint") or 13.25)
    ffo_src = st.get("ffo_midpoint_source") or "config prior guidance midpoint 13.25"

    if y10:
        cur_d, cur = y10[-1]
        if cur <= r["rate_low"] and st.get("rate_low_armed", True):
            st["rate_low_armed"] = False
            out.append(
                (
                    f"{TAG} SPG / US10Y {cur:.2f}% | 미 10년물 4.50% 이하",
                    f"미 10년물 {cur:.2f}%({cur_d}), {r['rate_low']:.2f}% 이하. "
                    f"리츠 할인율 부담 완화 쪽. {r['rate_low_rearm']:.2f}% 위로 올라가야 다시 무장.",
                    3,
                )
            )
        elif cur > r["rate_low_rearm"]:
            st["rate_low_armed"] = True
        st["y10_last_date"] = cur_d

    if bars:
        day, close = bars[-1]
        prev = bars[-2][1] if len(bars) >= 2 else None
        # high breakout
        if close > r["price_high"] and st.get("price_high_armed", True):
            st["price_high_armed"] = False
            out.append(
                (
                    f"{TAG} 종가 ${close:.2f} | 238.50 돌파(52주 고)",
                    f"{day} 종가 ${close:.2f}, 52주 고점선 ${r['price_high']:.2f} 위. "
                    f"재무장선 ${r['price_high_rearm']:.3f}(x0.99) 아래로 내려와야 다시 무장.",
                    4,
                )
            )
        elif close < r["price_high_rearm"]:
            st["price_high_armed"] = True

        if close < r["price_low"] and st.get("price_low_armed", True):
            st["price_low_armed"] = False
            out.append(
                (
                    f"{TAG} 종가 ${close:.2f} | 205 하회(애널 저점)",
                    f"{day} 종가 ${close:.2f}, 애널리스트 저점 타겟 ${r['price_low']:.2f} 아래. "
                    f"재무장선 ${r['price_low_rearm']:.2f}(x1.01) 위로 올라가야 다시 무장.",
                    4,
                )
            )
        elif close > r["price_low_rearm"]:
            st["price_low_armed"] = True

        st["price_last_date"] = day

        # daily summary for newest completed US bar not yet summarized
        if day != st.get("last_summary_date"):
            chg = ((close / prev - 1) * 100) if prev else None
            chg_txt = f"{chg:+.2f}%" if chg is not None else "비교 불가"
            d_hi = close - r["price_high"]
            d_lo = close - r["price_low"]
            pffo = close / ffo_mid if ffo_mid else None
            pffo_txt = f"{pffo:.2f}x" if pffo else "확인 불가"
            y_txt = f"{y10[-1][1]:.2f}%({y10[-1][0]})" if y10 else "확인 불가"
            out.append(
                (
                    f"{TAG} 일일 요약 | 종가 ${close:.2f}",
                    f"{day} 종가 ${close:.2f}, 전일 대비 {chg_txt}, "
                    f"238.50까지 {d_hi:+.2f}달러, 205까지 {d_lo:+.2f}달러, "
                    f"P/FFO {pffo_txt}(midpoint ${ffo_mid:.2f}, {ffo_src}), "
                    f"미 10년물 {y_txt}.",
                    2,
                )
            )
            st["last_summary_date"] = day

    return out


def fail_alerts(cfg: dict, st: dict, errs: dict, today: str) -> list[tuple[str, str, int]]:
    out = []
    names = {"y10": "미 10년물", "bars": "SPG 종가", "edgar": "SEC EDGAR"}
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


def send_actions(alerts: list[tuple[str, str, int]], stamp: str) -> list[tuple[str, str, int]]:
    built = []
    for title, text, pri in alerts:
        if "조회 실패" in title:
            msg = body(
                stamp,
                text,
                "다음 09:00 KST 재시도를 기다리세요.",
                "읽힌 항목은 정상 판단했습니다.",
                "조회 실패를 조건 충족으로 읽기.",
            )
        elif "일일 요약" in title:
            msg = body(
                stamp,
                text,
                "참고만. 오늘 새로 켠 이벤트 조건이 없으면 추가 조치는 없습니다.",
                "종가 238.50 돌파, 205 하회, US10Y 4.50% 이하, 실적 8-K 시에만 별도 알림.",
                "일일 요약만 보고 바로 매수/매도.",
            )
        elif "US10Y" in title:
            msg = body(
                stamp,
                text,
                "보유/편입 계획이 있으면 금리 구간을 다시 보세요. 한 번에 크게 넣지 마세요.",
                "4.60% 위로 올라가면 다시 무장됩니다.",
            )
        elif "238.50" in title or "돌파" in title:
            msg = body(
                stamp,
                text,
                "돌파 지속 여부와 거래량을 확인. 추격 매수는 계획 비중 안에서만.",
                "종가가 236.12 아래로 내려온 뒤 다시 238.50 위로 오면 또 알림.",
            )
        elif "205" in title or "하회" in title:
            msg = body(
                stamp,
                text,
                "보유 중이면 비중과 손절 기준을 오늘 다시 정하기. 신규는 바닥 확인부터.",
                "종가가 207.05 위로 올라온 뒤 다시 205 아래로 오면 또 알림.",
            )
        else:
            msg = body(stamp, text, "상태를 확인하세요.")
        built.append((title, msg, pri))
    return built


def gather(cfg: dict, now: datetime) -> tuple[dict, dict]:
    obs: dict = {}
    errs: dict = {}
    now_ny = now.astimezone(NY)
    try:
        obs["bars"] = fetch_bars(cfg, now_ny)
    except Exception as exc:  # noqa: BLE001
        obs["bars"] = None
        errs["bars"] = str(exc)[:200]
    try:
        obs["y10"] = y10_series(now_ny.date())
    except Exception as exc:  # noqa: BLE001
        obs["y10"] = None
        errs["y10"] = str(exc)[:200]
    return obs, errs


def run_earnings(cfg: dict, st: dict, dry: bool, stamp: str, fetch_sub=None, get=None) -> list[tuple[str, str, int]]:
    alerts: list[tuple[str, str, int]] = []
    fetch_sub = fetch_sub or (lambda: json.loads(edgar_get(cfg, cfg["edgar"]["submissions"])))
    try:
        filings = recent_filings(fetch_sub())
    except Exception as exc:  # noqa: BLE001
        print(f"EDGAR fetch failed: {exc}", file=sys.stderr)
        day = stamp[:10]
        fails = st.setdefault("fail", {})
        f = fails.get("edgar") or {"days": 0, "last": None, "notified": False}
        if f["last"] != day:
            f["days"] += 1
            f["last"] = day
        if f["days"] >= cfg["rules"]["fail_ops_after_days"] and not f["notified"]:
            f["notified"] = True
            alerts.append(
                (
                    f"{TAG} SEC EDGAR 조회 실패 | {f['days']}일 연속, 그 규칙 건너뜀",
                    f"EDGAR submissions 조회 실패: {type(exc).__name__} {exc}"[:240],
                    3,
                )
            )
        fails["edgar"] = f
        return alerts
    # clear edgar fail on success
    st.setdefault("fail", {})["edgar"] = {"days": 0, "last": None, "notified": False}
    seen = set(st.get("seen_accessions") or [])
    new = pick_new_202(filings, seen, cfg["edgar"].get("min_filing_date"))
    if not new:
        print("no new Item 2.02 8-K")
        return alerts
    for f in new:
        info = inspect_filing(cfg, f, get)
        t, b, p = earnings_alert(cfg, f, info, stamp)
        alerts.append((t, b, p))
        seen.add(f["acc"])
        m = info.get("metrics") or {}
        if "guidance_mid" in m:
            st["ffo_midpoint"] = m["guidance_mid"]
            st["ffo_midpoint_source"] = f"parsed from {f['acc']} ({f['date']})"
            print(f"ffo midpoint updated to {m['guidance_mid']}")
    st["seen_accessions"] = sorted(seen)
    return alerts


def publish_all(alerts: list[tuple[str, str, int]], dry: bool, cfg: dict) -> None:
    server = resolve_server(cfg.get("ntfy_server"))
    for title, msg, pri in alerts:
        topic = route_topic(resolve_topic("port", cfg.get("ntfy_topic")), title)
        if "조회 실패" in title:
            topic = resolve_topic("ops", cfg.get("ntfy_ops_topic"))
        # ban check
        blob = title + msg
        for ch in BANNED:
            if ch in blob:
                raise RuntimeError(f"banned char {ch!r} in alert text")
        if dry:
            print(f"DRY RUN would send ({topic}, p{pri}): {title}\n{msg}\n---")
        else:
            publish(topic, title, msg, pri, tags=["chart_with_upwards_trend"], server=server, config_name=NAME)
            print(f"sent {title}")


# ---------- main ----------

def main(argv: list[str] | None = None) -> int:
    argv = argv or sys.argv[1:]
    mode = (argv[0] if argv else "auto").lower()
    if mode == "selftest":
        return selftest()
    if mode not in {"auto", "dry", "install"}:
        print(f"unknown mode {mode}")
        return 2

    cfg = load_cfg()
    now = datetime.now(KST)
    if now > datetime.strptime(cfg["ends_kst"], "%Y-%m-%d %H:%M").replace(tzinfo=KST):
        print("ended")
        return 0

    dry = mode == "dry"
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    print(f"{stamp} spgwatch mode={mode}")
    if dry:
        print("DRY RUN (no ntfy, no state write)")

    if mode == "install":
        obs, errs = gather(cfg, now)
        b = (obs.get("bars") or [None])[-1]
        y = (obs.get("y10") or [None])[-1]
        status = (f"종가 ${b[1]:.2f}({b[0]})" if b else "종가 확인 불가")
        status += f", 미 10년물 {y[1]:.2f}%({y[0]})" if y else ", 미 10년물 확인 불가"
        if errs:
            status += f". 조회 실패: {json.dumps(errs, ensure_ascii=False)}"
        title = f"{TAG} 설치 확인 | 매매 신호 아님"
        msg = body(
            stamp,
            status + ". 지금 켜진 이벤트 조건 없음(설치 확인).",
            "없음. 평일 09:00 KST에 조건이 새로 켜질 때만 옵니다.",
            "조건: 실적 8-K Item 2.02, 종가>238.50 또는 <205, US10Y<=4.50%, 일일 요약.",
            "이 설치 확인을 매수나 매도 신호로 읽기.",
        )
        print(title)
        print(msg)
        if not dry:
            publish(
                resolve_topic("port", cfg.get("ntfy_topic")),
                title,
                msg,
                3,
                tags=["white_check_mark"],
                server=resolve_server(cfg.get("ntfy_server")),
                config_name=NAME,
            )
        return 0

    st = load_state()
    obs, errs = gather(cfg, now)
    if errs:
        print("errors: " + json.dumps(errs, ensure_ascii=False), file=sys.stderr)
    if obs.get("bars"):
        print(f"bars last {obs['bars'][-1]}")
    if obs.get("y10"):
        print(f"y10 last {obs['y10'][-1]}")

    today_key = now.strftime("%Y-%m-%d")
    earn = run_earnings(cfg, st, dry, stamp)
    # strip full bodies from evaluate raw status; rebuild
    raw = evaluate(cfg, st, obs, today_us=(obs.get("bars") or [(None, None)])[-1][0] or today_key)
    built = send_actions(raw, stamp)
    fails = fail_alerts(cfg, st, errs, today_key)
    fail_built = send_actions(fails, stamp)

    all_alerts = earn + built + fail_built
    if not all_alerts:
        print("nothing due; silent")
    publish_all(all_alerts, dry, cfg)
    save_state(st, dry)
    return 0


# ---------- selftest ----------

def selftest() -> int:
    cfg = load_cfg()
    fails = 0

    def check(label, got, want=True):
        nonlocal fails
        ok = got == want if not isinstance(want, bool) or want is not True or got is not True else bool(got) == want
        if want is True and not isinstance(got, bool):
            ok = bool(got)
        elif not isinstance(want, bool):
            ok = got == want
        else:
            ok = got == want
        fails += 0 if ok else 1
        print(("ok  " if ok else "FAIL"), label, "" if ok else f"got={got!r} want={want!r}")

    # parse fixture
    snippet = (FX / "ex991_q2_snippet.txt").read_text(encoding="utf-8")
    m = parse_earnings_metrics(snippet)
    check("parse re_ffo_ps", m.get("re_ffo_ps"), 3.29)
    check("parse guidance", (m.get("guidance_low"), m.get("guidance_high"), m.get("guidance_mid")), (13.20, 13.30, 13.25))
    check("parse occupancy", m.get("occupancy"), 96.0)
    check("parse headline", "Reports" in (m.get("headline") or ""), True)

    # pick 2.02
    filings = [
        {"form": "8-K", "date": "2026-08-10", "acc": "OLD", "items": "2.02,9.01", "doc": "a.htm"},
        {"form": "8-K", "date": "2026-11-05", "acc": "NEW1", "items": "7.01,9.01", "doc": "b.htm"},
        {"form": "8-K", "date": "2026-11-05", "acc": "NEW2", "items": "2.02,9.01", "doc": "c.htm"},
        {"form": "10-Q", "date": "2026-11-05", "acc": "Q", "items": "", "doc": "d.htm"},
    ]
    check("pick ignores seen and non-2.02", [x["acc"] for x in pick_new_202(filings, {"OLD"}, "2026-11-05")], ["NEW2"])
    check("pick min_date filters old", [x["acc"] for x in pick_new_202(filings, set(), "2026-11-05")], ["NEW2"])

    # price / rate evaluate
    def init():
        return default_state() | {"ffo_midpoint": 13.25, "ffo_midpoint_source": "test", "last_summary_date": "2026-11-03"}

    st = init()
    bars = [("2026-11-03", 228.0), ("2026-11-04", 239.0)]
    a = evaluate(cfg, st, {"bars": bars, "y10": [("2026-11-04", 4.89)]}, "2026-11-04")
    titles = [x[0] for x in a]
    check("high breakout fires", any("238.50 돌파" in t for t in titles), True)
    check("daily summary fires", any("일일 요약" in t for t in titles), True)
    check("high disarmed", st["price_high_armed"], False)
    # rearm needs < 236.115
    st["last_summary_date"] = "2026-11-04"
    evaluate(cfg, st, {"bars": [("2026-11-05", 237.0)], "y10": None}, "2026-11-05")
    check("237 no high rearm", st["price_high_armed"], False)
    evaluate(cfg, st, {"bars": [("2026-11-06", 236.0)], "y10": None}, "2026-11-06")
    check("236 rearms high", st["price_high_armed"], True)

    st = init()
    a = evaluate(cfg, st, {"bars": [("2026-11-03", 210.0), ("2026-11-04", 204.0)], "y10": None}, "2026-11-04")
    check("low fires", any("205 하회" in t for t in [x[0] for x in a]), True)
    check("low disarmed", st["price_low_armed"], False)
    st["last_summary_date"] = "2026-11-04"
    evaluate(cfg, st, {"bars": [("2026-11-05", 206.0)], "y10": None}, "2026-11-05")
    check("206 no low rearm", st["price_low_armed"], False)
    evaluate(cfg, st, {"bars": [("2026-11-06", 208.0)], "y10": None}, "2026-11-06")
    check("208 rearms low", st["price_low_armed"], True)

    st = init()
    a = evaluate(cfg, st, {"bars": None, "y10": [("2026-11-03", 4.70), ("2026-11-04", 4.49)]}, "2026-11-04")
    check("US10Y title", any("SPG / US10Y" in t and "4.49%" in t for t in [x[0] for x in a]), True)
    check("rate disarmed", st["rate_low_armed"], False)
    evaluate(cfg, st, {"bars": None, "y10": [("2026-11-05", 4.55)]}, "2026-11-05")
    check("4.55 no rearm", st["rate_low_armed"], False)
    evaluate(cfg, st, {"bars": None, "y10": [("2026-11-06", 4.61)]}, "2026-11-06")
    check("4.61 rearms", st["rate_low_armed"], True)

    # P/FFO in summary
    st = init()
    a = evaluate(cfg, st, {"bars": [("2026-11-03", 220.0), ("2026-11-04", 228.50)], "y10": [("2026-11-04", 4.89)]}, "2026-11-04")
    body_txt = " ".join(x[1] for x in a if "일일 요약" in x[0])
    check("pffo ~17.25", "17.25x" in body_txt or "17.2" in body_txt, True)

    # banned chars / SPG in titles
    st = init()
    msgs = []
    for obs in (
        {"bars": [("2026-11-03", 228.0), ("2026-11-04", 239.0)], "y10": [("2026-11-04", 4.40)]},
        {"bars": [("2026-11-03", 210.0), ("2026-11-04", 204.0)], "y10": None},
    ):
        for ti, tx, _ in evaluate(cfg, init(), obs, "2026-11-04"):
            msgs.append(ti + tx)
    # also earnings message
    info = {
        "index_url": "https://www.sec.gov/x",
        "main_url": "https://www.sec.gov/y",
        "ex_url": "https://www.sec.gov/z",
        "metrics": {"re_ffo_ps": 3.4, "guidance_low": 13.2, "guidance_high": 13.4, "guidance_mid": 13.3, "occupancy": 95.5},
        "headline": "Simon Reports Third Quarter",
        "note": "",
    }
    et, eb, _ = earnings_alert(cfg, {"date": "2026-11-05", "acc": "ACC"}, info, "2026-11-06 09:00 KST")
    msgs.append(et + eb)
    text = " ".join(msgs) + json.dumps(cfg, ensure_ascii=False)
    check("all titles have SPG", all("SPG" in m for m in msgs), True)
    check("titles start [SPG]", all(m.startswith(TAG) for m in msgs), True)
    check("no middle dot", "\u00b7" not in text, True)
    check("no em-dash", "\u2014" not in text, True)
    check("ends_kst", cfg["ends_kst"], "2027-11-05 23:59")
    check("related leaves spg alone", "spg (" in cfg["related_alerts"][0], True)
    # dry path: evaluate does not write
    check("fixture exists", (FX / "ex991_q2_snippet.txt").exists(), True)

    # routing: fail title -> ops
    check("fail routes ops", route_topic("cwally-port", f"{TAG} SPG 종가 조회 실패 | 2일 연속"), "cwally-ops")

    print(f"SELFTEST {'PASS' if not fails else 'FAIL'}: {fails} failed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
