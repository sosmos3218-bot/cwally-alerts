#!/usr/bin/env python3
"""[KAL] 대한항공 003490 관심 종목 알림 (2026-11-03, 사용자 분석 붙여 넣기, 판단 위임).

평일 15:40 KST (0090b0 슬롯에 부착). 모두 상태가 바뀔 때만 1번, 같은 실행에서 걸린 것은 한 건으로 묶음.
  price : 네이버 수정주가 일봉 확정 종가. config price_lines (아래 선 2개, 위 선 2개), 선마다 rearm 으로 재무장.
  fuel  : 제트유 대용 HO=F (Yahoo). 설치 때 시드한 기준 대비 +10% 이상 p3(+5% 아래에서 재무장),
          -15% 이하 p3 유가 안정 신호(-10% 위에서 재무장).
  events: 아시아나 합병 공시(2026-12-01~2027-01-31, 못 찾으면 2027-01-05 안내),
          4분기 잠정실적 전날 안내(2027-01-14) + 2027-01-04~02-28 영업(잠정)실적 공시 1번.
첫 실행은 조용히 시드(가격 선 무장 상태, HO=F 기준, 지금까지의 공시 id). 데이터 조회 실패는 하루 1번 cwally-ops.
usage: kal_monitor.py <auto|dry> | selftest | confirm
"""
from __future__ import annotations

import json
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
CFG_PATH = ROOT / "configs" / "kal.json"
STATE_PATH = ROOT / "state" / "kal.json"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
TAG = "[KAL] 003490"
BAD_WORDS = ("분할매수", "분할 매수", "매수 기회", "매수 구간", "저가 매수", "사세요", "진입 검토", "담을", "비중 확대", "지금 매수",
             "매수 검토", "매수하세요", "사 모으")


def won(v: float) -> str:
    return f"{v:,.0f}"


def get(url: str) -> bytes:
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as resp:
        return resp.read()


def price_rows(code: str) -> list[tuple[str, float]]:
    raw = get(f"https://fchart.stock.naver.com/sise.nhn?symbol={code}&timeframe=day&count=260&requestType=0").decode("euc-kr", "ignore")
    return sorted((f"{d[:4]}-{d[4:6]}-{d[6:]}", float(c))
                  for d, c in re.findall(r'item data="(\d{8})\|[^|]*\|[^|]*\|[^|]*\|([\d.]+)\|', raw))


def confirmed(rows: list[tuple[str, float]], now: datetime) -> list[tuple[str, float]]:
    today = now.strftime("%Y-%m-%d")
    return [r for r in rows if r[0] < today or (r[0] == today and (now.hour, now.minute) >= (15, 31))]


def fuel_price(symbol: str) -> float:
    d = json.loads(get(f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=5d&interval=1d"))["chart"]["result"][0]
    p = d["meta"].get("regularMarketPrice")
    if not p:
        closes = [c for c in d["indicators"]["quote"][0]["close"] if c]
        p = closes[-1] if closes else None
    if not p:
        raise ValueError("HO=F 시세 없음")
    return float(p)


def load_notices(code: str) -> list[dict]:
    import naver_api
    return naver_api.notices(code, 30)


# ---------- 판정 (순수 함수) ----------
def p_hit(ln: dict, p: float) -> bool:
    return p <= ln["level"] if ln["kind"] == "down" else p >= ln["level"]


def p_rearm(ln: dict, p: float) -> bool:
    return p >= ln["rearm"] if ln["kind"] == "down" else p < ln["rearm"]


def judge_price(lines: list[dict], armed: dict, p: float) -> list[dict]:
    fired = []
    for ln in lines:
        k = ln["key"]
        if k not in armed:  # new line seeds silently
            armed[k] = not p_hit(ln, p)
            continue
        if armed[k] and p_hit(ln, p):
            armed[k] = False
            fired.append(ln)
        elif not armed[k] and p_rearm(ln, p):
            armed[k] = True
    return fired


def judge_fuel(fc: dict, fs: dict, price: float) -> tuple[str | None, float]:
    """fs: {baseline, up_armed, down_armed}. Returns (fired 'up'|'down'|None, pct vs baseline)."""
    base = fc.get("baseline") or fs.get("baseline")
    if not base:
        fs.update({"baseline": price, "up_armed": True, "down_armed": True})
        return None, 0.0
    pct = (price / base - 1) * 100
    fired = None
    if fs.get("up_armed", True):
        if pct >= fc["up_pct"]:
            fs["up_armed"], fired = False, "up"
    elif pct < fc["up_rearm_pct"]:
        fs["up_armed"] = True
    if fs.get("down_armed", True):
        if pct <= fc["down_pct"]:
            fs["down_armed"], fired = False, "down"
    elif pct > fc["down_rearm_pct"]:
        fs["down_armed"] = True
    return fired, pct


def is_asiana(n: dict) -> bool:
    t = n.get("title") or ""
    return "합병등종료보고서" in t or ("합병" in t and "아시아나" in (t + " " + (n.get("text") or "")))


def is_q4(n: dict) -> bool:
    return "영업(잠정)실적" in (n.get("title") or "")


def judge_events(ec: dict, es: dict, notices: list[dict], today: str) -> list[tuple[str, dict | None]]:
    """es: {seen: [ids], asiana_found, asiana_reminded, q4_found, q4_reminded}. First call (no 'seen') seeds silently."""
    out: list[tuple[str, dict | None]] = []
    if "seen" not in es:
        es["seen"] = [n["id"] for n in notices]
        return out
    seen = set(es["seen"])
    a, q = ec["asiana"], ec["q4"]
    for n in notices:
        if n["id"] in seen:
            continue
        seen.add(n["id"])
        if is_asiana(n) and a["start"] <= today <= a["end"] and not es.get("asiana_found"):
            es["asiana_found"] = today
            out.append(("asiana", n))
        elif is_q4(n) and q["watch_start"] <= today <= q["watch_end"] and not es.get("q4_found"):
            es["q4_found"] = today
            out.append(("q4", n))
    es["seen"] = sorted(seen)[-200:]
    if a["reminder"] <= today <= a["end"] and not es.get("asiana_found") and not es.get("asiana_reminded"):
        es["asiana_reminded"] = today
        out.append(("asiana_rem", None))
    if q["reminder"] <= today <= q["watch_end"] and not es.get("q4_found") and not es.get("q4_reminded"):
        es["q4_reminded"] = today
        out.append(("q4_rem", None))
    return out


# ---------- 문구 ----------
def item_text(kind: str, obj, cfg: dict, p: float | None, fuel: tuple[float, float] | None) -> tuple[str, str, int]:
    """(title tail, 지금 할 일 문장, priority)"""
    fc = cfg["fuel"]
    if kind == "price":
        k = obj["key"]
        tail = f"종가 {won(p)} | {obj['label']}"
        if k == "d26500":
            return tail + " | 신규 매수 보류, 관찰", "반등이 꺼졌는지 관찰만 하세요. 제트유 대용(HO=F)과 4분기 실적 전망이 내려갔는지 확인.", obj["priority"]
        if k == "d24000":
            return tail + " | 신규 매수 보류, 근거 재점검", "투자 논리 약화 경고입니다. 유가 급등 또는 실적 하향이 반영됐는지, 합병 일정에 문제가 생겼는지 확인하세요.", obj["priority"]
        if k == "u32500":
            return tail + " | 추격 매수 보류, 근거 재확인", "추격 매수 보류, 근거 재확인. 합병 시너지 근거(운임, 노선 정리)와 유가 흐름이 같이 좋아졌는지 보세요.", obj["priority"]
        return tail + " | 밸류 재점검", "컨센서스 목표가에 왔습니다. 목표가가 올라가는지, PBR 과 2027년 이익 전망 대비 비싼지 다시 따져 보세요.", obj["priority"]
    if kind == "fuel":
        price, pct = fuel
        if obj == "up":
            return (f"제트유 대용 HO=F {price:.3f}달러 ({pct:+.1f}%) | 유가 부담 확대 | 신규 매수 보류",
                    "연료비 부담이 커졌습니다. 4분기, 2027년 영업이익 전망 하향 여부를 확인하세요.", fc["priority"])
        return (f"제트유 대용 HO=F {price:.3f}달러 ({pct:+.1f}%) | 유가 안정 신호 | 근거 재확인",
                "연료비 부담이 줄어드는 신호입니다. 하루 움직임인지 며칠 이어지는지, 실적 전망이 올라가는지 확인하세요.", fc["priority"])
    pri = cfg["events"]["asiana" if kind.startswith("asiana") else "q4"]["priority"]
    if kind == "asiana":
        return ("아시아나 합병 관련 공시 | 완료 여부 원문 확인",
                f"공시 '{obj['title']}' ({obj['datetime'][:10]}) 원문에서 합병 완료(합병기일, 종료 보고) 여부를 확인하세요.", pri)
    if kind == "asiana_rem":
        return ("아시아나 합병 완료 공시 아직 없음 | 일정 지연 여부 확인",
                "12월 완료 예정이던 합병 공시가 아직 안 보입니다. 지연 사유(규제, 운임 제한)가 있는지 뉴스와 공시를 확인하세요.", pri)
    if kind == "q4":
        return ("4분기 잠정실적 공시 | 영업이익, 연료비 확인",
                f"공시 '{obj['title']}' ({obj['datetime'][:10]}) 에서 영업이익, 연료비, 일회성 인건비를 확인하세요.", pri)
    return ("4분기 잠정실적 발표 예상 전날 | 확인 준비",
            "최근 패턴상 내일(1월 중순) 4분기 잠정실적이 나올 수 있습니다. 컨센서스와 연료비 가정을 미리 보세요.", pri)


def message(cfg: dict, items: list[tuple[str, object]], stamp: str, close: tuple[str, float] | None, ma: float | None,
            fuel: tuple[float, float] | None, base: float | None) -> tuple[str, str, int]:
    texts = [item_text(k, o, cfg, close[1] if close else None, fuel) for k, o in items]
    order = sorted(range(len(texts)), key=lambda i: -texts[i][2])
    top = texts[order[0]]
    title = f"{TAG} {top[0]}" + (f" 외 {len(texts) - 1}건" if len(texts) > 1 else "")
    st = []
    if close:
        st.append(f"{close[0]} 확정 종가 {won(close[1])}원")
    if ma:
        st.append(f"200일선 {won(ma)}원")
    if fuel and base:
        st.append(f"HO=F {fuel[0]:.3f}달러(기준 {base:.3f}, {fuel[1]:+.1f}%)")
    down = any(k in ("price",) and o["kind"] == "down" for k, o in items) or any(k == "fuel" and o == "up" for k, o in items)
    body = "\n".join([
        "이게 뭐 알림: 대한항공(003490) 관심 종목 알림입니다. 종가 선, 제트유 대용 지표(HO=F), 합병과 실적 일정이 바뀔 때만 옵니다. 미보유 기준이고 매매 지시가 아닙니다.",
        f"{stamp} | 003490 | 대한항공",
        "지금 결과/상태: " + ", ".join(st) + ". 걸린 것: " + "; ".join(texts[i][0] for i in order) + ".",
        "지금 할 일: " + " ".join(texts[i][1] for i in order),
        "나중에: 같은 선은 다시 무장된 뒤에만 다시 옵니다(26,500 은 27,300 위, 24,000 은 24,700 위 회복, 32,500 은 31,500 아래, 38,500 은 37,000 아래). "
        f"제트유 대용은 +5%, -10% 안으로 돌아와야 다시 무장. 종료 {cfg['ends_kst'][:10]}.",
        "익절/줄일 때: 미보유라 해당 없음. 나중에 사게 되면 평균가 기준 선을 따로 정하세요." if not down else
        "익절/줄일 때: 미보유라 해당 없음. 보유하게 되면 경고 때는 비중부터 점검하세요.",
        "하지 말 것: 추격 매수, 신용, 미수, 경고를 반등 기회로 읽기, 하루 움직임만 보고 결정하기.",
    ])
    return title, body, max(t[2] for t in texts)


# ---------- 실행 ----------
def ops(stamp: str, what: str, err: str) -> tuple[str, str, int]:
    body = "\n".join([
        "이게 뭐 알림: 대한항공(003490) 알림의 데이터 조회 실패 안내. 매매 신호 아님.",
        f"{stamp} | 003490 | 대한항공",
        f"지금 결과/상태: {what} 확인 불가 ({err[:150]}).",
        "지금 할 일: 필요하면 네이버 증권이나 Yahoo 에서 직접 보세요.",
        "나중에: 다음 평일 15:40 에 다시 봅니다.",
        "익절/줄일 때: 이 안내로 판단을 바꾸지 않기.",
        "하지 말 것: 조회 실패를 조건 미충족으로 읽기.",
    ])
    return f"{TAG} {what} 조회 실패 | 판정 보류", body, 3


def run(mode: str, now: datetime, cfg: dict) -> int:
    dry = mode == "dry"
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    today = now.strftime("%Y-%m-%d")
    st = json.loads(STATE_PATH.read_text(encoding="utf-8")) if STATE_PATH.exists() else {}
    if mode == "auto" and now.weekday() >= 5:
        print("weekend; silent")
        return 0
    items: list[tuple[str, object]] = []
    fails: list[tuple[str, str]] = []
    close = ma = fuel = None
    try:
        rows = confirmed(price_rows(cfg["code"]), now)
        if not rows:
            raise ValueError("확정 종가 없음")
        close = rows[-1]
        cl = [c for _, c in rows]
        ma = sum(cl[-200:]) / 200 if len(cl) >= 200 else None
        armed = st.setdefault("armed", {})
        if close[0] != st.get("last_date"):
            items += [("price", ln) for ln in judge_price(cfg["price_lines"], armed, close[1])]
            st["last_date"] = close[0]
        st["last_close"], st["ma200"] = list(close), round(ma, 1) if ma else None
    except Exception as exc:  # noqa: BLE001
        fails.append(("종가", str(exc)))
    try:
        fp = fuel_price(cfg["fuel"]["symbol"])
        fs = st.setdefault("fuel", {})
        seeded = not (cfg["fuel"].get("baseline") or fs.get("baseline"))
        f, pct = judge_fuel(cfg["fuel"], fs, fp)
        if seeded:
            fs["baseline_at"] = stamp
        fuel = (fp, pct)
        fs["last"] = [stamp, fp, round(pct, 2)]
        if f:
            items.append(("fuel", f))
    except Exception as exc:  # noqa: BLE001
        fails.append(("HO=F", str(exc)))
    try:
        es = st.setdefault("events", {})
        items += judge_events(cfg["events"], es, load_notices(cfg["code"]), today)
    except Exception as exc:  # noqa: BLE001
        fails.append(("공시", str(exc)))
    print(f"close={close} ma200={round(ma) if ma else None} fuel={fuel} armed={st.get('armed')} fuel_state={ {k: v for k, v in (st.get('fuel') or {}).items() if k != 'last'} }")
    out = []
    if items:
        base = cfg["fuel"].get("baseline") or (st.get("fuel") or {}).get("baseline")
        out.append(message(cfg, items, stamp, close, ma, fuel, base))
    if fails and st.get("fail_day") != today:
        st["fail_day"] = today
        out.append(ops(stamp, ", ".join(w for w, _ in fails), "; ".join(e for _, e in fails)))
    st["updated_at"] = stamp
    for t, m, pri in out:
        print(t)
        print(m)
        if not dry:
            publish(resolve_topic("port", cfg.get("ntfy_topic")), t, m, pri, ["warning"] if pri >= 4 else ["airplane"],
                    resolve_server(cfg.get("ntfy_server")), config_name="kal")
            st["last_sent"] = {"at": stamp, "title": t}
    if not out:
        print("no alert")
    if dry:
        print("DRY RUN: no publish, state not written")
    else:
        STATE_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


def selftest(cfg: dict) -> int:
    fails = 0

    def check(label, got, want):
        nonlocal fails
        ok = got == want
        fails += not ok
        print(("ok  " if ok else "FAIL"), label, "" if ok else f"got {got!r} want {want!r}")
    L = cfg["price_lines"]
    armed: dict = {}
    check("seed at 28,700 silent", judge_price(L, armed, 28700), [])
    check("seed all armed", armed, {"d26500": True, "d24000": True, "u32500": True, "u38500": True})
    walk = [29850, 26500, 26000, 27299, 26400, 27300, 26450, 23900, 24699, 23000, 24700, 32500, 33000, 31500, 31499, 32600, 38500, 37100, 36999, 38600]
    got = [[x["key"] for x in judge_price(L, armed, p)] for p in walk]
    check("price walk", got, [[], ["d26500"], [], [], [], [], ["d26500"], ["d24000"], [], [], [], ["u32500"], [], [], [], ["u32500"], ["u38500"], [], [], ["u38500"]])
    fc = cfg["fuel"]
    fs: dict = {}
    check("fuel seed", (judge_fuel(fc, fs, 4.424)[0], fs["baseline"]), (None, 4.424))
    seq = [4.6, 4.87, 5.2, 4.66, 4.64, 4.87, 3.77, 3.76, 3.70, 3.98, 3.99, 3.75]
    check("fuel walk", [judge_fuel(fc, fs, x)[0] for x in seq], [None, "up", None, None, None, "up", None, "down", None, None, None, "down"])
    ec = cfg["events"]
    es: dict = {}
    old = [{"id": "1", "title": "(주)대한항공 (정정)회사합병 결정", "datetime": "2026-06-26T06:50:13", "text": "아시아나항공"}]
    check("events seed silent", judge_events(ec, es, old, "2026-11-03"), [])
    mrg = {"id": "2", "title": "(주)대한항공 합병등종료보고서", "datetime": "2026-12-12T06:50:00", "text": ""}
    check("merger before window ignored", [k for k, _ in judge_events(ec, dict(es), [mrg], "2026-11-20")], [])
    e2 = json.loads(json.dumps(es))
    check("merger in window", [k for k, _ in judge_events(ec, e2, old + [mrg], "2026-12-12")], ["asiana"])
    check("no repeat", [k for k, _ in judge_events(ec, e2, old + [mrg], "2026-12-14")], [])
    check("found -> no 1/5 reminder", [k for k, _ in judge_events(ec, e2, old + [mrg], "2027-01-05")], [])
    e3 = json.loads(json.dumps(es))
    check("1/5 reminder when nothing", [k for k, _ in judge_events(ec, e3, old, "2027-01-05")], ["asiana_rem"])
    check("reminder once", [k for k, _ in judge_events(ec, e3, old, "2027-01-06")], [])
    check("q4 reminder 1/14", [k for k, _ in judge_events(ec, e3, old, "2027-01-14")], ["q4_rem"])
    q4n = {"id": "9", "title": "(주)대한항공 영업(잠정)실적(공정공시)", "datetime": "2027-01-15T06:51:00", "text": ""}
    check("q4 notice after reminder", [k for k, _ in judge_events(ec, e3, old + [q4n], "2027-01-15")], ["q4"])
    e4 = json.loads(json.dumps(es))
    q4k = [k for k, _ in judge_events(ec, e4, old + [q4n], "2027-01-12")] + [k for k, _ in judge_events(ec, e4, old + [q4n], "2027-01-14")]
    check("q4 notice before reminder -> no q4 reminder", [k for k in q4k if k.startswith("q4")], ["q4"])
    check("asiana text match", is_asiana({"title": "(주)대한항공 회사합병 결정", "text": "합병 상대 (주)아시아나항공"}), True)
    check("unrelated 합병 not asiana", is_asiana({"title": "(주)대한항공 회사합병 결정(종속회사의 주요경영사항)", "text": "진에어"}), False)
    stamp = "2026-12-01 15:40 KST"
    t1, b1, p1 = message(cfg, [("price", L[1]), ("fuel", "up"), ("price", L[0])], stamp, ("2026-12-01", 23800.0), 26240.0, (4.9, 10.8), 4.424)
    check("combo title p4 first", (t1, p1), ("[KAL] 003490 종가 23,800 | 24,000 이탈, 투자 논리 약화 경고 | 신규 매수 보류, 근거 재점검 외 2건", 4))
    t2, b2, _ = message(cfg, [("price", L[2])], stamp, ("2026-12-01", 32600.0), 26500.0, None, None)
    check("52w title", t2, "[KAL] 003490 종가 32,600 | 52주 고점 32,500 돌파 | 추격 매수 보류, 근거 재확인")
    t3, b3, _ = message(cfg, [("fuel", "down")], stamp, None, None, (3.70, -16.4), 4.424)
    check("fuel down title", t3, "[KAL] 003490 제트유 대용 HO=F 3.700달러 (-16.4%) | 유가 안정 신호 | 근거 재확인")
    t4, b4, _ = message(cfg, [("asiana_rem", None)], stamp, None, None, None, None)
    t5, b5, _ = message(cfg, [("q4_rem", None)], stamp, None, None, None, None)
    t6, b6, _ = message(cfg, [("price", L[3])], stamp, ("2026-12-01", 38600.0), 30000.0, None, None)
    allt = "".join([t1, b1, t2, b2, t3, b3, t4, b4, t5, b5, t6, b6])
    check("first line 이게 뭐 알림", all(b.split("\n")[0].startswith("이게 뭐 알림:") for b in (b1, b2, b3, b4, b5, b6)), True)
    check("six parts", [x.split(":")[0] for x in b1.split("\n")[2:]], ["지금 결과/상태", "지금 할 일", "나중에", "익절/줄일 때", "하지 말 것"])
    check("ticker in titles", all("003490" in t for t in (t1, t2, t3, t4, t5, t6)), True)
    check("no dot/dash", any(ch in allt for ch in "\u00b7\u2014"), False)
    check("no buy wording", [w for w in BAD_WORDS if w in allt], [])
    check("confirmed close before 15:31", confirmed([("2026-11-02", 1.0), ("2026-11-03", 2.0)], datetime(2026, 11, 3, 11, 0, tzinfo=KST))[-1][0], "2026-11-02")
    print("selftest", "PASS" if not fails else f"FAIL {fails}")
    return 1 if fails else 0


def confirm(cfg: dict) -> int:
    st = json.loads(STATE_PATH.read_text(encoding="utf-8")) if STATE_PATH.exists() else {}
    lc = st.get("last_close") or ["기록 없음", 0]
    fs = st.get("fuel") or {}
    body = "\n".join([
        "이게 뭐 알림: 대한항공(003490) 관심 종목 알림 설치 확인입니다. 매매 신호 아님.",
        f"{datetime.now(KST).strftime('%Y-%m-%d %H:%M KST')} | 003490 | 대한항공",
        f"지금 결과/상태: 확정 종가 {lc[0]} {won(lc[1])}원, 200일선 {won(st['ma200']) if st.get('ma200') else '계산 전'}원. "
        f"제트유 대용 HO=F 기준 {fs.get('baseline', 0):.3f}달러. 모든 선 무장, 지금은 발송 대상 없음.",
        "지금 할 일: 없음. 평일 15:40 KST 에 종가 26,500 재이탈 p3, 24,000 이탈 p4, 52주 고점 32,500 돌파 p3, 목표가 38,500 도달 p3, "
        "HO=F 기준 +10% p3, -15% p3 를 상태가 바뀔 때만 봅니다.",
        "나중에: 아시아나 합병 공시 2026-12-01~2027-01-31(없으면 1/5 안내), 4분기 잠정실적 전날 2027-01-14 안내. 종료 2027-10-31. 우선주 003495 는 알림 없음.",
        "익절/줄일 때: 해당 없음.",
        "하지 말 것: 이 설치 확인을 매수/매도 신호로 읽기.",
    ])
    title = f"{TAG} 대한항공 설치 확인 | 매매 신호 아님"
    print(title)
    print(body)
    publish(resolve_topic("port", cfg.get("ntfy_topic")), title, body, 3, ["white_check_mark"], resolve_server(cfg.get("ntfy_server")),
            config_name="kal")
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
        print(f"kal ended {ends} KST; silent")
        return 0
    return run(mode, now, cfg)


if __name__ == "__main__":
    sys.exit(main())
