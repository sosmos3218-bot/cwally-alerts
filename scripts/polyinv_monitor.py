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
주간 변화: state markets[<market id>] = {p, at}. 기준값 나이가 5~10일이면 그 값 대비,
  아니면 Gamma oneWeekPriceChange(폴리마켓 7일 변화, * 표시), 그것도 없으면 더 짧은 기준값(MM/DD 대비), 없으면 신규.
정렬: 연준 고정 먼저, 나머지는 |변화| 큰 순, 같으면 1주 거래대금. 최대 max_items 개, 분류당 max_per_category 개(고정 항목은 분류 한도에 안 셈).

Modes: auto (일요일 20:00 이후 또는 월요일 12:00 KST 전, 주 1회) | force (요일 무시, state 기록) |
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


def build_items(events: list[dict], cfg: dict, state: dict, now: datetime) -> tuple[list[dict], dict]:
    th = cfg["thresholds"]
    pin = cfg.get("fed_pin") or {}
    lo, hi = float(th["drop_low"]), float(th["drop_high"])
    items, fed_ok, fed_settled, snapshot = [], [], [], {}
    for ev in events:
        if (fnum(ev.get("volume1wk")) or 0) < th["min_event_volume_1wk"]:
            continue
        cat = classify(ev, cfg)
        if not cat:
            continue
        fed = is_fed_event(ev, cat, cfg)
        allm = []
        for m in open_markets(ev, th, now, 0 if fed else None):
            mid = str(m.get("id"))
            p = m["_p"]
            snapshot[mid] = {"p": round(p, 4), "at": now.isoformat(timespec="minutes")}
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
    items.sort(key=lambda it: (-(abs(it["ch"]) if it["ch"] is not None else -1), -it["vol"]))
    picked, per_cat = list(pinned), {}
    for it in items:
        if len(picked) >= th["max_items"]:
            break
        k = it["cat"]["key"]
        if per_cat.get(k, 0) >= th["max_per_category"]:
            continue
        per_cat[k] = per_cat.get(k, 0) + 1
        picked.append(it)
    return picked, snapshot


def item_lines(i: int, it: dict) -> list[str]:
    name = it["title_ko"] + (f" [{it['outcome']}]" if it["outcome"] else "")
    if it.get("pinned"):
        name += " " + str(it.get("pin_label") or "(고정 표시)")
        if it.get("settled_shown"):
            name += ", 97% 이상이지만 다른 연준 시장이 없어 표시"
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
        ("2) 지금 결과 (연준 고정 표시 먼저, 나머지는 |변화| 큰 순):" if any(it.get("pinned") for it in items)
         else "2) 지금 결과 (|변화| 큰 순):") if items else "2) 지금 결과: 거래대금/유동성 기준을 넘고 의미 있게 움직인 투자 관련 이벤트가 없음.",
    ]
    body_items = [ln for i, it in enumerate(items, start=1) for ln in item_lines(i, it)]
    foot = []
    if any(it["basis"] == "gamma" for it in items):
        foot.append("   * = 폴리마켓이 계산한 7일 변화 (이 알림의 지난주 기록이 아직 없는 시장)")
    foot += [
        "3) 지금 할 일: 크게 움직인 항목의 관련 자산 뉴스만 확인. 포지션 변경 없음.",
        "4) 나중에: 다음 일요일 21:00 KST 에 다시 비교. 같은 방향으로 2주 연속 움직이면 그때 관련 알림을 다시 봄.",
        "5) 익절/줄일 때: 적용 없음.",
        f"6) 하지 말 것: 확률만 보고 사거나 팔지 말 것. 거래 적은 시장(1주 {fmt_vol(th['min_event_volume_1wk'])} 미만)과 "
        f"{th['drop_low']*100:.0f}% 이하 또는 {th['drop_high']*100:.0f}% 이상(사실상 결정)은 뺐음.",
    ]
    return title, split_parts(head, body_items, foot, int(th["message_max_bytes"]))


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
