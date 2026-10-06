#!/usr/bin/env python3
"""Dongkook 086450 2026 Q3 check. Silent outside the window. No new cron."""
from __future__ import annotations

import json
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from html import unescape
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
CFG_PATH = ROOT / "configs" / "dkq3.json"
STATE_PATH = ROOT / "state" / "dkq3.json"
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


def save_state(state: dict, now: datetime) -> None:
    state["updated_at"] = now.isoformat()
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def emit(cfg: dict, title: str, lines: list[str], priority: int = 4) -> None:
    topic = resolve_topic("port", cfg.get("ntfy_topic"))
    server = resolve_server(cfg.get("ntfy_server"))
    publish(topic, title, "\n".join(lines), priority, ["chart_with_upwards_trend"], server)


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=25) as resp:
        text = resp.read().decode("utf-8", errors="ignore")
    if text.lstrip()[:1] in "[{":
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            import naver_api

            return naver_api.flatten_text(json.loads(text))
        except (ValueError, ImportError):
            pass
    return text


def strip_html(html: str) -> str:
    text = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", html)
    text = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", unescape(text))


def num(raw: str | None):
    if not raw:
        return None
    try:
        return float(raw.replace(",", ""))
    except ValueError:
        return None


def near(text: str, label: str, window: int = 80):
    m = re.search(label + r".{0," + str(window) + r"}?([0-9][0-9,]*(?:\.[0-9]+)?)\s*억", text)
    return num(m.group(1)) if m else None


def first_num(text: str, pattern: str):
    m = re.search(pattern, text)
    return num(m.group(1)) if m else None


def parse_text(text: str, h1: dict) -> dict:
    q3 = bool(re.search(r"3분기|3Q|분기보고서", text))
    sales = first_num(text, r"(?<![\-~])3분기 매출(?:액)?(?:은|는)?\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*억")
    op = first_num(text, r"(?<![\-~])3분기 영업이익(?:은|는)?\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*억")
    if op is None:
        op = first_num(text, r"(?<![\-~])3분기 매출(?:액)?(?:은|는)?\s*[0-9][0-9,]*(?:\.[0-9]+)?\s*억[^.]{0,40}영업이익(?:은|는)?\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*억")
    overseas = first_num(text, r"(?<![\-~])3분기 (?:해외매출|해외 매출|수출)(?:은|는)?\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*억")
    cum_sales = first_num(text, r"(?:1~3분기|1-3분기|누적) 매출(?:액)?(?:은|는)?\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*억")
    cum_op = first_num(text, r"(?:1~3분기|1-3분기|누적) 영업이익(?:은|는)?\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*억")
    cum_os = first_num(text, r"(?:1~3분기|1-3분기|누적) (?:해외매출|해외 매출|수출)(?:은|는)?\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*억")
    if sales is None and cum_sales and cum_sales > h1["sales_eok"]:
        sales = round(cum_sales - h1["sales_eok"], 1)
    if op is None and cum_op and cum_op > h1["op_eok"]:
        op = round(cum_op - h1["op_eok"], 1)
    if overseas is None and cum_os and cum_os > h1["overseas_eok"]:
        overseas = round(cum_os - h1["overseas_eok"], 1)
    opm = round(op / sales * 100, 1) if sales and op is not None and sales > 0 else None
    share = round(overseas / sales * 100, 1) if sales and overseas is not None and sales > 0 else None
    delay = bool(re.search(r"허가.{0,8}지연|공장.{0,8}지연|완공.{0,8}지연|가동.{0,8}지연", text))
    promo = bool(re.search(r"판촉비|광고선전비|판매수수료.{0,6}증가", text))
    published = q3 and (sales is not None or op is not None)
    return {
        "published": published,
        "sales": sales,
        "op": op,
        "opm": opm,
        "overseas": overseas,
        "share": share,
        "delay": delay,
        "promo": promo,
    }


def verdict(p: dict, cfg: dict) -> str:
    if not p.get("published"):
        return "미확인"
    rules = cfg["rules"]
    sales, op, opm, share = p.get("sales"), p.get("op"), p.get("opm"), p.get("share")
    cut = False
    if isinstance(op, float) and op <= float(rules["q_op_break_eok"]):
        cut = True
    if isinstance(opm, float) and opm < float(rules["q_opm_keep_pct"]):
        cut = True
    if isinstance(share, float) and share < 12.0:
        cut = True
    if p.get("delay"):
        cut = True
    keep = (
        isinstance(sales, float)
        and sales >= float(rules["q_sales_keep_eok"])
        and isinstance(op, float)
        and op > float(rules["q_op_break_eok"])
        and isinstance(opm, float)
        and opm >= float(rules["q_opm_keep_pct"])
        and (share is None or share >= float(rules["overseas_share_keep_pct"]))
        and not p.get("delay")
        and not p.get("promo")
    )
    if cut:
        return "하향"
    if keep:
        return "유지"
    return "부분유지"


def fmt(v, suffix=""):
    return f"{v:,.1f}{suffix}" if isinstance(v, float) else "확인 불가"


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    today = now.strftime("%Y-%m-%d")
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    state = load_json(
        STATE_PATH,
        {"result_notified": False, "empty_notified": False, "fail_notified": False, "last_key": None, "updated_at": None},
    )
    in_year = now.year == int(cfg["year_guard"])
    in_window = cfg["window_start"] <= today <= cfg["window_end"]
    print(f"{stamp} mode={mode} window={in_window}")
    if mode not in {"force", "dry"} and (not in_year or not in_window):
        print("outside 2026 window; silent")
        return 0
    h1 = cfg["h1"]
    parsed = {"published": False, "sales": None, "op": None, "opm": None, "overseas": None, "share": None, "delay": False, "promo": False}
    ok = False
    err = ""
    for url in cfg["sources"]:
        try:
            text = strip_html(fetch(url))
            ok = True
            got = parse_text(text, h1)
            if got.get("published"):
                parsed = got
                break
            parsed = got
        except Exception as exc:
            err = str(exc)
            print(f"fetch fail {url}: {exc}")
    if mode != "dry" and not ok:
        if not state.get("fail_notified"):
            emit(
                cfg,
                "[DKQ3] 동국제약 3Q 조회 실패 | 판정 유보",
                [
                    f"{stamp} | PORT | {cfg['event_label']}",
                    "이게 뭐 알림: 동국제약 3분기 실적이 나오면 상반기 대비 매출, 영업이익률, 해외 비중을 보고 목표 3만원을 유지할지 재평가.",
                    f"지금 상태: 공시·뉴스 조회 실패. {err or '출처 없음'}. 숫자를 추정하지 않음.",
                    "지금 할 일: 다음 기존 슬롯(09:00 또는 15:20)까지 대기. 목표 3만원 유지 여부 유보.",
                    "나중에: 원문이 열리면 한 번만 보냄. 만료 2026-11-17.",
                    "익절/줄일 때: 실패 알림으로 포지션을 바꾸지 마세요.",
                    "하지 말 것: 조회 실패를 호재나 악재로 읽기. 추격, 신용, 미수.",
                ],
                4,
            )
            state["fail_notified"] = True
        save_state(state, now)
        return 0
    tag = verdict(parsed, cfg)
    if not parsed.get("published") and today < cfg["window_end"] and mode not in {"force", "dry"}:
        print("not published yet; silent")
        save_state(state, now)
        return 0
    if parsed.get("published") and state.get("result_notified") and mode != "force":
        print("result already sent; silent")
        return 0
    if not parsed.get("published") and state.get("empty_notified") and mode != "force":
        print("empty already sent; silent")
        return 0
    share_txt = fmt(parsed.get("share"), "%")
    h1_share = h1["overseas_share_pct"]
    if tag == "유지":
        title = "[DKQ3] 동국제약 3Q 목표 3만 유지 | 숫자 확인"
        action = "목표 3만원은 유지. 추격 매수는 하지 마세요. 해외 비중이 비어 있으면 공시 원문만 다시 보세요."
    elif tag == "하향":
        title = "[DKQ3] 동국제약 3Q 목표 3만 재검토 | 조건 이탈"
        action = "목표 3만원은 유지하지 마세요. 신규 매수 보류. 보유는 공시 원문 확인 후 줄일지 판단."
    elif tag == "부분유지":
        title = "[DKQ3] 동국제약 3Q 목표 3만 보류 | 일부만 확인"
        action = "매출 또는 이익은 나왔지만 해외 비중·판촉·허가 중 하나가 비었거나 약함. 3만원 유지는 보류."
    else:
        title = "[DKQ3] 동국제약 3Q 아직 없음 | 판정 유보"
        action = "분기 숫자가 안 보입니다. 다음 공시까지 대기. 3만원 유지 여부 유보."
    lines = [
        f"{stamp} | PORT | {cfg['event_label']} | {tag}",
        "이게 뭐 알림: 3분기 매출, 영업이익률, 해외 비중이 상반기 5,099억/534억(이익률 10.5%, 해외 14.4%) 대비 어떤지 보고 목표 3만원 유지 여부를 재평가.",
        (
            f"지금 상태: {tag}. 3Q 매출 {fmt(parsed.get('sales'), '억')} / 기준 2,500억. "
            f"영업이익 {fmt(parsed.get('op'), '억')} / 하향선 230억. "
            f"영업이익률 {fmt(parsed.get('opm'), '%')} / 기준 10%. "
            f"해외 {fmt(parsed.get('overseas'), '억')}, 비중 {share_txt} / 상반기 {h1_share}%. "
            f"허가·공장 지연 {'있음' if parsed.get('delay') else '문구 없음'}. "
            f"판촉비 잠식 {'문구 있음' if parsed.get('promo') else '문구 없음'}."
        ),
        f"지금 할 일: {action}",
        "나중에: 같은 결과는 다시 안 보냄. 창은 2026-11-17 15:20까지. 그 뒤 침묵.",
        "익절/줄일 때: 이 알림은 목표 3만원 유지 판정이지 매도 지시가 아님.",
        "하지 말 것: 상반기 숫자를 3분기 확정으로 쓰기. 조회 실패를 호재로 읽기. 22,000원대 추격, 신용, 미수. 투자 권유 아님.",
        f"출처: {cfg['sources'][0]}",
        f"조회: {'OK' if ok and not err else ('일부 실패: ' + err if ok else '실패: ' + err)}",
    ]
    if mode == "dry":
        print(title)
        print("\n".join(lines))
        print("dry-run; no ntfy")
        return 0
    emit(cfg, title, lines, 4)
    if parsed.get("published"):
        state["result_notified"] = True
    else:
        state["empty_notified"] = True
    state["last_key"] = json.dumps({"tag": tag, "sales": parsed.get("sales"), "op": parsed.get("op"), "share": parsed.get("share")}, sort_keys=True)
    save_state(state, now)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
