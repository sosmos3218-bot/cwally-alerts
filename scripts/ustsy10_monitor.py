#!/usr/bin/env python3
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
CFG_PATH = ROOT / "configs" / "ustsy10.json"
STATE_PATH = ROOT / "state" / "ustsy10.json"
UA = {"User-Agent": "Mozilla/5.0 cwally-alerts"}


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=25) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def fetch_json(url: str) -> dict:
    return json.loads(fetch(url))


def load_state() -> dict:
    state = {
        "announce_notified": False,
        "result_3y_notified": False,
        "result_10y_notified": False,
        "result_30y_notified": False,
        "fail_notified": False,
        "last_phase": None,
        "updated_at": None,
        "parsed": {},
    }
    if STATE_PATH.exists():
        try:
            state.update(json.loads(STATE_PATH.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass
    return state


def save_state(state: dict, now: datetime) -> None:
    state["updated_at"] = now.isoformat()
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def emit(cfg: dict, title: str, lines: list[str], priority: int) -> None:
    topic = resolve_topic("port", cfg.get("ntfy_topic"))
    server = resolve_server(cfg.get("ntfy_server"))
    publish(topic, title, "\n".join(lines), priority, ["chart_with_upwards_trend"], server)


def api_rows(base: str, filters: str) -> list[dict]:
    params = {
        "filter": filters,
        "page[size]": "100",
        "sort": "-auction_date",
    }
    url = base.rstrip("/") + "?" + urllib.parse.urlencode(params)
    payload = fetch_json(url)
    return payload.get("data") or []


def term_ok(row: dict, needles: list[str]) -> bool:
    term = str(row.get("security_term") or "")
    typ = str(row.get("security_type") or "")
    blob = f"{term} {typ}"
    return any(n.lower() in blob.lower() for n in needles)


def fnum(row: dict, *keys):
    for key in keys:
        raw = row.get(key)
        if raw in (None, "", "."):
            continue
        try:
            return float(str(raw).replace(",", ""))
        except ValueError:
            continue
    return None


def pick_row(rows: list[dict], needles: list[str], auction_date: str):
    hits = [r for r in rows if term_ok(r, needles)]
    dated = [r for r in hits if str(r.get("auction_date") or "")[:10] == auction_date]
    return (dated or hits or [None])[0]


def offering_bn(row: dict | None):
    if not row:
        return None
    amt = fnum(row, "offering_amt", "offering_amount", "orig_offering_amt")
    if amt is None:
        return None
    if amt > 1000:
        return amt / 1_000_000_000
    return amt


def bidder_pct(row: dict | None, kind: str):
    if not row:
        return None
    accepted = fnum(row, "comp_accepted", "competitive_accepted")
    if not accepted:
        return None
    key = {
        "indirect": ("indirect_bidder_accepted", "indirect_accepted"),
        "direct": ("direct_bidder_accepted", "direct_accepted"),
        "dealer": ("primary_dealer_accepted", "primary_dealer_accepted_amt"),
    }[kind]
    val = fnum(row, *key)
    if val is None:
        return None
    return 100.0 * val / accepted


def parse_helious_tail(html: str):
    m = re.search(r"tail[_ \"]*[:=]?\s*([+-]?[0-9.]+)\s*bp", html, re.I)
    if m:
        return float(m.group(1))
    m = re.search(r"([+-][0-9.]+)bp (?:tail|through)", html, re.I)
    if m:
        return float(m.group(1))
    return None


def scrape_tail(url: str):
    try:
        html = fetch(url)
    except Exception as exc:
        print(f"helious fail {url}: {exc}")
        return None
    return parse_helious_tail(html)


def announced(row: dict | None) -> bool:
    if not row:
        return False
    cusip = str(row.get("cusip") or "").strip()
    size = offering_bn(row)
    return bool(cusip) or size is not None


def has_result(row: dict | None) -> bool:
    if not row:
        return False
    return fnum(row, "high_yield", "yield_high", "bid_to_cover_ratio") is not None


def score_long(tenor: str, tail, btc, indirect, avg: dict, thr: dict) -> dict:
    tail_hit = tail is not None and tail >= thr["tail_bp"]
    btc_hit = btc is not None and btc <= avg["btc"] - thr["btc_below_avg"]
    ind_hit = indirect is not None and indirect <= avg["indirect"] - thr["indirect_below_avg_pp"]
    known = [x is not None for x in (tail, btc, indirect)]
    if tenor == "3y":
        return {
            "label": "선행",
            "hits": int(tail_hit) + int(btc is not None and btc < 2.40) + int(indirect is not None and indirect < 60),
            "tail_hit": tail_hit,
            "btc_hit": btc is not None and btc < 2.40,
            "ind_hit": indirect is not None and indirect < 60,
            "missing_tail": tail is None,
        }
    if sum(known) < 2:
        label = "채점불가"
    elif tail_hit and btc_hit and ind_hit:
        label = "복제"
    elif tail is None and btc_hit and ind_hit:
        label = "부분약세"
    elif tail is not None and tail <= 0 and not btc_hit and not ind_hit:
        label = "기각"
    elif (indirect is not None and indirect >= 70) or (tail is not None and tail <= 0):
        label = "기각"
    elif tail_hit or btc_hit or ind_hit:
        label = "부분약세"
    else:
        label = "미복제"
    return {
        "label": label,
        "hits": int(tail_hit) + int(btc_hit) + int(ind_hit),
        "tail_hit": tail_hit,
        "btc_hit": btc_hit,
        "ind_hit": ind_hit,
        "missing_tail": tail is None,
    }


def fmt(v, suffix="", digits=2):
    if v is None:
        return "확인 불가"
    return f"{v:.{digits}f}{suffix}"


def row_line(label: str, row: dict | None, tail, avg=None, thr=None) -> str:
    if not row:
        return f"{label}: 공고/결과 없음"
    cusip = row.get("cusip") or "없음"
    size = offering_bn(row)
    btc = fnum(row, "bid_to_cover_ratio")
    high = fnum(row, "high_yield", "yield_high")
    indirect = bidder_pct(row, "indirect")
    dealer = bidder_pct(row, "dealer")
    wi = "CUSIP 있음=WI 개시 가능" if row.get("cusip") else "미개시"
    extra = ""
    if avg and thr:
        sc = score_long(label, tail, btc, indirect, avg, thr)
        extra = f" / 판정 {sc['label']}"
    return (
        f"{label}: 규모 {fmt(size, 'B')} CUSIP {cusip} WI {wi} "
        f"낙찰 {fmt(high, '%')} 테일 {fmt(tail, 'bp')} BTC {fmt(btc, 'x')} "
        f"간접 {fmt(indirect, '%')} 딜러 {fmt(dealer, '%')}{extra}"
    )


def collect(cfg: dict) -> tuple[dict, bool, str]:
    src = cfg["sources"]
    expect = cfg["expect"]["tenors"]
    err = ""
    rows = []
    upcoming = []
    try:
        rows = api_rows(src["auctions"], "auction_date:gte:2026-09-01,auction_date:lte:2026-10-15")
    except Exception as exc:
        err = f"auctions_query 실패: {exc}"
        print(err)
    try:
        upcoming = api_rows(src["upcoming"], "auction_date:gte:2026-10-01,auction_date:lte:2026-10-15")
    except Exception as exc:
        print(f"upcoming fail: {exc}")
        if not err:
            err = f"upcoming 실패: {exc}"
    pool = upcoming + rows
    out = {}
    tails = {}
    for key, spec in expect.items():
        row = pick_row(pool, spec["term_needles"], spec["auction_date"])
        out[key] = row
        tails[key] = scrape_tail(src[f"helious_{key}"]) if row and has_result(row) else None
    ok = bool(pool)
    return {"rows": out, "tails": tails, "err": err}, ok, err


def phase_for(now: datetime) -> str:
    d = now.strftime("%Y-%m-%d")
    hm = now.strftime("%H:%M")
    if d >= "2026-10-09" and hm >= "02:15":
        return "result_30y"
    if d >= "2026-10-08" and hm >= "02:15":
        return "result_10y"
    if d >= "2026-10-07" and hm >= "02:15":
        return "result_3y"
    if d >= "2026-10-02":
        return "announce"
    if d == "2026-10-01" and hm >= "15:20":
        return "announce"
    return "wait"


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    state = load_state()
    start = datetime.fromisoformat(cfg["window_start_kst"]).replace(tzinfo=KST)
    expire = datetime.fromisoformat(cfg["expire_date"] + "T23:59:59").replace(tzinfo=KST)
    in_window = start <= now <= expire and now.year == int(cfg.get("event_year", 2026))
    print(f"{now.isoformat()} mode={mode} window={in_window}")
    if mode not in {"force", "dry"} and not in_window:
        print("outside 2026 window; silent")
        save_state(state, now)
        return 0

    data, ok, err = collect(cfg)
    rows = data["rows"]
    tails = data["tails"]
    state["parsed"] = {
        k: {
            "cusip": (rows.get(k) or {}).get("cusip"),
            "auction_date": (rows.get(k) or {}).get("auction_date"),
            "offering_bn": offering_bn(rows.get(k)),
            "high_yield": fnum(rows.get(k) or {}, "high_yield", "yield_high"),
            "btc": fnum(rows.get(k) or {}, "bid_to_cover_ratio"),
            "indirect": bidder_pct(rows.get(k), "indirect"),
            "tail": tails.get(k),
        }
        for k in ("3y", "10y", "30y")
    }
    phase = phase_for(now)
    state["last_phase"] = phase
    print(f"phase={phase} ok={ok} err={err}")

    if mode != "dry" and not ok:
        if not state.get("fail_notified"):
            emit(
                cfg,
                "[USTSY] 입찰 조회 실패 | 재시도",
                [
                    f"{now.strftime('%Y-%m-%d %H:%M KST')} | USTSY10 | 10년·30년 입찰",
                    f"이게 뭐 알림: {cfg['explain']}",
                    f"지금 상태: 공식 API 조회 실패. {err}. 약세로 처리하지 않음.",
                    "지금 할 일: 다음 기존 슬롯까지 대기. 포지션 바꾸지 마세요.",
                    "나중에: 공고 또는 결과가 열리면 한 단계만 보냅니다. 만료 2026-10-10.",
                    "익절/줄일 때: 실패 알림으로는 미국채 ETF를 건드리지 마세요.",
                    "하지 말 것: 조회 실패를 수요 붕괴로 읽기.",
                ],
                4,
            )
            state["fail_notified"] = True
        save_state(state, now)
        return 0

    avg = cfg["avg"]
    thr = cfg["thresholds"]
    a10 = announced(rows.get("10y"))
    a30 = announced(rows.get("30y"))
    r3 = has_result(rows.get("3y"))
    r10 = has_result(rows.get("10y"))
    r30 = has_result(rows.get("30y"))
    sc10 = score_long("10y", tails.get("10y"), fnum(rows.get("10y") or {}, "bid_to_cover_ratio"), bidder_pct(rows.get("10y"), "indirect"), avg["10y"], thr)
    sc30 = score_long("30y", tails.get("30y"), fnum(rows.get("30y") or {}, "bid_to_cover_ratio"), bidder_pct(rows.get("30y"), "indirect"), avg["30y"], thr)

    send = False
    kind = "wait"
    flag = None
    title = "[USTSY] 입찰 대기"
    if phase == "announce" and (a10 or a30) and not state.get("announce_notified"):
        send, kind, flag = True, "announce", "announce_notified"
        title = "[USTSY] 10·30년 공고 확인 | 규모·CUSIP 기록"
    elif phase == "result_3y" and r3 and not state.get("result_3y_notified"):
        send, kind, flag = True, "result_3y", "result_3y_notified"
        title = "[USTSY] 3년 결과 | 10년 선행"
    elif phase == "result_10y" and r10 and not state.get("result_10y_notified"):
        send, kind, flag = True, "result_10y", "result_10y_notified"
        title = f"[USTSY] 10년 {sc10['label']} | 필수3 확인"
    elif phase == "result_30y" and r30 and not state.get("result_30y_notified"):
        send, kind, flag = True, "result_30y", "result_30y_notified"
        title = f"[USTSY] 30년 {sc30['label']} | 감시 종료"
    elif mode in {"force", "dry"}:
        send, kind = True, phase
        phase_ko = {"announce": "공고 확인", "result_3y": "3년 결과", "result_10y": "10년 결과", "result_30y": "30년 결과", "wait": "입찰 대기"}.get(phase, phase)
        title = f"[USTSY] 수동 점검 | {phase_ko}"

    todo = "공고·결과 전까지 미국채 ETF 비중을 이 알림으로 바꾸지 마세요."
    if kind == "announce":
        todo = "확정 규모가 잠정(10년 390억/30년 220억)과 다르면 공급 부담만 메모. 지금은 매매 신호 아님."
    elif kind == "result_3y":
        todo = "3년이 테일 +2bp이고 BTC가 약하면 다음날 10년 복제 확률을 한 단계 올리세요. 아니면 5년물 약세를 배꼽 이슈로 남기세요."
    elif kind == "result_10y":
        if sc10["label"] == "복제":
            todo = "10년이 5년물 약세를 복제. 듀레이션 추격 매수 금지. 기존 USTSY 환율 밴드만 유지."
        elif sc10["label"] == "기각":
            todo = "10년은 복제 실패. 5년물 약세를 장기 수요 붕괴로 확장하지 마세요."
        else:
            todo = "필수 3조건이 안 맞으면 복제로 쓰지 마세요. 30년 결과를 기다리세요."
    elif kind == "result_30y":
        todo = f"최종 10년 {sc10['label']} / 30년 {sc30['label']}. 둘 다 복제일 때만 장기 수요 약세로 읽으세요."

    lines = [
        f"{now.strftime('%Y-%m-%d %H:%M KST')} | USTSY10 | {kind}",
        f"이게 뭐 알림: {cfg['explain']}",
        "지금 상태: " + " / ".join([
            row_line("10y", rows.get("10y"), tails.get("10y"), avg["10y"], thr),
            row_line("30y", rows.get("30y"), tails.get("30y"), avg["30y"], thr),
            row_line("3y", rows.get("3y"), tails.get("3y")),
        ]),
        f"임계값: 테일 +{thr['tail_bp']}bp, BTC 평균-{thr['btc_below_avg']}, 간접 평균-{thr['indirect_below_avg_pp']}%p. 10년 평균 BTC {avg['10y']['btc']} 간접 {avg['10y']['indirect']}%. 30년 평균 BTC {avg['30y']['btc']} 간접 {avg['30y']['indirect']}%.",
        f"지금 할 일: {todo}",
        "나중에: 같은 단계는 다시 안 보냄. 만료 2026-10-10. 다음 슬롯은 기존 09:00/15:20 또는 원샷.",
        "익절/줄일 때: 이 알림은 484790/476760 매도 신호가 아님. 환율 밴드는 기존 USTSY 모니터.",
        "하지 말 것: 테일 없는 채점을 복제로 단정. 조회 실패를 약세로 처리. 5년물 한 건을 곡선 전체로 확장.",
        f"조회: {'OK' if ok else '실패'} {err}".strip(),
    ]

    if mode == "dry":
        print(title)
        print("\n".join(lines))
        print("dry-run; no ntfy")
        save_state(state, now)
        return 0
    if not send:
        print("condition not met; silent")
        save_state(state, now)
        return 0
    emit(cfg, title, lines, 4 if kind.startswith("result") else 3)
    if flag:
        state[flag] = True
    save_state(state, now)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
