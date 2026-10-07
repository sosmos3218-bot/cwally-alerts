#!/usr/bin/env python3
"""KODEX 미국러셀2000(H) 280930: 2026-10-07 분석 조건 점검.

Slot: existing weekday 09:00 morning bundle. No new cron, no one-shot.
Sends to cwally-port only when a watched band changes, plus one weekly summary
on the first successful run of each ISO week. Lookup problems go to cwally-ops
and block any trading line for that run.

Bands (configs/r2k930.json), edge only:
  etf     : close <= 13,400 / 13,400 초과 17,320 미만 / >= 17,320
  russell : Yahoo ^RUT < 2,400
  ust10   : US 10-year < 4.50%
  fx      : USD/KRW >= 1,500
  aum     : Naver marketValueRaw >= 1,000억원
Price alone never produces a buy line. Fed hike-stop is not auto-detected.
iNAV premium is not auto-measured (live iNAV and the prior KRX close are different sessions).

Modes: auto (default), dry (no state, no send), self-test (offline asserts).
"""
from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from kr_calendar import is_trading_day, prev_trading_day  # noqa: E402
from naver_api import get_json, num, stock_trend  # noqa: E402
from ntfy_util import publish, resolve_server  # noqa: E402

KST = timezone(timedelta(hours=9))
CFG_PATH = ROOT / "configs" / "r2k930.json"
STATE_PATH = ROOT / "state" / "r2k930.json"
CLOSE_READY = (20, 30)
MAX_AGE_DAYS = 5
UA = {"User-Agent": "Mozilla/5.0 (cwally-alerts; +mailto:sosmos3218@gmail.com)"}

ETF_KO = {"deep": "13,400원 이하", "neutral": "13,400원 초과 17,320원 미만", "chase": "17,320원 이상"}
RUSSELL_KO = {"deep": "2,400 미만", "normal": "2,400 이상"}
UST_KO = {"easier": "4.50% 미만", "tight": "4.50% 이상"}
FX_KO = {"weak_krw": "1,500원 이상", "firm": "1,500원 미만"}
AUM_KO = {"large": "1,000억원 이상", "small": "1,000억원 미만"}


def read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state: dict, now: datetime) -> None:
    state["updated_at"] = now.isoformat(timespec="seconds")
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(STATE_PATH)


def expected_session(now: datetime) -> date:
    d = now.date()
    if is_trading_day(d) and (now.hour, now.minute) >= CLOSE_READY:
        return d
    return prev_trading_day(d)


def parse_ends(cfg: dict) -> datetime | None:
    raw = cfg.get("ends_kst")
    if not raw:
        return None
    return datetime.strptime(str(raw), "%Y-%m-%d %H:%M").replace(tzinfo=KST)


def etf_band(close: float, lv: dict) -> str:
    if close <= float(lv["etf_deep"]):
        return "deep"
    if close >= float(lv["etf_chase"]):
        return "chase"
    return "neutral"


def russell_band(px: float, lv: dict) -> str:
    return "deep" if px < float(lv["russell_deep"]) else "normal"


def ust_band(y: float, lv: dict) -> str:
    return "easier" if y < float(lv["ust10_easier"]) else "tight"


def fx_band(px: float, lv: dict) -> str:
    return "weak_krw" if px >= float(lv["fx_rethink"]) else "firm"


def aum_band(raw: float, lv: dict) -> str:
    return "large" if raw >= float(lv["aum_large_krw"]) else "small"


def action_line(bands: dict) -> tuple[str, str]:
    if bands["etf"] == "deep" or bands["russell"] == "deep":
        return (
            "하단 근접 | 분할 검토 후보, 매수 아님",
            "가격이나 러셀2000이 분석의 하단 구간에 들어왔습니다. 이익 추정은 확인하지 않습니다. 한 번에 사지 마세요.",
        )
    if bands["etf"] == "chase":
        return ("고점 돌파 | 추격 금지", "52주 고점 이상입니다. 새로 사지 마세요.")
    if bands["fx"] == "weak_krw":
        return ("원화 약세 | 환헤지 재검토", "원/달러가 1,500원 이상입니다. 환헤지를 유지할지 다시 계산하세요. 이것만으로 사지 마세요.")
    if bands["ust10"] == "easier":
        return ("금리 부담 완화 | 신규는 아직 비추천", "미국 10년물이 4.50% 아래입니다. 상품 비용과 유동성은 그대로라 비중 확대는 보류하세요.")
    if bands["aum"] == "large":
        return ("규모 확대 | 상품 열위만 완화 후보", "시가총액이 1,000억원을 넘었습니다. 그래도 더 싼 해외 상장 상품과 비교하세요.")
    return ("판단 유지 | 신규 비추천", "2026-10-07 분석이 유지됩니다. 새로 담지 마세요. 환헤지가 꼭 필요하고 금액이 아주 작을 때만 기능합니다.")


def changed(prev: str | None, cur: str) -> bool:
    return bool(prev) and prev != cur


def evaluate(cfg: dict, state: dict, obs: dict) -> dict:
    lv = cfg["levels"]
    bands = {
        "etf": etf_band(obs["close"], lv),
        "russell": russell_band(obs["russell"], lv),
        "ust10": ust_band(obs["ust10"], lv),
        "fx": fx_band(obs["fx"], lv),
        "aum": aum_band(obs["aum"], lv),
    }
    events = []
    if changed(state.get("etf_band"), bands["etf"]):
        events.append(f"ETF 가격 구간: {ETF_KO.get(state['etf_band'], state['etf_band'])} -> {ETF_KO[bands['etf']]}")
    if changed(state.get("russell_band"), bands["russell"]):
        events.append(f"러셀2000: {RUSSELL_KO.get(state['russell_band'], state['russell_band'])} -> {RUSSELL_KO[bands['russell']]}")
    if changed(state.get("ust10_band"), bands["ust10"]):
        events.append(f"미국 10년물: {UST_KO.get(state['ust10_band'], state['ust10_band'])} -> {UST_KO[bands['ust10']]}")
    if changed(state.get("fx_band"), bands["fx"]):
        events.append(f"원/달러: {FX_KO.get(state['fx_band'], state['fx_band'])} -> {FX_KO[bands['fx']]}")
    if changed(state.get("aum_band"), bands["aum"]):
        events.append(f"시가총액: {AUM_KO.get(state['aum_band'], state['aum_band'])} -> {AUM_KO[bands['aum']]}")
    weekly_due = not (state.get("weekly_sent") or {}).get(obs["iso_week"])
    return {"bands": bands, "events": events, "weekly_due": weekly_due}


def eok(raw: float) -> str:
    return f"{raw / 100_000_000:,.0f}억원"


def value_line(obs: dict, lv: dict) -> str:
    value = obs.get("value")
    if not value or value <= 0:
        return "하루 거래대금: 미확인. 한 번에 크게 사지 마세요."
    cap = value * float(lv["order_vs_value_pct"]) / 100
    return f"하루 거래대금 약 {value / 100_000_000:.2f}억원. 한 번에 {cap / 10_000:,.0f}만원(10%)을 넘기지 마세요."


def build_message(cfg: dict, obs: dict, res: dict, kind: str) -> tuple[str, list[str]]:
    if kind == "ended":
        title = f"{cfg['title_prefix']} 기준 만료 | 알림 중지"
        lines = [
            "이게 뭐 알림: KODEX 미국러셀2000(H) 280930 점검이 끝나는 안내.",
            f"지금 결과/상태: 기준 시각 {cfg.get('ends_kst')} KST가 지났습니다. 2026-10-07 구간값으로 더 판단하지 않습니다.",
            "지금 할 일: 없음. 계속 보려면 구간값을 수동으로 갱신해야 합니다.",
            "나중에: 갱신 전에는 이 알림이 오지 않습니다.",
            "익절/줄일 때: 이 알림만으로 팔지 마세요.",
            "하지 말 것: 만료된 구간값을 현재 매수 신호로 보기.",
            f"출처: {cfg['sources']['page']}",
        ]
        return title, lines
    bands = res["bands"]
    action, todo = action_line(bands)
    title = f"{cfg['title_prefix']} {action}" if res["events"] else f"{cfg['title_prefix']} 주간 점검 | {action.split(' | ')[-1]}"
    why = "조건이 바뀌어서 보냅니다." if res["events"] else "매주 첫 거래일 정기 요약입니다."
    lines = [
        "이게 뭐 알림: KODEX 미국러셀2000(H) 280930 조건 점검. 2026-10-07 분석 기준. " + why,
        "지금 결과/상태:",
        f"- 종가 {obs['close']:,.0f}원 ({obs['session']:%Y-%m-%d} 완료 세션), 구간: {ETF_KO[bands['etf']]}",
        f"- 러셀2000 {obs['russell']:,.2f} ({obs['russell_day']}, {RUSSELL_KO[bands['russell']]})",
        f"- 미국 10년물 {obs['ust10']:.2f}% ({obs['ust10_day']}, {UST_KO[bands['ust10']]}). 연준 인상 중단은 확인하지 않음",
        f"- 원/달러 {obs['fx']:,.2f}원 ({obs['fx_day']}, {FX_KO[bands['fx']]})",
        f"- 시가총액 {eok(obs['aum'])} ({AUM_KO[bands['aum']]})",
        f"- {value_line(obs, cfg['levels'])}",
    ]
    if res["events"]:
        lines.append("바뀐 점:")
        lines += [f"- {e}" for e in res["events"]]
    lines += [
        "지금 할 일: " + todo,
        "나중에: 10년물 4.50% 아래, 러셀2000 2,400 아래, 시가총액 1,000억원 이상, 원/달러 1,500원 이상이 판단이 바뀌는 조건입니다.",
        "익절/줄일 때: 고점 돌파를 추격해 비중이 커졌으면 나눠 줄이세요. 원/달러 1,500원대면 환헤지 유지를 다시 계산하세요.",
        "하지 말 것: 추격매수, 가격만 보고 매수, iNAV보다 비싸게 매수, 하루 거래대금의 10%를 한 번에 매수.",
        f"출처: {cfg['sources']['page']}",
    ]
    return title, lines


def send(cfg: dict, title: str, lines: list[str], dry: bool, ops: bool = False, priority: int = 3) -> bool:
    topic = "cwally-ops" if ops else "cwally-port"
    if dry:
        print(f"[dry] topic={topic}", title, *lines, "dry-run; no ntfy", sep="\n")
        return True
    try:
        publish(
            topic,
            title,
            "\n".join(lines),
            priority,
            ["warning"] if ops else ["chart_with_upwards_trend"],
            resolve_server(cfg.get("ntfy_server")),
        )
        return True
    except Exception as exc:
        print(f"publish failed: {type(exc).__name__}")
        return False


def problem(cfg: dict, state: dict, now: datetime, key: str, detail: str, dry: bool) -> None:
    day = now.strftime("%Y-%m-%d")
    ops_daily = state.setdefault("ops_daily", {})
    if ops_daily.get(key) == day and not dry:
        print(f"ops already sent today: {key}")
        return
    title = f"{cfg['title_prefix']} {key} 조회 실패 | 판단 유보"
    lines = [
        "이게 뭐 알림: KODEX 미국러셀2000(H) 280930 정기 점검의 데이터 문제 안내.",
        f"지금 결과/상태: {detail}. {now:%Y-%m-%d %H:%M} KST. 이번 실행의 매매 판단은 막았습니다.",
        "지금 할 일: 없음. 다음 거래일에 자동 재시도합니다.",
        "나중에: 이틀 이상 계속되면 해당 API 변경 여부를 확인하세요.",
        "익절/줄일 때: 이 알림만으로 판단하지 마세요.",
        "하지 말 것: 빠진 숫자를 0이나 예전 값으로 보고 매매.",
        f"출처: {cfg['sources']['page']}",
    ]
    if send(cfg, title, lines, dry, ops=True, priority=2) and not dry:
        ops_daily[key] = day


def yahoo_last(ticker: str) -> tuple[str, float] | None:
    url = "https://query1.finance.yahoo.com/v8/finance/chart/" + urllib.parse.quote(ticker) + "?interval=1d&range=10d"
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=20) as resp:
        payload = json.loads(resp.read().decode())
    result = payload["chart"]["result"][0]
    stamps = result.get("timestamp") or []
    closes = (result.get("indicators") or {}).get("quote", [{}])[0].get("close") or []
    pairs = []
    for ts, px in zip(stamps, closes):
        if px is None:
            continue
        day = datetime.fromtimestamp(int(ts), tz=timezone.utc).astimezone(KST).date().isoformat()
        pairs.append((day, float(px)))
    return pairs[-1] if pairs else None


def series_last(category: str, reuters: str) -> tuple[str, float] | None:
    raw = get_json(
        "https://m.stock.naver.com/front-api/marketIndex/prices?category="
        + category
        + "&page=1&reutersCode="
        + urllib.parse.quote(reuters)
    )
    best = None
    for row in (raw or {}).get("result") or []:
        day = str(row.get("localTradedAt") or "")[:10]
        px = num(row.get("closePrice"))
        if px is None or len(day) != 10:
            continue
        if best is None or day > best[0]:
            best = (day, px)
    return best


def fresh(day: str, today: date) -> bool:
    try:
        age = (today - date.fromisoformat(day)).days
    except ValueError:
        return False
    return 0 <= age <= MAX_AGE_DAYS


def observe(cfg: dict, session: date, today: date) -> tuple[dict | None, str | None]:
    try:
        row = next((r for r in stock_trend(cfg["ticker"], 10) if r.get("day") == session.strftime("%Y%m%d")), None)
    except Exception as exc:
        return None, f"종가|{type(exc).__name__}"
    if not row or row.get("close") is None:
        return None, f"종가|{session:%Y-%m-%d} 완료 세션 종가가 아직 없습니다"
    try:
        rus = yahoo_last("^RUT")
    except Exception as exc:
        return None, f"러셀2000|{type(exc).__name__}"
    if not rus or not fresh(rus[0], today):
        return None, "러셀2000|최근 종가가 없습니다"
    try:
        ust = series_last("bond", "US10YT=RR")
    except Exception as exc:
        return None, f"미국10년|{type(exc).__name__}"
    if not ust or not fresh(ust[0], today):
        return None, "미국10년|최근 금리가 없습니다"
    try:
        fx = series_last("exchange", "FX_USDKRW")
    except Exception as exc:
        return None, f"원달러|{type(exc).__name__}"
    if not fx or not fresh(fx[0], today):
        return None, "원달러|최근 환율이 없습니다"
    try:
        integ = get_json(f"https://m.stock.naver.com/api/stock/{cfg['ticker']}/integration")
        aum = num(((integ or {}).get("etfKeyIndicator") or {}).get("marketValueRaw"))
    except Exception as exc:
        return None, f"시가총액|{type(exc).__name__}"
    if aum is None or aum <= 0:
        return None, "시가총액|marketValueRaw가 없습니다"
    vol = row.get("volume")
    value = vol * row["close"] if vol else None
    iso = today.isocalendar()
    return {
        "session": session,
        "close": row["close"],
        "value": value,
        "russell": rus[1],
        "russell_day": rus[0],
        "ust10": ust[1],
        "ust10_day": ust[0],
        "fx": fx[1],
        "fx_day": fx[0],
        "aum": aum,
        "iso_week": f"{iso[0]}-W{iso[1]:02d}",
    }, None


def apply_state(state: dict, obs: dict, res: dict, weekly: bool) -> None:
    b = res["bands"]
    state["etf_band"] = b["etf"]
    state["russell_band"] = b["russell"]
    state["ust10_band"] = b["ust10"]
    state["fx_band"] = b["fx"]
    state["aum_band"] = b["aum"]
    state["last_observation"] = {
        "session": obs["session"].isoformat(),
        "close": obs["close"],
        "russell": obs["russell"],
        "ust10": obs["ust10"],
        "fx": obs["fx"],
        "aum": obs["aum"],
    }
    if weekly:
        state.setdefault("weekly_sent", {})[obs["iso_week"]] = obs["session"].isoformat()
        state["weekly_sent"] = dict(sorted(state["weekly_sent"].items())[-12:])


def run(mode: str, now: datetime | None = None) -> int:
    now = (now or datetime.now(KST)).astimezone(KST)
    cfg = read_json(CFG_PATH)
    state = read_json(STATE_PATH)
    dry = mode == "dry"
    end = parse_ends(cfg)
    if end and now >= end:
        if state.get("ended_sent"):
            print("ended; silent")
            return 0
        title, lines = build_message(cfg, {}, {"bands": {}, "events": []}, "ended")
        if send(cfg, title, lines, dry, priority=3) and not dry:
            state["ended_sent"] = True
            save_state(state, now)
        return 0
    if not is_trading_day(now.date()):
        print("not a KRX trading day; silent")
        return 0
    session = expected_session(now)
    obs, err = observe(cfg, session, now.date())
    if err:
        key, detail = err.split("|", 1)
        problem(cfg, state, now, key, detail, dry)
        if not dry:
            save_state(state, now)
        return 0
    res = evaluate(cfg, state, obs)
    if not res["events"] and not res["weekly_due"]:
        print(f"no change; silent (close={obs['close']}, etf={res['bands']['etf']})")
        if not dry:
            apply_state(state, obs, res, weekly=False)
            save_state(state, now)
        return 0
    title, lines = build_message(cfg, obs, res, "event" if res["events"] else "weekly")
    if send(cfg, title, lines, dry, priority=4 if res["events"] else 3) and not dry:
        apply_state(state, obs, res, weekly=True)
        save_state(state, now)
    return 0


def self_test() -> int:
    cfg = read_json(CFG_PATH)
    lv = cfg["levels"]
    base = {
        "etf_band": "neutral",
        "russell_band": "normal",
        "ust10_band": "tight",
        "fx_band": "firm",
        "aum_band": "small",
        "weekly_sent": {"2026-W41": "2026-10-07"},
    }

    def obs(**kw):
        row = {
            "session": date(2026, 10, 7),
            "close": 15935,
            "value": 217_000_000,
            "russell": 2830.3,
            "russell_day": "2026-10-07",
            "ust10": 5.27,
            "ust10_day": "2026-10-06",
            "fx": 1339.3,
            "fx_day": "2026-10-07",
            "aum": 21_512_000_000,
            "iso_week": "2026-W41",
        }
        row.update(kw)
        return row

    r = evaluate(cfg, base, obs())
    assert not r["events"] and not r["weekly_due"], "unchanged must be silent"
    r = evaluate(cfg, {}, obs())
    assert not r["events"] and r["weekly_due"], "first run is weekly only"
    r = evaluate(cfg, base, obs(close=13400))
    assert r["bands"]["etf"] == "deep" and r["events"], "deep edge"
    assert "매수 아님" in action_line(r["bands"])[0]
    r = evaluate(cfg, base, obs(close=17320))
    assert r["bands"]["etf"] == "chase" and "추격 금지" in action_line(r["bands"])[0]
    r = evaluate(cfg, base, obs(russell=2399.9))
    assert r["bands"]["russell"] == "deep" and any("러셀2000" in e for e in r["events"])
    assert etf_band(13401, lv) == "neutral" and etf_band(17319, lv) == "neutral"
    assert ust_band(4.50, lv) == "tight" and ust_band(4.49, lv) == "easier"
    assert fx_band(1499.9, lv) == "firm" and fx_band(1500, lv) == "weak_krw"
    assert aum_band(99_999_999_999, lv) == "small" and aum_band(100_000_000_000, lv) == "large"
    r = evaluate(cfg, base, obs(ust10=4.2, fx=1510, aum=100_000_000_000, close=13000, russell=2300))
    assert action_line(r["bands"])[0].startswith("하단 근접"), "deep outranks other gates"
    r = evaluate(cfg, base, obs(iso_week="2026-W42"))
    assert r["weekly_due"] and not r["events"]
    assert expected_session(datetime(2026, 10, 8, 9, 0, tzinfo=KST)) == date(2026, 10, 7)
    assert not is_trading_day(date(2026, 10, 9))
    title, lines = build_message(cfg, obs(close=13000, russell=2300), evaluate(cfg, base, obs(close=13000, russell=2300)), "event")
    body = "\n".join([title, *lines])
    for head in ("이게 뭐 알림", "지금 결과/상태", "지금 할 일", "나중에", "익절/줄일 때", "하지 말 것"):
        assert head in body, head
    assert "꺼지는 때" not in body
    assert "\u00b7" not in body and "\u2014" not in body
    assert "매수하세요" not in body
    assert title.startswith("[280930]")
    ended_title, ended_lines = build_message(cfg, {}, {"bands": {}, "events": []}, "ended")
    assert ended_title.endswith("알림 중지")
    assert parse_ends(cfg) == datetime(2027, 6, 10, 9, 0, tzinfo=KST)
    print("self-test ok")
    return 0


if __name__ == "__main__":
    m = sys.argv[1] if len(sys.argv) > 1 else "auto"
    if m == "self-test":
        sys.exit(self_test())
    if m not in {"auto", "dry"}:
        print(f"unknown mode: {m}")
        sys.exit(2)
    sys.exit(run(m))
