#!/usr/bin/env python3
"""[000810] 삼성화재 3분기 실적 공시 1회 + 종가 615,000 / 580,000 분할매수 가격대 감시.

공시 (2026-10-29 ~ 11-30, morning 09:00 / afternoon 15:20 / k203780 19:00 슬롯):
  KIND 상세검색(실패하면 네이버 KRX 공시 피드)에서 영업(잠정)실적(공정공시) 계열(당기실적 2026-07-01 ~ 09-30)
  또는 분기보고서 (2026.09) 첫 공시를 1번 알림(제목, 접수 시각, 링크, 읽히면 지배순이익 vs 컨센서스 5,162억).
  보낸 뒤 정정 공시가 나오면 1번 더. 11/30 19:00 이후 첫 실행까지 못 찾으면 '못 찾음' 1번.
가격 (2026-10-28 ~ 2027-01-29, k203780 19:00 슬롯만): 네이버 확정 종가 <= 615,000 p3 1번, <= 580,000 p4 1번.
  수준 x 1.03 넘는 종가 뒤 다시 무장. 데이터 날짜로 중복 방지. 첫 실행은 상태만 기록.
modes: auto | dry(발송 없음, state 안 씀) | install(설치 확인 1회, state 안 씀) | selftest
  --parts earn,price (기본 둘 다; morning/afternoon 슬롯은 earn 만) ; --as-of "YYYY-MM-DD HH:MM" (dry 전용)
"""
from __future__ import annotations

import json
import re
import sys
import traceback
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import secdiv_monitor as sd  # noqa: E402  (읽기 전용 재사용: http, text_of, clean, kind_body, KIND, KIND_VIEW)
from kr_calendar import is_trading_day, session_day  # noqa: E402
from ntfy_util import publish, resolve_server, resolve_topic, route_topic  # noqa: E402

KST = sd.KST
TAG = "[000810]"
NAME = "k000810"
CFG_PATH = ROOT / "configs" / f"{NAME}.json"
STATE_PATH = ROOT / "state" / f"{NAME}.json"
CLOSE_HM = (15, 40)
EARN_RE = re.compile(r"영업\s*\(잠정\)\s*실적|분기보고서")
NAVER_NOTICE = "https://finance.naver.com/item/news_notice.naver?code=000810"


# ---------- 공시 ----------
def kind_list(cfg: dict, since: str, until: str) -> list[dict]:
    form = {
        "method": "searchDetailsSub", "currentPageSize": "100", "pageIndex": "1", "orderMode": "1", "orderStat": "D",
        "forward": "details_sub", "searchCodeType": "", "repIsuSrtCd": "A" + cfg["code"], "allRepIsuSrtCd": "",
        "oldSearchCorpName": "", "disclosureType": "", "disTypevalue": "", "reportNm": "", "reportCd": "",
        "searchCorpName": cfg["code"], "business": "", "marketType": "", "settlementMonth": "", "securities": "",
        "submitOblgNm": "", "enterprise": "", "fromDate": since, "toDate": until, "reportNmTemp": "", "bfrDsclsType": "on",
    }
    page = sd.http(sd.KIND + "/disclosure/details.do", data=form,
                   headers={"X-Requested-With": "XMLHttpRequest", "Referer": sd.KIND + "/disclosure/details.do?method=searchDetailsMain"})
    out, rows = [], 0
    for row in re.findall(r"(?s)<tr[^>]*>(.*?)</tr>", page):
        acpt = re.search(r"openDisclsViewer\('(\d{14})'", row)
        if not acpt:
            continue
        rows += 1
        t = sd.text_of(row)
        if cfg["corp_name"] not in t:
            continue
        dm = re.search(r"(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})", t)
        title = t[dm.end():].strip() if dm else t
        title = re.sub(r"\s*(공시차트|주가차트).*$", "", title).strip()
        title = re.sub(r"^" + re.escape(cfg["kind_corp_name"]) + r"\s*", "", title)
        title = re.sub(r"\s+" + re.escape(cfg["kind_corp_name"]) + r"$", "", title).strip()
        out.append({"id": "kind:" + acpt.group(1), "acptno": acpt.group(1), "title": title,
                    "filed": f"{dm.group(1)} {dm.group(2)}" if dm else "", "src": "KIND"})
    if rows == 0 and "조회된 결과값이 없습니다" not in page and "<table" not in page:
        raise RuntimeError("KIND 응답 형식 이상")
    return sorted(out, key=lambda r: r["acptno"])


def naver_list(cfg: dict, since: str, until: str) -> list[dict]:
    raw = json.loads(sd.http(f"https://stock.naver.com/api/domestic/detail/notice?itemCode={cfg['code']}&page=1&pageSize=100"))
    out = []
    for r in raw if isinstance(raw, list) else []:
        title = re.sub(r"^삼성화재해상보험\(주\)\s*", "", str(r.get("title") or "").strip())
        day = str(r.get("datetime") or "")[:10]
        if since <= day <= until:
            out.append({"id": "naver:" + str(r.get("no") or r.get("bizdateSeq")), "acptno": None, "title": title,
                        "filed": day, "src": "네이버 KRX 공시 피드", "body": sd.text_of(str(r.get("contents") or ""))})
    return sorted(out, key=lambda r: r["id"])


def is_earn(cfg: dict, title: str) -> bool:
    if not EARN_RE.search(title or ""):
        return False
    if "분기보고서" in title and "잠정" not in title:
        return cfg["earnings"]["report_period"] in title or "(" not in title
    return True


def is_correction(title: str) -> bool:
    return "정정" in (title or "")


def _n(tok: str | None) -> float | None:
    try:
        return float(str(tok).replace(",", ""))
    except (TypeError, ValueError):
        return None


def parse_prelim(text: str) -> dict | None:
    t = re.sub(r"\s+", " ", text or "")
    pm = re.search(r"당기실적\s*(\d{4}-\d{2}-\d{2})\s*~\s*(\d{4}-\d{2}-\d{2})", t)
    if not pm:
        return None
    g = lambda lab: _n((re.search(lab + r"\s*당해실적\s*(-?[\d,]+)", t) or [None, None])[1])  # noqa: E731
    unit = "백만원" if "백만원" in t[:600] else ("억원" if "억원" in t[:600] else "백만원")
    return {"p0": pm.group(1), "p1": pm.group(2), "unit": unit, "rev": g("매출액"), "op": g("영업이익"),
            "ni": g("당기순이익"), "cni": g(r"지배기업\s*소유주지분\s*순이익")}


def to_eok(v: float | None, unit: str) -> float | None:
    if v is None:
        return None
    return v / 100 if unit == "백만원" else v


def fetch_filings(cfg: dict, since: str, until: str) -> tuple[list[dict], list[str]]:
    notes: list[str] = []
    try:
        reps = [r for r in kind_list(cfg, since, until) if is_earn(cfg, r["title"])]
        nav = None
        for r in reps:
            try:
                r["body"] = sd.kind_body(r["acptno"]) if "분기보고서" not in r["title"] else ""
            except Exception as exc:  # noqa: BLE001
                notes.append(f"KIND 본문 {r['acptno']}: {exc}")
                try:
                    nav = nav if nav is not None else naver_list(cfg, since, until)
                    m = [n for n in nav if n["filed"] == r["filed"][:10] and EARN_RE.search(n["title"]) and is_correction(n["title"]) == is_correction(r["title"])]
                    r["body"] = m[-1]["body"] if m else ""
                except Exception as exc2:  # noqa: BLE001
                    notes.append(f"네이버 본문: {exc2}")
                    r["body"] = ""
        return reps, notes
    except Exception as exc:  # noqa: BLE001
        notes.append(f"KIND 목록: {exc}")
    return [r for r in naver_list(cfg, since, until) if is_earn(cfg, r["title"])], notes


def link_of(r: dict) -> str:
    return sd.KIND_VIEW.format(acpt=r["acptno"]) if r.get("acptno") else f"{NAVER_NOTICE} (네이버 공시 목록, KIND 목록을 못 읽음)"


def body(stamp: str, hdr: str, what: str, status: str, action: str, later: str, trim: str, avoid: str) -> str:
    return "\n".join([f"{stamp} | 000810 | 삼성화재 {hdr}", f"이게 뭐 알림: {what}", f"지금 결과/상태: {status}",
                      f"지금 할 일: {action}", f"나중에: {later}", f"익절/줄일 때: {trim}", f"하지 말 것: {avoid}"])


def num_line(cfg: dict, p: dict | None) -> tuple[str, str | None]:
    """(본문 문장, 비교 결과 상회/하회/None)."""
    e = cfg["earnings"]
    if not p or p.get("cni") is None:
        return "숫자는 자동으로 못 읽음. 링크에서 지배순이익을 직접 확인하세요.", None
    cni = to_eok(p["cni"], p["unit"])
    diff = cni / e["consensus_ni_eok"] - 1
    parts = [f"지배순이익 {cni:,.0f}억원, 컨센서스 {e['consensus_ni_eok']:,}억원 대비 {diff * 100:+.1f}%({'상회' if diff >= 0 else '하회'})"]
    if p.get("op") is not None:
        parts.append(f"영업이익 {to_eok(p['op'], p['unit']):,.0f}억원")
    if p.get("ni") is not None:
        parts.append(f"당기순이익 {to_eok(p['ni'], p['unit']):,.0f}억원")
    return ", ".join(parts) + f" (기간 {p['p0']} ~ {p['p1']}).", "상회" if diff >= 0 else "하회"


WHAT_E = "삼성화재(000810) 2026년 3분기 실적 공시가 처음 나오면 1번 보내는 알림. 매매 지시 아님."


def result_message(cfg: dict, r: dict, p: dict | None, stamp: str) -> tuple[str, str, int]:
    e = cfg["earnings"]
    line, cmp_ = num_line(cfg, p)
    title = f"{TAG} 3분기 실적 공시, 지배순이익 컨센서스 {cmp_}" if cmp_ else f"{TAG} 3분기 실적 공시 나옴, 원문 확인"
    st = f"{r['filed']} 접수 '{r['title']}' ({r['src']}). {line} 링크: {link_of(r)}"
    act = f"체크리스트: {e['checklist']}. 두 값을 보고 다음 분할매수 계속 여부 정하기."
    later = "정정 공시가 나오면 바뀐 점을 1번 더 보냅니다. 분기보고서는 따로 알리지 않습니다. 가격 알림(615,000 / 580,000원)은 계속."
    trim = "컨센서스를 크게 하회하고 K-ICS 나 신계약 CSM 도 기준 아래면 다음 분할매수를 미루기."
    avoid = "실적 한 줄만 보고 한 번에 비중을 크게 바꾸기."
    return title, sd.clean(body(stamp, "3분기 실적", WHAT_E, st, act, later, trim, avoid)), e["priority"]


def followup_message(cfg: dict, old: dict, r: dict, p: dict | None, stamp: str) -> tuple[str, str, int]:
    line, _ = num_line(cfg, p)
    olds = num_line(cfg, old.get("parsed"))[0]
    st = f"{r['filed']} 정정 공시 '{r['title']}' ({r['src']}). 정정 뒤: {line} 처음 알림 값: {olds} 링크: {link_of(r)}"
    return (f"{TAG} 3분기 실적 정정 공시, 바뀐 값 확인",
            sd.clean(body(stamp, "3분기 실적 정정", WHAT_E + " 정정 공시 1번 추가 안내.", st,
                          "정정 사유와 바뀐 숫자를 원문에서 확인하세요. " + cfg["earnings"]["checklist"] + ".",
                          "이 정정 안내로 공시 알림은 끝납니다. 가격 알림은 계속.", "바뀐 숫자가 판단을 바꿀 때만 계획을 고치기.",
                          "정정이 나왔다는 사실만으로 매도하기.")), cfg["earnings"]["followup_priority"])


def missing_message(cfg: dict, stamp: str) -> tuple[str, str, int]:
    return (f"{TAG} 3분기 실적 공시 못 찾음 | 직접 확인",
            body(stamp, "3분기 실적 못 찾음", WHAT_E,
                 "2026-10-29 ~ 11-30 KIND 와 네이버 공시 피드에서 3분기 잠정실적이나 분기보고서 (2026.09)를 못 찾았습니다.",
                 f"KIND 나 DART 에서 삼성화재 공시를 직접 보세요. {cfg['earnings']['checklist']}.",
                 "공시 확인은 여기서 끝납니다. 가격 알림(615,000 / 580,000원)은 2027-01-29 까지 계속.",
                 "해당 없음.", "못 찾음을 실적 부진으로 읽기."), cfg["earnings"]["missing_priority"])


def earn_step(cfg: dict, st: dict, now: datetime, stamp: str, errs: list[str], fetch=None) -> list[tuple[str, str, int, str]]:
    e, es = cfg["earnings"], st.setdefault("earn", {})
    today = now.strftime("%Y-%m-%d")
    if today < e["window_start"]:
        print(f"earn: {today} 창 시작 전({e['window_start']})")
        return []
    if es.get("final_done") or es.get("followup_sent"):
        print("earn: 끝남(못 찾음 안내 또는 정정 안내 보냄)")
        return []
    if es.get("fired") and today > e["window_end"]:
        print("earn: 창 끝, 정정 감시 종료")
        return []
    try:
        reps, notes = (fetch or fetch_filings)(cfg, e["window_start"], today)
    except Exception as exc:  # noqa: BLE001
        errs.append(f"KIND, 네이버 공시 피드 모두 조회 실패: {str(exc)[:120]}")
        return []
    for n in notes:
        print("note:", n)
    out, seen = [], set(es.get("seen") or [])
    cands = []
    for r in reps:
        p = parse_prelim(r.get("body") or "") if "잠정" in r["title"] else None
        print(f"filing {r['id']} {r['filed']} {r['title']!r} parsed={p}")
        if p and (p["p0"], p["p1"]) != (e["period_start"], e["period_end"]):
            continue
        cands.append((r, p))
    fresh = [(r, p) for r, p in cands if r["id"] not in seen]
    if not es.get("fired") and fresh:
        orig = [x for x in fresh if not is_correction(x[0]["title"])] or fresh
        pre = [x for x in orig if "잠정" in x[0]["title"]]
        r, p = (pre or orig)[0]
        t, m, pri = result_message(cfg, r, p, stamp)
        out.append((t, m, pri, "port"))
        es["fired"] = {"id": r["id"], "filed": r["filed"], "title": r["title"], "link": link_of(r), "parsed": p, "at": stamp}
        seen |= {x[0]["id"] for x in fresh if not is_correction(x[0]["title"])}
    elif es.get("fired"):
        corr = [x for x in fresh if is_correction(x[0]["title"])]
        if corr:
            r, p = corr[-1]
            t, m, pri = followup_message(cfg, es["fired"], r, p, stamp)
            out.append((t, m, pri, "port"))
            es["followup_sent"] = {"id": r["id"], "filed": r["filed"], "title": r["title"], "parsed": p, "at": stamp}
        seen |= {x[0]["id"] for x in fresh}
    es["seen"] = sorted(seen)
    if not es.get("fired") and not out:
        final = datetime.strptime(e["final_kst"], "%Y-%m-%d %H:%M").replace(tzinfo=KST)
        if now >= final:
            t, m, pri = missing_message(cfg, stamp)
            out.append((t, m, pri, "port"))
            es["final_done"] = stamp
        else:
            print(f"earn: 아직 3분기 실적 공시 없음, {e['final_kst']} KST 까지 계속 확인")
    return out


# ---------- 가격 ----------
def fetch_rows(cfg: dict) -> list[dict]:
    raw = json.loads(sd.http(cfg["sources"]["price"]))
    rows = []
    for r in raw if isinstance(raw, list) else []:
        d, c = str(r.get("localTradedAt") or "")[:10], _n(r.get("closePrice"))
        if len(d) == 10 and c:
            rows.append({"day": d, "close": c})
    if not rows:
        raise RuntimeError("시세 0건")
    return sorted(rows, key=lambda x: x["day"])


def confirmed(rows: list[dict], today: str, now: datetime) -> list[dict]:
    return [r for r in rows if r["day"] < today or (r["day"] == today and (now.hour, now.minute) >= CLOSE_HM)]


def seed_price(cfg: dict, rows: list[dict], stamp: str) -> dict:
    last = rows[-1]
    armed = {lv["key"]: not last["close"] <= lv["level"] for lv in cfg["price"]["levels"]}
    return {"last_date": last["day"], "last_close": last["close"], "armed": armed,
            "seed_note": f"설치 seed {stamp}: 마지막 확정 종가 {last['day']} {last['close']:,.0f}원"}


def price_eval(cfg: dict, ps: dict, rows: list[dict], today: str) -> list[dict]:
    out = []
    for row in rows:
        d, c = row["day"], row["close"]
        if d <= ps["last_date"] or d < cfg["price"]["window_start"] or d > today:
            continue
        for lv in cfg["price"]["levels"]:
            k, lvl = lv["key"], lv["level"]
            if c <= lvl and ps["armed"].get(k, True):
                ps["armed"][k] = False
                out.append({**lv, "day": d, "close": c})
            elif c > lvl * (1 + lv["rearm_pct"] / 100) and not ps["armed"].get(k, True):
                ps["armed"][k] = True
        ps["last_date"], ps["last_close"] = d, c
    return out


WHAT_P = "삼성화재(000810) 확정 종가 기준 분할매수 가격대 감시(615,000원 1차, 580,000원 3차). 매매 지시 아님."


def price_messages(cfg: dict, ev: list[dict], stamp: str) -> list[tuple[str, str, int, str]]:
    out = []
    ck = cfg["earnings"]["checklist"]
    for e in ev:
        lvl, rearm = e["level"], e["level"] * (1 + e["rearm_pct"] / 100)
        if e["key"] == "l615":
            t = f"{TAG} 종가 615,000 이하, 1차 분할매수 가격대"
            act = "1차 분할매수를 검토할 수 있음. 계획한 1차 수량만, 지정가로."
            trim = "3차 가격대 580,000원까지 내려가면 따로 알림. 그 전에는 1차 몫만."
        else:
            t = f"{TAG} 종가 580,000 이하, 3차 분할매수 가격대"
            act = "3차 분할매수나 비중 확대를 검토할 수 있음. 계획 비중 상한 안에서."
            trim = "비중 상한을 넘기지 않기. 실적 체크리스트가 나쁘면 확대를 미루기."
        st = f"{e['day']} 종가 {e['close']:,.0f}원, {e['label']} {lvl:,}원 이하로 내려옴."
        later = f"{lvl:,} x 1.03 = {rearm:,.0f}원 넘는 종가가 나온 뒤 다시 {lvl:,}원 이하로 오면 다시 알림. 3분기 실적 체크: {ck}."
        out.append((t, body(stamp, "가격", WHAT_P, st, act, later, trim, "장중 가격만 보고 판단하기. 이 알림은 확정 종가 기준."), e["priority"], "port"))
    return out


def price_step(cfg: dict, st: dict, now: datetime, stamp: str, errs: list[str], rows_fn=None) -> list[tuple[str, str, int, str]]:
    pc, today = cfg["price"], session_day(now).isoformat()
    if not (pc["window_start"] <= today <= pc["window_end"]):
        print(f"price: {today} 감시 기간 밖")
        return []
    if not is_trading_day(session_day(now)):
        print(f"price: {today} KRX 휴장")
        return []
    try:
        rows = confirmed((rows_fn or fetch_rows)(cfg), today, now)
    except Exception as exc:  # noqa: BLE001
        errs.append(f"000810 네이버 시세 조회 실패: {str(exc)[:120]}")
        return []
    if not (st.get("price") or {}).get("last_date"):
        st["price"] = seed_price(cfg, rows, stamp)
        print(f"price first run: state only, no alert: {st['price']}")
        return []
    out = price_messages(cfg, price_eval(cfg, st["price"], rows, today), stamp)
    print(f"price: {st['price']}")
    return out


# ---------- 실행 ----------
def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(st: dict) -> None:
    STATE_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def check_text(t: str, m: str) -> None:
    assert not re.search("[\u00b7\u318d\u2014]", t + m), t


def ops_msg(stamp: str, status: str) -> tuple[str, str]:
    return (f"{TAG} 공시 시세 조회 실패 | 원문 확인 필요",
            body(stamp, "데이터 읽기 실패", "삼성화재 감시(3분기 실적 공시, 종가)의 데이터 읽기 실패 안내. 하루 1번. 매매 신호 아님.", status,
                 "KIND 나 네이버 증권에서 삼성화재를 직접 보세요.", "다음 슬롯에서 다시 읽고 이어서 판정합니다.",
                 "이 안내로 비중을 바꾸지 않기.", "읽기 실패를 실적 부진이나 가격 이탈로 읽기."))


def install_message(cfg: dict, stamp: str, st: dict) -> tuple[str, str]:
    ps = st.get("price") or {}
    cur = f" 지금 기록: {ps['last_date']} 종가 {ps['last_close']:,.0f}원, 두 가격대 모두 위." if ps.get("last_date") and all(ps.get("armed", {}).values()) else ""
    return (f"{TAG} 설치 확인 | 매매 신호 아님",
            body(stamp, "설치 확인", "삼성화재(000810) 감시 설치 확인용 테스트 발송입니다. 매매 신호가 아닙니다.",
                 "1) 평일 09:00, 15:20, 19:00 KST 에 KIND 공시를 보고 3분기 실적 공시(잠정실적 또는 분기보고서)가 처음 나오면 1번 "
                 "(지배순이익 vs 컨센서스 5,162억, 링크, 체크리스트). 2) 평일 19:00 확정 종가 615,000원 이하 1번(1차 분할매수 가격대), "
                 "580,000원 이하 1번(3차 분할매수 가격대)." + cur,
                 "알림이 도착했는지 확인만 하세요.",
                 "정정 공시는 1번 더, 11/30 19:00 까지 못 찾으면 못 찾음 1번. 가격대는 수준 x 1.03 넘는 종가 뒤에 다시 켜집니다.",
                 "해당 없음.", "이 메시지를 매매 신호로 해석하기."))


def run(mode: str, now: datetime, cfg: dict, parts: set[str], pub=publish) -> int:
    dry = mode == "dry"
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    st = load_state()
    topic, server = resolve_topic("port", cfg.get("ntfy_topic")), resolve_server(cfg.get("ntfy_server"))
    print(f"{stamp} {NAME} mode={mode} parts={sorted(parts)}")
    if mode == "install":
        t, m = install_message(cfg, stamp, st)
        check_text(t, m)
        print(f"{t}\n{m}")
        pub(topic, t, m, 3, ["white_check_mark"], server, config_name=NAME)
        print("install confirmation sent (state untouched)")
        return 0
    out, errs = [], []
    if "earn" in parts:
        out += earn_step(cfg, st, now, stamp, errs)
    if "price" in parts:
        out += price_step(cfg, st, now, stamp, errs)
    ops = None
    today = now.strftime("%Y-%m-%d")
    if errs and st.get("ops_day") != today:
        ops = ops_msg(stamp, " / ".join(errs) + ". state 는 그대로.")
        st["ops_day"] = today
    for t, m, pri, _ in out:
        check_text(t, m)
        print(f"TITLE: {t}\nPRIORITY: {pri} TOPIC: {route_topic(topic, t)}\n{m}\n")
    if ops:
        print(f"OPS TITLE: {ops[0]} -> {route_topic(topic, ops[0])}\n{ops[1]}\n")
    if not out and not ops:
        print("보낼 것 없음")
    if dry:
        print("DRY RUN: no publish, state not written")
        return 0
    for t, m, pri, _ in out:
        pub(topic, t, m, pri, ["chart_with_downwards_trend" if "가격대" in t else "page_facing_up"], server, config_name=NAME)
    if ops:
        try:
            pub(resolve_topic("ops"), ops[0], ops[1], 3, ["warning"], server, config_name=NAME)
        except Exception as exc:  # noqa: BLE001
            print(f"ops notice failed: {exc}")
    st["updated_at"] = stamp
    save_state(st)
    return 0


def selftest() -> int:
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    raw = ("연결재무제표 기준 영업(잠정)실적(공정공시) 실적기간 당기실적 2026-07-01 ~ 2026-09-30 전기실적 2026-04-01 ~ 2026-06-30 "
           "1. 연결실적내용 단위 : 백만원, % 매출액 당해실적 5,732,271 6,857,430 영업이익 당해실적 664,064 811,465 "
           "당기순이익 당해실적 538,496 638,379 지배기업 소유주지분 순이익 당해실적 537,972 637,450")
    p = parse_prelim(raw)
    assert p and p["cni"] == 537972 and p["op"] == 664064 and p["p1"] == "2026-09-30", p
    assert abs(to_eok(p["cni"], p["unit"]) - 5379.72) < 0.01
    assert parse_prelim(raw.replace("2026-07-01 ~ 2026-09-30", "2026-04-01 ~ 2026-06-30"))["p0"] == "2026-04-01"
    assert is_earn(cfg, "연결재무제표기준영업(잠정)실적(공정공시)") and is_earn(cfg, "분기보고서(일반법인) (2026.09)")
    assert not is_earn(cfg, "반기보고서(일반법인) (2026.06)") and not is_earn(cfg, "분기보고서(일반법인) (2026.03)")
    assert is_correction("[기재정정]연결재무제표기준영업(잠정)실적(공정공시)")
    rep = {"id": "kind:20261113000014", "acptno": "20261113000014", "title": "연결재무제표기준영업(잠정)실적(공정공시)", "filed": "2026-11-13 08:06", "src": "KIND", "body": raw}
    old = {"id": "kind:x", "acptno": "x", "title": "연결재무제표기준영업(잠정)실적(공정공시)", "filed": "2026-08-13 08:16", "src": "KIND",
           "body": raw.replace("2026-07-01 ~ 2026-09-30", "2026-04-01 ~ 2026-06-30")}
    now = datetime(2026, 11, 13, 9, 0, tzinfo=KST)
    st: dict = {}
    out = earn_step(cfg, st, now, "T", [], fetch=lambda c, a, b: ([old, rep], []))
    assert len(out) == 1 and "상회" in out[0][0] and "5,380억원" in out[0][1] and "K-ICS" in out[0][1], out
    assert earn_step(cfg, st, now, "T", [], fetch=lambda c, a, b: ([old, rep], [])) == []
    corr = {**rep, "id": "kind:20261114000001", "acptno": "20261114000001", "title": "[기재정정]연결재무제표기준영업(잠정)실적(공정공시)"}
    out2 = earn_step(cfg, st, now, "T", [], fetch=lambda c, a, b: ([rep, corr], []))
    assert len(out2) == 1 and "정정" in out2[0][0] and st["earn"]["followup_sent"], out2
    st2: dict = {}
    assert earn_step(cfg, st2, datetime(2026, 11, 30, 15, 20, tzinfo=KST), "T", [], fetch=lambda c, a, b: ([], [])) == []
    out3 = earn_step(cfg, st2, datetime(2026, 11, 30, 19, 0, tzinfo=KST), "T", [], fetch=lambda c, a, b: ([], []))
    assert len(out3) == 1 and "못 찾음" in out3[0][0] and st2["earn"]["final_done"]
    nolink = earn_step(cfg, {}, now, "T", [], fetch=lambda c, a, b: ([{**rep, "body": ""}], []))
    assert "원문 확인" in nolink[0][0] and "직접 확인" in nolink[0][1]
    # 가격
    mk = lambda cs: [{"day": f"2026-11-{i + 2:02d}", "close": float(c)} for i, c in enumerate(cs)]  # noqa: E731
    ps = seed_price(cfg, [{"day": "2026-10-28", "close": 637000.0}], "T")
    assert all(ps["armed"].values())
    ev = price_eval(cfg, ps, mk([615000, 600000, 580000, 575000, 597400, 597500, 579000, 633450, 633451, 614000]), "2026-12-31")
    assert [(e["key"], e["day"]) for e in ev] == [("l615", "2026-11-02"), ("l580", "2026-11-04"), ("l580", "2026-11-08"), ("l615", "2026-11-11")], ev
    s2 = seed_price(cfg, [{"day": "2026-10-27", "close": 597000.0}], "T")
    assert s2["armed"] == {"l615": False, "l580": True}, s2
    for t, m, *_ in out + out2 + out3 + nolink + price_messages(cfg, ev, "T"):
        check_text(t, m)
    print("selftest ok")
    return 0


def main() -> int:
    args = sys.argv[1:]
    mode = (args[0] if args and not args[0].startswith("--") else "auto").lower()
    if mode == "selftest":
        return selftest()
    if mode not in {"auto", "dry", "install"}:
        print(f"usage: {NAME}_monitor.py <auto|dry|install|selftest> [--parts earn,price] [--as-of 'YYYY-MM-DD HH:MM']")
        return 2
    parts = set(args[args.index("--parts") + 1].split(",")) if "--parts" in args else {"earn", "price"}
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    if "--as-of" in args:
        if mode != "dry":
            print("--as-of 는 dry 에서만")
            return 2
        now = datetime.strptime(args[args.index("--as-of") + 1], "%Y-%m-%d %H:%M").replace(tzinfo=KST)
    try:
        return run(mode, now, cfg, parts)
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
