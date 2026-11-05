#!/usr/bin/env python3
"""[271560] 오리온 분기 확정치 DCF 재판정 (2026-11-05 요청 pt67u). 미보유, 평가 중.

k203780 평일 19:00 KST 슬롯(000815 다음). 새 cron 없음. 분기마다 창 안에서만 DART 를 봄:
  3Q26 2026-11-06~12-15, 4Q26 2027-01-25~03-31, 1Q27 2027-04-20~05-31, 2Q27 2027-07-20~08-31, 3Q27 2027-10-20~11-30
  Stage A  연결재무제표기준영업(잠정)실적 중 분기/연간 기간인 것(월별 잠정은 법인별 합 재료로만 씀) 1번:
           매출, 영업이익, OPM 전년 대비, 4개 법인 월별 합 대비. 분기 OPM 16% 미만이면 경계 줄.
  Stage B  분기/반기/사업보고서(정정 제외, 원본이 없을 때만 정정) 1번: 연결 손익, 현금흐름표로
           TTM FCF 마진 = TTM(영업활동현금흐름 - 유형자산의 취득) / TTM 매출(누적값 + 직전 사업연도 - 전년 같은 누적).
           법인별(한국/중국/베트남/러시아) 매출, 영업이익, OPM 전년 대비(그 분기 월별 잠정 3개 합, 4분기는 연간 잠정).
           DCF(WACC 7.5%, g 2.5%, 2026 매출 3.77조 후 +6/+5/+4/+3.5%, 마진 = TTM FCF 마진 4~16% 제한),
           6%/10.5%/13% 띠, WACC 8.5% 줄, 순현금 1.2조, 소수주주 3% 차감, 리가켐 지분 별도.
           판정 매력 유지 / 보통(설비투자 부담) / 매력 하향, 직전 분기 판정과 다르면 p4, 아니면 p3. 보유 관점 줄 포함.
  해석 실패: 수동 확인 알림 1번(DART 링크), 다음 실행에서 다시 시도. 창이 끝날 때까지 보고서가 없으면 ops 1번.
  소스(DART 목록, 종가) 2일 연속 실패면 cwally-ops 1번(회복 뒤 재무장).
modes: auto | dry(발송 없음, state 안 씀) | selftest
"""
from __future__ import annotations

import html as _html
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic, route_topic  # noqa: E402
from kr_calendar import KST  # noqa: E402

NAME = "k271560"
CFG_PATH = ROOT / "configs" / f"{NAME}.json"
STATE_PATH = ROOT / "state" / f"{NAME}.json"
FX = Path(__file__).resolve().parent / "fixtures" / NAME
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
TAG = "[271560]"
DART = "https://dart.fss.or.kr"
ENT = ["한국", "중국", "베트남", "러시아"]


def default_state() -> dict:
    return {"updated_at": None, "verdict": None, "verdict_q": None, "done": {}, "fy": {}, "q3ytd": {}, "fail": {}}


def load_state() -> dict:
    st = default_state()
    if STATE_PATH.exists():
        try:
            st.update(json.loads(STATE_PATH.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass
    return st


# ---------------- DART web ----------------
def http(url: str, data: bytes | None = None, timeout: int = 30) -> bytes:
    last = None
    for i in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=UA), timeout=timeout) as r:
                return r.read()
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(2 + 3 * i)
    raise last  # type: ignore[misc]


def decode(raw: bytes) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("euc-kr", "ignore")


def parse_list(raw: str) -> list[dict]:
    out = []
    for row in re.findall(r"<tr>.*?</tr>", raw, re.S):
        m = re.search(r'rcpNo=(\d+)"[^>]*>(.*?)</a>', row, re.S)
        if not m:
            continue
        filer = re.search(r'class="tL ellipsis" title="([^"]*)"', row)
        title = _html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", m.group(2)))).strip()
        day = re.search(r"<td>(\d{4})\.(\d{2})\.(\d{2})</td>", row)
        out.append({"rcp": m.group(1), "title": title, "filer": filer.group(1).strip() if filer else "",
                    "date": f"{day.group(1)}-{day.group(2)}-{day.group(3)}" if day else f"{m.group(1)[:4]}-{m.group(1)[4:6]}-{m.group(1)[6:8]}"})
    if "rcpNo=" not in raw and "조회 결과가 없습니다" not in raw and "검색된 자료가 없습니다" not in raw:
        raise ValueError("DART 목록 형식 확인 불가")
    return [r for r in out if r["filer"] in ("", "오리온")]


def dart_list(cik: str, start: date, end: date) -> list[dict]:
    data = urllib.parse.urlencode({"textCrpNm": "오리온", "textCrpCik": cik, "startDate": start.strftime("%Y%m%d"),
                                   "endDate": end.strftime("%Y%m%d"), "maxResults": 100, "currentPage": 1, "sort": "date",
                                   "series": "desc"}).encode()
    return parse_list(decode(http(f"{DART}/dsab007/detailSearch.ax", data)))


def parse_toc(raw: str) -> list[dict]:
    out = []
    for m in re.finditer(r"node\d\['text'\] = \"([^\"]*)\";.*?node\d\['dcmNo'\] = \"(\d+)\";\s*node\d\['eleId'\] = \"(\d+)\";\s*"
                         r"node\d\['offset'\] = \"(\d+)\";\s*node\d\['length'\] = \"(\d+)\";\s*node\d\['dtd'\] = \"([^\"]*)\"", raw, re.S):
        out.append(dict(zip(("text", "dcm", "ele", "offset", "length", "dtd"), m.groups())))
    if not out:
        d = re.search(r"openPdfDownload\('\d+', '(\d+)'\)", raw)
        if d:
            out.append({"text": "본문", "dcm": d.group(1), "ele": "0", "offset": "0", "length": "0", "dtd": "HTML"})
    return out


def fetch_doc(rcp: str, pick=None) -> dict[str, str]:
    """{toc text: section text(pipe 구분)}. pick(text) 가 참인 절만. 단일 문서(잠정 공시)는 '본문'."""
    toc = parse_toc(decode(http(f"{DART}/dsaf001/main.do?rcpNo={rcp}")))
    out = {}
    for n in toc:
        if pick and not pick(n["text"]):
            continue
        time.sleep(1)
        u = (f"{DART}/report/viewer.do?rcpNo={rcp}&dcmNo={n['dcm']}&eleId={n['ele']}&offset={n['offset']}"
             f"&length={n['length']}&dtd={n['dtd']}")
        out[n["text"]] = flat(decode(http(u)))
    if not out:
        raise ValueError("DART 목차 없음")
    return out


def flat(h: str) -> str:
    h = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", h)
    t = _html.unescape(re.sub(r"<[^>]+>", " | ", h)).replace("\xa0", " ")
    t = re.sub(r"\s+", " ", t)
    return re.sub(r"(\s*\|\s*)+", " | ", t)


# ---------------- 파서 ----------------
NUM = re.compile(r"^\(?-?[\d,]+(\.\d+)?\)?$")


def num(tok: str) -> float:
    neg = tok.startswith("(") or tok.startswith("-")
    v = float(tok.strip("()").replace(",", "").lstrip("-"))
    return -v if neg else v


def toks(text: str) -> list[str]:
    return [x.strip() for x in text.split("|") if x.strip()]


def row_vals(tk: list[str], labels: tuple[str, ...], start: int = 0, allow_dash: bool = False) -> tuple[list, int]:
    for i in range(start, len(tk)):
        if tk[i] in labels:
            vals = []
            for x in tk[i + 1:]:
                if NUM.match(x):
                    vals.append(num(x))
                elif allow_dash and x == "-":
                    vals.append(None)
                else:
                    break
            if vals:
                return vals, i
    return [], -1


def unit_div(text: str) -> float:
    """원 단위 값을 억원으로 바꾸는 나눗수."""
    m = re.search(r"단위\s*:\s*(백만원|천원|원)", text)
    return {"원": 1e8, "천원": 1e5, "백만원": 1e2}[m.group(1) if m else "원"]


def parse_is(text: str) -> dict:
    tk, d = toks(text), unit_div(text)
    rev, _ = row_vals(tk, ("수익(매출액)", "매출액", "영업수익", "수익"))
    op, _ = row_vals(tk, ("영업이익(손실)", "영업이익"))
    if len(rev) < 2 or len(op) < 2:
        raise ValueError("손익계산서 매출/영업이익 행 없음")
    if len(rev) >= 4 and len(op) >= 4:
        return {"rev_q": rev[0] / d, "rev_ytd": rev[1] / d, "rev_q_y": rev[2] / d, "rev_ytd_y": rev[3] / d,
                "op_q": op[0] / d, "op_ytd": op[1] / d, "op_q_y": op[2] / d, "op_ytd_y": op[3] / d}
    return {"rev_q": None, "rev_ytd": rev[0] / d, "rev_q_y": None, "rev_ytd_y": rev[1] / d,
            "op_q": None, "op_ytd": op[0] / d, "op_q_y": None, "op_ytd_y": op[1] / d}


def parse_cf(text: str) -> dict:
    tk, d = toks(text), unit_div(text)
    ocf, _ = row_vals(tk, ("영업활동현금흐름", "영업활동으로 인한 현금흐름", "영업활동으로인한현금흐름"))
    ppe, _ = row_vals(tk, ("유형자산의 취득", "유형자산의취득", "유형자산 취득"))
    ia, _ = row_vals(tk, ("무형자산의 취득", "무형자산의취득", "무형자산 취득"))
    if len(ocf) < 2 or len(ppe) < 2:
        raise ValueError("현금흐름표 영업활동/유형자산 취득 행 없음")
    ia = ia if len(ia) >= 2 else [0.0, 0.0]
    return {"ocf": ocf[0] / d, "ocf_y": ocf[1] / d, "ppe": abs(ppe[0]) / d, "ppe_y": abs(ppe[1]) / d,
            "ia": abs(ia[0]) / d, "ia_y": abs(ia[1]) / d}


def parse_prelim(text: str) -> dict:
    """잠정 공시: 기간 3개(당기, 전기, 전년동기), 연결 매출/영업이익(당해실적), 법인별 매출/영업이익(억원)."""
    tk = toks(text)
    per = re.findall(r"(20\d\d-\d\d-\d\d) \| ~ \| (20\d\d-\d\d-\d\d)", text)
    if not per:
        m = re.search(r"당기실적\s*\|?\s*\(?(\d\d)년\s*(\d{1,2})월\)?", text)
        if not m:
            raise ValueError("잠정 기간 없음")
        y, mo = 2000 + int(m.group(1)), int(m.group(2))
        last = (date(y + (mo == 12), mo % 12 + 1, 1) - timedelta(days=1)).isoformat()
        per = [(f"{y}-{mo:02d}-01", last)]
    cur, prev = per[0], per[1] if len(per) > 1 else None
    year_ago_col = 4 if "전환여부" in text else 3
    if prev and prev[0][:4] == str(int(cur[0][:4]) - 1) and prev[0][5:] == cur[0][5:]:
        year_ago_col = 1  # 연간 잠정: 전기가 전년
    def pick(vals):
        if not vals or vals[0] is None:
            return None, None
        ya = vals[year_ago_col] if len(vals) > year_ago_col else None
        return vals[0], ya
    cons = {}
    for key, lab in (("rev", "매출액"), ("op", "영업이익")):
        i = tk.index(lab) if lab in tk else -1
        if i >= 0 and i + 1 < len(tk) and tk[i + 1] == "당해실적":
            vals, _ = row_vals(tk, ("당해실적",), i, allow_dash=True)
            cons[key], cons[key + "_y"] = pick(vals)
    ent = {}
    ri = tk.index("국가별 매출액") if "국가별 매출액" in tk else -1
    oi = tk.index("국가별 영업이익") if "국가별 영업이익" in tk else -1
    if ri >= 0 and oi > ri:
        for name in ENT:
            rv = next((j for j in range(ri, oi) if tk[j].startswith(name + "(")), None)
            ov = next((j for j in range(oi, len(tk)) if tk[j].startswith(name + "(")), None)
            if rv is None or ov is None:
                continue
            r_vals, _ = row_vals(tk, (tk[rv],), rv, allow_dash=True)
            o_vals, _ = row_vals(tk, (tk[ov],), ov, allow_dash=True)
            r, ry = pick(r_vals)
            o, oy = pick(o_vals)
            if r is not None and o is not None:
                ent[name] = {"rev": r, "rev_y": ry, "op": o, "op_y": oy}
    return {"start": cur[0], "end": cur[1], "cons": cons if cons.get("rev") is not None else None, "ent": ent}


def tver_delay(text: str) -> bool:
    for m in re.finditer(r"트베리|Tver|TVER", text):
        if re.search(r"연기|지연", text[max(0, m.start() - 150): m.end() + 150]):
            return True
    return False


# ---------------- 계산 ----------------
def dcf(cfg: dict, margin: float, wacc: float | None = None) -> float:
    d = cfg["dcf"]
    wacc = wacc if wacc is not None else d["wacc"]
    rev, pv = d["base_rev_eok"], 0.0
    fcf = 0.0
    for t, gr in enumerate([0.0] + d["growth"], start=1):
        rev *= 1 + gr
        fcf = rev * margin
        pv += fcf / (1 + wacc) ** t
    n = 1 + len(d["growth"])
    tv = fcf * (1 + d["g"]) / (wacc - d["g"]) / (1 + wacc) ** n
    eq = (pv + tv + d["net_cash_eok"]) * (1 - d["minority_haircut"])
    return eq * 1e8 / d["shares"]


def quarter_of(period_end: str) -> str:
    return f"{(int(period_end[5:7]) - 1) // 3 + 1}Q{period_end[2:4]}"


def ttm(cfg: dict, st: dict, isd: dict, cfd: dict, year: str, is_fy: bool) -> dict:
    prev_fy = str(int(year) - 1)
    if is_fy:
        return {"rev": isd["rev_ytd"], "op": isd["op_ytd"], "ocf": cfd["ocf"], "ppe": cfd["ppe"], "ia": cfd["ia"]}
    fy = st.get("fy", {}).get(prev_fy) or cfg["fy"].get(prev_fy)
    if not fy:
        raise ValueError(f"{prev_fy} 사업연도 값 없음")
    return {"rev": fy["rev"] + isd["rev_ytd"] - isd["rev_ytd_y"], "op": fy["op"] + isd["op_ytd"] - isd["op_ytd_y"],
            "ocf": fy["ocf"] + cfd["ocf"] - cfd["ocf_y"], "ppe": fy["ppe"] + cfd["ppe"] - cfd["ppe_y"],
            "ia": fy["ia"] + cfd["ia"] - cfd["ia_y"]}


def verdict_of(up: float, fcf_m: float, opm_ttm: float, r: dict) -> str:
    if up < r["upside_low"] or opm_ttm < r["opm_ttm_low"]:
        return "매력 하향"
    if up >= r["upside_high"] and fcf_m >= r["fcf_good"]:
        return "매력 유지"
    return "보통(설비투자 부담)"


def horizon_of(fcf_m: float, up: float, tver: bool, r: dict) -> str:
    if tver or fcf_m < r["fcf_clamp"][0]:
        why = "트베리 지연 문구 발견" if tver else f"TTM FCF 마진 {fcf_m:.1f}%가 {r['fcf_clamp'][0]:.0f}% 미만"
        return f"관점 재검토({why}, 설비투자 회수 시점 다시 확인)"
    if fcf_m >= r["fcf_1y"] and up >= r["upside_high"]:
        return "1년 관점도 가능(FCF 마진 10.5% 이상 회복, 상승여력 50% 이상)"
    return "2~3년 관점(트베리 2027-09 완공, 설비투자 피크 통과 후 FCF 회복)"


def pct(a: float | None, b: float | None) -> str:
    return f"{(a / b - 1) * 100:+.1f}%" if a is not None and b else "비교 불가"


def ent_lines(ent: dict, r: dict) -> tuple[str, list[str]]:
    parts, flags = [], []
    for n in ENT:
        e = ent.get(n)
        if not e:
            parts.append(f"{n} 확인 불가")
            continue
        m = e["op"] / e["rev"] * 100 if e["rev"] else 0
        my = e["op_y"] / e["rev_y"] * 100 if e.get("rev_y") and e.get("op_y") is not None else None
        dm = m - my if my is not None else None
        parts.append(f"{n} 매출 {e['rev']:,.0f}억({pct(e['rev'], e.get('rev_y'))}), 영업이익 {e['op']:,.0f}억({pct(e['op'], e.get('op_y'))}), "
                     f"OPM {m:.1f}%" + (f"({dm:+.1f}%p)" if dm is not None else ""))
        if dm is not None and dm < -r["ent_opm_drop_pp"]:
            flags.append(f"{n} OPM {dm:+.1f}%p")
        if n == "한국" and e.get("op_y") is not None and e["op"] < e["op_y"]:
            flags.append(f"한국 영업이익 감소({pct(e['op'], e['op_y'])})")
    return "; ".join(parts), flags


def sum_ent(prelims: list[dict]) -> dict:
    out: dict = {}
    for p in prelims:
        for n, e in p["ent"].items():
            o = out.setdefault(n, {"rev": 0.0, "rev_y": 0.0, "op": 0.0, "op_y": 0.0})
            for k in o:
                o[k] = None if (o[k] is None or e.get(k) is None) else o[k] + e[k]
    return out


# ---------------- 메시지 ----------------
def stage_b(cfg: dict, st: dict, q: dict, rep: dict) -> tuple[str, str, int]:
    """rep: is, cf, ent(합), close (day, v), tver, link, title."""
    r = cfg["rules"]
    year, is_fy = q["period_end"][:4], q["period_end"][5:7] == "12"
    isd, cfd = rep["is"], rep["cf"]
    t = ttm(cfg, st, isd, cfd, year, is_fy)
    fcf_m = (t["ocf"] - t["ppe"]) / t["rev"] * 100
    fcf_mi = (t["ocf"] - t["ppe"] - t["ia"]) / t["rev"] * 100
    ytd_m = (cfd["ocf"] - cfd["ppe"]) / isd["rev_ytd"] * 100
    opm_ttm = t["op"] / t["rev"] * 100
    lo, hi = r["fcf_clamp"]
    used = min(max(fcf_m, lo), hi)
    base = dcf(cfg, used / 100)
    close_day, close = rep["close"]
    up = (base / close - 1) * 100
    band = ", ".join(f"{m:g}% {dcf(cfg, m / 100) / 1e4:.1f}만" for m in r["band"])
    w2 = dcf(cfg, used / 100, cfg["dcf"]["wacc_alt"])
    lig = cfg["dcf"]["ligachem_eok"] * 1e8 / cfg["dcf"]["shares"]
    v = verdict_of(up, fcf_m, opm_ttm, r)
    hz = horizon_of(fcf_m, up, rep.get("tver", False), r)
    if is_fy:
        q3 = st.get("q3ytd", {}).get(year)
        rq = (isd["rev_ytd"] - q3["rev"], isd["op_ytd"] - q3["op"]) if q3 else (None, None)
        rqy = (None, None)
    else:
        rq, rqy = (isd["rev_q"], isd["op_q"]), (isd["rev_q_y"], isd["op_q_y"])
    if rq[0]:
        qtxt = (f"분기 매출 {rq[0]:,.0f}억({pct(rq[0], rqy[0])}), 영업이익 {rq[1]:,.0f}억({pct(rq[1], rqy[1])}), OPM {rq[1] / rq[0] * 100:.1f}%"
                + (f"(전년 {rqy[1] / rqy[0] * 100:.1f}%)" if rqy[0] else ""))
    else:
        qtxt = "분기 단독 값 계산 불가(3분기 누적 기록 없음)"
    ent = rep.get("ent") or {}
    etxt, flags = ent_lines(ent, r) if ent else ("법인별 월별 잠정 확인 불가", [])
    msum = ""
    if ent and rq[0] and all(n in ent for n in ENT):
        sr, so = sum(ent[n]["rev"] for n in ENT), sum(ent[n]["op"] for n in ENT)
        msum = f" 4개 법인 합 매출 {sr:,.0f}억, 영업이익 {so:,.0f}억 대비 연결 {rq[0] - sr:+,.0f}억, {rq[1] - so:+,.0f}억."
    prev_v = st.get("verdict")
    changed = prev_v is not None and prev_v != v
    lines = [
        f"{q['label']} 확정({rep['title']}, {rep['date']}). {qtxt}.{msum}",
        f"법인별(월별 잠정 합): {etxt}." + (f" 주의: {', '.join(flags)}." if flags else ""),
        f"현금흐름: TTM FCF 마진 {fcf_m:.1f}%(영업현금 {t['ocf']:,.0f}억 - 유형자산 취득 {t['ppe']:,.0f}억, 매출 {t['rev']:,.0f}억), 무형 포함 {fcf_mi:.1f}%, "
        f"올해 누적 {ytd_m:.1f}%. 과거 2023 9.3%, 2024 17.5%, 2025 12.4%. TTM OPM {opm_ttm:.1f}%.",
        f"DCF 베이스 {base / 1e4:.1f}만원(마진 {used:.1f}%" + (f", 측정값 {fcf_m:.1f}%를 {lo:.0f}~{hi:.0f}%로 제한" if used != fcf_m else "")
        + f", WACC 7.5%, g 2.5%), {close_day} 종가 {close:,.0f}원 대비 {up:+.0f}%. 띠: {band}. WACC 8.5% {w2 / 1e4:.1f}만. "
        f"리가켐 지분은 별도 약 +{lig / 1e4:.1f}만원/주(미포함).",
        f"판정: {v}" + (f"(직전 {st.get('verdict_q')} {prev_v}에서 바뀜)" if changed else (f"(직전 {st.get('verdict_q')} 같음)" if prev_v else "")) + ". "
        "기준: 상승여력 50% 이상 + FCF 마진 9% 이상 매력 유지, 15~50% 보통, 15% 미만 또는 TTM OPM 15% 미만 매력 하향.",
        f"보유 관점: {hz}.",
        f"원문: {rep['link']}",
    ]
    title = f"{TAG} 오리온 {q['label']} 확정 | DCF 베이스 {base / 1e4:.1f}만({up:+.0f}%), {v}"
    return title, " ".join(lines), 4 if changed else 3, v


def stage_a(cfg: dict, q: dict, p: dict, link: str, date_s: str) -> tuple[str, str, int]:
    r = cfg["rules"]
    c = p["cons"]
    opm = c["op"] / c["rev"] * 100
    opm_y = c["op_y"] / c["rev_y"] * 100 if c.get("rev_y") and c.get("op_y") is not None else None
    ms = cfg.get("monthly_sum", {}).get(q["label"])
    ent = p["ent"]
    if ent and all(n in ent for n in ENT):
        ms = {"rev": sum(ent[n]["rev"] for n in ENT), "op": sum(ent[n]["op"] for n in ENT), "src": "공시 안 법인 합"}
    mtxt = (f" 4개 법인 합({ms.get('src', '월별 잠정 합')}) 매출 {ms['rev']:,.0f}억, 영업이익 {ms['op']:,.0f}억 대비 연결 {c['rev'] - ms['rev']:+,.0f}억, "
            f"{c['op'] - ms['op']:+,.0f}억." if ms else " 4개 법인 합 없음.")
    warn = opm < r["opm_q_warn"]
    title = f"{TAG} 오리온 {q['label']} 잠정 | 매출 {c['rev']:,.0f}억({pct(c['rev'], c.get('rev_y'))}), 영업이익 {c['op']:,.0f}억, OPM {opm:.1f}%" + (", 경계" if warn else "")
    text = (f"잠정({p['start']}~{p['end']}, {date_s} 공시). 매출 {c['rev']:,.0f}억(전년 {c.get('rev_y') or 0:,.0f}억, {pct(c['rev'], c.get('rev_y'))}), "
            f"영업이익 {c['op']:,.0f}억(전년 {c.get('op_y') or 0:,.0f}억, {pct(c['op'], c.get('op_y'))}), OPM {opm:.1f}%"
            + (f"(전년 {opm_y:.1f}%)" if opm_y is not None else "") + f".{mtxt}"
            + (f" 경계: 연결 OPM {opm:.1f}%가 {r['opm_q_warn']:.0f}% 미만." if warn else "")
            + f" DCF 재판정은 분기/사업보고서(현금흐름표) 나오면 따로 보냄. 원문: {link}")
    return title, text, 4 if warn else 3


def body(stamp: str, text: str) -> str:
    return "\n".join([
        f"{stamp} | 271560 | 오리온",
        "이게 뭐 알림: 오리온 미보유, 평가 중. 분기 확정치가 나오면 FCF 마진과 법인별 이익률로 DCF 베이스를 다시 판정. 매매 지시 아님.",
        f"지금 결과/상태: {text}",
        "하지 말 것: 한 분기 숫자만 보고 한 번에 매수, 설비투자 피크 구간의 낮은 FCF 를 영구 수준으로 보기, 시장가 주문.",
    ])


# ---------------- 실행 ----------------
def active_q(cfg: dict, today: str) -> dict | None:
    for q in cfg["quarters"]:
        if q["start"] <= today <= q["end"]:
            return q
    return None


def report_title_ok(t: str, q: dict) -> bool:
    ym = f"({q['period_end'][:4]}.{q['period_end'][5:7]})"
    return any(k in t for k in ("분기보고서", "반기보고서", "사업보고서")) and ym in t


def pick_report(rows: list[dict], q: dict) -> dict | None:
    cand = [r for r in rows if report_title_ok(r["title"], q) and r["date"] >= q["start"]]
    orig = [r for r in cand if "정정" not in r["title"]]
    return (orig or cand or [None])[-1] if (orig or cand) else None


def plan(cfg: dict, st: dict, today: str, io: dict) -> tuple[list[tuple[str, str, int]], dict]:
    """io: list() -> rows, prelim(rcp) -> dict, report(rcp) -> {is, cf, tver}, close() -> (day, v). 실패는 errs 로."""
    out: list[tuple[str, str, int]] = []
    errs: dict = {}
    q = active_q(cfg, today)
    if q is None:
        for q2 in cfg["quarters"]:
            k = f"{q2['label']}:miss"
            if q2["end"] < today and not st["done"].get(f"{q2['label']}:B") and not st["done"].get(k) and q2["end"] >= cfg["created"]:
                st["done"][k] = today
                out.append((f"{TAG} 오리온 {q2['label']} 분기보고서 미게시 | 창 {q2['start']}~{q2['end']} 종료",
                            f"{q2['label']} 보고서를 창 안에서 못 찾음. DART 에서 직접 확인: {DART}/dsab007/main.do", 3))
        return out, errs
    try:
        rows = io["list"]()
    except Exception as exc:  # noqa: BLE001
        errs["dart"] = str(exc)[:200]
        return out, errs
    rows = sorted(rows, key=lambda x: (x["date"], x["rcp"]))
    link = lambda rcp: f"{DART}/dsaf001/main.do?rcpNo={rcp}"  # noqa: E731
    # Stage A: 분기/연간 기간 잠정
    if not st["done"].get(f"{q['label']}:A") and not st["done"].get(f"{q['label']}:B"):
        seen = st.setdefault("seen_prelim", [])
        for r0 in [x for x in rows if "잠정" in x["title"] and "정정" not in x["title"] and x["date"] >= q["start"]]:
            if r0["rcp"] in seen:
                continue
            try:
                p = io["prelim"](r0["rcp"])
            except Exception as exc:  # noqa: BLE001
                errs["prelim"] = str(exc)[:200]
                continue
            seen.append(r0["rcp"])
            if p["end"][:7] == q["period_end"][:7] and months_of(p) >= 3 and p.get("cons"):
                out.append(stage_a(cfg, q, p, link(r0["rcp"]), r0["date"]))
                st["done"][f"{q['label']}:A"] = r0["rcp"]
                break
    # Stage B
    if st["done"].get(f"{q['label']}:B"):
        return out, errs
    rep_row = pick_report(rows, q)
    if not rep_row:
        return out, errs
    try:
        close = io["close"]()
    except Exception as exc:  # noqa: BLE001
        errs["close"] = str(exc)[:200]
        return out, errs
    try:
        rep = io["report"](rep_row["rcp"])
        rep.update(title=rep_row["title"], date=rep_row["date"], link=link(rep_row["rcp"]), close=close)
        ent = {}
        try:
            ent = io["entities"](rows, q)
        except Exception as exc:  # noqa: BLE001
            print(f"entities fail: {exc}", file=sys.stderr)
        rep["ent"] = ent
        title, text, pri, v = stage_b(cfg, st, q, rep)
    except Exception as exc:  # noqa: BLE001
        k = f"{q['label']}:manual:{rep_row['rcp']}"
        if not st["done"].get(k):
            st["done"][k] = today
            out.append((f"{TAG} 오리온 {q['label']} 보고서 나옴 | 자동 계산 안 됨, 수동 확인",
                        f"{rep_row['title']}({rep_row['date']}) 를 찾았지만 숫자를 못 읽음({str(exc)[:120]}). 다음 실행에서 다시 시도. "
                        f"원문: {link(rep_row['rcp'])}", 3))
        return out, errs
    out.append((title, text, pri))
    st["done"][f"{q['label']}:B"] = rep_row["rcp"]
    st.update(verdict=v, verdict_q=q["label"])
    yr = q["period_end"][:4]
    if q["period_end"][5:7] == "09":
        st.setdefault("q3ytd", {})[yr] = {"rev": rep["is"]["rev_ytd"], "op": rep["is"]["op_ytd"]}
    if q["period_end"][5:7] == "12":
        st.setdefault("fy", {})[yr] = {"rev": rep["is"]["rev_ytd"], "op": rep["is"]["op_ytd"], "ocf": rep["cf"]["ocf"],
                                       "ppe": rep["cf"]["ppe"], "ia": rep["cf"]["ia"]}
    return out, errs


def months_of(p: dict) -> int:
    return (int(p["end"][:4]) - int(p["start"][:4])) * 12 + int(p["end"][5:7]) - int(p["start"][5:7]) + 1


def entities_from(rows: list[dict], q: dict, prelim_fn) -> dict:
    """그 분기 월별 잠정 3개(4분기는 연간 잠정 1개)의 법인별 합."""
    pe = q["period_end"]
    if pe[5:7] == "12":
        want = {(f"{pe[:4]}-01-01", pe)}
    else:
        m = int(pe[5:7])
        want = set()
        for mm in (m - 2, m - 1, m):
            last = (date(int(pe[:4]) + (mm == 12), mm % 12 + 1, 1) - timedelta(days=1)).isoformat()
            want.add((f"{pe[:4]}-{mm:02d}-01", last))
    lo = f"{pe[:4]}-{int(pe[5:7]) - 1 if pe[5:7] != '12' else 1:02d}-01"
    got = {}
    for r0 in sorted([x for x in rows if "잠정" in x["title"] and "정정" not in x["title"] and lo <= x["date"] <= q["end"]],
                     key=lambda x: x["date"]):
        p = prelim_fn(r0["rcp"])
        k = (p["start"], p["end"])
        if k in want and k not in got and p["ent"]:
            got[k] = p
        if len(got) == len(want):
            break
    if len(got) != len(want):
        raise ValueError(f"월별 잠정 {len(got)}/{len(want)}개")
    return sum_ent(list(got.values()))


def fail_alerts(cfg: dict, st: dict, errs: dict, today: str) -> list[tuple[str, str, int]]:
    out = []
    names = {"dart": "DART 공시 목록", "close": "종가", "prelim": "잠정 공시 본문"}
    fails = st.setdefault("fail", {})
    for key in names:
        f = fails.get(key) or {"days": 0, "last": None, "notified": False}
        if key in errs:
            if f["last"] != today:
                f["days"] += 1
                f["last"] = today
            if f["days"] >= cfg["rules"]["fail_ops_after_days"] and not f["notified"]:
                f["notified"] = True
                out.append((f"{TAG} {names[key]} 조회 실패 | {f['days']}일 연속, 다음 실행에서 다시 시도",
                            f"{names[key]} 조회가 {f['days']}일 연속 실패({errs[key]}). 그 단계는 회복될 때까지 판단 안 함. 공시가 없다는 뜻 아님.", 3))
        else:
            f = {"days": 0, "last": None, "notified": False}
        fails[key] = f
    return out


def live_io(cfg: dict, today: date) -> dict:
    cik = cfg["dart_cik"]

    def lst():
        return dart_list(cik, today - timedelta(days=150), today)

    def prelim(rcp):
        return parse_prelim(fetch_doc(rcp)["본문"])

    def report(rcp):
        secs = fetch_doc(rcp, lambda t: t.strip() in ("2-2. 연결 포괄손익계산서", "2-2. 연결 손익계산서", "2-4. 연결 현금흐름표")
                         or "원재료 및 생산설비" in t)
        is_t = next(v for k, v in secs.items() if "손익" in k)
        cf_t = next(v for k, v in secs.items() if "현금흐름" in k)
        tv = next((v for k, v in secs.items() if "생산설비" in k), "")
        return {"is": parse_is(is_t), "cf": parse_cf(cf_t), "tver": tver_delay(tv)}

    def entities(rows, q):
        return entities_from(rows, q, prelim)

    def close():
        import krx_close
        bars = [b for b in krx_close.regular_bars("271560", 8) if b.day < today.isoformat() or datetime.now(KST).hour >= 16]
        if not bars:
            raise RuntimeError("종가 없음")
        return bars[-1].day, bars[-1].close
    return {"list": lst, "prelim": prelim, "report": report, "entities": entities, "close": close}


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    if mode == "selftest":
        return selftest()
    if mode not in {"auto", "dry"}:
        print(f"unknown mode {mode}")
        return 2
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    if len(sys.argv) > 2:  # dry 날짜 흉내: dry YYYY-MM-DD
        now = datetime.strptime(sys.argv[2], "%Y-%m-%d").replace(hour=19, tzinfo=KST)
    if now > datetime.strptime(cfg["ends_kst"], "%Y-%m-%d %H:%M").replace(tzinfo=KST):
        print("ended")
        return 0
    dry = mode == "dry"
    topic = resolve_topic("port", cfg.get("ntfy_topic"))
    server = resolve_server(cfg.get("ntfy_server"))
    st = load_state()
    today = now.date().isoformat()
    q = active_q(cfg, today)
    print(f"today {today}, window: {q['label'] if q else '없음'}")
    alerts, errs = plan(cfg, st, today, live_io(cfg, now.date()))
    if errs:
        print("errors: " + json.dumps(errs, ensure_ascii=False), file=sys.stderr)
    alerts += fail_alerts(cfg, st, errs, today)
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    for title, text, pri in alerts:
        print(f"TITLE: {title}\nPRIORITY: {pri} TOPIC: {route_topic(topic, title)}\n{body(stamp, text)}\n")
        if not dry:
            publish(topic, title, body(stamp, text), pri, ["office"], server)
    if not alerts:
        print("no alert")
    if dry:
        print("DRY RUN: no publish, state not written")
        return 0
    st["updated_at"] = now.isoformat()
    STATE_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


def selftest() -> int:
    import copy
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    r = cfg["rules"]
    fails: list[str] = []

    def check(name, got, want):
        ok = got == want
        print(("ok   " if ok else "FAIL ") + f"{name}: got={got!r} want={want!r}")
        if not ok:
            fails.append(name)
    docs = json.loads((FX / "docs.json").read_text(encoding="utf-8"))
    rows0 = json.loads((FX / "list.json").read_text(encoding="utf-8"))
    R = lambda d: {k: round(v, 1) if v is not None else None for k, v in d.items()}  # noqa: E731

    def rep_of(rcp):
        d = docs[rcp]
        return {"is": parse_is(d["2-2. 연결 포괄손익계산서"]), "cf": parse_cf(d["2-4. 연결 현금흐름표"]), "tver": tver_delay(d["3. 원재료 및 생산설비"])}
    pre = lambda rcp: parse_prelim(docs[rcp]["본문"])  # noqa: E731
    q325, fy25, h126 = rep_of("20251113000368"), rep_of("20260318001320"), rep_of("20260814001054")
    check("3Q25 IS real: rev 8,289 / OP 1,379, 9M 24,079 / 3,907", (round(q325["is"]["rev_q"]), round(q325["is"]["op_q"]), round(q325["is"]["rev_ytd"]), round(q325["is"]["op_ytd"])),
          (8289, 1379, 24079, 3907))
    check("3Q25 CF real: OCF 3,527, PP&E 922, 무형 193 (prior 4,341 / 811)", R(q325["cf"]),
          {"ocf": 3527.3, "ocf_y": 4341.0, "ppe": 922.3, "ppe_y": 810.8, "ia": 193.0, "ia_y": 116.0})
    check("FY2025 real (사업보고서, 2 columns): rev 33,324, OP 5,583, FCF margin 12.4%", (round(fy25["is"]["rev_ytd"]), round(fy25["is"]["op_ytd"]), fy25["is"]["rev_q"],
          round((fy25["cf"]["ocf"] - fy25["cf"]["ppe"]) / fy25["is"]["rev_ytd"] * 100, 1)), (33324, 5583, None, 12.4))
    check("FY2024 from comparative column: FCF margin 17.5%", round((fy25["cf"]["ocf_y"] - fy25["cf"]["ppe_y"]) / fy25["is"]["rev_ytd_y"] * 100, 1), 17.5)
    check("1H26 real ('수익' label): PP&E 1,686, YTD FCF 6.3%", (round(h126["cf"]["ppe"]), round((h126["cf"]["ocf"] - h126["cf"]["ppe"]) / h126["is"]["rev_ytd"] * 100, 1)), (1686, 6.3))
    check("config FY2025 equals parsed", [round(fy25["is"]["rev_ytd"], 1), round(fy25["cf"]["ocf"], 1), round(fy25["cf"]["ppe"], 1)],
          [cfg["fy"]["2025"]["rev"], cfg["fy"]["2025"]["ocf"], cfg["fy"]["2025"]["ppe"]])
    t2 = ttm(cfg, default_state(), h126["is"], h126["cf"], "2026", False)
    check("2Q26 TTM (state baseline): FCF 11.7%, OPM 16.9%", (round((t2["ocf"] - t2["ppe"]) / t2["rev"] * 100, 1), round(t2["op"] / t2["rev"] * 100, 1)), (11.7, 16.9))
    check("tver: no delay wording in real reports", (q325["tver"], fy25["tver"], h126["tver"]), (False, False, False))
    check("tver delay detector", (tver_delay("러시아 Tver 신공장 완공 시점이 2028년으로 연기"), tver_delay("트베리 공장 2027년 9월 완공 예정")), (True, False))
    p7 = pre("20250819800073")
    check("prelim old 2025 format (5 cols, no dates): Jul 2025 한국/러시아", (p7["start"], p7["end"], p7["ent"]["한국"], p7["ent"]["러시아"]["op_y"]),
          ("2025-07-01", "2025-07-31", {"rev": 951.0, "rev_y": 920.0, "op": 152.0, "op_y": 151.0}, 23.0))
    p9 = pre("20261013800166")
    check("prelim new 2026 format (7 cols): Sep 2026 한국 op 132 vs 152", (p9["start"], p9["ent"]["한국"], p9["cons"]), ("2026-09-01", {"rev": 992.0, "rev_y": 961.0, "op": 132.0, "op_y": 152.0}, None))
    pa = pre("20260205800242")
    check("annual prelim: cons 33,324 / 5,582 vs 31,043 / 5,436, 한국 1,868 vs 1,785", (pa["start"], pa["end"], pa["cons"], pa["ent"]["한국"]["op"], pa["ent"]["한국"]["op_y"]),
          ("2025-01-01", "2025-12-31", {"rev": 33324.0, "rev_y": 31043.0, "op": 5582.0, "op_y": 5436.0}, 1868.0, 1785.0))
    q3_26 = cfg["quarters"][0]
    e326 = entities_from(rows0, q3_26, pre)
    check("3Q26 4-entity sum (Jul~Sep 2026 monthlies): rev 9,203, OP 1,519, 한국 rev 2,965 OP down",
          (sum(e326[n]["rev"] for n in ENT), sum(e326[n]["op"] for n in ENT), e326["한국"]["rev"], e326["한국"]["op"] < e326["한국"]["op_y"]), (9203.0, 1519.0, 2965.0, True))
    check("China / Russia 3Q26 rev 3,840 / 1,092", (e326["중국"]["rev"], e326["러시아"]["rev"]), (3840.0, 1092.0))
    _, fl = ent_lines(e326, r)
    check("3Q26 entity flags: 한국 OP decline flagged", [f for f in fl if f.startswith("한국 영업이익")] != [], True)
    check("DCF 10.5% ~24.0만, 6% ~15만, WACC 8.5% ~20.4만", (round(dcf(cfg, 0.105) / 1e3), round(dcf(cfg, 0.06) / 1e3), round(dcf(cfg, 0.105, 0.085) / 1e3)), (239, 149, 204))
    check("verdict branches", (verdict_of(60, 9.0, 16.5, r), verdict_of(60, 8.9, 16.5, r), verdict_of(30, 12, 16.5, r), verdict_of(14.9, 12, 16.5, r),
                               verdict_of(80, 12, 14.9, r)), ("매력 유지", "보통(설비투자 부담)", "보통(설비투자 부담)", "매력 하향", "매력 하향"))
    check("horizon branches", [horizon_of(*a, r).split("(")[0] for a in ((10.5, 50, False), (10.4, 80, False), (12, 40, False), (12, 80, True), (3.9, 80, False))],
          ["1년 관점도 가능", "2~3년 관점", "2~3년 관점", "관점 재검토", "관점 재검토"])

    # ---- 실제 3Q25 보고서로 Stage B (FY2024 는 사업보고서 비교 열) ----
    cfg25 = copy.deepcopy(cfg)
    cfg25["fy"]["2024"] = {"rev": fy25["is"]["rev_ytd_y"], "op": fy25["is"]["op_ytd_y"], "ocf": fy25["cf"]["ocf_y"], "ppe": fy25["cf"]["ppe_y"], "ia": fy25["cf"]["ia_y"]}
    q25 = {"label": "3Q25", "start": "2025-11-06", "end": "2025-12-15", "period_end": "2025-09-30"}
    cfg25["quarters"] = [q25]
    st = default_state()
    st.update(verdict="매력 유지", verdict_q="2Q25")
    rows25 = [x for x in rows0 if x["date"] <= "2025-11-13"]
    io = {"list": lambda: rows25, "prelim": pre, "report": rep_of, "entities": lambda rows, q: entities_from(rows, q, pre),
          "close": lambda: ("2025-11-13", 120600.0)}
    out, errs = plan(cfg25, st, "2025-11-13", io)
    check("real 3Q25: one Stage B, no errors", ([o[0] for o in out], errs),
          ([f"{TAG} 오리온 3Q25 확정 | DCF 베이스 30.5만(+153%), 매력 유지"], {}))
    txt = out[0][1]
    check("3Q25 body: TTM FCF 13.8%, quarter rev/OP, entity lines, horizon 1년",
          ("TTM FCF 마진 13.8%" in txt, "분기 매출 8,289억(+7.0%), 영업이익 1,379억(+0.6%)" in txt, "한국 매출 2,827억" in txt, "보유 관점: 1년 관점도 가능" in txt, out[0][2]),
          (True, True, True, True, 3))
    check("3Q25 body: 4-entity sum vs consolidated, DCF band, WACC 8.5%, Ligachem separate",
          ("4개 법인 합 매출" in txt, "띠: 6% 14.9만, 10.5% 23.9만, 13% 28.9만" in txt, "WACC 8.5%" in txt, "리가켐 지분은 별도 약 +1.5만원/주" in txt), (True, True, True, True))
    check("3Q25: state done + q3ytd saved, second run silent", (st["done"].get("3Q25:B"), round(st["q3ytd"]["2025"]["rev"]), plan(cfg25, st, "2025-11-14", io)[0]),
          ("20251113000368", 24079, []))
    print("     3Q25 sample title:", out[0][0])

    # ---- 합성: 판정/관점/우선순위 ----
    def synth(ocf, ppe, op_scale=1.0, close=120600.0, prev="매력 유지", tver=False, report_title="분기보고서 (2026.09)", rows_extra=None):
        isd = dict(q325["is"])
        isd = {k: (v * 1.08 if v is not None else None) for k, v in isd.items()}
        isd.update(rev_ytd_y=h126["is"]["rev_ytd"] + 8289.2 - 0.0, rev_q_y=8289.2, op_q_y=1378.7, op_ytd_y=3907.0, rev_ytd_y2=None)
        isd["rev_ytd_y"] = 24078.6
        isd["op_ytd"] = isd["op_ytd"] * op_scale
        isd["op_q"] = isd["op_q"] * op_scale
        cfd = {"ocf": ocf, "ocf_y": 3527.3, "ppe": ppe, "ppe_y": 922.3, "ia": 60.0, "ia_y": 193.0}
        s2 = default_state()
        s2.update(verdict=prev, verdict_q="2Q26")
        rows = [x for x in rows0] + [{"rcp": "20261113000500", "title": report_title, "filer": "오리온", "date": "2026-11-13"}] + (rows_extra or [])
        io2 = {"list": lambda: rows, "prelim": pre, "report": lambda rcp: {"is": isd, "cf": cfd, "tver": tver},
               "entities": lambda rows, q: entities_from(rows, q, pre), "close": lambda: ("2026-11-13", close)}
        o, e = plan(cfg, s2, "2026-11-13", io2)
        return o, s2, e
    o, s2, _ = synth(ocf=4800, ppe=2800)
    ttm_fcf = round((cfg["fy"]["2025"]["ocf"] + 4800 - 3527.3 - (cfg["fy"]["2025"]["ppe"] + 2600 - 922.3)) / (cfg["fy"]["2025"]["rev"] + 24078.6 * 1.08 - 24078.6) * 100, 1)
    check("synthetic 3Q26 mid FCF (%.1f%%): 매력 유지 same as 2Q26 -> p3, horizon 2~3년" % ttm_fcf,
          (o[0][0].endswith("매력 유지"), o[0][2], "보유 관점: 2~3년 관점" in o[0][1], "(직전 2Q26 같음)" in o[0][1]), (True, 3, True, True))
    o, s2, _ = synth(ocf=4000, ppe=3300)
    check("synthetic FCF < 9%% -> 보통(설비투자 부담), changed -> p4", (o[0][0].split(", ")[-1], o[0][2], "(직전 2Q26 매력 유지에서 바뀜)" in o[0][1], s2["verdict"]),
          ("보통(설비투자 부담)", 4, True, "보통(설비투자 부담)"))
    o, _, _ = synth(ocf=4800, ppe=2800, close=240000.0)
    check("synthetic close 240,000 -> upside < 15%% -> 매력 하향 p4", (o[0][0].split(", ")[-1], o[0][2]), ("매력 하향", 4))
    o, _, _ = synth(ocf=4800, ppe=2800, op_scale=0.5)
    check("synthetic TTM OPM < 15%% -> 매력 하향", ("TTM OPM 1" in o[0][1], o[0][0].split(", ")[-1]), (True, "매력 하향"))
    o, _, _ = synth(ocf=4800, ppe=2800, close=180000.0, prev="보통(설비투자 부담)")
    check("synthetic upside 15~50%% -> 보통 (same as prev) p3", (o[0][0].split(", ")[-1], o[0][2]), ("보통(설비투자 부담)", 3))
    o, _, _ = synth(ocf=5400, ppe=1500)
    check("synthetic strong FCF -> horizon 1년 관점도 가능", "보유 관점: 1년 관점도 가능" in o[0][1], True)
    o, _, _ = synth(ocf=4800, ppe=2800, tver=True)
    check("synthetic Tver delay wording -> 관점 재검토", "보유 관점: 관점 재검토(트베리 지연 문구 발견" in o[0][1], True)
    o, _, _ = synth(ocf=2000, ppe=4600)
    check("synthetic FCF < 4%% -> clamp 4%%, 관점 재검토", ("측정값" in o[0][1] and "4~16%로 제한" in o[0][1], "보유 관점: 관점 재검토(TTM FCF 마진" in o[0][1]), (True, True))
    o, _, _ = synth(ocf=4800, ppe=2800, report_title="[기재정정]분기보고서 (2026.09)")
    check("only 정정 exists -> used", len(o), 1)
    o, s2, _ = synth(ocf=4800, ppe=2800, rows_extra=[{"rcp": "20261120000600", "title": "[기재정정]분기보고서 (2026.09)", "filer": "오리온", "date": "2026-11-20"}])
    check("original + 정정 -> original used", s2["done"].get("3Q26:B"), "20261113000500")

    # ---- 흐름: 창, 해석 실패, 미게시, Stage A ----
    st = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    st0 = dict(default_state(), **st)
    io_now = {"list": lambda: rows0, "prelim": pre, "report": rep_of, "entities": lambda rows, q: entities_from(rows, q, pre), "close": lambda: ("2026-11-05", 120600.0)}
    check("initial state 11/05 (before window): silent", plan(cfg, copy.deepcopy(st0), "2026-11-05", io_now), ([], {}))
    check("initial state 11/06 (window, no 3Q26 report yet): silent", plan(cfg, copy.deepcopy(st0), "2026-11-06", io_now), ([], {}))
    bad = {"rcp": "20261113000777", "title": "분기보고서 (2026.09)", "filer": "오리온", "date": "2026-11-13"}
    s3 = copy.deepcopy(st0)
    calls = {"n": 0}

    def flaky(rcp):
        calls["n"] += 1
        if calls["n"] < 3:
            raise ValueError("손익계산서 매출/영업이익 행 없음")
        return rep_of("20251113000368")
    io_bad = dict(io_now, list=lambda: rows0 + [bad], report=flaky)
    a1, _ = plan(cfg, s3, "2026-11-13", io_bad)
    a2, _ = plan(cfg, s3, "2026-11-16", io_bad)
    check("parse failure: one manual-check notice with DART link, then silent retry", ([x[0] for x in a1], "rcpNo=20261113000777" in a1[0][1], a2),
          ([f"{TAG} 오리온 3Q26 보고서 나옴 | 자동 계산 안 됨, 수동 확인"], True, []))
    check("manual notice goes to port (not ops)", route_topic("cwally-port", a1[0][0]), "cwally-port")
    s3["fy"]["2025"] = cfg["fy"]["2025"]
    a3, _ = plan(cfg, s3, "2026-11-17", io_bad)
    check("retry succeeds later -> Stage B sent", (len(a3), a3[0][0].startswith(f"{TAG} 오리온 3Q26 확정 | DCF 베이스")), (1, True))
    s4 = copy.deepcopy(st0)
    m1, _ = plan(cfg, s4, "2026-12-16", io_now)
    m2, _ = plan(cfg, s4, "2026-12-17", io_now)
    check("window ended without report: one 미게시 notice -> ops", ([x[0] for x in m1], route_topic("cwally-port", m1[0][0]), m2),
          ([f"{TAG} 오리온 3Q26 분기보고서 미게시 | 창 2026-11-06~2026-12-15 종료"], "cwally-ops", []))
    # Stage A: 4Q26 연간 잠정 (2025 연간 잠정 본문을 2026 으로 바꿔 씀)
    ann = docs["20260205800242"]["본문"].replace("2025-01-01", "2026-01-01").replace("2025-12-31", "2026-12-31").replace("2024-01-01", "2025-01-01").replace("2024-12-31", "2025-12-31")
    rowA = {"rcp": "20270205800100", "title": "연결재무제표기준영업(잠정)실적(공정공시)", "filer": "오리온", "date": "2027-02-05"}
    s5 = copy.deepcopy(st0)
    ioA = dict(io_now, list=lambda: rows0 + [rowA], prelim=lambda rcp: parse_prelim(ann) if rcp == rowA["rcp"] else pre(rcp))
    aA, _ = plan(cfg, s5, "2027-02-05", ioA)
    check("Stage A annual prelim 4Q26: one message, OPM 16.8% no 경계", [x[0] for x in aA],
          [f"{TAG} 오리온 4Q26 잠정 | 매출 33,324억(+7.3%), 영업이익 5,582억, OPM 16.8%"])
    check("Stage A not repeated", plan(cfg, s5, "2027-02-06", ioA)[0], [])
    annw = ann.replace("5,582", "5,182")
    s6 = copy.deepcopy(st0)
    aW, _ = plan(cfg, s6, "2027-02-05", dict(ioA, prelim=lambda rcp: parse_prelim(annw) if rcp == rowA["rcp"] else pre(rcp)))
    check("Stage A OPM 15.6% < 16% -> 경계 p4", (aW[0][0].endswith(", 경계"), aW[0][2], "경계: 연결 OPM 15.6%" in aW[0][1]), (True, 4, True))
    mrow = {"rcp": "20261110800001", "title": "연결재무제표기준영업(잠정)실적(공정공시)", "filer": "오리온", "date": "2026-11-10"}
    s7 = copy.deepcopy(st0)
    check("monthly prelim (Oct) in 3Q26 window: no Stage A", plan(cfg, s7, "2026-11-10", dict(io_now, list=lambda: rows0 + [mrow], prelim=lambda rcp: pre("20261013800166") if rcp == mrow["rcp"] else pre(rcp)))[0], [])
    # 실패 처리
    s8 = copy.deepcopy(st0)
    _, e1 = plan(cfg, s8, "2026-11-09", dict(io_now, list=lambda: (_ for _ in ()).throw(RuntimeError("timeout"))))
    f1 = fail_alerts(cfg, s8, e1, "2026-11-09")
    f2 = fail_alerts(cfg, s8, e1, "2026-11-10")
    f3 = fail_alerts(cfg, s8, e1, "2026-11-11")
    fail_alerts(cfg, s8, {}, "2026-11-12")
    f4 = fail_alerts(cfg, s8, e1, "2026-11-13")
    check("DART list failure: ops only on 2nd day, once, reset on recovery", (f1, [x[0] for x in f2], f3, f4),
          ([], [f"{TAG} DART 공시 목록 조회 실패 | 2일 연속, 다음 실행에서 다시 시도"], [], []))
    check("routing: failure -> ops", route_topic("cwally-port", f2[0][0]), "cwally-ops")
    s9 = copy.deepcopy(st0)
    _, e9 = plan(cfg, s9, "2026-11-13", dict(io_now, list=lambda: rows0 + [bad], close=lambda: (_ for _ in ()).throw(RuntimeError("no close"))))
    check("close failure: Stage B held (not done), error recorded", (s9["done"].get("3Q26:B"), "close" in e9), (None, True))
    allt = [x[0] + body("s", x[1]) for x in out + aA + aW + a1 + m1 + f2]
    text = " ".join(allt) + json.dumps(cfg, ensure_ascii=False) + Path(__file__).read_text(encoding="utf-8")
    check("titles start [271560], no middle dot or em-dash", (all(a.startswith(TAG) for a in allt), "\u00b7" in text, "\u2014" in text), (True, False, False))
    check("ends 2027-12-31 23:59, 5 windows", (cfg["ends_kst"], [q["label"] for q in cfg["quarters"]]), ("2027-12-31 23:59", ["3Q26", "4Q26", "1Q27", "2Q27", "3Q27"]))
    print(f"SELFTEST {'PASS' if not fails else 'FAIL'}: {len(fails)} failed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
