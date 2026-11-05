#!/usr/bin/env python3
"""CoreWeave dollar bond stress watch (CDS proxy).

Median price of three CoreWeave USD notes from the TradingView scanner decides the zone:
RECOVER >= 88, WATCH 80~88, DISTRESS < 80, SEVERE < 75 (1pt hysteresis on the way back).
A 3pt drop versus 5 observations ago sends a SHOCK note (5-day cooldown).
US HY OAS / CCC OAS (FRED) are context only; HY alarms belong to burry.

modes: auto (default, morning slot), force (send current state, writes state),
       dry / test (print only, no ntfy, no state), install (one install-confirmation send, no state).
"""
from __future__ import annotations

import json
import statistics
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
CFG_PATH = ROOT / "configs" / "crwvbond.json"
STATE_PATH = ROOT / "state" / "crwvbond.json"
UA = {"User-Agent": "Mozilla/5.0 cwally-alerts crwvbond"}
ZONE_ORDER = ["RECOVER", "WATCH", "DISTRESS", "SEVERE"]
ZONE_KO = {"RECOVER": "회복", "WATCH": "경계", "DISTRESS": "부실권", "SEVERE": "심각"}


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


def fetch_text(url: str, data: bytes | None = None, headers: dict | None = None, timeout: int = 30) -> str:
    h = dict(UA)
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h, method="POST" if data else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def tv_quotes(cfg: dict) -> dict:
    src = cfg["sources"]
    tickers = [b["symbol"] for b in src["bonds"]] + [src["stock"]]
    body = json.dumps({
        "symbols": {"tickers": tickers, "query": {"types": []}},
        "columns": ["name", "close", "yield_to_maturity", "last_bar_update_time", "change"],
    }).encode()
    raw = fetch_text(src["tv_scan"], body, {
        "Content-Type": "application/json",
        "Origin": "https://www.tradingview.com",
        "Referer": "https://www.tradingview.com/",
    })
    out = {}
    for row in json.loads(raw).get("data", []):
        d = row.get("d") or []
        if len(d) < 5:
            continue
        out[row["s"]] = {"close": d[1], "ytm": d[2], "ts": d[3], "chg": d[4]}
    return out


def fred_rows(url: str) -> list[tuple[str, float]]:
    rows = []
    for line in fetch_text(url).splitlines()[1:]:
        parts = line.split(",")
        if len(parts) < 2 or parts[1].strip() in {"", "."}:
            continue
        try:
            rows.append((parts[0].strip(), float(parts[1])))
        except ValueError:
            continue
    return rows


def series_view(rows, lookback_days: int) -> dict:
    if not rows:
        return {"latest": None, "date": None, "chg_bp": None}
    last_d, last_v = rows[-1]
    cut = (datetime.strptime(last_d, "%Y-%m-%d") - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    prev = None
    for d, v in rows:
        if d <= cut:
            prev = v
    chg = None if prev is None else round((last_v - prev) * 100)
    return {"latest": last_v, "date": last_d, "chg_bp": chg}


def zone_of(price: float, prev_zone: str | None, rules: dict) -> str:
    rec, dis, sev = float(rules["recover_price"]), float(rules["distress_price"]), float(rules["severe_price"])
    h = float(rules["hysteresis_pt"])

    def raw(p):
        if p < sev:
            return "SEVERE"
        if p < dis:
            return "DISTRESS"
        if p >= rec:
            return "RECOVER"
        return "WATCH"

    z = raw(price)
    if prev_zone not in ZONE_ORDER or z == prev_zone:
        return z
    # Moving to a better zone (lower index) needs the extra cushion; worse moves are immediate.
    if ZONE_ORDER.index(z) < ZONE_ORDER.index(prev_zone):
        return raw(price - h) if raw(price - h) != prev_zone else prev_zone
    return z


def collect(cfg: dict, now: datetime) -> dict:
    rules = cfg["rules"]
    snap = {"bonds": [], "fails": [], "stock": None, "ust5": None, "hy": {}, "ccc": {}}
    try:
        q = tv_quotes(cfg)
    except Exception as exc:  # noqa: BLE001
        q = {}
        snap["fails"].append(f"트레이딩뷰 {exc}")
    stale_cut = (now - timedelta(days=int(rules["stale_days"]))).timestamp()
    for b in cfg["sources"]["bonds"]:
        r = q.get(b["symbol"])
        if not r or r.get("close") is None:
            snap["fails"].append(f"{b['label']} 시세 없음")
            continue
        if not r.get("ts") or float(r["ts"]) < stale_cut:
            snap["fails"].append(f"{b['label']} 시세 멈춤")
            continue
        snap["bonds"].append({"label": b["label"], "price": float(r["close"]),
                              "ytm": None if r.get("ytm") is None else float(r["ytm"]),
                              "date": datetime.fromtimestamp(float(r["ts"]), KST).strftime("%m-%d %H:%M")})
    s = q.get(cfg["sources"]["stock"])
    if s and s.get("close") is not None:
        snap["stock"] = {"close": float(s["close"]), "chg": s.get("chg")}
    look = int(rules["hy_lookback_days"])
    for key, field in (("ust5_csv", "ust5"), ("hy_oas_csv", "hy"), ("ccc_oas_csv", "ccc")):
        try:
            v = series_view(fred_rows(cfg["sources"][key]), look)
        except Exception as exc:  # noqa: BLE001
            v = {"latest": None, "date": None, "chg_bp": None}
            snap["fails"].append(f"FRED {field} {exc}")
        snap[field] = v
    prices = [b["price"] for b in snap["bonds"]]
    ytms = [b["ytm"] for b in snap["bonds"] if b["ytm"] is not None]
    snap["median_price"] = round(statistics.median(prices), 3) if prices else None
    snap["median_ytm"] = round(statistics.median(ytms), 2) if ytms else None
    u5 = snap["ust5"].get("latest") if snap["ust5"] else None
    snap["spread_bp"] = round((snap["median_ytm"] - u5) * 100) if snap["median_ytm"] is not None and u5 is not None else None
    snap["ok"] = len(prices) >= int(rules["min_bonds"])
    return snap


def fmt(v, nd=1, suf=""):
    return "없음" if v is None else f"{v:.{nd}f}{suf}"


def market_tag(snap: dict, rules: dict) -> str:
    chg = (snap.get("hy") or {}).get("chg_bp")
    if chg is None:
        return "시장 동반 여부 확인불가"
    if chg >= float(rules["hy_market_widen_bp"]):
        return f"시장 동반 확산(하이일드 4주 {chg:+d}bp)"
    return f"코어위브 단독 쪽(하이일드 4주 {chg:+d}bp)"


def status_line(snap: dict, rules: dict) -> str:
    bonds = ", ".join(f"{b['label']} {b['price']:.2f}" for b in snap["bonds"]) or "없음"
    hy, ccc = snap.get("hy") or {}, snap.get("ccc") or {}
    stock = snap.get("stock")
    stock_txt = f"주가 {stock['close']:.2f}달러" if stock else "주가 없음"
    return (
        f"지금 결과/상태: 회사채 중앙값 {fmt(snap['median_price'], 2)}센트, 수익률 {fmt(snap['median_ytm'], 2, '%')}, "
        f"5년 국채 대비 {fmt(snap['spread_bp'], 0, 'bp')}. 개별 {bonds}. {stock_txt}. "
        f"미국 하이일드 OAS {fmt(hy.get('latest'), 2, '%')}, CCC {fmt(ccc.get('latest'), 2, '%')} ({hy.get('date') or '날짜없음'}). "
        f"{market_tag(snap, rules)}."
    )


def build(kind: str, zone: str | None, snap: dict, cfg: dict, stamp: str, drop: float | None = None):
    rules = cfg["rules"]
    intro = ("이게 뭐 알림: 코어위브 달러 회사채 가격으로 AI 인프라 신용 경색을 보는 알림입니다. "
             "CDS(부도 보험료) 대용이며 매수/매도 확정 신호가 아닙니다.")
    head = f"{stamp} | 코어위브 회사채 3종 중앙값"
    later = (f"나중에: 중앙값 {rules['distress_price']:.0f} 아래면 부실권, {rules['severe_price']:.0f} 아래면 심각, "
             f"{rules['recover_price']:.0f} 이상이면 회복으로 바꿉니다. 11월 3분기 실적은 [CRWVQ3]가 따로 봅니다.")
    p = snap.get("median_price")
    if kind == "FAIL":
        title = "[CRWVBOND] 회사채 시세 조회 실패 | 직접 확인"
        lines = [head, intro,
                 f"지금 결과/상태: 쓸 수 있는 회사채 시세 {len(snap['bonds'])}종. 원인 {', '.join(snap['fails'])[:300]}.",
                 "지금 할 일: 트레이딩뷰에서 FWB:USU2069EAB6, USU2069EAC4, USU2069EAE0을 직접 확인하세요.",
                 "나중에: 다음 평일 09:00 KST에 다시 조회합니다. 복구되면 구간 판정을 이어갑니다.",
                 "익절/줄일 때: 이번 알림은 매매 신호가 아닙니다.",
                 "하지 말 것: 조회 실패를 신용 안정이나 매수 신호로 보기."]
        return title, lines, 3
    if kind == "INSTALL":
        title = "[CRWVBOND] CRWV 설치 확인 | 매매 신호 아님"
        lines = [head, intro, status_line(snap, rules),
                 f"지금 할 일: 없음. 현재 판정 {ZONE_KO.get(zone, '확인불가')}. 다음 평일 09:00부터 구간이 바뀔 때만 알립니다.",
                 later,
                 "익절/줄일 때: 설치 확인 메시지라 해당 없음.",
                 "하지 말 것: 이 메시지를 매매 신호로 보기."]
        return title, lines, 2
    if kind == "SHOCK":
        title = f"[CRWVBOND] CRWV 1주 {drop:.1f}포인트 급락 | AI 추격 금지"
        action = "AI 인프라와 반도체 신규 추격을 멈추고, 신용 경색이 시장 전체로 번지는지 하이일드 OAS를 같이 보세요."
        pri = 4
    elif zone == "SEVERE":
        title = f"[CRWVBOND] CRWV 회사채 {p:.1f} 심각 | 레버리지 줄이기"
        action = "10/15 저점(78~80센트) 아래 신저가 구간입니다. AI 인프라 레버리지와 단기 매매 비중을 줄이는 쪽을 검토하세요."
        pri = 5
    elif zone == "DISTRESS":
        title = f"[CRWVBOND] CRWV 회사채 {p:.1f} 부실권 | 신규 추격 금지"
        action = "80센트 아래는 통상 부실 신호입니다. AI 인프라와 반도체 신규 매수는 미루고 보유분은 손절선만 점검하세요."
        pri = 4
    elif zone == "RECOVER":
        title = f"[CRWVBOND] CRWV 회사채 {p:.1f} 회복 | 신용 경색 완화"
        action = "신용 쪽 경고는 풀렸습니다. 외국인 수급 같은 다른 조건이 맞을 때만 분할로 다시 보세요."
        pri = 3
    else:
        title = f"[CRWVBOND] CRWV 회사채 {p:.1f} 경계 | 관망"
        action = "80~88센트 경계 구간입니다. 새 판단 없이 관망하고, 80 이탈 여부만 보세요."
        pri = 3
    if kind == "WEEKLY":
        title = f"[CRWVBOND] CRWV 주간 {p:.1f} {ZONE_KO.get(zone, '')} | 구간 유지"
    lines = [head, intro, status_line(snap, rules), f"지금 할 일: {action}", later,
             "익절/줄일 때: 부실권이나 심각이 나오면 AI 관련 수익 난 포지션부터 일부 줄입니다. 회복이어도 한 번에 되사지 않습니다.",
             "하지 말 것: 회사채 하나의 하루 시세로 부도를 단정하기. 신용, 미수, 물타기."]
    return title, lines, pri


def send(cfg: dict, title: str, lines: list[str], pri: int) -> None:
    publish(resolve_topic("port", cfg.get("ntfy_topic")), title, "\n".join(lines), pri,
            ["chart_with_downwards_trend"], resolve_server(cfg.get("ntfy_server")))


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    dry = mode in {"dry", "test", "dry-run"}
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    rules = cfg["rules"]
    state = load_json(STATE_PATH, {"zone": None, "history": [], "last_kind": None, "shock_sent_date": None,
                                   "weekly_sent_date": None, "fail_active": False, "updated_at": None})
    now = datetime.now(KST)
    today = now.strftime("%Y-%m-%d")
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    snap = collect(cfg, now)
    print(f"{stamp} mode={mode} ok={snap['ok']} median={snap['median_price']} ytm={snap['median_ytm']} "
          f"spread={snap['spread_bp']} fails={snap['fails']}")

    if mode == "install":
        zone = zone_of(snap["median_price"], state.get("zone"), rules) if snap["ok"] else None
        title, lines, pri = build("FAIL" if not snap["ok"] else "INSTALL", zone, snap, cfg, stamp)
        print(title)
        print("\n".join(lines))
        send(cfg, title, lines, pri)
        return 0  # install send never touches state

    if not snap["ok"]:
        kinds = ["FAIL"] if (not state.get("fail_active") or mode in {"force", "dry", "test", "dry-run"}) else []
        for k in kinds:
            title, lines, pri = build(k, None, snap, cfg, stamp)
            print(title)
            print("\n".join(lines))
            if not dry:
                send(cfg, title, lines, pri)
        if not dry:
            state["fail_active"] = True
            state["last_kind"] = "FAIL" if kinds else state.get("last_kind")
            save_state(state, now)
        else:
            print("DRY RUN; no ntfy, no state")
        return 0

    prev_zone = state.get("zone")
    zone = zone_of(snap["median_price"], prev_zone, rules)
    hist = [h for h in (state.get("history") or []) if h.get("date") != today]
    drop = None
    n = int(rules["shock_lookback_obs"])
    if len(hist) >= n:
        drop = round(hist[-n]["price"] - snap["median_price"], 2)
    elif hist:
        drop = round(hist[0]["price"] - snap["median_price"], 2)
    hist.append({"date": today, "price": snap["median_price"], "ytm": snap["median_ytm"], "spread_bp": snap["spread_bp"]})
    hist = hist[-30:]

    kinds = []
    if mode == "force" or dry:
        kinds.append("CHANGE")
    elif prev_zone is None:
        # first live run: baseline only, unless already distressed
        if zone in {"DISTRESS", "SEVERE"}:
            kinds.append("CHANGE")
    elif zone != prev_zone:
        kinds.append("CHANGE")
    shock_ok = True
    last_shock = state.get("shock_sent_date")
    if last_shock:
        shock_ok = (now.date() - datetime.strptime(last_shock, "%Y-%m-%d").date()).days >= int(rules["shock_cooldown_days"])
    if drop is not None and drop >= float(rules["shock_drop_pt"]) and shock_ok and "CHANGE" not in kinds:
        kinds.append("SHOCK")
    if not kinds and now.weekday() == 0 and state.get("weekly_sent_date") != today and prev_zone is not None:
        kinds.append("WEEKLY")
    print(f"zone {prev_zone}->{zone} drop={drop} kinds={kinds}")

    for k in kinds:
        title, lines, pri = build(k, zone, snap, cfg, stamp, drop)
        print(title)
        print("\n".join(lines))
        if not dry:
            send(cfg, title, lines, pri)
            if k == "SHOCK":
                state["shock_sent_date"] = today
            if now.weekday() == 0:
                state["weekly_sent_date"] = today
    if dry:
        print("DRY RUN; no ntfy, no state")
        return 0
    state.update({"zone": zone, "history": hist, "fail_active": False,
                  "last_kind": kinds[-1] if kinds else state.get("last_kind")})
    save_state(state, now)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
