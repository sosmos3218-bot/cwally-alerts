#!/usr/bin/env python3
"""091160 KODEX 반도체 통합 알림 (kodexsemi 가격 변경 조건 + 30/30/40 분할매수 단계 + 자동 펀더멘털).

평일 15:20 KST afternoon 슬롯. 가격·거래량은 당일 15:20 값, 외국인 수급은 전일까지 확정분.
판정 우선순위: 조회 실패 -> 122,695 이탈(축소) -> 펀더멘털 만료 -> 매수 보류 -> 리밸런싱
-> 분할매수 1/2/3차 -> 가격 이벤트(재시험/안착) -> 관망(침묵).
세션당 1회, 최종 상태가 바뀔 때만 보낸다. 조회 실패는 세션당 1회.
"""
from __future__ import annotations

import json
import statistics
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


def usable(date_str: str, now: datetime) -> bool:
    """가격은 15:20 이후면 당일 값을 쓴다(동시호가 전 판정)."""
    today = now.strftime("%Y-%m-%d")
    if date_str < today:
        return True
    return date_str == today and (now.hour, now.minute) >= (15, 20)


def etf_prices(code: str, now: datetime) -> list[tuple[str, float, float | None]]:
    rows = get_json(f"{NAVER}{code}/price?pageSize=30&page=1")
    out = []
    for r in rows:
        d = r.get("localTradedAt")
        c = num(r.get("closePrice"))
        v = num(r.get("accumulatedTradingVolume"))
        if d and c and usable(d, now):
            out.append((d, c, v))
    return out  # 최신순


def price_zone(price: float, vol, avg20, z: dict):
    vol_ok = None
    if vol is not None and avg20:
        vol_ok = vol >= z["vol_mult"] * avg20
    if price < z["support"]:
        return "breakdown", vol_ok
    if price <= z["retest_hi"]:
        return ("retest_thin" if vol_ok is False else "retest"), vol_ok
    if price >= z["breakout"]:
        return "breakout", vol_ok
    return "zone_hold", vol_ok


def etf_info(code: str) -> dict:
    d = get_json(f"{NAVER}{code}/integration")
    info = {x.get("code"): x.get("value") for x in d.get("totalInfos") or []}
    k = d.get("etfKeyIndicator") or {}
    dev = num(k.get("deviationRate"))
    if dev is not None and k.get("deviationSign") == "-":
        dev = -dev
    return {"high52": num(info.get("highPriceOf52Weeks")), "nav": num(k.get("nav") or info.get("nav")), "premium": dev}


def foreign_bn(code: str, session: str, now: datetime) -> tuple[list[float], list[float]]:
    """(외국인 순매수 억원 리스트, 거래대금 억원 리스트), 최신순."""
    rows = get_json(f"{NAVER}{code}/trend?pageSize=10")
    vals, tv = [], []
    for r in rows:
        d = str(r.get("bizdate") or "")
        d = f"{d[:4]}-{d[4:6]}-{d[6:8]}" if len(d) == 8 else ""
        if not d or not completed(d, now):
            continue
        q = num(r.get("foreignerPureBuyQuant"))
        c = num(r.get("closePrice"))
        if q is None or c is None:
            continue
        vals.append(q * c / 100_000_000.0)
        tv.append((num(r.get("accumulatedTradingVolume")) or 0) * c / 100_000_000.0)
    return vals, tv


FUND_STATE = ROOT / "state" / "k091160_fund.json"


def merged_fund(cfg_f: dict) -> tuple[dict, str]:
    """state 자동값이 있으면 우선, 없으면 config 수동값. 메모리 값은 분기가 같을 때만 수동값을 쓴다."""
    f = dict(cfg_f)
    src = []
    eff = {}
    if FUND_STATE.exists():
        try:
            eff = json.loads(FUND_STATE.read_text(encoding="utf-8")).get("effective") or {}
        except json.JSONDecodeError:
            eff = {}
    if eff.get("consensus_trend"):
        f["consensus_trend"] = eff["consensus_trend"]
        src.append(f"컨센서스 {eff['consensus_trend']}(자동: {eff.get('consensus_detail', '')})")
    else:
        src.append(f"컨센서스 {f.get('consensus_trend')}(수동 {cfg_f.get('as_of')})")
    if eff.get("prelim_detail"):
        src.append(eff["prelim_detail"])
    q = eff.get("memory_quarter")
    if q and q != cfg_f.get("memory_quarter"):
        for k in ("dram_qoq_pct", "nand_qoq_pct"):
            f[k] = eff.get(k)
        f["dram_dir"], f["nand_dir"] = eff.get("dram_dir"), eff.get("nand_dir")
        tag = "자동"
    else:
        for k in ("dram_qoq_pct", "nand_qoq_pct"):
            if eff.get(k) is not None:
                f[k] = eff[k]
        tag = "수동/자동"
    q = q or cfg_f.get("memory_quarter")
    d, n = f.get("dram_qoq_pct"), f.get("nand_qoq_pct")
    ds = f"{d:+.0f}%" if d is not None else (f"{f.get('dram_dir')} 방향만" if f.get("dram_dir") else "미확인")
    ns = f"{n:+.0f}%" if n is not None else (f"{f.get('nand_dir')} 방향만" if f.get("nand_dir") else "미확인")
    src.append(f"{q} DRAM {ds}, NAND {ns}({tag})")
    src.append(f"HBM {f.get('hbm')}(수동 {cfg_f.get('as_of')})")
    return f, " / ".join(src)


def fund_status(f: dict, today: str) -> dict:
    trend = f.get("consensus_trend")
    dram = num(f.get("dram_qoq_pct"))
    nand = num(f.get("nand_qoq_pct"))
    d_up = (dram or 0) > 0 or f.get("dram_dir") == "up"
    n_up = (nand or 0) > 0 or f.get("nand_dir") == "up"
    return {
        "stale": today > str(f.get("review_by") or "0000"),
        "bad_hold": trend in {"down5", "down10"} or bool(f.get("dram_nand_outlook_down")),
        "stage1_ok": trend in {"up", "flat"} and (d_up or n_up),
        "mem_up": d_up or n_up,
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
        "이게 뭐 알림: KODEX 반도체 통합 점검(가격 변경 조건 + 30/30/40 분할매수 단계)이다.",
        f"지금 결과/상태: {status}",
    ]
    if extra:
        head.append(f"근거: {extra}")
    copy = {
        "fail": (
            f"{P} 조회실패 | 판정 보류",
            [
                "지금 할 일: 조회 실패다. 조건 미충족이 아니다. 키움 HTS에서 091160 종가, NAV 괴리율, 삼성전자와 SK하이닉스 외국인 5일 순매수만 직접 본다.",
                "나중에: 다음 개장일 15:20에 한 번 더 판정한다.",
                "익절/줄일 때: 실패만으로 사거나 팔지 않는다.",
                "하지 말 것: 추정값으로 매수하지 않는다.",
            ],
            4,
        ),
        "stale": (
            f"{P} 점검 | 실적·메모리 값 갱신 필요",
            [
                "지금 할 일: 신규 매수 보류. configs/k091160.json의 fundamentals(컨센서스 추세, DRAM·NAND 전분기 대비 가격 전망, HBM 상태)를 최신 값으로 고치고 review_by를 늘린다.",
                "나중에: 값을 갱신한 다음 개장일 15:20부터 다시 단계 판정한다.",
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
        "breakdown": (
            f"{P} 12.3만 이탈 | 축소",
            [
                f"지금 할 일: 가격이 지지선 {cfg['zone']['support']:,}원 아래다. 보유 축소를 검토한다. 이 아래에서는 추가 매수 금지.",
                f"나중에: 종가가 {cfg['zone']['support']:,}원 위로 돌아오고 분할매수 단계 조건이 다시 맞을 때만 재진입을 본다.",
                "익절/줄일 때: 한 번에 전량이 아니라 목표비중 초과분부터 줄인다.",
                "하지 말 것: 물타기, 레버리지, 신용·미수.",
            ],
            5,
        ),
        "retest": (
            f"{P} 12.3만 재시험 | 단계 조건 확인",
            [
                f"지금 할 일: 지지선 {cfg['zone']['support']:,}~{cfg['zone']['retest_hi']:,}원 재시험이다. 근거 줄의 분할매수 단계 조건이 모두 충족일 때만 산다.",
                "나중에: 단계 조건이 맞으면 해당 단계 알림이 따로 온다.",
                "익절/줄일 때: 종가가 지지선을 깨면 축소 알림을 따른다.",
                "하지 말 것: 재시험만 보고 일괄 매수하지 않는다.",
            ],
            4,
        ),
        "retest_thin": (
            f"{P} 12.3만 접근 | 거래량 미확인",
            [
                f"지금 할 일: 가격만 재시험 구간이고 거래량이 20일 평균의 {cfg['zone']['vol_mult']}배에 못 미친다. 사지 않는다.",
                "나중에: 거래량이 붙거나 단계 조건이 맞으면 다시 알린다.",
                "익절/줄일 때: 지지선 이탈 시 축소 알림을 따른다.",
                "하지 말 것: 얇은 거래량 반등에 추격하지 않는다.",
            ],
            3,
        ),
        "breakout": (
            f"{P} 14.05만 안착 | 단계 조건 확인",
            [
                f"지금 할 일: {cfg['zone']['breakout']:,}원 위다. 근거 줄의 분할매수 단계 조건이 모두 충족일 때만 산다. 삼성전자·SK하이닉스 가이던스도 본다.",
                f"나중에: {cfg['etf']['hold_price']:,}원 이상이거나 5일 +{cfg['etf']['hold_5d_pct']:.0f}% 급등이면 매수 보류로 바뀐다.",
                f"익절/줄일 때: 52주 고점 {cfg['zone']['high_52w']:,}원 근처는 추격 금지.",
                "하지 말 것: 돌파 당일 일괄 매수하지 않는다.",
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
    test = "--test" in sys.argv
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
    r5 = r10 = None
    try:
        closes = etf_prices(code, now)
    except Exception as exc:
        err.append(f"가격 {exc}")
    try:
        info = etf_info(code)
    except Exception as exc:
        err.append(f"NAV {exc}")
    session = closes[0][0] if closes else None
    vol = closes[0][2] if closes else None
    past_v = [r[2] for r in closes[1:21] if r[2]]
    avg20 = statistics.mean(past_v) if len(past_v) >= 5 else None

    if cfg.get("year_guard") and now.year != int(cfg["year_guard"]):
        print("k091160 year-guard silent")
        return 0

    if not test and session and session == prev.get("session_date") and (prev.get("last_sent_key") or "").startswith(session):
        print(f"session {session} already evaluated; silent")
        return 0

    if session:
        net, tv = [0.0] * 10, [0.0] * 10
        ok5 = ok10 = True
        for key in ("samsung", "hynix"):
            try:
                v, t = foreign_bn(cfg["flow_codes"][key], session, now)
            except Exception as exc:
                err.append(f"수급 {key} {exc}")
                v, t = [], []
            ok5 = ok5 and len(v) >= 5
            ok10 = ok10 and len(v) >= 10
            for i in range(min(10, len(v))):
                net[i] += v[i]
                tv[i] += t[i]
        if ok5 and sum(tv[:5]) > 0:
            f5 = sum(net[:5])
            r5 = f5 / sum(tv[:5]) * 100
        if ok10 and sum(tv[:10]) > 0:
            f10 = sum(net[:10])
            r10 = f10 / sum(tv[:10]) * 100

    close = closes[0][1] if closes else None
    prem = info.get("premium")
    high52 = info.get("high52")
    if close is None or prem is None or high52 is None or f5 is None or r5 is None or len(closes) < 6:
        err.append("필수값 없음")

    base = {
        "updated_at": stamp,
        "session_date": session,
        "close": close,
        "premium_pct": prem,
        "f5_bn": f5,
        "f10_bn": f10,
        "f5_ratio_pct": r5,
        "f10_ratio_pct": r10,
        "volume": vol,
        "vol_avg20": avg20,
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
        if test:
            publish(title.replace(P, P + " 테스트 |", 1), msg, 2)
            return 0
        save({**prev, **base, "fail_sent_date": session or today})
        if send and not dry:
            publish(title, msg, pri)
        elif not send:
            print("duplicate fail; silent")
        return 0

    fund_cfg, fund_txt = merged_fund(cfg["fundamentals"])
    fund = fund_status(fund_cfg, today)
    s1, s2, s3, hold, reb = cfg["stage1"], cfg["stage2"], cfg["stage3"], cfg["hold"], cfg["rebalance"]
    zkind, vol_ok = price_zone(close, vol, avg20, cfg["zone"])
    c5ago = closes[5][1]
    chg5 = (close / c5ago - 1) * 100
    dd = (1 - close / high52) * 100
    last5 = [r[1] for r in closes[:5]]
    low_stopped = close > min(last5)
    fills = cfg.get("position", {}).get("fills") or []
    stages_done = sorted({int(f.get("stage")) for f in fills if f.get("stage")})
    fill1 = next((num(f.get("price")) for f in fills if int(f.get("stage") or 0) == 1), None)
    avg = num(cfg.get("position", {}).get("avg_cost"))

    status = (
        f"종가 {close:,.0f}원 / 5일 {chg5:+.1f}% / 52주 고점 대비 -{dd:.1f}% / "
        f"괴리율 {prem:+.2f}%(15:20 조회값) / 외국인(삼성+하이닉스) 5일 {f5:+,.0f}억(거래대금 대비 {r5:+.1f}%), "
        f"10일 {fmt(f10, '억', 0, True)}({fmt(r10, '%', 1, True)}, 전일까지 확정분)\n"
        f"가격 조건: {zkind} | 거래량 {fmt(vol, '주')} / 20일평 {fmt(avg20, '주')} / {cfg['zone']['vol_mult']}배 "
        f"{'확인' if vol_ok else ('미달' if vol_ok is False else '미확인')}\n펀더멘털: {fund_txt}"
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
        if r5 <= hold["f5_ratio_hold_pct"]:
            reasons.append(f"외국인 5일 순매도가 거래대금의 {hold['f5_ratio_hold_pct']:.0f}% 이하")
        if fund["bad_hold"]:
            reasons.append(f"컨센서스 {fund['trend']} 또는 메모리 가격 전망 하향")
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
                f"외국인 5일 거래대금 대비 {s1['f5_ratio_min_pct']:.0f}% 이상": r5 >= s1["f5_ratio_min_pct"],
                "컨센서스 하향 없음, DRAM 또는 NAND 상승 전망": fund["stage1_ok"],
            }
            kind = "s1" if all(checks.values()) else "wait"
            reasons = [("충족 " if v else "미충족 ") + k for k, v in checks.items()]
        elif stages_done == [1] and fill1:
            zone = close <= fill1 * (1 - s2["drop_from_fill1_pct"] / 100) or close <= s2["price_abs"]
            checks = {
                f"1차가 대비 -{s2['drop_from_fill1_pct']:.0f}% 또는 {s2['price_abs']:,}원 이하": zone,
                f"괴리율 {s2['prem_low']}~+{s2['prem_high']}%": s2["prem_low"] <= prem <= s2["prem_high"],
                f"외국인 10일 거래대금 대비 {s2['f10_ratio_min_pct']:.0f}% 이상": r10 is not None and r10 >= s2["f10_ratio_min_pct"],
                f"DRAM +{s2['dram_min_qoq']:.0f}% 이상, NAND {s2['nand_min_qoq']:.0f}% 이상, HBM 유지": (
                    (fund["dram"] or -99) >= s2["dram_min_qoq"] and (fund["nand"] or -99) >= s2["nand_min_qoq"] and fund["hbm_ok"]
                ),
            }
            kind = "s2" if all(checks.values()) else "wait"
            reasons = [("충족 " if v else "미충족 ") + k for k, v in checks.items()]
        elif stages_done == [1, 2] and fill1:
            zone = (close <= fill1 * (1 - s3["drop_from_fill1_pct"] / 100) or close <= s3["price_abs"]) and low_stopped
            prev_r10 = num(prev.get("f10_ratio_pct"))
            flow_ok = r10 is not None and (r10 >= s3["f10_ratio_min_pct"] or (prev_r10 is not None and r10 > prev_r10))
            checks = {
                f"1차가 대비 -{s3['drop_from_fill1_pct']:.0f}% 또는 {s3['price_abs']:,}원 이하 + 5일 저점 갱신 멈춤": zone,
                f"괴리율 {s3['prem_low']}~+{s3['prem_high']}%": s3["prem_low"] <= prem <= s3["prem_high"],
                "외국인 10일 순매수 또는 순매도 축소": flow_ok,
                "컨센서스 상향 + DRAM/NAND 상승 전망": fund["cons_up"] and fund["mem_up"],
            }
            kind = "s3" if all(checks.values()) else "wait"
            reasons = [("충족 " if v else "미충족 ") + k for k, v in checks.items()]
        else:
            kind = "wait"
            reasons.append("fills 기록 확인 필요 또는 3차까지 완료. 리밸런싱만 본다.")

    stage_kind = kind
    if zkind == "breakdown":
        kind = "breakdown"
    elif kind == "wait" and zkind in {"retest", "retest_thin", "breakout"}:
        kind = zkind
    title, msg, pri = build(kind, stamp, session, status, cfg, " / ".join(reasons))
    print(title)
    print(msg)
    key = f"{session}:{kind}"
    send = kind != prev.get("state") and kind != "wait"
    if test:
        title = title.replace(P, P + " 테스트 |", 1)
        print("test send; state not saved")
        publish(title, msg, 2)
        return 0
    save({**base, "state": kind, "stage": stage_kind, "zone": zkind, "last_sent_key": key, "fail_sent_date": None})
    if not send:
        print(f"state {kind}: unchanged or 관망; silent")
        return 0
    if dry:
        print("dry-run; not sent")
        return 0
    publish(title, msg, pri)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as exc:  # 다른 알림 스크립트 실행을 막지 않는다
        print(f"k091160 unexpected error: {exc!r}", file=sys.stderr)
        raise SystemExit(0)
