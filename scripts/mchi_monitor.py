#!/usr/bin/env python3
"""MCHI 소액 분할매수 판단 변경 조건 알림 (change-only).

morning 슬롯(평일 09:00 KST). 미국 직전 완료 일봉 종가만 본다(진행 중인 봉은 제외).
- 이탈: MCHI 완료 종가 < 49.84달러(52주 저가). 재무장: 종가 > 52달러
- 추세 반전: 완료 종가가 200일 이동평균 위로 새로 올라섬. 재무장: 평균 대비 -3%
- 상대강도: MCHI 20거래일 수익률 - SPY 20거래일 수익률이 +5%p 이상(강세) / -5%p 이하(약세) 구간에 새로 진입
- 상위 2종목(텐센트 TCEHY, 알리바바 BABA) 중 하나라도 완료 종가 기준 하루 ±7% 이상
실적, 내수, FCF 개선은 가격 알림이 못 본다. 추가 매수 판단은 직접 확인.
modes: auto(기본) | dry(발송 없음, state 안 씀) | install(설치 확인 1회, state 안 씀)
"""
from __future__ import annotations

import json
import sys
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic  # noqa: E402

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore[assignment]

KST = timezone(timedelta(hours=9))
NAME = "mchi"
CFG_PATH = ROOT / "configs" / f"{NAME}.json"
STATE_PATH = ROOT / "state" / f"{NAME}.json"
UA = {"User-Agent": "Mozilla/5.0 (cwally-alerts; +mailto:sosmos3218@gmail.com)"}


def ny_now() -> datetime:
    if ZoneInfo is not None:
        try:
            return datetime.now(ZoneInfo("America/New_York"))
        except Exception:  # noqa: BLE001
            pass
    off = -5 if datetime.now(timezone.utc).month in (11, 12, 1, 2, 3) else -4
    return datetime.now(timezone(timedelta(hours=off)))


def load_state() -> dict:
    base = {
        "updated_at": None,
        "breakdown_armed": True,
        "reclaim_armed": True,
        "above_ma": None,
        "rs_regime": None,
        "holding_alerted": {},
        "last_bar_date": None,
        "last_close": None,
        "last_ma": None,
        "last_rs_pct": None,
        "fetch_fail_date": None,
    }
    if STATE_PATH.exists():
        try:
            base.update(json.loads(STATE_PATH.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass
    return base


def fetch_bars(symbol: str, now_ny: datetime) -> list[tuple[date, float]]:
    """완료 일봉 (미국 동부 날짜, 종가) 오름차순. 진행 중인 오늘 봉은 뺀다."""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=1y&interval=1d"
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=20) as resp:
        res = json.loads(resp.read().decode())["chart"]["result"][0]
    tz = now_ny.tzinfo
    out: dict[date, float] = {}
    for t, c in zip(res.get("timestamp") or [], res["indicators"]["quote"][0].get("close") or []):
        if c is None:
            continue
        out[datetime.fromtimestamp(t, tz).date()] = float(c)
    # 직전 거래일 일봉 close가 한동안 null로 오므로, 장 마감 뒤 찍힌 regularMarketPrice로 채운다
    meta = res.get("meta") or {}
    rmt, rmp = meta.get("regularMarketTime"), meta.get("regularMarketPrice")
    if rmt and rmp:
        mt = datetime.fromtimestamp(rmt, tz)
        if mt.date() not in out and mt.hour * 60 + mt.minute >= 16 * 60:
            out[mt.date()] = float(rmp)
    today = now_ny.date()
    after_close = now_ny.hour * 60 + now_ny.minute >= 16 * 60 + 10
    return sorted((d, c) for d, c in out.items() if d < today or (d == today and after_close))


def body(stamp: str, status: str, action: str, later: str, trim: str, avoid: str) -> str:
    return "\n".join(
        [
            f"{stamp} | MCHI | iShares MSCI China ETF",
            "이게 뭐 알림: MCHI 소액 분할매수 점검 알림(10/22 분석 기준, 큰 비중 말고 위성 2~3%). 52주 저가 49.84달러 이탈, 200일 이동평균 회복, SPY 대비 20일 상대강도 전환, 상위 2종목(텐센트, 알리바바) 하루 ±7% 중 하나가 새로 켜질 때만 옴. 실적과 내수는 이 알림이 못 보니 직접 확인. 자동 주문 아님.",
            f"지금 결과/상태: {status}",
            f"지금 할 일: {action}",
            f"나중에: {later}",
            f"익절/줄일 때: {trim}",
            f"하지 말 것: {avoid}",
        ]
    )


TRIM = "종가 49.84달러 이탈이 이어지는데 텐센트와 알리바바 현금흐름(FCF) 적자도 안 줄면 위성 비중 축소를 검토. 소비 회복이 확인되기 전 반등은 비중을 키울 근거가 아님."
AVOID = "가격만 싸다고 물타기, 한 번에 목표 비중 다 채우기, 코어 비중으로 키우기, 이 알림만 보고 주문."
LATER_EARN = "텐센트와 알리바바 다음 분기 실적에서 FCF 적자가 줄었는지, 중국 소매판매가 살아나는지 직접 확인."


def pct(a: float, b: float) -> float:
    return (a / b - 1) * 100


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    now_ny = ny_now()
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    today_kst = now.strftime("%Y%m%d")
    topic = resolve_topic("port", cfg.get("ntfy_topic"))
    server = resolve_server(cfg.get("ntfy_server"))
    brk, ma_cfg, rs_cfg, hold = cfg["breakdown"], cfg["ma_reclaim"], cfg["rel_strength"], cfg["holdings"]
    peer = rs_cfg["peer"]
    hold_syms = list(hold["weights_pct"].keys())

    end = cfg.get("ends_kst")
    if mode != "install" and end and now > datetime.strptime(end, "%Y-%m-%d %H:%M").replace(tzinfo=KST):
        print(f"ended at {end}; silent")
        return 0

    errs: list[str] = []
    bars: dict[str, list[tuple[date, float]]] = {}
    for sym in [cfg["symbol"], peer, *hold_syms]:
        try:
            rows = fetch_bars(sym, now_ny)
            if len(rows) < 3:
                raise RuntimeError(f"{sym} 완료 일봉 {len(rows)}개")
            if (now_ny.date() - rows[-1][0]).days > int(cfg["stale_days"]):
                raise RuntimeError(f"{sym} 마지막 봉 {rows[-1][0]} 너무 오래됨")
            bars[sym] = rows
        except Exception as exc:  # noqa: BLE001
            errs.append(f"{sym}: {exc}")

    px = bars.get(cfg["symbol"], [])
    last_d = px[-1][0] if px else None
    close = px[-1][1] if px else None
    prev = px[-2][1] if len(px) >= 2 else None
    day_pct = pct(close, prev) if close and prev else None
    lo52 = min(c for _, c in px) if len(px) >= 200 else None

    win = int(ma_cfg["window"])
    ma = sum(c for _, c in px[-win:]) / win if len(px) >= int(ma_cfg["min_bars"]) else None

    # SPY 대비 상대강도: 두 종목 모두 있는 날짜로 맞춘다
    rs = None
    mchi_n = peer_n = None
    pb = dict(bars.get(peer, []))
    common = [d for d, _ in px if d in pb]
    n = int(rs_cfg["lookback_bars"])
    if len(common) > n:
        md = dict(px)
        d0, d1 = common[-n - 1], common[-1]
        mchi_n, peer_n = pct(md[d1], md[d0]), pct(pb[d1], pb[d0])
        rs = mchi_n - peer_n
    elif px and peer not in [e.split(":")[0] for e in errs]:
        errs.append(f"{peer}: 상대강도 계산용 공통 일봉 {len(common)}개 부족")

    # 상위 2종목 당일 변동 (MCHI 마지막 봉과 같은 날짜만)
    moves: dict[str, tuple[date, float]] = {}
    for sym in hold_syms:
        r = bars.get(sym, [])
        if len(r) >= 2 and r[-1][0] == last_d:
            moves[sym] = (r[-1][0], pct(r[-1][1], r[-2][1]))

    if close is not None:
        a_txt = f"MCHI 종가 ${close:,.2f}({last_d:%m/%d} 미국)"
        if day_pct is not None:
            a_txt += f", 전일 대비 {day_pct:+.2f}%"
        if lo52:
            a_txt += f", 종가 기준 52주 저가 ${lo52:,.2f}(저가 대비 {pct(close, lo52):+.1f}%)"
        if ma:
            a_txt += f", 200일 평균 ${ma:,.2f}(평균 대비 {pct(close, ma):+.1f}%)"
    else:
        a_txt = "MCHI 조회 실패"
    r_txt = (f"20거래일 MCHI {mchi_n:+.1f}% vs {peer} {peer_n:+.1f}% = {rs:+.1f}%p" if rs is not None
             else f"{peer} 대비 상대강도 계산 불가")
    m_txt = ", ".join(f"{hold['names'][s]} {mv:+.1f}%" for s, (_, mv) in moves.items()) or "상위 2종목 당일 변동 확인 불가"
    status = f"{a_txt}. {r_txt}. 상위 2종목: {m_txt}."
    if errs and close is not None:
        status += " 일부 조회 실패: " + "; ".join(errs) + "."

    is_break = bool(close is not None and close < brk["close_max"])
    is_above = None if (close is None or ma is None) else bool(close > ma)
    if rs is None:
        regime_now = None
    elif rs >= rs_cfg["lead_pct"]:
        regime_now = "lead"
    elif rs <= rs_cfg["lag_pct"]:
        regime_now = "lag"
    elif abs(rs) <= rs_cfg["reset_band_pct"]:
        regime_now = "neutral"
    else:
        regime_now = "hold"  # 히스테리시스 구간: 이전 상태 유지
    shocks = {s: mv for s, (_, mv) in moves.items() if abs(mv) >= float(hold["day_move_pct"])}

    if mode == "install":
        reg_label = {"lead": "강세", "lag": "약세", "neutral": "중립", "hold": "중간 구간"}.get(regime_now or "", "계산 불가")
        ma_label = {True: "200일 평균 위", False: "200일 평균 아래", None: "200일 평균 계산 불가"}[is_above]
        flags = [
            "49.84달러 이탈 충족" if is_break else "49.84달러 이탈 미충족",
            ma_label,
            f"SPY 대비 {reg_label}",
            ("상위 종목 급변 " + ", ".join(shocks)) if shocks else "상위 종목 급변 없음",
        ]
        publish(
            topic,
            "[MCHI] 설치 확인 | 매매 신호 아님",
            body(stamp, f"평일 09:00 KST morning 슬롯에 붙였습니다. {status} 지금 판정: {', '.join(flags)}.",
                 "설치 확인만 하세요. 이 알림으로 사거나 팔지 마세요.",
                 "조건이 새로 켜질 때만 옵니다. 200일 평균과 상대강도는 첫 실행 때 기준만 기록하고 그다음 전환부터 보냅니다. 조회 실패는 cwally-ops로 갑니다.",
                 TRIM, "설치 확인을 매수나 매도 신호로 읽기."),
            3, ["information_source"], server,
        )
        print("install confirmation sent")
        return 0

    dry = mode == "dry"
    state = load_state()
    first = state.get("updated_at") is None
    alerts: list[tuple[str, str, int, list[str]]] = []
    print(f"close={close} day_pct={day_pct} ma={ma} above={is_above} rs={rs} regime_now={regime_now} moves={moves} errs={errs}")

    if close is None:
        if state.get("fetch_fail_date") != today_kst:
            alerts.append((
                "[MCHI] 조회 실패 | 조건 미확인",
                body(stamp, status + " 조건을 확인하지 못했습니다.", "증권사 앱에서 MCHI, SPY 종가를 직접 보세요.",
                     "다음 평일 09:00에 다시 봅니다.", "이번 알림으로 비중을 바꾸지 마세요.", "조회 실패를 조건 충족이나 정상으로 읽기."),
                3, ["warning"],
            ))
            state["fetch_fail_date"] = today_kst
    else:
        if is_break and state.get("breakdown_armed", True):
            alerts.append((
                "[MCHI] 52주 저가 이탈 | 추가 매수 보류",
                body(stamp, status + f" 종가가 {brk['close_max']:.2f}달러(52주 저가) 아래로 내려왔습니다. 싸졌다는 뜻이 아니라 바닥이 확인되지 않았다는 신호입니다.",
                     "추가 매수는 멈추세요. 이미 산 소액은 그대로 두고, 텐센트와 알리바바 쪽 악재(실적, 규제)가 있는지만 확인.",
                     f"종가가 {brk['rearm_close']:.0f}달러 위로 돌아오면 이 조건이 다시 무장됩니다. {LATER_EARN}",
                     TRIM, AVOID),
                4, ["chart_with_downwards_trend"],
            ))
            state["breakdown_armed"] = False

        prev_above = state.get("above_ma")
        if is_above and prev_above is False and state.get("reclaim_armed", True) and not first:
            alerts.append((
                "[MCHI] 200일 평균 회복 | 추세 반전 1차 확인",
                body(stamp, status + " 종가가 200일 이동평균 위로 올라섰습니다. 가격 면에서 바닥 후보이지만 실적 근거는 아직 별개입니다.",
                     "소액 분할의 2차분은 가격이 아니라 실적으로 정하세요. 텐센트와 알리바바 FCF, 중국 소매판매를 먼저 확인하고 둘 중 하나라도 좋아졌으면 계획한 분할분만 진행.",
                     f"평균 대비 -{ma_cfg['rearm_below_pct']:.0f}% 아래로 내려가면 이 조건이 다시 무장됩니다. {LATER_EARN}",
                     TRIM, AVOID),
                4, ["chart_with_upwards_trend"],
            ))
            state["reclaim_armed"] = False
        if is_above is not None:
            state["above_ma"] = is_above

        prev_regime = state.get("rs_regime")
        if regime_now in ("lead", "lag", "neutral") and regime_now != prev_regime:
            if not first and regime_now == "lead":
                alerts.append((
                    f"[MCHI] {peer} 대비 강세 전환 | 중국 반등 확인 중",
                    body(stamp, status + f" 20거래일 상대수익이 +{rs_cfg['lead_pct']:.0f}%p 이상으로 올라섰습니다.",
                         "가격 반등만으로 비중을 키우지 말고 실적과 소비 지표가 따라오는지 확인. 계획한 분할 한도 안에서만 움직이기.",
                         f"±{rs_cfg['reset_band_pct']:.0f}%p 안으로 돌아오면 중립으로 리셋되고, -{abs(rs_cfg['lag_pct']):.0f}%p 이하면 약세 전환 알림이 옵니다.",
                         TRIM, AVOID),
                    3, ["chart_with_upwards_trend"],
                ))
            elif not first and regime_now == "lag":
                alerts.append((
                    f"[MCHI] {peer} 대비 약세 전환 | 비중 확대 보류",
                    body(stamp, status + f" 20거래일 상대수익이 {rs_cfg['lag_pct']:.0f}%p 이하로 내려갔습니다. 미국 대비 중국이 더 약해지고 있다는 뜻입니다.",
                         "추가 매수는 보류. 이미 산 소액이 위성 한도(전체 2~3%)를 넘지 않았는지만 확인.",
                         f"±{rs_cfg['reset_band_pct']:.0f}%p 안으로 돌아오면 중립으로 리셋되고, +{rs_cfg['lead_pct']:.0f}%p 이상이면 강세 전환 알림이 옵니다.",
                         TRIM, AVOID),
                    3, ["chart_with_downwards_trend"],
                ))
            state["rs_regime"] = regime_now

        sent_map = dict(state.get("holding_alerted") or {})
        new_shocks = {s: mv for s, mv in shocks.items() if sent_map.get(s) != moves[s][0].isoformat()}
        if new_shocks:
            impact = sum(float(hold["weights_pct"][s]) * mv / 100 for s, mv in new_shocks.items())
            parts = ", ".join(f"{hold['names'][s].split('(')[0]} {mv:+.1f}%" for s, mv in new_shocks.items())
            detail = ", ".join(f"{hold['names'][s]} 비중 {hold['weights_pct'][s]:.2f}%" for s in new_shocks)
            alerts.append((
                f"[MCHI] 상위 종목 급변 | {parts}",
                body(stamp, status + f" {detail}. MCHI 영향 추정 {impact:+.2f}%p(비중 x 변동).",
                     "그 종목의 실적, 규제, AI 투자 뉴스를 먼저 확인. 일회성인지 추세 변화인지 가려지기 전에는 MCHI 비중을 바꾸지 않기.",
                     f"같은 봉 날짜는 한 번만 알립니다. {LATER_EARN}",
                     TRIM, AVOID),
                4, ["rotating_light"],
            ))
            for s in new_shocks:
                sent_map[s] = moves[s][0].isoformat()
            state["holding_alerted"] = sent_map

        if not state.get("breakdown_armed", True) and not is_break and close > brk["rearm_close"]:
            state["breakdown_armed"] = True
        if (not state.get("reclaim_armed", True) and ma is not None
                and close < ma * (1 - ma_cfg["rearm_below_pct"] / 100)):
            state["reclaim_armed"] = True
        state.update(last_bar_date=last_d.isoformat(), last_close=close,
                     last_ma=(round(ma, 2) if ma is not None else None),
                     last_rs_pct=(round(rs, 2) if rs is not None else None), fetch_fail_date=None)

    for title, msg, pri, tags in alerts:
        print(title)
        print(msg)
        if not dry:
            publish(topic, title, msg, pri, tags, server)
    if not alerts:
        print(f"no alert (break={is_break} above_ma={is_above} regime={regime_now} shocks={shocks} first={first})")
    if dry:
        print("DRY RUN: no publish, state not written")
        return 0
    state["updated_at"] = now.isoformat()
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
