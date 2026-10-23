#!/usr/bin/env python3
"""476760 (ACE 미국30년국채액티브, 환노출) condition alert on the k203780 19:00 KST slot.

Events (each fires once when it turns on, re-arms per configs/476760.json, k079900 armed/fired_day style):
  add       476760 close <= price.add_max                         -> 분할 추가 신호
  raise     US 30Y (^TYX) >= yield.raise_min (re-rise)            -> 비중 확대 신호
  review_y  US 30Y > yield.review_over                            -> 판단 재검토
  fx_level  USD/KRW <= fx.level_max (원화 강세)                    -> 판단 재검토
  fx_move   USD/KRW 5-session change <= fx.move_pct (원화 강세)    -> 판단 재검토
Data: Naver daily price rows (same as k079900), Yahoo chart closes (^TYX like the ^TNX readers,
USDKRW=X like ustsy/krwfx). No new secret.

Modes: auto (slot) | force (ignore holiday) | dry (print, no ntfy, no state) |
       install (one install-confirmation send to cwally-port, no state) | seed (write state from live data, no ntfy)
"""
from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from kr_calendar import is_trading_day, session_day  # noqa: E402
from ntfy_util import publish, resolve_server, resolve_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
CFG_PATH = ROOT / "configs" / "476760.json"
STATE_PATH = ROOT / "state" / "476760.json"
UA = {"User-Agent": "Mozilla/5.0 cwally-alerts 476760"}
KEYS = ("add", "raise", "review_y", "fx_level", "fx_move")


def get_json(url: str):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8", errors="ignore"))


def num(v) -> float | None:
    if v is None:
        return None
    try:
        return float(str(v).replace(",", "").strip())
    except ValueError:
        return None


def price_rows(cfg: dict) -> list[dict]:
    raw = get_json(cfg["sources"]["price"])
    rows = []
    for r in raw if isinstance(raw, list) else []:
        d = str(r.get("localTradedAt") or "")[:10]
        close = num(r.get("closePrice"))
        if len(d) == 10 and close:
            rows.append({"day": d, "close": close})
    rows.sort(key=lambda x: x["day"])
    if not rows:
        raise RuntimeError("시세 0건")
    return rows


def yahoo_series(cfg: dict, symbol: str) -> tuple[float, list[float]]:
    url = cfg["sources"]["yahoo"].format(symbol=urllib.parse.quote(symbol))
    res = get_json(url)["chart"]["result"][0]
    closes = [float(x) for x in res["indicators"]["quote"][0]["close"] if x is not None]
    last = (res.get("meta") or {}).get("regularMarketPrice")
    last = float(last) if last is not None else (closes[-1] if closes else None)
    if last is None:
        raise RuntimeError(f"{symbol} 값 없음")
    if not closes or abs(closes[-1] - last) > 1e-9:
        closes.append(last)
    return last, closes


def move_pct(closes: list[float], sessions: int) -> float | None:
    if len(closes) < sessions + 1:
        return None
    old, new = closes[-(sessions + 1)], closes[-1]
    return (new / old - 1.0) * 100.0 if old else None


def load_state() -> dict:
    st = {"updated_at": None, "last_day": None, "seeded": False, "last_fail_day": None, "last": {}}
    for k in KEYS:
        st[k] = {"armed": True, "fired_day": None}
    if STATE_PATH.exists():
        try:
            st.update(json.loads(STATE_PATH.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass
    return st


def save_state(st: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def step(st: dict, key: str, hit: bool, rearm: bool, day: str, events: list, info: dict) -> None:
    s = st[key]
    if s.get("armed", True) and hit and s.get("fired_day") != day:
        s.update(armed=False, fired_day=day)
        events.append(dict(info, key=key, day=day))
    elif not s.get("armed", True) and rearm and (s.get("fired_day") or "") < day:
        s["armed"] = True
        print(f"{key} re-armed on {day}")


def evaluate(cfg: dict, st: dict, data: dict, today: str, now: datetime) -> list[dict]:
    """Update st in place; return newly fired events."""
    events: list[dict] = []
    pz, yz, fz = cfg["price"], cfg["yield"], cfg["fx"]
    rows = data.get("rows")
    if rows:
        start = st.get("last_day") or rows[-1]["day"]
        close_hm = tuple(map(int, cfg.get("market_close_kst", "15:30").split(":")))
        done = None
        for r in rows:
            if r["day"] < start:
                continue
            if r["day"] == today and (now.hour, now.minute) < close_hm:
                continue  # intraday row, not a close yet
            step(st, "add", r["close"] <= pz["add_max"], r["close"] > pz["add_rearm"], r["day"], events, {"close": r["close"]})
            done = r["day"]
        if done:
            st["last_day"] = done
    y = data.get("y30")
    if y is not None:
        step(st, "raise", y >= yz["raise_min"], y < yz["raise_rearm"], today, events, {"y30": y})
        step(st, "review_y", y > yz["review_over"], y < yz["review_rearm"], today, events, {"y30": y})
    fx = data.get("fx")
    if fx is not None:
        step(st, "fx_level", fx <= fz["level_max"], fx > fz["level_rearm"], today, events, {"fx": fx})
    mv = data.get("fx_move")
    if mv is not None:
        step(st, "fx_move", mv <= fz["move_pct"], mv > fz["move_rearm_pct"], today, events, {"fx_move": mv})
    return events


def collect(cfg: dict) -> tuple[dict, list[str]]:
    data: dict = {}
    fails: list[str] = []
    try:
        data["rows"] = price_rows(cfg)
    except Exception as exc:
        fails.append(f"476760 시세 ({exc})")
    try:
        data["y30"], _ = yahoo_series(cfg, cfg["yield"]["ticker"])
    except Exception as exc:
        fails.append(f"미국 30년물 ({exc})")
    try:
        fx, closes = yahoo_series(cfg, cfg["fx"]["ticker"])
        data["fx"] = fx
        data["fx_move"] = move_pct(closes, int(cfg["fx"]["move_sessions"]))
    except Exception as exc:
        fails.append(f"원달러 ({exc})")
    return data, fails


def status_line(cfg: dict, data: dict) -> str:
    rows = data.get("rows") or []
    n = datetime.now(KST)
    live = bool(rows) and rows[-1]["day"] == n.date().isoformat() and n.hour * 60 + n.minute < 15 * 60 + 30
    px = f"476760 {'현재가' if live else '종가'} {rows[-1]['close']:,.0f}원({rows[-1]['day']})" if rows else "476760 시세 없음"
    y = f"미국 30년물 {data['y30']:.3f}%" if data.get("y30") is not None else "미국 30년물 없음"
    fx = f"원달러 {data['fx']:,.1f}" if data.get("fx") is not None else "원달러 없음"
    mv = f"1주 {data['fx_move']:+.2f}%" if data.get("fx_move") is not None else "1주 변화 없음"
    return f"{px}. {y}. {fx}({mv})."


def build(cfg: dict, events: list[dict], data: dict, stamp: str) -> tuple[str, str, int]:
    keys = {e["key"] for e in events}
    pz, yz, fz = cfg["price"], cfg["yield"], cfg["fx"]
    hits = []
    if "add" in keys:
        e = next(e for e in events if e["key"] == "add")
        hits.append(f"분할 추가 신호: 종가 {e['close']:,.0f}원, {pz['add_max']:,}원 이하({e['day']}).")
    if "raise" in keys:
        hits.append(f"비중 확대 신호: 미국 30년물 {data['y30']:.3f}%, {yz['raise_min']}% 이상 재상승.")
    if "review_y" in keys:
        hits.append(f"판단 재검토: 미국 30년물 {data['y30']:.3f}%, {yz['review_over']}% 초과. 금리 상승이 이어지면 장기채 가격이 더 밀립니다.")
    if "fx_level" in keys:
        hits.append(f"판단 재검토: 원달러 {data['fx']:,.1f}, {fz['level_max']:,} 이하. 원화 강세가 이어지면 환노출 상품은 금리가 내려도 원화 수익이 깎입니다.")
    if "fx_move" in keys:
        hits.append(f"판단 재검토: 원달러 1주 {data['fx_move']:+.2f}%, 원화 강세 {abs(fz['move_pct']):g}% 이상. 환노출 상품에 불리합니다.")
    if keys & {"review_y", "fx_level", "fx_move"}:
        title = "[476760] 판단 재검토 | 추가매수 멈춤"
        action = "추가 매수를 멈추고 비중과 환헤지 여부를 다시 보세요. 원화 강세가 원인이면 헤지형(484790)과 나눠 볼지 검토."
        pri = 4
    elif "raise" in keys:
        title = "[476760] 30년물 5.4% 재상승 | 비중 확대 검토"
        action = "비중 확대 검토 구간입니다. 한 번에 넣지 말고 소액으로 나눠서, 포트폴리오 10% 상한 안에서만."
        pri = 4
    else:
        title = f"[476760] 종가 {pz['add_max']:,}원 이하 | 소액 분할 추가"
        action = "분할 추가 신호입니다. 계획한 1회분만 소액으로 넣으세요. 포트폴리오 10% 상한을 넘기지 마세요."
        pri = 3
    msg = "\n".join([
        f"{stamp} | 476760 | ACE 미국30년국채액티브(환노출)",
        "이게 뭐 알림: 476760 조건(종가 8,300원, 미국 30년물 5.4%/5.5%, 원화 강세)이 새로 켜질 때만 오는 알림입니다. 매수 지시가 아닙니다.",
        "지금 결과/상태: " + " ".join(hits) + " " + status_line(cfg, data),
        f"지금 할 일: {action}",
        f"나중에: 기본 태도는 그대로입니다. {cfg['stance']} 같은 조건은 재무장 선을 넘었다 와야 다시 옵니다.",
        "익절/줄일 때: 포트폴리오 10%를 넘으면 넘은 만큼 줄이세요. 원달러 1,300 이하가 이어지면 환노출 비중부터 줄입니다.",
        "하지 말 것: 한 번에 큰돈 넣기, 신용 및 미수, 월배당만 보고 비중 늘리기, 조회 실패를 신호로 읽기.",
    ])
    return title, msg, pri


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    dry = mode in {"dry", "test", "dry-run"}
    if dry:
        print("DRY RUN")
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    day = session_day(now)
    today = day.isoformat()
    topic = resolve_topic("port", cfg.get("ntfy_topic"))
    server = resolve_server(cfg.get("ntfy_server"))
    st = load_state()

    if mode == "install":
        data, fails = collect(cfg)
        title = "[476760] 설치 확인 | 매매 신호 아님"
        msg = "\n".join([
            f"{stamp} | 476760 | ACE 미국30년국채액티브(환노출)",
            "이게 뭐 알림: 476760 조건 알림 설치 확인입니다. KRX 거래일 19:00에 조건이 새로 켜질 때만 옵니다.",
            "지금 결과/상태: " + status_line(cfg, data) + (" 조회 실패: " + ", ".join(fails) if fails else " 지금 켜진 조건 없음."),
            "지금 할 일: 없음. 조건은 종가 8,300원 이하 분할 추가, 미국 30년물 5.4% 이상 재상승 비중 확대, 5.5% 초과나 원화 강세(원달러 1,300 이하 또는 1주 -2%) 판단 재검토.",
            f"나중에: {cfg['stance']}",
            "익절/줄일 때: 포트폴리오 10%를 넘으면 넘은 만큼 줄이세요.",
            "하지 말 것: 이 설치 확인을 매수나 매도 신호로 읽기.",
        ])
        print(title)
        print(msg)
        if not dry:
            publish(topic, title, msg, 3, ["white_check_mark"], server)
        return 0

    if not is_trading_day(day) and mode not in {"force", "dry", "seed"}:
        print(f"{today} KRX holiday; silent")
        return 0

    data, fails = collect(cfg)
    print(f"data y30={data.get('y30')} fx={data.get('fx')} fx_move={data.get('fx_move')} rows={(data.get('rows') or [])[-3:]} fails={fails}")

    if mode == "seed" or not st.get("seeded"):
        # First run: arm each condition from current values so nothing fires for a level already crossed
        # before install; a condition already true now starts disarmed (fires after it re-arms).
        tmp = {k: {"armed": True, "fired_day": None} for k in KEYS}
        tmp["last_day"] = None
        ev = evaluate(cfg, tmp, data, today, now)
        for k in KEYS:
            st[k] = tmp[k]
        done_rows = [r["day"] for r in (data.get("rows") or []) if r["day"] < today or now.hour * 60 + now.minute >= 15 * 60 + 30]
        st["last_day"] = tmp.get("last_day") or (done_rows[-1] if done_rows else None)
        st["seeded"] = True
        st["seed_note"] = f"{stamp} 시드. 그때 이미 켜진 조건: {[e['key'] for e in ev] or '없음'}"
        st["last"] = {k: data.get(k) for k in ("y30", "fx", "fx_move")}
        st["updated_at"] = now.isoformat()
        print(st["seed_note"])
        if not dry:
            save_state(st)
        return 0

    events = evaluate(cfg, st, data, today, now)
    st["last"] = {k: data.get(k) for k in ("y30", "fx", "fx_move")}
    st["updated_at"] = now.isoformat()
    sends = []
    if events:
        sends.append(build(cfg, events, data, stamp))
    else:
        print("no new condition; silent")
    if fails and st.get("last_fail_day") != today:
        title = "[476760] 조회 실패 | 조건 판단 보류"
        msg = "\n".join([
            f"{stamp} | 476760 | ACE 미국30년국채액티브(환노출)",
            "이게 뭐 알림: 476760 조건 점검이 일부 데이터를 못 읽었습니다. 매매 신호가 아닙니다.",
            "지금 결과/상태: 조회 실패: " + ", ".join(fails) + ".",
            "지금 할 일: 다음 거래일 19:00 재시도를 기다리세요.",
            "나중에: 읽힌 항목은 정상 판단했습니다.",
            "익절/줄일 때: 이 실패 알림만으로 줄이지 마세요.",
            "하지 말 것: 조회 실패를 조건 충족으로 읽기.",
        ])
        sends.append((title, msg, 3))
        if not dry:
            st["last_fail_day"] = today
    for title, msg, pri in sends:
        print(title)
        print(msg)
        print(f"topic={topic} send={not dry}")
        if not dry:
            publish(topic, title, msg, pri, ["chart_with_downwards_trend"], server)
    if not dry:
        save_state(st)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
