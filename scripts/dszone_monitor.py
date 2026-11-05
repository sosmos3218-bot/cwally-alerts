#!/usr/bin/env python3
"""[000150] 두산 3Q26 전자BG 영업이익, OPM 으로 75~80만원 첫 분할매수 구간 판정 (2026-11-05 요청 pt69u).

doosan 스텝(평일 09:00 morning slot, 10/26~11/16 평일 17:05 one-shot)에서 doosan_monitor 다음에 실행. 새 cron 없음.
2026-11-09 16:00 KST 전에는 아무것도 읽지 않음. 그 뒤 실행마다:
  1) 전자BG 3Q26 값: DART 분기보고서(2026.09) 부문별 정보(9개월 - 상반기)가 있으면 확정값,
     없으면 doosan.com IR 자료(3/4분기 ㈜두산 경영실적 PDF) 차트에서 '26.3Q 값을 읽음.
     IR 의 전자BG 쪽에 영업이익 라벨이 있으면 그 값, 없으면 자체사업 영업이익 - 2Q26 비전자 몫(107억)으로 추정.
  2) 판정 유지 / 조정 / 보류 1번(제목 [000150]). IR 추정으로 보낸 뒤 분기보고서 확정치로 판정이 바뀌면 정정 1번.
  3) 실적이 나왔는데(잠정 공시 또는 IR 자료) 전자BG 값을 못 읽으면 수동 확인 1번, 다음 실행에서 재시도.
     11/16 17:05 실행까지 못 보내면 ops 1번. 소스 2일 연속 실패 ops 1번.
modes: auto | dry(발송 없음, state 안 씀, DSZONE_NOW="YYYY-MM-DD HH:MM") | selftest
"""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic, route_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
NAME = "dszone"
CFG_PATH = ROOT / "configs" / f"{NAME}.json"
STATE_PATH = ROOT / "state" / f"{NAME}.json"
FX = Path(__file__).resolve().parent / "fixtures" / NAME
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
TAG = "[000150]"
IR_URL = "https://www.doosan.com/kr/ir/ir-report/"
DART = "https://dart.fss.or.kr"


def default_state() -> dict:
    return {"updated_at": None, "sent": None, "confirmed": None, "correction_sent": False, "manual_sent": False,
            "ops_sent": False, "fail": {}}


def load_state() -> dict:
    st = default_state()
    if STATE_PATH.exists():
        try:
            st.update(json.loads(STATE_PATH.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass
    return st


# ---------------- IR 자료(PDF 차트) ----------------
NUMTOK = re.compile(r"(?<![\d,.])(\d{1,2},\d{3}|\d{3,5})(?![\d,.%])")
PCT = re.compile(r"\((\d{1,2}\.\d)\)")


def nospace(s):
    return re.sub(r"\s+", "", s)


def pages(text):
    return text.split("\f")


def find_page(text, *keys):
    for p in pages(text):
        ns = nospace(p)
        if all(nospace(k) in ns for k in keys):
            return p
    return None


def label_col(page, qlabel):
    """qlabel like 26.3Q. returns (line index, center column) of the x-axis label."""
    lines = page.split("\n")
    for i, ln in enumerate(lines):
        m = re.search(r"['’‘]" + re.escape(qlabel), ln)
        if m and re.search(r"['’‘]\d\d\.\dQ", ln[:m.start()] + ln[m.end():]):
            return i, m.start() + len(m.group(0)) / 2
    return None, None


def chart_point(page, qlabel, tol=6):
    """rightmost-quarter values: (revenue, op, opm) near qlabel column. op/opm None if no (xx.x) label there."""
    ai, col = label_col(page, qlabel)
    if ai is None:
        return None
    lines = page.split("\n")[:ai]
    nums, pcts = [], []
    for li, ln in enumerate(lines):
        for m in NUMTOK.finditer(ln):
            c = (m.start() + m.end()) / 2
            if abs(c - col) <= tol:
                nums.append((li, c, float(m.group(1).replace(",", ""))))
        for m in PCT.finditer(ln):
            c = (m.start() + m.end()) / 2
            if abs(c - col) <= tol:
                pcts.append((li, c, float(m.group(1))))
    op = opm = None
    used = set()
    for li, c, p in pcts:
        cand = [(abs(c2 - c), li2, v) for li2, c2, v in nums if li - 2 <= li2 <= li and (li2 < li or c2 < c)]
        cand = [x for x in cand if x[0] <= tol]
        if cand:
            _, li2, v = min(cand)
            if op is None or li2 < op[0]:
                op, opm = (li2, v), p
    if op:
        used.add(op)
    revs = [v for li, c, v in nums if (li, v) not in used]
    if op:
        revs = [v for v in revs if v > op[1]]
    rev = max(revs) if revs else None
    return {"rev": rev, "op": op[1] if op else None, "opm": opm}


def consistent(pt: dict | None) -> bool:
    return bool(pt and pt.get("rev") and pt.get("op") and pt.get("opm") is not None and abs(pt["op"] / pt["rev"] * 100 - pt["opm"]) <= 0.35)


def parse_deck(text: str, qlabel: str, non_ebg_op: float) -> dict | None:
    """{'rev','op','opm','basis','own'} 또는 None. qlabel 예: 26.3Q."""
    ep = find_page(text, "전자BG 실적및", "분기별매출액추이") or find_page(text, "전자BG 실적", "매출액")
    op_page = find_page(text, "자체사업실적및", "분기별실적추이")
    e = chart_point(ep, qlabel) if ep else None
    own = chart_point(op_page, qlabel) if op_page else None
    if not e or not e.get("rev"):
        return None
    if consistent(e):
        return {"rev": e["rev"], "op": e["op"], "opm": e["opm"], "basis": "IR 전자BG 영업이익", "own": own if consistent(own) else None}
    if not consistent(own):
        return None
    op = own["op"] - non_ebg_op
    return {"rev": e["rev"], "op": op, "opm": op / e["rev"] * 100, "basis": "IR 추정(자체사업 영업이익 - 비전자 몫)", "own": own}


def flags_of(text: str) -> dict:
    t = re.sub(r"\s+", " ", text or "")
    delay = re.search(r"(증설|CAPA|캐파|설비 ?투자|신규 ?투자)[^.\n]{0,40}(연기|지연|재검토)|(연기|지연|재검토)[^.\n]{0,40}(증설|CAPA|캐파|설비 ?투자)", t)
    fin = re.search(r"인수 ?금융[^.\n]{0,80}(금리 ?상향|추가 ?담보|보증|조건 ?불리)|(금리 ?상향|추가 ?담보|조건 ?불리)[^.\n]{0,80}인수 ?금융", t)
    return {"delay": delay.group(0) if delay else None, "fin": fin.group(0) if fin else None}


def ir_deck_url(html: str, row_title: str) -> str | None:
    for m in re.finditer(r'<td class="title">\s*([^<]*?)\s*</td>(.*?)</tr>', html, re.S):
        if re.sub(r"\s+", "", m.group(1)) == re.sub(r"\s+", "", row_title):
            a = re.search(r'href="(/file/down/[^"]+)"[^>]*>\s*<span>국문', m.group(2))
            if a:
                return "https://www.doosan.com" + a.group(1)
    return None


def pdf_text(raw: bytes) -> str:
    with tempfile.NamedTemporaryFile(suffix=".pdf") as f:
        f.write(raw)
        f.flush()
        if not shutil.which("pdftotext"):
            raise RuntimeError("pdftotext 없음(poppler-utils)")
        out = subprocess.run(["pdftotext", "-layout", "-enc", "UTF-8", f.name, "-"], capture_output=True, timeout=120)
        if out.returncode != 0:
            raise RuntimeError(f"pdftotext rc {out.returncode}")
        return out.stdout.decode("utf-8", "replace")


# ---------------- DART 분기보고서 부문별 정보 ----------------
def seg_first(text: str, labels: tuple[str, ...]) -> float | None:
    tk = [x.strip() for x in text.split("|") if x.strip()]
    for i, x in enumerate(tk):
        if x in labels and i + 1 < len(tk):
            v = tk[i + 1]
            if re.match(r"^\(?-?[\d,]+\)?$", v):
                n = float(v.strip("()").replace(",", ""))
                return -n if v.startswith("(") else n
    return None


def parse_segment(text: str) -> dict:
    """첫 영업부문 표(당분기 또는 당반기 누적)의 전자BG 매출, 영업이익(억원). 전자BG 가 첫 열인지 확인."""
    i = text.find("영업부문에 대한 공시 | 당")
    if i < 0:
        raise ValueError("영업부문 표 없음")
    m2 = re.compile(r"\| 전(분기|반기|기)(누적)? \|").search(text, i + 30)
    part = text[i: m2.start() if m2 else len(text)]
    hdr = re.search(r"\| (전자BG|디지털이노베이션BU|두산에너빌리티|두산밥캣\(주\)) \|", part)
    if not hdr or hdr.group(1) != "전자BG":
        raise ValueError("전자BG 첫 열 아님")
    unit = {"백만원": 100.0, "천원": 100000.0, "원": 1e8}[re.search(r"단위 : (백만원|천원|원)", part).group(1)]
    rev = seg_first(part, ("수익(매출액)", "영업수익", "매출액"))
    op = seg_first(part, ("영업이익(손실)", "영업이익", "영업손익"))
    if rev is None or op is None:
        raise ValueError("전자BG 매출/영업이익 행 없음")
    return {"rev": rev / unit, "op": op / unit}


def dart_rows(cfg: dict, now: datetime) -> list[dict]:
    data = urllib.parse.urlencode({"textCrpNm": "두산", "textCrpCik": cfg["dart_cik"], "startDate": "20261101",
                                   "endDate": now.strftime("%Y%m%d"), "maxResults": 100, "currentPage": 1, "sort": "date",
                                   "series": "desc"}).encode()
    raw = http(f"{DART}/dsab007/detailSearch.ax", data).decode("utf-8", "replace")
    out = []
    for row in re.findall(r"<tr>.*?</tr>", raw, re.S):
        m = re.search(r'rcpNo=(\d+)"[^>]*>(.*?)</a>', row, re.S)
        f = re.search(r'class="tL ellipsis" title="([^"]*)"', row)
        if m and f and f.group(1).strip() == "두산":
            out.append({"rcp": m.group(1), "title": re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", m.group(2))).strip()})
    if "rcpNo=" not in raw and "조회 결과가 없습니다" not in raw and "검색된 자료가 없습니다" not in raw:
        raise ValueError("DART 목록 형식 확인 불가")
    return out


def dart_segment(cfg: dict, now: datetime) -> dict | None:
    rows = [r for r in dart_rows(cfg, now) if "분기보고서 (2026.09)" in r["title"]]
    rows = [r for r in rows if "정정" not in r["title"]] or rows
    if not rows:
        return None
    rcp = rows[-1]["rcp"]
    main = http(f"{DART}/dsaf001/main.do?rcpNo={rcp}").decode("utf-8", "replace")
    for m in re.finditer(r"node\d\['text'\] = \"([^\"]*)\";.*?node\d\['dcmNo'\] = \"(\d+)\";\s*node\d\['eleId'\] = \"(\d+)\";\s*"
                         r"node\d\['offset'\] = \"(\d+)\";\s*node\d\['length'\] = \"(\d+)\";\s*node\d\['dtd'\] = \"([^\"]*)\"", main, re.S):
        if "부문별 정보" in m.group(1) or "부문정보" in m.group(1):
            u = (f"{DART}/report/viewer.do?rcpNo={rcp}&dcmNo={m.group(2)}&eleId={m.group(3)}&offset={m.group(4)}"
                 f"&length={m.group(5)}&dtd={m.group(6)}")
            seg = parse_segment(flat(http(u).decode("utf-8", "replace")))
            h1 = cfg["h1_2026"]
            rev, op = seg["rev"] - h1["rev"], seg["op"] - h1["op"]
            return {"rev": rev, "op": op, "opm": op / rev * 100, "basis": "DART 분기보고서 부문별 정보(9개월 - 상반기)",
                    "link": f"{DART}/dsaf001/main.do?rcpNo={rcp}"}
    raise ValueError("분기보고서에 부문별 정보 절 없음")


def flat(h: str) -> str:
    import html as _h
    h = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", h)
    t = _h.unescape(re.sub(r"<[^>]+>", " | ", h)).replace("\xa0", " ")
    return re.sub(r"(\s*\|\s*)+", " | ", re.sub(r"\s+", " ", t))


def http(url: str, data: bytes | None = None, timeout: int = 30) -> bytes:
    with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=UA), timeout=timeout) as r:
        return r.read()


# ---------------- 판정 ----------------
def round_man(x: float) -> int:
    return int(math.floor(x + 0.5))


def judge(cfg: dict, f: dict, flags: dict) -> dict:
    r = cfg["rules"]
    op, opm, rev = f["op"], f["opm"], f["rev"]
    hard = []
    if op < r["op_hold"]:
        hard.append(f"영업이익 {op:,.0f}억 < {r['op_hold']:,}억")
    if opm < r["opm_hold"]:
        hard.append(f"OPM {opm:.1f}% < {r['opm_hold']:.0f}%")
    if flags.get("delay"):
        hard.append(f"증설 지연 문구('{flags['delay'][:40]}')")
    if flags.get("fin"):
        hard.append(f"인수금융 불리 문구('{flags['fin'][:40]}')")
    if hard:
        return {"verdict": "보류", "why": hard, "shift": None}
    if op >= r["op_keep"] and opm >= r["opm_keep"]:
        return {"verdict": "유지", "why": [f"영업이익 {op:,.0f}억 >= {r['op_keep']:,}억", f"OPM {opm:.1f}% >= {r['opm_keep']:.0f}%"], "shift": None}
    gap_op = max(0.0, r["op_keep"] - op)
    gap_opm = max(0.0, (r["opm_keep"] - opm) / 100 * rev)
    gap = max(gap_op, gap_opm)
    annual = gap * 4
    s_low = round_man(annual / 1000 * r["shift_per_1000_low_man"])
    s_peer_lo = annual / 1000 * peer_per_1000(cfg, r["peer_mult"][0])
    s_peer_hi = annual / 1000 * peer_per_1000(cfg, r["peer_mult"][1])
    why = ([f"영업이익 {op:,.0f}억, 2,000억 대비 {gap_op:,.0f}억 부족"] if gap_op else []) + \
          ([f"OPM {opm:.1f}%, 28% 대비 매출 기준 {gap_opm:,.0f}억 부족" + (" (영업이익은 2,000억 이상이라 OPM 부족분을 영업이익으로 환산)" if not gap_op else "")] if gap_opm else [])
    return {"verdict": "조정", "why": why, "shift": {"gap": gap, "annual": annual, "low": s_low,
                                                    "peer": (round_man(s_peer_lo), round_man(s_peer_hi))}}


def peer_per_1000(cfg: dict, mult: float) -> float:
    """연간 영업이익(대략 EBITDA) 1,000억 변화 x EV/EBITDA 배수 / 유통주식수, 만원/주."""
    return 1000e8 * mult / cfg["shares_circulating"] / 1e4


def position(close: float | None, lo: int, hi: int) -> str:
    if close is None:
        return "종가 확인 불가"
    if close < lo * 1e4:
        return "구간 아래, 첫 분할매수 검토(실적 확인 후)"
    if close <= hi * 1e4:
        return "첫 분할매수 검토"
    return "구간 위, 대기"


CHANGE = {
    "유지": ["분기보고서 확정 전자BG 영업이익이 2,000억 미만 또는 OPM 28% 미만이면 조정",
           "증설 연기, 지연, 재검토 문구가 나오면 보류",
           "SK실트론 인수금융이 금리 상향, 추가 담보, 보증 등 불리한 조건으로 나오면 보류"],
    "조정": ["확정치가 2,000억 이상 + OPM 28% 이상이면 75~80만 유지로 복귀",
           "영업이익 1,700억 미만 또는 OPM 25% 미만이면 보류",
           "증설 지연 문구나 인수금융 불리 조건이 나오면 보류"],
    "보류": ["다음 분기 전자BG 2,000억 이상 + OPM 28% 이상 회복, 지연/금융 문제 없음이면 유지 재검토",
           "1,700~2,000억 또는 OPM 25~28% 범위면 조정 구간으로 재판정",
           "인수금융 조건(약 2.5조, KDB/우리, 연말 약정 목표)이 공개되고 불리 조건이 없으면 재판정"],
}


def message(cfg: dict, f: dict, flags: dict, close: tuple[str, float] | None, links: list[str], correction_of: str | None = None) -> tuple[str, str, int, str]:
    r = cfg["rules"]
    j = judge(cfg, f, flags)
    v = j["verdict"]
    lo, hi = r["zone"]
    c = close[1] if close else None
    ctxt = f"{close[0]} KRX 정규장 종가 {close[1]:,.0f}원" if close else "정규장 종가 확인 불가"
    ref = cfg["ref"]
    head = (f"전자BG 3Q26 매출 {f['rev']:,.0f}억, 영업이익 {f['op']:,.0f}억, OPM {f['opm']:.1f}%({f['basis']}). "
            f"2Q26 {ref['op_2q26']:,}억/{ref['opm_2q26']}%, 컨센서스 약 {ref['cons_op']:,}억/{ref['cons_opm']}% 대비 {f['op'] - ref['cons_op']:+,.0f}억.")
    if f.get("own"):
        head += f" 자체사업 영업이익 {f['own']['op']:,.0f}억(OPM {f['own']['opm']:.1f}%)."
    if f["basis"].startswith("IR 추정"):
        near = min(abs(f["op"] - r["op_keep"]), abs(f["op"] - r["op_hold"]))
        head += " 전자BG 영업이익은 IR 에 따로 없어 추정(2Q26 기준 비전자 몫 107억 차감)." + (" 기준선과 100억 안쪽이라 분기보고서 확정치로 다시 봄." if near < 100 else "")
    if v == "유지":
        title_v, pri = f"{lo}~{hi}만 구간 유지", 3
        zone_txt = f"첫 분할매수 구간 {lo}~{hi}만 유지. {ctxt}: {position(c, lo, hi)}."
    elif v == "조정":
        s = j["shift"]
        nlo, nhi = lo - s["low"], hi - s["low"]
        plo, phi = lo - s["peer"][1], hi - s["peer"][1]
        title_v, pri = f"구간 조정 {nlo}~{nhi}만", 4
        zone_txt = (f"구간 조정: 부족분 {s['gap']:,.0f}억 x 4 = 연 {s['annual']:,.0f}억, 1,000억당 {r['shift_per_1000_low_man']}만(약 8.5배) 적용 "
                    f"-{s['low']}만 -> {nlo}~{nhi}만. 피어 배수(EV/EBITDA {r['peer_mult'][0]:g}~{r['peer_mult'][1]:g}배, 유통주식 "
                    f"{cfg['shares_circulating']:,}주, 1,000억당 {peer_per_1000(cfg, r['peer_mult'][0]):.1f}~{peer_per_1000(cfg, r['peer_mult'][1]):.1f}만) "
                    f"적용 시 -{s['peer'][0]}~{s['peer'][1]}만, 보수 하단 구간 {plo}~{phi}만. {ctxt}: {position(c, nlo, nhi)}.")
    else:
        title_v, pri = "75~80만 구간 보류", 4
        zone_txt = f"첫 분할매수 보류(구간 {lo}~{hi}만 적용 안 함). {ctxt}. 아래 조건이 풀릴 때 다시 판정."
    fin_txt = "" if flags.get("fin") else " 인수금융 조건 미공개, 확인 필요(SK실트론 70.6% 2.3조, 약 2.5조 패키지 KDB/우리, 연말 약정 목표)."
    delay_txt = "" if flags.get("delay") else " 증설 지연 문구 없음."
    text = (f"{head} 판정 {v}: {', '.join(j['why'])}. {zone_txt}{delay_txt}{fin_txt} "
            f"판정이 바뀌는 조건: 1) {CHANGE[v][0]} 2) {CHANGE[v][1]} 3) {CHANGE[v][2]}. 기준: 유지 = 영업이익 2,000억 이상 + OPM 28% 이상, "
            f"조정 = 1,700~2,000억 또는 OPM 25~28%, 보류 = 1,700억 미만 또는 OPM 25% 미만 또는 지연/금융 문구. 자료: {' '.join(links)}")
    if correction_of:
        text = f"정정: 앞서 IR 기준 {correction_of}로 보냈으나 분기보고서 확정치로 {v}. " + text
        title = f"{TAG} 두산 3Q26 전자BG 확정 정정 | {title_v}"
        pri = 4
    else:
        title = f"{TAG} 두산 3Q26 전자BG | {title_v}"
    return title, text, pri, v


def body(stamp: str, text: str) -> str:
    return "\n".join([
        f"{stamp} | 000150 | 두산",
        "이게 뭐 알림: 두산 3분기 전자BG 영업이익과 OPM 으로 첫 분할매수 75~80만원 구간을 유지, 조정, 보류 중 하나로 판정. 매매 지시 아님.",
        f"지금 결과/상태: {text}",
        "하지 말 것: 콜 직후 시간외 가격으로 바로 주문, 한 번에 전액 매수, 인수금융 조건 확인 전 비중 확대.",
    ])


# ---------------- 실행 ----------------
def plan(cfg: dict, st: dict, now: datetime, io: dict) -> tuple[list[tuple[str, str, int]], dict]:
    out: list[tuple[str, str, int]] = []
    errs: dict = {}
    start = datetime.strptime(cfg["release_kst"], "%Y-%m-%d %H:%M").replace(tzinfo=KST)
    if now < start:
        return out, errs
    links = [IR_URL]
    seg = None
    if not st.get("confirmed"):
        try:
            seg = io["segment"]()
        except Exception as exc:  # noqa: BLE001
            errs["dart"] = str(exc)[:200]
        if seg:
            st["confirmed"] = {k: seg[k] for k in ("rev", "op", "opm")}
            links.append(seg["link"])
    if st.get("sent") and (st["sent"].get("basis", "").startswith("DART") or st.get("correction_sent") or (st.get("confirmed") and not seg)):
        return out, errs
    deck, deck_text = None, ""
    if not st.get("sent"):
        try:
            deck_text = io["deck"]() or ""
        except Exception as exc:  # noqa: BLE001
            errs["ir"] = str(exc)[:200]
        if deck_text and not seg:
            deck = parse_deck(deck_text, cfg["qlabel"], cfg["non_ebg_op_2q26"])
    flags = flags_of(deck_text)
    close = None
    try:
        close = io["close"]()
    except Exception as exc:  # noqa: BLE001
        errs["close"] = str(exc)[:200]
    if st.get("sent"):
        if seg:
            t, x, p, v = message(cfg, seg, st["sent"].get("flags") or {}, close, links, correction_of=st["sent"]["verdict"])
            if v != st["sent"]["verdict"]:
                out.append((t, x, p))
                st["correction_sent"] = True
        return out, errs
    f = seg or deck
    if f:
        t, x, p, v = message(cfg, f, flags, close, links)
        out.append((t, x, p))
        st["sent"] = {"verdict": v, "basis": f["basis"], "op": round(f["op"]), "opm": round(f["opm"], 1), "at": now.isoformat(), "flags": flags}
        return out, errs
    released = bool(deck_text)
    try:
        released = released or bool(io["notice"]())
    except Exception as exc:  # noqa: BLE001
        errs["notice"] = str(exc)[:200]
    end = datetime.strptime(cfg["last_run_kst"], "%Y-%m-%d %H:%M").replace(tzinfo=KST)
    if released and not st.get("manual_sent"):
        st["manual_sent"] = now.isoformat()
        out.append((f"{TAG} 두산 3Q26 실적 나옴 | 전자BG 값 자동 판독 안 됨, 수동 확인",
                    f"3분기 실적은 나왔는데 IR 자료에서 전자BG 매출/영업이익을 못 읽음. IR 자료와 공시에서 직접 확인: {IR_URL} , "
                    f"{DART}/dsab007/main.do (두산 연결재무제표기준영업(잠정)실적). 다음 실행(평일 09:00, 17:05, 11/16까지)에서 다시 시도.", 3))
    elif now >= end and not st.get("ops_sent"):
        st["ops_sent"] = now.isoformat()
        out.append((f"{TAG} 두산 3Q26 전자BG 확인 불가 | 11/16까지 판정 못 함",
                    f"11/16 마지막 실행까지 IR 자료와 분기보고서에서 전자BG 값을 못 읽음. 판정 없이 종료. 직접 확인: {IR_URL}", 3))
    return out, errs


def fail_alerts(cfg: dict, st: dict, errs: dict, today: str) -> list[tuple[str, str, int]]:
    out = []
    names = {"ir": "두산 IR 자료", "dart": "DART 분기보고서", "close": "KRX 정규장 종가", "notice": "잠정 공시 목록"}
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
                            f"{names[key]} 조회가 {f['days']}일 연속 실패({errs[key]}). 판정이 없다는 뜻 아님.", 3))
        else:
            f = {"days": 0, "last": None, "notified": False}
        fails[key] = f
    return out


def live_io(cfg: dict, now: datetime) -> dict:
    def deck():
        html = http(IR_URL).decode("utf-8", "replace")
        url = ir_deck_url(html, cfg["ir_row_title"])
        return pdf_text(http(url)) if url else ""

    def close():
        import krx_close as kx
        bars = kx.regular_bars("000150", 8)
        today = now.strftime("%Y-%m-%d")
        if bars and bars[-1].day == today and now.strftime("%H:%M") < "15:35":
            bars = bars[:-1]
        if not bars:
            raise RuntimeError("정규장 종가 없음")
        return bars[-1].day, float(bars[-1].close)

    def notice():
        from naver_api import notices
        return [r for r in notices("000150", 30) if "잠정" in r.get("title", "") and "정정" not in r.get("title", "")
                and str(r.get("datetime", ""))[:10] >= cfg["release_kst"][:10]]
    return {"deck": deck, "segment": lambda: dart_segment(cfg, now), "close": close, "notice": notice}


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    if mode == "selftest":
        return selftest()
    if mode not in {"auto", "dry"}:
        print(f"unknown mode {mode}")
        return 2
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    if mode == "dry" and os.environ.get("DSZONE_NOW"):
        now = datetime.strptime(os.environ["DSZONE_NOW"], "%Y-%m-%d %H:%M").replace(tzinfo=KST)
    if now > datetime.strptime(cfg["ends_kst"], "%Y-%m-%d %H:%M").replace(tzinfo=KST):
        print("ended")
        return 0
    dry = mode == "dry"
    topic = resolve_topic("port", cfg.get("ntfy_topic"))
    server = resolve_server(cfg.get("ntfy_server"))
    st = load_state()
    try:
        alerts, errs = plan(cfg, st, now, live_io(cfg, now))
    except Exception as exc:  # noqa: BLE001  (스텝의 다른 알림이 막히지 않게)
        alerts, errs = [], {"ir": f"crash {exc}"[:200]}
    if errs:
        print("errors: " + json.dumps(errs, ensure_ascii=False), file=sys.stderr)
    alerts += fail_alerts(cfg, st, errs, now.date().isoformat())
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    for title, text, pri in alerts:
        print(f"TITLE: {title}\nPRIORITY: {pri} TOPIC: {route_topic(topic, title)}\n{body(stamp, text)}\n")
        if not dry:
            publish(topic, title, body(stamp, text), pri, ["office"], server)
    if not alerts:
        print(f"no alert ({now.strftime('%m-%d %H:%M')}, release {cfg['release_kst']})")
    if dry:
        print("DRY RUN: no publish, state not written")
        return 0
    st["updated_at"] = now.isoformat()
    STATE_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


def selftest() -> int:
    import copy
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    fails: list[str] = []

    def check(name, got, want):
        ok = got == want
        print(("ok   " if ok else "FAIL ") + f"{name}: got={got!r} want={want!r}")
        if not ok:
            fails.append(name)
    deck2 = (FX / "ir_2q26_layout.txt").read_text(encoding="utf-8")
    dj = json.loads((FX / "dart.json").read_text(encoding="utf-8"))
    irhtml = (FX / "ir_list_2q26.html").read_text(encoding="utf-8")
    R = lambda d: {k: round(v, 1) for k, v in d.items()}  # noqa: E731
    # 실제 2Q26 IR 차트
    ep = find_page(deck2, "전자BG 실적및", "분기별매출액추이")
    op_page = find_page(deck2, "자체사업실적및", "분기별실적추이")
    check("real 2Q26 IR: 전자BG rev by quarter column", [chart_point(ep, q)["rev"] for q in ("25.2Q", "25.3Q", "25.4Q", "26.1Q", "26.2Q")],
          [4762.0, 4398.0, 5567.0, 6173.0, 6760.0])
    check("real 2Q26 IR: 자체사업 rev/OP/OPM 26.2Q and 26.1Q", (chart_point(op_page, "26.2Q"), chart_point(op_page, "26.1Q")),
          ({"rev": 7652.0, "op": 2120.0, "opm": 27.7}, {"rev": 7023.0, "op": 1878.0, "opm": 26.7}))
    d2 = parse_deck(deck2, "26.2Q", cfg["non_ebg_op_2q26"])
    check("real 2Q26 IR -> 전자BG estimate 2,013억 / 29.8% (matches DART)", (d2["rev"], d2["op"], round(d2["opm"], 1), d2["basis"][:5]), (6760.0, 2013.0, 29.8, "IR 추정"))
    check("26.3Q label absent in 2Q deck -> None", parse_deck(deck2, "26.3Q", 107), None)
    check("IR list: 2/4분기 국문 PDF link, 3/4분기 not yet", (ir_deck_url(irhtml, "2/4분기 ㈜두산 경영실적"), ir_deck_url(irhtml, cfg["ir_row_title"])),
          ("https://www.doosan.com/file/down/6219bb5b-812c-47c7-8847-39944c89e030", None))
    s1q, sh1, s3q25 = (parse_segment(dj["segments"][k]) for k in ("20260515001456", "20260814004047", "20251114001452"))
    check("real DART segments 1Q26 / 1H26 / 9M25 전자BG", (R(s1q), R(sh1), R(s3q25)),
          ({"rev": 6173.4, "op": 1856.4}, {"rev": 12933.5, "op": 3869.1}, {"rev": 13185.4, "op": 3562.1}))
    check("2Q26 = 1H - 1Q = 2,013억 / 29.8%", (round(sh1["op"] - s1q["op"]), round((sh1["op"] - s1q["op"]) / (sh1["rev"] - s1q["rev"]) * 100, 1)), (2013, 29.8))
    check("config h1 equals parsed", (round(sh1["rev"], 2), round(sh1["op"], 2)), (cfg["h1_2026"]["rev"], cfg["h1_2026"]["op"]))
    check("real 2Q deck: no delay / financing wording", flags_of(deck2), {"delay": None, "fin": None})
    check("flag detectors", (bool(flags_of("진천 증설 일정이 내년으로 연기될 예정")["delay"]), bool(flags_of("CAPA 확대 재검토")["delay"]),
                             bool(flags_of("인수금융 금리 상향 조건")["fin"]), bool(flags_of("인수금융은 연말 약정 목표")["fin"])), (True, True, True, False))
    F = lambda op, opm, rev=None: {"rev": rev or op / opm * 100, "op": op, "opm": opm, "basis": "t"}  # noqa: E731
    nf = {"delay": None, "fin": None}
    check("judge 유지 / 조정 / 보류", [judge(cfg, F(*a), nf)["verdict"] for a in ((2000, 28.0), (2100, 29.5), (1999, 29), (2050, 27.9), (1700, 25.0),
                                                                            (1699, 29), (2100, 24.9))],
          ["유지", "유지", "조정", "조정", "조정", "보류", "보류"])
    check("judge delay / financing flag -> 보류", (judge(cfg, F(2300, 31), {"delay": "증설 연기", "fin": None})["verdict"],
                                                 judge(cfg, F(2300, 31), {"delay": None, "fin": "인수금융 금리 상향"})["verdict"]), ("보류", "보류"))
    j = judge(cfg, F(1850, 27.0, 6850), nf)
    check("조정 OP 1,850: gap 150 x4 = 600, low shift 4만, peer 5~8만", (j["shift"]["low"], j["shift"]["peer"], round(peer_per_1000(cfg, 12), 1), round(peer_per_1000(cfg, 18), 1)),
          (4, (5, 8), 8.9, 13.3))
    j = judge(cfg, F(2050, 27.0, 7593), nf)
    check("조정 OPM-only (2,050억, 27%): OPM gap 76억 x4 -> shift 2만", (round(j["shift"]["gap"]), j["shift"]["low"], "환산" in j["why"][0]), (76, 2, True))
    check("price position (11/5 regular 719,000)", (position(719000, 75, 80), position(770000, 75, 80), position(800000, 75, 80), position(820000, 75, 80), position(719000, 71, 76)),
          ("구간 아래, 첫 분할매수 검토(실적 확인 후)", "첫 분할매수 검토", "첫 분할매수 검토", "구간 위, 대기", "첫 분할매수 검토"))
    cl = ("2026-11-09", 719000.0)
    t, x, p, v = message(cfg, F(2150, 29.5, 7288), nf, cl, [IR_URL])
    check("유지 message: title, p3, zone kept, price below", (t, p, "구간 아래, 첫 분할매수 검토(실적 확인 후)" in x, "인수금융 조건 미공개, 확인 필요" in x, x.count("판정이 바뀌는 조건: 1)") == 1 and " 3) " in x),
          (f"{TAG} 두산 3Q26 전자BG | 75~80만 구간 유지", 3, True, True, True))
    t, x, p, v = message(cfg, F(1850, 27.0, 6850), nf, cl, [IR_URL])
    check("조정 message: title new zone, p4, peer conservative zone", (t, p, "보수 하단 구간 67~72만" in x, "-> 71~76만" in x), (f"{TAG} 두산 3Q26 전자BG | 구간 조정 71~76만", 4, True, True))
    t, x, p, v = message(cfg, F(1650, 24.0), nf, cl, [IR_URL])
    check("보류 message: p4", (t, p), (f"{TAG} 두산 3Q26 전자BG | 75~80만 구간 보류", 4))

    # ---- 흐름 ----
    st0 = default_state()
    st0.update(json.loads(STATE_PATH.read_text(encoding="utf-8")))
    cfgt = copy.deepcopy(cfg)
    cfgt["qlabel"] = "26.2Q"  # 실제 2Q 자료를 3Q 처럼 씀
    calls = []

    def io(deck=deck2, seg=None, notice=(), close=cl):
        def dk():
            calls.append("deck")
            if isinstance(deck, Exception):
                raise deck
            return deck
        return {"deck": dk, "segment": lambda: seg, "notice": lambda: list(notice), "close": lambda: close}
    at = lambda s_: datetime.strptime(s_, "%Y-%m-%d %H:%M").replace(tzinfo=KST)  # noqa: E731
    st = copy.deepcopy(st0)
    check("11/9 15:40 (before 16:00): silent, no fetch", (plan(cfgt, st, at("2026-11-09 15:40"), io()), calls), (([], {}), []))
    a, e = plan(cfgt, st, at("2026-11-09 17:05"), io())
    check("11/9 17:05 IR parsed: one 유지 message p3 (2Q-like numbers)", ([x[0] for x in a], a[0][2], e, st["sent"]["verdict"], st["sent"]["op"]),
          ([f"{TAG} 두산 3Q26 전자BG | 75~80만 구간 유지"], 3, {}, "유지", 2013))
    check("estimate wording in body", "전자BG 영업이익은 IR 에 따로 없어 추정" in a[0][1], True)
    check("next run silent", plan(cfgt, st, at("2026-11-10 09:00"), io())[0], [])
    seg_low = {"rev": 6900.0, "op": 1880.0, "opm": 1880 / 6900 * 100, "basis": "DART 분기보고서 부문별 정보(9개월 - 상반기)", "link": "L"}
    a, _ = plan(cfgt, st, at("2026-11-16 09:00"), io(seg=seg_low))
    check("DART confirms lower (1,880억) -> one correction 조정 p4", ([x[0] for x in a], a[0][2], a[0][1].startswith("정정: 앞서 IR 기준 유지로")),
          ([f"{TAG} 두산 3Q26 전자BG 확정 정정 | 구간 조정 72~77만"], 4, True))
    check("after correction silent", plan(cfgt, st, at("2026-11-16 17:05"), io(seg=seg_low))[0], [])
    st = copy.deepcopy(st0)
    plan(cfgt, st, at("2026-11-09 17:05"), io())
    seg_same = dict(seg_low, op=2050.0, rev=7000.0, opm=2050 / 7000 * 100)
    check("DART confirms same verdict -> silent, confirmed stored", (plan(cfgt, st, at("2026-11-13 17:05"), io(seg=seg_same))[0], round(st["confirmed"]["op"])), ([], 2050))
    st = copy.deepcopy(st0)
    a, _ = plan(cfgt, st, at("2026-11-09 17:05"), io(deck="", notice=[{"title": "잠정"}]))
    check("잠정 out but IR deck not posted/parsed: one manual-check notice (port)", ([x[0] for x in a], route_topic("cwally-port", a[0][0])),
          ([f"{TAG} 두산 3Q26 실적 나옴 | 전자BG 값 자동 판독 안 됨, 수동 확인"], "cwally-port"))
    check("manual notice not repeated", plan(cfgt, st, at("2026-11-10 09:00"), io(deck="", notice=[{"title": "잠정"}]))[0], [])
    a, _ = plan(cfgt, st, at("2026-11-10 17:05"), io())
    check("later run parses IR -> verdict sent", [x[0] for x in a], [f"{TAG} 두산 3Q26 전자BG | 75~80만 구간 유지"])
    st = copy.deepcopy(st0)
    check("nothing out yet (no 잠정, no deck): silent", plan(cfgt, st, at("2026-11-12 09:00"), io(deck=""))[0], [])
    a, _ = plan(cfgt, st, at("2026-11-16 17:05"), io(deck=""))
    check("11/16 17:05 still nothing: ops notice once", ([x[0] for x in a], route_topic("cwally-port", a[0][0]), plan(cfgt, st, at("2026-11-16 17:30"), io(deck=""))[0]),
          ([f"{TAG} 두산 3Q26 전자BG 확인 불가 | 11/16까지 판정 못 함"], "cwally-ops", []))
    st = copy.deepcopy(st0)
    dly = deck2.replace("신규증설설비양산을통해", "신규증설설비양산 일정 연기로")
    a, _ = plan(cfgt, st, at("2026-11-09 17:05"), io(deck=dly))
    check("IR text with 증설 연기 wording -> 보류 p4", ([x[0] for x in a], a[0][2]), ([f"{TAG} 두산 3Q26 전자BG | 75~80만 구간 보류"], 4))
    st = copy.deepcopy(st0)
    a, _ = plan(cfgt, st, at("2026-11-09 17:05"), io(close=None))
    check("close unavailable: still sends, says 종가 확인 불가", ("정규장 종가 확인 불가" in a[0][1], len(a)), (True, 1))
    st = copy.deepcopy(st0)
    _, e1 = plan(cfgt, st, at("2026-11-09 17:05"), io(deck=RuntimeError("timeout"), notice=[]))
    f1 = fail_alerts(cfg, st, e1, "2026-11-09")
    f2 = fail_alerts(cfg, st, e1, "2026-11-10")
    f3 = fail_alerts(cfg, st, e1, "2026-11-10")
    check("IR fetch failure: ops on 2nd day once", (f1, [x[0] for x in f2], f3), ([], [f"{TAG} 두산 IR 자료 조회 실패 | 2일 연속, 다음 실행에서 다시 시도"], []))
    check("routing failure -> ops", route_topic("cwally-port", f2[0][0]), "cwally-ops")
    allt = []
    for args in ((F(2150, 29.5, 7288), nf), (F(1850, 27.0, 6850), nf), (F(1650, 24.0), {"delay": "증설 연기", "fin": "인수금융 금리 상향"})):
        t, x, _, _ = message(cfg, *args, cl, [IR_URL])
        allt.append(t + body("s", x))
    text = " ".join(allt) + json.dumps(cfg, ensure_ascii=False) + Path(__file__).read_text(encoding="utf-8")
    check("titles contain [000150] first, no middle dot or em-dash", (all(a.startswith(TAG) for a in allt), "\u00b7" in text, "\u2014" in text), (True, False, False))
    check("ends 2026-11-16 23:59, release 11/9 16:00", (cfg["ends_kst"], cfg["release_kst"]), ("2026-11-16 23:59", "2026-11-09 16:00"))
    print(f"SELFTEST {'PASS' if not fails else 'FAIL'}: {len(fails)} failed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
