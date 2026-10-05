#!/usr/bin/env python3
"""PFFD change-condition alert. Silent unless a gate newly opens."""
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
CFG_PATH = ROOT / "configs" / "pffd.json"
STATE_PATH = ROOT / "state" / "pffd.json"
UA = {"User-Agent": "Mozilla/5.0 cwally-alerts"}


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


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def yahoo_last(ticker: str):
    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        + urllib.parse.quote(ticker)
        + "?interval=1d&range=10d"
    )
    req = urllib.request.Request(url, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read().decode())
        result = payload["chart"]["result"][0]
        meta = result.get("meta", {})
        price = meta.get("regularMarketPrice") or meta.get("previousClose")
        closes = (result.get("indicators") or {}).get("quote", [{}])[0].get("close") or []
        prev = None
        nums = [c for c in closes if isinstance(c, (int, float))]
        if len(nums) >= 2:
            prev = float(nums[-2])
        return (float(price) if price is not None else None), prev
    except Exception as exc:
        print(f"yahoo fail {ticker}: {exc}", file=sys.stderr)
        return None, None


def in_window(now: datetime, cfg: dict) -> bool:
    years = cfg.get("year_guard") or [now.year]
    if isinstance(years, int):
        years = [years]
    if now.year not in years:
        return False
    until = cfg.get("valid_until")
    if until and now.strftime("%Y-%m-%d") > until:
        return False
    return True


def body(stamp: str, price, tnx, price_on: bool, yield_on: bool, cfg: dict) -> tuple[str, str, int]:
    px = f"${price:.2f}" if price is not None else "시세 없음"
    yld = (cfg["ttm_distribution"] / price * 100) if price else None
    ytxt = f"{yld:.2f}%" if yld is not None else "계산 불가"
    ttxt = f"{tnx:.2f}%" if tnx is not None else "조회 실패"
    if price_on and yield_on:
        title = "[PFFD] 가격·금리 조건 동시 충족 | 분할 검토"
        pri = 4
    elif price_on:
        title = "[PFFD] $16 이하 | 분할 검토 가능"
        pri = 4
    else:
        title = "[PFFD] 미국 10년물 하락 전환 | 금리 조건만 충족"
        pri = 3
    lines = [
        f"{stamp} | PFFD 변경 조건 알림",
        "이게 뭐 알림: 우선주 ETF 신규 매수를 보류한 조건 중, 가격과 금리가 바뀐 항목만 보는 알림.",
        (
            f"지금 결과/상태: 가격 {px}, 12개월 분배 $1.20 기준 수익률 {ytxt}. "
            f"가격 관문($16 이하) {'충족' if price_on else '미충족'}. "
            f"미국 10년물 {ttxt}, 기준 대비 0.15%p 하락 {'충족' if yield_on else '미충족'}."
        ),
        "지금 할 일: 충족된 항목만 분할 검토. 전액 매수하지 말 것. 연준 경로는 즉시 확인할 것.",
        "나중에: 연준이 추가 인상을 철회했는지, 의무전환형 비중이 줄었는지는 이 알림이 확인하지 못함. 그 두 항목은 직접 볼 것.",
        "익절/줄일 때: 이 알림은 매도 신호가 아님. $16 위로 돌아가면 가격 조건은 다시 닫힘.",
        "하지 말 것: 조회 실패를 매수 신호로 보기. 신용/미수. 금리만 내렸다고 전액 매수.",
    ]
    return title, "\n".join(lines), pri


def fail_body(stamp: str) -> tuple[str, str]:
    lines = [
        f"{stamp} | PFFD 조회 실패",
        "이게 뭐 알림: PFFD 시세를 못 가져와 조건 판정을 못 한 운영 알림.",
        "지금 결과/상태: 가격 조회 실패. 조건 미충족이 아님.",
        "지금 할 일: 증권앱에서 PFFD와 미국 10년물을 직접 확인.",
        "나중에: 다음 평일 09:00 KST에 재시도.",
        "익절/줄일 때: 매매 신호 아님.",
        "하지 말 것: 실패를 조건 미충족으로 보기.",
    ]
    return "[PFFD] 시세 조회 실패 | 직접 확인", "\n".join(lines)


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    dry = mode in {"test", "dry", "dry-run"}
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    today = now.strftime("%Y-%m-%d")
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    state = load_json(
        STATE_PATH,
        {
            "updated_at": None,
            "price": None,
            "tnx": None,
            "tnx_baseline": None,
            "price_alerted": False,
            "yield_alerted": False,
            "fail_sent_date": None,
        },
    )
    print(f"{stamp} mode={mode}")
    if mode not in {"force", "dry", "test", "dry-run"} and not in_window(now, cfg):
        print("pffd outside window; silent")
        return 0

    price, _prev_px = yahoo_last(cfg["ticker"])
    tnx, prev_tnx = yahoo_last(cfg["tnx_ticker"])
    print(f"price={price} tnx={tnx} prev_tnx={prev_tnx}")

    if price is None:
        title, msg = fail_body(stamp)
        print(title)
        print(msg)
        if state.get("fail_sent_date") != today or mode == "force":
            if dry:
                print("dry-run; no ntfy")
            else:
                publish(
                    resolve_topic("ops"),
                    title,
                    msg,
                    4,
                    ["warning"],
                    resolve_server(cfg.get("ntfy_server")),
                )
                state["fail_sent_date"] = today
                state["updated_at"] = stamp
                save_state(state)
        else:
            print("fail already sent today; silent")
        return 1

    price_on = price <= float(cfg["price_max"])
    baseline = state.get("tnx_baseline")
    if baseline is None and tnx is not None:
        baseline = tnx
        state["tnx_baseline"] = tnx
        print(f"tnx baseline set {tnx}")
    yield_on = False
    if tnx is not None and baseline is not None and prev_tnx is not None:
        yield_on = tnx <= float(baseline) - float(cfg["tnx_drop_pp"]) and tnx < prev_tnx
    elif tnx is None:
        print("tnx missing; yield gate not evaluated")

    send_price = price_on and (not state.get("price_alerted") or mode == "force")
    send_yield = yield_on and (not state.get("yield_alerted") or mode == "force")
    state["updated_at"] = stamp
    state["price"] = price
    state["tnx"] = tnx
    state["fail_sent_date"] = None
    if not price_on:
        state["price_alerted"] = False
    if not yield_on:
        state["yield_alerted"] = False

    if not send_price and not send_yield:
        print(f"pffd no new gate price_on={price_on} yield_on={yield_on}; silent")
        if not dry:
            save_state(state)
        return 0

    title, msg, pri = body(stamp, price, tnx, price_on, yield_on, cfg)
    print(title)
    print(msg)
    if dry:
        print("dry-run; no ntfy")
    else:
        publish(
            resolve_topic("port", cfg.get("ntfy_topic")),
            title,
            msg,
            pri,
            ["chart_with_upwards_trend"],
            resolve_server(cfg.get("ntfy_server")),
        )
        if send_price:
            state["price_alerted"] = True
        if send_yield:
            state["yield_alerted"] = True
        save_state(state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
