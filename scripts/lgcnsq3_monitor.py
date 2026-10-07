#!/usr/bin/env python3
"""LG CNS 064400 2026 Q3 one-shot. Existing dispatcher slots only."""
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
CFG_PATH = ROOT / "configs" / "lgcnsq3.json"
STATE_PATH = ROOT / "state" / "lgcnsq3.json"
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
        return resp.read().decode("utf-8", errors="ignore")


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


def parse_news(text: str) -> dict:
    q3 = bool(re.search(r"3분기|3Q", text))
    actual = bool(re.search(r"잠정|실적\s*발표|기록했|공시", text))
    forecast_only = bool(re.search(r"전망|예상|컨센서스", text)) and not actual
    published = bool(q3 and actual and not forecast_only)
    op = grab_metric(text, r"3분기 연결 영업이익|연결 영업이익|3분기 영업이익") if published else None
    sales = grab_metric(text, r"3분기 매출액|3분기 매출|연결 매출액") if published else None
    date_m = re.search(r"(2026)[.\-/년\s]+(10|11)[.\-/월\s]+([0-3]?\d)", text)
    date_hit = None
    if date_m and re.search(r"실적|잠정|발표", text[max(0, date_m.start() - 24):date_m.end() + 24]):
        date_hit = f"{date_m.group(1)}-{int(date_m.group(2)):02d}-{int(date_m.group(3)):02d}"
    dbo_up = bool(re.search(r"데이터센터.{0,16}(증가|확대|인식)|DBO.{0,12}(증가|수주)", text))
    dbo_delay = bool(re.search(r"데이터센터.{0,16}(지연|취소)|삼송.{0,8}지연|죽전.{0,8}지연", text))
    gpu_up = bool(re.search(r"3,814억|3814억|GPU.{0,12}(증액|확대)", text))
    target_cut = bool(re.search(r"목표주가.{0,8}(하향|낮)|투자의견.{0,6}하향", text))
    if not published:
        op = sales = None
    margin = round(op / sales * 100, 1) if isinstance(op, float) and isinstance(sales, float) and sales else None
    return {
        "published": bool(published and op is not None),
        "op": op,
        "sales": sales,
        "margin": margin,
        "date_hit": date_hit,
        "dbo_up": dbo_up,
        "dbo_delay": dbo_delay,
        "gpu_up": gpu_up,
        "target_cut": target_cut,
    }


def parse_consensus(text: str, base: dict) -> dict:
    targets = [num(x) for x in re.findall(r"목표주가\s*([0-9][0-9,]{4,})", text)]
    targets = [t for t in targets if isinstance(t, float) and 50000 <= t <= 200000]
    target = targets[0] if targets else None
    eps_vals = [num(x) for x in re.findall(r"EPS\s*([0-9][0-9,]{3,})", text)]
    eps_vals = [v for v in eps_vals if isinstance(v, float) and 500 <= v <= 20000]
    fy_eps = eps_vals[0] if eps_vals else None
    cut = bool(re.search(r"하향|낮춤", text)) and target is not None and target < float(base["target"])
    return {"target": target, "fy_eps": fy_eps, "cut_vs_baseline": cut}


def verdict(news: dict, cons: dict, base: dict) -> str:
    if not news.get("published"):
        if cons.get("cut_vs_baseline") or news.get("target_cut"):
            return "추정 하향"
        return "발표 전"
    op = news.get("op")
    margin = news.get("margin")
    floor = float(base.get("h1_margin_pct") or 7.8)
    eps_fail = cons.get("fy_eps") is not None and cons["fy_eps"] < 4730
    margin_fail = (isinstance(margin, float) and margin < floor) or (isinstance(op, float) and op <= 1202)
    if margin_fail and (eps_fail or news.get("target_cut") or cons.get("cut_vs_baseline")):
        return "매력 하락"
    if isinstance(margin, float) and margin > floor and isinstance(op, float) and op > 1202 and not eps_fail:
        return "매력 상향"
    return "보류"


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
    state = load_json(STATE_PATH, {"sent_dates": [], "result_notified": False, "fail_dates": [], "last_key": None, "updated_at": None})
    in_year = now.year == int(cfg["year_guard"])
    in_window = cfg["window_start"] <= today <= cfg["window_end"]
    print(f"{stamp} mode={mode} window={in_window}")
    if mode not in {"force", "dry"} and (not in_year or not in_window):
        print("outside 2026 window; silent")
        return 0
    news = {"published": False, "op": None, "sales": None, "margin": None, "date_hit": None, "dbo_up": False, "dbo_delay": False, "gpu_up": False, "target_cut": False}
    cons = {"target": None, "fy_eps": None, "cut_vs_baseline": False}
    ok = False
    err = ""
    for url in cfg["sources"]:
        try:
            text = strip_html(fetch(url))
            ok = True
            if "fnguide" in url:
                cons = parse_consensus(text, cfg["baseline"])
            else:
                got = parse_news(text)
                if got.get("published") or not news.get("published"):
                    news = got
        except Exception as exc:
            err = str(exc)
            print(f"fetch fail {url}: {exc}")
    b = cfg["baseline"]
    if mode != "dry" and not ok:
        fails = list(state.get("fail_dates") or [])
        if today not in fails:
            emit(cfg, "[LG] 064400 조회 실패 | 판정 유보", [
                f"{stamp} | PORT | {cfg['event_label']}",
                "이게 뭐 알림: LG씨엔에스 064400 중립 상단 판단의 변경 조건, 3분기 마진과 목표주가 하향을 점검합니다.",
                f"지금 상태: 조회 실패. {err or '출처 없음'}. 숫자를 추정하지 않음.",
                "지금 할 일: 다음 슬롯까지 대기. 매수·매도 없음.",
                "나중에: 같은 날 실패는 다시 안 보냄. 마지막 2026-11-16 09:10.",
                "익절/줄일 때: 실패 알림으로 포지션을 바꾸지 마세요.",
                "하지 말 것: 조회 실패를 호재나 악재로 읽기. 추격, 신용, 미수.",
            ], 4)
            fails.append(today)
            state["fail_dates"] = fails
        save_state(state, now)
        return 0
    act = verdict(news, cons, b)
    sent = list(state.get("sent_dates") or [])
    if news.get("published") and state.get("result_notified") and mode != "force":
        print("result already sent; silent")
        return 0
    if not news.get("published") and today in sent and mode not in {"force", "dry"}:
        print("slot already sent; silent")
        return 0
    eps_7 = float(b["q3_eps"]) - float(b["q3_eps_7d"])
    eps_30 = float(b["q3_eps"]) - float(b["q3_eps_30d"])
    fy_7 = float(b["fy_eps"]) - float(b["fy_eps_7d"])
    fy_30 = float(b["fy_eps"]) - float(b["fy_eps_30d"])
    live_target = cons.get("target")
    live_eps = cons.get("fy_eps")
    if act == "매력 상향":
        title = "[LG] 064400 3Q 마진 상회 | 판단 상향"
        todo = "상반기 마진을 넘었습니다. 신규는 나눠 보세요. 추격은 하지 마세요."
    elif act == "매력 하락":
        title = "[LG] 064400 3Q 미스+하향 | 신규 중단"
        todo = "실적 미스와 추정 하향이 겹쳤습니다. 신규 매수 중단."
    elif act == "추정 하향":
        title = "[LG] 064400 목표 하향 | 신규 보류"
        todo = "실적 전 목표 하향입니다. 발표 숫자 나오기 전 추가매수 없음."
    elif act == "보류":
        title = "[LG] 064400 3Q 확인 부족 | 보류"
        todo = "마진과 추정 하향이 기준을 동시에 통과하지 않았습니다. 추가매수 없음."
    else:
        title = "[LG] 064400 중립 상단 | 신규 보류"
        todo = "잠정 숫자가 없습니다. 매수·매도 없음. 3분기 마진이 상반기 7.8%를 넘고 수주 마진이 확인되기 전 추가매수 없음."
    lines = [
        f"{stamp} | PORT | {cfg['event_label']} | {act}",
        "이게 뭐 알림: 064400 현 판단은 중립 상단입니다. 3분기 발표, 상반기 마진 7.8% 상회, 목표주가 하향을 확인합니다.",
        (
            f"지금 상태: {act}. 공식 일정 {news.get('date_hit') or '미확인'}. "
            f"기준 종가 {b['price']:,}원({cfg['baseline_asof']}, 목표가 {b['target']:,}원, 괴리 약 33%). "
            f"상반기 영업이익 +1.1%, 2분기 영업이익 -9.2%. "
            f"3분기 EPS {b['q3_eps']}원, 7일 전 대비 {eps_7:+.0f}원, 30일 전 대비 {eps_30:+.0f}원. "
            f"2026년 EPS {b['fy_eps']}원, 7일 전 대비 {fy_7:+.0f}원, 30일 전 대비 {fy_30:+.0f}원. "
            f"당일 조회 목표 {fmt(live_target, '원')}, 당일 EPS {fmt(live_eps, '원')}. "
            f"알려진 하향: {b['known_cut']}. 기사 하향 문구 {yn(news.get('target_cut'))}. "
            f"잠정 영업이익 {fmt(news.get('op'), '억')}, 매출 {fmt(news.get('sales'), '억')}, 마진 {news.get('margin') if news.get('margin') is not None else '확인 불가'}. "
            f"DBO 증가 문구 {yn(news.get('dbo_up'))}, 지연 문구 {yn(news.get('dbo_delay'))}, GPU 증액 문구 {yn(news.get('gpu_up'))}."
        ),
        f"지금 할 일: {todo}",
        "나중에: 발표 전은 슬롯마다 1회. 잠정 결과는 1회만. 마지막 2026-11-16 09:10. 그 뒤 침묵.",
        "익절/줄일 때: 3분기 마진이 7.8%를 넘고 영업이익이 1,202억을 넘으며 연간 EPS가 4,730원 이상이면 판단 상향. 마진 실패에 추정 하향이 겹치면 신규 중단.",
        "하지 말 것: 전망 기사를 잠정으로 쓰기. 조회 실패를 호재로 읽기. 추격, 신용, 미수. 투자 권유 아님.",
        f"출처: {cfg['sources'][0]}",
        f"조회: {'OK' if ok else '실패'} {err}",
    ]
    if mode == "dry":
        print(title)
        print("\n".join(lines))
        print("dry-run; no ntfy")
        return 0
    emit(cfg, title, lines, 4)
    if news.get("published"):
        state["result_notified"] = True
    if today not in sent:
        sent.append(today)
    state["sent_dates"] = sent
    state["last_key"] = json.dumps({"act": act, "op": news.get("op"), "target": cons.get("target")}, sort_keys=True, ensure_ascii=False)
    save_state(state, now)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
