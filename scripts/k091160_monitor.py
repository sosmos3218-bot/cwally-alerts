#!/usr/bin/env python3
"""091160 KODEX 반도체 신규편입 분할매수 규칙 알림.

판정 순서: 조회 실패 -> 펀더멘털 값 만료 -> 매수 보류 -> 리밸런싱 -> 다음 단계(1/2/3차) -> 관망.
세션당 1회, 상태가 바뀔 때만 보낸다. 조회 실패는 세션당 1회만 보낸다.
"""
from __future__ import annotations

import json
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parents[1]
CFG_PATH = ROOT / "configs" / "k091160.json"
STATE_PATH = ROOT / "state" / "k091160.json"
UA = {"User-Agent": "Mozilla/5.0 cwally-alerts"}
NAVER = "https://m.stock.naver.com/api/stock/"
P = "[091160]"


def get_json(url: str):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8", errors="ignore"))


def num(text) -> float | None:
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text)
    raw = str(text).replace(",", "").replace("%", "").replace("+", "").strip()
    try:
        return float(raw)
    except ValueError:
        return None


def completed(date_str: str, now: datetime) -> bool:
    """장 마감(15:40 KST) 전이면 오늘 행은 미확정으로 뺀다."""
    today = now.strftime("%Y-%m-%d")
    if date_str < today:
        return True
    return date_str == today and (now.hour, now.minute) >= (15, 40)


def etf_prices(code: str, now: datetime) -> list[tuple[str, float]]:
    rows = get_json(f"{NAVER}{code}/price?pageSize=15&page=1")
    out = []
    for r in rows:
        d = r.get("localTradedAt")
        c = num(r.get("closePrice"))
        if d and c and completed(d, now):
            out.append((d, c))
    return out  # 최신순


def etf_info(code: str) -> dict:
    d = get_json(f"{NAVER}{code}/integration")
    info = {x.get("code"): x.get("value") for x in d.get("totalInfos") or []}
    k = d.get("etfKeyIndicator") or {}
    dev = num(k.get("deviationRate"))
    if dev is not None and k.get("deviationSign") == "-":
        dev = -dev
    return {"high52": num(info.get("highPriceOf52Weeks")), "nav": num(k.get("nav") or info.get("nav")), "premium": dev}


def foreign_bn(code: str, session: str, now: datetime) -> tuple[float | None, float | None]:
    rows = get_json(f"{NAVER}{code}/trend?pageSize=10")
    vals = []
    for r in rows:
        d = str(r.get("bizdate") or "")
        d = f"{d[:4]}-{d[4:6]}-{d[6:8]}" if len(d) == 8 else ""
        if not d or not completed(d, now) or d > session:
            continue
        q = num(r.get("foreignerPureBuyQuant"))
        c = num(r.get("closePrice"))
        if q is None or c is None:
            continue
        vals.append(q * c / 100_000_000.0)
    if not vals or len(vals) < 5:
        return None, None
    f5 = sum(vals[:5])
    f10 = sum(vals[:10]) if len(vals) >= 10 else None
    return f5, f10


def fund_status(f: dict, today: str) -> dict:
    trend = f.get("consensus_trend")
    dram = num(f.get("dram_qoq_pct"))
    nand = num(f.get("nand_qoq_pct"))
    return {
        "stale": today > str(f.get("review_by") or "0000"),
        "bad_hold": trend in {"down5", "down10"} or bool(f.get("dram_nand_outlook_down")),
        "stage1_ok": trend in {"up", "flat"} and ((dram or 0) > 0 or (nand or 0) > 0),
        "dram": dram,
        "nand": nand,
        "hbm_ok": f.get("hbm") in {"tight", "ok"},
        "cons_up": trend == "up",
        "trend": trend,
    }


def fmt(v, unit="", digits=0, sign=False):
    if v is None:
        return "확인 불가"
    s = f"{v:+,.{digits}f}" if sign else f"{v:,.{digits}f}"
    return s + unit


def build(kind: str, stamp: str, session: str, status: str, cfg: dict, extra: str = "") -> tuple[str, str, int]:
    s1, s2, s3 = cfg["stage1"], cfg["stage2"], cfg["stage3"]
    head = [
        f"{stamp} | 091160 | 세션 {session}",
        "이게 뭐 알림: KODEX 반도체 신규편입 분할매수(30/30/40) 규칙 점검이다.",
        f"지금 결과/상태: {status}",
    ]
    if extra:
        head.append(f"근거: {extra}")
    copy = {
        "fail": (
            f"{P} 조회실패 | 판정 보류",
            [
                "지금 할 일: 조회 실패다. 조건 미충족이 아니다. 키움 HTS에서 091160 종가, NAV 괴리율, 삼성전자와 SK하이닉스 외국인 5일 순매수만 직접 본다.",
                "나중에: 다음 개장일 09:00에 한 번 더 판정한다.",
                "익절/줄일 때: 실패만으로 사거나 팔지 않는다.",
                "하지 말 것: 추정값으로 매수하지 않는다.",
            ],
            4,
        ),
        "stale": (
            f"{P} 점검 | 실적·메모리 값 갱신 필요",
            [
                "지금 할 일: 신규 매수 보류. configs/k091160.json의 fundamentals(컨센서스 추세, DRAM·NAND 전분기 대비 가격 전망, HBM 상태)를 최신 값으로 고치고 review_by를 늘린다.",
                "나중에: 값을 갱신한 다음 날 09:00부터 다시 단계 판정한다.",
                "익절/줄일 때: 기존 물량은 리밸런싱 규칙대로만 본다.",
                "하지 말 것: 오래된 실적 전망으로 추가 매수하지 않는다.",
            ],
            3,
        ),
        "hold": (
            f"{P} 매수 보류 | 신규 0%",
            [
                "지금 할 일: 오늘은 사지 않는다. 기존 물량은 그대로 둔다.",
                "나중에: 보류 사유가 모두 풀리면 다음 단계 알림이 다시 온다.",
                "익절/줄일 때: 평균단가 대비 +35% 이상이면 리밸런싱 알림을 따른다.",
                "하지 말 것: 급등이나 프리미엄(괴리율 +1% 초과) 구간에서 추격 매수하지 않는다.",
            ],
            3,
        ),
        "s1": (
            f"{P} 1차 매수 가능 | 목표금액 {s1['alloc_pct']}%",
            [
                f"지금 할 일: 목표금액의 {s1['alloc_pct']}%만 지정가로 산다. 주문 직전 괴리율이 {s1['prem_low']}%~+{s1['prem_high']}% 안인지 다시 본다.",
                f"나중에: 체결되면 configs/k091160.json position.fills에 1차 가격을 적는다. 2차는 1차가 대비 -{s2['drop_from_fill1_pct']:.0f}% 또는 {s2['price_abs']:,}원 이하에서만 본다.",
                "익절/줄일 때: 반도체 합산 비중이 주식자산 10%를 넘으면 늘리지 않는다.",
                "하지 말 것: 일괄매수, 시장가 주문, 신용·미수를 쓰지 않는다.",
            ],
            4,
        ),
        "s2": (
            f"{P} 2차 매수 가능 | 목표금액 {s2['alloc_pct']}%",
            [
                f"지금 할 일: 목표금액의 {s2['alloc_pct']}%를 지정가로 산다. 괴리율 {s2['prem_low']}%~+{s2['prem_high']}% 안에서만 산다.",
                f"나중에: 체결되면 fills에 2차를 적는다. 3차는 1차가 대비 -{s3['drop_from_fill1_pct']:.0f}% 또는 {s3['price_abs']:,}원 이하에서 5일 저점 갱신이 멈출 때만 본다.",
                "익절/줄일 때: 반도체 합산 10% 한도를 넘기지 않는다.",
                "하지 말 것: 3차 물량까지 한 번에 넣지 않는다.",
            ],
            4,
        ),
        "s3": (
            f"{P} 3차 매수 가능 | 목표금액 {s3['alloc_pct']}%",
            [
                f"지금 할 일: 목표금액의 남은 {s3['alloc_pct']}%를 지정가로 산다. 괴리율 {s3['prem_low']}%~+{s3['prem_high']}% 안에서만 산다.",
                "나중에: 체결되면 fills와 avg_cost를 갱신한다. 이후에는 리밸런싱만 본다.",
                "익절/줄일 때: 평균단가 대비 +35%면 목표비중으로 되돌린다.",
                "하지 말 것: 한도를 넘는 물타기, 신용·미수를 쓰지 않는다.",
            ],
            4,
        ),
        "rebal": (
            f"{P} 리밸런싱 | 목표비중으로 줄이기",
            [
                "지금 할 일: 평균단가 대비 +35% 이상이다. 목표비중을 넘는 부분만 판다.",
                "나중에: 반도체 합산 비중이 목표보다 30% 이상 많으면 그 초과분도 줄인다.",
                "익절/줄일 때: 전량 매도가 아니라 목표비중 복귀까지만 줄인다.",
                "하지 말 것: 판 돈으로 바로 다른 반도체 ETF를 사지 않는다.",
            ],
            4,
        ),
        "wait": (
            f"{P} 관망 | 조건 대기",
            [
                "지금 할 일: 오늘은 사지 않는다. 다음 단계 조건 중 빠진 항목만 기다린다.",
                "나중에: 조건이 모두 맞으면 해당 단계 알림이 온다.",
                "익절/줄일 때: 평균단가 대비 +35% 이상이면 리밸런싱 알림을 따른다.",
                "하지 말 것: 조건 일부만 맞았다고 미리 사지 않는다.",
            ],
            3,
        ),
    }
    title, tail, pri = copy[kind]
    return title, "\n".join(head + tail), pri


def publish(title: str, message: str, priority: int) -> None:
    sys.path.insert(0, str(ROOT / "scripts"))
    import ntfy_util

    ntfy_util.publish("cwally-port", title, message, priority=priority, tags=["chart_with_upwards_trend", "bell"])


def save(data: dict) -> None:
    STATE_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    dry = "--dry-run" in sys.argv
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    if len(sys.argv) > 1 and sys.argv[1].startswith("--now="):
        now = datetime.fromisoformat(sys.argv[1][6:]).replace(tzinfo=KST)
    today = now.strftime("%Y-%m-%d")
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    prev = {"session_date": None, "state": None, "last_sent_key": None, "fail_sent_date": None, "f10_bn": None}
    if STATE_PATH.exists():
        try:
            prev.update(json.loads(STATE_PATH.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass

    if today > str(cfg.get("valid_until") or "9999-12-31"):
        print("expired (valid_until); silent")
        return 0

    code = cfg["etf"]["code"]
    closes, info, f5, f10, err = [], {}, None, None, []
    try:
        closes = etf_prices(code, now)
    except Exception as exc:
        err.append(f"가격 {exc}")
    try:
        info = etf_info(code)
    except Exception as exc:
        err.append(f"NAV {exc}")
    session = closes[0][0] if closes else None

    if session and session == prev.get("session_date") and (prev.get("last_sent_key") or "").startswith(session):
        print(f"session {session} already evaluated; silent")
        return 0

    if session:
        sums = []
        for key in ("samsung", "hynix"):
            try:
                sums.append(foreign_bn(cfg["flow_codes"][key], session, now))
            except Exception as exc:
                err.append(f"수급 {key} {exc}")
                sums.append((None, None))
        if all(s[0] is not None for s in sums):
            f5 = sum(s[0] for s in sums)
        if all(s[1] is not None for s in sums):
            f10 = sum(s[1] for s in sums)

    close = closes[0][1] if closes else None
    prem = info.get("premium")
    high52 = info.get("high52")
    if close is None or prem is None or high52 is None or f5 is None or len(closes) < 6:
        err.append("필수값 없음")

    base = {
        "updated_at": stamp,
        "session_date": session,
        "close": close,
        "premium_pct": prem,
        "f5_bn": f5,
        "f10_bn": f10,
    }

    if err:
        status = (
            f"조회 실패({'; '.join(err)[:160]}). 종가 {fmt(close, '원')} / 괴리율 {fmt(prem, '%', 2, True)} / "
            f"외국인 5일 {fmt(f5, '억', 0, True)}"
        )
        title, msg, pri = build("fail", stamp, session or "없음", status, cfg)
        print(title)
        print(msg)
        send = prev.get("fail_sent_date") != (session or today)
        save({**prev, **base, "fail_sent_date": session or today})
        if send and not dry:
            publish(title, msg, pri)
        elif not send:
            print("duplicate fail; silent")
        return 0

    fund = fund_status(cfg["fundamentals"], today)
    s1, s2, s3, hold, reb = cfg["stage1"], cfg["stage2"], cfg["stage3"], cfg["hold"], cfg["rebalance"]
    c5ago = closes[5][1]
    chg5 = (close / c5ago - 1) * 100
    dd = (1 - close / high52) * 100
    last5 = [c for _, c in closes[:5]]
    low_stopped = close > min(last5)
    fills = cfg.get("position", {}).get("fills") or []
    stages_done = sorted({int(f.get("stage")) for f in fills if f.get("stage")})
    fill1 = next((num(f.get("price")) for f in fills if int(f.get("stage") or 0) == 1), None)
    avg = num(cfg.get("position", {}).get("avg_cost"))

    status = (
        f"종가 {close:,.0f}원 / 5일 {chg5:+.1f}% / 52주 고점 대비 -{dd:.1f}% / "
        f"괴리율 {prem:+.2f}%(09:00 조회값) / 외국인(삼성+하이닉스) 5일 {f5:+,.0f}억, 10일 {fmt(f10, '억', 0, True)}"
    )
    reasons: list[str] = []
    kind = "wait"

    if fund["stale"]:
        kind = "stale"
        reasons.append(f"fundamentals review_by {cfg['fundamentals'].get('review_by')} 지남")
    else:
        if close >= cfg["etf"]["hold_price"]:
            reasons.append(f"종가 {cfg['etf']['hold_price']:,}원 이상")
        if chg5 >= cfg["etf"]["hold_5d_pct"]:
            reasons.append(f"5일 +{cfg['etf']['hold_5d_pct']:.0f}% 이상 급등")
        if prem > hold["prem_max"]:
            reasons.append(f"괴리율 +{hold['prem_max']}% 초과")
        if f5 <= hold["f5_max_sell_bn"]:
            reasons.append(f"외국인 5일 {hold['f5_max_sell_bn']:,}억 이하 순매도")
        if fund["bad_hold"]:
            reasons.append("컨센서스 하향 또는 메모리 가격 전망 하향(수동값)")
        if reasons:
            kind = "hold"
        elif avg and close >= avg * (1 + reb["gain_from_avg_pct"] / 100):
            kind = "rebal"
            reasons.append(f"평균단가 {avg:,.0f}원 대비 {(close / avg - 1) * 100:+.1f}%")
        elif not stages_done:
            zone = s1["price_low"] <= close <= s1["price_high"] or s1["dd_min_pct"] <= dd <= s1["dd_max_pct"]
            checks = {
                f"가격 {s1['price_low']:,}~{s1['price_high']:,}원 또는 고점 대비 -{s1['dd_min_pct']:.0f}~-{s1['dd_max_pct']:.0f}%": zone,
                f"괴리율 {s1['prem_low']}~+{s1['prem_high']}%": s1["prem_low"] <= prem <= s1["prem_high"],
                f"외국인 5일 {s1['f5_min_bn']:,}억 이상": f5 >= s1["f5_min_bn"],
                "컨센서스 하향 없음, DRAM 또는 NAND 상승 전망": fund["stage1_ok"],
            }
            kind = "s1" if all(checks.values()) else "wait"
            reasons = [("충족 " if v else "미충족 ") + k for k, v in checks.items()]
        elif stages_done == [1] and fill1:
            zone = close <= fill1 * (1 - s2["drop_from_fill1_pct"] / 100) or close <= s2["price_abs"]
            checks = {
                f"1차가 대비 -{s2['drop_from_fill1_pct']:.0f}% 또는 {s2['price_abs']:,}원 이하": zone,
                f"괴리율 {s2['prem_low']}~+{s2['prem_high']}%": s2["prem_low"] <= prem <= s2["prem_high"],
                "외국인 10일 순매도 아님": f10 is not None and f10 >= s2["f10_min_bn"],
                f"DRAM +{s2['dram_min_qoq']:.0f}% 이상, NAND {s2['nand_min_qoq']:.0f}% 이상, HBM 유지": (
                    (fund["dram"] or -99) >= s2["dram_min_qoq"] and (fund["nand"] or -99) >= s2["nand_min_qoq"] and fund["hbm_ok"]
                ),
            }
            kind = "s2" if all(checks.values()) else "wait"
            reasons = [("충족 " if v else "미충족 ") + k for k, v in checks.items()]
        elif stages_done == [1, 2] and fill1:
            zone = (close <= fill1 * (1 - s3["drop_from_fill1_pct"] / 100) or close <= s3["price_abs"]) and low_stopped
            prev_f10 = num(prev.get("f10_bn"))
            flow_ok = f10 is not None and (f10 >= 0 or (prev_f10 is not None and f10 > prev_f10))
            checks = {
                f"1차가 대비 -{s3['drop_from_fill1_pct']:.0f}% 또는 {s3['price_abs']:,}원 이하 + 5일 저점 갱신 멈춤": zone,
                f"괴리율 {s3['prem_low']}~+{s3['prem_high']}%": s3["prem_low"] <= prem <= s3["prem_high"],
                "외국인 10일 순매수 또는 순매도 축소": flow_ok,
                "컨센서스 상향 + DRAM/NAND 상승 전망": fund["cons_up"] and ((fund["dram"] or 0) > 0 or (fund["nand"] or 0) > 0),
            }
            kind = "s3" if all(checks.values()) else "wait"
            reasons = [("충족 " if v else "미충족 ") + k for k, v in checks.items()]
        else:
            kind = "wait"
            reasons.append("fills 기록 확인 필요 또는 3차까지 완료. 리밸런싱만 본다.")

    title, msg, pri = build(kind, stamp, session, status, cfg, " / ".join(reasons))
    print(title)
    print(msg)
    key = f"{session}:{kind}"
    send = kind != prev.get("state")
    save({**base, "state": kind, "last_sent_key": key, "fail_sent_date": None})
    if not send:
        print(f"state {kind} unchanged; silent")
        return 0
    if dry:
        print("dry-run; not sent")
        return 0
    publish(title, msg, pri)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
