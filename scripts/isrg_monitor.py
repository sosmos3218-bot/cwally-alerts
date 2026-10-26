#!/usr/bin/env python3
"""ISRG periodic watch: price zones and Monday status. Q3 scoring stays in isrgq3.

2026-10-26 (user approved): end moved to 2027-02-06, baseline refreshed to Q3 2026, and a one-time
2027 guidance verdict: a new 8-K with Item 2.02 filed in Jan or Feb 2027 (after guidance.seed_after) ->
EX-99.1 press release -> worldwide da Vinci procedure growth range for 2027 -> verdict by midpoint
(config guidance_add / guidance_cut). Dedupe by accession number in state["guidance"].
modes: auto | dry (no ntfy, no state) | force | confirm | selftest (fixtures, no network)
"""
from __future__ import annotations

import html
import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from ntfy_util import publish, resolve_server, resolve_topic  # noqa: E402

KST = timezone(timedelta(hours=9))
CFG_PATH = ROOT / "configs" / "isrg.json"
STATE_PATH = ROOT / "state" / "isrg.json"
UA = {"User-Agent": "cwally-alerts isrg research monitor research@example.com"}


def load_json(path: Path, fallback: dict) -> dict:
    if not path.exists():
        return dict(fallback)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return dict(fallback)
    out = dict(fallback)
    out.update(data)
    return out


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=25) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def quote(url: str) -> float | None:
    data = json.loads(fetch(url))
    result = (data.get("chart") or {}).get("result") or []
    if not result:
        return None
    price = (result[0].get("meta") or {}).get("regularMarketPrice")
    try:
        return float(price) if price is not None else None
    except (TypeError, ValueError):
        return None


def submissions(url: str) -> dict:
    return json.loads(fetch(url))


def latest_sec_date_from(data: dict) -> str | None:
    recent = ((data.get("filings") or {}).get("recent") or {})
    for form, day in zip(recent.get("form") or [], recent.get("filingDate") or []):
        if form in {"8-K", "10-Q", "10-K"}:
            return day
    return None


# ---------- 2027 가이던스 판정 ----------
RANGE_RE = re.compile(r"(\d{1,2}(?:\.\d+)?)\s*%\s*(?:to|-|\u2013)\s*(\d{1,2}(?:\.\d+)?)\s*%")
CUT_RE = re.compile(r"(forward-looking statements|safe harbor|about intuitive|non-gaap measures|risk factors)", re.I)


def earnings_8ks(data: dict, months: tuple[str, ...]) -> list[dict]:
    """8-K Item 2.02, filingDate 의 YYYY-MM 이 months 안. 오래된 것부터."""
    recent = ((data.get("filings") or {}).get("recent") or {})
    out = []
    for i, form in enumerate(recent.get("form") or []):
        items = (recent.get("items") or [""] * (i + 1))[i] or ""
        day = (recent.get("filingDate") or [""] * (i + 1))[i] or ""
        if form == "8-K" and "2.02" in items and day[:7] in months:
            out.append({"acc": recent["accessionNumber"][i], "date": day, "doc": (recent.get("primaryDocument") or [""] * (i + 1))[i]})
    return sorted(out, key=lambda x: x["date"])


def filing_base(cik: str, acc: str) -> str:
    return f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc.replace('-', '')}/"


def html_to_text(raw: str) -> str:
    t = re.sub(r"(?is)<(script|style).*?</\1>", " ", raw)
    t = re.sub(r"<[^>]+>", " ", t)
    return re.sub(r"\s+", " ", html.unescape(t).replace("\xa0", " ")).strip()


def ex991_text(cik: str, acc: str) -> tuple[str, str]:
    base = filing_base(cik, acc)
    names = [x.get("name", "") for x in json.loads(fetch(base + "index.json")).get("directory", {}).get("item", [])]
    pick = [n for n in names if re.search(r"ex-?99-?1|ex991", n, re.I) and n.lower().endswith((".htm", ".html", ".txt"))]
    if not pick:
        pick = [n for n in names if re.search(r"ex-?99", n, re.I) and n.lower().endswith((".htm", ".html"))]
    if not pick:
        raise RuntimeError("EX-99.1 파일 없음")
    return html_to_text(fetch(base + pick[0])), base + pick[0]


def strip_boilerplate(text: str) -> str:
    """미래예측 진술, 위험요인, 회사 소개 같은 상용 문구 이후를 잘라냄."""
    m = CUT_RE.search(text)
    return text[: m.start()] if m and m.start() > 500 else text


def find_guidance(text: str, year: int) -> dict | None:
    """'{year}' 전 세계 da Vinci 시술 성장 범위. 없으면 None."""
    body = strip_boilerplate(text)
    sents = re.split(r"(?<=[.;])\s+|\u2022", body)
    cands = []
    for s in sents:
        low = s.lower()
        if str(year) not in s or "da vinci procedure" not in low:
            continue
        m = RANGE_RE.search(s)
        if not m:
            continue
        lo, hi = float(m.group(1)), float(m.group(2))
        if not (0 <= lo <= hi <= 60):
            continue
        us_only = re.search(r"\b(u\.s\.|ous|outside of the u)", low) and "worldwide" not in low
        cands.append((0 if "worldwide" in low else (2 if us_only else 1), lo, hi, s.strip()[:300]))
    if not cands:
        return None
    rank, lo, hi, sent = sorted(cands, key=lambda c: c[0])[0]
    if rank == 2:
        return None
    return {"low": lo, "high": hi, "mid": round((lo + hi) / 2, 2), "sentence": sent}


def find_us_growth(text: str) -> str | None:
    body = strip_boilerplate(text)
    m = re.search(r"U\.S\. (?:da Vinci )?procedures? (?:grew|growth of|increased)[^.%]{0,40}?(\d{1,2}(?:\.\d+)?)%", body)
    return f"{m.group(1)}%" if m else None


def guidance_alert(cfg: dict, stamp: str, f: dict, g: dict | None, us: str | None, link: str) -> tuple[str, str, int]:
    gc = cfg["guidance_2027"]
    add, cut = gc["guidance_add"], gc["guidance_cut"]
    prior = f"{gc['prior_2026_low']:g}~{gc['prior_2026_high']:g}%"
    us_txt = us or "확인 불가"
    if g is None:
        title, pri = "[ISRG] 2027 가이던스 공시 미기재 | 확인 필요", 3
        status = (f"{f['date']} 8-K(Item 2.02, {f['acc']}) 보도자료에서 2027 전 세계 다빈치 시술 성장 범위를 읽지 못했습니다. "
                  f"범위 확인 불가, 중간값 확인 불가. 직전 2026 가이던스 {prior}. 미국 시술 성장 {us_txt}. 원문 {link}")
        action = "실적 발표 콜이나 IR 자료에서 2027 시술 성장 가이던스를 직접 확인하세요."
    else:
        rng = f"{g['low']:g}~{g['high']:g}%"
        if g["mid"] >= add:
            title, pri, action = f"[ISRG] 2027 시술 가이던스 {add:g}% 이상 | 증액 검토", 4, f"중간값 {g['mid']:g}%가 {add:g}% 이상. 증액 검토 조건 충족. 가격 구간(380달러 이하 분할)과 함께 보세요."
        elif g["mid"] < cut:
            title, pri, action = f"[ISRG] 2027 시술 가이던스 {cut:g}% 미만 | 비중 축소 검토", 4, f"중간값 {g['mid']:g}%가 {cut:g}% 미만. 비중 축소 검토 조건 충족."
        else:
            title, pri, action = f"[ISRG] 2027 시술 가이던스 {cut:g}~{add:g}% | 유지", 3, f"중간값 {g['mid']:g}%가 {cut:g}~{add:g}% 사이. 기존 계획 유지."
        status = (f"{f['date']} 8-K(Item 2.02) 보도자료: 2027 전 세계 다빈치 시술 성장 {rng}, 중간값 {g['mid']:g}%. "
                  f"직전 2026 가이던스 {prior}. 미국 시술 성장 {us_txt}. 원문 문장: \"{g['sentence'][:200]}\" 링크 {link}")
    later = ("숫자가 실린 다음 1, 2월 실적 공시가 나오면 판정이 한 번 옵니다. 가격 구간과 월요일 상태 알림은 그대로 옵니다." if g is None
             else "이 판정은 한 번만 옵니다. 가격 구간과 월요일 상태 알림은 그대로 옵니다.")
    return title, body(stamp, status, action, later,
                       "가이던스 하나로 한 번에 비중을 바꾸지 말고 나눠서 하세요.",
                       "보도자료 숫자를 콜 발언으로 바꿔 읽기. 공시 미기재를 가이던스 하향으로 읽기."), pri


def guidance_check(cfg: dict, state: dict, data: dict, stamp: str) -> list[tuple[str, str, int]]:
    gc = cfg["guidance_2027"]
    gs = state.setdefault("guidance", {"seed_after": gc["seed_after"], "seen": [], "verdict": None, "fail": {}})
    if gs.get("verdict"):
        return []
    out = []
    for f in earnings_8ks(data, tuple(gc["months"])):
        if f["date"] <= gs["seed_after"] or f["acc"] in gs["seen"]:
            continue
        try:
            text, link = ex991_text(cfg["cik"], f["acc"])
        except Exception as exc:  # noqa: BLE001
            print(f"guidance {f['acc']} fetch fail: {exc}")
            if not gs["fail"].get(f["acc"]):
                gs["fail"][f["acc"]] = stamp
                out.append(("[ISRG] 2027 가이던스 원문 조회 실패 | 다음 실행 재시도",
                            body(stamp, f"{f['date']} 8-K {f['acc']} EX-99.1 조회 실패: {str(exc)[:150]}.", "할 일 없음. 다음 평일 09:00에 다시 읽습니다.",
                                 "계속 실패하면 원문을 직접 보세요.", "이 안내로 비중을 바꾸지 마세요.", "조회 실패를 가이던스 판정으로 읽기."), 3))
            continue
        g = find_guidance(text, 2027)
        title, msg, pri = guidance_alert(cfg, stamp, f, g, find_us_growth(text), link or filing_base(cfg["cik"], f["acc"]))
        out.append((title, msg, pri))
        gs["seen"].append(f["acc"])
        if g:
            gs["verdict"] = {"acc": f["acc"], "date": f["date"], "low": g["low"], "high": g["high"], "mid": g["mid"], "title": title, "at": stamp}
            break
    return out


def zone_of(price: float, split: float, chase: float) -> str:
    if price <= split:
        return "split"
    if price >= chase:
        return "chase"
    return "fair"


def body(stamp: str, status: str, action: str, later: str, trim: str, avoid: str) -> str:
    return "\n".join(
        [
            f"{stamp} | ISRG | 변경조건 점검",
            "이게 뭐 알림: 인튜이티브 서지컬 가격 구간과 변경 조건만 보는 주기 알림입니다. 매수 지시가 아닙니다. 3분기 절차 채점은 isrgq3가 합니다.",
            f"지금 결과/상태: {status}",
            f"지금 할 일: {action}",
            f"나중에: {later}",
            f"익절/줄일 때: {trim}",
            f"하지 말 것: {avoid}",
        ]
    )


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "auto").lower()
    dry = mode == "dry" or os.environ.get("ISRG_DRY") == "1"
    cfg = json.loads(CFG_PATH.read_text(encoding="utf-8"))
    if mode == "selftest":
        return selftest(cfg)
    now = datetime.now(KST)
    today = now.date()
    stamp = now.strftime("%Y-%m-%d %H:%M KST")
    topic = resolve_topic("port", cfg.get("ntfy_topic"))
    server = resolve_server(cfg.get("ntfy_server"))
    base = cfg["baseline"]
    levels = cfg["levels"]

    if mode == "confirm":
        msg = body(
            stamp,
            f"설치 확인. 기준일 {base['price_date']} 종가 {base['price']}달러. 분할 관심 {levels['split']}달러 이하, 추격 금지 {levels['chase']}달러 이상. 선행 PER {base['fwd_pe']}배는 저평가가 아님.",
            "평일 09:00 KST morning에서만 돕니다. 구간이 바뀌거나 월요일이면 보냅니다. 그 외는 침묵입니다.",
            "상향 조건은 미국 시술 재가속, 2027 가이던스 중간값 이상, 다빈치5와 SP 가격 할인 없는 설치입니다. 10/21 절차 점수는 isrgq3.",
            "500달러 이상은 추격 금지. 보유 여부는 이 알림이 모릅니다.",
            "이 설치 확인을 매수나 매도 신호로 읽기. 조회 실패를 조건 충족으로 세기.",
        )
        title = "[ISRG] 설치 확인 | 매매 신호 아님"
        print(title)
        print(msg)
        if not dry:
            publish(topic, title, msg, 3, ["white_check_mark"], server)
        return 0

    state = load_json(
        STATE_PATH,
        {
            "updated_at": None,
            "last_price": None,
            "zone": "fair",
            "last_weekly": None,
            "last_filing": base.get("last_known_filing"),
            "filing_alerted": None,
            "fetch_fail_date": None,
        },
    )
    if today.isoformat() > cfg["valid_until"] and mode not in {"force", "dry"}:
        print(f"after {cfg['valid_until']}; silent")
        return 0

    price = None
    qerr = None
    try:
        price = quote(cfg["sources"]["quote"])
    except Exception as exc:
        qerr = str(exc)
    print(f"price={price} qerr={qerr}")

    alerts: list[tuple[str, str, int]] = []
    if price is None:
        if state.get("fetch_fail_date") != today.isoformat() or mode == "force":
            alerts.append(
                (
                    "[ISRG] 주가 조회 실패 | 구간 미확인",
                    body(
                        stamp,
                        f"주가 조회 실패. {qerr or '빈 응답'}. 380달러 이탈이 아닙니다.",
                        "증권 앱에서 ISRG 종가를 직접 보세요. 이 알림으로 구간을 판정하지 마세요.",
                        "다음 평일 09:00 KST에 재시도합니다.",
                        "이번 알림으로 사지 말고 줄이지 마세요.",
                        "조회 실패를 380달러 이탈이나 정상으로 취급.",
                    ),
                    4,
                )
            )
            state["fetch_fail_date"] = today.isoformat()
    else:
        zone = zone_of(price, levels["split"], levels["chase"])
        prev = state.get("zone") or "fair"
        changed = zone != prev
        weekly = today.weekday() == 0 and state.get("last_weekly") != today.isoformat()
        if changed or weekly or mode == "force":
            if zone == "split":
                title = "[ISRG] 380달러 이하 | 분할만, 일괄 금지"
                action = "고점 대비 멀티플이 더 내려온 구간입니다. 새 돈은 나누고, 10/21 절차 점수 전 한 번에 넣지 마세요."
            elif zone == "chase":
                title = "[ISRG] 500달러 이상 | 추격 금지"
                action = "연초 프리미엄 구간에 가깝습니다. 신규 추격 금지. 보유분은 이 알림이 모릅니다."
            else:
                title = "[ISRG] 380~500달러 | 프리미엄 유지, 일괄 금지"
                action = "저평가가 아닙니다. 관망이거나 이미 둔 분할만. 일괄 매수 금지."
            if zone == "split" and base.get("buyback_avg_price_q3"):
                action += f"\n경영진 3분기 자사주 평균 매입가 약 {base['buyback_avg_price_q3']:.0f}달러 근처"
            why = "구간 변경" if changed else "월요일 상태"
            if changed and weekly:
                why = "구간 변경, 월요일 상태"
            alerts.append(
                (
                    title,
                    body(
                        stamp,
                        f"ISRG {price:.2f}달러. 기준일 {base['price_date']} {base['price']}달러, 52주 {base['low_52w']}~{base['high_52w']}. 구간 {prev} -> {zone}. {why}. 선행 PER 기준 {base['fwd_pe']}배.",
                        action,
                        "상향은 미국 시술 재가속과 2027 가이던스 중간값 이상. 하향은 미국 시술 10% 미만과 가이던스 하향. 절차 숫자는 isrgq3 또는 IR 원문.",
                        "500달러 이상이면 추격하지 마세요. 380달러 복귀는 손절 신호가 아닙니다.",
                        f"목표가 {base['analyst_target']:.0f}달러를 안전마진으로 쓰기. 신용, 미수, 실적 전 일괄 매수.",
                    ),
                    4 if zone != "fair" else 3,
                )
            )
            state["zone"] = zone
            state["last_price"] = price
            if weekly or mode == "force":
                state["last_weekly"] = today.isoformat()
        else:
            state["last_price"] = price
            state["zone"] = zone
            print(f"price zone unchanged ({zone}); silent")

    filing = None
    ferr = None
    sub = None
    try:
        sub = submissions(cfg["sources"]["sec"])
        filing = latest_sec_date_from(sub)
    except Exception as exc:
        ferr = str(exc)
        print(f"sec fail: {exc}")
    last_filing = state.get("last_filing") or base.get("last_known_filing")
    if filing and last_filing and filing > last_filing and state.get("filing_alerted") != filing:
        alerts.append(
            (
                "[ISRG] 새 공시 | 절차 점수는 원문",
                body(
                    stamp,
                    f"SEC 8-K/10-Q/10-K {filing}. 이전 {last_filing}. 미국 시술, 가이던스, ASP는 이 알림이 채점하지 않습니다.",
                    "IR 원문에서 미국 다빈치 시술 성장과 2026, 2027 가이던스를 보세요. 10/21 창은 isrgq3가 채점합니다.",
                    "상향은 재가속과 중간값 이상 가이던스. 하향은 10% 미만과 가이던스 하향.",
                    "공시 제목만 보고 비중을 바꾸지 마세요.",
                    "이 알림의 공시 감지를 절차 성장 확정으로 읽기.",
                ),
                4,
            )
        )
        state["last_filing"] = filing
        state["filing_alerted"] = filing
    elif ferr and state.get("fetch_fail_date") != today.isoformat() and today.weekday() == 0:
        alerts.append(
            (
                "[ISRG] 공시 조회 실패 | 변경조건 미확인",
                body(
                    stamp,
                    f"SEC 조회 실패. {ferr}. 변경 조건은 미확인입니다.",
                    "investor.intuitivesurgical.com 최신 실적을 직접 보세요.",
                    "다음 월요일에 재시도합니다.",
                    "이번 알림으로 비중을 바꾸지 마세요.",
                    "조회 실패를 가이던스 유지로 읽기.",
                ),
                4,
            )
        )
        state["fetch_fail_date"] = today.isoformat()

    if sub is not None:
        alerts.extend(guidance_check(cfg, state, sub, stamp))

    for title, msg, pri in alerts:
        print(title)
        print(msg)
        print(f"topic={topic} send={not dry}")
        if not dry:
            publish(topic, title, msg, pri, ["chart_with_upwards_trend"], server)
    if not alerts:
        print("no alert")

    state["updated_at"] = now.isoformat()
    if mode != "dry":
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


FIX_Q4_2025 = ("Fourth quarter 2025 revenue was $2.87 billion. 2026 Financial Outlook The Company expects the following results "
               "for the full year of 2026: \u2022 Worldwide da Vinci procedure growth of approximately 13% to 15% in 2026, compared to 18% in 2025. "
               "\u2022 Non-GAAP* gross profit margin to be within a range of 67% and 68%. " + "x " * 300 +
               "Forward-Looking Statements This press release contains forward-looking statements, including expected procedure growth "
               "in 2027 of 9% to 10% and risk factors.")
FIX_PRELIM = ("U.S. procedures grew approximately 11% in the fourth quarter. The Company expects worldwide da Vinci procedures to "
              "increase approximately 12.5% to 14.5% in 2027 as compared with 2026. " + "x " * 300 + "Forward-Looking Statements ...")


def selftest(cfg: dict) -> int:
    stamp = "2027-01-27 09:00 KST"
    f = {"acc": "0001035267-27-000009", "date": "2027-01-26", "doc": "isrg-20270126.htm"}
    link = "https://www.sec.gov/Archives/edgar/data/1035267/000103526727000009/q426ex-991earningsrelease.htm"
    # 실제 2026-01-22 보도자료 형식(2026 연도로) 읽기, 상용 문구 안의 2027 숫자는 무시
    g = find_guidance(FIX_Q4_2025, 2026)
    assert g and (g["low"], g["high"], g["mid"]) == (13.0, 15.0, 14.0), g
    assert find_guidance(FIX_Q4_2025, 2027) is None
    g = find_guidance(FIX_PRELIM, 2027)
    assert g and g["mid"] == 13.5 and find_us_growth(FIX_PRELIM) == "11%", g
    cases = [("13% 이상", "Worldwide da Vinci procedure growth of approximately 13% to 15% in 2027."),
             ("12% 미만", "Worldwide da Vinci procedure growth of approximately 10% to 12% in 2027."),
             ("12~13%", "Worldwide da Vinci procedure growth of approximately 11.5% to 13.5% in 2027."),
             ("미기재", "The Company will provide its 2027 outlook on the conference call.")]
    for want, txt in cases:
        g = find_guidance(txt, 2027)
        t, m, p = guidance_alert(cfg, stamp, f, g, find_us_growth(txt), link)
        assert want in t and "확인 불가" not in t and "조회 실패" not in t, t
        assert "\u00b7" not in t + m and "\u2014" not in t + m, t
        print(f"-- fixture {want}\nTITLE: {t}\nPRIORITY: {p}\n{m}\n")
    # 판정 1회, 시드 이전/2026 공시 무시
    sub = {"filings": {"recent": {"form": ["8-K", "8-K", "8-K"], "items": ["2.02,9.01", "2.02,9.01", "2.02"],
                                  "filingDate": ["2027-01-26", "2027-01-13", "2026-10-20"],
                                  "accessionNumber": ["A-27-2", "A-27-1", "0001035267-26-000067"], "primaryDocument": ["a", "b", "c"]}}}
    got = earnings_8ks(sub, tuple(cfg["guidance_2027"]["months"]))
    assert [x["acc"] for x in got] == ["A-27-1", "A-27-2"], got
    # 380 이하 본문 줄
    line = f"경영진 3분기 자사주 평균 매입가 약 {cfg['baseline']['buyback_avg_price_q3']:.0f}달러 근처"
    print(f"-- fixture split zone line\n{line}\n")
    print("SELFTEST OK: fixtures only, no network, no publish, no state")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
