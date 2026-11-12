#!/usr/bin/env python3
"""[LDOS] Leidos Holdings 관심 종목 종가 선 알림 (2026-11-03 사용자 분석 붙여 넣기, 미보유).

평일 09:00 KST (port-alerts morning 슬롯에 부착). Yahoo LDOS 일봉, 뉴욕 16:00 이후 끝난 날만(전일 미국 정규장 종가).
선(상태가 바뀔 때만 1번, 재무장 완충):
  up152            : 종가 >= 152    -> p3 컨센서스 목표가 도달, 추격 보류 (146 미만 재무장)
  dn127            : 종가 <= 127    -> p3 관심 가격 검토 (131 이상 재무장)
  rebound_giveback : 종가 <= 117.74 -> p3 반등분 전액 반납, 논리 재점검 (122.45 이상 재무장). 경고라 매수 문구 없음.
실적: 2027-02-01~03-15 창에서 SEC EDGAR 8-K Item 2.02 가 보이면 다음 09:00 에 p3 1번, 창 끝까지 없으면 '직접 확인' 1번.
같은 실행에서 걸린 것은 한 건. 첫 실행은 조용히 시드. 조회 실패는 3회 연속일 때 cwally-ops 하루 1번. ends_kst 뒤 조용히 종료.
usage: ldos_monitor.py <auto|dry> | selftest | confirm
"""
from __future__ import annotations

import json
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
NAME = "ldos"
TAG = "[LDOS]"
CFG_PATH = ROOT / "configs" / f"{NAME}.json"
STATE_PATH = ROOT / "state" / f"{NAME}.json"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
BUY_WORDS = ("매수 검토", "분할 매수", "매수 기회", "저가 매수", "사세요", "담을", "물타기 하", "비중 확대", "지금 매수", "관심 가격")
OPS = {"le": lambda v, x: v <= x, "lt": lambda v, x: v < x, "ge": lambda v, x: v >= x, "gt": lambda v, x: v > x}


def get(url: str, ua: str = UA) -> bytes:
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": ua, "Accept-Encoding": "identity"}), timeout=30) as r:
        return r.read()


def usd(v) -> str:
    return f"${v:,.2f}" if v is not None else "?"


def yahoo_rows(cfg: dict, now: datetime) -> list[tuple[str, float]]:
    d = json.loads(get(cfg["sources"]["price"]))["chart"]["result"][0]
    tz = ZoneInfo(d["meta"].get("exchangeTimezoneName") or "America/New_York")
    loc = now.astimezone(tz)
    hh, mm = (int(x) for x in cfg["close_hm"].split(":"))
    out = {}
    for t, c in zip(d.get("timestamp") or [], d["indicators"]["quote"][0].get("close") or []):
        if c is None:
            continue
        day = datetime.fromtimestamp(t, tz).date()
        if day < loc.date() or (day == loc.date() and (loc.hour, loc.minute) >= (hh, mm)):
            out[day.isoformat()] = float(c)
    return sorted(out.items())


def sec_202(cfg: dict) -> list[dict]:
    e = cfg["earnings"]
    d = json.loads(get(cfg["sources"]["sec"].format(cik=e["cik"]), e["user_agent"]))
    r = d["filings"]["recent"]
    out = []
    for i in range(len(r["form"])):
        if r["form"][i] in ("8-K", "8-K/A") and "2.02" in str(r["items"][i]).split(","):
            out.append({"date": r["filingDate"][i], "acc": r["accessionNumber"][i], "doc": r["primaryDocument"][i]})
    return out


def migrate_armed(armed: dict) -> dict:
    """Rename legacy dn130 key after the 127 watch-line change; no extra send."""
    if "dn130" in armed and "dn127" not in armed:
        armed["dn127"] = armed.pop("dn130")
    else:
        armed.pop("dn130", None)
    return armed


def judge(cfg: dict, armed: dict, p: float) -> list[dict]:
    fired = []
    for ln in cfg["lines"]:
        k = ln["key"]
        hit = OPS[ln["op"]](p, ln["level"])
        if k not in armed:
            armed[k] = not hit
        elif armed[k] and hit:
            armed[k] = False
            fired.append(ln)
        elif not armed[k] and OPS[ln["rearm_op"]](p, ln["rearm"]):
            armed[k] = True
    return fired


def line_text(ln: dict) -> tuple[str, str]:
    """(지금 할 일, 하지 말 것) per line."""
    k = ln["key"]
    if k == "up152":
        return ("컨센서스 평균 목표가(약 $152)까지의 업사이드를 다 썼다는 뜻. 추격 보류, 증권사 목표가 상향(가이던스 추가 상향, 대형 수주)이 따라오는지 기다리세요.",
                "목표가 도달을 추가 상승 신호로 읽기, 추격.")
    if k == "dn127":
        return ("관심 가격 검토(fwd PER 약 10배대). 확인: (1) 가이던스(EPS 12.50~12.70) 유지 여부, (2) Health 부문 추세, (3) 정부 예산, 셧다운 뉴스, "
                "(4) book-to-bill 1 이상. 자금: 미보유, 아직 정한 매수 계획(금액, 비중) 없음, 계획부터 정하기.",
                "계획 없이 한 번에 사기, 하루 하락을 바닥으로 읽기.")
    return ("10월 초(10/2 종가 $117.74)부터의 반등분을 모두 반납. 논리 재점검: 가이던스, 계약 타이밍, 정부 지출 지연, 부채, 성장 둔화 중 무엇이 바뀌었는지 확인.",
            "반납을 싸졌다는 신호로 읽기, 물타기, 신용.")


def message(cfg: dict, fired: list[dict], day: str, p: float, stamp: str) -> tuple[str, str, int]:
    warn = any(x["warn"] for x in fired)
    shown = [x for x in fired if x["warn"]] if warn else fired
    title = f"{TAG} LDOS 종가 {usd(p)} | " + " + ".join(x["title"] for x in shown)
    todo = " ".join(line_text(x)[0] for x in shown)
    dont = " ".join(line_text(x)[1] for x in shown)
    body = "\n".join([
        f"이게 뭐 알림: Leidos(LDOS) 관심 종목(미보유) 미국 정규장 종가가 미리 정한 선에 닿았다는 알림. {'경고.' if warn else '점검 시점.'} 매매 지시 아님.",
        f"{stamp} | LDOS | Leidos Holdings",
        f"지금 결과/상태: {day} 종가 {usd(p)}(Yahoo, 뉴욕 정규장). 걸린 선: {', '.join(x['title'] for x in shown)}. "
        f"참고 11/2 종가 $144.16, 컨센서스 목표가 약 $152, 2026 EPS 가이던스 12.50~12.70.",
        f"지금 할 일: {todo}",
        "나중에: 같은 선은 재무장 뒤에만 다시 옵니다($152 선은 $146 미만, $127 선은 $131 이상, $117.74 선은 $122.45 이상). "
        f"4분기 실적(2027-02 중순 추정)은 8-K 로 확인 뒤 다음 날 점검 알림. 종료 {cfg['ends_kst'][:10]}.",
        "익절/줄일 때: 미보유, 해당 없음.",
        f"하지 말 것: {dont}",
    ])
    return title, body, max(x["priority"] for x in fired)


def earn_message(cfg: dict, filing: dict | None, st: dict, stamp: str) -> tuple[str, str, int]:
    e = cfg["earnings"]
    lc = st.get("last_close") or ["?", None]
    if filing:
        acc = filing["acc"].replace("-", "")
        url = f"https://www.sec.gov/Archives/edgar/data/{int(e['cik'])}/{acc}/{filing['doc']}"
        head, state = f"{e['label']} 발표 확인, 점검", f"SEC 8-K Item 2.02 제출 {filing['date']} (미국 날짜): {url}"
    else:
        head, state = f"{e['label']} 8-K 미확인, 직접 확인", f"{e['window'][0]}~{e['window'][1]} 사이 SEC 8-K Item 2.02 를 찾지 못함(예상 {e['expected']})."
    checks = ", ".join(f"({i}) {x}" for i, x in enumerate(e["checklist"], 1))
    body = "\n".join([
        f"이게 뭐 알림: Leidos(LDOS) {e['label']} 다음 날 점검 알림(미보유 관심 종목). 매매 지시 아님.",
        f"{stamp} | LDOS | Leidos Holdings",
        f"지금 결과/상태: {state} 최근 종가 {lc[0]} {usd(lc[1])}.",
        f"지금 할 일: 실적 자료와 컨콜에서 확인: {checks}.",
        "나중에: 가격 선($152, $127, $117.74)은 그대로 상태가 바뀔 때만 옵니다. 결과로 관심 유지 여부와 선 조정을 정하세요.",
        "익절/줄일 때: 미보유, 해당 없음.",
        "하지 말 것: 헤드라인 EPS 하나로 판단, 실적 당일 변동 추격.",
    ])
    return f"{TAG} LDOS {head}", body, int(e["priority"])


def run(mode: str, now: datetime, cfg: dict, io: dict | None = None) -> tuple[list, dict]:
    io = io or {}
    dry = mode == "dry"
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    today = stamp[:10]
    st = io["state"] if "state" in io else (json.loads(STATE_PATH.read_text(encoding="utf-8")) if STATE_PATH.exists() else {})
    out, fails = [], []
    try:
        rows = io["rows"]() if "rows" in io else yahoo_rows(cfg, now)
        if not rows:
            raise ValueError("확정 종가 없음")
        day, p = rows[-1]
        armed = migrate_armed(st.setdefault("armed", {}))
        last = st.get("last_date")
        fired: list[dict] = []
        for d, c in rows:
            if (last is None and d == day) or (last is not None and d > last):
                for f in judge(cfg, armed, c):
                    fired = [x for x in fired if x["key"] != f["key"]] + [f]
        if fired and last is not None:
            out.append(message(cfg, fired, day, p, stamp))
        st.update(last_date=day, last_close=[day, p])
        print(f"close {day} {usd(p)} armed={armed}")
    except Exception as exc:  # noqa: BLE001
        fails.append(("LDOS 종가", str(exc)[:150]))
    e = cfg["earnings"]
    es = st.setdefault("earn", {})
    if not es.get("sent") and e["window"][0] <= today:
        if today <= (datetime.fromisoformat(e["window"][1]) + timedelta(days=1)).date().isoformat():
            try:
                fl = [f for f in (io["sec"]() if "sec" in io else sec_202(cfg)) if e["window"][0] <= f["date"] <= e["window"][1]]
                if fl:
                    f = sorted(fl, key=lambda x: x["date"])[0]
                    out.append(earn_message(cfg, f, st, stamp))
                    es.update(sent=stamp, filing=f)
            except Exception as exc:  # noqa: BLE001
                fails.append(("SEC 8-K", str(exc)[:150]))
        else:
            out.append(earn_message(cfg, None, st, stamp))
            es.update(sent=stamp, filing=None)
    fs = st.setdefault("fail_streak", {})
    keys = {w for w, _ in fails}
    for k in list(fs):
        if k not in keys:
            fs.pop(k)
    for k in keys:
        fs[k] = fs.get(k, 0) + 1
    loud = [(w, m) for w, m in fails if fs[w] >= 3]
    if loud and st.get("fail_day") != today:
        st["fail_day"] = today
        body = "\n".join([
            "이게 뭐 알림: LDOS 알림의 데이터 조회 실패 안내(3회 연속). 매매 신호 아님.",
            f"{stamp} | LDOS | Leidos Holdings",
            f"지금 결과/상태: {', '.join(w for w, _ in loud)} 확인 불가 ({'; '.join(m for _, m in loud)}).",
            "지금 할 일: 필요하면 Yahoo, SEC EDGAR 에서 직접 보세요.",
            "나중에: 다음 평일 09:00 에 다시 봅니다.",
            "익절/줄일 때: 이 안내로 판단을 바꾸지 않기.",
            "하지 말 것: 조회 실패를 조건 미충족으로 읽기.",
        ])
        out.append((f"{TAG} LDOS {', '.join(w for w, _ in loud)} 조회 실패 | 판정 보류", body, 3))
    st["updated_at"] = stamp
    if "state" in io:
        return out, st
    for t, m, pri in out:
        print(t)
        print(m)
        if not dry:
            publish(resolve_topic("port", cfg.get("ntfy_topic")), t, m, pri, ["warning"] if pri >= 4 else ["chart_with_upwards_trend"],
                    resolve_server(cfg.get("ntfy_server")), config_name=NAME)
            st["last_sent"] = {"at": stamp, "title": t}
    if not out:
        print("no alert")
    if dry:
        print("DRY RUN: no publish, state not written")
    else:
        STATE_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return out, st


def selftest(cfg: dict) -> int:
    fails = 0

    def check(label, got, want):
        nonlocal fails
        ok = got == want
        fails += not ok
        print(("ok  " if ok else "FAIL"), label, "" if ok else f"got {got!r} want {want!r}")

    a: dict = {}
    check("seed 144.16 silent, all armed", (judge(cfg, a, 144.16), a), ([], {"up152": True, "dn127": True, "rebound_giveback": True}))
    legacy = {"up152": True, "dn130": True, "rebound_giveback": True}
    check("migrate dn130 -> dn127", migrate_armed(legacy), {"up152": True, "dn127": True, "rebound_giveback": True})
    a_old = migrate_armed({"up152": True, "dn130": True, "rebound_giveback": True})
    check("133.89 does not fire new 127 line", (judge(cfg, a_old, 133.89), a_old["dn127"]), ([], True))
    a2: dict = {}
    judge(cfg, a2, 153.0)
    check("seed above 152 -> up152 seeded as fired (disarmed)", a2["up152"], False)
    walk = [150, 152, 151, 146.5, 145.99, 152.1, 128, 127, 130.9, 131, 126.5, 117.74, 122.4, 117.0, 122.45, 117.7]
    a = {"up152": True, "dn127": True, "rebound_giveback": True}
    got = [[x["key"] for x in judge(cfg, a, p)] for p in walk]
    check("walk with buffers", got, [[], ["up152"], [], [], [], ["up152"], [], ["dn127"], [], [], ["dn127"], ["rebound_giveback"], [], [], [], ["rebound_giveback"]])
    L = {x["key"]: x for x in cfg["lines"]}
    t1, b1, p1 = message(cfg, [L["up152"]], "2026-11-10", 152.3, "s")
    t2, b2, p2 = message(cfg, [L["dn127"]], "2026-12-01", 126.0, "s")
    t3, b3, p3 = message(cfg, [L["dn127"], L["rebound_giveback"]], "2026-12-02", 117.0, "s")
    check("titles", (t1, t2, t3), ("[LDOS] LDOS 종가 $152.30 | 컨센서스 목표가 도달, 추격 보류",
                                   "[LDOS] LDOS 종가 $126.00 | 관심 가격 검토 (fwd PER 약 10배대)",
                                   "[LDOS] LDOS 종가 $117.00 | 반등분 전액 반납, 논리 재점검"))
    check("dn127 checklist + funding note", all(k in b2 for k in ("가이던스", "Health", "셧다운", "book-to-bill", "미보유", "매수 계획")), True)
    check("warning has no buy wording", [w for w in BUY_WORDS if w in t3 + b3], [])
    allt = t1 + b1 + t2 + b2 + t3 + b3
    check("no dot/dash", any(ch in allt for ch in "\u00b7\u2014"), False)
    check("six parts", [x.split(":")[0] for x in b1.split("\n")[2:]], ["지금 결과/상태", "지금 할 일", "나중에", "익절/줄일 때", "하지 말 것"])
    rows = [("2026-10-30", 142.98), ("2026-11-02", 144.16)]
    io = {"state": {}, "rows": lambda: rows, "sec": lambda: [{"date": "2026-11-03", "acc": "0001336920-26-000290", "doc": "x.htm"}]}
    out, st = run("dry", datetime(2026, 11, 3, 21, 0, tzinfo=KST), cfg, io)
    check("seed run silent", out, [])
    rows.append(("2026-11-03", 151.0))
    io["state"] = st
    out, st = run("dry", datetime(2026, 11, 4, 9, 0, tzinfo=KST), cfg, io)
    check("11/3 151 no fire", out, [])
    rows.append(("2026-11-04", 126.9))
    out, st = run("dry", datetime(2026, 11, 5, 9, 0, tzinfo=KST), cfg, io)
    check("drop to 126.9 fires dn127 once", [o[0] for o in out], ["[LDOS] LDOS 종가 $126.90 | 관심 가격 검토 (fwd PER 약 10배대)"])
    out, st = run("dry", datetime(2026, 11, 5, 9, 5, tzinfo=KST), cfg, io)
    check("re-run same day silent", out, [])
    io["sec"] = lambda: [{"date": "2027-02-16", "acc": "0001336920-27-000010", "doc": "ldos-20270216.htm"},
                         {"date": "2026-11-03", "acc": "0001336920-26-000290", "doc": "x.htm"}]
    out, st = run("dry", datetime(2027, 2, 15, 9, 0, tzinfo=KST), cfg, dict(io, sec=lambda: [{"date": "2026-11-03", "acc": "a", "doc": "b"}]))
    check("no earnings before 8-K in window", [o[0] for o in out if "실적" in o[0]], [])
    out, st = run("dry", datetime(2027, 2, 17, 9, 0, tzinfo=KST), cfg, io)
    check("earnings reminder on 8-K 2.02", [(o[0], o[2]) for o in out], [("[LDOS] LDOS 4분기 FY2026 실적 발표 확인, 점검", 3)])
    check("earnings body checklist", all(k in out[0][1] for k in ("12.50~12.70", "book-to-bill", "Health", "부채", "sec.gov/Archives")), True)
    out, st = run("dry", datetime(2027, 2, 18, 9, 0, tzinfo=KST), cfg, io)
    check("earnings once", out, [])
    io2 = dict(io, state={"last_date": "2027-03-15", "last_close": ["2027-03-15", 140.0], "armed": {}}, rows=lambda: [("2027-03-15", 140.0)], sec=lambda: [])
    out, s2 = run("dry", datetime(2027, 3, 16, 9, 0, tzinfo=KST), cfg, io2)
    check("window still open 3/16 (KST) no fallback", out, [])
    io2["state"] = s2
    out, s2 = run("dry", datetime(2027, 3, 17, 9, 0, tzinfo=KST), cfg, io2)
    check("fallback after window", [o[0] for o in out], ["[LDOS] LDOS 4분기 FY2026 실적 8-K 미확인, 직접 확인"])
    bad = dict(io, state={}, rows=lambda: (_ for _ in ()).throw(OSError("x")), sec=lambda: [])
    r = []
    for d in (2, 3, 4):
        out, bad["state"] = run("dry", datetime(2026, 11, d + 2, 9, 0, tzinfo=KST), cfg, bad)
        r.append(len(out))
    check("fetch fail ops only on 3rd run", r, [0, 0, 1])
    print("selftest", "PASS" if not fails else f"FAIL {fails}")
    return 1 if fails else 0


def confirm(cfg: dict) -> int:
    st = json.loads(STATE_PATH.read_text(encoding="utf-8")) if STATE_PATH.exists() else {}
    lc = st.get("last_close") or ["기록 없음", None]
    body = "\n".join([
        "이게 뭐 알림: Leidos(LDOS) 관심 종목 종가 선 알림 설치 확인입니다. 매매 신호 아님.",
        f"{datetime.now(KST).strftime('%Y-%m-%d %H:%M KST')} | LDOS | Leidos Holdings",
        f"지금 결과/상태: 최근 확정 종가 {lc[0]} {usd(lc[1])}. 모든 선 무장, 지금은 발송 대상 없음. 3분기 실적 반영 11/3 종가는 내일 09:00 첫 판정.",
        "지금 할 일: 없음. 평일 09:00 KST 에 전일 미국 종가로 $152 이상(목표가 도달, 추격 보류), $127 이하(관심 가격 점검), "
        "$117.74 이하(반등분 반납, 논리 재점검)를 상태가 바뀔 때만 봅니다.",
        f"나중에: 4분기 FY2026 실적(2027-02 중순 추정)은 SEC 8-K 로 확인 뒤 다음 날 점검 알림. 종료 {cfg['ends_kst'][:10]}.",
        "익절/줄일 때: 미보유, 해당 없음.",
        "하지 말 것: 이 설치 확인을 매매 신호로 읽기.",
    ])
    title = f"{TAG} LDOS Leidos 관심 종목 알림 설치 확인 | 매매 신호 아님"
    print(title)
    print(body)
    publish(resolve_topic("port", cfg.get("ntfy_topic")), title, body, 3, ["white_check_mark"], resolve_server(cfg.get("ntfy_server")),
            config_name=NAME)
    return 0


def main() -> int:
    args = [a.lower() for a in sys.argv[1:]]
    mode = args[0] if args else "auto"
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    if mode == "selftest":
        return selftest(cfg)
    if mode == "confirm":
        return confirm(cfg)
    if mode not in {"auto", "dry"}:
        print(__doc__)
        return 2
    now = datetime.now(KST)
    ends = cfg.get("ends_kst")
    if ends and now > datetime.strptime(ends, "%Y-%m-%d %H:%M").replace(tzinfo=KST):
        print(f"{NAME} ended {ends} KST; silent")
        return 0
    run(mode, now, cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
