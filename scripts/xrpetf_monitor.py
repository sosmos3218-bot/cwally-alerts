#!/usr/bin/env python3
"""[XRP-ETF] 미국 현물 XRP ETF 주간 순유입 합계 (조건 알림만).

판정: 직전 미국 거래 주(월~금) 펀드별 일별 순유입(USD) 합계 >= 4천만 달러 -> cwally-port 1회.
출처: XRP Insights 무료 데이터셋 xrp-etf-flows.csv (발행사 웹사이트 보유량 기준). BITW 는 현물 XRP ETF 가 아니라 제외.
flow_status 가 computed 가 아닌 행(예: GXRP excluded)은 0으로 넣지 않고 '제외'로 본문에 밝힌다.
주 완료 판단: 그 주 금요일 행이 있거나 그보다 뒤 날짜 행이 있으면 완료. 아니면 미게시로 보고 다음 실행에서 다시 본다.
월요일 09:00 이후에도 미게시면 cwally-ops 로 1번 안내(제목 '미게시').
modes: auto | dry(발송 없음, state 안 씀) | seed(지난 주들을 판정 완료로 기록, 발송 없음)
매매 신호가 아니다.
"""
from __future__ import annotations

import csv
import io
import json
import sys
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic, route_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
NAME = "xrpetf"
CFG_PATH = ROOT / "configs" / f"{NAME}.json"
STATE_PATH = ROOT / "state" / f"{NAME}.json"
URL = "https://xrp-insights.com/data/xrp-etf-flows.csv"
UA = "cwally-alerts/1.0 (+https://github.com/sosmos3218-bot/cwally-alerts; personal ntfy alert)"
NA = "확인 불가"


def fetch_rows() -> list[dict]:
    err: Exception | None = None
    for _ in range(3):
        try:
            req = urllib.request.Request(URL, headers={"User-Agent": UA, "Accept": "text/csv"})
            with urllib.request.urlopen(req, timeout=40) as resp:
                text = resp.read().decode("utf-8")
            rows = list(csv.DictReader(io.StringIO(text)))
            if not rows or "flow_usd" not in rows[0] or "ticker" not in rows[0]:
                raise ValueError("CSV 형식이 예상과 다름")
            return rows
        except Exception as exc:  # noqa: BLE001
            err = exc
    raise RuntimeError(f"xrp-etf-flows.csv 조회 실패: {err}")


def fnum(v: str | None) -> float | None:
    try:
        return float(v) if v not in (None, "") else None
    except ValueError:
        return None


def week_of(d: date) -> date:
    return d - timedelta(days=d.weekday())


def target_friday(today: date) -> date:
    d = today - timedelta(days=2)
    while d.weekday() != 4:
        d -= timedelta(days=1)
    return d


def summarize(rows: list[dict], monday: date, exclude: set[str]) -> dict:
    friday = monday + timedelta(days=4)
    days = sorted({r["date"] for r in rows if monday.isoformat() <= r["date"] <= friday.isoformat()})
    later = any(r["date"] > friday.isoformat() for r in rows)
    complete = friday.isoformat() in days or (later and bool(days))
    total = 0.0
    total_xrp = 0.0
    by_fund: dict[str, float] = defaultdict(float)
    gaps: dict[str, list[str]] = defaultdict(list)
    hold: dict[str, list[tuple[str, float | None]]] = defaultdict(list)
    tickers = sorted({r["ticker"] for r in rows if r["date"] in days and r["ticker"] not in exclude})
    for r in rows:
        if r["date"] not in days or r["ticker"] in exclude:
            continue
        hold[r["ticker"]].append((r["date"], fnum(r.get("xrp_holdings"))))
        usd = fnum(r.get("flow_usd"))
        if (r.get("flow_status") or "") == "computed" and usd is not None:
            total += usd
            total_xrp += fnum(r.get("xrp_flow")) or 0.0
            by_fund[r["ticker"]] += usd
        else:
            gaps[r["ticker"]].append(r["date"])
    gap_notes = []
    for t, ds in sorted(gaps.items()):
        hs = [h for _, h in sorted(hold[t]) if h is not None]
        chg = f"보유량 {hs[0]:,.0f} -> {hs[-1]:,.0f} XRP" if len(hs) >= 2 else "보유량 변화 확인 불가"
        if len(hs) >= 2 and hs[0] == hs[-1]:
            chg += "(변화 없음)"
        gap_notes.append(f"{t} {len(ds)}일 흐름 미산출({chg})")
    return {"monday": monday.isoformat(), "friday": friday.isoformat(), "days": days, "complete": complete,
            "net_usd": round(total), "net_xrp": round(total_xrp), "by_fund": {k: round(v) for k, v in sorted(by_fund.items())},
            "tickers": tickers, "gaps": gap_notes}


def base_rate(rows: list[dict], upto_monday: date, exclude: set[str], thr: float) -> tuple[int, int, str | None]:
    if not rows:
        return 0, 0, None
    first = week_of(date.fromisoformat(min(r["date"] for r in rows)))
    if first.isoformat() < min(r["date"] for r in rows):
        first += timedelta(days=7)  # 첫 주가 부분 주면 뺀다
    n = hit = 0
    m = first
    while m < upto_monday:
        s = summarize(rows, m, exclude)
        if s["complete"] and s["days"]:
            n += 1
            hit += s["net_usd"] >= thr
        m += timedelta(days=7)
    return n, hit, first.isoformat()


def usd_m(v: float) -> str:
    return f"{v / 1e6:+,.1f}백만 달러"


def body(stamp: str, what: str, status: str, action: str, later: str, trim: str, avoid: str, tilt: str | None = None) -> str:
    return "\n".join([
        f"{stamp} | XRP 현물 ETF 주간 순유입",
        f"이게 뭐 알림: {what}",
        f"지금 결과/상태: {status}",
        *([f"기울기(기본 확률 대비): {tilt}"] if tilt else []),
        f"지금 할 일: {action}",
        f"나중에: {later}",
        f"익절/줄일 때: {trim}",
        f"하지 말 것: {avoid}",
    ])


def signal_msg(cfg: dict, s: dict, n: int, hit: int, first: str | None, stamp: str) -> tuple[str, str, int, list[str]]:
    thr = float(cfg["threshold_usd"])
    funds = ", ".join(f"{t} {usd_m(v)}" for t, v in sorted(s["by_fund"].items(), key=lambda kv: -abs(kv[1])) if v)
    gaps = "; ".join(s["gaps"]) if s["gaps"] else "없음"
    rate = f"집계 시작 주({first}) 이후 완료된 {n}주 중 {hit}주" if n else "과거 주 수 확인 불가"
    status = (
        f"{s['monday']}~{s['friday']} (미국 거래일 {len(s['days'])}일) 현물 XRP ETF 순유입 합계 {usd_m(s['net_usd'])}"
        f" ({s['net_xrp']:+,.0f} XRP), 기준 {thr / 1e6:,.0f}백만 달러 이상 충족. 펀드별: {funds or '변화 없음'}."
        f" 흐름 미산출(합계에서 뺌): {gaps}. 출처: XRP Insights 펀드별 일별 데이터(발행사 보유량 기준), BITW 제외."
        f" 참고로 주간 4천만 달러 이상은 {rate} (같은 데이터로 계산, GXRP 흐름 미산출 구간 포함)."
    )
    tilt = ("상방 가능성 다소 높음 (근거: 기관 자금이 한 주에 기준 이상 들어온 수요 신호."
            " 확률 수치는 계산하지 않았고 ETF 흐름만으로 가격 방향이 정해지지는 않음)")
    msg = body(stamp, "미국 현물 XRP ETF 들의 한 주 순유입 합계가 기준(4천만 달러) 이상일 때만 오는 알림입니다.", status,
               "보유 중이면 계획대로 유지. 새로 사려면 가격 위치와 거래소 보유량/에스크로 알림도 같이 보고 분할로만.",
               "다음 주 일요일 21:00에 다시 판정합니다. 기준 미만인 주는 알림이 없습니다.",
               "이 알림만으로 정하지 않기. 미리 정한 비중과 손절 기준대로.",
               "유입 소식 하나로 추격 매수, 미산출 펀드를 0으로 읽기, 한 주 수치를 추세로 단정.", tilt)
    return f"[XRP-ETF] 주간 순유입 {usd_m(s['net_usd'])} | 기준 이상, 추격 말고 분할 검토", msg, 4, ["moneybag"]


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    if mode not in {"auto", "dry", "seed"}:
        print(f"unknown mode {mode}")
        return 2
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    state = json.loads(STATE_PATH.read_text(encoding="utf-8")) if STATE_PATH.exists() else {}
    weeks = state.setdefault("weeks", {})
    topic = resolve_topic("port", cfg.get("ntfy_topic"))
    server = resolve_server(cfg.get("ntfy_server"))
    exclude = set(cfg.get("exclude_tickers") or [])
    thr = float(cfg["threshold_usd"])
    fri = target_friday(now.date())
    monday = fri - timedelta(days=4)
    key = monday.isoformat()
    out: list[tuple[str, str, int, list[str]]] = []
    print(f"target week {key}~{fri} (already judged: {key in weeks and weeks[key].get('judged')})")
    if mode != "dry" and weeks.get(key, {}).get("judged"):
        print("week already judged; nothing to do")
        return 0
    try:
        rows = fetch_rows()
        print(f"rows {len(rows)}, last date {max(r['date'] for r in rows)}")
        s = summarize(rows, monday, exclude)
        print("summary:", json.dumps(s, ensure_ascii=False))
        n, hit, first = base_rate(rows, monday, exclude, thr)
        print(f"base: {hit}/{n} complete weeks since {first} had net >= {thr:,.0f}")
        if mode == "seed":
            m = date.fromisoformat(first) if first else monday
            while m <= monday:
                ss = summarize(rows, m, exclude)
                if ss["complete"] and ss["days"]:
                    weeks[m.isoformat()] = {"judged": True, "net_usd": ss["net_usd"], "met": ss["net_usd"] >= thr,
                                            "sent": False, "note": "seed 2026-10-22 (발송 없음)"}
                m += timedelta(days=7)
            print(f"seeded {len(weeks)} weeks; target week met={s['net_usd'] >= thr} (no send)")
        elif not s["complete"]:
            print("week not complete yet (Friday row missing)")
            late = now >= datetime.combine(fri + timedelta(days=3), datetime.min.time(), KST) + timedelta(hours=9)
            fn = state.setdefault("fail_notice", {})
            if late and fn.get("missing") != key:
                out.append(("[XRP-ETF] 주간 ETF 흐름 미게시 | 판정 못 함", body(
                    stamp, "현물 XRP ETF 주간 순유입 알림의 데이터 지연 안내.",
                    f"{key}~{fri} 주 금요일 행이 XRP Insights 데이터에 아직 없음. 순유입 합계 {NA}.",
                    "필요하면 발행사 사이트나 SoSoValue에서 직접 확인.", "다음 morning 실행(평일 09:00)에서 다시 봅니다.",
                    "이번 안내로 비중을 바꾸지 마세요.", "미게시를 순유입 0으로 읽기."), 3, ["warning"]))
                fn["missing"] = key
        else:
            met = s["net_usd"] >= thr
            weeks[key] = {"judged": True, "net_usd": s["net_usd"], "met": met, "sent": met, "gaps": s["gaps"],
                          "at": now.isoformat()}
            if met:
                out.append(signal_msg(cfg, s, n, hit, first, stamp))
            else:
                print(f"condition not met: {s['net_usd']:,.0f} < {thr:,.0f} (no alert)")
    except Exception as exc:  # noqa: BLE001
        print(f"fetch/parse failed: {exc}", file=sys.stderr)
        fn = state.setdefault("fail_notice", {})
        if fn.get("fetch") != key and mode != "seed":
            out.append(("[XRP-ETF] ETF 흐름 조회 실패 | 판정 못 함", body(
                stamp, "현물 XRP ETF 주간 순유입 알림의 데이터 조회 실패 안내.", f"XRP Insights 데이터 {NA} ({str(exc)[:120]}).",
                "필요하면 발행사 사이트에서 직접 확인.", "다음 실행 때 다시 봅니다.", "이번 안내로 비중을 바꾸지 마세요.",
                "조회 실패를 유입 없음으로 읽기."), 3, ["warning"]))
            fn["fetch"] = key
    for title, msg, pri, tags in out:
        print(f"TITLE: {title}\nPRIORITY: {pri} TOPIC: {route_topic(topic, title)}\n{msg}\n")
        if mode == "auto":
            publish(topic, title, msg, pri, tags, server)
    if mode == "dry":
        print("DRY RUN: no publish, state not written")
        return 0
    state["updated_at"] = now.isoformat()
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
