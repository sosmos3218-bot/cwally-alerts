#!/usr/bin/env python3
"""[BOK] 한국은행 금통위 기준금리 결정이 3.25% 이상이면 p4 1번. 기존 평일 19:00 k203780 슬롯.
결정일(configs/krrate.json mpc.schedule + 한은 일정 페이지 올해, 내년)마다 1번 판정:
  새 기준금리 >= 3.25% 이고 아직 도달 알림 전 -> p4 '[BOK] KOSPI 기준금리 X.XX% 인상, 3.25% 도달'
  그 밖(동결, 인하, 3.25% 미만, 도달 뒤 회의) -> 결과 p3 1번 (회의가 열렸다는 확인용)
결정 판정은 scripts/krrate_monitor.mpc_decision(한은 보도자료, 기준금리 표, 회의 페이지, 기사) 재사용, ECOS 722Y001 교차확인.
결정일에 출처가 판정 못 하면 cwally-ops 하루 1번, retry_days 동안 다음 슬롯에서 다시 시도.
첫 실행은 지난 회의를 처리됨으로 기록만(state/bok.json). ends_kst(2027-03-31 19:00) 뒤 조용히 종료.
modes: auto | dry(발송 없음, state 안 씀; --as-of "YYYY-MM-DD HH:MM", --mpc-date YYYY-MM-DD) | install | selftest
"""
from __future__ import annotations

import json
import re
import sys
import traceback
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from kr_calendar import KST  # noqa: E402
from ntfy_util import publish, resolve_server, resolve_topic, route_topic  # noqa: E402
import krrate_monitor as kr  # noqa: E402  (읽기 전용: mpc_schedule, mpc_decision, ecos_base_rate)

TAG = "[BOK]"
NAME = "bok"
CFG_PATH = ROOT / "configs" / f"{NAME}.json"
KR_CFG_PATH = ROOT / "configs" / "krrate.json"
STATE_PATH = ROOT / "state" / f"{NAME}.json"
DEC_KO = {"hold": "동결", "cut": "인하", "hike": "인상"}
READY_HM = (10, 30)  # 결정 발표(보통 10시 전후) 뒤에만 판정


def body(stamp: str, hdr: str, what: str, status: str, action: str, later: str, trim: str, avoid: str) -> str:
    return "\n".join([f"{stamp} | 한국은행 기준금리 | {hdr}", f"이게 뭐 알림: {what}", f"지금 결과/상태: {status}",
                      f"지금 할 일: {action}", f"나중에: {later}", f"익절/줄일 때: {trim}", f"하지 말 것: {avoid}"])


WHAT = "한국은행 금통위 기준금리 결정이 3.25% 이상인지 회의마다 1번 보는 알림. 매매 신호 아님."


def cross_line(d: str, dec: dict, ecos_fn=None) -> str:
    parts = []
    try:
        ser = (ecos_fn or kr.ecos_base_rate)(date.fromisoformat(d) - timedelta(days=10), date.fromisoformat(d))
        if d in ser:
            parts.append(f"ECOS {ser[d]:.2f}% {'일치' if abs(ser[d] - dec['rate']) < 0.001 else '불일치, 원문 확인'}")
        else:
            parts.append("ECOS 결정일 값 아직 없음")
    except Exception as exc:  # noqa: BLE001
        parts.append(f"ECOS 확인 불가({type(exc).__name__})")
    nv = dec.get("naver")
    if nv and nv[0] == d and nv[1] is not None:
        parts.append(f"네이버 {nv[1]:.2f}% {'일치' if abs(nv[1] - dec['rate']) < 0.001 else '불일치, 원문 확인'}")
    return "교차확인: " + ", ".join(parts)


def next_after(days: list[str], d: str) -> str:
    nxt = [x for x in days if x > d]
    return nxt[0] if nxt else "일정 미발표(한은 2027년 일정 발표 뒤 자동 반영)"


def message(cfg: dict, d: str, dec: dict, reached_before: bool, nxt: str, cross: str, stamp: str) -> tuple[str, str, int, bool]:
    """(title, body, priority, reached_now)."""
    th, base = cfg["threshold"], cfg["baseline"]
    rate, prev, dk = dec["rate"], dec.get("prev"), DEC_KO.get(dec["decision"], dec["decision"])
    mv = f"{prev:.2f}% -> {rate:.2f}%" if prev is not None else f"{rate:.2f}%"
    st = (f"{d[5:7]}/{d[8:]} 금통위 기준금리 {dk} ({mv}). 기준선 {th:.2f}%, 설치 때 기준 {base['rate']:.2f}%({base['since']} 이후). "
          f"출처: {dec.get('source', '확인 불가')}. {cross}." + (f" 소수의견: {dec['dissent']}." if dec.get("dissent") else ""))
    later = f"다음 회의 {nxt}. 2027-03-31 19:00 까지 회의마다 1번."
    if rate >= th - 1e-9 and not reached_before:
        head = f"{TAG} KOSPI 기준금리 {rate:.2f}% {dk}, {th:.2f}% 도달" if dec["decision"] != "hold" else f"{TAG} KOSPI 기준금리 {rate:.2f}% 동결, {th:.2f}% 이상"
        return (head, body(stamp, f"{th:.2f}% 도달", WHAT, st,
                           "금리 민감 보유분(채권 ETF, 리츠, 고배당, 성장주) 점검. 대출 금리 변동분 확인.",
                           later + " 도달 알림은 이번 1번만, 다음 회의부터는 결과만 p3.",
                           "금리 상단 근처 판단은 결정문과 다음 회의 신호 문구([KR-RATE] 요약)로 따로 확인.",
                           "이 알림만으로 매수, 매도 결정하기."), cfg["priority"]["reach"], True)
    tail = f"{th:.2f}% 이상 유지" if rate >= th - 1e-9 else f"{th:.2f}% 미도달"
    return (f"{TAG} KOSPI {d[5:7]}/{d[8:]} 금통위 기준금리 {rate:.2f}% {dk} | {tail}",
            body(stamp, "금통위 결과", WHAT, st, "확인만 하세요. 할 일 없음.", later,
                 "해당 없음.", "이 결과 알림을 매매 신호로 보기."), cfg["priority"]["result"], False)


def ops_msg(stamp: str, status: str) -> tuple[str, str]:
    return (f"{TAG} KOSPI 금통위 결정 확인 실패 | 판정 보류",
            body(stamp, "출처 읽기 실패", "기준금리 3.25% 알림의 데이터 읽기 실패 안내. 하루 1번. 매매 신호 아님.", status,
                 "한국은행 홈페이지 통화정책방향 보도자료에서 결정을 직접 보세요.",
                 "다음 19:00 슬롯에서 다시 시도합니다(결정일 뒤 3일까지).",
                 "이 안내로 비중을 바꾸지 않기.", "기사 추정으로 3.25% 도달을 단정하기."))


def install_message(stamp: str) -> tuple[str, str]:
    return (f"{TAG} KOSPI 설치 확인 | 매매 신호 아님",
            body(stamp, "설치 확인", "한국은행 기준금리 3.25% 도달 알림 설치 확인용 테스트 발송입니다. 매매 신호가 아닙니다.",
                 "현재 기준금리 3.00%(2026-08-27 인상, 10/22 동결, 한은 기준금리 표). 다음 금통위 2026-11-26. "
                 "결정일 19:00 KST 에 새 기준금리가 3.25% 이상이면 p4 1번, 아니면 결과 p3 1번.",
                 "알림이 도착했는지 확인만 하세요.",
                 "2027년 회의는 한은 일정 발표 뒤 자동 반영. 2027-03-31 19:00 에 끝납니다.",
                 "해당 없음.", "이 메시지를 매매 신호로 해석하기."))


def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(st: dict) -> None:
    STATE_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def check_text(t: str, m: str) -> None:
    assert not re.search("[\u00b7\u318d\u2014]", t + m), t


def due(days: list[str], done: set[str], today: str, now: datetime, cfg: dict) -> list[str]:
    out = []
    for d in days:
        if d in done or d < cfg["window_start"] or d > today:
            continue
        if d == today and (now.hour, now.minute) < READY_HM:
            continue
        if (date.fromisoformat(today) - date.fromisoformat(d)).days <= cfg["retry_days"]:
            out.append(d)
    return out


def run(mode: str, now: datetime, cfg: dict, kcfg: dict, pub=publish, sched_fn=None, dec_fn=None, ecos_fn=None,
        mpc_date: str | None = None) -> int:
    dry = mode == "dry"
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    topic, server = resolve_topic("port", cfg.get("ntfy_topic")), resolve_server(cfg.get("ntfy_server"))
    print(f"{stamp} {NAME} mode={mode}")
    if mode == "install":
        t, m = install_message(stamp)
        check_text(t, m)
        print(f"{t}\n{m}")
        pub(topic, t, m, 3, ["white_check_mark"], server, config_name=NAME)
        print("install confirmation sent (state untouched)")
        return 0
    today = now.date().isoformat()
    if now.strftime("%Y-%m-%d %H:%M") > cfg["ends_kst"]:
        print(f"{today} 감시 끝({cfg['ends_kst']}): 조용히 종료")
        return 0
    days, notes = (sched_fn or kr.mpc_schedule)(kcfg, now.year)
    days = [d for d in days if d <= cfg["window_end"]]
    for n in notes:
        print("schedule note:", n)
    print("meetings in window:", [d for d in days if d >= cfg["window_start"]])
    st = load_state()
    if "done" not in st and not mpc_date:
        st = {"done": [d for d in days if d <= today], "reached": False, "baseline": cfg["baseline"],
              "seeded_at": stamp, "fail_day": {}}
        print(f"first run: state only, no alert: {st}")
        if not dry:
            save_state(st)
        return 0
    done = set(st.get("done", []))
    todo = [mpc_date] if mpc_date else due(days, done, today, now, cfg)
    if not todo:
        print("오늘 판정할 회의 없음")
    out, ops = [], None
    reached = bool(st.get("reached")) and not mpc_date
    for d in todo:
        try:
            dec = (dec_fn or (lambda x: kr.mpc_decision(kcfg, x)))(d)
        except Exception as exc:  # noqa: BLE001
            dec = {"status": "unknown", "why": f"{type(exc).__name__}: {str(exc)[:120]}"}
        print(f"{d} decision: {dec}")
        if dec.get("status") == "ok" and dec.get("rate") is not None:
            t, m, pri, now_reached = message(cfg, d, dec, reached, next_after(days, d), cross_line(d, dec, ecos_fn), stamp)
            reached = reached or now_reached
            if now_reached or cfg.get("notify_result_below", True):
                out.append((t, m, pri))
            done.add(d)
            st["last"] = {"date": d, "decision": dec["decision"], "rate": dec["rate"]}
        else:
            last_try = (date.fromisoformat(today) - date.fromisoformat(d)).days >= cfg["retry_days"]
            if st.get("fail_day", {}).get(d) != today:
                ops = ops_msg(stamp, f"{d} 금통위 결정을 판정하지 못함: {dec.get('why', dec.get('status'))}."
                              + (" 마지막 재시도라 이 회의는 넘김." if last_try else " state 는 그대로."))
                st.setdefault("fail_day", {})[d] = today
            if last_try:
                done.add(d)
    for t, m, pri in out:
        check_text(t, m)
        print(f"TITLE: {t}\nPRIORITY: {pri} TOPIC: {route_topic(topic, t)}\n{m}\n")
    if ops:
        check_text(*ops)
        print(f"OPS TITLE: {ops[0]} -> ops\n{ops[1]}\n")
    if dry or mpc_date:
        print("DRY RUN: no publish, state not written")
        return 0
    for t, m, pri in out:
        pub(topic, t, m, pri, ["bank"], server, config_name=NAME)
    if ops:
        try:
            pub(resolve_topic("ops"), ops[0], ops[1], 3, ["warning"], server, config_name=NAME)
        except Exception as exc:  # noqa: BLE001
            print(f"ops notice failed: {exc}")
    st["done"], st["reached"], st["updated_at"] = sorted(done), reached, stamp
    save_state(st)
    return 0


def selftest() -> int:
    global STATE_PATH
    import tempfile
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    sched = lambda k, y: (["2026-10-22", "2026-11-26", "2027-01-15"], [])  # noqa: E731
    ecos = lambda a, b: {}  # noqa: E731
    sent = []
    pub = lambda *a, **k: sent.append(a)  # noqa: E731
    with tempfile.TemporaryDirectory() as td:
        STATE_PATH = Path(td) / "bok.json"
        n = lambda s: datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=KST)  # noqa: E731
        run("auto", n("2026-10-30 19:00"), cfg, {}, pub, sched, None, ecos)
        assert load_state()["done"] == ["2026-10-22"] and not sent
        hike = lambda d: {"status": "ok", "decision": "hike", "rate": 3.25, "prev": 3.0, "source": "T", "naver": (d, 3.25)}  # noqa: E731
        run("auto", n("2026-11-26 09:00"), cfg, {}, pub, sched, hike, ecos)
        assert not sent
        fail = lambda d: {"status": "not_yet", "why": "x"}  # noqa: E731
        run("auto", n("2026-11-26 19:00"), cfg, {}, pub, sched, fail, ecos)
        assert len(sent) == 1 and sent[0][0] != "cwally-port" or "실패" in sent[0][1], sent
        sent.clear()
        run("auto", n("2026-11-27 19:00"), cfg, {}, pub, sched, hike, ecos)
        assert len(sent) == 1 and sent[0][1] == "[BOK] KOSPI 기준금리 3.25% 인상, 3.25% 도달" and sent[0][3] == 4, sent
        sent.clear()
        run("auto", n("2026-11-30 19:00"), cfg, {}, pub, sched, hike, ecos)
        assert not sent
        hold = lambda d: {"status": "ok", "decision": "hold", "rate": 3.25, "prev": 3.25, "source": "T"}  # noqa: E731
        run("auto", n("2027-01-15 19:00"), cfg, {}, pub, sched, hold, ecos)
        assert len(sent) == 1 and "3.25% 이상 유지" in sent[0][1] and sent[0][3] == 3, sent
    c = cfg
    t, m, p, r = message(c, "2026-11-26", {"decision": "hold", "rate": 3.0, "prev": 3.0, "source": "T"}, False, "x", "교차확인: -", "T")
    assert p == 3 and not r and "미도달" in t and "동결" in t, t
    t, m, p, r = message(c, "2026-11-26", {"decision": "cut", "rate": 2.75, "prev": 3.0, "source": "T"}, False, "x", "교차확인: -", "T")
    assert p == 3 and "인하" in t
    for tt, mm in (ops_msg("T", "x"), install_message("T")):
        check_text(tt, mm)
    print("selftest ok")
    return 0


def main() -> int:
    args = sys.argv[1:]
    mode = (args[0] if args and not args[0].startswith("--") else "auto").lower()
    if mode == "selftest":
        return selftest()
    if mode not in {"auto", "dry", "install"}:
        print(f"usage: {NAME}_monitor.py <auto|dry|install|selftest> [--as-of 'YYYY-MM-DD HH:MM'] [--mpc-date YYYY-MM-DD]")
        return 2
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    kcfg = json.loads(KR_CFG_PATH.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    mpc_date = None
    if ("--as-of" in args or "--mpc-date" in args) and mode != "dry":
        print("--as-of, --mpc-date 는 dry 에서만")
        return 2
    if "--as-of" in args:
        now = datetime.strptime(args[args.index("--as-of") + 1], "%Y-%m-%d %H:%M").replace(tzinfo=KST)
    if "--mpc-date" in args:
        mpc_date = args[args.index("--mpc-date") + 1]
    try:
        return run(mode, now, cfg, kcfg, mpc_date=mpc_date)
    except Exception as exc:  # noqa: BLE001
        print(traceback.format_exc(), file=sys.stderr)
        if mode != "auto":
            return 1
        try:
            t, m = ops_msg(now.strftime("%Y-%m-%d %H:%M KST"), f"스크립트 오류 {type(exc).__name__}: {str(exc)[:200]}. state 는 그대로.")
            publish(resolve_topic("ops"), t, m, 3, ["warning"], resolve_server(cfg.get("ntfy_server")), config_name=NAME)
        except Exception:  # noqa: BLE001
            return 1
        return 0


if __name__ == "__main__":
    sys.exit(main())
