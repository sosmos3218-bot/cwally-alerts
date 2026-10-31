#!/usr/bin/env python3
"""Hyundai Steel 004020 2026 Q3 prelim. Existing 09:00/15:20 slots only."""
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
CFG_PATH = ROOT / "configs" / "hsteelq3.json"
STATE_PATH = ROOT / "state" / "hsteelq3.json"
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


def first_num(text: str, pattern: str):
    m = re.search(pattern, text)
    return num(m.group(1)) if m else None


def to_eok(value: float | None, unit: str) -> float | None:
    if value is None:
        return None
    if unit == "조":
        return round(value * 10000, 1)
    if unit == "백만":
        return round(value / 100, 1)
    return round(value, 1)


def grab_op(text: str) -> float | None:
    patterns = [
        r"(?<![\-~])3분기 연결 영업이익(?:은|는|이)?\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*(억|백만|조)",
        r"(?<![\-~])연결 영업이익(?:은|는|이)?\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*(억|백만|조)",
        r"(?<![\-~])3분기 영업이익(?:은|는|이)?\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*(억|백만|조)",
    ]
    for pat in patterns:
        m = re.search(pat, text)
        if m and not re.search(r"전망|예상|컨센서스|추정", text[max(0, m.start() - 12):m.start()]):
            return to_eok(num(m.group(1)), m.group(2))
    return None


def parse_text(text: str, h1: dict) -> dict:
    q3 = bool(re.search(r"3분기|3Q", text))
    actual = bool(re.search(r"잠정|실적\s*발표|기록했|공시", text))
    forecast_only = bool(re.search(r"전망|예상|컨센서스", text)) and not actual
    op = grab_op(text) if q3 and actual and not forecast_only else None
    sales = None
    m = re.search(r"(?<![\-~])3분기 매출(?:액)?(?:은|는)?\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*(억|조)", text)
    if m and q3 and actual:
        sales = to_eok(num(m.group(1)), m.group(2))
    cum_op = first_num(text, r"(?:1~3분기|1-3분기|누적) 영업이익(?:은|는)?\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*억")
    if op is None and actual and q3 and cum_op and cum_op > float(h1["op_eok"]):
        op = round(cum_op - float(h1["op_eok"]), 1)
    spread_up = bool(re.search(r"스프레드 확대|롤마진 확대|롤마진 개선|판가 인상 효과|스프레드 개선", text))
    spread_down = bool(re.search(r"스프레드 축소|롤마진 축소|롤마진 악화|스프레드 하락", text))
    bars_narrow = bool(re.search(r"봉형강.{0,16}(적자|손실).{0,8}축소|봉형강.{0,10}흑자", text))
    bars_wide = bool(re.search(r"봉형강.{0,16}(적자|손실).{0,8}확대|봉형강.{0,12}적자 지속", text))
    bars_op = first_num(text, r"봉형강.{0,12}영업이익(?:은|는)?\s*([\-]?[0-9][0-9,]*(?:\.[0-9]+)?)\s*억")
    published = bool(q3 and actual and op is not None and not forecast_only)
    return {
        "published": published,
        "op": op,
        "sales": sales,
        "spread_up": spread_up,
        "spread_down": spread_down,
        "bars_narrow": bars_narrow,
        "bars_wide": bars_wide,
        "bars_op": bars_op,
    }


def verdict(p: dict, cfg: dict) -> str:
    if not p.get("published"):
        return "미확인"
    op = p.get("op")
    if not isinstance(op, float):
        return "미확인"
    if op < float(cfg["path_cut_op_eok"]) or p.get("spread_down") or p.get("bars_wide"):
        return "하향"
    if op < float(cfg["consensus_op_eok"]):
        return "경계"
    if p.get("spread_up") and not p.get("bars_wide"):
        return "상향열림"
    return "보류"


def fmt(v, suffix=""):
    return f"{v:,.1f}{suffix}" if isinstance(v, float) else "확인 불가"


def yn(flag: bool, empty: str = "문구 없음") -> str:
    return "있음" if flag else empty


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
    parsed = {
        "published": False,
        "op": None,
        "sales": None,
        "spread_up": False,
        "spread_down": False,
        "bars_narrow": False,
        "bars_wide": False,
        "bars_op": None,
    }
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
                "[STEEL] 005490/004020/103140 현대제철 3Q 조회 실패 | 판정 유보",
                [
                    f"{stamp} | PORT | {cfg['event_label']}",
                    "이게 뭐 알림: 현대제철 3분기 잠정 연결 영업이익을 1,200억 원과 비교하고, 판재 스프레드와 봉형강 적자가 연간 이익 경로를 바꾸는지 판정.",
                    f"지금 상태: 조회 실패. {err or '출처 없음'}. 숫자를 추정하지 않음.",
                    "지금 할 일: 다음 기존 슬롯(09:00 또는 15:20)까지 대기. 매수·매도 없음.",
                    "나중에: 원문이 열리면 한 번만 보냄. 만료 2026-11-13.",
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
    op = parsed.get("op")
    gap = round(op - float(cfg["consensus_op_eok"]), 1) if isinstance(op, float) else None
    if tag == "상향열림":
        title = "[STEEL] 005490/004020/103140 현대제철 3Q 1,200억 이상 | 회복 경로 열림"
        action = "연간 경로 상향 확정은 아님. 4분기 스프레드가 유지되는지 볼 때까지 추격 매수 없음."
    elif tag == "하향":
        title = "[STEEL] 005490/004020/103140 현대제철 3Q 경로 하향 | 신규 보류"
        action = "연간 이익 경로는 낮춰 보세요. 신규 매수 보류. 보유는 원문 확인 후 판단."
    elif tag == "경계":
        title = "[STEEL] 005490/004020/103140 현대제철 3Q 1,200억 미달 | 경로 유지 보류"
        action = "컨센서스 미달. 연간 상향은 하지 마세요. 스프레드 문구가 좋아도 이익이 기준 아래면 보류."
    elif tag == "보류":
        title = "[STEEL] 005490/004020/103140 현대제철 3Q 이익만 확인 | 경로 변경 안 함"
        action = "영업이익은 1,200억 이상이지만 판재 스프레드 또는 봉형강 적자 폭이 비었음. 연간 경로는 바꾸지 마세요."
    else:
        title = "[STEEL] 005490/004020/103140 현대제철 3Q 아직 없음 | 판정 유보"
        action = "잠정 숫자가 안 보입니다. 다음 공시까지 대기. 경로 변경 없음."
    lines = [
        f"{stamp} | PORT | {cfg['event_label']} | {tag}",
        "이게 뭐 알림: 3분기 연결 영업이익을 컨센서스 1,200억 원과 비교하고, 판재 스프레드와 봉형강 적자 폭이 2026년 이익 경로를 바꾸는지 판정.",
        (
            f"지금 상태: {tag}. 3Q 연결 영업이익 {fmt(op, '억')} / 기준 1,200억 / 하향선 1,000억. "
            f"격차 {fmt(gap, '억')}. 매출 {fmt(parsed.get('sales'), '억')}. "
            f"판재 스프레드 개선 {yn(parsed.get('spread_up'))}, 축소 {yn(parsed.get('spread_down'))}. "
            f"봉형강 적자 축소 {yn(parsed.get('bars_narrow'))}, 확대 {yn(parsed.get('bars_wide'))}, "
            f"봉형강 영업이익 {fmt(parsed.get('bars_op'), '억')}. "
            f"상반기 영업이익 {h1['op_eok']}억(1Q {h1['q1_op_eok']}+2Q {h1['q2_op_eok']})은 비교용이며 이번 조회에서 재검증하지 않음."
        ),
        f"지금 할 일: {action}",
        "나중에: 같은 결과는 다시 안 보냄. 창은 2026-11-13 15:20까지. 장후 잠정은 다음 09:00에 확인. 그 뒤 침묵.",
        "익절/줄일 때: 이 알림은 경로 판정이지 매도 지시가 아님. 1,200억 초과만으로 익절하지 마세요.",
        "하지 말 것: 전망 기사를 잠정으로 쓰기. 조회 실패를 호재로 읽기. 추격, 신용, 미수. 투자 권유 아님.",
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
    state["last_key"] = json.dumps({"tag": tag, "op": parsed.get("op")}, sort_keys=True)
    save_state(state, now)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
