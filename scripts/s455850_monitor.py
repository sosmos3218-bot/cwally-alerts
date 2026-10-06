#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

KST = timezone.utc  # replaced below
KST = __import__("datetime").timezone(__import__("datetime").timedelta(hours=9))
ROOT = Path(__file__).resolve().parents[1]
CFG_PATH = ROOT / "configs" / "s455850.json"
STATE_PATH = ROOT / "state" / "s455850.json"
UA = {"User-Agent": "Mozilla/5.0 cwally-alerts"}


def load_json(path: Path, fallback: dict) -> dict:
    if not path.exists():
        return dict(fallback)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        fallback.update(data)
        return fallback
    except json.JSONDecodeError:
        return fallback


def http_get(url: str, timeout: int = 20) -> str:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def naver_session(code: str):
    sys.path.insert(0, str(ROOT / "scripts"))
    import naver_api

    rows = [r for r in naver_api.stock_trend(code, 5) if r["close"] is not None]
    if not rows:
        return None, None
    d = rows[0]["day"]
    return f"{d[:4]}-{d[4:6]}-{d[6:8]}", rows[0]["close"]


def yahoo_session(ticker: str):
    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        + urllib.parse.quote(ticker)
        + "?interval=1d&range=15d"
    )
    payload = json.loads(http_get(url))
    result = payload["chart"]["result"][0]
    ts = result.get("timestamp") or []
    quotes = (result.get("indicators") or {}).get("quote") or [{}]
    closes = quotes[0].get("close") or []
    volumes = quotes[0].get("volume") or []
    last_date = None
    last_close = None
    for i in range(len(ts) - 1, -1, -1):
        close = closes[i] if i < len(closes) else None
        vol = volumes[i] if i < len(volumes) else None
        if close is None:
            continue
        if vol is not None and vol <= 0:
            continue
        last_date = datetime.fromtimestamp(ts[i], tz=timezone.utc).astimezone(KST).strftime("%Y-%m-%d")
        last_close = float(close)
        break
    return last_date, last_close


def parse_num(text: str):
    raw = re.sub(r"[^0-9+\-]", "", text or "")
    if raw in {"", "+", "-"}:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def naver_foreign_bn(code: str, session_date: str, close):
    """Foreign net buy (억원) for session via Naver mobile trend API (legacy frgn page redirects)."""
    sys.path.insert(0, str(ROOT / "scripts"))
    import naver_api

    want = session_date.replace("-", "")
    try:
        rows = naver_api.stock_trend(code, 10)
    except Exception as exc:
        print(f"trend fail {code}: {exc}", file=sys.stderr)
        return None
    for r in rows:
        if r["day"] != want or r["foreign"] is None:
            continue
        px = close if close is not None else r["close"]
        if px is None:
            return None
        return r["foreign"] * px / 100_000_000.0
    return None


def publish(title: str, message: str, priority: int) -> None:
    sys.path.insert(0, str(ROOT / "scripts"))
    import ntfy_util

    ntfy_util.publish(
        "cwally-port",
        title,
        message,
        priority=priority,
        tags=["warning", "chart_with_downwards_trend"],
    )


def body(kind: str, stamp: str, session: str, trig_txt: str, px_txt: str) -> str:
    lines = [
        f"{stamp} | 455850 | 세션 {session}",
        "이게 뭐 알림: SOL AI반도체소부장 조건부 손절 점검이다.",
        f"지금 결과/상태: {px_txt} | {trig_txt}",
    ]
    if kind == "fail":
        lines += [
            "지금 할 일: 수동으로 455850 25,000원, 한미 222,500원, 주성 외국인 200억 매도만 확인한다.",
            "나중에: 데이터가 나오면 그날 한 번만 다시 판정한다.",
            "익절/줄일 때: 실패만으로 축소하거나 청산하지 않는다.",
            "하지 말 것: 재시도 알림을 연속으로 보내지 않는다. 장중 추정으로 채우지 않는다.",
        ]
    elif kind == "1":
        lines += [
            "지금 할 일: 신규 매수 금지. 기존 물량은 유지하고 다음 개장일 종가만 본다.",
            "나중에: 같은 트리거가 하루 더 나오거나 다른 트리거가 겹치면 2단계다.",
            "익절/줄일 때: 지금은 줄이지 않는다.",
            "하지 말 것: 장중 터치로 팔지 않는다. 25,000원 위에서 추격 매수하지 않는다.",
        ]
    elif kind == "2":
        lines += [
            "지금 할 일: 보유 수량의 50%를 정리한다. 남은 50%의 최종 선은 455850 종가 23,520원이다.",
            "나중에: 다음 개장일에도 회복 실패하거나 T1이 확정되면 3단계다.",
            "익절/줄일 때: 지금 절반만 줄인다. 전량은 아직 아니다.",
            "하지 말 것: 판 돈을 바로 재매수하지 않는다. 23,520원을 1차 손절로 올리지 않는다.",
        ]
    else:
        lines += [
            "지금 할 일: 잔량을 정리한다. 나누면 당일 종가와 다음날 시가 두 번만 쓴다.",
            "나중에: 재가동은 455850 종가 2거래일 연속 25,000원 위, 한미 222,500원 위, 주성 외국인 200억 매도 없음, 이수 외국인·기관 동시 순매수가 모두 나올 때만이다.",
            "익절/줄일 때: 지금 잔량을 비운다.",
            "하지 말 것: 반등만 보고 규칙을 끄지 않는다. 신용·미수로 물타기하지 않는다.",
        ]
    return "\n".join(lines)


def main() -> int:
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    today = now.strftime("%Y-%m-%d")
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    prev = load_json(
        STATE_PATH,
        {
            "updated_at": None,
            "session_date": None,
            "level": 0,
            "triggers": [],
            "last_sent_key": None,
            "fail_sent_date": None,
            "px_etf": None,
            "px_hanmi": None,
            "px_jusung": None,
            "jusung_foreign_bn": None,
        },
    )

    if now.year != int(cfg.get("oneshot_year") or now.year):
        print("year guard; silent")
        return 0

    holidays = set(cfg.get("kr_holidays") or [])
    etf_date = hanmi_date = jusung_date = None
    px_etf = px_hanmi = px_jusung = None
    price_fail = False
    try:
        # Naver trend lists only closed sessions; Yahoo intraday bars mixed live prices into the 종가 check.
        etf_date, px_etf = naver_session(cfg["etf"]["code"])
        hanmi_date, px_hanmi = naver_session(cfg["hanmi"]["code"])
        jusung_date, px_jusung = naver_session(cfg["jusung"]["code"])
    except Exception as exc:
        print(f"price fetch failed: {exc}", file=sys.stderr)
        price_fail = True

    session = etf_date or hanmi_date or jusung_date or prev.get("session_date")
    if px_etf is None or px_hanmi is None:
        price_fail = True

    holiday_today = today in holidays or now.weekday() >= 5
    if holiday_today and session and session == prev.get("session_date"):
        print(f"holiday {today}; last session {session} already stored; silent")
        return 0
    if session and session == prev.get("session_date") and prev.get("last_sent_key"):
        print(f"session {session} already evaluated; silent")
        STATE_PATH.write_text(
            json.dumps({**prev, "updated_at": stamp}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return 0

    foreign_bn = None
    foreign_fail = False
    if session and px_jusung is not None:
        try:
            foreign_bn = naver_foreign_bn(cfg["jusung"]["code"], session, px_jusung)
        except Exception as exc:
            print(f"foreign fetch failed: {exc}", file=sys.stderr)
            foreign_fail = True
        if foreign_bn is None:
            foreign_fail = True
    else:
        foreign_fail = True

    t1 = bool(px_etf is not None and px_etf < cfg["etf"]["stop"])
    t2 = bool(px_hanmi is not None and px_hanmi < cfg["hanmi"]["stop"])
    t3 = bool(foreign_bn is not None and foreign_bn <= -float(cfg["jusung"]["foreign_sell_bn"]))
    triggers = [name for name, on in (("T1", t1), ("T2", t2), ("T3", t3)) if on]
    prev_triggers = list(prev.get("triggers") or [])
    same_repeat = (
        bool(triggers)
        and set(triggers) == set(prev_triggers)
        and prev.get("session_date") not in {None, session}
    )

    missing = "없음"
    foreign_bit = missing if foreign_bn is None else f"{foreign_bn:+.1f}억"
    if price_fail or (not t1 and not t2 and foreign_fail):
        send = prev.get("fail_sent_date") != (session or today)
        px_txt = (
            f"ETF {px_etf or missing} / 한미 {px_hanmi or missing} / "
            f"주성 {px_jusung or missing} / 외국인 {foreign_bit}"
        )
        msg = body("fail", stamp, session or missing, "조회 실패. 조건 미충족이 아니다.", px_txt)
        print(msg)
        STATE_PATH.write_text(
            json.dumps(
                {
                    **prev,
                    "updated_at": stamp,
                    "session_date": session,
                    "fail_sent_date": session or today,
                    "px_etf": px_etf,
                    "px_hanmi": px_hanmi,
                    "px_jusung": px_jusung,
                    "jusung_foreign_bn": foreign_bn,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        if send:
            publish("[455850] 조회실패 | 수급 미확정", msg, 4)
        else:
            print("duplicate fail; silent")
        return 0

    level = 0
    if t1 or (int(prev.get("level") or 0) >= 2 and (t1 or len(triggers) >= 2)):
        level = 3
    elif len(triggers) >= 2 or same_repeat:
        level = 2
    elif len(triggers) == 1:
        level = 1

    if px_etf and px_hanmi and px_jusung:
        px_txt = f"ETF {px_etf:,.0f}원 / 한미 {px_hanmi:,.0f}원 / 주성 {px_jusung:,.0f}원"
    else:
        px_txt = f"ETF {px_etf} / 한미 {px_hanmi} / 주성 {px_jusung}"
    foreign_txt = "확인 불가" if foreign_bn is None else f"{foreign_bn:+.1f}억"
    trig_txt = ", ".join(triggers) if triggers else "트리거 없음"
    trig_txt += f" | 주성 외국인 {foreign_txt} | {level}단계"

    key = f"{session}:{level}"
    send = level >= 1 and prev.get("last_sent_key") != key
    title = {
        1: "[455850] 경고 | 신규 금지",
        2: "[455850] 축소 | 절반 정리",
        3: "[455850] 청산 | 잔량 정리",
    }.get(level, "[455850] 관찰")
    msg = body(str(level) if level else "1", stamp, session or missing, trig_txt, px_txt)
    print(msg)
    if level == 0:
        print("no trigger; silent")
        send = False

    STATE_PATH.write_text(
        json.dumps(
            {
                "updated_at": stamp,
                "session_date": session,
                "level": level,
                "triggers": triggers,
                "last_sent_key": key if send or level == 0 else prev.get("last_sent_key"),
                "fail_sent_date": None,
                "px_etf": px_etf,
                "px_hanmi": px_hanmi,
                "px_jusung": px_jusung,
                "jusung_foreign_bn": foreign_bn,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    if send:
        publish(title, msg, 5 if level == 3 else (4 if level == 2 else 3))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
