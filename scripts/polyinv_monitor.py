#!/usr/bin/env python3
"""[POLYINV] 투자자용 주간 폴리마켓 확률 변화 (일요일 21:00 KST portlight 슬롯, [POLY] 옆).

[POLY] (scripts/poly_monitor.py) 는 그대로 두고, 이 스크립트는 투자에 영향 있는 이벤트만 골라
지난주 대비 확률 변화(%p)를 보여 준다. 데이터: Gamma API events (새 secret 없음).

필터 (configs/polyinv.json 에서 조정):
  - deny_tags / deny_keywords 에 걸리면 제외 (스포츠, 연예, 날씨, 토큰 에어드롭 등)
  - 선거 태그 이벤트는 election_allow_keywords (미 의회 다수당, 대선 승자, 셧다운) 만 남김
  - categories: 제목 키워드 먼저, 없으면 Gamma 태그로 분류. 분류 안 되면 제외
  - 1주 거래대금 < min_event_volume_1wk, 시장 유동성 < min_market_liquidity, 마감까지 < min_days_to_end 일 제외
  - 확률 drop_low(3%) 이하 / drop_high(97%) 이상 시장은 움직임과 상관없이 제외 (사실상 결정됨)
  - 여러 결과가 있는 이벤트: negRisk(상호배타)면 1위 결과가 drop_high 이상일 때 이벤트 전체 제외,
    아니면 남은 결과 중 가장 높은 확률 결과. 상호배타 아니면 가장 크게 움직인 시장(동률은 유동성)
  - 연준 고정(fed_pin): 금리 분류 중 미국 연준/FOMC 이벤트는 맨 위 고정(최대 3개, 전체 10개 안).
    97% 규칙은 적용하되, 남는 연준 시장이 없으면 가장 가까운 FOMC 결정 이벤트를 97% 이상이어도 표시.
    다른 중앙은행(BoC, ECB, BOJ, BOK)은 일반 순위.
  - 거시 고정(macro_pin, 2026-10-26 승인): 작은 미국 거시 시장(경기침체, CPI, 물가, PCE, 실업률, GDP) 1개를
    움직임과 상관없이 연준 고정 바로 뒤에 표시(전체 10개 안). 3% 초과 97% 미만, 마감 3일 이상, 유동성 $5K 이상
    (후보가 없을 때만 유동성 기준을 빼고 줄에 적음), 미국 먼저, 1주 거래대금 최대. 없으면 한 줄로 "거시 고정 항목 없음".
주간 변화: state markets[<market id>] = {p, at}. 기준값 나이가 5~10일이면 그 값 대비,
  아니면 Gamma oneWeekPriceChange(폴리마켓 7일 변화, * 표시), 그것도 없으면 더 짧은 기준값(MM/DD 대비), 없으면 신규.
정렬: 연준 고정 먼저, 나머지는 |변화| 큰 순, 같으면 1주 거래대금. 최대 max_items 개, 분류당 max_per_category 개(고정 항목은 분류 한도에 안 셈).

중복 묶음(dedupe_groups): 같은 사건의 다른 기한/창은 1주 거래대금이 가장 큰 이벤트 하나만. 분류별 max_items, min_event_volume_1wk 로 한도/거래대금 기준 개별 조정.
Modes: auto (일요일 20:00 이후 또는 월요일 12:00 KST 전, 주 1회) | force (요일 무시, state 기록) | selftest (오프라인 점검) |
       dry (출력만, ntfy 없음, state 안 씀) | seed (현재 확률을 state 에 기준값으로 저장, ntfy 없음) |
       install (설치 확인 1회, cwally-port, state 안 씀)
"""
from __future__ import annotations

import json
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
CFG_PATH = ROOT / "configs" / "polyinv.json"
STATE_PATH = ROOT / "state" / "polyinv.json"
UA = {"User-Agent": "Mozilla/5.0 cwally-alerts polyinv"}
PREFIX = "[POLYINV]"
MACRO_NONE = "   거시 고정 항목 없음 (조건 충족 시장 없음)"


# ---------- io ----------
def load_state() -> dict:
    base = {"weekly_sent": None, "markets": {}}
    try:
        base.update(json.loads(STATE_PATH.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        pass
    return base


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")


def get_json(url: str):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=25) as resp:
        return json.loads(resp.read().decode("utf-8", errors="ignore"))


def fetch_events(cfg: dict) -> list[dict]:
    src = cfg["source"]
    out, seen = [], set()
    for page in range(int(src.get("pages", 5))):
        q = urllib.parse.urlencode({
            "active": "true", "closed": "false", "order": src.get("order", "volume1wk"),
            "ascending": "false", "limit": str(src.get("page_size", 100)),
            "offset": str(page * int(src.get("page_size", 100))),
        })
        data = get_json(f"{src['gamma_events']}?{q}")
        if isinstance(data, dict):
            data = data.get("events") or data.get("data") or []
        if not isinstance(data, list):
            raise ValueError("events 응답 형식 아님")
        for ev in data:
            if ev.get("id") not in seen:
                seen.add(ev.get("id"))
                out.append(ev)
        if len(data) < int(src.get("page_size", 100)):
            break
    # 거래대금 상위 500 밖의 작은 거시 시장(CPI, 경기침체, 관세, 일본/한국)을 태그로 따로 가져옴
    slugs = list(src.get("extra_tag_slugs") or [])
    slugs += [t for t in (cfg.get("macro_pin") or {}).get("tag_slugs", []) if t not in slugs]
    for slug in slugs:
        q = urllib.parse.urlencode({
            "active": "true", "closed": "false", "order": src.get("order", "volume1wk"),
            "ascending": "false", "limit": str(src.get("extra_limit", 30)), "tag_slug": slug,
        })
        try:
            data = get_json(f"{src['gamma_events']}?{q}")
        except Exception as exc:  # 보조 조회 실패는 본 목록에 영향 없음
            print(f"polyinv extra tag {slug} fail: {type(exc).__name__}", file=sys.stderr)
            continue
        for ev in data if isinstance(data, list) else []:
            if ev.get("id") not in seen:
                seen.add(ev.get("id"))
                out.append(ev)
    if not out:
        raise ValueError("events 0건")
    return out


# ---------- helpers ----------
def fnum(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def yes_prob(m: dict) -> float | None:
    prices = m.get("outcomePrices")
    if isinstance(prices, str):
        try:
            prices = json.loads(prices)
        except json.JSONDecodeError:
            return None
    if not isinstance(prices, list) or not prices:
        return None
    return fnum(prices[0])


def has_kw(text: str, kw: str) -> bool:
    return re.search(r"(?<![a-z0-9])" + re.escape(kw.lower()) + r"(?![a-z0-9])", text) is not None


def parse_iso(s) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None


def fmt_vol(v) -> str:
    n = fnum(v) or 0.0
    if n >= 1_000_000:
        return f"${n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"${n/1_000:.0f}K"
    return f"${n:.0f}"


def fmt_pp(pp: float) -> str:
    return f"{pp:+.1f}%p"


def classify(ev: dict, cfg: dict) -> dict | None:
    title = (ev.get("title") or "").lower()
    tags = {str(t.get("slug") or "") for t in ev.get("tags") or [] if isinstance(t, dict)}
    tags_l = {t.lower() for t in tags}
    if tags_l & {t.lower() for t in cfg["deny_tags"]}:
        return None
    if any(has_kw(title, k) for k in cfg["deny_keywords"]):
        return None
    if tags_l & {t.lower() for t in cfg["election_tags"]}:
        if not any(has_kw(title, k) for k in cfg["election_allow_keywords"]):
            return None
    for cat in cfg["categories"]:
        if any(has_kw(title, k) for k in cat["keywords"]):
            return cat
    for cat in cfg["categories"]:
        if tags_l & {t.lower() for t in cat["tags"]}:
            return cat
    return None


def ko_date(text: str, cfg: dict) -> str | None:
    m = re.match(r"^\s*([A-Za-z]+)\s+(\d{1,2})(?:,\s*(\d{4}))?\s*\??$", text or "")
    if m and m.group(1).lower() in cfg["months"]:
        return f"{cfg['months'][m.group(1).lower()]}/{int(m.group(2))}"
    return None


def ko_title(title: str, cfg: dict, cat: dict) -> str:
    low = title.lower().strip()
    for pat, tmpl in cfg["ko_titles"]:
        m = re.search(pat, low)
        if not m:
            continue
        out = tmpl
        for i, g in enumerate(m.groups(), start=1):
            g = g or ""
            mon = cfg["months"].get(g.lower())
            out = out.replace(f"{{m{i}}}", f"{mon}월" if mon else g)
            out = out.replace(f"{{d{i}}}", ko_date(g, cfg) or g)
            out = out.replace(f"{{{i}}}", g)
        return out
    short = re.sub(r"\s+", " ", title).strip()
    if len(short) > 48:
        short = short[:47].rstrip() + ".."
    return f"{cat['label']}: {short}"


def ko_outcome(label: str, cfg: dict) -> str:
    raw = (label or "").strip()
    if not raw:
        return ""
    d = ko_date(raw, cfg)
    if d:
        return f"{d}까지"
    low = raw.lower()
    for pat, tmpl in cfg["ko_words"]:
        m = re.search(pat, low)
        if m:
            out = tmpl
            for i, g in enumerate(m.groups(), start=1):
                out = out.replace(f"{{{i}}}", g or "")
            return out
    return raw[:30]


def weekly_change(mid: str, p: float, m: dict, state: dict, now: datetime, th: dict) -> tuple[float | None, str]:
    """(change in %p or None, basis) basis: 'state' | 'gamma' | 'MM/DD' | 'new'."""
    rec = (state.get("markets") or {}).get(mid)
    age = None
    if rec and rec.get("at"):
        at = parse_iso(rec["at"])
        if at:
            age = (now - at).total_seconds() / 86400
    if rec and age is not None and th["baseline_min_days"] <= age <= th["baseline_max_days"]:
        return (p - float(rec["p"])) * 100, "state"
    g = fnum(m.get("oneWeekPriceChange"))
    if g is not None:
        return g * 100, "gamma"
    if rec and age is not None and age < th["baseline_min_days"]:
        return (p - float(rec["p"])) * 100, parse_iso(rec["at"]).astimezone(KST).strftime("%m/%d")
    return None, "new"


def open_markets(ev: dict, th: dict, now: datetime, min_days: float | None = None) -> list[dict]:
    out = []
    for m in ev.get("markets") or []:
        if m.get("closed") or m.get("active") is False or m.get("acceptingOrders") is False:
            continue
        p = yes_prob(m)
        if p is None:
            continue
        if (fnum(m.get("liquidityNum")) or fnum(m.get("liquidity")) or 0) < th["min_market_liquidity"]:
            continue
        end = parse_iso(m.get("endDate")) or parse_iso(ev.get("endDate"))
        if end and (end - now).total_seconds() < (th["min_days_to_end"] if min_days is None else min_days) * 86400:
            continue
        out.append(dict(m, _p=p, _end=end))
    return out


def is_fed_event(ev: dict, cat: dict, cfg: dict) -> bool:
    pin = cfg.get("fed_pin") or {}
    if not pin or cat.get("key") != pin.get("category", "rates"):
        return False
    title = (ev.get("title") or "").lower()
    return any(has_kw(title, k) for k in pin.get("keywords", []))


def macro_ko_title(ev: dict, cfg: dict, cat: dict, us: bool) -> str:
    """거시 고정 항목의 한글 제목. ko_titles 에 없으면 키워드로 짧은 한글 제목을 만듦."""
    title = ev.get("title") or ""
    ko = ko_title(title, cfg, cat)
    if not ko.startswith(f"{cat['label']}: "):
        return ko
    low = title.lower()
    word = next((k for kw, k in (cfg.get("macro_pin") or {}).get("generic_ko", []) if has_kw(low, kw)), "거시 지표")
    year = re.search(r"(20\d\d)", low)
    return ("미국 " if us else "") + word + (f" {year.group(1)}년" if year else "")


def pick_macro_pin(events: list[dict], cfg: dict, state: dict, now: datetime,
                   snap: dict | None = None) -> dict | None:
    """작은 거시 시장 1개 (macro_pin). 조건 충족 후보가 없으면 None. snap 에 후보 시장 확률을 기록."""
    snap = {} if snap is None else snap
    mp = cfg.get("macro_pin") or {}
    if not mp:
        return None
    th = cfg["thresholds"]
    lo, hi = float(th["drop_low"]), float(th["drop_high"])
    min_liq = float(mp.get("min_market_liquidity", th["min_market_liquidity"]))
    loose = dict(th, min_market_liquidity=0)
    cands = []
    for ev in events:
        cat = classify(ev, cfg)
        title = (ev.get("title") or "").lower()
        if not cat or cat["key"] not in mp.get("categories", ["inflation", "macro"]):
            continue
        if not any(has_kw(title, k) for k in mp.get("keywords", [])):
            continue
        us = not any(has_kw(title, k) for k in mp.get("non_us_keywords", []))
        allm = open_markets(ev, loose, now, float(mp.get("min_days_to_end", th["min_days_to_end"])))
        if not allm:
            continue
        neg = bool(ev.get("negRisk") or ev.get("enableNegRisk"))
        liq = lambda m: fnum(m.get("liquidityNum")) or fnum(m.get("liquidity")) or 0  # noqa: E731
        if neg and max(m["_p"] for m in allm) >= hi:
            continue                                  # 상호배타 1위가 97% 이상: 사실상 결정
        for relaxed in (False, True):
            ok = [m for m in allm if lo < m["_p"] < hi and (relaxed or liq(m) >= min_liq)]
            if not ok:
                continue
            rows = []
            for m in allm:                            # 거시 후보 시장 전체 기록 (다음 주 저장값 비교용)
                snap[str(m.get("id"))] = {"p": round(m["_p"], 4), "at": now.isoformat(timespec="minutes")}
            for m in ok:
                ch, basis = weekly_change(str(m.get("id")), m["_p"], m, state, now, th)
                rows.append({"m": m, "ch": ch, "basis": basis})
            lead = max(rows, key=lambda r: (r["m"]["_p"], liq(r["m"]))) if neg else \
                max(rows, key=lambda r: (abs(r["ch"] or 0), liq(r["m"])))
            cands.append({"ev": ev, "cat": cat, "us": us, "relaxed": relaxed, "lead": lead,
                          "vol": fnum(ev.get("volume1wk")) or 0})
            break
    if not cands:
        return None
    best = sorted(cands, key=lambda c: (c["relaxed"], not c["us"], -c["vol"]))[0]
    ev, cat, m = best["ev"], best["cat"], best["lead"]["m"]
    title_ko = macro_ko_title(ev, cfg, cat, best["us"])
    return {
        "cat": cat, "title_ko": title_ko,
        "outcome": ko_outcome(m.get("groupItemTitle") or "", cfg) if len(ev.get("markets") or []) > 1 else "",
        "p": m["_p"], "ch": best["lead"]["ch"], "basis": best["lead"]["basis"],
        "vol": best["vol"], "end": m["_end"],
        "link": cfg["source"]["event_link"] + str(ev.get("slug") or ""),
        "event_id": str(ev.get("id")), "market_id": str(m.get("id")),
        "title_en": ev.get("title") or "", "pinned": True, "macro_pin": True, "settled_shown": False,
        "relaxed_liquidity": best["relaxed"], "liq": fnum(m.get("liquidityNum")) or fnum(m.get("liquidity")) or 0,
        "group": dedupe_key(ev.get("title") or "", title_ko, cfg), "decision": False,
    }


def build_items(events: list[dict], cfg: dict, state: dict, now: datetime) -> tuple[list[dict], dict]:
    th = cfg["thresholds"]
    pin = cfg.get("fed_pin") or {}
    lo, hi = float(th["drop_low"]), float(th["drop_high"])
    items, fed_ok, fed_settled, snapshot = [], [], [], {}
    for ev in events:
        cat = classify(ev, cfg)
        if not cat:
            continue
        fed = is_fed_event(ev, cat, cfg)
        opened = open_markets(ev, th, now, 0 if fed else None)
        # 거래대금 기준 미달 이벤트도 확률은 기록: 다음 주 기준을 넘으면 저장값(지난주 대비)으로 비교
        for m in opened:
            snapshot[str(m.get("id"))] = {"p": round(m["_p"], 4), "at": now.isoformat(timespec="minutes")}
        if (fnum(ev.get("volume1wk")) or 0) < cat.get("min_event_volume_1wk", th["min_event_volume_1wk"]):
            continue
        allm = []
        for m in opened:
            mid = str(m.get("id"))
            p = m["_p"]
            ch, basis = weekly_change(mid, p, m, state, now, th)
            allm.append({"m": m, "p": p, "ch": ch, "basis": basis})
        if not allm:
            continue
        liq = lambda c: fnum(c["m"].get("liquidityNum")) or 0  # noqa: E731
        neg = bool(ev.get("negRisk") or ev.get("enableNegRisk"))
        top = max(allm, key=lambda c: (c["p"], liq(c)))
        settled = None
        if neg and top["p"] >= hi:
            settled, cands = top, []            # mutually exclusive leader >= 97%: whole event dropped
        else:
            cands = [c for c in allm if lo < c["p"] < hi]
            if not cands:
                settled = top
        if cands:
            lead = max(cands, key=lambda c: (c["p"], liq(c))) if neg else \
                max(cands, key=lambda c: (abs(c["ch"] or 0), liq(c)))
        m = (lead if cands else settled)["m"]
        chosen = lead if cands else settled
        multi = len(ev.get("markets") or []) > 1
        it = {
            "cat": cat, "title_ko": ko_title(ev.get("title") or m.get("question") or "?", cfg, cat),
            "outcome": ko_outcome(m.get("groupItemTitle") or "", cfg) if multi else "",
            "p": chosen["p"], "ch": chosen["ch"], "basis": chosen["basis"],
            "vol": fnum(ev.get("volume1wk")) or 0, "end": m["_end"],
            "link": cfg["source"]["event_link"] + str(ev.get("slug") or ""),
            "event_id": str(ev.get("id")), "market_id": str(m.get("id")),
            "title_en": ev.get("title") or "", "pinned": False, "settled_shown": False,
            "group": dedupe_key(ev.get("title") or "", ko_title(ev.get("title") or "", cfg, cat), cfg),
            "decision": any(has_kw((ev.get("title") or "").lower(), k) for k in pin.get("decision_keywords", [])),
        }
        if fed:
            (fed_ok if cands else fed_settled).append(it)
        elif cands:
            items.append(it)
    far = datetime.max.replace(tzinfo=KST)
    pinned = sorted(fed_ok, key=lambda it: (0 if it["decision"] else 1, it["end"] or far if it["decision"] else far,
                                           -abs(it["ch"] or 0), -it["vol"]))[: int(pin.get("max_items", 3))]
    if pin and not pinned and fed_settled:
        nxt = sorted(fed_settled, key=lambda it: (0 if it["decision"] else 1, it["end"] or far, -it["vol"]))[0]
        nxt["settled_shown"] = True
        pinned = [nxt]
    for it in pinned:
        it["pinned"] = True
    macro = pick_macro_pin(events, cfg, state, now, snapshot)
    if macro:
        snapshot[macro["market_id"]] = {"p": round(macro["p"], 4), "at": now.isoformat(timespec="minutes")}
        pinned = pinned + [macro]
        items = [it for it in items if it["event_id"] != macro["event_id"] and it["group"] != macro["group"]]
    items = dedupe_items(items)
    # 표시 단위(0.1%p)로 반올림해 정렬: 부동소수 오차로 같은 변화폭의 거래대금 순서가 뒤집히지 않게
    items.sort(key=lambda it: (-(round(abs(it["ch"]), 1) if it["ch"] is not None else -1), -it["vol"]))
    picked, per_cat = list(pinned), {}
    for it in items:
        if len(picked) >= th["max_items"]:
            break
        k = it["cat"]["key"]
        if per_cat.get(k, 0) >= int(it["cat"].get("max_items", th["max_per_category"])):
            continue
        per_cat[k] = per_cat.get(k, 0) + 1
        picked.append(it)
    return picked, snapshot


def dedupe_key(title_en: str, title_ko: str, cfg: dict) -> str:
    """같은 사건의 다른 기한/창(by Oct 31, by Dec 31 등)을 한 묶음으로. config dedupe_groups 먼저, 없으면 한글 제목에서 괄호 기한을 뺀 값."""
    low = (title_en or "").lower()
    for pat, key in cfg.get("dedupe_groups") or []:
        if re.search(pat, low):
            return key
    return re.sub(r"\s*\([^)]*\)\s*$", "", title_ko).strip()


def dedupe_items(items: list[dict]) -> list[dict]:
    """묶음마다 1주 거래대금이 가장 큰 이벤트 하나만 남김 (얇은 기한 시장의 시간 감소를 변화로 보지 않게)."""
    best: dict[str, dict] = {}
    for it in items:
        cur = best.get(it["group"])
        if cur is None or it["vol"] > cur["vol"]:
            best[it["group"]] = it
    keep = {id(v) for v in best.values()}
    return [it for it in items if id(it) in keep]


def item_lines(i: int, it: dict) -> list[str]:
    name = it["title_ko"] + (f" [{it['outcome']}]" if it["outcome"] else "")
    if it.get("pinned"):
        name += " " + str(it.get("pin_label") or "(고정 표시)")
        if it.get("settled_shown"):
            name += ", 97% 이상이지만 다른 연준 시장이 없어 표시"
        if it.get("relaxed_liquidity"):
            name += f", 유동성 {fmt_vol(it.get('liq'))}로 기준 $5K 미만이지만 조건 맞는 거시 시장이 없어 표시"
    if it["ch"] is None:
        chs = "신규"
    elif it["basis"] == "state":
        chs = f"지난주 대비 {fmt_pp(it['ch'])}"
    elif it["basis"] == "gamma":
        chs = f"1주 {fmt_pp(it['ch'])}*"
    else:
        chs = f"{it['basis']} 대비 {fmt_pp(it['ch'])}"
    end = it["end"].astimezone(KST).strftime("%Y-%m-%d") if it["end"] else "미정"
    return [
        f"{i}. [{it['cat']['label']}] {name}",
        f"   예 {it['p']*100:.1f}% ({chs}), 1주 거래 {fmt_vol(it['vol'])}, 마감 {end}",
        f"   관련 자산: {it['cat']['assets']}",
        f"   {it['link']}",
    ]


def render(items: list[dict], now: datetime, cfg: dict) -> tuple[str, list[str]]:
    th = cfg["thresholds"]
    pin_label = (cfg.get("fed_pin") or {}).get("label", "(고정 표시)")
    for it in items:
        it["pin_label"] = pin_label
    ranked = [it for it in items if not it.get("pinned")] or items
    if items:
        top = max(ranked, key=lambda it: abs(it["ch"]) if it["ch"] is not None else -1)
        hl = f"{top['title_ko'][:22]} {fmt_pp(top['ch'])}" if top["ch"] is not None else f"{len(items)}개"
        title = f"{PREFIX} 주간 확률 변화 {len(items)}개 | 가장 큰 변화: {hl} | 참고 지표"
    else:
        title = f"{PREFIX} 이번 주 기준 넘는 항목 없음 | 참고 지표"
    head = [
        f"{now.strftime('%Y-%m-%d %H:%M')} KST | 투자자용 폴리마켓 주간",
        "1) 이게 뭐 알림: 금리, 물가, 경기, 관세, 지정학, 미국 정치/재정, 가상자산, 빅테크 관련 폴리마켓 확률의 1주 변화. 확률은 참고 지표, 매매 신호 아님.",
        ("2) 지금 결과 (고정 표시 먼저: 연준, 거시 1개. 나머지는 |변화| 큰 순):" if any(it.get("pinned") for it in items)
         else "2) 지금 결과 (|변화| 큰 순):") if items else "2) 지금 결과: 거래대금/유동성 기준을 넘고 의미 있게 움직인 투자 관련 이벤트가 없음.",
    ]
    body_items = [ln for i, it in enumerate(items, start=1) for ln in item_lines(i, it)]
    foot = []
    if cfg.get("macro_pin") and not any(it.get("macro_pin") for it in items):
        foot.append(MACRO_NONE)
    if any(it["basis"] == "gamma" for it in items):
        foot.append("   * = 폴리마켓이 계산한 7일 변화 (이 알림의 지난주 기록이 아직 없는 시장)")
    foot += [
        "3) 지금 할 일: 크게 움직인 항목의 관련 자산 뉴스만 확인. 포지션 변경 없음.",
        "4) 나중에: 다음 일요일 21:00 KST 에 다시 비교. 같은 방향으로 2주 연속 움직이면 그때 관련 알림을 다시 봄.",
        "5) 익절/줄일 때: 적용 없음.",
        f"6) 하지 말 것: 확률만 보고 사거나 팔지 말 것. 거래 적은 시장(1주 {fmt_vol(th['min_event_volume_1wk'])} 미만{low_floor_note(cfg)})과 "
        f"{th['drop_low']*100:.0f}% 이하 또는 {th['drop_high']*100:.0f}% 이상(사실상 결정)은 뺐음.",
    ]
    return title, split_parts(head, body_items, foot, int(th["message_max_bytes"]))


def low_floor_note(cfg: dict) -> str:
    low = [c for c in cfg["categories"] if "min_event_volume_1wk" in c]
    if not low:
        return ""
    return f", {'/'.join(c['label'] for c in low)} 분류는 {fmt_vol(min(c['min_event_volume_1wk'] for c in low))} 미만"


def split_parts(head: list[str], items: list[str], foot: list[str], limit: int) -> list[str]:
    def size(lines):
        return len("\n".join(lines).encode("utf-8"))
    whole = head + items + foot
    if size(whole) <= limit:
        return ["\n".join(whole)]
    # split on item boundaries (4 lines per item); first part keeps head, last part keeps foot
    blocks = [items[i:i + 4] for i in range(0, len(items), 4)]
    parts, cur = [], list(head)
    for b in blocks:
        if size(cur + b) > limit and len(cur) > len(head if not parts else []):
            parts.append(cur)
            cur = ["2) 지금 결과 (이어서):"]
        cur += b
    if size(cur + foot) > limit:
        parts.append(cur)
        cur = []
    parts.append(cur + foot)
    return ["\n".join(p) for p in parts]


def send_parts(cfg: dict, title: str, parts: list[str], priority: int = 3) -> None:
    n = len(parts)
    for i, body in enumerate(parts, start=1):
        t = title if n == 1 else f"{title} ({i}/{n})"
        publish(resolve_topic("port", cfg.get("ntfy_topic")), t, body, priority, ["bar_chart"],
                resolve_server(cfg.get("ntfy_server")))


def week_key(now: datetime) -> str | None:
    """Sunday date of the slot this run belongs to, or None when outside the window."""
    if now.weekday() == 6 and now.hour >= 20:
        return now.strftime("%Y-%m-%d")
    if now.weekday() == 0 and now.hour < 12:
        return (now - timedelta(days=1)).strftime("%Y-%m-%d")
    return None


def install_body() -> str:
    return "\n".join([
        "1) 이게 뭐 알림: 투자자용 주간 폴리마켓 확률 변화 알림 설치 확인. 실제 결과 아님.",
        "2) 지금 결과/상태: 일요일 21:00 KST portlight 슬롯에 붙음. 금리, 물가, 경기, 관세, 지정학, 미국 정치/재정, 가상자산, 빅테크 이벤트만 최대 10개.",
        "3) 지금 할 일: 없음. 첫 발송은 2026-10-25(일) 21:00 KST.",
        "4) 나중에: 매주 지난주 대비 확률 변화(%p), 1주 거래대금, 마감일, 관련 자산, 링크를 보냄.",
        "5) 익절/줄일 때: 적용 없음.",
        "6) 하지 말 것: 확률은 참고 지표, 매매 신호 아님. 이 메시지로 사거나 팔지 말 것.",
    ])


def selftest(cfg: dict) -> int:
    """오프라인 점검: 한글 제목, 분류, 차단, 중복 묶음, 분류 한도, 금지 문장부호."""
    ok = True

    def check(name, got, want):
        nonlocal ok
        if got != want:
            ok = False
            print(f"FAIL {name}: got {got!r} want {want!r}")

    cat = {c["key"]: c for c in cfg["categories"]}

    def ev(title, tags=()):
        return {"title": title, "tags": [{"slug": t} for t in tags]}

    def ck(e):
        c = classify(e, cfg)
        return c["key"] if c else None

    check("ko xrp", ko_title("What price will XRP hit in October?", cfg, cat["crypto"]), "XRP 10월 도달 가격")
    check("ko btc year", ko_title("What price will Bitcoin hit in 2026?", cfg, cat["crypto"]), "비트코인 2026년 도달 가격")
    check("ko boj", ko_title("Bank of Japan Decision in October?", cfg, cat["asia"]), "일본은행 10월 금리 결정")
    check("ko usdkrw", ko_title("Will USD/KRW hit __ in 2026?", cfg, cat["asia"]), "2026 원달러 환율 도달 구간")
    check("ko fed", ko_title("Fed Decision in December?", cfg, cat["rates"]), "연준 12월 금리 결정")
    check("deny diesel", ck(ev("US imports Russian diesel by...?", ["oil"])), None)
    check("deny yemen daily", ck(ev("Saudi Arabia military action against Yemen on...?", ["geopolitics"])), None)
    check("deny ukraine map", ck(ev("Will Russia capture all of Kupiansk by...?", ["russia-capture", "russia"])), None)
    check("deny crypto weekly", ck(ev("Bitcoin above ___ on October 26?", ["bitcoin", "weekly"])), None)
    check("deny foreign cpi", ck(ev("Argentina Monthly Inflation - October", ["inflation"])), None)
    check("keep usdkrw", ck(ev("Will USD/KRW hit __ in 2026?", ["south-korea"])), "asia")
    check("keep boj", ck(ev("Bank of Japan Decision in October?", ["japan", "boj"])), "asia")
    check("keep core pce", ck(ev("Core PCE MoM - September 2026", ["inflation"])), "inflation")
    check("keep hormuz", ck(ev("Strait of Hormuz traffic returns to normal by December 31?", ["hormuz"])), "commod")
    check("cap crypto", int(cat["crypto"].get("max_items", 99)) <= 2, True)
    k1 = dedupe_key("Strait of Hormuz traffic returns to normal by October 31?",
                    ko_title("Strait of Hormuz traffic returns to normal by October 31?", cfg, cat["commod"]), cfg)
    k2 = dedupe_key("Strait of Hormuz traffic returns to normal by December 31?",
                    ko_title("Strait of Hormuz traffic returns to normal by December 31?", cfg, cat["commod"]), cfg)
    check("dedupe hormuz windows", k1 == k2, True)
    k3 = dedupe_key("Russia x Ukraine peace talks by...?", ko_title("Russia x Ukraine peace talks by...?", cfg, cat["geo"]), cfg)
    k4 = dedupe_key("Russia x Ukraine any diplomatic meeting by...?",
                    ko_title("Russia x Ukraine any diplomatic meeting by...?", cfg, cat["geo"]), cfg)
    check("dedupe ru-ua talks", k3 == k4, True)
    k5 = dedupe_key("What price will Bitcoin hit in October?", "", cfg)
    k6 = dedupe_key("What price will Bitcoin hit in 2026?", "", cfg)
    check("dedupe btc ladders", k5 == k6, True)
    items = [{"group": "a", "vol": 1}, {"group": "a", "vol": 5}, {"group": "b", "vol": 2}]
    check("dedupe keeps biggest", [it["vol"] for it in dedupe_items(items)], [5, 2])
    # 거시 고정(macro_pin)
    now = datetime(2026, 10, 25, 21, 0, tzinfo=KST)

    def mev(eid, title, p, liq, vol, days, tags=("economy",), neg=False, mid=None, extra=None):
        mk = [{"id": mid or f"m{eid}", "outcomePrices": json.dumps([str(p), str(1 - p)]), "liquidityNum": liq,
               "endDate": (now + timedelta(days=days)).isoformat(), "oneWeekPriceChange": 0.012}]
        return {"id": eid, "title": title, "slug": f"s{eid}", "tags": [{"slug": t} for t in tags],
                "volume1wk": vol, "negRisk": neg, "markets": mk + (extra or [])}

    rec = mev("1", "US recession by end of 2026?", 0.085, 69000, 46000, 96)
    une = mev("2", "How high will US unemployment go in 2026?", 0.106, 5100, 13000, 98, ("unemployment",))
    chn = mev("3", "China GDP growth (Y/Y) in Q4 2026?", 0.30, 9000, 90000, 98, ("gdp",))
    hot = mev("4", "Core PCE MoM - September 2026", 0.40, 9000, 99000, 2, ("inflation",))
    dead = mev("5", "Negative GDP growth in 2026?", 0.99, 9000, 98000, 94, ("gdp",))
    thin = mev("6", "October Inflation US - Monthly", 0.39, 2700, 900, 16, ("cpi",))
    fed = mev("7", "Fed Decision in December?", 0.765, 170000, 735000, 44, ("fed",))
    pin = pick_macro_pin([une, chn, hot, dead, rec, thin], cfg, {}, now)
    check("macro pick us recession", pin and pin["event_id"], "1")
    check("macro ko title", pin and pin["title_ko"], "미국 경기침체 2026년")
    check("macro strict liquidity", pin and pin["relaxed_liquidity"], False)
    check("macro weekly change shown", pin and (pin["basis"], round(pin["ch"], 1)), ("gamma", 1.2))
    check("macro skip settled/short", pick_macro_pin([hot, dead], cfg, {}, now), None)
    relax = pick_macro_pin([thin, hot], cfg, {}, now)
    check("macro relaxed liquidity", relax and (relax["event_id"], relax["relaxed_liquidity"]), ("6", True))
    check("macro relaxed ko title", relax and relax["title_ko"], "미국 CPI 전월비 (10월)")
    lines = item_lines(1, dict(relax, pin_label="(고정 표시)")) if relax else []
    check("macro relaxed note", bool(lines) and "기준 $5K 미만" in lines[0], True)
    check("macro us first", pick_macro_pin([chn, une], cfg, {}, now)["event_id"], "2")
    picked, snap = build_items([fed, rec, une, chn], cfg, {}, now)
    check("fed pin stays first", [it["event_id"] for it in picked][:2], ["7", "1"])
    check("macro not duplicated", [it["event_id"] for it in picked].count("1"), 1)
    check("macro in snapshot", "m1" in snap, True)
    check("cap", len(picked) <= cfg["thresholds"]["max_items"], True)
    _, parts = render(picked, now, cfg)
    check("macro label", "미국 경기침체 2026년 (고정 표시)" in parts[0], True)
    check("macro none line absent", MACRO_NONE in "\n".join(parts), False)
    _, parts = render([], now, cfg)
    check("macro none line", MACRO_NONE in "\n".join(parts), True)
    blob = json.dumps(cfg, ensure_ascii=False) + Path(__file__).read_text(encoding="utf-8")
    for ch in ("\u00b7", "\u2014"):
        check(f"no forbidden char U+{ord(ch):04X}", ch in blob, False)
    print("polyinv selftest " + ("OK" if ok else "FAILED"))
    return 0 if ok else 1


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    state = load_state()

    if mode == "install":
        publish(resolve_topic("port", cfg.get("ntfy_topic")), f"{PREFIX} 설치 확인 | 매매 신호 아님",
                install_body(), 2, ["white_check_mark"], resolve_server(cfg.get("ntfy_server")))
        return 0

    wk = week_key(now)
    if mode == "auto":
        if wk is None:
            print("polyinv silent: weekly slot is Sunday 21:00 KST")
            return 0
        if state.get("weekly_sent") == wk:
            print(f"polyinv already sent for week {wk}")
            return 0
    elif mode == "selftest":
        return selftest(cfg)
    elif mode not in {"force", "dry", "seed"}:
        print(f"unknown mode {mode}")
        return 2

    try:
        events = fetch_events(cfg)
    except Exception as exc:  # fetch failure -> cwally-ops via route_topic (title has 조회 실패)
        msg = f"{type(exc).__name__}: {exc}"[:200]
        print(f"polyinv fetch fail: {msg}", file=sys.stderr)
        if mode in {"dry", "seed"}:
            return 1
        publish(resolve_topic("port", cfg.get("ntfy_topic")), f"{PREFIX} 조회 실패 | 신호 미적용",
                "\n".join([
                    "1) 이게 뭐 알림: 투자자용 주간 폴리마켓 알림의 데이터 조회 실패. 매매 신호 아님.",
                    f"2) 지금 결과/상태: Gamma API events 조회 실패 ({msg}). 이번 주 확률 변화는 확인 불가.",
                    "3) 지금 할 일: 없음. 다음 실행에서 다시 시도.",
                    "4) 나중에: 2주 연속 실패면 Gamma API 주소/형식 확인.",
                    "5) 익절/줄일 때: 적용 없음.",
                    "6) 하지 말 것: 확인 불가를 0이나 정상으로 읽지 말 것.",
                ]), 3, ["warning"], resolve_server(cfg.get("ntfy_server")))
        return 1

    items, snapshot = build_items(events, cfg, state, now)

    if mode == "seed":
        state["markets"] = {**state.get("markets", {}), **snapshot}
        state["seeded_at"] = now.isoformat(timespec="minutes")
        save_state(state)
        print(f"seeded {len(snapshot)} market probabilities; would show {len(items)} items")
        return 0

    title, parts = render(items, now, cfg)
    if mode == "dry":
        print("DRY RUN: no publish, state not written")
        print(f"events fetched={len(events)} markets tracked={len(snapshot)} items={len(items)} parts={len(parts)}")
        for i, body in enumerate(parts, start=1):
            t = title if len(parts) == 1 else f"{title} ({i}/{len(parts)})"
            print(f"--- title: {t}  [{len(body.encode('utf-8'))} bytes]")
            print(body)
        return 0

    send_parts(cfg, title, parts)
    keep = {k: v for k, v in state.get("markets", {}).items()
            if (parse_iso(v.get("at")) or now) > now - timedelta(days=60)}
    keep.update(snapshot)
    state.update({"markets": keep, "weekly_sent": wk or now.strftime("%Y-%m-%d"),
                  "last_sent_at": now.isoformat(timespec="minutes"), "last_count": len(items)})
    save_state(state)
    print(title)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
