#!/usr/bin/env python3
"""[JYP] JYP Ent.(035900) 3분기 잠정실적 1회 알림 (2026-10-20 10:44 KST 채팅 승인, DOOSAN과 같은 구조).

  jyp_monitor.py auto   one-shot 11/9~11/17 평일 17:05: DART 공시검색 + KRX 공시 피드(네이버)에서 3분기
                        영업(잠정)실적(또는 3분기 분기보고서)을 찾으면 1번만 보낸다. 없으면 침묵, state 안 바꿈.
                        11/18 09:05(final_kst) 이후 실행은 마지막 확인: 그때까지 못 보냈으면 미발표 안내 1번.
  jyp_monitor.py snap   port-alerts morning 슬롯(평일 09:00). 2027 컨센서스(EPS, 영업이익) 기준값을 조용히 저장.
                        발송 없음. 값이 바뀐 날만 state 를 쓴다. 실적 알림을 보낸 뒤에는 아무것도 안 한다.
  jyp_monitor.py dry    실데이터로 판정만 출력 (ntfy 없음, state 쓰기 없음). 2분기 공시로 파서 자체 점검.
                        JYP_NOW="YYYY-MM-DD HH:MM" 으로 시각을 바꿔 볼 수 있다.
  jyp_monitor.py sim    시뮬레이션: JYP_NOW, JYP_INJECT(가짜 데이터 JSON 경로), JYP_STATE(저장소 밖 임시 state 경로)
                        필수. 네트워크와 ntfy 를 쓰지 않고 auto 와 같은 판정을 돌린다.

매매 신호가 아니다. 설치 확인 발송 모드는 두지 않는다(이번 승인: 테스트 발송 없음).
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic, route_topic  # noqa: E402
from naver_api import html_text  # noqa: E402

KST = timezone(timedelta(hours=9))
CFG_PATH = ROOT / "configs" / "jyp.json"
STATE_PATH = ROOT / "state" / "jyp.json"
UA = "Mozilla/5.0 (cwally-alerts; +mailto:sosmos3218@gmail.com)"
UNIT_TO_EOK = {"백만원": 100.0, "억원": 1.0, "천원": 100000.0, "원": 100000000.0}
DART_URL = "https://dart.fss.or.kr/dsab007/detailSearch.ax"
DART_VIEW = "https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcp}"
WR_BASE = "https://navercomp.wisereport.co.kr/v2/company/"
LABELS = {
    "rev": r"매출액",
    "op": r"영업이익",
    "pretax": r"법인세비용차감전계속사업이익",
    "net": r"당기순이익",
    "net_owner": r"지배기업\s*소유주지분\s*순이익",
}


class Ctx:
    """실행 모드. dry/sim 은 절대 발송하지 않고, dry 는 state 를 쓰지 않으며, sim 은 JYP_STATE 에만 쓴다."""

    def __init__(self, mode: str, now: datetime, inject: dict | None, state_path: Path | None):
        self.mode = mode
        self.now = now
        self.inject = inject
        self.state_path = state_path
        self.no_send = mode in ("dry", "sim")


# ---------- io ----------

def load_json(path: Path) -> dict:
    try:
        v = json.loads(path.read_text(encoding="utf-8"))
        return v if isinstance(v, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save(ctx: Ctx, state: dict) -> None:
    if ctx.mode == "dry":
        print("DRY RUN: state not written")
        return
    path = ctx.state_path or STATE_PATH
    state["updated_at"] = ctx.now.strftime("%Y-%m-%d %H:%M KST")
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"state written: {path}")


def http(url: str, data: bytes | None = None, referer: str | None = None, tries: int = 3) -> str:
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
                raw = resp.read()
                ctype = resp.headers.get("Content-Type", "")
            enc = "euc-kr" if "euc-kr" in ctype.lower() else "utf-8"
            return raw.decode(enc, errors="ignore")
        except Exception as exc:
            err = exc
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"{url.split('?')[0]}: {err}")


def send(ctx: Ctx, cfg: dict, title: str, body: str, pri: int, ops: bool = False) -> None:
    topic = resolve_topic("ops" if ops else "port", cfg.get("ntfy_topic"))
    print(f"TITLE: {title}\nPRIORITY: {pri}\nTOPIC: {route_topic(topic, title)}\n{body}")
    if ctx.no_send:
        print(f"{ctx.mode.upper()} RUN: no ntfy sent")
        return
    publish(topic, title[:180], body, pri, ["office"], resolve_server(cfg.get("ntfy_server")))


def eok(v: float | None) -> str:
    return "?" if v is None else f"{v:,.0f}억"


def pct(a: float | None, b: float | None) -> float | None:
    if a is None or not b:
        return None
    return (a / b - 1) * 100


def at_kst(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=KST)


# ---------- 공시 파싱 ----------

def _num(tok: str) -> float | None:
    try:
        return float(tok.replace(",", ""))
    except ValueError:
        return None


def parse_filing(text: str, pstart: str, pend: str) -> dict | None:
    """연결재무제표 기준 영업(잠정)실적 본문 -> 억원. 기간이 다르거나 영업이익이 없으면 None."""
    t = re.sub(r"\s+", " ", text or "")
    if not re.search(r"당기실적\s*" + re.escape(pstart) + r"\s*~\s*" + re.escape(pend), t):
        return None
    um = re.search(r"단위\s*[:：]\s*(백만원|억원|천원|원)", t)
    if not um:
        return None
    div = UNIT_TO_EOK[um.group(1)]
    out: dict = {"unit": um.group(1)}
    for key, label in LABELS.items():
        m = re.search(r"(?<![가-힣])" + label + r"\s*당해실적\s*(.*?)\s*누계실적", t)
        if not m:
            continue
        toks = m.group(1).split()
        # 순서: 당기, 전기, 전기대비%, 전환여부, 전년동기, 전년동기대비%, 전환여부
        cur = _num(toks[0]) if toks else None
        if cur is None:
            continue
        out[key] = round(cur / div, 1)
        if len(toks) >= 5 and _num(toks[4]) is not None:
            out[key + "_yoy_base"] = round(_num(toks[4]) / div, 1)
        if len(toks) >= 2 and _num(toks[1]) is not None:
            out[key + "_prev_q"] = round(_num(toks[1]) / div, 1)
    if "op" not in out:
        return None
    note = re.search(r"기타\s*투자판단에\s*참고할\s*사항(.*)$", t)
    out["remarks"] = (note.group(1) if note else "")[:1500]
    return out


_NOTICE_CACHE: dict = {}


def notices(code: str, size: int = 40) -> list[dict]:
    if (code, size) in _NOTICE_CACHE:
        return _NOTICE_CACHE[(code, size)]
    raw = json.loads(http(f"https://stock.naver.com/api/domestic/detail/notice?itemCode={code}&page=1&pageSize={size}"))
    out = [{"title": str(r.get("title") or "").strip(), "datetime": str(r.get("datetime") or ""),
            "text": html_text(str(r.get("contents") or ""))} for r in raw if isinstance(r, dict)] if isinstance(raw, list) else []
    _NOTICE_CACHE[(code, size)] = out
    return out


def naver_filing(cfg: dict, pstart: str, pend: str, since: str, size: int = 40) -> dict | None:
    """KRX 공시 피드(네이버)의 영업(잠정)실적 중 해당 기간을 파싱. 최신(정정 포함) 우선."""
    for r in notices(cfg["code"], size):
        if "영업(잠정)실적" not in r["title"] or r["datetime"][:10] < since:
            continue
        got = parse_filing(r["text"], pstart, pend)
        if got:
            got.update(day=r["datetime"][:10], title=r["title"], src="KRX 공시(네이버 피드)",
                       text_hit=r["text"])
            return got
    return None


def dart_rows(cfg: dict, since: str, now: datetime) -> list[dict]:
    form = urllib.parse.urlencode([
        ("currentPage", "1"), ("maxResults", "30"), ("textCrpNm", cfg["dart_name"]),
        ("startDate", since.replace("-", "")), ("endDate", now.strftime("%Y%m%d")),
    ]).encode()
    page = http(DART_URL, data=form)
    if "공시서류검색" not in page and "<tr" not in page:
        raise RuntimeError("DART 응답 형식 이상")
    rows = []
    for row in re.findall(r"(?s)<tr[^>]*>(.*?)</tr>", page):
        text = html_text(row)
        rcp = re.search(r"rcpNo=(\d+)", row)
        dm = re.findall(r"(\d{4})\.(\d{2})\.(\d{2})", text)
        if rcp and dm:
            rows.append({"text": text, "rcp": rcp.group(1), "day": "-".join(dm[-1])})
    return rows


def dart_doc_text(rcp: str) -> str:
    """DART 원문(첫 문서) 텍스트. 실패하면 예외."""
    main = http(DART_VIEW.format(rcp=rcp))
    m = re.search(r"viewDoc\(\s*'(\d+)'\s*,\s*'(\d+)'\s*,\s*'([^']*)'\s*,\s*'([^']*)'\s*,\s*'([^']*)'\s*,\s*'([^']*)'", main)
    if not m:
        raise RuntimeError("DART viewDoc 없음")
    q = urllib.parse.urlencode({"rcpNo": m.group(1), "dcmNo": m.group(2), "eleId": m.group(3),
                                "offset": m.group(4), "length": m.group(5), "dtd": m.group(6)})
    return html_text(http(f"https://dart.fss.or.kr/report/viewer.do?{q}", referer=DART_VIEW.format(rcp=rcp)))


def naver_quarter_actual(code: str, key: str) -> dict | None:
    raw = json.loads(http(f"https://m.stock.naver.com/api/stock/{code}/finance/quarter"))
    fi = raw.get("financeInfo") or {}
    cons = {t.get("key"): t.get("isConsensus") for t in fi.get("trTitleList") or []}
    if cons.get(key) != "N":
        return None
    vals = {}
    for row in fi.get("rowList") or []:
        name = {"매출액": "rev", "영업이익": "op", "당기순이익": "net"}.get(row.get("title"))
        if not name:
            continue
        v = ((row.get("columns") or {}).get(key) or {}).get("value")
        try:
            vals[name] = float(str(v).replace(",", ""))
        except (TypeError, ValueError):
            pass
    return vals if "op" in vals else None


def find_q3(ctx: Ctx, cfg: dict) -> tuple[dict | None, list[str], list[str]]:
    """(결과 또는 None, 성공한 소스, 실패 메모). 두 소스 모두 실패면 RuntimeError."""
    if ctx.inject is not None:
        f = ctx.inject.get("filing")
        return (dict(f) if f else None), ["inject"], []
    e = cfg["earnings"]
    ok, fails = [], []
    found = None
    try:
        found = naver_filing(cfg, e["period_start"], e["period_end"], e["since"])
        ok.append("KRX 공시 피드")
    except Exception as exc:
        fails.append(f"KRX 공시 피드 조회 실패({exc.__class__.__name__})")
    dart_hit = None
    try:
        rows = dart_rows(cfg, e["since"], ctx.now)
        ok.append("DART")
        prelim = [r for r in rows if "영업(잠정)실적" in r["text"]]
        qrep = [r for r in rows if "분기보고서" in r["text"] and e["period_end"][:7].replace("-", ".") in r["text"]]
        pick = [r for r in prelim if "정정" not in r["text"]] or prelim
        if pick:
            dart_hit = dict(pick[0], kind="prelim")
        elif qrep:
            dart_hit = dict(([r for r in qrep if "정정" not in r["text"]] or qrep)[0], kind="quarterly")
    except Exception as exc:
        fails.append(f"DART 조회 실패({exc.__class__.__name__})")
    if not ok:
        raise RuntimeError("; ".join(fails))
    if found:
        if dart_hit and dart_hit["kind"] == "prelim":
            found["rcp"] = dart_hit["rcp"]
        return found, ok, fails
    if dart_hit and dart_hit["kind"] == "prelim":
        try:
            got = parse_filing(dart_doc_text(dart_hit["rcp"]), e["period_start"], e["period_end"])
        except Exception as exc:
            got = None
            fails.append(f"DART 원문 읽기 실패({exc.__class__.__name__})")
        if got:
            got.update(day=dart_hit["day"], rcp=dart_hit["rcp"], src="DART 원문", title="연결재무제표기준영업(잠정)실적(공정공시)")
            return got, ok, fails
        return {"day": dart_hit["day"], "rcp": dart_hit["rcp"], "pending": True, "src": "DART 목록"}, ok, fails
    if dart_hit and dart_hit["kind"] == "quarterly":
        act = None
        try:
            act = naver_quarter_actual(cfg["code"], e["period_end"][:7].replace("-", ""))
        except Exception as exc:
            fails.append(f"네이버 분기 실적 조회 실패({exc.__class__.__name__})")
        res = {"day": dart_hit["day"], "rcp": dart_hit["rcp"], "src": "DART 분기보고서", "quarterly": True,
               "title": "분기보고서"}
        if act:
            res.update(act)
        else:
            res["pending"] = True
        return res, ok, fails
    return None, ok, fails


# ---------- 컨센서스 2027 ----------

def _table_cols(html: str, year: str) -> dict | None:
    """연간 표에서 {year}/12(E) 열의 영업이익, EPS, 매출액 (억원, 원)."""
    rows = re.findall(r"(?s)<tr[^>]*>(.*?)</tr>", html)
    periods: list[str] | None = None
    idx = None
    for row in rows:
        cells = [html_text(c) for c in re.findall(r"(?s)<t[hd][^>]*>(.*?)</t[hd]>", row)]
        per = [c for c in cells if re.match(r"^\d{4}/\d{2}", c)]
        if len(per) >= 3 and any(c.startswith(f"{year}/12") for c in per):
            periods = per
            idx = next(i for i, c in enumerate(per) if c.startswith(f"{year}/12"))
            if "(E)" not in periods[idx].replace(" ", ""):
                return None  # 추정치 열이 아니면 쓰지 않음
            break
    if periods is None or idx is None:
        return None
    out: dict = {}
    for row in rows:
        th = re.search(r"(?s)<th[^>]*>(.*?)</th>", row)
        tds = [html_text(c) for c in re.findall(r"(?s)<td[^>]*>(.*?)</td>", row)]
        if not th or len(tds) < len(periods):
            continue
        label = re.sub(r"\s+", "", html_text(th.group(1)))
        val = _num(tds[idx]) if idx < len(tds) else None
        if val is None:
            continue
        if label in ("영업이익",) and "op" not in out:
            out["op"] = val
        elif label.startswith("EPS") and "eps" not in out:
            out["eps"] = val
        elif label.startswith("매출액") and "rev" not in out:
            out["rev"] = val
    return out if ("op" in out or "eps" in out) else None


def consensus_2027(ctx: Ctx, cfg: dict) -> tuple[dict | None, str]:
    if ctx.inject is not None:
        c = ctx.inject.get("cons")
        return (dict(c) if c else None), ("inject" if c else "inject: 없음")
    code, year = cfg["code"], cfg["consensus_2027"]["year"]
    errs = []
    try:
        page = f"{WR_BASE}c1010001.aspx?cmp_cd={code}"
        html = http(page)
        enc = re.search(r"encparam\s*:\s*'([^']+)'", html)
        cid = re.search(r"\bid\s*:\s*'([^']+)'", html)
        if not enc:
            raise RuntimeError("encparam 없음")
        url = (f"{WR_BASE}ajax/cF1001.aspx?cmp_cd={code}&fin_typ=0&freq_typ=Y&encparam={enc.group(1)}"
               + (f"&id={cid.group(1)}" if cid else ""))
        got = _table_cols(http(url, referer=page), year)
        if got:
            got["src"] = "FnGuide 컨센서스(wisereport)"
            return got, "wisereport"
        errs.append("wisereport 2027(E) 열 없음")
    except Exception as exc:
        errs.append(f"wisereport 실패({exc.__class__.__name__})")
    try:
        html = http(f"https://comp.fnguide.com/SVO2/ASP/SVD_Main.asp?pGB=1&gicode=A{code}&cID=&MenuYn=Y&ReportGB=&NewMenuID=101&stkGb=701")
        i = html.find('id="highlight_D_Y"')
        seg = html[i:i + 60000] if i >= 0 else html
        got = _table_cols(seg, year)
        if got:
            got["src"] = "FnGuide Financial Highlight"
            return got, "fnguide"
        errs.append("FnGuide 2027(E) 열 없음")
    except Exception as exc:
        errs.append(f"FnGuide 실패({exc.__class__.__name__})")
    return None, "; ".join(errs)


def cons_compare(cur: dict | None, cs: dict, up_pct: float, after_filing: bool = True) -> tuple[str, str]:
    """(본문 줄, 방향 up/down/flat/unknown)."""
    if not cur:
        return "2027 컨센서스: 조회 실패(판단에 쓰지 않음)", "unknown"
    base = cs.get("baseline")
    last = cs.get("last")
    head = f"2027 컨센서스: EPS {cur.get('eps', 0):,.0f}원, 영업이익 {eok(cur.get('op'))}" if cur.get("eps") is not None \
        else f"2027 컨센서스: 영업이익 {eok(cur.get('op'))}"
    if not base:
        return head + " (비교 기준값 없음)", "unknown"
    d_eps, d_op = pct(cur.get("eps"), base.get("eps")), pct(cur.get("op"), base.get("op"))
    parts = []
    if d_eps is not None:
        parts.append(f"EPS {d_eps:+.1f}%")
    if d_op is not None:
        parts.append(f"영업이익 {d_op:+.1f}%")
    line = head + f". 기준 {base.get('as_of')} 대비 " + (", ".join(parts) if parts else "비교 불가")
    same_as_last = last and all(cur.get(k) == last.get(k) for k in ("eps", "op"))
    if same_as_last and after_filing:
        line += f". 공시 반영 갱신 전({last.get('as_of')} 값 그대로)"
    moves = [x for x in (d_eps, d_op) if x is not None]
    if moves and max(moves) >= up_pct and min(moves) > -up_pct:
        return line, ("flat" if same_as_last else "up")
    if moves and min(moves) <= -up_pct:
        return line, "down"
    return line, "flat"


# ---------- 투어 일정 ----------

def tour_line(ctx: Ctx, cfg: dict) -> str:
    if ctx.inject is not None:
        return ctx.inject.get("tour") or "2027 투어 일정: 확인 불가(발표 보도 못 찾음)"
    tc = cfg["tour"]
    pat = re.compile(tc["pattern"])
    cutoff = ctx.now - timedelta(days=int(tc.get("since_days", 45)))
    hits = []
    try:
        from email.utils import parsedate_to_datetime
        for q in tc["queries"]:
            url = "https://news.google.com/rss/search?" + urllib.parse.urlencode({"q": q, "hl": "ko", "gl": "KR", "ceid": "KR:ko"})
            rss = http(url, tries=2)
            for title, pub in re.findall(r"(?s)<item>.*?<title>(.*?)</title>.*?<pubDate>(.*?)</pubDate>", rss):
                title = html_text(title)
                try:
                    when = parsedate_to_datetime(pub).astimezone(KST)
                except Exception:
                    continue
                if when < cutoff or when > ctx.now:
                    continue
                if pat.search(title) and any(a in title for a in tc["artists"]) and any(w in title for w in tc["confirm_words"]):
                    hits.append((when, title))
    except Exception as exc:
        return f"2027 투어 일정: 확인 불가(뉴스 조회 실패 {exc.__class__.__name__})"
    if not hits:
        return "2027 투어 일정: 확인 불가(최근 발표 보도 못 찾음)"
    hits.sort(reverse=True)
    when, title = hits[0]
    more = f" 외 {len(hits) - 1}건" if len(hits) > 1 else ""
    return f"2027 투어 일정: 보도 있음, 공식 확인 전. {when.month}/{when.day} \"{title[:90]}\"{more}"


# ---------- 판정, 문구 ----------

def one_off(res: dict, cfg: dict) -> tuple[str, bool]:
    oc = cfg["earnings"]["one_off"]
    op, pre, net = res.get("op"), res.get("pretax"), res.get("net")
    flags = []
    large = False
    if op and op > 0 and pre is not None and pre < op * oc["pretax_vs_op_ratio"]:
        flags.append(f"세전이익 {eok(pre)}이 영업이익의 {pre / op * 100:.0f}%로 영업외 손실이 큼")
        large = True
    if op and op > 0 and net is not None and net < op * oc["net_vs_op_ratio"]:
        flags.append(f"순이익 {eok(net)}이 영업이익의 {net / op * 100:.0f}%")
        large = True
    words = [k for k in oc["keywords"] if k in (res.get("remarks") or "")]
    if words:
        flags.append("공시 비고에 " + ", ".join(f"'{w}'" for w in words[:3]) + " 언급")
    if flags:
        return "일회성 비용 신호: " + "; ".join(flags) + ". 내역은 실적 자료로 확인 필요", large
    return "일회성 비용 여부 확인 불가(잠정 공시에 비용 내역 없음. 세전이익, 순이익은 영업이익 대비 큰 차이 없음)", False


def decide(res: dict, cfg: dict, cons_dir: str, large: bool) -> tuple[str, str, int, str]:
    lo, hi = cfg["earnings"]["op_range_eok"]
    far = cfg["earnings"]["far_pct"] / 100
    op = res["op"]
    pos = "범위 아래" if op < lo else ("범위 위" if op > hi else "범위 안")
    if large:
        dec, why = "하향 검토", "큰 영업외 손실 또는 일회성 비용 신호"
    elif op < lo:
        dec, why = "하향 검토", f"영업이익이 예상 하단 {lo}억 아래"
    elif op > hi and cons_dir == "up":
        dec, why = "상향 검토", f"영업이익이 예상 상단 {hi}억 위이고 2027 컨센서스도 올라감"
    elif op > hi:
        dec, why = "중립 유지", "범위 위지만 2027 컨센서스 상향 확인 전"
    else:
        dec, why = "중립 유지", "예상 범위 안"
    outside_far = op < lo * (1 - far) or op > hi * (1 + far)
    pri = 4 if (outside_far or dec != "중립 유지") else 3
    return pos, dec, pri, why


def result_message(ctx: Ctx, cfg: dict, state: dict, res: dict) -> tuple[str, str, int]:
    lo, hi = cfg["earnings"]["op_range_eok"]
    cons, why_c = consensus_2027(ctx, cfg)
    if not cons:
        print(f"2027 consensus unavailable: {why_c}")
    cons_txt, cons_dir = cons_compare(cons, state.get("cons") or {}, cfg["consensus_2027"]["up_pct"])
    oneoff_txt, large = one_off(res, cfg)
    pos, dec, pri, why = decide(res, cfg, cons_dir, large)
    op = res["op"]
    title = f"[JYP] 3Q 영업이익 {op:,.0f}억 {pos} | {dec}"
    yoy = pct(op, res.get("op_yoy_base"))
    lines = [f"영업이익 {eok(op)} (예상 {lo}~{hi}억, {pos})"
             + (f". 전년 동기 {eok(res['op_yoy_base'])} 대비 {yoy:+.1f}%" if yoy is not None else "")]
    if res.get("rev") is not None:
        ry = pct(res["rev"], res.get("rev_yoy_base"))
        lines.append(f"매출 {eok(res['rev'])}" + (f" (전년 동기 대비 {ry:+.1f}%)" if ry is not None else ""))
    net = res.get("net_owner") if res.get("net_owner") is not None else res.get("net")
    if net is not None:
        base = res.get("net_owner_yoy_base") if res.get("net_owner") is not None else res.get("net_yoy_base")
        ny = pct(net, base)
        lines.append(f"순이익 {eok(net)}" + (f" (전년 동기 대비 {ny:+.1f}%)" if ny is not None else ""))
    lines.append(oneoff_txt)
    lines.append(cons_txt)
    lines.append(tour_line(ctx, cfg))
    lines.append(f"판단: 기존 {cfg['prior_view']} -> {dec} ({why})")
    link = f" DART {DART_VIEW.format(rcp=res['rcp'])}" if res.get("rcp") else ""
    kind = "분기보고서" if res.get("quarterly") else "잠정실적"
    subj = "분기보고서가" if res.get("quarterly") else "잠정실적이"
    lines.append(f"데이터: {res.get('day')} {kind}, {res.get('src')}.{link} 조회 {ctx.now:%Y-%m-%d %H:%M} KST")
    if dec == "상향 검토":
        todo = "지금 할 일: 바로 사지 말고, 내일 KRX 시가와 외인 수급이 같은 방향인지 본 뒤 비중 상향을 검토하세요."
    elif dec == "하향 검토":
        todo = "지금 할 일: 보유 중이면 비중 축소를 검토하세요. 시간외 가격에 바로 반응하지 말고 내일 KRX 시가를 보세요."
    else:
        todo = "지금 할 일: 없음. 중립/관망 유지. 내일 KRX 시가만 확인하세요."
    body = [
        f"이게 뭐 알림: JYP Ent.(035900) 3분기 {subj} 공시돼 예상 범위({lo}~{hi}억)와 비교한 1회 알림입니다. 매매 신호가 아닙니다.",
        "지금 결과/상태 (억원, 잠정치):",
    ] + [f"- {x}" for x in lines] + [
        todo,
        "나중에: 실적 컨퍼런스콜 자료와 2027 컨센서스 변경은 며칠 뒤 다시 보세요. 이 알림은 다시 오지 않습니다.",
        "익절/줄일 때: 보유 여부 미확인. 영업이익이 예상 하단보다 10% 넘게 낮거나 큰 일회성 비용이 확인되면 비중을 줄이는 쪽을 보세요.",
        "하지 말 것: 잠정치를 확정치로 보기. 공시 직후 시간외 가격 추격. 보도만 보고 투어 일정을 확정으로 보기. 신용, 미수. 투자 권유 아님.",
    ]
    return title, "\n".join(body), pri


def pending_message(ctx: Ctx, cfg: dict, res: dict) -> tuple[str, str, int]:
    title = "[JYP] 3분기 공시 접수 | 원문 확인, 판단 유지"
    body = "\n".join([
        "이게 뭐 알림: JYP Ent.(035900) 3분기 실적 공시가 DART에 올라왔지만 숫자를 아직 읽지 못한 마지막 확인입니다. 매매 신호가 아닙니다.",
        f"지금 결과/상태: {res.get('day')} 접수. 숫자는 DART 원문에서 확인하세요: {DART_VIEW.format(rcp=res.get('rcp'))}",
        f"지금 할 일: 원문의 영업이익을 예상 범위 {cfg['earnings']['op_range_eok'][0]}~{cfg['earnings']['op_range_eok'][1]}억과 비교만 하세요. 판단은 {cfg['prior_view']} 유지.",
        "나중에: 이 알림은 다시 오지 않습니다.",
        "익절/줄일 때: 해당 없음(보유 여부 미확인).",
        "하지 말 것: 숫자 확인 전 매매. 투자 권유 아님.",
    ])
    return title, body, 3


def final_missing_message(ctx: Ctx, cfg: dict, state: dict, notes: list[str]) -> tuple[str, str, int]:
    cons, _ = consensus_2027(ctx, cfg)
    cons_txt, _ = cons_compare(cons, state.get("cons") or {}, cfg["consensus_2027"]["up_pct"], after_filing=False)
    title = "[JYP] 3분기 실적 미발표 | 판단 유지(중립/관망)"
    lines = [f"11/9~11/17 평일 17:05와 {ctx.now:%m/%d %H:%M} 확인까지 DART, KRX 공시에 3분기 잠정실적과 분기보고서 없음",
             cons_txt]
    if notes:
        lines.append("참고: " + "; ".join(notes)[:300])
    body = "\n".join([
        "이게 뭐 알림: JYP Ent.(035900) 3분기 실적 알림의 마지막 확인입니다. 실패가 아니라 아직 공시가 없다는 정보입니다. 매매 신호가 아닙니다.",
        "지금 결과/상태:",
    ] + [f"- {x}" for x in lines] + [
        f"지금 할 일: 없음. 판단은 기존 {cfg['prior_view']} 유지.",
        "나중에: 이 알림은 여기서 끝납니다. 공시가 늦게 나오면 DART에서 직접 확인하세요.",
        "익절/줄일 때: 해당 없음(보유 여부 미확인).",
        "하지 말 것: 미발표를 호재나 악재로 해석. 투자 권유 아님.",
    ])
    return title, body, 3


def fail_once(ctx: Ctx, cfg: dict, state: dict, what: str) -> None:
    day = ctx.now.strftime("%Y-%m-%d")
    if state.get("fail_day") == day:
        print(f"fail already notified today: {what}")
        return
    state["fail_day"] = day
    send(ctx, cfg, "[JYP] 조회 실패 | 오늘 판정 보류", "\n".join([
        "이게 뭐 알림: JYP 3분기 실적 알림의 데이터 조회 실패 안내입니다. 매매 신호가 아닙니다.",
        f"지금 결과/상태: {what[:400]}",
        "지금 할 일: 없음. 조회 실패를 미발표나 호재로 읽지 마세요.",
        "나중에: 다음 예정 실행(17:05 또는 GitHub 예비 실행)에서 다시 확인합니다.",
        "익절/줄일 때: 해당 없음.",
        "하지 말 것: 이 안내로 매매 판단. 투자 권유 아님.",
    ]), 3, ops=True)
    save(ctx, state)


# ---------- 실행 ----------

def guard(cfg: dict, now: datetime) -> str | None:
    if now.year != int(cfg.get("year_guard") or 2026):
        return "year-guard"
    end = cfg.get("ends_kst")
    if end and now > at_kst(end):
        return "ended"
    return None


def run_auto(ctx: Ctx, cfg: dict, state: dict) -> None:
    now = ctx.now
    first = min(at_kst(x) for x in cfg["oneshot_kst"])
    final = now >= at_kst(cfg["final_kst"])
    if now < first - timedelta(minutes=10):
        print(f"{now:%Y-%m-%d %H:%M} before window ({first:%m/%d %H:%M}); silent")
        return
    if state.get("sent"):
        print(f"already sent {state['sent'].get('at')} ({state['sent'].get('title')}); silent")
        return
    if final and state.get("final_sent"):
        print(f"final notice already sent {state['final_sent'].get('at')}; silent")
        return
    try:
        res, ok, notes = find_q3(ctx, cfg)
    except Exception as exc:
        print(f"fetch failed: {exc}")
        fail_once(ctx, cfg, state, f"DART와 KRX 공시 피드 모두 조회 실패: {exc}")
        return
    for n in notes:
        print("note:", n)
    if res and res.get("pending"):
        if not final:
            print(f"공시 접수({res.get('day')}, {res.get('src')}) 숫자 반영 전; 다음 실행에서 다시 확인. silent")
            return
        title, body, pri = pending_message(ctx, cfg, res)
    elif res:
        title, body, pri = result_message(ctx, cfg, state, res)
    elif final:
        title, body, pri = final_missing_message(ctx, cfg, state, notes)
    else:
        print(f"{now:%Y-%m-%d %H:%M} KST 3분기 실적 공시 없음 (확인: {', '.join(ok)}); silent, state 그대로")
        return
    send(ctx, cfg, title, body, pri)
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    if res:
        state["sent"] = {"at": stamp, "title": title, "day": res.get("day"), "rcp": res.get("rcp"), "op": res.get("op")}
    else:
        state["final_sent"] = {"at": stamp, "title": title}
    state["last_title"] = title
    save(ctx, state)


def run_snap(ctx: Ctx, cfg: dict, state: dict) -> None:
    if state.get("sent") or state.get("final_sent"):
        print("result already handled; snap no-op")
        return
    cons, why = consensus_2027(ctx, cfg)
    if not cons:
        print(f"2027 consensus unavailable ({why}); silent, state 그대로")
        return
    snap = {"eps": cons.get("eps"), "op": cons.get("op"), "rev": cons.get("rev"), "src": cons.get("src"),
            "as_of": ctx.now.strftime("%Y-%m-%d")}
    cs = state.setdefault("cons", {})
    changed = False
    if not cs.get("baseline"):
        cs["baseline"] = snap
        changed = True
    last = cs.get("last")
    if not last or any(last.get(k) != snap.get(k) for k in ("eps", "op")):
        cs["last"] = snap
        changed = True
    print(f"2027 consensus {snap}; changed={changed}")
    if changed:
        save(ctx, state)


def selfcheck(cfg: dict) -> None:
    sc = cfg["earnings"]["selfcheck"]
    print("--- selfcheck: 2분기 잠정실적 공시 파싱 ---")
    try:
        got = naver_filing(cfg, sc["period_start"], sc["period_end"], "2026-08-01", 60)
        if not got:
            print("2분기 공시 못 찾음")
        else:
            show = {k: v for k, v in got.items() if k not in ("text_hit", "remarks")}
            print("parsed:", show)
            ok = round(got.get("op", 0)) == sc["expect_op_eok"] and round(got.get("rev", 0)) == sc["expect_rev_eok"]
            print(f"expect op {sc['expect_op_eok']}억 rev {sc['expect_rev_eok']}억 -> {'OK' if ok else 'MISMATCH'}")
            print("3분기 기간으로 같은 본문 파싱(None 이어야 함):",
                  parse_filing(got["text_hit"], cfg["earnings"]["period_start"], cfg["earnings"]["period_end"]))
    except Exception as exc:
        print("selfcheck 실패:", exc)


def main() -> int:
    args = [a.lower() for a in sys.argv[1:]]
    mode = args[0] if args else "auto"
    if mode not in ("auto", "snap", "dry", "sim"):
        print(f"unknown mode {mode}")
        return 2
    cfg = load_json(CFG_PATH)
    now = datetime.now(KST)
    if os.environ.get("JYP_NOW") and mode in ("dry", "sim"):
        now = at_kst(os.environ["JYP_NOW"])
    inject, spath = None, None
    if mode == "sim":
        if not os.environ.get("JYP_INJECT") or not os.environ.get("JYP_STATE"):
            print("sim needs JYP_INJECT and JYP_STATE")
            return 2
        spath = Path(os.environ["JYP_STATE"]).resolve()
        if spath == STATE_PATH.resolve() or ROOT in spath.parents:
            print("sim refuses to write inside the repo")
            return 2
        inject = load_json(Path(os.environ["JYP_INJECT"]))
    ctx = Ctx(mode, now, inject, spath)
    state = load_json(spath) if spath else load_json(STATE_PATH)
    print(f"{now.isoformat()} mode={mode}")
    why = guard(cfg, now)
    if why and mode not in ("dry",):
        print(f"jyp silent: {why}")
        return 0
    if why:
        print(f"(dry) guard would stop: {why}; continuing for preview")
    if mode == "dry":
        selfcheck(cfg)
        print("--- 2027 consensus (dry) ---")
        print(consensus_2027(ctx, cfg))
        print("--- tour (dry) ---")
        print(tour_line(ctx, cfg))
        print("--- auto preview (dry, window guard ignored) ---")
        st = json.loads(json.dumps(state))
        try:
            res, ok, notes = find_q3(ctx, cfg)
            print("sources ok:", ok, "notes:", notes)
            if res:
                show = {k: v for k, v in res.items() if k not in ("text_hit", "remarks")}
                print("found:", show)
                if not res.get("pending"):
                    t, b, p = result_message(ctx, cfg, st, res)
                    send(ctx, cfg, t, b, p)
            else:
                print("3분기 실적 공시 없음 -> 17:05 실행이면 침묵")
                if now >= at_kst(cfg["final_kst"]):
                    t, b, p = final_missing_message(ctx, cfg, st, notes)
                    send(ctx, cfg, t, b, p)
        except Exception as exc:
            print(f"fetch failed: {exc} -> ops 조회 실패 안내 대상")
        print("DRY RUN: state not written")
        return 0
    if mode == "snap":
        try:
            run_snap(ctx, cfg, state)
        except Exception as exc:  # morning 슬롯을 깨지 않는다
            print(f"snap skipped: {exc}")
        return 0
    run_auto(ctx, cfg, state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
