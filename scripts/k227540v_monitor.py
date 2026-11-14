#!/usr/bin/env python3
"""[227540] ETF vs 개별주 주간 판정 + 한국 헬스케어 섹터 판정 (주 1번, 2026-11-05 요청 pt56u/t56u).

언제: 기존 KR-HC step 에 형제 실행(k203780 19:00 슬롯, morning 09:00 늦은 발송). 주 계산은 krhc 와 같음.
무엇:
  1) 227540 상위 구성종목(WiseReport 비율, 실패 시 config)마다 평균 목표가 업사이드, 추정 EPS 약 4주 수정률,
     추정 PER, 13주 수익률(227540 대비), 200일선 위아래.
  2) 개별주 우위 = 업사이드가 ETF 가중 업사이드보다 10%p 이상 높고, 수정률 0% 이상(확인된 경우), 종가 200일선 이상.
     없으면 ETF 유지. 여럿이면 업사이드 차이가 가장 큰 종목이 1순위.
  3) 섹터 점수(13주 상대강도 vs 069500, 가중 수정률 방향, 227540 200일선): 2 이상 매력, -2 이하 비매력, 그 외 중립.
판정이 같아도 매주 보냄, 바뀌면 제목에 판정 변경. 데이터 전부 실패면 cwally-ops 조회 실패 하루 1번.
modes: auto | dry(발송 없음, state 안 씀, --as-of "YYYY-MM-DD HH:MM") | selftest
"""
from __future__ import annotations

import json
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic, route_topic  # noqa: E402
from kr_calendar import KST, session_day  # noqa: E402
from krhc_monitor import target_week  # noqa: E402

NAME = "k227540v"
CFG_PATH = ROOT / "configs" / f"{NAME}.json"
STATE_PATH = ROOT / "state" / f"{NAME}.json"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
NA = "확인 불가"
BAD = ("분할매수", "매수 기회", "사세요", "지금 매수", "비중 확대", "물타기", "전액", "수량")


def load_state() -> dict:
    base = {"updated_at": None, "last_sent_week": None, "verdict": None, "eps_snap": {}, "fail_notice": {}, "sent": {}}
    if STATE_PATH.exists():
        try:
            base.update(json.loads(STATE_PATH.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass
    return base


# ---------------- 데이터 ----------------
def http(url: str) -> str:
    import urllib.request
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read().decode("utf-8", "replace")


def num(v) -> float | None:
    if v is None:
        return None
    m = re.search(r"-?[\d,]+(?:\.\d+)?", str(v))
    return float(m.group(0).replace(",", "")) if m else None


def closes_upto(code: str, last: date) -> list[tuple[str, float]]:
    import krx_close
    bars = krx_close.regular_bars(code, 230)
    return [(b.day, b.close) for b in bars if b.day <= last.isoformat()]


def consensus(code: str) -> dict:
    d = json.loads(http(f"https://m.stock.naver.com/api/stock/{code}/integration"))
    t = {i.get("code"): i.get("value") for i in d.get("totalInfos", [])}
    c = d.get("consensusInfo") or {}
    return {"target": num(c.get("priceTargetMean")), "eps": num(t.get("cnsEps")), "per": num(t.get("cnsPer")),
            "cons_date": c.get("createDate")}


def weights(cfg: dict) -> tuple[list[dict], str]:
    fb = cfg["holdings_fallback"]
    try:
        s = http(f"https://navercomp.wisereport.co.kr/v2/ETF/index.aspx?cmp_cd={cfg['etf']['code']}")
        rows = re.findall(r'"TRD_DT":"([\d-]+)"[^{}]*?"STK_NM_KOR":"([^"]+)","ETF_WEIGHT":([\d.]+)', s)
        by = {n: (float(w), d) for d, n, w in rows}
        out = [dict(h, weight=by[h["name"]][0]) for h in fb if h["name"] in by]
        if len(out) < len(fb) // 2 + 1:
            raise RuntimeError("구성종목 표 이름 불일치")
        return out, f"WiseReport {rows[0][0]}"
    except Exception as exc:  # noqa: BLE001
        print(f"weights fallback: {exc}", file=sys.stderr)
        return fb, f"config 값({cfg.get('holdings_as_of')})"


def etf_info(code: str) -> dict:
    d = json.loads(http(f"https://m.stock.naver.com/api/stock/{code}/etfAnalysis"))
    inflow = (d.get("cumulativeNetInflowList") or {})
    if isinstance(inflow, list):
        inflow = inflow[0] if inflow else {}
    return {"aum_eok": num(d.get("totalNav")), "flow3m": inflow.get("cumulativeNetInflow3m")}


def gather(cfg: dict, last: date) -> tuple[dict, list[str]]:
    errs: list[str] = []
    hold, wsrc = weights(cfg)
    data = {"holdings": hold, "wsrc": wsrc, "stocks": {}, "etf": None, "bench": None, "info": None}
    for key, code in (("etf", cfg["etf"]["code"]), ("bench", cfg["benchmark"]["code"])):
        try:
            data[key] = closes_upto(code, last)
        except Exception as exc:  # noqa: BLE001
            errs.append(f"{code} 시세: {exc}")
    try:
        data["info"] = etf_info(cfg["etf"]["code"])
    except Exception as exc:  # noqa: BLE001
        errs.append(f"ETF 정보: {exc}")
    for h in hold:
        s: dict = {}
        try:
            s["closes"] = closes_upto(h["code"], last)
        except Exception as exc:  # noqa: BLE001
            errs.append(f"{h['code']} 시세: {exc}")
        try:
            s.update(consensus(h["code"]))
        except Exception as exc:  # noqa: BLE001
            errs.append(f"{h['code']} 컨센서스: {exc}")
        data["stocks"][h["code"]] = s
    return data, errs


# ---------------- 계산 ----------------
def ret_n(closes: list[tuple[str, float]] | None, n: int) -> float | None:
    if not closes or len(closes) <= n:
        return None
    return (closes[-1][1] / closes[-1 - n][1] - 1) * 100


def ma_gap(closes: list[tuple[str, float]] | None, n: int) -> float | None:
    if not closes or len(closes) < n:
        return None
    ma = sum(c for _, c in closes[-n:]) / n
    return (closes[-1][1] / ma - 1) * 100


def revision(snaps: dict, code: str, eps: float | None, last: date, r: dict) -> tuple[float | None, int | None]:
    """약 4주 전 저장 EPS 대비 수정률. 최소 revision_min_days 지난 저장값만. (수정률, 경과일)."""
    if eps is None:
        return None, None
    best = None
    for d, row in snaps.items():
        age = (last - date.fromisoformat(d)).days
        if age < r["revision_min_days"] or row.get(code) in (None, 0):
            continue
        if best is None or abs(age - r["revision_target_days"]) < abs(best[0] - r["revision_target_days"]):
            best = (age, row[code])
    if best is None:
        return None, None
    return (eps / best[1] - 1) * 100 if best[1] > 0 else None, best[0]


def evaluate(cfg: dict, data: dict, snaps: dict, last: date) -> dict:
    r = cfg["rules"]
    weeks = r["rel_weeks"] * 5
    etf = data.get("etf")
    etf_ret = ret_n(etf, weeks)
    rows = []
    for h in data["holdings"]:
        s = data["stocks"].get(h["code"], {})
        cl = s.get("closes")
        px = cl[-1][1] if cl else None
        up = (s["target"] / px - 1) * 100 if px and s.get("target") else None
        rev, age = revision(snaps, h["code"], s.get("eps"), last, r)
        sr = ret_n(cl, weeks)
        rows.append({"code": h["code"], "name": h["name"], "w": h["weight"], "px": px, "up": up, "rev": rev, "rev_age": age,
                     "per": s.get("per"), "eps": s.get("eps"), "rel": (sr - etf_ret) if sr is not None and etf_ret is not None else None,
                     "ma": ma_gap(cl, r["ma_days"])})

    def wavg(key):
        xs = [(x["w"], x[key]) for x in rows if x[key] is not None]
        tw = sum(w for w, _ in xs)
        return (sum(w * v for w, v in xs) / tw, tw) if xs and tw > 0 else (None, 0.0)
    etf_up, up_cov = wavg("up")
    etf_rev, rev_cov = wavg("rev")
    for x in rows:
        why = []
        if x["up"] is None or etf_up is None:
            x["ok"] = False
            why.append("업사이드 " + NA)
        else:
            x["gap"] = x["up"] - etf_up
            if x["gap"] < r["stock_upside_gap_pp"]:
                why.append(f"업사이드 차이 {x['gap']:+.1f}%p")
        if x["rev"] is not None and x["rev"] < r["stock_revision_min_pct"]:
            why.append(f"EPS 수정 {x['rev']:+.1f}%")
        if r.get("stock_need_above_ma", True) and (x["ma"] is None or x["ma"] < 0):
            why.append("200일선 아래" if x["ma"] is not None else "200일선 " + NA)
        x["ok"] = not why
        x["why"] = why
    winners = sorted([x for x in rows if x["ok"]], key=lambda x: -x["gap"])
    stock_verdict = "ETF 유지" if not winners else f"개별주 우위 {winners[0]['name']}"
    comps = []
    bench_ret = ret_n(data.get("bench"), weeks)
    if etf_ret is None or bench_ret is None:
        comps.append(("13주 상대강도(227540 vs 코스피200)", 0, NA))
    else:
        d = etf_ret - bench_ret
        comps.append(("13주 상대강도(227540 vs 코스피200)", 1 if d > 0 else -1, f"{etf_ret:+.1f}% vs {bench_ret:+.1f}%, {d:+.1f}%p"))
    if etf_rev is None:
        comps.append(("가중 EPS 수정 방향", 0, "미확인(주간 저장값이 쌓이는 중)"))
    else:
        db = float(r.get("sector_revision_deadband_pct", 0.0))
        sc = 1 if etf_rev > db else -1 if etf_rev < -db else 0
        comps.append(("가중 EPS 수정 방향", sc, f"{etf_rev:+.2f}%" + (f", ±{db:.1f}% 안쪽은 0점" if sc == 0 and db > 0 else "")))
    eg = ma_gap(etf, r["ma_days"])
    comps.append(("227540 200일선", 0 if eg is None else (1 if eg >= 0 else -1), NA if eg is None else f"{eg:+.1f}%"))
    score = sum(c[1] for c in comps)
    sector = "매력" if score >= r["sector_attractive_score"] else "비매력" if score <= r["sector_unattractive_score"] else "중립"
    return {"rows": rows, "winners": winners, "stock_verdict": stock_verdict, "etf_up": etf_up, "up_cov": up_cov,
            "etf_rev": etf_rev, "rev_cov": rev_cov, "comps": comps, "score": score, "sector": sector,
            "etf_px": etf[-1][1] if etf else None, "etf_ret": etf_ret, "etf_ma": eg}


def f1(v, unit="%", sign=True):
    if v is None:
        return NA
    return (f"{v:+.1f}" if sign else f"{v:.1f}") + unit


def build(cfg: dict, ev: dict, data: dict, last: date, now: datetime, prev: dict | None, late: bool) -> tuple[str, str, bool]:
    key = {"stock": ev["stock_verdict"], "sector": ev["sector"]}
    changed = bool(prev) and prev != key
    wk = f"{last:%m/%d}"
    title = (f"[227540] ETF vs 개별주 주간 판정{' 판정 변경' if changed else ''}{' (늦은 발송)' if late else ''} | "
             f"{ev['stock_verdict']}, 섹터 {ev['sector']}")
    lines = []
    for x in ev["rows"]:
        mark = "우위" if x["ok"] else "아님(" + ", ".join(x["why"]) + ")"
        rev = NA if x["rev"] is None and x["eps"] is None else ("쌓는 중" if x["rev"] is None else f"{x['rev']:+.1f}%({x['rev_age']}일)")
        px = f"{x['px']:,.0f}원" if x["px"] else NA
        per = f"{x['per']:.1f}배" if x["per"] else NA
        lines.append(f"{x['name']} {x['code']}(ETF 구성 {x['w']:.1f}%): {px}, 목표가 업사이드 {f1(x['up'])}, EPS 수정 {rev}, "
                     f"추정 PER {per}, 13주 227540 대비 {f1(x['rel'], '%p')}, 200일선 대비 {f1(x['ma'])} -> {mark}")
    info = data.get("info") or {}
    aum = info.get("aum_eok")
    aum_txt = NA if aum is None else f"{aum:,.0f}억원" + (" (작음, 유동성 주의)" if aum < cfg["rules"]["aum_warn_eok"] else "")
    comps = ", ".join(f"{n} {'+' if s > 0 else '-' if s < 0 else '0'} ({t})" for n, s, t in ev["comps"])
    top2 = sum(h["weight"] for h in sorted(data["holdings"], key=lambda h: -h["weight"])[:2])
    if ev["winners"]:
        rest = ", ".join(x["name"] for x in ev["winners"][1:])
        act = (f"{ev['winners'][0]['name']} 이 ETF 보다 조건이 낫습니다(업사이드 차이 {ev['winners'][0]['gap']:+.1f}%p)"
               + (f", 다음 {rest}" if rest else "") + ". 바꿀지는 미리 정한 계획 안에서만 보세요.")
    else:
        act = "개별주 조건을 다 맞춘 종목이 없어 227540 을 그대로 두는 쪽이 기본입니다."
    msg = "\n".join([
        f"{now:%Y-%m-%d %H:%M KST} | 227540 | ETF vs 개별주 주간 판정",
        "이게 뭐 알림: TIGER 200 헬스케어(227540)와 상위 구성종목 비교, 한국 헬스케어 섹터 매력 판정. 그 주 마지막 거래일 19:00 무렵 주 1번(판정이 같아도 옴). 매매 신호 아님.",
        f"지금 결과/상태: {wk} 종가 기준. 개별주 판정 {ev['stock_verdict']}, 섹터 {ev['sector']}(점수 {ev['score']:+d})"
        + (f", 지난주 {prev['stock']}, 섹터 {prev['sector']}에서 바뀜" if changed else "") + ".",
        f"ETF 가중 업사이드 {f1(ev['etf_up'])}, 가중 EPS 수정 {f1(ev['etf_rev'], '%') if ev['etf_rev'] is not None else '쌓는 중'}. "
        f"구성 비율 출처 {data['wsrc']}. 상위 2종목이 ETF 의 약 {top2:.0f}%라 ETF 도 두 종목 영향이 큼.",
        "종목: " + " / ".join(lines),
        f"섹터 점수 항목: {comps}.",
        f"227540 순자산 {aum_txt}, 3개월 순유입 {info.get('flow3m') or NA}.",
        f"기준: 개별주 우위 = 업사이드가 ETF 가중값보다 {cfg['rules']['stock_upside_gap_pp']:.0f}%p 이상 높음 + EPS 수정 0% 이상(확인된 경우) + 200일선 위. 섹터 2점 이상 매력, -2점 이하 비매력(비서 제안 기준, 컨센서스 아님).",
        f"지금 할 일: {act}",
        "나중에: 다음 주 마지막 거래일 19:00 무렵 다시 옵니다. 판정이 바뀌면 제목에 판정 변경.",
        "하지 말 것: 한 주 판정만 보고 갈아타기, 목표가 평균을 확정값으로 읽기, 확인 불가 항목을 0으로 읽기.",
    ])
    return title, msg, changed


# ---------------- 실행 ----------------
def main() -> int:
    args = sys.argv[1:]
    mode = (args[0] if args and not args[0].startswith("--") else "auto").lower()
    if mode == "selftest":
        return selftest()
    if mode not in {"auto", "dry"}:
        print(f"unknown mode {mode}")
        return 2
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    if "--as-of" in args:
        if mode != "dry":
            print("--as-of 는 dry 에서만")
            return 2
        now = datetime.strptime(args[args.index("--as-of") + 1], "%Y-%m-%d %H:%M").replace(tzinfo=KST)
    dry = mode == "dry"
    if now > datetime.strptime(cfg["ends_kst"], "%Y-%m-%d %H:%M").replace(tzinfo=KST):
        print("ended")
        return 0
    topic = resolve_topic("port", cfg.get("ntfy_topic"))
    server = resolve_server(cfg.get("ntfy_server"))
    state = load_state()
    tw = target_week(now, cfg.get("market_close_kst", "15:40"))
    print(f"now={now:%Y-%m-%d %H:%M} target={tw} last_sent_week={state.get('last_sent_week')}")
    if tw is None:
        return 0
    mon, last = tw
    key = mon.isoformat()
    if not dry and state.get("last_sent_week") and state["last_sent_week"] >= key:
        print("already sent for this week")
        return 0
    if (session_day(now) - last).days > int(cfg.get("catchup_days", 7)):
        print("target week too old, skip")
        return 0
    if last.isoformat() < cfg["created_at"][:10]:
        print("target week ended before this alert was created, skip")
        return 0
    late = session_day(now) != last
    data, errs = gather(cfg, last)
    if errs:
        print("errors: " + " | ".join(errs), file=sys.stderr)
    if not data.get("etf") or not any(s.get("closes") for s in data["stocks"].values()):
        title = "[227540] ETF vs 개별주 주간 판정 조회 실패 | 이번 주 판정 못 냄"
        msg = (f"{now:%Y-%m-%d %H:%M KST} | 227540 | 주간 판정\n지금 결과/상태: 227540 또는 구성종목 시세 {NA} "
               f"({' | '.join(errs)[:300]}).\n나중에: 다음 실행 때 다시 시도합니다.")
        print(f"TITLE: {title}\nTOPIC: {route_topic(topic, title)}\n{msg}")
        if dry:
            print("DRY RUN: no publish, state not written")
            return 0
        today = now.strftime("%Y-%m-%d")
        if state["fail_notice"].get("all") != today:
            publish(topic, title, msg, 3, ["warning"], server)
            state["fail_notice"]["all"] = today
            STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return 0
    ev = evaluate(cfg, data, state.get("eps_snap", {}), last)
    title, msg, changed = build(cfg, ev, data, last, now, state.get("verdict"), late)
    print(f"TITLE: {title}\nTOPIC: {route_topic(topic, title)}\n{msg}")
    if dry:
        print("DRY RUN: no publish, state not written")
        return 0
    publish(topic, title, msg, 4 if changed else 3, ["bar_chart"], server)
    snap = {c: s.get("eps") for c, s in data["stocks"].items() if s.get("eps")}
    state["eps_snap"][last.isoformat()] = snap
    state["eps_snap"] = dict(sorted(state["eps_snap"].items())[-12:])
    state["verdict"] = {"stock": ev["stock_verdict"], "sector": ev["sector"]}
    state["last_sent_week"] = key
    state["sent"][key] = {"sent_at": now.isoformat(), "stock": ev["stock_verdict"], "sector": ev["sector"], "score": ev["score"],
                          "changed": changed, "errors": errs[:6]}
    state["sent"] = dict(sorted(state["sent"].items())[-12:])
    state["updated_at"] = now.isoformat()
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


# ---------------- selftest ----------------
def selftest() -> int:
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    fails: list[str] = []

    def check(name, got, want):
        ok = got == want
        print(("ok   " if ok else "FAIL ") + f"{name}: got={got!r} want={want!r}")
        if not ok:
            fails.append(name)
    last = date(2026, 11, 6)

    def series(end, start, n=230):
        return [((last - timedelta(days=n - i)).isoformat(), start + (end - start) * i / (n - 1)) for i in range(n)]

    def mk(stocks, etf_trend=(18000, 20000), bench=(80000, 82000)):
        hold = [dict(h) for h in cfg["holdings_fallback"]]
        st = {}
        for h in hold:
            p = stocks.get(h["code"], {})
            st[h["code"]] = {"closes": series(p.get("end", 100), p.get("start", 90)), "target": p.get("target", 120),
                             "eps": p.get("eps", 10.0), "per": 20.0}
            if p.get("drop"):
                st[h["code"]] = {}
        return {"holdings": hold, "wsrc": "test", "stocks": st, "etf": series(etf_trend[1], etf_trend[0]),
                "bench": series(bench[1], bench[0]), "info": {"aum_eok": 296.0, "flow3m": "-29.3억"}}
    base_snap = {(last - timedelta(days=28)).isoformat(): {h["code"]: 10.0 for h in cfg["holdings_fallback"]}}
    ev = evaluate(cfg, mk({}), base_snap, last)
    check("all equal upside -> ETF 유지", ev["stock_verdict"], "ETF 유지")
    check("ETF weighted upside", round(ev["etf_up"], 1), 20.0)
    ev = evaluate(cfg, mk({"068270": {"target": 135}}), base_snap, last)
    check("upside +10.x%p gap, rev 0, above MA -> 개별주 우위 셀트리온", ev["stock_verdict"], "개별주 우위 셀트리온")
    ev = evaluate(cfg, mk({"068270": {"target": 135, "eps": 9.5}}), base_snap, last)
    check("negative revision blocks", ev["stock_verdict"], "ETF 유지")
    ev = evaluate(cfg, mk({"068270": {"target": 135, "end": 100, "start": 130}}), base_snap, last)
    check("below 200DMA blocks", (ev["stock_verdict"], "200일선 아래" in ev["rows"][1]["why"]), ("ETF 유지", True))
    ev = evaluate(cfg, mk({"068270": {"target": 140}, "000100": {"target": 145}}), base_snap, last)
    check("2 qualify -> strongest first", ([x["name"] for x in ev["winners"]], ev["stock_verdict"]),
          (["유한양행", "셀트리온"], "개별주 우위 유한양행"))
    ev = evaluate(cfg, mk({"068270": {"target": 135}}), {}, last)
    check("no snapshot -> revision unknown does not block, sector rev 0", (ev["stock_verdict"], ev["comps"][1][1]), ("개별주 우위 셀트리온", 0))
    young = {(last - timedelta(days=3)).isoformat(): {"068270": 20.0}}
    check("snapshot younger than 7 days ignored", revision(young, "068270", 10.0, last, cfg["rules"]), (None, None))
    two = {(last - timedelta(days=7)).isoformat(): {"x": 8.0}, (last - timedelta(days=28)).isoformat(): {"x": 5.0}}
    check("picks ~28 day snapshot", revision(two, "x", 10.0, last, cfg["rules"]), (100.0, 28))
    ev = evaluate(cfg, mk({"068270": {"drop": True}}), base_snap, last)
    check("missing stock data excluded, not winner", (ev["rows"][1]["ok"], ev["stock_verdict"]), (False, "ETF 유지"))
    up = {(last - timedelta(days=28)).isoformat(): {h["code"]: 9.0 for h in cfg["holdings_fallback"]}}
    ev = evaluate(cfg, mk({}, etf_trend=(18000, 21000), bench=(80000, 80500)), up, last)
    check("sector 매력 (+3)", (ev["sector"], ev["score"]), ("매력", 3))
    dn = {(last - timedelta(days=28)).isoformat(): {h["code"]: 11.0 for h in cfg["holdings_fallback"]}}
    ev = evaluate(cfg, mk({}, etf_trend=(22000, 18000), bench=(80000, 90000)), dn, last)
    check("sector 비매력 (-3)", (ev["sector"], ev["score"]), ("비매력", -3))
    ev = evaluate(cfg, mk({}, etf_trend=(18000, 20000), bench=(80000, 90000)), {}, last)
    check("sector 중립 (rs -, rev 0, ma +)", (ev["sector"], ev["score"]), ("중립", 0))
    tiny = {(last - timedelta(days=7)).isoformat(): {h["code"]: 10.0 for h in cfg["holdings_fallback"]}}
    ev = evaluate(cfg, mk({"068270": {"eps": 10.01}}), tiny, last)
    check("weighted rev inside deadband scores 0", (ev["comps"][1][1], "안쪽은 0점" in ev["comps"][1][2]), (0, True))
    ev = evaluate(cfg, mk({}), {(last - timedelta(days=7)).isoformat(): {h["code"]: 9.9 for h in cfg["holdings_fallback"]}}, last)
    check("weighted rev +1.0% beyond deadband scores +1", ev["comps"][1][1], 1)
    d = mk({"068270": {"target": 135}})
    ev = evaluate(cfg, d, base_snap, last)
    now = datetime(2026, 11, 6, 19, 0, tzinfo=KST)
    t1, m1, c1 = build(cfg, ev, d, last, now, None, False)
    t2, m2, c2 = build(cfg, ev, d, last, now, {"stock": "ETF 유지", "sector": "중립"}, False)
    t3, _, c3 = build(cfg, ev, d, last, now, {"stock": ev["stock_verdict"], "sector": ev["sector"]}, False)
    check("first week: no change flag", (c1, "판정 변경" in t1), (False, False))
    check("changed vs last week: flag in title and body", (c2, "판정 변경" in t2, "지난주 ETF 유지" in m2), (True, True, True))
    check("same verdict: still sent, no flag", (c3, "판정 변경" in t3), (False, False))
    check("title prefix and not routed to ops", (t2.startswith("[227540]"), route_topic("cwally-port", t2)), (True, "cwally-port"))
    check("fail title routes to ops", route_topic("cwally-port", "[227540] ETF vs 개별주 주간 판정 조회 실패 | 이번 주 판정 못 냄"), "cwally-ops")
    d2 = mk({})
    d2["info"] = {"aum_eok": 150.0, "flow3m": None}
    _, m4, _ = build(cfg, evaluate(cfg, d2, {}, last), d2, last, now, None, True)
    check("AUM < 200억 flagged, unknown rev shows 쌓는 중", ("유동성 주의" in m4, "쌓는 중" in m4), (True, True))
    allt = t1 + m1 + t2 + m2 + m4
    check("no middle dot / em-dash / banned words", ("\u00b7" in allt, "\u2014" in allt, [b for b in BAD if b in allt]), (False, False, []))
    check("ends 2027-10-31 23:59", cfg["ends_kst"], "2027-10-31 23:59")
    import krhc_monitor
    check("weekly target same as krhc (Fri 19:00 -> this week)", target_week(datetime(2026, 11, 6, 19, 0, tzinfo=KST), "15:40"),
          krhc_monitor.target_week(datetime(2026, 11, 6, 19, 0, tzinfo=KST), "15:40"))
    print(f"SELFTEST {'PASS' if not fails else 'FAIL'}: {len(fails)} failed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
