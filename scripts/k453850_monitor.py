#!/usr/bin/env python3
"""[453850] ACE 미국30년국채액티브(H) US30Y 관망 해제 / 리스크 / 52주 저점 점검.

morning 09:00 + k203780 19:00 (no new cron). Sources: FRED DGS30 primary, Yahoo ^TYX
fallback (same path 476760 already uses in Actions), Naver KRX daily closes.
Events (once per crossing, hysteresis rearm):
  upgrade  US30Y close < 5.00%  -> 관망 해제 점검 (rearm > 5.10)
  risk     US30Y close > 5.50%  -> 리스크 (rearm < 5.40)
  px_low   453850 close <= 6685 -> 분할매수 검토 (rearm >= 6886)
Review prompt only. Fed cut-cycle = manual (FOMC retired; usbond is IEF-only).
Modes: auto | dry | install | force | selftest | seed
"""
from __future__ import annotations

import csv
import io
import json
import re
import sys
import traceback
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from kr_calendar import KST, is_trading_day, session_day, session_close_ready  # noqa: E402
from ntfy_util import publish, resolve_server, resolve_topic  # noqa: E402

TAG = "[453850]"
NAME = "k453850"
CFG_PATH = ROOT / "configs" / f"{NAME}.json"
STATE_PATH = ROOT / "state" / f"{NAME}.json"
UA = {"User-Agent": "Mozilla/5.0 (cwally-alerts; +mailto:sosmos3218@gmail.com)"}
FORBIDDEN = ("\u00b7", "\u2014", "\u318d")
KEYS = ("upgrade", "risk", "px_low")


def scrub(s: str) -> str:
    for a, b in (("\u00b7", ", "), ("\u318d", ", "), ("\u2014", " - "), ("\u2027", ", "), ("\u30fb", ", ")):
        s = s.replace(a, b)
    # keep newlines (message body); collapse only horizontal runs
    s = re.sub(r"[^\S\n]+", " ", s)
    return "\n".join(line.strip() for line in s.strip().splitlines())


def check_text(t: str, m: str) -> None:
    for ch in FORBIDDEN:
        if ch in t or ch in m:
            raise ValueError(f"forbidden U+{ord(ch):04X} in {t}")


def get(url: str, timeout: int = 30) -> str:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def get_json(url: str):
    return json.loads(get(url))


def num(v) -> float | None:
    if v is None:
        return None
    try:
        return float(str(v).replace(",", "").strip())
    except ValueError:
        return None


def load_cfg() -> dict:
    return json.loads(CFG_PATH.read_text(encoding="utf-8"))


def load_state() -> dict:
    st = {
        "seeded": False,
        "updated_at": None,
        "last_day": None,
        "last_fail_day": None,
        "ops_day": None,
        "last": {},
    }
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


def ended(cfg: dict, now: datetime) -> bool:
    ends = cfg.get("ends_kst")
    if not ends:
        return False
    try:
        return now >= datetime.strptime(ends, "%Y-%m-%d %H:%M").replace(tzinfo=KST)
    except ValueError:
        return False


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


def fred_dgs30(start: date) -> tuple[str, float]:
    from fred_fetch import fetch_fred_csv
    txt = fetch_fred_csv("DGS30", start.isoformat())
    rows = list(csv.reader(io.StringIO(txt)))
    if not rows or "DGS30" not in (rows[0][-1] if rows[0] else ""):
        raise RuntimeError("FRED csv 형식 다름")
    last_d, last_v = None, None
    for r in rows[1:]:
        if len(r) >= 2:
            v = num(r[1])
            if v is not None:
                last_d, last_v = r[0], v
    if last_d is None or last_v is None:
        raise RuntimeError("FRED DGS30 숫자 없음")
    return last_d, last_v


def yahoo_tyx() -> tuple[str, float]:
    from datetime import timezone as _tz
    url = "https://query1.finance.yahoo.com/v8/finance/chart/%5ETYX?interval=1d&range=1mo"
    res = get_json(url)["chart"]["result"][0]
    ts = res.get("timestamp") or []
    closes = (res.get("indicators") or {}).get("quote", [{}])[0].get("close") or []
    last_d, last_v = None, None
    for t, c in zip(ts, closes):
        if c is None:
            continue
        d = datetime.fromtimestamp(int(t), tz=_tz.utc).date().isoformat()
        last_d, last_v = d, float(c)
    if last_d is None or last_v is None:
        raise RuntimeError("^TYX 값 없음")
    return last_d, last_v


def collect_yield(cfg: dict) -> tuple[float, str, str]:
    """Return (yield_pct, obs_day, source_label). Prefer fresher day; FRED on tie."""
    start = date.today() - timedelta(days=30)
    fred_err = tyx_err = None
    fred = tyx = None
    try:
        fred = fred_dgs30(start)
    except Exception as exc:
        fred_err = exc
    try:
        tyx = yahoo_tyx()
    except Exception as exc:
        tyx_err = exc
    if fred and tyx:
        if tyx[0] > fred[0]:
            return tyx[1], tyx[0], "Yahoo ^TYX"
        return fred[1], fred[0], "FRED DGS30"
    if fred:
        return fred[1], fred[0], "FRED DGS30"
    if tyx:
        return tyx[1], tyx[0], "Yahoo ^TYX"
    raise RuntimeError(f"미국 30년물 조회 실패 (FRED: {fred_err}; TYX: {tyx_err})")


def collect(cfg: dict) -> tuple[dict, list[str]]:
    data: dict = {}
    fails: list[str] = []
    try:
        data["rows"] = price_rows(cfg)
    except Exception as exc:
        fails.append(f"453850 시세 ({exc})")
    try:
        y, yd, src = collect_yield(cfg)
        data["y30"] = y
        data["y30_day"] = yd
        data["y30_src"] = src
    except Exception as exc:
        fails.append(f"미국 30년물 ({exc})")
    return data, fails



def fomc_cut_flag() -> dict | None:
    """Read shared flag written by scripts/fomccut_monitor.py."""
    path = ROOT / "state" / "fomccut.json"
    try:
        st = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    sig = st.get("signal") or {}
    if sig.get("active") and sig.get("class") in {"인하 사이클 진입", "인하 신호 강화"}:
        return sig
    return None

def div_yield_pct(cfg: dict, close: float | None) -> float | None:
    annual = num((cfg.get("dividend") or {}).get("annual_est_krw"))
    if not close or not annual:
        return None
    return annual / close * 100.0


def step(st: dict, key: str, hit: bool, rearm: bool, day: str, events: list, info: dict) -> None:
    s = st.setdefault(key, {"armed": True, "fired_day": None})
    if s.get("armed", True) and hit and s.get("fired_day") != day:
        s.update(armed=False, fired_day=day)
        events.append(dict(info, key=key, day=day))
    elif (not s.get("armed", True)) and rearm and (s.get("fired_day") or "") < day:
        s["armed"] = True
        print(f"{key} re-armed on {day}")


def evaluate(cfg: dict, st: dict, data: dict, today: str, now: datetime) -> list[dict]:
    events: list[dict] = []
    yz, pz = cfg["yield"], cfg["price"]
    rows = data.get("rows")
    if rows:
        start = st.get("last_day") or rows[-1]["day"]
        close_hm = tuple(map(int, cfg.get("market_close_kst", "15:30").split(":")))
        done = None
        for r in rows:
            if r["day"] < start:
                continue
            if not session_close_ready(r["day"], today, now, close_hm):
                continue
            step(
                st, "px_low",
                r["close"] <= pz["low_max"],
                r["close"] >= pz["low_rearm"],
                r["day"], events, {"close": r["close"]},
            )
            done = r["day"]
        if done:
            st["last_day"] = done
    y = data.get("y30")
    yday = data.get("y30_day") or today
    if y is not None:
        step(
            st, "upgrade",
            y < yz["upgrade_below"],
            y > yz["upgrade_rearm"],
            yday, events, {"y30": y, "y30_day": yday, "y30_src": data.get("y30_src")},
        )
        step(
            st, "risk",
            y > yz["risk_above"],
            y < yz["risk_rearm"],
            yday, events, {"y30": y, "y30_day": yday, "y30_src": data.get("y30_src")},
        )
    return events


def status_line(cfg: dict, data: dict) -> str:
    rows = data.get("rows") or []
    n = datetime.now(KST)
    live = bool(rows) and not session_close_ready(rows[-1]["day"], n.date().isoformat(), n, (15, 30))
    close = rows[-1]["close"] if rows else None
    px = (
        f"453850 {'현재가' if live else '종가'} {close:,.0f}원({rows[-1]['day']})"
        if rows else "453850 시세 없음"
    )
    if data.get("y30") is not None:
        y = f"미국 30년물 {data['y30']:.3f}%({data.get('y30_day')}, {data.get('y30_src')})"
    else:
        y = "미국 30년물 없음"
    dy = div_yield_pct(cfg, close)
    div = f"배당수익률 추정 약 {dy:.1f}%(연 {cfg['dividend']['annual_est_krw']:.0f}원/종가)" if dy is not None else "배당수익률 추정 없음"
    yz, pz = cfg["yield"], cfg["price"]
    if data.get("y30") is not None:
        d5 = (data["y30"] - yz["upgrade_below"]) * 100
        d55 = (yz["risk_above"] - data["y30"]) * 100
        dist = f"5.00%까지 {d5:+.0f}bp, 5.50%까지 {d55:+.0f}bp"
    else:
        dist = "트리거 거리 없음"
    if close is not None:
        dpx = close - pz["low_max"]
        dist += f", 6,685원까지 {dpx:+,.0f}원"
    return f"{px}. {y}. {div}. {dist}."


def checklist(cfg: dict, data: dict) -> str:
    y = data.get("y30")
    yz = cfg["yield"]
    bits = []
    if y is not None:
        bits.append("재평가(관망 해제): US30Y < 5.0% " + ("충족" if y < yz["upgrade_below"] else "미충족"))
        bits.append("리스크: US30Y > 5.5% " + ("충족" if y > yz["risk_above"] else "미충족"))
    flag = fomc_cut_flag()
    if flag:
        bits.append(
            f"FOMC 인하 신호 확인됨 ({flag.get('meeting')}, {flag.get('class')})"
        )
    else:
        bits.append("Fed 인하 사이클 시사: 수동 확인(자동 감지 없음, fomccut 대기)")
    rows = data.get("rows") or []
    if rows:
        c = rows[-1]["close"]
        bits.append(f"가격 52주 저(6,685) 근접: 종가 {c:,.0f}원 " + ("충족" if c <= cfg["price"]["low_max"] else "미충족"))
    return "점검표: " + " / ".join(bits) + "."


def body(stamp: str, hdr: str, what: str, status: str, action: str, later: str, trim: str, avoid: str) -> str:
    return "\n".join([
        f"{stamp} | 453850 | ACE 미국30년국채액티브(H) {hdr}",
        f"이게 뭐 알림: {what}",
        f"지금 결과/상태: {status}",
        f"지금 할 일: {action}",
        f"나중에: {later}",
        f"익절/줄일 때: {trim}",
        f"하지 말 것: {avoid}",
    ])


def build(cfg: dict, events: list[dict], data: dict, stamp: str) -> tuple[str, str, int]:
    keys = {e["key"] for e in events}
    yz, pz = cfg["yield"], cfg["price"]
    hits = []
    if "upgrade" in keys:
        e = next(e for e in events if e["key"] == "upgrade")
        hits.append(
            f"관망 해제 점검: 미국 30년물 {e['y30']:.3f}%({e.get('y30_day')}, {e.get('y30_src')}), "
            f"{yz['upgrade_below']:.2f}% 하회."
        )
    if "risk" in keys:
        e = next(e for e in events if e["key"] == "risk")
        hits.append(
            f"리스크: 미국 30년물 {e['y30']:.3f}%({e.get('y30_day')}, {e.get('y30_src')}), "
            f"{yz['risk_above']:.2f}% 상회. 초장기 듀레이션 가격 하락 위험."
        )
    if "px_low" in keys:
        e = next(e for e in events if e["key"] == "px_low")
        hits.append(f"분할매수 검토: 종가 {e['close']:,.0f}원, 52주 저 {pz['low_max']:,}원 이하({e['day']}).")

    if "risk" in keys:
        title = f"{TAG} US30Y 5.5% 상회 | 리스크 점검"
        action = "신규 진입을 멈추고 듀레이션 위험을 다시 보세요. Fed 스탠스와 보유 비중을 수동으로 점검하세요."
        pri = 4
        hdr = "리스크"
    elif "upgrade" in keys:
        title = f"{TAG} US30Y 5.0% 하회 | 관망 해제 점검"
        action = "관망 해제 여부를 점검하세요. Fed가 인하 사이클을 시사했는지 직접 확인한 뒤 분할 진입만 검토하세요. 매수 지시가 아닙니다."
        pri = 4
        hdr = "관망 해제"
    else:
        title = f"{TAG} 종가 {pz['low_max']:,}원 이하 | 분할매수 검토"
        action = "52주 저점 부근입니다. 계획한 소액 분할만 검토하세요. 금리 방향이 불확실하면 관망을 유지하세요."
        pri = 3
        hdr = "분할매수 검토"

    status = " ".join(hits) + " " + status_line(cfg, data) + " " + checklist(cfg, data)
    msg = body(
        stamp, hdr,
        "453850(헤지) 조건(US30Y 5.0%/5.5%, 종가 6,685원)이 새로 켜질 때만 오는 알림입니다. 매매 신호가 아닙니다. 리뷰 프롬프트만입니다.",
        scrub(status),
        action,
        f"기본 태도는 관망입니다. {cfg['thesis']['stance']} 같은 조건은 재무장 선(5.10% / 5.40% / 6,886원)을 넘었다 와야 다시 옵니다. Fed 인하 시사는 fomccut 알림(또는 수동).",
        "해당 없음(미보유/미등록). 이미 보유 중이면 듀레이션 위험을 먼저 보세요.",
        "이 알림을 매수나 매도 신호로 읽기. 한 번에 큰돈 넣기. 조회 실패를 신호로 읽기.",
    )
    return scrub(title), scrub(msg), pri


def ops_msg(stamp: str, status: str) -> tuple[str, str]:
    t = f"{TAG} 조회 실패 | 조건 판단 보류"
    m = body(
        stamp, "조회 실패",
        "453850 조건 점검이 일부 데이터를 못 읽었습니다. 매매 신호가 아닙니다.",
        status,
        "다음 슬롯(09:00 또는 19:00) 재시도를 기다리세요.",
        "읽힌 항목은 정상 판단했습니다.",
        "이 실패 알림만으로 줄이지 마세요.",
        "조회 실패를 조건 충족으로 읽기.",
    )
    return scrub(t), scrub(m)


def install_message(cfg: dict, stamp: str, data: dict, fails: list[str]) -> tuple[str, str]:
    t = f"{TAG} 설치 확인 | 매매 신호 아님"
    status = status_line(cfg, data)
    if fails:
        status += " 조회 실패: " + ", ".join(fails) + "."
    status += f" 보유: {cfg.get('holding', '미등록')}."
    m = body(
        stamp, "설치 확인",
        "ACE 미국30년국채액티브(H)(453850) 금리/가격 감시 알림 설치 확인입니다. 매매 신호가 아닙니다.",
        status,
        "알림이 도착했는지 확인만 하세요.",
        "평일 09:00, 19:00 KST 에 FRED DGS30(실패 시 Yahoo ^TYX)과 네이버 종가를 봅니다. "
        "US30Y 5.0% 하회(관망 해제), 5.5% 상회(리스크), 종가 6,685 이하(분할매수 검토)가 나오면 1번씩. "
        "Fed 인하 시사는 fomccut 알림(또는 수동). 끝: " + str(cfg.get("ends_kst")) + ".",
        "해당 없음.",
        "이 메시지를 매매 신호로 해석하기.",
    )
    # append ends line like other installs
    m += f"\n꺼지는 때: {cfg.get('ends_kst')} ({cfg.get('ends_note')})"
    return scrub(t), scrub(m)


def run(mode: str, as_of: datetime | None = None) -> int:
    dry = mode in {"dry", "test", "dry-run"}
    cfg = load_cfg()
    now = as_of or datetime.now(KST)
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    day = session_day(now)
    today = day.isoformat()
    topic = resolve_topic("port", cfg.get("ntfy_topic"))
    server = resolve_server(cfg.get("ntfy_server"))
    st = load_state()

    if ended(cfg, now) and mode not in {"install", "selftest", "dry", "force"}:
        print(f"{today} past ends_kst; silent")
        return 0

    if mode == "install":
        data, fails = collect(cfg)
        t, m = install_message(cfg, stamp, data, fails)
        check_text(t, m)
        print(stamp, f"{NAME} mode=install")
        print(t)
        print(m)
        if not dry:
            publish(topic, t, m, 3, ["white_check_mark"], server, config_name=NAME)
            print("install confirmation sent (state untouched)")
        return 0

    if (not is_trading_day(day)) and mode not in {"force", "dry", "seed"}:
        print(f"{today} KRX holiday; silent")
        return 0

    data, fails = collect(cfg)
    print(
        f"data y30={data.get('y30')} day={data.get('y30_day')} src={data.get('y30_src')} "
        f"rows={(data.get('rows') or [])[-3:]} fails={fails}"
    )

    if mode == "seed" or not st.get("seeded"):
        tmp = {k: {"armed": True, "fired_day": None} for k in KEYS}
        tmp["last_day"] = None
        ev = evaluate(cfg, tmp, data, today, now)
        for k in KEYS:
            st[k] = tmp[k]
        done_rows = [
            r["day"] for r in (data.get("rows") or [])
            if session_close_ready(r["day"], today, now, (15, 30))
        ]
        st["last_day"] = tmp.get("last_day") or (done_rows[-1] if done_rows else None)
        st["seeded"] = True
        st["seed_note"] = f"{stamp} 시드. 그때 이미 켜진 조건: {[e['key'] for e in ev] or '없음'}"
        st["last"] = {k: data.get(k) for k in ("y30", "y30_day", "y30_src")}
        st["updated_at"] = now.isoformat()
        print(st["seed_note"])
        if not dry:
            save_state(st)
        else:
            print("DRY RUN: no publish, state not written")
        return 0

    events = evaluate(cfg, st, data, today, now)
    st["last"] = {k: data.get(k) for k in ("y30", "y30_day", "y30_src")}
    st["updated_at"] = now.isoformat()
    sends: list[tuple[str, str, int, str]] = []
    if events:
        title, msg, pri = build(cfg, events, data, stamp)
        check_text(title, msg)
        sends.append((title, msg, pri, topic))
    else:
        print("no new condition; silent")

    if fails and st.get("last_fail_day") != today:
        ot, om = ops_msg(stamp, "조회 실패: " + ", ".join(fails) + ".")
        check_text(ot, om)
        sends.append((ot, om, 3, resolve_topic("ops")))
        if not dry:
            st["last_fail_day"] = today
            st["ops_day"] = today

    for title, msg, pri, top in sends:
        print(title)
        print(msg)
        print(f"topic={top} send={not dry}")
        if not dry:
            tags = ["warning"] if top == resolve_topic("ops") else ["chart_with_downwards_trend"]
            publish(top, title, msg, pri, tags, server, config_name=NAME)

    if dry:
        print("DRY RUN: no publish, state not written")
    else:
        save_state(st)
    return 0


def selftest() -> int:
    cfg = load_cfg()
    assert cfg["code"] == "453850"
    assert cfg["yield"]["upgrade_below"] == 5.0
    assert cfg["yield"]["risk_above"] == 5.5
    assert cfg["price"]["low_max"] == 6685
    # synthetic evaluate
    st = {k: {"armed": True, "fired_day": None} for k in KEYS}
    st["last_day"] = None
    data = {
        "y30": 4.95,
        "y30_day": "2026-11-04",
        "y30_src": "FRED DGS30",
        "rows": [
            {"day": "2026-11-03", "close": 7110},
            {"day": "2026-11-04", "close": 6600},
        ],
    }
    now = datetime(2026, 11, 5, 19, 0, tzinfo=KST)
    ev = evaluate(cfg, st, data, "2026-11-05", now)
    keys = {e["key"] for e in ev}
    assert "upgrade" in keys, keys
    assert "px_low" in keys, keys
    assert "risk" not in keys, keys
    title, msg, pri = build(cfg, ev, data, "2026-11-05 19:00 KST")
    check_text(title, msg)
    assert "5.0%" in title or "관망" in title
    assert "매수 지시" not in msg or "매수 지시가 아닙니다" in msg
    for ch in FORBIDDEN:
        assert ch not in title and ch not in msg
    # risk path
    st2 = {k: {"armed": True, "fired_day": None} for k in KEYS}
    st2["last_day"] = "2026-11-05"
    data2 = {"y30": 5.55, "y30_day": "2026-11-05", "y30_src": "Yahoo ^TYX", "rows": [{"day": "2026-11-05", "close": 7000}]}
    ev2 = evaluate(cfg, st2, data2, "2026-11-05", now)
    assert {e["key"] for e in ev2} == {"risk"}
    t2, m2, _ = build(cfg, ev2, data2, "T")
    check_text(t2, m2)
    assert "5.5%" in t2
    # rearm
    step(st2, "risk", False, True, "2026-11-06", [], {})
    assert st2["risk"]["armed"] is True
    # install / ops text
    for t, m in (ops_msg("T", "x"), install_message(cfg, "T", data, [])):
        check_text(t, m)
    # dry seed path without network mutation of real state: fake collect via evaluate only
    print("fire upgrade+px:", title)
    print(msg.split("\n")[0])
    print("fire risk:", t2)
    print("selftest ok")
    return 0


def main() -> int:
    argv = sys.argv[1:]
    mode = (argv[0] if argv else "auto").lower()
    as_of = None
    if "--as-of" in argv:
        i = argv.index("--as-of")
        as_of = datetime.strptime(argv[i + 1], "%Y-%m-%d %H:%M").replace(tzinfo=KST)
    if mode == "selftest":
        return selftest()
    if mode not in {"auto", "dry", "install", "force", "seed"}:
        print(f"usage: {NAME}_monitor.py <auto|dry|install|force|seed|selftest> [--as-of 'YYYY-MM-DD HH:MM']")
        return 2
    try:
        if mode == "dry":
            print("DRY RUN")
        return run(mode, as_of)
    except Exception as exc:
        traceback.print_exc()
        try:
            cfg = load_cfg()
            t, m = ops_msg(
                datetime.now(KST).strftime("%Y-%m-%d %H:%M KST"),
                f"스크립트 오류 {type(exc).__name__}: {str(exc)[:200]}",
            )
            if mode not in {"dry", "selftest"}:
                publish(
                    resolve_topic("ops"), t, m, 3, ["warning"],
                    resolve_server(cfg.get("ntfy_server")), config_name=NAME,
                )
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
