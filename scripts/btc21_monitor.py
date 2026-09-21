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
CFG_PATH = ROOT / "configs" / "btc21.json"
STATE_PATH = ROOT / "state" / "btc21.json"
UA = {"User-Agent": "Mozilla/5.0 cwally-alerts"}
FARSIDE_MONTH = {
    "Jan": "01",
    "Feb": "02",
    "Mar": "03",
    "Apr": "04",
    "May": "05",
    "Jun": "06",
    "Jul": "07",
    "Aug": "08",
    "Sep": "09",
    "Oct": "10",
    "Nov": "11",
    "Dec": "12",
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
        "final_notified": False,
        "missing_notified": 0,
        "fail_notified": False,
        "verdict": None,
        "updated_at": None,
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
    if not txt or txt in {"-", "—"}:
        return None
    neg = txt.startswith("(") and txt.endswith(")")
    txt = txt.strip("()")
    try:
        val = float(txt)
    except ValueError:
        return None
    return -val if neg else val


def etf_from_farside(html: str, target: str) -> dict | None:
    want = datetime.strptime(target, "%Y-%m-%d")
    pat = re.compile(
        r"(\d{1,2})\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(20\d{2}).{0,400}?(-?\(?[\d.,]+\)?|\u2014|-)",
        re.I | re.S,
    )
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
        rows.append((iso, total, cells))
    for iso, total, cells in rows:
        if iso == target and total is not None:
            return {"date": iso, "total_m": total, "source": "https://farside.co.uk/btc/", "raw": cells[-1]}
    _ = want, pat
    return None


def etf_from_sosovalue(html: str, target: str) -> dict | None:
    want = datetime.strptime(target, "%Y-%m-%d")
    alts = {
        target,
        want.strftime("%b %d, %Y"),
        want.strftime("%d %b %Y"),
        f"{want.month}/{want.day}/2026",
    }
    blob = re.sub(r"<[^>]+>", " ", html)
    blob = re.sub(r"\s+", " ", blob)
    for label in alts:
        m = re.search(re.escape(label) + r".{0,160}(-?\$?[\d,.]+)\s*(million|m|USD)?", blob, re.I)
        if not m:
            continue
        total = parse_num(m.group(1))
        if total is None:
            continue
        return {
            "date": target,
            "total_m": total,
            "source": "https://sosovalue.com/assets/etf/us-btc-spot",
            "raw": m.group(0)[:120],
        }
    return None


def fetch_etf(cfg: dict) -> tuple[dict | None, list[str]]:
    errors = []
    target = cfg["target_etf_date"]
    try:
        html = fetch(cfg["sources"]["etf"][0])
        row = etf_from_farside(html, target)
        if row:
            return row, errors
        errors.append("farside: 9월 21일 행 없음")
    except Exception as exc:
        errors.append(f"farside 조회 실패: {exc}")
    try:
        html = fetch(cfg["sources"]["etf"][1])
        row = etf_from_sosovalue(html, target)
        if row:
            return row, errors
        errors.append("sosovalue: 9월 21일 숫자 없음")
    except Exception as exc:
        errors.append(f"sosovalue 조회 실패: {exc}")
    return None, errors


def fetch_price(cfg: dict) -> tuple[float | None, str, list[str]]:
    errors = []
    try:
        data = fetch_json(cfg["sources"]["price"][0])
        last = float(data["lastPrice"])
        high = float(data.get("highPrice") or last)
        return last, f"binance last {last:.0f} / 24h high {high:.0f}", errors
    except Exception as exc:
        errors.append(f"binance 실패: {exc}")
    try:
        data = fetch_json(cfg["sources"]["price"][1])
        last = float(data["data"]["amount"])
        return last, f"coinbase spot {last:.0f}", errors
    except Exception as exc:
        errors.append(f"coinbase 실패: {exc}")
    return None, "", errors


def senate_status(cfg: dict) -> tuple[str, str, list[str]]:
    errors = []
    texts = []
    sources_ok = []
    for url in cfg["sources"]["senate"]:
        try:
            texts.append((url, fetch(url)))
            sources_ok.append(url)
        except Exception as exc:
            errors.append(f"상원 조회 실패 {url}: {exc}")
    if not texts:
        return "FETCH_FAIL", "상원 페이지를 열지 못했습니다.", errors
    blob = " ".join(re.sub(r"<[^>]+>", " ", html) for _, html in texts)
    blob = re.sub(r"\s+", " ", blob)
    day_out = bool(re.search(r"Sep(?:tember)?\s*21.{0,80}(day\s*out|not in session|recess)", blob, re.I)) or bool(
        re.search(r"Sep(?:tember)?\s*22.{0,80}(day\s*out|not in session|recess)", blob, re.I)
    )
    scheduled = bool(
        re.search(
            r"(clarity|h\.?r\.?\s*3633|cloture).{0,80}(sep(?:tember)?\s*22|9\s*/\s*22|22\s+sep)",
            blob,
            re.I,
        )
        or re.search(
            r"(sep(?:tember)?\s*22|9\s*/\s*22|22\s+sep).{0,80}(clarity|h\.?r\.?\s*3633|cloture)",
            blob,
            re.I,
        )
    )
    if scheduled and not day_out:
        return "SCHEDULED", f"공식 페이지에 9월 22일 Clarity/제3633 일정이 보입니다. 출처: {sources_ok[0]}", errors
    if day_out:
        return "NOT_SCHEDULED", "상원 잠정 일정에 9월 21~22일이 day out으로 나옵니다. 내일 2:15 ET 표결 소문과 맞지 않습니다.", errors
    return "NOT_SCHEDULED", "9월 22일 Clarity 재표결을 확정하는 공식 일정은 없습니다.", errors


def verdict(price: float | None, etf: dict | None, senate: str, hold: float) -> str:
    if price is None:
        return "FETCH_FAIL"
    if etf is None:
        return "DATA_MISSING"
    held = price >= hold
    inflow = etf["total_m"] > 0
    if senate == "SCHEDULED" and held:
        return "HOLD_OFFICIAL"
    if senate == "SCHEDULED" and not held:
        return "FADE_OFFICIAL"
    if held and inflow:
        return "HOLD"
    if not held and not inflow:
        return "FADE"
    if held and not inflow:
        return "HOLD_WEAK"
    return "FADE_WEAK"


def copy_for(kind: str, cfg: dict, price, etf, senate_label, senate_note) -> tuple[str, list[str], int]:
    px = f"{price:,.0f}달러" if price is not None else "없음"
    etf_txt = "9월 21일 집계 없음"
    if etf:
        sign = "+" if etf["total_m"] > 0 else ""
        etf_txt = f"9월 21일 순{sign}{etf['total_m']:.1f}억달러 ({etf['source']})"
    explain = cfg["explain"]
    if kind == "FETCH_FAIL":
        return (
            "[CLARITY] 조회 실패 | 판정 보류",
            [
                f"이게 무 알림: {explain}",
                "지금 결과: 가격 또는 상원 조회가 실패했습니다. 실패를 정상으로 처리하지 않습니다.",
                "지금 할 일: 직접 시세와 상원 일정을 확인하세요. 추격 금지.",
                "나중에: 오후 15:20 KST 슬롯에서 한 번 더 봅니다.",
                "익절/줄일 때: 이 알림으로 팔지 마세요.",
                "하지 말 것: 조회 실패를 호재/악재로 보기.",
            ],
            4,
        )
    if kind == "DATA_MISSING":
        return (
            "[CLARITY] ETF 숫자 없음 | 판정 보류",
            [
                f"이게 무 알림: {explain}",
                f"지금 결과: BTC {px}. 9월 21일 현물 ETF 순유입이 아직 없습니다. 상원: {senate_label}.",
                "지금 할 일: 판정을 밀지 말고 추격하지 마세요.",
                "나중에: 15:20 KST에 한 번 더 봅니다. 숫자 없으면 그때도 유보입니다.",
                "익절/줄일 때: 이 알림으로 팔지 마세요.",
                "하지 말 것: 없는 ETF 숫자를 맞춰 넣기. 소문을 공식 일정으로 취급.",
                f"상원 메모: {senate_note}",
            ],
            3,
        )
    titles = {
        "HOLD": "[CLARITY] 고점 유지 | 소문 아님",
        "HOLD_WEAK": "[CLARITY] 가격만 유지 | ETF 약함",
        "HOLD_OFFICIAL": "[CLARITY] 공식 일정 있음 | 추격 금지",
        "FADE": "[CLARITY] 헤드라인 반락 | 추격 금지",
        "FADE_WEAK": "[CLARITY] 가격 약해 | 소문 아님",
        "FADE_OFFICIAL": "[CLARITY] 공식 있는데 반락 | 관찰",
    }
    now_do = {
        "HOLD": "8만2천달러 위에 있고 9월 21일 ETF도 순유입입니다. 소문 추격은 하지 마세요.",
        "HOLD_WEAK": "가격은 겨넘지만 9월 21일 ETF는 순유입이 아닙니다. 새 매수는 보류하세요.",
        "HOLD_OFFICIAL": "공식 일정이 있습니다. 결과 전에 추격하지 마세요.",
        "FADE": "가격이 8만2천달러 아래로 돌아갔고 ETF도 받쳐주지 않았습니다. 헤드라인 반락으로 봅니다.",
        "FADE_WEAK": "가격은 약해지만 ETF는 순유입입니다. 반락이라고 단정하지 말고 추격만 금지하세요.",
        "FADE_OFFICIAL": "공식 일정은 있는데 가격은 8만2천달러을 지키지 못했습니다. 결과를 보고 다시 판정하세요.",
    }
    return (
        titles[kind],
        [
            f"이게 무 알림: {explain}",
            f"지금 결과: {kind} | BTC {px} | ETF {etf_txt} | 상원 {senate_label}.",
            f"지금 할 일: {now_do[kind]}",
            "나중에: 이 작업은 오늘자 일회성입니다. 내일 부터는 다시 안 보냅니다.",
            "익절/줄일 때: 8만2천달러를 기준으로 밥니다. 이 알림으로 팔지 마세요.",
            "하지 말 것: 소문 추격, 신용/미수, 없는 재표결을 기정에 넣기.",
            f"상원 메모: {senate_note}",
        ],
        5 if kind.startswith("FADE") or kind.endswith("OFFICIAL") else 4,
    )


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
    if state.get("final_notified"):
        print("already sent final; skip")
        return 0

    etf, etf_err = fetch_etf(cfg)
    price, price_note, price_err = fetch_price(cfg)
    senate, senate_note, senate_err = senate_status(cfg)
    kind = verdict(price, etf, senate, float(cfg["hold_usd"]))
    if senate == "FETCH_FAIL" and price is None:
        kind = "FETCH_FAIL"

    title, body, pri = copy_for(kind, cfg, price, etf, senate, senate_note)
    lines = [
        f"{now.strftime('%Y-%m-%d %H:%M KST')} | CLARITY | BTC ETF+Senate",
        *body,
    ]
    if price_note:
        lines.append(f"가격 메모: {price_note}")
    errs = etf_err + price_err + senate_err
    if errs:
        lines.append("조회 메모: " + " / ".join(errs[:4]))
    msg = "\n".join(lines)
    print(msg)
    print(f"verdict={kind} dry={dry}")

    send = False
    if kind == "FETCH_FAIL":
        send = not state.get("fail_notified")
        if send and not dry:
            state["fail_notified"] = True
    elif kind == "DATA_MISSING":
        send = int(state.get("missing_notified") or 0) < 2
        if send and not dry:
            state["missing_notified"] = int(state.get("missing_notified") or 0) + 1
    else:
        send = True
        if not dry:
            state["final_notified"] = True
            state["missing_notified"] = 2

    if dry:
        print("dry-run; no ntfy")
    elif send:
        publish(
            resolve_topic("port", cfg.get("ntfy_topic")),
            title,
            msg,
            pri,
            ["classical_building", "chart_with_upwards_trend"],
            resolve_server(cfg.get("ntfy_server")),
        )
    else:
        print("no ntfy; duplicate suppressed")

    state.update(
        {
            "verdict": kind,
            "price": price,
            "etf": etf,
            "senate": senate,
            "updated_at": now.isoformat(),
        }
    )
    if not dry:
        save_state(state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
