#!/usr/bin/env python3
"""[SEC-DIV] 삼성전자(005930) 3분기 배당 결정 공시 1회 알림 (approved in chat 2026-10-26).

Runs on the existing morning 09:00 / afternoon 15:20 / k203780 19:00 slots. No new cron.

  auto     KIND 회사별 공시 목록(키 없음)에서 2026-10-01 이후 '현금, 현물배당 결정' 을 찾고 원문을 읽는다.
           배당기준일이 2026-09-30 인 첫 원 공시면 1번 보낸다(state fired). 그 뒤 '[기재정정]' 등 정정 공시가
           새로 나오면 바뀐 점을 1번 더 보낸다(followup_sent). 11/13 19:00 이후 첫 실행까지 못 찾으면
           '못 찾음' 1번(final_done). KIND 가 안 되면 네이버 KRX 공시 피드로 대신 본다. 둘 다 실패하면
           cwally-ops 에 1번만 알린다(다음 성공에서 재무장).
  dry      실데이터로 판정만 출력(ntfy 없음, state 쓰기 없음). 아직 3분기 공시가 없으면 2분기 공시(7/30)로
           파서와 DART 링크 찾기를 점검해 그 메시지를 그대로 찍는다.
  selftest 네트워크 없이 파서, 분류, 문구 점검.
  install  설치 확인 1회 발송. state 를 건드리지 않는다.
매매 신호가 아니다. 투자 권유 아님.
"""
from __future__ import annotations

import json
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from html import unescape
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
CFG_PATH = ROOT / "configs" / "secdiv.json"
STATE_PATH = ROOT / "state" / "secdiv.json"
UA = "Mozilla/5.0 (cwally-alerts; +mailto:sosmos3218@gmail.com)"
KIND = "https://kind.krx.co.kr"
KIND_VIEW = KIND + "/common/disclsviewer.do?method=search&acptno={acpt}"
DART_SEARCH = "https://dart.fss.or.kr/dsab007/detailSearch.ax"
DART_VIEW = "https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcp}"
DIV_TITLE_RE = re.compile(r"현금\s*[ㆍ·,]?\s*현물\s*배당\s*결정")


def now_kst() -> datetime:
    return datetime.now(KST)


def load_cfg() -> dict:
    return json.loads(CFG_PATH.read_text(encoding="utf-8"))


def load_state() -> dict:
    try:
        v = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        return v if isinstance(v, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(st: dict, dry: bool) -> None:
    if dry:
        print("DRY RUN: state not written")
        return
    st["updated_at"] = now_kst().strftime("%Y-%m-%d %H:%M KST")
    STATE_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def text_of(html: str) -> str:
    t = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html or "")
    t = re.sub(r"<[^>]+>", " ", t)
    return re.sub(r"\s+", " ", unescape(t)).strip()


def clean(text: str) -> str:
    """Korean text rule: no middle dot / em-dash in ntfy text."""
    t = re.sub(r"\s*[ㆍ·•‧∙]\s*", ", ", text)
    return re.sub(r"\s*[—―]\s*", " ", t)


def http(url: str, data: dict | None = None, headers: dict | None = None, tries: int = 3, timeout: int = 25) -> str:
    h = {"User-Agent": UA}
    h.update(headers or {})
    body = urllib.parse.urlencode(data).encode() if data else None
    if body:
        h["Content-Type"] = "application/x-www-form-urlencoded"
    err: Exception | None = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, data=body, headers=h, method="POST" if body else "GET")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                ctype = resp.headers.get("Content-Type", "")
            return raw.decode("euc-kr" if "euc-kr" in ctype.lower() else "utf-8", errors="ignore")
        except Exception as exc:
            err = exc
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"{url.split('?')[0]}: {err}")


# ---------- sources ----------

def kind_list(cfg: dict, since: str, until: str) -> list[dict]:
    form = {
        "method": "searchDetailsSub", "currentPageSize": "100", "pageIndex": "1", "orderMode": "1", "orderStat": "D",
        "forward": "details_sub", "searchCodeType": "", "repIsuSrtCd": "A" + cfg["code"], "allRepIsuSrtCd": "",
        "oldSearchCorpName": "", "disclosureType": "", "disTypevalue": "", "reportNm": "배당", "reportCd": "",
        "searchCorpName": cfg["code"], "business": "", "marketType": "", "settlementMonth": "", "securities": "",
        "submitOblgNm": "", "enterprise": "", "fromDate": since, "toDate": until, "reportNmTemp": "", "bfrDsclsType": "on",
    }
    page = http(KIND + "/disclosure/details.do", data=form,
                headers={"X-Requested-With": "XMLHttpRequest", "Referer": KIND + "/disclosure/details.do?method=searchDetailsMain"})
    out, rows = [], 0
    for row in re.findall(r"(?s)<tr[^>]*>(.*?)</tr>", page):
        acpt = re.search(r"openDisclsViewer\('(\d{14})'", row)
        if not acpt:
            continue
        rows += 1
        t = text_of(row)
        if cfg["corp_name"] not in t or not DIV_TITLE_RE.search(t):
            continue
        dm = re.search(r"(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})", t)
        title = t[dm.end():].strip() if dm else t
        title = re.sub(r"\s*(공시차트|주가차트).*$", "", title).strip()
        out.append({"id": "kind:" + acpt.group(1), "acptno": acpt.group(1), "title": title,
                    "filed": f"{dm.group(1)} {dm.group(2)}" if dm else "", "src": "KIND"})
    if rows == 0 and "조회된 결과값이 없습니다" not in page and "<table" not in page:
        raise RuntimeError("KIND 응답 형식 이상")
    return sorted(out, key=lambda r: r["acptno"])


def kind_body(acpt: str) -> str:
    viewer = http(KIND_VIEW.format(acpt=acpt))
    m = re.search(r"<option value='(\d+)\|Y'", viewer) or re.search(r"<option value='(\d+)\|", viewer)
    if not m:
        raise RuntimeError("KIND 본문 문서번호 없음")
    sc = http(f"{KIND}/common/disclsviewer.do?method=searchContents&docNo={m.group(1)}")
    p = re.search(r"setPath\('[^']*','([^']+\.htm)'", sc)
    if not p:
        raise RuntimeError("KIND 본문 경로 없음")
    return text_of(http(p.group(1), timeout=60))


def naver_list(cfg: dict, since: str, until: str) -> list[dict]:
    raw = json.loads(http(f"https://stock.naver.com/api/domestic/detail/notice?itemCode={cfg['code']}&page=1&pageSize=100"))
    out = []
    for r in raw if isinstance(raw, list) else []:
        title = str(r.get("title") or "").strip()
        day = str(r.get("datetime") or "")[:10]
        if not DIV_TITLE_RE.search(title) or not (since <= day <= until):
            continue
        out.append({"id": "naver:" + str(r.get("no") or r.get("bizdateSeq")), "acptno": None, "title": title,
                    "filed": day, "src": "네이버 KRX 공시 피드", "body": text_of(str(r.get("contents") or ""))})
    return sorted(out, key=lambda r: r["id"])


def dart_link(cfg: dict, filed_day: str) -> str | None:
    """DART 공시검색(키 없음)에서 같은 날 '배당 결정' rcpNo. 못 찾으면 None."""
    d = filed_day.replace("-", "")
    try:
        page = http(DART_SEARCH, data={"currentPage": "1", "maxResults": "30", "textCrpNm": cfg["corp_name"],
                                       "textCrpCik": cfg["dart_corp_code"], "startDate": d, "endDate": d}, tries=2, timeout=20)
    except Exception as exc:
        print(f"DART search failed: {exc}")
        return None
    hits = []
    for row in re.findall(r"(?s)<tr[^>]*>(.*?)</tr>", page):
        rcp = re.search(r"rcpNo=(\d{14})", row)
        t = text_of(row)
        if rcp and DIV_TITLE_RE.search(t) and cfg["corp_name"] in t:
            hits.append(rcp.group(1))
    return DART_VIEW.format(rcp=max(hits)) if hits else None


def last_close(code: str) -> tuple[float | None, str | None]:
    try:
        raw = json.loads(http(f"https://m.stock.naver.com/api/stock/{code}/price?pageSize=2&page=1", tries=2))
        r = raw[0]
        return float(str(r["closePrice"]).replace(",", "")), str(r["localTradedAt"])
    except Exception as exc:
        print(f"price fetch failed {code}: {exc}")
        return None, None


# ---------- parse (pure) ----------

def _n(tok: str | None) -> float | None:
    try:
        return float(str(tok).replace(",", ""))
    except (TypeError, ValueError):
        return None


def parse_div(text: str) -> dict | None:
    t = re.sub(r"\s+", " ", text or "")
    if "배당구분" not in t or "1주당 배당금" not in t:
        return None
    g = lambda pat: (re.search(pat, t) or [None, None])[1]  # noqa: E731
    out = {
        "kind": g(r"배당구분\s*(\S+)"),
        "common": _n(g(r"1주당 배당금\s*\(원\)\s*보통주식\s*([\d,]+)")),
        "pref": _n(g(r"1주당 배당금\s*\(원\)\s*보통주식\s*[\d,]+\s*종류주식\s*([\d,]+)")),
        "yield_common": _n(g(r"시가배당률\s*\(%\)\s*보통주식\s*([\d.]+)")),
        "yield_pref": _n(g(r"시가배당률\s*\(%\)\s*보통주식\s*[\d.]+\s*종류주식\s*([\d.]+)")),
        "total": _n(g(r"배당금총액\s*\(원\)\s*([\d,]+)")),
        "record": g(r"배당기준일\s*(\d{4}-\d{2}-\d{2})"),
        "pay": g(r"배당금지급\s*예정일자\s*(\d{4}-\d{2}-\d{2})"),
        "board": g(r"이사회결의일\s*\(결정일\)\s*(\d{4}-\d{2}-\d{2})"),
    }
    return out if out["common"] is not None and out["record"] else None


def is_correction(title: str) -> bool:
    return bool(re.search(r"정정", title or ""))


def band(cfg: dict, dps: float) -> tuple[str, str]:
    lo, hi = cfg["estimate"]["low"], cfg["estimate"]["high"]
    if dps < lo:
        return "하회", f"추정 하단 {lo:,}원보다 {dps - lo:+,.0f}원({(dps / lo - 1) * 100:+.1f}%)"
    if dps > hi:
        return "상회", f"추정 상단 {hi:,}원보다 {dps - hi:+,.0f}원({(dps / hi - 1) * 100:+.1f}%)"
    return "범위 안", f"하단보다 {dps - lo:+,.0f}원, 상단보다 {dps - hi:+,.0f}원"


def won(v: float | None) -> str:
    return "?" if v is None else f"{v:,.0f}원"


def jo(v: float | None) -> str:
    return "?" if v is None else f"{v / 1e12:,.2f}조원"


def head(now: datetime, what: str) -> str:
    return f"{now:%Y-%m-%d %H:%M} KST | 005930 | 삼성전자 {what}"


def result_message(cfg: dict, d: dict, rep: dict, link: str, px: tuple[float | None, str | None], now: datetime,
                   label: str = "3분기") -> tuple[str, str, int]:
    est = cfg["estimate"]
    b, btxt = band(cfg, d["common"])
    close, cday = px
    yld = f"최근 종가 {won(close)}({cday}) 대비 {label} 배당수익률 {d['common'] / close * 100:.2f}%" if close else "최근 종가 조회 실패로 수익률 계산 못 함"
    paid = sum(x["dps"] for x in est["paid_2026"]) + d["common"]
    rest = est["full_year_2026"] - paid
    rest_txt = (f"{est['full_year_source']} {est['full_year_2026']:,}원에서 1~3분기({' + '.join(str(x['dps']) for x in est['paid_2026'])} + {d['common']:,.0f}원)를 빼면 "
                f"남은 몫은 약 {rest:,.0f}원" + (f"(최근 종가 대비 {rest / close * 100:.1f}%)" if close else "") + "이지만 추정치이고 확정이 아닙니다.")
    corr = " 정정 공시가 첫 공시로 잡혔습니다." if is_correction(rep["title"]) else ""
    title = f"[SEC-DIV] {label} 배당 주당 {d['common']:,.0f}원 확정 (추정 {b}) | 지금 사면 이번 배당 없음"
    lines = [
        head(now, f"{label} 배당 결정"),
        f"이게 뭐 알림: {cfg['display']}입니다. 공시가 나와서 보냅니다. 매매 신호 아님, 투자 권유 아님.",
        f"지금 결과/상태: 보통주 주당 {won(d['common'])}, 우선주 주당 {won(d['pref'])}, 배당금총액 {jo(d['total'])}(보통주와 우선주 합계). "
        f"배당기준일 {d['record']}, 지급 예정일 {d.get('pay') or '?'}, 이사회 {d.get('board') or '?'}. "
        f"{est['source']} {est['low']:,}~{est['high']:,}원 대비 {b}({btxt}). {yld}"
        + (f" (공시 시가배당률 보통주 {d['yield_common']:g}%, 우선주 {d['yield_pref']:g}%)." if d.get("yield_common") is not None else ".") + corr,
        f"지금 할 일: 배당락일 {cfg['ex_date']}이 지나서 지금 사도 이번 {label} 배당은 받지 못합니다. 9/29 전에 갖고 있던 주식이면 지급일에 입금만 확인하세요.",
        f"나중에: 지금 사는 사람에게 남은 것은 2027년 1월 이사회가 정할 2026년 잔여 주주환원(4분기 배당 등)입니다. {rest_txt} 2027년 이후 배당 정책은 아직 발표되지 않았습니다.",
        "익절/줄일 때: 이 알림만으로 정하지 않습니다. 보유분은 [SEC], [005930] 알림 기준을 따르세요.",
        f"하지 말 것: 배당 숫자만 보고 추격매수(이번 배당은 이미 지난 기준일 몫), 신용/미수, 이 알림만 보고 주문. 투자 권유 아님.",
        f"공시: {link}",
    ]
    pri = 4 if b != "범위 안" else 3
    return clean(title), clean("\n".join(lines)), pri


FIELDS = (("common", "보통주 주당", won), ("pref", "우선주 주당", won), ("total", "배당금총액", jo),
          ("record", "배당기준일", str), ("pay", "지급 예정일", str), ("board", "이사회", str))


def followup_message(cfg: dict, old: dict, new: dict, rep: dict, link: str, now: datetime) -> tuple[str, str, int]:
    diffs = [f"{lab} {fmt(old.get(k))} -> {fmt(new.get(k))}" for k, lab, fmt in FIELDS if old.get(k) != new.get(k)]
    what = ", ".join(diffs) if diffs else "숫자와 날짜 변화 없음(문구 정정으로 보임)"
    short = diffs[0] if diffs else "숫자 변화 없음"
    title = f"[SEC-DIV] 3분기 배당 정정 공시 | {short}"
    lines = [
        head(now, "3분기 배당 정정"),
        "이게 뭐 알림: 앞서 보낸 삼성전자 3분기 배당 결정 공시에 정정 공시가 나와서 1번 더 보냅니다. 매매 신호 아님.",
        f"지금 결과/상태: {what}. 정정 공시 접수 {rep.get('filed') or '?'}.",
        "지금 할 일: 바뀐 숫자만 확인하세요. 배당락일 9/29이 지나서 지금 사도 이번 배당은 받지 못하는 점은 같습니다.",
        "나중에: 이 알림은 여기서 끝납니다(정정 안내는 1번만). 남은 환원은 2027년 1월 이사회에서 정합니다.",
        "익절/줄일 때: 이 알림만으로 정하지 않습니다.",
        "하지 말 것: 정정 공시를 매매 신호로 읽기. 투자 권유 아님.",
        f"공시: {link}",
    ]
    return clean(title), clean("\n".join(lines)), 3


def missing_message(cfg: dict, now: datetime) -> tuple[str, str, int]:
    title = "[SEC-DIV] 11/13까지 3분기 배당 공시 못 찾음 | 확인만"
    lines = [
        head(now, "3분기 배당 공시 마지막 확인"),
        "이게 뭐 알림: 삼성전자 3분기 배당 결정 공시 알림의 마지막 확인입니다. 조회 실패가 아니라 기한까지 공시를 못 찾았다는 정보입니다. 매매 신호 아님.",
        f"지금 결과/상태: {cfg['since']} 이후 KIND 공시 목록에 배당기준일 {cfg['record_date']} 배당 결정 공시가 없습니다.",
        "지금 할 일: DART에서 삼성전자 공시를 직접 한 번 확인하세요.",
        "나중에: 이 알림은 여기서 끝납니다.",
        "익절/줄일 때: 해당 없음.",
        "하지 말 것: 공시 지연을 호재나 악재로 해석. 투자 권유 아님.",
    ]
    return title, clean("\n".join(lines)), 3


def install_message(cfg: dict, now: datetime) -> tuple[str, str]:
    lines = [
        head(now, "설치 확인"),
        f"이게 뭐 알림: {cfg['display']}입니다. 설치 확인용 테스트 발송이며 매매 신호가 아닙니다.",
        f"지금 결과/상태: 평일 09:00, 15:20, 19:00 KST 에 KIND 공시 목록을 봅니다. 배당기준일 {cfg['record_date']} 배당 결정 공시가 나오면 보통주와 우선주 주당 배당금, 총액, 기준일과 지급일, 대신증권 추정 4,500~4,654원 비교, 최근 종가 대비 수익률, DART 링크를 1번 보냅니다.",
        "지금 할 일: 알림이 도착했는지 확인만 하세요.",
        "나중에: 정정 공시가 나오면 바뀐 점을 1번 더 보냅니다. 11/13 19:00 까지 못 찾으면 '못 찾음' 1번 보내고 끝납니다.",
        "익절/줄일 때: 해당 없음.",
        "하지 말 것: 이 메시지를 매매 신호로 해석하기.",
    ]
    return "[SEC-DIV] 설치 확인 | 매매 신호 아님", clean("\n".join(lines))


# ---------- run ----------

def fetch_filings(cfg: dict, since: str, until: str) -> tuple[list[dict], list[str]]:
    """[{id, title, filed, src, parsed}] using KIND, falling back to the Naver KRX feed."""
    notes: list[str] = []
    try:
        reps = kind_list(cfg, since, until)
        for r in reps:
            try:
                r["body"] = kind_body(r["acptno"])
            except Exception as exc:
                notes.append(f"KIND 본문 {r['acptno']}: {exc}")
                r["body"] = ""
        if all(r["body"] for r in reps):
            return reps, notes
        # some bodies failed: fill from the Naver feed by same day + correction flag
        nav = naver_list(cfg, since, until)
        for r in reps:
            if not r["body"]:
                m = [n for n in nav if n["filed"] == r["filed"][:10] and is_correction(n["title"]) == is_correction(r["title"])]
                r["body"] = m[-1]["body"] if m else ""
        return reps, notes
    except Exception as exc:
        notes.append(f"KIND 목록: {exc}")
    return naver_list(cfg, since, until), notes


def run(mode: str) -> int:
    cfg = load_cfg()
    dry = mode == "dry"
    now = now_kst()
    print(f"{now:%Y-%m-%d %H:%M} KST secdiv mode={mode}")
    if dry:
        print("DRY RUN (no ntfy, no state write)")
    if mode == "install":
        t, b = install_message(cfg, now)
        print(f"{t}\n{b}")
        publish(resolve_topic("port"), t, b, 3, ["white_check_mark"], config_name="secdiv")
        print("install confirmation sent (state untouched)")
        return 0
    st = load_state()
    if now.year != int(cfg["year_guard"]) or now > datetime.strptime(cfg["ends_kst"], "%Y-%m-%d %H:%M").replace(tzinfo=KST):
        print("outside year guard / after ends_kst; silent")
        return 0
    if st.get("final_done") or st.get("followup_sent"):
        print("finished (final notice or follow-up already sent); silent")
        return 0
    try:
        reps, notes = fetch_filings(cfg, cfg["since"], now.strftime("%Y-%m-%d"))
    except Exception as exc:
        print(f"all sources failed: {exc}")
        if not st.get("fail_notified"):
            t = "[SEC-DIV] 공시 조회 실패 | 매매 신호 아님"
            b = f"삼성전자 3분기 배당 공시 확인 중 KIND, 네이버 공시 피드 조회 실패: {exc}. 다음 슬롯에서 다시 봅니다."
            if dry:
                print(f"DRY RUN would send (ops): {t}\n{b}")
            else:
                publish(resolve_topic("ops"), t, b, 3, ["warning"], config_name="secdiv")
                st["fail_notified"] = True
                save_state(st, dry)
        return 0
    for n in notes:
        print("note:", n)
    st["fail_notified"] = False
    q3 = []
    for r in reps:
        p = parse_div(r.get("body") or "")
        print(f"filing {r['id']} {r['filed']} {r['title']!r} parsed={p}")
        if p and p["record"] == cfg["record_date"]:
            q3.append((r, p))
    seen = set(st.get("seen") or [])
    fresh = [(r, p) for r, p in q3 if r["id"] not in seen]
    sent_any = False
    if not st.get("fired") and fresh:
        orig = [x for x in fresh if not is_correction(x[0]["title"])] or fresh
        r, p = orig[0]
        link = dart_link(cfg, r["filed"][:10]) or (KIND_VIEW.format(acpt=r["acptno"]) + " (KIND 원문, DART 링크 못 찾음)" if r.get("acptno") else "https://dart.fss.or.kr (DART 에서 삼성전자 검색, 링크 못 찾음)")
        t, b, pri = result_message(cfg, p, r, link, last_close(cfg["code"]), now)
        if dry:
            print(f"DRY RUN would send (p{pri}): {t}\n{b}")
        else:
            publish(resolve_topic("port"), t, b, pri, ["moneybag"], config_name="secdiv")
        st["fired"] = {"id": r["id"], "filed": r["filed"], "title": r["title"], "link": link, **p}
        seen |= {x[0]["id"] for x in fresh if not is_correction(x[0]["title"])} | {r["id"]}
        sent_any = True
    elif st.get("fired"):
        corr = [x for x in fresh if is_correction(x[0]["title"])]
        if corr:
            r, p = corr[-1]
            link = dart_link(cfg, r["filed"][:10]) or (KIND_VIEW.format(acpt=r["acptno"]) if r.get("acptno") else st["fired"].get("link", ""))
            t, b, pri = followup_message(cfg, st["fired"], p, r, link, now)
            if dry:
                print(f"DRY RUN would send (p{pri}): {t}\n{b}")
            else:
                publish(resolve_topic("port"), t, b, pri, ["pencil"], config_name="secdiv")
            st["followup_sent"] = True
            st["followup"] = {"id": r["id"], "filed": r["filed"], **p}
            sent_any = True
        seen |= {x[0]["id"] for x in fresh}
    st["seen"] = sorted(seen)
    if not st.get("fired") and not sent_any:
        final = datetime.strptime(cfg["final_kst"], "%Y-%m-%d %H:%M").replace(tzinfo=KST)
        if now >= final:
            t, b, pri = missing_message(cfg, now)
            if dry:
                print(f"DRY RUN would send (p{pri}): {t}\n{b}")
            else:
                publish(resolve_topic("port"), t, b, pri, ["hourglass"], config_name="secdiv")
            st["final_done"] = True
        else:
            print(f"no Q3 dividend filing yet (record date {cfg['record_date']}); silent until {cfg['final_kst']} KST final check")
            if dry:
                selfcheck_live(cfg, now)
    save_state(st, dry)
    return 0


def selfcheck_live(cfg: dict, now: datetime) -> None:
    """dry only: parse the Q2 (7/30) filing end to end and print the message it would make."""
    sc = cfg["selfcheck"]
    try:
        reps, notes = fetch_filings(cfg, sc["since"], sc["until"])
    except Exception as exc:
        print(f"SELFCHECK fetch failed: {exc}")
        return
    for n in notes:
        print("SELFCHECK note:", n)
    for r in reps:
        p = parse_div(r.get("body") or "")
        if p and p["record"] == sc["record_date"]:
            ok = (p["common"], p["pref"], p["total"], p["pay"]) == (sc["expect_common"], sc["expect_pref"], sc["expect_total"], sc["expect_pay"])
            link = dart_link(cfg, r["filed"][:10])
            print(f"SELFCHECK Q2 filing {r['id']} via {r['src']}: parsed={p} expected_match={ok} dart_link={link}")
            t, b, pri = result_message(cfg, p, r, link or KIND_VIEW.format(acpt=r.get("acptno")), last_close(cfg["code"]), now, label="(점검용 2분기)")
            print(f"SELFCHECK message (p{pri}, not sent):\n{t}\n{b}")
            return
    print("SELFCHECK: Q2 filing not found")


def selftest() -> int:
    cfg = load_cfg()
    fails = 0

    def check(label, got, want):
        nonlocal fails
        ok = got == want
        fails += not ok
        print(("ok  " if ok else "FAIL"), label, got, "" if ok else f"want {want}")

    q2 = ("현금ㆍ현물배당 결정 1. 배당구분 분기배당 2. 배당종류 현금배당 - 현물자산의 상세내역 - 3. 1주당 배당금(원) 보통주식 374 종류주식 374 "
          "- 차등배당 여부 미해당 4. 시가배당률(%) 보통주식 0.1 종류주식 0.2 5. 배당금총액(원) 2,455,894,601,644 6. 배당기준일 2026-06-30 "
          "7. 배당금지급 예정일자 2026-08-28 8. 주주총회 개최여부 미개최 9. 주주총회 예정일자 - 10. 이사회결의일(결정일) 2026-07-30")
    p = parse_div(q2)
    check("parse q2", (p["kind"], p["common"], p["pref"], p["yield_common"], p["yield_pref"], p["total"], p["record"], p["pay"], p["board"]),
          ("분기배당", 374.0, 374.0, 0.1, 0.2, 2455894601644.0, "2026-06-30", "2026-08-28", "2026-07-30"))
    check("parse garbage", parse_div("배당락 기준가격 안내"), None)
    check("title re KIND", bool(DIV_TITLE_RE.search("삼성전자 현금ㆍ현물 배당 결정")), True)
    check("title re no dot", bool(DIV_TITLE_RE.search("[기재정정]현금ㆍ현물배당결정")), True)
    check("title re 배당락 excluded", bool(DIV_TITLE_RE.search("중간(분기)배당락 기준가격 안내")), False)
    check("correction", (is_correction("[기재정정]현금ㆍ현물 배당 결정"), is_correction("현금ㆍ현물 배당 결정")), (True, False))
    check("band below", band(cfg, 4400)[0], "하회")
    check("band in low", band(cfg, 4500)[0], "범위 안")
    check("band in high", band(cfg, 4654)[0], "범위 안")
    check("band above", band(cfg, 4700)[0], "상회")
    q3 = dict(p, common=4600.0, pref=4600.0, total=30.9e12, record="2026-09-30", pay="2026-11-20", board="2026-10-29", yield_common=2.1, yield_pref=2.6)
    now = datetime(2026, 10, 29, 9, 5, tzinfo=KST)
    rep = {"id": "kind:20261029000100", "acptno": "20261029000100", "title": "현금ㆍ현물 배당 결정", "filed": "2026-10-29 08:50"}
    t, b, pri = result_message(cfg, q3, rep, "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20261029800100", (212000.0, "2026-10-28"), now)
    check("title", t, "[SEC-DIV] 3분기 배당 주당 4,600원 확정 (추정 범위 안) | 지금 사면 이번 배당 없음")
    check("yield 2.17%", "배당수익률 2.17%" in b, True)
    check("residual 7,668", "약 7,668원" in b and "3.6%" in b, True)
    check("ex-date note", "지금 사도 이번 3분기 배당은 받지 못합니다" in b, True)
    check("in-range priority 3", pri, 3)
    t2, b2, p2 = result_message(cfg, dict(q3, common=5000.0), rep, "x", (None, None), now)
    check("above priority 4", (p2, "상회" in t2), (4, True))
    tf, bf, _ = followup_message(cfg, q3, dict(q3, pay="2026-11-21"), {"filed": "2026-10-30 16:00"}, "x", now)
    check("followup diff", "지급 예정일 2026-11-20 -> 2026-11-21" in bf, True)
    tf2, bf2, _ = followup_message(cfg, q3, dict(q3), {"filed": "x"}, "x", now)
    check("followup no change", "숫자 변화 없음" in tf2, True)
    tm, bm, _ = missing_message(cfg, now)
    ti, bi = install_message(cfg, now)
    for lab, txt in (("result", t + b), ("above", t2 + b2), ("followup", tf + bf), ("missing", tm + bm), ("install", ti + bi)):
        check(f"no middle dot/em-dash ({lab})", any(ch in txt for ch in "\u00b7\u318d\u2014"), False)
    check("missing title not routed to ops", bool(re.search(r"조회\s?실패|수집\s?실패|파싱\s?실패|해석\s?불가|확인 불가|미게시", tm)), False)
    print(t)
    print(b)
    print("selftest", "PASS" if not fails else f"FAIL {fails}")
    return 1 if fails else 0


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    if mode == "selftest":
        return selftest()
    if mode not in {"auto", "dry", "install"}:
        print(f"unknown mode {mode}")
        return 2
    return run(mode)


if __name__ == "__main__":
    raise SystemExit(main())
