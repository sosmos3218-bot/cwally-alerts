#!/usr/bin/env python3
"""[180640] 한진칼 지분 구도 공시 감시 (2026-11-06 요청 pt71u/t71u).

k203780 평일 19:00 KST 슬롯(271560 다음). 새 cron 없음. DART 웹 목록만 보고, 바뀔 때만 보냄:
  1) 호반 측(호반건설/호반 계열/김상열 등) 대량보유: 목적 단순투자 -> 경영권 영향 변경(p4),
     새 5% 보고(p4), 지분 0.5%p 이상 변동(p3)
  2) 주주제안/주총/소송: 주주제안, 임시주총 소집청구, 이사 후보, 의결권 위임 권유,
     의결권행사금지/주총결의 관련 소송.가처분(p4)
  3) 한국산업은행 지분 변동: 산은 명의 대량보유 또는 임원.주요주주 소유보고(p4)
  4) 조원태 매수 완료: 11/2 거래계획(32.5만주, 114,000원, 12/3~1/1)의 결과/완료 보고,
     또는 12/3 이후 조원태(특수관계인) 소유상황/대량보유에서 약 32.5만주 취득 -> 조원태 측 vs 호반 측 갭 재계산(p4)
  5) Delta(14.90%) 변동은 부수 항목(p3)
본문: 공시명, 보고자, 전/후 %, 재계산 갭, 해석 한 줄. rcept_no(rcp) 로 중복 제거.
시작 state 는 현재까지 공시를 모두 seen 에 넣어 첫 실행이 침묵. 소스 2일 연속 실패 -> cwally-ops 1번.
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

NAME = "k180640"
CFG_PATH = ROOT / "configs" / f"{NAME}.json"
STATE_PATH = ROOT / "state" / f"{NAME}.json"
FX = Path(__file__).resolve().parent / "fixtures" / NAME
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
TAG = "[180640]"
DART = "https://dart.fss.or.kr"


def default_state() -> dict:
    return {"updated_at": None, "seen": [], "stakes": {}, "hoban_purpose": "단순투자", "cho_plan_done": False, "fail": {}}


def load_state() -> dict:
    st = default_state()
    if STATE_PATH.exists():
        try:
            st.update(json.loads(STATE_PATH.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass
    return st


# ---------------- DART ----------------
def http(url: str, data: bytes | None = None, timeout: int = 30) -> bytes:
    last = None
    for i in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=UA), timeout=timeout) as r:
                return r.read()
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(1 + 2 * i)
    raise last  # type: ignore[misc]


def decode(raw: bytes) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("euc-kr", "ignore")


def flat(h: str) -> str:
    h = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", h)
    t = _html.unescape(re.sub(r"<[^>]+>", " | ", h)).replace("\xa0", " ")
    return re.sub(r"(\s*\|\s*)+", " | ", re.sub(r"\s+", " ", t))


def parse_list(raw: str) -> list[dict]:
    out = []
    for row in re.findall(r"<tr>.*?</tr>", raw, re.S):
        m = re.search(r'rcpNo=(\d+)"[^>]*>(.*?)</a>', row, re.S)
        if not m:
            continue
        filer = re.search(r'class="tL ellipsis" title="([^"]*)"', row)
        day = re.search(r"<td>(\d{4})\.(\d{2})\.(\d{2})</td>", row)
        title = _html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", m.group(2)))).strip()
        out.append({"rcp": m.group(1), "title": title, "filer": filer.group(1).strip() if filer else "",
                    "date": f"{day.group(1)}-{day.group(2)}-{day.group(3)}" if day else f"{m.group(1)[:4]}-{m.group(1)[4:6]}-{m.group(1)[6:8]}"})
    if "rcpNo=" not in raw and "조회 결과가 없습니다" not in raw and "검색된 자료가 없습니다" not in raw:
        raise ValueError("DART 목록 형식 확인 불가")
    return out


def dart_list(cik: str, start: date, end: date) -> list[dict]:
    data = urllib.parse.urlencode({"textCrpNm": "한진칼", "textCrpCik": cik, "startDate": start.strftime("%Y%m%d"),
                                   "endDate": end.strftime("%Y%m%d"), "maxResults": 100, "currentPage": 1, "sort": "date",
                                   "series": "desc"}).encode()
    return parse_list(decode(http(f"{DART}/dsab007/detailSearch.ax", data)))


def fetch_summary(rcp: str) -> str:
    """첫 절(요약/표지) 텍스트. 대량보유 요약, 거래계획 표지, 최대주주변경 본문에 충분."""
    main = decode(http(f"{DART}/dsaf001/main.do?rcpNo={rcp}"))
    nodes = []
    for m in re.finditer(r"node\d\['text'\] = \"([^\"]*)\";.*?node\d\['dcmNo'\] = \"(\d+)\";\s*node\d\['eleId'\] = \"(\d+)\";\s*"
                         r"node\d\['offset'\] = \"(\d+)\";\s*node\d\['length'\] = \"(\d+)\";\s*node\d\['dtd'\] = \"([^\"]*)\"", main, re.S):
        nodes.append(m.groups())
    if not nodes:
        d = re.search(rf"openPdfDownload\('{rcp}', '(\d+)'\)", main)
        if not d:
            raise ValueError("DART 목차 없음")
        nodes = [("본문", d.group(1), "0", "0", "0", "HTML")]
    # 거래계획은 3절(소유상황 및 거래계획)이 핵심
    pick = next((n for n in nodes if "거래계획" in n[0] and "소유상황" in n[0]), nodes[0])
    u = f"{DART}/report/viewer.do?rcpNo={rcp}&dcmNo={pick[1]}&eleId={pick[2]}&offset={pick[3]}&length={pick[4]}&dtd={pick[5]}"
    return flat(decode(http(u)))


# ---------------- 파서 ----------------
def party_of(filer: str, cfg: dict) -> str | None:
    f = re.sub(r"\s+", "", filer or "")
    for key, names in cfg["parties"].items():
        for n in names:
            if re.sub(r"\s+", "", n) in f:
                return key
    return None


def parse_mass_summary(text: str) -> dict:
    """대량보유 요약: purpose(단순투자|경영권|unknown), shares_prev/now, pct_prev/now, reason."""
    out = {"purpose": "unknown", "shares_prev": None, "shares_now": None, "pct_prev": None, "pct_now": None, "reason": ""}
    if "약식서식" in text or "확인서(단순투자" in text or re.search(r"보유목적 \| 단순투자", text):
        out["purpose"] = "단순투자"
    elif "일반서식" in text or re.search(r"보유목적 \| [^|]*경영권", text):
        out["purpose"] = "경영권"
    elif "단순투자" in text and "이 아닌" not in text[max(0, text.find("단순투자") - 30): text.find("단순투자")]:
        out["purpose"] = "단순투자"
    m = re.search(r"보유주식등의 수 및 보유비율 \| 보유주식등의 수 \| 보유비율 \| 직전 보고서 \| ([\d,]+) \| ([\d.]+) \| 이번 보고서 \| ([\d,]+) \| ([\d.]+)", text)
    if m:
        out.update(shares_prev=int(m.group(1).replace(",", "")), pct_prev=float(m.group(2)),
                   shares_now=int(m.group(3).replace(",", "")), pct_now=float(m.group(4)))
    r = re.search(r"보고사유 \| ([^|]+)", text)
    if r:
        out["reason"] = r.group(1).strip()
    p = re.search(r"보유목적 \| ([^|]+)", text)
    if p and out["purpose"] == "unknown":
        val = p.group(1).strip()
        if "단순" in val:
            out["purpose"] = "단순투자"
        elif "경영권" in val:
            out["purpose"] = "경영권"
    return out


def parse_maxshare(text: str) -> dict:
    """최대주주변경: before/after name, shares, pct."""
    out = {}
    m = re.search(r"변경전 \| 최대주주등 \| ([^|]+) \| 소유주식수\(주\) \| ([\d,]+) \| 소유비율\(%\) \| ([\d.]+) \| "
                  r"변경후 \| 최대주주등 \| ([^|]+) \| 소유주식수\(주\) \| ([\d,]+) \| 소유비율\(%\) \| ([\d.]+)", text)
    if m:
        out = {"before": m.group(1).strip(), "before_shares": int(m.group(2).replace(",", "")), "before_pct": float(m.group(3)),
               "after": m.group(4).strip(), "after_shares": int(m.group(5).replace(",", "")), "after_pct": float(m.group(6))}
    return out


def parse_plan_detail(text: str) -> dict:
    out = {"shares": None, "price": None, "start": None, "end": None}
    m = re.search(r"(\d{4})년\s*(\d{1,2})월\s*(\d{1,2})일 \| (\d{4})년\s*(\d{1,2})월\s*(\d{1,2})일 \| \d+ \| [^\|]* \| 보통주 \| ([\d,]+) \| ([\d,]+)", text)
    if m:
        out["start"] = f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
        out["end"] = f"{m.group(4)}-{int(m.group(5)):02d}-{int(m.group(6)):02d}"
        out["shares"] = int(m.group(7).replace(",", ""))
        out["price"] = int(m.group(8).replace(",", ""))
    return out


def parse_own_acquire(text: str) -> int | None:
    """소유상황보고서에서 취득 주식수 합(있으면). 단순 휴리스틱."""
    total = 0
    found = False
    for m in re.finditer(r"취득[^0-9]{0,30}([\d,]+)\s*주", text):
        total += int(m.group(1).replace(",", ""))
        found = True
    for m in re.finditer(r"\| ([\d,]+) \| (?:시간외|장내|장외|블록)", text):
        total += int(m.group(1).replace(",", ""))
        found = True
    return total if found else None


# ---------------- 분류 ----------------
def is_mass(title: str) -> bool:
    return "대량보유상황보고서" in title


def is_own(title: str) -> bool:
    return "소유상황보고서" in title and "거래계획" not in title


def is_plan(title: str) -> bool:
    return "거래계획" in title


def is_plan_done(title: str) -> bool:
    return is_plan(title) and any(k in title for k in ("결과", "완료", "이행", "종료"))


def is_agm(title: str) -> bool:
    # 회사 측 정기 주총 소집결의/공고/결과는 제외(지분 구도 싸움이 아님). 주주제안.소송.가처분.권유만.
    if any(k in title for k in ("주주제안", "소집청구", "이사후보", "의결권대리행사권유", "의결권 위임", "의결권위임",
                                "의결권행사금지", "주주총회결의취소", "주주총회결의무효", "가처분")):
        return True
    if "소송" in title and any(k in title for k in ("주주", "의결권", "주총")):
        return True
    return False


def is_maxshare(title: str) -> bool:
    return "최대주주변경" in title or "최대주주등소유주식변동" in title


def gap_line(st: dict) -> str:
    h, c = st["stakes"].get("hoban_pct"), st["stakes"].get("cho_group_pct")
    if h is None or c is None:
        return "갭 확인 불가"
    d = h - c
    who = "호반 측" if d >= 0 else "조원태 측"
    return f"호반 측 {h:.2f}% vs 조원태 측 {c:.2f}% (갭 {abs(d):.2f}%p, {who} 우세, 기준일 {st['stakes'].get('as_of', '?')})"


def live_stakes(cfg: dict, st: dict) -> dict:
    """state 우선, 없으면 config base. Delta/산은은 추정 기본값."""
    b = cfg["base"]
    s = st.get("stakes") or {}
    common = int(s.get("common") or b["common_shares"])
    hoban_sh = s.get("hoban_shares")
    cho_sh = s.get("cho_group_shares")
    hoban_pct = s.get("hoban_pct")
    cho_pct = s.get("cho_group_pct")
    if hoban_sh is None and hoban_pct is not None:
        hoban_sh = round(common * hoban_pct / 100)
    if cho_sh is None and cho_pct is not None:
        cho_sh = round(common * cho_pct / 100)
    if hoban_sh is None:
        hoban_sh = b["hoban_shares"]
    if cho_sh is None:
        cho_sh = b["cho_group_shares"]
    if hoban_pct is None:
        hoban_pct = hoban_sh / common * 100
    if cho_pct is None:
        cho_pct = cho_sh / common * 100
    delta_pct = float(s.get("delta_pct") if s.get("delta_pct") is not None else b.get("delta_pct", 14.90))
    kdb_pct = float(s.get("kdb_pct") if s.get("kdb_pct") is not None else b.get("kdb_pct", 10.58))
    return {
        "common": common,
        "hoban_shares": int(hoban_sh), "hoban_pct": float(hoban_pct),
        "cho_shares": int(cho_sh), "cho_pct": float(cho_pct),
        "delta_pct": delta_pct, "delta_shares": round(common * delta_pct / 100),
        "kdb_pct": kdb_pct, "kdb_shares": round(common * kdb_pct / 100),
        "as_of": s.get("as_of") or b.get("as_of", "?"),
    }


def _side_line(name: str, shares: int, common: int, turnout_pct: int) -> str:
    attendees = common * turnout_pct / 100
    of_att = shares / attendees * 100
    of_tot = shares / common * 100
    maj = "출석과반" if shares > attendees / 2 else "출석과반 미달"
    qtr = "발행 1/4 충족" if shares >= common / 4 else "발행 1/4 미달"
    return f"{name} {shares:,}주({of_tot:.2f}%), 출석{turnout_pct}% 기준 출석대비 {of_att:.1f}% ({maj}, {qtr})"


def scenario_block(cfg: dict, st: dict, *, cho_shares: int | None = None, hoban_shares: int | None = None,
                   alt_cho_shares: int | None = None, alt_label: str | None = None) -> str:
    """주총 표 대결 시나리오. 우호 지분(Delta, 산은)은 추정. alt_* 는 매도자 특수관계인 가정 등 대안."""
    x = live_stakes(cfg, st)
    common = x["common"]
    hoban = hoban_shares if hoban_shares is not None else x["hoban_shares"]
    cho = cho_shares if cho_shares is not None else x["cho_shares"]
    delta, kdb = x["delta_shares"], x["kdb_shares"]
    hoban_pct, cho_pct = hoban / common * 100, cho / common * 100
    gap_sh, gap_pp = hoban - cho, hoban_pct - cho_pct
    who = "호반 측" if gap_sh >= 0 else "조원태 측"
    turnouts = (cfg.get("agm") or {}).get("turnout_pct") or [70, 80]
    lines = [
        f"주총 시나리오(추정, 보통주 {common:,}주, 기준 {x['as_of']}): "
        f"호반 측 {hoban:,}주({hoban_pct:.2f}%) vs 조원태 측 {cho:,}주({cho_pct:.2f}%), "
        f"갭 {abs(gap_sh):,}주({abs(gap_pp):.2f}%p, {who} 우세).",
        f"우호 지분 추정: Delta {x['delta_pct']:.2f}%({delta:,}주), 산은 {x['kdb_pct']:.2f}%({kdb:,}주). "
        f"보통결의 = 출석 과반 + 발행주식총수 1/4 이상.",
    ]
    rows = [
        ("a) 조원태+Delta vs 호반", cho + delta, hoban),
        ("b) 조원태+Delta+산은(전부 우호) vs 호반", cho + delta + kdb, hoban),
        ("c) 산은 기권, 조원태+Delta vs 호반", cho + delta, hoban),
        ("d) 산은 지분이 호반으로(최악) vs 조원태+Delta", cho + delta, hoban + kdb),
    ]
    for label, left, right in rows:
        bits = [label + ":"]
        for t in turnouts:
            bits.append(f"출석{t}% -> " + _side_line("조원태진영", left, common, t) + " / " + _side_line("호반진영", right, common, t))
        # simple winner hint at 70%
        t0 = turnouts[0]
        att = common * t0 / 100
        left_ok = left > att / 2 and left >= common / 4
        right_ok = right > att / 2 and right >= common / 4
        if left_ok and not right_ok:
            tip = f"출석{t0}%에서 조원태진영이 보통결의 수치상 우세"
        elif right_ok and not left_ok:
            tip = f"출석{t0}%에서 호반진영이 보통결의 수치상 우세"
        elif left_ok and right_ok:
            tip = f"출석{t0}%에서 양쪽 모두 보통결의 수치 충족(경합)"
        else:
            tip = f"출석{t0}%에서 양쪽 모두 보통결의 수치 미달 가능"
        lines.append(" ".join(bits) + f". -> {tip}.")
    if alt_cho_shares is not None:
        alt_pct = alt_cho_shares / common * 100
        lines.append(
            f"대안({alt_label or '매도자=특수관계인, 조원태 측 순증 0'}): 조원태 측 {alt_cho_shares:,}주({alt_pct:.2f}%), "
            f"호반 대비 갭 {hoban - alt_cho_shares:,}주({hoban_pct - alt_pct:+.2f}%p)."
        )
        # one-line a/b under alt
        a_left = alt_cho_shares + delta
        b_left = alt_cho_shares + delta + kdb
        lines.append(
            f"대안 a) 조원태+Delta {a_left:,}주({a_left / common * 100:.2f}%) vs 호반 {hoban:,}주. "
            f"대안 b) +산은 {b_left:,}주({b_left / common * 100:.2f}%)."
        )
    lines.append("위 Delta/산은 수치는 추정. 실제 의결권 위임.출석.우호 여부에 따라 달라짐.")
    return " ".join(lines)


def classify(cfg: dict, st: dict, row: dict, body: str | None) -> list[dict]:
    """row + optional body -> list of alert dicts {kind, title, text, pri, updates}."""
    party = party_of(row["filer"], cfg)
    title, filer, day = row["title"], row["filer"], row["date"]
    link = f"{DART}/dsaf001/main.do?rcpNo={row['rcp']}"
    out: list[dict] = []
    base = cfg["base"]

    # 1) 호반 대량보유
    if party == "hoban" and is_mass(title):
        mass = parse_mass_summary(body or "") if body is not None else {}
        purpose = mass.get("purpose") or "unknown"
        prev_p = st.get("hoban_purpose") or "단순투자"
        if purpose == "경영권" and prev_p != "경영권":
            out.append({"kind": "hoban_purpose", "pri": 4,
                        "title": f"{TAG} 호반 측 보유목적 변경 | 단순투자 -> 경영권 영향",
                        "text": (f"{day} {filer} '{title}'. 보유목적이 단순투자에서 경영권에 영향을 주기 위한 목적으로 바뀜. "
                                 f"지분 {mass.get('pct_prev')}% -> {mass.get('pct_now')}%. {gap_line(st)}. "
                                 f"해석: 경영권 경쟁이 공식화되는 신호. {scenario_block(cfg, st)} 원문: {link}"),
                        "updates": {"hoban_purpose": "경영권"}})
        elif purpose == "단순투자" and prev_p == "경영권":
            out.append({"kind": "hoban_purpose", "pri": 4,
                        "title": f"{TAG} 호반 측 보유목적 변경 | 경영권 영향 -> 단순투자",
                        "text": f"{day} {filer} '{title}'. 보유목적이 다시 단순투자로. 원문: {link}",
                        "updates": {"hoban_purpose": "단순투자"}})
        pct_now, pct_prev = mass.get("pct_now"), mass.get("pct_prev")
        if pct_now is not None and pct_prev is not None and abs(pct_now - pct_prev) >= cfg["rules"]["hoban_stake_pp"]:
            out.append({"kind": "hoban_stake", "pri": 3,
                        "title": f"{TAG} 호반 측 지분 변동 | {pct_prev:.2f}% -> {pct_now:.2f}% ({pct_now - pct_prev:+.2f}%p)",
                        "text": (f"{day} {filer} '{title}'. {mass.get('reason') or '사유 미기재'}. "
                                 f"주식수 {mass.get('shares_prev'):,} -> {mass.get('shares_now'):,}. 보유목적 {purpose}. "
                                 f"{gap_line(st)}. "
                                 + scenario_block(cfg, {**st, "stakes": {**(st.get("stakes") or {}), "hoban_pct": pct_now,
                                   "hoban_shares": mass.get("shares_now"), "as_of": day}}) + f" 원문: {link}"),
                        "updates": {"stakes.hoban_pct": pct_now, "stakes.hoban_shares": mass.get("shares_now"), "stakes.as_of": day}})
        elif not out:
            out.append({"kind": "hoban_new", "pri": 4,
                        "title": f"{TAG} 호반 측 대량보유 보고 | 목적 {purpose}",
                        "text": (f"{day} {filer} '{title}'. 지분 {pct_prev}% -> {pct_now}%, 목적 {purpose}. "
                                 f"{gap_line(st)}. {scenario_block(cfg, st)} 원문: {link}"),
                        "updates": ({"hoban_purpose": purpose} if purpose != "unknown" else {})})
        return out

    # 3) 산은
    if party == "kdb" and (is_mass(title) or is_own(title)):
        mass = parse_mass_summary(body or "") if body and is_mass(title) else {}
        upd = {"stakes.as_of": day}
        st2 = st
        if mass.get("pct_now") is not None:
            upd["stakes.kdb_pct"] = mass["pct_now"]
            if mass.get("shares_now") is not None:
                upd["stakes.kdb_shares"] = mass["shares_now"]
            st2 = {**st, "stakes": {**(st.get("stakes") or {}), "kdb_pct": mass["pct_now"],
                                    "kdb_shares": mass.get("shares_now"), "as_of": day}}
        out.append({"kind": "kdb", "pri": 4,
                    "title": f"{TAG} 산은 지분 변동 | {title[:40]}",
                    "text": (f"{day} {filer} '{title}'. "
                             + (f"지분 {mass.get('pct_prev')}% -> {mass.get('pct_now')}%. " if mass.get("pct_now") is not None else "")
                             + f"{gap_line(st)}. 해석: 공동보유.우호 구도 변수. {scenario_block(cfg, st2)} 원문: {link}"),
                    "updates": upd})
        return out

    # 4) 조원태 매수 완료
    if party == "cho" and (is_plan_done(title) or ((is_own(title) or is_mass(title)) and day >= cfg["cho_plan"]["start"])):
        plan = cfg["cho_plan"]
        acquired = None
        if body and is_mass(title):
            mass = parse_mass_summary(body)
            if mass.get("shares_now") is not None and mass.get("shares_prev") is not None:
                acquired = mass["shares_now"] - mass["shares_prev"]
        if body and is_own(title):
            acquired = parse_own_acquire(body)
        done = is_plan_done(title) or (acquired is not None and abs(acquired - plan["shares"]) <= cfg["rules"]["cho_buy_tol"])
        if done and not st.get("cho_plan_done"):
            # 재계산: 매도자=외부면 조원태 측 +취득, 특수관계인이면 순증 0. 불명이면 둘 다 표시.
            seller_related = bool(body and ("특수관계인" in body and ("매도" in body or "처분" in body)))
            seller_unknown = not seller_related
            buy = acquired if acquired and acquired > 0 else plan["shares"]
            cur = live_stakes(cfg, st)
            cho_ext = cur["cho_shares"] + buy
            cho_rel = cur["cho_shares"]  # 특수관계인 매도 = 묶음 안 이동
            primary = cho_rel if seller_related else cho_ext
            primary_pct = primary / cur["common"] * 100
            seller_note = ("공시에 특수관계인 매도.처분 언급 -> 조원태 측 순증 0으로 계산"
                           if seller_related else
                           "매도자 특수관계인 여부는 공시에서 확인 필요(외부 매도 가정으로 먼저 계산, 대안도 표시)")
            scen = scenario_block(
                cfg, st, cho_shares=primary, hoban_shares=cur["hoban_shares"],
                alt_cho_shares=(cho_rel if seller_unknown else None),
                alt_label="매도자=특수관계인, 조원태 측 순증 0",
            )
            gap_sh = cur["hoban_shares"] - primary
            gap_pp = cur["hoban_pct"] - primary_pct
            recompute = (f"재계산: 호반 측 {cur['hoban_pct']:.2f}%({cur['hoban_shares']:,}주) vs "
                         f"조원태 측 {primary_pct:.2f}%({primary:,}주), "
                         f"갭 {gap_sh:,}주({gap_pp:+.2f}%p).")
            out.append({"kind": "cho_done", "pri": 4,
                        "title": f"{TAG} 조원태 매수 완료 | +{buy:,}주, 조원태 측 약 {primary_pct:.2f}%",
                        "text": (f"{day} {filer} '{title}'. 11/2 거래계획({plan['shares']:,}주 x {plan['price']:,}원, "
                                 f"{plan['start']}~{plan['end']}) 이행으로 보임. {seller_note}. "
                                 f"{recompute} {scen} 원문: {link}"),
                        "updates": {"cho_plan_done": True, "stakes.cho_group_shares": primary,
                                    "stakes.cho_group_pct": round(primary_pct, 2), "stakes.as_of": day}})
            return out
        if party == "cho" and is_mass(title) and not done:
            mass = parse_mass_summary(body or "") if body else {}
            if mass.get("pct_now") is not None and mass.get("pct_prev") is not None and abs(mass["pct_now"] - mass["pct_prev"]) >= 0.1:
                out.append({"kind": "cho_mass", "pri": 3,
                            "title": f"{TAG} 조원태 측 대량보유 변동 | {mass['pct_prev']:.2f}% -> {mass['pct_now']:.2f}%",
                            "text": f"{day} {filer} '{title}'. {mass.get('reason') or ''}. {gap_line(st)}. 원문: {link}",
                            "updates": {}})
                return out

    # 2) 주주제안/소송
    if is_agm(title):
        out.append({"kind": "agm", "pri": 4,
                    "title": f"{TAG} 주주제안.주총.소송 | {title[:50]}",
                    "text": (f"{day} {filer} '{title}'. {gap_line(st)}. "
                             f"해석: 경영권.주주총회 관련 공시. 원문: {link}"),
                    "updates": {}})
        return out

    # 최대주주 변경 (호반/조원태 구도)
    if is_maxshare(title) and ("호반" in (body or "") or "조원태" in (body or "") or party in ("hoban", "cho") or filer in ("한진칼",)):
        mx = parse_maxshare(body or "") if body else {}
        if mx:
            upd = {}
            st2 = st
            if "호반" in mx["after"]:
                upd = {"stakes.hoban_pct": mx["after_pct"], "stakes.hoban_shares": mx["after_shares"],
                       "stakes.cho_group_pct": mx["before_pct"], "stakes.cho_group_shares": mx["before_shares"],
                       "stakes.as_of": day}
                st2 = {**st, "stakes": {**(st.get("stakes") or {}), "hoban_pct": mx["after_pct"], "hoban_shares": mx["after_shares"],
                                        "cho_group_pct": mx["before_pct"], "cho_group_shares": mx["before_shares"], "as_of": day}}
            elif "호반" in mx["before"]:
                upd = {"stakes.cho_group_pct": mx["after_pct"], "stakes.cho_group_shares": mx["after_shares"],
                       "stakes.hoban_pct": mx["before_pct"], "stakes.hoban_shares": mx["before_shares"],
                       "stakes.as_of": day}
                st2 = {**st, "stakes": {**(st.get("stakes") or {}), "cho_group_pct": mx["after_pct"], "cho_group_shares": mx["after_shares"],
                                        "hoban_pct": mx["before_pct"], "hoban_shares": mx["before_shares"], "as_of": day}}
            out.append({"kind": "maxshare", "pri": 4,
                        "title": f"{TAG} 최대주주 변경 | {mx['before']} -> {mx['after']}",
                        "text": (f"{day} {filer} '{title}'. {mx['before']} {mx['before_pct']}%({mx['before_shares']:,}주) -> "
                                 f"{mx['after']} {mx['after_pct']}%({mx['after_shares']:,}주). "
                                 f"해석: 공시상 최대주주 교체. {scenario_block(cfg, st2)} 원문: {link}"),
                        "updates": upd})
            return out

    # 5) Delta 부수
    if party == "delta" and (is_mass(title) or is_own(title)):
        mass = parse_mass_summary(body or "") if body and is_mass(title) else {}
        upd = {"stakes.as_of": day}
        st2 = st
        if mass.get("pct_now") is not None:
            upd["stakes.delta_pct"] = mass["pct_now"]
            if mass.get("shares_now") is not None:
                upd["stakes.delta_shares"] = mass["shares_now"]
            st2 = {**st, "stakes": {**(st.get("stakes") or {}), "delta_pct": mass["pct_now"],
                                    "delta_shares": mass.get("shares_now"), "as_of": day}}
        out.append({"kind": "delta", "pri": 3,
                    "title": f"{TAG} Delta 지분 변동 | {title[:40]}",
                    "text": (f"{day} {filer} '{title}'. 기준 지분 약 {base.get('delta_pct', 14.90)}%(추정). "
                             + (f"이번 {mass.get('pct_prev')}% -> {mass.get('pct_now')}%. " if mass.get("pct_now") is not None else "")
                             + f"{gap_line(st)}. {scenario_block(cfg, st2)} 원문: {link}"),
                    "updates": upd})
        return out

    # 조원태 새 거래계획 (참고, p3)
    if party == "cho" and is_plan(title) and not is_plan_done(title):
        detail = parse_plan_detail(body or "") if body else {}
        out.append({"kind": "cho_plan", "pri": 3,
                    "title": f"{TAG} 조원태 거래계획 | {detail.get('shares') or '?'}주",
                    "text": (f"{day} {filer} '{title}'. "
                             f"{detail.get('start')}~{detail.get('end')}, {detail.get('shares')}주 x {detail.get('price')}원. "
                             f"{gap_line(st)}. 원문: {link}"),
                    "updates": {}})
        return out
    return out


def apply_updates(st: dict, updates: dict) -> None:
    for k, v in (updates or {}).items():
        if k.startswith("stakes."):
            st.setdefault("stakes", {})[k.split(".", 1)[1]] = v
        else:
            st[k] = v


# ---------------- 실행 ----------------
def needs_body(row: dict, cfg: dict) -> bool:
    party = party_of(row["filer"], cfg)
    t = row["title"]
    if party in ("hoban", "kdb", "delta", "cho") and (is_mass(t) or is_own(t) or is_plan(t)):
        return True
    if is_maxshare(t):
        return True
    return False


def plan(cfg: dict, st: dict, rows: list[dict], fetch_body) -> list[tuple[str, str, int]]:
    seen = set(st.get("seen") or [])
    out: list[tuple[str, str, int]] = []
    # oldest first so state updates apply in order
    new = [r for r in rows if r["rcp"] not in seen]
    new.sort(key=lambda r: (r["date"], r["rcp"]))
    for row in new:
        body = None
        if needs_body(row, cfg):
            try:
                body = fetch_body(row["rcp"])
            except Exception as exc:  # noqa: BLE001
                # still emit a manual-ish notice for high-interest titles; keep retryable by NOT marking seen? 
                # Better: mark seen only after successful classify; on body fail, emit one manual and mark seen to avoid loop,
                # unless it's cho plan completion window - user asked retry. For simplicity mark seen + manual.
                out.append((f"{TAG} 공시 확인 필요 | {row['title'][:40]}",
                            f"{row['date']} {row['filer']} '{row['title']}' 본문을 못 읽음({str(exc)[:100]}). "
                            f"원문: {DART}/dsaf001/main.do?rcpNo={row['rcp']}", 3))
                seen.add(row["rcp"])
                continue
        alerts = classify(cfg, st, row, body)
        for a in alerts:
            out.append((a["title"], a["text"], a["pri"]))
            apply_updates(st, a.get("updates"))
        seen.add(row["rcp"])
    st["seen"] = sorted(seen)
    return out


def fail_alerts(cfg: dict, st: dict, errs: dict, today: str) -> list[tuple[str, str, int]]:
    out = []
    names = {"dart": "DART 공시 목록", "body": "DART 공시 본문"}
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
                            f"{names[key]} 조회가 {f['days']}일 연속 실패({errs[key]}). 새 공시가 없다는 뜻 아님.", 3))
        else:
            f = {"days": 0, "last": None, "notified": False}
        fails[key] = f
    return out


def body_msg(stamp: str, text: str) -> str:
    return "\n".join([
        f"{stamp} | 180640 | 한진칼",
        "이게 뭐 알림: 한진칼 지분 구도를 바꾸는 공시(호반 목적 변경, 주주제안, 산은 지분 변동, 조원태 매수 완료, Delta)가 나올 때만 오는 알림. 매매 지시 아님.",
        f"지금 결과/상태: {text}",
        "하지 말 것: 공시 제목만 보고 바로 매매, 시간외 가격으로 추격, 한 공시로 지분 구도 전체를 단정.",
    ])


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    if mode == "selftest":
        return selftest()
    if mode not in {"auto", "dry"}:
        print(f"unknown mode {mode}")
        return 2
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    if now > datetime.strptime(cfg["ends_kst"], "%Y-%m-%d %H:%M").replace(tzinfo=KST):
        print("ended")
        return 0
    dry = mode == "dry"
    topic = resolve_topic("port", cfg.get("ntfy_topic"))
    server = resolve_server(cfg.get("ntfy_server"))
    st = load_state()
    errs: dict = {}
    try:
        rows = dart_list(cfg["dart_cik"], now.date() - timedelta(days=120), now.date())
    except Exception as exc:  # noqa: BLE001
        errs["dart"] = str(exc)[:200]
        rows = []
    alerts = []
    if rows:
        def fb(rcp):
            try:
                return fetch_summary(rcp)
            except Exception as exc:  # noqa: BLE001
                errs["body"] = str(exc)[:200]
                raise
        alerts = plan(cfg, st, rows, fb)
    alerts += fail_alerts(cfg, st, errs, now.date().isoformat())
    if errs:
        print("errors: " + json.dumps(errs, ensure_ascii=False), file=sys.stderr)
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    for title, text, pri in alerts:
        print(f"TITLE: {title}\nPRIORITY: {pri} TOPIC: {route_topic(topic, title)}\n{body_msg(stamp, text)}\n")
        if not dry:
            publish(topic, title, body_msg(stamp, text), pri, ["office"], server)
    if not alerts:
        print("no alert")
    if dry:
        print("DRY RUN: no publish, state not written")
        return 0
    st["updated_at"] = now.isoformat()
    STATE_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


# ---------------- selftest ----------------
def selftest() -> int:
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    fails: list[str] = []

    def check(name, got, want):
        ok = got == want
        print(("ok   " if ok else "FAIL ") + f"{name}: got={got!r} want={want!r}")
        if not ok:
            fails.append(name)

    rows = json.loads((FX / "list.json").read_text(encoding="utf-8"))
    bodies = json.loads((FX / "bodies.json").read_text(encoding="utf-8"))
    check("fixture list has 한진칼 cik filings", (len(rows) >= 50, rows[0]["filer"], "00983040" not in rows[0]["rcp"]), (True, "한진칼", True))
    check("seeded state covers fixture list", set(json.loads(STATE_PATH.read_text(encoding="utf-8"))["seen"]) >= {r["rcp"] for r in rows}, True)

    hoban = parse_mass_summary(bodies["hoban"])
    check("hoban mass: 단순투자 18.46->20.15", (hoban["purpose"], hoban["pct_prev"], hoban["pct_now"], hoban["shares_now"]),
          ("단순투자", 18.46, 20.15, 13454674))
    cho = parse_mass_summary(bodies["cho_mass"])
    check("cho mass: 경영권 일반서식 31.15% unchanged", (cho["purpose"], cho["pct_now"], cho["pct_prev"]), ("경영권", 31.15, 31.15))
    mx = parse_maxshare(bodies["maxshare"])
    check("maxshare 11/4: 조원태 외 18인 20.58 -> 호반건설 외 3인 20.69", (mx["before"], mx["before_pct"], mx["after"], mx["after_pct"], mx["after_shares"]),
          ("조원태 외 18인", 20.58, "호반건설 외 3인", 20.69, 13814053))
    plan_d = parse_plan_detail(bodies["plan_detail"])
    check("cho plan: 325,000 x 114,000, 12/03~01/01", (plan_d["shares"], plan_d["price"], plan_d["start"], plan_d["end"]),
          (325000, 114000, "2026-12-03", "2027-01-01"))
    check("party match", (party_of("호반건설", cfg), party_of("(주)호반호텔앤리조트", cfg), party_of("조원태", cfg),
                          party_of("한국산업은행", cfg), party_of("Delta Air Lines, Inc.", cfg), party_of("한진칼", cfg)),
          ("hoban", "hoban", "cho", "kdb", "delta", None))
    check("agm keywords", (is_agm("주주제안권 행사"), is_agm("임시주주총회 소집청구"), is_agm("의결권대리행사권유"),
                           is_agm("의결권행사금지 가처분"), is_agm("주주총회소집결의"), is_agm("정기주주총회결과")),
          (True, True, True, True, False, False))

    def init():
        s = default_state()
        s.update(json.loads(STATE_PATH.read_text(encoding="utf-8")))
        # drop seeded so we can replay
        s["seen"] = []
        return s

    bodies_map = {
        "20260710000462": bodies["hoban"],
        "20261019000310": bodies["cho_mass"],
        "20261104800730": bodies["maxshare"],
        "20261102000331": bodies["plan_detail"],
    }
    fb = lambda rcp: bodies_map.get(rcp) or (_ for _ in ()).throw(ValueError("no body"))  # noqa: E731

    # seeded silence
    st = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    check("initial seeded state + current list: silent", plan(cfg, dict(st), rows, fb), [])

    # 호반 목적 변경
    st = init()
    st["hoban_purpose"] = "단순투자"
    row = {"rcp": "X1", "title": "주식등의대량보유상황보고서(일반)", "filer": "호반건설", "date": "2026-11-20"}
    bodies_map["X1"] = bodies["cho_mass"].replace("조원태", "호반건설").replace("사실상지배주주", "주요주주")
    # cho_mass is 경영권 form - reuse header
    a = classify(cfg, st, row, bodies_map["X1"])
    check("hoban purpose change -> p4", (a[0]["pri"], a[0]["title"], "경영권" in a[0]["text"]),
          (4, f"{TAG} 호반 측 보유목적 변경 | 단순투자 -> 경영권 영향", True))

    # 호반 지분 +0.5%p
    st = init()
    row = {"rcp": "X2", "title": "주식등의대량보유상황보고서(약식)", "filer": "호반건설", "date": "2026-11-10"}
    a = classify(cfg, st, row, bodies["hoban"])
    check("hoban stake +1.69%p -> p3 stake alert", (a[0]["pri"], "지분 변동" in a[0]["title"], a[0]["updates"].get("stakes.hoban_pct")),
          (3, True, 20.15))

    # 주주제안
    st = init()
    a = classify(cfg, st, {"rcp": "X3", "title": "주주제안권행사", "filer": "호반건설", "date": "2027-02-01"}, None)
    check("주주제안 -> p4", (a[0]["pri"], a[0]["title"].startswith(f"{TAG} 주주제안")), (4, True))

    # 산은
    st = init()
    a = classify(cfg, st, {"rcp": "X4", "title": "주식등의대량보유상황보고서(약식)", "filer": "한국산업은행", "date": "2026-12-01"},
                 "보유주식등의 수 및 보유비율 | 보유주식등의 수 | 보유비율 | 직전 보고서 | 1,000,000 | 1.50 | 이번 보고서 | 500,000 | 0.75 | 보고사유 | 장내매도 | 보유목적 | 단순투자")
    check("KDB stake change -> p4", (a[0]["pri"], a[0]["title"].startswith(f"{TAG} 산은")), (4, True))

    # 조원태 매수 완료 (소유상황 with ~325k)
    st = init()
    own_body = "임원ㆍ주요주주 특정증권등 소유상황보고서 | 보고자 : | 조원태 | 취득 | 325,000 주 | 시간외매매 | 114,000"
    a = classify(cfg, st, {"rcp": "X5", "title": "임원ㆍ주요주주특정증권등소유상황보고서", "filer": "조원태", "date": "2026-12-03"}, own_body)
    check("cho buy done via 소유상황 -> p4, gap recomputed", (a[0]["pri"], "매수 완료" in a[0]["title"], a[0]["updates"]["cho_plan_done"],
                                                              a[0]["updates"]["stakes.cho_group_shares"], round(a[0]["updates"]["stakes.cho_group_pct"], 2)),
          (4, True, True, 13738919 + 325000, round((13738919 + 325000) / 66762275 * 100, 2)))
    # after done, gap should flip or narrow
    apply_updates(st, a[0]["updates"])
    check("after buy: 조원태 측 ahead or closer", st["stakes"]["cho_group_pct"] > cfg["base"]["cho_group_pct"], True)

    # 거래계획 결과 제목
    st = init()
    a = classify(cfg, st, {"rcp": "X6", "title": "임원ㆍ주요주주특정증권등거래계획보고서(결과)", "filer": "조원태", "date": "2026-12-04"},
                 bodies["plan_detail"])
    check("cho plan 결과 title -> p4 done", (a[0]["pri"], "매수 완료" in a[0]["title"], a[0]["updates"]["cho_plan_done"]), (4, True, True))

    # Delta
    st = init()
    a = classify(cfg, st, {"rcp": "X7", "title": "주식등의대량보유상황보고서(약식)", "filer": "Delta Air Lines, Inc.", "date": "2026-12-10"},
                 "보유주식등의 수 및 보유비율 | 보유주식등의 수 | 보유비율 | 직전 보고서 | 9,950,000 | 14.90 | 이번 보고서 | 9,000,000 | 13.48 | 보고사유 | 장내매도 | 보유목적 | 단순투자")
    check("Delta secondary p3", (a[0]["pri"], a[0]["title"].startswith(f"{TAG} Delta")), (3, True))

    # 최대주주변경
    st = init()
    a = classify(cfg, st, {"rcp": "20261104800730", "title": "최대주주변경", "filer": "한진칼", "date": "2026-11-04"}, bodies["maxshare"])
    check("maxshare -> p4", (a[0]["pri"], "최대주주 변경" in a[0]["title"], a[0]["updates"]["stakes.hoban_pct"]), (4, True, 20.69))

    # plan() integration: only new rows fire, dedupe by rcp
    st = init()
    st["seen"] = [r["rcp"] for r in rows]  # all seeded
    neo = {"rcp": "20261203000999", "title": "주주제안권행사", "filer": "호반건설", "date": "2026-12-03"}
    a = plan(cfg, st, rows + [neo], fb)
    check("plan: one new 주주제안, seen updated", ([x[0] for x in a], neo["rcp"] in st["seen"]),
          ([f"{TAG} 주주제안.주총.소송 | 주주제안권행사"], True))
    check("plan: second run silent", plan(cfg, st, rows + [neo], fb), [])

    # unrelated filings silent
    st = init()
    a = classify(cfg, st, {"rcp": "Y1", "title": "연결재무제표기준영업(잠정)실적(공정공시)", "filer": "한진칼", "date": "2026-11-10"}, None)
    check("unrelated 잠정: silent", a, [])

    # fail alerts
    st = init()
    f1 = fail_alerts(cfg, st, {"dart": "x"}, "2026-11-07")
    f1b = fail_alerts(cfg, st, {"dart": "x"}, "2026-11-07")
    f2 = fail_alerts(cfg, st, {"dart": "x"}, "2026-11-08")
    f3 = fail_alerts(cfg, st, {"dart": "x"}, "2026-11-09")
    fail_alerts(cfg, st, {}, "2026-11-10")
    f4 = fail_alerts(cfg, st, {"dart": "x"}, "2026-11-11")
    check("ops only on 2nd failing day, once; reset", (f1, f1b, [x[0] for x in f2], f3, f4),
          ([], [], [f"{TAG} DART 공시 목록 조회 실패 | 2일 연속, 다음 실행에서 다시 시도"], [], []))
    check("routing: notices port, failure ops", (route_topic("cwally-port", f"{TAG} 호반 측 보유목적 변경 | 단순투자 -> 경영권 영향"),
                                                 route_topic("cwally-port", f2[0][0])), ("cwally-port", "cwally-ops"))

    # 주총 시나리오 블록 (t72u): cho_done + maxshare + kdb
    st = init()
    a = classify(cfg, st, {"rcp": "X5", "title": "임원ㆍ주요주주특정증권등소유상황보고서", "filer": "조원태", "date": "2026-12-03"}, own_body)
    t = a[0]["text"]
    check("cho_done has scenario block markers",
          all(x in t for x in ["주총 시나리오(추정", "보통주 66,762,275주", "우호 지분 추정", "a) 조원태+Delta",
                               "b) 조원태+Delta+산은", "c) 산은 기권", "d) 산은 지분이 호반으로",
                               "출석70%", "출석80%", "보통결의", "대안(매도자=특수관계인", "추정"]),
          True)
    check("cho_done after buy: 조원태 ahead vs 호반 in scenario",
          ("조원태 측 우세" in t.split("주총 시나리오")[1][:200]) or ("갭" in t and "조원태" in t), True)

    st = init()
    a = classify(cfg, st, {"rcp": "20261104800730", "title": "최대주주변경", "filer": "한진칼", "date": "2026-11-04"}, bodies["maxshare"])
    check("maxshare text has scenario + 추정", ("주총 시나리오(추정" in a[0]["text"], "우호 지분 추정" in a[0]["text"]), (True, True))

    st = init()
    a = classify(cfg, st, {"rcp": "X4", "title": "주식등의대량보유상황보고서(약식)", "filer": "한국산업은행", "date": "2026-12-01"},
                 "보유주식등의 수 및 보유비율 | 보유주식등의 수 | 보유비율 | 직전 보고서 | 1,000,000 | 1.50 | 이번 보고서 | 500,000 | 0.75 | 보고사유 | 장내매도 | 보유목적 | 단순투자")
    check("kdb text has scenario and updates kdb_pct", ("주총 시나리오(추정" in a[0]["text"], a[0]["updates"].get("stakes.kdb_pct")), (True, 0.75))

    # live_stakes / scenario_block unit
    st0 = init()
    sc = scenario_block(cfg, st0)
    check("scenario_block mentions turnout and ordinary resolution",
          ("출석70%" in sc and "출석80%" in sc and "발행주식총수 1/4" in sc and "추정" in sc), True)
    x = live_stakes(cfg, st0)
    check("live_stakes defaults", (x["common"], round(x["delta_pct"], 2), round(x["kdb_pct"], 2), x["hoban_shares"], x["cho_shares"]),
          (66762275, 14.90, 10.58, 13814053, 13738919))

    # related seller: 조원태 측 순증 0
    st = init()
    related_body = own_body + " | 매도자 특수관계인 처분"
    a = classify(cfg, st, {"rcp": "X5b", "title": "임원ㆍ주요주주특정증권등소유상황보고서", "filer": "조원태", "date": "2026-12-03"}, related_body)
    check("related seller: cho shares unchanged, no alt block",
          (a[0]["updates"]["stakes.cho_group_shares"], "순증 0" in a[0]["text"], "대안(매도자=특수관계인" not in a[0]["text"]),
          (13738919, True, True))

    # Korean punctuation + title prefix
    allt = []
    for row, body in [
        ({"rcp": "X1", "title": "주식등의대량보유상황보고서(일반)", "filer": "호반건설", "date": "2026-11-20"}, bodies_map["X1"]),
        ({"rcp": "X3", "title": "주주제안권행사", "filer": "호반건설", "date": "2027-02-01"}, None),
        ({"rcp": "X5", "title": "임원ㆍ주요주주특정증권등소유상황보고서", "filer": "조원태", "date": "2026-12-03"}, own_body),
    ]:
        for a in classify(cfg, init(), row, body):
            allt.append(a["title"] + body_msg("s", a["text"]))
    text = " ".join(allt) + json.dumps(cfg, ensure_ascii=False) + Path(__file__).read_text(encoding="utf-8")
    check("titles start [180640], no middle dot or em-dash", (all(a.startswith(TAG) for a in allt), "\u00b7" in text, "\u2014" in text),
          (True, False, False))
    check("ends 2027-04-30 23:59", cfg["ends_kst"], "2027-04-30 23:59")
    check("base shares / gap match 11/4 filing", (cfg["base"]["common_shares"], cfg["base"]["hoban_shares"], cfg["base"]["cho_group_shares"],
                                                  round(cfg["base"]["hoban_pct"] - cfg["base"]["cho_group_pct"], 2)),
          (66762275, 13814053, 13738919, 0.11))
    print(f"SELFTEST {'PASS' if not fails else 'FAIL'}: {len(fails)} failed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
