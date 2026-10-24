#!/usr/bin/env python3
"""[TSLA-SPCX] Tesla SpaceX 합병 감시. 매매 신호 아님, 자동 주문 아님.

매일 07:30 KST (cron-job.org 정시 dispatch {job:tslaspcx, oneoff_mode:auto} + GitHub cron 30 22 * * * 백업).
1) 공시 (우선순위 4, 확정 신호면 5): SEC EDGAR submissions (Tesla CIK 0001318605, SpaceX CIK 0001181412) 에서
   본 적 없는 8-K, 425, S-4(/A), DEFM14A, PREM14A, SC TO, DEFA14A. 주문서와 첨부 htm/txt 를 받아
   상대 회사 이름이나 merger 가 있으면 알림. exchange ratio, premium, per share, merger agreement,
   special committee 주변 발췌와 공시 링크.
2) 예측시장 급변 (4): Polymarket '2027년 말까지 공식 발표' Yes 가 7일 전 대비 10%p 이상 변하거나
   50%, 80% 를 넘으면(3%p 히스테리시스). 다른 기한 시장과 Kalshi 값은 참고 표시만.
3) 위원회 무산 신호 (4): 같은 공시 본문에 특별위원회 해산, 협상 종료, 합병 추진 안 함 문구.
중복 방지: state 의 본 접수번호(seen)와 예측시장 무장 상태. 두 실행(정시 + 백업)이 다 와도 한 번만.
조회 실패한 값은 본문 '확인 불가', 한 출처 3번 연속 실패와 스크립트 오류는 cwally-ops.
modes: auto | dry(발송 없음, state 안 씀) | selftest(가짜 값, 네트워크 없음) | seed(빌드 때 1회: 현재 값으로 state 작성, 발송 없음)
"""
from __future__ import annotations

import html
import json
import re
import sys
import time
import traceback
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic, route_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
NAME = "tslaspcx"
CFG_PATH = ROOT / "configs" / f"{NAME}.json"
STATE_PATH = ROOT / "state" / f"{NAME}.json"
NA = "확인 불가"
MAX_BODY_BYTES = 3500
GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
KALSHI = "https://api.elections.kalshi.com/trade-api/v2"


# ---------------- io ----------------
def load_state() -> dict:
    base = {"updated_at": None, "seeded": False, "sec": {}, "seen": [],
            "pm": {"move_armed": True, "levels": {}, "hist": {}}, "fail_runs": {}, "fail_notice": {}, "sent": []}
    if STATE_PATH.exists():
        try:
            base.update(json.loads(STATE_PATH.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass
    return base


def get(url: str, ua: str = "Mozilla/5.0 (cwally-alerts)", timeout: int = 30, limit: int | None = None) -> str:
    last: Exception | None = None
    for attempt in (1, 2):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept-Encoding": "identity"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read(limit + 1) if limit else resp.read()
                if limit and len(raw) > limit:
                    raw = raw[:limit]
                return raw.decode("utf-8", errors="replace")
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt == 1:
                time.sleep(2)
    raise last  # type: ignore[misc]


def clean(s: str) -> str:
    """Korean alert text rule: no middle dot, no em-dash (also normalise en-dash)."""
    return s.replace("\u2014", "-").replace("\u2013", "-").replace("\u00b7", " ").replace("\u2022", " ")


def strip_html(t: str) -> str:
    t = re.sub(r"(?is)<(script|style|ix:header)\b.*?</\1>", " ", t)
    t = re.sub(r"(?s)<[^>]+>", " ", t)
    t = html.unescape(t).replace("\xa0", " ")
    return re.sub(r"\s+", " ", t)


def kst(ts: str | None) -> str:
    if not ts:
        return NA
    try:
        d = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return ts
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(KST).strftime("%Y-%m-%d %H:%M KST")


# ---------------- SEC ----------------
def sec_recent(cik: str, ua: str) -> list[dict]:
    d = json.loads(get(f"https://data.sec.gov/submissions/CIK{cik}.json", ua))
    r = d["filings"]["recent"]
    out = []
    for i in range(len(r["accessionNumber"])):
        out.append({"acc": r["accessionNumber"][i], "form": r["form"][i], "filed": r["filingDate"][i],
                    "accepted": r["acceptanceDateTime"][i], "primary": r["primaryDocument"][i],
                    "desc": r.get("primaryDocDescription", [""] * (i + 1))[i]})
    return out


def filing_url(cik: str, acc: str, doc: str = "") -> str:
    return f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc.replace('-', '')}/{doc}"


def index_page(cik: str, acc: str) -> str:
    return filing_url(cik, acc, f"{acc}-index.htm")


def filing_texts(cik: str, f: dict, cfg: dict) -> list[tuple[str, str]]:
    ua = cfg["sec"]["user_agent"]
    names = [f["primary"]] if f.get("primary") else []
    try:
        idx = json.loads(get(filing_url(cik, f["acc"], "index.json"), ua))
        for it in idx["directory"]["item"]:
            n = it.get("name", "")
            low = n.lower()
            if n in names or not low.endswith((".htm", ".html", ".txt")):
                continue
            if re.match(r"r\d+\.htm$", low) or "index" in low or low == f"{f['acc']}.txt" or low.startswith("filingsummary"):
                continue
            names.append(n)
    except Exception as exc:  # noqa: BLE001
        if not names:
            raise RuntimeError(f"index.json {type(exc).__name__}")
    out = []
    for n in names[: int(cfg["sec"]["max_docs_per_filing"])]:
        if "/" in n:  # xsl 렌더(소유 공시 등)는 건너뜀
            continue
        raw = get(filing_url(cik, f["acc"], n), ua, 45, int(cfg["sec"]["max_doc_bytes"]))
        out.append((n, strip_html(raw) if n.lower().endswith((".htm", ".html")) else re.sub(r"\s+", " ", raw)))
    if not out:
        raise RuntimeError("문서 없음")
    return out


def analyze(texts: list[tuple[str, str]], comp: dict, cfg: dict, definitive: bool = False) -> dict:
    allt = " ".join(t for _, t in texts)
    prox = int(cfg["sec"]["proximity_chars"])
    deal = re.compile(cfg["sec"]["deal_terms_re"], re.I)
    rel = []
    names = [m for m in re.finditer(comp["counterpart_re"], allt, re.I)]
    for m in names:
        near = deal.search(allt, max(0, m.start() - prox), m.end() + prox)
        if near:
            rel += [m.group(0), near.group(0)]
    if definitive and names:
        rel += [names[0].group(0)]
    terms = cfg["sec"]["snippet_terms"]
    n = int(cfg["sec"]["snippet_chars"])
    counts = {}
    wins: list[list] = []  # [start, end, {terms}]
    for term in terms:
        hits = [m.start() for m in re.finditer(re.escape(term), allt, re.I)]
        counts[term] = len(hits)
        for h in hits[:2]:
            wins.append([max(0, h - n), min(len(allt), h + len(term) + n), {term}])
    snippets = [(", ".join(sorted(w[2], key=terms.index)), allt[w[0]:w[1]].strip()) for w in merge_windows(wins)][:6]
    tw: list[list] = []
    for pat in cfg["termination"]["patterns"]:
        m = re.search(pat, allt, re.I)
        if m:
            tw.append([max(0, m.start() - n), min(len(allt), m.end() + n), {"x"}])
    term_hits = [allt[w[0]:w[1]].strip() for w in merge_windows(tw)]
    low = allt.lower()
    definitive_text = "merger agreement" in low and ("exchange ratio" in low or "per share" in low)
    return {"relevant": bool(rel), "rel_words": sorted({r.strip().lower(): r.strip() for r in rel}.values(), key=str.lower)[:4], "snippets": snippets,
            "counts": counts, "termination": term_hits, "definitive_text": definitive_text,
            "docs": [d for d, _ in texts]}


def merge_windows(wins: list[list]) -> list[list]:
    out: list[list] = []
    for w in sorted(wins, key=lambda x: x[0]):
        if out and w[0] <= out[-1][1]:
            out[-1][1] = max(out[-1][1], w[1])
            out[-1][2] |= w[2]
        else:
            out.append([w[0], w[1], set(w[2])])
    return out


def form_ok(form: str, cfg: dict) -> bool:
    return form.upper() in {x.upper() for x in cfg["sec"]["forms"]}


# ---------------- 예측시장 ----------------
def pm_markets(cfg: dict) -> dict[str, dict]:
    slugs = [m["slug"] for m in cfg["markets"]["polymarket_show"]]
    q = "&".join("slug=" + urllib.parse.quote(s) for s in slugs)
    data = json.loads(get(f"{GAMMA}/markets?{q}&limit=50"))
    out = {}
    for m in data:
        try:
            yes = float(json.loads(m["outcomePrices"])[0]) * 100
            outs = json.loads(m.get("outcomes") or "[]")
            tok = json.loads(m.get("clobTokenIds") or "[]")
        except (KeyError, ValueError, TypeError, IndexError):
            continue
        if outs and outs[0] != "Yes":
            continue
        out[m["slug"]] = {"p": round(yes, 1), "token": tok[0] if tok else None, "closed": bool(m.get("closed")),
                          "updated": m.get("updatedAt")}
    if not out:
        raise RuntimeError("Gamma 빈 응답")
    return out


def pm_7d_ago(token: str, now: datetime) -> tuple[float, str] | None:
    end = int(now.timestamp())
    start = end - 10 * 86400
    d = json.loads(get(f"{CLOB}/prices-history?market={token}&startTs={start}&endTs={end}&fidelity=60"))
    target = end - 7 * 86400
    pts = [p for p in d.get("history") or [] if int(p["t"]) <= target]
    if not pts:
        return None
    p = pts[-1]
    return round(float(p["p"]) * 100, 1), datetime.fromtimestamp(int(p["t"]), KST).strftime("%Y-%m-%d %H:%M KST")


def kalshi_values(cfg: dict) -> dict[str, dict]:
    ev = cfg["markets"]["kalshi_event"]
    d = json.loads(get(f"{KALSHI}/markets?event_ticker={ev}&limit=100"))
    out = {}
    for m in d.get("markets") or []:
        bid, ask, last = (m.get("yes_bid_dollars"), m.get("yes_ask_dollars"), m.get("last_price_dollars"))
        try:
            b, a = float(bid), float(ask)
            mid = (b + a) / 2 * 100 if 0 < b <= a < 1 else None
        except (TypeError, ValueError):
            mid = None
        try:
            lp = float(last) * 100
        except (TypeError, ValueError):
            lp = None
        out[m["ticker"]] = {"mid": None if mid is None else round(mid, 1), "last": None if lp is None else round(lp, 1),
                            "status": m.get("status")}
    if not out:
        raise RuntimeError("Kalshi 빈 응답")
    return out


# ---------------- 판정 (순수 함수) ----------------
def level_init(p: float, lv: float, hy: float) -> dict:
    side = "above" if p >= lv else "below"
    return {"side": side, "away": abs(p - lv) >= hy}


def eval_level(st: dict, p: float, lv: float, hy: float) -> str | None:
    """선 lv 를 넘으면 1회. 마지막 알림 뒤 그 선에서 hy 이상 멀어진 적이 있거나, 반대쪽으로 hy 이상 넘어가야 다음 알림."""
    if st["side"] == "below":
        if p >= lv and (st["away"] or p >= lv + hy):
            st["side"], st["away"] = "above", p >= lv + hy
            return "up"
        if p <= lv - hy:
            st["away"] = True
        return None
    if p < lv and (st["away"] or p <= lv - hy):
        st["side"], st["away"] = "below", p <= lv - hy
        return "down"
    if p >= lv + hy:
        st["away"] = True
    return None


def eval_move(st: dict, delta: float | None, cfg: dict) -> str | None:
    if delta is None:
        return None
    g = cfg["markets"]
    if st.get("move_armed", True):
        if abs(delta) >= g["move_pp"] - 1e-9:
            st["move_armed"] = False
            return "move"
        return None
    if abs(delta) < g["move_rearm_pp"]:
        st["move_armed"] = True
        return "rearm"
    return None


# ---------------- 문구 ----------------
def body(stamp: str, what: str, status: str, action: str, later: str, trim: str, avoid: str) -> str:
    return "\n".join([f"{stamp} | TSLA-SPCX | Tesla SpaceX 합병 점검", f"이게 뭐 알림: {what}", f"지금 결과/상태: {status}",
                      f"지금 할 일: {action}", f"나중에: {later}", f"익절/줄일 때: {trim}", f"하지 말 것: {avoid}"])


def fit(parts_fixed: list[str], snippets: list[str]) -> str:
    """본문을 MAX_BODY_BYTES 아래로: 발췌를 뒤에서부터 줄인다."""
    snips = list(snippets)
    while True:
        status_extra = (" 발췌: " + " / ".join(snips)) if snips else ""
        txt = parts_fixed[0].replace("{SNIPS}", status_extra)
        if len(txt.encode("utf-8")) <= MAX_BODY_BYTES or not snips:
            return txt
        last = snips[-1]
        if len(last) > 120:
            snips[-1] = last[: len(last) // 2] + "..."
        else:
            snips.pop()


def market_line(pm: dict | None, cfg: dict, d7: tuple | None, ks: dict | None) -> str:
    g = cfg["markets"]
    if pm is None:
        pmt = f"Polymarket {NA}"
    else:
        vals = []
        for m in g["polymarket_show"]:
            v = pm.get(m["slug"])
            vals.append(f"{m['label']} {v['p']:.1f}%" if v else f"{m['label']} {NA}")
        pmt = "Polymarket(공식 발표 기한별 Yes) " + ", ".join(vals)
        prim = pm.get(g["polymarket_primary"])
        if prim:
            pmt += (f". 2027년 말 7일 전({d7[1][5:16]}) {d7[0]:.1f}% 대비 {prim['p'] - d7[0]:+.1f}%p" if d7 else f". 7일 전 값 {NA}")
    if ks is None:
        kt = ""  # 공개 시장 값이 없으면 생략
    else:
        vals = []
        for m in g["kalshi_show"]:
            v = ks.get(m["ticker"])
            if v and (v["mid"] is not None or v["last"] is not None):
                vals.append(f"{m['label']} {v['mid'] if v['mid'] is not None else v['last']:.1f}%")
        kt = (". Kalshi(구속력 있는 합의 발표) " + ", ".join(vals)) if vals else ""
    return pmt + kt


def filing_msg(cfg, stamp, comp, f, a, mline, both):
    defin = f["form"].upper() in {x.upper() for x in cfg["sec"]["definitive_forms"]} or a["definitive_text"]
    pri = cfg["sec"]["priority_definitive"] if defin else cfg["sec"]["priority_default"]
    who = comp["key"].replace("tesla", "Tesla").replace("spacex", "SpaceX")
    head = "합병 확정 신호 공시" if defin else "합병 관련 공시"
    title = f"[TSLA-SPCX] {who} {f['form']} {head} | 원문에서 조건 직접 확인"
    cnt = ", ".join(f"{k} {v}회" for k, v in a["counts"].items() if v) or "핵심 단어 없음"
    snips = [clean(f"[{t}] ...{s}...") for t, s in a["snippets"]]
    status = (f"{comp['name']} {f['form']} 접수 {kst(f['accepted'])} (제출일 {f['filed']}). 관련 단어: {', '.join(a['rel_words'])}. "
              f"단어 수: {cnt}. 문서 {len(a['docs'])}개. 링크 {index_page(comp['cik'], f['acc'])}"
              + (f" (상대 회사 공시에도 같은 접수번호: {both})" if both else "") + "{SNIPS}" + f". {mline}.")
    fixed = body(stamp, "Tesla 또는 SpaceX 가 합병 관련 양식(8-K, 425, S-4, 위임장, 공개매수 등)을 내고 본문에 상대 회사 이름이 합병 관련 단어와 같이 있으면 오는 알림. "
                        "확정 신호(425, S-4, 합병 위임장, 공개매수, 또는 merger agreement 와 교환비율/주당 가격)면 우선순위 5. 매매 신호 아님.",
                 status,
                 "링크에서 교환비율, 프리미엄, 주당 가격, 표결 조건을 원문으로 직접 확인. TSLA, SPCX 비중 판단은 조건을 본 뒤.",
                 "같은 건의 다음 공시(S-4, 합병 위임장, 425 추가)가 나오면 다시 옵니다. 매일 07:30 KST 점검.",
                 "발표 직후 급등락이면 계획한 비중 안에서만 나눠서 조정.",
                 "발췌나 기사 제목만 보고 바로 매매, 보도와 공시를 섞어 읽기.")
    return title, fit([fixed], snips), pri, (["rotating_light"] if defin else ["page_facing_up"])


def termination_msg(cfg, stamp, comp, f, a, mline):
    who = comp["key"].replace("tesla", "Tesla").replace("spacex", "SpaceX")
    title = f"[TSLA-SPCX] {who} {f['form']} 합병 무산 신호 | 원문 확인"
    snips = [clean(f"[무산 문구] ...{s}...") for s in a["termination"][:2]] + [clean(f"[{t}] ...{s}...") for t, s in a["snippets"][:2]]
    status = (f"{comp['name']} {f['form']} 접수 {kst(f['accepted'])}. 특별위원회 해산, 협상 종료, 합병 추진 안 함에 해당하는 문구가 보임. "
              f"링크 {index_page(comp['cik'], f['acc'])}" + "{SNIPS}" + f". {mline}.")
    fixed = body(stamp, "Tesla 또는 SpaceX 공시 본문에 합병 검토 무산 문구(특별위원회 해산, 협상 종료, 추진 안 함)가 보이면 오는 알림. 매매 신호 아님.",
                 status,
                 "원문에서 무산이 확정인지(종료, 해산) 보류인지(일시 중단) 직접 확인.",
                 "다시 검토한다는 공시가 나오면 공시 알림으로 옵니다. 무산이 확정이면 이 알림을 끄는 것도 검토.",
                 "합병 기대로 늘린 비중이 있으면 원래 계획 비중으로 돌릴지 확인.",
                 "문구 일부만 보고 확정으로 단정하기.")
    return title, fit([fixed], snips), cfg["termination"]["priority"], ["warning"]


def market_msg(cfg, stamp, p, d7, events, mline):
    g = cfg["markets"]
    why = []
    for ev in events:
        if ev == "move":
            why.append(f"7일 {p - d7[0]:+.1f}%p")
        elif ev.startswith("up"):
            why.append(f"{ev[2:]}% 위로")
        elif ev.startswith("down"):
            why.append(f"{ev[4:]}% 아래로")
    title = f"[TSLA-SPCX] Polymarket 2027년 말 합병 발표 {p:.1f}% ({', '.join(why)}) | 예측시장 급변"
    status = f"{mline}. 사유: {', '.join(why)}."
    m = body(stamp, f"Polymarket '2027년 말까지 Tesla SpaceX 합병 공식 발표' 확률이 7일 전 대비 {g['move_pp']}%p 이상 움직이거나 "
                    f"{'%, '.join(str(x) for x in g['levels_pct'])}% 를 넘을 때 오는 알림({g['hysteresis_pp']}%p 히스테리시스). 매매 신호 아님.",
             status,
             "확률 변화의 이유가 공시인지 보도인지 확인. 공시 알림이 같이 왔는지 보기.",
             f"변화폭이 {g['move_rearm_pp']}%p 아래로 줄면 다시 무장. 같은 선은 {g['hysteresis_pp']}%p 넘게 멀어졌다가 다시 넘을 때만 다시 옵니다.",
             "예측시장 숫자만으로 비중을 크게 바꾸지 말고 공시 확인 뒤 계획 안에서만.",
             "얇은 예측시장 호가 변화를 확정 소식으로 읽기.")
    return title, m, cfg["markets"]["priority_move"], ["chart_with_upwards_trend" if events and (events[0].startswith("up") or (d7 and p >= d7[0])) else "chart_with_downwards_trend"]


# ---------------- main ----------------
def fetch_markets(cfg: dict, now: datetime, errors: list[str]):
    pm = d7 = ks = None
    try:
        pm = pm_markets(cfg)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"polymarket: {type(exc).__name__} {str(exc)[:60]}")
    prim = (pm or {}).get(cfg["markets"]["polymarket_primary"])
    if prim and prim.get("token"):
        try:
            d7 = pm_7d_ago(prim["token"], now)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"polymarket history: {type(exc).__name__} {str(exc)[:60]}")
    try:
        ks = kalshi_values(cfg)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"kalshi: {type(exc).__name__} {str(exc)[:60]}")
    return pm, d7, ks


def run(mode: str, now: datetime, cfg: dict) -> int:
    dry = mode == "dry"
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    today = now.strftime("%Y-%m-%d")
    topic = resolve_topic("port", cfg.get("ntfy_topic"))
    server = resolve_server(cfg.get("ntfy_server"))
    state = load_state()
    if not state.get("seeded"):
        print("state 가 seed 되지 않음: 'seed' 모드를 먼저 실행")
        return 2
    errors: list[str] = []
    out: list[tuple[str, str, int, list[str]]] = []
    ok: dict[str, bool] = {}

    pm, d7, ks = fetch_markets(cfg, now, errors)
    ok["polymarket"] = pm is not None
    ok["kalshi"] = ks is not None
    mline = market_line(pm, cfg, d7, ks)

    # --- 1) + 3) 공시 ---
    seen = set(state.get("seen") or [])
    ua = cfg["sec"]["user_agent"]
    new_by_acc: dict[str, list] = {}
    for comp in cfg["sec"]["companies"]:
        cs = state.setdefault("sec", {}).setdefault(comp["key"], {})
        try:
            recent = sec_recent(comp["cik"], ua)
            ok[f"sec_{comp['key']}"] = True
        except Exception as exc:  # noqa: BLE001
            ok[f"sec_{comp['key']}"] = False
            errors.append(f"SEC {comp['key']}: {type(exc).__name__} {str(exc)[:60]}")
            continue
        cursor = cs.get("seed_accepted") or ""
        for f in recent:
            if f["acc"] in seen or f["accepted"] <= cursor:
                continue
            new_by_acc.setdefault(f["acc"], []).append((comp, f))
        if recent:
            top = max(recent, key=lambda x: x["accepted"])
            cs["latest"] = {"acc": top["acc"], "form": top["form"], "accepted": top["accepted"], "filed": top["filed"]}
    doc_fail = False
    lines = []
    for acc, items in sorted(new_by_acc.items(), key=lambda kv: kv[1][0][1]["accepted"]):
        comp, f = items[0]
        both = ", ".join(c["key"] for c, _ in items[1:])
        if not form_ok(f["form"], cfg):
            seen.add(acc)
            lines.append(f"skip form {comp['key']} {f['form']} {acc}")
            continue
        try:
            texts = filing_texts(comp["cik"], f, cfg)
        except Exception as exc:  # noqa: BLE001
            doc_fail = True
            errors.append(f"SEC doc {acc}: {type(exc).__name__} {str(exc)[:60]}")
            lines.append(f"doc fetch failed {comp['key']} {f['form']} {acc}: retry next run")
            continue
        a = analyze(texts, comp, cfg, f["form"].upper() in {x.upper() for x in cfg["sec"]["definitive_forms"]})
        seen.add(acc)
        lines.append(f"{comp['key']} {f['form']} {acc} relevant={a['relevant']} words={a['rel_words']} counts={a['counts']} term={len(a['termination'])}")
        if not a["relevant"]:
            continue
        if a["termination"]:
            out.append(termination_msg(cfg, stamp, comp, f, a, mline))
        else:
            out.append(filing_msg(cfg, stamp, comp, f, a, mline, both))
        state.setdefault("sent", []).append({"at": now.isoformat(), "kind": "termination" if a["termination"] else "filing", "acc": acc})
    ok["sec_docs"] = not doc_fail
    state["seen"] = sorted(seen)[-int(cfg["sec"]["seen_keep"]):]

    # --- 2) 예측시장 ---
    g = cfg["markets"]
    pms = state.setdefault("pm", {"move_armed": True, "levels": {}, "hist": {}})
    prim = (pm or {}).get(g["polymarket_primary"])
    if prim:
        p = prim["p"]
        pms.setdefault("hist", {})[today] = p
        pms["hist"] = dict(sorted(pms["hist"].items())[-30:])
        if d7 is None:  # CLOB 기록이 없으면 state 의 7일 전 값
            k7 = (now - timedelta(days=7)).strftime("%Y-%m-%d")
            older = [k for k in pms["hist"] if k <= k7]
            if older:
                d7 = (pms["hist"][max(older)], max(older) + " 00:00 (state)")
        events = []
        mv = eval_move(pms, (p - d7[0]) if d7 else None, cfg)
        if mv == "move":
            events.append("move")
        for lv in g["levels_pct"]:
            st = pms.setdefault("levels", {}).setdefault(str(lv), level_init(p, lv, g["hysteresis_pp"]))
            r = eval_level(st, p, lv, g["hysteresis_pp"])
            if r:
                events.append(f"{r}{lv}")
        pms["last"] = {"p": p, "at": now.isoformat(), "d7": d7[0] if d7 else None}
        lines.append(f"polymarket 2027 {p:.1f}% d7={d7} move={mv} levels={pms['levels']}")
        if events:
            out.append(market_msg(cfg, stamp, p, d7, events, mline))
            state.setdefault("sent", []).append({"at": now.isoformat(), "kind": "market", "events": events})
    else:
        lines.append(f"polymarket 2027 {NA}")
    state["sent"] = state.get("sent", [])[-50:]

    # --- 연속 실패 -> ops ---
    fr = state.setdefault("fail_runs", {})
    for k, good in ok.items():
        fr[k] = 0 if good else int(fr.get(k, 0)) + 1
        if fr[k] >= int(cfg["ops_fail_after_runs"]) and state.setdefault("fail_notice", {}).get(k) != today:
            state["fail_notice"][k] = today
            out.append((f"[TSLA-SPCX] {k} 조회 실패 {fr[k]}회 연속 | 판정 보류",
                        body(stamp, "TSLA-SPCX 알림의 데이터 조회 실패 안내.",
                             clean(f"{k} {NA}: {fr[k]}회 연속 실패. {' | '.join(errors)[:400]}"),
                             "필요하면 SEC EDGAR, Polymarket 사이트에서 직접 보세요.", "다음 07:30 실행 때 다시 봅니다.",
                             "이번 안내로 비중을 바꾸지 마세요.", "조회 실패를 '새 공시 없음'으로 읽기."), 3, ["warning"]))

    if errors:
        print("source errors: " + " | ".join(errors), file=sys.stderr)
    print(f"now={stamp} markets: {mline}")
    for ln in lines:
        print(ln)
    for title, msg, pri, tags in out:
        assert "\u00b7" not in title + msg and "\u2014" not in title + msg
        print(f"\n--- {'DRY ' if dry else ''}publish -> {route_topic(topic, title)} p{pri} {tags} ({len(msg.encode())} bytes)\nTITLE: {title}\n{msg}")
        if not dry:
            publish(topic, title, msg, pri, tags, server)
    if not out:
        print("no alert")
    if dry:
        print("DRY RUN: no publish, state not written")
        return 0
    state["updated_at"] = now.isoformat()
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


def seed(cfg: dict, now: datetime, force: bool) -> int:
    """빌드 때 1회: 지금 공시는 모두 본 것으로, 예측시장 무장 상태는 지금 값 기준으로. 발송 없음."""
    if STATE_PATH.exists() and json.loads(STATE_PATH.read_text(encoding="utf-8")).get("seeded") and not force:
        print("already seeded (use --force)")
        return 2
    errors: list[str] = []
    st = {"updated_at": now.isoformat(), "seeded": True, "seed_at": now.isoformat(), "sec": {}, "seen": [],
          "pm": {}, "fail_runs": {}, "fail_notice": {}, "sent": [], "seed_values": {}}
    seen = set()
    for comp in cfg["sec"]["companies"]:
        recent = sec_recent(comp["cik"], cfg["sec"]["user_agent"])
        top = max(recent, key=lambda x: x["accepted"])
        st["sec"][comp["key"]] = {"seed_accepted": top["accepted"],
                                  "latest": {"acc": top["acc"], "form": top["form"], "accepted": top["accepted"], "filed": top["filed"]}}
        seen.update(f["acc"] for f in recent)
    st["seen"] = sorted(seen)[-int(cfg["sec"]["seen_keep"]):]
    pm, d7, ks = fetch_markets(cfg, now, errors)
    if errors:
        print("seed source errors: " + " | ".join(errors))
    g = cfg["markets"]
    prim = (pm or {}).get(g["polymarket_primary"])
    if not prim:
        print("Polymarket 2027 값 없음: seed 중단")
        return 1
    p = prim["p"]
    delta = (p - d7[0]) if d7 else None
    st["pm"] = {"move_armed": not (delta is not None and abs(delta) >= g["move_pp"]),
                "levels": {str(lv): level_init(p, lv, g["hysteresis_pp"]) for lv in g["levels_pct"]},
                "hist": {now.strftime("%Y-%m-%d"): p}, "last": {"p": p, "at": now.isoformat(), "d7": d7[0] if d7 else None}}
    st["seed_values"] = {"fetched_at": now.isoformat(),
                         "polymarket": {s: {"yes_pct": v["p"], "gamma_updated": v["updated"]} for s, v in (pm or {}).items()},
                         "polymarket_2027_7d_ago": {"yes_pct": d7[0], "at": d7[1]} if d7 else None,
                         "kalshi": ks}
    STATE_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: st[k] for k in ("sec", "pm", "seed_values")}, ensure_ascii=False, indent=1))
    return 0


def selftest(cfg: dict) -> int:
    """가짜 값으로 조건과 문구만 검사 (네트워크, ntfy, state 없음)."""
    stamp = "2026-11-02 07:30 KST"
    hy = cfg["markets"]["hysteresis_pp"]
    st = level_init(45.0, 50, hy)
    seq = [(49.0, None), (50.5, "up"), (49.5, None), (51.0, None), (53.5, None), (49.9, "down"), (50.2, None),
           (46.9, None), (50.0, "up"), (44.0, "down"), (54.0, "up")]
    for p, want in seq:
        got = eval_level(st, p, 50, hy)
        assert got == want, (p, got, want, st)
    st = level_init(65.5, 80, hy)
    assert st == {"side": "below", "away": True}
    assert eval_level(st, 80.0, 80, hy) == "up" and eval_level(st, 76.5, 80, hy) == "down"
    print("levels ok")
    ms = {"move_armed": True}
    seq = [(5.0, None), (-10.0, "move"), (-12.0, None), (-7.5, None), (-6.9, "rearm"), (10.2, "move")]
    for d, want in seq:
        got = eval_move(ms, d, cfg)
        assert got == want, (d, got, want)
    assert eval_move(ms, None, cfg) is None
    print("move ok")
    tesla = cfg["sec"]["companies"][0]
    spacex = cfg["sec"]["companies"][1]
    txt_def = ("Tesla, Inc. and Space Exploration Technologies Corp. entered into an Agreement and Plan of Merger (the \u201cmerger agreement\u201d). "
               "The special committee of independent directors \u2014 advised by counsel \u2014 recommended the transaction. "
               "Each SpaceX share will be converted at an exchange ratio of 0.000 Tesla shares, a premium of 00% to the closing price per share.")
    a = analyze([("ex99.htm", txt_def)], tesla, cfg)
    assert a["relevant"] and a["definitive_text"] and not a["termination"], a
    a_sp_self = analyze([("x.htm", "Space Exploration Technologies Corp. reported quarterly results.")], spacex, cfg)
    assert not a_sp_self["relevant"], "SpaceX self-name must not count"
    txt_term = ("Tesla announced that the special committee formed to evaluate a potential combination with SpaceX has been dissolved "
                "and discussions have been terminated. The company is no longer pursuing a merger with SpaceX.")
    at = analyze([("ex99.htm", txt_term)], tesla, cfg)
    assert at["relevant"] and at["termination"], at
    a_unrel = analyze([("x.htm", "Tesla reported results. Item 2.02. Per share data attached. SpaceX equity investment unrealized (gain) loss "
                                 + "1,005 " * 80 + "acquisition of intangible assets")], tesla, cfg)
    assert not a_unrel["relevant"], a_unrel["rel_words"]
    a_cursor = analyze([("x.htm", "Space Exploration Technologies Corp. Cursor Merger completed. " + "x " * 200 + "Tesla vehicles")], spacex, cfg)
    assert not a_cursor["relevant"], a_cursor["rel_words"]
    a_425 = analyze([("x.htm", "Communication regarding SpaceX. " + "x " * 400)], tesla, cfg, True)
    assert a_425["relevant"]
    assert form_ok("425", cfg) and form_ok("SC TO-T", cfg) and form_ok("DEFA14A", cfg) and not form_ok("10-Q", cfg) and not form_ok("4", cfg)
    print("filings ok")
    f = {"acc": "0000000000-26-000000", "form": "425", "filed": "2026-11-01", "accepted": "2026-11-01T21:05:00.000Z", "primary": "x.htm"}
    pm = {m["slug"]: {"p": v, "token": "t", "closed": False, "updated": None}
          for m, v in zip(cfg["markets"]["polymarket_show"], [21.0, 30.0, 35.0, 60.0, 76.0])}
    ks = {m["ticker"]: {"mid": v, "last": v, "status": "active"} for m, v in zip(cfg["markets"]["kalshi_show"], [40.0, 72.0])}
    ml = market_line(pm, cfg, (65.5, "2026-10-26 07:00 KST"), ks)
    ml_fail = market_line(None, cfg, None, None)
    assert NA in ml_fail and "Kalshi" not in ml_fail
    samples = [filing_msg(cfg, stamp, tesla, f, a, ml, ""), termination_msg(cfg, stamp, tesla, dict(f, form="8-K"), at, ml_fail),
               market_msg(cfg, stamp, 76.0, (65.5, "2026-10-26 07:00 KST"), ["move"], ml),
               market_msg(cfg, stamp, 49.0, (55.0, "2026-10-26 07:00 KST"), ["down50"], ml)]
    big = analyze([("ex.htm", ("merger agreement exchange ratio per share premium special committee SpaceX " * 2 + "x" * 400) * 30)], tesla, cfg)
    samples.append(filing_msg(cfg, stamp, tesla, f, big, ml, "spacex"))
    for t, m, p, tg in samples:
        assert "\u00b7" not in t + m and "\u2014" not in t + m, t
        assert route_topic("cwally-port", t) == "cwally-port", t
        assert len(m.encode()) <= MAX_BODY_BYTES, len(m.encode())
    assert samples[0][2] == 5 and samples[1][2] == 4 and samples[2][2] == 4
    for t, m, p, tg in samples[:4]:
        print(f"-- sample (가짜 숫자)\nTITLE: {t}\nPRIORITY: {p} TAGS: {tg}\n{m}\n")
    assert route_topic("cwally-port", "[TSLA-SPCX] polymarket 조회 실패 3회 연속 | 판정 보류") == "cwally-ops"
    print("SELFTEST OK: no network, no publish, no state")
    return 0


def main() -> int:
    args = sys.argv[1:]
    mode = (args[0] if args and not args[0].startswith("--") else "auto").lower()
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    if mode == "selftest":
        return selftest(cfg)
    now = datetime.now(KST)
    if mode == "seed":
        return seed(cfg, now, "--force" in args)
    if mode not in {"auto", "dry"}:
        print(f"unknown mode {mode}")
        return 2
    try:
        return run(mode, now, cfg)
    except Exception as exc:  # noqa: BLE001  스크립트 오류 -> cwally-ops
        print(traceback.format_exc(), file=sys.stderr)
        if mode == "dry":
            return 1
        try:
            publish(resolve_topic("ops"), "[TSLA-SPCX] 스크립트 오류 | 이번 실행 판정 못 함",
                    body(now.strftime("%Y-%m-%d %H:%M KST"), "TSLA-SPCX 알림 스크립트 오류 안내.",
                         clean(f"{type(exc).__name__}: {str(exc)[:200]}. state 는 바꾸지 않음."),
                         "할 일 없음. 다음 07:30 실행 때 다시 돕니다.", "같은 오류가 이어지면 스크립트 점검 필요.",
                         "이번 안내로 비중을 바꾸지 마세요.", "오류를 '새 공시 없음'으로 읽기."),
                    3, ["warning"], resolve_server(cfg.get("ntfy_server")))
        except Exception:  # noqa: BLE001
            return 1
        return 0


if __name__ == "__main__":
    sys.exit(main())
