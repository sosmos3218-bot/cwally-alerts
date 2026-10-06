#!/usr/bin/env python3
"""AEO watch: $15 entry and the three flip conditions. Silent unless change."""
from __future__ import annotations

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
CFG_PATH = ROOT / "configs" / "aeo.json"
STATE_PATH = ROOT / "state" / "aeo.json"
UA = {"User-Agent": "cwally-alerts aeo research monitor research@example.com"}


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
    raw = fetch(url)
    data = json.loads(raw)
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


def visible(html: str) -> str:
    text = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", html)
    text = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text)


def latest_sec_date(url: str) -> str | None:
    raw = fetch(url)
    data = json.loads(raw)
    recent = ((data.get("filings") or {}).get("recent") or {})
    forms = recent.get("form") or []
    dates = recent.get("filingDate") or []
    for form, day in zip(forms, dates):
        if form in {"8-K", "10-Q", "10-K"}:
            return day
    return None


def signed_pct(blob: str, label: str) -> float | None:
    m = re.search(
        label + r"[^.]{0,90}?(decreas\w*|declin\w*|down|fell)[^.]{0,40}?(\d{1,2}(?:\.\d+)?)\s*percent",
        blob,
        re.I,
    )
    if m:
        return -float(m.group(2))
    m = re.search(
        label + r"[^.]{0,90}?(increas\w*|grew|up|rose)[^.]{0,40}?(\d{1,2}(?:\.\d+)?)\s*percent",
        blob,
        re.I,
    )
    if m:
        return float(m.group(2))
    m = re.search(label + r"[^.]{0,40}?(flat|unchanged|approximately flat)", blob, re.I)
    if m:
        return 0.0
    return None


def parse_earnings(blob: str) -> dict:
    ae = signed_pct(blob, r"american eagle comparable sales")
    if ae is None:
        ae = signed_pct(blob, r"american eagle brand comparable")
    aerie = signed_pct(blob, r"aerie comparable sales")
    units = None
    m = re.search(
        r"units[^.]{0,40}?(?:up|increas\w*)[^.]{0,20}?(\d{1,2}(?:\.\d+)?)\s*percent",
        blob,
        re.I,
    )
    if m:
        units = float(m.group(1))
    else:
        m = re.search(
            r"units[^.]{0,40}?(?:down|decreas\w*)[^.]{0,20}?(\d{1,2}(?:\.\d+)?)\s*percent",
            blob,
            re.I,
        )
        if m:
            units = -float(m.group(1))
    margin = None
    m = re.search(r"operating margin[^.]{0,40}?(\d{1,2}(?:\.\d+)?)\s*percent", blob, re.I)
    if m:
        margin = float(m.group(1))
    refund = bool(re.search(r"tariff refund", blob, re.I))
    return {
        "ae_comp": ae,
        "aerie_comp": aerie,
        "inventory_units": units,
        "op_margin": margin,
        "tariff_refund": refund,
    }


def body(stamp: str, status: str, action: str, later: str, trim: str, avoid: str) -> str:
    return "\n".join(
        [
            f"{stamp} | AEO | 변경조건 점검",
            "이게 뭐 알림: 아메리칸 이글 관망의 반대 근거와 변경 조건만 보는 알림입니다. 매수 지시가 아닙니다.",
            f"지금 결과/상태: {status}",
            f"지금 할 일: {action}",
            f"나중에: {later}",
            f"익절/줄일 때: {trim}",
            f"하지 말 것: {avoid}",
        ]
    )


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    dry = mode == "dry" or os.environ.get("AEO_DRY") == "1"
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    state = load_json(
        STATE_PATH,
        {
            "updated_at": None,
            "last_price": None,
            "zone": "above",
            "entry_alerted": False,
            "div_alerted": False,
            "last_filing": cfg.get("last_known_filing"),
            "earnings_fingerprint": None,
            "fetch_fail_date": None,
            "sec_fail_date": None,
        },
    )
    now = datetime.now(KST)
    today = now.date()
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    if now.year != int(cfg["event_year"]) and mode not in {"force", "dry"}:
        print(f"year {now.year} != {cfg['event_year']}; silent")
        return 0
    if today.isoformat() > cfg["expire_date"] and mode not in {"force", "dry"}:
        print(f"after {cfg['expire_date']}; silent")
        return 0

    th = cfg["thresholds"]
    base = cfg["baseline"]
    alerts: list[tuple[str, str, int]] = []
    price, div, qerr = None, None, None
    try:
        price, div, qerr = quote(cfg["sources"]["quote"])
    except Exception as exc:
        qerr = str(exc)
    print(f"price={price} div={div} qerr={qerr}")

    if price is None:
        if state.get("fetch_fail_date") != today.isoformat():
            alerts.append(
                (
                    "[AEO] 주가 조회 실패 | 15달러 미확인",
                    body(
                        stamp,
                        f"주가 조회 실패. {qerr or '빈 응답'}. 조건 미충족이 아닙니다.",
                        "야후나 증권 앱에서 AEO 종가를 직접 보세요. 15달러를 이 알림으로 판정하지 마세요.",
                        "다음 일회 슬롯에서 재시도합니다. 슬롯이 아니면 직접 확인.",
                        "이번 알림으로 줄이거나 사지 마세요.",
                        "조회 실패를 15달러 이탈이나 정상으로 취급.",
                    ),
                    4,
                )
            )
            state["fetch_fail_date"] = today.isoformat()
    else:
        zone = "buy" if price <= th["entry_price"] else "above"
        prev = state.get("zone") or "above"
        if zone == "buy" and prev != "buy":
            alerts.append(
                (
                    "[AEO] 15달러 이하 | 분할 관심, 추격 금지",
                    body(
                        stamp,
                        f"AEO {price:.2f}달러. 기준일 {base['price_date']} {base['price']}달러. 진입 구간 15달러 이하. 52주 저점 14.06달러에서 이미 반등한 상태라 추세 매수 근거는 아닙니다.",
                        "새 돈은 한 번에 넣지 마세요. 변경 조건 3개 중 2개가 실적에서 확인되기 전엔 관심만.",
                        "본진 동일점포 플러스, 재고 수량 증가 10% 미만, 환급 제외 영업이익률 8% 안팎 중 2개.",
                        "15.50달러 위로 복귀하면 가격 관심은 해제. 보유 손절 신호는 아닙니다.",
                        "15달러 터치를 매수 확정으로 쓰기. 신용, 미수, 추격.",
                    ),
                    4,
                )
            )
            state["entry_alerted"] = True
        elif zone == "above" and prev == "buy" and price >= th["exit_price"]:
            alerts.append(
                (
                    "[AEO] 15.50달러 위 | 진입 관심 해제",
                    body(
                        stamp,
                        f"AEO {price:.2f}달러. 15달러 진입 구간에서 벗어났습니다.",
                        "가격 관심만 끄세요. 관망 유지.",
                        "실적 3조건은 다음 공시에서 따로 보세요.",
                        "이번 반등으로 비중을 늘리지 마세요.",
                        "15달러 이탈 해제를 실적 개선으로 단정.",
                    ),
                    3,
                )
            )
            state["entry_alerted"] = False
        else:
            print(f"price zone unchanged ({zone}); silent")
        state["zone"] = zone
        state["last_price"] = price

        if div is None:
            print("dividend unavailable; not treated as cut")
        elif div + 1e-9 < th["div_cut_below"]:
            if not state.get("div_alerted"):
                alerts.append(
                    (
                        "[AEO] 배당 축소 | 15달러 하방 근거 약화",
                        body(
                            stamp,
                            f"연간 배당 {div:.2f}달러, 기준 {base['annual_dividend']}달러 미만.",
                            "배당이 받치던 하방 근거를 끄세요. 15달러도 지지로 보지 마세요.",
                            "공시에서 배당 결정을 확인하기 전 분할 관심 보류.",
                            "배당 축소가 원문과 맞으면 보유 재검토.",
                            "시세 배당률만 보고 삭감 단정. 조회 공백을 삭감으로 읽기.",
                        ),
                        5,
                    )
                )
                state["div_alerted"] = True
        else:
            state["div_alerted"] = False

    filing = None
    ferr = None
    try:
        filing = latest_sec_date(cfg["sources"]["sec"])
    except Exception as exc:
        ferr = str(exc)
        print(f"sec fail: {exc}")
    last_filing = state.get("last_filing") or cfg.get("last_known_filing")
    if filing and last_filing and filing > last_filing:
        ir_text = ""
        try:
            ir_text = visible(fetch(cfg["sources"]["ir"]))
        except Exception as exc:
            print(f"ir fail: {exc}")
        parsed = parse_earnings(ir_text) if ir_text else {}
        fingerprint = json.dumps(parsed, sort_keys=True)
        if fingerprint != state.get("earnings_fingerprint"):
            signals = []
            missing = []
            ae = parsed.get("ae_comp")
            units = parsed.get("inventory_units")
            margin = parsed.get("op_margin")
            aerie = parsed.get("aerie_comp")
            if ae is None:
                missing.append("본진 동일점포")
            elif ae > th["ae_comp_flip"]:
                signals.append(f"본진 동일점포 {ae:.1f}%")
            if units is None:
                missing.append("재고 수량")
            elif units < th["inventory_units_max"]:
                signals.append(f"재고 수량 {units:.1f}%")
            if margin is None:
                missing.append("영업이익률")
            elif margin >= 7.5 and not parsed.get("tariff_refund"):
                signals.append(f"영업이익률 {margin:.1f}%")
            elif margin is not None and parsed.get("tariff_refund"):
                missing.append("환급 제외 영업이익률")
            aerie_txt = f"Aerie {aerie:.1f}%" if aerie is not None else "Aerie 확인 불가"
            need = int(th["flip_need"])
            if len(signals) >= need and not missing:
                alerts.append(
                    (
                        "[AEO] 실적 | 변경 조건 충족",
                        body(
                            stamp,
                            f"새 공시 {filing}. 충족 {len(signals)}/3: {', '.join(signals)}. {aerie_txt}.",
                            "가격이 15달러 이하일 때만 분할 관심을 다시 보세요. 한 번에 넣지 마세요.",
                            "Aerie가 15% 미만이면 반대 근거는 약합니다. 원문으로 한 번 더 대조.",
                            "15달러 위면 조건이 켜져도 추격하지 마세요.",
                            "파싱 문구를 실적 확정으로 단정. 신용, 미수.",
                        ),
                        4,
                    )
                )
            else:
                alerts.append(
                    (
                        "[AEO] 실적 | 변경 조건 미충족 또는 미확인",
                        body(
                            stamp,
                            f"새 공시 {filing}. 충족 {len(signals)}/3. 확인 불가: {', '.join(missing) or '없음'}. {aerie_txt}. 환급 문구 {'있음' if parsed.get('tariff_refund') else '없음'}.",
                            "관망 유지. 확인 불가는 미충족이 아닙니다. IR 원문에서 본진 컴프, 재고 수량, 환급 제외 이익률을 대조하세요.",
                            "2개 확인 전 신규 비중 없음. Aerie 15% 이상은 반대 근거일 뿐 매수 전환이 아닙니다.",
                            "이번 알림으로 사지 마세요.",
                            "확인 불가를 충족이나 정상으로 세기. 주가 반응으로 조건 단정.",
                        ),
                        3,
                    )
                )
            state["earnings_fingerprint"] = fingerprint
            state["last_filing"] = filing
            state["last_parsed"] = parsed
    elif ferr and state.get("sec_fail_date") != today.isoformat() and today.isoformat() in {"2026-11-25", "2026-12-02", "2026-12-03"}:
        alerts.append(
            (
                "[AEO] 공시 조회 실패 | 실적 조건 미확인",
                body(
                    stamp,
                    f"SEC 조회 실패. {ferr}. 실적 조건은 미확인입니다.",
                    "aeo-inc.com 최신 실적을 직접 보세요.",
                    "다음 일회 슬롯에서 재시도합니다.",
                    "이번 알림으로 비중을 바꾸지 마세요.",
                    "조회 실패를 조건 미충족으로 두기.",
                ),
                4,
            )
        )
        state["sec_fail_date"] = today.isoformat()

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

    state["updated_at"] = now.isoformat()
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
