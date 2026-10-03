#!/usr/bin/env python3
from __future__ import annotations

import json
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
CFG_PATH = ROOT / "configs" / "cl.json"
STATE_PATH = ROOT / "state" / "cl.json"
UA = {"User-Agent": "cwally-alerts cl monitor"}


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


def save_state(state: dict, now: datetime) -> None:
    state["updated_at"] = now.isoformat()
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
        print(f"price fetch failed: {exc}", file=sys.stderr)
        return None


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=25) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def visible(text: str) -> str:
    text = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", text)
    text = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text)


def parse_num(blob: str, pattern: str):
    m = re.search(pattern, blob, re.I)
    if not m:
        return None
    raw = m.group(1).replace(",", "")
    try:
        return float(raw)
    except ValueError:
        return None


def scan_q3(cfg: dict) -> dict:
    found = False
    snippet = ""
    source = ""
    blob = ""
    fails = 0
    result_re = re.compile(
        r"third quarter 2026 results|reports third quarter 2026|q3 2026 results|"
        r"quarter ended september 30, 2026",
        re.I,
    )
    for url in cfg["sources"]:
        try:
            raw = fetch(url)
        except Exception as exc:
            print(f"fetch fail {url}: {exc}")
            fails += 1
            continue
        text = visible(raw)
        if result_re.search(text):
            found = True
            source = url
            snippet = text[:400]
            blob = text
            break
    organic = parse_num(blob, r"organic sales[^.]{0,60}?([0-9]+(?:\.[0-9]+)?)%")
    guide_raise = bool(re.search(r"raising.{0,40}guidance|increased.{0,30}guidance|now expects mid- to high", blob, re.I))
    guide_cut = bool(re.search(r"lowering.{0,40}guidance|guidance.{0,20}below|now expects.{0,40}decline", blob, re.I))
    margin_cut = bool(re.search(r"gross profit margin.{0,40}down|gross margin.{0,30}decline", blob, re.I))
    return {
        "found": found,
        "source": source,
        "snippet": snippet,
        "fails": fails,
        "organic": organic,
        "guide_raise": guide_raise,
        "guide_cut": guide_cut,
        "margin_cut": margin_cut,
    }


def verdict(m: dict, cfg: dict) -> str:
    rules = cfg["change_rules"]
    organic = m.get("organic")
    if organic is None and not m.get("found"):
        return "UNCONFIRMED"
    upgrade = organic is not None and organic > rules["upgrade_organic_gt"] and m.get("guide_raise")
    downgrade = (organic is not None and organic < rules["downgrade_organic_lt"]) or m.get("margin_cut") or m.get("guide_cut")
    if upgrade and not downgrade:
        return "UPGRADE"
    if downgrade and not upgrade:
        return "DOWNGRADE"
    if m.get("found"):
        return "HOLD"
    return "UNCONFIRMED"


def band_for(price, levels: dict) -> str:
    if price is None:
        return "NO_PX"
    if price <= levels["buy2"]:
        return "BUY2"
    if price <= levels["buy1"]:
        return "BUY1"
    if price >= levels["stretch"]:
        return "STRETCH"
    return "HOLD"


def px_txt(price) -> str:
    return f"${price:,.2f}" if price is not None else "시세 없음"


def body_price(stamp: str, cfg: dict, price, band: str):
    lv = cfg["levels"]
    common = [f"{stamp} | CL | {cfg['display']}", f"이게 뭐 알림: {cfg['explain']}"]
    if band == "BUY2":
        title = "[CL] $78 이하 | 2차 분할만"
        now_do = f"{px_txt(price)}입니다. 2차 분할만 검토하세요. 전액매수 금지."
        pri = 4
    elif band == "BUY1":
        title = "[CL] $80 이하 | 1차 분할만"
        now_do = f"{px_txt(price)}입니다. 1차 분할만 검토하세요. 추격 금지."
        pri = 4
    else:
        title = "[CL] $99 이상 | 추격 금지"
        now_do = f"{px_txt(price)}입니다. 새 돈으로 따라가지 마세요."
        pri = 4
    lines = common + [
        f"지금 결과/상태: {now_do} 기준가 {cfg['ref_price']}. 2026 유기 가이든스 1~4%.",
        "지금 할 일: 분할만. 실적 없이 비중을 키우지 마세요.",
        f"나중에: Q3는 {cfg['earnings_call_kst']} KST. 유기성장과 기준 EPS를 그때 다시 봅니다.",
        "익절/줄일 때: 유기 1% 미만 또는 마진 하향 가이든스면 추가매수 중단. $99 이상은 추격 금지.",
        "하지 말 것: 신용, 미수, 전액매수. 조회 실패를 매수 신호로 보기.",
    ]
    return title, lines, pri


def body_pre(stamp: str, cfg: dict, price):
    g = cfg["guide_fy2026"]
    title = "[CL] 오늘 밤 Q3 | 숫자 전 추격 금지"
    lines = [
        f"{stamp} | CL | {cfg['display']}",
        f"이게 뭐 알림: {cfg['explain']}",
        f"지금 결과/상태: 발표는 {cfg['earnings_call_kst']} KST. 현재가 {px_txt(price)}. 숫자는 아직 없음.",
        (
            "지금 할 일: 밤에 볼 항목만 준비. "
            f"1) 유기성장 {g['organic_lo']:.0f}~{g['organic_hi']:.0f}% "
            "2) 3% 초과면 상향 후보 "
            "3) 기준 EPS 상향 여부 "
            "4) 매출총이익률 하향 문구."
        ),
        "나중에: 다음 평일 09:00 KST에 발표문 대조. 발표 직후 추격 금지.",
        "익절/줄일 때: 유기 1% 미만 또는 마진 하향이면 $78 아래를 기다립니다.",
        "하지 말 것: 미발표 숫자를 추정해서 매매. 신용, 미수.",
    ]
    return title, lines, 4


def body_post(stamp: str, cfg: dict, price, m: dict):
    call = verdict(m, cfg)
    organic = f"{m['organic']:.1f}%" if m.get("organic") is not None else "파싱실패"
    if call == "UPGRADE":
        title = "[CL] Q3 상향조건 | 현가도 분할 검토"
        action = "유기 3% 초과와 기준 상향 문구가 같이 있습니다. 현가 분할만 검토. 전액 금지."
        pri = 5
    elif call == "DOWNGRADE":
        title = "[CL] Q3 하향조건 | $78 아래 대기"
        action = "유기 1% 미만 또는 마진/가이든스 하향 문구. 추가매수는 $78 아래만."
        pri = 5
    elif call == "HOLD":
        title = "[CL] Q3 보합 | 추격 금지"
        action = "상향·하향 조건 모두 아님. 기존 보유 유지, 새 돈 추격 금지."
        pri = 4
    else:
        title = "[CL] Q3 숫자 미확인 | 직접 확인"
        action = "IR/SEC에서 Q3 실적문을 확인하지 못함. 실패를 호재로 보지 마세요."
        pri = 4
    status = f"Q3 관련 문구를 찾음. 출처 {m.get('source') or 'IR'}." if m.get("found") else action
    lines = [
        f"{stamp} | CL | {cfg['display']}",
        f"이게 뭐 알림: {cfg['explain']}",
        f"지금 결과/상태: {status} 현재가 {px_txt(price)}. 판정 {call}.",
        f"파싱: 유기 {organic} | 가이든스상향 {m.get('guide_raise')} | 가이든스하향 {m.get('guide_cut')} | 마진하향 {m.get('margin_cut')}.",
        f"지금 할 일: {action} 원문을 대조하세요.",
        "나중에: 파싱실패 칸은 IR 원문만 믿으세요. 이 알림만으로 전량 매도하지 마세요.",
        "익절/줄일 때: 하향이면 $78 아래. 상향이어도 $99 추격은 금지.",
        "하지 말 것: 조회 실패를 실적으로 취급. 신용, 미수, 추격.",
    ]
    return title, lines, pri


def body_fail(stamp: str, cfg: dict):
    title = "[CL] 시세 조회 실패 | 직접 확인"
    lines = [
        f"{stamp} | CL | {cfg['display']}",
        f"이게 뭐 알림: {cfg['explain']}",
        "지금 결과/상태: 가격 조회 실패. 실패를 정상 처리하지 않습니다.",
        "지금 할 일: 야후/증권앱에서 CL 종가를 직접 확인하세요.",
        "나중에: 다음 평일 09:00 KST에 재시도합니다.",
        "익절/줄일 때: 이번 알림은 매매 신호가 아닙니다.",
        "하지 말 것: 실패를 매수/매도 근거로 쓰기.",
    ]
    return title, lines, 4


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    dry = mode in {"test", "dry", "dry-run"}
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    state = load_json(
        STATE_PATH,
        {
            "price": None,
            "band": None,
            "buy1_sent": False,
            "buy2_sent": False,
            "stretch_sent": False,
            "q3_pre_sent": False,
            "q3_post_sent": False,
            "fail_sent_date": None,
            "last_kind": None,
            "updated_at": None,
        },
    )
    now = datetime.now(KST)
    today = now.strftime("%Y-%m-%d")
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    year_ok = now.year == int(cfg["year_guard"])
    valid = today <= cfg["valid_until"]
    print(f"{stamp} mode={mode} year_ok={year_ok} valid={valid}")
    if mode not in {"force", "dry", "test", "dry-run"} and (not year_ok or not valid):
        print("outside 2026 window; silent")
        return 0

    price = yahoo_price(cfg["ticker"])
    band = band_for(price, cfg["levels"])
    print(f"price={price} band={band}")

    kind = None
    title = ""
    lines = []
    pri = 3
    metrics = {}
    dates = cfg["send_dates"]
    in_earn = cfg["window_start"] <= today <= cfg["window_end"]

    if today == dates["q3_pre"] and (mode in {"force", "dry", "test"} or not state.get("q3_pre_sent")):
        kind = "Q3_PRE"
        title, lines, pri = body_pre(stamp, cfg, price)
    elif today >= dates["q3_post"] and in_earn and (mode in {"force", "dry", "test"} or not state.get("q3_post_sent")):
        metrics = scan_q3(cfg)
        kind = "Q3_POST"
        title, lines, pri = body_post(stamp, cfg, price, metrics)
        if not metrics.get("found") and today < "2026-11-04" and mode not in {"force", "dry", "test"}:
            print("q3 not posted yet; wait")
            kind = None
    elif band == "BUY2" and (mode in {"force", "dry", "test"} or not state.get("buy2_sent")):
        kind = "BUY2"
        title, lines, pri = body_price(stamp, cfg, price, band)
    elif band == "BUY1" and (mode in {"force", "dry", "test"} or not state.get("buy1_sent")):
        kind = "BUY1"
        title, lines, pri = body_price(stamp, cfg, price, band)
    elif band == "STRETCH" and (mode in {"force", "dry", "test"} or not state.get("stretch_sent")):
        kind = "STRETCH"
        title, lines, pri = body_price(stamp, cfg, price, band)
    elif price is None and state.get("fail_sent_date") != today:
        kind = "FAIL"
        title, lines, pri = body_fail(stamp, cfg)

    if kind is None:
        print("no cl trigger; silent")
        state["price"] = price
        state["band"] = band
        if not dry:
            save_state(state, now)
        return 0

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
        if kind == "BUY1":
            state["buy1_sent"] = True
        elif kind == "BUY2":
            state["buy2_sent"] = True
            state["buy1_sent"] = True
        elif kind == "STRETCH":
            state["stretch_sent"] = True
        elif kind == "Q3_PRE":
            state["q3_pre_sent"] = True
        elif kind == "Q3_POST":
            state["q3_post_sent"] = True
        elif kind == "FAIL":
            state["fail_sent_date"] = today
    state["price"] = price
    state["band"] = band
    state["last_kind"] = kind
    if metrics:
        state["last_metrics"] = {k: v for k, v in metrics.items() if k != "snippet"}
    if not dry:
        save_state(state, now)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
