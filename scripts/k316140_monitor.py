#!/usr/bin/env python3
"""[316140] 우리금융지주 보유 종목 알림 (2026-11-05 승인 pt74u, 보유 25주 평단 33,018원 반영 pt76u).

KRX 거래일 15:40 KST (0090b0 슬롯에 부착). Daum KRX 확정 종가(실패 시 Yahoo 316140.KS), 당일 행은 15:35 이후만. 네이버 안 씀.
선(상태가 바뀔 때만 1번, 재무장 완충):
  l30400 : 종가 <= 30,400 -> p3 1차 추가매수 점검 (31,300 이상 재무장)
  l28800 : 종가 <= 28,800 -> p3 2차 추가매수 점검 (29,700 이상)
  h33100 : 종가 >= 33,100 -> p3 평단(33,018원)과 60일선 회복, 추격 매수 보류, 비중 점검 (32,100 미만)
  w26414 : 종가 <= 26,414 -> p4 경고 평단 -20% 손실 점검 (27,200 이상)
  w25400 : 종가 <= 25,400 -> p4 경고 52주 저점 이탈 (26,200 이상). 경고가 걸리면 경고만 보내고 매수 문구 없음.
본문에 평단 대비 평가손익(25주).
공시: KIND 최근 45일 316140 공시 중 대량보유, 기업가치 제고 계획, 현금 현물배당 결정, 자기주식 취득 결정, 주식 소각 결정
새 접수번호마다 p3 1번. 첫 실행은 가격과 공시 모두 조용히 시드. 종가 실패는 하루 1번, KIND 실패는 3회 연속일 때 cwally-ops.
WOORI 창(configs/woori.json window_start~window_end, 기본 2027-01-26~02-27): 기업가치 제고/자기주식 취득은 ntfy 없이 handed to WOORI(pending).
WOORI ops_fetch_day/ops_parse_day(공시일 이후) 또는 lag_days(기본 1) 지나도 seen_rcps 미표기면 p3 대체 발송(본문에 WOORI 판정 실패로 대체 발송).
usage: k316140_monitor.py <auto|dry> | selftest
"""
from __future__ import annotations

import json
import re
import sys
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic  # noqa: E402
from kr_calendar import session_close_ready  # noqa: E402

KST = timezone(timedelta(hours=9))
NAME = "k316140"
TAG = "[316140]"
CFG_PATH = ROOT / "configs" / f"{NAME}.json"
STATE_PATH = ROOT / "state" / f"{NAME}.json"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
BUY_WORDS = ("추가매수", "매수 검토", "분할 매수", "매수 기회", "저가 매수", "사세요", "담을", "물타기 하", "비중 확대", "지금 매수", "관심 점검")
OPS = {"le": lambda v, x: v <= x, "lt": lambda v, x: v < x, "ge": lambda v, x: v >= x}


def get(url: str, headers: dict | None = None) -> bytes:
    h = {"User-Agent": UA, "Accept-Encoding": "identity"}
    h.update(headers or {})
    with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=30) as r:
        return r.read()


def won(v) -> str:
    return f"{v:,.0f}원" if v is not None else "?"


def confirmed(rows: list[tuple[str, float]], now: datetime) -> list[tuple[str, float]]:
    today = now.strftime("%Y-%m-%d")
    return sorted(r for r in rows if session_close_ready(r[0], today, now, (15, 35)))


def daum_rows(cfg: dict) -> list[tuple[str, float]]:
    d = json.loads(get(cfg["sources"]["daum"], {"Referer": "https://finance.daum.net/quotes/A316140"}))
    return [(r["date"][:10], float(r["tradePrice"])) for r in d.get("data") or [] if r.get("tradePrice")]


def yahoo_rows(cfg: dict) -> list[tuple[str, float]]:
    r = json.loads(get(cfg["sources"]["yahoo"]))["chart"]["result"][0]
    q = r["indicators"]["quote"][0]["close"]
    return [(datetime.fromtimestamp(t, KST).date().isoformat(), float(c)) for t, c in zip(r.get("timestamp") or [], q) if c]


def prices(cfg: dict, now: datetime) -> tuple[list[tuple[str, float]], str, list[str]]:
    notes = []
    for name, fn in (("Daum KRX", daum_rows), ("Yahoo 316140.KS", yahoo_rows)):
        try:
            rows = confirmed(fn(cfg), now)
            if rows:
                return rows, name, notes
            notes.append(f"{name} 확정 종가 없음")
        except Exception as exc:  # noqa: BLE001
            notes.append(f"{name} {str(exc)[:80]}")
    raise RuntimeError("; ".join(notes))


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


TODO = {
    "l30400": "20일선(11/5 기준 30,940원)과 전일 종가 부근 되돌림. 점검: 하락 원인(오버행 매물, 금리, 업종 수급), 3분기 실적 흐름 유지 여부. "
              "예시: 30,400원에 10주 추가하면 평단 33,018원에서 약 32,270원(35주). 금액과 분할 계획부터 정하기.",
    "l28800": "10/27 저점대(28,850원) 재방문. 점검: 유진PE, 포스코 잔여 지분 매각 공시 여부, NIM, 대손비용 흐름. "
              "예시: 28,800원에 10주 추가하면 평단 약 31,813원(35주). 1차 점검 뒤 무엇이 바뀌었는지 비교.",
    "h33100": "평단(33,018원)과 60일선(11/5 기준 33,132원) 회복. 추격 매수 보류. 비중 점검: 손익분기 근처에서 목표 비중과 보유 이유(배당, 환원율)가 그대로인지 확인.",
    "w26414": "평단 대비 -20% 손실 구간. 논리 재점검: 처음 산 이유(배당, 기업가치 제고, 저PBR)가 아직 유효한지, 비중을 줄일지 유지할지 미리 정한 기준으로 판단. 가격 점검 선은 멈춤.",
    "w25400": "52주 저점(25,400원) 아래 종가. 논리 재점검: NIM 회복 실패, 대손비용률 급증, 오버행, 금리 경로 중 무엇이 원인인지 먼저 확인. 기존 가격 점검은 멈춤.",
}
DONT = {
    "l30400": "하루 되돌림만 보고 판단, 계획 없이 한 번에 늘리기.",
    "l28800": "저점대라는 이유만으로 판단, 신용 및 미수.",
    "h33100": "본전 심리로 급등 추격, 배당 수익률만 보고 판단.",
    "w26414": "손실 회복만을 위해 비중 늘리기, 신용 및 미수.",
    "w25400": "저점 이탈을 싸졌다는 신호로 읽기, 신용 및 미수.",
}


def pnl(cfg: dict, p: float, day: str) -> str:
    pos = cfg["position"]
    n, avg = int(pos["shares"]), float(pos["avg"])
    d = p - avg
    out = f"평가손익: {n}주, 평단 {won(avg)} 대비 주당 {d:+,.0f}원, 합계 {d * n:+,.0f}원({d / avg * 100:+.2f}%)."
    if day <= pos.get("div_note_until", ""):
        out += " " + pos["div_note"] + "."
    return out


def footer(cfg: dict) -> str:
    return f"참고({cfg['as_of']} 기준): {cfg['context']} 판단 변경 조건: {cfg['change_conditions']} 보유: {cfg['holding']}"


def line_message(cfg: dict, fired: list[dict], day: str, p: float, src: str, stamp: str) -> tuple[str, str, int]:
    warn = any(x["warn"] for x in fired)
    shown = [x for x in fired if x["warn"]] if warn else fired
    title = f"{TAG} 우리금융 316140 종가 {won(p)} | " + " + ".join(x["title"] for x in shown)
    body = "\n".join([
        f"{stamp} | 316140 | 우리금융지주",
        f"이게 뭐 알림: 우리금융지주 보유 종목 KRX 확정 종가가 미리 정한 선에 닿았다는 알림. {'경고.' if warn else '점검 시점.'} 매매 지시 아님.",
        f"지금 결과/상태: {day} 종가 {won(p)}({src}). 걸린 선: {', '.join(x['title'] for x in shown)}. {pnl(cfg, p, day)} {footer(cfg)}",
        "지금 할 일: " + " ".join(TODO[x["key"]] for x in shown),
        "나중에: 같은 선은 재무장 뒤에만 다시 옵니다(30,400 선은 31,300원 이상, 28,800 선은 29,700원 이상, 33,100 선은 32,100원 미만, "
        f"26,414 선은 27,200원 이상, 25,400 선은 26,200원 이상). 공시 감시는 계속. 종료 {cfg['ends_kst'][:10]}.",
        "익절/줄일 때: " + ("보유 25주. 줄일지 유지할지는 미리 정한 손실 기준으로 판단. 이 알림은 주문이 아님." if warn
                           else "보유 25주. 줄일 기준은 본인이 미리 정한 값으로 판단. 이 알림은 주문이 아님."),
        "하지 말 것: " + " ".join(DONT[x["key"]] for x in shown) + " 투자 권유 아님.",
    ])
    if warn:
        bad = [w for w in BUY_WORDS if w in title + body]
        if bad:
            raise ValueError(f"buy wording in warning: {bad}")
    return title, body, max(x["priority"] for x in fired)


def kind_rows(since: str, until: str) -> list[dict]:
    import usdiv_monitor as u  # lazy: KIND 상세검색 재사용
    return u.kind_search("316140", "", since, until, 3)


def disc_match(cfg: dict, title: str) -> bool:
    w = cfg["disclosure_watch"]
    return any(re.search(p, title) for p in w["patterns"]) and not any(re.search(x, title) for x in w["exclude"])


def show(title: str) -> str:
    t = re.sub(r"^우리금융지주\s+", "", title).replace("\u3006", "").replace("\u318d", ", ").replace("\u00b7", ", ")
    return re.sub(r"\s+", " ", t).strip()


def disc_kind(title: str) -> str:
    t = show(title)
    if "대량보유" in t:
        return "대량보유 보고"
    if "가치" in t and "제고" in t:
        return "기업가치 제고 계획"
    if "배당" in t:
        return "배당 결정"
    if "자기주식" in t:
        return "자기주식 취득 결정"
    if "소각" in t:
        return "주식 소각 결정"
    return "기타 공시"


def load_woori_cfg(io: dict | None = None) -> dict:
    if io and "woori_cfg" in io:
        return io["woori_cfg"]
    path = ROOT / "configs" / "woori.json"
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {"window_start": "2027-01-26", "window_end": "2027-02-27"}


def load_woori_state(io: dict | None = None) -> dict:
    if io and "woori_state" in io:
        return io["woori_state"]
    path = ROOT / "state" / "woori.json"
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {}


def woori_window(cfg: dict, io: dict | None = None) -> tuple[str, str]:
    wcfg = load_woori_cfg(io)
    return str(wcfg.get("window_start") or "2027-01-26"), str(wcfg.get("window_end") or "2027-02-27")


def in_woori_window(cfg: dict, day: str, io: dict | None = None) -> bool:
    h = (cfg.get("disclosure_watch") or {}).get("handoff_to_woori") or {}
    if not h.get("enabled", True):
        return False
    start, end = woori_window(cfg, io)
    return start <= day <= end


def is_handoff_disc(cfg: dict, title: str) -> bool:
    h = (cfg.get("disclosure_watch") or {}).get("handoff_to_woori") or {}
    pats = h.get("title_patterns") or []
    return any(re.search(p, title) for p in pats)


def filed_day(r: dict) -> str:
    return str(r.get("filed") or "")[:10]


def woori_processed(wst: dict, acptno: str) -> bool:
    seen = wst.get("seen_rcps") or []
    return any(acptno == str(x) or acptno in str(x) for x in seen)


def woori_failed_since(wst: dict, day: str) -> bool:
    for key in ("ops_fetch_day", "ops_parse_day"):
        d = wst.get(key)
        if d and str(d)[:10] >= day:
            return True
    return False


def pending_should_fallback(cfg: dict, item: dict, today: str, wst: dict) -> str | None:
    """Return reason string if fallback should fire, else None. 'resolved' means drop silently."""
    acpt = item["acptno"]
    fday = item["filed"][:10]
    if woori_processed(wst, acpt):
        return "resolved"
    if woori_failed_since(wst, fday):
        return "woori_ops_fail"
    lag = int(((cfg.get("disclosure_watch") or {}).get("handoff_to_woori") or {}).get("lag_days") or 1)
    if date.fromisoformat(today) >= date.fromisoformat(fday) + timedelta(days=lag):
        return "woori_lag"
    return None


def disc_message(cfg: dict, r: dict, stamp: str, *, fallback: str | None = None) -> tuple[str, str, int]:
    t = show(r["title"])
    kind = disc_kind(r["title"])
    url = cfg["sources"]["kind_view"].format(acptno=r["acptno"])
    hint = {"대량보유 보고": "보고자와 지분 변동(유진PE, 포스코 등 매도인지, 5% 이상 신규인지)을 확인. 오버행 재부각 여부 판단.",
            "기업가치 제고 계획": "총주주환원율 50% 이상 공식화, DPS 성장, CET1 목표가 바뀌었는지 확인.",
            "배당 결정": "주당 배당금, 기준일, 비과세(자본준비금 감액) 재원 여부 확인.",
            "자기주식 취득 결정": "매입 규모와 기간, 소각 여부 확인.",
            "주식 소각 결정": "소각 주식 수와 발행주식 대비 비율 확인.",
            "기타 공시": "원문 확인."}[kind]
    status = f"{t} ({r['filed']}, 접수번호 {r['acptno']}). 원문: {url}"
    if fallback:
        status += " WOORI 판정 실패로 대체 발송."
    body = "\n".join([
        f"{stamp} | 316140 | 우리금융지주",
        f"이게 뭐 알림: 우리금융지주 새 공시({kind}) 1회 알림. 내용 판단은 직접. 매매 지시 아님.",
        f"지금 결과/상태: {status} {footer(cfg)}",
        f"지금 할 일: {hint}",
        "나중에: 같은 접수번호는 다시 보내지 않습니다. 가격 선 감시는 계속.",
        "익절/줄일 때: 보유 25주. 공시만으로 줄이거나 늘리지 말고 내용 확인 뒤 판단.",
        "하지 말 것: 제목만 보고 매매, 공시 직후 급등락 추격. 투자 권유 아님.",
    ])
    return f"{TAG} 우리금융 316140 공시 | {kind}: {t[:60]}", body, int(cfg["disclosure_watch"]["priority"])


def run(mode: str, now: datetime, cfg: dict, io: dict | None = None) -> tuple[list, dict]:
    io = io or {}
    dry = mode == "dry"
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    today = stamp[:10]
    st = io["state"] if "state" in io else (json.loads(STATE_PATH.read_text(encoding="utf-8")) if STATE_PATH.exists() else {})
    out: list[tuple[str, str, int]] = []
    try:
        rows, src, notes = io["prices"]() if "prices" in io else prices(cfg, now)
        for n in notes:
            print(n)
        armed = st.setdefault("armed", {})
        last = st.get("last_date")
        fired: list[dict] = []
        day, p = rows[-1]
        for d, c in rows:
            if (last is None and d == day) or (last is not None and d > last):
                for f in judge(cfg, armed, c):
                    fired = [x for x in fired if x["key"] != f["key"]] + [f]
        if fired and last is not None:
            out.append(line_message(cfg, fired, day, p, src, stamp))
        st.update(last_date=day, last_close=[day, p], src=src)
        print(f"close {day} {won(p)} ({src}) armed={armed}")
    except Exception as exc:  # noqa: BLE001
        if st.get("price_fail_day") != today:
            st["price_fail_day"] = today
            out.append((f"{TAG} 우리금융 316140 종가 조회 실패 | 판정 보류", "\n".join([
                f"{stamp} | 316140 | 우리금융지주", "이게 뭐 알림: 확정 종가 조회 실패 안내. 매매 신호 아님.",
                f"지금 결과/상태: Daum KRX, Yahoo 모두 확인 불가 ({str(exc)[:150]}).", "지금 할 일: 필요하면 직접 확인.",
                "나중에: 다음 거래일 15:40 에 밀린 종가를 순서대로 판정.", "익절/줄일 때: 해당 없음.", "하지 말 것: 조회 실패를 조건 미충족으로 읽기."]), 3))
    seen = st.setdefault("disc_seen", [])
    pending = st.setdefault("woori_pending", [])  # [{acptno, filed, title}]
    try:
        since = (now.date() - timedelta(days=int(cfg["disclosure_watch"]["lookback_days"]))).isoformat()
        lst = io["kind"]() if "kind" in io else kind_rows(since, today)
        hits = [r for r in lst if disc_match(cfg, r["title"])]
        seeding = not st.get("disc_seeded")
        window = in_woori_window(cfg, today, io)
        wst = load_woori_state(io)
        # resolve / fallback pending first (dedupe by acptno)
        still: list[dict] = []
        pending_acs = {p["acptno"] for p in pending}
        for item in pending:
            reason = pending_should_fallback(cfg, item, today, wst)
            if reason == "resolved":
                print(f"handed to WOORI resolved (seen_rcps): {item['acptno']}")
                continue
            if reason:
                print(f"WOORI fallback ({reason}): {item['acptno']}")
                out.append(disc_message(cfg, item, stamp, fallback=reason))
                continue
            still.append(item)
        pending = still
        for r in sorted(hits, key=lambda x: x["acptno"]):
            acpt = r["acptno"]
            if acpt in seen:
                continue
            seen.append(acpt)
            if seeding:
                continue
            if window and is_handoff_disc(cfg, r["title"]):
                if acpt not in pending_acs and acpt not in {p["acptno"] for p in pending}:
                    pending.append({"acptno": acpt, "filed": r["filed"], "title": r["title"]})
                print(f"handed to WOORI: {acpt} {disc_kind(r['title'])}")
                continue
            out.append(disc_message(cfg, r, stamp))
        if seeding:
            st["disc_seeded"] = stamp
            print(f"disclosures seeded: {len(hits)} matches marked seen")
        st["disc_seen"] = seen[-200:]
        st["woori_pending"] = pending[-50:]
        st["kind_fail"] = 0
        print(f"KIND rows {len(lst)}, matches {len(hits)}, pending={len(pending)}")
    except Exception as exc:  # noqa: BLE001
        st["kind_fail"] = int(st.get("kind_fail") or 0) + 1
        print(f"KIND fail {st['kind_fail']}: {exc}")
        if st["kind_fail"] >= 3 and st.get("kind_fail_day") != today:
            st["kind_fail_day"] = today
            out.append((f"{TAG} 우리금융 316140 공시 조회 실패 | 공시 감시 보류", "\n".join([
                f"{stamp} | 316140 | 우리금융지주", "이게 뭐 알림: KIND 공시 조회 3회 연속 실패 안내. 매매 신호 아님.",
                f"지금 결과/상태: KIND 확인 불가 ({str(exc)[:150]}).", "지금 할 일: 필요하면 DART 에서 직접 확인.",
                "나중에: 복구되면 최근 45일 공시를 다시 보고 새 접수번호만 보냅니다.", "익절/줄일 때: 해당 없음.",
                "하지 말 것: 조회 실패를 공시 없음으로 읽기."]), 3))
    st["updated_at"] = stamp
    for t, m, _ in out:
        if "\u00b7" in t + m or "\u2014" in t + m:
            raise ValueError(f"forbidden char in {t}")
    if "state" in io:
        return out, st
    for t, m, pri in out:
        print(t)
        print(m)
        if not dry:
            publish(resolve_topic("port", cfg.get("ntfy_topic")), t, m, pri, ["bank"], resolve_server(cfg.get("ntfy_server")), config_name=NAME)
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
    check("seed at 31,950 silent, all armed", (judge(cfg, a, 31950), a), ([], {"l30400": True, "l28800": True, "h33100": True, "w25400": True, "w26414": True}))
    walk = [31000, 30400, 30000, 31299, 31300, 30300, 28800, 29600, 29700, 28700, 33100, 32200, 32099, 33200, 25400, 26100, 26200, 25300]
    a = {"l30400": True, "l28800": True, "h33100": True, "w25400": True, "w26414": True}
    got = [[x["key"] for x in judge(cfg, a, p)] for p in walk]
    check("walk with re-arm buffers", got, [[], ["l30400"], [], [], [], ["l30400"], ["l28800"], [], [], ["l28800"], ["h33100"], [], [], ["h33100"],
                                             ["l30400", "l28800", "w25400", "w26414"], [], [], ["w25400"]])  # gap down crosses all three; message shows only the warning
    L = {x["key"]: x for x in cfg["lines"]}
    t1, b1, p1 = line_message(cfg, [L["l30400"]], "2026-11-10", 30300, "Daum KRX", "s")
    t4, b4, p4 = line_message(cfg, [L["l30400"], L["l28800"], L["w25400"]], "2026-12-01", 25300, "Daum KRX", "s")
    t5, b5, p5 = line_message(cfg, [L["w26414"]], "2026-12-01", 26400, "Daum KRX", "s")
    check("titles and priorities", (t1, p1, t4, p4, t5, p5), ("[316140] 우리금융 316140 종가 30,300원 | 1차 추가매수 점검 (20일선, 전일 종가 부근 되돌림)", 3,
                                                       "[316140] 우리금융 316140 종가 25,300원 | 경고 52주 저점 이탈, 논리 재점검", 4,
                                                       "[316140] 우리금융 316140 종가 26,400원 | 경고 평단 -20% 손실 점검, 논리 재점검", 4))
    check("loss warning has no buy wording", [w for w in BUY_WORDS if w in t5 + b5], [])
    check("P/L vs avg and conditional dividend", ("주당 -2,718원, 합계 -67,950원(-8.23%)" in b1, "11/6까지 보유 시" in b1, "11/6까지" in b4, "32,270원" in b1),
          (True, True, False, True))
    _, b6, _ = line_message(cfg, [L["h33100"]], "2026-12-01", 33100, "Daum KRX", "s")
    check("avg recovery body", ("합계 +2,050원(+0.25%)" in b6, "비중 점검" in b6), (True, True))
    check("warning has no buy wording", [w for w in BUY_WORDS if w in t4 + b4], [])
    check("body has context and change conditions", all(k in b1 for k in ("2026-11-05 기준", "CET1 13.80%", "1,360원", "PBR 0.62", "NIM 회복 실패",
                                                                           "대손비용률 50bp", "유진PE", "포스코", "11/26", "50% 공식화", "보유 25주, 평단 33,018원")), True)
    check("seven lines", [x.split(":")[0] for x in b1.split("\n")[1:]], ["이게 뭐 알림", "지금 결과/상태", "지금 할 일", "나중에", "익절/줄일 때", "하지 말 것"])
    now = datetime(2026, 11, 5, 15, 40, tzinfo=KST)
    check("confirmed: today row only after 15:35", (confirmed([("2026-11-05", 1.0)], datetime(2026, 11, 5, 15, 20, tzinfo=KST)), confirmed([("2026-11-05", 1.0)], now)),
          ([], [("2026-11-05", 1.0)]))
    K = [{"acptno": "20261023000653", "filed": "2026-10-23 15:35", "title": "우리금융지주 현금\u318d현물 배당 결정 우리금융지주"},
         {"acptno": "20261027000343", "filed": "2026-10-27 15:00", "title": "우리금융지주 신탁계약에의한 취득 상황보고서 우리금융지주"},
         {"acptno": "20261020000482", "filed": "2026-10-20 15:56", "title": "우리금융지주 기업설명회(IR) 개최(안내공시) 우리금융지주"}]
    rows = [("2026-11-04", 30350.0), ("2026-11-05", 31950.0)]
    io = {"state": {}, "prices": lambda: (rows, "Daum KRX", []), "kind": lambda: list(K)}
    out, st = run("dry", now, cfg, io)
    check("first run: price and disclosures seeded silently", (out, st["disc_seen"], st["armed"]["l30400"]), ([], ["20261023000653"], True))
    K += [{"acptno": "20261110000100", "filed": "2026-11-10 17:00", "title": "우리금융지주 주식등의대량보유상황보고서(일반) 유진프라이빗에쿼티"},
          {"acptno": "20261110000101", "filed": "2026-11-10 17:05", "title": "우리금융지주 자기주식취득 신탁계약 해지 결정 우리금융지주"},
          {"acptno": "20270205000001", "filed": "2027-02-05 15:30", "title": "우리금융지주 기업가치 제고 계획(자율공시)(2027년) 우리금융지주"},
          {"acptno": "20270205000002", "filed": "2027-02-05 15:31", "title": "우리금융지주 [정정] 현금\u318d현물 배당 결정 우리금융지주"}]
    rows.append(("2026-11-06", 30300.0))
    io["state"] = st
    out, st = run("dry", datetime(2026, 11, 6, 15, 40, tzinfo=KST), cfg, io)
    titles = [o[0] for o in out]
    check("line + 3 new disclosures (해지 excluded)", (len(out), titles[0].endswith("1차 추가매수 점검 (20일선, 전일 종가 부근 되돌림)"),
                                                       sum("공시 |" in t for t in titles)), (4, True, 3))
    allt = "".join(o[0] + o[1] for o in out)
    check("no middle dot or em-dash, DART ㆍ rewritten", ("\u00b7" in allt, "\u2014" in allt, "\u318d" in allt, "현금, 현물 배당 결정" in allt), (False, False, False, True))
    check("all titles have [316140] and 우리금융", all(t.startswith("[316140] 우리금융 316140") for t in titles), True)
    out, st = run("dry", datetime(2026, 11, 6, 15, 45, tzinfo=KST), cfg, io)
    check("re-run same day silent", out, [])
    bad = dict(io, state={"disc_seeded": "x", "disc_seen": [], "armed": {}}, prices=lambda: (_ for _ in ()).throw(RuntimeError("down")),
               kind=lambda: (_ for _ in ()).throw(OSError("kind down")))
    r = []
    for d in (9, 10, 11):
        out, bad["state"] = run("dry", datetime(2026, 11, d, 15, 40, tzinfo=KST), cfg, bad)
        r.append([o[0].split(" | ")[0] for o in out])
    check("price fail daily ops, KIND fail on 3rd run", r, [["[316140] 우리금융 316140 종가 조회 실패"], ["[316140] 우리금융 316140 종가 조회 실패"],
                                                            ["[316140] 우리금융 316140 종가 조회 실패", "[316140] 우리금융 316140 공시 조회 실패"]])

    # --- WOORI handoff window (pt79u) ---
    wcfg = {"window_start": "2027-01-26", "window_end": "2027-02-27"}
    prices_ok = lambda: ([("2027-02-04", 32000.0), ("2027-02-05", 32100.0)], "Daum KRX", [])
    kind_in = [
        {"acptno": "20270205001001", "filed": "2027-02-05 15:00", "title": "우리금융지주 기업가치 제고 계획(자율공시) 우리금융지주"},
        {"acptno": "20270205001002", "filed": "2027-02-05 15:05", "title": "우리금융지주 자기주식취득 신탁계약 체결 결정 우리금융지주"},
        {"acptno": "20270205001003", "filed": "2027-02-05 15:10", "title": "우리금융지주 주식등의대량보유상황보고서(일반) 포스코"},
    ]
    st0 = {"disc_seeded": "x", "disc_seen": [], "armed": {"l30400": True, "l28800": True, "h33100": True, "w25400": True, "w26414": True},
           "last_date": "2027-02-04", "woori_pending": []}
    io_h = {"state": st0, "prices": prices_ok, "kind": lambda: list(kind_in),
            "woori_cfg": wcfg, "woori_state": {"seen_rcps": [], "ops_fetch_day": None, "ops_parse_day": None}}
    out, st = run("dry", datetime(2027, 2, 5, 15, 40, tzinfo=KST), cfg, io_h)
    titles = [o[0] for o in out]
    check("in-window handoff: only 대량보유 sent", (sum("공시 |" in t for t in titles),
                                                   any("대량보유" in t for t in titles),
                                                   any("기업가치" in t or "자기주식" in t for t in titles)), (1, True, False))
    check("in-window pending has 2 handoff filings", sorted(p["acptno"] for p in st["woori_pending"]),
          ["20270205001001", "20270205001002"])
    check("in-window handoff marked seen", all(a in st["disc_seen"] for a in ("20270205001001", "20270205001002", "20270205001003")), True)

    # same day again: silent (dedupe) even with pending
    io_h["state"] = st
    out2, st = run("dry", datetime(2027, 2, 5, 15, 50, tzinfo=KST), cfg, io_h)
    check("in-window same-day dedupe silent", out2, [])

    # next day, woori healthy but not processed -> lag fallback
    io_h["state"] = st
    io_h["prices"] = lambda: ([("2027-02-05", 32100.0), ("2027-02-06", 32200.0)], "Daum KRX", [])
    io_h["kind"] = lambda: list(kind_in)  # no new
    io_h["woori_state"] = {"seen_rcps": [], "ops_fetch_day": None, "ops_parse_day": None}
    out, st = run("dry", datetime(2027, 2, 6, 15, 40, tzinfo=KST), cfg, io_h)
    titles = [o[0] for o in out]
    check("lag fallback sends both handoff types", (sum("공시 |" in t for t in titles), len(st["woori_pending"]),
                                                    all("WOORI 판정 실패로 대체 발송" in o[1] for o in out if "공시 |" in o[0])),
          (2, 0, True))
    # dedupe after fallback
    io_h["state"] = st
    out, st = run("dry", datetime(2027, 2, 6, 15, 50, tzinfo=KST), cfg, io_h)
    check("fallback dedupe silent", out, [])

    # ops-fail same-day fallback
    st1 = {"disc_seeded": "x", "disc_seen": [], "armed": {"l30400": True, "l28800": True, "h33100": True, "w25400": True, "w26414": True},
           "last_date": "2027-02-04", "woori_pending": []}
    io_f = {"state": st1, "prices": prices_ok, "kind": lambda: [
        {"acptno": "20270205002001", "filed": "2027-02-05 14:00", "title": "우리금융지주 기업가치 제고 계획(자율공시) 우리금융지주"}],
            "woori_cfg": wcfg, "woori_state": {"seen_rcps": [], "ops_fetch_day": None, "ops_parse_day": None}}
    out, st = run("dry", datetime(2027, 2, 5, 15, 40, tzinfo=KST), cfg, io_f)
    check("ops-fail path: first handoff no send", (out, [p["acptno"] for p in st["woori_pending"]]), ([], ["20270205002001"]))
    io_f["state"] = st
    io_f["woori_state"] = {"seen_rcps": [], "ops_fetch_day": "2027-02-05", "ops_parse_day": None}
    out, st = run("dry", datetime(2027, 2, 5, 16, 00, tzinfo=KST), cfg, io_f)
    check("ops-fail same-day fallback", (len(out) == 1 and "기업가치" in out[0][0] and "WOORI 판정 실패로 대체 발송" in out[0][1],
                                         st["woori_pending"]), (True, []))

    # woori processed -> drop pending silently
    st2 = {"disc_seeded": "x", "disc_seen": ["20270205003001"], "armed": {"l30400": True, "l28800": True, "h33100": True, "w25400": True, "w26414": True},
           "last_date": "2027-02-05",
           "woori_pending": [{"acptno": "20270205003001", "filed": "2027-02-05 15:00",
                              "title": "우리금융지주 자기주식취득 신탁계약 체결 결정 우리금융지주"}]}
    io_r = {"state": st2, "prices": lambda: ([("2027-02-05", 32100.0), ("2027-02-06", 32200.0)], "Daum KRX", []),
            "kind": lambda: [], "woori_cfg": wcfg,
            "woori_state": {"seen_rcps": ["20270205003001"], "ops_fetch_day": None, "ops_parse_day": None}}
    out, st = run("dry", datetime(2027, 2, 6, 15, 40, tzinfo=KST), cfg, io_r)
    check("woori processed: pending cleared, no fallback send", (out, st["woori_pending"]), ([], []))

    # out-of-window: handoff types send normally
    st3 = {"disc_seeded": "x", "disc_seen": [], "armed": {"l30400": True, "l28800": True, "h33100": True, "w25400": True, "w26414": True},
           "last_date": "2026-12-01"}
    io_o = {"state": st3, "prices": lambda: ([("2026-12-01", 31000.0), ("2026-12-02", 30900.0)], "Daum KRX", []),
            "kind": lambda: [{"acptno": "20261202004001", "filed": "2026-12-02 15:00",
                              "title": "우리금융지주 기업가치 제고 계획(자율공시) 우리금융지주"}],
            "woori_cfg": wcfg, "woori_state": {}}
    out, st = run("dry", datetime(2026, 12, 2, 15, 40, tzinfo=KST), cfg, io_o)
    check("out-of-window send 기업가치", (len(out) == 1 and "기업가치" in out[0][0] and "대체 발송" not in out[0][1],
                                      st.get("woori_pending", [])), (True, []))

    print("selftest", "PASS" if not fails else f"FAIL {fails}")
    return 1 if fails else 0


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    if mode == "selftest":
        return selftest(cfg)
    if mode not in {"auto", "dry"}:
        print(__doc__)
        return 2
    now = datetime.now(KST)
    if now > datetime.strptime(cfg["ends_kst"], "%Y-%m-%d %H:%M").replace(tzinfo=KST):
        print(f"{NAME} ended; silent")
        return 0
    from kr_calendar import is_trading_day
    if not is_trading_day(date.fromisoformat(now.strftime("%Y-%m-%d"))) and mode != "dry":
        print("KRX holiday; silent")
        return 0
    run(mode, now, cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
