#!/usr/bin/env python3
"""[KR-RATE] 국고채 금리 조건 알림 (148070 국고10년 ETF 판단 보조). 매매 신호 아님, 자동 주문 아님.

기존 k203780 19:00 슬롯(cron-job.org 정시 dispatch + GitHub cron 백업)에 붙어 KRX 거래일에만 판정한다.
1) 국고채 10년 종가 >= yield10.trigger_pct -> '148070 분할매수 시작 검토' 1회. 종가 < rearm_below_pct 면 다시 무장.
2) 금통위 결정일: 기준금리 결정(동결/인상/인하, 새 수준)과 국고3년/10년 종가, 하루 변화만 요약 (판단 없음).
   근원물가(농산물및석유류제외 전년동월비): 새 달 수치가 처음 보이는 실행에서 1회, 2.5% 미만이면 강조.
3) 신용 스프레드(회사채 AA- 3년 - 국고채 3년)가 20거래일 전보다 widen_trigger_bp 넘게 확대 -> 1회.
   확대폭 < rearm_below_bp 면 다시 무장. 일별 값은 state hist 에 저장.
데이터: 한국은행 ECOS (KOFIA 최종호가, 기존 ECOS_API_KEY secret, 없으면 sample 키), 네이버 시장지표(대체, 출처 표시),
한국은행 금통위 일정/보도자료/기준금리 표(ktb148070_monitor 함수 재사용, 읽기 전용).
값의 날짜가 판정 거래일과 같을 때만 쓴다. 없으면 '확인 불가', 조회 실패는 cwally-ops.
modes: auto | dry(발송 없음, state 안 씀, --as-of "YYYY-MM-DD HH:MM" 가능) | selftest(가짜 값으로 조건 검사, 네트워크 없음)
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic, route_topic  # noqa: E402
from kr_calendar import KST, is_trading_day, prev_trading_day, session_day  # noqa: E402
import ktb148070_monitor as ktb  # noqa: E402  (읽기 전용 재사용: 한은 결정문/기준금리 표/회의 페이지/뉴스)

NAME = "krrate"
CFG_PATH = ROOT / "configs" / f"{NAME}.json"
STATE_PATH = ROOT / "state" / f"{NAME}.json"
NA = "확인 불가"
UA = {"User-Agent": "Mozilla/5.0 (cwally-alerts; +mailto:sosmos3218@gmail.com)"}
DEC_KO = {"hold": "동결", "cut": "인하", "hike": "인상"}
ECOS_ITEMS = {"y3": "010200000", "y10": "010210000", "aa": "010300000"}


# ---------------- io ----------------
def load_state() -> dict:
    base = {"updated_at": None, "seeded": False, "y10": {"armed": True, "last_day": None, "fired_day": None},
            "spread": {"armed": True, "last_day": None, "fired_day": None, "seeded": False},
            "cpi_last_month": None, "mpc_done": [], "hist": {}, "fail_notice": {}}
    if STATE_PATH.exists():
        try:
            base.update(json.loads(STATE_PATH.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass
    return base


def get(url: str, timeout: int = 25) -> str:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def num(v) -> float | None:
    try:
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


# ---------------- 데이터 ----------------
def ecos(stat: str, cycle: str, start: str, end: str, item: str) -> dict[str, float]:
    """{TIME: value}. 키가 없으면 sample 키로 10행씩 나눠 받는다."""
    key = os.environ.get("ECOS_API_KEY") or ""
    step = 100 if key else 10
    key = key or "sample"
    out: dict[str, float] = {}
    i = 1
    while i <= 400:
        url = (f"https://ecos.bok.or.kr/api/StatisticSearch/{urllib.parse.quote(key)}/json/kr/{i}/{i + step - 1}/"
               f"{stat}/{cycle}/{start}/{end}/{item}")
        d = json.loads(get(url))
        if "StatisticSearch" not in d:
            res = d.get("RESULT") or {}
            if res.get("CODE") == "INFO-200" and i > 1:  # 더 없음
                break
            if res.get("CODE") == "INFO-200":
                return out
            raise RuntimeError(f"ECOS {stat}/{item} {res.get('CODE')} {str(res.get('MESSAGE'))[:60]}")
        rows = d["StatisticSearch"].get("row") or []
        for r in rows:
            v = num(r.get("DATA_VALUE"))
            if v is not None:
                out[str(r["TIME"])] = v
        total = int(d["StatisticSearch"].get("list_total_count") or 0)
        i += step
        if i > total:
            break
    return out


def ecos_daily(item: str, start: date, end: date) -> dict[str, float]:
    raw = ecos("817Y002", "D", start.strftime("%Y%m%d"), end.strftime("%Y%m%d"), item)
    return {f"{k[:4]}-{k[4:6]}-{k[6:8]}": v for k, v in raw.items()}


def naver_bond(code: str) -> dict[str, float]:
    raw = json.loads(get("https://m.stock.naver.com/front-api/marketIndex/prices?category=bond&reutersCode="
                         f"{urllib.parse.quote(code)}&page=1&pageSize=30"))
    out = {}
    for r in (raw or {}).get("result") or []:
        d = str(r.get("localTradedAt") or "")[:10]
        v = num(r.get("closePrice"))
        if len(d) == 10 and v is not None:
            out[d] = v
    return out


def naver_aa() -> dict[str, float]:
    raw = json.loads(get("https://m.stock.naver.com/front-api/marketIndex/prices?category=domesticInterest"
                         "&reutersCode=KFIA103009&page=1&pageSize=30"))
    out = {}
    for r in (raw or {}).get("result") or []:
        d = str(r.get("localTradedAt") or "")[:10]
        v = num(r.get("closePrice"))
        if len(d) == 10 and v is not None:
            out[d] = v
    return out


def cpi_yoy(end_month: str) -> dict:
    """{'month': 'YYYY-MM', 'qb': yoy, 'qb_prev': yoy 전월, 'db': yoy} 최신 달 기준."""
    y, m = int(end_month[:4]), int(end_month[5:7])
    start = f"{y - 2}{m:02d}"
    qb = ecos("901Y010", "M", start, f"{y}{m:02d}", "QB")
    db = ecos("901Y010", "M", start, f"{y}{m:02d}", "DB")
    if not qb:
        raise RuntimeError("ECOS 901Y010/QB 비어 있음")
    last = max(qb)

    def yoy(series: dict, t: str) -> float | None:
        p = f"{int(t[:4]) - 1}{t[4:]}"
        return round((series[t] / series[p] - 1) * 100, 1) if t in series and p in series and series[p] else None

    lm = int(last[4:])
    prev_t = f"{int(last[:4]) - (1 if lm == 1 else 0)}{(12 if lm == 1 else lm - 1):02d}"
    return {"month": f"{last[:4]}-{last[4:]}", "qb": yoy(qb, last), "qb_prev": yoy(qb, prev_t), "db": yoy(db, last)}


def mpc_schedule(cfg: dict, year: int) -> tuple[list[str], list[str]]:
    """(날짜 목록, 메모). config 목록 + 한은 페이지(올해, 내년)."""
    days = set(cfg["mpc"]["schedule"])
    notes = []
    for y in (year, year + 1):
        try:
            html = get(cfg["sources"]["bok_meeting_page"].format(year=y))
            found = {f"{y}-{mo}-{d}" for mo, d in re.findall(r'<th scope="row">\s*(\d{2})월\s*(\d{2})일', html)}
            days |= found
            if not found:
                notes.append(f"{y}년 일정 한은 페이지에 아직 없음")
        except Exception as exc:  # noqa: BLE001
            notes.append(f"{y}년 한은 일정 페이지 조회 실패({type(exc).__name__})")
    return sorted(days), notes


def mpc_decision(cfg: dict, d: str) -> dict:
    """ktb148070 의 결정 판정 함수를 이 회의 날짜로 다시 쓴다."""
    y, mo, dd = d.split("-")
    md = datetime.strptime(d, "%Y-%m-%d")
    kcfg = {
        "meeting_date": d,
        "current_base_rate": None,
        "rules": cfg["mpc"]["rules"],
        "sources": {
            "bok_news_list": cfg["sources"]["bok_news_list"],
            "bok_release_title_re": rf"통화정책방향\s*\(\s*{y}\s*\.\s*0?{int(mo)}\s*\.\s*0?{int(dd)}\s*\)",
            "bok_base_rate_table": cfg["sources"]["bok_base_rate_table"],
            "bok_meeting_page": cfg["sources"]["bok_meeting_page"].format(year=y),
            "news_rss_query": f"한국은행 기준금리 금통위 after:{(md - timedelta(days=1)):%Y-%m-%d} before:{(md + timedelta(days=2)):%Y-%m-%d}",
        },
    }
    table = ktb.bok_base_table(kcfg)
    ref = table.get("latest_rate") if table.get("status") == "ok" else None
    kcfg["current_base_rate"] = ref if ref is not None else 0.0
    rel = ktb.bok_release(kcfg)
    mtg = ktb.bok_meeting(kcfg)
    news = ktb.news_source(kcfg, kcfg["current_base_rate"])
    dec = ktb.resolve_decision(kcfg, rel, table, mtg, news)
    try:  # 네이버 기준금리(발표 시각 포함) 교차확인
        rows = json.loads(get("https://stock.naver.com/api/securityService/marketindex/standardInterest"))
        r = next(x for x in rows if x.get("reutersCode") == "KROCRT=ECIX")
        dec["naver"] = (str(r.get("localTradedAt"))[:10], num(r.get("closePrice")))
    except Exception:  # noqa: BLE001
        dec["naver"] = None
    return dec


# ---------------- 판정 (순수 함수, selftest 대상) ----------------
def eval_y10(st: dict, v: float, cfg: dict) -> str | None:
    """'fire' | 'rearm' | None. st 를 바꾼다."""
    g = cfg["yield10"]
    if st["armed"] and v >= g["trigger_pct"]:
        st["armed"] = False
        return "fire"
    if not st["armed"] and v < g["rearm_below_pct"]:
        st["armed"] = True
        return "rearm"
    return None


def spread_widen(hist: dict, d: str, lookback: int) -> tuple[float, float, str, float] | None:
    """(오늘 bp, 확대 bp, 기준일, 기준일 bp). hist = {date: {'y3','aa'}} ECOS 관측일만."""
    days = sorted(k for k, v in hist.items() if v.get("y3") is not None and v.get("aa") is not None)
    if d not in days:
        return None
    i = days.index(d)
    if i < lookback:
        return None
    b = days[i - lookback]
    sp = (hist[d]["aa"] - hist[d]["y3"]) * 100
    bsp = (hist[b]["aa"] - hist[b]["y3"]) * 100
    return sp, sp - bsp, b, bsp


def eval_spread(st: dict, widen: float, cfg: dict) -> str | None:
    g = cfg["spread"]
    if st["armed"] and widen > g["widen_trigger_bp"]:
        st["armed"] = False
        return "fire"
    if not st["armed"] and widen < g["rearm_below_bp"]:
        st["armed"] = True
        return "rearm"
    return None


# ---------------- 메시지 ----------------
def body(stamp: str, what: str, status: str, action: str, later: str, trim: str, avoid: str) -> str:
    return "\n".join([f"{stamp} | KR-RATE | 국고채 금리 점검", f"이게 뭐 알림: {what}", f"지금 결과/상태: {status}",
                      f"지금 할 일: {action}", f"나중에: {later}", f"익절/줄일 때: {trim}", f"하지 말 것: {avoid}"])


def fmt(v: float | None, n: int = 3) -> str:
    return f"{v:.{n}f}%" if v is not None else NA


def y10_msg(cfg, stamp, d, v, src, prev, y3, nxt):
    g = cfg["yield10"]
    chg = f", 직전 거래일 {fmt(prev[1])} 대비 {(v - prev[1]) * 100:+.1f}bp" if prev and prev[1] is not None else ""
    title = f"[KR-RATE] 국고10년 {v:.3f}% ({d[5:7]}/{d[8:]} 종가) | 148070 분할매수 시작 검토"
    msg = body(stamp, f"국고채 10년 종가가 {g['trigger_pct']:.2f}% 이상이 되면 1번 오는 알림(148070 국고10년 ETF 분할매수 조건). 매매 신호 아님.",
               f"{d} 국고채 10년 종가 {v:.3f}% ({src}){chg}. 기준 {g['trigger_pct']:.2f}% 이상. 국고3년 {fmt(y3)}. 다음 금통위 {nxt}.",
               "148070 분할매수 시작을 검토. 머니마켓 ETF 에서 옮길 금액과 분할 횟수를 먼저 정하고 1회분만.",
               f"10년 종가가 {g['rearm_below_pct']:.2f}% 아래로 내려갔다가 다시 {g['trigger_pct']:.2f}% 이상이 되면 다시 옵니다.",
               "금리가 더 오르면(가격 하락) 정한 분할 간격대로만 추가. 목표 비중을 넘기지 않기.",
               "한 번에 전액 매수, 금리 고점 단정, 확인 불가 값을 0이나 정상으로 읽기.")
    return title, msg, 4, ["rotating_light"]


def spread_msg(cfg, stamp, d, aa, y3, sp, w, b, bsp):
    g = cfg["spread"]
    title = f"[KR-RATE] 신용 스프레드 {w:+.0f}bp 확대 (20거래일) | 회사채 위험 점검"
    msg = body(stamp, f"회사채 AA- 3년과 국고채 3년 금리 차이가 약 1개월(20거래일) 전보다 {g['widen_trigger_bp']}bp 넘게 벌어지면 1번 오는 알림. 매매 신호 아님.",
               f"{d} 회사채 AA- 3년 {aa:.3f}% - 국고채 3년 {y3:.3f}% = {sp:.0f}bp. {b} {bsp:.0f}bp 대비 {w:+.0f}bp "
               f"(기준 {g['widen_trigger_bp']}bp 초과). 출처 KOFIA 최종호가(한국은행 ECOS).",
               "회사채나 크레딧 상품 비중이 있으면 점검하고, 신용 경색 관련 뉴스를 확인. 국고채(148070)와 머니마켓 ETF 는 신용 위험과 따로 봅니다.",
               f"확대폭이 {g['rearm_below_bp']}bp 미만으로 줄면 다시 무장하고, 다시 벌어지면 또 옵니다.",
               "회사채형 상품이 있으면 비중 축소 검토.",
               "스프레드 하나로 국고채까지 팔기, 확인 불가 값을 정상으로 읽기.")
    return title, msg, 4, ["rotating_light"]


def mpc_msg(stamp, d, dec, y, nxt):
    dk = DEC_KO.get(dec["decision"], dec["decision"])
    prev = f"{dec['prev']:.2f}% → " if dec.get("prev") is not None else ""
    ylines = []
    for k, nm in (("y3", "국고3년"), ("y10", "국고10년")):
        v, p, src = y.get(k, (None, None, None))
        if v is None:
            ylines.append(f"{nm} 종가 {NA}")
        else:
            ch = f", 전일 대비 {(v - p) * 100:+.1f}bp" if p is not None else ", 전일 대비 확인 불가"
            ylines.append(f"{nm} 종가 {v:.3f}%{ch} ({src})")
    nv = dec.get("naver")
    cross = ""
    if nv and nv[1] is not None:
        cross = f" 네이버 기준금리 {nv[1]:.2f}% ({nv[0]} 발표) {'일치' if abs(nv[1] - dec['rate']) < 0.001 and nv[0] == d else '불일치, 원문 확인'}."
    y10 = y.get("y10", (None,))[0]
    title = f"[KR-RATE] {d[5:7]}/{d[8:]} 금통위 기준금리 {dk} {dec['rate']:.2f}%" + (f" | 국고10년 {y10:.3f}%" if y10 is not None else "")
    msg = body(stamp, "금통위(한국은행 금융통화위원회) 기준금리 결정일 저녁 사실 요약. 판단 없음. 매매 신호 아님.",
               f"기준금리 {prev}{dec['rate']:.2f}% ({dk}), 출처 {dec.get('source')}. {dec.get('dissent') or ''}.{cross} "
               + " / ".join(ylines) + f". 다음 금통위 {nxt}.",
               "결정과 금리 숫자만 확인. 148070 분할매수 조건(10년 종가 기준)은 따로 알림이 옵니다.",
               f"다음 금통위({nxt}) 저녁에 다시 옵니다.",
               "이 요약만으로 비중을 바꾸지 않기.",
               "결정 하나로 금리 방향 단정하기(인상 종료 같은 판단 포함), 확인 불가 값을 정상으로 읽기.")
    return title, msg, 3, ["bank"]


def cpi_msg(cfg, stamp, c):
    g = cfg["cpi"]
    emph = c["qb"] is not None and c["qb"] < g["emphasis_below_pct"]
    y, m = c["month"].split("-")
    title = f"[KR-RATE] {y}년 {int(m)}월 근원물가 {c['qb']:.1f}% (농산물 및 석유류 제외)" + (f" | {g['emphasis_below_pct']}% 미만" if emph else "")
    prev = f" (전월 {c['qb_prev']:.1f}%)" if c.get("qb_prev") is not None else ""
    db = f"{c['db']:.1f}%" if c.get("db") is not None else NA
    msg = body(stamp, "근원물가(농산물및석유류제외지수 전년동월비) 새 달 수치가 나오면 1번 오는 알림. 2.5% 미만이면 강조. 매매 신호 아님.",
               f"{y}년 {int(m)}월 농산물및석유류제외 전년동월비 {c['qb']:.1f}%{prev}, 기준 {g['emphasis_below_pct']}% "
               f"{'미만' if emph else '이상'}. 참고 식료품및에너지제외 {db}. 출처 국가데이터처 소비자물가동향 지수(한국은행 ECOS 901Y010, 지수로 계산).",
               "물가 흐름만 확인. 금리 판단은 국고10년 조건 알림과 금통위 요약으로.",
               "다음 달 소비자물가동향(보통 매월 초) 뒤 첫 19:00 실행에서 다시 옵니다.",
               "이 숫자만으로 비중을 바꾸지 않기.",
               "한 달 수치로 금리 인하나 인상을 단정하기.")
    return title, msg, (4 if emph else 3), (["rotating_light"] if emph else ["chart_with_downwards_trend"])


# ---------------- main ----------------
def trading_days_between(a: str | None, b: date, cap: int) -> list[str]:
    out, d = [], b
    while len(out) < cap and (a is None or d.isoformat() > a):
        if is_trading_day(d):
            out.append(d.isoformat())
        d -= timedelta(days=1)
    return sorted(out)


def main() -> int:
    args = sys.argv[1:]
    mode = (args[0] if args and not args[0].startswith("--") else "auto").lower()
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    if mode == "selftest":
        return selftest(cfg)
    now = datetime.now(KST)
    if "--as-of" in args:
        if mode != "dry":
            print("--as-of 는 dry 에서만")
            return 2
        now = datetime.strptime(args[args.index("--as-of") + 1], "%Y-%m-%d %H:%M").replace(tzinfo=KST)
    if mode not in {"auto", "dry"}:
        print(f"unknown mode {mode}")
        return 2
    dry = mode == "dry"
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    today = now.strftime("%Y-%m-%d")
    topic = resolve_topic("port", cfg.get("ntfy_topic"))
    server = resolve_server(cfg.get("ntfy_server"))
    state = load_state()
    sess = session_day(now)
    print(f"now={now:%Y-%m-%d %H:%M} session={sess} trading={is_trading_day(sess)} ecos_key={'yes' if os.environ.get('ECOS_API_KEY') else 'no(sample)'}")
    if not is_trading_day(sess):
        print("not a KRX trading day: skip")
        return 0
    late = now.date() > sess or now.hour >= 18
    out: list[tuple[str, str, int, list[str]]] = []
    errors: list[str] = []

    def ops_once(key: str, title: str, msg: str) -> None:
        fn = state.setdefault("fail_notice", {})
        if fn.get(key) != today:
            out.append((title, msg, 3, ["warning"]))
            fn[key] = today

    def ops_body(what: str, status: str) -> str:
        return body(stamp, what, status, "필요하면 한국은행 ECOS 나 증권사 앱에서 직접 보세요.", "다음 19:00 실행 때 다시 봅니다.",
                    "이번 안내로 비중을 바꾸지 마세요.", "조회 실패를 조건 미충족으로 읽기.")

    # --- 데이터 ---
    start = sess - timedelta(days=70)
    ec: dict[str, dict[str, float]] = {}
    for k, item in ECOS_ITEMS.items():
        try:
            ec[k] = ecos_daily(item, start, sess)
        except Exception as exc:  # noqa: BLE001
            ec[k] = {}
            errors.append(f"ECOS {k}: {type(exc).__name__} {str(exc)[:80]}")
    nv: dict[str, dict[str, float]] = {}
    for k, code in (("y10", "KR10YT=RR"), ("y3", "KR3YT=RR")):
        try:
            nv[k] = naver_bond(code)
        except Exception as exc:  # noqa: BLE001
            nv[k] = {}
            errors.append(f"네이버 {code}: {type(exc).__name__}")
    try:
        nv["aa"] = naver_aa()
    except Exception as exc:  # noqa: BLE001
        nv["aa"] = {}
        errors.append(f"네이버 AA-: {type(exc).__name__}")
    hist = state.setdefault("hist", {})
    for k in ECOS_ITEMS:
        for d, v in ec[k].items():
            hist.setdefault(d, {})[k] = v
    keep = int(cfg["spread"]["history_keep"])
    state["hist"] = hist = dict(sorted(hist.items())[-keep:])
    if errors:
        print("errors: " + " | ".join(errors), file=sys.stderr)

    def close(k: str, d: str) -> tuple[float | None, str | None]:
        if d in ec.get(k, {}):
            return ec[k][d], "KOFIA 최종호가(한국은행 ECOS)"
        if d in nv.get(k, {}):
            return nv[k][d], "로이터 시장 종가(네이버), ECOS 미반영"
        return None, None

    sched, sched_notes = mpc_schedule(cfg, sess.year)
    nxt_list = [d for d in sched if d > sess.isoformat()]
    nxt = nxt_list[0] if nxt_list else f"미정({', '.join(sched_notes) or '일정 없음'})"
    y10s, y3s = close("y10", sess.isoformat()), close("y3", sess.isoformat())
    print(f"y10 {sess}: {y10s} | y3: {y3s} | ECOS last y10={max(ec['y10']) if ec['y10'] else None} "
          f"aa={max(ec['aa']) if ec['aa'] else None} | naver last y10={max(nv['y10']) if nv['y10'] else None} "
          f"aa={max(nv['aa']) if nv['aa'] else None} | next MPC {nxt} {sched_notes}")

    seeding = not state.get("seeded")
    cap = int(cfg["catchup_trading_days"])

    # --- 1) 국고10년 ---
    st = state["y10"]
    for d in trading_days_between(st.get("last_day"), sess, cap):
        v, src = close("y10", d)
        if v is None:
            if d == sess.isoformat() and late:
                ops_once("y10", "[KR-RATE] 국고10년 조회 실패 | 조건 판정 못 함",
                         ops_body("국고채 10년 매수 조건 알림의 데이터 조회 실패 안내.",
                                  f"{d} 국고채 10년 종가 {NA} (ECOS, 네이버 모두 그날 값 없음. {' | '.join(errors)[:200]})."))
            break
        res = eval_y10(st, v, cfg)
        print(f"y10 {d} {v} {src} -> {res} armed={st['armed']}")
        if res == "fire":
            st["fired_day"] = d
            if not seeding:
                pd = prev_trading_day(date.fromisoformat(d)).isoformat()
                out.append(y10_msg(cfg, stamp, d, v, src, (pd, close("y10", pd)[0]), close("y3", d)[0], nxt))
        st["last_day"] = d

    # --- 3) 신용 스프레드 (ECOS 만, 같은 출처) ---
    sst = state["spread"]
    lb = int(cfg["spread"]["lookback_obs"])
    now_sp = None
    sdays = trading_days_between(sst.get("last_day"), sess, cap)
    if not sst.get("seeded"):  # 첫 판정은 계산되는 가장 최근 하루만 (지난 날짜로 울리지 않게)
        sdays = [d for d in sdays if spread_widen(hist, d, lb) is not None][-1:]
    for d in sdays:
        r = spread_widen(hist, d, lb)
        if r is None:
            break
        sp, w, b, bsp = r
        now_sp = (d, sp, w, b, bsp)
        if not sst.get("seeded"):
            sst["armed"] = w <= cfg["spread"]["widen_trigger_bp"]
            sst["seeded"] = True
            res = "seed"
        else:
            res = eval_spread(sst, w, cfg)
        print(f"spread {d} {sp:.1f}bp widen {w:+.1f}bp vs {b} -> {res} armed={sst['armed']}")
        if res == "fire":
            sst["fired_day"] = d
            if not seeding:
                out.append(spread_msg(cfg, stamp, d, hist[d]["aa"], hist[d]["y3"], sp, w, b, bsp))
        aa_n = nv.get("aa", {}).get(d)
        if aa_n is not None and abs(aa_n - hist[d]["aa"]) > 0.0015:
            print(f"note: AA- ECOS {hist[d]['aa']} vs 네이버 {aa_n} ({d})")
        sst["last_day"] = d
    if sst.get("last_day"):
        lag = len(trading_days_between(sst["last_day"], sess, 10)) - 1
        if lag >= 2 and late:
            ops_once("spread", "[KR-RATE] 신용 스프레드 조회 실패 | 판정 밀림",
                     ops_body("회사채 신용 스프레드 알림의 데이터 지연 안내.",
                              f"ECOS 회사채 AA-/국고3년 값이 {sst['last_day']} 이후 {lag}거래일째 없음 ({' | '.join(errors)[:200]})."))
    elif late and not hist:
        ops_once("spread", "[KR-RATE] 신용 스프레드 조회 실패 | 판정 못 함",
                 ops_body("회사채 신용 스프레드 알림의 데이터 조회 실패 안내.", f"ECOS 시장금리 {NA} ({' | '.join(errors)[:200]})."))

    # --- 2a) 금통위 결정일 ---
    done = set(state.get("mpc_done") or [])
    for d in [x for x in sched if x <= sess.isoformat() and x not in done]:
        age = (sess - date.fromisoformat(d)).days
        if seeding or age > int(cfg["mpc"]["retry_days"]):
            done.add(d)
            continue
        if not is_trading_day(date.fromisoformat(d)):
            continue
        dec = mpc_decision(cfg, d)
        print(f"mpc {d}: {dec}")
        if dec.get("status") != "ok":
            ops_once(f"mpc_{d}", "[KR-RATE] 금통위 결정 확인 불가 | 요약 못 보냄",
                     ops_body("금통위 결정일 요약의 데이터 확인 실패 안내.",
                              f"{d} 기준금리 결정 {NA} ({dec.get('why')}). 다음 실행 때 다시 봅니다({cfg['mpc']['retry_days']}일 안)."))
            continue
        pd = prev_trading_day(date.fromisoformat(d)).isoformat()
        y = {}
        for k in ("y3", "y10"):
            v, src = close(k, d)
            y[k] = (v, close(k, pd)[0], src)
        out.append(mpc_msg(stamp, d, dec, y, nxt))
        done.add(d)
    state["mpc_done"] = sorted(done)[-24:]

    # --- 2b) 근원물가 ---
    try:
        c = cpi_yoy(sess.strftime("%Y-%m"))
        print(f"cpi {c} last_reported={state.get('cpi_last_month')}")
        if c["qb"] is None:
            raise RuntimeError(f"{c['month']} 전년동월비 계산 못 함")
        if state.get("cpi_last_month") is None or c["month"] > state["cpi_last_month"]:
            if not seeding and state.get("cpi_last_month") is not None:
                out.append(cpi_msg(cfg, stamp, c))
            state["cpi_last_month"] = c["month"]
    except Exception as exc:  # noqa: BLE001
        print(f"cpi failed: {exc}", file=sys.stderr)
        if late:
            ops_once("cpi", "[KR-RATE] 근원물가 조회 실패 | 새 달 수치 판정 못 함",
                     ops_body("근원물가 알림의 데이터 조회 실패 안내.", f"ECOS 소비자물가지수(특수분류) {NA} ({str(exc)[:200]})."))

    if now_sp:
        print(f"spread now {now_sp[0]} {now_sp[1]:.1f}bp, 20obs widen {now_sp[2]:+.1f}bp vs {now_sp[3]} ({now_sp[4]:.1f}bp)")
    if seeding:
        state["seeded"] = True
        print("SEED RUN: conditions evaluated, nothing sent")
    for title, msg, pri, tags in out:
        print(f"TITLE: {title}\nPRIORITY: {pri} TOPIC: {route_topic(topic, title)}\n{msg}\n")
        if not dry:
            publish(topic, title, msg, pri, tags, server)
    if not out:
        print("no alert")
    if dry:
        print("DRY RUN: no publish, state not written")
        return 0
    state["updated_at"] = now.isoformat()
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


def selftest(cfg: dict) -> int:
    """가짜 값으로 조건과 문구만 검사 (네트워크, ntfy, state 없음)."""
    stamp = "2026-11-02 19:00 KST"
    st = {"armed": True}
    seq = [(4.30, None), (4.36, "fire"), (4.40, None), (4.31, None), (4.29, "rearm"), (4.34, None), (4.35, "fire")]
    for v, want in seq:
        got = eval_y10(st, v, cfg)
        assert got == want, (v, got, want)
    print("y10 ok:", seq)
    hist = {}
    d0 = date(2026, 9, 1)
    days = []
    d = d0
    while len(days) < 26:
        if is_trading_day(d):
            days.append(d.isoformat())
        d += timedelta(days=1)
    for i, dd in enumerate(days):
        hist[dd] = {"y3": 3.80, "aa": 4.45 + (0.25 if i >= 21 else 0.0) + (-0.20 if i >= 24 else 0.0)}
    sst = {"armed": True}
    res = []
    for dd in days[20:]:
        sp, w, b, bsp = spread_widen(hist, dd, 20)
        res.append((dd, round(w, 1), eval_spread(sst, w, cfg)))
    assert [r[2] for r in res] == [None, "fire", None, None, "rearm", None], res
    print("spread ok:", res)
    assert spread_widen(hist, days[5], 20) is None
    c = {"month": "2026-10", "qb": 2.4, "qb_prev": 2.7, "db": 2.6}
    t, m, p, tg = cpi_msg(cfg, stamp, c)
    assert p == 4 and tg == ["rotating_light"] and "2.5% 미만" in t
    t2, _, p2, _ = cpi_msg(cfg, stamp, {"month": "2026-10", "qb": 2.5, "qb_prev": 2.7, "db": 2.6})
    assert p2 == 3 and "미만" not in t2
    samples = [y10_msg(cfg, stamp, "2026-11-02", 4.361, "KOFIA 최종호가(한국은행 ECOS)", ("2026-10-30", 4.312), 3.951, "2026-11-26"),
               spread_msg(cfg, stamp, "2026-11-02", 4.75, 3.80, 95.0, 25.0, "2026-10-02", 70.0),
               mpc_msg(stamp, "2026-11-26", {"decision": "hold", "rate": 3.0, "prev": 3.0, "source": "한은 통화정책방향 보도자료",
                                            "dissent": "만장일치", "naver": ("2026-11-26", 3.0)},
                       {"y3": (3.85, 3.87, "KOFIA 최종호가(한국은행 ECOS)"), "y10": (4.20, None, "로이터 시장 종가(네이버), ECOS 미반영")}, "2027 일정 미발표"),
               (t, m, p, tg)]
    for t, m, p, tg in samples:
        assert "\u00b7" not in t + m and "\u2014" not in t + m
        assert route_topic("cwally-port", t) == "cwally-port", t
        print(f"-- sample (가짜 숫자)\nTITLE: {t}\nPRIORITY: {p} TAGS: {tg}\n{m}\n")
    assert route_topic("cwally-port", "[KR-RATE] 국고10년 조회 실패 | 조건 판정 못 함") == "cwally-ops"
    print("SELFTEST OK: no network, no publish, no state")
    return 0


if __name__ == "__main__":
    sys.exit(main())
