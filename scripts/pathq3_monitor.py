#!/usr/bin/env python3
"""UiPath FY27 Q3 oneshot. Year-guard 2026. Silent outside Dec 3-5 KST."""
from __future__ import annotations

import json
import re
import sys
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
CFG_PATH = ROOT / "configs" / "pathq3.json"
STATE_PATH = ROOT / "state" / "pathq3.json"
UA = {"User-Agent": "cwally-alerts pathq3 monitor contact@example.com"}
SEC_UA = {
    "User-Agent": "cwally-alerts research monitor contact@example.com",
    "Accept": "application/json",
}
RESULT_RE = re.compile(
    r"third quarter fiscal 2027|q3 fiscal 2027|quarter ended october 31, 2026|three months ended october 31, 2026",
    re.I,
)
OLD_RE = re.compile(r"second quarter fiscal 2027 results", re.I)


def fetch(url: str, headers: dict | None = None) -> str:
    req = urllib.request.Request(url, headers=headers or UA)
    with urllib.request.urlopen(req, timeout=25) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def visible(text: str) -> str:
    text = re.sub(r"<script[\s\S]*?</script>", " ", text, flags=re.I)
    text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text)


def money_m(blob: str, pattern: str):
    m = re.search(pattern, blob, re.I)
    if not m:
        return None
    val = float(m.group(1).replace(",", ""))
    unit = (m.group(2) or "").lower()
    if unit.startswith("b"):
        return round(val * 1000, 1)
    return round(val, 1)


def pct_near(blob: str, label: str):
    m = re.search(label + r".{0,80}?([0-9]+(?:\.[0-9]+)?)\s*(?:percent|%)", blob, re.I)
    if not m:
        return None
    return float(m.group(1))


def parse_metrics(blob: str, base_arr_m: float) -> dict:
    net_new = money_m(
        blob,
        r"net new arr(?:\s+of)?\s+\$([0-9]+(?:\.[0-9]+)?)\s*(million|billion)?",
    )
    arr = money_m(
        blob,
        r"(?:annualized recurring revenue|ARR) of \$([0-9]+(?:\.[0-9]+)?)\s*(million|billion)",
    )
    derived = None
    if net_new is None and arr is not None and arr > 1500:
        derived = round(arr - base_arr_m, 1)
    nrr = pct_near(blob, r"(?:dollar[- ]based net retention rate|net retention rate|net revenue retention)")
    mil_yoy = None
    m = re.search(
        r"\$1 million or more of ARR,? up\s+([0-9]+(?:\.[0-9]+)?)\s*(?:percent|%)",
        blob,
        re.I,
    )
    if not m:
        m = re.search(
            r"([0-9,]+)\s+(?:customers|customer[s]?) with \$1 million or more of ARR,? up\s+([0-9]+(?:\.[0-9]+)?)\s*(?:percent|%)",
            blob,
            re.I,
        )
        if m:
            mil_yoy = float(m.group(2))
    else:
        mil_yoy = float(m.group(1))
    return {
        "net_new_m": net_new,
        "net_new_derived_m": derived,
        "arr_m": arr,
        "nrr": nrr,
        "million_yoy": mil_yoy,
        "official_release": bool(RESULT_RE.search(blob)),
    }


def mark(value, line: float, derived: bool = False) -> str:
    if value is None:
        return "해석불가"
    status = "충족" if value >= line else "미달"
    return status + ("(산출)" if derived else "")


def judge(m: dict, lines: dict) -> tuple[str, list[str], dict]:
    net = m.get("net_new_m")
    derived = False
    if net is None and m.get("net_new_derived_m") is not None:
        net = m["net_new_derived_m"]
        derived = True
    rows = {
        "순증ARR": mark(net, lines["net_new_arr_m"], derived),
        "NRR": mark(m.get("nrr"), lines["nrr"]),
        "100만고객": mark(m.get("million_yoy"), lines["million_customer_yoy"]),
    }
    scored = [v.replace("(산출)", "") for v in rows.values()]
    if scored.count("해석불가") == 3:
        verdict = "해석불가"
    elif scored.count("충족") == 3:
        verdict = "재가속"
    elif scored.count("충족") == 2 and "미달" in scored:
        verdict = "보류"
    elif "미달" in scored and scored.count("해석불가") <= 1:
        verdict = "기각"
    else:
        verdict = "해석불가"
    shown = [f"{k}:{v}" for k, v in rows.items()]
    return verdict, shown, rows


def sec_doc(cfg: dict) -> tuple[str, str]:
    raw = fetch(f"https://data.sec.gov/submissions/CIK{cfg['cik']}.json", SEC_UA)
    data = json.loads(raw)
    recent = data.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])
    acc = recent.get("accessionNumber", [])
    docs = recent.get("primaryDocument", [])
    floor = cfg["filing_not_before"]
    for i, form in enumerate(forms):
        if form not in {"8-K", "8-K/A"}:
            continue
        if dates[i] < floor:
            continue
        accession = acc[i].replace("-", "")
        url = f"https://www.sec.gov/Archives/edgar/data/{int(cfg['cik'])}/{accession}/{docs[i]}"
        text = visible(fetch(url, SEC_UA))
        if RESULT_RE.search(text) and not OLD_RE.search(text[:500]):
            return url, text
    return "", ""


def fmt_num(value, suffix=""):
    if value is None:
        return "없음"
    return f"{value:g}{suffix}"


def body(cfg: dict, now: datetime, verdict: str, rows: list[str], m: dict, source: str) -> str:
    action = {
        "재가속": "세 칸 충족. 비중 확대는 분할만 보고 장중 추격은 하지 마세요.",
        "보류": "두 칸만 충족. 비중을 유지하고 나머지 칸 원문을 확인하세요.",
        "기각": "판정선 미달. 재가속 시나리오를 접고 저성장 수익성 종목으로 보세요.",
        "해석불가": "숫자 확인 전입니다. 충족이나 미달로 처리하지 마세요.",
    }[verdict]
    net = m.get("net_new_m")
    if net is None:
        net_txt = f"산출 {fmt_num(m.get('net_new_derived_m'), '백만달러')}" if m.get("net_new_derived_m") is not None else "없음"
    else:
        net_txt = f"{net:g}백만달러"
    lines = [
        f"{now.strftime('%Y-%m-%d %H:%M KST')} | PATH | FY27 3분기",
        "이게 뭐 알림: UiPath 3분기 실적을 재가속 판정선으로 보는 일회성 알림입니다.",
        f"지금 결과/상태: {verdict}. {' / '.join(rows)}.",
        f"순증 ARR {net_txt} (선 5,000만) | NRR {fmt_num(m.get('nrr'), '%')} (선 110) | 100만달러 고객 증가 {fmt_num(m.get('million_yoy'), '%')} (선 20).",
        "비교: 2분기 순증 3,700만, NRR 109%, 100만달러 고객 387곳 +21%. 3분기 ARR 가이던스 19.92~19.97억달러.",
        f"지금 할 일: {action}",
        "나중에: 12-05 이후 이 알림은 다시 오지 않습니다. Microsoft는 물량, ServiceNow는 업무 예산, Automation Anywhere는 신규 에이전트 예산 잠식입니다.",
        "익절/줄일 때: 재가속이어도 16~19달러는 이미 거부된 구간입니다. 기각이면 추가매수 중단.",
        "하지 말 것: 조회 실패를 미달로 보기, 해석불가를 충족으로 보기, 신용 추격.",
        f"출처: {source or '없음'}",
    ]
    return "\n".join(lines)


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    today = now.date()
    topic = resolve_topic("port", cfg.get("ntfy_topic"))
    server = resolve_server(cfg.get("ntfy_server"))
    state = {
        "updated_at": None,
        "phase": "idle",
        "results_notified": False,
        "miss_notified": False,
        "fail_notified": False,
        "source": None,
        "verdict": None,
        "parsed": None,
    }
    if STATE_PATH.exists():
        try:
            state.update(json.loads(STATE_PATH.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass
    start = date.fromisoformat(cfg["window_start"])
    end = date.fromisoformat(cfg["window_end"])
    year_ok = today.year == int(cfg["oneshot_year"])
    if mode not in {"force", "event"} and not (year_ok and start <= today <= end):
        print(f"outside window {start}~{end}; silent")
        return 0
    if state.get("results_notified") and mode != "force":
        print("already sent results; skip")
        return 0

    source, text = "", ""
    fails = 0
    try:
        source, text = sec_doc(cfg)
    except Exception as exc:
        print(f"sec fail: {exc}")
        fails += 1
    if not text:
        for url in cfg["sources"]:
            try:
                raw = visible(fetch(url))
            except Exception as exc:
                print(f"fetch fail {url}: {exc}")
                fails += 1
                continue
            if RESULT_RE.search(raw):
                source, text = url, raw
                break

    send = False
    title = "[PATH] 대기"
    msg = ""
    verdict, rows, metrics = "해석불가", [], {}
    base_arr_m = float(cfg["baseline"]["arr_b"]) * 1000
    if text:
        metrics = parse_metrics(text, base_arr_m)
        verdict, rows, _ = judge(metrics, cfg["lines"])
        if verdict == "해석불가" and not state.get("fail_notified"):
            send = True
            state["fail_notified"] = True
            state["phase"] = "unreadable"
            title = "[PATH] 해석불가 | 원문 확인"
            msg = body(cfg, now, verdict, rows, metrics, source)
        elif verdict != "해석불가":
            send = True
            state["results_notified"] = True
            state["phase"] = "results"
            state["verdict"] = verdict
            title = f"[PATH] {verdict} | 3분기 판정"
            msg = body(cfg, now, verdict, rows, metrics, source)
    elif today >= end and year_ok and not state.get("miss_notified"):
        send = True
        state["miss_notified"] = True
        state["phase"] = "missing"
        title = "[PATH] 발표 없음 | 판정 보류"
        msg = "\n".join([
            f"{now.strftime('%Y-%m-%d %H:%M KST')} | PATH | FY27 3분기",
            "이게 뭐 알림: UiPath 3분기 실적 확인 창이 끝났습니다.",
            "지금 결과/상태: 데이터 없음. 충족, 미달, 해석불가로 처리하지 않습니다.",
            "지금 할 일: SEC 8-K와 IR 보도자료를 직접 확인하세요.",
            "나중에: 숫자가 보이면 스크립트를 force로 한 번만 다시 돌리세요.",
            "익절/줄일 때: 이번 알림은 매도 신호가 아닙니다.",
            "하지 말 것: 미발표를 재가속 기각으로 보기.",
        ])
    elif fails and not state.get("fail_notified") and today == start:
        send = True
        state["fail_notified"] = True
        state["phase"] = "fetch_fail"
        title = "[PATH] 조회 실패 | 직접 확인"
        msg = "\n".join([
            f"{now.strftime('%Y-%m-%d %H:%M KST')} | PATH | FY27 3분기",
            "이게 뭐 알림: 실적 창 첫날 조회가 실패했습니다.",
            "지금 결과/상태: 조회 실패. 조건 미달이 아닙니다.",
            "지금 할 일: 12-04 07:30 KST 재확인을 기다리세요.",
            "나중에: 12-05 09:10 KST까지 미확인이면 발표 없음 알림 1회.",
            "익절/줄일 때: 매도 신호 아님.",
            "하지 말 것: 실패를 실적 발표로 취급.",
        ])
    else:
        print("no release yet; silent")

    print(msg or "no send")
    print(f"topic={topic} send={send} verdict={verdict}")
    if send and mode != "dry":
        publish(topic, title, msg, 4, ["chart_with_upwards_trend"], server)
    state.update({
        "updated_at": now.isoformat(),
        "source": source or state.get("source"),
        "parsed": metrics or state.get("parsed"),
    })
    if mode != "dry":
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
