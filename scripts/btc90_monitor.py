#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
CFG_PATH = ROOT / "configs" / "btc90.json"
STATE_PATH = ROOT / "state" / "btc90.json"
UA = {"User-Agent": "Mozilla/5.0 cwally-alerts"}
FARSIDE_MONTH = {
    "Jan": "01", "Feb": "02", "Mar": "03", "Apr": "04",
    "May": "05", "Jun": "06", "Jul": "07", "Aug": "08",
    "Sep": "09", "Oct": "10", "Nov": "11", "Dec": "12",
}


def fetch(url: str) -> str:
    import urllib.request
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=25) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def fetch_json(url: str):
    return json.loads(fetch(url))


def load_state() -> dict:
    state = {
        "snapshot_sent": False, "fail_notified": False, "missing_notified": 0,
        "last_kind": None, "last_alert_key": None, "trend_sent": False,
        "break80_sent": False, "break75_sent": False, "fomc_sent": False,
        "clarity_sent": False, "updated_at": None,
    }
    if STATE_PATH.exists():
        try:
            state.update(json.loads(STATE_PATH.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass
    return state


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def parse_num(raw: str) -> float | None:
    txt = raw.strip().replace(",", "").replace("$", "")
    if not txt or txt in {"-", "\u2014"}:
        return None
    neg = txt.startswith("(") and txt.endswith(")")
    txt = txt.strip("()")
    try:
        val = float(txt)
    except ValueError:
        return None
    return -val if neg else val


def closed_from_binance(url: str) -> tuple[str, float]:
    rows = fetch_json(url)
    now_utc = datetime.now(timezone.utc)
    closed = []
    for row in rows:
        ts = datetime.fromtimestamp(int(row[0]) / 1000, timezone.utc)
        if now_utc - ts >= timedelta(hours=24):
            closed.append((ts.strftime("%Y-%m-%d"), float(row[4])))
    if not closed:
        raise RuntimeError("no closed daily")
    return closed[-1]


def closed_from_kraken(url: str) -> tuple[str, float]:
    data = fetch_json(url)
    if data.get("error"):
        raise RuntimeError(",".join(data["error"]))
    result = data.get("result") or {}
    rows = []
    for key, val in result.items():
        if key != "last":
            rows = val
            break
    now_utc = datetime.now(timezone.utc)
    closed = []
    for row in rows:
        ts = datetime.fromtimestamp(int(row[0]), timezone.utc)
        if now_utc - ts >= timedelta(hours=24):
            closed.append((ts.strftime("%Y-%m-%d"), float(row[4])))
    if not closed:
        raise RuntimeError("kraken no closed daily")
    return closed[-1]


def closed_daily(cfg: dict) -> tuple[str | None, float | None, list[str]]:
    errors: list[str] = []
    try:
        day, close = closed_from_binance(cfg["sources"]["daily"])
        return day, close, errors
    except Exception as exc:
        errors.append(f"binance fail: {exc}")
    try:
        day, close = closed_from_kraken(cfg["sources"]["kraken_daily"])
        return day, close, errors
    except Exception as exc:
        errors.append(f"kraken fail: {exc}")
    try:
        spot = float(fetch_json(cfg["sources"]["spot"])["data"]["amount"])
        return None, spot, errors
    except Exception as exc:
        errors.append(f"coinbase fail: {exc}")
        return None, None, errors


def etf_rows(html: str) -> list[tuple[str, float]]:
    rows = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html, flags=re.I | re.S):
        cells = [re.sub(r"<[^>]+>", " ", c) for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, flags=re.I | re.S)]
        cells = [re.sub(r"\s+", " ", c).strip() for c in cells]
        if len(cells) < 2:
            continue
        m = re.match(r"(\d{1,2})\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(20\d{2})$", cells[0], re.I)
        if not m:
            continue
        day, mon, year = m.group(1), m.group(2).title(), m.group(3)
        iso = f"{year}-{FARSIDE_MONTH[mon]}-{int(day):02d}"
        total = parse_num(cells[-1])
        if total is None:
            continue
        raw_cells = " ".join(cells[1:-1])
        if raw_cells.count("-") >= 8 and abs(total) < 1e-9:
            continue
        rows.append((iso, total))
    rows.sort(key=lambda x: x[0])
    return rows


def fetch_etf(cfg: dict) -> tuple[list[tuple[str, float]], list[str]]:
    errors: list[str] = []
    try:
        rows = etf_rows(fetch(cfg["sources"]["etf"]))
        if not rows:
            errors.append("farside empty")
        return rows[-5:], errors
    except Exception as exc:
        errors.append(f"farside fail: {exc}")
        return [], errors


def senate_status(cfg: dict) -> tuple[str, str, list[str]]:
    errors: list[str] = []
    texts = []
    for url in cfg["sources"]["senate"]:
        try:
            texts.append(fetch(url))
        except Exception as exc:
            errors.append(f"senate fail {url}: {exc}")
    if not texts:
        return "FETCH_FAIL", "상원 페이지를 열지 못했습니다.", errors
    blob = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", " ".join(texts)))
    scheduled = bool(
        re.search(r"(clarity|h\.?r\.?\s*3633|cloture).{0,90}(vote|scheduled|consideration)", blob, re.I)
        and re.search(r"(oct(?:ober)?|nov(?:ember)?|10\s*/\s*\d+|11\s*/\s*\d+)", blob, re.I)
    )
    if scheduled:
        return "SCHEDULED", "공식 페이지에 Clarity/H.R.3633 추가 일정 흔적이 있습니다. 날짜를 직접 확인하세요.", errors
    return "NOT_SCHEDULED", "확정된 Clarity 재표결 일정은 없습니다.", errors


def regime(close: float | None, cfg: dict) -> str:
    if close is None:
        return "FETCH_FAIL"
    if close >= float(cfg["confirm_usd"]):
        return "TREND"
    if close >= float(cfg["hold_usd"]):
        return "HOLD"
    if close >= float(cfg["fail_usd"]):
        return "WEAK"
    return "FAIL"


def etf_note(rows: list[tuple[str, float]]) -> tuple[str, str]:
    if not rows:
        return "NONE", "ETF 집계 없음"
    last_date, last_val = rows[-1]
    sign = "+" if last_val > 0 else ""
    streak_in = streak_out = 0
    for _, val in reversed(rows):
        if val > 0:
            if streak_out:
                break
            streak_in += 1
        elif val < 0:
            if streak_in:
                break
            streak_out += 1
        else:
            break
    extra = ""
    if streak_in >= 3:
        extra = f" / {streak_in}거래일 연속 순유입"
    elif streak_out >= 3:
        extra = f" / {streak_out}거래일 연속 순유출"
    return f"{last_date}:{last_val:.1f}", f"{last_date} 순{sign}{last_val:.1f}백만달러{extra}"


def copy_for(kind, cfg, close_date, close, etf_txt, senate_label, senate_note, fomc_on):
    prefix = cfg["title_prefix"]
    px = f"{close:,.0f}달러" if close is not None else "없음"
    day = close_date or "종가일 미확정"
    fomc = cfg["fomc_note"]
    titles = {
        "FETCH_FAIL": f"{prefix} 조회 실패 | 판정 보류",
        "DATA_MISSING": f"{prefix} ETF 숫자 없음 | 가격만 기록",
        "SNAPSHOT": f"{prefix} 추세 미확정 | 9만 대기",
        "TREND": f"{prefix} 9만 종가 | 되돌림 관찰",
        "HOLD": f"{prefix} 8만 유지 | 추세 미확정",
        "WEAK": f"{prefix} 8만 이탈 | 반등 재평가",
        "FAIL": f"{prefix} 7만5천 이탈 | 시나리오 전환",
        "FOMC": f"{prefix} FOMC 창구 | 추격 금지",
        "CLARITY": f"{prefix} 공식 일정 단서 | 추격 금지",
    }
    now_do = {
        "FETCH_FAIL": "직접 시세만 보고, 이 알림으로 사지 마세요.",
        "DATA_MISSING": "가격 국면만 기록하세요. ETF 없이 추세라고 단정하지 마세요.",
        "SNAPSHOT": "지금은 강한 반등입니다. 9만 종가 전에는 추세 전환으로 올리지 마세요.",
        "TREND": "확인선은 넘었습니다. 다음엔 되돌림 때 8만 종가 방어만 보세요.",
        "HOLD": "8만은 지켰습니다. 9만 종가와 ETF 지속 유입을 기다리세요.",
        "WEAK": "8만 종가가 깨졌습니다. 새 추세가 아니라 큰 반등으로 낮추세요.",
        "FAIL": "7만5천 종가까지 잃었습니다. 이 반등 시나리오는 접으세요.",
        "FOMC": "금리 발표 전후입니다. 결과 나오기 전 추격하지 마세요.",
        "CLARITY": "공식 일정 단서만 있습니다. 통과를 기정사실로 사지 마세요.",
    }
    later = {
        "FETCH_FAIL": "다음 평일 09:00 KST에 다시 봅니다.",
        "DATA_MISSING": "ETF가 뜨면 같은 슬롯에서 다시 판정합니다.",
        "SNAPSHOT": "이후에는 9만 종가, 8만/7만5천 이탈, FOMC, 클라리티 공식 일정이 아니면 조용히 넘어갑니다.",
        "TREND": "되돌림 때 8만 종가가 깨지면 다시 알립니다.",
        "HOLD": "9만 종가 또는 8만 이탈 때만 다시 알립니다.",
        "WEAK": "7만5천을 잃거나 8만을 회복하면 다시 알립니다.",
        "FAIL": "이 국면 알림은 한 번만 보냅니다.",
        "FOMC": "10월 29일 점검 후 이 FOMC 창구 알림은 끝입니다.",
        "CLARITY": "확정 표결 날짜가 나오기 전에는 같은 단서로 반복하지 않습니다.",
    }
    body = [
        f"이게 뭐 알림: {cfg['explain']}",
        f"지금 결과: {kind} | UTC 종가 {day} {px} | ETF {etf_txt} | 상원 {senate_label}" + (" | FOMC 창구" if fomc_on else ""),
        f"지금 할 일: {now_do[kind]}",
        f"나중에: {later[kind]} 다음 금리: {fomc}.",
        "익절/줄일 때: 9만 종가+8만 방어면 추세, 8만 이탈이면 반등, 7만5천 이탈이면 시나리오 전환.",
        "하지 말 것: 장중 터치로 판정, 소문 추격, 신용/미수, 조회 실패를 호재/악재로 보기.",
        f"상원 메모: {senate_note}",
    ]
    # 일정 단서(CLARITY/FOMC)는 정보성이라 P3. 시나리오 전환만 P5.
    pri = 5 if kind == "FAIL" else 4 if kind in {"TREND", "WEAK"} else 3
    return titles[kind], body, pri


def pick_kind(regime_now, etf_ok, senate, fomc_on, state):
    if regime_now == "FETCH_FAIL":
        return "FETCH_FAIL"
    if not etf_ok and regime_now in {"HOLD", "TREND", "WEAK", "FAIL"} and not state.get("snapshot_sent"):
        return "DATA_MISSING"
    if senate == "SCHEDULED" and not state.get("clarity_sent"):
        return "CLARITY"
    if fomc_on and not state.get("fomc_sent"):
        return "FOMC"
    if regime_now == "TREND" and not state.get("trend_sent"):
        return "TREND"
    if regime_now == "FAIL" and not state.get("break75_sent"):
        return "FAIL"
    if regime_now == "WEAK" and not state.get("break80_sent"):
        return "WEAK"
    if not state.get("snapshot_sent"):
        return "SNAPSHOT"
    if regime_now == "HOLD" and state.get("last_kind") in {"WEAK", "FAIL"}:
        return "HOLD"
    return "SILENT"


def mark_sent(kind, state):
    if kind == "FETCH_FAIL":
        state["fail_notified"] = True
    elif kind == "DATA_MISSING":
        state["missing_notified"] = int(state.get("missing_notified") or 0) + 1
    elif kind == "SNAPSHOT":
        state["snapshot_sent"] = True
    elif kind == "TREND":
        state["trend_sent"] = True
        state["snapshot_sent"] = True
    elif kind == "WEAK":
        state["break80_sent"] = True
        state["snapshot_sent"] = True
    elif kind == "FAIL":
        state["break75_sent"] = True
        state["break80_sent"] = True
        state["snapshot_sent"] = True
    elif kind == "HOLD":
        state["snapshot_sent"] = True
        state["break80_sent"] = False
    elif kind == "FOMC":
        state["fomc_sent"] = True
        state["snapshot_sent"] = True
    elif kind == "CLARITY":
        state["clarity_sent"] = True
        state["snapshot_sent"] = True


def should_send(kind, state):
    if kind == "SILENT":
        return False
    if kind == "FETCH_FAIL":
        return not state.get("fail_notified")
    if kind == "DATA_MISSING":
        return int(state.get("missing_notified") or 0) < 2
    return True


def main() -> int:
    dry = len(sys.argv) > 1 and sys.argv[1].lower() in {"test", "dry", "dry-run"}
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    today = now.strftime("%Y-%m-%d")
    state = load_state()
    if now.year != int(cfg.get("event_year", 2026)):
        print("year-guard skip")
        return 0
    if today < cfg["window_start_kst"][:10] or today > cfg["expire_date"]:
        print("outside window; silent")
        return 0
    close_date, close, price_err = closed_daily(cfg)
    rows, etf_err = fetch_etf(cfg)
    etf_key, etf_txt = etf_note(rows)
    senate, senate_note, senate_err = senate_status(cfg)
    fomc_on = today in set(cfg.get("fomc_dates") or [])
    regime_now = regime(close, cfg)
    kind = pick_kind(regime_now, bool(rows), senate, fomc_on, state)
    if kind == "DATA_MISSING" and rows:
        kind = "SNAPSHOT" if not state.get("snapshot_sent") else regime_now
    title, body, pri = copy_for(kind if kind != "SILENT" else regime_now, cfg, close_date, close, etf_txt, senate, senate_note, fomc_on)
    if kind == "SILENT":
        title = f"{cfg['title_prefix']} 변동 없음 | 발송 안 함"
    lines = [f"{now.strftime('%Y-%m-%d %H:%M KST')} | BTC90 | 9만/8만/7만5천", *body]
    errs = price_err + etf_err + senate_err
    if errs:
        lines.append("조회 메모: " + " / ".join(errs[:4]))
    msg = "\n".join(lines)
    print(msg)
    print(f"verdict={kind} regime={regime_now} dry={dry}")
    send = should_send(kind, state)
    alert_key = f"{today}|{kind}|{regime_now}|{etf_key}|{senate}"
    if state.get("last_alert_key") == alert_key:
        send = False
    if dry:
        print("dry-run; no ntfy")
    elif send:
        publish(resolve_topic("port", cfg.get("ntfy_topic")), title, msg, pri, ["chart_with_upwards_trend", "orange_circle"], resolve_server(cfg.get("ntfy_server")))
        mark_sent(kind, state)
        state["last_alert_key"] = alert_key
    else:
        print("no ntfy; duplicate suppressed")
    state.update({"last_kind": kind, "regime": regime_now, "close": close, "close_date": close_date, "etf": etf_txt, "senate": senate, "updated_at": now.isoformat()})
    if not dry:
        save_state(state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
