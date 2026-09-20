#!/usr/bin/env python3
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
CFG_PATH = ROOT / "configs" / "addstop.json"
STATE_PATH = ROOT / "state" / "addstop.json"
UA = {"User-Agent": "cwally-alerts addstop monitor", "Accept": "text/html,application/json"}


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers=UA)
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


def unit_bn(blob: str, pattern: str):
    m = re.search(pattern, blob, re.I)
    if not m:
        return None
    try:
        val = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    unit = (m.group(2) or "billion").lower()
    if unit.startswith("million"):
        return val / 1000.0
    return val


def classify(blob: str, spec: dict) -> str:
    if re.search(spec["result_re"], blob, re.I):
        return "results"
    if re.search(spec["date_re"], blob, re.I):
        return "date"
    return "none"


def _nvda_rev(blob: str):
    m = re.search(
        r"reported revenue for the third quarter[^.']{0,80}\$([0-9]+(?:\.[0-9]+)?)\s*billion",
        blob,
        re.I,
    )
    if m:
        return float(m.group(1))
    m = re.search(
        r"third quarter fiscal 2027[^.']{0,40}revenue of \$([0-9]+(?:\.[0-9]+)?)\s*billion",
        blob,
        re.I,
    )
    if m:
        return float(m.group(1))
    return unit_bn(blob, r"revenue of \$([0-9]+(?:\.[0-9]+)?)\s*(billion)")


def parse_nvda(blob: str) -> dict:
    return {
        "rev_bn": _nvda_rev(blob),
        "gm_pct": num(blob, r"gross margins? (?:were|are expected to be|of) ([0-9]+(?:\.[0-9]+)?)%"),
        "next_guide_bn": unit_bn(
            blob,
            r"(?:outlook for the fourth quarter|fourth quarter of fiscal 2027)[^.']{0,160}revenue is expected to be \$([0-9]+(?:\.[0-9]+)?)\s*(billion)",
        ),
    }


def parse_tsla(blob: str) -> dict:
    auto_rev = unit_bn(blob, r"automotive segment\s+revenues?\s+\$([0-9]+(?:\.[0-9]+)?)\s*(billion|million)")
    auto_gp = unit_bn(blob, r"automotive segment\s+gross profit\s+\$([0-9]+(?:\.[0-9]+)?)\s*(billion|million)")
    energy_rev = unit_bn(
        blob,
        r"energy generation and storage(?: segment)?(?: revenue)?[^.']{0,40}\$([0-9]+(?:\.[0-9]+)?)\s*(billion|million)",
    )
    energy_gp = unit_bn(
        blob,
        r"energy generation and storage(?: segment)?[^.']{0,80}gross profit\s+\$([0-9]+(?:\.[0-9]+)?)\s*(billion|million)",
    )
    auto_gm = num(blob, r"automotive(?: segment)? gross margin[^.']{0,20}?([0-9]+(?:\.[0-9]+)?)%")
    energy_gm = num(blob, r"energy(?: generation and storage)?(?: segment)? gross margin[^.']{0,20}?([0-9]+(?:\.[0-9]+)?)%")
    if auto_gm is None and auto_rev and auto_gp and auto_rev > 0:
        auto_gm = 100.0 * auto_gp / auto_rev
    if energy_gm is None and energy_rev and energy_gp and energy_rev > 0:
        energy_gm = 100.0 * energy_gp / energy_rev
    return {"auto_gm_pct": auto_gm, "energy_rev_bn": energy_rev, "energy_gm_pct": energy_gm}


def parse_pltr(blob: str) -> dict:
    return {
        "rev_bn": unit_bn(
            blob,
            r"revenue[^.']{0,80}(?:grew|was|of)?[^.']{0,40}?\$\s*([0-9]+(?:\.[0-9]+)?)\s*(billion|million)",
        ),
        "fy_guide_lo": num(blob, r"revenue guidance to between \$([0-9]+(?:\.[0-9]+)?)\s*(?:–|-|to)"),
        "us_comm_fy": unit_bn(
            blob,
            r"u\.?s\.?\s*commercial revenue(?: guidance)?[^.']{0,80}(?:in excess of|to more than|>)?\s*\$([0-9]+(?:\.[0-9]+)?)\s*(billion|million)",
        ),
        "adj_oi_bn": unit_bn(
            blob,
            r"adjusted income from operations[^.']{0,80}(?:of between|of)?\s*\$([0-9]+(?:\.[0-9]+)?)\s*(billion|million)",
        ),
        "guide_cut": bool(
            re.search(r"lowering (our )?(full[- ]year|revenue|u\.?s\.?\s*commercial) guidance", blob, re.I)
        ),
    }


def judge(ticker: str, spec: dict, metrics: dict) -> tuple[str, str, list[str]]:
    th = spec["triggers"]
    hit = []
    if ticker == "NVDA":
        rev = metrics.get("rev_bn")
        gm = metrics.get("gm_pct")
        nxt = metrics.get("next_guide_bn")
        if rev is not None and rev < th["rev_floor_bn"]:
            hit.append(f"T1 매출 {rev:.2f}B < {th['rev_floor_bn']}B")
        if gm is not None and gm < th["gm_floor_pct"]:
            hit.append(f"T2 매출총이익률 {gm:.1f}% < {th['gm_floor_pct']}%")
        if nxt is not None and rev is not None and nxt < rev:
            hit.append(f"T2 다음분기 가이던스 {nxt:.2f}B < 이번 매출 {rev:.2f}B")
    elif ticker == "TSLA":
        agm = metrics.get("auto_gm_pct")
        egm = metrics.get("energy_gm_pct")
        erev = metrics.get("energy_rev_bn")
        if agm is not None and agm < th["auto_gm_floor_pct"]:
            hit.append(f"T1 자동차 마진 {agm:.1f}% < {th['auto_gm_floor_pct']}%")
        energy_down = erev is not None and erev < th["q3_2025_energy_bn"]
        energy_thin = egm is not None and egm < th["energy_gm_floor_pct"]
        if energy_down and energy_thin:
            hit.append(
                f"T2 에너지 매출 {erev:.3f}B < 전년 {th['q3_2025_energy_bn']}B 그리고 마진 {egm:.1f}% < {th['energy_gm_floor_pct']}%"
            )
    elif ticker == "PLTR":
        rev = metrics.get("rev_bn")
        fy = metrics.get("fy_guide_lo")
        usc = metrics.get("us_comm_fy")
        oi = metrics.get("adj_oi_bn")
        if rev is not None and rev < th["q3_rev_floor_bn"]:
            hit.append(f"T1 매출 {rev:.3f}B < {th['q3_rev_floor_bn']}B")
        if fy is not None and fy < th["fy_rev_floor_bn"]:
            hit.append(f"T1 연간 가이던스 {fy:.3f}B < {th['fy_rev_floor_bn']}B")
        if metrics.get("guide_cut"):
            hit.append("T1/T2 가이던스 하향 문구")
        if usc is not None and usc < th["us_comm_fy_floor_bn"]:
            hit.append(f"T2 미국 상업 가이던스 {usc:.3f}B < {th['us_comm_fy_floor_bn']}B")
        if oi is not None and oi < th["adj_oi_floor_bn"]:
            hit.append(f"T2 조정 영업이익 {oi:.3f}B < {th['adj_oi_floor_bn']}B")
    if hit:
        return "중단", "; ".join(hit), hit
    useful = [v for v in metrics.values() if v not in (None, False)]
    if useful:
        return "유지", "다음 실적 숫자가 중단 임계를 넘지 않음", hit
    return "확인 불가", "실적으로 보이지만 매출·마진·가이던스 숫자를 파싱하지 못함", hit


def body(now: datetime, ticker: str, spec: dict, kind: str, source: str, verdict: str, why: str, metrics: dict) -> str:
    lines = [
        f"{now.strftime('%Y-%m-%d %H:%M KST')} | {ticker} | {spec['display']}",
        f"이게 뭐 알림: {ticker} 다음 실적 숫자로 추가 매수를 멈추지 보는 알림입니다. 매도 신호가 아닙니다.",
    ]
    if kind == "date":
        lines += [
            "지금 결과/상태: 발표일 공지는 있으나 실적 숫자는 아직 없습니다.",
            f"지금 할 일: 숫자 나오기 전 추가 매수 결정을 바꾸지 마세요. {spec.get('expected_note', '')}",
            "나중에: 실적 알림이 오면 트리거만 보세요.",
            "익절/줄일 때: 이번 알림으로 매도하지 마세요.",
            "하지 말 것: 미확정 일정을 확정으로 취급, 주가 등락만으로 중단.",
        ]
    elif kind == "results":
        lines += [
            f"지금 결과/상태: 실적 공시로 보입니다. 추가매수 {verdict}. {why}.",
            f"파싱: {json.dumps(metrics, ensure_ascii=False)}",
            "지금 할 일: '중단'이면 이 종목 분할매수를 멈추세요. '유지'면 기존 규칙을 유지하세요. '확인 불가'면 IR 원문을 직접 보세요.",
            "나중에: 같은 분기 실적 알림은 다시 안 보냅니다.",
            "익절/줄일 때: 트리거 2개가 동시에 나와야 비중 축소를 따로 검토하세요. 이 알림은 추가 매수 중단만입니다.",
            "하지 말 것: 주가 반응, 기존 [PLTR] 김승호 알림과 혼동, 파싱 실패를 충족으로 취급.",
        ]
    elif kind == "miss":
        lines += [
            "지금 결과/상태: 점검 기간이 끝났는데 실적 문서를 확인하지 못했습니다.",
            "지금 할 일: IR·SEC를 직접 보고 '미확인'으로 두세요. 조건을 유지로 바꾸지 마세요.",
            "나중에: workflow_dispatch job=addstop 로 다시 돌릴 수 있습니다.",
            "익절/줄일 때: 매도 신호 아님.",
            "하지 말 것: 실패를 정상 발표로 취급.",
        ]
    else:
        lines += [
            "지금 결과/상태: 조회 실패가 반복됐습니다.",
            "지금 할 일: 출처를 직접 확인하세요.",
            "나중에: 다음 평일 장전·장후 슬롯에서 재시도합니다.",
            "익절/줄일 때: 매도 신호 아님.",
            "하지 말 것: 실패를 실적 발표로 취급.",
        ]
    if source:
        lines += ["", f"출처: {source}"]
    return "\n".join(lines)


def empty_ticker_state() -> dict:
    return {
        "phase": "idle",
        "date_notified": False,
        "results_notified": False,
        "miss_notified": False,
        "source": None,
        "parsed": None,
        "fetch_fails": 0,
        "fetch_fails_sent": None,
    }


def parse_for(ticker: str, blob: str) -> dict:
    if ticker == "NVDA":
        return parse_nvda(blob)
    if ticker == "TSLA":
        return parse_tsla(blob)
    return parse_pltr(blob)


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    today = now.date()
    topic = resolve_topic("port", cfg.get("ntfy_topic"))
    server = resolve_server(cfg.get("ntfy_server"))
    state = {"updated_at": None, "tickers": {}}
    if STATE_PATH.exists():
        try:
            state.update(json.loads(STATE_PATH.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass
    if "tickers" not in state or not isinstance(state["tickers"], dict):
        state["tickers"] = {}

    any_send = False
    for ticker, spec in cfg["tickers"].items():
        ts = empty_ticker_state()
        ts.update(state["tickers"].get(ticker) or {})
        start = date.fromisoformat(spec["window_start"])
        end = date.fromisoformat(spec["window_end"])
        year_ok = today.year == int(cfg["oneshot_year"])
        in_window = year_ok and start <= today <= end
        if mode not in {"force", "event"} and not in_window:
            print(f"{ticker} outside window {start}~{end}; silent")
            state["tickers"][ticker] = ts
            continue
        if ts.get("results_notified") and mode != "force":
            print(f"{ticker} already sent results; skip")
            state["tickers"][ticker] = ts
            continue

        kind, source, metrics = "none", "", {}
        fails = 0
        for url in spec["sources"]:
            try:
                raw = fetch(url)
            except Exception as exc:
                print(f"{ticker} fetch fail {url}: {exc}")
                fails += 1
                continue
            blob = visible(raw)
            if url.endswith(".json"):
                blob = raw[:8000]
            guess = classify(blob, spec)
            print(f"{ticker} {url} -> {guess}")
            if guess == "results":
                kind, source = guess, url
                metrics = parse_for(ticker, visible(raw) + " " + blob[:2000])
                break
            if guess == "date" and kind != "results":
                kind, source = guess, url
        if fails == len(spec["sources"]):
            kind = "fail"
        ts["fetch_fails"] = fails

        send = False
        title = f"[ADDSTOP] {ticker} 점검 대기"
        verdict, why = "", ""
        if kind == "results" and (not ts.get("results_notified") or mode == "force"):
            verdict, why, _hit = judge(ticker, spec, metrics)
            send = True
            ts["results_notified"] = True
            ts["date_notified"] = True
            ts["phase"] = "results"
            if verdict == "중단":
                title = f"[ADDSTOP] {ticker} 추가매수 중단"
            elif verdict == "유지":
                title = f"[ADDSTOP] {ticker} 추가매수 유지"
            else:
                title = f"[ADDSTOP] {ticker} 실적 확인 불가"
        elif kind == "date" and not ts.get("date_notified") and not ts.get("results_notified"):
            send = True
            ts["date_notified"] = True
            ts["phase"] = "date"
            title = f"[ADDSTOP] {ticker} 발표일 공지 | 숫자는 아직"
        elif (
            kind in {"none", "fail"}
            and today >= end
            and year_ok
            and not ts.get("results_notified")
            and not ts.get("miss_notified")
        ):
            send = True
            kind = "miss"
            ts["miss_notified"] = True
            ts["phase"] = "miss"
            title = f"[ADDSTOP] {ticker} 기간 종료 | 실적 미확인"
        elif kind == "fail" and fails == len(spec["sources"]) and ts.get("fetch_fails_sent") != today.isoformat():
            expected = {date.fromisoformat(x) for x in spec.get("expected_dates_kst", [])}
            if today in expected:
                send = True
                ts["fetch_fails_sent"] = today.isoformat()
                title = f"[ADDSTOP] {ticker} 조회 실패 | 직접 확인"

        msg = body(now, ticker, spec, kind, source, verdict, why, metrics)
        print(msg)
        print(f"{ticker} topic={topic} send={send}")
        if send:
            any_send = True
            pri = 5 if kind in {"results", "miss", "fail"} else 3
            publish(topic, title, msg, pri, ["chart_with_upwards_trend"], server)
        ts.update({"source": source or ts.get("source"), "parsed": metrics or ts.get("parsed")})
        state["tickers"][ticker] = ts

    state["updated_at"] = now.isoformat()
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"done send_any={any_send}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
