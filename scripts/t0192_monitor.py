#!/usr/bin/env python3
"""0192T0 change-condition watch. Weekday 15:20 KST only. Silent unless a flag changes."""
from __future__ import annotations

import json
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parents[1]
CFG_PATH = ROOT / "configs" / "t0192.json"
STATE_PATH = ROOT / "state" / "t0192.json"
UA = {"User-Agent": "Mozilla/5.0 cwally-alerts"}
NAMES = {"196170": "알테오젠", "086520": "에코프로", "247540": "에코프로비엠"}


def load_json(path: Path, fallback: dict) -> dict:
    if not path.exists():
        return dict(fallback)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        fallback.update(data)
        return fallback
    except json.JSONDecodeError:
        return fallback


def http_get(url: str, timeout: int = 20) -> str:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def num(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).replace(",", "").replace("%", "").replace("억", "").strip()
    if not text or text in {"-", "N/A"}:
        return None
    sign = -1 if text.startswith("-") else 1
    text = text.lstrip("+-")
    try:
        return sign * float(text)
    except ValueError:
        return None


def quote(symbol: str) -> dict:
    payload = json.loads(http_get(f"https://m.stock.naver.com/api/stock/{symbol}/basic"))
    price = num(payload.get("closePrice"))
    if price is None:
        raise ValueError(f"no price {symbol}")
    return {
        "price": price,
        "chg": num(payload.get("compareToPreviousClosePrice")),
        "pct": num(payload.get("fluctuationsRatio")),
        "name": payload.get("stockName") or NAMES.get(symbol, symbol),
    }


def etf_detail(symbol: str) -> dict:
    payload = json.loads(http_get(f"https://m.stock.naver.com/api/etf/{symbol}/basic"))
    return {
        "inav": num(payload.get("iNav")),
        "nav": num(payload.get("nav")),
        "aum_uk": num(payload.get("totalNav")),
    }


def trend(symbol: str) -> list[dict]:
    raw = json.loads(http_get(f"https://m.stock.naver.com/api/stock/{symbol}/trend?pageSize=5"))
    rows = raw if isinstance(raw, list) else raw.get("result") or []
    out = []
    for row in rows:
        out.append(
            {
                "date": str(row.get("bizdate") or ""),
                "foreign": num(row.get("foreignerPureBuyQuant")),
                "organ": num(row.get("organPureBuyQuant")),
                "indiv": num(row.get("individualPureBuyQuant")),
                "close": num(row.get("closePrice")),
            }
        )
    return out


def publish(topic: str, title: str, message: str, priority: int) -> None:
    if topic not in {"cwally-port", "cwally-ops"}:
        topic = "cwally-port"
    payload = json.dumps(
        {
            "topic": topic,
            "title": title,
            "message": message,
            "priority": priority,
            "tags": ["chart_with_upwards_trend"],
        }
    ).encode()
    req = urllib.request.Request(
        "https://ntfy.sh",
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        print(resp.read().decode())


def write_state(prev: dict, **updates) -> None:
    prev.update(updates)
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(prev, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def contrib(base: float | None, now: float | None, weight: float) -> float | None:
    if not base or now is None:
        return None
    return (now - base) / base * (weight / 100.0) * 100.0


def main() -> int:
    dry = "--dry-run" in sys.argv
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    today = now.strftime("%Y-%m-%d")
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    if now.year != int(cfg.get("year_guard") or 2026) or today > cfg.get("valid_until", "2026-12-31"):
        print("year guard or expired; silent")
        return 0

    prev = load_json(
        STATE_PATH,
        {
            "updated_at": None,
            "baseline_done": False,
            "flags": {},
            "flow_streak": 0,
            "last_fail_date": None,
            "last_action": None,
        },
    )
    errors = []
    etf = detail = rows = names = None
    try:
        etf = quote(cfg["code"])
    except Exception as exc:
        errors.append(f"etf {exc}")
    try:
        detail = etf_detail(cfg["code"])
    except Exception as exc:
        errors.append(f"detail {exc}")
    try:
        rows = trend(cfg["code"])
    except Exception as exc:
        errors.append(f"flow {exc}")
    names = {}
    for code in ("196170", "086520", "247540"):
        try:
            names[code] = quote(code)
        except Exception as exc:
            errors.append(f"{code} {exc}")

    if etf is None:
        if prev.get("last_fail_date") != today and not dry:
            publish(
                "cwally-ops",
                "[0192T0] 조회 실패 | 재실행 필요",
                "\n".join(
                    [
                        f"{stamp} | 0192T0 변경조건",
                        "이게 뭐 알림: ETF 시세 조회가 실패했습니다. 조건 미충족이 아닙니다.",
                        f"지금 상태: {' | '.join(errors) or '시세 없음'}",
                        "지금 할 일: 다음 평일 15:20 재실행을 기다립니다.",
                        "나중에: 시세가 오면 기준점 또는 변경 조건을 다시 봅니다.",
                        "익절/줄일 때: 해당 없음.",
                        "하지 말 것: 실패를 관망으로 보지 말 것.",
                    ]
                ),
                4,
            )
        write_state(prev, updated_at=stamp, last_fail_date=today, last_action="DATA_FAIL", errors=errors)
        print("price fetch failed", errors)
        return 1

    price = etf["price"]
    inav = (detail or {}).get("inav")
    nav = (detail or {}).get("nav")
    ref = inav or nav
    premium = (price - ref) / ref * 100.0 if ref else None
    aum = (detail or {}).get("aum_uk")
    latest = rows[0] if rows else None
    both_buy = bool(latest and (latest.get("foreign") or 0) > 0 and (latest.get("organ") or 0) > 0)
    streak = int(prev.get("flow_streak") or 0)
    if latest and latest.get("date") and latest.get("date") != prev.get("flow_date"):
        streak = streak + 1 if both_buy else 0
    elif latest is None:
        streak = int(prev.get("flow_streak") or 0)

    if not prev.get("baseline_done"):
        bases = {code: row["price"] for code, row in names.items()}
        write_state(
            prev,
            updated_at=stamp,
            baseline_done=True,
            baseline_date=today,
            baseline_price=price,
            baseline_names=bases,
            baseline_aum=aum,
            flags={},
            flow_streak=1 if both_buy else 0,
            flow_date=(latest or {}).get("date"),
            last_action="BASELINE",
            errors=errors,
        )
        print(f"baseline saved; silent price={price} premium={premium} aum={aum} flow={both_buy}")
        return 0

    weights = cfg["down"]["weights"]
    bases = prev.get("baseline_names") or {}
    hit = float(cfg["down"]["name_hit_pct"])
    alt = contrib(bases.get("196170"), (names.get("196170") or {}).get("price"), weights["196170"])
    eco = None
    eco_parts = []
    for code in ("086520", "247540"):
        part = contrib(bases.get(code), (names.get(code) or {}).get("price"), weights[code])
        eco_parts.append(part)
    if all(part is not None for part in eco_parts):
        eco = sum(eco_parts)
    flags = {
        "UP_RETEST": price <= float(cfg["up"]["retest_price"]) and both_buy,
        "DOWN_ALT": alt is not None and alt <= -hit,
        "DOWN_ECO": eco is not None and eco <= -hit,
        "GAP": premium is not None and abs(premium) >= float(cfg["down"]["gap_abs_pct"]),
        "AUM": aum is not None and prev.get("baseline_aum") and aum <= float(prev["baseline_aum"]) * (1 - float(cfg["down"]["aum_drop_pct"]) / 100.0),
        "FLOW2": streak >= int(cfg["counter"]["flow_streak"]) and price > float(cfg["up"]["retest_price"]),
    }
    old = prev.get("flags") or {}
    fresh = [key for key, on in flags.items() if on and not old.get(key)]
    write_state(
        prev,
        updated_at=stamp,
        price=price,
        premium_pct=premium,
        aum_uk=aum,
        alt_contrib=alt,
        eco_contrib=eco,
        flags=flags,
        flow_streak=streak,
        flow_date=(latest or {}).get("date") or prev.get("flow_date"),
        last_action="ALERT" if fresh else "WATCH",
        errors=errors,
        last_fail_date=prev.get("last_fail_date"),
    )
    if not fresh:
        print(f"no 0192T0 event; silent price={price} flags={flags} errors={errors}")
        return 0

    px = f"{price:,.0f}원"
    prem_txt = f"{premium:+.2f}%" if premium is not None else "확인 불가"
    aum_txt = f"{aum:.0f}억" if aum is not None else "확인 불가"
    alt_txt = f"{alt:+.2f}%p" if alt is not None else "확인 불가"
    eco_txt = f"{eco:+.2f}%p" if eco is not None else "확인 불가"
    flow_txt = "수급 확인 불가"
    if latest:
        flow_txt = f"{latest['date']} 외국인 {latest['foreign']:+,.0f}주 / 기관 {latest['organ']:+,.0f}주"
    if "DOWN_ALT" in fresh or "DOWN_ECO" in fresh:
        title = "[0192T0] 종목 기여 -5%p | 매력 하향"
        action = "알테오젠 또는 에코프로 계열 기여가 기준 대비 -5%p입니다. 위성 비중도 줄일지 점검하고 추가매수는 보류합니다."
    elif "AUM" in fresh:
        title = "[0192T0] 순자산 10% 유출 | 규모 점검"
        action = "순자산이 기준 대비 10% 이상 줄었습니다. 호가와 괴리를 보고 신규 매수를 보류합니다."
    elif "GAP" in fresh:
        title = "[0192T0] 괴리 1% 초과 | 체결 보류"
        action = "시장가와 실시간 순자산가치 괴리가 1%를 넘었습니다. 괴리가 좁아지기 전 체결하지 않습니다."
    elif "UP_RETEST" in fresh:
        title = "[0192T0] 7,000 재테스트+수급 | 상향 후보"
        action = "7,000원 이하이고 외국인·기관 동반 순매수입니다. 매력 상향 후보지만 추격은 금지하고 위성만 검토합니다."
    else:
        title = "[0192T0] 외인·기관 2일 순매수 | 추격 금지"
        action = "반대 근거인 수급만 확인됐습니다. 정책 기대를 매수 신호로 보지 않습니다."
    lines = [
        f"{stamp} | 0192T0 변경조건",
        "이게 뭐 알림: SOL 코스닥TOP10 판단 변경 조건입니다. 매수 신호가 아닙니다.",
        f"지금 상태: {px} | 괴리 {prem_txt} | 순자산 {aum_txt} | 알테오젠 {alt_txt} | 에코프로계열 {eco_txt} | {flow_txt}",
        f"지금 할 일: {action}",
        "나중에: 실적이 멀티플을 받치는지는 공시로 따로 확인합니다. 이 알림은 그 판단을 대신하지 않습니다.",
        "익절/줄일 때: 종목 기여 -5%p, 순자산 10% 유출, 괴리 1% 초과면 비중을 줄일지 점검합니다.",
        "하지 말 것: 추격매수, 신용, 미수. 15:20 가격을 확정 종가로 보기. 수급 2일을 정책 확정으로 보기.",
    ]
    if errors:
        lines.append("부분 실패: " + " | ".join(errors))
    msg = "\n".join(lines)
    print(msg)
    if not dry:
        publish("cwally-port", title, msg, 4 if any(k in fresh for k in ("DOWN_ALT", "DOWN_ECO", "AUM")) else 3)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
