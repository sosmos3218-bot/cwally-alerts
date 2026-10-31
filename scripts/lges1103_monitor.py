#!/usr/bin/env python3
"""LG Energy Solution 2026-11-03 confirmed Q3 print + Q3 earnings-call verdict. One-shot, year-guarded.

Part 1 (results, unchanged rules): 11/3 runs read the KRX/DART notice and send one
"[LGESF] 373220 LG엔솔 3Q 확정 <매수 검토|관망|보류>" (new-buy labels since 2026-10-27, same thresholds:
상향 -> 매수 검토, 유지 -> 관망, 판정 유보 -> 관망, 철회 -> 보류).
Part 2 (call verdict, approved in chat 2026-10-27, user does not hold 373220 = new-buy framing):
from 11/3 11:00 KST (call 10:00) reads the IR script/presentation PDFs, the LG press release,
11/3 notices and call news, then sends one "[LGESF] 373220 실적콜 판정: <매수 검토|관망|보류|확인 필요>".
  - 4Q ex-credit (보조금 제외) profit guidance: positive / negative / mixed or aspirational / not stated.
  - 3Q one-off (customer compensation etc.) as % of 3Q ex-credit OP (3,391억 prelim) and of reported OP.
  매수 검토 = guidance positive AND (one-off < 50% of ex-credit OP OR stated immaterial)
  보류      = guidance negative OR one-off >= ex-credit OP
  관망      = everything else (incl. one item undisclosed / not stated / aspirational only)
  확인 필요 = no call source found or none parseable by the last run (ops notice, routes to cwally-ops)
Modes: auto | force | dry [--now "YYYY-MM-DD HH:MM"] (print only, no ntfy, no state) | selftest.
"""
from __future__ import annotations

import html as htmllib
import json
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
from naver_api import html_text, notices  # noqa: E402
from ntfy_util import publish, resolve_server, resolve_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
CFG_PATH = ROOT / "configs" / "lges1103.json"
STATE_PATH = ROOT / "state" / "lges1103.json"
DISCLOSURES = "https://www.lgensol.com/kr/investors/electronic-disclosure"


def read_state() -> dict:
    if not STATE_PATH.exists():
        return {"result_notified": False, "empty_notified": False, "last_problem_key": None}
    return json.loads(STATE_PATH.read_text(encoding="utf-8"))


def save_state(state: dict, now: datetime) -> None:
    state["updated_at"] = now.isoformat(timespec="seconds")
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def send(cfg: dict, title: str, lines: list[str]) -> None:
    publish(
        resolve_topic("port", cfg.get("ntfy_topic")), title, "\n".join(lines), 4,
        ["chart_with_upwards_trend"], resolve_server(cfg.get("ntfy_server")),
    )


def eok(text: str, pattern: str) -> int | None:
    hit = re.search(pattern, text, re.I)
    if not hit:
        return None
    raw = int(hit.group(1).replace(",", ""))
    unit = hit.group(2)
    if unit.startswith("백만"):
        raw = raw // 100
    if not -200000 < raw < 300000:
        return None
    return raw


def grab(text: str, label: str) -> int | None:
    return eok(text, label + r"[^0-9\-]{0,24}(-?[\d,]+)\s*(억원|백만원)")


def is_final(item: dict) -> bool:
    title = item.get("title", "")
    body = item.get("text", "")
    blob = title + " " + body
    if not re.search(r"엘지에너지솔루션|LG에너지솔루션|에너지솔루션", blob):
        return False
    if not re.search(r"2026[\.\-/ ]*3분기|2026-07-01|2026\.07\.01|'?26\.3Q|3분기", blob):
        return False
    return bool(re.search(r"실적|기업설명회|컨퍼런스|영업이익", title + body[:400]))


def parse(item: dict) -> dict:
    text = item.get("title", "") + "\n" + item.get("text", "")
    op = grab(text, r"영업이익")
    sales = grab(text, r"매출액")
    net = grab(text, r"당기순이익|순이익")
    ampc = grab(text, r"AMPC|세액공제")
    comp = grab(text, r"보상금|위약금|최소구매")
    ev = grab(text, r"전기차|EV")
    ess = grab(text, r"ESS|에너지저장")
    ocf = grab(text, r"영업활동현금흐름|영업현금흐름")
    ex = None
    if op is not None and ampc is not None:
        ex = op - ampc
    return {
        "sales": sales, "op": op, "net": net, "ampc": ampc, "comp": comp,
        "ev": ev, "ess": ess, "ocf": ocf, "ex": ex,
    }


def fmt(v: int | None) -> str:
    return "확인 불가" if v is None else f"{v:+,}억원"


def verdict(p: dict, prelim: dict) -> tuple[str, str, str]:
    ex, comp, op = p["ex"], p["comp"], p["op"]
    if ex is None or op is None:
        return (
            "관망",
            "영업이익이나 AMPC를 확정 숫자로 분리하지 못해 판단할 수 없습니다.",
            "사지 말고 관망하세요. 공시 원문과 실적콜 판정이 나올 때까지 기다리세요.",
        )
    base_op = int(prelim["op_eok"])
    if ex < 0 or op < int(base_op * 0.7):
        return (
            "보류",
            "AMPC 제외 영업이익이 적자이거나 확정 영업이익이 잠정의 70% 미만입니다.",
            "신규 매수 보류. AMPC 제외 이익이 다시 흑자로 확인되기 전에는 사지 마세요.",
        )
    if comp is not None and (ex - comp) >= 2000 and op >= int(base_op * 0.9):
        return (
            "매수 검토",
            "보상금을 빼도 AMPC 제외 영업이익이 2,000억원 이상이고 확정 영업이익이 잠정의 90% 이상입니다.",
            "신규 매수 검토 가능. 계획 금액의 일부만 나눠서 들어가고, 실적콜 판정과 4분기 잠정실적 전까지 추가는 보류하세요.",
        )
    missing = []
    if comp is None:
        missing.append("보상금 금액")
    if p["ev"] is None or p["ess"] is None:
        missing.append("사업부 손익")
    if p["net"] is None:
        missing.append("순이익")
    if p["ocf"] is None:
        missing.append("현금흐름")
    gap = ", ".join(missing) if missing else "보상금 차감 후 2,000억 미만"
    return (
        "관망",
        f"AMPC 제외 흑자는 남았지만 {gap} 때문에 매수 검토 조건은 아닙니다.",
        "아직 사지 말고 관망하세요. 실적콜 판정이나 4분기 잠정실적을 기다리세요.",
    )


def problem(cfg: dict, state: dict, now: datetime, kind: str, detail: str, dry: bool) -> None:
    key = f"{kind}:{detail}"
    if state.get("last_problem_key") == key:
        print(f"problem already reported: {kind}")
        return
    title = f"[LGESF] 373220 LG엔솔 확정 {kind} | 판정 유보"
    lines = [
        "이게 뭐 알림: LG에너지솔루션 2026년 3분기 확정 실적과 컨퍼런스콜 판정.",
        f"지금 결과/상태: {kind}. {detail}. {now:%Y-%m-%d %H:%M} KST. 공시: {DISCLOSURES}",
        "지금 할 일: 매수 판단을 하지 마세요. 원문 확인 전까지 신규 매수를 보류하세요.",
        ("나중에: 같은 날 16:32 KST에 다시 확인합니다." if (now.hour, now.minute) < (16, 30)
         else "나중에: 같은 날 17:10 KST에 한 번 더 확인합니다. 그 뒤에는 실적콜 판정만 남습니다."),
        "익절/줄일 때: 미보유라 해당 없음.",
        "하지 말 것: 잠정 숫자나 증권사 추정을 확정 실적으로 쓰지 마세요.",
    ]
    if dry:
        print(title, *lines, sep="\n")
        return
    send(cfg, title, lines)
    state["last_problem_key"] = key
    save_state(state, now)


def results_part(cfg: dict, state: dict, now: datetime, mode: str) -> dict | None:
    """Confirmed-results part (behaviour of the original one-shot, 11/3 only). Returns the parse when a filing was found."""
    day = now.strftime("%Y-%m-%d")
    if not cfg["window_start"] <= day <= cfg.get("results_window_end", cfg["window_start"]):
        print("results part: outside 11/3; skip")
        return None
    if state.get("result_notified") and mode != "force":
        print("results part: result already notified; silent")
        return None
    try:
        rows = notices(cfg["ticker"], 30)
    except Exception as exc:
        problem(cfg, state, now, "조회 실패", type(exc).__name__, mode == "dry")
        return None
    matches = [r for r in rows if is_final(r) and "20261103" in r.get("datetime", "").replace("-", "")[:8] or (
        is_final(r) and r.get("datetime", "")[:10] in {"2026-11-03", "2026.11.03", "20261103"}
    )]
    if not matches:
        # Only 11/3 filings (stamp may be UTC or KST). Before 2026-10-27 this also matched the 10/8
        # "기업설명회(IR) 개최(안내공시)" and would have sent "판정 유보" at 09:22.
        matches = [r for r in rows if is_final(r) and "잠정" not in r.get("title", "")
                   and r.get("datetime", "") >= cfg.get("results_min_stamp", "2026-11-02T15:00")]
    if not matches:
        last = now.hour > 16 or (now.hour == 16 and now.minute >= 30)
        if last and not state.get("empty_notified"):
            problem(cfg, state, now, "미게시", "11월 3일 확정 실적 공시를 찾지 못했습니다", mode == "dry")
            if mode != "dry":
                state["empty_notified"] = True
                save_state(state, now)
        else:
            print("final disclosure not posted; silent")
        return None
    item = matches[0]
    p = parse(item)
    label, why, action = verdict(p, cfg["prelim"])
    title = f"[LGESF] 373220 LG엔솔 3Q 확정 {label}"
    lines = [
        "이게 뭐 알림: 373220 2026년 3분기 확정 실적에서 보상금, 사업부 손익, 순이익, 현금흐름을 분리해 신규 매수 여부를 봅니다. 미보유, 참고용, 투자 권유 아님.",
        (
            f"지금 결과/상태: 판정 {label}. {why} "
            f"매출 {fmt(p['sales'])}, 영업이익 {fmt(p['op'])}, AMPC {fmt(p['ampc'])}, "
            f"AMPC 제외 {fmt(p['ex'])}, 보상금 {fmt(p['comp'])}, EV {fmt(p['ev'])}, ESS {fmt(p['ess'])}, "
            f"순이익 {fmt(p['net'])}, 영업현금흐름 {fmt(p['ocf'])}. "
            f"잠정은 매출 96,434억원, 영업이익 7,560억원, AMPC 제외 3,391억원. "
            f"공시 {item.get('id')}. 조회 {now:%Y-%m-%d %H:%M} KST. {DISCLOSURES}"
        ),
        f"지금 할 일: {action}",
        "나중에: 4분기 보조금 제외 가이던스와 일회성 비중은 [LGESF] 실적콜 판정이 따로 봅니다. 4분기 잠정실적(보통 1월 초)에서 AMPC 제외 흑자를 다시 확인하세요.",
        "익절/줄일 때: 미보유라 해당 없음. 사게 되면 AMPC 제외 적자 전환이 확인될 때 비중 재검토.",
        "하지 말 것: 확인 불가 칸을 추정으로 채우기, 잠정 7,560억원을 확정 이익으로 보기, 한 번에 다 사기. 투자 권유 아님.",
    ]
    if mode == "dry":
        print(title, *lines, "dry-run; no ntfy", sep="\n")
        return p
    send(cfg, title, lines)
    state["result_notified"] = True
    state["last_key"] = item.get("id")
    state["verdict"] = label
    state["confirmed_op_eok"] = p["op"]
    state["confirmed_ex_eok"] = p["ex"]
    state["last_problem_key"] = None
    save_state(state, now)
    return p

# ======================= Part 2: Q3 earnings-call verdict (2026-10-27) =======================

UA_WEB = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) cwally-alerts", "Accept": "*/*"}
NEWS_API = "https://m.stock.naver.com/api/news/stock/{code}?pageSize=20&page={page}"
GNEWS = "https://news.google.com/rss/search?"

BOILER = re.compile(
    r"향후\s*전망[^.]{0,80}(달라질|변경|차이|유의)|예측\s*정보|미래\s*사업\s*환경|forward[- ]looking|"
    r"실제\s*(결과|실적)(와|과)?\s*(는\s*)?(차이|다를)|유의하시기\s*바랍니다|투자자들?의\s*편의를\s*위해|"
    r"외부감사인의\s*(회계\s*)?(검토|감사)가\s*완료되지|잠정치로서|확정치와는\s*다를|"
    r"actual results may differ|subject to change without notice", re.I)
QUESTION = re.compile(r"\?|질문|여쭤|궁금|부탁\s*드|말씀해\s*주|주시면|주실\s*수|해\s*주시|싶습니다")
ANALYST_SENT = re.compile(r"연구원|증권가|애널리스트|증권은|투자증권|증권사|목표\s*주가|컨센서스|추정|추산|관측|업계에서는|시장에서는|전문가")
ATTRIB = re.compile(r"밝혔|설명했|말했|강조했|언급했|전망했|예상했|답했|덧붙였|내다봤|기대했|자신했|“|\"|LG에너지솔루션은|LG엔솔은|회사는|회사\s*측|CFO|이창실|담당은")
CALL_TITLE = re.compile(r"컨콜|컨퍼런스\s*콜|실적\s*발표|실적\s*설명|실적설명회|어닝\s*콜")
ANALYST_LINE = re.compile(r"(증권|투자증권|자산운용|리서치|Securities|Investment)\S*\s*(의\s*)?\S{2,4}\s*(입니다|이라고\s*합니다)")
COMPANY_LINE = re.compile(r"^\s*(CFO|CEO|IR|사회자|진행자)\s*[:：]|(CFO|담당|부사장|전무|상무)\s*\S{2,4}\s*입니다")

Q4 = re.compile(r"4\s*분기|4Q|fourth quarter|다음\s*분기|연말", re.I)
EXC = re.compile(
    r"(보조금|IRA|AMPC|세액\s*공제|tax credit|production (tax )?credit|incentive|45X)\s*(효과|금액)?\s*(를|을|은|는|도)?\s*(제외|빼|뺀|없이)|"
    r"(excluding|ex-|without)\s*(the\s*)?(IRA|AMPC|tax credit|production)|진짜\s*흑자|실질\s*흑자|본업\s*(흑자|이익|수익성)", re.I)
PROFIT = re.compile(r"흑자|적자|손실|이익|손익|수익성|profit|loss|margin|마진", re.I)
NEG = re.compile(
    r"적자|손실|\bloss|unprofitable|흑자\s*(를\s*)?(유지|지속)\s*(는|가|이|하기)?\s*(가\s*)?(어렵|쉽지\s*않|불투명|힘들)|"
    r"이익\s*(감소|축소|둔화)\s*(는|가|이)?\s*불가피", re.I)
NEG_NEGATED = re.compile(r"(적자|손실)\s*(로\s*)?(전환|돌아설|를\s*낼|날)\s*(가능성|우려|일)\s*(은|는|이)?\s*(낮|크지\s*않|없)", re.I)
POS = re.compile(
    r"흑자|수익성\s*(을|도|이)?\s*(확보|개선|높|유지|강화)|이익\s*(이|은|도)?\s*(증가|개선|확대|늘)|"
    r"profit\w*\s*(to\s*)?(continue|remain|sustain|improve|expand|grow)|remain\s*profitable|stay\s*profitable", re.I)
ASPIRE = re.compile(r"최선|노력하|목표로|하도록\s*하겠|할\s*수\s*있도록|aim|strive", re.I)

ONEOFF = re.compile(r"보상금|일회성|일시적\s*(이익|요인|수익)|one[- ]?off|one[- ]?time|compensation|위약금|최소\s*구매|MOQ|물량\s*보상", re.I)
Q3 = re.compile(r"3\s*분기|이번\s*분기|당\s*분기|3Q|this quarter|third quarter|분기\s*실적", re.I)
FUTURE = re.compile(r"4\s*분기|내년|향후|2027|next quarter", re.I)
IMMATERIAL = re.compile(r"(크지\s*않|미미|제한적|소폭|비중\s*(은|이)?\s*(작|낮)|immaterial|not\s*(material|significant)|minor|small)", re.I)
UNDISCLOSED = re.compile(
    r"(공개|밝히|언급)\s*(하기|하지|되지|는|를)?\s*(는\s*)?(어렵|않|않았|불가)|말씀\s*드리기\s*(는\s*)?(어렵|곤란)|비공개|계약상|"
    r"not\s*disclose|undisclosed|did not (give|disclose)", re.I)
KNOWN_EOK = {4169, 3391, 7560, 96434, 92265, 1861, 2410, 1898, 1277, 3975}


def http_bytes(url: str, timeout: int = 25) -> bytes | None:
    req = urllib.request.Request(url, headers=UA_WEB)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise


def load_pypdf():
    try:
        import pypdf  # type: ignore
        return pypdf
    except ImportError:
        pass
    target = Path(os.environ.get("RUNNER_TEMP") or tempfile.gettempdir()) / "lges1103_pylib"
    try:
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "--target", str(target), "pypdf"], check=False, timeout=180)
    except Exception as exc:  # noqa: BLE001
        print(f"pypdf install failed: {exc}", file=sys.stderr)
    sys.path.insert(0, str(target))
    try:
        import pypdf  # type: ignore
        return pypdf
    except ImportError:
        return None


def pdf_text(raw: bytes) -> str:
    if shutil.which("pdftotext"):
        with tempfile.NamedTemporaryFile(suffix=".pdf") as f:
            f.write(raw)
            f.flush()
            out = subprocess.run(["pdftotext", "-enc", "UTF-8", f.name, "-"], capture_output=True, timeout=120)
            if out.returncode == 0 and out.stdout:
                return out.stdout.decode("utf-8", "replace")
    pypdf = load_pypdf()
    if pypdf is None:
        return ""
    import io
    reader = pypdf.PdfReader(io.BytesIO(raw))
    return "\n".join((pg.extract_text() or "") for pg in reader.pages)


def drop_analyst_blocks(text: str) -> str:
    """Conference-call script: keep company speakers, drop analyst question blocks."""
    keep, analyst = [], False
    for line in text.splitlines():
        if COMPANY_LINE.search(line):
            analyst = False
        elif ANALYST_LINE.search(line):
            analyst = True
        if not analyst:
            keep.append(line)
    return "\n".join(keep)


def sentences(text: str) -> list[str]:
    flat = re.sub(r"\s+", " ", text or "").strip()
    parts = re.split(r"(?<=[\.!?。])\s+|(?<=다)\s+(?=[가-힣A-Z\[\"“])|\s*[■□▲•]\s*", flat)
    parts = [re.sub(r"^(CFO|CEO|IR|사회자|진행자)\s*[:：]\s*", "", p.strip()) for p in parts]
    return [p for p in parts if len(p) >= 6]


def clean_sentences(text: str, kind: str) -> list[str]:
    out = []
    for snt in sentences(text):
        if BOILER.search(snt) or QUESTION.search(snt):
            continue
        if kind == "news" and (ANALYST_SENT.search(snt) or not ATTRIB.search(snt)):
            continue
        out.append(snt)
    return out


def parse_amounts(snt: str) -> list[tuple[int, int]]:
    """[(position, 억원)] for 1,500억 / 1천500억 / 0.12조 / 1조 2,000억 / KRW 150 billion."""
    out = []
    for m in re.finditer(r"(\d+(?:\.\d+)?)\s*조\s*(?:원)?\s*(?:(\d[\d,]*)\s*억)?", snt):
        out.append((m.start(), int(round(float(m.group(1)) * 10000)) + (int(m.group(2).replace(",", "")) if m.group(2) else 0)))
    for m in re.finditer(r"(?<![\d.,조])(\d+)\s*천\s*(\d{1,3})?\s*억", snt):
        out.append((m.start(), int(m.group(1)) * 1000 + (int(m.group(2)) if m.group(2) else 0)))
    for m in re.finditer(r"(?<![\d.,천조])(\d[\d,]*)\s*억", snt):
        if not re.search(r"조\s*(원)?\s*$", snt[max(0, m.start() - 4):m.start()]):
            out.append((m.start(), int(m.group(1).replace(",", ""))))
    for m in re.finditer(r"(?:KRW|won|₩)\s*(\d[\d,.]*)\s*(billion|bn|trillion)|(\d[\d,.]*)\s*(billion|trillion)\s*won", snt, re.I):
        num = float((m.group(1) or m.group(3)).replace(",", ""))
        unit = (m.group(2) or m.group(4)).lower()
        out.append((m.start(), int(round(num * (10000 if unit.startswith("tr") else 10)))))
    return sorted((pos, v) for pos, v in out if 0 < v < 20000 and v not in KNOWN_EOK)


def classify_guidance(sents: list[str]) -> tuple[str, str | None]:
    pos = neg = asp = None
    for i, snt in enumerate(sents):
        prev = sents[i - 1] if i else ""
        if not (EXC.search(snt) and PROFIT.search(snt) and (Q4.search(snt) or Q4.search(prev))):
            continue
        if NEG.search(snt) and not NEG_NEGATED.search(snt):
            neg = neg or snt
        elif POS.search(snt) or NEG_NEGATED.search(snt):
            if ASPIRE.search(snt):
                asp = asp or snt
            else:
                pos = pos or snt
    if neg and pos:
        return "mixed", neg
    if neg:
        return "negative", neg
    if pos:
        return "positive", pos
    if asp:
        return "aspirational", asp
    return "not_stated", None


def classify_oneoff(sents: list[str], kind: str, title: str = "") -> tuple[str, int | None, str | None]:
    best: tuple[str, int | None, str | None] = ("not_found", None, None)
    rank = {"not_found": 0, "undisclosed": 1, "immaterial": 2, "amount": 3}
    for snt in sents:
        m = ONEOFF.search(snt)
        if not m:
            continue
        if FUTURE.search(snt) and not Q3.search(snt):
            continue
        if kind == "news" and not (Q3.search(snt) or Q3.search(title)):
            continue
        amts = parse_amounts(snt)
        after = [v for pos, v in amts if 0 <= pos - m.start() <= 45]
        before = [v for pos, v in amts if 0 < m.start() - pos <= 25]
        if after or before:
            got = ("amount", (after or before)[0], snt)
            if best[0] != "amount" or got[1] > best[1]:  # conservative: the largest stated one-off
                best = got
            continue
        if IMMATERIAL.search(snt[m.start():]) and rank[best[0]] < rank["immaterial"]:
            best = ("immaterial", None, snt)
        elif rank[best[0]] < rank["undisclosed"]:
            # stated as not disclosed (UNDISCLOSED) or mentioned without any amount: both mean no number to judge
            best = ("undisclosed", None, snt)
    return best


def evaluate(sources: list[dict]) -> dict:
    """sources: [{label, kind: company|news, text, title}] -> guidance + one-off (company first, news fallback)."""
    out = {"guidance": ("not_stated", None, None), "oneoff": ("not_found", None, None, None)}
    for tier in ("company", "news"):
        pool = [s for s in sources if s["kind"] == tier]
        if out["guidance"][0] in ("not_stated", "aspirational"):
            for s in pool:
                g, ev = classify_guidance(clean_sentences(s["text"], tier))
                if g != "not_stated" and (out["guidance"][0] == "not_stated" or g != "aspirational"):
                    out["guidance"] = (g, ev, s["label"])
                    if g != "aspirational":
                        break
        if out["oneoff"][0] in ("not_found", "undisclosed"):
            for s in pool:
                st, amt, ev = classify_oneoff(clean_sentences(s["text"], tier), tier, s.get("title", ""))
                if st == "not_found":
                    continue
                if out["oneoff"][0] == "not_found" or st in ("amount", "immaterial"):
                    out["oneoff"] = (st, amt, ev, s["label"])
                if st in ("amount", "immaterial"):
                    break
    return out


def call_verdict(ev: dict, base_ex: int, have_sources: bool) -> tuple[str, str]:
    g = ev["guidance"][0]
    o, amt = ev["oneoff"][0], ev["oneoff"][1]
    if not have_sources:
        return "확인 필요", "실적콜 자료(IR 스크립트, 발표자료, 보도자료, 공시, 콜 기사)를 찾지 못했습니다"
    if g == "negative":
        return "보류", "회사가 4분기 보조금 제외 적자나 손실을 시사했습니다"
    if o == "amount" and amt is not None and amt >= base_ex:
        return "보류", f"일회성 {amt:,}억원이 보조금 제외 영업이익 {base_ex:,}억원 이상이라 일회성까지 빼면 적자입니다"
    if g == "positive" and ((o == "amount" and amt is not None and amt < base_ex * 0.5) or o == "immaterial"):
        why = "일회성이 크지 않다고 밝혔습니다" if o == "immaterial" else f"일회성 비중 {amt / base_ex * 100:.0f}%로 50% 미만입니다"
        return "매수 검토", f"4분기 보조금 제외 흑자 지속을 시사했고 {why}"
    miss = []
    if g != "positive":
        miss.append({"not_stated": "4분기 보조금 제외 가이던스 언급 없음", "mixed": "4분기 가이던스가 엇갈림",
                     "aspirational": "4분기 보조금 제외 흑자는 목표/노력 표현뿐"}.get(g, "4분기 가이던스 불명"))
    if o == "amount" and amt is not None:
        miss.append(f"일회성 비중 {amt / base_ex * 100:.0f}%로 50% 이상")
    elif o != "immaterial":
        miss.append({"undisclosed": "일회성(보상금) 금액 비공개", "not_found": "일회성(보상금) 금액 언급 못 찾음"}[o])
    return "관망", ", ".join(miss)


def gather_sources(cfg: dict, now: datetime) -> tuple[list[dict], dict]:
    c = cfg["call"]
    srcs: list[dict] = []
    meta = {"errors": [], "script": False, "deck": False}
    for key, label, flag in (("script_pdf", "IR 스크립트", "script"), ("deck_pdf", "IR 발표자료", "deck")):
        try:
            raw = http_bytes(c[key], timeout=40)
            if raw and raw[:4] == b"%PDF":
                txt = pdf_text(raw)
                if key == "script_pdf":
                    txt = drop_analyst_blocks(txt)
                if len(txt) > 1500:
                    srcs.append({"label": label, "kind": "company", "text": txt, "title": "", "url": c[key]})
                    meta[flag] = True
                else:
                    meta["errors"].append(f"{label}: 텍스트 추출 실패")
        except Exception as exc:  # noqa: BLE001
            meta["errors"].append(f"{label}: {type(exc).__name__}")
    try:
        page = (http_bytes(c["lg_release_list"]) or b"").decode("utf-8", "replace")
        for m in re.finditer(r'"headline"\s*:\s*"([^"]+)".{0,400}?"datePublished"\s*:\s*"([^"]+)".{0,300}?"name"\s*:\s*"([^"]+)".{0,300}?"url"\s*:\s*"(https://www\.lg\.co\.kr/media/release/\d+)"', page, re.S):
            head, pub, author, url = m.groups()
            if "에너지솔루션" in author and pub[:10] >= c["call_day"] and re.search(r"실적|경영", head):
                body = html_text((http_bytes(url) or b"").decode("utf-8", "replace"))
                srcs.append({"label": "LG 보도자료", "kind": "company", "text": head + ". " + body, "title": head, "url": url})
                break
    except Exception as exc:  # noqa: BLE001
        meta["errors"].append(f"LG 보도자료: {type(exc).__name__}")
    try:
        for r in notices(cfg["ticker"], 30):
            if r.get("datetime", "") >= cfg.get("results_min_stamp", "2026-11-02T15:00") and is_final(r):
                srcs.append({"label": "공시", "kind": "company", "text": r["title"] + ". " + r["text"], "title": r["title"], "url": r["url"]})
    except Exception as exc:  # noqa: BLE001
        meta["errors"].append(f"공시: {type(exc).__name__}")
    since = c["news_since"]  # "202611031000"
    fetched = 0
    try:
        for page_no in (1, 2, 3):
            raw = json.loads((http_bytes(NEWS_API.format(code=cfg["ticker"], page=page_no)) or b"[]").decode("utf-8", "replace"))
            items = [it for b in (raw if isinstance(raw, list) else []) for it in (b or {}).get("items") or []]
            if not items:
                break
            for it in items:
                if str(it.get("datetime", "")) < since:
                    continue
                title = html_text(str(it.get("titleFull") or it.get("title") or ""))
                if not (CALL_TITLE.search(title) or re.search(r"보조금|보상금|일회성|4분기|가이던스", title)):
                    continue
                url = f"https://n.news.naver.com/mnews/article/{it.get('officeId')}/{it.get('articleId')}"
                body = html_text(str(it.get("body") or ""))
                if fetched < int(c.get("max_articles", 12)):
                    try:
                        h = (http_bytes(url) or b"").decode("utf-8", "replace")
                        mm = re.search(r'id="dic_area"[^>]*>(.*?)</article>', h, re.S)
                        if mm:
                            body = html_text(mm.group(1))
                            fetched += 1
                    except Exception:  # noqa: BLE001
                        pass
                tq = " ".join(re.findall(r"[“\"']([^”\"']{6,})[”\"']", title)) if CALL_TITLE.search(title) else ""
                srcs.append({"label": f"뉴스 {it.get('officeName', '')}".strip(), "kind": "news",
                             "text": (f"회사는 “{tq}”라고 밝혔다. " if tq else "") + body, "title": title, "url": url})
            if str(items[-1].get("datetime", "")) < since:
                break
    except Exception as exc:  # noqa: BLE001
        meta["errors"].append(f"네이버 뉴스: {type(exc).__name__}")
    try:
        cutoff = datetime.strptime(since, "%Y%m%d%H%M").replace(tzinfo=KST)
        for q in c.get("gnews_queries", []):
            raw = (http_bytes(GNEWS + urllib.parse.urlencode({"q": q, "hl": "ko", "gl": "KR", "ceid": "KR:ko"})) or b"").decode("utf-8", "replace")
            for it in re.findall(r"<item>(.*?)</item>", raw, re.S):
                t = htmllib.unescape(re.sub(r"<[^>]+>", "", (re.search(r"<title>(.*?)</title>", it, re.S) or [None, ""])[1]))
                d = re.search(r"<pubDate>(.*?)</pubDate>", it)
                try:
                    when = datetime.strptime(d.group(1).strip(), "%a, %d %b %Y %H:%M:%S %Z").replace(tzinfo=timezone.utc) if d else None
                except ValueError:
                    when = None
                if when is None or when < cutoff or not CALL_TITLE.search(t):
                    continue
                tq = " ".join(re.findall(r"[“\"']([^”\"']{6,})[”\"']", t))
                if tq:
                    srcs.append({"label": "뉴스 제목", "kind": "news", "text": f"회사는 “{tq}”라고 밝혔다.", "title": t, "url": ""})
    except Exception as exc:  # noqa: BLE001
        meta["errors"].append(f"구글 뉴스: {type(exc).__name__}")
    return srcs, meta


GUIDE_TXT = {"positive": "흑자 지속/확대 시사", "negative": "적자/손실 시사", "mixed": "엇갈림",
             "aspirational": "목표/노력 표현만", "not_stated": "언급 없음"}


def call_message(cfg: dict, label: str, why: str, ev: dict, base_ex: int, base_op: int, srcs: list[dict], now: datetime) -> tuple[str, list[str], int]:
    g, gev, gsrc = ev["guidance"]
    o, amt, oev, osrc = ev["oneoff"]
    gtxt = GUIDE_TXT[g] + (f" ({gsrc}: \"{gev[:110]}\")" if gev else "")
    if o == "amount":
        otxt = f"{amt:,}억원, 보조금 제외 영업이익 {base_ex:,}억원의 {amt / base_ex * 100:.0f}%, 발표 영업이익 {base_op:,}억원의 {amt / base_op * 100:.0f}%"
    else:
        otxt = {"immaterial": "크지 않다고 언급", "undisclosed": "언급은 있으나 금액 비공개", "not_found": "금액 언급 못 찾음"}[o]
    if oev:
        otxt += f" ({osrc}: \"{oev[:110]}\")"
    used = []
    for s in srcs:
        if s["label"] not in used:
            used.append(s["label"])
    todo = {
        "매수 검토": "신규 매수 검토 가능. 한 번에 사지 말고 계획 금액의 일부만 나눠서, 4분기 잠정실적 전까지 추가는 보류.",
        "관망": "아직 새로 사지 말고 관망. 빠진 항목이 확인되거나 4분기 잠정실적이 나올 때까지 기다리기.",
        "보류": "신규 매수 보류. 보조금과 일회성을 뺀 이익이 다시 흑자로 확인되기 전에는 사지 않기.",
        "확인 필요": "판정 못 함. IR 자료와 공시 원문을 직접 확인하기 전에는 매수 결정 미루기.",
    }[label]
    title = f"[LGESF] 373220 실적콜 판정: {label}"
    lines = [
        "이게 뭐 알림: LG에너지솔루션(373220) 11/3 3분기 실적콜에서 4분기 보조금 제외 손익 가이던스와 3분기 일회성(보상금) 비중을 보고 신규 매수 여부를 판정합니다. 미보유, 참고용, 투자 권유 아님.",
        f"지금 결과/상태: 판정 {label}. 이유: {why}. 4분기 보조금 제외 가이던스: {gtxt}. 3분기 일회성: {otxt}. "
        + (f"3분기 보조금 제외 영업이익 확정 {base_ex:,}억원(잠정 3,391억원). " if base_ex != 3391 else "3분기 보조금 제외 영업이익 잠정 3,391억원(영업이익률 3.7%). ")
        + "1~3분기 누적 보조금 제외 -1,861억원(잠정 기준). "
        + f"자료: {', '.join(used) if used else '없음'}. 조회 {now:%Y-%m-%d %H:%M} KST.",
        "지금 할 일: " + todo,
        "나중에: 4분기 잠정실적(보통 1월 초)에서 보조금 제외 영업이익이 실제 흑자인지 확인. 가격 구간은 [LGES] 알림이 따로 봄.",
        "익절/줄일 때: 미보유라 해당 없음. 사게 되면 보조금 제외 적자 전환이 확인될 때 비중 재검토.",
        "하지 말 것: 기사 제목만 보고 추격 매수, 보조금이나 보상금이 낀 이익을 본업 이익으로 보기, 신용/미수. 투자 권유 아님.",
    ]
    return title, lines, (4 if label in ("매수 검토", "보류") else 3)


def send_call(cfg: dict, kind: str, title: str, lines: list[str], prio: int) -> None:
    publish(resolve_topic(kind, cfg.get("ntfy_topic") if kind == "port" else None), title, "\n".join(lines), prio,
            ["warning"] if kind == "ops" else ["telephone_receiver"], resolve_server(cfg.get("ntfy_server")))


def call_part(cfg: dict, state: dict, now: datetime, mode: str, results: dict | None = None,
              gather=gather_sources, sender=send_call) -> str:
    """Returns what happened: before_call | already | sent:<label> | ops | wait."""
    c = cfg["call"]
    dry = mode == "dry"
    start = datetime.strptime(c["judge_from_kst"], "%Y-%m-%d %H:%M").replace(tzinfo=KST)
    evening = datetime.strptime(c["evening_kst"], "%Y-%m-%d %H:%M").replace(tzinfo=KST)
    final = datetime.strptime(c["final_kst"], "%Y-%m-%d %H:%M").replace(tzinfo=KST)
    if now < start:
        print(f"call part: before {c['judge_from_kst']} KST; silent")
        return "before_call"
    if state.get("call_notified") and mode != "force":
        print("call part: verdict already sent; silent")
        return "already"
    srcs, meta = gather(cfg, now)
    ev = evaluate(srcs)
    base_ex = (results or {}).get("ex") or state.get("confirmed_ex_eok") or int(cfg["prelim"]["ex_ampc_eok"])
    base_op = (results or {}).get("op") or state.get("confirmed_op_eok") or int(cfg["prelim"]["op_eok"])
    label, why = call_verdict(ev, int(base_ex), bool(srcs))
    g, o = ev["guidance"][0], ev["oneoff"][0]
    print(f"call part: sources={[s['label'] for s in srcs]} errors={meta['errors']} script={meta['script']} "
          f"guidance={g} oneoff={o}:{ev['oneoff'][1]} -> {label} ({why})")
    found_any = g != "not_stated" or o != "not_found"
    complete = g in ("positive", "negative") and o != "not_found"
    if label == "보류" or complete:
        go = True
    elif now >= final:
        go = found_any or meta["script"] or meta["deck"]
    elif now >= evening:
        go = meta["script"]
    else:
        go = False
    if go and label != "확인 필요":
        title, lines, prio = call_message(cfg, label, why, ev, int(base_ex), int(base_op), srcs, now)
        if dry:
            print(f"DRY RUN would send (p{prio}): {title}", *lines, sep="\n")
            return f"sent:{label}"
        sender(cfg, "port", title, lines, prio)
        state.update({"call_notified": True, "call_verdict": label, "call_sent_at": now.isoformat(timespec="seconds"),
                      "call_guidance": g, "call_oneoff": o, "call_oneoff_eok": ev["oneoff"][1]})
        save_state(state, now)
        return f"sent:{label}"
    if now >= final:
        if state.get("call_problem_notified") and mode != "force":
            print("call part: ops notice already sent; silent")
            return "already"
        kind = "자료 조회 실패" if meta["errors"] and not srcs else "자료 미게시"
        title = f"[LGESF] 373220 실적콜 판정: 확인 필요 | {kind}"
        lines = [
            "이게 뭐 알림: LG에너지솔루션 11/3 3분기 실적콜 판정(신규 매수 기준). 미보유, 매매 신호 아님.",
            f"지금 결과/상태: 마지막 실행({now:%Y-%m-%d %H:%M} KST)까지 판정할 자료를 찾지 못했습니다. "
            f"찾은 자료: {', '.join(s['label'] for s in srcs) or '없음'}. 오류: {', '.join(meta['errors']) or '없음'}. "
            f"IR: {c['ir_page']}",
            "지금 할 일: IR 스크립트와 발표자료를 직접 열어 4분기 보조금 제외 가이던스와 3분기 보상금 금액을 확인하기.",
            "나중에: 이 알림은 더 돌지 않습니다.",
            "익절/줄일 때: 해당 없음.",
            "하지 말 것: 자료 없이 매수 판단하기.",
        ]
        if dry:
            print(f"DRY RUN would send (ops): {title}", *lines, sep="\n")
            return "ops"
        sender(cfg, "ops", title, lines, 3)
        state["call_problem_notified"] = True
        save_state(state, now)
        return "ops"
    print("call part: not enough yet; wait for the next run")
    return "wait"


# ---------- selftest ----------

def selftest() -> int:
    pre = {"op_eok": 7560}
    def rp(op, ampc, comp, ev=1, ess=1, net=1, ocf=1):
        return {"op": op, "ampc": ampc, "comp": comp, "ex": None if op is None or ampc is None else op - ampc,
                "ev": ev, "ess": ess, "net": net, "ocf": ocf, "sales": 1}
    results_cases = [
        ("results 상향 -> 매수 검토", rp(7560, 4169, 800), "매수 검토"),
        ("results 상향 edge op 90%", rp(6804, 4000, 500), "매수 검토"),
        ("results 유지 (보상금 미공개) -> 관망", rp(7560, 4169, None), "관망"),
        ("results 유지 (보상금 차감 2,000억 미만) -> 관망", rp(7560, 4169, 1500), "관망"),
        ("results 유지 (op 89%) -> 관망", rp(6700, 4000, 100), "관망"),
        ("results 철회 (AMPC 제외 적자) -> 보류", rp(4000, 4169, 100), "보류"),
        ("results 철회 (op 70% 미만) -> 보류", rp(5200, 4000, 100), "보류"),
        ("results 판정 유보 -> 관망", rp(None, 4169, None), "관망"),
    ]
    results_fail = 0
    for name, p_, want in results_cases:
        got = verdict(p_, pre)[0]
        print(("ok  " if got == want else "FAIL"), name, got)
        results_fail += got != want
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    fails = 0

    def check(label, got, want):
        nonlocal fails
        ok = got == want
        fails += not ok
        print(("ok  " if ok else "FAIL"), label, got, "" if ok else f"want {want}")

    fails += results_fail
    # results part renders with new-buy labels only (stub notices/parse, dry = print only)
    g = globals()
    keep = (g["notices"], g["parse"], g["is_final"])
    try:
        g["notices"] = lambda *_a, **_k: [{"id": "T1", "title": "연결재무제표기준영업실적", "datetime": "2026-11-03T15:40:00"}]
        g["is_final"] = lambda r: True
        for want, fx in [("매수 검토", rp(7560, 4169, 800)), ("관망", rp(7560, 4169, None)), ("보류", rp(4000, 4169, 100))]:
            g["parse"] = lambda _item, fx=fx: fx
            import io, contextlib
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                results_part(cfg, {"result_notified": False}, datetime(2026, 11, 3, 16, 32, tzinfo=KST), "dry")
            out = buf.getvalue()
            check(f"results dry title {want}", out.splitlines()[0], f"[LGESF] 373220 LG엔솔 3Q 확정 {want}")
            check(f"results {want} has no holder wording", any(w in out for w in ("상향하세요", "유지하세요", "철회하세요", "보유 비중을 재평가")), False)
            check(f"results {want} no middle dot/em-dash", any(ch in out for ch in "\u00b7\u2014"), False)
            print(out)
    finally:
        g["notices"], g["parse"], g["is_final"] = keep

    disclaimer = "본 실적 발표에 포함된 향후 전망은 미래 사업 환경의 변화 및 전략 수정에 따라 달라질 수 있으므로 유의하시기 바랍니다. 4분기 보조금 제외 적자 가능성도 실제 결과와 다를 수 있습니다."
    q_block = "\n질문 받겠습니다.\n하나증권의 김현수입니다.\n4분기에도 보조금 제외 적자가 우려되는데 어떻게 보시는지 여쭤보고 싶습니다.\n3분기 보상금이 5,000억원이라는 얘기도 있습니다.\nCFO:\n"

    def script(body: str) -> dict:
        return {"label": "IR 스크립트", "kind": "company", "text": drop_analyst_blocks(disclaimer + q_block + body), "title": ""}

    POS_G = "4분기에도 IRA 효과를 제외한 흑자 기조를 이어갈 것으로 예상합니다."
    cases = [
        ("매수 검토 (흑자 + 보상금 800억 24%)", [script(POS_G + " 3분기 실적에는 고객사 물량 보상금 약 800억원이 일회성으로 반영되었습니다.")], "매수 검토"),
        ("매수 검토 (흑자 + 일회성 미미)", [script(POS_G + " 이번 분기 일회성 보상금 규모는 크지 않습니다.")], "매수 검토"),
        ("관망 (흑자 + 일회성 2,000억 59%)", [script(POS_G + " 3분기에는 최소 구매 물량 보상금 2,000억원이 포함됐습니다.")], "관망"),
        ("관망 (흑자 + 보상금 비공개)", [script(POS_G + " 3분기 보상금 규모는 고객사와의 계약상 말씀드리기 어렵습니다.")], "관망"),
        ("관망 (가이던스 없음 + 보상금 500억)", [script("ESS 출하는 늘었습니다. 3분기 보상금은 500억원 수준입니다.")], "관망"),
        ("관망 (목표 표현만)", [script("4분기를 기점으로 IRA 효과를 제외하고도 수익성을 확보할 수 있도록 최선을 다하겠습니다. 3분기 보상금은 300억원입니다.")], "관망"),
        ("보류 (4분기 보조금 제외 적자)", [script("4분기는 EV 출하 감소로 보조금 제외 시 적자가 불가피할 것으로 보고 있습니다.")], "보류"),
        ("보류 (일회성 3,500억 >= 3,391억)", [script(POS_G + " 3분기 보상금 등 일회성 이익은 약 3,500억원입니다.")], "보류"),
        ("확인 필요 (자료 없음)", [], "확인 필요"),
    ]
    for name, srcs, want in cases:
        ev = evaluate(srcs)
        check(name, call_verdict(ev, 3391, bool(srcs))[0], want)
    ev = evaluate([script("ESS 출하는 늘었습니다.")])
    check("boilerplate + analyst question stripped -> not_stated / not_found", (ev["guidance"][0], ev["oneoff"][0]), ("not_stated", "not_found"))
    check("question block dropped", "김현수" in drop_analyst_blocks(q_block), False)
    check("disclaimer sentence dropped", any("유의" in x for x in clean_sentences(disclaimer, "company")), False)
    news = [{"label": "뉴스 A", "kind": "news", "title": "[컨콜] LG엔솔 3분기 실적",
             "text": "김철수 연구원은 4분기 보조금 제외 적자 전환을 예상했다. 회사는 4분기에도 보조금을 제외하고 흑자를 유지할 것이라고 밝혔다. "
                     "3분기 보상금 규모는 공개하지 않았다고 회사 측은 설명했다."}]
    ev = evaluate(news)
    check("news: analyst view ignored, company view used", ev["guidance"][0], "positive")
    check("news: undisclosed one-off", ev["oneoff"][0], "undisclosed")
    check("news -> 관망", call_verdict(ev, 3391, True)[0], "관망")
    title_src = [{"label": "뉴스 제목", "kind": "news", "title": "", "text": "회사는 “4분기도 보조금 빼고 흑자 지속”라고 밝혔다."}]
    check("quoted [컨콜] title positive", evaluate(title_src)["guidance"][0], "positive")
    check("company beats news", evaluate(news + [script("4분기 보조금 제외 손실이 예상됩니다.")])["guidance"][0], "negative")
    check("negated loss = positive", classify_guidance(["4분기 보조금 제외 적자 전환 가능성은 낮습니다."])[0], "positive")
    check("amounts 1천500억 / 0.12조 / KRW 150 billion", [v for _, v in parse_amounts("보상금 1천500억원, 0.12조원, KRW 150 billion")], [1500, 1200, 1500])
    check("AMPC 4,169억 not taken as one-off", classify_oneoff(["3분기 보조금 4,169억원과 보상금 600억원이 반영됐습니다."], "company")[:2], ("amount", 600))
    check("one-off mentioned without amount = undisclosed", classify_oneoff(["고객으로부터 일회성 물량 보상금도 일부 반영되며 이를 제외한 영업이익은 개선됐습니다."], "company")[0], "undisclosed")
    check("future one-off ignored", classify_oneoff(["4분기에는 보상금 1,000억원이 들어올 수 있습니다."], "company")[0], "not_found")

    # run logic with fake sources
    def fake(srcs, meta=None):
        return lambda _c, _n: (srcs, dict({"errors": [], "script": False, "deck": False}, **(meta or {})))

    sent = []

    def snd(_c, kind, title, lines, prio):
        sent.append((kind, title, prio, "\n".join(lines)))

    def at(s):
        return datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=KST)

    base = {"result_notified": False, "empty_notified": False, "last_problem_key": None}
    import copy
    global save_state
    real_save = save_state
    save_state = lambda st, n: None  # noqa: E731  (selftest never writes state/)
    try:
        st = copy.deepcopy(base)
        check("before 11:00 silent", call_part(cfg, st, at("2026-11-03 09:22"), "auto", gather=fake(cases[0][1]), sender=snd), "before_call")
        partial = [{"label": "뉴스 B", "kind": "news", "title": "[컨콜] LG엔솔", "text": "회사는 4분기에도 보조금을 제외하고 흑자를 유지할 것이라고 밝혔다."}]
        check("12:30 partial -> wait", call_part(cfg, st, at("2026-11-03 12:30"), "auto", gather=fake(partial), sender=snd), "wait")
        check("16:32 partial, no script -> wait", call_part(cfg, st, at("2026-11-03 16:32"), "auto", gather=fake(partial), sender=snd), "wait")
        check("17:10 script posted -> send", call_part(cfg, st, at("2026-11-03 17:10"), "auto", gather=fake(partial, {"script": True}), sender=snd), "sent:관망")
        check("verdict sent once", call_part(cfg, st, at("2026-11-04 09:22"), "auto", gather=fake(partial), sender=snd), "already")
        st = copy.deepcopy(base)
        check("12:30 complete -> send 매수 검토", call_part(cfg, st, at("2026-11-03 12:30"), "auto", gather=fake(cases[0][1]), sender=snd), "sent:매수 검토")
        st = copy.deepcopy(base)
        check("12:30 decisive 보류 -> send", call_part(cfg, st, at("2026-11-03 12:30"), "auto", gather=fake(cases[6][1]), sender=snd), "sent:보류")
        st = copy.deepcopy(base)
        check("final partial -> send 관망", call_part(cfg, st, at("2026-11-04 09:22"), "auto", gather=fake(partial), sender=snd), "sent:관망")
        st = copy.deepcopy(base)
        sent.clear()
        check("final nothing -> ops", call_part(cfg, st, at("2026-11-04 09:22"), "auto", gather=fake([]), sender=snd), "ops")
        check("ops title routes to cwally-ops", (sent[0][0], sent[0][1]), ("ops", "[LGESF] 373220 실적콜 판정: 확인 필요 | 자료 미게시"))
        check("ops once", call_part(cfg, st, at("2026-11-04 10:00"), "auto", gather=fake([]), sender=snd), "already")
        st = copy.deepcopy(base)
        sent.clear()
        check("dry prints only", (call_part(cfg, st, at("2026-11-03 12:30"), "dry", gather=fake(cases[0][1]), sender=snd), sent, st.get("call_notified")), ("sent:매수 검토", [], None))
        check("confirmed ex-credit OP used as base (2,000억: 3,391 -> 관망, 4,500 -> 매수 검토)", (call_verdict(evaluate(cases[2][1]), 3391, True)[0], call_verdict(evaluate(cases[2][1]), 4500, True)[0]), ("관망", "매수 검토"))
    finally:
        save_state = real_save
    # results-part fallback no longer matches the 10/8 IR notice
    rows = [{"title": "(주)엘지에너지솔루션 기업설명회(IR) 개최(안내공시)", "datetime": "2026-10-08T08:38:12", "text": "2026년 3분기 경영실적 발표 LG에너지솔루션"}]
    check("old IR notice not a result", [r for r in rows if is_final(r) and r["datetime"] >= cfg["results_min_stamp"]], [])
    samples = []
    for name, srcs, want in cases[:1] + cases[2:3] + cases[6:7]:
        ev = evaluate(srcs)
        lab, why = call_verdict(ev, 3391, True)
        t, ls, _ = call_message(cfg, lab, why, ev, 3391, 7560, srcs, at("2026-11-03 12:30"))
        samples.append(t + "\n" + "\n".join(ls))
    allt = "\n".join(samples + [x[3] for x in sent])
    check("no middle dot / em-dash in call texts", any(ch in allt for ch in "\u00b7\u2014"), False)
    src_txt = Path(__file__).read_text(encoding="utf-8")
    check("no middle dot in script push strings", "\u00b7" in src_txt.replace("횡령ㆍ배임", ""), False)
    print("\n--- sample ---\n" + "\n\n".join(samples))
    print("selftest", "PASS" if not fails else f"FAIL {fails}")
    return 1 if fails else 0


def main() -> int:
    args = sys.argv[1:]
    mode = (args[0] if args else "auto").lower()
    if mode == "selftest":
        return selftest()
    if mode not in {"auto", "dry", "force"}:
        raise SystemExit(f"unknown mode: {mode}")
    now = datetime.now(KST)
    if "--now" in args:
        if mode != "dry":
            raise SystemExit("--now is for dry only")
        now = datetime.strptime(args[args.index("--now") + 1], "%Y-%m-%d %H:%M").replace(tzinfo=KST)
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    state = read_state()
    day = now.strftime("%Y-%m-%d")
    if mode == "dry":
        print(f"DRY RUN (no ntfy, no state write) now={now:%Y-%m-%d %H:%M} KST")
    if now.year != int(cfg["year_guard"]) or not cfg["window_start"] <= day <= cfg["window_end"]:
        print("outside 2026 one-shot window; silent")
        return 0
    results = results_part(cfg, state, now, mode)
    call_part(cfg, state, now, mode, results)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
