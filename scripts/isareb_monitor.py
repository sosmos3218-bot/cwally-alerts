#!/usr/bin/env python3
"""ISA monthly rebalance note. Sends once per month on the Sunday portlight slot."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
CFG_PATH = ROOT / "configs" / "isareb.json"
STATE_PATH = ROOT / "state" / "isareb.json"


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


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def regular_close(code: str):
    """Latest completed KRX regular-session close via krx_close (Daum, else Naver 15:30 bar).

    Naver basic closePrice on a weekend is the NXT/after-hours last print, not the KRX close (2026-11-08).
    Returns (close, None); the second slot used to carry the Naver stock name and is unused.
    """
    import krx_close

    bars = krx_close.completed(krx_close.regular_bars(code, 5), datetime.now(KST))
    if not bars:
        return None, None
    return bars[-1].close, None


def won(n: float) -> str:
    return f"{int(round(n)):,}원"


def pct(n: float) -> str:
    return f"{n * 100:.1f}%"


def first_sunday(now: datetime) -> datetime:
    first = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    offset = (6 - first.weekday()) % 7
    return first + timedelta(days=offset)


def due(now: datetime, state: dict, mode: str) -> bool:
    if mode in {"force", "install"}:
        return True
    key = now.strftime("%Y-%m")
    if state.get("last_sent_month") == key:
        return False
    return now.date() >= first_sunday(now).date()


def value_rows(cfg: dict):
    rows = []
    missing = []
    for h in cfg["holdings"]:
        try:
            price, live_name = regular_close(h["code"])
        except Exception as exc:
            print(f"price fail {h['code']}: {exc}", file=sys.stderr)
            price, live_name = None, None
        if price is None:
            missing.append(h["name"])
            continue
        qty = int(h["qty"])
        cost = float(h["cost"])
        rows.append(
            {
                **h,
                "price": price,
                "live_name": live_name,
                "value": price * qty,
                "pnl": (price - cost) * qty,
                "pnl_pct": (price / cost - 1) if cost else 0,
            }
        )
    return rows, missing


def plan(cfg: dict, rows: list[dict]) -> dict:
    t = cfg["targets"]
    total = sum(r["value"] for r in rows) + float(cfg.get("cash_krw") or 0)
    by = {r["code"]: r for r in rows}

    def w(codes):
        return sum(by[c]["value"] for c in codes if c in by) / total if total else 0

    samsung = w(["005930", "005935"])
    single = by["005930"]["value"] / total
    kr = w(["005930", "005935", "270810", "064400"])
    us = w(["448290", "452360"])
    gold = w(["0022T0"])
    ig = w(["437350"])
    kosdaq = w(["270810"])
    lgcns = w(["064400"])
    gaps = []
    if single - t["single_cap"] >= t["action_gap"]:
        gaps.append(f"삼성전자 {pct(single)}, 단일 상한 {pct(t['single_cap'])}")
    if samsung - t["samsung_group_cap"] >= t["action_gap"]:
        gaps.append(f"삼성 보통+우선 {pct(samsung)}, 합산 상한 {pct(t['samsung_group_cap'])}")
    if kr - t["kr_equity_cap"] >= t["action_gap"]:
        gaps.append(f"국내주식 {pct(kr)}, 상한 {pct(t['kr_equity_cap'])}")
    if t["us_equity_target"] - us >= t["action_gap"]:
        gaps.append(f"미국주식 {pct(us)}, 목표 {pct(t['us_equity_target'])}")
    if t["us_ig_target"] - ig >= t["action_gap"]:
        gaps.append(f"미국 단기회사채 {pct(ig)}, 목표 {pct(t['us_ig_target'])}")
    if gold < t["gold_cc_band"][0] - t["action_gap"] or gold > t["gold_cc_band"][1] + t["action_gap"]:
        gaps.append(f"금커버드콜 {pct(gold)}, 밴드 {pct(t['gold_cc_band'][0])}-{pct(t['gold_cc_band'][1])}")

    actions = []
    if not gaps:
        return {
            "total": total,
            "weights": {
                "single": single,
                "samsung": samsung,
                "kr": kr,
                "us": us,
                "gold": gold,
                "ig": ig,
                "kosdaq": kosdaq,
                "lgcns": lgcns,
            },
            "gaps": [],
            "actions": ["밴드 안. 이번 달 매매 없음."],
            "hold": True,
        }

    # Trim common shares until single and group caps are met. Keep preferred.
    px = by["005930"]["price"]
    qty = by["005930"]["qty"]
    sell = 0
    while sell < qty:
        new_common = (qty - sell) * px
        new_group = new_common + by["005935"]["value"]
        if new_common / total <= t["single_cap"] and new_group / total <= t["samsung_group_cap"]:
            break
        sell += 1
    proceeds = sell * px
    if sell:
        actions.append(f"삼성전자 {sell}주 매도 검토, 약 {won(proceeds)}. 우선주는 유지.")

    need_us = max(0, t["us_equity_target"] * total - (by["448290"]["value"] + by["452360"]["value"]))
    need_ig = max(0, t["us_ig_target"] * total - by["437350"]["value"])
    budget = proceeds
    us_buy = min(budget, need_us)
    budget -= us_buy
    ig_buy = min(budget, need_ig)
    budget -= ig_buy
    spx_buy = us_buy * 0.6
    div_buy = us_buy - spx_buy
    if spx_buy >= by["448290"]["price"]:
        n = int(spx_buy // by["448290"]["price"])
        actions.append(f"TIGER 미국S&P500(H) {n}주 매수 검토, 약 {won(n * by['448290']['price'])}.")
    if div_buy >= by["452360"]["price"]:
        n = int(div_buy // by["452360"]["price"])
        actions.append(f"SOL 미국배당다우존스(H) {n}주 매수 검토, 약 {won(n * by['452360']['price'])}.")
    if ig_buy >= by["437350"]["price"]:
        n = int(ig_buy // by["437350"]["price"])
        actions.append(f"RISE 미국단기회사채 {n}주 매수 검토, 약 {won(n * by['437350']['price'])}.")
    if budget >= 100000:
        actions.append(f"남은 약 {won(budget)}는 예수금 또는 RP로 둘 것. 코스닥150, LG씨엔에스 추가매수 아님.")
    for r in rows:
        if r["pnl_pct"] <= t["no_add_loss"]:
            actions.append(f"{r['name']} 수익률 {r['pnl_pct'] * 100:.1f}%. 물타기 금지, 손익통산 매도만 별도 검토.")
    return {
        "total": total,
        "weights": {
            "single": single,
            "samsung": samsung,
            "kr": kr,
            "us": us,
            "gold": gold,
            "ig": ig,
            "kosdaq": kosdaq,
            "lgcns": lgcns,
        },
        "gaps": gaps,
        "actions": actions,
        "hold": False,
    }


def status_line(rows, p) -> str:
    w = p["weights"]
    bits = [
        f"평가 {won(p['total'])}",
        f"삼성전자 {pct(w['single'])}",
        f"삼성합산 {pct(w['samsung'])}",
        f"국내주식 {pct(w['kr'])}",
        f"미국주식 {pct(w['us'])}",
        f"금커버드콜 {pct(w['gold'])}",
        f"단기회사채 {pct(w['ig'])}",
    ]
    losers = [f"{r['name']} {r['pnl_pct'] * 100:.1f}%" for r in rows if r["pnl_pct"] <= -0.15]
    if losers:
        bits.append("큰 손실 " + ", ".join(losers))
    return ", ".join(bits)


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    dry = mode in {"dry", "test", "dry-run"}
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    state = load_json(STATE_PATH, {"updated_at": None, "last_sent_month": None, "fail_sent_date": None})
    print(f"{stamp} mode={mode}")

    if mode == "install":
        title = "[ISAREB] 005930/0022T0/437350/005935/448290/452360/270810/064400 설치 확인 | 매매 신호 아님"
        msg = "\n".join(
            [
                f"{stamp} | ISA 리밸런싱 알림 설치",
                "이게 뭐 알림: ISA 계좌 비중을 월 1회 보고, 밴드를 넘을 때만 검토 수량을 적는 알림.",
                "지금 결과/상태: 설치 확인. 이번 메시지는 매매 신호가 아님.",
                "지금 할 일: 없음. 목표 비중은 configs/isareb.json 가정값.",
                "나중에: 매월 첫째 일요일 이후 첫 일요일 21:00 KST 슬롯에서 1회.",
                "익절/줄일 때: 이 설치 메시지는 매도 신호가 아님.",
                "하지 말 것: 이 메시지를 주문으로 보기. 신용/미수.",
            ]
        )
        print(title)
        print(msg)
        publish(resolve_topic("port"), title, msg, 3, ["information_source"], resolve_server(cfg.get("ntfy_server")))
        print("install confirmation sent; state untouched")
        return 0

    if not due(now, state, mode):
        print("isareb not due; silent")
        return 0

    rows, missing = value_rows(cfg)
    if missing or len(rows) != len(cfg["holdings"]):
        title = "[ISAREB] 005930/0022T0/437350/005935/448290/452360/270810/064400 시세 조회 실패 | 직접 확인"
        msg = "\n".join(
            [
                f"{stamp} | ISA 리밸런싱 조회 실패",
                "이게 뭐 알림: 비중을 계산하지 못한 운영 알림.",
                f"지금 결과/상태: 시세 없음 {', '.join(missing) or '일부'}. 매매 신호 아님.",
                "지금 할 일: 증권앱에서 ISA 평가액을 직접 확인.",
                "나중에: 다음 일요일 21:00 슬롯에서 재시도.",
                "익절/줄일 때: 없음.",
                "하지 말 것: 조회 실패를 매도 신호로 보기.",
            ]
        )
        print(title)
        print(msg)
        if state.get("fail_sent_date") != now.strftime("%Y-%m-%d") or mode == "force":
            if not dry:
                publish(resolve_topic("ops"), title, msg, 4, ["warning"], resolve_server(cfg.get("ntfy_server")))
                state["fail_sent_date"] = now.strftime("%Y-%m-%d")
                state["updated_at"] = stamp
                save_state(state)
        return 1

    p = plan(cfg, rows)
    if p["hold"]:
        title = "[ISAREB] 005930/0022T0/437350/005935/448290/452360/270810/064400 밴드 안 | 이번 달 매매 없음"
        action = "지금 할 일: 주문하지 말 것."
    else:
        title = "[ISAREB] 005930/0022T0/437350/005935/448290/452360/270810/064400 삼성 비중 초과 | 교체 수량만 검토"
        action = "지금 할 일: " + " ".join(p["actions"])
    msg = "\n".join(
        [
            f"{stamp} | ISA 월 1회 리밸런싱",
            "이게 뭐 알림: ISA 계좌가 가정 밴드를 넘었는지 보고, 넘으면 검토 수량만 적는 알림. 주문 아님.",
            "지금 결과/상태: " + status_line(rows, p) + ("" if not p["gaps"] else ". 벗어남: " + "; ".join(p["gaps"])),
            action,
            "나중에: 목표 비중은 가정값. 바꾸려면 configs/isareb.json. 다음 발송은 다음 달.",
            "익절/줄일 때: 우선주, S&P500(H) 이익은 삼성 보통주 축소용 재원으로만 검토. 전량 매도 아님.",
            "하지 말 것: 신용/미수. LG씨엔에스와 코스닥150 물타기. 밴드 안 매매. 이 알림을 주문으로 실행.",
        ]
    )
    print(title)
    print(msg)
    if dry:
        print("dry-run; no ntfy")
        return 0
    publish(resolve_topic("port"), title, msg, 3 if p["hold"] else 4, ["chart_with_upwards_trend"], resolve_server(cfg.get("ntfy_server")))
    state["updated_at"] = stamp
    state["last_sent_month"] = now.strftime("%Y-%m")
    state["last_title"] = title
    state["fail_sent_date"] = None
    save_state(state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
