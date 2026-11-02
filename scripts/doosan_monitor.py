#!/usr/bin/env python3
"""두산 4종목(000150, 034020, 241560, 131970) 가격, 외인 수급, 3분기 실적 알림 (2026-10-19 승인, 추천안 B).

  doosan_monitor.py slot     port-alerts morning 평일 09:00. 전 거래일 KRX 종가(다음)와 확정 수급(네이버 주식 수,
                             다음 부호 확인)으로 새 조건만 보낸다. 월요일(그 주 첫 거래일)은 주간 요약을 붙인다.
                             실적 예고 공시 탐지, D-3/D-1/당일 안내, 컨센서스 고정 저장, 놓친 실적 결과 보충.
  doosan_monitor.py auto     one-shot 17:05 (10/26~11/16 평일). 오늘 접수된 3분기 실적만 컨센서스와 비교해 보낸다.
                             없으면 침묵.
  doosan_monitor.py dry      slot + auto 판정을 출력만 (ntfy 없음, state 쓰기 없음). 2분기 공시로 파서 자체 점검.
                             DOOSAN_NOW="YYYY-MM-DD HH:MM" 으로 시각을 바꿔 볼 수 있다.
  doosan_monitor.py force    slot 판정을 같은 날 다시 보낸다 (수동 실행은 채팅 승인 필요).
  doosan_monitor.py install  설치 확인 1회 발송 (AGENTS.md), state 쓰기 없음.
  doosan_monitor.py selftest 오프라인 점검: 모든 조회를 고정 자료로 대체, 네트워크, ntfy, state 파일 없음.

금액 = 순매수 주식 수 x KRX 종가 추정. 매매 신호가 아니다.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic, route_topic  # noqa: E402
from naver_api import html_text, stock_trend  # noqa: E402
import kr_calendar  # noqa: E402

KST = timezone(timedelta(hours=9))
CFG_PATH = ROOT / "configs" / "doosan.json"
STATE_PATH = ROOT / "state" / "doosan.json"
UA = "Mozilla/5.0 (cwally-alerts; +mailto:sosmos3218@gmail.com)"
EOK = 100_000_000
UNIT_TO_EOK = {"백만원": 100.0, "억원": 1.0, "천원": 100000.0, "조원": 0.0001}
DART_URL = "https://dart.fss.or.kr/dsab007/detailSearch.ax"


# ---------- io ----------

def load_json(path: Path) -> dict:
    try:
        v = json.loads(path.read_text(encoding="utf-8"))
        return v if isinstance(v, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save(state: dict, now: datetime, dry: bool) -> None:
    if dry:
        print("DRY RUN: state not written")
        return
    state["updated_at"] = now.strftime("%Y-%m-%d %H:%M KST")
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def http(url: str, data: bytes | None = None, referer: str | None = None, tries: int = 4) -> str:
    headers = {"User-Agent": UA}
    if referer:
        headers["Referer"] = referer
    if data is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    err: Exception | None = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
            with urllib.request.urlopen(req, timeout=20) as resp:
                return resp.read().decode("utf-8", errors="ignore")
        except Exception as exc:  # Daum TLS가 가끔 끊긴다: 재시도
            err = exc
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"{url.split('?')[0]}: {err}")


def send(cfg: dict, title: str, body: str, pri: int, dry: bool, ops: bool = False) -> None:
    topic = resolve_topic("ops" if ops else "port", cfg.get("ntfy_topic"))
    print(f"TITLE: {title}\nPRIORITY: {pri}\nTOPIC: {route_topic(topic, title)}\n{body}")
    if dry:
        print("DRY RUN: no ntfy sent")
        return
    publish(topic, title[:180], body, pri, ["office"], resolve_server(cfg.get("ntfy_server")))


def eok(v: float | None) -> str:
    return "?" if v is None else f"{v:+,.0f}억"


def won(v: float | None) -> str:
    return "?" if v is None else f"{v:,.0f}원"


def md(day: str) -> str:
    return f"{int(day[4:6])}/{int(day[6:8])}"


def trading_days_until(today: date, target: date) -> int:
    if target < today:
        return -1
    n, d = 0, today
    while d < target:
        d += timedelta(days=1)
        if kr_calendar.is_trading_day(d):
            n += 1
    return n


# ---------- 시세, 수급 ----------

def daum_days(cfg: dict, code: str) -> dict:
    raw = json.loads(http(cfg["sources"]["krx_days"].format(code=code), referer="https://finance.daum.net/"))
    out = {}
    for r in raw.get("data") or []:
        d = str(r.get("date") or "")[:10].replace("-", "")
        if d and r.get("tradePrice") is not None:
            out[d] = {
                "close": float(r["tradePrice"]),
                "prev": float(r.get("prevClosingPrice") or 0) or None,
                "chg": float(r.get("changeRate") or 0) * 100,
            }
    return out


def daum_investor(cfg: dict, code: str) -> dict:
    raw = json.loads(http(cfg["sources"]["investor_check"].format(code=code), referer="https://finance.daum.net/"))
    return {
        str(r.get("date") or "")[:10].replace("-", ""): r.get("foreignStraightPurchaseVolume")
        for r in raw.get("data") or []
    }


def naver_last_close(code: str) -> float | None:
    raw = json.loads(http(f"https://m.stock.naver.com/api/stock/{code}/integration"))
    for it in raw.get("totalInfos") or []:
        if it.get("code") == "lastClosePrice":
            try:
                return float(str(it.get("value")).replace(",", ""))
            except ValueError:
                return None
    return None


def ticker_data(cfg: dict, t: dict, d0: str, now: datetime) -> dict:
    code = t["code"]
    out = {"code": code, "name": t["name"], "short": t["short"], "notes": [], "rows": [], "ok": False}
    days = daum_days(cfg, code)
    if d0 not in days:
        raise RuntimeError(f"{t['name']} {md(d0)} KRX 종가 없음(다음)")
    trend = {r["day"]: r for r in stock_trend(code, 20)}
    rows = []
    for d in sorted(days):
        if d > d0:
            continue
        tr = trend.get(d)
        c = days[d]["close"]
        f = o = None
        if tr and tr.get("foreign") is not None:
            f = tr["foreign"] * c / EOK
            o = (tr.get("organ") or 0) * c / EOK
        rows.append({"day": d, "close": c, "chg": days[d]["chg"], "f": f, "o": o})
    out["rows"] = rows
    out["flow_ok"] = bool(rows and rows[-1]["f"] is not None)
    if not out["flow_ok"]:
        out["notes"].append(f"{t['short']} {md(d0)} 수급 미게시")
    closes = [r["close"] for r in rows]
    out["ma5"] = sum(closes[-5:]) / 5 if len(closes) >= 5 else None
    out["ma20"] = sum(closes[-20:]) / 20 if len(closes) >= 20 else None
    # 네이버 전일가 교차확인: 오늘 이후 시점이면 d0 종가, d0 당일 저녁이면 d0 전일 종가가 네이버 기준가다.
    try:
        lc = naver_last_close(code)
        expect = days[d0]["close"] if now.strftime("%Y%m%d") > d0 else days[d0]["prev"]
        if lc is not None and expect and abs(lc - expect) > 0.5:
            out["notes"].append(f"{t['short']} 종가 교차확인 차이(다음 {won(expect)}, 네이버 기준가 {won(lc)})")
    except Exception as exc:
        out["notes"].append(f"{t['short']} 네이버 기준가 교차확인 생략({exc.__class__.__name__})")
    # 다음 수급 부호 확인
    out["sign_ok"] = True
    if out["flow_ok"]:
        try:
            dv = daum_investor(cfg, code).get(d0)
            nv = trend[d0]["foreign"]
            if dv is not None and nv and dv and (dv > 0) != (nv > 0):
                out["sign_ok"] = False
                out["notes"].append(f"{t['short']} 외인 부호 불일치(네이버 {nv:+,.0f}주, 다음 {dv:+,.0f}주), 수급 조건 보류")
        except Exception as exc:
            out["notes"].append(f"{t['short']} 다음 수급 교차확인 생략({exc.__class__.__name__})")
    out["ok"] = True
    return out


def judge(t: dict, td: dict, rules: dict, d0: str) -> list[dict]:
    """처음부터 순서대로 걸린 상태를 다시 계산해 d0에 새로 걸린 조건만 돌려준다 (state 없이 결정적)."""
    p1, p2 = rules["price_pct"], rules["price_strong_pct"]
    thr, c5, strong = t["foreign_day_eok"], rules["foreign_5d_eok"], rules["foreign_strong_eok"]
    half, streak_n = rules["rearm_ratio"], int(rules["buy_streak_days"])
    fired = {"pu": 0, "pd": 0, "fb": 0, "fs": 0, "cb": 0, "cs": 0, "st": 0}
    hist: list[float] = []
    events: list[dict] = []
    for r in td["rows"]:
        today: list[dict] = []
        c = r["chg"]
        if c < 0 or abs(c) < p1 * half:
            fired["pu"] = 0
        if c > 0 or abs(c) < p1 * half:
            fired["pd"] = 0
        lvl = 2 if abs(c) >= p2 else (1 if abs(c) >= p1 else 0)
        key = "pu" if c > 0 else "pd"
        if lvl and lvl > fired[key]:
            fired[key] = lvl
            today.append({"kind": "price", "pri": 4 if lvl == 2 else 3,
                          "text": f"{td['short']} KRX 종가 {won(r['close'])} {c:+.2f}% (기준 {p2 if lvl == 2 else p1:.0f}%)",
                          "head": f"{td['short']} {c:+.1f}%"})
        a = r["f"]
        flow_ok = a is not None and (r["day"] != d0 or td.get("sign_ok", True))
        if flow_ok:
            hist.append(a)
            if a < 0 or abs(a) < thr * half:
                fired["fb"] = 0
            if a > 0 or abs(a) < thr * half:
                fired["fs"] = 0
            k = "fb" if a > 0 else "fs"
            if abs(a) >= thr and not fired[k]:
                fired[k] = 1
                word = "순매수" if a > 0 else "순매도"
                today.append({"kind": "flow", "pri": 4 if abs(a) >= strong else 3,
                              "text": f"{td['short']} 외인 하루 {word} {eok(a)} (기준 {thr}억), 기관 {eok(r['o'])}",
                              "head": f"{td['short']} 외인 {eok(a)}"})
            if len(hist) >= 5:
                cum = sum(hist[-5:])
                if cum < 0 or abs(cum) < c5 * half:
                    fired["cb"] = 0
                if cum > 0 or abs(cum) < c5 * half:
                    fired["cs"] = 0
                k = "cb" if cum > 0 else "cs"
                if abs(cum) >= c5 and not fired[k]:
                    fired[k] = 1
                    word = "순매수" if cum > 0 else "순매도"
                    today.append({"kind": "flow5", "pri": 3,
                                  "text": f"{td['short']} 외인 5일 누적 {word} {eok(cum)} ({c5}억 돌파)",
                                  "head": f"{td['short']} 외인 5일 {eok(cum)}"})
            if a <= 0:
                fired["st"] = 0
            if len(hist) >= streak_n and all(x > 0 for x in hist[-streak_n:]) and not fired["st"]:
                fired["st"] = 1
                today.append({"kind": "streak", "pri": 3,
                              "text": f"{td['short']} 외인 {streak_n}거래일 연속 순매수 (합 {eok(sum(hist[-streak_n:]))})",
                              "head": f"{td['short']} 외인 {streak_n}일 연속 매수"})
        if r["day"] == d0:
            events = today
    return events


def digest_line(td: dict) -> str:
    r = td["rows"][-1]
    ma20 = td.get("ma20")
    pos = "" if not ma20 else (" | 20일선 위" if r["close"] >= ma20 else " | 20일선 아래")
    fr = [x for x in td["rows"] if x["f"] is not None][-5:]
    upto = "" if not fr or fr[-1]["day"] == r["day"] else f"({md(fr[-1]['day'])}까지)"
    f5 = f" | 외인 5일{upto} {eok(sum(x['f'] for x in fr))}" if len(fr) == 5 else ""
    flow = f" | 외인 {eok(r['f'])} 기관 {eok(r['o'])}" if r["f"] is not None else " | 수급 미게시"
    return f"{td['name']} {won(r['close'])} {r['chg']:+.2f}%{pos}{flow}{f5}"


# ---------- 실적 ----------

def nums_after(text: str, label: str) -> list[float]:
    m = re.search(label + r"\s*당해실적\s*(.*?)누계실적", text)
    if not m:
        return []
    return [float(x.replace(",", "")) for x in re.findall(r"-?\d[\d,]*(?:\.\d+)?", m.group(1))]


def parse_result(text: str, period_end: str) -> dict | None:
    period_ok = period_end in text or bool(re.search(r"3분기|3Q", text))
    um = re.search(r"단위\s*[:：]\s*(조원|억원|백만원|천원)", text or "")
    if not period_ok or not um:
        return None
    div = UNIT_TO_EOK[um.group(1)]
    rev, op = nums_after(text, "매출액"), nums_after(text, "영업이익")
    if not op:
        return None
    return {
        "rev": round(rev[0] / div) if rev else None,
        "op": round(op[0] / div),
        "op_yoy_base": round(op[3] / div) if len(op) >= 5 else None,
    }


_NOTICE_CACHE: dict = {}


def notices(code: str, size: int = 30) -> list[dict]:
    """naver_api.notices 와 같은 형식, 재시도와 실행 중 캐시 포함 (응답이 커서 가끔 IncompleteRead)."""
    if (code, size) in _NOTICE_CACHE:
        return _NOTICE_CACHE[(code, size)]
    raw = json.loads(http(f"https://stock.naver.com/api/domestic/detail/notice?itemCode={code}&page=1&pageSize={size}"))
    out = [{"title": str(r.get("title") or "").strip(), "datetime": str(r.get("datetime") or ""),
            "text": html_text(str(r.get("contents") or ""))} for r in raw if isinstance(r, dict)] if isinstance(raw, list) else []
    _NOTICE_CACHE[(code, size)] = out
    return out


def scan_notices(code: str, ecfg: dict) -> dict:
    """결산실적공시 예고 / 3분기 IR / 3분기 영업(잠정)실적."""
    out: dict = {}
    for r in notices(code, 30):
        title, day = r.get("title", ""), str(r.get("datetime", ""))[:10]
        if day < ecfg["since"] or "정정" in title:
            continue
        text = r.get("text", "")
        if "결산실적공시" in title and "예고" in title and "date" not in out:
            m = re.search(r"종료일\s*(\d{4}-\d{2}-\d{2}).*?공시예정일\s*(\d{4}-\d{2}-\d{2})", text)
            if m and m.group(1) == ecfg["period_end"]:
                out["date"], out["date_src"] = m.group(2), f"결산실적공시 예고 {day}"
        elif "기업설명회" in title and "call" not in out:
            m = re.search(r"일시\s*(\d{4}-\d{2}-\d{2})\s*(\d{2}:\d{2})", text)
            pm = re.search(r"개최목적\s*(.*?)\s*4\.\s*개최방법\s*(.*?)\s*5\.", text)
            purpose, method = (pm.group(1), pm.group(2)) if pm else ("", "")
            if m and "3분기" in purpose and ("실적" in purpose or "컨퍼런스" in method) and "Non-Deal" not in purpose:
                out["call"], out["call_date"] = m.group(2), m.group(1)
        elif "영업(잠정)실적" in title and "result" not in out:
            got = parse_result(text, ecfg["period_end"])
            if got:
                got["day"] = day
                out["result"] = got
    return out


def dart_quarterly(ecfg: dict, now: datetime, kind: str = "분기보고서") -> dict | None:
    """DART 공시검색(조회 전용)에서 테스나 3분기 분기보고서."""
    form = urllib.parse.urlencode([
        ("currentPage", "1"), ("maxResults", "15"), ("textCrpNm", ecfg["dart_name"]),
        ("startDate", ecfg["since"].replace("-", "")), ("endDate", now.strftime("%Y%m%d")),
        ("publicType", "A002"), ("publicType", "A003"),
    ]).encode()
    page = http(DART_URL, data=form)
    for row in re.findall(r"(?s)<tr>(.*?)</tr>", page):
        text = html_text(row)
        if kind in text and ecfg["period_end"][:7].replace("-", ".") in text and "정정" not in text:
            rcp = re.search(r"rcpNo=(\d+)", row)
            dm = re.findall(r"(\d{4})\.(\d{2})\.(\d{2})", text)
            day = "-".join(dm[-1]) if dm else None
            return {"day": day, "rcp": rcp.group(1) if rcp else None}
    return None


def naver_quarter(code: str, key: str) -> dict | None:
    raw = json.loads(http(f"https://m.stock.naver.com/api/stock/{code}/finance/quarter"))
    fi = raw.get("financeInfo") or {}
    cons = {t.get("key"): t.get("isConsensus") for t in fi.get("trTitleList") or []}
    if key not in cons:
        return None
    vals = {}
    for row in fi.get("rowList") or []:
        if row.get("title") in ("매출액", "영업이익"):
            v = ((row.get("columns") or {}).get(key) or {}).get("value")
            try:
                vals["rev" if row["title"] == "매출액" else "op"] = float(str(v).replace(",", ""))
            except (TypeError, ValueError):
                pass
    if "op" not in vals:
        return None
    vals["is_consensus"] = cons[key] == "Y"
    return vals


def pct(a: float | None, b: float | None) -> float | None:
    if a is None or not b:
        return None
    return (a / b - 1) * 100


def result_block(cfg: dict, code: str, res: dict, cons: dict | None) -> tuple[str, int, str]:
    ecfg = cfg["earnings"]
    name = next(t["name"] for t in cfg["tickers"] if t["code"] == code)
    short = next(t["short"] for t in cfg["tickers"] if t["code"] == code)
    dop = pct(res.get("op"), (cons or {}).get("op"))
    drev = pct(res.get("rev"), (cons or {}).get("rev"))
    pri = 4 if dop is not None and abs(dop) >= ecfg["op_dev_pct"] else 3
    cons_txt = lambda v, d: "" if v is None else f" (컨센서스 {v:,.0f}억, {d:+.1f}%)" if d is not None else f" (컨센서스 {v:,.0f}억)"
    line = (f"{name} 3분기 매출 {res['rev']:,.0f}억{cons_txt((cons or {}).get('rev'), drev)}, "
            f"영업이익 {res['op']:,.0f}억{cons_txt((cons or {}).get('op'), dop)}") if res.get("rev") is not None else \
           f"{name} 3분기 영업이익 {res['op']:,.0f}억{cons_txt((cons or {}).get('op'), dop)}"
    if res.get("op_yoy_base"):
        line += f". 전년 동기 영업이익 {res['op_yoy_base']:,.0f}억"
    if cons and cons.get("as_of"):
        line += f". 컨센서스 {cons['as_of']} 기준"
    if code == ecfg["dssotp_code"]:
        line += ". 두산은 요약만, SOTP 표는 [DSSOTP] 알림 참고"
    head = f"{short} 3Q 영업익 {res['op']:,.0f}억" + (f" 컨센 대비 {dop:+.0f}%" if dop is not None else "")
    return line, pri, head


def find_results(cfg: dict, state: dict, now: datetime, only_today: bool) -> list[dict]:
    ecfg = cfg["earnings"]
    today = kr_calendar.session_day(now).isoformat()
    found = []
    for t in cfg["tickers"]:
        code = t["code"]
        es = state.setdefault("earn", {}).setdefault(code, {})
        if es.get("result_sent"):
            continue
        res = None
        if code == ecfg["dart_code"]:
            doc = dart_quarterly(ecfg, now)
            if doc:
                es["filed"] = doc
                act = naver_quarter(code, ecfg["period_end"][:7].replace("-", ""))
                res = {"day": doc["day"], "rcp": doc.get("rcp"), "dart": True}
                if act and not act["is_consensus"]:
                    res.update(rev=act.get("rev"), op=act["op"])
        else:
            res = scan_notices(code, ecfg).get("result")
        if not res or (only_today and res.get("day") != today):
            continue
        found.append({"code": code, "res": res})
    return found


def results_message(cfg: dict, state: dict, found: list[dict], final: bool) -> tuple[list[str], int, list[str], list[str]]:
    lines, heads, sent_codes, pri = [], [], [], 3
    for f in found:
        code, res = f["code"], f["res"]
        cons = (state.get("earn", {}).get(code, {}) or {}).get("cons") or cfg["earnings"]["consensus_seed"].get(code)
        if res.get("dart") and res.get("op") is None:
            if not final:
                print(f"{code} 분기보고서 접수, 숫자 반영 전: 다음 09:00 재시도")
                continue
            name = next(t["name"] for t in cfg["tickers"] if t["code"] == code)
            lines.append(f"{name} 3분기 분기보고서 접수 {res['day']}. 숫자는 아직 반영 전, DART 원문 확인: "
                         f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={res.get('rcp')}")
            heads.append(f"{next(t['short'] for t in cfg['tickers'] if t['code'] == code)} 분기보고서 접수")
        else:
            line, p, head = result_block(cfg, code, res, cons)
            if res.get("dart") and res.get("rcp"):
                line += f". DART https://dart.fss.or.kr/dsaf001/main.do?rcpNo={res['rcp']}"
            lines.append(line)
            heads.append(head)
            pri = max(pri, p)
        sent_codes.append(code)
    return lines, pri, heads, sent_codes


def refresh_consensus(cfg: dict, state: dict, notes: list[str]) -> None:
    key = cfg["earnings"]["period_end"][:7].replace("-", "")
    for t in cfg["tickers"]:
        es = state.setdefault("earn", {}).setdefault(t["code"], {})
        if es.get("result_sent"):
            continue
        try:
            q = naver_quarter(t["code"], key)
        except Exception as exc:
            notes.append(f"{t['short']} 컨센서스 갱신 생략({exc.__class__.__name__})")
            q = None
        if q and q["is_consensus"]:
            es["cons"] = {"rev": q.get("rev"), "op": q["op"], "as_of": datetime.now(KST).strftime("%Y-%m-%d")}
        elif not es.get("cons"):
            es["cons"] = cfg["earnings"]["consensus_seed"].get(t["code"])


def earnings_events(cfg: dict, state: dict, now: datetime, notes: list[str]) -> list[dict]:
    ecfg = cfg["earnings"]
    today = now.date()
    events = []
    for t in cfg["tickers"]:
        code, short = t["code"], t["short"]
        es = state.setdefault("earn", {}).setdefault(code, {})
        if es.get("result_sent"):
            continue
        known = ecfg.get("known", {}).get(code)
        if code == ecfg["dart_code"]:
            if not es.get("date"):
                es.update(date=ecfg["dart_deadline"], src="법정 기한(예상)", seen=True)
        else:
            try:
                sc = scan_notices(code, ecfg)
            except Exception as exc:
                notes.append(f"{short} 공시 조회 생략({exc.__class__.__name__})")
                sc = {}
            if sc.get("result"):
                continue  # 결과는 별도 블록
            if known and not es.get("date"):
                es.update(date=known["date"], call=known.get("call"), src=known.get("source"), seen=True)
            if sc.get("date") and sc["date"] != es.get("date"):
                es.update(date=sc["date"], src=sc["date_src"], seen=False)
            if sc.get("call") and sc.get("call_date") == es.get("date"):
                es["call"] = sc["call"]
        if not es.get("date"):
            continue
        target = date.fromisoformat(es["date"])
        n = trading_days_until(today, target)
        if n < 0:
            continue
        call = f", 콜 {es['call']}" if es.get("call") else ""
        expect = " 예상(법정 기한, 예년엔 기한일 제출)" if code == ecfg["dart_code"] else ""
        what = "분기보고서" if code == ecfg["dart_code"] else "3분기 잠정실적"
        label = {3: "D-3", 1: "D-1", 0: "당일"}.get(n)
        sent = es.setdefault("reminders", [])
        if not es.get("seen"):
            es["seen"] = True
            tag = label if label and label not in sent else None
            if tag:
                sent.append(tag)
            events.append({"kind": "earn", "pri": 3,
                           "text": f"{t['name']} {what} 발표일 새로 확인: {target.month}/{target.day}{call} ({es.get('src')}). "
                                   + (f"오늘이 {label}." if label else f"{n}거래일 남음."),
                           "head": f"{short} 실적 {target.month}/{target.day} 확인", "n": n})
            continue
        if label and label not in sent:
            sent.append(label)
            when = "오늘" if n == 0 else ("다음 거래일" if n == 1 else f"{n}거래일 뒤")
            events.append({"kind": "earn", "pri": 3,
                           "text": f"{t['name']} {what} {label}: {when} {target.month}/{target.day}{call}{expect}. "
                                   + cons_line(es.get("cons")),
                           "head": f"{short} 실적 {label}", "n": n})
    return events


def cons_line(cons: dict | None) -> str:
    if not cons:
        return "컨센서스 없음."
    rev = f"매출 {cons['rev']:,.0f}억, " if cons.get("rev") is not None else ""
    return f"컨센서스 {rev}영업이익 {cons['op']:,.0f}억 ({cons.get('as_of')} 기준)."


# ---------- 실행 ----------

def judge_day(now: datetime) -> date:
    today = now.date()
    if kr_calendar.is_trading_day(today) and now.hour >= 18:
        return today
    return kr_calendar.prev_trading_day(today)


def guard(cfg: dict, now: datetime) -> str | None:
    if now.year != int(cfg.get("year_guard") or 2026):
        return "year-guard"
    end = cfg.get("ends_kst")
    if end and now > datetime.strptime(end, "%Y-%m-%d %H:%M").replace(tzinfo=KST) + timedelta(minutes=59):
        return "ended"
    if not kr_calendar.is_trading_day(kr_calendar.session_day(now)):
        return "KRX 휴장/주말"
    return None


def fail_once(cfg: dict, state: dict, now: datetime, what: str, dry: bool) -> None:
    day = now.strftime("%Y-%m-%d")
    if state.get("fail_day") == day:
        print(f"fail already notified today: {what}")
        return
    state["fail_day"] = day
    send(cfg, "[DOOSAN] 000150/034020/241560/131970 조회 실패 | 오늘 판정 보류",
         "\n".join([
             "이게 뭐 알림: 두산 4종목 알림의 데이터 조회 실패 안내입니다. 매매 신호가 아닙니다.",
             f"지금 결과/상태: {what}",
             "지금 할 일: 없음. 조회 실패를 조건 미충족이나 호재로 읽지 마세요.",
             "나중에: 다음 슬롯에서 자동 재시도합니다.",
             "익절/줄일 때: 해당 없음.",
             "하지 말 것: 이 안내로 매매 판단. 투자 권유 아님.",
         ]), 3, dry, ops=True)


def run_slot(cfg: dict, state: dict, now: datetime, mode: str) -> None:
    dry = mode == "dry"
    d0 = judge_day(now).strftime("%Y%m%d")
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    notes: list[str] = []
    events: list[dict] = []
    data: dict = {}
    repeat = state.get("last_day") == d0 and mode not in ("force", "dry")
    failed = []
    for t in cfg["tickers"]:
        try:
            data[t["code"]] = ticker_data(cfg, t, d0, now)
        except Exception as exc:
            failed.append(f"{t['name']}: {exc}")
    if len(failed) == len(cfg["tickers"]):
        fail_once(cfg, state, now, f"{md(d0)} 4종목 모두 조회 실패. " + " / ".join(failed)[:400], dry)
        save(state, now, dry)
        return
    notes += [f"{x} 조회 실패" for x in failed]
    if not repeat:
        for t in cfg["tickers"]:
            td = data.get(t["code"])
            if td:
                notes += td["notes"]
                events += judge(t, td, cfg["rules"], d0)
    else:
        print(f"{d0} 가격, 수급 조건은 이미 판정함; 실적 부분만 점검")
    refresh_consensus(cfg, state, notes)
    try:
        events += earnings_events(cfg, state, now, notes)
    except Exception as exc:
        notes.append(f"실적 일정 점검 생략({exc.__class__.__name__}: {exc})")
    res_lines, res_pri, res_heads, res_codes = [], 3, [], []
    try:
        found = find_results(cfg, state, now, only_today=False)
        res_lines, res_pri, res_heads, res_codes = results_message(cfg, state, found, final=True)
    except Exception as exc:
        notes.append(f"실적 결과 점검 생략({exc.__class__.__name__}: {exc})")
    week = now.strftime("%G-W%V")
    digest = state.get("digest_week") != week and not repeat
    if not events and not res_lines and not digest:
        print(f"{stamp} {md(d0)} doosan silent; no new condition")
        for n in notes:
            print("note:", n)
        if not repeat:
            state["last_day"] = d0
        save(state, now, dry)
        return
    heads = res_heads + [e["head"] for e in sorted(events, key=lambda e: -e["pri"])]
    pri = max([e["pri"] for e in events] + ([res_pri] if res_lines else []) + [2])
    kinds = {e["kind"] for e in events}
    if res_lines:
        action = "다음 날 시가와 수급 확인"
    elif "earn" in kinds and any(e.get("n", 9) <= 1 for e in events if e["kind"] == "earn"):
        action = "실적 전 신규 진입 보류"
    elif kinds & {"price", "flow", "flow5", "streak"}:
        action = "추격 금지, 확인만"
    elif "earn" in kinds:
        action = "일정만 확인"
    else:
        action = "관망"
    if not heads:
        heads = [f"주간 요약 {md(d0)}"]
    title = f"[DOOSAN] 000150/034020/241560/131970 {', '.join(heads[:2])}" + (f" 외 {len(heads) - 2}건" if len(heads) > 2 else "") + f" | {action}"
    body = [f"이게 뭐 알림: 두산 4종목(두산, 두산에너빌리티, 두산밥캣, 두산테스나) {md(d0)} 확정치 점검에서 새로 걸린 조건"
            + (", 주간 요약" if digest else "") + "입니다. 매매 신호가 아닙니다."]
    body.append(f"지금 결과/상태 ({md(d0)} KRX 종가, 확정 수급, 억원):")
    body += [f"- {x}" for x in res_lines]
    body += [f"- {e['text']}" for e in sorted(events, key=lambda e: -e["pri"])]
    if digest:
        body.append("주간 요약:")
        for t in cfg["tickers"]:
            td = data.get(t["code"])
            body.append(f"- {digest_line(td)}" if td else f"- {t['name']} 조회 실패")
        sched = []
        for t in cfg["tickers"]:
            es = state.get("earn", {}).get(t["code"], {})
            if es.get("result_sent"):
                sched.append(f"{t['short']} 발표 완료")
            elif es.get("date"):
                dd = date.fromisoformat(es["date"])
                sched.append(f"{t['short']} {dd.month}/{dd.day}" + (" 예상" if es.get("src", "").startswith("법정") else ""))
            else:
                sched.append(f"{t['short']} 예고 공시 없음")
        body.append("- 실적 일정: " + ", ".join(sched))
    if notes:
        body.append("- 참고: " + "; ".join(notes)[:500])
    body.append(f"- 데이터: KRX 종가(다음), 수급 네이버 순매수 주식 수(다음 부호 확인), 금액=순매수 주식 수 x KRX 종가 추정. 조회 {stamp}.")
    if res_lines:
        body.append("지금 할 일: 내일 KRX 시가와 외인 수급을 보고 판단하세요. 공시 직후 시간외 가격에 바로 반응하지 마세요.")
    elif "earn" in kinds:
        body.append("지금 할 일: 실적 발표 전 신규 진입은 보류하고 일정만 확인하세요.")
    elif kinds:
        body.append("지금 할 일: 다음 거래일에 같은 방향 수급이 이어지는지만 보세요.")
    else:
        body.append("지금 할 일: 없음. 조건 알림이 오면 그때 확인하세요.")
    body.append("나중에: 같은 종목, 같은 방향 조건은 한 번만 알립니다. 반대 방향이나 기준 절반 아래 날이 오면 다시 열립니다. "
                "실적 결과는 발표일 17:05(놓치면 다음 날 09:00)에 컨센서스와 비교해 옵니다.")
    body.append("익절/줄일 때: 보유 여부 미확인. 10% 이상 급락이나 외인 300억 이상 매도가 겹치면 비중을 다시 보세요.")
    body.append("하지 말 것: 하루 수급으로 추세 전환 판단. 시간외 NXT 가격을 KRX 종가로 보기. 추정 금액을 확정치로 쓰기. 신용, 미수. 투자 권유 아님.")
    send(cfg, title, "\n".join(body), pri, dry)
    if not repeat:
        state["last_day"] = d0
    if digest:
        state["digest_week"] = week
    for c in res_codes:
        state.setdefault("earn", {}).setdefault(c, {})["result_sent"] = True
    state["last_title"] = title
    save(state, now, dry)


def run_auto(cfg: dict, state: dict, now: datetime, dry: bool) -> None:
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    try:
        found = find_results(cfg, state, now, only_today=True)
    except Exception as exc:
        print(f"result check failed: {exc}; 다음 09:00 슬롯이 다시 확인")
        return
    lines, pri, heads, codes = results_message(cfg, state, found, final=False)
    if not lines:
        print(f"{stamp} doosan auto silent; 오늘 접수된 3분기 실적 없음")
        save(state, now, dry)
        return
    title = f"[DOOSAN] 000150/034020/241560/131970 {', '.join(heads[:2])}" + (f" 외 {len(heads) - 2}건" if len(heads) > 2 else "") + " | 다음 날 시가 확인"
    body = ["이게 뭐 알림: 두산 4종목 중 오늘 접수된 3분기 실적을 발표 전 고정한 컨센서스와 비교한 결과입니다. 매매 신호가 아닙니다.",
            "지금 결과/상태 (잠정치, 억원):"]
    body += [f"- {x}" for x in lines]
    body.append(f"- 데이터: KRX/DART 공시(네이버 공시 피드, DART 검색), 컨센서스 네이버 증권. 조회 {stamp}.")
    body.append("지금 할 일: 내일 KRX 시가와 외인 수급을 보고 판단하세요.")
    body.append("나중에: 확정 수급은 다음 날 09:00 알림에 반영됩니다.")
    body.append(f"익절/줄일 때: 영업이익이 컨센서스를 {cfg['earnings']['op_dev_pct']}% 넘게 밑돌면 비중을 다시 보세요. 보유 여부 미확인.")
    body.append("하지 말 것: 잠정치를 확정치로 보기. 공시 직후 시간외 가격 추격. 신용, 미수. 투자 권유 아님.")
    send(cfg, title, "\n".join(body), pri, dry)
    for c in codes:
        state.setdefault("earn", {}).setdefault(c, {})["result_sent"] = True
    save(state, now, dry)


def selftest(cfg: dict) -> None:
    """2분기 잠정실적 공시로 파서 점검 (출력만)."""
    print("--- selftest: 2분기 공시 파싱 ---")
    for code in ("000150", "034020", "241560"):
        try:
            for r in notices(code, 60):  # 2분기 공시까지 닿도록 60
                if "영업(잠정)실적" in r.get("title", "") and r.get("datetime", "")[:7] == "2026-07":
                    print(code, r["datetime"][:10], parse_result(r.get("text", "").replace("2분기", "3분기"), "2026-06-30"))
                    break
            else:
                print(code, "2분기 공시 없음")
        except Exception as exc:
            print(code, "selftest 실패", exc)
    try:
        print("DART 테스나 3분기 분기보고서:", dart_quarterly(cfg["earnings"], datetime.now(KST)))
        q2 = dict(cfg["earnings"], period_end="2026-06-30", since="2026-08-01")
        print("DART 테스나 반기보고서 점검(2026.06, 8/14 기대):", dart_quarterly(q2, datetime.now(KST), "반기보고서"))
    except Exception as exc:
        print("DART 점검 실패", exc)


# ---------- offline selftest (no network, no ntfy, no state file) ----------
# Added 2026-11-02 (user approval pt33u). `doosan_monitor.py selftest` stubs every fetch at urllib.request.urlopen
# with the fixtures below (Daum days/investor, Naver trend/integration/notice/quarter, DART search), blocks sockets,
# captures publish() and save(), and seeds state from {} + configs/doosan.json consensus_seed. It never reads or
# writes state/doosan.json. The live parser check stays in `dry` (selftest(cfg) above, unchanged).

def _st_days(closes_chg: list[tuple[str, float, float]]) -> dict:
    """[(YYYYMMDD, close, chg%)] -> Daum days JSON."""
    data = []
    for d, c, chg in closes_chg:
        prev = round(c / (1 + chg / 100), 2) if chg else c
        data.append({"date": f"{d[:4]}-{d[4:6]}-{d[6:]} 00:00:00", "tradePrice": c, "prevClosingPrice": prev, "changeRate": chg / 100})
    return {"data": list(reversed(data))}


def _st_trading_days(end: str, n: int) -> list[str]:
    d = date(int(end[:4]), int(end[4:6]), int(end[6:]))
    out = []
    while len(out) < n:
        if kr_calendar.is_trading_day(d):
            out.append(d.strftime("%Y%m%d"))
        d -= timedelta(days=1)
    return list(reversed(out))


class _StResp:
    def __init__(self, url: str, body: str):
        self._url, self._body = url, body.encode("utf-8")

    def read(self):
        return self._body

    def geturl(self):
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def offline_selftest(cfg: dict) -> int:
    import copy
    from unittest import mock

    n = {"ok": 0, "fail": 0}

    def check(label, got, want):
        good = got == want
        n["ok" if good else "fail"] += 1
        print(("ok  " if good else "FAIL"), label, "" if good else f"got={got!r} want={want!r}")

    g = globals()
    sent: list[tuple[str, str, int, str]] = []
    saved: list[dict] = []
    calls: list[str] = []
    fx: dict = {}  # url fragment -> body (str) or Exception
    codes = [t["code"] for t in cfg["tickers"]]
    state_before = (lambda s: (s.st_size, s.st_mtime_ns))(STATE_PATH.stat()) if STATE_PATH.exists() else None

    def fake_urlopen(req, timeout=None, **_k):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        calls.append(url)
        for frag, body in fx.items():
            if frag in url:
                if isinstance(body, Exception):
                    raise body
                return _StResp(url, body if isinstance(body, str) else json.dumps(body, ensure_ascii=False))
        raise OSError(f"selftest: no fixture for {url}")

    def no_socket(*_a, **_k):
        raise OSError("selftest: network disabled")

    def fake_publish(topic, title, body, pri=3, tags=None, server=None, **_k):
        sent.append((route_topic(topic, title), title, pri, body))

    def fake_save(state, now, dry):
        saved.append(copy.deepcopy(state))

    def at(s: str) -> datetime:
        return datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=KST)

    def market(d0: str, special: dict | None = None, foreign: dict | None = None, missing_d0: set | None = None,
               no_flow_d0: set | None = None, notice: dict | None = None, quarter: dict | None = None, dart: str = "<table></table>"):
        """Load fixtures: 25 trading days ending d0, chg 0.5% unless special[code] = {day: chg}."""
        fx.clear()
        _NOTICE_CACHE.clear()
        days = _st_trading_days(d0, 25)
        for code in codes:
            base = {"000150": 100000.0, "034020": 50000.0, "241560": 60000.0, "131970": 30000.0}[code]
            rows, trend = [], []
            for d in days:
                if code in (missing_d0 or set()) and d == d0:
                    continue
                chg = (special or {}).get(code, {}).get(d, 0.5)
                rows.append((d, base, chg))
                fshares = (foreign or {}).get(code, {}).get(d, 1000.0)  # 1000 shares x 100,000 = 1억, under every threshold
                tr = {"bizdate": d, "closePrice": str(base), "foreignerPureBuyQuant": str(fshares), "organPureBuyQuant": "500"}
                if code in (no_flow_d0 or set()) and d == d0:
                    tr["foreignerPureBuyQuant"] = None
                trend.append(tr)
            fx[f"/api/quote/A{code}/days"] = _st_days(rows)
            fx[f"/api/investor/days?symbolCode=A{code}"] = {"data": [{"date": f"{d[:4]}-{d[4:6]}-{d[6:]}", "foreignStraightPurchaseVolume": 1000} for d in days]}
            fx[f"/api/stock/{code}/integration"] = {"totalInfos": [{"code": "lastClosePrice", "value": f"{base:,.0f}"}]}
            fx[f"/api/stock/{code}/trend"] = list(reversed(trend))
            fx[f"notice?itemCode={code}"] = (notice or {}).get(code, [])
            fx[f"/api/stock/{code}/finance/quarter"] = (quarter or {}).get(code) or {"financeInfo": {
                "trTitleList": [{"key": "202609", "isConsensus": "Y"}],
                "rowList": [{"title": "매출액", "columns": {"202609": {"value": "50,000"}}},
                            {"title": "영업이익", "columns": {"202609": {"value": "4,000"}}}]}}
        fx["dart.fss.or.kr"] = dart

    def seed_state(week: str | None = None) -> dict:
        st = {"earn": {c: {"cons": dict(v)} for c, v in cfg["earnings"]["consensus_seed"].items()}}
        if week:
            st["digest_week"] = week
        return st

    q3_body = ("<p>1. 연결실적내용 단위 : 억원, % 구분 당해실적 직전실적 증감률 전년동기실적 증감률 당기실적 2026-07-01 ~ 2026-09-30 "
               "매출액 당해실적 55,000 54,000 1.9 50,000 10.0 누계실적 160,000 영업이익 당해실적 4,100 4,800 -14.6 4,500 -8.9 누계실적 13,000 "
               "당기순이익 당해실적 2,000</p>")

    patches = [mock.patch("urllib.request.urlopen", fake_urlopen), mock.patch("socket.socket.connect", no_socket),
               mock.patch("socket.create_connection", no_socket), mock.patch("time.sleep", lambda *_a: None)]
    keep = {k: g[k] for k in ("publish", "save")}
    for p in patches:
        p.start()
    g["publish"], g["save"] = fake_publish, fake_save
    try:
        # --- parser
        r = parse_result(html_text(q3_body), "2026-09-30")
        check("parse 3Q notice (억원): rev, op, yoy base", r, {"rev": 55000, "op": 4100, "op_yoy_base": 4500})
        r = parse_result(html_text(q3_body.replace("단위 : 억원", "단위 : 백만원").replace("55,000", "5,500,000").replace("4,100", "410,000").replace("4,500", "450,000")), "2026-09-30")
        check("parse 3Q notice (백만원 -> 억원)", (r or {}).get("op"), 4100)
        check("no unit -> None", parse_result(html_text(q3_body.replace("단위 : 억원,", "")), "2026-09-30"), None)
        check("no 영업이익 row -> None", parse_result(html_text(q3_body.replace("영업이익", "영업손실이익X")), "2026-09-30"), None)
        check("2분기 notice is not read as 3분기", parse_result(html_text(q3_body.replace("2026-09-30", "2026-06-30")), "2026-09-30"), None)

        # --- judge: price fire once, re-arm below half, escalate, opposite direction
        t0 = cfg["tickers"][0]
        rules = cfg["rules"]

        def ev_at(seq_chg, seq_f=None):
            rows = [{"day": f"202610{i + 1:02d}", "close": 100000.0, "chg": c, "f": (seq_f[i] if seq_f else None), "o": 0.0} for i, c in enumerate(seq_chg)]
            td = {"short": t0["short"], "rows": rows, "sign_ok": True}
            return [[e["kind"] + ":" + str(e["pri"]) for e in judge(t0, td, rules, rows[i]["day"])] for i in range(len(rows))]

        check("price +6 fires p3, +6 again silent, +1 re-arms, +6 fires, +11 escalates p4, -6 fires down",
              ev_at([6, 6, 1, 6, 11, -6]), [["price:3"], [], [], ["price:3"], ["price:4"], ["price:3"]])
        check("price +4.9 under 5% never fires", ev_at([4.9, 4.9]), [[], []])
        check("foreign 120억 fires, 130 silent, 40 re-arms, 110 fires (100억 threshold)",
              [[k for k in e if k.startswith("flow:")] for e in ev_at([0, 0, 0, 0], [120, 130, 40, 110])], [["flow:3"], [], [], ["flow:3"]])
        check("foreign 3-day buy streak fires once", [[k for k in e if k.startswith("streak")] for e in ev_at([0] * 4, [10, 10, 10, 10])],
              [[], [], ["streak:3"], []])
        check("foreign 5-day cum 300억 fires once", [[k for k in e if k.startswith("flow5")] for e in ev_at([0] * 6, [70, 70, 70, 70, 70, 70])],
              [[], [], [], [], ["flow5:3"], []])

        # --- guard (year, end date, holiday)
        check("guard: trading day 2026-11-03 09:00 -> run", guard(cfg, at("2026-11-03 09:00")), None)
        check("guard: Saturday -> 휴장", guard(cfg, at("2026-11-07 09:00")), "KRX 휴장/주말")
        check("guard: after ends_kst + 59m -> ended", guard(cfg, at("2026-11-20 10:00")), "ended")
        check("guard: ends_kst + 30m still runs", guard(cfg, at("2026-11-20 09:30")), None)
        check("guard: 2027 -> year-guard", guard(cfg, at("2027-11-03 09:00")), "year-guard")

        # --- run_slot: fire, repeat same day silent, no-fire day silent
        now = at("2026-11-03 09:00")
        d0 = "20261102"
        week = now.strftime("%G-W%V")
        market(d0, special={"000150": {d0: 6.0}})
        st = seed_state(week)
        sent.clear(); saved.clear()
        run_slot(cfg, st, now, "slot")
        check("slot: 두산 +6% on 11/2 -> one send", len(sent), 1)
        title = sent[0][1] if sent else ""
        check("slot title has [DOOSAN] and the 4 tickers", title.startswith("[DOOSAN] 000150/034020/241560/131970 "), True)
        check("slot title names the move and action", ("두산 +6.0%" in title, title.endswith("| 추격 금지, 확인만")), (True, True))
        check("slot send goes to cwally-port p3", (sent[0][0], sent[0][2]) if sent else None, ("cwally-port", 3))
        check("slot body has the six-item order", all(k in (sent[0][3] if sent else "") for k in ("이게 뭐 알림", "지금 결과/상태", "지금 할 일", "나중에", "익절/줄일 때", "하지 말 것")), True)
        check("slot state: last_day = d0, saved once (captured, not written)", (st.get("last_day"), len(saved)), (d0, 1))
        sent.clear()
        run_slot(cfg, st, now, "slot")
        check("slot: same day again -> silent (price already judged)", sent, [])
        nxt = at("2026-11-04 09:00")
        market("20261103", special={"000150": {d0: 6.0, "20261103": 6.0}})
        run_slot(cfg, st, nxt, "slot")
        check("slot: next day +6% again (still armed off) -> silent", sent, [])
        st2 = seed_state(week)
        market(d0)
        run_slot(cfg, st2, now, "slot")
        check("slot: all flat, weekly summary already sent -> silent", (sent, st2.get("last_day")), ([], d0))
        st3 = seed_state(None)
        market(d0)
        run_slot(cfg, st3, now, "slot")
        check("slot: first run of the ISO week -> weekly summary once", (len(sent), "주간 요약" in (sent[0][1] if sent else ""), st3.get("digest_week")), (1, True, week))
        sent.clear()

        # --- missing data: hold, ops once a day, partial data noted
        market(d0)
        for code in codes:
            fx[f"/api/quote/A{code}/days"] = OSError("HTTP 503")
        st4 = seed_state(week)
        run_slot(cfg, st4, now, "slot")
        check("all fetches fail -> one 조회 실패 notice to cwally-ops, no judgment",
              [(s[0], s[1].split(" | ")[0][-5:]) for s in sent], [("cwally-ops", "조회 실패")])
        check("fetch fail keeps last_day unset (hold)", st4.get("last_day"), None)
        sent.clear()
        run_slot(cfg, st4, now + timedelta(hours=1), "slot")
        check("fetch fail same day -> no second notice", sent, [])
        market(d0, special={"034020": {d0: 7.0}}, missing_d0={"000150"}, no_flow_d0={"241560"})
        st5 = seed_state(week)
        run_slot(cfg, st5, now, "slot")
        body = sent[0][3] if sent else ""
        check("partial: 두산 d0 close missing -> noted, others still judged", (len(sent), "두산: 두산 11/2 KRX 종가 없음(다음) 조회 실패" in body, "에너빌 +7.0%" in (sent[0][1] if sent else "")), (1, True, True))
        check("partial: 밥캣 flow not posted -> 수급 미게시 note", "밥캣 11/2 수급 미게시" in body, True)
        sent.clear()

        # --- earnings schedule: 예고 공시 -> D-3 once, then D-1 once
        notice = {"000150": [{"title": "(주)두산 결산실적공시 예고(안내공시)", "datetime": "2026-10-26T16:00:00",
                              "contents": "1. 결산기간 종료일 2026-09-30 2. 공시예정일 2026-11-09 3. 기타"}]}
        market(d0, notice=notice)
        st6 = seed_state(week)
        evs = earnings_events(cfg, st6, at("2026-11-04 09:00"), [])
        check("예고 공시 2026-11-09 found on 11/4 -> new-date notice tagged D-3", [(e["head"], e["n"]) for e in evs], [("두산 실적 11/9 확인", 3)])
        check("same day again -> silent", earnings_events(cfg, st6, at("2026-11-04 09:30"), []), [])
        evs = earnings_events(cfg, st6, at("2026-11-06 09:00"), [])
        check("11/6 -> D-1 once", [e["head"] for e in evs if e["head"].startswith("두산")], ["두산 실적 D-1"])
        check("D-1 not repeated", [e for e in earnings_events(cfg, st6, at("2026-11-06 10:00"), []) if e["head"].startswith("두산")], [])

        # --- run_auto: today's 3Q result vs consensus, once
        res_notice = {"000150": [{"title": "(주)두산 연결재무제표기준영업(잠정)실적(공정공시)", "datetime": "2026-11-09T15:40:00", "contents": q3_body}]}
        market("20261109", notice=res_notice)
        st7 = seed_state(week)
        sent.clear(); saved.clear()
        run_auto(cfg, st7, at("2026-11-09 17:05"), False)
        title = sent[0][1] if sent else ""
        check("auto: 두산 3Q result today -> one send", len(sent), 1)
        check("auto title has [DOOSAN], ticker and OP vs consensus", (title.startswith("[DOOSAN] 000150/"), "두산 3Q 영업익 4,100억 컨센 대비 -15%" in title), (True, True))
        check("auto: OP 15% under consensus 4,797 -> p4", sent[0][2] if sent else None, 4)
        check("auto: result_sent recorded", st7["earn"]["000150"].get("result_sent"), True)
        sent.clear()
        run_auto(cfg, st7, at("2026-11-09 17:30"), False)
        check("auto: same result again -> silent", sent, [])
        st8 = seed_state(week)
        market("20261110", notice=res_notice)
        run_auto(cfg, st8, at("2026-11-10 17:05"), False)
        check("auto: yesterday's filing is not today's -> silent (09:00 slot picks it up)", sent, [])
        st9 = seed_state(week)
        market("20261109")
        run_auto(cfg, st9, at("2026-11-09 17:05"), False)
        check("auto: no filing -> silent", sent, [])
        market("20261109")
        for code in codes:
            fx[f"notice?itemCode={code}"] = OSError("HTTP 500")
        fx["dart.fss.or.kr"] = OSError("HTTP 500")
        st10 = seed_state(week)
        run_auto(cfg, st10, at("2026-11-09 17:05"), False)
        check("auto: fetch failure -> silent, nothing marked sent", (sent, any(v.get("result_sent") for v in st10["earn"].values())), ([], False))

        # --- 두산테스나 DART path: filed + actual numbers -> result; filed but only consensus -> 접수 line on the slot
        dart = ('<table><tr><td>코스닥</td><td><a href="/dsaf001/main.do?rcpNo=20261116000123">두산테스나</a></td>'
                "<td>분기보고서 (2026.09)</td><td>두산테스나</td><td>2026.11.16</td></tr></table>")
        actual = {"financeInfo": {"trTitleList": [{"key": "202609", "isConsensus": "N"}],
                                  "rowList": [{"title": "매출액", "columns": {"202609": {"value": "820"}}}, {"title": "영업이익", "columns": {"202609": {"value": "70"}}}]}}
        market("20261116", quarter={"131970": actual}, dart=dart)
        st11 = seed_state(week)
        found = find_results(cfg, st11, at("2026-11-16 17:05"), only_today=True)
        check("Tesna: DART 분기보고서 + actual OP 70 -> found", [(f["code"], f["res"].get("op"), f["res"].get("rcp")) for f in found], [("131970", 70.0, "20261116000123")])
        lines, pri, heads, sc = results_message(cfg, st11, found, final=False)
        check("Tesna result line has DART link, +11% vs consensus 63 -> p4", ("rcpNo=20261116000123" in lines[0], pri, heads), (True, 4, ["테스나 3Q 영업익 70억 컨센 대비 +11%"]))
        market("20261116", dart=dart)
        st12 = seed_state(week)
        found = find_results(cfg, st12, at("2026-11-16 17:05"), only_today=True)
        check("Tesna filed, numbers still consensus -> auto waits", results_message(cfg, st12, found, final=False)[3], [])
        check("... slot (final) -> 분기보고서 접수 line", results_message(cfg, st12, found, final=True)[2], ["테스나 분기보고서 접수"])

        # --- dry never publishes or saves to disk
        market(d0, special={"000150": {d0: 6.0}})
        sent.clear(); saved.clear()
        g["publish"] = keep["publish"]  # the real publish must not be reached in dry
        try:
            g["save"] = keep["save"]
            run_slot(cfg, seed_state(week), now, "dry")
        finally:
            g["publish"], g["save"] = fake_publish, fake_save
        check("dry: real publish not reached, no ntfy url requested", any("ntfy" in u for u in calls), False)
    finally:
        g.update(keep)
        for p in reversed(patches):
            p.stop()
        _NOTICE_CACHE.clear()
    check("no ntfy.sh request at any point", any("ntfy" in u for u in calls), False)
    check("state/doosan.json untouched (size, mtime)", ((lambda s: (s.st_size, s.st_mtime_ns))(STATE_PATH.stat()) if STATE_PATH.exists() else None) == state_before, True)
    print(f"SELFTEST {'OK' if not n['fail'] else 'FAIL'}: {n['ok']} passed, {n['fail']} failed (fixtures only, no network, no publish, no state)")
    return 1 if n["fail"] else 0


def install(cfg: dict, now: datetime) -> None:
    body = "\n".join([
        "이게 뭐 알림: 두산 4종목(두산, 두산에너빌리티, 두산밥캣, 두산테스나) 가격, 외인 수급, 3분기 실적 알림이 설치됐다는 확인입니다.",
        "지금 결과/상태: 평일 09:00에 전 거래일 KRX 종가와 확정 수급으로 새 조건만 보냅니다. 등락 5% 이상, 외인 하루 100억 이상"
        "(에너빌 300억), 외인 5일 누적 300억, 외인 3일 연속 순매수. 월요일은 주간 요약. 실적은 D-3, D-1, 당일 안내와 발표일 17:05 결과 비교.",
        "지금 할 일: 없음. 매매 신호가 아닙니다.",
        "나중에: 밥캣 3분기 실적 10/29(콜 16:30). 두산, 에너빌은 예고 공시가 뜨면 알립니다. 테스나는 분기보고서 기한 11/16.",
        "익절/줄일 때: 해당 없음.",
        "하지 말 것: 이 알림을 매매 신호로 해석. 투자 권유 아님.",
    ])
    send(cfg, "[DOOSAN] 000150/034020/241560/131970 설치 확인 | 매매 신호 아님", body, 3, False)


def main() -> int:
    args = [a.lower() for a in sys.argv[1:]]
    mode = args[0] if args else "auto"
    if mode not in ("slot", "auto", "dry", "force", "install", "selftest"):
        print(f"unknown mode {mode}")
        return 2
    cfg = load_json(CFG_PATH)
    if mode == "selftest":
        return offline_selftest(cfg)
    now = datetime.now(KST)
    if os.environ.get("DOOSAN_NOW") and mode == "dry":
        now = datetime.strptime(os.environ["DOOSAN_NOW"], "%Y-%m-%d %H:%M").replace(tzinfo=KST)
    state = load_json(STATE_PATH)
    print(f"{now.isoformat()} mode={mode}")
    if mode == "install":
        install(cfg, now)
        return 0
    why = guard(cfg, now)
    if why and mode != "dry":
        print(f"doosan silent: {why}")
        return 0
    if why:
        print(f"(dry) guard would stop: {why}; continuing for preview")
    if mode == "dry":
        selftest(cfg)
        print("--- slot preview (dry) ---")
        run_slot(cfg, json.loads(json.dumps(state)), now, "dry")
        print("--- auto preview (dry) ---")
        run_auto(cfg, json.loads(json.dumps(state)), now, True)
        return 0
    if mode == "auto":
        run_auto(cfg, state, now, False)
    else:
        run_slot(cfg, state, now, mode)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
