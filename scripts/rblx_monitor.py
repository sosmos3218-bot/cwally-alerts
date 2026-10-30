#!/usr/bin/env python3
"""RBLX watch: price zone, Q3 bookings guide, new filing, post-earnings scenarios. Silent unless change."""
from __future__ import annotations

import html
import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
CFG_PATH = ROOT / "configs" / "rblx.json"
STATE_PATH = ROOT / "state" / "rblx.json"
UA = {"User-Agent": "cwally-alerts rblx research monitor research@example.com"}
WINDOW = {"2026-10-29", "2026-10-30"}
Q3_JUDGE = "2026-10-30"
ONESHOT_DATES = {"2026-10-30"}

def load_json(path: Path, fallback: dict) -> dict:
    if not path.exists():
        return dict(fallback)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return dict(fallback)
    out = dict(fallback)
    out.update(data)
    return out

def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=25) as resp:
        return resp.read().decode("utf-8", errors="ignore")

def quote(url: str) -> tuple[float | None, str | None]:
    data = json.loads(fetch(url))
    result = (data.get("chart") or {}).get("result") or []
    if not result:
        return None, "quote empty"
    price = (result[0].get("meta") or {}).get("regularMarketPrice")
    try:
        return (float(price) if price is not None else None), None
    except (TypeError, ValueError):
        return None, "price parse"

def sec_recent(url: str) -> dict:
    data = json.loads(fetch(url))
    return (data.get("filings") or {}).get("recent") or {}

def latest_sec_date(url: str, recent: dict | None = None) -> str | None:
    if recent is not None:
        forms = recent.get("form") or []
        dates = recent.get("filingDate") or []
        for form, day in zip(forms, dates):
            if form in {"8-K", "10-Q", "10-K"}:
                return day
        return None
    data = json.loads(fetch(url))
    recent = ((data.get("filings") or {}).get("recent") or {})
    forms = recent.get("form") or []
    dates = recent.get("filingDate") or []
    for form, day in zip(forms, dates):
        if form in {"8-K", "10-Q", "10-K"}:
            return day
    return None

def visible(raw: str) -> str:
    text = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", raw)
    text = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    # SEC exhibits put zero-width spaces between table cells; \s does not match them.
    text = re.sub(r"[\u200b\u200c\u200d\u2060\ufeff]", " ", text)
    return re.sub(r"\s+", " ", text)

# Exact bookings in millions from the revenue-to-bookings reconciliation table
# ("Bookings $1,697"). Rounded prose ("bookings were $1.7 billion") and "$ 1.7 B" never match,
# because a rounded value cannot decide the 1,576/1,653 thresholds.
BOOKINGS_RE = re.compile(
    r"\bBookings\s*\$\s*([0-9]{1,2},[0-9]{3}|[0-9]{3,4})(?![0-9.,])(?!\s*(?:[MB]|million|billion)\b)"
)
DAU_RES = (
    re.compile(r"\bDAUs\s+were\s+([0-9]{2,3}(?:\.[0-9])?)\s*million", re.I),
    re.compile(r"Daily active users \(DAUs\)[^.]{0,80}?\bto\s+([0-9]{2,3}(?:\.[0-9])?)\s*million", re.I),
)

def parse_bookings(blob: str) -> float | None:
    for m in BOOKINGS_RE.finditer(blob or ""):
        # skip the next-quarter guidance reconciliation ("Guidance ... Bookings $ 1,890 $ 2,000")
        if "guidance" in blob[max(0, m.start() - 400) : m.start()].lower():
            continue
        return float(m.group(1).replace(",", ""))
    return None

def parse_dau(blob: str) -> float | None:
    for rx in DAU_RES:
        m = rx.search(blob or "")
        if m:
            return float(m.group(1))
    return None

def sec_doc_base(cik: str, accession: str) -> str:
    return f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}/"

def ex991_url(cik: str, accession: str) -> str | None:
    """Filing index page -> EX-99.1 document URL."""
    idx = fetch(sec_doc_base(cik, accession) + f"{accession}-index.html")
    for row in re.findall(r"(?is)<tr[^>]*>(.*?)</tr>", idx):
        cells = [re.sub(r"<[^>]+>", "", c).strip() for c in re.findall(r"(?is)<td[^>]*>(.*?)</td>", row)]
        if any(c.upper() == "EX-99.1" for c in cells):
            href = re.search(r'href="([^"]+)"', row)
            if href:
                link = href.group(1).replace("/ix?doc=", "")
                return link if link.startswith("http") else "https://www.sec.gov" + link
    return None

def sec_earnings(recent: dict, cik: str, since: str | None) -> dict:
    """Newest 8-K (Item 2.02) filed after `since` -> EX-99.1 -> exact bookings / DAU."""
    out = {"filing": None, "accession": None, "url": None, "bookings_m": None, "dau_m": None, "err": None, "source": "sec"}
    forms = recent.get("form") or []
    dates = recent.get("filingDate") or []
    accs = recent.get("accessionNumber") or []
    items = recent.get("items") or [""] * len(forms)
    for form, day, acc, item in zip(forms, dates, accs, items):
        if form != "8-K" or (since and day <= since):
            continue
        if item and "2.02" not in item:
            continue
        out.update(filing=day, accession=acc)
        try:
            url = ex991_url(cik, acc)
            if not url:
                out["err"] = f"EX-99.1 없음 ({acc})"
                return out
            out["url"] = url
            text = visible(fetch(url))
            out["bookings_m"] = parse_bookings(text)
            out["dau_m"] = parse_dau(text)
            if out["bookings_m"] is None:
                out["err"] = "EX-99.1 bookings 표 숫자 미확인"
        except Exception as exc:
            out["err"] = f"SEC 문서 조회 실패: {exc}"
        return out
    out["err"] = f"{since} 이후 실적 8-K 없음"
    return out

def earnings_with_fallback(cfg: dict, recent: dict | None, since: str | None, sec_err: str | None) -> dict:
    """SEC EDGAR first; ir.roblox.com only when SEC gave no exact number."""
    if recent is not None:
        res = sec_earnings(recent, str(cfg.get("cik") or "1315098"), since)
    else:
        res = {"filing": None, "accession": None, "url": None, "bookings_m": None, "dau_m": None, "err": f"SEC 조회 실패: {sec_err}", "source": "sec"}
    print(f"sec earnings: {json.dumps(res, ensure_ascii=False)}")
    if res["bookings_m"] is not None:
        return res
    try:
        ir_text = visible(fetch(cfg["sources"]["ir"]))
        ir_b = parse_bookings(ir_text)
        if ir_b is not None:
            res.update(bookings_m=ir_b, dau_m=res["dau_m"] or parse_dau(ir_text), url=cfg["sources"]["ir"], source="ir", err=None)
            print(f"ir fallback bookings={ir_b}")
    except Exception as exc:
        print(f"ir fallback fail: {exc}")
    return res

def dau_text(dau: float | None, q2: float | None) -> str:
    if dau is None:
        return "DAU는 원문 확인 필요."
    if q2:
        return f"DAU {dau:g}백만 명, 2분기 {q2:g}백만 명 대비 {dau - q2:+g}백만."
    return f"DAU {dau:g}백만 명."

def zone_of(price: float, stop: float, review: float, high: float) -> str:
    if price <= stop:
        return "loss20"
    if price <= review:
        return "loss10"
    if price > high:
        return "above_55"
    return "hold"

def pnl_text(price: float | None, shares: float, cost: float) -> str:
    if price is None:
        return "평가손익 미확인"
    pnl = (price - cost) * shares
    pct = (price / cost - 1) * 100
    return f"28주 평가 {pnl:+.2f}달러({pct:+.1f}%), 현재가 {price:.2f}달러, 평단 {cost:.4f}달러"

def q3_judgment(bookings: float | None, lo: float, hi: float, price: float | None, shares: float, cost: float, dau_note: str = "") -> tuple[str, str, str, int, str]:
    pnl = pnl_text(price, shares, cost)
    scenarios = (
        "상단: bookings 16.53억 초과 + DAU 순증 + 수익화 반등 → 단기 반등 가능.\n"
        "중간: 가이드 안 (15.76~16.53억) + DAU 안정 → 관망 유지.\n"
        "하단: 15.76억 미만 또는 DAU 추가 감소 → 비중 재검토."
    )
    if bookings is None:
        return (
            "[RBLX] 10/30 실적 | bookings 확인 불가",
            f"2026-10-30 판정. bookings 정확한 숫자를 읽지 못했습니다. 15.76억 미만이 아닙니다. {pnl}. {dau_note}",
            "SEC 8-K EX-99.1 원문에서 Q3 bookings/DAU를 직접 보세요. 다음 oneshot/평일 09:00에 다시 읽습니다. 비중을 바꾸지 마세요.",
            4,
            scenarios,
        )
    if bookings < lo:
        return (
            "[RBLX] 10/30 실적 | 가이드 하단 미달, 비중 재검토",
            f"파싱 bookings {bookings:.0f}백만 달러, 가이드 하단 {lo:.0f} 미만. {pnl}. {dau_note}",
            "보유 28주 비중 재검토. 파싱값과 원문이 다르면 원문을 우선하세요. 매도 지시가 아닙니다.",
            5,
            scenarios,
        )
    if bookings > hi:
        return (
            "[RBLX] 10/30 실적 | 가이드 상단 초과",
            f"파싱 bookings {bookings:.0f}백만 달러, 가이드 상단 {hi:.0f} 초과. 미국/캐나다 수익화는 미확인. {pnl}. {dau_note}",
            "보유 유지. 상단 초과만으로 추가하지 마세요.",
            4,
            scenarios,
        )
    return (
        "[RBLX] 10/30 실적 | 가이드 안, 보유 유지",
        f"파싱 bookings {bookings:.0f}백만 달러, 가이드 {lo:.0f}~{hi:.0f} 안. {pnl}. {dau_note}",
        "보유 유지. 가이드 안은 추가 조건이 아닙니다.",
        3,
        scenarios,
    )

def body(stamp: str, status: str, action: str, later: str, trim: str, avoid: str, scenarios: str = "") -> str:
    lines = [
        f"{stamp} | RBLX | 관망 조건 점검",
        "이게 뭐 알림: 보유 중인 로블록스 가격 구간과 Q3 bookings/DAU 조건만 보는 알림입니다. 추가매수 지시가 아닙니다.",
        f"지금 결과/상태: {status}",
        f"지금 할 일: {action}",
        f"나중에: {later}",
        f"익절/줄일 때: {trim}",
        f"하지 말 것: {avoid}",
    ]
    if scenarios:
        lines.append(f"시나리오: {scenarios}")
    return "\n".join(lines)

def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    topic = resolve_topic("port", cfg.get("ntfy_topic"))
    server = resolve_server(cfg.get("ntfy_server"))
    years = cfg.get("year_guard") or [now.year]
    if mode != "force" and now.year not in years:
        print("rblx year-guard silent")
        return 0
    if mode == "install":
        publish(
            topic,
            "[RBLX] 설치 확인 | 매매 신호 아님",
            body(
                stamp,
                "평일 09:00 KST morning + 10/30 oneshot. 기준가 2026-10-16 49.55달러. Q3 bookings 가이던스 15.76~16.53억 달러.",
                "설치 확인만 하세요. 사지 마세요.",
                "월요 상태, 38/55달러 구간 변경, 10/30 oneshot, 새 8-K/10-Q만 보냅니다.",
                "이번 알림으로 비중을 바꾸지 마세요.",
                "설치 확인을 매수나 매도 신호로 읽기.",
            ),
            3,
            ["information_source"],
            server,
        )
        print("install confirmation sent")
        return 0

    dry = mode == "dry" or os.environ.get("RBLX_DRY") == "1"
    state = load_json(
        STATE_PATH,
        {
            "updated_at": None,
            "last_price": None,
            "zone": "hold",
            "last_weekly": None,
            "last_filing": cfg["baseline"]["last_known_filing"],
            "earnings_fingerprint": None,
            "sent_dates": [],
            "fail_notified_dates": [],
            "fetch_fail_date": None,
        },
    )
    today = now.date()
    if today.isoformat() > cfg["valid_until"] and mode not in {"force", "dry"}:
        print(f"after {cfg['valid_until']}; silent")
        return 0

    base = cfg["baseline"]
    levels = cfg["levels"]
    price, qerr = None, None
    try:
        price, qerr = quote(cfg["sources"]["quote"])
    except Exception as exc:
        qerr = str(exc)
    print(f"price={price} qerr={qerr}")

    alerts: list[tuple[str, str, int]] = []
    if price is None:
        if state.get("fetch_fail_date") != today.isoformat():
            alerts.append(
                (
                    "[RBLX] 주가 조회 실패 | 조건 미확인",
                    body(
                        stamp,
                        f"주가 조회 실패. {qerr or '빈 응답'}. 조건 미충족이 아닙니다.",
                        "증권 앱에서 RBLX 전일 종가를 직접 보세요. 38달러, 55달러를 이 알림으로 판정하지 마세요.",
                        "다음 평일 09:00 KST에 재시도합니다.",
                        "이번 알림으로 줄이거나 사지 마세요.",
                        "조회 실패를 38달러 이탈이나 정상으로 취급.",
                    ),
                    4,
                )
            )
            state["fetch_fail_date"] = today.isoformat()
    else:
        shares = float(cfg["position"]["shares"])
        cost = float(cfg["position"]["avg_cost"])
        pnl = (price - cost) * shares
        pct = (price / cost - 1) * 100
        zone = zone_of(price, levels["loss20"], levels["loss10"], levels["hold_ceiling"])
        prev = state.get("zone") or "hold"
        changed = zone != prev
        weekly = today.weekday() == 0 and state.get("last_weekly") != today.isoformat()
        window = today.isoformat() in WINDOW and today.isoformat() not in (state.get("sent_dates") or [])
        if today.isoformat() == Q3_JUDGE:
            window = False
        if changed or weekly or window or mode == "force":
            if zone == "loss20":
                title = "[RBLX] 평단 -20% | 보유 재검토"
                action = "28주 보유는 유지하되 추가하지 마세요. 평단 대비 -20%는 비중 재검토 구간입니다."
            elif zone == "loss10":
                title = "[RBLX] 평단 -10% | 추가 금지"
                action = "보유는 유지하세요. -10% 아래 추가는 금지입니다."
            elif zone == "above_55":
                title = "[RBLX] 55달러 위 | 추가 금지"
                action = "보유는 유지하세요. 55달러 위 추가는 금지입니다."
            else:
                title = "[RBLX] -10%~55달러 | 보유 유지"
                action = "보유 유지, 추가 없음. 10/30 실적 확인 전 비중을 바꾸지 마세요."
            why = "구간 변경" if changed else "월요일 상태" if weekly else "실적 창"
            alerts.append(
                (
                    title,
                    body(
                        stamp,
                        f"RBLX {price:.2f}달러. 28주, 평단 {cost:.4f}달러, 평가 {pnl:+.2f}달러({pct:+.1f}%). 구간 {prev} -> {zone}. {why}. Q3 bookings 가이드 15.76~16.53억 달러.",
                        action,
                        "보유 유지. 상향은 bookings 16.53억 초과와 미국/캐나다 시간당 수익화 반등. 하향은 15.76억 미만이면 비중 재검토.",
                        "55달러 위 추가 금지. -10%는 추가 금지, -20%는 보유 재검토이지 매도 지시 아님.",
                        "손실 폭 터치를 추가매수나 전량매도 신호로 쓰기. 신용, 미수, 추격.",
                    ),
                    4 if zone != "hold" else 3,
                )
            )
            state["zone"] = zone
            state["last_price"] = price
            if weekly:
                state["last_weekly"] = today.isoformat()
            if window:
                sent = list(state.get("sent_dates") or [])
                sent.append(today.isoformat())
                state["sent_dates"] = sent
        else:
            state["last_price"] = price
            state["zone"] = zone
            print(f"price zone unchanged ({zone}); silent")

    # SEC EDGAR is the primary earnings source (ir.roblox.com returns 403 to the runner).
    recent, ferr = None, None
    try:
        recent = sec_recent(cfg["sources"]["sec"])
    except Exception as exc:
        ferr = str(exc)
        print(f"sec fail: {exc}")
    filing = latest_sec_date(cfg["sources"]["sec"], recent) if recent is not None else None
    last_filing = state.get("last_filing") or base["last_known_filing"]
    oneshot_handled = False

    # Post-earnings oneshot / Q3 judge with scenarios.
    # sent_dates gets the date only after an exact parse; a failure notice goes out at most once
    # per date (fail_notified_dates) so later oneshots / the morning run retry.
    day = today.isoformat()
    if day in ONESHOT_DATES and (day not in (state.get("sent_dates") or []) or mode == "force"):
        oneshot_handled = True
        shares = float(cfg["position"]["shares"])
        cost = float(cfg["position"]["avg_cost"])
        res = earnings_with_fallback(cfg, recent, base["last_known_filing"], ferr)
        bookings = res["bookings_m"]
        lo = base["q3_bookings_lo_m"]
        hi = base["q3_bookings_hi_m"]
        dau_note = dau_text(res["dau_m"], base.get("q2_dau_m"))
        if bookings is not None:
            dau_note += f" 출처: {res['url']}"
        elif res.get("err"):
            dau_note += f" 사유: {res['err']}."
        title, status, action, pri, scenarios = q3_judgment(bookings, lo, hi, price, shares, cost, dau_note)
        failed = bookings is None
        notified = list(state.get("fail_notified_dates") or [])
        if failed and day in notified and mode != "force":
            print(f"q3 parse still failing ({res.get('err')}); failure already notified today, silent retry")
        else:
            alerts.append(
                (
                    title,
                    body(
                        stamp,
                        status,
                        action,
                        "상향은 16.53억 초과와 미국/캐나다 수익화 반등이 같이 있을 때입니다. 독립 앱/오프라인은 2027 가시화 대기.",
                        "확인 불가와 가이드 안을 매도나 추가매수로 쓰지 마세요.",
                        "파싱 실패를 15.76억 미만으로 세기. 주가 반응으로 조건 충족 단정.",
                        scenarios,
                    ),
                    pri,
                )
            )
        if failed:
            if day not in notified:
                notified.append(day)
            state["fail_notified_dates"] = notified
        else:
            sent = list(state.get("sent_dates") or [])
            if day not in sent:
                sent.append(day)
            state["sent_dates"] = sent
            state["q3_bookings_m"] = bookings
            state["q3_dau_m"] = res["dau_m"]
            state["q3_source"] = res["url"]
            if res["accession"]:
                state["earnings_fingerprint"] = json.dumps({"accession": res["accession"], "bookings_m": bookings}, sort_keys=True)
                state["last_bookings_m"] = bookings
            if filing:
                state["last_filing"] = max(filing, last_filing or filing)
                last_filing = state["last_filing"]

    # New filing branch: now runs on oneshot dates too (skipped only when the oneshot above
    # already handled this run, so one run never sends two earnings notices).
    if filing and last_filing and filing > last_filing and not oneshot_handled:
        res = earnings_with_fallback(cfg, recent, last_filing, ferr)
        bookings = res["bookings_m"]
        if res["accession"] is None and bookings is None:
            # new 10-Q / 10-K / non-earnings 8-K only: nothing to judge
            print(f"new filing {filing} without earnings 8-K ({res.get('err')}); last_filing -> {filing}")
            state["last_filing"] = filing
        else:
            fingerprint = json.dumps({"accession": res["accession"], "bookings_m": bookings}, sort_keys=True)
            fdate = res["filing"] or filing
            if fingerprint != state.get("earnings_fingerprint"):
                lo = base["q3_bookings_lo_m"]
                hi = base["q3_bookings_hi_m"]
                dau_note = dau_text(res["dau_m"], base.get("q2_dau_m"))
                src = f" 출처: {res['url']}" if res["url"] else ""
                if bookings is None:
                    title = "[RBLX] 새 공시 | bookings 확인 불가"
                    status = f"새 공시 {fdate}. SEC EX-99.1에서 bookings 정확한 숫자를 읽지 못했습니다. 사유: {res.get('err')}. 미확인이며 미충족이 아닙니다.{src}"
                    action = "SEC 8-K EX-99.1 원문에서 bookings와 미국/캐나다 수익화를 직접 보세요. 다음 평일 09:00에 다시 읽습니다."
                    pri = 4
                elif bookings < lo:
                    title = "[RBLX] 실적 | 가이드 하단 미달"
                    status = f"새 공시 {fdate}. bookings {bookings:.0f}백만 달러, 가이드 하단 {lo} 미만. {dau_note}{src}"
                    action = "비중 축소 쪽으로 보세요. 파싱값과 원문이 다르면 원문을 우선하세요."
                    pri = 5
                elif bookings > hi:
                    title = "[RBLX] 실적 | 가이드 상단 초과"
                    status = f"새 공시 {fdate}. bookings {bookings:.0f}백만 달러, 가이드 상단 {hi} 초과. {dau_note} 미국/캐나다 수익화는 이 알림이 확인하지 못했습니다.{src}"
                    action = "관망을 유지하고 원문에서 미국/캐나다 시간당 수익화 반등을 확인하세요. 그 전 매수 전환 아님."
                    pri = 4
                else:
                    title = "[RBLX] 실적 | 가이드 안"
                    status = f"새 공시 {fdate}. bookings {bookings:.0f}백만 달러, 가이드 {lo}~{hi} 안. {dau_note}{src}"
                    action = "관망 유지. 가이드 안은 상향 조건이 아닙니다."
                    pri = 3
                alerts.append(
                    (
                        title,
                        body(
                            stamp,
                            status,
                            action,
                            "상향은 상단 초과와 미국/캐나다 수익화 반등이 같이 있을 때입니다.",
                            "파싱 실패나 가이드 안을 매수 전환으로 쓰지 마세요.",
                            "확인 불가를 충족으로 세기. 주가 반응으로 조건 충족 단정.",
                        ),
                        pri,
                    )
                )
                state["earnings_fingerprint"] = fingerprint
            else:
                print("earnings fingerprint unchanged; silent")
            if bookings is not None:
                # advance only after an exact parse, so a failed parse is retried next run
                state["last_filing"] = filing
                state["last_bookings_m"] = bookings
                state["last_dau_m"] = res["dau_m"]
    elif ferr and state.get("fetch_fail_date") != today.isoformat() and today.weekday() == 0:
        alerts.append(
            (
                "[RBLX] 공시 조회 실패 | 실적 조건 미확인",
                body(
                    stamp,
                    f"SEC 조회 실패. {ferr}. 실적 조건은 미확인입니다.",
                    "SEC EDGAR(CIK 1315098) 최신 8-K를 직접 보세요.",
                    "다음 월요일에 재시도합니다.",
                    "이번 알림으로 비중을 바꾸지 마세요.",
                    "조회 실패를 조건 미충족으로 두기.",
                ),
                4,
            )
        )
        state["fetch_fail_date"] = today.isoformat()

    for title, msg, pri in alerts:
        print(title)
        print(msg)
        print(f"topic={topic} send={not dry}")
        if not dry:
            publish(topic, title, msg, pri, ["chart_with_upwards_trend"], server)
    if not alerts:
        print("no alert")

    state["updated_at"] = now.isoformat()
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
