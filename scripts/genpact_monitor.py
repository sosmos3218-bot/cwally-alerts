#!/usr/bin/env python3
"""Genpact Q3 2026 oneshot. Existing 09:00/15:20 KST slots only. Silent outside 2026-11-06~11-09."""
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
CFG_PATH = ROOT / "configs" / "genpact.json"
STATE_PATH = ROOT / "state" / "genpact.json"
UA = {"User-Agent": "cwally-alerts genpact monitor contact@example.com"}
SEC_UA = {"User-Agent": "cwally-alerts research monitor contact@example.com", "Accept": "application/json"}

RESULT_RE = re.compile(
    r"third quarter 2026|q3 2026|quarter ended september 30, 2026",
    re.I,
)
Q2_ONLY = re.compile(r"second quarter 2026 results", re.I)


def fetch(url: str, headers: dict | None = None) -> str:
    req = urllib.request.Request(url, headers=headers or UA)
    with urllib.request.urlopen(req, timeout=25) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def visible(text: str) -> str:
    text = re.sub(r"<script[\s\S]*?</script>", " ", text, flags=re.I)
    text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text)


def num(blob: str, pattern: str):
    m = re.search(pattern, blob, re.I)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def signed_pct(blob: str, label: str):
    m = re.search(
        label + r".{0,120}?(up|down|increased|decreased|flat|unchanged)\s*(?:by\s*)?([0-9]+(?:\.[0-9]+)?)?\s*%?",
        blob,
        re.I,
    )
    if not m:
        m = re.search(label + r".{0,80}?([+-]?[0-9]+(?:\.[0-9]+)?)\s*%", blob, re.I)
        if not m:
            return None
        return float(m.group(1))
    word = m.group(1).lower()
    raw = m.group(2)
    if word in {"flat", "unchanged"}:
        return 0.0
    if raw is None:
        return None
    val = float(raw)
    if word in {"down", "decreased"}:
        return -val
    return val


def parse_metrics(blob: str) -> dict:
    ats = signed_pct(blob, r"advanced technology solutions")
    core = signed_pct(blob, r"core business services")
    eps = num(blob, r"adjusted diluted (?:earnings per share|eps)[^$]{0,40}\$([0-9]+\.[0-9]+)")
    if eps is None:
        eps = num(blob, r"adjusted diluted eps[^$]{0,20}\$([0-9]+\.[0-9]+)")
    fy_eps = num(blob, r"adjusted diluted eps[^.]{0,80}(?:at least|to|of)\s*\$([0-9]+\.[0-9]+)")
    raise_g = bool(re.search(r"rais(?:e|ing).{0,40}(guidance|outlook)", blob, re.I))
    cut_g = bool(re.search(r"(lower(?:ing)?|cut(?:ting)?|reduc(?:e|ing)).{0,40}(guidance|outlook)", blob, re.I))
    backlog = "확인불가"
    if re.search(r"(booking|backlog|pipeline).{0,40}(declin|down|lower|weak)", blob, re.I):
        backlog = "감소"
    elif re.search(r"record booking", blob, re.I) and re.search(r"\$[0-9]", blob):
        backlog = "기록 경신, 수치 있음"
    elif re.search(r"(record booking|backlog (increased|growth|grew)|increasing backlog)", blob, re.I):
        backlog = "증가 표현, 수치 없음"
    return {
        "ats_yoy": ats,
        "core_yoy": core,
        "adj_eps": eps,
        "fy_eps": fy_eps,
        "guide_raise": raise_g,
        "guide_cut": cut_g,
        "backlog": backlog,
    }


def item(name: str, status: str) -> str:
    return f"{name}:{status}"


def judge(cfg: dict, m: dict) -> tuple[str, list[str]]:
    c = cfg["checklist"]
    rows = []
    ats = m.get("ats_yoy")
    if ats is None:
        rows.append(item("ATS", "확인불가"))
        ats_s = "miss"
    elif ats >= c["ats_pass"]:
        rows.append(item("ATS", "통과"))
        ats_s = "pass"
    elif ats < c["ats_fail"]:
        rows.append(item("ATS", "실패"))
        ats_s = "fail"
    else:
        rows.append(item("ATS", "보류"))
        ats_s = "hold"
    core = m.get("core_yoy")
    if core is None:
        rows.append(item("코어", "확인불가"))
        core_s = "miss"
    elif core >= c["core_pass"]:
        rows.append(item("코어", "통과"))
        core_s = "pass"
    elif core <= c["core_fail"]:
        rows.append(item("코어", "실패"))
        core_s = "fail"
    else:
        rows.append(item("코어", "보류"))
        core_s = "hold"
    eps = m.get("adj_eps")
    if eps is None:
        rows.append(item("EPS", "확인불가"))
        eps_s = "miss"
    elif eps >= c["eps_pass"]:
        rows.append(item("EPS", "통과"))
        eps_s = "pass"
    elif eps < c["eps_fail"]:
        rows.append(item("EPS", "실패"))
        eps_s = "fail"
    else:
        rows.append(item("EPS", "보류"))
        eps_s = "hold"
    if m.get("guide_cut"):
        rows.append(item("가이던스", "실패"))
        g_s = "fail"
    elif m.get("guide_raise") or (m.get("fy_eps") is not None and m["fy_eps"] > c["fy_eps_guide"]):
        rows.append(item("가이던스", "통과"))
        g_s = "pass"
    elif m.get("fy_eps") is not None:
        rows.append(item("가이던스", "보류"))
        g_s = "hold"
    else:
        rows.append(item("가이던스", "확인불가"))
        g_s = "miss"
    bk = m.get("backlog") or "확인불가"
    if bk.startswith("기록"):
        rows.append(item("백로그", "통과"))
    elif bk == "감소":
        rows.append(item("백로그", "실패"))
    elif bk.startswith("증가"):
        rows.append(item("백로그", "보류"))
    else:
        rows.append(item("백로그", "확인불가"))
    if ats_s == "fail" or g_s == "fail":
        return "실패", rows
    if ats_s == "pass" and g_s == "pass" and core_s != "fail" and eps_s != "fail":
        return "통과", rows
    if ats_s == "miss" and eps_s == "miss":
        return "확인불가", rows
    return "보류", rows


def price() -> dict:
    url = "https://query1.finance.yahoo.com/v8/finance/chart/G?interval=1d&range=5d"
    raw = json.loads(fetch(url, {"User-Agent": "Mozilla/5.0"}))
    meta = raw["chart"]["result"][0]["meta"]
    return {
        "regular": meta.get("regularMarketPrice"),
        "post": meta.get("postMarketPrice"),
        "prev": meta.get("previousClose") or meta.get("chartPreviousClose"),
    }


def weighted(cfg: dict, verdict: str, px: float | None) -> tuple[str, float]:
    key = {"통과": "pass", "보류": "hold", "실패": "fail"}.get(verdict, "hold")
    w = dict(cfg["weights"][key])
    note = "장외 미확인, 사전 가중 유지"
    if px is not None:
        if px >= 38:
            w["42"] = min(0.85, w["42"] + 0.15)
            w["30"] = max(0.02, w["30"] - 0.08)
            note = f"장외 {px:.2f}, 42달러 가중 +15%p"
        elif px <= 31:
            w["30"] = min(0.80, w["30"] + 0.15)
            w["42"] = max(0.02, w["42"] - 0.10)
            note = f"장외 {px:.2f}, 30달러 가중 +15%p"
        else:
            note = f"장외 {px:.2f}, 33달러 부근이라 가중 유지"
    s = w["30"] + w["33"] + w["42"]
    exp = (w["30"] * 30 + w["33"] * 33 + w["42"] * 42) / s
    text = f"$30 {w['30']/s*100:.0f}% / $33 {w['33']/s*100:.0f}% / $42 {w['42']/s*100:.0f}% -> {exp:.1f}"
    return f"{note}. {text}", round(exp, 1)


def sec_doc(cfg: dict) -> tuple[str, str]:
    raw = fetch(f"https://data.sec.gov/submissions/CIK{cfg['cik']}.json", SEC_UA)
    data = json.loads(raw)
    recent = data.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    dates = recent.get("filingDate", [])
    acc = recent.get("accessionNumber", [])
    docs = recent.get("primaryDocument", [])
    for i, form in enumerate(forms):
        if form not in {"8-K", "8-K/A"}:
            continue
        if dates[i] < "2026-11-05":
            continue
        accession = acc[i].replace("-", "")
        url = f"https://www.sec.gov/Archives/edgar/data/{int(cfg['cik'])}/{accession}/{docs[i]}"
        text = visible(fetch(url, SEC_UA))
        if RESULT_RE.search(text) and not Q2_ONLY.search(text[:400]):
            return url, text
    return "", ""


def body(cfg: dict, now: datetime, verdict: str, rows: list[str], m: dict, px: dict, wtext: str, source: str) -> str:
    ats = m.get("ats_yoy")
    core = m.get("core_yoy")
    eps = m.get("adj_eps")
    action = {
        "통과": "추격하지 말고 34달러 재진입만 보세요.",
        "보류": "32달러 아래에서만 추가하세요.",
        "실패": "ATS 실패 또는 가이던스 하향이면 비중을 줄이세요.",
        "확인불가": "원문을 직접 보고 판정을 비워 두세요.",
    }[verdict]
    lines = [
        f"{now.strftime('%Y-%m-%d %H:%M KST')} | GENPACT G | Q3 2026",
        "이게 뭐 알림: 젠팩트 11-05 실적을 체크리스트로 통과, 보류, 실패 판정하는 일회성 알림입니다.",
        f"지금 결과/상태: {verdict}. {' / '.join(rows)}.",
        f"ATS {ats if ats is not None else '파싱실패'}% | 코어 {core if core is not None else '파싱실패'}% | 조정 EPS {eps if eps is not None else '파싱실패'} | 가이던스 {'하향' if m.get('guide_cut') else ('상향' if m.get('guide_raise') else '유지/불명')} | 백로그 {m.get('backlog')}",
        f"장외/정규 {px.get('post') or '없음'} / {px.get('regular') or '없음'} (10-02 기준 {cfg['baseline_price']}). {wtext}",
        f"지금 할 일: {action}",
        "나중에: 11-09 이후에는 이 알림이 다시 오지 않습니다. 파싱 실패 칸은 IR 원문으로 채우세요.",
        "익절/줄일 때: 통과여도 42달러는 12개월 목표입니다. 장중 42달러 터치만으로 익절 완료라고 보지 마세요.",
        "하지 말 것: 조회 실패를 실패 판정으로 취급, 신용 추격, 파싱 실패 숫자를 확정값으로 사용.",
        f"출처: {source}",
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
        "price": None,
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
        for url in cfg["sources"][1:]:
            try:
                raw = visible(fetch(url))
            except Exception as exc:
                print(f"fetch fail {url}: {exc}")
                fails += 1
                continue
            if RESULT_RE.search(raw):
                source, text = url, raw
                break
    px = {}
    try:
        px = price()
    except Exception as exc:
        print(f"price fail: {exc}")
        px = {"regular": None, "post": None, "prev": None}

    send = False
    title = "[GENPACT] 대기"
    msg = ""
    verdict, rows, metrics = "확인불가", [], {}
    if text:
        metrics = parse_metrics(text)
        verdict, rows = judge(cfg, metrics)
        if verdict == "확인불가" and not state.get("fail_notified"):
            send = True
            state["fail_notified"] = True
            state["phase"] = "parse_fail"
            title = "[GENPACT] 파싱 실패 | 원문 확인"
            wtext, _ = weighted(cfg, "보류", px.get("post") or px.get("regular"))
            msg = body(cfg, now, verdict, rows, metrics, px, wtext, source)
        elif verdict != "확인불가":
            send = True
            state["results_notified"] = True
            state["phase"] = "results"
            state["verdict"] = verdict
            wtext, exp = weighted(cfg, verdict, px.get("post") or px.get("regular"))
            title = f"[GENPACT] {verdict} | 가중 {exp:.0f}달러"
            msg = body(cfg, now, verdict, rows, metrics, px, wtext, source)
    elif today >= end and year_ok and not state.get("miss_notified"):
        send = True
        state["miss_notified"] = True
        state["phase"] = "miss"
        title = "[GENPACT] 기간 종료 | 실적 미확인"
        msg = "\n".join([
            f"{now.strftime('%Y-%m-%d %H:%M KST')} | GENPACT G",
            "이게 뭐 알림: 젠팩트 3분기 실적 확인 창이 끝났습니다.",
            "지금 결과/상태: 조회 실패 또는 일정 지연. 통과/보류/실패로 처리하지 않습니다.",
            "지금 할 일: IR과 SEC 8-K를 직접 확인하세요.",
            "나중에: 숫자가 보이면 workflow에서 스크립트를 force로 한 번만 다시 돌리세요.",
            "익절/줄일 때: 이번 알림은 매도 신호가 아닙니다.",
            "하지 말 것: 미확인을 실패 판정으로 취급.",
        ])
    elif fails and not state.get("fail_notified") and today == start:
        send = True
        state["fail_notified"] = True
        state["phase"] = "fetch_fail"
        title = "[GENPACT] 조회 실패 | 직접 확인"
        msg = "\n".join([
            f"{now.strftime('%Y-%m-%d %H:%M KST')} | GENPACT G",
            "이게 뭐 알림: 실적 창 첫날 조회가 실패했습니다.",
            "지금 결과/상태: 데이터 없음. 조건 미충족이 아닙니다.",
            "지금 할 일: 장후 발표가 늦으면 15:20 슬롯을 기다리세요.",
            "나중에: 11-09까지 재시도합니다.",
            "익절/줄일 때: 매도 신호 아님.",
            "하지 말 것: 실패를 실적 발표로 취급.",
        ])

    print(msg or "no send")
    print(f"topic={topic} send={send} verdict={verdict}")
    if send and mode != "dry":
        publish(topic, title, msg, 4, ["chart_with_upwards_trend"], server)
    state.update({
        "updated_at": now.isoformat(),
        "source": source or state.get("source"),
        "parsed": metrics or state.get("parsed"),
        "price": px,
    })
    if mode != "dry":
        STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
