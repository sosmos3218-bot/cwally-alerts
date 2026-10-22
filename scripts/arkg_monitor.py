#!/usr/bin/env python3
"""ARKG 위성 비중 판단 변경 조건 알림 (change-only).

morning 슬롯(평일 09:00 KST). 미국 직전 완료 일봉 종가만 본다(진행 중인 봉은 제외).
- 회복: ARKG 완료 종가 >= 56달러(10월 고점대). 재무장: 종가 < 53달러
- 이탈: ARKG 완료 종가 < 46.70달러(10/16 저점). 재무장: 종가 > 48.5달러
- 신고가: 완료 종가가 직전 1년 종가 고점 돌파. 재무장: 그 고점 대비 -5%
- 상대강도: ARKG 20거래일 수익률 - XBI 20거래일 수익률이 +5%p 이상(강세) / -5%p 이하(약세) 구간에 새로 진입
- 상위 3종목(TXG, TWST, TEM) 중 하나라도 완료 종가 기준 하루 ±12% 이상
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
NAME = "arkg"
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
        "recover_armed": True,
        "breakdown_armed": True,
        "high_armed": True,
        "high_ref": None,
        "rs_regime": None,
        "holding_alerted": {},
        "last_bar_date": None,
        "last_close": None,
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
        d = datetime.fromtimestamp(t, tz).date()
        out[d] = float(c)
    # 직전 거래일 일봉 close가 한동안 null로 오므로, 장 마감 뒤 찍힌 regularMarketPrice로 채운다
    meta = res.get("meta") or {}
    rmt, rmp = meta.get("regularMarketTime"), meta.get("regularMarketPrice")
    if rmt and rmp:
        mt = datetime.fromtimestamp(rmt, tz)
        if mt.date() not in out and mt.hour * 60 + mt.minute >= 16 * 60:
            out[mt.date()] = float(rmp)
    today = now_ny.date()
    after_close = now_ny.hour * 60 + now_ny.minute >= 16 * 60 + 10
    rows = sorted((d, c) for d, c in out.items() if d < today or (d == today and after_close))
    return rows


def body(stamp: str, status: str, action: str, later: str, trim: str, avoid: str) -> str:
    return "\n".join(
        [
            f"{stamp} | ARKG | ARK Genomic Revolution ETF",
            "이게 뭐 알림: ARKG 위성 비중 점검 알림(10/22 분석 기준, 핵심 말고 위성용). 10월 고점대 56달러 회복, 10/16 저점 46.70달러 이탈, 52주 종가 신고가, XBI 대비 20일 상대강도 전환, 상위 3종목(TXG, TWST, TEM) 하루 ±12% 중 하나가 새로 켜질 때만 옴. 자동 주문 아님.",
            f"지금 결과/상태: {status}",
            f"지금 할 일: {action}",
            f"나중에: {later}",
            f"익절/줄일 때: {trim}",
            f"하지 말 것: {avoid}",
        ]
    )


TRIM = "종가 46.70달러 이탈이나 XBI 대비 20일 상대수익 -5%p 이하 전환이 오면 위성 비중 축소를 검토. 상위 3종목 중 2곳 이상이 실적에서 가이던스를 내리면 같은 판단."
AVOID = "핵심(코어) 비중으로 키우기, 장중 급등 추격, 저점 이탈 직후 물타기, 한 번에 목표 비중 다 채우기, 이 알림만 보고 주문."


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
    rec, brk, nh, rs_cfg, hold = cfg["recover"], cfg["breakdown"], cfg["new_high"], cfg["rel_strength"], cfg["holdings"]
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

    ark = bars.get(cfg["symbol"], [])
    last_d = ark[-1][0] if ark else None
    close = ark[-1][1] if ark else None
    prev = ark[-2][1] if len(ark) >= 2 else None
    day_pct = pct(close, prev) if close and prev else None

    # 직전 1년 종가 고점(마지막 봉 제외)과 52주 범위
    prior_hi = max(c for _, c in ark[:-1]) if len(ark) > nh["min_bars"] else None
    hi52 = max(c for _, c in ark) if len(ark) >= nh["min_bars"] else None
    lo52 = min(c for _, c in ark) if len(ark) >= nh["min_bars"] else None

    # XBI 대비 상대강도: 두 종목 모두 있는 날짜로 맞춘다
    rs = None
    ark_n = peer_n = None
    pb = dict(bars.get(peer, []))
    common = [d for d, _ in ark if d in pb]
    n = int(rs_cfg["lookback_bars"])
    if len(common) > n:
        ad = dict(ark)
        d0, d1 = common[-n - 1], common[-1]
        ark_n, peer_n = pct(ad[d1], ad[d0]), pct(pb[d1], pb[d0])
        rs = ark_n - peer_n
    elif ark and peer not in [e.split(":")[0] for e in errs]:
        errs.append(f"{peer}: 상대강도 계산용 공통 일봉 {len(common)}개 부족")

    # 상위 3종목 당일 변동 (ARKG 마지막 봉과 같은 날짜만)
    moves: dict[str, tuple[date, float]] = {}
    for sym in hold_syms:
        r = bars.get(sym, [])
        if len(r) >= 2 and r[-1][0] == last_d:
            moves[sym] = (r[-1][0], pct(r[-1][1], r[-2][1]))

    if close is not None:
        a_txt = f"ARKG 종가 ${close:,.2f}({last_d:%m/%d} 미국)"
        if day_pct is not None:
            a_txt += f", 전일 대비 {day_pct:+.2f}%"
        if hi52 and lo52:
            a_txt += f", 종가 기준 52주 ${lo52:,.2f}~${hi52:,.2f}(고점 대비 {pct(close, hi52):+.1f}%)"
    else:
        a_txt = "ARKG 조회 실패"
    r_txt = (f"20거래일 ARKG {ark_n:+.1f}% vs {peer} {peer_n:+.1f}% = {rs:+.1f}%p" if rs is not None
             else f"{peer} 대비 상대강도 계산 불가")
    m_txt = ", ".join(f"{s} {mv:+.1f}%" for s, (_, mv) in moves.items()) or "상위 3종목 당일 변동 확인 불가"
    status = f"{a_txt}. {r_txt}. 상위 3종목: {m_txt}."
    if errs and close is not None:
        status += " 일부 조회 실패: " + "; ".join(errs) + "."

    is_recover = bool(close is not None and close >= rec["close_min"])
    is_break = bool(close is not None and close < brk["close_max"])
    is_high = bool(close is not None and prior_hi is not None and close > prior_hi)
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
        flags = [
            "56달러 회복 충족" if is_recover else "56달러 회복 미충족",
            "46.70달러 이탈 충족" if is_break else "46.70달러 이탈 미충족",
            "52주 종가 신고가" if is_high else "신고가 아님",
            f"XBI 대비 {reg_label}",
            ("상위 종목 급변 " + ", ".join(shocks)) if shocks else "상위 종목 급변 없음",
        ]
        publish(
            topic,
            "[ARKG] 설치 확인 | 매매 신호 아님",
            body(stamp, f"평일 09:00 KST morning 슬롯에 붙였습니다. {status} 지금 판정: {', '.join(flags)}.",
                 "설치 확인만 하세요. 이 알림으로 사거나 팔지 마세요.",
                 "조건이 새로 켜질 때만 옵니다. 상대강도는 첫 실행 때 기준만 기록하고 그다음 전환부터 보냅니다. 조회 실패는 cwally-ops로 갑니다.",
                 TRIM, "설치 확인을 매수나 매도 신호로 읽기."),
            3, ["information_source"], server,
        )
        print("install confirmation sent")
        return 0

    dry = mode == "dry"
    state = load_state()
    first = state.get("updated_at") is None
    alerts: list[tuple[str, str, int, list[str]]] = []
    print(f"close={close} day_pct={day_pct} prior_hi={prior_hi} rs={rs} regime_now={regime_now} moves={moves} errs={errs}")

    if close is None:
        if state.get("fetch_fail_date") != today_kst:
            alerts.append((
                "[ARKG] 조회 실패 | 조건 미확인",
                body(stamp, status + " 조건을 확인하지 못했습니다.", "증권사 앱에서 ARKG, XBI 종가를 직접 보세요.",
                     "다음 평일 09:00에 다시 봅니다.", "이번 알림으로 비중을 바꾸지 마세요.", "조회 실패를 조건 충족이나 정상으로 읽기."),
                3, ["warning"],
            ))
            state["fetch_fail_date"] = today_kst
    else:
        if is_break and state.get("breakdown_armed", True):
            alerts.append((
                "[ARKG] 10/16 저점 이탈 | 신규 편입 보류",
                body(stamp, status + f" 종가가 {brk['close_max']:.2f}달러(10/16 저점) 아래로 내려왔습니다. 10월 조정이 추세로 바뀌는 신호일 수 있습니다.",
                     "신규 편입은 멈추세요. 보유 중이면 미리 정한 손절 기준대로 점검하고, TXG, TWST, TEM 중 어느 종목이 끌어내렸는지 확인.",
                     f"종가가 {brk['rearm_close']:.1f}달러 위로 돌아오면 이 조건이 다시 무장됩니다. 3분기 실적 시즌(10월 말~11월 초) 이후 재평가.",
                     TRIM, AVOID),
                4, ["chart_with_downwards_trend"],
            ))
            state["breakdown_armed"] = False
        if is_recover and state.get("recover_armed", True):
            reg_hint = {"lead": "XBI 대비 강세라 신호가 받쳐집니다.", "lag": "다만 XBI 대비로는 약세라 신호가 약합니다."}.get(regime_now or "", "")
            alerts.append((
                "[ARKG] 10월 고점대 회복 | 모멘텀 재개 후보",
                body(stamp, status + f" 종가가 {rec['close_min']:.0f}달러(10월 고점대) 위로 올라왔습니다. {reg_hint}".rstrip(),
                     "XBI 대비 상대강도와 상위 3종목 뉴스를 확인. 둘 다 받쳐주면 미리 정한 위성 한도 안에서 분할 편입 검토. 미국 정규장 지정가, 개장 직후 30분은 피하기.",
                     f"52주 종가 고점을 넘으면 별도 알림이 옵니다. 종가가 {rec['rearm_close']:.0f}달러 아래로 내려가면 이 조건이 다시 무장됩니다.",
                     TRIM, AVOID),
                4, ["chart_with_upwards_trend"],
            ))
            state["recover_armed"] = False
        if is_high and state.get("high_armed", True):
            alerts.append((
                "[ARKG] 52주 종가 신고가 | 추격 말고 분할만",
                body(stamp, status + f" 직전 1년 종가 고점 ${prior_hi:,.2f}를 넘었습니다.",
                     "추격 매수 대신 이미 정한 분할 계획만 이어가세요. 위성 한도를 넘었으면 일부 덜어낼 기준부터 정하기.",
                     f"그 고점 대비 -{nh['rearm_drop_pct']:.0f}% 아래로 내려가면 이 조건이 다시 무장됩니다.",
                     TRIM, AVOID),
                4, ["trophy"],
            ))
            state["high_armed"] = False
            state["high_ref"] = prior_hi

        prev_regime = state.get("rs_regime")
        if regime_now in ("lead", "lag", "neutral") and regime_now != prev_regime:
            if not first and regime_now == "lead":
                alerts.append((
                    f"[ARKG] {peer} 대비 강세 전환 | 테마 모멘텀 재확인",
                    body(stamp, status + f" 20거래일 상대수익이 +{rs_cfg['lead_pct']:.0f}%p 이상으로 올라섰습니다.",
                         "가격 조건(56달러 회복)과 함께 보세요. 가격 조건 없이 상대강도만으로 편입하지 않기.",
                         f"±{rs_cfg['reset_band_pct']:.0f}%p 안으로 돌아오면 중립으로 리셋되고, -{abs(rs_cfg['lag_pct']):.0f}%p 이하면 약세 전환 알림이 옵니다.",
                         TRIM, AVOID),
                    3, ["chart_with_upwards_trend"],
                ))
            elif not first and regime_now == "lag":
                alerts.append((
                    f"[ARKG] {peer} 대비 약세 전환 | 비중 확대 보류",
                    body(stamp, status + f" 20거래일 상대수익이 {rs_cfg['lag_pct']:.0f}%p 이하로 내려갔습니다. 유전체 테마가 바이오 전체보다 약해지고 있다는 뜻입니다.",
                         "위성 비중 확대는 보류. 보유 중이면 미리 정한 축소 기준을 다시 확인.",
                         f"±{rs_cfg['reset_band_pct']:.0f}%p 안으로 돌아오면 중립으로 리셋되고, +{rs_cfg['lead_pct']:.0f}%p 이상이면 강세 전환 알림이 옵니다.",
                         TRIM, AVOID),
                    3, ["chart_with_downwards_trend"],
                ))
            state["rs_regime"] = regime_now

        sent_map = dict(state.get("holding_alerted") or {})
        new_shocks = {s: mv for s, mv in shocks.items() if sent_map.get(s) != moves[s][0].isoformat()}
        if new_shocks:
            impact = sum(float(hold["weights_pct"][s]) * mv / 100 for s, mv in new_shocks.items())
            parts = ", ".join(f"{s} {mv:+.1f}%" for s, mv in new_shocks.items())
            detail = ", ".join(f"{hold['names'][s]}({s}) 비중 {hold['weights_pct'][s]:.2f}%" for s in new_shocks)
            alerts.append((
                f"[ARKG] 상위 종목 급변 | {parts}",
                body(stamp, status + f" {detail}. ARKG 영향 추정 {impact:+.2f}%p(비중 x 변동).",
                     "그 종목의 실적, 임상, 가이던스 뉴스를 먼저 확인. 일회성인지 추세 변화인지 가려지기 전에는 ARKG 비중을 바꾸지 않기.",
                     "같은 봉 날짜는 한 번만 알립니다. 2곳 이상이 가이던스를 내리면 위성 비중 축소 검토 조건이 됩니다.",
                     TRIM, AVOID),
                4, ["rotating_light"],
            ))
            for s in new_shocks:
                sent_map[s] = moves[s][0].isoformat()
            state["holding_alerted"] = sent_map

        if not state.get("recover_armed", True) and not is_recover and close < rec["rearm_close"]:
            state["recover_armed"] = True
        if not state.get("breakdown_armed", True) and not is_break and close > brk["rearm_close"]:
            state["breakdown_armed"] = True
        ref = state.get("high_ref")
        if not state.get("high_armed", True) and not is_high and ref and close < ref * (1 - nh["rearm_drop_pct"] / 100):
            state["high_armed"] = True
        state.update(last_bar_date=last_d.isoformat(), last_close=close,
                     last_rs_pct=(round(rs, 2) if rs is not None else None), fetch_fail_date=None)

    for title, msg, pri, tags in alerts:
        print(title)
        print(msg)
        if not dry:
            publish(topic, title, msg, pri, tags, server)
    if not alerts:
        print(f"no alert (recover={is_recover} break={is_break} high={is_high} regime={regime_now} shocks={shocks} first={first})")
    if dry:
        print("DRY RUN: no publish, state not written")
        return 0
    state["updated_at"] = now.isoformat()
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
