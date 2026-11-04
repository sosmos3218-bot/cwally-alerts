#!/usr/bin/env python3
"""[SDI] 006400/006405: common/pref discount bands + common change-condition verdict, one alert.

Merged 2026-11-04 (pt11u) from [SDI-PREF] sdipref (19:00 discount zones) and the standalone sdichg
runs (09:00 verdict). Existing k203780 19:00 slot on KRX trading days, no new cron.
Event-only: send when the band or the sdichg verdict changes. No weekly repeat.
Discount = (1 - 006405 close / 006400 close) x 100 from same-day Daum KRX closes (no NXT, no Naver).
Bands with ~1%p hysteresis; target = pref share of the whole SDI position (never 100% pref).
Verdict = scripts/sdichg_monitor.evaluate_verdict until 2027-01-15 09:00, then bands only.
Modes: auto, dry (no publish, no state), selftest.
"""
from __future__ import annotations

import copy
import json
import re
import sys
import urllib.request
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from kr_calendar import KST, is_trading_day, session_day  # noqa: E402
from ntfy_util import OPS_TITLE_RE, publish, resolve_server  # noqa: E402

CFG_PATH = ROOT / "configs" / "sdimix.json"
STATE_PATH = ROOT / "state" / "sdimix.json"
TAG = "[SDI]"
PAIR = "006400/006405"
BANDS = ("Z_LOW", "Z_MID", "Z_HIGH1", "Z_HIGH2")
LABEL = {"Z_LOW": "우선주 상대 고평가", "Z_MID": "중립", "Z_HIGH1": "우선주 할인 확대", "Z_HIGH2": "우선주 할인 극단"}
REDUCE = "축소 점검"
UPGRADE = "분할매수 검토"
FORBIDDEN = ("\u00b7", "\u2014")  # middle dot, em-dash


# ---------- data ----------
def daum_rows(code: str, per: int = 760) -> dict[str, dict]:
    url = f"https://finance.daum.net/api/quote/A{code}/days?symbolCode=A{code}&page=1&perPage={per}&pagination=true"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Referer": f"https://finance.daum.net/quotes/A{code}"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = json.loads(resp.read().decode("utf-8"))
    out: dict[str, dict] = {}
    for r in raw.get("data") or []:
        if str(r.get("symbolCode")) != "A" + code:
            raise ValueError("종목 코드 불일치")
        day = str(r.get("date") or "")[:10]
        val = r.get("tradePrice")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day) or val is None:
            continue
        price = Decimal(str(val))
        if not price.is_finite() or price <= 0:
            raise ValueError("가격 오류")
        out[day] = {"close": price, "value": r.get("accTradePrice")}
    if not out:
        raise ValueError("KRX 일별 시세 없음")
    return out


def discount(c: Decimal, p: Decimal) -> Decimal:
    return (Decimal(1) - Decimal(str(p)) / Decimal(str(c))) * 100


def same_session(common: dict, pref: dict, day: str) -> tuple[Decimal, Decimal, Decimal]:
    if day not in common or day not in pref:
        raise ValueError(f"{day} 양 종목 KRX 종가 미확인. 지난 거래일이나 NXT로 대체하지 않음")
    c, p = common[day]["close"], pref[day]["close"]
    return c, p, discount(c, p)


def percentile(common: dict, pref: dict, day: str, d: Decimal) -> tuple[float | None, int]:
    start = (date.fromisoformat(day) - timedelta(days=365 * 3)).isoformat()
    days = [x for x in common if x in pref and start <= x <= day]
    if len(days) < 250:
        return None, len(days)
    ser = [discount(common[x]["close"], pref[x]["close"]) for x in days]
    return 100.0 * sum(1 for v in ser if v <= d) / len(ser), len(ser)


def avg_value_eok(rows: dict, day: str, n: int = 20) -> float | None:
    days = sorted(x for x in rows if x <= day)[-n:]
    vals = [rows[x].get("value") for x in days]
    if len(vals) < n or any(v is None for v in vals):
        return None
    return sum(float(v) for v in vals) / n / 1e8


# ---------- bands ----------
def band_of(d: Decimal, prev: str | None, cfg: dict) -> str:
    b = cfg["bands"]
    lo, h1, h2 = (Decimal(str(b[k])) for k in ("low_pct", "high1_pct", "high2_pct"))
    hy = Decimal(str(b.get("hysteresis_pp", 1)))
    if d >= h2 or (prev == "Z_HIGH2" and d >= h2 - hy):
        return "Z_HIGH2"
    if d >= h1 or (prev in ("Z_HIGH1", "Z_HIGH2") and d >= h1 - hy):
        return "Z_HIGH1"
    if d <= lo or (prev == "Z_LOW" and d <= lo + hy):
        return "Z_LOW"
    return "Z_MID"


def verdict_active(cfg: dict, now: datetime) -> bool:
    return now.strftime("%Y-%m-%d %H:%M") <= cfg["verdict_ends_kst"]


def decide(state: dict, day: str, d: Decimal, verdict: str | None, cfg: dict) -> tuple[dict, bool, str]:
    """New state, send?, reason. verdict None = verdict part ended (bands only)."""
    new = copy.deepcopy(state)
    if state.get("last_date", "") >= day:
        return new, False, "same data day"
    band = band_of(d, state.get("band"), cfg)
    reasons = []
    if state.get("band") is None:
        new.update(last_date=day, band=band, discount_pct=str(d))
        if verdict is not None:
            new["verdict"] = verdict
        return new, False, "seed"
    if band != state.get("band"):
        reasons.append(f"구간 {state.get('band')} -> {band}")
    if verdict is not None and verdict != state.get("verdict"):
        reasons.append(f"판정 {state.get('verdict')} -> {verdict}")
    new.update(last_date=day, band=band, discount_pct=str(d))
    if verdict is not None:
        new["verdict"] = verdict
    return new, bool(reasons), ", ".join(reasons) or "no change"


# ---------- copy ----------
def short_action(band: str, verdict: str | None, cfg: dict) -> str:
    if verdict == REDUCE:
        return "우선주 이동 보류, 둘 다 축소 검토"
    t = cfg["targets_pref_pct"]
    return {
        "Z_LOW": "우선주를 보통주로 환원 검토",
        "Z_MID": "현재 비중 유지",
        "Z_HIGH1": f"우선주 비중 {t['Z_HIGH1']}% 검토",
        "Z_HIGH2": f"우선주 비중 최대 {t['Z_HIGH2']}% 검토",
    }[band]


def title_for(band: str, d: Decimal, verdict: str | None, cfg: dict) -> str:
    tail = f"보통주 판정 {verdict}" if verdict is not None else "괴리율 구간만"
    return f"[SDI] 006400/006405 괴리율 {d:.1f}% {short_action(band, verdict, cfg)} | {tail}"


def todo_line(band: str, verdict: str | None, cfg: dict, value_eok: float | None) -> str:
    t = cfg["targets_pref_pct"]
    cap = cfg["liquidity"]["max_day_share_pct"]
    liq = (f"우선주 20일 평균 거래대금 약 {value_eok:.1f}억원이라 하루 주문은 그 {cap}% 안쪽, 며칠에 나눠 지정가로."
           if value_eok is not None else f"우선주 거래대금을 앱에서 먼저 보고, 하루 주문은 그날 거래대금의 {cap}% 안쪽, 지정가로.")
    if verdict == REDUCE:
        return ("보통주 판정이 축소 점검이라 괴리율 구간과 상관없이 우선주로 옮기지 마세요. "
                "보통주와 우선주 모두 줄일지 공시 원문과 비중을 보고 검토하세요. 매도 지시가 아닙니다. " + liq)
    if band == "Z_LOW":
        body = (f"우선주 목표 비중 {t['Z_LOW']}%. 할인이 줄어 우선주가 상대적으로 비쌉니다. "
                "갖고 있는 우선주를 보통주로 되돌리는 것을 검토하세요(SDI 총 보유는 그대로). ")
    elif band == "Z_MID":
        body = "새 전환 없이 지금 보통주와 우선주 비중을 그대로 두세요. "
    elif band == "Z_HIGH1":
        body = (f"우선주 목표 비중은 SDI 전체 보유의 약 {t['Z_HIGH1']}%. 지금 우선주가 그보다 적으면 "
                f"보통주 일부를 우선주로 나눠 옮기세요(전면 전환 아님, 보통주 약 {100 - t['Z_HIGH1']}% 유지). ")
    else:
        body = (f"우선주 목표 비중 최대 {t['Z_HIGH2']}%(보통주 {100 - t['Z_HIGH2']}% 이상 유지). "
                f"{t['Z_HIGH1']}%에서 더 옮긴다면 나눠서, 그 이상은 늘리지 마세요. ")
    if verdict == UPGRADE:
        body += "보통주 판정이 분할매수 검토라 추가 매수를 한다면 한 번에 말고 나눠서 하세요. "
    return body + liq


def body_for(cfg: dict, day: str, c: Decimal, p: Decimal, d: Decimal, band: str, pct: float | None,
             nhist: int, verdict: str | None, vinfo: dict | None, value_eok: float | None, reason: str) -> str:
    b = cfg["bands"]
    h = cfg["history_3y"]
    pct_txt = f"최근 3년({nhist}거래일) 백분위 {pct:.1f}%" if pct is not None else "3년 백분위 미확인"
    if verdict is None:
        vline = "보통주 변경 조건 판정은 2027-01-15 09:00 에 끝나 이제 괴리율 구간만 봅니다."
    else:
        why = (vinfo or {}).get("why", "")
        vline = f"보통주 판정 {verdict} ({why})."
        if vinfo and vinfo.get("status"):
            vline += f" {vinfo['status']}."
        if vinfo and vinfo.get("kept"):
            vline += " 공시 조회 실패라 직전 판정 유지."
    status = (f"{day} KRX 종가 보통주 {c:,.0f}원, 우선주 {p:,.0f}원, 괴리율 {d:.2f}%, "
              f"구간 {band} {LABEL[band]}, {pct_txt} (3년 범위 {h['min']}~{h['max']}%, 중앙값 {h['median']}%). "
              f"{vline} 바뀐 것: {reason}.")
    later = (f"KRX 거래일 19:00에 확인하고 구간이나 판정이 바뀔 때만 보냅니다. 구간: {b['low_pct']}% 이하 Z_LOW(우선주 0%), "
             f"{b['high1_pct']}% 이상 Z_HIGH1(약 {cfg['targets_pref_pct']['Z_HIGH1']}%), {b['high2_pct']}% 이상 Z_HIGH2(최대 "
             f"{cfg['targets_pref_pct']['Z_HIGH2']}%), 그 사이 Z_MID(유지). 해제는 1%p 여유: Z_HIGH2 는 "
             f"{b['high2_pct'] - b['hysteresis_pp']:g}% 미만, Z_HIGH1 은 {b['high1_pct'] - b['hysteresis_pp']:g}% 미만, "
             f"Z_LOW 는 {b['low_pct'] + b['hysteresis_pp']:g}% 초과.")
    if verdict is not None:
        later += " 보통주 판정(기준가 562,000원, 피어 373220 규칙)은 2027-01-15 09:00 까지."
    lines = [
        "이게 뭐 알림: 삼성SDI 보통주 006400과 우선주 006405의 같은 날 KRX 종가 괴리율 구간과 보통주 변경 조건 판정을 합친 비중 알림. 투자 권유 아님.",
        "지금 결과/상태: " + status,
        "지금 할 일: " + todo_line(band, verdict, cfg, value_eok),
        "나중에: " + later,
        "익절/줄일 때: 괴리율이 다시 50% 이하로 줄면 우선주를 보통주로 되돌려 할인 축소분을 챙기세요. "
        "보통주 판정이 축소 점검이면 우선주 이동을 멈추고 둘 다 줄일지 검토하세요.",
        "하지 말 것: 우선주 100% 전면 전환, 시장가 주문, NXT 가격 섞기, 괴리율만 보고 SDI 총 비중 늘리기. "
        "우선주는 2027년까지 무배당 정책이라 배당 차이를 기대하지 마세요.",
    ]
    return "\n".join(lines)


def text_ok(*parts: str) -> bool:
    return not any(ch in s for s in parts for ch in FORBIDDEN)


# ---------- verdict ----------
def get_verdict(state: dict, now: datetime, cfg: dict) -> tuple[str | None, dict | None]:
    if not verdict_active(cfg, now):
        return None, None
    import sdichg_monitor
    vcfg = json.loads((ROOT / "configs" / f"{cfg['verdict_config']}.json").read_text(encoding="utf-8"))
    info = sdichg_monitor.evaluate_verdict(vcfg, now)
    if info.get("nerr") and state.get("verdict"):
        info = dict(info, verdict=state["verdict"], why="공시 조회 실패, 직전 판정 유지", kept=True)
    return info["verdict"], info


# ---------- main ----------
def save(state: dict, now: datetime) -> None:
    state["updated_at"] = now.isoformat(timespec="seconds")
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "auto"
    if mode == "selftest":
        return selftest()
    if mode not in {"auto", "dry"}:
        raise SystemExit("unknown mode")
    dry = mode == "dry"
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    day = session_day(now)
    if not is_trading_day(day):
        print("non-session; silent" + ("\nDRY RUN: no ntfy, no state" if dry else ""))
        return 0
    if now.date() == day and (now.hour, now.minute) < (15, 40):
        print("not a completed KRX session; silent" + ("\nDRY RUN: no ntfy, no state" if dry else ""))
        return 0
    state = json.loads(STATE_PATH.read_text(encoding="utf-8")) if STATE_PATH.exists() else {}
    dk = day.isoformat()
    try:
        common, pref = daum_rows(cfg["ticker_common"]), daum_rows(cfg["ticker_pref"])
        c, p, d = same_session(common, pref, dk)
    except Exception as exc:
        print(type(exc).__name__, str(exc))
        if dry:
            print("DRY RUN: no ntfy, no state")
            return 0
        if state.get("ops_day") != dk:
            publish("cwally-ops", "[SDI] 006400/006405 종가 조회 실패 | 판정 보류",
                    f"{dk} 같은 날 KRX 종가 확인 실패: {exc}. 매매 신호 아님. 구간과 판정 상태는 바꾸지 않습니다.",
                    3, ["warning"], resolve_server(cfg["ntfy_server"]), config_name="sdimix")
            state["ops_day"] = dk
            save(state, now)
        return 0
    pct, nhist = percentile(common, pref, dk, d)
    value_eok = avg_value_eok(pref, dk, cfg["liquidity"]["avg_days"])
    verdict, vinfo = get_verdict(state, now, cfg)
    new, send, reason = decide(state, dk, d, verdict, cfg)
    band = new.get("band") or band_of(d, state.get("band"), cfg)
    print(f"{dk} KRX common={c} pref={p} discount={d:.4f}% band={band} pct={pct} n={nhist} "
          f"value20={value_eok} verdict={verdict} send={send} reason={reason} prev_band={state.get('band')} prev_verdict={state.get('verdict')}")
    if vinfo and (vinfo.get("perr") or vinfo.get("nerr")):
        print(f"verdict fetch errors perr={vinfo.get('perr')} nerr={vinfo.get('nerr')}")
    title = title_for(band, d, verdict, cfg)
    body = body_for(cfg, dk, c, p, d, band, pct, nhist, verdict, vinfo, value_eok, reason)
    if not text_ok(title, body):
        raise SystemExit("forbidden character in copy")
    if dry:
        print("WOULD SEND:" if send else "would not send (no band/verdict change). Title if it did:")
        print(title)
        print(body)
        print("DRY RUN: no ntfy, no state (send=False)")
        return 0
    if send:
        pri = 4 if (verdict == REDUCE or band in ("Z_HIGH2", "Z_LOW")) else 3
        publish("cwally-port", title, body, pri, ["chart_with_upwards_trend"], resolve_server(cfg["ntfy_server"]), config_name="sdimix")
    new.update(common_close=str(c), preferred_close=str(p))
    if vinfo:
        new["verdict_why"] = vinfo.get("why")
        new["last_price"] = vinfo.get("price")
    if new != state:
        save(new, now)
    return 0


# ---------- selftest ----------
def selftest() -> int:
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    D = lambda x: Decimal(str(x))  # noqa: E731
    # band edges and hysteresis
    assert band_of(D(50), None, cfg) == "Z_LOW"
    assert band_of(D("50.01"), None, cfg) == "Z_MID"
    assert band_of(D("50.9"), "Z_LOW", cfg) == "Z_LOW"
    assert band_of(D("51.01"), "Z_LOW", cfg) == "Z_MID"
    assert band_of(D("61.49"), "Z_MID", cfg) == "Z_MID"
    assert band_of(D("61.5"), "Z_MID", cfg) == "Z_HIGH1"
    assert band_of(D("61.7431"), None, cfg) == "Z_HIGH1"
    assert band_of(D("60.6"), "Z_HIGH1", cfg) == "Z_HIGH1"
    assert band_of(D("60.4"), "Z_HIGH1", cfg) == "Z_MID"
    assert band_of(D(65), "Z_HIGH1", cfg) == "Z_HIGH2"
    assert band_of(D("64.2"), "Z_HIGH2", cfg) == "Z_HIGH2"
    assert band_of(D("63.9"), "Z_HIGH2", cfg) == "Z_HIGH1"
    assert band_of(D("60.0"), "Z_HIGH2", cfg) == "Z_MID"
    assert band_of(D("49"), "Z_HIGH2", cfg) == "Z_LOW"
    # seeded state (today 11/4) never re-sends
    seed = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    assert seed["band"] == "Z_HIGH1" and seed["verdict"] == "보유" and seed["last_date"] == "2026-11-04"
    s, send, _ = decide(seed, "2026-11-04", D("61.74"), "보유", cfg)
    assert not send
    s, send, _ = decide(seed, "2026-11-05", D("61.0"), "보유", cfg)
    assert not send and s["band"] == "Z_HIGH1"  # inside hysteresis
    s, send, why = decide(s, "2026-11-06", D("60.2"), "보유", cfg)
    assert send and s["band"] == "Z_MID" and "구간" in why
    s, send, _ = decide(s, "2026-11-09", D("61.2"), "보유", cfg)
    assert not send  # not back above 61.5
    s, send, _ = decide(s, "2026-11-10", D("65.3"), "보유", cfg)
    assert send and s["band"] == "Z_HIGH2"
    # verdict change trigger with the same band
    s, send, why = decide(s, "2026-11-11", D("64.5"), UPGRADE, cfg)
    assert send and s["band"] == "Z_HIGH2" and "판정" in why
    s, send, _ = decide(s, "2026-11-12", D("64.4"), UPGRADE, cfg)
    assert not send
    # empty state seeds silently
    s0, send, why = decide({}, "2026-11-05", D(55), "보유", cfg)
    assert not send and why == "seed" and s0["band"] == "Z_MID"
    # reduce override beats every band
    for band in BANDS:
        t = title_for(band, D("61.7"), REDUCE, cfg)
        assert "우선주 이동 보류" in t and "축소 점검" in t
        assert "우선주로 옮기지 마세요" in todo_line(band, REDUCE, cfg, 16.0)
    assert "나눠서" in todo_line("Z_HIGH1", UPGRADE, cfg, 16.0)
    # expected title from the request
    assert title_for("Z_HIGH1", D("61.7431"), "보유", cfg) == "[SDI] 006400/006405 괴리율 61.7% 우선주 비중 30% 검토 | 보통주 판정 보유"
    assert "100%" not in title_for("Z_HIGH2", D(66), "보유", cfg)
    # end date: verdict off after 2027-01-15 09:00, bands only, verdict changes ignored
    assert verdict_active(cfg, datetime(2027, 1, 15, 9, 0, tzinfo=KST))
    assert not verdict_active(cfg, datetime(2027, 1, 15, 9, 1, tzinfo=KST))
    assert not verdict_active(cfg, datetime(2027, 1, 15, 19, 0, tzinfo=KST))
    end_state = {"last_date": "2027-01-14", "band": "Z_HIGH1", "verdict": "보유"}
    s, send, _ = decide(end_state, "2027-01-15", D("61.6"), None, cfg)
    assert not send and s["verdict"] == "보유"
    s, send, _ = decide(s, "2027-01-18", D("59.0"), None, cfg)
    assert send and s["band"] == "Z_MID"
    t_end = title_for("Z_MID", D("59.0"), None, cfg)
    assert t_end.endswith("| 괴리율 구간만") and "판정" not in t_end
    b_end = body_for(cfg, "2027-01-18", D(500000), D(205000), D(59), "Z_MID", 99.0, 730, None, None, 15.0, "구간")
    assert "2027-01-15 09:00 에 끝나 이제 괴리율 구간만" in b_end
    # every title: prefix, both tickers, not ops-routed; no forbidden characters anywhere
    for band in BANDS:
        for v in ("보유", UPGRADE, REDUCE, None):
            t = title_for(band, D("57.3"), v, cfg)
            b = body_for(cfg, "2026-11-05", D(545000), D(208500), D("61.74"), band, 100.0, 729, v,
                         {"why": "x", "status": "종가 545,000원"} if v else None, 16.0, "구간")
            assert t.startswith("[SDI] 006400/006405 "), t
            assert "006400" in t and "006405" in t
            assert not OPS_TITLE_RE.search(t), t
            assert text_ok(t, b), t
            assert "투자 권유 아님" in b
            assert [ln.split(":")[0] for ln in b.split("\n")] == ["이게 뭐 알림", "지금 결과/상태", "지금 할 일", "나중에", "익절/줄일 때", "하지 말 것"]
    assert not text_ok("a\u00b7b") and not text_ok("a\u2014b")
    # same-session guard
    try:
        same_session({"2026-11-04": {"close": D(1)}}, {"2026-11-03": {"close": D(1)}}, "2026-11-04")
    except ValueError:
        pass
    else:
        raise AssertionError("date mismatch not rejected")
    c, p, d = same_session({"2026-11-04": {"close": D(545000)}}, {"2026-11-04": {"close": D(208500)}}, "2026-11-04")
    assert abs(float(d) - 61.7431) < 0.001
    # config copy text also clean
    assert text_ok(json.dumps(cfg, ensure_ascii=False))
    print("SELFTEST PASS: bands + 1pp hysteresis, seed no-send, verdict-change trigger, reduce override, upgrade split note, "
          "2027-01-15 end (bands only), title tickers 006400/006405, no ops words, no middle dot / em-dash, 6-item body")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
