#!/usr/bin/env python3
"""LG Innotek 011070 2026 Q3 one-shot. Existing dispatcher slots only."""
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
CFG_PATH = ROOT / "configs" / "lgiq3.json"
STATE_PATH = ROOT / "state" / "lgiq3.json"
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


def to_eok(value: float | None, unit: str) -> float | None:
    if value is None:
        return None
    if unit == "조":
        return round(value * 10000, 1)
    if unit == "백만":
        return round(value / 100, 1)
    return round(value, 1)


def near_forecast(text: str, start: int) -> bool:
    window = text[max(0, start - 16):start]
    return bool(re.search(r"전망|예상|컨센서스|추정", window))


def grab_metric(text: str, labels: str) -> float | None:
    pat = rf"(?<![\-~])(?:{labels})(?:은|는|이)?\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*(억|백만|조)"
    for m in re.finditer(pat, text):
        if near_forecast(text, m.start()):
            continue
        return to_eok(num(m.group(1)), m.group(2))
    return None


def parse_text(text: str) -> dict:
    q3 = bool(re.search(r"3분기|3Q", text))
    actual = bool(re.search(r"잠정|실적\s*발표|기록했|공시", text))
    forecast_only = bool(re.search(r"전망|예상|컨센서스", text)) and not actual
    published = bool(q3 and actual and not forecast_only)
    op = grab_metric(text, r"3분기 연결 영업이익|연결 영업이익|3분기 영업이익") if published else None
    sales = grab_metric(text, r"3분기 매출액|3분기 매출|연결 매출액") if published else None
    optical = grab_metric(text, r"광학솔루션 영업이익|광학 솔루션 영업이익|광학솔루션사업 영업이익") if published else None
    package = grab_metric(text, r"패키지솔루션 영업이익|패키지 솔루션 영업이익|기판소재 영업이익") if published else None
    q4_op = grab_metric(text, r"4분기 영업이익|4분기 연결 영업이익") if published else None
    fx = bool(re.search(r"환율|원화 강세|재고 시차|원재료.{0,8}시차|고환율", text))
    share_down = bool(re.search(r"점유율 하락|점유율 축소|ASP 하락|판매단가 하락|고객 이탈", text))
    guide_down = bool(re.search(r"가이던스 하향|전망 하향|4분기 부진|출하 지연 지속", text))
    guide_ok = bool(re.search(r"가이던스 유지|가이던스 상향|4분기 회복|4분기 개선", text))
    pkg_down = bool(re.search(r"패키지솔루션.{0,12}(감소|부진|적자)|기판.{0,8}(감소|부진)", text))
    if not published:
        op = sales = optical = package = q4_op = None
    return {
        "published": bool(published and op is not None),
        "op": op,
        "sales": sales,
        "optical": optical,
        "package": package,
        "q4_op": q4_op,
        "fx": fx,
        "share_down": share_down,
        "guide_down": guide_down,
        "guide_ok": guide_ok,
        "pkg_down": pkg_down,
    }


def fx_judge(p: dict) -> str:
    op = p.get("op")
    if not isinstance(op, float):
        return "확인 불가"
    if p.get("share_down") or p.get("guide_down") or op < 1400:
        return "틀림"
    if 1400 <= op <= 1800 and p.get("fx"):
        return "맞음"
    if 1400 <= op <= 1800:
        return "일부"
    if op > 1800:
        return "상회"
    return "확인 불가"


def action(p: dict) -> str:
    if not p.get("published"):
        return "유보"
    op = p.get("op")
    if not isinstance(op, float):
        return "유보"
    if op < 1400 or p.get("guide_down") or p.get("share_down") or p.get("pkg_down"):
        return "축소"
    if op >= 2000 and not p.get("guide_down") and not p.get("share_down") and not p.get("pkg_down"):
        return "추가매수"
    if op >= 1400:
        return "보유"
    return "유보"


def fmt(v, suffix=""):
    return f"{v:,.0f}{suffix}" if isinstance(v, float) else "확인 불가"


def yn(flag: bool) -> str:
    return "있음" if flag else "없음"


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    today = now.strftime("%Y-%m-%d")
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    state = load_json(STATE_PATH, {"result_notified": False, "empty_notified": False, "fail_notified": False, "last_key": None, "updated_at": None})
    in_year = now.year == int(cfg["year_guard"])
    in_window = cfg["window_start"] <= today <= cfg["window_end"]
    print(f"{stamp} mode={mode} window={in_window}")
    if mode not in {"force", "dry"} and (not in_year or not in_window):
        print("outside 2026 window; silent")
        return 0
    parsed = {"published": False, "op": None, "sales": None, "optical": None, "package": None, "q4_op": None, "fx": False, "share_down": False, "guide_down": False, "guide_ok": False, "pkg_down": False}
    ok = False
    err = ""
    for url in cfg["sources"]:
        try:
            text = strip_html(fetch(url))
            ok = True
            got = parse_text(text)
            if got.get("published"):
                parsed = got
                break
            parsed = got
        except Exception as exc:
            err = str(exc)
            print(f"fetch fail {url}: {exc}")
    if mode != "dry" and not ok:
        if not state.get("fail_notified"):
            emit(cfg, "[LGIT] LG이노텍 3Q 조회 실패 | 판정 유보", [
                f"{stamp} | PORT | {cfg['event_label']}",
                "이게 뭐 알림: LG이노텍 3분기 매출·영업이익·광학·패키지 이익과 4분기 가이던스를 컨센서스와 비교하고 보유·추가매수·축소를 고릅니다.",
                f"지금 상태: 조회 실패. {err or '출처 없음'}. 숫자를 추정하지 않음.",
                "지금 할 일: 다음 슬롯까지 대기. 매수·매도 없음.",
                "나중에: 원문이 열리면 한 번만 보냄. 마지막 2026-11-03 09:10.",
                "익절/줄일 때: 실패 알림으로 포지션을 바꾸지 마세요.",
                "하지 말 것: 조회 실패를 호재나 악재로 읽기. 추격, 신용, 미수.",
            ], 4)
            state["fail_notified"] = True
        save_state(state, now)
        return 0
    act = action(parsed)
    fx = fx_judge(parsed)
    last = today >= cfg["window_end"]
    if not parsed.get("published") and not last and mode not in {"force", "dry"}:
        print("not published yet; silent")
        save_state(state, now)
        return 0
    if parsed.get("published") and state.get("result_notified") and mode != "force":
        print("result already sent; silent")
        return 0
    if not parsed.get("published") and state.get("empty_notified") and mode != "force":
        print("empty already sent; silent")
        return 0
    c = cfg["consensus"]
    op = parsed.get("op")
    gap = round(op - float(c["house_op_hi_eok"]), 1) if isinstance(op, float) else None
    if act == "추가매수":
        title = "[LGIT] LG이노텍 3Q 하우스 상회 | 추가매수"
        todo = "계획 수량의 일부만 추가. 오늘 급등분 추격은 하지 마세요."
    elif act == "축소":
        title = "[LGIT] LG이노텍 3Q 기준 미달 | 축소"
        todo = "신규 매수 중단. 보유분은 원문 확인 후 일부 축소."
    elif act == "보유":
        title = "[LGIT] LG이노텍 3Q 시차 부합 | 보유"
        todo = "보유 유지. 추가매수는 4분기 가이던스가 숫자로 확인된 뒤로 미루세요."
    else:
        title = "[LGIT] LG이노텍 3Q 아직 없음 | 판정 유보"
        todo = "잠정 숫자가 안 보입니다. 매수·매도 없음."
    lines = [
        f"{stamp} | PORT | {cfg['event_label']} | {act}",
        "이게 뭐 알림: 3분기 매출, 영업이익, 광학·패키지 이익, 4분기 가이던스를 컨센서스와 비교하고 환율 시차 가정을 판정한 뒤 보유·추가매수·축소 중 하나를 고릅니다.",
        (
            f"지금 상태: {act}. 환율 시차 {fx}. 영업이익 {fmt(op, '억')} / 하우스 1,389~1,501억 / 하향 전 컨센서스 3,020억. "
            f"하우스 상단 격차 {fmt(gap, '억')}. 매출 {fmt(parsed.get('sales'), '억')} / 밴드 5.70조~6.32조. "
            f"광학 이익 {fmt(parsed.get('optical'), '억')}, 패키지 이익 {fmt(parsed.get('package'), '억')}, 4분기 이익 문구 {fmt(parsed.get('q4_op'), '억')}. "
            f"환율 문구 {yn(parsed.get('fx'))}, 가이던스 하향 {yn(parsed.get('guide_down'))}, 가이던스 유지 {yn(parsed.get('guide_ok'))}, "
            f"점유율 훼손 {yn(parsed.get('share_down'))}, 패키지 하락 {yn(parsed.get('pkg_down'))}."
        ),
        f"지금 할 일: {todo}",
        "나중에: 같은 결과는 다시 안 보냄. 창은 2026-11-03 09:10까지. 그 뒤 침묵.",
        "익절/줄일 때: 2,000억 초과만으로 익절하지 마세요. 1,400억 미만이거나 가이던스 하향이면 축소.",
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
    state["last_key"] = json.dumps({"act": act, "fx": fx, "op": parsed.get("op")}, sort_keys=True)
    save_state(state, now)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
