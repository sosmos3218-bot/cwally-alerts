#!/usr/bin/env python3
"""LG Energy Solution 373220 2026 Q3 prelim one-shot. Existing dispatcher only."""
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
CFG_PATH = ROOT / "configs" / "lgesq3.json"
STATE_PATH = ROOT / "state" / "lgesq3.json"
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
    window = text[max(0, start - 18):start]
    return bool(re.search(r"전망|예상|컨센서스|추정|목표", window))


def grab_jo_eok(text: str, labels: str) -> float | None:
    pat = rf"(?:{labels})(?:은|는|이)?\s*([0-9]+(?:\.[0-9]+)?)조\s*([0-9][0-9,]*)억"
    for m in re.finditer(pat, text):
        if near_forecast(text, m.start()):
            continue
        jo = num(m.group(1))
        eok = num(m.group(2))
        if jo is None or eok is None:
            continue
        return round(jo * 10000 + eok, 1)
    return None


def grab_signed(text: str, labels: str) -> float | None:
    pat = rf"(?:{labels})(?:은|는|이)?\s*(-?[0-9][0-9,]*(?:\.[0-9]+)?)\s*(억|백만|조)"
    for m in re.finditer(pat, text):
        if near_forecast(text, m.start()):
            continue
        return to_eok(num(m.group(1)), m.group(2))
    return None


def grab_metric(text: str, labels: str) -> float | None:
    pat = rf"(?<![~\-])(?:{labels})(?:은|는|이)?\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*(억|백만|조)"
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
    sales = grab_jo_eok(text, r"3분기 매출액|3분기 매출|연결 매출액|매출액") if published else None
    if published and sales is None:
        sales = grab_metric(text, r"3분기 매출액|3분기 매출|연결 매출액|매출액")
    op = grab_metric(text, r"3분기 연결 영업이익|연결 영업이익|3분기 영업이익|영업이익") if published else None
    ampc = grab_metric(text, r"AMPC|첨단제조생산세액공제|세액공제") if published else None
    ex = grab_signed(text, r"AMPC 제외 영업이익|세액공제 제외 영업이익|보조금 제외 영업이익") if published else None
    if published and ex is None and isinstance(op, float) and isinstance(ampc, float):
        ex = round(op - ampc, 1)
    ess = None
    if published:
        m = re.search(r"ESS[^0-9]{0,24}수주\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*GWh", text)
        if not m:
            m = re.search(r"신규 수주\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*GWh", text)
        if m and not near_forecast(text, m.start()):
            ess = num(m.group(1))
    ess_delay = bool(re.search(r"ESS.{0,16}(수주 지연|수주 부진|목표 하회|미달)", text))
    poland_ok = bool(re.search(r"폴란드.{0,16}(BEP|손익분기|흑자 전환|흑자전환)(?!\s*지연)", text))
    poland_no = bool(re.search(r"폴란드.{0,20}(적자 확대|BEP 지연|손익분기 지연|가동률 부진)", text))
    if not published:
        sales = op = ampc = ex = ess = None
        ess_delay = poland_ok = poland_no = False
    return {
        "published": bool(published and op is not None),
        "sales": sales,
        "op": op,
        "ampc": ampc,
        "ex": ex,
        "ess_gwh": ess,
        "ess_delay": ess_delay,
        "poland_ok": poland_ok,
        "poland_no": poland_no,
    }


def judge(p: dict, c: dict) -> str:
    if not p.get("published"):
        return "유보"
    op = p.get("op")
    ex = p.get("ex")
    if not isinstance(op, float):
        return "유보"
    op_cut = round(float(c["op_eok"]) * 0.85, 1)
    if p.get("ess_delay") or p.get("poland_no") or op < op_cut or (isinstance(ex, float) and ex < -800):
        return "하향"
    if isinstance(ex, float) and ex >= 0 and op >= float(c["op_eok"]) and not p.get("ess_delay"):
        return "상향"
    return "유지"


def fmt(v, suffix=""):
    return f"{v:,.0f}{suffix}" if isinstance(v, float) else "확인 불가"


def gap(v, base):
    return f"{v - base:+,.0f}억" if isinstance(v, float) else "확인 불가"


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
    parsed = {"published": False, "sales": None, "op": None, "ampc": None, "ex": None, "ess_gwh": None, "ess_delay": False, "poland_ok": False, "poland_no": False}
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
            emit(cfg, "[LG] LG엔솔 3Q 조회 실패 | 판정 유보", [
                f"{stamp} | PORT | {cfg['event_label']}",
                "이게 뭐 알림: LG에너지솔루션 3분기 잠정 매출, 영업이익, AMPC 제외 영업이익을 컨센서스와 비교하고 39만 원 밸류에이션을 다시 봅니다.",
                f"지금 상태: 조회 실패. {err or '출처 없음'}. 숫자를 만들지 않음.",
                "지금 할 일: 다음 슬롯까지 대기. 매수·매도 없음.",
                "나중에: 원문이 열리면 한 번만 보냄. 마지막 2026-10-30 09:12.",
                "익절/줄일 때: 실패 알림으로 포지션을 바꾸지 마세요.",
                "하지 말 것: 조회 실패를 호재나 악재로 읽기. 추격, 신용, 미수.",
            ], 4)
            state["fail_notified"] = True
        save_state(state, now)
        return 0
    c = cfg["consensus"]
    act = judge(parsed, c)
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
    anchor = cfg["anchor"]["price"]
    if act == "상향":
        title = "[LG] LG엔솔 3Q 본업 흑자 | 39만 부담 완화"
        todo = "추격 금지. 본업 흑자가 잠정 원문에 있으면 관심만. 신규는 나눠서."
        later = "39만 선반영 부담은 줄었음. 4분기 ESS 수주가 90GWh 경로에 못 미치면 다시 중립."
    elif act == "하향":
        title = "[LG] LG엔솔 3Q 컨센서스 미달 | 39만 비쌈"
        todo = "신규 매수 중단. 보유분은 원문 확인 후 일부만 줄이세요."
        later = "AMPC 제외 적자가 줄거나 ESS 수주가 목표 경로로 오면 재평가."
    elif act == "유지":
        title = "[LG] LG엔솔 3Q 컨센서스 부합 | 39만 중립"
        todo = "39만 추격 금지. 보유면 유지, 신규는 4분기 본업 흑자 확인 뒤로."
        later = "AMPC 제외 흑자 또는 ESS 90GWh 근접이 나오면 다시 봄."
    else:
        title = "[LG] LG엔솔 3Q 아직 없음 | 판정 유보"
        todo = "잠정 숫자가 안 보입니다. 매수·매도 없음."
        later = "창은 2026-10-30 09:12까지. 그 뒤 침묵."
    lines = [
        f"{stamp} | PORT | {cfg['event_label']} | {act}",
        "이게 뭐 알림: 3분기 잠정 매출, 영업이익, AMPC 제외 영업이익을 컨센서스와 비교하고 ESS 수주·폴란드 BEP를 반영해 39만 원을 다시 봅니다.",
        (
            f"지금 상태: {act}. 매출 {fmt(parsed.get('sales'), '억')} / 컨센서스 8.96조({gap(parsed.get('sales'), c['sales_eok'])}). "
            f"영업이익 {fmt(parsed.get('op'), '억')} / 3,491억({gap(parsed.get('op'), c['op_eok'])}). "
            f"AMPC {fmt(parsed.get('ampc'), '억')}. AMPC 제외 {fmt(parsed.get('ex'), '억')} / -495억({gap(parsed.get('ex'), c['ex_ampc_eok'])}). "
            f"ESS 수주 {fmt(parsed.get('ess_gwh'), 'GWh')} / 목표 90, 10월 2일 추정 20~30. 지연 문구 {yn(parsed.get('ess_delay'))}. "
            f"폴란드 BEP 문구 {yn(parsed.get('poland_ok'))}, 부인 문구 {yn(parsed.get('poland_no'))}. "
            f"앵커 2026-10-06 종가 {anchor:,}원."
        ),
        f"지금 할 일: {todo}",
        f"나중에: {later} 같은 결과는 다시 안 보냄.",
        "익절/줄일 때: 컨센서스 부합만으로 익절하지 마세요. 본업 적자 확대나 수주 지연이면 신규를 줄이세요.",
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
    state["last_key"] = json.dumps({"act": act, "op": parsed.get("op"), "ex": parsed.get("ex")}, sort_keys=True)
    save_state(state, now)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
