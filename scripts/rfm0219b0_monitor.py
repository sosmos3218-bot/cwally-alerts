#!/usr/bin/env python3
"""[0219B0] 0219B0 가격 감시 + 우리로 계약 공시 감시 + RF머트리얼즈 3Q 확정치 재평가. 19:00 k203780 체인 같은 단계.

1) 가격 (2026-10-28 ~ 2027-02-26 KRX 거래일): 0219B0 일별 종가(네이버). 9,700 미만 진입 p4, 11,300 이상 진입 p3.
   진입마다 1번, 수준 반대편으로 3% 넘게 돌아간 종가가 나와야 다시 무장. 데이터 날짜로 중복 방지.
2) 우리로 (같은 기간 매일): seed 이후 새 KIND 공시 중 공급계약 정정/해지, 납기 연기, ETERN, 브이선라이트 관련이면 공시마다 1번.
   해지, 하향 정정, 납기 연기, 방향 판별 못 한 정정은 p4, 그 외 관련 공시 p3.
3) 3Q (2026-11-16 ~ 11-20): RF머트리얼즈 분기보고서(2026.09)를 찾으면 확정 3개월 매출과 영업이익을 잠정(10/27)과 비교,
   어느 하나라도 10% 넘게 낮으면 p4, 아니면 p3. 11/20 까지 없으면 잠정 수치로 p3. 우리로 요약 포함. 한 번만(state q3_sent).
읽기 실패: 알림 본문에 수동 확인 표시, cwally-ops 는 하루 1번.
modes: auto | dry(발송 없음, state 안 씀) | seed ; --as-of "YYYY-MM-DD HH:MM" (dry 전용)
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
import k079900_monitor as k79  # noqa: E402  (읽기 전용 재사용: http, text_of, kind_doc_text, parse_is)
from ntfy_util import publish, resolve_server, resolve_topic, route_topic  # noqa: E402

KST = k79.KST
NA = "확인 불가"
TAG = "[0219B0]"
CFG_PATH = ROOT / "configs" / "rfm0219b0.json"
STATE_PATH = ROOT / "state" / "rfm0219b0.json"
VIEW = k79.KIND_VIEW


def clean(s: str) -> str:
    """공시 제목의 가운뎃점류(U+318D, U+00B7 등)를 쉼표로."""
    return re.sub(r"\s*[\u318D\u00B7\u2027\u30FB]\s*", ", ", s).replace("\u2014", ", ")


def kind_all(code: str, since: str, until: str) -> list[dict]:
    form = {"method": "searchDetailsSub", "currentPageSize": "100", "pageIndex": "1", "orderMode": "1", "orderStat": "D",
            "forward": "details_sub", "searchCodeType": "", "repIsuSrtCd": "A" + code, "allRepIsuSrtCd": "", "oldSearchCorpName": "",
            "disclosureType": "", "disTypevalue": "", "reportNm": "", "reportCd": "", "searchCorpName": code, "business": "",
            "marketType": "", "settlementMonth": "", "securities": "", "submitOblgNm": "", "enterprise": "", "fromDate": since,
            "toDate": until, "reportNmTemp": "", "bfrDsclsType": "on"}
    page = k79.http(k79.KIND + "/disclosure/details.do", data=form,
                    headers={"X-Requested-With": "XMLHttpRequest", "Referer": k79.KIND + "/disclosure/details.do?method=searchDetailsMain"})
    out, rows = [], 0
    for row in re.findall(r"(?s)<tr[^>]*>(.*?)</tr>", page):
        a = re.search(r"openDisclsViewer\('(\d{14})'", row)
        if not a:
            continue
        rows += 1
        t = k79.text_of(row)
        dm = re.search(r"(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})", t)
        title = t[dm.end():].strip() if dm else t
        title = re.sub(r"\s*공시차트\s*주가차트\s*$", "", title)
        out.append({"acptno": a.group(1), "filed": f"{dm.group(1)} {dm.group(2)}" if dm else "", "title": title})
    if rows == 0 and "조회된 결과값이 없습니다" not in page and "<table" not in page:
        raise RuntimeError("KIND 응답 형식 이상")
    return sorted(out, key=lambda r: r["acptno"])


# ---------- RF머트리얼즈 3Q ----------
ROW = r"{name}\s*당해실적\s*([\-\d,.]+)\s+([\-\d,.]+)\s+([\-\d,.]+|-)\s+\S+\s+([\-\d,.]+)\s+([\-\d,.]+|-)"


def fnum(s: str) -> float | None:
    try:
        return float(s.replace(",", ""))
    except ValueError:
        return None


def parse_prelim(text: str) -> dict | None:
    t = re.sub(r"\s+", " ", text)
    per = re.search(r"당기실적\s*(\d{4}-\d{2}-\d{2})\s*~\s*(\d{4}-\d{2}-\d{2})", t)
    unit = re.search(r"단위\s*:\s*(백만원|억원|천원|원)", t)
    if not (per and unit):
        return None
    out = {"start": per.group(1), "end": per.group(2), "unit": unit.group(1)}
    for key, name in (("rev", "매출액"), ("op", "영업이익"), ("net", "당기순이익")):
        m = re.search(ROW.format(name=name), t)
        if not m:
            if key == "net":
                out[key] = None
                continue
            return None
        out[key] = {"cur": fnum(m.group(1)), "prev": fnum(m.group(2)), "qoq": fnum(m.group(3)), "yoy_base": fnum(m.group(4)), "yoy": fnum(m.group(5))}
    return out


def to_eok(v: float | None, unit: str) -> float | None:
    if v is None:
        return None
    return v * {"원": 1e-8, "천원": 1e-5, "백만원": 1e-2, "억원": 1.0}[unit]


def pct(v: float | None) -> str:
    return f"{v:+.1f}%" if v is not None else NA


def fmt_eok(v: float | None) -> str:
    return f"{v:,.1f}억원" if v is not None else NA


def prelim_line(p: dict) -> str:
    parts = []
    for key, name in (("rev", "매출"), ("op", "영업이익"), ("net", "순이익")):
        r = p.get(key)
        if not r:
            parts.append(f"{name} {NA}")
            continue
        parts.append(f"{name} {fmt_eok(to_eok(r['cur'], p['unit']))} (전분기 대비 {pct(r['qoq'])}, 전년동기 대비 {pct(r['yoy'])})")
    return ", ".join(parts)


# ---------- 우리로 ----------
CONTRACT_RE = re.compile(r"단일판매\s*[\u318D\u00B7,]?\s*공급계약|공급계약")


def amount_change(text: str) -> str | None:
    """정정 공시의 계약금액 정정전 -> 정정후. 'down' | 'up' | None."""
    t = re.sub(r"\s+", " ", text)
    m = re.search(r"계약금액[^0-9]{0,40}?([\d,]{7,})\s+([\d,]{7,})", t)
    if not m:
        return None
    a, b = fnum(m.group(1)), fnum(m.group(2))
    if a is None or b is None or a == b:
        return None
    return "down" if b < a else "up"



from kr_calendar import is_trading_day, session_day  # noqa: E402


# ---------- 0219B0 가격 ----------
def price_rows(code: str) -> list[dict]:
    return k79.naver_rows(code)


def price_eval(cfg: dict, st: dict, rows: list[dict], today: str, now: datetime) -> list[tuple[str, str, int, str]]:
    """새 확정 종가(데이터 날짜 > last_date)를 날짜순으로 판정. 장 마감 전 당일 행은 쓰지 않음."""
    pc = cfg["price"]
    ps = st.setdefault("price", {"last_date": "", "down_armed": True, "up_armed": True})
    lo, hi, r = pc["down"]["level"], pc["up"]["level"], pc["rearm_pct"] / 100
    out = []
    for row in rows:
        d = row["day"]
        if d <= ps["last_date"] or d < cfg["daily_window"]["start"] or d > today:
            continue
        if d == today and (now.hour, now.minute) < (15, 40):
            continue
        c = row["close"]
        if c < lo and ps["down_armed"]:
            ps["down_armed"] = False
            out.append((f"{TAG} 종가 9,700 이탈, 비중 축소 검토", "down", pc["down"]["priority"], d, c))
        elif c > lo * (1 + r) and not ps["down_armed"]:
            ps["down_armed"] = True
        if c >= hi and ps["up_armed"]:
            ps["up_armed"] = False
            out.append((f"{TAG} 종가 11,300 회복, 추가 매수 검토 가능", "up", pc["up"]["priority"], d, c))
        elif c < hi * (1 - r) and not ps["up_armed"]:
            ps["up_armed"] = True
        ps["last_date"], ps["last_close"] = d, c
    return out


def price_msgs(cfg: dict, fired: list, stamp: str) -> list[tuple[str, str, int]]:
    pc, msgs = cfg["price"], []
    for title, side, pri, d, c in fired:
        if side == "down":
            st_ = f"{d} 종가 {c:,.0f}원, 9,700원 아래로 내려옴. 다음 지지 {pc['down']['support']:,}원."
            act, trim = "0219B0 비중 축소를 검토. 8,640원 지지 여부를 보고 남길 수량 정하기.", "8,640원도 깨지면 남은 비중 다시 점검."
            later = f"9,700원 x 1.03 = {pc['down']['level'] * 1.03:,.0f}원 넘는 종가가 나온 뒤 다시 9,700원 아래로 오면 다시 알림."
        else:
            st_ = f"{d} 종가 {c:,.0f}원, 11,300원 이상으로 회복."
            act, trim = "추가 매수를 검토할 수 있음. 분할로, 계획 비중 안에서.", "급등 직후 추격 매수로 비중 넘기지 않기."
            later = f"11,300원 x 0.97 = {pc['up']['level'] * 0.97:,.0f}원 밑 종가가 나온 뒤 다시 11,300원 이상이면 다시 알림."
        msgs.append((title, body(stamp, f"{cfg['etf']['name']}(0219B0) 종가 기준 9,700원 이탈 / 11,300원 회복 감시. 매매 지시 아님.",
                                 st_, act, later, trim, "장중 가격만 보고 판단하기. 이 알림은 확정 종가 기준.", hdr="0219B0 종가 감시"), pri))
    return msgs


# ---------- 우리로 ----------
KEY_RE = re.compile(r"ETERN|이턴|브이선라이트|Vsunlight|VSunlight|중국", re.I)
WATCH_RE = re.compile(r"ETERN|이턴|브이선라이트|Vsunlight", re.I)
SKIP_TITLE = re.compile(r"투자경고|투자주의|투자위험|매매거래|단기과열|대량보유|소유상황|주주총회|감사보고서|주주명부")


def classify(t: str, body_: str) -> tuple[bool, list[str], bool]:
    """(관련 여부, 태그, p4 여부)."""
    is_contract = bool(CONTRACT_RE.search(t))
    delay = bool(re.search(r"납기\s*연기|납품\s*연기|납기\s*연장|계약기간\s*연장|공급\s*연기", t + " " + body_))
    watch = bool(WATCH_RE.search(t + " " + body_))
    china = bool(KEY_RE.search(t + " " + body_))
    tags, severe = [], False
    if "해지" in t:
        tags.append("해지")
        severe = True
    if "정정" in t and (is_contract or watch):
        d = amount_change(body_)
        tags.append({"down": "하향 정정", "up": "상향 정정"}.get(d or "", "정정(방향 원문 확인)"))
        if d != "up":
            severe = True
    if delay:
        tags.append("납기 연기")
        severe = True
    relevant = is_contract or watch or delay or china
    return relevant, tags, severe


def wooriro_scan(cfg: dict, st: dict, now: datetime) -> list[dict]:
    """seen 에 없는 새 공시 중 관련 공시. 반환 hit: {acptno, filed, title, tags, severe, line, link}. seen 갱신은 호출자가."""
    seen = set(st.get("wooriro_seen") or [])
    reps = kind_all(cfg["wooriro"]["code"], cfg["wooriro_baseline"]["seeded_at"], now.strftime("%Y-%m-%d"))
    hits, new = [], []
    for r in reps:
        if r["acptno"] in seen:
            continue
        new.append(r["acptno"])
        t = r["title"]
        body_ = ""
        if not SKIP_TITLE.search(t):
            try:
                body_ = k79.kind_doc_text(r["acptno"])
            except Exception as exc:  # noqa: BLE001
                body_ = f"(본문 {NA}: {str(exc)[:60]})"  # 정정이면 방향 판별 못 함 -> p4
        relevant, tags, severe = classify(t, body_)
        if not relevant:
            continue
        party = re.search(r"계약상대방?\s*(.{0,80}?)\s*-\s*최근", body_)
        who = clean(party.group(1).strip()) if party else ""
        hit = ", ".join(sorted({m.group(0) for m in KEY_RE.finditer(t + " " + body_)})) or "없음"
        line = f"{r['filed']} {clean(t)}{' [' + ', '.join(tags) + ']' if tags else ''}{' 상대방 ' + who if who else ''}, 키워드 {hit}"
        hits.append({"acptno": r["acptno"], "filed": r["filed"], "title": clean(t), "tags": tags, "severe": severe,
                     "line": line, "link": VIEW.format(acpt=r["acptno"])})
    st["_new_seen"] = new
    return hits


def wooriro_msgs(cfg: dict, hits: list[dict], stamp: str) -> list[tuple[str, str, int]]:
    out = []
    for h in hits:
        pri = 4 if h["severe"] else 3
        kind = ", ".join(h["tags"]) if h["tags"] else "계약 관련 공시"
        title = f"{TAG} 우리로 {kind}, 중국 계약 점검"
        out.append((title, body(stamp, "0219B0 편입사 우리로(046970)의 중국 계약(ETERN, 브이선라이트) 관련 새 KIND 공시 감시. 매매 지시 아님.",
                                h["line"] + "\n" + baseline_line(cfg) + "\nKIND 링크: " + h["link"],
                                "원문에서 계약 금액, 기간, 상대방 변화를 확인하고 0219B0 비중에 미칠 영향 점검." if pri == 4 else "원문 확인. 큰 변화가 아니면 계획 유지.",
                                "같은 공시는 다시 오지 않음. 새 공시가 나오면 다시 알림.",
                                "해지, 하향 정정, 납기 연기가 이어지면 0219B0 비중 축소 검토." if pri == 4 else "새 계약 하나로 비중 늘리지 않기.",
                                "제목만 보고 판단 끝내기.", hdr="우리로 중국 계약 공시"), pri))
    return out


# ---------- RF머트리얼즈 3Q ----------
def find_q3(cfg: dict, now: datetime) -> dict:
    """{prelim: (rep, parsed)|None, report: (rep, parsed|None, err)|None}."""
    q = cfg["q3"]
    reps = kind_all(cfg["rf"]["code"], q["search_from"], now.strftime("%Y-%m-%d"))
    found: dict = {"prelim": None, "report": None}
    for r in reps:
        t = r["title"]
        if "잠정" in t and "실적" in t and "영업" in t:
            try:
                p = parse_prelim(k79.kind_doc_text(r["acptno"]))
                if p and (p["start"], p["end"]) == (q["period_start"], q["period_end"]):
                    found["prelim"] = (r, p)
            except Exception:  # noqa: BLE001
                pass
        elif "분기보고서" in t and f"({q['report_period']})" in t:
            try:
                s = k79.parse_is(k79.kind_doc_text(r["acptno"]))
                if not s["interim"] or len(s["rev"]) < 4:
                    raise RuntimeError("3개월 열 없음")
                found["report"] = (r, s, None)
            except Exception as exc:  # noqa: BLE001
                found["report"] = (r, None, str(exc)[:120])
    return found


def prelim_from(cfg: dict, found: dict | None) -> tuple[dict, str, str]:
    """(값 dict 억원, 설명, 링크). KIND 에서 읽으면 그것, 아니면 config 에 적어둔 10/27 잠정."""
    if found and found.get("prelim"):
        r, p = found["prelim"]
        v = {k: to_eok(p[k]["cur"], p["unit"]) if p.get(k) else None for k in ("rev", "op", "net")}
        return v, f"잠정실적({r['filed']}): {prelim_line(p)}", VIEW.format(acpt=r["acptno"])
    c = cfg["q3"]["prelim"]
    line = (f"잠정실적({c['filed']}, 저장값): 매출 {fmt_eok(c['rev'])} (전분기 대비 {pct(c['rev_qoq'])}, 전년동기 대비 {pct(c['rev_yoy'])}), "
            f"영업이익 {fmt_eok(c['op'])} (전분기 대비 {pct(c['op_qoq'])}, 전년동기 대비 {pct(c['op_yoy'])}), "
            f"순이익 {fmt_eok(c['net'])} (전분기 대비 {pct(c['net_qoq'])}, 전년동기 대비 {pct(c['net_yoy'])})")
    return {"rev": c["rev"], "op": c["op"], "net": c["net"]}, line, VIEW.format(acpt=c["acptno"])


def below(final: float, pre: float | None, pct_: float) -> bool:
    if pre is None:
        return False
    if pre > 0:
        return final < pre * (1 - pct_ / 100)
    return final < pre - abs(pre) * pct_ / 100


def q3_build(cfg: dict, st: dict, stamp: str, found: dict | None, q3_err: str | None, final_day: bool):
    """None(아직 보낼 때 아님) 또는 (title, msg, pri, opsneed)."""
    rep = (found or {}).get("report")
    if rep is None and not final_day:
        return None
    pre, pre_line, pre_link = prelim_from(cfg, found)
    links, opsneed, pri = [pre_link], bool(q3_err), 3
    miss = cfg["q3"]["miss_pct"]
    if rep is None:
        title = f"{TAG} RF머트리얼즈 3Q 실적, 재평가 필요"
        rf_txt = (f"RF머트리얼즈({cfg['rf']['code']}) 3분기: 분기보고서(2026.09) 아직 미제출(11/20 19:00 기준"
                  f"{', KIND 목록 ' + NA if q3_err else ''}). 잠정 수치로 판단. {pre_line}")
    else:
        r, s, e = rep
        links.insert(0, VIEW.format(acpt=r["acptno"]))
        if s is None:
            title = f"{TAG} RF머트리얼즈 3Q 실적, 재평가 필요"
            rf_txt = f"RF머트리얼즈({cfg['rf']['code']}) 3분기: 분기보고서({r['filed']}) 값 수동 확인 필요 ({e}). {pre_line}"
            opsneed = True
        else:
            fr, fo = s["rev"][0], s["op"][0]
            dr = (fr / pre["rev"] - 1) * 100 if pre.get("rev") else None
            do = (fo / pre["op"] - 1) * 100 if pre.get("op") and pre["op"] > 0 else None
            weak = below(fr, pre.get("rev"), miss) or below(fo, pre.get("op"), miss)
            pri = 4 if weak else 3
            title = f"{TAG} RF머트리얼즈 3Q 확정치 잠정 대비 {miss}% 넘게 하회, 재평가 필요" if weak else f"{TAG} RF머트리얼즈 3Q 실적, 재평가 필요"
            yoy_r = (fr / s["rev"][2] - 1) * 100 if s["rev"][2] else None
            yoy_o = (fo / s["op"][2] - 1) * 100 if s["op"][2] > 0 else None
            rf_txt = (f"RF머트리얼즈({cfg['rf']['code']}) 3분기 확정(분기보고서 {r['filed']}, {s['basis']} 3개월): "
                      f"매출 {fmt_eok(fr)} (잠정 대비 {pct(dr)}, 전년동기 대비 {pct(yoy_r)}), 영업이익 {fmt_eok(fo)} (잠정 대비 {pct(do)}, 전년동기 대비 {pct(yoy_o)}). "
                      f"판정: {'잠정보다 ' + str(miss) + '% 넘게 낮은 항목 있음' if weak else '잠정 대비 ' + str(miss) + '% 이내 또는 상회'}. {pre_line}")
    hits = st.get("wooriro_hits") or []
    links += [h["link"] for h in hits]
    w_txt = (f"우리로({cfg['wooriro']['code']}) seed({cfg['wooriro_baseline']['seeded_at']}) 이후 관련 공시: "
             + ("; ".join(h["line"] for h in hits) if hits else "없음(새 공급계약 정정, 해지, 납기 연기, ETERN, 브이선라이트 공시 없음)"))
    if st.get("_wooriro_err"):
        w_txt += f" (오늘 목록 {NA}: {st['_wooriro_err']})"
        opsneed = True
    w_txt += ". " + baseline_line(cfg)
    status = "\n".join([rf_txt, "0219B0 재평가 필요", f"우리로 중국 계약 리스크 점검: {w_txt}", "KIND 링크: " + ", ".join(dict.fromkeys(links))])
    msg = body(stamp, f"{cfg['etf']['name']}(0219B0) 상위 편입사 RF머트리얼즈 3분기 실적 뒤 재평가 요청과 우리로 중국 계약(ETERN, 브이선라이트) 점검. 한 번만 옵니다. 매매 지시 아님.",
               status, "RF머트리얼즈 실적과 우리로 계약 변화를 보고 0219B0 비중 계획을 다시 정하기." + (" 값 일부는 원문으로 직접 확인." if opsneed else ""),
               "이 재평가 알림은 다시 오지 않습니다. 0219B0 종가 9,700 / 11,300 감시와 우리로 공시 감시는 2027-02-26 까지 계속.",
               "확정치가 잠정보다 크게 낮으면 비중 축소부터 검토." if pri == 4 else "실적 한 분기로 비중을 크게 바꾸지 않기.",
               "자동 요약만 보고 판단 끝내기, 확인 불가 값을 정상으로 읽기.")
    return title, msg, pri, opsneed


# ---------- 메시지 ----------
def body(stamp: str, what: str, status: str, action: str, later: str, trim: str, avoid: str, hdr: str = "RF머트리얼즈 3Q, 우리로 중국 계약") -> str:
    return "\n".join([f"{stamp} | 0219B0 | {hdr}", f"이게 뭐 알림: {what}", f"지금 결과/상태: {status}",
                      f"지금 할 일: {action}", f"나중에: {later}", f"익절/줄일 때: {trim}", f"하지 말 것: {avoid}"])


def baseline_line(cfg: dict) -> str:
    out = []
    for c in cfg["wooriro_baseline"]["contracts"]:
        out.append(f"{c['date']} {c['party']} {c['amount_krw'] / 1e8:,.1f}억원(USD {c['amount_usd']:,}, 매출 대비 {c['sales_pct']}%, {c['region']}, 종료 {c['end']})")
    return "기준 계약: " + "; ".join(out)


def ops_msg(stamp: str, status: str) -> tuple[str, str]:
    return (f"{TAG} 데이터 파싱 실패 | 원문 확인 필요",
            body(stamp, "0219B0 감시(종가, 우리로 공시, RF머트리얼즈 3Q)의 데이터 읽기 실패 안내. 하루 1번. 매매 신호 아님.", status,
                 "네이버 시세나 KIND 원문을 직접 보세요.", "다음 거래일 19:00 에 다시 읽습니다. 가격과 공시는 데이터 날짜, 접수번호로 이어서 판정.",
                 "이 안내로 비중을 바꾸지 않기.", "읽기 실패를 실적 부진이나 가격 이탈로 읽기.", hdr="데이터 읽기 실패"))


# ---------- 실행 ----------
def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(st: dict) -> None:
    st = {k: v for k, v in st.items() if not k.startswith("_")}
    STATE_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def run(mode: str, now: datetime, cfg: dict, pub=publish, rows_fn=None) -> int:
    dry = mode == "dry"
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    sday = session_day(now)
    today = sday.isoformat()
    st = load_state()
    topic, server = resolve_topic("port", cfg.get("ntfy_topic")), resolve_server(cfg.get("ntfy_server"))
    if mode == "seed":
        reps = kind_all(cfg["wooriro"]["code"], "2025-01-01", today)
        st["wooriro_seen"] = sorted({r["acptno"] for r in reps} | set(st.get("wooriro_seen") or []))
        rows = [r for r in price_rows(cfg["price"]["code"]) if r["day"] < today or (now.hour, now.minute) >= (15, 40)]
        last = rows[-1]
        st["price"] = {"last_date": last["day"], "last_close": last["close"],
                       "down_armed": not last["close"] < cfg["price"]["down"]["level"], "up_armed": not last["close"] >= cfg["price"]["up"]["level"],
                       "seed_note": f"seed {stamp}: 마지막 확정 종가 {last['day']} {last['close']:,.0f}원"}
        st["seeded_at"] = stamp
        save_state(st)
        print(f"seeded {len(st['wooriro_seen'])} 우리로 filings; price {st['price']}")
        return 0
    d0, d1 = cfg["daily_window"]["start"], cfg["daily_window"]["end"]
    if not (d0 <= today <= d1):
        print(f"{today} 감시 기간 밖({d0} ~ {d1}): 조용히 종료")
        return 0
    if not is_trading_day(sday):
        print(f"{today} KRX 휴장: 조용히 종료")
        return 0
    out: list[tuple[str, str, int]] = []
    errs: list[str] = []
    # 1) 가격
    try:
        fired = price_eval(cfg, st, (rows_fn or price_rows)(cfg["price"]["code"]), today, now)
        out += price_msgs(cfg, fired, stamp)
        print(f"price: {st['price']}")
    except Exception as exc:  # noqa: BLE001
        errs.append(f"0219B0 종가 {NA}: {str(exc)[:120]}")
    # 2) 우리로
    try:
        hits = wooriro_scan(cfg, st, now)
        out += wooriro_msgs(cfg, hits, stamp)
        st["wooriro_hits"] = (st.get("wooriro_hits") or []) + hits
        st["wooriro_seen"] = sorted(set(st.get("wooriro_seen") or []) | set(st.pop("_new_seen", [])))
        print(f"wooriro new relevant: {len(hits)}")
    except Exception as exc:  # noqa: BLE001
        st["_wooriro_err"] = str(exc)[:120]
        errs.append(f"우리로 KIND 목록 {NA}: {str(exc)[:120]}")
    # 3) 3Q
    q0, q1 = cfg["q3_window"]["start"], cfg["q3_window"]["end"]
    if q0 <= today <= q1 and not st.get("q3_sent"):
        found, q3_err = None, None
        try:
            found = find_q3(cfg, now)
        except Exception as exc:  # noqa: BLE001
            q3_err = str(exc)[:120]
        print(f"q3 report: {'found' if found and found.get('report') else 'none'}{' err ' + q3_err if q3_err else ''}")
        res = q3_build(cfg, st, stamp, found, q3_err, final_day=today >= q1)
        if res:
            title, msg, pri, opsneed = res
            out.append((title, msg, pri))
            st["q3_sent"] = {"at": stamp, "title": title, "priority": pri, "report": bool(found and found.get("report"))}
            if opsneed:
                errs.append("RF머트리얼즈 3분기 값이나 우리로 목록 일부를 자동으로 읽지 못함. 재평가 알림 본문의 수동 확인 항목 참고.")
        else:
            print("분기보고서 아직 없음, 11/20 전이라 3Q 는 대기")
    ops = None
    if errs and st.get("ops_day") != today:
        ops = ops_msg(stamp, " / ".join(errs))
        st["ops_day"] = today
    for title, msg, pri in out:
        assert "\u00b7" not in title + msg and "\u2014" not in title + msg, title
        assert "확인 불가" not in title and "조회 실패" not in title, title
        print(f"TITLE: {title}\nPRIORITY: {pri} TOPIC: {route_topic(topic, title)}\n{msg}\n")
    if ops:
        print(f"OPS TITLE: {ops[0]} -> {route_topic(topic, ops[0])}\n{ops[1]}\n")
    if not out and not ops:
        print("보낼 것 없음")
    if dry:
        print("DRY RUN: no publish, state not written")
        return 0
    for title, msg, pri in out:
        pub(topic, title, msg, pri, ["mag"], server, config_name="rfm0219b0")
    if ops:
        try:
            pub(resolve_topic("ops"), ops[0], ops[1], 3, ["warning"], server, config_name="rfm0219b0")
        except Exception as exc:  # noqa: BLE001
            print(f"ops notice failed: {exc}")
    save_state(st)
    return 0

def main() -> int:
    args = sys.argv[1:]
    mode = (args[0] if args and not args[0].startswith("--") else "auto").lower()
    if mode not in {"auto", "dry", "seed"}:
        print("usage: rfm0219b0_monitor.py <auto|dry|seed> [--as-of 'YYYY-MM-DD HH:MM']")
        return 2
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    if "--as-of" in args:
        if mode != "dry":
            print("--as-of 는 dry 에서만")
            return 2
        now = datetime.strptime(args[args.index("--as-of") + 1], "%Y-%m-%d %H:%M").replace(tzinfo=KST)
    try:
        return run(mode, now, cfg)
    except Exception as exc:  # noqa: BLE001
        print(traceback.format_exc(), file=sys.stderr)
        if mode != "auto":
            return 1
        try:
            t, m = ops_msg(now.strftime("%Y-%m-%d %H:%M KST"), f"스크립트 오류 {type(exc).__name__}: {str(exc)[:200]}. state 는 그대로.")
            publish(resolve_topic("ops"), t, m, 3, ["warning"], resolve_server(cfg.get("ntfy_server")))
        except Exception:  # noqa: BLE001
            return 1
        return 0


if __name__ == "__main__":
    sys.exit(main())
