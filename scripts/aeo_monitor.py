#!/usr/bin/env python3
"""AEO watch for the 27-share holding (avg $19.607): 3Q earnings date, 3Q verdict, -20% / +20% lines.

Change-only. Modes: auto (send + state), dry (print only, no publish, no state write), force (ignore
year/expiry guard). Sources: AEO IR press list (investors.ae.com Q4 feed) for the date notice and the
results headline, aeo-inc.com WordPress post for the release body, Yahoo chart for the last close.
"""
from __future__ import annotations

import html
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
CFG_PATH = ROOT / "configs" / "aeo.json"
STATE_PATH = ROOT / "state" / "aeo.json"
UA = {"User-Agent": "Mozilla/5.0 (cwally-alerts aeo monitor)"}
MONTHS = {m: i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July", "August",
     "September", "October", "November", "December"], start=1)}


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


def quote(url: str) -> tuple[float | None, float | None, str | None]:
    data = json.loads(fetch(url))
    result = (data.get("chart") or {}).get("result") or []
    if not result:
        return None, None, "quote empty"
    meta = result[0].get("meta") or {}
    price = meta.get("regularMarketPrice")
    div = meta.get("trailingAnnualDividendRate")
    if div is None:
        div = meta.get("dividendRate")
    try:
        price_f = float(price) if price is not None else None
    except (TypeError, ValueError):
        price_f = None
    try:
        div_f = float(div) if div is not None else None
    except (TypeError, ValueError):
        div_f = None
    return price_f, div_f, None


def clean_text(raw: str) -> str:
    text = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", raw)
    text = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = text.replace("\u2013", "-").replace("\u2014", "-").replace("\u2019", "'")
    return re.sub(r"\s+", " ", text)


# ---------- IR press list ----------

def press_list(url: str) -> list[dict]:
    data = json.loads(fetch(url))
    out = []
    for item in data.get("GetPressReleaseListResult") or []:
        out.append({"date": item.get("PressReleaseDate") or "", "headline": clean_text(item.get("Headline") or "")})
    return out


def find_date_notice(items: list[dict], quarter: str, fy: int) -> tuple[str | None, str | None]:
    """Return (YYYY-MM-DD ET release day, headline) from 'AEO Inc. to Report Third Quarter Fiscal 2026 Results on December 1, 2026'."""
    pat = re.compile(rf"to Report {quarter} Quarter(?: and [A-Za-z ]+)? Fiscal {fy} Results on ([A-Z][a-z]+) (\d{{1,2}}), (\d{{4}})", re.I)
    for it in items:
        m = pat.search(it["headline"])
        if m and m.group(1).capitalize() in MONTHS:
            day = f"{int(m.group(3)):04d}-{MONTHS[m.group(1).capitalize()]:02d}-{int(m.group(2)):02d}"
            return day, it["headline"]
    return None, None


def find_results_headline(items: list[dict], quarter: str, fy: int) -> str | None:
    pat = re.compile(rf"Reports? [A-Za-z ;]*{quarter} Quarter Fiscal {fy} Results", re.I)
    for it in items:
        if pat.search(it["headline"]) and not re.search(r"to Report", it["headline"], re.I):
            return it["headline"]
    return None


def wp_release_text(search_url: str, quarter: str, after: str) -> tuple[str | None, str | None]:
    """WordPress copy of the results release (full text). Returns (text, link)."""
    q = urllib.parse.quote(f"{quarter} Quarter")
    url = search_url.format(q=q)
    posts = json.loads(fetch(url))
    for p in posts:
        title = clean_text((p.get("title") or {}).get("rendered") or "")
        if re.search(rf"Reports {quarter} Quarter", title, re.I) and (p.get("date") or "") >= after:
            return clean_text((p.get("content") or {}).get("rendered") or ""), p.get("link")
    return None, None


# ---------- release parsing ----------

def _pct(word: str, num: str | None) -> float | None:
    w = word.lower()
    if "flat" in w:
        return 0.0
    if num is None:
        return None
    v = float(num)
    return -v if re.match(r"decreas|declin|fell|down", w) else v


def comp(blob: str, label: str) -> float | None:
    m = re.search(
        label + r" comparable sales (increased|grew|rose|were up|was up|decreased|declined|fell|were down|was down|were flat|was flat|were approximately flat)\s*(?:by )?(\d{1,2}(?:\.\d+)?)?\s*%?",
        blob, re.I)
    return _pct(m.group(1), m.group(2)) if m else None


def money_m(text: str) -> float:
    """'$1.38 billion' or '$211 million' -> millions."""
    m = re.match(r"\$?([\d,.]+)\s*(billion|million|B|M)", text.strip(), re.I)
    if not m:
        return float("nan")
    v = float(m.group(1).replace(",", ""))
    return v * 1000 if m.group(2).lower().startswith("b") else v


def parse_release(blob: str) -> dict:
    out: dict = {}
    out["total_comp"] = comp(blob, r"Total")
    out["ae_comp"] = comp(blob, r"American Eagle(?: brand)?")
    out["aerie_comp"] = comp(blob, r"Aerie")
    m = re.search(r"Total net revenue of (\$[\d.,]+ (?:billion|million)) (increased|decreased|declined) (\d{1,2}(?:\.\d+)?)%", blob, re.I)
    out["revenue_m"] = money_m(m.group(1)) if m else None
    out["revenue_growth"] = _pct(m.group(2), m.group(3)) if m else None
    m = re.search(r"Operating (?:profit|income) (?:was|of) (\$[\d.,]+ (?:billion|million))", blob, re.I)
    out["oi_m"] = money_m(m.group(1)) if m else None
    m = re.search(r"Included in operating (?:profit|income)[^.]{0,80}?net (?:benefit|gain) of \$([\d.,]+) million related to tariff refunds", blob, re.I)
    if not m:
        m = re.search(r"net operating income benefit of tariff refunds was \$([\d.,]+) million", blob, re.I)
    out["oi_refund_m"] = float(m.group(1).replace(",", "")) if m else 0.0
    out["refund_mentioned"] = bool(re.search(r"tariff refund", blob, re.I))
    m = re.search(r"gross margin of (\d{1,2}(?:\.\d+)?)% (expanded|increased|improved|rose|declined|decreased|deleveraged|contracted|was flat)\s*(\d+)?\s*(?:basis points)?", blob, re.I)
    if m:
        out["gm"] = float(m.group(1))
        bp = 0.0 if m.group(3) is None else float(m.group(3))
        out["gm_bp"] = -bp if re.match(r"declin|decreas|delever|contract", m.group(2), re.I) else bp
    else:
        out["gm"], out["gm_bp"] = None, None
    m = re.search(r"Included in gross profit[^.]{0,80}?tariff refunds, which drove (\d+) basis points", blob, re.I)
    out["gm_refund_bp"] = float(m.group(1)) if m else 0.0
    m = re.search(r"inventory at cost was (up|down|flat)\s*(\d{1,2}(?:\.\d+)?)?%?(?:,)? with units (up|down|flat)\s*(\d{1,2}(?:\.\d+)?)?%?", blob, re.I)
    if m:
        out["inv_cost"] = _pct(m.group(1).replace("up", "increased").replace("down", "decreased"), m.group(2)) if m.group(1).lower() != "flat" else 0.0
        out["inv_units"] = _pct(m.group(3).replace("up", "increased").replace("down", "decreased"), m.group(4)) if m.group(3).lower() != "flat" else 0.0
    else:
        out["inv_cost"], out["inv_units"] = None, None
    m = re.search(r"Operating Income \$(\d[\d,]*)\s*M to \$(\d[\d,]*)\s*M \$(\d[\d,]*)\s*M to \$(\d[\d,]*)\s*M", blob, re.I)
    if m:
        out["fy_lo"], out["fy_hi"] = float(m.group(3).replace(",", "")), float(m.group(4).replace(",", ""))
    else:
        m = re.search(r"(?:fiscal|full[- ]year)[^.]{0,30}?operating income[^.]{0,40}?\$(\d[\d,]*) (?:million )?to \$(\d[\d,]*) million", blob, re.I)
        out["fy_lo"], out["fy_hi"] = (float(m.group(1).replace(",", "")), float(m.group(2).replace(",", ""))) if m else (None, None)
    if out["oi_m"] is not None:
        out["oi_ex_m"] = round(out["oi_m"] - (out["oi_refund_m"] or 0.0), 1)
    else:
        out["oi_ex_m"] = None
    if out["gm_bp"] is not None:
        out["gm_bp_ex"] = out["gm_bp"] - (out["gm_refund_bp"] or 0.0)
    else:
        out["gm_bp_ex"] = None
    return out


def verdict(p: dict, cfg: dict) -> tuple[str, list[str], list[str]]:
    """Return (ADD|HOLD|SELL|PENDING, reasons, missing)."""
    g = cfg["guide"]
    th = cfg["thresholds"]
    labels = {"oi_ex_m": "환급 제외 영업이익", "ae_comp": "AE 동일점포",
              "inv_units": "재고 수량", "fy_lo": "연간 영업이익 가이던스"}
    missing = [name for key, name in labels.items() if p.get(key) is None]
    oi = p.get("oi_ex_m")
    ae = p.get("ae_comp")
    units = p.get("inv_units")
    fy_lo, fy_hi = p.get("fy_lo"), p.get("fy_hi")
    reasons = []
    oi_miss = oi is not None and oi < g["q3_oi_lo"]
    oi_deep = oi is not None and oi < g["q3_oi_lo"] * th["oi_deep_miss_ratio"]
    ae_neg = ae is not None and ae < th["ae_comp_flip"]
    fy_cut = fy_hi is not None and fy_hi < g["fy_oi_lo"]
    if oi is not None:
        reasons.append(f"환급 제외 영업이익 {oi:.0f}M (가이던스 {g['q3_oi_lo']}~{g['q3_oi_hi']}M)")
    if ae is not None:
        reasons.append(f"AE 동일점포 {ae:+.0f}%")
    if units is not None:
        reasons.append(f"재고 수량 {units:+.0f}%")
    if fy_lo is not None:
        reasons.append(f"연간 OI 가이던스 {fy_lo:.0f}~{fy_hi:.0f}M (기존 {g['fy_oi_lo']}~{g['fy_oi_hi']}M)")
    if oi_deep or (oi_miss and (ae_neg or fy_cut)) or (fy_cut and ae_neg):
        return "SELL", reasons, missing
    if oi is None or ae is None:
        return "PENDING", reasons, missing
    if (not missing and oi >= g["q3_oi_lo"] and ae >= th["ae_comp_flip"]
            and units < th["inventory_units_max"] and fy_lo >= g["fy_oi_lo"]):
        return "ADD", reasons, missing
    return "HOLD", reasons, missing


# ---------- copy ----------

def body(stamp: str, status: str, action: str, later: str, trim: str, avoid: str) -> str:
    return "\n".join(
        [
            f"{stamp} | AEO | 27주 보유 점검",
            "이게 뭐 알림: 아메리칸 이글 27주(평단 19.61달러) 보유분의 실적일, 3분기 판정, 손실 한도를 보는 알림입니다.",
            f"지금 결과/상태: {status}",
            f"지금 할 일: {action}",
            f"나중에: {later}",
            f"익절/줄일 때: {trim}",
            f"하지 말 것: {avoid}",
        ]
    )


def pnl(price: float | None, h: dict) -> str:
    if price is None:
        return "현재가 미조회"
    pct = (price / h["avg_cost"] - 1) * 100
    usd = (price - h["avg_cost"]) * h["shares"]
    return f"직전 종가 {price:.2f}달러, 평단 대비 {pct:+.1f}% ({usd:+.0f}달러)"


def first_slot_after(cfg: dict, after: datetime) -> datetime | None:
    times = []
    for raw in cfg.get("oneshot_kst") or []:
        try:
            at = datetime.strptime(raw, "%Y-%m-%d %H:%M").replace(tzinfo=KST)
        except ValueError:
            continue
        if at >= after:
            times.append(at)
    return min(times) if times else None


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    dry = mode == "dry" or os.environ.get("AEO_DRY") == "1"
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    state = load_json(
        STATE_PATH,
        {
            "updated_at": None,
            "last_price": None,
            "zone": "mid",
            "div_alerted": False,
            "fetch_fail_date": None,
            "ir_fail_date": None,
            "q3_date": None,
            "q3_date_sent": False,
            "q3_fingerprint": None,
            "q3_verdict": None,
        },
    )
    now = datetime.now(KST)
    if os.environ.get("AEO_NOW"):
        now = datetime.strptime(os.environ["AEO_NOW"], "%Y-%m-%d %H:%M").replace(tzinfo=KST)
    today = now.date()
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    if now.year != int(cfg["event_year"]) and mode not in {"force", "dry"}:
        print(f"year {now.year} != {cfg['event_year']}; silent")
        return 0
    if today.isoformat() > cfg["expire_date"] and mode not in {"force", "dry"}:
        print(f"after {cfg['expire_date']}; silent")
        return 0

    h = cfg["holding"]
    th = cfg["thresholds"]
    g = cfg["guide"]
    src = cfg["sources"]
    alerts: list[tuple[str, str, int]] = []
    add_txt = (f"1차 {h['add1_shares']}주 {h['add1_limit']:.2f}달러 이하, 2차 {h['add2_shares']}주 "
               f"{h['add2_limit']:.2f}달러 이하 지정가")

    # ---- price lines ----
    price, div, qerr = None, None, None
    try:
        price, div, qerr = quote(src["quote"])
    except Exception as exc:  # noqa: BLE001
        qerr = str(exc)
    print(f"price={price} div={div} qerr={qerr}")
    prev_zone = state.get("zone") or "mid"
    if prev_zone not in {"loss", "mid", "rich"}:
        prev_zone = "mid"
    if price is None:
        if state.get("fetch_fail_date") != today.isoformat():
            alerts.append((
                "[AEO] 주가 조회 실패 | 손실 한도 판정 보류",
                body(stamp, f"주가 조회 실패. {qerr or '빈 응답'}. 손실 한도 미도달이라는 뜻이 아닙니다.",
                     "증권 앱에서 AEO 종가를 직접 보세요. 15.69달러 이하면 정리 기준입니다.",
                     "다음 일회 슬롯에서 재시도합니다.",
                     "이번 알림으로 사거나 팔지 마세요.",
                     "조회 실패를 정상으로 취급."),
                4))
            state["fetch_fail_date"] = today.isoformat()
    else:
        loss_line, rearm_loss = th["max_loss_price"], th["loss_rearm_price"]
        rich_line, rearm_rich = th["trim_price"], th["trim_rearm_price"]
        zone = prev_zone
        if price <= loss_line:
            zone = "loss"
        elif price >= rich_line:
            zone = "rich"
        elif (prev_zone == "loss" and price >= rearm_loss) or (prev_zone == "rich" and price <= rearm_rich):
            zone = "mid"
        if zone == "loss" and prev_zone != "loss":
            alerts.append((
                "[AEO] 손실 한도 -20% 도달 | 27주 정리",
                body(stamp, f"{pnl(price, h)}. 손실 한도 {loss_line:.2f}달러(평단 -20%) 이하.",
                     "다음 정규장에서 27주를 지정가로 정리하세요. 직전 종가 근처 지정가, 시장가는 피하세요.",
                     f"정리 뒤 재진입은 3분기 실적에서 환급 제외 영업이익 {g['q3_oi_lo']}M 이상과 AE 동일점포 0% 이상이 확인될 때만.",
                     "정리 기준이라 익절 해당 없음.",
                     "물타기로 평단 낮추기. 한도를 낮춰 버티기. 신용, 미수."),
                5))
        elif zone == "rich" and prev_zone != "rich":
            judged = state.get("q3_verdict")
            alerts.append((
                "[AEO] +20% 도달 | 실적 확인 전이면 절반 익절",
                body(stamp, f"{pnl(price, h)}. 익절 기준 {rich_line:.2f}달러(평단 +20%) 이상. 3분기 판정: {judged or '아직 없음'}.",
                     "3분기 판정이 추가매수(ADD)가 아니면 13주를 지정가로 익절하세요. ADD면 전량 보유.",
                     "남은 14주는 평단 19.61달러 아래로 내려오면 정리 검토.",
                     "판정이 ADD여도 26달러 이상이면 다시 절반 익절 검토.",
                     "급등 추격 매수. 이 가격에서 추가 매수."),
                4))
        elif zone == "mid" and prev_zone == "loss":
            print("loss zone re-armed (silent)")
        else:
            print(f"price zone unchanged ({zone}); silent")
        state["zone"] = zone
        state["last_price"] = price
        if div is None:
            print("dividend unavailable; not treated as cut")
        elif div + 1e-9 < th["div_cut_below"]:
            if not state.get("div_alerted"):
                alerts.append((
                    "[AEO] 배당 축소 신호 | 추가매수 보류",
                    body(stamp, f"연간 배당 {div:.2f}달러, 기준 0.50달러 미만.",
                         "IR 공시로 배당 결정을 확인하세요. 확인 전 추가매수 보류.",
                         "삭감이 사실이면 3분기 판정을 한 단계 낮춰 보세요(추가매수는 보유로, 보유는 절반 축소 검토).",
                         "삭감 확인 시 반등 때 13주 정리 검토.",
                         "시세 배당률만 보고 삭감 단정."),
                    4))
                state["div_alerted"] = True
        else:
            state["div_alerted"] = False

    # ---- IR: date notice + results ----
    items: list[dict] = []
    ir_err = None
    try:
        items = press_list(src["ir_press_list"])
    except Exception as exc:  # noqa: BLE001
        ir_err = str(exc)
        print(f"ir list fail: {exc}")
    q, fy = cfg["event_quarter"], int(cfg["event_fy"])
    if ir_err:
        if state.get("ir_fail_date") != today.isoformat():
            alerts.append((
                "[AEO] IR 조회 실패 | 실적일 미확정 아님",
                body(stamp, f"AEO IR 보도자료 목록 조회 실패. {ir_err}.",
                     "investors.ae.com 보도자료에서 3분기 실적일 공지를 직접 보세요.",
                     "다음 일회 슬롯에서 재시도합니다.",
                     "이번 알림으로 사거나 팔지 마세요.",
                     "조회 실패를 실적일 미정으로 취급."),
                4))
            state["ir_fail_date"] = today.isoformat()
    else:
        day, headline = find_date_notice(items, q, fy)
        print(f"date notice={day} ({headline})")
        if day and (not state.get("q3_date_sent") or state.get("q3_date") != day):
            et_day = datetime.strptime(day, "%Y-%m-%d")
            # Releases come at 16:05 ET after the close, which is the next KST morning about 06:05.
            kst_ready = datetime(et_day.year, et_day.month, et_day.day, tzinfo=KST) + timedelta(days=1, hours=6, minutes=30)
            slot = first_slot_after(cfg, kst_ready)
            if slot is None:
                slot_txt = "실적 후 자동 판정 슬롯이 없습니다. Aside에 슬롯 추가를 요청하세요."
            else:
                lag_h = (slot - kst_ready).total_seconds() / 3600
                slot_txt = f"자동 판정은 {slot.strftime('%m/%d %H:%M')} KST" + (
                    f"(발표 후 약 {lag_h / 24:.0f}일 뒤. 더 빨리 받으려면 Aside에 슬롯 추가 요청)" if lag_h > 30 else "")
            changed = state.get("q3_date_sent") and state.get("q3_date") != day
            alerts.append((
                f"[AEO] 3분기 실적일 {'변경' if changed else '확정'} {et_day.month}/{et_day.day} | 발표 전 추가매수 보류",
                body(stamp, f"회사 공지: {headline}. 미국 {day} 장 마감 후 발표가 관례(한국 다음 날 아침). {pnl(price, h)}.",
                     f"27주 보유 유지. 발표 전 추가매수와 정리 모두 보류. 예외는 종가 {th['max_loss_price']:.2f}달러 이하(손실 한도).",
                     f"{slot_txt}. 기준: 환급 제외 영업이익 {g['q3_oi_lo']}~{g['q3_oi_hi']}M, AE 동일점포 0% 이상, 재고 수량 +{th['inventory_units_max']:.0f}% 미만, 연간 OI {g['fy_oi_lo']}~{g['fy_oi_hi']}M 유지.",
                     f"발표 전 {th['trim_price']:.2f}달러 이상이면 13주 익절.",
                     "실적 기대감으로 미리 추가매수. 9월 실적 다음 날 -14% 전례가 있습니다."),
                4))
            state["q3_date"] = day
            state["q3_date_sent"] = True

        res_head = find_results_headline(items, q, fy)
        text, link = None, None
        if res_head:
            try:
                text, link = wp_release_text(src["wp_search"], q, cfg["results_after"])
            except Exception as exc:  # noqa: BLE001
                print(f"wp fail: {exc}")
        print(f"results headline={res_head} wp={'yes' if text else 'no'}")
        if res_head:
            parsed = parse_release(text) if text else {}
            v, reasons, missing = verdict(parsed, cfg) if text else ("PENDING", [], ["보도자료 본문"])
            fingerprint = json.dumps({"v": v, "p": parsed}, sort_keys=True, ensure_ascii=False)
            print(f"parsed={json.dumps(parsed, ensure_ascii=False)}")
            print(f"verdict={v} reasons={reasons} missing={missing}")
            if fingerprint != state.get("q3_fingerprint"):
                extra = []
                if parsed.get("aerie_comp") is not None:
                    extra.append(f"Aerie 동일점포 {parsed['aerie_comp']:+.0f}%")
                if parsed.get("total_comp") is not None:
                    extra.append(f"전체 {parsed['total_comp']:+.0f}%")
                if parsed.get("gm_bp_ex") is not None:
                    extra.append(f"환급 제외 매출총이익률 {parsed['gm_bp_ex']:+.0f}bp")
                if parsed.get("oi_refund_m"):
                    extra.append(f"영업이익 중 환급 {parsed['oi_refund_m']:.0f}M 차감")
                status = f"{res_head}. " + ", ".join(reasons + extra) + f". {pnl(price, h)}."
                if missing:
                    status += f" 원문 대조 필요 항목: {', '.join(missing)}."
                tail = f" 원문: {link}" if link else ""
                if v == "SELL":
                    alerts.append((
                        "[AEO] 3분기 실적 미달 | 27주 정리",
                        body(stamp, status + tail,
                             "다음 정규장에서 27주를 지정가로 정리하세요. 발표 직후 급락이면 첫 30분은 피하고 지정가로.",
                             "정리 후 재진입은 다음 분기에 환급 제외 이익과 AE 동일점포가 함께 회복될 때만.",
                             "정리 판정이라 익절 해당 없음.",
                             "낮아진 PER만 보고 버티거나 물타기. 신용, 미수."),
                        5))
                elif v == "ADD":
                    alerts.append((
                        "[AEO] 3분기 조건 충족 | 지정가 분할 추가 가능",
                        body(stamp, status + tail,
                             f"보유 27주 유지. 추가는 {add_txt}로만 넣으세요(최대 27주, 총 54주).",
                             f"발표 다음 날 급등으로 지정가가 안 닿으면 추가하지 않고 보유만. 손실 한도 {th['max_loss_price']:.2f}달러는 그대로.",
                             f"{th['trim_price']:.2f}달러 이상이면 전량 보유, 26달러 이상이면 절반 익절 검토.",
                             "평단 19.61달러 위 추격 매수. 한 번에 몰아 사기."),
                        4))
                elif v == "HOLD":
                    alerts.append((
                        "[AEO] 3분기 혼조 | 27주 보유, 추가매수 보류",
                        body(stamp, status + tail,
                             "27주 보유만 하세요. 추가매수와 정리 모두 하지 않습니다.",
                             f"4분기 실적(3월 초)에서 다시 판정. 손실 한도 {th['max_loss_price']:.2f}달러는 계속 적용.",
                             f"{th['trim_price']:.2f}달러 이상이면 13주 익절.",
                             "Aerie만 보고 추가매수. 주가 반응만으로 판정 바꾸기."),
                        4))
                else:
                    alerts.append((
                        "[AEO] 3분기 발표됨 | 수치 원문 대조 전 보유",
                        body(stamp, status + tail,
                             "27주 보유. 보도자료 원문에서 영업이익, 환급 금액, AE 동일점포를 직접 보세요.",
                             "다음 일회 슬롯에서 다시 읽어 판정을 보냅니다.",
                             f"{th['trim_price']:.2f}달러 이상이면 13주 익절.",
                             "숫자가 덜 읽힌 상태에서 사거나 팔기."),
                        3))
                state["q3_fingerprint"] = fingerprint
                state["q3_verdict"] = v
                state["q3_parsed"] = parsed

    topic = resolve_topic("port", cfg.get("ntfy_topic"))
    server = resolve_server(cfg.get("ntfy_server"))
    for title, msg, pri in alerts:
        print(title)
        print(msg)
        print(f"topic={topic} send={not dry}")
        if not dry:
            publish(topic, title, msg, pri, ["chart_with_upwards_trend"], server)
    if not alerts:
        print("no alert")
    if dry:
        print("DRY RUN: no ntfy, state not written")
        return 0
    state["updated_at"] = now.isoformat()
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
