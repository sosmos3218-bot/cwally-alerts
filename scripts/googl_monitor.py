#!/usr/bin/env python3
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
CFG_PATH = ROOT / "configs" / "googl.json"
STATE_PATH = ROOT / "state" / "googl.json"
UA = {"User-Agent": "Mozilla/5.0 cwally-alerts"}
ACTION = {"BUY1", "BUY2", "BUY3", "FAIR", "STRETCH", "CAP"}


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


def yahoo_price(ticker: str):
    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        + urllib.parse.quote(ticker)
        + "?interval=1d&range=5d"
    )
    req = urllib.request.Request(url, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read().decode())
        meta = payload["chart"]["result"][0].get("meta", {})
        price = meta.get("regularMarketPrice") or meta.get("previousClose")
        return float(price) if price is not None else None
    except Exception as exc:
        print(f"yahoo fail {ticker}: {exc}", file=sys.stderr)
        return None


def band_for(price, levels: dict) -> str:
    if price is None:
        return "DATA_FAIL"
    if price >= levels["cap"]:
        return "CAP"
    if price >= levels["stretch"]:
        return "STRETCH"
    if price >= levels["fair"]:
        return "FAIR"
    if price <= levels["buy3"]:
        return "BUY3"
    if price <= levels["buy2"]:
        return "BUY2"
    if price <= levels["buy1"]:
        return "BUY1"
    return "HOLD"


def px_txt(price) -> str:
    return f"${price:,.2f}" if price is not None else "시세 없음"


def copy_for(kind: str, stamp: str, cfg: dict, price) -> tuple[str, list[str], int]:
    lv = cfg["levels"]
    tr = cfg["tranche"]
    w = cfg["max_weight_pct"]
    common = [f"{stamp} | GOOGL | {cfg['display']}", f"이게 뭐 알림: {cfg['explain']}"]
    later = (
        "나중에: 2026년 4분기~2027년 2분기만 모아가는 창. "
        "분기마다 검색(서비스) 성장 10% 이상, 클라우드 성장 25% 이상+이익률 개선, "
        "영업현금이 설비투자 증가보다 빠른지만 직접 확인하세요. 이 알림은 가격만 뱃니다."
    )
    never = "하지 말 것: 신용/미수, 전액 즉시매수, $700을 기본 전제로 추격. 조회 실패를 매수 신호로 보기. 나스닥100 ETF 속 알파벳 노출을 빼고 8%를 계산하기."
    if kind == "DATA_FAIL":
        return (
            "[GOOGL] 시세 조회 실패 | 직접 확인",
            common
            + [
                "지금 결과/상태: 가격 조회 실패. 실패를 정상으로 보지 마세요.",
                "지금 할 일: 증권앱에서 GOOGL 종가를 직접 확인하세요.",
                "나중에: 다음 평일 09:00 KST에 재시도합니다.",
                "익절/줄일 때: 이번 알림은 매매 신호가 아닙니다.",
                never,
            ],
            4,
        )
    if kind == "BUY1":
        title = f"[GOOGL] ${lv['buy1']:.0f} 부근 | 1차 {tr['buy1_pct']}%만"
        status = f"현재가 {px_txt(price)}. 1차 구간입니다."
        now_do = f"예정 총매수액의 {tr['buy1_pct']}%만 사세요. 직접 보유는 {w}%를 넘기지 마세요."
        pri = 4
    elif kind == "BUY2":
        title = f"[GOOGL] ${lv['buy2']:.0f} 이하 | 2차 {tr['buy2_pct']}%만"
        status = f"종가 {px_txt(price)}. 2차 구간입니다."
        now_do = f"예정 총매수액의 {tr['buy2_pct']}%만 추가하세요. 검색/클라우드가 같이 껏이면 추가를 머추세요."
        pri = 4
    elif kind == "BUY3":
        title = f"[GOOGL] ${lv['buy3']:.0f} 이하 | 3차 {tr['buy3_pct']}%만"
        status = f"종가 {px_txt(price)}. 3차 구간입니다."
        now_do = f"잔여 {tr['buy3_pct']}%만 채우세요. 가격만 보고 전량을 넣지 마세요."
        pri = 5
    elif kind == "FAIR":
        title = f"[GOOGL] ${lv['fair']:.0f} 도달 | 8% 초과분만"
        status = f"현재가 {px_txt(price)}. 기준가에 닮았습니다."
        now_do = f"전량 매도하지 마세요. 직접 보유가 {w}%를 넘긴 초과분만 줄이세요."
        pri = 4
    elif kind == "STRETCH":
        title = f"[GOOGL] ${lv['stretch']:.0f} 이상 | 25-33% 축소"
        status = f"현재가 {px_txt(price)}. 실적 없는 배수 확장 구간입니다."
        now_do = "2029년 EPS $20 경로가 안 보이면 보유량의 25-33%를 줄이세요."
        pri = 5
    elif kind == "CAP":
        title = f"[GOOGL] ${lv['cap']:.0f} 도달 | 절반 차익"
        status = f"현재가 {px_txt(price)}. 상방 시나리오를 선반영한 가격입니다."
        now_do = "보유량의 50%를 줄이세요."
        pri = 5
    else:
        title = "[GOOGL] 유지 | 오늘 손대지 말 것"
        status = f"현재가 {px_txt(price)}. 밴드 사이입니다."
        now_do = "오늘 새로 사지 마세요."
        pri = 3
    lines = common + [
        f"지금 결과/상태: {status} 기준가 ${lv['fair']:.0f}, 하방 ${lv['buy3']:.0f}, 상방 ${lv['cap']:.0f}.",
        f"지금 할 일: {now_do}",
        later,
        (
            f"익절/줄일 때: ${lv['fair']:.0f}은 초과분만, ${lv['stretch']:.0f}은 25~33%, "
            f"${lv['cap']:.0f}은 50%. 검색 성장 5% 미만이 2분기 연속이면 최소 50% 축소."
        ),
        never,
    ]
    return title, lines, pri


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


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "event").lower()
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
            "band": None,
            "last_alert_key": None,
            "fail_sent_date": None,
        },
    )
    print(f"{stamp} mode={mode} year={now.year}")
    if mode not in {"force", "dry", "test", "dry-run"} and not in_window(now, cfg):
        print("googl outside window; silent")
        return 0

    price = yahoo_price(cfg["ticker"])
    band = band_for(price, cfg["levels"])
    prev_band = state.get("band")
    print(f"price={price} band={band} prev={prev_band}")

    kind = None
    if band == "DATA_FAIL":
        if state.get("fail_sent_date") != today or mode in {"force", "dry", "test"}:
            kind = "DATA_FAIL"
    elif band in ACTION and band != prev_band:
        kind = band
    elif mode in {"force"} and band in ACTION:
        kind = band

    state["updated_at"] = stamp
    state["price"] = price
    if band != "DATA_FAIL":
        state["band"] = band

    if kind is None:
        print("googl no zone change; silent")
        if not dry:
            save_state(state)
        return 0

    title, lines, pri = copy_for(kind, stamp, cfg, price)
    msg = "\n".join(lines)
    print(title)
    print(msg)
    print(f"kind={kind} dry={dry}")
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
        if kind == "DATA_FAIL":
            state["fail_sent_date"] = today
        else:
            state["last_alert_key"] = kind
            state["fail_sent_date"] = None
    if not dry:
        save_state(state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
