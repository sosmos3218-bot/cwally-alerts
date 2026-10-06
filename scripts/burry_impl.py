#!/usr/bin/env python3
from __future__ import annotations
import json, re, sys, urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic
KST = timezone(timedelta(hours=9))
CFG_PATH = ROOT / "configs" / "burry.json"
STATE_PATH = ROOT / "state" / "burry.json"
UA = {"User-Agent": "Mozilla/5.0 cwally-alerts"}

def fetch_text(url, timeout=45):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "ignore")

def load_json(path, default):
    if not path.exists():
        return dict(default)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return dict(default)
    base = dict(default)
    base.update(data)
    return base

def save_state(state):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

def parse_fred(text):
    rows = []
    for line in text.splitlines()[1:]:
        parts = line.split(",")
        if len(parts) < 2 or parts[1].strip() in {"", "."}:
            continue
        try:
            rows.append((parts[0].strip(), float(parts[1])))
        except ValueError:
            continue
    return rows

def parse_fred_txt(text):
    rows = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 2 or not parts[0][:4].isdigit() or parts[1] == ".":
            continue
        try:
            rows.append((parts[0], float(parts[1])))
        except ValueError:
            continue
    return rows

def fetch_fred(csv_url, txt_url, timeout):
    errors = []
    for label, url, parser in (("csv", csv_url, parse_fred), ("txt", txt_url, parse_fred_txt)):
        try:
            rows = parser(fetch_text(url, timeout))
            if rows:
                return rows, label
            errors.append(label + " empty")
        except Exception as exc:
            errors.append(f"{label} {exc}")
    raise RuntimeError("; ".join(errors))

def multpl_pe(url, timeout):
    text = fetch_text(url, timeout)
    rows = re.findall(r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{1,2}),\s+(\d{4})[\s\S]{0,80}?([0-9]+\.[0-9]+)", text)
    if not rows:
        raise RuntimeError("multpl PER table missing")
    month = {"Jan":1,"Feb":2,"Mar":3,"Apr":4,"May":5,"Jun":6,"Jul":7,"Aug":8,"Sep":9,"Oct":10,"Nov":11,"Dec":12}
    parsed = sorted((f"{int(y):04d}-{month[m]:02d}-{int(d):02d}", float(v)) for m,d,y,v in rows)
    return {"trailing_pe": parsed[-1][1], "pe_date": parsed[-1][0], "eps_revision": "확인불가", "src": "multpl_trailing", "pe_drop": None}

def decide(hy, ig, fed, earn, rules):
    reasons, missing, exit_hit, delay_hit = [], [], False, False
    if hy.get("latest") is None:
        missing.append("하이일드 OAS 조회 실패")
    else:
        if hy["latest"] >= float(rules["hy_level_pct"]):
            exit_hit = True
            reasons.append(f"하이일드 OAS {hy['latest']:.2f}%")
        if hy.get("chg_bp") is None:
            missing.append("하이일드 4주 확인불가")
        elif hy["chg_bp"] >= float(rules["hy_4w_widen_bp"]):
            exit_hit = True
            reasons.append(f"하이일드 4주 {hy['chg_bp']:+.0f}bp")
    if ig.get("latest") is None:
        missing.append("투자등급 OAS 조회 실패")
    elif ig.get("chg_bp") is None:
        missing.append("투자등급 4주 확인불가")
    elif ig["chg_bp"] >= float(rules["ig_4w_widen_bp"]):
        exit_hit = True
        reasons.append(f"투자등급 4주 {ig['chg_bp']:+.0f}bp")
    rev = earn.get("eps_revision") or "확인불가"
    if rev == "확인불가":
        missing.append("실적 추정 확인불가")
    if earn.get("pe_drop") is None:
        missing.append("PER 4주 확인불가")
    cut = fed.get("cut_pct")
    if fed.get("latest") is None:
        missing.append("기준금리 조회 실패")
    if cut is not None and cut >= float(rules["fed_cut_pct"]) and rev == "상향":
        delay_hit = True
        reasons.append(f"금리 {cut:.2f}%p 인하와 실적 상향")
    signal = "exit_review" if exit_hit else "delay" if delay_hit else "none"
    return {"signal": signal, "reasons": reasons, "missing": missing, "core_fail": hy.get("latest") is None and ig.get("latest") is None}

def series_view(rows, lookback, asof):
    if not rows:
        return {"latest": None, "date": None, "chg_bp": None}
    last = rows[-1]
    past = (asof.date() - timedelta(days=lookback)).isoformat()
    prev = None
    for d, v in rows:
        if d <= past:
            prev = (d, v)
    chg = None if prev is None else round((last[1] - prev[1]) * 100, 1)
    return {"latest": last[1], "date": last[0], "chg_bp": chg}

def fed_view(rows):
    if not rows:
        return {"latest": None, "date": None, "cut_pct": None}
    last_d, last_v = rows[-1]
    prev = next(((d, v) for d, v in reversed(rows[:-1]) if abs(v - last_v) >= 0.01), None)
    cut = round(prev[1] - last_v, 2) if prev and prev[1] > last_v else (0.0 if prev else None)
    return {"latest": last_v, "date": last_d, "cut_pct": cut}

def main():
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    dry = mode in {"test", "dry", "dry-run"}
    cfg = load_json(CFG_PATH, {})
    rules = cfg.get("rules") or {}
    now = datetime.now(KST)
    today = now.strftime("%Y-%m-%d")
    state = load_json(STATE_PATH, {})
    timeout = int(cfg.get("timeout_sec") or 45)
    sources = cfg.get("sources") or {}
    errors = []
    hy_rows = ig_rows = fed_rows = None
    for key, label in (("hy", "하이일드"), ("ig", "투자등급"), ("fed", "기준금리")):
        pass
    try:
        hy_rows, src = fetch_fred(sources["hy_oas_csv"], sources["hy_oas_txt"], timeout)
        errors.append(f"하이일드 원천 {src}")
    except Exception as exc:
        errors.append(f"하이일드 OAS 조회 실패: {exc}")
    try:
        ig_rows, src = fetch_fred(sources["ig_oas_csv"], sources["ig_oas_txt"], timeout)
        errors.append(f"투자등급 원천 {src}")
    except Exception as exc:
        errors.append(f"투자등급 OAS 조회 실패: {exc}")
    try:
        fed_rows, src = fetch_fred(sources["fed_upper_csv"], sources["fed_upper_txt"], timeout)
        errors.append(f"기준금리 원천 {src}")
    except Exception as exc:
        errors.append(f"기준금리 조회 실패: {exc}")
    earn = {"trailing_pe": None, "eps_revision": "확인불가", "pe_drop": None, "src": "확인불가"}
    try:
        earn = multpl_pe(sources["spx_pe"], timeout)
    except Exception as exc:
        errors.append(f"후행 PER 조회 실패: {exc}")
    hy = series_view(hy_rows, int(rules.get("lookback_days", 28)), now)
    ig = series_view(ig_rows, int(rules.get("lookback_days", 28)), now)
    fed = fed_view(fed_rows)
    dec = decide(hy, ig, fed, earn, rules)
    kind = "FETCH_FAIL" if dec["core_fail"] else dec["signal"]
    print(f"{now.strftime('%Y-%m-%d %H:%M KST')} | BURRY | 바뀌는 조건")
    print(f"HY {hy} | IG {ig} | FED {fed} | PE {earn}")
    print("조회 메모: " + " / ".join(errors))
    print(f"kind={kind} signal={dec['signal']} dry={dry} missing={dec['missing']}")
    if dry:
        print("dry-run; no ntfy")
        return 0
    print("live path not used in this restore")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
