#!/usr/bin/env python3
"""[ASML] price-zone + weekly check (USD ADR primary, 2027-2028 EPS model).

Modes
  slot          morning bundle (weekdays 09:00 KST). Nasdaq ADR close (ASML, USD) ->
                zone. Sends only when the zone changes into an action zone, plus one weekly
                check per ISO week. Data failure -> one ops notice per day.
  dry-slot      same logic, prints only (no ntfy, no state write).
  force-slot    send the current status even without a zone change (manual use).

Exit 0 = normal (sent or silent). An uncaught error exits 1 (workflow ops notice).
State is written only after a successful publish, so a failed send retries next run.
"""
from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
CFG_PATH = ROOT / "configs" / "asml.json"
STATE_PATH = ROOT / "state" / "asml.json"
BROWSER_UA = {"User-Agent": "Mozilla/5.0 cwally-alerts"}
ACTION_BANDS = {"ZONE1", "ZONE2", "BEAR", "RICH", "BULL"}
NEVER = (
    "하지 말 것: 한 번에 전액 매수, 신용/미수, 급등일 추격, "
    "조회 실패나 가격만 보고 자동 매수하기."
)


def load_cfg() -> dict:
    return json.loads(CFG_PATH.read_text(encoding="utf-8"))


def load_state() -> dict:
    base = {
        "updated_at": None,
        "price_usd": None,
        "price_date": None,
        "band": None,
        "weekly_sent": None,
        "fail_sent_date": None,
    }
    try:
        base.update(json.loads(STATE_PATH.read_text(encoding="utf-8")))
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return base


def save_state(st: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def get(url: str, headers: dict, timeout: int = 25) -> bytes:
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def emit(cfg: dict, title: str, lines: list[str], pri: int, dry: bool, tags: list[str] | None = None) -> None:
    msg = "\n".join(lines)
    print(title)
    print(msg)
    if dry:
        print("dry-run; no ntfy")
        return
    publish(
        resolve_topic("port", cfg.get("ntfy_topic")),
        title,
        msg,
        pri,
        tags or ["chart_with_upwards_trend"],
        resolve_server(cfg.get("ntfy_server")),
    )


def yahoo_quote(ticker: str) -> dict | None:
    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        + urllib.parse.quote(ticker)
        + "?interval=1d&range=10d"
    )
    try:
        data = json.loads(get(url, BROWSER_UA).decode())
        res = data["chart"]["result"][0]
        meta = res.get("meta", {})
        ts = res.get("timestamp") or []
        closes = (res.get("indicators", {}).get("quote") or [{}])[0].get("close") or []
        pairs = [(t, c) for t, c in zip(ts, closes) if c is not None]
        price = meta.get("regularMarketPrice")
        mtime = meta.get("regularMarketTime")
        if price is None and pairs:
            mtime, price = pairs[-1]
        if price is None or mtime is None:
            return None
        prev = None
        mday = datetime.fromtimestamp(mtime, KST).date()
        older = [c for t, c in pairs if datetime.fromtimestamp(t, KST).date() < mday]
        if older:
            prev = older[-1]
        return {
            "price": float(price),
            "date": datetime.fromtimestamp(mtime, timezone.utc).strftime("%Y-%m-%d"),
            "prev": float(prev) if prev is not None else None,
        }
    except Exception as exc:
        print(f"yahoo fail {ticker}: {exc}", file=sys.stderr)
        return None


def band_for(price: float | None, lv: dict) -> str:
    if price is None:
        return "DATA_FAIL"
    if price >= lv["bull"]:
        return "BULL"
    if price >= lv["fair"]:
        return "RICH"
    if price <= lv["bear"]:
        return "BEAR"
    if price <= lv["zone_low"]:
        return "ZONE2"
    if price <= lv["zone_high"]:
        return "ZONE1"
    return "HOLD"


def usd_fmt(v: float | None) -> str:
    return f"${v:,.0f}" if v is not None else "없음"


def pct(a: float | None, b: float | None) -> str:
    if a is None or not b:
        return ""
    return f" ({(a / b - 1) * 100:+.1f}%)"


def slot_copy(cfg: dict, st: dict, band: str, q: dict, weekly: bool, stamp: str):
    lv = cfg["levels_usd"]
    eps27 = cfg["eps_usd"]["y2027e_base"]
    price = q["price"]
    per = price / eps27
    tr = cfg["tranche_pct"]
    status = (
        f"지금 결과/상태: 나스닥 ADR {q['date']} 종가 {usd_fmt(price)}{pct(price, q.get('prev'))}. "
        f"2027 기준 EPS 대비 PER {per:.1f}배 (EPS ${eps27}, {cfg['eps_usd']['as_of']} 기준). "
        f"선호 구간 ${lv['zone_low']:,}~{lv['zone_high']:,}, 비중 축소 검토 ${lv['fair']:,}, 12개월 참고 ${lv['bull']:,}, 약세 ${lv['bear']:,}."
    )
    if band == "ZONE1":
        title = f"[ASML] ${lv['zone_high']:,} 이하 | 1차 {tr[0]}%만"
        now_do = f"예정 총매수액의 {tr[0]}%만 분할 매수하세요. 2027 EPS 전망이 내려갔으면 보류하세요."
        pri = 4
    elif band == "ZONE2":
        title = f"[ASML] ${lv['zone_low']:,} 이하 | 2차 {tr[1]}%만"
        now_do = f"하락 이유(수출규제, 고객 투자 축소)를 먼저 확인하고, 이유가 일시적이면 {tr[1]}%만 추가하세요."
        pri = 4
    elif band == "BEAR":
        title = f"[ASML] 약세 시나리오 ${lv['bear']:,} | 자동 추가 금지"
        now_do = (
            "가격만 보고 추가하지 마세요. 2027 EPS 전망과 중국/수출규제 뉴스를 먼저 확인하고, "
            f"전망이 유지될 때만 남은 {tr[2]}%를 쓰세요."
        )
        pri = 5
    elif band == "RICH":
        title = f"[ASML] ${lv['fair']:,} 이상 | 신규 보류, 비중 점검"
        now_do = "새로 사지 마세요. 보유 비중이 계획보다 크면 초과분만 줄이는 것을 검토하세요."
        pri = 4
    elif band == "BULL":
        title = f"[ASML] 12개월 참고 ${lv['bull']:,} 도달 | 일부 차익 검토"
        now_do = "강세 시나리오를 미리 반영한 가격입니다. 보유분 일부 차익 실현을 검토하세요."
        pri = 5
    else:
        title = "[ASML] 주간 점검 | 관망, 분할 원칙 유지"
        now_do = f"관망하세요. 신규는 ${lv['zone_high']:,} 이하에서 {tr[0]}%씩 나눠 사는 원칙만 지키세요. 보유분은 유지."
        pri = 3
    if weekly and band != "HOLD":
        title = title.replace("[ASML]", "[ASML] 주간", 1)
    later = (
        "나중에: 2027년 1월 Q4 2026 실적 발표 후 가이던스에 맞게 모델 입력값을 고치세요. "
        "약세 신호 3개 이상이면 분할 속도를 늦추세요."
    )
    trim = (
        f"익절/줄일 때: ${lv['fair']:,} 이상은 신규 중단과 비중 점검, ${lv['bull']:,} 이상은 일부 차익. "
        "2027 매출 가이던스가 내려가면 가격과 무관하게 비중을 다시 보세요."
    )
    lines = [
        f"{stamp} | ASML | 나스닥 ADR USD 기준",
        f"이게 뭐 알림: {cfg['explain']}",
        status,
        f"지금 할 일: {now_do}",
        later,
        trim,
        NEVER,
    ]
    return title, lines, pri


def run_slot(cfg: dict, mode: str) -> int:
    dry = mode.startswith("dry")
    force = mode == "force-slot"
    now = datetime.now(KST)
    today = now.strftime("%Y-%m-%d")
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    st = load_state()
    years = cfg.get("year_guard") or [now.year]
    if not (dry or force) and (now.year not in years or today > cfg.get("valid_until", "9999-12-31")):
        print("asml outside window; silent")
        return 0
    q = yahoo_quote(cfg["ticker_usd"])
    band = band_for(q["price"] if q else None, cfg["levels_usd"])
    print(f"quote={q} band={band} prev_band={st.get('band')}")
    if band == "DATA_FAIL":
        if st.get("fail_sent_date") == today and not dry:
            print("data fail already notified today; silent")
            return 0
        emit(
            cfg,
            "[ASML] 시세 조회 실패 | 직접 확인",
            [
                f"{stamp} | ASML",
                "이게 뭐 알림: ASML 가격 구간 알림의 데이터 조회가 실패했습니다.",
                "지금 결과/상태: 나스닥 ADR(ASML) 종가를 받지 못했습니다. 실패를 정상으로 보지 마세요.",
                "지금 할 일: 증권앱에서 ASML 종가를 직접 확인하세요.",
                "나중에: 다음 평일 09:00 KST에 다시 조회합니다.",
                "익절/줄일 때: 이번 알림은 매매 신호가 아닙니다.",
                NEVER,
            ],
            4,
            dry,
            ["warning"],
        )
        if not dry:
            st["fail_sent_date"] = today
            save_state(st)
        return 0

    iso = now.isocalendar()
    week_key = f"{iso[0]}-W{iso[1]:02d}"
    zone_change = band in ACTION_BANDS and band != st.get("band")
    weekly = st.get("weekly_sent") != week_key
    send = zone_change or weekly or force
    st_new = dict(st)
    st_new.update(
        {
            "updated_at": stamp,
            "price_usd": q["price"],
            "price_date": q["date"],
            "band": band,
            "fail_sent_date": None,
        }
    )
    if not send:
        print("asml no zone change, weekly already sent; silent")
        if not dry:
            save_state(st_new)
        return 0
    title, lines, pri = slot_copy(cfg, st, band, q, weekly and not force, stamp)
    emit(cfg, title, lines, pri, dry)
    if not dry:
        if weekly:
            st_new["weekly_sent"] = week_key
        save_state(st_new)
    return 0


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "slot"
    cfg = load_cfg()
    if mode in ("slot", "dry-slot", "force-slot"):
        sys.exit(run_slot(cfg, mode))
    print(f"unknown mode {mode}")
    sys.exit(1)
