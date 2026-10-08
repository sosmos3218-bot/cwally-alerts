#!/usr/bin/env python3
"""Samsung Electronics 005930 2026 Q3 confirmed results (2026-10-29) vs SK hynix 000660.

Extracts only: DS / MX(MX/NW) / Display(SDC) / Harman operating profit, HBM4 revenue share (only if the
official text states a percent), and the Q4 outlook. Compares margin, share and valuation with SK hynix
and scores five re-rating conditions (configs/secq3.json "rules"). Not a trading signal: price-band
trading stays with [SEC] (sec.json).

Sources (official first):
  - Samsung IR deck PDF (Appendix "Results by Business Segment"): segment sales/OP incl. MX/NW and Memory.
  - Samsung Newsroom Korea press release "삼성전자, 2026년 3분기 실적 발표": DS/DX/Harman/SDC lines,
    "[4분기 전망]" section, HBM4 sentences.
  - stock.naver.com notice API: KRX 연결재무제표기준영업(잠정)실적 for both companies (Q3 span).
  - m.stock.naver.com finance/integration/basic APIs: SK hynix Q3 consensus or actual, PER/PBR, prices.
Third-party market share (TrendForce / Counterpoint) is a dated reference from config, never inferred.
News headlines on HBM4 share are shown only as "보도(미검증)" and never count toward a condition.

Modes: auto (one-shot), dry (print only, no state/send; accepts --deck/--news/--qlabel/--outlook/--now
for offline tests), force (re-send the result), install (one install-confirmation send).
Lookup/data problems go to cwally-ops via title words (미게시 / 파싱 실패 / 조회 실패).
"""
from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from html import unescape
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from naver_api import get_json, notices  # noqa: E402
from ntfy_util import publish  # noqa: E402

KST = timezone(timedelta(hours=9))
CFG_PATH = ROOT / "configs" / "secq3.json"
STATE_PATH = ROOT / "state" / "secq3.json"
SEC_CFG = ROOT / "configs" / "sec.json"
# news.samsung.com times out for the custom cwally UA (checked 2026-10-08); a plain browser UA answers in 0.1s.
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"}
MAX_BYTES = 3700  # ntfy turns bodies over 4096 bytes into attachments; leave room for the end line
DEFAULT_STATE = {
    "result_sent": False,
    "result_at": None,
    "result": None,
    "sk_kind_in_result": None,
    "sk_update_sent": False,
    "sk_cons_live": None,
    "ops_sent": {},
    "updated_at": None,
}


# ---------- state ----------

def read_state() -> dict:
    state = dict(DEFAULT_STATE)
    if STATE_PATH.exists():
        try:
            data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                state.update({k: v for k, v in data.items() if k in DEFAULT_STATE})
        except json.JSONDecodeError:
            pass
    if not isinstance(state.get("ops_sent"), dict):
        state["ops_sent"] = {}
    return state


def save_state(state: dict, now: datetime) -> None:
    state["updated_at"] = now.isoformat(timespec="seconds")
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(STATE_PATH)


# ---------- fetch ----------

def fetch(url: str, timeout: int = 30) -> bytes | None:
    """Body bytes, or None on 404/410 (not published yet). Other errors raise."""
    parts = urllib.parse.urlsplit(url)
    url = urllib.parse.urlunsplit(parts._replace(path=urllib.parse.quote(parts.path)))
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
            return r.read()
    except urllib.error.HTTPError as exc:
        if exc.code in (404, 410):
            return None
        raise


def num(raw) -> float | None:
    if raw is None:
        return None
    s = str(raw).replace(",", "").replace("배", "").replace("원", "").replace("%", "").strip()
    neg = s.startswith("(") and s.endswith(")") or s.startswith("△")
    s = s.strip("()△").strip()
    if s in {"", "-", "N/A"}:
        return None
    try:
        v = float(s)
    except ValueError:
        return None
    return -v if neg else v


# ---------- Samsung IR deck ----------

def load_pypdf():
    try:
        import pypdf  # type: ignore
        return pypdf
    except ImportError:
        pass
    target = Path(os.environ.get("RUNNER_TEMP") or tempfile.gettempdir()) / "secq3_pylib"
    try:
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "--target", str(target), "pypdf"],
                       check=False, timeout=180)
    except Exception as exc:  # pip missing / timeout: deck is optional, newsroom still works
        print(f"pypdf install failed: {exc}", file=sys.stderr)
    sys.path.insert(0, str(target))
    try:
        import pypdf  # type: ignore
        return pypdf
    except ImportError:
        return None


SEG_LABELS = ["Total", "DX", "MX / NW", "MX", "VD / DA", "VD", "DS", "Memory", "SDC", "Harman"]
NUM_RE = r"(\(?-?\d+(?:\.\d+)?\)?)"
ROW_RE = re.compile(r"^(" + "|".join(re.escape(x) for x in SEG_LABELS) + r")\s+" + r"\s+".join([NUM_RE] * 3) + r"(?:\s|$)")


def parse_deck(raw: bytes, qlabel: str) -> dict:
    """{'status': ok|parse_fail|no_pdf, 'sales': {label: (prev, cur)}, 'op': {...}, 'text': str}"""
    if not raw or not raw.lstrip().startswith(b"%PDF"):
        return {"status": "no_pdf"}
    pypdf = load_pypdf()
    if pypdf is None:
        return {"status": "parse_fail", "why": "pypdf 없음"}
    try:
        reader = pypdf.PdfReader(io.BytesIO(raw))
        pages = [(p.extract_text() or "") for p in reader.pages]
    except Exception as exc:
        return {"status": "parse_fail", "why": f"PDF 읽기 {type(exc).__name__}"}
    full = "\n".join(pages)
    # The segment table page: two blocks (sales, OP) in either order, each starting with a Total row.
    # 2026 decks: "Appendix 2: Results by Business Segment", sales first, header "2Q 26".
    # 2025 Q3 deck: OP first, header "3Q25". The block holding Memory/MX rows is the sales block.
    page = None
    for t in pages:
        rows = [ROW_RE.match(x.strip()) for x in t.splitlines()]
        labels = [m.group(1) for m in rows if m]
        if labels.count("Total") >= 2 and "Harman" in labels and "MX / NW" in labels:
            page = t
            break
    if page is None:
        return {"status": "parse_fail", "why": "부문 표 페이지 없음", "text": full}
    want = qlabel.replace(" ", "")
    heads = re.findall(r"(\dQ\s?\d\d)\s+(\dQ\s?\d\d)\s+(\dQ\s?\d\d)", page)
    if not heads or any(h[2].replace(" ", "") != want for h in heads):
        return {"status": "parse_fail", "why": f"표 분기 불일치({heads[0] if heads else '헤더 없음'})", "text": full}
    blocks: list[dict] = []
    for line in page.splitlines():
        m = ROW_RE.match(line.strip())
        if not m:
            continue
        label = m.group(1)
        vals = [num(m.group(i)) for i in (2, 3, 4)]
        if label == "Total" or not blocks:
            blocks.append({})
        if label not in blocks[-1]:
            blocks[-1][label] = (vals[1], vals[2])
    if len(blocks) < 2:
        return {"status": "parse_fail", "why": "표 블록 부족", "text": full}
    a, b = blocks[0], blocks[1]
    sales, op = (a, b) if ("Memory" in a or "MX" in a) else (b, a)
    need = {"DS", "SDC", "Harman", "MX / NW"}
    if not need <= set(op) or "DS" not in sales:
        return {"status": "parse_fail", "why": f"부문 손익 누락({sorted(need - set(op))})", "text": full}
    return {"status": "ok", "sales": sales, "op": op, "text": full}


# ---------- Samsung newsroom ----------

def newsroom_text(raw: bytes) -> tuple[str | None, str]:
    s = raw.decode("utf-8", errors="ignore")
    t = re.search(r"<title>(.*?)</title>", s, re.S)
    title = unescape(t.group(1)).strip() if t else ""
    i = s.find('class="single_contents"')
    if i < 0:
        return None, title
    seg = s[i:]
    j = seg.find('class="article_more"')
    if j > 0:
        seg = seg[:j]
    seg = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", seg, flags=re.S | re.I)
    seg = re.sub(r"<br\s*/?>", "\n", seg)
    seg = re.sub(r"</(p|h\d|li|div|tr)>", "\n", seg)
    seg = re.sub(r"<[^>]+>", "", seg)
    seg = unescape(seg).replace("\xa0", " ")
    seg = re.sub(r"[ \t]+", " ", seg)
    lines = [x.strip() for x in seg.splitlines()]
    return "\n".join(x for x in lines if x and not x.startswith('class="')), title


def find_newsroom(cfg_s: dict) -> tuple[str | None, str | None]:
    """(text, url) of the confirmed-results release; never the 잠정 release."""
    urls = [cfg_s["newsroom_url"]]
    try:
        rss = (fetch(cfg_s["newsroom_rss"]) or b"").decode("utf-8", errors="ignore")
        for title, link in re.findall(r"<item>.*?<title>(.*?)</title>.*?<link>(.*?)</link>", rss, re.S):
            title = re.sub(r"<!\[CDATA\[|\]\]>", "", title)
            if cfg_s["newsroom_title"] in title and "잠정" not in title:
                urls.append(link.strip())
    except Exception as exc:
        print(f"newsroom rss skipped: {exc}", file=sys.stderr)
    for url in urls:
        raw = fetch(url)
        if not raw:
            continue
        text, title = newsroom_text(raw)
        if text and cfg_s["newsroom_title"] in title and "잠정" not in title:
            return text, url
    return None, None


def jo(v: str) -> float | None:
    return num(v)


def parse_newsroom(text: str, outlook_head: str) -> dict:
    out: dict = {"op": {}, "sales": {}}
    pats = {
        "DS": r"DS\s*\(Device Solutions\)\s*부문\s*매출\s*([\d.]+)\s*조원\s*,\s*영업이익\s*(△?-?[\d.]+)\s*조원",
        "DX": r"DX\s*\(Device eXperience\)\s*부문\s*매출\s*([\d.]+)\s*조원\s*,\s*영업이익\s*(△?-?[\d.]+)\s*조원",
        "Harman": r"하만\s*매출\s*([\d.]+)\s*조원\s*,\s*영업이익\s*(△?-?[\d.]+)\s*조원",
        "SDC": r"(?:디스플레이|SDC)\s*매출\s*([\d.]+)\s*조원\s*,\s*영업이익\s*(△?-?[\d.]+)\s*조원",
    }
    for k, p in pats.items():
        m = re.search(p, text)
        if m:
            out["sales"][k] = jo(m.group(1))
            out["op"][k] = jo(m.group(2))
    m = re.search(r"연결\s*기준\s*(?:으로\s*)?매출\s*([\d.]+)\s*조원\s*,\s*영업이익\s*([\d.]+)\s*조원", text)
    if m:
        out["sales"]["Total"], out["op"]["Total"] = jo(m.group(1)), jo(m.group(2))
    # outlook section: from the heading to the next bracketed heading
    lines = text.splitlines()
    start = next((i for i, x in enumerate(lines) if outlook_head in x and re.match(r"^[\[【]", x.strip())), None)
    sec: list[str] = []
    if start is not None:
        for x in lines[start + 1:]:
            if re.match(r"^[\[【]", x.strip()):
                break
            sec.append(x.strip())
    out["outlook"] = sec
    return out


def outlook_pick(sec: list[str]) -> dict:
    """overall / memory / mx / sdc / harman sentences from the outlook section."""
    pick = {"overall": sec[0] if sec else None}
    cur = None
    for x in sec[1:]:
        if x in {"DS부문", "DX부문", "하만", "SDC", "디스플레이"}:
            cur = x
            continue
        if x.startswith("메모리") and "memory" not in pick:
            pick["memory"] = x
        elif x.startswith("MX") and "mx" not in pick:
            pick["mx"] = x
        elif cur in {"SDC", "디스플레이"} and "sdc" not in pick:
            pick["sdc"] = x
        elif cur == "하만" and "harman" not in pick:
            pick["harman"] = x
        elif x.startswith("DS부문") and "ds" not in pick:
            pick["ds"] = x
    return pick


def tone(sentence: str | None, rules: dict) -> str | None:
    if not sentence:
        return None
    pos = any(w in sentence for w in rules["q4_pos_words"])
    neg = any(w in sentence for w in rules["q4_neg_words"])
    if pos and not neg:
        return "긍정"
    if neg and not pos:
        return "부정"
    return "혼재"


# ---------- HBM4 ----------

def hbm4_official(texts: list[str]) -> tuple[float | None, str | None]:
    """Percent only when an official sentence ties HBM4 to a share of sales."""
    for t in texts:
        for sent in re.split(r"(?<=[.。])\s+|\n", t or ""):
            if "HBM4" not in sent:
                continue
            if not re.search(r"비중|차지|상회|share|portion|mix|accounted|percent of", sent, re.I):
                continue
            m = re.search(r"(\d{1,3}(?:\.\d+)?)\s*%", sent)
            if m:
                return float(m.group(1)), sent.strip()[:110]
    return None, None


def hbm4_news(now: datetime) -> list[str]:
    q = urllib.parse.quote("삼성전자 HBM4 비중 when:1d")
    try:
        raw = fetch(f"https://news.google.com/rss/search?q={q}&hl=ko&gl=KR&ceid=KR:ko", timeout=20) or b""
    except Exception as exc:
        print(f"news skipped: {exc}", file=sys.stderr)
        return []
    out = []
    for title in re.findall(r"<item>.*?<title>(.*?)</title>", raw.decode("utf-8", errors="ignore"), re.S):
        title = unescape(re.sub(r"<!\[CDATA\[|\]\]>", "", title)).strip()
        if "HBM4" in title and re.search(r"\d\s*%", title):
            out.append(title[:80])
        if len(out) >= 2:
            break
    return out


# ---------- KRX notices ----------

def notice_result(code: str, span: str, since: str) -> dict | None:
    """Sales/OP (조원) from the KRX 영업(잠정)실적 notice for span, posted on/after since (YYYY-MM-DD)."""
    for r in notices(code, 40):
        title, body = r.get("title", ""), r.get("text", "")
        if not ("잠정" in title and "실적" in title and "영업" in title):
            continue
        if str(r.get("datetime", ""))[:10] < since or not re.search(r"당기실적\s*" + re.escape(span), body):
            continue
        um = re.search(r"단위\s*[:：]\s*(조원|억원|백만원|천원)", body)
        div = {"조원": 1, "억원": 1e4, "백만원": 1e6, "천원": 1e9}.get(um.group(1) if um else "백만원", 1e6)
        vals = {}
        for k, lab in (("sales", "매출액"), ("op", "영업이익")):
            m = re.search(lab + r"\s*당해실적\s*(-?[\d,]+(?:\.\d+)?)", body)
            vals[k] = round(num(m.group(1)) / div, 2) if m and num(m.group(1)) is not None else None
        if vals["sales"] and vals["op"] is not None:
            vals["datetime"] = r.get("datetime")
            return vals
    return None


# ---------- Naver ----------

def naver_quarter(code: str) -> dict:
    d = get_json(f"https://m.stock.naver.com/api/stock/{code}/finance/quarter")["financeInfo"]
    cons = {t["key"]: t.get("isConsensus") == "Y" for t in d["trTitleList"]}
    rows = {r["title"]: {k: num(v.get("value")) for k, v in r["columns"].items()} for r in d["rowList"]}
    return {"cons": cons, "rows": rows}


def naver_annual(code: str) -> dict:
    d = get_json(f"https://m.stock.naver.com/api/stock/{code}/finance/annual")["financeInfo"]
    cons = {t["key"]: t.get("isConsensus") == "Y" for t in d["trTitleList"]}
    rows = {r["title"]: {k: num(v.get("value")) for k, v in r["columns"].items()} for r in d["rowList"]}
    return {"cons": cons, "rows": rows}


def valuation(code: str) -> dict:
    integ = get_json(f"https://m.stock.naver.com/api/stock/{code}/integration")
    info = {x.get("code"): x.get("value") for x in integ.get("totalInfos", [])}
    basic = get_json(f"https://m.stock.naver.com/api/stock/{code}/basic")
    price = num(basic.get("closePrice"))
    ann = naver_annual(code)
    eps26 = ann["rows"].get("EPS", {}).get("202612")
    bps26 = ann["rows"].get("BPS", {}).get("202612")
    return {
        "price": price,
        "price_at": str(basic.get("localTradedAt") or "")[:16].replace("T", " "),
        "per_ttm": num(info.get("per")),
        "per26": round(price / eps26, 2) if price and eps26 else num(info.get("cnsPer")),
        "pbr": num(info.get("pbr")),
        "pbr26": round(price / bps26, 2) if price and bps26 else None,
        "target": num((integ.get("consensusInfo") or {}).get("priceTargetMean")),
        "op26": (ann["rows"].get("영업이익", {}).get("202612") or 0) / 1e4 or None,
    }


def sk_numbers(cfg: dict, state: dict) -> dict:
    sk = cfg["skhynix"]
    try:
        act = notice_result(sk["code"], sk["notice_span"], "2026-10-01")
    except Exception as exc:
        print(f"sk notice skipped: {exc}", file=sys.stderr)
        act = None
    q = None
    try:
        q = naver_quarter(sk["code"])
    except Exception as exc:
        print(f"sk quarter skipped: {exc}", file=sys.stderr)
    if act:
        return {"kind": "actual", "sales": act["sales"], "op": act["op"], "src": "KRX 잠정실적 공시"}
    key = sk["quarter_key"]
    if q and key in q["cons"]:
        s, o = q["rows"].get("매출액", {}).get(key), q["rows"].get("영업이익", {}).get(key)
        if s and o is not None:
            s, o = round(s / 1e4, 2), round(o / 1e4, 2)
            if not q["cons"][key]:
                return {"kind": "actual", "sales": s, "op": o, "src": "네이버 분기 실적"}
            state["sk_cons_live"] = {"sales": s, "op": o}
            return {"kind": "consensus", "sales": s, "op": o, "src": "네이버 컨센서스"}
    live = state.get("sk_cons_live")
    if live:
        return {"kind": "consensus", "sales": live["sales"], "op": live["op"], "src": "저장된 컨센서스"}
    fb = sk["q3_consensus_fallback"]
    return {"kind": "consensus", "sales": fb["sales_jo"], "op": fb["op_jo"], "src": f"설정 컨센서스({fb['as_of']})"}


# ---------- formatting ----------

def f1(v, unit="조") -> str:
    if v is None:
        return "?"
    return f"{v:,.1f}{unit}" if abs(v) >= 0.05 or v == 0 else f"{v:,.2f}{unit}"


def pct(a, b) -> float | None:
    return round(a / b * 100, 1) if a is not None and b else None


def cut(s: str | None, n: int) -> str:
    if not s:
        return "자료 없음"
    s = s.strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def sec_bands() -> str:
    try:
        b = json.loads(SEC_CFG.read_text(encoding="utf-8"))["bands"]
        return (f"[SEC] 가격 기준을 따릅니다(1차 {b['sell1_lo']//10000}만~{b['sell1_hi']//10000}만원, "
                f"2차 {b['sell2_lo']//10000}만~{b['sell2_hi']//10000}만원).")
    except Exception:
        return "[SEC] 가격 구간 알림 기준을 따릅니다."



def mix_addon(d: dict) -> tuple[str, float, str]:
    """Won add-on only if the official text states a non-price mix figure. Else 0."""
    text = d.get("official_text") or ""
    foundry = None
    m = re.search(r"(?:파운드리|Foundry)[^0-9\-]{0,30}(-?[\d,.]+)\s*(조\s*원|조원|억원)", text, re.I)
    if m:
        raw = float(m.group(1).replace(",", ""))
        if "억" in m.group(2):
            raw /= 10000.0
        if -80 < raw < 80:
            foundry = round(raw, 2)
    d["foundry_op"] = foundry
    mix = None
    mm = re.search(r"(?:믹스\s*효과|구조적\s*(?:증분|가산)|mix effect)[^0-9\-]{0,24}(-?[\d,.]+)\s*(조\s*원|조원)", text, re.I)
    if mm and not re.search(r"가격|ASP|판가", mm.group(0)):
        mix = round(float(mm.group(1).replace(",", "")), 2)
    missing = []
    if d.get("ds_op") is None:
        missing.append("DS 영업이익")
    if d.get("hbm4_pct") is None:
        missing.append("HBM 매출 비중")
    if foundry is None:
        missing.append("파운드리 손익")
    if missing or mix is None:
        why = ", ".join(missing) if missing else "원문에 가격이 아닌 믹스 조 원 없음"
        return "계산 불가", 0.0, why
    return "계산 가능", mix, "확정 원문이 가격이 아닌 믹스 효과를 조 원으로 적음"


def evaluate(cfg: dict, d: dict) -> list[tuple[str, str, str]]:
    r = cfg["rules"]
    res = []
    gap = d.get("gap")
    if gap is None:
        res.append(("R1 마진 격차", "확인 불가", "DS 이익률 없음"))
    else:
        st = "충족" if gap <= r["margin_gap_ok_pp"] else "보통" if gap <= r["margin_gap_mid_pp"] else "미충족"
        res.append(("R1 마진 격차", st, f"{gap:+.1f}%p(기준 {r['margin_gap_ok_pp']:.0f}%p 이내)"))
    h = d.get("hbm4_pct")
    bar = cfg["hbm4_ref"]["bar_pct"]
    if h is None:
        res.append(("R2 HBM4 비중", "확인 불가", f"공식 수치 없음, 컨콜 수동 확인(기준 {bar}%)"))
    else:
        res.append(("R2 HBM4 비중", "충족" if h >= bar else "미충족", f"{h:g}%(기준 {bar}%)"))
    mx = d.get("mxnw_op")
    if mx is None:
        res.append(("R3 MX/NW 흑자", "확인 불가", "IR표 없음"))
    else:
        res.append(("R3 MX/NW 흑자", "충족" if mx >= r["mxnw_op_floor_jo"] else "미충족", f"{f1(mx)}"))
    t = d.get("q4_tone")
    if t is None:
        res.append(("R4 4분기 DS 전망", "확인 불가", "전망 문장 없음"))
    else:
        res.append(("R4 4분기 DS 전망", "충족" if t == "긍정" else "미충족" if t == "부정" else "보통", f"{t}(키워드 자동 분류)"))
    ratio = d.get("per_ratio")
    if ratio is None:
        res.append(("R5 PER 상대", "확인 불가", "PER 없음"))
    else:
        st = "충족" if ratio <= r["per_ratio_ok"] else "보통" if ratio <= r["per_ratio_mid"] else "미충족"
        res.append(("R5 PER 상대", st, f"삼성/SK {ratio:.2f}배(1.0 이하 충족)"))
    return res


def verdict(n_ok: int) -> tuple[str, str]:
    if n_ok >= 4:
        return "재평가 근거 강함", "판단 상향 검토. 새로 살 때는 [SEC] 매수 구간에서 나눠서만."
    if n_ok >= 2:
        return "일부 충족", "보유 유지, 추격 매수 금지. 비중은 그대로 두세요."
    return "근거 약함", "신규 매수 보류. 보유분은 [SEC] 가격 기준으로만 관리."


def build(cfg: dict, d: dict, now: datetime) -> tuple[str, str, int]:
    checks = evaluate(cfg, d)
    n_ok = sum(1 for _, st, _ in checks if st == "충족")
    head, act = verdict(n_ok)
    sk = d["sk"]
    sh = cfg["share_ref"]
    dram, hbm = sh["dram_trendforce_2q26"], sh["hbm_counterpoint_2q26"]
    sv, kv = d.get("sv") or {}, d.get("kv") or {}
    pre = cfg["samsung"]["prelim"]
    tot = d.get("total_op")
    lines_core = [
        "이게 뭐 알림: 삼성전자 3분기 확정실적의 부문 손익, HBM4 비중, 4분기 전망만 뽑아 SK하이닉스와 비교하고 재평가 조건을 점검합니다. 매매 신호 아님.",
        f"지금 결과/상태: 재평가 조건 {n_ok}/5 충족, {head}. ({now:%m-%d %H:%M} KST)",
        f"- 전사 영업이익 {f1(tot)} (잠정 {pre['op_jo']}조 대비 {f1((tot - pre['op_jo']) if tot is not None else None)})",
        f"- DS {f1(d.get('ds_op'))}, 이익률 {d.get('ds_opm') if d.get('ds_opm') is not None else '?'}% (전분기 {d.get('ds_opm_prev') if d.get('ds_opm_prev') is not None else '?'}%)",
        f"- MX/NW {f1(d.get('mxnw_op'))} (MX 단독 손익은 미공시)",
        f"- 디스플레이 {f1(d.get('sdc_op'))}, 하만 {f1(d.get('harman_op'))}",
        "- HBM4 매출 비중: " + (f"{d['hbm4_pct']:g}% (원문: {cut(d.get('hbm4_snip'), 60)})" if d.get("hbm4_pct") is not None
                             else "공식 자료에 수치 없음. 참고: " + cfg["hbm4_ref"]["text"]),
        "- 믹스 가산: " + (lambda r: f"{r[0]}, {r[1]:.2f}조 원. {r[2]}. 정상화 43.6조, 반영 {43.6 + r[1]:.2f}조. +20조는 민감도 상한")(mix_addon(d)),
        f"- 4분기 전망: {cut(d.get('q4_overall'), 70)}",
        f"- 4분기 메모리: {cut(d.get('q4_memory'), 80)}",
        f"- SK 비교 마진: 삼성 DS {d.get('ds_opm', '?')}% vs SK {d.get('sk_opm', '?')}% ({'SK 3분기 실적' if sk['kind'] == 'actual' else 'SK 3분기 컨센서스, 실적 전'}), 격차 {d['gap']:+.1f}%p" if d.get("gap") is not None else "- SK 비교 마진: 계산 불가",
        f"- 점유율(조사기관, 2분기): D램 삼성 {dram['samsung']}% vs SK {dram['skhynix']}%, HBM 삼성 {hbm['samsung']}% vs SK {hbm['skhynix']}%",
        f"- 매출 대리지표: 삼성 메모리 {f1(d.get('mem_sales'))} vs SK {f1(sk.get('sales'))}, 삼성 몫 {d.get('mem_share', '?')}% (전분기 {d.get('mem_share_prev', '?')}%)",
        f"- 밸류: 삼성 {sv.get('price', 0):,.0f}원, 26E PER {sv.get('per26', '?')}배, PBR {sv.get('pbr', '?')}배 / SK {kv.get('price', 0):,.0f}원, 26E PER {kv.get('per26', '?')}배, PBR {kv.get('pbr', '?')}배",
        f"- 4분기 암시 영업이익(연간 컨센서스-누적): {f1(d.get('q4_implied'))}",
        "- 조건: " + " / ".join(f"{k} {st}({why})" for k, st, why in checks),
    ]
    tail = [
        f"지금 할 일: {act}",
        "나중에: 11월 중순 분기보고서와 조사기관 3분기 점유율(11월 말)이 나오면 R2와 점유율을 다시 봅니다."
        + (" SK 3분기 실적이 나오면 11/3까지 1회 갱신합니다." if sk["kind"] != "actual" else ""),
        "익절/줄일 때: " + sec_bands(),
        "하지 말 것: 실적 당일 급등 추격 매수, HBM4 비중 기사 제목만 보고 판단 변경, 이 알림만으로 매매.",
    ]
    optional = []
    if d.get("news"):
        optional.append("- 보도(미검증): " + " | ".join(d["news"]))
    missing = d.get("missing") or []
    if missing:
        optional.append("- 빠진 자료: " + ", ".join(missing))
    body_lines = lines_core + optional + tail
    body = "\n".join(body_lines)
    drop_order = ["- 보도(미검증)", "- 4분기 메모리", "- 매출 대리지표", "- 4분기 암시"]
    for prefix in drop_order:
        if len(body.encode("utf-8")) <= MAX_BYTES:
            break
        body_lines = [x for x in body_lines if not x.startswith(prefix)]
        body = "\n".join(body_lines)
    if len(body.encode("utf-8")) > MAX_BYTES:
        body = body.encode("utf-8")[:MAX_BYTES].decode("utf-8", errors="ignore")
    title = f"[SECQ3] 3Q 확정 재평가 {n_ok}/5 | {act.split('.')[0]}"
    return title, body, n_ok


# ---------- gather ----------

def gather(cfg: dict, state: dict, now: datetime, opts: dict) -> dict:
    s = cfg["samsung"]
    qlabel = opts.get("qlabel") or s["quarter_label"]
    d: dict = {"missing": []}
    deck = {"status": "no_pdf"}
    try:
        raw = fetch(opts.get("deck") or s["deck_url"], timeout=60)
        deck = parse_deck(raw, qlabel) if raw else {"status": "no_pdf"}
    except Exception as exc:
        deck = {"status": "fetch_fail", "why": type(exc).__name__}
    d["deck_status"] = deck.get("status")
    news_text, news_url = None, None
    try:
        if opts.get("news"):
            raw = fetch(opts["news"])
            news_text = newsroom_text(raw)[0] if raw else None
            news_url = opts["news"]
        else:
            news_text, news_url = find_newsroom(s)
    except Exception as exc:
        print(f"newsroom failed: {exc}", file=sys.stderr)
    nr = parse_newsroom(news_text, opts.get("outlook") or s["outlook_head"]) if news_text else {"op": {}, "sales": {}, "outlook": []}
    d["news_ok"] = bool(nr["op"].get("DS") is not None)
    d["deck_ok"] = deck.get("status") == "ok"
    if not d["deck_ok"]:
        d["missing"].append(f"IR자료 표({deck.get('why') or deck.get('status')})")
    if not d["news_ok"]:
        d["missing"].append("보도자료")

    dop, dsl = (deck.get("op") or {}), (deck.get("sales") or {})
    pick = lambda k: (dop.get(k) or (None, None))[1] if k in dop else nr["op"].get(k)  # noqa: E731
    d["ds_op"] = pick("DS")
    d["sdc_op"] = pick("SDC")
    d["harman_op"] = pick("Harman")
    d["mxnw_op"] = (dop.get("MX / NW") or (None, None))[1]
    d["total_op"] = (dop.get("Total") or (None, None))[1] if "Total" in dop else nr["op"].get("Total")
    ds_sales = (dsl.get("DS") or (None, None))[1] if "DS" in dsl else nr["sales"].get("DS")
    d["ds_opm"] = pct(d["ds_op"], ds_sales)
    if "DS" in dsl and "DS" in dop:
        d["ds_opm_prev"] = pct(dop["DS"][0], dsl["DS"][0])
    d["mem_sales"] = (dsl.get("Memory") or (None, None))[1]
    mem_prev = (dsl.get("Memory") or (None, None))[0]

    pk = outlook_pick(nr.get("outlook") or [])
    d["q4_overall"], d["q4_memory"] = pk.get("overall"), pk.get("memory") or pk.get("ds")
    d["q4_tone"] = tone(pk.get("memory") or pk.get("ds"), cfg["rules"])

    d["official_text"] = "\n".join([news_text or "", deck.get("text") or ""])
    d["hbm4_pct"], d["hbm4_snip"] = hbm4_official([news_text or "", deck.get("text") or ""])
    if d["hbm4_pct"] is None and now >= datetime(2026, 10, 29, 11, 0, tzinfo=KST) and not opts.get("offline"):
        d["news"] = hbm4_news(now)

    sk = sk_numbers(cfg, state)
    d["sk"] = sk
    d["sk_opm"] = pct(sk["op"], sk["sales"])
    d["gap"] = round(d["sk_opm"] - d["ds_opm"], 1) if d.get("sk_opm") is not None and d.get("ds_opm") is not None else None
    if d["mem_sales"] and sk.get("sales"):
        d["mem_share"] = pct(d["mem_sales"], d["mem_sales"] + sk["sales"])
    q2 = cfg["skhynix"]["q2_actual"]
    if mem_prev:
        d["mem_share_prev"] = pct(mem_prev, mem_prev + q2["sales_jo"])
    try:
        d["sv"], d["kv"] = valuation(s["code"]), valuation(cfg["skhynix"]["code"])
        if d["sv"].get("per26") and d["kv"].get("per26"):
            d["per_ratio"] = round(d["sv"]["per26"] / d["kv"]["per26"], 2)
    except Exception as exc:
        d["missing"].append(f"밸류 조회({type(exc).__name__})")
    try:
        qq = naver_quarter(s["code"])["rows"].get("영업이익", {})
        q1, q2v = qq.get("202603"), qq.get("202606")
        op26 = (d.get("sv") or {}).get("op26")
        if op26 and q1 and q2v and d.get("total_op") is not None:
            d["q4_implied"] = round(op26 - q1 / 1e4 - q2v / 1e4 - d["total_op"], 1)
    except Exception as exc:
        print(f"implied q4 skipped: {exc}", file=sys.stderr)
    d["news_url"] = news_url
    return d


# ---------- send ----------

def send(cfg: dict, title: str, body: str, dry: bool, priority: int = 4) -> None:
    if dry:
        print("DRY", title)
        print(body)
        print(f"[{len(body.encode('utf-8'))} bytes]")
        return
    publish(cfg["ntfy_topic"], title, body, priority=priority, tags=["bar_chart"], server=cfg["ntfy_server"])


def ops(cfg: dict, state: dict, now: datetime, key: str, title_word: str, detail: str, dry: bool) -> None:
    if state["ops_sent"].get(key):
        return
    title = f"[SECQ3] 3분기 자료 {title_word}"
    body = "\n".join([
        "이게 뭐 알림: 삼성전자 3분기 확정실적 비교 알림의 운영 안내.",
        f"지금 결과/상태: {detail}. {now:%Y-%m-%d %H:%M} KST.",
        "지금 할 일: 판단 유보. 숫자 확인 전에는 비중을 바꾸지 마세요.",
        "나중에: 다음 원샷에서 다시 확인합니다. 자료: https://www.samsung.com/global/ir/",
        "익절/줄일 때: 자료 문제만으로 줄이지 마세요.",
        "하지 말 것: 기사 숫자로 공식 자료를 대신하지 마세요.",
    ])
    send(cfg, title, body, dry, priority=3)
    if not dry:
        state["ops_sent"][key] = now.isoformat(timespec="seconds")


def install(cfg: dict) -> None:
    body = "\n".join([
        "이게 뭐 알림: 삼성전자 3분기 확정실적(10/29 10:00 컨콜) 부문 손익, HBM4 비중, 4분기 전망을 SK하이닉스와 비교하는 원샷 알림 설치 확인.",
        "지금 결과/상태: 설치 완료. 실행 10/29 08:10, 09:40, 11:10, 16:10, 10/30 09:16, SK 갱신 11/3 09:16 KST.",
        "지금 할 일: 없음. 결과는 10/29 아침 공식 자료가 올라오면 1회 옵니다.",
        "나중에: SK하이닉스 3분기 실적이 삼성보다 늦으면 11/3까지 1회 갱신합니다.",
        "익절/줄일 때: " + sec_bands(),
        "하지 말 것: 이 설치 알림을 매매 신호로 보지 마세요.",
    ])
    publish(cfg["ntfy_topic"], "[SECQ3] 설치 확인 | 매매 신호 아님", body, priority=2, tags=["white_check_mark"],
            server=cfg["ntfy_server"])


def main() -> int:
    argv = sys.argv[1:]
    mode = (argv[0] if argv and not argv[0].startswith("--") else "auto").lower()
    if mode not in {"auto", "dry", "force", "install"}:
        raise SystemExit(f"unknown mode: {mode}")
    opts = {}
    for flag in ("--deck", "--news", "--qlabel", "--outlook", "--now"):
        if flag in argv and argv.index(flag) + 1 < len(argv):
            opts[flag[2:]] = argv[argv.index(flag) + 1]
    opts["offline"] = "--offline" in argv
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    if mode == "install":
        install(cfg)
        return 0
    dry = mode == "dry"
    now = datetime.fromisoformat(opts["now"]).replace(tzinfo=KST) if opts.get("now") else datetime.now(KST)
    day = now.strftime("%Y-%m-%d")
    if not dry and (now.year != int(cfg["year_guard"]) or not cfg["window_start"] <= day <= cfg["window_end"]):
        print("outside 2026 window; silent")
        return 0
    state = read_state()

    if state.get("result_sent") and mode != "force":
        if state.get("sk_kind_in_result") == "actual" or state.get("sk_update_sent"):
            print("result already sent; silent")
            return 0
        sk = sk_numbers(cfg, state)
        if sk["kind"] != "actual":
            print("SK hynix Q3 not out yet; silent")
            if not dry:
                save_state(state, now)
            return 0
        res = dict(state.get("result") or {})
        res["sk"] = sk
        res["sk_opm"] = pct(sk["op"], sk["sales"])
        if res.get("ds_opm") is not None and res["sk_opm"] is not None:
            res["gap"] = round(res["sk_opm"] - res["ds_opm"], 1)
        if res.get("mem_sales"):
            res["mem_share"] = pct(res["mem_sales"], res["mem_sales"] + sk["sales"])
        try:
            res["sv"], res["kv"] = valuation(cfg["samsung"]["code"]), valuation(cfg["skhynix"]["code"])
            if res["sv"].get("per26") and res["kv"].get("per26"):
                res["per_ratio"] = round(res["sv"]["per26"] / res["kv"]["per26"], 2)
        except Exception as exc:
            print(f"valuation skipped: {exc}", file=sys.stderr)
        res["missing"] = ["삼성 숫자는 10/29 결과 재사용"]
        title, body, _ = build(cfg, res, now)
        title = title.replace("3Q 확정", "SK 3Q 반영")
        send(cfg, title, body, dry)
        if not dry:
            state["sk_update_sent"] = True
            save_state(state, now)
        return 0

    d = gather(cfg, state, now, opts)
    full = d["deck_ok"] and d["news_ok"]
    partial = d["deck_ok"] or d["news_ok"]
    late = now >= datetime(2026, 10, 29, 11, 0, tzinfo=KST)
    last_try = now >= datetime(2026, 10, 29, 16, 0, tzinfo=KST)
    if full or (partial and (late or dry)) or mode == "force" and partial:
        title, body, n_ok = build(cfg, d, now)
        send(cfg, title, body, dry)
        if not dry:
            keep = {k: d.get(k) for k in ("ds_op", "ds_opm", "ds_opm_prev", "sdc_op", "harman_op", "mxnw_op",
                                          "total_op", "mem_sales", "mem_share_prev", "hbm4_pct", "hbm4_snip",
                                          "q4_overall", "q4_memory", "q4_tone", "q4_implied")}
            state.update({"result_sent": True, "result_at": now.isoformat(timespec="seconds"), "result": keep,
                          "sk_kind_in_result": d["sk"]["kind"]})
            save_state(state, now)
        return 0
    if last_try:
        if d.get("deck_status") in ("parse_fail",) and not d["news_ok"]:
            ops(cfg, state, now, f"parse@{day}", "파싱 실패", "IR자료는 있으나 부문 표를 못 읽었고 보도자료도 없음", dry)
        elif d.get("deck_status") == "fetch_fail" and not d["news_ok"]:
            ops(cfg, state, now, f"fetch@{day}", "조회 실패", "IR자료 접속 오류, 보도자료 없음", dry)
        else:
            ops(cfg, state, now, f"missing@{day}", "미게시", "IR자료와 보도자료가 아직 없음", dry)
    else:
        print(f"not published yet (deck={d.get('deck_status')}, news={d['news_ok']}); silent until next one-shot")
    if not dry:
        save_state(state, now)
    return 0


if __name__ == "__main__":
    sys.exit(main())
