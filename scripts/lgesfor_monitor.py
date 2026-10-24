#!/usr/bin/env python3
"""[LGESFOR] LG에너지솔루션(373220) 외국인 지분율 경고선 감시. k203780 19:00 슬롯에 부착.

규칙(히스테리시스, configs/lgesfor.json levels):
  지분율 < 5.50%  -> '경고선 이탈' (priority 4), 경고선 무장 해제
  지분율 < 5.45%  -> '추가 이탈' (priority 5), 추가선 무장 해제
  이탈 뒤 >= 5.55% -> '해제' (priority 3), 두 선 다시 무장
  상태가 바뀔 때만 발송, 아니면 조용히 state 만 기록.

데이터: 네이버 모바일 일별 추이(bizdate, foreignerHoldRatio). 판정은 데이터 자체의 거래일(bizdate)로 중복 제거.
  - state.last_date 보다 새 날짜만 순서대로 판정 (실행이 빠진 날도 따라잡음)
  - 같은 날짜(last_date) 값이 바뀌면(늦은 반영, 정정) 그 값만 한 번 더 판정
  - 지분율 칸이 비어 있으면 그 날은 판정하지 않음 (다음 실행에서 판정)
modes: auto | dry(발송 없음, state 안 씀, --as-of "YYYY-MM-DD HH:MM") | selftest(가짜 값, 네트워크 없음)
"""
from __future__ import annotations

import json
import sys
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from kr_calendar import is_trading_day, prev_trading_day, session_day  # noqa: E402
from naver_api import get_json, num  # noqa: E402
from ntfy_util import publish, resolve_server, resolve_topic, route_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
CFG_PATH = ROOT / "configs" / "lgesfor.json"
STATE_PATH = ROOT / "state" / "lgesfor.json"
NA = "확인 불가"
TAG = "[LGESFOR]"


# ---------- 데이터 ----------
def ratio_of(v) -> float | None:
    x = num(v)
    return x if x is not None and 0 < x < 100 else None


def parse_rows(raw) -> list[dict]:
    """네이버 trend 응답 -> [{date 'YYYY-MM-DD', ratio, net, close}] 오래된 것부터."""
    out = {}
    for r in raw if isinstance(raw, list) else []:
        if not isinstance(r, dict):
            continue
        d = str(r.get("bizdate") or "")
        if len(d) != 8 or not d.isdigit():
            continue
        day = f"{d[:4]}-{d[4:6]}-{d[6:]}"
        out[day] = {"date": day, "ratio": ratio_of(r.get("foreignerHoldRatio")),
                    "net": num(r.get("foreignerPureBuyQuant")), "close": num(r.get("closePrice"))}
    return [out[k] for k in sorted(out)]


def fetch_rows(code: str) -> list[dict]:
    return parse_rows(get_json(f"https://m.stock.naver.com/api/stock/{code}/trend?pageSize=20"))


def fetch_live_rate(code: str) -> float | None:
    """시세 화면 '외인소진율' (참고 기록용, 판정에 안 씀)."""
    try:
        raw = get_json(f"https://m.stock.naver.com/api/stock/{code}/integration")
        for x in raw.get("totalInfos", []):
            if x.get("code") == "foreignRate":
                return ratio_of(x.get("value"))
    except Exception as exc:  # noqa: BLE001
        print(f"integration fail: {exc}")
    return None


# ---------- 판정 ----------
def step(st: dict, ratio: float, lv: dict) -> str | None:
    """state(warn_armed, extra_armed)를 바꾸고 이벤트 이름을 돌려줌."""
    warn, extra, rel = lv["warn_below_pct"], lv["extra_below_pct"], lv["release_at_or_above_pct"]
    if ratio < extra and st["extra_armed"]:
        ev = "both" if st["warn_armed"] else "extra"
        st["warn_armed"] = st["extra_armed"] = False
        return ev
    if ratio < warn and st["warn_armed"]:
        st["warn_armed"] = False
        return "warn"
    if ratio >= rel and not (st["warn_armed"] and st["extra_armed"]):
        st["warn_armed"] = st["extra_armed"] = True
        return "release"
    return None


def pending(rows: list[dict], st: dict) -> list[tuple[dict, bool]]:
    """판정할 (row, 정정여부) 목록. 새 날짜 + 같은 날짜 값 변경만."""
    out = []
    last_d, last_r = st.get("last_date"), st.get("last_ratio")
    for r in rows:
        if r["ratio"] is None:
            continue
        if last_d is None or r["date"] > last_d:
            out.append((r, False))
        elif r["date"] == last_d and last_r is not None and abs(r["ratio"] - last_r) > 1e-9:
            out.append((r, True))
    return out


# ---------- 본문 ----------
def pp(x: float | None, sign: bool = True) -> str:
    if x is None:
        return NA
    return f"{x:+.2f}%p" if sign else f"{x:.2f}%"


def streak(rows: list[dict], upto: str) -> str:
    seq = [r for r in rows if r["date"] <= upto]
    if not seq or seq[-1]["net"] is None:
        return NA
    last = seq[-1]["net"]
    if last == 0:
        return "보합(순매매 0주)"
    sign, n, tot = (1 if last > 0 else -1), 0, 0.0
    for r in reversed(seq):
        if r["net"] is None or r["net"] == 0 or (r["net"] > 0) != (sign > 0):
            break
        n += 1
        tot += r["net"]
    word = "순매수" if sign > 0 else "순매도"
    return f"{n}거래일 연속 {word} (합계 {tot:+,.0f}주, 기준일 {seq[-1]['net']:+,.0f}주)"


def facts(rows: list[dict], row: dict, lv: dict) -> dict:
    seq = [r for r in rows if r["date"] <= row["date"] and r["ratio"] is not None]
    i = len(seq) - 1
    prev = seq[i - 1] if i >= 1 else None
    five = seq[i - 5] if i >= 5 else None
    return {
        "d1": (row["ratio"] - prev["ratio"]) if prev else None, "d1_date": prev["date"] if prev else None,
        "d5": (row["ratio"] - five["ratio"]) if five else None, "d5_date": five["date"] if five else None,
        "to_warn": row["ratio"] - lv["warn_below_pct"], "to_extra": row["ratio"] - lv["extra_below_pct"],
        "streak": streak(rows, row["date"]),
    }


def md(d: str | None) -> str:
    return f"{d[5:7]}/{d[8:]}" if d else NA


def body(stamp: str, what: str, status: str, action: str, later: str, trim: str, avoid: str) -> str:
    return "\n".join([f"{stamp} | LGESFOR | LG에너지솔루션 외국인 지분율", f"이게 뭐 알림: {what}", f"지금 결과/상태: {status}",
                      f"지금 할 일: {action}", f"나중에: {later}", f"익절/줄일 때: {trim}", f"하지 말 것: {avoid}"])


EV = {
    "warn": ("경고선 이탈", "warn"),
    "extra": ("추가 이탈", "extra"),
    "both": ("경고선 이탈 + 추가 이탈", "extra"),
    "release": ("해제(회복)", "release"),
}


def status_line(row: dict, f: dict, lv: dict, corrected: bool, st: dict) -> str:
    arm = "경고선 무장" if st["warn_armed"] else "경고선 이탈 중"
    arm += ", 추가선 무장" if st["extra_armed"] else ", 추가선 이탈 중"
    return (f"데이터 기준일 {row['date']}{' (같은 날짜 값 정정 반영)' if corrected else ''}, 외국인 지분율 {row['ratio']:.2f}%. "
            f"전 거래일({md(f['d1_date'])}) 대비 {pp(f['d1'])}, 5거래일 전({md(f['d5_date'])}) 대비 {pp(f['d5'])}. "
            f"경고선 {lv['warn_below_pct']:.2f}% 대비 {pp(f['to_warn'])}, 추가선 {lv['extra_below_pct']:.2f}% 대비 {pp(f['to_extra'])} "
            f"(해제선 {lv['release_at_or_above_pct']:.2f}% 이상). 외국인 순매매: {f['streak']}. 지금 상태: {arm}. "
            f"출처 네이버 증권 일별 외국인 보유율.")


def build_msg(cfg: dict, stamp: str, ev: str, row: dict, f: dict, corrected: bool, st: dict) -> tuple[str, str, int, list[str]]:
    lv, word = cfg["levels"], EV[ev][0]
    pri = cfg["priority"][EV[ev][1]]
    title = f"{TAG} LG엔솔 외국인 지분율 {word} {row['ratio']:.2f}% ({md(row['date'])})"
    if ev == "release":
        what = (f"LG에너지솔루션 외국인 지분율이 이탈 뒤 {lv['release_at_or_above_pct']:.2f}% 이상으로 회복해 경고를 풀고 "
                f"두 선(5.50%, 5.45%)을 다시 무장했다는 알림. 매매 신호 아님.")
        action = "할 일 없음. 외국인 수급 경고가 풀렸다는 사실만 확인."
        tags = ["white_check_mark"]
    else:
        what = (f"LG에너지솔루션 외국인 지분율이 경고선 아래로 내려갔다는 알림"
                f"({lv['warn_below_pct']:.2f}% 미만 경고, {lv['extra_below_pct']:.2f}% 미만 추가). 매매 신호 아님.")
        action = "지분율과 순매매 흐름만 확인. 이 알림 하나로 매수나 매도를 정하지 않기."
        tags = ["warning"]
    later = f"KRX 거래일 19:00 마다 다시 점검. 상태가 바뀔 때만 다시 옵니다(해제선 {lv['release_at_or_above_pct']:.2f}%)."
    trim = "지분율만으로 비중을 줄이지 않기. 가격 기준(손절, 목표)은 따로 정한 대로."
    avoid = "하루 숫자로 외국인 이탈을 단정하기, 반영이 늦은 날의 값을 그날 값으로 읽기."
    return title, body(stamp, what, status_line(row, f, lv, corrected, st), action, later, trim, avoid), pri, tags


# ---------- state ----------
def load_state() -> dict:
    st = json.loads(STATE_PATH.read_text(encoding="utf-8")) if STATE_PATH.exists() else {}
    st.setdefault("warn_armed", True)
    st.setdefault("extra_armed", True)
    st.setdefault("history", {})
    st.setdefault("obs", [])
    return st


def save_state(st: dict) -> None:
    hist = st["history"]
    for k in sorted(hist)[:-40]:
        del hist[k]
    st["obs"] = st["obs"][-30:]
    STATE_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# ---------- 실행 ----------
def evaluate(cfg: dict, st: dict, rows: list[dict], stamp: str) -> list[tuple[str, str, int, list[str]]]:
    msgs = []
    for row, corrected in pending(rows, st):
        ev = step(st, row["ratio"], cfg["levels"])
        st["history"][row["date"]] = row["ratio"]
        st["last_date"], st["last_ratio"] = row["date"], row["ratio"]
        print(f"judge {row['date']} {row['ratio']:.2f}%{' (정정)' if corrected else ''} -> {ev or '변화 없음'}")
        if ev:
            msgs.append(build_msg(cfg, stamp, ev, row, facts(rows, row, cfg["levels"]), corrected, st))
            st.setdefault("events", []).append({"date": row["date"], "ratio": row["ratio"], "event": ev, "at": stamp})
            st["events"] = st["events"][-20:]
    return msgs


def ops_msg(cfg: dict, stamp: str, title_tail: str, status: str) -> tuple[str, str]:
    return (f"{TAG} {title_tail}",
            body(stamp, "LG엔솔 외국인 지분율 데이터 수집 안내. 매매 신호 아님.", status,
                 "할 일 없음. 다음 19:00 실행 때 다시 확인합니다.", "같은 안내가 며칠 이어지면 데이터 출처 점검 필요.",
                 "이 안내로 비중을 바꾸지 않기.", "데이터 없음을 조건 미충족(안전)으로 읽기."))


def run(mode: str, now: datetime, cfg: dict) -> int:
    dry = mode == "dry"
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    sess = session_day(now)
    trading = is_trading_day(sess)
    print(f"now={stamp} session={sess} trading={trading} mode={mode}")
    if not trading and not dry:
        print("not a KRX trading day: skip")
        return 0
    if not trading:
        print("not a KRX trading day (dry: 그래도 조회와 판정만 보여줌)")
    server = resolve_server(cfg.get("ntfy_server"))
    st = load_state()
    before = json.dumps(st, sort_keys=True)
    err = None
    try:
        rows = fetch_rows(cfg["ticker"])
    except Exception as exc:  # noqa: BLE001
        rows, err = [], f"{type(exc).__name__}: {str(exc)[:150]}"
    live = fetch_live_rate(cfg["ticker"])
    with_ratio = [r for r in rows if r["ratio"] is not None]
    latest = with_ratio[-1] if with_ratio else None
    today_row = next((r for r in rows if r["date"] == sess.isoformat()), None)
    obs = {"run": stamp, "session": sess.isoformat(), "latest_date": latest["date"] if latest else None,
           "latest_ratio": latest["ratio"] if latest else None,
           "session_row": ("없음" if today_row is None else ("지분율 빈칸" if today_row["ratio"] is None else f"{today_row['ratio']:.2f}%")),
           "live_foreignRate": live}
    print("obs", json.dumps(obs, ensure_ascii=False))
    print("rows", [(r["date"], r["ratio"], r["net"]) for r in rows[-7:]])
    st["obs"].append(obs)
    msgs, ops = [], None
    if not latest:
        ops = ops_msg(cfg, stamp, "지분율 조회 실패 | 이번 실행 판정 못 함",
                      f"네이버 일별 외국인 보유율 {NA} ({err or '지분율 칸 없음'}). state 판정 상태는 그대로.")
    else:
        msgs = evaluate(cfg, st, rows, stamp)
        if not msgs:
            f = facts(rows, latest, cfg["levels"])
            print("quiet:", status_line(latest, f, cfg["levels"], False, st))
        expect = prev_trading_day(sess) if trading else None
        if expect and latest["date"] < expect.isoformat() and st.get("stale_noticed") != latest["date"]:
            ops = ops_msg(cfg, stamp, "지분율 미게시 | 판정 하루 이상 밀림",
                          f"최신 지분율 기준일 {latest['date']} ({latest['ratio']:.2f}%), 기대 기준일 {expect.isoformat()} 이상. "
                          "새 값이 올라오면 다음 실행에서 순서대로 판정.")
            st["stale_noticed"] = latest["date"]
    for t, m, p, tg in msgs:
        assert route_topic("cwally-port", t) == "cwally-port", t
        print(f"TITLE: {t}\nPRIORITY: {p} TOPIC: cwally-port\n{m}\n")
        if not dry:
            publish(resolve_topic("port", cfg.get("ntfy_topic")), t, m, p, tg, server, config_name="lgesfor")
    if ops:
        print(f"TITLE: {ops[0]}\nPRIORITY: 3 TOPIC: {route_topic('cwally-port', ops[0])}\n{ops[1]}\n")
        if not dry:
            publish(resolve_topic("ops"), ops[0], ops[1], 3, ["warning"], server, config_name="lgesfor")
    st["last_run"] = stamp
    if dry:
        print("DRY RUN: no publish, state not written", "(state would change)" if json.dumps(st, sort_keys=True) != before else "")
        return 0
    save_state(st)
    return 0


# ---------- selftest ----------
def selftest(cfg: dict) -> int:
    lv = cfg["levels"]
    stamp = "2026-11-02 19:00 KST"

    def mk(seq):
        return [{"date": f"2026-10-{i + 10:02d}", "ratio": r, "net": n, "close": 380000.0} for i, (r, n) in enumerate(seq)]

    def fresh(last_date="2026-10-09", last_ratio=5.56):
        return {"warn_armed": True, "extra_armed": True, "history": {}, "obs": [], "last_date": last_date, "last_ratio": last_ratio}

    # 1) 5.56 -> 5.49 경고, 5.47 조용, 5.44 추가, 5.52 조용, 5.55 해제, 5.49 다시 경고
    seq = [(5.56, 100), (5.49, -200), (5.47, -300), (5.44, -100), (5.52, 50), (5.55, 70), (5.49, -10)]
    st = fresh()
    evs = []
    for i in range(len(seq)):
        rows = mk(seq[: i + 1])
        evs.append([m[0].split(" 지분율 ")[1].split(" ")[0] for m in evaluate(cfg, st, rows, stamp)])
    assert evs == [[], ["경고선"], [], ["추가"], [], ["해제(회복)"], ["경고선"]], evs
    print("seq ok:", evs)
    # 2) 같은 날짜 재실행은 판정 안 함 (중복 없음)
    rows = mk(seq)
    assert evaluate(cfg, st, rows, stamp) == []
    # 3) 한 번에 5.44 로 떨어지면 하나의 우선순위 5 알림
    st = fresh()
    m = evaluate(cfg, st, mk([(5.56, 1), (5.44, -5)]), stamp)
    assert len(m) == 1 and m[0][2] == 5 and "경고선 이탈 + 추가 이탈" in m[0][0], m
    # 4) 늦은 반영: 19:00 에 그날 값이 전날 값 그대로(5.51) -> 조용, 다음 날 정정 5.49 -> 경고
    st = fresh()
    evaluate(cfg, st, mk([(5.52, 1), (5.51, -1)]), stamp)
    m = evaluate(cfg, st, mk([(5.52, 1), (5.49, -1)]), stamp)
    assert len(m) == 1 and m[0][2] == 4 and "정정 반영" in m[0][1], m
    # 5) 지분율 빈칸인 날은 건너뛰고 다음 실행에서 판정
    st = fresh()
    assert evaluate(cfg, st, mk([(5.52, 1), (None, -1)]), stamp) == [] and st["last_date"] == "2026-10-10"
    m = evaluate(cfg, st, mk([(5.52, 1), (5.48, -1)]), stamp)
    assert len(m) == 1 and m[0][2] == 4
    # 6) 여러 날 빠진 실행은 순서대로 따라잡음
    st = fresh()
    m = evaluate(cfg, st, mk([(5.56, 1), (5.49, -1), (5.44, -1), (5.56, 3)]), stamp)
    assert [x[2] for x in m] == [4, 5, 3], [x[2] for x in m]
    # 7) 본문 숫자: 전일/5일 대비, 거리, 연속 순매도
    rows = mk([(5.60, 5), (5.58, -1), (5.57, 2), (5.56, -3), (5.55, -4), (5.49, -5)])
    f = facts(rows, rows[-1], lv)
    assert abs(f["d1"] + 0.06) < 1e-9 and abs(f["d5"] + 0.11) < 1e-9 and abs(f["to_warn"] + 0.01) < 1e-9
    assert f["streak"].startswith("3거래일 연속 순매도 (합계 -12주"), f["streak"]
    f2 = facts(rows[:2], rows[1], lv)
    assert f2["d5"] is None and pp(f2["d5"]) == NA
    assert streak([{"date": "2026-10-10", "ratio": 5.5, "net": None, "close": None}], "2026-10-10") == NA
    # 8) 파서
    raw = [{"bizdate": "20261023", "foreignerHoldRatio": "5.56%", "foreignerPureBuyQuant": "+19,978", "closePrice": "381,500"},
           {"bizdate": "20261022", "foreignerHoldRatio": "", "foreignerPureBuyQuant": "-1", "closePrice": "1"}]
    pr = parse_rows(raw)
    assert pr[0]["date"] == "2026-10-22" and pr[0]["ratio"] is None and pr[1]["ratio"] == 5.56 and pr[1]["net"] == 19978, pr
    # 9) 표본, 금지 문자, 토픽
    st = fresh()
    rows = mk([(5.60, 5), (5.58, -1), (5.57, 2), (5.56, -3), (5.55, -4), (5.49, -5), (5.44, -6), (5.56, 9)])
    for t, msg, p, tg in evaluate(cfg, st, rows, stamp):
        assert "\u00b7" not in t + msg and "\u2014" not in t + msg, t
        assert route_topic("cwally-port", t) == "cwally-port", t
        print(f"-- sample (가짜 숫자)\nTITLE: {t}\nPRIORITY: {p} TAGS: {tg}\n{msg}\n")
    for tail in ("지분율 조회 실패 | 이번 실행 판정 못 함", "지분율 미게시 | 판정 하루 이상 밀림"):
        t, msg = ops_msg(cfg, stamp, tail, "x")
        assert route_topic("cwally-port", t) == "cwally-ops", t
    print("SELFTEST OK: no network, no publish, no state")
    return 0


def main() -> int:
    args = sys.argv[1:]
    mode = (args[0] if args and not args[0].startswith("--") else "auto").lower()
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    if mode == "selftest":
        return selftest(cfg)
    if mode not in {"auto", "dry"}:
        print(f"unknown mode {mode}")
        return 2
    now = datetime.now(KST)
    if "--as-of" in args:
        if mode != "dry":
            print("--as-of 는 dry 에서만")
            return 2
        now = datetime.strptime(args[args.index("--as-of") + 1], "%Y-%m-%d %H:%M").replace(tzinfo=KST)
    try:
        return run(mode, now, cfg)
    except Exception as exc:  # noqa: BLE001  스크립트 오류 -> cwally-ops
        print(traceback.format_exc(), file=sys.stderr)
        if mode == "dry":
            return 1
        try:
            stamp = now.strftime("%Y-%m-%d %H:%M KST")
            t, m = ops_msg(cfg, stamp, "스크립트 오류 | 이번 실행 판정 못 함",
                           f"{type(exc).__name__}: {str(exc)[:200]}. state 는 바꾸지 않음.")
            publish(resolve_topic("ops"), t, m, 3, ["warning"], resolve_server(cfg.get("ntfy_server")), config_name="lgesfor")
        except Exception:  # noqa: BLE001
            return 1
        return 0


if __name__ == "__main__":
    sys.exit(main())
